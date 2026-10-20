"""PULL_CLEAR in mod/FarmhandHitch.lua (Lua 5.1 and 5.4) on the kinematic stub of
tests/test_lua_maneuver.py (NOT GIANTS physics). It proves the bounded state
machine: straight forward only, within 8 m and 3 km/h, stopping on every
conflict and restoring the vehicle-control state on every exit.

Live geometry (23a8b1d881c7489b-3/-4): straight after the detach the tractor's joint
was 0.12 m from the cultivator's, both facing the same way, and GIANTS' agent stayed
BLOCKED for any target.
"""
import math
from pathlib import Path
import unittest

from lupa import lua51, lua54

from companion import hitch as companion_hitch
from tests.test_lua_maneuver import STUBS

ROOT = Path(__file__).resolve().parents[1]

# Just unhitched: the tractor's joint 0.12 m ahead of the implement's, on its axis.
DETACHED = dict(ahead=0.12, lateral=-0.05, heading=0.0)


class LuaPullClearTests(unittest.TestCase):
    runtime = lua54

    def setUp(self):
        self.lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        self.hitch = self.lua.execute((ROOT / "mod/FarmhandHitch.lua").read_text())
        self.W = self.lua.execute(STUBS, self.hitch)

    def world(self, **opts):
        geometry = dict(DETACHED, radius=5.5)
        geometry.update(opts)
        return self.W.new(self.lua.table(**geometry))

    def start(self, w, **overrides):
        return self.hitch.newPullClear(w.env, self.W.args(w, self.lua.table(**overrides)))

    def pull(self, w, session, steps=2000, hook=None):
        for i in range(1, steps + 1):
            if hook:
                hook(i)
            code = self.hitch.pullClearStep(w.env, session, 50)
            if code != "CONTINUE":
                return code, i
        return "CONTINUE", steps

    def drives(self, w):
        return [dict(d) for d in w.drives.values()]

    def separation(self, w):
        return math.dist((w.joint.x, w.joint.z), (w.eye.x, w.eye.z))

    def assert_ended_braked_and_restored(self, w):
        events = list(w.events.values())
        self.assertEqual(events[-2:], ["brake", "cruise:0"])
        self.assertNotEqual(w.tractor.forceIsActive, True)

    def test_mirrors_the_companion(self):
        self.assertEqual(self.hitch.PULL.clearM, companion_hitch.PULL_CLEAR_M + companion_hitch.PULL_CLEAR_MARGIN_M)

    def test_pulls_straight_forward_until_clear_then_brakes(self):
        w = self.world()
        x0, z0, yaw0 = w.steer.x, w.steer.z, w.steer.yaw
        session, why = self.start(w)
        self.assertIsNotNone(session, why)
        code, _ = self.pull(w, session)
        self.assertEqual(code, "CLEAR")
        moving = [d for d in self.drives(w) if d["allowed"]]
        self.assertTrue(moving and all(d["forwards"] and d["maxSpeed"] == 3 for d in moving))
        travelled = math.dist((w.steer.x, w.steer.z), (x0, z0))
        self.assertLess(travelled, 4.7)                       # stops as soon as it is clear
        self.assertGreaterEqual(self.separation(w), 4.5)
        self.assertAlmostEqual(w.steer.yaw, yaw0, places=6)   # straight
        self.assert_ended_braked_and_restored(w)
        self.assertEqual(self.hitch.pullClearStep(w.env, session, 50), "CLEAR")   # latched: brakes only
        self.assertFalse(self.drives(w)[-1]["allowed"])

    def test_three_point_arms_settling_after_the_detach_do_not_stop_it(self):
        # Live 4e9c443e19bd4de1-10 (seeder): after GIANTS' detach the lower links moved
        # the jointTransform back towards the implement (0.19 -> 0.14 m) while the
        # tractor pulled away. The separation is measured from the tractor's body.
        w = self.world(ahead=0.19)
        session, why = self.start(w)
        self.assertIsNotNone(session, why)

        def arms_settle(i):
            w.joint.x, w.joint.z = w.eye.x + 0.05 * math.sin(w.eye.yaw), w.eye.z + 0.05 * math.cos(w.eye.yaw)

        code, _ = self.pull(w, session, hook=arms_settle)
        self.assertEqual(code, "CLEAR")
        w.sync()
        self.assertGreaterEqual(self.separation(w), 4.5)

    def test_the_implement_settling_a_little_after_the_detach_does_not_stop_it(self):
        # Live 8d1f4ada61c14239-9 (seeder): it settled 6 cm towards the tractor after
        # GIANTS' detach. The tractor's own motion away is what must keep growing; the
        # implement's own movement stays bounded (IMPLEMENT_MOVED, 25 cm).
        w = self.world(ahead=0.19)
        session, why = self.start(w)
        self.assertIsNotNone(session, why)
        fx, fz = math.sin(w.eye.yaw), math.cos(w.eye.yaw)
        start = (w.eye.x, w.eye.z)

        def settle(i):
            if i <= 6:
                w.eye.x, w.eye.z = start[0] + 0.01 * i * fx, start[1] + 0.01 * i * fz

        code, _ = self.pull(w, session, hook=settle)
        self.assertEqual(code, "CLEAR")
        self.assertGreaterEqual(self.separation(w), 4.5)

    def test_limits_are_hard_capped(self):
        for overrides in ({"maxDistanceM": 8.5}, {"maxSpeedKmh": 3.5}, {"maxSpeedKmh": 0.5},
                          {"timeoutMs": 31000}, {"clearM": 6.5}, {"isReserved": False}):
            with self.subTest(**overrides):
                w = self.world()
                self.assertEqual(tuple(self.start(w, **overrides)), (None, "INVALID_LIMITS"))
                self.assertEqual(self.drives(w), [])

    def test_refusals_never_move(self):
        cases = [
            (dict(), lambda w: setattr(w, "entered", True), "SEATED_DURING_PULL_CLEAR"),
            (dict(), lambda w: setattr(w, "speed", 2.0), "NOT_STOPPED"),
            (dict(), lambda w: setattr(w, "ai", True), "AI_ACTIVE"),
            (dict(), lambda w: setattr(w, "reserved", False), "RESERVATION_LOST"),
            # The implement ahead of the tractor (a front joint): forward would drive into it.
            (dict(ahead=3.0, heading=math.pi), lambda w: None, "PULL_CLEAR_NOT_BEHIND"),
            (dict(), lambda w: w.addVehicle(0.0, 12.0), "PULL_CLEAR_BLOCKED"),
            (dict(), lambda w: setattr(w.player, "x", w.eye.x + 1.0) or setattr(w.player, "z", w.eye.z + 14.0),
             "PULL_CLEAR_PERSON"),
            (dict(ahead=4.6), lambda w: None, "ALREADY_CLEAR"),
        ]
        for geometry, setup, reason in cases:
            with self.subTest(reason=reason):
                w = self.world(**geometry)
                setup(w)
                session, why = self.start(w)
                self.assertIsNone(session)
                self.assertEqual(why, reason)
                self.assertEqual(self.drives(w), [])

    def test_every_conflict_while_moving_stops_it(self):
        cases = [
            (lambda w, i: i == 10 and w.takeOver(), "DRIVER_TOOK_OVER"),
            (lambda w, i: i == 10 and setattr(w, "entered", True), "SEATED_DURING_PULL_CLEAR"),
            (lambda w, i: i == 10 and setattr(w, "reserved", False), "RESERVATION_LOST"),
            (lambda w, i: i == 10 and setattr(w, "ai", True), "AI_ACTIVE"),
            (lambda w, i: i == 10 and (setattr(w.player, "x", w.steer.x) or setattr(w.player, "z", w.steer.z + 6.0)),
             "PERSON_TOO_CLOSE"),
            (lambda w, i: i == 10 and setattr(w.eye, "x", w.eye.x + 0.5), "IMPLEMENT_MOVED"),
            (lambda w, i: i == 10 and setattr(w, "stuck", True), "STALLED"),
            (lambda w, i: i == 10 and setattr(w.steer, "z", w.steer.z - 0.5), "SEPARATION_LOST"),
            (lambda w, i: i == 10 and setattr(w.steer, "x", w.steer.x + 0.8), "DIVERGED"),
        ]
        for hook, reason in cases:
            with self.subTest(reason=reason):
                w = self.world()
                session, why = self.start(w)
                self.assertIsNotNone(session, why)
                code, _ = self.pull(w, session, hook=lambda i, w=w, hook=hook: (hook(w, i), w.sync()))
                self.assertEqual(code, reason)
                self.assert_ended_braked_and_restored(w)

    def test_another_vehicle_moving_nearby_stops_it(self):
        w = self.world()
        other = w.addVehicle(-14.0, 0.0)            # parked beside, clear of the line
        session, why = self.start(w)
        self.assertIsNotNone(session, why)
        code, _ = self.pull(w, session, hook=lambda i: i == 10 and setattr(other.rootNode, "z", other.rootNode.z + 1))
        self.assertEqual(code, "OTHER_VEHICLE_MOVED")
        self.assert_ended_braked_and_restored(w)

    def test_bounded_by_time_and_distance(self):
        w = self.world()
        session, why = self.start(w, timeoutMs=1000)
        self.assertIsNotNone(session, why)
        self.assertEqual(self.pull(w, session)[0], "TIMEOUT")
        self.assert_ended_braked_and_restored(w)
        w = self.world()
        session, why = self.start(w, maxDistanceM=2)
        self.assertIsNotNone(session, why)
        self.assertEqual(self.pull(w, session)[0], "DISTANCE_LIMIT")
        self.assertLess(math.dist((w.steer.x, w.steer.z), (w.eye.x, w.eye.z)), 2.0 + 2.0 + 0.5)

    def test_an_abort_brakes_and_never_drives_again(self):
        w = self.world()
        session, why = self.start(w)
        self.assertIsNotNone(session, why)
        self.pull(w, session, steps=10)
        self.hitch.abort(w.env, session, "STOPPED")
        self.assertEqual(self.hitch.pullClearStep(w.env, session, 50), "STOPPED")
        self.assertFalse(self.drives(w)[-1]["allowed"])
        self.assertNotEqual(w.tractor.forceIsActive, True)


