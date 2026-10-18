-- Game-side HITCH primitives for one reserved tractor/wagon pair (server only).
--
-- Composition (companion/executor.py owns the order and verifies every step):
--   STAGE          GIANTS AIJobGoTo to the staging pose          (Hitch.startStaging)
--   ALIGN_REVERSE  bounded reverse; steering by GIANTS'
--                  AIVehicleUtil.driveToPoint                      (Hitch.newAlign/alignStep)
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
    maxSpeedKmh = 3,
    timeoutMs = 30000,       -- hard timeout for the whole reverse
    wagonToleranceM = 0.25,  -- the wagon's input joint may not move more than this
    stallMs = 4000,          -- no progress for this long means stalled
    stallProgressM = 0.05,   -- minimum gap reduction that counts as progress
    lookaheadM = 3,          -- steer at a point this far past the joint, on the wagon axis
    attachTimeoutMs = 15000, -- bound on the attach animation
    -- Farmhand's own maximum hitch-point height difference, independent of the
    -- candidate distance: the attach-range rule derives its vertical tolerance
    -- from that distance (Manual Attach VehicleAttachmentHandler.lua:462,521:
    -- dy^2 < 4*maxDistanceSq), so a larger radius would otherwise loosen it too.
    maxVerticalM = 0.5,
}

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
        maxDistanceM = capped(args.maxDistanceM, nil, L.maxDistanceM),
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
        -- The bounded reverse is not a GIANTS AI job, and for a seated player GIANTS
        -- applies the player's own controls every frame (Drivable:onUpdate 702-747,
        -- getIsVehicleControlledByPlayer): they would fight. The cab must be empty
        -- for these last metres; every AI-driven step still allows a rider.
        if tractor:getIsEntered() == true then return nil, "SEATED_DURING_REVERSE" end
        local speed = tractor:getLastSpeed()
        if not finite(speed) or math.abs(speed) >= 1 then return nil, "NOT_STOPPED" end
        local wx, _, wz = world(env, wagonJoint.node)
        local tx, _, tz = world(env, tractorJoint.jointTransform)
        local fx, _, fz = env.localDirectionToWorld(wagon.rootNode, 0, 0, 1)
        assert(finite(fx) and finite(fz))
        local s = {
            tractor = tractor, wagon = wagon, farmId = args.farmId, isReserved = args.isReserved,
            tractorJointIndex = args.tractorJoint, wagonJointIndex = args.wagonJoint,
            tractorJoint = tractorJoint, wagonJoint = wagonJoint,
            maxDistanceM = lim.maxDistanceM, maxSpeedKmh = lim.maxSpeedKmh, timeoutMs = lim.timeoutMs,
            attachDistanceSq = math.max(vanillaSq, lim.assistRadiusM ^ 2), maxVerticalM = lim.maxVerticalM,
            wagonStartX = wx, wagonStartZ = wz, tractorStartX = tx, tractorStartZ = tz,
            dirX = fx, dirZ = fz, elapsed = 0, sinceProgress = 0,
            bestGap = distance(tx, tz, wx, wz), lastGap = distance(tx, tz, wx, wz), result = nil,
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
function Hitch.halt(env, s, dt)
    pcall(env.AIVehicleUtil.driveToPoint, s.tractor, dt, 0, false, false, 0, 1, s.maxSpeedKmh)
    pcall(function() s.tractor:setCruiseControlState(env.Drivable.CRUISECONTROL_STATE_OFF) end)
end

local function checkAndDrive(env, s, dt)
    local tractor = s.tractor
    local denied = authority(s)
    if denied ~= nil then return denied end

    local wx, wy, wz = world(env, s.wagonJoint.node)
    if distance(wx, wz, s.wagonStartX, s.wagonStartZ) > Hitch.DEFAULTS.wagonToleranceM then
        return "WAGON_MOVED"
    end
    -- GIANTS decides attachability; the instant it names exactly our pair, stop.
    local found = exactCandidate(env, s)
    if found ~= nil then return found end

    local tx, _, tz = world(env, s.tractorJoint.jointTransform)
    if distance(tx, tz, s.tractorStartX, s.tractorStartZ) > s.maxDistanceM then return "DISTANCE_LIMIT" end
    if s.elapsed > s.timeoutMs then return "TIMEOUT" end
    local gap = distance(tx, tz, wx, wz)
    s.lastGap = gap
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
    -- Steer at a point on the wagon axis past the joint, at the joint's height;
    -- GIANTS does the steering. Local target in the reverser node, else the
    -- steering node, as Courseplay (CpAIWorker.lua:396-409).
    local gx = wx - s.dirX * Hitch.DEFAULTS.lookaheadM
    local gz = wz - s.dirZ * Hitch.DEFAULTS.lookaheadM
    local node = tractor:getAIReverserNode() or tractor:getAISteeringNode()
    local lx, _, lz = env.worldToLocal(node, gx, wy, gz)
    env.AIVehicleUtil.driveToPoint(tractor, dt, 1, true, false, lx, lz, s.maxSpeedKmh)
    return "CONTINUE"
end

-- One update of the bounded reverse. Returns "CONTINUE", "CANDIDATE" (GIANTS
-- reports exactly the planned joints; the tractor is braked), or a failure:
-- RESERVATION_LOST, OCCUPIED, AI_ACTIVE, WAGON_ATTACHED, WAGON_MOVED,
-- OTHER_CANDIDATE, VERTICAL_MISMATCH, DISTANCE_LIMIT, TIMEOUT, STALLED,
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
FarmhandHitch = Hitch
return Hitch
