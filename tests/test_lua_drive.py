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


if __name__ == "__main__":
    unittest.main()
