"""LOAD / UNLOAD in mod/FarmhandHitch.lua (Lua 5.1 and 5.4; AGENTS.md exception 5) on the
kinematic stub of tests/test_lua_unpark.py (NOT GIANTS physics: the stub tractor never
climbs, boxes stand in for collision shapes). It proves the bounded state machine: the
tractor alone, straight along the transport trailer's axis, only with its ramps down
(Foldable fold time 1) and its belts open, at most 3 km/h and the ramps plus the deck,
stopping with its body centred in the deck box (the tension belts' span), the swept
volume clear of everything but the tractor and the trailer (the truck towing it is not
excluded), every guard while moving, UNLOAD straight back off the ramps, and the
vehicle-control state restored on every exit.

Live motive: the user's tractor transport (2026-10-21): the Deutz-Fahr Series 9 onto the
1124 Deckover behind the GMC 3500HD.
"""
from pathlib import Path
import unittest

from lupa import lua51, lua54

from tests.test_lua_unpark import STUBS

ROOT = Path(__file__).resolve().parents[1]

# The transport in the tractor's start frame (a ahead, l to its local +x side): a 10 m
# trailer whose root is 9.1 m ahead of the tractor's steering node, four belts 2 m apart
# 1.0 m above the ground (deck box -5..3 m of the trailer's frame), its deck a vehicle
# shape up to 1.0 m; the truck towing it ahead of the deck.
TRANSPORT = r"""
local Hitch, W, w, o = ...
local F = W.F
local yaw = w.yaw + (o.turn or 0)
local s, c = math.sin(w.yaw), math.cos(w.yaw)
local x0, z0 = w.steer.x, w.steer.z
local function at(a, l)
    return x0 + s * a + c * l, z0 + c * a - s * l
end
local function node(a, l, y, nodeYaw) local x, z = at(a, l); return { x = x, y = y or 0, z = z, yaw = nodeYaw or yaw } end
-- A point of the trailer's frame (its root, its heading) in the world.
local tx, tz = at(o.ahead or 9.1, o.side or 0)
local ts, tc = math.sin(yaw), math.cos(yaw)
local function onTrailer(a, l, y)
    return { x = tx + ts * a + tc * l, y = y or 0, z = tz + tc * a - ts * l, yaw = yaw }
end
local truck = { ownerFarmId = 1, rootNode = onTrailer(9.0, 0), size = { width = 2.4, length = 6.5, height = 2.0,
                widthOffset = 0, lengthOffset = 0, heightOffset = 0 } }
truck.rootVehicle = truck
local trailer = { ownerFarmId = 1, rootNode = onTrailer(0, 0), rootVehicle = truck,
                  size = { width = 2.6, length = 10.0, height = 1.2, widthOffset = 0, lengthOffset = 0, heightOffset = 0 } }
w.speedTrailer = 0
function trailer:getLastSpeed() return w.speedTrailer end
w.foldTime, w.foldMoving = o.foldTime or 1, 0
trailer.spec_foldable = { foldingParts = { { animationName = "folding" } } }
setmetatable(trailer.spec_foldable, { __index = function(_, k) if k == "foldMoveDirection" then return w.foldMoving end end })
function trailer:getFoldAnimTime() return w.foldTime end
local belts = {}
for _, a in ipairs({ -4, -2, 0, 2 }) do
    belts[#belts + 1] = { startNode = onTrailer(a, -1.3, 1.0), endNode = onTrailer(a, 1.3, 1.0) }
end
trailer.spec_tensionBelts = { hasTensionBelts = true, areAllBeltsFastened = o.fastened or false, sortedBelts = belts }
truck.getAttachedImplements = function() return { { object = trailer } } end
w.truck, w.transportTrailer = truck, trailer
-- Collision: the deck (trailer, up to 1.0 m) and the truck's body.
local function shape(id, v, a0, a1, half, y0, y1, frame)
    w.objects[id] = v
    w.names[id] = "body" .. id
    table.insert(w.shapes, { id = id, group = F.VEHICLE, rect = function()
        local r = frame()
        local ca = (a0 + a1) / 2
        return { x = r.x + math.sin(r.yaw) * ca, z = r.z + math.cos(r.yaw) * ca, yaw = r.yaw, hx = half,
                 hz = (a1 - a0) / 2, y0 = y0, y1 = y1 }
    end })
end
shape(301, trailer, -4.9, 4.9, 1.25, 0.2, 1.0, function() return trailer.rootNode end)
shape(302, truck, -3.15, 3.15, 1.1, 0.2, 1.9, function() return truck.rootNode end)
local vehicles = w.env.g_currentMission.vehicleSystem.vehicles
table.insert(vehicles, truck)
table.insert(vehicles, trailer)
"""

