"""Companion side of the AC-001 FS25 command bridge, and the real World adapter.

Local only, following Farmhand Companion's proven pattern (PROTOCOL.md):
- game -> companion: JSON lines on a named pipe this process hosts (PipeServer);
- companion -> game: one command at a time in modSettings/FS25_Farmhand/command.xml,
  renamed into place atomically; the game deletes it on receipt;
- session.xml names this companion and beats once a second; the game accepts only
  this session's commands with strictly increasing ids, stops what this session
  started if the beat stops, and reports it in every snapshot. It also carries the
  latest notice for the player (shown in game once per notice id);
- the player's in-game key arrives as a `request` frame of this session.

A command's ACCEPTED and DONE results are never treated as success: FS25World
returns only the game's observed end of an operation, and the executor verifies
every step from a fresh snapshot (companion/executor.py).
"""
from __future__ import annotations

from collections import deque
from dataclasses import replace
import logging
import math
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from adapters.fs25 import Observation, observation_from_snapshot
from adapters.world import StepFailed, StepUnverified
from companion.hitch import STAGING_DISTANCE_M
from protocol.ipc import MAX_NOTICES, PIPE_PATH, FrameError, FrameReader, encode_command, encode_session
from protocol.model import Destination, Equipment, FarmInfo, Field, HitchPlan, Pose, Position, StoreItem

log = logging.getLogger("farmhand")

MOD_DIR = ("modSettings", "FS25_Farmhand")   # = mod/FarmhandBridge.lua Bridge.MOD_DIR
COMMAND_FILE, SESSION_FILE = "command.xml", "session.xml"
SNAPSHOT_TIMEOUT_S = 3.0      # game sends one every 0.5 s while linked
ACCEPT_TIMEOUT_S = 5.0        # the game reads the command file every frame
HEARTBEAT_S = 1.0             # game drops the session after 10 s without a beat
# Bounds on waiting for an operation's terminal result: the game's own bound plus
# a margin (mod/FarmhandBridge.lua, mod/FarmhandHitch.lua).
ALIGN_MAX_DISTANCE_M = 10.0      # = mod/FarmhandHitch.lua Hitch.LIMITS.maxDistanceM
DONE_TIMEOUT_S = {"drive": 1230.0, "navigate": 1230.0, "stage": 135.0, "maneuver": 190.0, "align": 70.0, "attach": 25.0, "detach": 25.0, "pull_clear": 40.0, "fieldwork": 7300.0, "buy": 60.0, "save": 75.0, "block": 15.0, "open_gates": 20.0,
                  "stop": 25.0}
TERMINAL = ("DONE", "FAILED", "UNVERIFIED")


