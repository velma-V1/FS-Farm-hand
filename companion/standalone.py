"""One Farmhand-owned travel goal, from observed rig to verified arrival.

The game-side ``farm_drive`` owns steering and local route search. This layer
resolves player intent, reserves the entire current rig, drives, checks physical
arrival, and releases only after every unit is observed stopped.
"""
from __future__ import annotations

import logging
import math
import threading
import time

from companion.drive_goal import DRIVEN, GoalError, driven_by_words, resolve_drive_goal
from protocol.model import Result

log = logging.getLogger("farmhand")

def lab_own_nav_only():
    """Temporary local proof toggle, absent during normal play and tests."""
    from companion.ai import data_dir
    return (data_dir() / "nav_proof.flag").is_file()

# Farmhand's own drive found no way, or could not move the rig: GIANTS' GOTO is tried.
# Parked vehicles this near the rig that is boxed in are driven out of the way, at most this many.
# Measured between roots: a vehicle touching a 20 m rig's trailer stands ~20 m from its root.
BLOCK_RADIUS_M, MAX_BLOCKERS = 25.0, 2
LOOSE_ASIDE_M = 40.0          # without a farm yard: a loose implement in the way goes this far from the rig
YARD_RADIUS_M = 100.0         # the farm yard: the farm building with the most others this near
FIELD_CLEAR_MARGIN_M = 10.0   # before a hand-off, loose implements this near a field's edge go too (its headland)
FIELD_SET_DOWN_M = 6.0        # and are set down this far beyond that margin, outside the field
MOVED_ENOUGH_M = 3.0          # a blocker this far from where it stood counts as moved
# Trips longer than this go to GIANTS' navigation (or AutoDrive) first; what is left after it,
# by the job's own arrival rule (``reached``), is Farmhand's own drive.
LONG_LEG_M = 300.0
# Teleport recovery, each try farther out while GIANTS still cannot start from where the rig
# landed (the user, 2026-10-31: about 25 m, then up to about 150 m; live f08f97a7aef54634-1: the
# nearest open spot was 4 m off inside the same yard pocket). The mod looks 50 m beyond each.
TELEPORT_STEPS_M = (0.0, 25.0, 60.0, 100.0)
# A loaded rig farther than this from AutoDrive's nearest waypoint is first taken onto a waypoint
# that has the route (live 2026-10-31: 51 m off in the yard, AutoDrive ended at once unmoved).
AD_ON_NETWORK_M = 10.0
# GIANTS' GOTO could not start from where the rig stands (cramped, against something).
GOTO_START_CODES = frozenset({"NAVIGATE_BLOCKED_AT_START", "NAVIGATE_GOTO_INVALID", "GOTO_ENDED", "NAVIGATE_STALLED"})
FALLBACK_CODES = frozenset({"NO_ROUTE", "START_NOT_CLEAR", "EGRESS_LIMIT", "EGRESS_DIVERGED", "EGRESS_BLOCKED",
                            "EGRESS_NOT_CLEAR", "TELEPORT_CARGO", "NO_OPEN_SPOT"})

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


