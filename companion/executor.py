"""One deterministic task. No LLM calls, game physics, transport, or simulator import."""
import copy
import json
import logging
import math
from pathlib import Path
from threading import Lock

from jsonschema import Draft202012Validator

from adapters.world import StepUnverified, World
from companion.hitch import plan_hitch
from protocol.model import Destination, Equipment, HitchPlan, Position, Result

log = logging.getLogger("farmhand")
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "protocol/intent.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)

# Provisional contract values, to be validated against real equipment in FS25.
# AutoDrive only routes to map markers (adapters/EVIDENCE.md), so "here" is the
# nearest routable marker within this radius of the requester's captured position.
DESTINATION_MAX_FROM_REQUESTER_M = 50.0
TRACTOR_ARRIVAL_TOLERANCE_M = 3.0
WAGON_ARRIVAL_TOLERANCE_M = 15.0

# HITCH observation checks (provisional; tune at the first FS25 test). These are
# coarse cross-checks of each step's outcome, not GIANTS' attach rules: GIANTS'
# attachableInfo stays the authority on attachability (adapters/EVIDENCE.md).
NEAR_RADIUS_M = 60.0                      # beyond this, AutoDrive travels first
STAGE_POSITION_TOLERANCE_M = 1.0
STAGE_YAW_TOLERANCE_RAD = math.radians(10)
WAGON_MOVED_TOLERANCE_M = 0.25
WAGON_YAW_TOLERANCE_RAD = math.radians(5)
# ALIGN_REVERSE may stop as soon as GIANTS names the exact pair within the
# Farmhand attach-assist envelope, so this must equal its cap
# (mod/FarmhandHitch.lua Hitch.LIMITS.maxAssistRadiusM; checked by a test).
ALIGN_PROGRESS_TOLERANCE_M = 1.5


class ObservationMismatch(Exception):
    pass


def valid_position(position: Position) -> bool:
    return (isinstance(position, (tuple, list)) and len(position) == 2
            and all(type(v) in (float, int) and math.isfinite(v) for v in position))


def usable(item: Equipment, farm_id: str, owner: str | None = None) -> bool:
    return (item.farm_id == farm_id and item.available and not item.busy
            and not item.occupied and item.reserved_by == owner
            and valid_position(item.position))


def select_destination(destinations, requester: Position,
                       radius: float = DESTINATION_MAX_FROM_REQUESTER_M) -> Destination | None:
    """Nearest valid destination within ``radius``; ties break on the opaque ID."""
    candidates = [d for d in destinations
                  if isinstance(d, Destination) and isinstance(d.id, str) and d.id
                  and valid_position(d.position)
                  and math.dist(d.position, requester) <= radius]
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


