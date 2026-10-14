import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import math
import random
import unittest

from companion.executor import Executor
from companion.hitch import plan_hitch
from protocol.model import Destination, Equipment, InputJoint, OutputJoint, Pose
from simulator.world import MockWorld


def intent(request_id="request-001"):
    return {"version": 1, "request_id": request_id,
            "task": "deliver_tractor_and_forage_wagon", "farm_id": "farm-a",
            "target": "player_at_acceptance"}


PLAYERS = {"farm-a": (100, 200)}
# A routable AutoDrive-style marker at the requester; IDs are opaque.
DESTINATIONS = (Destination("marker-at-requester", (100, 200)),
                Destination("marker-far-away", (900, 900)))


def inventory(seed=0):
    rng = random.Random(seed)
    identifiers = iter(rng.sample(range(100000, 999999), 30))

    def identity():
        return f"asset-{next(identifiers)}"

    tractor = Equipment(identity(), "tractor", "farm-a", (0, 0),
                        hitches=frozenset({"drawbar"}), power_kw=180,
                        capabilities=frozenset({"move", "attach", "stop", "pto"}),
                        heading=0.0, ai_root=Pose(0.0, 0.0, 0.0),
                        output_joints=(OutputJoint(3, "drawbar", 0.0, -2.5, True),))
    # Wagon faces +z with its drawbar eye 4 m ahead of its root.
    wagon = Equipment(identity(), "forage_wagon", "farm-a", (40, 20),
                      hitches=frozenset({"drawbar"}), required_power_kw=100, requires_pto=True,
                      heading=0.0, input_joints=(InputJoint(1, "drawbar", (40.0, 24.0), True),))
    decoys = [replace(tractor, id=identity(), category="truck"),
              replace(tractor, id=identity(), farm_id="other-farm"),
              replace(tractor, id=identity(), busy=True),
              replace(tractor, id=identity(), occupied=True),
              replace(tractor, id=identity(), available=False),
              replace(tractor, id=identity(), reserved_by="other-request"),
              replace(tractor, id=identity(), attached_to="some-trailer"),
              replace(tractor, id=identity(), power_kw=30),
              replace(tractor, id=identity(), hitches=frozenset({"three_point"})),
              replace(tractor, id=identity(), capabilities=frozenset({"move", "attach", "stop"})),
              replace(tractor, id=identity(), capabilities=frozenset({"pto"})),
              replace(wagon, id=identity(), category="bale_trailer"),
              replace(wagon, id=identity(), farm_id="other-farm"),
              replace(wagon, id=identity(), busy=True),
              replace(wagon, id=identity(), occupied=True),
              replace(wagon, id=identity(), available=False),
              replace(wagon, id=identity(), reserved_by="other-request"),
              replace(wagon, id=identity(), attached_to="other-tractor"),
              replace(wagon, id=identity(), hitches=frozenset({"incompatible"})),
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
                   and x.hitches == frozenset({"drawbar"}) and x.power_kw == 180
                   and x.capabilities == frozenset({"move", "attach", "stop", "pto"}))
    wagon = next(x for x in items if x.category == "forage_wagon" and x.farm_id == "farm-a"
                 and not x.busy and not x.occupied and x.available
                 and x.reserved_by is None and x.attached_to is None
                 and x.hitches == frozenset({"drawbar"}) and x.required_power_kw == 100)
    return MockWorld([tractor, wagon], PLAYERS, DESTINATIONS)


