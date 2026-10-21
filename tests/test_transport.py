"""Truck and gooseneck trailer (the user's completion plan, stage 3), on the mock world
(simulator/world.py; mock-only fold state, nothing here is GIANTS behaviour): a regular
bed's tailgate is lowered (GIANTS Foldable, the `fold` op) and verified before the
gooseneck is reversed under, stays down while hitched (clearance is never assumed), and
is down again before unhitching.
"""
from dataclasses import replace
import unittest

from companion.executor import Executor
from protocol.model import Equipment, InputJoint, OutputJoint, Pose
from simulator.world import MockWorld
from tests.test_farmhand import GOTO_TRACTOR, PLAYERS, intent

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


if __name__ == "__main__":
    unittest.main()
