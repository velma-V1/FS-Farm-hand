"""Resolve a travel goal from current game state, without choosing a provider.

An attached trailer and its belted cargo are part of the selected vehicle's rig.
This module only identifies and validates them; movement requires a separate
game-side driver and fresh physical verification.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

from protocol.model import Equipment, Position


class GoalError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DriveGoal:
    vehicle_id: str
    rig_ids: tuple[str, ...]
    target: Position
    cargo_id: str | None = None


def _words(value: str | None) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def _named(item: Equipment, wanted: str) -> bool:
    needle, haystack = _words(wanted), _words(item.name)
    return bool(needle) and all(word in haystack for word in needle)


def _usable(item: Equipment, farm_id: str) -> bool:
    return (item.farm_id == farm_id and item.available and not item.busy
            and not item.occupied and item.reserved_by is None and item.stopped)


# What drives itself and can lead a rig: tractors (trucks and cars among them) and
# combines (a harvester with its header).
DRIVEN = ("tractor", "harvester")
# The player's word for a kind of self-propelled machine, not a name.
KIND_WORDS = {"combine": "harvester", "harvester": "harvester", "combine harvester": "harvester"}


def driven_by_words(item: Equipment, words: str | None) -> bool:
    """The vehicle the player's words name: a kind word ("the combine") by its category,
    else every word in its name."""
    if item.category not in DRIVEN or not words:
        return False
    kind = KIND_WORDS.get(" ".join(words.casefold().split()))
    if kind is not None:
        return item.category == kind
    return all(w in (item.name or "").casefold() for w in words.casefold().split())


def resolve_drive_goal(items: tuple[Equipment, ...], farm_id: str, vehicle_name: str | None,
                       cargo_name: str | None, target: Position, vehicle_id: str | None = None) -> DriveGoal:
    """Select exactly one owned idle vehicle and preserve its observed rig.

    The names are player words only; ``vehicle_id`` is one this observation already
    resolved (the tractor towing an implement the player named). Missing or
    contradictory attachment and belt reports are never guessed.
    """
    if (len(target) != 2 or not all(type(x) in (int, float) and math.isfinite(x) for x in target)):
        raise GoalError("DESTINATION_INVALID")
    candidates = [item for item in items if item.category in DRIVEN
                  and (item.id == vehicle_id if vehicle_id is not None else _named(item, vehicle_name))
                  and _usable(item, farm_id) and item.belted_to is None]
    if not candidates:
        raise GoalError("VEHICLE_UNAVAILABLE")
    if len(candidates) != 1:
        raise GoalError("VEHICLE_AMBIGUOUS")
    vehicle = candidates[0]
    observed = {item.id: item for item in items}
    rig = [vehicle]
    current = vehicle
    parent = None
    while current.attached_to is not None and current.attached_to != parent:
        joined = observed.get(current.attached_to)
        if (joined is None or joined in rig or joined.attached_to != current.id
                or joined.category not in ("implement", "forage_wagon")
                or not _usable(joined, farm_id)):
            raise GoalError("RIG_LINK_UNREADABLE")
        rig.append(joined)
        parent = current.id
        current = joined
    # The single-link observation cannot safely represent another attachment.
    linked = {item.id for item in rig}
    if any(item.id not in linked and item.attached_to in linked for item in items):
        raise GoalError("RIG_LINK_UNREADABLE")
    cargo = []
    for unit in rig:
        for identifier in unit.belted:
            carried = observed.get(identifier)
            if (carried is None or carried.belted_to != unit.id or not _usable(carried, farm_id)
                    or carried.id in linked):
                raise GoalError("CARGO_STATE_UNREADABLE")
            cargo.append(carried)
            linked.add(carried.id)
    # What a carrier on the rig holds by GIANTS' DynamicMountAttacher (a header on its trailer) goes too.
    for item in items:
        if item.mounted_to in linked and item.id not in linked:
            if not _usable(item, farm_id) or item.attached_to is not None:
                raise GoalError("CARGO_STATE_UNREADABLE")
            cargo.append(item)
            linked.add(item.id)
    cargo_ids = {item.id for item in cargo}
    if any(item.attached_to is not None and item.attached_to not in cargo_ids for item in cargo):
        raise GoalError("CARGO_STATE_UNREADABLE")
    if any(item.id not in linked and item.attached_to in cargo_ids for item in items):
        raise GoalError("CARGO_STATE_UNREADABLE")
    if any(item.belted_to in linked and item.id not in linked for item in items):
        raise GoalError("CARGO_STATE_UNREADABLE")
    selected_cargo = None
    if cargo_name is not None:
        matches = [item for item in cargo if _named(item, cargo_name)]
        if not matches:
            raise GoalError("CARGO_NOT_ON_RIG")
        if len(matches) != 1:
            raise GoalError("CARGO_AMBIGUOUS")
        selected_cargo = matches[0].id
    return DriveGoal(vehicle.id, tuple(item.id for item in (*rig, *cargo)),
                     tuple(float(x) for x in target), selected_cargo)
