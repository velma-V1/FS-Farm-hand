"""Truck and gooseneck trailer (the user's completion plan, stage 3), on the mock world
(simulator/world.py; mock-only fold state, nothing here is GIANTS behaviour): a regular
bed's tailgate is lowered (GIANTS Foldable, the `fold` op) and verified before the
gooseneck is reversed under, stays down while hitched (clearance is never assumed), and
is down again before unhitching.
"""
from dataclasses import replace
import json
import math
from pathlib import Path
import unittest
import unittest.mock

from adapters.world import StepFailed
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

    def test_the_tractor_is_loaded_with_its_mounted_plow_on(self):
        # The user, 2026-10-21: "load the tractor and plow on the gooseneck" (AGENTS.md exception 5,
        # a mounted implement along; the mod checks it is lifted, folded and fits).
        world, tractor, deck, truck = transport_world()
        plow = Equipment("p-41", "implement", "farm-a", (tractor.position[0] + 1.0, tractor.position[1] - 3.0),
                         name="Titan Vineyard Plow", kinds=frozenset({"plow"}), attached_to=tractor.id)
        world.items[tractor.id] = replace(world.items[tractor.id], attached_to=plow.id)
        world.items[plow.id] = plow
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
        after = world.items
        self.assertEqual(after[tractor.id].attached_to, plow.id)                     # still on it
        moved = (after[tractor.id].position[0] - tractor.position[0], after[tractor.id].position[1] - tractor.position[1])
        self.assertAlmostEqual(after[plow.id].position[0], plow.position[0] + moved[0])
        self.assertAlmostEqual(after[plow.id].position[1], plow.position[1] + moved[1])

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

    def test_the_approach_runs_up_straight_behind_the_ramps_first(self):
        # Live f41b44f422974e15-5: GIANTS turned in from the side (a plow on) and ended 12.4 deg
        # off, then would not start that close to the ramps; a run-up makes the last leg straight.
        from companion import hitch as transport

        class Recording(MockWorld):
            refuse_run_up = False

            def navigate(self, tractor_id, pose, owner):
                self.aims = getattr(self, "aims", []) + [pose]
                if self.refuse_run_up and len(self.aims) == 1:
                    raise StepFailed("NAVIGATE_GOTO_INVALID")
                super().navigate(tractor_id, pose, owner)

        for refused in (False, True):
            world, tractor, deck, _ = transport_world(cls=Recording)
            world.refuse_run_up = refused
            result = Executor(world).execute(load())
            self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
            fronts = [transport.to_frame(deck, (a.x, a.z))[0] + 0.3 + 2.6 for a in world.aims]
            self.assertAlmostEqual(fronts[0], -5.0 - transport.RUN_UP_GAP_M)
            self.assertAlmostEqual(fronts[1], -5.0 - transport.FRONT_GAPS_M[0])
            self.assertEqual(len(world.aims), 2)

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
        # Series 9 belted on the deckover (and AutoDrive waited on it as traffic, live
        # 831d6868b9df40b0-1): refused before reserving, nothing moved, the player asked.
        world, tractor, deck, truck = transport_world()
        Executor(world).execute(load())
        world.players = {"farm-a": (500.0, 500.0)}
        before = world.snapshot()
        result = Executor(world).execute(dict(intent(request_id="move-1"), task="move_pair"))
        self.assertEqual((result.status, result.reason), ("BLOCKED", "LOADED_NO_DRIVER"), result)
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


def drive_by_hand(world, truck_id, deck_id, position):
    """The player drives the loaded rig there (mock-only; the belted cargo rides along): the
    truck stands at ``position``, the trailer where it was relative to it."""
    truck, deck = world.items[truck_id], world.items[deck_id]
    offset = (deck.position[0] - truck.position[0], deck.position[1] - truck.position[1])
    world.items[truck_id] = replace(truck, position=tuple(position), ai_root=Pose(*position, truck.heading or 0.0),
                                    stopped=True)
    world._move_wagon(deck, (position[0] + offset[0], position[1] + offset[1]))


