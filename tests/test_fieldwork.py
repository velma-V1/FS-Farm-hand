"""field_work: GIANTS' field-work helper on an owned field, complete only on the
field's measured state (companion/field.py, companion/executor.py)."""
from dataclasses import replace
import math
import unittest
from unittest import mock

from adapters.world import StepFailed, StepUnverified
from companion import hitch as transport
from companion.executor import Executor
from companion.executor import COVERAGE_REQUIRED, OPERATIONS, resolve_field
from protocol.model import Destination, Equipment, FarmInfo, Field, FieldCoverage, FillUnit, InputJoint, OutputJoint, Pose
from simulator.world import MockWorld

PLAYERS = {"farm-a": (0.0, 0.0)}
STUBBLE = FieldCoverage(ground=(("HARVEST_READY", 0.2), ("STUBBLE", 0.8)), spray=(), total_m2=4000.0,
                        fruit=(("NONE", 9),), sampled=9)


def field(fid, area, coverage=STUBBLE, at=(200.0, 300.0)):
    return Field(fid, area, at, coverage, 0.0)


def rig(kind, level=0.0, at=(100.0, 100.0)):
    """Tractor t-1 with an implement of `kind` hitched behind it."""
    x, z = at
    tractor = Equipment("t-1", "tractor", "farm-a", (x, z), heading=0.0, ai_root=Pose(x, z, 0.0),
                        capabilities=frozenset({"move", "attach", "stop"}), attached_to="i-1",
                        kinds=frozenset({"ai_field_worker", "fill_unit"}),
                        output_joints=(OutputJoint(1, "implement", 0.0, -2.0, False),))
    role = {"sowing_machine": "seed", "sprayer": "spray"}.get(kind)
    units = (FillUnit(1, "SEEDS" if role == "seed" else "LIQUIDFERTILIZER", level, 4000.0, role=role),) if role else ()
    implement = Equipment("i-1", "implement", "farm-a", (x, z - 4), kinds=frozenset({kind, "ai_implement"}),
                          heading=0.0, attached_to="t-1", fill_units=units,
                          input_joints=(InputJoint(1, "implement", (x, z - 2.0), True),))
    return [tractor, implement]


def work(operation, fid=7, request_id="field-001", **extra):
    return {"version": 1, "request_id": request_id, "task": "field_work", "farm_id": "farm-a",
            "target": "player_at_acceptance", "field": fid, "operation": operation, **extra}


FARM = FarmInfo(10000.0, False, False, True)


class FieldResolutionTests(unittest.TestCase):
    def test_smallest_owned_field_or_by_id(self):
        fields = (field(9, 1.2), field(7, 0.4), field(3, 0.4))
        self.assertEqual(resolve_field("smallest", fields)[0].id, 3)      # ties by id
        self.assertEqual(resolve_field(9, fields)[0].id, 9)
        self.assertEqual(resolve_field(5, fields), (None, "FIELD_NOT_OWNED"))
        self.assertEqual(resolve_field("smallest", ()), (None, "NO_OWNED_FIELD"))

    def test_done_is_measured_on_the_field_not_a_job_end(self):
        cultivated = FieldCoverage((("CULTIVATED", 0.96), ("STUBBLE", 0.04)), ((1, 0.5),), 4000.0, None, 9)
        self.assertGreaterEqual(OPERATIONS["cultivate"].done(cultivated), COVERAGE_REQUIRED)
        self.assertEqual(OPERATIONS["plow"].done(cultivated), 0)
        self.assertEqual(OPERATIONS["fertilize"].done(cultivated), 0.5)


class FieldWorkTests(unittest.TestCase):
    def world(self, kind, level=0.0, coverage=STUBBLE, farm=FARM, cls=MockWorld):
        return cls(rig(kind, level), PLAYERS, fields=(field(7, 0.4, coverage), field(9, 1.2)), farm=farm)

    def test_the_helper_starts_where_giants_finds_a_route_and_without_one_goes_the_usual_way(self):
        # Live 2026-11-05 (save 1): GIANTS answered NOT_REACHABLE for the MF 8570 to field 90 and 64,
        # then Farmhand's own drive planned for minutes and drove the wrong way. GIANTS never checks
        # a field-work target (AIJobFieldWork:validate): Farmhand asks its planner first (REACH).
        outline = ((150.0, 250.0), (250.0, 250.0), (250.0, 350.0), (150.0, 350.0))
        place = Destination("field-7", (200.0, 300.0), "field 7", outline)
        world = self.world("cultivator")
        world.mock_field_places = (place,)
        world.mock_unreachable = ((200.0, 300.0),)                      # its centre
        result = Executor(world).execute(work("cultivate"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)
        self.assertEqual(world.mock_reached[0], (200.0, 300.0))
        self.assertEqual(len(world.mock_reached), 2)                    # the nearest corner's inside point next
        # None reachable: never refused for it; the helper's own drive, then Farmhand's (_to_field), as before.
        none = self.world("cultivator")
        none.mock_field_places = (place,)
        none.mock_unreachable = ((200.0, 300.0),) + tuple(world.mock_reached[1:]) + tuple(
            (c[0] + (200.0 - c[0]) * 10.0 / math.dist(c, (200.0, 300.0)),
             c[1] + (300.0 - c[1]) * 10.0 / math.dist(c, (200.0, 300.0))) for c in outline)
        result = Executor(none).execute(work("cultivate"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)

    def test_each_operation_completes_on_the_measured_field(self):
        for operation, kind, level in (("cultivate", "cultivator", 0.0), ("sow", "sowing_machine", 500.0),
                                       ("fertilize", "sprayer", 500.0)):
            with self.subTest(operation=operation):
                world = self.world(kind, level)
                result = Executor(world).execute(work(operation))
                self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)
                self.assertEqual(result.states, ("FIND", "RESERVE", "FIELD_WORK", "VERIFY", "STOP", "RELEASE"))
                self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

    def test_a_field_not_yet_measured_is_waited_for_before_the_work(self):
        # Live, right after a reload: "before: unmeasured", 395 m2 worked counted as 8934 m2.
        class Late(MockWorld):
            reads = 0

            def fields(self):
                self.reads += 1
                out = super().fields()
                return tuple(replace(f, coverage=None) for f in out) if self.reads <= 3 else out

        world = self.world("cultivator", cls=Late)
        with mock.patch("time.sleep"):
            result = Executor(world).execute(work("cultivate"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)
        self.assertGreater(world.reads, 3)

    def test_fieldwork_does_not_preemptively_teleport_nearby_equipment(self):
        # Live 2026-11-04: the field-2 scan moved a parked canola trailer even
        # though the helper had not reported that trailer as an obstruction.
        from protocol.model import Destination
        world = self.world("sprayer", 500.0)
        world.items["i-9"] = Equipment("i-9", "implement", "farm-a", (200.0, 300.0),
                                         kinds=frozenset({"trailer"}), heading=0.0)
        outline = ((150.0, 250.0), (250.0, 350.0), (250.0, 350.0), (250.0, 250.0))
        world.mock_field_places = (Destination("field-7", (200.0, 300.0), "Field 7", outline),)
        world.teleport_recovery = True
        teleports = []
        world.teleport = lambda *args, **kwargs: teleports.append(args)
        result = Executor(world).execute(work("fertilize"))
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(teleports, [])


    def test_without_farmhands_drive_a_field_the_helper_cannot_reach_says_so(self):
        # Live 5c8eeb68ccea4304-2: the 3650 + spreader 1 km from field 10, GIANTS' helper ended
        # NOT_REACHABLE at once. A world without Farmhand's own drive has nothing else to take
        # it there (AutoDrive installed or not is never asked): the failure stands.
        for caps in (frozenset({"move", "attach", "stop"}), frozenset({"move", "attach", "stop", "autodrive"})):
            with self.subTest(caps=caps):
                items = rig("sprayer", 500.0, at=(1100.0, 900.0))
                items[0] = replace(items[0], capabilities=caps)
                items[1] = replace(items[1], required_pto_torque=0.0)
                world = MockWorld(items, PLAYERS, fields=(field(7, 0.4), field(9, 1.2)), farm=FARM)
                world.mock_helper_reach_m = 150.0
                result = Executor(world).execute(work("fertilize"))
                self.assertEqual((result.status, result.reason), ("FAILED", "NOT_REACHABLE"), result)
                self.assertNotIn("TO_FIELD", result.states)

    def test_a_field_the_helper_will_not_drive_to_is_reached_by_farmhands_own_drive(self):
        # Live 9211b19abaa24cce-3: the combine at the barn, GIANTS' helper ended COULD_NOT_PREPARE
        # at once (never ready to drive). Farmhand drives the pair a few metres onto the field and
        # the helper starts there; NOT_REACHABLE is answered the same way, without AutoDrive.
        from protocol.model import Destination
        outline = ((150.0, 250.0), (250.0, 250.0), (250.0, 350.0), (150.0, 350.0))

        class Driving(MockWorld):
            refusal = "COULD_NOT_PREPARE"
            driven = None

            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                self.driven = target
                for unit, behind in (("t-1", 0.0), ("i-1", 4.0)):
                    self.items[unit] = replace(self.items[unit], position=(target[0], target[1] - behind))
                if yaw is not None:
                    self.items["t-1"] = replace(self.items["t-1"], heading=yaw)

            def fieldwork(self, tractor_id, field_id, pose, owner, area_m2=0.0, seed=0, hand=False):
                if math.dist(self.items[tractor_id].position, (200.0, 300.0)) > 150.0:
                    return self.refusal
                return super().fieldwork(tractor_id, field_id, pose, owner, area_m2, seed, hand)

        for refusal in ("COULD_NOT_PREPARE", "NOT_REACHABLE"):
            with self.subTest(refusal=refusal):
                world = Driving(rig("cultivator", at=(600.0, 900.0)), PLAYERS, fields=(field(7, 0.4),), farm=FARM)
                world.refusal = refusal
                world.mock_field_places = (Destination("field-7", (200.0, 300.0), "Field 7", outline),)
                result = Executor(world).execute(work("cultivate"))
                self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)
                self.assertIn("TO_FIELD", result.states)
                self.assertTrue(150.0 < world.driven[0] < 250.0 and 250.0 < world.driven[1] < 350.0)   # inside
                # 8 m in from the edge point nearest the rig (here the field's corner).
                self.assertAlmostEqual(math.dist(world.driven, (250.0, 350.0)), 8.0, places=6)

    def test_hemmed_in_by_parked_equipment_on_its_way_to_the_field_it_moves_it_and_goes_on(self):
        # Live fdc69c298fa64d99-1: the Series 9 just unloaded, the Deckover's ramps 4 m ahead.
        from protocol.model import Destination
        outline = ((150.0, 250.0), (250.0, 250.0), (250.0, 350.0), (150.0, 350.0))
        moved = []

        class Hemmed(MockWorld):
            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                if not moved:
                    raise StepFailed("NO_ROUTE")
                for unit, behind in (("t-1", 0.0), ("i-1", 4.0)):
                    self.items[unit] = replace(self.items[unit], position=(target[0], target[1] - behind))

            def reposition(self, vehicle_id, owner):
                pass

            def fieldwork(self, tractor_id, field_id, pose, owner, area_m2=0.0, seed=0, hand=False):
                if math.dist(self.items[tractor_id].position, (200.0, 300.0)) > 150.0:
                    return "NOT_REACHABLE"
                return super().fieldwork(tractor_id, field_id, pose, owner, area_m2, seed, hand)

        world = Hemmed(rig("cultivator", at=(600.0, 900.0)), PLAYERS, fields=(field(7, 0.4),), farm=FARM)
        world.mock_field_places = (Destination("field-7", (200.0, 300.0), "Field 7", outline),)
        world.items["g-1"] = Equipment("g-1", "tractor", "farm-a", (604.0, 904.0), heading=0.0, name="GMC",
                                       capabilities=frozenset({"move", "stop"}))
        def move_blockers(rig_ids, farm_id, owner):     # the GMC driven aside; the next drive gets through
            moved.append("g-1")
            return 1
        with mock.patch("companion.standalone.StandaloneMove._move_blockers", side_effect=move_blockers):
            result = Executor(world).execute(work("cultivate"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)
        self.assertEqual(moved, ["g-1"])

    def test_a_job_that_ended_without_covering_the_field_is_not_complete(self):
        class Partial(MockWorld):
            def fieldwork(self, tractor_id, field_id, pose, owner, area_m2=0.0):
                f = self.field_map[field_id]
                self.field_map[field_id] = replace(f, coverage=replace(
                    f.coverage, ground=(("CULTIVATED", 0.6), ("STUBBLE", 0.4))))
                return "FINISHED"                                  # GIANTS said finished

        class OutOfFill(Partial):
            def fieldwork(self, *args):
                super().fieldwork(*args)
                return "OUT_OF_FILL"

        for cls, reason in ((Partial, "PARTIAL_COVERAGE"), (OutOfFill, "OUT_OF_FILL")):
            with self.subTest(world=cls.__name__):
                world = self.world("cultivator", cls=cls)
                result = Executor(world).execute(work("cultivate"))
                self.assertEqual((result.status, result.reason), ("FAILED", reason), result)
                self.assertIn("STOP", result.states)

    def test_material_must_actually_go(self):
        class NoUse(MockWorld):
            def fieldwork(self, tractor_id, field_id, pose, owner, area_m2=0.0):
                f = self.field_map[field_id]
                self.field_map[field_id] = replace(f, coverage=replace(f.coverage, ground=(("SOWN", 1.0),)))
                return "FINISHED"                                  # sown, but no seed used

        result = Executor(self.world("sowing_machine", 500.0, cls=NoUse)).execute(work("sow"))
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "MATERIAL_USE_NOT_OBSERVED"))

    def test_an_empty_implement_blocks_before_anything_moves_unless_the_helper_buys(self):
        world = self.world("sowing_machine", 0.0)
        result = Executor(world).execute(work("sow"))
        self.assertEqual((result.status, result.reason), ("BLOCKED", "NEEDS_SEED"))
        self.assertNotIn("RESERVE", result.states)
        self.assertEqual(Executor(self.world("sprayer", 0.0)).execute(work("fertilize")).reason, "NEEDS_FERTILIZER")

        class Buying(MockWorld):
            def fieldwork(self, *args):
                code = super().fieldwork(*args)
                self.farm_info = replace(self.farm_info, money=self.farm_info.money - 80.0)   # PURCHASE_SEEDS
                return code

        world = self.world("sowing_machine", 0.0, farm=replace(FARM, helper_buy_seeds=True), cls=Buying)
        result = Executor(world).execute(work("sow"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)

    def test_wrong_or_missing_implement_or_field_blocks(self):
        self.assertEqual(Executor(self.world("cultivator")).execute(work("sow")).reason, "NO_ATTACHED_IMPLEMENT")
        self.assertEqual(Executor(self.world("cultivator")).execute(work("cultivate", fid=5)).reason,
                         "FIELD_NOT_OWNED")
        world = MockWorld(rig("cultivator"), PLAYERS)
        self.assertEqual(Executor(world).execute(work("cultivate", fid="smallest")).reason, "NO_OWNED_FIELD")

    def test_giants_refusing_to_start_is_failed(self):
        class Refusing(MockWorld):
            def fieldwork(self, *args):
                raise StepFailed("FIELD_NOT_OWNED")

        result = Executor(self.world("cultivator", cls=Refusing)).execute(work("cultivate"))
        self.assertEqual((result.status, result.reason), ("FAILED", "FIELD_NOT_OWNED"))
        self.assertTrue(all(i.reserved_by is None for i in result and self.world("cultivator").snapshot()))


if __name__ == "__main__":
    unittest.main()


class PlanTests(unittest.TestCase):
    """The planner's steps per operation and the coordinator running them in order."""

    def items(self, *kinds, hitched=None):
        tractor = Equipment("t-1", "tractor", "farm-a", (0.0, 0.0), kinds=frozenset({"ai_field_worker"}),
                            attached_to=None)
        out = [tractor]
        for n, kind in enumerate(kinds):
            out.append(Equipment(f"i-{n}", "implement", "farm-a", (10.0 * n, 5.0),
                                 kinds=frozenset({kind, "ai_implement"})))
        if hitched is not None:
            out[0] = replace(tractor, attached_to=f"i-{hitched}")
            out[hitched + 1] = replace(out[hitched + 1], attached_to="t-1")
        return tuple(out)

    def test_each_operation_hitches_works_and_unhitches(self):
        from companion.coordinator import field_steps
        items = self.items("cultivator", "sowing_machine")
        steps, note = field_steps(items, "sow", 7, "farm-a")
        self.assertEqual(steps, [{"task": "hitch_pair", "implement": "sowing_machine"},
                                 {"task": "field_work", "field": 7, "operation": "sow", "implement": "sowing_machine"},
                                 {"task": "move_pair", "implement": "sowing_machine", "target": {"type": "origin"}},
                                 {"task": "detach_pair", "implement": "sowing_machine"}])
        hitched = self.items("cultivator", "sowing_machine", hitched=1)
        self.assertEqual(field_steps(hitched, "sow", 7, "farm-a", area_m2=100)[0],
                         [{"task": "field_work", "field": 7, "operation": "sow", "implement": "sowing_machine",
                           "area_m2": 100}])
        # No plow on the farm: the cultivator does it, and says so.
        steps, note = field_steps(items, "plow", 7, "farm-a")
        self.assertEqual((steps[1]["operation"], steps[1]["implement"], note),
                         ("cultivate", "cultivator", "NO_PLOW_CULTIVATING"))
        self.assertEqual(field_steps(items, "fertilize", 7, "farm-a"), (None, "NO_IMPLEMENT_FOR_FERTILIZE"))

    def test_the_coordinator_runs_the_plan_and_stops_at_the_first_step_not_complete(self):
        from companion import coordinator as coordination
        from protocol.model import Result

        class World(MockWorld):
            cancel = None

            def retained(self):
                return ()

        world = World(list(self.items("cultivator", "sowing_machine", "sprayer")), PLAYERS,
                      fields=(field(9, 1.2), field(7, 0.4)), farm=FARM)
        ran = []

        class Scripted:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                ran.append((intent["task"], intent.get("operation"), intent.get("implement"), intent.get("field")))
                if intent["task"] == "field_work" and intent["operation"] == "fertilize":
                    return Result("FAILED", "OUT_OF_FILL", ("t-1",), ("FIND", "RESERVE", "FIELD_WORK"))
                return Result("COMPLETE", "OK", ("t-1",), ("FIND",))

        notices = []
        c = coordination.Coordinator(world, Scripted(), lambda n, s=False: notices.append(n), lambda: "farm-a",
                                     lambda: "s1")
        c.submit({"version": 1, "command": "field_work", "field": "smallest",
                  "operations": ["plow", "sow", "fertilize"]})
        c.wait(10)
        self.assertEqual(ran, [
            ("hitch_pair", None, "cultivator", None), ("field_work", "cultivate", "cultivator", 7),
            ("move_pair", None, "cultivator", None), ("detach_pair", None, "cultivator", None),
            ("hitch_pair", None, "sowing_machine", None), ("field_work", "sow", "sowing_machine", 7),
            ("move_pair", None, "sowing_machine", None), ("detach_pair", None, "sowing_machine", None),
            ("hitch_pair", None, "sprayer", None), ("field_work", "fertilize", "sprayer", 7)])
        self.assertEqual((c.last.state, c.last.reason), ("FAILED", "OUT_OF_FILL"))
        self.assertEqual(notices[0], "ACCEPTED_FIELD_WORK")
        self.assertEqual(notices[-1], "FAILED_OUT_OF_FILL")


    def test_each_step_waits_for_the_tractor_to_come_to_rest(self):
        # Live 5454f21977df465b-2: the tractor still rolled when the helper stopped,
        # and the detach right after found no pair at rest.
        from companion import coordinator as coordination
        from protocol.model import Result
        seen = []

        class Rolling(MockWorld):
            rolls = 0

            def retained(self):
                return ()

            def snapshot(self):
                items = super().snapshot()
                if self.rolls:
                    self.rolls -= 1
                    return tuple(replace(i, stopped=False) if i.category == "tractor" else i for i in items)
                return items

        world = Rolling(list(self.items("sowing_machine")), PLAYERS, fields=(field(7, 0.4),), farm=FARM)

        class Scripted:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                seen.append((intent["task"], world.rolls))
                if intent["task"] == "field_work":
                    world.rolls = 1
                return Result("COMPLETE", "OK", ("t-1",), ("FIND",))

        c = coordination.Coordinator(world, Scripted(), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        c.submit({"version": 1, "command": "field_work", "field": "smallest", "operations": ["sow"]})
        c.wait(20)
        self.assertEqual(seen, [("hitch_pair", 0), ("field_work", 0), ("move_pair", 0), ("detach_pair", 0)])
        self.assertEqual(c.last.state, "COMPLETE")


    def test_a_tractor_on_its_dropped_implements_joint_is_freed_and_the_plan_runs_again(self):
        # Live 6ccefa7bf9c34797-1: the 3650 stood on the cultivator it had dropped (PULL_CLEAR
        # refused for the player), the seeder's hitch BLOCKED AGAINST_IMPLEMENT and the sowing
        # ended there. Once a job: hitched back and unhitched clear, then the plan again.
        from companion import coordinator as coordination
        from protocol.model import Result
        class Settled(MockWorld):
            def retained(self):
                return ()

        items = list(self.items("sowing_machine"))
        items[0] = replace(items[0], name="3650")
        world = Settled(items, PLAYERS, fields=(field(7, 0.4),), farm=FARM)
        seen, freed = [], []

        class Scripted:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                seen.append(intent["task"])
                if intent["task"] == "hitch_pair" and seen.count("hitch_pair") == 1:
                    return Result("BLOCKED", "AGAINST_IMPLEMENT", ("t-1", "w-1"), ("FIND",))
                return Result("COMPLETE", "OK", ("t-1",), ("FIND",))

        c = coordination.Coordinator(world, Scripted(), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        def clear(job, cancel, entered, name):
            freed.append(name)
            return Result("COMPLETE", "DETACH_OBSERVED")
        c._clear_of_dropped_implement = clear
        c.submit({"version": 1, "command": "field_work", "field": "smallest", "operations": ["sow"]})
        c.wait(20)
        self.assertEqual(len(freed), 1)
        self.assertEqual(seen[:2], ["hitch_pair", "hitch_pair"])
        self.assertEqual(c.last.state, "COMPLETE")

        seen.clear(); freed.clear()
        c._clear_of_dropped_implement = lambda *a: (freed.append(a[-1]), Result("BLOCKED", "AGAINST_IMPLEMENT"))[1]
        c.submit({"version": 1, "command": "field_work", "field": "smallest", "operations": ["sow"]})
        c.wait(20)
        self.assertEqual((c.last.state, c.last.reason), ("BLOCKED", "AGAINST_IMPLEMENT"))
        self.assertEqual(len(freed), 1)

        seen.clear(); freed.clear()       # waited for the player in its way, in vain: said so
        c._clear_of_dropped_implement = lambda *a: (freed.append(a[-1]), Result(
            "COMPLETE", "DETACHED_NOT_CLEAR", ("t-1", "k-1"), ("DETACH", "PULL_CLEAR", "WAIT_PERSON")))[1]
        c.submit({"version": 1, "command": "field_work", "field": "smallest", "operations": ["sow"]})
        c.wait(20)
        self.assertEqual((c.last.state, c.last.reason), ("BLOCKED", "PERSON_IN_THE_WAY"))

class ReturnToOriginTests(unittest.TestCase):
    """The user: "you have to take the plow back to where you got it, you can't just
    leave it in the field". move_pair to origin goes back to the tractor's pose when
    this companion took the implement; no coordinates come from words."""

    def intent(self, request_id, **extra):
        return {"version": 1, "request_id": request_id, "farm_id": "farm-a", "task": "move_pair",
                "implement": "sowing_machine", "target": {"type": "origin"}, **extra}

    def test_the_pair_goes_back_to_where_it_was_taken_then_unhitches_there(self):
        tractor, seeder = rig("sowing_machine", 500.0, at=(120.0, 80.0))
        world = MockWorld([tractor, replace(seeder, required_pto_torque=0.0)], PLAYERS,
                          fields=(field(7, 0.4),), farm=FARM)
        executor = Executor(world)
        self.assertEqual(executor.execute(work("sow")).status, "COMPLETE")
        tractor = next(i for i in world.snapshot() if i.id == "t-1")
        self.assertGreater(math.dist(tractor.position, (120.0, 80.0)), 50)       # out on the field
        result = executor.execute(self.intent("back-1"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "DELIVERY_OBSERVED"), result)
        tractor = next(i for i in world.snapshot() if i.id == "t-1")
        self.assertLess(math.dist(tractor.position, (120.0, 80.0)), 1.0)
        detach = {"version": 1, "request_id": "off-1", "farm_id": "farm-a", "task": "detach_pair",
                  "target": "player_at_acceptance", "implement": "sowing_machine"}
        self.assertEqual(executor.execute(detach).status, "COMPLETE")
        # Unhitched where it came from, the origin is spent.
        self.assertEqual(executor.execute(self.intent("back-2")).reason, "NO_ORIGIN_KNOWN")

    def test_arrival_is_judged_once_the_pair_comes_to_rest(self):
        # Live 84f4df027cb445bc-3: at the pose, still rolling just after GOTO ended.
        class Rolling(MockWorld):
            rolls = 0

            def navigate(self, tractor_id, pose, owner):
                super().navigate(tractor_id, pose, owner)
                self.rolls = 2

            def snapshot(self):
                items = super().snapshot()
                if self.rolls:
                    self.rolls -= 1
                    return tuple(replace(i, stopped=False) if i.id == "t-1" else i for i in items)
                return items

        tractor, seeder = rig("sowing_machine", 500.0, at=(120.0, 80.0))
        world = Rolling([tractor, replace(seeder, required_pto_torque=0.0)], PLAYERS,
                        fields=(field(7, 0.4),), farm=FARM)
        executor = Executor(world)
        executor.execute(work("sow"))
        result = executor.execute(self.intent("back-1"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "DELIVERY_OBSERVED"), result)

    def test_a_pair_this_companion_did_not_take_is_not_moved(self):
        world = MockWorld(rig("sowing_machine", 500.0), PLAYERS, fields=(field(7, 0.4),), farm=FARM)
        result = Executor(world).execute(self.intent("back-1"))
        self.assertEqual((result.status, result.reason, result.states), ("BLOCKED", "NO_ORIGIN_KNOWN", ("FIND",)))

    def test_origin_is_only_a_move_pair_target(self):
        world = MockWorld(rig("sowing_machine", 500.0), PLAYERS, fields=(field(7, 0.4),), farm=FARM)
        result = Executor(world).execute({"version": 1, "request_id": "x-1", "farm_id": "farm-a",
                                          "task": "hitch_pair", "target": {"type": "origin"}})
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.states, ())


class BlockedAtStartTests(unittest.TestCase):
    def test_a_helper_blocked_where_it_stands_has_the_parked_truck_moved_and_starts_again(self):
        # Live 9f1e101ef8da48d7-1: the Series 9 unloaded behind the GMC's deck, GIANTS' helper
        # blocked; a worker would pull the truck away first.
        moved = []

        class Boxed(MockWorld):
            blocked = True

            def reserve_rig(self, ids, owner):
                return True

            def release(self, owner):
                if not owner.endswith("-clear0"):
                    return super().release(owner)

            def stop(self, vehicle_id, owner):
                return True if owner.endswith("-clear0") else super().stop(vehicle_id, owner)

            def reposition(self, vehicle_id, owner):
                moved.append(vehicle_id)
                v = self.items[vehicle_id]
                self.items[vehicle_id] = replace(v, position=(v.position[0] + 40.0, v.position[1]))
                self.blocked = False

            code = "FIELDWORK_BLOCKED_AT_START"

            def fieldwork(self, tractor_id, field_id, pose, owner, area_m2=0.0, seed=0, hand=False):
                if self.blocked:
                    raise StepFailed(self.code)
                return super().fieldwork(tractor_id, field_id, pose, owner, area_m2, seed, hand)

        # At its start, or on its way (live 5eb1005df58446f7-1: the GMC left on the road).
        for code in ("FIELDWORK_BLOCKED_AT_START", "FIELDWORK_BLOCKED"):
            with self.subTest(code=code):
                moved.clear()
                truck = Equipment("g-1", "tractor", "farm-a", (100.0, 108.0), heading=0.0,
                                  ai_root=Pose(100.0, 108.0, 0.0), capabilities=frozenset({"move", "attach", "stop"}))
                world = Boxed(rig("cultivator") + [truck], PLAYERS, fields=(field(7, 0.4),), farm=FARM)
                world.code = code
                result = Executor(world).execute(work("cultivate"))
                self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)
                self.assertEqual(moved, ["g-1"])


class StrappedOnTransportTests(unittest.TestCase):
    def test_a_pair_strapped_on_a_transport_is_never_sent_to_work(self):
        # Live 2026-10-31 (save 1): "plow field 3" took the Series 9 and its Titan plow belted on
        # the GMC's deckover 800 m away.
        from dataclasses import replace as _replace
        tractor, plow = rig("plow")
        world = MockWorld([_replace(tractor, belted_to="d-1"), _replace(plow, belted_to="d-1")], PLAYERS,
                          fields=(field(7, 0.4),), farm=FARM)
        before = world.snapshot()
        result = Executor(world).execute(work("plow"))
        self.assertEqual(result.status, "BLOCKED", result)
        self.assertEqual(world.snapshot(), before)


class StandingCropTests(unittest.TestCase):
    """Live FourFields field 1: 100 % HARVEST_READY wheat. Cultivating or sowing it
    would destroy the crop: BLOCKED before anything is hitched or moved."""
    READY = FieldCoverage(ground=(("HARVEST_READY", 1.0),), spray=(), total_m2=4000.0,
                          fruit=(("WHEAT", 5),), sampled=5)
    GROWING = FieldCoverage(ground=(("SOWN", 0.9), ("CULTIVATED", 0.1)), spray=(), total_m2=4000.0,
                            fruit=(("WHEAT", 5),), sampled=5)

    def test_soil_work_or_sowing_on_a_standing_crop_moves_nothing(self):
        for operation, kind, coverage, reason in (
                ("cultivate", "cultivator", self.READY, "CROP_READY_TO_HARVEST"),
                ("sow", "sowing_machine", self.READY, "CROP_READY_TO_HARVEST"),
                ("cultivate", "cultivator", self.GROWING, "CROP_GROWING")):
            with self.subTest(operation=operation, reason=reason):
                world = MockWorld(rig(kind, 500.0), PLAYERS, fields=(field(7, 0.4, coverage),), farm=FARM)
                before = world.snapshot()
                result = Executor(world).execute(work(operation))
                self.assertEqual((result.status, result.reason), ("BLOCKED", reason))
                self.assertEqual(world.snapshot(), before)

    def test_harvested_stubble_still_marked_ready_is_worked(self):
        # Live 2026-10-31 (save 1 field 3): every sample cut, the ground state still HARVEST_READY;
        # "plow field 3" was refused CROP_READY_TO_HARVEST. Only standing crop blocks.
        from companion.executor import OPERATIONS, crop_problem
        cut = FieldCoverage(ground=(("HARVEST_READY", 1.0),), spray=(), total_m2=4000.0,
                            fruit=(("WHEAT", 5),), sampled=5, cut=5)
        self.assertIsNone(crop_problem(OPERATIONS["plow"], field(7, 0.4, cut)))
        self.assertIsNone(crop_problem(OPERATIONS["sow"], field(7, 0.4, cut)))
        self.assertEqual(crop_problem(OPERATIONS["plow"], field(7, 0.4, self.READY)), "CROP_READY_TO_HARVEST")

    def test_the_plan_stops_before_hitching(self):
        from companion import coordinator as coordination
        ran = []

        class World(MockWorld):
            def retained(self):
                return ()

        class Scripted:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                ran.append(intent["task"])

        world = World(list(PlanTests().items("cultivator", "sowing_machine")), PLAYERS,
                      fields=(field(7, 0.4, self.READY),), farm=FARM)
        c = coordination.Coordinator(world, Scripted(), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["plow", "sow"]})
        c.wait(10)
        self.assertEqual((ran, c.last.state, c.last.reason), ([], "BLOCKED", "CROP_READY_TO_HARVEST"))


