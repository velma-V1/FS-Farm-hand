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
_PREPOSITION = re.compile(r"\b(?:to|at|into|in)\s+(?:the\s+|my\s+|our\s+)?", re.IGNORECASE)
_DESTINATION = re.compile(r"\b(?:to|at|into|in)\s+(?:the\s+|my\s+|our\s+)?([^,.!?]+?)[\s,.!?]*$", re.IGNORECASE)


# Implements by the player's word -> GIANTS specialization kind (companion/executor.py
# HITCHABLE_KINDS). Ordered: the longer names first ("manure spreader" before
# "spreader", "mixer wagon" before the forage wagon's own "wagon" rules).
_IMPLEMENT_NOUNS = [(re.compile(p), kind) for p, kind in (
    (r"\bmixer wagons?\b|\bfeed mixers?\b", "mixer_wagon"),
    (r"\b(?:manure spreaders?|slurry (?:tankers?|tanks?)|manure barrels?|muck spreaders?)\b", "manure_barrel"),
    (r"\bsalt spreaders?\b", "salt_spreader"),
    (r"\b(?:water (?:trailers?|tanks?|bowsers?)|liquid (?:trailers?|tanks?))\b", "water_trailer"),
    (r"\bstone pickers?\b|\brock pickers?\b", "stone_picker"),
    (r"\b(?:bale )?wrappers?\b", "bale_wrapper"),
    (r"\btree planters?\b", "tree_planter"),
    (r"\b(?:ploughs?|plows?)\b", "plow"),
    (r"\b(?:cultivators?|cultivater|harrows?|discs? harrows?|discers?|subsoilers?)\b", "cultivator"),
    (r"\b(?:seeders?|seed drills?|drills?|planters?|sowing machines?|sowers?)\b", "sowing_machine"),
    (r"\b(?:sprayers?|fertili[sz]er spreaders?|spreaders?|lime spreaders?)\b", "sprayer"),
    (r"\bmowers?\b", "mower"),
    (r"\btedders?\b", "tedder"),
    (r"\b(?:windrowers?|rakes?|swathers?)\b", "windrower"),
    (r"\bbalers?\b", "baler"),
    (r"\b(?:trailers?|tippers?|grain carts?|grain trailers?)\b", "trailer"),
    (r"\brollers?\b", "roller"),
    (r"\bweeders?\b", "weeder"),
    (r"\bmulchers?\b", "mulcher"),
)]


def implement_kind(words: str) -> str | None:
    """The implement the words name (first in _IMPLEMENT_NOUNS order), or None."""
    for pattern, kind in _IMPLEMENT_NOUNS:
        if pattern.search(words):
            return kind
    return None


_EQUIPMENT_TARGET = {"equipment": True}           # _target: the destination is equipment
_EQUIPMENT_WAGON = re.compile(r"\bto\s+(?:the\s+|a\s+|an\s+)?(?:(?:forage|loader|silage)\s+)?wagon\b")
_KNOWN_THING = re.compile(r"\b(?:tractor|wagon|forage|it|this|that|them|these|those|pair|setup|rig|combo|everything)\b")


class Interpreter(Protocol):
    def interpret(self, text: str) -> dict | None:
        """A command proposal, or None when the words are not understood."""
        ...


# Common typing slips for the words the rules key on; nothing else is "corrected".
_TYPOS = {"brin": "bring", "brng": "bring", "brign": "bring", "bringg": "bring", "tracter": "tractor",
          "trator": "tractor", "tractr": "tractor", "wagin": "wagon", "waggon": "wagon", "wagn": "wagon"}
_HITCH_VERB = re.compile(r"\b(?:attach|hook|hitch|couple|connect)\b")
# Unhitch where it stands: a leading detach verb ("unhitch the cultivator", "unhook
# the wagon", "detach it"); "drop" only with the implement named ("drop the trailer").
_DETACH_VERB = re.compile(r"^(?:unhitch|detach|unhook|uncouple|disconnect|unattach)\b")
_DROP = re.compile(r"^drop(?:\s+off)?\b")
_IMPLEMENT_WORD = re.compile(r"\b(?:implement|equipment|tool)s?\b")
# A destination that is equipment, not a place: "take the tractor to the wagon".
_EQUIPMENT = re.compile(r"^(?:(?:the|a|an|my|that|this)\s+)?(?:(?:forage|loader|silage)\s+)?"
                        r"(?:wagon|tractor|trailer|implement)s?$")
