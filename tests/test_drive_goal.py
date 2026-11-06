"""Goal selection must preserve an observed vehicle and everything it carries."""
import unittest

from protocol.model import Equipment
from companion.drive_goal import resolve_drive_goal, GoalError


def unit(identifier, name, category, *, attached=None, belted_to=None, belted=(),
         farm="1", position=(0.0, 0.0), busy=False):
    return Equipment(id=identifier, name=name, category=category, farm_id=farm,
                     position=position, attached_to=attached, belted_to=belted_to,
                     belted=belted, busy=busy, capabilities=frozenset({"move", "stop"}))


class DriveGoalTests(unittest.TestCase):
    def test_named_vehicle_alone_is_a_complete_rig(self):
        gmc = unit("611", "2024 GMC Sierra Denali", "tractor")
        goal = resolve_drive_goal((gmc,), "1", "gmc", None, (-759.0, 129.0))
        self.assertEqual((goal.vehicle_id, goal.rig_ids, goal.target),
                         ("611", ("611",), (-759.0, 129.0)))

    def test_belted_cargo_stays_in_the_existing_rig(self):
        truck = unit("611", "2024 GMC Sierra Denali", "tractor", attached="620")
        deck = unit("620", "1124 Deckover Trailer", "implement", attached="611", belted=("622",))
        tractor = unit("622", "Series 9 2019", "tractor", belted_to="620")
        goal = resolve_drive_goal((tractor, deck, truck), "1", "gmc", "series 9", (-629.0, 211.0))
        self.assertEqual((goal.vehicle_id, goal.rig_ids, goal.cargo_id),
                         ("611", ("611", "620", "622"), "622"))

    def test_a_header_locked_on_the_towed_trailer_goes_with_the_combine(self):
        from dataclasses import replace
        combine = unit("689", "MF 8570", "harvester", attached="688")
        trailer = unit("688", "MF 8570 Trailer", "implement", attached="689")
        header = replace(unit("690", "MF 8570 Header", "implement"), mounted_to="688")
        goal = resolve_drive_goal((header, trailer, combine), "1", "mf 8570", None, (-917.0, 183.0))
        self.assertEqual(goal.rig_ids, ("689", "688", "690"))

    def test_belted_tractor_and_mounted_implement_remain_one_cargo_group(self):
        truck = unit("611", "GMC Sierra", "tractor", attached="620")
        deck = unit("620", "1124 Deckover", "implement", attached="611", belted=("621", "622"))
        plow = unit("621", "Titan Plow", "implement", attached="622", belted_to="620")
        tractor = unit("622", "Series 9", "tractor", attached="621", belted_to="620")
        goal = resolve_drive_goal((plow, truck, tractor, deck), "1", "gmc", "series 9", (-629.0, 211.0))
        self.assertEqual((goal.rig_ids, goal.cargo_id), (("611", "620", "621", "622"), "622"))

    def test_unbelted_mount_on_cargo_is_not_silently_left_out(self):
        truck = unit("611", "GMC", "tractor", attached="620")
        deck = unit("620", "Deckover", "implement", attached="611", belted=("622",))
        tractor = unit("622", "Series 9", "tractor", attached="621", belted_to="620")
        plow = unit("621", "Titan Plow", "implement", attached="622")
        with self.assertRaisesRegex(GoalError, "CARGO_STATE_UNREADABLE"):
            resolve_drive_goal((truck, deck, tractor, plow), "1", "gmc", "series 9", (1.0, 2.0))

    def test_named_cargo_must_be_observed_belted_to_that_rig(self):
        truck = unit("611", "GMC", "tractor", attached="620")
        deck = unit("620", "Deckover", "implement", attached="611")
        tractor = unit("622", "Series 9", "tractor")
        with self.assertRaisesRegex(GoalError, "CARGO_NOT_ON_RIG"):
            resolve_drive_goal((truck, deck, tractor), "1", "gmc", "series 9", (1.0, 2.0))

    def test_other_farm_and_busy_vehicle_are_never_selected(self):
        for truck in (unit("611", "GMC", "tractor", farm="2"),
                      unit("612", "GMC", "tractor", busy=True)):
            with self.subTest(truck=truck):
                with self.assertRaises(GoalError):
                    resolve_drive_goal((truck,), "1", "gmc", None, (1.0, 2.0))

    def test_ambiguous_vehicle_name_is_refused(self):
        trucks = (unit("1", "GMC Sierra", "tractor"), unit("2", "GMC Denali", "tractor"))
        with self.assertRaisesRegex(GoalError, "VEHICLE_AMBIGUOUS"):
            resolve_drive_goal(trucks, "1", "gmc", None, (1.0, 2.0))


if __name__ == "__main__":
    unittest.main()
