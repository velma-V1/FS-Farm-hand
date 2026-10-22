"""Synthetic world only: coordinates change instantly; no game APIs or physics."""
from dataclasses import replace
import math
from threading import RLock

from adapters.world import StepFailed
from protocol.model import Destination, Equipment, FarmInfo, Field, FieldCoverage, Pose

# Mock-only stand-ins for GIANTS' attach-range decision and AIJobGoTo accuracy.
# They are NOT GIANTS' rules (adapters/EVIDENCE.md); they only let orchestration
# tests distinguish "where the plan said" from "somewhere else".
MOCK_ATTACH_RANGE_M = 0.3
MOCK_POSE_TOLERANCE_M = 0.5
MOCK_TRAILER_OFFSET_M = 7.0
MOCK_PIPE_REACH_M = 6.0           # mock-only: a combine's pipe point to its left
MOCK_PIPE_HIT_M = 1.5             # mock-only: how near the trailer's middle must be
MOCK_ALONGSIDE_LATERAL_M = 2.5    # = mod/FarmhandHitch.lua Hitch.ALONGSIDE.startLateralM
MOCK_ALONGSIDE_MAX_M = 25.0       # = Hitch.ALONGSIDE.maxTravelM
MOCK_PASS_M = 12.0                # mock-only: how far on past the combine
MOCK_REVERSE_REACH_M = 10.0      # the mod's reverse cap (Hitch.LIMITS.maxDistanceM)
MOCK_LOCAL_RADIUS_M = 25.0       # the mod's local work radius (Hitch.LOCAL_LIMITS.workRadiusM)
MOCK_PULL_CLEAR_M = 4.5          # the mod's pull-clear separation (Hitch.PULL.clearM)
MOCK_PULL_MAX_M = 8.0            # and its cap (Hitch.PULL_LIMITS.maxDistanceM)
MOCK_UNPARK_MAX_M = 20.0         # the mod's extraction cap (Hitch.UNPARK_LIMITS.maxDistanceM)


