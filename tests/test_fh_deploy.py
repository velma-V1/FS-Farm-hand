"""scripts/fh_deploy.py: the reversible lab deployment keeps every byte it replaces."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fh_deploy  # noqa: E402
import fs25_host_runner as runner  # noqa: E402


class FailingStart(fh_deploy.RehearsalHost):
    def __init__(self):
        super().__init__()
        self.fail_next = True

    def start_companion(self, build):
        self.events.append(f"start {build}")
        if self.fail_next:
            self.fail_next = False
            raise runner.Refused("COMPANION_DID_NOT_START", build=build)
        return 1


class RewritesSettingsOnStart(fh_deploy.RehearsalHost):
    """The companion rewrites its settings while it starts: expected, not a failure."""

    def __init__(self, data):
        super().__init__()
        self.data = data

    def start_companion(self, build):
        (self.data / "settings.json").write_text('{"pay": 10, "scenery_ghosting": false}', encoding="utf-8")
        return super().start_companion(build)


class ChangesBuildOnStart(fh_deploy.RehearsalHost):
    """Something alters an installed build file after the start: the final check must fail."""

    def __init__(self, data):
        super().__init__()
        self.data, self.first = data, True

    def start_companion(self, build):
        if self.first:
            self.first = False
            (self.data / "app" / "companion" / "ai.py").write_bytes(b"# altered\n")
        return super().start_companion(build)


class ReloadAlwaysFails(fh_deploy.RehearsalHost):
    def reload(self):
        self.events.append("reload")
        return False, "fhReload: a Farmhand command is running; stop it first"


class FakeCompanion:
    def __init__(self, answer):
        self.answer = answer

    def ask(self, command):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeRunner:
    def __init__(self, pid, answer, game):
        self.pid, self.companion, self.game = pid, FakeCompanion(answer), game
        self.system = self

    def _companion_pid(self):
        return self.pid

    def processes(self, image):
        return [7] if self.game else []


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="fh-deploy-test-"))
        self.checks = fh_deploy.rehearse(self.base, say=lambda *_: None)
        self.data = self.base / "data"
        self.targets = runner.Targets(str(self.base / "profile"), str(self.base / "profile"), str(self.data),
                                      local=lambda p: Path(p))

    def deployer(self, host):
        return fh_deploy.Deployer(self.targets, host, self.base / "state", say=lambda *_: None)

    def test_round_trip_restores_the_lab_byte_for_byte(self):
        self.assertTrue(self.checks.pop("ok"))
        self.assertEqual(self.checks, {k: True for k in self.checks})

    def test_companion_that_does_not_start_puts_the_lab_back(self):
        host = FailingStart()
        deployer = self.deployer(host)
        before = deployer.installed().digest()
        with self.assertRaises(fh_deploy.DeployError) as caught:
            deployer.switch("abc1234")
        self.assertEqual(caught.exception.code, "ROLLED_BACK")
        self.assertEqual(caught.exception.detail["failed"], "COMPANION_DID_NOT_START")
        self.assertEqual(deployer.installed().digest(), before)
        self.assertIn("start 2f3d1f6-dirty", host.events)       # the lab's own companion again
        self.assertIsNone(deployer._active())

    def test_second_switch_keeps_the_first_baseline(self):
        deployer = self.deployer(fh_deploy.RehearsalHost())
        lab = deployer.installed().digest()
        deployer.switch("abc1234")
        first = deployer._active()["baseline"]
        deployer.switch("abc1234")
        self.assertEqual(deployer._active()["baseline"], first)
        deployer.restore()
        self.assertEqual(deployer.installed().digest(), lab)

    def test_refuses_while_a_job_runs_or_a_reload_is_pending(self):
        for name in ("job_running", "reload_pending"):
            host = fh_deploy.RehearsalHost()
            setattr(host, name, lambda: True)
            deployer = self.deployer(host)
            before = deployer.installed().digest()
            with self.assertRaises(fh_deploy.DeployError):
                deployer.switch("abc1234")
            self.assertEqual(deployer.installed().digest(), before)
            self.assertNotIn("stop", host.events)

    def test_game_closed_needs_no_reload(self):
        host = fh_deploy.RehearsalHost(running=False)
        deployer = self.deployer(host)
        deployer.switch("abc1234")
        deployer.restore()
        self.assertNotIn("reload", host.events)

    def test_tampered_build_or_snapshot_is_refused(self):
        deployer = self.deployer(fh_deploy.RehearsalHost())
        drive = self.base / "state" / "builds" / "abc1234" / "mod" / runner.MOD_NAME / "FarmhandDrive.lua"
        drive.write_bytes(b"-- changed\n")
        with self.assertRaises(fh_deploy.DeployError) as caught:
            deployer.switch("abc1234")
        self.assertEqual(caught.exception.code, "BUILD_HASH_MISMATCH")
        snap = sorted((self.base / "state" / "snapshots").iterdir())[0]
        next(p for p in (snap / "mod").rglob("*") if p.is_file()).write_bytes(b"x")
        with self.assertRaises(fh_deploy.DeployError) as caught:
            deployer.load_snapshot(snap.name)
        self.assertEqual(caught.exception.code, "SNAPSHOT_CORRUPT")

    def test_companion_rewriting_state_after_start_is_not_a_failure(self):
        deployer = self.deployer(RewritesSettingsOnStart(self.data))
        deployer.switch("abc1234")
        self.assertEqual(deployer._active()["build"], "abc1234")
        self.assertNotIn("pending", deployer._active())
        self.assertEqual(fh_deploy.build_of(deployer.installed().app), "abc1234")

    def test_final_verification_failure_rolls_back_inside_the_transaction(self):
        deployer = self.deployer(ChangesBuildOnStart(self.data))
        before = deployer.installed().digest()
        with self.assertRaises(fh_deploy.DeployError) as caught:
            deployer.switch("abc1234")
        self.assertEqual(caught.exception.code, "ROLLED_BACK")
        self.assertEqual(caught.exception.detail["failed"], "INSTALLED_MISMATCH")
        self.assertTrue(caught.exception.detail["restored"]["ok"])
        self.assertEqual(deployer.installed().digest(), before)
        self.assertIsNone(deployer._active())

    def test_incomplete_rollback_is_reported_and_restore_stays_possible(self):
        host = ReloadAlwaysFails()
        deployer = self.deployer(host)
        before = deployer.installed().digest()
        with self.assertRaises(fh_deploy.DeployError) as caught:
            deployer.switch("abc1234")
        self.assertEqual(caught.exception.code, "ROLLBACK_INCOMPLETE")
        restored = caught.exception.detail["restored"]
        self.assertFalse(restored["ok"])
        self.assertTrue(any(p.startswith("reload") for p in restored["problems"]))
        active = deployer._active()
        self.assertEqual(active["incomplete"], "ROLLBACK_INCOMPLETE")
        self.assertIn("baseline", active)
        # Once the game reloads again, restore reaches the lab's exact bytes.
        healthy = self.deployer(fh_deploy.RehearsalHost())
        healthy.restore()
        self.assertEqual(healthy.installed().digest(), before)
        self.assertIsNone(healthy._active())

    def test_uncertain_job_status_never_authorizes(self):
        host = object.__new__(fh_deploy.Host)
        idle = {"result": {"job": {"running": None}, "test_active": False}}
        cases = [(FakeRunner(None, idle, game=False), False),
                 (FakeRunner(5, idle, game=True), False),
                 (FakeRunner(5, {"result": {"job": {"running": {"owner": "x"}}, "test_active": False}}, True), True)]
        for fake, expected in cases:
            host.runner = fake
            self.assertIs(host.job_running(), expected)
        for fake in (FakeRunner(None, idle, game=True),                          # game up, companion down
                     FakeRunner(5, runner.Refused("COMPANION_NO_ANSWER"), True),
                     FakeRunner(5, {"result": {}}, True),
                     FakeRunner(5, {}, True)):
            host.runner = fake
            with self.assertRaises(fh_deploy.DeployError) as caught:
                host.job_running()
            self.assertEqual(caught.exception.code, "JOB_STATUS_UNKNOWN")

    def test_this_checkout_builds_a_deployable_build(self):
        out = self.base / "real"
        subprocess_args = [sys.executable, str(fh_deploy.ROOT / "scripts" / "package_probe.py"), "--build-dev", str(out)]
        import subprocess
        subprocess.run(subprocess_args, check=True, stdout=subprocess.DEVNULL)
        manifest = json.loads((out / "MANIFEST.json").read_text())
        contents = {n: (out / n).read_bytes() for n in manifest["files"]}
        deployer = self.deployer(fh_deploy.RehearsalHost())
        build = deployer.keep_build(manifest, contents)
        install = deployer.load_build(build)
        self.assertEqual(set(install.mod), set(runner.MOD_FILES))
        self.assertEqual(fh_deploy.build_of(install.app), manifest["build"])
        # The installed Lua must still carry the background reload a restore depends on.
        bridge = install.mod["FarmhandBridge.lua"].decode()
        self.assertIn('.fhReload"', bridge)
        self.assertIn("consoleReload", install.mod["FarmhandProbe.lua"].decode())
        for name in fh_deploy.RELOADED:
            self.assertIn(f'"{name}"', install.mod["FarmhandProbe.lua"].decode())


if __name__ == "__main__":
    unittest.main()
