-- Game-side HITCH primitives for one reserved tractor/wagon pair (server only).
--
-- Composition (companion/executor.py owns the order and verifies every step):
--   STAGE          GIANTS AIJobGoTo towards the staging pose, only from outside
--                  the local work radius: coarse travel, never arrival (Hitch.startStaging)
--   LOCAL_MANEUVER bounded forward curve onto the wagon's centreline from the
--                  observed joints; steering by GIANTS' driveToPoint (Hitch.newManeuver/maneuverStep)
--   ALIGN_REVERSE  bounded reverse on the observed joints; steering by
--                  GIANTS' AIVehicleUtil.driveToPoint               (Hitch.newAlign/alignStep)
--   ATTACH         GIANTS attachableInfo + isAttachAllowed +
--                  attachImplementFromInfo                         (Hitch.attach/attachState)
-- Every game call is cited in adapters/EVIDENCE.md. This is a library: only the
-- console-only probe (mod/FarmhandProbe.lua) calls it, and it has never run in FS25.
-- Game globals are injected as `env` (getfenv(0) in game). Lua 5.1 compatible.

local Hitch = {}

-- Hard caps: the reverse can never become a general navigation mode, and the
-- attach-assist envelope can never become a long-range "snap".
Hitch.LIMITS = { minSpeedKmh = 1, maxSpeedKmh = 5, maxDistanceM = 10, timeoutMs = 60000,
                 maxAssistRadiusM = 1.5, maxVerticalM = 1.0 }
-- Provisional values; tune at the first FS25 test.
Hitch.DEFAULTS = {
    -- GIANTS driveToPoint stops accelerating whenever maxSpeed * (1 - fac^0.25) < 1
    -- km/h, fac = steering error share (AIVehicleUtil 69-73): at 2 km/h any error
    -- above ~6% stalls it, at 3 km/h above ~20%; below 1 km/h it never moves.
    maxSpeedKmh = 4.8,       -- 3 mph on the curved and reversing moves (the user, 2026-10-31)
    timeoutMs = 30000,       -- hard timeout for the whole reverse
    wagonToleranceM = 0.25,  -- the wagon's input joint may not move more than this
    stallMs = 4000,          -- no progress for this long means stalled
    stallProgressM = 0.05,   -- minimum gap reduction that counts as progress
    -- Steer at a point on the wagon's axis this far beyond the joint's projection
    -- onto it, never more than this far past the wagon's joint: the observed joint
    -- is led onto the axis, then straight in (pure pursuit on the axis line).
    lookaheadM = 3,
    -- The reverse never corrects more than this: drifting this far sideways of the
    -- axis, or past the wagon's joint, ends it (DIVERGED). companion/hitch.py only
    -- starts it within ALIGN_MAX_LATERAL_M (2 m) of the axis.
    divergeLateralM = 3,
    divergeBehindM = 1,
    traceMs = 1000,          -- one progress sample per second for the end-of-reverse log
    attachTimeoutMs = 15000, -- bound on the attach animation
    -- Farmhand's own maximum hitch-point height difference, independent of the
    -- candidate distance: the attach-range rule derives its vertical tolerance
    -- from that distance (Manual Attach VehicleAttachmentHandler.lua:462,521:
    -- dy^2 < 4*maxDistanceSq), so a larger radius would otherwise loosen it too.
    maxVerticalM = 0.5,
}

local atan2 = math.atan2 or math.atan   -- Lua 5.1 (FS25) / 5.4 (tests)

local function finite(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

local function distance(ax, az, bx, bz)
    return math.sqrt((ax - bx) ^ 2 + (az - bz) ^ 2)
end

local function world(env, node)
    local x, y, z = env.getWorldTranslation(node)
    assert(finite(x) and finite(y) and finite(z))
    return x, y, z
end

-- ------------------------------------------------------------------ STAGE

-- Start GIANTS' own GOTO job towards (x, z, yaw). Job type "GOTO" is registered by
-- AIJobTypeManager (line 35); AIJobGoTo:applyCurrentState sets the vehicle (120)
-- and setValues turns the position/angle parameter into AITaskDriveTo's target
-- (147-157). The start sequence is AutoDrive's (Specialization.lua:1518-1527).
-- Returns true, or false and a reason. A started job is not arrival.
function Hitch.startStaging(env, tractor, farmId, x, z, yaw)
    if not (finite(x) and finite(z) and finite(yaw)) then return false, "INVALID_POSE" end
    local ok, started, reason = pcall(function()
        local mission = env.g_currentMission
        local job = mission.aiJobTypeManager:createJob(env.AIJobType.GOTO)
        if job == nil then return false, "NO_GOTO_JOB" end
        job:applyCurrentState(tractor, mission, farmId, true)
        job.positionAngleParameter:setPosition(x, z)
        job.positionAngleParameter:setAngle(yaw)
        job:setValues()
        local valid = job:validate(farmId)
        if valid ~= true then return false, "GOTO_INVALID" end
        mission.aiSystem:startJob(Hitch.onJobStart ~= nil and Hitch.onJobStart(job) or job, farmId)
        return true
    end)
    if not ok then return false, "STAGE_ERROR" end
    return started, reason
end

-- Cancel a running job the way GIANTS' own toggle does (AIJobVehicle line 431).
function Hitch.cancelStaging(env, tractor)
    return pcall(function()
        tractor:stopCurrentAIJob(env.AIMessageSuccessStoppedByUser.new())
    end)
end

-- --------------------------------------------------------- GIANTS candidate

-- Refresh and read GIANTS' attach candidate for the tractor, as Courseplay does
-- (AttacherJointController.lua:44, 77), but with the session's distance. GIANTS
-- still applies joint type, free joint, active input, attachability and its own
-- angle rule (MAX_ATTACH_ANGLE is a direction-component threshold, not radians:
-- Manual Attach VehicleAttachmentHandler.lua:529-536). Returns info or nil.
local function candidate(env, s)
    local joints = env.AttacherJoints
    joints.updateVehiclesInAttachRange(s.tractor, s.attachDistanceSq, joints.MAX_ATTACH_ANGLE, true)
    local info = s.tractor.spec_attacherJoints.attachableInfo
    if info == nil or info.attachable == nil then return nil end
    return info
end

-- The fields GIANTS' attachImplementFromInfo uses (AttacherJoints 1907-1934).
local function isExact(s, info)
    return info.attacherVehicle == s.tractor and info.attachable == s.wagon
        and info.attacherVehicleJointDescIndex == s.tractorJointIndex
        and info.attachableJointDescIndex == s.wagonJointIndex
end

-- nil (no candidate), "OTHER_CANDIDATE", "VERTICAL_MISMATCH", or "CANDIDATE" and
-- the info. Only the exact planned pair is ever accepted, never the nearest one.
local function exactCandidate(env, s)
    local info = candidate(env, s)
    if info == nil then return nil end
    if not isExact(s, info) then return "OTHER_CANDIDATE" end
    -- The same two points the attach-range rule measures (Manual Attach 460, 472).
    local _, ty = world(env, s.tractorJoint.jointTransform)
    local _, wy = world(env, s.wagonJoint.node)
    s.lastDy = math.abs(ty - wy)
    if s.lastDy > s.maxVerticalM then return "VERTICAL_MISMATCH" end
    return "CANDIDATE", info
end

-- ------------------------------------------------------------ ride-along
-- The requester may sit in the tractor while Farmhand drives it (single player:
-- the mod declares multiplayer unsupported, so a seated player is the requester).
-- What matters is control, not presence: GIANTS asks every function registered
-- with Drivable:registerPlayerVehicleControlAllowedFunction (GDN Drivable
-- 1130-1135) whenever the seated player gives a non-zero accelerate, brake or
-- steer input (actionEventAccelerate 1407-1422, actionEventBrake 1427-1442,
-- actionEventSteer 1447-1478, via getIsPlayerVehicleControlAllowed 1105-1123).
-- Farmhand's function always allows the input (it never fights the player) and
-- records the takeover; the running operation then yields: DRIVER_TOOK_OVER.
Hitch.takeovers = setmetatable({}, { __mode = "k" })   -- tractor -> true once the seated player took control
Hitch.armed = setmetatable({}, { __mode = "k" })       -- tractor -> true from a job's first motion to its stop
Hitch.registered = setmetatable({}, { __mode = "k" })  -- tractor -> our function is the one GIANTS asks

local function onPlayerInput(vehicle)
    local root = vehicle.rootVehicle or vehicle
    if Hitch.armed[root] then Hitch.takeovers[root] = true end
    return true                                     -- never block the player
end

-- Arm the takeover watch at a job's first motion (later steps keep it, so a
-- takeover between two steps still counts); Hitch.disarm at the job's stop.
-- Another mod's function registered for this tractor is never replaced
-- (Drivable:registerPlayerVehicleControlAllowedFunction 1130-1135 keys by the
-- registering vehicle); without ours a takeover is not seen, nothing else changes.
function Hitch.watchDriver(env, tractor)
    if Hitch.armed[tractor] then return end
    Hitch.armed[tractor], Hitch.takeovers[tractor] = true, nil
    if Hitch.registered[tractor] or type(tractor.registerPlayerVehicleControlAllowedFunction) ~= "function" then
        return
    end
    local ok, taken = pcall(function()
        local functions = tractor.spec_drivable and tractor.spec_drivable.playerControlAllowedFunctions
        local id = env.NetworkUtil.getObjectId(tractor)
        return functions ~= nil and id ~= nil and functions[id] ~= nil
    end)
    if ok and taken then return end
    tractor:registerPlayerVehicleControlAllowedFunction(tractor, onPlayerInput)
    Hitch.registered[tractor] = true
end

function Hitch.disarm(tractor)
    Hitch.armed[tractor], Hitch.takeovers[tractor] = nil, nil
end

-- nil, or why the tractor is not Farmhand's to drive right now: the seated player
-- took control, or whether anyone sits in it cannot be read (fail closed).
function Hitch.driverConflict(tractor)
    local entered = tractor:getIsEntered()
    if entered ~= true and entered ~= false then return "UNKNOWN_OBSERVATION" end
    if Hitch.takeovers[tractor] then return "DRIVER_TOOK_OVER" end
    return nil
end

-- The minimum authority re-check before every movement and before attaching.
local function authority(s)
    local tractor, wagon = s.tractor, s.wagon
    if s.isReserved() ~= true then return "RESERVATION_LOST" end
    if tractor.ownerFarmId ~= s.farmId or wagon.ownerFarmId ~= s.farmId then return "RESERVATION_LOST" end
    local conflict = Hitch.driverConflict(tractor)
    if conflict ~= nil then return conflict end
    -- Any GIANTS AI job, and AutoDrive (its override, Specialization.lua:1794-1800).
    local ai = tractor:getIsAIActive()
    if ai ~= false then return ai == true and "AI_ACTIVE" or "UNKNOWN_OBSERVATION" end
    if wagon:getAttacherVehicle() ~= nil then return "WAGON_ATTACHED" end
    return nil
end

-- --------------------------------------------------------- ALIGN_REVERSE

local function capped(value, default, cap)
    value = value == nil and default or value
    if not finite(value) or value <= 0 or value > cap then return nil end
    return value
end

-- Prepare one bounded reverse for the planned joint pair. `args`: tractor, wagon,
-- tractorJoint and wagonJoint (GIANTS joint desc indices), farmId, isReserved
-- (function: the companion's reservation still holds), maxDistanceM; optional
-- maxSpeedKmh, timeoutMs, maxVerticalM and assistRadiusM (Farmhand-only
-- candidate distance; nil = GIANTS' own MAX_ATTACH_DISTANCE_SQ, never smaller
-- than it, and the global constant is never changed). The tractor must be
-- stopped and free of any AI job. Returns a session, or nil and a reason.
-- The bounded numeric limits of `args` with defaults applied, or nil.
function Hitch.limits(args)
    local L, D = Hitch.LIMITS, Hitch.DEFAULTS
    local r = {
        -- 0: look only (the joints already meet): ask GIANTS for the candidate, never drive.
        maxDistanceM = args.maxDistanceM == 0 and 0 or capped(args.maxDistanceM, nil, L.maxDistanceM),
        maxSpeedKmh = capped(args.maxSpeedKmh, D.maxSpeedKmh, L.maxSpeedKmh),
        timeoutMs = capped(args.timeoutMs, D.timeoutMs, L.timeoutMs),
        maxVerticalM = capped(args.maxVerticalM, D.maxVerticalM, L.maxVerticalM),
        assistRadiusM = args.assistRadiusM == nil and 0 or capped(args.assistRadiusM, nil, L.maxAssistRadiusM),
    }
    if r.maxDistanceM == nil or r.maxSpeedKmh == nil or r.maxSpeedKmh < L.minSpeedKmh
            or r.timeoutMs == nil or r.maxVerticalM == nil or r.assistRadiusM == nil then
        return nil
    end
    return r
end

-- The way an implement is towed on ``jointNode``: its heading when the joint lies on its axis, else from its
-- root towards the joint (= companion/hitch.py plan_hitch; live 2026-11-06: the Colossus 9000's Titan header,
-- folded for the road, has its drawbar 10 m beside its root and is towed sideways; aligned to its heading,
-- the reverse onto it read 70 degrees off and DIVERGED).
Hitch.SIDE_DRAWBAR_M = 2.0
function Hitch.towingDir(env, wagon, jointNode)
    local fx, _, fz = env.localDirectionToWorld(wagon.rootNode, 0, 0, 1)
    local n = math.sqrt(fx * fx + fz * fz)
    fx, fz = fx / n, fz / n
    local rx, _, rz = env.getWorldTranslation(wagon.rootNode)
    local jx, _, jz = env.getWorldTranslation(jointNode)
    local dx, dz = jx - rx, jz - rz
    local along, lateral = dx * fx + dz * fz, dx * fz - dz * fx
    if math.abs(lateral) > math.max(math.abs(along), Hitch.SIDE_DRAWBAR_M) then
        local d = math.sqrt(dx * dx + dz * dz)
        return dx / d, dz / d
    end
    return fx, fz
end

function Hitch.newAlign(env, args)
    local lim = Hitch.limits(args)
    if lim == nil or type(args.isReserved) ~= "function" then return nil, "INVALID_LIMITS" end
    local ok, session, reason = pcall(function()
        local tractor, wagon = args.tractor, args.wagon
        local vanillaSq = env.AttacherJoints.MAX_ATTACH_DISTANCE_SQ
        assert(finite(vanillaSq) and vanillaSq > 0)
        local tractorJoint = tractor:getAttacherJoints()[args.tractorJoint]
        local wagonJoint = wagon:getInputAttacherJoints()[args.wagonJoint]
        if tractorJoint == nil or wagonJoint == nil then return nil, "UNKNOWN_JOINT" end
        if type(tractor.getAttacherJointByJointDescIndex) == "function"
                and tractor:getAttacherJointByJointDescIndex(args.tractorJoint) ~= tractorJoint then
            return nil, "UNKNOWN_JOINT"
        end
        if tractorJoint.jointType ~= wagonJoint.jointType then return nil, "JOINT_TYPE_MISMATCH" end
        if tractorJoint.jointIndex ~= 0 then return nil, "TRACTOR_JOINT_OCCUPIED" end
        -- The bounded reverse is not a GIANTS AI job: a seated player rides as a passenger
        -- while it drives (Hitch.holdActive); their input is a takeover.
        local speed = tractor:getLastSpeed()
        if not finite(speed) or math.abs(speed) >= 1 then return nil, "NOT_STOPPED" end
        local wx, _, wz = world(env, wagonJoint.node)
        local tx, _, tz = world(env, tractorJoint.jointTransform)
        local fx, fz = Hitch.towingDir(env, wagon, wagonJoint.node)
        assert(finite(fx) and finite(fz))
        local s = { easeSteering = false,   -- the final reverse steers from its first frame
            tractor = tractor, wagon = wagon, farmId = args.farmId, isReserved = args.isReserved,
            tractorJointIndex = args.tractorJoint, wagonJointIndex = args.wagonJoint,
            tractorJoint = tractorJoint, wagonJoint = wagonJoint,
            maxDistanceM = lim.maxDistanceM, maxSpeedKmh = lim.maxSpeedKmh, timeoutMs = lim.timeoutMs,
            attachDistanceSq = math.max(vanillaSq, lim.assistRadiusM ^ 2), maxVerticalM = lim.maxVerticalM,
            wagonStartX = wx, wagonStartZ = wz, tractorStartX = tx, tractorStartZ = tz,
            dirX = fx, dirZ = fz, elapsed = 0, sinceProgress = 0, trace = {}, sinceTrace = nil,
            bestGap = distance(tx, tz, wx, wz), lastGap = distance(tx, tz, wx, wz), result = nil,
            checkOnly = lim.maxDistanceM == 0,
        }
        Hitch.watchDriver(env, tractor)
        local denied = authority(s)
        if denied ~= nil then return nil, denied end
        return s
    end)
    if not ok then return nil, "UNKNOWN_OBSERVATION" end
    return session, reason
end

-- Brake: GIANTS driveToPoint with allowedToDrive=false sets zero acceleration and
-- the handbrake (AIVehicleUtil lines 90-94). It also writes maxSpeed to the motor's
-- speed limit (82), so the session speed is passed rather than 0, and switches
-- cruise control on (83-84), so that is switched off again exactly as AutoDrive
-- (Specialization.lua:1213) and Courseplay (CpAIWorker.lua:481) do when they stop.
-- GIANTS steers the wheels only of an active vehicle: WheelPhysics:serverUpdate
-- (799-819) calls updateSteeringAngle only `if self.vehicle.isActive`, and
-- Vehicle:update sets isActive = getIsActive() each frame (1975), which is false
-- for an unentered vehicle without an AI job unless forceIsActive is set
-- (Vehicle:getIsActive 2626-2636; AIJobVehicle:getIsActive 383-387 makes it true
-- for GIANTS' own jobs). Live 2026-10-18 the local maneuver drove with isActive
-- false: rotatedTime followed driveToPoint, the wheels stayed straight. AutoDrive
-- sets forceIsActive while it drives (Specialization.lua:1295, DrivePathModule.lua
-- :758) and clears it when it stops (Specialization.lua:1374). Farmhand does the
-- same for its own two motions: held from the first frame that drives, only if
-- nothing else already holds it, and released on the first halt (braking does not
-- need it: live, the inactive tractor braked to rest).
-- A seated player is a passenger while Farmhand's motion drives (the user, 2026-10-22: being
-- seated is not taking control): GIANTS applies a seated player's pedal and steering every frame
-- only while getIsVehicleControlledByPlayer() is true (Drivable:onUpdate 447, 702-747), as its
-- own AI turns that off while it drives (AIJobVehicle's override). Shadowed on this vehicle for
-- the motion and restored on every exit; real input still ends it (onPlayerInput, takeover).
local function passengerOnly() return false end

function Hitch.holdActive(s)
    if s.heldActive == nil or s.heldActive == false then
        s.heldActive = s.tractor.forceIsActive ~= true
        if s.heldActive then s.tractor.forceIsActive = true end
    end
    if (s.heldControl == nil or s.heldControl == false)
            and type(s.tractor.getIsVehicleControlledByPlayer) == "function" then
        s.heldControl = { fn = rawget(s.tractor, "getIsVehicleControlledByPlayer") }
        s.tractor.getIsVehicleControlledByPlayer = passengerOnly
    end
end

function Hitch.releaseActive(s)
    if s.heldActive then
        s.heldActive = false
        if s.tractor.forceIsActive == true then s.tractor.forceIsActive = false end
    end
    if s.heldControl ~= nil and s.heldControl ~= false then
        if rawget(s.tractor, "getIsVehicleControlledByPlayer") == passengerOnly then
            s.tractor.getIsVehicleControlledByPlayer = s.heldControl.fn
        end
        s.heldControl = false
    end
end

function Hitch.halt(env, s, dt)
    s.commandedAt = s.elapsed
    pcall(env.AIVehicleUtil.driveToPoint, s.tractor, dt, 0, false, false, 0, 1, s.maxSpeedKmh)
    pcall(function() s.tractor:setCruiseControlState(env.Drivable.CRUISECONTROL_STATE_OFF) end)
    pcall(Hitch.releaseActive, s)
end

-- ------------------------------------------------------------------ speed
-- Every self-driven motion's operating limit is its session's maxSpeedKmh (AGENTS.md:
-- "Motion speed"). GIANTS' driveToPoint only sets the motor's speed limit and drives at
-- full acceleration below it (AIVehicleUtil 82, 94): it is a target, not a limit, and live
-- the LOAD runs ended at 3.69-4.74 km/h on the ramps (5f19b1c28f614266-1: 4.14) with it at 3.
-- So Farmhand measures the speed itself: the tractor's root travel (3D, as the vehicle
-- moves) over the last windowMs. It commands commandShare of the limit; above coastShare of
-- it, no acceleration; above brakeShare, a braking frame (driveToPoint not allowed to
-- drive: the brakes, AIVehicleUtil 87-94); above the limit the motion ends braked:
-- OVERSPEED. GIANTS' own reading (getLastSpeed, smoothed) and the horizontal speed are
-- recorded beside it, and a trace every traceMs, for the log.
-- The shares since live 1b18d8f457ce42fe-1 (0.85 / 0.9 / 0.97 until then): LOAD's run-in hunted
-- +-0.5 km/h about its target (0.25 s readings 1.58-2.79) and touched 3.03 just after the soft start.
Hitch.SPEED = { windowMs = 250, commandShare = 0.65, coastShare = 0.8, brakeShare = 0.9,
                overHoldMs = 500, overHardShare = 1.2,
                traceMs = 500, traceMax = 240,
                -- Downhill in the direction of travel (live c696741383fe4388-1: UNLOAD rolling off
                -- the ramps' foot backwards went 2.88 -> 3.04 km/h while only coasting): it brakes
                -- above the commanded speed, and commands downhillShare of the limit there.
                downhillSine = 0.02, downhillShare = 0.7,
                -- The throttle in proportion to what is left below the command (live
                -- 15fc...: LOAD from rest at full throttle, steering onto the axis, overshot to
                -- 3.05 km/h within 0.5 s): throttleGainKmh of margin is full throttle, never under
                -- throttleMin; before the first reading, startThrottle.
                throttleGainKmh = 1.0, throttleMin = 0.15, startThrottle = 0.15,
                -- The throttle's ceiling eased in from startThrottle to full over throttleRampMs and
                -- the motor's target (driveToPoint's maxSpeed) from softStartKmh to the command over
                -- the same time (live b1d3f6cdc2ca44e3-1: from rest at 0.3 throttle and the full
                -- target 0.24 m in 0.47 s, 3.00 km/h over the last 0.25 s).
                throttleRampMs = 1500, softStartKmh = 1.5,
                -- The steering target eased in over steerRampMs from straight ahead (live
                -- b1d3f6cdc2ca44e3-1: LOAD's run-in from 2 m off and 17 deg turned moved 0.21 m in
                -- the first 0.25 s, 3.00 km/h, GIANTS 4.25, at a third of the throttle), and the
                -- trace every fastTraceMs for the first fastTraceUntilMs.
                steerRampMs = 1500, fastTraceMs = 100, fastTraceUntilMs = 2000 }

-- The tractor's measured speed in km/h (3D and horizontal) over the window, or nil before
-- the window has filled.
function Hitch.measureSpeed(env, s)
    local x, y, z = world(env, s.tractor.rootNode)
    local t = s.elapsed or 0
    local h = s.speedHistory or {}
    s.speedHistory = h
    h[#h + 1] = { t = t, x = x, y = y, z = z }
    while #h > 2 and t - h[2].t >= Hitch.SPEED.windowMs do table.remove(h, 1) end
    local o = h[1]
    local span = t - o.t
    if span < Hitch.SPEED.windowMs then return nil end
    local flat = math.sqrt((x - o.x) ^ 2 + (z - o.z) ^ 2)
    local full = math.sqrt(flat * flat + (y - o.y) ^ 2)
    return full / span * 3600, flat / span * 3600
end

local function pitchDeg(env, v)
    local fx, fy, fz = env.localDirectionToWorld(v.rootNode, 0, 0, 1)
    if not (finite(fx) and finite(fy) and finite(fz)) then return nil end
    return math.deg(atan2(fy, math.sqrt(fx * fx + fz * fz)))
end

-- Drive one frame toward (lx, lz) in the steering (or reverser) node's frame under the speed
-- limit. nil, or "OVERSPEED" (the caller ends the motion; its latch brakes).
function Hitch.commandKmh(cap)
    -- Never under 1 km/h (or the limit, if lower): GIANTS does not accelerate below it
    -- (AIVehicleUtil 72-76).
    return math.max(cap * Hitch.SPEED.commandShare, math.min(cap, 1))
end

function Hitch.drive(env, s, dt, forwards, lx, lz)
    s.commandedAt = s.elapsed            -- this frame commanded the vehicle (Drive.step brakes one that did not)
    local S, cap = Hitch.SPEED, s.maxSpeedKmh
    s.driveStartMs = s.driveStartMs or (s.elapsed or 0)
    local driven = (s.elapsed or 0) - s.driveStartMs
    if s.easeSteering ~= false then lx = lx * math.min(1, driven / S.steerRampMs) end
    local command = s.commandKmh or (s.commandShare and math.max(math.min(cap, 1), cap * s.commandShare)
        or Hitch.commandKmh(cap))
    command = math.max(math.min(cap, 1), math.min(cap, command))
    local okF, _, fy = pcall(env.localDirectionToWorld, s.tractor.rootNode, 0, 0, 1)
    local downhill = okF and finite(fy) and ((forwards and fy < -S.downhillSine) or (not forwards and fy > S.downhillSine))
    if downhill then command = math.max(math.min(cap, 1), cap * S.downhillShare) end
    local v, flat = Hitch.measureSpeed(env, s)
    s.measuredKmh = v
    local okR, reported = pcall(function() return s.tractor:getLastSpeed() end)
    reported = okR and finite(reported) and math.abs(reported) or nil
    if v ~= nil and v > (s.peakKmh or -1) then s.peakKmh, s.peakAlong, s.peakT = v, s.lastAlong, s.elapsed end
    if flat ~= nil and flat > (s.peakFlatKmh or -1) then s.peakFlatKmh = flat end
    if reported ~= nil and reported > (s.peakReportedKmh or -1) then
        s.peakReportedKmh, s.peakReportedAlong = reported, s.lastAlong
    end
    -- The root's travel decides; GIANTS' per-frame reading is only logged: live
    -- c8668155205849e2-1 it read 2.56, 0.88, 1.50, 3.22 km/h 0.12 s apart while the root
    -- travelled 0.04-0.07 m in each (1.2-2.2 km/h).
    local mode = "drive"
    -- Over the limit for overHoldMs, or past it by overHardShare at once: OVERSPEED. A single
    -- 250 ms reading just over it brakes instead (live 2026-10-31: LOAD ended at 3.01 km/h of 3.0,
    -- one sample up from 1.35 as the tractor met the ramps).
    if v ~= nil and v > cap then
        s.overSince = s.overSince or (s.elapsed or 0)
    else
        s.overSince = nil
    end
    if v ~= nil and v > cap and (v > cap * S.overHardShare or (s.elapsed or 0) - s.overSince >= S.overHoldMs) then
        mode = "over"
    elseif v ~= nil and v > cap then
        mode = "brake"
    elseif v ~= nil and (v > cap * S.brakeShare or (s.commandKmh ~= nil and v > command + 1)
            or (downhill and v > command)) then
        mode = "brake"
    elseif v ~= nil and (v > cap * S.coastShare or (s.commandKmh ~= nil and v > command)) then
        mode = "coast"
    end
    if downhill then s.downhillFrames = (s.downhillFrames or 0) + 1 end
    s.speedModes = s.speedModes or { drive = 0, coast = 0, brake = 0, over = 0 }
    s.speedModes[mode] = s.speedModes[mode] + 1
    s.lastSpeedMode = mode
    if (s.elapsed or 0) >= (s.traceAt or 0) or mode == "over" then
        s.traceAt = (s.elapsed or 0) + (driven < S.fastTraceUntilMs and S.fastTraceMs or S.traceMs)
        s.speedRows = s.speedRows or {}
        if #s.speedRows < S.traceMax then
            local okP, pitch = pcall(pitchDeg, env, s.tractor)
            s.speedRows[#s.speedRows + 1] = { t = s.elapsed or 0, along = s.lastAlong, lateral = s.lastLateral, v = v,
                                      flat = flat, reported = reported, pitch = okP and pitch or nil, mode = mode,
                                      cmd = command }
        end
    end
    if mode == "over" then
        s.overKmh = v
        return "OVERSPEED"
    end
    local ease = math.min(1, driven / S.throttleRampMs)
    local target = math.min(command, S.softStartKmh + (command - S.softStartKmh) * ease)
    if mode == "brake" then
        -- GIANTS updates steering only when allowedToDrive is true. Its wheel
        -- physics interprets acceleration opposite the current travel as a
        -- brake pedal; zero throttle can coast downhill through an endpoint.
        local pedal = math.max(0.2, math.min(1, (v - command) / 6))
        env.AIVehicleUtil.driveToPoint(s.tractor, dt, -pedal, true, forwards, lx, lz, target)
    else
        local throttle = 0
        if mode == "drive" then
            local ceiling = S.startThrottle + (1 - S.startThrottle) * ease
            throttle = v == nil and S.startThrottle
                or math.max(S.throttleMin, math.min(ceiling, (target - v) / S.throttleGainKmh))
        end
        env.AIVehicleUtil.driveToPoint(s.tractor, dt, throttle, true, forwards, lx, lz, target)
    end
    return nil
end

-- "limit=3.0 cmd=2.55 peak=2.61@-3.20 flat=2.60 giants=2.71@-3.10 modes=drive:500,coast:3,brake:0,over:0".
function Hitch.describeSpeed(s)
    if s == nil or s.maxSpeedKmh == nil then return "speed[-]" end
    local function f(n) return n and string.format("%.2f", n) or "?" end
    local m = s.speedModes or {}
    return string.format("speed[limit=%.1f cmd=%.2f peak=%s@%s flat=%s giants=%s@%s modes=drive:%d,coast:%d,brake:%d,over:%d downhill=%d%s]",
        s.maxSpeedKmh, Hitch.commandKmh(s.maxSpeedKmh), f(s.peakKmh), f(s.peakAlong), f(s.peakFlatKmh),
        f(s.peakReportedKmh), f(s.peakReportedAlong), m.drive or 0, m.coast or 0, m.brake or 0, m.over or 0,
        s.downhillFrames or 0,
        s.overKmh and string.format(" OVERSPEED=%.2f", s.overKmh) or "")
end

-- The trace as log lines of up to `per` samples each: "t along lateral v flat giants pitch mode".
function Hitch.speedTrace(s, per)
    local lines, row = {}, {}
    local function f(n) return n and string.format("%.2f", n) or "?" end
    for _, p in ipairs((s and s.speedRows) or {}) do
        row[#row + 1] = string.format("%d:%s/%s/%s/%s/%s/%s/%s/%s", p.t, f(p.along), f(p.lateral), f(p.v), f(p.flat),
                                      f(p.reported), p.pitch and string.format("%.1f", p.pitch) or "?", p.mode, f(p.cmd))
        if #row >= (per or 8) then lines[#lines + 1] = table.concat(row, " "); row = {} end
    end
    if #row > 0 then lines[#lines + 1] = table.concat(row, " ") end
    return lines
end

-- The tractor's heading off the wagon's, in degrees (diagnostics only: GIANTS'
-- AI root, AIDrivable:getAIRootNode 515-516), or nil if unreadable.
local function headingError(env, s)
    local ok, deg = pcall(function()
        local x, _, z = env.localDirectionToWorld(s.tractor:getAIRootNode(), 0, 0, 1)
        assert(finite(x) and finite(z))
        return math.deg(atan2(x * s.dirZ - z * s.dirX, x * s.dirX + z * s.dirZ))
    end)
    return ok and deg or nil
end

-- Record one progress sample per traceMs (and the first): elapsed, gap, ahead,
-- lateral, heading. Kept to the last 30 samples; logged when the reverse ends.
function Hitch.sample(env, s, dt)
    if s.sinceTrace ~= nil and s.sinceTrace + dt < Hitch.DEFAULTS.traceMs then
        s.sinceTrace = s.sinceTrace + dt
        return
    end
    s.sinceTrace = 0
    table.insert(s.trace, { s.elapsed, s.lastGap, s.lastAhead, s.lastLateral, headingError(env, s) })
    if #s.trace > 30 then table.remove(s.trace, 1) end
end

-- One line for the log: the latest error and the progress over time.
function Hitch.describe(s)
    local parts = {}
    for _, t in ipairs(s.trace or {}) do
        parts[#parts + 1] = string.format("%d:%.2f/%.2f/%.2f/%s", t[1], t[2], t[3], t[4],
            t[5] and string.format("%.1f", t[5]) or "?")
    end
    return string.format("gap=%.2f ahead=%s lateral=%s dy=%s progress(ms:gap/ahead/lateral/headingDeg)=%s",
        s.lastGap or -1, tostring(s.lastAhead), tostring(s.lastLateral), tostring(s.lastDy),
        table.concat(parts, " "))
end

local function checkAndDrive(env, s, dt)
    local tractor = s.tractor
    local denied = authority(s)
    if denied ~= nil then return denied end
    local seat = tractor:getIsEntered(); if seat ~= true and seat ~= false then return "UNKNOWN_OBSERVATION" end   -- a passenger rides; input is a takeover

    local wx, wy, wz = world(env, s.wagonJoint.node)
    if distance(wx, wz, s.wagonStartX, s.wagonStartZ) > Hitch.DEFAULTS.wagonToleranceM then
        return "WAGON_MOVED"
    end
    -- GIANTS decides attachability; the instant it names exactly our pair, stop.
    local found = exactCandidate(env, s)
    if found ~= nil then return found end
    if s.checkOnly then return "NO_CANDIDATE" end           -- look only: never drives

    local tx, _, tz = world(env, s.tractorJoint.jointTransform)
    if distance(tx, tz, s.tractorStartX, s.tractorStartZ) > s.maxDistanceM then return "DISTANCE_LIMIT" end
    if s.elapsed > s.timeoutMs then return "TIMEOUT" end
    local gap = distance(tx, tz, wx, wz)
    s.lastGap = gap
    -- The observed joints in the wagon's frame: the error signal, never a root pose.
    local ahead = (tx - wx) * s.dirX + (tz - wz) * s.dirZ
    local lateral = (tx - wx) * s.dirZ - (tz - wz) * s.dirX
    s.lastAhead, s.lastLateral = ahead, lateral
    Hitch.sample(env, s, dt)
    if math.abs(lateral) > Hitch.DEFAULTS.divergeLateralM or ahead < -Hitch.DEFAULTS.divergeBehindM then
        return "DIVERGED"
    end
    if gap < s.bestGap - Hitch.DEFAULTS.stallProgressM then
        s.bestGap, s.sinceProgress = gap, 0
    else
        s.sinceProgress = s.sinceProgress + dt
        if s.sinceProgress > Hitch.DEFAULTS.stallMs then return "STALLED" end
    end

    -- Keep the vehicle running and active as AutoDrive does (DrivePathModule 380-381),
    -- and active for GIANTS' wheel steering (Hitch.holdActive).
    if not tractor:getIsMotorStarted() then
        if not tractor:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
        tractor:startMotor()
    end
    Hitch.holdActive(s)
    tractor:raiseActive()
    -- Steer at a point on the wagon axis, lookaheadM nearer the wagon than the
    -- joint's projection (at most lookaheadM past the wagon's joint), at the joint's
    -- height; GIANTS does the steering. Local target in the reverser node, else the
    -- steering node, as Courseplay (CpAIWorker.lua:396-409).
    local along = math.max(ahead - Hitch.DEFAULTS.lookaheadM, -Hitch.DEFAULTS.lookaheadM)
    local gx = wx + s.dirX * along
    local gz = wz + s.dirZ * along
    local node = tractor:getAIReverserNode() or tractor:getAISteeringNode()
    local lx, _, lz = env.worldToLocal(node, gx, wy, gz)
    local over = Hitch.drive(env, s, dt, false, lx, lz)
    if over ~= nil then return over end
    return "CONTINUE"
end

-- One update of the bounded reverse. Returns "CONTINUE", "CANDIDATE" (GIANTS
-- reports exactly the planned joints; the tractor is braked), or a failure:
-- RESERVATION_LOST, DRIVER_TOOK_OVER, AI_ACTIVE, WAGON_ATTACHED, WAGON_MOVED,
-- OTHER_CANDIDATE, VERTICAL_MISMATCH, DISTANCE_LIMIT, TIMEOUT, STALLED, DIVERGED,
-- MOTOR_UNAVAILABLE, UNKNOWN_OBSERVATION. Any result other than CONTINUE is
-- final: later calls only brake.
function Hitch.alignStep(env, s, dt)
    if s.result ~= nil then
        Hitch.halt(env, s, dt)
        return s.result
    end
    s.elapsed = s.elapsed + dt
    local ok, code = pcall(checkAndDrive, env, s, dt)
    if not ok then code = "UNKNOWN_OBSERVATION" end
    if code ~= "CONTINUE" then
        s.result = code
        Hitch.halt(env, s, dt)
    end
    return code
end

-- End a session from outside (e.g. a stop command): latched like any terminal
-- result, so it brakes, switches cruise control off and never drives again.
function Hitch.abort(env, s, code)
    if s.result == nil then s.result = code end
    Hitch.halt(env, s, 0)
end

-- ------------------------------------------------- GIANTS collision query
-- Shared by LOCAL_MANEUVER, UNPARK and LOAD: GIANTS' own overlapBox (UNPARK below cites
-- the evidence), what to ask it for, and the rig that is never an obstacle to itself.
Hitch.MAX_RIG_UNITS = 6
-- The masks: everything a moving vehicle can hit (AutoDrive CollSensor.lua:47-48), the
-- static part of it without vehicles (CollSensorSplit.lua:3), vehicles alone (AutoDrive
-- measures its own vehicle with VEHICLE, AutoDrive.lua:218, CollisionDetectionUtils.lua:240)
-- and the terrain (UtilFuncs.lua:74).
Hitch.COLLISION_MASKS = {
    obstacles = { "DEFAULT", "STATIC_OBJECT", "DYNAMIC_OBJECT", "VEHICLE", "TERRAIN_DELTA", "TREE", "BUILDING",
                  "TRAFFIC_VEHICLE", "TRAFFIC_VEHICLE_BLOCKING" },
    static = { "DEFAULT", "STATIC_OBJECT", "DYNAMIC_OBJECT", "TERRAIN_DELTA", "TREE", "BUILDING" },
    vehicles = { "VEHICLE" },
    terrain = { "TERRAIN" },
}

