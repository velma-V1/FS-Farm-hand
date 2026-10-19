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
        game.link.on_frame({"event": "hello", "protocol": 3, "mod_version": "0.1.0.0", "build": build,
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

    def request(self, command, session=None, ident=None, created=None, raw=None, text=None):
        ident = ident or str(time.time_ns())
        value = {"version": 1, "id": ident, "command": command, "session": session or self.game.link.session,
                 "created": time.time() if created is None else created}
        if text is not None:
            value["text"] = text
        body = raw if raw is not None else json.dumps(value)
        answer = self.folder / f"response-{ident}.json"
        answer.unlink(missing_ok=True)                # a replayed id must not read the earlier answer
        temporary = self.folder / f".request-{ident}.tmp"           # renamed into place, as the runner does
        temporary.write_bytes(body.encode() if isinstance(body, str) else body)
        temporary.replace(self.folder / f"request-{ident}.json")
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

    def test_typed_words_run_through_the_production_rules_and_coordinator(self):
        game, live = self.start(autodrive=False, faults={"stage_end": (3.44, -12.40, 85.9)})
        with unittest.mock.patch.object(live.coordinator, "submit", wraps=live.coordinator.submit) as submit:
            answer = self.request("request", text="hitch the forage wagon")
            self.assertEqual((answer["ok"], answer["code"]), (True, "STARTED"), answer)
            record = self.record(answer["result"]["test_id"])
        submit.assert_called_once_with({"version": 1, "command": "hitch_pair"})
        self.assertEqual((record["status"], record["text"]), ("COMPLETE", "hitch the forage wagon"))
        self.assertEqual(record["proposal"], {"version": 1, "command": "hitch_pair"})
        self.assertEqual([c["op"] for c in game.commands], LIVE_HITCH)

    def test_typed_words_that_are_not_a_job_or_not_understood_start_nothing(self):
        game, live = self.start(autodrive=False)
        with unittest.mock.patch.object(live.coordinator, "submit", wraps=live.coordinator.submit) as submit:
            for text, code in (("stop", "NOT_A_JOB"), ("what are you doing", "NOT_A_JOB"),
                               ("open the settings", "NOT_A_JOB"), ("sing me a song", "NOT_UNDERSTOOD")):
                with self.subTest(text=text):
                    answer = self.request("request", text=text)
                    self.assertEqual((answer["ok"], answer["code"]), (False, code), answer)
        submit.assert_not_called()
        self.assertEqual(game.commands, [])

    def test_typed_words_are_only_words(self):
        game, _ = self.start(autodrive=False)
        session = game.link.session
        cases = [("BAD_TEXT", "print(os.system('x'))"), ("BAD_TEXT", "C:\\Windows"), ("BAD_TEXT", "a;b"),
                 ("BAD_TEXT", "x" * 161), ("BAD_TEXT", ""), ("BAD_TEXT", "line\nbreak"), ("BAD_TEXT", 7)]
        for code, text in cases:
            with self.subTest(text=text):
                self.assertEqual(self.request("request", text=text)["code"], code)
        for command, text in (("request", None), ("hitch", "hitch the forage wagon"), ("status", "x")):
            with self.subTest(command=command):
                ident = str(time.time_ns())
                value = {"version": 1, "id": ident, "command": command, "session": session, "created": time.time()}
                if text is not None:
                    value["text"] = text
                self.assertEqual(self.request(None, ident=ident, raw=json.dumps(value))["code"], "SCHEMA_VIOLATION")
        self.assertEqual(game.commands, [])

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
        inventory = {i["id"]: i for i in status["candidates"]["inventory"]}
        self.assertEqual(inventory["12"]["kinds"], ["dischargeable", "fill_unit", "forage_wagon"])
        self.assertEqual((inventory["11"]["category"], inventory["12"]["category"]), ("tractor", "forage_wagon"))

    def test_status_names_a_game_running_an_older_mod(self):
        self.start(build="0ld", autodrive=False)
        status = self.request("status")["result"]
        self.assertFalse(status["ready"])
        self.assertIn("MOD_BUILD_MISMATCH", status["problems"])

    def test_an_older_mod_without_a_build_is_not_ready(self):
        game, live = self.start(autodrive=False)
        game.link.on_frame({"event": "hello", "protocol": 3, "mod_version": "0.1.0.0", "autodrive_version": None})
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
        frame = {"event": "request", "protocol": 3, "session": "s", "id": 1, "action": "fetch_here",
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

    def close_fs25(self, force):
        self.calls.append(("close", host.FS25_IMAGE, force))
        if force or not getattr(self, "stubborn", False):
            self.fs25 = []

    def start(self, program, arguments, folder):
        self.calls.append(("start", program, tuple(arguments), folder))
        if getattr(self, "start_fails", None):
            return {"exit": 1, "stderr": self.start_fails, "stdout": ""}
        if program.endswith("pythonw.exe"):
            self.companion_pid = 4242
            (self.data / "companion.pid").write_text("4242")
            text = (self.data / "app" / "companion" / "buildinfo.py").read_text()
            build = text.split('\nBUILD = "')[1].split('"')[0]
            self.live.mkdir(parents=True, exist_ok=True)
            (self.live / "enabled.json").write_text(json.dumps({"build": build}))
        elif not (getattr(self, "helper_fails", False) and program.endswith("gamelaunchhelper.exe")):
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
        answer = self.runner.responses / f"response-{ident}.json"
        answer.unlink(missing_ok=True)                # a replayed id must not read the earlier answer
        (self.runner.requests / f"request-{ident}.json").write_bytes(body.encode() if isinstance(body, str) else body)
        self.runner.poll_once()
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
            ("BAD_ARGUMENTS", {"op": "run-request-test", "args": {}}),
            ("BAD_ARGUMENTS", {"op": "run-request-test", "args": {"text": "a; rm -rf /"}}),
            ("BAD_ARGUMENTS", {"op": "run-request-test", "args": {"text": "x" * 161}}),
            ("BAD_ARGUMENTS", {"op": "run-request-test", "args": {"text": 5}}),
            ("BAD_ARGUMENTS", {"op": "run-request-test", "args": {"text": "ok", "test_id": "a"}}),
            ("BAD_ARGUMENTS", {"op": "run-hitch-test", "args": {"text": "hitch"}}),
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

    def test_an_unreadable_process_list_is_unknown_never_not_running(self):
        system = host.System()
        answers = [("", "", 1), ("", "<3>WSL ERROR: UtilConnectUnix socket failed 1", 1), ("", "", 0)]
        for out, err, code in answers:
            with self.subTest(err=err, code=code):
                done = unittest.mock.Mock(stdout=out, stderr=err, returncode=code)
                with unittest.mock.patch.object(host.subprocess, "run", return_value=done):
                    with self.assertRaises(host.Refused) as raised:
                        system.processes(host.FS25_IMAGE)
                self.assertEqual(raised.exception.code, "WINDOWS_UNREACHABLE")
        none = unittest.mock.Mock(stdout="INFO: No tasks are running which match the specified criteria.\n",
                                  stderr="", returncode=0)
        running = unittest.mock.Mock(stdout='"FarmingSimulator2025Game.exe","4711","Console","1","9,000 K"\n',
                                     stderr="", returncode=0)
        with unittest.mock.patch.object(host.subprocess, "run", side_effect=[none, running]):
            self.assertEqual(system.processes(host.FS25_IMAGE), [])
            self.assertEqual(system.processes(host.FS25_IMAGE), [4711])
        with unittest.mock.patch.object(host.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaises(host.Refused):
                system.processes(host.FS25_IMAGE)

    def test_install_refuses_when_windows_cannot_be_asked(self):
        self.build()
        self.system.processes = unittest.mock.Mock(side_effect=host.Refused("WINDOWS_UNREACHABLE"))
        self.assertEqual(self.send("install-current-build")["code"], "WINDOWS_UNREACHABLE")
        self.assertFalse(self.targets.mod.exists())

    def test_log_lines_keep_a_whole_maneuver_steer_line_and_stay_bounded(self):
        # Live 2026-10-18: a 400-character cut dropped every field after steeredWheels.
        steer = ("2026-10-18 16:35:05.628 [Farmhand] maneuver LOCAL_APPROACH STEER t=583ms "
                 + " ".join(f"field{i}=0.1234" for i in range(50)) + " speedKmh=3.1215")
        self.assertGreater(len(steer), 700)
        lines = host._bounded([steer] * 3 + ["x" * 5000] * (host.MAX_LOG_LINES + 10))
        self.assertEqual(len(lines), host.MAX_LOG_LINES)
        self.assertTrue(all(len(line) <= host.MAX_LINE_CHARS for line in lines))
        self.assertEqual(host._bounded([steer]), [steer])

    def test_wsl_paths_map_the_fixed_targets(self):
        with unittest.mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(host.local_path(host.FARMHAND_DATA), Path("/mnt/c/Users/Matt/AppData/Local/FS25Farmhand"))


class HostRunnerSingleInstanceTests(RunnerCase):
    """Live 2026-10-18: a second runner on the checkout answered half the requests with
    BAD_TOKEN, and could have taken a hitch request the real runner also acted on."""

    def other_runner(self):
        return host.Runner(self.repo, self.targets, self.system, say=lambda *a, **k: None)

    def runner_json(self):
        return json.loads((self.runner.host / "runner.json").read_text())

    def test_a_second_runner_on_the_checkout_refuses_to_start_and_removes_nothing(self):
        self.runner.take_instance()
        second = self.other_runner()
        with self.assertRaises(host.Refused) as raised:
            second.serve()
        self.assertEqual(raised.exception.code, "ANOTHER_RUNNER")
        self.assertEqual(self.runner_json()["token"], self.runner.token)
        self.assertEqual(self.send("status")["code"], "STATUS")

    def test_a_live_heartbeat_of_another_runner_refuses_start_and_a_stale_one_does_not(self):
        # An older runner, or one across WSL where the OS lock does not reach: no lock,
        # only its heartbeat in runner.json.
        second = self.other_runner()
        with self.assertRaises(host.Refused) as raised:
            second.take_instance()
        self.assertEqual(raised.exception.code, "ANOTHER_RUNNER")
        state = self.runner_json()
        state["beat"] = time.time() - host.RUNNER_FRESH_S - 1
        (self.runner.host / "runner.json").write_text(json.dumps(state))
        second.take_instance()
        second.beat()
        self.assertEqual(self.runner_json()["token"], second.token)

    def test_a_runner_whose_heartbeat_another_took_over_stops_serving(self):
        self.runner.beat()
        state = self.runner_json()
        state["token"] = "f" * 32
        (self.runner.host / "runner.json").write_text(json.dumps(state))
        with self.assertRaises(host.Refused) as raised:
            self.runner.beat()
        self.assertEqual(raised.exception.code, "SUPERSEDED")
        with unittest.mock.patch.object(host, "BEAT_S", 0.01):
            threading.Thread(target=self.runner._beat_loop, daemon=True).start()
            wait_for(self.runner._stop.is_set)
        self.assertEqual(self.runner_json()["token"], "f" * 32)          # never written back

    def test_a_request_is_taken_by_exactly_one_runner(self):
        other = self.other_runner()
        ident = str(time.time_ns())
        (self.runner.requests / f"request-{ident}.json").write_text(json.dumps({
            "version": 1, "id": ident, "token": self.runner.token, "op": "status", "args": {},
            "created": time.time()}))
        read = other.tree.read

        def racing_read(*args):           # the real runner polls while the other one reads
            self.runner.poll_once()
            return read(*args)
        with unittest.mock.patch.object(other.tree, "read", racing_read):
            other.poll_once()
        answers = list(self.runner.responses.iterdir())
        self.assertEqual(len(answers), 1)
        answer = json.loads(answers[0].read_text())
        self.assertEqual((answer["code"], answer["runner"]), ("BAD_TOKEN", host.fingerprint(other.token)))
        self.assertEqual(list(self.runner.requests.iterdir()), [])

    def test_every_answer_names_its_runner(self):
        self.assertEqual(self.send("status")["runner"], host.fingerprint(self.runner.token))
        self.assertEqual(self.send("status", token="0" * 32)["runner"], host.fingerprint(self.runner.token))
        self.assertEqual(host.fingerprint("abc"), client.fingerprint("abc"))
        self.assertEqual(host.RUNNER_FRESH_S, client.RUNNER_STALE_S)


class ClientAnswerIdentityTests(unittest.TestCase):
    """The client takes only the answer of the runner whose token it sent, to its request."""

    def setUp(self):
        self.host = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.host, True)
        for folder in ("requests", "responses"):
            (self.host / folder).mkdir()
        self.token = "a" * 32
        (self.host / "runner.json").write_text(json.dumps({"token": self.token, "beat": time.time() + 3600}))

    def answer_with(self, change):
        stop = threading.Event()
        self.addCleanup(stop.set)
        thread = threading.Thread(target=lambda: responder(), daemon=True)

        def responder():
            while not stop.is_set():
                for path in (self.host / "requests").glob("request-*.json"):
                    request = json.loads(path.read_text())
                    path.unlink()
                    response = {"version": 1, "id": request["id"], "runner": client.fingerprint(self.token),
                                "op": request["op"], "ok": True, "code": "STATUS", "result": {}}
                    change(response)
                    target = self.host / "responses" / f"response-{request['id']}.json"
                    (self.host / "responses" / ".tmp").write_text(json.dumps(response))
                    (self.host / "responses" / ".tmp").replace(target)
                time.sleep(0.01)
        thread.start()
        return stop, thread

    def call(self):
        return client.call("status", host=self.host, timeout=5)

    def test_its_runners_answer_is_taken(self):
        self.answer_with(lambda r: None)
        self.assertEqual(self.call()["code"], "STATUS")

    def test_another_writers_answer_is_never_the_outcome(self):
        cases = [
            ("ANOTHER_RUNNER_ANSWERED", lambda r: r.pop("runner")),                         # an older runner
            ("ANOTHER_RUNNER_ANSWERED", lambda r: r.update(runner=client.fingerprint("b" * 32))),
            ("ANOTHER_RUNNER_ANSWERED", lambda r: r.update(runner=None, ok=False, code="BAD_TOKEN")),
            ("HOST_ANSWER_MISMATCH", lambda r: r.update(id="1")),
            ("HOST_ANSWER_MISMATCH", lambda r: r.update(op="run-hitch-test")),
            ("HOST_ANSWER_MISMATCH", lambda r: r.update(op=None)),                          # ok needs its op
            ("HOST_ANSWER_MISMATCH", lambda r: r.update(ok="yes")),
            ("HOST_ANSWER_MISMATCH", lambda r: r.update(version=2)),
            ("HOST_ANSWER_MISMATCH", lambda r: r.update(result=[])),
        ]
        for code, change in cases:
            with self.subTest(code=code):
                stop, thread = self.answer_with(change)
                with self.assertRaises(client.Unavailable) as raised:
                    self.call()
                stop.set()
                thread.join()
                self.assertEqual(raised.exception.code, code)

    def test_a_refusal_before_the_op_is_read_is_its_runners_answer(self):
        self.answer_with(lambda r: r.update(op=None, ok=False, code="MALFORMED"))
        self.assertEqual(self.call()["code"], "MALFORMED")


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
        # The package's launch helper first (appxmanifest Application Executable).
        helper = str(PureWindowsPath(host.FS25_EXE).parent.parent / "gamelaunchhelper.exe")
        answer = self.send("launch-fs25")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.system.calls, [("start", helper, ("-autoStartSavegameId", "3"),
                                              str(PureWindowsPath(helper).parent))])
        self.assertEqual(self.send("launch-fs25")["code"], "FS25_RUNNING")

    def test_a_launch_the_helper_cannot_start_falls_back_to_the_game_exe(self):
        self.system.helper_fails = True
        with unittest.mock.patch.object(host, "GAME_APPEAR_S", 0.2):
            self.assertEqual(self.send("launch-fs25")["code"], "LAUNCHED")
        helper = str(PureWindowsPath(host.FS25_EXE).parent.parent / "gamelaunchhelper.exe")
        self.assertEqual([c[1] for c in self.system.calls], [helper, host.FS25_EXE])

    def test_restart_stops_only_fs25_then_waits_for_ready(self):
        self.system.fs25 = [1]
        # Live 2026-10-18: the old game's last snapshot is not the new game being ready.
        old = (True, "STATUS", {"farmhand_ready": True, "live": {"game": {"last_frame_age_s": 9999}}})
        new = (True, "STATUS", {"farmhand_ready": True, "live": {"game": {"last_frame_age_s": 0.0}}})
        with unittest.mock.patch.object(host, "READY_S", 30), unittest.mock.patch.object(host.time, "sleep"), \
                unittest.mock.patch.object(self.runner, "op_status", side_effect=[old, old, new]) as status:
            answer = self.send("restart-fs25")
        self.assertEqual((answer["ok"], answer["code"]), (True, "READY"))
        self.assertEqual(status.call_count, 3)
        self.assertEqual(self.system.calls[0], ("close", host.FS25_IMAGE, False))
        self.assertEqual(self.system.calls[1][:2],
                         ("start", str(PureWindowsPath(host.FS25_EXE).parent.parent / "gamelaunchhelper.exe")))

    def test_a_stale_game_link_is_not_ready(self):
        self.system.fs25, self.system.companion_pid = [1], 4242
        (self.data / "companion.pid").write_text("4242")
        live = {"game": {"linked": True, "last_frame_age_s": 64.8}, "problems": ["GAME_STALE_SNAPSHOT"],
                "ready": False}
        with unittest.mock.patch.object(self.runner.companion, "ask", return_value={"result": live}):
            self.assertFalse(self.runner.op_status()[2]["farmhand_ready"])
            live.update(problems=[], ready=True)
            self.assertTrue(self.runner.op_status()[2]["farmhand_ready"])

    def test_a_launch_that_never_appears_says_what_the_starter_said(self):
        self.system.start_fails = "Start-Process : This command cannot be run due to the error: Access is denied."
        with unittest.mock.patch.object(host, "GAME_APPEAR_S", 0.1):
            answer = self.send("launch-fs25")
        self.assertEqual(answer["code"], "FS25_DID_NOT_START")
        for starter in ("gamelaunchhelper.exe", "FarmingSimulator2025Game.exe"):
            self.assertIn("Access is denied", answer["result"]["starter"][starter]["stderr"])

    def test_restart_reports_not_ready_within_its_bound(self):
        with unittest.mock.patch.object(self.runner, "op_status",
                                        return_value=(True, "STATUS", {"farmhand_ready": False})):
            code = None
            try:
                self.runner.op_restart_fs25(ready_timeout=0.1)
            except host.Refused as error:
                code = error.code
        self.assertEqual(code, "NOT_READY")

    def test_stop_fs25_closes_only_fs25(self):
        self.system.fs25 = [999]
        answer = self.send("stop-fs25")
        self.assertEqual((answer["ok"], answer["code"], answer["result"]), (True, "STOPPED", {"fs25_pids": [999]}))
        self.assertEqual(self.system.calls, [("close", host.FS25_IMAGE, False)])
        self.assertEqual(self.send("stop-fs25")["code"], "NOT_RUNNING")
        self.assertEqual(len(self.system.calls), 1)

    def test_stop_fs25_forces_only_after_asking(self):
        self.system.fs25, self.system.stubborn = [999], True
        with unittest.mock.patch.object(host, "GAME_CLOSE_S", 0.1):
            self.assertEqual(self.send("stop-fs25")["code"], "STOPPED")
        self.assertEqual(self.system.calls, [("close", host.FS25_IMAGE, False), ("close", host.FS25_IMAGE, True)])

    def test_stop_fs25_never_closes_the_game_under_a_running_job_or_test(self):
        self.system.fs25 = [999]
        for result in ({"job": {"running": "owner-1"}}, {"test_active": "owner-1"}):
            with self.subTest(result=result), unittest.mock.patch.object(
                    self.runner.companion, "ask", return_value={"ok": True, "result": result}):
                self.assertEqual(self.send("stop-fs25")["code"], "JOB_RUNNING")
        self.assertEqual(self.system.calls, [])
        self.assertEqual(self.system.fs25, [999])

    def test_stop_fs25_takes_no_argument_and_kills_no_other_program(self):
        for args in ({"image": "explorer.exe"}, {"pid": 4}, {"force": True}):
            with self.subTest(args=args):
                self.assertEqual(self.send("stop-fs25", args=args)["code"], "BAD_ARGUMENTS")
        self.assertEqual(self.system.calls, [])
        ran = []
        system = host.System()
        with unittest.mock.patch.object(system, "_run", lambda args, timeout=30.0: ran.append(args)):
            system.close_fs25(force=False)
            system.close_fs25(force=True)
        self.assertEqual(ran, [["taskkill.exe", "/IM", "FarmingSimulator2025Game.exe"],
                               ["taskkill.exe", "/F", "/IM", "FarmingSimulator2025Game.exe"]])
        self.assertFalse(hasattr(system, "close"))

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

    def test_claude_runs_the_players_typed_words_and_reads_the_result(self):
        game = self.wire(faults={"stage_end": (5.52, -17.97, 162.9)})
        self.assertEqual(client.main(["request", "hitch the forage wagon"]), 0, self.out)
        self.assertEqual((self.out[0]["event"], self.out[-1]["status"]), ("started", "COMPLETE"))
        self.assertEqual([c["op"] for c in game.commands], LIVE_HITCH)
        self.out.clear()
        self.assertEqual(client.main(["request", "sing me a song"]), client.INFRASTRUCTURE)
        self.assertEqual(self.out[-1]["code"], "NOT_UNDERSTOOD")

    def test_a_failed_test_exits_nonzero_with_its_reason(self):
        game = self.wire(faults={"stage_end": (3.44, -12.40, 85.9), "maneuver": "FAILED:LOCAL_PATH_HITS_WAGON"})
        self.assertEqual(client.command_hitch(poll=0.05), client.EXIT["FAILED"])
        self.assertEqual(self.out[-1]["reason"], "LOCAL_PATH_HITS_WAGON")
        # Refused unmoved; the fake game's STAGE ends where the first did, so no further
        # aim moves the tractor and the maneuver is not repeated; never reversed.
        self.assertEqual([c["op"] for c in game.commands], ["stage", "maneuver"] + ["stage"] * 5 + ["stop"])

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
