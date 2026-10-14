"""The minimal physical-world port for AC-001; no real adapter is implemented."""
from typing import Protocol
from protocol.model import Equipment, Position


class World(Protocol):
    def snapshot(self) -> tuple[Equipment, ...]:
        """Return fresh immutable authoritative observations."""
        ...

    def player_position(self, farm_id: str) -> Position:
        """Resolve the authorized requester, or reject an unknown farm."""
        ...

    def reserve(self, ids: tuple[str, str], owner: str) -> bool:
        """Atomically reserve both free units, or neither. False means no mutation."""
        ...

    def move(self, tractor_id: str, target: Position, owner: str) -> None:
        """Delegate bounded movement; a normal return is not arrival evidence."""
        ...

    def attach(self, tractor_id: str, wagon_id: str, owner: str) -> None:
        """Delegate bounded coupling; a normal return is not attachment evidence."""
        ...

    def stop(self, tractor_id: str, owner: str) -> bool:
        """Stop owned equipment; true only when the stop is confirmed."""
        ...

    def release(self, owner: str) -> None:
        """Release only this owner's reservations, only for stopped equipment."""
        ...
