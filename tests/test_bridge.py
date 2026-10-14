"""The companion half of the AC-001 bridge: command framing, sessions, and the
real executor running end to end over FS25World.

FakeGame stands in for mod/FarmhandBridge.lua: it parses each command file,
answers with protocol frames and moves records instantly. It has no physics;
it proves the companion's sequencing, gating and verification, not FS25.
"""
import copy
import json
import math
import sys
import threading
import time
import unittest
import unittest.mock
import xml.etree.ElementTree as ET

from adapters.world import StepFailed, StepUnverified
from companion import bridge
from companion.bridge import CommandLink, FS25World
from companion.executor import Executor
from companion.hitch import STAGING_DISTANCE_M
from protocol.ipc import encode_command, encode_session, parse_frame

TRACTOR_CATEGORY = "TRACTORS_TEST"
CATEGORIES = {TRACTOR_CATEGORY: "tractor"}


def record(identifier, kind, x, z):
    tractor = kind == "tractor"
    return {"id": identifier, "farm_id": 1, "category": TRACTOR_CATEGORY if tractor else "WAGONS",
            "x": x, "z": z, "speed_kmh": 0.0, "entered": False, "ai_active": False,
            "has_autodrive": tractor, "ad_active": False if tractor else None,
            "attachment_observed": True, "attacher_vehicle_id": None, "implement_ids": [], "heading": 0.0,
            "ai_root": {"x": x, "z": z, "yaw": 0.0} if tractor else None,
            "output_joints": [{"index": 1, "type": "TRAILER", "x": 0.0, "z": -2.5, "free": True,
                               "pto_names": ["rearPto"]}] if tractor else [],
            "input_joint_poses": [] if tractor else [{"index": 1, "type": "TRAILER", "x": x, "z": z + 4,
                                                       "active": True, "pto_names": ["rearPto"]}],
            "forage_wagon": not tractor,
            "motor": {"peak_torque": 1.0, "pto_rpm_ratio": 2.0} if tractor else None,
            "pto_torque": 0.0 if tractor else 1.2}


class FakeFiles:
    def __init__(self, game):
        self.game, self.files = game, {}

    def write(self, name, text):
        self.files[name] = text
        if name == "command.xml":
            self.game.receive(self)

    def remove(self, name):
        return self.files.pop(name, None) is not None


class FakeGame:
    """Snapshots every 5 ms on its own thread; commands answered by `faults`.

    Like the mod's single-threaded update() writing one FIFO pipe, frames are built
    and sent under one lock, so a snapshot sent after a result reflects that result."""

    def __init__(self, tractor_at=(0.0, 20.0), markers=None, faults=None, session_override=None):
        self.vehicles = {11: record(11, "tractor", *tractor_at), 12: record(12, "wagon", 0.0, -4.0)}
        self.markers = markers or {3: ("Yard", 100.0, 200.0), 4: ("Near wagon", 5.0, 30.0)}
        self.player = {"x": 101.0, "z": 199.0}
        self.faults = faults or {}
        self.session_override = session_override
        self.commands, self.lock, self.seq = [], threading.RLock(), 0
        self.link = CommandLink(FakeFiles(self))
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _emit(self, frame):
        parse_frame((json.dumps(frame) + "\n").encode())        # the real decoder's schema
        self.link.on_frame(frame)

    def _loop(self):
        while self.running:
            with self.lock:
                self.seq += 1
                session = self.link.session if self.session_override is None else self.session_override
                frame = {"event": "snapshot", "protocol": 1, "seq": self.seq, "session": session,
                         "farm_id": 1, "map_id": "MapUS", "player": self.player, "truncated": False,
                         "skipped": 0, "vehicles": copy.deepcopy(list(self.vehicles.values())),
                         "destinations": [{"id": i, "name": n, "x": x, "z": z}
                                          for i, (n, x, z) in self.markers.items()]}
                if not self.faults.get("silent_snapshots"):
                    self._emit(frame)
            time.sleep(0.005)

    def receive(self, files):
        with self.lock:
            self._receive(files)

    def _receive(self, files):
        command = ET.fromstring(files.files.pop("command.xml").split("\n", 1)[1]).attrib
        op, cid, session = command["op"], int(command["id"]), command["session"]
        self.commands.append(command)
        fault = self.faults.get(op)
        if fault == "unread":                     # never consumed: the file stays
            files.files["command.xml"] = "x"
            return
        if fault == "lost":                       # consumed, then nothing at all
            return
        result = {"event": "result", "protocol": 1, "session": session, "id": cid, "op": op}
        if fault and fault.startswith("REJECTED"):
            return self._emit({**result, "status": "REJECTED", "code": fault.split(":")[1]})
        self._emit({**result, "status": "ACCEPTED", "code": "ACCEPTED"})
        if fault == "no_terminal":
            return
        if fault:
            status, code = fault.split(":")
            return self._emit({**result, "status": status, "code": code})
        self._emit({**result, "status": "DONE", "code": self._apply(op, command)})

    def _apply(self, op, c):
        tractor, wagon = self.vehicles[11], self.vehicles[12]

        def place(v, x, z, yaw=None):
            v["x"], v["z"] = x, z
            if v["ai_root"] is not None:
                v["ai_root"] = {"x": x, "z": z, "yaw": v["ai_root"]["yaw"] if yaw is None else yaw}

        if op == "drive":
            _, x, z = self.markers[int(c["marker"])]
            if self.faults.get("drive_elsewhere"):
                x += 50
            place(tractor, x, z)
            if wagon["attacher_vehicle_id"] == 11:
                place(wagon, x, z - 8)
            return "AD_ENDED"
        if op == "stage":
            place(tractor, float(c["x"]), float(c["z"]), float(c["yaw"]))
            tractor["heading"] = float(c["yaw"])
            return "STAGE_ENDED"
        if op == "align":     # reverse straight back by the staging distance onto the joint
            root = tractor["ai_root"]
            place(tractor, root["x"] - STAGING_DISTANCE_M * math.sin(root["yaw"]),
                  root["z"] - STAGING_DISTANCE_M * math.cos(root["yaw"]))
            return "CANDIDATE"
        if op == "attach":
            if not self.faults.get("attach_other_joint"):
                tractor["output_joints"][0]["free"] = False
            tractor["implement_ids"], wagon["attacher_vehicle_id"] = [12], 11
            return "ATTACHED"
        return "STOPPED"


