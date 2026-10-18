"""Development/qualification only: the trusted host side of Farmhand's live FS25 tests.

Claude Code works inside a sandbox that may neither write the Windows FS25 folders
nor start Windows programs. This runner, started once by the user OUTSIDE that
sandbox (in WSL or on Windows), performs a fixed allowlist of FS25 development
operations for it and nothing else:

    status                 FS25 and the companion running? builds installed? READY?
    install-current-build  copy the checkout's build (.fs25-dev/build, made by
                           scripts/package_probe.py --build-dev) to the fixed mod and
                           companion folders, then start the companion with --live-test
    launch-fs25            start the configured FS25 executable
    restart-fs25           stop only FS25, start it again, wait for Farmhand READY
    stop-fs25              close FS25 (its own image name only; refused under a running job)
    run-hitch-test         ask the companion for the production hitch_pair (livetest)
    stop-test              the companion's production STOP
    collect                the latest test record plus bounded game/companion log lines

Two bridges, never one:
  Claude -> runner:   atomic JSON files in <repo>/.fs25-dev/host/{requests,responses}
                      (the only folder both sides can write); every request names this
                      runner's per-start token, a fresh increasing id and one op of the
                      allowlist with validated arguments; one runner per checkout
                      (runner.lock plus the heartbeat), each request claimed by one
                      atomic rename, each answer naming its runner (a token hash);
  runner -> companion: companion/livetest.py's files in <profile>/modSettings/
                      FS25_Farmhand/livetest; the companion runs the production
                      coordinator and executor (FIND ... LOCAL_MANEUVER, ALIGN_REVERSE,
                      ATTACH, VERIFY) exactly as for the player's own request.
No shell, PowerShell, CMD, Lua, Python or console text ever comes from a request;
no request names a path or a program. The targets are fixed here or by the user's
own command line when starting the runner. Standard library only, and no import
from the checkout: install it with ``install-runner`` to a folder the sandbox cannot
write, and start that copy:

    python3 scripts/fs25_host_runner.py install-runner
    python3 ~/.local/share/fs25-farmhand-host/fs25_host_runner.py serve --repo ~/projects/FS-Farm-hand
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time

VERSION = 1
SHARED = ".fs25-dev"
HOST = "host"
BUILD_DIR = "build"
# The known Windows targets (the user may override them when starting the runner only).
FS25_EXE = r"C:\XboxGames\Farming Simulator 25 (PC)\Content\x64\FarmingSimulator2025Game.exe"
FS25_PROFILE = r"C:\Users\Matt\AppData\Local\Packages\GIANTSSoftware.FarmingSimulator25PC_fa8jxm5fj0esw\LocalCache\Local"
FARMHAND_DATA = r"C:\Users\Matt\AppData\Local\FS25Farmhand"
FS25_IMAGE = "FarmingSimulator2025Game.exe"
MOD_NAME = "FS25_Farmhand"
MOD_SETTINGS = ("modSettings", "FS25_Farmhand")
LIVE_FOLDER = "livetest"
# What install-current-build may copy: exactly the built mod and companion files
# (= scripts/package_probe.py RUNTIME_FILES and APP_FILES; tests/test_livetest.py checks).
MOD_FILES = ("modDesc.xml", "FarmhandObserve.lua", "FarmhandHitch.lua", "FarmhandBridge.lua", "FarmhandProbe.lua",
             "translations/translation_en.xml")
APP_FILES = ("adapters/fs25.py", "adapters/world.py", "companion/ai.py", "companion/bridge.py",
             "companion/buildinfo.py", "companion/coordinator.py", "companion/executor.py", "companion/hitch.py",
             "companion/language.py", "companion/livetest.py", "companion/model.py", "companion/run.py",
             "companion/service.py", "companion/setup.py", "companion/voice.py", "protocol/command.schema.json",
             "protocol/frame.schema.json", "protocol/intent.schema.json", "protocol/ipc.py", "protocol/model.py")
BUILD_FILES = tuple(f"mod/{MOD_NAME}/{n}" for n in MOD_FILES) + tuple(f"app/{n}" for n in APP_FILES)
MAX_BUILD_FILE_BYTES = 2_000_000

# The allowlist: each op and the only arguments it takes (validated in parse_request).
OPS = {"status": (), "install-current-build": (), "launch-fs25": (), "restart-fs25": (), "stop-fs25": (),
       "run-hitch-test": (), "stop-test": (), "collect": ("test_id",)}
LIFECYCLE = frozenset({"install-current-build", "launch-fs25", "restart-fs25", "stop-fs25"})
REQUEST_MAX_BYTES = 4096
REQUEST_MAX_AGE_S = 60.0
POLL_S = 0.2
BEAT_S = 1.0
RUNNER_FRESH_S = 5.0           # = scripts/fs25_live_test.py RUNNER_STALE_S
COMPANION_ANSWER_S = 10.0
COMPANION_QUIT_S = 45.0
COMPANION_START_S = 30.0
GAME_CLOSE_S, GAME_KILL_S, GAME_APPEAR_S = 20.0, 15.0, 60.0
READY_S = 900.0
MAX_LOG_LINES, MAX_LINE_CHARS = 2000, 1200    # a full maneuver STEER line is ~700 characters
_ID = re.compile(r"[0-9]{1,20}")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_REQUEST = re.compile(r"request-([0-9]{1,20})\.json")
_CLAIMED = re.compile(r"\.claimed-([0-9]{1,20})\.json")
_LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


class Refused(ValueError):
    def __init__(self, code: str, **detail):
        super().__init__(code)
        self.code, self.detail = code, detail


# ---------------------------------------------------------------- the protocol
def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("DUPLICATE_KEY")
        result[key] = value
    return result


def _no_constant(_):
    raise Refused("NON_FINITE_NUMBER")


def strict_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant)
    except Refused:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused("MALFORMED") from None


def parse_request(raw: bytes, file_id: str, token: str, last_id: int, now: float) -> dict:
    """A request of the allowlist, or Refused naming why not."""
    if len(raw) > REQUEST_MAX_BYTES:
        raise Refused("TOO_LARGE")
    request = strict_json(raw)
    if not isinstance(request, dict) or set(request) != {"version", "id", "token", "op", "args", "created"}:
        raise Refused("SCHEMA_VIOLATION")
    if type(request["version"]) is not int or request["version"] != VERSION:
        raise Refused("VERSION_MISMATCH")
    if not isinstance(request["id"], str) or not _ID.fullmatch(request["id"]) or request["id"] != file_id:
        raise Refused("BAD_ID")
    if not isinstance(request["token"], str) or not secrets.compare_digest(request["token"], token):
        raise Refused("BAD_TOKEN")
    if not isinstance(request["op"], str) or request["op"] not in OPS:
        raise Refused("UNKNOWN_OP")
    args = request["args"]
    if not isinstance(args, dict) or not set(args) <= set(OPS[request["op"]]):
        raise Refused("BAD_ARGUMENTS")
    if "test_id" in args and (not isinstance(args["test_id"], str) or not _TOKEN.fullmatch(args["test_id"])):
        raise Refused("BAD_ARGUMENTS")
    created = request["created"]
    if type(created) not in (int, float) or not math.isfinite(created):
        raise Refused("SCHEMA_VIOLATION")
    if int(request["id"]) <= last_id:
        raise Refused("REPLAYED")
    if abs(now - created) > REQUEST_MAX_AGE_S:
        raise Refused("STALE")
    return request


# ---------------------------------------------------------------- the host
def local_path(windows: str) -> Path:
    """A fixed Windows target as this process reaches it (WSL: /mnt/<drive>/...)."""
    if sys.platform == "win32":
        return Path(windows)
    path = PureWindowsPath(windows)
    return Path("/mnt", path.drive.rstrip(":").lower(), *path.parts[1:])


def _plain(text: str) -> str:
    """A fixed target that may go inside a single-quoted PowerShell literal."""
    if any(c in text for c in "'\"`$\r\n\0") or not text.isprintable():
        raise Refused("UNSAFE_TARGET")
    return text


class System:
    """The few Windows programs the runner uses, each with fixed arguments."""

    def _run(self, args, timeout=30.0) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Refused("WINDOWS_UNREACHABLE", program=args[0]) from None

    def _tasks(self, condition: str) -> list[list[str]]:
        """tasklist's rows; an answer that is neither rows nor "no tasks" (e.g. WSL
        interop unavailable) is unknown, never "not running"."""
        done = self._run(["tasklist.exe", "/FI", condition, "/FO", "CSV", "/NH"])
        rows = [row for row in csv.reader(io.StringIO(done.stdout)) if len(row) > 1 and row[1].isdigit()]
        if done.returncode != 0 or (not rows and "INFO:" not in done.stdout + done.stderr):
            raise Refused("WINDOWS_UNREACHABLE", program="tasklist.exe")
        return rows

    def processes(self, image: str) -> list[int]:
        return [int(row[1]) for row in self._tasks(f"IMAGENAME eq {image}") if row[0].lower() == image.lower()]

    def image_of(self, pid: int) -> str | None:
        rows = [row for row in self._tasks(f"PID eq {int(pid)}") if row[1] == str(int(pid))]
        return rows[0][0] if rows else None

    def close_fs25(self, force: bool) -> None:
        """taskkill of FS25's own image name, the only process the runner ever ends."""
        self._run(["taskkill.exe", *(["/F"] if force else []), "/IM", FS25_IMAGE])

    def start(self, program: str, arguments: list[str], folder: str) -> None:
        """Detached, so it outlives this runner. Every value is fixed or validated."""
        if sys.platform == "win32":
            subprocess.Popen([program, *arguments], cwd=folder, close_fds=True,
                             creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | NEW_PROCESS_GROUP
            return
        quoted = " ".join(f'"{_plain(a)}"' if " " in a else _plain(a) for a in arguments)
        command = f"Start-Process -FilePath '{_plain(program)}' -WorkingDirectory '{_plain(folder)}'"
        if quoted:
            command += f" -ArgumentList '{quoted}'"
        self._run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command])


