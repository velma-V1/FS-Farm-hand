"""LOCAL_MANEUVER in mod/FarmhandHitch.lua (Lua 5.1 and 5.4) on a kinematic stub.

The stub is NOT GIANTS physics: a bicycle model (rear-axle steering node, a
minimum turning radius, a steering-rate limit) behind a driveToPoint that steers
on the circle through the local target, as GIANTS' driveToPoint 31-45 does. It
proves the bounded state machine and that the controller's own geometry closes
on the two live failures; never that FS25 vehicles follow it.

Live geometry (8ffafcc, tractor 170 / wagon 173, joints 4/1), in the wagon frame:
  A: gap 12.87 m, ahead 3.44 m, lateral -12.40 m, heading 85.9 deg
  B: gap 18.80 m, ahead 5.52 m, lateral -17.97 m, heading 162.9 deg
"""
import math
from pathlib import Path
import random
import unittest

from lupa import lua51, lua54

from companion import hitch as companion_hitch

ROOT = Path(__file__).resolve().parents[1]

STUBS = r"""
local Hitch = ...
local W = {}

local function node(x, z, yaw, y) return { x = x, y = y or 0, z = z, yaw = yaw or 0 } end

-- Wagon joint (eye) at (ex, ez) facing wagonYaw; the tractor's planned joint at
-- (ahead, lateral) in the wagon frame (lateral = the wagon's local +x side), the
-- tractor turned `heading` (rad) off the wagon. The steering node is `back` metres
-- ahead of the joint along the tractor.
function W.new(o)
    local w = { drives = {}, events = {}, cruise = {}, notes = {} }
    local env = { NetworkUtil = { getObjectId = function() return 11 end } }
    w.env = env
    local wy = o.wagonYaw or 0.4
    local fx, fz = math.sin(wy), math.cos(wy)
    local nx, nz = fz, -fx
    local ex, ez = o.ex or 370.14, o.ez or -861.21
    w.eye = node(ex, ez, wy)
    w.wagonRoot = node(ex - 4 * fx, ez - 4 * fz, wy)
    w.back = o.back or 2.0
    local jx = ex + o.ahead * fx + o.lateral * nx
    local jz = ez + o.ahead * fz + o.lateral * nz
    local ty = wy + o.heading
    w.steer = node(jx + w.back * math.sin(ty), jz + w.back * math.cos(ty), ty)
    w.joint = node(0, 0, ty)
    w.minRadius = o.minRadius or 5.5
    w.steerRate = o.steerRate or 0.25          -- curvature change per second (1/m/s)
    w.kappa = 0
    w.speed = 0
    w.entered, w.ai, w.reserved, w.motor = false, false, true, true
    function w.sync()
        w.joint.x = w.steer.x - w.back * math.sin(w.steer.yaw)
        w.joint.z = w.steer.z - w.back * math.cos(w.steer.yaw)
        w.joint.yaw = w.steer.yaw
    end
    w.sync()

    local tractor = { ownerFarmId = 1, spec_attacherJoints = {}, maxTurningRadius = o.radius }
    if o.radius == "none" then tractor.maxTurningRadius = nil end
    if o.aiRadius then function tractor:getAIMinTurningRadius() return o.aiRadius end end
    local wagon = { ownerFarmId = 1, rootNode = w.wagonRoot }
    local tractorJoint = { jointType = 3, jointIndex = 0, jointTransform = w.joint }
    local wagonJoint = { jointType = 3, node = w.eye }
    w.tractor, w.wagon = tractor, wagon
    tractor.rootVehicle = tractor
    function tractor:getAttacherJoints() return { [4] = tractorJoint } end
    function tractor:getAttacherJointByJointDescIndex(i) return ({ [4] = tractorJoint })[i] end
    function tractor:getIsEntered() return w.entered end
    function tractor:registerPlayerVehicleControlAllowedFunction(_, f) w.controlFunc = f end
    w.takeOver = function() return w.controlFunc(tractor) end
    function tractor:getIsAIActive() return w.ai end
    function tractor:setCruiseControlState(s) table.insert(w.cruise, s); table.insert(w.events, "cruise:" .. s) end
    function tractor:getIsMotorStarted() return w.motor end
    function tractor:getCanMotorRun() return true end
    function tractor:startMotor() w.motor = true end
    function tractor:raiseActive() end
    function tractor:getAISteeringNode() return w.steer end
    function tractor:getAIReverserNode() return nil end
    function tractor:getAIRootNode() return w.steer end
    function tractor:getLastSpeed() return w.speed end
    function tractor:getActiveFarm() return 1 end
    -- GIANTS' steering state as driveToPoint and Wheels use it (AIVehicleUtil 27-97,
    -- Wheels 644, 1950): rotatedTime moves towards getSteeringRotTimeByCurvature at
    -- getAISteeringSpeed per ms; the wheels' physics.steeringAngle follows the turn.
    -- o.bareSteering: none of it readable.
    if not o.bareSteering then
        tractor.rotatedTime, tractor.minRotTime, tractor.maxRotTime = 0, -1, 1
        tractor.wheelSteeringDuration, tractor.autoRotateBackSpeed = 1000, 1
        function tractor:getSteeringRotTimeByCurvature(c) return math.max(-1, math.min(1, -c * w.minRadius)) end
        function tractor:getAISteeringSpeed() return 0.001 end
        function tractor:getReverserDirection() return 1 end
        tractor.spec_wheels = { wheels = { { physics = { rotSpeed = 1, steeringAngle = 0 } },
                                           { physics = { rotSpeed = 0, steeringAngle = 0 } } } }
        tractor.isServer, tractor.isActive = true, true
        function tractor:getIsControlled() return false end
    end
    function wagon:getInputAttacherJoints() return { [1] = wagonJoint } end
    function wagon:getAttacherVehicle() return nil end
    env.Drivable = { CRUISECONTROL_STATE_OFF = 0 }
    env.getWorldTranslation = function(n)
        if n == w.brokenNode then error("engine error") end
        return n.x, n.y, n.z
    end
    env.localDirectionToWorld = function(n, x, y, z) return math.sin(n.yaw), 0, math.cos(n.yaw) end
    env.worldToLocal = function(n, x, y, z)
        local dx, dz = x - n.x, z - n.z
        return dx * math.cos(n.yaw) - dz * math.sin(n.yaw), y - n.y, dx * math.sin(n.yaw) + dz * math.cos(n.yaw)
    end
    -- The candidate rule stub (NOT GIANTS'): joints within the given distance.
    env.AttacherJoints = { MAX_ATTACH_DISTANCE_SQ = 0.49, MAX_ATTACH_ANGLE = 0.34 }
    function env.AttacherJoints.updateVehiclesInAttachRange(v, maxSq)
        local dx, dz = w.joint.x - w.eye.x, w.joint.z - w.eye.z
        tractor.spec_attacherJoints.attachableInfo = nil
        if dx * dx + dz * dz < maxSq then
            tractor.spec_attacherJoints.attachableInfo = { attacherVehicle = tractor, attachable = wagon,
                attacherVehicleJointDescIndex = 4, attachableJointDescIndex = 1 }
        end
    end
    -- Bicycle model: the circle through the local target, as driveToPoint; local +x
    -- turns the yaw up. Reverse runs the same circle backwards.
    env.AIVehicleUtil = {}
    function env.AIVehicleUtil.driveToPoint(v, dt, acc, allowed, forwards, lx, lz, maxSpeed)
        table.insert(w.drives, { allowed = allowed, forwards = forwards, lx = lx, lz = lz, maxSpeed = maxSpeed })
        table.insert(w.events, allowed and "drive" or "brake")
        if not allowed or acc <= 0 or w.stuck then w.speed = 0; return end
        local want = 2 * lx / math.max(lx * lx + lz * lz, 1e-6)
        if v.rotatedTime ~= nil then
            local target = v:getSteeringRotTimeByCurvature(want)
            local rate = dt * v:getAISteeringSpeed()
            v.rotatedTime = v.rotatedTime + math.max(-rate, math.min(rate, target - v.rotatedTime))
        end
        -- GIANTS turns the wheels only of an active vehicle (WheelPhysics:serverUpdate
        -- 799-819): unentered and without an AI job that needs forceIsActive.
        if w.noSteer or v.forceIsActive ~= true then want = 0 end
        local cap = 1 / w.minRadius
        want = math.max(-cap, math.min(cap, want))
        local step = w.steerRate * dt / 1000
        w.kappa = w.kappa + math.max(-step, math.min(step, want - w.kappa))
        local ds = maxSpeed / 3.6 * dt / 1000
        local sign = forwards and 1 or -1
        w.steer.x = w.steer.x + sign * ds * math.sin(w.steer.yaw)
        w.steer.z = w.steer.z + sign * ds * math.cos(w.steer.yaw)
        w.steer.yaw = w.steer.yaw + sign * ds * w.kappa
        if v.spec_wheels then v.spec_wheels.wheels[1].physics.steeringAngle = math.atan(w.kappa * 2.8) end
        w.sync()
        w.speed = sign * maxSpeed
    end
    -- GIANTS' vehicle list (the pair, anything attached to them, and w.others) and
    -- the local player, standing 60 m off unless moved.
    w.others = {}
    w.player = node(ex + 60, ez, 0)
    local vehicles = { tractor, wagon }        -- a plain table, as in Lua 5.1
    env.g_currentMission = { vehicleSystem = { vehicles = vehicles } }
    tractor.rootNode = w.steer
    env.g_localPlayer = { getPosition = function()
        if w.playerUnknown then return nil end
        return w.player.x, 0, w.player.z
    end }
    function w.addVehicle(dx, dz, attachedTo)
        local v = { rootNode = node(ex + dx, ez + dz, 0), rootVehicle = attachedTo }
        table.insert(w.others, v)
        table.insert(vehicles, v)
        return v
    end
    w.wagonNode = w.wagonRoot
    Hitch.onNote = function(line) table.insert(w.notes, line) end
    w.steers = {}
    Hitch.onSteer = function(line) table.insert(w.steers, line) end
    return w
end

function W.args(w, overrides)
    local a = { tractor = w.tractor, wagon = w.wagon, tractorJoint = 4, wagonJoint = 1, farmId = 1,
                isReserved = function() return w.reserved end }
    for k, v in pairs(overrides or {}) do a[k] = v end
    return a
end

-- Run until terminal; `hook(i)` mutates the world. Track the envelope as it runs.
function W.run(w, s, dt, maxSteps, hook)
    w.maxRadius, w.inBody = 0, false
    for i = 1, maxSteps do
        if hook then hook(i) end
        if w.resetSteer and w.tractor.rotatedTime then w.tractor.rotatedTime = 0 end   -- another writer
        local code = Hitch.maneuverStep(w.env, s, dt)
        local f = s.dirX and { s.dirX, s.dirZ } or { 0, 1 }
        for _, n in ipairs({ w.steer, w.joint }) do
            local dx, dz = n.x - w.eye.x, n.z - w.eye.z
            w.maxRadius = math.max(w.maxRadius, math.sqrt(dx * dx + dz * dz))
            local ahead, lat = dx * f[1] + dz * f[2], dx * f[2] - dz * f[1]
            if ahead < 0 and ahead > -s.bodyLength and math.abs(lat) < 3 then w.inBody = true end
        end
        if code ~= "CONTINUE" then return code, i end
    end
    return "CONTINUE", maxSteps
end

-- The planned joint against the wagon joint in the wagon frame (as companion/hitch.py).
function W.error(w)
    local fx, fz = math.sin(w.eye.yaw), math.cos(w.eye.yaw)
    local dx, dz = w.joint.x - w.eye.x, w.joint.z - w.eye.z
    local h = (w.steer.yaw - w.eye.yaw + math.pi) % (2 * math.pi) - math.pi
    return math.sqrt(dx * dx + dz * dz), dx * fx + dz * fz, dx * fz - dz * fx, h
end

-- The bounded reverse from wherever the maneuver left the tractor.
function W.reverse(w)
    w.speed = 0
    local s, why = Hitch.newAlign(w.env, W.args(w, { maxDistanceM = 10 }))
    if s == nil then return why end
    for _ = 1, 4000 do
        local code = Hitch.alignStep(w.env, s, 50)
        if code ~= "CONTINUE" then return code end
    end
    return "CONTINUE"
end

return W
"""

