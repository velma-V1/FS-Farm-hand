"""Optional local language model behind the deterministic rules (companion/language.py).

    python -m companion.model --install     download and verify the runtime and model once
    python -m companion.model "come here"   interpret one sentence (diagnostics)

Runtime: llama.cpp `llama-server`, CPU-only build, on 127.0.0.1, started by the
companion with no window at below-normal priority, so it never takes VRAM or the
game's CPU time first. Model: Qwen3-4B-Instruct-2507 Q4_K_M (Apache-2.0). Both are
pinned by URL and sha256 (adapters/EVIDENCE.md, "Local language model").

The model only classifies a sentence into MODEL_SCHEMA (a flat schema llama.cpp
can enforce while decoding); the result is mapped to a command proposal and
validated against protocol/command.schema.json. Anything else is None: the
coordinator then answers NOT_UNDERSTOOD. STOP never waits for the model (rules
decide it first), and every executor path works without it. Standard library
only; no network beyond 127.0.0.1 at runtime.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
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

log = logging.getLogger("farmhand")

# Pinned artifacts (verified 2026-10-17 against the GitHub release digest and the
# Hugging Face LFS sha256 of the pinned revision).
RUNTIME = {
    "url": "https://github.com/ggml-org/llama.cpp/releases/download/b11714/llama-b11714-bin-win-cpu-x64.zip",
    "sha256": "f78eded047061a3f86372db426797400a2f1bb0ec37868d5927dd820eb3386c0",
    "size": 19662606,
    "file": "llama-b11714-bin-win-cpu-x64.zip",
}
MODEL = {
    "url": ("https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/"
            "a06e946bb6b655725eafa393f4a9745d460374c9/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"),
    "sha256": "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
    "size": 2497281120,
    "file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
}
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
MODEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["command", "place", "field"],
    "properties": {
        "command": {"enum": ["fetch_pair", "move_pair", "stop", "status", "unknown"]},
        "place": {"type": "string", "maxLength": 64},
        "field": {"type": "integer", "minimum": 0, "maximum": 9999},
    },
}
MODEL_VALIDATOR = Draft202012Validator(MODEL_SCHEMA)

# The schema is not shown to the model by llama.cpp, so the prompt describes it.
PROMPT = """You turn one sentence from a Farming Simulator player into JSON for Farmhand, a farmhand that can only:
- fetch_pair: bring an available tractor and forage wagon, hitched together.
- move_pair: move the tractor and forage wagon that are already hitched ("this", "it") somewhere.
- stop: stop or cancel what it is doing.
- status: say what it is doing.
Answer with JSON only: {"command": one of fetch_pair, move_pair, stop, status, unknown, "place": the destination \
in the player's own words, or "" for here/me/none, "field": a field number, or 0}.
Use "unknown" for anything else, including other machines, fieldwork, buying or selling. Never invent a place.
Examples:
"get me the tractor and the forage wagon" -> {"command": "fetch_pair", "place": "", "field": 0}
"could you bring the tractor with the wagon over to the cow barn" -> {"command": "fetch_pair", "place": "cow barn", "field": 0}
"bring that back to the shed" -> {"command": "move_pair", "place": "shed", "field": 0}
"get over here with it" -> {"command": "move_pair", "place": "", "field": 0}
"drive it to field 7" -> {"command": "move_pair", "place": "", "field": 7}
"how far along are you" -> {"command": "status", "place": "", "field": 0}
"hold it right there" -> {"command": "stop", "place": "", "field": 0}
"plow field 3" -> {"command": "unknown", "place": "", "field": 0}"""


def to_proposal(answer) -> dict | None:
    """The command proposal for a schema-valid model answer, or None."""
    if not MODEL_VALIDATOR.is_valid(answer) or answer["command"] == "unknown":
        return None
    proposal = {"version": 1, "command": answer["command"]}
    if answer["command"] in ("fetch_pair", "move_pair"):
        place = answer["place"].strip()
        if answer["field"] > 0:
            proposal["target"] = {"type": "field_entrance", "field": answer["field"]}
        elif place:
            proposal["target"] = {"type": "place", "name": place}
    return proposal if COMMAND_VALIDATOR.is_valid(proposal) else None


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
            "temperature": 0, "max_tokens": 64,
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
        proposal = to_proposal(answer)
        log.info("model %.1fs: %r -> %s -> %s", time.monotonic() - started, text, answer, proposal)
        return proposal


# ------------------------------------------------------------------- runtime

def install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "FS25Farmhand" / "model"


def server_command(folder: Path, port: int = PORT) -> list[str]:
    """CPU only (-ngl 0 and no device: -ngl defaults to auto), one slot, local only."""
    return [str(folder / "llama" / "llama-server.exe"), "-m", str(folder / MODEL["file"]),
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
                 popen=subprocess.Popen):
        self.folder, self.port, self.opener, self.popen = folder or install_dir(), port, opener, popen
        self._ready = threading.Event()
        self.process = self.job = None

    def installed(self) -> bool:
        return (self.folder / "llama" / "llama-server.exe").is_file() and (self.folder / MODEL["file"]).is_file()

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
        self.process = self.popen(server_command(self.folder, self.port), stdout=subprocess.DEVNULL,
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


def install(folder: Path | None = None, opener=urllib.request.urlopen) -> Path:
    folder = folder or install_dir()
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / RUNTIME["file"]
    download(RUNTIME, archive, opener)
    extract(archive, folder / "llama")
    download(MODEL, folder / MODEL["file"], opener)
    return folder


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("text", nargs="?")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.install:
        print(f"Local language model installed in {install(None)}")
        return 0
    server = ModelServer()
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
