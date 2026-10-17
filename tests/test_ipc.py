"""Off-PC checks for the local IPC frame protocol and FS25 snapshot mapping.

These test our parsing and mapping of frames shaped like the source-backed
observations in adapters/EVIDENCE.md. They do not prove that FS25, the named
pipe, or the game-side mod produce such frames.
"""
import copy
import json
import random
import unittest

from adapters.fs25 import (MULTIPLE_ATTACHMENTS, UNKNOWN_ATTACHMENT, ObservationError,
                           STOPPED_BELOW_KMH, observation_from_snapshot)
from dataclasses import replace

from companion.executor import Executor, compatible, power_sufficient, usable
from companion.hitch import plan_hitch
from protocol.ipc import MAX_LINE_BYTES, FrameError, FrameReader, parse_frame

TRACTORS = "TEST_TRACTOR_CATEGORY"
WAGONS = "TEST_WAGON_CATEGORY"
# Real FS25 store category names are UNVERIFIED; tests inject their own map.
# Forage wagons are identified by GIANTS' ForageWagon spec, not by this map.
CATEGORIES = {TRACTORS: "tractor"}


def vehicle(identifier, kind, x=0.0, z=0.0, **overrides):
    item = {"id": identifier, "farm_id": 1, "category": kind, "x": x, "z": z,
            "speed_kmh": 0.0, "entered": False, "ai_active": False,
            "has_autodrive": kind == TRACTORS, "ad_active": False if kind == TRACTORS else None,
            "attachment_observed": True, "attacher_vehicle_id": None, "implement_ids": [],
            "heading": 0.0, "ai_root": {"x": x, "z": z, "yaw": 0.0} if kind == TRACTORS else None,
            "output_joints": [{"index": 1, "type": "TRAILER", "x": 0.0, "z": -2.5, "free": True,
                               "pto_names": ["rearPto"]}] if kind == TRACTORS else [],
            "input_joint_poses": [{"index": 1, "type": "TRAILER", "x": x, "z": z + 4, "active": True,
                                   "pto_names": ["rearPto"]}] if kind == WAGONS else [],
            "forage_wagon": kind == WAGONS,
            "goto_supported": kind == TRACTORS,
            "self_propelled": kind == TRACTORS,
            # GIANTS torque values: capacity 0.9 * 1.0 * 2.0 = 1.8 >= 1.2 required.
            "motor": {"peak_torque": 1.0, "pto_rpm_ratio": 2.0} if kind == TRACTORS else None,
            "pto_torque": 1.2 if kind == WAGONS else 0.0}
    item.update(overrides)
    return item


def snapshot(vehicles, destinations=(), **overrides):
    frame = {"event": "snapshot", "protocol": 2, "seq": 7, "session": None, "farm_id": 1, "map_id": "MapUS",
             "player": {"x": 100.0, "z": 200.0}, "truncated": False, "skipped": 0,
             "vehicles": list(vehicles),
             "destinations": list(destinations) or [{"id": 3, "name": "Yard", "x": 101.0, "z": 199.0}],
             "places": []}
    frame.update(overrides)
    return frame


def line(frame):
    return (json.dumps(frame) + "\n").encode()