class CommandLink:
    """Session, command ids, and the frames the game sends back. Thread-safe."""

    def __init__(self, files, clock=time.monotonic, session: str | None = None):
        self.files = files
        self.clock = clock
        # Identifies this companion run (a replay guard, not a secret).
        self.session = session or uuid.uuid4().hex[:16]
        self._cond = threading.Condition()
        self._snapshot: dict | None = None
        self.hello: dict | None = None    # the game's latest hello (its mod version and build)
        self._snapshots = 0
        self._last_frame_at: float | None = None
        self._results: dict[int, list[tuple[str, str]]] = {}
        self._next_id = 1
        self._beat = 0
        self._last_session: str | None = None
        self._requests: deque[dict] = deque()
        self._last_request = 0
        self._notices: deque[tuple[int, str]] = deque(maxlen=MAX_NOTICES)
        self._notice_id = 0
        # The heartbeat and pipe threads both write the session file.
        self._write_lock = threading.Lock()
        # One command file at a time, held from writing it until the game answers
        # it (normally one frame): a stop from another thread (the requester's
        # cancel) waits only for that, never for the running operation's end.
        self._send_lock = threading.Lock()
        self.closed = threading.Event()

    def _write_session(self) -> None:
        """Never raises: a file the game or antivirus holds too long costs one beat
        or one notice, never the companion or its heartbeat thread."""
        with self._write_lock:
            if self.closed.is_set():
                return
            try:
                with self._cond:
                    notices = list(self._notices)
                self.files.write(SESSION_FILE, encode_session(self.session, self._beat, notices))
            except OSError as error:
                log.warning("session file not written: %s", error)

    def heartbeat(self) -> None:
        self._write_session()
        self._beat += 1

    def notice(self, code: str) -> None:
        """Tell the player (an in-game notification) via the session file."""
        with self._cond:
            self._notice_id += 1
            self._notices.append((self._notice_id, code))
        log.info("notice %d %s", self._notice_id, code)
        self._write_session()

    def renew(self) -> None:
        """Start a new session id (the game never re-adopts one it dropped)."""
        with self._cond:
            self.session = uuid.uuid4().hex[:16]
            self._results.clear()
            self._requests.clear()
            self._next_id, self._last_request = 1, 0
        log.info("new session %s", self.session)
        self._write_session()

    def inject(self, request: dict) -> None:
        """Queue a request produced here (e.g. transcribed speech) behind the game's."""
        with self._cond:
            self._requests.append(request)
            self._cond.notify_all()

    def next_request(self, timeout: float) -> dict | None:
        """The player's next request frame of this session, or None."""
        def pop():
            return self._requests.popleft() if self._requests else None
        return self._wait(pop, timeout)

    def linked_session(self) -> tuple[str | None, float | None]:
        """The session the latest snapshot names, and when any frame last arrived."""
        with self._cond:
            return self._last_session, self._last_frame_at

    def close(self) -> None:
        """End the session: no more beats, and the game drops it (never re-adopted)."""
        with self._write_lock:
            self.closed.set()
            self.files.remove(SESSION_FILE)

    def on_frame(self, frame: dict) -> None:
        with self._cond:
            self._last_frame_at = self.clock()
            event = frame["event"]
            if event == "hello":
                # The game (re)opened the pipe, e.g. on re-adopting this session
                # after a savegame reload: its request ids restart there.
                self._last_request = 0
                self.hello = frame
            elif event == "snapshot":
                self._snapshot, self._snapshots = frame, self._snapshots + 1
                self._last_session = frame["session"]
            elif event == "catalog":
                self.catalog = tuple(StoreItem(i["id"], i["name"], i["brand"], tuple(i["categories"]), i["price"],
                                               i["mod"], i["width"], i["needed_power"], i["power"],
                                               tuple(i.get("hitches") or ()))
                                     for i in frame["items"])
            elif event == "result" and frame["session"] == self.session:
                self._results.setdefault(frame["id"], []).append((frame["status"], frame["code"]))
            elif event == "request" and frame["session"] == self.session and frame["id"] > self._last_request:
                # Strictly increasing per session: a replayed or older press never runs.
                self._last_request = frame["id"]
                self._requests.append(frame)
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

    def command(self, op: str, args: dict, cancel=None) -> tuple[str, str]:
        """Send one command; return its terminal (status, code) from the game.

        ``cancel`` (a threading.Event): once set, nothing but a stop is sent. It is
        checked under the send lock, and the requester's stop sets it before asking
        for that lock, so a step is either sent before the stop (which pre-empts it)
        or refused (StepFailed CANCELLED); it can never start after the stop."""
        with self._send_lock:
            if cancel is not None and cancel.is_set() and op != "stop":
                raise StepFailed("CANCELLED")
            # Ids are taken in send order: the game ignores any id lower than one it saw.
            with self._cond:
                command_id, self._next_id = self._next_id, self._next_id + 1
            text = encode_command(self.session, command_id, op, args)
            log.info("command id=%d op=%s %s", command_id, op, args)
            self.files.write(COMMAND_FILE, text)
            first = self._wait(lambda: (self._results.get(command_id) or [None])[0], ACCEPT_TIMEOUT_S)
            # Removing the file proves the game never read it; otherwise unknown.
            unread = first is None and self.files.remove(COMMAND_FILE)
        if first is None:
            if unread:
                raise StepFailed("NOT_ACCEPTED")
            raise StepUnverified("NO_ACCEPTANCE")
        if first[0] == "REJECTED":
            raise StepFailed("REJECTED_" + first[1])

        def terminal():
            done = [r for r in self._results.get(command_id, ()) if r[0] in TERMINAL]
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

    def __init__(self, link: CommandLink, categories: dict[str, str] | None = None):
        self.link = link
        self.categories = dict(categories or {})
        # The running job's cancel event (companion/coordinator.py): no step is
        # sent once it is set (CommandLink.command).
        self.cancel = None
        self._lock = threading.RLock()
        self._reservations: dict[str, tuple[str, tuple[str, str]]] = {}

    # ------------------------------------------------------------ observation
    def _observe(self) -> Observation:
        frame = self.link.fresh_snapshot()
        return observation_from_snapshot(frame, self.categories,
                                         bridge_ready=frame["session"] == self.link.session)

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

    def places(self) -> tuple[Destination, ...]:
        return self._observe().places

    def fields(self) -> tuple[Field, ...]:
        return self._observe().fields

    def farm(self) -> FarmInfo | None:
        return self._observe().farm

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
            # Unattached, or hitched to each other (move_pair); never to anything else.
            partners = {pair[0].id: pair[1].id, pair[1].id: pair[0].id}
            if any(e.reserved_by is not None or e.busy or e.occupied or not e.available
                   or e.attached_to not in (None, partners[e.id]) or not e.stopped for e in pair):
                return False
            if (pair[0].attached_to is None) != (pair[1].attached_to is None):
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
            if any(items[i].busy for i in reservation[1]):
                # An AI job or AutoDrive still controls it: not idle, not released.
                raise ValueError("Cannot release equipment still under AI control")
            del self._reservations[owner]
            log.info("released %s", reservation[1])

    def reservation(self, owner: str) -> tuple[str, str] | None:
        """The (tractor, wagon) ids ``owner`` holds, if any."""
        with self._lock:
            reservation = self._reservations.get(owner)
        return None if reservation is None else reservation[1]

    def retained(self) -> tuple[str, ...]:
        """Owners whose reservation outlived their request (stop never confirmed)."""
        with self._lock:
            return tuple(self._reservations)

    def recover(self, owner: str) -> bool:
        """Release a reservation kept after an unconfirmed stop, but only once a stop
        is confirmed now: the game's `stop` for the tractor ends DONE (its own motion
        over, at rest) and a fresh snapshot shows both units present and stopped.
        Otherwise it stays reserved (AGENTS.md); a missing unit is never assumed gone."""
        with self._lock:
            reservation = self._reservations.get(owner)
        if reservation is None:
            return True
        try:
            status, code = self.link.command("stop", {"tractor": int(reservation[1][0])})
            if status != "DONE":
                log.info("retained %s: stop %s %s", reservation[1], status, code)
                return False
            self.release(owner)                  # re-checks both present and stopped
        except (StepFailed, StepUnverified, ValueError) as error:
            log.info("retained %s: %s", reservation[1], getattr(error, "code", error))
            return False
        log.info("recovered %s for %s after a confirmed stop", reservation[1], owner)
        return True

    # ------------------------------------------------------------- operations
    def _run(self, op: str, **args) -> str:
        status, code = self.link.command(op, args, self.cancel)
        if status == "FAILED":
            raise StepFailed(code)
        if status == "UNVERIFIED":
            raise StepUnverified(code)
        return code

    def catalog(self) -> tuple[StoreItem, ...]:
        """The store's vehicle items (the game sends them once built)."""
        return getattr(self.link, "catalog", ()) or ()

    def open_gates(self, x: float, z: float, radius: float) -> str:
        """Open the closed gates within ``radius`` m of (x, z), as GIANTS opens a gate."""
        return self._run("open_gates", x=float(x), z=float(z), area=float(radius))

    def block_area(self, x: float, z: float, size: float) -> str:
        """Mark size x size m around (x, z) impassable for GIANTS' navigation."""
        return self._run("block", x=float(x), z=float(z), area=float(size))

    def save_game(self) -> str:
        """SAVE: the game's own save (mod/FarmhandBridge.lua starters.save); DONE only
        once the game's save stamp changed."""
        return self._run("save")

    def buy(self, item_id: int, hitch: int = 0) -> str:
        """BUY: GIANTS' own BuyVehicleData for the store item (mod/FarmhandBridge.lua
        starters.buy), with attacherJoint configuration ``hitch`` (1-based; 0 = the
        default). DONE BOUGHT names no vehicle: the executor finds it."""
        return self._run("buy", item=int(item_id), hitch=int(hitch))

    def fieldwork(self, tractor_id: str, field_id: int, pose: Pose, owner: str, area_m2: float = 0.0) -> str:
        self._reserved(tractor_id, owner)
        return self._run("fieldwork", tractor=int(tractor_id), field=int(field_id), x=pose.x, z=pose.z,
                         yaw=pose.yaw, area=float(area_m2))

    def travel(self, tractor_id: str, destination_id: str, owner: str) -> None:
        self._reserved(tractor_id, owner)
        self._run("drive", tractor=int(tractor_id), marker=int(destination_id))

    def navigate(self, tractor_id: str, pose: Pose, owner: str) -> None:
        self._reserved(tractor_id, owner)
        self._run("navigate", tractor=int(tractor_id), x=pose.x, z=pose.z, yaw=pose.yaw)

    def stage(self, tractor_id: str, wagon_id: str, pose: Pose, owner: str) -> None:
        self._reserved(tractor_id, owner, wagon_id)
        self._run("stage", tractor=int(tractor_id), x=pose.x, z=pose.z, yaw=pose.yaw)

    def maneuver(self, tractor_id: str, wagon_id: str, plan: HitchPlan, owner: str) -> None:
        self._reserved(tractor_id, owner, wagon_id)
        self._run("maneuver", tractor=int(tractor_id), wagon=int(wagon_id), tractorJoint=plan.tractor_joint,
                  wagonJoint=plan.wagon_joint)

    def align(self, tractor_id: str, wagon_id: str, plan: HitchPlan, owner: str, check_only: bool = False) -> None:
        self._reserved(tractor_id, owner, wagon_id)
        # 0 m: look only (mod/FarmhandHitch.lua Hitch.limits): GIANTS' candidate, no motion.
        reach = 0.0 if check_only else self._reverse_length(tractor_id, plan)
        self._run("align", tractor=int(tractor_id), wagon=int(wagon_id), tractorJoint=plan.tractor_joint,
                  wagonJoint=plan.wagon_joint, maxDistanceM=reach)

    def _reverse_length(self, tractor_id: str, plan: HitchPlan) -> float:
        """The bounded reverse's length: the gap STAGE actually left plus 2 m, never
        less than the planned staging distance + 2 nor more than the mod's cap."""
        tractor = next((e for e in self._observe().equipment if e.id == tractor_id), None)
        joint = tractor and next((j for j in tractor.output_joints if j.index == plan.tractor_joint), None)
        gap = STAGING_DISTANCE_M
        if tractor is not None and joint is not None and tractor.ai_root is not None:
            gap = max(gap, math.dist(tractor.ai_root.to_world(joint.x, joint.z), plan.target))
        return min(ALIGN_MAX_DISTANCE_M, gap + 2)

    def attach(self, tractor_id: str, wagon_id: str, joints: tuple[int, int], owner: str) -> None:
        self._reserved(tractor_id, owner, wagon_id)
        self._run("attach", tractor=int(tractor_id), wagon=int(wagon_id), tractorJoint=joints[0],
                  wagonJoint=joints[1])

    def detach(self, tractor_id: str, wagon_id: str, owner: str) -> None:
        self._reserved(tractor_id, owner, wagon_id)
        self._run("detach", tractor=int(tractor_id), wagon=int(wagon_id))

    def pull_clear(self, tractor_id: str, wagon_id: str, owner: str) -> None:
        self._reserved(tractor_id, owner, wagon_id)
        self._run("pull_clear", tractor=int(tractor_id), wagon=int(wagon_id))

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
    The game is the write-only client and reconnects by itself (FH Transport.lua).

    Every instance is created with FILE_FLAG_FIRST_PIPE_INSTANCE, so a second
    companion cannot host the same pipe: start() raises OSError instead of letting
    two companions fight over the game's session."""

    INBOUND, FIRST_INSTANCE, ERROR_PIPE_CONNECTED = 0x1, 0x00080000, 535

    def __init__(self, link: CommandLink, path: str = PIPE_PATH):
        super().__init__(daemon=True, name="fs25-pipe")
        self.link, self.path = link, path
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        k = self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle_t, dword = wintypes.HANDLE, wintypes.DWORD
        k.CreateNamedPipeW.restype = handle_t
        k.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR] + [dword] * 6 + [wintypes.LPVOID]
        k.ConnectNamedPipe.argtypes = [handle_t, wintypes.LPVOID]
        k.ReadFile.argtypes = [handle_t, wintypes.LPVOID, dword, ctypes.POINTER(dword), wintypes.LPVOID]
        k.DisconnectNamedPipe.argtypes = k.CloseHandle.argtypes = [handle_t]
        self._invalid, self._dword = wintypes.HANDLE(-1).value, dword
        self._handle = None

    def _create(self):
        handle = self._kernel32.CreateNamedPipeW(self.path, self.INBOUND | self.FIRST_INSTANCE, 0, 1, 0,
                                                 1 << 16, 0, None)   # byte mode, one instance
        if handle == self._invalid:
            raise OSError(self._ctypes.get_last_error(),
                          f"cannot host {self.path}; is another Farmhand companion running?")
        return handle

    def start(self) -> None:
        self._handle = self._create()
        super().start()

    def run(self) -> None:
        k, ctypes = self._kernel32, self._ctypes
        handle = self._handle
        while True:
            try:
                if k.ConnectNamedPipe(handle, None) or ctypes.get_last_error() == self.ERROR_PIPE_CONNECTED:
                    reader, buffer, read = FrameReader(), ctypes.create_string_buffer(1 << 16), self._dword()
                    while k.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None) and read.value:
                        for item in reader.feed(buffer.raw[:read.value]):
                            if isinstance(item, FrameError):
                                log.warning("dropped frame: %s", item.code)
                            else:
                                self.link.on_frame(item)
                else:
                    time.sleep(1)
            finally:
                k.DisconnectNamedPipe(handle)
                k.CloseHandle(handle)
            try:
                handle = self._create()
            except OSError:
                log.exception("cannot re-create the pipe; the companion stops")
                self.link.close()
                return


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