class HaulGoalTests(unittest.TestCase):
    """"Take the Deutz to field 3 and unload it" (BUILD_SPEC completion plan, C1/C2/C4), the
    coordinator planning each step from observed state, the real executor on the mock. A
    trailer carrying a belted vehicle is moved by no installed driver (GIANTS GOTO blocked live
    d14230aa9fb24d30-3, AutoDrive waiting on it as traffic live 831d6868b9df40b0-1): the goal
    pauses LOADED_NO_DRIVER, the player drives it there, "continue" unloads on arrival."""

    def coordinator(self, world):
        from companion import coordinator as coordination

        class World(type(world)):
            pass
        world.__class__ = World
        world.cancel = None
        world.retained = lambda: ()
        notices = []
        c = coordination.Coordinator(world, Executor(world), lambda n, s=False: notices.append(n), lambda: "farm-a",
                                     lambda: "s1")
        patch = unittest.mock.patch.object(coordination.Coordinator, "SETTLE_S", 0.0)
        patch.start()
        self.addCleanup(patch.stop)
        return c, notices

    def run_goal(self, world, words="Take the Deutz to field 3 and unload it"):
        from companion.language import RuleInterpreter
        c, notices = self.coordinator(world)
        c.submit(RuleInterpreter().interpret(words))
        c.wait(30)
        return c, notices

    def finish(self, c, world, truck, deck, where):
        drive_by_hand(world, truck.id, deck.id, where)
        c.resume()
        for _ in range(300):
            c.wait(0.1)
            if not c.busy:
                break
        return c.last

    def test_from_an_unprepared_state_every_step_runs_once_in_order(self):
        from companion import hitch as transport
        world, truck, deck, series9 = haul_world()
        c, notices = self.run_goal(world)
        first = c.last
        self.assertEqual((first.state, first.reason), ("BLOCKED", "LOADED_NO_DRIVER"), first.states)
        tasks = [s.split(":")[0] for s in first.states]
        self.assertEqual([t for i, t in enumerate(tasks) if i == 0 or tasks[i - 1] != t],
                         ["hitch_pair", "load_tractor", "move_pair"])
        self.assertNotIn("move_pair:RESERVE", first.states)                       # refused before anything
        self.assertEqual(world.items[series9.id].belted_to, deck.id)              # waiting, loaded
        self.assertEqual(notices[-1], "BLOCKED_LOADED_NO_DRIVER")
        job = self.finish(c, world, truck, deck, (893.0, 890.0))                  # 10 m from "Field 3"
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
        self.assertEqual([s.split(":")[0] for s in job.states if s.endswith(":FIND")], ["move_pair", "unload_tractor"])
        self.assertNotIn("move_pair:DELIVER", job.states)                         # nothing drove it
        after = world.items
        self.assertTrue(transport.off_ramps(after[series9.id], after[deck.id]))
        self.assertEqual((after[deck.id].fold, after[deck.id].belts_fastened), (0.0, False))
        self.assertIsNone(c.kept)

    def test_from_partly_done_states_nothing_completed_is_repeated(self):
        for label, prep, first in (("hitched", dict(hitched=True), "load_tractor"),
                                   ("loaded and belted", dict(loaded=True), "move_pair"),
                                   ("loaded, ramps left down", dict(loaded=True, ramps_down=True), "load_tractor")):
            with self.subTest(label):
                world, truck, deck, series9 = haul_world(**prep)
                c, _ = self.run_goal(world)
                self.assertEqual(c.last.reason, "LOADED_NO_DRIVER", c.last.states)
                self.assertEqual(c.last.states[0].split(":")[0], first)
                self.assertNotIn("hitch_pair:ATTACH", c.last.states)
                if prep.get("loaded"):
                    self.assertNotIn("load_tractor:LOAD", c.last.states)      # never driven up twice
                    self.assertNotIn("load_tractor:BELTS", c.last.states)
                job = self.finish(c, world, truck, deck, (900.0, 900.0))
                self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
                self.assertNotIn("load_tractor:LOAD", job.states)

    def test_no_unload_without_the_observed_arrival(self):
        # Driven only part of the way: "continue" does not unload short of the destination.
        world, truck, deck, series9 = haul_world(loaded=True)
        c, _ = self.run_goal(world)
        job = self.finish(c, world, truck, deck, (700.0, 700.0))                  # 280 m short
        self.assertEqual((job.state, job.reason), ("BLOCKED", "LOADED_NO_DRIVER"), job.states)
        self.assertNotIn("unload_tractor", " ".join(job.states))
        self.assertEqual(world.items[series9.id].belted_to, deck.id)
        self.assertIsNotNone(c.kept)

    def test_the_destination_is_autodrive_s_marker_made_once_where_its_network_meets_the_field(self):
        world, truck, deck, series9 = haul_world(loaded=True, marker=False)
        world.mock_ad_fields = {3: (-850.0, 420.0)}
        c, _ = self.run_goal(world)
        self.assertEqual(c.last.reason, "LOADED_NO_DRIVER")
        self.assertEqual(world.ad_markers_made, (3,))                             # where to drive it
        job = self.finish(c, world, truck, deck, (-845.0, 430.0))
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
        self.assertEqual(world.ad_markers_made, (3,))                             # not made twice

    def test_no_marker_is_made_for_what_goto_can_deliver(self):
        world, truck, deck, series9 = haul_world(hitched=True, marker=False)
        world.mock_ad_fields = {3: (-850.0, 420.0)}
        result = Executor(world).execute(dict(intent("m-1"), task="move_pair",
                                              target={"type": "field_entrance", "field": 3}))
        self.assertEqual(result.status, "COMPLETE", result)                # not loaded: GIANTS GOTO
        self.assertEqual(world.ad_markers_made, ())

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


