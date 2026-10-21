"""The executor's answer to a GOTO GIANTS took but could not start (UNPARK, AGENTS.md
exception 4) on the mock world (simulator/world.py: a "boxed" vehicle is a mock-only
stand-in for GIANTS' agent staying BLOCKED; nothing here is GIANTS behaviour). Never a
closed door (Farmhand does not open building doors), else the bounded extraction, then
GIANTS once more, which must move it; at most once per vehicle per job. The extraction itself is tests/test_lua_unpark.py.
"""
from dataclasses import replace
import math
import random
import threading
import unittest

from adapters.world import StepFailed
from companion.executor import Executor
from protocol.model import Pose
from simulator.world import MockWorld
from tests.test_farmhand import GOTO_TRACTOR, PLAYERS, hitched_pair, intent, move, simple_world

SHED = {"exit": 18.5}


def boxed_pair(seed, box=SHED, gates=()):
    """A hitched pair standing (mock-)boxed in, with decoys, IDs and order varied by seed."""
    rng = random.Random(seed)
    pair = hitched_pair(f"t-{rng.randrange(10**6)}", f"w-{rng.randrange(10**6)}", (130.0, 260.0))
    decoys = hitched_pair(f"t-{rng.randrange(10**6)}", f"w-{rng.randrange(10**6)}", (-600.0, 900.0))
    items = [*pair, *decoys]
    rng.shuffle(items)
    world = MockWorld(items, PLAYERS, ())
    world.boxed = {pair[0].id: dict(box)}
    world.gates = tuple(gates)
    return world, pair


class DeliverFromAShedTests(unittest.TestCase):
    def test_a_pair_boxed_in_a_shed_is_driven_out_and_delivered(self):
        for seed in range(6):
            with self.subTest(seed=seed):
                world, (tractor, wagon) = boxed_pair(seed)
                result = Executor(world).execute(move())
                self.assertEqual(result.status, "COMPLETE", result)
                self.assertEqual(result.states, ("FIND", "RESERVE", "DELIVER", "UNPARK", "STOP", "RELEASE"))
                self.assertEqual(world.unparked, (tractor.id,))
                self.assertEqual(world.items[tractor.id].attached_to, wagon.id)

    def test_a_closed_door_near_it_is_never_opened_nor_driven_round(self):
        # Live bfbc32565ecb4472-1 / the user, 2026-10-20: opening the Large Shed's wall shoved
        # four vehicles up to 11.7 m; building doors are not Farmhand's to open, and UNPARK
        # never makes up for one left closed.
        door = (131.0, 266.0)
        world, (tractor, wagon) = boxed_pair(1, gates=(door,))
        before = {i: world.items[i].position for i in (tractor.id, wagon.id)}
        result = Executor(world).execute(move())
        self.assertEqual((result.status, result.reason), ("FAILED", "UNPARK_DOOR_CLOSED"))
        self.assertEqual(world.opened, ())
        self.assertEqual({i: world.items[i].position for i in before}, before)
        self.assertNotIn("OPEN_DOORS", result.states)

    def test_a_hand_off_giants_still_cannot_start_fails_without_a_second_extraction(self):
        world, (tractor, wagon) = boxed_pair(3, {"exit": 9.0, "still": True})
        result = Executor(world).execute(move())
        self.assertEqual((result.status, result.reason), ("FAILED", "UNPARK_HANDOFF_BLOCKED"))
        self.assertEqual(world.unparked, (tractor.id,))
        self.assertEqual(result.states[-2:], ("STOP", "RELEASE"))
        self.assertTrue(all(world.items[i].reserved_by is None for i in (tractor.id, wagon.id)))

    def test_an_unpark_refused_before_moving_names_why_and_moves_nothing(self):
        for code in ("UNPARK_EXIT_BLOCKED", "UNPARK_TOO_TIGHT", "UNPARK_QUERY_UNVERIFIED"):
            with self.subTest(code):
                world, (tractor, wagon) = boxed_pair(4, {"refuse": code})
                before = {i: world.items[i].position for i in (tractor.id, wagon.id)}
                result = Executor(world).execute(move())
                self.assertEqual((result.status, result.reason), ("FAILED", code))
                self.assertEqual({i: world.items[i].position for i in before}, before)
                self.assertEqual(result.states[-2:], ("STOP", "RELEASE"))

    def test_a_vehicle_in_the_open_keeps_giants_reason(self):
        # GIANTS refused for another reason (UNPARK finds nothing boxing it in): the
        # original failure stands, never compensated by driving.
        class InTheOpen(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                raise StepFailed("NAVIGATE_BLOCKED_AT_START")

        tractor, wagon = hitched_pair("t1", "w1", (130.0, 260.0))
        world = InTheOpen([tractor, wagon], PLAYERS, ())
        result = Executor(world).execute(move())
        self.assertEqual((result.status, result.reason), ("FAILED", "NAVIGATE_BLOCKED_AT_START"))
        self.assertEqual(world.unparked, ("t1",))
        self.assertEqual(world.items["t1"].position, (130.0, 260.0))

    def test_an_invalid_destination_or_a_stall_on_the_way_never_unparks(self):
        for code in ("NAVIGATE_GOTO_INVALID", "NAVIGATE_STALLED", "TIMEOUT"):
            with self.subTest(code):
                class Refusing(MockWorld):
                    def navigate(self, tractor_id, pose, owner, code=code):
                        raise StepFailed(code)

                world = Refusing(hitched_pair("t1", "w1", (130.0, 260.0)), PLAYERS, ())
                result = Executor(world).execute(move())
                self.assertEqual((result.status, result.reason), ("FAILED", code))
                self.assertNotIn("UNPARK", result.states)
                self.assertEqual(world.unparked, ())

    def test_an_unpark_that_reports_done_without_moving_is_unverified(self):
        class Unmoved(MockWorld):
            def unpark(self, tractor_id, owner):
                self.unparked = self.unparked + (tractor_id,)

        tractor, wagon = hitched_pair("t1", "w1", (130.0, 260.0))
        world = Unmoved([tractor, wagon], PLAYERS, ())
        world.boxed = {"t1": dict(SHED)}
        result = Executor(world).execute(move())
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "UNPARK_NOT_OBSERVED"))

    def test_a_stop_before_the_extraction_cancels_it(self):
        world, (tractor, _) = boxed_pair(5)
        cancel = threading.Event()

        def entered(state):
            if state == "DELIVER":
                cancel.set()

        result = Executor(world).execute(move(), cancel=cancel, on_state=entered)
        self.assertEqual((result.status, result.reason), ("CANCELLED", "CANCELLED_BY_REQUESTER"))
        self.assertEqual(world.unparked, ())