def _beside_field(outline, point, distance):
    """A pose ``distance`` outside the field's edge, out from the edge point nearest ``point``
    (away from the field's centre), facing along the way out."""
    from protocol.model import Pose
    edge = _edge_point(outline, point)[0] or point
    cx = sum(p[0] for p in outline) / len(outline)
    cz = sum(p[1] for p in outline) / len(outline)
    dx, dz = edge[0] - cx, edge[1] - cz
    d = math.hypot(dx, dz) or 1.0
    ux, uz = dx / d, dz / d
    return Pose(edge[0] + ux * distance, edge[1] + uz * distance, math.atan2(ux, uz))


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
    def __init__(self, world, route_memory=None):
        self.world = world
        if route_memory is None:
            from companion.ai import data_dir
            from companion.route_memory import RouteMemory
            route_memory = RouteMemory(data_dir() / "routes.json")
        self.route_memory = route_memory
        self._route_used = None
        self._route_failed = set()
        self._wanted = None                     # the command's target, for AutoDrive's marker
        self._outline, self._area = (), 0.0     # the target's field outline / approach radius

    def _loaded_marker(self, goal):
        """AutoDrive's marker for a rig carrying belted cargo, or None. GIANTS' helper is blocked
        from its first frame by a vehicle belted on its own trailer (adapters/EVIDENCE.md "Moving a
        loaded transport", live d14230aa9fb24d30-3; again live abe8eb41ca6f4b5f-1: blocked from
        every spot, the teleport recovery no help); AutoDrive counts that cargo as the rig's own.
        A field's marker is made AutoDrive's own way when there is none (``ad_marker``)."""
        from companion.executor import _normalized_name, valid_position
        items = {i.id: i for i in self.world.snapshot()}
        tractor = items.get(goal.vehicle_id)
        if (tractor is None or "autodrive" not in tractor.capabilities or not hasattr(self.world, "travel")
                or not any(i.belted_to in goal.rig_ids for i in items.values())):
            return None
        wanted = self._wanted or {}
        if wanted.get("type") == "autodrive_marker":
            return str(wanted["id"])
        name = f"field {wanted['field']}" if wanted.get("type") == "field_entrance" else \
            _normalized_name(wanted.get("name") or "") if wanted.get("type") == "place" else None
        if not name:
            return None

        def find():
            return next((d.id for d in self.world.destinations()
                         if _normalized_name(d.name or "") == name and valid_position(d.position)), None)

        marker = find()
        if marker is None and wanted.get("type") == "field_entrance" and hasattr(self.world, "ad_marker"):
            try:
                self.world.ad_marker(field=int(wanted["field"]))
            except Exception as failure:
                log.info("AutoDrive marker for %s: %s", name, getattr(failure, "code", failure))
            marker = find()
        return marker

    def _onto_ad_network(self, goal, here, marker, owner):
        """A loaded rig off AutoDrive's network onto a waypoint with the route to ``marker``, facing
        along it: Farmhand's own drive for the short hop (the tight local part), else, when the
        player allows it, the teleport recovery onto it. Nothing when it is on the network
        already or the saved network cannot be read."""
        from companion import hitch as transport
        from protocol.model import Pose
        graph = self.world.ad_graph() if hasattr(self.world, "ad_graph") else None
        point = next((d.position for d in self.world.destinations() if d.id == marker), None)
        if graph is None or point is None or graph.closest(here)[1] <= AD_ON_NETWORK_M:
            return
        starts = transport.ad_route_starts(graph, here, point)
        failure = None
        for (x, z), yaw, wp in starts:
            log.info("loaded rig %.0f m off AutoDrive's network: Farmhand's own drive onto waypoint %s at (%.1f, %.1f)",
                     graph.closest(here)[1], wp, x, z)
            try:
                self.world.farm_drive(goal.vehicle_id, (x, z), owner, 0.0, yaw)
                return
            except Exception as error:
                failure = error
                log.info("onto waypoint %s: %s", wp, getattr(error, "code", error))
        if starts and getattr(self.world, "teleport_recovery", False) and hasattr(self.world, "teleport"):
            (x, z), yaw, wp = starts[0]
            log.info("no way onto AutoDrive's network by driving: teleport recovery onto waypoint %s", wp)
            if hasattr(self.world, "await_stopped"):
                self.world.await_stopped(goal.vehicle_id)
            self.world.teleport(goal.vehicle_id, owner, 0.0, Pose(x, z, yaw))
            return
        if failure is not None:
            raise failure

    def _rest_of_leg(self, goal):
        """After AutoDrive's or GIANTS' leg: None when the rig has arrived by the job's own rule
        (``reached``), else where Farmhand's own drive goes for the rest: for a field the nearest
        point of its real edge from where the rig stands (live d30f1350225a420b-1: AutoDrive's
        "Field 90" marker on the road beside the field, the rig there, arrival not observed),
        else the target."""
        if hasattr(self.world, "await_stopped"):
            self.world.await_stopped(goal.vehicle_id)
        items = {i.id: i for i in self.world.snapshot()}
        root = items.get(goal.vehicle_id)
        if root is None:
            return goal.target
        rig = [items[i].position for i in goal.rig_ids if i in items]
        if reached(self._outline, goal.target, root.position, rig, self._area):
            return None
        if len(self._outline) >= 3:
            return _edge_point(self._outline, root.position)[0] or goal.target
        return goal.target

    def _drive(self, goal, owner, area):
        """A long trip (over LONG_LEG_M): GIANTS' own navigation first, which knows the map's roads
        and bridges (the user, 2026-10-30: use the pieces already made; Farmhand's own search took
        minutes to find no route across the river), then Farmhand's own drive for what is left
        of it. A short one, or one GIANTS cannot do: Farmhand's own drive; with no route of its
        own (live 6d9dfa2f4a6e453c-1), GIANTS' GOTO for the long leg (``_long_leg``)."""
        if lab_own_nav_only():
            log.info("navigation proof: Farmhand-owned drive only to %s", goal.target)
            self.world.farm_drive(goal.vehicle_id, goal.target, owner, area)
            return
        items = self.world.snapshot()
        here = next((i.position for i in items if i.id == goal.vehicle_id), None)
        loaded = any(i.belted_to in goal.rig_ids for i in items)
        rest = None
        marker = None
        giants_failed = False
        start_contact = False
        route_done = False
        if here is not None and hasattr(self.world, "map_id") and hasattr(self.world, "farm"):
            farm = self.world.farm()
            scope = (f"{farm.savegame_dir}#{farm.savegame}" if farm is not None
                     and farm.savegame_dir and farm.savegame else None)
            chosen = self.route_memory.choose(self.world.map_id(), scope, here, goal.target) if scope else None
            if chosen is not None and chosen[0] in self._route_failed:
                chosen = None
            log.info("learned route lookup path=%s map=%s scope=%s start=%s target=%s selected=%s",
                     self.route_memory.path, self.world.map_id(), scope, here, goal.target,
                     chosen[0] if chosen else None)
            if chosen is not None:
                route_id, points = chosen
                try:
                    # Walking samples describe a centre line, not parking poses.
                    # Stop once at its far end; the game-side driver refines the
                    # entire line for the current rig and can make a wide turn.
                    via = points[:-1]
                    self.world.farm_drive(goal.vehicle_id, points[-1], owner, via=via)
                    observed = next((i.position for i in self.world.snapshot() if i.id == goal.vehicle_id), None)
                    if observed is None or math.dist(observed, points[-1]) > ARRIVE_M:
                        raise GoalError("LEARNED_ROUTE_UNVERIFIED")
                    here = observed
                    self._route_used = route_id
                    route_done = True
                    rest = self._rest_of_leg(goal)
                    if rest is None:
                        return
                    log.info("drove learned farm route %s; %.0f m remain", route_id, math.dist(here, rest))
                except Exception as failure:
                    if getattr(failure, "code", None) in {"STOPPED", "CANCELLED"}:
                        raise
                    self._route_failed.add(route_id)
                    self.route_memory.reject(route_id)
                    log.info("learned farm route %s ended at %s; replanning", route_id,
                             getattr(failure, "code", failure))
        if loaded and not route_done and hasattr(self.world, "navigate"):
            # GIANTS' own navigation first: the mod takes the belted cargo out of its AI obstacles
            # while it drives (RELEASE_BACKLOG "Loaded transport standalone"); AutoDrive only if
            # GIANTS cannot do it.
            try:
                log.info("DRIVE with belted cargo: GIANTS' navigation first")
                self._long_leg(goal.vehicle_id, goal.target, owner)
                rest = self._rest_of_leg(goal)
                if rest is None:
                    return
                # GIANTS took it most of the way: the rest below is a local drive.
            except Exception as failure:
                giants_failed = True
                start_contact = getattr(failure, "code", None) in {
                    "NAVIGATE_START_CONTACT", "NAVIGATE_STUCK_AT_START"}
                log.info("GIANTS' navigation with belted cargo: %s; trying another route",
                         getattr(failure, "code", failure))
        if loaded and giants_failed and not start_contact and here is not None \
                and math.dist(here, goal.target) > LONG_LEG_M:
            marker = self._loaded_marker(goal)
        if marker is not None:
            log.info("DRIVE %.0f m with belted cargo: AutoDrive to marker %s", math.dist(here, goal.target), marker)
            self._onto_ad_network(goal, here, marker, owner)
            self.world.travel(goal.vehicle_id, marker, owner)
            rest = self._rest_of_leg(goal)
            if rest is None:
                return
            log.info("AutoDrive ended short; Farmhand's own drive for the rest, to %s", rest)
        elif not loaded and here is not None and math.dist(here, goal.target) > LONG_LEG_M \
                and hasattr(self.world, "navigate"):
            try:
                log.info("DRIVE %.0f m: GIANTS' navigation for the long leg", math.dist(here, goal.target))
                self._long_leg(goal.vehicle_id, goal.target, owner)
            except Exception as failure:
                # Boxed in where GIANTS cannot start, even from the nearest open ground: the
                # blockers moved or the teleport recovery (``execute``), never Farmhand's own
                # search over the whole trip (live bae54215f79642a0: minutes across the river).
                if getattr(failure, "code", None) in GOTO_START_CODES | FALLBACK_CODES:
                    raise
                log.info("GIANTS' navigation: %s; Farmhand's own drive", getattr(failure, "code", failure))
            else:
                rest = self._rest_of_leg(goal)
                if rest is None:
                    return
                log.info("GIANTS' navigation ended short; Farmhand's own drive for the rest, to %s", rest)
        target = rest or goal.target
        try:
            self.world.farm_drive(goal.vehicle_id, target, owner, area)
        except Exception as failure:
            if start_contact or getattr(failure, "code", None) not in FALLBACK_CODES \
                    or not hasattr(self.world, "navigate"):
                raise
            log.info("DRIVE by Farmhand's own drive: %s; GIANTS GOTO to %s", failure.code, target)
            self._long_leg(goal.vehicle_id, target, owner)

    def _move_blockers(self, rig_ids, farm_id, owner) -> int:
        """The farm's parked vehicles within BLOCK_RADIUS_M of the rig, nearest first (at most
        MAX_BLOCKERS), each with what is attached to it driven onto open ground by Farmhand's own
        drive (``reposition``), stopped and released. How many moved."""
        items = list(self.world.snapshot())
        mine = [i.position for i in items if i.id in rig_ids]

        def near(i):
            return min(math.dist(i.position, p) for p in mine)

        loose_ok = getattr(self.world, "teleport_recovery", False) and hasattr(self.world, "teleport")

        def loose(i):
            # A loose implement (dropped, hitched to nothing) is teleported aside when the player
            # allows teleporting (live 2026-10-31: one Farmhand dropped blocked its own worker).
            return i.category not in DRIVEN and i.attached_to is None and loose_ok

        def why_not(i):
            return [reason for reason, bad in (
                ("not driven", i.category not in DRIVEN and not loose(i)), ("another farm's", i.farm_id != farm_id),
                # A driven vehicle's attached_to names what it tows (live 00:27: the 3650 and its
                # trailer passed over as "attached"); belted onto a transport it is cargo.
                ("belted onto a transport", i.belted_to is not None),
                ("moving", not i.stopped), (f"reserved by {i.reserved_by}", i.reserved_by is not None)) if bad]

        nearby = sorted((i for i in items if i.id not in rig_ids and i.position is not None
                         and near(i) <= BLOCK_RADIUS_M), key=near)
        for i in nearby:
            log.info("near the boxed-in rig: %s (%s) %.1f m%s", i.name, i.id, near(i),
                     "" if not why_not(i) else " - not moved: " + ", ".join(why_not(i)))
        blockers = [i for i in nearby if not why_not(i)]
        moved = 0
        for n, blocker in enumerate(blockers[:MAX_BLOCKERS]):
            if loose(blocker):
                other = f"{owner}-clear{n}"
                if not self.world.reserve_rig((blocker.id,), other, loose=True):
                    continue
                try:
                    spot = self._yard_spot(blocker, mine[0])
                    log.info("the %s (loose) blocks the way: teleported to (%.1f, %.1f)", blocker.name, spot.x, spot.z)
                    self.world.teleport(blocker.id, other, 0.0, spot)
                    moved += 1
                except Exception as failure:
                    log.info("the %s could not be teleported: %s", blocker.name, getattr(failure, "code", failure))
                finally:
                    self.world.release(other)
                continue
            ids, by_id = [blocker.id], {i.id: i for i in items}
            while True:
                # Hitched either way (attached_to names the other unit on both sides), or belted on.
                more = [i.id for i in items if i.id not in ids and (
                    i.attached_to in ids or i.belted_to in ids
                    or any(by_id[m].attached_to == i.id for m in ids if m in by_id))]
                if not more:
                    break
                ids += more
            other = f"{owner}-clear{n}"
            if not self.world.reserve_rig(tuple(ids), other):
                log.info("the %s blocks the way but cannot be taken (reserved, occupied or busy)", blocker.name)
                continue
            try:
                log.info("the %s blocks the way: driven onto open ground", blocker.name)
                self.world.reposition(blocker.id, other)
                moved += 1
            except Exception as failure:
                # Judged by where it is now: moved off far enough, the way may be open (live
                # 21a5e70740024850-1: both blockers driven most of the way, each ended short).
                now = next((i for i in self.world.snapshot() if i.id == blocker.id), None)
                gone = now is not None and math.dist(now.position, blocker.position) >= MOVED_ENOUGH_M
                log.info("the %s %s: %s", blocker.name, "moved off, short of its spot" if gone else "could not be moved",
                         getattr(failure, "code", failure))
                moved += 1 if gone else 0
            finally:
                try:
                    self.world.stop(blocker.id, other)
                finally:
                    self.world.release(other)
        return moved

    def clear_field(self, outline, keep_ids, farm_id, owner, margin=FIELD_CLEAR_MARGIN_M) -> int:
        """Before a field is handed to GIANTS' helper (the user, 2026-10-31: "it needs to scan the
        field and remove any loose blockers before they become an issue"): every loose implement on
        the field or within ``margin`` of its edge is set down just outside it, beyond the headland,
        when the player allows teleporting; it stays at the field ("some may need to stay at a field
        to complete the field work"). How many moved."""
        if len(outline) < 3 or not getattr(self.world, "teleport_recovery", False) or not hasattr(self.world, "teleport"):
            return 0
        items = list(self.world.snapshot())
        keep = set(keep_ids)
        loose = [i for i in items if i.id not in keep and i.category not in DRIVEN and i.attached_to is None
                 and i.belted_to is None and i.farm_id == farm_id and i.reserved_by is None and i.position is not None
                 and (_inside(outline, i.position) or (_edge_point(outline, i.position)[1] or 1e9) <= margin)]
        moved = 0
        for n, item in enumerate(loose):
            other = f"{owner}-field{n}"
            if not self.world.reserve_rig((item.id,), other, loose=True):
                continue
            try:
                spot = _beside_field(outline, item.position, margin + FIELD_SET_DOWN_M)
                log.info("field scan: the %s lies on the field; set down beside it (%.1f, %.1f)",
                         item.name, spot.x, spot.z)
                self.world.teleport(item.id, other, 0.0, spot)
                moved += 1
            except Exception as failure:
                log.info("field scan: the %s could not be moved: %s", item.name, getattr(failure, "code", failure))
            finally:
                self.world.release(other)
        return moved

    def _yard_spot(self, item, rig_position):
        """Where a loose implement in the way goes: the farm yard (the user, 2026-10-31: "i am
        going to end up with equipment all over the map, send it to the farm yard"), the farm
        building with the most other farm buildings within YARD_RADIUS_M; without one, LOOSE_ASIDE_M
        from the rig on the far side. Any open spot near there (the game picks it)."""
        from protocol.model import Pose
        places = [p for p in (self.world.places() if hasattr(self.world, "places") else ()) if p.position]
        if places:
            yard = max(places, key=lambda p: (sum(math.dist(p.position, q.position) <= YARD_RADIUS_M for q in places),
                                              -places.index(p)))
            return Pose(yard.position[0], yard.position[1], 0.0)
        rx, rz = rig_position
        bx, bz = item.position
        d = math.dist((rx, rz), (bx, bz)) or 1.0
        ux, uz = (bx - rx) / d, (bz - rz) / d
        return Pose(bx + ux * LOOSE_ASIDE_M, bz + uz * LOOSE_ASIDE_M, math.atan2(ux, uz))

    def _long_leg(self, vehicle_id, target, owner):
        """GIANTS' own GOTO for the long leg: it plans on the game's navigation map (roads,
        bridges) for the attached rig. Started from a cramped spot it is blocked at its start
        (live 7069876416d14b6d-1: the GMC beside the 3650, NAVIGATE_BLOCKED_AT_START), so the rig
        is first driven onto the nearest open ground by Farmhand's own drive, then GOTO again."""
        from protocol.model import Pose
        from adapters.world import StepFailed

        def goto():
            # Standing first (live 21a5e70740024850-2: GOTO straight after its own drive, NOT_STOPPED).
            if hasattr(self.world, "await_stopped"):
                self.world.await_stopped(vehicle_id)
            here = next(i for i in self.world.snapshot() if i.id == vehicle_id).position
            candidates = [target]
            if self._area > 0:
                # A building's reported centre can be inside its collision box.
                # The arrival rule already accepts a clear approach nearby.
                dx, dz = here[0] - target[0], here[1] - target[1]
                angle = math.atan2(dx, dz)
                radius = self._area * 0.75
                candidates = [(target[0] + math.sin(angle + offset) * radius,
                               target[1] + math.cos(angle + offset) * radius)
                              for offset in (0, math.pi/4, -math.pi/4, math.pi/2, -math.pi/2,
                                             3*math.pi/4, -3*math.pi/4, math.pi)]
            invalid = None
            for point in candidates:
                yaw = math.atan2(point[0] - here[0], point[1] - here[1])
                try:
                    self.world.navigate(vehicle_id, Pose(point[0], point[1], yaw), owner)
                    now = next(i for i in self.world.snapshot() if i.id == vehicle_id).position
                    if (math.dist(now, target) > self._area + ARRIVE_M
                            and math.dist(now, here) < MOVED_ENOUGH_M):
                        invalid = StepFailed("GOTO_ENDED")
                        continue
                    return
                except Exception as failure:
                    if getattr(failure, "code", None) != "NAVIGATE_GOTO_INVALID":
                        raise
                    invalid = failure
            if invalid is not None:
                raise invalid

        try:
            goto()
        except Exception as failure:
            if (getattr(failure, "code", None) not in GOTO_START_CODES
                    or getattr(failure, "code", None) == "NAVIGATE_GOTO_INVALID"
                    or not hasattr(self.world, "reposition")):
                raise
            log.info("GIANTS GOTO %s: the rig to open ground first", failure.code)
            self.world.reposition(vehicle_id, owner)
            goto()

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
        self._route_used = None
        self._route_failed = set()
        self._wanted = command.get("target") if isinstance(command.get("target"), dict) else None
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
            self._outline, self._area = outline, area
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
                try:
                    self._drive(goal, owner, area)
                except Exception as failure:
                    if lab_own_nav_only():
                        raise
                    # Boxed in by parked vehicles (live 140a6b52320b4587-1: the GMC rig against the
                    # 3650 and its trailer; no route, GIANTS blocked at its start, no way onto open
                    # ground): each is driven out of the way and the move resumes (the user, 2026-10-30:
                    # "if its a tractor have farmhand get in it and move it ... then resume").
                    code = getattr(failure, "code", None)
                    if code not in FALLBACK_CODES | GOTO_START_CODES or not hasattr(self.world, "reposition"):
                        raise
                    if self._move_blockers(ids, farm_id, owner):
                        try:
                            self._drive(goal, owner, area)
                            failure = None
                        except Exception as again:
                            failure = again
                    if failure is not None:
                        # Still no way out: the teleport recovery, when the player allows it (the user,
                        # 2026-10-30: "if farmhand still can not move it, it should teleport it and any
                        # attached items to an open area close by"), then the move again.
                        if not getattr(self.world, "teleport_recovery", False) or not hasattr(self.world, "teleport"):
                            raise failure
                        for step, min_m in enumerate(TELEPORT_STEPS_M):
                            log.info("no way out by driving (%s): teleport recovery, %.0f m out at least",
                                     getattr(failure, "code", failure), min_m)
                            self.world.teleport(goal.vehicle_id, owner, min_m)
                            try:
                                self._drive(goal, owner, area)
                                break
                            except Exception as again:
                                if (getattr(again, "code", None) not in FALLBACK_CODES | GOTO_START_CODES
                                        or step == len(TELEPORT_STEPS_M) - 1):
                                    raise
                                failure = again
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
        if status == "COMPLETE" and self._route_used is not None:
            self.route_memory.promote(self._route_used)
        return Result(status, reason, ids, tuple(states))
