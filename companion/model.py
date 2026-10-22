"""Optional local language model behind the deterministic rules (companion/language.py).

    python -m companion.model --install recommended   download and verify the runtime and a model once
    python -m companion.model "come here"   interpret one sentence (diagnostics)

Runtime: llama.cpp `llama-server`, CPU-only build, on 127.0.0.1, started by the
companion with no window at below-normal priority, so it never takes VRAM or the
game's CPU time first. Models: the user picks one of two (MANAGED; Apache-2.0),
each pinned by URL and sha256 (adapters/EVIDENCE.md, "Local language model"). A
model the user already has in Ollama is used through companion/ai.py instead.

The model only rewrites a sentence into MODEL_SCHEMA (a flat schema llama.cpp can
enforce while decoding): the command it means and the same request in the wording
the deterministic rules know ("plow field 3", "buy a large plow"). The rules then
read that wording (companion/language.py, the one parser for every capability), and
the result counts only if they reach the same command; it is validated against
protocol/command.schema.json. Anything else is None: the coordinator then answers
NOT_UNDERSTOOD. STOP never waits for the model (rules
decide it first), and every executor path works without it. Standard library
only; no network beyond 127.0.0.1 at runtime.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

from jsonschema import Draft202012Validator

from companion.coordinator import COMMAND_VALIDATOR
from companion.language import RuleInterpreter

log = logging.getLogger("farmhand")

# Pinned artifacts (verified 2026-10-17 against the GitHub release digest and the
# Hugging Face LFS sha256 of the pinned revision).
RUNTIME = {
    "url": "https://github.com/ggml-org/llama.cpp/releases/download/b11714/llama-b11714-bin-win-cpu-x64.zip",
    "sha256": "f78eded047061a3f86372db426797400a2f1bb0ec37868d5927dd820eb3386c0",
    "size": 19662606,
    "file": "llama-b11714-bin-win-cpu-x64.zip",
}
# The two Farmhand-installable models, qualified 2026-10-17 on a labelled set of
# wording the rules miss (adapters/EVIDENCE.md): about equal accuracy, the
# lightweight one about twice as fast and half the memory.
MANAGED = {
    "lightweight": {
        "title": "Lightweight (Qwen3 1.7B)",
        "url": ("https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/"
                "d7f544eead698dbd1f15126ef60b45a1e1933222/Qwen3-1.7B-Q4_K_M.gguf"),
        "sha256": "b139949c5bd74937ad8ed8c8cf3d9ffb1e99c866c823204dc42c0d91fa181897",
        "size": 1107409472,
        "file": "Qwen3-1.7B-Q4_K_M.gguf",
        "ram_gb": 2,
    },
    "recommended": {
        "title": "Recommended (Qwen3 4B)",
        "url": ("https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/"
                "a06e946bb6b655725eafa393f4a9745d460374c9/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"),
        "sha256": "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
        "size": 2497281120,
        "file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "ram_gb": 4,
    },
}
MODEL = MANAGED["recommended"]
PORT = 39231
THREADS = 4                       # of the 7800X3D's 8 cores; FS25 keeps the rest
CONTEXT = 2048
TIMEOUT_S = 10.0                  # per sentence; the rules already answered STOP
START_TIMEOUT_S = 180.0           # loading 2.5 GB from disk
BELOW_NORMAL_PRIORITY_CLASS, CREATE_NO_WINDOW = 0x00004000, 0x08000000
# 127.0.0.1 only, never through a proxy (urllib also reads the Windows registry's).
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

# What llama.cpp enforces while decoding: flat, no oneOf mixed with properties
# (grammars/README.md "Can't mix properties w/ anyOf / oneOf").
# The commands a sentence can be rewritten into; "unknown" for anything else.
MODEL_COMMANDS = ["fetch_pair", "move_pair", "hitch_pair", "detach_pair", "field_work", "buy", "save_game",
                  "haul_tractor", "contract", "sell", "store", "unload_combine", "land", "sequence", "resume",
                  "stop", "status", "unknown"]
MODEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["command", "say"],
    "properties": {
        "command": {"enum": MODEL_COMMANDS},
        "say": {"type": "string", "maxLength": 160},
    },
}
MODEL_VALIDATOR = Draft202012Validator(MODEL_SCHEMA)
RULES = RuleInterpreter()

# The schema is not shown to the model by llama.cpp, so the prompt describes it.
PROMPT = """You rewrite one sentence from a Farming Simulator player for Farmhand, a farmhand that can:
- fetch_pair: bring a tractor with an implement (forage wagon, cultivator, plow, seeder, sprayer, trailer), hitched.
- move_pair: move the tractor and implement that are already hitched ("this", "it") somewhere.
- hitch_pair: hitch a tractor to an implement where it stands.
- detach_pair: unhitch an implement where it stands.
- field_work: plow, cultivate, seed or fertilize a field (or a small test area of one).
- buy: buy a plow, a named truck or a gooseneck trailer from the store.
- haul_tractor: carry a tractor on a truck's transport trailer to a place and unload it there.
- contract: take a field contract, do it and collect the reward.
- sell / store: take the harvested grain to be sold, or into the farm's own silo.
- unload_combine: empty a combine into a trailer.
- land: buy or sell the land a field lies on.
- sequence: several of these, one after another.
- resume: go on with what was stopped.
- save_game: save the game.
- stop: stop or cancel what it is doing.
- status: say what it is doing.
Answer with JSON only: {"command": one of fetch_pair, move_pair, hitch_pair, detach_pair, field_work, buy, save_game, \
haul_tractor, contract, sell, store, unload_combine, land, sequence, resume, stop, status, unknown, \
"say": the same request in Farmhand's own words, exactly in one of these forms}:
bring a tractor and the forage wagon here | bring the <implement> here | bring the <implement> to <place> | \
bring the <implement> to field <n> | ... with the <vehicle name> | come here | take this to <place> | \
take this to field <n> | hitch the <implement> | hitch the <model number> <implement> | unhitch the <implement> | \
unhitch it | <plow|cultivate|seed|fertilize> [and ...] field <n> | ... a small area of field <n> | \
... the smallest field | ... anyway (only if the player says to work over a crop) | buy a large plow | \
buy the <name> truck | buy a gooseneck | save the game | \
use the <truck> to take the <tractor> to <place> and unload it | use the <truck> to take the <tractor> to field <n> and unload it | \
do a contract | do the <plow|cultivate|fertilize> contract on field <n> | sell the grain | store the grain | \
unload the combine | harvest field <n> | harvest field <n> and sell the grain | \
<plow|cultivate> field <n>, seed it with <crop>, then fertilize it | buy field <n> | sell field <n> | continue; \
a sequence is its parts in these forms joined by ", then ". For stop and status "say" is "".
Use "unknown" with "say" "" for anything else, and for questions or negations. Keep the player's own place and \
vehicle names; never invent a place, a field number, a name or an amount.
Examples:
"get the forage wagon hooked up and over to me" -> {"command": "fetch_pair", "say": "bring a tractor and the forage wagon here"}
"could you bring the cultivator over to the cow barn" -> {"command": "fetch_pair", "say": "bring the cultivator to the cow barn"}
"drag that combo back to the shed" -> {"command": "move_pair", "say": "take this to the shed"}
"go hook a tractor up to the 980 plow" -> {"command": "hitch_pair", "say": "hitch the 980 plow"}
"drop the seeder off" -> {"command": "detach_pair", "say": "unhitch the seeder"}
"could you get field 3 plowed and seeded" -> {"command": "field_work", "say": "plow and seed field 3"}
"pick up a big plow from the dealer for us" -> {"command": "buy", "say": "buy a large plow"}
"put the farm on disk" -> {"command": "save_game", "say": "save the game"}
"put the deutz on the gmc's trailer and bring it out to field 3" -> {"command": "haul_tractor", "say": "use the gmc to take the deutz to field 3 and unload it"}
"grab us one of those jobs off the board" -> {"command": "contract", "say": "do a contract"}
"the combine's tank is full, dump it" -> {"command": "unload_combine", "say": "unload the combine"}
"cash in the wheat" -> {"command": "sell", "say": "sell the grain"}
"get the plow through field 3, drill wheat after that and spread fertilizer" -> {"command": "field_work", "say": "plow field 3, seed it with wheat, then fertilize it"}
"pick up the land at field 12 for us" -> {"command": "land", "say": "buy field 12"}
"get rid of field 12" -> {"command": "land", "say": "sell field 12"}
"carry on where you were" -> {"command": "resume", "say": "continue"}
"empty the combine and then sell what's in the trailer" -> {"command": "sequence", "say": "unload the combine, then sell the grain"}
"how far along are you" -> {"command": "status", "say": ""}
"hold it right there" -> {"command": "stop", "say": ""}
"tell me a joke about cows" -> {"command": "unknown", "say": ""}"""


# A field operation counts only when the player said one of its words (measured: Qwen3 4B
# rewrote "collect the straw on field 5 with the forage wagon" as "cultivate field 5").
OPERATION_WORDS = {
    "plow": ("plow", "plough", "turn over", "turn the soil"),
    "cultivate": ("cultivat", "till", "disc", "disk", "harrow", "work the ground", "prepare", "prep"),
    "sow": ("sow", "seed", "plant", "drill"),
    "fertilize": ("fertili", "spray", "spread", "lime", "manure", "slurry", "feed the"),
    "harvest": ("harvest", "cut", "combine", "thresh", "reap", "bring in the"),
}


def grounded(proposal: dict, text: str) -> bool:
    """Whether what the model's rewording asks for is in the player's own sentence: every
    number (a field, an amount), every named vehicle's words, every field operation by one of
    its words. Anything else is the model inventing (AGENTS.md: the model never supplies what
    the player did not say)."""
    said = " ".join(re.findall(r"[0-9a-z]+", text.casefold()))
    numbers = set(re.findall(r"\d+", said))
    steps = proposal.get("steps") or [proposal]
    for step in steps:
        if isinstance(step.get("field"), int) and str(step["field"]) not in numbers:
            return False
        for key in ("tractor_name", "truck_name", "implement_name"):
            name = step.get(key)
            if name and not all(word in said for word in name.split()):
                return False
        for operation in step.get("operations") or ():
            if not any(word in said for word in OPERATION_WORDS.get(operation, (operation,))):
                return False
    return True


def to_proposal(answer, text: str | None = None) -> dict | None:
    """The command proposal for a schema-valid model answer, or None: status as it is;
    anything else is what the rules read from the answer's wording, only if they reach the
    model's command (hitching to a place is a fetch to it) and, given the player's ``text``,
    only if it is grounded in it."""
    if not MODEL_VALIDATOR.is_valid(answer) or answer["command"] == "unknown":
        return None
    if answer["command"] == "stop":
        # STOP is the rules' alone (they read every stop word first); a stop from the model is a
        # misreading that could cancel the player's work (measured: "don't sell the grain yet").
        return None
    if answer["command"] == "status":
        return {"version": 1, "command": answer["command"]}
    proposal = RULES.interpret(answer["say"])
    if proposal is None:
        return None
    agrees = proposal["command"] == answer["command"] or (answer["command"], proposal["command"]) == (
        "hitch_pair", "fetch_pair")
    if not agrees or not COMMAND_VALIDATOR.is_valid(proposal):
        return None
    return proposal if text is None or grounded(proposal, text) else None


class ModelInterpreter:
    """Interpreter over a local llama-server; None whenever it is not sure, down or slow."""

    def __init__(self, url: str = f"http://127.0.0.1:{PORT}", timeout: float = TIMEOUT_S,
                 ready=lambda: True, opener=LOCAL):
        self.url, self.timeout, self.ready, self.opener = url, timeout, ready, opener

    def interpret(self, text: str) -> dict | None:
        if not isinstance(text, str) or not text.strip() or not self.ready():
            return None
        body = json.dumps({
            "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": text[:200]}],
            "temperature": 0, "max_tokens": 96,
            # Hybrid "thinking" models answer directly; others ignore the flag.
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "schema": MODEL_SCHEMA},
        }).encode()
        request = urllib.request.Request(self.url + "/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
        started = time.monotonic()
        try:
            with self.opener(request, timeout=self.timeout) as response:
                reply = json.loads(response.read())
            answer = json.loads(reply["choices"][0]["message"]["content"])
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            log.info("model: no answer (%s)", type(error).__name__)
            return None
        proposal = to_proposal(answer, text)
        log.info("model %.1fs: %r -> %s -> %s", time.monotonic() - started, text, answer, proposal)
        return proposal


# ------------------------------------------------------------------- runtime

def install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "FS25Farmhand" / "model"


def server_command(folder: Path, port: int = PORT, model_file: str = MODEL["file"]) -> list[str]:
    """CPU only (-ngl 0 and no device: -ngl defaults to auto), one slot, local only.
    The managed runtime is the CPU build on purpose: it never takes VRAM from FS25;
    a GPU is used through the user's own Ollama, by Ollama's own rules."""
    return [str(folder / "llama" / "llama-server.exe"), "-m", str(folder / model_file),
            "-ngl", "0", "--device", "none", "-t", str(THREADS), "-c", str(CONTEXT), "-np", "1",
            "--host", "127.0.0.1", "--port", str(port), "--no-webui"]


def bind_to_this_process(process):
    """Windows: put ``process`` in a Job Object that kills it when its last handle
    closes, i.e. when this process ends however it ends (Task Manager included).
    Returns the job handle, which must stay referenced."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER), ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x00002000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):   # extended limits
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    return job


def close_job(job) -> None:
    import ctypes
    ctypes.WinDLL("kernel32").CloseHandle(job)


class ModelServer:
    """Starts llama-server if it is installed; ready() once /health answers from
    the process it started (never from something else on the port)."""

    def __init__(self, folder: Path | None = None, port: int = PORT, opener=LOCAL,
                 popen=subprocess.Popen, choice: str = "recommended"):
        self.folder, self.port, self.opener, self.popen = folder or install_dir(), port, opener, popen
        self.model_file = MANAGED[choice]["file"]
        self._ready = threading.Event()
        self.process = self.job = None

    def installed(self) -> bool:
        return (self.folder / "llama" / "llama-server.exe").is_file() and (self.folder / self.model_file).is_file()

    def _answers(self) -> bool:
        try:
            with self.opener(f"http://127.0.0.1:{self.port}/health", timeout=2) as response:
                return response.status == 200
        except OSError:
            return False

    def start(self) -> bool:
        if not self.installed():
            log.info("local model not installed (%s): plain commands only", self.folder)
            return False
        if self._answers():
            log.warning("port %d already answers; the local model stays off", self.port)
            return False
        flags = BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.process = self.popen(server_command(self.folder, self.port, self.model_file), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, creationflags=flags)
        if sys.platform == "win32":
            try:
                self.job = bind_to_this_process(self.process)
            except OSError:
                log.exception("cannot tie the local model to the companion; it stays off")
                self.stop()
                return False
        threading.Thread(target=self._wait_ready, daemon=True, name="farmhand-model").start()
        return True

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline and self.process.poll() is None:
            if self._answers() and self.process.poll() is None:      # our server, still running
                self._ready.set()
                log.info("local model ready")
                return
            time.sleep(1)
        log.warning("local model did not start; plain commands only")

    def ready(self) -> bool:
        return self._ready.is_set() and self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        self._ready.clear()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.job is not None:
            close_job(self.job)
            self.job = None


# ------------------------------------------------------------------ install

def download(artifact: dict, target: Path, opener=urllib.request.urlopen) -> None:
    """Fetch to a temporary name, verify size and sha256, then rename into place."""
    if target.is_file() and _sha256(target) == artifact["sha256"]:
        return
    partial = target.with_name(target.name + ".part")
    digest, size = hashlib.sha256(), 0
    with opener(artifact["url"], timeout=60) as response, open(partial, "wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    if size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
        partial.unlink()
        raise ValueError(f"{artifact['file']}: download does not match its pinned sha256")
    os.replace(partial, target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def extract(archive: Path, folder: Path) -> None:
    """Unzip with no entry outside ``folder`` (no absolute paths, no "..")."""
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            parts = PurePosixPath(name.replace("\\", "/")).parts
            if not parts or name.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
                raise ValueError(f"unsafe archive entry {name!r}")
        zf.extractall(folder)


def installed_choices(folder: Path | None = None) -> list[str]:
    folder = folder or install_dir()
    if not (folder / "llama" / "llama-server.exe").is_file():
        return []
    return [key for key, m in MANAGED.items() if (folder / m["file"]).is_file()]


def install(folder: Path | None = None, opener=urllib.request.urlopen, choice: str = "recommended") -> Path:
    folder = folder or install_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "llama" / "llama-server.exe").is_file():   # never over a running server
        archive = folder / RUNTIME["file"]
        download(RUNTIME, archive, opener)
        extract(archive, folder / "llama")
    download(MANAGED[choice], folder / MANAGED[choice]["file"], opener)
    return folder


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", choices=sorted(MANAGED))
    parser.add_argument("text", nargs="?")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.install:
        print(f"Local language model installed in {install(None, choice=args.install)}")
        return 0
    server = ModelServer(choice=(installed_choices() or ["recommended"])[0])
    if not server.start():
        return 1
    deadline = time.monotonic() + START_TIMEOUT_S
    while not server.ready() and time.monotonic() < deadline:
        time.sleep(1)
    try:
        print(json.dumps(ModelInterpreter(ready=server.ready).interpret(args.text or "")))
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
