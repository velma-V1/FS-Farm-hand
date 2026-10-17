"""Optional push-to-talk speech: microphone -> local speech-to-text -> the text path.

    python -m companion.voice --install        download and verify the speech model once
    python -m companion.voice clip.wav          transcribe a 16 kHz mono WAV (diagnostics)

Holding the Farmhand talk key in game (mod/FarmhandBridge.lua pushToTalk) sends
talk_start / talk_end. Between them the default microphone records (16 kHz mono,
at most MAX_SECONDS); the words are transcribed locally by faster-whisper
(`base.en`, CPU int8) and then handled exactly like typed words. Without the voice
packages, the model or a microphone, voice is simply unavailable (VOICE_UNAVAILABLE)
and typing keeps working. STOP is a key and a typed word too; speech is never the
only way to stop.

Replies to spoken requests are also spoken (Windows' built-in System.Speech through
Windows PowerShell; the text goes in on stdin, never on a command line).
Measured on generated fixtures (adapters/EVIDENCE.md "Speech"): about 0.3 s per
command on 4 CPU threads, with the domain prompt below; nothing on silence or noise.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import wave

from companion.model import download

log = logging.getLogger("farmhand")

SAMPLE_RATE = 16000
MAX_SECONDS = 15.0                # a lost talk_end still ends the recording
MIN_SECONDS = 0.3
THREADS = 4
# faster-whisper base.en (MIT) at a pinned revision; sizes and sha256 verified
# 2026-10-17 (model.bin against the Hugging Face LFS hash).
WHISPER_REVISION = "3d3d5dee26484f91867d81cb899cfcf72b96be6c"
WHISPER_FILES = {
    "model.bin": (145216508, "2a166925539a16005f14ff328359f9b9adb9dc4fb631bb3b227526862e93e2ef"),
    "config.json": (2227, "f3bc3821e9fc76a27bae538e11ae5b677dcdd352b4600429ce7951d398569aeb"),
    "tokenizer.json": (2128466, "929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df"),
    "vocabulary.txt": (422309, "ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf"),
}
# Without it "bring me a tractor" was heard as "bring the attractor" and "barn" as
# "bar" on the fixtures; with it all were exact. The farm's place names are added.
DOMAIN_PROMPT = ("Farmhand, bring me a tractor and forage wagon. Come here. Stop. What are you doing? "
                 "Take this back to the barn.")
HOTWORDS = "tractor forage wagon Farmhand barn"


def whisper_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "FS25Farmhand" / "whisper-base.en"


def install(folder: Path | None = None, opener=None) -> Path:
    folder = folder or whisper_dir()
    folder.mkdir(parents=True, exist_ok=True)
    base = f"https://huggingface.co/Systran/faster-whisper-base.en/resolve/{WHISPER_REVISION}/"
    for name, (size, sha256) in WHISPER_FILES.items():
        artifact = {"url": base + name, "file": name, "size": size, "sha256": sha256}
        download(artifact, folder / name, *([opener] if opener else []))
    return folder


# ------------------------------------------------------------------ devices

class Unavailable(Exception):
    """No voice: packages, model or microphone missing."""


class SoundDeviceMicrophone:
    """The default input device through PortAudio (sounddevice), int16 PCM."""

    def __init__(self):
        try:
            import sounddevice
        except ImportError as error:
            raise Unavailable("sounddevice not installed") from error
        try:
            sounddevice.query_devices(kind="input")
        except Exception as error:               # no input device on this PC
            raise Unavailable(f"no microphone ({error})") from error
        self._sd, self._chunks, self._stream = sounddevice, [], None

    def start(self) -> None:
        self._chunks = []
        self._stream = self._sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                               callback=lambda data, frames, t, status: self._chunks.append(bytes(data)))
        self._stream.start()

    def stop(self) -> bytes:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        return b"".join(self._chunks)


class WhisperTranscriber:
    """faster-whisper on the CPU; loaded on first use."""

    def __init__(self, folder: Path | None = None):
        self.folder = folder or whisper_dir()
        if not all((self.folder / name).is_file() for name in WHISPER_FILES):
            raise Unavailable(f"speech model not installed ({self.folder})")
        try:
            import faster_whisper
        except ImportError as error:
            raise Unavailable("faster-whisper not installed") from error
        self._module, self._model, self._lock = faster_whisper, None, threading.Lock()

    def transcribe(self, pcm: bytes, places=()) -> str:
        import numpy
        with self._lock:
            if self._model is None:
                self._model = self._module.WhisperModel(str(self.folder), device="cpu", compute_type="int8",
                                                        cpu_threads=THREADS)
            audio = numpy.frombuffer(pcm, dtype=numpy.int16).astype(numpy.float32) / 32768
            prompt = DOMAIN_PROMPT + "".join(f" {name}." for name in list(places)[:20])
            segments, _ = self._model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                                 condition_on_previous_text=False, initial_prompt=prompt,
                                                 hotwords=HOTWORDS)
            return " ".join(segment.text.strip() for segment in segments).strip()


# --------------------------------------------------------------- front end

class VoiceInput:
    """talk_start / talk_end from the game -> text for ``on_text``.

    ``microphone`` is a factory (a microphone may be plugged in later: each press
    looks again); ``transcriber`` is a factory too. ``notify`` shows a token in
    game; ``places`` returns the farm's place names for the speech prompt."""

    def __init__(self, microphone, transcriber, on_text, notify, places=lambda: (), timer=threading.Timer):
        self.microphone_factory, self.transcriber_factory = microphone, transcriber
        self.on_text, self.notify, self.places, self.timer = on_text, notify, places, timer
        self._lock = threading.Lock()
        self._mic, self._transcriber, self._limit = None, None, None

    def _get_transcriber(self):
        if self._transcriber is None:
            self._transcriber = self.transcriber_factory()
        return self._transcriber

    def talk_start(self) -> None:
        with self._lock:
            if self._mic is not None:
                return                               # already listening
            try:
                self._get_transcriber()
                mic = self.microphone_factory()
                mic.start()
            except Unavailable as error:
                log.info("voice unavailable: %s", error)
                self.notify("VOICE_UNAVAILABLE")
                return
            except Exception:
                log.exception("microphone failed")
                self.notify("VOICE_UNAVAILABLE")
                return
            self._mic = mic
            self._limit = self.timer(MAX_SECONDS, self.talk_end)
            self._limit.daemon = True
            self._limit.start()
        self.notify("LISTENING")

    def talk_end(self) -> None:
        with self._lock:
            mic, self._mic = self._mic, None
            if self._limit is not None:
                self._limit.cancel()
                self._limit = None
        if mic is None:
            return
        try:
            pcm = mic.stop()
        except Exception:
            log.exception("microphone failed")
            self.notify("NOT_HEARD")
            return
        if len(pcm) < MIN_SECONDS * SAMPLE_RATE * 2:
            self.notify("NOT_HEARD")
            return
        threading.Thread(target=self._transcribe, args=(pcm,), daemon=True, name="farmhand-stt").start()

    def _transcribe(self, pcm: bytes) -> None:
        try:
            text = self._get_transcriber().transcribe(pcm, self.places())
        except Exception:
            log.exception("speech-to-text failed")
            text = ""
        log.info("heard %r", text)
        if not text:
            self.notify("NOT_HEARD")
            return
        self.on_text(text[:200])


