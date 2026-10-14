"""Synthetic world only: coordinates change instantly; no game APIs or physics."""
from dataclasses import replace
import math
from threading import RLock

from protocol.model import Equipment, Position


class MockWorld:
    def __init__(self, equipment, players):
        equipment = tuple(equipment)
        self.items: dict[str, Equipment] = {item.id: item for item in equipment}
        if len(self.items) != len(equipment):
            raise ValueError("Duplicate equipment IDs")
        self.players = dict(players)
        self._lock = RLock()

    def snapshot(self):
        with self._lock:
            return tuple(self.items.values())

    def player_position(self, farm_id):
        with self._lock:
            return self.players[farm_id]

    def reserve(self, ids, owner):
        with self._lock:
            if not owner or len(set(ids)) != 2 or any(i not in self.items for i in ids):
                return False
            pair = [self.items[i] for i in ids]
            if any(item.reserved_by is not None or item.busy or item.occupied
                   or not item.available or item.attached_to is not None
                   or not item.stopped for item in pair):
                return False
            if pair[0].farm_id != pair[1].farm_id:
                return False
            for item in pair:
                self.items[item.id] = replace(item, reserved_by=owner)
            return True

    def _owned(self, identifier, owner):
        item = self.items[identifier]
        if (item.reserved_by != owner or owner is None or not item.available
                or item.busy or item.occupied):
            raise ValueError("Equipment not available to request")
        return item

    def move(self, tractor_id, target: Position, owner):
        with self._lock:
            tractor = self._owned(tractor_id, owner)
            if tractor.category != "tractor" or "move" not in tractor.capabilities:
                raise ValueError("Movement unsupported")
            if len(target) != 2 or not all(math.isfinite(v) for v in target):
                raise ValueError("Invalid target")
            wagon = None
            if tractor.attached_to is not None:
                wagon = self._owned(tractor.attached_to, owner)
                if wagon.attached_to != tractor.id or wagon.farm_id != tractor.farm_id:
                    raise ValueError("Invalid attachment")
            # Mock-only teleport, never a substitute for real navigation.
            self.items[tractor.id] = replace(tractor, position=tuple(target), stopped=True)
            if wagon is not None:
                self.items[wagon.id] = replace(wagon, position=tuple(target), stopped=True)

    def attach(self, tractor_id, wagon_id, owner):
        with self._lock:
            tractor, wagon = self._owned(tractor_id, owner), self._owned(wagon_id, owner)
            if (tractor.category != "tractor" or wagon.category != "forage_wagon"
                    or "attach" not in tractor.capabilities
                    or not tractor.hitch or tractor.hitch != wagon.hitch
                    or tractor.power_kw < wagon.required_power_kw
                    or wagon.requires_pto and "pto" not in tractor.capabilities
                    or tractor.farm_id != wagon.farm_id
                    or tractor.attached_to is not None or wagon.attached_to is not None
                    or not tractor.stopped or not wagon.stopped
                    or math.dist(tractor.position, wagon.position) > 2):
                raise ValueError("Cannot attach")
            self.items[tractor.id] = replace(tractor, attached_to=wagon.id)
            self.items[wagon.id] = replace(wagon, attached_to=tractor.id)

    def stop(self, tractor_id, owner):
        with self._lock:
            tractor = self.items.get(tractor_id)
            if (tractor is None or tractor.reserved_by != owner or tractor.occupied
                    or "stop" not in tractor.capabilities):
                return False
            owned = [item for item in self.items.values() if item.reserved_by == owner]
            if any(item.occupied for item in owned):
                return False
            for item in owned:
                self.items[item.id] = replace(item, stopped=True)
            return True

    def release(self, owner):
        with self._lock:
            owned = [item for item in self.items.values() if item.reserved_by == owner]
            if any(not item.stopped for item in owned):
                raise ValueError("Cannot release moving equipment")
            for item in owned:
                self.items[item.id] = replace(item, reserved_by=None)
