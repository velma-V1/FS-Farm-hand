"""UNPARK in mod/FarmhandHitch.lua (Lua 5.1 and 5.4) on a kinematic stub (NOT GIANTS
physics): oriented boxes stand in for collision shapes, answered in GIANTS' overlapBox
call shape (GDN engine Physics: synchronous, half extents, the callback per shape, true
ends the query). It proves the bounded state machine: straight only, at most 20 m and
3 km/h, back only with nothing attached, the whole rig's swept volume clear with the
margin before it moves, the nearest exit, every guard while moving, and the
vehicle-control state restored on every exit.

Live motive (53c77d1e809645b3-3, FourFields save 2): the Super-Liner and its hopper
inside the Large Shed, GIANTS' agent BLOCKED from the first frame, unmoved.
"""
import math
from pathlib import Path
import unittest

from lupa import lua51, lua54

from companion import executor as companion_executor

ROOT = Path(__file__).resolve().parents[1]

STUBS = r"""
local Hitch = ...
local W = {}
local F = { DEFAULT = 1, STATIC_OBJECT = 2, DYNAMIC_OBJECT = 4, VEHICLE = 8, TERRAIN_DELTA = 16, TREE = 32,
            BUILDING = 64, TRAFFIC_VEHICLE = 128, TRAFFIC_VEHICLE_BLOCKING = 256, TERRAIN = 512 }
W.F = F
local function has(mask, flag) return math.floor(mask / flag) % 2 == 1 end
local function node(x, z, yaw) return { x = x, y = 0, z = z, yaw = yaw or 0 } end

-- Oriented rectangles: centre, yaw (local +z is (sin, cos)), half extents along local x / z.
local function axes(r)
    local s, c = math.sin(r.yaw), math.cos(r.yaw)
    return { { c, -s }, { s, c } }
end
local function project(r, ax)
    local a = axes(r)
    local c = r.x * ax[1] + r.z * ax[2]
    local e = r.hx * math.abs(a[1][1] * ax[1] + a[1][2] * ax[2]) + r.hz * math.abs(a[2][1] * ax[1] + a[2][2] * ax[2])
    return c - e, c + e
end
local function overlaps(p, q)
    if p.y1 < q.y0 or q.y1 < p.y0 then return false end
    for _, r in ipairs({ p, q }) do
        for _, ax in ipairs(axes(r)) do
            local a0, a1 = project(p, ax)
            local b0, b1 = project(q, ax)
            if a1 < b0 or b1 < a0 then return false end
        end
    end
    return true
end
local function inside(r, x, z)
    local a = axes(r)
    local dx, dz = x - r.x, z - r.z
    return math.abs(dx * a[1][1] + dz * a[1][2]) <= r.hx and math.abs(dx * a[2][1] + dz * a[2][2]) <= r.hz
end

-- The rig: the tractor (its root is its steering node) facing `yaw` at (x, z), sized
-- o.size; o.trailer: a towed unit `back` metres behind, sized, turned `turn` rad.
function W.new(o)
    local w = { drives = {}, events = {}, shapes = {}, objects = {}, names = {}, returns = {}, others = {} }
    local env = { NetworkUtil = { getObjectId = function() return 11 end }, CollisionFlag = {} }
    for k, v in pairs(F) do env.CollisionFlag[k] = v end
    w.env = env
    local yaw = o.yaw or 0.7
    w.yaw = yaw
    w.steer = node(o.x or 512.3, o.z or -233.8, yaw)
    w.speed, w.entered, w.ai, w.reserved, w.motor = 0, false, false, true, true
    w.ground = 0
    local size = o.size or { width = 2.6, length = 5.2, height = 3.0 }
    local tractor = { ownerFarmId = 1, rootNode = w.steer,
                      size = { width = size.width, length = size.length, height = size.height, widthOffset = 0,
                               lengthOffset = 0, heightOffset = 0 } }
    tractor.rootVehicle = tractor
    w.tractor = tractor
    w.attached = {}
    function tractor:getAttachedImplements() return w.attached end
    function tractor:getIsEntered() return w.entered end
    function tractor:registerPlayerVehicleControlAllowedFunction(_, f) w.controlFunc = f end
    w.takeOver = function() return w.controlFunc(tractor) end
    function tractor:getIsAIActive() return w.ai end
    function tractor:setCruiseControlState(s) table.insert(w.events, "cruise:" .. s) end
    function tractor:getIsMotorStarted() return w.motor end
    function tractor:getCanMotorRun() return true end
    function tractor:startMotor() w.motor = true end
    function tractor:raiseActive() end
    function tractor:getAISteeringNode() return w.steer end
    function tractor:getAIReverserNode() return nil end
    function tractor:getLastSpeed() return w.speed end
    -- A unit's own collision: its size box, a little inside it.
    local function body(id, v)
        w.objects[id] = v
        w.names[id] = "body" .. id
        table.insert(w.shapes, { id = id, group = F.VEHICLE, rect = function()
            local r = v.rootNode
            return { x = r.x, z = r.z, yaw = r.yaw, hx = v.size.width / 2 - 0.1, hz = v.size.length / 2 - 0.1,
                     y0 = 0.2, y1 = v.size.height - 0.1 }
        end })
    end
    body(101, tractor)
    if o.trailer then
        local t = o.trailer
        local tyaw = yaw + (t.turn or 0)
        local tx = w.steer.x - t.back * math.sin(yaw)
        local tz = w.steer.z - t.back * math.cos(yaw)
        local trailer = { ownerFarmId = 1, rootNode = node(tx, tz, tyaw), rootVehicle = tractor,
                          size = { width = t.width, length = t.length, height = t.height, widthOffset = 0,
                                   lengthOffset = 0, heightOffset = 0 } }
        w.trailer = trailer
        w.attached = { { object = trailer } }
        body(102, trailer)
    end
    env.Drivable = { CRUISECONTROL_STATE_OFF = 0 }
    env.getWorldTranslation = function(n) return n.x, n.y, n.z end
    env.localDirectionToWorld = function(n, x, y, z)
        local s, c = math.sin(n.yaw), math.cos(n.yaw)
        return x * c + z * s, y, -x * s + z * c
    end
    env.localToWorld = function(n, x, y, z)
        local s, c = math.sin(n.yaw), math.cos(n.yaw)
        return n.x + x * c + z * s, n.y + y, n.z - x * s + z * c
    end
    env.worldToLocal = function(n, x, y, z)
        local dx, dz = x - n.x, z - n.z
        return dx * math.cos(n.yaw) - dz * math.sin(n.yaw), y - n.y, dx * math.sin(n.yaw) + dz * math.cos(n.yaw)
    end
    env.getTerrainHeightAtWorldPos = function() return w.ground end
    env.getName = function(id) return w.names[id] or ("node" .. tostring(id)) end
    -- GIANTS' overlapBox call shape: every overlapping shape the mask lets through gets a
    -- callback (the terrain first), until one returns true; the count is returned.
    env.overlapBox = function(x, y, z, rx, ry, rz, ex, ey, ez, cb, target, mask, dyn, kin, stat, exact)
        w.queries = (w.queries or 0) + 1
        local q = { x = x, z = z, yaw = ry, hx = ex, hz = ez, y0 = y - ey, y1 = y + ey }
        local n = 0
        local function report(id)
            n = n + 1
            local ret = target[cb](target, id, 0)
            table.insert(w.returns, ret)
            return ret == true
        end
        if has(mask, F.TERRAIN) and not w.noTerrain and q.y0 <= w.ground and w.ground <= q.y1 then
            if report(1) then return n + (w.countSkew or 0) end
        end
        for _, shape in ipairs(w.shapes) do
            if has(mask, shape.group) and overlaps(q, shape.rect()) then
                if report(shape.id) then break end
            end
        end
        return n + (w.countSkew or 0)
    end
    -- GIANTS' navigation map (stub): ground under any static shape is not reachable.
    local vehicles = { tractor, w.trailer }
    env.g_currentMission = { terrainRootNode = 1, vehicleSystem = { vehicles = vehicles },
        getNodeObject = function(_, id) if w.unresolved then return nil end return w.objects[id] end,
        aiSystem = { getIsPositionReachable = function(_, x, _, z)
            if w.reachUnknown then return nil end
            for _, shape in ipairs(w.shapes) do
                if shape.static and inside(shape.rect(), x, z) then return false end
            end
            return true
        end } }
    w.player = node(w.steer.x + 60, w.steer.z, 0)
    env.g_localPlayer = { getPosition = function() return w.player.x, 0, w.player.z end }
    -- A box in the start frame of the tractor (a ahead, l to its local +x side).
    function w.box(a0, a1, l0, l1, y0, y1, group, name, object)
        local s, c = math.sin(yaw), math.cos(yaw)
        local x0, z0 = o.x or 512.3, o.z or -233.8
        local ca, cl = (a0 + a1) / 2, (l0 + l1) / 2
        local id = #w.shapes + 200
        local shape = { id = id, group = group or F.BUILDING, static = object == nil and (group or F.BUILDING) ~= F.DYNAMIC_OBJECT }
        local rect = { x = x0 + s * ca + c * cl, z = z0 + c * ca - s * cl, yaw = yaw, hx = (l1 - l0) / 2,
                       hz = (a1 - a0) / 2, y0 = y0, y1 = y1 }
        shape.rect = function() return rect end
        table.insert(w.shapes, shape)
        w.names[id] = name or ("shape" .. id)
        if object then w.objects[id] = object end
        return shape
    end
    -- A parked vehicle with its own collision, at (a, l) of the start frame.
    function w.vehicle(a, l, length, width)
        local s, c = math.sin(yaw), math.cos(yaw)
        local x0, z0 = o.x or 512.3, o.z or -233.8
        local v = { rootNode = node(x0 + s * a + c * l, z0 + c * a - s * l, yaw) }
        v.rootVehicle = v
        table.insert(vehicles, v)
        table.insert(w.others, v)
        w.box(a - (length or 4) / 2, a + (length or 4) / 2, l - (width or 2.5) / 2, l + (width or 2.5) / 2, 0.2, 2.8,
              F.VEHICLE, "parked", v)
        return v
    end
    -- An open-ended shed in the start frame: interior a0..a1, half width `half`, a back
    -- wall at a0 (o.backAtFront: at a1 instead), side walls, a roof at `roof` metres.
    function w.shed(a0, a1, half, roof, backAtFront)
        local back = backAtFront and a1 or a0
        local sign = backAtFront and 1 or -1
        w.box(math.min(back, back + sign * 0.3), math.max(back, back + sign * 0.3), -half - 0.3, half + 0.3, 0, roof,
              F.BUILDING, "backWall")
        w.box(a0, a1, half, half + 0.3, 0, roof, F.BUILDING, "sideWallL")
        w.box(a0, a1, -half - 0.3, -half, 0, roof, F.BUILDING, "sideWallR")
        w.box(a0, a1, -half - 0.3, half + 0.3, roof, roof + 0.3, F.BUILDING, "roof")
    end
    -- Kinematics: straight along the tractor's heading, forward or back; the towed unit
    -- moves with it. w.drift: metres sideways per call; w.stuck: no motion.
    env.AIVehicleUtil = {}
    function env.AIVehicleUtil.driveToPoint(v, dt, acc, allowed, forwards, lx, lz, maxSpeed)
        table.insert(w.drives, { allowed = allowed, forwards = forwards, lx = lx, lz = lz, maxSpeed = maxSpeed })
        table.insert(w.events, allowed and "drive" or "brake")
        if not allowed or acc <= 0 or w.stuck then w.speed = 0; return end
        local ds = (forwards and 1 or -1) * maxSpeed / 3.6 * dt / 1000
        local dx, dz = ds * math.sin(w.steer.yaw), ds * math.cos(w.steer.yaw)
        local px, pz = (w.drift or 0) * math.cos(w.steer.yaw), -(w.drift or 0) * math.sin(w.steer.yaw)
        for _, n in ipairs({ w.steer, w.trailer and w.trailer.rootNode }) do
            n.x, n.z = n.x + dx + px, n.z + dz + pz
        end
        w.speed = (forwards and 1 or -1) * maxSpeed
    end
    return w
end

function W.args(w, overrides)
    local a = { tractor = w.tractor, farmId = 1, isReserved = function() return w.reserved end }
    for k, v in pairs(overrides or {}) do a[k] = v end
    return a
end

-- Run until terminal; `hook(i)` mutates the world.
function W.run(w, s, maxSteps, hook)
    for i = 1, maxSteps do
        if hook then hook(i) end
        local code = Hitch.unparkStep(w.env, s, 50)
        if code ~= "CONTINUE" then return code, i end
    end
    return "CONTINUE", maxSteps
end

-- The steering node's travel along the start heading (signed) and sideways.
function W.travel(w, x0, z0)
    local s, c = math.sin(w.yaw), math.cos(w.yaw)
    local dx, dz = w.steer.x - x0, w.steer.z - z0
    return dx * s + dz * c, dx * c - dz * s
end

return W
"""

