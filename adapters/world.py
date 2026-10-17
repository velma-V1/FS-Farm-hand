"""The minimal physical-world port for AC-001. Real adapter: companion/bridge.py
FS25World; mock: simulator/world.py.

See adapters/EVIDENCE.md for which upstream capability backs each operation.
"""
from typing import Protocol
from protocol.model import Destination, Equipment, HitchPlan, Pose, Position


class StepFailed(RuntimeError):
    """A physical step was refused or failed and ended in a known state."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class StepUnverified(Exception):
    """A physical step's outcome cannot be established (unknown state, lost link)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class World(Protocol):
    def snapshot(self) -> tuple[Equipment, ...]:
        """Return fresh immutable authoritative observations."""
        ...

    def player_position(self, farm_id: str) -> Position:
        """Resolve the authorized requester, or reject an unknown farm."""
        ...

    def destinations(self) -> tuple[Destination, ...]:
        """Return fresh AutoDrive map markers (empty without AutoDrive)."""
        ...

    def places(self) -> tuple[Destination, ...]:
        """Return the farm's own named placeables (buildings), fresh."""
        ...

    def map_id(self) -> str | None:
        """Return the loaded map's identity (missionInfo.mapId), or None if unknown."""
        ...

    def reserve(self, ids: tuple[str, str], owner: str) -> bool:
        """Atomically bind a fresh owner to this pair and farm, or change nothing."""
        ...

    # HITCH steps. Each rechecks authority, reservation and compatibility at action
    # time, is bounded, and its normal return is never evidence: the executor
    # verifies every step from a fresh snapshot. Sources: adapters/EVIDENCE.md.

    def travel(self, tractor_id: str, destination_id: str, owner: str) -> None:
        """TRAVEL_NEAR: AutoDrive marker travel of the reserved, unattached tractor."""
        ...

    def navigate(self, tractor_id: str, pose: Pose, owner: str) -> None:
        """TRAVEL_NEAR or DELIVER without AutoDrive: GIANTS AIJobGoTo of the reserved
        tractor's AI root to ``pose``, with the wagon if attached (GIANTS plans the
        path for the whole combination)."""
        ...

    def stage(self, tractor_id: str, wagon_id: str, pose: Pose, owner: str) -> None:
        """STAGE: GIANTS AIJobGoTo of the tractor's AI root to ``pose``."""
        ...

    def align(self, tractor_id: str, wagon_id: str, plan: HitchPlan, owner: str) -> None:
        """ALIGN_REVERSE: bounded reverse (mod/FarmhandHitch.lua) that stops as soon
        as GIANTS reports exactly the planned joints as the attach candidate."""
        ...

    def attach(self, tractor_id: str, wagon_id: str, joints: tuple[int, int], owner: str) -> None:
        """ATTACH: re-check GIANTS' candidate is exactly (tractor joint, wagon joint),
        ask isAttachAllowed, then attachImplementFromInfo. Not attachment evidence."""
        ...

    def deliver(self, tractor_id: str, destination_id: str, owner: str) -> None:
        """Recheck authority/compatibility; delegate bounded AutoDrive travel to a marker."""
        ...

    def stop(self, tractor_id: str, owner: str) -> bool:
        """Stop owned equipment; true only when the stop is confirmed."""
        ...

    def release(self, owner: str) -> None:
        """Release only the exact reserved pair after ownership and stop checks."""
        ...
