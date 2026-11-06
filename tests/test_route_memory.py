import json
import math
import tempfile
import unittest
import unittest.mock
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


class LearnedRouteTests(unittest.TestCase):
    """Taught routes (owner directive 2026-11-06, section 6): observed lines, quality kept apart
    from validation, validation per direction and per rig size."""

    def memory(self, folder):
        return RouteMemory(Path(folder) / "routes.json")

    def test_a_taught_line_is_validated_per_direction_and_rig_size(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = self.memory(folder)
            route_id = memory.add("m", "s", [(0, 0), (0, 40), (0, 80)], source="player", quality="clean",
                                  rig=(2.4, 6.0))
            pickup, lowboy = (2.4, 6.0), (3.4, 22.0)
            # Usable both ways as a hint, never validated by being recorded.
            self.assertEqual(memory.choose("m", "s", (0, -3), (0, 100), pickup)[2], False)
            self.assertEqual(memory.choose("m", "s", (0, 83), (0, -20), pickup)[2], True)
            memory.promote(route_id, reverse=False, rig=pickup)
            route = memory._read()[0]
            self.assertEqual(route["quality"], "validated")
            self.assertEqual(route["validated"], {"forward": [[2.4, 6.0]], "reverse": []})
            # Proven forward with the pickup: not proven in reverse, nor for the lowboy.
            memory.promote(route_id, reverse=True, rig=lowboy)
            self.assertEqual(memory._read()[0]["validated"]["reverse"], [[3.4, 22.0]])
            self.assertEqual(memory._read()[0]["validated"]["forward"], [[2.4, 6.0]])

    def test_the_route_expected_to_finish_soonest_is_chosen(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = self.memory(folder)
            slow = memory.add("m", "s", [(0, 0), (0, 50), (0, 100)], source="player",
                              poses=[(0, 8, 0)] * 3, rig=(2.4, 6.0))
            fast = memory.add("m", "s", [(1, 0), (6, 50), (1, 100)], source="player",
                              poses=[(0, 40, 0)] * 3, rig=(2.4, 6.0))
            self.assertEqual(memory.choose("m", "s", (0, -2), (0, 110), (2.4, 6.0))[0], fast)
            # Validation weighs (UNVALIDATED_FACTOR), it does not override: five times faster still wins.
            memory.promote(slow, rig=(2.4, 6.0))
            self.assertEqual(memory.choose("m", "s", (0, -2), (0, 110), (2.4, 6.0))[0], fast)

    def test_names_rename_delete_and_a_bounded_store(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = self.memory(folder)
            first = memory.add("m", "s", [(0, 0), (0, 30)], name="Farm to Shop", source="player")
            again = memory.add("m", "s", [(0, 0), (0, 40)], name="farm to shop", source="player")
            self.assertEqual([r["id"] for r in memory.listed("m", "s")], [again])     # the name saved again
            self.assertNotEqual(first, again)
            self.assertTrue(memory.rename(again, "Shop run"))
            self.assertEqual(memory.named("m", "s", "shop  RUN")["id"], again)
            self.assertTrue(memory.delete(again))
            self.assertEqual(memory.listed("m", "s"), [])
            from companion import route_memory
            with unittest.mock.patch.object(route_memory, "MAX_ROUTES", 3):
                kept = memory.add("m", "s", [(0, 0), (0, 30)], name="kept", source="player")
                for n in range(4):
                    memory.add("m", "s", [(n, 0), (n, 30)], source="journey")
                ids = [r["id"] for r in memory.listed("m", "s")]
            self.assertEqual(len(ids), 3)
            self.assertIn(kept, ids)

    def test_a_driven_line_keeps_its_turns_reversing_and_stops_and_drops_straight_samples(self):
        from companion.route_memory import simplify
        samples = [(0.0, z * 1.0, 0.0, 20.0, False, z * 0.2) for z in range(0, 100)]        # 100 m north
        samples += [(a * 1.0, 99.0 + a * 0.0, math.pi / 2, 20.0, False, 20 + a * 0.2) for a in range(1, 40)]
        samples += [(39.0, 99.0, math.pi / 2, 0.0, False, 30 + t) for t in range(6)]      # a 6 s stop
        samples += [(39.0 - r, 99.0, math.pi / 2, 5.0, True, 40 + r) for r in range(1, 10)]   # backing west
        kept = simplify(samples)
        self.assertLess(len(kept), 25)
        self.assertEqual(kept[0], samples[0])
        self.assertEqual(kept[-1], samples[-1])
        self.assertTrue(any(abs(k[2] - math.pi / 2) < 1e-6 and k[1] == 99.0 for k in kept))     # the corner
        self.assertTrue(any(k[4] for k in kept) and any(k[3] == 0.0 for k in kept))
        gaps = [math.dist(a[:2], b[:2]) for a, b in zip(kept, kept[1:])]
        self.assertLessEqual(max(gaps), 30.0 + 1e-6)

    def test_recording_the_players_drive_saves_the_observed_line(self):
        from companion import route_memory
        from companion.route_memory import RouteRecorder

        class World:
            def __init__(self):
                self.pos, self.heading, self.moving = (0.0, 0.0), 0.0, True

            def snapshot(self):
                return [SimpleNamespace(id="car", position=self.pos, heading=self.heading, stopped=not self.moving,
                                        ridden=True, name="GMC", size=(6.0, 2.4, 2.0, 0.0), attached_to=None,
                                        belted_to=None),
                        SimpleNamespace(id="other", position=(50.0, 50.0), heading=0.0, stopped=True, ridden=False,
                                        name="3650", size=None, attached_to=None, belted_to=None)]

            def map_id(self):
                return "m"

            def farm(self):
                return SimpleNamespace(savegame_dir="s", savegame=1)

        with tempfile.TemporaryDirectory() as folder, \
                unittest.mock.patch.object(route_memory, "SAMPLE_S", 0.005):
            world, memory = World(), self.memory(folder)
            recorder = RouteRecorder(world, memory)
            self.assertEqual(recorder.start("player", name="Farm to Shop"), "ROUTE_RECORDING")
            self.assertEqual(recorder.start("player"), "ROUTE_ALREADY_RECORDING")
            import time
            for z in range(0, 60, 2):
                world.pos = (0.0, float(z))
                time.sleep(0.02)
            notice, route_id = recorder.finish()
            self.assertIn(notice, ("ROUTE_SAVED_CLEAN", "ROUTE_SAVED"))
            route = memory.named("m", "s#1", "farm to shop")
            self.assertEqual(route["id"], route_id)
            self.assertEqual(route["source"], "player")
            self.assertEqual(route["rig"], [2.4, 6.0])
            self.assertEqual(route["validated"], {"forward": [], "reverse": []})   # recorded is not validated
            self.assertGreater(route["meta"]["length_m"], 40)
            self.assertEqual(route["meta"]["vehicle"], "GMC")
            self.assertEqual(recorder.finish()[0], "ROUTE_NOT_RECORDING")
            recorder.start("player")
            self.assertEqual(recorder.discard(), "ROUTE_DISCARDED")
            self.assertEqual(len(memory.listed("m", "s#1")), 1)


class RouteCommandTests(unittest.TestCase):
    """The player's words for routes reach the recorder and the store, never a machine."""

    def coordinator(self, folder):
        from companion.coordinator import Coordinator
        notices = []

        class World:
            def snapshot(self):
                return []

            def map_id(self):
                return "m"

            def farm(self):
                return SimpleNamespace(savegame_dir="s", savegame=1)

        c = Coordinator(World(), None, lambda n, spoken=False: notices.append(n), lambda: "1", lambda: "x")
        c.route_memory = RouteMemory(Path(folder) / "routes.json")
        return c, notices

    def test_route_words_are_understood(self):
        from companion.language import RuleInterpreter
        said = RuleInterpreter().interpret
        self.assertEqual(said("Farmhand, record this route."), {"version": 1, "command": "route", "action": "start"})
        self.assertEqual(said("Stop recording and save.")["action"], "save")          # never STOP
        self.assertEqual(said("Save this route as Farm to Shop.")["name"], "farm to shop")
        self.assertEqual(said("Discard that recording.")["action"], "discard")
        self.assertEqual(said("stop"), {"version": 1, "command": "stop"})
        self.assertEqual(said("Take the GMC to the farm and remember the route.")["remember_route"], True)
        self.assertEqual(said("Take the Ford to the mechanic. Don't forget the way.")["tractor_name"], "ford")
        self.assertEqual(said("GMC, follow me and remember this route."),
                         {"version": 1, "command": "follow_me", "tractor_name": "gmc", "remember_route": True})
        from companion.coordinator import COMMAND_VALIDATOR
        for words in ("record the way to the mechanic", "rename route a to b", "list my routes",
                      "take the gmc to the farm and remember the route", "follow me and learn the way"):
            self.assertTrue(COMMAND_VALIDATOR.is_valid(said(words)), words)

    def test_rename_delete_and_count_through_the_coordinator(self):
        with tempfile.TemporaryDirectory() as folder:
            c, notices = self.coordinator(folder)
            c.route_memory.add("m", "s#1", [(0, 0), (0, 30)], name="farm to shop", source="player")
            c.submit({"version": 1, "command": "route", "action": "rename", "name": "the barn",
                      "new_name": "shop"})
            self.assertEqual(notices[-1], "ROUTE_NOT_FOUND")
            c.submit({"version": 1, "command": "route", "action": "rename", "name": "farm to shop",
                      "new_name": "shop run"})
            self.assertEqual(notices[-1], "ROUTE_RENAMED")
            c.submit({"version": 1, "command": "route", "action": "list"})
            self.assertEqual(notices[-1], "ROUTES_SAVED_1")
            c.submit({"version": 1, "command": "route", "action": "delete", "name": "shop run"})
            self.assertEqual(notices[-1], "ROUTE_DELETED")
            c.submit({"version": 1, "command": "route", "action": "save"})
            self.assertEqual(notices[-1], "ROUTE_NOT_RECORDING")

    def test_an_ambiguous_rename_splits_where_a_route_is_named(self):
        with tempfile.TemporaryDirectory() as folder:
            c, notices = self.coordinator(folder)
            route_id = c.route_memory.add("m", "s#1", [(0, 0), (0, 30)], name="farm to shop", source="player")
            c.submit({"version": 1, "command": "route", "action": "rename", "name": "farm",
                      "new_name": "shop to shop run"})
            self.assertEqual(notices[-1], "ROUTE_RENAMED")
            self.assertEqual(c.route_memory._read()[0]["name"], "shop run")
            self.assertEqual(c.route_memory._read()[0]["id"], route_id)


if __name__ == "__main__":
    unittest.main()
