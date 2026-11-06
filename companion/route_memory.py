"""Learned, map-scoped driving lines for Farmhand travel.

These are hints, not permission to cross an obstacle. Each leg is still driven
and checked by the game-side whole-rig controller before the next leg starts.

A line is taught three ways, all from observed poses (never guessed map points):
the player drives it while Farmhand records ("record this route"), the player
leads a follower ("follow me and remember the route": the leader's line, never the
follower's), or a Farmhand journey that ended at its goal ("... and remember the way").

Quality and validation are kept apart: ``quality`` says how the line was observed
(recorded, clean: no unplanned stop or recovery), ``validated`` lists the rigs that
drove it to a verified arrival, per direction. Proven forward is not proven in reverse;
proven with a pickup is not proven with a lowboy.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

log = logging.getLogger("farmhand")

MAX_ROUTES = 200            # the store never grows without bound: the oldest unnamed, unvalidated go first
RIG_MARGIN_WIDTH_M = 0.3    # a rig this much wider than any validated one is not validated by them
RIG_MARGIN_LENGTH_M = 2.0   # nor this much longer
DEFAULT_SPEED_KMH = 20.0    # expected travel speed on a line with no observed speed
UNVALIDATED_FACTOR = 1.3    # a line not validated for this rig and direction is expected slower (it may fail)


def _length(line) -> float:
    return sum(math.dist(a, b) for a, b in zip(line, line[1:]))


def _fits(rig, proven) -> bool:
    """Whether ``rig`` (width, length) is no bigger than a rig that drove the line."""
    return (rig is not None and proven is not None
            and rig[0] <= proven[0] + RIG_MARGIN_WIDTH_M and rig[1] <= proven[1] + RIG_MARGIN_LENGTH_M)


class RouteMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data.get("routes", []) if isinstance(data, dict) and isinstance(data.get("routes"), list) else []

    def _write(self, routes):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "routes": routes}, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def add(self, map_id: str, savegame_dir: str, points, *, verified=False, name=None, source=None,
            quality="recorded", poses=None, rig=None, meta=None):
        """Remember an observed line; use its real map and save identity. ``poses``: per point
        (heading, km/h, reversing); ``rig``: the (width, length) that drove it; ``meta``: what was
        observed of the drive (length, seconds, stops, recovery)."""
        line = [[float(x), float(z)] for x, z in points]
        if not map_id or not savegame_dir or len(line) < 2 or not all(
                math.isfinite(v) for point in line for v in point):
            raise ValueError("invalid route")
        if _length(line) < 10:
            raise ValueError("route too short")
        entry = {"id": uuid4().hex, "map": map_id, "save": savegame_dir,
                 "points": line, "verified": bool(verified), "failures": 0,
                 "created": time.time()}
        if name:
            entry["name"] = str(name)[:60]
        if source:
            entry["source"] = source
            entry["quality"] = quality
            # A taught line is driven the way it was taught; the other way is its own proof.
            entry["validated"] = {"forward": [list(rig)] if verified and rig else [], "reverse": []}
        if poses is not None and len(poses) == len(line):
            entry["poses"] = [[round(float(h), 4), round(float(s), 1), int(bool(r))] for h, s, r in poses]
        if rig is not None:
            entry["rig"] = [round(float(rig[0]), 2), round(float(rig[1]), 2)]
        if meta:
            entry["meta"] = meta
        with self._lock:
            routes = self._read()
            if name:
                routes = [r for r in routes if not (r.get("name", "").casefold() == str(name).casefold()
                                                    and r.get("map") == map_id and r.get("save") == savegame_dir)]
            routes.append(entry)
            if len(routes) > MAX_ROUTES:
                spare = sorted((r for r in routes if not r.get("name") and not r.get("verified")),
                               key=lambda r: r.get("created", 0))
                drop = {r["id"] for r in spare[:len(routes) - MAX_ROUTES]}
                routes = [r for r in routes if r["id"] not in drop]
            self._write(routes)
        return entry["id"]

    def _update(self, route_id: str, change) -> bool:
        with self._lock:
            routes = self._read()
            for route in routes:
                if route.get("id") == route_id:
                    change(route)
                    self._write(routes)
                    return True
        return False

    def promote(self, route_id: str, reverse: bool = False, rig=None):
        """Mark a line proven only after the whole rig actually drove it, in that direction."""
        def change(route):
            route["failures"] = 0
            if "source" not in route:                      # a walked line from before quality was kept
                route["verified"] = True
                return
            validated = route.setdefault("validated", {"forward": [], "reverse": []})
            rigs = validated.setdefault("reverse" if reverse else "forward", [])
            if rig is not None and not any(_fits(rig, tuple(r)) for r in rigs):
                rigs.append([round(float(rig[0]), 2), round(float(rig[1]), 2)])
            route["verified"] = route.get("verified", False) or not reverse
            route["quality"] = "validated" if validated.get("forward") else route.get("quality", "recorded")
        return self._update(route_id, change)

    def reject(self, route_id: str):
        """Keep a taught line as user data, but stop automatic retries after repeat failures."""
        def change(route):
            try:
                failures = max(0, int(route.get("failures", 0)))
            except (TypeError, ValueError):
                failures = 0
            route["failures"] = failures + 1
            if "source" in route:
                route["quality"] = "degraded" if route["failures"] < 2 else "unavailable"
        return self._update(route_id, change)

    def named(self, map_id: str, savegame_dir: str, name: str):
        """The route of this save called ``name`` (case and spacing ignored), else None."""
        wanted = " ".join(str(name).casefold().split())
        for route in self._read():
            if (route.get("map") == map_id and route.get("save") == savegame_dir
                    and " ".join(route.get("name", "").casefold().split()) == wanted):
                return route
        return None

    def rename(self, route_id: str, name: str) -> bool:
        return self._update(route_id, lambda route: route.__setitem__("name", str(name)[:60]))

    def delete(self, route_id: str) -> bool:
        with self._lock:
            routes = self._read()
            kept = [r for r in routes if r.get("id") != route_id]
            if len(kept) == len(routes):
                return False
            self._write(kept)
            return True

    def listed(self, map_id: str, savegame_dir: str):
        return [r for r in self._read() if r.get("map") == map_id and r.get("save") == savegame_dir]

    def choose(self, map_id: str, savegame_dir: str, start, target, rig=None):
        """A line is useful only from near one end toward a goal near its other end.

        Returns (route id, points in driving order, reversed) or None. Among useful lines the one
        expected to finish soonest: the way onto it, along it at its observed speed and on to the
        goal, slower where it is not validated for this rig (``rig``: width, length) in this
        direction. A taught line is used against its taught direction only as an unvalidated hint
        (it may be one-way); crop and obstacle clearance remain the game-side controller's decision.
        """
        options = []
        for route in self._read():
            if route.get("map") != map_id or route.get("save") != savegame_dir:
                continue
            failures = route.get("failures", 0)
            if not route.get("verified") and isinstance(failures, int) and failures >= 2:
                continue
            if route.get("quality") == "unavailable":
                continue
            try:
                line = [tuple(map(float, p)) for p in route["points"]]
                if len(line) < 2 or not all(math.isfinite(v) for p in line for v in p):
                    continue
                length = _length(line)
                speeds = [float(p[1]) for p in route.get("poses", ()) if float(p[1]) > 1.0]
                speed = (sum(speeds) / len(speeds) if speeds else DEFAULT_SPEED_KMH) / 3.6
                for reverse, ordered in ((False, line), (True, line[::-1])):
                    entry = math.dist(start, ordered[0])
                    exit_distance = math.dist(target, ordered[-1])
                    direct = math.dist(start, target)
                    if not (entry <= 150 and exit_distance <= 120 and direct - exit_distance >= 8
                            and entry + length + exit_distance <= direct * 1.5 + 15):
                        continue
                    if "source" in route:
                        proven = route.get("validated", {}).get("reverse" if reverse else "forward", [])
                        valid = rig is not None and any(_fits(rig, tuple(r)) for r in proven)
                    else:
                        valid = bool(route.get("verified"))      # a walked line: either way, as before
                    seconds = (entry + exit_distance) / (DEFAULT_SPEED_KMH / 3.6) + length / speed
                    options.append((seconds * (1.0 if valid else UNVALIDATED_FACTOR), route["id"], ordered, reverse))
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        if not options:
            return None
        _, route_id, line, reverse = min(options, key=lambda o: (o[0], o[1]))
        return route_id, line, reverse


# ---------------------------------------------------------------- recording
SAMPLE_S = 0.25          # observation period while recording
MIN_STEP_M = 0.3         # samples nearer than this to the last kept add nothing (standing still)
TURN_KEEP_RAD = math.radians(6)    # a heading change this large since the last kept point is kept
SIMPLIFY_M = 0.4         # straight runs kept within this of the driven line
MAX_GAP_M = 30.0         # but never a gap longer than this between kept points
STOP_KMH = 1.0           # slower than this is standing
STOP_S = 4.0             # standing this long (not at the end) is a stop
MAX_SAMPLES = 40000      # ~2.8 h at SAMPLE_S: a forgotten recording never grows without bound


def rig_extent(items, vehicle_id):
    """(width, length) of the vehicle and everything attached or belted to it, in the vehicle's
    heading frame, from each unit's GIANTS size box; None when the vehicle is unobserved."""
    observed = {i.id: i for i in items}
    root = observed.get(vehicle_id)
    if root is None or getattr(root, "heading", None) is None:
        return None
    members, frontier = {root.id}, [root.id]
    while frontier:
        current = frontier.pop()
        for item in observed.values():
            if item.id in members:
                continue
            if current in (getattr(item, "attached_to", None), getattr(item, "belted_to", None)) \
                    or getattr(observed[current], "attached_to", None) == item.id:
                members.add(item.id)
                frontier.append(item.id)
    fx, fz = math.sin(root.heading), math.cos(root.heading)
    along, across = [], []
    for unit in (observed[m] for m in members):
        size = getattr(unit, "size", None)
        length, width, offset = (size[0], size[1], size[3]) if size else (2.0, 2.0, 0.0)
        heading = unit.heading if getattr(unit, "heading", None) is not None else root.heading
        ux, uz = math.sin(heading), math.cos(heading)
        cx, cz = unit.position[0] + ux * offset, unit.position[1] + uz * offset
        for a in (-length / 2, length / 2):
            for b in (-width / 2, width / 2):
                px, pz = cx + ux * a + uz * b, cz + uz * a - ux * b
                dx, dz = px - root.position[0], pz - root.position[1]
                along.append(dx * fx + dz * fz)
                across.append(dx * fz - dz * fx)
    return round(max(across) - min(across), 2), round(max(along) - min(along), 2)


