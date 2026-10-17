"""Install, update or remove Farmhand on this PC: one double-click, Windows dialogs only.

    Setup.cmd       -> runtime\\pythonw.exe -m companion.setup install
    Uninstall.cmd   -> runtime\\pythonw.exe -m companion.setup uninstall

The package carries its own Python (the official Windows embeddable 3.12, with the
pinned packages already inside), so the user installs nothing else. Install:
finds the FS25 profile, asks a running companion to quit (the quit flag), copies
the mod into the game's mods folder and the companion into
%LOCALAPPDATA%\\FS25Farmhand, registers the windowless start at logon, starts it,
and checks that it came up. The user's settings and downloaded models are kept
across updates. Uninstall removes the logon start, the mod and the companion, and
asks before deleting downloaded models.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import time

from companion.ai import data_dir
from companion.bridge import game_profile_dir

TITLE = "FS25 Farmhand"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "FS25 Farmhand companion"
MOD_NAME = "FS25_Farmhand"
PID_FILE, QUIT_FILE = "companion.pid", "quit"
QUIT_WAIT_S, START_WAIT_S = 45.0, 20.0
# Replaced on install and removed on uninstall; everything else in the data folder
# (settings.json, downloaded models) is the user's and stays on update.
PROGRAM_DIRS = ("runtime", "app")


class Dialogs:
    """Windows message boxes; ``quiet`` prints instead (CI, diagnostics)."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet

    def _box(self, text: str, flags: int) -> int:
        if self.quiet or sys.platform != "win32":
            print(text)
            return 6                                   # IDYES
        import ctypes
        return ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags)

    def info(self, text: str) -> None:
        self._box(text, 0x40)                          # MB_ICONINFORMATION

    def error(self, text: str) -> None:
        self._box(text, 0x10)                          # MB_ICONERROR

    def ask(self, text: str) -> bool:
        return self._box(text, 0x04 | 0x20) == 6       # MB_YESNO | MB_ICONQUESTION -> IDYES


def autostart_command(data: Path, profile: Path | None = None) -> str:
    """The logon command: the installed, windowless runtime running the service."""
    command = f'"{data / "runtime" / "pythonw.exe"}" -m companion.service'
    if profile is not None:
        command += f' --profile "{profile}"'
    if '"' in str(data) or (profile is not None and '"' in str(profile)):
        raise ValueError("paths with quotes are not supported")
    return command


def set_autostart(command: str | None) -> None:
    import winreg  # Windows only
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if command is None:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)


def stop_running(data: Path, wait: float | None = None) -> bool:
    """Ask a running companion to quit (it polls the flag every second, stops a
    running job first) and wait until its pid file is gone. True if none runs."""
    pid = data / PID_FILE
    if not pid.exists():
        return True
    data.mkdir(parents=True, exist_ok=True)
    (data / QUIT_FILE).write_text("quit", encoding="utf-8")
    deadline = time.monotonic() + (QUIT_WAIT_S if wait is None else wait)
    while pid.exists() and time.monotonic() < deadline:
        time.sleep(0.25)
    return not pid.exists()


def _replace_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def install(package: Path, profile: Path | None, data: Path, ui: Dialogs, start=True,
            pass_profile: bool = False) -> int:
    profile = profile or game_profile_dir()
    if not profile.is_dir():
        ui.error(f"Farming Simulator 25 was not found ({profile}).\n\nStart FS25 once, then run Setup again.")
        return 1
    for part in ("runtime", "app", f"mod/{MOD_NAME}"):
        if not (package / part).is_dir():
            ui.error(f"This Farmhand package is incomplete ({part} is missing). Download it again.")
            return 1
    if not stop_running(data):
        ui.error("The running Farmhand companion did not stop. Restart Windows, then run Setup again.")
        return 1
    (profile / "mods").mkdir(exist_ok=True)
    _replace_tree(package / "mod" / MOD_NAME, profile / "mods" / MOD_NAME)
    data.mkdir(parents=True, exist_ok=True)
    for name in PROGRAM_DIRS:
        _replace_tree(package / name, data / name)
    (data / QUIT_FILE).unlink(missing_ok=True)
    command = autostart_command(data, profile if pass_profile else None)
    set_autostart(command)
    if start:
        subprocess.Popen(command, creationflags=0x00000008 | 0x00000200, close_fds=True)  # detached
        deadline = time.monotonic() + START_WAIT_S
        while not (data / PID_FILE).exists() and time.monotonic() < deadline:
            time.sleep(0.25)
        if not (data / PID_FILE).exists():
            ui.error(f"Farmhand was installed but its companion did not start.\n\nSee {data / 'companion.log'}.")
            return 1
    ui.info("Farmhand is installed and running.\n\n"
            "Start Farming Simulator 25 and load a save: Farmhand connects by itself.\n"
            "On foot: Left Alt + K to tell Farmhand what to do, Left Alt + L to stop,\n"
            "Left Alt + N to choose an AI model (optional; offered once in game).")
    return 0


def uninstall(profile: Path | None, data: Path, ui: Dialogs) -> int:
    if not ui.ask("Remove Farmhand from this PC?"):
        return 0
    if not stop_running(data):
        ui.error("The running Farmhand companion did not stop. Restart Windows, then try again.")
        return 1
    set_autostart(None)
    profile = profile or game_profile_dir()
    shutil.rmtree(profile / "mods" / MOD_NAME, ignore_errors=True)
    for name in PROGRAM_DIRS:
        shutil.rmtree(data / name, ignore_errors=True)
    (data / QUIT_FILE).unlink(missing_ok=True)
    if data.exists() and any(data.iterdir()) and ui.ask(
            "Also delete Farmhand's settings and downloaded AI and speech models?"):
        shutil.rmtree(data, ignore_errors=True)
    ui.info("Farmhand was removed.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--profile", type=Path, help="FarmingSimulator2025 folder (default: Documents)")
    parser.add_argument("--data", type=Path, help="install folder (default: %%LOCALAPPDATA%%\\FS25Farmhand)")
    parser.add_argument("--quiet", action="store_true", help="print instead of showing dialogs")
    args = parser.parse_args(argv)
    ui = Dialogs(args.quiet)
    data = args.data or data_dir()
    try:
        if args.action == "install":
            package = Path(sys.executable).resolve().parents[1]      # <package>\runtime\python(w).exe
            return install(package, args.profile, data, ui, pass_profile=args.profile is not None)
        return uninstall(args.profile, data, ui)
    except Exception as error:                                       # always tell the user
        ui.error(f"Farmhand setup failed: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
