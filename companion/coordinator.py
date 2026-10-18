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

from jsonschema import Draft202012Validator

from adapters.world import StepFailed, StepUnverified

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
MILESTONES = frozenset({"TRAVEL_NEAR", "STAGE", "LOCAL_MANEUVER", "DELIVER"})
# The executor task and the player-facing notices of each job command.
JOBS = {"fetch_pair": ("deliver_tractor_and_forage_wagon", "ACCEPTED", "COMPLETE"),
        "move_pair": ("move_pair", "ACCEPTED_MOVE", "COMPLETE_MOVE"),
        "hitch_pair": ("hitch_pair", "ACCEPTED_HITCH", "COMPLETE_HITCH")}


def result_notice(status: str, reason: str, complete: str = "COMPLETE") -> str:
    if status == "COMPLETE":
        return complete
    if status == "CANCELLED":
        return status
    return token(f"{status}_{reason}")


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
            return self.start(command, proposal.get("target", "here"), spoken, proposal.get("implement"))
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
    def start(self, command: str, target, spoken: bool = False, implement: str | None = None) -> Job | None:
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
        self._worker = threading.Thread(target=self._run, args=(job, cancel, command, target, implement), daemon=True,
                                        name=f"farmhand-job-{job.number}")
        self._worker.start()
        return job

    def _run(self, job: Job, cancel: threading.Event, command: str, target, implement: str | None = None) -> None:
        task, _, complete = JOBS[command]
        try:
            for owner in self.world.retained():   # an earlier job's unconfirmed stop
                self.world.recover(owner)
            intent = {"version": 1, "request_id": job.owner, "task": task,
                      "farm_id": self.farm(), "target": "player_at_acceptance" if target == "here" else target}
            if implement is not None:
                intent["implement"] = implement
            log.info("job %d intent %s", job.number, json.dumps(intent))

            def entered(state):
                job.step = state
                if state in MILESTONES:
                    self.notify(f"WORKING_{state}", job.spoken)

            self.world.cancel = cancel           # the link refuses steps once it is set
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

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the running job (tests and shutdown)."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