def simplify(samples):
    """The kept points of a recorded drive: its ends, every turn (TURN_KEEP_RAD), every change of
    driving direction and every stop, and between those the fewest points that keep the line within
    SIMPLIFY_M of what was driven, no gap longer than MAX_GAP_M. ``samples``: (x, z, heading, km/h,
    reversing) in order. Returns the kept samples."""
    if len(samples) <= 2:
        return list(samples)
    keep = {0, len(samples) - 1}
    last = 0
    for i in range(1, len(samples) - 1):
        a, b = samples[last], samples[i]
        turned = abs((b[2] - a[2] + math.pi) % (2 * math.pi) - math.pi) >= TURN_KEEP_RAD
        flipped = b[4] != samples[i - 1][4]
        stopped = (b[3] < STOP_KMH) != (samples[i - 1][3] < STOP_KMH)
        if turned or flipped or stopped:
            keep.add(i)
            last = i

    def chord(i, j):
        if j - i < 2:
            return
        (ax, az), (bx, bz) = samples[i][:2], samples[j][:2]
        dx, dz = bx - ax, bz - az
        span = math.hypot(dx, dz)
        worst, at = 0.0, None
        for k in range(i + 1, j):
            px, pz = samples[k][:2]
            d = abs(dx * (az - pz) - dz * (ax - px)) / span if span > 1e-6 else math.hypot(px - ax, pz - az)
            if d > worst:
                worst, at = d, k
        if worst <= SIMPLIFY_M:
            if span <= MAX_GAP_M:
                return
            at = (i + j) // 2
        keep.add(at)
        chord(i, at)
        chord(at, j)

    marks = sorted(keep)
    for i, j in zip(marks, marks[1:]):
        chord(i, j)
    return [samples[i] for i in sorted(keep)]