# A destination that is itself a named implement ("take the tractor to the plough").
_IMPLEMENT_DESTINATION = re.compile(r"^(?:(?:the|a|an|my|that|this)\s+)?[a-z ]{0,30}$")
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
        kind = implement_kind(words)
        if _DETACH_VERB.match(words) or _DROP.match(words):
            return self._detach(text, words, kind)
        if kind is not None:
            return self._implement(text, words, kind)
        tractor, wagon = _TRACTOR.search(words), _WAGON.search(words)
        back = re.search(r"\bback\b", words)
        referent = _REFERENT.search(words) or back
        target = self._target(text, words)
        if target == _EQUIPMENT_TARGET:
            # "take a tractor to the wagon", "deliver the tractor to the forage wagon":
            # an order (leading verb) to take a named tractor plainly "to" the wagon
            # means hitch it where the wagon stands. "go to the wagon", "park it next
            # to the wagon", "i attached the tractor to the wagon": not that.
            if (_ANY_VERB.match(words) or _HITCH_VERB.match(words)) and tractor and not referent \
                    and _EQUIPMENT_WAGON.search(words) and not re.search(r"\bnext to\b", words):
                return {"version": 1, "command": "hitch_pair"}
            return None
        # "hook up the forage wagon and bring it to the barn": an "it" after the wagon
        # is that pair; "hook it up to the wagon" names no pair to fetch.
        if wagon and _HITCH_VERB.search(words) and not back and (
                not referent or referent.start() > wagon.start()) and (
                _ANY_VERB.match(words) or _HITCH_VERB.match(words)):
            # "take the tractor and attach the forage wagon", "hook up the wagon": hitch
            # where the wagon stands; with "bring"/"to me" or a place: fetch it there.
            if target is None:
                return None
            if target or _DELIVERY.search(words):
                return {"version": 1, "command": "fetch_pair", **target}
            return {"version": 1, "command": "hitch_pair"}
        # Naming a tractor and a wagon asks for a pair, even with "hook them together",
        # when it is "a"/"an"/"any"/"available" one, a fetch-only verb is used, or both
        # are named without "back" and without a leading move/park/drive ("take the
        # tractor and get the forage wagon ... and park it at the farm").
        new_pair = (_NEW_PAIR.search(words) or _FETCH_ONLY.search(words)
                    or (tractor and wagon and not _LEAD_MOVE.match(words)))
        # "grab something that'll pull that forage wagon": a fetch-only verb and a
        # wagon ask for a pair even when no tractor is named.
        pair_named = tractor or _FETCH_ONLY.search(words)
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

    def _detach(self, text: str, words: str, kind: str | None) -> dict | None:
        """Unhitch the named implement (any, when none is named) where it stands.
        Never somewhere else: "drop the trailer at the barn" is not understood."""
        if kind is None and _WAGON.search(words):
            kind = "forage_wagon"
        if _DROP.match(words) and kind is None and not _IMPLEMENT_WORD.search(words):
            return None
        if self._target(text, words) != {}:
            return None
        proposal = {"version": 1, "command": "detach_pair"}
        return {**proposal, "implement": kind} if kind is not None else proposal

    def _implement(self, text: str, words: str, kind: str) -> dict | None:
        """A named implement other than the forage wagon: hitch it where it stands
        ("hitch the plough", "take the tractor to the seeder", "attach the trailer
        to a tractor"), or fetch it hitched to the requester or a place ("bring me
        the cultivator", "hook up the sprayer and take it to field 3")."""
        if not (_HITCH_VERB.search(words) or _FETCH_VERB.search(words) or _ANY_VERB.match(words)):
            return None
        target = self._target(text, words)
        if target is None:
            return None
        proposal = {"version": 1, "implement": kind}
        if target == _EQUIPMENT_TARGET:
            return {**proposal, "command": "hitch_pair"}
        if target or _DELIVERY.search(words) or (_FETCH_VERB.search(words) and not _HITCH_VERB.search(words)):
            return {**proposal, "command": "fetch_pair", **target}
        return {**proposal, "command": "hitch_pair"}

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
        # The last "to/at/into/in" names the destination ("hitch the tractor to the
        # wagon then take it to the barn" -> barn; "to me at the barn" -> barn).
        prepositions = list(_PREPOSITION.finditer(spoken))
        if not prepositions:
            return {}
        name = spoken[prepositions[-1].end():].strip(" ,.!?")
        # "to me at the barn": the place follows the requester words.
        name = re.sub(r"^(?:me|us|here)\s+(?:at|by|near|to|in)\s+(?:the\s+|my\s+)?", "", name, flags=re.IGNORECASE)
        plain = normalize(name)
        if not plain or _HERE.match(plain) or _REFERENT.fullmatch(plain):
            return {}
        if _EQUIPMENT.match(plain) or (implement_kind(plain) is not None and _IMPLEMENT_DESTINATION.match(plain)):
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
