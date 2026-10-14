"""The minimal physical-world port for AC-001; no real adapter is implemented.

See adapters/EVIDENCE.md for which upstream capability backs each operation.
"""
from typing import Protocol
from protocol.model import Destination, Equipment, Position


class World(Protocol):
    def snapshot(self) -> tuple[Equipment, ...]:
        """Return fresh immutable authoritative observations."""
        ...

    def player_position(self, farm_id: str) -> Position:
        """Resolve the authorized requester, or reject an unknown farm."""
        ...

    def destinations(self) -> tuple[Destination, ...]:
        """Return fresh routable delivery destinations (AutoDrive map markers)."""
        ...

    def reserve(self, ids: tuple[str, str], owner: str) -> bool:
        """Atomically bind a fresh owner to this pair and farm, or change nothing."""
        ...

    def approach(self, tractor_id: str, wagon_id: str, owner: str) -> None:
        """Recheck authority/compatibility; bring the tractor hitch to the wagon.

        No proven upstream provider exists (GAP); a real adapter must refuse."""
        ...

    def attach(self, tractor_id: str, wagon_id: str, owner: str) -> None:
        """Delegate bounded coupling; a normal return is not attachment evidence."""
        ...

    def deliver(self, tractor_id: str, destination_id: str, owner: str) -> None:
        """Recheck authority/compatibility; delegate bounded travel to a destination."""
        ...

    def stop(self, tractor_id: str, owner: str) -> bool:
        """Stop owned equipment; true only when the stop is confirmed."""
        ...

    def release(self, owner: str) -> None:
        """Release only the exact reserved pair after ownership and stop checks."""
        ...