class FaultWorld(MockWorld):
    def __init__(self, fault):
        base = simple_world()
        super().__init__(base.snapshot(), base.players, base.destinations())
        self.fault = fault
        self.moves = 0
        self.attach_calls = []

    def reserve(self, ids, owner):
        if self.fault == "reserve_conflict":
            return False
        ok = super().reserve(ids, owner)
        if self.fault == "stale":
            self.items[ids[1]] = replace(self.items[ids[1]], busy=True)
        return ok

    def _fault(self, stage):
        self.moves += 1
        if self.fault == stage:
            raise RuntimeError("physical failure")
        if self.fault == stage + "_timeout":
            raise TimeoutError("operation deadline")

    def travel(self, tractor_id, destination_id, owner):
        self._fault("travel")
        super().travel(tractor_id, destination_id, owner)

    def stage(self, tractor_id, wagon_id, pose, owner):
        self._fault("stage")
        if self.fault == "stage_elsewhere":
            pose = Pose(pose.x + 5, pose.z, pose.yaw)
        super().stage(tractor_id, wagon_id, pose, owner)
        if self.fault == "player_moved":
            self.players["farm-a"] = (900, 900)
        if self.fault == "wagon_moved_after_stage":
            wagon = self.items[wagon_id]
            moved = tuple(replace(j, position=(j.position[0] + 1.0, j.position[1])) for j in wagon.input_joints)
            self.items[wagon_id] = replace(wagon, input_joints=moved)
        if self.fault == "reservation_lost_after_stage":
            self.items[wagon_id] = replace(self.items[wagon_id], reserved_by="thief")

    def align(self, tractor_id, wagon_id, plan, owner):
        self._fault("align")
        if self.fault == "align_no_progress":
            return
        super().align(tractor_id, wagon_id, plan, owner)

    def deliver(self, tractor_id, destination_id, owner):
        self._fault("delivery")
        if self.fault == "false_arrival":
            return
        wagon_id = self.items[tractor_id].attached_to
        old_position = self.items[wagon_id].position if wagon_id else None
        super().deliver(tractor_id, destination_id, owner)
        if self.fault == "lost_attachment":
            self.items[tractor_id] = replace(self.items[tractor_id], attached_to=None)
        if self.fault == "wagon_left":
            self.items[wagon_id] = replace(self.items[wagon_id], position=old_position)
        if self.fault == "moving":
            self.items[tractor_id] = replace(self.items[tractor_id], stopped=False)

    def attach(self, tractor_id, wagon_id, joints, owner):
        self.attach_calls.append(joints)
        if self.fault == "attach":
            raise RuntimeError("coupling failed")
        if self.fault == "attach_timeout":
            raise TimeoutError("coupling deadline")
        if self.fault == "false_attach":
            return
        super().attach(tractor_id, wagon_id, joints, owner)
        if self.fault == "wrong_attachment":
            self.items[wagon_id] = replace(self.items[wagon_id], attached_to="stranger")
        if self.fault == "still_moving_after_attach":
            self.items[tractor_id] = replace(self.items[tractor_id], stopped=False)

    def stop(self, tractor_id, owner):
        if self.fault == "stop":
            return False
        return super().stop(tractor_id, owner)


