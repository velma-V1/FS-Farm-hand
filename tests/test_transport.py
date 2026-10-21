"""Truck and gooseneck trailer (the user's completion plan, stage 3), on the mock world
(simulator/world.py; mock-only fold state, nothing here is GIANTS behaviour): a regular
bed's tailgate is lowered (GIANTS Foldable, the `fold` op) and verified before the
gooseneck is reversed under, stays down while hitched (clearance is never assumed), and
is down again before unhitching.
"""
from dataclasses import replace
import math
import unittest
import unittest.mock

from companion.executor import Executor
from protocol.model import Equipment, InputJoint, OutputJoint, Pose
from simulator.world import MockWorld
from tests.test_farmhand import AUTODRIVE_TRACTOR, GOTO_TRACTOR, PLAYERS, intent

TRUCK_JOINTS = (OutputJoint(1, "trailerCar", 0.0, -3.0, True), OutputJoint(3, "semitrailerCar", 0.0, 0.0, True))


def rig(truck_fold=0.0, joint="semitrailerCar"):
    """A truck with a Foldable tailgate (fold time 0: up) and a deckover 40 m off whose
    gooseneck input matches; IDs opaque."""
    truck = Equipment("k-731", "tractor", "farm-a", (0.0, 0.0), capabilities=GOTO_TRACTOR, name="GMC Sierra",
                      heading=0.0, ai_root=Pose(0.0, 0.0, 0.0), output_joints=TRUCK_JOINTS, fold=truck_fold,
                      folding=False if truck_fold is not None else None)
    deck = Equipment("d-118", "implement", "farm-a", (40.0, 20.0), required_pto_torque=0.0, heading=0.0,
                     name="1124 Deckover Trailer", kinds=frozenset({"trailer"}),
                     input_joints=(InputJoint(2, joint, (40.0, 26.0), True, frozenset()),),
                     fold=0.0, folding=False)
    return truck, deck


def hitch(name="deckover"):
    return dict(intent(), task="hitch_pair", implement="trailer", implement_name=name, tractor_name="gmc")


