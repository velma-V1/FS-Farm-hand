"""A point-to-point goal keeps the observed rig intact through arrival."""
from dataclasses import replace
import math
import unittest

from companion.standalone import APPROACH_M, ARRIVE_M, CLEAR_M, StandaloneMove, _edge_point
from companion.executor import Executor
from protocol.model import Destination, Equipment


def unit(identifier, name, category, *, attached=None, belted_to=None, belted=(), position=(0.0, 0.0)):
    return Equipment(identifier, category, "1", position, name=name, attached_to=attached,
                     belted_to=belted_to, belted=belted, capabilities=frozenset({"move", "stop"}))


FIELD_5 = ((-640.0, 200.0), (-610.0, 200.0), (-610.0, 230.0), (-640.0, 230.0))


class World:
    def __init__(self, items, outline=FIELD_5, stop_short=0.0):
        self.items = {item.id: item for item in items}
        self.outline, self.stop_short = outline, stop_short
        self.calls = []
        self.owner = None

    def snapshot(self):
        return tuple(self.items.values())

    def places(self):
        return (Destination("barn", (-759.0, 129.0), "Farm Barn"),)

    def destinations(self):
        return ()

    def field_places(self):
        return (Destination("field-5", (-629.0, 211.0), "Field 5", self.outline),)

    def player_position(self, farm_id):
        return (-696.0, 139.0)

    def reserve_rig(self, ids, owner):
        self.calls.append(("reserve", ids))
        if self.owner is not None:
            return False
        self.owner = owner
        self.items = {i: replace(item, reserved_by=owner) if i in ids else item
                      for i, item in self.items.items()}
        return True

    def farm_drive(self, vehicle_id, target, owner, area=0.0):
        self.calls.append(("drive", vehicle_id, target, area))
        origin = self.items[vehicle_id].position
        if area:   # the game's chosen approach point: here 20 m short of the centre
            span = math.dist(origin, target)
            target = tuple(t - (t - o) * 20.0 / span for t, o in zip(target, origin))
        span = math.dist(origin, target)
        share = 0.0 if span == 0 else max(0.0, span - self.stop_short) / span
        delta = ((target[0] - origin[0]) * share, (target[1] - origin[1]) * share)
        self.items = {i: replace(item, position=(item.position[0] + delta[0], item.position[1] + delta[1]))
                      for i, item in self.items.items()}

    def stop(self, vehicle_id, owner):
        self.calls.append(("stop", vehicle_id))
        return True

    def release(self, owner):
        self.calls.append(("release", owner))
        self.items = {i: replace(item, reserved_by=None) for i, item in self.items.items()}


