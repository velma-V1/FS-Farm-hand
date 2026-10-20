import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import math
import random
import unittest

from adapters.world import StepFailed
from companion.executor import DELIVERY_STANDOFF_M, MAX_APPROACH_DRIVES, PLACE_STANDOFF_M, Executor
from companion.hitch import PULL_CLEAR_M, against_loose_implement, approach_poses, joint_separation, plan_hitch
from protocol.model import Destination, Equipment, InputJoint, OutputJoint, Pose
from simulator.world import MockWorld


def intent(request_id="request-001"):
    return {"version": 1, "request_id": request_id,
            "task": "deliver_tractor_and_forage_wagon", "farm_id": "farm-a",
            "target": "player_at_acceptance"}


PLAYERS = {"farm-a": (100, 200)}
GOTO_TRACTOR = frozenset({"move", "attach", "stop"})
AUTODRIVE_TRACTOR = GOTO_TRACTOR | {"autodrive"}
# A routable AutoDrive-style marker at the requester; IDs are opaque.
DESTINATIONS = (Destination("marker-at-requester", (100, 200)),
                Destination("marker-far-away", (900, 900)))


def inventory(seed=0):
    rng = random.Random(seed)
    identifiers = iter(rng.sample(range(100000, 999999), 30))

    def identity():
        return f"asset-{next(identifiers)}"

    # Torques are in GIANTS' own unit; only the comparison matters.
    tractor = Equipment(identity(), "tractor", "farm-a", (0, 0), pto_torque_capacity=1.8,
                        capabilities=AUTODRIVE_TRACTOR,
                        heading=0.0, ai_root=Pose(0.0, 0.0, 0.0),
                        output_joints=(OutputJoint(3, "drawbar", 0.0, -2.5, True, frozenset({"rearPto"})),))
    # Wagon faces +z with its drawbar eye 4 m ahead of its root; it needs PTO power.
    wagon = Equipment(identity(), "forage_wagon", "farm-a", (40, 20), required_pto_torque=1.0,
                      heading=0.0, input_joints=(InputJoint(1, "drawbar", (40.0, 24.0), True,
                                                            frozenset({"rearPto"})),))
    no_pto = (OutputJoint(3, "drawbar", 0.0, -2.5, True, frozenset()),)
    three_point = (OutputJoint(3, "three_point", 0.0, -2.5, True, frozenset({"rearPto"})),)
    incompatible = (InputJoint(1, "incompatible", (40.0, 24.0), True, frozenset({"rearPto"})),)
    decoys = [replace(tractor, id=identity(), category="truck"),
              replace(tractor, id=identity(), farm_id="other-farm"),
              replace(tractor, id=identity(), busy=True),
              replace(tractor, id=identity(), occupied=True),
              replace(tractor, id=identity(), available=False),
              replace(tractor, id=identity(), reserved_by="other-request"),
              replace(tractor, id=identity(), attached_to="some-trailer"),
              replace(tractor, id=identity(), pto_torque_capacity=0.3),
              replace(tractor, id=identity(), output_joints=three_point),
              replace(tractor, id=identity(), output_joints=no_pto),
              replace(tractor, id=identity(), pto_torque_capacity=None),
              replace(tractor, id=identity(), capabilities=frozenset({"move", "attach"})),
              replace(wagon, id=identity(), category="bale_trailer"),
              replace(wagon, id=identity(), farm_id="other-farm"),
              replace(wagon, id=identity(), busy=True),
              replace(wagon, id=identity(), occupied=True),
              replace(wagon, id=identity(), available=False),
              replace(wagon, id=identity(), reserved_by="other-request"),
              replace(wagon, id=identity(), attached_to="other-tractor"),
              replace(wagon, id=identity(), input_joints=incompatible),
              replace(wagon, id=identity(), required_pto_torque=5.0),
              replace(wagon, id=identity(), required_pto_torque=None)]
    items = [tractor, wagon, *decoys]
    rng.shuffle(items)
    return items


def simple_world():
    items = inventory()
    # Pick the baseline pair by its behavior, never by an ID.
    tractor = next(x for x in items if x.category == "tractor" and x.farm_id == "farm-a"
                   and not x.busy and not x.occupied and x.available
                   and x.reserved_by is None and x.attached_to is None
                   and x.pto_torque_capacity == 1.8
                   and x.capabilities == AUTODRIVE_TRACTOR
                   and x.output_joints[0].joint_type == "drawbar" and x.output_joints[0].pto_names)
    wagon = next(x for x in items if x.category == "forage_wagon" and x.farm_id == "farm-a"
                 and not x.busy and not x.occupied and x.available
                 and x.reserved_by is None and x.attached_to is None
                 and x.input_joints[0].joint_type == "drawbar" and x.required_pto_torque == 1.0)
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

    def navigate(self, tractor_id, pose, owner):
        delivering = self.items[tractor_id].attached_to is not None
        self._fault("delivery" if delivering else "travel")
        if self.fault == "false_arrival" and delivering:
            return
        super().navigate(tractor_id, pose, owner)
        if self.fault == "moving" and delivering:
            self.items[tractor_id] = replace(self.items[tractor_id], stopped=False)

    def stage(self, tractor_id, wagon_id, pose, owner):
        self._fault("stage")
        if self.fault == "stage_elsewhere" or str(self.fault).startswith("maneuver"):
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

    def maneuver(self, tractor_id, wagon_id, plan, owner):
        self._fault("maneuver")
        super().maneuver(tractor_id, wagon_id, plan, owner)
        if self.fault == "maneuver_elsewhere":          # "lined up", but observed 3 m to the side
            pose = self.items[tractor_id].ai_root
            self._place(self.items[tractor_id], Pose(pose.x + 3, pose.z, pose.yaw))

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
                self.assertEqual(result.states, ("FIND", "RESERVE", "STAGE", "LOCAL_MANEUVER", "ALIGN_REVERSE",
                                                 "ATTACH", "VERIFY", "DELIVER", "STOP", "RELEASE"))
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
                self.assertEqual(tractor.output_joints[0].joint_type, wagon.input_joints[0].joint_type)
                self.assertGreaterEqual(tractor.pto_torque_capacity, wagon.required_pto_torque)
                self.assertTrue({"move", "attach", "stop"} <= tractor.capabilities)
                # The planned joints share a PTO, as the wagon needs PTO power.
                self.assertTrue(tractor.output_joints[0].pto_names & wagon.input_joints[0].pto_names)
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
                    joints = tuple(replace(j, pto_names=frozenset()) for j in tractor.output_joints)
                    world.items[tractor.id] = replace(tractor, output_joints=joints)
                else:
                    joints = tuple(replace(j, joint_type="incompatible") for j in wagon.input_joints)
                    world.items[wagon.id] = replace(wagon, input_joints=joints)
                before = world.snapshot()
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(world.snapshot(), before)

    def test_pair_search(self):
        world = simple_world()
        tractor, wagon = world.snapshot()
        # A first-sorted tractor is unsuitable; the later pair must still be found.
        world.items["000"] = replace(tractor, id="000", pto_torque_capacity=0.1)
        world.items["001"] = replace(wagon, id="001", required_pto_torque=99.0)
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
                expected_moves = {"stage": 1, "align": 3, "attach": 3, "delivery": 4}[fault.split("_")[0]]
                self.assertEqual(world.moves, expected_moves)
                self.assertEqual(len(world.attach_calls), 0 if fault.startswith(("stage", "align")) else 1)

    def test_false_observations(self):
        for fault in ("false_attach", "wrong_attachment", "false_arrival", "lost_attachment", "wagon_left", "moving"):
            with self.subTest(fault=fault):
                world = FaultWorld(fault)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "UNVERIFIED", result)
                if fault in ("false_attach", "wrong_attachment"):
                    self.assertEqual(world.moves, 3)  # stage, maneuver, align; never delivery

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
        for change in ({"pto_torque_capacity": 0.1}, {"category": "truck"},
                       {"capabilities": frozenset({"move", "attach"})}, {"pto_torque_capacity": None}):
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

    def test_no_marker_near_requester_delivers_by_giants_goto(self):
        # No usable AutoDrive marker near "here": GIANTS GOTO delivers instead.
        for markers in ((), (Destination("far", (100, 400)),),
                        (Destination("bad", (float("inf"), 200)),),
                        (Destination("", (100, 200)),)):
            with self.subTest(markers=markers):
                base = simple_world()
                world = MockWorld(base.snapshot(), PLAYERS, markers)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "COMPLETE", result)
                tractor, wagon = (world.items[x] for x in result.equipment_ids)
                assert_goto_delivery(self, tractor, wagon, PLAYERS["farm-a"])

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
        # Joint types are compared per joint pair; any rear tractor joint may match.
        base = simple_world()
        tractor, wagon = base.snapshot()

        def outputs(*types):
            return tuple(OutputJoint(i + 1, t, 0.0, -2.5, True, frozenset({"rearPto"}))
                         for i, t in enumerate(types))

        def inputs(*types):
            return tuple(InputJoint(i + 1, t, (40.0, 24.0 + i), True, frozenset({"rearPto"}))
                         for i, t in enumerate(types))

        for tractor_types, wagon_types, expected in (
                (("three_point", "trailer"), ("trailer",), "COMPLETE"),
                (("three_point", "trailer"), ("trailerlow", "trailer"), "COMPLETE"),
                (("three_point", "trailerlow"), ("trailer",), "BLOCKED"),
                ((), (), "BLOCKED")):
            with self.subTest(tractor=tractor_types, wagon=wagon_types):
                world = MockWorld([replace(tractor, output_joints=outputs(*tractor_types)),
                                   replace(wagon, input_joints=inputs(*wagon_types))],
                                  PLAYERS, DESTINATIONS)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, expected, result)
                if expected == "COMPLETE":
                    used = world.items[result.equipment_ids[0]]
                    joint = next(j for j in used.output_joints if not j.free)
                    self.assertEqual(joint.joint_type, "trailer")


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


