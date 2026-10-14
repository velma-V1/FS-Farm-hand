"""Map validated FS25 snapshot frames into the internal executor contract.

Pure functions only: no game calls, no transport, no simulated success. Each
field's upstream source and evidence state is in adapters/EVIDENCE.md. Where
upstream data is unproven, the mapping fails closed (ineligible equipment).
"""
from dataclasses import dataclass

from protocol.model import Destination, Equipment, InputJoint, OutputJoint, Pose, Position

# AutoDrive treats ``lastSpeedReal * 3600 < 1`` (km/h) as not moving
# (scripts/Modules/TrailerModule.lua:502); getLastSpeed() reports km/h. Whether
# this is a safe "stopped" threshold for our delivery check is UNVERIFIED.
STOPPED_BELOW_KMH = 1.0
# Never equal to a real ID: IDs are stringified non-negative integers.
MULTIPLE_ATTACHMENTS = "*multiple*"
UNKNOWN_ATTACHMENT = "*unknown*"
ALLOWED_CATEGORIES = frozenset({"tractor", "forage_wagon"})


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class Observation:
    farm_id: str
    map_id: str | None
    player: Position | None
    equipment: tuple[Equipment, ...]
    destinations: tuple[Destination, ...]


def _claimed_links(vehicles) -> dict[int, set[int]]:
    """Every attachment any vehicle reports, recorded on both ends."""
    links: dict[int, set[int]] = {}
    for vehicle in vehicles:
        partners = set(vehicle["implement_ids"])
        if vehicle["attacher_vehicle_id"] is not None:
            partners.add(vehicle["attacher_vehicle_id"])
        for partner in partners:
            links.setdefault(vehicle["id"], set()).add(partner)
            links.setdefault(partner, set()).add(vehicle["id"])
    return links


def _attached_to(vehicle, links: set[int]) -> str | None:
    if not vehicle["attachment_observed"]:
        return UNKNOWN_ATTACHMENT
    if not links:
        return None
    return str(next(iter(links))) if len(links) == 1 else MULTIPLE_ATTACHMENTS


def equipment_from_vehicle(vehicle: dict, categories: dict[str, str],
                           links: frozenset[int] = frozenset()) -> Equipment:
    """``vehicle`` must already be schema-validated by protocol.ipc.parse_frame.

    ``links`` adds attachments other vehicles report for this one. Unknown (None)
    occupancy/AI state maps to occupied/busy AND unavailable, never to free.
    """
    category = categories.get(vehicle["category"]) if vehicle["category"] else None
    speed = vehicle["speed_kmh"]
    has_autodrive = vehicle["has_autodrive"]
    # Unobserved state is never assumed safe: any missing field makes it unavailable.
    observed = (speed is not None and vehicle["entered"] is not None
                and vehicle["ai_active"] is not None
                and (not has_autodrive or vehicle["ad_active"] is not None)
                and vehicle["attachment_observed"])
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
        # Only stopAutoDrive() is granted: no companion -> game command channel
        # exists yet and the HITCH composition is UNVERIFIED in FS25.
        capabilities=frozenset({"stop"}) if has_autodrive else frozenset(),
        available=observed,
        busy=(vehicle["ai_active"] is not False
              or (has_autodrive and vehicle["ad_active"] is not False)),
        occupied=vehicle["entered"] is not False,
        # Reservations are owned by the companion, never by game state.
        reserved_by=None,
        attached_to=_attached_to(vehicle, set(links)),
        heading=vehicle["heading"],
        ai_root=None if vehicle["ai_root"] is None else Pose(**vehicle["ai_root"]),
        output_joints=tuple(OutputJoint(j["index"], j["type"], j["x"], j["z"], j["free"])
                            for j in vehicle["output_joints"]),
        input_joints=tuple(InputJoint(j["index"], j["type"], (j["x"], j["z"]), j["active"])
                           for j in vehicle["input_joint_poses"]),
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
    links = _claimed_links(frame["vehicles"])
    equipment = tuple(equipment_from_vehicle(v, categories, frozenset(links.get(v["id"], ())))
                      for v in frame["vehicles"])
    if len({e.id for e in equipment}) != len(equipment):
        raise ObservationError("DUPLICATE_VEHICLE_ID")
    destinations = tuple(Destination(str(d["id"]), (d["x"], d["z"]), d["name"])
                         for d in frame["destinations"])
    if len({d.id for d in destinations}) != len(destinations):
        raise ObservationError("DUPLICATE_DESTINATION_ID")
    player = frame["player"]
    return Observation(
        farm_id=str(frame["farm_id"]),
        map_id=frame["map_id"],
        player=None if player is None else (player["x"], player["z"]),
        equipment=equipment,
        destinations=destinations,
    )
