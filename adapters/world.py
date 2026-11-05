"""The minimal physical-world port for AC-001. Real adapter: companion/bridge.py
FS25World; mock: simulator/world.py.

See adapters/EVIDENCE.md for which upstream capability backs each operation.
"""
from typing import Protocol
from protocol.model import Contract, Destination, Equipment, FarmInfo, Field, HitchPlan, Pose, Position


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

    def places(self) -> tuple[Destination, ...]:
        """Return the farm's own named placeables (buildings), fresh."""
        ...

    def fields(self) -> tuple[Field, ...]:
        """Return the farm's own fields with their last measured coverage, fresh."""
        ...

    def farm(self) -> FarmInfo | None:
        """Return the farm's money and GIANTS' helper purchase settings, fresh."""
        ...

    def contracts(self) -> tuple[Contract, ...]:
        """GIANTS' field contracts: available (CREATED) and this farm's own."""
        ...

    def contract(self, field: int, start: bool) -> str:
        """CONTRACT: take the field's available contract (DONE once GIANTS runs it) or
        collect this farm's finished one (GIANTS pays the reward); never evidence of the
        money: the caller reads the farm's balance."""
        ...

    def land(self) -> tuple:
        """Every map field's farmland parcel (protocol.model.LandParcel): owner, price."""
        ...

    def land_change(self, field: int, buy: bool) -> str:
        """LAND: the field's farmland parcel bought or sold through GIANTS' own request at its
        price; DONE once GIANTS shows the new owner, never evidence of the money: the caller
        reads the farm's balance."""
        ...

    def pipe(self, vehicle_id: str, out: bool, owner: str) -> str:
        """PIPE_OUT / PIPE_IN: the reserved combine's pipe to its unloading state or folded
        (GIANTS Pipe); DONE once there."""
        ...

    def alongside(self, tractor_id: str, combine_id: str, owner: str, combine_owner: str, past: bool = False) -> str:
        """ALONGSIDE: the reserved tractor drives onto the pipe's line until its trailer's middle
        is under the reserved combine's pipe (UNDER_PIPE), or (``past``) on until clear of the
        combine (PASSED); AGENTS.md exception 6."""
        ...

    def reach(self, vehicle_id: str, pose: Pose, owner: str, width: float = 0.0) -> str:
        """REACH: whether GIANTS' agent plans a route for the reserved vehicle to ``pose``; never drives.
        REACHABLE, NOT_REACHABLE, REACH_BLOCKED or REACH_UNDECIDED."""
        ...

    def escort(self, tractor_id: str, combine_id: str, owner: str) -> str:
        """ESCORT: the reserved tractor's trailer held under the pipe of the combine harvesting
        under GIANTS' helper, at its speed, behind it while it turns, until TRAILER_FULL or
        COMBINE_STOPPED (the helper ended); the combine is never driven. The caller checks the grain."""
        ...

    def backoff(self, tractor_id: str, trailer_id: str, owner: str) -> str:
        """BACKOFF: the reserved tractor straight back from the reserved transport trailer's
        ramps it waits behind (AGENTS.md exception 5); BACKED_OFF at rest."""
        ...

    def giants_deliver(self, tractor_id: str, owner: str, store: bool = False) -> str:
        """SELL: GIANTS' Deliver helper takes the reserved rig's cargo to the best paid selling
        station and tips it there, or (``store``) to the farm's own storage nearest it; the
        caller checks the cargo (and, selling, the money)."""
        ...

    def overload(self, combine_id: str, trailer_id: str, owner: str) -> str:
        """OVERLOAD: the reserved standing combine (pipe out) discharges into the trailer
        under its pipe, GIANTS' own discharge; EMPTIED, TRAILER_FULL or OVERLOAD_STOPPED.
        The caller checks both fill levels."""
        ...

    def open_gates(self, x: float, z: float, radius: float) -> str:
        """Open closed gates near (x, z) the way GIANTS opens one (opening hours).
        StepFailed NO_CLOSED_GATE when there is none."""
        ...

    def block_area(self, x: float, z: float, size: float) -> str:
        """Mark a box of ground GIANTS' navigation must route around (a gate it
        believes open). Never makes anything passable."""
        ...

    def fieldwork(self, tractor_id: str, field_id: int, pose: Pose, owner: str, area_m2: float = 0.0,
                  seed: int = 0, hand: bool = False) -> str:
        """FIELD_WORK: GIANTS' own field-work helper (AIJobFieldWork) for the reserved
        tractor and what is hitched to it, driving to ``pose`` inside the field first;
        with ``area_m2`` the mod stops it once that much of the field changed (AREA_REACHED);
        ``seed`` (a position in the hitched seeder's ``seeds``, 0: as set) chooses the crop first;
        ``hand``: returns WORKING once GIANTS' helper works the field, which then goes on as
        the game's hired worker (``stop`` still ends it).
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

    def navigate(self, tractor_id: str, pose: Pose, owner: str) -> None:
        """TRAVEL_NEAR or DELIVER: GIANTS' navigation agent, under Farmhand, takes the
        reserved tractor's AI root to ``pose``, with the wagon if attached (GIANTS plans
        the path for the whole combination)."""
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

    def fold(self, vehicle_id: str, direction: int, owner: str) -> str:
        """One of the reserved units' own folding parts (a pickup's tailgate, a deckover's
        ramps) moved to one end (+1: fold time 1, -1: 0) by GIANTS' Foldable, where GIANTS
        allows it. Not evidence: the executor reads the fold time from a fresh snapshot."""
        ...

    def belts(self, vehicle_id: str, fasten: bool, owner: str) -> str:
        """The reserved transport's tension belts fastened or released (GIANTS
        TensionBelts). Not evidence: the executor reads what they hold."""
        ...

    def unpark(self, tractor_id: str, owner: str) -> None:
        """UNPARK: only after GIANTS' agent could not start from where the reserved vehicle
        stands and no closed door explains it, Farmhand's bounded straight extraction
        (mod/FarmhandHitch.lua Hitch.newUnpark) of it and whatever is attached to it to the
        nearest exit its collision query shows clear. Refusals (UNPARK_*) move nothing. Not
        evidence of the exit: the executor observes it and hands back to GIANTS GOTO."""
        ...

    def stop(self, tractor_id: str, owner: str) -> bool:
        """Stop owned equipment; true only when the stop is confirmed."""
        ...

    def release(self, owner: str) -> None:
        """Release only the exact reserved pair after ownership and stop checks."""
        ...
