"""Development/qualification only: a live-test hook into the running companion.

Off unless the companion is started with ``--live-test`` (the host runner,
scripts/fs25_host_runner.py, starts it so; the logon start never does). It is an
actuator and a reader, nothing more:
- ``hitch`` asks the coordinator for exactly what the player gets from "hitch the
  forage wagon" (the proposal {"command": "hitch_pair"}); the executor selects,
  reserves, maneuvers, attaches and verifies as always;
- ``request`` runs the player's own typed words: the production rules
  (companion/language.py, as for typed text) turn them into a proposal and the
  coordinator validates and runs it; only job proposals are accepted (no stop,
  status, settings or report), and words the rules do not understand are refused;
- ``stop`` is the player's STOP key;
- ``status`` reads what the companion already observes.
It never moves, attaches, reserves or changes an observation itself.

Transport: files in modSettings/FS25_Farmhand/livetest (local only; no listener):
  request-<id>.json   {"version": 1, "id": "<digits>", "session": "<this companion's
                       session>", "command": "status"|"hitch"|"request"|"stop",
                       "created": <unix seconds>[, "text": "<words>" for request only]},
                       renamed into place atomically;
  response-<id>.json  the answer to exactly that request;
  test-<test id>.json the record of one hitch test, rewritten as it progresses;
  enabled.json        present while this hook runs.
A request is refused unless its version, keys, session and command are exact, its id
is greater than any seen, and it was written in the last REQUEST_MAX_AGE_S.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time

from adapters.world import StepUnverified
from companion.buildinfo import BUILD
from companion.executor import compatible, rejections, usable
from companion.hitch import joint_error, plan_hitch
from companion.language import refuses

log = logging.getLogger("farmhand")

FOLDER = "livetest"
VERSION = 1
COMMANDS = ("status", "hitch", "request", "stop")
# Proposals that are not jobs: never started through a live test.
NOT_JOBS = frozenset({"stop", "status", "settings", "report", "unsupported"})
# Typed words, as a player could type them: letters, digits, spaces, light punctuation.
_TEXT = re.compile(r"[A-Za-z0-9 .,'?!-]{1,160}")
# The proposal the words "hitch the forage wagon" produce (companion/language.py).
HITCH = {"version": 1, "command": "hitch_pair"}
STOP = {"version": 1, "command": "stop"}
REQUEST_MAX_BYTES = 4096
REQUEST_MAX_AGE_S = 30.0
POLL_S = 0.25
MONITOR_POLL_S = 0.2
MONITOR_MAX_S = 23400.0         # beyond the longest plan: three field jobs of at most 2 h (companion/bridge.py DONE_TIMEOUT_S)
MAX_LOG_LINES = 5000
KEEP_TESTS = 20
_ID = re.compile(r"[0-9]{1,20}")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_REQUEST_FILE = re.compile(r"request-([0-9]{1,20})\.json")


class Refused(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("DUPLICATE_KEY")
        result[key] = value
    return result


def _no_constant(_):
    raise Refused("NON_FINITE_NUMBER")


def parse_request(raw: bytes, file_id: str, session: str, last_id: int, now: float, since: float) -> dict:
    """The validated request, or Refused with the reason. ``since``: requests written
    before this companion started are stale."""
    if len(raw) > REQUEST_MAX_BYTES:
        raise Refused("TOO_LARGE")
    try:
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant)
    except Refused:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused("MALFORMED") from None
    base = {"version", "id", "session", "command", "created"}
    if not isinstance(request, dict) or set(request) not in (base, base | {"text"}):
        raise Refused("SCHEMA_VIOLATION")
    if type(request["version"]) is not int or request["version"] != VERSION:
        raise Refused("VERSION_MISMATCH")
    if not isinstance(request["id"], str) or not _ID.fullmatch(request["id"]) or request["id"] != file_id:
        raise Refused("BAD_ID")
    if request["command"] not in COMMANDS:
        raise Refused("UNKNOWN_COMMAND")
    if ("text" in request) != (request["command"] == "request"):
        raise Refused("SCHEMA_VIOLATION")
    if "text" in request and (not isinstance(request["text"], str) or not _TEXT.fullmatch(request["text"])):
        raise Refused("BAD_TEXT")
    created = request["created"]
    if type(created) not in (int, float) or not math.isfinite(created):
        raise Refused("SCHEMA_VIOLATION")
    if not isinstance(request["session"], str) or request["session"] != session:
        raise Refused("SESSION_MISMATCH")
    if int(request["id"]) <= last_id:
        raise Refused("REPLAYED")
    if created < since or abs(now - created) > REQUEST_MAX_AGE_S:
        raise Refused("STALE")
    return request


class _Capture(logging.Handler):
    """The companion's own log lines while one test runs (as companion.log has them)."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record):
        if len(self.lines) < MAX_LOG_LINES:
            self.lines.append(self.format(record))