def assert_goto_delivery(test, tractor, wagon, requester):
    """Delivered by GIANTS GOTO: the tractor's AI root stopped DELIVERY_STANDOFF_M
    short of the requester, facing them; the wagon attached behind it."""
    root = tractor.ai_root
    test.assertAlmostEqual(math.dist((root.x, root.z), requester), DELIVERY_STANDOFF_M, places=6)
    facing = (math.sin(root.yaw), math.cos(root.yaw))
    toward = ((requester[0] - root.x) / DELIVERY_STANDOFF_M, (requester[1] - root.z) / DELIVERY_STANDOFF_M)
    test.assertAlmostEqual(facing[0] * toward[0] + facing[1] * toward[1], 1.0, places=6)
    test.assertEqual((tractor.attached_to, wagon.attached_to), (wagon.id, tractor.id))
    test.assertLessEqual(math.dist(wagon.position, (root.x, root.z)), 15)
    test.assertTrue(tractor.stopped and wagon.stopped)
    test.assertIsNone(tractor.reserved_by)


class GiantsNavigationTests(unittest.TestCase):
    """AutoDrive is optional: without it, or without a fitting marker, GIANTS' own
    GOTO navigation travels and delivers (adapters/EVIDENCE.md)."""

    def world(self, capabilities=GOTO_TRACTOR, markers=DESTINATIONS, far=False, cls=MockWorld):
        tractor, wagon = simple_world().snapshot()
        tractor = replace(tractor, capabilities=capabilities)
        if far:
            tractor = replace(tractor, position=(600.0, 600.0), ai_root=Pose(600.0, 600.0, 0.0))
        return cls([tractor, wagon], PLAYERS, markers)

    def test_without_autodrive_here_is_delivered_by_goto(self):
        # A marker sits exactly at the requester, but this tractor has no AutoDrive.
        calls = []

        class Recording(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                calls.append(("navigate", pose))
                return super().navigate(tractor_id, pose, owner)

            def deliver(self, *args):
                raise AssertionError("AutoDrive used without AutoDrive")

            travel = deliver

        for seed in range(10):
            with self.subTest(seed=seed):
                calls.clear()
                rng = random.Random(seed)
                requester = (rng.uniform(-500, 500), rng.uniform(-500, 500))
                world = self.world(cls=Recording)
                world.players = {"farm-a": requester}
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "COMPLETE", result)
                self.assertEqual(result.states, ("FIND", "RESERVE", "STAGE", "LOCAL_MANEUVER", "ALIGN_REVERSE",
                                                 "ATTACH", "VERIFY", "DELIVER", "STOP", "RELEASE"))
                self.assertEqual(len(calls), 1)
                tractor, wagon = (world.items[x] for x in result.equipment_ids)
                assert_goto_delivery(self, tractor, wagon, requester)
                self.assertEqual(calls[0][1], tractor.ai_root)

    def test_goto_delivery_pose_is_decided_before_reservation(self):
        # The requester walking away after acceptance does not move the target.
        class Walk(MockWorld):
            def stage(self, *args):
                super().stage(*args)
                self.players["farm-a"] = (-700.0, 900.0)

        world = self.world(cls=Walk)
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE", result)
        tractor, wagon = (world.items[x] for x in result.equipment_ids)
        assert_goto_delivery(self, tractor, wagon, PLAYERS["farm-a"])

    def test_requester_at_the_wagon_still_gets_a_defined_pose(self):
        tractor, wagon = simple_world().snapshot()
        plan = plan_hitch(tractor, wagon)
        world = self.world()
        world.players = {"farm-a": plan.target}
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE", result)
        tractor, wagon = (world.items[x] for x in result.equipment_ids)
        assert_goto_delivery(self, tractor, wagon, plan.target)

    def test_goto_travel_that_ends_far_aims_at_the_next_approach_up_to_the_drive_cap(self):
        # Each drive moves the tractor but ends far off (NOT_REACHABLE on the way).
        for short, outcome in ((1, "COMPLETE"), (MAX_APPROACH_DRIVES, "FAILED")):
            with self.subTest(short=short):
                calls = []

                class Short(MockWorld):
                    def navigate(self, tractor_id, pose, owner):
                        calls.append(pose)
                        if len(calls) <= short:
                            x, z = self.items[tractor_id].position
                            return super().navigate(tractor_id, Pose(x - 20, z, pose.yaw), owner)
                        return super().navigate(tractor_id, pose, owner)

                world = self.world(GOTO_TRACTOR, far=True, cls=Short)
                tractor, wagon = world.snapshot()
                plan = plan_hitch(tractor, wagon)
                aims = approach_poses(plan, tractor.position, ()) + (plan.staging,)   # + the staging pose
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, outcome, result)
                self.assertEqual(calls[:min(short + 1, MAX_APPROACH_DRIVES)],
                                 list(aims[:min(short + 1, MAX_APPROACH_DRIVES)]))
                if outcome == "FAILED":
                    self.assertEqual(result.reason, "TRAVEL_ENDED_TOO_FAR")
                    self.assertEqual(len(calls), MAX_APPROACH_DRIVES)    # never more drives
                    self.assertNotIn("STAGE", result.states)

    def test_aims_that_leave_the_tractor_where_it_stood_cost_no_drive(self):
        # Live 27539c7115a44b86-1: NOT_REACHABLE at once, 1248 m off, unmoved.
        for unmoved, outcome in ((6, ("COMPLETE", "DELIVERY_OBSERVED")), (7, ("BLOCKED", "NO_SAFE_APPROACH"))):
            with self.subTest(unmoved=unmoved):
                calls = []

                class Unmoved(MockWorld):
                    def navigate(self, tractor_id, pose, owner):
                        calls.append(pose)
                        if len(calls) <= unmoved:
                            return None                     # GOTO ended where it started
                        return super().navigate(tractor_id, pose, owner)

                world = self.world(GOTO_TRACTOR, far=True, cls=Unmoved)
                result = Executor(world).execute(intent())
                self.assertEqual((result.status, result.reason), outcome, result)
                if outcome[0] == "BLOCKED":
                    self.assertEqual(len(calls), 7)                 # each aim once (six approaches + staging)
                    self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

    def test_a_goto_stopped_as_stalled_is_judged_by_the_observation(self):
        # Live 4167b7fd4e8143db-1: 1.2 km, then NAVIGATE_STALLED 12 m short of the aim.
        for near, outcome in ((True, "COMPLETE"), (False, "FAILED")):
            with self.subTest(near=near):
                calls = []

                class Stalled(MockWorld):
                    def navigate(self, tractor_id, pose, owner):
                        calls.append(pose)
                        if len(calls) > MAX_APPROACH_DRIVES or (near and len(calls) > 1):
                            return super().navigate(tractor_id, pose, owner)    # the delivery
                        if near:                                 # stuck 3 m short of the aim
                            super().navigate(tractor_id, Pose(pose.x + 3, pose.z, pose.yaw), owner)
                        raise StepFailed("NAVIGATE_STALLED")

                world = self.world(GOTO_TRACTOR, far=True, cls=Stalled)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, outcome, result)
                if near:
                    self.assertEqual(result.states.count("TRAVEL_NEAR"), 1)
                    self.assertIn("LOCAL_MANEUVER", result.states)
                else:                    # stuck where it stood, then nothing moves it
                    self.assertEqual(result.reason, "TRAPPED")
                    self.assertEqual(len(calls), 2)

    def test_a_stall_far_from_the_implement_ends_the_job_at_once(self):
        # Live 2ded907e5cb34b6c-4: ~560 m driven, stuck 718 m short; retrying other
        # aims beside the same implement from there was pointless.
        calls = []

        class FarStall(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                calls.append(pose)
                x, z = self.items[tractor_id].position
                super().navigate(tractor_id, Pose(x - 200, z - 200, pose.yaw), owner)
                raise StepFailed("NAVIGATE_STALLED")

        world = self.world(GOTO_TRACTOR, far=True, cls=FarStall)
        result = Executor(world).execute(intent())
        self.assertEqual((result.status, result.reason), ("FAILED", "NAVIGATE_STALLED"))
        self.assertEqual(len(calls), 1)

    def test_a_stall_beside_the_player_says_they_are_in_the_way(self):
        # Live 1600bdcae60f4499-1: GIANTS' agent BLOCKED 4.7 m from the player on foot.
        class PersonInTheWay(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                x, z = self.items[tractor_id].position
                super().navigate(tractor_id, Pose(x - 200, z - 200, pose.yaw), owner)
                self.players = {"farm-a": (x - 196.0, z - 197.0)}            # 5 m away
                raise StepFailed("NAVIGATE_STALLED")

        result = Executor(self.world(GOTO_TRACTOR, far=True, cls=PersonInTheWay)).execute(intent())
        self.assertEqual((result.status, result.reason), ("FAILED", "BLOCKED_BY_PERSON"))

    def test_stuck_beside_the_implement_then_unable_to_move_is_trapped(self):
        # Live 2ded907e5cb34b6c-1 (a building beside the trailer): GOTO stalled 7 m
        # from the approach, the maneuver refused, and no GOTO could move the tractor.
        calls = []

        class Trapped(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                calls.append(("navigate", pose))
                super().navigate(tractor_id, Pose(pose.x + 5, pose.z, pose.yaw), owner)
                raise StepFailed("NAVIGATE_STALLED")

            def stage(self, tractor_id, wagon_id, pose, owner):
                calls.append(("stage", pose))                 # ends where it stands

            def maneuver(self, *args):
                calls.append(("maneuver",))
                raise StepFailed("LOCAL_PATH_HITS_WAGON")

        world = self.world(GOTO_TRACTOR, far=True, cls=Trapped)
        result = Executor(world).execute(intent())
        self.assertEqual((result.status, result.reason), ("FAILED", "TRAPPED"))
        self.assertEqual([c[0] for c in calls], ["navigate", "maneuver", "stage"])     # no wasted retries
        self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

    def loose_world(self, cls):
        """The far tractor with a loose cultivator unhitched on its rear joint."""
        tractor, wagon = self.world(far=True).snapshot()
        rear = min(tractor.output_joints, key=lambda j: j.z)
        joint = tractor.ai_root.to_world(rear.x, rear.z + 0.12)
        loose = Equipment(id="loose-cultivator", category="implement", farm_id=tractor.farm_id,
                          position=(joint[0], joint[1] - 3.0), kinds=frozenset({"cultivator"}),
                          heading=0.0, input_joints=(InputJoint(1, rear.joint_type, joint, True),))
        return cls([tractor, wagon, loose], PLAYERS, DESTINATIONS), tractor

    def test_a_tractor_against_an_unhitched_implement_is_refused_before_any_motion(self):
        # Live 23a8b1d881c7489b-4: GIANTS' agent stays BLOCKED in every direction next
        # to a loose implement; no GOTO is started that cannot move it.
        calls = []

        class Recording(MockWorld):
            def navigate(self, *args):
                calls.append("navigate")

            stage = maneuver = align = navigate

        world, _ = self.loose_world(Recording)
        before = {i.id: i.position for i in world.snapshot()}
        result = Executor(world).execute(intent())
        self.assertEqual((result.status, result.reason), ("BLOCKED", "AGAINST_IMPLEMENT"), result)
        self.assertEqual(calls, [])
        self.assertEqual({i.id: i.position for i in world.snapshot()}, before)
        self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

    def test_against_a_loose_implement_only_for_another_unattached_implement_on_a_joint(self):
        world, tractor = self.loose_world(MockWorld)
        _, wagon, loose = world.snapshot()
        self.assertTrue(against_loose_implement(tractor, [loose]))
        self.assertAlmostEqual(joint_separation(tractor, loose), 0.12)
        self.assertFalse(against_loose_implement(tractor, [wagon]))       # far away
        self.assertFalse(against_loose_implement(tractor, [replace(loose, attached_to="someone")]))
        moved = replace(loose, input_joints=tuple(replace(i, position=(i.position[0], i.position[1] - 2.0))
                                                  for i in loose.input_joints))
        self.assertFalse(against_loose_implement(tractor, [moved]))

    def test_aims_giants_refuses_as_blocked_are_skipped_without_moving(self):
        # AIParameterPosition:validate 83-90: a target on blocked ground is refused.
        for refused, outcome in ((6, "COMPLETE"), (7, "BLOCKED")):          # six approaches + the staging pose
            with self.subTest(refused=refused):
                calls = []

                class Blocked(MockWorld):
                    def navigate(self, tractor_id, pose, owner):
                        calls.append(pose)
                        if len(calls) <= refused:
                            raise StepFailed("NAVIGATE_GOTO_INVALID")
                        return super().navigate(tractor_id, pose, owner)

                world = self.world(GOTO_TRACTOR, far=True, cls=Blocked)
                tractor, wagon = world.snapshot()
                plan = plan_hitch(tractor, wagon)
                aims = approach_poses(plan, tractor.position, ()) + (plan.staging,)   # + the staging pose
                before = world.snapshot()
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, outcome, result)
                self.assertEqual(calls[:len(aims)], list(aims[:len(calls)]))
                if outcome == "BLOCKED":
                    self.assertEqual(result.reason, "NO_SAFE_APPROACH")
                    self.assertEqual(len(calls), len(aims))          # each aim once
                    self.assertEqual([i.position for i in world.snapshot()], [i.position for i in before])

    def test_far_tractor_without_marker_travels_by_goto_to_staging(self):
        for capabilities in (GOTO_TRACTOR, AUTODRIVE_TRACTOR):     # AD, but no marker near the wagon
            with self.subTest(capabilities=sorted(capabilities)):
                calls = []

                class Recording(MockWorld):
                    def navigate(self, tractor_id, pose, owner):
                        calls.append(pose)
                        return super().navigate(tractor_id, pose, owner)

                    def travel(self, *args):
                        raise AssertionError("AutoDrive travel without a marker near the wagon")

                world = self.world(capabilities, far=True, cls=Recording)
                tractor, wagon = world.snapshot()
                plan = plan_hitch(tractor, wagon)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, "COMPLETE", result)
                # One GOTO, aimed at the approach beside the implement; it ended there,
                # inside the work radius, so no second GOTO: the local maneuver lines up.
                self.assertEqual(result.states[:5],
                                 ("FIND", "RESERVE", "TRAVEL_NEAR", "LOCAL_MANEUVER", "ALIGN_REVERSE"))
                self.assertEqual(calls[0], approach_poses(plan, tractor.position, ())[0])

    def test_goto_travel_or_delivery_not_observed_stops_and_fails_closed(self):
        class Short(MockWorld):
            def __init__(self, *args, step, miss):
                super().__init__(*args)
                self.step, self.miss, self.after = step, miss, []

            def navigate(self, tractor_id, pose, owner):
                attached = self.items[tractor_id].attached_to is not None
                if (self.step == "deliver") == attached:
                    pose = Pose(pose.x + self.miss, pose.z, pose.yaw)     # GIANTS ended elsewhere
                super().navigate(tractor_id, pose, owner)

            def stage(self, *args):
                self.after.append("stage")
                return super().stage(*args)

            def maneuver(self, *args):
                self.after.append("maneuver")
                return super().maneuver(*args)

        # Coarse travel need only end near the wagon (inside the work radius the local
        # maneuver lines up); a delivery must arrive.
        for step, miss, outcome, far in (("travel", 4.0, ("COMPLETE", "DELIVERY_OBSERVED"), True),
                                         ("travel", 100.0, ("FAILED", "TRAVEL_ENDED_TOO_FAR"), True),
                                         ("deliver", 4.0, ("UNVERIFIED", "DELIVERY_NOT_OBSERVED"), False)):
            with self.subTest(step=step, miss=miss):
                tractor, wagon = self.world(far=far).snapshot()
                world = Short([tractor, wagon], PLAYERS, (), step=step, miss=miss)
                result = Executor(world).execute(intent())
                self.assertEqual((result.status, result.reason), outcome)
                self.assertEqual(result.states[-2:], ("STOP", "RELEASE"))
                if step == "travel":
                    self.assertEqual(world.after, [] if miss > 60 else ["maneuver"])
                self.assertTrue(all(x.reserved_by is None and x.stopped for x in world.snapshot()))

    def test_goto_step_failures_stop_without_further_action(self):
        for fault, status in (("delivery", "FAILED"), ("delivery_timeout", "FAILED"),
                              ("false_arrival", "UNVERIFIED"), ("moving", "UNVERIFIED")):
            with self.subTest(fault=fault):
                world = FaultWorld(fault)
                tractor_id = next(x.id for x in world.snapshot() if x.category == "tractor")
                world.items[tractor_id] = replace(world.items[tractor_id], capabilities=GOTO_TRACTOR)
                result = Executor(world).execute(intent())
                self.assertEqual(result.status, status, result)
                self.assertEqual(result.states[-3:], ("DELIVER", "STOP", "RELEASE"))

    def test_explicit_marker_needs_autodrive_and_blocks_before_reservation(self):
        world = self.world()
        before = world.snapshot()
        command = dict(intent(), target={"type": "autodrive_marker", "marker_id": "marker-at-requester"})
        result = Executor(world).execute(command)
        self.assertEqual((result.status, result.reason), ("BLOCKED", "AUTODRIVE_UNAVAILABLE"))
        self.assertNotIn("RESERVE", result.states)
        self.assertEqual(world.snapshot(), before)

    def test_autodrive_is_preferred_when_a_marker_fits(self):
        world = self.world(AUTODRIVE_TRACTOR)
        world.navigate = None          # any GOTO call would raise TypeError
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(world.items[result.equipment_ids[0]].position, (100, 200))

    def test_mock_navigate_refuses_unreserved_or_invalid(self):
        world = self.world()
        tractor, wagon = world.snapshot()
        with self.assertRaises(ValueError):
            world.navigate(tractor.id, Pose(0.0, 0.0, 0.0), "nobody")
        self.assertTrue(world.reserve((tractor.id, wagon.id), "owner"))
        with self.assertRaises(ValueError):
            world.navigate(tractor.id, Pose(float("nan"), 0.0, 0.0), "owner")


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
                if name in ("reserve", "travel", "stage", "maneuver", "align", "attach", "deliver", "stop", "release"):
                    calls.append(name)
                return super().__getattribute__(name)

        base = explicit_world()
        world = RecordingWorld(base.snapshot(), PLAYERS, MARKERS, map_id="MapUS")
        result = Executor(world, field_entrances=FIELD_ENTRANCES).execute(
            target_intent({"type": "field_entrance", "field": 12}))
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(calls, ["reserve", "stage", "maneuver", "align", "attach", "deliver", "stop", "release"])

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

            def maneuver(self, tractor_id, wagon_id, plan, owner):
                calls.append(("maneuver", tractor_id, wagon_id, plan))
                return super().maneuver(tractor_id, wagon_id, plan, owner)

            def align(self, tractor_id, wagon_id, plan, owner):
                calls.append(("align", tractor_id, wagon_id, plan))
                return super().align(tractor_id, wagon_id, plan, owner)

        world = Recording(None)
        tractor, wagon = world.snapshot()
        plan = plan_hitch(tractor, wagon)
        aim = approach_poses(plan, tractor.position, ())[0]
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE", result)
        # GOTO aims beside the implement; the bounded maneuver lines up from there.
        self.assertEqual(calls, [("stage", tractor.id, wagon.id, aim),
                                 ("maneuver", tractor.id, wagon.id, plan),
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
        # AutoDrive's marker is 10 m from the staging pose: inside the local work
        # radius, so no GOTO; the local maneuver lines up.
        self.assertEqual(result.states, ("FIND", "RESERVE", "TRAVEL_NEAR", "LOCAL_MANEUVER", "ALIGN_REVERSE",
                                         "ATTACH", "VERIFY", "DELIVER", "STOP", "RELEASE"))

        class ShortTravel(MockWorld):
            def travel(self, tractor_id, destination_id, owner):
                super().travel(tractor_id, destination_id, owner)
                tractor = self.items[tractor_id]
                # AutoDrive ended far from the marker (and the wagon).
                self.items[tractor_id] = replace(tractor, position=(500.0, 500.0), ai_root=Pose(500.0, 500.0, 0.0))

            def stage(self, *args):
                raise AssertionError("staged after travel that ended far away")

        world = ShortTravel([far, wagon], PLAYERS, DESTINATIONS + (near_wagon,))
        result = Executor(world).execute(intent())
        self.assertEqual((result.status, result.reason), ("FAILED", "TRAVEL_ENDED_TOO_FAR"))

    def test_unreachable_or_unplannable_pairs_block_before_reservation(self):
        base = simple_world()
        tractor, wagon = base.snapshot()
        far = replace(tractor, position=(600.0, 600.0), ai_root=Pose(600.0, 600.0, 0.0))
        cases = [
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
            "wagon_moved_after_stage": ("WAGON_MOVED", 1, False),
            "reservation_lost_after_stage": ("CLEANUP_NOT_CONFIRMED", 1, False),
            "align_no_progress": ("ALIGN_NOT_OBSERVED", 3, False),
            "false_attach": ("ATTACHMENT_MISMATCH", 3, True),
            "still_moving_after_attach": ("EQUIPMENT_MOVING", 3, True),
        }
        for fault, (reason, moves, attached) in cases.items():
            with self.subTest(fault=fault):
                world, result = self.run_fault(fault)
                self.assertEqual((result.status, result.reason), ("UNVERIFIED", reason))
                self.assertEqual(world.moves, moves)
                self.assertEqual(len(world.attach_calls), 1 if attached else 0)
                self.assertNotIn("DELIVER", result.states)

    def test_a_stage_end_outside_the_reverse_envelope_is_lined_up_locally(self):
        # GOTO ended 5 m to the side of the staging aim: not reachable by a reverse, so
        # the bounded local maneuver runs first (never a second GOTO).
        world, result = self.run_fault("stage_elsewhere")
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(result.states[2:5], ("STAGE", "LOCAL_MANEUVER", "ALIGN_REVERSE"))
        self.assertEqual(world.moves, 4)                    # stage, maneuver, align, delivery

    def test_a_maneuver_end_outside_the_reverse_envelope_never_reverses(self):
        world, result = self.run_fault("maneuver_elsewhere")
        self.assertEqual((result.status, result.reason), ("FAILED", "ALIGN_LATERAL_OUT_OF_RANGE"))
        self.assertEqual(world.moves, 2)                    # stage, maneuver
        self.assertEqual(world.attach_calls, [])
        self.assertEqual(result.states[-4:], ("STAGE", "LOCAL_MANEUVER", "STOP", "RELEASE"))
        self.assertTrue(all(x.reserved_by is None and x.stopped for x in world.snapshot()))

    def test_a_failed_maneuver_stops_and_goes_no_further(self):
        for fault in ("maneuver", "maneuver_timeout"):
            with self.subTest(fault=fault):
                world, result = self.run_fault(fault)
                self.assertEqual(result.status, "FAILED", result)
                self.assertEqual(world.moves, 2)
                self.assertEqual(world.attach_calls, [])
                self.assertNotIn("ALIGN_REVERSE", result.states)
                self.assertTrue(all(x.reserved_by is None and x.stopped for x in world.snapshot()))

    def test_stage_ending_beyond_the_work_radius_stops_before_any_local_motion(self):
        class FarStage(FaultWorld):
            def stage(self, tractor_id, wagon_id, pose, owner):
                super().stage(tractor_id, wagon_id, Pose(pose.x + 60, pose.z, pose.yaw), owner)

            def maneuver(self, *args):
                raise AssertionError("local maneuver beyond the work radius")

        world = FarStage(None)
        result = Executor(world).execute(intent())
        self.assertEqual((result.status, result.reason), ("FAILED", "TRAVEL_ENDED_TOO_FAR"))
        # Each approach is aimed at once, at most MAX_APPROACH_DRIVES of them, then it stops.
        self.assertEqual(world.moves, MAX_APPROACH_DRIVES)
        self.assertEqual(result.states.count("STAGE"), MAX_APPROACH_DRIVES)

    def test_a_tractor_inside_the_work_radius_never_uses_goto(self):
        base = simple_world()
        tractor, wagon = base.snapshot()
        plan = plan_hitch(tractor, wagon)
        # 15 m to the side of the wagon's joint, turned 90 degrees: inside the radius.
        start = Pose(plan.target[0] + 15.0, plan.target[1], plan.final.yaw + math.pi / 2)
        near = replace(tractor, position=(start.x, start.z), ai_root=start, heading=start.yaw)

        class NoGoto(FaultWorld):
            def stage(self, *args):
                raise AssertionError("GOTO inside the work radius")

        world = NoGoto(None)
        world.items[near.id] = near
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(result.states[2:4], ("LOCAL_MANEUVER", "ALIGN_REVERSE"))

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

    def test_attachment_through_another_joint_is_not_verified(self):
        base = simple_world()
        tractor, wagon = base.snapshot()
        joints = tractor.output_joints + (OutputJoint(4, "drawbar", 0.0, -2.6, True, frozenset({"rearPto"})),)

        class OtherJoint(MockWorld):
            def attach(self, tractor_id, wagon_id, planned, owner):
                super().attach(tractor_id, wagon_id, planned, owner)
                # Both ends name each other, but GIANTS used joint 4, not the plan's 3.
                t = self.items[tractor_id]
                self.items[tractor_id] = replace(t, output_joints=tuple(
                    replace(j, free=j.index != 4) for j in t.output_joints))

        world = OtherJoint([replace(tractor, output_joints=joints), wagon], PLAYERS, DESTINATIONS)
        result = Executor(world).execute(intent())
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "ATTACHMENT_MISMATCH"))
        self.assertNotIn("DELIVER", result.states)

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


# --------------------------------------------------------------------- POWER/PTO
class PowerAndPtoTests(unittest.TestCase):
    """GIANTS-defined power sufficiency and per-joint PTO compatibility."""

    def run_pair(self, tractor_change=None, wagon_change=None):
        base = simple_world()
        tractor, wagon = base.snapshot()
        world = MockWorld([replace(tractor, **(tractor_change or {})),
                           replace(wagon, **(wagon_change or {}))], PLAYERS, DESTINATIONS)
        before = world.snapshot()
        result = Executor(world).execute(intent())
        return world, before, result

    def assert_blocked(self, reason, **changes):
        world, before, result = self.run_pair(**changes)
        self.assertEqual((result.status, result.reason), ("BLOCKED", reason))
        self.assertNotIn("RESERVE", result.states)
        self.assertEqual(world.snapshot(), before)

    def test_adequate_or_exact_capacity_is_accepted(self):
        for capacity in (1.0, 1.8, 50.0):
            with self.subTest(capacity=capacity):
                _, _, result = self.run_pair({"pto_torque_capacity": capacity})
                self.assertEqual(result.status, "COMPLETE", result)

    def test_insufficient_capacity_is_rejected(self):
        for capacity in (0.0, 0.99):
            with self.subTest(capacity=capacity):
                self.assert_blocked("NO_ELIGIBLE_PAIR", tractor_change={"pto_torque_capacity": capacity})

    def test_no_power_requirement_is_not_rejected(self):
        # A wagon needing no PTO power: tractor capacity and PTO names are irrelevant.
        no_pto = (OutputJoint(3, "drawbar", 0.0, -2.5, True, None),)
        for tractor_change in ({"pto_torque_capacity": None}, {"pto_torque_capacity": 0.0},
                               {"output_joints": no_pto}):
            with self.subTest(tractor_change=tractor_change):
                _, _, result = self.run_pair(tractor_change, {"required_pto_torque": 0.0})
                self.assertEqual(result.status, "COMPLETE", result)

    def test_unknown_power_fails_closed(self):
        for wagon_change in ({"required_pto_torque": None}, {"required_pto_torque": float("nan")},
                             {"required_pto_torque": -1.0}):
            with self.subTest(wagon_change=wagon_change):
                self.assert_blocked("NO_ELIGIBLE_PAIR", wagon_change=wagon_change)
        for capacity in (None, float("nan"), float("inf")):
            with self.subTest(capacity=capacity):
                self.assert_blocked("NO_ELIGIBLE_PAIR", tractor_change={"pto_torque_capacity": capacity})

    def test_pto_required_without_matching_pto_at_the_planned_joints(self):
        def outputs(names):
            return (OutputJoint(3, "drawbar", 0.0, -2.5, True, names),)

        def inputs(names):
            return (InputJoint(1, "drawbar", (40.0, 24.0), True, names),)

        cases = [({"output_joints": outputs(frozenset())}, None),
                 ({"output_joints": outputs(frozenset({"frontPto"}))}, None),
                 ({"output_joints": outputs(None)}, None),             # unknown outputs
                 (None, {"input_joints": inputs(frozenset())}),
                 (None, {"input_joints": inputs(None)})]                # unknown inputs
        for tractor_change, wagon_change in cases:
            with self.subTest(tractor=tractor_change, wagon=wagon_change):
                self.assert_blocked("NO_HITCH_GEOMETRY", tractor_change=tractor_change,
                                    wagon_change=wagon_change)

    def test_pto_is_matched_on_the_planned_joint_not_any_joint(self):
        # The only joint with the matching PTO is a front joint, which is never planned.
        joints = (OutputJoint(1, "drawbar", 0.0, 3.0, True, frozenset({"rearPto"})),
                  OutputJoint(3, "drawbar", 0.0, -2.5, True, frozenset()))
        self.assert_blocked("NO_HITCH_GEOMETRY", tractor_change={"output_joints": joints})
        # With a matching rear joint as well, that joint is planned and used.
        joints += (OutputJoint(4, "drawbar", 0.0, -2.6, True, frozenset({"rearPto"})),)
        world, _, result = self.run_pair({"output_joints": joints})
        self.assertEqual(result.status, "COMPLETE", result)
        tractor = world.items[result.equipment_ids[0]]
        self.assertEqual([j.index for j in tractor.output_joints if not j.free], [4])


class CancellationTests(unittest.TestCase):
    """STOP/CANCEL: deterministic, honoured before every physical step, and a
    cancelled pair is stopped and released like after any failure."""

    def test_cancel_before_start_moves_and_reserves_nothing(self):
        import threading
        world = simple_world()
        before = world.snapshot()
        cancel = threading.Event()
        cancel.set()
        result = Executor(world).execute(intent(), cancel=cancel)
        self.assertEqual((result.status, result.reason), ("CANCELLED", "CANCELLED_BY_REQUESTER"))
        self.assertNotIn("RESERVE", result.states)
        self.assertEqual(world.snapshot(), before)

    def test_cancel_between_steps_stops_before_the_next_one(self):
        import threading
        for step in ("stage", "maneuver", "align", "attach"):
            with self.subTest(after=step):
                cancel = threading.Event()
                calls = []

                class Cancelling(FaultWorld):
                    def stage(self, *args):
                        calls.append("stage")
                        super().stage(*args)
                        if step == "stage":
                            cancel.set()

                    def maneuver(self, *args):
                        calls.append("maneuver")
                        super().maneuver(*args)
                        if step == "maneuver":
                            cancel.set()

                    def align(self, *args):
                        calls.append("align")
                        super().align(*args)
                        if step == "align":
                            cancel.set()

                    def attach(self, *args):
                        calls.append("attach")
                        super().attach(*args)
                        if step == "attach":
                            cancel.set()

                    def deliver(self, *args):
                        calls.append("deliver")
                        super().deliver(*args)

                world = Cancelling(None)
                result = Executor(world).execute(intent(), cancel=cancel)
                self.assertEqual((result.status, result.reason), ("CANCELLED", "CANCELLED_BY_REQUESTER"))
                self.assertEqual(calls[-1], step)                  # nothing after the cancel
                self.assertEqual(result.states[-2:], ("STOP", "RELEASE"))
                self.assertTrue(all(x.reserved_by is None and x.stopped for x in world.snapshot()))

    def test_a_step_pre_empted_by_the_stop_is_cancelled_not_failed(self):
        import threading
        cancel = threading.Event()

        class Preempted(FaultWorld):
            def align(self, *args):
                cancel.set()                                       # the requester said stop...
                raise RuntimeError("STOPPED")                      # ...and the game pre-empted ALIGN

        result = Executor(Preempted(None)).execute(intent(), cancel=cancel)
        self.assertEqual(result.status, "CANCELLED")
        world, plain = Preempted(None), None
        plain = Executor(world).execute(intent())                  # the same failure without a cancel
        self.assertEqual(plain.status, "FAILED")

    def test_an_attach_interrupted_by_the_stop_is_cancelled_once_the_stop_is_confirmed(self):
        import threading
        from adapters.world import StepUnverified
        for stop_fault, expected in ((None, ("CANCELLED", "CANCELLED_BY_REQUESTER")),
                                     ("stop", ("UNVERIFIED", "CLEANUP_NOT_CONFIRMED"))):
            with self.subTest(stop_fault=stop_fault):
                cancel = threading.Event()

                class Interrupted(FaultWorld):
                    def attach(self, *args):
                        cancel.set()                       # the stop pre-empted GIANTS' attach:
                        raise StepUnverified("ATTACH_ABSENT")   # its end is unknown

                world = Interrupted(stop_fault)
                result = Executor(world).execute(intent(), cancel=cancel)
                self.assertEqual((result.status, result.reason), expected)
                self.assertNotIn("DELIVER", result.states)
        plain = Executor(type("Plain", (FaultWorld,), {"attach": lambda self, *a: (_ for _ in ()).throw(
            StepUnverified("ATTACH_ABSENT"))})(None)).execute(intent())
        self.assertEqual(plain.status, "UNVERIFIED")              # without a cancel: still unclear

    def test_cancelled_pair_that_cannot_be_stopped_stays_reserved(self):
        import threading
        cancel = threading.Event()

        class NoStop(FaultWorld):
            def stage(self, *args):
                super().stage(*args)
                cancel.set()

        world = NoStop("stop")
        result = Executor(world).execute(intent(), cancel=cancel)
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "CLEANUP_NOT_CONFIRMED"))
        self.assertTrue(all(x.reserved_by is not None for x in world.snapshot()))

    def test_states_are_reported_as_they_are_entered(self):
        seen = []
        result = Executor(simple_world()).execute(intent(), on_state=seen.append)
        self.assertEqual(tuple(seen), result.states)