class ResumeTests(unittest.TestCase):
    """Pause, takeover, a changed destination and resume keep the goal; nothing completed
    repeats (BUILD_SPEC completion plan requirement 4; ACCEPTANCE C5-C7)."""

    def coordinator(self, world, interrupt):
        """The real executor, except where ``interrupt(intent, cancel)`` returns a Result."""
        import threading
        from companion import coordinator as coordination
        executor = Executor(world)
        ran = []

        class Wrapped:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                ran.append(intent)
                early = interrupt(intent, cancel)
                if early is not None:
                    return early
                return executor.execute(intent, cancel=cancel, on_state=on_state, job=job)

        world.cancel = None
        world.retained = lambda: ()
        world.reservation = lambda owner: None
        notices = []
        c = coordination.Coordinator(world, Wrapped(), lambda n, s=False: notices.append(n), lambda: "farm-a",
                                     lambda: "s1")
        self.patch = unittest.mock.patch.object(coordination.Coordinator, "SETTLE_S", 0.0)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        return c, ran, notices, threading

    def haul(self, c, words="Take the Deutz to field 3 and unload it"):
        from companion.language import RuleInterpreter
        c.submit(RuleInterpreter().interpret(words))

    def wait_idle(self, c):
        import time
        for _ in range(300):
            c.wait(1)
            if not c.busy:
                time.sleep(0.05)
                if not c.busy:
                    return
        self.fail("still busy")

    def test_stop_pauses_the_goal_and_continue_finishes_it_without_repeating(self):
        from protocol.model import Result
        world, truck, deck, series9 = haul_world(loaded=True)
        first = {"move": True}

        def interrupt(intent, cancel):
            if intent["task"] == "move_pair" and first["move"]:
                first["move"] = False
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", (truck.id, deck.id), ("FIND", "RESERVE", "DELIVER"))
            return None

        c, ran, notices, _ = self.coordinator(world, interrupt)
        self.haul(c)
        self.wait_idle(c)
        self.assertEqual(c.last.state, "CANCELLED")
        self.assertEqual(notices[-1], "PAUSED")
        self.assertIsNotNone(c.kept)
        self.assertEqual(world.items[series9.id].belted_to, deck.id)        # never unloaded on a stop
        drive_by_hand(world, truck.id, deck.id, (900.0, 900.0))
        ran.clear()
        c.resume()
        self.wait_idle(c)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), c.last.states)
        self.assertEqual([i["task"] for i in ran], ["move_pair", "unload_tractor"])
        self.assertIsNone(c.kept)
        self.assertIn("RESUMING", notices)

    def test_a_takeover_pauses_and_continue_resumes_from_the_observed_state(self):
        from protocol.model import Result
        world, truck, deck, series9 = haul_world(hitched=True)
        taken = {"done": False}

        def interrupt(intent, cancel):
            if intent["task"] == "load_tractor" and not taken["done"]:
                taken["done"] = True
                return Result("FAILED", "DRIVER_TOOK_OVER", (series9.id, deck.id), ("FIND", "RESERVE", "RAMPS_DOWN"))
            return None

        c, ran, notices, _ = self.coordinator(world, interrupt)
        self.haul(c)
        self.wait_idle(c)
        self.assertEqual((c.last.state, c.last.reason), ("FAILED", "DRIVER_TOOK_OVER"))
        self.assertIsNotNone(c.kept)
        ran.clear()
        c.submit({"version": 1, "command": "resume"})
        self.wait_idle(c)
        self.assertEqual((c.last.state, c.last.reason), ("BLOCKED", "LOADED_NO_DRIVER"), c.last.states)
        self.assertEqual([i["task"] for i in ran], ["load_tractor", "move_pair"])
        drive_by_hand(world, truck.id, deck.id, (900.0, 900.0))
        c.submit({"version": 1, "command": "resume"})
        self.wait_idle(c)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), c.last.states)
        self.assertEqual([i["task"] for i in ran], ["load_tractor", "move_pair", "move_pair", "unload_tractor"])

    def test_a_new_destination_while_driving_stops_and_carries_on_there(self):
        from protocol.model import Destination, Result
        world, truck, deck, series9 = haul_world(loaded=True)
        world.destination_map = {**world.destination_map, "m-2": Destination("m-2", (-600.0, 300.0), "Field 2")}
        started = []

        def interrupt(intent, cancel):
            if intent["task"] == "move_pair" and not started:
                started.append(intent["target"])
                cancel.wait(5)                          # driving until the stop arrives
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", (truck.id, deck.id), ("FIND", "RESERVE", "DELIVER"))
            return None

        c, ran, notices, threading = self.coordinator(world, interrupt)
        self.haul(c)
        for _ in range(100):
            if started:
                break
            c.wait(0.05)
        c.submit({"version": 1, "command": "retarget", "target": {"type": "field_entrance", "field": 2}})
        self.wait_idle(c)
        self.assertEqual(c.last.reason, "LOADED_NO_DRIVER", c.last.states)            # now bound for field 2
        self.assertEqual(ran[1]["target"], {"type": "field_entrance", "field": 2})
        drive_by_hand(world, truck.id, deck.id, (900.0, 900.0))                       # to field 3: not it
        c.resume()
        self.wait_idle(c)
        self.assertEqual(c.last.reason, "LOADED_NO_DRIVER")
        drive_by_hand(world, truck.id, deck.id, (-600.0, 300.0))
        c.resume()
        self.wait_idle(c)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), c.last.states)
        self.assertIn("RETARGETED", notices)
        self.assertIn("RETARGETED", notices)

    def test_a_new_destination_for_a_kept_goal_and_nothing_to_continue(self):
        from protocol.model import Destination, Result
        world, truck, deck, series9 = haul_world(loaded=True)
        world.destination_map = {**world.destination_map, "m-2": Destination("m-2", (-600.0, 300.0), "Field 2")}
        once = {"stop": True}

        def interrupt(intent, cancel):
            if intent["task"] == "move_pair" and once["stop"]:
                once["stop"] = False
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", (truck.id, deck.id), ("FIND",))
            return None

        c, ran, notices, _ = self.coordinator(world, interrupt)
        c.submit({"version": 1, "command": "resume"})
        self.assertEqual(notices[-1], "NOTHING_TO_RESUME")
        self.haul(c)
        self.wait_idle(c)
        c.submit({"version": 1, "command": "retarget", "target": {"type": "field_entrance", "field": 2}})
        self.wait_idle(c)
        self.assertEqual(c.last.reason, "LOADED_NO_DRIVER", c.last.states)
        drive_by_hand(world, truck.id, deck.id, (-600.0, 300.0))
        c.resume()
        self.wait_idle(c)
        self.assertEqual(c.last.reason, "HAULED_AND_UNLOADED", c.last.states)


