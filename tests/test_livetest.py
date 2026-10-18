"""The development-only live-test bridges.

1. companion/livetest.py inside the real serve() loop over FakeGame
   (tests/test_bridge.py): a hitch request runs the production coordinator and
   executor, exactly the ops the player's "hitch the forage wagon" sends; STOP is the
   production stop; anything malformed, stale, replayed or unknown is refused.
2. scripts/fs25_host_runner.py: only its allowlisted ops with validated arguments,
   fixed targets, no links out of the checkout, only the exact checked build.
3. scripts/fs25_live_test.py (the sandbox client) through the runner to the
   companion and back.
None of it proves FS25, Windows processes or the real file system timing.
"""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

from companion import bridge, livetest
from companion.bridge import FS25World
from protocol.ipc import encode_session
from tests.test_bridge import FakeGame

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fs25_host_runner as host            # noqa: E402
import fs25_live_test as client            # noqa: E402
import package_probe                       # noqa: E402

LIVE_HITCH = ["stage", "maneuver", "align", "attach", "stop"]


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


class CompanionCase(unittest.TestCase):
    """A FakeGame, the real serve() loop and a LiveTest polling a temporary folder."""

    def start(self, build="abc123", **game_args):
        import logging
        from companion import service
        farmhand = logging.getLogger("farmhand")      # INFO, as companion/service.py configures it
        self.addCleanup(farmhand.setLevel, farmhand.level)
        farmhand.setLevel(logging.INFO)
        game = FakeGame(**game_args)
        self.addCleanup(game.stop)
        game.link.on_frame({"event": "hello", "protocol": 2, "mod_version": "0.1.0.0", "build": build,
                            "autodrive_version": None})
        world = game.link_world = FS25World(game.link)
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        for patch in (unittest.mock.patch.object(bridge, "ACCEPT_TIMEOUT_S", 0.3),
                      unittest.mock.patch.dict(bridge.DONE_TIMEOUT_S, {op: 0.3 for op in bridge.DONE_TIMEOUT_S}),
                      unittest.mock.patch.object(livetest, "BUILD", "abc123"),
                      unittest.mock.patch.object(livetest, "POLL_S", 0.02)):
            patch.start()
            self.addCleanup(patch.stop)
        live = livetest.LiveTest(world, self.folder)
        live.start()
        self.addCleanup(live.close)
        stop = threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=service.serve, args=(world,), kwargs={"should_stop": stop.is_set, "live": live},
                         daemon=True).start()
        wait_for(lambda: live.coordinator is not None)
        self.live, self.game = live, game
        return game, live

    def request(self, command, session=None, ident=None, created=None, raw=None):
        ident = ident or str(time.time_ns())
        body = raw if raw is not None else json.dumps({"version": 1, "id": ident, "command": command,
                                                       "session": session or self.game.link.session,
                                                       "created": time.time() if created is None else created})
        (self.folder / f"request-{ident}.json").write_bytes(body.encode() if isinstance(body, str) else body)
        answer = self.folder / f"response-{ident}.json"
        wait_for(answer.exists)
        time.sleep(0.02)
        return json.loads(answer.read_text())

    def record(self, test_id):
        path = self.folder / f"test-{test_id}.json"
        wait_for(lambda: path.exists() and json.loads(path.read_text())["status"] != "WORKING")
        return json.loads(path.read_text())


