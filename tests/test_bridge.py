"""The companion half of the AC-001 bridge: command framing, sessions, and the
real executor running end to end over FS25World.

FakeGame stands in for mod/FarmhandBridge.lua: it parses each command file,
answers with protocol frames and moves records instantly. It has no physics;
it proves the companion's sequencing, gating and verification, not FS25.
"""
import copy
import json
import math
import os
from pathlib import Path, PureWindowsPath
import sys
import threading
import time
import unittest
import unittest.mock
import xml.etree.ElementTree as ET

from adapters.world import StepFailed, StepUnverified
from companion import bridge
from companion.bridge import CommandLink, FS25World
from companion.executor import DELIVERY_STANDOFF_M, PLACE_STANDOFF_M, Executor
from companion.hitch import STAGING_DISTANCE_M
from protocol.model import Pose
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
            "kinds": [] if tractor else ["forage_wagon", "fill_unit", "dischargeable"],
            "name": None,
            "fill_units": [],
            "goto_supported": tractor,
            "self_propelled": tractor,
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

    # By default the tractor's joint starts beyond the local work radius (25 m) of the
    # wagon's, so GIANTS' GOTO (stage) brings it in; tractor_at=(0, 20) starts inside it.
    def __init__(self, tractor_at=(0.0, 40.0), markers=None, faults=None, session_override=None,
                 autodrive=True):
        self.vehicles = {11: record(11, "tractor", *tractor_at), 12: record(12, "wagon", 0.0, -4.0)}
        if not autodrive:      # AutoDrive not installed: no vehicle.ad, no markers
            self.vehicles[11].update(has_autodrive=False, ad_active=None)
        self.markers = ({} if not autodrive else
                        markers or {3: ("Yard", 100.0, 200.0), 4: ("Near wagon", 5.0, 30.0)})
        self.player = {"x": 101.0, "z": 199.0}
        self.places = [("Cow Barn (Medium)", -200.0, 50.0), ("Machine Shed", 300.0, -80.0)]
        self.faults = faults or {}
        self.session_override = session_override
        self.commands, self.lock, self.seq = [], threading.RLock(), 0
        self.held = None          # (result frame, op, command) of an operation still running
        self.link = CommandLink(FakeFiles(self))
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False

    def press(self, request_id, session=None):
        """The player's Farmhand key (mod/FarmhandBridge.lua requestHere)."""
        with self.lock:
            self._emit({"event": "request", "protocol": 3, "session": session or self.link.session,
                        "id": request_id, "action": "fetch_here"})

    def key(self, request_id, action):
        """A fixed key: fetch_here, stop, talk_start, talk_end."""
        with self.lock:
            self._emit({"event": "request", "protocol": 3, "session": self.link.session,
                        "id": request_id, "action": action})

    def pick(self, request_id, menu, choice):
        """A line picked in the in-game AI menu (GIANTS OptionDialog)."""
        with self.lock:
            self._emit({"event": "request", "protocol": 3, "session": self.link.session,
                        "id": request_id, "menu": menu, "choice": choice})

    def say(self, request_id, text):
        """Typed words from the in-game text field."""
        with self.lock:
            self._emit({"event": "request", "protocol": 3, "session": self.link.session,
                        "id": request_id, "text": text})

    def _emit(self, frame):
        parse_frame((json.dumps(frame) + "\n").encode())        # the real decoder's schema
        self.link.on_frame(frame)

    def _loop(self):
        while self.running:
            with self.lock:
                self.seq += 1
                session = self.link.session if self.session_override is None else self.session_override
                frame = {"event": "snapshot", "protocol": 3, "seq": self.seq, "session": session,
                         "farm_id": 1, "map_id": "MapUS", "player": self.player, "truncated": False,
                         "skipped": 0, "vehicles": copy.deepcopy(list(self.vehicles.values())),
                         "destinations": [{"id": i, "name": n, "x": x, "z": z}
                                          for i, (n, x, z) in self.markers.items()],
                         "places": [{"name": n, "x": x, "z": z} for n, x, z in self.places]}
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
        if isinstance(fault, list):               # one fault per call, then none
            fault = fault.pop(0) if fault else None
        if fault == "unread":                     # never consumed: the file stays
            files.files["command.xml"] = "x"
            return
        if fault == "lost":                       # consumed, then nothing at all
            return
        result = {"event": "result", "protocol": 3, "session": session, "id": cid, "op": op}
        if op == "stop" and self.held is not None:      # pre-empts the running operation
            self._emit({**self.held[0], "status": "FAILED", "code": "STOPPED"})
            self.held = None
        if fault and fault.startswith("REJECTED"):
            return self._emit({**result, "status": "REJECTED", "code": fault.split(":")[1]})
        self._emit({**result, "status": "ACCEPTED", "code": "ACCEPTED"})
        if fault == "no_terminal":
            return
        if fault == "hold":                       # still running until finish() or a stop
            self.held = (result, op, command)
            return
        if fault:
            status, code = fault.split(":")
            return self._emit({**result, "status": status, "code": code})
        self._emit({**result, "status": "DONE", "code": self._apply(op, command)})

    def _apply(self, op, c):
        if op in ("pay", "charge"):        # Farmhand's pay: recorded, no vehicle
            self.paid = getattr(self, "paid", []) + [(op, c)]
            return "PAY_SET" if op == "pay" else "CHARGED"
        tractor, wagon = self.vehicles[11], self.vehicles[12]

        def place(v, x, z, yaw=None):
            v["x"], v["z"] = x, z
            if v["ai_root"] is not None:
                v["ai_root"] = {"x": x, "z": z, "yaw": v["ai_root"]["yaw"] if yaw is None else yaw}

        def place_joint(ahead, lateral, turn):
            """The tractor with its joint (ahead, lateral) of the wagon's, turned `turn` deg."""
            eye, heading = wagon["input_joint_poses"][0], wagon["heading"]
            fx, fz = math.sin(heading), math.cos(heading)
            jx, jz = eye["x"] + ahead * fx + lateral * fz, eye["z"] + ahead * fz - lateral * fx
            yaw = heading + math.radians(turn)
            joint = tractor["output_joints"][0]
            dx, dz = Pose(0.0, 0.0, yaw).to_world(joint["x"], joint["z"])
            place(tractor, jx - dx, jz - dz, yaw)
            tractor["heading"] = yaw

        if op == "drive":
            _, x, z = self.markers[int(c["marker"])]
            if self.faults.get("drive_elsewhere"):
                x += 50
            place(tractor, x, z)
            if wagon["attacher_vehicle_id"] == 11:
                place(wagon, x, z - 8)
            return "AD_ENDED"
        if op == "teleport":       # teleport recovery: the rig to open ground 12 m aside
            self.teleports = getattr(self, "teleports", 0) + 1
            place(tractor, tractor["x"] + 12.0, tractor["z"])
            if wagon["attacher_vehicle_id"] == 11:
                place(wagon, wagon["x"] + 12.0, wagon["z"])
            return "TELEPORTED"
        if op == "farm_drive":     # Farmhand's own drive of the rig; the attached wagon behind
            x, z = float(c["x"]), float(c["z"])
            if self.faults.get("farm_drive_elsewhere"):
                x += 50
            yaw = math.atan2(x - tractor["x"], z - tractor["z"]) if (x, z) != (tractor["x"], tractor["z"]) \
                else tractor["heading"]
            if abs(float(c["yaw"])) <= 2 * math.pi:      # arriving at the asked heading
                yaw = float(c["yaw"])
            place(tractor, x, z, yaw)
            tractor["heading"] = yaw
            # Farmhand steers the vehicle root; GIANTS' AI root (live: the node its agent
            # steers) stands metres from it, so a delivery is checked at the root.
            tractor["ai_root"] = {"x": x + 3.5 * math.sin(yaw), "z": z + 3.5 * math.cos(yaw), "yaw": yaw}
            if wagon["attacher_vehicle_id"] == 11:
                place(wagon, x - 7 * math.sin(yaw), z - 7 * math.cos(yaw))
            return "ARRIVED"
        if op == "navigate":       # GIANTS GOTO; an attached wagon follows behind
            x, z, yaw = float(c["x"]), float(c["z"]), float(c["yaw"])
            if self.faults.get("navigate_elsewhere"):
                x += 50
            place(tractor, x, z, yaw)
            tractor["heading"] = yaw
            if wagon["attacher_vehicle_id"] == 11:
                place(wagon, x - 7 * math.sin(yaw), z - 7 * math.cos(yaw))
            return "GOTO_ENDED"
        if op == "stage":
            x, z, yaw = float(c["x"]) + self.faults.get("stage_offset", 0.0), float(c["z"]), float(c["yaw"])
            place(tractor, x, z, yaw)
            tractor["heading"] = yaw
            if "stage_end" in self.faults:     # GOTO ended here: (ahead, lateral, heading deg) of the joint
                place_joint(*self.faults["stage_end"])
            if self.faults.get("stage_ai_stays"):          # GIANTS reports the end, control lingers
                tractor["ai_active"] = True
            return "STAGE_ENDED"
        if op == "maneuver":   # lined up on the wagon's axis, 5 m ahead, unless told otherwise
            place_joint(*self.faults.get("maneuver_end", (5.0, 0.0, 0.0)))
            return "LINED_UP"
        if op == "align":     # the reverse ends with the tractor joint on the wagon joint
            yaw = tractor["ai_root"]["yaw"]
            joint, eye = tractor["output_joints"][0], wagon["input_joint_poses"][0]
            dx, dz = Pose(0.0, 0.0, yaw).to_world(joint["x"], joint["z"])
            place(tractor, eye["x"] - dx, eye["z"] - dz, yaw)
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
    def test_farm_drive_accepts_a_bounded_numeric_route_guide(self):
        args = {"tractor": 11, "x": 20, "z": 30, "area": 0, "yaw": 10, "via": "0,0;0,12;4,25"}
        attrs = ET.fromstring(encode_command("s", 1, "farm_drive", args).split("\n", 1)[1]).attrib
        self.assertEqual(attrs["via"], args["via"])
        with self.assertRaises(ValueError):
            encode_command("s", 1, "farm_drive", {**args, "via": '0,0\"/><bad'})

    def test_only_the_scenery_motions_take_the_optional_ghost_flag(self):
        def attrs(op, args):
            return ET.fromstring(encode_command("s", 1, op, args).split("\n", 1)[1]).attrib
        drive = {"tractor": 11, "x": 20, "z": 30, "area": 0, "yaw": 10}
        self.assertNotIn("gh", attrs("farm_drive", drive))                   # absent: off
        self.assertEqual(attrs("farm_drive", {**drive, "ghost": 1})["gh"], "1")
        self.assertEqual(attrs("farm_drive", {**drive, "ghost": 0})["gh"], "0")
        self.assertEqual(attrs("align", {"tractor": 1, "wagon": 2, "tractorJoint": 1, "wagonJoint": 1,
                                         "maxDistanceM": 10.0, "ghost": 1})["gh"], "1")
        for op, args in (("farm_drive", {**drive, "ghost": True}), ("farm_drive", {**drive, "ghost": 2}),
                         ("farm_drive", {**drive, "ghost": "1"}), ("farm_drive", {**drive, "ghost": 1.0}),
                         ("stage", {"tractor": 1, "x": 0, "z": 0, "yaw": 0, "ghost": 1}),     # never a GOTO
                         ("load", {"tractor": 1, "wagon": 2, "direction": 1, "ghost": 1}),
                         ("stop", {"tractor": 1, "ghost": 0})):
            with self.subTest(op=op, args=args):
                with self.assertRaises(ValueError):
                    encode_command("s", 1, op, args)

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
        link.on_frame({"event": "result", "protocol": 3, "session": "someone-else", "id": 1,
                       "op": "stop", "status": "DONE", "code": "STOPPED"})
        with unittest.mock.patch.dict(bridge.DONE_TIMEOUT_S, {"stop": 0.2}):
            with self.assertRaises(StepUnverified) as caught:
                link.command("stop", {"tractor": 11})
        self.assertEqual(caught.exception.code, "RESULT_TIMEOUT")
        ids = [int(c["id"]) for c in game.commands]
        self.assertEqual(ids, sorted(set(ids)))                    # never reused

    def test_acknowledgement_is_not_completion(self):
        # An unread stop is never withdrawn (live bb8dc55381b14a54-1: a stalled game resumed
        # a drive whose removed stop it never saw); it stays for the game and is unverified.
        cases = {"unread": (StepUnverified, "NO_ACCEPTANCE"), "lost": (StepUnverified, "NO_ACCEPTANCE"),
                 "REJECTED:BUSY": (StepFailed, "REJECTED_BUSY"), "no_terminal": (StepUnverified, "RESULT_TIMEOUT")}
        for fault, (error, code) in cases.items():
            with self.subTest(fault=fault):
                game = FakeGame(faults={"stop": fault})
                self.addCleanup(game.stop)
                with unittest.mock.patch.object(bridge, "STOP_ACCEPT_TIMEOUT_S", 0.2), \
                        unittest.mock.patch.dict(bridge.DONE_TIMEOUT_S, {"stop": 0.2}):
                    with self.assertRaises(error) as caught:
                        game.link.command("stop", {"tractor": 11})
                self.assertEqual(caught.exception.code, code)
                if fault == "unread":
                    self.assertIn("command.xml", game.link.files.files)

    def test_an_unread_ordinary_command_is_withdrawn(self):
        game = FakeGame(faults={"unpark": "unread"})
        self.addCleanup(game.stop)
        with unittest.mock.patch.object(bridge, "ACCEPT_TIMEOUT_S", 0.2):
            with self.assertRaises(StepFailed) as caught:
                game.link.command("unpark", {"tractor": 11})
        self.assertEqual(caught.exception.code, "NOT_ACCEPTED")
        self.assertNotIn("command.xml", game.link.files.files)

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
        for override, expected in ((None, {"move", "attach", "stop", "autodrive"}), ("other-session", set())):
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


