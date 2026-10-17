"""The Farmhand companion as a background process (Windows, next to FS25).

    python -m companion.service --install      start now and at every Windows logon
    python -m companion.service --uninstall    remove the logon start
    python -m companion.service                run in this terminal (diagnostics)

Installed, it runs without a window. It hosts the local link, waits for the game,
and runs AC-001 "here" each time the player presses the Farmhand key in game
(mod/FarmhandBridge.lua requestHere). The key press is only a wish: this process
supplies the request id and the farm, and the executor selects the equipment. The
outcome is shown in game as a notification (CommandLink.notice). The log is
modSettings/FS25_Farmhand/companion.log in the FS25 profile folder.

farmhand.json (optional trusted local configuration; tractors are recognised
without it):
    {"tractor_store_categories": ["<categoryName from fhHitchList>", ...],
     "field_entrances": {"<map_id>": {"12": "<AutoDrive marker name>"}}}
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys

from adapters.world import StepUnverified
from companion.bridge import (MOD_DIR, SNAPSHOT_TIMEOUT_S, FS25World, game_profile_dir,
                              start_local_link)
from companion.executor import Executor

log = logging.getLogger("farmhand")

ROOT = Path(__file__).resolve().parents[1]
# The game never re-adopts a session it dropped (e.g. after 10 s without a beat
# while the PC slept); a live game naming another session this long gets a new one.
RENEW_AFTER_S = 15.0
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "FS25 Farmhand companion"
LOG_FILE = "companion.log"


def load_config(path: Path) -> tuple[dict[str, str], dict]:
    config = json.loads(path.read_text(encoding="utf-8"))
    names = config.get("tractor_store_categories", [])
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise ValueError("tractor_store_categories must be a list of names")
    entrances = {}
    for map_id, fields in (config.get("field_entrances") or {}).items():
        if not isinstance(fields, dict):
            raise ValueError("field_entrances must map map_id -> {field: marker name}")
        entrances[map_id] = {int(field): name for field, name in fields.items()}
    return {name: "tractor" for name in names}, entrances


def notice_code(status: str, reason: str) -> str:
    """A session-file token for a result (protocol.ipc encode_session)."""
    if status == "COMPLETE":
        return "COMPLETE"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{status}_{reason}")[:64]


def handle(world: FS25World, executor: Executor, request_id: int) -> None:
    """Run one player request as AC-001 "here", telling the player what happened."""
    link = world.link
    try:
        frame = link.fresh_snapshot()
    except StepUnverified as error:
        link.notice(notice_code("BLOCKED", error.code))
        return
    if frame["session"] != link.session:
        link.notice("BLOCKED_NOT_LINKED")
        return
    for owner in world.retained():             # an earlier run's unconfirmed stop
        world.recover(owner)
    intent = {"version": 1, "request_id": f"{link.session}-{request_id}",
              "task": "deliver_tractor_and_forage_wagon", "farm_id": str(frame["farm_id"]),
              "target": "player_at_acceptance"}
    log.info("request %s", json.dumps(intent))
    link.notice("ACCEPTED")
    result = executor.execute(intent)
    log.info("result %s", json.dumps({"status": result.status, "reason": result.reason,
                                      "equipment": list(result.equipment_ids), "states": list(result.states)}))
    link.notice(notice_code(result.status, result.reason))


def serve(world: FS25World, field_entrances=None, should_stop=lambda: False) -> None:
    """Answer the player's requests, one at a time, until the link closes."""
    link = world.link
    executor = Executor(world, field_entrances=field_entrances)
    foreign_since = None
    while not link.closed.is_set() and not should_stop():
        request_id = link.next_request(timeout=1.0)
        if request_id is not None:
            try:
                handle(world, executor, request_id)
            except Exception:   # keep serving; the executor has already stopped its work
                log.exception("request %d failed", request_id)
                link.notice("FAILED_INTERNAL_ERROR")
            finally:
                link.done()
            continue
        session, last_frame = link.linked_session()
        now = link.clock()
        if last_frame is not None and now - last_frame < 3 * SNAPSHOT_TIMEOUT_S and session != link.session:
            foreign_since = now if foreign_since is None else foreign_since
            if now - foreign_since > RENEW_AFTER_S:
                link.renew()
                foreign_since = None
        else:
            foreign_since = None


# ------------------------------------------------------------- Windows logon

def autostart_command(config: Path | None = None) -> str:
    """The logon command: windowless pythonw running this module from ROOT."""
    pythonw = PureWindowsPath(sys.executable).with_name("pythonw.exe")
    paths = [pythonw, ROOT] + ([config] if config else [])
    if any(c in str(p) for p in paths for c in "\"'"):
        raise ValueError("paths with quotes are not supported")
    args = repr(["--config", str(config.resolve())] if config else [])
    code = f"import sys; sys.path.insert(0, r'{ROOT}'); import companion.service as s; s.main({args})"
    return f'"{pythonw}" -c "{code}"'


def install(config: Path | None = None) -> int:
    import winreg  # Windows only
    command = autostart_command(config)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
    detached = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(command, creationflags=detached, close_fds=True)
    print(f"Installed: the Farmhand companion starts at logon and is running now.\n{command}")
    return 0


def uninstall() -> int:
    import winreg  # Windows only
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass
    print("Removed the logon start. A running companion stops with the next logoff or in Task Manager.")
    return 0


def configure_logging(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(folder / LOG_FILE, maxBytes=1_000_000, backupCount=2,
                                                     encoding="utf-8")]
    if sys.stderr is not None:                  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    parser.add_argument("--config", type=Path, help="optional farmhand.json")
    parser.add_argument("--profile", type=Path, help="FarmingSimulator2025 folder (default: Documents)")
    args = parser.parse_args(argv)
    if args.install:
        return install(args.config)
    if args.uninstall:
        return uninstall()
    profile = args.profile or game_profile_dir()
    if not profile.is_dir():
        raise SystemExit(f"FS25 profile folder not found: {profile}")
    configure_logging(profile.joinpath(*MOD_DIR))
    categories, entrances = load_config(args.config) if args.config else ({}, {})
    link = start_local_link(profile)
    log.info("companion started; session %s; waiting for FS25", link.session)
    try:
        serve(FS25World(link, categories), entrances)
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
