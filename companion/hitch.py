"""Pure hitch geometry for HITCH: which joints, final pose, staging pose, and
where the bounded reverse can take over.

No game calls and no steering. GIANTS remains the authority on whether the pair
can actually attach (attachableInfo); this only says where to put the tractor.
"""
import math
from dataclasses import dataclass

from protocol.model import Equipment, HitchPlan, Pose

# What a tractor can be hitched to: forage wagons and every other towed implement
# (adapters/fs25.py: category "implement" = a GIANTS work specialization).
IMPLEMENT_CATEGORIES = frozenset({"forage_wagon", "implement"})

# Where STAGE aims GIANTS' GOTO: this far straight ahead of the final hitch pose.
# Only an aim, never a tolerance: GOTO may end anywhere (live: 14 m and 138 degrees
# off), and what follows is decided by the observed joints (align_problem).
# 6 m leaves the agent room in front of the drawbar and slack inside ALIGN_REACH_M.
STAGING_DISTANCE_M = 6.0
# A rear joint: behind the AI root, or up to this far ahead of it, where a pickup's
# gooseneck ball or fifth wheel sits over the rear axle (live: the GMC Sierra's at
# z = +0.0014 m). Front hitches are metres ahead and stay excluded.
REAR_JOINT_MAX_Z = 0.5
MAX_STAGING_DISTANCE_M = 10.0

# Where GIANTS' GOTO is aimed (TRAVEL_NEAR, STAGE): beside the implement, never in
# front of it. Live the agent reported NOT_REACHABLE for the staging pose (8ffafcc
# twice; 3da28a3f24dd450b-1 after 1.2 km, -2 at once): arriving there facing away
# from the implement takes a path across it. GIANTS also refuses a target on
# blocked ground (AIParameterPosition:validate 83-90, aiSystem:getIsPositionReachable;
# live 27539c7115a44b86: both sides of a trailer in a yard), so there are several
# aims. Each puts the planned tractor joint (ahead, aside) of the implement's joint,
# turned this many degrees from its heading towards its axis; from each the bounded
# LOCAL_MANEUVER lines up (mod/FarmhandHitch.lua Hitch.chooseCurve, empty yard:
# 13-22 m at a 4.8 m planning radius; the first also at 6.6 m). Aims only.
APPROACHES = ((-2.0, 10.0, 45.0), (2.0, 12.0, 90.0), (-6.0, 15.0, 0.0))
APPROACH_CLEARANCE_M = 10.0         # an aim this close to other equipment is tried after the clear ones

# A tractor standing with one of its joints on a loose implement's input joint (live
# 5cdd589ebdce4f01-3, 23a8b1d881c7489b-4: just unhitched, joints 0.12 m apart) cannot
# be moved by GIANTS' agent at all: the implement is a navigation obstacle touching it,
# and the agent stays BLOCKED even for a target straight ahead. Straight after its own
# detach Farmhand pulls the tractor forward until the joints are PULL_CLEAR_M apart
# (= mod/FarmhandHitch.lua Hitch.PULL.clearM); a hitch from such a stand is refused.
PULL_CLEAR_JOINT_M = 1.5
PULL_CLEAR_M = 4.0                  # verified on GIANTS' joint offsets
# The mod drives this much further (= mod/FarmhandHitch.lua Hitch.PULL.clearM - PULL_CLEAR_M): its
# measure of a three-point joint differs from GIANTS' offsets (live 0.31 m).
PULL_CLEAR_MARGIN_M = 0.5

# The envelope from which the bounded reverse (mod/FarmhandHitch.lua) can bring the
# planned tractor joint to the wagon's input joint, measured on the observed joints
# in the wagon's frame. Provisional: every check logs the numbers.
ALIGN_REACH_M = 9.0                 # the reverse's 10 m cap (Hitch.LIMITS.maxDistanceM) less 1 m of curve
ALIGN_MAX_HEADING_RAD = math.radians(30)
ALIGN_MAX_LATERAL_M = 2.0

# Inside this distance of the wagon's input joint (the planned tractor joint's gap)
# Farmhand drives the last forward metres itself (LOCAL_MANEUVER, = mod/FarmhandHitch.lua
# Hitch.LOCAL_LIMITS.workRadiusM); GIANTS' GOTO is used only from farther away.
LOCAL_WORK_RADIUS_M = 25.0


def _finite(*values) -> bool:
    return all(type(v) in (int, float) and math.isfinite(v) for v in values)


