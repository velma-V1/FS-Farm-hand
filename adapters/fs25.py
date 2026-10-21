"""Map validated FS25 snapshot frames into the internal executor contract.

Pure functions only: no game calls, no transport, no simulated success. Each
field's upstream source and evidence state is in adapters/EVIDENCE.md. Where
upstream data is unproven, the mapping fails closed (ineligible equipment).
"""
from dataclasses import dataclass

from protocol.model import (Destination, Equipment, FarmInfo, Field, FieldCoverage, FillUnit, InputJoint, OutputJoint,
                            Pose, Position)

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
# Towed work equipment, by GIANTS specialization (Observe.KINDS). A towed vehicle
# (input attacher joints, not self-propelled) with one of these is an "implement".
# Pallets, big bags and loader tools are not.
IMPLEMENT_KINDS = frozenset({"plow", "cultivator", "sowing_machine", "sprayer", "mower", "tedder", "windrower",
                             "baler", "bale_wrapper", "bale_loader", "forage_wagon", "trailer", "roller",
                             "weeder", "mulcher", "stone_picker", "manure_barrel", "tree_planter",
                             "fruit_preparer", "leveler", "mixer_wagon", "salt_spreader", "water_trailer",
                             "livestock_trailer", "hook_lift_trailer", "cutter"})
# A self-propelled vehicle that itself does work (a combine, a self-propelled
# sprayer or mower) is a machine, never a tractor (live 2026-10-18: the CH7.70
# combine has a motor and a PTO output). Loader tools (shovel) are not implements.
WORK_KINDS = (IMPLEMENT_KINDS | {"combine"}) - {"trailer"}
LOADER_TOOL_KINDS = frozenset({"shovel"})
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
    fields: tuple[Field, ...] = ()
    farm: FarmInfo | None = None


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
    """A towing vehicle (category "tractor") without configuration: self-propelled
    (Motorized, not Attachable), with a loaded motor and at least one attacher joint,
    and a readable store category that is not a loader's. A pickup or truck with a
    trailer hitch tows too (the GMC and its gooseneck): PTO power and joint types are
    decided per joint pair, so it can never take an implement it cannot hitch or power.
    No tractor store category name is proven (adapters/EVIDENCE.md)."""
    category = vehicle["category"]
    return (vehicle["self_propelled"] is True and vehicle["motor"] is not None
            and category is not None and category.upper() not in LOADER_STORE_CATEGORIES
            and not WORK_KINDS & set(vehicle["kinds"])
            and bool(vehicle["output_joints"]))


def _category(vehicle, categories) -> str | None:
    mapped = categories.get(vehicle["category"]) if vehicle["category"] else None
    if vehicle["forage_wagon"]:
        return "forage_wagon" if mapped is None else None  # contradictory: unknown
    if mapped is not None:
        return mapped                                       # explicit local override
    if _towing_vehicle(vehicle):
        return "tractor"
    if (vehicle["self_propelled"] is False and vehicle["input_joint_poses"]
            and not LOADER_TOOL_KINDS & set(vehicle["kinds"])
            and (IMPLEMENT_KINDS & set(vehicle["kinds"]) or vehicle["category"] is not None)):
        # A towed vehicle with a hitch: an implement even without a known work
        # specialization (live: the Gooseneck Trailer, only fill_unit); what it can be
        # used for still comes from its kinds, or the player's own name for it.
        return "implement"
    return None


def _capabilities(vehicle, bridge_ready: bool) -> frozenset[str]:
    """Physical capability exists only through the live command bridge
    (mod/FarmhandBridge.lua). Moving needs GIANTS' GOTO job on the tractor (STAGE
    always uses it); AutoDrive is an optional extra travel provider."""
    if not bridge_ready or vehicle["goto_supported"] is not True:
        return frozenset()
    return frozenset({"move", "attach", "stop"} | ({"autodrive"} if vehicle["has_autodrive"] else set()))


def _busy_reason(vehicle) -> str | None:
    """Which controller holds the vehicle, for diagnostics (busy decides)."""
    if vehicle["has_autodrive"] and vehicle["ad_active"] is True:
        return "AUTODRIVE_ACTIVE"
    if vehicle["ai_active"] is True:
        return "AI_ACTIVE"
    if vehicle["ai_active"] is None or (vehicle["has_autodrive"] and vehicle["ad_active"] is None):
        return "AI_STATE_UNKNOWN"
    return None


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
        kinds=frozenset(vehicle["kinds"]),
        name=vehicle["name"],
        fill_units=tuple(FillUnit(u["index"], u["fill_type"], u["level"], u["capacity"], frozenset(u["supported"]),
                                  u["role"]) for u in vehicle["fill_units"]),
        # GIANTS' torque values from the loaded vehicles (actual configuration);
        # None when unobserved, which is never sufficient.
        pto_torque_capacity=None if motor is None or motor["pto_rpm_ratio"] is None else
        GIANTS_PTO_TORQUE_SHARE * motor["peak_torque"] * motor["pto_rpm_ratio"],
        required_pto_torque=vehicle["pto_torque"],
        capabilities=_capabilities(vehicle, bridge_ready),
        available=observed,
        busy=(vehicle["ai_active"] is not False
              or (has_autodrive and vehicle["ad_active"] is not False)),
        busy_reason=_busy_reason(vehicle),
        # Ride-along: a seated requester is not a conflicting controller (single
        # player; the mod refuses multiplayer). Only an unreadable seat counts.
        occupied=vehicle["entered"] is None,
        ridden=vehicle["entered"] is True,
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
        fold=(vehicle.get("fold") or {}).get("time"),
        folding=(vehicle.get("fold") or {}).get("moving"),
        belts_fastened=(vehicle.get("belts") or {}).get("fastened"),
        belted=tuple(str(i) for i in (vehicle.get("belts") or {}).get("held", ())),
        belted_to=None if vehicle.get("belted_to") is None else str(vehicle["belted_to"]),
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
    fields = tuple(field_from_frame(f) for f in frame.get("fields", ()))
    if len({f.id for f in fields}) != len(fields):
        raise ObservationError("DUPLICATE_FIELD_ID")
    farm = frame.get("farm")
    player = frame["player"]
    return Observation(
        farm_id=str(frame["farm_id"]),
        map_id=frame["map_id"],
        player=None if player is None else (player["x"], player["z"]),
        equipment=equipment,
        destinations=destinations,
        places=places,
        fields=fields,
        farm=None if farm is None else FarmInfo(farm["money"], farm["helper_buy_seeds"],
                                                farm["helper_buy_fertilizer"], farm["helper_buy_fuel"]),
    )


def field_from_frame(f) -> Field:
    c = f["coverage"]
    coverage = None
    if c is not None:
        coverage = FieldCoverage(
            ground=tuple(sorted((name, min(1.0, share)) for name, share in c["ground"].items())),
            spray=tuple(sorted((int(level), min(1.0, share)) for level, share in c["spray"].items())),
            total_m2=c["total_m2"],
            fruit=None if c["fruit"] is None else tuple(sorted(c["fruit"].items())),
            sampled=c["sampled"])
    return Field(f["id"], f["area_ha"], (f["x"], f["z"]), coverage, f["age_ms"])