class Lua51PullClearTests(LuaPullClearTests):
    runtime = lua51


if __name__ == "__main__":
    unittest.main()


class DescribeJointsTests(unittest.TestCase):
    """Live FourFields 7R: the plan's joint 2 and the maneuver's read 25 m apart; the
    refusal now logs every joint as the game has it (read-only diagnostics)."""

    def test_every_joint_with_world_local_and_offset(self):
        for runtime in (lua51, lua54):
            with self.subTest(runtime=runtime.__name__):
                lua = runtime.LuaRuntime(unpack_returned_tuples=True)
                hitch = lua.execute((ROOT / "mod/FarmhandHitch.lua").read_text())
                text = lua.execute("""
                    local hitch = ...
                    local root = { x = 10, y = 0, z = 20 }
                    local env = {
                        getWorldTranslation = function(n) return n.x, n.y, n.z end,
                        worldToLocal = function(n, x, y, z) return x - n.x, y, z - n.z end,
                    }
                    local tractor = { spec_aiDrivable = { attacherJointOffsets = { { x = 0, z = -2 } } } }
                    function tractor:getAIRootNode() return root end
                    function tractor:getAttacherJoints()
                        return { { jointType = 3, jointIndex = 0, jointTransform = { x = 10, y = 0, z = 18 } },
                                 { jointType = 3, jointIndex = 0, jointTransform = { x = 18, y = 0, z = 42 } } }
                    end
                    return hitch.describeJoints(env, tractor)
                """, hitch)
                self.assertIn("aiRoot=(10.00, 20.00)", text)
                self.assertIn("#1 type=3 used=0 world=(10.00, 18.00) local=(0.00, -2.00) offset=(0.00, -2.00)", text)
                self.assertIn("#2 type=3 used=0 world=(18.00, 42.00) local=(8.00, 22.00)", text)
                self.assertEqual(hitch.describeJoints(lua.eval("{}"), lua.eval("{}")), "unreadable")
