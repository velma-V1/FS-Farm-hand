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
from companion.drive_goal import DRIVEN
from companion.hitch import STAGING_DISTANCE_M, load_ad_graph
from protocol.ipc import (GHOST_OPS, MAX_NOTICES, PIPE_PATH, FrameError, FrameReader, encode_command,
                          encode_session)
from protocol.model import Contract, Destination, Equipment, FarmInfo, Field, HitchPlan, Pose, Position, StoreItem

log = logging.getLogger("farmhand")

MOD_DIR = ("modSettings", "FS25_Farmhand")   # = mod/FarmhandBridge.lua Bridge.MOD_DIR
COMMAND_FILE, SESSION_FILE = "command.xml", "session.xml"
SNAPSHOT_TIMEOUT_S = 3.0      # game sends one every 0.5 s while linked
ACCEPT_TIMEOUT_S = 5.0        # the game reads the command file every frame
STOP_ACCEPT_TIMEOUT_S = 30.0  # a stop is never withdrawn: a stalled game reads it when it resumes
HEARTBEAT_S = 1.0             # game drops the session after 10 s without a beat
# Bounds on waiting for an operation's terminal result: the game's own bound plus
# a margin (mod/FarmhandBridge.lua, mod/FarmhandHitch.lua).
ALIGN_MAX_DISTANCE_M = 10.0      # = mod/FarmhandHitch.lua Hitch.LIMITS.maxDistanceM
# farm_drive's yaw for "any heading" (= mod/FarmhandDrive.lua: beyond +-2 pi is none).
ANY_HEADING = 10.0
TELEPORT_HERE = 90000.0            # teleport x/z: near the rig itself (= mod/FarmhandBridge.lua, beyond any map)
# Farmhand drives restarted after ending against something on the way.
CONTACT_RESTARTS = 2
# A Farmhand drive that ends with the rig unable to move: teleport recovery (the user,
# 2026-10-30; AGENTS.md 19, "allow teleport recovery", a setting), once per job, then the
# drive again from the open spot.
STUCK_CODES = frozenset({"EGRESS_LIMIT", "EGRESS_DIVERGED", "EGRESS_BLOCKED", "EGRESS_NOT_CLEAR",
                         "EGRESS_NOT_AHEAD", "EGRESS_NOT_BEHIND", "START_NOT_CLEAR"})
