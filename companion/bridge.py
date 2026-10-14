"""Companion side of the AC-001 FS25 command bridge, and the real World adapter.

Local only, following Farmhand Companion's proven pattern (PROTOCOL.md):
- game -> companion: JSON lines on a named pipe this process hosts (PipeServer);
- companion -> game: one command at a time in modSettings/FS25_Farmhand/command.xml,
  renamed into place atomically; the game deletes it on receipt;
- session.xml names this companion and beats once a second; the game accepts only
  this session's commands with strictly increasing ids, stops what this session
  started if the beat stops, and reports it in every snapshot.

A command's ACCEPTED and DONE results are never treated as success: FS25World
returns only the game's observed end of an operation, and the executor verifies
every step from a fresh snapshot (companion/executor.py).
"""
from __future__ import annotations

from dataclasses import replace
import logging
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from adapters.fs25 import Observation, observation_from_snapshot
from adapters.world import StepFailed, StepUnverified
from companion.hitch import STAGING_DISTANCE_M
from protocol.ipc import PIPE_PATH, FrameError, FrameReader, encode_command, encode_session
from protocol.model import Destination, Equipment, HitchPlan, Pose, Position

log = logging.getLogger("farmhand")

MOD_DIR = ("modSettings", "FS25_Farmhand")   # = mod/FarmhandBridge.lua Bridge.MOD_DIR
COMMAND_FILE, SESSION_FILE = "command.xml", "session.xml"
SNAPSHOT_TIMEOUT_S = 3.0      # game sends one every 0.5 s while linked
ACCEPT_TIMEOUT_S = 5.0        # the game reads the command file every frame
HEARTBEAT_S = 1.0             # game drops the session after 10 s without a beat
# Bounds on waiting for an operation's terminal result: the game's own bound plus
# a margin (mod/FarmhandBridge.lua, mod/FarmhandHitch.lua).
DONE_TIMEOUT_S = {"drive": 1230.0, "stage": 135.0, "align": 70.0, "attach": 25.0, "stop": 25.0}
TERMINAL = ("DONE", "FAILED", "UNVERIFIED", "REJECTED")


class CommandLink:
    """Session, command ids, and the frames the game sends back. Thread-safe."""

    def __init__(self, files, clock=time.monotonic, session: str | None = None):
        self.files = files
        self.clock = clock
        # Identifies this companion run (a replay guard, not a secret).
        self.session = session or uuid.uuid4().hex[:16]
        self._cond = threading.Condition()
        self._snapshot: dict | None = None
        self._snapshots = 0
        self._last_frame_at: float | None = None
        self._results: dict[int, list[tuple[str, str]]] = {}
        self._next_id = 1
        self._beat = 0
        self.closed = threading.Event()

    def heartbeat(self) -> None:
        if not self.closed.is_set():
            self.files.write(SESSION_FILE, encode_session(self.session, self._beat))
            self._beat += 1

    def close(self) -> None:
        """End the session: no more beats, and the game drops it (never re-adopted)."""
        self.closed.set()
        self.files.remove(SESSION_FILE)

    def on_frame(self, frame: dict) -> None:
        with self._cond:
            self._last_frame_at = self.clock()
            if frame["event"] == "snapshot":
                self._snapshot, self._snapshots = frame, self._snapshots + 1
            elif frame["event"] == "result" and frame["session"] == self.session:
                self._results.setdefault(frame["id"], []).append((frame["status"], frame["code"]))
            self._cond.notify_all()

    def _wait(self, predicate, timeout: float):
        deadline = self.clock() + timeout
        with self._cond:
            while True:
                value = predicate()
                if value is not None:
                    return value
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 0.25))

    def fresh_snapshot(self, timeout: float = SNAPSHOT_TIMEOUT_S) -> dict:
        """A snapshot received after this call started (never a cached one)."""
        with self._cond:
            seen = self._snapshots
        frame = self._wait(lambda: self._snapshot if self._snapshots > seen else None, timeout)
        if frame is None:
            raise StepUnverified("STALE_SNAPSHOT")
        return frame

    def command(self, op: str, args: dict) -> tuple[str, str]:
        """Send one command; return its terminal (status, code) from the game."""
        with self._cond:
            command_id, self._next_id = self._next_id, self._next_id + 1
        text = encode_command(self.session, command_id, op, args)
        log.info("command id=%d op=%s %s", command_id, op, args)
        self.files.write(COMMAND_FILE, text)
        first = self._wait(lambda: (self._results.get(command_id) or [None])[0], ACCEPT_TIMEOUT_S)
        if first is None:
            # Removing the file proves the game never read it; otherwise unknown.
            if self.files.remove(COMMAND_FILE):
                raise StepFailed("NOT_ACCEPTED")
            raise StepUnverified("NO_ACCEPTANCE")
        if first[0] == "REJECTED":
            raise StepFailed("REJECTED_" + first[1])

        def terminal():
            done = [r for r in self._results.get(command_id, ()) if r[0] in TERMINAL[:3]]
            if done:
                return done[0]
            # The game sends snapshots while linked; silence means the link is gone.
            if self._last_frame_at is not None and self.clock() - self._last_frame_at > 3 * SNAPSHOT_TIMEOUT_S:
                return ("LINK_LOST", "LINK_LOST")
            return None

        result = self._wait(terminal, DONE_TIMEOUT_S[op])
        if result is None or result[0] == "LINK_LOST":
            raise StepUnverified("LINK_LOST" if result else "RESULT_TIMEOUT")
        log.info("result id=%d op=%s status=%s code=%s", command_id, op, *result)
        return result


