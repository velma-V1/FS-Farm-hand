"""One deterministic task. No LLM calls, game physics, transport, or simulator import."""
import json
import math
from pathlib import Path
from threading import Lock

from jsonschema import Draft202012Validator

from adapters.world import World
from protocol.model import Equipment, Position, Result

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "protocol/intent.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


class ObservationMismatch(Exception):
    pass


def valid_position(position: Position) -> bool:
    return (isinstance(position, (tuple, list)) and len(position) == 2
            and all(type(v) in (float, int) and math.isfinite(v) for v in position))


def usable(item: Equipment, farm_id: str, owner: str | None = None) -> bool:
    return (item.farm_id == farm_id and item.available and not item.busy
            and not item.occupied and item.reserved_by == owner
            and valid_position(item.position))


def compatible(tractor: Equipment, wagon: Equipment) -> bool:
    return (tractor.category == "tractor" and wagon.category == "forage_wagon"
            and bool(tractor.hitch) and tractor.hitch == wagon.hitch
            and math.isfinite(tractor.power_kw) and math.isfinite(wagon.required_power_kw)
            and tractor.power_kw >= wagon.required_power_kw > 0
            and {"move", "attach", "stop"} <= tractor.capabilities
            and (not wagon.requires_pto or "pto" in tractor.capabilities))


class Executor:
    """Serialized in-process execution. Replay cache is NOT crash-durable."""

    def __init__(self, world: World):
        self.world = world
        self._lock = Lock()
        self._results: dict[str, tuple[dict, Result]] = {}

    def execute(self, intent: object) -> Result:
        if not VALIDATOR.is_valid(intent):
            return Result("BLOCKED", "INVALID_INTENT")
        # Only scalar fields are allowed by the schema; detach caller-owned input.
        command = dict(intent)
        with self._lock:
            request_id = command["request_id"]
            if request_id in self._results:
                old, result = self._results[request_id]
                return result if old == command else Result("BLOCKED", "REQUEST_ID_CONFLICT")
            result = self._run(command)
            self._results[request_id] = (command, result)
            return result

    def _pair(self, ids, farm_id, owner, attached):
        observed = {item.id: item for item in self.world.snapshot()}
        if any(identifier not in observed for identifier in ids):
            raise ObservationMismatch("EQUIPMENT_MISSING")
        tractor, wagon = (observed[identifier] for identifier in ids)
        if not all(usable(item, farm_id, owner) for item in (tractor, wagon)):
            raise ObservationMismatch("EQUIPMENT_CHANGED")
        if not compatible(tractor, wagon):
            raise ObservationMismatch("COMPATIBILITY_CHANGED")
        expected = (wagon.id, tractor.id) if attached else (None, None)
        if (tractor.attached_to, wagon.attached_to) != expected:
            raise ObservationMismatch("ATTACHMENT_MISMATCH")
        return tractor, wagon

    def _arrival(self, ids, farm_id, owner, target):
        tractor, wagon = self._pair(ids, farm_id, owner, attached=True)
        if (not tractor.stopped or not wagon.stopped
                or math.dist(tractor.position, target) > 3
                or math.dist(wagon.position, target) > 15):
            raise ObservationMismatch("DELIVERY_NOT_OBSERVED")

    def _run(self, command):
        owner, farm_id = command["request_id"], command["farm_id"]
        states, ids = [], ()
        reserved = physical_attempted = False
        status, reason = "BLOCKED", "NO_ELIGIBLE_PAIR"
        try:
            target = self.world.player_position(farm_id)
            if not valid_position(target):
                return Result("BLOCKED", "INVALID_REQUESTER_POSITION")
            target = tuple(target)
            states.append("FIND")
            items = sorted(self.world.snapshot(), key=lambda item: item.id)
            free = [item for item in items if usable(item, farm_id)
                    and item.attached_to is None and item.stopped]
            pair = next(((tractor, wagon) for tractor in free for wagon in free
                         if compatible(tractor, wagon)), None)
            if pair is None:
                return Result(status, reason, states=tuple(states))
            ids = tuple(item.id for item in pair)
            states.append("RESERVE")
            if not self.world.reserve(ids, owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", ids, tuple(states))
            reserved = True
            tractor, wagon = self._pair(ids, farm_id, owner, attached=False)
            states.append("MOVE")
            physical_attempted = True
            self.world.move(tractor.id, wagon.position, owner)
            self._pair(ids, farm_id, owner, attached=False)
            states.append("ATTACH")
            self.world.attach(*ids, owner)
            states.append("VERIFY")
            self._pair(ids, farm_id, owner, attached=True)
            states.append("DELIVER")
            self.world.move(tractor.id, target, owner)
            self._arrival(ids, farm_id, owner, target)
            status, reason = "COMPLETE", "DELIVERY_OBSERVED"
        except ObservationMismatch as error:
            status, reason = "UNVERIFIED", str(error)
        except (RuntimeError, TimeoutError, ValueError, KeyError) as error:
            status, reason = "FAILED", type(error).__name__
        except Exception as error:
            # An unexpected adapter failure must not fabricate completion.
            status, reason = "UNVERIFIED", type(error).__name__
        finally:
            if reserved:
                try:
                    if physical_attempted:
                        if not self.world.stop(ids[0], owner):
                            raise ObservationMismatch("STOP_NOT_CONFIRMED")
                        stopped = {item.id: item for item in self.world.snapshot()}
                        if any(identifier not in stopped or not stopped[identifier].stopped
                               or stopped[identifier].reserved_by != owner for identifier in ids):
                            raise ObservationMismatch("STOP_NOT_OBSERVED")
                    if status == "COMPLETE":
                        self._arrival(ids, farm_id, owner, target)
                    self.world.release(owner)
                    if any(item.reserved_by == owner for item in self.world.snapshot()):
                        raise ObservationMismatch("RELEASE_NOT_OBSERVED")
                except Exception:
                    status, reason = "UNVERIFIED", "CLEANUP_NOT_CONFIRMED"
        return Result(status, reason, ids, tuple(states))