class OverCropTests(unittest.TestCase):
    def test_anyway_works_the_field_over_the_crop(self):
        from companion import coordinator as coordination
        from companion.language import RuleInterpreter
        from protocol.model import Result
        proposal = RuleInterpreter().interpret("cultivate a small area of field 7 anyway")
        self.assertTrue(proposal["over_crop"])
        ran = []

        class World(MockWorld):
            def retained(self):
                return ()

        class Scripted:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                ran.append((intent["task"], intent.get("over_crop")))
                return Result("COMPLETE", "OK", ("t-1",), ("FIND",))

        world = World(list(PlanTests().items("cultivator")), PLAYERS,
                      fields=(field(7, 0.4, StandingCropTests.READY),), farm=FARM)
        c = coordination.Coordinator(world, Scripted(), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        c.submit(proposal)
        c.wait(20)
        self.assertEqual(c.last.state, "COMPLETE")
        # A small area is reported as one, not as the whole field (live abf1610fc62543be-2).
        self.assertEqual(c.last.reason, "AREA_WORK_OBSERVED")
        self.assertIn(("field_work", True), ran)
        # The executor honours it too.
        world = MockWorld(rig("cultivator"), PLAYERS, fields=(field(7, 0.4, StandingCropTests.READY),), farm=FARM)
        self.assertEqual(Executor(world).execute(work("cultivate", over_crop=True)).status, "COMPLETE")


class TestAreaTests(unittest.TestCase):
    """A bounded test area: the mod stops the helper once that much of the field
    changed; complete only when the operation's own measured share grew by it."""

    def world(self, kind, level=0.0, cls=MockWorld):
        return cls(rig(kind, level), PLAYERS, fields=(field(7, 0.4),), farm=FARM)

    def test_a_small_area_is_worked_and_verified_on_the_field(self):
        for operation, kind, level in (("cultivate", "cultivator", 0.0), ("sow", "sowing_machine", 500.0),
                                       ("fertilize", "sprayer", 500.0)):
            with self.subTest(operation=operation):
                world = self.world(kind, level)
                result = Executor(world).execute(work(operation, area_m2=400))
                self.assertEqual((result.status, result.reason), ("COMPLETE", "AREA_WORK_OBSERVED"), result)
                worked = OPERATIONS[operation].done(world.field_map[7].coverage) * 4000.0
                self.assertGreaterEqual(worked, 400 * COVERAGE_REQUIRED)
                self.assertLess(worked, 4000 * COVERAGE_REQUIRED)               # not the whole field

    def test_less_than_the_asked_area_is_not_complete(self):
        class Short(MockWorld):
            def fieldwork(self, tractor_id, field_id, pose, owner, area_m2=0.0):
                return super().fieldwork(tractor_id, field_id, pose, owner, area_m2 / 2)   # stopped early

        result = Executor(self.world("cultivator", cls=Short)).execute(work("cultivate", area_m2=400))
        self.assertEqual((result.status, result.reason), ("FAILED", "PARTIAL_COVERAGE"), result)

    def test_the_words_ask_for_a_test_area(self):
        from companion.language import RuleInterpreter, TEST_AREA_M2
        rules = RuleInterpreter()
        self.assertEqual(rules.interpret("plow, seed and fertilize a small area of the smallest field"),
                         {"version": 1, "command": "field_work", "field": "smallest",
                          "operations": ["plow", "sow", "fertilize"], "area_m2": TEST_AREA_M2})
        self.assertEqual(rules.interpret("cultivate 500 square meters of field 7")["area_m2"], 500)
        self.assertIsNone(rules.interpret("seed 10 m2 of field 2"))                   # below the schema's 50 m2


class AlreadyDoneTests(unittest.TestCase):
    """Live (6c74538): every owned field was already 100 % CULTIVATED."""

    def test_an_operation_the_field_already_shows_moves_nothing(self):
        cultivated = FieldCoverage((("CULTIVATED", 1.0),), (), 21907.0, (("NONE", 9),), 9)
        world = MockWorld(rig("cultivator"), PLAYERS, fields=(field(3, 0.54, cultivated),), farm=FARM)
        before = {i.id: i.position for i in world.snapshot()}
        for extra in ({}, {"area_m2": 400}):
            result = Executor(world).execute(work("cultivate", fid=3, request_id=f"done-{len(extra)}", **extra))
            self.assertEqual((result.status, result.reason), ("COMPLETE", "ALREADY_DONE"), result)
            self.assertNotIn("RESERVE", result.states)
        self.assertEqual({i.id: i.position for i in world.snapshot()}, before)
        # Not done: sowing that same field still has work.
        from companion.executor import already_done
        self.assertFalse(already_done(OPERATIONS["sow"], field(3, 0.54, cultivated), 400))


class FieldDetourTests(unittest.TestCase):
    def test_field_work_stuck_at_a_closed_gate_opens_it_and_goes_on(self):
        class Gate(MockWorld):
            def fieldwork(self, *args):
                if self.gates:
                    raise StepFailed("FIELDWORK_STUCK")
                return super().fieldwork(*args)

        world = Gate(rig("cultivator"), PLAYERS, fields=(field(7, 0.4),), farm=FARM)
        world.gates = ((103.0, 104.0),)
        result = Executor(world).execute(work("cultivate", area_m2=400))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "AREA_WORK_OBSERVED"), result)
        self.assertEqual(world.opened, ((103.0, 104.0),))

    def test_field_work_stuck_on_a_fence_ends_precisely(self):
        # Live 2026-10-20: GIANTS' helper pressed against a fence beside field 3.
        class Fence(MockWorld):
            def fieldwork(self, *args):
                raise StepFailed("FIELDWORK_STUCK")

        world = Fence(rig("cultivator"), PLAYERS, fields=(field(7, 0.4),), farm=FARM)
        result = Executor(world).execute(work("cultivate", area_m2=400))
        self.assertEqual((result.status, result.reason), ("FAILED", "STUCK_ON_OBSTACLE"), result)
        self.assertEqual(world.blocked, ())


