"""One deterministic task. No LLM calls, game physics, transport, or simulator import."""
import copy
from dataclasses import dataclass
import functools
import json
import logging
import math
from pathlib import Path
from threading import Lock

from jsonschema import Draft202012Validator

from adapters.world import StepFailed, StepUnverified, World
from companion.hitch import (IMPLEMENT_CATEGORIES, LOCAL_WORK_RADIUS_M, align_problem, approach_poses, hitch_problem,
                             joint_error,
                             plan_hitch)
from protocol.model import Destination, Equipment, HitchPlan, Pose, Position, Result

log = logging.getLogger("farmhand")
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "protocol/intent.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)

# Provisional contract values, to be validated against real equipment in FS25.
# Travel is AutoDrive's when the tractor has it and a marker fits (AutoDrive only
# routes to map markers), otherwise GIANTS' own GOTO navigation to a pose
# (adapters/EVIDENCE.md). "here" prefers the nearest marker within this radius of
# the requester's captured position; without one, GOTO stops the tractor
# DELIVERY_STANDOFF_M short of that position, facing it, so it never drives at
# the requester.
DESTINATION_MAX_FROM_REQUESTER_M = 50.0
DELIVERY_STANDOFF_M = 10.0
# A named building is a destination without AutoDrive: GOTO stops this far short of
# its root node (its centre, which is inside it), facing it. Provisional.
PLACE_STANDOFF_M = 15.0
TRACTOR_ARRIVAL_TOLERANCE_M = 3.0
WAGON_ARRIVAL_TOLERANCE_M = 15.0

# HITCH observation checks (provisional; tune at the first FS25 test). These are
# coarse cross-checks of each step's outcome, not GIANTS' attach rules: GIANTS'
# attachableInfo stays the authority on attachability (adapters/EVIDENCE.md).
NEAR_RADIUS_M = 60.0                      # beyond this, TRAVEL_NEAR runs before STAGE
# TRAVEL_NEAR and STAGE are coarse GIANTS GOTO (or AutoDrive) travel: an ended job
# is never taken as having reached its pose. GIANTS also ends a GOTO when its agent
# finds the target NOT_REACHABLE (AIDrivable:onUpdate 320-322), and live it ended
# 14 m and 138 degrees off, and at 8ffafcc twice NOT_REACHABLE 12.9 and 18.8 m off.
# So GOTO is only long-range travel: after TRAVEL_NEAR the tractor need only be near
# (NEAR_RADIUS_M plus the marker arrival tolerance); STAGE runs only while the
# planned joint is outside LOCAL_WORK_RADIUS_M of the wagon's and must end inside it.
# There Farmhand's bounded LOCAL_MANEUVER lines the tractor up, unless the observed
# joints already lie in the bounded reverse's envelope (companion/hitch.py align_problem).
ALIGN_MAX_DISTANCE_M = 10.0               # = mod/FarmhandHitch.lua Hitch.LIMITS.maxDistanceM
WAGON_MOVED_TOLERANCE_M = 0.25
WAGON_YAW_TOLERANCE_RAD = math.radians(5)
# ALIGN_REVERSE may stop as soon as GIANTS names the exact pair within the
# Farmhand attach-assist envelope, so this must equal its cap
# (mod/FarmhandHitch.lua Hitch.LIMITS.maxAssistRadiusM; checked by a test).
ALIGN_PROGRESS_TOLERANCE_M = 1.5


class ObservationMismatch(Exception):
    pass


class _CleanupFailed(Exception):
    """Cleanup could not prove the pair is idle; the reason says what remained."""


class Cancelled(Exception):
    """The requester cancelled; raised before the next physical step starts."""



# Cancellation is honoured before reserving and before each step that starts
# physical work.
# LOCAL_MANEUVER refusals made before anything moved (mod/FarmhandHitch.lua
# Hitch.newManeuver) because of where the tractor stands: GOTO may aim at the
# other approach and the maneuver is tried again from there. Bounded by the
# approaches (two GOTO aims per hitch); a person nearby is never driven round.
REAPPROACH_CODES = frozenset({"LOCAL_PATH_HITS_WAGON", "LOCAL_PATH_LEAVES_AREA", "LOCAL_PATH_TOO_LONG",
                              "LOCAL_PATH_BLOCKED", "LOCAL_NO_PATH", "LOCAL_START_AT_WAGON",
                              "OUTSIDE_LOCAL_AREA"})

