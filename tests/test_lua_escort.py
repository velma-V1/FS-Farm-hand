"""ESCORT in mod/FarmhandHitch.lua (Lua 5.1 and 5.4): a grain cart held under the pipe of a combine
harvesting under GIANTS' helper (FINAL_FARMHAND_PRODUCT_SPEC.md 5, unloading on the move).

On the kinematic stub of tests/test_lua_unpark.py with a steering model added here (a bicycle
tractor and a towed trailer that trails its hitch; NOT GIANTS physics) and a scripted combine
that drives straight at harvesting speed, its pipe out (and discharging) only while the trailer's
middle is within its unloading trigger, as GIANTS' combine strategy does. It proves the control
and the bounded state machine: from behind, onto the slot's line and up beside the combine at
its speed; behind it on its trail while it turns; the cart kept clear of a header wider than
the pipe reach; TRAILER_FULL and COMBINE_STOPPED braked; refused unmoved when it cannot start.
"""
import math
from pathlib import Path
import unittest

from lupa import lua51, lua54

from tests.test_lua_unpark import STUBS

ROOT = Path(__file__).resolve().parents[1]

TRAILER = dict(back=7.0, width=2.6, length=7.0, height=3.0)

# The combine: `ahead` metres ahead of the cart's steering node along the cart's heading and
# `side` to its +x side (it drives the cart's heading), at `kmh`; its pipe's trigger and discharge
# point at (pipeA, pipeL) of its root; o.header: a header that wide ahead of it.
COMBINE = r"""
local W, w, o = ...
local yaw = w.yaw
local s, c = math.sin(yaw), math.cos(yaw)
local x0, z0 = w.steer.x, w.steer.z
local function at(a, l) return x0 + s * a + c * l, z0 + c * a - s * l end
local rx, rz = at(o.ahead or 30, o.side or -6)
local combine = { ownerFarmId = 1, rootNode = { x = rx, y = 0, z = rz, yaw = yaw },
                  size = { width = 3.4, length = 8, height = 3.8, widthOffset = 0, lengthOffset = 0, heightOffset = 0 },
                  movingDirection = 1 }
combine.rootVehicle = combine
w.combine, w.combineKmh, w.combineAi, w.turning = combine, o.kmh or 8, o.ai ~= false, false
w.pipeOut, w.grain, w.flowLps = false, o.grain or 6000, o.flow or 120
local pipeA, pipeL = o.pipeA or -1.0, o.pipeL or 6.5
local function pipeWorld()
    local r = combine.rootNode
    return W.env.localToWorld(r, pipeL, 4, pipeA)
end
local trigger = {}
combine.spec_pipe = { unloadingStates = { [2] = true }, unloadingTriggers = { { node = trigger } } }
function combine:getCurrentPipeState() return w.pipeOut and 2 or 1 end
function combine:getCurrentDischargeNode()
    local x, y, z = pipeWorld()
    return { node = { x = x, y = y, z = z, yaw = combine.rootNode.yaw }, fillUnitIndex = 1 }
end
w.combineCrop = o.combineCrop or 7
function combine:getFillUnitFillType() return w.combineCrop end
function combine:getFillUnitFillLevel() return w.grain end
function combine:getFillUnitCapacity() return o.capacity or 10000 end
function combine:getLastSpeed() return w.combineAi and w.combineKmh or 0 end
function combine:getIsAIActive() return w.combineAi end
function combine:getIsTurnedOn() return w.combineAi end
function combine:getAIFieldWorkerIsTurning() return w.turning end
local header = nil
if o.header then
    header = { ownerFarmId = 1, rootVehicle = combine,
               rootNode = { x = 0, y = 0, z = 0, yaw = yaw },
               size = { width = o.header, length = 2.5, height = 1.5, widthOffset = 0, lengthOffset = 0, heightOffset = 0 } }
end
function combine:getAttachedImplements() return header and { { object = header } } or {} end
local function place()
    local r = combine.rootNode
    local tx, ty, tz = pipeWorld()
    if o.triggerL then tx, ty, tz = W.env.localToWorld(r, o.triggerL, 4, pipeA) end   -- the folded pipe's trigger
    trigger.x, trigger.y, trigger.z, trigger.yaw = tx, ty, tz, r.yaw
    if header then
        header.rootNode.x, header.rootNode.z = W.env.localToWorld(r, 0, 0, 5.3)
        local hx, _, hz = W.env.localToWorld(r, 0, 0, 5.3)
        header.rootNode.x, header.rootNode.z, header.rootNode.yaw = hx, hz, r.yaw
    end
end
W.env = w.env
place()
w.objects[400] = combine
w.names[400] = "combine"
table.insert(w.shapes, { id = 400, group = W.F.VEHICLE, rect = function()
    local r = combine.rootNode
    return { x = r.x, z = r.z, yaw = r.yaw, hx = 1.6, hz = 3.9, y0 = 0.2, y1 = 3.7 }
end })
table.insert(w.env.g_currentMission.vehicleSystem.vehicles, combine)
if header then table.insert(w.env.g_currentMission.vehicleSystem.vehicles, header) end
w.env.FillType = { UNKNOWN = 0 }
w.player.x, w.player.z = w.player.x + 5000, w.player.z

-- The trailer's grain: GIANTS' fill units, filled from the pipe while its middle is in the trigger.
local trailer = w.trailer
trailer.level, trailer.capacity, trailer.crop = o.level or 0, o.capacity or 20000, o.crop or 0
function trailer:getFillUnits() return { {} } end
function trailer:getFillUnitFillLevel() return trailer.level end
function trailer:getFillUnitCapacity() return trailer.capacity end
function trailer:getFillUnitFillType() return trailer.crop end
function trailer:getFillUnitSupportsFillType(_, t) return o.refuses ~= t and w.refuses ~= t end

-- Each frame: the combine on (turning: round to its +x side or the other way at its speed), then
-- the pipe as GIANTS' strategy works it: out while the trailer's middle is in the trigger, grain
-- flowing only then.
w.combineYawRate = 0
function w.tick(dt)
    local r = combine.rootNode
    local kmh = combine:getLastSpeed()
    local ds = kmh / 3.6 * dt / 1000
    r.yaw = r.yaw + (w.combineYawRate or 0) * ds
    r.x, r.z = r.x + ds * math.sin(r.yaw), r.z + ds * math.cos(r.yaw)
    place()
    local mx, _, mz = W.env.localToWorld(trailer.rootNode, 0, 0, 0)
    local lx, _, lz = W.env.worldToLocal(trigger, mx, 0, mz)
    w.pipeOut = math.abs(lx) < trailer.size.width / 2 and math.abs(lz) < trailer.size.length / 2 - 0.5
    if w.pipeOut and w.grain > 0 and trailer.level < trailer.capacity then
        local q = math.min(w.grain, w.flowLps * dt / 1000, trailer.capacity - trailer.level)
        w.grain, trailer.level = w.grain - q, trailer.level + q
        if kmh > 1.5 then w.movedGrain = (w.movedGrain or 0) + q end
    end
end
"""