def intent(target="player_at_acceptance"):
    return {"version": 1, "request_id": "req-1", "task": "deliver_tractor_and_forage_wagon",
            "farm_id": "1", "target": target}


class CommandFramingTests(unittest.TestCase):
    def test_commands_are_strict_and_session_bound(self):
        text = encode_command("abc123", 7, "stage", {"tractor": 11, "x": 1.5, "z": -2, "yaw": 0.25})
        attrs = ET.fromstring(text.split("\n", 1)[1]).attrib
        self.assertEqual(attrs, {"session": "abc123", "id": "7", "op": "stage", "tractor": "11",
                                 "x": "1.5", "z": "-2.0", "yaw": "0.25"})
        bad = [("a b", 1, "stop", {"tractor": 1}), ("s", 0, "stop", {"tractor": 1}),
               ("s", 2 ** 31, "stop", {"tractor": 1}), ("s", True, "stop", {"tractor": 1}),
               ("s", 1, "detach", {"tractor": 1}), ("s", 1, "stop", {}),
               ("s", 1, "stop", {"tractor": 1, "marker": 2}), ("s", 1, "stop", {"tractor": "11"}),
               ("s", 1, "stage", {"tractor": 1, "x": float("nan"), "z": 0, "yaw": 0}),
               ("s", 1, "stage", {"tractor": 1, "x": 1e9, "z": 0, "yaw": 0}),
               ('s"/><x', 1, "stop", {"tractor": 1})]
        for args in bad:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    encode_command(*args)
        self.assertEqual(ET.fromstring(encode_session("abc", 3).split("\n", 1)[1]).attrib,
                         {"id": "abc", "beat": "3"})

    def test_results_from_other_sessions_and_ids_are_ignored(self):
        game = FakeGame(faults={"stop": "no_terminal"})
        self.addCleanup(game.stop)
        link = game.link
        link.on_frame({"event": "result", "protocol": 1, "session": "someone-else", "id": 1,
                       "op": "stop", "status": "DONE", "code": "STOPPED"})
        with unittest.mock.patch.dict(bridge.DONE_TIMEOUT_S, {"stop": 0.2}):
            with self.assertRaises(StepUnverified) as caught:
                link.command("stop", {"tractor": 11})
        self.assertEqual(caught.exception.code, "RESULT_TIMEOUT")
        ids = [int(c["id"]) for c in game.commands]
        self.assertEqual(ids, sorted(set(ids)))                    # never reused

    def test_acknowledgement_is_not_completion(self):
        cases = {"unread": (StepFailed, "NOT_ACCEPTED"), "lost": (StepUnverified, "NO_ACCEPTANCE"),
                 "REJECTED:BUSY": (StepFailed, "REJECTED_BUSY"), "no_terminal": (StepUnverified, "RESULT_TIMEOUT")}
        for fault, (error, code) in cases.items():
            with self.subTest(fault=fault):
                game = FakeGame(faults={"stop": fault})
                self.addCleanup(game.stop)
                with unittest.mock.patch.object(bridge, "ACCEPT_TIMEOUT_S", 0.2), \
                        unittest.mock.patch.dict(bridge.DONE_TIMEOUT_S, {"stop": 0.2}):
                    with self.assertRaises(error) as caught:
                        game.link.command("stop", {"tractor": 11})
                self.assertEqual(caught.exception.code, code)

    def test_silent_game_is_link_lost_not_success(self):
        game = FakeGame(faults={"stage": "no_terminal"})
        self.addCleanup(game.stop)
        game.link.fresh_snapshot()
        game.faults["silent_snapshots"] = True
        with unittest.mock.patch.object(bridge, "SNAPSHOT_TIMEOUT_S", 0.05):
            with self.assertRaises(StepUnverified) as caught:
                game.link.command("stage", {"tractor": 11, "x": 0, "z": 0, "yaw": 0})
        self.assertEqual(caught.exception.code, "LINK_LOST")

    def test_heartbeat_and_close(self):
        game = FakeGame()
        self.addCleanup(game.stop)
        game.link.heartbeat()
        game.link.heartbeat()
        self.assertIn('beat="1"', game.link.files.files["session.xml"])
        game.link.close()
        game.link.heartbeat()
        self.assertNotIn("session.xml", game.link.files.files)


