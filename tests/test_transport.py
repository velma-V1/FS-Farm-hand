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
    def test_explicit_trailer_name_selects_that_trailer_over_another_compatible_one(self):
        truck, deck = rig()
        generic = replace(deck, id="d-near", name="Utility Trailer", position=(8.0, 3.0),
                          input_joints=(InputJoint(2, "semitrailerCar", (8.0, 9.0), True, frozenset()),))
        world = MockWorld([generic, truck, deck], PLAYERS, ())
        result = Executor(world).execute(hitch("deckover"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"), result)
        self.assertEqual(world.items[deck.id].attached_to, truck.id)
        self.assertIsNone(world.items[generic.id].attached_to)

    def test_of_two_trailers_one_name_fits_the_named_truck_takes_the_nearer(self):
        # Live 4c41d6e784cc445e-1: four trailers of one name; the first by ID was chosen while
        # the 3650 stood on another's joint (AGAINST_IMPLEMENT). IDs vary: never the ID order.
        for far_id, near_id in (("d-001", "d-999"), ("d-999", "d-001")):
            with self.subTest(near=near_id):
                truck, deck = rig()
                far = replace(deck, id=far_id, position=(300.0, 200.0),
                              input_joints=(InputJoint(2, deck.input_joints[0].joint_type, (300.0, 206.0), True,
                                                       frozenset()),))
                near = replace(deck, id=near_id)
                # Its body nearer the truck than the one hitched, its joint farther (live
                # c06633894f8c4c51-1: by the roots another trailer than the one stood on).
                body = replace(deck, id="d-500", position=(30.0, 15.0),
                               input_joints=(InputJoint(2, deck.input_joints[0].joint_type, (90.0, 60.0), True,
                                                        frozenset()),))
                world = MockWorld([far, body, truck, near], PLAYERS, ())
                result = Executor(world).execute(hitch("deckover"))
                self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"), result)
                self.assertEqual(world.items[near_id].attached_to, truck.id)
                self.assertIsNone(world.items[far_id].attached_to)
                self.assertIsNone(world.items["d-500"].attached_to)

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

    def test_loaded_rig_tries_giants_without_an_autodrive_marker_even_nearby(self):
        from types import SimpleNamespace
        from companion.drive_goal import DriveGoal
        from companion.standalone import StandaloneMove

        class World:
            def __init__(self):
                self.units = [SimpleNamespace(id="truck", position=(0.0, 0.0), belted_to=None),
                              SimpleNamespace(id="deck", position=(-8.0, 0.0), belted_to=None),
                              SimpleNamespace(id="cargo", position=(-8.0, 0.0), belted_to="deck")]
                self.navigated = False

            def snapshot(self):
                return self.units

            def navigate(self, vehicle_id, pose, owner):
                self.navigated = True
                for unit in self.units:
                    unit.position = (unit.position[0] + pose.x, unit.position[1] + pose.z)

            def farm_drive(self, *args):
                raise AssertionError("loaded rig should try GIANTS first")

        world = World()
        StandaloneMove(world)._drive(DriveGoal("truck", ("truck", "deck", "cargo"), (40.0, 0.0), "cargo"),
                                     "job", 0.0)
        self.assertTrue(world.navigated)

    def test_named_trucks_trailer_means_its_attached_deck(self):
        world, tractor, deck, truck = transport_world()
        # A pickup bed can also fold and carry belts. Its trailer remains the deck
        # attached behind it when the player says "the GMC trailer".
        world.items[truck.id] = replace(truck, fold=0.0, folding=False,
                                        belts_fastened=False, belt_markers=deck.belt_markers)
        result = Executor(world).execute(load(tractor_name="series 9", implement_name="gmc"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
        self.assertEqual(result.equipment_ids, (tractor.id, deck.id))
        self.assertEqual(world.items[tractor.id].belted_to, deck.id)

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
            if refused:                        # still off to the side: GIANTS to right behind the ramps
                self.assertAlmostEqual(fronts[1], -5.0 - transport.FRONT_GAPS_M[0])
                self.assertEqual(len(world.aims), 2)
            else:                              # in LOAD's run-in: LOAD lines it up itself
                self.assertEqual(len(world.aims), 1)

    def test_a_run_up_ending_turned_towards_the_axis_is_left_to_the_run_in(self):
        # Live 3389dfbd1b564660-1: GIANTS' run-up GOTO ended on position alone, the Series 9
        # 1.6 m off and 18.4 deg turned (heading for the axis); the near aims then stalled
        # 90 s each. Inside LOAD's run-in (25 deg): LOAD steers it on, no further aim.
        from companion import hitch as transport

        class Turned(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                self.aims = getattr(self, "aims", []) + [pose]
                super().navigate(tractor_id, pose, owner)
                t = self.items[tractor_id]
                yaw = pose.yaw + math.radians(18.4)
                lx, lz = math.cos(pose.yaw) * -1.6, -math.sin(pose.yaw) * -1.6
                self.items[tractor_id] = replace(t, position=(t.position[0] + lx, t.position[1] + lz), heading=yaw,
                                                 ai_root=Pose(t.position[0] + lx, t.position[1] + lz, yaw))

        world, tractor, deck, _ = transport_world(cls=Turned)
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
        self.assertEqual(len(world.aims), 1)
        self.assertNotIn("BACKOFF", result.states)
        self.assertAlmostEqual(transport.HEADING_RUN_IN_RAD, math.radians(25.0))

    def test_a_turned_tractor_needs_six_metres_of_run_in_a_straight_one_does_not(self):
        # Live 95b84283873043d7-1: from 4.1 m behind the ramps' foot, 19 deg turned and 1.5 m off,
        # LOAD reached the foot still 10.5 deg off (NOT_LINED_UP_AT_RAMPS).
        from companion import hitch as transport
        _, tractor, deck, _ = transport_world()
        foot = -5.0 - transport.RAMP_M
        fx, fz = math.sin(deck.heading), math.cos(deck.heading)

        def standing(front, lateral, turn):
            a = front - 0.3 - 2.6                        # the root, its front 2.6 m + 0.3 m ahead
            x = deck.position[0] + fx * a + fz * lateral
            z = deck.position[1] + fz * a - fx * lateral
            return replace(tractor, position=(x, z), heading=deck.heading + turn,
                           ai_root=Pose(x, z, deck.heading + turn))

        turned = math.radians(19.0)
        RAMP_ON_M = transport.RAMP_M - 0.1                    # its front past the trailer's rear
        self.assertFalse(transport.can_run_in(standing(foot - 4.1, -1.5, turned), deck))
        self.assertFalse(transport.can_run_in(standing(foot - 6.2, -1.5, turned), deck))
        self.assertTrue(transport.can_run_in(standing(foot - 7.0, -1.5, turned), deck))
        self.assertTrue(transport.can_run_in(standing(foot - 15.0, -1.5, turned), deck))
        self.assertFalse(transport.can_run_in(standing(foot - 16.0, -1.5, turned), deck))
        self.assertTrue(transport.can_run_in(standing(foot - 4.1, 0.1, 0.0), deck))      # straight: any distance
        self.assertTrue(transport.can_run_in(standing(foot, 0.0, 0.002), deck))          # where UNLOAD leaves it
        self.assertTrue(transport.can_run_in(standing(foot + RAMP_ON_M, 0.0, 0.0), deck))   # up the ramps, straight
        self.assertFalse(transport.can_run_in(standing(2.9, 0.0, 0.0), deck))              # on the deck

    def test_a_turned_tractor_right_behind_the_ramps_backs_off_before_any_goto(self):
        # Live 6601d036cf7b4334-1-h2: left at the ramps' foot, GIANTS did not start it and UNPARK
        # drove it 1 m on towards them. Outside the run-in and right behind: BACKOFF first.
        calls = []

        class Recording(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                calls.append("navigate")
                super().navigate(tractor_id, pose, owner)

            def unpark(self, tractor_id, owner):
                calls.append("unpark")
                return super().unpark(tractor_id, owner)

            def backoff(self, tractor_id, trailer_id, owner):
                calls.append("backoff")
                return super().backoff(tractor_id, trailer_id, owner)

        world, tractor, deck, _ = transport_world(cls=Recording, tractor_ahead=-13.0, tractor_side=-0.3)
        world.items[tractor.id] = replace(world.items[tractor.id], heading=deck.heading + 0.18,
                                          ai_root=Pose(*tractor.position, deck.heading + 0.18))
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
        self.assertEqual(calls[0], "backoff")
        self.assertNotIn("unpark", calls)

    def test_a_tractor_far_behind_inside_loads_straight_window_loads_without_any_goto(self):
        # Live 2026-10-31: the Series 9 17 m behind the deck, 0.34 m off, 4.5 deg turned (inside
        # LOAD's own straight window) was sent round by GIANTS' GOTO and never loaded.
        calls = []

        class Recording(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                calls.append("navigate")
                super().navigate(tractor_id, pose, owner)

            def backoff(self, tractor_id, trailer_id, owner):
                calls.append("backoff")
                return super().backoff(tractor_id, trailer_id, owner)

        world, tractor, deck, _ = transport_world(cls=Recording, tractor_ahead=-17.0, tractor_side=-0.34)
        turned = deck.heading + math.radians(4.5)
        world.items[tractor.id] = replace(world.items[tractor.id], heading=turned,
                                          ai_root=Pose(*tractor.position, turned))
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
        self.assertEqual(calls, [])

    def test_a_tractor_lined_up_behind_or_up_the_ramps_loads_without_any_goto(self):
        # Live 51899b1bbe434794-1: a STOP left the Series 9 straight with its front over the
        # trailer's rear; the approach still sent GIANTS GOTO, BACKOFF and UNPARK, and failed.
        for ahead in (-16.0, -9.5):
            with self.subTest(ahead=ahead):
                calls = []

                class Recording(MockWorld):
                    def navigate(self, tractor_id, pose, owner):
                        calls.append("navigate")
                        super().navigate(tractor_id, pose, owner)

                    def backoff(self, tractor_id, trailer_id, owner):
                        calls.append("backoff")
                        return super().backoff(tractor_id, trailer_id, owner)

                world, tractor, deck, _ = transport_world(cls=Recording, tractor_ahead=ahead, tractor_side=0.0)
                world.items[tractor.id] = replace(world.items[tractor.id], heading=deck.heading,
                                                  ai_root=Pose(*tractor.position, deck.heading))
                result = Executor(world).execute(load())
                self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
                self.assertEqual(calls, [])

    def test_a_load_stopped_not_lined_up_backs_off_only_once_at_rest(self):
        # Live 95b84283873043d7-1: BACKOFF sent 16 ms after LOAD braked from 2.69 km/h was refused
        # NOT_STOPPED. The retry waits for a snapshot with the tractor at rest.
        events = []

        class Rolling(MockWorld):
            loads = 0

            def load(self, tractor_id, trailer_id, direction, owner):
                self.loads += 1
                if self.loads == 1:
                    with self._lock:
                        self.items[tractor_id] = replace(self.items[tractor_id], stopped=False)
                    events.append("load failed")
                    raise StepFailed("NOT_LINED_UP_AT_RAMPS")
                return super().load(tractor_id, trailer_id, direction, owner)

            def snapshot(self):
                items = super().snapshot()
                if "load failed" in events and events[-1] != "at rest":
                    events.append("at rest")
                    with self._lock:
                        for i, item in self.items.items():
                            self.items[i] = replace(item, stopped=True)
                return items

            def backoff(self, tractor_id, trailer_id, owner):
                events.append("backoff" if self.items[tractor_id].stopped else "backoff while moving")
                return super().backoff(tractor_id, trailer_id, owner)

        world, *_ = transport_world(cls=Rolling)
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
        self.assertIn("backoff", events)
        self.assertNotIn("backoff while moving", events)
        self.assertLess(events.index("at rest"), events.index("backoff"))

    def test_a_tractor_stuck_right_behind_the_ramps_backs_off_and_runs_up_again(self):
        # Live 49b5344a47ec418a-1: the Series 9 + plow left at the ramps, turned; GIANTS
        # BLOCKED_AT_START there, UNPARK refused (in the open): BACKOFF 8 m, then the run-up.
        world, tractor, deck, _ = transport_world(tractor_ahead=-9.0, tractor_side=0.6)
        world.boxed = {tractor.id: {"refuse": "UNPARK_NOT_BOXED"}}
        world.items[tractor.id] = replace(world.items[tractor.id], heading=deck.heading + 0.2,
                                          ai_root=Pose(*tractor.position, deck.heading + 0.2))
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
        self.assertIn("BACKOFF", result.states)
        self.assertEqual(world.backed_off, (tractor.id,))

    def test_whichever_aim_fails_a_tractor_left_turned_right_behind_the_ramps_backs_off(self):
        # Live 15fc3a8297604156-1: the run-up ended UNPARK_HANDOFF_BLOCKED and the near aims
        # BLOCKED_AT_START, the Series 9 right behind the ramps 0.35 m off and 15.6 deg turned:
        # BACKOFF once, then the run-in.
        class Refusing(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                if not self.backed_off:
                    raise StepFailed("UNPARK_HANDOFF_BLOCKED")
                super().navigate(tractor_id, pose, owner)

        world, tractor, deck, _ = transport_world(cls=Refusing, tractor_ahead=-9.0, tractor_side=0.35)
        world.items[tractor.id] = replace(world.items[tractor.id], heading=deck.heading + 0.27,
                                          ai_root=Pose(*tractor.position, deck.heading + 0.27))
        result = Executor(world).execute(load())
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LOADED_AND_SECURED"), result)
        self.assertEqual(result.states.count("BACKOFF"), 1)

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

    def test_an_unload_refused_before_moving_leaves_the_tractor_secured_as_found(self):
        # Live 4a63f2e75a1146d4-3: belts released, ramps down, then LOAD_PATH_BLOCKED (the path
        # behind hit an object) and the Series 9 was left loose on the deck with the ramps down.
        class Blocked(MockWorld):
            def load(self, tractor_id, trailer_id, direction, owner):
                if direction < 0:
                    raise StepFailed("LOAD_PATH_BLOCKED")
                return super().load(tractor_id, trailer_id, direction, owner)

        world, tractor, deck, _ = transport_world(cls=Blocked)
        Executor(world).execute(load())
        result = Executor(world).execute(dict(intent(), task="unload_tractor"))
        self.assertEqual((result.status, result.reason), ("FAILED", "LOAD_PATH_BLOCKED"), result)
        after = world.items
        self.assertEqual(after[tractor.id].belted_to, deck.id)
        self.assertEqual((after[deck.id].fold, after[deck.id].belts_fastened), (0.0, True))
        self.assertIn("RAMPS_UP", result.states)

    def test_a_tractor_stopped_on_the_ramps_is_unloaded_from_there(self):
        # Live ac71c4b45aea40dd-1: UNLOAD stopped ENVELOPE_CONTACT with the Series 9 half down the
        # ramps, belts released, ramps down; "unload the deutz" then found no loaded tractor.
        from companion import hitch as transport
        world, tractor, deck, _ = transport_world()
        Executor(world).execute(load())
        on = world.items[tractor.id]
        fx, fz = math.sin(deck.heading), math.cos(deck.heading)
        a, _ = transport.to_frame(deck, on.position)
        rear = transport._ends(deck)[0]
        front = transport._ends(on)[1]
        back = (a + front) - (rear + 1.5)                                   # its front 1.5 m over the trailer's rear
        world.items[tractor.id] = replace(on, position=(on.position[0] - fx * back, on.position[1] - fz * back),
                                          belted_to=None)
        world.items[deck.id] = replace(world.items[deck.id], fold=1.0, belts_fastened=False)
        self.assertTrue(transport.on_ramps(world.items[tractor.id], world.items[deck.id]))
        self.assertFalse(transport.on_deck(world.items[tractor.id], world.items[deck.id]))
        result = Executor(world).execute(dict(intent(), task="unload_tractor"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "UNLOADED"), result)
        self.assertTrue(transport.off_ramps(world.items[tractor.id], world.items[deck.id]))

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

    def test_a_loaded_rig_with_farmhands_drive_goes_a_long_trip_by_autodrive(self):
        # Live e738799ee3be4904-1: the GMC with the Series 9 from field 90 toward field 3 by
        # Farmhand's own drive, still searching after 4 minutes. AutoDrive drives a loaded rig.
        world, truck, deck, series9 = haul_world(loaded=True)
        drives = []

        class WithOwnDrive(type(world)):
            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                drives.append(target)
                raise AssertionError("Farmhand's own drive over the whole trip")

        world.__class__ = WithOwnDrive
        result = Executor(world).execute({"version": 1, "request_id": "h-1", "farm_id": "farm-a", "task": "move_pair",
                                          "target": {"type": "field_entrance", "field": 3}, "cargo_name": "series 9"})
        self.assertNotIn("FAILED", result.status, result)
        self.assertEqual(drives, [])

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


def haul_world(hitched=False, loaded=False, ramps_down=False, marker=True, stall=False, autodrive=True):
    """The user's transport rig on the mock (mock-only belts, ramps and LOAD): a GMC with a
    gooseneck joint (AutoDrive unless ``autodrive`` is False), a deckover with Foldable ramps
    and four belts 40 m off (heading 0), a Series 9 (DEUTZFAHR) standing apart; an AutoDrive
    marker "Field 3" far away. ``stall``: AutoDrive never moves the rig once it carries the
    Series 9 (its drive ends AD_WAITING_ON_OBSTACLE, as live 831d6868b9df40b0-1 never moved)."""
    from protocol.model import Destination
    truck, deck = rig()
    truck = replace(truck, capabilities=AUTODRIVE_TRACTOR if autodrive else GOTO_TRACTOR, size=(6.5, 2.4, 2.0, 0.0),
                    fold=1.0)
    belts = tuple((40.0 - 1.3, 1.0, 20.0 + a, 40.0 + 1.3, 1.0, 20.0 + a) for a in (-4.0, -2.0, 0.0, 2.0))
    deck = replace(deck, size=(10.0, 2.6, 1.2, 0.0), belts_fastened=False, belt_markers=belts, kinds=frozenset({"trailer"}))
    series9 = Equipment("s-9", "tractor", "farm-a", (10.0, -10.0), name="Series 9 2019", brand="DEUTZFAHR",
                        heading=1.2, ai_root=Pose(10.0, -10.0, 1.2), capabilities=GOTO_TRACTOR,
                        size=(5.2, 2.7, 3.0, 0.3))
    markers = (Destination("m-3", (900.0, 900.0), "Field 3"),) if marker else ()
    world = MockWorld([deck, series9, truck], PLAYERS, markers)
    world.mock_field_places = (Destination("field-3", (700.0, 650.0), "field 3"),)
    world.mock_ad_loaded_stall = "AD_WAITING_ON_OBSTACLE" if stall else None
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
    coordinator planning each step from observed state, the real executor on the mock. GIANTS
    GOTO does not start a trailer carrying a belted vehicle (live d14230aa9fb24d30-3); AutoDrive
    counts the belted cargo as the rig's own (AttacherJoints/TensionBelts getHasObjectMounted),
    so it drives the loaded rig to its marker and one request hauls and unloads. When AutoDrive
    does not move it (its drive ends with what it waited on) or there is no AutoDrive, the
    player drives it there (assisted) and "continue" unloads on the observed arrival."""

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

    def test_one_request_hitches_loads_secures_drives_and_unloads(self):
        from companion import hitch as transport
        world, truck, deck, series9 = haul_world()
        c, notices = self.run_goal(world)
        job = c.last
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
        tasks = [s.split(":")[0] for s in job.states]
        self.assertEqual([t for i, t in enumerate(tasks) if i == 0 or tasks[i - 1] != t],
                         ["hitch_pair", "stage_transport", "load_tractor", "move_pair", "unload_tractor"])
        self.assertIn("load_tractor:BELTS", job.states)                           # secured before it went
        self.assertIn("move_pair:DELIVER", job.states)                            # AutoDrive drove it
        after = world.items
        self.assertLess(math.dist(after[truck.id].position, (900.0, 900.0)), 1.0)
        self.assertTrue(transport.off_ramps(after[series9.id], after[deck.id]))
        self.assertEqual((after[deck.id].fold, after[deck.id].belts_fastened), (0.0, False))
        self.assertIsNone(c.kept)
        self.assertEqual(notices[-1], "COMPLETE_HAUL")

    def test_autodrives_saved_network_is_read_as_it_writes_it(self):
        # AutoDrive_config.xml's <waypoints> (as the user's Riverbend Springs file holds them):
        # a dead end 1-2 by the rig, the routed road 3-4-5 to the marker at waypoint 5.
        from companion.hitch import ad_route_start, ad_routed, parse_ad_graph
        text = """<?xml version="1.0"?>\n<AutoDrive><waypoints>
            <id>1,2,3,4,5</id><x>0,4,60,100,500</x><y>1,1,1,1,1</y><z>0,0,0,0,0</z>
            <out>2;-1;4;5;-1</out><incoming>-1;1;-1;3;4</incoming><flags>2,2,2,2,2</flags>
            </waypoints><mapmarker><mm1><id>5.000000</id><name>Farm Barn</name></mm1></mapmarker></AutoDrive>"""
        graph = parse_ad_graph(text)
        self.assertEqual(graph.out, {1: (2,), 2: (), 3: (4,), 4: (5,), 5: ()})
        self.assertFalse(ad_routed(graph, (1.0, 0.0), (500.0, 0.0)))          # its nearest is the dead end
        (x, z), yaw, wp = ad_route_start(graph, (1.0, 0.0), (500.0, 0.0))
        self.assertEqual((wp, (x, z)), (3, (60.0, 0.0)))
        self.assertAlmostEqual(yaw, math.pi / 2)                               # along the road to 4
        self.assertTrue(ad_routed(graph, (61.0, 0.0), (500.0, 0.0)))
        self.assertIsNone(ad_routed(graph, (1.0, 0.0), (250.0, 9.0)))          # not a marker on the network
        self.assertIsNone(ad_route_start(graph, (1.0, 0.0), (500.0, 0.0), max_m=30.0))

    def test_autodrives_dead_end_lane_is_joined_to_its_road_before_anything_moves(self):
        # Live 2026-10-22: the store yard's lane stops 5.5 m short of the road it joins. With the
        # mod's ad_connect (AutoDrive's own setConnectionBetween, after a backup) there is a route
        # from where the rig stands: nothing is staged, the rig is loaded there and AutoDrive
        # drives it.
        from companion.hitch import ADGraph
        world, truck, deck, series9 = haul_world()
        world.mock_ad_graph = ADGraph(points={1: (52.0, 25.0), 2: (56.0, 25.0), 10: (60.0, 25.0), 12: (900.0, 900.0)},
                                      out={1: (2,), 2: (), 10: (12,), 12: ()})
        world.mock_ad_link = (2, 10)
        c, notices = self.run_goal(world)
        job = c.last
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
        self.assertIn("stage_transport:AD_LINK", job.states)
        self.assertNotIn("stage_transport:STAGE", job.states)
        self.assertEqual(world.ad_links_made, ((2, 10),))
        self.assertLess(math.dist(world.items[truck.id].position, (900.0, 900.0)), 1.0)

    def test_staging_prefers_where_autodrives_own_path_runs_out(self):
        # Live 2026-10-22 (Riverbend Springs): the lane from the store yard (11800-11808) stops
        # 5.5 m short of the road it joins (5767); a parallel road (3515) is nearer the rig but
        # GIANTS' agent could not plan there (planning 96 s). The lane's end comes first.
        from companion.hitch import ADGraph, ad_route_starts
        graph = ADGraph(points={1: (0.0, 0.0), 2: (0.0, 40.0), 3: (0.0, 80.0),       # the dead-end lane
                                10: (5.0, 82.0), 11: (50.0, 82.0),                  # the road it should join
                                20: (60.0, 0.0), 21: (60.0, 40.0), 22: (500.0, 500.0)},
                        out={1: (2,), 2: (3,), 3: (), 10: (11,), 11: (22,), 20: (21,), 21: (22,), 22: ()})
        starts = ad_route_starts(graph, (1.0, -2.0), (500.0, 500.0))
        self.assertEqual([wp for _, _, wp in starts], [10, 20, 21])

    def test_with_its_own_drive_farmhand_loads_where_the_tractor_is_without_staging(self):
        # Live 77e30fc15938406c-1: the GMC sent 133 m the other way to an AutoDrive waypoint.
        from companion import coordinator as coordination
        from protocol.model import Result
        world, truck, deck, series9 = haul_world(hitched=True)
        tasks = []

        class Own:
            def _farm_drives(self):
                return True

            def execute(self, intent, cancel=None, on_state=None, job=None):
                tasks.append(intent["task"])
                return Result("BLOCKED", "STOP_HERE", (), ())

        world.retained = lambda: ()
        c = coordination.Coordinator(world, Own(), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        from companion.language import RuleInterpreter
        c.submit(RuleInterpreter().interpret("Take the Deutz to field 3 and unload it"))
        c.wait(10)
        self.assertEqual(tasks[:1], ["load_tractor"])

    def test_the_empty_transport_is_staged_where_autodrive_has_a_route_before_it_is_loaded(self):
        # Live 2026-10-22 (Riverbend Springs): the waypoint nearest the GMC was on a dead end,
        # AutoDrive "could not calculate path". Its saved network (read only) says so; GIANTS
        # GOTO takes the empty GMC and Deckover onto the nearest waypoint that has a route, the
        # Series 9 is loaded there and AutoDrive drives it to "Field 3".
        from companion.hitch import ADGraph as Graph
        world, truck, deck, series9 = haul_world()
        world.mock_ad_graph = Graph(points={1: (52.0, 25.0), 2: (56.0, 25.0),             # the dead end
                                            10: (120.0, 25.0), 11: (300.0, 300.0), 12: (900.0, 900.0)},
                                    out={1: (2,), 2: (), 10: (11,), 11: (12,), 12: ()})
        c, notices = self.run_goal(world)
        job = c.last
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
        self.assertIn("stage_transport:STAGE", job.states)
        self.assertLess(job.states.index("stage_transport:STAGE"), job.states.index("load_tractor:LOAD"))
        self.assertLess(math.dist(world.items[truck.id].position, (900.0, 900.0)), 1.0)
        # Without staging (already loaded where there is no route): AutoDrive refuses up front
        # (AD_NO_ROUTE) and the player drives it.
        world, truck, deck, series9 = haul_world(loaded=True)
        world.mock_ad_graph = Graph(points={1: (52.0, 25.0), 12: (900.0, 900.0)}, out={1: (), 12: ()})
        c, notices = self.run_goal(world)
        self.assertEqual((c.last.state, c.last.reason), ("FAILED", "AD_NO_ROUTE"), c.last.states)
        self.assertIsNotNone(c.kept)

    def test_one_transport_throughout_the_named_trucks_when_there_are_two(self):
        # Live 2026-10-22: the farm also has the 379 with an LB 56 26 lowboy; the load took the
        # lowboy nearest the Series 9, not the Deckover the haul had staged.
        world, truck, deck, series9 = haul_world(hitched=True)
        lowboy = replace(world.items[deck.id], id="lb-1", name="LB 56 26", attached_to="pb-1",
                         position=(12.0, -10.0), belt_markers=tuple((m[0] - 28.0, m[1], m[2] - 30.0, m[3] - 28.0, m[4],
                                                                     m[5] - 30.0) for m in world.items[deck.id].belt_markers))
        peterbilt = replace(world.items[truck.id], id="pb-1", name="379", attached_to="lb-1", position=(12.0, -1.0),
                            ai_root=Pose(12.0, -1.0, 0.0))
        world.items.update({lowboy.id: lowboy, peterbilt.id: peterbilt})
        c, notices = self.run_goal(world, "use the gmc to take the deutz to field 3 and unload it")
        job = c.last
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
        self.assertIn(deck.id, job.equipment)
        self.assertNotIn(lowboy.id, job.equipment)
        self.assertEqual(world.items[lowboy.id].position, lowboy.position)          # never touched

    def test_when_autodrive_does_not_move_the_loaded_rig_the_player_drives_it(self):
        # Assisted, never counted as autonomous: AutoDrive's drive ended with what it waited on;
        # the goal is kept, "continue" before the rig is there does not hand it back to AutoDrive,
        # and on the observed arrival it unloads without driving.
        world, truck, deck, series9 = haul_world(stall=True)
        c, notices = self.run_goal(world)
        first = c.last
        self.assertEqual((first.state, first.reason), ("FAILED", "AD_WAITING_ON_OBSTACLE"), first.states)
        self.assertIn("move_pair:DELIVER", first.states)
        self.assertEqual(world.items[series9.id].belted_to, deck.id)              # waiting, loaded
        self.assertIsNotNone(c.kept)
        job = self.finish(c, world, truck, deck, (700.0, 700.0))                  # driven part of the way
        self.assertEqual((job.state, job.reason), ("BLOCKED", "LOADED_NO_DRIVER"), job.states)
        self.assertNotIn("move_pair:RESERVE", job.states)
        job = self.finish(c, world, truck, deck, (893.0, 890.0))                  # 10 m from "Field 3"
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
        self.assertNotIn("move_pair:DELIVER", job.states)                         # nothing drove it

    def test_without_autodrive_the_loaded_rig_is_the_players_to_drive(self):
        world, truck, deck, series9 = haul_world(autodrive=False)
        c, notices = self.run_goal(world)
        self.assertEqual((c.last.state, c.last.reason), ("BLOCKED", "LOADED_NO_DRIVER"), c.last.states)
        self.assertNotIn("move_pair:RESERVE", c.last.states)                      # refused before anything
        self.assertEqual(notices[-1], "BLOCKED_LOADED_NO_DRIVER")
        job = self.finish(c, world, truck, deck, (705.0, 650.0))                  # at GIANTS' field 3 point
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)

    def test_from_partly_done_states_nothing_completed_is_repeated(self):
        for label, prep, first in (("hitched", dict(hitched=True), "stage_transport"),
                                   ("loaded and belted", dict(loaded=True), "move_pair"),
                                   ("loaded, ramps left down", dict(loaded=True, ramps_down=True), "load_tractor")):
            with self.subTest(label):
                world, truck, deck, series9 = haul_world(**prep)
                c, _ = self.run_goal(world)
                job = c.last
                self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
                self.assertEqual(job.states[0].split(":")[0], first)
                self.assertNotIn("hitch_pair:ATTACH", job.states)
                if prep.get("loaded"):
                    self.assertNotIn("load_tractor:LOAD", job.states)          # never driven up twice
                    if not prep.get("ramps_down"):
                        self.assertNotIn("load_tractor:BELTS", job.states)

    def test_no_unload_without_the_observed_arrival(self):
        # Driven only part of the way: "continue" does not unload short of the destination.
        world, truck, deck, series9 = haul_world(loaded=True, stall=True)
        c, _ = self.run_goal(world)
        self.assertEqual(c.last.reason, "AD_WAITING_ON_OBSTACLE")
        job = self.finish(c, world, truck, deck, (700.0, 700.0))                  # 280 m short
        self.assertEqual((job.state, job.reason), ("BLOCKED", "LOADED_NO_DRIVER"), job.states)
        self.assertNotIn("unload_tractor", " ".join(job.states))
        self.assertEqual(world.items[series9.id].belted_to, deck.id)
        self.assertIsNotNone(c.kept)

    def test_the_destination_is_autodrive_s_marker_made_once_where_its_network_meets_the_field(self):
        world, truck, deck, series9 = haul_world(loaded=True, marker=False)
        world.mock_ad_fields = {3: (-850.0, 420.0)}
        c, _ = self.run_goal(world)
        job = c.last
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
        self.assertEqual(world.ad_markers_made, (3,))                             # where AutoDrive drove it
        self.assertLess(math.dist(world.items[truck.id].position, (-850.0, 420.0)), 1.0)

    def test_a_farm_building_gets_an_autodrive_marker_by_its_own_name(self):
        from protocol.model import Destination
        world, truck, deck, series9 = haul_world(loaded=True, marker=False)
        barn = Destination("pl-1", (-759.0, 129.0), "Farm Barn")
        world.place_list = (barn,)
        world.mock_ad_places = {(-759.0, 129.0): (-745.0, 140.0)}
        c, _ = self.run_goal(world, "take the deutz to the farm barn and unload it")
        job = c.last
        self.assertEqual((job.state, job.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), job.states)
        self.assertEqual(world.ad_markers_made, ("Farm Barn",))
        self.assertLess(math.dist(world.items[truck.id].position, (-745.0, 140.0)), 1.0)

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


class TransportSelectionTests(unittest.TestCase):
    """Feasibility before proximity (BUILD_SPEC 11, the user 2026-10-22): a transport already
    carrying something or whose deck the rig does not fit is not taken; a named one is kept
    and why it cannot take the load is said. IDs opaque."""

    def two_transports(self, near_cargo=False, near_short=False):
        from protocol.model import Equipment as E
        world, tractor, deck, truck = transport_world()
        yaw = deck.heading
        fx, fz = math.sin(yaw), math.cos(yaw)

        def at(a, l=0.0, x0=300.0, z0=-120.0):
            return (x0 + fx * a + fz * l, z0 + fz * a - fx * l)
        # A second rig nearer the tractor: a 379 towing a lowboy.
        x0, z0 = tractor.position[0] + 12.0, tractor.position[1]
        spacing = (-1.5, -0.5, 0.5, 1.5) if near_short else (-4.0, -2.0, 0.0, 2.0)
        lowboy = E("w-902", "implement", "farm-a", at(0.0, x0=x0, z0=z0), name="LB 56 26", heading=yaw,
                   kinds=frozenset({"trailer"}), attached_to="k-14", fold=0.0, folding=False, belts_fastened=False,
                   size=(10.0, 2.6, 1.2, 0.0), belted=("x-1",) if near_cargo else (),
                   belt_markers=tuple((*at(a, -1.3, x0, z0)[:1], 1.0, at(a, -1.3, x0, z0)[1],
                                       *at(a, 1.3, x0, z0)[:1], 1.0, at(a, 1.3, x0, z0)[1]) for a in spacing))
        truck2 = E("k-14", "tractor", "farm-a", at(9.0, x0=x0, z0=z0), name="379 2050hp", heading=yaw,
                   attached_to="w-902", size=(7.0, 2.5, 3.0, 0.0))
        items = tuple(world.snapshot()) + (lowboy, truck2)
        return items, tractor, deck, lowboy

    def step(self, items, **names):
        from companion.coordinator import haul_step
        return haul_step(items, "farm-a", "series 9", {"type": "place", "name": "farm"}, False, staged=True, **names)

    def test_the_nearest_transport_is_taken_when_it_can_take_the_load(self):
        items, tractor, deck, lowboy = self.two_transports()
        self.assertEqual(self.step(items)[0]["implement_name"], "lb 56 26")

    def test_a_nearer_transport_carrying_something_or_too_short_is_passed_over(self):
        for case in ({"near_cargo": True}, {"near_short": True}):
            with self.subTest(**case):
                items, tractor, deck, lowboy = self.two_transports(**case)
                self.assertEqual(self.step(items)[0]["implement_name"], "1124 deckover trailer")

    def test_a_named_transport_that_cannot_take_the_load_is_kept_and_explained(self):
        items, *_ = self.two_transports(near_cargo=True)
        self.assertEqual(self.step(items, truck_name="379"), (None, "TRANSPORT_LOADED"))
        items, *_ = self.two_transports(near_short=True)
        self.assertEqual(self.step(items, transport_name="lb 56"), (None, "TRANSPORT_TOO_SMALL"))


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
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), c.last.states)
        self.assertEqual([i["task"] for i in ran], ["load_tractor", "move_pair", "unload_tractor"])

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
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), c.last.states)
        self.assertEqual(ran[1]["target"], {"type": "field_entrance", "field": 2})     # now bound for field 2
        self.assertLess(math.dist(world.items[truck.id].position, (-600.0, 300.0)), 1.0)
        self.assertIn("RETARGETED", notices)
        self.assertIn("RETARGETED", notices)

    def test_stop_after_a_new_destination_drives_nowhere_and_continue_goes_there(self):
        # AGENTS.md 17: STOP invalidates stale queued movement. "Take it to field 2 instead",
        # then STOP: the job's own end resumed the goal to field 2 anyway, as if never stopped.
        from protocol.model import Destination, Result
        world, truck, deck, series9 = haul_world(loaded=True)
        world.destination_map = {**world.destination_map, "m-2": Destination("m-2", (-600.0, 300.0), "Field 2")}
        started, release = [], __import__("threading").Event()

        def interrupt(intent, cancel):
            if intent["task"] == "move_pair" and not started:
                started.append(intent["target"])
                cancel.wait(5)                          # driving until the retarget's stop arrives
                release.wait(5)                         # its cleanup still running when STOP comes
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", (truck.id, deck.id), ("FIND", "RESERVE", "DELIVER"))
            return None

        c, ran, notices, _ = self.coordinator(world, interrupt)
        self.haul(c)
        for _ in range(100):
            if started:
                break
            c.wait(0.05)
        c.submit({"version": 1, "command": "retarget", "target": {"type": "field_entrance", "field": 2}})
        c.submit({"version": 1, "command": "stop"})                 # the player's STOP
        release.set()
        self.wait_idle(c)
        self.assertEqual(c.last.state, "CANCELLED")
        self.assertEqual(len(ran), 1)                                # nothing started by itself
        self.assertNotIn("RESUMING", notices)
        self.assertEqual(notices[-1], "PAUSED")
        self.assertEqual(c.kept.target, {"type": "field_entrance", "field": 2})   # the latest destination
        c.resume()                                                   # "continue": to field 2
        self.wait_idle(c)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "HAULED_AND_UNLOADED"), c.last.states)
        self.assertEqual(ran[1]["target"], {"type": "field_entrance", "field": 2})

    def test_a_stop_between_taking_and_starting_a_queued_request_drops_it(self):
        # The window between a finished job taking the next queued request and starting it:
        # a STOP there must still win (live: a queued request once ran behind STOP).
        from protocol.model import Result
        world, truck, deck, series9 = haul_world(loaded=True)
        c, ran, notices, threading = self.coordinator(world, lambda intent, cancel: Result(
            "COMPLETE", "SAVED", (), ("SAVE",)) if intent["task"] == "save_game" else None)
        taken = c.start("save_game", "here")
        self.assertIsNotNone(taken)
        stops = c._stops
        c.stop()                                                     # after the request was taken
        self.assertIsNone(c.start("save_game", "here", stops=stops))
        self.wait_idle(c)
        self.assertEqual(sum(1 for i in ran if i["task"] == "save_game"), 1)
        self.assertIsNotNone(c.start("save_game", "here", stops=c._stops))   # no stop since: it starts
        self.wait_idle(c)

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
        self.assertEqual(c.last.reason, "HAULED_AND_UNLOADED", c.last.states)
        self.assertLess(math.dist(world.items[truck.id].position, (-600.0, 300.0)), 1.0)


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
        world, truck, deck, series9 = haul_world(loaded=True, stall=True)
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
            self.assertEqual(run("take the deutz to field 3 and unload it").reason, "AD_WAITING_ON_OBSTACLE")
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