class FS25World:
    """The World port (adapters/world.py) over a CommandLink.

    Reservations are owned here, by the single companion process; the game
    re-checks ownership, occupancy and AI state itself on every frame of every
    operation. Physical capability is granted only while the latest snapshot
    names this companion's session (adapters/fs25.py)."""

    def __init__(self, link: CommandLink, categories: dict[str, str]):
        self.link = link
        self.categories = dict(categories)
        self._lock = threading.RLock()
        self._reservations: dict[str, tuple[str, tuple[str, str]]] = {}
        self._observation: Observation | None = None

    # ------------------------------------------------------------ observation
    def _observe(self) -> Observation:
        frame = self.link.fresh_snapshot()
        observation = observation_from_snapshot(frame, self.categories,
                                                bridge_ready=frame["session"] == self.link.session)
        self._observation = observation
        return observation

    def snapshot(self) -> tuple[Equipment, ...]:
        observation = self._observe()
        with self._lock:
            owners = {i: owner for owner, (_, ids) in self._reservations.items() for i in ids}
        return tuple(replace(e, reserved_by=owners.get(e.id)) for e in observation.equipment)

    def player_position(self, farm_id: str) -> Position | None:
        observation = self._observe()
        return observation.player if observation.farm_id == farm_id else None

    def destinations(self) -> tuple[Destination, ...]:
        return self._observe().destinations

    def map_id(self) -> str | None:
        return self._observe().map_id

    # ------------------------------------------------------------ reservation
    def reserve(self, ids: tuple[str, str], owner: str) -> bool:
        with self._lock:
            if not owner or len(set(ids)) != 2 or owner in self._reservations:
                return False
            items = {e.id: e for e in self.snapshot()}
            if any(i not in items for i in ids):
                return False
            pair = [items[i] for i in ids]
            if any(e.reserved_by is not None or e.busy or e.occupied or not e.available
                   or e.attached_to is not None or not e.stopped for e in pair):
                return False
            if pair[0].farm_id != pair[1].farm_id:
                return False
            self._reservations[owner] = (pair[0].farm_id, tuple(ids))
            log.info("reserved tractor=%s wagon=%s for %s", ids[0], ids[1], owner)
            return True

    def _reserved(self, tractor_id: str, owner: str, wagon_id: str | None = None) -> tuple[str, str]:
        with self._lock:
            reservation = self._reservations.get(owner)
        if reservation is None or reservation[1][0] != tractor_id or wagon_id not in (None, reservation[1][1]):
            raise StepFailed("NOT_RESERVED")
        return reservation[1]

    def release(self, owner: str) -> None:
        with self._lock:
            reservation = self._reservations.get(owner)
            if reservation is None:
                return
            items = {e.id: e for e in self._observe().equipment}
            if any(i not in items or not items[i].stopped for i in reservation[1]):
                raise ValueError("Cannot release moving or missing equipment")
            del self._reservations[owner]
            log.info("released %s", reservation[1])

    # ------------------------------------------------------------- operations
    def _run(self, op: str, **args) -> None:
        status, code = self.link.command(op, args)
        if status == "FAILED":
            raise StepFailed(code)
        if status == "UNVERIFIED":
            raise StepUnverified(code)

    def travel(self, tractor_id: str, destination_id: str, owner: str) -> None:
        self._reserved(tractor_id, owner)
        self._run("drive", tractor=int(tractor_id), marker=int(destination_id))

    def stage(self, tractor_id: str, wagon_id: str, pose: Pose, owner: str) -> None:
        self._reserved(tractor_id, owner, wagon_id)
        self._run("stage", tractor=int(tractor_id), x=pose.x, z=pose.z, yaw=pose.yaw)

    def align(self, tractor_id: str, wagon_id: str, plan: HitchPlan, owner: str) -> None:
        self._reserved(tractor_id, owner, wagon_id)
        self._run("align", tractor=int(tractor_id), wagon=int(wagon_id), tractorJoint=plan.tractor_joint,
                  wagonJoint=plan.wagon_joint, maxDistanceM=STAGING_DISTANCE_M + 2)

    def attach(self, tractor_id: str, wagon_id: str, joints: tuple[int, int], owner: str) -> None:
        self._reserved(tractor_id, owner, wagon_id)
        self._run("attach", tractor=int(tractor_id), wagon=int(wagon_id), tractorJoint=joints[0],
                  wagonJoint=joints[1])

    def deliver(self, tractor_id: str, destination_id: str, owner: str) -> None:
        self._reserved(tractor_id, owner)
        self._run("drive", tractor=int(tractor_id), marker=int(destination_id))

    def stop(self, tractor_id: str, owner: str) -> bool:
        try:
            self._reserved(tractor_id, owner)
            return self.link.command("stop", {"tractor": int(tractor_id)})[0] == "DONE"
        except (StepFailed, StepUnverified, ValueError):
            return False


