"""Pure hitch geometry: joint selection, final pose and staging pose.

Conventions under test are GIANTS': yaw is measured from +z toward +x, so a
heading's direction is (sin yaw, cos yaw) (MathUtil, adapters/EVIDENCE.md), and
tractor joint offsets are in the AI root frame (AIDrivable attacherJointOffsets).
These tests prove arithmetic only, not that FS25 places vehicles this way.
"""
import math
import random
import unittest

from companion.hitch import STAGING_DISTANCE_M, plan_hitch
from protocol.model import Equipment, InputJoint, OutputJoint, Pose


def tractor(pose=Pose(0.0, 0.0, 0.0), joints=None, **kw):
    joints = joints if joints is not None else (OutputJoint(2, "TRAILER", 0.0, -2.5, True),)
    return Equipment("t", "tractor", "farm-a", (pose.x, pose.z), ai_root=pose,
                     heading=pose.yaw, output_joints=tuple(joints), **kw)


def wagon(position=(10.0, 20.0), heading=0.0, joints=None, **kw):
    joints = joints if joints is not None else (InputJoint(1, "TRAILER", position, True),)
    return Equipment("w", "forage_wagon", "farm-a", position, heading=heading,
                     input_joints=tuple(joints), **kw)


class PoseTests(unittest.TestCase):
    def test_to_world_follows_giants_yaw_convention(self):
        # Local +z is forward: world direction (sin yaw, cos yaw).
        for yaw in (0.0, math.pi / 2, math.pi, -math.pi / 2, 0.3):
            pose = Pose(5.0, -3.0, yaw)
            fx, fz = pose.to_world(0.0, 1.0)
            self.assertAlmostEqual(fx - 5.0, math.sin(yaw))
            self.assertAlmostEqual(fz + 3.0, math.cos(yaw))
            # A proper rotation: lengths and the right angle are preserved.
            lx, lz = pose.to_world(1.0, 0.0)
            self.assertAlmostEqual(math.dist((lx, lz), (5.0, -3.0)), 1.0)
            self.assertAlmostEqual((lx - 5.0) * (fx - 5.0) + (lz + 3.0) * (fz + 3.0), 0.0)


