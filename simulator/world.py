"""Synthetic world only: coordinates change instantly; no game APIs or physics."""
from dataclasses import replace
import math
from threading import RLock

from protocol.model import Destination, Equipment


class MockWorld:
    def __init__(self, equipment, players, destinations=()):
        equipment, destinations = tuple(equipment), tuple(destinations)
        self.items: dict[str, Equipment] = {item.id: item for item in equipment}
        if len(self.items) != len(equipment):
            raise ValueError("Duplicate equipment IDs")
        self.destination_map: dict[str, Destination] = {d.id: d for d in destinations}
        if len(self.destination_map) != len(destinations):
            raise ValueError("Duplicate destination IDs")
        self.players = dict(players)
        self._lock = RLock()
        self._reservations: dict[str, tuple[str, tuple[str, str]]] = {}

    def snapshot(self):
        with self._lock:
            return tuple(self.items.values())

    def player_position(self, farm_id):
        with self._lock:
            return self.players[farm_id]

    def destinations(self):
        with self._lock:
            return tuple(self.destination_map.values())

    def reserve(self, ids, owner):
        with self._lock:
            if not owner or len(set(ids)) != 2 or any(i not in self.items for i in ids):
                return False
            if owner in self._reservations or any(x.reserved_by == owner for x in self.items.values()):
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
            self._reservations[owner] = (pair[0].farm_id, tuple(ids))
            return True

    def _owned(self, identifier, owner):
        item = self.items[identifier]
        reservation = self._reservations.get(owner)
        if (not reservation or identifier not in reservation[1]
                or item.farm_id != reservation[0]
                or item.reserved_by != owner or not item.available
                or item.busy or item.occupied):
            raise ValueError("Equipment not available to request")
        return item

    @staticmethod
    def _compatible(tractor, wagon):
        # Independent mock-side check at mutation time, under the world lock.
        if (tractor.category != "tractor" or wagon.category != "forage_wagon"
                or not {"move", "attach", "stop"} <= tractor.capabilities
                or not tractor.hitches & wagon.hitches
                or not math.isfinite(tractor.power_kw)
                or not math.isfinite(wagon.required_power_kw)
                or not tractor.power_kw >= wagon.required_power_kw > 0
                or wagon.requires_pto and "pto" not in tractor.capabilities
                or tractor.farm_id != wagon.farm_id):
            raise ValueError("Incompatible equipment")

    def _reserved_pair(self, tractor_id, wagon_id, owner):
        tractor = self._owned(tractor_id, owner)
        if (tractor_id, wagon_id) != self._reservations[owner][1]:
            raise ValueError("Not the reserved pair")
        wagon = self._owned(wagon_id, owner)
        self._compatible(tractor, wagon)
        return tractor, wagon

    def approach(self, tractor_id, wagon_id, owner):
        with self._lock:
            tractor, wagon = self._reserved_pair(tractor_id, wagon_id, owner)
            if tractor.attached_to is not None or wagon.attached_to is not None:
                raise ValueError("Approach requires an unattached pair")
            # Mock-only teleport, never a substitute for real navigation or alignment.
            self.items[tractor.id] = replace(tractor, position=wagon.position, stopped=True)

    def deliver(self, tractor_id, destination_id, owner):
        with self._lock:
            ids = self._reservations.get(owner, (None, (None, None)))[1]
            tractor, wagon = self._reserved_pair(tractor_id, ids[1], owner)
            if tractor.attached_to != wagon.id or wagon.attached_to != tractor.id:
                raise ValueError("Invalid attachment")
            destination = self.destination_map.get(destination_id)
            if destination is None or not all(math.isfinite(v) for v in destination.position):
                raise ValueError("Unknown destination")
            # Mock-only teleport of the coupled pair; AutoDrive does real routing.
            for item in (tractor, wagon):
                self.items[item.id] = replace(item, position=destination.position, stopped=True)

    def attach(self, tractor_id, wagon_id, owner):
        with self._lock:
            tractor, wagon = self._owned(tractor_id, owner), self._owned(wagon_id, owner)
            self._compatible(tractor, wagon)
            if (tractor.attached_to is not None or wagon.attached_to is not None
                    or not tractor.stopped or not wagon.stopped
                    or math.dist(tractor.position, wagon.position) > 2):
                raise ValueError("Cannot attach")
            self.items[tractor.id] = replace(tractor, attached_to=wagon.id)
            self.items[wagon.id] = replace(wagon, attached_to=tractor.id)

    def stop(self, tractor_id, owner):
        with self._lock:
            reservation = self._reservations.get(owner)
            if not reservation or tractor_id != reservation[1][0]:
                return False
            tractor = self.items.get(tractor_id)
            if (tractor is None or tractor.reserved_by != owner or tractor.occupied
                    or "stop" not in tractor.capabilities):
                return False
            owned = [self.items.get(identifier) for identifier in reservation[1]]
            if any(item is None or item.occupied or item.reserved_by != owner
                   or item.farm_id != reservation[0] for item in owned):
                return False
            for item in owned:
                self.items[item.id] = replace(item, stopped=True)
            return True

    def release(self, owner):
        with self._lock:
            reservation = self._reservations.get(owner)
            if not reservation:
                return
            owned = [self.items.get(identifier) for identifier in reservation[1]]
            if any(item is None or not item.stopped or item.reserved_by != owner
                   or item.farm_id != reservation[0] for item in owned):
                raise ValueError("Cannot release moving or changed equipment")
            for item in owned:
                self.items[item.id] = replace(item, reserved_by=None)
            del self._reservations[owner]