class CropTests(unittest.TestCase):
    """"Plant barley on field 7": the player says what, the seeder's own crop list says
    whether it can, GIANTS' seeder is set to it, and the field shows it afterwards."""
    SEEDS = ("WHEAT", "BARLEY", "CANOLA", "MAIZE", "SUGARBEET", "SOYBEAN", "OAT", "POTATO")
    READY = FieldCoverage((("CULTIVATED", 1.0),), (), 4000.0, (("NONE", 9),), 9)

    def seeded(self, seeds, at=(100.0, 100.0)):
        tractor, seeder = rig("sowing_machine", 500.0, at)
        return [tractor, replace(seeder, seeds=tuple(seeds))]

    def test_the_players_words_name_the_seeders_own_crops(self):
        from companion.executor import seed_position
        seeder = replace(rig("sowing_machine")[1], seeds=self.SEEDS)
        for words, crop in (("wheat", "WHEAT"), ("corn", "MAIZE"), ("sugar beets", "SUGARBEET"),
                            ("soybeans", "SOYBEAN"), ("oats", "OAT"), ("potatoes", "POTATO"), ("rapeseed", "CANOLA"),
                            ("Barley", "BARLEY")):
            with self.subTest(words=words):
                self.assertEqual(self.SEEDS[seed_position(seeder, words) - 1], crop)
        self.assertIsNone(seed_position(seeder, "rice"))
        self.assertIsNone(seed_position(replace(seeder, seeds=()), "wheat"))
        self.assertIsNone(seed_position(seeder, None))

    def test_the_seeder_is_set_to_the_crop_and_the_field_shows_it(self):
        for seeds in (self.SEEDS, tuple(reversed(self.SEEDS))):
            with self.subTest(order=seeds[0]):
                world = MockWorld(self.seeded(seeds), PLAYERS, fields=(field(7, 0.4, self.READY),), farm=FARM)
                result = Executor(world).execute(work("sow", crop="barley"))
                self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)
                self.assertEqual(dict(world.field_map[7].coverage.fruit), {"BARLEY": 9})

    def test_a_seeder_that_cannot_sow_it_is_never_taken(self):
        world = MockWorld(self.seeded(("WHEAT", "OAT")), PLAYERS, fields=(field(7, 0.4, self.READY),), farm=FARM)
        result = Executor(world).execute(work("sow", crop="canola"))
        self.assertEqual(result.status, "BLOCKED", result)
        self.assertNotIn("FIELD_WORK", result.states)
        self.assertEqual(world.field_map[7].coverage, self.READY)

    def test_another_crop_on_the_field_afterwards_is_not_complete(self):
        class Ignores(MockWorld):
            def fieldwork(self, tractor_id, field_id, pose, owner, area_m2=0.0, seed=0):
                return super().fieldwork(tractor_id, field_id, pose, owner, area_m2)    # the seeder's first crop

        world = Ignores(self.seeded(("WHEAT", "BARLEY")), PLAYERS, fields=(field(7, 0.4, self.READY),), farm=FARM)
        result = Executor(world).execute(work("sow", crop="barley"))
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "CROP_NOT_OBSERVED"), result)

    def test_the_plan_takes_a_seeder_that_can_sow_it(self):
        from companion.coordinator import field_steps
        tractor = Equipment("t-1", "tractor", "farm-a", (0.0, 0.0), kinds=frozenset({"ai_field_worker"}))
        oats = Equipment("i-0", "implement", "farm-a", (5.0, 5.0), kinds=frozenset({"sowing_machine", "ai_implement"}),
                         seeds=("OAT",), attached_to="t-1")
        canola = replace(oats, id="i-1", seeds=("WHEAT", "CANOLA"), attached_to=None, position=(15.0, 5.0))
        items = (replace(tractor, attached_to="i-0"), oats, canola)
        steps, note = field_steps(items, "sow", 7, "farm-a", crop="canola")
        self.assertEqual(steps[:2], [{"task": "hitch_pair", "implement": "sowing_machine", "crop": "canola"},
                                     {"task": "field_work", "field": 7, "operation": "sow",
                                      "implement": "sowing_machine", "crop": "canola"}])
        self.assertEqual(field_steps(items, "sow", 7, "farm-a", crop="oats")[0][0]["task"], "field_work")
        self.assertEqual(field_steps(items, "sow", 7, "farm-a", crop="rice"), (None, "NO_SEEDER_FOR_CROP"))
        # The crop is for sowing only: cultivating asks for a cultivator, not a seeder.
        self.assertEqual(field_steps(items, "cultivate", 7, "farm-a", crop="rice"), (None, "NO_IMPLEMENT_FOR_CULTIVATE"))

    def test_plant_prepares_the_ground_then_sows_what_was_said(self):
        from companion.language import RuleInterpreter
        r = RuleInterpreter()
        for words, field_id, operations, crop in (
                ("plant wheat on field 3", 3, ["cultivate", "sow"], "wheat"),
                ("Sow sugar beets in field 12", 12, ["cultivate", "sow"], "sugar beets"),
                ("plant field 5 with corn", 5, ["cultivate", "sow"], "corn"),
                ("plow and plant canola on field 4", 4, ["plow", "sow"], "canola")):
            with self.subTest(words=words):
                self.assertEqual(r.interpret(words), {"version": 1, "command": "field_work", "field": field_id,
                                                      "operations": operations, "crop": crop})
        self.assertEqual(r.interpret("sow field 3"), {"version": 1, "command": "field_work", "field": 3,
                                                      "operations": ["sow"]})          # as the seeder is set

    def test_the_coordinator_skips_ready_ground_and_sows_the_crop(self):
        from companion import coordinator as coordination
        from protocol.model import Result

        class World(MockWorld):
            cancel = None

            def retained(self):
                return ()

        items = [*self.seeded(("WHEAT", "BARLEY"), at=(30.0, 0.0)),
                 Equipment("i-9", "implement", "farm-a", (60.0, 0.0), kinds=frozenset({"cultivator", "ai_implement"}))]
        items[0] = replace(items[0], attached_to=None)
        items[1] = replace(items[1], attached_to=None)
        world = World(items, PLAYERS, fields=(field(7, 0.4, self.READY),), farm=FARM)
        ran = []

        class Scripted:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                ran.append((intent["task"], intent.get("operation"), intent.get("crop")))
                return Result("COMPLETE", "OK", ("t-1",), ("FIND",))

        c = coordination.Coordinator(world, Scripted(), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["cultivate", "sow"],
                  "crop": "barley"})
        c.wait(10)
        self.assertEqual(ran, [("hitch_pair", None, "barley"), ("field_work", "sow", "barley"),
                               ("move_pair", None, None), ("detach_pair", None, None)])
        self.assertEqual(c.last.state, "COMPLETE")


class ContractTests(unittest.TestCase):
    """"Do a contract": GIANTS' field contracts taken as the menu takes them, the field
    worked by the field-work plan, collected once GIANTS finished it, complete only when
    the reward is on the farm's balance."""

    def world(self, contracts, kind="plow"):
        from protocol.model import Contract

        class World(MockWorld):
            cancel = None

            def retained(self):
                return ()

        tractor, plow = rig(kind, at=(120.0, 80.0))
        world = World([tractor, replace(plow, required_pto_torque=0.0)], PLAYERS, fields=(field(7, 0.4),), farm=FARM)
        world.mock_contracts = tuple(Contract(*c) for c in contracts)
        world.mock_contract_fields = {c[0]: field(c[0], 1.5, at=(400.0 + c[0], 500.0)) for c in contracts}
        return world

    OFFERS = ((20, "bale", "CREATED", False, 9000.0),          # not a contract Farmhand does
              (31, "fertilize", "CREATED", False, 5000.0),     # no sprayer on the farm
              (21, "plow", "CREATED", False, 3000.0),
              (22, "plow", "CREATED", False, 2000.0))

    def test_the_best_paid_contract_the_farm_can_do_is_taken_worked_and_paid(self):
        for offers in (self.OFFERS, tuple(reversed(self.OFFERS))):
            with self.subTest(first=offers[0][0]):
                world = self.world(offers)
                c, notices = coordinated(world)
                c.submit({"version": 1, "command": "contract"})
                c.wait(30)
                self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "CONTRACT_PAID"), c.last)
                self.assertEqual(world.farm().money, FARM.money + 3000.0)
                self.assertEqual({x.field for x in world.contracts()}, {20, 31, 22})   # the rest still offered
                self.assertEqual(world._worked, {21})                                  # only its field worked
                self.assertNotIn(21, {f.id for f in world.fields()})          # collected: no longer ours
                self.assertEqual((notices[0], notices[-1]), ("ACCEPTED_CONTRACT", "COMPLETE_CONTRACT"))
                self.assertEqual(c.kept, None)

    def test_the_field_or_kind_said_picks_that_contract(self):
        world = self.world(self.OFFERS)
        c, _ = coordinated(world)
        c.submit({"version": 1, "command": "contract", "field": 22})
        c.wait(30)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "CONTRACT_PAID"))
        self.assertEqual(world.farm().money, FARM.money + 2000.0)

    def test_a_harvest_contract_is_taken_only_for_a_crop_a_farm_header_cuts(self):
        # Live 2026-11-05, save 1: field 9's harvest contract was spinach; the farm's only header
        # (the MF 8570's) cuts grain. Taken, it could never be done.
        from companion.coordinator import choose_contract
        from protocol.model import Contract
        header = Equipment("h-1", "implement", "farm-a", (0.0, 0.0), kinds=frozenset({"cutter", "ai_implement"}),
                           cuts=("WHEAT", "BARLEY", "CANOLA", "OAT"))
        combine = Equipment("c-1", "harvester", "farm-a", (0.0, 0.0), kinds=frozenset({"combine"}), attached_to="h-1")
        spinach = Contract(9, "harvestmission", "CREATED", False, 2280.0, fruit="SPINACH")
        oats = Contract(10, "harvestmission", "CREATED", False, 2000.0, fruit="OAT")
        unknown = Contract(11, "harvestmission", "CREATED", False, 9000.0)
        items = (header, combine)
        self.assertEqual(choose_contract((spinach,), items, "farm-a"), (None, "NO_HEADER_FOR_THE_CROP"))
        self.assertEqual(choose_contract((unknown,), items, "farm-a"), (None, "NO_HEADER_FOR_THE_CROP"))
        self.assertEqual(choose_contract((spinach, oats, unknown), items, "farm-a")[0], oats)

    def test_nothing_is_taken_or_moved_when_no_contract_fits(self):
        for work, reason in (({"field": 20}, "CONTRACT_NOT_SUPPORTED"), ({"field": 31}, "NO_IMPLEMENT_FOR_FERTILIZE"),
                             ({"field": 40}, "NO_CONTRACT_ON_FIELD"), ({"kind": "mow"}, "NO_CONTRACT")):
            with self.subTest(reason=reason):
                world = self.world(self.OFFERS)
                c, _ = coordinated(world)
                c.submit({"version": 1, "command": "contract", **work})
                c.wait(30)
                self.assertEqual((c.last.state, c.last.reason), ("BLOCKED", reason))
                self.assertFalse(any(x.mine for x in world.contracts()))
                self.assertEqual(next(i for i in world.snapshot() if i.id == "t-1").position, (120.0, 80.0))

    def test_not_yet_finished_by_the_game_is_kept_and_continue_only_collects(self):
        class Slow(type(self.world(()))):
            finished = False

            def contracts(self):
                found = super().contracts()
                if not self.finished:
                    found = tuple(replace(x, status="RUNNING") if x.mine else x for x in found)
                    self.mock_contracts = found
                return found

        world = self.world(((21, "plow", "CREATED", False, 3000.0),))
        world.__class__ = Slow
        c, _ = coordinated(world)
        c.submit({"version": 1, "command": "contract"})
        c.wait(30)
        self.assertEqual((c.last.state, c.last.reason), ("UNVERIFIED", "CONTRACT_NOT_FINISHED"))
        self.assertEqual(c.kept.command, "contract")
        worked = world._worked
        world.finished = True
        c.submit({"version": 1, "command": "resume"})
        c.wait(30)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "CONTRACT_PAID"))
        self.assertEqual(c.last.states, ("COLLECT",))                       # nothing worked twice
        self.assertEqual(world._worked, worked)
        self.assertEqual(world.farm().money, FARM.money + 3000.0)

    def test_the_words(self):
        from companion.language import RuleInterpreter
        r = RuleInterpreter()
        for words, extra in (("do a contract", {}), ("take the best paying contract", {}),
                             ("do the plow contract on field 12", {"kind": "plow", "field": 12}),
                             ("collect the contract", {}), ("take a cultivating contract", {"kind": "cultiv"})):
            with self.subTest(words=words):
                self.assertEqual(r.interpret(words), {"version": 1, "command": "contract", **extra})
        self.assertIsNone(r.interpret("do you have a contract"))
        self.assertIsNone(r.interpret("dont do a contract"))


class CoordinatedWorld(MockWorld):
    """The mock as the coordinator sees the bridge: reservations it can list, never a
    worker's recovered as a stale one."""
    cancel = None

    def retained(self):
        return tuple(self._reservations)

    def recover(self, owner):
        raise AssertionError(f"a worker's reservation was recovered: {owner}")

    def reservation(self, owner):
        r = self._reservations.get(owner)
        return None if r is None else r[1]


def coordinated(world):
    """A coordinator over the real executor, its watch and settling quick."""
    from companion import coordinator as coordination
    notices = []
    c = coordination.Coordinator(world, Executor(world), lambda n, s=False: notices.append(n), lambda: "farm-a",
                                 lambda: "s1")
    c.WATCH_S = 0.02
    c.SETTLE_S = 0.5
    c.CONTRACT_WAIT_S = 2.0
    # The stationary paths (a combine full, paused, emptied standing) are tested without the grain
    # cart that otherwise follows each harvest (GrainCartTests turn it on).
    c.grain_cart = False
    return c, notices


def until(check, seconds=10.0):
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


