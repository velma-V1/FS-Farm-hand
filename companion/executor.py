"""One deterministic task. No LLM calls, game physics, transport, or simulator import."""
import copy
from dataclasses import dataclass
import functools
import json
import logging
import math
import re
import time
from pathlib import Path
from threading import Lock

from jsonschema import Draft202012Validator

from adapters.world import StepFailed, StepUnverified, World
from companion import hitch as transport   # its tractor-transport section
from companion.hitch import (ALIGN_MAX_HEADING_RAD, APPROACH_CLEARANCE_M, IMPLEMENT_CATEGORIES, LOCAL_WORK_RADIUS_M, align_problem,
                             against_loose_implement, approach_poses, hitch_problem, joint_error,
                             joint_separation, plan_hitch, PULL_CLEAR_M)
from protocol.model import (Destination, Equipment, FarmInfo, Field, FieldCoverage, HitchPlan, Pose, Position,
                            Result, StoreItem)

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
YARD_M = 150.0                    # buildings matching the words this close together are one place
TRACTOR_ARRIVAL_TOLERANCE_M = 3.0
# GIANTS' field-work helper ended without driving to the field (its planner, or never ready
# to drive): Farmhand's own drive takes the pair this far inside the field's edge.
HELPER_DID_NOT_DRIVE = ("NOT_REACHABLE", "COULD_NOT_PREPARE")
FIELD_INSIDE_M = 8.0
# A loaded transport (cargo belted on its trailer) GIANTS GOTO does not start (live
# d14230aa9fb24d30-3) goes by AutoDrive to a marker; with none to be had the player drives it
# there (assisted); it has arrived within this of the destination's point.
ARRIVED_BY_HAND_M = 25.0
# How AutoDrive's drive ends when it never moved (mod/FarmhandBridge.lua adStallCode): for a
# loaded rig the player then drives it (assisted), and "continue" does not hand it back to AutoDrive.
AD_STALLS = frozenset({"AD_PATHFINDING_STALLED", "AD_WAITING_ON_OBSTACLE", "AD_PAUSED", "AD_NOT_MOVING",
                       "AD_NO_ROUTE", "AD_ENDED_UNMOVED"})
WAGON_ARRIVAL_TOLERANCE_M = 15.0
ARRIVAL_SETTLE_S = 10.0
MAX_TRACTOR_TRIES = 3            # tractors tried for one hitch whose attempt moved nothing
UNMOVED_M = 0.5
# A field farther than this from a tractor GIANTS' helper could not start for is reached by
# AutoDrive first (its helper plans the drive itself; live 5c8eeb68ccea4304-2: 1 km off, refused).
FIELD_REACH_BY_AD_M = 150.0
NOT_RETRIED = frozenset({"RESERVATION_CONFLICT", "CANCELLED_BY_REQUESTER", "INVALID_INTENT"})          # an arrival is judged at rest, waited for this long

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


class _Blocked(Exception):
    """No safe way to start the step: nothing moved (BLOCKED, with its reason)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Cancelled(Exception):
    """The requester cancelled; raised before the next physical step starts."""



# Cancellation is honoured before reserving and before each step that starts
# physical work.
# LOCAL_MANEUVER refusals made before anything moved (mod/FarmhandHitch.lua
# Hitch.newManeuver) because of where the tractor stands: GOTO may aim at the
# other approach and the maneuver is tried again from there. Bounded by the
# approaches (two GOTO aims per hitch); a person nearby is never driven round.
REAPPROACH_CODES = frozenset({"LOCAL_PATH_HITS_WAGON", "LOCAL_PATH_LEAVES_AREA", "LOCAL_PATH_TOO_LONG",
                              "LOCAL_PATH_OBSTRUCTED",
                              "LOCAL_PATH_BLOCKED", "LOCAL_NO_PATH", "LOCAL_START_AT_WAGON",
                              "OUTSIDE_LOCAL_AREA"})

# A towing rig staged by Farmhand's own drive comes this far straight onto its aim.
STRAIGHT_LEAD_M = 12.0

# GOTO drives to approaches per hitch (TRAVEL_NEAR and STAGE together); aims
# GIANTS refuses outright (GOTO_INVALID) move nothing and do not count.
MAX_APPROACH_DRIVES = 3
# A GOTO drive to an approach the game stopped for making no progress (mod/FarmhandBridge.lua
# NAVIGATE_STALL_MS; live 4167b7fd4e8143db-1: 1.2 km, then stuck 12 m short): the tractor is
# stopped, and like an ended GOTO what follows is decided by the observed joints.
GOTO_STALL_CODES = frozenset({"NAVIGATE_STALLED", "NAVIGATE_STUCK"})
# GIANTS' agent still "driving" but nothing moves: something physical its map treats as
# open (live: a closed gate on the road to the store). That spot, just ahead of the
# vehicle, is marked impassable for GIANTS' navigation and the same GOTO asked again,
# at most MAX_DETOURS times per drive.
MAX_DETOURS = 2
DETOUR_AHEAD_M, DETOUR_SIZE_M = 6.0, 8.0
GATE_RADIUS_M = 15.0              # closed gates this near a stuck vehicle are opened first
# Joints at most this far apart already meet: GIANTS' candidate is asked without
# any motion (look-only ALIGN_REVERSE); never a reverse from the far side.
ATTACH_READY_M = 0.5
# A GOTO stopped for good with the player this close to the tractor stopped for them.
PERSON_BLOCK_M = 12.0
# A GOTO that left the tractor within this of where it stood did not drive.
GOTO_MOVED_M = 1.0
# PULL_CLEAR's refusals before any motion (mod/FarmhandHitch.lua Hitch.newPullClear):
# the detach stands (DETACHED_NOT_CLEAR). Every other failure moved or may have.
PULL_CLEAR_REFUSALS = frozenset({"PULL_CLEAR_BLOCKED", "PULL_CLEAR_PERSON", "PULL_CLEAR_NOT_BEHIND",
                                 "LOCAL_AREA_UNKNOWN", "NOT_STOPPED"})
# Stopped short by something ahead after it moved (live dfd431a3308b4da4-1: 4.15 of 4.5 m, a
# parked combine ahead): the detach stands just as well; the tractor, verified at rest, leaves
# later by Farmhand's own drive, which departs from a parked implement behind it (planEgress).
PULL_CLEAR_SHORT = frozenset({"STALLED"})
# The observed joint separation after CLEAR may be this much under PULL_CLEAR_M
# (one snapshot after the brake; the mod stops the instant it reaches it).
PULL_CLEAR_TOLERANCE_M = 0.25
# UNPARK (user-authorized 2026-10-20, AGENTS.md exception 4). GIANTS took the target but
# its agent cannot start from where the vehicle stands (mod/FarmhandBridge.lua
# BLOCKED_START_MS; live 53c77d1e809645b3-3: the Super-Liner inside the Large Shed, BLOCKED
# from the first frame, unmoved; 2026-10-20 the X8 and Puma behind closed Farm Barn doors,
# every aim not_reachable). Building doors are never opened (live bfbc32565ecb4472-1:
# opening the Large Shed's wall shoved four vehicles up to 11.7 m; the user, 2026-10-20:
# gates on roads only) and never driven round: UNPARK refuses with a closed door near the
# rig (UNPARK_DOOR_CLOSED). Otherwise Farmhand's bounded straight extraction
# (mod/FarmhandHitch.lua Hitch.newUnpark) takes it to the nearest exit, and GIANTS gets the
# same target once more, which must move it. At most once per vehicle per job (a plan's
# steps and the next-tractor attempts are one job).
START_BLOCKED_CODES = frozenset({"NAVIGATE_BLOCKED_AT_START", "STAGE_BLOCKED_AT_START"})
# LOAD stopped or refused for its line-up only: BACKOFF and in again, at most LOAD_ATTEMPTS in all.
LOAD_RETRY_CODES = frozenset({"NOT_LINED_UP_AT_RAMPS", "LOAD_NOT_LINED_UP", "DIVERGED"})
LOAD_ATTEMPTS = 3
# UNPARK's cap (= mod/FarmhandHitch.lua Hitch.UNPARK_LIMITS.maxDistanceM; a test checks it)
# and how much more a snapshot of the root may show.
UNPARK_MAX_M, UNPARK_TOLERANCE_M = 20.0, 1.0
UNPARK_MIN_M = 0.5                # an UNPARK DONE that moved less did not happen
# UNPARK's refusals before any motion (mod/FarmhandHitch.lua Hitch.newUnpark).
UNPARK_REFUSALS = frozenset({
    "UNPARK_EXIT_BLOCKED", "UNPARK_TOO_TIGHT", "UNPARK_EXIT_TOO_FAR", "UNPARK_BLOCKED", "UNPARK_PERSON",
    "UNPARK_START_NOT_CLEAR", "UNPARK_REVERSE_NOT_ALLOWED", "UNPARK_RIG_NOT_STRAIGHT", "UNPARK_GEOMETRY_UNKNOWN",
    "UNPARK_ARTICULATED_UNSUPPORTED", "UNPARK_QUERY_UNVERIFIED", "UNPARK_QUERY_UNKNOWN",
    "LOCAL_AREA_UNKNOWN", "NOT_STOPPED", "NOT_ROOT_VEHICLE", "UNPARK_DOOR_CLOSED"})

CANCELLABLE_STEPS = frozenset({"RESERVE", "FIELD_WORK", "PULL_CLEAR", "UNPARK", "TAILGATE", "TRAVEL_NEAR", "STAGE", "LOCAL_MANEUVER", "ALIGN_REVERSE", "ATTACH",
                               "DETACH", "DELIVER", "RAMPS_DOWN", "APPROACH", "LOAD", "BELTS", "UNBELT", "UNLOAD",
                               "RAMPS_UP", "FOLLOW"})


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
    if len(containing) > 1:
        # The buildings of one yard ("the farm": its barn and its silo) are one place: the one
        # nearest their middle (ties by name). Spread across the map: ambiguous.
        spots = [b for b in containing if valid_position(b.position)]
        if len(spots) == len(containing) and max(math.dist(a.position, b.position) for a in spots for b in spots) <= YARD_M:
            mid = (sum(b.position[0] for b in spots) / len(spots), sum(b.position[1] for b in spots) / len(spots))
            containing = [min(spots, key=lambda b: (round(math.dist(b.position, mid), 3), b.name, b.id))]
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


def compatible(tractor: Equipment, wagon: Equipment, kind: str = "forage_wagon", name: str | None = None,
               crop: str | None = None) -> bool:
    """Pair-level checks. Joint type and PTO are checked per planned joint pair in
    plan_hitch, which every HITCH step re-runs. With ``name`` (the player named the
    implement, "the gooseneck") any implement of that name will do, whatever its kind;
    with ``crop``, only a seeder that can sow it."""
    fits = (wagon.category in IMPLEMENT_CATEGORIES and named(wagon, name)) if name else of_kind(wagon, kind)
    if crop is not None and seed_position(wagon, crop) is None:
        return False
    return (tractor.category == "tractor" and fits
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


def _pose_of(tractor: Equipment) -> Pose:
    """The pose GIANTS' agent steers to (the AI root), else the vehicle root."""
    if tractor.ai_root is not None:
        return tractor.ai_root
    return Pose(tractor.position[0], tractor.position[1], tractor.heading or 0.0)


def movable(tractor: Equipment, wagon: Equipment) -> bool:
    """move_pair: a tractor with its implement, or a combine with its header (brought
    back from the field it harvested); towing needs no PTO power."""
    return (tractor.category in ("tractor", "harvester") and wagon.category in IMPLEMENT_CATEGORIES
            and {"move", "stop"} <= tractor.capabilities)


def detachable(tractor: Equipment, implement: Equipment, kind: str | None = None) -> bool:
    """detach_pair: a tractor with its implement (of ``kind`` when named)."""
    return (tractor.category == "tractor" and implement.category in IMPLEMENT_CATEGORIES
            and (kind is None or of_kind(implement, kind)) and {"attach", "stop"} <= tractor.capabilities)


# ---------------------------------------------------------------- field work
# Which owned field, which implement, and what "done" means. The authority is the
# field's own state as GIANTS stores it, measured by mod/FarmhandObserve.lua with GIANTS'
# field-mission pattern (adapters/EVIDENCE.md "Field coverage"): a helper job that ended
# is never "worked"; the measured share is.
# GIANTS' contracts count a field as done at AbstractMission.SUCCESS_FACTOR of its
# area (AbstractFieldMission:getCompletion 452-456 divides by it); its value is not
# documented, so ours is fixed here: 95 % of the field's measured area.
COVERAGE_REQUIRED = 0.95

# Ground types (GIANTS FieldGroundType names, as the running game defines them) that
# each operation leaves. Names the game does not define simply never match.
PREPARED = frozenset({"PLOWED", "CULTIVATED", "SEEDBED", "ROLLED_SEEDBED", "STUBBLE_TILLAGE"})
PLOWED = frozenset({"PLOWED"})
SOWN = frozenset({"SOWN", "DIRECT_SOWN", "PLANTED", "RIDGE", "RIDGE_SOWN"})
# Collecting windrows (straw, grass swaths) with a forage wagon is done when no more than this
# lies on the field per hectare, as measured (mod/FarmhandObserve.lua Observe.fieldWindrow).
# Ours: a forage wagon leaves crumbs, a swath left over is thousands of litres.
WINDROW_LEFT_L_PER_HA = 100.0


@dataclass(frozen=True)
class Operation:
    name: str
    kinds: tuple[str, ...]            # implement kinds (Observe.KINDS) that do it, preferred first
    material: str | None              # the implement's fill-unit role it uses (FillUnit.role)

    def done(self, coverage: FieldCoverage) -> float:
        if self.name == "fertilize":
            return coverage.sprayed()
        if self.name == "collect":
            left = coverage.windrow_litres()
            allowed = WINDROW_LEFT_L_PER_HA * (coverage.total_m2 or 0.0) / 10000.0
            return 1.0 if left is not None and left <= allowed else 0.0
        if self.name == "harvest":
            # The share of the field's crop samples GIANTS counts as cut (getIsCut).
            grown = sum(n for name, n in coverage.fruit or () if name != "NONE")
            return coverage.cut / grown if grown else 0.0
        names = {"plow": PLOWED, "cultivate": PREPARED, "sow": SOWN}[self.name]
        return coverage.share(*names)


OPERATIONS = {
    "plow": Operation("plow", ("plow",), None),
    "cultivate": Operation("cultivate", ("cultivator", "plow"), None),
    "sow": Operation("sow", ("sowing_machine",), "seed"),
    "fertilize": Operation("fertilize", ("sprayer",), "spray"),
    "harvest": Operation("harvest", ("cutter",), None),       # a combine (harvester) with its header
    "collect": Operation("collect", ("forage_wagon",), None),  # windrows picked up (GIANTS ForageWagon)
}


def already_done(operation: Operation, field: Field, area_m2=None) -> bool:
    """The field already shows the operation's result over COVERAGE_REQUIRED of its
    area, or (for a test area) less than that area is left undone."""
    c = field.coverage
    if c is None or operation.name == "collect":
        return False                  # windrow never there is not "collected": crop_problem says it
    done = operation.done(c)
    if area_m2:
        return (1.0 - done) * (c.total_m2 or 0.0) < area_m2
    return done >= COVERAGE_REQUIRED


CROP_READY = frozenset({"HARVEST_READY", "HARVEST_READY_OTHER"})
CROP_SHARE_BLOCKS = 0.5          # most of the field: the work area would be in the crop


def crop_problem(operation: Operation, field: Field) -> str | None:
    """Soil work or sowing would destroy the crop standing on most of the field (live
    FourFields field 1: 100 % HARVEST_READY wheat): harvest comes first. A growing crop
    blocks soil work the same way."""
    c = field.coverage
    if c is None or operation.name == "fertilize":
        return None
    if operation.name == "collect":
        if c.windrow_litres() is None:
            return "WINDROW_UNKNOWN"
        return "NO_WINDROW_ON_FIELD" if operation.done(c) >= 1.0 else None
    if operation.name == "harvest":
        return None if c.share(*CROP_READY) >= CROP_SHARE_BLOCKS else "CROP_NOT_READY"
    if c.share(*CROP_READY) >= CROP_SHARE_BLOCKS:
        return "CROP_READY_TO_HARVEST"
    if operation.name in ("plow", "cultivate") and c.share(*SOWN) >= CROP_SHARE_BLOCKS:
        return "CROP_GROWING"
    return None


def resolve_field(wanted, fields: tuple[Field, ...]) -> tuple[Field | None, str | None]:
    """An owned field by its GIANTS id, or "smallest": the smallest owned field."""
    if not fields:
        return None, "NO_OWNED_FIELD"
    if wanted == "smallest":
        return min(fields, key=lambda f: (f.area_ha, f.id)), None
    match = next((f for f in fields if f.id == wanted), None)
    return (match, None) if match is not None else (None, "FIELD_NOT_OWNED")


def describe(coverage: FieldCoverage | None) -> str:
    if coverage is None:
        return "unmeasured"
    ground = " ".join(f"{name}={share:.3f}" for name, share in coverage.ground)
    spray = " ".join(f"{level}={share:.3f}" for level, share in coverage.spray)
    fruit = " ".join(f"{name}x{n}" for name, n in coverage.fruit or ())
    return f"ground[{ground}] spray[{spray}] fruit[{fruit}]/{coverage.sampled} m2={coverage.total_m2}"


