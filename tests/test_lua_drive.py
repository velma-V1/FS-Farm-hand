"""Farmhand's map-independent route search; a probe stands for the collision world."""
import math
from pathlib import Path
import unittest

from lupa import lua51, lua54


SOURCE = Path(__file__).resolve().parents[1] / "mod" / "FarmhandDrive.lua"
HITCH = SOURCE.with_name("FarmhandHitch.lua")


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

    def test_search_can_leave_the_start_goal_rectangle_to_go_around_a_wall(self):
        # A direct line and the old fixed-margin search are both blocked. The
        # gap is open terrain discovered through the same clearance probe.
        def clear(x, z, _yaw):
            return not (-130 < x < 130 and 18 < z < 82)
        path, reason = self.plan(lua54, (0, 0, 0), (0, 100), clear)
        self.assertIsNone(reason)
        self.assertGreater(max(abs(point[1]) for point in path.values()), 130)
        self.assertLess(math.hypot(path[len(path)][1], path[len(path)][2] - 100), 3)
        self.assertTrue(all(clear(point[1], point[2], point[3]) for point in path.values()))

    def test_global_corridor_refines_for_a_long_turning_rig(self):
        def clear(x, z, _yaw):
            return not (-130 < x < 130 and 18 < z < 82)
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        probe = lua.eval("function(f) return function(x,z,yaw) return f(x,z,yaw) end end")(clear)
        path, reason = drive.plan(lua.table_from((0, 0, 0)),
                                  lua.table_from((0, 100)), 13, probe)
        self.assertIsNone(reason)
        self.assertGreater(len(path), 50)
        self.assertTrue(all(clear(point[1], point[2], point[3]) for point in path.values()))

    def test_global_search_retries_grid_alignment_for_a_narrow_opening(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        def clear(x, z, _yaw):
            return not (38 < z < 62 and not (2 < x < 8))
        probe = lua.eval("function(f) return function(x,z,yaw) return f(x,z,yaw) end end")(clear)
        route = drive.globalRoute(lua.table_from((0, 0, 0)),
                                  lua.table_from((0, 100)), probe)
        self.assertIsNotNone(route)
        self.assertTrue(any(2 < point[1] < 8 and 38 < point[2] < 62
                            for point in route.values()))

    def test_global_search_finds_a_narrow_bridge_at_an_angle_between_cell_centres(self):
        # Live c5b7ddb950a846d9-1: the deck of a bridge lay between grid centres at every
        # offset; across it only a rig centred within ~1.5 m of its line, heading along it, fits.
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        line = math.radians(12)
        def clear(x, z, yaw):
            if not 40 < z < 100:
                return True                      # open banks on both sides of the river
            off = abs((x - 13.3) * math.cos(line) - (z - 40) * math.sin(line))
            heading = abs((yaw - line + math.pi) % (2 * math.pi) - math.pi)
            return off < 1.5 and heading < math.radians(25)
        probe = lua.eval("function(f) return function(x,z,yaw) return f(x,z,yaw) end end")(clear)
        route = drive.globalRoute(lua.table_from((0, 0, 0)), lua.table_from((30, 140)), probe)
        self.assertIsNotNone(route)
        points = [(p[1], p[2]) for p in route.values()]
        self.assertTrue(any(50 < z < 90 for _, z in points))

    def test_a_tractor_alone_leaves_a_pocket_by_a_turning_reverse_a_towed_rig_never_plans(self):
        # Live 9d309797b6744336-1: the 3650 between a tree ahead and its planter behind; straight
        # back and forward turns found no way out. Nothing towed, a reverse may turn.
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        def clear(x, z, yaw, *_):
            for d in [i * 0.5 for i in range(-7, 6)]:          # 3.5 m behind the root to 2.5 m ahead
                for w in (-1.3, 0, 1.3):
                    px = x + math.sin(yaw) * d + math.cos(yaw) * w
                    pz = z + math.cos(yaw) * d - math.sin(yaw) * w
                    if -3 < px < 3 and 3.2 < pz < 6 or -3 < px < 3 and -8.5 < pz < -6.5 or abs(px) > 8:
                        return False
            return True
        probe = lua.eval("function(f) return function(x,z,yaw,c) return f(x,z,yaw,c) end end")(clear)
        plan = lambda steered: drive.plan(lua.table_from((0, 0, 0)), lua.table_from((0, 30)), 4, probe,
                                          None, None, True, 8000, 6, steered)
        self.assertIsNone(plan(False)[0])
        path = plan(True)[0]
        self.assertIsNotNone(path)
        points = list(path.values())
        self.assertTrue(any(p[4] == -1 and p[5] not in (None, 0) for p in points))
        self.assertIsNone(points[-1][4])                          # it ends driving forward
        self.assertTrue(all(clear(p[1], p[2], p[3]) for p in points))

    def test_a_shuffle_may_begin_inside_its_margin_of_something_behind(self):
        # Live f13bb95c295c4a4b-1: stopped off a turning reverse by its planter, the start pose
        # touched it within the margin while every move forward was clear.
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        probe = lua.eval("function() return function(x,z) return z > 0.5 end end")()
        start, goal = lua.table_from((0, 0, 0)), lua.table_from((0, 30))
        self.assertEqual(drive.plan(start, goal, 4, probe, None, None, True)[1], "START_NOT_CLEAR")
        path = drive.plan(start, goal, 4, probe, None, None, True, 8000, 6)[0]
        self.assertIsNotNone(path)
        self.assertTrue(all(p[2] > 0.5 for i, p in path.items() if i > 1))

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

    def test_start_keeps_global_corridor_when_first_rig_section_needs_retry(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        env, args, hitch = lua.execute("""
            local vehicle = {ownerFarmId='1', rootNode=1,
                spec_drivable={cruiseControl={speed=40}}, maxTurningRadius=6,
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return false end,
                getAITurningRadius=function() return 6 end,
                getCruiseControlMaxSpeed=function() return 50 end,
                setCruiseControlMaxSpeed=function() end}
            vehicle.rootVehicle=vehicle
            local env={getWorldTranslation=function() return 0,0,0 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return {units={vehicle},towLength=0} end,
                routePoseClear=function() return true end,
                watchDriver=function() end}
            local args={tractor=vehicle,farmId='1',x=300,z=0,
                isReserved=function() return true end}
            return env,args,hitch
        """)
        drive.section = lua.eval("function() return nil end")
        session, reason = drive.begin(env, args, hitch)
        self.assertIsNone(reason)
        self.assertIsNotNone(session)
        self.assertIsNone(session.path)
        self.assertGreater(len(session.corridor), 2)

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

    def test_a_rig_touching_something_ahead_departs_straight_back_ignoring_only_it(self):
        # Live 2d946758a8054123-1: the 3650's nose against a tree after a pull-clear; every start
        # (Farmhand's and GIANTS') refused, the job ended.
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        start, tree = lua.table_from((0, 0, 0)), lua.table_from((0, 5))
        ordinary = lua.eval("function() return function(_, z) return z <= -3 end end")()
        ignored = lua.eval("function() return function() return true end end")()
        self.assertEqual(drive.planEgress(start, tree, ordinary, ignored, -1), (3, None))
        self.assertEqual(drive.planEgress(start, tree, ordinary, ignored, 1)[1], "EGRESS_NOT_BEHIND")
        behind = lua.table_from((0, -5))
        self.assertEqual(drive.planEgress(start, behind, ordinary, ignored, -1)[1], "EGRESS_NOT_AHEAD")
        lines = []
        lua.globals().print = lambda *p: lines.append(" ".join(map(str, p)))
        env, args, hitch = lua.execute("""
            local tree = {x=0, z=4}
            local vehicle = {ownerFarmId='1', rootNode={x=0,z=0,yaw=0},
                spec_drivable={cruiseControl={speed=40}}, maxTurningRadius=6,
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return false end,
                getAITurningRadius=function() return 6 end,
                getCruiseControlMaxSpeed=function() return 50 end,
                setCruiseControlMaxSpeed=function() end}
            vehicle.rootVehicle = vehicle
            local foot = {units={vehicle}, towLength=0, lMin=-1, lMax=1, aMin=-2, aMax=3, excluded={}}
            local env = {getWorldTranslation=function(n) return n.x,0,n.z end,
                localDirectionToWorld=function(n) return math.sin(n.yaw),0,math.cos(n.yaw) end,
                g_currentMission={getNodeObject=function() return nil end}}
            local hitch = {routeFootprint=function() return foot end,
                routePoseClear=function(_,f,x,z)
                    if f.excluded[tree] then return z > -6 end            -- the tree left out: clear behind
                    if z > -2.5 then return false, "OBSTACLE LOD0(0,4)", {tree} end
                    return true
                end,
                rigContacts=function() return "unit1:LOD0" end, watchDriver=function() end}
            return env, {tractor=vehicle, farmId='1', x=0, z=-60, isReserved=function() return true end}, hitch
        """)
        session, reason = drive.begin(env, args, hitch)
        self.assertIsNone(reason)
        self.assertEqual(session.egress.dir, -1)
        self.assertEqual(session.egress.plannedM, 3)
        self.assertIsNone(session.egress.obstacle)                   # scenery: ignored by its own nodes
        self.assertTrue(any("bounded departure 3.0m back" in l for l in lines))

    def test_a_departure_is_still_driven_when_no_route_goes_on_from_it(self):
        # Live c5b7ddb950a846d9-1: NO_ROUTE before the departure moved; the 3650 was left
        # touching the tree and GIANTS' helper could not start it either.
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        lua.globals().print = lambda *p: None
        env, args, hitch, state = lua.execute("""
            local tree = {x=0, z=4}
            local state = {}
            local vehicle = {ownerFarmId='1', rootNode={x=0,z=0,yaw=0},
                spec_drivable={cruiseControl={speed=40}}, maxTurningRadius=6,
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return false end,
                getAITurningRadius=function() return 6 end,
                getCruiseControlMaxSpeed=function() return 50 end,
                setCruiseControlMaxSpeed=function() end}
            vehicle.rootVehicle = vehicle
            local foot = {units={vehicle}, towLength=0, lMin=-1, lMax=1, aMin=-2, aMax=3, excluded={}}
            local env = {getWorldTranslation=function(n) return n.x,0,n.z end,
                localDirectionToWorld=function(n) return math.sin(n.yaw),0,math.cos(n.yaw) end,
                g_currentMission={getNodeObject=function() return nil end}}
            local hitch = {routeFootprint=function() return foot end,
                routePoseClear=function(_,f,x,z)
                    -- A pocket: clear only between 2.5 and 6 m behind, the tree left out or not.
                    if z <= -6 or math.abs(x) > 2 then return false, "GRADE" end
                    if not f.excluded[tree] and z > -2.5 then return false, "OBSTACLE LOD0(0,4)", {tree} end
                    return true
                end,
                rigContacts=function() return "unit1:LOD0" end, watchDriver=function() end}
            return env, {tractor=vehicle, farmId='1', x=0, z=-300, isReserved=function() return true end}, hitch, state
        """)
        session, reason = drive.begin(env, args, hitch)
        self.assertIsNone(reason)
        self.assertEqual(session.afterEgress, "NO_ROUTE")
        self.assertEqual(session.egress.dir, -1)
        self.assertIsNone(session.initial)

    def test_blocked_forward_turn_can_depart_by_a_clear_bounded_reverse(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        blocked_front = lua.eval("function(_,z) return z < 2 or z > 6 end")
        self.assertEqual(drive.planRetreat(lua.table_from((0,0,0)), 6, blocked_front), 4)
        clear = lua.eval("function() return true end")
        self.assertIsNone(drive.planRetreat(lua.table_from((0,0,0)), 6, clear))
        blocked_back = lua.eval("function(_,z) return z > -2 and (z < 2 or z > 6) end")
        self.assertIsNone(drive.planRetreat(lua.table_from((0,0,0)), 6, blocked_back))

    def test_reverse_candidate_requires_a_rig_clear_path_and_forward_continuation(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        clear = lua.eval("function(_,z) return z <= 0 end")
        length = drive.planRetreatRoute(lua.table_from((0,0,0)),
                                        lua.table_from((0,30)), 6, clear)
        self.assertIsNone(length)
        open_ground = lua.eval("function() return true end")
        self.assertEqual(drive.planRetreatRoute(lua.table_from((0,0,0)),
                                               lua.table_from((0,30)), 6, open_ground), 4)
        blocked_rear = lua.eval("function(_,z) return z > -2 end")
        self.assertIsNone(drive.planRetreatRoute(lua.table_from((0,0,0)),
                                                lua.table_from((0,30)), 6, blocked_rear))

    def test_large_turning_rig_can_back_farther_to_an_actual_exit(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        drive.plan = lua.eval("function(start) if start[2] <= -24 then return {{start[1],start[2],start[3]}} end end")
        clear = lua.eval("function() return true end")
        self.assertEqual(drive.planRetreatRoute(lua.table_from((0,0,0)),
                                               lua.table_from((0,30)), 12, clear), 24)

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

    def test_pure_pursuit_aim_is_interpolated_ahead_on_the_actual_path(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        route = lua.table_from(((0, 0, 0), (0, 4, 0), (2, 8, math.pi / 8),
                                (5, 11, math.pi / 4), (9, 12, math.pi / 2)), recursive=True)
        x, z = drive.goalPoint(route, 2, 0, 0, 6)
        self.assertGreater(x, 0)
        self.assertLess(x, 2)
        self.assertGreater(z, 4)
        self.assertLess(z, 8)
        x, z = drive.goalPoint(route, 2, 0, 5, 6)
        self.assertGreater(x, 2)
        self.assertLess(x, 9)

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

    def test_path_error_detects_a_vehicle_leaving_the_generated_line(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        path = lua.table_from(((0,0,0),(0,4,0),(0,8,0)),recursive=True)
        self.assertAlmostEqual(drive.pathError(path,2,0.5,5),0.5)
        self.assertAlmostEqual(drive.pathError(path,2,5,5),5)

    def test_tracking_speed_buys_steering_time_before_a_rig_leaves_its_path(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        self.assertEqual(drive.trackingSpeed(24, 0, 3.6, 0), 24)
        self.assertLess(drive.trackingSpeed(24, 0.2, 3.6, 0.2), 15)
        self.assertLess(drive.trackingSpeed(24, 1.2, 3.6, 0.2), 8)
        self.assertLessEqual(drive.trackingSpeed(24, 2.4, 3.6, 0.2), 4)
        self.assertGreaterEqual(drive.trackingSpeed(64, 3, 3.6, 0.5), 3)

    def test_each_stopped_section_gets_a_new_steering_and_throttle_ramp(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        session = lua.table_from({"elapsed": 12000, "driveStartMs": 0,
                                  "movedAt": 0, "sectionAttempt": 5})
        path = lua.table_from(((10, 0, 0), (14, 0, 0)), recursive=True)
        drive.activateSection(session, path, 9, 10, 0)
        self.assertIsNone(session.driveStartMs)
        self.assertEqual((session.next, session.reached, session.sectionAttempt), (2, 9, 1))
        self.assertEqual((session.lastMovedX, session.lastMovedZ, session.movedAt),
                         (10, 0, 12000))

    def test_intermediate_section_requires_observed_endpoint_heading(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        path = lua.table_from(((0, 0, 0), (10, 0, math.pi / 2)), recursive=True)
        self.assertFalse(drive.sectionReached(path, 7.5, 0, math.pi / 2))
        self.assertFalse(drive.sectionReached(path, 9.7, 0, 2.01))
        self.assertTrue(drive.sectionReached(path, 9.7, 0, 1.62))

    def test_arrival_does_not_probe_beyond_a_stopped_rig(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        env, session, hitch = lua.execute("""
            local vehicle = { rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end }
            local foot = {units={vehicle}, lMin=-1, lMax=1}
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

    def test_a_path_point_behind_the_vehicle_brakes_and_keeps_the_goal_for_replanning(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        env, session, hitch = lua.execute("""
            local vehicle = { rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 12 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end,
                getAISteeringNode=function() return 1 end }
            local foot = {units={vehicle}, lMin=-1, lMax=1}
            local env = {
                getWorldTranslation=function() return 0, 0, 6 end,
                localDirectionToWorld=function() return 0, 0, 1 end,
                worldToLocal=function(_,x,_,z) return 0,0,-10 end }
            local hitch = {
                routeFootprint=function() return foot end,
                routePoseClear=function() return true end,
                driverConflict=function() return nil end,
                halt=function() vehicle.braked = true end }
            local session = { tractor=vehicle, farmId='1', isReserved=function() return true end,
                foot=foot, targetX=100, targetZ=0,
                path={{0,0,0},{0,4,0}}, next=2, radius=6,
                elapsed=0, movedAt=0, lastMovedX=0, lastMovedZ=6 }
            return env, session, hitch
        """)
        self.assertEqual(drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertTrue(session.tractor.braked)
        self.assertTrue(session.recovering)
        self.assertIsNone(session.result)
        self.assertEqual((session.targetX, session.targetZ), (100, 0))

    def test_stationary_route_planning_is_not_refused_by_an_object_straight_ahead(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        env, session, hitch = lua.execute("""
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end}
            local foot={units={vehicle},lMin=-1,lMax=1}
            local env={getWorldTranslation=function() return 0,0,0 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function(_,_,x,z)
                    if math.abs(x)<0.1 and math.abs(z-2)<0.1 then return false,'SEED_BAG' end
                    return true
                end,
                driverConflict=function() return nil end,
                halt=function() end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=10,targetZ=0,path=nil,
                corridor={{0,0,10},{10,0,0}},reached=1,sectionAttempt=1,
                radius=6,elapsed=0,replans=0}
            return env,session,hitch
        """)
        drive.section = lua.eval("function() return {{0,0,0},{-2,2,0}},2 end")
        self.assertEqual(drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertEqual(session.replans, 0)
        self.assertIsNotNone(session.path)

    def test_blocked_first_section_after_a_departure_retries_from_the_corridor_start(self):
        for departure in ("retreat", "egress"):
            lua = lua54.LuaRuntime(unpack_returned_tuples=True)
            drive = lua.execute(SOURCE.read_text())
            env, session, hitch = lua.execute("""
                local mode = ...
                local vehicle = {rootNode=1, ownerFarmId='1',
                    getLastSpeed=function() return 0 end,
                    getIsAIActive=function() return false end,
                    getIsEntered=function() return true end}
                local foot={units={vehicle},lMin=-1,lMax=1}
                local env={getWorldTranslation=function() return 0,0,mode=='retreat' and -4 or 4 end,
                    localDirectionToWorld=function() return 0,0,1 end}
                local hitch={routeFootprint=function() return foot end,
                    routePoseClear=function() return true end,
                    driverConflict=function() return nil end,
                    halt=function() end}
                local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                    foot=foot,targetX=300,targetZ=0,radius=6,elapsed=0,replans=0,movedAt=0}
                if mode == 'retreat' then
                    session.retreat={x=0,z=0,yaw=0,length=4}
                else
                    session.egress={obstacle={rootNode=2},ox=0,oz=-6,x=0,z=0,yaw=0,lastDistance=9,ignoredFoot=foot}
                end
                return env,session,hitch
            """, departure)
            if departure == "egress":
                env.getWorldTranslation = lua.eval(
                    "function(node) if node == 2 then return 0,0,-6 end return 0,0,4 end")
            drive.section = lua.eval("function() return nil,9,'SECTION_BLOCKED' end")
            self.assertEqual(drive.step(env, session, 16, hitch), "CONTINUE", departure)
            self.assertIsNone(session.result, departure)
            self.assertGreater(len(session.corridor), 2, departure)
            self.assertEqual((session.reached, session.sectionAttempt), (1, 2), departure)
            self.assertFalse(session.recovering, departure)

    def test_a_due_departure_drives_before_any_section_is_planned(self):
        # Live 940689f3466445c2-1: sections planned from where the departure would end were
        # START_NOT_CLEAR; the drive ended NO_ROUTE unmoved, the 3650 still against the tree.
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        lua.globals().print = lambda *p: None
        env, session, hitch, drives = lua.execute("""
            local drives = {}
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end,
                getIsMotorStarted=function() return true end,
                raiseActive=function() end}
            local foot={units={vehicle},lMin=-1,lMax=1}
            local ignored={units={vehicle},lMin=-1,lMax=1}       -- the tree left out: clear
            local env={getWorldTranslation=function() return 0,0,0 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function(_, f) if f == ignored then return true end return false, "OBSTACLE LOD0" end,
                driverConflict=function() return nil end,
                halt=function() end, holdActive=function() end,
                drive=function(_, _, _, forward) drives[#drives+1] = forward end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=300,targetZ=0,radius=6,elapsed=0,replans=0,movedAt=0,
                corridor={{0,0},{0,10},{0,20}},reached=1,sectionAttempt=2,
                egress={ox=0,oz=4,x=0,z=0,yaw=0,dir=-1,lastDistance=4,ignoredFoot=ignored,plannedM=2}}
            return env,session,hitch,drives
        """)
        drive.section = lua.eval("function() error('a section planned before the departure') end")
        self.assertEqual(drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNone(session.result)
        self.assertEqual(list(drives.values()), [False])          # straight back, away from it

    def test_a_turning_reverse_is_followed_on_its_circle_and_stops_off_it(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        said = []
        lua.globals().print = lambda *p: said.append(" ".join(map(str, p)))
        c, length = 0.25, 8.0
        def on_arc(t):              # `advance` from (0, 0, 0) by -t at curvature c
            yaw = -t * c
            return (1 - math.cos(yaw)) / c, (math.sin(yaw)) / c, yaw
        env, session, hitch, drives, node = lua.execute("""
            local c, length = ...
            local drives = {}
            local node = {x=0, z=0, yaw=0}
            local vehicle = {rootNode=node, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end,
                getIsMotorStarted=function() return true end,
                raiseActive=function() end}
            local foot={units={vehicle},lMin=-1,lMax=1,towLength=0}
            local env={getWorldTranslation=function(n) return n.x,0,n.z end,
                localDirectionToWorld=function(n) return math.sin(n.yaw),0,math.cos(n.yaw) end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function() return true end,
                driverConflict=function() return nil end,
                halt=function() end, holdActive=function() end,
                drive=function(_, _, _, forward, lx, lz) drives[#drives+1] = {forward, lx, lz} end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=300,targetZ=0,radius=4,elapsed=0,replans=0,movedAt=0,
                retreat={x=0,z=0,yaw=0,length=length,curve=c}}
            return env,session,hitch,drives,node
        """, c, length)
        for t in (0.0, 2.0, 5.0):
            node.x, node.z, node.yaw = on_arc(t)
            self.assertEqual(drive.step(env, session, 16, hitch), "CONTINUE", t)
            forward, lx, lz = drives[len(drives)][1], drives[len(drives)][2], drives[len(drives)][3]
            self.assertFalse(forward)
            ax, az, _ = on_arc(t + 3)                       # aimed 3 m on along the circle
            x, z, yaw = on_arc(t)
            self.assertAlmostEqual(lx, (ax - x) * math.cos(yaw) - (az - z) * math.sin(yaw), places=6)
            self.assertAlmostEqual(lz, (ax - x) * math.sin(yaw) + (az - z) * math.cos(yaw), places=6)
            self.assertLess(lz, 0)
            self.assertFalse(session.retreat.diverged, t)
        x, z, yaw = on_arc(5.0)
        node.x, node.z = x + 1.5 * math.cos(yaw), z - 1.5 * math.sin(yaw)   # pushed 1.5 m off it
        drive.step(env, session, 16, hitch)
        self.assertTrue(any("reverse diverged" in l for l in said))

    def test_a_reverse_pushed_off_its_line_stops_there_says_why_and_searches_on(self):
        # Live dee63f6c79b24196-4: RETREAT_DIVERGED ended the job with the rig left bent.
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        said = []
        lua.globals().print = said.append
        env, session, hitch = lua.execute("""
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end}
            local foot={units={vehicle},lMin=-1,lMax=1}
            local env={getWorldTranslation=function() return 1.2,0,-5 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function() return true end,
                driverConflict=function() return nil end,
                halt=function() end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=300,targetZ=0,radius=6,elapsed=0,replans=0,movedAt=0,
                retreat={x=0,z=0,yaw=0,length=32}}
            return env,session,hitch
        """)
        self.assertEqual(drive.step(env, session, 16, hitch), "CONTINUE")
        line = [text for text in said if "reverse diverged" in text]
        self.assertEqual(len(line), 1)
        self.assertIn("at 1.2,-5.0", line[0])
        self.assertIn("after 5.0 of 32.0m: lateral 1.20", line[0])
        self.assertIsNone(session.retreat)
        self.assertIsNotNone(session.corridor)
        self.assertTrue(any("stopped off its line after 5.0m; route search resumed" in t for t in said))

    def test_giants_navigation_map_steers_the_corridor_but_never_decides_passage(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        clear = lua.eval("function() return true end")
        start, target = lua.table_from((0, 0, 0)), lua.table_from((0, 100))
        hall = lua.eval("function(x,z) if math.abs(x) < 15 and z > 30 and z < 70 then return false end end")
        inside = lambda r: [p for p in r.values() if abs(p[1]) < 15 and 30 < p[2] < 70]
        self.assertTrue(inside(drive.globalRoute(start, target, clear, 6)))
        self.assertFalse(inside(drive.globalRoute(start, target, clear, 6, None, hall)))
        everywhere = lua.eval("function() return false end")
        self.assertIsNotNone(drive.globalRoute(start, target, clear, 6, None, everywhere))
        wall = lua.eval("function(_,z) return z < 40 or z > 50 end")
        self.assertIsNone(drive.globalRoute(start, lua.table_from((0, 60)), wall, 6, None,
                                            lua.eval("function() return nil end")))

    def test_navigation_hint_reads_only_giants_server_map_and_fails_open(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        env = lua.eval("""{g_currentMission={aiSystem={isServer=true, navigationMap=1,
            getIsPositionReachable=function(_,x) if x > 5 then error('boom') end return x < 0 end}}}""")
        hint = drive.navHint(env)
        self.assertEqual((hint(-1, 0), hint(1, 0), hint(9, 0)), (None, False, None))
        env.g_currentMission.aiSystem.isServer = False
        self.assertIsNone(drive.navHint(env))
        self.assertIsNone(drive.navHint(lua.eval("{g_currentMission={}}")))

    def test_final_goal_is_planned_inside_the_arrival_check(self):
        for target in ((7, 23), (-15, 9), (31, -4)):
            path = self.plan(lua54, (0, 0, 0), target, lambda *_: True)[0]
            self.assertIsNotNone(path, target)
            end = path[len(path)]
            self.assertLess(math.dist((end[1], end[2]), target), 2, target)

    def test_spent_corridor_short_of_the_target_replans_instead_of_failing(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        env, session, hitch = lua.execute("""
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end}
            local foot={units={vehicle},lMin=-1,lMax=1}
            local env={getWorldTranslation=function() return 0,0,0 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function() return true end,
                driverConflict=function() return nil end,
                halt=function() end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=1.6,targetZ=2.6,path={{-1,-1,0},{0,0,0}},next=2,
                corridor={{-1,-1,3},{1.6,2.6,0}},reached=2,sectionAttempt=1,
                radius=6,elapsed=0,replans=0,movedAt=0}
            return env,session,hitch
        """)
        self.assertEqual(drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNone(session.result)
        self.assertTrue(session.recovering)
        self.assertEqual((session.targetX, session.targetZ), (1.6, 2.6))

    def test_rigid_route_box_in_a_bend_is_confirmed_by_each_units_own_box(self):
        for contacts, expected in (("-", "CONTINUE"), ("unit2:tree floor^0.40 standing", "ENVELOPE_CONTACT"),
                                   ("unit1:?", "ENVELOPE_CONTACT")):
            lua = lua54.LuaRuntime(unpack_returned_tuples=True)
            drive = lua.execute(SOURCE.read_text())
            env, session, hitch = lua.execute("""
                local contacts = ...
                local vehicle = {rootNode=1, ownerFarmId='1',
                    getLastSpeed=function() return 0 end,
                    getIsAIActive=function() return false end,
                    getIsEntered=function() return true end}
                local foot={units={vehicle},lMin=-1,lMax=1}
                local env={getWorldTranslation=function() return 0,0,0 end,
                    localDirectionToWorld=function() return 0,0,1 end}
                local hitch={routeFootprint=function() return foot end,
                    routePoseClear=function(_,_,x,z) if x == 0 and z == 0 then return false,'OBSTACLE LOD0' end return true end,
                    rigContacts=function() return contacts end,
                    driverConflict=function() return nil end,
                    halt=function() end}
                local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                    foot=foot,targetX=0,targetZ=60,path=nil,
                    corridor={{0,0,60},{0,60,0}},reached=1,sectionAttempt=1,
                    radius=6,elapsed=0,replans=0,movedAt=0}
                return env,session,hitch
            """, contacts)
            drive.section = lua.eval("function() return {{0,0,0},{0,4,0}},2 end")
            self.assertEqual(drive.step(env, session, 16, hitch), expected, contacts)

    def test_avoided_area_around_the_rig_itself_does_not_forbid_departure(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        clear = lua.eval("function() return true end")
        avoided = lua.eval("{{5,5,17}, {0,150,12}}")
        route = drive.globalRoute(lua.table_from((0,0,0)), lua.table_from((0,300)), clear, 6, avoided)
        self.assertIsNotNone(route)
        self.assertTrue(all(math.dist((p[1], p[2]), (0, 150)) >= 12 for p in route.values()))

    def test_stuck_pose_without_alternate_corridor_backs_out_before_no_route(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        drive = lua.execute(SOURCE.read_text())
        env, session, hitch = lua.execute("""
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end}
            local foot={units={vehicle},lMin=-1,lMax=1}
            local env={getWorldTranslation=function() return 0,0,0 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function(_,_,_,z) return z <= 0.5 end,
                driverConflict=function() return nil end,
                halt=function() end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=0,targetZ=60,path=nil,
                corridor={{0,0,60},{0,10,50},{0,20,40},{0,60,0}},reached=1,sectionAttempt=6,
                radius=6,elapsed=0,replans=0,movedAt=0}
            return env,session,hitch
        """)
        drive.section = lua.eval("function() return nil,2,'SECTION_BLOCKED' end")
        drive.planRetreatRoute = lua.eval("function() return 8 end")
        self.assertEqual(drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNone(session.result)
        self.assertEqual(session.retreat.length, 8)
        self.assertEqual(session.retreats, 1)



def pocket(back):
    """Facing east in a yard: a wall 5 m ahead, the fence 4 m to the south and
    `back` metres behind; the way out lies to the north."""
    return lambda x, z, _yaw: (back < x < 5 and z > -4) or (z > 30 and -60 < x < 60)


def pen(x, z, _yaw):
    """A building at (0, 100) with a fenced pen on its south side, closed by the building."""
    building = abs(x) < 31 and abs(z - 100) < 8
    fence = (-34 < x < 34 and 58 < z < 62) or (30 < abs(x) < 34 and 58 < z < 100)
    return not (building or fence)


class ShuffleAndApproachTests(unittest.TestCase):
    RUNTIME = lua54

    def setUp(self):
        self.lua = self.RUNTIME.LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().print = lambda *_: None
        self.drive = self.lua.execute(SOURCE.read_text())
        self.wrap = self.lua.eval("function(f) return function(x,z,yaw) return f(x,z,yaw) end end")

    def test_a_long_rig_boxed_in_turns_out_by_a_bounded_forward_reverse_shuffle(self):
        # Live df635e4133aa4940-1: the GMC rig, radius 11.2, boxed in a yard; every forward
        # search expanded 7 nodes and NO_ROUTE ended it.
        start, goal = self.lua.table_from((0, 0, math.pi / 2)), self.lua.table_from((0, 45))
        for back in (-6, -2):
            clear = pocket(back)
            probe = self.wrap(clear)
            self.assertEqual(self.drive.plan(start, goal, 11.2, probe, None, 0.0, True, 8000)[1], "NO_ROUTE")
            path, why = self.drive.plan(start, goal, 11.2, probe, None, 0.0, True, 8000,
                                        self.drive.SHUFFLE_SWITCHES)
            self.assertIsNone(why, back)
            points = [tuple(path[i][k] for k in (1, 2, 3, 4)) for i in range(1, len(path) + 1)]
            gears = [p[3] or 1 for p in points]
            self.assertIn(-1, gears)
            self.assertLessEqual(sum(a != b for a, b in zip(gears[1:], gears[2:])), self.drive.SHUFFLE_SWITCHES)
            self.assertEqual(gears[-1], 1)                       # it leaves driving forward
            self.assertTrue(all(clear(*p[:3]) for p in points))
            for before, after, gear in zip(points, points[1:], gears[1:]):
                if gear < 0:                                     # a reverse is straight back
                    self.assertAlmostEqual(before[2], after[2])
                    self.assertAlmostEqual(math.dist(before[:2], after[:2]), 4)
            self.assertLess(math.dist(points[-1][:2], (0, 45)), 3)
            self.assertLess(abs(points[-1][2]), math.pi / 12)

    def test_a_place_is_approached_at_a_point_the_rig_reaches_not_the_fenced_nearest(self):
        # Live 9315e713c7e3430f-3: the one standoff beside the barn was fenced in; NO_ROUTE.
        goals = self.drive.approaches(0, 100, 40, self.wrap(pen))
        points = [(goals[i][1], goals[i][2]) for i in range(1, len(goals) + 1)]
        self.assertGreater(len(points), 20)
        self.assertTrue(all(pen(x, z, 0) for x, z in points))
        self.assertTrue(all(math.dist(p, (0, 100)) <= 40.001 for p in points))
        fenced = [p for p in points if abs(p[0]) < 30 and 62 < p[1] < 92]
        self.assertTrue(fenced)
        start, probe = self.lua.table_from((0, 0, 0)), self.wrap(pen)
        nearest = min(fenced, key=lambda p: math.dist(p, (0, 0)))
        alone = self.lua.eval("function(x, z) return {goals={{x, z, 0}}} end")(*nearest)
        self.assertIsNone(self.drive.globalRoute(start, alone, probe, 6))   # one standoff: fenced in
        route = self.drive.globalRoute(start, self.lua.table(goals=goals), probe, 6)
        self.assertIsNotNone(route)
        end = (route[len(route)][1], route[len(route)][2])
        self.assertIn(end, points)
        self.assertNotIn(end, fenced)

    PLACE = """
        local f, area = ...
        local vehicle = {ownerFarmId='1', rootNode=1,
            spec_drivable={cruiseControl={speed=40}}, maxTurningRadius=6,
            getLastSpeed=function() return 0 end,
            getIsAIActive=function() return false end,
            getIsEntered=function() return false end,
            getAITurningRadius=function() return 6 end,
            getCruiseControlMaxSpeed=function() return 50 end,
            setCruiseControlMaxSpeed=function() end}
        vehicle.rootVehicle=vehicle
        local env={getWorldTranslation=function() return 0,0,0 end,
            localDirectionToWorld=function() return 0,0,1 end}
        local hitch={routeFootprint=function() return {units={vehicle},towLength=0} end,
            routePoseClear=function(_,_,x,z,yaw) return f(x,z,yaw) end,
            watchDriver=function() end}
        return env, {tractor=vehicle,farmId='1',x=0,z=100,area=area,
            isReserved=function() return true end}, hitch
    """

    def test_a_drive_to_a_place_targets_the_reachable_approach_point(self):
        lines = []
        self.lua.globals().print = lambda *parts: lines.append(" ".join(map(str, parts)))
        env, args, hitch = self.lua.execute(self.PLACE, pen, 40)
        session, reason = self.drive.begin(env, args, hitch)
        self.assertIsNone(reason)
        target = (session.targetX, session.targetZ)
        self.assertTrue(pen(*target, 0))
        self.assertFalse(abs(target[0]) < 30 and 62 < target[1] < 92)   # not inside the pen
        self.assertLessEqual(math.dist(target, (0, 100)), 40.001)
        self.assertEqual((session.corridor[len(session.corridor)][1],
                          session.corridor[len(session.corridor)][2]), target)
        self.assertTrue(any("approaches the place" in l for l in lines))
        for area, code in ((500, "INVALID_DESTINATION"), (-1, "INVALID_DESTINATION")):
            env, args, hitch = self.lua.execute(self.PLACE, pen, area)
            self.assertEqual(self.drive.begin(env, args, hitch)[1], code)
        env, args, hitch = self.lua.execute(self.PLACE, lambda x, z, _y: x * x + z * z < 25, 40)
        self.assertEqual(self.drive.begin(env, args, hitch)[1], "NO_CLEAR_APPROACH")

    def test_a_shuffle_runs_leg_by_leg_and_plans_each_next_leg_from_where_it_stopped(self):
        env, session, hitch, at = self.lua.execute("""
            local at = {x=0, z=0}
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end}
            local foot={units={vehicle},lMin=-1,lMax=1}
            local env={getWorldTranslation=function() return at.x,0,at.z end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function(_,_,_,z) return z <= 0.5 end,
                driverConflict=function() return nil end,
                halt=function() end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=0,targetZ=60,path=nil,retreats=2,
                corridor={{0,0,60},{0,10,50},{0,20,40},{0,60,0}},reached=1,sectionAttempt=6,
                radius=6,elapsed=0,replans=0,movedAt=0}
            return env,session,hitch,at
        """)
        self.drive.section = self.lua.eval("""function(_,_,_,_,_,_,switches)
            if switches then return {{0,0,0},{0,-4,0,-1},{0,-8,0,-1},{0,-4,0}}, 4 end
            return nil,2,'SECTION_BLOCKED'
        end""")
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNone(session.result)
        self.assertEqual(session.retreat.length, 8)              # the first leg: 8 m straight back
        self.assertIsNone(session.path)
        self.assertEqual(session.shuffle.reached, 4)
        at.z = -8
        planned = []
        self.drive.plan = self.lua.eval("""function(log) return function(start, target, _, _, _, _, _, _, switches)
            log(start[2], target[2], switches)
            return {{start[1],start[2],start[3]},{0,-4,0},{0,0,0},{0,4,0}}
        end end""")(lambda *a: planned.append(a))
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertEqual(planned, [(-8, 60, self.drive.SHUFFLE_SWITCHES)])   # from where it stopped
        self.assertIsNone(session.retreat)
        self.assertIsNone(session.shuffle)                       # the last leg, forward to the corridor
        self.assertEqual(session.reached, 4)
        self.assertEqual(len(session.path), 4)
        self.assertEqual(session.maxSpeedKmh, 64)
        self.assertEqual(session.shuffleLegs, 2)


    def test_a_shuffle_with_no_way_onto_its_corridor_shuffles_onto_another_avoiding_where_it_led(self):
        # Live 47d6feb676f24e32-2: the rig faced a gate it could not turn through and every
        # alternate corridor still led through it (an area around the rig is never avoided).
        lines = []
        self.lua.globals().print = lambda *parts: lines.append(" ".join(map(str, parts)))
        env, session, hitch = self.lua.execute("""
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end}
            local foot={units={vehicle},lMin=-1,lMax=1}
            local env={getWorldTranslation=function() return 0,0,0 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function() return true end,
                driverConflict=function() return nil end,
                halt=function() end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=0,targetZ=60,path=nil,retreats=2,replans=5,
                corridor={{0,0,60},{0,5,55},{0,10,50},{0,60,0}},reached=1,sectionAttempt=6,
                radius=6,elapsed=0,movedAt=0}
            return env,session,hitch
        """)
        calls = []
        self.drive.section = self.lua.eval("""function(log) return function(_,_,_,corridor,_,_,switches)
            log(switches ~= nil)
            if switches and #corridor ~= 4 then return {{0,0,0},{0,-4,0,-1},{0,-8,0,-1},{0,-4,0}}, 3 end
            return nil,2,'SECTION_BLOCKED'
        end end""")(calls.append)
        self.drive.planRetreatRoute = self.lua.eval("function() return nil end")
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNone(session.result)
        self.assertEqual(session.retreat.length, 8)
        self.assertNotEqual(len(session.corridor), 4)               # the other corridor
        area = session.avoided[len(session.avoided)]
        self.assertEqual((area[1], area[2]), (0, 10))
        self.assertLess(area[3], 10)                                  # inside the rig's distance: honoured
        self.assertTrue(all(math.dist((p[1], p[2]), (0, 10)) >= area[3]
                            for p in session.corridor.values()))
        self.assertTrue(any("shuffle tries another corridor" in l for l in lines))

    def test_with_no_shuffle_and_no_other_corridor_it_backs_out_to_where_one_avoiding_the_way_begins(self):
        # Live 33e1238ca71b481d-2: no corridor at all from a lane the grid's headings miss.
        env, session, hitch = self.lua.execute("""
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end}
            local foot={units={vehicle},lMin=-1,lMax=1}
            local env={getWorldTranslation=function() return 0,0,0 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function(_,_,x,z) return math.abs(x) < 3 and z < 9 end,
                driverConflict=function() return nil end,
                halt=function() end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=0,targetZ=60,path=nil,retreats=2,replans=5,
                corridor={{0,0,60},{0,5,55},{0,10,50},{0,60,0}},reached=1,sectionAttempt=6,
                radius=6,elapsed=0,movedAt=0}
            return env,session,hitch
        """)
        self.drive.section = self.lua.eval("function() return nil,2,'SECTION_BLOCKED' end")
        asked = []
        self.drive.planRetreatRoute = self.lua.eval("function() return nil end")
        self.drive.planBackOut = self.lua.eval("""function(log) return function(_,_,_,_,_,avoided)
            log(avoided)
            if avoided then return 12 end
        end end""")(asked.append)
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNone(session.result)
        self.assertEqual(session.retreat.length, 12)
        self.assertTrue(session.retreat.backOut)
        area = session.avoided[len(session.avoided)]
        self.assertEqual((area[1], area[2], area[3]), (0, 10, 7))
        self.assertIsNotNone(asked[-1])

    def test_backing_out_goes_as_far_back_as_is_clear_to_where_an_avoiding_corridor_begins(self):
        # A lane 30 m long behind the rig, a yard beyond it and a road from there; ahead closed.
        lane = lambda x, z, _y: (abs(x) < 3 and -30 < z < 9) or z < -30 or x > 40
        probe = self.wrap(lane)
        start, target = self.lua.table_from((0, 0, 0)), self.lua.table_from((60, 60))
        back = self.drive.planBackOut(start, target, 6, probe, None, None)
        self.assertIsNotNone(back)
        self.assertEqual(back % 4, 0)
        self.assertLessEqual(back, 29)
        self.assertGreaterEqual(back, 24)
        self.assertIsNone(self.drive.planBackOut(start, target, 6, self.wrap(lambda x, z, _y: z > -2), None, None))

    def test_held_fast_under_throttle_marks_just_past_the_front_as_blocked_and_replans(self):
        # Live 7fd526481ca24c1c-3: stopped dead from 6.6 km/h by something no query saw;
        # four replans drove at it again and the drive ended OBSTACLE_AHEAD.
        env, session, hitch = self.lua.execute("""
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end}
            local foot={units={vehicle},lMin=-1.2,lMax=1.2,aMin=-14,aMax=3}
            local env={getWorldTranslation=function() return 0,0,0 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function() return true end,
                driverConflict=function() return nil end,
                halt=function() end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=0,targetZ=60,path={{0,0,0},{0,4,0},{0,8,0}},next=2,
                radius=6,elapsed=8000,movedAt=0,lastMovedX=0,lastMovedZ=0,replans=0,
                lastSpeedMode='drive',previewAt=1e9}
            return env,session,hitch
        """)
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertTrue(session.recovering)
        held = [(session.foot.held[i][1], session.foot.held[i][2]) for i in range(1, len(session.foot.held) + 1)]
        self.assertEqual(len(held), 3)
        self.assertTrue(all(abs(z - 3.75) < 1e-9 for _, z in held))
        self.assertEqual(sorted(round(x, 6) for x, _ in held), [-0.9, 0.0, 0.9])

    def test_a_reverse_that_does_not_move_stops_and_the_search_goes_on(self):
        # Live 2268b28b08994e3b-2: a 36 m escape reversed five minutes at 0.2 km/h.
        lines = []
        self.lua.globals().print = lambda *parts: lines.append(" ".join(map(str, parts)))
        env, session, hitch = self.lua.execute("""
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end,
                getIsMotorStarted=function() return true end,
                raiseActive=function() end}
            local foot={units={vehicle},lMin=-1,lMax=1}
            local env={getWorldTranslation=function() return 0,0,0 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function() return true end,
                driverConflict=function() return nil end,
                halt=function() end, holdActive=function() end,
                drive=function() return nil end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=300,targetZ=0,radius=6,elapsed=0,replans=0,movedAt=0,
                retreat={x=0,z=0,yaw=0,length=36},commandKmh=14}
            return env,session,hitch
        """)
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNone(session.commandKmh)       # never a forward command left at the reverse's cap
        for _ in range(600):
            self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
            if session.retreat is None:
                break
        self.assertIsNone(session.retreat)
        self.assertTrue(any("held fast reversing" in l for l in lines))
        self.assertGreaterEqual(session.elapsed, 8000)

    def test_a_drive_starting_where_no_corridor_begins_backs_out_to_where_one_does(self):
        # Live 7353315fb9eb453a-3: left at the barn's approach point between a shed and a
        # tree, the first corridor search found nothing and the drive ended NO_ROUTE unmoved.
        axis = 0.35                               # a lane no grid heading (45 degree steps) fits
        def lane(x, z, yaw):
            along = x * math.sin(axis) + z * math.cos(axis)
            across = x * math.cos(axis) - z * math.sin(axis)
            aligned = abs((yaw - axis + math.pi) % math.pi) < 0.1 or abs((yaw - axis + math.pi) % math.pi - math.pi) < 0.1
            return (abs(across) < 3 and -30 < along < 6 and aligned) or along < -30 or x > 60
        lines = []
        self.lua.globals().print = lambda *parts: lines.append(" ".join(map(str, parts)))
        env, args, hitch = self.lua.execute(self.PLACE, lane, None)
        args.x, args.z = 80, 80
        self.lua.execute("""
            local env, args = ...
            env.localDirectionToWorld = function() return math.sin(0.35), 0, math.cos(0.35) end
            args.tractor.getAITurningRadius = function() return 9 end
        """, env, args)
        session, reason = self.drive.begin(env, args, hitch)
        self.assertIsNone(reason)
        self.assertIsNotNone(session.retreat)
        self.assertTrue(session.retreat.backOut)
        self.assertEqual(session.retreat.length, 28)          # the lane ends 30 m back
        self.assertTrue(any("backs" in l and "out to where one begins" in l for l in lines))

    def test_a_forward_leg_before_a_gear_change_aims_below_its_overspeed_cap(self):
        # Live 4e62355bbe684190-4: a 16 m leg aimed at its own 5 km/h cap; 5.01 latched OVERSPEED.
        env, session, hitch = self.lua.execute("""
            local vehicle = {rootNode=1, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end}
            local foot={units={vehicle},lMin=-1,lMax=1}
            local env={getWorldTranslation=function() return 0,0,0 end,
                localDirectionToWorld=function() return 0,0,1 end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function() return true end,
                driverConflict=function() return nil end,
                halt=function() end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=0,targetZ=60,path=nil,retreats=2,
                corridor={{0,0,60},{0,10,50},{0,20,40},{0,60,0}},reached=1,sectionAttempt=6,
                radius=6,elapsed=0,replans=5,movedAt=0,commandKmh=14}
            return env,session,hitch
        """)
        self.drive.section = self.lua.eval("""function(_,_,_,_,_,_,switches)
            if switches then return {{0,0,0},{0,4,0},{0,8,0},{0,4,0,-1},{0,8,0}}, 4 end
            return nil,2,'SECTION_BLOCKED'
        end""")
        self.drive.planRetreatRoute = self.lua.eval("function() return nil end")
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNotNone(session.shuffle)                 # a forward leg, then a reverse
        self.assertEqual(len(session.path), 3)
        self.assertEqual((session.maxSpeedKmh, session.legKmh), (8, 5))

    def test_a_reverse_checks_behind_along_the_trailer_line_and_a_blocked_one_searches_on(self):
        # Live e44e3f6ba43348a2-2: the tractor swung 29 degrees straightening the trailer; the
        # rigid box at its heading touched a tree beside the rig (each unit's box was clear)
        # and the drive ended OBSTACLE_AHEAD.
        env, session, hitch, probes = self.lua.execute("""
            local probes = {}
            local trailer = {x=0, z=-8, yaw=0}
            local vehicle = {rootNode={x=0,z=0,yaw=0.5}, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end,
                getIsMotorStarted=function() return true end,
                raiseActive=function() end}
            local foot={units={vehicle},lMin=-1,lMax=1,towed={rootNode=trailer},towLength=8}
            local env={getWorldTranslation=function(n) return n.x,0,n.z end,
                localDirectionToWorld=function(n) return math.sin(n.yaw),0,math.cos(n.yaw) end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function(_,_,x,z,yaw)
                    probes[#probes+1] = {x,z,yaw}
                    return math.abs(yaw) < 0.01, "OBSTACLE tree"
                end,
                driverConflict=function() return nil end,
                halt=function() end, holdActive=function() end,
                drive=function() return nil end, rigContacts=function() return "-" end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=300,targetZ=0,radius=6,elapsed=0,replans=0,movedAt=0,
                retreat={x=0,z=0,yaw=0.5,length=12}}
            return env,session,hitch,probes
        """)
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNotNone(session.retreat)
        self.assertFalse(session.retreat.diverged)          # behind it along the trailer: clear
        last = probes[len(probes)]
        self.assertAlmostEqual(last[3], 0)                  # probed at the trailer line's heading
        hitch.routePoseClear = self.lua.eval("function() return false, 'OBSTACLE wall' end")
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")   # not OBSTACLE_AHEAD
        self.assertIsNone(session.result)

    def test_a_rig_left_bent_straightens_back_along_its_trailer_before_it_plans(self):
        # Live 07d720c908304dc4-2: the box planned straight behind the truck touched a tree
        # its bent trailer was clear of; START_NOT_CLEAR with nothing moved.
        lines = []
        self.lua.globals().print = lambda *parts: lines.append(" ".join(map(str, parts)))
        env, args, hitch = self.lua.execute("""
            local trailer = {x=-4, z=-7, yaw=0.5}
            local root = {x=0, z=0, yaw=0}
            local vehicle = {ownerFarmId='1', rootNode=root,
                spec_drivable={cruiseControl={speed=40}}, maxTurningRadius=6,
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return false end,
                getAITurningRadius=function() return 6 end,
                getCruiseControlMaxSpeed=function() return 50 end,
                setCruiseControlMaxSpeed=function() end}
            vehicle.rootVehicle = vehicle
            local foot = {units={vehicle}, towLength=8, towed={rootNode=trailer}, lMin=-1, lMax=1, aMin=-12, aMax=3}
            local env = {getWorldTranslation=function(n) return n.x,0,n.z end,
                localDirectionToWorld=function(n) return math.sin(n.yaw),0,math.cos(n.yaw) end,
                g_currentMission={getNodeObject=function() return nil end}}
            local hitch = {routeFootprint=function() return foot end,
                routePoseClear=function(_,_,x,z,yaw)
                    if math.abs(yaw - 0.5) < 0.01 then return true end
                    return false, "OBSTACLE LOD0(-8,-6)", {1}
                end,
                rigContacts=function() return "-" end, watchDriver=function() end}
            return env, {tractor=vehicle, farmId='1', x=300, z=60, isReserved=function() return true end}, hitch
        """)
        session, reason = self.drive.begin(env, args, hitch)
        self.assertIsNone(reason)
        self.assertIsNone(session.initial)                       # planned once it is in line
        self.assertEqual(session.retreat.length, 10)             # the trailer's length and 2 m
        self.assertAlmostEqual(session.retreat.line.yaw, 0.5)
        self.assertTrue(any("straightening back 10m along the trailer" in l for l in lines))
        hitch.rigContacts = self.lua.eval("function() return 'unit2:LOD0' end")   # really touching
        self.assertEqual(self.drive.begin(env, args, hitch)[1], "START_NOT_CLEAR")

    def test_a_bent_rig_with_its_way_back_blocked_straightens_forward_then_plans(self):
        # Live 45b4ea8c443d4f3d-2: the tree was behind the trailer; START_NOT_CLEAR again.
        lines = []
        self.lua.globals().print = lambda *parts: lines.append(" ".join(map(str, parts)))
        env, args, hitch, state = self.lua.execute("""
            local state = {full = false}
            local trailer = {x=-4, z=-7, yaw=0.5}
            local root = {x=0, z=0, yaw=0}
            local vehicle = {ownerFarmId='1', rootNode=root,
                spec_drivable={cruiseControl={speed=40}}, maxTurningRadius=6,
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end,
                getAITurningRadius=function() return 6 end,
                getCruiseControlMaxSpeed=function() return 50 end,
                setCruiseControlMaxSpeed=function() end,
                getIsMotorStarted=function() return true end, raiseActive=function() end}
            vehicle.rootVehicle = vehicle
            local foot = {units={vehicle}, towLength=8, towed={rootNode=trailer}, lMin=-1, lMax=1, aMin=-12, aMax=3}
            local env = {getWorldTranslation=function(n) return n.x,0,n.z end,
                localDirectionToWorld=function(n) return math.sin(n.yaw),0,math.cos(n.yaw) end,
                g_currentMission={getNodeObject=function() return nil end}}
            local hitch = {routeFootprint=function() return foot end,
                routePoseClear=function(_,f,x,z,yaw)
                    if f.aMin == -1 then return true end          -- the tractor's own box ahead
                    if state.full then return true end
                    return false, "OBSTACLE LOD0(-8,-9)", {1}      -- the box planned straight behind
                end,
                rigContacts=function() return "-" end, watchDriver=function() end,
                driverConflict=function() return nil end, halt=function() end,
                holdActive=function() end, drive=function() state.drove = true end}
            return env, {tractor=vehicle, farmId='1', x=300, z=60, isReserved=function() return true end}, hitch, state
        """)
        session, reason = self.drive.begin(env, args, hitch)
        self.assertIsNone(reason)
        self.assertIsNotNone(session.straightenForward)
        self.assertEqual(session.straightenForward.length, 10)
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertTrue(state.drove)                           # forward, the rig braked otherwise
        state.full = True                                       # in line: it still goes its length
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNotNone(session.straightenForward)
        self.lua.execute("local env = ... ; env.getWorldTranslation = function(n) if n.z == -7 then return -4,0,-7 end return 0,0,10.5 end", env)
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNone(session.straightenForward)
        self.assertTrue(any("straightened forward" in l and "its length, in line" in l for l in lines))
        self.assertTrue(any("back along the trailer blocked after 1m" in l for l in lines))

    def test_a_rig_bent_by_a_turn_straightens_before_it_plans_on(self):
        # Live 125141b03ef949ff-2: a 48 m leg ended in a turn beside a fill station; every
        # plan from there was START_NOT_CLEAR and the drive ended NO_ROUTE.
        env, session, hitch = self.lua.execute("""
            local trailer = {x=-4, z=-7, yaw=0.5}
            local vehicle = {rootNode={x=0,z=0,yaw=0}, ownerFarmId='1',
                getLastSpeed=function() return 0 end,
                getIsAIActive=function() return false end,
                getIsEntered=function() return true end}
            local foot={units={vehicle},lMin=-1,lMax=1,aMin=-12,aMax=3,towed={rootNode=trailer},towLength=8}
            local env={getWorldTranslation=function(n) return n.x,0,n.z end,
                localDirectionToWorld=function(n) return math.sin(n.yaw),0,math.cos(n.yaw) end}
            local hitch={routeFootprint=function() return foot end,
                routePoseClear=function(_,_,x,z,yaw)
                    if math.abs(yaw - 0.5) < 0.01 then return true end
                    return false, "OBSTACLE collision@Fill Station"
                end,
                rigContacts=function() return "-" end,
                driverConflict=function() return nil end,
                halt=function() end}
            local session={tractor=vehicle,farmId='1',isReserved=function() return true end,
                foot=foot,targetX=0,targetZ=60,path=nil,
                corridor={{0,0,60},{0,10,50},{0,20,40},{0,60,0}},reached=1,sectionAttempt=1,
                radius=6,elapsed=0,replans=0,movedAt=0}
            return env,session,hitch
        """)
        self.assertEqual(self.drive.step(env, session, 16, hitch), "CONTINUE")
        self.assertIsNone(session.result)
        self.assertTrue(session.retreat.onTowed)               # back along the trailer, into line
        self.assertEqual(session.unbends, 1)

    AHEAD = """
        local tick = ...
        local vehicle = {rootNode=1, ownerFarmId='1'}
        local foot = {units={vehicle}, lMin=-1, lMax=1, aMin=-2, aMax=2, towLength=0}
        local now = 0
        local env = {}
        if tick then env.getTimeSec = function() now = now + tick; return now end end
        local hitch = {routePoseClear=function() return true end}
        local path = {}
        for i = 0, 10 do path[#path+1] = {0, i*4, 0} end
        local corridor = {}
        for i = 0, 30 do corridor[#corridor+1] = {0, i*10, (30-i)*10} end
        local session = {tractor=vehicle, foot=foot, radius=6, path=path, next=6,
            corridor=corridor, reached=4, elapsed=0}
        return env, session, hitch
    """

    def test_the_next_section_is_joined_while_driving_without_a_stop(self):
        lines = []
        self.lua.globals().print = lambda *parts: lines.append(" ".join(map(str, parts)))
        env, session, hitch = self.lua.execute(self.AHEAD, None)
        starts = []
        self.drive.section = self.lua.eval("""function(log) return function(start, _, _, _, reached, attempt)
            log(start[1], start[2], start[3], reached, attempt)
            return {{start[1],start[2],start[3]},{0,44,0},{0,48,0},{0,52,0}}, 9
        end end""")(lambda *a: starts.append(a))
        self.assertTrue(self.drive.planAhead(env, session, hitch))
        self.assertEqual(starts, [(0, 40, 0, 4, 1)])           # from the planned end of this one
        points = [(session.path[i][1], session.path[i][2]) for i in range(1, len(session.path) + 1)]
        self.assertEqual(points[-1], (0, 52))
        self.assertEqual(len(set(points)), len(points))         # the end point joined once
        self.assertEqual(session.path[session.next][2], 20)     # the follower still at its point
        self.assertEqual(session.reached, 9)
        self.assertTrue(any("joined ahead" in l for l in lines))
        for name, value in (("shuffle", self.lua.table()), ("legKmh", 5), ("recovering", True)):
            env, session, hitch = self.lua.execute(self.AHEAD, None)
            session[name] = value
            self.assertIsNone(self.drive.planAhead(env, session, hitch), name)
        env, session, hitch = self.lua.execute(self.AHEAD, None)
        session.reached = 31                                      # the last section: it stops there
        self.assertIsNone(self.drive.planAhead(env, session, hitch))

    def test_a_section_searched_ahead_spreads_over_frames_and_leaves_the_driven_path_alone(self):
        env, session, hitch = self.lua.execute(self.AHEAD, 0.001)
        before = [(session.path[i][1], session.path[i][2]) for i in range(1, len(session.path) + 1)]
        frames = 0
        while not self.drive.planAhead(env, session, hitch):
            frames += 1
            self.assertLess(frames, 5000)
            now = [(session.path[i][1], session.path[i][2]) for i in range(1, len(session.path) + 1)]
            self.assertEqual(now, before)
        self.assertGreater(frames, 1)                             # it did not fit one frame's budget
        self.assertGreater(len(session.path), len(before))
        self.assertGreater(session.reached, 4)

class Lua51ShuffleAndApproachTests(ShuffleAndApproachTests):
    RUNTIME = lua51


class FrameBudgetTests(unittest.TestCase):
    """Courseplay-style search across frames (FS25 has no coroutine.yield): a search
    past its frame budget continues next frame from its kept state, the rig braked."""

    WORLD = """
        local tick = ...
        local calls = { halts = 0 }
        local vehicle = {ownerFarmId='1', rootNode=1, maxTurningRadius=6,
            spec_drivable={cruiseControl={speed=40}},
            getLastSpeed=function() return 0 end,
            getIsAIActive=function() return false end,
            getIsEntered=function() return false end,
            getAITurningRadius=function() return 6 end,
            getCruiseControlMaxSpeed=function() return 50 end,
            setCruiseControlMaxSpeed=function() end}
        vehicle.rootVehicle = vehicle
        local foot = {units={vehicle}, lMin=-1, lMax=1, aMin=-2, aMax=2, towLength=0}
        local now = 0
        local env = {getWorldTranslation=function() return 0,0,0 end,
            localDirectionToWorld=function() return 0,0,1 end,
            g_localPlayer={getPosition=function() return 900,0,900 end}}
        if tick then env.getTimeSec = function() now = now + tick; return now end end
        local hitch = {routeFootprint=function() return foot end,
            routePoseClear=function(_,_,x,z) return not (z > 20 and z < 40 and x < 150) end,
            watchDriver=function() end, driverConflict=function() return nil end,
            halt=function() calls.halts = calls.halts + 1 end}
        local args = {tractor=vehicle, farmId='1', x=300, z=60, isReserved=function() return true end}
        return env, args, hitch, calls
    """

    def runtime(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.lines = []
        lua.globals().print = lambda *parts: self.lines.append(" ".join(str(p) for p in parts))
        return lua, lua.execute(SOURCE.read_text())

    @staticmethod
    def points(route):
        return [tuple(round(route[i][k], 6) for k in (1, 2)) for i in range(1, len(route) + 1)]

    def resume(self, drive, env, session, hitch, done, limit=20000):
        for frames in range(limit):
            if done():
                return frames
            self.assertEqual(drive.step(env, session, 16, hitch), "CONTINUE")
        self.fail("search never finished")

    def test_a_long_first_search_continues_over_frames_to_the_same_route(self):
        lua, drive = self.runtime()
        env, args, hitch, _ = lua.execute(self.WORLD, None)
        whole, reason = drive.begin(env, args, hitch)
        self.assertIsNone(reason)
        lua, drive = self.runtime()
        env, args, hitch, calls = lua.execute(self.WORLD, 0.004)
        sliced, reason = drive.begin(env, args, hitch)
        self.assertIsNone(reason)
        self.assertIsNotNone(sliced.episode)            # started, the search not finished
        self.assertIsNone(sliced.corridor)
        self.resume(drive, env, sliced, hitch, lambda: sliced.initial is None)
        self.assertEqual(self.points(sliced.corridor), self.points(whole.corridor))
        self.assertEqual(self.points(sliced.path), self.points(whole.path))
        self.assertGreater(calls.halts, 10)             # braked while it searched
        starts = [l for l in self.lines if "drive starting corridor section" in l]
        self.assertEqual(len(starts), 1, self.lines[-5:])

    def test_stop_while_a_search_continues_ends_it(self):
        lua, drive = self.runtime()
        env, args, hitch, _ = lua.execute(self.WORLD, 0.004)
        session, _ = drive.begin(env, args, hitch)
        self.assertEqual(drive.step(env, session, 16, hitch), "CONTINUE")
        drive.abort(env, session, hitch)
        self.assertEqual(drive.step(env, session, 16, hitch), "STOPPED")

    def test_a_replayed_replan_counts_and_logs_once(self):
        lua, drive = self.runtime()
        env, args, hitch, _ = lua.execute(self.WORLD, None)
        session, _ = drive.begin(env, args, hitch)
        env.getTimeSec = lua.eval("function() local now = 0 return function() now = now + 0.004; return now end end")()
        session.recovering = True
        frames = self.resume(drive, env, session, hitch,
                             lambda: session.episode is None and not session.recovering and session.path is not None)
        self.assertGreater(frames, 2)                   # the replan was replayed across frames
        self.assertEqual(session.replans, 1)
        self.assertEqual(len([l for l in self.lines if "drive replan 1" in l]), 1)

    def test_a_replay_that_asks_for_a_different_search_does_not_reuse_a_kept_one(self):
        lua, drive = self.runtime()
        holder, clock = lua.table(), lua.eval(
            "function() local now = 0 return function() now = now + 0.004; return now end end")()
        targets = [(300, 60), (300, 60), (-300, 60)]
        step = lua.eval("""function(drive, target)
            return function() return drive.globalRoute({0,0,0}, target, function() return true end, 6) end
        end""")
        same, done, frames = lua.eval("function(a, b) return a == b end"), None, 0
        for frames in range(20000):
            result = drive.planned(holder, clock, step(drive, lua.table_from(targets[min(frames, 2)])))
            if not same(result, drive.PENDING):
                done = result
                break
        self.assertGreater(frames, 2)
        self.assertIsNotNone(done)
        self.assertEqual(self.points(done)[-1], (-300.0, 60.0))   # never the kept search to (300, 60)


# Nodes as {x, z, yaw}; GIANTS' Y rotation: local +z is (sin yaw, cos yaw), local +x is (cos yaw, -sin yaw).
RIG = """
local function rotate(n, x, z) return x*math.cos(n.yaw)+z*math.sin(n.yaw), -x*math.sin(n.yaw)+z*math.cos(n.yaw) end
local env = {getWorldTranslation=function(n) return n.x,0,n.z end,
    localDirectionToWorld=function(n,x,y,z) local a,b = rotate(n,x,z); return a,y,b end,
    localToWorld=function(n,x,y,z) local a,b = rotate(n,x,z); return n.x+a,y,n.z+b end,
    getTerrainHeightAtWorldPos=function() return 0 end,
    g_currentMission={terrainRootNode=0},
    CollisionFlag=setmetatable({}, {__index=function() return 1 end})}
local function size(w,l) return {width=w,length=l,height=2,widthOffset=0,lengthOffset=0,heightOffset=0} end
local trailerNode = {x=0,z=-8,yaw=0}
local trailer = {rootNode=trailerNode, steeringAxleNode=trailerNode, spec_wheels={wheels={1,2}}, size=size(2.5,10)}
local tractorNode = {x=0,z=0,yaw=0}
local tractor = {rootNode=tractorNode, size=size(2.5,6),
    getAttachedImplements=function() return {{object=trailer}} end}
return env, tractor, trailerNode, tractorNode
"""


class TowedReverseTests(unittest.TestCase):
    RUNTIME = lua54

    def setUp(self):
        self.lua = self.RUNTIME.LuaRuntime(unpack_returned_tuples=True)
        self.drive = self.lua.execute(SOURCE.read_text())
        self.hitch = self.lua.execute(HITCH.read_text())
        self.env, self.tractor, self.trailer, self.root = self.lua.execute(RIG)

    def footprint(self):
        foot = self.hitch.routeFootprint(self.env, self.tractor)
        self.assertIsNotNone(foot)
        return foot

    def test_a_trailer_left_at_an_angle_is_planned_straight_behind(self):
        # Live 07eb4cc505f04b31-1/-2: the GMC's deckover left jackknifed made the planning box
        # as wide as the trailer is long; every forward search on an open road failed.
        straight = self.footprint()
        box = lambda f: tuple(round(f[k], 6) for k in ("aMin", "aMax", "lMin", "lMax"))
        angle = math.radians(60)
        self.trailer.x, self.trailer.z, self.trailer.yaw = -8 * math.sin(angle), -8 * math.cos(angle), angle
        bent = self.footprint()
        self.assertEqual(box(bent), box(straight))
        self.assertTrue(self.lua.eval("function(a, b) return a == b end")(bent.towed, straight.towed))
        self.assertIsNotNone(bent.towed)
        # A rigid unit beside the tractor's axis (same heading) keeps its observed place.
        self.trailer.x, self.trailer.z, self.trailer.yaw = 3.0, -8.0, 0.0
        self.assertGreater(self.footprint().lMax, straight.lMax + 2.9)

    def reverse(self, steer, metres=32.0, start_hitch=2.0, length=8.0, radius=9.0):
        """Kinematic tractor + semi-trailer (hitch at the tractor's root, axle `length` behind)
        reversing along the line it started on; returns the largest hitch angle (degrees)
        and the trailer's largest distance from the line."""
        line = self.lua.table_from({"x": 0.0, "z": 0.0, "yaw": 0.0})
        foot = self.lua.table_from({"towed": self.lua.table_from({"rootNode": self.trailer}), "towLength": length})
        yaw, theta, x, z, worst, drift = 0.0, math.radians(start_hitch), 0.0, 0.0, 0.0, 0.0
        ds = 0.05
        for _ in range(int(metres / ds)):
            self.trailer.x, self.trailer.z = x - length * math.sin(theta), z - length * math.cos(theta)
            self.trailer.yaw = theta
            lx, lz = steer(line, foot, yaw)
            # Toward the local aim point (pure pursuit, 10 m) at most a 6 m radius: aiming
            # at the tractor's local -x while reversing turns it to a larger yaw.
            curvature = max(-1 / radius, min(1 / radius, -2 * lx / (lx * lx + lz * lz)))
            x, z = x - ds * math.sin(yaw), z - ds * math.cos(yaw)
            yaw += ds * curvature
            theta += -ds * math.sin(yaw - theta) / length
            worst = max(worst, abs(math.degrees(theta - yaw)))
            drift = max(drift, abs(self.trailer.x))
        return worst, drift

    def towed_reverse(self, start_deg, length, held=True, metres=32.0, radius=9.0):
        """The live reverse in the same kinematics: on the towed unit's own line
        (Drive.towedLine), steered by reverseAim, judged by reverseOffLine every step.
        Returns where it stopped and why (None: the whole leg), the largest hitch
        angle (degrees), the trailer's largest distance from its line, the end hitch,
        and where the old rule (the tractor 0.75 m or 15 degrees off its own line) stopped."""
        foot = self.lua.table_from({"towed": self.lua.table_from({"rootNode": self.trailer}), "towLength": length})
        yaw, theta, x, z = 0.0, math.radians(start_deg), 0.0, 0.0
        self.trailer.x, self.trailer.z = x - length * math.sin(theta), z - length * math.cos(theta)
        self.trailer.yaw = theta
        line = self.drive.towedLine(self.env, foot)
        r = self.lua.table()
        ds, travelled, stop, old, worst, drift = 0.05, 0.0, None, None, 0.0, 0.0
        wrap = lambda a: (a + math.pi) % (2 * math.pi) - math.pi
        for _ in range(int(metres / ds)):
            self.trailer.x, self.trailer.z = x - length * math.sin(theta), z - length * math.cos(theta)
            self.trailer.yaw = theta
            lx, lz, hitch, cross, orient = self.drive.reverseAim(self.env, foot, line, yaw, radius)
            if old is None and (abs(x) > 0.75 or abs(yaw) > math.pi / 12):
                old = travelled
            lateral = abs((x - line.x) * math.cos(line.yaw) - (z - line.z) * math.sin(line.yaw))
            why = self.drive.reverseOffLine(r, lateral, wrap(yaw - line.yaw), self.lua.table_from(
                {"cross": cross, "orient": orient, "hitch": hitch}))
            if why:
                stop = (travelled, why)
                break
            if not held:
                lx, lz = 0.0, -10.0
            curvature = max(-1 / radius, min(1 / radius, -2 * lx / (lx * lx + lz * lz)))
            x, z = x - ds * math.sin(yaw), z - ds * math.cos(yaw)
            yaw += ds * curvature
            theta += -ds * math.sin(yaw - theta) / length
            travelled += ds
            worst = max(worst, abs(math.degrees(theta - yaw)))
            drift = max(drift, abs(cross))
        return stop, worst, drift, abs(math.degrees(theta - yaw)), old

    def test_a_trailer_left_angled_reverses_the_whole_leg_on_its_own_line_and_straightens(self):
        # Live: every reverse with an angled trailer stopped on the tractor's 15 degrees after
        # 0.2-7.6 m (7fd526481ca24c1c-2, 3703bb62ebd94f1b-1), hitch 0.08-0.15 rad.
        for length in (6.0, 8.0, 12.0, 15.0):
            for start in (5.0, 10.0, 15.0):
                stop, worst, drift, end, old = self.towed_reverse(start, length)
                self.assertIsNone(stop, (length, start, stop))
                self.assertLess(old, 16, (length, start))               # the old rule stopped it early
                self.assertLessEqual(worst, start + 0.5, (length, start))   # never more bent than it began
                self.assertLess(drift, 0.75, (length, start))
                self.assertLess(end, 2, (length, start))                # straight at the end
        # A straight wheel jackknifes; the hitch guard stops it first.
        stop, worst, _, _, _ = self.towed_reverse(10.0, 8.0, held=False)
        self.assertIsNotNone(stop)
        self.assertLess(worst, 40)

    def test_the_terrain_under_the_route_box_is_measured_for_grade_climb_tilt_and_steps(self):
        # Measurement only: the data the slope limits are to be set from.
        foot = self.footprint()
        def profile(height):
            self.env.getTerrainHeightAtWorldPos = self.lua.eval(
                "function(f) return function(_, x, _, z) return f(x, z) end end")(height)
            p = self.hitch.terrainProfile(self.env, foot, 0, 0, 0, self.root)
            return {k: p[k] for k in ("grade", "climb", "cross", "step", "pitch", "roll")}
        flat = profile(lambda x, z: 0.0)
        self.assertEqual((flat["grade"], flat["climb"], flat["cross"], flat["step"]), (0, 0, 0, 0))
        self.assertEqual((flat["pitch"], flat["roll"]), (0, 0))
        ramp = profile(lambda x, z: z * math.tan(math.radians(10)))
        self.assertAlmostEqual(ramp["grade"], 10, places=6)
        self.assertAlmostEqual(ramp["climb"], 10, places=6)
        self.assertAlmostEqual(ramp["cross"], 0, places=6)
        tilt = profile(lambda x, z: x * math.tan(math.radians(6)))
        self.assertAlmostEqual(tilt["cross"], 6, places=6)
        self.assertAlmostEqual(tilt["grade"], 0, places=6)
        bank = profile(lambda x, z: 0.5 if z > 2.2 else 0.0)
        self.assertAlmostEqual(bank["step"], 0.5)
        self.assertGreater(bank["climb"], 14)

    def test_a_turning_trailer_widens_the_route_box_on_the_inside_of_the_turn_only(self):
        # Live 83e039589c2743dc-2: widened by the full off-tracking on both sides, the GMC rig's
        # box on any arc was ~12 m wide and no shuffle fitted the lane it stood in.
        boxes = []
        self.lua.execute("""
            local env, boxes = ...
            env.getWaterYAtWorldPosition = nil
            env.g_currentMission.environmentAreaSystem = {getWaterYAtWorldPosition=function() return nil end}
            env.g_currentMission.getNodeObject = function() return nil end
            env.overlapBox = function(x,y,z,rx,ry,rz,hw,hh,hl) boxes(x,z,hw) return 0 end
        """, self.env, lambda x, z, hw: boxes.append((x, z, hw)))
        foot = self.footprint()
        edges = {}
        for curve in (0.0, 1 / 11.2, -1 / 11.2):
            boxes.clear()
            self.hitch.routePoseClear(self.env, foot, 0, 0, 0, curve)
            x, _, hw = boxes[0]
            edges[curve] = (round(x - hw, 6), round(x + hw, 6))
        straight, right, left = edges[0.0], edges[1 / 11.2], edges[-1 / 11.2]
        self.assertEqual(right[0], straight[0])       # turning to +x: the outer (-x) edge stays
        self.assertGreater(right[1], straight[1])     # the inside grows by the off-tracking
        self.assertEqual(left[1], straight[1])
        self.assertLess(left[0], straight[0])
        offtrack = 11.2 - math.sqrt(11.2 ** 2 - foot.towLength ** 2)
        self.assertAlmostEqual(right[1] - straight[1], min(foot.towLength, offtrack), places=5)

    def test_a_scenery_node_a_departure_leaves_is_not_an_obstacle_to_it(self):
        self.lua.execute("""
            local env = ...
            env.g_currentMission.environmentAreaSystem = {getWaterYAtWorldPosition=function() return nil end}
            env.g_currentMission.getNodeObject = function() return nil end
            env.g_currentMission.terrainRootNode = "terrain"
            env.overlapBox = function(x,y,z,rx,ry,rz,hw,hh,hl,cb,q)
                q[cb](q, "terrain"); q[cb](q, 77); return 2
            end
        """, self.env)
        foot = self.footprint()
        self.assertEqual(self.hitch.routePoseClear(self.env, foot, 0, 0, 0)[0], False)
        foot.excluded[77] = True
        self.assertEqual(self.hitch.routePoseClear(self.env, foot, 0, 0, 0)[0], True)

    def test_a_bridge_deck_is_the_surface_where_the_terrain_below_is_a_riverbed(self):
        # Live 5aafb45731a844eb-1: read as terrain alone, a bridge was a cliff over a riverbed
        # (GRADE) and no route from the farm reached the town across the river.
        rays = []
        foot = self.footprint()
        self.lua.execute("""
            local env, rays = ...
            -- A river 10 m below the road for |z| < 30, water 2 m deep; a deck (ROAD) over |x| < 4.
            env.getTerrainHeightAtWorldPos = function(_, x, _, z) return math.abs(z) < 30 and -10 or 0 end
            env.g_currentMission.environmentAreaSystem = {getWaterYAtWorldPosition=function(_, x, y, z)
                if math.abs(z) < 30 then return -8 end end}
            env.g_currentMission.getNodeObject = function() return nil end
            env.overlapBox = function() return 0 end
            env.CollisionFlag.ROAD = 8192
            env.raycastClosest = function(x, y, z, dx, dy, dz, length, cb, target, mask)
                rays(y, dy, length, mask)
                if math.abs(x) < 4 and math.abs(z) < 34 then target[cb](target, 5, x, 0.0, z, y) return 1 end
                return 0
            end
        """, self.env, lambda y, dy, length, mask: rays.append((y, dy, length, mask)))
        on = self.hitch.routePoseClear(self.env, foot, 0, 0, 0, 0)
        self.assertEqual(on[0], True, on)
        self.assertEqual(rays[0][1:], (-1, 26, 8192))       # straight down from 25 m, roads only
        beside = self.hitch.routePoseClear(self.env, foot, 12, 0, 0, 0)
        self.assertEqual((beside[0], beside[1]), (False, "WATER"))
        self.lua.execute("local env = ...; env.raycastClosest = nil", self.env)
        self.assertEqual(self.hitch.routePoseClear(self.env, foot, 0, 0, 0, 0)[0], False)   # terrain alone

    def test_a_point_the_rig_was_held_fast_at_blocks_the_route_box_that_reaches_it(self):
        self.lua.execute("""
            local env = ...
            env.g_currentMission.environmentAreaSystem = {getWaterYAtWorldPosition=function() return nil end}
            env.g_currentMission.getNodeObject = function() return nil end
            env.overlapBox = function() return 0 end
        """, self.env)
        foot = self.footprint()
        foot.held = self.lua.eval("function(z) return {{0, z}} end")(foot.aMax + 0.75)
        here = self.hitch.routePoseClear(self.env, foot, 0, 0, 0, 0)
        on = self.hitch.routePoseClear(self.env, foot, 0, 1, 0, 0)
        beside = self.hitch.routePoseClear(self.env, foot, 6, 1, 0, 0)
        self.assertNotIn("HELD_FAST", str(here[1]))
        self.assertEqual(on[0], False)
        self.assertIn("HELD_FAST", on[1])
        self.assertNotIn("HELD_FAST", str(beside[1]))

    def test_a_reverse_holds_a_towed_trailer_in_line_where_a_straight_wheel_jackknifes(self):
        # Live dee63f6c79b24196-4: a straight-wheel 32 m escape left the deckover jackknifed.
        straight = lambda line, foot, yaw: (0.0, -10.0)
        held = lambda line, foot, yaw: self.drive.reverseAim(self.env, foot, line, yaw, 9.0)[:2]
        self.assertGreater(self.reverse(straight)[0], 30)
        for length in (6.0, 8.0, 12.0, 15.0):
            worst, drift = self.reverse(held, length=length)
            self.assertLess(worst, 5, length)
            self.assertLess(drift, 0.75, length)
        self.assertEqual(tuple(self.drive.reverseAim(self.env, None, self.lua.table_from(
            {"x": 0, "z": 0, "yaw": 0}), 0)[:2]), (0, -10))   # nothing towed: straight back


class Lua51TowedReverseTests(TowedReverseTests):
    RUNTIME = lua51


if __name__ == "__main__":
    unittest.main()