class WorkerTests(unittest.TestCase):
    """The user (2026-10-21): "farm hand needs to be able to leave workers doing field work
    and do something else like start the seeder behind the plow tractor". Field work is
    handed to GIANTS' helper; the job is done once it works, the pair stays reserved for
    it, and once the helper ends the field is measured and the equipment comes back."""

    def world(self, *kinds, coverage=STUBBLE):
        items = []
        for n, kind in enumerate(kinds):
            tractor, implement = rig(kind, 500.0, at=(100.0 + 40 * n, 100.0))
            items += [replace(tractor, id=f"t-{n + 1}", attached_to=f"i-{n + 1}"),
                      replace(implement, id=f"i-{n + 1}", attached_to=f"t-{n + 1}", required_pto_torque=0.0)]
        return CoordinatedWorld(items, PLAYERS, fields=(field(7, 0.4, coverage),), farm=FARM)

    def unit(self, world, unit_id):
        return next(i for i in world.snapshot() if i.id == unit_id)

    def test_a_later_obstruction_can_trigger_a_new_clearance(self):
        c, _ = coordinated(self.world("plow"))
        c.WORKER_BLOCKED_S = 0.0
        self.assertTrue(c._blocked_long("worker-a", True))
        self.assertFalse(c._blocked_long("worker-a", True))   # same blockage: no loop
        self.assertFalse(c._blocked_long("worker-a", False))  # moved and working again
        self.assertTrue(c._blocked_long("worker-a", True))    # a different blockage later
        c._resume_progress[(7, "plow")] = (0.2, 2)
        c.stop()
        self.assertFalse(c._resume_progress)                  # a new objective starts with a clean retry state

    def test_recoverable_stopped_worker_resumes_remaining_work_without_early_cleanup(self):
        from companion.coordinator import Worker
        world = self.world("plow")
        world.items["t-1"] = replace(world.items["t-1"], ai_stop="BLOCKED_BY_OBJECT")
        c, notices = coordinated(world)
        w = Worker("worker-a", "t-1", 7, "plow", [{"task": "detach_pair"}])
        with mock.patch.object(world, "stop", return_value=True), mock.patch.object(world, "release"):
            c.workers.append(w)
            c._worker_ended(w)
            self.assertEqual([x["job"] for x in c._after], ["clear_worker", "field_work"])
            self.assertEqual(c._after[1]["work"]["operations"], ["plow"])
            self.assertNotIn([{"task": "detach_pair"}], c._after)
            c._after.clear()
            c.workers.append(w)
            c._worker_ended(w)
            self.assertEqual([x["job"] for x in c._after], ["clear_worker", "field_work"])
            c._after.clear()
            c.workers.append(w)
            c._worker_ended(w)
            self.assertEqual([x["job"] for x in c._after], ["clear_worker", "field_work"])
            c._after.clear()
            c.workers.append(w)
            c._worker_ended(w)
        self.assertNotIn("field_work", [x.get("job") for x in c._after if isinstance(x, dict)])
        self.assertIn("WORKER_STOPPED", notices)
        # A road-planner failure does not authorize moving unrelated parked equipment.
        c._after.clear()
        c._resume_progress.clear()
        world.items["t-1"] = replace(world.items["t-1"], ai_stop="NOT_REACHABLE")
        with mock.patch.object(world, "stop", return_value=True), mock.patch.object(world, "release"):
            c.workers.append(w)
            c._worker_ended(w)
        self.assertEqual([x["job"] for x in c._after], ["field_work"])

    def test_a_worker_ending_after_stop_queues_nothing(self):
        from companion.coordinator import Worker
        world = self.world("plow")
        world.items["t-1"] = replace(world.items["t-1"], ai_stop="BLOCKED_BY_OBJECT")
        c, _ = coordinated(world)
        w = Worker("worker-a", "t-1", 7, "plow", [{"task": "detach_pair"}])
        # STOP emptied the worker list while the watch was still measuring this worker.
        with mock.patch.object(world, "stop", return_value=True), mock.patch.object(world, "release"):
            c._worker_ended(w)
        self.assertEqual(c._after, [])

    def test_worker_watch_survives_a_stale_game_snapshot(self):
        from companion.coordinator import Coordinator

        class World:
            reads = 0

            def snapshot(self):
                self.reads += 1
                if self.reads == 1:
                    raise StepUnverified("STALE_SNAPSHOT")
                return ()

        world = World()
        coordinator = Coordinator(world, None, lambda *_: None, lambda: "farm-a", lambda: "s1")
        coordinator.WATCH_S = 0
        coordinator._watch()
        self.assertEqual(world.reads, 2)

        world.reads = 1
        reads = [0]

        def farm():
            reads[0] += 1
            if reads[0] == 1:
                raise StepUnverified("STALE_SNAPSHOT")
            return "farm-a"

        coordinator.farm = farm
        coordinator._watch()
        self.assertEqual(reads[0], 2)

    def test_the_helper_works_on_and_its_equipment_comes_back_once_it_is_done(self):
        world = self.world("plow")
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["plow"]})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"))
        self.assertEqual(notices[-1], "WORKERS_STARTED")
        self.assertEqual([w.tractor for w in c.workers], ["t-1"])
        self.assertIsNotNone(self.unit(world, "t-1").reserved_by)                # kept for the helper
        self.assertEqual(c.status(), "STATUS_HELPERS_WORKING")
        world.finish_worker("t-1")
        self.assertTrue(until(lambda: not c.workers and c.last.command == "worker_return"
                                   and c.last.state == "COMPLETE"))
        self.assertIn("WORKER_DONE", notices)
        self.assertEqual(notices[-1], "COMPLETE_RETURN")
        self.assertGreaterEqual(dict(world.field_map[7].coverage.ground)["PLOWED"], COVERAGE_REQUIRED)
        tractor, plow = self.unit(world, "t-1"), self.unit(world, "i-1")
        self.assertLess(math.dist(tractor.position, (100.0, 100.0)), 10.0)      # back (then pulled clear)
        self.assertIsNone(tractor.attached_to)
        self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

    def test_farmhand_does_the_next_job_while_the_helper_works(self):
        world = self.world("plow", "sowing_machine")
        c, _ = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["plow"]})
        c.wait(10)
        # Another job meanwhile: the busy rig is never taken; the other one moves.
        c.submit({"version": 1, "command": "move_pair", "implement": "sowing_machine", "target": {"type": "origin"}})
        c.wait(10)
        self.assertNotEqual(c.last.reason, "INTERNAL_ERROR")
        self.assertEqual([w.tractor for w in c.workers], ["t-1"])
        self.assertTrue(self.unit(world, "t-1").busy)

    def test_the_seeder_starts_behind_the_plow_once_the_plow_is_ahead(self):
        world = self.world("plow", "sowing_machine")
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["plow", "sow"]})
        self.assertTrue(until(lambda: c.workers))
        self.assertTrue(until(lambda: c.current is not None and c.current.step == "WAIT_AHEAD"))
        self.assertEqual([w.tractor for w in c.workers], ["t-1"])                 # not ahead of the plow
        f = world.field_map[7]
        world.field_map[7] = replace(f, coverage=replace(f.coverage, ground=(("PLOWED", 0.4), ("STUBBLE", 0.6))))
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"))
        self.assertEqual(sorted(w.tractor for w in c.workers), ["t-1", "t-2"])
        self.assertEqual(notices.count("WORKER_STARTED"), 2)

    def test_an_implement_unreachable_where_it_stands_is_teleported_to_the_field_and_hitched(self):
        # The user, 2026-10-31: the seeder's drawbar against a tree ("can farmhand teleport it to
        # the field when stuck?"). Once, when teleport recovery is on, to the field's edge.
        from companion.executor import Executor as _E
        from protocol.model import Destination, Result as _R
        world = self.world("plow")
        seeder = Equipment("i-9", "implement", "farm-a", (160.0, 100.0), kinds=frozenset({"sowing_machine", "ai_implement"}),
                           heading=0.0, fill_units=(FillUnit(1, "SEEDS", 500.0, 4000.0, role="seed"),),
                           input_joints=(InputJoint(1, "implement", (160.0, 102.0), True),), required_pto_torque=0.0)
        world.items["i-9"] = seeder
        world.items["t-2"] = replace(world.items["t-1"], id="t-2", position=(170.0, 120.0), attached_to=None,
                                     ai_root=Pose(170.0, 120.0, 0.0),
                                     output_joints=(OutputJoint(1, "implement", 0.0, -2.0, True),))
        outline = ((150.0, 250.0), (250.0, 250.0), (250.0, 350.0), (150.0, 350.0))
        world.mock_field_places = (Destination("field-7", (200.0, 300.0), "Field 7", outline),)
        teleports = []
        world.teleport_recovery = True
        world.reserve_rig = lambda ids, owner, loose=False: loose and ids == ("i-9",)
        world.release = (lambda release: lambda owner: None if owner.endswith("-tp") else release(owner))(world.release)

        def teleport(vehicle_id, owner, min_m=0.0, at=None):
            teleports.append((vehicle_id, (at.x, at.z)))
            world.items["i-9"] = replace(world.items["i-9"], position=(at.x, at.z),
                                         input_joints=(InputJoint(1, "implement", (at.x, at.z + 2.0), True),))
        world.teleport = teleport
        c, notices = coordinated(world)
        real = c.executor.execute
        refused = []

        def execute(intent, **kw):
            if intent["task"] == "hitch_pair" and intent.get("implement") == "sowing_machine" and not teleports:
                refused.append(intent["request_id"])
                return _R("FAILED", "LOCAL_PATH_OBSTRUCTED", ("t-2", "i-9"), ("FIND", "RESERVE"))
            return real(intent, **kw)
        c.executor.execute = execute
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["sow"]})
        c.wait(15)
        self.assertEqual(len(refused), 1)
        self.assertEqual(teleports, [("i-9", (160.0, 250.0))])                  # the edge nearest it
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"), c.last)

    def test_a_worker_blocked_by_a_dropped_implement_has_it_teleported_aside(self):
        # Live 2026-10-31: the seeder's worker stopped at field 3's corner by the implement Farmhand
        # dropped there to free the MT600; GIANTS' helper waits for an obstacle it knows.
        world = self.world("plow")
        world.items["i-7"] = Equipment("i-7", "implement", "farm-a", (104.0, 96.0), kinds=frozenset({"cultivator"}),
                                       heading=0.0)
        moved = []
        world.teleport_recovery = True
        world.reserve_rig = lambda ids, owner, loose=False: loose and ids == ("i-7",)
        world.release = (lambda release: lambda owner: None if "-clear" in owner else release(owner))(world.release)
        world.teleport = lambda vehicle_id, owner, min_m=0.0, at=None: moved.append((vehicle_id, round(math.dist(
            (at.x, at.z), (104.0, 96.0)))))
        c, notices = coordinated(world)
        c.WORKER_BLOCKED_S = 0.0
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["plow"]})
        c.wait(10)
        self.assertEqual([w.tractor for w in c.workers], ["t-1"])
        world.items["t-1"] = replace(world.items["t-1"], ai_blocked=True)
        self.assertTrue(until(lambda: moved, 15))
        self.assertEqual(moved, [("i-7", 40)])                                 # 40 m away from the rig
        self.assertIn("WORKER_BLOCKED", notices)

    def test_loose_implements_on_the_field_are_set_down_beside_it_before_the_hand_off(self):
        # The user, 2026-10-31: "when farmhand hands off the controls to giant worker it needs to scan
        # the field and remove any loose blockers before they become an issue".
        from companion.standalone import StandaloneMove
        from protocol.model import Destination
        world = self.world("plow")
        world.place_list = (Destination("p-2", (-700.0, 60.0), "Grain Bin"), Destination("p-3", (-698.0, 88.0), "Petrol"))
        outline = ((150.0, 250.0), (250.0, 250.0), (250.0, 350.0), (150.0, 350.0))
        for iid, at in (("i-7", (200.0, 300.0)), ("i-8", (200.0, 245.0)), ("i-9", (200.0, 200.0))):
            world.items[iid] = Equipment(iid, "implement", "farm-a", at, kinds=frozenset({"cultivator"}), heading=0.0)
        moved = []
        world.teleport_recovery = True
        world.reserve_rig = lambda ids, owner, loose=False: loose
        world.release = lambda owner: None
        world.teleport = lambda vehicle_id, owner, min_m=0.0, at=None: moved.append((vehicle_id, (at.x, at.z)))
        n = StandaloneMove(world).clear_field(outline, ("t-1", "i-1"), "farm-a", "job-1")
        self.assertEqual(n, 2)                                   # on the field and on its headland, not 50 m off
        self.assertEqual(sorted(m[0] for m in moved), ["i-7", "i-8"])
        # They stay at the field (the user: "some may need to stay at a field"): just outside its
        # edge, beyond the headland, never the farm yard.
        from companion.standalone import _edge_point, _inside
        for _, at in moved:
            self.assertFalse(_inside(outline, at))
            self.assertTrue(10.0 < _edge_point(outline, at)[1] <= 20.0, at)
        world.teleport_recovery = False                          # teleporting off in Settings: nothing moves
        self.assertEqual(StandaloneMove(world).clear_field(outline, ("t-1",), "farm-a", "job-2"), 0)

    def test_a_loose_blocker_goes_to_the_farm_yard(self):
        # The user, 2026-10-31: "i am going to end up with equipment all over the map, send it to
        # the farm yard": the farm building with the most other farm buildings around it.
        from companion.standalone import StandaloneMove
        from protocol.model import Destination
        world = self.world("plow")
        world.place_list = (Destination("p-1", (900.0, 900.0), "Small Shed"),
                            Destination("p-2", (-700.0, 60.0), "Grain Bin"),
                            Destination("p-3", (-698.0, 88.0), "Petrol Tank"),
                            Destination("p-4", (-660.0, 79.0), "Silo"))
        loose = Equipment("i-7", "implement", "farm-a", (104.0, 96.0), kinds=frozenset({"cultivator"}), heading=0.0)
        spot = StandaloneMove(world)._yard_spot(loose, (100.0, 100.0))
        self.assertIn((spot.x, spot.z), [(-700.0, 60.0), (-698.0, 88.0)])     # in the cluster, not the far shed

    def test_seeding_a_mostly_sown_field_skips_its_cultivating_and_sows_the_rest(self):
        # Live 2026-10-31: field 3 82 % sown, 18 % plowed; "seed field 3 with wheat" (cultivate,
        # sow) was refused CROP_GROWING at its cultivate step.
        world = self.world("sowing_machine")
        f = world.field_map[7]
        world.field_map[7] = replace(f, coverage=replace(f.coverage, ground=(("SOWN", 0.82), ("PLOWED", 0.18))))
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["cultivate", "sow"]})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"), c.last)
        self.assertIn("cultivate:SOWN_ALREADY", c.last.states)
        self.assertEqual([w.operation for w in c.workers], ["sow"])

    def test_a_helper_kept_through_a_save_and_blocked_has_its_way_cleared(self):
        # Live 2026-10-31: GIANTS kept the seeder's helper through the save and reload; blocked by
        # an implement, it was not Farmhand's worker after the companion restarted.
        world = self.world("plow")
        world.items["t-1"] = replace(world.items["t-1"], busy=True, busy_reason="AI_ACTIVE", ai_blocked=True)
        world.items["i-7"] = Equipment("i-7", "implement", "farm-a", (104.0, 96.0), kinds=frozenset({"cultivator"}),
                                       heading=0.0)
        moved = []
        world.teleport_recovery = True
        world.reserve_rig = lambda ids, owner, loose=False: loose and ids == ("i-7",)
        world.release = (lambda release: lambda owner: None if "-clear" in owner else release(owner))(world.release)
        world.teleport = lambda vehicle_id, owner, min_m=0.0, at=None: moved.append(vehicle_id)
        c, notices = coordinated(world)
        c.WORKER_BLOCKED_S = 0.0
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["sow"]})
        self.assertTrue(until(lambda: moved, 15))
        self.assertEqual(moved, ["i-7"])

    def test_a_request_while_busy_waits_its_turn(self):
        # The user, 2026-10-31: "farmhand needs a queue (waiting list) for field work".
        import threading as _t
        world = self.world("plow")
        c, notices = coordinated(world)
        release, started = _t.Event(), []
        real = c.executor.execute

        def execute(intent, **kw):
            started.append(intent["task"])
            if intent["task"] == "save_game" and len(started) == 1:
                release.wait(5)
            return real(intent, **kw)
        c.executor.execute = execute
        c.submit({"version": 1, "command": "save_game"})
        self.assertTrue(until(lambda: started == ["save_game"]))
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["plow"]})
        self.assertIn("QUEUED", notices)
        release.set()
        self.assertTrue(until(lambda: "field_work" in started, 10))

    def test_with_one_tractor_the_next_implement_waits_for_it_to_come_back(self):
        world = self.world("plow")
        seeder = Equipment("i-9", "implement", "farm-a", (160.0, 100.0), kinds=frozenset({"sowing_machine", "ai_implement"}),
                           heading=0.0, fill_units=(FillUnit(1, "SEEDS", 500.0, 4000.0, role="seed"),),
                           input_joints=(InputJoint(1, "implement", (160.0, 102.0), True),), required_pto_torque=0.0)
        world.items["i-9"] = seeder
        c, _ = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["plow", "sow"]})
        self.assertTrue(until(lambda: c.workers))
        f = world.field_map[7]
        world.field_map[7] = replace(f, coverage=replace(f.coverage, ground=(("PLOWED", 0.4), ("STUBBLE", 0.6))))
        self.assertTrue(until(lambda: c.current is not None and c.current.step == "WAIT_AHEAD"))
        world.finish_worker("t-1")
        c.wait(15)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"), c.last)
        self.assertEqual(self.unit(world, "t-1").attached_to, "i-9")              # the plow back, the seeder on
        self.assertIsNone(self.unit(world, "i-1").attached_to)
        self.assertEqual([w.operation for w in c.workers], ["sow"])

    def test_stop_stops_the_helpers_and_brings_nothing_back(self):
        world = self.world("plow")
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["plow"]})
        c.wait(10)
        c.submit({"version": 1, "command": "stop"})
        self.assertTrue(until(lambda: self.unit(world, "t-1").reserved_by is None))
        self.assertFalse(self.unit(world, "t-1").busy)
        self.assertEqual(c.workers, [])
        self.assertEqual(notices[-1], "STOPPING")
        self.assertEqual(self.unit(world, "t-1").attached_to, "i-1")              # left where it stopped


class FreeATractorTests(unittest.TestCase):
    """Live 7dada469b72f44ed-3: a fertilize contract taken, every tractor with something
    hitched (NO_ELIGIBLE_PAIR). The tractor nearest the implement needed leaves its own
    (never a transport, never the kind needed) where it stands, then takes the needed one."""

    def test_the_tractor_nearest_the_spreader_unhitches_its_plow_first(self):
        from dataclasses import replace as rep
        from companion.coordinator import field_steps
        near = [rep(e, id=e.id + "n", attached_to=(e.attached_to + "n"), name="Series 9 2019" if e.category == "tractor"
                    else "Titan Vineyard Plow") for e in rig("plow", at=(10.0, 10.0))]
        far = [rep(e, id=e.id + "f", attached_to=(e.attached_to + "f"), name="3650" if e.category == "tractor" else "EDK 650")
               for e in rig("cultivator", at=(500.0, 500.0))]
        spreader = rep(rig("sprayer", level=1000.0, at=(30.0, 10.0))[1], id="s-1", attached_to=None)
        steps, _ = field_steps(tuple(near + far + [spreader]), "fertilize", 7, "farm-a")
        self.assertEqual(steps[:2], [{"task": "detach_pair", "tractor_name": "series 9 2019"},
                                     {"task": "hitch_pair", "implement": "sprayer", "tractor_name": "series 9 2019"}])
        # The nearest one facing something close ahead (live: the Series 9 at the Deckover's ramps)
        # could not pull clear after the unhitch: the other one is freed.
        deck = Equipment("d-1", "implement", "farm-a", (10.0, 23.0), heading=0.0, size=(15.0, 2.6, 1.2, 0.0))  # root 13 m ahead, end 5.5 m
        steps, _ = field_steps(tuple(near + far + [spreader, deck]), "fertilize", 7, "farm-a")
        self.assertEqual(steps[0], {"task": "detach_pair", "tractor_name": "3650"})
        free = rep(far[0], attached_to=None)
        steps, _ = field_steps(tuple(near + [free, spreader]), "fertilize", 7, "farm-a")
        self.assertEqual(steps[0], {"task": "hitch_pair", "implement": "sprayer"})     # one is free: nothing unhitched


