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
_STOP_WORDS = ("stop", "halt", "cancel", "abort", "freeze", "wait", "whoa", "woah")
_STOP_PHRASES = ("hold on", "hold it", "never mind", "nevermind", "stand down", "forget it", "stay there",
                 "stay put", "stand still", "no stop")
# A request that ends in a stop ("bring it back here and stop") is a stop.
_STOP_TAIL = re.compile(r"\b(?:and|then|now)\s+(?:stop|halt|cancel)$")
# Words that make a sentence not an order: never move on a negation or a question.
_NEGATION = re.compile(r"\b(?:don t|dont|do not|does not|doesn t|not|never|no|didn t|didnt|won t|wont)\b")
_QUESTION = re.compile(r"^(?:where|why|did|is|are|was|were|has|have|had|who|when|which|what|whose|does|do)\b")
# Too general to name one place; the sentence is not understood instead.
# ("farm" is a name the executor resolves exactly or blocks: a marker or one building.)
_GENERIC = frozenset({"field", "the field", "there", "over there", "that", "it", "this",
                      "place", "somewhere", "building", "a building", "anywhere"})
# Trailing words about time or manner, not the place ("to the barn in a minute").
_TRAILING = re.compile(r"\s+(?:please|now|right now|right away|asap|quickly|soon|again|thanks|thank you|"
                       r"in a (?:minute|moment|bit|sec|second)|when you can)$", re.IGNORECASE)
_SETTINGS = re.compile(r"^(?:(?:open|show|change|choose|select|pick|set up|setup)\s+)?(?:the\s+)?(?:farmhand\s+)?"
                       r"(?:ai|a i|model|ai model|language model|settings|options|menu)(?:\s+(?:settings|model|menu))?$")
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


_EQUIPMENT_TARGET = {"equipment": True}           # _target: the destination is equipment
_EQUIPMENT_WAGON = re.compile(r"\b(?:to|at)\s+(?:the\s+|a\s+)?(?:(?:forage|loader|silage)\s+)?wagon")
_KNOWN_THING = re.compile(r"\b(?:tractor|wagon|forage|it|this|that|them|these|those|pair|setup|rig|combo|everything)\b")


class Interpreter(Protocol):
    def interpret(self, text: str) -> dict | None:
        """A command proposal, or None when the words are not understood."""
        ...


# Common typing slips for the words the rules key on; nothing else is "corrected".
_TYPOS = {"brin": "bring", "brng": "bring", "brign": "bring", "bringg": "bring", "tracter": "tractor",
          "trator": "tractor", "tractr": "tractor", "wagin": "wagon", "waggon": "wagon", "wagn": "wagon"}
_HITCH_VERB = re.compile(r"\b(?:attach|hook|hitch|couple|connect)\b")
# A destination that is equipment, not a place: "take the tractor to the wagon".
_EQUIPMENT = re.compile(r"^(?:(?:the|a|an|my|that|this)\s+)?(?:(?:forage|loader|silage)\s+)?"
                        r"(?:wagon|tractor|trailer|implement)s?$")
# Delivery asked for: the pair is to come to the requester or a place.
_DELIVERY = re.compile(r"\b(?:bring|deliver|here|to me|over to me)\b")
_LEAD_MOVE = re.compile(r"^(?:move|park|drive|return)\b")
_ANY_VERB = re.compile(r"^(?:bring|take|get|fetch|grab|send|drive|deliver|move)\b")
_NAMED = re.compile(r"^(?:bring|take|get|fetch|grab|send|drive|deliver|move)\s+(?:the|my|our)\s+"
                    r"([a-z0-9][a-z0-9 ]*?)\s+(?:to|here|over|back|into|at)\b")


def normalize(text: str) -> str:
    words = re.sub(r"[^0-9a-z]+", " ", text.casefold()).strip()
    words = " ".join(_TYPOS.get(w, w) for w in words.split())
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
    if tokens[0] in _STOP_WORDS or any(words.startswith(p) for p in _STOP_PHRASES) or _STOP_TAIL.search(words):
        return True
    return len(tokens) <= 3 and any(t in _STOP_WORDS for t in tokens)


def refuses(text: str) -> bool:
    """A negation or a question that is not a stop or status: never an order, so
    no model may turn it into one ("don't bring the tractor")."""
    if not isinstance(text, str):
        return True
    words = normalize(text)
    if is_stop(words) or _STATUS.search(words):
        return False
    return bool(_NEGATION.search(words) or _QUESTION.match(words))