# GIANTS' Deliver helper refusing a rig before it moves (mod/FarmhandBridge.lua starters.deliver):
# AutoDrive may take it instead.
DELIVER_REFUSALS = frozenset({"NO_DELIVER_JOB", "DELIVER_UNAVAILABLE", "DELIVER_INVALID", "NO_SELL_POINT",
                              "DELIVER_ERROR"})


# ------------------------------------------------------------------ procurement
# What to buy for a want, from the store's own catalog (mod/FarmhandObserve.lua
# Observe.storeItem). A want is {"kind": "plow" | "truck" | "trailer" | "tractor" | "spreader" | "sprayer", "size":
# "large" | None, "name": words the player named | None}; the purchase itself and its
# verification are the executor's (task "buy").
STORE_KINDS = {"plow": ("PLOW",), "truck": ("CAR", "TRUCK", "PICKUP"), "trailer": ("TRAILER",),
               "tractor": ("TRACTOR",), "spreader": ("FERTILIZERSPREADER",), "sprayer": ("SPRAYER",)}
# Kinds matched by GIANTS' store category exactly (a substring would also take SEMITRAILERS,
# which need a truck): a tractor-drawn grain tipper.
STORE_KINDS_EXACT = {"grain_trailer": ("TRAILERS",)}
BUY_MONEY_TOLERANCE = 0.99        # the money must fall by at least this share of the price
CATALOG_WAIT_S = 90.0             # the game builds the catalog after a load (25 items per snapshot)


def _matches(item: StoreItem, want: dict) -> bool:
    words = [w for w in (want.get("name") or "").upper().split() if w]
    text = f"{item.name or ''} {item.brand or ''}".upper()
    if words and not all(w in text for w in words):
        return False
    if want["kind"] in STORE_KINDS_EXACT:
        # A grain trailer by what the store says it is and takes (live c83d16674c7645cc-1: the
        # cheapest TRAILERS item was a platform body, no fill unit, no drawbar for the 3650).
        return (any(c in STORE_KINDS_EXACT[want["kind"]] for c in item.categories)
                and (item.capacity or 0.0) >= max(want.get("min_capacity", 0.0), 1.0)
                and (not want.get("fill_type") or want["fill_type"] in item.fill_types))
    kinds = STORE_KINDS.get(want["kind"], ())
    if want["kind"] in ("truck", "tractor") and item.power is None:
        return False                          # a truck drives itself (live: a GMC truck bed was bought)
    if want.get("name") and want["kind"] in ("truck", "trailer", "tractor"):
        return True                           # a named vehicle: its name decides
    return any(k in c for c in item.categories for k in kinds)


def choose_hitch(item: StoreItem, want: dict) -> int:
    """The attacherJoint configuration (1-based) a want asks for, else 0 (the default):
    an option naming the hitch ("gooseneck"), preferring one that also keeps a plain
    hitch ("Gooseneck - Drop Hitch") over one carrying other gear ("Plow - Gooseneck")."""
    hitch = want.get("hitch")
    if not hitch:
        return 0
    options = [(n, name) for n, name in enumerate(item.hitches, 1) if hitch in name.lower()]
    if not options:
        return 0
    return min(options, key=lambda o: ("hitch" not in o[1].lower().replace(hitch, ""), "plow" in o[1].lower(),
                                       len(o[1]), o[0]))[0]


def choose_engine(item: StoreItem, want: dict) -> int:
    """The motor configuration (1-based) whose name has every word of the want's engine
    ("boss hoss": "Boss Hoss"), else 0 (the default; none asked or none named so)."""
    words = (want.get("engine") or "").lower().split()
    if not words:
        return 0
    return next((n for n, name in enumerate(item.engines, 1) if all(w in name.lower() for w in words)), 0)


def _compact(text: str) -> str:
    return "".join(c for c in (text or "").casefold() if c.isalnum())


def _covers(phrase: str, name: str) -> bool:
    """The phrase's letters and digits, run together, are whole words of the name run
    together ("25 mph" is "25MPH" of "25MPH / 41KPH"; "25m" is not part of "25MPH")."""
    key = _compact(phrase)
    tokens = [_compact(t) for t in re.split(r"[^0-9A-Za-z]+", name or "") if t]
    for i in range(len(tokens)):
        run = ""
        for token in tokens[i:]:
            run += token
            if run == key:
                return True
            if len(run) >= len(key):
                break
    return False


def choose_options(item: StoreItem, want: dict) -> tuple[int, ...] | None:
    """The positions (1-based, the mod's Observe.itemOptions order) of the options the
    want names, each the one option whose name it covers word for word ("25 mph":
    "25MPH / 41KPH", not "15MPH / 25KPH"); None when one matches none or several."""
    chosen = []
    for phrase in want.get("options") or ():
        hits = [n for n, option in enumerate(item.options, 1) if _compact(phrase) and _covers(phrase, option[3])]
        if len(hits) != 1:
            log.info("option %r: %d matches among %s", phrase, len(hits), [o[3] for o in item.options])
            return None
        chosen.append(hits[0])
    return tuple(chosen)


def choose_store_item(catalog, want: dict, money: float | None, tractor_power: float | None):
    """(item, None) or (None, why). Large: the widest the farm's strongest tractor can
    power (needed power known and within it); else the cheapest that matches."""
    candidates = [i for i in catalog if _matches(i, want)]
    if not candidates:
        return None, "NOT_IN_STORE"
    if want["kind"] == "plow":
        candidates = [i for i in candidates if i.needed_power is not None and tractor_power is not None
                      and i.needed_power <= tractor_power]
        if not candidates:
            return None, "NO_PLOW_FOR_OUR_TRACTORS"
    if money is not None:
        affordable = [i for i in candidates if i.price <= money]
        if not affordable:
            return None, "NOT_ENOUGH_MONEY"
        candidates = affordable
    if want.get("size") == "large":
        return max(candidates, key=lambda i: ((i.width or 0), -i.price, -i.id)), None
    if want["kind"] in STORE_KINDS_EXACT:
        # Bought for a job, not named: the game's own items only (live e3e3b0c194914776-2: a mod's
        # "EDK 650 Unrealistic" came as a bale loader), the cheapest.
        candidates = [i for i in candidates if not i.mod]
        if not candidates:
            return None, "NOT_IN_STORE"
        return min(candidates, key=lambda i: (i.price, i.id)), None
    return min(candidates, key=lambda i: (-_name_fit(i, want), i.price, i.id)), None


def _name_fit(item: StoreItem, want: dict) -> int:
    """For a named want: 2 when the item's own name has the words and the kind word
    ("Gooseneck Trailer" over "Gooseneck Forestry", live), 1 when its name has the
    words (over a brand-only match), else 0."""
    words = (want.get("name") or "").upper().split()
    name = (item.name or "").upper()
    if not words or not all(w in name for w in words):
        return 0
    return 2 if want["kind"].upper() in name else 1


def named_trailer(item: Equipment, name: str | None) -> bool:
    """A trailer the player named: by its name, or "the gooseneck" for one with a gooseneck
    input (GIANTS' semitrailer joint types; live, the 1124 Deckover is one)."""
    if name and name.strip() in ("gooseneck", "goosenecks", "gooseneck trailer"):
        return named(item, name) or any("semitrailer" in (j.joint_type or "").lower() for j in item.input_joints)
    return named(item, name)


def named(item: Equipment, name: str | None) -> bool:
    """The player named it ("with the gmc", "the deutz"): every word in its store name or
    brand (live: the Series 9 2019 is DEUTZFAHR's)."""
    if not name:
        return True
    text = " ".join(t for t in (item.name, item.brand) if t).casefold()
    return bool(text) and all(w in text.split() or w in text for w in name.split())


# The player's word for a crop where it is not GIANTS' fruit name.
CROP_WORDS = {"corn": "maize", "rapeseed": "canola", "rape": "canola", "soy": "soybean", "beans": "soybean",
              "beets": "sugarbeet", "beet": "sugarbeet", "oats": "oat"}


def seed_position(implement: Equipment, crop: str | None) -> int | None:
    """The 1-based position of ``crop`` (the player's words, "sugar beets") in the
    seeder's own crops (GIANTS fruit names, SUGARBEET), or None if it cannot sow it."""
    if not crop:
        return None
    word = re.sub(r"[^a-z0-9]", "", CROP_WORDS.get(crop.casefold(), crop.casefold()))
    wanted = {word, word[:-1] if word.endswith("s") else word, word[:-2] if word.endswith("es") else word}
    wanted |= {re.sub(r"[^a-z0-9]", "", CROP_WORDS[w]) for w in tuple(wanted) if w in CROP_WORDS}
    return next((n for n, name in enumerate(implement.seeds, 1) if name.casefold() in wanted), None)


def workable(tractor: Equipment, implement: Equipment, kinds: tuple[str, ...] = (), crop: str | None = None) -> bool:
    """field_work: a tractor GIANTS' field-work helper can run (AIFieldWorker) with an
    AI-usable implement (AIImplement) of one of ``kinds`` hitched to it; with ``crop``,
    a seeder that can sow it."""
    harvesting = tractor.category == "harvester" and "cutter" in kinds and of_kind(implement, "cutter")
    return ((detachable(tractor, implement) or (harvesting and implement.attached_to == tractor.id
                                                and {"move", "stop"} <= tractor.capabilities))
            and "ai_field_worker" in tractor.kinds
            and "ai_implement" in implement.kinds and any(of_kind(implement, k) for k in kinds)
            and (crop is None or seed_position(implement, crop) is not None))


def _material(implement: Equipment, role: str | None):
    """The implement's fill unit of ``role`` (seed, spray), or None."""
    return next((u for u in implement.fill_units if role is not None and u.role == role), None)


def _angle(a: float, b: float) -> float:
    """Absolute difference of two yaws, wrapped to [0, pi]."""
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


