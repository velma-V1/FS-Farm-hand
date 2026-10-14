"""Off-PC checks for the local IPC frame protocol and FS25 snapshot mapping.

These test our parsing and mapping of frames shaped like the source-backed
observations in adapters/EVIDENCE.md. They do not prove that FS25, the named
pipe, or the game-side mod produce such frames.
"""
import copy
import json
import random
import unittest

from adapters.fs25 import (MULTIPLE_ATTACHMENTS, ObservationError, STOPPED_BELOW_KMH,
                           observation_from_snapshot)
from companion.executor import Executor, compatible, usable
from protocol.ipc import FrameError, FrameReader, parse_frame

TRACTORS = "TEST_TRACTOR_CATEGORY"
WAGONS = "TEST_WAGON_CATEGORY"
# Real FS25 store category names are UNVERIFIED; tests inject their own map.
CATEGORIES = {TRACTORS: "tractor", WAGONS: "forage_wagon"}


def vehicle(identifier, kind, x=0.0, z=0.0, **overrides):
    item = {"id": identifier, "farm_id": 1, "category": kind, "x": x, "z": z,
            "speed_kmh": 0.0, "entered": False, "ai_active": False,
            "has_autodrive": kind == TRACTORS, "ad_active": False if kind == TRACTORS else None,
            "attacher_vehicle_id": None, "implement_ids": [],
            "attacher_joints": ["TRAILER", "IMPLEMENT"] if kind == TRACTORS else [],
            "input_joints": ["TRAILER"] if kind == WAGONS else []}
    item.update(overrides)
    return item


def snapshot(vehicles, destinations=(), **overrides):
    frame = {"event": "snapshot", "protocol": 1, "seq": 7, "farm_id": 1,
             "player": {"x": 100.0, "z": 200.0}, "truncated": False, "skipped": 0,
             "vehicles": list(vehicles),
             "destinations": list(destinations) or [{"id": 3, "x": 101.0, "z": 199.0}]}
    frame.update(overrides)
    return frame


def line(frame):
    return (json.dumps(frame) + "\n").encode()


class FrameTests(unittest.TestCase):
    def test_valid_frames_parse(self):
        hello = {"event": "hello", "protocol": 1, "mod_version": "0.1.0.0", "autodrive_version": "3.0.1.4"}
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
                 line({**good, "protocol": 2}), line({**good, "farm_id": 0}),
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
        good = {"event": "hello", "protocol": 1, "mod_version": "1", "autodrive_version": None}
        results = reader.feed(b"x" * 500 + b"\n" + b"garbage\n" + line(good))
        self.assertIsInstance(results[0], FrameError)
        self.assertEqual(results[0].code, "LINE_TOO_LONG")
        self.assertIsInstance(results[1], FrameError)
        self.assertEqual(results[2], good)
        # An unterminated oversized line is dropped once, not buffered forever.
        self.assertEqual(len(reader.feed(b"y" * 300)), 1)
        self.assertEqual(reader.feed(b"y" * 300), [])
        self.assertEqual(reader.feed(b"\n" + line(good)), [good])


class MappingTests(unittest.TestCase):
    def test_compatible_pair_maps_but_stays_ineligible_until_gaps_close(self):
        frame = parse_frame(line(snapshot([vehicle(11, TRACTORS), vehicle(12, WAGONS, x=40)])))
        observed = observation_from_snapshot(frame, CATEGORIES)
        self.assertEqual(observed.farm_id, "1")
        self.assertEqual(observed.player, (100.0, 200.0))
        tractor, wagon = sorted(observed.equipment, key=lambda e: e.category, reverse=True)
        self.assertEqual((tractor.id, tractor.category, wagon.id, wagon.category),
                         ("11", "tractor", "12", "forage_wagon"))
        self.assertEqual(wagon.position, (40, 0.0))
        self.assertTrue(tractor.hitches & wagon.hitches)
        self.assertTrue(usable(tractor, "1") and usable(wagon, "1"))
        self.assertTrue(tractor.stopped and wagon.stopped)
        self.assertEqual([(d.id, d.position) for d in observed.destinations], [("3", (101.0, 199.0))])
        # Power, PTO, unattended approach and attach have no proven source (GAP /
        # UNVERIFIED), so real data must not yet produce an eligible pair.
        self.assertEqual((tractor.power_kw, wagon.required_power_kw), (0, 0))
        self.assertTrue(wagon.requires_pto)
        self.assertNotIn("move", tractor.capabilities)
        self.assertNotIn("attach", tractor.capabilities)
        self.assertFalse(compatible(tractor, wagon))

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
                 ({"category": "SOMETHING_ELSE"}, "category", "unknown"),
                 ({"has_autodrive": False, "ad_active": None}, "capabilities", frozenset())]
        for change, field, expected in cases:
            with self.subTest(change=change):
                frame = parse_frame(line(snapshot([vehicle(11, TRACTORS, **change)])))
                item, = observation_from_snapshot(frame, CATEGORIES).equipment
                self.assertEqual(getattr(item, field), expected)
                self.assertIsNone(item.reserved_by)

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
        hello = parse_frame(line({"event": "hello", "protocol": 1, "mod_version": "1",
                                  "autodrive_version": None}))
        duplicate = snapshot([vehicle(11, TRACTORS), vehicle(11, WAGONS)])
        duplicate_marker = snapshot([vehicle(11, TRACTORS)], destinations=[
            {"id": 3, "x": 0, "z": 0}, {"id": 3, "x": 1, "z": 1}])
        for frame in (hello, snapshot([], truncated=True), duplicate, duplicate_marker):
            with self.subTest(frame=frame.get("event")):
                with self.assertRaises(ObservationError):
                    observation_from_snapshot(frame, CATEGORIES)

    def test_missing_player_maps_to_none(self):
        frame = parse_frame(line(snapshot([], player=None)))
        self.assertIsNone(observation_from_snapshot(frame, CATEGORIES).player)

    def test_category_map_is_explicit_input(self):
        frame = parse_frame(line(snapshot([vehicle(11, TRACTORS)])))
        item, = observation_from_snapshot(frame, {}).equipment
        self.assertEqual(item.category, "unknown")
        with self.assertRaises(ObservationError):
            observation_from_snapshot(frame, {TRACTORS: "harvester"})

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
