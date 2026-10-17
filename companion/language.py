"""Turn the player's words into a command proposal (protocol/command.schema.json).

The interpreters here only classify and extract; they never choose equipment,
positions or control values, and their output is validated by the coordinator.

``RuleInterpreter`` is deterministic and always present: it is the structured
path that works without any model, and STOP never goes past it. A local model
(companion/model.py) may be chained behind it for wording the rules do not know.
"""
from __future__ import annotations

import re
from typing import Protocol

# Leading words that address Farmhand rather than say anything.
_ADDRESS = re.compile(r"^(?:(?:hey|ok|okay|hi|yo)\s+)?(?:farm\s*hand|farmhand)\b\s*|^(?:please|pls|could you|can you|would you)\s+")
_STOP_WORDS = ("stop", "halt", "cancel", "abort", "freeze")
_STOP_PHRASES = ("hold on", "never mind", "nevermind", "stand down", "forget it")
_STATUS = re.compile(r"\b(?:what are you doing|what s happening|whats happening|what are you up to|status|where are you|"
                     r"how s it going|hows it going|how is it going|progress|report|are you done|are you finished)\b")
_FETCH_VERB = re.compile(r"\b(?:bring|get|fetch|grab|send|need|want|give|hitch|hook)\b")
_TRACTOR = re.compile(r"\btractors?\b")
_WAGON = re.compile(r"\b(?:forage|loader|silage)?\s*wagons?\b|\bforage\b")
_FIELD = re.compile(r"\bto\s+field\s+(?:number\s+)?(\d{1,4})\b")
_MARKER = re.compile(r"\bto\s+(?:the\s+)?(.+?)\s*$")
_HERE = re.compile(r"^(?:me|here|my place|my position|where i am|where i m standing|right here|over here)$")


class Interpreter(Protocol):
    def interpret(self, text: str) -> dict | None:
        """A command proposal, or None when the words are not understood."""
        ...


def normalize(text: str) -> str:
    words = re.sub(r"[^0-9a-z]+", " ", text.casefold()).strip()
    previous = None
    while previous != words:                      # "hey farmhand please stop" -> "stop"
        previous, words = words, _ADDRESS.sub("", words).strip()
    return words


def is_stop(words: str) -> bool:
    """STOP: a stop word first, or a short utterance with one. A longer sentence
    that only mentions one (a marker called "bus stop") is not a stop."""
    tokens = words.split()
    if not tokens:
        return False
    if tokens[0] in _STOP_WORDS or any(words.startswith(p) for p in _STOP_PHRASES):
        return True
    return len(tokens) <= 3 and any(t in _STOP_WORDS for t in tokens)


class RuleInterpreter:
    """Deterministic wording rules for the supported commands."""

    def interpret(self, text: str) -> dict | None:
        if not isinstance(text, str):
            return None
        words = normalize(text)
        if is_stop(words):
            return {"version": 1, "command": "stop"}
        if _STATUS.search(words):
            return {"version": 1, "command": "status"}
        if _TRACTOR.search(words) and _WAGON.search(words) and (_FETCH_VERB.search(words) or len(words.split()) <= 6):
            return {"version": 1, "command": "fetch_pair", **self._target(text, words)}
        return None

    @staticmethod
    def _target(text: str, words: str) -> dict:
        field = _FIELD.search(words)
        if field:
            return {"target": {"type": "field_entrance", "field": int(field.group(1))}}
        # Everything after the last "to" names the destination unless it means here.
        tail = words.rsplit(" wagon", 1)[-1] if " wagon" in words else words
        marker = _MARKER.search(tail)
        if marker is None:
            return {}
        name = re.sub(r"\s+please$", "", marker.group(1)).strip()
        if not name or _HERE.match(name):
            return {}
        # Keep the player's own spelling for the marker (matched exactly after
        # whitespace/case normalization by the executor).
        original = re.search(r"\bto\s+(?:the\s+)?(.+?)[\s.!?]*$", text, re.IGNORECASE)
        spelled = re.sub(r"\s+please$", "", original.group(1), flags=re.IGNORECASE).strip() if original else name
        return {"target": {"type": "autodrive_marker", "marker_name": spelled[:128]}}


class ChainInterpreter:
    """Rules first; a model only for wording the rules do not understand. STOP is
    always decided by the rules, never delayed by a model."""

    def __init__(self, *interpreters: Interpreter):
        self.interpreters = interpreters

    def interpret(self, text: str) -> dict | None:
        for interpreter in self.interpreters:
            try:
                proposal = interpreter.interpret(text)
            except Exception:                     # a failing model must not break text input
                proposal = None
            if proposal is not None:
                return proposal
        return None
