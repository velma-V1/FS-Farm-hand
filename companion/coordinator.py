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
from pathlib import Path
import re
import threading
import time

from jsonschema import Draft202012Validator

from adapters.world import StepFailed, StepUnverified
from companion.executor import OPERATIONS, already_done, of_kind, resolve_field, usable
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
MILESTONES = frozenset({"FIELD_WORK", "PULL_CLEAR", "TRAVEL_NEAR", "STAGE", "LOCAL_MANEUVER", "DELIVER"})
# The executor task and the player-facing notices of each job command.
JOBS = {"fetch_pair": ("deliver_tractor_and_forage_wagon", "ACCEPTED", "COMPLETE"),
        "move_pair": ("move_pair", "ACCEPTED_MOVE", "COMPLETE_MOVE"),
        "hitch_pair": ("hitch_pair", "ACCEPTED_HITCH", "COMPLETE_HITCH"),
        "detach_pair": ("detach_pair", "ACCEPTED_DETACH", "COMPLETE_DETACH"),
        "field_work": ("field_work", "ACCEPTED_FIELD_WORK", "COMPLETE_FIELD_WORK"),
        "buy": ("buy", "ACCEPTED_BUY", "COMPLETE_BUY"),
        "save_game": ("save_game", "ACCEPTED_SAVE", "COMPLETE_SAVE")}


def result_notice(status: str, reason: str, complete: str = "COMPLETE") -> str:
    if status == "COMPLETE":
        # Unhitched, but the tractor could not pull clear (nothing moved): say so.
        return complete + "_NOT_CLEAR" if reason == "DETACHED_NOT_CLEAR" else complete
    if status == "CANCELLED":
        return status
    return token(f"{status}_{reason}")


# ------------------------------------------------------------- field planning
# A field-work goal as executor steps, one operation at a time, from fresh state. The
# planner decides only what happens next; the executor selects, validates and
# verifies every step. For each operation: hitch an implement that does it (unless one
# is already hitched to a tractor), work the field, then unhitch it where it stands
# (PULL_CLEAR included) so the next operation's tool change has a tractor.
def implement_kind(items: tuple[Equipment, ...], operation: str, farm_id: str) -> tuple[str | None, str | None]:
    """The implement kind to use for ``operation``: the first of its kinds the farm
    has (owned, available); a cultivator stands in for a plow only when the farm has
    no plow (said in the note). None and why when the farm has none."""
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


def _hitched(items, kind) -> bool:
    observed = {i.id: i for i in items}
    return any(i.category == "tractor" and observed.get(i.attached_to) is not None
               and of_kind(observed[i.attached_to], kind) for i in items)


def field_steps(items: tuple[Equipment, ...], operation: str, field, farm_id: str, area_m2=None):
    """The steps for one operation on ``field`` (an id or "smallest"), as executor
    intent fragments, or (None, why)."""
    kind, note = implement_kind(items, operation, farm_id)
    if kind is None:
        return None, note
    work_op = "cultivate" if note == "NO_PLOW_CULTIVATING" else operation
    steps = []
    if not _hitched(items, kind):
        steps.append({"task": "hitch_pair", "implement": kind})
    steps.append({"task": "field_work", "field": field, "operation": work_op, "implement": kind,
                  **({"area_m2": area_m2} if area_m2 else {})})
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


class Coordinator:
    """``world`` is the FS25World; ``executor`` an Executor over it; ``notify(code,
    spoken)`` shows a token in game (and speaks it when the request was spoken); ``farm`` returns the linked farm id, or raises
    StepUnverified when the game is not linked to this companion."""

    def __init__(self, world, executor, notify, farm, session):
        self.world, self.executor, self.notify = world, executor, notify
        self.farm, self.session = farm, session
        self._lock = threading.Lock()
        self._jobs = 0
        self.current: Job | None = None
        self.last: Job | None = None
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None

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
        else:
            work = ({"field": proposal["field"], "operations": proposal["operations"],
                     "area_m2": proposal.get("area_m2")} if command == "field_work"
                    else {"wants": proposal["wants"]} if command == "buy" else None)
            if command in ("fetch_pair", "hitch_pair"):
                names = {k: proposal[k] for k in ("tractor_name", "implement_name") if k in proposal}
                work = names or None
            return self.start(command, proposal.get("target", "here"), spoken, proposal.get("implement"), work)
        return None

    # ------------------------------------------------------------------ status
    def status(self) -> str:
        with self._lock:
            job, last = self.current, self.last
        if job is not None:
            return token(f"STATUS_WORKING_{job.step or 'FIND'}")
        if last is not None:
            return token(f"STATUS_LAST_{last.state}")
        return "STATUS_IDLE"

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
            threading.Thread(target=self._stop_reserved, args=(job.owner,), daemon=True,
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
                self.current = Job(self._jobs, f"{self.session()}-{self._jobs}", spoken=spoken)
                job, cancel = self.current, self._cancel
        if busy:
            self.notify("BUSY", spoken)
            return None
        self.notify(JOBS[command][1], spoken)
        self._worker = threading.Thread(target=self._run, args=(job, cancel, command, target, implement, work),
                                        daemon=True, name=f"farmhand-job-{job.number}")
        self._worker.start()
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
            else:
                intent = {"version": 1, "request_id": job.owner, "task": task,
                          "farm_id": self.farm(), "target": "player_at_acceptance" if target == "here" else target}
                if implement is not None:
                    intent["implement"] = implement
                intent.update(work or {})            # tractor_name / implement_name
                log.info("job %d intent %s", job.number, json.dumps(intent))
                result = self.executor.execute(intent, cancel=cancel, on_state=entered)
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
            with self._lock:
                self.current, self.last = None, job
            self.notify(result_notice(job.state, job.reason or "", complete), job.spoken)

    def _buy(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """Each want in turn, one executor purchase each; the first not COMPLETE ends it."""
        states, bought = [], []
        wants = [dict(w) for w in work["wants"]]
        # A truck bought with a gooseneck trailer gets a gooseneck hitch (live: the GMC's
        # default is "No Attachments").
        if any(w["kind"] == "trailer" and "gooseneck" in (w.get("name") or "") for w in wants):
            for w in wants:
                if w["kind"] == "truck":
                    w.setdefault("hitch", "gooseneck")
        for n, want in enumerate(wants, 1):
            intent = {"version": 1, "request_id": f"{job.owner}-{n}", "farm_id": self.farm(),
                      "target": "player_at_acceptance", "task": "buy", "want": want}
            log.info("job %d buy %d intent %s", job.number, n, json.dumps(intent))
            result = self.executor.execute(intent, cancel=cancel, on_state=entered)
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
            steps, note = field_steps(tuple(self.world.snapshot()), operation, field.id, self.farm(),
                                      work.get("area_m2"))
            if steps is None:
                return Result("BLOCKED", note, tuple(equipment), tuple(states))
            if note is not None:
                log.info("job %d %s: %s", job.number, operation, note)
            for step in steps:
                n += 1
                self._settle()
                intent = {"version": 1, "request_id": f"{job.owner}-{n}", "farm_id": self.farm(),
                          "target": "player_at_acceptance", **step}
                log.info("job %d step %d intent %s", job.number, n, json.dumps(intent))
                result = self.executor.execute(intent, cancel=cancel, on_state=entered)
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