local function collisionMasks(env)
    local out = {}
    for key, names in pairs(Hitch.COLLISION_MASKS) do
        local total = 0
        for _, name in ipairs(names) do
            local flag = env.CollisionFlag[name]
            assert(finite(flag) and flag > 0)
            total = total + flag
        end
        out[key] = total
    end
    return out
end

-- The tractor and everything attached below it (getAttachedImplements entries' .object,
-- as Bridge.logFieldWork), the tractor first. Errors on a loop or a deep chain.
local function rigUnits(tractor)
    local units, set = {}, {}
    local function add(v)
        assert(type(v) == "table" and not set[v] and #units < Hitch.MAX_RIG_UNITS)
        set[v] = true
        units[#units + 1] = v
        if type(v.getAttachedImplements) == "function" then
            for _, implement in ipairs(v:getAttachedImplements() or {}) do add(implement.object) end
        end
    end
    add(tractor)
    return units, set
end

local function terrainAt(env, x, z)
    local mission = env.g_currentMission
    local y = env.getTerrainHeightAtWorldPos(mission.terrainRootNode, x, 0, z)
    assert(finite(y))
    return y
end

-- The surface a wheel stands on at x, z: the terrain, or a road above it. A bridge deck is
-- GIANTS' ROAD_BRIDGE collision, CollisionFlag.ROAD (CollisionPreset.lua 46); it is read as
-- GIANTS' VehicleSystem finds where to place a vehicle, a ray straight down from 25 m above
-- (VehicleSystem.lua 841-847, RaycastUtil.raycastClosest). Read as terrain alone, a bridge
-- was a cliff over a riverbed (GRADE) and no route crossed the river (live 5aafb45731a844eb-1).
-- An overpass reads as its deck too, so a road under one is refused, never driven blind.
-- true as the second result for a road deck above the terrain.
Hitch.DECK = { aboveM = 25, minM = 0.3 }
local function surfaceAt(env, x, z)
    local ground = terrainAt(env, x, z)
    local flag = env.CollisionFlag ~= nil and env.CollisionFlag.ROAD or nil
    if type(env.raycastClosest) ~= "function" or not finite(flag) then return ground, false end
    local hit = {}
    -- A vehicle's drivable deck is no road (live 058d1ca14df04245-2: the GMC under its own
    -- just-unhitched Deckover's gooseneck read GRADE); only static decks (bridges) carry a route.
    local mission = env.g_currentMission
    function hit.onFarmhandSurface(_, node, _, y)
        local object = mission ~= nil and type(mission.getNodeObject) == "function" and mission:getNodeObject(node) or nil
        if object ~= nil and object.rootNode ~= nil then return true end      -- go on past it
        if hit.y == nil then hit.y = y end
        return false
    end
    -- Every hit, nearest first, where the engine offers it (raycastAll, FSBaseMission 2667): a
    -- vehicle on a bridge does not hide the bridge's deck.
    local cast = type(env.raycastAll) == "function" and env.raycastAll or env.raycastClosest
    cast(x, ground + Hitch.DECK.aboveM, z, 0, -1, 0, Hitch.DECK.aboveM + 1, "onFarmhandSurface", hit, flag)
    if finite(hit.y) and hit.y > ground + Hitch.DECK.minM then return hit.y, true end
    return ground, false
end

local function bitAnd(a, b, env)
    local b32 = env.bit32
    if b32 ~= nil and type(b32.band) == "function" then return b32.band(a, b) end
    local out, bit = 0, 1
    while a > 0 and b > 0 do
        if a % 2 == 1 and b % 2 == 1 then out = out + bit end
        a, b, bit = math.floor(a / 2), math.floor(b / 2), bit * 2
    end
    return out
end

local function staticMask(env)
    local total = 0
    for _, name in ipairs(Hitch.GHOST.staticBits) do
        local flag = env.CollisionFlag[name]
        assert(finite(flag) and flag > 0)
        total = total + flag
    end
    return total
end

-- GIANTS overlapBox over an oriented box (centre x z, yaw, half width, half length), h0..h1
-- metres above the terrain at its centre (or above `base`), with `mask`. Returns the query: hits (node ids
-- that are neither `rig` nor the terrain), own and terrain counts; nil when it cannot be
-- trusted (the shape count GIANTS returns must equal the callbacks performed, GDN overlapBox).
local function boxQuery(env, rig, x, z, yaw, halfWidth, halfLength, h0, h1, mask, base, everything)
    local mission = env.g_currentMission
    local ground = base or terrainAt(env, x, z)
    local q = { seen = 0, own = 0, terrain = 0, hits = {} }
    function q.onUnparkOverlap(_, nodeId)
        q.seen = q.seen + 1
        if nodeId == mission.terrainRootNode then
            q.terrain = q.terrain + 1
        else
            -- The object a collision node belongs to, as AutoDrive asks (PathFinderModule.lua:3040).
            local object = mission:getNodeObject(nodeId)
            if (object ~= nil and (rig[object] or (object.rootVehicle ~= nil and rig[object.rootVehicle])))
                    or rig[nodeId] then           -- a scenery node a bounded departure leaves (Drive.begin)
                q.own = q.own + 1
            elseif not everything and (Hitch.looseSign(env, nodeId) or Hitch.ghostPassable(env, nodeId)) then
                q.own = q.own + 1                 -- driven through, as AutoDrive / GIANTS' AI do
            else
                q.hits[#q.hits + 1] = nodeId
            end
        end
        -- true: go on (continueChecking). GIANTS' own synchronous use relies on it
        -- (TensionBelts:objectOverlapCallback returns true and collects every object);
        -- with false a query can end at its first shape (often the rig itself).
        return true
    end
    local n = env.overlapBox(x, ground + (h0 + h1) / 2, z, 0, yaw, 0, halfWidth, (h1 - h0) / 2, halfLength,
                             "onUnparkOverlap", q, mask, true, true, true, true)
    if n ~= q.seen then return nil end
    return q
end

-- Names of the first few hits, for the log (getName, as AutoDrive logs them).
local function hitNames(env, hits)
    local names = {}
    for i = 1, math.min(#hits, 4) do
        local ok, name = pcall(env.getName, hits[i])
        names[i] = ok and tostring(name) or tostring(hits[i])
        -- Where it is: map scenery has no owning object (live "col,col,col").
        local okAt, hx, _, hz = pcall(env.getWorldTranslation, hits[i])
        if okAt and finite(hx) and finite(hz) then
            names[i] = names[i] .. string.format("(%.0f,%.0f)", hx, hz)
        end
        -- A tree (GIANTS split shape: getSplitType > 0, as TreePlantManager and the chainsaw read it).
        pcall(function()
            if type(env.getSplitType) == "function" and (env.getSplitType(hits[i]) or 0) > 0 then
                names[i] = names[i] .. ":tree"
            end
        end)
        -- The object the shape belongs to (a vehicle's or placeable's name), for diagnosis.
        pcall(function()
            local object = env.g_currentMission:getNodeObject(hits[i])
            if object ~= nil then
                local owner = type(object.getName) == "function" and object:getName() or object.typeName
                names[i] = names[i] .. "@" .. tostring(owner)
            end
        end)
    end
    return table.concat(names, ",")
end

-- ---------------------------------------------------------------- envelope
-- While LOAD / UNLOAD, BACKOFF and ALONGSIDE move (AGENTS.md exceptions 5 and 6), the
-- whole rig as it stands each moment (every unit's size box at its own pose, pitched as
-- the unit is, as AutoDrive pitches its boxes: Specialization.lua 1903-1909) is asked of
-- GIANTS' collision world every checkMs, from clearanceM above each box's floor (raised by what
-- its pitch drops the box's ends) to its top:
-- `contactM` wider and longer all round it must be clear of everything but the rig, the
-- transport and the terrain (else ENVELOPE_CONTACT, braked), `nearM` wider is recorded
-- (how often and what came that close). The swept-volume checks before and ahead plan the
-- motion; this observes the clearance the rig actually kept, mounted implement included.
Hitch.ENVELOPE = { checkMs = 100, contactM = 0.15, nearM = 0.5, clearanceM = 0.3, pushedM = 0.05 }

-- A loose sign or cone: a shape with no vehicle or object of its own, a dynamic rigid body
-- with DYNAMIC_OBJECT collision. AutoDrive keeps driving through exactly these ("probably a
-- traffic sign", ADSensor:isElementBlockingVehicle, VirtualSensors.lua 391-410); so does
-- Farmhand (live f4fb84d66f9e4191-1, 13f374b86b594744-2: traffic cones ended its drives).
-- Unreadable: blocking.
function Hitch.looseSign(env, nodeId)
    local ok, loose = pcall(function()
        if env.g_currentMission:getNodeObject(nodeId) ~= nil then return false end
        return env.getHasClassId(nodeId, env.ClassIds.SHAPE)
            and env.getRigidBodyType(nodeId) == env.RigidBodyType.DYNAMIC
            and env.CollisionFlag.getHasMaskFlagSet(nodeId, env.CollisionFlag.DYNAMIC_OBJECT)
    end)
    return ok and loose == true
end

-- Scenery GIANTS' own AI drives through (AISystem.lua 105: its obstacles are AI_BLOCKING
-- alone) whose only real collision is static (STATIC_OBJECT, BUILDING, TREE): what a ghosted
-- rig (Hitch.ghost) no longer touches. Live d053a7e5822a4489-1: "collisionNonAIBlocking" beside
-- the Titan trailer refused every approach and the bounded steps. Bits that only mark (camera,
-- placement, tipping, rain, navigation, AI_DRIVABLE) are not collision. Unreadable: blocking.
local MARKING_BITS = { "CAMERA_BLOCKING", "GROUND_TIP_BLOCKING", "PLACEMENT_BLOCKING", "PRECIPITATION_BLOCKING",
                       "AI_DRIVABLE", "TRAFFIC_VEHICLE_BLOCKING", "ANIMAL_NAV_MESH_BLOCKING", "ANIMAL_POSITIONING" }
-- staticOnly: its only real collision is static, AI_BLOCKING or not (what a ghosted rig no
-- longer touches at all).
function Hitch.ghostPassable(env, nodeId, staticOnly)
    local ok, passable = pcall(function()
        if env.g_currentMission:getNodeObject(nodeId) ~= nil then return false end
        local F = env.CollisionFlag
        local group = env.getCollisionFilterGroup(nodeId)
        if not finite(group) or group <= 0 then return false end
        if not staticOnly and bitAnd(group, F.AI_BLOCKING, env) ~= 0 then return false end
        local rest = group - (staticOnly and bitAnd(group, F.AI_BLOCKING, env) or 0)
        for _, name in ipairs(MARKING_BITS) do
            if finite(F[name]) and F[name] > 0 then rest = rest - bitAnd(rest, F[name], env) end
        end
        local static = staticMask(env)
        return rest > 0 and bitAnd(rest, static, env) == rest
    end)
    return ok and passable == true
end

local function unitEnvelope(env, excluded, v, margin, mask, clearance)
    local E, size = Hitch.ENVELOPE, v.size
    local w, l, h = size.width, size.length, size.height
    local wo, lo, ho = size.widthOffset, size.lengthOffset, size.heightOffset
    assert(finite(w) and finite(l) and finite(h) and finite(wo) and finite(lo) and finite(ho))
    local fx, fy, fz = env.localDirectionToWorld(v.rootNode, 0, 0, 1)
    assert(finite(fx) and finite(fy) and finite(fz))
    -- A size box's floor is where the wheels meet the ground: pitched over a change of slope
    -- (the ramps' foot) its ends dip below the ground by up to half its length times the sine
    -- of the pitch (live bc5716eb69e74af4-1: the Series 9's floor 0.32 m under the yard's
    -- paving at 14 degrees, "contact" with the paving only). The band starts that much higher.
    clearance = (clearance or E.clearanceM) + l / 2 * math.abs(fy)
    local half = math.max(0.05, (h - clearance) / 2)
    local x, y, z = env.localToWorld(v.rootNode, wo, ho + clearance + half, lo)
    assert(finite(x) and finite(y) and finite(z))
    local mission = env.g_currentMission
    -- Ghosted (Hitch.ghost): the scenery it no longer collides with is no contact.
    local ghosted = Hitch.ghosted ~= nil and Hitch.ghosted[v.rootVehicle or v] or nil
    local q = { seen = 0, hits = {} }
    function q.onEnvelopeOverlap(_, nodeId)
        q.seen = q.seen + 1
        if nodeId ~= mission.terrainRootNode then
            local object = mission:getNodeObject(nodeId)
            if not (object ~= nil and (excluded[object] or (object.rootVehicle ~= nil and excluded[object.rootVehicle])))
                    and not Hitch.looseSign(env, nodeId)
                    and not (ghosted ~= nil and ghosted.pairs[nodeId]) then
                q.hits[#q.hits + 1] = nodeId
            end
        end
        return true
    end
    local n = env.overlapBox(x, y, z, -atan2(fy, math.sqrt(fx * fx + fz * fz)), atan2(fx, fz), 0,
                             w / 2 + margin, half, l / 2 + margin, "onEnvelopeOverlap", q, mask, true, true, true, true)
    if n ~= q.seen then return nil end
    return q.hits
end

-- The lowest corner of a unit's size box floor above the terrain under it, metres (diagnostics).
local function unitFloorClearance(env, v)
    local size = v.size
    local low = math.huge
    for _, sx in ipairs({ -1, 1 }) do
        for _, sz in ipairs({ -1, 1 }) do
            local x, y, z = env.localToWorld(v.rootNode, size.widthOffset + sx * size.width / 2, size.heightOffset,
                                             size.lengthOffset + sz * size.length / 2)
            low = math.min(low, y - terrainAt(env, x, z))
        end
    end
    return low
end

-- What a hit shape is, for the log: its name and its object's name and class (ClassUtil, as the
-- bridge's AI_JOB_STOPPED listener reads classes).
local function hitDetail(env, hits)
    local names = {}
    for i = 1, math.min(#hits, 3) do
        local text = hitNames(env, { hits[i] })
        pcall(function()
            local object = env.g_currentMission:getNodeObject(hits[i])
            if object ~= nil then text = text .. "<" .. tostring(env.ClassUtil.getClassNameByObject(object)) .. ">" end
            local _, hy = env.getWorldTranslation(hits[i])
            local hx, _, hz = env.getWorldTranslation(hits[i])
            text = text .. string.format("^%.2f", hy - terrainAt(env, hx, hz))
        end)
        names[#names + 1] = text
    end
    return table.concat(names, ",")
end

-- Diagnostics for a rig another driver could not move (an AutoDrive stall): what touches each
-- of its units within contactM, the rig itself and what its tension belts hold excluded, as
-- "unitN:hits floor^clearance groundOnly|standing", or "-" (live b8e4fcf886464f0a-1: the
-- loaded GMC crept 3 min at full lock with nothing in AutoDrive's sensors).
-- ------------------------------------------------- ghosting past buildings
-- An early workaround, now the player's own opt-in setting (AGENTS.md 10, default OFF): ordinary
-- Farmhand motion keeps every collision. Only when the player has turned "pass through scenery
-- in tight spots" on, and only for the motion that carries that permission, does a rig near
-- buildings or held fast stop colliding with the scenery obstacles beside it. Its motive (the
-- user, 2026-10-30): around buildings the follower may be up to ~1 m off its planned line where
-- everything is tight; rigs wedged on posts and structure legs (live 13f374b86b594744-2,
-- 1682e794a2ee4476-1). Pair by pair: GIANTS' setPairCollision(componentNode, node, false), as
-- AttacherJoints (1866, 2052) and MountableObject (159, 188) switch a vehicle's parts off one
-- other node. Only obstacles: a static node GIANTS' AI avoids (AI_BLOCKING). Never the ground
-- the rig drives on: a node a ray down from a unit meets (a shed's or a dock's floor), nor
-- scenery GIANTS' AI drives over (live 6e781c7702a84a26-1: a pad's collision off, the tractor
-- dropped through it, OVERSPEED). Terrain, roads, vehicles, people, loads and triggers are
-- untouched. Planning still keeps its clearances. Restored once the rig is clear of scenery
-- (Hitch.unghost; the bridge's watcher, Hitch.releaseGhosts, for every rig no motion drives).
-- A ghosted motion is no proof of ordinary navigation.
Hitch.GHOST = { staticBits = { "STATIC_OBJECT", "BUILDING", "TREE" }, nearM = 2.0, clearM = 0.5 }
Hitch.ghosted = {}            -- tractor -> { components = {node, ...}, pairs = {[node] = true}, why }
-- Optional observer, told once per rig when its collision with scenery first goes off
-- (tractor, why, pairs): the bridge shows the player a notification, so it is never silent.
Hitch.onGhost = nil

-- The static nodes within `extra` metres of the rig's route box at x, z, yaw (every one, the
-- passable ones too), or nil when unreadable.
function Hitch.staticNodesNear(env, foot, x, z, yaw, extra)
    if foot == nil or not (finite(x) and finite(z) and finite(yaw)) then return nil end
    local ok, hits = pcall(function()
        local side, ahead = (foot.lMin + foot.lMax) / 2, (foot.aMin + foot.aMax) / 2
        local cx = x + math.sin(yaw) * ahead + math.cos(yaw) * side
        local cz = z + math.cos(yaw) * ahead - math.sin(yaw) * side
        local halfW = (foot.lMax - foot.lMin) / 2 + extra
        local halfL = (foot.aMax - foot.aMin) / 2 + extra
        local q = boxQuery(env, foot.excluded, cx, cz, yaw, halfW, halfL, 0.3, foot.height + 0.5, staticMask(env),
                           (surfaceAt(env, cx, cz)), true)
        return q and q.hits or nil
    end)
    if not ok then return nil end
    return hits
end

-- Static scenery within `extra` metres of the rig's route box: true, false, or nil.
function Hitch.staticNear(env, foot, x, z, yaw, extra)
    local hits = Hitch.staticNodesNear(env, foot, x, z, yaw, extra)
    if hits == nil then return nil end
    return #hits > 0
end

-- The nodes under the rig's units (a ray down at each root and size corner): its floor.
local function floorNodes(env, units)
    local under, mask = {}, staticMask(env)
    for _, unit in ipairs(units) do
        local points = { { 0, 0 } }
        local size = unit.size
        if size ~= nil and finite(size.width) and finite(size.length) then
            for _, sx in ipairs({ -0.4, 0.4 }) do
                for _, sz in ipairs({ -0.4, 0.4 }) do
                    points[#points + 1] = { size.width * sx + (size.widthOffset or 0), size.length * sz + (size.lengthOffset or 0) }
                end
            end
        end
        for _, pt in ipairs(points) do
            local x, y, z = env.localToWorld(unit.rootNode, pt[1], 0, pt[2])
            local hit = {}
            function hit.onFarmhandFloor(_, node) hit.node = node end
            env.raycastClosest(x, y + 1, z, 0, -1, 0, 3, "onFarmhandFloor", hit, mask)
            if hit.node ~= nil then under[hit.node] = true end
        end
    end
    return under
end

-- Whether `node` is an obstacle to ghost: a static node with no object of its own that GIANTS'
-- AI avoids (AI_BLOCKING in its collision group).
local function ghostable(env, node)
    if env.g_currentMission:getNodeObject(node) ~= nil then return false end
    local group = env.getCollisionFilterGroup(node)
    return finite(group) and bitAnd(group, env.CollisionFlag.AI_BLOCKING, env) ~= 0
end

-- Ghost the rig against `nodes` (scenery near it): true once at least one pair is off, false
-- and why otherwise (not permitted, none ghostable, all under it, an unreadable call).
-- `permitted` must be exactly true: the player's setting carried by the running motion's own
-- command. Anything else switches nothing off (GHOSTING_OFF).
function Hitch.ghost(env, tractor, why, nodes, permitted)
    if permitted ~= true then return false, "GHOSTING_OFF" end
    local isNew = Hitch.ghosted[tractor] == nil
    local ok, result, reason = pcall(function()
        local g = Hitch.ghosted[tractor]
        if g == nil then
            local components = {}
            for _, unit in ipairs((rigUnits(tractor))) do
                for _, component in ipairs(unit.components or {}) do components[#components + 1] = component.node end
            end
            assert(#components > 0)
            g = { components = components, pairs = {}, why = why, units = (rigUnits(tractor)) }
        end
        local under = floorNodes(env, g.units)
        local added = 0
        -- Recorded before each pair goes off: an error part way still leaves every pair it
        -- switched off restorable (Hitch.unghost; setting a pair back on that never went off is harmless).
        Hitch.ghosted[tractor] = g
        for _, node in ipairs(nodes or {}) do
            if not g.pairs[node] and not under[node] and ghostable(env, node) then
                g.pairs[node] = true
                for _, component in ipairs(g.components) do env.setPairCollision(component, node, false) end
                added = added + 1
            end
        end
        if next(g.pairs) == nil then
            Hitch.ghosted[tractor] = nil
            return false, "NOTHING_TO_GHOST"
        end
        return true, added
    end)
    if not ok then
        if Hitch.ghosted[tractor] ~= nil and next(Hitch.ghosted[tractor].pairs) == nil then Hitch.ghosted[tractor] = nil end
        return false, "GHOST_UNREADABLE"
    end
    if result == true and isNew and Hitch.onGhost ~= nil then
        pcall(Hitch.onGhost, tractor, why, reason)       -- a failing observer never undoes the ghost
    end
    return result, reason
end

-- Restore every pair Hitch.ghost switched off for the rig.
function Hitch.unghost(env, tractor)
    local g = Hitch.ghosted[tractor]
    if g == nil then return true end
    for node in pairs(g.pairs) do
        for _, component in ipairs(g.components) do pcall(env.setPairCollision, component, node, true) end
    end
    Hitch.ghosted[tractor] = nil
    return true
end

-- A copy of `foot` that no longer counts the scenery nodes the rig is ghosted against (they
-- do not hold it); everything else, vehicles first, still counts.
function Hitch.withGhosted(_, foot, tractor)
    local g = Hitch.ghosted[tractor]
    if g == nil then return foot end
    local copy, excluded = {}, {}
    for k, v in pairs(foot) do copy[k] = v end
    for k, v in pairs(foot.excluded or {}) do excluded[k] = v end
    for node in pairs(g.pairs) do excluded[node] = true end
    copy.excluded = excluded
    return copy
end

-- Teleport recovery (the user, 2026-10-30: "if farmhand still can not move it, teleport it and
-- any attached items to an open area close by"; AGENTS.md 19, allow teleport recovery): the
-- nearest pose within Hitch.TELEPORT.maxM where the whole rig's route box is clear and 1 m from
-- scenery, on the ground (never a bridge deck: GIANTS places it at the terrain), at the rig's
-- own heading first. x, z, yaw, or nil.
Hitch.TELEPORT = { minM = 4, maxM = 50, stepM = 2, angles = 16, clearM = 1.0 }
-- minM: no nearer than this (a spot to drive to is at least a rig length off: live
-- 4f0491d8288840c6-1, 4 m beside the 20 m GMC rig at its own heading, unreachable).
-- accept(px, pz): an optional further test of a spot (Drive: one GIANTS' navigation reaches);
-- maxM: how far out to look (Hitch.TELEPORT.maxM by default).
function Hitch.openSpot(env, foot, x, z, yaw, minM, accept, maxM)
    local T = Hitch.TELEPORT
    local headings = { yaw, yaw + math.pi / 2, yaw - math.pi / 2, yaw + math.pi }
    for r = math.max(T.minM, minM or 0), maxM or T.maxM, T.stepM do
        for a = 0, T.angles - 1 do
            local angle = yaw + a * 2 * math.pi / T.angles
            local px, pz = x + math.sin(angle) * r, z + math.cos(angle) * r
            local _, deck = surfaceAt(env, px, pz)
            if not deck and (accept == nil or accept(px, pz)) then
                for _, h in ipairs(headings) do
                    if Hitch.routePoseClear(env, foot, px, pz, h, 0) == true
                            and Hitch.staticNear(env, foot, px, pz, h, T.clearM) == false then
                        return px, pz, h
                    end
                end
            end
        end
    end
    return nil
end

-- Ghosted rigs no drive holds any more, restored once each stands clear of scenery
-- (Hitch.GHOST.clearM). `busy(tractor)`: true while a Farmhand motion still drives it.
function Hitch.releaseGhosts(env, busy)
    local gone = {}
    for tractor in pairs(Hitch.ghosted) do
        if not busy(tractor) then
            -- One rig at a time: one that cannot be read (sold, deleted) never holds up the others.
            local read, clear, x, z = pcall(function()
                local okFoot, foot = pcall(Hitch.routeFootprint, env, tractor)
                local px, _, pz = world(env, tractor.rootNode)
                local fx, _, fz = env.localDirectionToWorld(tractor.rootNode, 0, 0, 1)
                local yaw = (math.atan2 or math.atan)(fx, fz)
                return okFoot and foot ~= nil and Hitch.staticNear(env, foot, px, pz, yaw, Hitch.GHOST.clearM) == false,
                       px, pz
            end)
            if not read then
                gone[#gone + 1] = tractor
            elseif clear then
                Hitch.unghost(env, tractor)
                print(string.format("[Farmhand] collision with buildings restored for the rig at %.1f,%.1f (clear of them)", x, z))
            end
        end
    end
    for _, tractor in ipairs(gone) do
        Hitch.unghost(env, tractor)          -- each pair put back under pcall; the entry dropped
        print("[Farmhand] a ghosted rig can no longer be read (removed?): its collision pairs put back")
    end
end

-- Proxy-envelope overlap, not a native physics contact event. In particular a
-- long moving-tool box can retain a tree hit while the vehicle backs away.
function Hitch.rigContacts(env, vehicle, foot)
    local E = Hitch.ENVELOPE
    -- A drive footprint also contains belted cargo and its mounted equipment;
    -- observe those same units for contact instead of only the hitch chain.
    local units, excluded
    if foot ~= nil then units, excluded = foot.units, foot.excluded
    else units, excluded = rigUnits(vehicle) end
    for _, unit in ipairs(units) do
        local belts = unit.spec_tensionBelts
        for _, data in pairs(belts ~= nil and belts.objectsToJoint or {}) do
            if data.object ~= nil then excluded[data.object] = true end
        end
        -- And what a carrier holds by GIANTS' DynamicMountAttacher (live 2026-11-06: the MF 8570 Header
        -- locked on its trailer read as fixed contact, NAVIGATE_START_CONTACT).
        local mounts = unit.spec_dynamicMountAttacher
        for object in pairs(mounts ~= nil and mounts.dynamicMountedObjects or {}) do excluded[object] = true end
    end
    local masks = collisionMasks(env)
    local out = {}
    for index, unit in ipairs(units) do
        local hits = unitEnvelope(env, excluded, unit, E.contactM, masks.obstacles)
        if hits == nil then
            out[#out + 1] = string.format("unit%d:?", index)
        elseif #hits > 0 then
            local high = unitEnvelope(env, excluded, unit, E.contactM, masks.obstacles, E.clearanceM + 0.5)
            local okLow, low = pcall(unitFloorClearance, env, unit)
            out[#out + 1] = string.format("unit%d:%s floor^%s %s", index, hitDetail(env, hits),
                                          okLow and finite(low) and string.format("%.2f", low) or "?",
                                          high == nil and "above?" or (#high == 0 and "groundOnly" or "standing"))
        end
    end
    return #out > 0 and table.concat(out, " ") or "-"
end

-- nil (clear), or "ENVELOPE_CONTACT" / "UNKNOWN_OBSERVATION". s.units (the rig), s.rig (excluded:
-- LOAD's includes the trailer it drives onto, never the truck), s.transport (excluded too when
-- s.envelopeTransport: BACKOFF starts right at the ramps), s.masks.obstacles.
function Hitch.envelope(env, s)
    local E = Hitch.ENVELOPE
    if (s.elapsed or 0) < (s.envelopeAt or 0) then return nil end
    s.envelopeAt = (s.elapsed or 0) + E.checkMs
    local excluded = {}
    for unit in pairs(s.rig or {}) do excluded[unit] = true end
    for unit in pairs(s.envelopeTransport and s.transport or {}) do excluded[unit] = true end
    s.envelopeChecks = (s.envelopeChecks or 0) + 1
    for index, unit in ipairs(s.units or {}) do
        local okLow, low = pcall(unitFloorClearance, env, unit)
        if okLow and finite(low) then
            s.envelopeLow = s.envelopeLow or {}
            s.envelopeLow[index] = math.min(s.envelopeLow[index] or math.huge, low)
        end
        local hits = unitEnvelope(env, excluded, unit, E.contactM, s.masks.obstacles)
        if hits == nil then return "UNKNOWN_OBSERVATION" end
        if s.pushedOnly and #hits > 0 then
            -- LOAD / UNLOAD / BACKOFF / ALONGSIDE: another vehicle's size box is no contact (the user, 2026-10-31:
            -- the MT635 parked beside the GMC "is not in the way", its box met the Series 9's duals
            -- near the ground only); that vehicle pushed more than pushedM is.
            local kept = {}
            for _, nodeId in ipairs(hits) do
                local object = env.g_currentMission:getNodeObject(nodeId)
                local vehicle = object ~= nil and (object.rootVehicle or object) or nil
                if vehicle ~= nil and vehicle.rootNode ~= nil then
                    local vx, _, vz = world(env, vehicle.rootNode)
                    s.pushWatch = s.pushWatch or {}
                    local first = s.pushWatch[vehicle]
                    if first == nil then
                        s.pushWatch[vehicle] = { vx, vz }
                    elseif math.sqrt((vx - first[1]) ^ 2 + (vz - first[2]) ^ 2) > E.pushedM then
                        kept[#kept + 1] = nodeId
                    end
                else
                    kept[#kept + 1] = nodeId
                end
            end
            hits = kept
        end
        if #hits > 0 then
            -- Diagnostics: the same box from 0.5 m higher; clear there, the contact is near the
            -- ground only (a surface, a kerb), else something standing (a wall, a vehicle).
            local high = unitEnvelope(env, excluded, unit, E.contactM, s.masks.obstacles, E.clearanceM + 0.5)
            s.envelopeHits = string.format("unit%d:%s floor^%s %s", index, hitDetail(env, hits),
                                           okLow and finite(low) and string.format("%.2f", low) or "?",
                                           high == nil and "above?" or (#high == 0 and "groundOnly" or "standing"))
            return "ENVELOPE_CONTACT"
        end
        local near = unitEnvelope(env, excluded, unit, E.nearM, s.masks.obstacles)
        if near == nil then return "UNKNOWN_OBSERVATION" end
        if #near > 0 then
            s.envelopeNear = (s.envelopeNear or 0) + 1
            s.envelopeNearNames = s.envelopeNearNames or hitNames(env, near)
            s.envelopeNearAlong = s.envelopeNearAlong or s.lastAlong
        end
    end
    return nil
end

function Hitch.describeEnvelope(s)
    if s == nil then return "envelope[-]" end
    local lows = {}
    for i, low in pairs(s.envelopeLow or {}) do lows[#lows + 1] = string.format("unit%d^%.2f", i, low) end
    table.sort(lows)
    return string.format("envelope[units=%d checks=%d lowestFloor=%s contact=%s near=%d%s]", #(s.units or {}),
        s.envelopeChecks or 0, #lows > 0 and table.concat(lows, "/") or "-", tostring(s.envelopeHits or "-"), s.envelopeNear or 0,
        s.envelopeNearNames and string.format(" first=%s@%s", s.envelopeNearNames,
            s.envelopeNearAlong and string.format("%.2f", s.envelopeNearAlong) or "?") or "")
end


-- -------------------------------------------------------- LOCAL_MANEUVER
-- GIANTS' GOTO only brings the tractor near (live it ended 12-19 m off with its
-- agent NOT_REACHABLE). Inside the local work radius Farmhand drives the last
-- forward metres itself, with the same GIANTS driveToPoint as the reverse:
--   LOCAL_TURN      first arc, turning towards the approach
--   LOCAL_APPROACH  the straight
--   LOCAL_LINEUP    the last arc onto the wagon's centreline, then straight on it
-- ending LINED_UP (braked) with the planned joint 3-8 m ahead of the wagon's joint,
-- on its axis and facing its heading; ALIGN_REVERSE then reverses as before.
-- The reference is the shortest forward curve (two arcs of the tractor's own
-- turning radius and a straight: Dubins' CSC words) from the observed tractor to
-- that line-up, in the frame of the observed wagon joint. It is chosen once, from
-- at most 36 closed-form candidates (5 line-up points, then a farther fallback, x at most 6 words), so that
-- it keeps a margin from the work radius, from the wagon's body, from every other
-- vehicle's root and from the player; there is no search around obstacles: no
-- such curve, no motion. Every frame re-observes, steers at a point LOOKAHEAD
-- metres further along the curve, and stops on any deviation, on any other
-- vehicle moving or coming close, or on the player coming close. Forward only,
-- never past the caps; no retry.
Hitch.LOCAL_LIMITS = { workRadiusM = 25, maxPathM = 70, timeoutMs = 180000, minTurnRadiusM = 3,
                       maxTurnRadiusM = 8 }
Hitch.LOCAL = {
    timeoutMs = 180000,       -- the whole maneuver: 70 m at the governor's 1.95 km/h is 129 s
    turnMargin = 1.1,         -- plan 10% wider than the tractor's own minimum radius
    lineupAheadM = { 3.5, 4.5, 5.5, 6.5, 7 },  -- candidate line-up points (planned joint ahead)
    -- Only when none of those keeps the margins: a joint over the steering axle (a gooseneck
    -- ball, live 170ee44249ca4fab-1) swept the cab through the wagon box's corner on every
    -- last arc ending 7 m ahead. At doneMaxAheadM it ends inside the reverse's envelope.
    lineupFallbackAheadM = 8,
    lookaheadM = 3,
    sampleM = 0.5,            -- envelope check spacing along a candidate
    planMarginM = 1.5,        -- a candidate keeps this clearance from the radius and the wagon's box
    divergeM = 0.75,          -- farther than this from the curve: DIVERGED (inside planMarginM)
    keepOutStopM = 0.5,       -- while moving, stop this far short of the wagon's box (braking)
    searchBackM = 0.5, searchAheadM = 3.5, searchStepM = 0.1,
    missedM = 3,              -- this far past the curve's end without lining up: LINEUP_MISSED
    stallMs = 4000, stallProgressM = 0.05,
    doneLateralM = 0.5, doneHeadingDeg = 5, doneMinAheadM = 3, doneMaxAheadM = 8,
    -- At the curve's end the reverse's own envelope is enough (= companion/hitch.py
    -- ALIGN_REACH_M, ALIGN_MAX_LATERAL_M, ALIGN_MAX_HEADING_RAD).
    reverseReachM = 9, reverseLateralM = 2, reverseHeadingDeg = 30, endSlackM = 1.5,
    -- The wagon's body is not observed: a box behind its input joint, as long as
    -- twice the joint-to-root distance plus 2 m (never under 12 m), and this wide
    -- either side of the axis (half a wagon plus half a tractor). The tractor is
    -- checked at its planned joint, its steering node and FRONT_M ahead of it; the
    -- work radius applies to the planned joint (the companion's measure).
    bodyHalfWidthM = 3, minBodyLengthM = 12, frontM = 4.5,
    -- Any other vehicle (its root; not the pair or what is attached to them) must
    -- stay this far from the curve's tractor points, and stops it when this close
    -- while moving; the player likewise. Vehicles within the radius plus this are
    -- watched for movement.
    vehicleClearanceM = 8, vehicleStopM = 6, playerClearanceM = 6, playerStopM = 4, watchExtraM = 15,
    movedM = 0.25,
    -- Static geometry (live 34c38284ac53469b-1, 8f44d1209a9c4d40-1: it stalled on what it did
    -- not see): the tractor's size box, this much wider either side, from just above the
    -- terrain to above its top, must be clear in GIANTS' collision query every footStepM
    -- along a curve before it is chosen, and aheadM further along while it drives.
    footMarginM = 0.25, footStepM = 1.0, aheadM = 2.0, floorClearanceM = 0.3, topMarginM = 0.3,
}

local TAU = 2 * math.pi

local function wrap(a) return (a + math.pi) % TAU - math.pi end

-- A pose in the wagon frame: x ahead of the wagon's joint, y lateral (its local
-- +x side, as companion/hitch.py joint_error), p the heading off the wagon's.
local function arcEnd(x, y, p, turn, length, r)
    if turn == 0 then return x + length * math.cos(p), y + length * math.sin(p), p end
    local cx, cy = x - turn * r * math.sin(p), y + turn * r * math.cos(p)
    local q = p + turn * length / r
    return cx + turn * r * math.sin(q), cy - turn * r * math.cos(q), q
end

-- The curve's point `d` metres from its start; past the end, straight on.
function Hitch.curvePoint(c, d)
    local x, y, p = c.x, c.y, c.p
    for _, seg in ipairs(c.segs) do
        if d <= seg[2] then return arcEnd(x, y, p, seg[1], d, c.r) end
        x, y, p = arcEnd(x, y, p, seg[1], seg[2], c.r)
        d = d - seg[2]
    end
    return arcEnd(x, y, p, 0, d, c.r)
end

-- Every CSC curve of radius r from (x, y, p) to (gx, 0, 0); each one's end is
-- re-integrated and kept only if it lands on the goal.
function Hitch.curves(x, y, p, gx, r)
    local out = {}
    for _, t1 in ipairs({ 1, -1 }) do
        for _, t2 in ipairs({ 1, -1 }) do
            local c1x, c1y = x - t1 * r * math.sin(p), y + t1 * r * math.cos(p)
            local c2x, c2y = gx, t2 * r
            local dx, dy = c2x - c1x, c2y - c1y
            local d, th = math.sqrt(dx * dx + dy * dy), atan2(dy, dx)
            local headings = {}
            if t1 == t2 then headings = { th }
            elseif d >= 2 * r then
                local a = math.asin(2 * r / d)
                headings = { th + a, th - a }
            end
            for _, psi in ipairs(headings) do
                local a1, a2 = (t1 * (psi - p)) % TAU, (t2 * (0 - psi)) % TAU
                -- No turn that rounding made a full circle.
                if a1 > TAU - 1e-9 then a1 = 0 end
                if a2 > TAU - 1e-9 then a2 = 0 end
                a1, a2 = a1 * r, a2 * r
                local ax, ay = arcEnd(x, y, p, t1, a1, r)
                local back = -t2 * a2 / r
                local bx, by = c2x + t2 * r * math.sin(back), c2y - t2 * r * math.cos(back)
                local straight = (bx - ax) * math.cos(psi) + (by - ay) * math.sin(psi)
                if straight >= -1e-6 then
                    local c = { x = x, y = y, p = p, r = r, segs = { { t1, a1 }, { 0, math.max(0, straight) }, { t2, a2 } } }
                    c.length = a1 + math.max(0, straight) + a2
                    local ex, ey, ep = Hitch.curvePoint(c, c.length)
                    if math.sqrt((ex - gx) ^ 2 + ey ^ 2) < 1e-3 and math.abs(wrap(ep)) < 1e-3 then
                        out[#out + 1] = c
                    end
                end
            end
        end
    end
    table.sort(out, function(a, b) return a.length < b.length end)
    return out
end

-- The tractor's checked points for a steering-node pose (x, y, p): the steering
-- node, the planned joint (`back` behind it) and its front.
local function bodyPoints(x, y, p, back)
    local cx, cy, front = math.cos(p), math.sin(p), Hitch.LOCAL.frontM
    return { { x, y }, { x - back * cx, y - back * cy }, { x + front * cx, y + front * cy } }
end

-- nil, "ENVELOPE_EXCEEDED", "KEEP_OUT", "OTHER_VEHICLE_NEAR" or "PERSON_TOO_CLOSE"
-- for the pose: the planned joint `margin` inside the work radius, every point
-- `margin` outside the wagon's box, `vehicleM` from every other vehicle's root and
-- `playerM` from the player (both in the wagon frame, s.others / s.player).
local function outside(s, x, y, p, margin, vehicleM, playerM)
    local half = Hitch.LOCAL.bodyHalfWidthM + margin
    local points = bodyPoints(x, y, p, s.back)
    local joint = points[2]
    if math.sqrt(joint[1] ^ 2 + joint[2] ^ 2) > Hitch.LOCAL_LIMITS.workRadiusM - margin then
        return "ENVELOPE_EXCEEDED"
    end
    for _, pt in ipairs(points) do
        if pt[1] < margin and pt[1] > -s.bodyLength - margin and math.abs(pt[2]) < half then return "KEEP_OUT" end
        for _, other in ipairs(s.others) do
            if math.sqrt((pt[1] - other.x) ^ 2 + (pt[2] - other.y) ^ 2) < vehicleM then return "OTHER_VEHICLE_NEAR" end
        end
        if s.player ~= nil and math.sqrt((pt[1] - s.player.x) ^ 2 + (pt[2] - s.player.y) ^ 2) < playerM then
            return "PERSON_TOO_CLOSE"
        end
    end
    return nil
end

local PLAN_REASON = { KEEP_OUT = "LOCAL_PATH_HITS_WAGON", ENVELOPE_EXCEEDED = "LOCAL_PATH_LEAVES_AREA",
                      OTHER_VEHICLE_NEAR = "LOCAL_PATH_BLOCKED", PERSON_TOO_CLOSE = "LOCAL_PATH_PERSON",
                      STATIC = "LOCAL_PATH_OBSTRUCTED" }

-- The shortest candidate that keeps the margin, or nil and why none does: the
-- reason of the shortest candidate refused (live 3da28a3f24dd450b-1 named the
-- longest one's, the work radius, when a parked vehicle blocked every short one).
-- The fallback line-up point is tried only when no other keeps the margin.
function Hitch.chooseCurve(s, x, y, p)
    local D = Hitch.LOCAL
    local best, why, whyLength = nil, "LOCAL_NO_PATH", math.huge
    -- false when GIANTS' collision query could not be read
    local function try(ahead)
        for _, c in ipairs(Hitch.curves(x, y, p, ahead + s.back, s.turnRadius)) do
            if c.length > Hitch.LOCAL_LIMITS.maxPathM then
                if why == "LOCAL_NO_PATH" then why = "LOCAL_PATH_TOO_LONG" end
                break
            end
            if best ~= nil and c.length >= best.length then break end
            -- The margin grows over the first metre, so a start near an edge may still
            -- drive away from it.
            local bad, d = nil, 0
            while d <= c.length and bad == nil do
                local px, py, pp = Hitch.curvePoint(c, d)
                bad = outside(s, px, py, pp, math.min(D.planMarginM, d), D.vehicleClearanceM, D.playerClearanceM)
                d = d + D.sampleM
            end
            if bad == nil and s.staticCheck ~= nil then
                local blocked, names = s.staticCheck(c)
                if blocked == "UNKNOWN" then return false end
                if blocked ~= nil then
                    bad = "STATIC"
                    if c.length < whyLength then s.staticHits = names end
                end
            end
            if bad == nil then
                c.lineupAheadM = ahead
                best = c
                break
            end
            if c.length < whyLength then why, whyLength = PLAN_REASON[bad], c.length end
        end
        return true
    end
    for _, ahead in ipairs(D.lineupAheadM) do
        if not try(ahead) then return nil, "LOCAL_AREA_UNKNOWN" end
    end
    if best == nil and not try(D.lineupFallbackAheadM) then return nil, "LOCAL_AREA_UNKNOWN" end
    return best, why
end

-- The tractor's own minimum turning radius as Courseplay reads it
-- (AIUtil.lua:246-256: vehicle.maxTurningRadius, raised to GIANTS'
-- getAIMinTurningRadius() when that is known). nil if neither is readable.
local function turningRadius(tractor)
    local r = tractor.maxTurningRadius
    if not finite(r) or r <= 0 then r = nil end
    if type(tractor.getAIMinTurningRadius) == "function" then
        local ai = tractor:getAIMinTurningRadius()
        if finite(ai) and ai > 0 then r = math.max(r or 0, ai) end
    end
    return r
end

-- The tractor's footprint centre and yaw d metres along curve c (wagon frame -> world),
-- from its steering node by the size box offset in s.foot.
local function curvePose(s, c, d)
    local x, y, p = Hitch.curvePoint(c, d)
    local o, fx, fz = s.last, s.dirX, s.dirZ
    local sx, sz = o.wx + fx * x + fz * y, o.wz + fz * x - fx * y
    local hx, hz = math.cos(p) * fx + math.sin(p) * fz, math.cos(p) * fz - math.sin(p) * fx
    local f = s.foot
    return sx + hx * f.ahead + hz * f.side, sz + hz * f.ahead - hx * f.side, atan2(hx, hz)
end

-- GIANTS' collision query over the tractor's footprint d metres along curve c.
local function footprintAt(env, s, c, d)
    local D, f = Hitch.LOCAL, s.foot
    local x, z, yaw = curvePose(s, c, d)
    return boxQuery(env, s.rig, x, z, yaw, f.halfW + D.footMarginM, f.halfL, D.floorClearanceM,
                    f.height + D.topMarginM, s.masks.obstacles)
end

-- The tractor's size box relative to its steering node (GIANTS' vehicle size around its
-- rootNode, as AIDrivable:setAIRootNodeDirty 526-531 reads it), the collision masks, and
-- the query proven on it (its own body under VEHICLE, the terrain under TERRAIN). Errors
-- when any of it cannot be read or proven.
local function prepareFootprint(env, s)
    local tractor = s.tractor
    local size = tractor.size
    assert(finite(size.width) and size.width > 0 and finite(size.length) and size.length > 0
           and finite(size.height) and size.height > 0)
    assert(finite(size.widthOffset) and finite(size.lengthOffset) and finite(size.heightOffset))
    local node = tractor:getAISteeringNode()
    local cx, top, cz = env.localToWorld(tractor.rootNode, size.widthOffset, size.heightOffset + size.height,
                                         size.lengthOffset)
    assert(finite(cx) and finite(top) and finite(cz))
    local lx, _, lz = env.worldToLocal(node, cx, top, cz)
    assert(finite(lx) and finite(lz))
    s.foot = { ahead = lz, side = lx, halfW = size.width / 2, halfL = size.length / 2,
               height = top - terrainAt(env, cx, cz) }
    s.masks = collisionMasks(env)
    s.rig = select(2, rigUnits(tractor))
    local here = { x = s.last.x, y = s.last.y, p = s.last.p, r = 1, segs = {} }
    local x, z, yaw = curvePose(s, here, 0)
    local f = s.foot
    local body = boxQuery(env, s.rig, x, z, yaw, f.halfW * 0.8, f.halfL * 0.8, 0.1 * f.height, 0.9 * f.height,
                          s.masks.vehicles)
    local ground = boxQuery(env, s.rig, x, z, yaw, f.halfW * 0.8, f.halfL * 0.8, -0.5, 0.5, s.masks.terrain)
    assert(body ~= nil and body.own > 0 and ground ~= nil and ground.terrain > 0)
end

-- Fresh observation in the wagon frame: the steering node's pose (x, y, p), the
-- planned joint's (jx, jy), the wagon joint's world point and the wagon root.
local function observeLocal(env, s)
    local wx, wy, wz = world(env, s.wagonJoint.node)
    local rx, _, rz = world(env, s.wagon.rootNode)
    local node = s.tractor:getAISteeringNode()
    local sx, _, sz = world(env, node)
    local hx, _, hz = env.localDirectionToWorld(node, 0, 0, 1)
    local tx, _, tz = world(env, s.tractorJoint.jointTransform)
    assert(finite(hx) and finite(hz))
    local fx, fz = s.dirX, s.dirZ
    local function frame(px, pz) return (px - wx) * fx + (pz - wz) * fz, (px - wx) * fz - (pz - wz) * fx end
    local o = { wx = wx, wy = wy, wz = wz, rx = rx, rz = rz, node = node, tx = tx, tz = tz }
    o.x, o.y = frame(sx, sz)
    o.jx, o.jy = frame(tx, tz)
    o.p = atan2(hx * fz - hz * fx, hx * fx + hz * fz)
    return o
end

-- A point in the wagon frame of session `s`.
local function inFrame(s, px, pz)
    local o = s.last
    return (px - o.wx) * s.dirX + (pz - o.wz) * s.dirZ, (px - o.wx) * s.dirZ - (pz - o.wz) * s.dirX
end

-- Every other vehicle within the work radius plus watchExtraM of the wagon's joint
-- (GIANTS' vehicle list as FarmhandObserve reads it, FH Collectors.lua:323-324;
-- a vehicle attached to the pair has it as rootVehicle), and the player (as
-- FarmhandObserve playerPosition). Errors if any position is unreadable. A player seated
-- in a vehicle (its getIsEntered, AutoDriveUtilFuncs.lua:777, or GIANTS'
-- Player:getIsInVehicle) is no person on foot: nil (live
-- 71e0ec3bab26413c-1: seated in the 3650 itself, every maneuver was LOCAL_PATH_PERSON;
-- the player is an observer).
local function surroundings(env, s)
    local mission = env.g_currentMission
    local vehicles = (mission.vehicleSystem and mission.vehicleSystem.vehicles) or mission.vehicles
    assert(type(vehicles) == "table")
    local others, reach = {}, Hitch.LOCAL_LIMITS.workRadiusM + Hitch.LOCAL.watchExtraM
    local seated = false
    for _, v in pairs(vehicles) do
        if type(v.getIsEntered) == "function" and v:getIsEntered() == true then seated = true end
    end
    for _, v in pairs(vehicles) do
        local root = v.rootVehicle or v
        if v ~= s.tractor and v ~= s.wagon and root ~= s.tractor and root ~= s.wagon then
            local vx, _, vz = world(env, v.rootNode)
            local x, y = inFrame(s, vx, vz)
            if math.sqrt(x * x + y * y) <= reach then
                others[#others + 1] = { vehicle = v, x = x, y = y, wx = vx, wz = vz }
            end
        end
    end
    local player = env.g_localPlayer
    -- GIANTS' own Player:getIsInVehicle (Player.lua 479; PlayerStateDriving / Passenger 74-87).
    local inVehicle = type(player.getIsInVehicle) == "function" and player:getIsInVehicle()
    if inVehicle == true then seated = true end
    -- Diagnostics (the bridge logs them): what said the player was seated or not.
    s.seatedRead = string.format("player getIsInVehicle %s (%s), a vehicle entered %s",
                                 tostring(inVehicle), type(player.getIsInVehicle), tostring(seated))
    if seated then return others, nil end
    local px, _, pz
    if type(player.getPosition) == "function" then px, _, pz = player:getPosition()
    else px, _, pz = world(env, player.rootNode) end
    assert(finite(px) and finite(pz))
    local x, y = inFrame(s, px, pz)
    return others, { x = x, y = y }
end

-- Prepare the local maneuver for the planned joint pair. `args` as Hitch.newAlign
-- (tractor, wagon, tractorJoint, wagonJoint, farmId, isReserved, optional
-- maxSpeedKmh, timeoutMs). The tractor must be stopped, empty, free of any AI job
-- and within the work radius, and a curve must exist. Returns a session, or nil
-- and a reason (nothing has moved).
function Hitch.newManeuver(env, args)
    local L, D = Hitch.LIMITS, Hitch.DEFAULTS
    local speed = capped(args.maxSpeedKmh, D.maxSpeedKmh, L.maxSpeedKmh)
    local timeout = capped(args.timeoutMs, Hitch.LOCAL.timeoutMs, Hitch.LOCAL_LIMITS.timeoutMs)
    if speed == nil or speed < L.minSpeedKmh or timeout == nil or type(args.isReserved) ~= "function" then
        return nil, "INVALID_LIMITS"
    end
    local ok, session, reason, refused = pcall(function()
        local tractor, wagon = args.tractor, args.wagon
        local tractorJoint = tractor:getAttacherJoints()[args.tractorJoint]
        local wagonJoint = wagon:getInputAttacherJoints()[args.wagonJoint]
        if tractorJoint == nil or wagonJoint == nil then return nil, "UNKNOWN_JOINT" end
        if type(tractor.getAttacherJointByJointDescIndex) == "function"
                and tractor:getAttacherJointByJointDescIndex(args.tractorJoint) ~= tractorJoint then
            return nil, "UNKNOWN_JOINT"
        end
        if tractorJoint.jointType ~= wagonJoint.jointType then return nil, "JOINT_TYPE_MISMATCH" end
        if tractorJoint.jointIndex ~= 0 then return nil, "TRACTOR_JOINT_OCCUPIED" end
        local v = tractor:getLastSpeed()
        if not finite(v) or math.abs(v) >= 1 then return nil, "NOT_STOPPED" end
        local radius = turningRadius(tractor)
        if radius == nil then return nil, "TURN_RADIUS_UNKNOWN" end
        local fx, fz = Hitch.towingDir(env, wagon, wagonJoint.node)
        assert(finite(fx) and finite(fz))
        local s = {
            tractor = tractor, wagon = wagon, farmId = args.farmId, isReserved = args.isReserved,
            tractorJointIndex = args.tractorJoint, wagonJointIndex = args.wagonJoint,
            tractorJoint = tractorJoint, wagonJoint = wagonJoint, dirX = fx, dirZ = fz,
            maxSpeedKmh = speed, timeoutMs = timeout, elapsed = 0, progress = 0, bestProgress = 0,
            sinceProgress = 0, phase = nil, result = nil, observedRadius = radius,
            turnRadius = radius * Hitch.LOCAL.turnMargin, log = {}, others = {},
        }
        local o = observeLocal(env, s)
        s.wagonStartX, s.wagonStartZ, s.rootStartX, s.rootStartZ = o.wx, o.wz, o.rx, o.rz
        s.back = math.sqrt((o.x - o.jx) ^ 2 + (o.y - o.jy) ^ 2)
        local rootAhead = (o.rx - o.wx) * fx + (o.rz - o.wz) * fz
        s.bodyLength = math.max(Hitch.LOCAL.minBodyLengthM, 2 * math.abs(rootAhead) + 2)
        s.last = o
        local okAround, others, player = pcall(surroundings, env, s)
        if not okAround then return nil, "LOCAL_AREA_UNKNOWN", s end
        s.others, s.player = others, player
        if not pcall(prepareFootprint, env, s) then return nil, "LOCAL_AREA_UNKNOWN", s end
        s.staticCheck = function(c)
            local d = Hitch.LOCAL.footStepM / 2
            while d <= c.length do
                local q = footprintAt(env, s, c, d)
                if q == nil then return "UNKNOWN" end
                if #q.hits > 0 then return "HIT", string.format("%s at %.1f m", hitNames(env, q.hits), d) end
                d = d + Hitch.LOCAL.footStepM
            end
            return nil
        end
        Hitch.watchDriver(env, tractor)
        local denied = authority(s)
        if denied ~= nil then return nil, denied end
        if s.turnRadius < Hitch.LOCAL_LIMITS.minTurnRadiusM or s.turnRadius > Hitch.LOCAL_LIMITS.maxTurnRadiusM then
            return nil, "TURN_RADIUS_OUT_OF_RANGE", s
        end
        local start = outside(s, o.x, o.y, o.p, 0, Hitch.LOCAL.vehicleStopM,
                              Hitch.LOCAL.playerStopM)
        if start == "ENVELOPE_EXCEEDED" then return nil, "OUTSIDE_LOCAL_AREA", s end
        if start == "KEEP_OUT" then return nil, "LOCAL_START_AT_WAGON", s end
        if start ~= nil then return nil, PLAN_REASON[start], s end
        local curve, why = Hitch.chooseCurve(s, o.x, o.y, o.p)
        if curve == nil then return nil, why, s end
        s.curve = curve
        return s
    end)
    if not ok then return nil, "UNKNOWN_OBSERVATION" end
    -- Refused geometry: the half-built session only carries the numbers for the log.
    if refused ~= nil then
        if reason == "LOCAL_PATH_PERSON" and Hitch.onDiagnostic ~= nil then
            Hitch.onDiagnostic(string.format("%s; player at (%.1f, %.1f) in the wagon frame", tostring(refused.seatedRead),
                                             refused.player and refused.player.x or 0, refused.player and refused.player.y or 0))
        end
        Hitch.note(refused, "REFUSED " .. tostring(reason) .. (refused.staticHits and (" " .. refused.staticHits) or ""),
                   0, 0)
    end
    return session, reason
end

-- The phase for a distance along the curve: its first arc, straight, last arc (and on).
local function phaseAt(c, d)
    if d < c.segs[1][2] then return "LOCAL_TURN" end
    if d < c.segs[1][2] + c.segs[2][2] then return "LOCAL_APPROACH" end
    return "LOCAL_LINEUP"
end

-- One diagnostics line (kept on the session, printed by the bridge): the joints,
-- gap, errors in the wagon frame, the command and why.
-- Read-only: every output joint of `tractor` as the game has it now: type index,
-- jointIndex, the jointTransform's world point and its point in the AI root frame,
-- and GIANTS' load-time AI offset (AIDrivable attacherJointOffsets). Diagnostics only.
function Hitch.describeJoints(env, tractor)
    local parts = {}
    local ok = pcall(function()
        local root = tractor:getAIRootNode()
        local rx, _, rz = world(env, root)
        parts[1] = string.format("aiRoot=(%.2f, %.2f)", rx, rz)
        local offsets = tractor.spec_aiDrivable and tractor.spec_aiDrivable.attacherJointOffsets or {}
        for i, joint in ipairs(tractor:getAttacherJoints()) do
            local text = string.format("#%d type=%s used=%s", i, tostring(joint.jointType), tostring(joint.jointIndex))
            pcall(function()
                local wx, wy, wz = env.getWorldTranslation(joint.jointTransform)
                local lx, _, lz = env.worldToLocal(root, wx, wy, wz)
                text = text .. string.format(" world=(%.2f, %.2f) local=(%.2f, %.2f)", wx, wz, lx, lz)
            end)
            local o = offsets[i]
            if o ~= nil then text = text .. string.format(" offset=(%.2f, %.2f)", o.x or 0 / 0, o.z or 0 / 0) end
            parts[#parts + 1] = text
        end
    end)
    if not ok then parts[#parts + 1] = "unreadable" end
    return table.concat(parts, "; ")
end

function Hitch.note(s, reason, curvature, speed)
    local o = s.last
    if o == nil then return end
    local line = string.format(
        "%s %s t=%dms tractorJoint=(%.2f, %.2f) wagonJoint=(%.2f, %.2f) gap=%.2f ahead=%.2f lateral=%.2f "
        .. "headingDeg=%.1f steerCurvature=%.3f direction=forward speedKmh=%.1f along=%.1f/%s turnRadius=%.1f",
        tostring(s.phase or "LOCAL"), reason, s.elapsed, o.tx, o.tz, o.wx, o.wz,
        math.sqrt(o.jx ^ 2 + o.jy ^ 2), o.jx, o.jy, math.deg(o.p), curvature, speed, s.progress,
        s.curve and string.format("%.1f", s.curve.length) or "-", s.turnRadius or -1)
    table.insert(s.log, line)
    if #s.log > 20 then table.remove(s.log, 1) end
    if Hitch.onNote ~= nil then Hitch.onNote(line) end
end

-- Read-only steering diagnostics: where a steering command ends up at each GIANTS
-- layer, without changing anything. AIVehicleUtil.driveToPoint (27-97) turns the
-- target into getSteeringRotTimeByCurvature (Wheels 1950), clamps it to
-- min/maxRotTime and moves rotatedTime towards it by getAISteeringSpeed() per ms;
-- Wheels turns rotatedTime into each steered wheel's physics.steeringAngle (read
-- by GIANTS' dashboards, Wheels 644; wheels with physics.rotSpeed ~= 0 steer,
-- Wheels 1960), an articulated tractor into its axis rotation (ArticulatedAxis 277,
-- only with a componentJoint, 235-240). Every read is protected; unreadable is nil.
Hitch.STEER_SAMPLE_MS = 500
Hitch.STEER_FIRST_FRAMES = 5

local function readOr(f)
    local ok, value = pcall(f)
    if ok then return value end
    return nil
end

local function steerValue(v)
    if type(v) == "number" then
        if v ~= v or v == math.huge or v == -math.huge then return tostring(v) end
        if v % 1 == 0 and math.abs(v) < 1e9 then return string.format("%d", v) end
        return string.format("%.4f", v)
    end
    if type(v) == "boolean" then return tostring(v) end
    return "nil"
end

function Hitch.steeringState(env, tractor, curvature)
    local st = {}
    st.rotatedTime = readOr(function() return tractor.rotatedTime end)
    st.minRotTime = readOr(function() return tractor.minRotTime end)
    st.maxRotTime = readOr(function() return tractor.maxRotTime end)
    st.targetRotTime = readOr(function() return tractor:getSteeringRotTimeByCurvature(curvature) end)
    st.aiSteeringSpeed = readOr(function() return tractor:getAISteeringSpeed() end)
    st.wheelSteeringDuration = readOr(function() return tractor.wheelSteeringDuration end)
    st.autoRotateBackSpeed = readOr(function() return tractor.autoRotateBackSpeed end)
    st.maxTurningRadius = readOr(function() return tractor.maxTurningRadius end)
    st.reverser = readOr(function() return tractor:getReverserDirection() end)
    st.isServer = readOr(function() return tractor.isServer end)
    st.isActive = readOr(function() return tractor.isActive end)
    st.controlled = readOr(function() return tractor:getIsControlled() end)
    st.aiActive = readOr(function() return tractor:getIsAIActive() end)
    st.speedKmh = readOr(function() return tractor:getLastSpeed() end)
    local wheels = readOr(function() return tractor.spec_wheels.wheels end)
    if type(wheels) == "table" then
        local count, steerable, steered, angle = 0, 0, 0, nil
        for _, wheel in ipairs(wheels) do
            count = count + 1
            local rotSpeed = readOr(function() return wheel.physics.rotSpeed end)
            local a = readOr(function() return wheel.physics.steeringAngle end)
            if type(rotSpeed) == "number" and rotSpeed ~= 0 then steerable = steerable + 1 end
            if type(a) == "number" then
                if math.abs(a) > 1e-4 then steered = steered + 1 end
                if angle == nil or math.abs(a) > math.abs(angle) then angle = a end
            end
        end
        st.wheels, st.steerableWheels, st.steeredWheels = count, steerable, steered
        st.wheelAngleDeg = angle and math.deg(angle) or nil
    end
    local axis = readOr(function() return tractor.spec_articulatedAxis end)
    st.articulated = axis ~= nil
    if axis ~= nil then
        st.articulatedJoint = readOr(function() return axis.componentJoint ~= nil end)
        st.articulatedRotSpeed = readOr(function() return axis.rotSpeed end)
        st.articulatedRotMin = readOr(function() return axis.rotMin end)
        st.articulatedRotMax = readOr(function() return axis.rotMax end)
        st.articulatedInterpolated = readOr(function() return axis.interpolatedRotatedTime end)
    end
    local function yawOfNode(node)
        local dx, _, dz = env.localDirectionToWorld(node, 0, 0, 1)
        return math.deg(atan2(dx, dz))
    end
    st.steerNodeYawDeg = readOr(function() return yawOfNode(tractor:getAISteeringNode()) end)
    st.rootYawDeg = readOr(function() return yawOfNode(tractor.rootNode) end)
    return st
end

Hitch.STEER_FIELDS = { "rotatedTimePrevEnd", "rotatedTimeBefore", "rotatedTime", "targetRotTime", "minRotTime", "maxRotTime",
                       "aiSteeringSpeed", "wheelSteeringDuration", "autoRotateBackSpeed", "maxTurningRadius",
                       "wheels", "steerableWheels", "steeredWheels", "wheelAngleDeg", "articulated",
                       "articulatedJoint", "articulatedRotSpeed", "articulatedRotMin", "articulatedRotMax",
                       "articulatedInterpolated", "steerNodeYawDeg", "rootYawDeg", "reverser", "isServer",
                       "isActive", "controlled", "aiActive", "speedKmh" }

-- One STEER line: the command (curvature and its target in the steering node's
-- frame); rotatedTime right after the previous frame's driveToPoint (PrevEnd), at
-- the start of this frame (Before: anything else that wrote it in between shows as
-- a difference) and after this frame's driveToPoint; and the GIANTS steering state.
function Hitch.steerNote(env, s, curvature, lx, lz, before, prevEnd)
    local st = Hitch.steeringState(env, s.tractor, curvature)
    st.rotatedTimeBefore, st.rotatedTimePrevEnd = before, prevEnd
    local parts = { string.format("%s STEER t=%dms along=%.1f cmdCurvature=%s targetLocal=(%s, %s)",
        tostring(s.phase or "LOCAL"), s.elapsed, s.progress or 0, steerValue(curvature), steerValue(lx),
        steerValue(lz)) }
    for _, field in ipairs(Hitch.STEER_FIELDS) do
        parts[#parts + 1] = field .. "=" .. steerValue(st[field])
    end
    local line = table.concat(parts, " ")
    if Hitch.onSteer ~= nil then Hitch.onSteer(line) end
    return line
end

local function maneuverDrive(env, s, dt)
    local D = Hitch.LOCAL
    local denied = authority(s)
    if denied ~= nil then return denied end
    local seat = s.tractor:getIsEntered(); if seat ~= true and seat ~= false then return "UNKNOWN_OBSERVATION" end   -- a passenger rides; input is a takeover
    local o = observeLocal(env, s)
    s.last = o
    if distance(o.wx, o.wz, s.wagonStartX, s.wagonStartZ) > Hitch.DEFAULTS.wagonToleranceM
            or distance(o.rx, o.rz, s.rootStartX, s.rootStartZ) > Hitch.DEFAULTS.wagonToleranceM then
        return "WAGON_MOVED"
    end
    if s.elapsed > s.timeoutMs then return "TIMEOUT" end
    -- Nothing else may move near it, nor come close: no obstacle is ever pushed.
    for _, other in ipairs(s.others) do
        local vx, _, vz = world(env, other.vehicle.rootNode)
        if distance(vx, vz, other.wx, other.wz) > D.movedM then return "OTHER_VEHICLE_MOVED" end
    end
    local others, player = surroundings(env, s)
    s.others, s.player = others, player
    local bad = outside(s, o.x, o.y, o.p, 0, D.vehicleStopM, D.playerStopM)
    if bad == nil then
        -- The wagon's box with the braking margin.
        bad = outside(s, o.x, o.y, o.p, D.keepOutStopM, 0, 0)
        if bad == "ENVELOPE_EXCEEDED" then bad = nil end
    end
    if bad ~= nil then return bad end
    -- Lined up: GIANTS' reverse takes it from here.
    if math.abs(o.jy) <= D.doneLateralM and math.abs(math.deg(o.p)) <= D.doneHeadingDeg
            and o.jx >= D.doneMinAheadM and o.jx <= D.doneMaxAheadM then
        return "LINED_UP"
    end
    -- Past the curve's end, or about to leave the line-up window, the reverse's own
    -- envelope is enough.
    if (s.progress >= s.curve.length + D.endSlackM or o.jx >= D.doneMaxAheadM - 0.5) and o.jx > 0 and math.sqrt(o.jx ^ 2 + o.jy ^ 2) <= D.reverseReachM - 1
            and math.abs(o.jy) <= D.reverseLateralM and math.abs(math.deg(o.p)) <= D.reverseHeadingDeg then
        return "LINED_UP"
    end
    -- Progress: the nearest curve point a little behind to a little ahead of the last.
    local c, bestD, bestAt = s.curve, math.huge, s.progress
    local d = math.max(0, s.progress - D.searchBackM)
    while d <= s.progress + D.searchAheadM do
        local px, py = Hitch.curvePoint(c, d)
        local e = math.sqrt((px - o.x) ^ 2 + (py - o.y) ^ 2)
        if e < bestD then bestD, bestAt = e, d end
        d = d + D.searchStepM
    end
    s.progress, s.offCurve = math.max(s.progress, bestAt), bestD
    if bestD > D.divergeM then return "DIVERGED" end
    if s.progress > c.length + D.missedM then return "LINEUP_MISSED" end
    -- Where it is about to be: clear in GIANTS' collision query, or stop.
    local ahead = footprintAt(env, s, c, s.progress + D.aheadM)
    if ahead == nil then return "UNKNOWN_OBSERVATION" end
    if #ahead.hits > 0 then
        s.aheadHits = hitNames(env, ahead.hits)
        return "OBSTACLE_AHEAD"
    end
    if s.progress > s.bestProgress + D.stallProgressM then
        s.bestProgress, s.sinceProgress = s.progress, 0
    else
        s.sinceProgress = s.sinceProgress + dt
        if s.sinceProgress > D.stallMs then return "STALLED" end
    end

    local tractor = s.tractor
    if not tractor:getIsMotorStarted() then
        if not tractor:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
        tractor:startMotor()
    end
    Hitch.holdActive(s)          -- active, or GIANTS never turns the wheels
    tractor:raiseActive()
    -- Steer at the curve point LOOKAHEAD further on, in the steering node's frame
    -- (forward: CP CpAIWorker.lua:396-409 uses the steering node), at the wagon
    -- joint's height. GIANTS turns the wheels (driveToPoint 31-45).
    local gx, gy = Hitch.curvePoint(c, s.progress + D.lookaheadM)
    local wx = o.wx + s.dirX * gx + s.dirZ * gy
    local wz = o.wz + s.dirZ * gx - s.dirX * gy
    local lx, _, lz = env.worldToLocal(o.node, wx, o.wy, wz)
    local curvature = 2 * lx / math.max(lx * lx + lz * lz, 1e-6)
    local phase = phaseAt(c, s.progress)
    if phase ~= s.phase then
        local from = s.phase
        s.phase = phase
        Hitch.note(s, from == nil and "START" or ("FROM_" .. from), curvature, s.maxSpeedKmh)
    end
    s.lastCurvature = curvature
    local before = readOr(function() return tractor.rotatedTime end)
    local over = Hitch.drive(env, s, dt, true, lx, lz)
    if over ~= nil then return over end
    -- Diagnostics only: sampled, never steering anything.
    s.steerFrames = (s.steerFrames or 0) + 1
    if s.steerFrames <= Hitch.STEER_FIRST_FRAMES or s.elapsed >= (s.steerAt or 0) then
        s.steerAt = s.elapsed + Hitch.STEER_SAMPLE_MS
        pcall(Hitch.steerNote, env, s, curvature, lx, lz, before, s.rotatedTimeEnd)
    end
    s.rotatedTimeEnd = readOr(function() return tractor.rotatedTime end)
    return "CONTINUE"
end

-- One update of the local maneuver: "CONTINUE", "LINED_UP" (braked: the planned
-- joint is on the wagon's axis, 3-8 m ahead, facing its heading) or a failure:
-- RESERVATION_LOST, DRIVER_TOOK_OVER, AI_ACTIVE,
-- WAGON_ATTACHED, WAGON_MOVED, OTHER_VEHICLE_MOVED, OTHER_VEHICLE_NEAR,
-- PERSON_TOO_CLOSE, TIMEOUT, ENVELOPE_EXCEEDED, KEEP_OUT, DIVERGED,
-- LINEUP_MISSED, STALLED, MOTOR_UNAVAILABLE, UNKNOWN_OBSERVATION. Any result
-- other than CONTINUE is final (latched): later calls only brake.
function Hitch.maneuverStep(env, s, dt)
    if s.result ~= nil then
        Hitch.halt(env, s, dt)
        return s.result
    end
    s.elapsed = s.elapsed + dt
    local ok, code = pcall(maneuverDrive, env, s, dt)
    if not ok then code = "UNKNOWN_OBSERVATION" end
    if code ~= "CONTINUE" then
        s.result = code
        Hitch.halt(env, s, dt)
        Hitch.note(s, "END " .. code, s.lastCurvature or 0, 0)
    end
    return code
end

-- ------------------------------------------------------------- PULL_CLEAR
-- Live 23a8b1d881c7489b-4: next to an implement it has just unhitched, the tractor
-- cannot be moved by any GIANTS job: the loose implement is a navigation obstacle
-- touching it (AISystem:addObstacle; the agent collects only attached implements,
-- AIDrivable:createAgent 353-397), so GIANTS' agent stays BLOCKED even for a target
-- straight ahead. Authorized by the user 2026-10-19: straight after Farmhand's own
-- successful detach, Farmhand drives the tractor straight forward, with the same
-- GIANTS driveToPoint and the same safety model as LOCAL_MANEUVER, until the joints
-- are clearM apart. The implement is never made passable; GIANTS' collision
-- handling is not touched.
-- Straight moves 5 mph when their path is clear (the user, 2026-10-31: 3 km/h "sometimes too slow").
Hitch.PULL_LIMITS = { maxDistanceM = 8, maxSpeedKmh = 8, timeoutMs = 30000, maxClearM = 6 }
Hitch.PULL = {
    -- Joint separation that ends it (CLEAR): the companion's verified 4 m plus 0.5 m, as
    -- the two measures of a three-point joint differ (live 8d1f4ada61c14239-3: 4.01 m
    -- here from the joint's post-detach point, 3.70 m from GIANTS' load-time offsets).
    clearM = 4.5,
    timeoutMs = 20000,        -- 8 m at the governor's 1.95 km/h is 14.8 s
    lookaheadM = 3,
    divergeM = 0.5,           -- farther than this off the starting line: DIVERGED
    -- The separation may never shrink more than this: SEPARATION_LOST. The tractor settles
    -- or rolls back a few cm as its brake lets go (live a1d3a7852497400f-2-cd, b4d1db624d374aa4-1:
    -- 0.05 -> 0.00 m within the first 5 cm, joints 5 cm apart); backing into it is more.
    separationSlackM = 0.15,
    -- The unhitched implement is behind when its joint is no farther ahead than the
    -- tractor's own joint plus this (= companion/hitch.py FRONT_JOINT_SLACK_M) and its root
    -- is behind the steering node: a gooseneck ball sits over the steering axle (live
    -- 31365e0476674ed6-4, GMC 3500HD: 0.02 m), a drawbar 1-2 m behind it.
    jointSlackM = 0.5,
}

-- The tractor's checked points at `along` metres on its starting line (x ahead,
-- y lateral, in the start frame): its steering node and frontM ahead of it.
local function pullPoints(along, reverse)
    -- The end that leads: the front pulling forward, the rear backing straight off (a front implement).
    return { { along, 0 }, { along + (reverse and -Hitch.LOCAL.frontM or Hitch.LOCAL.frontM), 0 } }
end

-- nil, "OTHER_VEHICLE_NEAR" or "PERSON_TOO_CLOSE" for the tractor's points
-- `points` (start frame) against s.others / s.player.
-- ``from`` (optional): the points where the motion starts; a vehicle then counts only where the motion
-- brings it nearer than it was at the start (a machine backing off the trailer its header was just set
-- down on, its front over that trailer, moves only away from it).
local function pullConflict(s, points, vehicleM, playerM, from)
    local function nearest(list, other)
        local best = math.huge
        for _, pt in ipairs(list) do best = math.min(best, math.sqrt((pt[1] - other.x) ^ 2 + (pt[2] - other.y) ^ 2)) end
        return best
    end
    for _, pt in ipairs(points) do
        for _, other in ipairs(s.others) do
            local d = math.sqrt((pt[1] - other.x) ^ 2 + (pt[2] - other.y) ^ 2)
            if d < vehicleM and (from == nil or d < nearest(from, other) - 0.05) then return "OTHER_VEHICLE_NEAR" end
        end
        if s.player ~= nil and math.sqrt((pt[1] - s.player.x) ^ 2 + (pt[2] - s.player.y) ^ 2) < playerM then
            return "PERSON_TOO_CLOSE"
        end
    end
    return nil
end

-- The steering node's position in the start frame, and the joints' separation. The
-- tractor's joint is the point fixed to its body where the joint was at the start
-- (s.jointRight, s.jointAhead in the steering node's frame): live 4e9c443e19bd4de1-10
-- a three-point jointTransform rode the lower links back towards the seeder after
-- the detach (separation 0.19 -> 0.14 m while the tractor moved 0.07 m forward).
local function observePull(env, s)
    local node = s.tractor:getAISteeringNode()
    local sx, _, sz = world(env, node)
    local fx, _, fz = env.localDirectionToWorld(node, 0, 0, 1)
    assert(finite(fx) and finite(fz))
    -- local +x is world (fz, -fx) for the forward direction (fx, fz) (MathUtil yaw).
    local tx = sx + s.jointRight * fz + s.jointAhead * fx
    local tz = sz - s.jointRight * fx + s.jointAhead * fz
    local ix, _, iz = world(env, s.inputJoint.node)
    local rx, _, rz = world(env, s.wagon.rootNode)
    local x, y = inFrame(s, sx, sz)
    -- away: from where the implement stood at the start, so only the tractor's own
    -- motion counts (live 8d1f4ada61c14239-9: the seeder settled 6 cm towards it after
    -- the detach; its own motion is bounded by IMPLEMENT_MOVED).
    local away = s.implementX and distance(tx, tz, s.implementX, s.implementZ) or nil
    return { x = x, y = y, separation = distance(tx, tz, ix, iz), away = away, ix = ix, iz = iz, rx = rx, rz = rz }
end

-- Prepare the pull-clear. `args`: tractor, wagon (the implement just unhitched),
-- tractorJoint and wagonJoint (the joints it was on), farmId, isReserved; optional
-- maxDistanceM, maxSpeedKmh, timeoutMs, clearM (each capped by PULL_LIMITS). The
-- tractor must be stopped, empty and free of any AI job, the implement unattached
-- and behind it, and the straight line ahead clear of vehicles and the player.
-- Returns a session, or nil and a reason (nothing has moved).
function Hitch.newPullClear(env, args)
    local P, L = Hitch.PULL, Hitch.PULL_LIMITS
    local maxDistance = capped(args.maxDistanceM, L.maxDistanceM, L.maxDistanceM)
    local speed = capped(args.maxSpeedKmh, L.maxSpeedKmh, L.maxSpeedKmh)
    local timeout = capped(args.timeoutMs, P.timeoutMs, L.timeoutMs)
    local clear = capped(args.clearM, P.clearM, L.maxClearM)
    if maxDistance == nil or speed == nil or speed < Hitch.LIMITS.minSpeedKmh or timeout == nil or clear == nil
            or type(args.isReserved) ~= "function" then
        return nil, "INVALID_LIMITS"
    end
    local ok, session, reason = pcall(function()
        local tractor, wagon = args.tractor, args.wagon
        local tractorJoint = tractor:getAttacherJoints()[args.tractorJoint]
        local inputJoint = wagon:getInputAttacherJoints()[args.wagonJoint]
        if tractorJoint == nil or inputJoint == nil then return nil, "UNKNOWN_JOINT" end
        local v = tractor:getLastSpeed()
        if not finite(v) or math.abs(v) >= 1 then return nil, "NOT_STOPPED" end
        local node = tractor:getAISteeringNode()
        local sx, _, sz = world(env, node)
        local fx, _, fz = env.localDirectionToWorld(node, 0, 0, 1)
        assert(finite(fx) and finite(fz))
        local s = {
            tractor = tractor, wagon = wagon, farmId = args.farmId, isReserved = args.isReserved,
            tractorJoint = tractorJoint, inputJoint = inputJoint, dirX = fx, dirZ = fz,
            maxDistanceM = maxDistance, maxSpeedKmh = speed, timeoutMs = timeout, clearM = clear,
            elapsed = 0, sinceProgress = 0, result = nil, others = {},
            last = { wx = sx, wz = sz },        -- the start frame (inFrame): x ahead, y lateral
        }
        local jx, _, jz = world(env, tractorJoint.jointTransform)
        s.jointRight = (jx - sx) * fz - (jz - sz) * fx
        s.jointAhead = (jx - sx) * fx + (jz - sz) * fz
        local o = observePull(env, s)
        s.implementX, s.implementZ, s.rootX, s.rootZ = o.ix, o.iz, o.rx, o.rz
        s.bestSeparation, s.startSeparation = o.separation, o.separation      -- away == separation here
        -- Forward would drive into it: an implement hung in front (a combine's header) is backed straight
        -- off instead (live 2026-11-05: the MF 8570 left touching its header after the detach); one that is
        -- neither clearly behind nor clearly ahead is refused.
        local jointA, rootA = inFrame(s, o.ix, o.iz), inFrame(s, o.rx, o.rz)
        if jointA > s.jointAhead + P.jointSlackM or rootA >= 0 then
            if not (jointA >= s.jointAhead - P.jointSlackM and rootA > 0) then return nil, "PULL_CLEAR_NOT_BEHIND" end
            s.reverse = true
        end
        local okAround, others, player = pcall(surroundings, env, s)
        if not okAround then return nil, "LOCAL_AREA_UNKNOWN" end
        s.others, s.player = others, player
        Hitch.watchDriver(env, tractor)
        local denied = authority(s)
        if denied ~= nil then return nil, denied end
        -- The whole straight line, with the maneuver's planning clearances.
        local along = 0
        while along <= maxDistance do
            local bad = pullConflict(s, pullPoints(s.reverse and -along or along, s.reverse),
                                     Hitch.LOCAL.vehicleClearanceM, Hitch.LOCAL.playerClearanceM, pullPoints(0, s.reverse))
            if bad == "OTHER_VEHICLE_NEAR" then return nil, "PULL_CLEAR_BLOCKED" end
            if bad == "PERSON_TOO_CLOSE" then return nil, "PULL_CLEAR_PERSON" end
            along = along + Hitch.LOCAL.sampleM
        end
        if o.separation >= clear then return nil, "ALREADY_CLEAR" end
        return s
    end)
    if not ok then return nil, "UNKNOWN_OBSERVATION" end
    return session, reason
end

local function pullDrive(env, s, dt)
    local P, D = Hitch.PULL, Hitch.LOCAL
    local denied = authority(s)
    if denied ~= nil then return denied end
    local seat = s.tractor:getIsEntered(); if seat ~= true and seat ~= false then return "UNKNOWN_OBSERVATION" end   -- a passenger rides; input is a takeover
    local o = observePull(env, s)
    s.lastSeparation, s.lastX, s.lastY, s.lastAway = o.separation, o.x, o.y, o.away
    pcall(function() s.lastSpeed = s.tractor:getLastSpeed() end)
    if distance(o.ix, o.iz, s.implementX, s.implementZ) > Hitch.DEFAULTS.wagonToleranceM
            or distance(o.rx, o.rz, s.rootX, s.rootZ) > Hitch.DEFAULTS.wagonToleranceM then
        return "IMPLEMENT_MOVED"
    end
    if o.separation >= s.clearM then return "CLEAR" end
    if o.away < s.bestSeparation - P.separationSlackM then return "SEPARATION_LOST" end
    if s.elapsed > s.timeoutMs then return "TIMEOUT" end
    if (s.reverse and -o.x or o.x) > s.maxDistanceM then return "DISTANCE_LIMIT" end
    if math.abs(o.y) > P.divergeM then return "DIVERGED" end
    for _, other in ipairs(s.others) do
        local vx, _, vz = world(env, other.vehicle.rootNode)
        if distance(vx, vz, other.wx, other.wz) > D.movedM then return "OTHER_VEHICLE_MOVED" end
    end
    local others, player = surroundings(env, s)
    s.others, s.player = others, player
    local bad = pullConflict(s, pullPoints(o.x, s.reverse), D.vehicleStopM, D.playerStopM, pullPoints(0, s.reverse))
    if bad ~= nil then return bad end
    -- Progress is separation gained: a collision with anything unobserved stalls it.
    if o.away > s.bestSeparation + D.stallProgressM then
        s.bestSeparation, s.sinceProgress = o.away, 0
    else
        s.sinceProgress = s.sinceProgress + dt
        if s.sinceProgress > D.stallMs then return "STALLED" end
    end
    local tractor = s.tractor
    if not tractor:getIsMotorStarted() then
        if not tractor:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
        tractor:startMotor()
    end
    Hitch.holdActive(s)
    tractor:raiseActive()
    -- Steer at the starting line lookaheadM on (GIANTS driveToPoint, forward; backing off a front implement,
    -- the point behind it in reverse).
    local node = tractor:getAISteeringNode()
    local _, wy = env.getWorldTranslation(node)
    local ahead = o.x + (s.reverse and -P.lookaheadM or P.lookaheadM)
    local lx, _, lz = env.worldToLocal(node, s.last.wx + s.dirX * ahead, wy, s.last.wz + s.dirZ * ahead)
    local over = Hitch.drive(env, s, dt, not s.reverse, lx, lz)
    if over ~= nil then return over end
    return "CONTINUE"
end

-- One update of the pull-clear: "CONTINUE", "CLEAR" (braked: the joints are clearM
-- apart) or a failure: RESERVATION_LOST, DRIVER_TOOK_OVER,
-- AI_ACTIVE, WAGON_ATTACHED, IMPLEMENT_MOVED, SEPARATION_LOST, TIMEOUT, DISTANCE_LIMIT,
-- DIVERGED, OTHER_VEHICLE_MOVED, OTHER_VEHICLE_NEAR, PERSON_TOO_CLOSE, STALLED,
-- MOTOR_UNAVAILABLE, UNKNOWN_OBSERVATION. Any result other than CONTINUE is final
-- (latched): later calls only brake, and every end restores cruise control and
-- forceIsActive (Hitch.halt).
function Hitch.pullClearStep(env, s, dt)
    if s.result ~= nil then
        Hitch.halt(env, s, dt)
        return s.result
    end
    s.elapsed = s.elapsed + dt
    local ok, code = pcall(pullDrive, env, s, dt)
    if not ok then code = "UNKNOWN_OBSERVATION" end
    if code ~= "CONTINUE" then
        s.result = code
        Hitch.halt(env, s, dt)
    end
    return code
end

function Hitch.describePull(s)
    return string.format("separation=%.2f (start %.2f, clear %.1f) along=%s lateral=%s t=%dms",
        s.lastSeparation or s.startSeparation or -1, s.startSeparation or -1, s.clearM or -1,
        s.lastX and string.format("%.2f", s.lastX) or "?", s.lastY and string.format("%.2f", s.lastY) or "?",
        s.elapsed or 0) .. string.format(" away=%s best=%s speed=%s",
        s.lastAway and string.format("%.2f", s.lastAway) or "?",
        s.bestSeparation and string.format("%.2f", s.bestSeparation) or "?",
        s.lastSpeed and string.format("%.2f", s.lastSpeed) or "?")
end

-- ----------------------------------------------------------------- UNPARK
-- Live 53c77d1e809645b3-3 (FourFields save 2): the Super-Liner and its hopper inside the
-- Large Shed; GIANTS accepted a GOTO to a valid target 94 m away, its agent stayed
-- BLOCKED from the first frame and nothing moved: no GIANTS job drives a vehicle out of
-- a building. Authorized by the user 2026-10-20 (AGENTS.md exception 4), only after
-- GIANTS refused to start and no closed door explains it (the companion opens doors
-- first): straight forward, or straight back for a vehicle with nothing attached, at
-- most 20 m, 3 km/h and 40 s, to the nearest exit, steered by GIANTS' driveToPoint with
-- PULL_CLEAR's guards; then GIANTS' GOTO takes over once. Static geometry is checked
-- with GIANTS' own collision query, overlapBox (GDN engine Physics: synchronous, every
-- callback performed before it returns; the callback returns true to go on, as GIANTS'
-- TensionBelts:objectOverlapCallback does;
-- half extents and the yaw rotation as AutoDrive calls it, Specialization.lua:1909,
-- PathFinderModule.lua:2535), over the whole rig's swept volume: its length, width and
-- height from GIANTS' size boxes (AIDrivable:setAIRootNodeDirty 526-531 reads them),
-- the lateral margin either side, from just above the terrain (the supporting surface)
-- to above its top. Only the rig's own collision nodes and the terrain are excluded;
-- an unreadable or unproven query is never clear. Nothing is made passable.
-- 60 s: 20 m at the governor's 1.95 km/h is 37 s plus the 1.5 s soft start.
Hitch.UNPARK_LIMITS = { maxDistanceM = 20, maxSpeedKmh = 8, timeoutMs = 60000 }
Hitch.UNPARK = {
    lateralMarginM = 0.5,     -- either side of the rig over the whole swept volume
    floorClearanceM = 0.3,    -- boxes start this far above the terrain under them
    topMarginM = 0.3,         -- and end this far above the rig's highest point
    openMarginM = 1.5,        -- at the exit: the rig's footprint this much larger all round is clear
    roofCheckM = 8,           -- and nothing static over it up to this far above the ground
    reachAheadM = { 2, 5 },   -- and GIANTS' navigation map takes these points past its leading end
    stepM = 0.5,              -- the swept path is planned and checked in slabs this long
    footSlabM = 2,            -- footprint boxes at most this long (one terrain height each)
    minLookaheadM = 2,        -- while moving: the box past the leading end is at least this long,
    reactionS = 1, decelMs2 = 1, -- else speed * reaction + stopping distance + 0.5 m
    lookaheadM = 3,           -- steering target on the line
    divergeM = 0.5,           -- farther off the line: DIVERGED (PULL_CLEAR's)
    straightDeg = 10,         -- each attached unit within this of the tractor's heading
    rigTurnDeg = 3,           -- a unit turning this far relative to the tractor while moving: RIG_CHANGED
    exitSlackM = 0.25,
}
-- authority() for a vehicle with no partner: reserved, the farm's, nobody at the
-- controls, no AI of any kind, and not itself towed by something else.
local function unparkAuthority(s)
    local tractor = s.tractor
    if s.isReserved() ~= true or tractor.ownerFarmId ~= s.farmId then return "RESERVATION_LOST" end
    local conflict = Hitch.driverConflict(tractor)
    if conflict ~= nil then return conflict end
    local ai = tractor:getIsAIActive()
    if ai ~= false then return ai == true and "AI_ACTIVE" or "UNKNOWN_OBSERVATION" end
    if tractor.rootVehicle ~= nil and tractor.rootVehicle ~= tractor then return "NOT_ROOT_VEHICLE" end
    return nil
end

-- A point of the start frame (x ahead along the line, y lateral) in the world.
local function fromFrame(s, a, l)
    return s.ox + s.dirX * a + s.dirZ * l, s.oz + s.dirZ * a - s.dirX * l
end

-- GIANTS overlapBox over [a0, a1] x [l0, l1] of the start frame, h0..h1 above the terrain.
local function overlap(env, s, a0, a1, l0, l1, h0, h1, mask)
    local x, z = fromFrame(s, (a0 + a1) / 2, (l0 + l1) / 2)
    return boxQuery(env, s.rig, x, z, s.yaw, (l1 - l0) / 2, (a1 - a0) / 2, h0, h1, mask)
end

-- A unit's heading off the line, in degrees.
local function unitTurn(env, s, v)
    local x, _, z = env.localDirectionToWorld(v.rootNode, 0, 0, 1)
    assert(finite(x) and finite(z))
    return math.deg(atan2(x * s.dirZ - z * s.dirX, x * s.dirX + z * s.dirZ))
end

-- A unit's size box (GIANTS' vehicle size around its rootNode) in the start frame:
-- its a/l extent and its top above the terrain under it. Errors on unknown sizes.
local function unitBox(env, s, v)
    local size = v.size
    local w, l, h = size.width, size.length, size.height
    local wo, lo, ho = size.widthOffset, size.lengthOffset, size.heightOffset
    assert(finite(w) and w > 0 and finite(l) and l > 0 and finite(h) and h > 0)
    assert(finite(wo) and finite(lo) and finite(ho))
    local box = { aMin = math.huge, aMax = -math.huge, lMin = math.huge, lMax = -math.huge }
    for _, sx in ipairs({ -1, 1 }) do
        for _, sz in ipairs({ -1, 1 }) do
            local x, _, z = env.localToWorld(v.rootNode, wo + sx * w / 2, ho, lo + sz * l / 2)
            assert(finite(x) and finite(z))
            local a, b = inFrame(s, x, z)
            box.aMin, box.aMax = math.min(box.aMin, a), math.max(box.aMax, a)
            box.lMin, box.lMax = math.min(box.lMin, b), math.max(box.lMax, b)
        end
    end
    local cx, top, cz = env.localToWorld(v.rootNode, wo, ho + h, lo)
    assert(finite(cx) and finite(top) and finite(cz))
    box.height = top - terrainAt(env, cx, cz)
    return box
end

-- A conservative box fixed to the drive vehicle for a proposed route. It includes
-- every attached unit and every object held by its tension belts. This is a
-- planning envelope: the live envelope still checks each unit at its actual pose.
function Hitch.routeFootprint(env, tractor)
    local ok, footprint = pcall(function()
        local units, excluded = rigUnits(tractor)
        local initial = #units
        for index = 1, initial do
            local belts = units[index].spec_tensionBelts
            local held = {}
            for _, data in pairs(belts ~= nil and belts.objectsToJoint or {}) do held[#held + 1] = data end
            -- What a carrier holds by GIANTS' DynamicMountAttacher (a header locked on its trailer)
            -- moves with the rig as belted cargo does.
            local mounts = units[index].spec_dynamicMountAttacher
            for object in pairs(mounts ~= nil and mounts.dynamicMountedObjects or {}) do
                if type(object) == "table" and object.rootNode ~= nil then held[#held + 1] = { object = object } end
            end
            for _, data in ipairs(held) do
                if data.object ~= nil and not excluded[data.object] then
                    -- Belt cargo may itself carry implements; they move with the rig too.
                    local carried = rigUnits(data.object)
                    for _, unit in ipairs(carried) do
                        if not excluded[unit] then
                            assert(#units < Hitch.MAX_RIG_UNITS)
                            excluded[unit] = true
                            units[#units + 1] = unit
                        end
                    end
                end
            end
        end
        local x, _, z = world(env, tractor.rootNode)
        local fx, _, fz = env.localDirectionToWorld(tractor.rootNode, 0, 0, 1)
        assert(finite(fx) and finite(fz))
        local towLength, towed, towMeasured = 0, nil, false
        for index = 2, initial do
            local unit = units[index]
            local ux, _, uz = world(env, unit.rootNode)
            -- Mounted on a three-point joint (GIANTS AttacherJoints.JOINTTYPE_IMPLEMENT): it turns with
            -- the tractor, wheels or not (live 123c4e0630984ad6-1: the Titan Vineyard Plow's depth
            -- wheel made the Series 9 an articulated rig; no turning round in place, 5.5 min of
            -- wrong-way reverse escapes). Semi-mounted and trailed units still articulate.
            local mounted = readOr(function()
                local joint = unit:getActiveInputAttacherJoint()
                return joint ~= nil and env.AttacherJoints ~= nil
                    and joint.jointType == env.AttacherJoints.JOINTTYPE_IMPLEMENT
            end) == true
            if not mounted and (ux - x) * fx + (uz - z) * fz < -0.5 then
                local node = unit.steeringAxleNode or unit.rootNode
                local ax, _, az = world(env, node)
                towLength = math.max(towLength, math.sqrt((ax - x) ^ 2 + (az - z) ^ 2))
                -- The first wheeled unit behind is the one a reverse must keep in line.
                if towed == nil and #(readOr(function() return unit.spec_wheels.wheels end) or {}) > 0 then
                    towed = unit
                end
            end
        end
        if towed ~= nil and type(towed.getActiveInputAttacherJoint) == "function" then
            local joint = readOr(function() return towed:getActiveInputAttacherJoint() end)
            local wheels = readOr(function() return towed.spec_wheels.wheels end) or {}
            if joint ~= nil and joint.node ~= nil and #wheels >= 2 then
                local okJoint, jx, _, jz = pcall(world, env, joint.node)
                local ax, az, count = 0, 0, 0
                for _, wheel in ipairs(wheels) do
                    if wheel.repr ~= nil then
                        local okWheel, wx, _, wz = pcall(world, env, wheel.repr)
                        if okWheel and finite(wx) and finite(wz) then
                            ax, az, count = ax + wx, az + wz, count + 1
                        end
                    end
                end
                if okJoint and finite(jx) and finite(jz) and count >= 2 then
                    ax, az = ax / count, az / count
                    local measured = math.sqrt((jx - ax) ^ 2 + (jz - az) ^ 2)
                    if measured > 0.5 then towLength, towMeasured = measured, true end
                end
            end
        end
        local hitchLimit = math.rad(35)
        if towed ~= nil and type(towed.getActiveInputAttacherJoint) == "function" then
            local joint = readOr(function() return towed:getActiveInputAttacherJoint() end)
            if joint ~= nil and env.AttacherJoints ~= nil
                    and (joint.jointType == env.AttacherJoints.JOINTTYPE_SEMITRAILERCAR
                         or joint.jointType == env.AttacherJoints.JOINTTYPE_SEMITRAILER) then
                hitchLimit = math.rad(90)
            end
        end
        local frame = { last = { wx = x, wz = z }, dirX = fx, dirZ = fz }
        local aMin, aMax, lMin, lMax, height = math.huge, -math.huge, math.huge, -math.huge, 0
        local unitRects = {}
        local startYaw = atan2(fx, fz)
        for _, unit in ipairs(units) do
            local box = unitBox(env, frame, unit)
            -- A unit behind at an angle to the tractor (a trailer left jackknifed, its
            -- cargo) is planned straight behind at its observed distance: the route
            -- straightens it, and the live envelope checks every unit where it is.
            local ux, _, uz = world(env, unit.rootNode)
            local ufx, _, ufz = env.localDirectionToWorld(unit.rootNode, 0, 0, 1)
            local ul, fl = math.sqrt(ufx * ufx + ufz * ufz), math.sqrt(fx * fx + fz * fz)
            assert(finite(ul) and ul > 0 and fl > 0)
            ufx, ufz = ufx / ul, ufz / ul
            if unit ~= tractor and (ux - x) * fx + (uz - z) * fz < -0.5
                    and (fx * ufx + fz * ufz) / fl < math.cos(math.rad(5)) then
                box = unitBox(env, { last = { wx = ux, wz = uz }, dirX = ufx, dirZ = ufz }, unit)
                local back = math.sqrt((ux - x) ^ 2 + (uz - z) ^ 2)
                box.aMin, box.aMax = box.aMin - back, box.aMax - back
            end
            aMin, aMax = math.min(aMin, box.aMin), math.max(aMax, box.aMax)
            lMin, lMax = math.min(lMin, box.lMin), math.max(lMax, box.lMax)
            height = math.max(height, box.height)
            local dx, dz = ux - x, uz - z
            unitRects[#unitRects + 1] = {
                ahead = dx * math.sin(startYaw) + dz * math.cos(startYaw),
                side = dx * math.cos(startYaw) - dz * math.sin(startYaw),
                yaw = atan2(ufx, ufz) - startYaw,
                size = unit.size,
                height = box.height,
            }
        end
        assert(height > 0 and aMax > aMin and lMax > lMin)
        return { tractor = tractor, units = units, excluded = excluded, masks = collisionMasks(env),
                 towLength = towLength, towed = towed, towMeasured = towMeasured,
                 hitchLimit = hitchLimit,
                 aMin = aMin, aMax = aMax, lMin = lMin, lMax = lMax, height = height,
                 unitRects = unitRects }
    end)
    if not ok or footprint == nil then return nil end
    -- The articulated route search's rig (Drive.plan): nil where it cannot be measured.
    local modelled, model = pcall(Hitch.rigModel, env, footprint)
    footprint.model = modelled and model or nil
    return footprint
end

-- Read-only inventory for the geometry experiment. A rigid-body AABB is a
-- measurement of loaded physics, not an oriented clearance footprint: an
-- angled body can still leave empty space inside this world-axis box.
function Hitch.physicalInventory(env, tractor)
    local foot = Hitch.routeFootprint(env, tractor)
    if foot == nil then return nil end
    local out = {}
    for _, unit in ipairs(foot.units) do
        local entry = { unit = unit, rootNode = unit.rootNode, bodies = {}, shapes = {}, size = unit.size }
        local seen = {}
        local function visit(node, depth)
            if node == nil or seen[node] or depth > 32 then return end
            seen[node] = true
            local collides = nil
            if type(env.getHasCollision) == "function" then
                local okCollision, value = pcall(env.getHasCollision, node)
                if okCollision then collides = value end
            end
            if env.ClassIds ~= nil and type(env.getHasClassId) == "function"
                    and type(env.getShapeBoundingSphere) == "function" then
                local okShape, shape = pcall(env.getHasClassId, node, env.ClassIds.SHAPE)
                if okShape and shape and collides == true then
                    local okSphere, sx, sy, sz, radius, geometry = pcall(env.getShapeBoundingSphere, node)
                    if okSphere and finite(sx) and finite(sy) and finite(sz)
                            and finite(radius) and radius > 0 then
                        local group = nil
                        if type(env.getCollisionFilterGroup) == "function" then
                            local okGroup, value = pcall(env.getCollisionFilterGroup, node)
                            if okGroup then group = value end
                        end
                        local wx, wy, wz = env.localToWorld(node, sx, sy, sz)
                        entry.shapes[#entry.shapes + 1] = {
                            node = node, localX = sx, localY = sy, localZ = sz,
                            worldX = wx, worldY = wy, worldZ = wz,
                            radius = radius, usesGeometry = geometry, hasCollision = collides,
                            group = group,
                        }
                    end
                end
            end
            if collides ~= false and type(env.getRigidBodyAABB) == "function" then
                local ok, minX, maxX, minY, maxY, minZ, maxZ = pcall(env.getRigidBodyAABB, node)
                if ok and finite(minX) and finite(maxX) and finite(minY) and finite(maxY)
                        and finite(minZ) and finite(maxZ) then
                    entry.bodies[#entry.bodies + 1] = {
                        node = node, minX = minX, maxX = maxX, minY = minY, maxY = maxY,
                        minZ = minZ, maxZ = maxZ,
                    }
                end
            end
            if type(env.getNumOfChildren) == "function" and type(env.getChildAt) == "function" then
                local ok, count = pcall(env.getNumOfChildren, node)
                if ok and type(count) == "number" then
                    for i = 0, math.min(count, 128) - 1 do
                        local childOk, child = pcall(env.getChildAt, node, i)
                        if childOk then visit(child, depth + 1) end
                    end
                end
            end
        end
        for _, component in ipairs(unit.components or {}) do visit(component.node, 0) end
        out[#out + 1] = entry
    end
    return out
end

-- Laboratory predictor: propagate an observed truck/root displacement through
-- one measured hitch and wheel group. The caller supplies the truck endpoint;
-- this does not predict steering, contact, or an independently articulated
-- second trailer. A short chord is split so reverse uses its actual direction.
function Hitch.predictFromRootPose(env, foot, endX, endZ, endYaw)
    if foot == nil or foot.tractor == nil or foot.towed == nil or foot.towMeasured ~= true
            or not (finite(endX) and finite(endZ) and finite(endYaw))
            or not (finite(foot.towLength) and foot.towLength > 0.5) then return nil end
    local truck, trailer = foot.tractor, foot.towed
    local belted = {}
    for _, unit in ipairs(foot.units) do
        local belts = unit.spec_tensionBelts
        for _, data in pairs(belts ~= nil and belts.objectsToJoint or {}) do
            if data.object ~= nil then belted[data.object] = true end
        end
    end
    for _, unit in ipairs(foot.units) do
        if unit ~= truck and unit ~= trailer and not belted[unit]
                and type(unit.getActiveInputAttacherJoint) == "function"
                and #(readOr(function() return unit.spec_wheels.wheels end) or {}) >= 2 then
            return nil -- another articulated wheel group needs another measured segment
        end
    end
    if type(trailer.getActiveInputAttacherJoint) ~= "function" then return nil end
    local joint = readOr(function() return trailer:getActiveInputAttacherJoint() end)
    if joint == nil or joint.node == nil then return nil end
    local tx, _, tz = world(env, truck.rootNode)
    if (endX - tx)^2 + (endZ - tz)^2 > 25 then return nil end
    local fx, _, fz = env.localDirectionToWorld(truck.rootNode, 0, 0, 1)
    local startYaw = atan2(fx, fz)
    local hx, _, hz = world(env, joint.node)
    local rx, _, rz = world(env, trailer.rootNode)
    local rfx, _, rfz = env.localDirectionToWorld(trailer.rootNode, 0, 0, 1)
    local trailerYaw = atan2(rfx, rfz)
    local function offset(ox, oz, yaw, x, z)
        local dx, dz = x - ox, z - oz
        return dx * math.cos(yaw) - dz * math.sin(yaw),
               dx * math.sin(yaw) + dz * math.cos(yaw)
    end
    local function at(x, z, yaw, side, ahead)
        return x + math.cos(yaw) * side + math.sin(yaw) * ahead,
               z - math.sin(yaw) * side + math.cos(yaw) * ahead
    end
    local truckSide, truckAhead = offset(tx, tz, startYaw, hx, hz)
    local hitchSide, hitchAhead = offset(rx, rz, trailerYaw, hx, hz)
    local steps = math.max(1, math.ceil(math.sqrt((endX - tx)^2 + (endZ - tz)^2) / 0.25))
    local lastHx, lastHz = hx, hz
    for i = 1, steps do
        local share = i / steps
        local x, z = tx + (endX - tx) * share, tz + (endZ - tz) * share
        local yaw = startYaw + (endYaw - startYaw) * share
        local nextHx, nextHz = at(x, z, yaw, truckSide, truckAhead)
        local lateral = (nextHx - lastHx) * math.cos(trailerYaw)
                      - (nextHz - lastHz) * math.sin(trailerYaw)
        trailerYaw = trailerYaw + lateral / foot.towLength
        lastHx, lastHz = nextHx, nextHz
    end
    local trailerX, trailerZ = at(lastHx, lastHz, trailerYaw, -hitchSide, -hitchAhead)
    local poses = { { unit = truck, x = endX, z = endZ, yaw = endYaw },
                    { unit = trailer, x = trailerX, z = trailerZ, yaw = trailerYaw } }
    for _, unit in ipairs(foot.units) do
        if unit ~= truck and unit ~= trailer then
            local ux, _, uz = world(env, unit.rootNode)
            local ufx, _, ufz = env.localDirectionToWorld(unit.rootNode, 0, 0, 1)
            local side, ahead = offset(rx, rz, atan2(rfx, rfz), ux, uz)
            local px, pz = at(trailerX, trailerZ, trailerYaw, side, ahead)
            poses[#poses + 1] = { unit = unit, x = px, z = pz,
                                  yaw = trailerYaw + atan2(ufx, ufz) - atan2(rfx, rfz),
                                  rigidFollower = true }
        end
    end
    return poses
end

-- The measured geometry of a one-hitch articulated rig, read once from the live rig (the
-- same hitch, wheel group and offsets as predictFromRootPose), for the route search to carry
-- the trailer's heading as part of its state (Drive.trailerStep, Hitch.modelPoses). nil where
-- predictFromRootPose would refuse (unmeasured tow, a second articulated wheel group).
function Hitch.rigModel(env, foot)
    if foot == nil or foot.tractor == nil or foot.towed == nil or foot.towMeasured ~= true
            or not (finite(foot.towLength) and foot.towLength > 0.5) then return nil end
    local truck, trailer = foot.tractor, foot.towed
    local tx, _, tz = world(env, truck.rootNode)
    if Hitch.predictFromRootPose(env, foot, tx, tz, 0) == nil then return nil end
    local joint = readOr(function() return trailer:getActiveInputAttacherJoint() end)
    if joint == nil or joint.node == nil then return nil end
    local fx, _, fz = env.localDirectionToWorld(truck.rootNode, 0, 0, 1)
    local truckYaw = atan2(fx, fz)
    local hx, _, hz = world(env, joint.node)
    local rx, _, rz = world(env, trailer.rootNode)
    local rfx, _, rfz = env.localDirectionToWorld(trailer.rootNode, 0, 0, 1)
    local trailerYaw = atan2(rfx, rfz)
    local function offset(ox, oz, yaw, x, z)
        local dx, dz = x - ox, z - oz
        return dx * math.cos(yaw) - dz * math.sin(yaw), dx * math.sin(yaw) + dz * math.cos(yaw)
    end
    local model = { truck = truck, trailer = trailer, towLength = foot.towLength, trailerYaw = trailerYaw,
                    followers = {} }
    model.truckSide, model.truckAhead = offset(tx, tz, truckYaw, hx, hz)
    model.hitchSide, model.hitchAhead = offset(rx, rz, trailerYaw, hx, hz)
    for _, unit in ipairs(foot.units) do
        if unit ~= truck and unit ~= trailer then
            local ux, _, uz = world(env, unit.rootNode)
            local ufx, _, ufz = env.localDirectionToWorld(unit.rootNode, 0, 0, 1)
            local side, ahead = offset(rx, rz, trailerYaw, ux, uz)
            model.followers[#model.followers + 1] = { unit = unit, side = side, ahead = ahead,
                                                      yaw = atan2(ufx, ufz) - trailerYaw }
        end
    end
    return model
end

-- The unit poses of the modelled rig with the truck's root at x, z, yaw and the trailer at
-- trailerYaw (predictFromRootPose's frames).
function Hitch.modelPoses(model, x, z, yaw, trailerYaw)
    local function at(px, pz, pyaw, side, ahead)
        return px + math.cos(pyaw) * side + math.sin(pyaw) * ahead,
               pz - math.sin(pyaw) * side + math.cos(pyaw) * ahead
    end
    local hx, hz = at(x, z, yaw, model.truckSide, model.truckAhead)
    local trailerX, trailerZ = at(hx, hz, trailerYaw, -model.hitchSide, -model.hitchAhead)
    local poses = { { unit = model.truck, x = x, z = z, yaw = yaw },
                    { unit = model.trailer, x = trailerX, z = trailerZ, yaw = trailerYaw } }
    for _, f in ipairs(model.followers) do
        local px, pz = at(trailerX, trailerZ, trailerYaw, f.side, f.ahead)
        poses[#poses + 1] = { unit = f.unit, x = px, z = pz, yaw = trailerYaw + f.yaw, rigidFollower = true }
    end
    return poses
end

-- A commanded short arc starts at the powered vehicle's observed heading.
-- Each sample propagates the articulation from that same observation; the
-- measured trailer length is the average of the exposed wheel representations,
-- not a verified active wheel-group centre.
function Hitch.predictShortMotion(env, foot, length, curvature)
    if foot == nil or foot.tractor == nil or not finite(length) or not finite(curvature)
            or math.abs(length) > 3 or math.abs(curvature) > 0.3 then return nil end
    local x, _, z = world(env, foot.tractor.rootNode)
    local fx, _, fz = env.localDirectionToWorld(foot.tractor.rootNode, 0, 0, 1)
    local yaw = atan2(fx, fz)
    local samples = {}
    for i = 1, math.max(1, math.ceil(math.abs(length) / 0.25)) do
        local d = length * i / math.max(1, math.ceil(math.abs(length) / 0.25))
        local eyaw = yaw + d * curvature
        local ex, ez
        if math.abs(curvature) < 0.00001 then
            ex, ez = x + math.sin(yaw) * d, z + math.cos(yaw) * d
        else
            ex = x + (math.cos(yaw) - math.cos(eyaw)) / curvature
            ez = z + (math.sin(eyaw) - math.sin(yaw)) / curvature
        end
        local poses = Hitch.predictFromRootPose(env, foot, ex, ez, eyaw)
        if poses == nil then return nil end
        samples[#samples + 1] = { distance = d, poses = poses }
    end
    return samples
end

-- Local experimental clearance of the predicted connected units. Unlike
-- routePoseClear, no empty whole-rig rectangle can veto this motion.
-- One predicted unit pose's own box (size + 0.5 m) against the obstacles: its hits, or nil.
local function unitPoseHits(env, foot, pose)
    local size = pose.unit.size
    if size == nil or not (finite(size.width) and finite(size.length)) then return nil, "UNIT_SIZE_UNREADABLE" end
    local bx = pose.x + math.sin(pose.yaw) * (size.lengthOffset or 0)
               + math.cos(pose.yaw) * (size.widthOffset or 0)
    local bz = pose.z + math.cos(pose.yaw) * (size.lengthOffset or 0)
               - math.sin(pose.yaw) * (size.widthOffset or 0)
    local query = boxQuery(env, foot.excluded, bx, bz, pose.yaw,
        size.width / 2 + 0.5, size.length / 2 + 0.5,
        0.3, (size.height or foot.height) + 0.5,
        foot.masks.obstacles, terrainAt(env, bx, bz))
    if query == nil then return nil, "COLLISION_UNREADABLE" end
    return query.hits
end

-- What each unit's own box touches where the rig stands now: per unit index, a set of nodes.
function Hitch.unitStartHits(env, foot)
    local x, _, z = world(env, foot.tractor.rootNode)
    local fx, _, fz = env.localDirectionToWorld(foot.tractor.rootNode, 0, 0, 1)
    local poses = Hitch.predictFromRootPose(env, foot, x, z, atan2(fx, fz))
    if poses == nil then return nil end
    local out = {}
    for index, pose in ipairs(poses) do
        local hits = unitPoseHits(env, foot, pose)
        if hits == nil then return nil end
        out[index] = {}
        for _, node in ipairs(hits) do out[index][node] = true end
    end
    return out
end

function Hitch.predictedUnitsClear(env, foot, samples)
    if foot == nil or samples == nil then return nil, "PREDICTION_UNREADABLE" end
    -- Reversing off what a unit touched at the start (Drive.begin: foot.leaving.unitHits), that
    -- unit's own contact is no veto inside the leaving radius (live 732546fb86914a8c-1: the
    -- loaded GMC's nose against scenery, every 0.5 m reverse refused by that same contact).
    local leaving = foot.leaving
    local away = leaving ~= nil and leaving.unitHits or nil
    local ok, clear, reason, hits = pcall(function()
        for _, sample in ipairs(samples) do
            for index, pose in ipairs(sample.poses) do
                local found, why = unitPoseHits(env, foot, pose)
                if found == nil then return nil, why end
                if away ~= nil and away[index] ~= nil and sample.distance < 0
                        and math.sqrt((sample.poses[1].x - leaving.x) ^ 2 + (sample.poses[1].z - leaving.z) ^ 2)
                            < leaving.radius then
                    local kept = {}
                    for _, node in ipairs(found) do
                        if not away[index][node] then kept[#kept + 1] = node end
                    end
                    found = kept
                end
                if #found > 0 then
                    return false, string.format("unit%d@%.2fm OBSTACLE %s", index,
                        sample.distance, hitNames(env, found)), found
                end
            end
        end
        return true
    end)
    if not ok then return nil, "QUERY_ERROR " .. tostring(clear) end
    return clear, reason, hits
end

-- true only when GIANTS' overlapBox can read the entire proposed rig pose,
-- including the supporting terrain. Buildings, vehicles, trees and objects all
-- block; neither the rig nor its belted cargo blocks itself. Nothing is passable.
function Hitch.routePoseClear(env, foot, x, z, yaw, curvature, reversing)
    if foot == nil or not (finite(x) and finite(z) and finite(yaw)) then return nil end
    local ok, clear, reason, hits = pcall(function()
        local side, ahead = (foot.lMin + foot.lMax) / 2, (foot.aMin + foot.aMax) / 2
        local halfW = (foot.lMax - foot.lMin) / 2 + 0.5
        if foot.towLength ~= nil and foot.towLength > 0 and finite(curvature) and math.abs(curvature) > 0.0001 then
            -- A towed unit cuts to the inside of the turn only (positive curvature turns
            -- to local +x): the box grows by its off-tracking on that side alone. Widened
            -- both ways, no turn fitted a lane under ~12 m (live 83e039589c2743dc-2).
            local radius = 1 / math.abs(curvature)
            local inside = math.sqrt(math.max(0, radius * radius - foot.towLength * foot.towLength))
            local offtrack = math.min(foot.towLength, radius - inside)
            if reversing then
                -- The trailer's observed articulation can shift its swept
                -- volume to either side while backing around a bend.
                halfW = halfW + offtrack
            else
                halfW = halfW + offtrack / 2
                side = side + (curvature > 0 and 1 or -1) * offtrack / 2
            end
        end
        local cx = x + math.sin(yaw) * ahead + math.cos(yaw) * side
        local cz = z + math.cos(yaw) * ahead - math.sin(yaw) * side
        local halfL = (foot.aMax - foot.aMin) / 2 + 0.5
        -- Where this drive was held fast by something no query saw (Drive: a stall).
        for _, point in ipairs(foot.held or {}) do
            local dx, dz = point[1] - cx, point[2] - cz
            if math.abs(dx * math.sin(yaw) + dz * math.cos(yaw)) <= halfL
                    and math.abs(dx * math.cos(yaw) - dz * math.sin(yaw)) <= halfW then
                return false, string.format("HELD_FAST(%.0f,%.0f)", point[1], point[2])
            end
        end
        local mission = env.g_currentMission
        local samples, decks = {}, {}
        for _, lateral in ipairs({ -halfW, 0, halfW }) do
            for _, longitudinal in ipairs({ -halfL, 0, halfL }) do
                local px = cx + math.sin(yaw) * longitudinal + math.cos(yaw) * lateral
                local pz = cz + math.cos(yaw) * longitudinal - math.sin(yaw) * lateral
                local groundY, deck = surfaceAt(env, px, pz)
                if not deck then
                    local waterY = mission.environmentAreaSystem:getWaterYAtWorldPosition(px, groundY, pz)
                    if waterY ~= nil and (not finite(waterY) or waterY > groundY - 0.5) then return false, "WATER" end
                end
                samples[#samples + 1] = groundY
                decks[#samples] = deck
            end
        end
        -- Adjacent samples at the front, middle and rear. A steep or unreadable
        -- grade is not a proven route for an arbitrary rig.
        local maxRise = math.tan(math.rad(22)) * halfL
        for column = 1, 3 do
            if math.abs(samples[column * 3] - samples[column * 3 - 1]) > maxRise
                    or math.abs(samples[column * 3 - 1] - samples[column * 3 - 2]) > maxRise then
                -- Third: what was read, rear/mid/front of the line (a deck marked d), and the limit.
                local read = {}
                for i = column * 3 - 2, column * 3 do
                    read[#read + 1] = string.format("%.1f%s", samples[i], decks[i] and "d" or "")
                end
                return false, "GRADE", string.format("%s>%.1f", table.concat(read, "/"), maxRise)
            end
        end
        -- The centre sample; on a deck the ray that read it is the support (no terrain there).
        local base, deck = samples[5], decks[5]
        -- Leaving what it started against (Drive.begin): that object left out near the start.
        local excluded = foot.excluded
        local leaving = foot.leaving
        if leaving ~= nil and math.sqrt((x - leaving.x) ^ 2 + (z - leaving.z) ^ 2) < leaving.radius then
            excluded = leaving.excluded
        end
        local body
        local departing = leaving ~= nil and math.sqrt((x - leaving.x) ^ 2 + (z - leaving.z) ^ 2) < leaving.radius
        if foot.unitRects ~= nil and (departing or foot.towed == nil) then
            -- The rig's one bounding rectangle contains empty space beside its units. A rig with
            -- no towed unit moves as one body, so each observed unit is probed at its place in
            -- it (live 57022e28117b42d8-1: the Series 9 and its mounted plow, the rectangle
            -- touching a tree, every unit's own box clear, every search NO_ROUTE); a towed rig
            -- so only for a short departure (a bend moves its units; Drive.routeProbe models
            -- them where it can). The whole-rig terrain/grade checks above stay.
            body = { hits = {} }
            for _, rect in ipairs(foot.unitRects) do
                local rx = x + math.sin(yaw) * rect.ahead + math.cos(yaw) * rect.side
                local rz = z + math.cos(yaw) * rect.ahead - math.sin(yaw) * rect.side
                local uyaw, size = yaw + rect.yaw, rect.size
                local bx = rx + math.sin(uyaw) * size.lengthOffset + math.cos(uyaw) * size.widthOffset
                local bz = rz + math.cos(uyaw) * size.lengthOffset - math.sin(uyaw) * size.widthOffset
                local margin = foot.departureNudge and 0.05 or 0.5
                local one = boxQuery(env, excluded, bx, bz, uyaw, size.width / 2 + margin,
                                     size.length / 2 + margin, 0.3, rect.height + 0.5,
                                     foot.masks.obstacles, terrainAt(env, bx, bz))
                if one == nil then return nil, "COLLISION_UNREADABLE" end
                for _, hit in ipairs(one.hits) do body.hits[#body.hits + 1] = hit end
            end
        else
            body = boxQuery(env, excluded, cx, cz, yaw, halfW, halfL, 0.3,
                            foot.height + 0.5, foot.masks.obstacles, base)
        end
        local ground = boxQuery(env, excluded, cx, cz, yaw, halfW * 0.8, halfL * 0.8,
                                -0.5, 0.5, foot.masks.terrain, base)
        if body == nil or ground == nil or (ground.terrain == 0 and not deck) then
            return nil, "COLLISION_UNREADABLE"
        end
        if #body.hits > 0 then return false, "OBSTACLE " .. hitNames(env, body.hits), body.hits end
        return true, nil
    end)
    if not ok then return nil, "QUERY_ERROR " .. tostring(clear) end
    return clear, reason, hits
end

-- Installed FS25 FSDensityMapUtil reads FieldDensityMap.GROUND_TYPE at a world
-- point. Painted yard and road ground has no field ground type; cultivated and
-- sown land does. Fruit is a stronger reason to keep transport on the lane.
-- This is a route preference, never a collision or a refusal of field work.
function Hitch.routeSurface(env, x, z)
    local util = env.FSDensityMapUtil
    if util == nil or type(util.getFieldDataAtWorldPosition) ~= "function" then return nil end
    local ok, field = pcall(util.getFieldDataAtWorldPosition, x, 0, z)
    if not ok or field ~= true then return nil end
    if type(util.getFruitTypeIndexAtWorldPos) == "function" then
        local fruitOk, index = pcall(util.getFruitTypeIndexAtWorldPos, x, z)
        if fruitOk and index ~= nil then
            local manager = env.g_fruitTypeManager
            local desc = manager ~= nil and manager:getFruitTypeByIndex(index) or nil
            local name = desc ~= nil and type(desc.name) == "string" and string.upper(desc.name) or ""
            if name ~= "GRASS" and name ~= "MEADOW" then return "CROP" end
        end
    end
    return "FIELD"
end

-- Diagnostics: the surface (surfaceAt) along a line from x, z at yaw, every metre for
-- `metres`, at each lateral offset: "lateral: h h h ...", a deck marked d.
function Hitch.surfaceScan(env, x, z, yaw, metres, laterals)
    local ok, out = pcall(function()
        local lines = {}
        for _, lateral in ipairs(laterals) do
            local read = {}
            for d = 0, metres do
                local px = x + math.sin(yaw) * d + math.cos(yaw) * lateral
                local pz = z + math.cos(yaw) * d - math.sin(yaw) * lateral
                local y, deck = surfaceAt(env, px, pz)
                read[#read + 1] = string.format("%.1f%s", y, deck and "d" or "")
            end
            lines[#lines + 1] = string.format("%+.0f: %s", lateral, table.concat(read, " "))
        end
        return table.concat(lines, " | ")
    end)
    return ok and out or nil
end

-- Measurement only (no route is refused on it yet): the terrain under the rig's route box
-- at x, z, yaw, sampled every 2 m along its centre line and both sides: the whole box's
-- grade, the steepest 2 m climb and the worst side tilt (degrees), the largest height
-- step between neighbouring samples (m), and the rig's root pitch and roll as GIANTS
-- has it (localDirectionToWorld). The 22 degree GRADE check compares points ~half the
-- rig apart, so a bank or ditch edge shorter than that is not seen (live 7fd526481ca24c1c-3).
function Hitch.terrainProfile(env, foot, x, z, yaw, rootNode)
    if foot == nil or not (finite(x) and finite(z) and finite(yaw)) then return nil end
    local ok, out = pcall(function()
        local side, ahead = (foot.lMin + foot.lMax) / 2, (foot.aMin + foot.aMax) / 2
        local cx = x + math.sin(yaw) * ahead + math.cos(yaw) * side
        local cz = z + math.cos(yaw) * ahead - math.sin(yaw) * side
        local halfW, halfL = (foot.lMax - foot.lMin) / 2, (foot.aMax - foot.aMin) / 2
        local n = math.max(1, math.floor(halfL))
        local lines = {}
        for li, lateral in ipairs({ -halfW, 0, halfW }) do
            lines[li] = {}
            for i = -n, n do
                local longitudinal = halfL * i / n
                local px = cx + math.sin(yaw) * longitudinal + math.cos(yaw) * lateral
                local pz = cz + math.cos(yaw) * longitudinal - math.sin(yaw) * lateral
                lines[li][#lines[li] + 1] = terrainAt(env, px, pz)
            end
        end
        local gap = halfL / n
        local climb, cross, step = 0, 0, 0
        for _, line in ipairs(lines) do
            for i = 2, #line do
                local rise = math.abs(line[i] - line[i - 1])
                step = math.max(step, rise)
                climb = math.max(climb, math.deg(math.atan(rise / gap)))
            end
        end
        for i = 1, #lines[1] do
            local rise = math.abs(lines[3][i] - lines[1][i])
            step = math.max(step, math.abs(lines[2][i] - lines[1][i]), math.abs(lines[3][i] - lines[2][i]))
            if halfW > 0 then cross = math.max(cross, math.deg(math.atan(rise / (2 * halfW)))) end
        end
        local mid = lines[2]
        local grade = math.deg(math.atan((mid[#mid] - mid[1]) / (2 * halfL)))
        local pitch, roll
        if rootNode ~= nil then
            local _, fy = env.localDirectionToWorld(rootNode, 0, 0, 1)
            local _, sy = env.localDirectionToWorld(rootNode, 1, 0, 0)
            if finite(fy) and finite(sy) then
                pitch = math.deg(math.asin(math.max(-1, math.min(1, fy))))
                roll = math.deg(math.asin(math.max(-1, math.min(1, sy))))
            end
        end
        return { grade = grade, climb = climb, cross = cross, step = step, pitch = pitch, roll = roll }
    end)
    return ok and out or nil
end

-- nil if [a0, a1] (any order) is clear for the rig's width with `side` margin and its height,
-- else "HIT" and the names, or "UNKNOWN". Slabs of at most `slab` metres.
local function clearVolume(env, s, a0, a1, side, slab, h0, h1, mask)
    if a0 > a1 then a0, a1 = a1, a0 end
    local a = a0
    while a < a1 - 1e-6 do
        local b = math.min(a1, a + slab)
        local q = overlap(env, s, a, b, s.lMin - side, s.lMax + side, h0, h1, mask)
        if q == nil then return "UNKNOWN" end
        if #q.hits > 0 then return "HIT", hitNames(env, q.hits) end
        a = b
    end
    return nil
end

-- Whether the rig moved `d` metres along the line (negative: back) stands in the open:
-- nothing static over it, its footprint with openMarginM all round clear, and GIANTS'
-- navigation map takes the ground past its leading end (AIParameterPosition:validate
-- 83-90, aiSystem:getIsPositionReachable). true, false and why, or nil (unknown).
local function openAt(env, s, d)
    local U = Hitch.UNPARK
    local roof = overlap(env, s, s.aMin + d, s.aMax + d, s.lMin, s.lMax, s.height + U.topMarginM,
                         math.max(U.roofCheckM, s.height + U.topMarginM + 2), s.masks.static)
    if roof == nil then return nil end
    if #roof.hits > 0 then return false, "ROOF " .. hitNames(env, roof.hits) end
    local m = U.openMarginM
    local bad, names = clearVolume(env, s, s.aMin + d - m, s.aMax + d + m, m, U.footSlabM, U.floorClearanceM,
                                   s.height + U.topMarginM, s.masks.obstacles)
    if bad == "UNKNOWN" then return nil end
    if bad ~= nil then return false, "NEAR " .. names end
    local sign = d < 0 and -1 or 1
    local lead = sign > 0 and s.aMax or s.aMin
    for _, ahead in ipairs(U.reachAheadM) do
        local x, z = fromFrame(s, lead + d + sign * ahead, (s.lMin + s.lMax) / 2)
        local reachable = env.g_currentMission.aiSystem:getIsPositionReachable(x, 0, z)
        if reachable ~= true then return reachable == false and false or nil, "NOT_REACHABLE" end
    end
    return true
end

-- The points PULL_CLEAR's vehicle and player guards watch, `d` along the line: the
-- steering node and the middle of the rig's leading end.
local function unparkPoints(s, d)
    return { { d, 0 }, { s.lead + d, (s.lMin + s.lMax) / 2 } }
end

-- PULL_CLEAR's guards for the rig moved `d` along the line, with one difference (the user,
-- 2026-10-20, after live bc8307516e994d85-1: the PROLANDER's root 5.6 m beside the 7R in
-- the Open Hall): another vehicle's root counts only once it is ahead of the rig's leading
-- end in the direction of travel. One beside or behind is checked by its body (the swept
-- volume's collision query with the margin, again every frame) and stops it if it moves.
-- The player counts on every side. nil, "OTHER_VEHICLE_NEAR" or "PERSON_TOO_CLOSE".
local function unparkConflict(s, d, vehicleM, playerM)
    local front = s.lead + d
    local ahead = { others = {}, player = s.player }
    for _, other in ipairs(s.others) do
        if s.sign * (other.x - front) > 0 then ahead.others[#ahead.others + 1] = other end
    end
    return pullConflict(ahead, unparkPoints(s, d), vehicleM, playerM)
end

-- One direction (sign 1 forward, -1 back): the nearest exit along the line, or why there
-- is none. Every slab from the rig's far end to the exit is clear with the lateral margin.
local function planUnpark(env, s, sign)
    local U, L = Hitch.UNPARK, Hitch.UNPARK_LIMITS
    s.sign = sign
    s.lead = sign > 0 and s.aMax or s.aMin
    local top = s.height + U.topMarginM
    -- Beside the rig where it stands: the margin it will pass through.
    local bad, names = clearVolume(env, s, s.aMin, s.aMax, U.lateralMarginM, U.stepM, U.floorClearanceM, top,
                                   s.masks.obstacles)
    if bad == "UNKNOWN" then return { why = "UNPARK_QUERY_UNKNOWN" } end
    if bad ~= nil then return { why = "UNPARK_TOO_TIGHT", at = 0, names = names } end
    local d = 0
    while d < L.maxDistanceM - 1e-6 do
        local nd = math.min(L.maxDistanceM, d + U.stepM)
        bad, names = clearVolume(env, s, s.lead + sign * d, s.lead + sign * nd, U.lateralMarginM, U.stepM,
                                 U.floorClearanceM, top, s.masks.obstacles)
        if bad == "UNKNOWN" then return { why = "UNPARK_QUERY_UNKNOWN" } end
        if bad ~= nil then return { why = "UNPARK_EXIT_BLOCKED", at = nd, names = names } end
        local conflict = unparkConflict(s, sign * nd, Hitch.LOCAL.vehicleClearanceM, Hitch.LOCAL.playerClearanceM)
        if conflict == "OTHER_VEHICLE_NEAR" then return { why = "UNPARK_BLOCKED", at = nd } end
        if conflict == "PERSON_TOO_CLOSE" then return { why = "UNPARK_PERSON", at = nd } end
        local open = openAt(env, s, sign * nd)
        if open == nil then return { why = "UNPARK_QUERY_UNKNOWN" } end
        if open then return { exit = nd } end
        d = nd
    end
    return { why = "UNPARK_EXIT_TOO_FAR" }
end

local function describePlan(p)
    if p == nil then return "-" end
    if p.exit then return string.format("exit %.1f m", p.exit) end
    return p.why .. (p.at and string.format(" at %.1f m", p.at) or "") .. (p.names and (" (" .. p.names .. ")") or "")
end

-- Prepare the extraction. `args`: tractor, farmId, isReserved; optional maxDistanceM,
-- maxSpeedKmh, timeoutMs (each capped by UNPARK_LIMITS). Refused before any motion
-- unless the tractor is empty, at rest and free of AI, every unit's geometry is known,
-- GIANTS' collision query is proven on this rig (it reports the rig's own body and the
-- terrain), nothing but the rig and the ground touches it where it stands, it is not
-- already in the open, and a straight line leads out within the caps. Returns a session,
-- or nil, a reason and (for the log) the half-built session.
function Hitch.newUnpark(env, args)
    local U, L = Hitch.UNPARK, Hitch.UNPARK_LIMITS
    local maxDistance = capped(args.maxDistanceM, L.maxDistanceM, L.maxDistanceM)
    local speed = capped(args.maxSpeedKmh, L.maxSpeedKmh, L.maxSpeedKmh)
    local timeout = capped(args.timeoutMs, L.timeoutMs, L.timeoutMs)
    if maxDistance == nil or speed == nil or speed < Hitch.LIMITS.minSpeedKmh or timeout == nil
            or type(args.isReserved) ~= "function" then
        return nil, "INVALID_LIMITS"
    end
    local s = { tractor = args.tractor, farmId = args.farmId, isReserved = args.isReserved, maxDistanceM = maxDistance,
                maxSpeedKmh = speed, timeoutMs = timeout, elapsed = 0, sinceProgress = 0, best = 0, result = nil,
                others = {}, plans = {} }
    local ok, reason = pcall(function()
        local tractor = s.tractor
        local v = tractor:getLastSpeed()
        if not finite(v) or math.abs(v) >= 1 then return "NOT_STOPPED" end
        Hitch.watchDriver(env, tractor)
        local denied = unparkAuthority(s)
        if denied ~= nil then return denied end
        -- Articulated steering only with a componentJoint (ArticulatedAxis onPostLoad 233-241;
        -- live 758c6fbfdbf94125-2: the 7R and the X8 carry the spec without one).
        local axis = tractor.spec_articulatedAxis
        if axis ~= nil and axis.componentJoint ~= nil then return "UNPARK_ARTICULATED_UNSUPPORTED" end
        local node = tractor:getAISteeringNode()
        local ox, _, oz = world(env, node)
        local fx, _, fz = env.localDirectionToWorld(node, 0, 0, 1)
        assert(finite(fx) and finite(fz))
        s.ox, s.oz, s.dirX, s.dirZ = ox, oz, fx, fz
        s.yaw = atan2(s.dirX, s.dirZ)
        s.last = { wx = s.ox, wz = s.oz }   -- the start frame for surroundings
        local okUnits, units, set = pcall(rigUnits, tractor)
        if not okUnits then return "UNPARK_GEOMETRY_UNKNOWN" end
        s.units, s.rig = units, set
        s.aMin, s.aMax, s.lMin, s.lMax, s.height = math.huge, -math.huge, math.huge, -math.huge, 0
        s.turns, s.boxes = {}, {}
        for i, unit in ipairs(units) do
            local okBox, box = pcall(unitBox, env, s, unit)
            if not okBox then return "UNPARK_GEOMETRY_UNKNOWN" end
            s.aMin, s.aMax = math.min(s.aMin, box.aMin), math.max(s.aMax, box.aMax)
            s.lMin, s.lMax = math.min(s.lMin, box.lMin), math.max(s.lMax, box.lMax)
            s.height = math.max(s.height, box.height)
            s.turns[i] = unitTurn(env, s, unit)
            if i > 1 and math.abs(s.turns[i] - s.turns[1]) > U.straightDeg then return "UNPARK_RIG_NOT_STRAIGHT" end
            s.boxes[i] = box
        end
        local okMasks, masks = pcall(collisionMasks, env)
        if not okMasks then return "UNPARK_QUERY_UNKNOWN" end
        s.masks = masks
        -- The query proves itself on this rig first: GIANTS reports the tractor's own
        -- body (vehicle mask) and the terrain under it (terrain mask).
        local b = s.boxes[1]
        local da, dl = (b.aMax - b.aMin) * 0.1, (b.lMax - b.lMin) * 0.1
        local body = overlap(env, s, b.aMin + da, b.aMax - da, b.lMin + dl, b.lMax - dl, 0.1 * b.height,
                             0.9 * b.height, masks.vehicles)
        local ground = overlap(env, s, b.aMin + da, b.aMax - da, b.lMin + dl, b.lMax - dl, -0.5, 0.5, masks.terrain)
        if body == nil or body.own < 1 or ground == nil or ground.terrain < 1 then return "UNPARK_QUERY_UNVERIFIED" end
        -- Where it stands, nothing but the rig and the ground (a raised floor, cargo or a
        -- wall it touches all show here).
        for _, box in ipairs(s.boxes) do
            local q = overlap(env, s, box.aMin, box.aMax, box.lMin, box.lMax, U.floorClearanceM, box.height,
                              masks.obstacles)
            if q == nil then return "UNPARK_QUERY_UNKNOWN" end
            if #q.hits > 0 then
                s.startHits = hitNames(env, q.hits)
                return "UNPARK_START_NOT_CLEAR"
            end
        end
        local okAround, others, player = pcall(surroundings, env, s)
        if not okAround then return "LOCAL_AREA_UNKNOWN" end
        s.others, s.player = others, player
        s.sign = 1
        local open, why = openAt(env, s, 0)
        if open == nil then return "UNPARK_QUERY_UNKNOWN" end
        s.startOpen = why
        if open then return "UNPARK_NOT_BOXED" end
        -- Forward always; back only with nothing attached (no towed unit behind, and no
        -- attachment geometry to reverse with).
        s.plans.forward = planUnpark(env, s, 1)
        s.plans.back = #units == 1 and planUnpark(env, s, -1) or { why = "UNPARK_REVERSE_NOT_ALLOWED" }
        local f, r = s.plans.forward, s.plans.back
        local choice
        if f.exit and (not r.exit or f.exit <= r.exit) then choice = 1
        elseif r.exit then choice = -1 end
        if choice == nil then return f.why end            -- both directions are in the log
        s.sign = choice
        s.lead = choice > 0 and s.aMax or s.aMin
        s.exitM = choice > 0 and f.exit or r.exit
        if s.exitM > maxDistance then return "UNPARK_EXIT_TOO_FAR" end
        return nil
    end)
    if not ok then return nil, "UNKNOWN_OBSERVATION", s end
    if reason ~= nil then return nil, reason, s end
    return s
end

-- The steering node's place on the line, its lateral offset and the leading end.
local function observeUnpark(env, s)
    local node = s.tractor:getAISteeringNode()
    local x, _, z = world(env, node)
    local along, lateral = inFrame(s, x, z)
    return along, lateral, distance(x, z, s.ox, s.oz)
end

local function unparkDrive(env, s, dt)
    local U = Hitch.UNPARK
    local denied = unparkAuthority(s)
    if denied ~= nil then return denied end
    local seat = s.tractor:getIsEntered(); if seat ~= true and seat ~= false then return "UNKNOWN_OBSERVATION" end   -- a passenger rides; input is a takeover
    local along, lateral, moved = observeUnpark(env, s)
    local progress = s.sign * along
    s.lastAlong, s.lastLateral = along, lateral
    pcall(function() s.lastSpeed = s.tractor:getLastSpeed() end)
    -- The rig must stay the rig it planned with: the same units, none turning.
    local units = rigUnits(s.tractor)
    if #units ~= #s.units then return "RIG_CHANGED" end
    for i, unit in ipairs(units) do
        if unit ~= s.units[i] then return "RIG_CHANGED" end
        if i > 1 and math.abs((unitTurn(env, s, unit) - unitTurn(env, s, units[1])) - (s.turns[i] - s.turns[1]))
                > U.rigTurnDeg then
            return "RIG_CHANGED"
        end
    end
    if math.abs(lateral) > U.divergeM then return "DIVERGED" end
    if moved > s.maxDistanceM or progress > s.maxDistanceM then return "DISTANCE_LIMIT" end
    if s.elapsed > s.timeoutMs then return "TIMEOUT" end
    for _, other in ipairs(s.others) do
        local vx, _, vz = world(env, other.vehicle.rootNode)
        if distance(vx, vz, other.wx, other.wz) > Hitch.LOCAL.movedM then return "OTHER_VEHICLE_MOVED" end
    end
    local others, player = surroundings(env, s)
    s.others, s.player = others, player
    local bad = unparkConflict(s, along, Hitch.LOCAL.vehicleStopM, Hitch.LOCAL.playerStopM)
    if bad ~= nil then return bad end
    if progress >= s.exitM then
        -- At the exit: confirmed in the open from fresh queries, else nothing more.
        local open, why = openAt(env, s, along)
        s.exitWhy = why
        if open == nil then return "UNKNOWN_OBSERVATION" end
        return open and "CLEAR" or "EXIT_NOT_CONFIRMED"
    end
    -- Past the leading end: at least the stopping distance at this speed, never under 2 m.
    local v = math.abs(s.lastSpeed or 0) / 3.6
    local reach = math.max(U.minLookaheadM, v * U.reactionS + v * v / (2 * U.decelMs2) + 0.5)
    local front = s.lead + along
    local hit, names = clearVolume(env, s, front, front + s.sign * reach, U.lateralMarginM, reach, U.floorClearanceM,
                                   s.height + U.topMarginM, s.masks.obstacles)
    if hit == "UNKNOWN" then return "UNKNOWN_OBSERVATION" end
    if hit ~= nil then s.aheadHits = names; return "OBSTACLE_AHEAD" end
    -- Progress along the line: a collision with anything unseen stalls it.
    if progress > s.best + Hitch.LOCAL.stallProgressM then
        s.best, s.sinceProgress = progress, 0
    else
        s.sinceProgress = s.sinceProgress + dt
        if s.sinceProgress > Hitch.LOCAL.stallMs then return "STALLED" end
    end
    local tractor = s.tractor
    if not tractor:getIsMotorStarted() then
        if not tractor:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
        tractor:startMotor()
    end
    Hitch.holdActive(s)
    tractor:raiseActive()
    -- Steer at the line lookaheadM further on (GIANTS driveToPoint); back in the reverser
    -- node, else the steering node, as ALIGN_REVERSE.
    local node = s.sign > 0 and tractor:getAISteeringNode() or (tractor:getAIReverserNode() or tractor:getAISteeringNode())
    local _, wy = env.getWorldTranslation(node)
    local gx, gz = fromFrame(s, along + s.sign * U.lookaheadM, 0)
    local lx, _, lz = env.worldToLocal(node, gx, wy, gz)
    local over = Hitch.drive(env, s, dt, s.sign > 0, lx, lz)
    if over ~= nil then return over end
    return "CONTINUE"
end

-- One update of the extraction: "CONTINUE", "CLEAR" (braked at the exit, in the open) or a
-- failure: RESERVATION_LOST, DRIVER_TOOK_OVER, AI_ACTIVE,
-- RIG_CHANGED, DIVERGED, DISTANCE_LIMIT, TIMEOUT, OTHER_VEHICLE_MOVED, OTHER_VEHICLE_NEAR,
-- PERSON_TOO_CLOSE, OBSTACLE_AHEAD, EXIT_NOT_CONFIRMED, STALLED, MOTOR_UNAVAILABLE,
-- UNKNOWN_OBSERVATION. Any result other than CONTINUE is final (latched): later calls
-- only brake, and every end restores cruise control and forceIsActive (Hitch.halt).
function Hitch.unparkStep(env, s, dt)
    if s.result ~= nil then
        Hitch.halt(env, s, dt)
        return s.result
    end
    s.elapsed = s.elapsed + dt
    local ok, code = pcall(unparkDrive, env, s, dt)
    if not ok then code = "UNKNOWN_OBSERVATION" end
    if code ~= "CONTINUE" then
        s.result = code
        Hitch.halt(env, s, dt)
    end
    return code
end

-- One line for the log: the rig, each direction's plan, and where it stands now.
function Hitch.describeUnpark(s)
    if s == nil then return "-" end
    local function f(n) return n and string.format("%.2f", n) or "?" end
    return string.format("rig units=%s length=%s width=%s height=%s start=%s forward=%s back=%s direction=%s "
        .. "exit=%s along=%s lateral=%s speed=%s t=%dms startHits=%s aheadHits=%s exitWhy=%s",
        s.units and #s.units or "?", s.aMax and f(s.aMax - s.aMin), s.lMax and f(s.lMax - s.lMin), f(s.height),
        tostring(s.startOpen or "boxed"), describePlan(s.plans and s.plans.forward),
        describePlan(s.plans and s.plans.back), s.exitM and (s.sign > 0 and "forward" or "back") or "-",
        f(s.exitM), f(s.lastAlong), f(s.lastLateral), f(s.lastSpeed), s.elapsed or 0, tostring(s.startHits or "-"),
        tostring(s.aheadHits or "-"), tostring(s.exitWhy or "-"))
end

-- ------------------------------------------------------------- LOAD / UNLOAD
-- AGENTS.md exception 5 (the user, 2026-10-20, for tractor transport: no GIANTS job or
-- installed mod drives a vehicle onto a deck; Universal Autoload moves pallets, bales
-- and bags only). LOAD: the tractor alone, straight along the transport trailer's axis
-- up its rear ramps (verified down through GIANTS' Foldable: fold time 1, at rest), at
-- most 3 km/h and at most the ramps plus the deck; it stops, braked, with its body inside
-- the deck box, which is GIANTS' own cargo area: the span of the trailer's tension belts
-- (TensionBelts sortedBelts start/end nodes, as getObjectToMount 1100-1193 builds the box
-- a belt holds), half a belt spacing longer at each end. UNLOAD: belts released, ramps
-- down, straight back off the ramps. Everything else is UNPARK's: the start frame (here
-- the trailer's), GIANTS' collision query proven on the tractor (own body, terrain) and on
-- the trailer (seen as the transport, excluded), the swept volume clear of everything but
-- the tractor and the transport trailer (the truck towing it is not excluded) from just
-- above the terrain to the tractor's top on the deck, PULL_CLEAR's guards, the line
-- followed by GIANTS' driveToPoint, and every exit restoring the vehicle-control state.
-- Nothing is teleported, no collision is disabled, and the truck and trailer must not move.
-- 90 s: up to 12 m of run-in, 3 m of ramps and a 15 m deck at the governor's 1.95 km/h is 55 s.
Hitch.LOAD_LIMITS = { maxSpeedKmh = 4.8, timeoutMs = 90000, rampM = 3 }
Hitch.LOAD = {
    rampM = 3,                -- the ramps on the ground: the tractor's leading end at most this behind the trailer
    lateralStartM = 0.5,      -- the steering node this close to the trailer's axis at the start
    headingStartDeg = 7,      -- small steering offset at the ramps; swept path and divergence still guard contact
    divergeM = 0.3,           -- while moving: farther off the axis is DIVERGED (the ramps)
    lookaheadM = 3,
    lateralMarginM = 0.5, floorClearanceM = 0.3, topMarginM = 0.3, stepM = 0.5,
    overhangM = 1.0,          -- the rig's width on the axis: at most the deck's half width plus this
    aheadEndM = 0.5,          -- the swept volume ends this far past where the tractor stops
    minLookaheadM = 2, reactionS = 1, decelMs2 = 1,
    rampsDownTime = 1,        -- Foldable fold time of ramps down (FS25_UGR1124 brandtTrailer.xml 283-337)
    movedM = 0.25,            -- the truck or trailer moving this far: TRANSPORT_MOVED
    -- A mounted implement along (the user, 2026-10-21): the rig as far forward as the belt
    -- area's front less this; the implement may overhang the belt area only onto the
    -- trailer's own deck, at least this inside its rear.
    frontMarginM = 0.3, rearMarginM = 0.25,
    -- Run-in (live 2f6986868b5245f1-2: GIANTS GOTO left the rig 1.6 m off and 12 deg turned
    -- and took no closer aim): from up to runInM behind the ramps, within lateralRunInM and
    -- headingRunInDeg of the axis, it steers onto the axis (driveToPoint at lookaheadRunInM);
    -- at the ramps' foot it must be inside the start window, else NOT_LINED_UP_AT_RAMPS, braked.
    -- 25 deg since live 3389dfbd1b564660-1: GIANTS' run-up GOTO ends on position alone and left
    -- the Series 9 15.6, 17.2 and 18.4 deg turned (1.6 m off, heading for the axis). 6-16 m
    -- (4-12 until) since live 95b84283873043d7-1: from 4.1 m, 19 deg and 1.5 m off it reached
    -- the foot 0.31 m off but still 10.5 deg turned; BACKOFF's 8 m from the foot reaches 6.
    runInM = 16, minRunInM = 6, lateralRunInM = 2.5, headingRunInDeg = 25, lookaheadRunInM = 4,
}

-- The one implement a LOAD may carry: mounted on the tractor's three-point joint (GIANTS
-- AttacherJoints.JOINTTYPE_IMPLEMENT through getAttacherJointByJointDescIndex), lifted
-- (getIsLowered false) and, if it folds, folded (Foldable:getIsUnfolded false: an unfolded
-- implement is wider than its size box). nil, or the reason it may not come along.
local function mountedProblem(env, tractor, implement)
    local entry = (tractor:getAttachedImplements() or {})[1]
    if entry == nil or entry.object ~= implement then return "LOAD_TRACTOR_NOT_ALONE" end
    local joint = tractor:getAttacherJointByJointDescIndex(entry.jointDescIndex)
    if joint == nil or joint.jointType ~= env.AttacherJoints.JOINTTYPE_IMPLEMENT then return "LOAD_IMPLEMENT_NOT_MOUNTED" end
    if type(implement.getIsLowered) ~= "function" or implement:getIsLowered() ~= false then return "LOAD_IMPLEMENT_LOWERED" end
    local fold = implement.spec_foldable
    if fold ~= nil and next(fold.foldingParts or {}) ~= nil and implement:getIsUnfolded() ~= false then
        return "LOAD_IMPLEMENT_UNFOLDED"
    end
    return nil
end

-- The deck box in the trailer's frame from its belts: { aMin, aMax, lMin, lMax, height }
-- (height: the belts above the terrain). Errors on fewer than two belts.
local function deckBox(env, s, trailer)
    local spec = trailer.spec_tensionBelts
    assert(type(spec) == "table" and spec.hasTensionBelts)
    local along, box, heights = {}, { lMin = math.huge, lMax = -math.huge }, 0
    for _, belt in ipairs(spec.sortedBelts or {}) do
        local sx, sy, sz = env.getWorldTranslation(belt.startNode)
        local ex, ey, ez = env.getWorldTranslation(belt.endNode)
        assert(finite(sx) and finite(sy) and finite(sz) and finite(ex) and finite(ey) and finite(ez))
        local a1, l1 = inFrame(s, sx, sz)
        local a2, l2 = inFrame(s, ex, ez)
        along[#along + 1] = (a1 + a2) / 2
        box.lMin, box.lMax = math.min(box.lMin, l1, l2), math.max(box.lMax, l1, l2)
        heights = heights + (sy + ey) / 2 - terrainAt(env, (sx + ex) / 2, (sz + ez) / 2)
    end
    assert(#along >= 2)
    table.sort(along)
    local pad = (along[#along] - along[1]) / (#along - 1) / 2
    box.aMin, box.aMax, box.height = along[1] - pad, along[#along] + pad, heights / #along
    return box
end

-- The transport: the trailer and everything in its rig (the truck towing it and what
-- else hangs there), as a set.
local function transportSet(trailer)
    local _, set = rigUnits(trailer.rootVehicle or trailer)
    set[trailer] = true
    return set
end

local function rampsDown(trailer)
    local spec = trailer.spec_foldable
    if spec == nil or next(spec.foldingParts or {}) == nil then return false end
    local t = trailer:getFoldAnimTime()
    return finite(t) and math.abs(t - Hitch.LOAD.rampsDownTime) < 0.001 and spec.foldMoveDirection == 0
end

-- Other vehicles and the player as surroundings() reads them, without the transport.
local function loadSurroundings(env, s)
    local others, player = surroundings(env, s)
    local out = {}
    for _, other in ipairs(others) do
        local v = other.vehicle
        if not (s.transport[v] or (v.rootVehicle ~= nil and s.transport[v.rootVehicle])) then out[#out + 1] = other end
    end
    return out, player
end

-- The tractor's swept volume from `a0` to `a1` (trailer frame) clear? nil, "HIT" and the
-- names, or "UNKNOWN".
local function loadVolume(env, s, a0, a1)
    local D = Hitch.LOAD
    if not s.runIn or s.sign < 0 then
        return clearVolume(env, s, a0, a1, D.lateralMarginM, D.stepM, D.floorClearanceM,
                           s.rigBox.height + s.deck.height + D.topMarginM, s.masks.obstacles)
    end
    -- Run-in: the corridor from where the rig stands onto the axis up to the ramps' foot, the
    -- rig's own straight width on the axis from there (live 987f65f7d073425c-1: one wide
    -- corridor over the ramps and deck met the trailer's surroundings).
    if a0 > a1 then a0, a1 = a1, a0 end
    local lMin, lMax = s.lMin, s.lMax
    local function part(b0, b1, l0, l1)
        if b1 - b0 <= 1e-6 then return nil end
        s.lMin, s.lMax = l0, l1
        -- Static obstacles only: other vehicles' size boxes overstate them (live 2026-10-31, the
        -- MT635 3 m off the line refused every time; the user: "farmhand has room"); a vehicle
        -- really in the way is met by the envelope watch at 3 km/h, braked on contact.
        local hit, names = clearVolume(env, s, b0, b1, D.lateralMarginM, D.stepM, D.floorClearanceM,
                                       s.rigBox.height + s.deck.height + D.topMarginM, s.masks.static or s.masks.obstacles)
        s.lMin, s.lMax = lMin, lMax
        return hit, names
    end
    local hit, names = part(a0, math.min(a1, s.rampFoot), lMin, lMax)
    if hit ~= nil then return hit, names end
    return part(math.max(a0, s.rampFoot), a1, -s.halfWidth, s.halfWidth)
end

-- Prepare LOAD (direction 1) or UNLOAD (-1). `args`: tractor, trailer, direction, farmId,
-- isReserved; optional maxSpeedKmh, timeoutMs (capped by LOAD_LIMITS). Refused before any
-- motion unless every check holds. Returns a session, or nil, a reason and the half-built
-- session (for the log).
function Hitch.newLoad(env, args)
    local D, L = Hitch.LOAD, Hitch.LOAD_LIMITS
    local speed = capped(args.maxSpeedKmh, L.maxSpeedKmh, L.maxSpeedKmh)
    local timeout = capped(args.timeoutMs, L.timeoutMs, L.timeoutMs)
    local sign = (args.direction == 1 and 1) or (args.direction == -1 and -1) or nil
    if speed == nil or speed < Hitch.LIMITS.minSpeedKmh or timeout == nil or sign == nil
            or type(args.isReserved) ~= "function" then
        return nil, "INVALID_LIMITS"
    end
    local s = { tractor = args.tractor, wagon = args.trailer, trailer = args.trailer, farmId = args.farmId, pushedOnly = true,
                isReserved = args.isReserved, maxSpeedKmh = speed, timeoutMs = timeout, sign = sign, elapsed = 0,
                sinceProgress = 0, best = 0, result = nil, others = {} }
    local ok, reason = pcall(function()
        local tractor, trailer = s.tractor, s.trailer
        local v = tractor:getLastSpeed()
        if not finite(v) or math.abs(v) >= 1 then return "NOT_STOPPED" end
        Hitch.watchDriver(env, tractor)
        local denied = unparkAuthority(s)
        if denied ~= nil then return denied end
        if trailer.ownerFarmId ~= s.farmId then return "RESERVATION_LOST" end
        local tv = trailer:getLastSpeed()
        if not finite(tv) or math.abs(tv) >= 1 then return "TRANSPORT_NOT_STOPPED" end
        local axis = tractor.spec_articulatedAxis
        if axis ~= nil and axis.componentJoint ~= nil then return "LOAD_ARTICULATED_UNSUPPORTED" end
        local okUnits, units = pcall(rigUnits, tractor)
        if not okUnits then return "LOAD_GEOMETRY_UNKNOWN" end
        if #units > 2 then return "LOAD_TRACTOR_NOT_ALONE" end
        if #units == 2 then
            local problem = mountedProblem(env, tractor, units[2])
            if problem ~= nil then return problem end
        end
        s.units = units
        if not rampsDown(trailer) then return "LOAD_RAMPS_NOT_DOWN" end
        local belts = trailer.spec_tensionBelts
        if belts == nil or not belts.hasTensionBelts then return "LOAD_NO_BELTS" end
        if belts.areAllBeltsFastened ~= false or tractor.tensionMountObject ~= nil then return "LOAD_BELTS_FASTENED" end
        -- The trailer's frame: its root, its forward axis.
        local ox, _, oz = world(env, trailer.rootNode)
        local fx, _, fz = env.localDirectionToWorld(trailer.rootNode, 0, 0, 1)
        assert(finite(fx) and finite(fz))
        local n = math.sqrt(fx * fx + fz * fz)
        s.ox, s.oz, s.dirX, s.dirZ = ox, oz, fx / n, fz / n
        s.yaw = atan2(s.dirX, s.dirZ)
        s.last = { wx = s.ox, wz = s.oz }
        s.transport = transportSet(trailer)
        s.rig = { [trailer] = true }                       -- never the truck
        for _, unit in ipairs(units) do s.rig[unit] = true end
        local okTrailer, trailerBox = pcall(unitBox, env, s, trailer)
        local okTractor, tractorBox = pcall(unitBox, env, s, tractor)
        local okDeck, deck = pcall(deckBox, env, s, trailer)
        if not (okTrailer and okTractor and okDeck) then return "LOAD_GEOMETRY_UNKNOWN" end
        s.trailerBox, s.tractorBox, s.deck = trailerBox, tractorBox, deck
        -- The rig: the tractor and what is mounted on it, one box over them all.
        local rigBox = { aMin = tractorBox.aMin, aMax = tractorBox.aMax, lMin = tractorBox.lMin, lMax = tractorBox.lMax,
                         height = tractorBox.height }
        for i = 2, #units do
            local okUnit, box = pcall(unitBox, env, s, units[i])
            if not okUnit then return "LOAD_GEOMETRY_UNKNOWN" end
            rigBox.aMin, rigBox.aMax = math.min(rigBox.aMin, box.aMin), math.max(rigBox.aMax, box.aMax)
            rigBox.lMin, rigBox.lMax = math.min(rigBox.lMin, box.lMin), math.max(rigBox.lMax, box.lMax)
            rigBox.height = math.max(rigBox.height, box.height)
        end
        s.rigBox = rigBox
        -- The steering node on the axis, the tractor facing along the trailer.
        local node = tractor:getAISteeringNode()
        local sx, _, sz = world(env, node)
        s.startAlong, s.startLateral = inFrame(s, sx, sz)
        s.turn = unitTurn(env, s, tractor)
        if math.abs(s.startLateral) > D.lateralStartM or math.abs(s.turn) > D.headingStartDeg then
            if sign < 0 or math.abs(s.startLateral) > D.lateralRunInM or math.abs(s.turn) > D.headingRunInDeg then
                return "LOAD_NOT_LINED_UP"
            end
            s.runIn = true
        end
        local length = tractorBox.aMax - tractorBox.aMin
        if length > deck.aMax - deck.aMin then return "LOAD_DOES_NOT_FIT" end
        -- Where it goes: LOAD from behind the ramps to its body centred on the deck (a mounted
        -- implement along: the rig forward to the belt area's front); UNLOAD from the deck back
        -- until its leading end is behind the ramps.
        if sign > 0 then
            s.rampFoot = trailerBox.aMin - D.rampM
            -- Not yet with its body on the deck: a LOAD stopped on the ramps (overspeed, envelope)
            -- goes on from there under the same start window.
            if tractorBox.aMin >= deck.aMin or rigBox.aMax > deck.aMax or rigBox.aMax < s.rampFoot - D.runInM then
                return "LOAD_NOT_BEHIND_RAMPS"
            end
            if s.runIn and rigBox.aMax > s.rampFoot - D.minRunInM then return "LOAD_NOT_LINED_UP" end
            if not s.runIn and rigBox.aMax < s.rampFoot then s.runIn = true end
            if #units == 1 then
                s.travelM = (deck.aMin + deck.aMax) / 2 - (tractorBox.aMin + tractorBox.aMax) / 2
            else
                s.travelM = deck.aMax - D.frontMarginM - rigBox.aMax
                if tractorBox.aMin + s.travelM < deck.aMin or tractorBox.aMax + s.travelM > deck.aMax
                        or rigBox.aMin + s.travelM < trailerBox.aMin + D.rearMarginM then
                    return "LOAD_DOES_NOT_FIT"
                end
            end
            s.sweepFrom, s.sweepTo = rigBox.aMax, rigBox.aMax + s.travelM + D.aheadEndM
        else
            -- On the deck, or stopped on its way off (live ac71c4b45aea40dd-1: ENVELOPE_CONTACT with
            -- the rig half down the ramps, which no motion could then take back off): on the axis
            -- (the start window above) with part of the rig still over the ramps or the deck.
            local onDeck = tractorBox.aMin >= deck.aMin - 0.25 and rigBox.aMin >= trailerBox.aMin
            local onRamps = tractorBox.aMax > trailerBox.aMin + 0.5          -- its front up over the trailer
            if tractorBox.aMax > deck.aMax + 0.25 or not (onDeck or onRamps) then
                return "UNLOAD_NOT_ON_DECK"
            end
            s.travelM = rigBox.aMax - (trailerBox.aMin - D.rampM)
            s.sweepFrom, s.sweepTo = rigBox.aMin, rigBox.aMin - s.travelM - D.aheadEndM
        end
        -- Never more than the ramps plus the trailer.
        if s.travelM <= 0 or s.travelM > D.rampM + D.runInM + (trailerBox.aMax - trailerBox.aMin) then
            return "LOAD_TOO_FAR"
        end
        -- The rig's own half width straight on the axis (its size boxes), and the run-in
        -- corridor from where it stands onto the axis.
        s.halfWidth = 0
        for _, unit in ipairs(units) do
            s.halfWidth = math.max(s.halfWidth, unit.size.width / 2 + math.abs(unit.size.widthOffset or 0))
        end
        -- GIANTS' size box overstates some bodies (live 2026-10-31: the Series 9 5.46 m wide on a
        -- 2.67 m deck, LOAD_PATH_BLOCKED by a tractor parked close beside; the user: "close, not in
        -- the way"): at most the deck's half width plus D.overhangM. Real contact during the motion
        -- still stops it (the envelope watch).
        s.halfWidth = math.min(s.halfWidth, (deck.lMax - deck.lMin) / 2 + D.overhangM)
        -- The run-in corridor: from the tractor's own lateral place onto the axis, at that capped
        -- width (live 2026-10-31: the size box's 5.46 m reached the MT635 parked 3 m off the line).
        local lateral = s.startLateral or 0
        s.lMin = math.min(-s.halfWidth, lateral - s.halfWidth)
        s.lMax = math.max(s.halfWidth, lateral + s.halfWidth)
        if not s.runIn then s.lMin, s.lMax = rigBox.lMin, rigBox.lMax end
        local okMasks, masks = pcall(collisionMasks, env)
        if not okMasks then return "LOAD_QUERY_UNKNOWN" end
        s.masks = masks
        -- The query proves itself: the tractor's own body and the terrain under it, and the
        -- trailer's deck seen and excluded as the transport.
        local b = tractorBox
        local da, dl = (b.aMax - b.aMin) * 0.1, (b.lMax - b.lMin) * 0.1
        local body = overlap(env, s, b.aMin + da, b.aMax - da, b.lMin + dl, b.lMax - dl, 0.1 * b.height,
                             0.9 * b.height, masks.vehicles)
        local ground = overlap(env, s, b.aMin + da, b.aMax - da, b.lMin + dl, b.lMax - dl, -0.5, 0.5, masks.terrain)
        local dk = overlap(env, s, deck.aMin, deck.aMax, deck.lMin, deck.lMax, deck.height - 0.5, deck.height + 0.2,
                           masks.vehicles)
        if body == nil or body.own < 1 or ground == nil or ground.terrain < 1 or dk == nil or dk.own < 1 then
            return "LOAD_QUERY_UNVERIFIED"
        end
        local bad, names = loadVolume(env, s, s.sweepFrom, s.sweepTo)
        if bad == "UNKNOWN" then return "LOAD_QUERY_UNKNOWN" end
        if bad ~= nil then s.pathHits = names; return "LOAD_PATH_BLOCKED" end
        local okAround, others, player = pcall(loadSurroundings, env, s)
        if not okAround then return "LOCAL_AREA_UNKNOWN" end
        s.others, s.player = others, player
        -- Other parked vehicles do not block loading or unloading (the user, 2026-10-31: "can you
        -- not just disable the blockers while loading and unloading?"): a person still does, and
        -- real contact during the motion stops it (the envelope watch).
        local conflict = pullConflict(s, { { s.startAlong, 0 }, { s.startAlong + sign * s.travelM, 0 } },
                                      0, Hitch.LOCAL.playerClearanceM)
        if conflict == "OTHER_VEHICLE_NEAR" then return "LOAD_BLOCKED" end
        if conflict == "PERSON_TOO_CLOSE" then return "LOAD_PERSON" end
        -- The transport stays where it is: every unit's root watched.
        s.watched = {}
        for unit in pairs(s.transport) do
            local ux, _, uz = world(env, unit.rootNode)
            s.watched[#s.watched + 1] = { vehicle = unit, wx = ux, wz = uz }
        end
        return nil
    end)
    if not ok then return nil, "UNKNOWN_OBSERVATION", s end
    if reason ~= nil then return nil, reason, s end
    return s
end

local function loadDrive(env, s, dt)
    local D = Hitch.LOAD
    local denied = unparkAuthority(s)
    if denied ~= nil then return denied end
    local seat = s.tractor:getIsEntered(); if seat ~= true and seat ~= false then return "UNKNOWN_OBSERVATION" end   -- a passenger rides; input is a takeover
    if s.trailer.ownerFarmId ~= s.farmId then return "RESERVATION_LOST" end
    local node = s.tractor:getAISteeringNode()
    local x, _, z = world(env, node)
    local along, lateral = inFrame(s, x, z)
    local progress = s.sign * (along - s.startAlong)
    s.lastAlong, s.lastLateral = along, lateral
    pcall(function() s.lastSpeed = s.tractor:getLastSpeed() end)
    local units = rigUnits(s.tractor)
    if #units ~= #s.units then return "RIG_CHANGED" end
    for i, unit in ipairs(units) do
        if unit ~= s.units[i] then return "RIG_CHANGED" end
    end
    if #units == 2 and mountedProblem(env, s.tractor, units[2]) ~= nil then return "IMPLEMENT_CHANGED" end
    for _, w in ipairs(s.watched) do
        local ux, _, uz = world(env, w.vehicle.rootNode)
        if distance(ux, uz, w.wx, w.wz) > D.movedM then return "TRANSPORT_MOVED" end
    end
    if not rampsDown(s.trailer) then return "RAMPS_MOVED" end
    local onRun = s.runIn and s.sign > 0 and s.rigBox.aMax + progress < s.rampFoot
    if s.runIn and s.sign > 0 and not onRun and not s.footChecked then
        -- At the ramps' foot: lined up as a LOAD start, or stopped there.
        s.footChecked = true
        s.footLateral, s.footTurn = lateral, unitTurn(env, s, s.tractor)
        if math.abs(lateral) > D.lateralStartM or math.abs(s.footTurn) > D.headingStartDeg then
            return "NOT_LINED_UP_AT_RAMPS"
        end
    end
    if onRun then
        if math.abs(lateral) > math.abs(s.startLateral) + D.divergeM then return "DIVERGED" end
    elseif math.abs(lateral) > D.divergeM and not (s.footChecked and math.abs(lateral) <= D.lateralStartM) then
        return "DIVERGED"
    end
    if progress > s.travelM + 0.5 then return "DISTANCE_LIMIT" end
    if s.elapsed > s.timeoutMs then return "TIMEOUT" end
    for _, other in ipairs(s.others) do
        local vx, _, vz = world(env, other.vehicle.rootNode)
        if distance(vx, vz, other.wx, other.wz) > Hitch.LOCAL.movedM then return "OTHER_VEHICLE_MOVED" end
    end
    local others, player = loadSurroundings(env, s)
    s.others, s.player = others, player
    local bad = pullConflict(s, { { along, 0 } }, 0, Hitch.LOCAL.playerStopM)   -- parked vehicles: as at its start
    if bad ~= nil then return bad end
    if progress >= s.travelM then return s.sign > 0 and "LOADED" or "UNLOADED" end
    -- Past the leading end: the stopping distance, never past the planned volume's end.
    local v = math.abs(s.lastSpeed or 0) / 3.6
    local reach = math.max(D.minLookaheadM, v * D.reactionS + v * v / (2 * D.decelMs2) + 0.5)
    local lead = (s.sign > 0 and s.rigBox.aMax or s.rigBox.aMin) + s.sign * progress
    local stop = s.sweepTo
    local far = s.sign > 0 and math.min(lead + reach, stop) or math.max(lead - reach, stop)
    if s.sign * (far - lead) > 0.05 then
        local hit, names = loadVolume(env, s, lead, far)
        if hit == "UNKNOWN" then return "UNKNOWN_OBSERVATION" end
        if hit ~= nil then s.aheadHits = names; return "OBSTACLE_AHEAD" end
    end
    if progress > s.best + Hitch.LOCAL.stallProgressM then
        s.best, s.sinceProgress = progress, 0
    else
        s.sinceProgress = s.sinceProgress + dt
        if s.sinceProgress > Hitch.LOCAL.stallMs then return "STALLED" end
    end
    local tractor = s.tractor
    if not tractor:getIsMotorStarted() then
        if not tractor:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
        tractor:startMotor()
    end
    Hitch.holdActive(s)
    tractor:raiseActive()
    local steer = s.sign > 0 and node or (tractor:getAIReverserNode() or node)
    local _, wy = env.getWorldTranslation(steer)
    local gx, gz = fromFrame(s, along + s.sign * (onRun and D.lookaheadRunInM or D.lookaheadM), 0)
    local lx, _, lz = env.worldToLocal(steer, gx, wy, gz)
    local touched = Hitch.envelope(env, s)
    if touched ~= nil then return touched end
    local over = Hitch.drive(env, s, dt, s.sign > 0, lx, lz)
    if over ~= nil then return over end
    return "CONTINUE"
end

-- One update: "CONTINUE", "LOADED" / "UNLOADED" (braked where planned) or a failure:
-- RESERVATION_LOST, DRIVER_TOOK_OVER, AI_ACTIVE, RIG_CHANGED,
-- TRANSPORT_MOVED, RAMPS_MOVED, DIVERGED, DISTANCE_LIMIT, TIMEOUT, OTHER_VEHICLE_MOVED,
-- OTHER_VEHICLE_NEAR, PERSON_TOO_CLOSE, OBSTACLE_AHEAD, STALLED, MOTOR_UNAVAILABLE,
-- UNKNOWN_OBSERVATION. Latched like UNPARK: later calls only brake.
function Hitch.loadStep(env, s, dt)
    if s.result ~= nil then
        Hitch.halt(env, s, dt)
        return s.result
    end
    s.elapsed = s.elapsed + dt
    local ok, code = pcall(loadDrive, env, s, dt)
    if not ok then code = "UNKNOWN_OBSERVATION" end
    if code ~= "CONTINUE" then
        s.result = code
        Hitch.halt(env, s, dt)
    end
    return code
end

function Hitch.describeLoad(s)
    if s == nil then return "-" end
    local function f(n) return n and string.format("%.2f", n) or "?" end
    local t, d, b = s.trailerBox or {}, s.deck or {}, s.tractorBox or {}
    return string.format("direction=%s trailer=[%s,%s] deck=[%s,%s]x[%s,%s] h=%s tractor=[%s,%s] w=%s h=%s "
        .. "start=(%s,%s) turn=%s runIn=%s foot=(%s,%s) travel=%s along=%s lateral=%s speed=%s t=%dms pathHits=%s aheadHits=%s",
        tostring(s.sign), f(t.aMin), f(t.aMax), f(d.aMin), f(d.aMax), f(d.lMin), f(d.lMax), f(d.height), f(b.aMin),
        f(b.aMax), b.lMax and f(b.lMax - b.lMin) or "?", f(b.height), f(s.startAlong), f(s.startLateral), f(s.turn),
        tostring(s.runIn == true), f(s.footLateral), f(s.footTurn), f(s.travelM), f(s.lastAlong), f(s.lastLateral), f(s.lastSpeed), s.elapsed or 0, tostring(s.pathHits or "-"),
        tostring(s.aheadHits or "-"))
end

-- ---------------------------------------------------------------- BACKOFF
-- AGENTS.md exception 5 (the user, 2026-10-21: "figure this out, quit stopping at every small
-- issue"). A tractor waiting right behind a transport trailer's ramps but not lined up
-- (GIANTS ended it there turned or aside) cannot be started again by GIANTS' agent so close
-- to the trailer (live f41b44f422974e15-5, 49b5344a47ec418a-1: BLOCKED_AT_START): straight
-- back along its own heading, at most travelM at 3 km/h, so GIANTS GOTO can line it up from
-- the run-up. Only the tractor alone or with one mounted implement lifted and folded (as
-- LOAD), only with the transport ahead within nearM, the swept volume behind clear in GIANTS'
-- collision query and no other vehicle (the transport aside) or person near the path; LOAD's
-- guards while moving and the transport watched; restore-on-every-exit. Nothing is teleported.
Hitch.BACKOFF = {
    -- nearM 10 (8 until live 95b84283873043d7-1): LOAD's turned run-in needs 6 m behind the
    -- ramps' foot, 9 m behind the rear; a tractor nearer backs off into it.
    maxSpeedKmh = 8, timeoutMs = 30000, travelM = 8, nearM = 10,
    divergeM = 0.3, lookaheadM = 3,
    lateralMarginM = 0.3, floorClearanceM = 0.3, topMarginM = 0.3, stepM = 0.5, aheadEndM = 0.5,
    minLookaheadM = 2, reactionS = 1, decelMs2 = 1, movedM = 0.25,
}

local function backoffVolume(env, s, a0, a1)
    local D = Hitch.BACKOFF
    -- Static obstacles only, as LOAD (live 2026-10-31: BACKOFF_PATH_BLOCKED behind the Series 9 sent it
    -- round by GIANTS' GOTO); a vehicle really behind it is met by the envelope watch at 3 km/h.
    return clearVolume(env, s, a0, a1, D.lateralMarginM, D.stepM, D.floorClearanceM, s.rigBox.height + D.topMarginM,
                       s.masks.static or s.masks.obstacles)
end

-- `args`: tractor, trailer (the transport it waits behind), farmId, isReserved. A session, or
-- nil, a reason and the half-built session (for the log).
function Hitch.newBackOff(env, args)
    local D = Hitch.BACKOFF
    local s = { tractor = args.tractor, wagon = args.trailer, trailer = args.trailer, farmId = args.farmId, pushedOnly = true,
                isReserved = args.isReserved, maxSpeedKmh = D.maxSpeedKmh, timeoutMs = D.timeoutMs, sign = -1,
                travelM = D.travelM, elapsed = 0, sinceProgress = 0, best = 0, result = nil, others = {} }
    if type(args.isReserved) ~= "function" then return nil, "INVALID_LIMITS" end
    local ok, reason = pcall(function()
        local tractor, trailer = s.tractor, s.trailer
        local v = tractor:getLastSpeed()
        if not finite(v) or math.abs(v) >= 1 then return "NOT_STOPPED" end
        Hitch.watchDriver(env, tractor)
        local denied = unparkAuthority(s)
        if denied ~= nil then return denied end
        local axis = tractor.spec_articulatedAxis
        if axis ~= nil and axis.componentJoint ~= nil then return "BACKOFF_ARTICULATED_UNSUPPORTED" end
        local okUnits, units = pcall(rigUnits, tractor)
        if not okUnits then return "BACKOFF_GEOMETRY_UNKNOWN" end
        if #units > 2 then return "BACKOFF_NOT_ALONE" end
        if #units == 2 then
            local problem = mountedProblem(env, tractor, units[2])
            if problem ~= nil then return problem end
        end
        s.units = units
        local node = tractor:getAISteeringNode()
        local ox, _, oz = world(env, node)
        local fx, _, fz = env.localDirectionToWorld(tractor.rootNode, 0, 0, 1)
        assert(finite(fx) and finite(fz))
        local n = math.sqrt(fx * fx + fz * fz)
        s.ox, s.oz, s.dirX, s.dirZ = ox, oz, fx / n, fz / n
        s.yaw = atan2(s.dirX, s.dirZ)
        s.last = { wx = s.ox, wz = s.oz }
        s.transport = transportSet(trailer)
        s.rig = {}
        for _, unit in ipairs(units) do s.rig[unit] = true end
        s.envelopeTransport = true                         -- it backs away from the ramps it touches
        local rigBox = nil
        for _, unit in ipairs(units) do
            local okBox, box = pcall(unitBox, env, s, unit)
            if not okBox then return "BACKOFF_GEOMETRY_UNKNOWN" end
            rigBox = rigBox == nil and box or { aMin = math.min(rigBox.aMin, box.aMin), aMax = math.max(rigBox.aMax, box.aMax),
                                                lMin = math.min(rigBox.lMin, box.lMin), lMax = math.max(rigBox.lMax, box.lMax),
                                                height = math.max(rigBox.height, box.height) }
        end
        s.rigBox, s.lMin, s.lMax = rigBox, rigBox.lMin, rigBox.lMax
        -- The transport ahead, near (its size box's nearest corner within nearM of the front).
        local okT, tBox = pcall(unitBox, env, s, trailer)
        if not okT then return "BACKOFF_GEOMETRY_UNKNOWN" end
        s.gapM = tBox.aMin - rigBox.aMax
        if tBox.aMax < rigBox.aMax or s.gapM > D.nearM then return "BACKOFF_NOT_AT_THE_TRANSPORT" end
        local okMasks, masks = pcall(collisionMasks, env)
        if not okMasks then return "BACKOFF_QUERY_UNKNOWN" end
        s.masks = masks
        local b = rigBox
        local da, dl = (b.aMax - b.aMin) * 0.1, (b.lMax - b.lMin) * 0.1
        local body = overlap(env, s, b.aMin + da, b.aMax - da, b.lMin + dl, b.lMax - dl, 0.1 * b.height,
                             0.9 * b.height, masks.vehicles)
        local ground = overlap(env, s, b.aMin + da, b.aMax - da, b.lMin + dl, b.lMax - dl, -0.5, 0.5, masks.terrain)
        if body == nil or body.own < 1 or ground == nil or ground.terrain < 1 then return "BACKOFF_QUERY_UNVERIFIED" end
        s.sweepFrom, s.sweepTo = rigBox.aMin, rigBox.aMin - s.travelM - D.aheadEndM
        local bad, names = backoffVolume(env, s, s.sweepFrom, s.sweepTo)
        if bad == "UNKNOWN" then return "BACKOFF_QUERY_UNKNOWN" end
        if bad ~= nil then s.pathHits = names; return "BACKOFF_PATH_BLOCKED" end
        local okAround, others, player = pcall(loadSurroundings, env, s)
        if not okAround then return "LOCAL_AREA_UNKNOWN" end
        s.others, s.player = others, player
        local conflict = pullConflict(s, { { 0, 0 }, { -s.travelM, 0 } }, 0, Hitch.LOCAL.playerClearanceM)
        if conflict == "OTHER_VEHICLE_NEAR" then return "BACKOFF_BLOCKED" end
        if conflict == "PERSON_TOO_CLOSE" then return "BACKOFF_PERSON" end
        s.watched = {}
        for unit in pairs(s.transport) do
            local ux, _, uz = world(env, unit.rootNode)
            s.watched[#s.watched + 1] = { vehicle = unit, wx = ux, wz = uz }
        end
        return nil
    end)
    if not ok then return nil, "UNKNOWN_OBSERVATION", s end
    if reason ~= nil then return nil, reason, s end
    return s
end

local function backoffDrive(env, s, dt)
    local D = Hitch.BACKOFF
    local denied = unparkAuthority(s)
    if denied ~= nil then return denied end
    local seat = s.tractor:getIsEntered(); if seat ~= true and seat ~= false then return "UNKNOWN_OBSERVATION" end   -- a passenger rides; input is a takeover
    local node = s.tractor:getAISteeringNode()
    local x, _, z = world(env, node)
    local along, lateral = inFrame(s, x, z)
    local progress = -along
    s.lastAlong, s.lastLateral = along, lateral
    pcall(function() s.lastSpeed = s.tractor:getLastSpeed() end)
    local units = rigUnits(s.tractor)
    if #units ~= #s.units then return "RIG_CHANGED" end
    for i, unit in ipairs(units) do
        if unit ~= s.units[i] then return "RIG_CHANGED" end
    end
    if #units == 2 and mountedProblem(env, s.tractor, units[2]) ~= nil then return "IMPLEMENT_CHANGED" end
    for _, w in ipairs(s.watched) do
        local ux, _, uz = world(env, w.vehicle.rootNode)
        if distance(ux, uz, w.wx, w.wz) > D.movedM then return "TRANSPORT_MOVED" end
    end
    if math.abs(lateral) > D.divergeM then return "DIVERGED" end
    if progress > s.travelM + 0.5 then return "DISTANCE_LIMIT" end
    if s.elapsed > s.timeoutMs then return "TIMEOUT" end
    for _, other in ipairs(s.others) do
        local vx, _, vz = world(env, other.vehicle.rootNode)
        if distance(vx, vz, other.wx, other.wz) > Hitch.LOCAL.movedM then return "OTHER_VEHICLE_MOVED" end
    end
    local others, player = loadSurroundings(env, s)
    s.others, s.player = others, player
    local bad = pullConflict(s, { { along, 0 } }, Hitch.LOCAL.vehicleStopM, Hitch.LOCAL.playerStopM)
    if bad ~= nil then return bad end
    if progress >= s.travelM then return "BACKED_OFF" end
    local v = math.abs(s.lastSpeed or 0) / 3.6
    local reach = math.max(D.minLookaheadM, v * D.reactionS + v * v / (2 * D.decelMs2) + 0.5)
    local lead = s.rigBox.aMin - progress
    local far = math.max(lead - reach, s.sweepTo)
    if lead - far > 0.05 then
        local hit, names = backoffVolume(env, s, lead, far)
        if hit == "UNKNOWN" then return "UNKNOWN_OBSERVATION" end
        if hit ~= nil then s.aheadHits = names; return "OBSTACLE_AHEAD" end
    end
    if progress > s.best + Hitch.LOCAL.stallProgressM then
        s.best, s.sinceProgress = progress, 0
    else
        s.sinceProgress = s.sinceProgress + dt
        if s.sinceProgress > Hitch.LOCAL.stallMs then return "STALLED" end
    end
    local tractor = s.tractor
    if not tractor:getIsMotorStarted() then
        if not tractor:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
        tractor:startMotor()
    end
    Hitch.holdActive(s)
    tractor:raiseActive()
    local steer = tractor:getAIReverserNode() or node
    local _, wy = env.getWorldTranslation(steer)
    local gx, gz = fromFrame(s, along - D.lookaheadM, 0)
    local lx, _, lz = env.worldToLocal(steer, gx, wy, gz)
    local touched = Hitch.envelope(env, s)
    if touched ~= nil then return touched end
    local over = Hitch.drive(env, s, dt, false, lx, lz)
    if over ~= nil then return over end
    return "CONTINUE"
end

-- One update: "CONTINUE", "BACKED_OFF" (braked travelM back) or a failure as LOAD's.
-- Latched: later calls only brake.
function Hitch.backOffStep(env, s, dt)
    if s.result ~= nil then
        Hitch.halt(env, s, dt)
        return s.result
    end
    s.elapsed = s.elapsed + dt
    local ok, code = pcall(backoffDrive, env, s, dt)
    if not ok then code = "UNKNOWN_OBSERVATION" end
    if code ~= "CONTINUE" then
        s.result = code
        Hitch.halt(env, s, dt)
    end
    return code
end

function Hitch.describeBackOff(s)
    if s == nil then return "-" end
    local function f(n) return n and string.format("%.2f", n) or "?" end
    return string.format("gap=%s travel=%s along=%s lateral=%s speed=%s t=%dms pathHits=%s aheadHits=%s",
        f(s.gapM), f(s.travelM), f(s.lastAlong), f(s.lastLateral), f(s.lastSpeed), s.elapsed or 0,
        tostring(s.pathHits or "-"), tostring(s.aheadHits or "-"))
end

-- ---------------------------------------------------------------- ALONGSIDE
-- AGENTS.md exception 6 (the user, 2026-10-21, "combine full, figure this out"): a standing
-- combine's grain into a trailer. GIANTS' helper cannot start with the tank full
-- (COULD_NOT_PREPARE, live 3dc00834639340f7-2), AutoDrive's unloader serves only a combine
-- whose AI runs, and GIANTS GOTO calls a spot under the pipe not_reachable (live
-- 3dc00834639340f7-1, 15-23 m short). After GIANTS has brought the tractor and its towed
-- trailer near the pipe's line behind the combine (the line through the pipe's discharge
-- point along the combine's heading; Pipe: getCurrentDischargeNode, the pipe out:
-- spec.unloadingStates[getCurrentPipeState()]), the tractor drives forward onto and along
-- that line with GIANTS' driveToPoint, at most 3 km/h and maxTravelM, until the trailer's
-- middle (its size box centre) reaches the pipe point, and must end within lateralTolM of it.
-- Refused unmoved unless the tractor starts within startLateralM and headingStartDeg of the
-- line, the trailer swung at most trailerStartDeg, its middle behind the point, the swept
-- corridor of the whole rig clear in GIANTS'
-- collision query (the combine is not excluded: a pipe low enough to touch refuses), no other
-- vehicle (the combine and its header aside) or person near the path; it stops on any of
-- LOAD's guards, and if the combine moves. Nothing is teleported, no collision is disabled.
Hitch.ALONGSIDE = {
    maxSpeedKmh = 8, timeoutMs = 90000, maxTravelM = 25,
    startLateralM = 2.5,      -- the steering node at most this far off the pipe's line at the start
    headingStartDeg = 15,     -- the tractor at most this far off the line's heading (live 75ef60d7f5ad4735-1:
                              -- GIANTS reached the aim 0.3 m off but turned about 7 deg, the pipe 1.9 m aside)
    trailerStartDeg = 30,     -- the trailer at most this far swung (live 6c66894767044432-1: 19-21 deg) within
    straightenDegPerM = 2.5,  -- 6 m of the pipe; this much more per metre farther it still runs straight on,
    trailerMaxStartDeg = 55,  -- and never more than this
    lateralTolM = 0.8,        -- the trailer's middle this close to the pipe point at the end
    passClearM = 2,           -- pass: on until the rig's rear is this far past the combine's front (live
                              -- 15dbb6c7eb994721-1: GIANTS' deliver helper would not start the rig beside it)
    divergeM = 0.3,           -- the steering node never farther off the line than at the start plus this
    lookaheadM = 6,
    lateralMarginM = 0.3, floorClearanceM = 0.3, topMarginM = 0.3, stepM = 0.5, aheadEndM = 0.5,
    minLookaheadM = 2, reactionS = 1, decelMs2 = 1, movedM = 0.25,
}

-- Where each combine's unfolded pipe discharges, in its root's frame (ahead, to its +l side), learned
-- whenever its pipe is seen out (an overload, a pipe-out): GIANTS' harvesting helper keeps the pipe folded
-- until a trailer is under it, so a moving escort aims at the learned point (live 2026-11-06, field 91:
-- the folded pipe's trigger 4.55 m out, inside the MF 8570's 6.8 m header plus clearance: NO_SAFE_SLOT).
Hitch.pipeOut = setmetatable({}, { __mode = "k" })

local function pipePoint(env, combine)
    local spec = combine.spec_pipe
    assert(type(spec) == "table" and type(spec.unloadingStates) == "table")
    if spec.unloadingStates[combine:getCurrentPipeState()] == nil then return nil end
    local node = combine:getCurrentDischargeNode()
    assert(node ~= nil and node.node ~= nil)
    local x, _, z = env.getWorldTranslation(node.node)
    assert(finite(x) and finite(z))
    pcall(function()
        local rx, _, rz = env.getWorldTranslation(combine.rootNode)
        local fx, _, fz = env.localDirectionToWorld(combine.rootNode, 0, 0, 1)
        local n = math.sqrt(fx * fx + fz * fz)
        fx, fz = fx / n, fz / n
        local dx, dz = x - rx, z - rz
        Hitch.pipeOut[combine] = { a = dx * fx + dz * fz, l = dx * fz - dz * fx }
    end)
    return x, z
end

local function middleOf(env, v)
    local x, _, z = env.localToWorld(v.rootNode, v.size.widthOffset or 0, 0, v.size.lengthOffset or 0)
    assert(finite(x) and finite(z))
    return x, z
end

-- The sweep before the motion leaves out the combine and what hangs on it (s.sweepRig): the rig
-- drives beside it by design, and the size boxes are bigger than the bodies (the user, 2026-10-31,
-- live at field 2: "it is the box around the equipment it is not colliding with the harvester";
-- ALONGSIDE_PATH_BLOCKED on mf8570_main_component1 every time). Real contact with it during the
-- motion still stops the rig (Hitch.envelope keeps only the rig itself out).
local function alongsideVolume(env, s, a0, a1)
    local D = Hitch.ALONGSIDE
    local rig = s.rig
    s.rig = s.sweepRig or rig
    local bad, names = clearVolume(env, s, a0, a1, D.lateralMarginM, D.stepM, D.floorClearanceM,
                                   s.rigBox.height + D.topMarginM, s.masks.obstacles)
    s.rig = rig
    return bad, names
end

-- `args`: tractor, combine, farmId, isReserved; pass: on along the line until the rig is past
-- the combine (after the overload, the pipe still out). The frame is the pipe's line: through
-- the pipe point along the combine's heading (a ahead, l to its local +x side), the point at 0.
-- A session, or nil, a reason and the half-built session (for the log).
function Hitch.newAlongside(env, args)
    local D = Hitch.ALONGSIDE
    local s = { tractor = args.tractor, wagon = args.combine, combine = args.combine, farmId = args.farmId, pushedOnly = true,
                pass = args.pass == true,
                isReserved = args.isReserved, maxSpeedKmh = D.maxSpeedKmh, timeoutMs = D.timeoutMs, sign = 1,
                elapsed = 0, sinceProgress = 0, best = -math.huge, result = nil, others = {} }
    if type(args.isReserved) ~= "function" then return nil, "INVALID_LIMITS" end
    local ok, reason = pcall(function()
        local tractor, combine = s.tractor, s.combine
        local v = tractor:getLastSpeed()
        if not finite(v) or math.abs(v) >= 1 then return "NOT_STOPPED" end
        Hitch.watchDriver(env, tractor)
        local denied = unparkAuthority(s)
        if denied ~= nil then return denied end
        local cv = combine:getLastSpeed()
        if not finite(cv) or math.abs(cv) >= 1 then return "COMBINE_NOT_STOPPED" end
        local axis = tractor.spec_articulatedAxis
        if axis ~= nil and axis.componentJoint ~= nil then return "ALONGSIDE_ARTICULATED_UNSUPPORTED" end
        local okUnits, units = pcall(rigUnits, tractor)
        if not okUnits or #units ~= 2 then return "ALONGSIDE_NOT_ONE_TRAILER" end
        s.units, s.trailer = units, units[2]
        local px, pz = pipePoint(env, combine)
        if px == nil then return "PIPE_NOT_OUT" end
        local fx, _, fz = env.localDirectionToWorld(combine.rootNode, 0, 0, 1)
        assert(finite(fx) and finite(fz))
        -- Along the combine either way: a trailer facing against it passes under the pipe from
        -- its front (live 7b52570db6ee40d0-1: the lane behind it blocked by a dropped seeder).
        local tfx, _, tfz = env.localDirectionToWorld(tractor.rootNode, 0, 0, 1)
        if finite(tfx) and finite(tfz) and tfx * fx + tfz * fz < 0 then fx, fz = -fx, -fz end
        local n = math.sqrt(fx * fx + fz * fz)
        s.ox, s.oz, s.dirX, s.dirZ = px, pz, fx / n, fz / n
        s.yaw = atan2(s.dirX, s.dirZ)
        s.last = { wx = s.ox, wz = s.oz }
        s.rig = { [tractor] = true, [s.trailer] = true }
        s.sweepRig = { [tractor] = true, [s.trailer] = true }
        local okC, cUnits = pcall(rigUnits, combine)
        for _, unit in ipairs(okC and cUnits or { combine }) do s.sweepRig[unit] = true end
        local node = tractor:getAISteeringNode()
        local sx, _, sz = world(env, node)
        s.startAlong, s.startLateral = inFrame(s, sx, sz)
        s.turn, s.trailerTurn = unitTurn(env, s, tractor), unitTurn(env, s, s.trailer)
        if math.abs(s.startLateral) > D.startLateralM or math.abs(s.turn) > D.headingStartDeg then
            return "ALONGSIDE_NOT_LINED_UP"
        end
        local mx, mz = middleOf(env, s.trailer)
        s.middleAlong, s.middleLateral = inFrame(s, mx, mz)
        -- Driving straight on straightens a towed trailer within about its own length: the farther its
        -- middle still has to go to the pipe, the more swing it may start with (live 2026-11-06, field 91:
        -- the 3650 lined up on the pipe's line 13.9 m back, its Z 18051 swung 46.7 and 33.8 degrees).
        local togo = s.pass and 0 or math.max(0, -s.middleAlong)
        local allowed = math.min(D.trailerMaxStartDeg, D.trailerStartDeg + D.straightenDegPerM * math.max(0, togo - 6))
        if math.abs(s.trailerTurn) > allowed then return "ALONGSIDE_RIG_NOT_STRAIGHT" end
        if not s.pass then
            if s.middleAlong >= 0 then return "ALONGSIDE_PAST_THE_PIPE" end
            s.travelM = -s.middleAlong
            if s.travelM > D.maxTravelM then return "ALONGSIDE_TOO_FAR" end
        end
        local rigBox = nil
        for _, unit in ipairs(units) do
            local okBox, box = pcall(unitBox, env, s, unit)
            if not okBox then return "ALONGSIDE_GEOMETRY_UNKNOWN" end
            rigBox = rigBox == nil and box or { aMin = math.min(rigBox.aMin, box.aMin), aMax = math.max(rigBox.aMax, box.aMax),
                                                lMin = math.min(rigBox.lMin, box.lMin), lMax = math.max(rigBox.lMax, box.lMax),
                                                height = math.max(rigBox.height, box.height) }
        end
        s.rigBox = rigBox
        if s.pass then
            -- The combine's front (the combine and what hangs on it, its header) in the frame.
            local okFront, frontUnits = pcall(rigUnits, combine)
            if not okFront then return "ALONGSIDE_GEOMETRY_UNKNOWN" end
            local front = -math.huge
            for _, unit in ipairs(frontUnits) do
                local okBox, box = pcall(unitBox, env, s, unit)
                if not okBox then return "ALONGSIDE_GEOMETRY_UNKNOWN" end
                front = math.max(front, box.aMax)
            end
            s.combineFront = front
            s.travelM = front + D.passClearM - rigBox.aMin
            if s.travelM <= 0 then return "ALONGSIDE_ALREADY_CLEAR" end
            if s.travelM > D.maxTravelM then return "ALONGSIDE_TOO_FAR" end
        end
        -- The corridor: from where the rig stands to where it ends on the line.
        s.lMin = math.min(rigBox.lMin, rigBox.lMin - s.startLateral)
        s.lMax = math.max(rigBox.lMax, rigBox.lMax - s.startLateral)
        local okMasks, masks = pcall(collisionMasks, env)
        if not okMasks then return "ALONGSIDE_QUERY_UNKNOWN" end
        s.masks = masks
        local b = rigBox
        local da, dl = (b.aMax - b.aMin) * 0.1, (b.lMax - b.lMin) * 0.1
        local body = overlap(env, s, b.aMin + da, b.aMax - da, b.lMin + dl, b.lMax - dl, 0.1 * b.height,
                             0.9 * b.height, masks.vehicles)
        local ground = overlap(env, s, b.aMin + da, b.aMax - da, b.lMin + dl, b.lMax - dl, -0.5, 0.5, masks.terrain)
        if body == nil or body.own < 1 or ground == nil or ground.terrain < 1 then return "ALONGSIDE_QUERY_UNVERIFIED" end
        s.sweepFrom, s.sweepTo = rigBox.aMax, rigBox.aMax + s.travelM + D.aheadEndM
        local bad, names = alongsideVolume(env, s, s.sweepFrom, s.sweepTo)
        if bad == "UNKNOWN" then return "ALONGSIDE_QUERY_UNKNOWN" end
        if bad ~= nil then s.pathHits = names; return "ALONGSIDE_PATH_BLOCKED" end
        local okAround, others, player = pcall(surroundings, env, s)
        if not okAround then return "LOCAL_AREA_UNKNOWN" end
        s.others, s.player = others, player
        local conflict = pullConflict(s, { { s.startAlong, s.startLateral }, { s.startAlong + s.travelM, 0 } },
                                      Hitch.LOCAL.vehicleClearanceM, Hitch.LOCAL.playerClearanceM)
        if conflict == "OTHER_VEHICLE_NEAR" then return "ALONGSIDE_BLOCKED" end
        if conflict == "PERSON_TOO_CLOSE" then return "ALONGSIDE_PERSON" end
        local cx, _, cz = world(env, combine.rootNode)
        s.combineAt = { wx = cx, wz = cz }
        return nil
    end)
    if not ok then return nil, "UNKNOWN_OBSERVATION", s end
    if reason ~= nil then return nil, reason, s end
    return s
end

local function alongsideDrive(env, s, dt)
    local D = Hitch.ALONGSIDE
    local denied = unparkAuthority(s)
    if denied ~= nil then return denied end
    local seat = s.tractor:getIsEntered(); if seat ~= true and seat ~= false then return "UNKNOWN_OBSERVATION" end   -- a passenger rides; input is a takeover
    local node = s.tractor:getAISteeringNode()
    local x, _, z = world(env, node)
    local along, lateral = inFrame(s, x, z)
    local progress = along - s.startAlong
    s.lastAlong, s.lastLateral = along, lateral
    pcall(function() s.lastSpeed = s.tractor:getLastSpeed() end)
    local units = rigUnits(s.tractor)
    if #units ~= 2 or units[2] ~= s.trailer then return "RIG_CHANGED" end
    local cx, _, cz = world(env, s.combine.rootNode)
    if distance(cx, cz, s.combineAt.wx, s.combineAt.wz) > D.movedM then return "COMBINE_MOVED" end
    if not s.pass and pipePoint(env, s.combine) == nil then return "PIPE_MOVED" end
    if math.abs(lateral) > math.abs(s.startLateral) + D.divergeM then return "DIVERGED" end
    if progress > s.travelM + 1.0 then return "DISTANCE_LIMIT" end
    if s.elapsed > s.timeoutMs then return "TIMEOUT" end
    for _, other in ipairs(s.others) do
        local vx, _, vz = world(env, other.vehicle.rootNode)
        if distance(vx, vz, other.wx, other.wz) > Hitch.LOCAL.movedM then return "OTHER_VEHICLE_MOVED" end
    end
    local others, player = surroundings(env, s)
    s.others, s.player = others, player
    local bad = pullConflict(s, { { along, lateral } }, Hitch.LOCAL.vehicleStopM, Hitch.LOCAL.playerStopM)
    if bad ~= nil then return bad end
    if s.pass and progress >= s.travelM then return "PASSED" end
    -- The trailer's own middle reaching the pipe point ends it; under it only if close.
    local mx, mz = middleOf(env, s.trailer)
    s.middleAlong, s.middleLateral = inFrame(s, mx, mz)
    if not s.pass and s.middleAlong >= 0 then
        if math.abs(s.middleLateral) > D.lateralTolM then return "ALONGSIDE_NOT_UNDER" end
        return "UNDER_PIPE"
    end
    local v = math.abs(s.lastSpeed or 0) / 3.6
    local reach = math.max(D.minLookaheadM, v * D.reactionS + v * v / (2 * D.decelMs2) + 0.5)
    local lead = s.rigBox.aMax + progress
    local far = math.min(lead + reach, s.sweepTo)
    if far - lead > 0.05 then
        local hit, names = alongsideVolume(env, s, lead, far)
        if hit == "UNKNOWN" then return "UNKNOWN_OBSERVATION" end
        if hit ~= nil then s.aheadHits = names; return "OBSTACLE_AHEAD" end
    end
    if progress > s.best + Hitch.LOCAL.stallProgressM then
        s.best, s.sinceProgress = progress, 0
    else
        s.sinceProgress = s.sinceProgress + dt
        if s.sinceProgress > Hitch.LOCAL.stallMs then return "STALLED" end
    end
    local tractor = s.tractor
    if not tractor:getIsMotorStarted() then
        if not tractor:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
        tractor:startMotor()
    end
    Hitch.holdActive(s)
    tractor:raiseActive()
    -- Onto the line and along it: GIANTS' driveToPoint at a point on the line ahead.
    local _, wy = env.getWorldTranslation(node)
    local gx, gz = fromFrame(s, along + D.lookaheadM, 0)
    local lx, _, lz = env.worldToLocal(node, gx, wy, gz)
    local touched = Hitch.envelope(env, s)
    if touched ~= nil then return touched end
    local over = Hitch.drive(env, s, dt, true, lx, lz)
    if over ~= nil then return over end
    return "CONTINUE"
end

-- One update: "CONTINUE", "UNDER_PIPE" / "PASSED" (braked there) or a failure as LOAD's,
-- plus COMBINE_MOVED, PIPE_MOVED and ALONGSIDE_NOT_UNDER. Latched: later calls only brake.
function Hitch.alongsideStep(env, s, dt)
    if s.result ~= nil then
        Hitch.halt(env, s, dt)
        return s.result
    end
    s.elapsed = s.elapsed + dt
    local ok, code = pcall(alongsideDrive, env, s, dt)
    if not ok then code = "UNKNOWN_OBSERVATION" end
    if code ~= "CONTINUE" then
        s.result = code
        Hitch.halt(env, s, dt)
    end
    return code
end

function Hitch.describeAlongside(s)
    if s == nil then return "-" end
    local function f(n) return n and string.format("%.2f", n) or "?" end
    return string.format("start=(%s,%s) turn=%s trailerTurn=%s middle=(%s,%s) travel=%s along=%s lateral=%s speed=%s "
        .. "t=%dms pathHits=%s aheadHits=%s", f(s.startAlong), f(s.startLateral), f(s.turn), f(s.trailerTurn),
        f(s.middleAlong), f(s.middleLateral), f(s.travelM), f(s.lastAlong), f(s.lastLateral), f(s.lastSpeed),
        s.elapsed or 0, tostring(s.pathHits or "-"), tostring(s.aheadHits or "-"))
end

-- ------------------------------------------------------------------ ESCORT
-- A grain cart beside a combine that is harvesting under GIANTS' helper (FINAL_FARMHAND_PRODUCT_SPEC.md
-- 5: unloading on the move is a release gate). GIANTS' own combine strategy swings the pipe out
-- and discharges whenever a trailer that takes the grain is in the pipe's unloading trigger, and
-- keeps harvesting with the pipe out where its turnOnAllowed states allow it
-- (AIDriveStrategyCombine:getDriveData 79-176; Pipe:unloadingTriggerCallback 836-, its
-- nearestObjectInTriggers). Farmhand only drives the cart: the trailer's middle held at the slot
-- under the pipe (the unloading trigger until the pipe is out, then the discharge node), at the
-- combine's speed, its tractor kept clear of the combine's and header's boxes; behind the combine
-- on its own trail while it turns or is not yet straight. Never a teleport, never collision off.
-- Ends braked: TRAILER_FULL, COMBINE_STOPPED (its helper ended), TRAILER_WRONG_CROP (the combine
-- holds a crop this trailer cannot take), or a failure.
Hitch.ESCORT = {
    maxSpeedKmh = 20,           -- the cart's limit while it catches up
    timeoutMs = 7200000,
    startRangeM = 90,           -- the steering node at most this far from the combine at the start
    lookaheadM = 6, lookaheadS = 1.2,
    gainKmhPerM = 1.5,          -- above the combine's speed per metre the trailer is behind its target
    catchUpKmh = 8,             -- at most this much above the combine's speed
    onLineM = 1.5,              -- the cart this near the slot's line before it moves up beside the combine
    clearanceM = 0.8,           -- kept between the cart's and the combine rig's boxes
    followGapM = 8, turnGapM = 16,   -- the cart's front behind the combine rig's rear on its trail
    trailStepM = 1, trailBackM = 60, trailMax = 900, trailSearch = 60,
    lineDeg = 8, steadyMs = 2500,    -- the combine this straight this long before the cart comes alongside
    alignDeg = 25,              -- the cart within this of the combine's heading to come alongside
    spoutMarginM = 0.3,         -- the spout kept this far inside the trailer's box when the slot shifts (live
                                -- 2026-11-06, field 91: the MF 8570's pipe reaches 4.76 m, its header 3.4 m; at
                                -- 0.8 m no spot kept the cart its clearance from the header, NO_SAFE_SLOT)
    triggerMarginM = 0.3,       -- the folded pipe's trigger point this far inside it (GIANTS swings the pipe out)
    fullShare = 0.98,
    idleAiMs = 3000,            -- the combine's helper off (the player out of it) this long: COMBINE_STOPPED (PLAYER_LEFT)
    playerTurnDegS = 6,         -- a player's combine turning this fast: no slot beside it
    coverOpenMs = 1500, coverCloseMs = 500,   -- the transfer wanted (or not) this long before the cover moves
    fullStandingShare = 0.95,   -- a combine standing with its tank this full waits to be unloaded: unloaded standing
    emptyShare = 0.03,          -- a one-time unloading is done with the tank this empty
    noSlotMs = 30000,           -- no clear slot beside the combine this long: NO_SAFE_SLOT
    stallMs = 30000, stallM = 1.0,
    checkMs = 250, traceMs = 1000, traceMax = 900,
}

local function yawOf(env, node)
    local x, _, z = env.localDirectionToWorld(node, 0, 0, 1)
    assert(finite(x) and finite(z))
    return atan2(x, z)
end

local function angleDiff(a, b) return (a - b + math.pi) % (2 * math.pi) - math.pi end

-- The frame of inFrame/fromFrame on the combine's root: a ahead along its heading, l to its +x side.
local function combineFrame(env, s)
    local cx, _, cz = world(env, s.combine.rootNode)
    local fx, _, fz = env.localDirectionToWorld(s.combine.rootNode, 0, 0, 1)
    local n = math.sqrt(fx * fx + fz * fz)
    assert(finite(cx) and finite(cz) and finite(n) and n > 0.1)
    s.last, s.ox, s.oz = { wx = cx, wz = cz }, cx, cz
    s.dirX, s.dirZ = fx / n, fz / n
    s.yaw = atan2(s.dirX, s.dirZ)
    return cx, cz
end

local function cartLevel(trailer)
    local level, capacity = 0, 0
    for i in ipairs(trailer:getFillUnits()) do
        level = level + (trailer:getFillUnitFillLevel(i) or 0)
        capacity = capacity + (trailer:getFillUnitCapacity(i) or 0)
    end
    assert(finite(level) and finite(capacity))
    return level, capacity
end

-- Whether the trailer takes what the combine's tank holds (anything while it is empty): a fill
-- unit that supports it and is empty or holds the same.
local function cartTakes(env, s)
    local node = s.combine:getCurrentDischargeNode()
    if node == nil then return false end
    local unknown = env.FillType ~= nil and env.FillType.UNKNOWN or 0
    local crop = s.combine:getFillUnitFillType(node.fillUnitIndex)
    if not finite(crop) or crop == unknown or (s.combine:getFillUnitFillLevel(node.fillUnitIndex) or 0) <= 0 then
        return true
    end
    for i in ipairs(s.trailer:getFillUnits()) do
        local held = s.trailer:getFillUnitFillType(i)
        if s.trailer:getFillUnitSupportsFillType(i, crop)
                and (held == crop or held == unknown or (s.trailer:getFillUnitFillLevel(i) or 0) <= 0) then
            return true
        end
    end
    return false
end

-- The slot under the pipe in the combine's frame: the discharge node while the pipe is out (the
-- last one seen while it is folded again), else the pipe's unloading trigger.
local function escortSlot(env, s)
    local px, pz = pipePoint(env, s.combine)
    local learned = Hitch.pipeOut[s.combine]
    if px ~= nil then
        s.slotA, s.slotL = inFrame(s, px, pz)
        s.slotSource = "pipe"
    elseif learned ~= nil then
        s.slotA, s.slotL = learned.a, learned.l
        s.slotSource = "learned"
    elseif s.slotSource ~= "pipe" then
        local x, _, z = world(env, s.trigger)
        s.slotA, s.slotL = inFrame(s, x, z)
        s.slotSource = "trigger"
    end
end

local function boxesOf(env, s, units)
    local out = {}
    for _, unit in ipairs(units) do out[#out + 1] = unitBox(env, s, unit) end
    return out
end

local function bandsMeet(b, a0, a1, l0, l1, margin)
    return a0 < b.aMax + margin and a1 > b.aMin - margin and l0 < b.lMax + margin and l1 > b.lMin - margin
end

-- Where the trailer's middle goes: the slot, else the nearest spot with the spout still over the
-- trailer where the straight cart (its band about its middle, s.cart) is clear of every box of the
-- combine rig. nil when there is none.
local function escortTarget(s)
    local E, c = Hitch.ESCORT, s.cart
    local side = s.slotL >= 0 and 1 or -1
    -- The folded pipe's trigger only has to see the trailer: GIANTS' combine strategy then swings the pipe
    -- out over it (AIDriveStrategyCombine, nearestObjectInTriggers -> setPipeState(2)) and the slot follows
    -- the real discharge point with the full margin (live 2026-11-06, field 91: the trigger 4.55 m out, the
    -- MF 8570's header 3.4 m: no spot kept the spout 0.8 m inside the trailer and the cart clear of it).
    for _, shift in ipairs(s.slotSource == "trigger" and s.triggerShifts or s.shifts) do
        local a, l = s.slotA + shift[1], s.slotL + side * shift[2]
        local clear = true
        for _, b in ipairs(s.combineBoxes) do
            if bandsMeet(b, a + c.rear, a + c.front, l - c.halfW, l + c.halfW, E.clearanceM) then
                clear = false
                break
            end
        end
        if clear then return a, l end
    end
    return nil
end

-- The along/outward shifts a slot may take with the spout still inside the trailer, cheapest first.
local function slotShifts(c, margin)
    local E, out = Hitch.ESCORT, {}
    margin = margin or E.spoutMarginM
    local maxA, maxL = math.max(0, c.trailerHalfLen - margin), math.max(0, c.trailerHalfW - margin)
    for i = -math.floor(maxA / 0.5 + 1e-6), math.floor(maxA / 0.5 + 1e-6) do
        for j = 0, math.floor(maxL / 0.25 + 1e-6) do
            out[#out + 1] = { i * 0.5, j * 0.25 }
        end
    end
    table.sort(out, function(p, q)
        local cp, cq = math.abs(p[1]) + 2 * p[2], math.abs(q[1]) + 2 * q[2]
        if cp ~= cq then return cp < cq end
        if p[1] ~= q[1] then return p[1] < q[1] end    -- back before forward: the tractor away from the header
        return p[2] < q[2]
    end)
    return out
end

-- `args`: tractor (towing one trailer), combine (harvesting under GIANTS' helper), farmId, isReserved.
function Hitch.newEscort(env, args)
    local E = Hitch.ESCORT
    local s = { tractor = args.tractor, combine = args.combine, wagon = args.combine, farmId = args.farmId,
                isReserved = args.isReserved, maxSpeedKmh = E.maxSpeedKmh, timeoutMs = E.timeoutMs,
                startShare = tonumber(args.startShare) or 0,
                playerCombine = args.playerCombine == true, once = args.once == true,
                pushedOnly = true, elapsed = 0, result = nil, mode = "follow", trail = {}, trailIdx = 1,
                checkAt = 0, aiOffMs = 0, steadyMs = 0, noSlotMs = 0, stallMs = 0, rows = {}, traceAt = 0,
                moved = 0, movingL = 0, standingL = 0, alongsideMs = 0, pipeOutMovingMs = 0,
                brakes = { close = 0, person = 0, vehicle = 0, gap = 0, slow = 0 } }
    if type(args.isReserved) ~= "function" then return nil, "INVALID_LIMITS" end
    local ok, reason = pcall(function()
        local tractor, combine = s.tractor, s.combine
        Hitch.watchDriver(env, tractor)
        local denied = unparkAuthority(s)
        if denied ~= nil then return denied end
        if combine.ownerFarmId ~= s.farmId then return "COMBINE_NOT_OWNED" end
        local pipe = combine.spec_pipe
        if type(pipe) ~= "table" or type(pipe.unloadingTriggers) ~= "table" or pipe.unloadingTriggers[1] == nil then
            return "NO_PIPE"
        end
        s.trigger = pipe.unloadingTriggers[1].node
        -- The player's own combine (the owner, 2026-11-06: "come unload my combine"): never taken over, only
        -- watched; else GIANTS' helper must be harvesting with it.
        if s.playerCombine then
            if type(combine.getIsEntered) ~= "function" or combine:getIsEntered() ~= true then return "PLAYER_NOT_IN_COMBINE" end
        elseif combine:getIsAIActive() ~= true then return "COMBINE_NOT_HARVESTING" end
        local units = rigUnits(tractor)
        if #units ~= 2 then return "ESCORT_NOT_ONE_TRAILER" end
        s.units, s.trailer = units, units[2]
        local level, capacity = cartLevel(s.trailer)
        if capacity <= 0 then return "ESCORT_NOT_ONE_TRAILER" end
        if level >= E.fullShare * capacity then return "TRAILER_FULL" end
        s.startLevel, s.lastLevel, s.capacity = level, level, capacity
        if combine:getCurrentDischargeNode() == nil then return "NO_DISCHARGE_NODE" end
        if not cartTakes(env, s) then return "TRAILER_WRONG_CROP" end
        -- The straight cart about its trailer's middle (along the tractor's heading).
        local mx, mz = middleOf(env, s.trailer)
        local fx, _, fz = env.localDirectionToWorld(tractor.rootNode, 0, 0, 1)
        local n = math.sqrt(fx * fx + fz * fz)
        s.last, s.dirX, s.dirZ = { wx = mx, wz = mz }, fx / n, fz / n
        local cart = { front = -math.huge, rear = math.huge, halfW = 0 }
        for _, box in ipairs(boxesOf(env, s, units)) do
            cart.front, cart.rear = math.max(cart.front, box.aMax), math.min(cart.rear, box.aMin)
            cart.halfW = math.max(cart.halfW, -box.lMin, box.lMax)
        end
        local tb = unitBox(env, s, s.trailer)
        cart.trailerHalfLen, cart.trailerHalfW = (tb.aMax - tb.aMin) / 2, (tb.lMax - tb.lMin) / 2
        s.cart, s.shifts = cart, slotShifts(cart)
        s.triggerShifts = slotShifts(cart, Hitch.ESCORT.triggerMarginM)
        local okMasks, masks = pcall(collisionMasks, env)
        if not okMasks then return "ESCORT_QUERY_UNKNOWN" end
        s.masks = masks
        -- Contact with the combine rig is the clearance rule's (it moves; a push watch would see it pushed).
        local okC, cUnits = pcall(rigUnits, combine)
        if not okC then return "ESCORT_GEOMETRY_UNKNOWN" end
        s.combineUnits = cUnits
        s.rig = { [tractor] = true, [s.trailer] = true }
        for _, unit in ipairs(cUnits) do s.rig[unit] = true end
        local cx, cz = combineFrame(env, s)
        s.combineBoxes = boxesOf(env, s, cUnits)
        local sx, _, sz = world(env, tractor:getAISteeringNode())
        if distance(sx, sz, cx, cz) > E.startRangeM then return "ESCORT_TOO_FAR" end
        local a = inFrame(s, sx, sz)
        local front = -math.huge
        for _, b in ipairs(s.combineBoxes) do front = math.max(front, b.aMax) end
        if a >= front + E.clearanceM then return "ESCORT_AHEAD_OF_COMBINE" end
        escortSlot(env, s)
        -- The combine's trail: the line it came along (behind it), then where it is.
        for d = E.trailBackM, 0, -E.trailStepM do
            s.trail[#s.trail + 1] = { cx - s.dirX * d, cz - s.dirZ * d }
        end
        s.lineYaw = s.yaw
        s.progressAt = { sx, sz }
        return nil
    end)
    if not ok then return nil, "UNKNOWN_OBSERVATION", s end
    if reason ~= nil then return nil, reason, s end
    return s
end

local function brakeFrame(env, s, dt, why)
    s.brakes[why] = (s.brakes[why] or 0) + 1
    s.lastBrake = why
    pcall(Hitch.measureSpeed, env, s)
    pcall(env.AIVehicleUtil.driveToPoint, s.tractor, dt, 0, false, true, 0, 1, s.maxSpeedKmh)
end

local function trailAdd(s, cx, cz)
    local E, t = Hitch.ESCORT, s.trail
    local lastPt = t[#t]
    if distance(lastPt[1], lastPt[2], cx, cz) < E.trailStepM then return end
    t[#t + 1] = { cx, cz }
    if #t > E.trailMax then
        table.remove(t, 1)
        s.trailIdx = math.max(1, s.trailIdx - 1)
    end
end

-- The trail point nearest (x, z) at or after s.trailIdx (within trailSearch points), kept.
local function trailNearest(s, x, z)
    local E, t = Hitch.ESCORT, s.trail
    local best, bestD = s.trailIdx, math.huge
    for i = math.max(1, s.trailIdx - 5), math.min(#t, s.trailIdx + E.trailSearch) do
        local d = distance(t[i][1], t[i][2], x, z)
        if d < bestD then best, bestD = i, d end
    end
    s.trailIdx = best
    return best, bestD
end

local function escortDrive(env, s, dt)
    local E = Hitch.ESCORT
    local denied = unparkAuthority(s)
    if denied ~= nil then return denied end
    local seat = s.tractor:getIsEntered(); if seat ~= true and seat ~= false then return "UNKNOWN_OBSERVATION" end
    local units = rigUnits(s.tractor)
    if #units ~= 2 or units[2] ~= s.trailer then return "RIG_CHANGED" end
    if s.elapsed > s.timeoutMs then return "TIMEOUT" end
    local combine = s.combine
    if s.playerCombine then
        -- The player left the combine: nothing more to serve.
        if combine:getIsEntered() ~= true then
            s.aiOffMs = s.aiOffMs + dt
            if s.aiOffMs >= E.idleAiMs then return "PLAYER_LEFT" end
        else
            s.aiOffMs = 0
        end
    elseif combine:getIsAIActive() ~= true then
        s.aiOffMs = s.aiOffMs + dt
        if s.aiOffMs >= E.idleAiMs then return "COMBINE_STOPPED" end
    else
        s.aiOffMs = 0
    end
    local cx, cz = combineFrame(env, s)
    -- A player's turn is seen, not told (no helper reports it): its heading changing faster than playerTurnDegS.
    local yawRate = 0
    if s.prevYaw ~= nil and dt > 0 then yawRate = math.abs(angleDiff(s.yaw, s.prevYaw)) / dt * 1000 end
    s.prevYaw = s.yaw
    local vc = math.abs(combine:getLastSpeed() or 0)
    assert(finite(vc))
    local root = combine.rootVehicle or combine
    -- Turning, reversing, or not threshing (its helper still driving to the field, or stopped full):
    -- no slot beside it, the cart behind it.
    local turning = (type(root.getAIFieldWorkerIsTurning) == "function" and root:getAIFieldWorkerIsTurning() == true)
        or (s.playerCombine and yawRate > math.rad(E.playerTurnDegS))
        or (combine.movingDirection ~= nil and combine.movingDirection < 0)
        or (type(combine.getIsTurnedOn) == "function" and combine:getIsTurnedOn() ~= true)
    if math.abs(angleDiff(s.yaw, s.lineYaw)) > math.rad(E.lineDeg) or turning then
        s.lineYaw, s.steadyMs = s.yaw, 0
    else
        s.steadyMs = s.steadyMs + dt
    end
    trailAdd(s, cx, cz)
    -- The grain: what came into the trailer, and how much of it while the combine moved.
    -- Grain only while the combine moves threshing on its lane with the cart alongside (the owner, 2026-11-06:
    -- no unloading without movement; live, field 91: 1082 l went in while both stood at a headland). GIANTS'
    -- own control: a closed cover refuses the combine's discharge (Cover:getFillUnitSupportsToolType), so
    -- nothing has to be driven near the header to stop it.
    -- The owner, 2026-11-06 (clarified): moving-only was the test's rule, not the product's. A combine standing
    -- because its tank is full (GIANTS' helper waits for a trailer under the pipe) is unloaded standing; only
    -- a turn (header and pipe swinging) keeps the transfer shut.
    local helperTurning = type(root.getAIFieldWorkerIsTurning) == "function" and root:getAIFieldWorkerIsTurning() == true
    s.fullStanding = (s.tankShare or 0) >= E.fullStandingShare and vc < 1.0 and not helperTurning
        and not (s.playerCombine and yawRate > math.rad(E.playerTurnDegS))
    local threshingNow = type(combine.getIsTurnedOn) == "function" and combine:getIsTurnedOn() == true
    local transfer = s.mode == "alongside" and ((vc > 1.5 and threshingNow and not turning) or s.fullStanding)
    local cover = s.trailer.spec_cover
    if cover ~= nil and cover.hasCovers and type(s.trailer.setCoverState) == "function" then
        s.coverStart = s.coverStart or cover.state
        local open = s.coverOpen or (cover.state > 0 and cover.state or 1)
        s.coverOpen = open
        local want = transfer and open or 0
        -- Held steady before the cover moves (live 2026-11-06, field 91: changed 46 times in 80 s, the pipe out
        -- 7 s over the trailer and nothing came in): opened after 1.5 s of transfer, closed after 0.5 s without.
        if want ~= s.coverWant then s.coverWant, s.coverWantAt = want, s.elapsed end
        local held = s.elapsed - (s.coverWantAt or 0) >= (want == 0 and E.coverCloseMs or E.coverOpenMs)
        s.coverState = cover.state
        if held and cover.state ~= want and (type(s.trailer.getIsNextCoverStateAllowed) ~= "function"
                                             or s.trailer:getIsNextCoverStateAllowed(want)) then
            pcall(s.trailer.setCoverState, s.trailer, want)
            s.coverChanges = (s.coverChanges or 0) + 1
        end
    else
        s.noCover = true
    end
    local level = cartLevel(s.trailer)
    if level > s.lastLevel then
        if vc > 1.5 then s.movingL = s.movingL + (level - s.lastLevel) else s.standingL = s.standingL + (level - s.lastLevel) end
        -- The test's evidence (the owner, 2026-11-06: "while actually harvesting, not just driving"): grain
        -- that came while the combine moved threshing on its lane, not in a turn.
        local threshing = type(combine.getIsTurnedOn) == "function" and combine:getIsTurnedOn() == true
        if vc > 1.5 and threshing and not turning then s.harvestingL = (s.harvestingL or 0) + (level - s.lastLevel) end
    end
    s.lastLevel = level
    if level >= E.fullShare * s.capacity then return "TRAILER_FULL" end
    -- Asked once ("come unload my combine"): done when what it held came over and its tank is empty.
    if s.once and (s.harvestingL or 0) + s.movingL > 0 and (s.tankShare or 1) < E.emptyShare then return "COMBINE_EMPTIED" end
    local node = s.tractor:getAISteeringNode()
    local x, wy, z = world(env, node)
    local ta, tl = inFrame(s, x, z)
    local mx, mz = middleOf(env, s.trailer)
    local ma, ml = inFrame(s, mx, mz)
    s.ta, s.tl, s.ma, s.ml, s.vc, s.turning = ta, tl, ma, ml, vc, turning
    if s.elapsed >= s.checkAt then
        s.checkAt = s.elapsed + E.checkMs
        escortSlot(env, s)
        -- The combine now holds a crop this trailer cannot take (it emptied another into it first).
        if not cartTakes(env, s) then return "TRAILER_WRONG_CROP" end
        if s.slotSource == "pipe" and vc > 1.5 then s.pipeOutMovingMs = s.pipeOutMovingMs + E.checkMs end
        s.combineBoxes = boxesOf(env, s, s.combineUnits)
        s.cartBoxes = boxesOf(env, s, units)
        s.rear, s.front = math.huge, -math.huge
        for _, b in ipairs(s.combineBoxes) do s.rear, s.front = math.min(s.rear, b.aMin), math.max(s.front, b.aMax) end
        s.tA, s.tL = escortTarget(s)
        s.close = false
        for _, cb in ipairs(s.cartBoxes) do
            for _, b in ipairs(s.combineBoxes) do
                if bandsMeet(b, cb.aMin, cb.aMax, cb.lMin, cb.lMax, E.clearanceM * 0.5) then s.close = true end
            end
        end
        local others, player = surroundings(env, s)
        s.others, s.player = others, player
        s.conflict = pullConflict(s, { { ta, tl } }, Hitch.LOCAL.vehicleStopM, Hitch.LOCAL.playerStopM)
    end
    if s.tA == nil then s.noSlotMs = s.noSlotMs + dt else s.noSlotMs = 0 end
    if s.noSlotMs > E.noSlotMs then return "NO_SAFE_SLOT" end
    local cartYaw = yawOf(env, s.tractor.rootNode)
    local aligned = math.abs(angleDiff(cartYaw, s.yaw)) < math.rad(E.alignDeg)
    -- It shadows the combine on its trail from the harvest's start and comes alongside only once the tank
    -- holds startShare (the owner, 2026-11-06: unloading from about 60-70 %; waiting for that first and then
    -- sending the cart, it came too late and the combine filled up).
    local share = 1
    pcall(function()
        local node = combine:getCurrentDischargeNode()
        local capacity = combine:getFillUnitCapacity(node.fillUnitIndex)
        if capacity ~= nil and capacity > 0 and capacity < math.huge then
            share = combine:getFillUnitFillLevel(node.fillUnitIndex) / capacity
        end
    end)
    s.tankShare = share
    local wanted = s.tA ~= nil and aligned and share >= s.startShare
        and ((not turning and s.steadyMs >= E.steadyMs) or s.fullStanding)
    if s.mode == "alongside" and not wanted then s.mode = "follow" end
    if s.mode == "follow" and wanted then s.mode = "alongside" end
    local cartFront = -math.huge
    for _, cb in ipairs(s.cartBoxes) do cartFront = math.max(cartFront, cb.aMax) end
    local lookahead = math.max(E.lookaheadM, (s.measuredKmh or 0) / 3.6 * E.lookaheadS)
    local gx, gz, desired
    if s.mode == "alongside" then
        s.alongsideMs = s.alongsideMs + dt
        gx, gz = fromFrame(s, ta + lookahead, s.tL)
        local err = s.tA - ma
        if math.abs(s.tL - tl) > E.onLineM and math.abs(s.tL - ml) > E.onLineM then
            -- Not on the slot's line yet: its front stays behind the combine rig while it gets there.
            err = math.min(err, (s.rear - E.clearanceM - 2) - cartFront)
        end
        s.err = err
        desired = vc + math.max(-vc, math.min(E.catchUpKmh, E.gainKmhPerM * err))
    else
        local i = trailNearest(s, x, z)
        local j = math.min(#s.trail, i + math.ceil(lookahead / E.trailStepM))
        gx, gz = s.trail[j][1], s.trail[j][2]
        if j == #s.trail then gx, gz = fromFrame(s, s.rear - (turning and E.turnGapM or E.followGapM), 0) end
        local gap = s.rear - cartFront                    -- beside or ahead of it: negative, it waits
        local want = turning and E.turnGapM or E.followGapM
        s.err = gap - want
        desired = vc + math.max(-vc - 1, math.min(E.catchUpKmh, E.gainKmhPerM * (gap - want)))
        if gap < want * 0.5 then desired = 0 end
    end
    s.desired = desired
    if s.elapsed >= s.traceAt then
        s.traceAt = s.elapsed + E.traceMs
        if #s.rows < E.traceMax then
            s.rows[#s.rows + 1] = { t = s.elapsed, mode = s.mode, vc = vc, v = s.measuredKmh, err = s.err,
                                    lat = s.tL and (s.tL - ml) or nil, pipe = s.slotSource, level = level,
                                    cover = s.coverState, want = s.coverWant }
        end
    end
    local moved = distance(x, z, s.progressAt[1], s.progressAt[2])
    if moved >= E.stallM or desired < 1.5 then
        s.progressAt, s.stallMs = { x, z }, 0
    else
        s.stallMs = s.stallMs + dt
        if s.stallMs > E.stallMs then return "STALLED" end
    end
    if s.close then brakeFrame(env, s, dt, "close"); return "CONTINUE" end
    if s.conflict == "PERSON_TOO_CLOSE" then brakeFrame(env, s, dt, "person"); return "CONTINUE" end
    if s.conflict == "OTHER_VEHICLE_NEAR" then brakeFrame(env, s, dt, "vehicle"); return "CONTINUE" end
    if desired < 1 then brakeFrame(env, s, dt, s.mode == "follow" and "gap" or "slow"); return "CONTINUE" end
    local tractor = s.tractor
    if not tractor:getIsMotorStarted() then
        if not tractor:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
        tractor:startMotor()
    end
    Hitch.holdActive(s)
    tractor:raiseActive()
    local touched = Hitch.envelope(env, s)
    if touched ~= nil then return touched end
    s.commandKmh = math.min(s.maxSpeedKmh * 0.95, desired)
    local lx, _, lz = env.worldToLocal(node, gx, wy, gz)
    local over = Hitch.drive(env, s, dt, true, lx, lz)
    if over ~= nil then return over end
    return "CONTINUE"
end

-- One update: "CONTINUE", TRAILER_FULL / COMBINE_STOPPED (braked) or a failure. Latched.
function Hitch.escortStep(env, s, dt)
    if s.result ~= nil then
        Hitch.halt(env, s, dt)
        return s.result
    end
    s.elapsed = s.elapsed + dt
    local ok, code = pcall(escortDrive, env, s, dt)
    if not ok then
        s.error = tostring(code)
        code = "UNKNOWN_OBSERVATION"
    end
    if code ~= "CONTINUE" then
        s.result = code
        Hitch.halt(env, s, dt)
        Hitch.escortRestore(s)
    end
    return code
end

-- The cart's cover back as the escort found it (every end, STOP's abort too).
function Hitch.escortRestore(s)
    if s ~= nil and s.coverStart ~= nil and s.trailer ~= nil and type(s.trailer.setCoverState) == "function" then
        pcall(s.trailer.setCoverState, s.trailer, s.coverStart)
        s.coverStart = nil
    end
end

function Hitch.describeEscort(s)
    if s == nil then return "-" end
    local function f(n) return n and string.format("%.2f", n) or "?" end
    local b = s.brakes or {}
    return string.format("mode=%s slot=(%s,%s,%s) target=(%s,%s) trailer=(%s,%s) tractor=(%s,%s) err=%s vc=%s v=%s "
        .. "desired=%s turning=%s steady=%dms alongside=%dms pipeOutMoving=%dms grain=%s->%s moving=%s harvesting=%s standing=%s "
        .. "brakes=close:%d,person:%d,vehicle:%d,gap:%d,slow:%d cover=%s t=%dms%s",
        tostring(s.mode), f(s.slotA), f(s.slotL), tostring(s.slotSource), f(s.tA), f(s.tL), f(s.ma), f(s.ml),
        f(s.ta), f(s.tl), f(s.err), f(s.vc), f(s.measuredKmh), f(s.desired), tostring(s.turning), s.steadyMs or 0,
        s.alongsideMs or 0, s.pipeOutMovingMs or 0, f(s.startLevel), f(s.lastLevel), f(s.movingL), f(s.harvestingL or 0), f(s.standingL),
        b.close or 0, b.person or 0, b.vehicle or 0, b.gap or 0, b.slow or 0,
        s.noCover and "none" or tostring(s.coverChanges or 0), s.elapsed or 0,
        s.error and (" error=" .. s.error) or "")
end

-- ------------------------------------------------------------------ ATTACH

-- Attach only if GIANTS' fresh candidate is exactly the planned pair and GIANTS
-- allows it (Courseplay AttacherJointController.lua:44-49). Returns
-- "ATTACH_REQUESTED" (not success), or NOT_ALIGNED, NOT_STOPPED, RESERVATION_LOST,
-- OCCUPIED, AI_ACTIVE, WAGON_ATTACHED, NO_CANDIDATE, OTHER_CANDIDATE,
-- VERTICAL_MISMATCH, ATTACH_REJECTED, UNKNOWN_OBSERVATION.
function Hitch.attach(env, s)
    if s.result ~= "CANDIDATE" or s.attachRequested then return "NOT_ALIGNED" end
    local ok, code = pcall(function()
        local tractor, wagon = s.tractor, s.wagon
        local denied = authority(s)
        if denied ~= nil then return denied end
        local speed = tractor:getLastSpeed()
        if not finite(speed) or math.abs(speed) >= 1 then return "NOT_STOPPED" end
        local found, info = exactCandidate(env, s)
        if found == nil then return "NO_CANDIDATE" end
        if found ~= "CANDIDATE" then return found end
        local farm = tractor:getActiveFarm()
        if farm ~= s.farmId then return "RESERVATION_LOST" end
        local allowed = wagon:isAttachAllowed(farm, tractor)
        if allowed ~= true then return "ATTACH_REJECTED" end
        s.attachRequested = true
        if tractor:attachImplementFromInfo(info) ~= true then return "ATTACH_REJECTED" end
        s.attachElapsed = 0
        return "ATTACH_REQUESTED"
    end)
    if not ok then return "UNKNOWN_OBSERVATION" end
    return code
end

-- Poll after ATTACH_REQUESTED. "ATTACHED" only when GIANTS lists the wagon as
-- this tractor's implement on exactly the planned joints (implement.jointDescIndex
-- and inputJointDescIndex, AttacherJoints:createAttachmentJoint 2279-2280), its
-- smooth-attach has finished (attachingIsInProgress, set at 2357; Courseplay
-- AttacherJointController.lua:79) and the wagon names the tractor. Otherwise
-- ATTACH_IN_PROGRESS, ATTACH_ABSENT, ATTACH_MISMATCH, ATTACH_TIMEOUT or
-- UNKNOWN_OBSERVATION. The executor still verifies from a fresh snapshot.
function Hitch.attachState(_env, s, dt)
    if not s.attachRequested then return "ATTACH_ABSENT" end
    s.attachElapsed = (s.attachElapsed or 0) + dt
    local ok, code = pcall(function()
        local implement = s.tractor:getImplementByObject(s.wagon)
        if implement == nil then return "ATTACH_ABSENT" end
        if implement.jointDescIndex ~= s.tractorJointIndex
                or implement.inputJointDescIndex ~= s.wagonJointIndex then
            return "ATTACH_MISMATCH"
        end
        if implement.attachingIsInProgress then
            if s.attachElapsed > Hitch.DEFAULTS.attachTimeoutMs then return "ATTACH_TIMEOUT" end
            return "ATTACH_IN_PROGRESS"
        end
        if s.wagon:getAttacherVehicle() ~= s.tractor then return "ATTACH_ABSENT" end
        return "ATTACHED"
    end)
    if not ok then return "UNKNOWN_OBSERVATION" end
    return code
end

-- FS25 discards a sourceFile's return value (see FarmhandObserve.lua).
if FarmhandReloading and type(FarmhandObserve) == "table" and type(FarmhandObserve.adoptState) == "function" then
    FarmhandObserve.adoptState(FarmhandHitch, Hitch)            -- see Observe.adoptState
end
FarmhandHitch = Hitch
return Hitch