def hitched_pair(tractor_id, wagon_id, at, capabilities=GOTO_TRACTOR):
    """A tractor with its forage wagon already hitched behind it, at ``at``."""
    x, z = at
    tractor = Equipment(tractor_id, "tractor", "farm-a", (x, z), pto_torque_capacity=1.8,
                        capabilities=capabilities, heading=0.0, ai_root=Pose(x, z, 0.0),
                        attached_to=wagon_id,
                        output_joints=(OutputJoint(3, "drawbar", 0.0, -2.5, False, frozenset({"rearPto"})),))
    wagon = Equipment(wagon_id, "forage_wagon", "farm-a", (x, z - 7), required_pto_torque=1.0,
                      heading=0.0, attached_to=tractor_id,
                      input_joints=(InputJoint(1, "drawbar", (x, z - 2.5), True, frozenset({"rearPto"})),))
    return tractor, wagon


BARN = Destination("place-0", (-200.0, 50.0), "Cow Barn (Medium)")
SHED = Destination("place-1", (300.0, -80.0), "Machine Shed")


def move(target="player_at_acceptance", request_id="move-001"):
    return {**intent(request_id), "task": "move_pair", "target": target}


class MovePairTests(unittest.TestCase):
    """"come here" / "take this to the barn": the hitched pair nearest the
    requester ("this") is moved, verified, stopped and released."""

    def test_come_here_moves_the_nearest_hitched_pair(self):
        for seed in range(10):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                near = hitched_pair(f"t-{rng.randrange(10**6)}", f"w-{rng.randrange(10**6)}", (130.0, 260.0))
                far = hitched_pair(f"t-{rng.randrange(10**6)}", f"w-{rng.randrange(10**6)}", (-600.0, 900.0))
                loose = simple_world().snapshot()                     # an unhitched pair: not "this"
                items = [*near, *far, *loose]
                rng.shuffle(items)
                world = MockWorld(items, PLAYERS, ())
                before = {x.id: x for x in world.snapshot()}
                result = Executor(world).execute(move())
                self.assertEqual(result.status, "COMPLETE", result)
                self.assertEqual(result.states, ("FIND", "RESERVE", "DELIVER", "STOP", "RELEASE"))
                self.assertEqual(result.equipment_ids, (near[0].id, near[1].id))
                tractor, wagon = (world.items[x] for x in result.equipment_ids)
                assert_goto_delivery(self, tractor, wagon, PLAYERS["farm-a"])
                for item in (*far, *loose):
                    self.assertEqual(world.items[item.id], before[item.id])

    def test_nothing_hitched_or_usable_blocks_before_reservation(self):
        tractor, wagon = hitched_pair("t1", "w1", (0.0, 0.0))
        cases = {"nothing hitched": simple_world().snapshot(),
                 "occupied": (replace(tractor, occupied=True), wagon),
                 "busy wagon": (tractor, replace(wagon, busy=True)),
                 "moving": (replace(tractor, stopped=False), wagon),
                 "one-sided hitch": (tractor, replace(wagon, attached_to=None)),
                 "no move capability": (replace(tractor, capabilities=frozenset({"stop"})), wagon),
                 "other farm": (replace(tractor, farm_id="farm-b"), replace(wagon, farm_id="farm-b"))}
        for name, items in cases.items():
            with self.subTest(name):
                world = MockWorld(items, PLAYERS, ())
                before = world.snapshot()
                result = Executor(world).execute(move())
                self.assertEqual((result.status, result.reason), ("BLOCKED", "NO_HITCHED_PAIR"))
                self.assertEqual(world.snapshot(), before)

    def test_a_named_building_is_reached_by_goto_short_of_it(self):
        world = MockWorld(hitched_pair("t1", "w1", (100.0, 180.0)), PLAYERS, (), places=(BARN, SHED))
        result = Executor(world).execute(move({"type": "place", "name": "barn"}))
        self.assertEqual(result.status, "COMPLETE", result)
        root = world.items["t1"].ai_root
        self.assertAlmostEqual(math.dist((root.x, root.z), BARN.position), PLACE_STANDOFF_M, places=6)
        toward = (BARN.position[0] - root.x, BARN.position[1] - root.z)
        self.assertGreater(math.sin(root.yaw) * toward[0] + math.cos(root.yaw) * toward[1], 0)

    def test_an_exact_marker_is_preferred_with_autodrive(self):
        marker = Destination("m-9", (-180.0, 60.0), "Barn")
        world = MockWorld(hitched_pair("t1", "w1", (100.0, 180.0), AUTODRIVE_TRACTOR), PLAYERS, (marker,),
                          places=(BARN,))
        result = Executor(world).execute(move({"type": "place", "name": "barn"}))
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(world.items["t1"].position, marker.position)
        # Without AutoDrive the same words go to the building instead.
        world = MockWorld(hitched_pair("t1", "w1", (100.0, 180.0)), PLAYERS, (marker,), places=(BARN,))
        self.assertEqual(Executor(world).execute(move({"type": "place", "name": "barn"})).status, "COMPLETE")
        root = world.items["t1"].ai_root
        self.assertAlmostEqual(math.dist((root.x, root.z), BARN.position), PLACE_STANDOFF_M, places=6)

    def test_unknown_ambiguous_or_marker_only_places_block_before_reservation(self):
        second_barn = Destination("place-2", (10.0, 10.0), "Pig Barn")
        marker_only = Destination("m-1", (5.0, 5.0), "Yard")
        cases = [("barn", (BARN, second_barn), (), "DESTINATION_AMBIGUOUS"),
                 ("silo", (BARN, SHED), (), "DESTINATION_NOT_FOUND"),
                 ("barn shed", (BARN, SHED), (), "DESTINATION_NOT_FOUND"),   # all words must match one
                 ("yard", (), (marker_only,), "AUTODRIVE_UNAVAILABLE")]       # marker, but no AutoDrive
        for name, places, markers, reason in cases:
            with self.subTest(name=name):
                world = MockWorld(hitched_pair("t1", "w1", (0.0, 0.0)), PLAYERS, markers, places=places)
                before = world.snapshot()
                result = Executor(world).execute(move({"type": "place", "name": name}))
                self.assertEqual((result.status, result.reason), ("BLOCKED", reason))
                self.assertEqual(world.snapshot(), before)

    def test_fetch_to_a_named_building(self):
        base = simple_world()
        world = MockWorld([replace(x, capabilities=GOTO_TRACTOR) if x.category == "tractor" else x
                           for x in base.snapshot()], PLAYERS, (), places=(SHED,))
        result = Executor(world).execute({**intent(), "target": {"type": "place", "name": "machine shed"}})
        self.assertEqual(result.status, "COMPLETE", result)
        root = world.items[result.equipment_ids[0]].ai_root
        self.assertAlmostEqual(math.dist((root.x, root.z), SHED.position), PLACE_STANDOFF_M, places=6)

    def test_move_failures_and_cancel_stop_and_fail_closed(self):
        import threading

        class Short(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                super().navigate(tractor_id, Pose(pose.x + 6.0, pose.z, pose.yaw), owner)

        world = Short(hitched_pair("t1", "w1", (0.0, 0.0)), PLAYERS, ())
        result = Executor(world).execute(move())
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "DELIVERY_NOT_OBSERVED"))
        self.assertTrue(all(x.reserved_by is None and x.stopped for x in world.snapshot()))
        cancel = threading.Event()

        class CancelOnReserve(MockWorld):
            def reserve(self, ids, owner):
                ok = super().reserve(ids, owner)
                cancel.set()
                return ok

        world = CancelOnReserve(hitched_pair("t1", "w1", (0.0, 0.0)), PLAYERS, ())
        result = Executor(world).execute(move(), cancel=cancel)
        self.assertEqual((result.status, result.reason), ("CANCELLED", "CANCELLED_BY_REQUESTER"))
        self.assertNotIn("DELIVER", result.states)
        self.assertEqual(world.items["t1"].position, (0.0, 0.0))

    def test_reservation_accepts_only_unattached_or_each_other(self):
        tractor, wagon = hitched_pair("t1", "w1", (0.0, 0.0))
        cases = [((tractor, wagon), True),
                 ((replace(tractor, attached_to="stranger"), replace(wagon, attached_to="stranger-2")), False),
                 ((tractor, replace(wagon, attached_to=None)), False),          # one-sided
                 ((replace(tractor, attached_to=None), replace(wagon, attached_to=None)), True)]
        for (t, w), expected in cases:
            with self.subTest(attached=(t.attached_to, w.attached_to)):
                world = MockWorld((t, w), PLAYERS, ())
                self.assertEqual(world.reserve((t.id, w.id), "owner"), expected)