CASE_A = dict(ahead=3.44, lateral=-12.40, heading=math.radians(85.9))
CASE_B = dict(ahead=5.52, lateral=-17.97, heading=math.radians(162.9))


class LuaManeuverTests(unittest.TestCase):
    runtime = lua54

    def setUp(self):
        self.lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        self.hitch = self.lua.execute((ROOT / "mod/FarmhandHitch.lua").read_text())
        self.W = self.lua.execute(STUBS, self.hitch)

    def world(self, **opts):
        opts.setdefault("radius", 5.5)
        return self.W.new(self.lua.table(**opts))

    def start(self, w, **overrides):
        return self.hitch.newManeuver(w.env, self.W.args(w, self.lua.table(**overrides)))

    def drives(self, w):
        return [dict(d) for d in w.drives.values()]

    def notes(self, w):
        return list(w.notes.values())

    def run_to_end(self, w, session, steps=4000, hook=None):
        return self.W.run(w, session, 50, steps, hook)

    def assert_lined_up(self, w, tight=True):
        gap, ahead, lateral, heading = self.W.error(w)
        # Within the bounded reverse's envelope, as companion/hitch.py checks it.
        self.assertLessEqual(gap, companion_hitch.ALIGN_REACH_M)
        self.assertGreater(ahead, 0)
        self.assertLessEqual(abs(lateral), companion_hitch.ALIGN_MAX_LATERAL_M)
        self.assertLessEqual(abs(heading), companion_hitch.ALIGN_MAX_HEADING_RAD)
        if not tight:            # ended by the curve's end inside the reverse's envelope
            return
        # And tighter: the maneuver's own line-up tolerance.
        self.assertLessEqual(abs(lateral), self.hitch.LOCAL.doneLateralM + 0.05)
        self.assertLessEqual(abs(math.degrees(heading)), self.hitch.LOCAL.doneHeadingDeg + 0.5)

    def assert_braked_last(self, w):
        events = list(w.events.values())
        self.assertEqual(events[-2:], ["brake", "cruise:0"])

    # ------------------------------------------------------- live geometry
    def test_case_a_lines_up_forward_then_reverses_onto_the_exact_candidate(self):
        for radius in (4.5, 5.5, 6.5):
            with self.subTest(radius=radius):
                w = self.world(radius=radius, minRadius=radius, **CASE_A)
                session, why = self.start(w)
                if radius > 6 and session is None:          # a wide tractor may be refused, unmoved
                    self.assertIn(why, ("LOCAL_PATH_HITS_WAGON", "LOCAL_PATH_LEAVES_AREA"))
                    self.assertEqual(self.drives(w), [])
                    continue
                self.assertIsNotNone(session, why)
                code, _ = self.run_to_end(w, session)
                self.assertEqual(code, "LINED_UP")
                self.assertTrue(all(d["forwards"] for d in self.drives(w) if d["allowed"]))
                self.assertLessEqual(w.maxRadius, self.hitch.LOCAL_LIMITS.workRadiusM)
                self.assertFalse(w.inBody)
                self.assert_lined_up(w)
                self.assert_braked_last(w)
                self.assertEqual(self.W.reverse(w), "CANDIDATE")

    def test_case_b_never_reverses_first_and_lines_up_or_refuses_before_moving(self):
        w = self.world(radius=5.5, minRadius=5.5, **CASE_B)
        session, why = self.start(w)
        self.assertIsNotNone(session, why)
        code, _ = self.run_to_end(w, session)
        self.assertEqual(code, "LINED_UP")
        drives = [d for d in self.drives(w) if d["allowed"]]
        self.assertTrue(drives and all(d["forwards"] for d in drives))
        self.assertLessEqual(w.maxRadius, self.hitch.LOCAL_LIMITS.workRadiusM)
        self.assertFalse(w.inBody)
        self.assert_lined_up(w)
        self.assertEqual(self.W.reverse(w), "CANDIDATE")

    def test_live_geometry_beyond_the_tractors_turning_capability_is_refused_unmoved(self):
        for case in (CASE_A, CASE_B):
            with self.subTest(case=case):
                w = self.world(radius=7.5, minRadius=7.5, **case)
                session, why = self.start(w)
                self.assertIsNone(session)
                self.assertIn(why, ("LOCAL_PATH_LEAVES_AREA", "LOCAL_PATH_HITS_WAGON", "LOCAL_PATH_TOO_LONG",
                                    "TURN_RADIUS_OUT_OF_RANGE"))
                self.assertEqual(self.drives(w), [])
                # The refusal is logged with the numbers.
                self.assertTrue(any("REFUSED " + why in n and "lateral=" in n for n in self.notes(w)))

    def test_phases_are_logged_once_each_in_order_with_the_diagnostics(self):
        w = self.world(**CASE_A)
        session, _ = self.start(w)
        self.run_to_end(w, session)
        notes = self.notes(w)
        phases = [n.split(" ")[0] for n in notes]
        order = [p for i, p in enumerate(phases) if i == 0 or phases[i - 1] != p]
        self.assertEqual(order, ["LOCAL_TURN", "LOCAL_APPROACH", "LOCAL_LINEUP"])
        self.assertLessEqual(len(notes), 5)                  # phase changes and the end, no per-frame spam
        self.assertIn("END LINED_UP", notes[-1])
        for field in ("tractorJoint=", "wagonJoint=", "gap=", "ahead=", "lateral=", "headingDeg=",
                      "steerCurvature=", "direction=forward", "speedKmh=", "t=", "FROM_LOCAL_TURN"):
            self.assertTrue(any(field in n for n in notes), field)

    # ---------------------------------------------------------- corrections
    def test_forward_correction_of_a_lateral_offset_on_the_right_heading(self):
        for lateral in (-4.0, 4.0):
            with self.subTest(lateral=lateral):
                w = self.world(ahead=1.0, lateral=lateral, heading=0.0)
                session, why = self.start(w)
                self.assertIsNotNone(session, why)
                first = self.drives(w)
                code, _ = self.run_to_end(w, session)
                self.assertEqual(code, "LINED_UP")
                self.assert_lined_up(w)
                # Steers towards the axis first: the local target is on the axis side.
                start = [d for d in self.drives(w) if d["allowed"]][0]
                self.assertEqual(first, [])
                self.assertTrue(start["forwards"])

    def test_steering_follows_the_side_of_the_turn(self):
        # Facing away from the axis on either side, the first command turns back to it.
        for lateral, heading in ((-10.0, math.radians(-60)), (10.0, math.radians(60))):
            with self.subTest(lateral=lateral):
                w = self.world(ahead=8.0, lateral=lateral, heading=heading)
                session, why = self.start(w)
                self.assertIsNotNone(session, why)
                self.assertEqual(self.run_to_end(w, session)[0], "LINED_UP")
                self.assert_lined_up(w)

    def test_already_on_the_centreline_lines_up_immediately_without_moving(self):
        w = self.world(ahead=5.0, lateral=0.1, heading=math.radians(2))
        session, _ = self.start(w)
        self.assertEqual(self.run_to_end(w, session)[0], "LINED_UP")
        self.assertEqual([d for d in self.drives(w) if d["allowed"]], [])

    def test_random_starts_line_up_or_are_refused_before_any_motion(self):
        rng = random.Random(1709)
        outcomes = {}
        for _ in range(60):
            r, a = rng.uniform(4, 20), rng.uniform(-math.pi, math.pi)
            radius = rng.choice((4.5, 5.5, 6.5))
            w = self.world(ahead=r * math.cos(a), lateral=r * math.sin(a), heading=rng.uniform(-math.pi, math.pi),
                           radius=radius, minRadius=radius, wagonYaw=rng.uniform(-3, 3))
            session, why = self.start(w)
            if session is None:
                self.assertEqual(self.drives(w), [])
                outcomes[why] = outcomes.get(why, 0) + 1
                continue
            code, _ = self.run_to_end(w, session)
            outcomes[code] = outcomes.get(code, 0) + 1
            self.assertEqual(code, "LINED_UP", outcomes)
            self.assertLessEqual(w.maxRadius, self.hitch.LOCAL_LIMITS.workRadiusM)
            self.assertFalse(w.inBody)
            self.assert_lined_up(w, tight=False)
        self.assertGreater(outcomes.get("LINED_UP", 0), 20, outcomes)

    # ------------------------------------------------------------ refusals
    def test_refusals_before_any_motion(self):
        cases = (
            (dict(radius="none"), {}, "TURN_RADIUS_UNKNOWN"),
            (dict(radius=9.0), {}, "TURN_RADIUS_OUT_OF_RANGE"),
            (dict(ahead=20.0, lateral=-18.0), {}, "OUTSIDE_LOCAL_AREA"),
            (dict(ahead=-5.0, lateral=-1.0, heading=0.0), {}, "LOCAL_START_AT_WAGON"),
            ({}, dict(maxSpeedKmh=6), "INVALID_LIMITS"),
            ({}, dict(timeoutMs=0), "INVALID_LIMITS"),
            ({}, dict(tractorJoint=2), "UNKNOWN_JOINT"),
        )
        for world_opts, overrides, reason in cases:
            with self.subTest(reason=reason, opts=world_opts):
                opts = dict(CASE_A)
                opts.update(world_opts)
                w = self.world(**opts)
                session, why = self.start(w, **overrides)
                self.assertIsNone(session)
                self.assertEqual(why, reason)
                self.assertEqual(self.drives(w), [])

    def test_giants_ai_minimum_radius_raises_the_planned_radius(self):
        w = self.world(radius=4.0, aiRadius=5.0, **CASE_A)
        session, why = self.start(w)
        self.assertIsNotNone(session, why)
        self.assertAlmostEqual(session.turnRadius, 5.0 * self.hitch.LOCAL.turnMargin)

    def test_seated_tractor_is_refused_and_entering_mid_maneuver_stops_it(self):
        w = self.world(**CASE_A)
        w.entered = True
        self.assertEqual(tuple(self.start(w))[:2], (None, "SEATED_DURING_MANEUVER"))
        w = self.world(**CASE_A)
        session, _ = self.start(w)

        def enter(i):
            if i == 40:
                w.entered = True
        self.assertEqual(self.run_to_end(w, session, hook=enter)[0], "SEATED_DURING_MANEUVER")
        self.assert_braked_last(w)

    def test_mid_maneuver_failures_stop_and_latch(self):
        def at(n, fn):
            return lambda i: fn() if i == n else None

        cases = (
            ("DRIVER_TOOK_OVER", lambda w: at(30, w.takeOver)),
            ("AI_ACTIVE", lambda w: at(30, lambda: setattr(w, "ai", True))),
            ("RESERVATION_LOST", lambda w: at(30, lambda: setattr(w, "reserved", False))),
            ("WAGON_MOVED", lambda w: at(30, lambda: setattr(w.eye, "x", w.eye.x + 0.4))),
            ("WAGON_MOVED", lambda w: at(30, lambda: setattr(w.wagonRoot, "z", w.wagonRoot.z + 0.4))),
            ("UNKNOWN_OBSERVATION", lambda w: at(30, lambda: setattr(w, "brokenNode", w.joint))),
            ("ENVELOPE_EXCEEDED", lambda w: at(30, lambda: setattr(w.steer, "x", w.eye.x + 30))),
        )
        for reason, hook in cases:
            with self.subTest(reason=reason):
                w = self.world(**CASE_A)
                session, _ = self.start(w)
                code, steps = self.run_to_end(w, session, hook=hook(w))
                self.assertEqual(code, reason)
                self.assertEqual(steps, 30)
                self.assert_braked_last(w)
                before = len(w.drives)
                self.assertEqual(self.hitch.maneuverStep(w.env, session, 50), reason)   # latched
                self.assertFalse(dict(w.drives[before + 1])["allowed"])
                self.assertIn("END " + reason, self.notes(w)[-1])

    def test_stall(self):
        w = self.world(**CASE_A)
        w.stuck = True
        session, _ = self.start(w)
        code, steps = self.run_to_end(w, session)
        self.assertEqual(code, "STALLED")
        self.assertAlmostEqual(steps * 50, self.hitch.LOCAL.stallMs, delta=200)
        self.assert_braked_last(w)

    # --------------------------------------------- steering diagnostics (read-only)
    def steers(self, w):
        lines = list(w.steers.values())
        return [dict(p.split("=", 1) for p in line.split(" ") if "=" in p and not p.startswith("targetLocal"))
                for line in lines], lines

    def test_steering_is_sampled_at_every_giants_layer_and_changes_nothing(self):
        w = self.world(**CASE_A)
        session, _ = self.start(w)
        code, steps = self.run_to_end(w, session)
        samples, lines = self.steers(w)
        fields = list(self.hitch.STEER_FIELDS.values())
        self.assertGreater(len(samples), self.hitch.STEER_FIRST_FRAMES)
        self.assertLessEqual(len(samples), self.hitch.STEER_FIRST_FRAMES + steps * 50 // self.hitch.STEER_SAMPLE_MS + 1)
        for sample, line in zip(samples, lines):
            self.assertIn(" STEER ", line)
            self.assertIn("targetLocal=(", line)
            for field in ["cmdCurvature", "t", "along", *fields]:
                self.assertIn(field, sample, line)
        self.assertEqual(samples[0]["articulated"], "false")
        self.assertEqual((samples[0]["wheels"], samples[0]["steerableWheels"]), ("2", "1"))
        self.assertTrue(any(abs(float(s["rotatedTime"])) > 0.05 for s in samples))
        self.assertEqual(len(self.notes(w)), len(set(self.notes(w))))    # phase notes unchanged in kind
        # Read-only: the same run without the diagnostics drives exactly the same.
        quiet = self.world(**CASE_A)
        self.hitch.onSteer = None
        session, _ = self.start(quiet)
        self.assertEqual(self.run_to_end(quiet, session), (code, steps))
        self.assertEqual(self.drives(quiet), self.drives(w))

    def test_steering_diagnostics_show_a_command_the_wheels_do_not_follow(self):
        # The live 2026-10-18 case: commands grow, the heading never changes.
        w = self.world(**CASE_A)
        w.noSteer = True
        session, _ = self.start(w)
        self.assertEqual(self.run_to_end(w, session)[0], "DIVERGED")
        samples, _ = self.steers(w)
        lost = [s for s in samples if abs(float(s["rotatedTime"])) > 0.1 and float(s["wheelAngleDeg"]) == 0]
        self.assertTrue(lost)
        self.assertEqual(len({s["steerNodeYawDeg"] for s in samples}), 1)

    def test_steering_diagnostics_show_another_writer_resetting_the_steering(self):
        w = self.world(**CASE_A)
        w.resetSteer = True
        session, _ = self.start(w)
        self.run_to_end(w, session)
        samples, _ = self.steers(w)
        self.assertTrue(any(s["rotatedTimePrevEnd"] not in ("nil", s["rotatedTimeBefore"]) for s in samples))
        self.assertTrue(all(float(s["rotatedTimeBefore"]) == 0 for s in samples))

    def test_unreadable_steering_state_is_logged_nil_and_changes_nothing(self):
        w = self.world(bareSteering=True, **CASE_A)
        session, _ = self.start(w)
        code, _ = self.run_to_end(w, session)
        self.assertEqual(code, "LINED_UP")
        samples, _ = self.steers(w)
        self.assertTrue(samples)
        self.assertEqual({samples[0][f] for f in ("rotatedTime", "targetRotTime", "aiSteeringSpeed", "wheels")},
                         {"nil"})

    def test_the_tractor_is_active_while_it_drives_and_released_on_every_end(self):
        # Live 2026-10-18: without forceIsActive GIANTS never turned the wheels.
        for end in ("lined_up", "abort", "failure"):
            with self.subTest(end=end):
                w = self.world(**CASE_A)
                session, _ = self.start(w)
                if end == "abort":
                    self.run_to_end(w, session, steps=40)
                    self.assertIs(w.tractor.forceIsActive, True)
                    self.hitch.abort(w.env, session, "STOPPED")
                elif end == "failure":
                    self.assertEqual(self.run_to_end(w, session, hook=lambda i: i == 60 and w.wagonRoot.__setitem__(
                        "x", w.wagonRoot.x + 5))[0], "WAGON_MOVED")
                else:
                    self.assertEqual(self.run_to_end(w, session)[0], "LINED_UP")
                self.assertIs(w.tractor.forceIsActive, False)
                samples, _ = self.steers(w)
                self.assertTrue(any(int(s["steeredWheels"]) > 0 for s in samples))

    def test_without_being_active_the_wheels_never_turn(self):
        # The live defect, reproduced: hold nothing and the curve diverges.
        w = self.world(**CASE_A)
        session, _ = self.start(w)
        original = self.hitch.holdActive
        self.hitch.holdActive = self.lua.eval("function() end")
        try:
            self.assertEqual(self.run_to_end(w, session)[0], "DIVERGED")
        finally:
            self.hitch.holdActive = original
        samples, _ = self.steers(w)
        self.assertTrue(all(s["steeredWheels"] == "0" for s in samples))

    def test_an_activity_flag_someone_else_holds_is_left_alone(self):
        w = self.world(**CASE_A)
        w.tractor.forceIsActive = True
        session, _ = self.start(w)
        self.assertEqual(self.run_to_end(w, session)[0], "LINED_UP")
        self.assertIs(w.tractor.forceIsActive, True)

    def test_divergence_when_the_tractor_does_not_turn(self):
        w = self.world(**CASE_A)
        w.noSteer = True
        session, _ = self.start(w)
        self.assertEqual(self.run_to_end(w, session)[0], "DIVERGED")
        self.assert_braked_last(w)

    def test_a_tractor_turning_wider_than_it_reports_diverges_or_stays_bounded(self):
        w = self.world(radius=4.5, minRadius=8.0, **CASE_B)
        session, _ = self.start(w)
        code, _ = self.run_to_end(w, session)
        self.assertIn(code, ("DIVERGED", "KEEP_OUT", "ENVELOPE_EXCEEDED", "LINEUP_MISSED"))
        self.assertLessEqual(w.maxRadius, self.hitch.LOCAL_LIMITS.workRadiusM + 1)

    def test_timeout(self):
        w = self.world(**CASE_B)
        session, _ = self.start(w, timeoutMs=3000)
        code, steps = self.run_to_end(w, session)
        self.assertEqual(code, "TIMEOUT")
        self.assertAlmostEqual(steps * 50, 3000, delta=100)
        self.assert_braked_last(w)

    def test_abort_brakes_and_latches(self):
        w = self.world(**CASE_A)
        session, _ = self.start(w)
        self.run_to_end(w, session, steps=20)
        self.hitch.abort(w.env, session, "STOPPED")
        self.assertEqual(self.hitch.maneuverStep(w.env, session, 50), "STOPPED")
        self.assertFalse(dict(w.drives[len(w.drives)])["allowed"])

    def test_speed_is_the_session_speed_and_capped(self):
        w = self.world(**CASE_A)
        session, _ = self.start(w, maxSpeedKmh=4)
        self.run_to_end(w, session, steps=50)
        self.assertTrue(all(d["maxSpeed"] == 4 for d in self.drives(w)))

    # ------------------------------------------------- review regressions
    def test_a_start_just_beside_the_wagon_may_still_drive_away(self):
        # Parallel, 4.2 m beside the drawbar: outside the box, inside its planning
        # margin. The margin grows along the curve, so this is not refused.
        for lateral in (4.2, -4.2):
            with self.subTest(lateral=lateral):
                w = self.world(ahead=0.0, lateral=lateral, heading=0.0)
                session, why = self.start(w)
                self.assertIsNotNone(session, why)
                self.assertEqual(self.run_to_end(w, session)[0], "LINED_UP")
                self.assertFalse(w.inBody)

    def test_a_curve_past_another_vehicle_is_refused_unmoved(self):
        w = self.world(**CASE_A)
        session, _ = self.start(w)
        # Put a parked vehicle on the chosen curve, 10 m along it.
        x, y = self.hitch.curvePoint(session.curve, 10.0)[:2]
        fx, fz = math.sin(w.eye.yaw), math.cos(w.eye.yaw)
        w = self.world(**CASE_A)
        w.addVehicle(x * fx + y * fz, x * fz - y * fx)
        session, why = self.start(w)
        if session is not None:          # another curve keeps the clearance: it must hold
            code, _ = self.run_to_end(w, session)
            self.assertEqual(code, "LINED_UP")
        else:
            self.assertEqual(why, "LOCAL_PATH_BLOCKED")
            self.assertEqual(self.drives(w), [])

    def test_vehicles_attached_to_the_pair_or_far_away_do_not_block(self):
        w = self.world(**CASE_A)
        w.addVehicle(0.0, 60.0)                                  # far away
        w.addVehicle(w.steer.x - w.eye.x, w.steer.z - w.eye.z, w.tractor)   # e.g. a front weight
        session, why = self.start(w)
        self.assertIsNotNone(session, why)
        self.assertEqual(self.run_to_end(w, session)[0], "LINED_UP")

    def test_another_vehicle_moving_or_coming_close_stops_it(self):
        w = self.world(**CASE_A)
        parked = w.addVehicle(-20.0, -20.0)                      # watched: within radius + 15 m
        session, _ = self.start(w)

        def nudge(i):
            if i == 30:
                parked.rootNode.x = parked.rootNode.x + 0.5      # pushed or driven
        self.assertEqual(self.run_to_end(w, session, hook=nudge), ("OTHER_VEHICLE_MOVED", 30))
        self.assert_braked_last(w)

        w = self.world(**CASE_A)
        session, _ = self.start(w)

        def arrive(i):
            if i == 30:                                          # a vehicle drives up to it
                w.addVehicle(w.steer.x - w.eye.x + 3.0, w.steer.z - w.eye.z)
        self.assertEqual(self.run_to_end(w, session, hook=arrive), ("OTHER_VEHICLE_NEAR", 30))

    def test_the_player_on_the_curve_or_coming_close(self):
        w = self.world(**CASE_A)
        w.player.x, w.player.z = w.joint.x, w.joint.z            # standing at the tractor
        session, why = self.start(w)
        self.assertIsNone(session)
        self.assertEqual(why, "LOCAL_PATH_PERSON")
        w = self.world(**CASE_A)
        w.playerUnknown = True
        self.assertEqual(tuple(self.start(w))[:2], (None, "LOCAL_AREA_UNKNOWN"))
        w = self.world(**CASE_A)
        session, _ = self.start(w)

        def walk_up(i):
            if i == 30:
                w.player.x, w.player.z = w.steer.x + 2.0, w.steer.z
        self.assertEqual(self.run_to_end(w, session, hook=walk_up), ("PERSON_TOO_CLOSE", 30))
        self.assert_braked_last(w)

    def test_a_zero_turn_never_becomes_a_full_circle(self):
        for noise in (1e-13, -1e-13, 1e-15):
            with self.subTest(noise=noise):
                curves = list(self.hitch.curves(5.0, noise, -noise, 7.0, 6.0).values())
                self.assertAlmostEqual(curves[0].length, 2.0, places=6)
                self.assertTrue(all(c.length < 3 for c in curves))

    def test_a_curve_end_inside_the_reverse_envelope_is_lined_up_not_missed(self):
        w = self.world(**CASE_A)
        session, _ = self.start(w)
        self.hitch.LOCAL.doneHeadingDeg = 0.01                  # the tight line-up never happens
        try:
            code, _ = self.run_to_end(w, session)
        finally:
            self.hitch.LOCAL.doneHeadingDeg = 5
        self.assertEqual(code, "LINED_UP")
        self.assert_lined_up(w, tight=False)

    def test_limits_are_the_documented_bounds(self):
        L = self.hitch.LOCAL_LIMITS
        self.assertEqual(L.workRadiusM, companion_hitch.LOCAL_WORK_RADIUS_M)
        self.assertLessEqual(self.hitch.LOCAL.timeoutMs, L.timeoutMs)
        self.assertLessEqual(self.hitch.LOCAL.doneMaxAheadM, companion_hitch.ALIGN_REACH_M - 1)
        self.assertLessEqual(self.hitch.LOCAL.doneLateralM, companion_hitch.ALIGN_MAX_LATERAL_M)
        D = self.hitch.LOCAL
        self.assertEqual(D.reverseReachM, companion_hitch.ALIGN_REACH_M)
        self.assertEqual(D.reverseLateralM, companion_hitch.ALIGN_MAX_LATERAL_M)
        self.assertAlmostEqual(math.radians(D.reverseHeadingDeg), companion_hitch.ALIGN_MAX_HEADING_RAD)
        self.assertLess(D.divergeM + D.keepOutStopM, D.planMarginM + 0.5)     # drift stops before the box
        self.assertLess(D.vehicleStopM, D.vehicleClearanceM)
        self.assertLess(D.playerStopM, D.playerClearanceM)


class LuaManeuverTests51(LuaManeuverTests):
    runtime = lua51


if __name__ == "__main__":
    unittest.main()