class HarvestTests(unittest.TestCase):
    """"Harvest field 7": a free combine with its header on, GIANTS' helper, the field
    harvested as GIANTS judges a cut crop (getIsCut on the samples)."""
    READY = FieldCoverage((("HARVEST_READY", 1.0),), (), 4000.0, (("WHEAT", 9),), 9)

    def combine(self, at=(100.0, 100.0)):
        x, z = at
        combine = Equipment("c-1", "harvester", "farm-a", (x, z), heading=0.0, ai_root=Pose(x, z, 0.0),
                            capabilities=frozenset({"move", "attach", "stop"}), attached_to="h-1",
                            kinds=frozenset({"combine", "ai_field_worker", "fill_unit"}))
        header = Equipment("h-1", "implement", "farm-a", (x, z + 4), kinds=frozenset({"cutter", "ai_implement"}),
                           heading=0.0, attached_to="c-1")
        return [combine, header]

    def test_the_header_comes_off_the_combine_and_goes_back_on_where_it_was_carried(self):
        # Combine transport (the owner, 2026-11-05): the header off first, and on again afterwards. Re-attached
        # from the geometry recorded at the detach (the combine's cutter joint is not observed: it moves).
        drives = []

        class Drives(MockWorld):
            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None, via=None):
                drives.append((target, yaw))
                with self._lock:
                    v = self._owned(vehicle_id, owner)
                    self.items[vehicle_id] = replace(v, position=tuple(target), heading=yaw,
                                                     ai_root=Pose(target[0], target[1], yaw))
                return "ARRIVED"

        combine, header = self.combine()
        world = Drives([combine, replace(header, required_pto_torque=0.0)], PLAYERS,
                       fields=(field(7, 0.4, self.READY),), farm=FARM)
        executor = Executor(world)
        base = {"version": 1, "farm_id": "farm-a", "target": "player_at_acceptance", "implement": "cutter"}
        off = executor.execute(dict(base, request_id="r1", task="detach_pair"))
        self.assertEqual(off.status, "COMPLETE", off)
        self.assertIsNone(world.items["h-1"].attached_to)
        self.assertIn("h-1", executor._front_attachments)
        world.items["c-1"] = replace(world.items["c-1"], position=(100.0, 80.0), ai_root=Pose(100.0, 80.0, 0.0))
        on = executor.execute(dict(base, request_id="r2", task="hitch_pair"))
        self.assertEqual((on.status, on.reason), ("COMPLETE", "HITCH_OBSERVED"), on)
        self.assertEqual(world.items["h-1"].attached_to, "c-1")
        self.assertEqual(drives[0][1], 0.0)                                  # at the heading it carried it
        self.assertAlmostEqual(drives[0][0][1], 100.0 - Executor.REATTACH_BEHIND_M, places=6)

    def test_the_header_is_set_down_on_the_trailer_lock_its_combine_lines_up_for(self):
        # Live 2026-11-06: the MF 8570 Trailer keeps a GIANTS lock position for mf8570Header.xml (header
        # root there facing west); the combine carries the header 5.2 m ahead of its AI root.
        from protocol.model import LockPosition
        calls = []

        class Carrier(MockWorld):
            def reserve_rig(self, ids, owner, loose=False, carrier=False):
                calls.append(("reserve", ids, carrier))
                return self.reserve(ids[:2], owner)

            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None, via=None):
                calls.append(("drive", tuple(round(t, 3) for t in target), yaw))
                with self._lock:
                    v = self._owned(vehicle_id, owner)
                    self.items[vehicle_id] = replace(v, position=tuple(target), heading=yaw,
                                                     ai_root=Pose(target[0], target[1], yaw))
                return "ARRIVED"

            def mount(self, vehicle_id, carrier_id, owner):
                calls.append(("mount", vehicle_id, carrier_id))
                with self._lock:
                    self.items[vehicle_id] = replace(self.items[vehicle_id], attached_to=None)
                    self.items["h-1"] = replace(self.items["h-1"], attached_to=None, mounted_to=carrier_id)
                return "MOUNTED"

            def pull_clear(self, vehicle_id, implement_id, owner):
                calls.append(("pull_clear", vehicle_id, implement_id))

        combine, header = self.combine(at=(0.0, 0.0))
        combine = replace(combine, name="MF 8570")
        header = replace(header, position=(0.0, 5.2), config_file="data/vehicles/masseyFerguson/mf8570Header/mf8570Header.xml",
                         required_pto_torque=0.0)
        lock = LockPosition("vehicles/masseyFerguson/mf8570Header/mf8570Header.xml", (-118.3, -159.4), -math.pi / 2, True)
        trailer = Equipment("t-9", "implement", "farm-a", (-117.5, -158.0), heading=0.0, name="MF 8570 Trailer",
                            lock_positions=(lock,))
        other = replace(trailer, id="t-8", position=(-700.0, 160.0),
                        lock_positions=(replace(lock, position=(-700.0, 160.0)),))
        world = Carrier([combine, header, trailer, other], PLAYERS, fields=(field(7, 0.4, self.READY),), farm=FARM)
        result = Executor(world).execute({"version": 1, "farm_id": "farm-a", "target": "player_at_acceptance",
                                          "implement": "cutter", "tractor_name": "mf 8570", "request_id": "r1",
                                          "task": "mount_front"})
        self.assertEqual((result.status, result.reason), ("COMPLETE", "MOUNTED"), result)
        self.assertEqual(calls[0], ("reserve", ("c-1", "h-1", "t-9"), True))           # the nearer trailer
        _, (x, z), yaw = calls[1]
        self.assertAlmostEqual(yaw, -math.pi / 2)                                       # facing west, like the header
        self.assertAlmostEqual(z, -159.4, places=3)                                     # on the lock's line
        self.assertAlmostEqual(x, -118.3 + 5.2 + Executor.MOUNT_BEHIND_M, places=3)     # east of it, header over the lock
        self.assertEqual([c[0] for c in calls[2:]], ["mount", "pull_clear"])

    def test_a_named_combine_hitches_its_header_trailer_and_a_plain_hitch_still_takes_a_tractor(self):
        # Live 2026-11-06: the loaded MF 8570 Trailer (no GIANTS Trailer specialization, a carrier with a
        # lock position for the header) towed by the MF 8570's rear TRAILER joint.
        from protocol.model import LockPosition
        combine, header = self.combine(at=(-113.0, -159.4))
        combine = replace(combine, attached_to=None, name="MF 8570", heading=0.0,
                          output_joints=(OutputJoint(1, "CUTTER", 0.0, 1.78, True), OutputJoint(2, "TRAILER", 0.0, -4.1, True)))
        lock = LockPosition("vehicles/masseyFerguson/mf8570Header/mf8570Header.xml", (-118.3, -159.4), -math.pi / 2, False)
        trailer = Equipment("t-9", "implement", "farm-a", (-117.5, -158.0), heading=0.0, name="MF 8570 Trailer",
                            kinds=frozenset({"fill_unit", "ai_implement"}), lock_positions=(lock,), required_pto_torque=0.0,
                            input_joints=(InputJoint(1, "TRAILER", (-117.5, -153.66), True),))
        from companion.executor import compatible
        self.assertTrue(compatible(combine, trailer, "trailer", driver="mf 8570"))
        self.assertFalse(compatible(combine, trailer, "trailer"))                       # not named: not a tow
        from companion.executor import named, of_kind
        self.assertTrue(of_kind(trailer, "trailer"))
        self.assertTrue(named(trailer, "header"))                                        # "the header trailer"

    def test_the_header_comes_off_the_towed_trailer_parked_with_its_unloading_side_open(self):
        # Live 2026-11-06: the trailer unhitched at field 2's entrance stood among trees, where no line-up
        # to the header on it was found; the carrier is parked first where its right side is open.
        from protocol.model import InputJoint, LockPosition
        calls = []

        class Site(MockWorld):
            def reserve_rig(self, ids, owner, loose=False, carrier=False):
                calls.append(("reserve", ids))
                return self.reserve(ids[:2], owner)

            def park(self, vehicle_id, owner, right_m):
                calls.append(("park", vehicle_id, right_m))

            def detach(self, tractor_id, wagon_id, owner):
                calls.append(("detach", tractor_id, wagon_id))
                with self._lock:
                    self.items[tractor_id] = replace(self.items[tractor_id], attached_to=None)
                    self.items[wagon_id] = replace(self.items[wagon_id], attached_to=None)

            def pull_clear(self, tractor_id, wagon_id, owner):
                calls.append(("pull_clear", tractor_id))

            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None, via=None):
                calls.append(("drive",))
                with self._lock:
                    v = self._owned(vehicle_id, owner)
                    self.items[vehicle_id] = replace(v, position=tuple(target), heading=yaw,
                                                     ai_root=Pose(target[0], target[1], yaw))

        combine, header = self.combine(at=(0.0, 10.0))
        combine = replace(combine, attached_to="t-9", name="MF 8570",
                          output_joints=(OutputJoint(1, "CUTTER", 0.0, 1.78, True), OutputJoint(2, "TRAILER", 0.0, -4.1, False)))
        trailer = Equipment("t-9", "implement", "farm-a", (0.0, 0.0), heading=0.0, name="MF 8570 Trailer",
                            attached_to="c-1", lock_positions=(LockPosition("mf8570Header.xml", (-0.8, -1.4), -math.pi / 2, False),))
        header = replace(header, attached_to=None, mounted_to="t-9", position=(-0.8, -1.4), heading=-math.pi / 2,
                         required_pto_torque=0.0, input_joints=(InputJoint(1, "CUTTER", (0.4, -1.4), True),))
        world = Site([combine, trailer, header], PLAYERS, fields=(field(7, 0.4, self.READY),), farm=FARM)
        result = Executor(world).execute({"version": 1, "farm_id": "farm-a", "target": "player_at_acceptance",
                                          "implement": "cutter", "tractor_name": "mf 8570", "request_id": "r1",
                                          "task": "unmount_front"})
        self.assertEqual((result.status, result.reason), ("COMPLETE", "HITCH_OBSERVED"), result)
        self.assertEqual([c[0] for c in calls[:4]], ["reserve", "park", "detach", "pull_clear"])
        self.assertEqual(calls[1][2], Executor.UNMOUNT_LANE_M)
        self.assertEqual(world.items["h-1"].attached_to, "c-1")

    def test_a_combine_carrying_nothing_a_trailer_takes_is_not_loaded(self):
        world = MockWorld(self.combine(), PLAYERS, fields=(field(7, 0.4, self.READY),), farm=FARM)
        result = Executor(world).execute({"version": 1, "farm_id": "farm-a", "target": "player_at_acceptance",
                                          "implement": "cutter", "request_id": "r1", "task": "mount_front"})
        self.assertEqual((result.status, result.reason), ("BLOCKED", "NO_CARRIER_FOR_IMPLEMENT"))

    def test_a_header_never_carried_is_approached_from_the_observed_joints(self):
        # Live 2026-11-06: the MF 8570 and its header reset at the store, nothing recorded; the combine's
        # free CUTTER joint 1.78 m ahead of its AI root goes onto the header's CUTTER input joint, at
        # the header's heading (facing south, 12.7 m east of the combine facing north).
        from protocol.model import InputJoint, OutputJoint
        drives = []

        class Drives(MockWorld):
            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None, via=None):
                drives.append((target, yaw))
                with self._lock:
                    v = self._owned(vehicle_id, owner)
                    self.items[vehicle_id] = replace(v, position=tuple(target), heading=yaw,
                                                     ai_root=Pose(target[0], target[1], yaw))
                return "ARRIVED"

        combine, header = self.combine(at=(-108.9, -159.7))
        combine = replace(combine, attached_to=None, name="MF 8570", output_joints=(OutputJoint(1, "CUTTER", 0.0, 1.782, True),
                                                                     OutputJoint(2, "TRAILER", 0.0, -4.1, True)))
        header = replace(header, attached_to=None, position=(-96.2, -158.2), heading=-math.pi, required_pto_torque=0.0,
                         input_joints=(InputJoint(1, "CUTTER", (-96.2, -157.05), True),))
        world = Drives([combine, header], PLAYERS, fields=(field(7, 0.4, self.READY),), farm=FARM)
        executor = Executor(world)
        on = executor.execute({"version": 1, "farm_id": "farm-a", "target": "player_at_acceptance",
                               "implement": "cutter", "tractor_name": "mf", "request_id": "r1", "task": "hitch_pair"})
        self.assertEqual((on.status, on.reason), ("COMPLETE", "HITCH_OBSERVED"), on)
        (x, z), yaw = drives[0]
        self.assertAlmostEqual(yaw, -math.pi)
        self.assertAlmostEqual(x, -96.2, places=3)
        self.assertAlmostEqual(z, -157.05 + 1.782 + Executor.REATTACH_BEHIND_M, places=3)    # north of it, facing south

    def test_an_unreachable_header_is_set_down_in_front_of_the_combine_and_attached(self):
        # Live 2026-11-06: NO_ROUTE to the loose header beside a tree, at every staging distance. With
        # teleport recovery allowed the header is set down where the combine, as it stands, carries it
        # (the recorded geometry), the first clear gap behind; then GIANTS' attach as always.
        placed = []

        class Unreachable(MockWorld):
            teleport_recovery = True
            refuse = 1                                        # the nearest pose is not clear

            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None, via=None):
                raise StepFailed("NO_ROUTE")

            def navigate(self, vehicle_id, pose, owner):
                raise StepFailed("NAVIGATE_BLOCKED_AT_START")

            def place(self, implement_id, owner, at):
                with self._lock:
                    self._owned(implement_id, owner)
                    if len(placed) < self.refuse:
                        placed.append(None)
                        raise StepFailed("PLACE_NOT_CLEAR")
                    placed.append(at)
                    self.items[implement_id] = replace(self.items[implement_id], position=(at.x, at.z), heading=at.yaw)

        combine, header = self.combine()
        world = Unreachable([combine, replace(header, required_pto_torque=0.0)], PLAYERS,
                            fields=(field(7, 0.4, self.READY),), farm=FARM)
        executor = Executor(world)
        base = {"version": 1, "farm_id": "farm-a", "target": "player_at_acceptance", "implement": "cutter"}
        self.assertEqual(executor.execute(dict(base, request_id="r1", task="detach_pair")).status, "COMPLETE")
        world.items["h-1"] = replace(world.items["h-1"], position=(160.0, 140.0), heading=1.0)   # far, turned
        world.items["c-1"] = replace(world.items["c-1"], position=(100.0, 80.0), ai_root=Pose(100.0, 80.0, 0.0))
        on = executor.execute(dict(base, request_id="r2", task="hitch_pair"))
        self.assertEqual((on.status, on.reason), ("COMPLETE", "HITCH_OBSERVED"), on)
        self.assertIn("PLACE", on.states)
        at = placed[-1]
        along = executor._front_attachments.get("h-1")                   # dropped once attached
        self.assertIsNone(along)
        self.assertAlmostEqual(at.x, 100.0, places=6)                     # lined up ahead of it
        self.assertAlmostEqual(at.yaw, 0.0, places=6)
        self.assertAlmostEqual(at.z, 80.0 + Executor.REATTACH_PLACE_GAPS_M[1] + 4.0, places=6)

    def test_an_unreachable_header_stays_where_it_lies_without_teleport_recovery(self):
        class Unreachable(MockWorld):
            teleport_recovery = False

            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None, via=None):
                raise StepFailed("NO_ROUTE")

            def navigate(self, vehicle_id, pose, owner):
                raise StepFailed("NAVIGATE_BLOCKED_AT_START")

            def place(self, implement_id, owner, at):
                raise AssertionError("placed without teleport recovery")

        combine, header = self.combine()
        world = Unreachable([combine, replace(header, required_pto_torque=0.0)], PLAYERS,
                            fields=(field(7, 0.4, self.READY),), farm=FARM)
        executor = Executor(world)
        base = {"version": 1, "farm_id": "farm-a", "target": "player_at_acceptance", "implement": "cutter"}
        self.assertEqual(executor.execute(dict(base, request_id="r1", task="detach_pair")).status, "COMPLETE")
        world.items["c-1"] = replace(world.items["c-1"], position=(100.0, 80.0), ai_root=Pose(100.0, 80.0, 0.0))
        on = executor.execute(dict(base, request_id="r2", task="hitch_pair"))
        self.assertEqual((on.status, on.reason), ("FAILED", "NAVIGATE_BLOCKED_AT_START"), on)
        self.assertIsNone(world.items["h-1"].attached_to)

    def test_an_approach_farmhands_search_cannot_find_goes_by_giants_navigation(self):
        # Live 2026-11-06: the MF 8570 beside its header trailer on a field-edge grade, NO_ROUTE.
        went = []

        class Searchless(MockWorld):
            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None, via=None):
                raise StepFailed("NO_ROUTE")

            def navigate(self, vehicle_id, pose, owner):
                went.append(pose)
                with self._lock:
                    v = self._owned(vehicle_id, owner)
                    self.items[vehicle_id] = replace(v, position=(pose.x, pose.z), heading=pose.yaw,
                                                     ai_root=Pose(pose.x, pose.z, pose.yaw))

        combine, header = self.combine()
        world = Searchless([combine, replace(header, required_pto_torque=0.0)], PLAYERS,
                           fields=(field(7, 0.4, self.READY),), farm=FARM)
        executor = Executor(world)
        base = {"version": 1, "farm_id": "farm-a", "target": "player_at_acceptance", "implement": "cutter"}
        self.assertEqual(executor.execute(dict(base, request_id="r1", task="detach_pair")).status, "COMPLETE")
        world.items["c-1"] = replace(world.items["c-1"], position=(100.0, 80.0), ai_root=Pose(100.0, 80.0, 0.0))
        on = executor.execute(dict(base, request_id="r2", task="hitch_pair"))
        self.assertEqual((on.status, on.reason), ("COMPLETE", "HITCH_OBSERVED"), on)
        self.assertAlmostEqual(went[0].yaw, 0.0)

    def test_the_combine_harvests_and_the_field_shows_it_cut(self):
        world = MockWorld(self.combine(), PLAYERS, fields=(field(7, 0.4, self.READY),), farm=FARM)
        result = Executor(world).execute(work("harvest"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)
        self.assertEqual(world.field_map[7].coverage.cut, 9)

    def test_a_combine_standing_in_the_field_works_on_from_where_it_stands(self):
        # Live bc801c056bac4966-1: half harvested, the combine in the field; GIANTS' helper sent to
        # the field's point (beside a parked rig) ended not_reachable. Off the field: the point.
        class Recording(MockWorld):
            def fieldwork(self, tractor_id, field_id, pose, owner, area_m2=0.0, seed=None, hand=False):
                self.poses = getattr(self, "poses", []) + [pose]
                return super().fieldwork(tractor_id, field_id, pose, owner, area_m2, seed, hand)
        for at, expected in (((210.0, 290.0), (210.0, 290.0)), ((100.0, 100.0), (200.0, 300.0))):
            world = Recording(self.combine(at=at), PLAYERS, fields=(field(7, 0.4, self.READY),), farm=FARM)
            Executor(world).execute(work("harvest"))
            self.assertEqual((world.poses[0].x, world.poses[0].z), expected)

    def test_a_crop_not_ready_or_no_combine_moves_nothing(self):
        from companion.coordinator import field_steps
        growing = FieldCoverage((("SOWN", 1.0),), (), 4000.0, (("WHEAT", 9),), 9)
        world = MockWorld(self.combine(), PLAYERS, fields=(field(7, 0.4, growing),), farm=FARM)
        result = Executor(world).execute(work("harvest"))
        self.assertEqual((result.status, result.reason), ("BLOCKED", "CROP_NOT_READY"))
        self.assertEqual(field_steps(tuple(rig("plow")), "harvest", 7, "farm-a"), (None, "NO_HARVESTER_WITH_HEADER"))
        # Then back where it was taken from, its header on (the user: never left in the field).
        self.assertEqual(field_steps(tuple(self.combine()), "harvest", 7, "farm-a")[0],
                         [{"task": "field_work", "field": 7, "operation": "harvest", "implement": "cutter"},
                          {"task": "move_pair", "implement": "cutter", "target": {"type": "origin"}}])
        from companion.executor import movable
        combine, header = (next(i for i in self.combine() if i.category == c) for c in ("harvester", "implement"))
        self.assertTrue(movable(combine, header))

    def test_harvest_the_words_and_the_combine_is_not_a_tractor(self):
        from adapters.fs25 import _category
        from companion.language import RuleInterpreter
        self.assertEqual(RuleInterpreter().interpret("harvest field 3"),
                         {"version": 1, "command": "field_work", "field": 3, "operations": ["harvest"]})
        combine = {"category": "HARVESTERS", "forage_wagon": False, "self_propelled": True, "motor": {"x": 1},
                   "kinds": ["combine", "fill_unit"], "output_joints": [{"index": 1}], "input_joint_poses": []}
        self.assertEqual(_category(combine, {}), "harvester")


class Policy:
    """companion/ai.py Settings' harvest policy, in memory."""

    def __init__(self, destination=None):
        self.destination = destination

    def harvest_destination(self):
        return self.destination

    def set_harvest_destination(self, destination):
        self.destination = destination


class UnloadAndSellTests(unittest.TestCase):
    """The user (2026-10-21): unload the combines while they harvest, sell the product; and
    (2026-10-22) keep unloading, storage and selling separate. "Unload the combine" transfers
    the crop into a trailer and nothing more. A harvest's crop is sold or stored only when the
    request says so or the player's saved harvest policy does; else it stays in the trailer.
    Selling is complete only on the cargo gone and the money in, storing on the cargo gone."""

    def world(self, unloader=True, cargo=0.0):
        from protocol.model import Destination

        combine = HarvestTests().combine()
        items = list(combine)
        if unloader:
            tractor = Equipment("t-5", "tractor", "farm-a", (300.0, 100.0), heading=0.0, ai_root=Pose(300.0, 100.0, 0.0),
                                capabilities=frozenset({"move", "attach", "stop", "autodrive"}), attached_to="w-5",
                                kinds=frozenset({"ai_field_worker"}))
            trailer = Equipment("w-5", "implement", "farm-a", (300.0, 94.0), kinds=frozenset({"trailer", "fill_unit"}),
                                heading=0.0, attached_to="t-5",
                                fill_units=(FillUnit(1, "WHEAT", cargo, 30000.0),))
            items += [tractor, trailer]
        world = CoordinatedWorld(items, PLAYERS,
                      fields=(field(7, 0.4, HarvestTests.READY),), farm=FARM)
        world.mock_sell_at = (900.0, 900.0)
        return world

    def test_a_harvest_is_done_only_when_giants_finished_it_and_the_field_shows_it(self):
        # GIANTS' helper ending is its own verdict (AIMessageSuccessFinishedJob: FINISHED); the
        # measured grid over the field must agree. Ended any other way: not done.
        for ai_stop, notice in (("FINISHED", "WORKER_DONE"), ("AI_STOPPED", "WORKER_STOPPED"), (None, "WORKER_STOPPED")):
            with self.subTest(ai_stop=ai_stop):
                world = self.world(unloader=False)
                c, notices = coordinated(world)
                c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
                c.wait(10)
                with world._lock:                                             # one observation: cut, ended so
                    world.finish_worker("c-1")
                    world.items["c-1"] = replace(world.items["c-1"], ai_stop=ai_stop)
                self.assertTrue(until(lambda: notice in notices))
                self.assertNotIn({"WORKER_DONE": "WORKER_STOPPED", "WORKER_STOPPED": "WORKER_DONE"}[notice], notices)

    def full_combine_world(self):
        # Live 49b5344a47ec418a-2: the MF 8570 full, its helper ending 7 s in; the 3650 with
        # the Fliegl EDK 650 hitched by the player, 60 m away.
        combine, header = HarvestTests().combine()
        combine = replace(combine, pipe=(False, 100.0, 100.0),
                          fill_units=(FillUnit(1, "DIESEL", 300.0, 400.0), FillUnit(2, "WHEAT", 9000.0, 9000.0)))
        tractor = Equipment("t-5", "tractor", "farm-a", (160.0, 60.0), heading=1.0, ai_root=Pose(160.0, 60.0, 1.0),
                            capabilities=frozenset({"move", "attach", "stop", "autodrive"}), attached_to="w-5",
                            kinds=frozenset({"ai_field_worker"}))
        trailer = Equipment("w-5", "implement", "farm-a", Pose(160.0, 60.0, 1.0).to_world(0.0, -7.0),
                            kinds=frozenset({"trailer", "fill_unit"}), heading=1.0, attached_to="t-5",
                            size=(6.0, 2.5, 2.0, 0.4), required_pto_torque=0.0,
                            fill_units=(FillUnit(1, None, 0.0, 20000.0, frozenset({"WHEAT", "BARLEY"})),))
        world = CoordinatedWorld([combine, header, tractor, trailer], PLAYERS, fields=(field(7, 0.4, HarvestTests.READY),),
                                 farm=FARM)
        world.mock_sell_at = (900.0, 900.0)
        return world

    def test_the_trailer_is_driven_onto_the_pipes_run_up_at_the_combines_heading(self):
        # Live ae9262cb2492470b-1: GIANTS GOTO ended at the run-up turned 178 degrees, the trailer
        # bent 140 (ALONGSIDE_NOT_LINED_UP twice). Farmhand's own drive, at its heading, first.
        from protocol.model import Pose
        calls = []

        aims, targets = [], []

        class Goto(CoordinatedWorld):
            def navigate(self, tractor_id, pose, owner):
                aims.append(pose)
                return super().navigate(tractor_id, pose, owner)

        class Driving(CoordinatedWorld):
            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                calls.append((vehicle_id, yaw))
                targets.append(target)
                # Arrives with its root at the target, so its AI root (3 m ahead) at the aim.
                pose = Pose(target[0] + math.sin(yaw) * 3.0, target[1] + math.cos(yaw) * 3.0, yaw)
                return super().navigate(vehicle_id, pose, owner)

        def rigged(cls):
            world = self.full_combine_world()
            world.__class__ = cls
            t = world.items["t-5"]
            # GIANTS' AI root 3 m ahead of the vehicle's root (live: metres apart).
            world.items["t-5"] = replace(t, ai_root=Pose(t.position[0] + math.sin(t.heading) * 3.0,
                                                         t.position[1] + math.cos(t.heading) * 3.0, t.heading))
            return world
        world = rigged(Goto)                     # where GIANTS GOTO is aimed (its AI root)
        c, _ = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        world = rigged(Driving)
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), (c.last, notices))
        heading = world.items["c-1"].heading or 0.0
        self.assertIn(("t-5", heading), calls)
        # The drive's target is the run-up aim moved back by the root's 3 m (live ae9262cb2492470b-1:
        # aimed with the root, the trailer stopped 25.8 m from the pipe, over ALONGSIDE's 25 m).
        aim = aims[0]
        self.assertAlmostEqual(math.dist(targets[1], (aim.x, aim.z)), 3.0, places=6)
        # Towing, it comes the last 12 m straight along the heading first (live b46c3abddb2b4cee-1:
        # the trailer bent 50-69 degrees at the run-up, ALONGSIDE_RIG_NOT_STRAIGHT).
        self.assertAlmostEqual(math.dist(targets[0], targets[1]), 12.0, places=6)
        dx, dz = targets[1][0] - targets[0][0], targets[1][1] - targets[0][1]
        self.assertAlmostEqual(math.atan2(dx, dz), heading, places=6)

    def test_with_the_lane_behind_the_combine_blocked_the_trailer_comes_from_its_front(self):
        # Live 7b52570db6ee40d0-1: a dropped seeder in the lane behind the combine,
        # ALONGSIDE_PATH_BLOCKED at both run-ups; from its front, facing against it.
        aims = []

        class Lane(CoordinatedWorld):
            def navigate(self, tractor_id, pose, owner):
                aims.append(pose)
                return super().navigate(tractor_id, pose, owner)

            def alongside(self, tractor_id, combine_id, owner, combine_owner, *args, **kwargs):
                if len(aims) <= len(Executor.OVERLOAD_RUN_UPS_M):
                    raise StepFailed("ALONGSIDE_PATH_BLOCKED")
                return super().alongside(tractor_id, combine_id, owner, combine_owner, *args, **kwargs)

        world = self.full_combine_world()
        world.__class__ = Lane
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), (c.last, notices))
        heading = world.items["c-1"].heading or 0.0
        n = len(Executor.OVERLOAD_RUN_UPS_M)
        self.assertTrue(all(abs(a.yaw - heading) < 1e-9 for a in aims[:n]))            # behind it first
        self.assertAlmostEqual(aims[n].yaw, heading + math.pi)                        # then from its front

    def test_with_no_way_under_the_pipe_it_tries_twice_and_says_why_moving_nothing_else(self):
        # The user, 2026-10-31: "farmhand is just out here driving the equipment around in circles
        # it has lined up 5 or 6 times". Once from behind, once from the front; the combine is
        # never driven around; then the player is told.
        events = []

        class Pocket(CoordinatedWorld):
            def reposition(self, vehicle_id, owner):
                events.append(("reposition", vehicle_id))

            def alongside(self, tractor_id, combine_id, owner, combine_owner, *args, **kwargs):
                events.append(("alongside",))
                raise StepFailed("ALONGSIDE_PATH_BLOCKED")

        world = self.full_combine_world()
        world.__class__ = Pocket
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("FAILED", "NOT_UNDER_THE_PIPE"), (c.last, notices))
        # From where the rig stands (nothing moves when refused), then once behind and once in front.
        self.assertEqual(events, [("alongside",)] * 3)

    def test_a_pipe_folded_after_the_dry_try_where_the_rig_stood_goes_out_again(self):
        # Live 78d50d7a616a49db-3: nothing under the pipe where the rig stood (NOT_OVERLOADING);
        # let go idle the combine folded its pipe and ALONGSIDE refused PIPE_NOT_OUT.
        class Folding(CoordinatedWorld):
            def overload(self, combine_id, trailer_id, owner):
                try:
                    return super().overload(combine_id, trailer_id, owner)
                except StepFailed:
                    c = self.items[combine_id]
                    self.items[combine_id] = replace(c, pipe=(False,) + tuple(c.pipe[1:]))
                    raise

            def alongside(self, tractor_id, combine_id, owner, combine_owner, *args, **kwargs):
                if not self.items[combine_id].pipe[0]:
                    raise StepFailed("PIPE_NOT_OUT")
                return super().alongside(tractor_id, combine_id, owner, combine_owner, *args, **kwargs)

        world = self.full_combine_world()
        world.__class__ = Folding
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), (c.last, notices))

    def test_a_rig_already_under_the_pipe_unloads_where_it_stands(self):
        # The user, 2026-10-31: "the tractor and trailer are in the correct place at the moment
        # for unloading": GIANTS' own discharge from there, nothing driven.
        from protocol.model import Pose
        from simulator.world import MOCK_PIPE_REACH_M
        world = self.full_combine_world()
        combine = world.items["c-1"]
        t, w = world.items["t-5"], world.items["w-5"]
        # The trailer's middle right under where the pipe reaches (to the combine's left), the
        # tractor ahead of it, both along the combine.
        pipe = Pose(*combine.position, combine.heading or 0.0).to_world(-MOCK_PIPE_REACH_M, 0.0)
        heading = combine.heading or 0.0
        tractor_at = Pose(*pipe, heading).to_world(0.0, 7.0 - w.size[3])
        world.items["t-5"] = replace(t, position=tractor_at, heading=heading, ai_root=Pose(*tractor_at, heading))
        world.items["w-5"] = replace(w, position=Pose(*tractor_at, heading).to_world(0.0, -7.0), heading=heading)
        drives = []
        world.farm_drive = lambda *a, **k: drives.append(a)
        world.navigate = lambda *a, **k: drives.append(a)
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), (c.last, notices))
        self.assertEqual(drives, [])
        self.assertEqual(world.items["c-1"].fill_units[1].level, 0.0)

    def test_per_job_pay_is_charged_for_a_completed_job_that_worked_equipment(self):
        # The user, 2026-10-30: pay per job.
        charges = []

        class Paying(CoordinatedWorld):
            def charge(self, amount):
                charges.append(amount)

        world = self.full_combine_world()
        world.__class__ = Paying
        c, _ = coordinated(world)
        c.pay = lambda: (2, 500.0)                         # per job, 500
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual(c.last.state, "COMPLETE")
        self.assertEqual(charges, [500.0])
        c.pay = lambda: (1, 0.0)                           # per hour: charged by the game, not here
        world2 = self.full_combine_world()
        world2.__class__ = Paying
        c2, _ = coordinated(world2)
        c2.pay = c.pay
        c2.submit({"version": 1, "command": "unload_combine"})
        c2.wait(10)
        self.assertEqual(charges, [500.0])

    def test_unload_the_combine_transfers_the_grain_and_nothing_more(self):
        world = self.full_combine_world()
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), c.last)
        self.assertEqual(world.items["c-1"].fill_units[1].level, 0.0)
        self.assertFalse(world.items["c-1"].pipe[0])                          # pipe back in
        self.assertEqual(world.mock_passed, ("t-5",))                         # on past the combine first
        self.assertEqual(world.items["w-5"].fill_units[0].level, 9000.0)      # in the trailer
        self.assertEqual(world.farm().money, FARM.money)                       # not sold
        self.assertEqual(notices[-2:], ["COMBINE_UNLOADED", "COMPLETE_UNLOAD_COMBINE"])
        self.assertNotIn("COMPLETE_SELL", notices)
        self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))
        # Even with a saved policy to sell: "unload the combine" says nothing about selling.
        world = self.full_combine_world()
        c, notices = coordinated(world)
        c.policy = Policy("sell")
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual(world.items["w-5"].fill_units[0].level, 9000.0)
        self.assertEqual(world.farm().money, FARM.money)

    def test_unload_does_not_claim_success_when_native_fill_change_is_unaccounted(self):
        # Live fill transfers balanced within 1 L at logged precision. Opposite
        # signs alone would accept a large loss between combine and trailer.
        class Unaccounted(CoordinatedWorld):
            def overload(self, combine_id, trailer_id, owner):
                result = super().overload(combine_id, trailer_id, owner)
                trailer = self.items[trailer_id]
                unit = trailer.fill_units[0]
                self.items[trailer_id] = replace(trailer, fill_units=(replace(unit, level=unit.level - 1000.0),)
                                                 + trailer.fill_units[1:])
                return result

        world = self.full_combine_world()
        world.__class__ = Unaccounted
        c, _ = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("UNVERIFIED", "GRAIN_UNACCOUNTED"), c.last)

    def test_the_players_own_words_unload_the_combine_and_sell_the_grain_in_one_go(self):
        # Live d0b10ed974a1467f-1: "the harvester still has wheat in it, unload it and sell it" as a
        # sequence made its unload step an executor intent without a field (INVALID_INTENT); a
        # step runs exactly as the command said alone does.
        from companion.language import RuleInterpreter
        world = self.full_combine_world()
        c, notices = coordinated(world)
        proposal = RuleInterpreter().interpret("the harvester still has wheat in it, unload it and sell it")
        c.submit(proposal)
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "SEQUENCE_DONE"), c.last)
        self.assertEqual(world.items["c-1"].fill_units[1].level, 0.0)       # out of the combine
        self.assertGreater(world.farm().money, FARM.money)                  # and sold
        self.assertIn("unload_combine:ALONGSIDE", c.last.states)

    def test_with_no_rig_a_tractor_is_freed_and_hitched_to_the_free_trailer_first(self):
        # Live f0a179a567804793-1: the EDK 650 stood unhitched and every tractor had an implement
        # on (BLOCKED NO_TRAILER_FOR_THE_GRAIN). The tractor's ordinary implement is unhitched
        # where it has room ahead, the trailer with room hitched, then the grain transferred.
        world = self.full_combine_world()
        t, w = world.items["t-5"], world.items["w-5"]
        cultivator = Equipment("k-2", "implement", "farm-a", Pose(160.0, 60.0, 1.0).to_world(0.0, -4.0),
                               kinds=frozenset({"cultivator", "ai_implement"}), heading=1.0, attached_to="t-5",
                               required_pto_torque=0.0, size=(3.0, 3.0, 1.0, 0.0), name="Cultivator 980",
                               input_joints=(InputJoint(1, "implement", Pose(160.0, 60.0, 1.0).to_world(0.0, -2.0),
                                                        True),))
        world.items["t-5"] = replace(t, attached_to="k-2", name="MT600 Series",
                                     output_joints=(OutputJoint(1, "implement", 0.0, -2.0, False),
                                                    OutputJoint(2, "trailer", 0.0, -2.0, True)))
        free_at = (150.0, 30.0)
        world.items["w-5"] = replace(w, attached_to=None, name="EDK 650", position=free_at, heading=0.0,
                                     input_joints=(InputJoint(1, "trailer", (free_at[0], free_at[1] + 3.0), True),))
        world.items["k-2"] = cultivator
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), (c.last, notices))
        self.assertIsNone(world.items["k-2"].attached_to)                     # the cultivator left where it was
        self.assertEqual(world.items["w-5"].attached_to, "t-5")               # the trailer hitched
        self.assertEqual(world.items["w-5"].fill_units[0].level, 9000.0)      # and filled
        self.assertEqual(world.items["c-1"].fill_units[1].level, 0.0)

    def test_a_tractor_standing_on_its_dropped_implement_is_cleared_before_the_hitch(self):
        # Live fbbcfc1e99724b07-1: the 3650 stood on the joint of the RZK 300H it had dropped
        # (no pull-clear then): AGAINST_IMPLEMENT. The implement is hitched back where the joints
        # touch, unhitched with its pull-clear, then the trailer hitched and the grain moved.
        world = self.full_combine_world()
        t, w = world.items["t-5"], world.items["w-5"]
        root = Pose(160.0, 60.0, 1.0)
        world.items["t-5"] = replace(t, attached_to=None, name="MT600 Series",
                                     output_joints=(OutputJoint(1, "implement", 0.0, -2.0, True),
                                                    OutputJoint(2, "trailer", 0.0, -2.0, True)))
        world.items["k-2"] = Equipment("k-2", "implement", "farm-a", root.to_world(0.0, -4.0),
                                       kinds=frozenset({"cultivator", "ai_implement"}), heading=1.0,
                                       required_pto_torque=0.0, size=(3.0, 3.0, 1.0, 0.0), name="Cultivator 980",
                                       input_joints=(InputJoint(1, "implement", root.to_world(0.0, -2.05), True),))
        free_at = (150.0, 30.0)
        world.items["w-5"] = replace(w, attached_to=None, name="EDK 650", position=free_at, heading=0.0,
                                     input_joints=(InputJoint(1, "trailer", (free_at[0], free_at[1] + 3.0), True),))
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), (c.last, notices))
        self.assertIsNone(world.items["k-2"].attached_to)
        self.assertEqual(world.items["w-5"].attached_to, "t-5")
        self.assertEqual(world.items["w-5"].fill_units[0].level, 9000.0)

    def test_with_no_trailer_it_can_use_farmhand_says_so_and_buys_nothing_untold(self):
        # The user, 2026-10-30: "the only time farmhand should buy anything is under instructions".
        from protocol.model import StoreItem

        class Walled(CoordinatedWorld):
            def stage(self, tractor_id, wagon_id, pose, owner):
                raise StepFailed("NAVIGATE_GOTO_INVALID")

            def navigate(self, tractor_id, pose, owner):
                raise StepFailed("NAVIGATE_GOTO_INVALID")

        world = self.full_combine_world()
        world.__class__ = Walled
        t, w = world.items["t-5"], world.items["w-5"]
        world.items["t-5"] = replace(t, attached_to=None, name="MT600 Series",
                                     output_joints=(OutputJoint(2, "trailer", 0.0, -2.0, True),))
        world.items["w-5"] = replace(w, attached_to=None, name="EDK 650", position=(100.0, -200.0), heading=0.0,
                                     input_joints=(InputJoint(1, "trailer", (100.0, -197.0), True),))
        world.store_items = (StoreItem(37, "Tipper 12", "T", ("TRAILERS",), 6000.0, False, None, None, None,
                                       capacity=12000.0, fill_types=("WHEAT",)),)
        money = world.farm().money
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertNotEqual(c.last.state, "COMPLETE")
        self.assertFalse(any(i.id.startswith("new-") for i in world.items.values()))   # nothing bought
        self.assertEqual(world.farm().money, money)

    def test_with_the_only_trailer_unreachable_a_grain_trailer_is_bought_once_and_used(self):
        # Live 51bff50a47804f8e-1: GIANTS refused every aim on the EDK 650's drawbar side; the
        # 3650 ended behind it (LOCAL_PATH_HITS_WAGON). The job's own requirement is bought, the
        # cheapest grain tipper that takes the crop and holds the load (GIANTS' TRAILERS, never a
        # semitrailer, a platform body, one too small or for another crop), hitched, the grain moved.
        from protocol.model import StoreItem

        class Walled(CoordinatedWorld):
            def stage(self, tractor_id, wagon_id, pose, owner):
                if wagon_id == "w-5":
                    raise StepFailed("NAVIGATE_GOTO_INVALID")
                return super().stage(tractor_id, wagon_id, pose, owner)

            def navigate(self, tractor_id, pose, owner):
                reserved = self._reservations.get(owner, (None, (None, None)))[1]
                if reserved[1] == "w-5":
                    raise StepFailed("NAVIGATE_GOTO_INVALID")
                return super().navigate(tractor_id, pose, owner)

        world = self.full_combine_world()
        world.__class__ = Walled
        t, w = world.items["t-5"], world.items["w-5"]
        world.items["t-5"] = replace(t, attached_to=None, name="MT600 Series",
                                     output_joints=(OutputJoint(2, "trailer", 0.0, -2.0, True),))
        walled_at = (100.0, -200.0)
        world.items["w-5"] = replace(w, attached_to=None, name="EDK 650", position=walled_at, heading=0.0,
                                     input_joints=(InputJoint(1, "trailer", (walled_at[0], walled_at[1] + 3.0), True),))
        shop = (210.0, 90.0)
        world.mock_bought = replace(w, id="tmp", attached_to=None, position=shop, heading=0.0,
                                    input_joints=(InputJoint(1, "trailer", (shop[0], shop[1] + 3.0), True),))
        world.store_items = (StoreItem(31, "Semi Tipper", "X", ("SEMITRAILERS",), 3000.0, False, None, None, None,
                                       capacity=30000.0, fill_types=("WHEAT",)),
                             StoreItem(34, "Platform body", "W", ("TRAILERS",), 2000.0, False, None, None, None),
                             StoreItem(35, "Tipper 6", "V", ("TRAILERS",), 4000.0, False, None, None, None,
                                       capacity=6000.0, fill_types=("WHEAT",)),         # too small for 9,000 l
                             StoreItem(36, "Potato box", "U", ("TRAILERS",), 3500.0, False, None, None, None,
                                       capacity=20000.0, fill_types=("POTATO",)),       # not for wheat
                             StoreItem(32, "Tipper 12", "Y", ("TRAILERS",), 8000.0, False, None, None, None,
                                       capacity=12000.0, fill_types=("WHEAT", "BARLEY")),
                             StoreItem(33, "Tipper 8", "Z", ("TRAILERS",), 6000.0, False, None, None, None,
                                       capacity=9500.0, fill_types=("WHEAT",)),
                             StoreItem(37, "Mod Tipper", "M", ("TRAILERS",), 1000.0, True, None, None, None,
                                       capacity=20000.0, fill_types=("WHEAT",)))       # a mod: never for a job
        c, notices = coordinated(world)
        c.buy_when_needed = True                  # buying allowed for this test
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), (c.last, notices))
        new = [i for i in world.items.values() if i.id.startswith("new-")]
        self.assertEqual([i.name for i in new], ["Tipper 8"])                  # the cheapest grain tipper, once
        self.assertEqual(new[0].attached_to, "t-5")
        self.assertEqual(new[0].fill_units[0].level, 9000.0)
        self.assertEqual(world.farm().money, FARM.money - 6000.0)

    def test_a_trailer_out_of_reach_is_passed_for_the_next_one(self):
        # Live 7059fb537cf04e6a-1: the Titan trailer beyond grades (no route of Farmhand's own,
        # GIANTS could not start the tractor), then the EDK 650 under the same request id:
        # REQUEST_ID_CONFLICT. Each trailer is its own request; the reachable one is used.
        class OutOfReach(CoordinatedWorld):
            def navigate(self, tractor_id, pose, owner):
                reserved = self._reservations.get(owner, (None, (None, None)))[1]
                if reserved[1] == "w-far":
                    raise StepFailed("NAVIGATE_GOTO_INVALID")
                return super().navigate(tractor_id, pose, owner)

            def stage(self, tractor_id, wagon_id, pose, owner):
                if wagon_id == "w-far":
                    raise StepFailed("NAVIGATE_GOTO_INVALID")
                return super().stage(tractor_id, wagon_id, pose, owner)

        world = self.full_combine_world()
        world.__class__ = OutOfReach
        t, w = world.items["t-5"], world.items["w-5"]
        world.items["t-5"] = replace(t, attached_to=None, name="3650",
                                     output_joints=(OutputJoint(2, "trailer", 0.0, -2.0, True),))
        far, near = (100.0, -200.0), (200.0, 70.0)
        world.items["w-far"] = replace(w, id="w-far", attached_to=None, name="Titan Trailer", position=far, heading=0.0,
                                       fill_units=(FillUnit(1, None, 0.0, 30000.0, frozenset({"WHEAT"})),),
                                       input_joints=(InputJoint(1, "trailer", (far[0], far[1] + 3.0), True),))
        world.items["w-5"] = replace(w, attached_to=None, name="EDK 650", position=near, heading=0.0,
                                     input_joints=(InputJoint(1, "trailer", (near[0], near[1] + 3.0), True),))
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), (c.last, notices))
        self.assertEqual(world.items["w-5"].attached_to, "t-5")
        self.assertIsNone(world.items["w-far"].attached_to)

    def test_a_trailer_that_cannot_be_lined_up_is_passed_for_the_next_one(self):
        # Live ad2bdc29c456469f-1: the Titan trailer on a loading pad, the bounded steps STALLED
        # there; the next trailer is taken, as a person would.
        class Stalls(CoordinatedWorld):
            def align(self, tractor_id, wagon_id, *args, **kwargs):
                if wagon_id == "w-far":
                    raise StepFailed("STALLED")
                return super().align(tractor_id, wagon_id, *args, **kwargs)

        world = self.full_combine_world()
        world.__class__ = Stalls
        t, w = world.items["t-5"], world.items["w-5"]
        world.items["t-5"] = replace(t, attached_to=None, name="3650",
                                     output_joints=(OutputJoint(2, "trailer", 0.0, -2.0, True),))
        far, near = (100.0, -200.0), (200.0, 70.0)
        world.items["w-far"] = replace(w, id="w-far", attached_to=None, name="Titan Trailer", position=far, heading=0.0,
                                       fill_units=(FillUnit(1, None, 0.0, 30000.0, frozenset({"WHEAT"})),),
                                       input_joints=(InputJoint(1, "trailer", (far[0], far[1] + 3.0), True),))
        world.items["w-5"] = replace(w, attached_to=None, name="EDK 650", position=near, heading=0.0,
                                     input_joints=(InputJoint(1, "trailer", (near[0], near[1] + 3.0), True),))
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), (c.last, notices))
        self.assertEqual(world.items["w-5"].attached_to, "t-5")

    def test_a_trailer_bought_after_three_out_of_reach_is_its_own_request(self):
        # Live 5aafb45731a844eb-1: the third trailer tried and the bought one shared a request
        # id (REQUEST_ID_CONFLICT); the bought trailer was never hitched.
        from protocol.model import StoreItem

        class OutOfReach(CoordinatedWorld):
            def navigate(self, tractor_id, pose, owner):
                reserved = self._reservations.get(owner, (None, (None, None)))[1]
                if str(reserved[1]).startswith("w-far"):
                    raise StepFailed("NAVIGATE_GOTO_INVALID")
                return super().navigate(tractor_id, pose, owner)

            def stage(self, tractor_id, wagon_id, pose, owner):
                if wagon_id.startswith("w-far"):
                    raise StepFailed("NAVIGATE_GOTO_INVALID")
                return super().stage(tractor_id, wagon_id, pose, owner)

        world = self.full_combine_world()
        world.__class__ = OutOfReach
        t, w = world.items["t-5"], world.items.pop("w-5")
        world.items["t-5"] = replace(t, attached_to=None, name="3650",
                                     output_joints=(OutputJoint(2, "trailer", 0.0, -2.0, True),))
        for n, name in enumerate(("Titan Trailer", "Bulk Cart", "Grain Wagon")):
            at = (100.0 + 40 * n, -200.0)
            world.items[f"w-far{n}"] = replace(w, id=f"w-far{n}", attached_to=None, name=name, position=at,
                                               heading=0.0, input_joints=(InputJoint(1, "trailer", (at[0], at[1] + 3.0), True),))
        shop = (210.0, 90.0)
        world.mock_bought = replace(w, id="tmp", attached_to=None, name="Tipper 8", position=shop, heading=0.0,
                                    input_joints=(InputJoint(1, "trailer", (shop[0], shop[1] + 3.0), True),))
        world.store_items = (StoreItem(33, "Tipper 8", "Z", ("TRAILERS",), 6000.0, False, None, None, None,
                                       capacity=30000.0, fill_types=("WHEAT",)),)
        c, notices = coordinated(world)
        c.buy_when_needed = True                  # buying allowed for this test
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "COMBINE_UNLOADED"), (c.last, notices))
        new = [i for i in world.items.values() if i.id.startswith("new-")]
        self.assertEqual(len(new), 1)

    def test_a_bought_trailer_that_arrives_unable_to_take_the_crop_is_said_and_never_hitched(self):
        # Live e3e3b0c194914776-2: the store specs said grain, the default configuration bought
        # was a bale loader. Checked as it arrived: UNVERIFIED BOUGHT_ITEM_UNSUITABLE, not hitched.
        from protocol.model import StoreItem
        world = self.full_combine_world()
        t, w = world.items["t-5"], world.items["w-5"]
        world.items["t-5"] = replace(t, attached_to=None, name="MT600 Series",
                                     output_joints=(OutputJoint(2, "trailer", 0.0, -2.0, True),))
        del world.items["w-5"]
        shop = (210.0, 90.0)
        world.mock_bought = replace(w, id="tmp", attached_to=None, position=shop, heading=0.0,
                                    fill_units=(FillUnit(1, None, 0.0, 12.0, frozenset({"SQUAREBALE"})),
                                                # live e3e3b0c194914776-3: a nominal grain unit of 0 l
                                                FillUnit(2, None, 0.0, 0.0, frozenset({"WHEAT"}))),
                                    input_joints=(InputJoint(1, "trailer", (shop[0], shop[1] + 3.0), True),))
        world.store_items = (StoreItem(33, "Tipper 8", "Z", ("TRAILERS",), 6000.0, False, None, None, None,
                                       capacity=9500.0, fill_types=("WHEAT",)),)
        c, notices = coordinated(world)
        c.buy_when_needed = True                  # buying allowed for this test
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("UNVERIFIED", "BOUGHT_ITEM_UNSUITABLE"), c.last)
        new = [i for i in world.items.values() if i.id.startswith("new-")]
        self.assertEqual(len(new), 1)
        self.assertIsNone(new[0].attached_to)

    def test_harvest_with_a_full_combine_empties_it_first(self):
        world = self.full_combine_world()
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"), c.last)
        self.assertIn("COMBINE_UNLOADED", notices)
        self.assertEqual(world.items["c-1"].fill_units[1].level, 0.0)

    def harvest_until_full(self, world, c, notices, **said):
        unit = world.items["c-1"].fill_units[1]
        world.items["c-1"] = replace(world.items["c-1"], fill_units=(world.items["c-1"].fill_units[0],
                                                                     replace(unit, level=0.0)))
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"], **said})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"), c.last)
        combine = world.items["c-1"]
        world.items["c-1"] = replace(combine, fill_units=(combine.fill_units[0], replace(unit, level=9000.0)))
        self.assertTrue(until(lambda: "COMBINE_FULL" in notices))
        self.assertTrue(until(lambda: c.last is not None and c.last.command == "combine_full"
                              and c.last.state == "COMPLETE"))

    def test_a_combine_full_while_harvesting_is_emptied_into_a_trailer_and_harvests_on(self):
        # GIANTS' helper waits at a full tank for a trailer (AIDriveStrategyCombine); with no
        # AutoDrive unloader none comes: Farmhand stops it, transfers the grain, starts it again.
        world = self.full_combine_world()
        c, notices = coordinated(world)
        self.harvest_until_full(world, c, notices)
        self.assertEqual(world.items["c-1"].fill_units[1].level, 0.0)
        self.assertEqual(world.items["w-5"].fill_units[0].level, 9000.0)
        self.assertEqual(world.farm().money, FARM.money)                       # never sold unasked
        self.assertEqual([w.tractor for w in c.workers if w.operation == "harvest"], ["c-1"])   # harvesting on
        self.assertIsNone(c.kept)

    def test_the_destination_is_used_once_the_trailer_cannot_take_another_tankful(self):
        # 12,000 l of trailer, a 9,000 l tank: after the transfer 3,000 l of room, so the grain
        # goes where this harvest said (sold) or the saved policy says (stored).
        for said, policy, sold, stored in (({"destination": "sell"}, None, 9000.0, 0.0),
                                           ({}, "store", 0.0, 9000.0), ({}, "keep", 0.0, 0.0)):
            with self.subTest(said=said, policy=policy):
                world = self.full_combine_world()
                trailer = world.items["w-5"]
                world.items["w-5"] = replace(trailer, fill_units=(replace(trailer.fill_units[0], capacity=12000.0),))
                world.mock_store_at = (400.0, 400.0)
                c, notices = coordinated(world)
                c.policy = Policy(policy)
                self.harvest_until_full(world, c, notices, **said)
                self.assertTrue(until(lambda: c.current is None))
                self.assertEqual(world.farm().money, FARM.money + sold * world.mock_price)
                self.assertEqual(world.stored_litres, stored)
                left = world.items["w-5"].fill_units[0].level
                self.assertEqual(left, 0.0 if sold or stored else 9000.0)

    def test_with_no_trailer_room_and_nowhere_to_take_it_the_combine_waits_and_continue_goes_on(self):
        world = self.full_combine_world()
        trailer = world.items["w-5"]
        world.items["w-5"] = replace(trailer, fill_units=(replace(trailer.fill_units[0], level=20000.0,
                                                                  fill_type="WHEAT"),))
        c, notices = coordinated(world)
        unit = world.items["c-1"].fill_units[1]
        world.items["c-1"] = replace(world.items["c-1"], fill_units=(world.items["c-1"].fill_units[0],
                                                                     replace(unit, level=0.0)))
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
        c.wait(10)
        combine = world.items["c-1"]
        world.items["c-1"] = replace(combine, fill_units=(combine.fill_units[0], replace(unit, level=9000.0)))
        self.assertTrue(until(lambda: "COMBINE_FULL_NO_TRAILER" in notices))
        self.assertTrue(until(lambda: c.current is None and c.last.command == "combine_full"))
        self.assertEqual((c.last.state, c.last.reason), ("BLOCKED", "NO_TRAILER_FOR_THE_GRAIN"))
        self.assertEqual(world.farm().money, FARM.money)
        self.assertIsNotNone(c.kept)                                         # waits for the player
        c.submit({"version": 1, "command": "sell"})                          # the player says where
        self.assertTrue(until(lambda: c.current is None and c.last.command == "sell"))
        c.submit({"version": 1, "command": "resume"})
        self.assertTrue(until(lambda: c.current is None and c.last.command == "combine_full"
                              and c.last.state == "COMPLETE"))
        self.assertEqual(world.items["c-1"].fill_units[1].level, 0.0)
        self.assertEqual([w.tractor for w in c.workers if w.operation == "harvest"], ["c-1"])

    def test_unload_the_combine_while_it_harvests_pauses_it_transfers_and_harvests_on(self):
        world = self.full_combine_world()
        unit = world.items["c-1"].fill_units[1]
        world.items["c-1"] = replace(world.items["c-1"], fill_units=(world.items["c-1"].fill_units[0],
                                                                     replace(unit, level=0.0)))
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
        c.wait(10)
        combine = world.items["c-1"]
        world.items["c-1"] = replace(combine, fill_units=(combine.fill_units[0], replace(unit, level=5000.0)),
                                     busy=False)
        c.submit({"version": 1, "command": "unload_combine"})
        self.assertTrue(until(lambda: c.current is None and c.last.command == "unload_combine"))
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"), c.last.states)
        self.assertEqual(world.items["w-5"].fill_units[0].level, 5000.0)
        self.assertEqual(world.farm().money, FARM.money)
        self.assertEqual([w.tractor for w in c.workers if w.operation == "harvest"], ["c-1"])

    # ------------------------------------------------- the grain cart (unloading on the move)
    def cart_world(self):
        world = self.full_combine_world()
        unit = world.items["c-1"].fill_units[1]
        world.items["c-1"] = replace(world.items["c-1"], fill_units=(world.items["c-1"].fill_units[0],
                                                                     replace(unit, level=0.0)))
        world.items["t-5"] = replace(world.items["t-5"], name="MT600 Series")
        world.items["w-5"] = replace(world.items["w-5"], name="EDK 650")
        return world, unit

    def fill(self, world, unit, level):
        combine = world.items["c-1"]
        world.items["c-1"] = replace(combine, fill_units=(combine.fill_units[0], replace(unit, level=level)))

    def test_a_grain_cart_follows_the_harvest_and_takes_the_grain_on_the_move(self):
        # FINAL_FARMHAND_PRODUCT_SPEC.md 5: a separate worker unloads the combine while it harvests;
        # the combine is never stopped for it.
        world, unit = self.cart_world()
        c, notices = coordinated(world)
        c.grain_cart = True
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
        self.assertTrue(until(lambda: "t-5" in getattr(world, "mock_escorting", ())), notices)
        self.assertIn("ACCEPTED_GRAIN_CART", notices)
        self.assertTrue(until(lambda: c.current is None))                  # Farmhand is free meanwhile
        self.fill(world, unit, 6000.0)                     # harvested while the cart rides beside it
        self.assertTrue(until(lambda: world.items["w-5"].fill_units[0].level == 6000.0))
        self.assertEqual(world.items["c-1"].fill_units[1].level, 0.0)
        self.assertEqual([w.tractor for w in c.workers if w.operation == "harvest"], ["c-1"])   # never paused
        self.assertTrue(world.items["c-1"].busy)
        world.finish_worker("c-1")                          # the field harvested
        self.assertTrue(until(lambda: "COMPLETE_GRAIN_CART" in notices), notices)
        self.assertFalse(c._carts)
        self.assertNotIn("COMBINE_FULL", notices)
        self.assertIsNone(world.items["t-5"].reserved_by)

    def test_a_full_cart_takes_its_load_where_the_harvest_goes_and_comes_back(self):
        world, unit = self.cart_world()
        trailer = world.items["w-5"]
        world.items["w-5"] = replace(trailer, fill_units=(replace(trailer.fill_units[0], capacity=6000.0),))
        c, notices = coordinated(world)
        c.grain_cart = True
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"],
                  "destination": "sell"})
        self.assertTrue(until(lambda: "t-5" in getattr(world, "mock_escorting", ())))
        # (Under the 95 % a standing mock combine counts as waiting full: the watch's own case.)
        self.fill(world, unit, 7000.0)
        # 6,000 l taken: full, sold, and back beside the combine for the 1,000 l left.
        self.assertTrue(until(lambda: world.farm().money == FARM.money + 6000.0 * world.mock_price), notices)
        self.assertTrue(until(lambda: world.items["w-5"].fill_units[0].level == 1000.0))
        self.assertIn("COMPLETE_SELL", notices)
        self.assertIn(7, c._carts)
        self.assertTrue(world.items["c-1"].busy)
        self.assertNotIn("COMBINE_FULL", notices)
        world.finish_worker("c-1")
        # The harvest done: what is left in the cart goes where the harvest goes.
        self.assertTrue(until(lambda: world.farm().money == FARM.money + 7000.0 * world.mock_price), notices)

    def test_unload_the_combine_while_it_harvests_sends_the_cart_not_a_stop(self):
        world, unit = self.cart_world()
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
        c.wait(10)
        c.grain_cart = True
        self.fill(world, unit, 5000.0)
        c.submit({"version": 1, "command": "unload_combine"})
        self.assertTrue(until(lambda: world.items["w-5"].fill_units[0].level == 5000.0))
        self.assertTrue(world.items["c-1"].busy)            # it harvests on
        self.assertEqual([w.tractor for w in c.workers if w.operation == "harvest"], ["c-1"])
        c.stop()
        self.assertTrue(until(lambda: c.current is None))
        self.assertEqual(c.last.state, "CANCELLED")
        self.assertNotIn("t-5", getattr(world, "mock_escorting", ()))
        self.assertIsNone(world.items["t-5"].reserved_by)

    def test_any_machine_that_drives_and_tows_a_trailer_is_a_grain_cart(self):
        # Capabilities, not a "tractor" label: a truck (the 379 is category unknown in save 1) tows the cart.
        world, unit = self.cart_world()
        world.items["t-5"] = replace(world.items["t-5"], category="unknown")
        c, notices = coordinated(world)
        c.grain_cart = True
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
        self.assertTrue(until(lambda: "t-5" in getattr(world, "mock_escorting", ())), notices)
        world.finish_worker("c-1")
        self.assertTrue(until(lambda: "COMPLETE_GRAIN_CART" in notices), notices)

    def test_the_cart_works_beside_the_next_job_and_stop_ends_both(self):
        # FINAL_FARMHAND_PRODUCT_SPEC.md 4: concurrent workers, not a queue. The cart rides beside the
        # harvesting combine while the player's next job runs; STOP ends the job, the cart and the helper.
        world, unit = self.cart_world()
        c, notices = coordinated(world)
        c.grain_cart = True
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
        self.assertTrue(until(lambda: "t-5" in getattr(world, "mock_escorting", ())), notices)
        self.assertTrue(until(lambda: c.current is None))
        c.submit({"version": 1, "command": "save_game"})
        self.assertTrue(until(lambda: c.last is not None and c.last.command == "save_game" and c.current is None),
                        c.last)
        self.assertIn("t-5", world.mock_escorting)                         # still at work beside it
        c.stop()
        self.assertTrue(until(lambda: not c._carts), notices)
        self.assertNotIn("t-5", world.mock_escorting)
        self.assertFalse(world.items["c-1"].busy)
        self.assertTrue(until(lambda: world.items["t-5"].reserved_by is None))

    def test_with_no_cart_rig_one_is_hitched_for_the_harvest(self):
        # The trailer left unhitched: a free tractor is hitched to it (never bought unasked).
        world, unit = self.cart_world()
        free_at = (150.0, 30.0)
        world.items["t-5"] = replace(world.items["t-5"], attached_to=None,
                                     output_joints=(OutputJoint(2, "trailer", 0.0, -2.0, True),))
        world.items["w-5"] = replace(world.items["w-5"], attached_to=None, position=free_at, heading=0.0,
                                     input_joints=(InputJoint(1, "trailer", (free_at[0], free_at[1] + 3.0), True),))
        c, notices = coordinated(world)
        c.grain_cart = True
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
        self.assertTrue(until(lambda: "t-5" in getattr(world, "mock_escorting", ())), (c.last, notices))
        self.assertEqual(world.items["w-5"].attached_to, "t-5")
        world.finish_worker("c-1")
        self.assertTrue(until(lambda: "COMPLETE_GRAIN_CART" in notices), notices)

    def test_the_harvest_policy_is_saved_from_the_players_words(self):
        from companion.language import RuleInterpreter
        world = self.world()
        c, notices = coordinated(world)
        c.policy = Policy()
        c.submit(RuleInterpreter().interpret("from now on sell the harvest"))
        self.assertEqual((c.policy.destination, notices[-1]), ("sell", "HARVEST_POLICY_SELL"))
        c.submit(RuleInterpreter().interpret("keep the grain in the trailer"))
        self.assertEqual((c.policy.destination, notices[-1]), ("keep", "HARVEST_POLICY_KEEP"))
        c.policy = None
        c.submit(RuleInterpreter().interpret("always store the grain"))
        self.assertEqual(notices[-1], "POLICY_UNAVAILABLE")

    def test_store_the_grain_takes_it_to_the_farms_own_storage_unpaid(self):
        world = self.world(cargo=8000.0)
        world.mock_store_at = (400.0, 400.0)
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "store"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "STORED"), c.last)
        self.assertEqual((world.stored_litres, world.farm().money), (8000.0, FARM.money))
        self.assertEqual(notices[-1], "COMPLETE_STORE")
        world = self.world(cargo=8000.0)                                      # no storage: nothing moves
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "store"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("FAILED", "NO_STORAGE"), c.last)

    def test_a_rig_left_beside_the_combine_is_taken_past_it_before_the_harvest(self):
        # Live 931324a7d25a48d7-1: the EDK rig stood beside the MF 8570 (from an overload before
        # PASS existed); GIANTS' agent started neither, FIELDWORK_STALLED "blocked".
        world = self.full_combine_world()
        unit = world.items["c-1"].fill_units[1]
        world.items["c-1"] = replace(world.items["c-1"], fill_units=(world.items["c-1"].fill_units[0],
                                                                     replace(unit, level=0.0)))
        for i, at in (("t-5", (106.0, 104.0)), ("w-5", (106.0, 97.0))):
            world.items[i] = replace(world.items[i], position=at, heading=0.0,
                                     ai_root=Pose(*at, 0.0) if i == "t-5" else None)
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"), c.last)
        self.assertEqual(world.mock_passed, ("t-5",))
        self.assertFalse(world.items["c-1"].pipe[0])

    def test_sell_uses_giants_deliver_helper_without_autodrive(self):
        world = self.world(cargo=8000.0)
        tractor = world.items["t-5"]
        world.items["t-5"] = replace(tractor, capabilities=tractor.capabilities - {"autodrive"})
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "sell"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "SOLD"), c.last)
        world.mock_no_deliver = True
        world.items["w-5"] = replace(world.items["w-5"], fill_units=(FillUnit(1, "WHEAT", 500.0, 30000.0),))
        c.submit({"version": 1, "command": "sell"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("FAILED", "DELIVER_UNAVAILABLE"))   # no AutoDrive to fall back on

    def test_the_trailer_must_stand_under_the_pipe(self):
        class Wide(CoordinatedWorld):
            def overload(self, combine_id, trailer_id, owner):
                raise StepFailed("NOT_OVERLOADING")             # never under it, at every aim
        world = self.full_combine_world()
        world.__class__ = Wide
        result = Executor(world).execute({"version": 1, "request_id": "o-1", "farm_id": "farm-a",
                                          "target": "player_at_acceptance", "task": "overload"})
        self.assertEqual((result.status, result.reason), ("FAILED", "NOT_UNDER_THE_PIPE"), result)
        self.assertFalse(world.items["c-1"].pipe[0])
        self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

    def test_sell_the_grain_is_complete_only_on_the_money(self):
        world = self.world(cargo=8000.0)
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "sell"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "SOLD"), c.last)
        self.assertEqual(world.farm().money, FARM.money + 16000.0)
        self.assertEqual(notices[-1], "COMPLETE_SELL")
        c.submit({"version": 1, "command": "sell"})                      # nothing left
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("BLOCKED", "NO_RIG_TO_SELL"))

    def test_the_words(self):
        from companion.language import RuleInterpreter
        r = RuleInterpreter()
        for words in ("sell the grain", "sell the wheat in the trailer", "go sell the load"):
            self.assertEqual(r.interpret(words), {"version": 1, "command": "sell"})
        for words in ("sell the tractor", "sell the plow"):
            self.assertNotEqual((r.interpret(words) or {}).get("command"), "sell")
        self.assertEqual(r.interpret("harvest field 3 and sell the grain"),
                         {"version": 1, "command": "field_work", "field": 3, "operations": ["harvest"],
                          "destination": "sell"})
        for words in ("harvest field 3 and store it", "harvest field 3 and put the grain in the silo"):
            self.assertEqual(r.interpret(words)["destination"], "store")
        self.assertNotIn("destination", r.interpret("harvest field 3"))
        for words in ("store the grain", "put the wheat in the grain bin", "take the load to the silo"):
            self.assertEqual(r.interpret(words), {"version": 1, "command": "store"})
        self.assertNotEqual((r.interpret("take this to the farm silo") or {}).get("command"), "store")
        self.assertEqual(r.interpret("from now on sell the harvest"),
                         {"version": 1, "command": "harvest_policy", "destination": "sell"})
        self.assertEqual(r.interpret("sell the grain from now on")["destination"], "sell")
        self.assertEqual(r.interpret("keep the grain in the trailer")["destination"], "keep")
        self.assertIsNone(r.interpret("don't sell the harvest"))
        self.assertEqual(r.interpret("unload the combine"), {"version": 1, "command": "unload_combine"})
        self.assertEqual(r.interpret("unload the harvester on field 3"),
                         {"version": 1, "command": "unload_combine", "field": 3})
        self.assertEqual(r.interpret("unload the tractor"), {"version": 1, "command": "unload_tractor"})
        # The unloader hitched by the store names (live save 1: the Fliegl EDK 650, the 3650).
        self.assertEqual(r.interpret("hitch the edk 650 to the 3650"),
                         {"version": 1, "command": "hitch_pair", "implement_name": "edk 650", "tractor_name": "3650"})


