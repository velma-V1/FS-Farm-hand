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
    maxSpeedKmh = 3,
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
function Hitch.holdActive(s)
    if s.heldActive == nil then
        s.heldActive = s.tractor.forceIsActive ~= true
        if s.heldActive then s.tractor.forceIsActive = true end
    end
end

function Hitch.releaseActive(s)
    if s.heldActive then
        s.heldActive = false
        if s.tractor.forceIsActive == true then s.tractor.forceIsActive = false end
    end
end

function Hitch.halt(env, s, dt)
    pcall(env.AIVehicleUtil.driveToPoint, s.tractor, dt, 0, false, false, 0, 1, s.maxSpeedKmh)
    pcall(function() s.tractor:setCruiseControlState(env.Drivable.CRUISECONTROL_STATE_OFF) end)
    pcall(Hitch.releaseActive, s)
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
    if tractor:getIsEntered() ~= false then return "SEATED_DURING_REVERSE" end   -- someone got in

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
    env.AIVehicleUtil.driveToPoint(tractor, dt, 1, true, false, lx, lz, s.maxSpeedKmh)
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
-- at most 30 closed-form candidates (5 line-up points x at most 6 words), so that
-- it keeps a margin from the work radius, from the wagon's body, from every other
-- vehicle's root and from the player; there is no search around obstacles: no
-- such curve, no motion. Every frame re-observes, steers at a point LOOKAHEAD
-- metres further along the curve, and stops on any deviation, on any other
-- vehicle moving or coming close, or on the player coming close. Forward only,
-- never past the caps; no retry.
Hitch.LOCAL_LIMITS = { workRadiusM = 25, maxPathM = 70, timeoutMs = 180000, minTurnRadiusM = 3,
                       maxTurnRadiusM = 8 }
Hitch.LOCAL = {
    timeoutMs = 120000,       -- the whole maneuver: 70 m at 3 km/h is 84 s
    turnMargin = 1.1,         -- plan 10% wider than the tractor's own minimum radius
    lineupAheadM = { 3.5, 4.5, 5.5, 6.5, 7 },  -- candidate line-up points (planned joint ahead)
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
                      OTHER_VEHICLE_NEAR = "LOCAL_PATH_BLOCKED", PERSON_TOO_CLOSE = "LOCAL_PATH_PERSON" }

-- The shortest candidate that keeps the margin, or nil and why none does: the
-- reason of the shortest candidate refused (live 3da28a3f24dd450b-1 named the
-- longest one's, the work radius, when a parked vehicle blocked every short one).
function Hitch.chooseCurve(s, x, y, p)
    local D = Hitch.LOCAL
    local best, why, whyLength = nil, "LOCAL_NO_PATH", math.huge
    for _, ahead in ipairs(D.lineupAheadM) do
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
            if bad == nil then
                c.lineupAheadM = ahead
                best = c
                break
            end
            if c.length < whyLength then why, whyLength = PLAN_REASON[bad], c.length end
        end
    end
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
-- FarmhandObserve playerPosition). Errors if any position is unreadable.
local function surroundings(env, s)
    local mission = env.g_currentMission
    local vehicles = (mission.vehicleSystem and mission.vehicleSystem.vehicles) or mission.vehicles
    assert(type(vehicles) == "table")
    local others, reach = {}, Hitch.LOCAL_LIMITS.workRadiusM + Hitch.LOCAL.watchExtraM
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
        -- Not an AI job: a seated player's controls would apply alongside (see newAlign).
        if tractor:getIsEntered() == true then return nil, "SEATED_DURING_MANEUVER" end
        local v = tractor:getLastSpeed()
        if not finite(v) or math.abs(v) >= 1 then return nil, "NOT_STOPPED" end
        local radius = turningRadius(tractor)
        if radius == nil then return nil, "TURN_RADIUS_UNKNOWN" end
        local fx, _, fz = env.localDirectionToWorld(wagon.rootNode, 0, 0, 1)
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
    if refused ~= nil then Hitch.note(refused, "REFUSED " .. tostring(reason), 0, 0) end
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
    if s.tractor:getIsEntered() ~= false then return "SEATED_DURING_MANEUVER" end
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
    env.AIVehicleUtil.driveToPoint(tractor, dt, 1, true, true, lx, lz, s.maxSpeedKmh)
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
-- RESERVATION_LOST, DRIVER_TOOK_OVER, SEATED_DURING_MANEUVER, AI_ACTIVE,
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
Hitch.PULL_LIMITS = { maxDistanceM = 8, maxSpeedKmh = 3, timeoutMs = 30000, maxClearM = 6 }
Hitch.PULL = {
    -- Joint separation that ends it (CLEAR): the companion's verified 4 m plus 0.5 m, as
    -- the two measures of a three-point joint differ (live 8d1f4ada61c14239-3: 4.01 m
    -- here from the joint's post-detach point, 3.70 m from GIANTS' load-time offsets).
    clearM = 4.5,
    timeoutMs = 20000,        -- 8 m at 3 km/h is 9.6 s
    lookaheadM = 3,
    divergeM = 0.5,           -- farther than this off the starting line: DIVERGED
    separationSlackM = 0.05,  -- the separation may never shrink more than this: SEPARATION_LOST
}