class RideAlongTests(unittest.TestCase):
    def test_the_tractor_the_requester_rides_is_used_first(self):
        tractor, wagon = simple_world().snapshot()
        other = replace(tractor, id="000-first-by-id")
        ridden = replace(tractor, id="zzz-ridden", ridden=True)
        world = MockWorld([other, ridden, wagon], PLAYERS, DESTINATIONS)
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(result.equipment_ids[0], "zzz-ridden")

    def test_an_unknown_seat_still_fails_closed(self):
        tractor, wagon = simple_world().snapshot()
        world = MockWorld([replace(tractor, occupied=True), wagon], PLAYERS, DESTINATIONS)
        self.assertEqual(Executor(world).execute(intent()).status, "BLOCKED")


class HitchOnlyTests(unittest.TestCase):
    def test_hitch_pair_hitches_where_the_wagon_stands_and_delivers_nothing(self):
        world = simple_world()
        tractor, wagon = world.snapshot()
        plan = plan_hitch(tractor, wagon)
        result = Executor(world).execute({**intent(), "task": "hitch_pair"})
        self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"))
        self.assertEqual(result.states, ("FIND", "RESERVE", "STAGE", "LOCAL_MANEUVER", "ALIGN_REVERSE", "ATTACH", "VERIFY",
                                         "STOP", "RELEASE"))
        tractor, wagon = (world.items[x] for x in result.equipment_ids)
        self.assertEqual((tractor.attached_to, wagon.attached_to), (wagon.id, tractor.id))
        self.assertEqual(tractor.ai_root, plan.final)                 # at the wagon, not at the requester
        self.assertIsNone(tractor.reserved_by)

    def test_the_named_implement_kind_decides_the_pair(self):
        tractor, wagon = simple_world().snapshot()
        # A cultivator and a seeder beside the forage wagon (IDs sort before it).
        cultivator = replace(wagon, id="0-cultivator", category="implement", kinds=frozenset({"cultivator"}),
                             position=(wagon.position[0] + 30, wagon.position[1]),
                             input_joints=tuple(replace(j, position=(j.position[0] + 30, j.position[1]))
                                                for j in wagon.input_joints))
        seeder = replace(cultivator, id="0-seeder", kinds=frozenset({"sowing_machine", "fill_unit"}),
                         position=(wagon.position[0] - 30, wagon.position[1]),
                         input_joints=tuple(replace(j, position=(j.position[0] - 30, j.position[1]))
                                            for j in wagon.input_joints))
        for kind, expected in ((None, wagon.id), ("forage_wagon", wagon.id), ("cultivator", "0-cultivator"),
                               ("sowing_machine", "0-seeder")):
            with self.subTest(kind=kind):
                world = MockWorld([tractor, cultivator, seeder, wagon], PLAYERS, DESTINATIONS)
                request = {**intent(), "task": "hitch_pair", **({"implement": kind} if kind else {})}
                result = Executor(world).execute(request)
                self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"), kind)
                self.assertEqual(result.equipment_ids, (tractor.id, expected))
        world = MockWorld([tractor, cultivator, wagon], PLAYERS, DESTINATIONS)
        result = Executor(world).execute({**intent(), "task": "hitch_pair", "implement": "plow"})
        self.assertEqual((result.status, result.reason), ("BLOCKED", "NO_ELIGIBLE_PAIR"))     # none on the farm
        self.assertEqual(Executor(world).execute({**intent(), "task": "move_pair", "implement": "plow"}).reason,
                         "INVALID_INTENT")                                                  # not for move_pair

    def test_joints_that_already_meet_attach_without_any_motion(self):
        # Live ec4422f2a4484f9e-3: re-hitching right after a detach, gap 0.12 m with the
        # tractor joint 0.11 m on the far side: a look-only reverse, then ATTACH.
        calls = []

        class Recording(MockWorld):
            def align(self, tractor_id, wagon_id, plan, owner, check_only=False):
                calls.append(("align", check_only))
                return super().align(tractor_id, wagon_id, plan, owner, check_only)

            def maneuver(self, *args):
                raise AssertionError("no maneuver at the hitch point")

            def stage(self, *args):
                raise AssertionError("no GOTO at the hitch point")

        tractor, wagon = simple_world().snapshot()
        plan = plan_hitch(tractor, wagon)
        back = math.sin(plan.final.yaw) * -0.11, math.cos(plan.final.yaw) * -0.11
        there = Pose(plan.final.x + back[0], plan.final.z + back[1], plan.final.yaw)
        world = Recording([replace(tractor, position=(there.x, there.z), ai_root=there, heading=there.yaw), wagon],
                          PLAYERS, DESTINATIONS)
        result = Executor(world).execute({**intent(), "task": "hitch_pair"})
        self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"), result)
        self.assertEqual(result.states, ("FIND", "RESERVE", "ALIGN_REVERSE", "ATTACH", "VERIFY", "STOP", "RELEASE"))
        self.assertEqual(calls, [("align", True)])

    def test_a_failed_hitch_is_never_complete(self):
        world = FaultWorld("false_attach")
        result = Executor(world).execute({**intent(), "task": "hitch_pair"})
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "ATTACHMENT_MISMATCH"))


