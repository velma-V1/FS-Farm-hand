"""Farmhand's map-independent route search; a probe stands for the collision world."""
import math
from pathlib import Path
import unittest

from lupa import lua51, lua54


SOURCE = Path(__file__).resolve().parents[1] / "mod" / "FarmhandDrive.lua"


class DrivePlannerTests(unittest.TestCase):
    def plan(self, runtime, start, target, clear):
        lua = runtime.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        probe = lua.eval("function(f) return function(x,z,yaw) return f(x,z,yaw) end end")(clear)
        return drive.plan(lua.table_from(start), lua.table_from(target), 6, probe)

    def test_open_ground_reaches_a_point_without_a_marker(self):
        for runtime in (lua51, lua54):
            with self.subTest(runtime=runtime):
                path, reason = self.plan(runtime, (0, 0, 0), (0, 30), lambda *_: True)
                self.assertIsNone(reason)
                self.assertGreater(len(path), 2)
                self.assertLess(math.hypot(path[len(path)][1], path[len(path)][2] - 30), 3)

    def test_obstacle_is_never_crossed(self):
        def clear(x, z, _yaw):
            return not (-3 < x < 3 and 12 < z < 21)
        path, reason = self.plan(lua54, (0, 0, 0), (0, 25), clear)
        self.assertIsNone(reason)
        self.assertTrue(all(clear(point[1], point[2], point[3]) for point in path.values()))

    def test_unreadable_or_impassable_ground_has_no_route(self):
        for response in (None, False):
            with self.subTest(response=response):
                path, reason = self.plan(lua54, (0, 0, 0), (0, 30), lambda *_: response)
                self.assertIsNone(path)
                self.assertIn(reason, ("START_NOT_CLEAR", "NO_ROUTE"))

    def test_invalid_goal_is_refused_before_probing(self):
        path, reason = self.plan(lua54, (0, 0, 0), (float("nan"), 30),
                                 lambda *_: self.fail("probe called"))
        self.assertIsNone(path)
        self.assertEqual(reason, "INVALID_DESTINATION")

    def test_motion_refuses_lost_reservation_without_starting(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        args = lua.table_from({"isReserved": lua.eval("function() return false end")})
        session, reason = drive.begin(lua.table(), args, lua.table())
        self.assertIsNone(session)
        self.assertEqual(reason, "RESERVATION_LOST")

    def test_departure_from_a_rear_obstacle_requires_a_clear_forward_corridor(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        start = lua.table_from((0, 0, 0))
        blocker = lua.table_from((0, -5))
        ordinary = lua.eval("function() return function(_, z) return z >= 5 end end")()
        ignored = lua.eval("function() return function() return true end end")()
        distance, reason = drive.planEgress(start, blocker, ordinary, ignored)
        self.assertEqual((distance, reason), (5, None))
        blocked = lua.eval("function() return function(_, z) return z < 3 end end")()
        self.assertEqual(drive.planEgress(start, blocker, ordinary, blocked)[1], "EGRESS_BLOCKED")
        ahead = lua.table_from((0, 5))
        self.assertEqual(drive.planEgress(start, ahead, ordinary, ignored)[1], "EGRESS_NOT_BEHIND")

    def test_trailer_length_widens_turns_and_speed_eases_for_bends_and_arrival(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        self.assertGreater(drive.turnRadius(6, 11), 12)
        self.assertEqual(drive.turnRadius(6, 0), 6)
        straight = lua.table_from(((0, 0, 0), (0, 20, 0), (0, 40, 0), (0, 80, 0)), recursive=True)
        bent = lua.table_from(((0, 0, 0), (0, 20, 0), (4, 24, math.pi / 4), (8, 28, math.pi / 4)), recursive=True)
        self.assertGreater(drive.travelSpeed(straight, 2, 0, 0, 6), 30)
        self.assertLess(drive.travelSpeed(bent, 2, 0, 0, 6), 30)
        self.assertLess(drive.travelSpeed(straight, 4, 0, 76, 6), 8)
        self.assertLessEqual(drive.travelSpeed(straight, 2, 0, 0, 6), 64)

    def test_lookahead_does_not_skip_across_a_trailer_turn(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        route = lua.table_from(((0, 0, 0), (0, 4, 0), (2, 8, math.pi / 8),
                                (5, 11, math.pi / 4), (9, 12, math.pi / 2)), recursive=True)
        self.assertEqual(drive.lookaheadIndex(route, 2, 0, 0, 64), 3)

    def test_preview_samples_the_planned_arc_at_its_actual_heading(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        radius = 6.0
        change = 4.0 / radius
        end = (radius * (1 - math.cos(change)), radius * math.sin(change), change)
        path = lua.table_from(((0.0, 0.0, 0.0), end), recursive=True)
        x, z, yaw, curvature = drive.routeSample(path, 2, 2.0)
        self.assertAlmostEqual(x, radius * (1 - math.cos(2.0 / radius)), places=5)
        self.assertAlmostEqual(z, radius * math.sin(2.0 / radius), places=5)
        self.assertAlmostEqual(yaw, 2.0 / radius, places=5)
        self.assertAlmostEqual(curvature, 1.0 / radius, places=5)

    def test_arrival_does_not_probe_beyond_a_stopped_rig(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        env, session, hitch = lua.execute("""
            local vehicle = { rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end }
            local foot = {units={vehicle}}
            local env = {
                getWorldTranslation=function() return 0, 0, 0 end,
                localDirectionToWorld=function() return 0, 0, 1 end }
            local hitch = {
                routeFootprint=function() return foot end,
                routePoseClear=function(_, _, _, z) if z > 1 then return false, 'OBSTACLE barn' end return true end,
                driverConflict=function() return nil end,
                halt=function() vehicle.braked = true end }
            local session = { tractor=vehicle, farmId='1', isReserved=function() return true end,
                foot=foot, targetX=0, targetZ=0, path={{0,0,0}}, next=1, elapsed=0 }
            return env, session, hitch
        """)
        self.assertEqual(drive.step(env, session, 16, hitch), "ARRIVED")
        self.assertTrue(session.tractor.braked)


if __name__ == "__main__":
    unittest.main()
