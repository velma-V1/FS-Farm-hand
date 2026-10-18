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
class Equipment:
    id: str
    category: str
    farm_id: str
    position: Position
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
    stopped: bool = True
    # Hitch geometry; unknown values are None/empty and make a vehicle unhitchable.
    # Joint types are compared per joint pair (companion/hitch.py), as GIANTS does.
    heading: float | None = None          # root forward yaw (world)
    ai_root: Pose | None = None           # AI root pose (vehicles with AIDrivable)
    output_joints: tuple[OutputJoint, ...] = ()
    input_joints: tuple[InputJoint, ...] = ()


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
