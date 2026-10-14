"""Internal data contract. These are not GIANTS API types."""
from dataclasses import dataclass

Position = tuple[float, float]


@dataclass(frozen=True)
class Equipment:
    id: str
    category: str
    farm_id: str
    position: Position
    hitch: str = ""
    power_kw: float = 0
    required_power_kw: float = 0
    requires_pto: bool = False
    capabilities: frozenset[str] = frozenset()
    available: bool = True
    busy: bool = False
    occupied: bool = False
    reserved_by: str | None = None
    attached_to: str | None = None
    stopped: bool = True


@dataclass(frozen=True)
class Result:
    status: str
    reason: str
    equipment_ids: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