def _pto_connects(outputs, inputs) -> bool:
    """GIANTS' PowerTakeOffs:attachPowerTakeOff (747-766) connects each output at
    the tractor joint to the wagon inputs at the wagon joint with the same ptoName.
    A wagon joint with no input PTO at all (known empty) has nothing to connect:
    PowerConsumer turns on by torque alone (power_sufficient), never by a shaft."""
    if outputs is None or inputs is None:
        return False
    return not inputs or bool(outputs & inputs)


def plan_hitch(tractor: Equipment, wagon: Equipment,
               staging_distance: float = STAGING_DISTANCE_M) -> HitchPlan | None:
    """Plan a rear hitch of ``wagon`` to ``tractor``, or None if geometry is unusable.

    Joint choice: a free rear tractor joint (z < REAR_JOINT_MAX_Z) and an active
    wagon input joint of the same joint type which, if the wagon needs PTO power,
    share a PTO name; the lowest (tractor, wagon) index pair wins, independent of
    observation order. Unknown power or PTO data yields no plan. The tractor faces the wagon's
    forward heading, so the last move is a straight reverse along the wagon axis.
    """
    if not _finite(staging_distance) or not 0 < staging_distance <= MAX_STAGING_DISTANCE_M:
        raise ValueError("Invalid staging distance")
    if tractor.category != "tractor" or wagon.category not in IMPLEMENT_CATEGORIES:
        return None
    root, heading = tractor.ai_root, wagon.heading
    if root is None or not _finite(root.x, root.z, root.yaw, heading):
        return None
    need = wagon.required_pto_torque
    if not _finite(need) or need < 0:
        return None
    for joints in (tractor.output_joints, wagon.input_joints):
        if len({joint.index for joint in joints}) != len(joints):
            return None  # Contradictory observation: joint indices must be unique.
    pairs = sorted(((out, inp) for out in tractor.output_joints for inp in wagon.input_joints
                    if out.free and inp.active and out.joint_type == inp.joint_type
                    and _finite(out.x, out.z, *inp.position) and out.z < REAR_JOINT_MAX_Z
                    and (need == 0 or _pto_connects(out.pto_names, inp.pto_names))),
                   key=lambda pair: (pair[0].index, pair[1].index))
    if not pairs:
        return None
    joint, inp = pairs[0]
    target = tuple(inp.position)
    # Place the AI root so that root + rotate(yaw, joint offset) == target.
    jx, jz = Pose(0.0, 0.0, heading).to_world(joint.x, joint.z)
    final = Pose(target[0] - jx, target[1] - jz, heading)
    staging = Pose(final.x + staging_distance * math.sin(heading),
                   final.z + staging_distance * math.cos(heading), heading)
    return HitchPlan(joint.index, inp.index, joint.joint_type, target, final, staging)


def approach_poses(plan: HitchPlan, origin, others) -> tuple[Pose, ...]:
    """GOTO's aims for ``plan``: the AI root pose for each APPROACHES entry on each
    side of the implement. Aims clear of every position in ``others`` (other
    equipment) by APPROACH_CLEARANCE_M come first, then by APPROACHES order, then
    the side nearer ``origin``."""
    heading = plan.final.yaw
    # The planned joint's offset in the AI root frame (plan.final puts it on target).
    back = Pose(0.0, 0.0, -heading).to_world(plan.target[0] - plan.final.x, plan.target[1] - plan.final.z)
    fx, fz = math.sin(heading), math.cos(heading)
    others = [tuple(p) for p in others]
    ranked = []
    for rank, (ahead, aside, turn) in enumerate(APPROACHES):
        for side in (1, -1):
            jx = plan.target[0] + ahead * fx + side * aside * fz
            jz = plan.target[1] + ahead * fz - side * aside * fx
            yaw = heading - side * math.radians(turn)       # turned towards the axis
            ox, oz = Pose(0.0, 0.0, yaw).to_world(*back)
            pose = Pose(jx - ox, jz - oz, yaw)
            crowded = any(math.dist((pose.x, pose.z), other) < APPROACH_CLEARANCE_M for other in others)
            ranked.append(((crowded, rank, math.dist((pose.x, pose.z), tuple(origin))), pose))
    ranked.sort(key=lambda entry: entry[0])
    return tuple(pose for _, pose in ranked)


def joint_separation(tractor: Equipment, implement: Equipment) -> float | None:
    """The smallest distance between any of ``tractor``'s joints and any of
    ``implement``'s input joints, or None when unreadable."""
    root = tractor.ai_root
    if root is None or not _finite(root.x, root.z, root.yaw):
        return None
    joints = [root.to_world(j.x, j.z) for j in tractor.output_joints if _finite(j.x, j.z)]
    inputs = [i.position for i in implement.input_joints if _finite(*i.position)]
    if not joints or not inputs:
        return None
    return min(math.dist(j, p) for j in joints for p in inputs)