class CompanionLiveTestTests(CompanionCase):
    def test_hitch_runs_the_production_pipeline_and_records_what_happened(self):
        # The live 8ffafcc A pose: GOTO ends 12.9 m off, the local maneuver lines up.
        game, _ = self.start(autodrive=False, faults={"stage_end": (3.44, -12.40, 85.9)})
        answer = self.request("hitch")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["code"], "STARTED")
        record = self.record(answer["result"]["test_id"])
        self.assertEqual(record["status"], "COMPLETE")
        self.assertEqual([c["op"] for c in game.commands], LIVE_HITCH)        # nothing but production ops
        self.assertEqual(record["states"][:3], ["FIND", "RESERVE", "STAGE"])
        self.assertIn("LOCAL_MANEUVER", record["states"])
        self.assertIn("ALIGN_REVERSE", record["states"])
        self.assertEqual(sorted(record["equipment"]), ["11", "12"])
        self.assertEqual(record["final_observation"]["12"]["attached_to"], "11")
        self.assertTrue(any("STAGE end:" in line and " gap " in line for line in record["companion_log"]))
        self.assertTrue(any("LOCAL_MANEUVER end:" in line for line in record["companion_log"]))
        self.assertEqual(record["mod_build"], "abc123")
        self.assertEqual(game.link_world._reservations, {})

    def test_the_hitch_is_the_players_proposal_through_the_coordinator(self):
        game, live = self.start(autodrive=False)
        with unittest.mock.patch.object(live.coordinator, "submit", wraps=live.coordinator.submit) as submit:
            answer = self.request("hitch")
            self.record(answer["result"]["test_id"])
        submit.assert_called_once_with({"version": 1, "command": "hitch_pair"})

    def test_stop_is_the_production_stop_and_cancels_the_running_test(self):
        game, _ = self.start(autodrive=False, faults={"align": "hold"})
        test_id = self.request("hitch")["result"]["test_id"]
        wait_for(lambda: game.held is not None)
        stop = self.request("stop")
        self.assertEqual((stop["ok"], stop["code"], stop["result"]["test_id"]), (True, "STOP_SENT", test_id))
        record = self.record(test_id)
        self.assertEqual(record["status"], "CANCELLED")
        ops = [c["op"] for c in game.commands]
        self.assertIn("stop", ops)
        self.assertNotIn("attach", ops)
        self.assertEqual(game.link_world._reservations, {})

    def test_one_test_at_a_time_and_never_over_a_players_job(self):
        game, _ = self.start(autodrive=False, faults={"align": "hold"})
        first = self.request("hitch")
        wait_for(lambda: game.held is not None)
        self.assertEqual(self.request("hitch")["code"], "TEST_ACTIVE")
        self.request("stop")
        self.record(first["result"]["test_id"])
        game.press(1)                                  # the player's own key
        wait_for(lambda: game.held is not None)
        busy = self.request("hitch")
        self.assertEqual((busy["ok"], busy["code"]), (False, "BUSY"))

    def test_status_reports_link_builds_farm_candidates_and_readiness(self):
        self.start(autodrive=False)
        status = self.request("status")["result"]
        self.assertTrue(status["game"]["linked"])
        self.assertEqual((status["companion"]["build"], status["game"]["mod_build"]), ("abc123", "abc123"))
        self.assertEqual(status["farm"]["farm_id"], "1")
        self.assertEqual(status["candidates"]["pair"]["tractor"], "11")
        self.assertEqual(status["candidates"]["pair"]["wagon"], "12")
        self.assertIsNone(status["test_active"])
        self.assertTrue(status["ready"], status["problems"])

    def test_status_names_a_game_running_an_older_mod(self):
        self.start(build="0ld", autodrive=False)
        status = self.request("status")["result"]
        self.assertFalse(status["ready"])
        self.assertIn("MOD_BUILD_MISMATCH", status["problems"])

    def test_an_older_mod_without_a_build_is_not_ready(self):
        game, live = self.start(autodrive=False)
        game.link.on_frame({"event": "hello", "protocol": 2, "mod_version": "0.1.0.0", "autodrive_version": None})
        self.assertIn("MOD_BUILD_MISMATCH", live.status()["problems"])

    def test_malformed_unauthorized_or_stale_requests_are_refused_and_nothing_moves(self):
        game, _ = self.start(autodrive=False)
        session = game.link.session

        def body(**change):
            value = {"version": 1, "id": "", "command": "hitch", "session": session, "created": time.time()}
            value.update(change)
            return value

        cases = [
            ("MALFORMED", b"{not json"),
            ("MALFORMED", b"\xff\xfe"),
            ("SCHEMA_VIOLATION", body(extra="x")),
            ("SCHEMA_VIOLATION", [1, 2]),
            ("VERSION_MISMATCH", body(version=2)),
            ("UNKNOWN_COMMAND", body(command="lua")),
            ("UNKNOWN_COMMAND", body(command="attach")),
            ("SESSION_MISMATCH", body(session="someone-else")),
            ("STALE", body(created=time.time() - 120)),
            ("STALE", body(created=time.time() + 120)),
            ("NON_FINITE_NUMBER", None),
            ("DUPLICATE_KEY", None),
            ("TOO_LARGE", body(command="x" * 5000)),
            ("BAD_ID", body(id="1")),
        ]
        for code, value in cases:
            with self.subTest(code=code):
                ident = str(time.time_ns())
                if code == "NON_FINITE_NUMBER":
                    raw = ('{"version": 1, "id": "%s", "command": "hitch", "session": "%s", "created": NaN}'
                           % (ident, session))
                elif code == "DUPLICATE_KEY":
                    raw = ('{"version": 1, "id": "%s", "command": "status", "command": "hitch", "session": "%s", '
                           '"created": %f}' % (ident, session, time.time()))
                elif isinstance(value, bytes):
                    raw = value
                elif isinstance(value, dict):
                    raw = json.dumps({**value, "id": value["id"] or ident})
                else:
                    raw = json.dumps(value)
                answer = self.request(None, ident=ident, raw=raw)
                self.assertEqual((answer["ok"], answer["code"]), (False, code))
        self.assertEqual(game.commands, [])

    def test_a_replayed_id_is_refused(self):
        game, _ = self.start(autodrive=False)
        ident = str(time.time_ns())
        self.assertTrue(self.request("status", ident=ident)["ok"])
        self.assertEqual(self.request("hitch", ident=ident)["code"], "REPLAYED")
        older = str(int(ident) - 5)
        self.assertEqual(self.request("hitch", ident=older)["code"], "REPLAYED")
        self.assertEqual(game.commands, [])

    def test_requests_left_from_before_the_companion_never_run(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        ident = str(time.time_ns())
        (folder / f"request-{ident}.json").write_text("{}")
        game = FakeGame(autodrive=False)
        self.addCleanup(game.stop)
        live = livetest.LiveTest(FS25World(game.link), folder)
        live.start()
        self.addCleanup(live.close)
        self.assertFalse((folder / f"request-{ident}.json").exists())
        self.assertEqual(json.loads((folder / f"response-{ident}.json").read_text())["code"], "STALE")
        self.assertTrue((folder / "enabled.json").exists())
        live.close()
        self.assertFalse((folder / "enabled.json").exists())


class ReleaseBoundaryTests(unittest.TestCase):
    def test_serve_ignores_a_live_request_without_the_hook(self):
        from companion import service
        game = FakeGame(autodrive=False)
        self.addCleanup(game.stop)
        stop = threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=service.serve, args=(FS25World(game.link),), kwargs={"should_stop": stop.is_set},
                         daemon=True).start()
        game.link.inject({"live": {"id": "1", "command": "hitch"}})
        time.sleep(0.3)
        self.assertEqual(game.commands, [])

    def test_the_logon_start_never_enables_it(self):
        from companion.setup import autostart_command
        self.assertNotIn("--live-test", autostart_command(Path("C:/x"), Path("C:/p")))

    def test_the_flag_is_off_by_default(self):
        import companion.service as service
        source = Path(service.__file__).read_text()
        self.assertIn('parser.add_argument("--live-test", action="store_true"', source)
        self.assertIn("if args.live_test:", source)

    def test_game_frames_cannot_carry_a_live_request(self):
        from protocol.ipc import FrameError, parse_frame
        frame = {"event": "request", "protocol": 2, "session": "s", "id": 1, "action": "fetch_here",
                 "live": {"command": "hitch"}}
        with self.assertRaises(FrameError):
            parse_frame((json.dumps(frame) + "\n").encode())


