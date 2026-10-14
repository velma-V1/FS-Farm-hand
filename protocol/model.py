"""Internal data contract. These are not GIANTS API types."""
from dataclasses import dataclass

Position = tuple[float, float]


@dataclass(frozen=True)
class Equipment:
    id: str
    category: str
    farm_id: str
    position: Position
    # Attacher joint type names: every joint a tractor offers, or every input
    # joint a towed implement accepts (FS25 vehicles can expose several).
    hitches: frozenset[str] = frozenset()
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
class Destination:
    """A routable delivery point, e.g. an AutoDrive map marker. Opaque ID."""
    id: str
    position: Position


@dataclass(frozen=True)
class Result:
    status: str
    reason: str
    equipment_ids: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