class CapabilityTests(unittest.TestCase):
    def test_capabilities_only_with_this_companions_live_session(self):
        for override, expected in ((None, {"move", "attach", "stop"}), ("other-session", set())):
            with self.subTest(session=override):
                game = FakeGame(session_override=override)
                self.addCleanup(game.stop)
                if override:
                    game.session_override = override
                tractor = next(e for e in FS25World(game.link, CATEGORIES).snapshot() if e.id == "11")
                self.assertEqual(set(tractor.capabilities), expected)

    def test_unlinked_game_blocks_before_reservation(self):
        game = FakeGame(session_override="other-session")
        self.addCleanup(game.stop)
        result = Executor(FS25World(game.link, CATEGORIES)).execute(intent())
        self.assertEqual((result.status, result.reason), ("BLOCKED", "NO_ELIGIBLE_PAIR"))
        self.assertEqual(game.commands, [])

    def test_observation_trouble_before_reservation_only_blocks(self):
        game = FakeGame()
        self.addCleanup(game.stop)
        game.faults["silent_snapshots"] = True
        with unittest.mock.patch.object(bridge, "SNAPSHOT_TIMEOUT_S", 0.05):
            result = Executor(FS25World(game.link, CATEGORIES)).execute(intent())
        self.assertEqual((result.status, result.reason), ("BLOCKED", "STALE_SNAPSHOT"))
        self.assertEqual(game.commands, [])

    def test_stale_snapshot_never_reads_as_current(self):
        game = FakeGame()
        self.addCleanup(game.stop)
        world = FS25World(game.link, CATEGORIES)
        world.snapshot()
        game.faults["silent_snapshots"] = True
        with unittest.mock.patch.object(bridge, "SNAPSHOT_TIMEOUT_S", 0.05):
            with self.assertRaises(StepUnverified):
                world.snapshot()


