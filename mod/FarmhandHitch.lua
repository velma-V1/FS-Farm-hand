-- Game-side HITCH primitives for one reserved tractor/wagon pair (server only).
--
-- Composition (companion/executor.py owns the order and verifies every step):
--   STAGE          GIANTS AIJobGoTo to the staging pose          (Hitch.startStaging)
--   ALIGN_REVERSE  bounded reverse; steering by GIANTS'
--                  AIVehicleUtil.driveToPoint                      (Hitch.newAlign/alignStep)
--   ATTACH         GIANTS attachableInfo + isAttachAllowed +
--                  attachImplementFromInfo                         (Hitch.attach/attachState)
-- Every game call is cited in adapters/EVIDENCE.md. This is a library only: it is
-- not wired to an update loop or command channel and has never run inside FS25.
-- Game globals are injected as `env` (pass _G in game). Lua 5.1 compatible.

local Hitch = {}

-- Hard caps: the reverse can never become a general navigation mode.
Hitch.LIMITS = { maxSpeedKmh = 3, maxDistanceM = 10, timeoutMs = 60000 }
-- Provisional values; tune at the first FS25 test.
Hitch.DEFAULTS = {
    maxSpeedKmh = 2,         -- very low reverse speed
    timeoutMs = 30000,       -- hard timeout for the whole reverse
    wagonToleranceM = 0.25,  -- the wagon's input joint may not move more than this
    stallMs = 4000,          -- no progress for this long means stalled
    stallProgressM = 0.05,   -- minimum gap reduction that counts as progress
    lookaheadM = 3,          -- steer at a point this far past the joint, on the wagon axis
    attachTimeoutMs = 15000, -- bound on the attach animation
}

