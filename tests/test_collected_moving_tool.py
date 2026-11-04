"""Regression for GIANTS Cylindered's modulo rotation at the zero boundary."""

from pathlib import Path
import unittest

from lupa import LuaRuntime


SOURCE = Path(__file__).resolve().parents[1] / "collected/proven_systems/bounded_moving_tool.lua"


class CollectedMovingToolTest(unittest.TestCase):
    def test_native_acceleration_is_braked_before_settled_target(self):
        # Cat 395 live: clearing the target at its angle left ~0.019 rad of
        # post-target travel. Simulate Cylindered's observed speed ramp/coast.
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("function getRotation(node) return node.angle, 0, 0 end")
        control = lua.execute(SOURCE.read_text())
        vehicle = lua.execute("""return {
            isServer=true, motor=true,
            getIsMovingToolActive=function() return true end,
            getIsAIActive=function() return false end,
            getLastSpeed=function() return 0 end,
            getIsMotorStarted=function(self) return self.motor end,
            startMotor=function(self) self.motor=true end,
            stopMotor=function(self) self.motor=false end,
            raiseActive=function() end
        }""")
        node = lua.table_from({"angle": 0.09173})
        tool = lua.table_from({"node": node, "rotationAxis": 1,
                               "rotSpeed": 0.0003490658503988659,
                               "rotAcceleration": 8.377580409572782e-7,
                               "lastRotSpeed": 0.0,
                               "rotMin": -0.2, "rotMax": 0.4})
        target = node["angle"] + 0.02
        session = control.begin(vehicle, tool, target)
        self.assertIsNotNone(session)
        for _ in range(250):
            result = control.step(session, 16)
            state = result[0] if isinstance(result, tuple) else result
            if state != "RUNNING":
                break
            desired = (tool["curTargetDirection"] or 0) * tool["rotSpeed"]
            previous = tool["lastRotSpeed"]
            change = tool["rotAcceleration"] * 16
            speed = min(desired, previous + change) if previous < desired else max(desired, previous - change)
            tool["lastRotSpeed"] = speed
            node["angle"] += speed * 16
        self.assertEqual(state, "DONE", result)
        self.assertLessEqual(abs(node["angle"] - target), 0.002)

    def test_limited_joint_keeps_native_normalized_target(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("function getRotation(node) return node.angle, 0, 0 end")
        control = lua.execute(SOURCE.read_text())
        vehicle = lua.execute("""return {
            isServer=true, motor=true,
            getIsMovingToolActive=function() return true end,
            getIsAIActive=function() return false end,
            getLastSpeed=function() return 0 end,
            getIsMotorStarted=function(self) return self.motor end,
            startMotor=function(self) self.motor=true end,
            stopMotor=function(self) self.motor=false end,
            raiseActive=function() end
        }""")
        node = lua.table_from({"angle": -0.65})
        tool = lua.table_from({"node": node, "rotationAxis": 1,
                               "rotSpeed": 0.0002, "rotMin": -0.8, "rotMax": 0.2})
        session = control.begin(vehicle, tool, -0.63)
        self.assertIsNotNone(session)
        for _ in range(100):
            result = control.step(session, 20)
            state = result[0] if isinstance(result, tuple) else result
            if state == "DONE":
                break
            if tool["curTargetPosition"] is not None:
                self.assertEqual(tool["curTargetDirection"], 1)
                self.assertAlmostEqual(tool["curTargetPosition"], 0.17)
                node["angle"] += 0.004
        self.assertEqual(state, "DONE")

    def test_unlimited_joint_crosses_zero_in_either_direction(self):
        for start, target, direction in ((0.01, -0.01, -1),
                                         (6.273185307179586, 6.293185307179586, 1)):
            with self.subTest(start=start, target=target):
                lua = LuaRuntime(unpack_returned_tuples=True)
                lua.execute("function getRotation(node) return 0, node.angle, 0 end")
                control = lua.execute(SOURCE.read_text())
                vehicle = lua.execute("""return {
                    isServer=true, motor=true,
                    getIsMovingToolActive=function() return true end,
                    getIsAIActive=function() return false end,
                    getLastSpeed=function() return 0 end,
                    getIsMotorStarted=function(self) return self.motor end,
                    startMotor=function(self) self.motor=true end,
                    stopMotor=function(self) self.motor=false end,
                    raiseActive=function() end
                }""")
                node = lua.table_from({"angle": start})
                tool = lua.table_from({"node": node, "rotationAxis": 2,
                                       "rotSpeed": 0.0006})
                session = control.begin(vehicle, tool, target)
                self.assertIsNotNone(session)
                state = None
                for _ in range(100):
                    result = control.step(session, 20)
                    state = result[0] if isinstance(result, tuple) else result
                    if state == "DONE":
                        break
                    self.assertEqual(state, "RUNNING")
                    if tool["curTargetPosition"] is not None:
                        self.assertEqual(tool["curTargetDirection"], direction)
                        self.assertGreaterEqual(tool["curTargetPosition"], 0)
                        self.assertLess(tool["curTargetPosition"], 2 * 3.141592653589793)
                        node["angle"] = (node["angle"] + direction * 0.004) % (2 * 3.141592653589793)
                self.assertEqual(state, "DONE")
                self.assertIsNone(tool["curTargetPosition"])

    def test_continued_motion_after_target_is_reported_as_failure(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("function getRotation(node) return node.angle, 0, 0 end")
        control = lua.execute(SOURCE.read_text())
        vehicle = lua.execute("""return {
            isServer=true, motor=true,
            getIsMovingToolActive=function() return true end,
            getIsAIActive=function() return false end,
            getLastSpeed=function() return 0 end,
            getIsMotorStarted=function(self) return self.motor end,
            startMotor=function(self) self.motor=true end,
            stopMotor=function(self) self.motor=false end,
            raiseActive=function() end
        }""")
        node = lua.table_from({"angle": 0.01})
        tool = lua.table_from({"node": node, "rotationAxis": 1,
                               "rotSpeed": 0.0006, "rotMin": 0, "rotMax": 1})
        session = control.begin(vehicle, tool, 0.03)
        self.assertIsNotNone(session)
        coast_frames = 0
        for _ in range(100):
            result = control.step(session, 20)
            state = result[0] if isinstance(result, tuple) else result
            if state == "FAILED":
                self.assertEqual(result[1]["reason"], "TARGET_MISSED")
                self.assertGreater(result[1]["error"], 0.005)
                break
            self.assertEqual(state, "RUNNING")
            if tool["curTargetPosition"] is not None:
                node["angle"] += 0.004
            elif coast_frames < 4:
                node["angle"] += 0.003
                coast_frames += 1
        self.assertEqual(state, "FAILED")


if __name__ == "__main__":
    unittest.main()