CANCELLABLE_STEPS = frozenset({"RESERVE", "TRAVEL_NEAR", "STAGE", "LOCAL_MANEUVER", "ALIGN_REVERSE", "ATTACH",
                               "DELIVER"})


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


def standoff_pose(goal: Position, origin: Position, fallback_yaw: float,
                  standoff: float = DELIVERY_STANDOFF_M) -> Pose:
    """The GOTO pose ``standoff`` short of ``goal`` (the requester, or a building)
    on the line from ``origin`` (where the pair starts), facing the goal."""
    dx, dz = goal[0] - origin[0], goal[1] - origin[1]
    yaw = math.atan2(dx, dz) if math.hypot(dx, dz) > 1e-6 else fallback_yaw
    return Pose(goal[0] - math.sin(yaw) * standoff, goal[1] - math.cos(yaw) * standoff, yaw)


@dataclass(frozen=True)
class PlaceTarget:
    """A place named in the player's words: an AutoDrive marker of exactly that
    name (used when the tractor has AutoDrive) and/or the one owned building whose
    name contains all its words (GIANTS GOTO to PLACE_STANDOFF_M short of it)."""
    name: str
    marker: Destination | None
    building: Destination | None


def resolve_place(name: str, markers, buildings) -> tuple[PlaceTarget | None, str | None]:
    """Both candidates by exact rules; ambiguity anywhere blocks, nothing is guessed."""
    wanted = _normalized_name(name)
    words = wanted.split()
    exact = [d for d in markers if isinstance(d, Destination) and isinstance(d.name, str)
             and d.name and _normalized_name(d.name) == wanted]
    containing = [b for b in buildings if isinstance(b, Destination) and isinstance(b.name, str)
                  and words and all(w in _normalized_name(b.name).split() for w in words)]
    if len(exact) > 1 or len(containing) > 1:
        return None, "DESTINATION_AMBIGUOUS"
    marker = exact[0] if exact and valid_position(exact[0].position) else None
    building = containing[0] if containing and valid_position(containing[0].position) else None
    if marker is None and building is None:
        return None, "DESTINATION_INVALID" if exact or containing else "DESTINATION_NOT_FOUND"
    return PlaceTarget(name, marker, building), None


def route_position(route: Destination | Pose) -> Position:
    return route.position if isinstance(route, Destination) else (route.x, route.z)


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


# The implement kinds a job may ask for by name (adapters/fs25.py IMPLEMENT_KINDS
# minus headers, which go on combines). "forage_wagon" is the original task.
HITCHABLE_KINDS = frozenset({"forage_wagon", "plow", "cultivator", "sowing_machine", "sprayer", "mower",
                             "tedder", "windrower", "baler", "bale_wrapper", "trailer", "roller", "weeder",
                             "mulcher", "stone_picker", "manure_barrel", "mixer_wagon", "water_trailer",
                             "salt_spreader", "tree_planter"})


def of_kind(item: Equipment, kind: str) -> bool:
    """An implement of the asked kind: GIANTS' ForageWagon specialization for the
    forage wagon (as always), else a towed implement carrying that specialization."""
    if kind == "forage_wagon":
        return item.category == "forage_wagon"
    return item.category in IMPLEMENT_CATEGORIES and kind in item.kinds


def compatible(tractor: Equipment, wagon: Equipment, kind: str = "forage_wagon") -> bool:
    """Pair-level checks. Joint type and PTO are checked per planned joint pair in
    plan_hitch, which every HITCH step re-runs."""
    return (tractor.category == "tractor" and of_kind(wagon, kind)
            and power_sufficient(tractor, wagon)
            and {"move", "attach", "stop"} <= tractor.capabilities)


