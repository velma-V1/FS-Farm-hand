"""One job at a time for the player; STOP and STATUS never wait for it.

Every way of asking (the fixed key, typed text, later speech through a local
model) ends here as a command proposal (protocol/command.schema.json). A proposal
carries no authority: this module adds the request id and farm, and the executor
validates the resulting intent and selects all equipment. STOP is deterministic
and immediate: it marks the job cancelled (no further step starts) and asks the
game to stop the reserved tractor, which pre-empts the running operation.

Replies are notice tokens shown in game (CommandLink.notice, mod/FarmhandBridge.lua
Bridge.noticeText). No simulator import; no game calls except through the World.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time

from jsonschema import Draft202012Validator

from adapters.world import StepFailed, StepUnverified
from companion import hitch as transport   # its tractor-transport section
from companion.executor import (COVERAGE_REQUIRED, Executor, OPERATIONS, already_done, crop_problem, named, of_kind,
                                resolve_field, seed_position, usable)
from protocol.model import Equipment, Result

log = logging.getLogger("farmhand")
COMMAND_SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "protocol/command.schema.json").read_text())
COMMAND_VALIDATOR = Draft202012Validator(COMMAND_SCHEMA)
# A proposal the key always produces: no interpretation involved.
FETCH_HERE = {"version": 1, "command": "fetch_pair"}


def token(text: str) -> str:
    """A notice token (protocol.ipc encode_session)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)[:64]


# Steps worth telling the player about while a job works (WORKING_<step>); the
# rest is visible on request (STATUS_WORKING_<step>).
# Steps that move equipment: STATUS says "driving" only when it is observed moving.
MOTION_STEPS = frozenset({"TRAVEL_NEAR", "STAGE", "LOCAL_MANEUVER", "ALIGN_REVERSE", "PULL_CLEAR", "UNPARK",
                          "DELIVER", "FIELD_WORK", "APPROACH", "LOAD", "UNLOAD", "FOLLOW"})
MILESTONES = frozenset({"FIELD_WORK", "PULL_CLEAR", "TRAVEL_NEAR", "STAGE", "LOCAL_MANEUVER", "DELIVER",
                        "RAMPS_DOWN", "LOAD", "BELTS", "RAMPS_UP", "UNLOAD"})
# The executor task and the player-facing notices of each job command.
JOBS = {"fetch_pair": ("deliver_tractor_and_forage_wagon", "ACCEPTED", "COMPLETE"),
        "move_pair": ("move_pair", "ACCEPTED_MOVE", "COMPLETE_MOVE"),
        "hitch_pair": ("hitch_pair", "ACCEPTED_HITCH", "COMPLETE_HITCH"),
        "detach_pair": ("detach_pair", "ACCEPTED_DETACH", "COMPLETE_DETACH"),
        "field_work": ("field_work", "ACCEPTED_FIELD_WORK", "COMPLETE_FIELD_WORK"),
        "buy": ("buy", "ACCEPTED_BUY", "COMPLETE_BUY"),
        "save_game": ("save_game", "ACCEPTED_SAVE", "COMPLETE_SAVE"),
        "load_tractor": ("load_tractor", "ACCEPTED_LOAD", "COMPLETE_LOAD"),
        "unload_tractor": ("unload_tractor", "ACCEPTED_UNLOAD", "COMPLETE_UNLOAD"),
        "sequence": ("sequence", "ACCEPTED_SEQUENCE", "COMPLETE_SEQUENCE"),
        "haul_tractor": ("haul_tractor", "ACCEPTED_HAUL", "COMPLETE_HAUL"),
        "follow_me": ("follow_me", "ACCEPTED_FOLLOW", "COMPLETE_FOLLOW"),
        "contract": ("contract", "ACCEPTED_CONTRACT", "COMPLETE_CONTRACT"),
        "worker_return": ("worker_return", "ACCEPTED_RETURN", "COMPLETE_RETURN"),
        "sell": ("sell", "ACCEPTED_SELL", "COMPLETE_SELL"),
        "store": ("store", "ACCEPTED_STORE", "COMPLETE_STORE"),
        "unload_combine": ("unload_harvester", "ACCEPTED_UNLOAD_COMBINE", "COMPLETE_UNLOAD_COMBINE"),
        "combine_full": ("overload", "COMBINE_FULL", "COMPLETE_UNLOAD_COMBINE")}
# The proposal fields that pass into a job command's intent.
NAMES = ("tractor_name", "implement_name", "cargo_name")


def result_notice(status: str, reason: str, complete: str = "COMPLETE") -> str:
    if status == "COMPLETE" and reason == "WORKERS_STARTED":
        return "WORKERS_STARTED"                  # the helpers work on; Farmhand is free
    if status == "COMPLETE":
        # Unhitched, but the tractor could not pull clear (nothing moved): say so.
        return complete + "_NOT_CLEAR" if reason == "DETACHED_NOT_CLEAR" else complete
    if status == "CANCELLED":
        return status
    return token(f"{status}_{reason}")


def owned_like(items: tuple[Equipment, ...], farm_id: str, wants) -> int:
    """How many of the farm's vehicles answer these wants (by name or brand words, or a
    plow by kind): a purchase step's before-and-after measure."""
    count = 0
    for item in items:
        if item.farm_id != farm_id:
            continue
        for want in wants:
            if want.get("name") and named(item, want["name"].casefold()):
                count += 1
            elif not want.get("name") and want["kind"] in ("plow", "spreader", "sprayer") \
                    and of_kind(item, "plow" if want["kind"] == "plow" else "sprayer"):
                count += 1
    return count


def combine_full(item: Equipment, share: float) -> bool:
    """A combine (with a pipe) whose grain tank is at least ``share`` full."""
    if item.category != "harvester" or item.pipe is None:
        return False
    grain = [u for u in item.fill_units if (u.fill_type or "UNKNOWN") not in Executor.NOT_CARGO and u.capacity]
    return bool(grain) and sum(u.level or 0.0 for u in grain) >= share * sum(u.capacity for u in grain)


# ------------------------------------------------------------- sequence context
# What "it" is in a sequence: the tractor a transport step worked with (by its store name,
# so a reload's new IDs do not matter), the rig carrying it, and what was just bought.
def with_context(step: dict, context: dict) -> dict:
    step = dict(step)
    if step.get("together"):
        # "hitch them together": the tractor and the implement just bought.
        bought = context.get("bought", {})
        # "hitch it to the 3650": the implement just bought, to that tractor.
        implement = next((k for k in bought if k not in ("tractor", "truck", "trailer")), None)
        if implement and ("tractor" in bought or step.get("tractor_name")):
            step.pop("together")
            step["implement"] = "sprayer" if implement == "spreader" else implement   # GIANTS' Sprayer spreads too
            if bought.get("tractor") and not step.get("tractor_name"):
                step["tractor_name"] = bought["tractor"]
            if bought[implement]:
                step["implement_name"] = bought[implement]
    if step["command"] == "load_tractor" and not step.get("carry") and context.get("hitched"):
        # "load them": the tractor with the implement hitched to it in this sequence.
        step["carry"] = context["hitched"]["kind"]
        if context["hitched"].get("name"):
            step["carry_name"] = context["hitched"]["name"]
    tractor = context.get("tractor")
    if tractor and step["command"] in ("haul_tractor", "load_tractor", "unload_tractor") and not step.get("tractor_name"):
        step["tractor_name"] = tractor
    if tractor and step["command"] == "move_pair" and not step.get("tractor_name") and not step.get("cargo_name") \
            and context.get("loaded"):
        step["cargo_name"] = tractor                      # "bring it back", "deliver it": the rig carrying it
    if step.get("carry") and not step.get("carry_name"):
        kind = _carry_kind(step["carry"])
        if kind and context.get("bought", {}).get(kind):
            step["carry_name"] = context["bought"][kind]  # "load the tractor and plow": the plow just bought
    return step


def remember(context: dict, step: dict, result: Result, items) -> None:
    observed = {i.id: i for i in items}
    if step["command"] in ("haul_tractor", "load_tractor", "unload_tractor") and result.equipment_ids:
        # The carried tractor, never the truck towing the transport (a resumed haul's first
        # unit may be the truck).
        cargo = [observed[i] for i in result.equipment_ids if i in observed and observed[i].category == "tractor"
                 and not transport.is_transport(observed[i])
                 and not (observed.get(observed[i].attached_to) is not None
                          and transport.is_transport(observed[observed[i].attached_to]))]
        if cargo and _words(cargo[0].name):
            context["tractor"] = _words(cargo[0].name)
        context["loaded"] = step["command"] == "load_tractor"
    elif step["command"] == "buy":
        for want in step["wants"]:
            context.setdefault("bought", {})[want["kind"]] = _words(want["name"]) if want.get("name") else ""
    elif step["command"] == "hitch_pair" and step.get("implement") and len(result.equipment_ids) == 2:
        tractor = observed.get(result.equipment_ids[0])
        if tractor is not None and _words(tractor.name):
            context["tractor"] = _words(tractor.name)
        context["hitched"] = {"kind": step["implement"], "name": step.get("implement_name")}


def _carry_kind(words: str) -> str | None:
    from companion.language import implement_kind
    return implement_kind(words) or ("plow" if re.search(r"\bplows?\b|\bploughs?\b", words or "") else None)


