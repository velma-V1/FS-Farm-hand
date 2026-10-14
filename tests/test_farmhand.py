import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import math
import random
import unittest

from companion.executor import Executor
from protocol.model import Equipment
from simulator.world import MockWorld


def intent(request_id="request-001"):
    return {"version": 1, "request_id": request_id,
            "task": "deliver_tractor_and_forage_wagon", "farm_id": "farm-a",
            "target": "player_at_acceptance"}


def inventory(seed=0):
    rng = random.Random(seed)
    identifiers = iter(rng.sample(range(100000, 999999), 30))

    def identity():
        return f"asset-{next(identifiers)}"

    tractor = Equipment(identity(), "tractor", "farm-a", (0, 0),
                        hitch="drawbar", power_kw=180,
                        capabilities=frozenset({"move", "attach", "stop", "pto"}))
    wagon = Equipment(identity(), "forage_wagon", "farm-a", (40, 20),
                      hitch="drawbar", required_power_kw=100, requires_pto=True)
    decoys = [replace(tractor, id=identity(), category="truck"),
              replace(tractor, id=identity(), farm_id="other-farm"),
              replace(tractor, id=identity(), busy=True),
              replace(tractor, id=identity(), occupied=True),
              replace(tractor, id=identity(), available=False),
              replace(tractor, id=identity(), reserved_by="other-request"),
              replace(tractor, id=identity(), attached_to="some-trailer"),
              replace(tractor, id=identity(), power_kw=30),
              replace(tractor, id=identity(), hitch="three_point"),
              replace(tractor, id=identity(), capabilities=frozenset({"move", "attach", "stop"})),
              replace(tractor, id=identity(), capabilities=frozenset({"pto"})),
              replace(wagon, id=identity(), category="bale_trailer"),
              replace(wagon, id=identity(), farm_id="other-farm"),
              replace(wagon, id=identity(), busy=True),
              replace(wagon, id=identity(), occupied=True),
              replace(wagon, id=identity(), available=False),
              replace(wagon, id=identity(), reserved_by="other-request"),
              replace(wagon, id=identity(), attached_to="other-tractor"),
              replace(wagon, id=identity(), hitch="incompatible"),
              replace(wagon, id=identity(), required_power_kw=500)]
    items = [tractor, wagon, *decoys]
    rng.shuffle(items)
    return items


def simple_world():
    items = inventory()
    # Pick the baseline pair by its behavior, never by an ID.
    tractor = next(x for x in items if x.category == "tractor" and x.farm_id == "farm-a"
                   and not x.busy and not x.occupied and x.available
                   and x.reserved_by is None and x.attached_to is None
                   and x.hitch == "drawbar" and x.power_kw == 180
                   and x.capabilities == frozenset({"move", "attach", "stop", "pto"}))
    wagon = next(x for x in items if x.category == "forage_wagon" and x.farm_id == "farm-a"
                 and not x.busy and not x.occupied and x.available
                 and x.reserved_by is None and x.attached_to is None
                 and x.hitch == "drawbar" and x.required_power_kw == 100)
    return MockWorld([tractor, wagon], {"farm-a": (100, 200)})


class FaultWorld(MockWorld):
    def __init__(self, fault):
        base = simple_world()
        super().__init__(base.snapshot(), base.players)
        self.fault = fault
        self.moves = 0

    def reserve(self, ids, owner):
        if self.fault == "reserve_conflict":
            return False
        ok = super().reserve(ids, owner)
        if self.fault == "stale":
            self.items[ids[1]] = replace(self.items[ids[1]], busy=True)
        return ok

    def move(self, tractor_id, target, owner):
        self.moves += 1
        stage = "approach" if self.moves == 1 else "delivery"
        if self.fault == stage:
            raise RuntimeError("physical failure")
        if self.fault == stage + "_timeout":
            raise TimeoutError("operation deadline")
        if self.fault == "false_arrival" and self.moves == 2:
            return
        wagon_id = self.items[tractor_id].attached_to
        old_position = self.items[wagon_id].position if wagon_id else None
        super().move(tractor_id, target, owner)
        if self.moves == 2 and self.fault == "lost_attachment":
            self.items[tractor_id] = replace(self.items[tractor_id], attached_to=None)
        if self.moves == 2 and self.fault == "wagon_left":
            self.items[wagon_id] = replace(self.items[wagon_id], position=old_position)
        if self.moves == 2 and self.fault == "moving":
            self.items[tractor_id] = replace(self.items[tractor_id], stopped=False)
        if self.moves == 1 and self.fault == "player_moved":
            self.players["farm-a"] = (900, 900)

    def attach(self, tractor_id, wagon_id, owner):
        if self.fault == "attach":
            raise RuntimeError("coupling failed")
        if self.fault == "attach_timeout":
            raise TimeoutError("coupling deadline")
        if self.fault == "false_attach":
            return
        super().attach(tractor_id, wagon_id, owner)
        if self.fault == "wrong_attachment":
            self.items[wagon_id] = replace(self.items[wagon_id], attached_to="stranger")

    def stop(self, tractor_id, owner):
        if self.fault == "stop":
            return False
        return super().stop(tractor_id, owner)


