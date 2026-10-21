"""One job at a time for the player; STOP and STATUS never wait for it.

Every way of asking (the fixed key, typed text, later speech through a local
model) ends here as a command proposal (protocol/command.schema.json). A proposal
carries no authority: this module adds the request id and farm, and the executor
validates the resulting intent and selects all equipment. STOP is deterministic
and immediate: it marks the job cancelled (no further step starts) and asks the
game to stop the reserved tractor, which pre-empts the running operation.

Replies are notice tokens shown in game (CommandLink.notice, mod/FarmhandBridge.lua
Bridge.noticeText). No simulator import; no game calls except through the World.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time

from jsonschema import Draft202012Validator

from adapters.world import StepFailed, StepUnverified
from companion import hitch as transport   # its tractor-transport section
from companion.executor import (OPERATIONS, already_done, crop_problem, named, of_kind, resolve_field, seed_position,
                                usable)
from protocol.model import Equipment, Result

log = logging.getLogger("farmhand")
COMMAND_SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "protocol/command.schema.json").read_text())
COMMAND_VALIDATOR = Draft202012Validator(COMMAND_SCHEMA)
# A proposal the key always produces: no interpretation involved.
FETCH_HERE = {"version": 1, "command": "fetch_pair"}


def token(text: str) -> str:
    """A notice token (protocol.ipc encode_session)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)[:64]


# Steps worth telling the player about while a job works (WORKING_<step>); the
# rest is visible on request (STATUS_WORKING_<step>).
# Steps that move equipment: STATUS says "driving" only when it is observed moving.
MOTION_STEPS = frozenset({"TRAVEL_NEAR", "STAGE", "LOCAL_MANEUVER", "ALIGN_REVERSE", "PULL_CLEAR", "UNPARK",
                          "DELIVER", "FIELD_WORK", "APPROACH", "LOAD", "UNLOAD", "FOLLOW"})
MILESTONES = frozenset({"FIELD_WORK", "PULL_CLEAR", "TRAVEL_NEAR", "STAGE", "LOCAL_MANEUVER", "DELIVER",
                        "RAMPS_DOWN", "LOAD", "BELTS", "RAMPS_UP", "UNLOAD"})
# The executor task and the player-facing notices of each job command.
JOBS = {"fetch_pair": ("deliver_tractor_and_forage_wagon", "ACCEPTED", "COMPLETE"),
        "move_pair": ("move_pair", "ACCEPTED_MOVE", "COMPLETE_MOVE"),
        "hitch_pair": ("hitch_pair", "ACCEPTED_HITCH", "COMPLETE_HITCH"),
        "detach_pair": ("detach_pair", "ACCEPTED_DETACH", "COMPLETE_DETACH"),
        "field_work": ("field_work", "ACCEPTED_FIELD_WORK", "COMPLETE_FIELD_WORK"),
        "buy": ("buy", "ACCEPTED_BUY", "COMPLETE_BUY"),
        "save_game": ("save_game", "ACCEPTED_SAVE", "COMPLETE_SAVE"),
        "load_tractor": ("load_tractor", "ACCEPTED_LOAD", "COMPLETE_LOAD"),
        "unload_tractor": ("unload_tractor", "ACCEPTED_UNLOAD", "COMPLETE_UNLOAD"),
        "sequence": ("sequence", "ACCEPTED_SEQUENCE", "COMPLETE_SEQUENCE"),
        "haul_tractor": ("haul_tractor", "ACCEPTED_HAUL", "COMPLETE_HAUL"),
        "follow_me": ("follow_me", "ACCEPTED_FOLLOW", "COMPLETE_FOLLOW")}
# The proposal fields that pass into a job command's intent.
NAMES = ("tractor_name", "implement_name", "cargo_name")


def result_notice(status: str, reason: str, complete: str = "COMPLETE") -> str:
    if status == "COMPLETE":
        # Unhitched, but the tractor could not pull clear (nothing moved): say so.
        return complete + "_NOT_CLEAR" if reason == "DETACHED_NOT_CLEAR" else complete
    if status == "CANCELLED":
        return status
    return token(f"{status}_{reason}")


def owned_like(items: tuple[Equipment, ...], farm_id: str, wants) -> int:
    """How many of the farm's vehicles answer these wants (by name or brand words, or a
    plow by kind): a purchase step's before-and-after measure."""
    count = 0
    for item in items:
        if item.farm_id != farm_id:
            continue
        for want in wants:
            if want.get("name") and named(item, want["name"].casefold()):
                count += 1
            elif not want.get("name") and want["kind"] == "plow" and of_kind(item, "plow"):
                count += 1
    return count