class Targets:
    """The fixed Windows locations, as Windows and as this process sees them."""

    def __init__(self, exe=FS25_EXE, profile=FS25_PROFILE, data=FARMHAND_DATA, savegame: int | None = None,
                 local=local_path):
        self.exe, self.profile, self.data, self.savegame = exe, profile, data, savegame
        for text in (exe, profile, data):
            _plain(text)
        self.local_profile, self.local_data = local(profile), local(data)
        self.local_exe = local(exe)
        self.mod = self.local_profile / "mods" / MOD_NAME
        self.settings = self.local_profile.joinpath(*MOD_SETTINGS)
        self.live = self.settings / LIVE_FOLDER
        self.game_log = self.local_profile / "log.txt"

    def game_arguments(self) -> list[str]:
        # FS25's autostart argument; unverified on the Microsoft Store build (adapters/EVIDENCE.md).
        return [] if self.savegame is None else ["-autoStartSavegameId", str(int(self.savegame))]

    def describe(self) -> dict:
        return {"exe": self.exe, "profile": self.profile, "data": self.data, "savegame": self.savegame}


def _no_link(path: Path) -> Path:
    if path.is_symlink():
        raise Refused("SYMLINK_REFUSED", path=path.name)
    return path


def _write_json(folder: Path, name: str, value: dict) -> None:
    _no_link(folder)
    temporary = folder / (name + ".tmp")
    if temporary.is_symlink():
        temporary.unlink()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1)
    for attempt in range(20):
        try:
            os.replace(temporary, folder / name)          # replaces a planted link, never follows it
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def _read_small(path: Path, limit: int) -> bytes:
    _no_link(path)
    with open(path, "rb") as handle:
        return handle.read(limit + 1)


