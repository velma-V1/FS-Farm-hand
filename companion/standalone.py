"""One Farmhand-owned travel goal, from observed rig to verified arrival.

The game-side ``farm_drive`` owns steering and local route search. This layer
resolves player intent, reserves the entire current rig, drives, checks physical
arrival, and releases only after every unit is observed stopped.
"""
from __future__ import annotations

import math
import threading
import time

from companion.drive_goal import GoalError, resolve_drive_goal
from protocol.model import Result


class StandaloneMove:
    def __init__(self, world):
        self.world = world

    def _target(self, command, start):
        from companion.executor import resolve_place, standoff_pose, valid_position

        wanted = command["target"]
        if wanted == "player_at_acceptance":
            player = self.world.player_position(command["farm_id"])
            if not valid_position(player):
                raise GoalError("INVALID_REQUESTER_POSITION")
            point = standoff_pose(tuple(player), start.position, start.heading or 0.0, 10)
            return point.x, point.z
        if not isinstance(wanted, dict):
            raise GoalError("DESTINATION_INVALID")
        if wanted["type"] == "place":
            place, failure = resolve_place(wanted["name"], self.world.destinations(), self.world.places())
            if place is None:
                raise GoalError(failure)
            # A building's centre is occupied. Approach its yard from the rig's side.
            if place.building is not None:
                point = standoff_pose(place.building.position, start.position, start.heading or 0.0, 20)
                return point.x, point.z
            return place.marker.position
        if wanted["type"] == "field_entrance":
            matches = [p for p in self.world.field_places() if p.id == f"field-{wanted['field']}"]
            if len(matches) != 1 or not valid_position(matches[0].position):
                raise GoalError("FIELD_NOT_FOUND")
            return matches[0].position
        if wanted["type"] == "autodrive_marker":
            matches = [p for p in self.world.destinations() if p.id == str(wanted["id"])]
            if len(matches) != 1 or not valid_position(matches[0].position):
                raise GoalError("DESTINATION_NOT_FOUND")
            return matches[0].position
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
            named = [i for i in items if i.category == "tractor" and command.get("tractor_name")
                     and all(w in (i.name or "").casefold() for w in command["tractor_name"].casefold().split())]
            if len(named) != 1:
                raise GoalError("VEHICLE_AMBIGUOUS" if named else "VEHICLE_UNAVAILABLE")
            target = self._target(command, named[0])
            goal = resolve_drive_goal(items, farm_id, command["tractor_name"], command.get("cargo_name"), target)
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
                self.world.farm_drive(goal.vehicle_id, goal.target, owner)
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
                if math.dist(after[goal.vehicle_id].position, goal.target) > 3:
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
