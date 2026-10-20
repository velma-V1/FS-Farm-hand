"""The minimal physical-world port for AC-001. Real adapter: companion/bridge.py
FS25World; mock: simulator/world.py.

See adapters/EVIDENCE.md for which upstream capability backs each operation.
"""
from typing import Protocol
from protocol.model import Destination, Equipment, FarmInfo, Field, HitchPlan, Pose, Position


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

    def fields(self) -> tuple[Field, ...]:
        """Return the farm's own fields with their last measured coverage, fresh."""
        ...

    def farm(self) -> FarmInfo | None:
        """Return the farm's money and GIANTS' helper purchase settings, fresh."""
        ...

    def block_area(self, x: float, z: float, size: float) -> str:
        """Mark a box of ground GIANTS' navigation must route around (a gate it
        believes open). Never makes anything passable."""
        ...

    def fieldwork(self, tractor_id: str, field_id: int, pose: Pose, owner: str, area_m2: float = 0.0) -> str:
        """FIELD_WORK: GIANTS' own field-work helper (AIJobFieldWork) for the reserved
        tractor and what is hitched to it, driving to ``pose`` inside the field first;
        with ``area_m2`` the mod stops it once that much of the field changed (AREA_REACHED).
        Returns GIANTS' end reason (FINISHED, FIELD_NOT_READY, ...); never evidence
        that the field was worked: the executor measures the field."""
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
        """STAGE: GIANTS AIJobGoTo of the tractor's AI root towards ``pose``, only
        from outside the local work radius; coarse travel, never arrival."""
        ...

    def maneuver(self, tractor_id: str, wagon_id: str, plan: HitchPlan, owner: str) -> None:
        """LOCAL_MANEUVER: bounded forward drive (mod/FarmhandHitch.lua) inside the
        local work radius that ends braked with the planned joint on the wagon's
        centreline, a few metres ahead of its joint, facing its heading."""
        ...

    def align(self, tractor_id: str, wagon_id: str, plan: HitchPlan, owner: str, check_only: bool = False) -> None:
        """ALIGN_REVERSE: bounded reverse (mod/FarmhandHitch.lua) that stops as soon
        as GIANTS reports exactly the planned joints as the attach candidate.
        ``check_only``: the joints already meet; ask for the candidate, never move."""
        ...

    def attach(self, tractor_id: str, wagon_id: str, joints: tuple[int, int], owner: str) -> None:
        """ATTACH: re-check GIANTS' candidate is exactly (tractor joint, wagon joint),
        ask isAttachAllowed, then attachImplementFromInfo. Not attachment evidence."""
        ...

    def detach(self, tractor_id: str, wagon_id: str, owner: str) -> None:
        """DETACH: GIANTS' own detach of the implement from the reserved, idle tractor,
        only where GIANTS allows it (isDetachAllowed). Not detachment evidence."""
        ...

    def pull_clear(self, tractor_id: str, wagon_id: str, owner: str) -> None:
        """PULL_CLEAR: straight after that detach only, Farmhand's bounded straight
        forward pull (mod/FarmhandHitch.lua Hitch.newPullClear) until the joints are
        clear. Not evidence of the separation: the executor observes it."""
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