class TailgateTests(unittest.TestCase):
    def test_the_tailgate_is_lowered_and_verified_before_the_gooseneck_comes_under(self):
        calls = []

        class Recording(MockWorld):
            def fold(self, vehicle_id, direction, owner):
                calls.append(("fold", vehicle_id, direction, self.items[vehicle_id].position))
                return super().fold(vehicle_id, direction, owner)

            def align(self, *args, **kwargs):
                calls.append(("align", self.items[args[0]].fold))
                return super().align(*args, **kwargs)

        truck, deck = rig()
        world = Recording([truck, deck], PLAYERS, ())
        result = Executor(world).execute(hitch())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"), result)
        self.assertEqual(calls[0][:3], ("fold", truck.id, 1))
        self.assertEqual(calls[0][3], truck.position)                 # before anything moved
        self.assertEqual(calls[-1], ("align", 1.0))                   # down when it reverses under
        self.assertEqual(result.states[2], "TAILGATE")
        self.assertEqual(world.items[truck.id].fold, 1.0)             # stays down while hitched
        self.assertEqual(world.items[deck.id].fold, 0.0)              # the trailer's ramps untouched

    def test_a_tailgate_giants_ai_raised_on_the_way_is_lowered_again_before_reversing_under(self):
        # GIANTS' Foldable folds a part for transport whenever an AI job starts
        # (onAIImplementPrepareForTransport, allowUnfoldingByAI defaults true): live
        # d6701ca959d044c0-1 reversed under with the tailgate up after the GOTO stages.
        calls = []

        class GotoRaises(MockWorld):
            def _raised(self, vehicle_id):
                self.items[vehicle_id] = replace(self.items[vehicle_id], fold=0.0)

            def navigate(self, tractor_id, pose, owner):
                outcome = super().navigate(tractor_id, pose, owner)
                self._raised(tractor_id)
                return outcome

            def stage(self, tractor_id, wagon_id, pose, owner):
                outcome = super().stage(tractor_id, wagon_id, pose, owner)
                self._raised(tractor_id)
                return outcome

            def align(self, *args, **kwargs):
                calls.append(self.items[args[0]].fold)
                return super().align(*args, **kwargs)

        truck, deck = rig()
        world = GotoRaises([truck, deck], PLAYERS, ())
        result = Executor(world).execute(hitch())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"), result)
        self.assertEqual(calls, [1.0])
        self.assertEqual(result.states[result.states.index("ALIGN_REVERSE") - 1], "TAILGATE")

    def test_a_trailer_still_settling_after_the_attach_is_waited_for_not_left_unconfirmed(self):
        # Live d6701ca959d044c0-1: GIANTS' attach pulled the Deckover's nose 0.44 m onto the
        # ball; the cleanup's at-rest check came 0.5 s later. Mock-only motion.
        class Settling(MockWorld):
            moving = 0

            def attach(self, *args, **kwargs):
                outcome = super().attach(*args, **kwargs)
                self.moving = 3                                       # three snapshots in motion
                return outcome

            def snapshot(self):
                items = super().snapshot()
                if self.moving:
                    self.moving -= 1
                    items = [replace(i, stopped=False) if i.id == deck.id else i for i in items]
                return items

        truck, deck = rig()
        result = Executor(Settling([truck, deck], PLAYERS, ())).execute(hitch())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"), result)

    def test_no_tailgate_step_without_a_gooseneck_or_a_foldable_bed(self):
        for name, (truck, deck) in {"bumper pull": rig(joint="trailerCar"), "flatbed": rig(truck_fold=None)}.items():
            with self.subTest(name):
                world = MockWorld([truck, deck], PLAYERS, ())
                result = Executor(world).execute(hitch())
                self.assertEqual(result.status, "COMPLETE", result)
                self.assertNotIn("TAILGATE", result.states)

    def test_a_tailgate_that_will_not_go_down_moves_nothing(self):
        class Stuck(MockWorld):
            def fold(self, vehicle_id, direction, owner):
                return "FOLDED"                                       # says so, never observed

        truck, deck = rig()
        world = Stuck([truck, deck], PLAYERS, ())
        result = Executor(world).execute(hitch())
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "TAILGATE_NOT_OBSERVED"))
        self.assertEqual(world.items[truck.id].position, truck.position)

    def test_the_tailgate_is_down_before_a_gooseneck_is_unhitched(self):
        truck, deck = rig()
        joints = (OutputJoint(3, "semitrailerCar", 0.0, 0.0, False),)
        truck = replace(truck, attached_to=deck.id, output_joints=joints, fold=0.0, position=(100.0, 190.0),
                        ai_root=Pose(100.0, 190.0, 0.0))
        deck = replace(deck, attached_to=truck.id, position=(100.0, 182.0),
                       input_joints=(InputJoint(2, "semitrailerCar", (100.0, 190.0), True, frozenset()),))
        world = MockWorld([truck, deck], PLAYERS, ())
        result = Executor(world).execute(dict(intent(), task="detach_pair", implement="trailer"))
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertLess(result.states.index("TAILGATE"), result.states.index("DETACH"))
        self.assertEqual(world.items[truck.id].fold, 1.0)