# A bicycle tractor (its root is its steering node) and a trailer that trails a hitch 2 m behind
# it, 5 m to its own axle: the motion GIANTS' driveToPoint commands, not GIANTS physics.
STEERING = r"""
local W, w = ...
local trailer = w.trailer
function w.env.AIVehicleUtil.driveToPoint(v, dt, acc, allowed, forwards, lx, lz, maxSpeed)
    table.insert(w.drives, { allowed = allowed, forwards = forwards, lx = lx, lz = lz, maxSpeed = maxSpeed, acc = acc })
    table.insert(w.events, allowed and (acc > 0 and "drive" or acc == 0 and "coast" or "brake") or "brake")
    if not allowed or acc < 0 then
        w.speed = math.max(0, w.speed - 12 * dt / 1000)
    elseif acc > 0 then
        w.speed = math.min(maxSpeed, w.speed + 6 * dt / 1000)
    end
    local ds = w.speed / 3.6 * dt / 1000
    local r2 = lx * lx + lz * lz
    local k = r2 > 1e-6 and 2 * lx / r2 or 0
    k = math.max(-1 / 6, math.min(1 / 6, k))
    if not allowed then k = 0 end
    local n = w.steer
    n.yaw = n.yaw + k * ds
    n.x, n.z = n.x + ds * math.sin(n.yaw), n.z + ds * math.cos(n.yaw)
    local hx, hz = n.x - 2 * math.sin(n.yaw), n.z - 2 * math.cos(n.yaw)
    local t = trailer.rootNode
    t.yaw = t.yaw + ds / 5 * math.sin(n.yaw - t.yaw)
    t.x, t.z = hx - 5 * math.sin(t.yaw), hz - 5 * math.cos(t.yaw)
end
"""