def rejections(item: Equipment, farm_id: str) -> list[str]:
    """Why ``item`` cannot take part in a new job (diagnostics; usable() decides)."""
    reasons = []
    if item.farm_id != farm_id:
        reasons.append("WRONG_FARM")
    if not item.available:
        reasons.append("STATE_UNOBSERVED")
    if item.occupied:
        reasons.append("SEAT_UNKNOWN")
    if item.busy:
        reasons.append(item.busy_reason or "BUSY")
    if item.reserved_by is not None:
        reasons.append("RESERVED")
    if not valid_position(item.position):
        reasons.append("INVALID_POSITION")
    if not item.stopped:
        reasons.append("MOVING")
    if item.attached_to == "*unknown*":                  # adapters.fs25 UNKNOWN_ATTACHMENT
        reasons.append("ATTACHMENT_UNKNOWN")
    elif item.attached_to is not None:
        reasons.append("ATTACHED")
    if item.category != "tractor" and item.category not in IMPLEMENT_CATEGORIES:
        reasons.append(f"CATEGORY_{item.category}")
    if item.category == "tractor":
        reasons += [f"NO_{c.upper()}_CAPABILITY" for c in ("move", "attach", "stop") if c not in item.capabilities]
    return reasons


def movable(tractor: Equipment, wagon: Equipment) -> bool:
    """move_pair: a tractor with its implement; towing needs no PTO power."""
    return (tractor.category == "tractor" and wagon.category in IMPLEMENT_CATEGORIES
            and {"move", "stop"} <= tractor.capabilities)


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
        self._kind = "forage_wagon"
        self._compatible = compatible

    def execute(self, intent: object, cancel=None, on_state=None) -> Result:
        """``cancel``: an optional threading.Event; once set, no further physical
        step starts, the pair is stopped and released as after any failure, and
        the result is CANCELLED (or UNVERIFIED if that stop is not confirmed). A
        step already running is interrupted by the caller's own stop command.
        ``on_state``: optional callback receiving each state as it is entered."""
        if not VALIDATOR.is_valid(intent):
            return Result("BLOCKED", "INVALID_INTENT")
        # Detach caller-owned input, including the nested target object.
        command = copy.deepcopy(intent)
        with self._lock:
            request_id = command["request_id"]
            if request_id in self._results:
                old, result = self._results[request_id]
                return result if old == command else Result("BLOCKED", "REQUEST_ID_CONFLICT")
            result = self._run(command, cancel, on_state)
            self._results[request_id] = (command, result)
            return result

    def _pair(self, ids, farm_id, owner, attached, check=None):
        check = check or self._compatible
        observed = {item.id: item for item in self.world.snapshot()}
        if any(identifier not in observed for identifier in ids):
            raise ObservationMismatch("EQUIPMENT_MISSING")
        tractor, wagon = (observed[identifier] for identifier in ids)
        if not all(usable(item, farm_id, owner) for item in (tractor, wagon)):
            changed = {i.id: rejections(i, farm_id) for i in (tractor, wagon) if not usable(i, farm_id, owner)}
            log.info("reserved pair changed: %s", changed)
            if any(i.busy for i in (tractor, wagon)) and all(i.available for i in (tractor, wagon)):
                # An AI job or AutoDrive still controls a reserved unit: after a step
                # of ours, our own control has not cleared.
                raise ObservationMismatch("AI_NOT_CLEARED")
            raise ObservationMismatch("EQUIPMENT_CHANGED")
        if not check(tractor, wagon):
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

    def _choose(self, free, destinations, target, items=()):
        """Deterministic first pair (by opaque ID) that is compatible, has a hitch
        plan and a route; all decided before reservation. ``target`` is a resolved
        marker (explicit) or the requester position ("here"). Routes are AutoDrive
        markers (Destination) when the tractor has AutoDrive and one fits, else
        GIANTS GOTO poses (Pose)."""
        reason = "NO_ELIGIBLE_PAIR"
        for tractor in free:
            for wagon in free:
                if not self._compatible(tractor, wagon):
                    if tractor.category == "tractor" and of_kind(wagon, self._kind):
                        log.info("pair %s/%s rejected: %s", tractor.id, wagon.id,
                                 "INSUFFICIENT_PTO_POWER" if not power_sufficient(tractor, wagon)
                                 else "MISSING_CAPABILITY")
                    continue
                plan = plan_hitch(tractor, wagon)
                if plan is None:
                    log.info("pair %s/%s rejected: %s", tractor.id, wagon.id, hitch_problem(tractor, wagon))
                    reason = "NO_HITCH_GEOMETRY" if reason == "NO_ELIGIBLE_PAIR" else reason
                    continue
                autodrive = "autodrive" in tractor.capabilities
                delivery, failure = self._route(target, tractor, plan.target, plan.final.yaw, destinations)
                if delivery is None:
                    log.info("pair %s/%s rejected: %s", tractor.id, wagon.id, failure)
                    reason = failure
                    continue
                aims = approach_poses(plan, tractor.position,
                                      [i.position for i in items if i.id not in (tractor.id, wagon.id)])
                aim = (aims[0].x, aims[0].z)
                travel = None
                if math.dist(tractor.position, aim) > NEAR_RADIUS_M:
                    travel = (autodrive and select_destination(destinations, aim, NEAR_RADIUS_M)
                              or aims[0])
                return (tractor, wagon), (plan, aims), travel, delivery, None
        return None, None, None, None, reason

    @staticmethod
    def _route(target, tractor, origin, fallback_yaw, markers):
        """The delivery route for ``target`` (a resolved marker, a PlaceTarget, or the
        requester's position) by this tractor, from ``origin``: an AutoDrive marker
        when the tractor has AutoDrive and one fits, else a GIANTS GOTO pose."""
        autodrive = "autodrive" in tractor.capabilities
        if isinstance(target, Destination):            # a marker is AutoDrive's; no substitute
            return (target, None) if autodrive else (None, "AUTODRIVE_UNAVAILABLE")
        if isinstance(target, PlaceTarget):
            if autodrive and target.marker is not None:
                return target.marker, None
            if target.building is not None:
                return standoff_pose(target.building.position, origin, fallback_yaw, PLACE_STANDOFF_M), None
            return None, "AUTODRIVE_UNAVAILABLE"
        marker = select_destination(markers, target) if autodrive else None
        return (marker or standoff_pose(target, origin, fallback_yaw)), None

    def _choose_hitched(self, items, farm_id, requester, target, markers):
        """move_pair: the hitched tractor and forage wagon nearest the requester ("this"),
        ties by opaque ID, and its route; decided before reservation."""
        observed = {item.id: item for item in items}
        pairs = []
        for tractor in items:
            wagon = observed.get(tractor.attached_to)
            if (wagon is not None and wagon.attached_to == tractor.id and movable(tractor, wagon)
                    and all(usable(x, farm_id) and x.stopped for x in (tractor, wagon))):
                pairs.append((math.dist(tractor.position, requester), tractor.id, tractor, wagon))
        if not pairs:
            return None, None, "NO_HITCHED_PAIR"
        _, _, tractor, wagon = min(pairs, key=lambda p: (p[0], p[1]))
        route, failure = self._route(target, tractor, tractor.position, tractor.heading or 0.0, markers)
        if route is None:
            return None, None, failure
        return (tractor, wagon), route, None

    def _move(self, tractor_id, route, owner, deliver=False):
        """One travel step by the route's provider: AutoDrive to a marker, or GIANTS
        GOTO to a pose."""
        if isinstance(route, Pose):
            self.world.navigate(tractor_id, route, owner)
        elif deliver:
            self.world.deliver(tractor_id, route.id, owner)
        else:
            self.world.travel(tractor_id, route.id, owner)

    @staticmethod
    def _at(tractor, route) -> bool:
        """AutoDrive markers are checked at the vehicle root; GOTO poses at the AI
        root, the node GIANTS' agent steers (as STAGE is checked)."""
        if isinstance(route, Pose):
            point = tractor.ai_root and (tractor.ai_root.x, tractor.ai_root.z)
        else:
            point = tractor.position
        return (point is not None and valid_position(point)
                and math.dist(point, route_position(route)) <= TRACTOR_ARRIVAL_TOLERANCE_M)

    def _arrival(self, ids, farm_id, owner, route, check=None):
        tractor, wagon = self._pair(ids, farm_id, owner, attached=True, check=check)
        target = route_position(route)
        if (not tractor.stopped or not wagon.stopped or not self._at(tractor, route)
                or math.dist(wagon.position, target) > WAGON_ARRIVAL_TOLERANCE_M):
            log.info("arrival not observed: tractor %s wagon %s target %s stopped %s/%s",
                     tractor.position, wagon.position, target, tractor.stopped, wagon.stopped)
            raise ObservationMismatch("DELIVERY_NOT_OBSERVED")

    def _joints(self, label, ids, farm_id, owner, plan, beyond="STAGE", unaligned="LOCAL_MANEUVER"):
        """VALIDATE plus the observed joint error of the planned pair, logged with the
        step it leads to: ``beyond`` the work radius, ``unaligned`` outside the
        reverse's envelope, else ALIGN_REVERSE."""
        tractor, wagon = self._hitch_state(ids, farm_id, owner, plan)
        error = joint_error(tractor, wagon, plan)
        problem = align_problem(error)
        following = ((beyond or problem) if error and error.gap > LOCAL_WORK_RADIUS_M
                     else (unaligned or problem) if problem else "ALIGN_REVERSE")
        log.info("%s: ai_root %s root %s stopped %s; %s; reverse envelope: %s -> %s", label, tractor.ai_root,
                 tractor.position, tractor.stopped, error.describe() if error else "joints unobservable",
                 problem or "inside", following)
        if error is None:
            raise ObservationMismatch("JOINTS_NOT_OBSERVED")
        return tractor, wagon, error

    def _log_joints(self, label, ids, plan):
        """Diagnostics only: the observed joint error of the pair, whatever happened."""
        try:
            observed = {item.id: item for item in self.world.snapshot()}
            error = joint_error(observed[ids[0]], observed[ids[1]], plan)
        except Exception as failure:                  # never hides the step's own outcome
            error = None
            log.info("%s: no observation (%s)", label, type(failure).__name__)
        log.info("%s: %s", label, error.describe() if error else "joints unobservable")

    def _run(self, command, cancel=None, on_state=None):
        owner, farm_id = command["request_id"], command["farm_id"]
        states, ids = [], ()

        def enter(state):
            if state in CANCELLABLE_STEPS and cancel is not None and cancel.is_set():
                raise Cancelled()
            states.append(state)
            if on_state is not None:
                on_state(state)

        reserved = physical_attempted = False
        status, reason = "BLOCKED", "NO_ELIGIBLE_PAIR"
        moving = command["task"] == "move_pair"
        hitch_only = command["task"] == "hitch_pair"
        # One implement kind per job; every pair check of this job uses it.
        self._kind = command.get("implement", "forage_wagon")
        self._compatible = functools.partial(compatible, kind=self._kind)
        check = movable if moving else self._compatible
        delivery = None
        try:
            # Every destination is resolved and validated before any reservation.
            requester = None
            if command["target"] == "player_at_acceptance" or moving:
                # move_pair always needs the requester: "this" is the pair nearest them.
                requester = self.world.player_position(farm_id)
                if not valid_position(requester):
                    return Result("BLOCKED", "INVALID_REQUESTER_POSITION")
                requester = tuple(requester)
                log.info("requester at %s", requester)
            if command["target"] == "player_at_acceptance":
                enter("FIND")
                target = requester
            elif command["target"]["type"] == "place":
                enter("FIND")
                target, failure = resolve_place(command["target"]["name"], self.world.destinations(),
                                                self.world.places())
                if target is None:
                    return Result("BLOCKED", failure, states=tuple(states))
                log.info("place %r: marker %s building %s", target.name, target.marker, target.building)
            else:
                enter("FIND")
                map_id = (self.world.map_id() if command["target"]["type"] == "field_entrance"
                          else None)
                destination, failure = resolve_explicit_destination(
                    command["target"], self.world.destinations(), map_id, self.field_entrances)
                if destination is None:
                    return Result("BLOCKED", failure, states=tuple(states))
                target = destination
                log.info("destination %s %r at %s", destination.id, destination.name, destination.position)
            # The tractor the requester rides comes first; then the opaque-ID order.
            items = sorted(self.world.snapshot(), key=lambda item: (not item.ridden, item.id))
            if moving:
                pair, delivery, failure = self._choose_hitched(items, farm_id, requester, target,
                                                               self.world.destinations())
                plan = travel = None
                aims = ()
            else:
                free = [item for item in items if usable(item, farm_id)
                        and item.attached_to is None and item.stopped]
                for item in items:
                    if item not in free and (item.category == "tractor" or of_kind(item, self._kind)):
                        log.info("candidate %s %s rejected: %s", item.id, item.category,
                                 ", ".join(rejections(item, farm_id)) or "UNKNOWN")
                pair, plan, travel, delivery, failure = self._choose(free, self.world.destinations(), target, items)
                plan, aims = plan or (None, ())
            if pair is None:
                return Result(status, failure, states=tuple(states))
            ids = tuple(item.id for item in pair)
            if moving:
                log.info("moving hitched tractor %s wagon %s to %s", *ids, delivery)
            else:
                log.info("selected tractor %s wagon %s joints %d/%d approaches %s travel %s delivery %s", *ids,
                         plan.tractor_joint, plan.wagon_joint, list(aims), travel, delivery)
            enter("RESERVE")
            if not self.world.reserve(ids, owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", ids, tuple(states))
            reserved = True
            if moving:
                tractor, _ = self._pair(ids, farm_id, owner, attached=True, check=movable)
                if not tractor.stopped:
                    raise ObservationMismatch("EQUIPMENT_MOVING")
                physical_attempted = True
                enter("DELIVER")
                self._move(tractor.id, delivery, owner, deliver=True)
                self._arrival(ids, farm_id, owner, delivery, check=movable)
            else:
                # HITCH: VALIDATE -> [TRAVEL_NEAR] -> [STAGE] -> [LOCAL_MANEUVER] -> ALIGN_REVERSE
                # -> ATTACH -> VERIFY. Every step is followed by a fresh-snapshot check;
                # returns are not evidence, and each next step is chosen from it.
                # GOTO aims at the approaches beside the implement, each at most once
                # (TRAVEL_NEAR to a pose counts as the first); STAGE runs while the planned
                # joint is outside LOCAL_WORK_RADIUS_M or after LOCAL_MANEUVER refused,
                # unmoved, to start from where the tractor stands (REAPPROACH_CODES).
                physical_attempted = True
                aimed = 0
                if travel is not None:
                    tractor, _ = self._hitch_state(ids, farm_id, owner, plan)
                    enter("TRAVEL_NEAR")
                    self._move(tractor.id, travel, owner)
                    aimed = 1 if travel == aims[0] else 0
                    tractor, _ = self._hitch_state(ids, farm_id, owner, plan)
                    left = math.dist(tractor.position, (aims[0].x, aims[0].z))
                    log.info("TRAVEL_NEAR end: root %s heading %s, %.1f m from the approach",
                             tractor.position, tractor.heading, left)
                    if left > NEAR_RADIUS_M + TRACTOR_ARRIVAL_TOLERANCE_M:
                        raise StepFailed("TRAVEL_ENDED_TOO_FAR")
                tractor, wagon, error = self._joints("HITCH start", ids, farm_id, owner, plan)
                restage = False
                while True:
                    if error.gap > LOCAL_WORK_RADIUS_M or restage:
                        if aimed >= len(aims):
                            raise StepFailed("TRAVEL_ENDED_TOO_FAR")
                        enter("STAGE")
                        self.world.stage(tractor.id, wagon.id, aims[aimed], owner)
                        aimed += 1
                        restage = False
                        tractor, wagon, error = self._joints("STAGE end", ids, farm_id, owner, plan,
                                                             beyond="TRAVEL_ENDED_TOO_FAR")
                        if error.gap > LOCAL_WORK_RADIUS_M:
                            continue
                    if align_problem(error) is None:
                        break
                    enter("LOCAL_MANEUVER")
                    try:
                        self.world.maneuver(tractor.id, wagon.id, plan, owner)
                    except StepFailed as failure:
                        self._log_joints("LOCAL_MANEUVER failed", ids, plan)
                        if failure.code not in REAPPROACH_CODES or aimed >= len(aims):
                            raise
                        log.info("LOCAL_MANEUVER refused unmoved (%s): re-approach %d of %d",
                                 failure.code, aimed + 1, len(aims))
                        restage = True
                        tractor, wagon, error = self._joints("re-approach start", ids, farm_id, owner, plan)
                        continue
                    except Exception:
                        self._log_joints("LOCAL_MANEUVER failed", ids, plan)
                        raise
                    tractor, wagon, error = self._joints("LOCAL_MANEUVER end", ids, farm_id, owner, plan,
                                                         beyond=None, unaligned=None)
                    problem = align_problem(error)
                    if problem is not None:
                        raise StepFailed(problem)
                    break
                enter("ALIGN_REVERSE")
                try:
                    self.world.align(tractor.id, wagon.id, plan, owner)
                finally:
                    self._log_joints("ALIGN_REVERSE end", ids, plan)
                tractor, wagon = self._hitch_state(ids, farm_id, owner, plan)
                error = joint_error(tractor, wagon, plan)
                if error is None or error.gap > ALIGN_PROGRESS_TOLERANCE_M:
                    raise ObservationMismatch("ALIGN_NOT_OBSERVED")
                enter("ATTACH")
                self.world.attach(tractor.id, wagon.id, (plan.tractor_joint, plan.wagon_joint), owner)
                enter("VERIFY")
                tractor, _ = self._pair(ids, farm_id, owner, attached=True)
                if not tractor.stopped:
                    raise ObservationMismatch("EQUIPMENT_MOVING")
                # Both ends name each other (_pair), and the planned tractor joint is the
                # one now in use: an attachment at any other joint is not the plan.
                used = next((j for j in tractor.output_joints if j.index == plan.tractor_joint), None)
                if used is None or used.free:
                    raise ObservationMismatch("ATTACHMENT_MISMATCH")
                if hitch_only:
                    status, reason = "COMPLETE", "HITCH_OBSERVED"     # hitched where the wagon stood
                else:
                    enter("DELIVER")
                    self._move(tractor.id, delivery, owner, deliver=True)
                    self._arrival(ids, farm_id, owner, delivery)
            if not hitch_only:
                status, reason = "COMPLETE", "DELIVERY_OBSERVED"
        except Cancelled:
            status, reason = "CANCELLED", "CANCELLED_BY_REQUESTER"
        except ObservationMismatch as error:
            status, reason = "UNVERIFIED", str(error)
        except StepUnverified as error:
            status, reason = "UNVERIFIED", error.code
            if cancel is not None and cancel.is_set():
                # A step the requester's stop interrupted (e.g. an attach whose end is
                # unknown): CANCELLED, but only if the cleanup below confirms the
                # pair stopped; otherwise it becomes CLEANUP_NOT_CONFIRMED.
                status, reason = "CANCELLED", "CANCELLED_BY_REQUESTER"
        except (RuntimeError, TimeoutError, ValueError, KeyError) as error:
            # Adapter codes (StepFailed, ObservationError) are kept as the reason.
            status, reason = "FAILED", getattr(error, "code", None) or type(error).__name__
            if cancel is not None and cancel.is_set():
                # The step ended because the requester's stop pre-empted it.
                status, reason = "CANCELLED", "CANCELLED_BY_REQUESTER"
        except Exception as error:
            # An unexpected adapter failure must not fabricate completion.
            status, reason = "UNVERIFIED", type(error).__name__
        finally:
            if not reserved and status != "CANCELLED":
                # Nothing can have moved before RESERVE: observation trouble only blocks.
                status = "BLOCKED"
            if reserved:
                try:
                    if physical_attempted:
                        enter("STOP")
                        if not self.world.stop(ids[0], owner):
                            raise ObservationMismatch("STOP_NOT_CONFIRMED")
                        stopped = {item.id: item for item in self.world.snapshot()}
                        if any(identifier not in stopped or not stopped[identifier].stopped
                               or stopped[identifier].reserved_by != owner for identifier in ids):
                            raise ObservationMismatch("STOP_NOT_OBSERVED")
                        if any(stopped[identifier].busy for identifier in ids):
                            # STOP said STOPPED, but an AI job or AutoDrive still controls a
                            # unit: never released as idle; logged, kept reserved.
                            log.info("after STOP still controlled: %s",
                                     {i: stopped[i].busy_reason for i in ids if stopped[i].busy})
                            raise _CleanupFailed("CLEANUP_AI_NOT_CLEARED")
                    if status == "COMPLETE" and hitch_only:
                        tractor, _ = self._pair(ids, farm_id, owner, attached=True)
                        if not tractor.stopped:
                            raise ObservationMismatch("EQUIPMENT_MOVING")
                    elif status == "COMPLETE":
                        self._arrival(ids, farm_id, owner, delivery, check=check)
                    enter("RELEASE")
                    self.world.release(owner)
                    if any(item.reserved_by == owner for item in self.world.snapshot()):
                        raise ObservationMismatch("RELEASE_NOT_OBSERVED")
                except _CleanupFailed as error:
                    status, reason = "UNVERIFIED", str(error)
                except Exception:
                    status, reason = "UNVERIFIED", "CLEANUP_NOT_CONFIRMED"
        log.info("result %s %s states %s", status, reason, ",".join(states))
        return Result(status, reason, ids, tuple(states))
