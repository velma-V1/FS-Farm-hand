"""Synthetic world only: coordinates change instantly; no game APIs or physics."""
from dataclasses import replace
import math
from threading import RLock

from protocol.model import Destination, Equipment, Pose

# Mock-only stand-ins for GIANTS' attach-range decision and AIJobGoTo accuracy.
# They are NOT GIANTS' rules (adapters/EVIDENCE.md); they only let orchestration
# tests distinguish "where the plan said" from "somewhere else".
MOCK_ATTACH_RANGE_M = 0.3
MOCK_POSE_TOLERANCE_M = 0.5
MOCK_TRAILER_OFFSET_M = 7.0
MOCK_REVERSE_REACH_M = 10.0      # the mod's reverse cap (Hitch.LIMITS.maxDistanceM)
MOCK_LOCAL_RADIUS_M = 25.0       # the mod's local work radius (Hitch.LOCAL_LIMITS.workRadiusM)


class MockWorld:
    def __init__(self, equipment, players, destinations=(), map_id=None, places=()):
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

    def map_id(self):
        with self._lock:
            return self.current_map_id

    def reserve(self, ids, owner):
        with self._lock:
            if not owner or len(set(ids)) != 2 or any(i not in self.items for i in ids):
                return False
            if owner in self._reservations or any(x.reserved_by == owner for x in self.items.values()):
                return False
            pair = [self.items[i] for i in ids]
            # Unattached, or hitched to each other (move_pair); never to anything else.
            partners = {pair[0].id: pair[1].id, pair[1].id: pair[0].id}
            if any(item.reserved_by is not None or item.busy or item.occupied
                   or not item.available or item.attached_to not in (None, partners[item.id])
                   or not item.stopped for item in pair):
                return False
            if (pair[0].attached_to is None) != (pair[1].attached_to is None):
                return False
            if pair[0].farm_id != pair[1].farm_id:
                return False
            for item in pair:
                self.items[item.id] = replace(item, reserved_by=owner)
            self._reservations[owner] = (pair[0].farm_id, tuple(ids))
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
            tractor, wagon = self._reserved_pair(tractor_id, ids[1], owner)
            coupled = (tractor.attached_to, wagon.attached_to) == (wagon.id, tractor.id)
            if not coupled and (tractor.attached_to is not None or wagon.attached_to is not None):
                raise ValueError("Invalid attachment")
            self._place(tractor, pose)
            if coupled:
                behind = pose.to_world(0.0, -MOCK_TRAILER_OFFSET_M)
                self.items[wagon.id] = replace(wagon, position=behind, stopped=True)

    def stage(self, tractor_id, wagon_id, pose, owner):
        with self._lock:
            tractor, _ = self._unattached_pair(tractor_id, wagon_id, owner)
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
            self.items[wagon.id] = replace(wagon, position=destination.position, stopped=True)

    def attach(self, tractor_id, wagon_id, joints, owner):
        with self._lock:
            tractor, wagon = self._unattached_pair(tractor_id, wagon_id, owner)
            out = next((j for j in tractor.output_joints if j.index == joints[0]), None)
            inp = next((j for j in wagon.input_joints if j.index == joints[1]), None)
            if (out is None or inp is None or tractor.ai_root is None
                    or not out.free or not inp.active or out.joint_type != inp.joint_type
                    or wagon.required_pto_torque and not (out.pto_names and inp.pto_names
                                                          and out.pto_names & inp.pto_names)
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