RUN = r"""
local Hitch = ...
return function(w, s, maxSteps, hook)
    for i = 1, maxSteps do
        if hook then hook(i) end
        w.tick(50)
        local code = Hitch.escortStep(w.env, s, 50)
        if code ~= "CONTINUE" then return code, i end
    end
    return "CONTINUE", maxSteps
end
"""


class LuaEscortTests(unittest.TestCase):
    runtime = lua54

    def setUp(self):
        self.lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        self.hitch = self.lua.execute((ROOT / "mod/FarmhandHitch.lua").read_text())
        self.W = self.lua.execute(STUBS, self.hitch)
        loader = "load" if self.runtime is lua54 else "loadstring"
        self.load = self.lua.eval(f"function(src, W, w, o) return {loader}(src)(W, w, o) end")
        self.runner = self.lua.execute(RUN, self.hitch)

    def world(self, trailer=None, yaw=0.0, **opts):
        w = self.W.new(self.lua.table(yaw=yaw, trailer=self.lua.table(**(trailer or TRAILER))))
        # The combine's frame turns with the right-handed local frame of the stub: its +x side.
        self.load(COMBINE, self.W, w, self.lua.table(**opts))
        self.load(STEERING, self.W, w, None)
        return w

    def start(self, w):
        out = self.hitch.newEscort(w.env, self.W.args(w, self.lua.table(combine=w.combine)))
        out = out if isinstance(out, tuple) else (out,)
        return out + (None,) * (3 - len(out))

    def frame(self, w, x, z):
        """(along, lateral) of a world point in the combine's frame (Hitch's inFrame)."""
        r = w.combine.rootNode
        fx, fz = math.sin(r.yaw), math.cos(r.yaw)
        dx, dz = x - r.x, z - r.z
        return dx * fx + dz * fz, dx * fz - dz * fx

    def test_from_behind_it_comes_up_beside_the_moving_combine_and_takes_grain_there(self):
        w = self.world()
        s, why, refused = self.start(w)
        self.assertIsNotNone(s, (why, refused and self.hitch.describeEscort(refused)))
        code, _ = self.runner(w, s, 120 * 20, None)                   # two minutes
        self.assertEqual(code, "CONTINUE", self.hitch.describeEscort(s))
        self.assertEqual(s.mode, "alongside", self.hitch.describeEscort(s))
        t = w.trailer.rootNode
        a, l = self.frame(w, t.x, t.z)
        self.assertAlmostEqual(l, s.slotL, delta=0.6, msg=self.hitch.describeEscort(s))
        self.assertAlmostEqual(a, s.slotA, delta=1.5, msg=self.hitch.describeEscort(s))
        self.assertAlmostEqual(w.speed, 8.0, delta=1.0)                # the combine's speed
        self.assertTrue(w.pipeOut)
        self.assertGreater(s.movingL, 3000)                            # into the trailer while it moved
        self.assertEqual(s.standingL, 0)
        self.assertGreater(w.movedGrain, 3000)

    def test_full_it_stops_braked_and_says_so(self):
        w = self.world(capacity=4000)
        s, why, _ = self.start(w)
        self.assertIsNotNone(s, why)
        code, _ = self.runner(w, s, 300 * 20, None)
        self.assertEqual(code, "TRAILER_FULL", self.hitch.describeEscort(s))
        self.assertGreaterEqual(w.trailer.level, 0.98 * 4000)
        self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])
        self.assertNotEqual(w.tractor.forceIsActive, True)
        self.assertEqual(self.hitch.escortStep(w.env, s, 50), "TRAILER_FULL")      # latched

    def test_its_helper_ending_ends_it(self):
        w = self.world()
        s, why, _ = self.start(w)
        self.assertIsNotNone(s, why)

        def hook(i):
            if i == 40 * 20:
                w.combineAi = False
        code, steps = self.runner(w, s, 120 * 20, hook)
        self.assertEqual(code, "COMBINE_STOPPED", self.hitch.describeEscort(s))
        self.assertLess(steps, 40 * 20 + 4 * 20)                      # within its 3 s
        self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])

    def test_while_the_combine_turns_the_cart_drops_back_onto_its_trail(self):
        w = self.world()
        s, why, _ = self.start(w)
        self.assertIsNotNone(s, why)
        self.runner(w, s, 90 * 20, None)
        self.assertEqual(s.mode, "alongside")
        w.turning = True
        w.combineYawRate = -1 / 12.0 if s.slotL > 0 else 1 / 12.0     # round away from the pipe's side
        code, _ = self.runner(w, s, 25 * 20, None)
        self.assertEqual(code, "CONTINUE", self.hitch.describeEscort(s))
        self.assertEqual(s.mode, "follow")
        self.assertFalse(w.pipeOut)
        r, n = w.combine.rootNode, w.steer
        self.assertGreater(math.hypot(r.x - n.x, r.z - n.z), 10.0)     # well behind it
        self.assertEqual(s.brakes.close, 0, self.hitch.describeEscort(s))

    def test_a_header_wider_than_the_pipe_reach_moves_the_cart_out_not_into_it(self):
        # A 9.6 m header 5.3 m ahead of the combine's root, the spout 6.5 m out: the tractor at the
        # slot would stand beside the header; the cart runs farther out, the spout still over the
        # trailer, never against the header.
        w = self.world(header=9.6)
        s, why, _ = self.start(w)
        self.assertIsNotNone(s, why)
        code, _ = self.runner(w, s, 120 * 20, None)
        self.assertEqual(code, "CONTINUE", self.hitch.describeEscort(s))
        self.assertEqual(s.mode, "alongside", self.hitch.describeEscort(s))
        self.assertGreater(s.tL, s.slotL + 0.3)
        self.assertLessEqual(s.tL - s.slotL, TRAILER["width"] / 2 - 0.8 + 1e-9)    # the spout still over it
        self.assertEqual(s.brakes.close, 0, self.hitch.describeEscort(s))
        self.assertTrue(w.pipeOut)
        self.assertGreater(s.movingL, 1000)

    def test_with_no_clear_spot_beside_it_the_cart_waits_behind_and_says_so(self):
        # A 12 m header: no spot with the spout over the trailer keeps the cart clear of it.
        w = self.world(header=12.0)
        s, why, _ = self.start(w)
        self.assertIsNotNone(s, why)
        code, _ = self.runner(w, s, 120 * 20, None)
        self.assertEqual(code, "NO_SAFE_SLOT", self.hitch.describeEscort(s))
        self.assertEqual(s.alongsideMs, 0)
        self.assertEqual(s.brakes.close, 0, self.hitch.describeEscort(s))
        self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])

    def test_the_cover_lets_grain_in_only_while_the_combine_moves_threshing_beside_it(self):
        # Live 2026-11-06, field 91: 1082 l went in while both stood at a headland (the owner: never standing).
        w = self.world()
        covers = self.lua.eval("""function(t) t.spec_cover = { hasCovers = true, state = 1 }; t.sets = {}
            function t:setCoverState(v) self.spec_cover.state = v; self.sets[#self.sets + 1] = v end end""")
        s, why, _ = self.start(w)
        self.assertIsNotNone(s, why)
        covers(s.trailer)
        self.runner(w, s, 120 * 20, None)
        self.assertGreater(s.alongsideMs, 0, self.hitch.describeEscort(s))
        self.assertEqual(s.trailer.spec_cover.state, 1)                  # alongside, moving: open
        w.combineKmh = 0                                                 # it stops at a headland
        self.runner(w, s, 10, None)
        self.assertEqual(s.trailer.spec_cover.state, 0)                  # closed: GIANTS discharges into nothing
        self.hitch.escortRestore(s)
        self.assertEqual(s.trailer.spec_cover.state, 1)                  # as it was found

    def test_it_shadows_the_combine_and_comes_alongside_only_from_the_tank_share_asked(self):
        # The owner, 2026-11-06: the cart starts right after the combine and unloads from 60-70 % (test 10 %).
        w = self.world(grain=500)                                     # 5 % of 10000
        s, why, _ = self.start_with(w, 0.1)
        self.assertIsNotNone(s, why)
        self.runner(w, s, 60 * 20, None)
        self.assertEqual(s.alongsideMs, 0, self.hitch.describeEscort(s))    # it follows, close behind
        w.grain = 1500                                                # 15 %
        self.runner(w, s, 60 * 20, None)
        self.assertGreater(s.alongsideMs, 0, self.hitch.describeEscort(s))

    def start_with(self, w, share):
        args = self.W.args(w, self.lua.table(combine=w.combine))
        args.startShare = share
        out = self.hitch.newEscort(w.env, args)
        out = out if isinstance(out, tuple) else (out,)
        return out + (None,) * (3 - len(out))

    def test_the_folded_pipe_aims_the_cart_at_where_the_pipe_was_seen_out(self):
        # Live 2026-11-06, field 91: the folded pipe's trigger 4.55 m out, inside the MF 8570's 6.8 m header
        # and its clearance: NO_SAFE_SLOT twice. The pipe-out point the stationary unload saw is used.
        w = self.world(header=6.8, triggerL=4.5)
        s, why, _ = self.start(w)
        self.assertIsNotNone(s, why)
        code, _ = self.runner(w, s, 120 * 20, lambda *_: s.alongsideMs > 0)
        # Nothing learned yet: the trailer goes under the folded pipe's trigger, beside the header, clear of it.
        self.assertGreater(s.alongsideMs, 0, self.hitch.describeEscort(s))
        self.assertEqual(s.brakes.close, 0, self.hitch.describeEscort(s))
        w = self.world(header=6.8, triggerL=4.5)
        s, why, _ = self.start(w)
        self.assertIsNotNone(s, why)
        w.pipeOut = False
        self.hitch.pipeOut[w.combine] = self.lua.table(a=-1.0, l=6.5)           # seen out at a stationary unload
        code, _ = self.runner(w, s, 120 * 20, lambda *_: s.alongsideMs > 3000)
        self.assertGreater(s.alongsideMs, 3000, self.hitch.describeEscort(s))
        self.assertEqual(s.slotSource, "learned")

    def test_once_the_combine_holds_a_crop_the_trailer_cannot_take_it_stops_and_says_so(self):
        # Live, save 1: the MF 8570 held canola from field 5, sent to the oats of field 90: the
        # canola goes into the cart on the move, then the oats need another trailer.
        w = self.world()
        s, why, _ = self.start(w)
        self.assertIsNotNone(s, why)
        self.runner(w, s, 90 * 20, None)
        self.assertEqual(s.mode, "alongside")
        w.combineCrop, w.refuses, w.grain = 9, 9, 500   # oats coming in
        code, _ = self.runner(w, s, 20, None)
        self.assertEqual(code, "TRAILER_WRONG_CROP", self.hitch.describeEscort(s))
        self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])

    def test_refused_unmoved_when_it_cannot_start(self):
        for opts, reason in ((dict(ai=False), "COMBINE_NOT_HARVESTING"),
                             (dict(ahead=150), "ESCORT_TOO_FAR"),
                             (dict(ahead=-20), "ESCORT_AHEAD_OF_COMBINE"),
                             (dict(level=20000), "TRAILER_FULL"),
                             (dict(refuses=7), "TRAILER_WRONG_CROP"),
                             (dict(crop=3, level=100), "TRAILER_WRONG_CROP")):
            w = self.world(**opts)
            s, why, _ = self.start(w)
            self.assertIsNone(s, opts)
            self.assertEqual(why, reason, opts)
            self.assertEqual(len(w.drives), 0)

    def test_stopped_by_the_player_taking_the_wheel(self):
        w = self.world()
        w.entered = True
        s, why, _ = self.start(w)
        self.assertIsNotNone(s, why)

        def hook(i):
            if i == 200:
                w.takeOver()
        code, _ = self.runner(w, s, 600, hook)
        self.assertEqual(code, "DRIVER_TOOK_OVER")


class LuaEscortTests51(LuaEscortTests):
    runtime = lua51


if __name__ == "__main__":
    unittest.main()