# ------------------------------------------------------------- sequence context
# What "it" is in a sequence: the tractor a transport step worked with (by its store name,
# so a reload's new IDs do not matter), the rig carrying it, and what was just bought.
def with_context(step: dict, context: dict) -> dict:
    step = dict(step)
    tractor = context.get("tractor")
    if tractor and step["command"] in ("haul_tractor", "load_tractor", "unload_tractor") and not step.get("tractor_name"):
        step["tractor_name"] = tractor
    if tractor and step["command"] == "move_pair" and not step.get("tractor_name") and not step.get("cargo_name") \
            and context.get("loaded"):
        step["cargo_name"] = tractor                      # "bring it back", "deliver it": the rig carrying it
    if step.get("carry") and not step.get("carry_name"):
        kind = _carry_kind(step["carry"])
        if kind and context.get("bought", {}).get(kind):
            step["carry_name"] = context["bought"][kind]  # "load the tractor and plow": the plow just bought
    return step


def remember(context: dict, step: dict, result: Result, items) -> None:
    observed = {i.id: i for i in items}
    if step["command"] in ("haul_tractor", "load_tractor", "unload_tractor") and result.equipment_ids:
        # The carried tractor, never the truck towing the transport (a resumed haul's first
        # unit may be the truck).
        cargo = [observed[i] for i in result.equipment_ids if i in observed and observed[i].category == "tractor"
                 and not transport.is_transport(observed[i])
                 and not (observed.get(observed[i].attached_to) is not None
                          and transport.is_transport(observed[observed[i].attached_to]))]
        if cargo and _words(cargo[0].name):
            context["tractor"] = _words(cargo[0].name)
        context["loaded"] = step["command"] == "load_tractor"
    elif step["command"] == "buy":
        for want in step["wants"]:
            if want.get("name"):
                context.setdefault("bought", {})[want["kind"]] = _words(want["name"])


def _carry_kind(words: str) -> str | None:
    from companion.language import implement_kind
    return implement_kind(words) or ("plow" if re.search(r"\bplows?\b|\bploughs?\b", words or "") else None)


def _tractor_for(items, farm_id: str, name: str | None, transports):
    """The tractor a transport goal means: named, else the one on a transport, else the free
    tractor nearest a transport (within 50 m; "the tractor" beside the deckover)."""
    towing = {t.attached_to for t in transports}          # the truck towing a transport is never it
    if name:
        found = [i for i in items if i.category == "tractor" and i.farm_id == farm_id and named(i, name)
                 and not transport.is_transport(i) and i.id not in towing]
    else:
        found = [i for i in items if i.category == "tractor" and i.farm_id == farm_id and i.id not in towing
                 and any(i.belted_to == t.id or transport.on_deck(i, t) for t in transports)]
        if not found:
            near = sorted((min(math.dist(i.position, t.position) for t in transports), i.id, i) for i in items
                          if i.category == "tractor" and i.farm_id == farm_id and i.id not in towing and transports
                          and not transport.is_transport(i))
            found = [near[0][2]] if near and near[0][0] <= 50.0 else []
    return found


def load_with_step(items, farm_id: str, work: dict):
    """The next step of "load the tractor and plow": (intent fragment, None) or (None, why)."""
    observed = {i.id: i for i in items}
    transports = [i for i in items if transport.is_transport(i) and i.farm_id == farm_id]
    tractors = _tractor_for(items, farm_id, work.get("tractor_name"), transports)
    if len(tractors) != 1:
        return None, "NO_SUCH_TRACTOR" if not tractors else "TRACTOR_AMBIGUOUS"
    tractor = tractors[0]
    kind = _carry_kind(work.get("carry"))
    if kind is None:
        return None, "UNKNOWN_IMPLEMENT"
    words = work.get("tractor_name") or _words(tractor.name)
    implement = observed.get(tractor.attached_to)
    carrying = implement is not None and of_kind(implement, kind) and named(implement, work.get("carry_name"))
    aboard = tractor.belted_to is not None or any(transport.on_deck(tractor, t) for t in transports)
    if carrying and aboard and tractor.belted_to is not None:
        return None, "LOADED"
    if carrying:
        step = {"task": "load_tractor", "tractor_name": words}
        if work.get("implement_name"):
            step["implement_name"] = work["implement_name"]
        return step, None
    if aboard:
        return {"task": "unload_tractor", "tractor_name": words}, None
    if implement is not None:
        return None, "TRACTOR_CARRIES_SOMETHING_ELSE"
    step = {"task": "hitch_pair", "implement": kind, "tractor_name": words}
    if work.get("carry_name"):
        step["implement_name"] = work["carry_name"]
    return step, None