class BudgetTests(unittest.TestCase):
    def test_one_extraction_per_vehicle_per_job_across_its_steps(self):
        world, (tractor, _) = boxed_pair(6)
        executor = Executor(world)
        first = executor.execute(move(request_id="job-7-1"), job="job-7")
        self.assertEqual(first.status, "COMPLETE", first)
        world.boxed = {tractor.id: dict(SHED)}                   # boxed in again, same job
        second = executor.execute(move(request_id="job-7-2"), job="job-7")
        self.assertEqual((second.status, second.reason), ("FAILED", "UNPARK_ALREADY_USED"))
        self.assertEqual(world.unparked, (tractor.id,))
        third = executor.execute(move(request_id="job-8"), job="job-8")   # a new job: a new budget
        self.assertEqual(third.status, "COMPLETE", third)
        self.assertEqual(world.unparked, (tractor.id, tractor.id))


class HitchFromAShedTests(unittest.TestCase):
    def hitch_world(self, box=SHED, extra=()):
        tractor, wagon = simple_world().snapshot()
        tractor = replace(tractor, capabilities=GOTO_TRACTOR)
        world = MockWorld([tractor, wagon, *extra], PLAYERS, ())
        world.boxed = {tractor.id: dict(box)}
        return world, tractor, wagon

    def test_a_lone_tractor_boxed_in_is_unparked_then_hitches(self):
        world, tractor, wagon = self.hitch_world()
        result = Executor(world).execute(dict(intent(), task="hitch_pair"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"), result)
        self.assertEqual(result.states[:4], ("FIND", "RESERVE", "STAGE", "UNPARK"))
        self.assertEqual(result.states.count("UNPARK"), 1)
        self.assertEqual(world.items[tractor.id].attached_to, wagon.id)

    def test_a_tractor_that_cannot_be_unparked_hands_over_to_the_next_one(self):
        tractor, wagon = simple_world().snapshot()
        tractor = replace(tractor, capabilities=GOTO_TRACTOR)
        second = replace(tractor, id=tractor.id + "-b", position=(5.0, -3.0), ai_root=Pose(5.0, -3.0, 0.0))
        world = MockWorld([tractor, second, wagon], PLAYERS, ())
        world.boxed = {tractor.id: {"refuse": "UNPARK_EXIT_BLOCKED"}}
        result = Executor(world).execute(dict(intent(), task="hitch_pair"))
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(result.equipment_ids, (second.id, wagon.id))
        self.assertEqual(world.items[tractor.id].position, tractor.position)    # never moved
        self.assertEqual(world.unparked, (tractor.id,))

    def test_inside_the_local_radius_after_unpark_the_bounded_hitch_steps_take_over(self):
        # Live 34c38284ac53469b-1: UNPARK took the 7R 8.5 m out of the Open Hall, ~8 m from
        # the PROLANDER's drawbar; the hand-off GOTO to an aim beside it was not_reachable.
        # The user, 2026-10-20: re-plan from the exit (inside 25 m no GOTO at all).
        tractor, wagon = simple_world().snapshot()
        tractor = replace(tractor, capabilities=GOTO_TRACTOR, position=(30.0, -6.0), ai_root=Pose(30.0, -6.0, 0.0))
        world = MockWorld([tractor, wagon], PLAYERS, ())
        world.boxed = {tractor.id: {"exit": 15.0, "still": True}}     # any GOTO after it would fail
        result = Executor(world).execute(dict(intent(), task="hitch_pair"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"), result)
        after = result.states[result.states.index("UNPARK") + 1:]
        self.assertEqual(after[:3], ("LOCAL_MANEUVER", "ALIGN_REVERSE", "ATTACH"))
        self.assertEqual(world.unparked, (tractor.id,))

    def test_after_unpark_the_first_goto_giants_takes_must_move_it(self):
        world, tractor, _ = self.hitch_world({"exit": 6.0, "still": True})
        result = Executor(world).execute(dict(intent(), task="hitch_pair"))
        self.assertEqual((result.status, result.reason), ("FAILED", "UNPARK_HANDOFF_BLOCKED"))
        self.assertEqual(world.unparked, (tractor.id,))
        self.assertGreater(math.dist(world.items[tractor.id].position, tractor.position), 5.0)


if __name__ == "__main__":
    unittest.main()