# FS25 writes one "  Load mod: <name>" line (indented) per mod the loading savegame activates,
# all within a second or so, interleaved with the mods' own lines; loads are minutes apart.
_LOAD_MOD = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+\s{2,}Load mod: (\S+)\s*$")
LOAD_GAP_S = 60
LOG_TAIL_BYTES = 16 * 1024 * 1024


def loaded_mods(path: Path | None) -> list[str] | None:
    """The mods the loaded savegame activated, from FS25's own log of its latest load (the
    last contiguous block of "Load mod:" lines), sorted; None when the log is unreadable."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return None
    block, last = [], None
    for line in lines:
        match = _LOAD_MOD.match(line)
        if match:
            at = time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
            if last is None or at - last > LOAD_GAP_S:
                block = []
            block.append(match.group(2))
            last = at
    return sorted(block)


def _local(at: float) -> str:
    """The wall time as FS25's log.txt and companion.log print it (local time)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) + ".%03d" % int((at % 1) * 1000)


class LiveTest:
    """Polls the folder on its own thread. ``world`` is the FS25World; ``attach`` gives
    it the coordinator once serve() has made one."""

    def __init__(self, world, folder: Path, clock=time.time, game_log: Path | None = None):
        self.world, self.link = world, world.link
        self.folder = folder
        self.game_log = game_log              # FS25's log.txt: the save's mods for each record
        self.clock = clock
        self.started = clock()
        self.coordinator = None
        self._last_id = 0
        self._lock = threading.Lock()
        self.active: dict | None = None
        self._stopped = threading.Event()
        self.model = None
        self._stops = 0                       # live STOPs: a model answer asked before one is void

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        for stale in self.folder.glob("request-*.json"):     # from before this companion: never run
            self._refuse_file(stale, "STALE")
        self._write("enabled.json", {"version": VERSION, "pid": os.getpid(), "build": BUILD,
                                     "started": _local(self.started)})
        log.warning("LIVE TEST INTERFACE ENABLED (development only): %s", self.folder)
        threading.Thread(target=self._poll, daemon=True, name="farmhand-livetest").start()

    def close(self) -> None:
        self._stopped.set()
        try:
            (self.folder / "enabled.json").unlink(missing_ok=True)
        except OSError:
            pass

    def attach(self, coordinator, interpret=None, model=None) -> None:
        """``interpret``: the production rules' text interpreter; ``model``: the user's AI
        for words the rules miss, or None (companion/service.py, as the player's words)."""
        self.coordinator, self.interpret, self.model = coordinator, interpret, model

    # ---------------------------------------------------------------- files
    def _write(self, name: str, value: dict) -> None:
        temporary = self.folder / (name + ".tmp")
        temporary.write_text(json.dumps(value, indent=1), encoding="utf-8")
        for attempt in range(20):            # a reader may hold the target for a moment
            try:
                os.replace(temporary, self.folder / name)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)

    def respond(self, request: dict, ok: bool, code: str, **result) -> None:
        self._write(f"response-{request['id']}.json", {"version": VERSION, "id": request["id"],
                                                       "session": self.link.session, "command": request["command"],
                                                       "ok": ok, "code": code, "result": result})

    def _refuse_file(self, path: Path, code: str) -> None:
        match = _REQUEST_FILE.fullmatch(path.name)
        try:
            path.unlink()
        except OSError:
            return
        log.info("live test request %s refused: %s", path.name, code)
        if match:
            self._write(f"response-{match.group(1)}.json", {"version": VERSION, "id": match.group(1),
                                                            "session": self.link.session, "command": None,
                                                            "ok": False, "code": code, "result": {}})

    # ---------------------------------------------------------------- requests
    def _poll(self) -> None:
        while not self._stopped.wait(POLL_S) and not self.link.closed.is_set():
            try:
                self.poll_once()
            except Exception:              # never the companion: one bad poll is logged
                log.exception("live test poll failed")

    def poll_once(self) -> None:
        files = sorted((p for p in self.folder.glob("request-*.json") if _REQUEST_FILE.fullmatch(p.name)),
                       key=lambda p: int(_REQUEST_FILE.fullmatch(p.name).group(1)))
        for path in files:
            file_id = _REQUEST_FILE.fullmatch(path.name).group(1)
            try:
                raw = path.read_bytes()[:REQUEST_MAX_BYTES + 1]
                path.unlink()
            except OSError:
                continue                       # still being renamed into place: next poll
            try:
                request = parse_request(raw, file_id, self.link.session, self._last_id, self.clock(), self.started)
            except Refused as error:
                log.info("live test request %s refused: %s", file_id, error.code)
                self._write(f"response-{file_id}.json", {"version": VERSION, "id": file_id,
                                                         "session": self.link.session, "command": None,
                                                         "ok": False, "code": error.code, "result": {}})
                continue
            self._last_id = int(request["id"])
            log.info("live test request %s: %s", request["id"], request["command"])
            if request["command"] == "status":
                self.respond(request, True, "STATUS", **self.status())
            else:
                # The serve loop runs it, exactly where the player's requests run.
                self.link.inject({"live": request})

    # ---------------------------------------------------------------- serve loop
    def run(self, request: dict, coordinator) -> None:
        """On the serve thread (companion/service.py): never waits for a job."""
        if request["command"] == "stop":
            self._stops += 1
            coordinator.submit(dict(STOP))
            with self._lock:
                active = self.active and self.active["test_id"]
            self.respond(request, True, "STOP_SENT", test_id=active)
            return
        with self._lock:
            if self.active is not None:
                self.respond(request, False, "TEST_ACTIVE", test_id=self.active["test_id"])
                return
        interpreter = None
        if request["command"] == "request":
            if "model_proposal" in request:            # the model's answer, back on this thread
                if request["stops"] != self._stops:
                    self.respond(request, False, "STOPPED_BEFORE_START")
                    return
                proposal, interpreter = request["model_proposal"], "model"
            else:
                proposal = self.interpret(request["text"]) if self.interpret is not None else None
                interpreter = "rules"
                log.info("live test request %s text %r -> %s", request["id"], request["text"], json.dumps(proposal))
                if proposal is None and self.model is not None and not refuses(request["text"]):
                    # As the player's words (companion/service.py ask_model): off this thread,
                    # so STOP is never held up while the model thinks.
                    threading.Thread(target=self._ask_model, args=(request, self._stops), daemon=True,
                                     name="farmhand-livetest-model").start()
                    return
            if proposal is None:
                self.respond(request, False, "NOT_UNDERSTOOD")
                return
            if proposal.get("command") in NOT_JOBS:
                self.respond(request, False, "NOT_A_JOB", proposal=proposal)
                return
        else:
            proposal = dict(HITCH)
        capture = _Capture()
        log.addHandler(capture)
        job = coordinator.submit(dict(proposal))
        if job is None:                            # BUSY with a player's job, or not understood
            log.removeHandler(capture)
            self.respond(request, False, "BUSY")
            return
        at = self.clock()
        record = {"version": VERSION, "test_id": job.owner, "job": job.number, "request": request["id"],
                  "proposal": proposal, "text": request.get("text"), "companion_build": BUILD, "mod_build": self.mod_build(),
                  "started": _local(at), "started_unix": at, "ended": None, "ended_unix": None,
                  "status": "WORKING", "reason": None, "steps": [], "states": [], "equipment": [],
                  "final_observation": None, "companion_log": [], "configuration": self.configuration(),
                  "interpreter": interpreter}
        with self._lock:
            self.active = record
        self._save(record, capture)
        self.respond(request, True, "STARTED", test_id=job.owner)
        threading.Thread(target=self._monitor, args=(job, record, capture, coordinator), daemon=True,
                         name="farmhand-livetest-monitor").start()

    def _ask_model(self, request: dict, stops: int) -> None:
        proposal = self.model(request["text"])
        log.info("live test request %s model %r -> %s", request["id"], request["text"], json.dumps(proposal))
        self.link.inject({"live": dict(request, model_proposal=proposal, stops=stops)})

    def _save(self, record: dict, capture: _Capture) -> None:
        record["companion_log"] = list(capture.lines)
        self._write(f"test-{record['test_id']}.json", record)

    def _monitor(self, job, record: dict, capture: _Capture, coordinator) -> None:
        deadline = self.clock() + MONITOR_MAX_S
        try:
            while job.state == "WORKING" and self.clock() < deadline:
                if job.step is not None and (not record["steps"] or record["steps"][-1]["step"] != job.step):
                    record["steps"].append({"step": job.step, "at": _local(self.clock())})
                    self._save(record, capture)
                time.sleep(MONITOR_POLL_S)
            while coordinator.current is job and self.clock() < deadline:     # its cleanup and notice
                time.sleep(MONITOR_POLL_S)
            at = self.clock()
            if job.state == "WORKING":
                record.update(status="UNVERIFIED", reason="MONITOR_TIMEOUT")
            else:
                record.update(status=job.state, reason=job.reason)
            record.update(ended=_local(at), ended_unix=at, states=list(job.states), equipment=list(job.equipment))
            record["final_observation"] = self._observe(job.equipment)
        except Exception:
            log.exception("live test monitor failed")
            record.update(status="UNVERIFIED", reason="MONITOR_FAILED")
        finally:
            log.removeHandler(capture)
            self._save(record, capture)
            with self._lock:
                self.active = None
            self._prune()

    def _observe(self, ids) -> dict | None:
        """What a fresh snapshot shows of the test's pair at its end (read only)."""
        try:
            items = {item.id: item for item in self.world.snapshot()}
        except StepUnverified as error:
            return {"error": error.code}
        return {i: {"attached_to": items[i].attached_to, "stopped": items[i].stopped,
                    "position": list(items[i].position), "busy": items[i].busy, "occupied": items[i].occupied}
                if i in items else None for i in ids}

    def _prune(self) -> None:
        tests = sorted(self.folder.glob("test-*.json"), key=lambda p: p.stat().st_mtime)
        for old in tests[:-KEEP_TESTS]:
            old.unlink(missing_ok=True)
        cutoff = time.time() - 600
        for old in self.folder.glob("response-*.json"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)

    # ---------------------------------------------------------------- status
    def configuration(self) -> dict:
        """What this test ran on besides the builds: map, savegame slot and the save's mods."""
        try:
            frame = self.link.fresh_snapshot()
        except (StepUnverified, KeyError, TypeError):
            frame = {}
        return {"map": frame.get("map_id"), "savegame": (frame.get("farm") or {}).get("savegame"),
                "mods": loaded_mods(self.game_log)}

    def mod_build(self) -> str | None:
        hello = getattr(self.link, "hello", None)
        return None if hello is None else hello.get("build", "unknown")

    def status(self) -> dict:
        link, problems = self.link, []
        session, last_frame = link.linked_session()
        age = None if last_frame is None else round(link.clock() - last_frame, 2)
        hello = getattr(link, "hello", None) or {}
        game = {"linked": session == link.session, "snapshot_session": session, "last_frame_age_s": age,
                "mod_version": hello.get("mod_version"), "mod_build": self.mod_build(),
                "autodrive_version": hello.get("autodrive_version")}
        farm = candidates = None
        try:
            frame = link.fresh_snapshot()
            farm = {"farm_id": str(frame["farm_id"]), "map_id": frame.get("map_id"),
                    "player": frame.get("player"), "info": frame.get("farm"), "fields": frame.get("fields"),
                    "places": [(p.get("name"), round(p.get("x")), round(p.get("z"))) for p in frame.get("places") or []][:60],
                    "markers": [(d.get("name"), round(d.get("x")), round(d.get("z")))
                                for d in frame.get("destinations") or []][:300],
                    # Why a motorized vehicle is no towing vehicle (adapters/fs25.py _towing_vehicle).
                    "unclassified": [{k: v.get(k) for k in ("id", "name", "category", "self_propelled", "motor",
                                                            "kinds", "output_joints")}
                                     for v in frame.get("vehicles") or []
                                     if v.get("motor") is not None or v.get("self_propelled")][:20],
                    # Why an implement pairs with no tractor (hitch_problem): its input joints and PTO need.
                    "implements": [{k: v.get(k) for k in ("id", "name", "pto_torque", "input_joint_poses")}
                                   for v in frame.get("vehicles") or []
                                   if v.get("motor") is None and not v.get("self_propelled")][:40]}
            candidates = self.candidates(farm["farm_id"])
        except StepUnverified as error:
            problems.append("GAME_" + error.code)
        if not game["linked"]:
            problems.append("GAME_NOT_LINKED")
        if BUILD == "source":
            problems.append("COMPANION_BUILD_UNSTAMPED")      # run from a checkout, not installed
        elif game["mod_build"] != BUILD:
            problems.append("MOD_BUILD_MISMATCH")             # e.g. the game still runs an older mod
        if farm is not None and not isinstance(farm["player"], dict):
            problems.append("NO_PLAYER_POSITION")
        if candidates is not None and candidates["pair"] is None:
            problems.append("NO_CANDIDATE_PAIR")
        coordinator = self.coordinator
        current = coordinator.current if coordinator is not None else None
        last = coordinator.last if coordinator is not None else None
        with self._lock:
            active = self.active and self.active["test_id"]
        if current is not None:
            problems.append("JOB_RUNNING")
        if active:
            problems.append("TEST_ACTIVE")
        return {"companion": {"build": BUILD, "session": link.session, "pid": os.getpid(), "live_test": True},
                "game": game, "farm": farm, "candidates": candidates,
                "job": {"running": None if current is None else {"owner": current.owner, "step": current.step},
                        "last": None if last is None else {"owner": last.owner, "state": last.state,
                                                           "reason": last.reason}},
                "test_active": active, "ready": not problems, "problems": problems}

    def candidates(self, farm_id: str) -> dict:
        """A read-only preview with the executor's own rules (usable, compatible,
        plan_hitch, in its order): what FIND would see now. The executor decides."""
        items = sorted(self.world.snapshot(), key=lambda item: (not item.ridden, item.id))
        relevant = [i for i in items if i.category in ("tractor", "forage_wagon")]
        free = [i for i in relevant if usable(i, farm_id) and i.attached_to is None and i.stopped]
        pair = None
        for tractor in free:
            for wagon in free:
                plan = compatible(tractor, wagon) and plan_hitch(tractor, wagon)
                if plan:
                    error = joint_error(tractor, wagon, plan)
                    pair = {"tractor": tractor.id, "wagon": wagon.id,
                            "joints": [plan.tractor_joint, plan.wagon_joint],
                            "observed": error.describe() if error else None}
                    break
            if pair:
                break
        return {"equipment": [{"id": i.id, "category": i.category,
                               "rejected": [] if i in free else rejections(i, farm_id) or ["UNKNOWN"]}
                              for i in relevant][:40],
                "pair": pair,
                # Everything of this farm as observed (read only): what the planner can work with.
                "inventory": [{"id": i.id, "category": i.category, "name": i.name, "kinds": sorted(i.kinds),
                               "attached_to": i.attached_to, "position": [round(i.position[0], 1),
                                                                          round(i.position[1], 1)],
                               "heading": None if i.heading is None else round(i.heading, 3),
                               # GIANTS Foldable and TensionBelts state, when it has them.
                               **({"fold": i.fold, "folding": i.folding} if i.fold is not None else {}),
                               **({"belts_fastened": i.belts_fastened, "belted": list(i.belted),
                                   "belt_markers": [[round(c, 2) for c in m] for m in i.belt_markers]}
                                  if i.belts_fastened is not None else {}),
                               **({"belted_to": i.belted_to} if i.belted_to is not None else {}),
                               "fill_units": [{"index": u.index, "fill_type": u.fill_type, "level": u.level,
                                               "capacity": u.capacity, "role": u.role,
                                               "supported": sorted(u.supported)[:12]} for u in i.fill_units]}
                              for i in items if i.farm_id == farm_id][:120]}
