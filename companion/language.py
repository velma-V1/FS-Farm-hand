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
                 "stay put", "stand still", "no stop", "kill the engine", "shut it down", "shut down", "pull over",
                 "park it there", "that s enough", "enough")
# A request that ends in a stop ("bring it back here and stop") is a stop.
_STOP_TAIL = re.compile(r"\b(?:and|then|now)\s+(?:stop|halt|cancel|stay\s+put|stay\s+there|stand\s+still)$")
# Words that make a sentence not an order: never move on a negation or a question.
_NEGATION = re.compile(r"\b(?:don t|dont|do not|does not|doesn t|not|never|no|didn t|didnt|won t|wont)\b")
_QUESTION = re.compile(r"^(?:where|why|did|is|are|was|were|has|have|had|who|when|which|what|whose|does|do)\b")
# Too general to name one place; the sentence is not understood instead.
# ("farm" is a name the executor resolves exactly or blocks: a marker or one building.)
_GENERIC = frozenset({"field", "the field", "there", "over there", "that", "it", "this",
                      "place", "somewhere", "building", "a building", "anywhere"})
# Trailing words about time or manner, not the place ("to the barn in a minute").
_TRAILING = re.compile(r"\s+(?:please|now|right now|right away|asap|quickly|soon|again|thanks|thank you|for me|for us|"
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
_MOVE_VERB = re.compile(r"\b(?:bring|take|drive|move|park|send|return|put|deliver|haul)\b")
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
    (r"\b(?:trailers?|tippers?|grain carts?|grain trailers?|goosenecks?|deckovers?)\b", "trailer"),
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
# "take the seeder off (the tractor)", "take off the plough": unhitch (was read as a hitch).
_TAKE_OFF = re.compile(r"^(?:take|get|pull)\s+(?:\S+\s+){0,3}?off\b")
_IMPLEMENT_WORD = re.compile(r"\b(?:implement|equipment|tool)s?\b")
# A destination that is equipment, not a place: "take the tractor to the wagon".
_EQUIPMENT = re.compile(r"^(?:(?:the|a|an|my|that|this)\s+)?(?:(?:forage|loader|silage)\s+)?"
                        r"(?:wagon|tractor|trailer|implement)s?$")
# A destination that is itself a named implement ("take the tractor to the plough").
_IMPLEMENT_DESTINATION = re.compile(r"^(?:(?:the|a|an|my|that|this)\s+)?[a-z ]{0,30}$")
# Delivery asked for: the pair is to come to the requester or a place.
_DELIVERY = re.compile(r"\b(?:bring|deliver|here|to me|over to me)\b")
_LEAD_MOVE = re.compile(r"^(?:move|park|drive|return)\b")
_ANY_VERB = re.compile(r"^(?:bring|take|get|fetch|grab|send|drive|deliver|move|haul|transport)\b")
_NAMED = re.compile(r"^(?:bring|take|get|fetch|grab|send|drive|deliver|move|haul|transport)\s+(?:the|my|our)\s+"
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


# Field work: "plow, seed and fertilize field 7", "seed the smallest field".
_FIELD_VERBS = {"plow": "plow", "plough": "plow", "cultivate": "cultivate", "till": "cultivate",
                "seed": "sow", "sow": "sow", "plant": "sow", "drill": "sow",
                "fertilize": "fertilize", "fertilise": "fertilize", "spray": "fertilize", "harvest": "harvest",
                "combine": "harvest", "cut": "harvest"}
_FIELD_VERB = r"(?:" + "|".join(_FIELD_VERBS) + r")"
_FIELD_WORK = re.compile(
    rf"^(?:please\s+)?(?:go\s+(?:and\s+)?)?(?P<verbs>{_FIELD_VERB}(?:\s+(?:(?:and|then|and\s+then)\s+)?{_FIELD_VERB})*)"
    r"\s+(?:(?:a\s+)?(?:(?P<test>small|test)\s+(?:test\s+)?(?:area|patch|strip|plot)"
    r"|(?P<m2>\d{2,5})\s+(?:square\s+(?:meters|metres)|m2|sqm))\s+(?:of|on|in)\s+)?"
    r"(?:on\s+|in\s+)?(?:the\s+|our\s+|my\s+)?(?:(?P<smallest>smallest|small)\s+(?:owned\s+)?field"
    r"|field\s+(?:number\s+)?(?P<number>\d{1,5}))(?:\s+please)?$")


# "plow field 3, seed it with wheat, then fertilize it": a field operation on the field just
# named ("it", "that", "the field"), the crop when sown; one field's operations make one job.
_FIELD_IT = re.compile(rf"^(?:and\s+)?(?:then\s+)?(?P<verb>{_FIELD_VERB})\s+(?:it|that|the\s+field|that\s+field|there)"
                       r"(?:\s+(?:with|to)\s+(?:some\s+)?(?P<crop>[a-z]{2,15}(?:\s+[a-z]{2,15})?))?(?:\s+please)?$")


# "the harvester still has wheat in it, unload it and sell it": a clause saying what a combine
# holds makes "it" the combine for the clauses after it (unload it -> unload the combine).
_COMBINE_SUBJECT = re.compile(r"^(?:the|my|our)\s+(?:combines?|harvesters?)\s+(?:is|s|still|has|have|got|are)\b.*$")
_UNLOAD_IT = re.compile(r"^(?:and\s+)?(?:then\s+)?(?:unload|empty)\s+(?:it|that|them)(?:\s+please)?$")


# "buy field 12", "buy the land at field 12", "sell field 12", "sell the farmland of field 12":
# the farmland parcel the field lies on, bought or sold (never the grain: "sell the grain").
_LAND = re.compile(r"^(?:please\s+)?(?:go\s+(?:and\s+)?)?(?P<verb>buy|purchase|sell)\s+(?:me\s+|us\s+)?(?:the\s+|that\s+)?"
                   r"(?:(?:land|farmland|parcel|plot|ground)\s+(?:(?:at|of|on|for|with|under|around)\s+)?(?:the\s+)?)?"
                   r"field\s+(?:number\s+)?(?P<number>\d{1,5})(?:\s+please)?$")


def land(words: str) -> dict | None:
    match = _LAND.match(words)
    if match is None or int(match["number"]) < 1:
        return None
    return {"version": 1, "command": "land", "land_action": "sell" if match["verb"] == "sell" else "buy",
            "field": int(match["number"])}


TEST_AREA_M2 = 400        # "a small area of field 7": about a minute of work for one implement


# What one command already does: a sequence of only these is that command ("hitch the
# tractor to the forage wagon then take it to the barn" is one fetch).
_COVERS = {"fetch_pair": {"hitch_pair", "fetch_pair", "move_pair"}, "buy": {"buy"},
           "load_tractor": {"load_tractor"}, "field_work": {"field_work", "sell", "store"},
           # "... and take it off there" after a haul is its unload, not an unhitch
           "haul_tractor": {"haul_tractor", "unload_tractor", "move_pair", "detach_pair"}}
# "Take it back", "put the deckover back (where it was)": back to where Farmhand took it from.
_BACK = re.compile(r"(?:please\s+)?(?:take|put|bring|drive|move|return)\s+(?:it|them|that|this|everything"
                   r"|(?:the|that|my)\s+(?P<thing>[a-z0-9 ]{1,40}?))\s+back(?:\s+(?:up|over))?"
                   r"(?:\s+(?:where|to where)\s+(?:it|they)\s+(?:was|were|came from|belongs?))?")
# "Follow me" (FollowMe), only ever on these words: "follow me (with the gmc)", "have the gmc
# follow me", "gmc, follow me".
_FOLLOW = re.compile(r"(?:please\s+)?(?:(?:have|let|make)\s+(?:the\s+|my\s+)?(?P<a>[a-z0-9 ]{1,40}?)\s+follow\s+me"
                     r"|follow\s+me(?:\s+with\s+(?:the\s+|my\s+)?(?P<b>[a-z0-9 ]{1,40}?))?"
                     r"|(?:the\s+|my\s+)?(?P<c>[a-z0-9 ]{1,40}?)\s+follow\s+me)(?:\s+please)?")
# Continue the kept goal; a new destination for it.
_RESUME = re.compile(r"(?:please\s+)?(?:continue|resume|carry on|keep going|go on|go ahead|pick up where you left off"
                     r"|finish (?:it|the job))(?:\s+(?:please|the job|with it|now))?")
_INSTEAD = re.compile(r"^(?:no\s+|actually\s+|wait\s+)?(?:(?:(?:take|bring|deliver|move|haul|send|drive)\s+(?:it|them|everything|the tractor|the rig|the truck)\s+|go\s+)(to\s+.+?)\s+instead"
                      r"|change\s+(?:the\s+)?destination\s+(to\s+.+?))$")
# "take the tractor to the farm", "bring the deutz to field 3": a tractor by itself to a place.
_TRACTOR_TO = re.compile(r"^(?:please\s+)?(?:take|bring|haul|deliver|transport|move)\s+(?:the|my|our)\s+(?:tractor|deutz[a-z0-9 ]*?|series\s+\d+)"
                         r"\s+(?:to|over\s+to)\s+\S")
# "... and unload it (there)": the end of a haul goal.
_UNLOAD_TAIL = re.compile(r"\s+(?:and\s+)?(?:then\s+)?(?:unload\s+(?:it|him|that|the tractor)|(?:take|drop|get|put)\s+(?:it|him|that)"
                          r"\s+off)(?:\s+there)?$")
# "... on the 379", "... with the gmc's trailer": the truck whose transport carries the tractor.
_ON_TRUCK = re.compile(r"\s+(?:on|with|using|in)\s+(?:the\s+|my\s+|our\s+)?(?P<truck>[a-z0-9][a-z0-9-]{0,20})(?:\s+s)?"
                       r"(?:\s+(?:truck|trailer|lowboy|low\s+loader|lowloader|deckover|flatbed|gooseneck|transport))?"
                       r"(?=\s+(?:and|then)\b|$)")
# Clause breaks of a sequence: commas, "then", "and then", ", and", semicolons.
_VERB = (r"(?:load|unload|buy|purchase|deliver|bring|take|haul|hitch|unhitch|detach|move|drive|park|follow|save|use|sell|store"
         # a field verb only with its field after it ("and plow it", "and seed field 3"; "the tractor
         # and plow" is the implement)
         r"|(?:plow|plough|cultivate|till|seed|sow|plant|drill|fertili[sz]e|spray|harvest)"
         r"(?=\s+(?:it|that|there|the\s+field|field\s+\d|(?:the\s+|our\s+|my\s+)?field\s+\d))"
         r"|do(?=\s+(?:a|an|the)\s+(?:[a-z]+\s+)?contract))\b")
_CLAUSE_BREAK = re.compile(r"\s*,\s*(?:and\s+)?(?:then\s+)?|\s+(?:and\s+)?then\s+|\s*;\s*"
                           # "harvest field 5 and while that runs take the deutz to the farm": the
                           # helper works on while the next job goes (field work is a background worker)
                           r"|\s+(?:and\s+)?(?:while|whilst)\s+(?:that|it|this|they|the\s+helper)\s+"
                           r"(?:runs?|works?|harvests?|goes|is\s+(?:running|working|going|harvesting))\s*,?\s+"
                           r"|\s+and\s+(?:in\s+the\s+)?meanwhile\s+"
                           # "... and load the tractor": "and" before a command verb
                           r"|\s+and\s+(?=" + _VERB + r")"
                           # "load it back up bring it back up": one command running into the next
                           r"|(?<=\bup)\s+(?=" + _VERB + r")|(?<=\bback)\s+(?=" + _VERB + r")"
                           r"|(?<=\bhere)\s+(?=" + _VERB + r")|(?<=\bthere)\s+(?=" + _VERB + r")")
# Tractor transport (AGENTS.md exception 5): "load the tractor onto the trailer", "load the
# deutz on the deckover and secure it", "unload it", "take the tractor off the trailer".
# Belting it and raising the ramps are part of every load ("secure it", "raise the ramps").
_THING = r"[a-z0-9]{1,20}(?:\s+[a-z0-9]{1,20}){0,3}?"
_LOAD = re.compile(
    r"^(?:please\s+)?(?P<verb>load|put|drive|get|run)\s+(?:up\s+)?(?:the\s+|my\s+|our\s+|a\s+)?(?P<tractor>" + _THING + r")"
    r"(?:\s+(?:up\s+)?(?:on|onto|on to|into|up)\s+(?:the\s+|my\s+|our\s+)?(?P<trailer>" + _THING + r"))?"
    r"(?:\s+(?:and|then)\s+(?:secure|strap|belt|tie|chain)\s+(?:it|him|that)(?:\s+down)?)?"
    r"(?:\s+(?:and|then)\s+(?:raise|lift|close|put\s+up)\s+the\s+ramps(?:\s+up)?)?$")
_UNLOAD = re.compile(
    r"^(?:please\s+)?(?:(?:unload|offload)(?:\s+(?:the\s+|my\s+)?(?P<tractor>" + _THING + r"))?"
    r"|(?:take|drive|get|back|run)\s+(?:the\s+|my\s+)?(?P<off>" + _THING + r")\s+off)"
    r"(?:\s+(?:of\s+)?(?:off\s+|from\s+)?(?:the\s+|my\s+)?(?P<trailer>" + _THING + r"))?$")
# What a transport clause may name: a trailer word, a truck, or the thing itself.
_TRAILER_WORDS = re.compile(r"\b(?:trailers?|decks?|deckovers?|goosenecks?|lowboys?|lowloaders?|low\s+loaders?|flatbeds?)\b")
_TRANSPORT_NOT = re.compile(r"\b(?:wagon|forage|implement|bales?|pallets?|grain|silage|logs?|wood)\b")
# Clauses a sequence folds into its load (the load always belts and raises the ramps).
_ABSORBED = re.compile(r"^(?:then\s+)?(?:(?:secure|strap|belt|tie|chain)\s+(?:it|him|that|the tractor)(?:\s+down)?"
                       r"|(?:raise|lift|close|put\s+up|lower|drop|put\s+down)\s+the\s+ramps(?:\s+(?:up|down))?)$")
# "take the gmc", "use the gmc": the towing vehicle for the hitch that follows.
_TAKE_VEHICLE = re.compile(r"^(?:take|use|grab|get|with)\s+(?:the|my|our)\s+([a-z0-9][a-z0-9 ]{0,30}?)(?:\s+truck)?$")


def _vehicle_name(words: str | None) -> str | None:
    """A tractor named by its words ("the deutz series 9"), or None for just "the tractor"/"it"."""
    if not words:
        return None
    words = re.sub(r"\s*\btractors?\b\s*", " ", words).strip()
    if not words or _KNOWN_THING.fullmatch(words) or re.fullmatch(r"(?:it|him|that|this|one)", words):
        return None
    return words


def _trailer_name(words: str | None) -> str | None:
    """A transport trailer named beyond its kind ("the deckover", "the lizard trailer")."""
    if not words:
        return None
    if _GOOSENECK.search(words):
        return "gooseneck"
    if _DECKOVER.search(words):
        return "deckover"
    name = re.sub(r"\s*\b(?:trailers?|decks?)\b\s*", " ", words).strip()
    return name or None


def transport(words: str) -> dict | None:
    words = _DEUTZ.sub("deutz", words)
    # "load it back up", "put it back on": load it again.
    words = re.sub(r"^((?:please\s+)?(?:load|put)\s+(?:it|him|that|the tractor|the deutz))\s+back(?:\s+(?:up|on))?\b",
                   r"\1", words)
    load, unload = _LOAD.match(words), _UNLOAD.match(words)
    if load is not None:
        trailer = load["trailer"]
        if load["verb"] != "load" and (trailer is None or not _TRAILER_WORDS.search(trailer)):
            return None                   # "drive the tractor onto the field" is no load
        if _TRANSPORT_NOT.search(load["tractor"]) or (trailer and _TRANSPORT_NOT.search(trailer)):
            return None                   # a wagon or a load of goods: not a tractor transport
        proposal = {"version": 1, "command": "load_tractor"}
        # "load the tractor and (the) plow on the gooseneck": the tractor with that implement on it.
        carried = re.match(r"^(?P<t>.+?)\s+(?:and|with)\s+(?:the\s+|its\s+|a\s+|his\s+)?(?P<i>.+)$", load["tractor"])
        if carried is not None and (implement_kind(carried["i"]) or re.search(r"\bplows?\b|\bploughs?\b", carried["i"])):
            proposal["carry"] = carried["i"][:40]
            load_tractor_words = carried["t"]
        else:
            load_tractor_words = load["tractor"]
        tractor = _vehicle_name(load_tractor_words)
        if tractor is not None and _TRAILER_WORDS.search(tractor):
            return None
        if tractor is not None:
            proposal["tractor_name"] = tractor
        name = _trailer_name(trailer) if trailer and _TRAILER_WORDS.search(trailer) else None
        if name is not None:
            proposal["implement_name"] = name
        return proposal
    if unload is not None:
        thing, trailer = unload["tractor"] or unload["off"], unload["trailer"]
        if unload["off"] is not None and (trailer is None or not _TRAILER_WORDS.search(trailer)):
            return None                   # "take the seeder off the tractor" is an unhitch
        if thing and _TRANSPORT_NOT.search(thing):
            return None
        proposal = {"version": 1, "command": "unload_tractor"}
        tractor = None if thing and _TRAILER_WORDS.search(thing) else _vehicle_name(thing)
        if tractor is not None:
            proposal["tractor_name"] = tractor
        name = _trailer_name(trailer) if trailer and _TRAILER_WORDS.search(trailer) else None
        if name is None and thing and _TRAILER_WORDS.search(thing):
            name = _trailer_name(thing)
        if name is not None:
            proposal["implement_name"] = name
        return proposal
    return None


# Buying: "buy a large plow, the gmc truck and a gooseneck", "buy a fertilizer spreader".
_BUY = re.compile(r"^(?:please\s+)?(?:go\s+(?:and\s+)?)?(?:buy|purchase|get us|order)\s+(?P<items>.+?)"
                  r"(?:\s+from\s+the\s+(?:store|shop|dealer))?$")
_BUY_ITEM = re.compile(
    r"\s*(?:(?:and|plus|also)\s+)?(?:(?:a|an|the|one|new)\s+)?(?:new\s+)?(?P<size>large|big|biggest|largest)?\s*"
    # A tractor by its maker ("a deutz series 9", "a john deere 8r"): the store's brand words,
    # never followed by another kind's noun ("a john deere plow" is a plow).
    r"(?:(?P<bname>(?:deutz|fendt|john\s+deere|case(?:\s+ih)?|new\s+holland|massey(?:\s+ferguson)?|claas|valtra"
    r"|kubota|mccormick|lindner|steyr|landini|zetor|jcb|same|lamborghini|antonio\s+carraro|rigitrac)"
    r"(?:\s+(?!(?:plows?|ploughs?|gooseneck|deckover|trailer|truck|pickup|tractor|with|and|plus)\b)[a-z0-9]{1,20}){0,3}?)"
    r"(?=\s+(?:and|plus|also)\b|$)"
    r"|(?P<spreader>(?:fertili[sz]er\s+)?spreaders?)|(?P<sprayer>(?:crop\s+)?sprayers?)"
    r"|(?P<plow>plows?|ploughs?)|(?P<pname>[a-z0-9]{1,20}(?:\s+[a-z0-9]{1,20}){0,4}?)\s+(?:plows?|ploughs?)|(?P<goose>gooseneck)(?:\s+trailer)?"
    r"|(?P<deck>(?:[a-z0-9-]*[0-9][a-z0-9-]*\s+)?deckover)(?:\s+trailer)?"
    # A named tractor, with the engine option ("... with a boss hoss engine setup") or the word.
    r"|(?P<tractor>[a-z0-9]{1,20}(?:\s+[a-z0-9]{1,20}){0,3}?)(?:\s+tractor)?\s+with\s+(?:a|an|the)\s+"
    r"(?P<engine>[a-z0-9]{1,20}(?:\s+[a-z0-9]{1,20}){0,2}?)\s+(?:engine|motor)"
    r"(?:\s+(?:setup|set\s+up|option|configuration|package))?"
    r"|(?P<tname>[a-z0-9]{1,20}(?:\s+[a-z0-9]{1,20}){0,3}?)\s+tractor"

    r"|(?P<name>[a-z0-9][a-z0-9-]{0,20}(?:\s+[a-z0-9][a-z0-9-]{0,20})?)\s+(?:truck|pickup))\b")
# GIANTS spells the brand as one word (DEUTZFAHR): the player's "deutz-fahr" (and its
# slips, live "Deutz-Farhr") is matched by "deutz".
_DEUTZ = re.compile(r"\bdeutz\s+f(?:ahr|arh|arhr|ahrr|har)\b")


def buy(words: str) -> dict | None:
    match = _BUY.match(words)
    if match is None:
        return None
    text, wants, at = _DEUTZ.sub("deutz", match["items"]), [], 0
    # "... plow with the 25m unrealistic working width and the 25 mph speed selection": the
    # item's configuration choices, by their own words (an engine is its own rule).
    options = None
    split = re.match(r"^(?P<item>.+?)\s+with\s+(?P<options>.+)$", text)
    if split is not None and not re.search(r"\b(?:engine|motor)\b", split["options"]):
        options = [_option_words(o) for o in re.split(r"\s*,\s*|\s+and\s+", split["options"]) if o.strip()]
        if not options or not all(options) or len(options) > 4:
            return None
        text = split["item"]
    while at < len(text):
        item = _BUY_ITEM.match(text, at)
        if item is None or item.end() == at:
            return None                               # not something Farmhand knows how to choose
        if item["plow"]:
            want = {"kind": "plow"}
        elif item["spreader"] or item["sprayer"]:
            want = {"kind": "spreader" if item["spreader"] else "sprayer"}
        elif item["pname"]:
            if re.search(r"\b(?:a|an|the|and|plus|with)\b", item["pname"]):
                return None
            name = item["pname"]
            width = re.match(r"^(\d{1,3}(?:\.\d)?\s?m)\s+(.+)$", name)        # "a 25m titan ...": its width option
            want = {"kind": "plow", "name": width.group(2) if width else name}
            if width:
                want["options"] = [width.group(1).replace(" ", "")]
        elif item["goose"]:
            want = {"kind": "trailer", "name": "gooseneck"}
        elif item["deck"]:
            want = {"kind": "trailer", "name": item["deck"]}
        elif item["tractor"] or item["tname"] or item["bname"]:
            name = item["tractor"] or item["tname"] or item["bname"]
            if re.search(r"\b(?:a|an|the|and|plus|with)\b", name):
                return None
            want = {"kind": "tractor", "name": name}
            if item["engine"]:
                want["engine"] = item["engine"]
        else:
            if re.search(r"\b(?:a|an|the|and|plus)\b", item["name"]):
                return None
            want = {"kind": "truck", "name": item["name"]}
        if item["size"]:
            want["size"] = "large"
        wants.append(want)
        at = item.end()
    if not wants or len(wants) > 4:
        return None
    if options is not None:
        if len(wants) != 1:
            return None                       # whose options? said for one item only
        wants[0]["options"] = (wants[0].get("options") or []) + [o for o in options if o not in wants[0].get("options", [])]
    return {"version": 1, "command": "buy", "wants": wants}


_OPTION_FILLER = re.compile(r"^(?:(?:the|a|an)\s+)|\s+(?:working\s+width|work\s+width|width|speed\s+(?:selection|section|setting|option)"
                            r"|speed|selection|setting|option|configuration|setup|config)$")


def _option_words(phrase: str) -> str | None:
    """An option as the player said it, without articles and kind words ("25m unrealistic")."""
    words, previous = phrase.strip(), None
    while previous != words:
        previous, words = words, _OPTION_FILLER.sub("", words).strip()
    return words[:40] if re.fullmatch(r"[a-z0-9][a-z0-9 -]*", words or "") else None


# Where crop goes: the farm's own storage, and what the crop is called.
_SILO = r"(?:the\s+|my\s+|our\s+)?(?:farm\s+)?(?:silos?|grain\s+bins?|bins?|storage)"
_CROP_NAME = r"(?:the|my|our)\s+(?:grain|crops?|harvest|load|wheat|barley|canola|corn|oats|soybeans|sorghum|straw|grass|hay)"
_CROP_WORDS = rf"(?:it|them|that|{_CROP_NAME})"
# "harvest field 3 and sell the grain", "... and store it", "... and put it in the silo": where
# this harvest's crop goes (else the saved harvest policy; else it stays in the trailer).
_HARVEST_TO = re.compile(rf"\s+(?:and\s+|then\s+)+(?:(?P<sell>sell\b.*)"
                         rf"|store(?:\s+{_CROP_WORDS})?(?:\s+(?:in|into)\s+{_SILO})?"
                         rf"|(?:put|take|bring|haul)\s+{_CROP_WORDS}\s+(?:in|into|to)\s+{_SILO})(?:\s+please)?$")


# "collect the straw on field 5 with the forage wagon", "pick up the grass swaths from field 2":
# windrows picked up with a forage wagon (GIANTS' helper drives it), field_work "collect".
_FORAGE_WAGON = r"(?:\s+(?:with|using)\s+(?:the\s+|a\s+|our\s+|my\s+)?(?:forage|loading|loader|pick\s*up)\s+wagon)?"
_COLLECT_VERB = re.compile(r"^(?:please\s+)?(?:go\s+(?:and\s+)?)?(?:collect|pick\s+up|gather|load\s+up|bring\s+in|rake\s+up)\s")
_COLLECT = re.compile(
    r"^(?:please\s+)?(?:go\s+(?:and\s+)?)?(?:collect|pick\s+up|gather|load\s+up|bring\s+in|rake\s+up)\s+"
    r"(?:the\s+|our\s+|my\s+|all\s+the\s+)?(?:straw|grass|hay|windrows?|swaths?|swathes?)"
    r"(?:\s+(?:windrows?|swaths?|swathes?|rows))?" + _FORAGE_WAGON +
    r"\s+(?:on|from|off|in|of)\s+(?:the\s+)?field\s+(?:number\s+)?(?P<number>\d{1,5})" + _FORAGE_WAGON +
    r"(?:\s+please)?$")


def field_work(words: str) -> dict | None:
    words = _TRAILING.sub("", words)            # "go cut field 5 for me"
    destination = None
    if words.startswith("harvest") or _COLLECT_VERB.match(words):
        to = _HARVEST_TO.search(words)
        if to is not None:
            destination = "sell" if to["sell"] else "store"
            words = words[:to.start()]
    # "... anyway": the player knows a crop stands there and wants the work done.
    over_crop = re.search(r"\s+(?:anyway|anyways|regardless)$", words)
    if over_crop is not None:
        words = words[:over_crop.start()]
    collect = _COLLECT.match(words)
    if collect is not None:
        field = int(collect["number"])
        if field < 1:
            return None
        work = {"version": 1, "command": "field_work", "field": field, "operations": ["collect"]}
        if destination is not None:
            work["destination"] = destination
        return work
    match = _FIELD_WORK.match(words)
    if match is None:
        return None
    operations = []
    for verb in re.findall(_FIELD_VERB, match["verbs"]):
        operation = _FIELD_VERBS[verb]
        if operation in operations:
            return None                              # "seed and sow": not an order of work
        operations.append(operation)
    field = "smallest" if match["smallest"] else int(match["number"])
    if field != "smallest" and field < 1:
        return None
    work = {"version": 1, "command": "field_work", "field": field, "operations": operations}
    if destination is not None:
        work["destination"] = destination
    if over_crop is not None:
        work["over_crop"] = True
    if match["test"]:
        work["area_m2"] = TEST_AREA_M2
    elif match["m2"]:
        area = int(match["m2"])
        if not 50 <= area <= 20000:
            return None
        work["area_m2"] = area
    return work


# "plant wheat on field 3", "sow sugar beets in field 12", "plant field 3 with corn": the
# player says what to plant; Farmhand prepares the ground first where it is not ready.
_SOW_VERB = r"(?:plant|sow|seed|drill)"
_FIELD_N = r"(?:the\s+|our\s+|my\s+)?field\s+(?:number\s+)?(?P<number>\d{1,5})"
_CROP = r"(?:some\s+)?(?P<crop>[a-z]{2,15}(?:\s+[a-z]{2,15})?)"
_PLANT = re.compile(
    rf"^(?:please\s+)?(?:go\s+(?:and\s+)?)?(?:(?P<prep>{_FIELD_VERB})\s+(?:and\s+(?:then\s+)?|then\s+)?)?(?:"
    rf"{_SOW_VERB}\s+{_CROP}\s+(?:on|in)\s+{_FIELD_N}"
    rf"|{_SOW_VERB}\s+{_FIELD_N.replace('number>', 'number2>')}\s+with\s+{_CROP.replace('crop>', 'crop2>')}"
    r")(?:\s+please)?$")
_NOT_CROPS = frozenset(_FIELD_VERBS) | {"a", "an", "the", "it", "field", "small", "test", "area", "and", "then"}


# "do a contract", "take the best contract", "do the plow contract on field 12", "finish the
# contract", "collect the contract": one goal (taken, worked, collected; re-planned from
# GIANTS' own state). The type by the start of GIANTS' name for it.
_CONTRACT_KINDS = (("plow", "plow"), ("plough", "plow"), ("cultivat", "cultiv"), ("fertili", "fertili"),
                   ("spray", "herbici"), ("sow", "sow"), ("seed", "sow"), ("harvest", "harvest"), ("mow", "mow"),
                   ("bal", "bale"), ("weed", "weed"))
_CONTRACT = re.compile(
    r"^(?:please\s+)?(?:go\s+(?:and\s+)?)?(?:do|take|start|accept|complete|finish|work|get|collect|cash\s+in)\s+"
    r"(?:(?:me\s+)?(?:a|an|the|one|my|our)\s+)?(?:(?:best\s+paying|highest\s+paying|best|top)\s+)?"
    r"(?:(?P<kind>[a-z]+)\s+)?(?:contract|mission)s?(?:\s+(?:reward|money|pay))?"
    rf"(?:\s+(?:on|in|at|for)\s+{_FIELD_N})?(?:\s+please)?$")


def contract(words: str) -> dict | None:
    match = _CONTRACT.match(words)
    if match is None:
        return None
    proposal = {"version": 1, "command": "contract"}
    if match["kind"]:
        kind = next((k for word, k in _CONTRACT_KINDS if match["kind"].startswith(word)), None)
        if kind is None:
            return None
        proposal["kind"] = kind
    if match["number"]:
        proposal["field"] = int(match["number"])
        if proposal["field"] < 1:
            return None
    return proposal


# "sell the grain", "sell the wheat in the trailer", "go sell the load": the cargo a rig
# carries, to the best paid sell point (AutoDrive's own). Never a vehicle ("sell the tractor").
_SELL = re.compile(r"^(?:please\s+)?(?:go\s+(?:and\s+)?)?sell\s+(?P<what>.{1,60}?)"
                   r"(?:\s+(?:in|on|from)\s+(?:the|my|our)\s+(?:trailer|truck|wagon))?(?:\s+please)?$")
_SELL_VEHICLE = re.compile(r"\b(?:tractor|truck|trailer|wagon|combine|harvester|plow|plough|cultivator|seeder|"
                           r"sprayer|implement|vehicle|deutz|gmc)s?\b")


def sell(words: str) -> dict | None:
    match = _SELL.match(words)
    if match is None or _SELL_VEHICLE.search(match["what"]) or implement_kind(match["what"]):
        return None
    return {"version": 1, "command": "sell"}


# "store the grain", "put the grain in the silo", "take the load to the grain bin": the cargo a
# rig carries into the farm's own storage (GIANTS' Deliver helper), nothing sold. Never a vehicle.
# The crop by name only ("take this to the silo" is the hitched pair).
_STORE = re.compile(rf"^(?:please\s+)?(?:go\s+(?:and\s+)?)?(?:store\s+{_CROP_NAME}(?:\s+(?:in|into)\s+{_SILO})?"
                    rf"|(?:put|take|bring|haul|unload)\s+{_CROP_NAME}(?:\s+in\s+the\s+trailer)?\s+(?:in|into|to)\s+{_SILO})"
                    r"(?:\s+please)?$")


def store(words: str) -> dict | None:
    return {"version": 1, "command": "store"} if _STORE.match(words) else None


# "from now on sell the harvest", "always store the grain", "keep the grain in the trailer": where
# harvested crop goes when a harvest request does not say (saved). Selling or storing needs the
# "from now on" (else it is an order for the cargo there now).
_POLICY = re.compile(r"^(?:please\s+)?(?P<pre>(?:from\s+now\s+on|always|by\s+default)\s+)?(?P<verb>sell|store|keep)\s+"
                     r"(?:the\s+|my\s+|our\s+|all\s+(?:the\s+)?)?(?:harvests?|grain|crops?)"
                     r"(?:\s+in\s+the\s+trailers?)?(?P<post>\s+(?:from\s+now\s+on|by\s+default|always))?$")


def harvest_policy(words: str) -> dict | None:
    match = _POLICY.match(words)
    if match is None or (match["verb"] != "keep" and not (match["pre"] or match["post"])):
        return None
    return {"version": 1, "command": "harvest_policy", "destination": match["verb"]}


# "hitch them together", "hook them up": the tractor and implement just bought (the
# sequence's context names them); "hitch the plow to the deutz": that implement to that tractor.
_TOGETHER = re.compile(r"^(?:please\s+)?(?:hitch|hook|couple|attach|connect)\s+(?:them|those|these|both)"
                       r"(?:\s+(?:together|up))?(?:\s+please)?$")
_HITCH_TO = re.compile(r"^(?:please\s+)?(?:hitch|hook|couple|attach|connect)\s+(?:up\s+)?(?:the|my|our)\s+"
                       r"(?P<a>[a-z0-9-]+(?:\s+[a-z0-9-]+){0,3}?)\s+(?:to|on|onto|behind)\s+(?:the|my|our)\s+"
                       r"(?P<b>[a-z0-9-]+(?:\s+[a-z0-9-]+){0,3}?)(?:\s+please)?$")
# "hitch it to the 3650": what was just bought, to that tractor (the sequence's context).
_HITCH_IT = re.compile(r"^(?:please\s+)?(?:hitch|hook|couple|attach|connect)\s+(?:it|that|this|them)(?:\s+up)?\s+"
                       r"(?:to|on|onto|behind)\s+(?:the|my|our)\s+(?P<b>[a-z0-9-]+(?:\s+[a-z0-9-]+){0,3}?)(?:\s+please)?$")
# A vehicle as the player names one: its maker, or "tractor" / "truck" (never a place).
_VEHICLE_WORDS = re.compile(r"\b(?:tractor|truck|pickup|deutz|fendt|john\s+deere|case|new\s+holland|massey|claas"
                            r"|valtra|kubota|mccormick|lindner|steyr|landini|zetor|jcb|lamborghini|gmc|ram|ford"
                            r"|chevrolet|dodge)\b")
# "use the gmc to take them to the farm": the rest of the sentence, done with that vehicle.
_USE = re.compile(r"^(?:please\s+)?(?:and\s+)?use\s+(?:the|my|our)\s+(?P<name>[a-z0-9][a-z0-9-]{0,20}"
                  r"(?:\s+[a-z0-9][a-z0-9-]{0,20})?)"
                  # "use the 379 and the lowboy to ...": the transport it tows, by its word or name
                  r"(?:\s+(?:and|with)\s+(?:the|its|my|our)\s+(?P<with>[a-z0-9][a-z0-9 -]{0,30}?))?"
                  r"\s+to\s+(?P<rest>.+)$")


def hitch_named(words: str) -> dict | None:
    if _TOGETHER.match(words):
        return {"version": 1, "command": "hitch_pair", "together": True}
    it = _HITCH_IT.match(_DEUTZ.sub("deutz", words))
    if it is not None and not implement_kind(it["b"]) and not _TRAILER_WORDS.search(it["b"]) \
            and (re.search(r"[0-9]", it["b"]) or _VEHICLE_WORDS.search(it["b"])):
        name = re.sub(r"\s*\btractor\b\s*", " ", it["b"]).strip()
        return {"version": 1, "command": "hitch_pair", "together": True, **({"tractor_name": name} if name else {})}
    match = _HITCH_TO.match(_DEUTZ.sub("deutz", words))
    if match is None:
        return None
    # "hitch the edk 650 to the 3650": both by their store names (a model number each side).
    implement, tractor = match["a"], match["b"]
    if not implement_kind(implement) and not implement_kind(tractor) and not _TRAILER_WORDS.search(tractor) \
            and re.search(r"[0-9]", implement) and (re.search(r"[0-9]", tractor) or _VEHICLE_WORDS.search(tractor)) \
            and not re.search(r"\b(?:and|then|it|them)\b", implement + " " + tractor):
        name = re.sub(r"\s*\btractor\b\s*", " ", tractor).strip()
        return {"version": 1, "command": "hitch_pair", "implement_name": implement, **({"tractor_name": name} if name else {})}
    for implement, tractor in ((match["a"], match["b"]), (match["b"], match["a"])):
        kind = implement_kind(implement)
        if kind and not implement_kind(tractor) and not _TRAILER_WORDS.search(tractor) \
                and (_VEHICLE_WORDS.search(tractor) or re.search(r"[0-9]", tractor)) \
                and not re.search(r"\b(?:and|then|it|them)\b", implement):
            proposal = {"version": 1, "command": "hitch_pair", "implement": kind,
                        "tractor_name": re.sub(r"\s*\btractor\b\s*", " ", tractor).strip() or None}
            if not proposal["tractor_name"]:
                del proposal["tractor_name"]
            if model_name(implement):
                proposal["implement_name"] = model_name(implement)
            return proposal
    return None


# "unload the combine", "unload the harvester on field 3": its grain transferred into a trailer,
# nothing more (a helper harvesting: the field said, else the one being harvested).
_UNLOAD_COMBINE = re.compile(r"^(?:please\s+)?(?:go\s+(?:and\s+)?)?(?:unload|empty|dump)\s+(?:the|my|our)\s+(?:combines?|harvesters?)"
                             r"(?:\s+s)?(?:\s+(?:tank|grain|hopper))?(?:\s+(?:into|in)\s+(?:a|the)\s+(?:trailer|wagon|cart))?"
                             r"(?:\s+(?:on|in|at)\s+field\s+(?P<field>\d{1,5}))?(?:\s+please)?$")


def unload_combine(words: str) -> dict | None:
    match = _UNLOAD_COMBINE.match(words)
    if match is None:
        return None
    proposal = {"version": 1, "command": "unload_combine"}
    if match["field"]:
        proposal["field"] = int(match["field"])
    return proposal


def plant(words: str) -> dict | None:
    """What to plant: cultivate (skipped where the ground is ready) then sow the crop, or
    the preparation the player named first ("plow and plant canola on field 4")."""
    match = _PLANT.match(words)
    if match is None:
        return None
    crop = match["crop"] or match["crop2"]
    if set(crop.split()) & _NOT_CROPS:
        return None
    field = int(match["number"] or match["number2"])
    if field < 1:
        return None
    prep = _FIELD_VERBS[match["prep"]] if match["prep"] else "cultivate"
    if prep in ("sow", "fertilize"):
        return None
    return {"version": 1, "command": "field_work", "field": field, "operations": [prep, "sow"], "crop": crop}


_WITH = re.compile(r"\s+(?:with|using)\s+(?:the|my|our)\s+([a-z0-9][a-z0-9-]{0,20}(?:\s+[a-z0-9][a-z0-9-]{0,20})?)$")
_GOOSENECK = re.compile(r"\bgoosenecks?\b")
_DECKOVER = re.compile(r"\bdeckovers?\b")


def model_name(words: str) -> str | None:
    """A model number right before the implement's noun ("the 980 plow", "the
    vantage 4300 sprayer"): the one word with a digit in it, never a size or colour."""
    for pattern, _ in _IMPLEMENT_NOUNS:
        noun = pattern.search(words)
        if noun is not None:
            before = words[:noun.start()].split()
            if before and re.fullmatch(r"[a-z0-9-]*[0-9][a-z0-9-]*", before[-1]) and len(before[-1]) <= 20:
                return before[-1]
            return None
    return None


def _merge_field_work(steps: list) -> list:
    """Consecutive field work on the same field (whole field, no destination) as one job, its
    operations in the order said, the crop of its sowing."""
    out = []
    for step in steps:
        last = out[-1] if out else None
        if (last is not None and step["command"] == last["command"] == "field_work"
                and step["field"] == last["field"] and not ({"area_m2", "destination", "over_crop"} & (set(last) | set(step)))
                and not set(step["operations"]) & set(last["operations"])
                and not (step.get("crop") and last.get("crop"))):
            merged = {**last, "operations": last["operations"] + step["operations"]}
            if step.get("crop"):
                merged["crop"] = step["crop"]
            out[-1] = merged
        else:
            out.append(step)
    return out


class RuleInterpreter:
    """Deterministic wording rules for the supported commands."""

    def interpret(self, text: str) -> dict | None:
        if not isinstance(text, str):
            return None
        # Several commands in one go win over one reading of the whole sentence (live words
        # of the final acceptance request read as a single fetch); a clause not understood
        # leaves the whole sentence to the single reading ("buy a plow, the gmc truck ...").
        single = self._clause(text)
        if _CLAUSE_BREAK.search(text.casefold()):
            several = self._sequence(text)
            if several is not None and several["command"] != "sequence":
                return several                # clauses that make one command
            if several is not None and not (single is not None and {s["command"] for s in several["steps"]}
                                            <= _COVERS.get(single["command"], set())):
                return several
        return single

    def _clause(self, text: str) -> dict | None:
        """One command: the goal and reference rules first, then the plain rules."""
        words = normalize(text)
        parcel = land(words)
        if parcel is not None:
            return parcel
        use = _USE.match(words)
        if use is not None and not implement_kind(use["name"]):
            rest = self._clause(use["rest"])
            if rest is None or rest.get("command") not in ("move_pair", "haul_tractor", "fetch_pair", "hitch_pair",
                                                            "follow_me", "sell"):
                return None
            if rest["command"] == "haul_tractor":
                # "use the gmc to take the deutz to the farm": the truck whose transport carries it.
                haul = {**rest, "truck_name": use["name"]}
                if use["with"]:
                    if not _TRAILER_WORDS.search(use["with"]):
                        return None
                    # A kind word ("the lowboy", "the trailer") names nothing beyond the one the
                    # truck tows; a name ("the deckover", "the lizard trailer") names that one.
                    generic = re.fullmatch(r"(?:(?:a|the)\s+)?(?:trailers?|decks?|lowboys?|lowloaders?|flatbeds?"
                                           r"|low\s+loaders?)", use["with"])
                    if generic is None:
                        haul["implement_name"] = _trailer_name(use["with"])
                return haul
            if use["with"]:
                return None
            return {**rest, "tractor_name": use["name"]}
        if _RESUME.fullmatch(words):
            return {"version": 1, "command": "resume"}
        back = _BACK.fullmatch(_DEUTZ.sub("deutz", words))
        if back is not None:
            proposal = {"version": 1, "command": "move_pair", "target": {"type": "origin"}}
            thing = back.group("thing")
            if thing and (implement_kind(thing) or _TRAILER_WORDS.search(thing)):
                name = _trailer_name(thing) if _TRAILER_WORDS.search(thing) else None
                if name or model_name(thing):
                    proposal["implement_name"] = model_name(thing) or name
            elif thing and _vehicle_name(thing):
                proposal["tractor_name"] = _vehicle_name(thing)
            return proposal
        follow = _FOLLOW.fullmatch(_DEUTZ.sub("deutz", words))
        if follow is not None:
            proposal = {"version": 1, "command": "follow_me"}
            name = _vehicle_name(follow.group("a") or follow.group("b") or follow.group("c"))
            if name and not re.search(r"\b(?:me|us)\b", name):
                proposal["tractor_name"] = re.sub(r"\s*\b(?:truck|rig|pickup)\b\s*", " ", name).strip() or None
                if not proposal["tractor_name"]:
                    del proposal["tractor_name"]
            return proposal
        instead = _INSTEAD.match(words)
        if instead is not None:
            place = instead.group(1) or instead.group(2)
            target = self._target(place, place)
            if target and target != _EQUIPMENT_TARGET:
                return {"version": 1, "command": "retarget", **target}
            return None
        haul = self._haul(text)
        if haul is not None:
            return haul
        return self._interpret_one(text)

    def _haul(self, text: str) -> dict | None:
        """"Take the Deutz to field 3 and unload it": one goal (haul_tractor), the move part
        read like any move ("take the deutz to field 3"), its vehicle the tractor carried."""
        words = normalize(text)
        unload = _UNLOAD_TAIL.search(words)
        if unload is None:
            # "take the tractor to the farm": a tractor alone is taken on a transport (the
            # user, 2026-10-21: haul it on the Deckover), so it is the same haul.
            if not _TRACTOR_TO.match(words):
                return None
            end = len(words)
        else:
            end = unload.start()
        prefix = re.sub(r"\s+back(?=\s+to\b)", "", _DEUTZ.sub("deutz", words[:end]))   # "haul the 3650 back to the farm"
        truck = None
        on_truck = _ON_TRUCK.search(prefix)
        if on_truck is not None and unload is not None and not implement_kind(on_truck["truck"]) \
                and on_truck["truck"] not in ("it", "this", "that", "farm", "field", "road"):
            truck, prefix = on_truck["truck"], prefix[:on_truck.start()]
        move = self._interpret_one(prefix)
        if move is not None and move.get("command") == "unsupported" and move.get("reason") == "tractor_only":
            # "take the tractor to field 3 and unload it": the tractor on the transport.
            target = self._target(prefix, prefix)
            move = None if target is None or target == _EQUIPMENT_TARGET else {"command": "move_pair", **target}
        if move is None or move.get("command") != "move_pair" or "target" not in move and not re.search(
                r"\b(?:here|to me)\b", prefix):
            return None
        proposal = {"version": 1, "command": "haul_tractor"}
        if move.get("tractor_name"):
            proposal["tractor_name"] = move["tractor_name"]
        if truck:
            proposal["truck_name"] = truck
        if "target" in move:
            proposal["target"] = move["target"]
        return proposal

    def _sequence(self, text: str) -> dict | None:
        """Several job commands said in one go ("take the gmc, hitch the deckover, load the
        tractor, secure it, raise the ramps, deliver it to field 3, and unload it"): each
        clause is understood on its own, in order; "take the gmc" names the towing vehicle
        of the hitch that follows, belting and the ramps fold into the load, and names carry
        to the steps after them. Any clause not understood: no sequence."""
        clauses = [c.strip(" .!") for c in _CLAUSE_BREAK.split(text.casefold())]
        steps, towing, trailer, combine = [], None, None, False
        for clause in clauses:
            words = normalize(clause)
            if not words:
                continue
            if _COMBINE_SUBJECT.match(words):
                combine = True                # "it" is the combine from here on
                continue
            if combine and _UNLOAD_IT.match(words):
                steps.append({"version": 1, "command": "unload_combine"})
                continue
            if steps and steps[-1]["command"] == "haul_tractor" and _UNLOAD_IT.match(words):
                continue                      # a haul unloads where it delivers
            take = _TAKE_VEHICLE.match(words)
            if (take is not None and not _KNOWN_THING.search(take.group(1)) and implement_kind(take.group(1)) is None
                    # a vehicle only, never one with a destination ("take the deutz to the farm")
                    and not re.search(r"\b(?:to|into|onto|off|over|back|here|there)\b", take.group(1))):
                towing = take.group(1)
                continue
            if _ABSORBED.match(words):
                lowering = re.search(r"\b(?:lower|drop|down)\b", words)
                if not lowering and not any(s["command"] == "load_tractor" for s in steps):
                    return None               # securing or raising with nothing loaded
                continue
            field_it = _FIELD_IT.match(words)
            last_field = next((s for s in reversed(steps) if s["command"] == "field_work"), None)
            if field_it is not None and last_field is not None and last_field["field"] != "smallest":
                operation = _FIELD_VERBS[field_it["verb"]]
                step = {"version": 1, "command": "field_work", "field": last_field["field"], "operations": [operation]}
                if field_it["crop"]:
                    if operation != "sow" or set(field_it["crop"].split()) & _NOT_CROPS:
                        return None
                    step["crop"] = field_it["crop"]
                steps.append(step)
                continue
            step = self._clause(clause)
            if step is None or step["command"] in ("stop", "status", "settings", "report", "unsupported", "sequence",
                                                   "resume", "retarget"):
                return None
            if step["command"] in ("hitch_pair", "fetch_pair", "move_pair") and towing and "tractor_name" not in step:
                step["tractor_name"] = towing
            if step["command"] in ("hitch_pair", "fetch_pair") and step.get("implement_name"):
                trailer = step["implement_name"]
            if step["command"] in ("load_tractor", "unload_tractor") and trailer and "implement_name" not in step:
                step["implement_name"] = trailer
            steps.append(step)
        steps = _merge_field_work(steps)
        if len(steps) == 1 and (steps[0]["command"] == "field_work" or combine):
            return steps[0]                   # "plow field 3, seed it with wheat, then fertilize it": one job
        if not 2 <= len(steps) <= 8:
            return None
        return {"version": 1, "command": "sequence", "steps": steps}

    def _interpret_one(self, text: str) -> dict | None:
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
        if not _NEGATION.search(words) and contract(words):
            return contract(words)                    # "do a contract": an order, not a question
        if _NEGATION.search(words) or _QUESTION.match(words):
            return None                               # never an order to move
        if _COME.match(words):
            return {"version": 1, "command": "move_pair"}
        if re.fullmatch(r"(?:please\s+)?save(?:\s+the)?(?:\s+(?:game|farm|progress))?(?:\s+now)?", words):
            return {"version": 1, "command": "save_game"}
        work = (harvest_policy(words) or field_work(words) or plant(words) or sell(words) or store(words)
                or unload_combine(words) or hitch_named(words) or buy(words) or transport(words))
        if work is not None:
            return work
        # "... with the gmc": the towing vehicle by its name.
        towing = _WITH.search(words)
        if towing is not None and (_KNOWN_THING.search(towing.group(1)) or implement_kind(towing.group(1))):
            towing = None                             # "the tractor with the loader wagon": equipment, not a name
        if towing is not None:
            words = words[:towing.start()].strip()
            text = re.sub(r"\s+(?:with|using)\s+(?:the|my|our)\s+\S+(?:\s+\S+)?\s*[.!?]*$", "", text.strip(),
                          flags=re.IGNORECASE)
        proposal = self._interpret_rest(text, words)
        model = model_name(words)
        if proposal is not None and proposal.get("command") not in ("fetch_pair", "hitch_pair"):
            model = None                              # a model number only picks among candidates
        named_kind = "gooseneck" if _GOOSENECK.search(words) else "deckover" if _DECKOVER.search(words) else None
        if proposal is None or (towing is None and named_kind is None and model is None):
            return proposal
        if proposal.get("command") not in ("fetch_pair", "hitch_pair"):
            return None                               # a name only where a pair is chosen
        if towing is not None:
            proposal["tractor_name"] = towing.group(1)
        if model is not None:
            proposal["implement_name"] = model         # "the 980 plow", "the 1124 deckover": that one
        elif named_kind is not None:
            proposal["implement_name"] = named_kind
        return proposal

    def _interpret_rest(self, text: str, words: str) -> dict | None:
        kind = implement_kind(words)
        if _DETACH_VERB.match(words) or _DROP.match(words) or _TAKE_OFF.match(words):
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
            # "move the gmc here", "bring the gmc to field 3": the rig that vehicle tows
            # (move_pair filtered by its name; nothing else is chosen). Anything else by
            # a name is not supported yet; never guessed.
            if (_MOVE_VERB.match(words) and target is not None
                    and (target or re.search(r"\b(?:here|to me|over here)\b", words))):
                return {"version": 1, "command": "move_pair", "tractor_name": named.group(1), **target}
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
