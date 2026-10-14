"""Local IPC frame protocol v1: game mod -> companion, newline-delimited JSON.

Transport (see adapters/EVIDENCE.md, Farmhand Companion PROTOCOL.md/Transport.lua):
the FS25 Lua sandbox has no sockets, so the mod opens a Windows named pipe that
the companion hosts, with ``io.open(PIPE_PATH, "w")``, and writes one JSON
object per line. Hosting the pipe is Windows-only and is not implemented here;
this module is the transport-independent, untrusted-input decoder.
"""
import json
from pathlib import Path

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