# A plow on the tractor's three-point joint (GIANTS AttacherJoints.JOINTTYPE_IMPLEMENT), lifted,
# folded (Foldable getIsUnfolded false); o.joint, o.lowered, o.unfolded spoil it.
MOUNTED = r"""
local w, o = ...
w.env.AttacherJoints = { JOINTTYPE_IMPLEMENT = 3, JOINTTYPE_TRAILER = 5 }
w.attached[1].jointDescIndex = 1
function w.tractor:getAttacherJointByJointDescIndex(i)
    if i == 1 then return { jointType = o.joint or 3 } end
end
local plow = w.trailer
plow.lowered, plow.unfolded = o.lowered or false, o.unfolded or false
function plow:getIsLowered() return self.lowered end
plow.spec_foldable = { foldingParts = { {} } }
function plow:getIsUnfolded() return self.unfolded end
"""

PLOW = dict(back=3.6, width=1.7, length=2.4, height=1.35)

RUN = r"""
local Hitch = ...
return function(w, s, maxSteps, hook)
    for i = 1, maxSteps do
        if hook then hook(i) end
        local code = Hitch.loadStep(w.env, s, 50)
        if code ~= "CONTINUE" then return code, i end
    end
    return "CONTINUE", maxSteps
end
"""


class LuaLoadTests(unittest.TestCase):
    runtime = lua54

    def setUp(self):
        self.lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        self.hitch = self.lua.execute((ROOT / "mod/FarmhandHitch.lua").read_text())
        self.W = self.lua.execute(STUBS, self.hitch)
        self.transport = self.lua.eval("function(src, H, W, w, o) return load(src)(H, W, w, o) end") \
            if self.runtime is lua54 else self.lua.eval("function(src, H, W, w, o) return loadstring(src)(H, W, w, o) end")
        self.runner = self.lua.execute(RUN, self.hitch)

    def world(self, size=None, plow=None, **opts):
        o = {"size": self.lua.table(**size)} if size else {}
        if plow:
            o["trailer"] = self.lua.table(**plow)       # the stub's towed unit stands in for a mounted plow
        w = self.W.new(self.lua.table(**o))
        self.transport(TRANSPORT, self.hitch, self.W, w, self.lua.table(**opts))
        if plow:
            self.lua.execute(MOUNTED, w, self.lua.table(**{k: v for k, v in plow.items() if k in ("joint", "lowered", "unfolded")}))
        return w

    def start(self, w, direction=1, **overrides):
        args = self.W.args(w, self.lua.table(trailer=w.transportTrailer, direction=direction, **overrides))
        out = self.hitch.newLoad(w.env, args)
        out = out if isinstance(out, tuple) else (out,)
        return out + (None,) * (3 - len(out))

    def drive(self, w, session, steps=3000, hook=None):
        return self.runner(w, session, steps, hook)

    def drives(self, w):
        return [dict(d) for d in w.drives.values()]

    def moving(self, w):
        return [d for d in self.drives(w) if d["allowed"]]

    def assert_ended_braked_and_restored(self, w):
        events = list(w.events.values())
        self.assertEqual(events[-2:], ["brake", "cruise:0"])
        self.assertNotEqual(w.tractor.forceIsActive, True)

    def assert_refused(self, w, reason, direction=1, **overrides):
        session, why, refused = self.start(w, direction, **overrides)
        self.assertIsNone(session)
        self.assertEqual(why, reason, self.hitch.describeLoad(refused))
        self.assertEqual(self.drives(w), [])

    def along(self, w, x0, z0):
        return self.W.travel(w, x0, z0)

    # ------------------------------------------------------------------ LOAD
    def test_drives_straight_up_onto_the_deck_and_stops_centred_on_it(self):
        w = self.world()
        x0, z0 = w.steer.x, w.steer.z
        session, why, _ = self.start(w)
        self.assertIsNotNone(session, why)
        self.assertAlmostEqual(session.travelM, 8.1, places=6)         # its centre to the deck's (-1)
        code, _ = self.drive(w, session)
        self.assertEqual(code, "LOADED", self.hitch.describeLoad(session))
        along, lateral = self.along(w, x0, z0)
        self.assertGreaterEqual(along, 8.1)
        self.assertLess(along, 8.3)
        self.assertAlmostEqual(lateral, 0.0, places=6)
        moving = self.moving(w)
        self.assertTrue(moving and all(d["forwards"] and d["maxSpeed"] == 3 for d in moving))
        self.assert_ended_braked_and_restored(w)
        self.assertEqual(self.hitch.loadStep(w.env, session, 50), "LOADED")         # latched: brakes only
        self.assertFalse(self.drives(w)[-1]["allowed"])
        self.assertEqual(set(w.returns.values()), {True})

    def test_limits_are_hard_capped(self):
        for overrides in ({"maxSpeedKmh": 3.5}, {"maxSpeedKmh": 0.5}, {"timeoutMs": 61000}, {"isReserved": False}):
            with self.subTest(**overrides):
                self.assert_refused(self.world(), "INVALID_LIMITS", **overrides)
        self.assert_refused(self.world(), "INVALID_LIMITS", direction=0)
        self.assertEqual(self.hitch.LOAD_LIMITS.maxSpeedKmh, 3)

    def test_mirrors_the_companion(self):
        import math
        from companion import hitch as companion_hitch
        D = self.hitch.LOAD
        self.assertEqual(D.rampM, companion_hitch.RAMP_M)
        self.assertEqual(D.lateralStartM, companion_hitch.LATERAL_START_M)
        self.assertAlmostEqual(math.radians(D.headingStartDeg), companion_hitch.HEADING_START_RAD)
        self.assertTrue(all(0 < g < D.rampM for g in companion_hitch.FRONT_GAPS_M))

    def test_every_precondition_is_checked_before_any_motion(self):
        cases = [
            ("LOAD_RAMPS_NOT_DOWN", dict(foldTime=0), None),
            ("LOAD_BELTS_FASTENED", dict(fastened=True), None),
            ("LOAD_NOT_LINED_UP", dict(side=0.8), None),                 # beside the axis
            ("LOAD_NOT_LINED_UP", dict(turn=0.15), None),                # turned 8.6 deg off it
            ("LOAD_NOT_BEHIND_RAMPS", dict(ahead=12.0), None),           # 4.4 m behind the trailer
            ("LOAD_NOT_BEHIND_RAMPS", dict(ahead=7.0), None),            # already on the ramps
            ("LOAD_DOES_NOT_FIT", dict(), dict(width=2.6, length=8.6, height=3.0)),
            ("LOAD_TRACTOR_NOT_ALONE", dict(), "attach"),
            ("SEATED_DURING_LOAD", dict(), "seated"),
            ("NOT_STOPPED", dict(), "rolling"),
            ("TRANSPORT_NOT_STOPPED", dict(), "trailer rolling"),
            ("LOAD_QUERY_UNVERIFIED", dict(), "no terrain"),
            ("LOAD_QUERY_UNVERIFIED", dict(), "count skew"),       # an untrustworthy count
        ]
        for reason, opts, spoil in cases:
            with self.subTest(reason=reason, opts=opts, spoil=spoil):
                w = self.world(size=spoil if isinstance(spoil, dict) else None, **opts)
                if spoil == "attach":
                    w.attached = self.lua.table(self.lua.table(object=w.truck))
                elif spoil == "seated":
                    w.entered = True
                elif spoil == "rolling":
                    w.speed = 2
                elif spoil == "trailer rolling":
                    w.speedTrailer = 2
                elif spoil == "no terrain":
                    w.noTerrain = True
                elif spoil == "count skew":
                    w.countSkew = 1
                self.assert_refused(w, reason)

    def test_anything_on_the_deck_or_the_path_refuses_it_but_the_trailer_itself_never_does(self):
        w = self.world()
        w.box(4.0, 5.0, -1.0, 1.0, 1.0, 2.0, self.W.F.DYNAMIC_OBJECT, "pallet")    # on the deck
        session, why, refused = self.start(w)
        self.assertEqual(why, "LOAD_PATH_BLOCKED")
        self.assertIn("pallet", refused.pathHits)
        self.assertEqual(self.drives(w), [])

    def test_the_truck_towing_the_trailer_is_not_excluded(self):
        # The truck's body reaching back over the deck's front: an obstacle like any other.
        w = self.world()
        w.truck.rootNode.x = w.transportTrailer.rootNode.x + 4.6 * (w.truck.rootNode.x - w.transportTrailer.rootNode.x) / 9.0
        w.truck.rootNode.z = w.transportTrailer.rootNode.z + 4.6 * (w.truck.rootNode.z - w.transportTrailer.rootNode.z) / 9.0
        session, why, refused = self.start(w)
        self.assertEqual(why, "LOAD_PATH_BLOCKED", self.hitch.describeLoad(refused))
        self.assertIn("body302", refused.pathHits)

    def test_every_guard_while_moving_stops_it_braked_and_restored(self):
        cases = [
            (lambda w, i: i == 40 and setattr(w.truck.rootNode, "x", w.truck.rootNode.x + 0.5), "TRANSPORT_MOVED"),
            (lambda w, i: i == 40 and setattr(w, "foldMoving", -1), "RAMPS_MOVED"),
            (lambda w, i: i == 40 and setattr(w, "foldTime", 0.6), "RAMPS_MOVED"),
            (lambda w, i: i == 40 and w.takeOver(), "DRIVER_TOOK_OVER"),
            (lambda w, i: i == 40 and setattr(w, "entered", True), "SEATED_DURING_LOAD"),
            (lambda w, i: i == 40 and setattr(w, "reserved", False), "RESERVATION_LOST"),
            (lambda w, i: i == 40 and setattr(w, "ai", True), "AI_ACTIVE"),
            (lambda w, i: i == 40 and setattr(w, "stuck", True), "STALLED"),
            (lambda w, i: i == 40 and setattr(w, "drift", 0.02), "DIVERGED"),
            (lambda w, i: i == 40 and (setattr(w.player, "x", w.steer.x + 2.0) or setattr(w.player, "z", w.steer.z)),
             "PERSON_TOO_CLOSE"),
            (lambda w, i: i == 40 and w.box(4.0, 5.0, -1.0, 1.0, 1.0, 2.0, self.W.F.DYNAMIC_OBJECT, "dropped"),
             "OBSTACLE_AHEAD"),
        ]
        for hook, reason in cases:
            with self.subTest(reason=reason):
                w = self.world()
                session, why, _ = self.start(w)
                self.assertIsNotNone(session, why)
                code, _ = self.drive(w, session, hook=lambda i, w=w, hook=hook: hook(w, i))
                self.assertEqual(code, reason, self.hitch.describeLoad(session))
                self.assert_ended_braked_and_restored(w)

    def test_timeout(self):
        w = self.world()
        session, _, _ = self.start(w, timeoutMs=3000)
        code, steps = self.drive(w, session)
        self.assertEqual(code, "TIMEOUT")
        self.assertAlmostEqual(steps * 50, 3000, delta=100)
        self.assert_ended_braked_and_restored(w)

    # -------------------------------------------- a mounted implement along (2026-10-21)
    def test_a_lifted_folded_mounted_plow_comes_along_and_the_rig_stops_forward(self):
        w = self.world(plow=PLOW)
        x0, z0 = w.steer.x, w.steer.z
        session, why, refused = self.start(w)
        self.assertIsNotNone(session, self.hitch.describeLoad(refused) + " " + str(why))
        # The rig's front (2.6 m ahead of the node, at -6.5 of the trailer frame) to the belt
        # area's front (3) less 0.3 m: 9.2 m.
        self.assertAlmostEqual(session.travelM, 9.2, places=6)
        code, _ = self.drive(w, session)
        self.assertEqual(code, "LOADED", self.hitch.describeLoad(session))
        along, _ = self.along(w, x0, z0)
        self.assertGreaterEqual(along, 9.2)
        self.assert_ended_braked_and_restored(w)

    def test_a_mounted_implement_comes_along_only_lifted_folded_on_three_points_and_fitting(self):
        for plow, reason in ((dict(PLOW, lowered=True), "LOAD_IMPLEMENT_LOWERED"),
                             (dict(PLOW, unfolded=True), "LOAD_IMPLEMENT_UNFOLDED"),
                             (dict(PLOW, joint=5), "LOAD_IMPLEMENT_NOT_MOUNTED"),
                             (dict(PLOW, back=4.2), "LOAD_DOES_NOT_FIT")):        # its rear off the deck
            with self.subTest(reason=reason):
                self.assert_refused(self.world(plow=plow), reason)

    def test_the_rig_moves_together_or_stops(self):
        w = self.world(plow=PLOW)
        session, _, _ = self.start(w)
        code, _ = self.drive(w, session, hook=lambda i: i == 40 and setattr(w.trailer, "lowered", True))
        self.assertEqual(code, "IMPLEMENT_CHANGED")
        self.assert_ended_braked_and_restored(w)

    def test_unload_backs_the_whole_rig_off(self):
        w = self.world(plow=PLOW, ahead=0.0)          # on the deck: tractor [-2.6, 2.6], plow [-4.8, -2.4]
        session, why, refused = self.start(w, direction=-1)
        self.assertIsNotNone(session, self.hitch.describeLoad(refused) + " " + str(why))
        self.assertEqual(self.drive(w, session)[0], "UNLOADED")

    # ---------------------------------------------------------------- UNLOAD
    def test_unload_reverses_straight_off_the_ramps(self):
        w = self.world(ahead=1.0)              # the tractor's centre 1 m behind the trailer's root: on the deck
        x0, z0 = w.steer.x, w.steer.z
        self.assert_refused(self.world(ahead=1.0), "LOAD_NOT_BEHIND_RAMPS")          # already on: no LOAD
        session, why, refused = self.start(w, direction=-1)
        self.assertIsNotNone(session, self.hitch.describeLoad(refused) + " " + str(why))
        code, _ = self.drive(w, session)
        self.assertEqual(code, "UNLOADED", self.hitch.describeLoad(session))
        along, lateral = self.along(w, x0, z0)
        # Its front (2.6 m ahead of its node, at 1.6 of the trailer frame at the start) to
        # 3 m behind the trailer's rear (-5): 9.6 m back.
        self.assertLessEqual(along, -9.6)
        self.assertGreater(along, -9.8)
        moving = self.moving(w)
        self.assertTrue(moving and not any(d["forwards"] for d in moving))
        self.assert_ended_braked_and_restored(w)

    def test_unload_refusals(self):
        self.assert_refused(self.world(), "UNLOAD_NOT_ON_DECK", direction=-1)       # behind the ramps
        self.assert_refused(self.world(ahead=1.0, fastened=True), "LOAD_BELTS_FASTENED", direction=-1)
        self.assert_refused(self.world(ahead=1.0, foldTime=0), "LOAD_RAMPS_NOT_DOWN", direction=-1)


class Lua51LoadTests(LuaLoadTests):
    runtime = lua51


if __name__ == "__main__":
    unittest.main()
