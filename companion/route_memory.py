"""Player-taught, map-scoped driving lines for Farmhand transport.

These are hints, not permission to cross an obstacle. Each leg is still driven
and checked by the game-side whole-rig controller before the next leg starts.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from uuid import uuid4


class RouteMemory:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data.get("routes", []) if isinstance(data, dict) and isinstance(data.get("routes"), list) else []

    def add(self, map_id: str, savegame_dir: str, points, *, verified=False):
        """Remember an observed line; use its real map and save identity."""
        line = [[float(x), float(z)] for x, z in points]
        if not map_id or not savegame_dir or len(line) < 2 or not all(
                math.isfinite(v) for point in line for v in point):
            raise ValueError("invalid route")
        if sum(math.dist(a, b) for a, b in zip(line, line[1:])) < 10:
            raise ValueError("route too short")
        routes = self._read()
        entry = {"id": uuid4().hex, "map": map_id, "save": savegame_dir,
                 "points": line, "verified": bool(verified), "failures": 0}
        routes.append(entry)
        self._write(routes)
        return entry["id"]

    def _write(self, routes):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "routes": routes}, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def promote(self, route_id: str):
        """Mark a line proven only after the whole rig actually drove it."""
        routes = self._read()
        for route in routes:
            if route.get("id") == route_id:
                route["verified"] = True
                route["failures"] = 0
                self._write(routes)
                return True
        return False

    def reject(self, route_id: str):
        """Keep a taught line as user data, but stop automatic retries after repeat failures."""
        routes = self._read()
        for route in routes:
            if route.get("id") == route_id:
                try:
                    failures = max(0, int(route.get("failures", 0)))
                except (TypeError, ValueError):
                    failures = 0
                route["failures"] = failures + 1
                self._write(routes)
                return True
        return False

    def choose(self, map_id: str, savegame_dir: str, start, target):
        """A line is useful only from near one end toward a goal near its other end.

        The result is in driving order, including the endpoint nearest the rig.
        The same learned line works both ways. Crop and obstacle clearance remain
        the game-side controller's decision.
        """
        options = []
        for route in self._read():
            if route.get("map") != map_id or route.get("save") != savegame_dir:
                continue
            failures = route.get("failures", 0)
            if not route.get("verified") and isinstance(failures, int) and failures >= 2:
                continue
            try:
                line = [tuple(map(float, p)) for p in route["points"]]
                if len(line) < 2 or not all(math.isfinite(v) for p in line for v in p):
                    continue
                length = sum(math.dist(a, b) for a, b in zip(line, line[1:]))
                for ordered in (line, line[::-1]):
                    entry = math.dist(start, ordered[0])
                    exit_distance = math.dist(target, ordered[-1])
                    direct = math.dist(start, target)
                    if (entry <= 150 and exit_distance <= 120 and direct - exit_distance >= 8
                            and entry + length + exit_distance <= direct * 1.5 + 15):
                        options.append((entry + exit_distance, not route.get("verified", False), route["id"], ordered))
            except (KeyError, TypeError, ValueError):
                continue
        if not options:
            return None
        _, _, route_id, line = min(options)
        return route_id, line