class RouteRecorder:
    """One recording at a time: the observed poses of a vehicle (``vehicle_id``), or of whichever
    vehicle the player sits in, sampled every SAMPLE_S from the game's own observations. Passive:
    it never commands, reserves or stops anything, so it runs beside any job."""

    def __init__(self, world, memory: RouteMemory):
        self.world, self.memory = world, memory
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.active = None            # the recording: dict, or None

    def start(self, source: str, vehicle_id=None, name=None) -> str:
        with self._lock:
            if self.active is not None:
                return "ROUTE_ALREADY_RECORDING"
            self.active = {"source": source, "vehicle": vehicle_id, "name": name, "samples": [],
                           "paused": False, "started": time.monotonic(), "rig": None, "vehicle_name": None}
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="farmhand-route-recorder")
            self._thread.start()
        log.info("route recording started: %s vehicle=%s name=%s", source, vehicle_id or "the player's", name)
        return "ROUTE_RECORDING"

    def pause(self, paused: bool) -> str:
        with self._lock:
            if self.active is None:
                return "ROUTE_NOT_RECORDING"
            self.active["paused"] = paused
        return "ROUTE_PAUSED" if paused else "ROUTE_RESUMED"

    def discard(self) -> str:
        recording = self._end()
        return "ROUTE_DISCARDED" if recording is not None else "ROUTE_NOT_RECORDING"

    def finish(self, name=None, clean=None, recovery=(), verified_rig=None):
        """End the recording and keep it: (notice, route id or None). ``clean``: the caller's verdict
        on the journey (None: judged here from the samples alone); ``recovery``: what had to be done
        on the way (moved blockers, a teleport); ``verified_rig``: the rig that arrived, verified."""
        recording = self._end()
        if recording is None:
            return "ROUTE_NOT_RECORDING", None
        samples = recording["samples"]
        if len(samples) < 2:
            return "ROUTE_TOO_SHORT", None
        kept = simplify(samples)
        points = [(s[0], s[1]) for s in kept]
        if _length(points) < 10:
            return "ROUTE_TOO_SHORT", None
        stops = self._stops(samples)
        seconds = samples[-1][5] - samples[0][5]
        reverse_m = sum(math.dist(a[:2], b[:2]) for a, b in zip(samples, samples[1:]) if b[4])
        judged = not stops and not recovery
        quality = "clean" if (judged if clean is None else (clean and judged)) else "recorded"
        meta = {"arrived": verified_rig is not None, "length_m": round(_length(points), 1), "seconds": round(seconds, 1), "stops": stops,
                "reverse_m": round(reverse_m, 1), "recovery": list(recovery),
                "vehicle": recording["vehicle_name"], "samples": len(samples)}
        try:
            farm = self.world.farm()
            scope = f"{farm.savegame_dir}#{farm.savegame}" if farm and farm.savegame_dir and farm.savegame else None
            route_id = self.memory.add(self.world.map_id(), scope, points, verified=verified_rig is not None,
                                       name=name or recording["name"], source=recording["source"], quality=quality,
                                       poses=[(s[2], s[3], s[4]) for s in kept],
                                       rig=verified_rig or recording["rig"], meta=meta)
        except (ValueError, OSError, AttributeError) as error:
            log.info("route not saved: %s", error)
            return "ROUTE_NOT_SAVED", None
        log.info("route %s saved: %s %s, %d of %d samples kept, %s", route_id, recording["source"], quality,
                 len(points), len(samples), meta)
        return ("ROUTE_SAVED_CLEAN" if quality == "clean" else "ROUTE_SAVED"), route_id

    @staticmethod
    def _stops(samples):
        """Standing still STOP_S or longer anywhere but before the first motion (a planner's start)
        or at the end: [(x, z, seconds)]."""
        stops, since, moved = [], None, False
        for s in samples:
            if s[3] < STOP_KMH:
                since = since or (s if moved else None)
            else:
                if since is not None and s[5] - since[5] >= STOP_S:
                    stops.append((round(since[0], 1), round(since[1], 1), round(s[5] - since[5], 1)))
                since, moved = None, True
        return stops

    def _end(self):
        with self._lock:
            recording, self.active = self.active, None
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2 * SAMPLE_S + 1.0)
        return recording

    def _vehicle(self, items, recording):
        if recording["vehicle"] is not None:
            return next((i for i in items if i.id == recording["vehicle"]), None)
        return next((i for i in items if i.ridden), None)

    def _run(self):
        last = None
        while not self._stop.wait(SAMPLE_S):
            with self._lock:
                recording = self.active
            if recording is None:
                return
            try:
                items = tuple(self.world.snapshot())
            except Exception:            # a save/menu transition: no observation, nothing recorded
                continue
            vehicle = self._vehicle(items, recording)
            if vehicle is None or vehicle.heading is None or recording["paused"]:
                last = None              # a gap: the next sample does not imply motion across it
                continue
            now = time.monotonic()
            x, z = vehicle.position
            samples = recording["samples"]
            if recording["rig"] is None:
                recording["rig"] = rig_extent(items, vehicle.id)
                recording["vehicle_name"] = vehicle.name
            if last is not None:
                dt = max(1e-3, now - last[5])
                step = math.dist((x, z), last[:2])
                kmh = 0.0 if vehicle.stopped else step / dt * 3.6
                dx, dz = x - last[0], z - last[1]
                reversing = step > 0.05 and dx * math.sin(vehicle.heading) + dz * math.cos(vehicle.heading) < 0
                if step < MIN_STEP_M and kmh >= STOP_KMH:
                    continue             # creeping: wait until it has moved enough to measure
                sample = (x, z, vehicle.heading, kmh, reversing if step > 0.05 else last[4], now)
            else:
                sample = (x, z, vehicle.heading, 0.0, False, now)
            if len(samples) < MAX_SAMPLES:
                samples.append(sample)
            last = sample