class Executor:
    """Serialized in-process execution. Replay cache is NOT crash-durable."""

    active: tuple = ()        # the units the running step reserved (observed progress)

    def __init__(self, world: World, field_entrances=None):
        self.world = world
        self.field_entrances = validated_field_entrances(field_entrances)
        self._lock = Lock()
        self._results: dict[str, tuple[dict, Result]] = {}
        self._kind = "forage_wagon"
        self._compatible = compatible
        self._skip: set[str] = set()     # tractors this request already found unable to hitch
        self._chosen: dict[str, tuple] = {}   # the pair this attempt chose, where it stood
        # Where each implement was taken from, by this companion: the tractor's pose
        # when it hitched it (or found it hitched for field work). move_pair to
        # {"type": "origin"} takes it back there (the user: "you can't just leave it
        # in the field"). Never coordinates from words.
        self._origins: dict[str, Pose] = {}
        # Loaded trailers AutoDrive did not move (its stall code): the player drives those.
        self._ad_stalled: dict[str, str] = {}
        # The vehicles UNPARK was asked for, this job only, and those whose next GOTO is
        # its hand-off (it must move them).
        self._job: str | None = None
        self._unparked: set[str] = set()
        self._handoff: set[str] = set()
        self._enter = lambda state: None

    def execute(self, intent: object, cancel=None, on_state=None, job: str | None = None) -> Result:
        """``cancel``: an optional threading.Event; once set, no further physical
        step starts, the pair is stopped and released as after any failure, and
        the result is CANCELLED (or UNVERIFIED if that stop is not confirmed). A
        step already running is interrupted by the caller's own stop command.
        ``on_state``: optional callback receiving each state as it is entered.
        ``job``: the requester's job this intent is a step of (a plan's steps share
        one); per-job budgets such as UNPARK's are kept across its steps."""
        if not VALIDATOR.is_valid(intent):
            return Result("BLOCKED", "INVALID_INTENT")
        # Detach caller-owned input, including the nested target object.
        command = copy.deepcopy(intent)
        with self._lock:
            request_id = command["request_id"]
            if request_id in self._results:
                old, result = self._results[request_id]
                return result if old == command else Result("BLOCKED", "REQUEST_ID_CONFLICT")
            if (job or request_id) != self._job:
                self._job, self._unparked, self._handoff = job or request_id, set(), set()
            result = self._with_other_tractors(command, cancel, on_state)
            self._results[request_id] = (command, result)
            return result

    def _with_other_tractors(self, command, cancel, on_state) -> Result:
        """A hitch that failed with nothing moved (live FourFields: the first tractor by
        ID stood between a hall and a row of implements, GIANTS `not_reachable` for every
        aim) is tried with the next compatible tractor, at most MAX_TRACTOR_TRIES in all;
        the states of every attempt are kept."""
        self._skip = set()
        if command["task"] not in ("hitch_pair", "deliver_tractor_and_forage_wagon"):
            return self._run(command, cancel, on_state)
        states, first = (), None
        for attempt in range(MAX_TRACTOR_TRIES):
            self._chosen = {}
            result = self._run(command, cancel, on_state)
            if first is not None and len(result.equipment_ids) != 2:
                return first                    # no other tractor: the failure that stands
            states += result.states
            result = Result(result.status, result.reason, result.equipment_ids, states)
            if (result.status not in ("FAILED", "BLOCKED") or result.reason in NOT_RETRIED
                    or len(result.equipment_ids) != 2 or (cancel is not None and cancel.is_set())
                    or attempt == MAX_TRACTOR_TRIES - 1 or set(self._chosen) != set(result.equipment_ids)):
                return result
            after = {i.id: i for i in self.world.snapshot()}
            unmoved = all(
                i in after and after[i].attached_to is None
                and math.dist(self._chosen[i], after[i].position) <= UNMOVED_M
                for i in result.equipment_ids)
            if not unmoved:
                return result
            log.info("tractor %s could not reach implement %s (%s), nothing moved: next tractor",
                     result.equipment_ids[0], result.equipment_ids[1], result.reason)
            self._skip.add(result.equipment_ids[0])
            first = result
        return result

    def _pair(self, ids, farm_id, owner, attached, check=None):
        check = check or self._compatible
        # Observed once both are at rest, waiting at most ARRIVAL_SETTLE_S (live 32be7171003041c4-1:
        # the spreader GIANTS just detached still settled onto the ground, EQUIPMENT_MOVING; and
        # c2316ec3fbb54123-1 after UNPARK braked): still moving after that is a real motion.
        observed = self._at_rest(tuple(ids))
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
                # Last: the staging pose itself, straight ahead of the drawbar, from which
                # the bounded reverse attaches without a line-up curve (live: the store's
                # spawn yard leaves no 8 m clearance for any curve; GIANTS plans the way).
                aims = approach_poses(plan, tractor.position,
                                      [i.position for i in items if i.id not in (tractor.id, wagon.id)]) + (plan.staging,)
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

    def _choose_hitched(self, items, farm_id, requester, target, markers, origins=None, kind=None, name=None,
                        cargo=None, implement_name=None):
        """move_pair: the hitched tractor and implement nearest the requester ("this"),
        ties by opaque ID, and its route; decided before reservation. With ``origins``
        (target origin): only an implement this companion took (of ``kind``), routed
        back to the pose it was taken from."""
        observed = {item.id: item for item in items}
        pairs = []
        for tractor in items:
            wagon = observed.get(tractor.attached_to)
            if origins is not None and (wagon is None or wagon.id not in origins
                                        or (kind is not None and not of_kind(wagon, kind))
                                        or not named(wagon, implement_name)):
                continue
            if (wagon is not None and wagon.attached_to == tractor.id and movable(tractor, wagon)
                    and named(tractor, name)       # "move the gmc here": that vehicle's rig only
                    and (cargo is None or any(named(c, cargo) and c.belted_to == wagon.id for c in items))
                    and all(usable(x, farm_id) and x.stopped for x in (tractor, wagon))):
                pairs.append((math.dist(tractor.position, requester), tractor.id, tractor, wagon))
        if not pairs:
            return None, None, "NO_ORIGIN_KNOWN" if origins is not None else "NO_HITCHED_PAIR"
        _, _, tractor, wagon = min(pairs, key=lambda p: (p[0], p[1]))
        if origins is not None:
            route = origins[wagon.id]
            if any(item.belted_to == wagon.id for item in items) and not self._farm_drives() \
                    and math.dist(tractor.position, route_position(route)) > ARRIVED_BY_HAND_M:
                return None, None, "LOADED_NO_DRIVER"          # "bring it back": the player drives it there
            return (tractor, wagon), route, None
        route, failure = self._route(target, tractor, tractor.position, tractor.heading or 0.0, markers)
        if route is None:
            return None, None, failure
        # Where it starts from, for "bring it back" (kept if Farmhand took it from elsewhere),
        # recorded before a loaded rig is left for the player to drive.
        self._origins.setdefault(wagon.id, _pose_of(tractor))
        if any(item.belted_to == wagon.id for item in items) and not self._farm_drives():
            # Cargo belted on the towed trailer. GIANTS GOTO does not start such a rig (live
            # d14230aa9fb24d30-3: its agent BLOCKED from the first frame). AutoDrive counts belted
            # cargo as the rig's own: its sensors skip what checkIsConnected calls connected
            # (AutoDriveUtilFuncs 53-80), getHasObjectMounted, which GIANTS' AttacherJoints
            # passes on to the towed trailer (GDN AttacherJoints 4074-4089) and whose
            # TensionBelts holds the cargo (TensionBelts 1486-1503). So AutoDrive drives it, to a
            # marker made AutoDrive's own way where there is none yet; its drive ends FAILED with
            # what it waited on if it does not move. With no marker the player drives it there
            # and says continue: an assisted delivery (LOADED_NO_DRIVER).
            stalled = self._ad_stalled.get(wagon.id)
            if isinstance(route, Pose) and stalled is None:
                marker, stalled = self._place_marker(target, tractor)
                route = marker or route
            if (stalled is not None or isinstance(route, Pose)) \
                    and math.dist(tractor.position, route_position(route)) > ARRIVED_BY_HAND_M:
                log.info("loaded rig %s/%s %.0f m from %s: no driver (%s); the player drives it", tractor.id,
                         wagon.id, math.dist(tractor.position, route_position(route)), route, stalled)
                return None, None, "LOADED_NO_DRIVER"
        return (tractor, wagon), route, None

    def _to_field(self, tractor, field, ids, owner, enter) -> bool:
        """GIANTS' helper did not start its own drive to the field (NOT_REACHABLE: its
        planner; COULD_NOT_PREPARE: never ready to drive, live 9211b19abaa24cce-3, the combine
        from the barn to field 90): Farmhand's own drive takes the pair a few metres inside
        the field, where the helper starts without a drive of its own (within 3 m of its
        start, AIJobFieldWork:getStartTaskIndex). Else AutoDrive. True when it got there."""
        if not self._farm_drives():
            return self._to_field_by_autodrive(tractor, field, ids, owner, enter)
        from companion.standalone import _edge_point, _inside
        place = next((p for p in self.world.field_places() if p.id == f"field-{field.id}"), None)
        outline = tuple(place.outline) if place is not None else ()
        if len(outline) >= 3 and _inside(outline, tractor.position):
            return False                               # on it already: the helper's own refusal stands
        target = field.center
        if len(outline) >= 3:
            edge = _edge_point(outline, tractor.position)[0]
            span = math.dist(edge, field.center)
            for inward in (FIELD_INSIDE_M, FIELD_INSIDE_M / 2):
                point = (edge[0] + (field.center[0] - edge[0]) * inward / span,
                         edge[1] + (field.center[1] - edge[1]) * inward / span) if span > inward else field.center
                if _inside(outline, point):
                    target = point
                    break
        try:
            enter("TO_FIELD")
            log.info("field %s: GIANTS' helper did not drive there; Farmhand's own drive to %s", field.id, target)
            self.world.farm_drive(tractor.id, tuple(target), owner, 0.0)
        except (StepFailed, StepUnverified) as failure:
            log.info("TO_FIELD by Farmhand's drive: %s", failure.code)
            return self._to_field_by_autodrive(tractor, field, ids, owner, enter)
        self._settled_transport(ids)
        return True

    def _to_field_by_autodrive(self, tractor, field, ids, owner, enter) -> bool:
        """After GIANTS' helper ended NOT_REACHABLE with the pair unmoved far from the field:
        AutoDrive (installed, the tractor its own) to the field's marker, made AutoDrive's own
        way when there is none, under Farmhand's speed cap. True when it got there."""
        now = next((i for i in self.world.snapshot() if i.id == tractor.id), None)
        if (now is None or "autodrive" not in tractor.capabilities
                or math.dist(now.position, tractor.position) > UNMOVED_M
                or math.dist(now.position, field.center) <= FIELD_REACH_BY_AD_M):
            return False
        name = f"field {field.id}"
        marker = next((d for d in self.world.destinations() if d.name.casefold() == name), None)
        try:
            if marker is None:
                self.world.ad_marker(field=field.id)       # AutoDrive's own, after a backup
                marker = next((d for d in self.world.destinations() if d.name.casefold() == name), None)
            if marker is None:
                return False
            enter("TO_FIELD")
            log.info("field %s not reachable for GIANTS' helper from %s: AutoDrive to %s", field.id, now.position,
                     marker)
            graph = self.world.ad_graph()
            if graph is not None:
                # Onto AutoDrive's network where it has the route (live 57e9dbe9633044fd-1: the
                # waypoint nearest the 3650 was on a branch with none, AD_NO_ROUTE).
                self._onto_ad_route(now, ids[1], marker, graph, owner, enter)
                enter("TO_FIELD")
            self.world.deliver(tractor.id, marker.id, owner)      # the pair, by AutoDrive
        except (StepFailed, StepUnverified) as failure:
            log.info("TO_FIELD by AutoDrive: %s", failure.code)
            return False
        except ObservationMismatch as error:
            log.info("TO_FIELD by AutoDrive: %s", error)
            return False
        self._settled_transport(ids)
        return True

    def _place_marker(self, target, tractor):
        """A loaded rig to a place with no AutoDrive marker for it: AutoDrive's own marker made
        where its network meets the place (world.ad_marker, after a backup of its network):
        "Field N" by field N's edge, or the farm building's own name by the building; the
        minimum change, only when the delivery needs it."""
        if "autodrive" not in tractor.capabilities or not isinstance(target, PlaceTarget):
            return None, "LOADED_NEEDS_AUTODRIVE"
        match = re.fullmatch(r"field (\d+)", _normalized_name(target.name))
        if match:
            number = int(match.group(1))
            wanted, place = f"field {number}", {"field": number}
        elif target.building is not None and target.building.name:
            wanted, place = _normalized_name(target.building.name), {"place": target.building.position}
        else:
            return None, "LOADED_NEEDS_AUTODRIVE"
        try:
            self.world.ad_marker(**place)
        except (StepFailed, StepUnverified) as failure:
            log.info("AutoDrive marker for %s: %s", wanted, failure.code)
            return None, failure.code
        marker = next((d for d in self.world.destinations() if isinstance(d, Destination)
                       and _normalized_name(d.name or "") == wanted and valid_position(d.position)), None)
        log.info("AutoDrive marker for %s: %s", wanted, marker)
        return (marker, None) if marker is not None else (None, "MARKER_NOT_OBSERVED")

    def _choose_attached(self, items, farm_id, requester, tractor_name=None):
        """detach_pair: the tractor with its implement (of the job's kind, if named; that
        tractor, if named) nearest the requester, ties by opaque ID; decided before reservation."""
        observed = {item.id: item for item in items}
        pairs = []
        for tractor in items:
            implement = observed.get(tractor.attached_to)
            if (implement is not None and implement.attached_to == tractor.id and self._compatible(tractor, implement)
                    and named(tractor, tractor_name)
                    and all(usable(x, farm_id) and x.stopped for x in (tractor, implement))):
                pairs.append((math.dist(implement.position, requester), tractor.id, tractor, implement))
        if not pairs:
            return None, "NO_ATTACHED_IMPLEMENT"
        _, _, tractor, implement = min(pairs, key=lambda p: (p[0], p[1]))
        return (tractor, implement), None

    def _stall_reason(self, farm_id, tractor) -> str:
        """Why GIANTS' agent stopped for good on the way: the player standing within
        PERSON_BLOCK_M of the tractor (live 1600bdcae60f4499-1: agent BLOCKED 4.7 m from
        them), else stalled."""
        try:
            player = self.world.player_position(farm_id)
        except Exception:                      # diagnostics only
            player = None
        if valid_position(player) and math.dist(tuple(player), tractor.position) <= PERSON_BLOCK_M:
            return "BLOCKED_BY_PERSON"
        return "NAVIGATE_STALLED"

    def _buy(self, command, enter, states) -> Result:
        """FIND (the store's catalog) -> BUY (GIANTS' BuyVehicleData) -> VERIFY: a new
        vehicle of the item's name owned by the farm, and the money gone."""
        farm_id, want = command["farm_id"], command["want"]
        try:
            enter("FIND")
            catalog, waited = self.world.catalog(), 0.0
            while not catalog and waited < CATALOG_WAIT_S:
                time.sleep(3.0)
                waited += 3.0
                catalog = self.world.catalog()
            if not catalog:
                return Result("BLOCKED", "CATALOG_NOT_READY", states=tuple(states))
            before = {i.id: i for i in self.world.snapshot()}
            farm = self.world.farm()
            money = farm.money if farm is not None else None
            names = {i.name for i in catalog if i.power}
            # Only tractors with a three-point (implement) hitch can pull a plow (live: a
            # pickup's 300 hp chose a plow the farm's tractor could not pull).
            power = max((c.power for c in catalog for e in before.values()
                         if e.category == "tractor" and e.name and e.name == c.name and c.power
                         and any("implement" in (j.joint_type or "").lower() for j in e.output_joints)),
                        default=None)
            item, failure = choose_store_item(catalog, want, money, power)
            if item is None:
                log.info("buy %s: %s (tractor power %s, money %s, %d named items)", want, failure, power, money,
                         len(names))
                return Result("BLOCKED", failure, states=tuple(states))
            log.info("buy %s: %s %r (%s) for %.0f, width %s, needs %s of %s", want, item.id, item.name, item.brand,
                     item.price, item.width, item.needed_power, power)
            hitch = choose_hitch(item, want)
            if want.get("hitch") and not hitch:
                return Result("BLOCKED", "NO_HITCH_OPTION", states=tuple(states))
            log.info("buy: hitch option %d %r", hitch, item.hitches[hitch - 1] if hitch else "default")
            engine = choose_engine(item, want)
            if want.get("engine") and not engine:
                log.info("buy: no engine option %r among %s", want["engine"], item.engines)
                return Result("BLOCKED", "NO_ENGINE_OPTION", states=tuple(states))
            log.info("buy: engine option %d %r", engine, item.engines[engine - 1] if engine else "default")
            options = choose_options(item, want)
            if options is None:
                return Result("BLOCKED", "NO_SUCH_OPTION", states=tuple(states))
            log.info("buy: options %s", [item.options[n - 1] for n in options])
            enter("BUY")
            self.world.buy(item.id, hitch, engine, options)
            enter("VERIFY")
            after = {i.id: i for i in self.world.snapshot()}
            new = [i for i in after.values() if i.id not in before and i.name == item.name]
            farm_after = self.world.farm()
            spent = (money - farm_after.money) if (money is not None and farm_after is not None
                                                    and farm_after.money is not None) else None
            log.info("buy: new %s at %s; money %s -> %s", [i.id for i in new], [i.position for i in new], money,
                     farm_after and farm_after.money)
            if len(new) != 1:
                return Result("UNVERIFIED", "PURCHASE_NOT_OBSERVED", states=tuple(states))
            if spent is None or spent < item.price * BUY_MONEY_TOLERANCE:
                return Result("UNVERIFIED", "PAYMENT_NOT_OBSERVED", (new[0].id,), tuple(states))
            return Result("COMPLETE", "BOUGHT", (new[0].id,), tuple(states))
        except Cancelled:
            return Result("CANCELLED", "CANCELLED_BY_REQUESTER", states=tuple(states))
        except StepUnverified as error:
            return Result("UNVERIFIED", error.code, states=tuple(states))
        except (RuntimeError, ValueError, KeyError) as error:
            return Result("FAILED", getattr(error, "code", None) or type(error).__name__, states=tuple(states))

    LAND_VERIFY_S = 10.0

    def _land(self, command, enter, states) -> Result:
        """FIND (the field's farmland parcel, its owner and price) -> BUY_LAND / SELL_LAND
        (GIANTS' own request at the parcel's price) -> VERIFY: the owner changed and the money
        moved (bought: fell by the price; sold: rose). Refused unchanged when already the farm's,
        not for sale, not the farm's to sell, or the balance short."""
        field, buying = command["field"], command["land_action"] == "buy"
        try:
            enter("FIND")
            parcel = next((p for p in self.world.land() if p.field == field), None)
            if parcel is None or parcel.farmland is None or parcel.owner is None:
                return Result("BLOCKED", "FIELD_NOT_FOUND", states=tuple(states))
            farm = self.world.farm()
            money = farm.money if farm is not None else None
            if buying:
                if parcel.owner == "mine":
                    return Result("BLOCKED", "LAND_ALREADY_OWNED", states=tuple(states))
                if parcel.owner != "none":
                    return Result("BLOCKED", "LAND_NOT_FOR_SALE", states=tuple(states))
                if parcel.price is None or money is None:
                    return Result("BLOCKED", "LAND_PRICE_UNKNOWN", states=tuple(states))
                if money < parcel.price:
                    return Result("BLOCKED", "NOT_ENOUGH_MONEY", states=tuple(states))
            elif parcel.owner != "mine":
                return Result("BLOCKED", "LAND_NOT_OWNED", states=tuple(states))
            log.info("land: field %d farmland %d %s for %s (balance %s)", field, parcel.farmland,
                     "buying" if buying else "selling", parcel.price, money)
            enter("BUY_LAND" if buying else "SELL_LAND")
            self.world.land_change(field, buying)
            enter("VERIFY")
            want = "mine" if buying else "none"
            deadline = time.monotonic() + self.LAND_VERIFY_S
            while True:
                now = next((p for p in self.world.land() if p.field == field), None)
                after = self.world.farm()
                spent = (money - after.money) if (money is not None and after is not None and after.money is not None) else None
                paid = (spent is not None and parcel.price is not None and spent >= parcel.price * BUY_MONEY_TOLERANCE
                        if buying else spent is not None and spent < 0)
                if now is not None and now.owner == want and paid:
                    log.info("land: field %d now %s; money %s -> %s", field, want, money, after.money)
                    return Result("COMPLETE", "LAND_BOUGHT" if buying else "LAND_SOLD", states=tuple(states))
                if time.monotonic() >= deadline:
                    log.info("land: field %d owner %s, money %s -> %s", field, now and now.owner, money,
                             after and after.money)
                    return Result("UNVERIFIED", "LAND_NOT_OBSERVED" if now is None or now.owner != want
                                  else "PAYMENT_NOT_OBSERVED", states=tuple(states))
                time.sleep(0.5)
        except Cancelled:
            return Result("CANCELLED", "CANCELLED_BY_REQUESTER", states=tuple(states))
        except StepUnverified as error:
            return Result("UNVERIFIED", error.code, states=tuple(states))
        except StepFailed as error:
            return Result("BLOCKED" if states[-1:] == ["FIND"] else "FAILED", error.code, states=tuple(states))

    # ------------------------------------------------------------ transport
    def _transport(self, command, enter, states) -> Result:
        """Tractor transport (AGENTS.md exception 5), each step verified from a fresh snapshot.
        load_tractor: FIND (a transport trailer with Foldable ramps and belts; a free tractor
        alone) -> RESERVE -> RAMPS_DOWN -> APPROACH (GIANTS GOTO right behind the ramps, on
        the trailer's axis) -> LOAD -> BELTS (the trailer holding the tractor) -> RAMPS_UP.
        unload_tractor: FIND (a trailer holding or carrying a tractor) -> RESERVE -> UNBELT ->
        RAMPS_DOWN -> UNLOAD (straight back off) -> RAMPS_UP. Then STOP and RELEASE; a
        failure after RESERVE stops the tractor and keeps the reservation unless confirmed."""
        owner, farm_id = command["request_id"], command["farm_id"]
        loading = command["task"] == "load_tractor"
        reserved = moved = False
        ids: tuple = ()
        status, reason = "BLOCKED", "NO_TRANSPORT"
        try:
            enter("FIND")
            items = sorted(self.world.snapshot(), key=lambda i: i.id)
            observed = {i.id: i for i in items}
            trailers = [i for i in items if usable(i, farm_id) and i.stopped and transport.is_transport(i)
                        and named_trailer(i, command.get("implement_name"))
                        and (i.attached_to is None or (observed.get(i.attached_to) is not None
                                                       and observed[i.attached_to].stopped))]
            if not trailers:
                return Result("BLOCKED", "NO_TRANSPORT", states=tuple(states))
            pairs = []
            for trailer in trailers:
                for tractor in items:
                    implement = observed.get(tractor.attached_to)
                    if (tractor.category != "tractor" or tractor.id == trailer.id or tractor.id == trailer.attached_to
                            or not usable(tractor, farm_id) or not tractor.stopped
                            # Alone, or with what it carries (a mounted implement: the mod decides
                            # whether it may come along); never towing the transport or a trailer.
                            or (tractor.attached_to is not None and (implement is None or implement.id == trailer.id
                                                                     or implement.attached_to != tractor.id
                                                                     or transport.is_transport(implement)))
                            or not named(tractor, command.get("tractor_name"))):
                        continue
                    aboard = tractor.belted_to == trailer.id or transport.on_deck(tractor, trailer)
                    if loading and aboard:
                        # Already on it (a goal resumed, or loaded by hand): verify and continue.
                        pairs.append((-1.0, tractor.id, tractor, trailer))
                    elif loading and (tractor.belted_to is None and transport.load_aims(tractor, trailer)
                                      and not any(transport.on_deck(tractor, t) for t in trailers)):
                        pairs.append((math.dist(tractor.position, trailer.position), tractor.id, tractor, trailer))
                    elif not loading and (tractor.belted_to == trailer.id or transport.on_deck(tractor, trailer)
                                          or transport.on_ramps(tractor, trailer)):    # stopped on its way off
                        pairs.append((0.0, tractor.id, tractor, trailer))
            if not pairs:
                return Result("BLOCKED", "NO_TRACTOR_TO_LOAD" if loading else "NO_LOADED_TRACTOR",
                              states=tuple(states))
            _, _, tractor, trailer = min(pairs, key=lambda p: (p[0], p[1]))
            ids = (tractor.id, trailer.id)
            log.info("%s tractor %s %r on trailer %s %r (towed by %s)", "loading" if loading else "unloading",
                     tractor.id, tractor.name, trailer.id, trailer.name, trailer.attached_to)
            enter("RESERVE")
            if not self.world.reserve(ids, owner, transport=True):
                return Result("BLOCKED", "RESERVATION_CONFLICT", ids, tuple(states))
            reserved = True
            self.active = ids
            if loading:
                aboard = tractor.belted_to == trailer.id or transport.on_deck(tractor, trailer)
                if not aboard:
                    self._ramps(trailer.id, 1, owner, "RAMPS_DOWN")
                    tractor, trailer = self._transport_pair(ids)
                    if not transport.lined_up(tractor, trailer, share=0.5):
                        enter("APPROACH")
                        moved = True
                        self._approach(tractor, trailer, owner)
                    enter("LOAD")
                    moved = True
                    for attempt in range(LOAD_ATTEMPTS):
                        try:
                            self.world.load(tractor.id, trailer.id, 1, owner)
                            break
                        except StepFailed as failure:
                            # Stopped at the ramps' foot not lined up (or refused there): straight back
                            # along its now straighter heading and in again (BACKOFF, exception 5).
                            if failure.code not in LOAD_RETRY_CODES or attempt == LOAD_ATTEMPTS - 1:
                                raise
                            log.info("LOAD %s: back off and in again", failure.code)
                            # At rest first (live 95b84283873043d7-1-h2: BACKOFF 16 ms after LOAD
                            # braked from 2.69 km/h was refused NOT_STOPPED).
                            self._settled_transport(ids)
                            enter("BACKOFF")
                            self.world.backoff(tractor.id, trailer.id, owner)
                            tractor, trailer = self._settled_transport(ids)
                            enter("LOAD")
                    tractor, trailer = self._settled_transport(ids)
                    if not transport.on_deck(tractor, trailer):
                        raise ObservationMismatch("NOT_ON_DECK")
                else:
                    log.info("tractor %s already on trailer %s (belted: %s): continuing", tractor.id, trailer.id,
                             tractor.belted_to == trailer.id)
                if tractor.belted_to != trailer.id or trailer.belts_fastened is not True:
                    enter("BELTS")
                    self.world.belts(trailer.id, True, owner)
                tractor, trailer = self._transport_pair(ids)
                if trailer.belts_fastened is not True or tractor.belted_to != trailer.id:
                    log.info("belts: fastened %s, holding %s, tractor belted to %s", trailer.belts_fastened,
                             trailer.belted, tractor.belted_to)
                    raise ObservationMismatch("BELTS_NOT_HOLDING")
                self._ramps(trailer.id, -1, owner, "RAMPS_UP")
                status, reason = "COMPLETE", "LOADED_AND_SECURED"
            else:
                enter("UNBELT")
                self.world.belts(trailer.id, False, owner)
                tractor, trailer = self._transport_pair(ids)
                if trailer.belts_fastened is not False or tractor.belted_to is not None:
                    raise ObservationMismatch("BELTS_NOT_RELEASED")
                self._ramps(trailer.id, 1, owner, "RAMPS_DOWN")
                enter("UNLOAD")
                moved = True
                self.world.load(tractor.id, trailer.id, -1, owner)
                tractor, trailer = self._settled_transport(ids)
                if not transport.off_ramps(tractor, trailer):
                    raise ObservationMismatch("NOT_OFF_RAMPS")
                self._ramps(trailer.id, -1, owner, "RAMPS_UP")
                status, reason = "COMPLETE", "UNLOADED"
        except Cancelled:
            status, reason = "CANCELLED", "CANCELLED_BY_REQUESTER"
        except ObservationMismatch as error:
            status, reason = "UNVERIFIED", str(error)
        except StepUnverified as error:
            status, reason = "UNVERIFIED", error.code
        except StepFailed as error:
            status, reason = "FAILED", error.code
        except (RuntimeError, ValueError, KeyError) as error:
            status, reason = "FAILED", getattr(error, "code", None) or type(error).__name__
        finally:
            if reserved:
                try:
                    if moved:
                        enter("STOP")
                        if not self.world.stop(ids[0], owner):
                            raise ObservationMismatch("STOP_NOT_CONFIRMED")
                        if not self._at_rest(ids)[ids[0]].stopped:
                            raise ObservationMismatch("STOP_NOT_OBSERVED")
                    enter("RELEASE")
                    self.world.release(owner)
                except Exception as error:
                    log.info("cleanup not confirmed: %s %s", type(error).__name__, error)
                    status, reason = "UNVERIFIED", "CLEANUP_NOT_CONFIRMED"
        self.active = ()
        log.info("result %s %s states %s", status, reason, ",".join(states))
        return Result(status, reason, ids, tuple(states))

    def _follow(self, command, enter, states) -> Result:
        """"Follow me" (only when the player asks): FIND the hitched rig named (else the one
        nearest the player that they do not ride) -> RESERVE -> FOLLOW (FollowMe, until it
        ends) -> STOP -> RELEASE. It never delivers, unloads or does anything else."""
        owner, farm_id = command["request_id"], command["farm_id"]
        ids, reserved, status, reason = (), False, "BLOCKED", "NO_RIG_TO_FOLLOW"
        try:
            enter("FIND")
            player = self.world.player_position(farm_id)
            items = sorted(self.world.snapshot(), key=lambda i: i.id)
            observed = {i.id: i for i in items}
            rigs = []
            for tractor in items:
                wagon = observed.get(tractor.attached_to)
                if (tractor.category == "tractor" and wagon is not None and wagon.attached_to == tractor.id
                        and not tractor.ridden and named(tractor, command.get("tractor_name"))
                        and all(usable(x, farm_id) and x.stopped for x in (tractor, wagon))):
                    distance = math.dist(tractor.position, player) if valid_position(player) else 0.0
                    rigs.append((distance, tractor.id, tractor, wagon))
            if not rigs:
                return Result("BLOCKED", "NO_RIG_TO_FOLLOW", states=tuple(states))
            _, _, tractor, wagon = min(rigs, key=lambda r: (r[0], r[1]))
            ids = (tractor.id, wagon.id)
            enter("RESERVE")
            if not self.world.reserve(ids, owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", ids, tuple(states))
            reserved = True
            self.active = ids
            enter("FOLLOW")
            self.world.follow(tractor.id, owner)
            status, reason = "COMPLETE", "FOLLOW_ENDED"
        except Cancelled:
            status, reason = "CANCELLED", "CANCELLED_BY_REQUESTER"
        except StepUnverified as error:
            status, reason = "UNVERIFIED", error.code
        except StepFailed as error:
            status, reason = "FAILED", error.code
        except (RuntimeError, ValueError, KeyError) as error:
            status, reason = "FAILED", getattr(error, "code", None) or type(error).__name__
        finally:
            if reserved:
                try:
                    enter("STOP")
                    if not self.world.stop(ids[0], owner):
                        raise ObservationMismatch("STOP_NOT_CONFIRMED")
                    if not self._at_rest(ids)[ids[0]].stopped:
                        raise ObservationMismatch("STOP_NOT_OBSERVED")
                    enter("RELEASE")
                    self.world.release(owner)
                except Exception as error:
                    log.info("cleanup not confirmed: %s %s", type(error).__name__, error)
                    status, reason = "UNVERIFIED", "CLEANUP_NOT_CONFIRMED"
        self.active = ()
        log.info("result %s %s states %s", status, reason, ",".join(states))
        return Result(status, reason, ids, tuple(states))

    def _autodrive_rig(self, command, enter, states) -> Result:
        """AutoDrive's own jobs for a rig with a trailer (the user 2026-10-21: unload the
        combines while they harvest; sell the product).
        unload_harvester: FIND an AutoDrive tractor with a trailer that has room, free ->
        RESERVE -> UNLOADER (AutoDrive's combine unloader, waiting at the field's marker and
        unloading at the best paid sell point for its crop): COMPLETE UNLOADER_STARTED with
        the rig still reserved (it goes on; the coordinator stops it).
        sell: FIND the rig with the most cargo (by name if said) -> RESERVE -> SELL (GIANTS'
        own Deliver helper to the best paid selling station, tipped there; AutoDrive's
        deliver mode when GIANTS refuses the rig) -> VERIFY (the cargo gone, the money
        arrived) -> STOP -> RELEASE.
        store: as sell, to the farm's own storage (GIANTS' Deliver helper; never AutoDrive's
        sell mode) -> STORE -> VERIFY (the cargo gone; nothing is paid)."""
        owner, farm_id = command["request_id"], command["farm_id"]
        storing = command["task"] == "store"
        selling = command["task"] in ("sell", "store")
        ids, reserved, handed, moved = (), False, False, False
        status, reason = "BLOCKED", "NO_RIG_TO_SELL" if selling else "NO_UNLOADER"

        def cargo(unit):
            return sum(u.level or 0.0 for u in unit.fill_units)

        try:
            enter("FIND")
            items = sorted(self.world.snapshot(), key=lambda i: i.id)
            observed = {i.id: i for i in items}
            rigs = []
            for tractor in items:
                trailer = observed.get(tractor.attached_to)
                if not (tractor.category == "tractor" and (selling or "autodrive" in tractor.capabilities) and trailer is not None
                        and trailer.attached_to == tractor.id and "trailer" in trailer.kinds and trailer.fill_units
                        and all(usable(x, farm_id) and x.stopped for x in (tractor, trailer))
                        and named(tractor, command.get("tractor_name"))):
                    continue
                room = sum((u.capacity or 0.0) - (u.level or 0.0) for u in trailer.fill_units)
                if selling and cargo(trailer) > 0:
                    rigs.append((-cargo(trailer), tractor.id, tractor, trailer))
                elif not selling and room > 0:
                    rigs.append((cargo(trailer), tractor.id, tractor, trailer))
            if not rigs:
                return Result("BLOCKED", reason, states=tuple(states))
            _, _, tractor, trailer = min(rigs, key=lambda r: (r[0], r[1]))
            ids = (tractor.id, trailer.id)
            marker = None
            if not selling:
                name = f"field {command['field']}"
                marker = next((d for d in self.world.destinations() if d.name.casefold() == name), None)
                if marker is None:
                    self.world.ad_marker(field=command["field"])  # AutoDrive's own, after a backup
                    marker = next((d for d in self.world.destinations() if d.name.casefold() == name), None)
                if marker is None:
                    return Result("BLOCKED", "AD_NO_FIELD_MARKER", ids, tuple(states))
            enter("RESERVE")
            if not self.world.reserve(ids, owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", ids, tuple(states))
            reserved = True
            self.active = ids
            if not selling:
                enter("UNLOADER")
                moved = True
                self.world.ad_unload(tractor.id, marker.id, command["field"], owner)
                handed = True
                status, reason = "COMPLETE", "UNLOADER_STARTED"
            else:
                before = cargo(trailer)
                farm = self.world.farm()
                money = farm.money if farm is not None else None
                enter("STORE" if storing else "SELL")
                moved = True
                try:
                    self.world.giants_deliver(tractor.id, owner, store=storing)  # GIANTS' own Deliver helper
                except StepFailed as failure:
                    if storing or failure.code not in DELIVER_REFUSALS or "autodrive" not in tractor.capabilities:
                        raise
                    log.info("SELL: GIANTS' deliver refused (%s); AutoDrive instead", failure.code)
                    self.world.ad_sell(tractor.id, owner)
                enter("VERIFY")
                after = self._at_rest(ids)
                left = cargo(after[trailer.id])
                farm = self.world.farm()
                log.info("%s: cargo %.0f -> %.0f l; money %s -> %s", "STORE" if storing else "SELL", before, left,
                         money, farm and farm.money)
                if left > before * 0.05:
                    raise ObservationMismatch("CARGO_NOT_UNLOADED")
                if storing:
                    status, reason = "COMPLETE", "STORED"
                elif money is None or farm is None or farm.money is None or farm.money <= money:
                    raise ObservationMismatch("PAYMENT_NOT_OBSERVED")
                else:
                    status, reason = "COMPLETE", "SOLD"
        except Cancelled:
            status, reason = "CANCELLED", "CANCELLED_BY_REQUESTER"
        except ObservationMismatch as error:
            status, reason = "UNVERIFIED", str(error)
        except StepUnverified as error:
            status, reason = "UNVERIFIED", error.code
        except StepFailed as error:
            status, reason = "FAILED", error.code
        except (RuntimeError, ValueError, KeyError) as error:
            status, reason = "FAILED", getattr(error, "code", None) or type(error).__name__
        finally:
            if reserved and handed:
                log.info("unloader %s: reserved under %s while AutoDrive works", ids[0], owner)
            elif reserved:
                try:
                    if moved:
                        enter("STOP")
                        if not self.world.stop(ids[0], owner):
                            raise ObservationMismatch("STOP_NOT_CONFIRMED")
                        if not self._at_rest(ids)[ids[0]].stopped:
                            raise ObservationMismatch("STOP_NOT_OBSERVED")
                    enter("RELEASE")
                    self.world.release(owner)
                except Exception as error:
                    log.info("cleanup not confirmed: %s %s", type(error).__name__, error)
                    status, reason = "UNVERIFIED", "CLEANUP_NOT_CONFIRMED"
        self.active = ()
        log.info("result %s %s states %s", status, reason, ",".join(states))
        return Result(status, reason, ids, tuple(states))

    # Not grain: what a vehicle burns or breathes (GIANTS' propellant and air fill units).
    NOT_CARGO = frozenset({"DIESEL", "DEF", "AIR", "ELECTRICCHARGE", "METHANE", "UNKNOWN"})
    # GIANTS GOTO puts the trailer's middle this far behind the pipe point, on its line, then
    # ALONGSIDE drives it under (live 3dc00834639340f7-1: GIANTS took no spot right under it).
    # live 6c66894767044432-1: 12 m was not_reachable, 18 m reached; at most 21 m, so the drive's 3 m
    # arrival tolerance stays inside ALONGSIDE's 25 m (live 87f20d07d3184240-1: 22 m, ALONGSIDE_TOO_FAR).
    # One run-up, once from behind and once from the front, nothing more (the user, 2026-10-31:
    # "driving the equipment around in circles ... lined up 5 or 6 times"): then it says why.
    OVERLOAD_RUN_UPS_M = (18.0,)

    def _grain(self, vehicle):
        return sum(u.level or 0.0 for u in vehicle.fill_units
                   if (u.level or 0.0) > 0 and (u.fill_type or "UNKNOWN") not in self.NOT_CARGO)

    def _overload(self, command, enter, states) -> Result:
        """A standing combine emptied into a trailer under its pipe, as a player does it (the
        user 2026-10-21: the combine full, its helper would not work on, and AutoDrive's
        unloader serves only a combine whose helper runs). FIND the combine with the most
        grain and the nearest tractor with a trailer that takes it -> RESERVE both ->
        PIPE_OUT -> APPROACH (GIANTS GOTO onto the pipe's line behind it) -> ALONGSIDE
        (onto the pipe's line until the trailer's middle is under the pipe; AGENTS.md exception 6)
        -> OVERLOAD (GIANTS' own discharge) -> PASS (on past the combine) -> PIPE_IN -> VERIFY
        (the grain moved into the trailer) -> STOP -> RELEASE."""
        owner, farm_id = command["request_id"], command["farm_id"]
        combine_owner = f"{owner}-c"
        rig_ids, combine_ids, reserved, moved, piped = (), (), [], False, False
        status, reason = "BLOCKED", "NO_FULL_COMBINE"
        try:
            enter("FIND")
            items = sorted(self.world.snapshot(), key=lambda i: i.id)
            observed = {i.id: i for i in items}
            combines = [c for c in items if c.category == "harvester" and c.pipe is not None and self._grain(c) > 0
                        and usable(c, farm_id) and c.stopped and named(c, command.get("tractor_name"))]
            if not combines:
                return Result("BLOCKED", reason, states=tuple(states))
            combine = max(combines, key=lambda c: (self._grain(c), c.id))
            grain = next(u.fill_type for u in combine.fill_units
                         if (u.level or 0.0) > 0 and (u.fill_type or "UNKNOWN") not in self.NOT_CARGO)
            rigs = []
            for tractor in items:
                trailer = observed.get(tractor.attached_to)
                if not (tractor.category == "tractor" and trailer is not None and trailer.attached_to == tractor.id
                        and "trailer" in trailer.kinds and trailer.fill_units and trailer.size is not None
                        and trailer.heading is not None and tractor.ai_root is not None and tractor.heading is not None
                        and all(usable(x, farm_id) and x.stopped for x in (tractor, trailer))):
                    continue
                room = sum((u.capacity or 0.0) - (u.level or 0.0) for u in trailer.fill_units
                           if grain in u.supported and (not u.level or u.fill_type == grain))
                if room > 0:
                    rigs.append((math.dist(tractor.position, combine.position), tractor.id, tractor, trailer))
            if not rigs:
                return Result("BLOCKED", "NO_TRAILER_FOR_THE_GRAIN", (combine.id,), tuple(states))
            _, _, tractor, trailer = min(rigs, key=lambda r: (r[0], r[1]))
            rig_ids = (tractor.id, trailer.id)
            combine_ids = (combine.id, combine.attached_to) if combine.attached_to else ()
            enter("RESERVE")
            if not combine_ids or not self.world.reserve(combine_ids, combine_owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", (combine.id,), tuple(states))
            reserved.append(combine_owner)
            if not self.world.reserve(rig_ids, owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", rig_ids, tuple(states))
            reserved.append(owner)
            self.active = rig_ids
            before = (self._grain(combine), sum(u.level or 0.0 for u in trailer.fill_units))
            enter("PIPE_OUT")
            self.world.pipe(combine.id, True, combine_owner)
            piped = True
            combine = next(i for i in self.world.snapshot() if i.id == combine.id)
            if not (combine.pipe and combine.pipe[0]):
                raise ObservationMismatch("PIPE_NOT_OBSERVED")
            done = None
            # Already standing at the combine (the user, 2026-10-31: "the tractor and trailer are in
            # the correct place at the moment for unloading"): from where it stands first, GIANTS'
            # own discharge if the trailer is under the pipe (NOT_OVERLOADING after 15 s if not),
            # else ALONGSIDE from there; only then the approaches.
            try:
                enter("OVERLOAD")
                done = self.world.overload(combine.id, trailer.id, combine_owner)
            except StepFailed as failure:
                log.info("OVERLOAD where the rig stands: %s", failure.code)
                if failure.code != "NOT_OVERLOADING":
                    raise
                # Let go idle, the combine folds its pipe (live 78d50d7a616a49db-3: PIPE_NOT_OUT next).
                self.world.pipe(combine.id, True, combine_owner)
                combine = next(i for i in self.world.snapshot() if i.id == combine.id)
            if done is None:
                try:
                    enter("ALONGSIDE")
                    self.world.alongside(tractor.id, combine.id, owner, combine_owner)
                    moved = True
                    self._settled_transport(rig_ids)
                    enter("OVERLOAD")
                    done = self.world.overload(combine.id, trailer.id, combine_owner)
                except StepFailed as failure:
                    log.info("ALONGSIDE from where the rig stands: %s", failure.code)
                    if not (failure.code.startswith("ALONGSIDE_") or failure.code == "NOT_OVERLOADING"):
                        raise
                    if failure.code == "NOT_OVERLOADING":
                        self.world.pipe(combine.id, True, combine_owner)
            # The combine stays where it is: with no way under its pipe there the player is told
            # (moving the combine around and trying again was the circling the user saw).
            for _ in range(0 if done is not None else 1):
                # The trailer's middle (its size box centre) relative to the tractor's AI root, along the tractor.
                tx, tz = math.sin(tractor.heading), math.cos(tractor.heading)
                fx, fz = math.sin(trailer.heading), math.cos(trailer.heading)
                middle = (trailer.position[0] + fx * trailer.size[3], trailer.position[1] + fz * trailer.size[3])
                behind = (middle[0] - tractor.ai_root.x) * tx + (middle[1] - tractor.ai_root.z) * tz
                cx, cz = math.sin(combine.heading or 0.0), math.cos(combine.heading or 0.0)
                done = None
                # From behind the combine first, then from its front facing against it (live 7b52570db6ee40d0-1:
                # the lane behind blocked by a dropped seeder, ALONGSIDE_PATH_BLOCKED both times).
                for run_up, way in [(r, 1) for r in self.OVERLOAD_RUN_UPS_M] + [(r, -1) for r in self.OVERLOAD_RUN_UPS_M]:
                    # GIANTS GOTO to the pipe's line, the trailer's middle run_up before the pipe point
                    # (GIANTS will not take a spot right under it); then ALONGSIDE straight forward.
                    ahead = way * (-run_up - behind)
                    aim = Pose(combine.pipe[1] + cx * ahead, combine.pipe[2] + cz * ahead,
                               (combine.heading or 0.0) + (0.0 if way > 0 else math.pi))
                    enter("APPROACH")
                    moved = True
                    try:
                        # Farmhand's own drive onto the run-up at its heading first (live
                        # ae9262cb2492470b-1: GIANTS GOTO ended there turned 178 degrees, the trailer bent 140,
                        # ALONGSIDE_NOT_LINED_UP twice); GIANTS GOTO if it finds no route.
                        if not self._farm_staged(tractor.id, aim, owner):
                            self._navigate(tractor.id, aim, owner, free=False)
                        self._settled_transport(rig_ids)
                        enter("ALONGSIDE")
                        self.world.alongside(tractor.id, combine.id, owner, combine_owner)
                        self._settled_transport(rig_ids)
                        enter("OVERLOAD")
                        done = self.world.overload(combine.id, trailer.id, combine_owner)
                        break
                    except StepFailed as failure:
                        log.info("OVERLOAD run-up %.0f m, aim %s: %s", run_up, aim, failure.code)
                        if not (failure.code.startswith(("ALONGSIDE_", "NAVIGATE_")) or failure.code == "NOT_OVERLOADING"):
                            raise
                        if failure.code == "NOT_OVERLOADING":
                            self.world.pipe(combine.id, True, combine_owner)
                if done is not None:
                    break
            if done is None:
                raise StepFailed("NOT_UNDER_THE_PIPE")
            # On past the combine, the pipe still out (live 15dbb6c7eb994721-1: GIANTS would not
            # start the rig from beside it); a refusal here leaves it where it is.
            enter("PASS")
            try:
                self.world.alongside(tractor.id, combine.id, owner, combine_owner, past=True)
                self._settled_transport(rig_ids)
            except StepFailed as failure:
                log.info("PASS: %s", failure.code)
            enter("PIPE_IN")
            self.world.pipe(combine.id, False, combine_owner)
            piped = False
            enter("VERIFY")
            after = self._at_rest(rig_ids + (combine.id,))
            now = (self._grain(after[combine.id]), sum(u.level or 0.0 for u in after[trailer.id].fill_units))
            log.info("OVERLOAD %s: combine %.0f -> %.0f l, trailer %.0f -> %.0f l", done, before[0], now[0],
                     before[1], now[1])
            if not (now[0] < before[0] and now[1] > before[1]):
                raise ObservationMismatch("GRAIN_NOT_MOVED")
            status, reason = "COMPLETE", "COMBINE_UNLOADED"
        except Cancelled:
            status, reason = "CANCELLED", "CANCELLED_BY_REQUESTER"
        except ObservationMismatch as error:
            status, reason = "UNVERIFIED", str(error)
        except StepUnverified as error:
            status, reason = "UNVERIFIED", error.code
        except StepFailed as error:
            status, reason = "FAILED", error.code
        except (RuntimeError, ValueError, KeyError) as error:
            status, reason = "FAILED", getattr(error, "code", None) or type(error).__name__
        finally:
            try:
                if piped:
                    self.world.pipe(combine_ids[0], False, combine_owner)
                if moved:
                    enter("STOP")
                    if not self.world.stop(rig_ids[0], owner):
                        raise ObservationMismatch("STOP_NOT_CONFIRMED")
                if reserved:
                    enter("RELEASE")
                    for who in reserved:
                        self.world.release(who)
            except Exception as error:
                log.info("cleanup not confirmed: %s %s", type(error).__name__, error)
                status, reason = "UNVERIFIED", "CLEANUP_NOT_CONFIRMED"
        self.active = ()
        log.info("result %s %s states %s", status, reason, ",".join(states))
        return Result(status, reason, rig_ids + combine_ids[:1], tuple(states))

    def _haul_target(self, command):
        """The haul's destination (place or field) as a PlaceTarget, or (None, why)."""
        target = command.get("target")
        if isinstance(target, dict) and target.get("type") == "place":
            return resolve_place(target["name"], self.world.destinations(), self.world.places())
        if isinstance(target, dict) and target.get("type") == "field_entrance":
            number = target["field"]
            marker = next((d for d in self.world.destinations() if isinstance(d, Destination)
                           and _normalized_name(d.name or "") == f"field {number}"), None)
            place = next((p for p in self.world.field_places() if p.id == f"field-{number}"), None)
            if marker is None and place is None:
                return None, "FIELD_NOT_MAPPED"
            return PlaceTarget(f"field {number}", marker, place), None
        return None, "DESTINATION_NOT_FOUND"

    def _onto_ad_route(self, vehicle, partner_id, marker, graph, owner, enter) -> str:
        """The reserved rig onto AutoDrive's network where it has a route to ``marker``: ROUTED
        (already), LINKED (AutoDrive's dead-end lane from here joined to the routed road it stops
        short of, AutoDrive's own way, after a backup; nothing moved) or STAGED (GIANTS GOTO, which
        plans for what is towed, onto the nearest waypoint that has the route, facing along it,
        verified). StepFailed when none of these got it there."""
        if transport.ad_routed(graph, vehicle.position, marker.position):
            return "ROUTED"
        starts = transport.ad_route_starts(graph, vehicle.position, marker.position)
        # The network's own gap first (the user 2026-10-22: configure the existing AutoDrive
        # network by its supported mechanisms): nothing moves.
        enter("AD_LINK")
        try:
            linked = self.world.ad_connect(vehicle.id, marker.id, owner)
            log.info("STAGE: AutoDrive network %s", linked)
            return "LINKED"
        except StepFailed as failure:
            log.info("STAGE: no AutoDrive link to make (%s); GIANTS GOTO to a routed waypoint", failure.code)
        if not starts:
            raise StepFailed("AD_NO_ROUTED_WAYPOINT_NEAR")
        for n, ((x, z), yaw, wp) in enumerate(starts, 1):
            log.info("STAGE %d: no AutoDrive route from %s to %s; to waypoint %s at (%.1f, %.1f), %.0f m off", n,
                     vehicle.position, marker.name, wp, x, z, math.dist(vehicle.position, (x, z)))
            enter("STAGE")
            try:
                self._navigate(vehicle.id, Pose(x, z, yaw), owner, partner_id)
                break
            except StepFailed as failure:
                # GIANTS could not plan or drive there (live: its agent planning 96 s, 72 m off):
                # the next routed waypoint.
                if n == len(starts) or not failure.code.startswith(("NAVIGATE_", "GOTO_")):
                    raise
                log.info("STAGE %d: %s", n, failure.code)
        enter("VERIFY")
        after = self._at_rest((vehicle.id, partner_id))
        if not transport.ad_routed(graph, after[vehicle.id].position, marker.position):
            raise ObservationMismatch("STAGE_NOT_ROUTED")
        return "STAGED"

    def _stage_transport(self, command, enter, states) -> Result:
        """Before a transport is loaded: AutoDrive will drive the loaded rig from the network
        waypoint nearest it, and from a dead end it cannot (live 2026-10-22: the waypoint nearest
        the GMC was on a 7-waypoint stub, "Could not calculate path"). Its saved network
        (companion/hitch.py, read only) says whether there is a route to the haul's marker
        (made AutoDrive's own way when missing); if not, GIANTS GOTO takes the empty transport
        (truck and trailer, nothing on it: GOTO drives that) onto the nearest waypoint that has
        one, facing along it. FIND -> (ROUTED, nothing moved) or RESERVE -> AD_LINK (AutoDrive's
        dead-end lane from here joined to the routed road it stops short of: LINKED, nothing
        moved) or STAGE (GOTO) -> VERIFY (the nearest waypoint now has the route) -> STOP ->
        RELEASE. BLOCKED, nothing moved, when there is no AutoDrive or no network to read."""
        owner, farm_id = command["request_id"], command["farm_id"]
        ids, reserved, moved = (), False, False
        status, reason = "BLOCKED", "NO_TRANSPORT"
        try:
            enter("FIND")
            items = sorted(self.world.snapshot(), key=lambda i: i.id)
            observed = {i.id: i for i in items}
            rigs = [(t, observed[t.attached_to]) for t in items
                    if t.attached_to in observed and transport.is_transport(observed[t.attached_to])
                    and observed[t.attached_to].attached_to == t.id and named(t, command.get("tractor_name"))
                    and named_trailer(observed[t.attached_to], command.get("implement_name"))
                    and all(usable(x, farm_id) and x.stopped for x in (t, observed[t.attached_to]))
                    and not any(i.belted_to == t.attached_to for i in items)]
            if not rigs:
                return Result("BLOCKED", "NO_TRANSPORT", states=tuple(states))
            truck, trailer = rigs[0]
            ids = (truck.id, trailer.id)
            if "autodrive" not in truck.capabilities:
                return Result("BLOCKED", "LOADED_NEEDS_AUTODRIVE", ids, tuple(states))
            target, failure = self._haul_target(command)
            if target is None:
                return Result("BLOCKED", failure, ids, tuple(states))
            marker = target.marker
            if marker is None:
                marker, failure = self._place_marker(target, truck)
                if marker is None:
                    return Result("BLOCKED", failure, ids, tuple(states))
            graph = self.world.ad_graph()
            if graph is None:
                return Result("BLOCKED", "AD_NETWORK_UNREADABLE", ids, tuple(states))
            routed = transport.ad_routed(graph, truck.position, marker.position)
            if routed is None:
                return Result("BLOCKED", "AD_MARKER_NOT_ON_NETWORK", ids, tuple(states))
            if routed:
                return Result("COMPLETE", "ROUTED", ids, tuple(states))
            enter("RESERVE")
            if not self.world.reserve(ids, owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", ids, tuple(states))
            reserved = True
            self.active = ids
            moved = True                       # GIANTS GOTO may drive (STOP confirms it at rest)
            reason = self._onto_ad_route(truck, trailer.id, marker, graph, owner, enter)
            status = "COMPLETE"
        except Cancelled:
            status, reason = "CANCELLED", "CANCELLED_BY_REQUESTER"
        except ObservationMismatch as error:
            status, reason = "UNVERIFIED", str(error)
        except StepUnverified as error:
            status, reason = "UNVERIFIED", error.code
        except StepFailed as error:
            status, reason = "FAILED", error.code
        except (RuntimeError, ValueError, KeyError) as error:
            status, reason = "FAILED", getattr(error, "code", None) or type(error).__name__
        finally:
            if reserved:
                try:
                    if moved:
                        enter("STOP")
                        if not self.world.stop(ids[0], owner):
                            raise ObservationMismatch("STOP_NOT_CONFIRMED")
                    enter("RELEASE")
                    self.world.release(owner)
                except Exception as error:
                    log.info("cleanup not confirmed: %s %s", type(error).__name__, error)
                    status, reason = "UNVERIFIED", "CLEANUP_NOT_CONFIRMED"
        self.active = ()
        log.info("result %s %s states %s", status, reason, ",".join(states))
        return Result(status, reason, ids, tuple(states))

    PART_NEAR_M = 12.0        # a trailer rig this near a combine blocks GIANTS' agent for both

    def _part(self, command, enter, states) -> Result:
        """A trailer rig left beside a combine (GIANTS' agent then starts neither; live
        931324a7d25a48d7-1, FIELDWORK_STALLED "blocked"): the pipe out, the rig on past the
        combine along the pipe's line (ALONGSIDE, AGENTS.md exception 6), the pipe in.
        COMPLETE PARTED, or NOTHING_TO_PART with nothing moved."""
        owner, farm_id = command["request_id"], command["farm_id"]
        combine_owner = f"{owner}-c"
        reserved, piped, moved, rig_ids, combine_ids = [], False, False, (), ()
        status, reason = "BLOCKED", "NOTHING_TO_PART"
        try:
            enter("FIND")
            items = sorted(self.world.snapshot(), key=lambda i: i.id)
            observed = {i.id: i for i in items}
            pairs = []
            for combine in items:
                if not (combine.category == "harvester" and combine.pipe is not None and combine.attached_to
                        and usable(combine, farm_id) and combine.stopped):
                    continue
                for tractor in items:
                    trailer = observed.get(tractor.attached_to)
                    if (tractor.category == "tractor" and trailer is not None and trailer.attached_to == tractor.id
                            and "trailer" in trailer.kinds and all(usable(x, farm_id) and x.stopped for x in (tractor, trailer))):
                        near = min(math.dist(x.position, combine.position) for x in (tractor, trailer))
                        if near <= self.PART_NEAR_M:
                            pairs.append((near, combine, tractor, trailer))
            if not pairs:
                return Result("BLOCKED", reason, states=tuple(states))
            _, combine, tractor, trailer = min(pairs, key=lambda p: (p[0], p[1].id, p[2].id))
            rig_ids, combine_ids = (tractor.id, trailer.id), (combine.id, combine.attached_to)
            enter("RESERVE")
            if not self.world.reserve(combine_ids, combine_owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", (combine.id,), tuple(states))
            reserved.append(combine_owner)
            if not self.world.reserve(rig_ids, owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", rig_ids, tuple(states))
            reserved.append(owner)
            self.active = rig_ids
            enter("PIPE_OUT")
            self.world.pipe(combine.id, True, combine_owner)
            piped = True
            enter("PASS")
            moved = True
            self.world.alongside(tractor.id, combine.id, owner, combine_owner, past=True)
            self._settled_transport(rig_ids)
            enter("PIPE_IN")
            self.world.pipe(combine.id, False, combine_owner)
            piped = False
            status, reason = "COMPLETE", "PARTED"
        except Cancelled:
            status, reason = "CANCELLED", "CANCELLED_BY_REQUESTER"
        except ObservationMismatch as error:
            status, reason = "UNVERIFIED", str(error)
        except StepUnverified as error:
            status, reason = "UNVERIFIED", error.code
        except StepFailed as error:
            status, reason = "FAILED", error.code
        except (RuntimeError, ValueError, KeyError) as error:
            status, reason = "FAILED", getattr(error, "code", None) or type(error).__name__
        finally:
            try:
                if piped:
                    self.world.pipe(combine_ids[0], False, combine_owner)
                if moved:
                    enter("STOP")
                    if not self.world.stop(rig_ids[0], owner):
                        raise ObservationMismatch("STOP_NOT_CONFIRMED")
                if reserved:
                    enter("RELEASE")
                    for who in reserved:
                        self.world.release(who)
            except Exception as error:
                log.info("cleanup not confirmed: %s %s", type(error).__name__, error)
                status, reason = "UNVERIFIED", "CLEANUP_NOT_CONFIRMED"
        self.active = ()
        log.info("result %s %s states %s", status, reason, ",".join(states))
        return Result(status, reason, rig_ids + combine_ids[:1], tuple(states))

    def _transport_pair(self, ids):
        observed = {i.id: i for i in self.world.snapshot()}
        if any(i not in observed for i in ids):
            raise ObservationMismatch("EQUIPMENT_MISSING")
        return tuple(observed[i] for i in ids)

    def _settled_transport(self, ids):
        observed = self._at_rest(ids)
        if any(i not in observed for i in ids):
            raise ObservationMismatch("EQUIPMENT_MISSING")
        if not observed[ids[0]].stopped:
            raise ObservationMismatch("EQUIPMENT_MOVING")
        return observed[ids[0]], observed[ids[1]]

    def _ramps(self, trailer_id, direction, owner, state):
        """The trailer's ramps (GIANTS Foldable) to one end, verified: fold time 1 down, 0 up."""
        trailer = self._transport_pair((trailer_id,))[0]
        want = 1.0 if direction > 0 else 0.0
        if trailer.fold == want and not trailer.folding:
            return
        self._enter(state)
        self.world.fold(trailer_id, direction, owner)
        after = self._transport_pair((trailer_id,))[0]
        if after.fold is None or abs(after.fold - want) > 1e-3 or after.folding:
            raise ObservationMismatch("RAMPS_NOT_OBSERVED")
        log.info("%s: trailer %s fold %.2f", state, trailer_id, after.fold)

    def _back_off_and_run_up(self, tractor, trailer, owner):
        """BACKOFF (AGENTS.md exception 5) straight back from the ramps, once per approach,
        then the run-up again; refusals are logged and the tractor stays where it is."""
        self._backed_off = True
        try:
            self._enter("BACKOFF")
            self.world.backoff(tractor.id, trailer.id, owner)
            tractor, trailer = self._settled_transport((tractor.id, trailer.id))
            if transport.can_run_in(tractor, trailer):
                return tractor, trailer
            run_up = transport.load_aims(tractor, trailer, (transport.RUN_UP_GAP_M,))[0]
            self._navigate(tractor.id, run_up, owner, free=False)
        except StepFailed as again:
            log.info("APPROACH after BACKOFF: %s", again.code)
        return self._settled_transport((tractor.id, trailer.id))

    def _approach(self, tractor, trailer, owner):
        """GIANTS GOTO to right behind the ramps; the first aim GIANTS reaches and that
        lines the tractor up for LOAD, after a run-up straight behind them (where GIANTS
        refuses it, straight on; waiting right behind the ramps not lined up, BACKOFF once).
        LOAD itself re-checks the window."""
        self._backed_off = False
        if transport.can_run_in(tractor, trailer):
            log.info("APPROACH: LOAD can start where it stands")
            return
        if transport.close_behind(tractor, trailer):
            # Right behind the ramps, outside the run-in: straight back first (live
            # 6601d036cf7b4334-1-h2: GIANTS did not start it at the ramps' foot and UNPARK, taking
            # something static over it for a roof, drove it 1 m on towards them).
            tractor, trailer = self._back_off_and_run_up(tractor, trailer, owner)
            if transport.can_run_in(tractor, trailer):
                log.info("APPROACH: in LOAD's run-in window after BACKOFF")
                return
        for run_up in transport.load_aims(tractor, trailer, (transport.RUN_UP_GAP_M,)):
            try:
                self._navigate(tractor.id, run_up, owner, free=True)
                tractor, trailer = self._settled_transport((tractor.id, trailer.id))
            except StepFailed as failure:
                log.info("APPROACH run-up %s: %s", run_up, failure.code)
                if failure.code not in START_BLOCKED_CODES and failure.code != "UNPARK_ALREADY_USED":
                    continue
                # Waiting right behind the ramps, not lined up, and GIANTS will not start it there
                # (live 49b5344a47ec418a-1): straight back (BACKOFF), then the run-up again.
                tractor, trailer = self._back_off_and_run_up(tractor, trailer, owner)
        tractor, trailer = self._settled_transport((tractor.id, trailer.id))
        if transport.can_run_in(tractor, trailer):
            log.info("APPROACH: in LOAD's run-in window; LOAD lines it up")
            return
        if not self._backed_off and transport.close_behind(tractor, trailer):
            # Live 15fc3a8297604156-1: after UNPARK the Series 9 stood right behind the ramps,
            # 0.35 m off, 15.6 deg turned, and GIANTS started it nowhere closer: BACKOFF.
            tractor, trailer = self._back_off_and_run_up(tractor, trailer, owner)
            if transport.can_run_in(tractor, trailer):
                log.info("APPROACH: in LOAD's run-in window after BACKOFF")
                return
        for aim in transport.load_aims(tractor, trailer):
            try:
                self._navigate(tractor.id, aim, owner, free=True)
            except StepFailed as failure:
                log.info("APPROACH aim %s: %s", aim, failure.code)
                continue
            tractor, trailer = self._settled_transport((tractor.id, trailer.id))
            a, l = transport.to_frame(trailer, tractor.position)
            log.info("APPROACH end: ai_root %s, along %.2f lateral %.2f heading %.1f deg off the trailer",
                     tractor.ai_root, a, l, math.degrees(_angle(tractor.heading or 0.0, trailer.heading)))
            if transport.lined_up(tractor, trailer, share=0.9) or transport.can_run_in(tractor, trailer):
                return
        raise StepFailed("LOAD_APPROACH_FAILED")

    def _field(self, field_id):
        """The field's latest measured state (the mod measures it again at the start
        and the end of every field-work job)."""
        field = next((f for f in self.world.fields() if f.id == field_id), None)
        if field is None:
            raise ObservationMismatch("FIELD_NOT_OBSERVED")
        return field

    def _helper_buys(self, operation) -> bool:
        """GIANTS' helper buys this material itself (missionInfo helperBuySeeds /
        helperBuyFertilizer; SowingMachine 510-519, Sprayer 558-581)."""
        farm = self.world.farm()
        if farm is None:
            return False
        return {"seed": farm.helper_buy_seeds, "spray": farm.helper_buy_fertilizer}.get(operation.material) is True

    def _supply_problem(self, implement, operation):
        """BLOCKED before reserving when the implement has none of the material and
        GIANTS' helper will not buy it (it would end OUT_OF_FILL at once)."""
        if operation.material is None:
            return None
        unit = _material(implement, operation.material)
        if unit is None:
            return "MATERIAL_UNKNOWN"
        if (unit.level or 0) > 0 or self._helper_buys(operation):
            return None
        return {"seed": "NEEDS_SEED", "spray": "NEEDS_FERTILIZER"}[operation.material]

    def _tailgate_down(self, truck: Equipment, joint_index: int, owner: str) -> None:
        """A regular bed under a gooseneck (the user's completion plan, stage 3): the truck's
        GIANTS Foldable part (its tailgate) goes down and is verified down from a fresh
        snapshot before the gooseneck comes over it or leaves it; it stays down while
        hitched, as clearance is never assumed. Nothing to do without a gooseneck joint
        or a Foldable part, or when it is already down (fold time 1)."""
        joint = next((j for j in truck.output_joints if j.index == joint_index), None)
        if joint is None or "semitrailer" not in (joint.joint_type or "").lower() or truck.fold is None \
                or (truck.fold >= 1.0 and not truck.folding):
            return
        self._enter("TAILGATE")
        self.world.fold(truck.id, 1, owner)
        after = next((i for i in self.world.snapshot() if i.id == truck.id), None)
        if after is None or after.fold is None or after.fold < 1.0 or after.folding:
            raise ObservationMismatch("TAILGATE_NOT_OBSERVED")
        log.info("TAILGATE: %s down (fold %.2f) for its gooseneck joint %d", truck.id, after.fold, joint_index)

    def _position(self, vehicle_id) -> Position:
        vehicle = next((i for i in self.world.snapshot() if i.id == vehicle_id), None)
        if vehicle is None or not valid_position(vehicle.position):
            raise ObservationMismatch("EQUIPMENT_MISSING")
        return vehicle.position

    def _goto_moved(self, vehicle_id, start) -> bool:
        """Whether a GOTO GIANTS took moved the vehicle; the hand-off after UNPARK must."""
        moved = math.dist(start, self._position(vehicle_id)) >= GOTO_MOVED_M
        if vehicle_id in self._handoff:
            self._handoff.discard(vehicle_id)
            if not moved:
                log.info("UNPARK hand-off: GIANTS still does not start %s from the exit", vehicle_id)
                raise StepFailed("UNPARK_HANDOFF_BLOCKED")
        return moved

    def _navigate(self, tractor_id, pose, owner, partner_id=None, free=True):
        """GIANTS GOTO to ``pose``, routing around what physically stops it (see
        MAX_DETOURS): the stuck spot just ahead of the vehicle is blocked for GIANTS'
        planner and the same GOTO asked again. With ``free``, a GOTO GIANTS took that
        could not start the vehicle from where it stands is answered by UNPARK once
        (_free_start) and asked again."""
        detours = 0
        while True:
            start = self._position(tractor_id)
            failed = None
            try:
                self.world.navigate(tractor_id, pose, owner)
            except StepFailed as failure:
                if failure.code == "NAVIGATE_STUCK" and detours < MAX_DETOURS:
                    self._block_ahead(tractor_id, detours)
                    detours += 1
                    continue
                failed = failure
            moved = self._goto_moved(tractor_id, start)
            vehicle = next(i for i in self.world.snapshot() if i.id == tractor_id)
            if not free or moved or (failed is None and self._at(vehicle, pose)):
                if failed is not None:
                    raise failed
                return
            if failed is not None and failed.code not in START_BLOCKED_CODES:
                raise failed
            log.info("navigate to %s: GIANTS did not start %s from %s (%s)", pose, tractor_id, start,
                     failed.code if failed is not None else "ended unmoved")
            if self._free_start(tractor_id, owner) is None:
                if failed is not None:
                    raise failed
                return                     # ended: the caller's observation decides, as before

    def _free_start(self, vehicle_id, owner):
        """GIANTS took a target but cannot start ``vehicle_id`` from where it stands. Once
        per vehicle per job, UNPARK takes it (and what is attached) to the nearest exit
        ("UNPARKED"), verified at rest and within its cap, and its next GOTO is the
        hand-off. None when UNPARK finds it not boxed in (GIANTS refused for another
        reason: never compensated). UNPARK's refusals (a closed door near it among them)
        and failures raise; so does a spent budget (UNPARK_ALREADY_USED)."""
        if vehicle_id in self._unparked:
            raise StepFailed("UNPARK_ALREADY_USED")
        self._unparked.add(vehicle_id)
        start = self._position(vehicle_id)
        self._enter("UNPARK")
        try:
            self.world.unpark(vehicle_id, owner)
        except StepFailed as failure:
            if failure.code == "UNPARK_NOT_BOXED":
                log.info("UNPARK: %s stands in the open; GIANTS refused for another reason", vehicle_id)
                return None
            if failure.code in UNPARK_REFUSALS:
                log.info("UNPARK refused unmoved (%s)", failure.code)
            raise
        # At rest first (live c2316ec3fbb54123-1: braked from 1.78 km/h, the snapshot right after
        # still showed it rolling, UNPARK_NOT_OBSERVED after a 1.01 m exit).
        vehicle = self._at_rest((vehicle_id,)).get(vehicle_id)
        if (vehicle is None or not vehicle.stopped or not valid_position(vehicle.position)
                or not UNPARK_MIN_M <= math.dist(start, vehicle.position) <= UNPARK_MAX_M + UNPARK_TOLERANCE_M):
            raise ObservationMismatch("UNPARK_NOT_OBSERVED")
        log.info("UNPARK end: %s from %s to %s (%.1f m), at rest", vehicle_id, start, vehicle.position,
                 math.dist(start, vehicle.position))
        self._handoff.add(vehicle_id)
        return "UNPARKED"

    def _block_ahead(self, tractor_id, detour):
        """Something physical holds the vehicle while GIANTS drives: a closed gate near
        it is opened as GIANTS opens one; with none, the ground just ahead is marked
        impassable for GIANTS so it routes around."""
        vehicle = next((i for i in self.world.snapshot() if i.id == tractor_id), None)
        if vehicle is None or vehicle.heading is None or not valid_position(vehicle.position):
            raise StepFailed("NAVIGATE_STUCK")
        try:
            self.world.open_gates(vehicle.position[0], vehicle.position[1], GATE_RADIUS_M)
            log.info("stuck at %s: opened the closed gate(s) within %.0f m, retry %d", vehicle.position,
                     GATE_RADIUS_M, detour + 1)
            return
        except StepFailed as failure:
            if failure.code != "NO_CLOSED_GATE":
                raise
        # No gate: something GIANTS' map does not show holds it (live 2026-10-20: a
        # fence beside field 3). Marking ground just ahead does not help a vehicle pressed
        # against it (GIANTS' agent cannot reverse and then stays BLOCKED by the mark),
        # so nothing is marked: the job ends with the precise reason.
        log.info("stuck at %s on something GIANTS' map does not show (no gate near)", vehicle.position)
        raise StepFailed("STUCK_ON_OBSTACLE")


    def _farm_staged(self, tractor_id: str, pose: Pose, owner: str) -> bool:
        """STAGE onto an approach by Farmhand's own drive, at its heading, where there is one:
        True once it arrived; False (nothing driven, or no route) for GIANTS' stage."""
        if not self._farm_drives():
            return False
        # The aim is a pose of GIANTS' AI root (what GOTO steers); Farmhand's drive steers the
        # vehicle's root, metres from it along its heading (live ae9262cb2492470b-1: the trailer
        # 25.8 m from the pipe, over ALONGSIDE's 25 m). The same offset, at the aim's heading.
        target = (pose.x, pose.z)
        tractor = next((i for i in self.world.snapshot() if i.id == tractor_id), None)
        if tractor is not None and tractor.ai_root is not None and tractor.heading is not None and math.isfinite(tractor.heading):
            hx, hz = math.sin(tractor.heading), math.cos(tractor.heading)
            ahead = (tractor.ai_root.x - tractor.position[0]) * hx + (tractor.ai_root.z - tractor.position[1]) * hz
            if math.isfinite(ahead) and abs(ahead) < 10:
                target = (pose.x - math.sin(pose.yaw) * ahead, pose.z - math.cos(pose.yaw) * ahead)
        # Towing, the last STRAIGHT_LEAD_M come straight along the aim's heading, so what it tows
        # falls in line behind it (live b46c3abddb2b4cee-1: at the run-up the tractor at its heading, the
        # trailer bent 50-69 degrees, ALONGSIDE_RIG_NOT_STRAIGHT). Without room for that run-in,
        # the aim directly.
        towing = any(i.attached_to == tractor_id for i in self.world.snapshot())
        if towing:
            lead = (target[0] - math.sin(pose.yaw) * STRAIGHT_LEAD_M, target[1] - math.cos(pose.yaw) * STRAIGHT_LEAD_M)
            try:
                self.world.farm_drive(tractor_id, lead, owner, 0.0, pose.yaw)
            except StepFailed as failure:
                log.info("STAGE aim %s: no straight run-in (%s); the aim directly", pose, failure.code)
                self._standing(tractor_id)
        try:
            self.world.farm_drive(tractor_id, target, owner, 0.0, pose.yaw)
        except StepFailed as failure:
            log.info("STAGE aim %s by Farmhand's own drive: %s; GIANTS stages", pose, failure.code)
            self._standing(tractor_id)
            return False
        return True

    def _standing(self, vehicle_id: str) -> None:
        """Until a drive that just ended stands, before the next try (live 46b3e43ac1da4902-1: the
        retry and GIANTS' stage both refused NOT_STOPPED while it rolled to a stop)."""
        if hasattr(self.world, "await_stopped"):
            self.world.await_stopped(vehicle_id)

    def _farm_drives(self) -> bool:
        """The world has Farmhand's own drive (the game link; not every test world)."""
        return getattr(type(self.world), "farm_drive", None) is not None

    def _move(self, tractor_id, route, owner, deliver=False, partner_id=None):
        """One travel step; returns the route its arrival is checked against. A delivery
        (the pair to the player, a place, back where it came from) is Farmhand's own drive
        of the reserved rig, checked at its root (AGENTS.md 7: a provider failure is not
        the destination's); only if that ends unreached the route's provider is tried:
        AutoDrive to a marker, or GIANTS GOTO to a pose."""
        if deliver and self._farm_drives():
            target = tuple(route_position(route))
            try:
                self.world.farm_drive(tractor_id, target, owner, 0.0)
                return Destination("farmhand", target)
            except StepFailed as failure:
                log.info("Farmhand's own drive to %s ended %s; the route's provider is tried", target, failure.code)
        if isinstance(route, Pose):
            self._navigate(tractor_id, route, owner, partner_id)
        elif deliver:
            self.world.deliver(tractor_id, route.id, owner)
        else:
            self.world.travel(tractor_id, route.id, owner)
        return route

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

    def _at_rest(self, ids):
        """A fresh snapshot by ID once every unit is at rest, waiting at most
        ARRIVAL_SETTLE_S: a trailer still settles after GIANTS' attach (live
        d6701ca959d044c0-1: the Deckover's nose pulled 0.44 m onto the ball)."""
        deadline = time.monotonic() + ARRIVAL_SETTLE_S
        while True:
            observed = {item.id: item for item in self.world.snapshot()}
            if all(i in observed and observed[i].stopped for i in ids) or time.monotonic() >= deadline:
                return observed
            time.sleep(0.5)

    def _arrival(self, ids, farm_id, owner, route, check=None):
        # GIANTS ends GOTO while the tractor still rolls (live 84f4df027cb445bc-3:
        # 0.4 m from the pose, moving 0.17 s after GOTO_ENDED): at rest first, bounded.
        deadline = time.monotonic() + ARRIVAL_SETTLE_S
        tractor, wagon = self._pair(ids, farm_id, owner, attached=True, check=check)
        while not (tractor.stopped and wagon.stopped) and time.monotonic() < deadline:
            time.sleep(0.5)
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

    def _hitched_rig(self, command):
        """The one tractor already towing the implement a fetch names (its kind, or its
        name; the tractor's name when given), or None. That assembled rig is the
        equipment the player means (AGENTS.md 8: preserve it), never a new pair: live
        a852385ab373498b-2, "take the 3650 and planter to field 4" was NO_ELIGIBLE_PAIR,
        every planter and tractor being hitched already."""
        target = command.get("target")
        if not (target == "player_at_acceptance" or (isinstance(target, dict) and target.get("type") in (
                "place", "field_entrance", "autodrive_marker"))):
            return None
        try:
            items = tuple(self.world.snapshot())
        except (StepFailed, StepUnverified):
            return None                            # the ordinary flow observes again and says why
        observed = {i.id: i for i in items}
        kind, name = command.get("implement", "forage_wagon"), command.get("implement_name")
        rigs = []
        for tractor in items:
            implement = observed.get(tractor.attached_to) if tractor.attached_to else None
            if (tractor.category != "tractor" or tractor.farm_id != command["farm_id"] or implement is None
                    or implement.attached_to != tractor.id or implement.category not in IMPLEMENT_CATEGORIES):
                continue
            if not (named(implement, name) if name else of_kind(implement, kind)):
                continue
            if command.get("tractor_name") and not named(tractor, command["tractor_name"]):
                continue
            rigs.append(tractor.id)
        return rigs[0] if len(rigs) == 1 else None

    def _run(self, command, cancel=None, on_state=None):
        if command["task"] == "move_pair" and command.get("tractor_name"):
            from companion.standalone import StandaloneMove
            return StandaloneMove(self.world).execute(command, cancel, on_state)
        if command["task"] == "deliver_tractor_and_forage_wagon" and getattr(type(self.world), "farm_drive", None):
            vehicle = self._hitched_rig(command)
            if vehicle is not None:
                from companion.standalone import StandaloneMove
                log.info("the named implement is hitched to %s: that rig is moved", vehicle)
                return StandaloneMove(self.world).execute({**command, "task": "move_pair", "vehicle_id": vehicle},
                                                          cancel, on_state)
        owner, farm_id = command["request_id"], command["farm_id"]
        states, ids = [], ()

        def enter(state):
            if state in CANCELLABLE_STEPS and cancel is not None and cancel.is_set():
                raise Cancelled()
            states.append(state)
            if on_state is not None:
                on_state(state)
        self._enter = enter

        if command["task"] == "buy":
            return self._buy(command, enter, states)
        if command["task"] == "land":
            return self._land(command, enter, states)
        if command["task"] in ("load_tractor", "unload_tractor"):
            return self._transport(command, enter, states)
        if command["task"] == "follow_me":
            return self._follow(command, enter, states)
        if command["task"] in ("unload_harvester", "sell", "store"):
            return self._autodrive_rig(command, enter, states)
        if command["task"] == "overload":
            return self._overload(command, enter, states)
        if command["task"] == "part":
            return self._part(command, enter, states)
        if command["task"] == "stage_transport":
            return self._stage_transport(command, enter, states)
        if command["task"] == "save_game":
            try:
                enter("SAVE")
                self.world.save_game()            # DONE only on a changed save stamp
                return Result("COMPLETE", "SAVED", states=tuple(states))
            except StepUnverified as error:
                return Result("UNVERIFIED", error.code, states=tuple(states))
            except (RuntimeError, ValueError) as error:
                return Result("FAILED", getattr(error, "code", None) or type(error).__name__, states=tuple(states))

        reserved = physical_attempted = already_there = handed = False
        status, reason = "BLOCKED", "NO_ELIGIBLE_PAIR"
        moving = command["task"] == "move_pair"
        detaching = command["task"] == "detach_pair"
        working = command["task"] == "field_work"
        operation = OPERATIONS.get(command.get("operation")) if working else None
        hitch_only = command["task"] == "hitch_pair"
        # One implement kind per job; every pair check of this job uses it. A detach
        # without a named kind takes any implement.
        if working:
            kinds = (command["implement"],) if command.get("implement") else operation.kinds
            self._kind = kinds[0]
            self._compatible = functools.partial(workable, kinds=kinds, crop=command.get("crop"))
        elif detaching:
            self._kind = command.get("implement")
            self._compatible = functools.partial(detachable, kind=self._kind)
        else:
            self._kind = command.get("implement", "forage_wagon")
            self._compatible = functools.partial(compatible, kind=self._kind, name=command.get("implement_name"),
                                                 crop=command.get("crop"))
        check = movable if moving else self._compatible
        delivery = None
        try:
            # Every destination is resolved and validated before any reservation.
            requester = None
            to_origin = isinstance(command["target"], dict) and command["target"]["type"] == "origin"
            if (command["target"] == "player_at_acceptance" or moving or detaching) and not working:
                # move_pair and detach_pair always need the requester: "this" is the pair nearest them.
                requester = self.world.player_position(farm_id)
                if not valid_position(requester):
                    return Result("BLOCKED", "INVALID_REQUESTER_POSITION")
                requester = tuple(requester)
                log.info("requester at %s", requester)
            if working:
                enter("FIND")
                field, failure = resolve_field(command["field"], self.world.fields())
                if field is None:
                    return Result("BLOCKED", failure, states=tuple(states))
                target = field.center
                log.info("field %s: %.2f ha at %s, before: %s", field.id, field.area_ha, field.center,
                         describe(field.coverage))
                if already_done(operation, field, command.get("area_m2")):
                    # Nothing to do (live: every owned field was already 100 % CULTIVATED):
                    # said so, nothing moved.
                    return Result("COMPLETE", "ALREADY_DONE", states=tuple(states))
                failure = None if command.get("over_crop") else crop_problem(operation, field)
                if failure is not None:
                    return Result("BLOCKED", failure, states=tuple(states))
            elif command["target"] == "player_at_acceptance" or to_origin:
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
                if destination is None and failure in ("FIELD_NOT_MAPPED", "MAP_UNKNOWN"):
                    # Any map: an AutoDrive marker named "field N" (AutoDrive's own name for it),
                    # else GIANTS' own field position by GOTO.
                    number = command["target"]["field"]
                    named_marker = [d for d in self.world.destinations() if isinstance(d, Destination)
                                    and _normalized_name(d.name or "") == f"field {number}"]
                    place = next((p for p in self.world.field_places() if p.id == f"field-{number}"), None)
                    if len(named_marker) > 1:
                        return Result("BLOCKED", "DESTINATION_AMBIGUOUS", states=tuple(states))
                    if named_marker and valid_position(named_marker[0].position):
                        target = PlaceTarget(f"field {number}", named_marker[0], place)
                        log.info("field %s: AutoDrive marker %s %r", number, named_marker[0].id, named_marker[0].name)
                    elif place is None:
                        return Result("BLOCKED", failure, states=tuple(states))
                    else:
                        target = PlaceTarget(place.name, None, place)
                        log.info("field %s at %s (GIANTS' field position)", number, place.position)
                elif destination is None:
                    return Result("BLOCKED", failure, states=tuple(states))
                else:
                    target = destination
                    log.info("destination %s %r at %s", destination.id, destination.name, destination.position)
            # The tractor the requester rides comes first; then the opaque-ID order.
            items = sorted(self.world.snapshot(), key=lambda item: (not item.ridden, item.id))
            if moving:
                pair, delivery, failure = self._choose_hitched(items, farm_id, requester, target,
                                                               self.world.destinations(),
                                                               self._origins if to_origin else None,
                                                               command.get("implement"), command.get("tractor_name"),
                                                               command.get("cargo_name"), command.get("implement_name"))
                plan = travel = None
                aims = ()
            elif detaching or working:
                pair, failure = self._choose_attached(items, farm_id, requester or target,
                                                      command.get("tractor_name") if detaching else None)
                plan = travel = None
                aims = ()
                if working and pair is not None:
                    failure = self._supply_problem(pair[1], operation)
                    if failure is not None:
                        return Result("BLOCKED", failure, tuple(i.id for i in pair), tuple(states))
            else:
                free = [item for item in items if usable(item, farm_id)
                        and item.attached_to is None and item.stopped and item.id not in self._skip
                        and named(item, command.get("tractor_name") if item.category == "tractor"
                                  else command.get("implement_name"))]
                named_tractors = [i for i in free if i.category == "tractor"]
                if command.get("tractor_name") and len(named_tractors) == 1:
                    # The named tractor to the nearest of the implements the words fit (live
                    # 4c41d6e784cc445e-1: four trailers of one name, the first by ID chosen while
                    # the 3650 stood on another's joint); ties by opaque ID.
                    # Nearest by the joints (live c06633894f8c4c51-1: by the roots another
                    # trailer than the one the 3650 stood on), else by the roots.
                    named_tractor = named_tractors[0]

                    def nearness(i):
                        gap = joint_separation(named_tractor, i) if i.category != "tractor" else None
                        return gap if gap is not None else math.dist(named_tractor.position, i.position)
                    free.sort(key=lambda i: (i.category != "tractor", nearness(i), i.id))
                for item in items:
                    if item not in free and (item.category == "tractor" or of_kind(item, self._kind)):
                        log.info("candidate %s %s rejected: %s", item.id, item.category,
                                 ", ".join(rejections(item, farm_id)) or "UNKNOWN")
                pair, plan, travel, delivery, failure = self._choose(free, self.world.destinations(), target, items)
                plan, aims = plan or (None, ())
                self._chosen = {i.id: i.position for i in pair or ()}
            if pair is None:
                return Result(status, failure, states=tuple(states))
            ids = tuple(item.id for item in pair)
            if moving:
                log.info("moving hitched tractor %s wagon %s to %s", *ids, delivery)
            elif detaching:
                log.info("detaching implement %s from tractor %s", ids[1], ids[0])
            elif working:
                log.info("field work %s on field %s: tractor %s implement %s", operation.name, field.id, *ids)
            else:
                log.info("selected tractor %s wagon %s joints %d/%d approaches %s travel %s delivery %s", *ids,
                         plan.tractor_joint, plan.wagon_joint, list(aims), travel, delivery)
            enter("RESERVE")
            if not self.world.reserve(ids, owner):
                return Result("BLOCKED", "RESERVATION_CONFLICT", ids, tuple(states))
            reserved = True
            self.active = ids                     # observed progress (Coordinator.status)
            if moving:
                tractor, wagon = self._pair(ids, farm_id, owner, attached=True, check=movable)
                if not tractor.stopped:
                    raise ObservationMismatch("EQUIPMENT_MOVING")
                # Where it came from, for "bring it back" (unless Farmhand took it from elsewhere).
                self._origins.setdefault(wagon.id, _pose_of(tractor))
                if (any(i.belted_to == wagon.id for i in self.world.snapshot())
                        and math.dist(tractor.position, route_position(delivery)) <= ARRIVED_BY_HAND_M):
                    log.info("loaded rig already at %s (driven there): no drive", delivery)
                    already_there = True
                else:
                    physical_attempted = True
                    enter("DELIVER")
                    try:
                        # The route its arrival is checked against, here and at the end (_move).
                        delivery = self._move(tractor.id, delivery, owner, deliver=True, partner_id=ids[1])
                    except StepFailed as failure:
                        if failure.code in AD_STALLS and any(i.belted_to == wagon.id for i in self.world.snapshot()):
                            # AutoDrive did not move the loaded rig: the player drives it there.
                            self._ad_stalled[wagon.id] = failure.code
                        raise
                    self._arrival(ids, farm_id, owner, delivery, check=movable)
            elif working:
                # FIELD_WORK -> VERIFY: GIANTS' field-work helper drives to the field and
                # works it; complete only when the field's own measured state shows the
                # operation over COVERAGE_REQUIRED of its area, and the material went.
                tractor, implement = self._pair(ids, farm_id, owner, attached=True)
                if not tractor.stopped:
                    raise ObservationMismatch("EQUIPMENT_MOVING")
                self._origins.setdefault(implement.id, _pose_of(tractor))
                before = self._field(field.id)
                before_done = operation.done(before.coverage) if before.coverage else None
                material = _material(implement, operation.material)
                money = (self.world.farm() or FarmInfo(None, None, None, None)).money
                physical_attempted = True
                enter("FIELD_WORK")
                yaw = math.atan2(field.center[0] - tractor.position[0], field.center[1] - tractor.position[1])
                start = Pose(field.center[0], field.center[1], yaw)
                if (field.area_ha and tractor.heading is not None
                        and math.dist(tractor.position, field.center) <= math.sqrt(field.area_ha * 10000.0) / 2):
                    # Already on the field (a harvest partly done): on from where it stands, GIANTS'
                    # own default (live bc801c056bac4966-1: the field's point was not_reachable).
                    start = Pose(tractor.position[0], tractor.position[1], tractor.heading)
                area = command.get("area_m2")
                seed = seed_position(implement, command.get("crop")) or 0      # the player's crop, else as set
                extra = {**({"seed": seed} if seed else {}),
                         **({"hand": True} if command.get("hand_off") and not area else {})}
                for detour in range(MAX_DETOURS + 1):
                    try:
                        ended = self.world.fieldwork(tractor.id, field.id, start, owner, area or 0.0, **extra)
                        break
                    except StepFailed as failure:
                        # Stuck on the way while GIANTS still drives: as _navigate.
                        if failure.code != "FIELDWORK_STUCK" or detour == MAX_DETOURS:
                            raise
                        self._block_ahead(tractor.id, detour)
                if ended in HELPER_DID_NOT_DRIVE and self._to_field(tractor, field, ids, owner, enter):
                    # GIANTS' helper did not drive to the field (live 5c8eeb68ccea4304-2: the 3650
                    # 1 km off at field 69, NOT_REACHABLE at once; 9211b19abaa24cce-3: the combine,
                    # COULD_NOT_PREPARE): Farmhand took the pair onto it; the helper starts there.
                    tractor = self._pair(ids, farm_id, owner, attached=True)[0]
                    start = Pose(tractor.position[0], tractor.position[1], tractor.heading or 0.0)
                    enter("FIELD_WORK")
                    ended = self.world.fieldwork(tractor.id, field.id, start, owner, area or 0.0, **extra)
                if ended == "WORKING":
                    # Handed off (the user 2026-10-21: workers go on while Farmhand does something
                    # else): GIANTS' helper works on as the game's worker; the pair stays reserved
                    # for it until the coordinator sees the helper end, then measures the field.
                    handed = True
                    status, reason = "COMPLETE", "WORKER_STARTED"
                else:
                    enter("VERIFY")
                    tractor, implement = self._pair(ids, farm_id, owner, attached=True)
                    after = self._field(field.id)
                    if after.coverage is None:
                        raise ObservationMismatch("FIELD_NOT_OBSERVED")
                    done = operation.done(after.coverage)
                    log.info("FIELD_WORK %s ended %s: field %s %s %.3f -> %.3f; after: %s", operation.name, ended,
                             field.id, operation.name, before_done if before_done is not None else -1, done,
                             describe(after.coverage))
                    if area:
                        # A bounded test area: the operation's own measured share grew by it.
                        worked = (done - (before_done or 0.0)) * (after.coverage.total_m2 or 0.0)
                        log.info("FIELD_WORK test area: %.0f m2 worked of %.0f m2 asked", worked, area)
                        if worked < area * COVERAGE_REQUIRED:
                            raise StepFailed(ended if ended not in ("FINISHED", "AREA_REACHED") else "PARTIAL_COVERAGE")
                    elif done < COVERAGE_REQUIRED:
                        # GIANTS' own reason when it stopped early (OUT_OF_FILL, FIELD_NOT_READY, ...).
                        raise StepFailed(ended if ended != "FINISHED" else "PARTIAL_COVERAGE")
                    if seed:
                        # The crop asked for is what now grows where the samples fell.
                        grown = max(after.coverage.fruit or (), key=lambda f: f[1], default=(None, 0))[0]
                        log.info("FIELD_WORK crop %r: seeder %s, field shows %s", command["crop"],
                                 implement.seeds[seed - 1], grown)
                        if grown != implement.seeds[seed - 1]:
                            raise ObservationMismatch("CROP_NOT_OBSERVED")
                    if operation.material is not None:
                        used = _material(implement, operation.material)
                        farm = self.world.farm()
                        level_fell = (material is not None and used is not None and material.level is not None
                                      and used.level is not None and used.level < material.level)
                        bought = (farm is not None and money is not None and farm.money is not None
                                  and farm.money < money and self._helper_buys(operation))
                        log.info("material %s: %s -> %s l; money %s -> %s", operation.material,
                                 material and material.level, used and used.level, money, farm and farm.money)
                        if not (level_fell or bought):
                            raise ObservationMismatch("MATERIAL_USE_NOT_OBSERVED")
                    status, reason = "COMPLETE", "AREA_WORK_OBSERVED" if area else "FIELD_WORK_OBSERVED"
            elif detaching:
                # DETACH -> VERIFY: GIANTS' own detach where it allows it; complete only
                # when a fresh snapshot shows neither end attached and both at rest.
                tractor, _ = self._pair(ids, farm_id, owner, attached=True)
                if not tractor.stopped:
                    raise ObservationMismatch("EQUIPMENT_MOVING")
                physical_attempted = True
                used = next((j for j in tractor.output_joints if not j.free), None)
                if used is not None:
                    self._tailgate_down(tractor, used.index, owner)
                enter("DETACH")
                self.world.detach(ids[0], ids[1], owner)
                enter("VERIFY")
                tractor, implement = self._pair(ids, farm_id, owner, attached=False)
                if not (tractor.stopped and implement.stopped):
                    raise ObservationMismatch("EQUIPMENT_MOVING")
                status, reason = "COMPLETE", "DETACH_OBSERVED"
                self._origins.pop(implement.id, None)
                # PULL_CLEAR (user-authorized 2026-10-19): GIANTS' agent cannot start
                # next to the implement just unhitched, so Farmhand's bounded straight
                # pull takes the tractor clear; refused unmoved, the detach still stands.
                if against_loose_implement(tractor, [implement]):
                    enter("PULL_CLEAR")
                    try:
                        self.world.pull_clear(ids[0], ids[1], owner)
                    except StepFailed as failure:
                        if failure.code != "ALREADY_CLEAR":
                            if failure.code not in PULL_CLEAR_REFUSALS | PULL_CLEAR_SHORT:
                                raise
                            log.info("PULL_CLEAR %s (%s)", "stopped short" if failure.code in PULL_CLEAR_SHORT
                                     else "refused unmoved", failure.code)
                            reason = "DETACHED_NOT_CLEAR"
                    enter("VERIFY")
                    tractor, implement = self._pair(ids, farm_id, owner, attached=False)
                    gap = joint_separation(tractor, implement)
                    log.info("PULL_CLEAR end: root %s, joints %s m apart", tractor.position,
                             "?" if gap is None else f"{gap:.2f}")
                    if not (tractor.stopped and implement.stopped):
                        raise ObservationMismatch("EQUIPMENT_MOVING")
                    if reason == "DETACH_OBSERVED" and (gap is None or gap < PULL_CLEAR_M - PULL_CLEAR_TOLERANCE_M):
                        raise ObservationMismatch("PULL_CLEAR_NOT_OBSERVED")
            else:
                # HITCH: VALIDATE -> [TRAVEL_NEAR] -> [STAGE] -> [LOCAL_MANEUVER] -> ALIGN_REVERSE
                # -> ATTACH -> VERIFY. Every step is followed by a fresh-snapshot check;
                # returns are not evidence, and each next step is chosen from it.
                # GOTO aims at the approaches beside the implement (companion/hitch.py
                # approach_poses), each at most once: one GIANTS refuses as blocked ground
                # (GOTO_INVALID, nothing moved) is skipped, and at most
                # MAX_APPROACH_DRIVES are driven. TRAVEL_NEAR by GOTO aims while far;
                # STAGE while the planned joint is outside LOCAL_WORK_RADIUS_M, or after
                # LOCAL_MANEUVER refused unmoved for where the tractor stands
                # (REAPPROACH_CODES).
                physical_attempted = True
                aim = {"next": 0, "drives": 0, "stuck": False, "unmoved": 0}
                aims = list(aims)

                def more():
                    return aim["next"] < len(aims) and aim["drives"] < MAX_APPROACH_DRIVES

                def goto(step):
                    """GOTO to the next aim that moves the tractor: "ended" or "stalled"
                    once it has driven, None when no aim is left. An aim GIANTS refuses
                    (GOTO_INVALID) or that leaves the tractor where it stood (live: at
                    once NOT_REACHABLE) costs no drive; one after the tractor got stuck
                    that cannot move it is TRAPPED. When no aim GIANTS took moved it (or
                    GIANTS' agent was blocked at its start, which no other aim changes),
                    UNPARK frees it once (_free_start) and "unparked" hands back to the
                    hitch planner, which re-plans from the exit from fresh joints (the user,
                    2026-10-20, after live 34c38284ac53469b-1): inside the local radius the
                    bounded steps, else the aims again, the first GOTO being the hand-off."""
                    while True:
                        while more():
                            pose = aims[aim["next"]]
                            aim["next"] += 1
                            before = self._hitch_state(ids, farm_id, owner, plan)[0].position
                            enter(step)
                            if aim.get("giants_blocked") and step == "STAGE":
                                # GIANTS' agent cannot start here; the other aims by Farmhand's own
                                # drive alone (live f530ca8d474e459d-1: the first had no route, the
                                # rest were never tried).
                                if self._farm_staged(ids[0], pose, owner):
                                    aim["drives"] += 1
                                    return "ended"
                                aim["unmoved"] += 1
                                continue
                            stalled = blocked = False
                            try:
                                if step == "TRAVEL_NEAR":
                                    self._navigate(ids[0], pose, owner, free=False)
                                else:
                                    self.world.stage(ids[0], ids[1], pose, owner)
                            except StepFailed as failure:
                                if failure.code.endswith("GOTO_INVALID"):
                                    log.info("%s aim %s refused by GIANTS (%s)", step, pose, failure.code)
                                    if step == "STAGE" and self._farm_staged(ids[0], pose, owner):
                                        aim["drives"] += 1
                                        return "ended"
                                    continue
                                if failure.code in START_BLOCKED_CODES:
                                    blocked = True
                                elif failure.code not in GOTO_STALL_CODES and failure.code != "STAGE_TIMEOUT":
                                    # A STAGE that timed out had its job cancelled by the mod
                                    # (live 4f792bc3dbeb41fc-3: wedged by a trailer and a new
                                    # plow, 2 m in 2 min): a stalled drive, the next aim tried.
                                    raise
                                else:
                                    # Stopped by the game's no-progress watchdog: an ended drive
                                    # like any other; the fresh observation decides.
                                    stalled = True
                            self._hitch_state(ids, farm_id, owner, plan)
                            if not self._goto_moved(ids[0], before) and step == "STAGE" \
                                    and self._farm_staged(ids[0], pose, owner):
                                # GIANTS did not move it (live a0dca0862a424319-1, every aim by the
                                # Titan trailer): Farmhand's own drive onto the aim at its heading.
                                aim["drives"] += 1
                                return "ended"
                            if not self._goto_moved(ids[0], before):
                                log.info("%s aim %s: the tractor did not move%s", step, pose,
                                         " (blocked at its start)" if blocked else " (stalled)" if stalled else "")
                                aim["unmoved"] += 1
                                if blocked and step == "STAGE" and self._farm_drives() and more():
                                    aim["giants_blocked"] = True
                                    continue
                                if blocked:
                                    break
                                if aim["stuck"]:
                                    raise StepFailed("TRAPPED")
                                aim["stuck"] = stalled
                                continue
                            aim["drives"] += 1
                            aim["stuck"] = stalled
                            if stalled:
                                log.info("%s to aim %s stalled after driving; observing", step, pose)
                            return "stalled" if stalled else "ended"
                        if aim["drives"] or not aim["unmoved"] or self._free_start(ids[0], owner) is None:
                            return None
                        aim.update(next=0, unmoved=0, stuck=False)
                        return "unparked"

                def unreached():
                    # No aim moved the tractor: there is no way in from here.
                    if aim["drives"] == 0:
                        return _Blocked("NO_SAFE_APPROACH")
                    return StepFailed("TRAVEL_ENDED_TOO_FAR")

                # A tractor standing on another loose implement's joint cannot be
                # moved by GIANTS' agent (live: BLOCKED in every direction), and only
                # straight after Farmhand's own detach may Farmhand pull it clear:
                # refused before any motion, never a GOTO that cannot start.
                tractor, _ = self._hitch_state(ids, farm_id, owner, plan)
                if against_loose_implement(tractor, [i for i in self.world.snapshot() if i.id != ids[1]]):
                    raise _Blocked("AGAINST_IMPLEMENT")
                self._tailgate_down(tractor, plan.tractor_joint, owner)
                if isinstance(travel, Pose) and self._farm_drives():
                    # Near the implement by Farmhand's own drive (live f27757a15ce94d2a-2: GIANTS
                    # GOTO ended at once at every aim, the 3650 unmoved 340 m from the trailer),
                    # onto the nearest approach at its heading (live a0dca0862a424319-1: arrived
                    # near it facing 43 degrees off, the bounded steps refused and GIANTS could
                    # not stage it); GIANTS GOTO below only if that finds no route.
                    tractor, _ = self._hitch_state(ids, farm_id, owner, plan)
                    near = min(aims, key=lambda a: math.dist(tractor.position, (a.x, a.z)))
                    enter("TRAVEL_NEAR")
                    try:
                        self.world.farm_drive(ids[0], (near.x, near.z), owner, 0.0, near.yaw)
                    except StepFailed as failure:
                        log.info("TRAVEL_NEAR by Farmhand's own drive: %s; GIANTS GOTO is tried", failure.code)
                    else:
                        tractor, _ = self._hitch_state(ids, farm_id, owner, plan)
                        left = min(math.dist(tractor.position, (a.x, a.z)) for a in aims)
                        log.info("TRAVEL_NEAR by Farmhand's own drive: %.1f m from the approaches", left)
                        if left <= NEAR_RADIUS_M + TRACTOR_ARRIVAL_TOLERANCE_M:
                            travel = None
                if travel is not None:
                    tractor, _ = self._hitch_state(ids, farm_id, owner, plan)
                    while True:
                        outcome = None
                        if isinstance(travel, Pose):
                            outcome = goto("TRAVEL_NEAR")
                            if outcome is None:
                                raise unreached()
                        else:
                            enter("TRAVEL_NEAR")
                            self._move(tractor.id, travel, owner)
                        tractor, _ = self._hitch_state(ids, farm_id, owner, plan)
                        left = min(math.dist(tractor.position, (a.x, a.z)) for a in aims)
                        log.info("TRAVEL_NEAR end: root %s heading %s, %.1f m from the approaches",
                                 tractor.position, tractor.heading, left)
                        if left <= NEAR_RADIUS_M + TRACTOR_ARRIVAL_TOLERANCE_M:
                            break
                        if outcome == "stalled":
                            # Stuck on the way, far from the implement: the other aims lie
                            # beside the same implement, so none is tried from here.
                            raise StepFailed(self._stall_reason(farm_id, tractor))
                        if not isinstance(travel, Pose):
                            raise StepFailed("TRAVEL_ENDED_TOO_FAR")
                tractor, wagon, error = self._joints("HITCH start", ids, farm_id, owner, plan)
                refused = None
                at_hitch = False
                while True:
                    if error.gap > LOCAL_WORK_RADIUS_M or refused is not None:
                        if not goto("STAGE"):
                            raise StepFailed(refused) if refused is not None else unreached()
                        refused = None
                        tractor, wagon, error = self._joints("STAGE end", ids, farm_id, owner, plan,
                                                             beyond="TRAVEL_ENDED_TOO_FAR")
                        if error.gap > LOCAL_WORK_RADIUS_M:
                            continue
                    # Inside the local radius the bounded steps take over: after UNPARK no
                    # hand-off GOTO is owed.
                    self._handoff.discard(ids[0])
                    if error.gap <= ATTACH_READY_M and abs(error.heading) <= ALIGN_MAX_HEADING_RAD:
                        # The joints already meet (live: re-hitching after a detach, gap
                        # 0.12 m, 0.11 m on the far side): no motion, only GIANTS'
                        # candidate decides (a look-only ALIGN_REVERSE).
                        at_hitch = True
                        break
                    if align_problem(error) is None:
                        break
                    enter("LOCAL_MANEUVER")
                    try:
                        self.world.maneuver(tractor.id, wagon.id, plan, owner)
                    except StepFailed as failure:
                        self._log_joints("LOCAL_MANEUVER failed", ids, plan)
                        if failure.code not in REAPPROACH_CODES or not more():
                            raise
                        log.info("LOCAL_MANEUVER refused unmoved (%s): re-approach", failure.code)
                        refused = failure.code
                        # No room to curve in: the staging pose (straight ahead of the
                        # drawbar, a plain reverse from there) is the next aim (live
                        # 9c1beb5ad65c4255-1: the store yard refused every curve and the
                        # drive cap ran out before it).
                        # Not when other equipment stands by it (live 4f792bc3dbeb41fc-3: 3 m
                        # beside the deckover's rear, GIANTS never got there).
                        others = [i.position for i in self.world.snapshot() if i.id not in ids]
                        if plan.staging in aims[aim["next"]:] and not any(
                                math.dist((plan.staging.x, plan.staging.z), p) < APPROACH_CLEARANCE_M for p in others):
                            aims.remove(plan.staging)
                            aims.insert(aim["next"], plan.staging)
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
                # GIANTS' AI folds a Foldable part for transport whenever a job starts
                # (Foldable: onAIImplementPrepareForTransport; allowUnfoldingByAI defaults
                # true): a GOTO on the way raised the tailgate again (live
                # d6701ca959d044c0-1). Down and verified once more right before reversing under.
                tractor, wagon = self._hitch_state(ids, farm_id, owner, plan)
                self._tailgate_down(tractor, plan.tractor_joint, owner)
                enter("ALIGN_REVERSE")
                try:
                    if at_hitch:
                        self.world.align(tractor.id, wagon.id, plan, owner, check_only=True)
                    else:
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
                self._origins[wagon.id] = _pose_of(tractor)
                if hitch_only:
                    status, reason = "COMPLETE", "HITCH_OBSERVED"     # hitched where the wagon stood
                else:
                    enter("DELIVER")
                    delivery = self._move(tractor.id, delivery, owner, deliver=True, partner_id=ids[1])
                    self._arrival(ids, farm_id, owner, delivery)
            if not (hitch_only or detaching or working):
                status, reason = "COMPLETE", "ALREADY_THERE" if already_there else "DELIVERY_OBSERVED"
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
        except _Blocked as error:
            status, reason = "BLOCKED", error.code
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
            if reserved and handed:
                log.info("worker %s on field %s: reserved under %s while GIANTS' helper works", ids[0],
                         command.get("field"), owner)
            elif reserved:
                try:
                    if physical_attempted:
                        enter("STOP")
                        if not self.world.stop(ids[0], owner):
                            raise ObservationMismatch("STOP_NOT_CONFIRMED")
                        stopped = self._at_rest(ids)
                        if any(identifier not in stopped or not stopped[identifier].stopped
                               or stopped[identifier].reserved_by != owner for identifier in ids):
                            log.info("after STOP: %s", {i: (stopped[i].stopped, stopped[i].reserved_by)
                                                        for i in ids if i in stopped})
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
                    elif status == "COMPLETE" and working:
                        tractor, _ = self._pair(ids, farm_id, owner, attached=True)
                        if not tractor.stopped:
                            raise ObservationMismatch("EQUIPMENT_MOVING")
                    elif status == "COMPLETE" and detaching:
                        tractor, implement = self._pair(ids, farm_id, owner, attached=False)
                        if not (tractor.stopped and implement.stopped):
                            raise ObservationMismatch("EQUIPMENT_MOVING")
                    elif status == "COMPLETE" and already_there:
                        tractor, wagon = self._pair(ids, farm_id, owner, attached=True, check=check)
                        if not (tractor.stopped and wagon.stopped):
                            raise ObservationMismatch("EQUIPMENT_MOVING")
                    elif status == "COMPLETE":
                        self._arrival(ids, farm_id, owner, delivery, check=check)
                    enter("RELEASE")
                    self.world.release(owner)
                    if any(item.reserved_by == owner for item in self.world.snapshot()):
                        raise ObservationMismatch("RELEASE_NOT_OBSERVED")
                except _CleanupFailed as error:
                    status, reason = "UNVERIFIED", str(error)
                except Exception as error:
                    # What ended the job is said too (live 67d76c8d3a614461-1: only the cleanup was).
                    log.info("cleanup not confirmed: %s %s (after %s %s)", type(error).__name__, error,
                             status, reason)
                    status, reason = "UNVERIFIED", "CLEANUP_NOT_CONFIRMED"
        self.active = ()
        log.info("result %s %s states %s", status, reason, ",".join(states))
        return Result(status, reason, ids, tuple(states))