class FrameTests(unittest.TestCase):
    def test_valid_frames_parse(self):
        hello = {"event": "hello", "protocol": 2, "mod_version": "0.1.0.0", "autodrive_version": "3.0.1.4"}
        self.assertEqual(parse_frame(line(hello)), hello)
        frame = snapshot([vehicle(11, TRACTORS), vehicle(12, WAGONS)])
        self.assertEqual(parse_frame(line(frame)), frame)

    def test_invalid_frames_are_rejected(self):
        good = snapshot([vehicle(11, TRACTORS)])
        bad_vehicle = copy.deepcopy(good)
        del bad_vehicle["vehicles"][0]["speed_kmh"]
        extra = copy.deepcopy(good)
        extra["vehicles"][0]["vehicle_override"] = True
        cases = [b"", b"not json\n", b"[]\n", b"\xff\xfe\n",
                 # Only the duplicate key is wrong: last-value-wins would accept it.
                 b'{"event":"hello","protocol":2,"protocol":1,"mod_version":"1","autodrive_version":null}\n',
                 line({**good, "protocol": 1}), line({**good, "protocol": 3}), line({**good, "farm_id": 0}),
                 line({**good, "event": "command"}), line(bad_vehicle), line(extra),
                 line({**good, "seq": -1}), line({**good, "seq": True}),
                 line({**good, "vehicles": [vehicle(11, 'BAD"CATEGORY')]})]
        for raw in cases:
            with self.subTest(raw=raw[:60]):
                with self.assertRaises(FrameError):
                    parse_frame(raw)
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(token=token):
                raw = line(good).replace(b'"x": 0.0', b'"x": ' + token, 1)
                self.assertIn(token, raw)
                with self.assertRaises(FrameError):
                    parse_frame(raw)

    def test_reader_reassembles_arbitrary_chunks(self):
        frames = [snapshot([vehicle(i, TRACTORS)], seq=i) for i in range(5)]
        stream = b"".join(line(f) for f in frames)
        for seed in range(20):
            rng = random.Random(seed)
            reader, out, position = FrameReader(), [], 0
            while position < len(stream):
                step = rng.randint(1, 40)
                out += reader.feed(stream[position:position + step])
                position += step
            self.assertEqual(out, frames)

    def test_reader_isolates_bad_and_oversized_lines(self):
        reader = FrameReader(max_line_bytes=200)
        good = {"event": "hello", "protocol": 2, "mod_version": "1", "autodrive_version": None}
        results = reader.feed(b"x" * 500 + b"\n" + b"garbage\n" + line(good))
        self.assertIsInstance(results[0], FrameError)
        self.assertEqual(results[0].code, "LINE_TOO_LONG")
        self.assertIsInstance(results[1], FrameError)
        self.assertEqual(results[2], good)
        # An unterminated oversized line is dropped once, not buffered forever.
        self.assertEqual(len(reader.feed(b"y" * 300)), 1)
        self.assertEqual(reader.feed(b"y" * 300), [])
        self.assertEqual(reader.feed(b"\n" + line(good)), [good])

    def test_deeply_nested_input_is_a_controlled_error(self):
        # Below the 1 MiB line limit, but deep enough to exhaust the decoder's
        # recursion budget on any CPython version.
        good = {"event": "hello", "protocol": 2, "mod_version": "1", "autodrive_version": None}
        frame = snapshot([vehicle(11, TRACTORS)])
        deep_cases = [b"[" * 10_000 + b"]" * 10_000,
                      b"[" * 200_000 + b"]" * 200_000,
                      b'{"a":' * 100_000 + b"1" + b"}" * 100_000,
                      # Deep value inside an otherwise well-formed frame.
                      json.dumps(frame).replace('"vehicles": [', '"vehicles": [' + "[" * 100_000, 1)
                      .replace(']', ']' * 100_000 + ']', 1).encode()]
        for deep in deep_cases:
            with self.subTest(length=len(deep), start=deep[:12]):
                self.assertLess(len(deep), MAX_LINE_BYTES)
                with self.assertRaises(FrameError) as caught:
                    parse_frame(deep)
                self.assertIn(caught.exception.code, ("TOO_DEEP", "MALFORMED", "SCHEMA_VIOLATION"))
                reader = FrameReader()
                results = reader.feed(deep + b"\n" + line(good) + line(frame))
                self.assertEqual(len(results), 3)
                self.assertIsInstance(results[0], FrameError)
                self.assertEqual(results[1:], [good, frame])
                self.assertEqual(reader.feed(line(good)), [good])
        with self.assertRaises(FrameError) as caught:
            parse_frame(b"[" * 200_000 + b"]" * 200_000)
        self.assertEqual(caught.exception.code, "TOO_DEEP")