class StrawTests(unittest.TestCase):
    """The user (2026-10-22): "the field still needs to have a forage wagon run to collect the
    straw and be sold". GIANTS' field-work helper drives the forage wagon over the field
    (GIANTS ForageWagon picks the windrow up); done only when the windrow is measured gone
    from the field (Observe.fieldWindrow); a full wagon's load goes where the request said and
    collecting goes on; sold only on the load gone and the money in; then the wagon back."""
    STRAW = FieldCoverage(ground=(("STUBBLE", 1.0),), spray=(), total_m2=4000.0, fruit=(("NONE", 9),), sampled=9,
                          windrow=(("STRAW", 20000.0),))

    def world(self, capacity=40000.0, windrow=STRAW.windrow):
        x, z = 100.0, 100.0
        tractor = Equipment("t-1", "tractor", "farm-a", (x, z), heading=0.0, ai_root=Pose(x, z, 0.0),
                            capabilities=frozenset({"move", "attach", "stop"}), attached_to="fw-1", name="Fendt 724",
                            kinds=frozenset({"ai_field_worker"}),
                            output_joints=(OutputJoint(1, "trailerLow", 0.0, -2.0, False),))
        wagon = Equipment("fw-1", "forage_wagon", "farm-a", (x, z - 6), heading=0.0, attached_to="t-1",
                          kinds=frozenset({"forage_wagon", "trailer", "fill_unit", "ai_implement"}), name="Tebbe",
                          required_pto_torque=0.0,
                          fill_units=(FillUnit(1, "UNKNOWN", 0.0, capacity),),
                          input_joints=(InputJoint(1, "trailerLow", (x, z - 2.0), True),))
        world = CoordinatedWorld([tractor, wagon], PLAYERS,
                                 fields=(field(7, 0.4, replace(self.STRAW, windrow=windrow)),), farm=FARM)
        world.mock_sell_at = (900.0, 900.0)
        return world

    def collect(self, c, destination="sell"):
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["collect"],
                  **({"destination": destination} if destination else {})})
        c.wait(10)

    def test_done_only_when_the_windrow_is_measured_gone(self):
        collect = OPERATIONS["collect"]
        self.assertEqual(collect.done(self.STRAW), 0.0)
        self.assertEqual(collect.done(replace(self.STRAW, windrow=(("STRAW", 30.0),))), 1.0)    # crumbs on 0.4 ha
        self.assertEqual(collect.done(replace(self.STRAW, windrow=None)), 0.0)                  # unknown: never done

    def test_the_straw_is_collected_sold_and_the_wagon_brought_back(self):
        world = self.world()
        c, notices = coordinated(world)
        self.collect(c)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"))
        self.assertEqual([(w.tractor, w.operation) for w in c.workers], [("t-1", "collect")])
        world.finish_worker("t-1")
        self.assertTrue(until(lambda: c.last.command == "worker_return" and c.last.state == "COMPLETE"))
        self.assertEqual(world.field_map[7].coverage.windrow, ())
        self.assertEqual(world.farm().money, FARM.money + 20000.0 * world.mock_price)
        self.assertEqual(world.items["fw-1"].fill_units[0].level, 0.0)
        self.assertIsNone(world.items["fw-1"].attached_to)                 # back and unhitched
        self.assertIn("WORKER_DONE", notices)
        self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

    def test_a_full_wagon_is_sold_and_collecting_goes_on_to_the_end(self):
        world = self.world(capacity=12000.0)
        c, notices = coordinated(world)
        self.collect(c)
        world.mock_pickup("t-1", litres=12000.0)                          # full halfway
        self.assertTrue(until(lambda: "WAGON_FULL" in notices))
        self.assertTrue(until(lambda: [w.operation for w in c.workers] == ["collect"]
                              and world.items["fw-1"].fill_units[0].level == 0.0))
        self.assertEqual(world.farm().money, FARM.money + 12000.0 * world.mock_price)   # the first load sold
        world.finish_worker("t-1")
        self.assertTrue(until(lambda: c.last.command == "worker_return" and c.last.state == "COMPLETE"))
        self.assertEqual(world.field_map[7].coverage.windrow, ())
        self.assertEqual(world.farm().money, FARM.money + 20000.0 * world.mock_price)
        self.assertIsNone(world.items["fw-1"].attached_to)

    def test_without_a_destination_a_full_wagon_waits_and_says_so(self):
        world = self.world(capacity=12000.0)
        c, notices = coordinated(world)
        self.collect(c, destination=None)
        world.mock_pickup("t-1", litres=12000.0)
        self.assertTrue(until(lambda: "WAGON_FULL_NO_DESTINATION" in notices and c.workers == []))
        self.assertEqual(world.farm().money, FARM.money)                   # nothing sold unasked
        self.assertEqual(world.items["fw-1"].attached_to, "t-1")           # it waits, loaded

    def test_no_windrow_or_an_unreadable_one_is_said_before_anything_moves(self):
        for windrow, reason in (((), "NO_WINDROW_ON_FIELD"), (None, "WINDROW_UNKNOWN")):
            with self.subTest(reason=reason):
                world = self.world(windrow=windrow)
                c, _ = coordinated(world)
                self.collect(c)
                self.assertEqual((c.last.state, c.last.reason), ("BLOCKED", reason))
                self.assertEqual(world.items["t-1"].position, (100.0, 100.0))

    def test_without_a_forage_wagon_the_helper_can_use_it_is_said(self):
        world = self.world()
        world.items["fw-1"] = replace(world.items["fw-1"], kinds=frozenset({"forage_wagon", "trailer", "fill_unit"}))
        c, _ = coordinated(world)
        self.collect(c)
        self.assertEqual((c.last.state, c.last.reason), ("BLOCKED", "NO_IMPLEMENT_FOR_COLLECT"))

    def test_the_words(self):
        from companion.language import RuleInterpreter
        rules = RuleInterpreter()
        for words, destination in (("collect the straw on field 7 with the forage wagon and sell it", "sell"),
                                   ("pick up the straw from field 7", None),
                                   ("collect the straw with the forage wagon on field 7 and store it", "store"),
                                   ("gather the grass swaths on field 7", None)):
            with self.subTest(words=words):
                got = rules.interpret(words)
                self.assertEqual((got["command"], got["field"], got["operations"], got.get("destination")),
                                 ("field_work", 7, ["collect"], destination))


