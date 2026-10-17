"""The Farmhand companion as a background process (Windows, next to FS25).

    python -m companion.service                run in this terminal (diagnostics)

Installed by Setup (companion/setup.py), it starts at logon without a window. It hosts the local link, waits for the game
and answers the player: the Farmhand key asks for AC-001 "here" directly; typed
words are interpreted (companion/language.py) into a command proposal. Either is
only a wish: the coordinator (companion/coordinator.py) supplies the request id
and farm, runs one job at a time, and answers STOP and STATUS at once; the
executor selects the equipment. Replies are shown in game as notifications
(CommandLink.notice). The log is modSettings/FS25_Farmhand/companion.log in the
FS25 profile folder.

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
import os
from pathlib import Path
import sys
import threading
import time

from adapters.world import StepUnverified
from companion.bridge import (MOD_DIR, SNAPSHOT_TIMEOUT_S, FS25World, game_profile_dir,
                              start_local_link)
from companion.ai import AI, Settings, data_dir
from companion.coordinator import FETCH_HERE, Coordinator
from companion.executor import Executor
from companion.language import RuleInterpreter, refuses
from companion.setup import PID_FILE, QUIT_FILE
from companion.voice import Replies, SapiSpeaker, SoundDeviceMicrophone, Unavailable, VoiceInput, WhisperTranscriber
from protocol.ipc import encode_menu

log = logging.getLogger("farmhand")

# The game never re-adopts a session it dropped (e.g. after 10 s without a beat
# while the PC slept); a live game naming another session this long gets a new one.
RENEW_AFTER_S = 15.0
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


def _no_voice():
    raise Unavailable("voice not set up")


# The fixed keys need no interpretation: STOP never waits for words or a model.
ACTIONS = {"fetch_here": FETCH_HERE, "stop": {"version": 1, "command": "stop"}}


def proposal_for(request: dict, rules) -> dict | None:
    """The command proposal for one request: a fixed key as is, a model's answer
    as given, typed or spoken words by the deterministic rules (None when not
    understood)."""
    if "proposal" in request:
        return request["proposal"]
    if "text" in request:
        proposal = rules.interpret(request["text"])
        log.info("%s %r -> %s", "heard" if request.get("spoken") else "text", request["text"], json.dumps(proposal))
        return proposal
    return dict(ACTIONS[request["action"]])


MENU_FILE = "menu.xml"
REPORT_LOG_LINES = 4000          # the newest [Farmhand] lines of the game's log.txt


def desktop_dir() -> Path:
    if sys.platform == "win32":
        import ctypes
        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0:   # CSIDL_DESKTOPDIRECTORY
            return Path(buffer.value)
    return Path.home()


def save_report(profile: Path, link, target: Path | None = None, clock=time.strftime) -> Path:
    """Everything a live test needs, in one zip on the Desktop; nothing is sent."""
    import zipfile
    target = (target or desktop_dir()) / f"Farmhand-report-{clock('%Y%m%d-%H%M%S')}.zip"
    logs = profile.joinpath(*MOD_DIR)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as report:
        for log_file in sorted(logs.glob(LOG_FILE + "*")):
            report.write(log_file, log_file.name)
        game_log = profile / "log.txt"
        if game_log.is_file():
            lines = [l for l in game_log.read_text(encoding="utf-8", errors="replace").splitlines() if "[Farmhand]" in l
                     or "Error" in l or "Warning" in l]
            report.writestr("log-farmhand-lines.txt", "\n".join(lines[-REPORT_LOG_LINES:]))
        settings = data_dir() / "settings.json"
        if settings.is_file():
            report.write(settings, "settings.json")
        try:
            report.writestr("snapshot.json", json.dumps(link.fresh_snapshot(), indent=1))
        except StepUnverified:
            report.writestr("snapshot.json", "null")
        report.writestr("versions.txt", f"python {sys.version}\nplatform {sys.platform}\n")
    return target


def ask_model(link, model, request, generation: int) -> None:
    """On its own thread, so the serve loop keeps answering (STOP above all) while
    the model thinks; its answer returns through the request queue, tagged with
    the generation it was asked in (a STOP or a new session since voids it)."""
    proposal = model.interpret(request["text"])
    log.info("model %r -> %s", request["text"], json.dumps(proposal))
    link.inject({"proposal": proposal, "spoken": request.get("spoken", False), "generation": generation})


def linked_farm(link):
    """The farm of the game linked to this companion, from a fresh snapshot."""
    frame = link.fresh_snapshot()
    if frame["session"] != link.session:
        raise StepUnverified("NOT_LINKED")
    return str(frame["farm_id"])


def serve(world: FS25World, field_entrances=None, should_stop=lambda: False, interpreter=None,
          microphone=None, transcriber=None, speaker=None, ai=None, report=None) -> None:
    """Answer the player's requests until the link closes: one job at a time,
    STOP and STATUS at once (companion/coordinator.py). ``ai`` (companion/ai.py,
    optional) answers only words the rules neither understand nor refuse, off
    this thread, and owns the in-game AI menu.
    ``microphone`` and
    ``transcriber`` are factories for push-to-talk (companion/voice.py); without
    them, or when they raise Unavailable, voice is unavailable and text works."""
    link = world.link
    rules = interpreter or RuleInterpreter()
    replies = Replies(link.notice, speaker)
    coordinator = Coordinator(world, Executor(world, field_entrances=field_entrances), replies,
                              farm=lambda: linked_farm(link), session=lambda: link.session)
    voice = VoiceInput(microphone or _no_voice, transcriber or _no_voice,
                       on_text=lambda text: link.inject({"text": text, "spoken": True}),
                       notify=lambda code: replies(code, code != "LISTENING"),
                       places=lambda: [p.name for p in world.places()])
    menu = {"id": 0, "prompted": False}
    menu_lock = threading.Lock()
    generation = [0]                    # bumped by STOP and a new session

    def build_menu():
        try:
            with menu_lock:
                labels = [c.label for c in ai.open_menu()]
                menu["id"] += 1
                link.files.write(MENU_FILE, encode_menu(menu["id"], "Farmhand AI model", labels))
        except (OSError, ValueError):
            log.exception("menu not written")
            replies("AI_UNAVAILABLE")
            return
        replies("MENU")

    def show_menu():
        """The AI choices as a list in game (mod: GIANTS OptionDialog), built on a
        worker thread: asking Ollama for its models never delays STOP."""
        if ai is None or not hasattr(ai, "open_menu"):
            replies("AI_UNAVAILABLE")
            return
        threading.Thread(target=build_menu, daemon=True, name="farmhand-menu").start()

    if ai is not None and hasattr(ai, "show_menu"):
        ai.show_menu = show_menu
    foreign_since = None
    while not link.closed.is_set() and not should_stop():
        request = link.next_request(timeout=1.0)
        if request is not None:
            try:
                if "menu" in request:
                    with menu_lock:
                        current = request["menu"] == menu["id"]
                    if current and request["choice"] > 0 and hasattr(ai, "choose"):
                        ai.choose(request["choice"])
                    continue
                if "proposal" in request and request.get("generation") != generation[0]:
                    log.info("model answer dropped: a stop or new session came first")
                    continue
                action = request.get("action")
                if action == "settings":
                    show_menu()
                    continue
                if action == "talk_start":
                    voice.talk_start()
                    continue
                if action == "talk_end":
                    voice.talk_end()
                    continue
                spoken = bool(request.get("spoken"))
                proposal = proposal_for(request, rules)
                if proposal is not None and proposal.get("command") == "stop":
                    generation[0] += 1          # answers still being thought about are void
                if proposal is not None and proposal.get("command") == "settings":
                    show_menu()
                    continue
                if proposal is not None and proposal.get("command") == "report":
                    try:
                        log.info("report saved: %s", report() if report else None)
                        replies("REPORT_SAVED" if report else "REPORT_FAILED")
                    except OSError:
                        log.exception("report failed")
                        replies("REPORT_FAILED")
                    continue
                if (proposal is None and "text" in request and ai is not None and getattr(ai, "enabled", True)
                        and not refuses(request["text"])):
                    threading.Thread(target=ask_model, args=(link, ai, request, generation[0]), daemon=True,
                                     name="farmhand-model-ask").start()
                    continue
                coordinator.submit(proposal, spoken=spoken)
            except Exception:   # keep serving; a job's own failures end inside the coordinator
                log.exception("request %s failed", request.get("id"))
                replies("FAILED_INTERNAL_ERROR")
            continue
        session, last_frame = link.linked_session()
        now = link.clock()
        # The first time this companion is linked and no AI choice was ever made,
        # offer it once (the user may also open it any time).
        if session == link.session and not menu["prompted"] and ai is not None:
            menu["prompted"] = True
            if getattr(ai, "startup_problem", None):
                replies(ai.startup_problem)     # e.g. the saved model is gone: reported, not replaced
            if hasattr(ai, "settings") and ai.settings.ai() is None:
                show_menu()
        if (last_frame is not None and now - last_frame < 3 * SNAPSHOT_TIMEOUT_S and session != link.session
                and not coordinator.busy):
            foreign_since = now if foreign_since is None else foreign_since
            if now - foreign_since > RENEW_AFTER_S:
                link.renew()
                generation[0] += 1
                foreign_since = None
        else:
            foreign_since = None
    if coordinator.busy:                  # quitting during a job: stop it first
        coordinator.stop()
    coordinator.wait(timeout=30)


def configure_logging(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(folder / LOG_FILE, maxBytes=1_000_000, backupCount=2,
                                                     encoding="utf-8")]
    if sys.stderr is not None:                  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="optional farmhand.json")
    parser.add_argument("--profile", type=Path, help="FarmingSimulator2025 folder (default: Documents)")
    args = parser.parse_args(argv)
    profile = args.profile or game_profile_dir()
    if not profile.is_dir():
        raise SystemExit(f"FS25 profile folder not found: {profile}")
    configure_logging(profile.joinpath(*MOD_DIR))
    categories, entrances = load_config(args.config) if args.config else ({}, {})
    try:
        link = start_local_link(profile)
    except OSError:
        log.exception("companion not started")
        return 1
    log.info("companion started; session %s; waiting for FS25", link.session)
    # The optional AI the user chose: words the rules do not know; never STOP, never execution.
    ai = AI(Settings(), notify=link.notice)
    ai.start()
    # Setup asks a running companion to quit (companion/setup.py stop_running).
    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    (data / QUIT_FILE).unlink(missing_ok=True)
    (data / PID_FILE).write_text(str(os.getpid()), encoding="utf-8")
    try:
        serve(FS25World(link, categories), entrances, ai=ai, should_stop=(data / QUIT_FILE).exists,
              report=lambda: save_report(profile, link),
              microphone=SoundDeviceMicrophone, transcriber=WhisperTranscriber,
              speaker=SapiSpeaker() if sys.platform == "win32" else None)
    finally:
        ai.stop()
        link.close()
        (data / PID_FILE).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
