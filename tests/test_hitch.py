"""Pure hitch geometry: joint selection, final pose and staging pose.

Conventions under test are GIANTS': yaw is measured from +z toward +x, so a
heading's direction is (sin yaw, cos yaw) (MathUtil, adapters/EVIDENCE.md), and
tractor joint offsets are in the AI root frame (AIDrivable attacherJointOffsets).
These tests prove arithmetic only, not that FS25 places vehicles this way.
"""
import math
import random
import unittest
from dataclasses import replace

from companion.hitch import (ALIGN_MAX_HEADING_RAD, ALIGN_MAX_LATERAL_M, ALIGN_REACH_M, APPROACH_BEHIND_M,
                            APPROACH_CLEARANCE_M, APPROACH_SIDE_M, LOCAL_WORK_RADIUS_M, STAGING_DISTANCE_M,
                            align_problem, approach_poses, joint_error, plan_hitch)
from protocol.model import Equipment, InputJoint, OutputJoint, Pose


def tractor(pose=Pose(0.0, 0.0, 0.0), joints=None, **kw):
    joints = joints if joints is not None else (OutputJoint(2, "TRAILER", 0.0, -2.5, True),)
    return Equipment("t", "tractor", "farm-a", (pose.x, pose.z), ai_root=pose,
                     heading=pose.yaw, output_joints=tuple(joints), **kw)


def wagon(position=(10.0, 20.0), heading=0.0, joints=None, **kw):
    joints = joints if joints is not None else (InputJoint(1, "TRAILER", position, True),)
    kw.setdefault("required_pto_torque", 0.0)  # observed: no PTO power needed
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


class ApproachTests(unittest.TestCase):
    """GOTO's aims beside the implement (live: the staging pose ahead of the drawbar
    was NOT_REACHABLE for GIANTS' agent)."""

    def test_each_approach_puts_the_planned_joint_beside_the_implement_facing_its_heading(self):
        rng = random.Random(11)
        for _ in range(100):
            heading = rng.uniform(-math.pi, math.pi)
            w = wagon((rng.uniform(-500, 500), rng.uniform(-500, 500)), heading)
            t = tractor(Pose(rng.uniform(-500, 500), rng.uniform(-500, 500), rng.uniform(-math.pi, math.pi)))
            plan = plan_hitch(t, w)
            sides = []
            for pose in approach_poses(plan, t.position, ()):
                self.assertAlmostEqual(pose.yaw, heading)
                error = joint_error(replace(t, ai_root=pose), w, plan)
                self.assertAlmostEqual(error.ahead, -APPROACH_BEHIND_M)
                self.assertAlmostEqual(abs(error.lateral), APPROACH_SIDE_M)
                self.assertAlmostEqual(error.heading, 0.0)
                self.assertLess(error.gap, LOCAL_WORK_RADIUS_M)       # LOCAL_MANEUVER takes over
                sides.append(math.copysign(1, error.lateral))
            self.assertEqual(sorted(sides), [-1, 1])                  # one on each side

    def test_a_clear_side_comes_first_then_the_nearer_one(self):
        t, w = tractor(Pose(0.0, 0.0, 0.0)), wagon((10.0, 20.0), 0.0)
        plan = plan_hitch(t, w)
        # Heading 0 (+z): lateral +x is the +x side.
        plus, minus = sorted(approach_poses(plan, t.position, ()), key=lambda p: -p.x)
        self.assertEqual(approach_poses(plan, (100.0, 0.0), ())[0], plus)     # nearer
        self.assertEqual(approach_poses(plan, (-100.0, 0.0), ())[0], minus)
        parked = (plus.x + APPROACH_CLEARANCE_M - 1, plus.z)
        self.assertEqual(approach_poses(plan, (100.0, 0.0), (parked,))[0], minus)  # crowded side second
        far = (plus.x + APPROACH_CLEARANCE_M + 1, plus.z)
        self.assertEqual(approach_poses(plan, (100.0, 0.0), (far,))[0], plus)