class MappingTests(unittest.TestCase):
    def test_real_shaped_pair_is_eligible_except_for_the_command_gate(self):
        frame = parse_frame(line(snapshot([vehicle(11, TRACTORS), vehicle(12, WAGONS, x=40)])))
        observed = observation_from_snapshot(frame, CATEGORIES)
        self.assertEqual(observed.farm_id, "1")
        self.assertEqual(observed.player, (100.0, 200.0))
        tractor, wagon = sorted(observed.equipment, key=lambda e: e.category, reverse=True)
        self.assertEqual((tractor.id, tractor.category, wagon.id, wagon.category),
                         ("11", "tractor", "12", "forage_wagon"))
        self.assertEqual(wagon.position, (40, 0.0))
        self.assertTrue(usable(tractor, "1") and usable(wagon, "1"))
        self.assertTrue(tractor.stopped and wagon.stopped)
        self.assertEqual([(d.id, d.name, d.position) for d in observed.destinations],
                         [("3", "Yard", (101.0, 199.0))])
        self.assertEqual(observed.map_id, "MapUS")
        # Power and PTO now come from GIANTS data and are sufficient/compatible.
        self.assertAlmostEqual(tractor.pto_torque_capacity, 1.8)
        self.assertEqual((tractor.required_pto_torque, wagon.required_pto_torque), (0.0, 1.2))
        self.assertTrue(power_sufficient(tractor, wagon))
        plan = plan_hitch(tractor, wagon)
        self.assertEqual((plan.tractor_joint, plan.wagon_joint), (1, 1))
        # Only the deliberate gate remains: no command channel grants move/attach.
        self.assertNotIn("move", tractor.capabilities)
        self.assertNotIn("attach", tractor.capabilities)
        self.assertFalse(compatible(tractor, wagon))
        self.assertTrue(compatible(replace(tractor, capabilities=frozenset({"move", "attach", "stop"})),
                                   wagon))

    def test_capability_needs_giants_goto_and_the_live_bridge_not_autodrive(self):
        move = {"move", "attach", "stop"}
        cases = [({}, True, move | {"autodrive"}),
                 ({"has_autodrive": False, "ad_active": None}, True, move),
                 ({"goto_supported": False}, True, set()),
                 ({"goto_supported": None}, True, set()),
                 ({"goto_supported": False, "has_autodrive": True}, True, set()),
                 ({}, False, set())]
        for change, ready, expected in cases:
            with self.subTest(change=change, bridge_ready=ready):
                frame = parse_frame(line(snapshot([vehicle(11, TRACTORS, **change)])))
                item, = observation_from_snapshot(frame, CATEGORIES, bridge_ready=ready).equipment
                self.assertEqual(set(item.capabilities), expected)

    def test_goto_supported_is_required_in_the_frame(self):
        record = vehicle(11, TRACTORS)
        del record["goto_supported"]
        with self.assertRaises(FrameError):
            parse_frame(line(snapshot([record])))
        with self.assertRaises(FrameError):
            parse_frame(line(snapshot([vehicle(11, TRACTORS, goto_supported="yes")])))

    def test_state_mapping(self):
        cases = [({"entered": True}, "occupied", True),
                 ({"ai_active": True}, "busy", True),
                 ({"ad_active": True}, "busy", True),
                 ({"speed_kmh": STOPPED_BELOW_KMH}, "stopped", False),
                 ({"speed_kmh": -STOPPED_BELOW_KMH * 2}, "stopped", False),
                 ({"speed_kmh": STOPPED_BELOW_KMH / 2}, "stopped", True),
                 ({"speed_kmh": None}, "available", False),
                 ({"entered": None}, "available", False),
                 ({"ai_active": None}, "available", False),
                 ({"ad_active": None}, "available", False),
                 ({"farm_id": 2}, "farm_id", "2"),
                 ({"category": None}, "category", "unknown"),
                 ({"category": "SOMETHING_ELSE"}, "category", "tractor"),     # recognised, not mapped
                 ({"category": "WHEELLOADERVEHICLES"}, "category", "unknown"),
                 ({"has_autodrive": False, "ad_active": None}, "capabilities", frozenset())]
        for change, field, expected in cases:
            with self.subTest(change=change):
                frame = parse_frame(line(snapshot([vehicle(11, TRACTORS, **change)])))
                item, = observation_from_snapshot(frame, CATEGORIES).equipment
                self.assertEqual(getattr(item, field), expected)
                self.assertIsNone(item.reserved_by)

    def test_unknown_booleans_never_read_as_free(self):
        # Observed False is free; None (unknown) is unavailable AND occupied/busy.
        base = vehicle(11, TRACTORS)
        for field, flag in (("entered", "occupied"), ("ai_active", "busy"), ("ad_active", "busy")):
            with self.subTest(field=field):
                item, = observation_from_snapshot(
                    parse_frame(line(snapshot([{**base, field: False}]))), CATEGORIES).equipment
                self.assertFalse(getattr(item, flag))
                self.assertTrue(item.available)
                self.assertTrue(usable(item, "1"))
                item, = observation_from_snapshot(
                    parse_frame(line(snapshot([{**base, field: None}]))), CATEGORIES).equipment
                self.assertTrue(getattr(item, flag))
                self.assertFalse(item.available)
                self.assertFalse(usable(item, "1"))
        # Without AutoDrive, ad_active is not applicable and does not block.
        item, = observation_from_snapshot(parse_frame(line(snapshot(
            [{**base, "has_autodrive": False, "ad_active": None}]))), CATEGORIES).equipment
        self.assertFalse(item.busy)
        self.assertTrue(item.available)

    def test_unknown_attachment_is_not_empty_attachment(self):
        observed_empty = parse_frame(line(snapshot([vehicle(12, WAGONS)])))
        item, = observation_from_snapshot(observed_empty, CATEGORIES).equipment
        self.assertIsNone(item.attached_to)
        self.assertTrue(item.available)
        unknown = parse_frame(line(snapshot([vehicle(12, WAGONS, attachment_observed=False)])))
        item, = observation_from_snapshot(unknown, CATEGORIES).equipment
        self.assertEqual(item.attached_to, UNKNOWN_ATTACHMENT)
        self.assertFalse(item.available)
        # The schema forbids attachment claims alongside an unobserved state.
        for claim in ({"attacher_vehicle_id": 11}, {"implement_ids": [13]}):
            with self.subTest(claim=claim):
                with self.assertRaises(FrameError):
                    parse_frame(line(snapshot([vehicle(12, WAGONS, attachment_observed=False, **claim)])))

    def test_attachment_claims_by_other_vehicles_are_honoured(self):
        # The wagon reports itself free, but a tractor reports it as its implement.
        frame = parse_frame(line(snapshot([vehicle(11, TRACTORS, implement_ids=[12]),
                                           vehicle(12, WAGONS)])))
        items = {e.id: e for e in observation_from_snapshot(frame, CATEGORIES).equipment}
        self.assertEqual((items["11"].attached_to, items["12"].attached_to), ("12", "11"))
        frame = parse_frame(line(snapshot([vehicle(11, TRACTORS), vehicle(13, TRACTORS),
                                           vehicle(12, WAGONS, attacher_vehicle_id=13)])))
        items = {e.id: e for e in observation_from_snapshot(frame, CATEGORIES).equipment}
        self.assertEqual((items["13"].attached_to, items["12"].attached_to), ("12", "13"))
        self.assertIsNone(items["11"].attached_to)
        # Conflicting claims are never resolved to a single partner.
        frame = parse_frame(line(snapshot([vehicle(11, TRACTORS, implement_ids=[12]),
                                           vehicle(12, WAGONS, attacher_vehicle_id=13),
                                           vehicle(13, TRACTORS)])))
        items = {e.id: e for e in observation_from_snapshot(frame, CATEGORIES).equipment}
        self.assertEqual(items["12"].attached_to, MULTIPLE_ATTACHMENTS)

    def test_attachment_mapping_is_bidirectional_and_fails_closed(self):
        frame = parse_frame(line(snapshot([
            vehicle(11, TRACTORS, implement_ids=[12]),
            vehicle(12, WAGONS, attacher_vehicle_id=11),
            vehicle(13, TRACTORS, implement_ids=[14, 15]),
            vehicle(16, WAGONS, attacher_vehicle_id=13, implement_ids=[17])])))
        items = {e.id: e for e in observation_from_snapshot(frame, CATEGORIES).equipment}
        self.assertEqual((items["11"].attached_to, items["12"].attached_to), ("12", "11"))
        self.assertEqual(items["13"].attached_to, MULTIPLE_ATTACHMENTS)
        self.assertEqual(items["16"].attached_to, MULTIPLE_ATTACHMENTS)
        self.assertNotIn(MULTIPLE_ATTACHMENTS, items)

    def test_untrustworthy_snapshots_are_rejected(self):
        hello = parse_frame(line({"event": "hello", "protocol": 2, "mod_version": "1",
                                  "autodrive_version": None}))
        duplicate = snapshot([vehicle(11, TRACTORS), vehicle(11, WAGONS)])
        duplicate_marker = snapshot([vehicle(11, TRACTORS)], destinations=[
            {"id": 3, "name": "a", "x": 0, "z": 0}, {"id": 3, "name": "b", "x": 1, "z": 1}])
        for frame in (hello, snapshot([], truncated=True), duplicate, duplicate_marker):
            with self.subTest(frame=frame.get("event")):
                with self.assertRaises(ObservationError):
                    observation_from_snapshot(frame, CATEGORIES)

    def test_missing_player_maps_to_none(self):
        frame = parse_frame(line(snapshot([], player=None)))
        self.assertIsNone(observation_from_snapshot(frame, CATEGORIES).player)

    def test_tractor_is_recognised_without_configuration(self):
        no_pto = [{"index": 1, "type": "TRAILER", "x": 0.0, "z": -2.5, "free": True, "pto_names": []}]
        unknown_pto = [{**no_pto[0], "pto_names": None}]
        cases = [({}, "tractor"),
                 ({"category": "ANY_MOD_CATEGORY"}, "tractor"),
                 ({"self_propelled": False}, "unknown"),           # towed, or no motor table
                 ({"self_propelled": None}, "unknown"),            # unreadable
                 ({"motor": None}, "unknown"),
                 ({"output_joints": []}, "unknown"),
                 ({"output_joints": no_pto}, "unknown"),           # no PTO output: not a tractor
                 ({"output_joints": unknown_pto}, "unknown"),
                 ({"category": None}, "unknown"),                  # loader exclusion needs the category
                 ({"category": "TELELOADERVEHICLES"}, "unknown"),
                 ({"category": "skidSteerVehicles"}, "unknown"),
                 ({"forage_wagon": True}, "forage_wagon")]
        for change, expected in cases:
            with self.subTest(change=change):
                frame = parse_frame(line(snapshot([vehicle(11, TRACTORS, **change)])))
                for categories in (None, {}):
                    item, = observation_from_snapshot(frame, categories).equipment
                    self.assertEqual(item.category, expected)
        # The optional local map still names tractors the rule would not.
        frame = parse_frame(line(snapshot([vehicle(11, TRACTORS, self_propelled=None)])))
        item, = observation_from_snapshot(frame, {TRACTORS: "tractor"}).equipment
        self.assertEqual(item.category, "tractor")
        record = vehicle(11, TRACTORS)
        del record["self_propelled"]
        with self.assertRaises(FrameError):
            parse_frame(line(snapshot([record])))

    def test_category_map_names_only_tractors(self):
        frame = parse_frame(line(snapshot([vehicle(11, TRACTORS)])))
        for bad in ({TRACTORS: "harvester"}, {WAGONS: "forage_wagon"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ObservationError):
                    observation_from_snapshot(frame, bad)

    def test_forage_wagon_is_identified_by_giants_specialization(self):
        cases = [({}, "forage_wagon"),                                   # no store category needed
                 ({"category": None}, "forage_wagon"),
                 ({"forage_wagon": False}, "unknown"),                    # an ordinary trailer
                 ({"category": TRACTORS}, "unknown")]                     # contradictory: never guessed
        for change, expected in cases:
            with self.subTest(change=change):
                frame = parse_frame(line(snapshot([vehicle(12, WAGONS, **change)])))
                item, = observation_from_snapshot(frame, CATEGORIES).equipment
                self.assertEqual(item.category, expected)

    def test_power_and_pto_mapping_preserves_unknown(self):
        frame = parse_frame(line(snapshot([
            vehicle(11, TRACTORS, motor=None,
                    output_joints=[{"index": 1, "type": "TRAILER", "x": 0.0, "z": -2.5, "free": True,
                                    "pto_names": None}]),
            vehicle(12, WAGONS, pto_torque=None,
                    input_joint_poses=[{"index": 1, "type": "TRAILER", "x": 0.0, "z": 4.0, "active": True,
                                        "pto_names": []}])])))
        items = {e.id: e for e in observation_from_snapshot(frame, CATEGORIES).equipment}
        tractor, wagon = items["11"], items["12"]
        self.assertIsNone(tractor.pto_torque_capacity)
        self.assertIsNone(wagon.required_pto_torque)
        self.assertIsNone(tractor.output_joints[0].pto_names)
        self.assertEqual(wagon.input_joints[0].pto_names, frozenset())   # observed: no PTO input
        self.assertFalse(power_sufficient(tractor, wagon))
        self.assertIsNone(plan_hitch(tractor, wagon))
        # A wagon that observably needs no PTO power is not rejected for it.
        no_need = replace(wagon, required_pto_torque=0.0)
        self.assertTrue(power_sufficient(tractor, no_need))
        self.assertIsNotNone(plan_hitch(tractor, no_need))

    def test_invalid_power_values_are_rejected_by_the_schema(self):
        for change in ({"motor": {"peak_torque": 0, "pto_rpm_ratio": 2.0}},
                       {"motor": {"peak_torque": 1.0, "pto_rpm_ratio": 0}},
                       {"motor": {"peak_torque": 1.0}},
                       {"pto_torque": -0.5}):
            with self.subTest(change=change):
                with self.assertRaises(FrameError):
                    parse_frame(line(snapshot([vehicle(11, TRACTORS, **change)])))

    def test_mapped_real_data_cannot_reach_movement(self):
        # Even a perfect-looking snapshot cannot start physical work today.
        class FrameWorld:
            def __init__(self, observed):
                self.observed, self.calls = observed, []

            def snapshot(self):
                return self.observed.equipment

            def player_position(self, farm_id):
                return self.observed.player

            def destinations(self):
                return self.observed.destinations

            def __getattr__(self, name):
                def record(*args):
                    self.calls.append(name)
                    raise AssertionError(name)
                return record

        frame = parse_frame(line(snapshot([vehicle(11, TRACTORS), vehicle(12, WAGONS, x=40)])))
        world = FrameWorld(observation_from_snapshot(frame, CATEGORIES))
        intent = {"version": 1, "request_id": "r-1", "task": "deliver_tractor_and_forage_wagon",
                  "farm_id": "1", "target": "player_at_acceptance"}
        result = Executor(world).execute(intent)
        self.assertEqual((result.status, result.reason), ("BLOCKED", "NO_ELIGIBLE_PAIR"))
        self.assertEqual(world.calls, [])