NOFOLLOW, DIRECTORY = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_DIRECTORY", 0)
FD_SAFE = bool(NOFOLLOW and DIRECTORY and os.open in os.supports_dir_fd)


def fingerprint(token: str) -> str:
    """Names the runner in each answer (= scripts/fs25_live_test.py fingerprint), so the
    client accepts only the answer of the runner whose token it used."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()[:16]


def _lock_exclusive(fd: int) -> bool:
    """A non-blocking exclusive OS lock, held until the process ends."""
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


class SharedTree:
    """The checkout's .fs25-dev, the one folder the sandbox can also write. Every
    path is opened component by component from the checkout without following links
    (Linux/WSL), so a link planted or swapped in at any moment is refused, never
    followed: nothing outside the checkout is read or written through it. On Windows
    (no dir_fd) links are checked by path; creating them there needs privileges."""

    def __init__(self, repo: Path):
        self.repo = repo

    def _open(self, parts, create=False) -> int:
        fd = os.open(self.repo, os.O_RDONLY | DIRECTORY)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    raise Refused("NOT_FOUND", path=part) from None
                except OSError:                       # ELOOP, ENOTDIR: a link or a file
                    raise Refused("SYMLINK_REFUSED", path=part) from None
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _path(self, parts, name=None) -> Path:
        path = self.repo
        for part in (*parts, *([name] if name else [])):
            path = _no_link(path / part)
        return path

    def ensure(self, parts) -> None:
        if FD_SAFE:
            os.close(self._open(parts, create=True))
        else:
            for depth in range(1, len(parts) + 1):
                self._path(parts[:depth]).mkdir(mode=0o700, exist_ok=True)

    def names(self, parts) -> list[str]:
        if not FD_SAFE:
            return os.listdir(self._path(parts))
        fd = self._open(parts)
        try:
            return os.listdir(fd)
        finally:
            os.close(fd)

    def files(self, parts) -> set[str]:
        """Every entry below, as relative paths (links included, never followed)."""
        if not FD_SAFE:
            root = self._path(parts)
            return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}
        fd, found = self._open(parts), set()
        try:
            for folder, dirs, files, folder_fd in os.fwalk(".", dir_fd=fd, follow_symlinks=False):
                prefix = "" if folder == "." else folder[2:] + "/"
                found |= {prefix + n for n in files}
                found |= {prefix + d for d in dirs if os.path.islink(os.path.join(f"/proc/self/fd/{folder_fd}", d))}
        finally:
            os.close(fd)
        return found

    def read(self, parts, name: str, limit: int) -> bytes:
        *more, name = name.split("/")
        parts = (*parts, *more)
        if not FD_SAFE:
            return _read_small(self._path(parts, name), limit)
        fd = self._open(parts)
        try:
            try:
                handle = os.open(name, os.O_RDONLY | NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                raise Refused("NOT_FOUND", path=name) from None
            except OSError:
                raise Refused("SYMLINK_REFUSED", path=name) from None
            with os.fdopen(handle, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise Refused("NOT_A_FILE", path=name)
                return stream.read(limit + 1)
        finally:
            os.close(fd)

    def claim(self, parts, name: str, claimed: str) -> bool:
        """Rename ``name`` to ``claimed`` (a link is renamed, never followed). Only one
        process can win the rename, so two runners never both take a request."""
        try:
            if not FD_SAFE:
                os.rename(self._path(parts) / name, self._path(parts) / claimed)
                return True
            fd = self._open(parts)
            try:
                os.rename(name, claimed, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                os.close(fd)
            return True
        except FileNotFoundError:
            return False

    def open_lock(self, parts, name: str) -> int:
        """The open lock file (created if missing, a link refused)."""
        if not FD_SAFE:
            return os.open(_no_link(self._path(parts) / name), os.O_RDWR | os.O_CREAT, 0o600)
        fd = self._open(parts)
        try:
            return os.open(name, os.O_RDWR | os.O_CREAT | NOFOLLOW, 0o600, dir_fd=fd)
        except OSError as error:
            if isinstance(error, FileNotFoundError):
                raise
            raise Refused("SYMLINK_REFUSED", path=name) from None
        finally:
            os.close(fd)

    def write_json(self, parts, name: str, value: dict) -> None:
        if not FD_SAFE:
            return _write_json(self._path(parts), name, value)
        fd = self._open(parts)
        try:
            temporary = name + ".tmp"
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=1)
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)     # replaces a planted link, never follows it
        finally:
            os.close(fd)

    def unlink(self, parts, name: str) -> None:
        try:
            if not FD_SAFE:
                self._path(parts, name).unlink(missing_ok=True)
                return
            fd = self._open(parts)
            try:
                os.unlink(name, dir_fd=fd)
            finally:
                os.close(fd)
        except (FileNotFoundError, Refused):
            pass


def repo_head(repo: Path) -> str | None:
    """The checkout's commit, read from .git without running git (a consistency check
    of the build's label, not a security boundary: the sandbox can write .git too)."""
    git = repo / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head if re.fullmatch(r"[0-9a-f]{40}", head) else None
        ref = head[5:]
        if not re.fullmatch(r"refs/[A-Za-z0-9_./-]{1,200}", ref) or ".." in ref:
            return None
        if (git / ref).is_file():
            return (git / ref).read_text(encoding="utf-8").strip()
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        return None
    return None


