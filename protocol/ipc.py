"""Local IPC protocol v3 (Farmhand Companion pattern: PROTOCOL.md, Transport.lua).

Game -> companion: the FS25 Lua sandbox has no sockets, so the mod opens the
Windows named pipe the companion hosts with ``io.open(PIPE_PATH, "w")`` and
writes one JSON object per line (``hello``, ``snapshot``, ``result``, ``catalog``).
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
PROTOCOL_VERSION = 3
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
    "farm_drive": ("tractor", "x", "z", "area", "yaw"),   # area > 0: any clear approach point that near (x, z); yaw beyond +-2 pi: any heading
    "stage": ("tractor", "x", "z", "yaw"),
    "navigate": ("tractor", "x", "z", "yaw"),
    "fieldwork": ("tractor", "field", "x", "z", "yaw", "area", "seed", "hand"),   # seed: in the seeder's crops (0 as set); hand 1: ends WORKING
    "buy": ("item", "hitch", "engine", "opt1", "opt2", "opt3", "opt4"),
    "save": (),
    "block": ("x", "z", "area"),
    "open_gates": ("x", "z", "area"),
    "maneuver": ("tractor", "wagon", "tractorJoint", "wagonJoint"),
    "align": ("tractor", "wagon", "tractorJoint", "wagonJoint", "maxDistanceM"),
    "attach": ("tractor", "wagon", "tractorJoint", "wagonJoint"),
    "detach": ("tractor", "wagon"),
    "pull_clear": ("tractor", "wagon"),
    "unpark": ("tractor",),
    "teleport": ("tractor", "area", "x", "z", "yaw"),   # teleport recovery near (x, z) (the rig itself when x is beyond 50000), no nearer than area m, the search 50 m beyond it; yaw beyond +-2 pi: the rig's own
    "place": ("tractor", "x", "z", "yaw"),         # a loose implement set down at exactly (x, z, yaw) when that pose is clear, else refused (never slid)
    "reposition": ("tractor", "area"),           # a vehicle to open ground nearby (a standing combine: lanes beside and behind it; area > 0: a clear lane that long behind the rig, at the heading it was found clear)
    "pay": ("direction", "area"),                 # Farmhand's pay: mode (0 none, 1 per hour, 2 per job, 3 per day, 4 the game's own wages), amount (0: GIANTS' rate)
    "charge": ("area",),                          # per-job pay: this amount for a completed request
    "fold": ("tractor", "direction"),            # the vehicle's own Foldable parts to one end
    "worktool": ("tractor", "direction"),        # the plow lowered with field creation on (1), raised and limited again (-1)
    "belts": ("tractor", "direction"),           # a transport's TensionBelts fastened (1) or released (-1)
    "load": ("tractor", "wagon", "direction"),   # the tractor alone onto the trailer's deck (1) or off it (-1)
    "follow": ("tractor",),                      # FollowMe: the vehicle follows the one the player drives
    "contract": ("field", "direction"),          # the field's contract taken (1) or its reward collected (-1)
    "land": ("field", "direction"),              # the field's farmland parcel bought (1) or sold (-1), GIANTS' FarmlandStateEvent
    "pipe": ("tractor", "direction"),            # a combine's pipe out (1) or in (-1), GIANTS Pipe
    "overload": ("tractor", "wagon"),            # a standing combine discharges into the trailer under its pipe
    "alongside": ("tractor", "wagon", "direction"),  # the trailer under the combine's pipe (1) or on past the combine (-1)
    "mount": ("tractor", "wagon"),               # the machine's raised front implement carried onto the carrier's GIANTS lock position and set down there
    "reattach": ("tractor", "wagon"),            # the loose implement it stands at attached by GIANTS' own range rule
    "reach": ("tractor", "x", "z", "yaw", "area"),    # area > 0: a diagnostic probe narrower than the rig (never driven)       # whether GIANTS' agent plans a route there for the vehicle (never drives)
    "escort": ("tractor", "wagon"),              # the trailer held under the pipe of the combine (wagon) harvesting under GIANTS' helper
    "deliver": ("tractor", "direction"),        # GIANTS' Deliver helper: the cargo to the best paid selling station (1) or the farm's storage (-1)
    "backoff": ("tractor", "wagon"),             # the tractor straight back from the transport's ramps it waits behind
    "stop": ("tractor",),
}
_ATTRIBUTE = {"tractor": "tractor", "wagon": "wagon", "x": "x", "z": "z",
              "yaw": "yaw", "tractorJoint": "tj", "wagonJoint": "wj", "maxDistanceM": "dist", "field": "field", "area": "area", "seed": "seed", "hand": "hand", "item": "item", "hitch": "hitch",
              "engine": "eng", "opt1": "o1", "opt2": "o2", "opt3": "o3", "opt4": "o4",
              "direction": "fd"}
_INTEGER = {"tractor", "wagon", "tractorJoint", "wagonJoint", "field", "seed", "hand", "item", "hitch", "engine",
            "opt1", "opt2", "opt3", "opt4"}
# The motions that may pass through scenery beside the rig, and only with the player's opt-in
# setting (companion/ai.py Settings.scenery_ghosting, default off): an optional ghost=1. Absent is
# off, so no older or other sender can turn it on by leaving a field out (AGENTS.md 10).
GHOST_OPS = frozenset({"farm_drive", "reposition", "maneuver", "align"})
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_COMMAND_ID = 2147483647


def encode_command(session: str, command_id: int, op: str, args: dict) -> str:
    """One command file's text. Raises ValueError on anything outside the contract."""
    if not isinstance(session, str) or not _TOKEN.fullmatch(session):
        raise ValueError("INVALID_SESSION")
    if type(command_id) is not int or not 1 <= command_id <= MAX_COMMAND_ID:
        raise ValueError("INVALID_COMMAND_ID")
    via = args.get("via") if op == "farm_drive" else None
    ghost = args.get("ghost") if op in GHOST_OPS else None
    expected = set(COMMAND_ARGS.get(op, ()))
    if via is not None:
        expected.add("via")
    if ghost is not None:
        expected.add("ghost")
    if op not in COMMAND_ARGS or set(args) != expected:
        raise ValueError("INVALID_COMMAND")
    if ghost is not None and (type(ghost) is not int or ghost not in (0, 1)):
        raise ValueError("INVALID_ARGUMENT")
    if via is not None and (not isinstance(via, str) or len(via) > 2048 or
                            not re.fullmatch(r"-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?(?:;-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?)*", via) or
                            len(via.split(";")) > 128):
        raise ValueError("INVALID_ARGUMENT")
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
    if via is not None:
        parts.append(f'via="{via}"')
    if ghost is not None:
        parts.append(f'gh="{ghost}"')
    return '<?xml version="1.0" encoding="utf-8" standalone="no" ?>\n<command ' + " ".join(parts) + " />\n"