def against_loose_implement(tractor: Equipment, others) -> bool:
    """Whether one of ``tractor``'s joints is within PULL_CLEAR_JOINT_M of an input
    joint of an implement in ``others`` that is attached to nothing."""
    for other in others:
        if other.id != tractor.id and other.attached_to is None:
            gap = joint_separation(tractor, other)
            if gap is not None and gap <= PULL_CLEAR_JOINT_M:
                return True
    return False


def hitch_problem(tractor: Equipment, wagon: Equipment) -> str:
    """Why plan_hitch finds no plan, for diagnostics only (never a decision)."""
    if tractor.ai_root is None or not _finite(tractor.ai_root.x, tractor.ai_root.z, tractor.ai_root.yaw):
        return "TRACTOR_GEOMETRY_UNKNOWN"
    if not _finite(wagon.heading):
        return "WAGON_HEADING_UNKNOWN"
    if not _finite(wagon.required_pto_torque):
        return "PTO_NEED_UNKNOWN"
    rear = [j for j in tractor.output_joints if j.free and _finite(j.x, j.z) and j.z < REAR_JOINT_MAX_Z]
    active = [j for j in wagon.input_joints if j.active]
    if not rear:
        return "NO_FREE_REAR_JOINT"
    if not active:
        return "NO_ACTIVE_WAGON_JOINT"
    shared = [(o, i) for o in rear for i in active if o.joint_type == i.joint_type]
    if not shared:
        return "INCOMPATIBLE_HITCH_TYPE"
    if wagon.required_pto_torque > 0 and not any(_pto_connects(o.pto_names, i.pto_names) for o, i in shared):
        return "NO_MATCHING_PTO"
    return "NO_VALID_HITCH_GEOMETRY"


@dataclass(frozen=True)
class JointError:
    """The planned tractor joint relative to the wagon's input joint, in the wagon's
    frame: ``ahead`` along its forward axis (the side a tractor hitches from),
    ``lateral`` sideways (its local +x), ``heading`` the tractor's turn off the wagon's
    heading (radians, signed). ``joint`` and ``eye`` are the world points."""
    gap: float
    ahead: float
    lateral: float
    heading: float
    joint: tuple
    eye: tuple

    def describe(self) -> str:
        return ("tractor joint (%.2f, %.2f) wagon joint (%.2f, %.2f) gap %.2f m ahead %.2f m "
                "lateral %.2f m heading %.1f deg" % (*self.joint, *self.eye, self.gap, self.ahead,
                                                     self.lateral, math.degrees(self.heading)))


def joint_error(tractor: Equipment, wagon: Equipment, plan: HitchPlan) -> JointError | None:
    """The observed error of the planned joint pair, or None if unobservable."""
    root = tractor.ai_root
    joint = next((j for j in tractor.output_joints if j.index == plan.tractor_joint), None)
    eye = next((j for j in wagon.input_joints if j.index == plan.wagon_joint), None)
    if root is None or joint is None or eye is None or not _finite(
            root.x, root.z, root.yaw, joint.x, joint.z, *eye.position, plan.final.yaw):
        return None
    jx, jz = root.to_world(joint.x, joint.z)
    dx, dz = jx - eye.position[0], jz - eye.position[1]
    fx, fz = math.sin(plan.final.yaw), math.cos(plan.final.yaw)
    heading = (root.yaw - plan.final.yaw + math.pi) % (2 * math.pi) - math.pi
    return JointError(math.hypot(dx, dz), dx * fx + dz * fz, dx * fz - dz * fx, heading,
                      (jx, jz), tuple(eye.position))


def align_problem(error: JointError | None) -> str | None:
    """None when the bounded reverse can take over from here, else why not. The
    reverse only reverses: it never turns a tractor around or crosses the wagon."""
    if error is None:
        return "UNKNOWN_OBSERVATION"
    if error.gap > ALIGN_REACH_M:
        return "TRAVEL_ENDED_TOO_FAR"
    if error.ahead <= 0:
        return "ALIGN_WRONG_SIDE"
    if abs(error.heading) > ALIGN_MAX_HEADING_RAD:
        return "ALIGN_HEADING_OUT_OF_RANGE"
    if abs(error.lateral) > ALIGN_MAX_LATERAL_M:
        return "ALIGN_LATERAL_OUT_OF_RANGE"
    return None
