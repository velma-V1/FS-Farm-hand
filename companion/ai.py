"""The optional AI model: discovery, the user's choice, validation, persistence.

Farmhand never picks a model for the user. The player opens the AI menu in game
(the settings key or "choose AI model"; also offered once at first connection),
sees the models already on this PC (their own Ollama, and any model Farmhand
installed), the two Farmhand downloads when they are not there yet, and
"Continue without AI". The chosen model gets a short bounded validation; only a
model that passes is saved, and a failing one is reported and the menu is shown
again. A saved model that later disappears or stops answering is reported, never
replaced: words outside the deterministic rules are then simply not understood.

Ollama runs the model with its own normal CPU/GPU behaviour; Farmhand only sends
schema-constrained requests to 127.0.0.1. The managed path is companion/model.py.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

from companion import model as managed

log = logging.getLogger("farmhand")

LOCAL = managed.LOCAL
OLLAMA_URL = "http://127.0.0.1:11434"
VALIDATION_TIMEOUT_S = 60.0          # the whole validation, model load included
# What Farmhand needs from a model: valid structured answers, the right command for
# plain wording, and no movement for a sentence that is not a supported command.
VALIDATION = [
    ("get the forage wagon hooked up and over to me", "fetch_pair"),
    ("drag that combo back to the barn", "move_pair"),
    ("how far along are you", "status"),
    ("kill the engine and stay put", "stop"),
    ("plow field 3", "unknown"),
    ("could you rig up a tractor with the forage wagon for me", "fetch_pair"),
]
NEEDED_CORRECT = 5                   # of 6; "plow field 3" must never become a movement


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "FS25Farmhand"


# ---------------------------------------------------------------- settings

class Settings:
    """settings.json beside the managed models: {"ai": {"provider", "model"}} or
    {"ai": {"provider": "none"}}; no entry means the user has not chosen yet."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "settings.json"

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)

    def ai(self) -> dict | None:
        choice = self.load().get("ai")
        if isinstance(choice, dict) and choice.get("provider") in ("ollama", "farmhand", "none"):
            return choice
        return None

    def set_ai(self, choice: dict) -> None:
        data = self.load()
        data["ai"] = choice
        self.save(data)


# --------------------------------------------------------------- providers

@dataclass(frozen=True)
class Choice:
    provider: str            # "ollama", "farmhand", "install", "none"
    model: str               # Ollama model name, or a managed key
    label: str               # one menu line, at most 60 characters


def _gb(size) -> str:
    return f"{size / 1e9:.1f} GB" if isinstance(size, (int, float)) and size > 0 else "?"


class Ollama:
    """The user's own Ollama on 127.0.0.1 (OLLAMA_HOST if set to a local address)."""

    def __init__(self, url: str | None = None, opener=LOCAL, timeout: float = managed.TIMEOUT_S):
        host = os.environ.get("OLLAMA_HOST", "")
        local = host and host.split("://")[-1].split(":")[0] in ("127.0.0.1", "localhost")
        self.url = url or (("http://" + host.split("://")[-1]) if local else OLLAMA_URL)
        self.opener, self.timeout = opener, timeout

    def models(self) -> list[dict]:
        """Installed models (GET /api/tags), or [] when Ollama is not running."""
        try:
            with self.opener(self.url + "/api/tags", timeout=3) as response:
                listed = json.loads(response.read()).get("models", [])
        except (OSError, ValueError, AttributeError):
            return []
        return [m for m in listed if isinstance(m, dict) and isinstance(m.get("name"), str)]

    def interpreter(self, name: str) -> "OllamaInterpreter":
        return OllamaInterpreter(self, name)


