"""Guard and contract tests for mod/FarmhandHitch.lua (Lua 5.1 and 5.4).

Stubs implement only the upstream-evidenced calls (adapters/EVIDENCE.md). The
"physics" is a straight-line move and the candidate rule is a stub; these tests
prove the bounded state machine, not driving, steering or GIANTS' attach range.
"""
from pathlib import Path
import unittest

from lupa import lua51, lua54

ROOT = Path(__file__).resolve().parents[1]

STUBS = r"""
local Hitch = ...
local W = {}

local function node(x, z, yaw, y) return { x = x, y = y or 0, z = z, yaw = yaw or 0 } end

-- A tractor facing +z whose rear joint is 2.5 m behind it, staged so that the joint
-- is `gap` metres ahead of a wagon drawbar at (0, 0); the wagon also faces +z.
function W.new(opts)
    opts = opts or {}
    local gap = opts.gap or 4
    local w = { calls = {}, drives = {}, attachCalls = 0, candidateOverride = nil, speedOverride = nil,
                cruise = {}, ranges = {}, events = {} }
    local env = { NetworkUtil = { getObjectId = function() return 11 end } }
    w.env = env
    local wagon = { ownerFarmId = 1, rootNode = node(0, -4, 0) }
    local wagonJoint = { jointType = 3, node = node(opts.wagonX or 0, 0, 0, opts.wagonY or 0) }
    local otherWagon = { ownerFarmId = 1 }
    local tractor = { ownerFarmId = 1, spec_attacherJoints = {} }
    local tractorJoint = { jointType = opts.tractorJointType or 3, jointIndex = opts.occupied and 1 or 0,
                           jointTransform = node(0, gap) }
    local frontJoint = { jointType = 1, jointIndex = 0, jointTransform = node(0, gap + 5.5) }
    local joints = { frontJoint, tractorJoint }
    w.tractor, w.wagon, w.otherWagon = tractor, wagon, otherWagon
    w.tractorJoint, w.wagonJoint = tractorJoint, wagonJoint
    w.reserved, w.entered, w.motor, w.canRun = true, false, opts.motor ~= false, true
    w.ai = false
    w.speed = 0.0
    w.attachedImplement = nil
    w.jobValid, w.noJob = opts.jobValid, opts.noJob

    function tractor:getAttacherJoints() return joints end
    function tractor:getAttacherJointByJointDescIndex(i) return joints[i] end
    function tractor:getIsEntered() return w.entered end
    -- GIANTS Drivable:registerPlayerVehicleControlAllowedFunction; w.takeOver() is the
    -- seated player pressing a pedal or steering (actionEventAccelerate etc.).
    tractor.rootVehicle = tractor
    function tractor:registerPlayerVehicleControlAllowedFunction(vehicle, func) w.controlFunc = func end
    w.takeOver = function() return w.controlFunc(tractor) end
    function tractor:getIsAIActive() return w.ai end
    function tractor:setCruiseControlState(state)
        table.insert(w.cruise, state)
        table.insert(w.events, "cruise:" .. tostring(state))
    end
    function tractor:getIsMotorStarted() return w.motor end
    function tractor:getCanMotorRun() return w.canRun end
    function tractor:startMotor() w.motor = true; table.insert(w.calls, "startMotor") end
    function tractor:raiseActive() end
    function tractor:getAIReverserNode() return tractorJoint.jointTransform end
    function tractor:getAISteeringNode() return tractorJoint.jointTransform end
    w.aiRoot = node(0, gap + 2.5, opts.tractorYaw or 0)
    function tractor:getAIRootNode() return w.aiRoot end
    function tractor:getLastSpeed() return w.speedOverride or w.speed end
    function tractor:getActiveFarm() return 1 end
    function tractor:attachImplementFromInfo(info)
        w.attachCalls = w.attachCalls + 1
        if w.attachReturns == false then return false end
        if not w.attachVanishes then
            -- Field names from AttacherJoints:createAttachmentJoint 2279-2280, 2357.
            w.attachedImplement = { object = info.attachable, attachingIsInProgress = true,
                                    jointDescIndex = info.attacherVehicleJointDescIndex,
                                    inputJointDescIndex = info.attachableJointDescIndex }
            w.wagonAttacher = tractor
        end
        return true
    end
    function tractor:getImplementByObject(object)
        if w.attachedImplement and w.attachedImplement.object == object then return w.attachedImplement end
        return nil
    end
    function tractor:stopCurrentAIJob(message) table.insert(w.calls, "stopCurrentAIJob:" .. message.kind) end
    env.Drivable = { CRUISECONTROL_STATE_OFF = 0, CRUISECONTROL_STATE_ACTIVE = 1 }

    function wagon:getInputAttacherJoints() return { wagonJoint } end
    function wagon:getAttacherVehicle() return w.wagonAttacher end
    function wagon:isAttachAllowed(farmId, attacher)
        table.insert(w.calls, "isAttachAllowed")
        if w.attachDenied then return false, "warning" end
        return farmId == 1 and attacher == tractor, nil
    end

    env.getWorldTranslation = function(n)
        if n == w.brokenNode then error("engine error") end
        return n.x, n.y, n.z
    end
    env.localDirectionToWorld = function(n, x, y, z) return math.sin(n.yaw), 0, math.cos(n.yaw) end
    env.worldToLocal = function(n, x, y, z) return x - n.x, y, z - n.z end
    -- Stub candidate rule (NOT GIANTS'): horizontal distance within the caller's
    -- maxDistanceSq. Vertical and angle rules are deliberately absent here.
    env.AttacherJoints = { MAX_ATTACH_DISTANCE_SQ = 0.49, MAX_ATTACH_ANGLE = 0.34 }
    function env.AttacherJoints.updateVehiclesInAttachRange(vehicle, maxDistanceSq, maxAngle, isAI)
        assert(vehicle == tractor and maxAngle == 0.34 and isAI == true)
        table.insert(w.ranges, maxDistanceSq)
        if w.candidateBroken then error("engine error") end
        local info = nil
        if w.candidateOverride then
            info = w.candidateOverride
        else
            local dx = tractorJoint.jointTransform.x - wagonJoint.node.x
            local dz = tractorJoint.jointTransform.z - wagonJoint.node.z
            if dx * dx + dz * dz < maxDistanceSq then
                info = { attacherVehicle = tractor, attachable = wagon,
                         attacherVehicleJointDescIndex = 2, attachableJointDescIndex = 1 }
            end
        end
        tractor.spec_attacherJoints.attachableInfo = info
    end
    -- Stub driving: reverse straight along -z at maxSpeed (km/h), unless stuck.
    env.AIVehicleUtil = {}
    function env.AIVehicleUtil.driveToPoint(v, dt, acc, allowed, forwards, lx, lz, maxSpeed)
        table.insert(w.drives, { dt = dt, acc = acc, allowed = allowed, forwards = forwards,
                                 lx = lx, lz = lz, maxSpeed = maxSpeed })
        table.insert(w.events, allowed and "drive" or "brake")
        if allowed and acc > 0 and not w.stuck then
            local step = maxSpeed / 3.6 * dt / 1000
            tractorJoint.jointTransform.z = tractorJoint.jointTransform.z - step
            w.speed = maxSpeed
        else
            w.speed = 0
        end
    end
    -- STAGE stubs (AIJobGoTo shape).
    env.AIJobType = { GOTO = 1 }
    env.AIMessageSuccessStoppedByUser = { new = function() return { kind = "stoppedByUser" } end }
    local job = { positionAngleParameter = {} }
    w.job = job
    function job:applyCurrentState(v, mission, farmId, isDirectStart)
        table.insert(w.calls, string.format("applyCurrentState:%s:%s", tostring(farmId), tostring(isDirectStart)))
    end
    function job.positionAngleParameter:setPosition(x, z) table.insert(w.calls, string.format("setPosition:%g:%g", x, z)) end
    function job.positionAngleParameter:setAngle(a) table.insert(w.calls, string.format("setAngle:%g", a)) end
    function job:setValues() table.insert(w.calls, "setValues") end
    function job:validate(farmId) table.insert(w.calls, "validate"); return w.jobValid ~= false, "error" end
    env.g_currentMission = {
        aiJobTypeManager = { createJob = function(self, t)
            table.insert(w.calls, "createJob:" .. tostring(t))
            if w.noJob then return nil end
            return job
        end },
        aiSystem = { startJob = function(self, j, farmId)
            table.insert(w.calls, "startJob:" .. tostring(farmId))
        end },
    }
    return w
end

function W.align(w, overrides)
    local args = { tractor = w.tractor, wagon = w.wagon, tractorJoint = 2, wagonJoint = 1, farmId = 1,
                   isReserved = function() return w.reserved end, maxDistanceM = 6 }
    for k, v in pairs(overrides or {}) do args[k] = v end
    return Hitch.newAlign(w.env, args)
end

-- Run steps of `dt` ms until a terminal result or `maxSteps`; `hook(i)` mutates the world.
function W.run(w, session, dt, maxSteps, hook)
    local result
    for i = 1, maxSteps do
        if hook then hook(i) end
        result = Hitch.alignStep(w.env, session, dt)
        if result ~= "CONTINUE" then return result, i end
    end
    return result, maxSteps
end

return W
"""


