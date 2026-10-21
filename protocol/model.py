"""Internal data contract. These are not GIANTS API types."""
from dataclasses import dataclass
import math

Position = tuple[float, float]


@dataclass(frozen=True)
class Pose:
    """Horizontal pose. ``yaw`` uses GIANTS' convention: measured from +z toward +x,
    so the forward direction is (sin yaw, cos yaw) (MathUtil, adapters/EVIDENCE.md)."""
    x: float
    z: float
    yaw: float

    def to_world(self, local_x: float, local_z: float) -> Position:
        """Map a point in this pose's frame (local +z forward) to world x, z."""
        sin, cos = math.sin(self.yaw), math.cos(self.yaw)
        return (self.x + local_x * cos + local_z * sin,
                self.z - local_x * sin + local_z * cos)


@dataclass(frozen=True)
class OutputJoint:
    """A tractor attacher joint. ``x``/``z`` are GIANTS' own offsets of the joint's
    jointTransform in the AI root frame (AIDrivable attacherJointOffsets)."""
    index: int
    joint_type: str
    x: float
    z: float
    free: bool
    # ptoName of each PTO output at this joint; None = unknown.
    pto_names: frozenset[str] | None = frozenset()


@dataclass(frozen=True)
class InputJoint:
    """A towed implement's input attacher joint at its observed world position."""
    index: int
    joint_type: str
    position: Position
    active: bool
    # ptoNames that have a PTO input at this joint; None = unknown.
    pto_names: frozenset[str] | None = frozenset()


@dataclass(frozen=True)
class FillUnit:
    """One of GIANTS' fill units (FillUnit spec). None = unreadable."""
    index: int
    fill_type: str | None          # GIANTS fill type name, e.g. "SEEDS", "FERTILIZER", "UNKNOWN" when empty
    level: float | None
    capacity: float | None
    supported: frozenset[str] = frozenset()
    role: str | None = None        # "seed" (SowingMachine) or "spray" (Sprayer: fertilizer, lime, herbicide)


@dataclass(frozen=True)
class Equipment:
    id: str
    category: str
    farm_id: str
    position: Position
    # GIANTS specializations present (mod/FarmhandObserve.lua Observe.KINDS), e.g.
    # "plow", "sowing_machine", "sprayer", "trailer"; and the store item's name
    # (for the player's words only, never for selection rules).
    kinds: frozenset[str] = frozenset()
    name: str | None = None
    fill_units: tuple[FillUnit, ...] = ()
    # GIANTS' own power rule (PowerConsumer:getCanBeTurnedOn), rearranged as
    # required_pto_torque <= pto_torque_capacity. Both are in GIANTS' torque unit,
    # so no hp/kW conversion is involved. None = unknown (never sufficient); a
    # required 0 means the implement needs no PTO power (adapters/EVIDENCE.md).
    pto_torque_capacity: float | None = None
    required_pto_torque: float | None = None
    capabilities: frozenset[str] = frozenset()
    available: bool = True
    busy: bool = False
    busy_reason: str | None = None        # AI_ACTIVE, AUTODRIVE_ACTIVE, AI_STATE_UNKNOWN (diagnostics)
    occupied: bool = False
    reserved_by: str | None = None
    # The requester sits in it (single player): allowed, never "occupied"; such a
    # tractor is preferred, so "bring a forage wagon" uses the one you are riding.
    ridden: bool = False
    attached_to: str | None = None
    # GIANTS Foldable progress 0..1 (tailgate, ramps) and whether it still moves; TensionBelts
    # all fastened and the vehicles they hold; the transport this vehicle is belted to.
    # None: nothing of the kind, or unreadable.
    fold: float | None = None
    folding: bool | None = None
    belts_fastened: bool | None = None
    belted: tuple[str, ...] = ()
    belted_to: str | None = None
    belt_markers: tuple[tuple[float, ...], ...] = ()   # each belt's start x y z, end x y z (world)
    stopped: bool = True
    # Hitch geometry; unknown values are None/empty and make a vehicle unhitchable.
    # Joint types are compared per joint pair (companion/hitch.py), as GIANTS does.
    heading: float | None = None          # root forward yaw (world)
    ai_root: Pose | None = None           # AI root pose (vehicles with AIDrivable)
    output_joints: tuple[OutputJoint, ...] = ()
    input_joints: tuple[InputJoint, ...] = ()


@dataclass(frozen=True)
class FieldCoverage:
    """Shares of a field's area (mod/FarmhandObserve.lua Observe.fieldCoverage): per
    GIANTS FieldGroundType name and per spray level, the field's m2, and FieldState
    crop samples (fruit name -> count of `sampled` points)."""
    ground: tuple[tuple[str, float], ...]
    spray: tuple[tuple[int, float], ...]
    total_m2: float | None
    fruit: tuple[tuple[str, int], ...] | None
    sampled: int

    def share(self, *ground_types: str) -> float:
        return sum(share for name, share in self.ground if name in ground_types)

    def sprayed(self) -> float:
        return sum(share for level, share in self.spray if level >= 1)


@dataclass(frozen=True)
class Field:
    """A field the farm owns."""
    id: int
    area_ha: float
    center: Position
    coverage: FieldCoverage | None
    age_ms: float | None


@dataclass(frozen=True)
class StoreItem:
    """A store vehicle item (GIANTS StoreManager item): what Farmhand may buy."""
    id: int
    name: str | None
    brand: str | None
    categories: tuple[str, ...]
    price: float
    mod: bool
    width: float | None
    needed_power: float | None
    power: float | None
    hitches: tuple[str, ...] = ()       # attacherJoint configuration names, in GIANTS' order (1-based)


@dataclass(frozen=True)
class FarmInfo:
    money: float | None
    helper_buy_seeds: bool | None
    helper_buy_fertilizer: bool | None
    helper_buy_fuel: bool | None


@dataclass(frozen=True)
class HitchPlan:
    tractor_joint: int    # attacher joint desc index on the tractor
    wagon_joint: int      # input attacher joint desc index on the wagon
    joint_type: str
    target: Position      # wagon input joint world position when planned
    final: Pose           # AI root pose with the tractor joint on the wagon joint
    staging: Pose         # final pose moved the staging distance forward


@dataclass(frozen=True)
class Destination:
    """A routable delivery point, e.g. an AutoDrive map marker. Opaque ID.

    ``name`` is the marker's display name, used only for exact explicit selection."""
    id: str
    position: Position
    name: str = ""


@dataclass(frozen=True)
class Result:
    status: str
    reason: str
    equipment_ids: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