class AcceptanceTests(unittest.TestCase):
    def test_selection_is_behavioral(self):
        for seed in range(40):
            with self.subTest(seed=seed):
                world = MockWorld(inventory(seed), PLAYERS, DESTINATIONS)
                before = {x.id: x for x in world.snapshot()}
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "COMPLETE", result)
                self.assertEqual(result.states, ("FIND", "RESERVE", "STAGE", "ALIGN_REVERSE",
                                                 "ATTACH", "VERIFY", "DELIVER"))
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
                self.assertTrue(tractor.hitches & wagon.hitches)
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
                    world.items[wagon.id] = replace(wagon, hitches=frozenset({"incompatible"}))
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
        for fault in ("stage", "align", "attach", "delivery", "stage_timeout", "align_timeout",
                      "attach_timeout", "delivery_timeout"):
            with self.subTest(fault=fault):
                world = FaultWorld(fault)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "FAILED", result)
                self.assertTrue(all(x.stopped and x.reserved_by is None for x in world.snapshot()))
                # No physical step runs after the failing one.
                expected_moves = {"stage": 1, "align": 2, "attach": 2, "delivery": 3}[fault.split("_")[0]]
                self.assertEqual(world.moves, expected_moves)
                self.assertEqual(len(world.attach_calls), 0 if fault.startswith(("stage", "align")) else 1)

    def test_false_observations(self):
        for fault in ("false_attach", "wrong_attachment", "false_arrival", "lost_attachment", "wagon_left", "moving"):
            with self.subTest(fault=fault):
                world = FaultWorld(fault)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "UNVERIFIED", result)
                if fault in ("false_attach", "wrong_attachment"):
                    self.assertEqual(world.moves, 2)  # stage + align; never delivery

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
        plan = plan_hitch(tractor, wagon)
        with self.assertRaises(ValueError):
            world.travel(tractor.id, DESTINATIONS[0].id, "intruder")
        with self.assertRaises(ValueError):
            world.stage(tractor.id, wagon.id, plan.staging, "intruder")
        with self.assertRaises(ValueError):
            world.align(tractor.id, wagon.id, plan, "intruder")
        with self.assertRaises(ValueError):
            world.deliver(tractor.id, DESTINATIONS[0].id, "intruder")
        with self.assertRaises(ValueError):
            world.attach(tractor.id, wagon.id, (1, 1), "intruder")
        with self.assertRaises(ValueError):
            world.stage(wagon.id, tractor.id, plan.staging, "owner")  # Not the reserved order.
        with self.assertRaises(RuntimeError):
            world.attach(tractor.id, wagon.id, (1, 1), "owner")  # Joints too far apart.
        with self.assertRaises(RuntimeError):
            world.align(tractor.id, wagon.id, plan, "owner")      # Not staged yet.
        self.assertFalse(world.stop(tractor.id, "intruder"))

    def test_snapshot_is_immutable(self):
        world = simple_world()
        snapshot = world.snapshot()
        with self.assertRaises(AttributeError):
            snapshot[0].busy = True
        with self.assertRaises(ValueError):
            MockWorld([snapshot[0], snapshot[0]], world.players)
        with self.assertRaises(ValueError):
            MockWorld(snapshot, world.players, DESTINATIONS * 2)

    def test_action_time_farm_change_prevents_movement(self):
        class FarmChangeWorld(FaultWorld):
            def stage(self, tractor_id, wagon_id, pose, owner):
                self.items[tractor_id] = replace(self.items[tractor_id], farm_id="other-farm")
                return super().stage(tractor_id, wagon_id, pose, owner)

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
        for change in ({"power_kw": 1}, {"hitches": frozenset({"wrong"})},
                       {"capabilities": frozenset({"move", "attach", "stop"})}):
            with self.subTest(change=change):
                class CompatibilityChangeWorld(FaultWorld):
                    def deliver(self, tractor_id, destination_id, owner):
                        self.items[tractor_id] = replace(self.items[tractor_id], **change)
                        self.before_delivery = {x.id: x.position for x in self.snapshot()}
                        return super().deliver(tractor_id, destination_id, owner)

                world = CompatibilityChangeWorld(None)
                result = Executor(world).execute(intent())
                self.assertNotEqual(result.status, "COMPLETE")
                self.assertEqual({x.id: x.position for x in world.snapshot()}, world.before_delivery)

    def test_stop_without_owner_is_rejected(self):
        world = simple_world()
        tractor = world.snapshot()[0]
        world.items[tractor.id] = replace(tractor, stopped=False)
        before = world.snapshot()
        self.assertFalse(world.stop(tractor.id, None))
        self.assertEqual(world.snapshot(), before)

    def test_destination_is_nearest_routable_marker(self):
        # AutoDrive only routes to map markers: "here" resolves to the nearest
        # valid marker within range, and arrival is verified at that marker.
        for seed in range(10):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                markers = [Destination(f"m-{rng.randrange(10**6)}-near", (104, 203)),
                           Destination(f"m-{rng.randrange(10**6)}-mid", (120, 200)),
                           Destination(f"m-{rng.randrange(10**6)}-nan", (float("nan"), 200)),
                           Destination(f"m-{rng.randrange(10**6)}-far", (100, 400))]
                rng.shuffle(markers)
                base = simple_world()
                world = MockWorld(base.snapshot(), PLAYERS, markers)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "COMPLETE", result)
                tractor, wagon = (world.items[x] for x in result.equipment_ids)
                self.assertEqual(tractor.position, (104, 203))
                self.assertEqual(wagon.position, (104, 203))

    def test_destination_tie_break_is_order_independent(self):
        markers = [Destination("b", (103, 200)), Destination("a", (97, 200))]
        for ordering in (markers, markers[::-1]):
            base = simple_world()
            world = MockWorld(base.snapshot(), PLAYERS, ordering)
            result = Executor(world).execute(intent())
            self.assertEqual(result.status, "COMPLETE")
            self.assertEqual(world.items[result.equipment_ids[0]].position, (97, 200))

    def test_no_destination_near_requester(self):
        for markers in ((), (Destination("far", (100, 400)),),
                        (Destination("bad", (float("inf"), 200)),),
                        (Destination("", (100, 200)),)):
            with self.subTest(markers=markers):
                base = simple_world()
                world = MockWorld(base.snapshot(), PLAYERS, markers)
                before = world.snapshot()
                result = Executor(world).execute(intent())
                self.assertEqual((result.status, result.reason),
                                 ("BLOCKED", "NO_DESTINATION_NEAR_REQUESTER"))
                self.assertEqual(world.snapshot(), before)

    def test_unknown_destination_fails_closed(self):
        class VanishingMarkerWorld(FaultWorld):
            def deliver(self, tractor_id, destination_id, owner):
                self.destination_map.clear()
                return super().deliver(tractor_id, destination_id, owner)

        world = VanishingMarkerWorld(None)
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "FAILED", result)
        self.assertTrue(all(x.reserved_by is None and x.stopped for x in world.snapshot()))

    def test_multi_joint_tractor_matches_any_shared_joint(self):
        base = simple_world()
        tractor, wagon = base.snapshot()
        for tractor_hitches, wagon_hitches, expected in (
                ({"three_point", "trailer"}, {"trailer"}, "COMPLETE"),
                ({"three_point", "trailer"}, {"trailerlow", "trailer"}, "COMPLETE"),
                ({"three_point", "trailerlow"}, {"trailer"}, "BLOCKED"),
                (set(), set(), "BLOCKED")):
            with self.subTest(tractor=tractor_hitches, wagon=wagon_hitches):
                world = MockWorld([replace(tractor, hitches=frozenset(tractor_hitches)),
                                   replace(wagon, hitches=frozenset(wagon_hitches))],
                                  PLAYERS, DESTINATIONS)
                self.assertEqual(Executor(world).execute(intent()).status, expected)


