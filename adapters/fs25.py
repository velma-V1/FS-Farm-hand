"""Map validated FS25 snapshot frames into the internal executor contract.

Pure functions only: no game calls, no transport, no simulated success. Each
field's upstream source and evidence state is in adapters/EVIDENCE.md. Where
upstream data is unproven, the mapping fails closed (ineligible equipment).
"""
from dataclasses import dataclass

from protocol.model import Destination, Equipment, Position

# AutoDrive treats ``lastSpeedReal * 3600 < 1`` (km/h) as not moving
# (scripts/Modules/TrailerModule.lua:502); getLastSpeed() reports km/h. Whether
# this is a safe "stopped" threshold for our delivery check is UNVERIFIED.
STOPPED_BELOW_KMH = 1.0
# Never equal to a real ID: IDs are stringified non-negative integers.
MULTIPLE_ATTACHMENTS = "*multiple*"
ALLOWED_CATEGORIES = frozenset({"tractor", "forage_wagon"})


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class Observation:
    farm_id: str
    player: Position | None
    equipment: tuple[Equipment, ...]
    destinations: tuple[Destination, ...]


def _attached_to(vehicle) -> str | None:
    links = set(vehicle["implement_ids"])
    if vehicle["attacher_vehicle_id"] is not None:
        links.add(vehicle["attacher_vehicle_id"])
    if not links:
        return None
    return str(links.pop()) if len(links) == 1 else MULTIPLE_ATTACHMENTS


def equipment_from_vehicle(vehicle: dict, categories: dict[str, str]) -> Equipment:
    """``vehicle`` must already be schema-validated by protocol.ipc.parse_frame."""
    category = categories.get(vehicle["category"]) if vehicle["category"] else None
    speed = vehicle["speed_kmh"]
    has_autodrive = vehicle["has_autodrive"]
    # Unobserved state is never assumed safe: any missing field makes it unavailable.
    observed = (speed is not None and vehicle["entered"] is not None
                and vehicle["ai_active"] is not None
                and (not has_autodrive or vehicle["ad_active"] is not None))
    hitches = vehicle["attacher_joints"] if category == "tractor" else vehicle["input_joints"]
    return Equipment(
        id=str(vehicle["id"]),
        category=category or "unknown",
        farm_id=str(vehicle["farm_id"]),
        position=(vehicle["x"], vehicle["z"]),
        hitches=frozenset(hitches),
        # GAP: no proven upstream source for tractor power or implement power/PTO
        # needs, so the executor's power check cannot pass on real data yet.
        power_kw=0,
        required_power_kw=0,
        requires_pto=True,
        # Only stopAutoDrive() is a proven control; approach is a GAP and
        # unattended attach is UNVERIFIED, so "move"/"attach" are not granted.
        capabilities=frozenset({"stop"}) if has_autodrive else frozenset(),
        available=observed,
        busy=bool(vehicle["ai_active"] or vehicle["ad_active"]),
        occupied=bool(vehicle["entered"]),
        # Reservations are owned by the companion, never by game state.
        reserved_by=None,
        attached_to=_attached_to(vehicle),
        stopped=speed is not None and abs(speed) < STOPPED_BELOW_KMH,
    )


def observation_from_snapshot(frame: dict, categories: dict[str, str]) -> Observation:
    """Build an authoritative observation, or raise ObservationError."""
    if not set(categories.values()) <= ALLOWED_CATEGORIES:
        raise ObservationError("INVALID_CATEGORY_MAP")
    if frame.get("event") != "snapshot":
        raise ObservationError("NOT_A_SNAPSHOT")
    if frame["truncated"]:
        raise ObservationError("SNAPSHOT_TRUNCATED")
    equipment = tuple(equipment_from_vehicle(v, categories) for v in frame["vehicles"])
    if len({e.id for e in equipment}) != len(equipment):
        raise ObservationError("DUPLICATE_VEHICLE_ID")
    destinations = tuple(Destination(str(d["id"]), (d["x"], d["z"])) for d in frame["destinations"])
    if len({d.id for d in destinations}) != len(destinations):
        raise ObservationError("DUPLICATE_DESTINATION_ID")
    player = frame["player"]
    return Observation(
        farm_id=str(frame["farm_id"]),
        player=None if player is None else (player["x"], player["z"]),
        equipment=equipment,
        destinations=destinations,
    )
