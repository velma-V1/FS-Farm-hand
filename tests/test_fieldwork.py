"""field_work: GIANTS' field-work helper on an owned field, complete only on the
field's measured state (companion/field.py, companion/executor.py)."""
from dataclasses import replace
import unittest

from adapters.world import StepFailed
from companion.executor import Executor
from companion.executor import COVERAGE_REQUIRED, OPERATIONS, resolve_field
from protocol.model import Equipment, FarmInfo, Field, FieldCoverage, FillUnit, InputJoint, OutputJoint, Pose
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

    def test_each_operation_completes_on_the_measured_field(self):
        for operation, kind, level in (("cultivate", "cultivator", 0.0), ("sow", "sowing_machine", 500.0),
                                       ("fertilize", "sprayer", 500.0)):
            with self.subTest(operation=operation):
                world = self.world(kind, level)
                result = Executor(world).execute(work(operation))
                self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)
                self.assertEqual(result.states, ("FIND", "RESERVE", "FIELD_WORK", "VERIFY", "STOP", "RELEASE"))
                self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))

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
                                 {"task": "detach_pair", "implement": "sowing_machine"}])
        hitched = self.items("cultivator", "sowing_machine", hitched=1)
        self.assertEqual(field_steps(hitched, "sow", 7, "farm-a")[0][0]["task"], "field_work")   # already on
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
            def execute(self, intent, cancel=None, on_state=None):
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
            ("detach_pair", None, "cultivator", None),
            ("hitch_pair", None, "sowing_machine", None), ("field_work", "sow", "sowing_machine", 7),
            ("detach_pair", None, "sowing_machine", None),
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
            def execute(self, intent, cancel=None, on_state=None):
                seen.append((intent["task"], world.rolls))
                if intent["task"] == "field_work":
                    world.rolls = 1
                return Result("COMPLETE", "OK", ("t-1",), ("FIND",))

        c = coordination.Coordinator(world, Scripted(), lambda n, s=False: None, lambda: "farm-a", lambda: "s1")
        c.submit({"version": 1, "command": "field_work", "field": "smallest", "operations": ["sow"]})
        c.wait(20)
        self.assertEqual(seen, [("hitch_pair", 0), ("field_work", 0), ("detach_pair", 0)])
        self.assertEqual(c.last.state, "COMPLETE")


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
