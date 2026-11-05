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

    def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
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

    def test_with_no_route_of_its_own_giants_goto_is_tried_before_the_job_ends(self):
        # Live 6d9dfa2f4a6e453c-1: "take the gmc to field 90", the field across the river,
        # Farmhand's drive NO_ROUTE and the job ended; GIANTS GOTO plans on its own map.
        from adapters.world import StepFailed

        class NoRoute(World):
            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                self.calls.append(("drive", vehicle_id, target, area))
                raise StepFailed("NO_ROUTE")

            def navigate(self, vehicle_id, pose, owner):
                self.calls.append(("navigate", vehicle_id, (pose.x, pose.z)))
                delta = (pose.x - self.items[vehicle_id].position[0], pose.z - self.items[vehicle_id].position[1])
                self.items = {i: replace(item, position=(item.position[0] + delta[0], item.position[1] + delta[1]))
                              for i, item in self.items.items()}

        world = NoRoute((unit("611", "GMC Sierra", "tractor", position=(-696.0, 139.0)),))
        result = Executor(world).execute({"version": 1, "request_id": "gmc-barn", "farm_id": "1",
                                          "task": "move_pair", "tractor_name": "gmc",
                                          "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual([c[0] for c in world.calls[1:3]], ["drive", "navigate"])
        # Anything else Farmhand's drive refuses still ends the job (never a blind GOTO).
        class Seated(NoRoute):
            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                raise StepFailed("PERSON_TOO_CLOSE")
        world = Seated((unit("611", "GMC Sierra", "tractor", position=(-696.0, 139.0)),))
        result = Executor(world).execute({"version": 1, "request_id": "gmc-barn", "farm_id": "1",
                                          "task": "move_pair", "tractor_name": "gmc",
                                          "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("navigate", [c[0] for c in world.calls])

    def test_giants_goto_blocked_at_its_start_goes_again_from_open_ground(self):
        # Live 7069876416d14b6d-1: GIANTS GOTO from beside the 3650 was blocked at its start; the
        # rig is driven onto open ground first (Farmhand's own drive), then GOTO again.
        from adapters.world import StepFailed

        class Cramped(World):
            opened = False

            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                self.calls.append(("drive",))
                raise StepFailed("NO_ROUTE")

            def reposition(self, vehicle_id, owner):
                self.calls.append(("reposition", vehicle_id))
                self.opened = True

            def navigate(self, vehicle_id, pose, owner):
                self.calls.append(("navigate",))
                if not self.opened:
                    raise StepFailed("NAVIGATE_BLOCKED_AT_START")
                delta = (pose.x - self.items[vehicle_id].position[0], pose.z - self.items[vehicle_id].position[1])
                self.items = {i: replace(item, position=(item.position[0] + delta[0], item.position[1] + delta[1]))
                              for i, item in self.items.items()}

        world = Cramped((unit("611", "GMC Sierra", "tractor", position=(-696.0, 139.0)),))
        result = Executor(world).execute({"version": 1, "request_id": "gmc-barn", "farm_id": "1",
                                          "task": "move_pair", "tractor_name": "gmc",
                                          "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual([c[0] for c in world.calls[1:5]], ["drive", "navigate", "reposition", "navigate"])

    def test_a_rig_boxed_in_by_a_parked_tractor_has_it_moved_out_of_the_way_then_goes(self):
        # Live 140a6b52320b4587-1: the GMC rig against the 3650 and its trailer; no route, GIANTS
        # blocked at its start, no way onto open ground. The user, 2026-10-30: "if its a tractor have
        # farmhand get in it and move it somewhere out of the way then resume the original task".
        from adapters.world import StepFailed

        class Boxed(World):
            def reserve_rig(self, ids, owner):
                self.calls.append(("reserve", ids, owner))
                self.items = {i: replace(item, reserved_by=owner) if i in ids else item
                              for i, item in self.items.items()}
                return True

            def release(self, owner):
                self.calls.append(("release", owner))
                self.items = {i: replace(item, reserved_by=None) if item.reserved_by == owner else item
                              for i, item in self.items.items()}

            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                self.calls.append(("drive", vehicle_id))
                if math.dist(self.items["615"].position, self.items["611"].position) < 12.0:
                    raise StepFailed("NO_ROUTE")
                return super().farm_drive(vehicle_id, target, owner, area, yaw)

            def navigate(self, vehicle_id, pose, owner):
                self.calls.append(("navigate", vehicle_id))
                raise StepFailed("NAVIGATE_BLOCKED_AT_START")

            def reposition(self, vehicle_id, owner):
                self.calls.append(("reposition", vehicle_id))
                if vehicle_id == "611":
                    raise StepFailed("NO_ROUTE")          # the boxed rig itself cannot get out
                for i in ("615", "633"):                    # the 3650 and its trailer, out of the way
                    x, z = self.items[i].position
                    self.items[i] = replace(self.items[i], position=(x + 40.0, z))

        world = Boxed((unit("611", "GMC Sierra", "tractor", position=(-696.0, 139.0)),
                       # As the game reports it: the tractor's attached_to names the trailer it tows.
                       unit("615", "3650", "tractor", position=(-690.0, 135.0), attached="633"),
                       unit("633", "Z 18051", "implement", position=(-690.0, 128.0), attached="615"),
                       unit("700", "Far Tractor", "tractor", position=(-500.0, 300.0))))
        result = Executor(world).execute({"version": 1, "request_id": "gmc-barn", "farm_id": "1",
                                          "task": "move_pair", "tractor_name": "gmc",
                                          "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "COMPLETE", result)
        moved = [c for c in world.calls if c[0] == "reposition"]
        self.assertEqual(moved, [("reposition", "611"), ("reposition", "615")])   # only the blocker, not the far one
        self.assertIn(("reserve", ("615", "633"), "gmc-barn-clear0"), world.calls)   # with its trailer
        self.assertIn(("release", "gmc-barn-clear0"), world.calls)
        self.assertEqual(world.items["611"].reserved_by, None)
        # Moved off but short of its spot (live 21a5e70740024850-1: OBSTACLE_AHEAD 2 m from it):
        # judged by where it is, the way may be open and the move resumes.
        class Short(Boxed):
            def reposition(self, vehicle_id, owner):
                super().reposition(vehicle_id, owner)
                if vehicle_id != "611":
                    raise StepFailed("OBSTACLE_AHEAD")
        world = Short((unit("611", "GMC Sierra", "tractor", position=(-696.0, 139.0)),
                       unit("615", "3650", "tractor", position=(-690.0, 135.0), attached="633"),
                       unit("633", "Z 18051", "implement", position=(-690.0, 128.0), attached="615")))
        result = Executor(world).execute({"version": 1, "request_id": "gmc-barn", "farm_id": "1",
                                          "task": "move_pair", "tractor_name": "gmc",
                                          "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "COMPLETE", result)

    def test_a_long_trip_goes_to_giants_navigation_first_and_farmhand_drives_what_is_left(self):
        # The user, 2026-10-30: use the pieces already made; Farmhand's own search took minutes to
        # find no route across the river before GIANTS was even asked.
        class Far(World):
            short = 0.0

            def navigate(self, vehicle_id, pose, owner):
                self.calls.append(("navigate", vehicle_id))
                x, z = self.items[vehicle_id].position
                k = 1.0 - self.short / math.dist((x, z), (pose.x, pose.z))
                self.items = {i: replace(item, position=(x + (pose.x - x) * k, z + (pose.z - z) * k))
                              for i, item in self.items.items()}

        world = Far((unit("611", "GMC Sierra", "tractor", position=(-100.0, 500.0)),))
        result = Executor(world).execute({"version": 1, "request_id": "gmc-barn", "farm_id": "1",
                                          "task": "move_pair", "tractor_name": "gmc",
                                          "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual([c[0] for c in world.calls[1:]], ["navigate", "stop", "release"])   # no own drive
        world = Far((unit("611", "GMC Sierra", "tractor", position=(-100.0, 500.0)),))
        world.short = 60.0                                    # GIANTS ends 60 m short
        result = Executor(world).execute({"version": 1, "request_id": "gmc-barn", "farm_id": "1",
                                          "task": "move_pair", "tractor_name": "gmc",
                                          "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual([c[0] for c in world.calls[1:3]], ["navigate", "drive"])           # the rest, its own

    def test_the_navigation_test_option_keeps_every_leg_on_farmhands_own_driver(self):
        # AGENTS.md 11: standalone navigation is qualified with no GIANTS GOTO, AutoDrive, blocker
        # moving or teleport; the developer option (it replaced the hidden nav_proof.flag) does that.
        from adapters.world import StepFailed

        class Far(World):
            navigation_proof = True
            teleport_recovery = True
            refuse = False

            def navigate(self, vehicle_id, pose, owner):
                self.calls.append(("navigate",))

            def reposition(self, vehicle_id, owner):
                self.calls.append(("reposition",))

            def teleport(self, vehicle_id, owner, min_m=0.0, at=None):
                self.calls.append(("teleport",))

            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                if self.refuse:
                    self.calls.append(("drive",))
                    raise StepFailed("NO_ROUTE")
                return super().farm_drive(vehicle_id, target, owner, area, yaw)

        world = Far((unit("611", "GMC Sierra", "tractor", position=(-100.0, 500.0)),))   # a long trip
        request = {"version": 1, "request_id": "gmc-barn", "farm_id": "1", "task": "move_pair",
                   "tractor_name": "gmc", "target": {"type": "place", "name": "farm barn"}}
        result = Executor(world).execute(request)
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual([c[0] for c in world.calls[1:]], ["drive", "stop", "release"])     # its own drive only
        world = Far((unit("611", "GMC Sierra", "tractor", position=(-100.0, 500.0)),))
        world.refuse = True                                   # no route of its own: said, nothing else
        result = Executor(world).execute(request)
        self.assertEqual((result.status, result.reason), ("FAILED", "NO_ROUTE"), result)
        self.assertEqual({c[0] for c in world.calls} & {"navigate", "reposition", "teleport"}, set())

    def test_with_no_way_out_by_driving_the_teleport_recovery_frees_the_rig_when_allowed(self):
        # The user, 2026-10-30: "if farmhand still can not move it, it should teleport it and any
        # attached items to an open area close by".
        from adapters.world import StepFailed

        class Pocket(World):
            teleport_recovery = True
            freed = False

            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                self.calls.append(("drive",))
                if not self.freed:
                    raise StepFailed("NO_ROUTE")
                return super().farm_drive(vehicle_id, target, owner, area, yaw)

            def navigate(self, vehicle_id, pose, owner):
                raise StepFailed("NAVIGATE_BLOCKED_AT_START")

            def reposition(self, vehicle_id, owner):
                raise StepFailed("NO_ROUTE")

            def teleport(self, vehicle_id, owner, min_m=0.0, at=None):
                self.calls.append(("teleport", vehicle_id))
                self.freed = True

        world = Pocket((unit("611", "GMC Sierra", "tractor", position=(-696.0, 139.0)),))
        result = Executor(world).execute({"version": 1, "request_id": "gmc-barn", "farm_id": "1",
                                          "task": "move_pair", "tractor_name": "gmc",
                                          "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertIn(("teleport", "611"), world.calls)
        # A long trip (live bae54215f79642a0: GIANTS blocked at the start, no way onto open
        # ground, then minutes of Farmhand's own search across the river): straight to the
        # recovery, never Farmhand's own drive over the whole trip first.
        world = Pocket((unit("611", "GMC Sierra", "tractor", position=(-100.0, 500.0)),))
        Executor(world).execute({"version": 1, "request_id": "gmc-far", "farm_id": "1", "task": "move_pair",
                                 "tractor_name": "gmc", "target": {"type": "place", "name": "farm barn"}})
        names = [c[0] for c in world.calls]
        self.assertIn("teleport", names)
        self.assertNotIn("drive", names[:names.index("teleport")])
        world = Pocket((unit("611", "GMC Sierra", "tractor", position=(-696.0, 139.0)),))
        world.teleport_recovery = False                       # turned off in Settings: never
        result = Executor(world).execute({"version": 1, "request_id": "gmc-barn", "farm_id": "1",
                                          "task": "move_pair", "tractor_name": "gmc",
                                          "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("teleport", [c[0] for c in world.calls])

    def test_teleport_recovery_goes_farther_out_while_giants_still_cannot_start(self):
        # Live f08f97a7aef54634-1: set down 4 m off inside the same yard pocket, GIANTS blocked
        # again; the user, 2026-10-31: farther out each time, about 25 m then up to about 150 m.
        from adapters.world import StepFailed

        class DeepPocket(World):
            teleport_recovery = True
            out = None

            def farm_drive(self, vehicle_id, target, owner, area=0.0, yaw=None):
                raise StepFailed("NO_ROUTE")

            def navigate(self, vehicle_id, pose, owner):
                if self.out is None or self.out < 60:
                    raise StepFailed("NAVIGATE_BLOCKED_AT_START")
                self.items = {i: replace(item, position=(pose.x, pose.z)) for i, item in self.items.items()}

            def reposition(self, vehicle_id, owner):
                raise StepFailed("NO_ROUTE")

            def teleport(self, vehicle_id, owner, min_m=0.0, at=None):
                self.calls.append(("teleport", min_m))
                self.out = min_m

        world = DeepPocket((unit("611", "GMC Sierra", "tractor", position=(-100.0, 500.0)),))
        result = Executor(world).execute({"version": 1, "request_id": "gmc-far", "farm_id": "1", "task": "move_pair",
                                          "tractor_name": "gmc", "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual([c[1] for c in world.calls if c[0] == "teleport"], [0.0, 25.0, 60.0])
        world = DeepPocket((unit("611", "GMC Sierra", "tractor", position=(-100.0, 500.0)),))
        world.navigate = lambda *a: (_ for _ in ()).throw(StepFailed("NAVIGATE_BLOCKED_AT_START"))
        result = Executor(world).execute({"version": 1, "request_id": "gmc-far", "farm_id": "1", "task": "move_pair",
                                          "tractor_name": "gmc", "target": {"type": "place", "name": "farm barn"}})
        self.assertEqual(result.status, "FAILED")                              # never past the last step
        self.assertEqual(len([c for c in world.calls if c[0] == "teleport"]), 4)

    def test_a_rig_with_belted_cargo_is_driven_by_farmhand_never_autodrive(self):
        # GIANTS' agent refused the loaded rig (live d14230aa9fb24d30-3, abe8eb41ca6f4b5f-1):
        # Farmhand's own drive takes it on. AutoDrive installed, with a marker at the field, is
        # never asked (it is not Farmhand's navigation provider).
        from adapters.world import StepFailed

        class Loaded(World):
            def travel(self, vehicle_id, marker, owner):
                self.calls.append(("travel", marker))

            def deliver(self, vehicle_id, marker, owner):
                self.calls.append(("deliver", marker))

            def navigate(self, vehicle_id, pose, owner):
                self.calls.append(("navigate", (pose.x, pose.z)))
                raise StepFailed("NAVIGATE_BLOCKED_AT_START")

        def rig(position):
            truck = replace(unit("611", "GMC Sierra", "tractor", attached="620", position=position),
                            capabilities=frozenset({"move", "stop", "autodrive"}))
            deck = unit("620", "1124 Deckover", "implement", attached="611", belted=("622",), position=position)
            return truck, deck, unit("622", "Series 9", "tractor", belted_to="620", position=position)

        world = Loaded(rig((-100.0, 600.0)))
        result = StandaloneMove(world).execute({"request_id": "g", "farm_id": "1", "task": "move_pair",
                                                "tractor_name": "gmc", "target": {"type": "field_entrance", "field": 5}})
        self.assertEqual(result.status, "COMPLETE", result)
        names = [c[0] for c in world.calls]
        self.assertIn("navigate", names)                                   # GIANTS' agent first
        self.assertGreater(len(names) - 1 - names[::-1].index("drive"), names.index("navigate"))
        self.assertFalse({"travel", "deliver", "teleport"} & set(names))

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