class StandaloneMoveTests(unittest.TestCase):
    def test_named_move_enters_the_standalone_core(self):
        world = World((unit("611", "GMC Sierra", "tractor", position=(-696.0, 139.0)),))
        result = Executor(world).execute({"version": 1, "request_id": "gmc-barn", "farm_id": "1",
                                          "task": "move_pair", "tractor_name": "gmc",
                                          "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(world.calls[1][0], "drive")

    def test_bare_named_vehicle_goes_to_a_building_usable_spot(self):
        world = World((unit("611", "GMC Sierra", "tractor", position=(-696.0, 139.0)),))
        result = StandaloneMove(world).execute({"request_id": "a", "farm_id": "1", "task": "move_pair",
                                                "tractor_name": "gmc", "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.equipment_ids, ("611",))
        self.assertEqual([c[0] for c in world.calls], ["reserve", "drive", "stop", "release"])
        # The barn's centre and its approach radius: the game picks the approach point
        # the rig can reach (a single standoff beside it was fenced in live).
        self.assertEqual(world.calls[1][2:], ((-759.0, 129.0), APPROACH_M))

    def test_existing_belted_rig_moves_without_reassembly(self):
        truck = unit("611", "GMC Sierra", "tractor", attached="620")
        deck = unit("620", "1124 Deckover", "implement", attached="611", belted=("622",))
        cargo = unit("622", "Series 9", "tractor", belted_to="620")
        world = World((cargo, deck, truck))
        result = StandaloneMove(world).execute({"request_id": "b", "farm_id": "1", "task": "move_pair",
                                                "tractor_name": "gmc", "cargo_name": "series 9",
                                                "target": {"type": "field_entrance", "field": 5}})
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.equipment_ids, ("611", "620", "622"))
        self.assertEqual(world.items["611"].attached_to, "620")
        self.assertEqual(world.items["622"].belted_to, "620")
        self.assertFalse(any(c[0] in ("attach", "detach", "load") for c in world.calls))


    def field_trip(self, stop_short, outline=FIELD_5):
        truck = unit("611", "GMC Sierra", "tractor", position=(-700.0, 215.0))
        world = World((truck,), outline, stop_short)
        result = StandaloneMove(world).execute({"request_id": "f", "farm_id": "1", "task": "move_pair",
                                                "tractor_name": "gmc",
                                                "target": {"type": "field_entrance", "field": 5}})
        return world, result

    def test_a_field_is_reached_at_the_nearest_point_of_its_edge(self):
        world, result = self.field_trip(0.0)
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(world.calls[1][2], (-640.0, 215.0))   # its west edge, not its centre

    def test_a_rig_on_the_fields_boundary_has_arrived_but_short_of_it_has_not(self):
        self.assertEqual(self.field_trip(2.5)[1].status, "COMPLETE")
        world, result = self.field_trip(5.0)
        self.assertEqual((result.status, result.reason), ("FAILED", "ARRIVAL_NOT_OBSERVED"))

    def test_a_field_without_a_readable_edge_keeps_its_point_and_a_place_its_approach_area(self):
        world, result = self.field_trip(0.0, outline=())
        self.assertEqual(world.calls[1][2:], ((-629.0, 211.0), 0.0))
        short = APPROACH_M + ARRIVE_M + 1.0 - 20.0   # stopped outside every approach point
        world = World((unit("611", "GMC Sierra", "tractor", position=(-659.0, 129.0)),), stop_short=short)
        result = StandaloneMove(world).execute({"request_id": "a", "farm_id": "1", "task": "move_pair",
                                                "tractor_name": "gmc", "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual((result.status, result.reason), ("FAILED", "ARRIVAL_NOT_OBSERVED"))


    def test_a_towed_unit_inside_the_field_counts_for_the_rig(self):
        truck = unit("611", "GMC Sierra", "tractor", attached="620", position=(-650.0, 215.0))
        deck = unit("620", "1124 Deckover", "implement", attached="611", position=(-636.0, 215.0))
        world = World((truck, deck), stop_short=0.0)
        world.farm_drive = lambda vehicle_id, target, owner, area: world.calls.append(("drive", vehicle_id, target))
        result = StandaloneMove(world).execute({"request_id": "t", "farm_id": "1", "task": "move_pair",
                                                "tractor_name": "gmc",
                                                "target": {"type": "field_entrance", "field": 5}})
        self.assertEqual(result.status, "COMPLETE")   # the root 10 m out, its trailer on the field
        self.assertEqual(world.calls[1][2], (-640.0, 215.0))   # its own trailer never pushes the target

    def test_a_field_edge_point_held_by_parked_equipment_moves_along_the_edge(self):
        # Live: the GMC rig was sent to the edge point a parked 3650 and its stand stood on.
        truck = unit("611", "GMC Sierra", "tractor", position=(-700.0, 215.0))
        parked = unit("615", "3650", "tractor", attached="614", position=(-641.0, 216.0))
        stand = unit("614", "Solid Stand", "implement", attached="615", position=(-640.0, 212.0))
        world = World((truck, parked, stand))
        result = StandaloneMove(world).execute({"request_id": "p", "farm_id": "1", "task": "move_pair",
                                                "tractor_name": "gmc",
                                                "target": {"type": "field_entrance", "field": 5}})
        self.assertEqual(result.status, "COMPLETE")
        target = world.calls[1][2]
        self.assertAlmostEqual(_edge_point(FIELD_5, target)[1], 0.0, places=6)
        self.assertGreaterEqual(min(math.dist(target, p) for p in ((-641.0, 216.0), (-640.0, 212.0))), CLEAR_M)
        self.assertLess(math.dist(target, (-700.0, 215.0)), 70.0)   # still the near side, not across the field


    def test_fetching_an_implement_already_hitched_moves_that_rig(self):
        # Live a852385ab373498b-2: "take the 3650 and planter to field 4" was NO_ELIGIBLE_PAIR,
        # the planter being hitched to the 3650 already.
        def rig(tractor_name="3650"):
            tractor = replace(unit("615", "3650", "tractor", attached="614", position=(-700.0, 215.0)),
                              capabilities=frozenset({"move", "attach", "stop"}))
            planter = replace(unit("614", "SOLID STAND 1500", "implement", attached="615", position=(-708.0, 215.0)),
                              kinds=frozenset({"sowing_machine"}))
            other = unit("611", "GMC Sierra", "tractor", attached="620", position=(-600.0, 100.0))
            deck = replace(unit("620", "1124 Deckover", "implement", attached="611", position=(-608.0, 100.0)),
                           kinds=frozenset({"trailer"}))
            return World((tractor, planter, other, deck))
        for words in ({"tractor_name": "3650"}, {}):
            world = rig()
            intent = {"version": 1, "request_id": f"r{len(words)}", "farm_id": "1",
                      "task": "deliver_tractor_and_forage_wagon", "implement": "sowing_machine",
                      "target": {"type": "field_entrance", "field": 5}, **words}
            result = Executor(world).execute(intent)
            self.assertEqual((result.status, result.reason), ("COMPLETE", "DELIVERY_OBSERVED"), words)
            self.assertEqual(result.equipment_ids, ("615", "614"))
            self.assertEqual(world.calls[1][:2], ("drive", "615"))
            self.assertEqual(world.items["615"].attached_to, "614")          # the rig kept
        world = rig()
        result = Executor(world).execute({"version": 1, "request_id": "x", "farm_id": "1",
                                          "task": "deliver_tractor_and_forage_wagon", "implement": "sowing_machine",
                                          "tractor_name": "gmc", "target": {"type": "field_entrance", "field": 5}})
        self.assertNotEqual(result.reason, "DELIVERY_OBSERVED")             # the GMC tows no planter
        self.assertFalse(any(c[0] == "drive" for c in world.calls))

    def test_the_combine_by_its_kind_word_moves_with_its_header(self):
        # Live ab2795521e884f2d-4: the combine was left on the field it harvested; the player
        # must be able to send it ("take the combine to ...") like any driven vehicle.
        combine = unit("616", "MF 8570", "harvester", attached="598", position=(-700.0, 215.0))
        header = replace(unit("598", "MF 8570 Header", "implement", attached="616", position=(-694.0, 215.0)),
                         kinds=frozenset({"cutter"}))
        for words in ("combine", "mf 8570"):
            world = World((combine, header))
            result = StandaloneMove(world).execute({"request_id": words, "farm_id": "1", "task": "move_pair",
                                                    "tractor_name": words,
                                                    "target": {"type": "field_entrance", "field": 5}})
            self.assertEqual((result.status, result.reason), ("COMPLETE", "DELIVERY_OBSERVED"), words)
            self.assertEqual(result.equipment_ids, ("616", "598"))

if __name__ == "__main__":
    unittest.main()