def detach(implement=None, request_id="detach-001"):
    command = {**intent(request_id), "task": "detach_pair"}
    return {**command, "implement": implement} if implement else command


class DetachTests(unittest.TestCase):
    """"unhitch the cultivator": the hitched pair nearest the requester (of the named
    kind) is detached where it stands, verified from fresh state and released."""

    def test_detach_unhitches_the_nearest_pair_of_the_kind_and_moves_nothing(self):
        for seed in range(6):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                near = hitched_pair(f"t-{rng.randrange(10**6)}", f"w-{rng.randrange(10**6)}", (130.0, 260.0))
                far = hitched_pair(f"t-{rng.randrange(10**6)}", f"w-{rng.randrange(10**6)}", (-600.0, 900.0))
                items = [*near, *far]
                rng.shuffle(items)
                world = MockWorld(items, PLAYERS, ())
                before = {i.id: i.position for i in world.snapshot()}
                result = Executor(world).execute(detach())
                self.assertEqual((result.status, result.reason), ("COMPLETE", "DETACH_OBSERVED"), result)
                self.assertEqual(result.states, ("FIND", "RESERVE", "DETACH", "VERIFY", "PULL_CLEAR", "VERIFY",
                                                 "STOP", "RELEASE"))
                self.assertEqual(set(result.equipment_ids), {near[0].id, near[1].id})
                after = {i.id: i for i in world.snapshot()}
                self.assertEqual((after[near[0].id].attached_to, after[near[1].id].attached_to), (None, None))
                self.assertEqual(after[far[0].id].attached_to, far[1].id)          # the other pair untouched
                # Only the detached tractor moved: straight ahead, until clear.
                self.assertEqual({i: a.position for i, a in after.items() if i != near[0].id},
                                 {i: p for i, p in before.items() if i != near[0].id})
                tractor = after[near[0].id]
                self.assertAlmostEqual(tractor.position[0], before[near[0].id][0])
                self.assertGreater(tractor.position[1], before[near[0].id][1])
                self.assertGreaterEqual(joint_separation(tractor, after[near[1].id]), PULL_CLEAR_M)
                self.assertTrue(all(a.reserved_by is None for a in after.values()))

    def test_a_named_kind_picks_that_implement_or_blocks(self):
        tractor, wagon = hitched_pair("t-1", "w-1", (130.0, 260.0))
        cultivator = replace(wagon, category="implement", kinds=frozenset({"cultivator"}))
        for implement, items, outcome in (("cultivator", [tractor, cultivator], ("COMPLETE", "DETACH_OBSERVED")),
                                          ("sprayer", [tractor, cultivator], ("BLOCKED", "NO_ATTACHED_IMPLEMENT")),
                                          ("forage_wagon", [tractor, wagon], ("COMPLETE", "DETACH_OBSERVED")),
                                          (None, [tractor, cultivator], ("COMPLETE", "DETACH_OBSERVED"))):
            with self.subTest(implement=implement):
                world = MockWorld(items, PLAYERS, ())
                result = Executor(world).execute(detach(implement))
                self.assertEqual((result.status, result.reason), outcome, result)
                if outcome[0] == "BLOCKED":
                    self.assertNotIn("RESERVE", result.states)

    def test_nothing_hitched_blocks_before_reserving(self):
        world = simple_world()
        result = Executor(world).execute(detach())
        self.assertEqual((result.status, result.reason), ("BLOCKED", "NO_ATTACHED_IMPLEMENT"))
        self.assertNotIn("RESERVE", result.states)

    def test_a_pull_clear_refused_before_moving_leaves_the_detach_standing(self):
        for code in ("PULL_CLEAR_BLOCKED", "PULL_CLEAR_PERSON", "SEATED_DURING_PULL_CLEAR"):
            with self.subTest(code=code):
                class Refusing(MockWorld):
                    def pull_clear(self, *args):
                        raise StepFailed(code)

                world = Refusing(list(hitched_pair("t-1", "w-1", (130.0, 260.0))), PLAYERS, ())
                before = {i.id: i.position for i in world.snapshot()}
                result = Executor(world).execute(detach())
                self.assertEqual((result.status, result.reason), ("COMPLETE", "DETACHED_NOT_CLEAR"), result)
                self.assertEqual({i.id: i.position for i in world.snapshot()}, before)
                self.assertEqual(world.items["t-1"].attached_to, None)
                self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

    def test_a_pull_clear_that_fails_or_is_not_observed_is_never_complete(self):
        class Stalled(MockWorld):
            def pull_clear(self, *args):
                raise StepFailed("STALLED")

        class Silent(MockWorld):
            def pull_clear(self, *args):
                return None                                     # reported clear, never moved

        for cls, outcome in ((Stalled, ("FAILED", "STALLED")), (Silent, ("UNVERIFIED", "PULL_CLEAR_NOT_OBSERVED"))):
            with self.subTest(world=cls.__name__):
                world = cls(list(hitched_pair("t-1", "w-1", (130.0, 260.0))), PLAYERS, ())
                result = Executor(world).execute(detach())
                self.assertEqual((result.status, result.reason), outcome, result)
                self.assertIn("STOP", result.states)
                self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

    def test_a_refused_or_unobserved_detach_is_never_complete(self):
        class Refusing(MockWorld):
            def detach(self, tractor_id, wagon_id, owner):
                raise StepFailed("DETACH_NOT_ALLOWED")          # GIANTS' isDetachAllowed said no

        class Silent(MockWorld):
            def detach(self, tractor_id, wagon_id, owner):
                return None                                     # reported done, still attached

        for cls, outcome in ((Refusing, ("FAILED", "DETACH_NOT_ALLOWED")),
                             (Silent, ("UNVERIFIED", "ATTACHMENT_MISMATCH"))):
            with self.subTest(world=cls.__name__):
                world = cls(list(hitched_pair("t-1", "w-1", (130.0, 260.0))), PLAYERS, ())
                result = Executor(world).execute(detach())
                self.assertEqual((result.status, result.reason), outcome, result)
                self.assertEqual(world.items["t-1"].attached_to, "w-1")
                self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))