class RestartTests(unittest.TestCase):
    """A kept goal survives a reload or a companion restart and is re-planned from the
    observed world; a purchase is never repeated (ACCEPTANCE C8). Mock world only."""

    def test_a_paused_goal_is_saved_reloaded_by_a_new_companion_and_finished(self):
        import tempfile
        from companion import coordinator as coordination
        from protocol.model import Result
        world, truck, deck, series9 = haul_world(loaded=True)
        world.cancel, world.retained, world.reservation = None, (lambda: ()), (lambda owner: None)
        folder = Path(tempfile.mkdtemp())
        goal = folder / "goal.json"
        executor = Executor(world)

        class Interrupted:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                if intent["task"] == "move_pair":
                    return Result("UNVERIFIED", "LINK_LOST", (truck.id, deck.id), ("FIND", "RESERVE", "DELIVER"))
                return executor.execute(intent, cancel=cancel, on_state=on_state, job=job)

        with unittest.mock.patch.object(coordination.Coordinator, "SETTLE_S", 0.0):
            first = coordination.Coordinator(world, Interrupted(), lambda n, s=False: None, lambda: "farm-a",
                                             lambda: "s1", goal_file=goal)
            from companion.language import RuleInterpreter
            first.submit(RuleInterpreter().interpret("take the deutz to field 3 and unload it"))
            first.wait(30)
            self.assertTrue(goal.exists())
            drive_by_hand(world, truck.id, deck.id, (900.0, 900.0))              # the player drove it there
            # A new companion (restart): new session, same world.
            notices = []
            second = coordination.Coordinator(world, Executor(world), lambda n, s=False: notices.append(n),
                                              lambda: "farm-a", lambda: "s2", goal_file=goal)
            self.assertEqual(second.status(), "STATUS_PAUSED")
            second.submit({"version": 1, "command": "resume"})
            second.wait(30)
            for _ in range(100):
                if not second.busy:
                    break
                second.wait(0.1)
        self.assertEqual((second.last.state, second.last.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), second.last.states)
        self.assertFalse(goal.exists())                                     # done: nothing kept

    def test_a_resumed_goal_never_buys_twice(self):
        import tempfile
        from companion import coordinator as coordination
        from protocol.model import Result
        world, truck, deck, series9 = haul_world()
        world.cancel, world.retained, world.reservation = None, (lambda: ()), (lambda owner: None)
        bought = []

        class Scripted:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                if intent["task"] == "buy":
                    bought.append(intent["want"])
                    return Result("COMPLETE", "BOUGHT", ("x",), ("FIND", "BUY", "VERIFY"))
                return Result("COMPLETE", "OK", (), ("FIND",))

        goal = Path(tempfile.mkdtemp()) / "goal.json"
        # As left by a companion that crashed after the purchase, before marking it done:
        # the step's baseline recorded, the farm now owning one more Series 9.
        steps = [{"version": 1, "command": "buy", "wants": [{"kind": "tractor", "name": "deutz series 9"}]},
                 {"version": 1, "command": "load_tractor"}]
        goal.write_text(json.dumps({"command": "sequence", "target": "here", "implement": None,
                                    "work": {"steps": steps, "done": 0, "baseline": {"1": 0}}}))
        c = coordination.Coordinator(world, Scripted(), lambda n, s=False: None, lambda: "farm-a", lambda: "s3",
                                     goal_file=goal)
        c.submit({"version": 1, "command": "resume"})
        c.wait(10)
        self.assertEqual(bought, [])                                        # the Series 9 is already there
        self.assertEqual((c.last.state, c.last.states[0]), ("COMPLETE", "buy:ALREADY_DONE"))