class JoinFieldsTests(unittest.TestCase):
    """The user, 2026-10-31: "add field 4 to field 5 with create fields enabled": the strip
    between them plowed with field creation on (GIANTS' field work then takes them as one)."""

    def world(self):
        from protocol.model import Destination
        events = []

        class Joining(MockWorld):
            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                events.append(("drive", tuple(round(c, 1) for c in target)))
                for unit, behind in (("t-1", 0.0), ("i-1", 4.0)):
                    self.items[unit] = replace(self.items[unit], position=(target[0], target[1] - behind))
                if yaw is not None:
                    self.items["t-1"] = replace(self.items["t-1"], heading=yaw)

            def worktool(self, tractor_id, lower, owner):
                events.append(("lower" if lower else "raise",))
                return "LOWERED" if lower else "RAISED"

        world = Joining(rig("plow", at=(90.0, 30.0)), PLAYERS, fields=(field(4, 0.6), field(5, 0.5)), farm=FARM)
        world.items["i-1"] = replace(world.items["i-1"], work_width=3.0)
        world.mock_field_places = (
            Destination("field-4", (50.0, 30.0), "Field 4", ((0.0, 0.0), (100.0, 0.0), (100.0, 60.0), (0.0, 60.0))),
            Destination("field-5", (156.0, 30.0), "Field 5", ((112.0, 0.0), (200.0, 0.0), (200.0, 60.0), (112.0, 60.0))))
        return world, events

    def test_each_lane_is_driven_raised_to_its_start_and_plowed_lowered_to_its_end(self):
        world, events = self.world()
        result = Executor(world).execute({"version": 1, "request_id": "j-1", "farm_id": "farm-a", "task": "join_fields",
                                          "target": "player_at_acceptance", "field": 4, "into_field": 5})
        self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELDS_JOINED"), result)
        kinds = [e[0] for e in events]
        self.assertEqual(kinds[:4], ["drive", "lower", "drive", "raise"])          # beside it: onto the line, plow
        self.assertEqual(kinds.count("lower"), kinds.count("raise"))
        self.assertGreaterEqual(kinds.count("lower"), 5)                           # the whole 18 m strip
        self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

    def test_a_drive_ending_a_few_metres_short_of_a_lane_point_has_arrived(self):
        # Live 1e27b06900b64385-6: 1.9 m from the lane start, OBSTACLE_AHEAD with nothing there.
        for short, outcome in ((1.9, "COMPLETE"), (6.0, "FAILED")):
            with self.subTest(short=short):
                world, events = self.world()
                base = type(world).farm_drive

                def farm_drive(vehicle_id, target, owner, area=0.0, yaw=None, base=base, world=world, short=short):
                    # Short along the lane (on its line): arrived; the lanes run along z here.
                    along = math.copysign(short, math.cos(yaw)) if yaw is not None else short
                    base(world, vehicle_id, (target[0], target[1] - along), owner, area, yaw)
                    raise StepFailed("OBSTACLE_AHEAD")
                world.farm_drive = farm_drive
                result = Executor(world).execute({"version": 1, "request_id": "j-4", "farm_id": "farm-a",
                                                  "task": "join_fields", "target": "player_at_acceptance",
                                                  "field": 4, "into_field": 5})
                self.assertEqual(result.status, outcome, result)

    def test_never_lowered_unless_lined_up_on_the_lane(self):
        # Live 41701e68ab: lowered 2 m off and turned, it plowed a curve ("a boomerang").
        world, events = self.world()
        base = type(world).farm_drive

        def farm_drive(vehicle_id, target, owner, area=0.0, yaw=None, base=base, world=world):
            base(world, vehicle_id, target, owner, area, None if yaw is None else yaw + math.radians(20))
        world.farm_drive = farm_drive
        result = Executor(world).execute({"version": 1, "request_id": "j-7", "farm_id": "farm-a",
                                          "task": "join_fields", "target": "player_at_acceptance",
                                          "field": 4, "into_field": 5})
        self.assertEqual((result.status, result.reason), ("FAILED", "LANE_NOT_LINED_UP"))
        self.assertNotIn("lower", [e[0] for e in events])               # never lowered

    def test_a_lane_whose_run_up_is_out_of_reach_is_plowed_from_its_other_end(self):
        world, events = self.world()
        base, refused = type(world).farm_drive, []

        def farm_drive(vehicle_id, target, owner, area=0.0, yaw=None, base=base, world=world):
            if not refused:
                refused.append(target)
                raise StepFailed("NO_ROUTE")
            base(world, vehicle_id, target, owner, area, yaw)
        world.farm_drive = farm_drive
        result = Executor(world).execute({"version": 1, "request_id": "j-8", "farm_id": "farm-a",
                                          "task": "join_fields", "target": "player_at_acceptance",
                                          "field": 4, "into_field": 5})
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertIn("lower", [e[0] for e in events])

    def test_standing_on_the_lane_it_plows_from_where_it_stands(self):
        # Live 2026-10-31: the user placed the Series 9 on the strip; it drove off to a run-up instead.
        world, events = self.world()
        world.items["i-1"] = replace(world.items["i-1"], work_width=15.0)       # one lane
        lane = transport.strip_lanes(tuple(p.outline for p in world.mock_field_places)[0],
                                     tuple(p.outline for p in world.mock_field_places)[1], 15.0 * 0.9)[0]
        (ax, az), (bx, bz) = lane
        mid = ((ax + bx) / 2 + 0.8, (az + bz) / 2)
        yaw = math.atan2(bx - ax, bz - az)
        world.items["t-1"] = replace(world.items["t-1"], position=mid, heading=yaw)
        result = Executor(world).execute({"version": 1, "request_id": "j-9", "farm_id": "farm-a",
                                          "task": "join_fields", "target": "player_at_acceptance",
                                          "field": 4, "into_field": 5})
        self.assertEqual(result.status, "COMPLETE", result)
        kinds = [e[0] for e in events]
        self.assertEqual(kinds[:3], ["lower", "drive", "raise"])                # plowed at once, no run-up
        self.assertEqual(kinds.count("lower"), 2)                                # then the half behind it

    def test_turned_across_the_strip_it_drives_onto_the_line_ahead_and_plows(self):
        world, events = self.world()
        world.items["i-1"] = replace(world.items["i-1"], work_width=15.0)
        lane = transport.strip_lanes(tuple(p.outline for p in world.mock_field_places)[0],
                                     tuple(p.outline for p in world.mock_field_places)[1], 15.0 * 0.9)[0]
        (ax, az), (bx, bz) = lane
        yaw = math.atan2(bx - ax, bz - az)
        world.items["t-1"] = replace(world.items["t-1"], position=(ax + (bx - ax) * 0.2, az + (bz - az) * 0.2),
                                     heading=yaw + math.radians(68))
        result = Executor(world).execute({"version": 1, "request_id": "j-10", "farm_id": "farm-a",
                                          "task": "join_fields", "target": "player_at_acceptance",
                                          "field": 4, "into_field": 5})
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual([e[0] for e in events][:4], ["drive", "lower", "drive", "raise"])   # onto the line, plow

    def test_at_an_end_of_the_strip_it_plows_straight_from_where_it_stands(self):
        world, events = self.world()
        world.items["i-1"] = replace(world.items["i-1"], work_width=15.0)
        lane = transport.strip_lanes(tuple(p.outline for p in world.mock_field_places)[0],
                                     tuple(p.outline for p in world.mock_field_places)[1], 15.0 * 0.9)[0]
        (ax, az), (bx, bz) = lane
        at = (ax + 3.0, az - 4.0)
        world.items["t-1"] = replace(world.items["t-1"], position=at,
                                     heading=math.atan2(bx - at[0], bz - at[1]) + math.radians(10))
        result = Executor(world).execute({"version": 1, "request_id": "j-11", "farm_id": "farm-a",
                                          "task": "join_fields", "target": "player_at_acceptance",
                                          "field": 4, "into_field": 5})
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(events[:3], [("lower",), ("drive", tuple(round(c, 1) for c in (bx, bz))), ("raise",)])

    def test_lanes_go_by_the_plows_working_width(self):
        # Live 2026-10-31: a 10 m plow's size box said 1.67 m and nine lanes were planned for one
        # narrow strip; the user: "always go by working width". 12 m between the fields here.
        for width, lanes in ((15.0, 1), (8.0, 2)):          # 13 m to cover, passes 0.9 widths apart
            with self.subTest(width=width):
                world, events = self.world()
                world.items["i-1"] = replace(world.items["i-1"], work_width=width)
                result = Executor(world).execute({"version": 1, "request_id": "j-5", "farm_id": "farm-a",
                                                  "task": "join_fields", "target": "player_at_acceptance",
                                                  "field": 4, "into_field": 5})
                self.assertEqual(result.status, "COMPLETE", result)
                self.assertIn([e[0] for e in events].count("lower"), (lanes, lanes + 1))   # + the part behind
        world, events = self.world()
        world.items["i-1"] = replace(world.items["i-1"], work_width=None)
        result = Executor(world).execute({"version": 1, "request_id": "j-6", "farm_id": "farm-a",
                                          "task": "join_fields", "target": "player_at_acceptance",
                                          "field": 4, "into_field": 5})
        self.assertEqual((result.status, result.reason), ("BLOCKED", "WORK_WIDTH_UNKNOWN"))
        self.assertEqual(events, [])

    def test_fields_far_apart_or_no_plow_move_nothing(self):
        world, events = self.world()
        result = Executor(world).execute({"version": 1, "request_id": "j-2", "farm_id": "farm-a", "task": "join_fields",
                                          "target": "player_at_acceptance", "field": 4, "into_field": 9})
        self.assertEqual((result.status, result.reason), ("BLOCKED", "FIELD_NOT_FOUND"))
        world.items["i-1"] = replace(world.items["i-1"], kinds=frozenset({"cultivator", "ai_implement"}))
        result = Executor(world).execute({"version": 1, "request_id": "j-3", "farm_id": "farm-a", "task": "join_fields",
                                          "target": "player_at_acceptance", "field": 4, "into_field": 5})
        self.assertEqual((result.status, result.reason), ("BLOCKED", "NO_PLOW_HITCHED"))
        self.assertEqual(events, [])