class ProductionSequenceTests(unittest.TestCase):
    def run_ac001(self, target="player_at_acceptance", field_entrances=None, **game_args):
        game = FakeGame(**game_args)
        self.addCleanup(game.stop)
        world = FS25World(game.link, CATEGORIES)
        with unittest.mock.patch.object(bridge, "ACCEPT_TIMEOUT_S", 0.3), \
                unittest.mock.patch.dict(bridge.DONE_TIMEOUT_S, {op: 0.3 for op in bridge.DONE_TIMEOUT_S}):
            result = Executor(world, field_entrances=field_entrances).execute(intent(target))
        return game, world, result

    def ops(self, game):
        return [c["op"] for c in game.commands]

    def test_complete_run_near_the_wagon_skips_autodrive_pickup(self):
        game, world, result = self.run_ac001()
        self.assertEqual((result.status, result.reason), ("COMPLETE", "DELIVERY_OBSERVED"), result)
        self.assertEqual(result.equipment_ids, ("11", "12"))
        self.assertEqual(result.states, ("FIND", "RESERVE", "STAGE", "ALIGN_REVERSE", "ATTACH", "VERIFY",
                                         "DELIVER", "STOP", "RELEASE"))
        self.assertEqual(self.ops(game), ["stage", "align", "attach", "drive", "stop"])
        drive = game.commands[3]
        self.assertEqual(drive["marker"], "3")                      # "here": marker 3 near the player
        self.assertEqual((game.commands[1]["tj"], game.commands[1]["wj"]), ("1", "1"))
        self.assertEqual(world._reservations, {})                   # released only at the very end

    def test_far_tractor_uses_autodrive_first(self):
        game, _, result = self.run_ac001(tractor_at=(600.0, 600.0))
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(self.ops(game), ["drive", "stage", "align", "attach", "drive", "stop"])
        self.assertEqual(game.commands[0]["marker"], "4")           # the marker near the wagon
        self.assertEqual(result.states[2], "TRAVEL_NEAR")

    def test_explicit_targets_resolve_before_any_command(self):
        game, _, result = self.run_ac001({"type": "autodrive_marker", "marker_name": "Yard"})
        self.assertEqual(result.status, "COMPLETE")
        game, _, result = self.run_ac001({"type": "field_entrance", "field": 12},
                                         field_entrances={"MapUS": {12: "Missing gate"}})
        self.assertEqual((result.status, result.reason), ("BLOCKED", "DESTINATION_NOT_FOUND"))
        self.assertEqual(game.commands, [])

    def test_every_step_failure_stops_with_its_reason_and_goes_no_further(self):
        cases = [
            ({"drive": "FAILED:AD_STOPPED_WITH_ERROR"}, (600.0, 600.0), "FAILED", "AD_STOPPED_WITH_ERROR",
             ["drive", "stop"]),
            ({"stage": "FAILED:GOTO_NOT_STARTED"}, None, "FAILED", "GOTO_NOT_STARTED", ["stage", "stop"]),
            ({"align": "FAILED:VERTICAL_MISMATCH"}, None, "FAILED", "VERTICAL_MISMATCH",
             ["stage", "align", "stop"]),
            ({"align": "UNVERIFIED:UNKNOWN_OBSERVATION"}, None, "UNVERIFIED", "UNKNOWN_OBSERVATION",
             ["stage", "align", "stop"]),
            ({"attach": "UNVERIFIED:ATTACH_TIMEOUT"}, None, "UNVERIFIED", "ATTACH_TIMEOUT",
             ["stage", "align", "attach", "stop"]),
            ({"attach": "REJECTED:BUSY"}, None, "FAILED", "REJECTED_BUSY",
             ["stage", "align", "attach", "stop"]),
            ({"stage": "no_terminal"}, None, "UNVERIFIED", "RESULT_TIMEOUT", ["stage", "stop"]),
            ({"attach_other_joint": True}, None, "UNVERIFIED", "ATTACHMENT_MISMATCH",
             ["stage", "align", "attach", "stop"]),
        ]
        for faults, tractor_at, status, reason, ops in cases:
            with self.subTest(faults=faults):
                game, world, result = self.run_ac001(faults=faults, **({"tractor_at": tractor_at} if tractor_at else {}))
                self.assertEqual((result.status, result.reason), (status, reason), result)
                self.assertEqual(self.ops(game), ops)
                self.assertEqual(world._reservations, {})            # stop observed: released

    def test_delivery_and_arrival(self):
        # AutoDrive "ended" elsewhere: not arrival.
        game, world, result = self.run_ac001(faults={"drive_elsewhere": True})
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "DELIVERY_NOT_OBSERVED"))
        game, world, result = self.run_ac001(faults={"drive": "FAILED:TIMEOUT"})
        self.assertEqual((result.status, result.reason), ("FAILED", "TIMEOUT"))
        self.assertEqual(self.ops(game), ["stage", "align", "attach", "drive", "stop"])

    def test_unconfirmed_stop_keeps_the_reservation(self):
        game, world, result = self.run_ac001(faults={"align": "FAILED:STALLED", "stop": "FAILED:NOT_STOPPED"})
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "CLEANUP_NOT_CONFIRMED"))
        self.assertEqual(list(world._reservations.values()), [("1", ("11", "12"))])

    def test_replayed_request_sends_no_new_command(self):
        game = FakeGame()
        self.addCleanup(game.stop)
        executor = Executor(FS25World(game.link, CATEGORIES))
        first = executor.execute(intent())
        count = len(game.commands)
        self.assertEqual(executor.execute(intent()), first)
        self.assertEqual(len(game.commands), count)

    def test_moving_equipment_is_never_released(self):
        game = FakeGame()
        self.addCleanup(game.stop)
        world = FS25World(game.link, CATEGORIES)
        self.assertTrue(world.reserve(("11", "12"), "owner"))
        with game.lock:
            game.vehicles[11]["speed_kmh"] = 4.0
        with self.assertRaises(ValueError):
            world.release("owner")
        self.assertIn("owner", world._reservations)
        with game.lock:
            game.vehicles[11]["speed_kmh"] = 0.0
        world.release("owner")
        self.assertEqual(world._reservations, {})

    def test_wrong_reservation_never_reaches_the_game(self):
        game = FakeGame()
        self.addCleanup(game.stop)
        world = FS25World(game.link, CATEGORIES)
        self.assertTrue(world.reserve(("11", "12"), "owner"))
        self.assertFalse(world.reserve(("11", "12"), "other"))
        for call in (lambda: world.stage("11", "12", None, "intruder"),
                     lambda: world.travel("12", "3", "owner"),
                     lambda: world.align("11", "99", None, "owner")):
            with self.assertRaises(StepFailed):
                call()
        self.assertFalse(world.stop("11", "intruder"))
        self.assertEqual(game.commands, [])