class RuleInterpreter:
    """Deterministic wording rules for the supported commands."""

    def interpret(self, text: str) -> dict | None:
        if not isinstance(text, str):
            return None
        words = normalize(text)
        if is_stop(words):
            return {"version": 1, "command": "stop"}
        if _SETTINGS.match(words):
            return {"version": 1, "command": "settings"}
        if re.match(r"^(?:(?:save|make|send|write|create)\s+(?:a\s+)?)?(?:farmhand\s+)?"
                    r"(?:report|diagnostics|bug report|logs)$", words):
            return {"version": 1, "command": "report"}
        if _STATUS.search(words):
            return {"version": 1, "command": "status"}
        if _NEGATION.search(words) or _QUESTION.match(words):
            return None                               # never an order to move
        if _COME.match(words):
            return {"version": 1, "command": "move_pair"}
        tractor, wagon = _TRACTOR.search(words), _WAGON.search(words)
        back = re.search(r"\bback\b", words)
        referent = _REFERENT.search(words) or back
        target = self._target(text, words)
        if target == _EQUIPMENT_TARGET:
            # "take a tractor to the wagon", "deliver the tractor to the forage wagon":
            # the tractor goes to the wagon, i.e. hitch it where the wagon stands.
            return {"version": 1, "command": "hitch_pair"} if wagon or _EQUIPMENT_WAGON.search(text.casefold()) \
                else None
        if tractor and wagon and _HITCH_VERB.search(words) and not target and not _DELIVERY.search(words) \
                and not back:
            # "take the tractor and attach the forage wagon", "hook the tractor to the forage wagon"
            return {"version": 1, "command": "hitch_pair"}
        # Naming a tractor and a wagon asks for a pair, even with "hook them together",
        # when it is "a"/"an"/"any"/"available" one, a fetch-only verb is used, or both
        # are named without "back" and without a leading move/park/drive ("take the
        # tractor and get the forage wagon ... and park it at the farm").
        new_pair = (_NEW_PAIR.search(words) or _FETCH_ONLY.search(words) or _HITCH_VERB.search(words)
                    or (tractor and wagon and not _LEAD_MOVE.match(words)))
        # "grab something that'll pull that forage wagon": a fetch-only verb and a
        # wagon ask for a pair even when no tractor is named.
        pair_named = tractor or _FETCH_ONLY.search(words) or _HITCH_VERB.search(words)
        if pair_named and wagon and not back and (new_pair or not referent) and (
                _FETCH_VERB.search(words) or len(words.split()) <= 6):
            return None if target is None else {"version": 1, "command": "fetch_pair", **target}
        if (referent or (tractor and wagon)) and _MOVE_VERB.search(words):
            if target is not None and (target or re.search(r"\b(?:here|to me|back)\b", words)):
                return {"version": 1, "command": "move_pair", **target}
        if tractor and not wagon and _ANY_VERB.match(words):
            # "bring a tractor to me": a tractor alone is not a job Farmhand does yet;
            # never add a forage wagon the player did not ask for.
            return {"version": 1, "command": "unsupported", "reason": "tractor_only"}
        named = _NAMED.match(words)
        if named and not _KNOWN_THING.search(named.group(1)):
            # "bring the Boss Hoss to the farm": a vehicle by its name is not
            # supported yet; never guessed.
            return {"version": 1, "command": "unsupported", "reason": "named_vehicle"}
        return None

    @staticmethod
    def _target(text: str, words: str) -> dict:
        """A field number, a place in the player's own spelling, {} for
        "here"/"me", or None when the words name no single place (the sentence is
        then not understood, never sent "here" instead). The executor matches
        places exactly (markers) or by all their words (the farm's buildings)."""
        field = _FIELD.search(words)
        if field:
            return {"target": {"type": "field_entrance", "field": int(field.group(1))}}
        spoken = text.strip().rstrip(".!? ")
        previous = None
        while previous != spoken:                     # "... to the barn in a minute please"
            previous, spoken = spoken, _TRAILING.sub("", spoken).rstrip(".!?, ")
        destination = _DESTINATION.search(spoken)
        if destination is None:
            return {}
        name = destination.group(1).strip()
        # "to me at the barn": the place follows the requester words.
        name = re.sub(r"^(?:me|us|here)\s+(?:at|by|near|to|in)\s+(?:the\s+|my\s+)?", "", name, flags=re.IGNORECASE)
        plain = normalize(name)
        if not plain or _HERE.match(plain) or _REFERENT.fullmatch(plain):
            return {}
        if _EQUIPMENT.match(plain):
            return _EQUIPMENT_TARGET                  # equipment, never a place name
        if plain in _GENERIC or len(plain) > 64:
            return None                               # names no single place: not understood
        return {"target": {"type": "place", "name": name[:64]}}


class ChainInterpreter:
    """Rules first; a model only for wording the rules do not understand and do
    not refuse. STOP is always decided by the rules, never delayed by a model."""

    def __init__(self, *interpreters: Interpreter):
        self.interpreters = interpreters

    def interpret(self, text: str) -> dict | None:
        for n, interpreter in enumerate(self.interpreters):
            if n > 0 and refuses(text):
                return None
            try:
                proposal = interpreter.interpret(text)
            except Exception:                     # a failing model must not break text input
                proposal = None
            if proposal is not None:
                return proposal
        return None
