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