# ---------------------------------------------------------------- host runner
class FakeSystem:
    """Windows programs, recorded; FS25 and the companion are flags."""

    def __init__(self, data: Path, live: Path):
        self.data, self.live = data, live
        self.fs25, self.companion_pid, self.calls = [], None, []
        self.on_start = None

    def processes(self, image):
        return list(self.fs25) if image == host.FS25_IMAGE else []

    def image_of(self, pid):
        return "pythonw.exe" if pid == self.companion_pid else None

    def close(self, image, force):
        self.calls.append(("close", image, force))
        if force or not getattr(self, "stubborn", False):
            self.fs25 = []

    def start(self, program, arguments, folder):
        self.calls.append(("start", program, tuple(arguments), folder))
        if program.endswith("pythonw.exe"):
            self.companion_pid = 4242
            (self.data / "companion.pid").write_text("4242")
            text = (self.data / "app" / "companion" / "buildinfo.py").read_text()
            build = text.split('\nBUILD = "')[1].split('"')[0]
            self.live.mkdir(parents=True, exist_ok=True)
            (self.live / "enabled.json").write_text(json.dumps({"build": build}))
        else:
            self.fs25 = [999]
        if self.on_start:
            self.on_start(program)


class RunnerCase(unittest.TestCase):
    HEAD = "0123456789abcdef0123456789abcdef01234567"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / ".git" / "HEAD").write_text(self.HEAD + "\n")
        self.profile, self.data = self.tmp / "profile", self.tmp / "data"
        (self.data / "runtime").mkdir(parents=True)
        (self.data / "runtime" / "pythonw.exe").write_text("")
        self.profile.mkdir()
        places = {host.FS25_PROFILE: self.profile, host.FARMHAND_DATA: self.data, host.FS25_EXE: self.tmp / "fs25.exe"}
        self.targets = host.Targets(savegame=3, local=lambda windows: places[windows])
        self.system = FakeSystem(self.data, self.targets.live)
        self.runner = host.Runner(self.repo, self.targets, self.system, say=lambda *a, **k: None)
        self.runner.beat()

    def send(self, op, args=None, token=None, ident=None, raw=None, created=None):
        ident = ident or str(time.time_ns())
        body = raw if raw is not None else json.dumps({
            "version": 1, "id": ident, "token": token or self.runner.token, "op": op, "args": args or {},
            "created": time.time() if created is None else created})
        (self.runner.requests / f"request-{ident}.json").write_bytes(body.encode() if isinstance(body, str) else body)
        self.runner.poll_once()
        answer = self.runner.responses / f"response-{ident}.json"
        wait_for(answer.exists)
        time.sleep(0.02)
        return json.loads(answer.read_text())

    def build(self, head=None, extra=None, tamper=None):
        with unittest.mock.patch.object(package_probe, "build_id", lambda: (head or self.HEAD)):
            with unittest.mock.patch("builtins.print"):
                package_probe.build_dev(self.repo / ".fs25-dev" / "build")
        folder = self.repo / ".fs25-dev" / "build"
        if extra:
            (folder / extra).parent.mkdir(parents=True, exist_ok=True)
            (folder / extra).write_text("x")
        if tamper:
            (folder / tamper).write_text("tampered")
        return folder


