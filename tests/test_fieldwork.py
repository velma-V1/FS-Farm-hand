"""field_work: GIANTS' field-work helper on an owned field, complete only on the
field's measured state (companion/field.py, companion/executor.py)."""
from dataclasses import replace
import math
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

    def test_a_field_the_helper_cannot_reach_from_afar_is_reached_by_autodrive_first(self):
        # Live 5c8eeb68ccea4304-2: the 3650 + spreader 1 km from field 10, GIANTS' helper ended
        # NOT_REACHABLE at once. AutoDrive takes the pair to the field's marker (made its own
        # way), the helper starts from there; without AutoDrive the failure stands.
        from protocol.model import Destination
        for autodrive in (True, False):
            with self.subTest(autodrive=autodrive):
                items = rig("sprayer", 500.0, at=(1100.0, 900.0))
                caps = frozenset({"move", "attach", "stop"} | ({"autodrive"} if autodrive else set()))
                items[0] = replace(items[0], capabilities=caps)
                items[1] = replace(items[1], required_pto_torque=0.0)
                world = MockWorld(items, PLAYERS, (Destination("m-7", (205.0, 290.0), "Field 7"),),
                                  fields=(field(7, 0.4), field(9, 1.2)), farm=FARM)
                world.mock_helper_reach_m = 150.0
                # AutoDrive's network: the waypoint nearest the rig on a branch with no route
                # (live 57e9dbe9633044fd-1); a routed road 60 m off leads to the field.
                from companion.hitch import ADGraph
                world.mock_ad_graph = ADGraph(points={1: (1100.0, 915.0), 2: (1100.0, 960.0),
                                                      10: (1160.0, 900.0), 11: (205.0, 290.0)},
                                              out={1: (2,), 2: (), 10: (11,), 11: ()})
                result = Executor(world).execute(work("fertilize"))
                if autodrive:
                    self.assertEqual((result.status, result.reason), ("COMPLETE", "FIELD_WORK_OBSERVED"), result)
                    self.assertIn("TO_FIELD", result.states)
                    self.assertIn("STAGE", result.states)            # GIANTS GOTO onto the routed road first
                else:
                    self.assertEqual((result.status, result.reason), ("FAILED", "NOT_REACHABLE"), result)
                    self.assertNotIn("TO_FIELD", result.states)

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

    OFFERS = ((20, "harvest", "CREATED", False, 9000.0),       # not a contract Farmhand does
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
        self.assertEqual(field_steps(tuple(self.combine()), "harvest", 7, "farm-a")[0],
                         [{"task": "field_work", "field": 7, "operation": "harvest", "implement": "cutter"}])

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
        world = CoordinatedWorld(items, PLAYERS, destinations=(Destination("17", (210.0, 290.0), "Field 7"),),
                      fields=(field(7, 0.4, HarvestTests.READY),), farm=FARM)
        world.mock_sell_at = (900.0, 900.0)
        return world

    def test_a_harvest_to_be_sold_is_unloaded_while_it_harvests_and_the_rest_is_sold_after(self):
        world = self.world()
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"], "destination": "sell"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"))
        self.assertEqual(sorted((w.tractor, w.operation) for w in c.workers), [("c-1", "harvest"), ("t-5", "unload")])
        self.assertIn("UNLOADER_STARTED", notices)
        unit = world.items["w-5"]
        world.items["w-5"] = replace(unit, fill_units=(replace(unit.fill_units[0], level=5000.0),))   # AutoDrive loaded it
        world.finish_worker("c-1")
        self.assertTrue(until(lambda: c.last.command == "worker_return" and c.last.state == "COMPLETE"))
        self.assertEqual(world.farm().money, FARM.money + 10000.0)
        self.assertEqual(world.items["w-5"].fill_units[0].level, 0.0)
        self.assertEqual(c.workers, [])
        self.assertTrue(all(i.reserved_by is None for i in world.snapshot()))
        self.assertIn("WORKER_DONE", notices)

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

    def test_without_an_unloader_the_player_is_told(self):
        world = self.world(unloader=False)
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"], "destination": "sell"})
        c.wait(10)
        self.assertIn("NO_UNLOADER", notices)
        self.assertEqual([w.tractor for w in c.workers], ["c-1"])

    def test_a_harvest_not_said_to_be_sold_never_sends_autodrives_selling_unloader(self):
        # AutoDrive's combine unloader drives its load to a sell point: never unless selling.
        world = self.world()
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"]})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "WORKERS_STARTED"))
        self.assertEqual([(w.tractor, w.operation) for w in c.workers], [("c-1", "harvest")])
        self.assertNotIn("UNLOADER_STARTED", notices)
        self.assertNotIn("NO_UNLOADER", notices)
        world.finish_worker("c-1")
        self.assertTrue(until(lambda: c.workers == [] and not c._after and c.current is None))
        self.assertEqual(world.farm().money, FARM.money)                       # nothing sold

    def test_unload_the_combine_sends_an_unloader_hitched_after_the_harvest_began(self):
        # Live f41b44f422974e15-4: the harvest began with no unloader; the player hitches a
        # trailer to an AutoDrive tractor and says "unload the combine".
        world = self.world(unloader=False)
        c, notices = coordinated(world)
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("BLOCKED", "NO_FULL_COMBINE"))   # none standing full either
        c.submit({"version": 1, "command": "field_work", "field": 7, "operations": ["harvest"], "destination": "sell"})
        c.wait(10)
        self.assertIn("NO_UNLOADER", notices)
        rig = self.world().items
        world.items.update({k: rig[k] for k in ("t-5", "w-5")})
        c.workers[0].destination = "sell"                     # this harvest's crop is to be sold
        c.submit({"version": 1, "command": "unload_combine"})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "UNLOADER_STARTED"), c.last)
        self.assertEqual(sorted((w.tractor, w.operation) for w in c.workers), [("c-1", "harvest"), ("t-5", "unload")])
        c.submit({"version": 1, "command": "unload_combine", "field": 7})
        c.wait(10)
        self.assertEqual((c.last.state, c.last.reason), ("COMPLETE", "UNLOADER_ALREADY_WORKING"))
        world.finish_worker("c-1")
        self.assertTrue(until(lambda: c.workers == []))

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
