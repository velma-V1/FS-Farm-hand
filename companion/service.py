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


def ask_model(link, model, request) -> None:
    """On its own thread, so the serve loop keeps answering (STOP above all) while
    the model thinks; its answer returns through the request queue."""
    proposal = model.interpret(request["text"])
    log.info("model %r -> %s", request["text"], json.dumps(proposal))
    link.inject({"proposal": proposal, "spoken": request.get("spoken", False)})


def linked_farm(link):
    """The farm of the game linked to this companion, from a fresh snapshot."""
    frame = link.fresh_snapshot()
    if frame["session"] != link.session:
        raise StepUnverified("NOT_LINKED")
    return str(frame["farm_id"])


def serve(world: FS25World, field_entrances=None, should_stop=lambda: False, interpreter=None,
          microphone=None, transcriber=None, speaker=None, ai=None) -> None:
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

    def show_menu():
        """The AI choices as a list in game (mod: GIANTS OptionDialog)."""
        if ai is None or not hasattr(ai, "open_menu"):
            replies("AI_UNAVAILABLE")
            return
        labels = [c.label for c in ai.open_menu()]
        menu["id"] += 1
        try:
            link.files.write(MENU_FILE, encode_menu(menu["id"], "Farmhand AI model", labels))
        except OSError:
            log.exception("menu not written")
            return
        replies("MENU")

    if ai is not None and hasattr(ai, "show_menu"):
        ai.show_menu = show_menu
    foreign_since = None
    while not link.closed.is_set() and not should_stop():
        request = link.next_request(timeout=1.0)
        if request is not None:
            try:
                if "menu" in request:
                    if request["menu"] == menu["id"] and request["choice"] > 0 and hasattr(ai, "choose"):
                        ai.choose(request["choice"])
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
                if proposal is not None and proposal.get("command") == "settings":
                    show_menu()
                    continue
                if (proposal is None and "text" in request and ai is not None and getattr(ai, "enabled", True)
                        and not refuses(request["text"])):
                    threading.Thread(target=ask_model, args=(link, ai, request), daemon=True,
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
        if (session == link.session and not menu["prompted"] and ai is not None
                and hasattr(ai, "settings") and ai.settings.ai() is None):
            menu["prompted"] = True
            show_menu()
        if (last_frame is not None and now - last_frame < 3 * SNAPSHOT_TIMEOUT_S and session != link.session
                and not coordinator.busy):
            foreign_since = now if foreign_since is None else foreign_since
            if now - foreign_since > RENEW_AFTER_S:
                link.renew()
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
              microphone=SoundDeviceMicrophone, transcriber=WhisperTranscriber,
              speaker=SapiSpeaker() if sys.platform == "win32" else None)
    finally:
        ai.stop()
        link.close()
        (data / PID_FILE).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