local function finite(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

local function distance(ax, az, bx, bz)
    return math.sqrt((ax - bx) ^ 2 + (az - bz) ^ 2)
end

local function worldXZ(env, node)
    local x, _, z = env.getWorldTranslation(node)
    assert(finite(x) and finite(z))
    return x, z
end

-- ------------------------------------------------------------------ STAGE

-- Start GIANTS' own GOTO job towards (x, z, yaw). Job type "GOTO" is registered by
-- AIJobTypeManager (line 35); AIJobGoTo:setValues turns the position/angle
-- parameter into AITaskDriveTo's target (lines 147-157). The start sequence is
-- AutoDrive's (Specialization.lua:1518-1527). Returns true, or false and a reason.
-- A started job is not arrival: the executor verifies the observed pose.
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
        mission.aiSystem:startJob(job, farmId)
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

-- Refresh and read GIANTS' attach candidate for the tractor, exactly as
-- Courseplay does (AttacherJointController.lua:44, 77). Returns info or nil.
local function candidate(env, tractor)
    local joints = env.AttacherJoints
    joints.updateVehiclesInAttachRange(tractor, joints.MAX_ATTACH_DISTANCE_SQ, joints.MAX_ATTACH_ANGLE, true)
    local info = tractor.spec_attacherJoints.attachableInfo
    if info == nil or info.attachable == nil then return nil end
    return info
end

-- The fields GIANTS' attachImplementFromInfo uses (AttacherJoints 1907-1934).
local function isExact(session, info)
    return info.attacherVehicle == session.tractor and info.attachable == session.wagon
        and info.attacherVehicleJointDescIndex == session.tractorJointIndex
        and info.attachableJointDescIndex == session.wagonJointIndex
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
-- maxSpeedKmh and timeoutMs. Returns a session, or nil and a reason.
function Hitch.newAlign(env, args)
    local maxDistance = capped(args.maxDistanceM, nil, Hitch.LIMITS.maxDistanceM)
    local maxSpeed = capped(args.maxSpeedKmh, Hitch.DEFAULTS.maxSpeedKmh, Hitch.LIMITS.maxSpeedKmh)
    local timeout = capped(args.timeoutMs, Hitch.DEFAULTS.timeoutMs, Hitch.LIMITS.timeoutMs)
    if maxDistance == nil or maxSpeed == nil or timeout == nil or type(args.isReserved) ~= "function" then
        return nil, "INVALID_LIMITS"
    end
    local ok, session, reason = pcall(function()
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
        if wagon:getAttacherVehicle() ~= nil then return nil, "WAGON_ATTACHED" end
        local wx, wz = worldXZ(env, wagonJoint.node)
        local tx, tz = worldXZ(env, tractorJoint.jointTransform)
        local fx, _, fz = env.localDirectionToWorld(wagon.rootNode, 0, 0, 1)
        assert(finite(fx) and finite(fz))
        return {
            tractor = tractor, wagon = wagon, farmId = args.farmId, isReserved = args.isReserved,
            tractorJointIndex = args.tractorJoint, wagonJointIndex = args.wagonJoint,
            tractorJoint = tractorJoint, wagonJoint = wagonJoint,
            maxDistanceM = maxDistance, maxSpeedKmh = maxSpeed, timeoutMs = timeout,
            wagonStartX = wx, wagonStartZ = wz, tractorStartX = tx, tractorStartZ = tz,
            dirX = fx, dirZ = fz, elapsed = 0, sinceProgress = 0,
            bestGap = distance(tx, tz, wx, wz), result = nil,
        }
    end)
    if not ok then return nil, "UNKNOWN_OBSERVATION" end
    return session, reason
end

-- Brake: GIANTS driveToPoint with allowedToDrive=false sets zero acceleration and
-- the handbrake (AIVehicleUtil lines 82-94).
function Hitch.halt(env, session, dt)
    pcall(env.AIVehicleUtil.driveToPoint, session.tractor, dt, 0, false, false, 0, 1, 0)
end

local function checkAndDrive(env, s, dt)
    local tractor, wagon = s.tractor, s.wagon
    if s.isReserved() ~= true then return "RESERVATION_LOST" end
    if tractor.ownerFarmId ~= s.farmId or wagon.ownerFarmId ~= s.farmId then return "RESERVATION_LOST" end
    local entered = tractor:getIsEntered()
    if entered ~= false then return entered == true and "OCCUPIED" or "UNKNOWN_OBSERVATION" end
    if wagon:getAttacherVehicle() ~= nil then return "WAGON_ATTACHED" end

    local wx, wz = worldXZ(env, s.wagonJoint.node)
    if distance(wx, wz, s.wagonStartX, s.wagonStartZ) > Hitch.DEFAULTS.wagonToleranceM then
        return "WAGON_MOVED"
    end
    -- GIANTS decides attachability; the instant it names exactly our pair, stop.
    local info = candidate(env, tractor)
    if info ~= nil then return isExact(s, info) and "CANDIDATE" or "OTHER_CANDIDATE" end

    local tx, tz = worldXZ(env, s.tractorJoint.jointTransform)
    if distance(tx, tz, s.tractorStartX, s.tractorStartZ) > s.maxDistanceM then return "DISTANCE_LIMIT" end
    if s.elapsed > s.timeoutMs then return "TIMEOUT" end
    local gap = distance(tx, tz, wx, wz)
    if gap < s.bestGap - Hitch.DEFAULTS.stallProgressM then
        s.bestGap, s.sinceProgress = gap, 0
    else
        s.sinceProgress = s.sinceProgress + dt
        if s.sinceProgress > Hitch.DEFAULTS.stallMs then return "STALLED" end
    end

    -- Keep the vehicle running and active as AutoDrive does (DrivePathModule 380-381).
    if not tractor:getIsMotorStarted() then
        if not tractor:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
        tractor:startMotor()
    end
    tractor:raiseActive()
    -- Steer at a point on the wagon axis past the joint; GIANTS does the steering.
    -- Local target and reverser/steering node as in CpAIWorker.lua:396-409.
    local gx = wx - s.dirX * Hitch.DEFAULTS.lookaheadM
    local gz = wz - s.dirZ * Hitch.DEFAULTS.lookaheadM
    local gy = env.getTerrainHeightAtWorldPos(env.g_terrainNode, gx, 0, gz)
    local node = tractor:getAIReverserNode() or tractor:getAISteeringNode()
    local lx, _, lz = env.worldToLocal(node, gx, gy, gz)
    env.AIVehicleUtil.driveToPoint(tractor, dt, 1, true, false, lx, lz, s.maxSpeedKmh)
    return "CONTINUE"
end

-- One update of the bounded reverse. Returns "CONTINUE", "CANDIDATE" (GIANTS
-- reports exactly the planned joints; the tractor is braked), or a failure:
-- RESERVATION_LOST, OCCUPIED, WAGON_ATTACHED, WAGON_MOVED, OTHER_CANDIDATE,
-- DISTANCE_LIMIT, TIMEOUT, STALLED, MOTOR_UNAVAILABLE, UNKNOWN_OBSERVATION.
-- Any result other than CONTINUE is final: later calls only brake.
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

-- ------------------------------------------------------------------ ATTACH

-- Attach only if GIANTS' fresh candidate is exactly the planned pair and GIANTS
-- allows it (Courseplay AttacherJointController.lua:44-49). Returns
-- "ATTACH_REQUESTED" (not success), or NOT_ALIGNED, NOT_STOPPED, RESERVATION_LOST,
-- NO_CANDIDATE, OTHER_CANDIDATE, ATTACH_REJECTED, UNKNOWN_OBSERVATION.
function Hitch.attach(env, s)
    if s.result ~= "CANDIDATE" or s.attachRequested then return "NOT_ALIGNED" end
    local ok, code = pcall(function()
        local tractor, wagon = s.tractor, s.wagon
        if s.isReserved() ~= true then return "RESERVATION_LOST" end
        local speed = tractor:getLastSpeed()
        if not finite(speed) or math.abs(speed) >= 1 then return "NOT_STOPPED" end
        local info = candidate(env, tractor)
        if info == nil then return "NO_CANDIDATE" end
        if not isExact(s, info) then return "OTHER_CANDIDATE" end
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
-- this tractor's implement, its attach animation has finished
-- (attachingIsInProgress, Courseplay AttacherJointController.lua:79) and the wagon
-- names the tractor. Otherwise ATTACH_IN_PROGRESS, ATTACH_ABSENT, ATTACH_TIMEOUT,
-- UNKNOWN_OBSERVATION. The executor still verifies from a fresh snapshot.
function Hitch.attachState(env, s, dt)
    if not s.attachRequested then return "ATTACH_ABSENT" end
    s.attachElapsed = (s.attachElapsed or 0) + dt
    local ok, code = pcall(function()
        local implement = s.tractor:getImplementByObject(s.wagon)
        if implement == nil then return "ATTACH_ABSENT" end
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

return Hitch