class HostRunnerProtocolTests(RunnerCase):
    def test_only_the_allowlist_with_validated_arguments(self):
        cases = [
            ("UNKNOWN_OP", {"op": "shell", "args": {}}),
            ("UNKNOWN_OP", {"op": "powershell", "args": {}}),
            ("UNKNOWN_OP", {"op": "cmd", "args": {}}),
            ("UNKNOWN_OP", {"op": "run-lua", "args": {}}),
            ("UNKNOWN_OP", {"op": "console", "args": {}}),
            ("UNKNOWN_OP", {"op": 7, "args": {}}),
            ("BAD_ARGUMENTS", {"op": "launch-fs25", "args": {"exe": "C:\\Windows\\System32\\cmd.exe"}}),
            ("BAD_ARGUMENTS", {"op": "install-current-build", "args": {"source": "C:\\x"}}),
            ("BAD_ARGUMENTS", {"op": "restart-fs25", "args": {"arguments": ["-console"]}}),
            ("BAD_ARGUMENTS", {"op": "collect", "args": {"test_id": "../../etc/passwd"}}),
            ("BAD_ARGUMENTS", {"op": "collect", "args": {"test_id": "C:\\Windows"}}),
            ("BAD_ARGUMENTS", {"op": "collect", "args": {"test_id": 5}}),
            ("BAD_ARGUMENTS", {"op": "status", "args": []}),
        ]
        for code, change in cases:
            with self.subTest(code=code, change=change):
                ident = str(time.time_ns())
                raw = json.dumps({"version": 1, "id": ident, "token": self.runner.token,
                                  "created": time.time(), **change})
                self.assertEqual(self.send(None, ident=ident, raw=raw)["code"], code)
        self.assertEqual(self.system.calls, [])

    def test_malformed_stale_replayed_or_foreign_requests_are_refused(self):
        ident = str(time.time_ns())
        cases = [
            ("BAD_TOKEN", dict(token="0" * 32)),
            ("STALE", dict(created=time.time() - 3600)),
            ("MALFORMED", dict(raw=b"\x00garbage")),
            ("SCHEMA_VIOLATION", dict(raw=json.dumps({"version": 1, "id": "1", "op": "status"}))),
            ("TOO_LARGE", dict(raw=" " * (host.REQUEST_MAX_BYTES + 10))),
        ]
        for code, change in cases:
            with self.subTest(code=code):
                self.assertEqual(self.send("status", **change)["code"], code)
        self.assertEqual(self.send("status", ident=ident)["code"], "STATUS")
        self.assertEqual(self.send("status", ident=ident)["code"], "REPLAYED")
        bad_name = str(int(ident) + 10)
        raw = json.dumps({"version": 1, "id": "1", "token": self.runner.token, "op": "status", "args": {},
                          "created": time.time()})
        self.assertEqual(self.send(None, ident=bad_name, raw=raw)["code"], "BAD_ID")

    def test_a_link_out_of_the_checkout_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        shutil.rmtree(self.runner.responses)
        os.symlink(outside, self.runner.responses)
        with self.assertRaises(host.Refused) as raised:
            self.runner.poll_once()
        self.assertEqual(raised.exception.code, "SYMLINK_REFUSED")
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_linked_request_file_is_not_read(self):
        secret = self.tmp / "secret.txt"
        secret.write_text("private")
        ident = str(time.time_ns())
        os.symlink(secret, self.runner.requests / f"request-{ident}.json")
        self.runner.poll_once()
        answer = json.loads((self.runner.responses / f"response-{ident}.json").read_text())
        self.assertEqual(answer["code"], "SYMLINK_REFUSED")
        self.assertNotIn("private", json.dumps(answer))

    def test_status_without_a_companion_says_so(self):
        status = self.send("status")
        self.assertTrue(status["ok"])
        result = status["result"]
        self.assertEqual((result["fs25_running"], result["companion_pid"], result["farmhand_ready"]),
                         (False, None, False))
        self.assertEqual(result["live_problem"], "LIVE_TEST_NOT_ENABLED")
        self.assertEqual(result["repo_head"], self.HEAD)

    def test_the_file_lists_are_the_packaged_ones(self):
        self.assertEqual(host.MOD_FILES, package_probe.RUNTIME_FILES)
        self.assertEqual(host.APP_FILES, package_probe.APP_FILES)
        self.assertEqual(host.MOD_SETTINGS, bridge.MOD_DIR)
        self.assertEqual(host.LIVE_FOLDER, livetest.FOLDER)

    def test_powershell_never_gets_a_quote_or_a_variable(self):
        for text in ("C:\\a'b", 'C:\\a"b', "C:\\$env:x", "C:\\a`b", "C:\\a\nb"):
            with self.subTest(text=text):
                with self.assertRaises(host.Refused):
                    host._plain(text)
        with self.assertRaises(host.Refused):
            host.Targets(exe="C:\\x'; Remove-Item C:\\ -Recurse; '")

    def test_wsl_paths_map_the_fixed_targets(self):
        with unittest.mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(host.local_path(host.FARMHAND_DATA), Path("/mnt/c/Users/Matt/AppData/Local/FS25Farmhand"))