# ------------------------------------------------------------ explicit destinations
MARKERS = (Destination("1", (100, 200), "Home Yard"),
           Destination("7", (640, -120), "Field 12 gate"),
           Destination("9", (900, 900), "Silo"),
           Destination("10", (300, 300), "Dup"),
           Destination("11", (310, 310), "dup"),
           Destination("12", (float("nan"), 5), "Broken"))
FIELD_ENTRANCES = {"MapUS": {12: "Field 12 gate", 3: "Dup", 4: "Nowhere", 5: "Broken"}}


def explicit_world(markers=MARKERS, map_id="MapUS"):
    base = simple_world()
    return MockWorld(base.snapshot(), PLAYERS, markers, map_id=map_id)


def target_intent(target, request_id="request-001"):
    return {**intent(request_id), "target": target}


class ExplicitDestinationTests(unittest.TestCase):
    def run_target(self, target, world=None, field_entrances=FIELD_ENTRANCES):
        world = world or explicit_world()
        before = world.snapshot()
        result = Executor(world, field_entrances=field_entrances).execute(target_intent(target))
        return world, before, result

    def assert_delivered_to(self, world, result, position):
        self.assertEqual(result.status, "COMPLETE", result)
        tractor, wagon = (world.items[x] for x in result.equipment_ids)
        self.assertEqual((tractor.position, wagon.position), (position, position))
        self.assertEqual((tractor.attached_to, wagon.attached_to), (wagon.id, tractor.id))

    def test_explicit_marker_by_id_and_by_name(self):
        for target in ({"type": "autodrive_marker", "marker_id": "9"},
                       {"type": "autodrive_marker", "marker_name": "Silo"},
                       {"type": "autodrive_marker", "marker_name": "  silo "}):
            with self.subTest(target=target):
                world, _, result = self.run_target(target)
                # Far from the requester: explicit selection is not limited to 50 m.
                self.assert_delivered_to(world, result, (900, 900))

    def test_field_resolves_through_explicit_map_entry(self):
        world, _, result = self.run_target({"type": "field_entrance", "field": 12})
        self.assert_delivered_to(world, result, (640, -120))

    def test_unresolvable_targets_block_before_reservation(self):
        cases = [
            ({"type": "autodrive_marker", "marker_id": "404"}, None, "DESTINATION_NOT_FOUND"),
            ({"type": "autodrive_marker", "marker_name": "Sil"}, None, "DESTINATION_NOT_FOUND"),
            ({"type": "autodrive_marker", "marker_name": "Home Yard 2"}, None, "DESTINATION_NOT_FOUND"),
            ({"type": "autodrive_marker", "marker_name": "DUP"}, None, "DESTINATION_AMBIGUOUS"),
            ({"type": "autodrive_marker", "marker_name": "Broken"}, None, "DESTINATION_INVALID"),
            ({"type": "autodrive_marker", "marker_id": "12"}, None, "DESTINATION_INVALID"),
            ({"type": "field_entrance", "field": 99}, None, "FIELD_NOT_MAPPED"),
            ({"type": "field_entrance", "field": 3}, None, "DESTINATION_AMBIGUOUS"),
            ({"type": "field_entrance", "field": 4}, None, "DESTINATION_NOT_FOUND"),
            ({"type": "field_entrance", "field": 5}, None, "DESTINATION_INVALID"),
            # Field numbers are per map: another or unknown map never resolves.
            ({"type": "field_entrance", "field": 12}, explicit_world(map_id="OtherMap"), "FIELD_NOT_MAPPED"),
            ({"type": "field_entrance", "field": 12}, explicit_world(map_id=None), "MAP_UNKNOWN"),
            ({"type": "field_entrance", "field": 12}, explicit_world(map_id=""), "MAP_UNKNOWN"),
            ({"type": "field_entrance", "field": 12}, explicit_world(map_id=["MapUS"]), "MAP_UNKNOWN"),
        ]
        for target, world, reason in cases:
            with self.subTest(target=target, reason=reason):
                world, before, result = self.run_target(target, world)
                self.assertEqual((result.status, result.reason), ("BLOCKED", reason))
                self.assertEqual(result.equipment_ids, ())
                self.assertNotIn("RESERVE", result.states)
                self.assertEqual(world.snapshot(), before)
        # A field target without any configured mapping is unresolved, not guessed.
        world, before, result = self.run_target({"type": "field_entrance", "field": 12},
                                                field_entrances=None)
        self.assertEqual((result.status, result.reason), ("BLOCKED", "FIELD_NOT_MAPPED"))
        self.assertEqual(world.snapshot(), before)

    def test_no_nearby_substitution_for_explicit_targets(self):
        # A marker at the requester exists, but a missing explicit marker must not
        # fall back to it, nor to the nearest marker of a similar name.
        world, before, result = self.run_target(
            {"type": "autodrive_marker", "marker_name": "Home"})
        self.assertEqual((result.status, result.reason), ("BLOCKED", "DESTINATION_NOT_FOUND"))
        self.assertEqual(world.snapshot(), before)

    def test_explicit_target_does_not_need_requester_position(self):
        world = explicit_world()
        world.players["farm-a"] = None
        result = Executor(world).execute(target_intent({"type": "autodrive_marker", "marker_id": "9"}))
        self.assert_delivered_to(world, result, (900, 900))

    def test_arrival_is_verified_at_the_resolved_destination(self):
        class WrongMarkerWorld(FaultWorld):
            def deliver(self, tractor_id, destination_id, owner):
                # Drives somewhere else (the requester's marker) and reports success.
                return super().deliver(tractor_id, "1", owner)

        world = WrongMarkerWorld(None)
        world.destination_map.update({d.id: d for d in MARKERS})
        world.current_map_id = "MapUS"
        result = Executor(world, field_entrances=FIELD_ENTRANCES).execute(
            target_intent({"type": "field_entrance", "field": 12}))
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "DELIVERY_NOT_OBSERVED"))

    def test_field_travel_performs_only_delivery_steps(self):
        calls = []

        class RecordingWorld(MockWorld):
            def __getattribute__(self, name):
                if name in ("reserve", "travel", "stage", "align", "attach", "deliver", "stop", "release"):
                    calls.append(name)
                return super().__getattribute__(name)

        base = explicit_world()
        world = RecordingWorld(base.snapshot(), PLAYERS, MARKERS, map_id="MapUS")
        result = Executor(world, field_entrances=FIELD_ENTRANCES).execute(
            target_intent({"type": "field_entrance", "field": 12}))
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(calls, ["reserve", "stage", "align", "attach", "deliver", "stop", "release"])

    def test_schema_rejects_work_coordinates_and_mixed_selectors(self):
        for target in ({"type": "field_entrance", "field": 12, "operation": "harvest"},
                       {"type": "field_work", "field": 12},
                       {"type": "point", "x": 10, "z": 20},
                       {"type": "autodrive_marker", "marker_id": "9", "marker_name": "Silo"},
                       {"type": "autodrive_marker"},
                       {"type": "autodrive_marker", "marker_name": ""},
                       {"type": "autodrive_marker", "marker_name": "Silo\n"},
                       {"type": "autodrive_marker", "marker_id": "../9"},
                       {"type": "field_entrance", "field": 0},
                       {"type": "field_entrance", "field": True},
                       {"type": "field_entrance", "field": 12.5},
                       {"type": "field_entrance", "field": "12"}):
            with self.subTest(target=target):
                world, before, result = self.run_target(target)
                self.assertEqual((result.status, result.reason), ("BLOCKED", "INVALID_INTENT"))
                self.assertEqual(world.snapshot(), before)
        work = {**target_intent({"type": "field_entrance", "field": 12}), "task": "work_field"}
        self.assertEqual(Executor(explicit_world()).execute(work).reason, "INVALID_INTENT")

    def test_replay_is_detached_from_caller_target(self):
        world = explicit_world()
        executor = Executor(world)
        command = target_intent({"type": "autodrive_marker", "marker_id": "9"})
        first = executor.execute(command)
        self.assertEqual(first.status, "COMPLETE")
        command["target"]["marker_id"] = "1"
        self.assertEqual(executor.execute(command).reason, "REQUEST_ID_CONFLICT")
        self.assertEqual(executor.execute(target_intent({"type": "autodrive_marker", "marker_id": "9"})),
                         first)

    def test_field_entrance_config_is_validated(self):
        for config in ({"": {12: "Gate"}}, {"MapUS": {0: "Gate"}}, {"MapUS": {True: "Gate"}},
                       {"MapUS": {"12": "Gate"}}, {"MapUS": {12: ""}}, {"MapUS": {12: 7}},
                       {"MapUS": [12]}):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    Executor(explicit_world(), field_entrances=config)