class DetourTests(unittest.TestCase):
    """Live 2026-10-20: GIANTS' GOTO kept "driving" against something its map does not
    show. Without a gate to open, the job ends STUCK_ON_OBSTACLE and marks nothing
    (a mark next to a vehicle pressed against the fence only kept GIANTS' agent BLOCKED)."""

    def test_stuck_without_a_gate_ends_precisely_and_marks_nothing(self):
        tractor, wagon = simple_world().snapshot()
        tractor = replace(tractor, capabilities=GOTO_TRACTOR, position=(600.0, 600.0),
                          ai_root=Pose(600.0, 600.0, 0.0), heading=0.0)

        class Fence(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                raise StepFailed("NAVIGATE_STUCK")

        world = Fence([tractor, wagon], PLAYERS, DESTINATIONS)
        result = Executor(world).execute(intent())
        self.assertEqual((result.status, result.reason), ("FAILED", "STUCK_ON_OBSTACLE"), result)
        self.assertEqual(world.blocked, ())
        self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))


class GateTests(unittest.TestCase):
    def test_a_closed_gate_near_a_stuck_vehicle_is_opened_before_any_detour(self):
        tractor, wagon = simple_world().snapshot()
        tractor = replace(tractor, capabilities=GOTO_TRACTOR, position=(600.0, 600.0),
                          ai_root=Pose(600.0, 600.0, 0.0), heading=0.0)
        tries = []

        class Gate(MockWorld):
            def navigate(self, tractor_id, pose, owner):
                tries.append(1)
                if self.gates:
                    raise StepFailed("NAVIGATE_STUCK")
                return super().navigate(tractor_id, pose, owner)

        world = Gate([tractor, wagon], PLAYERS, DESTINATIONS)
        world.gates = ((603.0, 604.0),)
        result = Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual((world.opened, world.blocked), (((603.0, 604.0),), ()))