class OllamaInterpreter:
    """Schema-constrained /api/chat; None whenever it is not sure, down or slow."""

    def __init__(self, ollama: Ollama, name: str):
        self.ollama, self.name = ollama, name
        self.missing = False                       # Ollama says the model is gone

    def _chat(self, text: str, think: bool):
        body = {"model": self.name, "stream": False, "format": managed.MODEL_SCHEMA,
                "options": {"temperature": 0},
                "messages": [{"role": "system", "content": managed.PROMPT},
                             {"role": "user", "content": text[:200]}]}
        if not think:
            body["think"] = False
        request = urllib.request.Request(self.ollama.url + "/api/chat", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        with self.ollama.opener(request, timeout=self.ollama.timeout) as response:
            return json.loads(response.read())

    def interpret(self, text: str) -> dict | None:
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            try:
                reply = self._chat(text, think=False)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    self.missing = True
                    raise
                if error.code != 400:
                    raise
                reply = self._chat(text, think=True)    # older Ollama: no "think" field
            answer = json.loads(reply["message"]["content"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            log.info("ollama %s: no answer (%s)", self.name, type(error).__name__)
            return None
        return managed.to_proposal(answer)


def _speech_installed(folder: Path | None) -> bool:
    from companion import voice
    target = (folder / "whisper-base.en") if folder else voice.whisper_dir()
    return all((target / name).is_file() for name in voice.WHISPER_FILES)


def discover(ollama: Ollama | None = None, folder: Path | None = None) -> list[Choice]:
    """Every model the user could pick, then the two downloads not yet installed,
    then no AI; and the speech model for push-to-talk while it is missing. Labels
    show only what is cheap and reliable to know."""
    choices = []
    for m in (ollama or Ollama()).models():
        details = m.get("details") if isinstance(m.get("details"), dict) else {}
        extra = " ".join(str(details[k]) for k in ("parameter_size", "quantization_level") if details.get(k))
        choices.append(Choice("ollama", m["name"], f"{m['name']} (Ollama, {_gb(m.get('size'))} {extra})"[:60]))
    installed = managed.installed_choices(folder)
    for key in installed:
        item = managed.MANAGED[key]
        choices.append(Choice("farmhand", key, f"{item['title']} (Farmhand, {_gb(item['size'])})"[:60]))
    for key, item in managed.MANAGED.items():
        if key not in installed:
            choices.append(Choice("install", key, f"Install {item['title']}: {_gb(item['size'])}, "
                                                  f"~{item['ram_gb']} GB RAM"[:60]))
    choices.append(Choice("none", "", "Continue without AI"))
    if not _speech_installed(folder):
        choices.append(Choice("voice", "", "Install speech recognition for push-to-talk: 0.15 GB"))
    return choices


# -------------------------------------------------------------- validation

def validate(interpreter, budget: float = VALIDATION_TIMEOUT_S, clock=time.monotonic) -> tuple[bool, str]:
    """A short bounded check of exactly what Farmhand needs from the model."""
    started, correct = clock(), 0
    for text, expected in VALIDATION:
        if clock() - started > budget:
            return False, "TOO_SLOW"
        proposal = interpreter.interpret(text)
        got = proposal["command"] if proposal else "unknown"
        if expected == "unknown" and got in ("fetch_pair", "move_pair"):
            return False, "UNSAFE_ANSWER"
        correct += got == expected
        if getattr(interpreter, "missing", False):
            return False, "MODEL_MISSING"
    if clock() - started > budget:
        return False, "TOO_SLOW"
    if correct < NEEDED_CORRECT:
        return False, f"ONLY_{correct}_OF_{len(VALIDATION)}"
    return True, f"{correct}_OF_{len(VALIDATION)}"


# ---------------------------------------------------------------- the AI

class AI:
    """The interpreter behind the rules for the user's saved choice, or none.

    ``interpret`` never raises and never blocks STOP (the service calls it off
    its request loop). ``notify`` shows a token in game."""

    def __init__(self, settings: Settings, notify, show_menu=lambda: None, ollama: Ollama | None = None,
                 folder: Path | None = None, server_factory=managed.ModelServer, installer=managed.install,
                 speech_installer=None):
        self.settings, self.notify, self.show_menu = settings, notify, show_menu
        self.speech_installer = speech_installer
        self.ollama, self.folder = ollama or Ollama(), folder
        self.server_factory, self.installer = server_factory, installer
        self._lock = threading.Lock()
        self.server = None
        self.interpreter = None
        self.busy = False                          # validating or installing
        self.menu: list[Choice] = []

    # -- current interpreter
    def start(self) -> None:
        """Bring up the saved choice, if any (at companion start)."""
        choice = self.settings.ai()
        if choice and choice["provider"] != "none":
            self._activate(choice)

    def _activate(self, choice: dict):
        self.stop()
        if choice["provider"] == "ollama":
            interpreter = self.ollama.interpreter(choice["model"])
        else:
            server = self.server_factory(self.folder, choice=choice["model"])
            if not server.start():
                self.notify("AI_MODEL_MISSING")
                return None
            self.server = server
            interpreter = managed.ModelInterpreter(ready=server.ready)
        with self._lock:
            self.interpreter = interpreter
        return interpreter

    def stop(self) -> None:
        with self._lock:
            server, self.server, self.interpreter = self.server, None, None
        if server is not None:
            server.stop()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self.interpreter is not None

    def interpret(self, text: str) -> dict | None:
        with self._lock:
            interpreter = self.interpreter
        if interpreter is None:
            return None
        try:
            proposal = interpreter.interpret(text)
        except Exception:
            log.exception("model failed")
            return None
        if getattr(interpreter, "missing", False):
            interpreter.missing = False
            self.notify("AI_MODEL_MISSING")       # reported, never replaced
        return proposal

    # -- the menu
    def open_menu(self) -> list[Choice]:
        self.menu = discover(self.ollama, self.folder)
        return self.menu

    def choose(self, index: int) -> None:
        """The player picked menu line ``index`` (1-based); runs in the background."""
        if self.busy or not 1 <= index <= len(self.menu):
            return
        choice = self.menu[index - 1]
        if choice.provider == "none":
            self.stop()
            self.settings.set_ai({"provider": "none"})
            self.notify("AI_OFF")
            return
        self.busy = True
        threading.Thread(target=self._try, args=(choice,), daemon=True, name="farmhand-ai-choice").start()

    def _try(self, choice: Choice) -> None:
        try:
            if choice.provider == "voice":
                self.notify("VOICE_INSTALLING")
                try:
                    from companion import voice
                    (self.speech_installer or voice.install)((self.folder / "whisper-base.en") if self.folder else None)
                except Exception:
                    log.exception("speech model install failed")
                    self.notify("VOICE_INSTALL_FAILED")
                    return
                self.notify("VOICE_READY")
                return
            if choice.provider == "install":
                self.notify("AI_INSTALLING")
                try:
                    self.installer(self.folder, choice=choice.model)
                except Exception:
                    log.exception("model install failed")
                    self.notify("AI_INSTALL_FAILED")
                    return
                choice = Choice("farmhand", choice.model, choice.label)
            self.notify("AI_VALIDATING")
            wanted = {"provider": choice.provider, "model": choice.model}
            previous = self.settings.ai()
            interpreter = self._activate(wanted)
            ok, detail = self._validate(interpreter)
            log.info("AI validation %s: %s %s", wanted, ok, detail)
            if not ok:
                self.stop()
                if previous and previous.get("provider") != "none" and previous != wanted:
                    self._activate(previous)       # the earlier working choice stays
                self.notify("AI_INCOMPATIBLE")
                self.show_menu()                   # back to the selection
                return
            self.settings.set_ai(wanted)
            self.notify("AI_READY")
        finally:
            self.busy = False

    def _validate(self, interpreter) -> tuple[bool, str]:
        if interpreter is None:
            return False, "NOT_RUNNING"
        server = self.server
        if server is not None:                     # managed: wait for the model to load
            deadline = time.monotonic() + managed.START_TIMEOUT_S
            while not server.ready() and time.monotonic() < deadline and server.process.poll() is None:
                time.sleep(0.5)
            if not server.ready():
                return False, "NOT_RUNNING"
        return validate(interpreter)