def _tractor_for(items, farm_id: str, name: str | None, transports):
    """The tractor a transport goal means: named, else the one on a transport, else the free
    tractor nearest a transport (within 50 m; "the tractor" beside the deckover)."""
    towing = {t.attached_to for t in transports}          # the truck towing a transport is never it
    if name:
        found = [i for i in items if i.category == "tractor" and i.farm_id == farm_id and named(i, name)
                 and not transport.is_transport(i) and i.id not in towing]
    else:
        found = [i for i in items if i.category == "tractor" and i.farm_id == farm_id and i.id not in towing
                 and any(i.belted_to == t.id or transport.on_deck(i, t) for t in transports)]
        if not found:
            near = sorted((min(math.dist(i.position, t.position) for t in transports), i.id, i) for i in items
                          if i.category == "tractor" and i.farm_id == farm_id and i.id not in towing and transports
                          and not transport.is_transport(i))
            found = [near[0][2]] if near and near[0][0] <= 50.0 else []
    return found


def work_for(proposal: dict) -> dict | None:
    """What a job command carries into its run, the same whether said alone or as a step of a
    sequence (one dispatch, Coordinator._dispatch)."""
    command = proposal["command"]
    if command == "field_work":
        return {"field": proposal["field"], "operations": proposal["operations"],
                "area_m2": proposal.get("area_m2"), "over_crop": proposal.get("over_crop", False),
                "crop": proposal.get("crop"), "destination": proposal.get("destination")}
    if command == "buy":
        return {"wants": proposal["wants"]}
    if command in ("fetch_pair", "hitch_pair", "move_pair", "load_tractor", "unload_tractor", "follow_me", "sell",
                   "store"):
        work = {k: proposal[k] for k in NAMES if k in proposal}
        if command == "load_tractor" and proposal.get("carry"):
            work["carry"] = proposal["carry"]
        return work or None
    if command == "sequence":
        return {"steps": proposal["steps"]}
    if command == "haul_tractor":
        work = {"tractor_name": proposal.get("tractor_name"), "target": proposal.get("target", "here"),
                "truck_name": proposal.get("truck_name")}
        if proposal.get("implement_name"):     # "... and the lowboy": that transport, kept
            work["transport"] = proposal["implement_name"]
        return work
    if command == "contract":
        return {"field": proposal.get("field"), "kind": proposal.get("kind")}
    if command == "unload_combine":
        return {"field": proposal.get("field")}
    return None


def load_with_step(items, farm_id: str, work: dict):
    """The next step of "load the tractor and plow": (intent fragment, None) or (None, why)."""
    observed = {i.id: i for i in items}
    transports = [i for i in items if transport.is_transport(i) and i.farm_id == farm_id]
    tractors = _tractor_for(items, farm_id, work.get("tractor_name"), transports)
    if len(tractors) != 1:
        return None, "NO_SUCH_TRACTOR" if not tractors else "TRACTOR_AMBIGUOUS"
    tractor = tractors[0]
    kind = _carry_kind(work.get("carry"))
    if kind is None:
        return None, "UNKNOWN_IMPLEMENT"
    words = work.get("tractor_name") or _words(tractor.name)
    implement = observed.get(tractor.attached_to)
    carrying = implement is not None and of_kind(implement, kind) and named(implement, work.get("carry_name"))
    aboard = tractor.belted_to is not None or any(transport.on_deck(tractor, t) or transport.on_ramps(tractor, t)
                                                  for t in transports)
    if carrying and aboard and tractor.belted_to is not None:
        return None, "LOADED"
    if carrying:
        step = {"task": "load_tractor", "tractor_name": words}
        if work.get("implement_name"):
            step["implement_name"] = work["implement_name"]
        return step, None
    if aboard:
        return {"task": "unload_tractor", "tractor_name": words}, None
    if implement is not None:
        return None, "TRACTOR_CARRIES_SOMETHING_ELSE"
    step = {"task": "hitch_pair", "implement": kind, "tractor_name": words}
    if work.get("carry_name"):
        step["implement_name"] = work["carry_name"]
    return step, None


# ------------------------------------------------------------- haul planning
def _words(name: str | None) -> str | None:
    """A store name as intent words (lower case, letters, digits, spaces, hyphens)."""
    words = re.sub(r"[^a-z0-9 -]+", " ", (name or "").casefold()).split()
    return " ".join(words)[:40].strip() or None


def haul_step(items: tuple[Equipment, ...], farm_id: str, name: str | None, target, delivered: bool,
              staged: bool = True, transport_name: str | None = None, truck_name: str | None = None):
    """The next executor step of a haul goal from observed state, as an intent fragment,
    or (None, why): HAULED when it is done; NO_SUCH_TRACTOR, TRACTOR_AMBIGUOUS,
    NO_TRANSPORT when it cannot go on. Not yet ``staged``: before the load, the empty
    transport is taken where AutoDrive has a route to the target (stage_transport). One
    transport throughout (live 2026-10-22: with two on the farm the load took the lowboy nearest
    the tractor, not the Deckover just staged): the one named (``transport_name``, kept by the
    goal once chosen), else the one the named truck tows, else the one nearest the tractor."""
    observed = {i.id: i for i in items}
    transports = [i for i in items if transport.is_transport(i) and i.farm_id == farm_id]
    tractors = _tractor_for(items, farm_id, name, transports)
    if not tractors:
        return None, "NO_SUCH_TRACTOR"
    if len(tractors) > 1:
        return None, "TRACTOR_AMBIGUOUS"
    tractor = tractors[0]
    trailer = observed.get(tractor.belted_to) or next((t for t in transports if transport.on_deck(tractor, t)), None)
    words = name or _words(tractor.name)
    destination = {} if target in (None, "here") else {"target": target}
    if delivered:
        if trailer is None:
            return None, "HAULED"
        return {"task": "unload_tractor", "target": "player_at_acceptance", "tractor_name": words}, None
    if trailer is not None and tractor.belted_to == trailer.id and trailer.fold == 0.0 \
            and observed.get(trailer.attached_to) is not None:
        return {"task": "move_pair", "target": "player_at_acceptance", **destination, "cargo_name": words}, None
    if trailer is not None:                 # aboard, not yet belted or the ramps not up: finish it
        return {"task": "load_tractor", "target": "player_at_acceptance", "tractor_name": words,
                "implement_name": _words(trailer.name)}, None
    hitched = [t for t in transports if observed.get(t.attached_to) is not None]
    named_one = bool(transport_name or truck_name)
    if transport_name:
        hitched = [t for t in hitched if named(t, transport_name)]
    elif truck_name:
        hitched = [t for t in hitched if named(observed[t.attached_to], truck_name)]
    # Feasibility before distance: a transport already carrying something, or whose deck the
    # rig does not fit (GIANTS' size boxes; unknown sizes rank after known fits), is not taken.
    implement = observed.get(tractor.attached_to)
    implement = implement if implement is not None and implement.attached_to == tractor.id else None
    feasible = [t for t in hitched if not transport.carrying(t, items)
                and transport.fits(tractor, implement, t) is not False]
    if hitched and not feasible and named_one:
        # The one the player named is kept, and why it cannot take the load is said.
        return None, ("TRANSPORT_LOADED" if all(transport.carrying(t, items) for t in hitched)
                      else "TRANSPORT_TOO_SMALL")
    hitched = feasible
    hitched.sort(key=lambda t: (transport.fits(tractor, implement, t) is None,
                                math.dist(t.position, tractor.position), t.id))
    which = {"implement_name": _words(hitched[0].name)} if hitched and _words(hitched[0].name) else {}
    if hitched and not staged and destination:
        return {"task": "stage_transport", **destination, **which}, None
    if hitched:
        return {"task": "load_tractor", "target": "player_at_acceptance", "tractor_name": words, **which}, None
    free = sorted((t for t in transports if observed.get(t.attached_to) is None and not transport.carrying(t, items)
                   and transport.fits(tractor, implement, t) is not False),
                  key=lambda t: (transport.fits(tractor, implement, t) is None,
                                 math.dist(t.position, tractor.position), t.id))
    if free:
        return {"task": "hitch_pair", "target": "player_at_acceptance", "implement": "trailer",
                "implement_name": _words(free[0].name)}, None
    return None, "NO_FREE_TRANSPORT" if transports else "NO_TRANSPORT"


# ------------------------------------------------------------- field planning
# A field-work goal as executor steps, one operation at a time, from fresh state. The
# planner decides only what happens next; the executor selects, validates and
# verifies every step. For each operation: hitch an implement that does it (unless one
# is already hitched to a tractor), work the field, then unhitch it where it stands
# (PULL_CLEAR included) so the next operation's tool change has a tractor.
def implement_kind(items: tuple[Equipment, ...], operation: str, farm_id: str,
                   crop: str | None = None) -> tuple[str | None, str | None]:
    """The implement kind to use for ``operation``: the first of its kinds the farm
    has (owned, available); a cultivator stands in for a plow only when the farm has
    no plow (said in the note). None and why when the farm has none; with ``crop``,
    a seeder that can sow it."""
    if crop is not None and not any(of_kind(i, "sowing_machine") and usable(i, farm_id) and "ai_implement" in i.kinds
                                    and seed_position(i, crop) is not None for i in items):
        return None, "NO_SEEDER_FOR_CROP"
    kinds = OPERATIONS[operation].kinds
    if operation == "plow" and not _have(items, "plow", farm_id):
        kinds = OPERATIONS["cultivate"].kinds
        if _have(items, "cultivator", farm_id):
            return "cultivator", "NO_PLOW_CULTIVATING"
    for kind in kinds:
        if _have(items, kind, farm_id):
            return kind, None
    return None, "NO_IMPLEMENT_FOR_" + operation.upper()