class PlanTests(unittest.TestCase):
    def assert_joint_meets_target(self, plan, t, joint):
        # At the final pose, the chosen tractor joint lies exactly on the wagon joint.
        jx, jz = plan.final.to_world(joint.x, joint.z)
        self.assertAlmostEqual(jx, plan.target[0])
        self.assertAlmostEqual(jz, plan.target[1])

    def test_final_and_staging_pose_for_random_geometry(self):
        rng = random.Random(7)
        for _ in range(200):
            heading = rng.uniform(-math.pi, math.pi)
            target = (rng.uniform(-500, 500), rng.uniform(-500, 500))
            joint = OutputJoint(rng.randint(1, 5), "TRAILER", rng.uniform(-0.3, 0.3),
                                rng.uniform(-4.0, -0.5), True)
            t = tractor(Pose(rng.uniform(-500, 500), rng.uniform(-500, 500),
                             rng.uniform(-math.pi, math.pi)), (joint,))
            w = wagon(target, heading, (InputJoint(3, "TRAILER", target, True),))
            plan = plan_hitch(t, w)
            self.assertIsNotNone(plan)
            self.assertEqual((plan.tractor_joint, plan.wagon_joint, plan.joint_type),
                             (joint.index, 3, "TRAILER"))
            self.assertEqual(plan.target, target)
            # The tractor faces the wagon's forward direction at both poses.
            self.assertAlmostEqual(plan.final.yaw, heading)
            self.assertAlmostEqual(plan.staging.yaw, heading)
            self.assert_joint_meets_target(plan, t, joint)
            # Staging is STAGING_DISTANCE_M straight ahead of the final pose, so the
            # final move is a straight reverse along the wagon's axis.
            dx, dz = plan.staging.x - plan.final.x, plan.staging.z - plan.final.z
            self.assertAlmostEqual(math.hypot(dx, dz), STAGING_DISTANCE_M)
            self.assertAlmostEqual(dx, STAGING_DISTANCE_M * math.sin(heading))
            self.assertAlmostEqual(dz, STAGING_DISTANCE_M * math.cos(heading))
            # The final pose is in front of the wagon joint (a rear hitch).
            ahead = ((plan.final.x - target[0]) * math.sin(heading)
                     + (plan.final.z - target[1]) * math.cos(heading))
            self.assertGreater(ahead, 0)

    def test_staging_distance_is_explicit(self):
        plan = plan_hitch(tractor(), wagon(heading=0.0), staging_distance=6.0)
        self.assertAlmostEqual(plan.staging.z - plan.final.z, 6.0)
        for bad in (0.0, -1.0, float("nan"), float("inf"), 50.0):
            with self.subTest(staging_distance=bad):
                with self.assertRaises(ValueError):
                    plan_hitch(tractor(), wagon(), staging_distance=bad)

    def test_intended_joint_selected_among_many(self):
        rear_trailer = OutputJoint(4, "TRAILER", 0.0, -2.4, True)
        candidates = [
            OutputJoint(1, "TRAILER", 0.0, 3.1, True),           # front joint: not reversed onto
            OutputJoint(2, "TRAILER", 0.0, -2.5, False),         # occupied joint
            OutputJoint(3, "IMPLEMENT", 0.0, -2.0, True),        # no matching wagon joint
            rear_trailer,
            OutputJoint(6, "TRAILER", 0.0, -2.6, True),          # same type, higher index
        ]
        inputs = [InputJoint(5, "CUTTER", (10.0, 20.0), True),          # no matching tractor joint
                  InputJoint(2, "TRAILER", (10.0, 20.2), False),  # inactive input joint
                  InputJoint(7, "TRAILER", (10.0, 20.0), True)]
        for seed in range(30):
            rng = random.Random(seed)
            rng.shuffle(candidates)
            rng.shuffle(inputs)
            plan = plan_hitch(tractor(joints=candidates), wagon(joints=inputs))
            # Lowest qualifying indices win, whatever the observation order.
            self.assertEqual((plan.tractor_joint, plan.wagon_joint), (4, 7))
            self.assert_joint_meets_target(plan, None, rear_trailer)

    def test_incompatible_or_unusable_geometry_has_no_plan(self):
        cases = {
            "type mismatch": (tractor(), wagon(joints=(InputJoint(1, "SEMITRAILER", (10.0, 20.0), True),))),
            "only front joint": (tractor(joints=(OutputJoint(1, "TRAILER", 0.0, 3.0, True),)), wagon()),
            "joint on root axis": (tractor(joints=(OutputJoint(1, "TRAILER", 0.0, 0.0, True),)), wagon()),
            "occupied joint": (tractor(joints=(OutputJoint(1, "TRAILER", 0.0, -2.5, False),)), wagon()),
            "inactive input": (tractor(), wagon(joints=(InputJoint(1, "TRAILER", (10.0, 20.0), False),))),
            "no tractor joints": (tractor(joints=()), wagon()),
            "no wagon joints": (tractor(), wagon(joints=())),
            "unknown ai root": (Equipment("t", "tractor", "farm-a", (0, 0), heading=0.0,
                                          output_joints=(OutputJoint(1, "TRAILER", 0.0, -2.5, True),)),
                                wagon()),
            "unknown wagon heading": (tractor(), wagon(heading=None)),
            "non-finite heading": (tractor(), wagon(heading=float("nan"))),
            "non-finite joint": (tractor(joints=(OutputJoint(1, "TRAILER", 0.0, float("inf"), True),)), wagon()),
            "non-finite input": (tractor(), wagon(joints=(InputJoint(1, "TRAILER", (float("nan"), 0.0), True),))),
            "wrong categories": (wagon(), tractor()),
            "duplicate tractor joint index": (tractor(joints=(OutputJoint(1, "TRAILER", 0.0, -2.5, True),
                                                              OutputJoint(1, "TRAILER", 0.0, -2.6, True))),
                                              wagon()),
            "duplicate wagon joint index": (tractor(), wagon(joints=(InputJoint(1, "TRAILER", (10.0, 20.0), True),
                                                                     InputJoint(1, "TRAILER", (10.0, 20.1), True)))),
        }
        for name, (t, w) in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(plan_hitch(t, w))

    def test_plan_is_deterministic(self):
        t, w = tractor(Pose(1.0, 2.0, 0.4)), wagon((30.0, -7.0), 2.1)
        self.assertEqual(plan_hitch(t, w), plan_hitch(t, w))
