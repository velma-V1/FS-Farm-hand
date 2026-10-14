"""One deterministic task. No LLM calls, game physics, transport, or simulator import."""
import copy
import json
import math
from pathlib import Path
from threading import Lock

from jsonschema import Draft202012Validator

from adapters.world import World
from protocol.model import Destination, Equipment, Position, Result

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "protocol/intent.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)

# Provisional contract values, to be validated against real equipment in FS25.
# AutoDrive only routes to map markers (adapters/EVIDENCE.md), so "here" is the
# nearest routable marker within this radius of the requester's captured position.
DESTINATION_MAX_FROM_REQUESTER_M = 50.0
TRACTOR_ARRIVAL_TOLERANCE_M = 3.0
WAGON_ARRIVAL_TOLERANCE_M = 15.0


class ObservationMismatch(Exception):
    pass


def valid_position(position: Position) -> bool:
    return (isinstance(position, (tuple, list)) and len(position) == 2
            and all(type(v) in (float, int) and math.isfinite(v) for v in position))


def usable(item: Equipment, farm_id: str, owner: str | None = None) -> bool:
    return (item.farm_id == farm_id and item.available and not item.busy
            and not item.occupied and item.reserved_by == owner
            and valid_position(item.position))


def select_destination(destinations, requester: Position) -> Destination | None:
    """Nearest valid destination within range; ties break on the opaque ID."""
    candidates = [d for d in destinations
                  if isinstance(d, Destination) and isinstance(d.id, str) and d.id
                  and valid_position(d.position)
                  and math.dist(d.position, requester) <= DESTINATION_MAX_FROM_REQUESTER_M]
    return min(candidates, key=lambda d: (math.dist(d.position, requester), d.id), default=None)


def _normalized_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _unique(matches, reason_missing: str):
    """Exactly one match with a valid position, or an explicit failure reason."""
    if not matches:
        return None, reason_missing
    if len(matches) > 1:
        return None, "DESTINATION_AMBIGUOUS"
    if not valid_position(matches[0].position):
        return None, "DESTINATION_INVALID"
    return matches[0], None


def resolve_explicit_destination(target: dict, destinations, map_id: str | None,
                                 field_entrances) -> tuple[Destination | None, str | None]:
    """Resolve a schema-valid explicit target without guessing or substitution.

    Markers match by exact ID, or by name after whitespace/case normalization only.
    A field resolves solely through the trusted local field_entrances[map_id][field]
    marker name: AutoDrive has no verified field-to-marker association.
    """
    destinations = [d for d in destinations if isinstance(d, Destination)]
    if target["type"] == "autodrive_marker":
        if "marker_id" in target:
            return _unique([d for d in destinations if d.id == target["marker_id"]],
                           "DESTINATION_NOT_FOUND")
        wanted = _normalized_name(target["marker_name"])
    else:
        if not isinstance(map_id, str) or not map_id:
            return None, "MAP_UNKNOWN"
        marker_name = field_entrances.get(map_id, {}).get(target["field"])
        if marker_name is None:
            return None, "FIELD_NOT_MAPPED"
        wanted = _normalized_name(marker_name)
    return _unique([d for d in destinations
                    if isinstance(d.name, str) and d.name and _normalized_name(d.name) == wanted],
                   "DESTINATION_NOT_FOUND")


def validated_field_entrances(config) -> dict[str, dict[int, str]]:
    """Trusted local configuration: {map_id: {field_number: marker_name}}."""
    result = {}
    for map_id, fields in (config or {}).items():
        if not isinstance(map_id, str) or not map_id or not isinstance(fields, dict):
            raise ValueError("Invalid field entrance map")
        result[map_id] = {}
        for field, marker_name in fields.items():
            if (type(field) is not int or field < 1
                    or not isinstance(marker_name, str) or not marker_name.strip()):
                raise ValueError("Invalid field entrance")
            result[map_id][field] = marker_name
    return result


def compatible(tractor: Equipment, wagon: Equipment) -> bool:
    return (tractor.category == "tractor" and wagon.category == "forage_wagon"
            and bool(tractor.hitches & wagon.hitches)
            and math.isfinite(tractor.power_kw) and math.isfinite(wagon.required_power_kw)
            and tractor.power_kw >= wagon.required_power_kw > 0
            and {"move", "attach", "stop"} <= tractor.capabilities
            and (not wagon.requires_pto or "pto" in tractor.capabilities))


class Executor:
    """Serialized in-process execution. Replay cache is NOT crash-durable."""

    def __init__(self, world: World, field_entrances=None):
        self.world = world
        self.field_entrances = validated_field_entrances(field_entrances)
        self._lock = Lock()
        self._results: dict[str, tuple[dict, Result]] = {}

    def execute(self, intent: object) -> Result:
        if not VALIDATOR.is_valid(intent):
            return Result("BLOCKED", "INVALID_INTENT")
        # Detach caller-owned input, including the nested target object.
        command = copy.deepcopy(intent)
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
                or math.dist(tractor.position, target) > TRACTOR_ARRIVAL_TOLERANCE_M
                or math.dist(wagon.position, target) > WAGON_ARRIVAL_TOLERANCE_M):
            raise ObservationMismatch("DELIVERY_NOT_OBSERVED")

    def _run(self, command):
        owner, farm_id = command["request_id"], command["farm_id"]
        states, ids = [], ()
        reserved = physical_attempted = False
        status, reason = "BLOCKED", "NO_ELIGIBLE_PAIR"
        try:
            # Every destination is resolved and validated before any reservation.
            if command["target"] == "player_at_acceptance":
                requester = self.world.player_position(farm_id)
                if not valid_position(requester):
                    return Result("BLOCKED", "INVALID_REQUESTER_POSITION")
                states.append("FIND")
                destination = select_destination(self.world.destinations(), tuple(requester))
                if destination is None:
                    return Result("BLOCKED", "NO_DESTINATION_NEAR_REQUESTER", states=tuple(states))
            else:
                states.append("FIND")
                map_id = (self.world.map_id() if command["target"]["type"] == "field_entrance"
                          else None)
                destination, failure = resolve_explicit_destination(
                    command["target"], self.world.destinations(), map_id, self.field_entrances)
                if destination is None:
                    return Result("BLOCKED", failure, states=tuple(states))
            target = tuple(destination.position)
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
            self.world.approach(*ids, owner)
            self._pair(ids, farm_id, owner, attached=False)
            states.append("ATTACH")
            self.world.attach(*ids, owner)
            states.append("VERIFY")
            self._pair(ids, farm_id, owner, attached=True)
            states.append("DELIVER")
            self.world.deliver(tractor.id, destination.id, owner)
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