def _have(items, kind, farm_id) -> bool:
    return any(of_kind(i, kind) and usable(i, farm_id) and "ai_implement" in i.kinds for i in items)


def _hitched(items, kind, crop=None) -> bool:
    observed = {i.id: i for i in items}
    return any(i.category == "tractor" and observed.get(i.attached_to) is not None
               and of_kind(observed[i.attached_to], kind)
               and (crop is None or seed_position(observed[i.attached_to], crop) is not None) for i in items)


FREE_AHEAD_M, FREE_SIDE_M = 10.0, 3.0   # PULL_CLEAR's way out after the unhitch: nothing there


def _boxed_ahead(tractor: Equipment, items) -> bool:
    """Something stands in the way straight ahead of the tractor (live 6f538eb5c4f641fc-1: the
    Series 9 unhitched its plow facing the Deckover's ramps and could not pull clear)."""
    if tractor.heading is None:
        return True
    fx, fz = math.sin(tractor.heading), math.cos(tractor.heading)
    for other in items:
        if other.id in (tractor.id, tractor.attached_to) or other.attached_to == tractor.id:
            continue
        dx, dz = other.position[0] - tractor.position[0], other.position[1] - tractor.position[1]
        along, side = dx * fx + dz * fz, dx * fz - dz * fx
        # Its size box, not its root (live dfbef3dc652f4624-1: the Deckover's root 13 m ahead,
        # its ramps 6 m): any part of it within the way out.
        half_length, half_width = ((other.size[0] / 2, other.size[1] / 2) if other.size else (0.0, 0.0))
        reach = max(half_length, half_width)
        if along + reach > 0.0 and along - reach <= FREE_AHEAD_M and abs(side) - reach <= FREE_SIDE_M:
            return True
    return False


def _tractor_to_free(items, kind: str, farm_id: str) -> str | None:
    """With no free tractor: the store name of the tractor nearest an implement of ``kind``
    whose own hitched implement is an ordinary one (not a transport, not of that kind), so it
    can be unhitched where it stands; None when a tractor is free or none qualifies."""
    tractors = [i for i in items if i.category == "tractor" and usable(i, farm_id) and i.stopped
                and {"move", "attach", "stop"} <= i.capabilities]
    if any(t.attached_to is None for t in tractors):
        return None
    observed = {i.id: i for i in items}
    wanted = [i for i in items if of_kind(i, kind) and usable(i, farm_id) and i.attached_to is None]
    choices = []
    for t in tractors:
        held = observed.get(t.attached_to)
        if (held is None or held.attached_to != t.id or not usable(held, farm_id) or of_kind(held, kind)
                or transport.is_transport(held) or held.belted or not t.name or _boxed_ahead(t, items)):
            continue
        # A joint the implement can go on (a truck has no three-point joint for a spreader).
        types = {o.joint_type.casefold() for o in t.output_joints}
        if not any(j.joint_type.casefold() in types for w in wanted for j in w.input_joints):
            continue
        near = min((math.dist(t.position, w.position) for w in wanted), default=math.inf)
        choices.append((near, t.id, t))
    if not choices or not wanted:
        return None
    return _words(min(choices, key=lambda c: (c[0], c[1]))[2].name)


def field_steps(items: tuple[Equipment, ...], operation: str, field, farm_id: str, area_m2=None, crop=None):
    """The steps for one operation on ``field`` (an id or "smallest"), as executor
    intent fragments, or (None, why). ``crop`` (sowing only): the player's crop."""
    crop = crop if operation == "sow" else None
    if operation == "harvest":
        # A combine with its header on, free: harvested where it stands ready; it is not
        # unhitched or brought back (the player decides where it goes).
        observed = {i.id: i for i in items}
        if not any(i.category == "harvester" and usable(i, farm_id) and observed.get(i.attached_to) is not None
                   and of_kind(observed[i.attached_to], "cutter") for i in items):
            return None, "NO_HARVESTER_WITH_HEADER"
        return [{"task": "field_work", "field": field, "operation": "harvest", "implement": "cutter",
                 **({"area_m2": area_m2} if area_m2 else {})}], None
    kind, note = implement_kind(items, operation, farm_id, crop)
    if kind is None:
        return None, note
    work_op = "cultivate" if note == "NO_PLOW_CULTIVATING" else operation
    with_crop = {"crop": crop} if crop else {}
    steps = []
    if not _hitched(items, kind, crop):
        freed = _tractor_to_free(items, kind, farm_id)
        if freed is not None:
            # Every tractor has something hitched (live 7dada469b72f44ed-3, a contract taken):
            # the one nearest the implement needed leaves what it has where it stands.
            steps.append({"task": "detach_pair", "tractor_name": freed})
            steps.append({"task": "hitch_pair", "implement": kind, "tractor_name": freed, **with_crop})
        else:
            steps.append({"task": "hitch_pair", "implement": kind, **with_crop})
    steps.append({"task": "field_work", "field": field, "operation": work_op, "implement": kind,
                  **({"area_m2": area_m2} if area_m2 else {}), **with_crop})
    # Back where it was taken from, then unhitched there: never left in the field.
    steps.append({"task": "move_pair", "implement": kind, "target": {"type": "origin"}})
    steps.append({"task": "detach_pair", "implement": kind})
    return steps, note


@dataclass
class Job:
    number: int
    owner: str                    # the executor request id: reservation owner
    state: str = "WORKING"        # WORKING, then COMPLETE/BLOCKED/FAILED/UNVERIFIED/CANCELLED
    step: str | None = None
    reason: str | None = None
    spoken: bool = False          # asked by voice: replies are spoken too
    stop_sent: bool = False       # at most one requester stop per job
    states: tuple = ()            # the executor's result: states entered, equipment used
    equipment: tuple = ()
    command: str | None = None    # the job command (JOBS key)


# Goals that are kept when they do not complete (stopped, taken over, blocked, failed):
# "continue" re-plans them from the observed state; their ``work`` carries the progress
# (a haul's observed delivery, a sequence's completed steps), so nothing done repeats.
GOALS = frozenset({"haul_tractor", "sequence", "contract", "combine_full"})


# GIANTS' contract types Farmhand works (by the start of their registered name) -> the
# field operation. Others (sowing: its crop is not readable; harvest, bales, ...) are not taken.
CONTRACT_WORK = (("plow", "plow"), ("cultiv", "cultivate"), ("fertili", "fertilize"))


def contract_operation(kind: str) -> str | None:
    return next((op for stem, op in CONTRACT_WORK if kind.casefold().startswith(stem)), None)


def choose_contract(contracts, items, farm_id: str, field=None, kind=None):
    """The contract to work: this farm's own first (finish what was taken), else the best
    paid available one Farmhand can do with the farm's implements, on ``field`` and of
    ``kind`` (a type-name start, "plow") when the player said so. (contract, None) or
    (None, why)."""
    def fits(c):
        return (field is None or c.field == field) and (kind is None or c.kind.casefold().startswith(kind))

    mine = [c for c in contracts if c.mine and fits(c)]
    if mine:
        return mine[0], None
    offers = [c for c in contracts if c.status == "CREATED" and fits(c)]
    if not offers:
        return None, "NO_CONTRACT_ON_FIELD" if field is not None else "NO_CONTRACT"
    workable = [c for c in offers if contract_operation(c.kind)]
    if not workable:
        return None, "CONTRACT_NOT_SUPPORTED"
    doable = [c for c in workable if implement_kind(items, contract_operation(c.kind), farm_id)[0] is not None]
    if not doable:
        return None, implement_kind(items, contract_operation(workable[0].kind), farm_id)[1]
    return max(doable, key=lambda c: (c.reward or 0.0, -c.field)), None


@dataclass
class Worker:
    """A field job handed to GIANTS' helper (the user 2026-10-21: workers go on while
    Farmhand does something else). Its pair stays reserved under ``owner`` until the
    helper is seen to end; then the field is measured and ``after`` (bring the
    implement back, unhitch it) runs as soon as Farmhand is free."""
    owner: str
    tractor: str
    field: int
    operation: str
    after: list
    destination: str | None = None    # harvest: where the crop goes, as the request said it (else the saved policy)


# A next operation on the same field starts once the one ahead shows this share done
# (the seeder behind the plow, not ahead of it).
HEAD_START = 0.3


@dataclass
class Kept:
    command: str
    target: object
    implement: str | None
    work: dict