# ------------------------------------------------------------- haul planning
def _words(name: str | None) -> str | None:
    """A store name as intent words (lower case, letters, digits, spaces, hyphens)."""
    words = re.sub(r"[^a-z0-9 -]+", " ", (name or "").casefold()).split()
    return " ".join(words)[:40].strip() or None


def haul_step(items: tuple[Equipment, ...], farm_id: str, name: str | None, target, delivered: bool):
    """The next executor step of a haul goal from observed state, as an intent fragment,
    or (None, why): HAULED when it is done; NO_SUCH_TRACTOR, TRACTOR_AMBIGUOUS,
    NO_TRANSPORT when it cannot go on."""
    observed = {i.id: i for i in items}
    transports = [i for i in items if transport.is_transport(i) and i.farm_id == farm_id]
    tractors = _tractor_for(items, farm_id, name, transports)
    if not tractors:
        return None, "NO_SUCH_TRACTOR"
    if len(tractors) > 1:
        return None, "TRACTOR_AMBIGUOUS"
    tractor = tractors[0]
    trailer = observed.get(tractor.belted_to) or next((t for t in transports if transport.on_deck(tractor, t)), None)
    words = name or _words(tractor.name)
    destination = {} if target in (None, "here") else {"target": target}
    if delivered:
        if trailer is None:
            return None, "HAULED"
        return {"task": "unload_tractor", "target": "player_at_acceptance", "tractor_name": words}, None
    if trailer is not None and tractor.belted_to == trailer.id and trailer.fold == 0.0 \
            and observed.get(trailer.attached_to) is not None:
        return {"task": "move_pair", "target": "player_at_acceptance", **destination, "cargo_name": words}, None
    if trailer is not None:                 # aboard, not yet belted or the ramps not up: finish it
        return {"task": "load_tractor", "target": "player_at_acceptance", "tractor_name": words,
                "implement_name": _words(trailer.name)}, None
    hitched = [t for t in transports if observed.get(t.attached_to) is not None]
    if hitched:
        return {"task": "load_tractor", "target": "player_at_acceptance", "tractor_name": words}, None
    if transports:
        return {"task": "hitch_pair", "target": "player_at_acceptance", "implement": "trailer",
                "implement_name": _words(transports[0].name)}, None
    return None, "NO_TRANSPORT"


# ------------------------------------------------------------- field planning
# A field-work goal as executor steps, one operation at a time, from fresh state. The
# planner decides only what happens next; the executor selects, validates and
# verifies every step. For each operation: hitch an implement that does it (unless one
# is already hitched to a tractor), work the field, then unhitch it where it stands
# (PULL_CLEAR included) so the next operation's tool change has a tractor.
def implement_kind(items: tuple[Equipment, ...], operation: str, farm_id: str,
                   crop: str | None = None) -> tuple[str | None, str | None]:
    """The implement kind to use for ``operation``: the first of its kinds the farm
    has (owned, available); a cultivator stands in for a plow only when the farm has
    no plow (said in the note). None and why when the farm has none; with ``crop``,
    a seeder that can sow it."""
    if crop is not None and not any(of_kind(i, "sowing_machine") and usable(i, farm_id) and "ai_implement" in i.kinds
                                    and seed_position(i, crop) is not None for i in items):
        return None, "NO_SEEDER_FOR_CROP"
    kinds = OPERATIONS[operation].kinds
    if operation == "plow" and not _have(items, "plow", farm_id):
        kinds = OPERATIONS["cultivate"].kinds
        if _have(items, "cultivator", farm_id):
            return "cultivator", "NO_PLOW_CULTIVATING"
    for kind in kinds:
        if _have(items, kind, farm_id):
            return kind, None
    return None, "NO_IMPLEMENT_FOR_" + operation.upper()


def _have(items, kind, farm_id) -> bool:
    return any(of_kind(i, kind) and usable(i, farm_id) and "ai_implement" in i.kinds for i in items)


def _hitched(items, kind, crop=None) -> bool:
    observed = {i.id: i for i in items}
    return any(i.category == "tractor" and observed.get(i.attached_to) is not None
               and of_kind(observed[i.attached_to], kind)
               and (crop is None or seed_position(observed[i.attached_to], crop) is not None) for i in items)


def field_steps(items: tuple[Equipment, ...], operation: str, field, farm_id: str, area_m2=None, crop=None):
    """The steps for one operation on ``field`` (an id or "smallest"), as executor
    intent fragments, or (None, why). ``crop`` (sowing only): the player's crop."""
    crop = crop if operation == "sow" else None
    kind, note = implement_kind(items, operation, farm_id, crop)
    if kind is None:
        return None, note
    work_op = "cultivate" if note == "NO_PLOW_CULTIVATING" else operation
    with_crop = {"crop": crop} if crop else {}
    steps = []
    if not _hitched(items, kind, crop):
        steps.append({"task": "hitch_pair", "implement": kind, **with_crop})
    steps.append({"task": "field_work", "field": field, "operation": work_op, "implement": kind,
                  **({"area_m2": area_m2} if area_m2 else {}), **with_crop})
    # Back where it was taken from, then unhitched there: never left in the field.
    steps.append({"task": "move_pair", "implement": kind, "target": {"type": "origin"}})
    steps.append({"task": "detach_pair", "implement": kind})
    return steps, note