class AutoDriveBackupTests(unittest.TestCase):
    def test_the_network_is_copied_and_verified_before_any_change_or_nothing_changes(self):
        import tempfile
        from adapters.world import StepFailed
        from companion.bridge import backup_autodrive_config
        root = Path(tempfile.mkdtemp())
        save, profile = root / "savegame2", root / "profile"
        save.mkdir()
        (save / "AutoDrive_config.xml").write_text("<AutoDrive><MapName>FourFields</MapName></AutoDrive>")
        copy = backup_autodrive_config(str(save), profile, root / "backup")
        self.assertEqual(copy.read_text(), (save / "AutoDrive_config.xml").read_text())
        (profile / "save").mkdir(parents=True)                               # the Xbox build's active save
        (profile / "save" / "AutoDrive_config.xml").write_text("<AutoDrive/>")
        self.assertEqual(backup_autodrive_config(None, profile, root / "b2").read_text(), "<AutoDrive/>")
        with self.assertRaises(StepFailed) as failed:
            backup_autodrive_config(str(root / "nowhere"), root / "empty", root / "b3")
        self.assertEqual(failed.exception.code, "AD_CONFIG_BACKUP_FAILED")


class ProgressTests(unittest.TestCase):
    """STATUS says a motion step is driving only when its equipment is observed moving (C10)."""

    def test_status_follows_observed_motion(self):
        from companion import coordinator as coordination
        world, truck, deck, series9 = haul_world(hitched=True)

        class Executor_:
            active = ()

        executor = Executor_()
        c = coordination.Coordinator(world, executor, lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        c.current = coordination.Job(1, "s1-1", step="DELIVER", command="haul_tractor")
        self.assertEqual(c.status(), "STATUS_WAITING_DELIVER")                 # nothing reserved yet
        executor.active = (truck.id, deck.id)
        self.assertEqual(c.status(), "STATUS_WAITING_DELIVER")                 # reserved, at rest
        world.items[truck.id] = replace(world.items[truck.id], stopped=False)
        self.assertEqual(c.status(), "STATUS_WORKING_DELIVER")                 # observed moving
        c.current = coordination.Job(1, "s1-1", step="BELTS", command="haul_tractor")
        self.assertEqual(c.status(), "STATUS_WORKING_BELTS")                   # not a motion step


class FollowMeTests(unittest.TestCase):
    """"Follow me" only on the player's words; it never unloads (requirement 12, C13)."""

    def test_follow_then_continue_unloads_only_where_it_was_led(self):
        from companion import coordinator as coordination
        from companion.language import RuleInterpreter
        world, truck, deck, series9 = haul_world(loaded=True)
        world.cancel, world.retained = None, (lambda: ())
        world.mock_follow_to = (895.0, 895.0)                                # led to "Field 3"
        c = coordination.Coordinator(world, Executor(world), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        rules = RuleInterpreter()

        def run(words):
            c.submit(rules.interpret(words))
            for _ in range(300):
                c.wait(0.1)
                if not c.busy:
                    break
            return c.last

        with unittest.mock.patch.object(coordination.Coordinator, "SETTLE_S", 0.0):
            self.assertEqual(run("take the deutz to field 3 and unload it").reason, "LOADED_NO_DRIVER")
            followed = run("have the gmc follow me")
            self.assertEqual((followed.state, followed.reason), ("COMPLETE", "FOLLOW_ENDED"), followed.states)
            self.assertEqual(world.items[series9.id].belted_to, deck.id)    # following never unloads
            self.assertIsNotNone(c.kept)                                     # the haul is still kept
            done = run("continue")
        self.assertEqual((done.state, done.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), done.states)

    def test_nothing_follows_without_followme_or_a_rig(self):
        world, truck, deck, series9 = haul_world()
        result = Executor(world).execute(dict(intent("f-1"), task="follow_me"))
        self.assertEqual(result.reason, "NO_RIG_TO_FOLLOW")                 # nothing hitched yet
        world, *_ = haul_world(hitched=True)
        result = Executor(world).execute(dict(intent("f-2"), task="follow_me"))
        self.assertEqual((result.status, result.reason), ("FAILED", "FOLLOWME_UNAVAILABLE"))


class ReferenceTests(unittest.TestCase):
    """"Take it back", "take the deckover back": where Farmhand took it from (requirement 6, C9)."""

    def test_take_it_back_returns_the_rig_to_where_farmhand_took_it(self):
        from companion import coordinator as coordination
        from companion.language import RuleInterpreter
        world, truck, deck, series9 = haul_world()
        world.cancel, world.retained = None, (lambda: ())
        c = coordination.Coordinator(world, Executor(world), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        rules = RuleInterpreter()

        def run(words):
            c.submit(rules.interpret(words))
            for _ in range(300):
                c.wait(0.1)
                if not c.busy:
                    break
            return c.last

        with unittest.mock.patch.object(coordination.Coordinator, "SETTLE_S", 0.0):
            self.assertEqual(run("take the deckover back").reason, "NO_ORIGIN_KNOWN")       # never taken
            self.assertEqual(run("hitch the deckover with the gmc").state, "COMPLETE")
            taken = world.items[truck.id].ai_root
            self.assertEqual(run("move this here").state, "COMPLETE")                     # the player at (100, 200)
            self.assertGreater(math.dist(world.items[truck.id].position, (taken.x, taken.z)), 50)
            back = run("take the deckover back")
        self.assertEqual(back.state, "COMPLETE", back.states)
        root = world.items[truck.id].ai_root
        self.assertLess(math.dist((root.x, root.z), (taken.x, taken.z)), 3.5)

    def test_the_words(self):
        from companion.language import RuleInterpreter
        rules = RuleInterpreter()
        self.assertEqual(rules.interpret("take it back"), {"version": 1, "command": "move_pair", "target": {"type": "origin"}})
        self.assertEqual(rules.interpret("put the deckover back where it was")["implement_name"], "deckover")
        self.assertEqual(rules.interpret("take the gmc back")["tractor_name"], "gmc")
        self.assertNotEqual(rules.interpret("take the deckover back")["command"], "hitch_pair")


class WholeRequestTests(unittest.TestCase):
    """The user's request of 2026-10-21 as one command (each piece a Farmhand function):
    "take the tractor to the farm then load it back up bring it back up buy a 25m lizard
    titan vineyard plow (with the 25/mph speed selection) and load the tractor and plow on
    the gooseneck and deliver it to the farm". Mock world; the player drives the loaded rig
    where no installed driver can (LOADED_NO_DRIVER), then says "continue"."""

    WORDS = ("take the tractor to the farm then load it back up bring it back up buy a 25m lizard titan vineyard "
             "plow with the 25/mph speed selection and load the tractor and plow on the gooseneck and deliver it to the farm")

    def test_the_whole_request_runs_in_order_and_nothing_done_is_redone(self):
        from companion import coordinator as coordination
        from companion import hitch as transport
        from companion.executor import PLACE_STANDOFF_M
        from companion.language import RuleInterpreter
        from protocol.model import Destination, FarmInfo, InputJoint, OutputJoint, StoreItem
        world, truck, deck, series9 = haul_world(hitched=True)
        three_point = (OutputJoint(1, "implement", 0.0, -2.0, True),)
        world.items[series9.id] = replace(world.items[series9.id], output_joints=three_point, pto_torque_capacity=2000.0)
        farm = Destination("place-farm", (-400.0, 700.0), "Farm Barn")
        world.place_list = (farm,)
        world.farm_info = FarmInfo(1e6, True, True, True)
        world.store_items = (StoreItem(31, "Titan Vineyard Plow", "LIZARD", ("PLOWS",), 3000, True, 1.7, 65, None,
                                       options=(("config", "design16", 4, "25MPH / 41KPH"),
                                                ("set", None, 5, "25M Unrealistic"))),
                             StoreItem(21, "Series 9 2019", "DEUTZFAHR", ("TRACTORSL",), 200000, True, None, None, 3100))
        world.mock_bought = Equipment("x", "implement", "farm-a", (60.0, 60.0), kinds=frozenset({"plow"}),
                                      required_pto_torque=0.0, heading=0.0,
                                      input_joints=(InputJoint(1, "implement", (60.0, 61.0), True, frozenset()),))
        world.cancel, world.retained = None, (lambda: ())
        bought = []
        original_buy = world.buy

        def buy(item_id, hitch=0, engine=0, options=()):
            bought.append((item_id, tuple(options)))
            return original_buy(item_id, hitch, engine, options)
        world.buy = buy
        c = coordination.Coordinator(world, Executor(world), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        rules = RuleInterpreter()

        def run(proposal):
            c.submit(proposal)
            for _ in range(600):
                c.wait(0.1)
                if not c.busy:
                    break
            return c.last

        # The farm's GOTO point is PLACE_STANDOFF_M short of its building, toward the rig.
        def farm_point():
            here = world.items[truck.id].position
            d = math.dist(here, farm.position)
            t = (d - PLACE_STANDOFF_M) / d
            return (here[0] + (farm.position[0] - here[0]) * t, here[1] + (farm.position[1] - here[1]) * t)

        start = world.items[truck.id].position
        with unittest.mock.patch.object(coordination.Coordinator, "SETTLE_S", 0.0):
            job = run(rules.interpret(self.WORDS))
            self.assertEqual(job.reason, "LOADED_NO_DRIVER", job.states)               # to the farm: the player drives
            drive_by_hand(world, truck.id, deck.id, farm_point())
            job = run({"version": 1, "command": "resume"})
            self.assertEqual(job.reason, "LOADED_NO_DRIVER", job.states)               # unloaded, loaded; back: drive
            self.assertIn("haul_tractor:unload_tractor:UNLOAD", job.states)            # unloaded at the farm
            self.assertIn("load_tractor:LOAD", job.states)                             # and loaded back up
            drive_by_hand(world, truck.id, deck.id, start)
            job = run({"version": 1, "command": "resume"})
            self.assertEqual(job.reason, "LOADED_NO_DRIVER", job.states)               # bought, loaded with the plow; to the farm
            self.assertEqual(job.states[:2], ("haul_tractor:ALREADY_DONE", "load_tractor:ALREADY_DONE"))
            plow = next(i for i in world.items.values() if i.name == "Titan Vineyard Plow")
            self.assertEqual(world.items[series9.id].attached_to, plow.id)
            self.assertEqual(world.items[series9.id].belted_to, deck.id)
            drive_by_hand(world, truck.id, deck.id, farm_point())
            job = run({"version": 1, "command": "resume"})
        self.assertEqual((job.state, job.reason), ("COMPLETE", "SEQUENCE_DONE"), job.states)
        self.assertEqual(bought, [(31, (2, 1))])                                        # once, with both options
        self.assertLess(math.dist(world.items[truck.id].position, farm_point()), 1.0)
        self.assertEqual(world.items[series9.id].belted_to, deck.id)                   # delivered loaded
        self.assertIsNone(c.kept)


if __name__ == "__main__":
    unittest.main()


class BuyHitchLoadHaulTests(unittest.TestCase):
    """The user (2026-10-21): "buy y and z and hitch them together and load them on the
    trailer use the gmc to take them to the farm/field/...": one sequence, "them" the
    things just bought, the load carrying the implement hitched in it, the truck named."""

    WORDS = ("buy a deutz series 9 and a plow and hitch them together and load them on the gooseneck "
             "and use the gmc to take them to the farm")

    def test_the_words_are_the_steps_in_order(self):
        from companion.language import RuleInterpreter
        self.assertEqual(RuleInterpreter().interpret(self.WORDS), {"version": 1, "command": "sequence", "steps": [
            {"version": 1, "command": "buy", "wants": [{"kind": "tractor", "name": "deutz series 9"}, {"kind": "plow"}]},
            {"version": 1, "command": "hitch_pair", "together": True},
            {"version": 1, "command": "load_tractor", "implement_name": "gooseneck"},
            {"version": 1, "command": "move_pair", "target": {"type": "place", "name": "farm"}, "tractor_name": "gmc"}]})

    def test_them_is_what_was_just_bought_and_then_what_was_hitched(self):
        from companion.coordinator import remember, with_context
        from companion.language import RuleInterpreter
        from protocol.model import Result
        steps = RuleInterpreter().interpret(self.WORDS)["steps"]
        context = {}
        remember(context, steps[0], Result("COMPLETE", "BOUGHT", (), ()), ())
        hitch = with_context(steps[1], context)
        self.assertEqual({k: hitch.get(k) for k in ("together", "implement", "tractor_name")},
                         {"together": None, "implement": "plow", "tractor_name": "deutz series 9"})
        tractor = Equipment("t-9", "tractor", "farm-a", (0.0, 0.0), name="Series 9 2019", brand="DEUTZFAHR")
        remember(context, hitch, Result("COMPLETE", "ATTACH_OBSERVED", ("t-9", "p-1"), ()), (tractor,))
        load = with_context(steps[2], context)
        self.assertEqual((load["carry"], load["tractor_name"], load["implement_name"]), ("plow", "series 9 2019", "gooseneck"))
        context["loaded"] = True
        self.assertEqual(with_context(steps[3], context)["tractor_name"], "gmc")      # the truck, not the cargo

    def test_buy_a_spreader_hitch_it_to_a_tractor_and_do_a_contract(self):
        # The user (2026-10-21, save 1): "buy anything needed" for the fertilize contract.
        from companion.coordinator import remember, with_context
        from companion.executor import STORE_KINDS, _matches
        from companion.language import RuleInterpreter
        from protocol.model import Result, StoreItem
        steps = RuleInterpreter().interpret("buy a fertilizer spreader, hitch it to the 3650 and do a contract")["steps"]
        self.assertEqual(steps, [{"version": 1, "command": "buy", "wants": [{"kind": "spreader"}]},
                                 {"version": 1, "command": "hitch_pair", "together": True, "tractor_name": "3650"},
                                 {"version": 1, "command": "contract"}])
        context = {}
        remember(context, steps[0], Result("COMPLETE", "BOUGHT", (), ()), ())
        hitch = with_context(steps[1], context)
        self.assertEqual({k: hitch.get(k) for k in ("together", "implement", "tractor_name")},
                         {"together": None, "implement": "sprayer", "tractor_name": "3650"})
        self.assertIn("FERTILIZERSPREADER", STORE_KINDS["spreader"])
        item = lambda categories: StoreItem(1, "ZA-TS", "AMAZONE", categories, 9000.0, False, 18.0, 50.0, None)
        self.assertTrue(_matches(item(("FERTILIZERSPREADERS",)), {"kind": "spreader"}))
        self.assertFalse(_matches(item(("SPRAYERS",)), {"kind": "spreader"}))

    def test_hitch_them_together_with_nothing_bought_asks_which(self):
        from companion import coordinator as coordination
        notices = []
        c = coordination.Coordinator(MockWorld([], {"farm-a": (0.0, 0.0)}), None, lambda n, s=False: notices.append(n),
                                     lambda: "farm-a", lambda: "s1")
        self.assertIsNone(c.submit({"version": 1, "command": "hitch_pair", "together": True}))
        self.assertEqual(notices, ["NOTHING_TO_HITCH_TOGETHER"])

    def test_hitch_one_to_the_other_by_name_and_a_tractor_by_its_brand(self):
        from companion.language import RuleInterpreter
        r = RuleInterpreter()
        for words in ("hitch the plow to the deutz", "hitch the deutz to the plow"):
            self.assertEqual(r.interpret(words), {"version": 1, "command": "hitch_pair", "implement": "plow",
                                                  "tractor_name": "deutz"})
        self.assertEqual(r.interpret("hitch the plow to the series 9"),          # live 6f538eb5c4f641fc-2: a place
                         {"version": 1, "command": "hitch_pair", "implement": "plow", "tractor_name": "series 9"})
        self.assertEqual(r.interpret("buy a fendt 942 and the 1124 deckover")["wants"],
                         [{"kind": "tractor", "name": "fendt 942"}, {"kind": "trailer", "name": "1124 deckover"}])
        self.assertEqual(r.interpret("buy a john deere plow")["wants"], [{"kind": "plow", "name": "john deere"}])