class Coordinator:
    """``world`` is the FS25World; ``executor`` an Executor over it; ``notify(code,
    spoken)`` shows a token in game (and speaks it when the request was spoken); ``farm`` returns the linked farm id, or raises
    StepUnverified when the game is not linked to this companion."""

    def __init__(self, world, executor, notify, farm, session, goal_file: Path | None = None, policy=None):
        self.world, self.executor, self.notify = world, executor, notify
        # The player's saved choices (companion/ai.py Settings): where harvested crop goes
        # unless the request says so ("from now on sell the harvest"); None: nothing saved.
        self.policy = policy
        self.farm, self.session = farm, session
        # The kept goal survives a game reload or a companion restart: "continue" re-plans it
        # from the observed world (equipment by name and brand; session IDs change).
        self.goal_file = goal_file
        self._lock = threading.Lock()
        self._jobs = 0
        self.current: Job | None = None
        self.last: Job | None = None
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        # The request id of the executor job running now: a plan's step reserves under its
        # own id, and STOP must pre-empt that reservation's running bridge operation.
        self.running: str | None = None
        self.kept: Kept | None = self._load_goal()  # the goal "continue" resumes
        self._then = None                         # a destination to resume with once this job ends
        self.workers: list[Worker] = []           # GIANTS' helpers working on (handed off)
        self._harvest_rigs: dict[int, str] = {}   # field -> the rig (tractor words) its grain was transferred into
        self._after: list[list] = []              # their follow-up steps, run when Farmhand is free
        self._watcher: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self.current is not None

    def submit(self, proposal, spoken: bool = False) -> Job | None:
        """Answer one proposal now; a job runs on its own thread and is returned
        (None when none started). ``spoken``: the request was spoken, so this answer
        (and that job's notices) are too."""
        if proposal is None or not COMMAND_VALIDATOR.is_valid(proposal):
            self.notify("NOT_UNDERSTOOD", spoken)
            return None
        command = proposal["command"]
        if command == "stop":
            self.stop(spoken)
        elif command == "status":
            self.notify(self.status(), spoken)
        elif command == "unsupported":                 # understood: say exactly what is missing
            self.notify(token(f"UNSUPPORTED_{proposal['reason'].upper()}"), spoken)
        elif command == "resume":
            return self.resume(spoken)
        elif proposal.get("together"):                 # "hitch them together" alone: which ones?
            self.notify("NOTHING_TO_HITCH_TOGETHER", spoken)
        elif command == "retarget":
            return self.retarget(proposal["target"], spoken)
        elif command == "harvest_policy":
            self.notify(self._save_policy(proposal["destination"]), spoken)
        else:
            return self.start(command, proposal.get("target", "here"), spoken, proposal.get("implement"),
                              work_for(proposal))
        return None

    # ------------------------------------------------------------------ policy
    def _save_policy(self, destination: str) -> str:
        """The player's saved choice of where harvested crop goes (sell, store, keep)."""
        if self.policy is None:
            return "POLICY_UNAVAILABLE"
        try:
            self.policy.set_harvest_destination(destination)
        except OSError as error:
            log.info("harvest policy not saved: %s", error)
            return "POLICY_NOT_SAVED"
        log.info("harvest policy: %s", destination)
        return token(f"HARVEST_POLICY_{destination.upper()}")

    def _destination(self, said: str | None) -> str | None:
        """Where harvested crop goes: what the request said, else the saved policy; None
        (keep it in the trailer) unless one of them says sell or store. Never assumed."""
        if said in ("sell", "store"):
            return said
        saved = self.policy.harvest_destination() if self.policy is not None else None
        return saved if saved in ("sell", "store") else None

    # ------------------------------------------------------------------ status
    def status(self) -> str:
        with self._lock:
            job, last = self.current, self.last
        if job is not None:
            step = job.step or "FIND"
            if step in MOTION_STEPS and not self._moving():
                return token(f"STATUS_WAITING_{step}")    # a pending command is not motion
            return token(f"STATUS_WORKING_{step}")
        if self.kept is not None:
            return "STATUS_PAUSED"
        if self.workers:
            return "STATUS_HELPERS_WORKING"
        if last is not None:
            return token(f"STATUS_LAST_{last.state}")
        return "STATUS_IDLE"

    def _moving(self) -> bool:
        """Whether a unit the running step reserved is observed moving now."""
        ids = set(getattr(self.executor, "active", ()) or ())
        if not ids:
            return False
        try:
            return any(item.id in ids and not item.stopped for item in self.world.snapshot())
        except Exception:                          # unobserved is not moving
            return False

    # -------------------------------------------------------------------- stop
    def stop(self, spoken: bool = False) -> None:
        """Deterministic and immediate; never waits for an interpreter or the job.
        One requester stop per job, never during its own cleanup: a second stop
        command would pre-empt the job's confirming stop."""
        with self._lock:
            job = self.current
            send = False
            if job is not None:
                self._cancel.set()
                send = not job.stop_sent and job.step not in ("STOP", "RELEASE")
                job.stop_sent = job.stop_sent or send
        # STOP stops everything Farmhand started: the job and every helper working on.
        workers = self._stop_workers()
        if job is None:
            retained = [o for o in self.world.retained() if o not in {w.owner for w in workers}]
            self.notify("STOPPING" if retained or workers else "NOTHING_TO_STOP", spoken)
            for owner in retained:                 # an earlier job's unconfirmed stop
                threading.Thread(target=self.world.recover, args=(owner,), daemon=True).start()
            return
        self.notify("STOPPING", spoken)
        if send:
            threading.Thread(target=self._stop_reserved, args=(self.running or job.owner,), daemon=True,
                             name="farmhand-stop").start()

    # ------------------------------------------------------------------ workers
    WATCH_S = 5.0

    def _worker_owners(self) -> set:
        with self._lock:
            return {w.owner for w in self.workers}

    def _stop_workers(self) -> list:
        """Every handed-off helper stopped (its job ended through the game) and released
        once stopped; nothing is brought back afterwards (the player decides)."""
        with self._lock:
            workers, self.workers, self._after = list(self.workers), [], []
        for w in workers:
            def end(w=w):
                try:
                    if self.world.stop(w.tractor, w.owner):
                        self.world.release(w.owner)
                except (StepFailed, StepUnverified, ValueError) as error:
                    log.info("worker %s stop: %s (kept reserved)", w.tractor, getattr(error, "code", error))
            threading.Thread(target=end, daemon=True, name="farmhand-stop-worker").start()
        return workers

    def _add_worker(self, worker: Worker) -> None:
        with self._lock:
            self.workers.append(worker)
            start = self._watcher is None or not self._watcher.is_alive()
            if start:
                self._watcher = threading.Thread(target=self._watch, daemon=True, name="farmhand-workers")
        if start:
            self._watcher.start()

    def _watch(self) -> None:
        """Until no helper works and no follow-up waits: each helper seen ended (no AI
        job, at rest) has its field measured, is stopped and released, and its follow-up
        steps run as a job once Farmhand is free."""
        while True:
            with self._lock:
                workers, waiting = list(self.workers), bool(self._after)
            if not workers and not waiting:
                return
            observed = {i.id: i for i in self.world.snapshot()}
            for w in workers:
                tractor = observed.get(w.tractor)
                if tractor is None:
                    continue
                # GIANTS' combine helper waits at a full tank for a trailer under the pipe
                # (AIDriveStrategyCombine:getDriveData 231-245); with no unloader none comes.
                if (w.operation == "harvest" and tractor.busy and tractor.stopped
                        and combine_full(tractor, self.COMBINE_WAITING_SHARE)
                        and not any(u.operation == "unload" and u.field == w.field for u in workers)):
                    self._worker_ended(w, full=True)
                    continue
                if tractor.busy or not tractor.stopped:
                    continue
                self._worker_ended(w)
            with self._lock:
                idle = self.current is None and self._after
                steps = self._after.pop(0) if idle else None
            if isinstance(steps, dict):
                self.start(steps["job"], "here", work=steps["work"])
            elif steps:
                self.start("worker_return", "here", work={"steps": steps})
            time.sleep(self.WATCH_S)

    def _worker_ended(self, w: Worker, full: bool = False) -> None:
        field = next((f for f in self.world.fields() if f.id == w.field), None)
        done = (OPERATIONS[w.operation].done(field.coverage)
                if w.operation in OPERATIONS and field is not None and field.coverage else None)
        log.info("worker %s on field %s ended: %s %s", w.tractor, w.field, w.operation,
                 "?" if done is None else f"{done:.3f}")
        try:
            if not self.world.stop(w.tractor, w.owner):
                raise StepFailed("STOP_NOT_CONFIRMED")
            self.world.release(w.owner)
        except (StepFailed, StepUnverified, ValueError) as error:
            log.info("worker %s: not released (%s)", w.tractor, getattr(error, "code", error))
            return                                 # watched again next round
        with self._lock:
            if w in self.workers:
                self.workers.remove(w)
            if w.after:
                self._after.append(w.after)
            unloaders = [u for u in self.workers if u.operation == "unload" and u.field == w.field] \
                if w.operation == "harvest" else []
        if w.operation == "unload":
            self.notify("UNLOADER_STOPPED")             # AutoDrive ended it on its own
            return
        combine = next((i for i in self.world.snapshot() if i.id == w.tractor), None)
        if w.operation == "harvest" and (done is None or done < COVERAGE_REQUIRED) and combine is not None \
                and (full or combine_full(combine, self.COMBINE_WAITING_SHARE)):
            # Full before the field was done: its grain transferred into a trailer (delivered only
            # by the harvest's destination) and harvested on: a job of its own.
            with self._lock:
                self._after.append({"job": "combine_full", "work": {"field": w.field, "combine": combine.name,
                                                                    "destination": w.destination}})
            self.notify("COMBINE_FULL")
            return
        finished = combine is not None and combine.ai_stop == "FINISHED" if w.operation == "harvest" else True
        worked = done is not None and done >= COVERAGE_REQUIRED and finished
        log.info("worker %s on field %s: GIANTS ended %s, measured %s: %s", w.tractor, w.field,
                 combine.ai_stop if combine is not None else "?", "?" if done is None else f"{done:.3f}",
                 "done" if worked else "not done")
        self.notify("WORKER_DONE" if worked else "WORKER_STOPPED")
        destination = self._destination(w.destination) if w.operation == "harvest" and not unloaders else None
        rig = self._harvest_rigs.get(w.field)
        if worked and destination and rig is not None:
            # The harvest is done: what this harvest put in the trailer goes where it was told.
            with self._lock:
                self._after.append([{"task": destination, "tractor_name": rig}])
        # The combine is done: its unloader stops, and what it still carries is sold.
        for u in unloaders:
            try:
                if not self.world.stop(u.tractor, u.owner):
                    raise StepFailed("STOP_NOT_CONFIRMED")
                self.world.release(u.owner)
            except (StepFailed, StepUnverified, ValueError) as error:
                log.info("unloader %s: not released (%s)", u.tractor, getattr(error, "code", error))
                continue
            with self._lock:
                if u in self.workers:
                    self.workers.remove(u)
            observed = {i.id: i for i in self.world.snapshot()}
            trailer = observed.get(observed[u.tractor].attached_to) if u.tractor in observed else None
            if trailer is not None and sum(f.level or 0.0 for f in trailer.fill_units) > 0:
                with self._lock:
                    self._after.append([{"task": "sell"}])

    def _stop_reserved(self, owner: str) -> None:
        """Pre-empt the running operation; the job's own cleanup confirms the stop."""
        ids = self.world.reservation(owner)
        if ids is None:
            return                                 # not reserved yet: the cancel suffices
        try:
            self.world.stop(ids[0], owner)
        except (StepFailed, StepUnverified, ValueError) as error:
            log.info("stop for %s: %s", owner, getattr(error, "code", error))

    # -------------------------------------------------------------------- jobs
    def start(self, command: str, target, spoken: bool = False, implement: str | None = None,
              work: dict | None = None) -> Job | None:
        with self._lock:
            if self.current is not None:
                busy = True
            else:
                busy = False
                self._jobs += 1
                self._cancel = threading.Event()
                self.current = Job(self._jobs, f"{self.session()}-{self._jobs}", spoken=spoken, command=command)
                job, cancel = self.current, self._cancel
        if busy:
            self.notify("BUSY", spoken)
            return None
        self.notify(JOBS[command][1], spoken)
        worker = threading.Thread(target=self._run, args=(job, cancel, command, target, implement, work),
                                  daemon=True, name=f"farmhand-job-{job.number}")
        worker.start()
        self._worker = worker                      # published started: wait() never joins an unstarted thread
        return job

    def _run(self, job: Job, cancel: threading.Event, command: str, target, implement: str | None = None,
             work: dict | None = None) -> None:
        task, _, complete = JOBS[command]
        try:
            for owner in self.world.retained():   # an earlier job's unconfirmed stop
                if owner not in self._worker_owners():
                    self.world.recover(owner)

            def entered(state):
                job.step = state
                if state in MILESTONES:
                    self.notify(f"WORKING_{state}", job.spoken)

            self.world.cancel = cancel           # the link refuses steps once it is set
            result = self._dispatch(job, cancel, entered, command, target, implement, work)
            job.states, job.equipment = tuple(result.states), tuple(result.equipment_ids)
            job.state, job.reason = result.status, result.reason
            log.info("job %d result %s", job.number, json.dumps({
                "status": result.status, "reason": result.reason,
                "equipment": list(result.equipment_ids), "states": list(result.states)}))
        except StepUnverified as error:            # not linked: nothing was sent
            job.state, job.reason = "BLOCKED", error.code
        except Exception:                          # the executor already stopped its own work
            log.exception("job %d failed", job.number)
            job.state, job.reason = "FAILED", "INTERNAL_ERROR"
        finally:
            self.world.cancel = None
            self.running = None
            then = None
            with self._lock:
                self.current, self.last = None, job
                if command in GOALS:
                    self.kept = None if job.state == "COMPLETE" else Kept(command, target, implement, work)
                    self._save_goal()
                then, self._then = self._then, None
            notice = result_notice(job.state, job.reason or "", complete)
            if command in GOALS and job.state != "COMPLETE" and then is None:
                notice = "PAUSED" if job.state == "CANCELLED" else notice
            self.notify(notice, job.spoken)
            if then is not None and self.kept is not None:
                self._retarget_kept(then)
                self.resume(job.spoken)

    # ---------------------------------------------------------- resume / retarget
    def resume(self, spoken: bool = False) -> Job | None:
        """Continue the kept goal from the observed state (nothing completed repeats)."""
        with self._lock:
            kept, busy = self.kept, self.current is not None
        if busy:
            self.notify("BUSY", spoken)
            return None
        if kept is None:
            self.notify("NOTHING_TO_RESUME", spoken)
            return None
        log.info("resuming %s to %s with %s", kept.command, kept.target, kept.work)
        self.notify("RESUMING", spoken)
        return self.start(kept.command, kept.target, spoken, kept.implement, kept.work)

    def retarget(self, target, spoken: bool = False) -> Job | None:
        """A new destination for the goal: a running one is stopped and resumed with it
        once its stop is confirmed; a kept one is resumed with it."""
        with self._lock:
            job = self.current
            running_goal = job is not None and job.command in GOALS
            if running_goal:
                self._then = target
        if running_goal:
            self.notify("RETARGETED", spoken)
            self.stop(spoken)
            return None
        if job is not None:
            self.notify("BUSY", spoken)
            return None
        if self.kept is None:
            self.notify("NOTHING_TO_RESUME", spoken)
            return None
        self._retarget_kept(target)
        self.notify("RETARGETED", spoken)
        return self.resume(spoken)

    def _load_goal(self) -> Kept | None:
        if self.goal_file is None:
            return None
        try:
            data = json.loads(self.goal_file.read_text(encoding="utf-8"))
            if data.get("command") not in GOALS or not isinstance(data.get("work"), dict):
                raise ValueError("not a goal")
            log.info("kept goal from %s: %s", self.goal_file, data)
            return Kept(data["command"], data.get("target", "here"), data.get("implement"), data["work"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError) as error:
            log.info("kept goal unreadable (%s): ignored", error)
            return None

    def _save_goal(self) -> None:
        """Atomically: the kept goal, or no file when there is none."""
        if self.goal_file is None:
            return
        try:
            if self.kept is None:
                self.goal_file.unlink(missing_ok=True)
                return
            self.goal_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.goal_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"command": self.kept.command, "target": self.kept.target,
                                       "implement": self.kept.implement, "work": self.kept.work}), encoding="utf-8")
            os.replace(tmp, self.goal_file)
        except OSError as error:
            log.info("kept goal not saved: %s", error)

    def _retarget_kept(self, target) -> None:
        kept = self.kept
        kept.target = target
        if kept.command == "haul_tractor":
            kept.work["target"] = target
            kept.work["delivered"] = False          # delivered somewhere else: deliver again
        elif kept.command == "sequence":
            for n, step in enumerate(kept.work["steps"]):
                if n >= kept.work.get("done", 0) and step["command"] in ("move_pair", "fetch_pair"):
                    step["target"] = target
        log.info("goal %s retargeted to %s", kept.command, target)
        self._save_goal()

    def _dispatch(self, job: Job, cancel: threading.Event, entered, command: str, target, implement, work,
                  parent: str | None = None):
        """Run one command for ``job``: its own planner for a goal, else one executor intent.
        ``parent``: the job whose per-job budgets a sequence step shares (UNPARK's, once per job)."""
        if command == "field_work":
            return self._field_work(job, cancel, entered, work)
        if command == "buy":
            return self._buy(job, cancel, entered, work)
        if command == "sequence":
            return self._sequence(job, cancel, entered, work)
        if command == "haul_tractor":
            return self._haul(job, cancel, entered, work)
        if command == "contract":
            return self._contract(job, cancel, entered, work)
        if command == "worker_return":
            return self._run_steps(job, cancel, entered, work["steps"], "r")
        if command == "unload_combine":
            return self._unload_combine(job, cancel, entered, work)
        if command == "combine_full":
            return self._combine_full(job, cancel, entered, work)
        if command == "load_tractor" and work and work.get("carry"):
            return self._load_with(job, cancel, entered, work)
        intent = {"version": 1, "request_id": job.owner, "task": JOBS[command][0],
                  "farm_id": self.farm(), "target": "player_at_acceptance" if target == "here" else target}
        if implement is not None:
            intent["implement"] = implement
        intent.update({k: v for k, v in (work or {}).items() if k in NAMES})   # tractor_name / implement_name
        log.info("job %d intent %s", job.number, json.dumps(intent))
        self.running = intent["request_id"]
        return self.executor.execute(intent, cancel=cancel, on_state=entered, job=parent or job.owner)

    def _sequence(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """The player's commands in order, one job: each step starts only after the one
        before it completed (COMPLETE, from its own verification) and only if not cancelled;
        the first that did not complete ends it with its own result."""
        states, used = [], []
        work.setdefault("done", 0)                # kept with the goal: a resume starts after these
        for n, step in enumerate(work["steps"], 1):
            if n <= work["done"]:
                states.append(f"{step['command']}:ALREADY_DONE")
                continue
            if cancel.is_set():
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", tuple(used), tuple(states))
            command = step["command"]
            if command == "buy":
                baseline = work.setdefault("baseline", {})
                count = owned_like(tuple(self.world.snapshot()), self.farm(), step["wants"])
                if str(n) in baseline and count > baseline[str(n)]:
                    # Bought before a restart or a stop (the farm has more of it than before
                    # this step first ran): never bought twice.
                    log.info("job %d step %d buy: already bought (%d > %d)", job.number, n, count, baseline[str(n)])
                    states.append("buy:ALREADY_DONE")
                    work["done"] = n
                    self._save_goal()
                    continue
                baseline.setdefault(str(n), count)
                self._save_goal()
            sub = Job(number=job.number, owner=f"{job.owner}-s{n}", spoken=job.spoken)
            step = with_context(step, work.setdefault("context", {}))
            if step.get("together"):
                return Result("BLOCKED", "NOTHING_TO_HITCH_TOGETHER", (), tuple(states))
            result = self._dispatch(sub, cancel, entered, command, step.get("target", "here"), step.get("implement"),
                                    work_for(step), parent=job.owner)
            states += [f"{command}:{s}" for s in result.states]
            used += [i for i in result.equipment_ids if i not in used]
            log.info("job %d step %d %s result %s %s", job.number, n, command, result.status, result.reason)
            if result.status != "COMPLETE":
                return Result(result.status, result.reason, tuple(used), tuple(states))
            remember(work["context"], step, result, tuple(self.world.snapshot()))
            work["done"] = n
            self._save_goal()
        return Result("COMPLETE", "SEQUENCE_DONE", tuple(used), tuple(states))

    MAX_LOAD_WITH_STEPS = 6

    def _load_with(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """"Load the tractor and plow on the gooseneck", planned from observed state before every
        step: a tractor on the deck without the implement is unloaded, the implement hitched to
        it, then the two loaded together (AGENTS.md exception 5, a mounted implement along)."""
        states, used = [], []
        for n in range(1, self.MAX_LOAD_WITH_STEPS + 1):
            if cancel.is_set():
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", tuple(used), tuple(states))
            self._settle()
            step, failure = load_with_step(tuple(self.world.snapshot()), self.farm(), work)
            if step is None:
                if failure == "LOADED":
                    return Result("COMPLETE", "LOADED_AND_SECURED", tuple(used), tuple(states))
                return Result("BLOCKED", failure, tuple(used), tuple(states))
            intent = {"version": 1, "request_id": f"{job.owner}-w{n}", "farm_id": self.farm(),
                      "target": "player_at_acceptance", **step}
            log.info("job %d load-with step %d intent %s", job.number, n, json.dumps(intent))
            self.running = intent["request_id"]
            result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
            states += [f"{step['task']}:{s}" for s in result.states]
            used += [i for i in result.equipment_ids if i not in used]
            if result.status != "COMPLETE":
                return Result(result.status, result.reason, tuple(used), tuple(states))
            if step["task"] == "load_tractor":
                return Result("COMPLETE", "LOADED_AND_SECURED", tuple(used), tuple(states))
        return Result("FAILED", "LOAD_WITH_PLAN_TOO_LONG", tuple(used), tuple(states))

    MAX_HAUL_STEPS = 8

    def _haul(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """Take a tractor somewhere on a transport trailer and unload it there, planned from
        the observed state before every step (BUILD_SPEC completion plan, requirement 1):
        hitch a transport to a truck if none is hitched, load (an executor load continues
        whatever is already done), deliver the rig carrying the tractor, unload. Unloading
        only after this goal's delivery step completed (AGENTS.md: never because the rig
        stopped). The first step not complete ends it with that step's result."""
        name, target = work.get("tractor_name"), work["target"]
        states, used = [], []
        work.setdefault("delivered", False)       # kept with the goal: a resume never unloads early
        work.setdefault("staged", False)          # the empty transport where AutoDrive has a route
        for n in range(1, self.MAX_HAUL_STEPS + 1):
            if cancel.is_set():
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", tuple(used), tuple(states))
            self._settle()
            step, failure = haul_step(tuple(self.world.snapshot()), self.farm(), name, target, work["delivered"],
                                      work["staged"], work.get("transport"), work.get("truck_name"))
            if step is not None and step.get("implement_name") and step["task"] in ("stage_transport", "load_tractor"):
                work.setdefault("transport", step["implement_name"])       # this transport from now on
            if step is None:
                if failure == "HAULED":
                    return Result("COMPLETE", "HAULED_AND_UNLOADED", tuple(used), tuple(states))
                return Result("BLOCKED", failure, tuple(used), tuple(states))
            intent = {"version": 1, "request_id": f"{job.owner}-h{n}", "farm_id": self.farm(), **step}
            log.info("job %d haul step %d intent %s", job.number, n, json.dumps(intent))
            self.running = intent["request_id"]
            result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
            states += [f"{step['task']}:{s}" for s in result.states]
            used += [i for i in result.equipment_ids if i not in used]
            log.info("job %d haul step %d %s result %s %s", job.number, n, step["task"], result.status, result.reason)
            if step["task"] == "stage_transport" and result.status in ("COMPLETE", "BLOCKED"):
                work["staged"] = True             # routed, staged, or nothing to stage with: load here
                continue
            if result.status != "COMPLETE":
                return Result(result.status, result.reason, tuple(used), tuple(states))
            if step["task"] == "move_pair":
                work["delivered"] = True          # observed arrival (DELIVERY_OBSERVED): unloading may follow
            elif step["task"] == "unload_tractor":
                return Result("COMPLETE", "HAULED_AND_UNLOADED", tuple(used), tuple(states))
        return Result("FAILED", "HAUL_PLAN_TOO_LONG", tuple(used), tuple(states))

    CONTRACT_WAIT_S = 60.0

    def _until(self, check, seconds: float, cancel: threading.Event):
        """``check()`` once it is truthy within ``seconds`` (observed state), else None."""
        deadline = time.monotonic() + seconds
        while not cancel.is_set():
            found = check()
            if found or time.monotonic() >= deadline:
                return found or None
            time.sleep(1.0)
        return None

    def _contract(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """A field contract as the player takes it in the menu: chosen (this farm's own
        first, else the best paid one the farm's implements can do), taken through GIANTS,
        its field worked by the field-work plan, then collected once GIANTS finished it,
        complete only when the reward reached the farm's balance. Kept when not complete:
        "continue" picks it up from GIANTS' own state."""
        contract, failure = choose_contract(self.world.contracts(), tuple(self.world.snapshot()), self.farm(),
                                            work.get("field"), work.get("kind"))
        if contract is None:
            return Result("BLOCKED", failure)
        operation = contract_operation(contract.kind)
        work["field"] = contract.field                 # a resume continues this one
        log.info("job %d contract: field %s %s (%s) reward %s", job.number, contract.field, contract.kind,
                 contract.status, contract.reward)
        states = []

        def mine():
            return next((c for c in self.world.contracts() if c.field == contract.field and c.mine), None)

        try:
            if not contract.mine:
                entered("CONTRACT")
                states.append("CONTRACT")
                self.world.contract(contract.field, start=True)
                # Its field is Farmhand's to work once the snapshot lists it.
                if self._until(lambda: any(f.id == contract.field for f in self.world.fields()),
                               self.CONTRACT_WAIT_S, cancel) is None:
                    return Result("UNVERIFIED", "CONTRACT_FIELD_NOT_OBSERVED", (), tuple(states))
            current = mine()
            if current is None:
                return Result("FAILED", "CONTRACT_LOST", (), tuple(states))
            used = ()
            if current.status != "FINISHED":
                result = self._field_work(job, cancel, entered, {"field": contract.field, "operations": [operation],
                                                                 "background": False})
                states += list(result.states)
                used = tuple(result.equipment_ids)
                if result.status != "COMPLETE":
                    return Result(result.status, result.reason, used, tuple(states))
                if self._until(lambda: (mine() or contract).status == "FINISHED", self.CONTRACT_WAIT_S, cancel) is None:
                    return Result("UNVERIFIED", "CONTRACT_NOT_FINISHED", used, tuple(states))
            # GIANTS' own verdict (MissionStatus FINISHED) and its completion share, for the record.
            log.info("job %d contract field %s: GIANTS %s, completion %s", job.number, contract.field,
                     (mine() or contract).status, (mine() or contract).completion)
            farm = self.world.farm()
            money = farm.money if farm is not None else None
            entered("COLLECT")
            states.append("COLLECT")
            self.world.contract(contract.field, start=False)

            def arrived():
                now = self.world.farm()
                return money is not None and now is not None and now.money is not None and now.money > money

            paid = self._until(arrived, 10.0, cancel)
            if not paid:
                return Result("UNVERIFIED", "PAYMENT_NOT_OBSERVED", used, tuple(states))
            log.info("job %d contract field %s paid: %s -> %s", job.number, contract.field, money,
                     self.world.farm().money)
            return Result("COMPLETE", "CONTRACT_PAID", used, tuple(states))
        except StepFailed as failure:
            return Result("FAILED" if states[-1:] == ["COLLECT"] else "BLOCKED", failure.code, (), tuple(states))
        except StepUnverified as unknown:
            return Result("UNVERIFIED", unknown.code, (), tuple(states))

    def _buy(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """Each want in turn, one executor purchase each; the first not COMPLETE ends it."""
        states, bought = [], []
        wants = [dict(w) for w in work["wants"]]
        # A truck bought with a gooseneck trailer (a deckover is one) gets a gooseneck hitch (live: the GMC's
        # default is "No Attachments").
        if any(w["kind"] == "trailer" and any(k in (w.get("name") or "") for k in ("gooseneck", "deckover"))
               for w in wants):
            for w in wants:
                if w["kind"] == "truck":
                    w.setdefault("hitch", "gooseneck")
        for n, want in enumerate(wants, 1):
            intent = {"version": 1, "request_id": f"{job.owner}-{n}", "farm_id": self.farm(),
                      "target": "player_at_acceptance", "task": "buy", "want": want}
            log.info("job %d buy %d intent %s", job.number, n, json.dumps(intent))
            self.running = intent["request_id"]
            result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
            states += [f"buy:{s}" for s in result.states]
            bought += list(result.equipment_ids)
            log.info("job %d buy %d result %s %s %s", job.number, n, result.status, result.reason, result.equipment_ids)
            if result.status != "COMPLETE":
                return Result(result.status, result.reason, tuple(bought), tuple(states))
        return Result("COMPLETE", "BOUGHT", tuple(bought), tuple(states))

    SETTLE_S = 30.0

    def _settle(self) -> None:
        """Before each planned step, let the farm's vehicles come to rest (live
        5454f21977df465b-2: the tractor still rolled 1.5 s after the helper stopped, so
        the next step found no pair at rest)."""
        deadline = time.monotonic() + self.SETTLE_S
        while time.monotonic() < deadline:
            # A helper working on (AI job) moves by design: never waited for.
            if all(i.stopped or i.busy for i in self.world.snapshot() if i.category == "tractor"):
                return
            time.sleep(1.0)

    def _field_work(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """Each operation in turn on the same field (resolved once): the planner's next
        steps from fresh state, each a full executor job; the first one that is not
        COMPLETE ends the plan with its result. The field work itself is handed to
        GIANTS' helper (unless ``work["background"]`` is False, or a test area): the job
        goes on (the next operation, the next command) while it works; a next operation on
        the same field starts once the one ahead has HEAD_START of it done, and, with no
        free tractor, once the one ahead has come back."""
        field, failure = resolve_field(work["field"], self.world.fields())
        if field is None:
            return Result("BLOCKED", failure)
        log.info("job %d field work %s on field %s (%.2f ha)", job.number, work["operations"], field.id, field.area_ha)
        background = work.get("background", True) and not work.get("area_m2")
        states, equipment, started = [], [], []
        n = 0

        def fresh():
            return next((f for f in self.world.fields() if f.id == field.id), field)

        def wait_for(done) -> bool:
            entered("WAIT_AHEAD")
            while not cancel.is_set() and not done():
                time.sleep(self.WATCH_S)
            return not cancel.is_set()

        if "harvest" in work["operations"]:
            self._empty_full_combines(job, cancel, entered, work.get("destination"), field.id)
            self._part_rigs(job, cancel, entered)
        for operation in work["operations"]:
            ahead = started[-1] if started else None
            if ahead is not None and not wait_for(lambda: ahead not in self.workers or (
                    fresh().coverage is not None
                    and OPERATIONS[ahead.operation].done(fresh().coverage) >= HEAD_START)):
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", tuple(equipment), tuple(states))
            for attempt in (1, 2):
                field = fresh()
                if already_done(OPERATIONS[operation], field, work.get("area_m2")):
                    log.info("job %d %s: field %s already done; skipped", job.number, operation, field.id)
                    states.append(f"{operation}:ALREADY_DONE")
                    break
                crop = None if work.get("over_crop") else crop_problem(OPERATIONS[operation], field)
                if crop is not None:
                    # Before anything is hitched or moved.
                    log.info("job %d %s: field %s %s", job.number, operation, field.id, crop)
                    return Result("BLOCKED", crop, tuple(equipment), tuple(states))
                steps, note = field_steps(tuple(self.world.snapshot()), operation, field.id, self.farm(),
                                          work.get("area_m2"), work.get("crop"))
                if steps is None:
                    return Result("BLOCKED", note, tuple(equipment), tuple(states))
                if note is not None:
                    log.info("job %d %s: %s", job.number, operation, note)
                for step in steps:
                    if step["task"] == "field_work":
                        if work.get("over_crop"):
                            step["over_crop"] = True
                        if background:
                            step["hand_off"] = True
                retry = False
                for i, step in enumerate(steps):
                    n += 1
                    self._settle()
                    intent = {"version": 1, "request_id": f"{job.owner}-{n}", "farm_id": self.farm(),
                              "target": "player_at_acceptance", **step}
                    log.info("job %d step %d intent %s", job.number, n, json.dumps(intent))
                    self.running = intent["request_id"]
                    result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
                    states += [f"{step['task']}:{s}" for s in result.states]
                    equipment += [e for e in result.equipment_ids if e not in equipment]
                    log.info("job %d step %d result %s %s", job.number, n, result.status, result.reason)
                    if (result.status == "BLOCKED" and step["task"] == "hitch_pair" and ahead is not None
                            and attempt == 1):
                        # No free tractor while the one ahead works: it comes back first.
                        if not wait_for(lambda: ahead not in self.workers):
                            return Result("CANCELLED", "CANCELLED_BY_REQUESTER", tuple(equipment), tuple(states))
                        result = self._run_after(job, cancel, entered, ahead)
                        if result.status != "COMPLETE":
                            return Result(result.status, result.reason, tuple(equipment), tuple(states))
                        retry = True
                        break
                    if result.status != "COMPLETE":
                        return Result(result.status, result.reason, tuple(equipment), tuple(states))
                    if result.reason == "WORKER_STARTED":
                        worker = Worker(intent["request_id"], result.equipment_ids[0], field.id, step["operation"],
                                        steps[i + 1:], work.get("destination") if step["operation"] == "harvest" else None)
                        self._add_worker(worker)
                        started.append(worker)
                        self.notify("WORKER_STARTED", job.spoken)
                        # AutoDrive's combine unloader takes the grain to a sell point: only when
                        # this harvest's crop is to be sold.
                        if step["operation"] == "harvest" and self._destination(worker.destination) == "sell":
                            self._start_unloader(job, cancel, entered, field.id)
                        break
                if not retry:
                    break
        if started:
            return Result("COMPLETE", "WORKERS_STARTED", tuple(equipment), tuple(states))
        return Result("COMPLETE", "FIELD_WORK_OBSERVED", tuple(equipment), tuple(states))

    COMBINE_FULL_SHARE = 0.9          # a combine this full is emptied before its helper starts
    COMBINE_WAITING_SHARE = 0.95      # a harvesting combine this full and standing waits for a trailer

    def _empty_full_combines(self, job: Job, cancel: threading.Event, entered, destination=None, field=None) -> None:
        """Before a harvest: a full combine's helper would not work (live 3dc00834639340f7-2,
        COULD_NOT_PREPARE), so its grain is transferred into a trailer first (and delivered
        only by the harvest's destination)."""
        for combine in self.world.snapshot():
            if combine.farm_id == self.farm() and combine_full(combine, self.COMBINE_FULL_SHARE):
                log.info("job %d combine %s full: emptied first", job.number, combine.id)
                self._service_combine(job, cancel, entered, combine.name, destination, field)

    def _part_rigs(self, job: Job, cancel: threading.Event, entered) -> None:
        """Before a harvest: a trailer rig left beside a combine is taken on past it first
        (GIANTS' agent starts neither while they stand together)."""
        intent = {"version": 1, "request_id": f"{job.owner}-p", "farm_id": self.farm(),
                  "target": "player_at_acceptance", "task": "part"}
        self.running = intent["request_id"]
        result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
        log.info("job %d part: %s %s", job.number, result.status, result.reason)

    def _combine_full(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """A harvesting combine found full (its helper stopped by the watcher, or "unload the
        combine" while it harvests): its grain transferred into a trailer, delivered only by
        the harvest's destination, and the field's harvest started again. Kept when it cannot
        go on (no trailer with room and nowhere to take the grain): "continue" picks it up."""
        result = self._service_combine(job, cancel, entered, work.get("combine"), work.get("destination"), work["field"])
        if result.status != "COMPLETE":
            return result
        return self._field_work(job, cancel, entered, {"field": work["field"], "operations": ["harvest"],
                                                       "destination": work.get("destination")})

    def _execute(self, job: Job, cancel: threading.Event, entered, tag: str, **fields) -> Result:
        intent = {"version": 1, "request_id": f"{job.owner}-{tag}", "farm_id": self.farm(),
                  "target": "player_at_acceptance", **{k: v for k, v in fields.items() if v is not None}}
        self.running = intent["request_id"]
        result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
        log.info("job %d %s: %s %s", job.number, fields.get("task"), result.status, result.reason)
        return result

    def _service_combine(self, job: Job, cancel: threading.Event, entered, combine_name=None,
                         destination=None, field=None) -> Result:
        """A standing combine's grain transferred into a trailer under its pipe ("unload the
        combine" is that and nothing more). Where it goes then is the harvest's destination
        (the request's words, else the saved harvest policy) only: delivered (sold, or stored
        in the farm's own storage) once the trailer could not take another tankful; with no
        destination it stays in the trailer. With no trailer room left: with a destination the
        fullest rig delivers first and the transfer follows; without one it waits, said."""
        destination = self._destination(destination)
        fields = {"task": "overload", **({"tractor_name": combine_name.casefold()} if combine_name else {})}
        result = self._execute(job, cancel, entered, "o", **fields)
        if result.status == "BLOCKED" and result.reason == "NO_TRAILER_FOR_THE_GRAIN" and destination:
            delivered = self._execute(job, cancel, entered, "d", task=destination)
            self.notify(result_notice(delivered.status, delivered.reason, f"COMPLETE_{destination.upper()}"), job.spoken)
            if delivered.status == "COMPLETE":
                result = self._execute(job, cancel, entered, "o2", **fields)
        if result.status == "BLOCKED" and result.reason == "NO_TRAILER_FOR_THE_GRAIN":
            # No tractor tows a trailer with room (live f0a179a567804793-1: the EDK 650 unhitched,
            # every tractor with an implement on): make that rig, then the transfer.
            made = self._make_grain_rig(job, cancel, entered)
            if made is not None and made.status != "COMPLETE":
                return made
            if made is not None:
                result = self._execute(job, cancel, entered, "o3", **fields)
        if result.status == "BLOCKED" and result.reason == "NO_TRAILER_FOR_THE_GRAIN":
            self.notify("COMBINE_FULL_NO_TRAILER", job.spoken)     # say sell / store the grain, then continue
            return result
        self.notify(result_notice(result.status, result.reason, "COMBINE_UNLOADED"), job.spoken)
        if result.status != "COMPLETE":
            return result
        observed = {i.id: i for i in self.world.snapshot()}
        tractor, trailer, combine = (observed.get(i) for i in (tuple(result.equipment_ids) + (None,) * 3)[:3])
        if tractor is not None and field is not None and _words(tractor.name):
            self._harvest_rigs[field] = _words(tractor.name)
        if destination and trailer is not None and combine is not None:
            room = sum((u.capacity or 0.0) - (u.level or 0.0) for u in trailer.fill_units)
            tank = max((u.capacity or 0.0) for u in combine.fill_units
                       if any(t not in self.executor.NOT_CARGO for t in u.supported) or not u.supported)
            log.info("job %d after the transfer: trailer room %.0f l, combine tank %.0f l", job.number, room, tank)
            if room < tank:
                delivered = self._execute(job, cancel, entered, "d", task=destination,
                                          tractor_name=_words(tractor.name) if tractor is not None else None)
                self.notify(result_notice(delivered.status, delivered.reason, f"COMPLETE_{destination.upper()}"),
                            job.spoken)
        return result

    def _make_grain_rig(self, job: Job, cancel: threading.Event, entered) -> Result | None:
        """A tractor and a trailer with room for grain, hitched, when none is: the trailer that
        stands free with the most room; a free tractor, else one freed (its ordinary implement
        unhitched where it has room ahead, _tractor_to_free). None when there is no such
        trailer or tractor (nothing done); else the hitch's (or the unhitch's) result."""
        items, farm = tuple(self.world.snapshot()), self.farm()

        def room(t):
            return sum((u.capacity or 0.0) - (u.level or 0.0) for u in t.fill_units
                       if not u.supported or any(f not in self.executor.NOT_CARGO for f in u.supported))
        trailers = sorted((t for t in items if usable(t, farm) and t.attached_to is None and "trailer" in t.kinds
                           and t.fill_units and not transport.is_transport(t) and room(t) > 0 and _words(t.name)),
                          key=lambda t: (-room(t), t.id))
        if not trailers:
            return None
        trailer = trailers[0]
        free = [t for t in items if t.category == "tractor" and usable(t, farm) and t.stopped and t.attached_to is None
                and {"move", "attach", "stop"} <= t.capabilities and _words(t.name)]
        if free:
            name = _words(min(free, key=lambda t: (math.dist(t.position, trailer.position), t.id)).name)
        else:
            name = _tractor_to_free(items, "trailer", farm)
            if name is None:
                return None
            log.info("job %d grain rig: freeing the %s for the %s", job.number, name, trailer.name)
            freed = self._execute(job, cancel, entered, "gf", task="detach_pair", tractor_name=name)
            if freed.status != "COMPLETE":
                return freed
        log.info("job %d grain rig: the %s to the %s", job.number, name, trailer.name)
        return self._execute(job, cancel, entered, "gh", task="hitch_pair", implement="trailer",
                             implement_name=_words(trailer.name), tractor_name=name)

    def _unload_combine(self, job: Job, cancel: threading.Event, entered, work: dict) -> Result:
        """"Unload the combine": its grain transferred into a trailer, nothing more. With no
        helper harvesting, the standing combine (the one said, else the fullest) under its
        pipe. With one harvesting (the field said, else the only one): AutoDrive's unloader
        when this harvest's crop is to be sold (it delivers to a sell point); otherwise the
        helper is paused, the grain transferred and the harvest goes on."""
        with self._lock:
            harvesting = [w for w in self.workers if w.operation == "harvest"]
        fields = sorted({w.field for w in harvesting})
        field_id = work.get("field")
        if not fields:
            return self._service_combine(job, cancel, entered)
        if field_id is None and len(fields) != 1:
            return Result("BLOCKED", "SAY_WHICH_FIELD")
        field_id = fields[0] if field_id is None else field_id
        if field_id not in fields:
            return Result("BLOCKED", "NO_COMBINE_HARVESTING")
        with self._lock:
            if any(w.field == field_id and w.operation == "unload" for w in self.workers):
                return Result("COMPLETE", "UNLOADER_ALREADY_WORKING")
        worker = next(w for w in harvesting if w.field == field_id)
        if self._destination(worker.destination) == "sell":
            result = self._start_unloader(job, cancel, entered, field_id)
            if result.reason == "UNLOADER_STARTED":
                return result
        combine = next((i for i in self.world.snapshot() if i.id == worker.tractor), None)
        if not self._pause_worker(worker):
            return Result("UNVERIFIED", "STOP_NOT_CONFIRMED")
        return self._combine_full(job, cancel, entered, {"field": field_id, "destination": worker.destination,
                                                         "combine": combine.name if combine is not None else None})

    def _pause_worker(self, w: Worker) -> bool:
        """A harvesting helper stopped and released, no longer watched (it is started again)."""
        try:
            if not self.world.stop(w.tractor, w.owner):
                raise StepFailed("STOP_NOT_CONFIRMED")
            self.world.release(w.owner)
        except (StepFailed, StepUnverified, ValueError) as error:
            log.info("worker %s: not paused (%s)", w.tractor, getattr(error, "code", error))
            return False
        with self._lock:
            if w in self.workers:
                self.workers.remove(w)
        return True

    def _start_unloader(self, job: Job, cancel: threading.Event, entered, field_id: int) -> Result:
        """Unloading while harvesting (the user 2026-10-21): an AutoDrive rig with a
        trailer that has room runs AutoDrive's combine unloader for this field, a worker
        until the combine is done. Without one the combine waits when full (said so)."""
        intent = {"version": 1, "request_id": f"{job.owner}-u{field_id}", "farm_id": self.farm(),
                  "target": "player_at_acceptance", "task": "unload_harvester", "field": field_id}
        self.running = intent["request_id"]
        result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
        log.info("job %d unloader for field %s: %s %s", job.number, field_id, result.status, result.reason)
        if result.reason == "UNLOADER_STARTED":
            self._add_worker(Worker(intent["request_id"], result.equipment_ids[0], field_id, "unload", []))
            self.notify("UNLOADER_STARTED", job.spoken)
        else:
            self.notify("NO_UNLOADER", job.spoken)
        return result

    def _run_after(self, job: Job, cancel: threading.Event, entered, worker: Worker) -> Result:
        """The ended worker's follow-up steps now (taken off the waiting list), in order."""
        with self._lock:
            steps = worker.after
            self._after = [a for a in self._after if a is not steps]
        return self._run_steps(job, cancel, entered, steps, "r")

    def _run_steps(self, job: Job, cancel: threading.Event, entered, steps, tag: str) -> Result:
        states, equipment = [], []
        for n, step in enumerate(steps, 1):
            if cancel.is_set():
                return Result("CANCELLED", "CANCELLED_BY_REQUESTER", tuple(equipment), tuple(states))
            self._settle()
            intent = {"version": 1, "request_id": f"{job.owner}-{tag}{n}", "farm_id": self.farm(),
                      "target": "player_at_acceptance", **step}
            log.info("job %d %s%d intent %s", job.number, tag, n, json.dumps(intent))
            self.running = intent["request_id"]
            result = self.executor.execute(intent, cancel=cancel, on_state=entered, job=job.owner)
            states += [f"{step['task']}:{s}" for s in result.states]
            equipment += [e for e in result.equipment_ids if e not in equipment]
            if result.status != "COMPLETE":
                return Result(result.status, result.reason, tuple(equipment), tuple(states))
        return Result("COMPLETE", "RETURNED", tuple(equipment), tuple(states))

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the running job (tests and shutdown)."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
