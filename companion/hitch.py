"""Pure hitch geometry for HITCH: which joints, final pose, staging pose.

No game calls and no steering. GIANTS remains the authority on whether the pair
can actually attach (attachableInfo); this only says where to put the tractor.
"""
import math

from protocol.model import Equipment, HitchPlan, Pose

# Distance the staging pose sits straight ahead of the final hitch pose, i.e. the
# length of the bounded reverse. Provisional: tune at the first FS25 test.
STAGING_DISTANCE_M = 4.0
MAX_STAGING_DISTANCE_M = 10.0


def _finite(*values) -> bool:
    return all(type(v) in (int, float) and math.isfinite(v) for v in values)


def _pto_connects(outputs, inputs) -> bool:
    """GIANTS' PowerTakeOffs:attachPowerTakeOff (747-766) connects each output at
    the tractor joint to the wagon inputs at the wagon joint with the same ptoName."""
    return outputs is not None and inputs is not None and bool(outputs & inputs)


def plan_hitch(tractor: Equipment, wagon: Equipment,
               staging_distance: float = STAGING_DISTANCE_M) -> HitchPlan | None:
    """Plan a rear hitch of ``wagon`` to ``tractor``, or None if geometry is unusable.

    Joint choice: a free tractor joint behind the AI root (z < 0) and an active
    wagon input joint of the same joint type which, if the wagon needs PTO power,
    share a PTO name; the lowest (tractor, wagon) index pair wins, independent of
    observation order. Unknown power or PTO data yields no plan. The tractor faces the wagon's
    forward heading, so the last move is a straight reverse along the wagon axis.
    """
    if not _finite(staging_distance) or not 0 < staging_distance <= MAX_STAGING_DISTANCE_M:
        raise ValueError("Invalid staging distance")
    if tractor.category != "tractor" or wagon.category != "forage_wagon":
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
                    and _finite(out.x, out.z, *inp.position) and out.z < 0
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


def hitch_problem(tractor: Equipment, wagon: Equipment) -> str:
    """Why plan_hitch finds no plan, for diagnostics only (never a decision)."""
    if tractor.ai_root is None or not _finite(tractor.ai_root.x, tractor.ai_root.z, tractor.ai_root.yaw):
        return "TRACTOR_GEOMETRY_UNKNOWN"
    if not _finite(wagon.heading):
        return "WAGON_HEADING_UNKNOWN"
    if not _finite(wagon.required_pto_torque):
        return "PTO_NEED_UNKNOWN"
    rear = [j for j in tractor.output_joints if j.free and _finite(j.x, j.z) and j.z < 0]
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