MAX_NOTICES = 8
MAX_MENU = 9


def _attribute(text: str, limit: int) -> str:
    """Printable text with nothing XML would need to escape, so the game reads it
    back exactly whether or not its parser decodes entities."""
    text = "".join(c for c in str(text) if c.isprintable())
    return text.translate(str.maketrans({'"': "'", "&": "+", "<": "(", ">": ")"}))[:limit]


def encode_menu(menu_id: int, title: str, options) -> str:
    """menu.xml: a list for the player to pick from in game (GIANTS OptionDialog).
    One element, numbered attributes (o1..o9), each line at most 60 characters."""
    options = list(options)
    if type(menu_id) is not int or not 1 <= menu_id <= MAX_COMMAND_ID or not 1 <= len(options) <= MAX_MENU:
        raise ValueError("INVALID_MENU")
    lines = " ".join(f'o{n}="{_attribute(o, 60)}"' for n, o in enumerate(options, start=1))
    return (f'<?xml version="1.0" encoding="utf-8" standalone="no" ?>\n'
            f'<menu id="{menu_id}" title="{_attribute(title, 60)}" count="{len(options)}" {lines} />\n')


def encode_session(session: str, beat: int, notices=()) -> str:
    """The session file: names the companion; ``beat`` changes while it lives.
    ``notices``: the latest (id, token) pairs, oldest first, ids increasing; the
    game polls once a second and shows each id it has not shown, in order, so a
    quick STOPPING then CANCELLED are both seen."""
    if not isinstance(session, str) or not _TOKEN.fullmatch(session) or type(beat) is not int or beat < 0:
        raise ValueError("INVALID_SESSION")
    notices = list(notices)
    if len(notices) > MAX_NOTICES or any(
            type(i) is not int or not 1 <= i <= MAX_COMMAND_ID or not isinstance(c, str) or not _TOKEN.fullmatch(c)
            for i, c in notices) or any(a[0] >= b[0] for a, b in zip(notices, notices[1:])):
        raise ValueError("INVALID_NOTICE")
    extra = f' notices="{" ".join(f"{i}:{c}" for i, c in notices)}"' if notices else ""
    return (f'<?xml version="1.0" encoding="utf-8" standalone="no" ?>\n'
            f'<session id="{session}" beat="{beat}"{extra} />\n')