class HostRunnerLifecycleTests(RunnerCase):
    def test_install_copies_exactly_the_checked_build_and_starts_the_live_companion(self):
        self.build()
        old = self.targets.mod / "stale.lua"
        old.parent.mkdir(parents=True)
        old.write_text("old")
        answer = self.send("install-current-build")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["installed"], {"mod": self.HEAD, "app": self.HEAD})
        self.assertFalse(old.exists())
        self.assertEqual(sorted(p.relative_to(self.targets.mod).as_posix() for p in self.targets.mod.rglob("*")
                                if p.is_file()), sorted(host.MOD_FILES))
        program = str(PureWindowsPath(host.FARMHAND_DATA) / "runtime" / "pythonw.exe")
        self.assertEqual(self.system.calls[-1], ("start", program, ("-m", "companion.service", "--profile",
                                                                   host.FS25_PROFILE, "--live-test"),
                                                  host.FARMHAND_DATA))

    def test_install_refuses_an_unchecked_build(self):
        cases = [("BUILD_NOT_CURRENT", dict(head="f" * 40)),
                 ("BUILD_FILES_NOT_ALLOWED", dict(extra="app/companion/evil.py")),
                 ("BUILD_HASH_MISMATCH", dict(tamper="app/companion/service.py"))]
        for code, change in cases:
            with self.subTest(code=code):
                self.build(**change)
                answer = self.send("install-current-build")
                self.assertEqual((answer["ok"], answer["code"]), (False, code))
        self.assertFalse(self.targets.mod.exists())
        self.assertEqual(self.system.calls, [])

    def test_install_refuses_a_linked_build_file(self):
        folder = self.build()
        target = folder / "app" / "companion" / "service.py"
        copy = self.tmp / "service.py"
        shutil.copy(target, copy)
        target.unlink()
        os.symlink(copy, target)
        self.assertEqual(self.send("install-current-build")["code"], "SYMLINK_REFUSED")

    def test_install_refuses_a_linked_build_folder(self):
        folder = self.build()
        moved = self.tmp / "companion"
        shutil.move(str(folder / "app" / "companion"), moved)
        os.symlink(moved, folder / "app" / "companion")
        self.assertEqual(self.send("install-current-build")["code"], "BUILD_FILES_NOT_ALLOWED")
        os.unlink(folder / "app" / "companion")
        os.symlink(moved, folder / "app" / "companion", target_is_directory=True)
        with unittest.mock.patch.object(host.SharedTree, "files", return_value=set(host.BUILD_FILES)):
            self.assertEqual(self.send("install-current-build")["code"], "SYMLINK_REFUSED")
        self.assertFalse(self.targets.mod.exists())

    def test_install_never_runs_under_the_game(self):
        self.build()
        self.system.fs25 = [999]
        self.assertEqual(self.send("install-current-build")["code"], "FS25_RUNNING")

    def test_launch_starts_only_the_configured_executable(self):
        answer = self.send("launch-fs25")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.system.calls, [("start", host.FS25_EXE, ("-autoStartSavegameId", "3"),
                                              str(PureWindowsPath(host.FS25_EXE).parent))])
        self.assertEqual(self.send("launch-fs25")["code"], "FS25_RUNNING")

    def test_restart_stops_only_fs25_then_waits_for_ready(self):
        self.system.fs25 = [1]
        with unittest.mock.patch.object(host, "READY_S", 0.5), \
                unittest.mock.patch.object(self.runner, "op_status",
                                           return_value=(True, "STATUS", {"farmhand_ready": True})):
            answer = self.send("restart-fs25")
        self.assertEqual((answer["ok"], answer["code"]), (True, "READY"))
        self.assertEqual(self.system.calls[0], ("close", host.FS25_IMAGE, False))
        self.assertEqual(self.system.calls[1][:2], ("start", host.FS25_EXE))

    def test_restart_reports_not_ready_within_its_bound(self):
        with unittest.mock.patch.object(self.runner, "op_status",
                                        return_value=(True, "STATUS", {"farmhand_ready": False})):
            code = None
            try:
                self.runner.op_restart_fs25(ready_timeout=0.1)
            except host.Refused as error:
                code = error.code
        self.assertEqual(code, "NOT_READY")

    def test_only_one_lifecycle_op_at_a_time(self):
        self.runner._lifecycle.acquire()
        try:
            self.assertEqual(self.send("launch-fs25")["code"], "LIFECYCLE_BUSY")
            self.assertEqual(self.send("run-hitch-test")["code"], "LIFECYCLE_BUSY")
        finally:
            self.runner._lifecycle.release()