TRUCK = dict(width=2.5, length=7.0, height=3.6)
HOPPER = dict(back=9.0, width=2.8, length=10.0, height=3.8)


class LuaUnparkTests(unittest.TestCase):
    runtime = lua54

    def setUp(self):
        self.lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        self.hitch = self.lua.execute((ROOT / "mod/FarmhandHitch.lua").read_text())
        self.W = self.lua.execute(STUBS, self.hitch)

    def world(self, size=None, trailer=None, **opts):
        o = dict(opts)
        if size:
            o["size"] = self.lua.table(**size)
        if trailer:
            o["trailer"] = self.lua.table(**trailer)
        return self.W.new(self.lua.table(**o))

    def start(self, w, **overrides):
        """(session, reason, refused session) from Hitch.newUnpark."""
        out = self.hitch.newUnpark(w.env, self.W.args(w, self.lua.table(**overrides)))
        out = out if isinstance(out, tuple) else (out,)
        return out + (None,) * (3 - len(out))

    def drives(self, w):
        return [dict(d) for d in w.drives.values()]

    def moving(self, w):
        return [d for d in self.drives(w) if d["allowed"]]

    def assert_ended_braked_and_restored(self, w):
        events = list(w.events.values())
        self.assertEqual(events[-2:], ["brake", "cruise:0"])
        self.assertNotEqual(w.tractor.forceIsActive, True)

    def extract(self, w, steps=2000, hook=None, **overrides):
        x0, z0 = w.steer.x, w.steer.z
        session, why, _ = self.start(w, **overrides)
        self.assertIsNotNone(session, why)
        code, _ = self.W.run(w, session, steps, hook)
        along, lateral = self.W.travel(w, x0, z0)
        return session, code, along, lateral

    def assert_refused(self, w, reason, **overrides):
        session, why, _ = self.start(w, **overrides)
        self.assertIsNone(session)
        self.assertEqual(why, reason)
        self.assertEqual(self.drives(w), [])

    # ---------------------------------------------------------------- limits
    def test_mirrors_the_companion(self):
        self.assertEqual(self.hitch.UNPARK_LIMITS.maxDistanceM, companion_executor.UNPARK_MAX_M)

    def test_limits_are_hard_capped(self):
        for overrides in ({"maxDistanceM": 20.5}, {"maxSpeedKmh": 3.5}, {"maxSpeedKmh": 0.5},
                          {"timeoutMs": 41000}, {"isReserved": False}):
            with self.subTest(**overrides):
                w = self.world()
                w.shed(-9, 6, 4, 4.5)
                self.assert_refused(w, "INVALID_LIMITS", **overrides)

    # -------------------------------------------------------------- the exit
    def test_drives_straight_out_of_an_open_front_shed_to_the_nearest_exit(self):
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        session, code, along, lateral = self.extract(w)
        self.assertEqual(code, "CLEAR", self.hitch.describeUnpark(session))
        moving = self.moving(w)
        self.assertTrue(moving and all(d["forwards"] and d["maxSpeed"] == 3 for d in moving))
        # The tractor's rear (2.6 m behind its node) out from under the roof (6 m): 8.6 m,
        # planned in 0.5 m slabs; never farther than that exit.
        self.assertAlmostEqual(session.exitM, 9.0)
        self.assertGreaterEqual(along, 9.0)
        self.assertLess(along, 9.3)
        self.assertAlmostEqual(lateral, 0.0, places=6)
        self.assertAlmostEqual(w.steer.yaw, w.yaw, places=9)
        self.assert_ended_braked_and_restored(w)
        self.assertEqual(self.hitch.unparkStep(w.env, session, 50), "CLEAR")   # latched: brakes only
        self.assertFalse(self.drives(w)[-1]["allowed"])
        self.assertNotIn(True, list(w.returns.values()))          # never ends GIANTS' query early

    def test_a_vehicle_parked_nose_in_backs_out_when_nothing_is_attached(self):
        w = self.world(yaw=-2.1, x=-75.0, z=301.4)
        w.shed(-6, 9, 4, 4.5, True)                                # the back wall ahead of it
        session, code, along, _ = self.extract(w)
        self.assertEqual(code, "CLEAR", self.hitch.describeUnpark(session))
        moving = self.moving(w)
        self.assertTrue(moving and not any(d["forwards"] for d in moving))
        self.assertLess(along, -9.0)
        self.assertGreater(along, -9.3)
        self.assertIn("UNPARK_EXIT_BLOCKED", self.hitch.describeUnpark(session))   # forward: the back wall

    def test_a_rig_with_a_towed_unit_never_reverses(self):
        w = self.world(size=TRUCK, trailer=HOPPER)
        w.shed(-16, 9, 4, 5.0, True)
        session, why, refused = self.start(w)
        self.assertIsNone(session)
        self.assertEqual(why, "UNPARK_EXIT_BLOCKED")
        self.assertIn("UNPARK_REVERSE_NOT_ALLOWED", self.hitch.describeUnpark(refused))
        self.assertEqual(self.drives(w), [])

    def test_the_super_liner_and_hopper_leave_the_shed_forward_whole(self):
        w = self.world(size=TRUCK, trailer=HOPPER, yaw=0.15, x=955.6, z=779.6)
        w.shed(-16, 4, 4, 5.0)
        session, code, along, _ = self.extract(w)
        self.assertEqual(code, "CLEAR", self.hitch.describeUnpark(session))
        # The hopper's rear (14 m behind the truck's node) out from under the roof (4 m):
        # 18 m only touches the roof's edge, which counts as under it; the next slab.
        self.assertAlmostEqual(session.exitM, 18.5)
        self.assertGreaterEqual(along, 18.5)
        self.assertLessEqual(along, 18.8)
        self.assertEqual(session.sign, 1)

    def test_the_exit_must_lie_within_the_cap(self):
        w = self.world(size=TRUCK, trailer=HOPPER)
        w.shed(-16, 8, 4, 5.0)                                   # 22 m to the rear's exit
        self.assert_refused(w, "UNPARK_EXIT_TOO_FAR")
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        self.assert_refused(w, "UNPARK_EXIT_TOO_FAR", maxDistanceM=5)

    def test_a_vehicle_already_in_the_open_is_not_unparked(self):
        # GIANTS refused for another reason (a destination problem): never compensated.
        w = self.world()
        self.assert_refused(w, "UNPARK_NOT_BOXED")

    # ------------------------------------------------- the swept volume, clear
    def test_anything_in_the_swept_path_blocks_it_before_any_motion(self):
        cases = {
            "a low beam over the door": lambda w: w.box(6, 6.3, -4, 4, 2.8, 3.2, self.W.F.BUILDING, "beam"),
            "a pallet": lambda w: w.box(10, 11, -0.6, 0.6, 0, 1.2, self.W.F.DYNAMIC_OBJECT, "pallet"),
            "a tree": lambda w: w.box(12, 12.5, 1.0, 1.5, 0, 9, self.W.F.TREE, "tree"),
            "a parked vehicle's body": lambda w: w.box(11, 13, 1.5, 3.0, 0.2, 2.8, self.W.F.VEHICLE, "car", self.lua.table()),
        }
        for name, add in cases.items():
            with self.subTest(name):
                w = self.world()
                w.shed(-9, 6, 4, 4.5)
                add(w)
                session, why, refused = self.start(w)
                self.assertIsNone(session)
                self.assertEqual(why, "UNPARK_EXIT_BLOCKED")
                self.assertEqual(self.drives(w), [])
                self.assertIn("UNPARK_EXIT_BLOCKED", self.hitch.describeUnpark(refused))

    def test_a_beam_above_the_rig_and_its_margin_lets_it_pass(self):
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        w.box(6, 6.3, -4, 4, 3.4, 3.8, self.W.F.BUILDING, "beam")
        self.assertEqual(self.extract(w)[1], "CLEAR")

    def test_the_whole_rig_is_checked_not_only_the_tractor(self):
        wide = dict(HOPPER, width=3.4)
        cases = {
            # A post beside the truck's path but inside the wider hopper's (1.7 m + 0.5 m).
            "width": lambda w: w.box(6, 6.4, 1.9, 2.0, 0, 3, self.W.F.STATIC_OBJECT, "post"),
            # A beam over the truck's 3.6 m + 0.3 m but under the hopper's 3.8 m + 0.3 m.
            "height": lambda w: w.box(6, 6.3, -4, 4, 3.95, 4.05, self.W.F.BUILDING, "beam"),
        }
        for name, add in cases.items():
            with self.subTest(name):
                w = self.world(size=TRUCK, trailer=wide)
                w.shed(-16, 4, 4, 5.0)
                add(w)
                self.assert_refused(w, "UNPARK_EXIT_BLOCKED")

    def test_a_bay_narrower_than_the_rig_and_its_margin_is_refused(self):
        w = self.world()
        w.shed(-9, 6, 1.6, 4.5)
        self.assert_refused(w, "UNPARK_TOO_TIGHT")

    def test_other_vehicles_and_the_player_keep_pull_clears_distances(self):
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        w.vehicle(14, 6.5)                                        # its root 6.5 m off the line
        self.assert_refused(w, "UNPARK_BLOCKED")
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        w.player.x, w.player.z = w.steer.x + 10 * math.sin(w.yaw), w.steer.z + 10 * math.cos(w.yaw)
        self.assert_refused(w, "UNPARK_PERSON")

    def test_a_vehicle_parked_beside_or_behind_counts_only_by_its_body(self):
        # Live bc8307516e994d85-1: the PROLANDER's root 5.6 m beside the 7R in the Open Hall.
        # The user, 2026-10-20: the 8 m clearance only for vehicles ahead of the leading end.
        for name, (a, l) in {"beside": (0.0, 5.6), "behind": (-8.0, 0.0)}.items():
            with self.subTest(name):
                w = self.world()
                w.shed(-12, 6, 8, 4.5)
                w.vehicle(a, l, 4.0, 2.4)
                session, code, along, _ = self.extract(w)
                self.assertEqual(code, "CLEAR", self.hitch.describeUnpark(session))
                self.assertGreater(along, 8.5)
        w = self.world()
        w.shed(-12, 6, 8, 4.5)
        w.vehicle(0.0, 2.8, 4.0, 2.4)                             # its body inside the path's margin
        self.assert_refused(w, "UNPARK_TOO_TIGHT")

    def test_the_player_keeps_the_distance_on_every_side(self):
        w = self.world()
        w.shed(-12, 6, 8, 4.5)
        s, c = math.sin(w.yaw), math.cos(w.yaw)
        w.player.x, w.player.z = w.steer.x + 5.0 * c, w.steer.z - 5.0 * s     # 5 m to its side
        self.assert_refused(w, "UNPARK_PERSON")

    def test_cargo_or_a_wall_touching_the_rig_where_it_stands_is_refused(self):
        w = self.world(size=TRUCK, trailer=HOPPER)
        w.shed(-16, 4, 4, 5.0)
        w.box(-12, -6, -1.0, 1.0, 1.5, 2.5, self.W.F.DYNAMIC_OBJECT, "bigBag")   # on the hopper
        session, why, refused = self.start(w)
        self.assertEqual(why, "UNPARK_START_NOT_CLEAR")
        self.assertIn("bigBag", self.hitch.describeUnpark(refused))
        self.assertEqual(self.drives(w), [])

    # ------------------------------------------------------ the query itself
    def test_a_query_that_cannot_prove_itself_is_never_clear(self):
        cases = {
            "count differs from callbacks": lambda w: setattr(w, "countSkew", 1),
            "own body not resolvable": lambda w: setattr(w, "unresolved", True),
            "terrain not reported": lambda w: setattr(w, "noTerrain", True),
        }
        for name, spoil in cases.items():
            with self.subTest(name):
                w = self.world()
                w.shed(-9, 6, 4, 4.5)
                spoil(w)
                self.assert_refused(w, "UNPARK_QUERY_UNVERIFIED")
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        w.env.CollisionFlag.BUILDING = None
        self.assert_refused(w, "UNPARK_QUERY_UNKNOWN")
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        w.reachUnknown = True
        self.assert_refused(w, "UNPARK_QUERY_UNKNOWN")

    # ------------------------------------------------------------- refusals
    def test_refusals_never_move(self):
        cases = [
            (lambda w: setattr(w, "entered", True), "SEATED_DURING_UNPARK"),
            (lambda w: setattr(w, "speed", 2.0), "NOT_STOPPED"),
            (lambda w: setattr(w, "ai", True), "AI_ACTIVE"),
            (lambda w: setattr(w, "reserved", False), "RESERVATION_LOST"),
            (lambda w: setattr(w.tractor, "spec_articulatedAxis", self.lua.table(componentJoint=self.lua.table())),
             "UNPARK_ARTICULATED_UNSUPPORTED"),
            (lambda w: setattr(w.tractor.size, "height", None), "UNPARK_GEOMETRY_UNKNOWN"),
        ]
        for setup, reason in cases:
            with self.subTest(reason=reason):
                w = self.world()
                w.shed(-9, 6, 4, 4.5)
                setup(w)
                self.assert_refused(w, reason)

    def test_a_rigid_tractor_carrying_the_articulated_spec_is_not_refused(self):
        # Live 758c6fbfdbf94125-2: the 7R and the X8 have spec_articulatedAxis, no componentJoint.
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        w.tractor.spec_articulatedAxis = self.lua.table()
        self.assertEqual(self.extract(w)[1], "CLEAR")

    def test_a_rig_that_is_not_straight_is_refused(self):
        w = self.world(size=TRUCK, trailer=dict(HOPPER, turn=0.3))
        w.shed(-16, 4, 5, 5.0)
        self.assert_refused(w, "UNPARK_RIG_NOT_STRAIGHT")

    # ------------------------------------------------------- while it moves
    def test_every_conflict_while_moving_stops_it(self):
        def ahead(w, i):
            if i == 40:   # 1.5 m past the tractor's front, inside the lookahead
                s, c = math.sin(w.yaw), math.cos(w.yaw)
                travelled = (w.steer.x - 512.3) * s + (w.steer.z + 233.8) * c
                w.box(2.6 + travelled + 1.4, 2.6 + travelled + 1.8, -0.5, 0.5, 0, 1, self.W.F.DYNAMIC_OBJECT, "bale")

        cases = [
            (lambda w, i: i == 10 and w.takeOver(), "DRIVER_TOOK_OVER"),
            (lambda w, i: i == 10 and setattr(w, "entered", True), "SEATED_DURING_UNPARK"),
            (lambda w, i: i == 10 and setattr(w, "reserved", False), "RESERVATION_LOST"),
            (lambda w, i: i == 10 and setattr(w, "ai", True), "AI_ACTIVE"),
            (lambda w, i: i == 10 and setattr(w, "stuck", True), "STALLED"),
            (lambda w, i: i == 10 and setattr(w, "drift", 0.05), "DIVERGED"),
            (lambda w, i: i == 10 and (setattr(w.player, "x", w.steer.x + 5 * math.sin(w.yaw))
                                       or setattr(w.player, "z", w.steer.z + 5 * math.cos(w.yaw))),
             "PERSON_TOO_CLOSE"),
            (ahead, "OBSTACLE_AHEAD"),
        ]
        for hook, reason in cases:
            with self.subTest(reason=reason):
                w = self.world()
                w.shed(-9, 6, 4, 4.5)
                session, code, _, _ = self.extract(w, hook=lambda i, w=w, hook=hook: hook(w, i))
                self.assertEqual(code, reason, self.hitch.describeUnpark(session))
                self.assert_ended_braked_and_restored(w)

    def test_a_change_to_the_rig_stops_it(self):
        def turn(w, i):
            if i == 10:
                w.trailer.rootNode.yaw = w.trailer.rootNode.yaw + 0.1

        cases = {"unhitched": lambda w, i: i == 10 and setattr(w, "attached", self.lua.table()), "turned": turn}
        for name, hook in cases.items():
            with self.subTest(name):
                w = self.world(size=TRUCK, trailer=HOPPER)
                w.shed(-16, 4, 4, 5.0)
                _, code, _, _ = self.extract(w, hook=lambda i, w=w, hook=hook: hook(w, i))
                self.assertEqual(code, "RIG_CHANGED")
                self.assert_ended_braked_and_restored(w)

    def test_another_vehicle_moving_nearby_stops_it(self):
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        other = w.vehicle(0, 14)                                   # parked beside, clear of the line
        _, code, _, _ = self.extract(w, hook=lambda i: i == 10 and setattr(other.rootNode, "z", other.rootNode.z + 1))
        self.assertEqual(code, "OTHER_VEHICLE_MOVED")
        self.assert_ended_braked_and_restored(w)

    def test_bounded_by_time(self):
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        self.assertEqual(self.extract(w, timeoutMs=1000)[1], "TIMEOUT")
        self.assert_ended_braked_and_restored(w)

    def test_the_exit_is_confirmed_from_fresh_queries_before_it_counts(self):
        def pallet(i):
            if i == 20:            # beside where the exit will be, inside its 1.5 m margin only
                w.box(9, 10, 2.0, 2.6, 0, 1, self.W.F.DYNAMIC_OBJECT, "pallet")

        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        session, code, along, _ = self.extract(w, hook=pallet)
        self.assertEqual(code, "EXIT_NOT_CONFIRMED", self.hitch.describeUnpark(session))
        self.assertLess(along, 9.3)                                  # nothing more
        self.assert_ended_braked_and_restored(w)

    def test_an_abort_brakes_and_never_drives_again(self):
        w = self.world()
        w.shed(-9, 6, 4, 4.5)
        session, _, _ = self.start(w)
        self.W.run(w, session, 10)
        self.hitch.abort(w.env, session, "STOPPED")
        self.assertEqual(self.hitch.unparkStep(w.env, session, 50), "STOPPED")
        self.assertFalse(self.drives(w)[-1]["allowed"])
        self.assertNotEqual(w.tractor.forceIsActive, True)


class Lua51UnparkTests(LuaUnparkTests):
    runtime = lua51