class LocalIoTests(unittest.TestCase):
    def test_command_file_is_replaced_atomically_and_removal_reports_receipt(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            files = bridge.ModSettingsDir(Path(folder))
            files.write("command.xml", "one")
            files.write("command.xml", "two")
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ["command.xml"])
            self.assertEqual((Path(folder) / "command.xml").read_text(), "two")
            self.assertTrue(files.remove("command.xml"))
            self.assertFalse(files.remove("command.xml"))       # already consumed by the game

    @unittest.skipIf(sys.platform == "win32", "non-Windows refusal")
    def test_bridge_refuses_to_start_off_windows(self):
        with self.assertRaises(OSError):
            bridge.start_local_link()

    @unittest.skipUnless(sys.platform == "win32", "Windows named pipe")
    def test_pipe_server_receives_frames_written_like_the_mod(self):
        link = CommandLink(FakeFiles(None))
        path = r"\\.\pipe\fs_farmhand_test_" + link.session
        bridge.PipeServer(link, path).start()
        frame = {"event": "result", "protocol": 1, "session": link.session, "id": 1, "op": "stop",
                 "status": "DONE", "code": "STOPPED"}
        deadline = time.monotonic() + 10
        while True:
            try:
                pipe = open(path, "w", encoding="utf-8")          # what io.open(path, "w") does in FS25
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        with pipe:
            pipe.write('{"bad json\n' + json.dumps(frame) + "\n")
            pipe.flush()
        self.assertEqual(link._wait(lambda: link._results.get(1), 5), [("DONE", "STOPPED")])


class RunnerTests(unittest.TestCase):
    def test_config_and_structured_targets(self):
        import argparse
        import tempfile
        from pathlib import Path
        from companion import run
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "farmhand.json"
            path.write_text(json.dumps({"tractor_store_categories": ["TRACTORSL"],
                                        "field_entrances": {"MapUS": {"12": "Gate"}}}))
            self.assertEqual(run.load_config(path), ({"TRACTORSL": "tractor"}, {"MapUS": {12: "Gate"}}))
            path.write_text(json.dumps({"tractor_store_categories": "TRACTORSL"}))
            with self.assertRaises(ValueError):
                run.load_config(path)
        cases = [("here", None, "player_at_acceptance"),
                 ("marker", "Home Yard", {"type": "autodrive_marker", "marker_name": "Home Yard"}),
                 ("marker-id", "7", {"type": "autodrive_marker", "marker_id": "7"}),
                 ("field", "12", {"type": "field_entrance", "field": 12})]
        for kind, value, expected in cases:
            self.assertEqual(run.target(argparse.Namespace(kind=kind, value=value)), expected)
            intent_ = {"version": 1, "request_id": "ac001-x", "task": "deliver_tractor_and_forage_wagon",
                       "farm_id": "1", "target": expected}
            from companion.executor import VALIDATOR
            self.assertTrue(VALIDATOR.is_valid(intent_))
