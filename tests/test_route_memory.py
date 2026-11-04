import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from companion.route_memory import RouteMemory


class RouteMemoryTests(unittest.TestCase):
    def test_walked_line_is_scoped_to_the_save_and_reused_in_either_direction(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = RouteMemory(Path(folder) / "routes.json")
            route_id = memory.add("map-a", "save-1", [(0, 0), (0, 12), (5, 25), (20, 32)])
            self.assertEqual(memory.choose("map-a", "save-1", (1, -2), (34, 30))[0], route_id)
            self.assertEqual(memory.choose("map-a", "save-1", (20, 33), (0, -10))[1][-1], (0.0, 0.0))
            self.assertIsNone(memory.choose("map-b", "save-1", (1, -2), (34, 30)))
            self.assertIsNone(memory.choose("map-a", "save-2", (1, -2), (34, 30)))
            self.assertIsNone(memory.choose("map-a", "save-1", (200, 0), (34, 30)))
            self.assertIsNone(memory.choose("map-a", "save-1", (1, -2), (-50, -2)))
            # A rig can join the line after being rerouted away from the yard.
            self.assertEqual(memory.choose("map-a", "save-1", (100, -20), (-90, 20))[0], route_id)

    def test_malformed_or_short_routes_cannot_be_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "routes.json"
            memory = RouteMemory(path)
            with self.assertRaises(ValueError):
                memory.add("map", "save", [(0, 0), (1, 1)])
            path.write_text(json.dumps({"routes": [{"map": "map", "save": "save", "points": [[0, 0], ["bad", 12]]}]}))
            self.assertIsNone(memory.choose("map", "save", (0, 0), (12, 0)))

    def test_repeatedly_failed_unverified_line_is_kept_but_not_auto_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = RouteMemory(Path(folder) / "routes.json")
            route_id = memory.add("map", "save", [(0, 0), (0, 15), (0, 30)])
            memory.reject(route_id)
            self.assertIsNotNone(memory.choose("map", "save", (0, -2), (0, 60)))
            memory.reject(route_id)
            self.assertIsNone(memory.choose("map", "save", (0, -2), (0, 60)))
            self.assertEqual(memory._read()[0]["id"], route_id)

    def test_a_normal_transport_goal_follows_a_taught_line_before_other_navigation(self):
        from companion.drive_goal import DriveGoal
        from companion.standalone import StandaloneMove

        with tempfile.TemporaryDirectory() as folder:
            memory = RouteMemory(Path(folder) / "routes.json")
            memory.add("map-a", "save-1#1", [(0, 0), (0, 12), (4, 25), (15, 32)])

            class World:
                def __init__(self):
                    self.pos = (0.0, -2.0)
                    self.drives = []

                def snapshot(self):
                    return [SimpleNamespace(id="truck", position=self.pos, belted_to=None),
                            SimpleNamespace(id="deck", position=self.pos, belted_to=None),
                            SimpleNamespace(id="cargo", position=self.pos, belted_to="deck")]

                def map_id(self):
                    return "map-a"

                def farm(self):
                    return SimpleNamespace(savegame_dir="save-1", savegame=1)

                def farm_drive(self, vehicle_id, point, owner, area=0.0, yaw=None, via=None):
                    self.drives.append((tuple(point), via))
                    self.pos = tuple(point)

                def navigate(self, *args):
                    raise AssertionError("the taught route should be driven first")

            world = World()
            move = StandaloneMove(world, memory)
            move._drive(DriveGoal("truck", ("truck", "deck", "cargo"), (30, 31), "cargo"), "job", 0)
            self.assertEqual(world.drives[0], ((15.0, 32.0), [(0.0, 0.0), (0.0, 12.0), (4.0, 25.0)]))
            self.assertEqual(world.drives[-1][0], (30, 31))
            self.assertIsNotNone(move._route_used)

    def test_failed_walked_route_is_not_retried_during_the_same_job(self):
        from adapters.world import StepFailed
        from companion.drive_goal import DriveGoal
        from companion.standalone import StandaloneMove

        with tempfile.TemporaryDirectory() as folder:
            memory = RouteMemory(Path(folder) / "routes.json")
            memory.add("map-a", "save-1#1", [(0, 0), (0, 15), (0, 30)])

            class World:
                pos = (0.0, -2.0)
                guided = 0
                native = 0

                def snapshot(self):
                    return [SimpleNamespace(id="truck", position=self.pos, belted_to=None),
                            SimpleNamespace(id="cargo", position=self.pos, belted_to="truck")]

                def map_id(self):
                    return "map-a"

                def farm(self):
                    return SimpleNamespace(savegame_dir="save-1", savegame=1)

                def farm_drive(self, vehicle_id, point, owner, area=0, yaw=None, via=None):
                    if via:
                        self.guided += 1
                        raise StepFailed("NO_ROUTE")
                    self.pos = tuple(point)

                def navigate(self, vehicle_id, pose, owner):
                    self.native += 1
                    self.pos = (pose.x, pose.z)

            world = World()
            move = StandaloneMove(world, memory)
            goal = DriveGoal("truck", ("truck", "cargo"), (0, 65), "cargo")
            move._drive(goal, "job", 0)
            world.pos = (0.0, -2.0)  # a later recovery attempt in this job
            move._drive(goal, "job", 0)
            self.assertEqual((world.guided, world.native), (1, 2))

    def test_native_navigation_aims_near_a_place_instead_of_its_invalid_centre(self):
        from adapters.world import StepFailed
        from companion.standalone import StandaloneMove

        class World:
            pos = (0.0, 0.0)
            attempts = []

            def snapshot(self):
                return [SimpleNamespace(id="truck", position=self.pos)]

            def navigate(self, vehicle_id, pose, owner):
                self.attempts.append((pose.x, pose.z))
                if (pose.x, pose.z) == (100, 0):
                    raise StepFailed("NAVIGATE_GOTO_INVALID")
                self.pos = (pose.x, pose.z)

        world = World()
        move = StandaloneMove(world)
        move._area = 40
        move._long_leg("truck", (100, 0), "job")
        self.assertEqual(len(world.attempts), 1)
        self.assertGreater(world.pos[0], 60)
        self.assertLess(math.dist(world.pos, (100, 0)), 40)

        world.pos = (0.0, 0.0)
        world.attempts = []
        original = world.navigate
        def first_approach_blocked(vehicle_id, pose, owner):
            if not world.attempts:
                world.attempts.append((pose.x, pose.z))
                raise StepFailed("NAVIGATE_GOTO_INVALID")
            return original(vehicle_id, pose, owner)
        world.navigate = first_approach_blocked
        move._long_leg("truck", (100, 0), "job")
        self.assertEqual(len(world.attempts), 2)

        world.pos = (0.0, 0.0)
        world.attempts = []
        def first_approach_ends_without_motion(vehicle_id, pose, owner):
            if not world.attempts:
                world.attempts.append((pose.x, pose.z))
                return
            return original(vehicle_id, pose, owner)
        world.navigate = first_approach_ends_without_motion
        move._long_leg("truck", (100, 0), "job")
        self.assertEqual(len(world.attempts), 2)


if __name__ == "__main__":
    unittest.main()