class AcceptanceTests(unittest.TestCase):
    def test_selection_is_behavioral(self):
        for seed in range(40):
            with self.subTest(seed=seed):
                world = MockWorld(inventory(seed), {"farm-a": (100, 200)})
                before = {x.id: x for x in world.snapshot()}
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "COMPLETE", result)
                self.assertEqual(result.states, ("FIND", "RESERVE", "MOVE", "ATTACH", "VERIFY", "DELIVER"))
                tractor, wagon = (world.items[x] for x in result.equipment_ids)
                self.assertEqual((tractor.category, wagon.category), ("tractor", "forage_wagon"))
                for item in (tractor, wagon):
                    original = before[item.id]
                    self.assertEqual(item.farm_id, "farm-a")
                    self.assertTrue(original.available)
                    self.assertFalse(original.busy or original.occupied)
                    self.assertIsNone(original.reserved_by)
                    self.assertIsNone(original.attached_to)
                    self.assertTrue(item.stopped)
                    self.assertIsNone(item.reserved_by)
                self.assertEqual(tractor.hitch, wagon.hitch)
                self.assertGreaterEqual(tractor.power_kw, wagon.required_power_kw)
                self.assertTrue({"move", "attach", "stop", "pto"} <= tractor.capabilities)
                self.assertEqual(tractor.attached_to, wagon.id)
                self.assertEqual(wagon.attached_to, tractor.id)
                self.assertLessEqual(math.dist(tractor.position, (100, 200)), 3)
                self.assertLessEqual(math.dist(wagon.position, (100, 200)), 15)
                for item in world.snapshot():
                    if item.id not in result.equipment_ids:
                        self.assertEqual(item, before[item.id])

    def test_no_pair(self):
        for mutation in ("missing_wagon", "missing_capability", "incompatible", "no_pto"):
            with self.subTest(mutation=mutation):
                world = simple_world()
                tractor, wagon = world.snapshot()
                if mutation == "missing_wagon":
                    del world.items[wagon.id]
                elif mutation == "missing_capability":
                    world.items[tractor.id] = replace(tractor, capabilities=frozenset())
                elif mutation == "no_pto":
                    world.items[tractor.id] = replace(tractor, capabilities=frozenset({"move", "attach", "stop"}))
                else:
                    world.items[wagon.id] = replace(wagon, hitch="incompatible")
                before = world.snapshot()
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(world.snapshot(), before)

    def test_pair_search(self):
        world = simple_world()
        tractor, wagon = world.snapshot()
        # A first-sorted tractor is unsuitable; the later pair must still be found.
        world.items["000"] = replace(tractor, id="000", power_kw=1)
        world.items["001"] = replace(wagon, id="001", required_power_kw=999)
        self.assertEqual(Executor(world).execute(intent()).status, "COMPLETE")

    def test_atomic_contention(self):
        world = simple_world()
        ids = tuple(x.id for x in world.snapshot())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda owner: world.reserve(ids, owner), ("a", "b")))
        self.assertEqual(sorted(results), [False, True])
        owners = {x.reserved_by for x in world.snapshot()}
        self.assertEqual(len(owners), 1)
        owner = owners.pop()
        world.release("not-the-owner")
        self.assertTrue(all(x.reserved_by == owner for x in world.snapshot()))
        world.release(owner)
        self.assertFalse(world.reserve((ids[0], "missing"), "c"))
        self.assertTrue(all(x.reserved_by is None for x in world.snapshot()))

    def test_reservation_conflict(self):
        world = FaultWorld("reserve_conflict")
        self.assertEqual(Executor(world).execute(intent()).status, "BLOCKED")
        self.assertEqual(world.moves, 0)

    def test_changed_after_reservation(self):
        world = FaultWorld("stale")
        self.assertNotEqual(Executor(world).execute(intent()).status, "COMPLETE")
        self.assertEqual(world.moves, 0)

    def test_physical_failures(self):
        for fault in ("approach", "attach", "delivery", "approach_timeout", "attach_timeout", "delivery_timeout"):
            with self.subTest(fault=fault):
                world = FaultWorld(fault)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "FAILED", result)
                self.assertTrue(all(x.stopped and x.reserved_by is None for x in world.snapshot()))
                if fault.startswith("attach"):
                    self.assertEqual(world.moves, 1)

    def test_false_observations(self):
        for fault in ("false_attach", "wrong_attachment", "false_arrival", "lost_attachment", "wagon_left", "moving"):
            with self.subTest(fault=fault):
                world = FaultWorld(fault)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "UNVERIFIED", result)
                if fault in ("false_attach", "wrong_attachment"):
                    self.assertEqual(world.moves, 1)

    def test_stop_failure_retains_reservation(self):
        world = FaultWorld("stop")
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertTrue(all(x.reserved_by == intent()["request_id"] for x in world.snapshot()))

    def test_here_is_snapshot(self):
        world = FaultWorld("player_moved")
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(world.items[result.equipment_ids[0]].position, (100, 200))

    def test_replay(self):
        world = FaultWorld(None)
        executor = Executor(world)
        first = executor.execute(intent())
        moves = world.moves
        self.assertEqual(executor.execute(intent()), first)
        changed = intent()
        changed["farm_id"] = "other-farm"
        self.assertEqual(executor.execute(changed).status, "BLOCKED")
        self.assertEqual(world.moves, moves)

    def test_schema_rejects_bad_intent(self):
        invalid = [None, [], "go get tractor", {}, {**intent(), "version": True},
                   {**intent(), "version": 2}, {**intent(), "request_id": ""},
                   {**intent(), "request_id": "x" * 65}, {**intent(), "request_id": "bad\n"},
                   {**intent(), "farm_id": None}, {**intent(), "task": "harvest"},
                   {**intent(), "target": [10, 20]}, {**intent(), "vehicle_id": "forced"}]
        for bad in invalid:
            with self.subTest(bad=bad):
                world = simple_world()
                before = copy.deepcopy(world.snapshot())
                self.assertEqual(Executor(world).execute(bad).status, "BLOCKED")
                self.assertEqual(world.snapshot(), before)

    def test_invalid_target(self):
        for position in ((float("nan"), 0), (float("inf"), 0), None):
            world = simple_world()
            world.players["farm-a"] = position
            self.assertNotEqual(Executor(world).execute(intent()).status, "COMPLETE")
            self.assertTrue(all(x.reserved_by is None for x in world.snapshot()))

    def test_mock_rejects_unauthorized_actions(self):
        world = simple_world()
        tractor, wagon = world.snapshot()
        self.assertTrue(world.reserve((tractor.id, wagon.id), "owner"))
        with self.assertRaises(ValueError):
            world.move(tractor.id, (1, 1), "intruder")
        with self.assertRaises(ValueError):
            world.attach(tractor.id, wagon.id, "intruder")
        with self.assertRaises(ValueError):
            world.attach(tractor.id, wagon.id, "owner")  # Too far apart.
        self.assertFalse(world.stop(tractor.id, "intruder"))

    def test_snapshot_is_immutable(self):
        world = simple_world()
        snapshot = world.snapshot()
        with self.assertRaises(AttributeError):
            snapshot[0].busy = True
        with self.assertRaises(ValueError):
            MockWorld([snapshot[0], snapshot[0]], world.players)

    def test_action_time_farm_change_prevents_movement(self):
        class FarmChangeWorld(FaultWorld):
            def move(self, tractor_id, target, owner):
                self.items[tractor_id] = replace(self.items[tractor_id], farm_id="other-farm")
                return super().move(tractor_id, target, owner)

        world = FarmChangeWorld(None)
        positions = {x.id: x.position for x in world.snapshot()}
        result = Executor(world).execute(intent())
        self.assertNotEqual(result.status, "COMPLETE")
        self.assertEqual({x.id: x.position for x in world.snapshot()}, positions)

    def test_active_owner_collision_preserves_unselected_equipment(self):
        world = simple_world()
        tractor = world.snapshot()[0]
        foreign = replace(tractor, id="foreign", farm_id="other-farm", stopped=False,
                          reserved_by=intent()["request_id"])
        world.items[foreign.id] = foreign
        before = world.snapshot()
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(world.snapshot(), before)

    def test_action_time_compatibility_change_prevents_delivery(self):
        for change in ({"power_kw": 1}, {"hitch": "wrong"},
                       {"capabilities": frozenset({"move", "attach", "stop"})}):
            with self.subTest(change=change):
                class CompatibilityChangeWorld(FaultWorld):
                    def move(self, tractor_id, target, owner):
                        if self.moves == 1:
                            self.items[tractor_id] = replace(self.items[tractor_id], **change)
                        return super().move(tractor_id, target, owner)

                world = CompatibilityChangeWorld(None)
                wagon_position = world.snapshot()[1].position
                result = Executor(world).execute(intent())
                self.assertNotEqual(result.status, "COMPLETE")
                self.assertTrue(all(x.position == wagon_position for x in world.snapshot()))

    def test_stop_without_owner_is_rejected(self):
        world = simple_world()
        tractor = world.snapshot()[0]
        world.items[tractor.id] = replace(tractor, stopped=False)
        before = world.snapshot()
        self.assertFalse(world.stop(tractor.id, None))
        self.assertEqual(world.snapshot(), before)