# ------------------------------------------------------------------ local I/O

class ModSettingsDir:
    """The game's modSettings/FS25_Farmhand folder (Farmhand Companion PROTOCOL.md)."""

    def __init__(self, path: Path):
        self.path = path

    def write(self, name: str, text: str) -> None:
        temporary = self.path / (name + ".tmp")
        temporary.write_text(text, encoding="utf-8", newline="\n")
        # Atomic: never a half-written command. The game may hold the target open
        # for a moment while reading it (FH Transport.lua:206-211), so retry briefly.
        for attempt in range(20):
            try:
                os.replace(temporary, self.path / name)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)

    def remove(self, name: str) -> bool:
        try:
            os.remove(self.path / name)
            return True
        except FileNotFoundError:
            return False


def game_profile_dir() -> Path:
    """Documents/My Games/FarmingSimulator2025, honouring a redirected Documents."""
    import ctypes  # Windows only
    buffer = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) != 0:   # CSIDL_PERSONAL
        raise OSError("Documents folder not found")
    return Path(buffer.value) / "My Games" / "FarmingSimulator2025"


class PipeServer(threading.Thread):
    """Hosts PIPE_PATH (Windows) and feeds every decoded frame to a CommandLink.
    The game is the write-only client and reconnects by itself (FH Transport.lua)."""

    def __init__(self, link: CommandLink, path: str = PIPE_PATH):
        super().__init__(daemon=True, name="fs25-pipe")
        self.link, self.path = link, path

    def run(self) -> None:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle_t, dword = wintypes.HANDLE, wintypes.DWORD
        kernel32.CreateNamedPipeW.restype = handle_t
        kernel32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR] + [dword] * 6 + [wintypes.LPVOID]
        kernel32.ConnectNamedPipe.argtypes = [handle_t, wintypes.LPVOID]
        kernel32.ReadFile.argtypes = [handle_t, wintypes.LPVOID, dword, ctypes.POINTER(dword), wintypes.LPVOID]
        kernel32.DisconnectNamedPipe.argtypes = kernel32.CloseHandle.argtypes = [handle_t]
        invalid = wintypes.HANDLE(-1).value
        while True:
            handle = kernel32.CreateNamedPipeW(self.path, 0x1, 0, 1, 0, 1 << 16, 0, None)  # inbound, byte mode
            if handle == invalid:
                raise OSError(ctypes.get_last_error(), "CreateNamedPipeW failed")
            try:
                if not kernel32.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != 535:
                    time.sleep(1)                              # 535: client connected first
                    continue
                reader, buffer, read = FrameReader(), ctypes.create_string_buffer(1 << 16), wintypes.DWORD()
                while kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None) and read.value:
                    for item in reader.feed(buffer.raw[:read.value]):
                        if isinstance(item, FrameError):
                            log.warning("dropped frame: %s", item.code)
                        else:
                            self.link.on_frame(item)
            finally:
                kernel32.DisconnectNamedPipe(handle)
                kernel32.CloseHandle(handle)


def start_local_link(profile_dir: Path | None = None) -> CommandLink:
    """Host the pipe, create the folder the mod uses, and start the heartbeat."""
    if sys.platform != "win32":
        raise OSError("The FS25 bridge runs on the Windows PC next to the game")
    base = (profile_dir or game_profile_dir())
    if not base.is_dir():
        raise OSError(f"FS25 profile folder not found: {base}")
    folder = base.joinpath(*MOD_DIR)
    folder.mkdir(parents=True, exist_ok=True)     # never the game's own folders above it
    link = CommandLink(ModSettingsDir(folder))
    PipeServer(link).start()

    def beat():
        while not link.closed.wait(HEARTBEAT_S):
            link.heartbeat()

    link.heartbeat()

    threading.Thread(target=beat, daemon=True, name="fs25-heartbeat").start()
    return link
