"""Push-to-talk voice without a microphone: WAV fixtures through the device
abstraction, fakes for absent hardware, and real speech-to-text on the fixtures
when faster-whisper and the pinned model are present (FARMHAND_WHISPER_DIR)."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest

from companion import voice
from companion.language import RuleInterpreter

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "voice"
EXPECTED = {
    "fetch": {"version": 1, "command": "fetch_pair"},
    "stop": {"version": 1, "command": "stop"},
    "status": {"version": 1, "command": "status"},
    "barn": {"version": 1, "command": "move_pair", "target": {"type": "place", "name": "barn"}},
    "come": {"version": 1, "command": "move_pair"},
}


class WavMicrophone:
    """A microphone that 'hears' a fixture: the device abstraction under test."""

    def __init__(self, name):
        self.pcm = voice.read_wav(FIXTURES / f"{name}.wav")
        self.started = self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1
        return self.pcm


class Transcriber:
    """Stands in for faster-whisper: maps a fixture's audio back to its words."""

    WORDS = {"fetch": "Bring me a tractor and forage wagon.", "stop": "Stop.", "status": "What are you doing?",
             "barn": "Take this back to the barn.", "come": "Come here."}

    def __init__(self):
        self.by_audio = {voice.read_wav(FIXTURES / f"{n}.wav"): t for n, t in self.WORDS.items()}
        self.places = None

    def transcribe(self, pcm, places=()):
        self.places = list(places)
        return self.by_audio.get(pcm, "")


class ManualTimer:
    """threading.Timer stand-in that fires only when told to."""

    made = []

    def __init__(self, seconds, function):
        self.seconds, self.function, self.cancelled = seconds, function, False
        ManualTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


class VoiceInputTests(unittest.TestCase):
    def setup(self, microphone, transcriber=Transcriber):
        self.heard, self.notices, self.done = [], [], threading.Event()

        def on_text(text):
            self.heard.append(text)
            self.done.set()

        def notify(code):
            self.notices.append(code)
            if code == "NOT_HEARD":
                self.done.set()

        ManualTimer.made.clear()
        return voice.VoiceInput(microphone, transcriber, on_text, notify, places=lambda: ["Cow Barn (Medium)"],
                                timer=ManualTimer)

    def test_each_fixture_becomes_words_and_then_the_expected_command(self):
        rules = RuleInterpreter()
        for name, expected in EXPECTED.items():
            with self.subTest(fixture=name):
                transcriber = Transcriber()
                front = self.setup(lambda n=name: WavMicrophone(n), lambda t=transcriber: t)
                front.talk_start()
                front.talk_end()
                self.assertTrue(self.done.wait(5))
                self.assertEqual(self.notices, ["LISTENING"])
                self.assertEqual(rules.interpret(self.heard[0]), expected)
                self.assertEqual(transcriber.places, ["Cow Barn (Medium)"])      # the farm's words help

    def test_no_microphone_or_model_disables_only_voice(self):
        def missing():
            raise voice.Unavailable("no microphone")

        front = self.setup(missing)
        front.talk_start()
        front.talk_end()
        self.assertEqual(self.notices, ["VOICE_UNAVAILABLE"])
        self.assertEqual(self.heard, [])
        front = self.setup(lambda: WavMicrophone("stop"), missing)              # no speech model
        front.talk_start()
        self.assertEqual(self.notices, ["VOICE_UNAVAILABLE"])

    def test_a_microphone_plugged_in_later_is_found_on_the_next_press(self):
        devices = []

        def factory():
            if not devices:
                raise voice.Unavailable("no microphone")
            return devices.pop()

        front = self.setup(factory)
        front.talk_start()
        self.assertEqual(self.notices, ["VOICE_UNAVAILABLE"])
        devices.append(WavMicrophone("come"))
        front.talk_start()
        front.talk_end()
        self.assertTrue(self.done.wait(5))
        self.assertEqual(self.heard, ["Come here."])

    def test_too_short_silent_or_failing_audio_is_not_heard(self):
        class Short(WavMicrophone):
            def stop(self):
                return b"\0\0" * int(voice.SAMPLE_RATE * 0.1)

        class Broken(WavMicrophone):
            def stop(self):
                raise OSError("device lost")

        class Deaf(Transcriber):
            def transcribe(self, pcm, places=()):
                raise RuntimeError("model crashed")

        for microphone, transcriber in ((lambda: Short("stop"), Transcriber), (lambda: Broken("stop"), Transcriber),
                                        (lambda: WavMicrophone("stop"), Deaf),
                                        (lambda: WavMicrophone("stop"), lambda: type("Silent", (), {
                                            "transcribe": lambda self, pcm, places=(): ""})())):
            with self.subTest(microphone=microphone, transcriber=transcriber):
                front = self.setup(microphone, transcriber)
                front.talk_start()
                front.talk_end()
                self.assertTrue(self.done.wait(5))
                self.assertEqual(self.notices, ["LISTENING", "NOT_HEARD"])
                self.assertEqual(self.heard, [])

    def test_a_lost_key_release_still_ends_the_recording(self):
        mic = WavMicrophone("stop")
        front = self.setup(lambda: mic)
        front.talk_start()
        front.talk_start()                                   # held: no second recording
        self.assertEqual(mic.started, 1)
        timer, = ManualTimer.made
        self.assertEqual(timer.seconds, voice.MAX_SECONDS)
        timer.function()                                     # the cap fires
        self.assertTrue(self.done.wait(5))
        self.assertEqual(self.heard, ["Stop."])
        front.talk_end()                                     # the late release changes nothing
        self.assertEqual(mic.stopped, 1)


