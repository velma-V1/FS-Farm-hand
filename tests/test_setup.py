"""Setup: one install that needs nothing else, updates that keep the user's
choices, a clean uninstall; plus a real Windows install of the built package."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

from companion import setup


def fake_package(root: Path) -> Path:
    for part in ("runtime", "app/companion", "mod/FS25_Farmhand"):
        (root / part).mkdir(parents=True)
    (root / "runtime" / "pythonw.exe").write_bytes(b"new runtime")
    (root / "app" / "companion" / "service.py").write_text("# new")
    (root / "mod" / "FS25_Farmhand" / "modDesc.xml").write_text("<modDesc/>")
    return root


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.package = fake_package(self.root / "package")
        self.profile, self.data = self.root / "FarmingSimulator2025", self.root / "lad" / "FS25Farmhand"
        self.profile.mkdir()
        self.registered = []
        patch = unittest.mock.patch.object(setup, "set_autostart", self.registered.append)
        patch.start()
        self.addCleanup(patch.stop)
        self.ui = setup.Dialogs(quiet=True)

    def install(self):
        return setup.install(self.package, self.profile, self.data, self.ui, start=False)

    def test_install_puts_the_mod_and_the_companion_in_place_and_registers_it(self):
        self.assertEqual(self.install(), 0)
        self.assertTrue((self.profile / "mods" / "FS25_Farmhand" / "modDesc.xml").is_file())
        self.assertEqual((self.data / "runtime" / "pythonw.exe").read_bytes(), b"new runtime")
        self.assertTrue((self.data / "app" / "companion" / "service.py").is_file())
        self.assertEqual(self.registered, [f'"{self.data / "runtime" / "pythonw.exe"}" -m companion.service'])

    def test_an_update_replaces_the_program_and_keeps_the_users_choices(self):
        self.install()
        (self.data / "settings.json").write_text('{"ai": {"provider": "none"}}')
        (self.data / "model").mkdir()
        (self.data / "model" / "Qwen3-1.7B-Q4_K_M.gguf").write_bytes(b"weights")
        (self.data / "app" / "companion" / "stale.py").write_text("# from the old version")
        (self.profile / "mods" / "FS25_Farmhand" / "old.lua").write_text("-- old")
        self.assertEqual(self.install(), 0)
        self.assertEqual((self.data / "settings.json").read_text(), '{"ai": {"provider": "none"}}')
        self.assertTrue((self.data / "model" / "Qwen3-1.7B-Q4_K_M.gguf").is_file())
        self.assertFalse((self.data / "app" / "companion" / "stale.py").exists())
        self.assertFalse((self.profile / "mods" / "FS25_Farmhand" / "old.lua").exists())

    def test_no_fs25_or_an_incomplete_package_changes_nothing(self):
        missing = self.root / "nowhere"
        self.assertEqual(setup.install(self.package, missing, self.data, self.ui, start=False), 1)
        self.assertFalse(self.data.exists())
        broken = fake_package(self.root / "broken")
        __import__("shutil").rmtree(broken / "runtime")
        self.assertEqual(setup.install(broken, self.profile, self.data, self.ui, start=False), 1)
        self.assertFalse((self.profile / "mods").exists())
        self.assertEqual(self.registered, [])

    def test_a_running_companion_is_asked_to_quit_first(self):
        self.data.mkdir(parents=True)
        (self.data / setup.PID_FILE).write_text("123")

        def companion():                         # polls the flag like serve() does
            while not (self.data / setup.QUIT_FILE).exists():
                time.sleep(0.05)
            (self.data / setup.PID_FILE).unlink()

        threading.Thread(target=companion, daemon=True).start()
        self.assertEqual(self.install(), 0)
        self.assertFalse((self.data / setup.QUIT_FILE).exists())    # cleared for the new one

    def test_a_companion_that_does_not_quit_stops_the_install(self):
        self.data.mkdir(parents=True)
        (self.data / setup.PID_FILE).write_text("123")
        with unittest.mock.patch.object(setup, "QUIT_WAIT_S", 0.3):
            self.assertFalse(setup.stop_running(self.data, wait=0.3))
            self.assertEqual(setup.install(self.package, self.profile, self.data, self.ui, start=False), 1)
        self.assertFalse((self.profile / "mods").exists())

    def test_uninstall_removes_everything_it_installed(self):
        self.install()
        (self.data / "settings.json").write_text("{}")
        self.assertEqual(setup.uninstall(self.profile, self.data, self.ui), 0)   # quiet: yes to both
        self.assertFalse((self.profile / "mods" / "FS25_Farmhand").exists())
        self.assertFalse(self.data.exists())
        self.assertEqual(self.registered[-1], None)

    def test_quoted_paths_are_refused(self):
        with self.assertRaises(ValueError):
            setup.autostart_command(Path('C:/odd"name'))


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("FARMHAND_PACKAGE"),
                     "a real install: Windows CI with the built package")
class WindowsInstallTests(unittest.TestCase):
    def test_install_starts_the_companion_and_uninstall_removes_it(self):
        import winreg
        package = Path(os.environ["FARMHAND_PACKAGE"]).resolve()
        root = Path(tempfile.mkdtemp())
        profile = root / "FarmingSimulator2025"
        profile.mkdir()
        env = dict(os.environ, LOCALAPPDATA=str(root / "lad"))
        data = root / "lad" / "FS25Farmhand"
        python = package / "runtime" / "python.exe"
        run = lambda *args: subprocess.run([str(python), "-m", "companion.setup", *args, "--quiet",
                                            "--profile", str(profile)], env=env, timeout=120)
        self.addCleanup(lambda: run("uninstall"))
        self.assertEqual(run("install").returncode, 0)
        self.assertTrue((profile / "mods" / "FS25_Farmhand" / "modDesc.xml").is_file())
        self.assertTrue((data / setup.PID_FILE).is_file())                   # the companion runs
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, setup.RUN_KEY) as key:
            command, _ = winreg.QueryValueEx(key, setup.RUN_VALUE)
        self.assertIn(str(data / "runtime" / "pythonw.exe"), command)
        log = (profile / "modSettings" / "FS25_Farmhand" / "companion.log").read_text()
        self.assertIn("companion started", log)
        self.assertEqual(run("install").returncode, 0)                       # update over a running one
        self.assertEqual(run("uninstall").returncode, 0)
        self.assertFalse((data / setup.PID_FILE).exists())
        self.assertFalse((profile / "mods" / "FS25_Farmhand").exists())
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, setup.RUN_KEY) as key:
            with self.assertRaises(FileNotFoundError):
                winreg.QueryValueEx(key, setup.RUN_VALUE)


if __name__ == "__main__":
    unittest.main()