# ------------------------------------------------------------------ replies

PHRASES = {
    "ACCEPTED": "Fetching a tractor and forage wagon.", "ACCEPTED_MOVE": "On my way.",
    "COMPLETE": "Tractor and forage wagon delivered.", "COMPLETE_MOVE": "Arrived.",
    "CANCELLED": "Stopped.", "STOPPING": "Stopping.", "NOTHING_TO_STOP": "Nothing to stop.",
    "BUSY": "I am already working on something.", "NOT_UNDERSTOOD": "Sorry, I did not understand.",
    "NOT_HEARD": "Sorry, I did not hear that.", "STATUS_IDLE": "Nothing right now.",
}


def phrase(code: str) -> str | None:
    """What to say for a notice, or None for notices not worth speaking."""
    if code in PHRASES:
        return PHRASES[code]
    if code.startswith("WORKING_"):
        return None                                   # progress is shown, not spoken
    if code.startswith("STATUS_WORKING_"):
        return "Working on it: " + code[len("STATUS_WORKING_"):].replace("_", " ").lower() + "."
    if code.startswith("STATUS_LAST_"):
        return "Idle. The last job " + ("finished." if code.endswith("COMPLETE") else "did not finish.")
    if code.split("_", 1)[0] in ("BLOCKED", "FAILED", "UNVERIFIED"):
        return "I could not do that. The reason is shown on screen."
    return None


class SapiSpeaker:
    """Windows' built-in voice; nothing to install. Text via stdin, so no quoting.
    One reply at a time, in order, on its own thread: say() never blocks and
    replies never overlap; a stuck one is ended after SPEAK_TIMEOUT_S."""

    COMMAND = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
               "Add-Type -AssemblyName System.Speech; "
               "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak([Console]::In.ReadToEnd())"]
    SPEAK_TIMEOUT_S = 30.0
    MAX_QUEUED = 4                      # older replies are dropped rather than lag behind

    def __init__(self, popen=subprocess.Popen):
        import queue
        self.popen = popen
        self._queue = queue.Queue(self.MAX_QUEUED)
        threading.Thread(target=self._speak_in_turn, daemon=True, name="farmhand-tts").start()

    def say(self, text: str) -> None:
        import queue
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            log.info("speech output busy; dropped %r", text)

    def _speak_in_turn(self) -> None:
        flags = 0x08000000 if sys.platform == "win32" else 0      # CREATE_NO_WINDOW
        while True:
            text = self._queue.get()
            try:
                process = self.popen(self.COMMAND, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=flags)
                process.stdin.write(text.encode("utf-8"))
                process.stdin.close()
                try:
                    process.wait(timeout=self.SPEAK_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    process.kill()
            except OSError as error:
                log.info("speech output unavailable: %s", error)


class Replies:
    """Every notice goes to the game; a notice about a spoken request (or its job)
    is also spoken."""

    def __init__(self, notice, speaker=None):
        self.notice, self.speaker = notice, speaker

    def __call__(self, code: str, spoken: bool = False) -> None:
        self.notice(code)
        text = phrase(code) if spoken and self.speaker is not None else None
        if text:
            self.speaker.say(text)


def read_wav(path: Path) -> bytes:
    with wave.open(str(path)) as clip:
        if (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError("expected 16 kHz 16-bit mono")
        return clip.readframes(clip.getnframes())


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("wav", nargs="?", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.install:
        print(f"Speech model installed in {install()}")
        return 0
    print(WhisperTranscriber().transcribe(read_wav(args.wav)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