-- The tractor's checked points at `along` metres on its starting line (x ahead,
-- y lateral, in the start frame): its steering node and frontM ahead of it.
local function pullPoints(along)
    return { { along, 0 }, { along + Hitch.LOCAL.frontM, 0 } }
end

-- nil, "OTHER_VEHICLE_NEAR" or "PERSON_TOO_CLOSE" for the tractor's points
-- `points` (start frame) against s.others / s.player.
local function pullConflict(s, points, vehicleM, playerM)
    for _, pt in ipairs(points) do
        for _, other in ipairs(s.others) do
            if math.sqrt((pt[1] - other.x) ^ 2 + (pt[2] - other.y) ^ 2) < vehicleM then return "OTHER_VEHICLE_NEAR" end
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
        if tractor:getIsEntered() == true then return nil, "SEATED_DURING_PULL_CLEAR" end
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
        local ix = inFrame(s, o.ix, o.iz)
        if ix >= 0 then return nil, "PULL_CLEAR_NOT_BEHIND" end     -- forward would drive into it
        local okAround, others, player = pcall(surroundings, env, s)
        if not okAround then return nil, "LOCAL_AREA_UNKNOWN" end
        s.others, s.player = others, player
        Hitch.watchDriver(env, tractor)
        local denied = authority(s)
        if denied ~= nil then return nil, denied end
        -- The whole straight line, with the maneuver's planning clearances.
        local along = 0
        while along <= maxDistance do
            local bad = pullConflict(s, pullPoints(along), Hitch.LOCAL.vehicleClearanceM,
                                     Hitch.LOCAL.playerClearanceM)
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
    if s.tractor:getIsEntered() ~= false then return "SEATED_DURING_PULL_CLEAR" end
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
    if o.x > s.maxDistanceM then return "DISTANCE_LIMIT" end
    if math.abs(o.y) > P.divergeM then return "DIVERGED" end
    for _, other in ipairs(s.others) do
        local vx, _, vz = world(env, other.vehicle.rootNode)
        if distance(vx, vz, other.wx, other.wz) > D.movedM then return "OTHER_VEHICLE_MOVED" end
    end
    local others, player = surroundings(env, s)
    s.others, s.player = others, player
    local bad = pullConflict(s, pullPoints(o.x), D.vehicleStopM, D.playerStopM)
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
    -- Steer at the starting line lookaheadM ahead (GIANTS driveToPoint, forward).
    local node = tractor:getAISteeringNode()
    local _, wy = env.getWorldTranslation(node)
    local ahead = o.x + P.lookaheadM
    local lx, _, lz = env.worldToLocal(node, s.last.wx + s.dirX * ahead, wy, s.last.wz + s.dirZ * ahead)
    env.AIVehicleUtil.driveToPoint(tractor, dt, 1, true, true, lx, lz, s.maxSpeedKmh)
    return "CONTINUE"
end