class PtoTests(unittest.TestCase):
    """PTO is decided for the planned joint pair, as GIANTS' attachPowerTakeOff
    connects outputs at the tractor joint to inputs at the wagon joint by ptoName."""

    joints = (OutputJoint(1, "TRAILER", 0.0, -2.4, True, frozenset()),           # no PTO here
              OutputJoint(2, "TRAILER", 0.0, -2.6, True, frozenset({"rearPto"})))

    def plan(self, need, inputs=frozenset({"rearPto"}), outputs=None):
        w = wagon(joints=(InputJoint(1, "TRAILER", (10.0, 20.0), True, inputs),),
                  required_pto_torque=need)
        return plan_hitch(tractor(joints=outputs or self.joints), w)

    def test_pto_requirement_selects_the_joint_with_a_matching_pto(self):
        self.assertEqual(self.plan(0.4).tractor_joint, 2)
        # No PTO power needed: the lowest compatible joint, PTO or not.
        self.assertEqual(self.plan(0.0).tractor_joint, 1)
        self.assertEqual(self.plan(0.0, inputs=frozenset()).tractor_joint, 1)

    def test_pto_required_without_a_matching_pair_has_no_plan(self):
        self.assertIsNone(self.plan(0.4, inputs=frozenset({"frontPto"})))
        self.assertIsNone(self.plan(0.4, inputs=frozenset()))
        no_pto = (OutputJoint(1, "TRAILER", 0.0, -2.4, True, frozenset()),)
        self.assertIsNone(self.plan(0.4, outputs=no_pto))

    def test_unknown_pto_or_power_fails_closed(self):
        unknown_out = (OutputJoint(2, "TRAILER", 0.0, -2.6, True, None),)
        self.assertIsNone(self.plan(0.4, outputs=unknown_out))
        self.assertIsNone(self.plan(0.4, inputs=None))
        # Unknown names do not matter when no PTO power is needed.
        self.assertEqual(self.plan(0.0, inputs=None, outputs=unknown_out).tractor_joint, 2)
        for need in (None, float("nan"), -0.1, float("inf")):
            with self.subTest(need=need):
                self.assertIsNone(self.plan(need))


class AlignEnvelopeTests(unittest.TestCase):
    """Where the bounded reverse may take over, from the observed joints only."""

    def setUp(self):
        self.rng = random.Random(11)

    def case(self, heading, ahead, lateral, turn):
        w = wagon((self.rng.uniform(-500, 500), self.rng.uniform(-500, 500)), heading)
        plan = plan_hitch(tractor(), w)
        fx, fz = math.sin(heading), math.cos(heading)
        eye = plan.target
        jx, jz = eye[0] + ahead * fx + lateral * fz, eye[1] + ahead * fz - lateral * fx
        yaw = heading + turn
        dx, dz = Pose(0.0, 0.0, yaw).to_world(0.0, -2.5)
        return tractor(Pose(jx - dx, jz - dz, yaw)), w, plan

    def test_the_error_is_measured_on_the_joints_in_the_wagon_frame(self):
        for _ in range(50):
            heading = self.rng.uniform(-math.pi, math.pi)
            ahead, lateral, turn = self.rng.uniform(-9, 9), self.rng.uniform(-5, 5), self.rng.uniform(-3, 3)
            t, w, plan = self.case(heading, ahead, lateral, turn)
            error = joint_error(t, w, plan)
            self.assertAlmostEqual(error.ahead, ahead)
            self.assertAlmostEqual(error.lateral, lateral)
            self.assertAlmostEqual(error.gap, math.hypot(ahead, lateral))
            self.assertAlmostEqual(math.cos(error.heading), math.cos(turn))
            self.assertAlmostEqual(math.sin(error.heading), math.sin(turn))
            self.assertLessEqual(abs(error.heading), math.pi)

    def test_the_staging_aim_is_inside_the_envelope_and_the_pose_itself_is_not_required(self):
        t, w, plan = self.case(0.7, STAGING_DISTANCE_M, 0.0, 0.0)
        self.assertIsNone(align_problem(joint_error(t, w, plan)))
        self.assertLessEqual(STAGING_DISTANCE_M + 1, ALIGN_REACH_M)       # slack for GOTO
        for ahead, lateral, turn in ((3.0, 1.9, 0.5), (8.9, -1.0, -0.5), (1.0, 0.0, 0.0)):
            t, w, plan = self.case(-2.0, ahead, lateral, turn)
            self.assertIsNone(align_problem(joint_error(t, w, plan)), (ahead, lateral, turn))

    def test_outside_the_envelope_each_reason_is_precise(self):
        cases = [((4.4, -15.0, math.radians(138)), "TRAVEL_ENDED_TOO_FAR"),        # 5304adc live end
                 ((ALIGN_REACH_M + 0.1, 0.0, 0.0), "TRAVEL_ENDED_TOO_FAR"),
                 ((-0.5, 0.0, 0.0), "ALIGN_WRONG_SIDE"),
                 ((6.0, 0.0, math.radians(138)), "ALIGN_HEADING_OUT_OF_RANGE"),
                 ((6.0, 0.0, -ALIGN_MAX_HEADING_RAD - 0.01), "ALIGN_HEADING_OUT_OF_RANGE"),
                 ((6.0, ALIGN_MAX_LATERAL_M + 0.1, 0.0), "ALIGN_LATERAL_OUT_OF_RANGE")]
        for (ahead, lateral, turn), reason in cases:
            with self.subTest(reason=reason, ahead=ahead):
                t, w, plan = self.case(1.1478, ahead, lateral, turn)
                self.assertEqual(align_problem(joint_error(t, w, plan)), reason)

    def test_unobservable_joints_are_never_inside(self):
        t, w, plan = self.case(0.0, 5.0, 0.0, 0.0)
        self.assertIsNone(joint_error(replace(t, ai_root=None), w, plan))
        self.assertIsNone(joint_error(t, replace(w, input_joints=()), plan))
        self.assertEqual(align_problem(None), "UNKNOWN_OBSERVATION")
