"""Local IPC protocol v1 (Farmhand Companion pattern: PROTOCOL.md, Transport.lua).

Game -> companion: the FS25 Lua sandbox has no sockets, so the mod opens the
Windows named pipe the companion hosts with ``io.open(PIPE_PATH, "w")`` and
writes one JSON object per line (``hello``, ``snapshot``, ``result``).
Companion -> game: the sandbox reads files only through the engine XML API, so
one command at a time is an XML file renamed into place (``encode_command``).
This module is the transport-independent part: untrusted-frame decoding and
strict command encoding. The I/O lives in companion/bridge.py.
"""
import json
import math
from pathlib import Path
import re

from jsonschema import Draft202012Validator

# Distinct from Farmhand Companion's \\.\pipe\flowkits_fs25: Windows allows one
# server per pipe name, so sharing it would collide with that plugin.
PIPE_PATH = r"\\.\pipe\fs_farmhand_ac001"
PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 1 << 20

SCHEMA = json.loads((Path(__file__).resolve().parent / "frame.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


class FrameError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _reject_constant(token):
    raise FrameError("NON_FINITE_NUMBER")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FrameError("DUPLICATE_KEY")
        result[key] = value
    return result


def parse_frame(raw: bytes) -> dict:
    """Decode and validate one line. Raises FrameError; never returns partial data."""
    try:
        text = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise FrameError("MALFORMED") from None
    try:
        frame = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_keys)
    except FrameError:
        raise
    except ValueError:
        raise FrameError("MALFORMED") from None
    except RecursionError:
        # The json decoder recurses per nesting level, so a deep line far below
        # MAX_LINE_BYTES exhausts the interpreter's budget. Only this call is
        # guarded; the recursion limit itself is left unchanged.
        raise FrameError("TOO_DEEP") from None
    if not VALIDATOR.is_valid(frame):
        raise FrameError("SCHEMA_VIOLATION")
    return frame


class FrameReader:
    """Reassemble frames from arbitrary byte chunks with a bounded line buffer.

    ``feed`` returns parsed frames and FrameError instances in stream order, so a
    bad line is reported and skipped without corrupting the following frames.
    """

    def __init__(self, max_line_bytes: int = MAX_LINE_BYTES):
        self.max_line_bytes = max_line_bytes
        self._buffer = bytearray()
        self._discarding = False

    def feed(self, chunk: bytes) -> list:
        results = []
        self._buffer += chunk
        while True:
            end = self._buffer.find(b"\n")
            if end < 0:
                if len(self._buffer) > self.max_line_bytes:
                    if not self._discarding:
                        results.append(FrameError("LINE_TOO_LONG"))
                    self._discarding = True
                    self._buffer.clear()
                return results
            line = bytes(self._buffer[:end])
            del self._buffer[:end + 1]
            if self._discarding:
                self._discarding = False
                continue
            if len(line) > self.max_line_bytes:
                results.append(FrameError("LINE_TOO_LONG"))
                continue
            try:
                results.append(parse_frame(line))
            except FrameError as error:
                results.append(error)


# ------------------------------------------------------------------ commands
# The only operations mod/FarmhandBridge.lua executes, with their required
# arguments; nothing else can be encoded. XML attribute names match the mod.
COMMAND_ARGS = {
    "drive": ("tractor", "marker"),
    "stage": ("tractor", "x", "z", "yaw"),
    "align": ("tractor", "wagon", "tractorJoint", "wagonJoint", "maxDistanceM"),
    "attach": ("tractor", "wagon", "tractorJoint", "wagonJoint"),
    "stop": ("tractor",),
}
_ATTRIBUTE = {"tractor": "tractor", "wagon": "wagon", "marker": "marker", "x": "x", "z": "z",
              "yaw": "yaw", "tractorJoint": "tj", "wagonJoint": "wj", "maxDistanceM": "dist"}
_INTEGER = {"tractor", "wagon", "marker", "tractorJoint", "wagonJoint"}
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_COMMAND_ID = 2147483647


def encode_command(session: str, command_id: int, op: str, args: dict) -> str:
    """One command file's text. Raises ValueError on anything outside the contract."""
    if not isinstance(session, str) or not _TOKEN.fullmatch(session):
        raise ValueError("INVALID_SESSION")
    if type(command_id) is not int or not 1 <= command_id <= MAX_COMMAND_ID:
        raise ValueError("INVALID_COMMAND_ID")
    if op not in COMMAND_ARGS or set(args) != set(COMMAND_ARGS[op]):
        raise ValueError("INVALID_COMMAND")
    parts = [f'session="{session}"', f'id="{command_id}"', f'op="{op}"']
    for name in COMMAND_ARGS[op]:
        value = args[name]
        if name in _INTEGER:
            if type(value) is not int or not 0 <= value <= MAX_COMMAND_ID:
                raise ValueError("INVALID_ARGUMENT")
            text = str(value)
        else:
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 100000:
                raise ValueError("INVALID_ARGUMENT")
            text = repr(float(value))
        parts.append(f'{_ATTRIBUTE[name]}="{text}"')
    return '<?xml version="1.0" encoding="utf-8" standalone="no" ?>\n<command ' + " ".join(parts) + " />\n"


def encode_session(session: str, beat: int) -> str:
    """The session file: names the companion; ``beat`` changes while it lives."""
    if not isinstance(session, str) or not _TOKEN.fullmatch(session) or type(beat) is not int or beat < 0:
        raise ValueError("INVALID_SESSION")
    return f'<?xml version="1.0" encoding="utf-8" standalone="no" ?>\n<session id="{session}" beat="{beat}" />\n'