-- One update of the pull-clear: "CONTINUE", "CLEAR" (braked: the joints are clearM
-- apart) or a failure: RESERVATION_LOST, DRIVER_TOOK_OVER, SEATED_DURING_PULL_CLEAR,
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
-- callback performed before it returns, a callback returning true ends the query;
-- half extents and the yaw rotation as AutoDrive calls it, Specialization.lua:1909,
-- PathFinderModule.lua:2535), over the whole rig's swept volume: its length, width and
-- height from GIANTS' size boxes (AIDrivable:setAIRootNodeDirty 526-531 reads them),
-- the lateral margin either side, from just above the terrain (the supporting surface)
-- to above its top. Only the rig's own collision nodes and the terrain are excluded;
-- an unreadable or unproven query is never clear. Nothing is made passable.
Hitch.UNPARK_LIMITS = { maxDistanceM = 20, maxSpeedKmh = 3, timeoutMs = 40000 }
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
    maxUnits = 6,
}
-- The masks: everything a moving vehicle can hit (AutoDrive CollSensor.lua:47-48), the
-- static part of it without vehicles (CollSensorSplit.lua:3), vehicles alone (AutoDrive
-- measures its own vehicle with VEHICLE, AutoDrive.lua:218, CollisionDetectionUtils.lua:240)
-- and the terrain (UtilFuncs.lua:74).
Hitch.UNPARK_MASKS = {
    obstacles = { "DEFAULT", "STATIC_OBJECT", "DYNAMIC_OBJECT", "VEHICLE", "TERRAIN_DELTA", "TREE", "BUILDING",
                  "TRAFFIC_VEHICLE", "TRAFFIC_VEHICLE_BLOCKING" },
    static = { "DEFAULT", "STATIC_OBJECT", "DYNAMIC_OBJECT", "TERRAIN_DELTA", "TREE", "BUILDING" },
    vehicles = { "VEHICLE" },
    terrain = { "TERRAIN" },
}

local function unparkMasks(env)
    local out = {}
    for key, names in pairs(Hitch.UNPARK_MASKS) do
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

-- The tractor and everything attached below it (getAttachedImplements entries' .object,
-- as Bridge.logFieldWork), the tractor first. Errors on a loop or a deep chain.
local function rigUnits(tractor)
    local units, set = {}, {}
    local function add(v)
        assert(type(v) == "table" and not set[v] and #units < Hitch.UNPARK.maxUnits)
        set[v] = true
        units[#units + 1] = v
        if type(v.getAttachedImplements) == "function" then
            for _, implement in ipairs(v:getAttachedImplements() or {}) do add(implement.object) end
        end
    end
    add(tractor)
    return units, set
end

-- A point of the start frame (x ahead along the line, y lateral) in the world.
local function fromFrame(s, a, l)
    return s.ox + s.dirX * a + s.dirZ * l, s.oz + s.dirZ * a - s.dirX * l
end

local function terrainAt(env, x, z)
    local mission = env.g_currentMission
    local y = env.getTerrainHeightAtWorldPos(mission.terrainRootNode, x, 0, z)
    assert(finite(y))
    return y
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

-- GIANTS overlapBox over [a0, a1] x [l0, l1] of the start frame, h0..h1 metres above the
-- terrain at its centre, with `mask`. Returns the query: hits (node ids that are neither
-- the rig nor the terrain), own and terrain counts; nil when it cannot be trusted (the
-- shape count GIANTS returns must equal the callbacks performed, GDN overlapBox).
local function overlap(env, s, a0, a1, l0, l1, h0, h1, mask)
    local mission = env.g_currentMission
    local x, z = fromFrame(s, (a0 + a1) / 2, (l0 + l1) / 2)
    local ground = terrainAt(env, x, z)
    local q = { seen = 0, own = 0, terrain = 0, hits = {} }
    function q.onUnparkOverlap(_, nodeId)
        q.seen = q.seen + 1
        if nodeId == mission.terrainRootNode then
            q.terrain = q.terrain + 1
        else
            -- The object a collision node belongs to, as AutoDrive asks (PathFinderModule.lua:3040).
            local object = mission:getNodeObject(nodeId)
            if object ~= nil and (s.rig[object] or (object.rootVehicle ~= nil and s.rig[object.rootVehicle])) then
                q.own = q.own + 1
            else
                q.hits[#q.hits + 1] = nodeId
            end
        end
        return false                 -- true would end GIANTS' query before every shape is seen
    end
    local n = env.overlapBox(x, ground + (h0 + h1) / 2, z, 0, s.yaw, 0, (l1 - l0) / 2, (h1 - h0) / 2, (a1 - a0) / 2,
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
    end
    return table.concat(names, ",")
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
        if tractor:getIsEntered() == true then return "SEATED_DURING_UNPARK" end
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
        local okMasks, masks = pcall(unparkMasks, env)
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
    if s.tractor:getIsEntered() ~= false then return "SEATED_DURING_UNPARK" end
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
    env.AIVehicleUtil.driveToPoint(tractor, dt, 1, true, s.sign > 0, lx, lz, s.maxSpeedKmh)
    return "CONTINUE"
end

-- One update of the extraction: "CONTINUE", "CLEAR" (braked at the exit, in the open) or a
-- failure: RESERVATION_LOST, DRIVER_TOOK_OVER, SEATED_DURING_UNPARK, AI_ACTIVE,
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