class EndToEndTests(CompanionCase):
    """client -> runner -> companion files -> serve() -> executor -> FakeGame, and back."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def wire(self, **game_args):
        game, live = self.start(autodrive=False, **game_args)
        repo, profile = self.tmp / "repo", self.tmp / "profile"
        (repo / ".git").mkdir(parents=True)
        settings = profile.joinpath(*bridge.MOD_DIR)
        settings.mkdir(parents=True)
        (settings / "session.xml").write_text(encode_session(game.link.session, 1))
        shutil.rmtree(settings / "livetest", ignore_errors=True)
        os.symlink(self.folder, settings / "livetest")           # the companion's own folder
        (profile / "log.txt").write_text("2000-01-01 00:00:00.000 [Farmhand] before the test\n")
        places = {host.FS25_PROFILE: profile, host.FARMHAND_DATA: self.tmp / "data", host.FS25_EXE: self.tmp / "x"}
        targets = host.Targets(local=lambda w: places[w])
        targets.live = self.folder
        system = FakeSystem(self.tmp / "data", self.folder)
        system.fs25, system.companion_pid = [999], 4242            # FS25 and the companion are running
        (self.tmp / "data").mkdir()
        (self.tmp / "data" / "companion.pid").write_text("4242")
        runner = host.Runner(repo, targets, system, say=lambda *a, **k: None)
        stop = threading.Event()
        self.addCleanup(stop.set)
        runner._stop = stop
        threading.Thread(target=runner.serve, daemon=True).start()
        wait_for(lambda: (runner.host / "runner.json").exists())
        for patch in (unittest.mock.patch.object(client, "HOST", runner.host),
                      unittest.mock.patch.object(client, "EVIDENCE", self.tmp / "evidence"),
                      unittest.mock.patch.object(client, "emit", lambda **f: self.out.append(f))):
            patch.start()
            self.addCleanup(patch.stop)
        self.out = []
        return game

    def test_claude_runs_one_production_hitch_and_reads_its_result(self):
        game = self.wire(faults={"stage_end": (5.52, -17.97, 162.9)})        # the live 8ffafcc B pose
        self.assertEqual(client.main(["status"]), 0, self.out)
        self.assertTrue(self.out[-1]["result"]["hitch_ready"])
        self.out.clear()
        code = client.command_hitch(poll=0.05)
        self.assertEqual(code, 0, self.out)
        self.assertEqual(self.out[0]["event"], "started")
        result = self.out[-1]
        self.assertEqual((result["event"], result["status"]), ("result", "COMPLETE"))
        summary = result["summary"]
        self.assertIn("LOCAL_MANEUVER", summary["states"])
        self.assertTrue(summary["joints"])
        self.assertTrue(summary["attach"])
        self.assertEqual([c["op"] for c in game.commands], LIVE_HITCH)
        evidence = Path(result["evidence"])
        self.assertTrue((evidence / "summary.json").is_file())
        self.assertTrue((evidence / "companion-log.txt").read_text())
        self.assertEqual(client.main(["collect"]), 0)

    def test_a_failed_test_exits_nonzero_with_its_reason(self):
        game = self.wire(faults={"stage_end": (3.44, -12.40, 85.9), "maneuver": "FAILED:LOCAL_PATH_HITS_WAGON"})
        self.assertEqual(client.command_hitch(poll=0.05), client.EXIT["FAILED"])
        self.assertEqual(self.out[-1]["reason"], "LOCAL_PATH_HITS_WAGON")
        self.assertEqual([c["op"] for c in game.commands], ["stage", "maneuver", "stop"])     # never reversed

    def test_without_a_runner_the_client_says_so(self):
        with unittest.mock.patch.object(client, "HOST", self.tmp / "nowhere"):
            out = []
            with unittest.mock.patch.object(client, "emit", lambda **f: out.append(f)):
                self.assertEqual(client.main(["status"]), client.INFRASTRUCTURE)
        self.assertEqual(out[-1]["code"], "HOST_RUNNER_NOT_RUNNING")


class BuildDevTests(unittest.TestCase):
    def test_the_dev_build_is_stamped_and_named_by_its_manifest(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        with unittest.mock.patch.object(package_probe, "build_id", lambda: "feedbeef"), \
                unittest.mock.patch("builtins.print"):
            manifest = package_probe.build_dev(folder / "b")
        self.assertEqual(set(manifest["files"]), set(host.BUILD_FILES))
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((folder / "b" / name).read_bytes()).hexdigest(), digest)
        self.assertIn('Observe.BUILD = "feedbeef"', (folder / "b/mod/FS25_Farmhand/FarmhandObserve.lua").read_text())
        self.assertIn('BUILD = "feedbeef"', (folder / "b/app/companion/buildinfo.py").read_text())


if __name__ == "__main__":
    unittest.main()
