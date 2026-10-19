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
            dirX = fx, dirZ = fz, elapsed = 0, sinceProgress = 0, trace = {}, sinceTrace = nil,
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