class Companion:
    """runner -> companion: companion/livetest.py's request/response files."""

    def __init__(self, targets: Targets, clock=time.time):
        self.targets, self.clock = targets, clock
        self._last = 0

    def session(self) -> str | None:
        try:
            text = _read_small(self.targets.settings / "session.xml", 4096).decode("utf-8", "replace")
        except (OSError, Refused):
            return None
        match = re.search(r'<session id="([A-Za-z0-9_.-]{1,64})"', text)
        return match.group(1) if match else None

    def enabled(self) -> dict | None:
        try:
            value = strict_json(_read_small(self.targets.live / "enabled.json", 4096))
        except (OSError, Refused):
            return None
        return value if isinstance(value, dict) else None

    def ask(self, command: str, timeout: float = COMPANION_ANSWER_S) -> dict:
        if command not in ("status", "hitch", "stop"):
            raise Refused("UNKNOWN_COMMAND")
        if self.enabled() is None:
            raise Refused("LIVE_TEST_NOT_ENABLED")
        session = self.session()
        if session is None:
            raise Refused("COMPANION_SESSION_UNKNOWN")
        self._last = request_id = max(time.time_ns(), self._last + 1)
        folder = _no_link(self.targets.live)
        _write_json(folder, f"request-{request_id}.json", {"version": 1, "id": str(request_id), "session": session,
                                                           "command": command, "created": self.clock()})
        answer = folder / f"response-{request_id}.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if answer.exists():
                try:
                    response = strict_json(_read_small(answer, 1_000_000))
                except (OSError, Refused):
                    time.sleep(0.1)
                    continue
                answer.unlink(missing_ok=True)
                if not isinstance(response, dict) or response.get("id") != str(request_id):
                    raise Refused("COMPANION_ANSWER_MISMATCH")
                return response
            time.sleep(0.1)
        (folder / f"request-{request_id}.json").unlink(missing_ok=True)
        raise Refused("COMPANION_NO_ANSWER")

    def test(self, test_id: str | None) -> dict | None:
        """The latest (or the named) test record the companion wrote."""
        folder = _no_link(self.targets.live)
        if test_id is not None:
            paths = [folder / f"test-{test_id}.json"]
        else:
            paths = sorted(folder.glob("test-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:1]
        for path in paths:
            try:
                record = strict_json(_read_small(path, 8_000_000))
            except (OSError, Refused):
                continue
            if isinstance(record, dict):
                return record
        return None


def _later(stamp: str | None, seconds: int = 10) -> str | None:
    """A local wall time a little after ``stamp``: the game's last lines (the final
    notice) follow the companion's by a moment. Naive arithmetic: both sides print
    the same Windows local time, whatever this process's time zone."""
    if stamp is None:
        return None
    try:
        at = datetime.datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (at + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _bounded(lines: list[str]) -> list[str]:
    return [line[:MAX_LINE_CHARS] for line in lines[-MAX_LOG_LINES:]]


def game_log_window(path: Path, start: str | None, end: str | None) -> list[str]:
    """FS25's own [Farmhand], Error and Warning lines between two local times
    (log.txt and the test record both print "YYYY-mm-dd HH:MM:SS")."""
    try:
        _no_link(path)
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, Refused):
        return []
    lines, inside = [], start is None
    for line in text.splitlines():
        stamp = _LOG_TIME.match(line)
        if stamp:
            at = stamp.group(1)
            inside = (start is None or at >= start[:19]) and (end is None or at <= end[:19])
        if inside and ("[Farmhand]" in line or "Error" in line or "Warning" in line):
            lines.append(line)
    return _bounded(lines)


def summarize(record: dict, game_lines: list[str]) -> dict:
    """The facts a diagnosis starts from, picked from the logs the system already writes."""
    companion = [str(line) for line in record.get("companion_log", [])][-MAX_LOG_LINES:]
    return {
        "test_id": record.get("test_id"), "status": record.get("status"), "reason": record.get("reason"),
        "states": record.get("states"), "steps": record.get("steps"), "equipment": record.get("equipment"),
        "builds": {"companion": record.get("companion_build"), "mod": record.get("mod_build")},
        "started": record.get("started"), "ended": record.get("ended"),
        "selected": [l for l in companion if " selected tractor " in l][:3],
        "joints": [l for l in companion if " gap " in l and ("end:" in l or "start:" in l)][:20],
        "maneuver": [l for l in game_lines if "[Farmhand] maneuver" in l][:200],
        "align": [l for l in game_lines + companion if "op=align" in l][:20],
        "attach": [l for l in game_lines + companion if "op=attach" in l][:20],
        "refusals": [l for l in companion if "rejected" in l or "refused" in l][:40],
        "final_observation": record.get("final_observation"),
    }


class Runner:
    def __init__(self, repo: Path, targets: Targets, system: System | None = None, clock=time.time, say=print):
        self.say = say
        self.repo = repo.resolve()
        self.targets, self.system, self.clock = targets, system or System(), clock
        self.companion = Companion(targets, clock)
        self.tree = SharedTree(self.repo)
        self.shared = self.repo / SHARED
        self.host = self.shared / HOST
        self.requests, self.responses = self.host / "requests", self.host / "responses"
        self.HOST, self.REQUESTS, self.RESPONSES = (SHARED, HOST), (SHARED, HOST, "requests"), (SHARED, HOST, "responses")
        self.token = secrets.token_hex(16)
        self.started = clock()
        self._last_id = 0
        self._lifecycle = threading.Lock()
        self._stop = threading.Event()
        self._lock_fd: int | None = None
        self._beaten = False

    # ------------------------------------------------------------ the shared folder
    def _check_folders(self) -> None:
        """Create the shared folders; refuse any that is, or passes through, a link."""
        for parts in (self.REQUESTS, self.RESPONSES):
            self.tree.ensure(parts)

    def _runner_json(self) -> dict | None:
        try:
            state = strict_json(self.tree.read(self.HOST, "runner.json", REQUEST_MAX_BYTES))
        except Refused as error:
            if error.code in ("NOT_FOUND", "MALFORMED", "DUPLICATE_KEY", "NON_FINITE_NUMBER"):
                return None
            raise
        except OSError:
            return None
        return state if isinstance(state, dict) else None

    def take_instance(self) -> None:
        """One runner per checkout, or none. An OS lock refuses a second runner of this
        version; a fresh heartbeat of another token (an older runner, or one on the
        other side of WSL, where the lock does not reach) refuses it too."""
        self._check_folders()
        fd = self.tree.open_lock(self.HOST, "runner.lock")
        if not _lock_exclusive(fd):
            os.close(fd)
            raise Refused("ANOTHER_RUNNER", hint="another runner serves this checkout; stop it first")
        other = self._runner_json()
        if (other is not None and other.get("token") != self.token
                and isinstance(other.get("beat"), (int, float))
                and self.clock() - other["beat"] <= RUNNER_FRESH_S):
            os.close(fd)
            raise Refused("ANOTHER_RUNNER", pid=other.get("pid"),
                          hint="runner.json has a live heartbeat of another runner; stop it first")
        self._lock_fd = fd
        for name in self.tree.names(self.REQUESTS):       # claimed by a runner that died mid-read
            if _CLAIMED.fullmatch(name):
                self.tree.unlink(self.REQUESTS, name)

    def beat(self) -> None:
        self._check_folders()
        if self._beaten:
            other = self._runner_json()
            if other is not None and other.get("token") != self.token:
                raise Refused("SUPERSEDED", pid=other.get("pid"))
        self._beaten = True
        self.tree.write_json(self.HOST, "runner.json", {"version": VERSION, "token": self.token, "pid": os.getpid(),
                                                        "started": self.started, "beat": self.clock(),
                                                        "ops": sorted(OPS), "targets": self.targets.describe()})

    def _beat_loop(self) -> None:
        """Its own thread: a long operation never makes the runner look gone."""
        while not self._stop.wait(BEAT_S):
            try:
                self.beat()
            except Refused as error:
                if error.code == "SUPERSEDED":                 # runner.json is no longer ours
                    self.say("another runner took over runner.json: this runner stops", flush=True)
                    self._stop.set()
                    return
                self.say(f"heartbeat refused: {error.code}", flush=True)
            except OSError as error:
                self.say(f"heartbeat refused: {error}", flush=True)

    def serve(self) -> None:
        self.take_instance()                 # before the try: a refused runner removes nothing
        self.beat()
        self.say(f"FS25 host runner serving {self.host} (token in runner.json); Ctrl+C to stop", flush=True)
        threading.Thread(target=self._beat_loop, daemon=True, name="runner-beat").start()
        try:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except (OSError, Refused) as error:          # e.g. a planted link: refused, never followed
                    self.say(f"poll refused: {getattr(error, 'code', error)}", flush=True)
                self._stop.wait(POLL_S)
        finally:
            self._stop.set()
            try:
                ours = (self._runner_json() or {}).get("token") == self.token
            except (OSError, Refused):
                ours = False
            if ours:
                self.tree.unlink(self.HOST, "runner.json")

    def poll_once(self) -> None:
        self._check_folders()
        named = [(int(m.group(1)), n) for n in self.tree.names(self.REQUESTS) if (m := _REQUEST.fullmatch(n))]
        for _, name in sorted(named):
            file_id = _REQUEST.fullmatch(name).group(1)
            claimed = f".claimed-{file_id}.json"
            try:
                if not self.tree.claim(self.REQUESTS, name, claimed):
                    continue                                   # another process took it first
                raw = self.tree.read(self.REQUESTS, claimed, REQUEST_MAX_BYTES)
            except Refused as error:
                self.tree.unlink(self.REQUESTS, claimed)
                self._answer(file_id, None, False, error.code)
                continue
            except OSError:
                self.tree.unlink(self.REQUESTS, claimed)
                continue
            self.tree.unlink(self.REQUESTS, claimed)
            try:
                request = parse_request(raw, file_id, self.token, self._last_id, self.clock())
            except Refused as error:
                self._answer(file_id, None, False, error.code)
                continue
            self._last_id = int(file_id)
            if request["op"] in LIFECYCLE:
                if not self._lifecycle.acquire(blocking=False):
                    self._answer(file_id, request["op"], False, "LIFECYCLE_BUSY")
                    continue
                threading.Thread(target=self._lifecycle_op, args=(request,), daemon=True).start()
            else:
                self._handle(request)

    def _answer(self, request_id, op, ok, code, **result) -> None:
        self.tree.write_json(self.RESPONSES, f"response-{request_id}.json", {
            "version": VERSION, "id": request_id, "runner": fingerprint(self.token), "op": op, "ok": ok,
            "code": code, "result": result})
        self.say(f"{op or '?'} {request_id}: {code}", flush=True)

    def _lifecycle_op(self, request) -> None:
        try:
            self._handle(request)
        finally:
            self._lifecycle.release()

    def _handle(self, request) -> None:
        op = request["op"]
        try:
            ok, code, result = getattr(self, "op_" + op.replace("-", "_"))(**request["args"])
        except Refused as error:
            ok, code, result = False, error.code, error.detail
        except Exception as error:                   # reported, never a crash of the runner
            ok, code, result = False, "HOST_ERROR", {"error": f"{type(error).__name__}: {error}"[:300]}
        self._answer(request["id"], op, ok, code, **result)

    # ------------------------------------------------------------ the operations
    def _installed_builds(self) -> dict:
        def stamped(path: Path, pattern: str):
            try:
                text = _read_small(path, 200_000).decode("utf-8", "replace")
            except (OSError, Refused):
                return None
            match = re.search(pattern, text)
            return match.group(1) if match else "unstamped"
        return {"mod": stamped(self.targets.mod / "FarmhandObserve.lua", r'Observe\.BUILD = "([A-Za-z0-9_.-]+)"'),
                "app": stamped(self.targets.local_data / "app" / "companion" / "buildinfo.py",
                               r'BUILD = "([A-Za-z0-9_.-]+)"')}

    def _companion_pid(self) -> int | None:
        try:
            pid = int(_read_small(self.targets.local_data / "companion.pid", 32).decode().strip())
        except (OSError, ValueError, Refused):
            return None
        image = self.system.image_of(pid)
        return pid if image and image.lower() in ("pythonw.exe", "python.exe") else None

    def op_status(self):
        windows = None
        try:
            fs25, pid = self.system.processes(FS25_IMAGE), self._companion_pid()
        except Refused as error:                    # unknown, reported as such, never "not running"
            fs25, pid, windows = None, None, error.code
        live, problem = None, None
        try:
            answer = self.companion.ask("status")
            live = answer.get("result")
        except Refused as error:
            problem = error.code
        builds = self._installed_builds()
        head = repo_head(self.repo)
        # READY: FS25 and the companion run, the game is linked to it and runs the
        # companion's own build. hitch_ready adds the companion's other checks.
        ready = bool(fs25 and pid and live and live.get("game", {}).get("linked")
                     and not {"MOD_BUILD_MISMATCH", "COMPANION_BUILD_UNSTAMPED"} & set(live.get("problems", ())))
        return True, "STATUS", {"fs25_running": None if fs25 is None else bool(fs25), "fs25_pids": fs25,
                                "windows_problem": windows, "companion_pid": pid,
                                "installed": builds, "repo_head": head, "live": live, "live_problem": problem,
                                "farmhand_ready": ready, "hitch_ready": bool(ready and live.get("ready")),
                                "targets": self.targets.describe()}

    def _validated_build(self) -> dict:
        build = (SHARED, BUILD_DIR)
        manifest = strict_json(self.tree.read(build, "MANIFEST.json", 200_000))
        if (not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict)
                or not isinstance(manifest.get("build"), str) or not _TOKEN.fullmatch(manifest["build"])):
            raise Refused("BAD_MANIFEST")
        if set(manifest["files"]) != set(BUILD_FILES):
            raise Refused("BUILD_FILES_NOT_ALLOWED")
        head = repo_head(self.repo)
        if head is None or manifest["build"].split("-")[0] != head:
            raise Refused("BUILD_NOT_CURRENT", build=manifest["build"], repo_head=head)
        if self.tree.files(build) - set(BUILD_FILES) - {"MANIFEST.json"}:
            raise Refused("BUILD_FILES_NOT_ALLOWED")
        contents = {}
        for name in BUILD_FILES:
            data = self.tree.read(build, name, MAX_BUILD_FILE_BYTES)        # no link anywhere on the way
            if len(data) > MAX_BUILD_FILE_BYTES or hashlib.sha256(data).hexdigest() != manifest["files"][name]:
                raise Refused("BUILD_HASH_MISMATCH", file=name)
            contents[name] = data                     # what is copied: exactly the bytes checked
        return manifest, contents

    def _stop_companion(self) -> None:
        data = self.targets.local_data
        if self._companion_pid() is None:
            (data / "companion.pid").unlink(missing_ok=True)          # stale or absent: nothing to stop
            return
        (data / "quit").write_text("quit", encoding="utf-8")         # companion/setup.py QUIT_FILE
        deadline = time.monotonic() + COMPANION_QUIT_S
        while self._companion_pid() is not None:
            if time.monotonic() > deadline:
                raise Refused("COMPANION_DID_NOT_STOP")
            time.sleep(0.5)

    def _start_companion(self, build: str) -> int:
        data = self.targets.local_data
        (data / "quit").unlink(missing_ok=True)
        (self.targets.live / "enabled.json").unlink(missing_ok=True)
        program = str(PureWindowsPath(self.targets.data) / "runtime" / "pythonw.exe")
        self.system.start(program, ["-m", "companion.service", "--profile", self.targets.profile, "--live-test"],
                          self.targets.data)
        deadline = time.monotonic() + COMPANION_START_S
        while time.monotonic() < deadline:
            enabled = self.companion.enabled()
            pid = self._companion_pid()
            if enabled and pid and enabled.get("build") == build:
                return pid
            time.sleep(0.5)
        raise Refused("COMPANION_DID_NOT_START", build=build)

    @staticmethod
    def _copy(contents: dict, names, prefix: str, target: Path) -> None:
        _no_link(target)
        if target.exists():
            shutil.rmtree(target)
        for name in names:
            out = target / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(contents[f"{prefix}/{name}"])

    def op_install_current_build(self):
        if self.system.processes(FS25_IMAGE):
            raise Refused("FS25_RUNNING")              # restart-fs25 after installing, never under the game
        manifest, contents = self._validated_build()
        data = self.targets.local_data
        if not (data / "runtime" / "pythonw.exe").is_file():
            raise Refused("NO_COMPANION_RUNTIME")      # run the qualification package's Setup.cmd once
        if not self.targets.local_profile.is_dir():
            raise Refused("NO_FS25_PROFILE")
        self._stop_companion()
        (self.targets.local_profile / "mods").mkdir(exist_ok=True)
        self._copy(contents, MOD_FILES, f"mod/{MOD_NAME}", self.targets.mod)
        self._copy(contents, APP_FILES, "app", data / "app")
        pid = self._start_companion(manifest["build"])
        return True, "INSTALLED", {"build": manifest["build"], "companion_pid": pid,
                                   "installed": self._installed_builds()}

    def _launch(self):
        self.system.start(self.targets.exe, self.targets.game_arguments(), str(PureWindowsPath(self.targets.exe).parent))
        deadline = time.monotonic() + GAME_APPEAR_S
        while time.monotonic() < deadline:
            pids = self.system.processes(FS25_IMAGE)
            if pids:
                return pids
            time.sleep(1.0)
        raise Refused("FS25_DID_NOT_START")

    def op_launch_fs25(self):
        if self.system.processes(FS25_IMAGE):
            raise Refused("FS25_RUNNING")
        return True, "LAUNCHED", {"fs25_pids": self._launch(), "arguments": self.targets.game_arguments()}

    def _stop_game(self) -> list[int]:
        """Close FS25 (its image name only): asked first, forced after GAME_CLOSE_S."""
        pids = self.system.processes(FS25_IMAGE)
        if pids:
            self.system.close_fs25(force=False)
            if not self._gone(GAME_CLOSE_S):
                self.system.close_fs25(force=True)
                if not self._gone(GAME_KILL_S):
                    raise Refused("FS25_DID_NOT_STOP")
        return pids

    def op_stop_fs25(self):
        """Only FarmingSimulator2025Game.exe, and never under a running job or test
        (stop-test first): the companion's own answer decides; if it cannot answer,
        the game is closed anyway (nothing of Farmhand can then be driving)."""
        try:
            live = self.companion.ask("status")
        except Refused:
            live = {}
        result = live.get("result") or {}
        if result.get("test_active") or (result.get("job") or {}).get("running"):
            raise Refused("JOB_RUNNING", hint="stop-test first")
        pids = self._stop_game()
        return True, ("STOPPED" if pids else "NOT_RUNNING"), {"fs25_pids": pids}

    def op_restart_fs25(self, ready_timeout: float = READY_S):
        self._stop_game()
        pids = self._launch()
        deadline = time.monotonic() + ready_timeout
        status = None
        while time.monotonic() < deadline:
            _, _, status = self.op_status()
            if status["farmhand_ready"]:
                return True, "READY", {"fs25_pids": pids, "status": status}
            time.sleep(5.0)
        raise Refused("NOT_READY", status=status)

    def _gone(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.system.processes(FS25_IMAGE):
                return True
            time.sleep(1.0)
        return not self.system.processes(FS25_IMAGE)

    def op_run_hitch_test(self):
        if not self._lifecycle.acquire(blocking=False):
            raise Refused("LIFECYCLE_BUSY")
        self._lifecycle.release()
        answer = self.companion.ask("hitch")
        return bool(answer.get("ok")), answer.get("code", "UNKNOWN"), dict(answer.get("result") or {})

    def op_stop_test(self):
        answer = self.companion.ask("stop")
        return bool(answer.get("ok")), answer.get("code", "UNKNOWN"), dict(answer.get("result") or {})

    def op_collect(self, test_id: str | None = None):
        record = self.companion.test(test_id)
        if record is None:
            raise Refused("NO_TEST_RECORD")
        terminal = record.get("status") not in (None, "WORKING")
        game = (game_log_window(self.targets.game_log, record.get("started"), _later(record.get("ended")))
                if terminal else [])                    # the game's log is read once the test has ended
        return True, "TERMINAL" if terminal else "WORKING", {
            "summary": summarize(record, game), "record": {**record, "companion_log": _bounded(
                [str(l) for l in record.get("companion_log", [])])}, "game_log": game}


# ---------------------------------------------------------------- command line
INSTALL_DIR = Path.home() / ".local" / "share" / "fs25-farmhand-host"


def install_runner() -> int:
    """Copy this file outside the checkout, so the sandbox cannot change the code that
    runs on the host; re-run (and review the diff) to update it."""
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    target = INSTALL_DIR / Path(__file__).name
    data = Path(__file__).read_bytes()
    target.write_bytes(data)
    print(f"installed {target}\nsha256 {hashlib.sha256(data).hexdigest()}\n"
          f"start it (outside the Claude sandbox):\n  python3 {target} serve --repo {Path(__file__).resolve().parents[1]}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("install-runner", help="copy this runner to " + str(INSTALL_DIR))
    serve = sub.add_parser("serve", help="serve the checkout's .fs25-dev/host requests until Ctrl+C")
    serve.add_argument("--repo", type=Path, required=True, help="the Farmhand checkout Claude works in")
    serve.add_argument("--exe", default=FS25_EXE, help="FS25 executable (Windows path)")
    serve.add_argument("--profile", default=FS25_PROFILE, help="FS25 profile folder (Windows path)")
    serve.add_argument("--data", default=FARMHAND_DATA, help="Farmhand companion folder (Windows path)")
    serve.add_argument("--savegame", type=int, choices=range(1, 21), metavar="1-20",
                       help="FS25 savegame slot to auto-load on launch (-autoStartSavegameId; unverified)")
    args = parser.parse_args(argv)
    if args.action == "install-runner":
        return install_runner()
    if not (args.repo / ".git").exists():
        parser.error(f"not a checkout: {args.repo}")
    try:
        Runner(args.repo, Targets(args.exe, args.profile, args.data, args.savegame)).serve()
    except Refused as error:
        print(f"refused: {error.code} {json.dumps(error.detail)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
