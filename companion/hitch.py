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


def plan_hitch(tractor: Equipment, wagon: Equipment,
               staging_distance: float = STAGING_DISTANCE_M) -> HitchPlan | None:
    """Plan a rear hitch of ``wagon`` to ``tractor``, or None if geometry is unusable.

    Joint choice: a free tractor joint behind the AI root (z < 0) and an active
    wagon input joint of the same joint type; the lowest (tractor, wagon) index
    pair wins, independent of observation order. The tractor faces the wagon's
    forward heading, so the last move is a straight reverse along the wagon axis.
    """
    if not _finite(staging_distance) or not 0 < staging_distance <= MAX_STAGING_DISTANCE_M:
        raise ValueError("Invalid staging distance")
    if tractor.category != "tractor" or wagon.category != "forage_wagon":
        return None
    root, heading = tractor.ai_root, wagon.heading
    if root is None or not _finite(root.x, root.z, root.yaw, heading):
        return None
    for joints in (tractor.output_joints, wagon.input_joints):
        if len({joint.index for joint in joints}) != len(joints):
            return None  # Contradictory observation: joint indices must be unique.
    pairs = sorted(((out, inp) for out in tractor.output_joints for inp in wagon.input_joints
                    if out.free and inp.active and out.joint_type == inp.joint_type
                    and _finite(out.x, out.z, *inp.position) and out.z < 0),
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