def _known(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def power_sufficient(tractor: Equipment, wagon: Equipment) -> bool:
    """GIANTS' turn-on rule (PowerConsumer:getCanBeTurnedOn): the wagon's PTO torque
    must not exceed the tractor's capacity. No requirement needs no capacity."""
    need, capacity = wagon.required_pto_torque, tractor.pto_torque_capacity
    if not _known(need):
        return False
    return need == 0 or (_known(capacity) and capacity >= need)


def compatible(tractor: Equipment, wagon: Equipment) -> bool:
    """Pair-level checks. Joint type and PTO are checked per planned joint pair in
    plan_hitch, which every HITCH step re-runs."""
    return (tractor.category == "tractor" and wagon.category == "forage_wagon"
            and power_sufficient(tractor, wagon)
            and {"move", "attach", "stop"} <= tractor.capabilities)


def _angle(a: float, b: float) -> float:
    """Absolute difference of two yaws, wrapped to [0, pi]."""
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


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

    def _hitch_state(self, ids, farm_id, owner, plan: HitchPlan):
        """VALIDATE: fresh, reserved, unattached, stopped pair whose wagon has not
        moved and whose planned joints are still the ones a fresh plan selects."""
        tractor, wagon = self._pair(ids, farm_id, owner, attached=False)
        if not tractor.stopped or not wagon.stopped:
            raise ObservationMismatch("EQUIPMENT_MOVING")
        fresh = plan_hitch(tractor, wagon)
        if fresh is None or (fresh.tractor_joint, fresh.wagon_joint) != (plan.tractor_joint, plan.wagon_joint):
            raise ObservationMismatch("HITCH_GEOMETRY_CHANGED")
        if (math.dist(fresh.target, plan.target) > WAGON_MOVED_TOLERANCE_M
                or _angle(fresh.final.yaw, plan.final.yaw) > WAGON_YAW_TOLERANCE_RAD):
            raise ObservationMismatch("WAGON_MOVED")
        return tractor, wagon

    def _choose(self, free, destinations):
        """Deterministic first pair (by opaque ID) that is compatible, has a hitch
        plan, and can reach its staging pose; all decided before reservation."""
        reason = "NO_ELIGIBLE_PAIR"
        for tractor in free:
            for wagon in free:
                if not compatible(tractor, wagon):
                    continue
                plan = plan_hitch(tractor, wagon)
                if plan is None:
                    reason = "NO_HITCH_GEOMETRY" if reason == "NO_ELIGIBLE_PAIR" else reason
                    continue
                staging = (plan.staging.x, plan.staging.z)
                travel = None
                if math.dist(tractor.position, staging) > NEAR_RADIUS_M:
                    # Long-range travel is AutoDrive's job; no marker, no attempt.
                    travel = select_destination(destinations, staging, NEAR_RADIUS_M)
                    if travel is None:
                        reason = "NO_DESTINATION_NEAR_WAGON"
                        continue
                return (tractor, wagon), plan, travel, None
        return None, None, None, reason

    def _arrival(self, ids, farm_id, owner, target):
        tractor, wagon = self._pair(ids, farm_id, owner, attached=True)
        if (not tractor.stopped or not wagon.stopped
                or math.dist(tractor.position, target) > TRACTOR_ARRIVAL_TOLERANCE_M
                or math.dist(wagon.position, target) > WAGON_ARRIVAL_TOLERANCE_M):
            log.info("arrival not observed: tractor %s wagon %s target %s stopped %s/%s",
                     tractor.position, wagon.position, target, tractor.stopped, wagon.stopped)
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
            log.info("destination %s %r at %s", destination.id, destination.name, target)
            items = sorted(self.world.snapshot(), key=lambda item: item.id)
            free = [item for item in items if usable(item, farm_id)
                    and item.attached_to is None and item.stopped]
            pair, plan, travel, failure = self._choose(free, self.world.destinations())
            if pair is None:
                return Result(status, failure, states=tuple(states))
            ids = tuple(item.id for item in pair)
            log.info("selected tractor %s wagon %s joints %d/%d staging %s travel %s", *ids,
                     plan.tractor_joint, plan.wagon_joint, plan.staging, travel and travel.id)
            states.append("RESERVE")
            if not self.world.reserve(ids, owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", ids, tuple(states))
            reserved = True
            # HITCH: VALIDATE -> [TRAVEL_NEAR] -> STAGE -> ALIGN_REVERSE -> ATTACH -> VERIFY.
            # Every step is followed by a fresh-snapshot check; returns are not evidence.
            tractor, wagon = self._hitch_state(ids, farm_id, owner, plan)
            physical_attempted = True
            if travel is not None:
                states.append("TRAVEL_NEAR")
                self.world.travel(tractor.id, travel.id, owner)
                tractor, _ = self._hitch_state(ids, farm_id, owner, plan)
                if math.dist(tractor.position, travel.position) > TRACTOR_ARRIVAL_TOLERANCE_M:
                    raise ObservationMismatch("TRAVEL_NOT_OBSERVED")
            states.append("STAGE")
            self.world.stage(tractor.id, wagon.id, plan.staging, owner)
            tractor, _ = self._hitch_state(ids, farm_id, owner, plan)
            root = tractor.ai_root
            if (root is None
                    or math.dist((root.x, root.z), (plan.staging.x, plan.staging.z)) > STAGE_POSITION_TOLERANCE_M
                    or _angle(root.yaw, plan.staging.yaw) > STAGE_YAW_TOLERANCE_RAD):
                raise ObservationMismatch("STAGE_NOT_OBSERVED")
            states.append("ALIGN_REVERSE")
            self.world.align(tractor.id, wagon.id, plan, owner)
            tractor, _ = self._hitch_state(ids, farm_id, owner, plan)
            joint = next(j for j in tractor.output_joints if j.index == plan.tractor_joint)
            if (tractor.ai_root is None
                    or math.dist(tractor.ai_root.to_world(joint.x, joint.z), plan.target)
                    > ALIGN_PROGRESS_TOLERANCE_M):
                raise ObservationMismatch("ALIGN_NOT_OBSERVED")
            states.append("ATTACH")
            self.world.attach(tractor.id, wagon.id, (plan.tractor_joint, plan.wagon_joint), owner)
            states.append("VERIFY")
            tractor, _ = self._pair(ids, farm_id, owner, attached=True)
            if not tractor.stopped:
                raise ObservationMismatch("EQUIPMENT_MOVING")
            # Both ends name each other (_pair), and the planned tractor joint is the
            # one now in use: an attachment at any other joint is not the plan.
            used = next((j for j in tractor.output_joints if j.index == plan.tractor_joint), None)
            if used is None or used.free:
                raise ObservationMismatch("ATTACHMENT_MISMATCH")
            states.append("DELIVER")
            self.world.deliver(tractor.id, destination.id, owner)
            self._arrival(ids, farm_id, owner, target)
            status, reason = "COMPLETE", "DELIVERY_OBSERVED"
        except ObservationMismatch as error:
            status, reason = "UNVERIFIED", str(error)
        except StepUnverified as error:
            status, reason = "UNVERIFIED", error.code
        except (RuntimeError, TimeoutError, ValueError, KeyError) as error:
            # Adapter codes (StepFailed, ObservationError) are kept as the reason.
            status, reason = "FAILED", getattr(error, "code", None) or type(error).__name__
        except Exception as error:
            # An unexpected adapter failure must not fabricate completion.
            status, reason = "UNVERIFIED", type(error).__name__
        finally:
            if not reserved:
                # Nothing can have moved before RESERVE: observation trouble only blocks.
                status = "BLOCKED"
            if reserved:
                try:
                    if physical_attempted:
                        states.append("STOP")
                        if not self.world.stop(ids[0], owner):
                            raise ObservationMismatch("STOP_NOT_CONFIRMED")
                        stopped = {item.id: item for item in self.world.snapshot()}
                        if any(identifier not in stopped or not stopped[identifier].stopped
                               or stopped[identifier].reserved_by != owner for identifier in ids):
                            raise ObservationMismatch("STOP_NOT_OBSERVED")
                    if status == "COMPLETE":
                        self._arrival(ids, farm_id, owner, target)
                    states.append("RELEASE")
                    self.world.release(owner)
                    if any(item.reserved_by == owner for item in self.world.snapshot()):
                        raise ObservationMismatch("RELEASE_NOT_OBSERVED")
                except Exception:
                    status, reason = "UNVERIFIED", "CLEANUP_NOT_CONFIRMED"
        log.info("result %s %s states %s", status, reason, ",".join(states))
        return Result(status, reason, ids, tuple(states))