def transport_world(tractor_ahead=-30.0, tractor_side=6.0, cls=MockWorld):
    """A GMC towing a deckover (ramps up: fold 0, belts open, four belts 2 m apart) facing
    yaw 0.4, and a Series 9 standing off to one side; IDs opaque and unordered."""
    yaw = 0.4
    fx, fz = math.sin(yaw), math.cos(yaw)

    def at(a, l=0.0):
        return (300.0 + fx * a + fz * l, -120.0 + fz * a - fx * l)

    deck = Equipment("q-77", "implement", "farm-a", at(0.0), name="1124 Deckover Trailer", heading=yaw,
                     kinds=frozenset({"trailer"}), attached_to="g-31", fold=0.0, folding=False,
                     belts_fastened=False, size=(10.0, 2.6, 1.2, 0.0),
                     belt_markers=tuple((*at(a, -1.3)[:1], 1.0, at(a, -1.3)[1], *at(a, 1.3)[:1], 1.0, at(a, 1.3)[1])
                                        for a in (-4.0, -2.0, 0.0, 2.0)))
    truck = Equipment("g-31", "tractor", "farm-a", at(9.0), name="2024 GMC Sierra Denali 2500 - 3500HD",
                      heading=yaw, attached_to="q-77", capabilities=GOTO_TRACTOR, size=(6.5, 2.4, 2.0, 0.0))
    position = at(tractor_ahead, tractor_side)
    tractor = Equipment("t-05", "tractor", "farm-a", position, name="Series 9 2019", heading=-1.0,
                        ai_root=Pose(*position, -1.0), capabilities=GOTO_TRACTOR, size=(5.2, 2.7, 3.0, 0.3))
    world = cls([deck, tractor, truck], PLAYERS, ())
    return world, tractor, deck, truck


def load(**extra):
    return dict(intent(), task="load_tractor", **extra)


