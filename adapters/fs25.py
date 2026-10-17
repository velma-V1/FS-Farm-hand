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
# The optional category map can only name tractors; forage wagons are identified
# by GIANTS' ForageWagon specialization.
ALLOWED_CATEGORIES = frozenset({"tractor"})
# Store categories Farmhand Companion classifies as loaders (Collectors.lua:110-116;
# store category is its verified class signal, :97-99). Never a tractor here.
LOADER_STORE_CATEGORIES = frozenset({"FRONTLOADERVEHICLES", "WHEELLOADERVEHICLES", "TELELOADERVEHICLES",
                                     "FORKLIFTS", "SKIDSTEERVEHICLES"})
# GIANTS PowerConsumer:getCanBeTurnedOn (lines 276-282) refuses when the PTO torque
# divided by motor:getPtoMotorRpmRatio() exceeds 0.9 * motor:getPeakTorque(); the
# same rule as capacity = 0.9 * peak torque * ratio (ratio > 0). Other implements'
# load (line 277) is zero here, as only unattached tractors are selected.
GIANTS_PTO_TORQUE_SHARE = 0.9


class ObservationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Observation:
    farm_id: str
    map_id: str | None
    player: Position | None
    equipment: tuple[Equipment, ...]
    destinations: tuple[Destination, ...]
    places: tuple[Destination, ...] = ()


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


def _towing_vehicle(vehicle) -> bool:
    """A tractor without configuration: self-propelled (Motorized, not Attachable),
    with a loaded motor and a PTO output on some attacher joint, and a readable
    store category that is not a loader's. No tractor store category name is
    proven (adapters/EVIDENCE.md), so the rule rests on GIANTS' own data; whether
    the pair can actually hitch and be powered is still decided per joint pair."""
    category = vehicle["category"]
    return (vehicle["self_propelled"] is True and vehicle["motor"] is not None
            and category is not None and category.upper() not in LOADER_STORE_CATEGORIES
            and any(joint["pto_names"] for joint in vehicle["output_joints"]))


def _category(vehicle, categories) -> str | None:
    mapped = categories.get(vehicle["category"]) if vehicle["category"] else None
    if vehicle["forage_wagon"]:
        return "forage_wagon" if mapped is None else None  # contradictory: unknown
    if mapped is not None:
        return mapped                                       # explicit local override
    return "tractor" if _towing_vehicle(vehicle) else None


def _capabilities(vehicle, bridge_ready: bool) -> frozenset[str]:
    """Physical capability exists only through the live command bridge
    (mod/FarmhandBridge.lua). Moving needs GIANTS' GOTO job on the tractor (STAGE
    always uses it); AutoDrive is an optional extra travel provider."""
    if not bridge_ready or vehicle["goto_supported"] is not True:
        return frozenset()
    return frozenset({"move", "attach", "stop"} | ({"autodrive"} if vehicle["has_autodrive"] else set()))


def _pto_names(names) -> frozenset[str] | None:
    return None if names is None else frozenset(names)


def equipment_from_vehicle(vehicle: dict, categories: dict[str, str],
                           links: frozenset[int] = frozenset(), bridge_ready: bool = False) -> Equipment:
    """``vehicle`` must already be schema-validated by protocol.ipc.parse_frame.

    ``links`` adds attachments other vehicles report for this one. Unknown (None)
    occupancy/AI state maps to occupied/busy AND unavailable, never to free.
    ``bridge_ready``: the snapshot names this companion's live command session.
    """
    category = _category(vehicle, categories)
    speed = vehicle["speed_kmh"]
    motor = vehicle["motor"]
    has_autodrive = vehicle["has_autodrive"]
    # Unobserved state is never assumed safe: any missing field makes it unavailable.
    observed = (speed is not None and vehicle["entered"] is not None
                and vehicle["ai_active"] is not None
                and (not has_autodrive or vehicle["ad_active"] is not None)
                and vehicle["attachment_observed"])
    return Equipment(
        id=str(vehicle["id"]),
        category=category or "unknown",
        farm_id=str(vehicle["farm_id"]),
        position=(vehicle["x"], vehicle["z"]),
        # GIANTS' torque values from the loaded vehicles (actual configuration);
        # None when unobserved, which is never sufficient.
        pto_torque_capacity=None if motor is None else
        GIANTS_PTO_TORQUE_SHARE * motor["peak_torque"] * motor["pto_rpm_ratio"],
        required_pto_torque=vehicle["pto_torque"],
        capabilities=_capabilities(vehicle, bridge_ready),
        available=observed,
        busy=(vehicle["ai_active"] is not False
              or (has_autodrive and vehicle["ad_active"] is not False)),
        occupied=vehicle["entered"] is not False,
        # Reservations are owned by the companion, never by game state.
        reserved_by=None,
        attached_to=_attached_to(vehicle, set(links)),
        heading=vehicle["heading"],
        ai_root=None if vehicle["ai_root"] is None else Pose(**vehicle["ai_root"]),
        output_joints=tuple(OutputJoint(j["index"], j["type"], j["x"], j["z"], j["free"],
                                        _pto_names(j["pto_names"]))
                            for j in vehicle["output_joints"]),
        input_joints=tuple(InputJoint(j["index"], j["type"], (j["x"], j["z"]), j["active"],
                                      _pto_names(j["pto_names"]))
                           for j in vehicle["input_joint_poses"]),
        stopped=speed is not None and abs(speed) < STOPPED_BELOW_KMH,
    )


def observation_from_snapshot(frame: dict, categories: dict[str, str] | None = None,
                              bridge_ready: bool = False) -> Observation:
    """Build an authoritative observation, or raise ObservationError.

    ``categories`` optionally maps store categories to "tractor" (a local override;
    tractors are otherwise recognised by _towing_vehicle)."""
    categories = categories or {}
    if not set(categories.values()) <= ALLOWED_CATEGORIES:
        raise ObservationError("INVALID_CATEGORY_MAP")
    if frame.get("event") != "snapshot":
        raise ObservationError("NOT_A_SNAPSHOT")
    if frame["truncated"]:
        raise ObservationError("SNAPSHOT_TRUNCATED")
    links = _claimed_links(frame["vehicles"])
    equipment = tuple(equipment_from_vehicle(v, categories, frozenset(links.get(v["id"], ())), bridge_ready)
                      for v in frame["vehicles"])
    if len({e.id for e in equipment}) != len(equipment):
        raise ObservationError("DUPLICATE_VEHICLE_ID")
    destinations = tuple(Destination(str(d["id"]), (d["x"], d["z"]), d["name"])
                         for d in frame["destinations"])
    if len({d.id for d in destinations}) != len(destinations):
        raise ObservationError("DUPLICATE_DESTINATION_ID")
    # Owned placeables, ordered by content so their opaque ids are repeatable.
    places = tuple(Destination(f"place-{n}", (p["x"], p["z"]), p["name"]) for n, p in enumerate(
        sorted(frame["places"], key=lambda p: (p["name"], p["x"], p["z"]))))
    player = frame["player"]
    return Observation(
        farm_id=str(frame["farm_id"]),
        map_id=frame["map_id"],
        player=None if player is None else (player["x"], player["z"]),
        equipment=equipment,
        destinations=destinations,
        places=places,
    )