class LuaHitchTests(unittest.TestCase):
    runtime = lua54

    def setUp(self):
        self.lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        self.hitch = self.lua.execute((ROOT / "mod/FarmhandHitch.lua").read_text())
        self.W = self.lua.execute(STUBS, self.hitch)

    def world(self, **opts):
        return self.W.new(self.lua.table(**opts))

    def align(self, w, **overrides):
        return self.W.align(w, self.lua.table(**overrides))

    def drives(self, w):
        return [dict(d) for d in w.drives.values()]

    def assert_braked_only_after(self, w, index):
        drives = self.drives(w)
        moving = [d for d in drives[index:] if d["allowed"] or d["acc"] != 0]
        self.assertEqual(moving, [])
        self.assertFalse(drives[-1]["allowed"])
        # Every terminal brake is followed by GIANTS' cruise control OFF (AD/CP pattern).
        self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])

    # ----------------------------------------------------------------- staging
    def test_staging_uses_giants_goto_sequence(self):
        w = self.world()
        ok, reason = self.hitch.startStaging(w.env, w.tractor, 1, 10.5, -3.25, 1.5)
        self.assertEqual((ok, reason), (True, None))
        self.assertEqual(list(w.calls.values()), [
            "createJob:1", "applyCurrentState:1:true", "setPosition:10.5:-3.25", "setAngle:1.5",
            "setValues", "validate", "startJob:1"])

    def test_staging_refusals_never_start_a_job(self):
        cases = [({"jobValid": False}, "GOTO_INVALID"), ({"noJob": True}, "NO_GOTO_JOB")]
        for opts, reason in cases:
            with self.subTest(reason=reason):
                w = self.world(**opts)
                self.assertEqual(tuple(self.hitch.startStaging(w.env, w.tractor, 1, 1, 2, 0)), (False, reason))
                self.assertFalse(any(c.startswith("startJob") for c in w.calls.values()))
        w = self.world()
        self.assertEqual(tuple(self.hitch.startStaging(w.env, w.tractor, 1, float("nan"), 2, 0)),
                         (False, "INVALID_POSE"))
        self.assertEqual(len(w.calls), 0)
        w.env.g_currentMission = None
        self.assertEqual(tuple(self.hitch.startStaging(w.env, w.tractor, 1, 1, 2, 0)), (False, "STAGE_ERROR"))

    def test_cancel_uses_giants_stop(self):
        w = self.world()
        self.assertTrue(self.hitch.cancelStaging(w.env, w.tractor))
        self.assertEqual(list(w.calls.values()), ["stopCurrentAIJob:stoppedByUser"])

    # ------------------------------------------------------------ preconditions
    def test_align_refuses_unsafe_requests(self):
        cases = [
            ({}, {"maxSpeedKmh": 5.5}, "INVALID_LIMITS"),
            ({}, {"maxSpeedKmh": 0.5}, "INVALID_LIMITS"),          # below 1 km/h GIANTS never accelerates
            ({}, {"assistRadiusM": 1.6}, "INVALID_LIMITS"),
            ({}, {"assistRadiusM": 0}, "INVALID_LIMITS"),
            ({}, {"maxVerticalM": 1.1}, "INVALID_LIMITS"),
            ({}, {"maxDistanceM": 10.5}, "INVALID_LIMITS"),
            ({}, {"maxDistanceM": 0}, "INVALID_LIMITS"),
            ({}, {"timeoutMs": 61000}, "INVALID_LIMITS"),
            ({}, {"isReserved": False}, "INVALID_LIMITS"),
            ({}, {"tractorJoint": 9}, "UNKNOWN_JOINT"),
            ({}, {"wagonJoint": 2}, "UNKNOWN_JOINT"),
            ({}, {"tractorJoint": 1}, "JOINT_TYPE_MISMATCH"),     # the front joint
            ({"tractorJointType": 4}, {}, "JOINT_TYPE_MISMATCH"),
            ({"occupied": True}, {}, "TRACTOR_JOINT_OCCUPIED"),
        ]
        for opts, overrides, reason in cases:
            with self.subTest(reason=reason, overrides=overrides, opts=opts):
                w = self.world(**opts)
                if overrides.get("isReserved") is False:
                    overrides = {"isReserved": "not a function"}
                session, got = self.align(w, **overrides)
                self.assertIsNone(session)
                self.assertEqual(got, reason)
                self.assertEqual(len(w.drives), 0)
        w = self.world()
        w.wagonAttacher = w.otherWagon
        self.assertEqual(tuple(self.align(w)), (None, "WAGON_ATTACHED"))
        w = self.world()
        w.brokenNode = w.wagonJoint.node
        self.assertEqual(tuple(self.align(w)), (None, "UNKNOWN_OBSERVATION"))

    # ------------------------------------------------------------------ success
    def test_reverse_stops_the_instant_giants_reports_the_exact_pair(self):
        w = self.world(gap=4)
        session, _ = self.align(w)
        result, steps = self.W.run(w, session, 100, 1000)
        self.assertEqual(result, "CANDIDATE")
        drives = self.drives(w)
        driving = [d for d in drives if d["allowed"]]
        self.assertTrue(driving)
        # Always reverse, never above the session speed, steering left to GIANTS.
        self.assertTrue(all(not d["forwards"] and d["maxSpeed"] == 3 and d["acc"] == 1 for d in driving))
        self.assertEqual(drives[-1], {"dt": 100, "acc": 0, "allowed": False, "forwards": False,
                                      "lx": 0, "lz": 1, "maxSpeed": 3})
        # Terminal: further steps only brake, never drive.
        count = len(drives)
        self.assertEqual(self.hitch.alignStep(w.env, session, 100), "CANDIDATE")
        self.assert_braked_only_after(w, count - 1)

    def test_motor_is_started_like_autodrive_or_fails(self):
        w = self.world(motor=False)
        session, _ = self.align(w)
        self.assertEqual(self.hitch.alignStep(w.env, session, 100), "CONTINUE")
        self.assertIn("startMotor", list(w.calls.values()))
        w = self.world(motor=False)
        w.canRun = False
        session, _ = self.align(w)
        self.assertEqual(self.hitch.alignStep(w.env, session, 100), "MOTOR_UNAVAILABLE")
        self.assert_braked_only_after(w, 0)

    # ----------------------------------------------------------------- failures
    def test_every_fail_closed_guard(self):
        def at(step, action):
            return lambda i: action() if i == step else None

        cases = {
            "TIMEOUT": (dict(gap=4), dict(timeoutMs=1000), lambda w: None),
            "DISTANCE_LIMIT": (dict(gap=4, wagonX=1.0), dict(maxDistanceM=2), lambda w: None),
            "STALLED": (dict(gap=4), {}, lambda w: setattr(w, "stuck", True)),
            "RESERVATION_LOST": (dict(gap=4), {}, lambda w: setattr(w, "reserved", False)),
            "DRIVER_TOOK_OVER": (dict(gap=4), {}, lambda w: w.takeOver()),
            "UNKNOWN_OBSERVATION": (dict(gap=4), {}, lambda w: setattr(w, "entered", None)),
            "WAGON_ATTACHED": (dict(gap=4), {}, lambda w: setattr(w, "wagonAttacher", w.otherWagon)),
            "WAGON_MOVED": (dict(gap=4), {}, lambda w: setattr(w.wagonJoint.node, "x", 0.5)),
            "OTHER_CANDIDATE": (dict(gap=4), {}, lambda w: setattr(w, "candidateOverride", self.lua.table(
                attacherVehicle=w.tractor, attachable=w.otherWagon,
                attacherVehicleJointDescIndex=2, attachableJointDescIndex=1))),
            "WRONG_JOINT": (dict(gap=4), {}, lambda w: setattr(w, "candidateOverride", self.lua.table(
                attacherVehicle=w.tractor, attachable=w.wagon,
                attacherVehicleJointDescIndex=1, attachableJointDescIndex=1))),
            "CANDIDATE_UNREADABLE": (dict(gap=4), {}, lambda w: setattr(w, "candidateBroken", True)),
            "NODE_UNREADABLE": (dict(gap=4), {}, lambda w: setattr(w, "brokenNode", w.tractorJoint.jointTransform)),
            "FARM_CHANGED": (dict(gap=4), {}, lambda w: setattr(w.wagon, "ownerFarmId", 2)),
            "AI_ACTIVE": (dict(gap=4), {}, lambda w: setattr(w, "ai", True)),
            "AI_UNKNOWN": (dict(gap=4), {}, lambda w: setattr(w, "ai", None)),
            "VERTICAL_MISMATCH": (dict(gap=4), {}, lambda w: setattr(w.wagonJoint.node, "y", 0.6)),
            # Swung 3.5 m off the wagon's axis: never chased further.
            "DIVERGED": (dict(gap=4), {}, lambda w: setattr(w.tractorJoint.jointTransform, "x", 3.5)),
        }
        expected = {"WRONG_JOINT": "OTHER_CANDIDATE", "CANDIDATE_UNREADABLE": "UNKNOWN_OBSERVATION",
                    "NODE_UNREADABLE": "UNKNOWN_OBSERVATION", "FARM_CHANGED": "RESERVATION_LOST",
                    "AI_UNKNOWN": "UNKNOWN_OBSERVATION"}
        for name, (opts, overrides, mutate) in cases.items():
            with self.subTest(guard=name):
                w = self.world(**opts)
                session, _ = self.align(w, **overrides)
                trigger = 3
                result, steps = self.W.run(w, session, 100, 2000,
                                           at(trigger, lambda: mutate(w)) if name not in ("TIMEOUT", "DISTANCE_LIMIT") else None)
                self.assertEqual(result, expected.get(name, name))
                drives = self.drives(w)
                self.assertFalse(drives[-1]["allowed"])
                # No further movement once failed.
                for _ in range(3):
                    self.assertEqual(self.hitch.alignStep(w.env, session, 100), expected.get(name, name))
                self.assert_braked_only_after(w, len(drives) - 1)
                self.assertEqual(w.attachCalls, 0)
                self.assertEqual(self.hitch.attach(w.env, session), "NOT_ALIGNED")
                # Another controller's job is never cancelled or taken over.
                self.assertNotIn("stopCurrentAIJob:stoppedByUser", list(w.calls.values()))

    def test_steering_follows_the_wagon_axis_from_the_observed_joints(self):
        # Joint 6 m ahead and 1.5 m beside the drawbar: aim on the axis 3 m nearer
        # the wagon than the joint's projection (pure pursuit onto the line).
        w = self.world(gap=6, wagonX=1.5)
        session, _ = self.align(w, maxDistanceM=8)
        self.assertEqual(self.hitch.alignStep(w.env, session, 100), "CONTINUE")
        first = self.drives(w)[0]
        self.assertAlmostEqual(first["lx"], 1.5)
        self.assertAlmostEqual(first["lz"], 3 - 6)
        # Close in: 2 m past the wagon's joint for a joint 1 m ahead of it...
        w = self.world(gap=1)
        session, _ = self.align(w)
        self.hitch.alignStep(w.env, session, 100)
        self.assertAlmostEqual(self.drives(w)[0]["lz"], (1 - 3) - 1)
        # ...and never more than 3 m past it, even with the joint beside the drawbar.
        w = self.world(gap=-0.5, wagonX=0.8)
        session, _ = self.align(w)
        self.assertEqual(self.hitch.alignStep(w.env, session, 100), "CONTINUE")
        self.assertAlmostEqual(self.drives(w)[0]["lz"], -3 + 0.5)

    def test_the_end_of_a_reverse_describes_its_progress(self):
        w = self.world(gap=4)
        session, _ = self.align(w)
        result, _ = self.W.run(w, session, 100, 200, lambda i: setattr(w, "stuck", i > 20))
        self.assertEqual(result, "STALLED")
        text = self.hitch.describe(session)
        self.assertIn("gap=", text)
        self.assertIn("lateral=0", text)
        samples = text.split("=")[-1].split()
        self.assertGreaterEqual(len(samples), 6)                # one a second, from the start
        self.assertTrue(samples[0].startswith("100:4.00/4.00/0.00/0.0"), samples[0])

    def test_terminal_result_never_resumes_when_the_cause_clears(self):
        # Reservation lost, then restored: the session must stay stopped.
        w = self.world(gap=4)
        session, _ = self.align(w)
        result, _ = self.W.run(w, session, 100, 50, lambda i: setattr(w, "reserved", i < 3))
        self.assertEqual(result, "RESERVATION_LOST")
        count = len(w.drives)
        w.reserved = True
        for _ in range(5):
            self.assertEqual(self.hitch.alignStep(w.env, session, 100), "RESERVATION_LOST")
        self.assert_braked_only_after(w, count - 1)
        # Candidate reached, then lost (e.g. a nudge): no further reversing either.
        w = self.world(gap=1)
        session, _ = self.align(w)
        self.assertEqual(self.W.run(w, session, 100, 1000)[0], "CANDIDATE")
        count = len(w.drives)
        w.tractorJoint.jointTransform.z = 2.0
        for _ in range(5):
            self.assertEqual(self.hitch.alignStep(w.env, session, 100), "CANDIDATE")
        self.assert_braked_only_after(w, count - 1)

    def test_reverse_distance_is_bounded(self):
        # The joint passes beside the drawbar (never a candidate): it must stop
        # within maxDistanceM (+ one step), never keep reversing.
        w = self.world(gap=4, wagonX=1.0)
        session, _ = self.align(w, maxDistanceM=3)
        result, _ = self.W.run(w, session, 100, 5000)
        self.assertEqual(result, "DISTANCE_LIMIT")
        travelled = 4 - w.tractorJoint.jointTransform.z
        self.assertLessEqual(travelled, 3 + 3 / 3.6 * 0.1 + 1e-9)

    # ------------------------------------------------------------------- attach
    def aligned(self):
        w = self.world(gap=1)
        session, _ = self.align(w)
        result, _ = self.W.run(w, session, 100, 1000)
        self.assertEqual(result, "CANDIDATE")
        w.speed = 0.0
        return w, session

    def test_exact_pair_attaches_and_completes(self):
        w, session = self.aligned()
        self.assertEqual(self.hitch.attach(w.env, session), "ATTACH_REQUESTED")
        self.assertEqual(w.attachCalls, 1)
        self.assertEqual(self.hitch.attachState(w.env, session, 100), "ATTACH_IN_PROGRESS")
        w.attachedImplement.attachingIsInProgress = False
        self.assertEqual(self.hitch.attachState(w.env, session, 100), "ATTACHED")
        # One attempt only.
        self.assertEqual(self.hitch.attach(w.env, session), "NOT_ALIGNED")
        self.assertEqual(w.attachCalls, 1)

    def test_attach_refusals_never_call_attach(self):
        def other(w):
            w.candidateOverride = self.lua.table(attacherVehicle=w.tractor, attachable=w.otherWagon,
                                                 attacherVehicleJointDescIndex=2, attachableJointDescIndex=1)

        cases = {
            "ATTACH_REJECTED": lambda w: setattr(w, "attachDenied", True),
            "OTHER_CANDIDATE": other,
            "NO_CANDIDATE": lambda w: setattr(w.tractorJoint.jointTransform, "z", 2.0),
            "NOT_STOPPED": lambda w: setattr(w, "speedOverride", 1.5),
            "RESERVATION_LOST": lambda w: setattr(w, "reserved", False),
            "UNKNOWN_OBSERVATION": lambda w: setattr(w, "candidateBroken", True),
            "DRIVER_TOOK_OVER": lambda w: w.takeOver(),
            "AI_ACTIVE": lambda w: setattr(w, "ai", True),
            "VERTICAL_MISMATCH": lambda w: setattr(w.wagonJoint.node, "y", 0.9),
        }
        for reason, mutate in cases.items():
            with self.subTest(reason=reason):
                w, session = self.aligned()
                mutate(w)
                self.assertEqual(self.hitch.attach(w.env, session), reason)
                self.assertEqual(w.attachCalls, 0)

    def test_acknowledged_attach_is_not_success(self):
        w, session = self.aligned()
        w.attachVanishes = True
        self.assertEqual(self.hitch.attach(w.env, session), "ATTACH_REQUESTED")
        self.assertEqual(self.hitch.attachState(w.env, session, 100), "ATTACH_ABSENT")
        w, session = self.aligned()
        w.attachReturns = False
        self.assertEqual(self.hitch.attach(w.env, session), "ATTACH_REJECTED")
        self.assertEqual(self.hitch.attachState(w.env, session, 100), "ATTACH_ABSENT")
        self.assertEqual(self.hitch.attach(w.env, session), "NOT_ALIGNED")  # no retry

    def test_attach_animation_is_bounded(self):
        w, session = self.aligned()
        self.assertEqual(self.hitch.attach(w.env, session), "ATTACH_REQUESTED")
        state = None
        for _ in range(200):
            state = self.hitch.attachState(w.env, session, 100)
            if state != "ATTACH_IN_PROGRESS":
                break
        self.assertEqual(state, "ATTACH_TIMEOUT")


    # ------------------------------------------- limits, assist envelope, joints
    def test_module_is_exposed_for_fs25(self):
        # FS25 discards a sourceFile's return value; the mod reaches it by name.
        self.assertTrue(self.lua.eval("function(t) return FarmhandHitch == t end")(self.hitch))

    def test_speed_default_is_bounded_and_not_two(self):
        limits = self.hitch.LIMITS
        self.assertEqual((self.hitch.DEFAULTS.maxSpeedKmh, limits.minSpeedKmh, limits.maxSpeedKmh), (3, 1, 5))
        for speed in (1, 4, 5):
            w = self.world()
            session, _ = self.align(w, maxSpeedKmh=speed)
            self.assertEqual(self.hitch.alignStep(w.env, session, 100), "CONTINUE")
            self.assertEqual(self.drives(w)[-1]["maxSpeed"], speed)

    def test_the_last_metres_need_an_empty_cab(self):
        # GIANTS applies a seated player's controls every frame outside an AI job
        # (Drivable:onUpdate 702-747): the bounded reverse would fight them.
        w = self.world()
        w.entered = True
        self.assertEqual(tuple(self.align(w)), (None, "SEATED_DURING_REVERSE"))
        self.assertEqual(len(w.drives), 0)
        # Someone getting in while it reverses stops it too: their controls would apply.
        w = self.world()
        session, _ = self.align(w)
        self.assertEqual(self.hitch.alignStep(w.env, session, 100), "CONTINUE")
        w.entered = True
        self.assertEqual(self.hitch.alignStep(w.env, session, 100), "SEATED_DURING_REVERSE")
        self.assertEqual(self.drives(w)[-1]["allowed"], False)

    def test_a_takeover_during_an_earlier_step_is_still_seen(self):
        w = self.world()
        self.hitch.watchDriver(w.env, w.tractor)              # armed at the job's first motion (STAGE)
        self.assertTrue(w.takeOver())                         # the player drove between STAGE and ALIGN
        self.assertEqual(tuple(self.align(w)), (None, "DRIVER_TOOK_OVER"))
        self.hitch.disarm(w.tractor)                          # the job's stop
        self.assertTrue(w.takeOver())                         # ordinary driving outside a job: ignored
        session, reason = self.align(w)
        self.assertIsNotNone(session, reason)

    def test_another_mods_control_function_is_never_replaced(self):
        w = self.world()
        w.tractor.spec_drivable = self.lua.table(playerControlAllowedFunctions=self.lua.table())
        w.tractor.spec_drivable.playerControlAllowedFunctions[11] = self.lua.eval("function() return false end")
        w.controlFunc = None
        self.hitch.watchDriver(w.env, w.tractor)
        self.assertIsNone(w.controlFunc)                      # we did not register over it

    def test_reverse_refuses_to_start_unless_stopped_and_free_of_ai(self):
        for attr, value, reason in (("ai", True, "AI_ACTIVE"), ("ai", None, "UNKNOWN_OBSERVATION"),
                                    ("entered", None, "UNKNOWN_OBSERVATION"), ("speed", 1.5, "NOT_STOPPED"),
                                    ("reserved", False, "RESERVATION_LOST")):
            with self.subTest(reason=reason):
                w = self.world()
                setattr(w, attr, value)
                self.assertEqual(tuple(self.align(w)), (None, reason))
                self.assertEqual(len(w.drives), 0)
                self.assertNotIn("stopCurrentAIJob:stoppedByUser", list(w.calls.values()))

    def test_default_uses_giants_distance_unchanged(self):
        w = self.world(gap=4)
        session, _ = self.align(w)
        self.assertEqual(self.W.run(w, session, 100, 1000)[0], "CANDIDATE")
        self.assertEqual(set(w.ranges.values()), {0.49})
        self.assertLess(session.lastGap, 0.7 + 0.1)

    def test_assist_radius_finds_the_exact_pair_earlier_without_touching_giants(self):
        w = self.world(gap=4)
        session, _ = self.align(w, assistRadiusM=1.2)
        result, _ = self.W.run(w, session, 100, 1000)
        self.assertEqual(result, "CANDIDATE")
        self.assertEqual({round(r, 6) for r in w.ranges.values()}, {1.44})
        self.assertEqual(w.env.AttacherJoints.MAX_ATTACH_DISTANCE_SQ, 0.49)   # global unchanged
        gap = 4 - (4 - w.tractorJoint.jointTransform.z)
        self.assertTrue(0.7 < gap < 1.2, gap)
        self.assert_braked_only_after(w, len(w.drives) - 1)
        w.speed = 0.0
        self.assertEqual(self.hitch.attach(w.env, session), "ATTACH_REQUESTED")
        # Smaller than GIANTS' own distance never shrinks it.
        w = self.world(gap=4)
        session, _ = self.align(w, assistRadiusM=0.3)
        self.assertEqual(session.attachDistanceSq, 0.49)

    def test_assist_radius_still_only_accepts_the_exact_pair(self):
        def other_wagon(w):
            return self.lua.table(attacherVehicle=w.tractor, attachable=w.otherWagon,
                                  attacherVehicleJointDescIndex=2, attachableJointDescIndex=1)

        def other_joint(w):
            return self.lua.table(attacherVehicle=w.tractor, attachable=w.wagon,
                                  attacherVehicleJointDescIndex=1, attachableJointDescIndex=1)

        for make in (other_wagon, other_joint):
            with self.subTest(candidate=make.__name__):
                w = self.world(gap=4)
                session, _ = self.align(w, assistRadiusM=1.5)
                w.candidateOverride = make(w)
                self.assertEqual(self.W.run(w, session, 100, 10)[0], "OTHER_CANDIDATE")
                self.assertEqual(w.attachCalls, 0)

    def test_vertical_guard_is_independent_of_the_radius(self):
        for radius, dy, maxVertical, expected in ((None, 0.6, None, "VERTICAL_MISMATCH"),
                                                  (1.5, 0.6, None, "VERTICAL_MISMATCH"),
                                                  (1.5, 0.45, None, "CANDIDATE"),
                                                  (1.5, 0.6, 0.8, "CANDIDATE"),
                                                  (1.5, 0.3, 0.2, "VERTICAL_MISMATCH")):
            with self.subTest(radius=radius, dy=dy, maxVertical=maxVertical):
                w = self.world(gap=4, wagonY=dy)
                overrides = {k: v for k, v in (("assistRadiusM", radius), ("maxVerticalM", maxVertical)) if v}
                session, _ = self.align(w, **overrides)
                self.assertEqual(self.W.run(w, session, 100, 1000)[0], expected)
                self.assertAlmostEqual(session.lastDy, dy)
                if expected != "CANDIDATE":
                    self.assertEqual(self.hitch.attach(w.env, session), "NOT_ALIGNED")
                    self.assertEqual(w.attachCalls, 0)

    def test_attached_through_the_wrong_joint_is_not_attached(self):
        for field in ("jointDescIndex", "inputJointDescIndex"):
            with self.subTest(field=field):
                w, session = self.aligned()
                self.assertEqual(self.hitch.attach(w.env, session), "ATTACH_REQUESTED")
                w.attachedImplement.attachingIsInProgress = False
                w.attachedImplement[field] = 7
                self.assertEqual(self.hitch.attachState(w.env, session, 100), "ATTACH_MISMATCH")


class Lua51HitchTests(LuaHitchTests):
    runtime = lua51