class ReplyTests(unittest.TestCase):
    def test_spoken_requests_get_spoken_replies_typed_ones_do_not(self):
        said, shown = [], []

        class Speaker:
            def say(self, text):
                said.append(text)

        replies = voice.Replies(shown.append, Speaker())
        replies("COMPLETE")                                          # typed: shown only
        for code in ("ACCEPTED", "WORKING_DELIVER", "STATUS_WORKING_ALIGN_REVERSE", "BLOCKED_NO_ELIGIBLE_PAIR",
                     "CONNECTED_X"):
            replies(code, True)
        self.assertEqual(shown, ["COMPLETE", "ACCEPTED", "WORKING_DELIVER", "STATUS_WORKING_ALIGN_REVERSE",
                                 "BLOCKED_NO_ELIGIBLE_PAIR", "CONNECTED_X"])
        self.assertEqual(said, ["Fetching a tractor and forage wagon.", "Working on it: align reverse.",
                                "I could not do that. The reason is shown on screen."])
        voice.Replies(shown.append, None)("COMPLETE", True)          # no speaker: shown only

    def test_text_to_speech_text_travels_on_stdin_only(self):
        calls = []

        class Process:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdin.close = lambda: calls.append(("stdin", self.stdin.getvalue()))

        def popen(command, **kwargs):
            calls.append(("argv", command))
            return Process()

        voice.SapiSpeaker(popen).say('Stopped."; Remove-Item C:\\ -Recurse; "')
        argv = calls[0][1]
        self.assertEqual(argv[0], "powershell.exe")
        self.assertNotIn("Remove-Item", " ".join(argv))
        self.assertEqual(calls[1], ("stdin", 'Stopped."; Remove-Item C:\\ -Recurse; "'.encode()))
        voice.SapiSpeaker(lambda *a, **k: (_ for _ in ()).throw(OSError("no powershell"))).say("x")   # never raises


class InstallTests(unittest.TestCase):
    def test_speech_model_files_are_pinned_and_verified(self):
        self.assertEqual(set(voice.WHISPER_FILES), {"model.bin", "config.json", "tokenizer.json", "vocabulary.txt"})
        self.assertEqual(voice.WHISPER_FILES["model.bin"][1],
                         "2a166925539a16005f14ff328359f9b9adb9dc4fb631bb3b227526862e93e2ef")
        urls = []

        def opener(url, timeout):
            urls.append(url)
            name = url.rsplit("/", 1)[1]
            return io.BytesIO(b"x" * voice.WHISPER_FILES[name][0])

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):                    # contents do not match the pin
                voice.install(Path(folder), opener)
        self.assertTrue(all(f"/resolve/{voice.WHISPER_REVISION}/" in u for u in urls))

    def test_wav_reader_accepts_only_the_capture_format(self):
        import wave
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stereo.wav"
            with wave.open(str(path), "wb") as clip:
                clip.setnchannels(2)
                clip.setsampwidth(2)
                clip.setframerate(16000)
                clip.writeframes(b"\0" * 64)
            with self.assertRaises(ValueError):
                voice.read_wav(path)


@unittest.skipUnless(os.environ.get("FARMHAND_WHISPER_DIR"), "real speech-to-text: set FARMHAND_WHISPER_DIR")
class RealSpeechToTextTests(unittest.TestCase):
    """faster-whisper base.en on the CPU, on the generated fixtures."""

    def test_fixtures_are_understood(self):
        folder = Path(os.environ["FARMHAND_WHISPER_DIR"])
        for name, (size, sha256) in voice.WHISPER_FILES.items():
            self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), sha256, name)
        transcriber = voice.WhisperTranscriber(folder)
        rules = RuleInterpreter()
        for name, expected in EXPECTED.items():
            with self.subTest(fixture=name):
                text = transcriber.transcribe(voice.read_wav(FIXTURES / f"{name}.wav"), ["Cow Barn (Medium)"])
                self.assertEqual(rules.interpret(text), expected, text)
        self.assertEqual(transcriber.transcribe(b"\0\0" * voice.SAMPLE_RATE * 2), "")      # silence: nothing


if __name__ == "__main__":
    unittest.main()