class MockWorld:
    def __init__(self, equipment, players, destinations=(), map_id=None, places=(), fields=(), farm=None):
        equipment, destinations = tuple(equipment), tuple(destinations)
        self.items: dict[str, Equipment] = {item.id: item for item in equipment}
        if len(self.items) != len(equipment):
            raise ValueError("Duplicate equipment IDs")
        self.destination_map: dict[str, Destination] = {d.id: d for d in destinations}
        if len(self.destination_map) != len(destinations):
            raise ValueError("Duplicate destination IDs")
        self.players = dict(players)
        self.current_map_id = map_id
        self.place_list = tuple(places)
        self.field_map: dict[int, Field] = {f.id: f for f in fields}
        self.farm_info = farm
        self._lock = RLock()
        self._reservations: dict[str, tuple[str, tuple[str, str]]] = {}

    def snapshot(self):
        with self._lock:
            return tuple(self.items.values())

    def player_position(self, farm_id):
        with self._lock:
            return self.players[farm_id]

    def destinations(self):
        with self._lock:
            return tuple(self.destination_map.values())

    def places(self):
        with self._lock:
            return self.place_list

    def fields(self):
        with self._lock:
            return tuple(self.field_map.values())

    mock_field_places = ()        # mock-only: Destination("field-N", position, "field N") for every map field
    mock_ad_fields = {}           # mock-only: field N -> where AutoDrive's network passes its edge
    ad_markers_made = ()

    mock_follow_to = None         # mock-only: where following the player ends; None: FollowMe absent

    def follow(self, tractor_id, owner):
        # Mock-only: the rig (and what is belted on it) ends where the player led it.
        with self._lock:
            tractor = self._owned(tractor_id, owner)
            if self.mock_follow_to is None:
                raise StepFailed("FOLLOWME_UNAVAILABLE")
            wagon = self.items.get(tractor.attached_to)
            self._place(tractor, Pose(*self.mock_follow_to, tractor.heading or 0.0))
            if wagon is not None:
                self._move_wagon(wagon, self.mock_follow_to)
            return "FOLLOW_ENDED"

    def ad_marker(self, field):
        # Mock-only: AutoDrive's own marker "Field N" where its network meets field N.
        with self._lock:
            position = self.mock_ad_fields.get(field)
            if position is None:
                raise StepFailed("AD_NO_WAYPOINT_AT_FIELD")
            self.destination_map = {**self.destination_map,
                                    f"ad-{field}": Destination(f"ad-{field}", position, f"Field {field}")}
            self.ad_markers_made = self.ad_markers_made + (field,)
            return "MARKER_CREATED"

    def field_places(self):
        return tuple(self.mock_field_places)

    store_items = ()              # StoreItems the mock store sells
    mock_bought = None            # Equipment template the mock purchase spawns (name from the item)

    def catalog(self):
        return tuple(self.store_items)

    saved = 0
    blocked = ()

    gates = ()                    # mock closed gates: positions
    opened = ()
    # Mock-only: vehicles GIANTS' agent cannot start from (a shed), id -> {"exit": metres
    # straight ahead UNPARK takes it (and what is attached), "refuse": an UNPARK refusal
    # code, "still": blocked after UNPARK}. A closed gate within 15 m refuses UNPARK.
    boxed = None
    unparked = ()

    def open_gates(self, x, z, radius):
        near = [g for g in self.gates if math.dist(g, (x, z)) <= radius]
        if not near:
            raise StepFailed("NO_CLOSED_GATE")
        self.gates = tuple(g for g in self.gates if g not in near)
        self.opened = self.opened + tuple(near)
        return "GATES_OPENED"

    def fold(self, vehicle_id, direction, owner):
        # Mock-only: the fold time jumps to the asked end.
        with self._lock:
            vehicle = self._owned(vehicle_id, owner)
            if vehicle.fold is None:
                raise StepFailed("NOT_FOLDABLE")
            self.items[vehicle_id] = replace(vehicle, fold=1.0 if direction > 0 else 0.0, folding=False)
            return "FOLDED"

    def belts(self, vehicle_id, fasten, owner):
        # Mock-only: belts hold every vehicle standing on the transport (mock_on_deck).
        with self._lock:
            transport = self._owned(vehicle_id, owner)
            if transport.belts_fastened is None:
                raise StepFailed("NO_BELTS")
            held = tuple(i for i in self.mock_on_deck.get(vehicle_id, ())) if fasten else ()
            self.items[vehicle_id] = replace(transport, belts_fastened=fasten, belted=held)
            for other in held:
                self.items[other] = replace(self.items[other], belted_to=vehicle_id)
            if not fasten:
                for other in self.items.values():
                    if other.belted_to == vehicle_id:
                        self.items[other.id] = replace(other, belted_to=None)
            return "BELTS_FASTENED" if fasten else "BELTS_RELEASED"

    mock_on_deck = {}             # mock-only: transport id -> ids standing on its deck
    _transports = frozenset()     # owners whose reservation is a tractor and a transport trailer
    mock_load_moves = True        # mock-only: False and LOAD says LOADED without moving anything

    def load(self, tractor_id, trailer_id, direction, owner):
        # Mock-only teleport standing in for the mod's straight drive: onto the middle of
        # the deck box, or back until its front is 3.5 m behind the trailer's rear.
        with self._lock:
            tractor, trailer = self._owned(tractor_id, owner), self._owned(trailer_id, owner)
            if trailer.fold != 1.0:
                raise StepFailed("LOAD_RAMPS_NOT_DOWN")
            if trailer.belts_fastened:
                raise StepFailed("LOAD_BELTS_FASTENED")
            if not self.mock_load_moves:
                return "LOADED" if direction > 0 else "UNLOADED"
            fx, fz = math.sin(trailer.heading), math.cos(trailer.heading)
            length, _, _, offset = tractor.size
            if direction > 0:
                mids = [((m[0] + m[3]) / 2 - trailer.position[0]) * fx + ((m[2] + m[5]) / 2 - trailer.position[1]) * fz
                        for m in trailer.belt_markers]
                a = (min(mids) + max(mids)) / 2 - offset          # the belts' middle
                self.mock_on_deck = {**self.mock_on_deck, trailer_id: (tractor_id,)}
            else:
                t_length, _, _, t_offset = trailer.size
                a = t_offset - t_length / 2 - 3.5 - (offset + length / 2)
                self.mock_on_deck = {k: tuple(i for i in v if i != tractor_id) for k, v in self.mock_on_deck.items()}
            x, z = trailer.position[0] + fx * a, trailer.position[1] + fz * a
            self._carry(tractor, Pose(x, z, trailer.heading))
            return "LOADED" if direction > 0 else "UNLOADED"

    def _start_blocked(self, tractor_id, prefix):
        # Mock-only: GIANTS accepts the target, its agent stays BLOCKED, nothing moves.
        if tractor_id in (self.boxed or {}):
            raise StepFailed(prefix + "BLOCKED_AT_START")

    def unpark(self, tractor_id, owner):
        # Mock-only teleport straight ahead, standing in for the bounded extraction.
        with self._lock:
            tractor = self._owned(tractor_id, owner)
            box = (self.boxed or {}).get(tractor_id)
            self.unparked = self.unparked + (tractor_id,)
            if box is None:
                raise StepFailed("UNPARK_NOT_BOXED")
            if box.get("refuse"):
                raise StepFailed(box["refuse"])
            if any(math.dist(g, tractor.position) <= 15.0 for g in self.gates):
                raise StepFailed("UNPARK_DOOR_CLOSED")
            root = tractor.ai_root
            ahead = box.get("exit", 8.0)
            if ahead > MOCK_UNPARK_MAX_M:
                raise StepFailed("UNPARK_EXIT_TOO_FAR")
            x, z = root.to_world(0.0, ahead)
            self._place(tractor, Pose(x, z, root.yaw))
            wagon = self.items.get(tractor.attached_to)
            if wagon is not None:
                wx, wz = wagon.position
                dx, dz = x - root.x, z - root.z
                self._move_wagon(wagon, (wx + dx, wz + dz))
            if not box.get("still"):
                self.boxed = {i: b for i, b in self.boxed.items() if i != tractor_id}

    def block_area(self, x, z, size):
        self.blocked = self.blocked + ((x, z, size),)
        return "BLOCKED_AREA"

    def save_game(self):
        self.saved += 1
        return "SAVED"

    def buy(self, item_id, hitch=0, engine=0, options=()):
        # Mock-only: spawns a copy of mock_bought named as the item, and charges it.
        with self._lock:
            item = next((i for i in self.store_items if i.id == item_id), None)
            if item is None or self.mock_bought is None:
                raise RuntimeError("UNKNOWN_STORE_ITEM")
            n = len(self.items) + 1
            self.items[f"new-{n}"] = replace(self.mock_bought, id=f"new-{n}", name=item.name, brand=item.brand)
            if self.farm_info is not None and self.farm_info.money is not None:
                self.farm_info = replace(self.farm_info, money=self.farm_info.money - item.price)
            return "BOUGHT"

    def farm(self):
        with self._lock:
            return self.farm_info

    # Mock-only contracts: Contract records and each contract field (not owned until taken).
    # A running contract finishes once Farmhand worked its field (GIANTS judges it in game).
    mock_contracts = ()
    mock_contract_fields = {}

    def contracts(self):
        with self._lock:
            worked = getattr(self, "_worked", set())
            self.mock_contracts = tuple(replace(c, status="FINISHED") if c.mine and c.status == "RUNNING"
                                        and c.field in worked else c for c in self.mock_contracts)
            return tuple(self.mock_contracts)

    def contract(self, field, start):
        with self._lock:
            mine = next((c for c in self.contracts() if c.field == field and c.mine), None)
            if start:
                offer = next((c for c in self.mock_contracts if c.field == field and c.status == "CREATED"), None)
                if offer is None:
                    raise StepFailed("NO_CONTRACT_ON_FIELD")
                if any(c.mine for c in self.mock_contracts):
                    raise StepFailed("CONTRACT_LIMIT_REACHED")
                self.mock_contracts = tuple(replace(c, status="RUNNING", mine=True) if c is offer else c
                                            for c in self.mock_contracts)
                self.field_map[field] = self.mock_contract_fields[field]
                return "CONTRACT_RUNNING"
            if mine is None:
                raise StepFailed("NO_CONTRACT_ON_FIELD")
            if mine.status != "FINISHED":
                raise StepFailed("CONTRACT_NOT_FINISHED")
            self.mock_contracts = tuple(c for c in self.mock_contracts if c is not mine)
            self.field_map.pop(field, None)
            self.farm_info = replace(self.farm_info, money=self.farm_info.money + (mine.reward or 0))
            return "CONTRACT_COLLECTED"

    # What each implement kind leaves on the field (mock only, never GIANTS').
    MOCK_WORK = {"cultivator": ("CULTIVATED", None), "plow": ("PLOWED", None), "sowing_machine": ("SOWN", "WHEAT"),
                 "sprayer": (None, None), "cutter": (None, None)}

    def fieldwork(self, tractor_id, field_id, pose, owner, area_m2=0.0, seed=0, hand=False):
        # Mock-only: the hitched implement's kind turns the whole field into its result
        # and uses 100 l of its material; the tractor ends inside the field. Handed off,
        # the helper works on (the tractor busy) until finish_worker or a stop.
        if hand:
            with self._lock:
                reservation = self._reservations.get(owner)
                if not reservation or tractor_id != reservation[1][0]:
                    raise RuntimeError("NOT_RESERVED")
                self.mock_workers = {**self.mock_workers, tractor_id: (field_id, pose, owner, area_m2, seed)}
                self.items[tractor_id] = replace(self.items[tractor_id], busy=True, busy_reason="AI_ACTIVE")
                return "WORKING"
        with self._lock:
            reservation = self._reservations.get(owner)
            if not reservation or tractor_id != reservation[1][0]:
                raise RuntimeError("NOT_RESERVED")
            tractor = self.items[tractor_id]
            implement = self.items.get(tractor.attached_to)
            field = self.field_map.get(field_id)
            if implement is None or field is None:
                raise RuntimeError("NO_FIELD_FOUND")
            kind = next((k for k in self.MOCK_WORK if k in implement.kinds), None)
            if kind is None:
                raise RuntimeError("FIELDWORK_UNAVAILABLE")
            ground, fruit = self.MOCK_WORK[kind]
            if seed:                          # the crop chosen from the seeder's own list
                if kind != "sowing_machine":
                    raise StepFailed("NO_SEEDER")
                if not 1 <= seed <= len(implement.seeds):
                    raise StepFailed("SEED_NOT_SUPPORTED")
                fruit = implement.seeds[seed - 1]
            elif kind == "sowing_machine" and implement.seeds:
                fruit = implement.seeds[0]
            old = field.coverage or FieldCoverage((), (), 10000.0, None, 9)
            share = min(1.0, area_m2 / old.total_m2) if area_m2 else 1.0     # a bounded test area
            rest = tuple((n, s * (1 - share)) for n, s in old.ground if n != ground)
            coverage = FieldCoverage(
                ground=tuple(sorted(rest + ((ground, share + dict(old.ground).get(ground, 0) * (1 - share)),)))
                if ground else old.ground,
                spray=((1, share + dict(old.spray).get(1, 0) * (1 - share)),) if kind == "sprayer" else old.spray,
                total_m2=old.total_m2, fruit=((fruit, 9),) if fruit else old.fruit, sampled=old.sampled,
                cut=sum(n for name, n in old.fruit or () if name != "NONE") if kind == "cutter" else old.cut)
            self.field_map[field_id] = replace(field, coverage=coverage, age_ms=0.0)
            self._worked = getattr(self, "_worked", set()) | {field_id}
            units = tuple(replace(u, level=max(0.0, u.level - 100.0)) if u.role and u.level else u
                          for u in implement.fill_units)
            self.items[implement.id] = replace(implement, fill_units=units)
            self._place(tractor, pose)
            return "AREA_REACHED" if area_m2 else "FINISHED"

    mock_workers = {}             # mock-only: tractor id -> a handed-off helper's job
    mock_sell_at = None           # mock-only: where the best paid sell point is; None: none reachable
    mock_no_deliver = False       # mock-only: GIANTS' Deliver helper refuses the rig
    mock_passed = ()              # mock-only: the rigs ALONGSIDE took on past a combine
    mock_price = 2.0              # mock-only: money per litre sold

    def _cargo(self, tractor_id):
        tractor = self.items[tractor_id]
        return self.items.get(tractor.attached_to)

    def ad_unload(self, tractor_id, marker_id, field, owner):
        # Mock-only: AutoDrive's unloader runs (busy) until stopped; tests fill its trailer.
        with self._lock:
            reservation = self._reservations.get(owner)
            if not reservation or tractor_id != reservation[1][0]:
                raise RuntimeError("NOT_RESERVED")
            if marker_id not in self.destination_map:
                raise StepFailed("UNKNOWN_MARKER")
            if self.mock_sell_at is None:
                raise StepFailed("NO_SELL_POINT")
            self.mock_workers = {**self.mock_workers, tractor_id: None}
            self.items[tractor_id] = replace(self.items[tractor_id], busy=True, busy_reason="AUTODRIVE_ACTIVE")
            return "UNLOADING"

    def pipe(self, vehicle_id, out, owner):
        # Mock-only: the pipe reaches MOCK_PIPE_REACH_M to the combine's left.
        with self._lock:
            reservation = self._reservations.get(owner)
            if not reservation or vehicle_id not in reservation[1]:
                raise RuntimeError("NOT_RESERVED")
            v = self.items[vehicle_id]
            if v.pipe is None:
                raise StepFailed("NO_PIPE")
            x, z = Pose(*v.position, v.heading or 0.0).to_world(-MOCK_PIPE_REACH_M, 0.0)
            self.items[vehicle_id] = replace(v, pipe=(bool(out), x, z))
            return "PIPE_OUT" if out else "PIPE_IN"

    def alongside(self, tractor_id, combine_id, owner, combine_owner, past=False):
        # Mock-only teleport straight ahead until the trailer's middle is level with the pipe
        # point, refused like the mod unless that point is on the trailer's middle line.
        with self._lock:
            tractor = self._owned(tractor_id, owner)
            combine = self.items[combine_id]
            trailer = self.items[tractor.attached_to]
            if combine.reserved_by != combine_owner or not (combine.pipe and combine.pipe[0]):
                raise StepFailed("PIPE_NOT_OUT")
            heading = tractor.heading or 0.0
            fx, fz = math.sin(heading), math.cos(heading)
            if past:                       # on past the combine: MOCK_PASS_M ahead
                self._carry(tractor, Pose(tractor.position[0] + fx * MOCK_PASS_M,
                                          tractor.position[1] + fz * MOCK_PASS_M, heading))
                self.mock_passed = self.mock_passed + (tractor_id,)
                return "PASSED"
            middle = Pose(*trailer.position, trailer.heading or 0.0).to_world(0.0, (trailer.size or (0, 0, 0, 0.0))[3])
            dx, dz = combine.pipe[1] - middle[0], combine.pipe[2] - middle[1]
            travel, lateral = dx * fx + dz * fz, dx * fz - dz * fx
            if abs(lateral) > MOCK_ALONGSIDE_LATERAL_M:
                raise StepFailed("ALONGSIDE_NOT_LINED_UP")
            if not 0 < travel <= MOCK_ALONGSIDE_MAX_M:
                raise StepFailed("ALONGSIDE_TOO_FAR" if travel > 0 else "ALONGSIDE_PAST_THE_PIPE")
            self._carry(tractor, Pose(tractor.position[0] + fx * travel, tractor.position[1] + fz * travel, heading))
            return "UNDER_PIPE"

    def overload(self, combine_id, trailer_id, owner):
        # Mock-only: a trailer whose middle is within MOCK_PIPE_HIT_M of the pipe's point
        # takes what it has room for.
        with self._lock:
            reservation = self._reservations.get(owner)
            if not reservation or combine_id not in reservation[1]:
                raise RuntimeError("NOT_RESERVED")
            combine, trailer = self.items[combine_id], self.items[trailer_id]
            middle = Pose(*trailer.position, trailer.heading or 0.0).to_world(0.0, (trailer.size or (0, 0, 0, 0.0))[3])
            if not (combine.pipe and combine.pipe[0]) or math.dist(middle, combine.pipe[1:]) > MOCK_PIPE_HIT_M:
                raise StepFailed("NOT_OVERLOADING")
            grain = next(u for u in combine.fill_units if u.fill_type not in ("DIESEL", "DEF", "AIR") and u.level)
            unit = trailer.fill_units[0]
            moved = min(grain.level, unit.capacity - (unit.level or 0.0))
            self.items[combine_id] = replace(combine, fill_units=tuple(
                replace(u, level=u.level - moved) if u is grain else u for u in combine.fill_units))
            self.items[trailer_id] = replace(trailer, fill_units=(replace(unit, fill_type=grain.fill_type,
                                                                          level=(unit.level or 0.0) + moved),)
                                             + trailer.fill_units[1:])
            return "EMPTIED" if moved == grain.level else "TRAILER_FULL"

    def giants_deliver(self, tractor_id, owner):
        # Mock-only: GIANTS' Deliver helper; as ad_sell unless mock_no_deliver.
        if self.mock_no_deliver:
            raise StepFailed("DELIVER_UNAVAILABLE")
        return self.ad_sell(tractor_id, owner)

    def ad_sell(self, tractor_id, owner):
        # Mock-only: the cargo goes to the sell point, the farm is paid for it.
        with self._lock:
            reservation = self._reservations.get(owner)
            if not reservation or tractor_id != reservation[1][0]:
                raise RuntimeError("NOT_RESERVED")
            cargo = self._cargo(tractor_id)
            litres = sum(u.level or 0.0 for u in cargo.fill_units) if cargo else 0.0
            if not litres:
                raise StepFailed("NOTHING_TO_SELL")
            if self.mock_sell_at is None:
                raise StepFailed("NO_SELL_POINT")
            self.items[cargo.id] = replace(cargo, fill_units=tuple(replace(u, level=0.0) for u in cargo.fill_units))
            self.farm_info = replace(self.farm_info, money=self.farm_info.money + litres * self.mock_price)
            self._place(self.items[tractor_id], Pose(*self.mock_sell_at, 0.0))
            return "AD_ENDED"

    def finish_worker(self, tractor_id, work=True):
        """Mock-only: the handed-off helper ends (its field worked unless ``work`` is False)."""
        with self._lock:
            field_id, pose, owner, area_m2, seed = self.mock_workers[tractor_id]
            self.mock_workers = {k: v for k, v in self.mock_workers.items() if k != tractor_id}
            self.items[tractor_id] = replace(self.items[tractor_id], busy=False, busy_reason=None)
            if work:
                self.fieldwork(tractor_id, field_id, pose, owner, area_m2, seed)

    def map_id(self):
        with self._lock:
            return self.current_map_id

    def reserve(self, ids, owner, transport=False):
        with self._lock:
            if not owner or len(set(ids)) != 2 or any(i not in self.items for i in ids):
                return False
            if owner in self._reservations or any(x.reserved_by == owner for x in self.items.values()):
                return False
            pair = [self.items[i] for i in ids]
            # Unattached, or hitched to each other (move_pair); never to anything else.
            partners = {pair[0].id: pair[1].id, pair[1].id: pair[0].id}
            if any(item.reserved_by is not None or item.busy or item.occupied
                   or not item.available or not item.stopped for item in pair):
                return False
            if transport:                 # only the trailer may hang on its truck
                # The tractor alone or carrying its own implement; never towing the transport.
                if pair[0].attached_to == pair[1].id or pair[1].attached_to == pair[0].id:
                    return False
            elif (any(item.attached_to not in (None, partners[item.id]) for item in pair)
                  or (pair[0].attached_to is None) != (pair[1].attached_to is None)):
                return False
            if pair[0].farm_id != pair[1].farm_id:
                return False
            for item in pair:
                self.items[item.id] = replace(item, reserved_by=owner)
            self._reservations[owner] = (pair[0].farm_id, tuple(ids))
            self._transports = (self._transports - {owner}) | ({owner} if transport else set())
            return True

    def _owned(self, identifier, owner):
        item = self.items[identifier]
        reservation = self._reservations.get(owner)
        if (not reservation or identifier not in reservation[1]
                or item.farm_id != reservation[0]
                or item.reserved_by != owner or not item.available
                or item.busy or item.occupied):
            raise ValueError("Equipment not available to request")
        return item

    @staticmethod
    def _compatible(tractor, wagon):
        # Independent mock-side check at mutation time, under the world lock.
        if (tractor.category != "tractor" or wagon.category not in ("forage_wagon", "implement")
                or not {"move", "attach", "stop"} <= tractor.capabilities
                or wagon.required_pto_torque is None
                or not math.isfinite(wagon.required_pto_torque)
                or wagon.required_pto_torque < 0
                or wagon.required_pto_torque > 0 and not (
                    tractor.pto_torque_capacity is not None
                    and math.isfinite(tractor.pto_torque_capacity)
                    and tractor.pto_torque_capacity >= wagon.required_pto_torque)
                or tractor.farm_id != wagon.farm_id):
            raise ValueError("Incompatible equipment")

    def _reserved_pair(self, tractor_id, wagon_id, owner):
        tractor = self._owned(tractor_id, owner)
        if (tractor_id, wagon_id) != self._reservations[owner][1]:
            raise ValueError("Not the reserved pair")
        wagon = self._owned(wagon_id, owner)
        self._compatible(tractor, wagon)
        return tractor, wagon

    def _unattached_pair(self, tractor_id, wagon_id, owner):
        tractor, wagon = self._reserved_pair(tractor_id, wagon_id, owner)
        if tractor.attached_to is not None or wagon.attached_to is not None:
            raise ValueError("Hitch steps require an unattached pair")
        return tractor, wagon

    def _carry(self, tractor, pose):
        # Mock-only: the tractor placed; a mounted implement keeps its offset to it.
        implement = self.items.get(tractor.attached_to)
        before = tractor.position
        self._place(tractor, pose)
        if implement is not None and implement.attached_to == tractor.id:
            dx, dz = pose.x - before[0], pose.z - before[1]
            self.items[implement.id] = replace(implement, position=(implement.position[0] + dx, implement.position[1] + dz),
                                               stopped=True)

    def _move_wagon(self, wagon, position):
        # Mock-only: what is belted on it rides along (GIANTS' tension-belt mount joint).
        dx, dz = position[0] - wagon.position[0], position[1] - wagon.position[1]
        # Its belt markers are its own nodes (GIANTS): they move with it.
        markers = tuple((m[0] + dx, m[1], m[2] + dz, m[3] + dx, m[4], m[5] + dz) for m in wagon.belt_markers)
        self.items[wagon.id] = replace(wagon, position=tuple(position), stopped=True, belt_markers=markers)
        for item in list(self.items.values()):
            if item.belted_to == wagon.id:
                moved = (item.position[0] + dx, item.position[1] + dz)
                root = item.ai_root and Pose(item.ai_root.x + dx, item.ai_root.z + dz, item.ai_root.yaw)
                self.items[item.id] = replace(item, position=moved, ai_root=root, stopped=True)

    def _place(self, tractor, pose):
        # Mock-only teleport, never a substitute for real navigation or reversing.
        if not all(math.isfinite(v) for v in (pose.x, pose.z, pose.yaw)):
            raise ValueError("Invalid pose")
        self.items[tractor.id] = replace(tractor, position=(pose.x, pose.z), ai_root=pose,
                                         heading=pose.yaw, stopped=True)

    def travel(self, tractor_id, destination_id, owner):
        with self._lock:
            ids = self._reservations.get(owner, (None, (None, None)))[1]
            tractor, _ = self._unattached_pair(tractor_id, ids[1], owner)
            destination = self.destination_map.get(destination_id)
            if destination is None or not all(math.isfinite(v) for v in destination.position):
                raise ValueError("Unknown destination")
            yaw = tractor.ai_root.yaw if tractor.ai_root else 0.0
            self._place(tractor, Pose(*destination.position, yaw))

    def navigate(self, tractor_id, pose, owner):
        # Mock-only teleport standing in for GIANTS GOTO; an attached wagon lands
        # MOCK_TRAILER_OFFSET_M behind the tractor's AI root.
        with self._lock:
            ids = self._reservations.get(owner, (None, (None, None)))[1]
            if owner in self._transports:         # the tractor (and what it carries) beside its transport
                tractor = self._owned(tractor_id, owner)
                if ids[0] != tractor_id or tractor.attached_to == ids[1]:
                    raise ValueError("Not the reserved tractor")
                self._start_blocked(tractor_id, "NAVIGATE_")
                self._carry(tractor, pose)
                return
            tractor, wagon = self._reserved_pair(tractor_id, ids[1], owner)
            coupled = (tractor.attached_to, wagon.attached_to) == (wagon.id, tractor.id)
            # A lone tractor may drive beside a reserved wagon another vehicle tows (transport).
            if not coupled and (tractor.attached_to is not None or wagon.attached_to == tractor.id):
                raise ValueError("Invalid attachment")
            self._start_blocked(tractor_id, "NAVIGATE_")
            self._place(tractor, pose)
            if coupled:
                behind = pose.to_world(0.0, -MOCK_TRAILER_OFFSET_M)
                self._move_wagon(wagon, behind)

    def stage(self, tractor_id, wagon_id, pose, owner):
        with self._lock:
            tractor, _ = self._unattached_pair(tractor_id, wagon_id, owner)
            self._start_blocked(tractor_id, "STAGE_")
            self._place(tractor, pose)

    def maneuver(self, tractor_id, wagon_id, plan, owner):
        # Mock-only teleport to the staging pose, standing in for the bounded forward
        # line-up; like the real step, only from within the local work radius.
        with self._lock:
            tractor, wagon = self._unattached_pair(tractor_id, wagon_id, owner)
            joint = next((j for j in wagon.input_joints if j.index == plan.wagon_joint), None)
            out = next((j for j in tractor.output_joints if j.index == plan.tractor_joint), None)
            root = tractor.ai_root
            if (joint is None or root is None or out is None
                    or math.dist(joint.position, plan.target) > MOCK_POSE_TOLERANCE_M
                    or math.dist(root.to_world(out.x, out.z), plan.target) > MOCK_LOCAL_RADIUS_M):
                raise RuntimeError("OUTSIDE_LOCAL_AREA")
            self._place(tractor, plan.staging)

    def align(self, tractor_id, wagon_id, plan, owner, check_only=False):
        with self._lock:
            tractor, wagon = self._unattached_pair(tractor_id, wagon_id, owner)
            if check_only:                    # look only: the candidate is there or not
                out = next((j for j in tractor.output_joints if j.index == plan.tractor_joint), None)
                if out is None or tractor.ai_root is None or math.dist(
                        tractor.ai_root.to_world(out.x, out.z), plan.target) > MOCK_ATTACH_RANGE_M:
                    raise RuntimeError("NO_CANDIDATE")
                return
            joint = next((j for j in wagon.input_joints if j.index == plan.wagon_joint), None)
            root = tractor.ai_root
            # Like the real step, only reverse towards an unmoved wagon from within the
            # reverse's reach of its joint (never from the exact staging pose alone).
            out = next((j for j in tractor.output_joints if j.index == plan.tractor_joint), None)
            if (joint is None or root is None or out is None
                    or math.dist(joint.position, plan.target) > MOCK_POSE_TOLERANCE_M
                    or math.dist(root.to_world(out.x, out.z), plan.target) > MOCK_REVERSE_REACH_M):
                raise RuntimeError("Not within reach of this wagon")
            self._place(tractor, plan.final)

    def deliver(self, tractor_id, destination_id, owner):
        with self._lock:
            ids = self._reservations.get(owner, (None, (None, None)))[1]
            tractor, wagon = self._reserved_pair(tractor_id, ids[1], owner)
            if tractor.attached_to != wagon.id or wagon.attached_to != tractor.id:
                raise ValueError("Invalid attachment")
            destination = self.destination_map.get(destination_id)
            if destination is None or not all(math.isfinite(v) for v in destination.position):
                raise ValueError("Unknown destination")
            # Mock-only teleport of the coupled pair; AutoDrive does real routing.
            yaw = tractor.ai_root.yaw if tractor.ai_root else 0.0
            self._place(tractor, Pose(*destination.position, yaw))
            self._move_wagon(wagon, destination.position)

    def attach(self, tractor_id, wagon_id, joints, owner):
        with self._lock:
            tractor, wagon = self._unattached_pair(tractor_id, wagon_id, owner)
            out = next((j for j in tractor.output_joints if j.index == joints[0]), None)
            inp = next((j for j in wagon.input_joints if j.index == joints[1]), None)
            if (out is None or inp is None or tractor.ai_root is None
                    or not out.free or not inp.active or out.joint_type != inp.joint_type
                    or wagon.required_pto_torque and not (
                        out.pto_names is not None and inp.pto_names is not None
                        and (not inp.pto_names or out.pto_names & inp.pto_names))
                    or not tractor.stopped or not wagon.stopped
                    or math.dist(tractor.ai_root.to_world(out.x, out.z), inp.position)
                    > MOCK_ATTACH_RANGE_M):
                raise RuntimeError("Attach rejected")
            joints_after = tuple(replace(j, free=False) if j.index == out.index else j
                                 for j in tractor.output_joints)
            self.items[tractor.id] = replace(tractor, attached_to=wagon.id, output_joints=joints_after)
            self.items[wagon.id] = replace(wagon, attached_to=tractor.id)

    def detach(self, tractor_id, wagon_id, owner):
        with self._lock:
            tractor, wagon = self._reserved_pair(tractor_id, wagon_id, owner)
            if (tractor.attached_to, wagon.attached_to) != (wagon.id, tractor.id):
                raise RuntimeError("NOT_ATTACHED")
            if not tractor.stopped or not wagon.stopped:
                raise RuntimeError("NOT_STOPPED")
            joints_after = tuple(replace(j, free=True) if not j.free else j for j in tractor.output_joints)
            self.items[tractor.id] = replace(tractor, attached_to=None, output_joints=joints_after)
            self.items[wagon.id] = replace(wagon, attached_to=None)

    def pull_clear(self, tractor_id, wagon_id, owner):
        # Mock-only teleport straight ahead, standing in for the bounded pull-clear;
        # like the real step, only for an unattached pair, forward, within its cap.
        with self._lock:
            tractor, implement = self._unattached_pair(tractor_id, wagon_id, owner)
            root = tractor.ai_root
            gaps = [math.dist(root.to_world(j.x, j.z), i.position)
                    for j in tractor.output_joints for i in implement.input_joints]
            if not gaps:
                raise RuntimeError("UNKNOWN_JOINT")
            if min(gaps) >= MOCK_PULL_CLEAR_M:
                raise StepFailed("ALREADY_CLEAR")
            ahead = MOCK_PULL_CLEAR_M + 0.1 - min(gaps)
            if ahead > MOCK_PULL_MAX_M:
                raise StepFailed("DISTANCE_LIMIT")
            x, z = root.to_world(0.0, ahead)
            self._place(tractor, Pose(x, z, root.yaw))

    def stop(self, tractor_id, owner):
        with self._lock:
            reservation = self._reservations.get(owner)
            if not reservation or tractor_id != reservation[1][0]:
                return False
            tractor = self.items.get(tractor_id)
            if (tractor is None or tractor.reserved_by != owner or tractor.occupied
                    or "stop" not in tractor.capabilities):
                return False
            owned = [self.items.get(identifier) for identifier in reservation[1]]
            if any(item is None or item.occupied or item.reserved_by != owner
                   or item.farm_id != reservation[0] for item in owned):
                return False
            if tractor_id in self.mock_workers:          # a handed-off helper: its job ends
                self.mock_workers = {k: v for k, v in self.mock_workers.items() if k != tractor_id}
                self.items[tractor_id] = replace(self.items[tractor_id], busy=False, busy_reason=None)
                owned = [self.items[identifier] for identifier in reservation[1]]
            for item in owned:
                self.items[item.id] = replace(item, stopped=True)
            return True

    def release(self, owner):
        with self._lock:
            reservation = self._reservations.get(owner)
            if not reservation:
                return
            owned = [self.items.get(identifier) for identifier in reservation[1]]
            if any(item is None or not item.stopped or item.reserved_by != owner
                   or item.farm_id != reservation[0] for item in owned):
                raise ValueError("Cannot release moving or changed equipment")
            for item in owned:
                self.items[item.id] = replace(item, reserved_by=None)
            del self._reservations[owner]
