"""One Farmhand-owned travel goal, from observed rig to verified arrival.

The game-side ``farm_drive`` owns steering and local route search. This layer
resolves player intent, reserves the entire current rig, drives, checks physical
arrival, and releases only after every unit is observed stopped.
"""
from __future__ import annotations

import math
import threading
import time

from companion.drive_goal import DRIVEN, GoalError, driven_by_words, resolve_drive_goal
from protocol.model import Result

ARRIVE_M = 3.0   # a place's point; for a field, any rig unit this close to its edge
CLEAR_M = 15.0   # a field-edge target keeps this far from equipment outside the rig
APPROACH_M = 40.0      # a building is reached at any clear approach point this near its centre
MAX_APPROACH_M = 100.0  # = mod/FarmhandDrive.lua Drive.MAX_APPROACH_M


def _edge_point(outline, point, avoid=()):
    """The point on the closed outline nearest ``point`` and at least CLEAR_M from
    every position in ``avoid``, and its distance (None, inf if there is none)."""
    best, best_d = None, math.inf
    for (ax, az), (bx, bz) in zip(outline, outline[1:] + outline[:1]):
        dx, dz = bx - ax, bz - az
        length2 = dx * dx + dz * dz
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - az) * dz) / length2))
        steps = int(math.sqrt(length2)) if avoid else 0
        # The projection first; with something parked on the edge, every metre of it.
        for s in (t, *(i / steps for i in range(steps + 1) if steps)):
            candidate = (ax + dx * s, az + dz * s)
            d = math.dist(candidate, point)
            if d < best_d and all(math.dist(candidate, a) >= CLEAR_M for a in avoid):
                best, best_d = candidate, d
    return best, best_d


def _inside(outline, point):
    x, z = point
    inside = False
    for (ax, az), (bx, bz) in zip(outline, outline[1:] + outline[:1]):
        if (az > z) != (bz > z) and x < ax + (bx - ax) * (z - az) / (bz - az):
            inside = not inside
    return inside


def reached(goal_outline, target, root, rig_positions, area=0.0):
    """A field: any observed unit of the rig inside its boundary or within ARRIVE_M
    of it. A building (farm, store, sell point, production, barn): the root at one
    of its approach points, all within ``area`` of its centre. A point: the root there."""
    if len(goal_outline) >= 3:
        return any(_inside(goal_outline, p) or _edge_point(goal_outline, p)[1] <= ARRIVE_M
                   for p in rig_positions)
    return math.dist(root, target) <= area + ARRIVE_M