class DriveStopTests(unittest.TestCase):
    """Live abf1610fc62543be-3: after the requester's STOP, Farmhand's own drive ended STOPPED
    and the executor logged "the route's provider is tried". A STOP ends the drive."""

    def run_move(self, code):
        calls = []

        class World(MockWorld):
            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                calls.append("farm_drive")
                raise StepFailed(code)

        executor = Executor(World(rig("plow"), PLAYERS, fields=(field(4, 0.6),), farm=FARM))
        executor._navigate = lambda *args: calls.append("navigate")
        try:
            executor._move("t-1", Pose(10.0, 20.0, 0.0), "o-1", deliver=True, partner_id="i-1")
        except StepFailed as failure:
            calls.append(failure.code)
        return calls

    def test_a_stopped_drive_tries_no_other_provider(self):
        for code in ("STOPPED", "RESERVATION_LOST"):
            self.assertEqual(self.run_move(code), ["farm_drive", code])

    def test_a_failed_drive_still_tries_the_route_provider(self):
        self.assertEqual(self.run_move("NO_GOAL_PROGRESS"), ["farm_drive", "navigate"])


class ChangedAreaTests(unittest.TestCase):
    """One measure stops a test area (mod) and accepts it (companion)."""

    def test_the_companion_and_the_mod_measure_a_test_area_alike(self):
        from pathlib import Path
        from lupa import lua54
        from companion.executor import changed_area
        cases = (
            ((("STUBBLE", 1.0),), (), (("STUBBLE", 0.96), ("PLOWED", 0.04)), ()),          # 400 m2 plowed
            ((("PLOWED", 0.5), ("CULTIVATED", 0.5)), ((1, 0.5),),
             (("PLOWED", 0.5), ("CULTIVATED", 0.5)), ((1, 0.54),)),                        # 400 m2 sprayed
        )
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        bridge = lua.execute(Path("mod/FarmhandBridge.lua").read_text())
        for g0, s0, g1, s1 in cases:
            before = FieldCoverage(g0, s0, 10000.0, None, 0)
            after = FieldCoverage(g1, s1, 10000.0, None, 0)
            ours = changed_area(before, after)
            theirs = bridge.changedArea(lua.table(ground=lua.table(**dict(g0)), spray=lua.table_from(dict(s0))),
                                        lua.table(ground=lua.table(**dict(g1)), spray=lua.table_from(dict(s1)),
                                                  total_m2=10000.0))
            self.assertAlmostEqual(ours, 400.0, places=6)
            self.assertAlmostEqual(theirs, ours, places=6)
