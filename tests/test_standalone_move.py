"""A point-to-point goal keeps the observed rig intact through arrival."""
from dataclasses import replace
import math
import unittest

from companion.standalone import StandaloneMove
from companion.executor import Executor
from protocol.model import Destination, Equipment


def unit(identifier, name, category, *, attached=None, belted_to=None, belted=(), position=(0.0, 0.0)):
    return Equipment(identifier, category, "1", position, name=name, attached_to=attached,
                     belted_to=belted_to, belted=belted, capabilities=frozenset({"move", "stop"}))


class World:
    def __init__(self, items):
        self.items = {item.id: item for item in items}
        self.calls = []
        self.owner = None

    def snapshot(self):
        return tuple(self.items.values())

    def places(self):
        return (Destination("barn", (-759.0, 129.0), "Farm Barn"),)

    def destinations(self):
        return ()

    def field_places(self):
        return (Destination("field-5", (-629.0, 211.0), "Field 5"),)

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

    def farm_drive(self, vehicle_id, target, owner):
        self.calls.append(("drive", vehicle_id, target))
        origin = self.items[vehicle_id].position
        delta = (target[0] - origin[0], target[1] - origin[1])
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
        self.assertLess(world.calls[1][2][0], -730)   # near the barn, clear of its centre
        self.assertGreaterEqual(math.dist(world.calls[1][2], (-759.0, 129.0)), 19.9)

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


if __name__ == "__main__":
    unittest.main()