@dataclass
class Job:
    number: int
    owner: str                    # the executor request id: reservation owner
    state: str = "WORKING"        # WORKING, then COMPLETE/BLOCKED/FAILED/UNVERIFIED/CANCELLED
    step: str | None = None
    reason: str | None = None
    spoken: bool = False          # asked by voice: replies are spoken too
    stop_sent: bool = False       # at most one requester stop per job
    states: tuple = ()            # the executor's result: states entered, equipment used
    equipment: tuple = ()
    command: str | None = None    # the job command (JOBS key)


# Goals that are kept when they do not complete (stopped, taken over, blocked, failed):
# "continue" re-plans them from the observed state; their ``work`` carries the progress
# (a haul's observed delivery, a sequence's completed steps), so nothing done repeats.
GOALS = frozenset({"haul_tractor", "sequence"})


@dataclass
class Kept:
    command: str
    target: object
    implement: str | None
    work: dict


class Coordinator:
    """``world`` is the FS25World; ``executor`` an Executor over it; ``notify(code,
    spoken)`` shows a token in game (and speaks it when the request was spoken); ``farm`` returns the linked farm id, or raises
    StepUnverified when the game is not linked to this companion."""

    def __init__(self, world, executor, notify, farm, session, goal_file: Path | None = None):
        self.world, self.executor, self.notify = world, executor, notify
        self.farm, self.session = farm, session
        # The kept goal survives a game reload or a companion restart: "continue" re-plans it
        # from the observed world (equipment by name and brand; session IDs change).
        self.goal_file = goal_file
        self._lock = threading.Lock()
        self._jobs = 0
        self.current: Job | None = None
        self.last: Job | None = None
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        # The request id of the executor job running now: a plan's step reserves under its
        # own id, and STOP must pre-empt that reservation's running bridge operation.
        self.running: str | None = None
        self.kept: Kept | None = self._load_goal()  # the goal "continue" resumes
        self._then = None                         # a destination to resume with once this job ends

    @property
    def busy(self) -> bool:
        with self._lock:
            return self.current is not None

    def submit(self, proposal, spoken: bool = False) -> Job | None:
        """Answer one proposal now; a job runs on its own thread and is returned
        (None when none started). ``spoken``: the request was spoken, so this answer
        (and that job's notices) are too."""
        if proposal is None or not COMMAND_VALIDATOR.is_valid(proposal):
            self.notify("NOT_UNDERSTOOD", spoken)
            return None
        command = proposal["command"]
        if command == "stop":
            self.stop(spoken)
        elif command == "status":
            self.notify(self.status(), spoken)
        elif command == "unsupported":                 # understood: say exactly what is missing
            self.notify(token(f"UNSUPPORTED_{proposal['reason'].upper()}"), spoken)
        elif command == "resume":
            return self.resume(spoken)
        elif command == "retarget":
            return self.retarget(proposal["target"], spoken)
        else:
            work = ({"field": proposal["field"], "operations": proposal["operations"],
                     "area_m2": proposal.get("area_m2"), "over_crop": proposal.get("over_crop", False),
                     "crop": proposal.get("crop")}
                    if command == "field_work"
                    else {"wants": proposal["wants"]} if command == "buy" else None)
            if command in ("fetch_pair", "hitch_pair", "move_pair", "load_tractor", "unload_tractor", "follow_me"):
                names = {k: proposal[k] for k in NAMES if k in proposal}
                work = names or None
            elif command == "sequence":
                work = {"steps": proposal["steps"]}
            elif command == "haul_tractor":
                work = {"tractor_name": proposal.get("tractor_name"), "target": proposal.get("target", "here")}
            return self.start(command, proposal.get("target", "here"), spoken, proposal.get("implement"), work)
        return None

    # ------------------------------------------------------------------ status
    def status(self) -> str:
        with self._lock:
            job, last = self.current, self.last
        if job is not None:
            step = job.step or "FIND"
            if step in MOTION_STEPS and not self._moving():
                return token(f"STATUS_WAITING_{step}")    # a pending command is not motion
            return token(f"STATUS_WORKING_{step}")
        if self.kept is not None:
            return "STATUS_PAUSED"
        if last is not None:
            return token(f"STATUS_LAST_{last.state}")
        return "STATUS_IDLE"

    def _moving(self) -> bool:
        """Whether a unit the running step reserved is observed moving now."""
        ids = set(getattr(self.executor, "active", ()) or ())
        if not ids:
            return False
        try:
            return any(item.id in ids and not item.stopped for item in self.world.snapshot())
        except Exception:                          # unobserved is not moving
            return False

    # -------------------------------------------------------------------- stop
    def stop(self, spoken: bool = False) -> None:
        """Deterministic and immediate; never waits for an interpreter or the job.
        One requester stop per job, never during its own cleanup: a second stop
        command would pre-empt the job's confirming stop."""
        with self._lock:
            job = self.current
            send = False
            if job is not None:
                self._cancel.set()
                send = not job.stop_sent and job.step not in ("STOP", "RELEASE")
                job.stop_sent = job.stop_sent or send
        if job is None:
            retained = self.world.retained()
            self.notify("STOPPING" if retained else "NOTHING_TO_STOP", spoken)
            for owner in retained:                 # an earlier job's unconfirmed stop
                threading.Thread(target=self.world.recover, args=(owner,), daemon=True).start()
            return
        self.notify("STOPPING", spoken)
        if send:
            threading.Thread(target=self._stop_reserved, args=(self.running or job.owner,), daemon=True,
                             name="farmhand-stop").start()

    def _stop_reserved(self, owner: str) -> None:
        """Pre-empt the running operation; the job's own cleanup confirms the stop."""
        ids = self.world.reservation(owner)
        if ids is None:
            return                                 # not reserved yet: the cancel suffices
        try:
            self.world.stop(ids[0], owner)
        except (StepFailed, StepUnverified, ValueError) as error:
            log.info("stop for %s: %s", owner, getattr(error, "code", error))

    # -------------------------------------------------------------------- jobs
    def start(self, command: str, target, spoken: bool = False, implement: str | None = None,
              work: dict | None = None) -> Job | None:
        with self._lock:
            if self.current is not None:
                busy = True
            else:
                busy = False
                self._jobs += 1
                self._cancel = threading.Event()
                self.current = Job(self._jobs, f"{self.session()}-{self._jobs}", spoken=spoken, command=command)
                job, cancel = self.current, self._cancel
        if busy:
            self.notify("BUSY", spoken)
            return None
        self.notify(JOBS[command][1], spoken)
        worker = threading.Thread(target=self._run, args=(job, cancel, command, target, implement, work),
                                  daemon=True, name=f"farmhand-job-{job.number}")
        worker.start()
        self._worker = worker                      # published started: wait() never joins an unstarted thread
        return job

    def _run(self, job: Job, cancel: threading.Event, command: str, target, implement: str | None = None,
             work: dict | None = None) -> None:
        task, _, complete = JOBS[command]
        try:
            for owner in self.world.retained():   # an earlier job's unconfirmed stop
                self.world.recover(owner)

            def entered(state):
                job.step = state
                if state in MILESTONES:
                    self.notify(f"WORKING_{state}", job.spoken)

            self.world.cancel = cancel           # the link refuses steps once it is set
            if command == "field_work":
                result = self._field_work(job, cancel, entered, work)
            elif command == "buy":
                result = self._buy(job, cancel, entered, work)
            elif command == "sequence":
                result = self._sequence(job, cancel, entered, work)
            elif command == "haul_tractor":
                result = self._haul(job, cancel, entered, work)
            else:
                intent = {"version": 1, "request_id": job.owner, "task": task,
                          "farm_id": self.farm(), "target": "player_at_acceptance" if target == "here" else target}
                if implement is not None:
                    intent["implement"] = implement
                intent.update(work or {})            # tractor_name / implement_name
                log.info("job %d intent %s", job.number, json.dumps(intent))
                self.running = intent["request_id"]
                result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
            job.states, job.equipment = tuple(result.states), tuple(result.equipment_ids)
            job.state, job.reason = result.status, result.reason
            log.info("job %d result %s", job.number, json.dumps({
                "status": result.status, "reason": result.reason,
                "equipment": list(result.equipment_ids), "states": list(result.states)}))
        except StepUnverified as error:            # not linked: nothing was sent
            job.state, job.reason = "BLOCKED", error.code
        except Exception:                          # the executor already stopped its own work
            log.exception("job %d failed", job.number)
            job.state, job.reason = "FAILED", "INTERNAL_ERROR"
        finally:
            self.world.cancel = None
            self.running = None
            then = None
            with self._lock:
                self.current, self.last = None, job
                if command in GOALS:
                    self.kept = None if job.state == "COMPLETE" else Kept(command, target, implement, work)
                    self._save_goal()
                then, self._then = self._then, None
            notice = result_notice(job.state, job.reason or "", complete)
            if command in GOALS and job.state != "COMPLETE" and then is None:
                notice = "PAUSED" if job.state == "CANCELLED" else notice
            self.notify(notice, job.spoken)
            if then is not None and self.kept is not None:
                self._retarget_kept(then)
                self.resume(job.spoken)

    # ---------------------------------------------------------- resume / retarget
    def resume(self, spoken: bool = False) -> Job | None:
        """Continue the kept goal from the observed state (nothing completed repeats)."""
        with self._lock:
            kept, busy = self.kept, self.current is not None
        if busy:
            self.notify("BUSY", spoken)
            return None
        if kept is None:
            self.notify("NOTHING_TO_RESUME", spoken)
            return None
        log.info("resuming %s to %s with %s", kept.command, kept.target, kept.work)
        self.notify("RESUMING", spoken)
        return self.start(kept.command, kept.target, spoken, kept.implement, kept.work)

    def retarget(self, target, spoken: bool = False) -> Job | None:
        """A new destination for the goal: a running one is stopped and resumed with it
        once its stop is confirmed; a kept one is resumed with it."""
        with self._lock:
            job = self.current
            running_goal = job is not None and job.command in GOALS
            if running_goal:
                self._then = target
        if running_goal:
            self.notify("RETARGETED", spoken)
            self.stop(spoken)
            return None
        if job is not None:
            self.notify("BUSY", spoken)
            return None
        if self.kept is None:
            self.notify("NOTHING_TO_RESUME", spoken)
            return None
        self._retarget_kept(target)
        self.notify("RETARGETED", spoken)
        return self.resume(spoken)

    def _load_goal(self) -> Kept | None:
        if self.goal_file is None:
            return None
        try:
            data = json.loads(self.goal_file.read_text(encoding="utf-8"))
            if data.get("command") not in GOALS or not isinstance(data.get("work"), dict):
                raise ValueError("not a goal")
            log.info("kept goal from %s: %s", self.goal_file, data)
            return Kept(data["command"], data.get("target", "here"), data.get("implement"), data["work"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError) as error:
            log.info("kept goal unreadable (%s): ignored", error)
            return None

    def _save_goal(self) -> None:
        """Atomically: the kept goal, or no file when there is none."""
        if self.goal_file is None:
            return
        try:
            if self.kept is None:
                self.goal_file.unlink(missing_ok=True)
                return
            self.goal_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.goal_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"command": self.kept.command, "target": self.kept.target,
                                       "implement": self.kept.implement, "work": self.kept.work}), encoding="utf-8")
            os.replace(tmp, self.goal_file)
        except OSError as error:
            log.info("kept goal not saved: %s", error)

    def _retarget_kept(self, target) -> None:
        kept = self.kept
        kept.target = target
        if kept.command == "haul_tractor":
            kept.work["target"] = target
            kept.work["delivered"] = False          # delivered somewhere else: deliver again
        elif kept.command == "sequence":
            for n, step in enumerate(kept.work["steps"]):
                if n >= kept.work.get("done", 0) and step["command"] in ("move_pair", "fetch_pair"):
                    step["target"] = target
        log.info("goal %s retargeted to %s", kept.command, target)
        self._save_goal()

    def _sequence(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """The player's commands in order, one job: each step starts only after the one
        before it completed (COMPLETE, from its own verification) and only if not cancelled;
        the first that did not complete ends it with its own result."""
        states, used = [], []
        work.setdefault("done", 0)                # kept with the goal: a resume starts after these
        for n, step in enumerate(work["steps"], 1):
            if n <= work["done"]:
                states.append(f"{step['command']}:ALREADY_DONE")
                continue
            if cancel.is_set():
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", tuple(used), tuple(states))
            command = step["command"]
            if command == "buy":
                baseline = work.setdefault("baseline", {})
                count = owned_like(tuple(self.world.snapshot()), self.farm(), step["wants"])
                if str(n) in baseline and count > baseline[str(n)]:
                    # Bought before a restart or a stop (the farm has more of it than before
                    # this step first ran): never bought twice.
                    log.info("job %d step %d buy: already bought (%d > %d)", job.number, n, count, baseline[str(n)])
                    states.append("buy:ALREADY_DONE")
                    work["done"] = n
                    self._save_goal()
                    continue
                baseline.setdefault(str(n), count)
                self._save_goal()
            sub = Job(number=job.number, owner=f"{job.owner}-s{n}", spoken=job.spoken)
            step = with_context(step, work.setdefault("context", {}))
            if command == "haul_tractor":
                result = self._haul(sub, cancel, entered, {"tractor_name": step.get("tractor_name"),
                                                           "target": step.get("target", "here")})
            elif command == "load_tractor" and step.get("carry"):
                result = self._load_with(sub, cancel, entered, step)
            elif command == "field_work":
                result = self._field_work(sub, cancel, entered, {
                    "field": step["field"], "operations": step["operations"], "area_m2": step.get("area_m2"),
                    "over_crop": step.get("over_crop", False), "crop": step.get("crop")})
            elif command == "buy":
                result = self._buy(sub, cancel, entered, {"wants": step["wants"]})
            else:
                target = step.get("target", "here")
                intent = {"version": 1, "request_id": sub.owner, "task": JOBS[command][0], "farm_id": self.farm(),
                          "target": "player_at_acceptance" if target == "here" else target}
                if step.get("implement") is not None:
                    intent["implement"] = step["implement"]
                intent.update({k: step[k] for k in NAMES if k in step})
                log.info("job %d step %d intent %s", job.number, n, json.dumps(intent))
                self.running = intent["request_id"]
                result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
            states += [f"{command}:{s}" for s in result.states]
            used += [i for i in result.equipment_ids if i not in used]
            log.info("job %d step %d %s result %s %s", job.number, n, command, result.status, result.reason)
            if result.status != "COMPLETE":
                return Result(result.status, result.reason, tuple(used), tuple(states))
            remember(work["context"], step, result, tuple(self.world.snapshot()))
            work["done"] = n
            self._save_goal()
        return Result("COMPLETE", "SEQUENCE_DONE", tuple(used), tuple(states))

    MAX_LOAD_WITH_STEPS = 6

    def _load_with(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """"Load the tractor and plow on the gooseneck", planned from observed state before every
        step: a tractor on the deck without the implement is unloaded, the implement hitched to
        it, then the two loaded together (AGENTS.md exception 5, a mounted implement along)."""
        states, used = [], []
        for n in range(1, self.MAX_LOAD_WITH_STEPS + 1):
            if cancel.is_set():
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", tuple(used), tuple(states))
            self._settle()
            step, failure = load_with_step(tuple(self.world.snapshot()), self.farm(), work)
            if step is None:
                if failure == "LOADED":
                    return Result("COMPLETE", "LOADED_AND_SECURED", tuple(used), tuple(states))
                return Result("BLOCKED", failure, tuple(used), tuple(states))
            intent = {"version": 1, "request_id": f"{job.owner}-w{n}", "farm_id": self.farm(),
                      "target": "player_at_acceptance", **step}
            log.info("job %d load-with step %d intent %s", job.number, n, json.dumps(intent))
            self.running = intent["request_id"]
            result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
            states += [f"{step['task']}:{s}" for s in result.states]
            used += [i for i in result.equipment_ids if i not in used]
            if result.status != "COMPLETE":
                return Result(result.status, result.reason, tuple(used), tuple(states))
            if step["task"] == "load_tractor":
                return Result("COMPLETE", "LOADED_AND_SECURED", tuple(used), tuple(states))
        return Result("FAILED", "LOAD_WITH_PLAN_TOO_LONG", tuple(used), tuple(states))

    MAX_HAUL_STEPS = 8

    def _haul(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """Take a tractor somewhere on a transport trailer and unload it there, planned from
        the observed state before every step (BUILD_SPEC completion plan, requirement 1):
        hitch a transport to a truck if none is hitched, load (an executor load continues
        whatever is already done), deliver the rig carrying the tractor, unload. Unloading
        only after this goal's delivery step completed (AGENTS.md: never because the rig
        stopped). The first step not complete ends it with that step's result."""
        name, target = work.get("tractor_name"), work["target"]
        states, used = [], []
        work.setdefault("delivered", False)       # kept with the goal: a resume never unloads early
        for n in range(1, self.MAX_HAUL_STEPS + 1):
            if cancel.is_set():
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", tuple(used), tuple(states))
            self._settle()
            step, failure = haul_step(tuple(self.world.snapshot()), self.farm(), name, target, work["delivered"])
            if step is None:
                if failure == "HAULED":
                    return Result("COMPLETE", "HAULED_AND_UNLOADED", tuple(used), tuple(states))
                return Result("BLOCKED", failure, tuple(used), tuple(states))
            intent = {"version": 1, "request_id": f"{job.owner}-h{n}", "farm_id": self.farm(), **step}
            log.info("job %d haul step %d intent %s", job.number, n, json.dumps(intent))
            self.running = intent["request_id"]
            result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
            states += [f"{step['task']}:{s}" for s in result.states]
            used += [i for i in result.equipment_ids if i not in used]
            log.info("job %d haul step %d %s result %s %s", job.number, n, step["task"], result.status, result.reason)
            if result.status != "COMPLETE":
                return Result(result.status, result.reason, tuple(used), tuple(states))
            if step["task"] == "move_pair":
                work["delivered"] = True          # observed arrival (DELIVERY_OBSERVED): unloading may follow
            elif step["task"] == "unload_tractor":
                return Result("COMPLETE", "HAULED_AND_UNLOADED", tuple(used), tuple(states))
        return Result("FAILED", "HAUL_PLAN_TOO_LONG", tuple(used), tuple(states))

    def _buy(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """Each want in turn, one executor purchase each; the first not COMPLETE ends it."""
        states, bought = [], []
        wants = [dict(w) for w in work["wants"]]
        # A truck bought with a gooseneck trailer (a deckover is one) gets a gooseneck hitch (live: the GMC's
        # default is "No Attachments").
        if any(w["kind"] == "trailer" and any(k in (w.get("name") or "") for k in ("gooseneck", "deckover"))
               for w in wants):
            for w in wants:
                if w["kind"] == "truck":
                    w.setdefault("hitch", "gooseneck")
        for n, want in enumerate(wants, 1):
            intent = {"version": 1, "request_id": f"{job.owner}-{n}", "farm_id": self.farm(),
                      "target": "player_at_acceptance", "task": "buy", "want": want}
            log.info("job %d buy %d intent %s", job.number, n, json.dumps(intent))
            self.running = intent["request_id"]
            result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
            states += [f"buy:{s}" for s in result.states]
            bought += list(result.equipment_ids)
            log.info("job %d buy %d result %s %s %s", job.number, n, result.status, result.reason, result.equipment_ids)
            if result.status != "COMPLETE":
                return Result(result.status, result.reason, tuple(bought), tuple(states))
        return Result("COMPLETE", "BOUGHT", tuple(bought), tuple(states))

    SETTLE_S = 30.0

    def _settle(self) -> None:
        """Before each planned step, let the farm's vehicles come to rest (live
        5454f21977df465b-2: the tractor still rolled 1.5 s after the helper stopped, so
        the next step found no pair at rest)."""
        deadline = time.monotonic() + self.SETTLE_S
        while time.monotonic() < deadline:
            if all(i.stopped for i in self.world.snapshot() if i.category == "tractor"):
                return
            time.sleep(1.0)

    def _field_work(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """Each operation in turn on the same field (resolved once): the planner's next
        steps from fresh state, each a full executor job; the first one that is not
        COMPLETE ends the plan with its result."""
        field, failure = resolve_field(work["field"], self.world.fields())
        if field is None:
            return Result("BLOCKED", failure)
        log.info("job %d field work %s on field %s (%.2f ha)", job.number, work["operations"], field.id, field.area_ha)
        states, equipment, n = [], [], 0
        for operation in work["operations"]:
            field = next((f for f in self.world.fields() if f.id == field.id), field)     # fresh state
            if already_done(OPERATIONS[operation], field, work.get("area_m2")):
                log.info("job %d %s: field %s already done; skipped", job.number, operation, field.id)
                states.append(f"{operation}:ALREADY_DONE")
                continue
            crop = None if work.get("over_crop") else crop_problem(OPERATIONS[operation], field)
            if crop is not None:
                # Before anything is hitched or moved.
                log.info("job %d %s: field %s %s", job.number, operation, field.id, crop)
                return Result("BLOCKED", crop, tuple(equipment), tuple(states))
            steps, note = field_steps(tuple(self.world.snapshot()), operation, field.id, self.farm(),
                                      work.get("area_m2"), work.get("crop"))
            if steps is None:
                return Result("BLOCKED", note, tuple(equipment), tuple(states))
            if note is not None:
                log.info("job %d %s: %s", job.number, operation, note)
            if work.get("over_crop"):
                for step in steps:
                    if step["task"] == "field_work":
                        step["over_crop"] = True
            for step in steps:
                n += 1
                self._settle()
                intent = {"version": 1, "request_id": f"{job.owner}-{n}", "farm_id": self.farm(),
                          "target": "player_at_acceptance", **step}
                log.info("job %d step %d intent %s", job.number, n, json.dumps(intent))
                self.running = intent["request_id"]
                result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
                states += [f"{step['task']}:{s}" for s in result.states]
                equipment += [e for e in result.equipment_ids if e not in equipment]
                log.info("job %d step %d result %s %s", job.number, n, result.status, result.reason)
                if result.status != "COMPLETE":
                    return Result(result.status, result.reason, tuple(equipment), tuple(states))
        return Result("COMPLETE", "FIELD_WORK_OBSERVED", tuple(equipment), tuple(states))

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the running job (tests and shutdown)."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