# ------------------------------------------------------------------------- HITCH
class HitchCompositionTests(unittest.TestCase):
    def run_fault(self, fault):
        world = FaultWorld(fault)
        result = Executor(world).execute(intent())
        return world, result

    def test_steps_follow_the_plan_exactly(self):
        calls = []

        class Recording(FaultWorld):
            def stage(self, tractor_id, wagon_id, pose, owner):
                calls.append(("stage", tractor_id, wagon_id, pose))
                return super().stage(tractor_id, wagon_id, pose, owner)

            def align(self, tractor_id, wagon_id, plan, owner):
                calls.append(("align", tractor_id, wagon_id, plan))
                return super().align(tractor_id, wagon_id, plan, owner)

        world = Recording(None)
        tractor, wagon = world.snapshot()
        plan = plan_hitch(tractor, wagon)
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(calls, [("stage", tractor.id, wagon.id, plan.staging),
                                 ("align", tractor.id, wagon.id, plan)])
        self.assertEqual(world.attach_calls, [(plan.tractor_joint, plan.wagon_joint)])

    def test_far_tractor_travels_by_autodrive_first(self):
        base = simple_world()
        tractor, wagon = base.snapshot()
        far = replace(tractor, position=(600.0, 600.0), ai_root=Pose(600.0, 600.0, 0.0))
        staging = plan_hitch(tractor, wagon).staging
        near_wagon = Destination("marker-near-wagon", (staging.x + 10, staging.z))
        world = MockWorld([far, wagon], PLAYERS, DESTINATIONS + (near_wagon,))
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(result.states, ("FIND", "RESERVE", "TRAVEL_NEAR", "STAGE", "ALIGN_REVERSE",
                                         "ATTACH", "VERIFY", "DELIVER"))

        class ShortTravel(MockWorld):
            def travel(self, tractor_id, destination_id, owner):
                super().travel(tractor_id, destination_id, owner)
                tractor = self.items[tractor_id]
                self.items[tractor_id] = replace(tractor, position=(0.0, 0.0), ai_root=Pose(0.0, 0.0, 0.0))

            def stage(self, *args):
                raise AssertionError("staged after unobserved travel")

        world = ShortTravel([far, wagon], PLAYERS, DESTINATIONS + (near_wagon,))
        result = Executor(world).execute(intent())
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "TRAVEL_NOT_OBSERVED"))

    def test_unreachable_or_unplannable_pairs_block_before_reservation(self):
        base = simple_world()
        tractor, wagon = base.snapshot()
        far = replace(tractor, position=(600.0, 600.0), ai_root=Pose(600.0, 600.0, 0.0))
        cases = [
            ([far, wagon], "NO_DESTINATION_NEAR_WAGON"),        # far, and no marker near the wagon
            ([replace(tractor, ai_root=None), wagon], "NO_HITCH_GEOMETRY"),
            ([tractor, replace(wagon, heading=None)], "NO_HITCH_GEOMETRY"),
            ([replace(tractor, output_joints=(OutputJoint(1, "drawbar", 0.0, 3.0, True),)), wagon],
             "NO_HITCH_GEOMETRY"),                               # only a front joint
            ([tractor, replace(wagon, input_joints=(InputJoint(1, "drawbar", (40.0, 24.0), False),))],
             "NO_HITCH_GEOMETRY"),                               # inactive input joint
        ]
        for items, reason in cases:
            with self.subTest(reason=reason):
                world = MockWorld(items, PLAYERS, DESTINATIONS)
                before = world.snapshot()
                result = Executor(world).execute(intent())
                self.assertEqual((result.status, result.reason), ("BLOCKED", reason))
                self.assertNotIn("RESERVE", result.states)
                self.assertEqual(world.snapshot(), before)

    def test_observed_step_failures_stop_without_further_action(self):
        cases = {
            # fault: (reason, physical steps run, attach attempted)
            "stage_elsewhere": ("STAGE_NOT_OBSERVED", 1, False),
            "wagon_moved_after_stage": ("WAGON_MOVED", 1, False),
            "reservation_lost_after_stage": ("CLEANUP_NOT_CONFIRMED", 1, False),
            "align_no_progress": ("ALIGN_NOT_OBSERVED", 2, False),
            "false_attach": ("ATTACHMENT_MISMATCH", 2, True),
            "still_moving_after_attach": ("EQUIPMENT_MOVING", 2, True),
        }
        for fault, (reason, moves, attached) in cases.items():
            with self.subTest(fault=fault):
                world, result = self.run_fault(fault)
                self.assertEqual((result.status, result.reason), ("UNVERIFIED", reason))
                self.assertEqual(world.moves, moves)
                self.assertEqual(len(world.attach_calls), 1 if attached else 0)
                self.assertNotIn("DELIVER", result.states)

    def test_lost_reservation_is_never_released_or_reused(self):
        world, result = self.run_fault("reservation_lost_after_stage")
        tractor, wagon = world.snapshot()
        # The executor does not sweep or release equipment it no longer owns.
        self.assertEqual(wagon.reserved_by, "thief")
        self.assertEqual(tractor.reserved_by, intent()["request_id"])

    def test_wagon_rotation_after_staging_is_detected(self):
        class Rotate(FaultWorld):
            def stage(self, tractor_id, wagon_id, pose, owner):
                super().stage(tractor_id, wagon_id, pose, owner)
                self.items[wagon_id] = replace(self.items[wagon_id], heading=0.3)

        world = Rotate(None)
        result = Executor(world).execute(intent())
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "WAGON_MOVED"))
        self.assertEqual(world.attach_calls, [])

    def test_joint_occupied_after_reservation_is_detected(self):
        class Occupy(FaultWorld):
            def stage(self, tractor_id, wagon_id, pose, owner):
                super().stage(tractor_id, wagon_id, pose, owner)
                tractor = self.items[tractor_id]
                joints = tuple(replace(j, free=False) for j in tractor.output_joints)
                self.items[tractor_id] = replace(tractor, output_joints=joints)

        world = Occupy(None)
        result = Executor(world).execute(intent())
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "HITCH_GEOMETRY_CHANGED"))
        self.assertEqual(world.attach_calls, [])
