"""One deterministic task. No LLM calls, game physics, transport, or simulator import."""
import copy
from dataclasses import dataclass
import functools
import json
import logging
import math
import time
from pathlib import Path
from threading import Lock

from jsonschema import Draft202012Validator

from adapters.world import StepFailed, StepUnverified, World
from companion.hitch import (ALIGN_MAX_HEADING_RAD, IMPLEMENT_CATEGORIES, LOCAL_WORK_RADIUS_M, align_problem,
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
TRACTOR_ARRIVAL_TOLERANCE_M = 3.0
WAGON_ARRIVAL_TOLERANCE_M = 15.0
ARRIVAL_SETTLE_S = 10.0
MAX_TRACTOR_TRIES = 3            # tractors tried for one hitch whose attempt moved nothing
UNMOVED_M = 0.5
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
                              "LOCAL_PATH_BLOCKED", "LOCAL_NO_PATH", "LOCAL_START_AT_WAGON",
                              "OUTSIDE_LOCAL_AREA"})

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
                                 "SEATED_DURING_PULL_CLEAR", "LOCAL_AREA_UNKNOWN", "NOT_STOPPED"})
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
# UNPARK's cap (= mod/FarmhandHitch.lua Hitch.UNPARK_LIMITS.maxDistanceM; a test checks it)
# and how much more a snapshot of the root may show.
UNPARK_MAX_M, UNPARK_TOLERANCE_M = 20.0, 1.0
UNPARK_MIN_M = 0.5                # an UNPARK DONE that moved less did not happen
# UNPARK's refusals before any motion (mod/FarmhandHitch.lua Hitch.newUnpark).
UNPARK_REFUSALS = frozenset({
    "UNPARK_EXIT_BLOCKED", "UNPARK_TOO_TIGHT", "UNPARK_EXIT_TOO_FAR", "UNPARK_BLOCKED", "UNPARK_PERSON",
    "UNPARK_START_NOT_CLEAR", "UNPARK_REVERSE_NOT_ALLOWED", "UNPARK_RIG_NOT_STRAIGHT", "UNPARK_GEOMETRY_UNKNOWN",
    "UNPARK_ARTICULATED_UNSUPPORTED", "UNPARK_QUERY_UNVERIFIED", "UNPARK_QUERY_UNKNOWN", "SEATED_DURING_UNPARK",
    "LOCAL_AREA_UNKNOWN", "NOT_STOPPED", "NOT_ROOT_VEHICLE", "UNPARK_DOOR_CLOSED"})

CANCELLABLE_STEPS = frozenset({"RESERVE", "FIELD_WORK", "PULL_CLEAR", "UNPARK", "TAILGATE", "TRAVEL_NEAR", "STAGE", "LOCAL_MANEUVER", "ALIGN_REVERSE", "ATTACH",
                               "DETACH", "DELIVER"})


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