class StandaloneMove:
    def __init__(self, world):
        self.world = world

    def _target(self, command, start, avoid=()):
        """The drive's point, for a field its outline (else ()) and for a building
        the approach radius around that point (else 0). A field's edge point stays
        clear of the ``avoid`` positions (equipment outside the rig)."""
        from companion.executor import resolve_place, standoff_pose, valid_position

        wanted = command["target"]
        if wanted == "player_at_acceptance":
            player = self.world.player_position(command["farm_id"])
            if not valid_position(player):
                raise GoalError("INVALID_REQUESTER_POSITION")
            point = standoff_pose(tuple(player), start.position, start.heading or 0.0, 10)
            return (point.x, point.z), (), 0.0
        if not isinstance(wanted, dict):
            raise GoalError("DESTINATION_INVALID")
        if wanted["type"] == "place":
            place, failure = resolve_place(wanted["name"], self.world.destinations(), self.world.places())
            if place is None:
                raise GoalError(failure)
            # A building's centre is occupied, and one standoff beside it can be fenced
            # in (live 9315e713c7e3430f-3). The game searches every clear approach
            # point around it at once and drives to one the rig reaches.
            if place.building is not None:
                return place.building.position, (), APPROACH_M
            return place.marker.position, (), 0.0
        if wanted["type"] == "field_entrance":
            matches = [p for p in self.world.field_places() if p.id == f"field-{wanted['field']}"]
            if len(matches) != 1 or not valid_position(matches[0].position):
                raise GoalError("FIELD_NOT_FOUND")
            outline = tuple(matches[0].outline)
            if len(outline) < 3:
                return matches[0].position, (), 0.0
            # From point A to the nearest point of the field's edge: arriving
            # there, or anywhere along the edge, is arriving at the field.
            point = _edge_point(outline, start.position, avoid)[0] or _edge_point(outline, start.position)[0]
            return point, outline, 0.0
        if wanted["type"] == "autodrive_marker":
            matches = [p for p in self.world.destinations() if p.id == str(wanted["id"])]
            if len(matches) != 1 or not valid_position(matches[0].position):
                raise GoalError("DESTINATION_NOT_FOUND")
            return matches[0].position, (), 0.0
        raise GoalError("DESTINATION_INVALID")

    def execute(self, command, cancel: threading.Event | None = None, on_state=None) -> Result:
        states = []
        def enter(state):
            states.append(state)
            if on_state is not None:
                on_state(state)

        owner, farm_id = command["request_id"], command["farm_id"]
        ids = ()
        reserved = attempted = False
        status, reason = "BLOCKED", "INVALID_GOAL"
        try:
            enter("FIND")
            items = tuple(self.world.snapshot())
            vehicle_id = command.get("vehicle_id")     # resolved by the executor from the player's words
            named = [i for i in items if (i.id == vehicle_id and i.category in DRIVEN if vehicle_id is not None
                                          else driven_by_words(i, command.get("tractor_name")))]
            if len(named) != 1:
                raise GoalError("VEHICLE_AMBIGUOUS" if named else "VEHICLE_UNAVAILABLE")
            vehicle_id = named[0].id
            # The rig first (its units never block its own target), then the target.
            from companion.executor import valid_position
            name, cargo = None, command.get("cargo_name")
            rig = resolve_drive_goal(items, farm_id, name, cargo, (0.0, 0.0), vehicle_id)
            avoid = tuple(i.position for i in items if i.id not in rig.rig_ids and valid_position(i.position))
            target, outline, area = self._target(command, named[0], avoid)
            goal = resolve_drive_goal(items, farm_id, name, cargo, target, vehicle_id)
            ids = goal.rig_ids
            enter("RESERVE")
            if not self.world.reserve_rig(ids, owner):
                raise GoalError("RESERVATION_CONFLICT")
            reserved = True
            if cancel is not None and cancel.is_set():
                status, reason = "CANCELLED", "CANCELLED_BY_REQUESTER"
            else:
                enter("DRIVE")
                attempted = True
                self.world.farm_drive(goal.vehicle_id, goal.target, owner, area)
                enter("VERIFY")
                deadline = time.monotonic() + 10.0
                while True:
                    after = {i.id: i for i in self.world.snapshot()}
                    if all(i in after and after[i].stopped for i in ids) or time.monotonic() >= deadline:
                        break
                    time.sleep(0.25)
                before = {i.id: i for i in items}
                if any(identifier not in after or not after[identifier].stopped
                       or after[identifier].farm_id != farm_id for identifier in ids):
                    raise GoalError("RIG_NOT_STOPPED")
                if any(after[i].attached_to != before[i].attached_to
                       or after[i].belted_to != before[i].belted_to
                       or after[i].belted != before[i].belted for i in ids):
                    raise GoalError("RIG_CHANGED")
                if not reached(outline, goal.target, after[goal.vehicle_id].position,
                               [after[i].position for i in ids], area):
                    raise GoalError("ARRIVAL_NOT_OBSERVED")
                status, reason = "COMPLETE", "DELIVERY_OBSERVED"
        except GoalError as error:
            status, reason = ("FAILED" if attempted else "BLOCKED"), error.code
        except Exception as error:
            from companion.bridge import StepUnverified
            status = "UNVERIFIED" if isinstance(error, StepUnverified) else "FAILED" if attempted else "BLOCKED"
            reason = getattr(error, "code", type(error).__name__)
        finally:
            if reserved:
                try:
                    if attempted:
                        enter("STOP")
                        if not self.world.stop(ids[0], owner):
                            raise GoalError("STOP_NOT_CONFIRMED")
                    enter("RELEASE")
                    # The physical brake can need several frames to settle. The
                    # world adapter waits and retains ownership if it never stops.
                    self.world.release(owner)
                except Exception as error:
                    status, reason = "UNVERIFIED", getattr(error, "code", type(error).__name__)
        return Result(status, reason, ids, tuple(states))