NEAR = (0.0, 20.0)      # the tractor's joint starts inside the local work radius


class ProductionSequenceTests(unittest.TestCase):
    def run_ac001(self, target="player_at_acceptance", field_entrances=None, categories=CATEGORIES,
                  teleport=True, ghosting=None, **game_args):
        game = FakeGame(**game_args)
        self.addCleanup(game.stop)
        world = FS25World(game.link, categories)
        world.teleport_recovery = teleport
        if ghosting is not None:
            world.scenery_ghosting = ghosting
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
        self.assertEqual(result.states, ("FIND", "RESERVE", "STAGE", "LOCAL_MANEUVER", "ALIGN_REVERSE", "ATTACH", "VERIFY",
                                         "DELIVER", "STOP", "RELEASE"))
        # Delivered by Farmhand's own drive to marker 3, the one near the player ("here").
        self.assertEqual(self.ops(game), ["stage", "maneuver", "align", "attach", "farm_drive", "stop"])
        drive = game.commands[4]
        self.assertEqual((float(drive["x"]), float(drive["z"])), tuple(map(float, game.markers[3][1:])))
        self.assertEqual((game.commands[1]["tj"], game.commands[1]["wj"]), ("1", "1"))
        self.assertEqual(world._reservations, {})                   # released only at the very end

    def test_motions_pass_through_scenery_only_when_the_player_turned_it_on(self):
        # AGENTS.md 10: by default no command carries the scenery permission; with the player's
        # opt-in setting only the motions that may use it (protocol.ipc GHOST_OPS) carry gh="1".
        game, world, result = self.run_ac001()
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertFalse(world.scenery_ghosting)
        self.assertEqual([c for c in game.commands if "gh" in c], [])
        game, world, result = self.run_ac001(ghosting=True)
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual([(c["op"], c.get("gh")) for c in game.commands],
                         [("stage", None), ("maneuver", "1"), ("align", "1"), ("attach", None),
                          ("farm_drive", "1"), ("stop", None)])

    def test_no_configuration_is_needed(self):
        # No category map, no AutoDrive: the tractor is recognised from GIANTS data.
        game, world, result = self.run_ac001(categories=None, autodrive=False)
        self.assertEqual((result.status, result.reason), ("COMPLETE", "DELIVERY_OBSERVED"), result)
        self.assertEqual(self.ops(game), ["stage", "maneuver", "align", "attach", "farm_drive", "stop"])

    def test_far_tractor_uses_autodrive_first(self):
        game, _, result = self.run_ac001(tractor_at=(600.0, 600.0))
        self.assertEqual(result.status, "COMPLETE", result)
        self.assertEqual(self.ops(game), ["drive", "stage", "maneuver", "align", "attach", "farm_drive", "stop"])
        self.assertEqual(game.commands[0]["marker"], "4")           # the marker near the wagon
        self.assertEqual(result.states[2], "TRAVEL_NEAR")

    def test_a_drive_ended_against_something_on_the_way_restarts_from_there(self):
        # Live f4fb84d66f9e4191-1: the 3650 touched a traffic cone on the road, ENVELOPE_CONTACT.
        game, world, result = self.run_ac001(tractor_at=(600.0, 600.0), autodrive=False,
                                             faults={"farm_drive": ["FAILED:ENVELOPE_CONTACT"] * 2})
        self.assertEqual((result.status, result.reason), ("COMPLETE", "DELIVERY_OBSERVED"), result)
        self.assertEqual(self.ops(game)[:3], ["farm_drive"] * 3)
        game, world, result = self.run_ac001(tractor_at=(600.0, 600.0), autodrive=False,
                                             faults={"farm_drive": ["FAILED:ENVELOPE_CONTACT"] * 3})
        self.assertEqual(self.ops(game)[:4], ["farm_drive"] * 3 + ["navigate"])   # then GIANTS GOTO

    def test_a_rig_farmhand_cannot_move_is_teleported_clear_once_and_driven_again(self):
        # The user, 2026-10-30: if Farmhand still cannot move it, teleport the rig and everything
        # attached to open ground nearby (AGENTS.md 19: allow teleport recovery, a setting).
        stuck = {"farm_drive": ["FAILED:EGRESS_LIMIT"]}
        game, world, result = self.run_ac001(tractor_at=(600.0, 600.0), autodrive=False, faults=stuck)
        self.assertEqual((result.status, result.reason), ("COMPLETE", "DELIVERY_OBSERVED"), result)
        self.assertEqual(self.ops(game)[:3], ["farm_drive", "teleport", "farm_drive"])
        # Once per job: stuck again after it, the drive's own fallback as before.
        game, world, result = self.run_ac001(tractor_at=(600.0, 600.0), autodrive=False,
                                             faults={"farm_drive": ["FAILED:EGRESS_LIMIT"] * 2})
        self.assertEqual(self.ops(game)[:4], ["farm_drive", "teleport", "farm_drive", "navigate"])
        # Turned off: never.
        game, world, result = self.run_ac001(tractor_at=(600.0, 600.0), autodrive=False, faults=dict(stuck),
                                             teleport=False)
        self.assertNotIn("teleport", self.ops(game))
        # A drive that found no route is not "stuck": no teleport.
        game, world, result = self.run_ac001(tractor_at=(600.0, 600.0), autodrive=False,
                                             faults={"farm_drive": ["FAILED:NO_ROUTE"]})
        self.assertNotIn("teleport", self.ops(game))

    def test_farmhands_pay_goes_to_the_game_once_per_session_before_its_first_command(self):
        # The user, 2026-10-30: Farmhand's own pay (per hour, per job, per day, none) instead of
        # GIANTS' and AutoDrive's wages.
        game = FakeGame(autodrive=False)
        self.addCleanup(game.stop)
        world = FS25World(game.link, CATEGORIES)
        world.pay = lambda: (3, 750.0)              # per day, 750
        with unittest.mock.patch.object(bridge, "ACCEPT_TIMEOUT_S", 0.3), \
                unittest.mock.patch.dict(bridge.DONE_TIMEOUT_S, {op: 0.3 for op in bridge.DONE_TIMEOUT_S}):
            result = Executor(world).execute(intent())
            Executor(world).execute(intent())
        self.assertEqual(result.status, "COMPLETE", result)
        pays = [c for c in game.commands if c["op"] == "pay"]
        self.assertEqual(len(pays), 1)                                     # once this session
        self.assertEqual(game.commands[0]["op"], "pay")                    # before its first command
        self.assertEqual((float(pays[0]["fd"]), float(pays[0]["area"])), (3.0, 750.0))

    def test_without_autodrive_giants_goto_travels_and_delivers_here(self):
        # Farmhand's own drive travels to the wagon (live f27757a15ce94d2a-2: GIANTS GOTO never
        # moved the 3650 there) and delivers the pair; GIANTS GOTO travels only when it finds no route.
        for tractor_at, ops in (((0.0, 20.0), ["maneuver", "align", "attach", "farm_drive", "stop"]),
                                ((0.0, 40.0), ["stage", "maneuver", "align", "attach", "farm_drive", "stop"]),
                                # Travel ends at the approach: no GOTO; the maneuver lines up.
                                ((600.0, 600.0), ["farm_drive", "maneuver", "align", "attach", "farm_drive", "stop"])):
            with self.subTest(tractor_at=tractor_at):
                game, world, result = self.run_ac001(tractor_at=tractor_at, autodrive=False)
                self.assertEqual((result.status, result.reason), ("COMPLETE", "DELIVERY_OBSERVED"), result)
                self.assertEqual(self.ops(game), ops)
                if ops[0] == "farm_drive":
                    # onto an approach at its heading (live a0dca0862a424319-1); delivery: any heading
                    self.assertLessEqual(abs(float(game.commands[0]["yaw"])), math.pi)
                    self.assertGreater(abs(float(game.commands[-2]["yaw"])), 2 * math.pi)
                deliver = game.commands[-2]
                player = (game.player["x"], game.player["z"])
                goal = (float(deliver["x"]), float(deliver["z"]))
                self.assertAlmostEqual(math.dist(goal, player), DELIVERY_STANDOFF_M, places=6)
                self.assertNotIn("marker", deliver)
                self.assertEqual(world._reservations, {})

    def test_travel_to_a_far_wagon_falls_back_to_giants_goto_when_farmhand_finds_no_route(self):
        game, world, result = self.run_ac001(tractor_at=(600.0, 600.0), autodrive=False,
                                             faults={"farm_drive": "FAILED:NO_ROUTE"})
        self.assertEqual(self.ops(game)[:2], ["farm_drive", "navigate"])
        self.assertEqual(world._reservations, {})

    def test_goto_failures_without_autodrive_stop_and_go_no_further(self):
        # Farmhand's own drive found no route: GIANTS GOTO is tried, and its failure ends the job.
        cases = [({"navigate": "FAILED:NAVIGATE_STALLED"}, "FAILED", "NAVIGATE_STALLED"),
                 ({"navigate": "UNVERIFIED:UNKNOWN_OBSERVATION"}, "UNVERIFIED", "UNKNOWN_OBSERVATION"),
                 ({"navigate_elsewhere": True}, "UNVERIFIED", "DELIVERY_NOT_OBSERVED")]
        for faults, status, reason in cases:
            with self.subTest(faults=faults):
                game, world, result = self.run_ac001(faults={**faults, "farm_drive": "FAILED:NO_ROUTE"},
                                                     autodrive=False)
                self.assertEqual((result.status, result.reason), (status, reason), result)
                self.assertEqual(self.ops(game), ["stage", "maneuver", "align", "attach", "farm_drive", "navigate",
                                                  "stop"])
                self.assertEqual(world._reservations, {})

    def test_farmhands_own_drive_delivers_and_its_arrival_is_checked(self):
        game, world, result = self.run_ac001(faults={"farm_drive_elsewhere": True}, autodrive=False)
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "DELIVERY_NOT_OBSERVED"))
        game, world, result = self.run_ac001(faults={"farm_drive": "UNVERIFIED:UNKNOWN_OBSERVATION"}, autodrive=False)
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "UNKNOWN_OBSERVATION"))
        self.assertEqual(self.ops(game), ["stage", "maneuver", "align", "attach", "farm_drive", "stop"])

    def test_explicit_marker_without_autodrive_blocks_before_any_command(self):
        game, _, result = self.run_ac001({"type": "autodrive_marker", "marker_name": "Yard"}, autodrive=False)
        self.assertEqual((result.status, result.reason), ("BLOCKED", "DESTINATION_NOT_FOUND"))
        self.assertEqual(game.commands, [])

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
             ["stage", "maneuver", "align", "stop"]),
            ({"align": "UNVERIFIED:UNKNOWN_OBSERVATION"}, None, "UNVERIFIED", "UNKNOWN_OBSERVATION",
             ["stage", "maneuver", "align", "stop"]),
            # The last metres refused before they begin: stop, release, and say why.
            ({"align": "REJECTED:NOT_STOPPED"}, None, "FAILED", "REJECTED_NOT_STOPPED",
             ["stage", "maneuver", "align", "stop"]),
            # Local alignment's own bounded failures: stalled, the wagon moved, the
            # seated player took the controls, another AI job, diverging.
            ({"align": "FAILED:STALLED"}, None, "FAILED", "STALLED", ["stage", "maneuver", "align", "stop"]),
            ({"align": "FAILED:WAGON_MOVED"}, None, "FAILED", "WAGON_MOVED", ["stage", "maneuver", "align", "stop"]),
            ({"align": "FAILED:DRIVER_TOOK_OVER"}, None, "FAILED", "DRIVER_TOOK_OVER", ["stage", "maneuver", "align", "stop"]),
            ({"align": "FAILED:AI_ACTIVE"}, None, "FAILED", "AI_ACTIVE", ["stage", "maneuver", "align", "stop"]),
            ({"align": "FAILED:DIVERGED"}, None, "FAILED", "DIVERGED", ["stage", "maneuver", "align", "stop"]),
            ({"attach": "UNVERIFIED:ATTACH_TIMEOUT"}, None, "UNVERIFIED", "ATTACH_TIMEOUT",
             ["stage", "maneuver", "align", "attach", "stop"]),
            ({"attach": "REJECTED:BUSY"}, None, "FAILED", "REJECTED_BUSY",
             ["stage", "maneuver", "align", "attach", "stop"]),
            ({"stage": "no_terminal"}, None, "UNVERIFIED", "RESULT_TIMEOUT", ["stage", "stop"]),
            ({"attach_other_joint": True}, None, "UNVERIFIED", "ATTACHMENT_MISMATCH",
             ["stage", "maneuver", "align", "attach", "stop"]),
            # LOCAL_MANEUVER (the tractor starts inside the work radius): its refusals
            # and bounded failures stop, release, and never reverse or attach.
            ({"maneuver": "FAILED:KEEP_OUT"}, NEAR, "FAILED", "KEEP_OUT",
             ["maneuver", "stop"]),
            # Refused unmoved for where the tractor stands (no room to curve in): GOTO to
            # the staging pose straight ahead of the drawbar next, then the plain reverse
            # attaches (live 9c1beb5ad65c4255-1: the store's crowded spawn yard).
            ({"maneuver": "FAILED:LOCAL_PATH_LEAVES_AREA"}, NEAR, "COMPLETE", "DELIVERY_OBSERVED",
             ["maneuver", "stage", "align", "attach", "farm_drive", "stop"]),
            ({"maneuver": "FAILED:TURN_RADIUS_UNKNOWN"}, NEAR, "FAILED", "TURN_RADIUS_UNKNOWN", ["maneuver", "stop"]),
            ({"maneuver": "FAILED:STALLED"}, NEAR, "FAILED", "STALLED", ["maneuver", "stop"]),
            # Planned again from where it stands twice (MANEUVER_RETRIES), then the end.
            ({"maneuver": "FAILED:DIVERGED"}, NEAR, "FAILED", "DIVERGED", ["maneuver"] * 3 + ["stop"]),
            ({"maneuver": "FAILED:TIMEOUT"}, NEAR, "FAILED", "TIMEOUT", ["maneuver", "stop"]),
            ({"maneuver": "FAILED:ENVELOPE_EXCEEDED"}, NEAR, "FAILED", "ENVELOPE_EXCEEDED", ["maneuver", "stop"]),
            ({"maneuver": "FAILED:KEEP_OUT"}, NEAR, "FAILED", "KEEP_OUT", ["maneuver", "stop"]),
            ({"maneuver": "FAILED:WAGON_MOVED"}, NEAR, "FAILED", "WAGON_MOVED", ["maneuver", "stop"]),
            ({"maneuver": "FAILED:DRIVER_TOOK_OVER"}, NEAR, "FAILED", "DRIVER_TOOK_OVER", ["maneuver", "stop"]),
            ({"maneuver": "FAILED:AI_ACTIVE"}, NEAR, "FAILED", "AI_ACTIVE", ["maneuver", "stop"]),
            ({"maneuver": "UNVERIFIED:UNKNOWN_OBSERVATION"}, NEAR, "UNVERIFIED", "UNKNOWN_OBSERVATION",
             ["maneuver", "stop"]),
            ({"maneuver": "no_terminal"}, NEAR, "UNVERIFIED", "RESULT_TIMEOUT", ["maneuver", "stop"]),
        ]
        for faults, tractor_at, status, reason, ops in cases:
            with self.subTest(faults=faults):
                game, world, result = self.run_ac001(faults=faults, **({"tractor_at": tractor_at} if tractor_at else {}))
                self.assertEqual((result.status, result.reason), (status, reason), result)
                self.assertEqual(self.ops(game), ops)
                self.assertEqual(world._reservations, {})            # stop observed: released

    def test_a_failed_local_alignment_logs_the_joints(self):
        with self.assertLogs("farmhand", level="INFO") as logs:
            _, _, result = self.run_ac001(faults={"align": "FAILED:STALLED"})
        self.assertEqual((result.status, result.reason), ("FAILED", "STALLED"))
        end = next(line for line in logs.output if "ALIGN_REVERSE end:" in line)
        for part in ("tractor joint", "wagon joint", "gap", "ahead", "lateral", "heading"):
            self.assertIn(part, end)

    def test_delivery_and_arrival(self):
        # AutoDrive, tried when Farmhand's own drive found no route, "ended" elsewhere: not arrival.
        unrouted = {"farm_drive": "FAILED:NO_ROUTE"}
        game, world, result = self.run_ac001(faults={**unrouted, "drive_elsewhere": True})
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "DELIVERY_NOT_OBSERVED"))
        game, world, result = self.run_ac001(faults={**unrouted, "drive": "FAILED:TIMEOUT"})
        self.assertEqual((result.status, result.reason), ("FAILED", "TIMEOUT"))
        self.assertEqual(self.ops(game), ["stage", "maneuver", "align", "attach", "farm_drive", "drive", "stop"])

    def test_unconfirmed_stop_keeps_the_reservation(self):
        game, world, result = self.run_ac001(faults={"align": "FAILED:STALLED", "stop": "FAILED:NOT_STOPPED"})
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "CLEANUP_NOT_CONFIRMED"))
        self.assertEqual(list(world._reservations.values()), [("1", ("11", "12"))])

    def test_recovery_needs_a_done_stop_and_an_observed_rest(self):
        game, world, result = self.run_ac001(faults={"align": "FAILED:STALLED", "stop": "FAILED:NOT_STOPPED"})
        owner, = world.retained()
        del game.faults["stop"]
        game.vehicles[11]["speed_kmh"] = 5.0          # the game says DONE, the snapshot says moving
        self.assertFalse(world.recover(owner))
        self.assertEqual(world.retained(), (owner,))
        game.vehicles[11]["speed_kmh"] = 0.0
        self.assertTrue(world.recover(owner))
        self.assertEqual(world.retained(), ())
        self.assertTrue(world.recover(owner))         # nothing left to recover

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
        world.RELEASE_SETTLE_S = 0.6                  # still rolling after it: refused
        with self.assertRaises(ValueError):
            world.release("owner")
        self.assertIn("owner", world._reservations)
        # Rolling to rest within it (live 4902eb24a6264922-1: released mid-roll after a stop): released.
        world.RELEASE_SETTLE_S = 5.0

        def rest():
            with game.lock:
                game.vehicles[11]["speed_kmh"] = 0.0
        threading.Timer(0.5, rest).start()
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
    def test_a_second_companion_cannot_host_the_pipe(self):
        path = r"\\.\pipe\fs_farmhand_test_single_" + CommandLink(FakeFiles(None)).session
        bridge.PipeServer(CommandLink(FakeFiles(None)), path).start()
        with self.assertRaises(OSError):
            bridge.PipeServer(CommandLink(FakeFiles(None)), path).start()

    @unittest.skipUnless(sys.platform == "win32", "Windows named pipe")
    def test_pipe_server_receives_frames_written_like_the_mod(self):
        link = CommandLink(FakeFiles(None))
        path = r"\\.\pipe\fs_farmhand_test_" + link.session
        bridge.PipeServer(link, path).start()
        frame = {"event": "result", "protocol": 3, "session": link.session, "id": 1, "op": "stop",
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


class ServiceTests(unittest.TestCase):
    """The windowless companion: in-game key -> AC-001 "here" -> in-game notice."""

    def start(self, voice=None, **game_args):
        from companion import service
        game = FakeGame(**game_args)
        self.addCleanup(game.stop)
        world = game.link_world = FS25World(game.link)
        notices = []
        original = game.link.notice
        self.progress = []                  # WORKING_<step> milestones, asserted on their own
        game.link.notice = lambda code: ((self.progress if code.startswith("WORKING_") else notices).append(code),
                                         original(code))
        stop = threading.Event()
        self.addCleanup(stop.set)
        patches = [unittest.mock.patch.object(bridge, "ACCEPT_TIMEOUT_S", 0.3),
                   unittest.mock.patch.dict(bridge.DONE_TIMEOUT_S, {op: 0.3 for op in bridge.DONE_TIMEOUT_S}),
                   unittest.mock.patch.object(service, "RENEW_AFTER_S", 0.2)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        thread = threading.Thread(target=service.serve, args=(world,),
                                  kwargs={"should_stop": stop.is_set, **(voice or {})}, daemon=True)
        thread.start()
        return game, notices

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            time.sleep(0.01)

    def session_notice(self, game):
        """The newest (token, id) in the session file."""
        attrs = ET.fromstring(game.link.files.files["session.xml"].split("\n", 1)[1]).attrib
        newest = attrs["notices"].split()[-1].split(":")
        return newest[1], int(newest[0])

    def test_key_press_fetches_the_pair_and_tells_the_player(self):
        game, notices = self.start(autodrive=False)
        game.press(1)
        self.wait_for(lambda: len(notices) >= 2)
        self.assertEqual(notices, ["ACCEPTED", "COMPLETE"])
        self.assertEqual(self.progress, ["WORKING_STAGE", "WORKING_LOCAL_MANEUVER", "WORKING_DELIVER"])
        self.assertEqual(self.ops(game), ["stage", "maneuver", "align", "attach", "farm_drive", "stop"])
        self.assertEqual(self.session_notice(game), ("COMPLETE", 5))

    def ops(self, game):
        # Farmhand's pay, sent before each job, is settings plumbing (its own test below).
        return [c["op"] for c in game.commands if c["op"] not in ("pay", "charge")]

    def test_while_a_job_runs_presses_are_queued_and_status_and_stop_answer_at_once(self):
        # A press while busy waits its turn (the user, 2026-10-31: "farmhand needs a queue");
        # STOP empties the waiting list, so nothing starts after it.
        game, notices = self.start(autodrive=False, faults={"align": "hold"})
        game.press(1)
        self.wait_for(lambda: game.held is not None)            # reversing to the wagon
        game.press(2)
        self.wait_for(lambda: notices[-1:] == ["QUEUED"])
        game.say(3, "Farmhand, what are you doing?")
        # The held reverse moves nothing: said so (an accepted command is not motion).
        self.wait_for(lambda: notices[-1:] == ["STATUS_WAITING_ALIGN_REVERSE"])
        game.say(4, "STOP!")
        self.wait_for(lambda: notices[-1:] == ["CANCELLED"])
        self.assertEqual(notices, ["ACCEPTED", "QUEUED", "STATUS_WAITING_ALIGN_REVERSE", "STOPPING", "CANCELLED"])
        ops = self.ops(game)
        self.assertEqual(ops[:3], ["stage", "maneuver", "align"])
        self.assertNotIn("attach", ops)                          # nothing after the stop
        self.assertIn("stop", ops)
        self.assertEqual(game.link_world._reservations, {})     # stopped, then released
        game.say(5, "what are you doing")
        self.wait_for(lambda: notices[-1:] == ["STATUS_LAST_CANCELLED"])

    def test_come_here_and_take_this_back_to_the_barn_move_the_hitched_pair(self):
        game, notices = self.start(autodrive=False)
        game.say(1, "bring me a tractor and forage wagon")
        self.wait_for(lambda: notices[-1:] == ["COMPLETE"])
        game.player = {"x": -40.0, "z": 120.0}                  # the player walked off
        before = len(game.commands)
        game.say(2, "come here")
        self.wait_for(lambda: notices[-1:] == ["COMPLETE_MOVE"])
        self.assertEqual(notices[2:], ["ACCEPTED_MOVE", "COMPLETE_MOVE"])
        self.assertEqual([c["op"] for c in game.commands[before:]], ["farm_drive", "stop"])
        goal = game.commands[before]
        self.assertAlmostEqual(math.dist((float(goal["x"]), float(goal["z"])), (-40.0, 120.0)),
                               DELIVERY_STANDOFF_M, places=4)
        before = len(game.commands)
        game.say(3, "take this back to the barn")
        self.wait_for(lambda: notices[-1:] == ["COMPLETE_MOVE"] and len(notices) == 6)
        goal = game.commands[before]
        barn = next((x, z) for n, x, z in game.places if "Barn" in n)
        self.assertAlmostEqual(math.dist((float(goal["x"]), float(goal["z"])), barn), PLACE_STANDOFF_M, places=4)
        game.say(4, "take this to the silo")                    # no such place: nothing moves
        self.wait_for(lambda: notices[-1:] == ["BLOCKED_DESTINATION_NOT_FOUND"])
        self.assertEqual(game.link_world._reservations, {})

    def test_push_to_talk_fetches_the_pair_and_answers_aloud(self):
        from companion.voice import read_wav
        fixtures = Path(__file__).resolve().parent / "fixtures" / "voice"

        class Microphone:
            def start(self):
                pass

            def stop(self):
                return read_wav(fixtures / "fetch.wav")

        class Transcriber:
            def transcribe(self, pcm, places=()):
                return "Bring me a tractor and forage wagon." if pcm == read_wav(fixtures / "fetch.wav") else ""

        said = []
        speaker = type("Speaker", (), {"say": lambda self, text: said.append(text)})()
        game, notices = self.start(autodrive=False, voice={"microphone": Microphone, "transcriber": Transcriber,
                                                           "speaker": speaker})
        game.key(1, "talk_start")
        self.wait_for(lambda: notices == ["LISTENING"])
        game.key(2, "talk_end")
        self.wait_for(lambda: notices[-1:] == ["COMPLETE"])
        self.assertEqual(notices, ["LISTENING", "ACCEPTED", "COMPLETE"])
        self.assertEqual(said, ["Fetching a tractor and the implement.", "Tractor and implement delivered."])
        game.say(3, "what are you doing")                    # typed: answered on screen only
        self.wait_for(lambda: notices[-1:] == ["STATUS_LAST_COMPLETE"])
        self.assertEqual(len(said), 2)

    def test_without_voice_set_up_push_to_talk_is_unavailable_and_text_works(self):
        game, notices = self.start(autodrive=False)
        game.key(1, "talk_start")
        game.key(2, "talk_end")
        self.wait_for(lambda: notices == ["VOICE_UNAVAILABLE"])
        game.say(3, "stop")
        self.wait_for(lambda: notices[-1:] == ["NOTHING_TO_STOP"])

    def test_a_stop_that_wins_the_race_with_the_next_step_still_stops_it(self):
        # The requester's stop reaches the game before DELIVER's command: the game
        # has nothing to pre-empt, so the link must refuse the drive itself.
        from companion import coordinator as coordination
        game = FakeGame(autodrive=False)
        self.addCleanup(game.stop)
        world = FS25World(game.link)
        notices = []
        holder = {}

        class StopAtDeliver(Executor):
            def execute(self, intent, cancel=None, on_state=None, job=None):
                def entered(state):
                    on_state(state)
                    if state == "DELIVER":
                        holder["c"].stop()
                        deadline = time.monotonic() + 5
                        while not any(c["op"] == "stop" for c in game.commands):
                            assert time.monotonic() < deadline
                            time.sleep(0.01)
                return super().execute(intent, cancel=cancel, on_state=entered)

        with unittest.mock.patch.object(bridge, "ACCEPT_TIMEOUT_S", 0.3), \
                unittest.mock.patch.dict(bridge.DONE_TIMEOUT_S, {op: 0.3 for op in bridge.DONE_TIMEOUT_S}):
            c = holder["c"] = coordination.Coordinator(world, StopAtDeliver(world),
                                                       lambda code, spoken=False: notices.append(code),
                                                       farm=lambda: "1", session=lambda: game.link.session)
            c.start("fetch_pair", "here")
            c.wait(10)
        self.assertEqual(notices[-1], "CANCELLED")
        ops = [x["op"] for x in game.commands]
        self.assertEqual(ops, ["stage", "maneuver", "align", "attach", "stop", "stop"])   # never the drive
        self.assertEqual(world._reservations, {})

    def test_repeated_stop_presses_keep_the_cancel_confirmed(self):
        game, notices = self.start(autodrive=False, faults={"align": "hold"})
        game.press(1)
        self.wait_for(lambda: game.held is not None)
        for request_id in range(2, 6):
            game.key(request_id, "stop")
        self.wait_for(lambda: notices[-1:] == ["CANCELLED"])
        self.assertEqual([c["op"] for c in game.commands].count("stop"), 2)    # one requester stop + cleanup
        self.assertEqual(game.link_world._reservations, {})

    def test_stop_is_answered_while_the_model_thinks(self):
        asked, release = [], threading.Event()

        class SlowModel:
            def interpret(self, text):
                asked.append(text)
                release.wait(5)
                return {"version": 1, "command": "status"}

        game, notices = self.start(autodrive=False, voice={"ai": SlowModel()})
        game.say(1, "how far along are we, mate")             # the rules do not know it
        self.wait_for(lambda: asked == ["how far along are we, mate"])
        started = time.monotonic()
        game.key(2, "stop")
        self.wait_for(lambda: notices[-1:] == ["NOTHING_TO_STOP"])
        self.assertLess(time.monotonic() - started, 1.0)       # not behind the model
        release.set()
        time.sleep(0.3)
        self.assertEqual(notices, ["NOTHING_TO_STOP"])           # the answer came after a STOP: void
        game.say(3, "how far along are we, then")               # no stop in between: used
        self.wait_for(lambda: notices[-1:] == ["STATUS_IDLE"])

    def test_refused_words_never_reach_the_model(self):
        asked = []
        model = type("Model", (), {"interpret": lambda self, text: asked.append(text) or
                                   {"version": 1, "command": "fetch_pair"}})()
        game, notices = self.start(autodrive=False, voice={"ai": model})
        for n, text in enumerate(["don't bring the tractor and wagon", "where is the tractor and wagon",
                                  "never bring it back"], start=1):
            game.say(n, text)
        self.wait_for(lambda: notices == ["NOT_UNDERSTOOD"] * 3)
        self.assertEqual(asked, [])
        self.assertEqual(game.commands, [])

    def menu(self, game):
        attrs = ET.fromstring(game.link.files.files["menu.xml"].split("\n", 1)[1]).attrib
        return int(attrs["id"]), [attrs[f"o{n}"] for n in range(1, int(attrs["count"]) + 1)]

    def test_the_settings_screen_opens_from_the_command_window_and_a_pick_applies_at_once(self):
        # The user, 2026-10-30: a settings section reached from Farmhand's command window.
        import tempfile as tmp
        from companion import ai
        folder = Path(tmp.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))

        def line(lines, start):
            return next(n for n, text in enumerate(lines, start=1) if text.startswith(start))

        with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": str(folder)}):
            game, notices = self.start(autodrive=False)
            game.say(1, "settings")                                     # typed in the command window
            self.wait_for(lambda: "MENU" in notices)
            menu_id, lines = self.menu(game)
            self.assertEqual(lines[0], "Pay: per hour at GIANTS' rate")
            self.assertIn("Teleport a stuck rig: on", lines)
            self.assertIn("Pass through scenery in tight spots: off", lines)
            self.assertIn("Spending limit: none", lines)
            self.assertFalse(game.link_world.scenery_ghosting)             # off until the player says
            self.assertIsNone(game.link_world.spending_limit)
            game.pick(2, menu_id, line(lines, "Teleport"))              # teleport recovery: off
            self.wait_for(lambda: notices.count("MENU") == 2)
            self.assertFalse(ai.Settings().teleport_recovery())
            self.wait_for(lambda: game.link_world.teleport_recovery is False)  # the running world too
            menu_id, lines = self.menu(game)
            self.assertIn("Teleport a stuck rig: off", lines)
            game.pick(3, menu_id, line(lines, "Pay"))
            self.wait_for(lambda: notices.count("MENU") == 3)
            menu_id, lines = self.menu(game)
            game.pick(4, menu_id, lines.index("per day") + 1)
            self.wait_for(lambda: notices.count("MENU") == 4)
            menu_id, lines = self.menu(game)
            game.pick(5, menu_id, lines.index("1,000") + 1)
            self.wait_for(lambda: notices.count("MENU") == 5)
            self.assertEqual(ai.Settings().pay(), (3, 1000.0))
            menu_id, lines = self.menu(game)
            self.assertEqual(lines[0], "Pay: 1,000 per day")
            game.pick(6, menu_id, line(lines, "Pass through scenery"))   # pass through scenery: on
            self.wait_for(lambda: notices.count("MENU") == 6)
            self.assertTrue(ai.Settings().scenery_ghosting())
            self.wait_for(lambda: game.link_world.scenery_ghosting is True)  # applied to the running world
            menu_id, lines = self.menu(game)
            self.assertIn("Pass through scenery in tight spots: on", lines)
            game.pick(7, menu_id, line(lines, "Spending limit"))
            self.wait_for(lambda: notices.count("MENU") == 7)
            menu_id, lines = self.menu(game)
            game.pick(8, menu_id, lines.index("25,000") + 1)
            self.wait_for(lambda: notices.count("MENU") == 8)
            self.assertEqual(ai.Settings().spending_limit(), 25000.0)
            self.wait_for(lambda: game.link_world.spending_limit == 25000.0)  # every purchase from now on
            self.assertIn("Spending limit: 25,000 a purchase", self.menu(game)[1])

    def test_the_ai_menu_is_offered_once_then_on_request_and_a_pick_is_validated(self):
        import tempfile as tmp
        from companion import ai
        try:
            from tests.test_ai import GOOD, FakeOllama
        except ImportError:                      # unittest discover -s tests
            from test_ai import GOOD, FakeOllama
        fake = FakeOllama(answers=GOOD)
        self.addCleanup(fake.close)
        folder = Path(tmp.mkdtemp())
        game = None
        brain = ai.AI(ai.Settings(folder / "settings.json"), lambda code: game.link.notice(code),
                      ollama=ai.Ollama(fake.url, timeout=2), folder=folder)
        game, notices = self.start(autodrive=False, voice={"ai": brain})
        self.wait_for(lambda: "MENU" in notices)                       # first link, no choice yet
        menu_id, lines = self.menu(game)
        self.assertEqual(lines[0], "qwen3:4b (Ollama, 2.5 GB 4.0B Q4_K_M)")
        self.assertIn("Continue without AI", lines)
        game.pick(1, menu_id + 1, 1)                                    # a stale/unknown menu: ignored
        game.pick(2, menu_id, 0)                                        # dialog cancelled: nothing
        time.sleep(0.2)
        self.assertEqual(notices, ["MENU"])
        game.pick(3, menu_id, 1)
        self.wait_for(lambda: notices[-1:] == ["AI_READY"])
        self.assertEqual(brain.settings.ai(), {"provider": "ollama", "model": "qwen3:4b"})
        game.say(4, "choose AI model")
        self.wait_for(lambda: notices.count("MENU") == 2)
        game.key(5, "settings")
        self.wait_for(lambda: notices.count("MENU") == 3)
        self.assertGreater(self.menu(game)[0], menu_id)

    def test_a_saved_model_missing_at_start_is_shown_when_the_game_links(self):
        import tempfile as tmp
        from companion import ai
        folder = Path(tmp.mkdtemp())
        settings = ai.Settings(folder / "settings.json")
        settings.set_ai({"provider": "farmhand", "model": "recommended"})
        brain = ai.AI(settings, lambda code: None, ollama=ai.Ollama("http://127.0.0.1:9", timeout=0.3), folder=folder)
        brain.start()
        game, notices = self.start(autodrive=False, voice={"ai": brain})
        self.wait_for(lambda: "AI_MODEL_MISSING" in notices)
        self.assertNotIn("MENU", notices)                          # a choice exists: reported, not re-asked
        self.assertEqual(settings.load()["ai"], {"provider": "farmhand", "model": "recommended"})   # never rewritten

    def test_without_ai_unknown_words_are_answered_at_once(self):
        import tempfile as tmp
        from companion import ai
        folder = Path(tmp.mkdtemp())
        settings = ai.Settings(folder / "settings.json")
        settings.set_ai({"provider": "none"})
        brain = ai.AI(settings, lambda code: None, ollama=ai.Ollama("http://127.0.0.1:9", timeout=0.3), folder=folder)
        game, notices = self.start(autodrive=False, voice={"ai": brain})
        game.say(1, "rig up something to haul the silage")
        self.wait_for(lambda: notices == ["NOT_UNDERSTOOD"])            # no menu: a choice exists
        game.say(2, "bring me a tractor and forage wagon")             # the rules still work
        self.wait_for(lambda: notices[-1:] == ["COMPLETE"])

    def test_report_collects_the_evidence_into_one_zip(self):
        import tempfile as tmp
        import zipfile
        from companion import service
        root = Path(tmp.mkdtemp())
        profile = root / "FarmingSimulator2025"
        logs = profile / "modSettings" / "FS25_Farmhand"
        logs.mkdir(parents=True)
        (logs / "companion.log").write_text("companion started\n")
        (profile / "log.txt").write_text("noise\n[Farmhand] bridge ready\nError: something\nmore noise\n")
        game = FakeGame(autodrive=False)
        self.addCleanup(game.stop)
        path = service.save_report(profile, game.link, target=root, clock=lambda f: "now")
        with zipfile.ZipFile(path) as report:
            names = set(report.namelist())
            self.assertTrue({"companion.log", "log-farmhand-lines.txt", "snapshot.json", "versions.txt"} <= names)
            self.assertEqual(report.read("log-farmhand-lines.txt").decode(), "[Farmhand] bridge ready\nError: something")
            self.assertEqual(json.loads(report.read("snapshot.json"))["event"], "snapshot")
        made = []
        game2, notices = self.start(autodrive=False, voice={"report": lambda: made.append(1) or root})
        game2.say(1, "save a report")
        self.wait_for(lambda: notices == ["REPORT_SAVED"])
        self.assertEqual(made, [1])

    def test_live_case_1_the_requester_rides_along(self):
        # Tractor 170 / wagon 400: "navigate -> FAILED OCCUPIED" with the requester seated.
        game, notices = self.start(autodrive=False)
        game.vehicles[11]["entered"] = True
        game.say(1, "bring me a tractor and forage wagon")
        self.wait_for(lambda: notices[-1:] == ["COMPLETE"])
        self.assertEqual(self.ops(game), ["stage", "maneuver", "align", "attach", "farm_drive", "stop"])

    def test_live_case_2_stage_end_is_judged_by_the_joints_not_the_pose(self):
        # Tractor 379 / wagon 400 (STAGE_ENDED 2-4 m off), tractor 170 / wagon 173 at
        # 5304adc (14 m and 138 degrees off) and at 8ffafcc (GIANTS' agent
        # NOT_REACHABLE twice: A 12.9 m / lateral -12.4 / 85.9 deg, B 18.8 m / -18.0 /
        # 162.9 deg). GOTO is long-range travel only: inside the local work radius the
        # bounded forward LOCAL_MANEUVER lines up, unless the joints already lie in the
        # reverse's envelope. GOTO aims at each approach beside the implement at most
        # once: a STAGE ending beyond the radius, or a maneuver refused unmoved for
        # where the tractor stands, aims at the next; an aim that leaves the tractor
        # where it stood costs no drive and is not followed by the same maneuver.
        reverse = ["stage", "align", "attach", "stop"]
        local = ["stage", "maneuver", "align", "attach", "stop"]

        def refused_then_each_aim_once(ops):
            # Refused unmoved: every other aim staged once (Farmhand's own drive only onto the
            # ones 15 m away or more), the maneuver never repeated, never reversed.
            self.assertEqual(ops[:2], ["stage", "maneuver"])
            self.assertEqual(ops[-1], "stop")
            self.assertEqual(ops.count("maneuver"), 1)
            self.assertNotIn("align", ops)
            self.assertEqual(ops.count("stage"), 11)
            self.assertTrue(set(ops[2:-1]) <= {"stage", "farm_drive"})

        cases = [
            # (ahead, lateral, heading deg) of the tractor joint from the wagon's joint
            ((3.44, -12.40, 85.9), {}, "COMPLETE_HITCH", local),           # live 8ffafcc A
            ((5.52, -17.97, 162.9), {}, "COMPLETE_HITCH", local),          # live 8ffafcc B
            ((4.4, -15.0, 138.0), {}, "COMPLETE_HITCH", local),            # live 5304adc
            ((6.0, 0.5, 138.0), {}, "COMPLETE_HITCH", local),              # facing away
            ((6.0, 3.0, 0.0), {}, "COMPLETE_HITCH", local),                # beside the axis
            ((5.0, 1.5, 15.0), {}, "COMPLETE_HITCH", reverse),             # inside the envelope
            ((8.5, -1.9, -28.0), {}, "COMPLETE_HITCH", reverse),           # edge of the envelope
            # Beyond 25 m, no aim GIANTS takes moving it (live a0dca0862a424319-1): Farmhand's
            # own drive onto the next aim at its heading, then the bounded steps.
            ((20.0, -18.0, 90.0), {}, "COMPLETE_HITCH", ["stage", "stage", "farm_drive", "maneuver", "align", "attach", "stop"]),
            # Beyond 25 m: one drive, then no aim moves it, GIANTS' nor Farmhand's own (no route).
            ((20.0, -18.0, 90.0), {"farm_drive": "FAILED:NO_ROUTE"}, "FAILED_TRAVEL_ENDED_TOO_FAR",
             ["stage"] + ["stage", "farm_drive"] * 10 + ["stop"]),
            # The game refuses the geometry before moving, or the maneuver's end is not
            # in the envelope when observed: never reversed.
            ((5.52, -17.97, 162.9), {"maneuver": "FAILED:LOCAL_PATH_HITS_WAGON", "farm_drive": "FAILED:NO_ROUTE"},
             "FAILED_LOCAL_PATH_HITS_WAGON", refused_then_each_aim_once),
            ((3.44, -12.40, 85.9), {"maneuver_end": (6.0, 3.0, 0.0)},
             "FAILED_ALIGN_LATERAL_OUT_OF_RANGE", ["stage", "maneuver", "stop"]),
            ((3.44, -12.40, 85.9), {"maneuver": "FAILED:DIVERGED"}, "FAILED_DIVERGED",
             ["stage"] + ["maneuver"] * 3 + ["stop"]),
        ]
        for end, faults, outcome, ops in cases:
            with self.subTest(end=end, faults=faults):
                game, notices = self.start(autodrive=False, faults={"stage_end": end, **faults})
                with self.assertLogs("farmhand", level="INFO") as logs:
                    game.say(1, "hitch the forage wagong")
                    self.wait_for(lambda: notices[-1:] == [outcome])
                if callable(ops):
                    ops(self.ops(game))
                else:
                    self.assertEqual(self.ops(game), ops)
                stage_end = next(line for line in logs.output if "STAGE end:" in line)
                for part in ("tractor joint", "wagon joint", "gap", "ahead", "lateral",
                             "heading %.1f deg" % end[2]):
                    self.assertIn(part, stage_end)
                if outcome.startswith("COMPLETE") and "maneuver" in ops:
                    self.assertTrue(any("LOCAL_MANEUVER end:" in line and "-> ALIGN_REVERSE" in line
                                        for line in logs.output))
                self.assertEqual(game.link_world._reservations, {})              # stopped, then released

    def test_after_giants_cannot_start_the_other_aims_are_tried_by_farmhands_own_drive(self):
        # Live f530ca8d474e459d-1: GIANTS blocked at its start, Farmhand's drive found no route
        # to the first aim, and the other aims were never tried: UNPARK_BLOCKED.
        game, notices = self.start(autodrive=False, tractor_at=(0.0, 40.0),
                                   faults={"stage": "FAILED:STAGE_BLOCKED_AT_START",
                                           "farm_drive": ["FAILED:NO_ROUTE"]})
        game.say(1, "hitch the forage wagon")
        self.wait_for(lambda: notices[-1:] == ["COMPLETE_HITCH"])
        self.assertEqual(self.ops(game), ["stage", "farm_drive", "farm_drive", "maneuver", "align", "attach", "stop"])

    def test_a_maneuver_stopped_off_its_curve_is_planned_again_from_where_it_stands(self):
        # Live ac799523dcfc4f75-1: DIVERGED 0.74 m off after 2 m, and the job ended.
        for faults, ops in (({"maneuver": ["FAILED:DIVERGED"]},
                             ["stage", "maneuver", "maneuver", "align", "attach", "stop"]),
                            ({"maneuver": ["FAILED:OVERSPEED", "FAILED:DIVERGED"]},
                             ["stage", "maneuver", "maneuver", "maneuver", "align", "attach", "stop"]),
                            ({"maneuver": "FAILED:DIVERGED"},          # every time: twice more, then the end
                             ["stage", "maneuver", "maneuver", "maneuver", "stop"])):
            with self.subTest(faults=faults):
                game, notices = self.start(autodrive=False, faults={"stage_end": (3.44, -12.40, 85.9), **faults})
                game.say(1, "hitch the forage wagon")
                self.wait_for(lambda: notices[-1:] and notices[-1].split("_")[0] in ("COMPLETE", "FAILED"))
                self.assertEqual(self.ops(game), ops)

    def test_a_tractor_starting_inside_the_work_radius_never_uses_goto(self):
        game, notices = self.start(autodrive=False, tractor_at=(0.0, 20.0))
        game.say(1, "hitch the forage wagon")
        self.wait_for(lambda: notices[-1:] == ["COMPLETE_HITCH"])
        self.assertEqual(self.ops(game), ["maneuver", "align", "attach", "stop"])
        self.assertEqual(self.progress, ["WORKING_LOCAL_MANEUVER"])        # "lining up with the wagon"
        maneuver = game.commands[0]
        self.assertEqual((maneuver["tractor"], maneuver["wagon"], maneuver["tj"], maneuver["wj"]),
                         ("11", "12", "1", "1"))

    def test_live_case_3_lingering_ai_is_proven_then_the_tractor_is_reusable(self):
        # STAGE_ENDED -> STOPPED -> AI still active -> later NO_ELIGIBLE_PAIR, now explained.
        game, notices = self.start(autodrive=False, faults={"stage_ai_stays": True})
        game.press(1)
        self.wait_for(lambda: len(notices) >= 2)
        self.assertEqual(notices, ["ACCEPTED", "UNVERIFIED_CLEANUP_AI_NOT_CLEARED"])
        self.assertEqual(len(game.link_world._reservations), 1)          # never released while controlled
        self.assertNotIn("stopCurrentAIJob", str(game.commands))         # nobody else's job is touched
        with self.assertLogs("farmhand", level="INFO") as logs:
            game.press(2)                                                 # still controlled
            self.wait_for(lambda: len(notices) >= 4)
        self.assertEqual(notices[2:], ["ACCEPTED", "BLOCKED_NO_ELIGIBLE_PAIR"])
        self.assertTrue(any("candidate 11 tractor rejected:" in line and "RESERVED" in line and "AI_ACTIVE" in line
                            for line in logs.output), logs.output)
        del game.faults["stage_ai_stays"]
        game.vehicles[11]["ai_active"] = False                            # genuinely idle now
        game.press(3)
        self.wait_for(lambda: len(notices) >= 6)
        self.assertEqual(notices[4:], ["ACCEPTED", "COMPLETE"])
        self.assertEqual(game.link_world._reservations, {})

    def test_hitch_only_and_unsupported_requests(self):
        game, notices = self.start(autodrive=False)
        game.say(1, "take the tractor and attach the forage wagon")
        self.wait_for(lambda: notices[-1:] == ["COMPLETE_HITCH"])
        self.assertEqual(notices, ["ACCEPTED_HITCH", "COMPLETE_HITCH"])
        self.assertEqual(self.ops(game), ["stage", "maneuver", "align", "attach", "stop"])    # no drive anywhere
        commands = len(game.commands)
        game.say(2, "bring a tractor to me")
        # A vehicle by its name (2026-10-21, "move the gmc here"): the rig it tows, if any;
        # no "Boss Hoss" here, so nothing is chosen and nothing moves.
        game.say(3, "bring the Boss Hoss to the farm")
        self.wait_for(lambda: len(notices) >= 5)
        self.assertEqual(notices[2:4], ["UNSUPPORTED_TRACTOR_ONLY", "ACCEPTED_MOVE"])
        self.assertTrue(notices[4].startswith("BLOCKED_"), notices)
        self.assertEqual(len(game.commands), commands)                           # nothing moved

    def test_replayed_or_foreign_requests_never_run(self):
        game, notices = self.start(autodrive=False)
        game.press(1)
        self.wait_for(lambda: notices[-1:] == ["COMPLETE"])
        commands = len(game.commands)
        game.press(1)                              # replayed id
        game.press(2, session="someone-else")      # not this companion's session
        time.sleep(0.2)
        self.assertEqual(notices, ["ACCEPTED", "COMPLETE"])
        self.assertEqual(len(game.commands), commands)

    def test_typed_words_are_interpreted_and_unknown_words_change_nothing(self):
        game, notices = self.start(autodrive=False)
        game.say(1, "make me a sandwich")
        self.wait_for(lambda: notices == ["NOT_UNDERSTOOD"])
        game.say(2, "stop")
        self.wait_for(lambda: notices[-1:] == ["NOTHING_TO_STOP"])
        game.say(3, "status")
        self.wait_for(lambda: notices[-1:] == ["STATUS_IDLE"])
        self.assertEqual(game.commands, [])
        game.say(4, "please bring me a tractor and forage wagon")
        self.wait_for(lambda: notices[-1:] == ["COMPLETE"])
        self.assertEqual(self.ops(game), ["stage", "maneuver", "align", "attach", "farm_drive", "stop"])

    def test_failures_are_reported_with_their_reason(self):
        game, notices = self.start(autodrive=False)
        game.vehicles.pop(12)                      # no forage wagon
        game.press(1)
        self.wait_for(lambda: len(notices) >= 2)
        self.assertEqual(notices, ["ACCEPTED", "BLOCKED_NO_ELIGIBLE_PAIR"])
        self.assertEqual(game.commands, [])
        game.vehicles[12] = record(12, "wagon", 0.0, -4.0)
        game.faults["farm_drive"] = "FAILED:NO_ROUTE"   # no route of Farmhand's own: GIANTS GOTO tried
        game.faults["navigate"] = "FAILED:NAVIGATE_STALLED"
        game.press(2)
        self.wait_for(lambda: len(notices) >= 4)
        self.assertEqual(notices[2:], ["ACCEPTED", "FAILED_NAVIGATE_STALLED"])

    def test_dropped_session_is_renewed_and_not_linked_requests_block(self):
        game, notices = self.start(autodrive=False, session_override="dropped-earlier")
        first = game.link.session
        self.wait_for(lambda: game.link.session != first)
        game.session_override = None               # the game adopts the new session
        game.press(1)
        self.wait_for(lambda: notices[-1:] == ["COMPLETE"])
        game.session_override = "someone-else"
        game.press(2)
        self.wait_for(lambda: notices[-1:] == ["BLOCKED_NOT_LINKED"])

    def test_unconfirmed_stop_is_recovered_only_by_a_later_confirmed_stop(self):
        game, notices = self.start(autodrive=False, faults={"align": "FAILED:STALLED",
                                                            "stop": "FAILED:NOT_STOPPED"})
        game.press(1)
        self.wait_for(lambda: len(notices) >= 2)
        self.assertEqual(notices, ["ACCEPTED", "UNVERIFIED_CLEANUP_NOT_CONFIRMED"])
        world_reservations = lambda: game.link_world._reservations
        self.assertEqual(len(world_reservations()), 1)          # kept: stop never confirmed
        game.press(2)                                           # stop still fails: still kept
        self.wait_for(lambda: len(notices) >= 4)
        self.assertEqual(notices[2:], ["ACCEPTED", "BLOCKED_NO_ELIGIBLE_PAIR"])
        self.assertEqual(len(world_reservations()), 1)
        del game.faults["stop"], game.faults["align"]
        before = len(game.commands)
        game.press(3)
        self.wait_for(lambda: len(notices) >= 6)
        self.assertEqual(notices[4:], ["ACCEPTED", "COMPLETE"])
        # The tractor still stands where STAGE left it, inside the reverse's envelope.
        self.assertEqual([c["op"] for c in game.commands[before:]],
                         ["stop", "align", "attach", "farm_drive", "stop"])
        self.assertEqual(world_reservations(), {})

    def test_reloaded_game_restarting_request_ids_is_served_with_fresh_work(self):
        game, notices = self.start(autodrive=False)
        game.press(1)
        self.wait_for(lambda: notices[-1:] == ["COMPLETE"])
        first = len(game.commands)
        # Quit and load the save: the game re-adopts the same session, re-opens the
        # pipe (hello) and numbers its requests from 1 again.
        game.link.on_frame({"event": "hello", "protocol": 3, "mod_version": "0.1.0.0", "autodrive_version": None})
        for vehicle, xz in ((11, (0.0, 20.0)), (12, (0.0, -4.0))):
            game.vehicles[vehicle] = record(vehicle, "tractor" if vehicle == 11 else "wagon", *xz)
        game.press(1)
        self.wait_for(lambda: len(notices) >= 4)
        self.assertEqual(notices[2:], ["ACCEPTED", "COMPLETE"])
        self.assertGreater(len(game.commands), first)          # real work, not a cached result

    def test_a_cancelled_link_sends_nothing_but_stop(self):
        link = CommandLink(FakeFiles(None))
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(bridge.StepFailed) as refused:
            link.command("navigate", {"tractor": 11, "x": 0.0, "z": 0.0, "yaw": 0.0}, cancel)
        self.assertEqual(refused.exception.code, "CANCELLED")
        self.assertNotIn("command.xml", link.files.files)

    def test_failed_session_writes_never_stop_the_companion(self):
        class Flaky(FakeFiles):
            def write(self, name, text):
                if name == "session.xml":
                    raise PermissionError("held by the game")
                super().write(name, text)

        link = CommandLink(Flaky(None))
        link.heartbeat()
        link.notice("BUSY")
        link.on_frame({"event": "request", "protocol": 3, "session": link.session, "id": 1, "action": "fetch_here"})
        self.assertEqual(link.next_request(0.1)["id"], 1)
        self.assertFalse(link.closed.is_set())

    def test_notices_are_strict_tokens(self):
        from companion.coordinator import result_notice as notice_code
        self.assertEqual(notice_code("COMPLETE", "DELIVERY_OBSERVED"), "COMPLETE")
        self.assertEqual(notice_code("CANCELLED", "CANCELLED_BY_REQUESTER"), "CANCELLED")
        self.assertEqual(notice_code("FAILED", "bad reason!"), "FAILED_bad_reason_")
        self.assertEqual(len(notice_code("FAILED", "X" * 200)), 64)
        self.assertIn('notices="3:BUSY 4:CANCELLED"', encode_session("s1", 0, [(3, "BUSY"), (4, "CANCELLED")]))
        self.assertNotIn("notice", encode_session("s1", 0))
        for bad in ([(1, "two words")], [(0, "OK")], [(True, "OK")], [(2, "A"), (1, "B")],
                    [(n, "A") for n in range(1, 10)]):
            with self.assertRaises(ValueError):
                encode_session("s1", 0, bad)

    def test_the_session_file_keeps_the_latest_notices(self):
        link = CommandLink(FakeFiles(None))
        for n in range(10):
            link.notice(f"N{n}")
        attrs = ET.fromstring(link.files.files["session.xml"].split("\n", 1)[1]).attrib
        self.assertEqual(attrs["notices"].split(), [f"{n + 1}:N{n}" for n in range(2, 10)])

    def test_closed_link_never_writes_its_session_again(self):
        link = CommandLink(FakeFiles(None))
        link.heartbeat()
        link.close()
        link.heartbeat()
        link.notice("COMPLETE")
        self.assertNotIn("session.xml", link.files.files)


class OpContractTests(unittest.TestCase):
    def test_every_command_op_has_a_wait_bound(self):
        # Live 8f95c84618a14e92-1: "buy" had none, a KeyError before anything was sent.
        from protocol.ipc import COMMAND_ARGS
        self.assertEqual(set(COMMAND_ARGS) - set(bridge.DONE_TIMEOUT_S), set())


class CatalogPageTests(unittest.TestCase):
    """The store catalog arrives in pages (live 2026-10-21: one frame with every item's
    options outgrew the 1 MiB line and was dropped); complete only with every page, and a
    new game link never keeps another game's catalog."""

    ITEM = {"id": 1, "name": "Titan Vineyard Plow", "brand": "LIZARD", "categories": ["PLOWS"], "price": 3000,
            "mod": True, "width": None, "needed_power": 65, "power": None,
            "options": [{"kind": "set", "type": None, "index": 5, "name": "25M Unrealistic"}]}

    def test_pages_make_one_catalog_and_a_new_link_clears_it(self):
        link = CommandLink(FakeFiles(None))
        page = lambda n, pages, item_id: {"event": "catalog", "protocol": 3, "truncated": False, "page": n,
                                          "pages": pages, "items": [dict(self.ITEM, id=item_id)]}
        link.on_frame(page(0, 2, 1))
        self.assertEqual(link.catalog, ())                                  # one page of two: not yet
        link.on_frame(page(1, 2, 2))
        self.assertEqual([i.id for i in link.catalog], [1, 2])
        self.assertEqual(link.catalog[0].options, (("set", None, 5, "25M Unrealistic"),))
        link.on_frame({"event": "hello", "protocol": 3})
        self.assertEqual(link.catalog, ())                                  # another game: gone