def compatible(tractor: Equipment, wagon: Equipment, kind: str = "forage_wagon", name: str | None = None) -> bool:
    """Pair-level checks. Joint type and PTO are checked per planned joint pair in
    plan_hitch, which every HITCH step re-runs. With ``name`` (the player named the
    implement, "the gooseneck") any implement of that name will do, whatever its kind."""
    fits = (wagon.category in IMPLEMENT_CATEGORIES and named(wagon, name)) if name else of_kind(wagon, kind)
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
    """move_pair: a tractor with its implement; towing needs no PTO power."""
    return (tractor.category == "tractor" and wagon.category in IMPLEMENT_CATEGORIES
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


@dataclass(frozen=True)
class Operation:
    name: str
    kinds: tuple[str, ...]            # implement kinds (Observe.KINDS) that do it, preferred first
    material: str | None              # the implement's fill-unit role it uses (FillUnit.role)

    def done(self, coverage: FieldCoverage) -> float:
        if self.name == "fertilize":
            return coverage.sprayed()
        names = {"plow": PLOWED, "cultivate": PREPARED, "sow": SOWN}[self.name]
        return coverage.share(*names)


OPERATIONS = {
    "plow": Operation("plow", ("plow",), None),
    "cultivate": Operation("cultivate", ("cultivator", "plow"), None),
    "sow": Operation("sow", ("sowing_machine",), "seed"),
    "fertilize": Operation("fertilize", ("sprayer",), "spray"),
}


def already_done(operation: Operation, field: Field, area_m2=None) -> bool:
    """The field already shows the operation's result over COVERAGE_REQUIRED of its
    area, or (for a test area) less than that area is left undone."""
    c = field.coverage
    if c is None:
        return False
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


# ------------------------------------------------------------------ procurement
# What to buy for a want, from the store's own catalog (mod/FarmhandObserve.lua
# Observe.storeItem). A want is {"kind": "plow" | "truck" | "trailer", "size":
# "large" | None, "name": words the player named | None}; the purchase itself and its
# verification are the executor's (task "buy").
STORE_KINDS = {"plow": ("PLOW",), "truck": ("CAR", "TRUCK", "PICKUP"), "trailer": ("TRAILER",)}
BUY_MONEY_TOLERANCE = 0.99        # the money must fall by at least this share of the price
CATALOG_WAIT_S = 90.0             # the game builds the catalog after a load (25 items per snapshot)


def _matches(item: StoreItem, want: dict) -> bool:
    words = [w for w in (want.get("name") or "").upper().split() if w]
    text = f"{item.name or ''} {item.brand or ''}".upper()
    if words and not all(w in text for w in words):
        return False
    kinds = STORE_KINDS.get(want["kind"], ())
    if want["kind"] == "truck" and item.power is None:
        return False                          # a truck drives itself (live: a GMC truck bed was bought)
    if want.get("name") and want["kind"] in ("truck", "trailer"):
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


def named(item: Equipment, name: str | None) -> bool:
    """The player named it ("with the gmc"): every word in its store name."""
    if not name:
        return True
    return item.name is not None and all(w in item.name.casefold().split() or w in item.name.casefold()
                                         for w in name.split())


def workable(tractor: Equipment, implement: Equipment, kinds: tuple[str, ...] = ()) -> bool:
    """field_work: a tractor GIANTS' field-work helper can run (AIFieldWorker) with an
    AI-usable implement (AIImplement) of one of ``kinds`` hitched to it."""
    return (detachable(tractor, implement) and "ai_field_worker" in tractor.kinds
            and "ai_implement" in implement.kinds and any(of_kind(implement, k) for k in kinds))


def _material(implement: Equipment, role: str | None):
    """The implement's fill unit of ``role`` (seed, spray), or None."""
    return next((u for u in implement.fill_units if role is not None and u.role == role), None)


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
        self._skip: set[str] = set()     # tractors this request already found unable to hitch
        self._chosen: dict[str, tuple] = {}   # the pair this attempt chose, where it stood
        # Where each implement was taken from, by this companion: the tractor's pose
        # when it hitched it (or found it hitched for field work). move_pair to
        # {"type": "origin"} takes it back there (the user: "you can't just leave it
        # in the field"). Never coordinates from words.
        self._origins: dict[str, Pose] = {}
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

    def _choose_hitched(self, items, farm_id, requester, target, markers, origins=None, kind=None):
        """move_pair: the hitched tractor and implement nearest the requester ("this"),
        ties by opaque ID, and its route; decided before reservation. With ``origins``
        (target origin): only an implement this companion took (of ``kind``), routed
        back to the pose it was taken from."""
        observed = {item.id: item for item in items}
        pairs = []
        for tractor in items:
            wagon = observed.get(tractor.attached_to)
            if origins is not None and (wagon is None or wagon.id not in origins
                                        or (kind is not None and not of_kind(wagon, kind))):
                continue
            if (wagon is not None and wagon.attached_to == tractor.id and movable(tractor, wagon)
                    and all(usable(x, farm_id) and x.stopped for x in (tractor, wagon))):
                pairs.append((math.dist(tractor.position, requester), tractor.id, tractor, wagon))
        if not pairs:
            return None, None, "NO_ORIGIN_KNOWN" if origins is not None else "NO_HITCHED_PAIR"
        _, _, tractor, wagon = min(pairs, key=lambda p: (p[0], p[1]))
        if origins is not None:
            return (tractor, wagon), origins[wagon.id], None
        route, failure = self._route(target, tractor, tractor.position, tractor.heading or 0.0, markers)
        if route is None:
            return None, None, failure
        return (tractor, wagon), route, None

    def _choose_attached(self, items, farm_id, requester):
        """detach_pair: the tractor with its implement (of the job's kind, if named)
        nearest the requester, ties by opaque ID; decided before reservation."""
        observed = {item.id: item for item in items}
        pairs = []
        for tractor in items:
            implement = observed.get(tractor.attached_to)
            if (implement is not None and implement.attached_to == tractor.id and self._compatible(tractor, implement)
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
            enter("BUY")
            self.world.buy(item.id, hitch)
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
        vehicle = next((i for i in self.world.snapshot() if i.id == vehicle_id), None)
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


    def _move(self, tractor_id, route, owner, deliver=False, partner_id=None):
        """One travel step by the route's provider: AutoDrive to a marker, or GIANTS
        GOTO to a pose."""
        if isinstance(route, Pose):
            self._navigate(tractor_id, route, owner, partner_id)
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

    def _run(self, command, cancel=None, on_state=None):
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
        if command["task"] == "save_game":
            try:
                enter("SAVE")
                self.world.save_game()            # DONE only on a changed save stamp
                return Result("COMPLETE", "SAVED", states=tuple(states))
            except StepUnverified as error:
                return Result("UNVERIFIED", error.code, states=tuple(states))
            except (RuntimeError, ValueError) as error:
                return Result("FAILED", getattr(error, "code", None) or type(error).__name__, states=tuple(states))

        reserved = physical_attempted = False
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
            self._compatible = functools.partial(workable, kinds=kinds)
        elif detaching:
            self._kind = command.get("implement")
            self._compatible = functools.partial(detachable, kind=self._kind)
        else:
            self._kind = command.get("implement", "forage_wagon")
            self._compatible = functools.partial(compatible, kind=self._kind, name=command.get("implement_name"))
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
                if destination is None:
                    return Result("BLOCKED", failure, states=tuple(states))
                target = destination
                log.info("destination %s %r at %s", destination.id, destination.name, destination.position)
            # The tractor the requester rides comes first; then the opaque-ID order.
            items = sorted(self.world.snapshot(), key=lambda item: (not item.ridden, item.id))
            if moving:
                pair, delivery, failure = self._choose_hitched(items, farm_id, requester, target,
                                                               self.world.destinations(),
                                                               self._origins if to_origin else None,
                                                               command.get("implement"))
                plan = travel = None
                aims = ()
            elif detaching or working:
                pair, failure = self._choose_attached(items, farm_id, requester or target)
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
            if moving:
                tractor, _ = self._pair(ids, farm_id, owner, attached=True, check=movable)
                if not tractor.stopped:
                    raise ObservationMismatch("EQUIPMENT_MOVING")
                physical_attempted = True
                enter("DELIVER")
                self._move(tractor.id, delivery, owner, deliver=True, partner_id=ids[1])
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
                area = command.get("area_m2")
                for detour in range(MAX_DETOURS + 1):
                    try:
                        ended = self.world.fieldwork(tractor.id, field.id, Pose(field.center[0], field.center[1], yaw),
                                                     owner, area or 0.0)
                        break
                    except StepFailed as failure:
                        # Stuck on the way while GIANTS still drives: as _navigate.
                        if failure.code != "FIELDWORK_STUCK" or detour == MAX_DETOURS:
                            raise
                        self._block_ahead(tractor.id, detour)
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
                            if failure.code not in PULL_CLEAR_REFUSALS:
                                raise
                            log.info("PULL_CLEAR refused unmoved (%s)", failure.code)
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
                            stalled = blocked = False
                            try:
                                if step == "TRAVEL_NEAR":
                                    self._navigate(ids[0], pose, owner, free=False)
                                else:
                                    self.world.stage(ids[0], ids[1], pose, owner)
                            except StepFailed as failure:
                                if failure.code.endswith("GOTO_INVALID"):
                                    log.info("%s aim %s refused by GIANTS (%s)", step, pose, failure.code)
                                    continue
                                if failure.code in START_BLOCKED_CODES:
                                    blocked = True
                                elif failure.code not in GOTO_STALL_CODES:
                                    raise
                                else:
                                    # Stopped by the game's no-progress watchdog: an ended drive
                                    # like any other; the fresh observation decides.
                                    stalled = True
                            self._hitch_state(ids, farm_id, owner, plan)
                            if not self._goto_moved(ids[0], before):
                                log.info("%s aim %s: the tractor did not move%s", step, pose,
                                         " (blocked at its start)" if blocked else " (stalled)" if stalled else "")
                                aim["unmoved"] += 1
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
                        if plan.staging in aims[aim["next"]:]:
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
                    self._move(tractor.id, delivery, owner, deliver=True, partner_id=ids[1])
                    self._arrival(ids, farm_id, owner, delivery)
            if not (hitch_only or detaching or working):
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
                    elif status == "COMPLETE" and working:
                        tractor, _ = self._pair(ids, farm_id, owner, attached=True)
                        if not tractor.stopped:
                            raise ObservationMismatch("EQUIPMENT_MOVING")
                    elif status == "COMPLETE" and detaching:
                        tractor, implement = self._pair(ids, farm_id, owner, attached=False)
                        if not (tractor.stopped and implement.stopped):
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