class TractorTransportTests(unittest.TestCase):
    """The user's tractor transport (2026-10-21; AGENTS.md exception 5) on the mock world:
    ramps down and verified, GIANTS GOTO right behind them, LOAD, belts verified holding the
    tractor, ramps up and verified; UNLOAD the reverse. Mock-only fold, belt and LOAD state."""

    def test_load_lowers_the_ramps_lines_up_drives_up_belts_and_raises_the_ramps(self):
        from companion import hitch as transport
        world, tractor, deck, truck = transport_world()
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
        self.assertEqual(result.states, ("FIND", "RESERVE", "RAMPS_DOWN", "APPROACH", "LOAD", "BELTS", "RAMPS_UP",
                                         "STOP", "RELEASE"))
        self.assertEqual(set(result.equipment_ids), {tractor.id, deck.id})
        after = world.items
        self.assertTrue(transport.on_deck(after[tractor.id], after[deck.id]))
        self.assertEqual(after[tractor.id].belted_to, deck.id)
        self.assertEqual((after[deck.id].fold, after[deck.id].belts_fastened), (0.0, True))
        self.assertEqual(after[truck.id].position, truck.position)                 # the truck never moved
        self.assertTrue(all(i.reserved_by is None for i in after.values()))

    def test_approach_aims_put_the_front_right_behind_the_ramps_on_the_axis(self):
        from companion import hitch as transport
        world, tractor, deck, _ = transport_world()
        aims = transport.load_aims(tractor, deck)
        self.assertEqual(len(aims), 3)
        for aim, gap in zip(aims, transport.FRONT_GAPS_M):
            a, l = transport.to_frame(deck, (aim.x, aim.z))
            self.assertAlmostEqual(l, 0.0, places=9)
            self.assertAlmostEqual(aim.yaw, deck.heading)
            self.assertAlmostEqual(a + 0.3 + 2.6, -5.0 - gap)       # its front (offset + half length)

    def test_unload_releases_the_belts_lowers_the_ramps_backs_off_and_raises_them(self):
        from companion import hitch as transport
        world, tractor, deck, _ = transport_world()
        Executor(world).execute(load())
        result = Executor(world).execute(dict(intent(), task="unload_tractor"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "UNLOADED"), result)
        self.assertEqual(result.states, ("FIND", "RESERVE", "UNBELT", "RAMPS_DOWN", "UNLOAD", "RAMPS_UP", "STOP",
                                         "RELEASE"))
        after = world.items
        self.assertTrue(transport.off_ramps(after[tractor.id], after[deck.id]))
        self.assertIsNone(after[tractor.id].belted_to)
        self.assertEqual((after[deck.id].fold, after[deck.id].belts_fastened), (0.0, False))

    def test_ramps_that_do_not_move_move_nothing(self):
        class Stuck(MockWorld):
            def fold(self, vehicle_id, direction, owner):
                return "FOLDED"                                   # said so, never observed

        world, tractor, deck, _ = transport_world(cls=Stuck)
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "RAMPS_NOT_OBSERVED"))
        self.assertEqual(world.items[tractor.id].position, tractor.position)
        self.assertNotIn("LOAD", result.states)

    def test_belts_that_do_not_hold_the_tractor_are_never_secured(self):
        class Loose(MockWorld):
            def belts(self, vehicle_id, fasten, owner):
                outcome = super().belts(vehicle_id, fasten, owner)
                tractor = next(i for i in self.items.values() if i.belted_to == vehicle_id)
                self.items[tractor.id] = replace(tractor, belted_to=None)
                return outcome

        world, *_ = transport_world(cls=Loose)
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "BELTS_NOT_HOLDING"))
        self.assertNotIn("RAMPS_UP", result.states)

    def test_a_load_that_never_reached_the_deck_is_not_belted(self):
        world, *_ = transport_world()
        world.mock_load_moves = False
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "NOT_ON_DECK"))
        self.assertNotIn("BELTS", result.states)

    def test_a_loaded_rig_is_never_sent_by_giants_goto(self):
        # Live d14230aa9fb24d30-3: GIANTS' agent stayed BLOCKED from the first frame with the
        # Series 9 belted on the deckover. Without an AutoDrive marker: refused, nothing moved.
        world, tractor, deck, truck = transport_world()
        Executor(world).execute(load())
        world.players = {"farm-a": (500.0, 500.0)}
        before = world.snapshot()
        result = Executor(world).execute(dict(intent(request_id="move-1"), task="move_pair"))
        self.assertEqual((result.status, result.reason), ("BLOCKED", "LOADED_NEEDS_AUTODRIVE"), result)
        self.assertNotIn("RESERVE", result.states)
        self.assertEqual(world.snapshot(), before)

    def test_what_is_chosen(self):
        # Named: the deutz; a tractor towing something, or one the player rides away, is never loaded.
        world, tractor, deck, truck = transport_world()
        self.assertEqual(Executor(world).execute(load(tractor_name="fendt")).reason, "NO_TRACTOR_TO_LOAD")
        world, tractor, deck, truck = transport_world()
        self.assertEqual(Executor(world).execute(load(implement_name="lowboy")).reason, "NO_TRANSPORT")
        world, tractor, deck, truck = transport_world()
        world.items[deck.id] = replace(deck, belt_markers=())
        self.assertEqual(Executor(world).execute(load()).reason, "NO_TRANSPORT")
        world, tractor, deck, truck = transport_world()
        world.items[tractor.id] = replace(tractor, attached_to="x-1")
        self.assertEqual(Executor(world).execute(load()).reason, "NO_TRACTOR_TO_LOAD")
        world, *_ = transport_world()
        self.assertEqual(Executor(world).execute(dict(intent(), task="unload_tractor")).reason, "NO_LOADED_TRACTOR")


FINAL = "Take the GMC, hitch the Deckover, load the tractor, secure it, raise the ramps, deliver it to field 3, and unload it."


