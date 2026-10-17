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
_NEW_PAIR = re.compile(r"\b(?:a|an|any|another|available|free|spare)\s+(?:\w+\s+)?(?:tractor|forage|loader|silage|wagon)")
_FETCH_ONLY = re.compile(r"\b(?:grab|fetch|get me|i need|i want)\b")
_FETCH_VERB = re.compile(r"\b(?:bring|get|fetch|grab|send|need|want|give|hitch|hook)\b")
_TRACTOR = re.compile(r"\btractors?\b")
_WAGON = re.compile(r"\b(?:forage|loader|silage)?\s*wagons?\b|\bforage\b")
_FIELD = re.compile(r"\bto\s+field\s+(?:number\s+)?(\d{1,4})\b")
_HERE = re.compile(r"^(?:me|here|my place|my position|where i am|where i m standing|right here|over here|back here)$")
# "this"/"it": the hitched pair the player means (the executor picks the one nearest them).
_REFERENT = re.compile(r"\b(?:it|this|that|them|these|those|everything)\b")
_MOVE_VERB = re.compile(r"\b(?:bring|take|drive|move|park|send|return|put)\b")
_COME = re.compile(r"^(?:come|come here|come to me|come over here|come back|come back here|get over here)$")
# The destination: whatever follows the last "to"/"at"/"into" (and an optional "the"/"my").
_DESTINATION = re.compile(r"\b(?:to|at|into|in)\s+(?:the\s+|my\s+|our\s+)?([^,.!?]+?)[\s,.!?]*$", re.IGNORECASE)


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
        if _COME.match(words):
            return {"version": 1, "command": "move_pair"}
        back = re.search(r"\bback\b", words)
        referent = _REFERENT.search(words) or back
        # Naming a tractor and a wagon asks for a pair, even with "hook them together",
        # when it is "a"/"an"/"any"/"available" one or a fetch-only verb is used; "the
        # tractor and wagon ... back" or "this" is the pair already out there.
        new_pair = _NEW_PAIR.search(words) or _FETCH_ONLY.search(words)
        if _TRACTOR.search(words) and _WAGON.search(words) and not back and (new_pair or not referent) and (
                _FETCH_VERB.search(words) or len(words.split()) <= 6):
            return {"version": 1, "command": "fetch_pair", **self._target(text, words)}
        if referent and _MOVE_VERB.search(words):
            target = self._target(text, words)
            if target or re.search(r"\b(?:here|to me|back)\b", words):
                return {"version": 1, "command": "move_pair", **target}
        return None

    @staticmethod
    def _target(text: str, words: str) -> dict:
        """A field number, or a place in the player's own spelling; nothing for
        "here"/"me". The executor matches places exactly (markers) or by all
        their words (the farm's buildings), and never guesses."""
        field = _FIELD.search(words)
        if field:
            return {"target": {"type": "field_entrance", "field": int(field.group(1))}}
        spoken = re.sub(r"\s+please[\s.!?]*$", "", text.strip(), flags=re.IGNORECASE)
        destination = _DESTINATION.search(spoken)
        if destination is None:
            return {}
        name = destination.group(1).strip()
        if not name or _HERE.match(normalize(name)) or _REFERENT.fullmatch(normalize(name)):
            return {}
        return {"target": {"type": "place", "name": name[:64]}}


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