CONTACT_SETTLE_S = 10.0     # waited for it to stand before a restart
DONE_TIMEOUT_S = {"pay": 15.0, "charge": 15.0, "teleport": 20.0, "worktool": 25.0, "reposition": 14700.0, "farm_drive": 14700.0, "drive": 14700.0, "navigate": 14700.0, "stage": 135.0, "maneuver": 190.0, "align": 70.0, "attach": 25.0, "detach": 25.0, "pull_clear": 40.0, "unpark": 75.0, "fold": 30.0, "belts": 15.0, "load": 105.0, "ad_marker": 15.0, "ad_connect": 15.0, "follow": 7300.0, "contract": 75.0, "land": 25.0, "ad_unload": 20.0, "ad_sell": 14700.0, "pipe": 30.0, "overload": 310.0, "alongside": 105.0, "deliver": 14700.0, "backoff": 45.0, "fieldwork": 7300.0, "buy": 60.0, "save": 75.0, "block": 15.0, "open_gates": 20.0,
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
        self._catalog_pages: dict[int, list] = {}
        self.catalog: tuple = ()            # the store items of the linked game (complete pages only)
        self._snapshots = 0
        self._last_frame_at: float | None = None
        self._results: dict[int, list[tuple[str, str]]] = {}
        self._next_id = 1
        self._beat = 0
        self._last_session: str | None = None
        self._requests: deque[dict] = deque()
        self._last_request = 0
        self._notices: deque[tuple[int, str]] = deque(maxlen=MAX_NOTICES)
        # Field outlines are static map data on every Observe.OUTLINE_EVERY-th snapshot
        # (= mod/FarmhandObserve.lua): (map id, "field-N") -> the last outline received.
        self.field_outlines: dict[tuple, tuple] = {}
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
                # Its store items come again: never buy from another game's catalog.
                self.catalog, self._catalog_pages = (), {}
            elif event == "snapshot":
                self._snapshot, self._snapshots = frame, self._snapshots + 1
                self._last_session = frame["session"]
                for place in frame.get("field_places", ()):
                    if place.get("outline"):
                        self.field_outlines[(frame.get("map_id"), f"field-{place['id']}")] = tuple(
                            (p[0], p[1]) for p in place["outline"])
            elif event == "catalog":
                # In pages (page 0 starts a catalog); complete only once every page is here.
                page, pages = frame.get("page", 0), frame.get("pages", 1)
                if page == 0:
                    self._catalog_pages = {}
                self._catalog_pages[page] = frame["items"]
                if set(self._catalog_pages) == set(range(pages)):
                    items = [i for n in range(pages) for i in self._catalog_pages[n]]
                    self.catalog = tuple(StoreItem(i["id"], i["name"], i["brand"], tuple(i["categories"]), i["price"],
                                                   i["mod"], i["width"], i["needed_power"], i["power"],
                                                   tuple(i.get("hitches") or ()), tuple(i.get("engines") or ()),
                                                   tuple((o["kind"], o["type"], o["index"], o["name"])
                                                         for o in i.get("options") or ()),
                                                   i.get("capacity"), tuple(i.get("fill_types") or ()))
                                         for i in items)
                    self._catalog_pages = {}
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
            first = self._wait(lambda: (self._results.get(command_id) or [None])[0],
                               STOP_ACCEPT_TIMEOUT_S if op == "stop" else ACCEPT_TIMEOUT_S)
            # Removing the file proves the game never read it; otherwise unknown. A stop
            # stays (live bb8dc55381b14a54-1: a removed, unread stop let a stalled game's
            # drive resume with no job behind it).
            unread = first is None and op != "stop" and self.files.remove(COMMAND_FILE)
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
        self._reservations: dict[str, tuple[str, tuple[str, ...]]] = {}
        # Teleport recovery allowed (Settings.teleport_recovery; companion/service.py sets it),
        # and the jobs that used theirs.
        self.teleport_recovery = True
        self._teleported: set[str] = set()
        # Passing through scenery beside the rig in tight spots (Settings.scenery_ghosting, the
        # player's opt-in, default off; companion/service.py sets it): only then does a motion
        # command carry ghost=1 (protocol.ipc GHOST_OPS). Off, every motion keeps all collisions.
        self.scenery_ghosting = False
        # The most one purchase may cost (Settings.spending_limit, the player's; None: no limit
        # beyond the farm's money), checked before every store and land purchase (executor).
        self.spending_limit = None
        # Farmhand's pay (mode, amount) reader (Settings.pay; companion/service.py), sent once per
        # game session; None: the game's default in the mod.
        self.pay = None
        self._paid_session = None

    # ------------------------------------------------------------ observation
    def _observe(self) -> Observation:
        frame = self.link.fresh_snapshot()
        observation = observation_from_snapshot(frame, self.categories,
                                                bridge_ready=frame["session"] == self.link.session)
        # The link keeps every field outline received (CommandLink.field_outlines).
        outlines = getattr(self.link, "field_outlines", {})
        places = tuple(place if place.outline else replace(
            place, outline=outlines.get((observation.map_id, place.id), ())) for place in observation.field_places)
        return replace(observation, field_places=places)

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

    def field_places(self) -> tuple[Destination, ...]:
        """Every map field as a place "field N" at GIANTS' own field position."""
        return self._observe().field_places

    def farm(self) -> FarmInfo | None:
        return self._observe().farm

    def map_id(self) -> str | None:
        return self._observe().map_id

    # ------------------------------------------------------------ reservation
    def reserve(self, ids: tuple[str, str], owner: str, transport: bool = False) -> bool:
        """Both units or neither. ``transport``: a free tractor and a transport trailer, which
        alone may be hitched outside the pair (to the truck towing it)."""
        with self._lock:
            if not owner or len(set(ids)) != 2 or owner in self._reservations:
                return False
            items = {e.id: e for e in self.snapshot()}
            if any(i not in items for i in ids):
                return False
            pair = [items[i] for i in ids]
            # Unattached, or hitched to each other (move_pair); never to anything else.
            partners = {pair[0].id: pair[1].id, pair[1].id: pair[0].id}
            if any(e.reserved_by is not None or e.busy or e.occupied or not e.available or not e.stopped
                   for e in pair):
                return False
            if transport:
                # The tractor alone or carrying its own implement; never towing the transport.
                if pair[0].attached_to == pair[1].id or pair[1].attached_to == pair[0].id:
                    return False
            elif (any(e.attached_to not in (None, partners[e.id]) for e in pair)
                  or (pair[0].attached_to is None) != (pair[1].attached_to is None)):
                return False
            if pair[0].farm_id != pair[1].farm_id:
                return False
            self._reservations[owner] = (pair[0].farm_id, tuple(ids))
            log.info("reserved tractor=%s wagon=%s for %s", ids[0], ids[1], owner)
            return True

    def reserve_rig(self, ids: tuple[str, ...], owner: str, loose: bool = False) -> bool:
        """Atomically reserve an observed root vehicle and every attached/belted unit; ``loose``: a
        lone implement hitched to nothing (for the teleport recovery)."""
        with self._lock:
            if not owner or not 1 <= len(ids) <= 6 or len(set(ids)) != len(ids) or owner in self._reservations:
                return False
            observed = {i.id: i for i in self.snapshot()}
            if any(i not in observed for i in ids):
                return False
            rig = [observed[i] for i in ids]
            farm = rig[0].farm_id
            if rig[0].belted_to is not None:
                return False
            if rig[0].category not in DRIVEN and not (loose and len(ids) == 1 and rig[0].attached_to is None):
                return False
            if any(i.farm_id != farm or i.reserved_by is not None or not i.available
                   or i.busy or i.occupied or not i.stopped for i in rig):
                return False
            members = set(ids)
            if any(i.attached_to not in (None, *members) or i.belted_to not in (None, *members) for i in rig):
                return False
            if any(i.id not in members and (i.attached_to in members or i.belted_to in members)
                   for i in observed.values()):
                return False
            if any(i.id != rig[0].id and i.attached_to is None and i.belted_to is None for i in rig):
                return False
            self._reservations[owner] = (farm, tuple(ids))
            log.info("reserved rig %s for %s", ids, owner)
            return True

    def farm_drive(self, vehicle_id: str, target: tuple[float, float], owner: str, area: float = 0.0,
                   yaw: float | None = None, via=None) -> None:
        """Drive the reserved existing rig by Farmhand's game-side route follower: to
        the point, or (``area`` > 0) to any clear approach point that near it; with
        ``yaw``, arriving at that heading (GIANTS' yaw: +z is 0)."""
        with self._lock:
            reservation = self._reservations.get(owner)
        if reservation is None or reservation[1][0] != vehicle_id:
            raise StepFailed("NOT_RESERVED")
        try:
            self._farm_drive(vehicle_id, target, owner, area, yaw, via)
        except StepFailed as failure:
            if failure.code not in STUCK_CODES or not self.teleport_recovery or owner in self._teleported:
                raise
            self._teleported.add(owner)
            log.info("farm_drive could not move the rig (%s): teleport recovery to open ground nearby", failure.code)
            self.await_stopped(vehicle_id)        # live 0d44db5189854c18-1: refused NOT_STOPPED while rolling
            self.teleport(vehicle_id, owner)
            self._farm_drive(vehicle_id, target, owner, area, yaw, via)

    def set_pay(self, mode: int, amount: float) -> None:
        """Farmhand's pay in the game (mod/FarmhandBridge.lua Bridge.pay): mode and amount."""
        self._run("pay", direction=int(mode), area=float(amount))

    def charge(self, amount: float) -> None:
        """Per-job pay for a completed request (charged only in that mode, by the game)."""
        self._run("charge", area=float(amount))

    def reposition(self, vehicle_id: str, owner: str) -> None:
        """The reserved vehicle (a standing combine) to the nearest open ground with lanes beside and
        behind it, by Farmhand's own drive."""
        with self._lock:
            reservation = self._reservations.get(owner)
        if reservation is None or vehicle_id not in reservation[1]:
            raise StepFailed("NOT_RESERVED")
        self._run("reposition", tractor=int(vehicle_id))

    def teleport(self, vehicle_id: str, owner: str, min_m: float = 0.0, at: Pose | None = None) -> None:
        """Teleport recovery: the reserved rig and everything attached to the nearest open spot,
        no nearer than ``min_m``; near ``at`` at its heading when given (a loaded rig onto
        AutoDrive's road), else near where the rig stands."""
        with self._lock:
            reservation = self._reservations.get(owner)
        if reservation is None or reservation[1][0] != vehicle_id:
            raise StepFailed("NOT_RESERVED")
        x, z, yaw = (at.x, at.z, math.remainder(float(at.yaw), math.tau)) if at is not None else \
            (TELEPORT_HERE, TELEPORT_HERE, ANY_HEADING)
        self._run("teleport", tractor=int(vehicle_id), area=float(min_m), x=float(x), z=float(z), yaw=float(yaw))
        self.link.notice("TELEPORTED")          # never silent (AGENTS.md 19)

    def _farm_drive(self, vehicle_id, target, owner, area, yaw, via=None):
        # Ended against something on the way (live f4fb84d66f9e4191-1: a traffic cone on the road),
        # the drive starts again from there: its start departs from what it touches and plans
        # around it. At most CONTACT_RESTARTS times; any other end is the drive's own.
        for restart in range(CONTACT_RESTARTS + 1):
            try:
                args = dict(tractor=int(vehicle_id), x=float(target[0]), z=float(target[1]),
                            area=float(area), yaw=ANY_HEADING if yaw is None else math.remainder(float(yaw), math.tau))
                if via:
                    args["via"] = ";".join(f"{float(x):.3f},{float(z):.3f}" for x, z in via)
                self._run("farm_drive", **args)
                return
            except StepFailed as failure:
                if failure.code != "ENVELOPE_CONTACT" or restart == CONTACT_RESTARTS:
                    raise
                log.info("farm_drive ended against something on the way; restarting from there (%d)", restart + 1)
                # Once it stands (live 13f374b86b594744-2: restarted while still rolling, NOT_STOPPED).
                self.await_stopped(vehicle_id)

    def await_stopped(self, vehicle_id: str) -> None:
        """Until the vehicle is observed standing, at most CONTACT_SETTLE_S."""
        deadline = time.monotonic() + CONTACT_SETTLE_S
        while time.monotonic() < deadline:
            try:
                vehicle = next((i for i in self.snapshot() if i.id == vehicle_id), None)
            except StepUnverified:
                vehicle = None
            if vehicle is not None and vehicle.stopped:
                return
            time.sleep(0.25)

    def _reserved(self, tractor_id: str, owner: str, wagon_id: str | None = None) -> tuple[str, str]:
        with self._lock:
            reservation = self._reservations.get(owner)
        if (reservation is None or reservation[1][0] != tractor_id
                or (wagon_id is not None and wagon_id not in reservation[1][1:])):
            raise StepFailed("NOT_RESERVED")
        return reservation[1]

    RELEASE_SETTLE_S = 10.0       # a stopped rig may roll this long (live 4902eb24a6264922-1: released mid-roll)

    def release(self, owner: str) -> None:
        deadline = time.monotonic() + self.RELEASE_SETTLE_S
        while True:
            with self._lock:
                reservation = self._reservations.get(owner)
                if reservation is None:
                    return
                items = {e.id: e for e in self._observe().equipment}
                missing = any(i not in items for i in reservation[1])
                moving = not missing and any(not items[i].stopped for i in reservation[1])
                if not missing and not moving:
                    if any(items[i].busy for i in reservation[1]):
                        # An AI job or AutoDrive still controls it: not idle, not released.
                        raise ValueError("Cannot release equipment still under AI control")
                    del self._reservations[owner]
                    log.info("released %s", reservation[1])
                    return
            if missing or time.monotonic() >= deadline:
                raise ValueError("Cannot release moving or missing equipment")
            time.sleep(0.5)

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
        # Farmhand's pay goes to each game session once, before its first real command (never
        # before a job that blocks with nothing sent).
        if op not in ("pay", "charge") and self.pay is not None and self._paid_session != self.link.session:
            self._paid_session = self.link.session
            try:
                self.set_pay(*self.pay())
            except (StepFailed, StepUnverified) as failure:
                log.info("pay not set: %s", failure.code)
        if op in GHOST_OPS and self.scenery_ghosting is True:
            args = {**args, "ghost": 1}
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

    def contracts(self) -> tuple[Contract, ...]:
        """GIANTS' field contracts: those available and this farm's own."""
        return self._observe().contracts

    def contract(self, field: int, start: bool) -> str:
        """CONTRACT: the field's available contract taken (DONE once GIANTS runs it), or this
        farm's finished one collected (GIANTS pays its reward); mod/FarmhandBridge.lua
        starters.contract."""
        return self._run("contract", field=int(field), direction=1 if start else -1)

    def land(self) -> tuple:
        return self._observe().land

    def land_change(self, field: int, buy: bool) -> str:
        """LAND: the field's farmland parcel bought or sold; mod/FarmhandBridge.lua starters.land."""
        return self._run("land", field=int(field), direction=1 if buy else -1)

    UNLOAD_TO = {"sell": 1, "store": -1, "keep": 0}

    def ad_unload(self, tractor_id: str, marker_id: str, field: int, owner: str, destination: str = "sell") -> str:
        """UNLOADER: AutoDrive's own combine unloader for the reserved rig (mod/FarmhandBridge.lua
        starters.ad_unload); UNLOADING once AutoDrive runs it, which goes on until stopped. A full
        trailer goes where the harvest goes: sell, store (the farm's silo) or keep (parked at the
        field's marker)."""
        self._reserved(tractor_id, owner)
        return self._run("ad_unload", tractor=int(tractor_id), marker=int(marker_id), field=int(field),
                         direction=self.UNLOAD_TO[destination])

    def ad_sell(self, tractor_id: str, owner: str) -> str:
        """SELL: AutoDrive delivers the reserved rig's cargo to the best paid sell point for it
        (mod/FarmhandBridge.lua starters.ad_sell); never evidence of the sale."""
        self._reserved(tractor_id, owner)
        return self._run("ad_sell", tractor=int(tractor_id))

    def pipe(self, vehicle_id: str, out: bool, owner: str) -> str:
        """The reserved combine's pipe out or in (mod/FarmhandBridge.lua starters.pipe)."""
        self._unit(vehicle_id, owner)
        return self._run("pipe", tractor=int(vehicle_id), direction=1 if out else -1)

    def alongside(self, tractor_id: str, combine_id: str, owner: str, combine_owner: str, past: bool = False) -> str:
        """ALONGSIDE (AGENTS.md exception 6): the reserved tractor's trailer onto the pipe's line
        and under the reserved combine's pipe, or (``past``) on along it until clear of the
        combine (mod/FarmhandBridge.lua starters.alongside)."""
        self._reserved(tractor_id, owner)
        self._unit(combine_id, combine_owner)
        return self._run("alongside", tractor=int(tractor_id), wagon=int(combine_id), direction=-1 if past else 1)

    def backoff(self, tractor_id: str, trailer_id: str, owner: str) -> str:
        """BACKOFF (AGENTS.md exception 5): the reserved tractor straight back from the reserved
        transport trailer's ramps (mod/FarmhandBridge.lua starters.backoff)."""
        self._reserved(tractor_id, owner, trailer_id)
        return self._run("backoff", tractor=int(tractor_id), wagon=int(trailer_id))

    def giants_deliver(self, tractor_id: str, owner: str, store: bool = False) -> str:
        """SELL: GIANTS' own Deliver helper takes the reserved rig's cargo to the best paid
        selling station its AI can unload at, or (``store``) to the farm's own storage
        nearest it (mod/FarmhandBridge.lua starters.deliver); never evidence of either."""
        self._reserved(tractor_id, owner)
        return self._run("deliver", tractor=int(tractor_id), direction=-1 if store else 1)

    def overload(self, combine_id: str, trailer_id: str, owner: str) -> str:
        """The reserved standing combine discharges into the trailer under its pipe
        (mod/FarmhandBridge.lua starters.overload); never evidence of what moved."""
        self._unit(combine_id, owner)
        return self._run("overload", tractor=int(combine_id), wagon=int(trailer_id))

    def save_game(self) -> str:
        """SAVE: the game's own save (mod/FarmhandBridge.lua starters.save); DONE only
        once the game's save stamp changed."""
        return self._run("save")

    def buy(self, item_id: int, hitch: int = 0, engine: int = 0, options: tuple[int, ...] = ()) -> str:
        """BUY: GIANTS' own BuyVehicleData for the store item (mod/FarmhandBridge.lua
        starters.buy), with attacherJoint configuration ``hitch`` and motor configuration
        ``engine`` (1-based; 0 = the default). DONE BOUGHT names no vehicle: the executor
        finds it."""
        chosen = list(options)[:4] + [0] * (4 - min(len(options), 4))
        return self._run("buy", item=int(item_id), hitch=int(hitch), engine=int(engine), opt1=int(chosen[0]),
                         opt2=int(chosen[1]), opt3=int(chosen[2]), opt4=int(chosen[3]))

    def fieldwork(self, tractor_id: str, field_id: int, pose: Pose, owner: str, area_m2: float = 0.0,
                  seed: int = 0, hand: bool = False) -> str:
        self._reserved(tractor_id, owner)
        return self._run("fieldwork", tractor=int(tractor_id), field=int(field_id), x=pose.x, z=pose.z,
                         yaw=pose.yaw, area=float(area_m2), seed=int(seed), hand=1 if hand else 0)

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

    def worktool(self, tractor_id: str, lower: bool, owner: str) -> str:
        """The reserved tractor's plow lowered with field creation on, or raised with its field
        limit back on (mod/FarmhandBridge.lua starters.worktool)."""
        self._reserved(tractor_id, owner)
        return self._run("worktool", tractor=int(tractor_id), direction=1 if lower else -1)

    def fold(self, vehicle_id: str, direction: int, owner: str) -> str:
        """One of the reserved pair's own folding parts to an end (GIANTS Foldable)."""
        self._unit(vehicle_id, owner)
        return self._run("fold", tractor=int(vehicle_id), direction=int(direction))

    def belts(self, vehicle_id: str, fasten: bool, owner: str) -> str:
        """The reserved transport's tension belts fastened or released (GIANTS TensionBelts)."""
        self._unit(vehicle_id, owner)
        return self._run("belts", tractor=int(vehicle_id), direction=1 if fasten else -1)

    def load(self, tractor_id: str, trailer_id: str, direction: int, owner: str) -> str:
        """LOAD (1) / UNLOAD (-1): the reserved tractor alone straight onto the reserved
        trailer's deck or back off it (mod/FarmhandBridge.lua starters.load); DONE LOADED /
        UNLOADED only braked at rest where planned. The companion verifies from a snapshot."""
        self._reserved(tractor_id, owner, trailer_id)
        return self._run("load", tractor=int(tractor_id), wagon=int(trailer_id), direction=int(direction))

    def follow(self, tractor_id: str, owner: str) -> str:
        """FollowMe: the reserved vehicle follows the one the player drives, until that ends
        (mod/FarmhandBridge.lua starters.follow). Only when the player asked."""
        self._reserved(tractor_id, owner)
        return self._run("follow", tractor=int(tractor_id))

    def ad_marker(self, field: int | None = None, place: tuple[float, float] | None = None) -> str:
        """An AutoDrive destination made AutoDrive's own way (mod/FarmhandBridge.lua
        starters.ad_marker): "Field N" by field N's edge, or the farm building at ``place`` by
        its own name; only after AutoDrive's saved network was backed up."""
        if (field is None) == (place is None):
            raise StepFailed("INVALID_MARKER_PLACE")
        farm = self._observe().farm
        folder = getattr(self.link.files, "path", None)
        if folder is None:
            raise StepFailed("AD_CONFIG_BACKUP_FAILED")
        backup_autodrive_config(farm.savegame_dir if farm else None, Path(folder).parent.parent,
                                Path(folder) / "backup")
        if field is not None:
            return self._run("ad_marker", field=int(field), x=0.0, z=0.0)
        return self._run("ad_marker", field=0, x=float(place[0]), z=float(place[1]))

    def ad_connect(self, tractor_id: str, marker_id: str, owner: str) -> str:
        """AutoDrive's lane from the vehicle, a dead end within a few metres of a road that has a
        route to the marker, joined to it AutoDrive's own way (mod/FarmhandBridge.lua
        starters.ad_connect), only after AutoDrive's saved network was backed up."""
        self._reserved(tractor_id, owner)
        farm = self._observe().farm
        folder = getattr(self.link.files, "path", None)
        if folder is None:
            raise StepFailed("AD_CONFIG_BACKUP_FAILED")
        backup_autodrive_config(farm.savegame_dir if farm else None, Path(folder).parent.parent,
                                Path(folder) / "backup")
        return self._run("ad_connect", tractor=int(tractor_id), marker=int(marker_id))

    _ad_graph_cache: tuple | None = None

    def ad_graph(self):
        """AutoDrive's road network as the loaded save holds it (companion/hitch.py load_ad_graph; the
        save's own AutoDrive_config.xml, else the profile's active save folder), read only;
        None when there is none to read."""
        farm = self._observe().farm
        folder = getattr(self.link.files, "path", None)
        candidates = [Path(farm.savegame_dir) / "AutoDrive_config.xml"] if farm and farm.savegame_dir else []
        if folder is not None:
            candidates.append(Path(folder).parent.parent / "save" / "AutoDrive_config.xml")
        for source in candidates:
            try:
                stamp = (str(source), source.stat().st_mtime_ns)
                if self._ad_graph_cache is None or self._ad_graph_cache[0] != stamp:
                    self._ad_graph_cache = (stamp, load_ad_graph(source))
                return self._ad_graph_cache[1]
            except (OSError, ValueError) as error:
                log.info("AutoDrive network %s unreadable: %s", source, error)
        return None

    def _unit(self, vehicle_id: str, owner: str) -> None:
        with self._lock:
            reservation = self._reservations.get(owner)
        if reservation is None or vehicle_id not in reservation[1]:
            raise StepFailed("NOT_RESERVED")

    def unpark(self, tractor_id: str, owner: str) -> None:
        self._reserved(tractor_id, owner)
        self._run("unpark", tractor=int(tractor_id))

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

def backup_autodrive_config(savegame_dir: str | None, profile: Path, backup_dir: Path) -> Path:
    """A verified copy of the loaded save's AutoDrive network (AutoDrive_config.xml) before
    Farmhand adds anything to it: the save's own folder, else the profile's active save
    folder. No copy, no change (AD_CONFIG_BACKUP_FAILED)."""
    candidates = [Path(savegame_dir) / "AutoDrive_config.xml"] if savegame_dir else []
    candidates.append(profile / "save" / "AutoDrive_config.xml")
    for source in candidates:
        try:
            data = source.read_bytes()
        except OSError:
            continue
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / time.strftime("AutoDrive_config-%Y%m%d-%H%M%S.xml")
        target.write_bytes(data)
        if target.read_bytes() != data:
            break
        log.info("AutoDrive network backed up: %s -> %s", source, target)
        return target
    raise StepFailed("AD_CONFIG_BACKUP_FAILED")


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
        last = time.monotonic()
        while not link.closed.wait(HEARTBEAT_S):
            link.heartbeat()
            now = time.monotonic()
            if now - last > 3 * HEARTBEAT_S:      # the game drops the session at 10 s
                log.warning("heartbeat late: %.1f s since the previous beat", now - last)
            last = now

    link.heartbeat()

    threading.Thread(target=beat, daemon=True, name="fs25-heartbeat").start()
    return link