class SequenceTests(unittest.TestCase):
    """The user's final acceptance request as one job (the completion plan, 2026-10-20)."""

    def test_the_final_request_is_four_steps_in_order(self):
        from companion.language import RuleInterpreter
        proposal = RuleInterpreter().interpret(FINAL)
        self.assertEqual(proposal["command"], "sequence")
        self.assertEqual(proposal["steps"], [
            {"version": 1, "command": "hitch_pair", "implement": "trailer", "implement_name": "deckover",
             "tractor_name": "gmc"},
            {"version": 1, "command": "load_tractor", "implement_name": "deckover"},
            {"version": 1, "command": "move_pair", "target": {"type": "field_entrance", "field": 3},
             "tractor_name": "gmc"},
            {"version": 1, "command": "unload_tractor", "implement_name": "deckover"}])

    def test_transport_words(self):
        from companion.language import RuleInterpreter
        rules = RuleInterpreter()
        cases = {
            "load the tractor": {"command": "load_tractor"},
            "load the tractor onto the trailer and secure it": {"command": "load_tractor"},
            "load the Deutz-Fahr series 9 on the deckover, strap it down and raise the ramps":
                {"command": "load_tractor", "tractor_name": "deutz series 9", "implement_name": "deckover"},
            "put the deutz on the gooseneck": {"command": "load_tractor", "tractor_name": "deutz",
                                               "implement_name": "gooseneck"},
            "unload it": {"command": "unload_tractor"},
            "unload the tractor off the deckover": {"command": "unload_tractor", "implement_name": "deckover"},
            "take the tractor off the trailer": {"command": "unload_tractor"},
            "move the gmc here": {"command": "move_pair", "tractor_name": "gmc"},
        }
        for words, expected in cases.items():
            with self.subTest(words=words):
                got = rules.interpret(words)
                self.assertIsNotNone(got)
                self.assertEqual({k: got.get(k) for k in expected}, expected)
        # Not transports: an implement off the tractor, a wagon, a field drive, raising with nothing loaded.
        self.assertEqual(rules.interpret("take the seeder off the tractor")["command"], "detach_pair")
        self.assertIsNone(rules.interpret("load the forage wagon"))
        self.assertNotEqual((rules.interpret("drive the tractor onto the field") or {}).get("command"), "load_tractor")
        self.assertIsNone(rules.interpret("secure it, raise the ramps"))
        # A sequence of what one command does stays that command.
        self.assertEqual(rules.interpret("hitch the tractor to the forage wagon then take it to the barn")["command"],
                         "fetch_pair")

    def test_the_coordinator_runs_the_steps_in_order_and_stops_at_the_first_not_complete(self):
        from companion import coordinator as coordination
        from companion.language import RuleInterpreter
        from protocol.model import Result
        ran = []

        class World(MockWorld):
            cancel = None

            def retained(self):
                return ()

        class Scripted:
            def __init__(self, fail_at=None):
                self.fail_at = fail_at

            def execute(self, intent, cancel=None, on_state=None, job=None):
                ran.append((intent["task"], intent.get("tractor_name"), intent.get("implement_name"),
                            intent["target"], job))
                if intent["task"] == self.fail_at:
                    return Result("FAILED", "LOAD_PATH_BLOCKED", ("t-1",), ("FIND",))
                return Result("COMPLETE", "OK", ("t-1",), ("FIND",))

        for fail_at, tasks, last in ((None, ["hitch_pair", "load_tractor", "move_pair", "unload_tractor"],
                                      ("COMPLETE", "SEQUENCE_DONE", "COMPLETE_SEQUENCE")),
                                     ("load_tractor", ["hitch_pair", "load_tractor"],
                                      ("FAILED", "LOAD_PATH_BLOCKED", "FAILED_LOAD_PATH_BLOCKED"))):
            with self.subTest(fail_at=fail_at):
                ran.clear()
                notices = []
                c = coordination.Coordinator(World([], PLAYERS, ()), Scripted(fail_at),
                                             lambda n, s=False: notices.append(n), lambda: "farm-a", lambda: "s1")
                c.submit(RuleInterpreter().interpret(FINAL))
                c.wait(10)
                self.assertEqual([r[0] for r in ran], tasks)
                self.assertEqual(ran[0][1:3], ("gmc", "deckover"))
                self.assertEqual(ran[2][3] if len(ran) > 2 else None,
                                 {"type": "field_entrance", "field": 3} if fail_at is None else None)
                self.assertEqual(len({r[4] for r in ran}), 1)                  # one job for every step
                self.assertEqual((c.last.state, c.last.reason, notices[-1]), last)
                self.assertEqual(notices[0], "ACCEPTED_SEQUENCE")

    def test_stop_preempts_the_running_steps_own_reservation(self):
        # A plan's step reserves under its own request id; STOP must stop that one
        # (before 2026-10-21 it looked for the job's id and pre-empted nothing).
        import threading
        from companion import coordinator as coordination
        from protocol.model import Result
        started, stopped = threading.Event(), []

        class World(MockWorld):
            cancel = None
            current = None

            def retained(self):
                return ()

            def reservation(self, owner):
                return ("t-1", "w-1") if owner == self.current else None

            def stop(self, tractor_id, owner):
                stopped.append((tractor_id, owner))
                return True

        world = World([], PLAYERS, ())

        class Scripted:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                world.current = intent["request_id"]
                started.set()
                cancel.wait(5)
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", ("t-1",), ("FIND", "RESERVE"))

        c = coordination.Coordinator(world, Scripted(), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        c.submit({"version": 1, "command": "sequence", "steps": [
            {"version": 1, "command": "load_tractor"}, {"version": 1, "command": "unload_tractor"}]})
        self.assertTrue(started.wait(5))
        c.stop()
        c.wait(10)
        self.assertEqual(stopped, [("t-1", world.current)])
        self.assertTrue(world.current.endswith("-s1"))
        self.assertEqual(c.last.state, "CANCELLED")


def haul_world(hitched=False, loaded=False, ramps_down=False, marker=True):
    """The user's transport rig on the mock (mock-only belts, ramps and LOAD): a GMC with a
    gooseneck joint, a deckover with Foldable ramps and four belts 40 m off (heading 0), a
    Series 9 (DEUTZFAHR) standing apart; an AutoDrive marker "Field 3" far away."""
    from protocol.model import Destination
    truck, deck = rig()
    truck = replace(truck, capabilities=AUTODRIVE_TRACTOR, size=(6.5, 2.4, 2.0, 0.0), fold=1.0)
    belts = tuple((40.0 - 1.3, 1.0, 20.0 + a, 40.0 + 1.3, 1.0, 20.0 + a) for a in (-4.0, -2.0, 0.0, 2.0))
    deck = replace(deck, size=(10.0, 2.6, 1.2, 0.0), belts_fastened=False, belt_markers=belts, kinds=frozenset({"trailer"}))
    series9 = Equipment("s-9", "tractor", "farm-a", (10.0, -10.0), name="Series 9 2019", brand="DEUTZFAHR",
                        heading=1.2, ai_root=Pose(10.0, -10.0, 1.2), capabilities=GOTO_TRACTOR,
                        size=(5.2, 2.7, 3.0, 0.3))
    markers = (Destination("m-3", (900.0, 900.0), "Field 3"),) if marker else ()
    world = MockWorld([deck, series9, truck], PLAYERS, markers)
    world.mock_field_places = (Destination("field-3", (700.0, 650.0), "field 3"),)
    if hitched or loaded:
        executor = Executor(world)
        assert executor.execute(dict(intent("prep-1"), task="hitch_pair", implement="trailer",
                                     implement_name="deckover")).status == "COMPLETE"
    if loaded:
        assert Executor(world).execute(dict(intent("prep-2"), task="load_tractor")).status == "COMPLETE"
        if ramps_down:
            world.items[deck.id] = replace(world.items[deck.id], fold=1.0)
    return world, truck, deck, series9


class HaulGoalTests(unittest.TestCase):
    """"Take the Deutz to field 3 and unload it" (BUILD_SPEC completion plan, C1/C2/C4), the
    coordinator planning each step from observed state, the real executor on the mock."""

    def run_goal(self, world, words="Take the Deutz to field 3 and unload it"):
        from companion import coordinator as coordination
        from companion.language import RuleInterpreter

        class World(type(world)):
            pass
        world.__class__ = World
        world.cancel = None
        world.retained = lambda: ()
        notices = []
        c = coordination.Coordinator(world, Executor(world), lambda n, s=False: notices.append(n), lambda: "farm-a",
                                     lambda: "s1")
        with unittest.mock.patch.object(coordination.Coordinator, "SETTLE_S", 0.0):
            c.submit(RuleInterpreter().interpret(words))
            c.wait(30)
        return c.last, notices

    def test_from_an_unprepared_state_every_step_runs_once_in_order(self):
        from companion import hitch as transport
        world, truck, deck, series9 = haul_world()
        job, notices = self.run_goal(world)
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
        tasks = [s.split(":")[0] for s in job.states]
        self.assertEqual([t for i, t in enumerate(tasks) if i == 0 or tasks[i - 1] != t],
                         ["hitch_pair", "load_tractor", "move_pair", "unload_tractor"])
        after = world.items
        self.assertTrue(transport.off_ramps(after[series9.id], after[deck.id]))
        self.assertLess(math.dist(after[deck.id].position, (900.0, 900.0)), 1.0)       # unloaded there
        self.assertEqual((after[deck.id].fold, after[deck.id].belts_fastened), (0.0, False))
        self.assertEqual((notices[0], notices[-1]), ("ACCEPTED_HAUL", "COMPLETE_HAUL"))

    def test_from_partly_done_states_nothing_completed_is_repeated(self):
        for label, prep, first in (("hitched", dict(hitched=True), "load_tractor"),
                                   ("loaded and belted", dict(loaded=True), "move_pair"),
                                   ("loaded, ramps left down", dict(loaded=True, ramps_down=True), "load_tractor")):
            with self.subTest(label):
                world, truck, deck, series9 = haul_world(**prep)
                job, _ = self.run_goal(world)
                self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
                self.assertEqual(job.states[0].split(":")[0], first)
                self.assertNotIn("hitch_pair:ATTACH", job.states)
                if prep.get("loaded"):
                    self.assertNotIn("load_tractor:LOAD", job.states)          # never driven up twice
                    self.assertNotIn("load_tractor:BELTS", job.states)

    def test_no_unload_without_the_observed_arrival(self):
        # No AutoDrive marker: the loaded rig is not sent by GOTO; the goal stops there and
        # the tractor stays belted on the trailer.
        world, truck, deck, series9 = haul_world(loaded=True, marker=False)
        job, notices = self.run_goal(world)
        self.assertEqual((job.state, job.reason), ("BLOCKED", "LOADED_NEEDS_AUTODRIVE"), job.states)
        self.assertNotIn("unload_tractor", " ".join(job.states))
        self.assertEqual(world.items[series9.id].belted_to, deck.id)

    def test_the_next_step_from_each_observed_state(self):
        from companion.coordinator import haul_step
        world, truck, deck, series9 = haul_world()
        items = tuple(world.snapshot())
        target = {"type": "field_entrance", "field": 3}
        self.assertEqual(haul_step(items, "farm-a", "deutz", target, False)[0]["task"], "hitch_pair")
        self.assertEqual(haul_step(items, "farm-a", "fendt", target, False), (None, "NO_SUCH_TRACTOR"))
        world, *_ = haul_world(loaded=True)
        items = tuple(world.snapshot())
        step = haul_step(items, "farm-a", "deutz", target, False)[0]
        self.assertEqual((step["task"], step["cargo_name"], step["target"]), ("move_pair", "deutz", target))
        self.assertEqual(haul_step(items, "farm-a", "deutz", target, True)[0]["task"], "unload_tractor")
        self.assertEqual(haul_step(items, "farm-a", None, target, False)[0]["task"], "move_pair")   # "the tractor"


if __name__ == "__main__":
    unittest.main()
