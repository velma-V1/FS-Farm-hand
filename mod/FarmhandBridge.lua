-- AC-001 game-side command bridge (server only). Two parts:
--
-- 1. Operations, shared by the companion and the console probe:
--      navigate GIANTS' navigation agent to a pose under Farmhand's own control, any
--               distance                          (TRAVEL_NEAR and DELIVER)
--      stage    GIANTS GOTO towards the staging pose, only from outside the
--               local work radius                 (Hitch.startStaging)
--      maneuver Farmhand's bounded forward LOCAL_MANEUVER onto the wagon's
--               centreline                        (Hitch.newManeuver / maneuverStep)
--      align  bounded ALIGN_REVERSE               (Hitch.newAlign / alignStep)
--      attach GIANTS attach of the exact candidate (Hitch.attach / attachState)
--      detach GIANTS detach of one attached implement, where GIANTS allows it
--      pull_clear Farmhand's bounded straight pull forward, only straight after
--               that detach                       (Hitch.newPullClear / pullClearStep)
--      fieldwork GIANTS' own field-work helper (AIJobFieldWork): drives to a point
--               inside the field, then works the field it finds there
--      stop   stop the Farmhand-owned motion on a tractor
--    Bridge.start(env, spec) -> task | nil, code[, status]
--    Bridge.step(env, task, dt) -> nil while running, else status, code
--    status: DONE (observed end of the operation, never "arrived" or "attached"
--    on its own: the companion verifies), FAILED, or UNVERIFIED.
--
-- 2. The companion link, following Farmhand Companion's proven pattern
--    (scripts/Transport.lua, PROTOCOL.md): JSON lines out over the companion's
--    named pipe; one command in at a time through an atomically renamed XML
--    file read with the engine XML API; a session file that names the current
--    companion and beats while it is alive. Commands must carry that session
--    and a strictly increasing id; anything else is ignored or REJECTED.
--
-- 3. Player requests, on foot: FARMHAND_FETCH_HERE (AC-001 "here") and FARMHAND_STOP
--    are fixed keys; FARMHAND_TALK opens GIANTS' text dialog and sends the typed
--    words. Each is a `request` frame; the companion interprets and answers through
--    the session file's notice, shown as an in-game notification.
--
-- Every game and mod call is cited in adapters/EVIDENCE.md. Lua 5.1 compatible.

local Bridge = {}

Bridge.MOD_DIR = "modSettings/FS25_Farmhand/"
Bridge.COMMAND_FILE = "command.xml"
Bridge.SESSION_FILE = "session.xml"
Bridge.MENU_FILE = "menu.xml"           -- companion's list for the player (protocol.ipc encode_menu)
Bridge.SNAPSHOT_MS = 500
Bridge.SESSION_CHECK_MS = 1000
Bridge.COMPANION_TIMEOUT_MS = 10000   -- no heartbeat for this long: stop, drop session
Bridge.PIPE_RETRY_MS = 5000
Bridge.START_TIMEOUT_MS = 5000        -- a job not running by then was refused
Bridge.SETTLE_TIMEOUT_MS = 5000       -- an ended job must come to rest within this
Bridge.STAGE_TIMEOUT_MS = 120000
Bridge.DETACH_TIMEOUT_MS = 10000      -- GIANTS' detach (lowering arms) must end within this
Bridge.PULL_AFTER_DETACH_MS = 30000   -- a pull-clear only this soon after its detach
Bridge.FIELDWORK_TIMEOUT_MS = 7200000 -- 2 h: a small owned field takes minutes
Bridge.FIELDWORK_STALL_MS = 300000    -- running but unmoved (< 2 m) for 5 min: stopped
Bridge.FIELDWORK_CHECK_MS = 3000      -- a bounded test area is re-measured this often before work starts
Bridge.FIELDWORK_WORKING_CHECK_MS = 250 -- and this often once it works (live fb3522a3e1d64f75-1: 0 -> 1244 m2 in 3 s)
Bridge.FIELDWORK_AREA_SHARE = 0.97     -- stopped at the first reading this share of the request (companion: 0.95)
Bridge.FIELDWORK_MAX_AREA_M2 = 20000  -- the largest bounded test area
-- Travel (GIANTS' agent, GOTO, Deliver) is judged by progress: each has its
-- own unmoved watch (NAVIGATE_STALL_MS, DELIVER_STALL_MS); this is only a safety
-- bound, long enough for a 16x map (the user, 2026-10-22: a healthy long drive must not fail on a
-- short fixed timeout; was 20 min).
Bridge.DRIVE_TIMEOUT_MS = 14400000
-- A navigate whose tractor gains no NAVIGATE_PROGRESS_M for NAVIGATE_STALL_MS is
-- stopped: GIANTS keeps a BLOCKED agent waiting (AIDrivable:onUpdate 314-316).
Bridge.NAVIGATE_STALL_MS = 90000
Bridge.NAVIGATE_PROGRESS_M = 2
-- GIANTS' agent BLOCKED this long while the vehicle never left where the job started:
-- it cannot start from there (live 53c77d1e809645b3-3: inside the Large Shed, 94 s). As
-- long as the no-progress watchdog, so a passing blocker is still waited for as before.
Bridge.BLOCKED_START_MS = Bridge.NAVIGATE_STALL_MS
Bridge.STOP_TIMEOUT_MS = 15000
Bridge.REQUEST_COOLDOWN_MS = 2000      -- a held or repeated fetch key sends one request
Bridge.MAX_TEXT = 200                   -- protocol/frame.schema.json request.text

local function log(fmt, ...)
    local ok, msg = pcall(string.format, fmt, ...)
    print("[Farmhand] " .. (ok and msg or tostring(fmt)))
end

local function finite(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

local atan2 = math.atan2 or math.atan   -- Lua 5.1 (FS25) / 5.4 (tests)

local function stopped(v)
    local observe, env = Bridge.Observe, FarmhandBridge and FarmhandBridge.env
    if observe ~= nil and type(observe.atRest) == "function" then return observe.atRest(env, v) end
    local speed = v:getLastSpeed()
    return finite(speed) and math.abs(speed) < 1
end

-- Before any motion: owned, no AI job of any kind, at rest. The requester may sit
-- in it (ride-along, Hitch.watchDriver); from here on a takeover by the seated
-- player ends the operation.
local function idle(env, v, farmId)
    if v.ownerFarmId ~= farmId then return "NOT_OWNED" end
    local entered = v:getIsEntered()
    if entered ~= true and entered ~= false then return "UNKNOWN_OBSERVATION" end
    local ai = v:getIsAIActive()
    if ai ~= false then return ai == true and "AI_ACTIVE" or "UNKNOWN_OBSERVATION" end
    if not stopped(v) then
        local observe = Bridge.Observe
        if observe ~= nil and type(observe.atRest) == "function" then
            local _, reading, travelled = observe.atRest(env, v)
            log("not at rest: GIANTS reads %s km/h, root travel %s km/h", tostring(reading), tostring(travelled))
        end
        return "NOT_STOPPED"
    end
    Bridge.Hitch.watchDriver(env, v)
    return nil
end

-- While a job we started runs: still ours, and the seated player (if any) has
-- not taken the controls.
local function stillOurs(spec)
    local v = spec.tractor
    if spec.owned() ~= true or v.ownerFarmId ~= spec.farmId then return "FAILED", "RESERVATION_LOST" end
    local conflict = Bridge.Hitch.driverConflict(v)
    if conflict ~= nil then return conflict == "DRIVER_TOOK_OVER" and "FAILED" or "UNVERIFIED", conflict end
    return nil
end

-- An ended job is DONE only once the tractor is observed at rest.
local function settle(task, code)
    task.endedAt = task.endedAt or task.elapsed
    if stopped(task.spec.tractor) then return "DONE", code end
    if task.elapsed - task.endedAt > Bridge.SETTLE_TIMEOUT_MS then return "FAILED", "NOT_STOPPED" end
    return nil
end

local starters, steppers, aborts = {}, {}, {}

-- A self-driven motion's end: its own description, the speed it kept (Hitch.describeSpeed:
-- the limit, Farmhand's measured peak, GIANTS' reading, the governor's frames), the rig's
-- observed envelope (Hitch.describeEnvelope) and the speed trace, for the record.
local function logMotionEnd(op, code, session, describe)
    log("%s end %s %s %s %s", op, code, describe(session), Bridge.Hitch.describeSpeed(session),
        Bridge.Hitch.describeEnvelope(session))
    for i, line in ipairs(Bridge.Hitch.speedTrace(session, 8)) do log("%s trace %d %s", op, i, line) end
end
-- A running motion pre-empted (STOP, a lost session): braked by the latch, and its record
-- logged as at any other end (live 902c1b52e05742ec-1: a STOPPED LOAD left none).
local function abortMotion(op, env, task, describe)
    Bridge.Hitch.abort(env, task.session, "STOPPED")
    if task.session ~= nil then logMotionEnd(op, "STOPPED", task.session, describe) end
end
local gateCandidates          -- (open_gates) also read by unpark
Bridge.pendingGoto = {}       -- tractor -> ms left to watch an aborted, unseen GOTO

-- GIANTS WheelDestruction:update uses WheelDestruction.isCareWheel to decide
-- whether a contacting wheel destroys fruit (installed FS25 1.25). During a
-- Farmhand transport drive, give only this rig's wheels that native care-wheel
-- behavior; restore every original value on completion, failure, or STOP.
function Bridge.protectTransportCrops(root)
    local saved, seen = {}, {}
    local function visit(v)
        if v == nil or seen[v] then return end
        seen[v] = true
        for _, wheel in ipairs(v.spec_wheels ~= nil and v.spec_wheels.wheels or {}) do
            local destruction = wheel.destruction
            if destruction ~= nil and type(wheel.setIsCareWheel) == "function" then
                saved[#saved + 1] = { wheel, destruction.isCareWheel }
                wheel:setIsCareWheel(true)
            end
        end
        for _, implement in ipairs(type(v.getAttachedImplements) == "function" and v:getAttachedImplements() or {}) do
            visit(implement.object)
        end
        local belts = v.spec_tensionBelts
        for _, data in pairs(belts ~= nil and belts.objectsToJoint or {}) do visit(data.object) end
    end
    visit(root)
    log("transport crop protection active on %d wheel(s)", #saved)
    return saved
end

function Bridge.restoreTransportCrops(saved)
    local restored = 0
    for _, state in ipairs(saved or {}) do
        local ok = pcall(state[1].setIsCareWheel, state[1], state[2])
        if ok and state[1].destruction ~= nil
                and state[1].destruction.isCareWheel == state[2] then restored = restored + 1 end
    end
    if saved ~= nil then log("transport crop protection restored %d/%d wheel(s)", restored, #saved) end
end

-- Farmhand's own point-to-point drive. It searches from the observed collision
-- world and steers the rig itself; no marker or provider job is required.
function starters.farm_drive(env, spec)
    local denied = idle(env, spec.tractor, spec.farmId)
    if denied ~= nil then return nil, denied end
    local session, reason = Bridge.Drive.begin(env, {
        tractor = spec.tractor, x = spec.x, z = spec.z, area = spec.area, yaw = spec.yaw,
        via = spec.via,
        farmId = spec.farmId, isReserved = spec.owned,
        ghost = spec.ghost == 1,      -- the player's opt-in scenery setting, this drive only
    }, Bridge.Hitch)
    if session == nil then Bridge.Hitch.disarm(spec.tractor); return nil, reason end
    Bridge.lastDrive = session
    return { session = session, cropWheels = Bridge.protectTransportCrops(spec.tractor) }
end

function steppers.farm_drive(env, task, dt)
    local code = Bridge.Drive.step(env, task.session, dt, Bridge.Hitch)
    if code == "CONTINUE" then return nil end
    Bridge.restoreTransportCrops(task.cropWheels)
    task.cropWheels = nil
    if not task.endLogged then
        task.endLogged = true
        logMotionEnd("farm_drive", code, task.session, function(s)
            return string.format("target=(%.1f,%.1f) route=%d", s.targetX, s.targetZ, #(s.path or {}))
        end)
    end
    if code ~= "ARRIVED" then return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code end
    return settle(task, code)
end

function aborts.farm_drive(env, task)
    Bridge.Drive.abort(env, task.session, Bridge.Hitch)
    if task.session ~= nil and not task.endLogged then
        -- A stopped drive keeps its record too (its speed trace: what the governor commanded and observed).
        task.endLogged = true
        pcall(logMotionEnd, "farm_drive", "STOPPED", task.session, function(s)
            return string.format("target=(%.1f,%.1f) route=%d held=%d", s.targetX, s.targetZ, #(s.path or {}),
                                 s.heldFrames or 0)
        end)
    end
    Bridge.restoreTransportCrops(task.cropWheels)
    task.cropWheels = nil
end

-- Farmhand's travel speed limit (the user, 2026-10-22: 40 mph): a drive's peak is judged against it.
Bridge.TRAVEL_LIMIT_KMH = 40 * 1.609344

-- A task's end hook (once).
local function finish(task)
    if task.ended then return end
    task.ended = true
    if task.onEnd ~= nil then pcall(task.onEnd) end
    if task.peakKmh ~= nil and task.peakKmh >= 5 then
        log("%s peak travel %.1f km/h%s", tostring(task.op), task.peakKmh,
            task.peakKmh > Bridge.TRAVEL_LIMIT_KMH and string.format(" OVER Farmhand's %.1f (40 mph)", Bridge.TRAVEL_LIMIT_KMH)
            or "")
    end
end

-- ------------------------------------------------------- stage, navigate
-- Both run GIANTS' GOTO job (Hitch.startStaging). Its start creates the vehicle's
-- navigation agent, which first collects the attached implements, so a coupled
-- wagon is part of the planned path (AIDrivable:createAgent 360-372). The agent
-- ends the job itself when the target is NOT_REACHABLE (AIDrivable:onUpdate
-- 320-322). An ended job is not arrival: the companion verifies the pose.
local function startGoto(env, spec, prefix)
    local denied = idle(env, spec.tractor, spec.farmId)
    if denied ~= nil then return nil, denied end
    local ok, reason = Bridge.Hitch.startStaging(env, spec.tractor, spec.farmId, spec.x, spec.z, spec.yaw)
    if not ok then return nil, prefix .. tostring(reason) end
    return { seen = false, started = true }
end

-- Diagnostics only, never a decision: the navigation agent's last state (DRIVING,
-- PLANNING, BLOCKED, TARGET_REACHED, NOT_REACHABLE) and its distance to the target,
-- kept in spec_aiDrivable during and after the job (AIDrivable:onUpdate 277, 317-325;
-- unsetAITarget 496-505 resets neither).
local function logAgent(env, v, what)
    pcall(function()
        local spec = v.spec_aiDrivable
        local names = env.AIDrivable and env.AIDrivable.STATES
        local left = spec.distanceToTarget
        log("%s: agent state %s, %s m from its target", what,
            tostring(names and names[spec.lastState] or spec.lastState),
            finite(left) and string.format("%.1f", left) or "?")
    end)
end

-- Whether GIANTS' agent reports BLOCKED (live casing is lower case).
local function agentBlocked(env, v)
    return pcall(function()
        assert(tostring(env.AIDrivable.STATES[v.spec_aiDrivable.lastState]):upper() == "BLOCKED")
    end)
end

-- Whether GIANTS' agent is still planning (no way found yet).
Bridge.HELPER_PLANNING_MS = 30000
local function agentPlanning(env, v)
    return pcall(function()
        assert(tostring(env.AIDrivable.STATES[v.spec_aiDrivable.lastState]):upper() == "PLANNING")
    end)
end

-- GIANTS' field worker holding for something in its collision triggers (AIFieldWorker).
local function fieldWorkerBlocked(v)
    return pcall(function() assert(v:getAIFieldWorkerIsBlocked() == true) end)
end

-- The player on foot within `radius` m of x, z (seated in a vehicle is not on foot).
Bridge.HELPER_PLAYER_M = 20
local function playerOnFootNear(env, x, z, radius)
    local ok, near = pcall(function()
        local player = env.g_localPlayer
        if player == nil or (type(player.getIsInVehicle) == "function" and player:getIsInVehicle() == true) then
            return false
        end
        local px, _, pz
        if type(player.getPosition) == "function" then px, _, pz = player:getPosition()
        else px, _, pz = env.getWorldTranslation(player.rootNode) end
        return finite(px) and finite(pz) and math.sqrt((px - x) ^ 2 + (pz - z) ^ 2) <= radius
    end)
    return ok and near == true
end

-- Ends the job (prefix .. BLOCKED_AT_START) once GIANTS' agent has reported BLOCKED for
-- BLOCKED_START_MS while the vehicle never got NAVIGATE_PROGRESS_M from where the job
-- started; the companion then looks for a closed door or a way out (UNPARK).
local function blockedAtStart(env, task, v, prefix)
    local x, _, z = env.getWorldTranslation(v.rootNode)
    if not (finite(x) and finite(z)) then return nil end
    if task.startX == nil then task.startX, task.startZ = x, z end
    if task.leftStart or math.sqrt((x - task.startX) ^ 2 + (z - task.startZ) ^ 2) >= Bridge.NAVIGATE_PROGRESS_M then
        task.leftStart, task.blockedSince = true, nil
        return nil
    end
    if not agentBlocked(env, v) then task.blockedSince = nil; return nil end
    task.blockedSince = task.blockedSince or task.elapsed
    if task.elapsed - task.blockedSince < Bridge.BLOCKED_START_MS then return nil end
    logAgent(env, v, task.op .. " blocked at its start")
    return prefix .. "BLOCKED_AT_START"
end

-- progress: whether a lack of movement ends the job (navigate only; a stage is
-- short and bounded by its own timeout).
local function stepGoto(env, task, timeoutMs, timeoutCode, endCode, progress, prefix)
    local status, code = stillOurs(task.spec)
    if status ~= nil then return status, code end
    local v = task.spec.tractor
    local active = v:getIsAIActive()
    if active == true then task.seen = true end
    if not task.seen then
        if task.elapsed > Bridge.START_TIMEOUT_MS then return "FAILED", "GOTO_NOT_STARTED" end
        return nil
    end
    if active ~= false then
        if active ~= true then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
        if task.elapsed > timeoutMs then return "FAILED", timeoutCode end
        local stuck = blockedAtStart(env, task, v, prefix)
        if stuck ~= nil then return "FAILED", stuck end
        if progress then
            local x, _, z = env.getWorldTranslation(v.rootNode)
            if not (finite(x) and finite(z)) then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
            if task.fromX == nil or math.sqrt((x - task.fromX) ^ 2 + (z - task.fromZ) ^ 2)
                    >= Bridge.NAVIGATE_PROGRESS_M then
                task.fromX, task.fromZ, task.fromAt = x, z, task.elapsed
            elseif task.elapsed - task.fromAt > Bridge.NAVIGATE_STALL_MS then
                logAgent(env, v, task.op .. " stalled")
                -- Never left its start and GIANTS' agent says BLOCKED: it cannot start from
                -- there (live fc25907b61fb402f-1: the agent planned first, so this watch
                -- fired before BLOCKED_START_MS of uninterrupted BLOCKED had passed).
                if not task.leftStart and agentBlocked(env, v) then return "FAILED", prefix .. "BLOCKED_AT_START" end
                -- The agent still drives (it has a path) yet nothing moves: something
                -- physical holds the vehicle (live: a closed gate GIANTS' map treats as
                -- open road). A BLOCKED agent waits for an obstacle it knows.
                local driving = pcall(function()
                    -- GIANTS' names are lower case live ("agent state driving").
                    assert(tostring(env.AIDrivable.STATES[v.spec_aiDrivable.lastState]):upper() == "DRIVING")
                end)
                return "FAILED", driving and "NAVIGATE_STUCK" or "NAVIGATE_STALLED"
            end
            if task.cargo ~= nil and #task.cargo > 0 and not task.leftStart
                    and task.fromAt ~= nil and task.elapsed - task.fromAt > 6000 then
                log("navigate: loaded rig did not leave its start in 6 s; Farmhand departs it")
                return "FAILED", "NAVIGATE_STUCK_AT_START"
            end
        end
        return nil
    end
    if not task.endLogged then
        -- Diagnostics only: why GIANTS ended it. The agent's last state (TARGET_REACHED,
        -- or NOT_REACHABLE, which ends the job with AIMessageErrorNotReachable) and its
        -- distance to the target, both left in spec_aiDrivable after the job
        -- (AIDrivable:onUpdate 277, 317-325; unsetAITarget 496-505 resets neither).
        task.endLogged = true
        logAgent(env, v, task.op .. " ended")
    end
    return settle(task, endCode)
end

-- Cancel the GOTO this task started (GIANTS AIJobVehicle 431). idle() saw no AI
-- before the start, so AI that is active now is ours. A job not yet seen running
-- may still start within START_TIMEOUT_MS: it is watched and cancelled then
-- (Bridge.watchGoto), so an aborted GOTO can never drive unowned later.
local function abortGoto(env, task)
    local v = task.spec.tractor
    if v:getIsAIActive() == true then
        Bridge.Hitch.cancelStaging(env, v)
    elseif not task.seen and task.elapsed < Bridge.START_TIMEOUT_MS then
        Bridge.pendingGoto[v] = Bridge.START_TIMEOUT_MS - task.elapsed
    end
end

-- One frame of the watch over aborted, not yet running GOTO jobs.
function Bridge.watchGoto(env, dt)
    for v, left in pairs(Bridge.pendingGoto) do
        local ok, active = pcall(function() return v:getIsAIActive() end)
        if ok and active == true then
            log("an aborted GOTO started late; cancelled")
            Bridge.Hitch.cancelStaging(env, v)
            Bridge.pendingGoto[v] = nil
        elseif left - dt <= 0 then
            Bridge.pendingGoto[v] = nil
        else
            Bridge.pendingGoto[v] = left - dt
        end
    end
end

-- Belted cargo out of GIANTS' AI obstacles while Farmhand's GOTO drives the loaded rig: a
-- vehicle strapped on the towed trailer registers its own components as AI obstacles
-- (AIVehicleObstacle:createAIVehicleObstacle -> aiSystem:addObstacle per component), so the
-- towing agent is BLOCKED from its first frame (live d14230aa9fb24d30-3, abe8eb41ca6f4b5f-1).
-- AIVehicleObstacle:removeAIVehicleObstacle takes them out (re-applied each frame, in case GIANTS
-- re-creates them); setAIVehicleObstacleStateDirty lets GIANTS restore them by its own rules on
-- every end, stop or abort.
function Bridge.beltedCargo(v)
    local out, seen = {}, {}
    local function add(u)
        local belts = u.spec_tensionBelts
        for _, data in pairs(belts ~= nil and belts.objectsToJoint or {}) do
            local o = data.object
            if o ~= nil and not seen[o] and type(o.removeAIVehicleObstacle) == "function" then
                seen[o] = true
                out[#out + 1] = o
            end
        end
        for _, implement in ipairs(type(u.getAttachedImplements) == "function" and u:getAttachedImplements() or {}) do
            add(implement.object)
        end
    end
    add(v)
    return out
end

local function cargoObstaclesOff(task)
    for _, o in ipairs(task.cargo or {}) do pcall(o.removeAIVehicleObstacle, o) end
end

local function cargoObstaclesBack(task)
    for _, o in ipairs(task.cargo or {}) do
        if type(o.setAIVehicleObstacleStateDirty) == "function" then pcall(o.setAIVehicleObstacleStateDirty, o) end
    end
    task.cargo = nil
end

-- ------------------------------------------------- navigate: GIANTS' agent, Farmhand's drive
-- GIANTS' engine navigation agent plans over the map's navigation mesh for the whole rig
-- (createVehicleNavigationAgent with its trailers' data, AIDrivable:createAgent) and gives one
-- curvature, a speed and a state per frame (getVehicleNavigationAgentNextCurvature). GIANTS' own
-- controller (AIDrivable:onUpdate) applies it only while an AI job runs and gives up on its own
-- terms. Farmhand runs that loop itself: no job or hired helper, its speed limit, its watch for
-- a rig that is commanded yet does not move (live 47e8e973c13140c0-2: GIANTS' job crept 0.3 m
-- in 80 s against scenery until its 90 s watchdog), and its own end on every exit path.
Bridge.AGENT = { maxKmh = 64, stuckMs = 8000, stuckM = 1.0, blockedMs = 20000, planningMs = 30000 }
local function agentState(env, state)
    local names = env.AIDrivable and env.AIDrivable.STATES
    return tostring(names and names[state] or state):upper()
end

local function agentEnd(env, task, dt)
    local v = task.spec.tractor
    if task.agentMade and (task.replans or 0) > 0 then
        log("%s: GIANTS' agent replanned %d times on the way", task.op or "navigate", task.replans)
    end
    pcall(Bridge.Hitch.halt, env, task.drive, dt or 0)
    if task.agentMade then pcall(v.deleteAgent, v); task.agentMade = nil end
    if task.cruiseBefore ~= nil then
        pcall(v.setCruiseControlMaxSpeed, v, task.cruiseBefore)
        task.cruiseBefore = nil
    end
end

-- GIANTS' agent for the vehicle (AIDrivable:createAgent). `cap` > 0 (REACH only, a diagnostic probe
-- that never drives): planned at most that wide, never narrower than the vehicle's own agent. GIANTS sizes it by the widest attachment
-- (getAIAgentSize, static XML widths: the MF 8570's header 6.80 m on a 3.99 m combine, live
-- 2026-11-05), wider than the 6 m its roads default to (AISystem:addRoadSpline), so no route
-- crosses the map. A header overhangs a road's edges for a player too; Farmhand's own watch
-- (contact, stuck) supervises the drive.
-- A front offset behind the AI root (the MF 8570: -3.70 m, live 2026-11-05; the Series 9 +3.26) is
-- taken as that far ahead: GIANTS' planner refused every target for it within two frames.
function Bridge.frontOffset(fo)
    if finite(fo) and fo < 0 then return -fo end
    return fo
end

function Bridge.createAgent(v, cap)
    if not (finite(cap) and cap > 0) then return v:createAgent() end
    if cap < 1 then
        -- Under a metre: the vehicle alone, none of its attachments in the agent (what it carries is
        -- Farmhand's own watch's to supervise).
        local ownCollect = rawget(v, "collectAIAgentAttachments")
        local ownSize = rawget(v, "getAIAgentSize")
        local base = v.getAIAgentSize
        v.collectAIAgentAttachments = function() end
        -- And under the 4 m GIANTS builds its navigation map for (AISystem createVehicleNavigationMap,
        -- vehicleMaxHeight): a combine taller than that finds no cell to drive in.
        v.getAIAgentSize = function(self, ...)
            local w, l, lo, fo, h = base(self, ...)
            return w, l, lo, Bridge.frontOffset(fo), math.min(h or 0, 3.9)
        end
        local ok, err = pcall(v.createAgent, v)
        v.collectAIAgentAttachments = ownCollect
        v.getAIAgentSize = ownSize
        if not ok then error(err) end
        log("agent planned for the vehicle alone, under 3.9 m")
        return
    end
    local own = rawget(v, "getAIAgentSize")
    local base = v.getAIAgentSize
    v.getAIAgentSize = function(self, ...)
        local w, l, lo, fo, h = base(self, ...)
        local floor = self.spec_aiDrivable and self.spec_aiDrivable.agentInfo and self.spec_aiDrivable.agentInfo.width or 0
        return math.min(w, math.max(cap, floor)), l, lo, Bridge.frontOffset(fo), h
    end
    local ok, err = pcall(v.createAgent, v)
    v.getAIAgentSize = own
    if not ok then error(err) end
    log("agent planned at most %.2f m wide", cap)
end

local function startAgent(env, spec)
    local v = spec.tractor
    local denied = idle(env, v, spec.farmId)
    if denied ~= nil then return nil, denied end
    local ai = v.spec_aiDrivable
    if ai == nil or type(v.createAgent) ~= "function" or type(env.getVehicleNavigationAgentNextCurvature) ~= "function"
            or not (ai.agentInfo ~= nil and ai.agentInfo.isValid) or ai.agentId ~= nil then
        return nil, "NO_AGENT"
    end
    local task = { seen = true, started = true, drive = { tractor = v, maxSpeedKmh = 0 } }
    local ok, why = pcall(function()
        local cruise = v.spec_drivable and v.spec_drivable.cruiseControl
        task.cruiseBefore = cruise and cruise.speed
        Bridge.createAgent(v, spec.op == "reach" and spec.area or nil)
        task.agentMade = true
        local mission = env.g_currentMission
        local y = env.getTerrainHeightAtWorldPos(mission.terrainRootNode, spec.x, 0, spec.z)
        local dx, dz = 0, 0
        if finite(spec.yaw) and math.abs(spec.yaw) <= 2 * math.pi then dx, dz = math.sin(spec.yaw), math.cos(spec.yaw) end
        env.setVehicleNavigationAgentTarget(ai.agentId, spec.x, y, spec.z, dx, 0, dz)
    end)
    if not ok then
        log("navigate: GIANTS' agent could not be made (%s)", tostring(why))
        agentEnd(env, task, 0)
        return nil, "NO_AGENT"
    end
    return task
end

-- One frame of Farmhand's drive along GIANTS' agent. Codes as GIANTS' GOTO had them: DONE
-- GOTO_ENDED when the agent reaches its target or finds it NOT_REACHABLE (the companion judges
-- the arrival), NAVIGATE_BLOCKED_AT_START / NAVIGATE_STALLED for a BLOCKED agent, NAVIGATE_STUCK
-- for a rig commanded to drive that does not move.
-- `codes`: the op's own (Bridge.AGENT_CODES).
Bridge.AGENT_CODES = {
    navigate = { ended = "GOTO_ENDED", timeoutMs = "DRIVE_TIMEOUT_MS", timeout = "TIMEOUT",
                 stuck = "NAVIGATE_STUCK", stuckAtStart = "NAVIGATE_STUCK_AT_START",
                 blocked = "NAVIGATE_STALLED", blockedAtStart = "NAVIGATE_BLOCKED_AT_START" },
    stage = { ended = "STAGE_ENDED", timeoutMs = "STAGE_TIMEOUT_MS", timeout = "STAGE_TIMEOUT",
              -- unmoved from its start: ended, as GIANTS' NOT_REACHABLE (the hitch then takes
              -- the aim by Farmhand's own drive, live a34770b22e2a46fa-2)
              stuck = "NAVIGATE_STUCK", stuckAtStart = "STAGE_ENDED",
              blocked = "NAVIGATE_STALLED", blockedAtStart = "STAGE_BLOCKED_AT_START" },
}
local function stepAgent(env, task, dt, codes)
    local v = task.spec.tractor
    local status, code = stillOurs(task.spec)
    if status ~= nil then agentEnd(env, task, dt); return status, code end
    if task.ended then return settle(task, task.ended) end
    if v:getIsAIActive() ~= false then agentEnd(env, task, dt); return "FAILED", "AI_ACTIVE" end
    if task.elapsed > Bridge[codes.timeoutMs] then agentEnd(env, task, dt); return "FAILED", codes.timeout end
    local ai = v.spec_aiDrivable
    local x, _, z = env.getWorldTranslation(v.rootNode)
    if not (finite(x) and finite(z)) then agentEnd(env, task, dt); return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
    if task.startX == nil then task.startX, task.startZ = x, z end
    if not task.leftStart and math.sqrt((x - task.startX) ^ 2 + (z - task.startZ) ^ 2) >= Bridge.NAVIGATE_PROGRESS_M then
        task.leftStart = true
    end
    v:updateAIAgentPoseData()
    pcall(function() ai.distanceToTarget = math.sqrt((x - task.spec.x) ^ 2 + (z - task.spec.z) ^ 2) end)
    local speed = math.abs((v.lastSpeedReal or 0) * 1000)
    local curvature, curveMps, state = env.getVehicleNavigationAgentNextCurvature(ai.agentId, ai.poseData, speed)
    ai.lastState = state                                   -- the diagnostics (logAgent) read it here
    local name = agentState(env, state)
    if name ~= task.lastName then
        -- Its replanning flickers driving/planning (live 77e30fc15938406c-1: every frame for
        -- seconds); those are counted, every other change is logged.
        local flicker = (name == "PLANNING" and task.lastName == "DRIVING")
                        or (name == "DRIVING" and task.lastName == "PLANNING" and task.drove)
        if flicker then task.replans = (task.replans or 0) + (name == "PLANNING" and 1 or 0)
        else log("%s: GIANTS' agent %s at %.1f,%.1f", task.op, name:lower(), x, z) end
        task.lastName, task.stateSince = name, task.elapsed
    end
    local function finish(endCode)
        agentEnd(env, task, dt)
        task.ended = endCode
        return settle(task, endCode)
    end
    if name == "DRIVING" and finite(curvature) then
        local cap = math.min(Bridge.AGENT.maxKmh, task.cruiseBefore or Bridge.AGENT.maxKmh)
        local kmh = math.min(finite(curveMps) and curveMps * 3.6 or cap, cap)
        if not v:getIsMotorStarted() then
            if not v:getCanMotorRun() then return finish("MOTOR_UNAVAILABLE") end
            v:startMotor()
        end
        Bridge.Hitch.holdActive(task.drive)
        v:raiseActive()
        env.AIVehicleUtil.driveAlongCurvature(v, dt, curvature, kmh, 1)
        task.drove = true
        if kmh <= 1 then task.fromAt = task.elapsed end      -- the agent itself asks it to wait
    else
        Bridge.Hitch.halt(env, task.drive, dt)
    end
    -- Driven along the agent, and through its replanning (live a34770b22e2a46fa-2: planning and
    -- driving by turns every 5 s, unmoved 33 s before NOT_REACHABLE), yet the root does not move.
    if task.fromX == nil or math.sqrt((x - task.fromX) ^ 2 + (z - task.fromZ) ^ 2) >= Bridge.AGENT.stuckM then
        task.fromX, task.fromZ, task.fromAt = x, z, task.elapsed
    elseif task.drove and (name == "DRIVING" or name == "PLANNING")
            and task.elapsed - (task.fromAt or task.elapsed) > Bridge.AGENT.stuckMs then
        local contacts = "?"
        pcall(function() contacts = Bridge.Hitch.rigContacts(env, v) end)
        log("%s: driven along GIANTS' agent but unmoved %.0f s at %.1f,%.1f (contacts %s)",
            task.op, Bridge.AGENT.stuckMs / 1000, x, z, tostring(contacts))
        local stuck = task.leftStart and codes.stuck or codes.stuckAtStart
        if stuck == codes.ended then return finish(stuck) end
        agentEnd(env, task, dt)
        return "FAILED", stuck
    end
    if name == "DRIVING" and finite(curvature) then return nil end
    if name == "BLOCKED" then task.fromAt = task.elapsed end   -- its own bound below
    if name == "TARGET_REACHED" or name == "NOT_REACHABLE" then
        logAgent(env, v, task.op .. " ended")
        return finish(codes.ended)
    end
    local waited = task.elapsed - (task.stateSince or task.elapsed)
    if name == "BLOCKED" and waited > Bridge.AGENT.blockedMs then
        logAgent(env, v, task.op .. " blocked")
        agentEnd(env, task, dt)
        return "FAILED", task.leftStart and codes.blocked or codes.blockedAtStart
    end
    if name ~= "BLOCKED" and waited > Bridge.AGENT.planningMs then
        logAgent(env, v, task.op .. " planning")
        agentEnd(env, task, dt)
        return "FAILED", codes.blocked
    end
    return nil
end

-- -------------------------------------------------------------------- reach
-- REACH: whether GIANTS' agent finds a route for the reserved vehicle (its rig as GIANTS sizes it,
-- AIDrivable:createAgent / getAIAgentSize) from where it stands to (x, z, yaw): its own planner
-- only, never a drive. The agent is made and given the target (setVehicleNavigationAgentTarget),
-- its state polled with the vehicle braked (getVehicleNavigationAgentNextCurvature, as
-- AIDrivable:onUpdate 218-243), then deleted. DONE REACHABLE (it plans a drive or is there),
-- NOT_REACHABLE, REACH_BLOCKED (planned, but something stands at its start), or REACH_UNDECIDED
-- after REACH_MS. A field-work job's target is never checked before GIANTS drives
-- (AIJobFieldWork:validate): this is that check.
Bridge.REACH_MS = 20000

function starters.reach(env, spec)
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local task, code = startAgent(env, spec)
    if task == nil then return nil, code end
    local ok, w, l, lo, fo, h = pcall(function() return spec.tractor:getAIAgentSize() end)
    local radius = spec.tractor.maxTurningRadius
    -- Where its width comes from: the vehicle's own agent, or an attachment (AIDrivable getAIAgentSize:
    -- the widest of them), each as GIANTS registered it (updateAIAgentAttachments, AIVehicle).
    local okParts, parts = pcall(function()
        local ai = spec.tractor.spec_aiDrivable
        local out = { string.format("own=%.2f", ai.agentInfo.width or -1) }
        for i, a in ipairs(ai.attachments or {}) do
            local name = a.vehicle ~= nil and a.vehicle.getName ~= nil and a.vehicle:getName() or "?"
            out[#out + 1] = string.format("%d:%s=%.2fx%.2f", i, tostring(name), a.width or -1, a.length or -1)
        end
        return table.concat(out, " ")
    end)
    log("reach: agent widths %s", okParts and parts or "?")
    local okId, id = pcall(function() return env.NetworkUtil.getObjectId(spec.tractor) end)
    log("reach: agent %s to %.1f,%.1f yaw %.2f (width %s length %s offsets %s/%s height %s radius %s)",
        okId and tostring(id) or "?", spec.x, spec.z, spec.yaw or 0,
        ok and tostring(w) or "?", ok and tostring(l) or "?", ok and tostring(lo) or "?", ok and tostring(fo) or "?",
        ok and tostring(h) or "?", tostring(radius))
    return task
end

function steppers.reach(env, task, dt)
    local v = task.spec.tractor
    if task.spec.owned() ~= true then agentEnd(env, task, dt); return "FAILED", "RESERVATION_LOST" end
    local ai = v.spec_aiDrivable
    pcall(Bridge.Hitch.halt, env, task.drive, dt)
    v:updateAIAgentPoseData()
    local _, _, state = env.getVehicleNavigationAgentNextCurvature(ai.agentId, ai.poseData, 0)
    local name = agentState(env, state)
    local code = (name == "DRIVING" or name == "TARGET_REACHED") and "REACHABLE"
        or (name == "NOT_REACHABLE" and "NOT_REACHABLE") or (name == "BLOCKED" and "REACH_BLOCKED")
        or (task.elapsed > Bridge.REACH_MS and "REACH_UNDECIDED") or nil
    if code == nil then return nil end
    if code ~= "REACHABLE" then
        -- Whether its footprint has room in GIANTS' cost map where it stands and at the target
        -- (AIDrivable:debugGetAgentHasSpaceAt, the base agent size): a planner refusing at once.
        pcall(function()
            local node = v:getAIRootNode()
            local x, y, z = env.getWorldTranslation(node)
            local dx, dy, dz = env.localDirectionToWorld(node, 0, 0, 1)
            local ty = env.getTerrainHeightAtWorldPos(env.g_currentMission.terrainRootNode, task.spec.x, 0, task.spec.z)
            local yaw = task.spec.yaw or 0
            log("reach: room at its start %s, at the target %s", tostring(v:debugGetAgentHasSpaceAt(x, y, z, dx, dy, dz)),
                tostring(v:debugGetAgentHasSpaceAt(task.spec.x, ty, task.spec.z, math.sin(yaw), 0, math.cos(yaw))))
        end)
    end
    log("reach: %s after %d ms", code, task.elapsed)
    agentEnd(env, task, dt)
    return "DONE", code
end

function aborts.reach(env, task) agentEnd(env, task, 0) end

-- Both drive GIANTS' agent the same way (startAgent / stepAgent); the GOTO job only without one.
function starters.stage(env, spec)
    local task, code = startAgent(env, spec)
    if task == nil and code == "NO_AGENT" then return startGoto(env, spec, "STAGE_") end
    if task ~= nil then task.agent = true end
    return task, code
end
function steppers.stage(env, task, dt)
    if not task.agent then
        return stepGoto(env, task, Bridge.STAGE_TIMEOUT_MS, "STAGE_TIMEOUT", "STAGE_ENDED", false, "STAGE_")
    end
    local ok, status, code = pcall(stepAgent, env, task, dt or 0, Bridge.AGENT_CODES.stage)
    if ok then return status, code end
    log("stage: agent step error %s", tostring(status))
    agentEnd(env, task, dt or 0)
    return "UNVERIFIED", "UNKNOWN_OBSERVATION"
end
aborts.stage = function(env, task)
    if task.agent then return agentEnd(env, task, 0) end
    return abortGoto(env, task)
end

function starters.navigate(env, spec)
    -- A rig already pressed against fixed scenery needs a Farmhand departure
    -- maneuver before GIANTS GOTO. Starting its agent here just drives into
    -- the contact until the 90 s stall watchdog fires.
    if stopped(spec.tractor) and Bridge.Hitch ~= nil
            and type(Bridge.Hitch.rigContacts) == "function" then
        local ok, contacts = pcall(Bridge.Hitch.rigContacts, env, spec.tractor)
        if ok and type(contacts) == "string" and contacts:find("standing", 1, true) then
            log("navigate: fixed contact at start (%s); Farmhand must depart first", contacts)
            return nil, "NAVIGATE_START_CONTACT"
        end
    end
    local task, code = startAgent(env, spec)
    if task == nil and code == "NO_AGENT" then
        task, code = startGoto(env, spec, "NAVIGATE_")         -- no agent of its own: GIANTS' GOTO
    else
        if task ~= nil then task.agent = true end
    end
    if task ~= nil then
        task.cropWheels = Bridge.protectTransportCrops(spec.tractor)
        task.cargo = Bridge.beltedCargo(spec.tractor)
        if #task.cargo > 0 then
            cargoObstaclesOff(task)
            log("navigate: %d belted cargo vehicle(s) out of GIANTS' AI obstacles while it drives", #task.cargo)
        end
    end
    return task, code
end
local function traceNavigateSurface(env, task)
    if task.elapsed < (task.surfaceAt or 0) then return end
    task.surfaceAt = task.elapsed + 5000
    local ok, foot = pcall(Bridge.Hitch.routeFootprint, env, task.spec.tractor)
    if not ok or foot == nil then return end
    local parts, counts = {}, { NON_FIELD = 0, FIELD = 0, CROP = 0 }
    for _, unit in ipairs(foot.units) do
        local at, x, _, z = pcall(env.getWorldTranslation, unit.rootNode)
        if at and finite(x) and finite(z) then
            local ground = Bridge.Hitch.routeSurface(env, x, z) or "NON_FIELD"
            counts[ground] = (counts[ground] or 0) + 1
            parts[#parts + 1] = string.format("%.1f,%.1f:%s", x, z, ground)
        end
    end
    log("navigate surface rig %s (non-field=%d field=%d crop=%d)", table.concat(parts, " "),
        counts.NON_FIELD, counts.FIELD, counts.CROP)
end
function steppers.navigate(env, task, dt)
    pcall(traceNavigateSurface, env, task)
    cargoObstaclesOff(task)
    local status, code
    if task.agent then
        local ok, s1, c1 = pcall(stepAgent, env, task, dt or 0, Bridge.AGENT_CODES.navigate)
        if ok then status, code = s1, c1
        else
            log("navigate: agent step error %s", tostring(s1))
            agentEnd(env, task, dt or 0)
            status, code = "UNVERIFIED", "UNKNOWN_OBSERVATION"
        end
    else
        status, code = stepGoto(env, task, Bridge.DRIVE_TIMEOUT_MS, "TIMEOUT", "GOTO_ENDED", true, "NAVIGATE_")
    end
    if status ~= nil then
        cargoObstaclesBack(task)
        Bridge.restoreTransportCrops(task.cropWheels)
        task.cropWheels = nil
    end
    return status, code
end
aborts.navigate = function(env, task)
    cargoObstaclesBack(task)
    Bridge.restoreTransportCrops(task.cropWheels)
    task.cropWheels = nil
    if task.agent then return agentEnd(env, task, 0) end
    return abortGoto(env, task)
end

-- --------------------------------------------------------------- maneuver
-- A hitch step with buildings within its reach, and the player's opt-in scenery setting on for
-- it (the command's gh="1"; AGENTS.md 10, default off): the tractor's collision with them off
-- first, as Farmhand's drive does then (Hitch.ghost; the bridge's watcher restores it once
-- clear). Without the setting the step keeps every collision.
local function ghostNearBuildings(env, spec, step)
    local H, tractor = Bridge.Hitch, spec.tractor
    if spec.ghost ~= 1 or type(H.ghost) ~= "function" then return end
    pcall(function()
        local foot = H.routeFootprint(env, tractor)
        if foot == nil then return end
        local x, _, z = env.getWorldTranslation(tractor.rootNode)
        local fx, _, fz = env.localDirectionToWorld(tractor.rootNode, 0, 0, 1)
        -- Anywhere a hitch step may reach (its work radius).
        local nodes = H.staticNodesNear(env, foot, x, z, atan2(fx, fz), H.LOCAL_LIMITS.workRadiusM)
        if nodes ~= nil and #nodes > 0 then
            local ok, added = H.ghost(env, tractor, step, nodes, true)
            log("%s near buildings: the rig's collision with %s scenery obstacle(s) off%s", step,
                ok and tostring(added) or "no", ok and "" or (" (" .. tostring(added) .. ")"))
        end
    end)
end

-- Hitch.onNote prints each phase line (once per phase and at the end).
function starters.maneuver(env, spec)
    ghostNearBuildings(env, spec, "maneuver")
    Bridge.Hitch.onNote = function(line) log("maneuver %s", line) end
    Bridge.Hitch.onDiagnostic = function(line) log("maneuver diagnostics: %s", line) end
    -- Read-only steering samples of the local maneuver (diagnostics, 2 per second).
    Bridge.Hitch.onSteer = function(line) log("maneuver %s", line) end
    local session, reason = Bridge.Hitch.newManeuver(env, {
        tractor = spec.tractor, wagon = spec.wagon, tractorJoint = spec.tractorJoint,
        wagonJoint = spec.wagonJoint, farmId = spec.farmId, isReserved = spec.owned,
    })
    if session == nil then
        log("maneuver refused %s: joints %s", tostring(reason), Bridge.Hitch.describeJoints(env, spec.tractor))
        return nil, reason
    end
    Bridge.lastManeuver = session
    return { session = session }
end

function steppers.maneuver(env, task, dt)
    local code = Bridge.Hitch.maneuverStep(env, task.session, dt)
    if code == "CONTINUE" then return nil end
    if not task.endLogged then
        task.endLogged = true
        logMotionEnd("maneuver", code, task.session, function() return "" end)
    end
    if code ~= "LINED_UP" then return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code end
    return settle(task, code)       -- braked by the latch; DONE once observed at rest
end

function aborts.maneuver(env, task)
    abortMotion("maneuver", env, task, function() return "" end)
end

-- ------------------------------------------------------------------ align
function starters.align(env, spec)
    ghostNearBuildings(env, spec, "align")
    local session, reason = Bridge.Hitch.newAlign(env, {
        tractor = spec.tractor, wagon = spec.wagon, tractorJoint = spec.tractorJoint,
        wagonJoint = spec.wagonJoint, farmId = spec.farmId, isReserved = spec.owned,
        maxDistanceM = spec.maxDistanceM, assistRadiusM = spec.assistRadiusM,
        maxVerticalM = spec.maxVerticalM, maxSpeedKmh = spec.maxSpeedKmh,
    })
    if session == nil then return nil, reason end
    Bridge.lastAlign, Bridge.lastManeuver = session, nil     -- the maneuver is over
    return { session = session }
end

function steppers.align(env, task, dt)
    local code = Bridge.Hitch.alignStep(env, task.session, dt)
    if code == "CONTINUE" then return nil end
    logMotionEnd("align", code, task.session, Bridge.Hitch.describe)
    if code == "CANDIDATE" then return "DONE", code end
    return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code
end

function aborts.align(env, task)
    abortMotion("align", env, task, Bridge.Hitch.describe)
end

-- ----------------------------------------------------------------- attach
-- Only straight after the align that ended on exactly this candidate.
function starters.attach(env, spec)
    local s = Bridge.lastAlign
    if s == nil or s.tractor ~= spec.tractor or s.wagon ~= spec.wagon or s.tractorJointIndex ~= spec.tractorJoint
            or s.wagonJointIndex ~= spec.wagonJoint then
        return nil, "NOT_ALIGNED"
    end
    local code = Bridge.Hitch.attach(env, s)
    if code ~= "ATTACH_REQUESTED" then
        return nil, code, code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED"
    end
    return { session = s }
end

function steppers.attach(env, task, dt)
    local state = Bridge.Hitch.attachState(env, task.session, dt)
    if state == "ATTACH_IN_PROGRESS" then return nil end
    Bridge.lastAlign = nil           -- consumed: later stops never drive driveToPoint again
    if state == "ATTACHED" then return "DONE", state end
    return "UNVERIFIED", state       -- GIANTS was asked to attach: outcome unknown
end

-- GIANTS' smooth attach is not undone.
function aborts.attach() end

-- --------------------------------------------------------------- reattach
-- REATTACH: the reserved, idle machine attaches the loose implement it stands at, as the player's
-- own attach key does (AttacherJoints:updateVehiclesInAttachRange 3684-3777): one of its free
-- attacher joints of the implement's input-joint type within GIANTS' attach range (0.7 m across,
-- 1.4 m up or down), GIANTS' isAttachAllowed, then attachImplement. For a machine whose joint
-- Farmhand does not observe (a combine's cutter joint moves with its feeder): no plan of joints
-- is needed. DONE ATTACHED once the implement names it; REATTACH_NOT_IN_RANGE (the nearest pair's
-- gap logged) when nothing is in range; nothing moves.
Bridge.REATTACH = { xzM = 0.7, yM = 1.4, settleMs = 5000, creepM = 9.0, creepKmh = 3.0, lateralM = 1.0,
                     headingDeg = 6, timeoutMs = 40000, implementMovedM = 0.3 }

-- The free attacher joint of `v` and input joint of `impl` of one type nearest each other, their gap
-- across and up, and the input joint's node.
local function reattachPair(env, v, impl)
    local best
    for ji, joint in ipairs(v:getAttacherJoints() or {}) do
        if joint.jointIndex == 0 and joint.jointTransform ~= nil then
            local jx, jy, jz = env.getWorldTranslation(joint.jointTransform)
            for ii, input in ipairs(impl:getInputAttacherJoints() or {}) do
                if input.jointType == joint.jointType and input.node ~= nil then
                    local ix, iy, iz = env.getWorldTranslation(input.node)
                    local gap = math.sqrt((jx - ix) ^ 2 + (jz - iz) ^ 2)
                    if best == nil or gap < best.gap then
                        best = { joint = ji, input = ii, gap = gap, dy = jy - iy, node = input.node, jx = jx, jz = jz,
                                 ix = ix, iz = iz }
                    end
                end
            end
        end
    end
    return best
end

local function reattachNow(env, task, pair)
    local v, impl = task.spec.tractor, task.impl
    if impl:isAttachAllowed(task.spec.farmId, v) ~= true then return "FAILED", "ATTACH_REJECTED" end
    log("reattach: joint %d to input %d, %.2f m across, %.2f m up", pair.joint, pair.input, pair.gap, pair.dy)
    pcall(Bridge.Hitch.halt, env, task.drive, 0)
    v:attachImplement(impl, pair.input, pair.joint)
    task.asked = true
    return nil
end

function starters.reattach(env, spec)
    local v, impl = spec.tractor, spec.wagon
    local denied = idle(env, v, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if impl.getAttacherVehicle ~= nil and impl:getAttacherVehicle() ~= nil then return nil, "WAGON_ATTACHED" end
    local pair = reattachPair(env, v, impl)
    if pair == nil then return nil, "NO_MATCHING_JOINT" end
    local R = Bridge.REATTACH
    local task = { impl = impl, drive = { tractor = v, maxSpeedKmh = R.creepKmh, elapsed = 0 }, startGap = pair.gap }
    if pair.gap > R.xzM or math.abs(pair.dy) > R.yM then
        -- Not in range: only a short straight creep onto it (it backed straight off it), lined up.
        local node = v:getAISteeringNode()
        local lx, _, lz = env.worldToLocal(node, pair.ix, 0, pair.iz)
        local jl, _, jz = env.worldToLocal(node, pair.jx, 0, pair.jz)
        local ahead, across = lz - jz, lx - jl
        log("reattach: %.2f m off (%.2f ahead, %.2f across, %.2f up)", pair.gap, ahead, across, pair.dy)
        if ahead <= 0 or ahead > R.creepM or math.abs(across) > R.lateralM or math.abs(pair.dy) > R.yM then
            return nil, "REATTACH_NOT_IN_RANGE"
        end
        task.creep = true
        Bridge.Hitch.watchDriver(env, v)
    end
    return task
end

function steppers.reattach(env, task, dt)
    local v, impl, R = task.spec.tractor, task.impl, Bridge.REATTACH
    if task.spec.owned() ~= true then pcall(Bridge.Hitch.halt, env, task.drive, dt); return "FAILED", "RESERVATION_LOST" end
    if impl.getAttacherVehicle ~= nil and impl:getAttacherVehicle() == v then
        pcall(Bridge.Hitch.halt, env, task.drive, dt)
        return settle(task, "ATTACHED")
    end
    task.drive.elapsed = task.elapsed
    if task.asked then
        pcall(Bridge.Hitch.halt, env, task.drive, dt)
        if task.elapsed - (task.askedAt or task.elapsed) > R.settleMs then return "UNVERIFIED", "ATTACH_NOT_OBSERVED" end
        task.askedAt = task.askedAt or task.elapsed
        return nil
    end
    local pair = reattachPair(env, v, impl)
    if pair == nil then pcall(Bridge.Hitch.halt, env, task.drive, dt); return "FAILED", "NO_MATCHING_JOINT" end
    if pair.gap <= R.xzM and math.abs(pair.dy) <= R.yM then return reattachNow(env, task, pair) end
    if not task.creep then return "FAILED", "REATTACH_NOT_IN_RANGE" end
    -- Never pushes it (live 2026-11-05: the MF 8570 shoved its loose header 14.5 m, the gap unchanged):
    -- the implement moving at all ends it, and so does driving farther than the gap it started at.
    local ix, _, iz = env.getWorldTranslation(impl.rootNode)
    local vx, _, vz = env.getWorldTranslation(v.rootNode)
    task.implAt = task.implAt or { ix, iz }
    task.from = task.from or { vx, vz }
    if math.sqrt((ix - task.implAt[1]) ^ 2 + (iz - task.implAt[2]) ^ 2) > R.implementMovedM then
        pcall(Bridge.Hitch.halt, env, task.drive, dt)
        return "FAILED", "IMPLEMENT_MOVED"
    end
    if task.elapsed > R.timeoutMs or pair.gap > task.startGap + 0.5
            or math.sqrt((vx - task.from[1]) ^ 2 + (vz - task.from[2]) ^ 2) > task.startGap + 0.5 then
        pcall(Bridge.Hitch.halt, env, task.drive, dt)
        return "FAILED", "REATTACH_NOT_IN_RANGE"
    end
    local conflict = Bridge.Hitch.driverConflict and Bridge.Hitch.driverConflict(v)
    if conflict ~= nil then pcall(Bridge.Hitch.halt, env, task.drive, dt); return "FAILED", conflict end
    if not v:getIsMotorStarted() and v:getCanMotorRun() then v:startMotor() end
    Bridge.Hitch.holdActive(task.drive)
    v:raiseActive()
    -- Steer the joint onto the input joint: GIANTS' driveToPoint at the input joint, in the steering frame.
    local node = v:getAISteeringNode()
    local lx, _, lz = env.worldToLocal(node, pair.ix, 0, pair.iz)
    Bridge.Hitch.drive(env, task.drive, dt, true, lx, math.max(lz, 1))
    return nil
end

function aborts.reattach(env, task) pcall(Bridge.Hitch.halt, env, task.drive, 0) end

-- ------------------------------------------------------------------ mount
-- The machine's raised front implement (a header) carried straight onto the free GIANTS lock position
-- a carrier keeps for it (DynamicMountAttacher lockPosition, matched by the implement's config file,
-- as DynamicMountAttacher:addDynamicMountedObject matches it), set down there by GIANTS' own detach;
-- the carrier mounts what stands still in its trigger (DynamicMountAttacher:onUpdateTick, 1 s at
-- rest) and locks it into place. Only lined up (lateralM, headingDeg) and short (maxM); the carrier
-- moving at all (pushed), a drive past the start distance, or the seated player taking over ends it.
-- DONE MOUNTED once the implement reports the carrier as its dynamic mount (getDynamicMountObject).
Bridge.MOUNT = { creepKmh = 2.0, maxM = 12, lateralM = 0.6, headingDeg = 6, arriveM = 0.15, carrierMovedM = 0.15,
                 timeoutMs = 60000, raiseMs = 4000, mountMs = 8000, aboveM = 0.05 }

local function mountPlan(env, v, carrier)
    local spec = carrier.spec_dynamicMountAttacher
    if spec == nil then return nil, "NOT_A_CARRIER" end
    for _, entry in ipairs(v:getAttachedImplements() or {}) do
        local implement = entry.object
        local file = implement ~= nil and implement.configFileName or nil
        for _, lock in ipairs(spec.lockPositions or {}) do
            if type(file) == "string" and lock.state ~= true and file:sub(-#lock.xmlFilename) == lock.xmlFilename then
                return { implement = implement, lock = lock, joint = entry.jointDescIndex }
            end
        end
    end
    return nil, "NO_IMPLEMENT_FOR_CARRIER"
end

-- Where the implement's root still has to go: (ahead along the machine, across, up, heading off).
local function mountGap(env, v, plan)
    local ix, iy, iz = env.getWorldTranslation(plan.implement.rootNode)
    local lx, ly, lz = env.getWorldTranslation(plan.lock.jointNode)
    local fx, _, fz = env.localDirectionToWorld(v.rootNode, 0, 0, 1)
    local dx, dz = lx - ix, lz - iz
    local ifx, _, ifz = env.localDirectionToWorld(plan.implement.rootNode, 0, 0, 1)
    local lfx, _, lfz = env.localDirectionToWorld(plan.lock.jointNode, 0, 0, 1)
    local turned = math.abs((math.atan2(ifx, ifz) - math.atan2(lfx, lfz) + math.pi) % (2 * math.pi) - math.pi)
    return dx * fx + dz * fz, dx * fz - dz * fx, iy - ly, turned
end

function starters.mount(env, spec)
    local v, carrier = spec.tractor, spec.wagon
    local denied = idle(env, v, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if not stopped(carrier) then return nil, "NOT_STOPPED" end
    if type(carrier.getAllowDynamicMountObjects) ~= "function" or carrier:getAllowDynamicMountObjects() ~= true then
        return nil, "CARRIER_NOT_READY"
    end
    local plan, why = mountPlan(env, v, carrier)
    if plan == nil then return nil, why end
    local M = Bridge.MOUNT
    local ahead, across, up, turned = mountGap(env, v, plan)
    log("mount: %s %.2f m ahead, %.2f across, %.2f above its lock, %.1f deg off", tostring(plan.implement:getName()),
        ahead, across, up, math.deg(turned))
    if ahead < -M.arriveM or ahead > M.maxM or math.abs(across) > M.lateralM or turned > math.rad(M.headingDeg) then
        return nil, "MOUNT_NOT_LINED_UP"
    end
    local cx, _, cz = env.getWorldTranslation(carrier.rootNode)
    local vx, _, vz = env.getWorldTranslation(v.rootNode)
    local task = { plan = plan, drive = { tractor = v, maxSpeedKmh = M.creepKmh, elapsed = 0 },
                   carrierAt = { cx, cz }, from = { vx, vz }, startAhead = ahead }
    -- Raised to carry it over the deck (GIANTS' own lift of that joint), never lowered onto it.
    if type(v.getJointMoveDown) == "function" and v:getJointMoveDown(plan.joint) then
        v:setJointMoveDown(plan.joint, false)
        task.raising = 0
    end
    Bridge.Hitch.watchDriver(env, v)
    return task
end

function steppers.mount(env, task, dt)
    local v, carrier, plan, M = task.spec.tractor, task.spec.wagon, task.plan, Bridge.MOUNT
    local function halt() pcall(Bridge.Hitch.halt, env, task.drive, dt) end
    if task.spec.owned() ~= true then halt(); return "FAILED", "RESERVATION_LOST" end
    task.drive.elapsed = task.elapsed
    if task.detached then
        halt()
        if plan.implement:getDynamicMountObject() == carrier then return settle(task, "MOUNTED") end
        if task.elapsed - task.detached > M.mountMs then return "UNVERIFIED", "MOUNT_NOT_OBSERVED" end
        return nil
    end
    local cx, _, cz = env.getWorldTranslation(carrier.rootNode)
    if math.sqrt((cx - task.carrierAt[1]) ^ 2 + (cz - task.carrierAt[2]) ^ 2) > M.carrierMovedM then
        halt()
        return "FAILED", "CARRIER_MOVED"
    end
    local conflict = Bridge.Hitch.driverConflict and Bridge.Hitch.driverConflict(v)
    if conflict ~= nil then halt(); return "FAILED", conflict end
    if task.raising ~= nil then
        halt()
        task.raising = task.raising + dt
        if task.raising < M.raiseMs then return nil end
        task.raising = nil
    end
    local ahead, across, up = mountGap(env, v, plan)
    if task.checkedHeight == nil then
        task.checkedHeight = true
        -- Carried no higher than the deck it goes onto, it would be pushed into the carrier.
        if up < M.aboveM then halt(); log("mount: carried %.2f m above its lock, too low", up); return "FAILED", "CARRIED_TOO_LOW" end
    end
    if ahead <= M.arriveM then
        halt()
        if not stopped(v) then return nil end
        if plan.implement:isDetachAllowed() ~= true then return "FAILED", "DETACH_NOT_ALLOWED" end
        local entry = v:getImplementByObject(plan.implement)
        task.joints = entry and { entry.jointDescIndex, entry.inputJointDescIndex } or nil
        log("mount: at its lock (%.2f ahead, %.2f across, %.2f above): set down", ahead, across, up)
        v:detachImplementByObject(plan.implement)
        task.detached = task.elapsed
        -- A pull-clear may back the machine off it, as after any detach.
        Bridge.lastDetach = { tractor = v, implement = plan.implement, joints = task.joints, age = 0 }
        return nil
    end
    local vx, _, vz = env.getWorldTranslation(v.rootNode)
    if task.elapsed > M.timeoutMs or math.abs(across) > M.lateralM
            or math.sqrt((vx - task.from[1]) ^ 2 + (vz - task.from[2]) ^ 2) > task.startAhead + 0.5 then
        halt()
        return "FAILED", "MOUNT_NOT_LINED_UP"
    end
    if not v:getIsMotorStarted() and v:getCanMotorRun() then v:startMotor() end
    Bridge.Hitch.holdActive(task.drive)
    v:raiseActive()
    local node = v:getAISteeringNode()
    local lx, _, lz = env.worldToLocal(node, env.getWorldTranslation(plan.lock.jointNode))
    local ix, _, iz = env.worldToLocal(node, env.getWorldTranslation(plan.implement.rootNode))
    Bridge.Hitch.drive(env, task.drive, dt, true, lx - ix, math.max(lz - iz, 1))
    return nil
end

function aborts.mount(env, task) pcall(Bridge.Hitch.halt, env, task.drive, 0) end

-- ----------------------------------------------------------------- detach
-- GIANTS' own detach behind GIANTS' own permission, for the reserved, idle
-- tractor: the implement must be its (AttacherJoints:getImplementByObject
-- 3209-3219) and at rest, and must allow detaching (Attachable:isDetachAllowed
-- 1398-1434, the check the player's own toggle makes, AttacherJoints:
-- detachingIsPossible 3039-3051); then AttacherJoints:detachImplementByObject
-- 2820-2831 (detachImplement 2605-2619 broadcasts VehicleDetachEvent on the
-- server). DONE once neither end lists the other (Attachable:getAttacherVehicle
-- 1211-1213) and the tractor is at rest; the executor verifies from a snapshot.
function starters.detach(env, spec)
    local denied = idle(env, spec.tractor, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local v, implement = spec.tractor, spec.wagon
    if v:getImplementByObject(implement) == nil then return nil, "NOT_ATTACHED" end
    if not stopped(implement) then return nil, "NOT_STOPPED" end
    if implement:isDetachAllowed() ~= true then return nil, "DETACH_NOT_ALLOWED" end
    -- The joints it was on (AttacherJoints:createAttachmentJoint 2279-2280), for PULL_CLEAR.
    local entry = v:getImplementByObject(implement)
    local joints = { entry.jointDescIndex, entry.inputJointDescIndex }
    v:detachImplementByObject(implement)
    return { joints = joints }
end

function steppers.detach(_env, task)
    local status, code = stillOurs(task.spec)
    if status ~= nil then return status, code end
    local v, implement = task.spec.tractor, task.spec.wagon
    if v:getImplementByObject(implement) == nil and implement:getAttacherVehicle() == nil then
        local ended, endCode = settle(task, "DETACHED")
        if ended == "DONE" then
            Bridge.lastDetach = { tractor = v, implement = implement, joints = task.joints, age = 0 }
        end
        return ended, endCode
    end
    if task.elapsed > Bridge.DETACH_TIMEOUT_MS then return "UNVERIFIED", "DETACH_TIMEOUT" end
    return nil
end

-- GIANTS' detach is not undone.
function aborts.detach() end

-- ------------------------------------------------------------- pull_clear
-- Only for the tractor and implement of the detach that ended DONE just before,
-- with no other operation in between (Bridge.start clears it) and within
-- PULL_AFTER_DETACH_MS; on the joints that detach freed.
function starters.pull_clear(env, spec)
    local last = Bridge.lastDetach
    Bridge.lastDetach = nil                      -- one pull-clear per detach
    if last == nil or last.tractor ~= spec.tractor or last.implement ~= spec.wagon
            or last.age > Bridge.PULL_AFTER_DETACH_MS then
        return nil, "NOT_JUST_DETACHED"
    end
    local denied = idle(env, spec.tractor, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local session, reason = Bridge.Hitch.newPullClear(env, {
        tractor = spec.tractor, wagon = spec.wagon, tractorJoint = last.joints[1], wagonJoint = last.joints[2],
        farmId = spec.farmId, isReserved = spec.owned,
    })
    if session == nil then
        -- Refused before any motion (the player in its way, live cc1d2a73fb8243fc-1): the same
        -- detach may be pulled clear again, inside its window, nothing else in between.
        if reason == "PULL_CLEAR_PERSON" then Bridge.lastDetach = last end
        return nil, reason
    end
    Bridge.lastPull = session
    return { session = session }
end

function steppers.pull_clear(env, task, dt)
    local code = Bridge.Hitch.pullClearStep(env, task.session, dt)
    if code == "CONTINUE" then return nil end
    if not task.endLogged then          -- once, not every frame while it brakes to rest
        task.endLogged = true
        logMotionEnd("pull_clear", code, task.session, Bridge.Hitch.describePull)
    end
    if code ~= "CLEAR" then return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code end
    return settle(task, code)       -- braked by the latch; DONE once observed at rest
end

function aborts.pull_clear(env, task)
    abortMotion("pull_clear", env, task, Bridge.Hitch.describePull)
end

-- ----------------------------------------------------------------- unpark
-- UNPARK (AGENTS.md exception 4): the reserved vehicle, with whatever is attached to it,
-- straight out of the building GIANTS' agent could not start from (Hitch.newUnpark). The
-- companion asks for it only after GIANTS refused to start and no closed door was found;
-- DONE UNPARKED once it is clear and at rest. Every refusal leaves it where it stood.
-- Building doors are never opened and never driven round (live bfbc32565ecb4472-1:
-- opening the Large Shed's wall shoved four vehicles up to 11.7 m; the user, 2026-10-20:
-- gates on roads only): a closed door or gate within UNPARK_DOOR_M of any unit of the
-- rig refuses it (UNPARK_DOOR_CLOSED), read only.
Bridge.UNPARK_DOOR_M = 15

-- The first closed door within UNPARK_DOOR_M of the vehicle or what is attached to it, as
-- "name at (x, z) d m", or nil. Errors on an unreadable unit position.
local function closedDoorNear(env, v)
    local points = {}
    local function add(u)
        local x, _, z = env.getWorldTranslation(u.rootNode)
        assert(finite(x) and finite(z))
        points[#points + 1] = { x, z }
        if type(u.getAttachedImplements) == "function" then
            for _, implement in ipairs(u:getAttachedImplements() or {}) do add(implement.object) end
        end
    end
    add(v)
    for _, o in ipairs(gateCandidates(env)) do
        local ok, found = pcall(function()
            if o.isEnabled == false or (o.animation.time or 0) >= 1 then return nil end
            local gx, _, gz = env.getWorldTranslation(o.nodeId)
            for _, p in ipairs(points) do
                local d = math.sqrt((gx - p[1]) ^ 2 + (gz - p[2]) ^ 2)
                if d <= Bridge.UNPARK_DOOR_M then
                    return string.format("%s at (%.1f, %.1f) %.1f m", tostring(o.saveId), gx, gz, d)
                end
            end
        end)
        if ok and found ~= nil then return found end
    end
    return nil
end

-- --------------------------------------------------------------- reposition
-- A vehicle (a standing combine) driven by Farmhand's own drive to the nearest open ground
-- where it and a lane each side and a run-up behind are clear (Hitch.openSpot on its route box
-- grown by REPOSITION), so a trailer can come under its pipe (live ec7e8bafc78e4129-1: in the
-- yard pocket a dropped seeder blocked the lane behind, the barn the front).
Bridge.REPOSITION = { sideM = 6, behindM = 18, aheadM = 4 }
-- Any other vehicle (moved out of the way: the user, 2026-10-30) needs only room around it.
Bridge.CLEAR_AWAY = { sideM = 1.5, behindM = 2, aheadM = 2 }
Bridge.REPOSITION_REACH_M = 80        -- how far out a spot GIANTS' navigation reaches is looked for
-- Where a teleported rig lands, room for GIANTS' helper to start: a lane ahead and beside (live
-- 5e0f5dc02a834ae2-1: set down 4 m off inside the yard pocket, "AI worker is blocked by an
-- object" at once, its collision check looks ahead of the rig).
Bridge.TELEPORT_ROOM = { sideM = 3, behindM = 1, aheadM = 15 }
-- A teleport asked to land no nearer than spec.area (the companion goes farther out each time
-- GIANTS still cannot start: the user, 2026-10-31, up to about 150 m) looks 50 m beyond that.
Bridge.TELEPORT_BEYOND_M, Bridge.TELEPORT_MAX_M = 50, 150
-- The route box ``foot`` grown by ``room`` (sideM each side, behindM, aheadM).
function Bridge.grown(foot, room)
    local big = {}
    for k, val in pairs(foot) do big[k] = val end
    big.lMin, big.lMax = foot.lMin - room.sideM, foot.lMax + room.sideM
    big.aMin, big.aMax = foot.aMin - room.behindM, foot.aMax + room.aheadM
    return big
end
function starters.reposition(env, spec)
    local v = spec.tractor
    local denied = idle(env, v, spec.farmId)
    if denied ~= nil then return nil, denied end
    local H = Bridge.Hitch
    local foot = H.routeFootprint(env, v)
    if foot == nil then return nil, "RIG_GEOMETRY_UNKNOWN" end
    -- area > 0: a transport moved where a machine can be driven up its ramps (live
    -- 8fa6ad416fa14943-1: the Deckover's run-in lane past a tree and the truck itself).
    local lane = finite(spec.area) and spec.area > 0 and spec.area <= Bridge.REPOSITION_REACH_M and spec.area or nil
    local big = Bridge.grown(foot, lane ~= nil and { sideM = 2, behindM = lane, aheadM = 4 }
                             or v.spec_pipe ~= nil and Bridge.REPOSITION or Bridge.CLEAR_AWAY)
    local x, _, z = env.getWorldTranslation(v.rootNode)
    local fx, _, fz = env.localDirectionToWorld(v.rootNode, 0, 0, 1)
    -- At least the rig's own length away: driven to, not set down beside it. First where GIANTS'
    -- own navigation can start from (live 21a5e70740024850-3: every "open ground" inside the farm
    -- yard left GIANTS' GOTO blocked at its start), up to Bridge.REPOSITION_REACH_M out; else any.
    local length, yaw = foot.aMax - foot.aMin, atan2(fx, fz)
    local ai = env.g_currentMission.aiSystem
    local navigable = nil
    if ai ~= nil and type(ai.getIsPositionReachable) == "function" and ai.navigationMap ~= nil then
        navigable = function(px, pz)
            local ok, reachable = pcall(ai.getIsPositionReachable, ai, px, 0, pz)
            return ok and reachable == true
        end
    end
    local tx, tz, tyaw
    if navigable ~= nil then
        tx, tz, tyaw = H.openSpot(env, big, x, z, yaw, length, navigable, Bridge.REPOSITION_REACH_M)
    end
    if tx == nil then tx, tz, tyaw = H.openSpot(env, big, x, z, yaw, length) end
    if tx == nil then return nil, "NO_OPEN_SPOT" end
    log("reposition: the %s from %.1f,%.1f to open ground at %.1f,%.1f yaw %.2f", tostring(v.getName and v:getName() or "vehicle"),
        x, z, tx, tz, tyaw)
    -- At the heading the spot was found clear at (live 8611351ac09e4a1b-1: arrived at any heading,
    -- the GMC rig's nose against an object). 21a5e70740024850-1 (24 degrees off, corrected until
    -- OBSTACLE_AHEAD) was the old lattice search, which reached a heading only by chance; the exact
    -- goal curve (Drive.dubins) plans it.
    return starters.farm_drive(env, { tractor = v, x = tx, z = tz, area = 0,
                                      yaw = tyaw, farmId = spec.farmId,
                                      owned = spec.owned, ghost = spec.ghost })
end
steppers.reposition = function(env, task, dt) return steppers.farm_drive(env, task, dt) end
aborts.reposition = function(env, task) return aborts.farm_drive(env, task) end

-- --------------------------------------------------------------- teleport
-- Teleport recovery (AGENTS.md 19: an explicit mode, never silent): the rig, everything
-- attached, to the nearest open spot (Hitch.openSpot) by GIANTS' own FSBaseMission:teleportVehicle
-- (FSBaseMission.lua 2422-2479: attached implements detached, moved with their offsets and
-- re-attached). Refused with cargo on it (belted or mounted objects stay behind).
-- GIANTS' own teleport (FSBaseMission:teleportVehicle 2422-2479), with what the rig carries on its
-- belts too (live: the GMC's deckover with the Series 9 strapped on, TELEPORT_CARGO every time):
-- each unit's offset from the tractor recorded, the belts released (TensionBelts
-- setAllTensionBeltsActive), the implements detached, everything out of physics, placed at the
-- same offsets from the tractor's new pose (setAbsolutePosition), back in physics, the implements
-- attached again on their joints and the belts fastened again. How many units moved, the units
-- moved, and each belted unit with the cargo it held ({unit, objects}): fastened in the same frame
-- the cargo is back in physics the belts may find nothing (live 506d0f085bd64e9b-1: the Series 9
-- left unstrapped, then off the deck), so steppers.teleport fastens them again once it stands and
-- checks each object is held (Bridge.beltsHold).
function Bridge.teleportRig(env, v, tx, tz, tyaw)
    local mission = env.g_currentMission
    local entries, combos, belted, detach, seen = {}, {}, {}, {}, {}
    local function add(u)
        if seen[u] then return end
        seen[u] = true
        local x, y, z = env.getWorldTranslation(u.rootNode)
        local rx, ry, rz = env.getWorldRotation(u.rootNode)
        entries[#entries + 1] = { vehicle = u, offset = { env.worldToLocal(v.rootNode, x, y, z) },
                                  rotation = { env.worldRotationToLocal(v.rootNode, rx, ry, rz) } }
        for _, implement in ipairs(type(u.getAttachedImplements) == "function" and u:getAttachedImplements() or {}) do
            local object = implement.object
            if object ~= nil and not (object.getIsAdditionalAttachment ~= nil and object:getIsAdditionalAttachment()) then
                add(object)
                detach[#detach + 1] = object
                combos[#combos + 1] = { vehicle = u, object = object, jointDescIndex = implement.jointDescIndex,
                                        inputIndex = object:getActiveInputAttacherJointDescIndex() }
            end
        end
        local belts = u.spec_tensionBelts
        if belts ~= nil and next(belts.objectsToJoint or {}) ~= nil then
            local held = { unit = u, objects = {} }
            belted[#belted + 1] = held
            for _, data in pairs(belts.objectsToJoint) do
                local object = data.object
                if object ~= nil and object.rootNode ~= nil and type(object.setAbsolutePosition) == "function" then
                    held.objects[#held.objects + 1] = object
                    add(object.rootVehicle or object)
                end
            end
        end
    end
    add(v)
    mission.isTeleporting = true
    for _, b in ipairs(belted) do b.unit:setAllTensionBeltsActive(false, true) end
    for _, object in ipairs(detach) do
        local attacher = object:getAttacherVehicle()
        if attacher ~= nil then attacher:detachImplementByObject(object, true) end
    end
    for _, e in ipairs(entries) do e.vehicle:removeFromPhysics() end
    for i, e in ipairs(entries) do
        if i == 1 then
            local y = env.getTerrainHeightAtWorldPos(mission.terrainRootNode, tx, 300, tz) + 0.5
            e.vehicle:setAbsolutePosition(tx, y, tz, 0, tyaw, 0)
        else
            local x, y, z = env.localToWorld(v.rootNode, e.offset[1], e.offset[2], e.offset[3])
            local rx, ry, rz = env.localRotationToWorld(v.rootNode, e.rotation[1], e.rotation[2], e.rotation[3])
            e.vehicle:setAbsolutePosition(x, y, z, rx, ry, rz)
        end
    end
    for _, e in ipairs(entries) do e.vehicle:addToPhysics() end
    for _, c in ipairs(combos) do c.vehicle:attachImplement(c.object, c.inputIndex, c.jointDescIndex, true) end
    for _, b in ipairs(belted) do b.unit:setAllTensionBeltsActive(true, true) end
    mission.isTeleporting = false
    local units = {}
    for i, e in ipairs(entries) do units[i] = e.vehicle end
    return #entries, units, belted
end

-- Whether every object a belted unit held before the teleport is held again.
function Bridge.beltsHold(belted)
    for _, b in ipairs(belted) do
        local joints = b.unit.spec_tensionBelts ~= nil and b.unit.spec_tensionBelts.objectsToJoint or {}
        for _, object in ipairs(b.objects) do
            local held = false
            for _, data in pairs(joints) do
                if data.object == object then held = true end
            end
            if not held then return false end
        end
    end
    return true
end
Bridge.TELEPORT_SETTLE_MS, Bridge.TELEPORT_FASTEN_MS, Bridge.TELEPORT_BELTS_MS = 1000, 1000, 8000

-- A named spot (a lane, at its heading): the rig set down on it, or a few metres along it, with
-- only its own box clear (live 3de53846739b4945-1: the room rule for GIANTS' helper put it 28 m
-- off the road, crosswise). x, z, yaw, or nil.
Bridge.LANE_SLIDE_M = { 0, 3, -3, 6, -6, 9, -9, 12, -12 }
-- Across the lane too, toward the road's middle or its edge (live 386189c90cff4142-1: the loaded
-- GMC's box on the lane's centre touched roadside objects 2.5 m to its side).
Bridge.LANE_SHIFT_M = { 0, -1, 1, -2, 2 }
function Bridge.laneSpot(env, foot, x, z, yaw)
    local H = Bridge.Hitch
    local sx, sz = math.sin(yaw), math.cos(yaw)
    local why = {}
    for _, d in ipairs(Bridge.LANE_SLIDE_M) do
        for _, w in ipairs(Bridge.LANE_SHIFT_M) do
            local px, pz = x + sx * d + sz * w, z + sz * d - sx * w
            local clear, reason = H.routePoseClear(env, foot, px, pz, yaw, 0)
            local near = nil
            if clear == true then near = H.staticNear(env, foot, px, pz, yaw, H.TELEPORT.clearM) end
            if clear == true and near == false then return px, pz, yaw end
            if w == 0 then
                why[#why + 1] = string.format("%+d:%s", d, clear ~= true and tostring(reason) or "scenery within 1 m")
            end
        end
    end
    log("teleport onto the lane at %.1f,%.1f yaw %.2f: no clear spot (%s)", x, z, yaw, table.concat(why, "; "))
    return nil
end

-- A loose implement (no seat: a seeder, a trailer) for the teleport recovery: owned, standing,
-- hitched to nothing (the user, 2026-10-31: "can farmhand teleport it to the field when stuck?",
-- the seeder's drawbar against a tree).
local function looseIdle(v, farmId)
    if v.ownerFarmId ~= farmId then return "NOT_OWNED" end
    if type(v.getAttacherVehicle) == "function" and v:getAttacherVehicle() ~= nil then return "ATTACHED" end
    if not stopped(v) then return "NOT_STOPPED" end
    return nil
end

function starters.teleport(env, spec)
    local v = spec.tractor
    local denied
    if v.spec_enterable ~= nil then denied = idle(env, v, spec.farmId) else denied = looseIdle(v, spec.farmId) end
    if denied ~= nil then return nil, denied end
    local H = Bridge.Hitch
    -- Seated is no refusal (the player is an observer; GIANTS' teleportVehicle is made for the
    -- vehicle the player drives, FSBaseMission.lua 2595; live 8570c6316cb9406f-2: TELEPORT_SEATED);
    -- belted cargo goes along (Bridge.teleportRig).
    local own = H.routeFootprint(env, v)
    if own == nil then return nil, "RIG_GEOMETRY_UNKNOWN" end
    local foot = Bridge.grown(own, Bridge.TELEPORT_ROOM)
    local x, _, z = env.getWorldTranslation(v.rootNode)
    local fx, _, fz = env.localDirectionToWorld(v.rootNode, 0, 0, 1)
    local yaw = atan2(fx, fz)
    -- Near (spec.x, spec.z) at spec.yaw when the companion names a spot (a blocker set aside);
    -- x beyond 50000: near the rig itself (= companion/bridge.py TELEPORT_HERE, 90000).
    local cx, cz = x, z
    local tx, tz, tyaw
    if tonumber(spec.x) ~= nil and math.abs(spec.x) < 50000 and tonumber(spec.z) ~= nil then
        cx, cz = spec.x, spec.z
        if tonumber(spec.yaw) ~= nil and math.abs(spec.yaw) <= 2 * math.pi then yaw = spec.yaw end
        tx, tz, tyaw = Bridge.laneSpot(env, own, cx, cz, yaw)
    end
    -- Where GIANTS' own navigation can start from, if there is such a spot within reach (a rig set
    -- down in a pocket GIANTS cannot leave helps nothing); else any open spot.
    local ai = env.g_currentMission.aiSystem
    local minM = math.max(0, tonumber(spec.area) or 0)
    local reach = math.min(Bridge.TELEPORT_MAX_M, math.max(Bridge.REPOSITION_REACH_M, minM + Bridge.TELEPORT_BEYOND_M))
    if tx == nil and ai ~= nil and type(ai.getIsPositionReachable) == "function" and ai.navigationMap ~= nil then
        tx, tz, tyaw = H.openSpot(env, foot, cx, cz, yaw, minM, function(px, pz)
            local ok, reachable = pcall(ai.getIsPositionReachable, ai, px, 0, pz)
            return ok and reachable == true
        end, reach)
    end
    if tx == nil then tx, tz, tyaw = H.openSpot(env, foot, cx, cz, yaw, minM, nil, reach) end
    if tx == nil then return nil, "NO_OPEN_SPOT" end
    if H.ghosted ~= nil and H.ghosted[v] ~= nil then H.unghost(env, v) end
    local moved, units, belted = Bridge.teleportRig(env, v, tx, tz, tyaw)
    log("teleport recovery: the rig from %.1f,%.1f to %.1f,%.1f yaw %.2f (%d unit(s) with its cargo)", x, z, tx, tz, tyaw, moved)
    return { tx = tx, tz = tz, waited = 0, units = units, belted = belted }
end

-- Done once the tractor is where it was set, every unit stands (live 506d0f085bd64e9b-1: GOTO
-- straight after, NOT_STOPPED) and every belt holds its cargo again, fastened again each
-- TELEPORT_FASTEN_MS while it does not; cargo still loose is a failure, never driven on.
function steppers.teleport(env, task, dt)
    task.waited = task.waited + dt
    local x, _, z = env.getWorldTranslation(task.spec.tractor.rootNode)
    if math.sqrt((x - task.tx) ^ 2 + (z - task.tz) ^ 2) > 1.5 then
        if task.waited > 5000 then return "FAILED", "TELEPORT_NOT_OBSERVED" end
        return nil
    end
    for _, u in ipairs(task.units or {}) do
        if not stopped(u) then task.still = nil end
    end
    task.still = task.still or task.waited
    if task.waited - task.still < Bridge.TELEPORT_SETTLE_MS then
        if task.waited > Bridge.TELEPORT_BELTS_MS then return "FAILED", "NOT_STOPPED" end
        return nil
    end
    if Bridge.beltsHold(task.belted or {}) then return "DONE", "TELEPORTED" end
    if task.waited > Bridge.TELEPORT_BELTS_MS then return "FAILED", "CARGO_NOT_BELTED" end
    if task.fastened == nil or task.waited - task.fastened >= Bridge.TELEPORT_FASTEN_MS then
        task.fastened = task.waited
        for _, b in ipairs(task.belted) do
            b.unit:setAllTensionBeltsActive(false, true)
            b.unit:setAllTensionBeltsActive(true, true)
        end
        log("teleport recovery: fastening the belts again")
    end
    return nil
end

function aborts.teleport() end

-- A loose implement set down at exactly the pose the companion computed (a combine's header
-- in front of the machine, at the pose its recorded cutter joint attaches from), by the same
-- GIANTS teleportVehicle path as the teleport recovery. Never slid, turned or moved to "any
-- open spot": the pose is either clear (its own boxes, water, grade, scenery within
-- TELEPORT.clearM) or refused, and nothing moves.
function starters.place(env, spec)
    local v = spec.tractor
    if v.spec_enterable ~= nil then return nil, "NOT_LOOSE" end
    local denied = looseIdle(v, spec.farmId)
    if denied ~= nil then return nil, denied end
    if not (finite(spec.x) and finite(spec.z) and finite(spec.yaw)) then return nil, "INVALID_TARGET" end
    local H = Bridge.Hitch
    local own = H.routeFootprint(env, v)
    if own == nil then return nil, "RIG_GEOMETRY_UNKNOWN" end
    local clear, reason = H.routePoseClear(env, own, spec.x, spec.z, spec.yaw, 0)
    if clear ~= true then
        log("place: %.1f,%.1f yaw %.2f not clear (%s)", spec.x, spec.z, spec.yaw, tostring(reason))
        return nil, clear == false and "PLACE_NOT_CLEAR" or "PLACE_UNREADABLE"
    end
    local near = H.staticNear(env, own, spec.x, spec.z, spec.yaw, H.TELEPORT.clearM)
    if near ~= false then
        log("place: %.1f,%.1f yaw %.2f scenery within %.1f m", spec.x, spec.z, spec.yaw, H.TELEPORT.clearM)
        return nil, near == true and "PLACE_NOT_CLEAR" or "PLACE_UNREADABLE"
    end
    local x, _, z = env.getWorldTranslation(v.rootNode)
    local moved, units, belted = Bridge.teleportRig(env, v, spec.x, spec.z, spec.yaw)
    log("place: %s from %.1f,%.1f to %.1f,%.1f yaw %.2f (%d unit(s))", tostring(v:getName()), x, z,
        spec.x, spec.z, spec.yaw, moved)
    return { tx = spec.x, tz = spec.z, waited = 0, units = units, belted = belted }
end

steppers.place = steppers.teleport
aborts.place = aborts.teleport

function starters.unpark(env, spec)
    local denied = idle(env, spec.tractor, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local okDoor, door = pcall(closedDoorNear, env, spec.tractor)
    if not okDoor then return nil, "UNPARK_QUERY_UNKNOWN" end
    if door ~= nil then
        log("unpark refused UNPARK_DOOR_CLOSED: closed %s", door)
        return nil, "UNPARK_DOOR_CLOSED"
    end
    local session, reason, refused = Bridge.Hitch.newUnpark(env, {
        tractor = spec.tractor, farmId = spec.farmId, isReserved = spec.owned,
    })
    if session == nil then
        log("unpark refused %s: %s", tostring(reason), Bridge.Hitch.describeUnpark(refused))
        return nil, reason
    end
    log("unpark start: %s", Bridge.Hitch.describeUnpark(session))
    Bridge.lastUnpark = session
    return { session = session }
end

function steppers.unpark(env, task, dt)
    local code = Bridge.Hitch.unparkStep(env, task.session, dt)
    if code == "CONTINUE" then return nil end
    if not task.endLogged then          -- once, not every frame while it brakes to rest
        task.endLogged = true
        logMotionEnd("unpark", code, task.session, Bridge.Hitch.describeUnpark)
    end
    if code ~= "CLEAR" then return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code end
    return settle(task, "UNPARKED")     -- braked by the latch; DONE once observed at rest
end

function aborts.unpark(env, task)
    abortMotion("unpark", env, task, Bridge.Hitch.describeUnpark)
end

-- ------------------------------------------------------------------- load
-- LOAD (direction 1) / UNLOAD (-1): the reserved tractor alone straight up the reserved
-- transport trailer's ramps onto its deck, or straight back off (Hitch.newLoad, AGENTS.md
-- exception 5). The ramps and belts are the companion's steps (fold, belts); this op
-- only drives, and refuses unless the ramps are down and the belts open.
function starters.load(env, spec)
    local denied = idle(env, spec.tractor, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local direction = (spec.direction == 1 and 1) or (spec.direction == -1 and -1) or nil
    if direction == nil then return nil, "INVALID_DIRECTION" end
    local session, reason, refused = Bridge.Hitch.newLoad(env, {
        tractor = spec.tractor, trailer = spec.wagon, direction = direction, farmId = spec.farmId,
        isReserved = spec.owned,
    })
    if session == nil then
        log("load refused %s: %s", tostring(reason), Bridge.Hitch.describeLoad(refused))
        return nil, reason
    end
    log("load start: %s", Bridge.Hitch.describeLoad(session))
    Bridge.lastLoad = session
    return { session = session }
end

function steppers.load(env, task, dt)
    local code = Bridge.Hitch.loadStep(env, task.session, dt)
    if code == "CONTINUE" then return nil end
    if not task.endLogged then
        task.endLogged = true
        logMotionEnd("load", code, task.session, Bridge.Hitch.describeLoad)
    end
    if code ~= "LOADED" and code ~= "UNLOADED" then
        return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code
    end
    return settle(task, code)           -- braked by the latch; DONE once observed at rest
end

function aborts.load(env, task)
    abortMotion("load", env, task, Bridge.Hitch.describeLoad)
end

-- ---------------------------------------------------------------- backoff
-- BACKOFF (AGENTS.md exception 5): the reserved tractor straight back from the reserved
-- transport trailer's ramps it waits behind, so GIANTS can line it up again (Hitch.newBackOff).
function starters.backoff(env, spec)
    local denied = idle(env, spec.tractor, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local session, reason, refused = Bridge.Hitch.newBackOff(env, {
        tractor = spec.tractor, trailer = spec.wagon, farmId = spec.farmId, isReserved = spec.owned,
    })
    if session == nil then
        log("backoff refused %s: %s", tostring(reason), Bridge.Hitch.describeBackOff(refused))
        return nil, reason
    end
    log("backoff start: %s", Bridge.Hitch.describeBackOff(session))
    return { session = session }
end

function steppers.backoff(env, task, dt)
    local code = Bridge.Hitch.backOffStep(env, task.session, dt)
    if code == "CONTINUE" then return nil end
    if not task.endLogged then
        task.endLogged = true
        logMotionEnd("backoff", code, task.session, Bridge.Hitch.describeBackOff)
    end
    if code ~= "BACKED_OFF" then return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code end
    return settle(task, code)
end

function aborts.backoff(env, task)
    abortMotion("backoff", env, task, Bridge.Hitch.describeBackOff)
end

-- -------------------------------------------------------------- alongside
-- ALONGSIDE (AGENTS.md exception 6): the reserved tractor with its towed trailer straight
-- forward until the trailer's middle is under the reserved standing combine's pipe
-- (Hitch.newAlongside). The pipe is the companion's step (pipe); this op only drives.
-- direction 1: under the pipe; -1 (pass): after the overload, on along the line until the rig
-- is past the combine, so GIANTS' agent can start it again.
function starters.alongside(env, spec)
    local denied = idle(env, spec.tractor, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if spec.direction ~= 1 and spec.direction ~= -1 then return nil, "INVALID_DIRECTION" end
    local session, reason, refused = Bridge.Hitch.newAlongside(env, {
        tractor = spec.tractor, combine = spec.wagon, farmId = spec.farmId, isReserved = spec.owned,
        pass = spec.direction == -1,
    })
    if session == nil then
        log("alongside refused %s: %s", tostring(reason), Bridge.Hitch.describeAlongside(refused))
        return nil, reason
    end
    log("alongside start: %s", Bridge.Hitch.describeAlongside(session))
    return { session = session }
end

function steppers.alongside(env, task, dt)
    local code = Bridge.Hitch.alongsideStep(env, task.session, dt)
    if code == "CONTINUE" then return nil end
    if not task.endLogged then
        task.endLogged = true
        logMotionEnd("alongside", code, task.session, Bridge.Hitch.describeAlongside)
    end
    if code ~= "UNDER_PIPE" and code ~= "PASSED" then
        return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code
    end
    return settle(task, code)
end

function aborts.alongside(env, task)
    abortMotion("alongside", env, task, Bridge.Hitch.describeAlongside)
end

-- ----------------------------------------------------------------- escort
-- ESCORT: the reserved tractor's trailer held under the pipe of a combine harvesting under
-- GIANTS' helper, at its speed, behind it on its trail while it turns (Hitch.newEscort): the
-- combine unloaded on the move. DONE TRAILER_FULL or COMBINE_STOPPED (its helper ended), braked.
-- It runs beside other operations (Bridge.CONCURRENT): only its own cart and combine conflict.
Bridge.ESCORT_LOG_MS = 5000

function starters.escort(env, spec)
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if spec.tractor.ownerFarmId ~= spec.farmId then return nil, "NOT_OWNED" end
    local session, reason, refused = Bridge.Hitch.newEscort(env, {
        tractor = spec.tractor, combine = spec.wagon, farmId = spec.farmId, isReserved = spec.owned,
    })
    if session == nil then
        log("escort refused %s: %s", tostring(reason), Bridge.Hitch.describeEscort(refused))
        return nil, reason
    end
    log("escort start: %s", Bridge.Hitch.describeEscort(session))
    return { session = session, logAt = 0 }
end

function steppers.escort(env, task, dt)
    local code = Bridge.Hitch.escortStep(env, task.session, dt)
    if task.elapsed >= task.logAt then
        task.logAt = task.elapsed + Bridge.ESCORT_LOG_MS
        log("escort %s", Bridge.Hitch.describeEscort(task.session))
    end
    if code == "CONTINUE" then return nil end
    if not task.endLogged then
        task.endLogged = true
        logMotionEnd("escort", code, task.session, Bridge.Hitch.describeEscort)
        for i, row in ipairs(task.session.rows or {}) do
            if i % 5 == 1 then
                log("escort row t=%d mode=%s vc=%.1f v=%s err=%s lat=%s pipe=%s level=%.0f", row.t, row.mode, row.vc,
                    row.v and string.format("%.1f", row.v) or "?", row.err and string.format("%.2f", row.err) or "?",
                    row.lat and string.format("%.2f", row.lat) or "?", tostring(row.pipe), row.level or 0)
            end
        end
    end
    if code ~= "TRAILER_FULL" and code ~= "COMBINE_STOPPED" and code ~= "TRAILER_WRONG_CROP" then
        return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code
    end
    return settle(task, code)
end

function aborts.escort(env, task)
    abortMotion("escort", env, task, Bridge.Hitch.describeEscort)
end

-- ----------------------------------------------------------------- follow
-- "Follow me", only when the player asks (AGENTS.md): FollowMe's own start, its registered
-- vehicle function initiateFollowVehicle(leader) (FollowVehicle.lua 556-571: FollowMe's job
-- MOD_FOLLOW_VEHICLE created, validated and started with GIANTS' aiSystem:startJob), the leader
-- being the vehicle the player sits in (it carries FollowMe too). Runs while FollowMe says it
-- follows (getIsFollowVehicleActive); DONE when it ends (the player's FollowMe key or Farmhand's
-- stop, which ends the AI job as GIANTS' own toggle does). Never unloads or does anything else.
Bridge.FOLLOW_START_MS = 10000
Bridge.FOLLOW_TIMEOUT_MS = 7200000

local function playersVehicle(env, except)
    local vehicles = env.g_currentMission.vehicleSystem and env.g_currentMission.vehicleSystem.vehicles
        or env.g_currentMission.vehicles
    for _, v in pairs(vehicles or {}) do
        if v ~= except and type(v.getIsEntered) == "function" and v:getIsEntered() == true then return v end
    end
    return nil
end

function starters.follow(env, spec)
    local v = spec.tractor
    if v.ownerFarmId ~= spec.farmId then return nil, "NOT_OWNED" end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if type(v.initiateFollowVehicle) ~= "function" or type(v.getIsFollowVehicleActive) ~= "function"
            or type(v.getCanStartFollowVehicle) ~= "function" then
        return nil, "FOLLOWME_UNAVAILABLE"
    end
    if v:getIsEntered() == true then return nil, "SEATED_IN_FOLLOWER" end
    local leader = playersVehicle(env, v)
    if leader == nil then return nil, "PLAYER_NOT_IN_VEHICLE" end
    if type(leader.addTrailDrop) ~= "function" then return nil, "LEADER_UNSUPPORTED" end
    if v:getCanStartFollowVehicle() ~= true then return nil, "FOLLOW_NOT_ALLOWED" end
    v:initiateFollowVehicle(leader)
    return { leader = leader }
end

function steppers.follow(_env, task)
    local v = task.spec.tractor
    if task.spec.owned() ~= true then return "FAILED", "RESERVATION_LOST" end
    local active = v:getIsFollowVehicleActive()
    if active == true then task.seen = true end
    if not task.seen then
        if task.elapsed > Bridge.FOLLOW_START_MS then return "FAILED", "FOLLOW_NOT_STARTED" end
        return nil
    end
    if active == true then
        if task.elapsed > Bridge.FOLLOW_TIMEOUT_MS then return "FAILED", "TIMEOUT" end
        return nil
    end
    return settle(task, "FOLLOW_ENDED")
end

function aborts.follow(env, task)
    local v = task.spec.tractor
    if v:getIsFollowVehicleActive() == true then Bridge.Hitch.cancelStaging(env, v) end
end

-- What the rig carries to sell: the first filled unit behind the tractor (getFillUnits,
-- getFillUnitFillLevel, getFillUnitFillType as Observe.fillUnits reads them).
local function cargoFillType(v)
    for _, impl in ipairs(v:getAttachedImplements() or {}) do
        local o = impl.object
        if o ~= nil and type(o.getFillUnits) == "function" then
            for i in ipairs(o:getFillUnits()) do
                if (o:getFillUnitFillLevel(i) or 0) > 0 then return o:getFillUnitFillType(i) end
            end
        end
    end
    return nil
end

-- ------------------------------------------------------------------- fold
-- A reserved vehicle's own folding parts (a pickup's tailgate, a deckover's ramps) to one
-- end through GIANTS' Foldable, never its animations directly: getIsFoldAllowed 987-1009,
-- then setFoldDirection 811 (-> setFoldState 817-..., which sends GIANTS' own event).
-- DONE once getFoldAnimTime (962-964) is at that end and foldMoveDirection is 0
-- (onUpdate 552-611 stops it there); which end is "down" is the companion's to know.
Bridge.FOLD_TIMEOUT_MS = 20000

-- worktool (tractor, direction): the tractor's implements set to work outside a field (1) or
-- back (-1), for plowing the strip that joins two fields (the user, 2026-10-31: "add field 4 to
-- field 5 with create fields enabled"). 1: a plow's field limit off (Plow:setPlowLimitToField
-- false: the ALLOW CREATE FIELDS toggle, Plow.lua 316), refused where the game forbids creating
-- fields (getPlowForceLimitToField; the farm's createFields permission, Plow.lua 576-581), then
-- each AI implement lowered as GIANTS' AI starts a line (AIImplement:aiImplementStartLine
-- 831-838, its action controller lowers it). -1: raised as GIANTS' AI ends a line
-- (aiImplementEndLine 839-846) and the field limit back on. DONE once each says it is lowered
-- or raised (Attachable:getIsLowered), or after WORKTOOL_SETTLE_MS without that getter.
Bridge.WORKTOOL_SETTLE_MS = 4000
Bridge.WORKTOOL_TIMEOUT_MS = 15000

local function workTools(v)
    local out = {}
    local function add(u)
        for _, implement in ipairs(type(u.getAttachedImplements) == "function" and u:getAttachedImplements() or {}) do
            out[#out + 1] = implement.object
            add(implement.object)
        end
    end
    add(v)
    return out
end

function starters.worktool(env, spec)
    if spec.owned() ~= true or spec.tractor.ownerFarmId ~= spec.farmId then return nil, "RESERVATION_LOST" end
    local direction = (spec.direction == 1 and 1) or (spec.direction == -1 and -1) or nil
    if direction == nil then return nil, "INVALID_DIRECTION" end
    local tools = workTools(spec.tractor)
    local plows = 0
    for _, u in ipairs(tools) do
        if u.spec_plow ~= nil and type(u.setPlowLimitToField) == "function" then plows = plows + 1 end
    end
    if plows == 0 then return nil, "NO_PLOW_HITCHED" end
    local mission = env.g_currentMission
    if direction == 1 then
        for _, u in ipairs(tools) do
            if u.spec_plow ~= nil and type(u.getPlowForceLimitToField) == "function" and u:getPlowForceLimitToField() then
                return nil, "FIELD_CREATION_DISABLED"
            end
        end
        if type(mission.getHasPlayerPermission) == "function"
                and not mission:getHasPlayerPermission("createFields", nil, nil, true) then
            return nil, "FIELD_CREATION_NOT_PERMITTED"
        end
    end
    for _, u in ipairs(tools) do
        if direction == 1 then
            if u.spec_plow ~= nil and type(u.setPlowLimitToField) == "function" then u:setPlowLimitToField(false) end
            if type(u.aiImplementStartLine) == "function" then u:aiImplementStartLine() end
        else
            if type(u.aiImplementEndLine) == "function" then u:aiImplementEndLine() end
            if u.spec_plow ~= nil and type(u.setPlowLimitToField) == "function" then u:setPlowLimitToField(true) end
        end
    end
    log("worktool %s: %d implement(s) %s", tostring(env.NetworkUtil.getObjectId(spec.tractor)), #tools,
        direction == 1 and "lowered, field creation on" or "raised, field limit on")
    return { tools = tools, lower = direction == 1 }
end

function steppers.worktool(_env, task)
    if task.spec.owned() ~= true then return "FAILED", "RESERVATION_LOST" end
    local known, all = false, true
    for _, u in ipairs(task.tools) do
        if type(u.getIsLowered) == "function" then
            known = true
            local ok, lowered = pcall(u.getIsLowered, u)
            if not ok or (lowered == true) ~= task.lower then all = false end
        end
    end
    local code = task.lower and "LOWERED" or "RAISED"
    if known and all and task.elapsed >= 500 then return "DONE", code end
    if not known and task.elapsed >= Bridge.WORKTOOL_SETTLE_MS then return "DONE", code end
    if task.elapsed > Bridge.WORKTOOL_TIMEOUT_MS then return "FAILED", "WORKTOOL_TIMEOUT" end
    return nil
end

function aborts.worktool(_env, task)
    for _, u in ipairs(task.tools or {}) do
        pcall(function()
            if type(u.aiImplementEndLine) == "function" then u:aiImplementEndLine() end
            if u.spec_plow ~= nil and type(u.setPlowLimitToField) == "function" then u:setPlowLimitToField(true) end
        end)
    end
end

function starters.fold(env, spec)
    if spec.owned() ~= true or spec.tractor.ownerFarmId ~= spec.farmId then return nil, "RESERVATION_LOST" end
    local v = spec.tractor
    local foldable = v.spec_foldable
    if foldable == nil or next(foldable.foldingParts or {}) == nil then return nil, "NOT_FOLDABLE" end
    -- 2: folded for transport, away from the end GIANTS calls unfolded (Foldable:getIsUnfolded:
    -- foldAnimTime at turnOnFoldDirection's end), whichever way this implement folds.
    local direction = (spec.direction == 1 and 1) or (spec.direction == -1 and -1)
        or (spec.direction == 2 and -(foldable.turnOnFoldDirection == -1 and -1 or 1)) or nil
    if direction == nil then return nil, "INVALID_DIRECTION" end
    if not stopped(v) then return nil, "NOT_STOPPED" end
    if v:getIsFoldAllowed(direction, false) ~= true then return nil, "FOLD_NOT_ALLOWED" end
    v:setFoldDirection(direction)
    local okId, id = pcall(function() return env.NetworkUtil.getObjectId(v) end)
    log("fold %s towards %d from %s", okId and tostring(id) or "?", direction, tostring(v:getFoldAnimTime()))
    return { target = direction > 0 and 1 or 0 }
end

function steppers.fold(_env, task)
    local v = task.spec.tractor
    if task.spec.owned() ~= true then return "FAILED", "RESERVATION_LOST" end
    local t = v:getFoldAnimTime()
    if not finite(t) then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
    if v.spec_foldable.foldMoveDirection == 0 and math.abs(t - task.target) < 0.001 then return "DONE", "FOLDED" end
    if task.elapsed > Bridge.FOLD_TIMEOUT_MS then return "FAILED", "FOLD_TIMEOUT" end
    return nil
end

function aborts.fold() end          -- GIANTS finishes the fold it started; nothing drives

-- ---------------------------------------------------------------- deliver
-- GIANTS' own Deliver helper takes the rig's cargo to a selling station and tips it there
-- (AIJobTypeManager "DELIVER" -> AIJobDeliver: applyCurrentState 173-212 lists the stations
-- GIANTS' AI can unload at; getStartTaskIndex 258-288 skips the loading leg when the trailer
-- already holds something; the drive and AITaskDischarge are GIANTS'). The station: the best
-- paid SellingStation for what the rig carries among those (getIsFillTypeAISupported,
-- getEffectiveFillTypePrice as ADTriggerManager:getHighestPayingSellStation reads it); no
-- looping. DONE once GIANTS' job ends with the rig at rest; the companion checks the cargo
-- went and the money came. direction -1 (store): the farm's own storage instead: the nearest
-- of those stations that is a silo's own unloading station (PlaceableSilo:onLoad 100-102 sets
-- spec_silo.unloadingStation.owningPlaceable to the silo) on a placeable the farm owns, taking
-- the cargo (getIsFillTypeAISupported); never an animal feeder or a production's input; nothing
-- is sold.
Bridge.DELIVER_TIMEOUT_MS = 14400000    -- a safety bound (as DRIVE_TIMEOUT_MS); progress is DELIVER_STALL_MS
Bridge.DELIVER_STALL_MS = 600000        -- GIANTS' helper running but unmoved 2 m for 10 min (tipping included)

-- Whether a farm's running mission that is paid by deliveries (a harvest contract: fillSold) takes
-- this fill type at the station (SellingStation.missions, as its own addFillLevelFromTool reads it).
function Bridge.contractStation(station, fillType, farmId)
    for key, value in pairs(station.missions or {}) do
        local mission = type(value) == "table" and value or key
        if type(mission) == "table" and mission.fillSold ~= nil and mission.fillTypeIndex == fillType
                and mission.farmId == farmId then
            return true
        end
    end
    return false
end

function Bridge.deliverStation(env, spec, job, fillType, v)
    local store = spec.direction == -1
    local best, bestScore, vx, vz = nil, nil, nil, nil
    if store then
        local x, _, z = env.getWorldTranslation(v.rootNode)
        vx, vz = x, z
    end
    for _, id in ipairs(job.unloadingStationParameter.unloadingStationIds or {}) do
        local station = env.NetworkUtil.getObject(id)
        if station ~= nil and station:getIsFillTypeAISupported(fillType) then
            local selling = station:isa(env.SellingStation) == true
            if not store and selling and Bridge.contractStation(station, fillType, spec.farmId) then
                -- This farm's harvest contract takes this crop here (SellingStation:addFillLevelFromTool
                -- credits it to the mission, HarvestMission:fillSold): the contract's station first.
                return station, station:getEffectiveFillTypePrice(fillType)
            elseif not store and selling then
                local price = station:getEffectiveFillTypePrice(fillType)
                if finite(price) and (bestScore == nil or price > bestScore) then best, bestScore = station, price end
            elseif store and not selling and station.owningPlaceable ~= nil
                    and station.owningPlaceable.spec_silo ~= nil
                    and station.owningPlaceable.spec_silo.unloadingStation == station
                    and station.owningPlaceable.ownerFarmId == spec.farmId then
                local sx, _, sz = env.getWorldTranslation(station.owningPlaceable.rootNode)
                local d = math.sqrt((sx - vx) ^ 2 + (sz - vz) ^ 2)
                if finite(d) and (bestScore == nil or -d > bestScore) then best, bestScore = station, -d end
            end
        end
    end
    return best, bestScore
end

function starters.deliver(env, spec)
    local v = spec.tractor
    local denied = idle(env, v, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local okType, fillType = pcall(cargoFillType, v)
    if not okType or fillType == nil then return nil, "NOTHING_TO_SELL" end
    local ok, reason = pcall(function()
        local mission = env.g_currentMission
        local job = mission.aiJobTypeManager:createJob(env.AIJobType.DELIVER)
        if job == nil then return "NO_DELIVER_JOB" end
        if job:getIsAvailableForVehicle(v) ~= true then return "DELIVER_UNAVAILABLE" end
        job:applyCurrentState(v, mission, spec.farmId, false)
        local best, score = Bridge.deliverStation(env, spec, job, fillType, v)
        if best == nil then return spec.direction == -1 and "NO_STORAGE" or "NO_SELL_POINT" end
        job.unloadingStationParameter:setUnloadingStation(best)
        job.loopingParameter:setIsLooping(false)
        job:setValues()
        if job:validate(spec.farmId) ~= true then return "DELIVER_INVALID" end
        Bridge.aiStops[v] = nil
        if spec.direction == -1 then
            log("deliver: into storage %s, %.0f m off", tostring(best:getName()), -score)
        else
            log("deliver: to %s at %.3f per litre", tostring(best:getName()), score)
        end
        mission.aiSystem:startJob(Bridge.farmhandJob(job), spec.farmId)
        return nil
    end)
    if not ok then return nil, "DELIVER_ERROR" end
    if reason ~= nil then return nil, reason end
    return { seen = false }
end

function steppers.deliver(env, task)
    local status, code = stillOurs(task.spec)
    if status ~= nil then return status, code end
    local v = task.spec.tractor
    local active = v:getIsAIActive()
    if active == true then
        task.seen = true
        v:raiseActive()
        if task.elapsed > Bridge.DELIVER_TIMEOUT_MS then return "FAILED", "TIMEOUT" end
        local okAt, x, _, z = pcall(env.getWorldTranslation, v.rootNode)
        if okAt and finite(x) and finite(z) then
            if task.fromX == nil or math.sqrt((x - task.fromX) ^ 2 + (z - task.fromZ) ^ 2) >= 2 then
                task.fromX, task.fromZ, task.fromAt = x, z, task.elapsed
            elseif task.elapsed - task.fromAt > Bridge.DELIVER_STALL_MS then
                return "FAILED", "DELIVER_STALLED"
            end
        end
        return nil
    end
    if not task.seen then
        if task.elapsed > Bridge.START_TIMEOUT_MS then return "FAILED", Bridge.aiStops[v] or "DELIVER_NOT_STARTED" end
        return nil
    end
    return settle(task, Bridge.aiStops[v] or "AI_STOPPED")
end

aborts.deliver = abortGoto

-- ------------------------------------------------------------------ pipe / overload
-- A standing combine emptied into a trailer the way GIANTS' own overloading does it (the
-- user 2026-10-21: the combine was full and its helper would not work on).
-- pipe (vehicle, direction): Pipe:setPipeState (732-759) to its first unloading state
-- (spec.unloadingStates, as Pipe:onUpdateTick's own unfolding 527-553) or back to 1;
-- DONE once spec.currentState is there (getCurrentPipeState 763-765).
-- overload (combine, trailer): GIANTS discharges into the object under the pipe by itself
-- (automaticDischarge, default true, 200; handleDischargeRaycast 1254-1277) while the
-- combine is powered (Motorized:getIsPowered 3341-): its motor is started
-- (Motorized:startMotor 2153-2158) and it is kept active, nothing drives. DONE once the
-- combine is empty or the trailer full (EMPTIED / TRAILER_FULL), or nothing more flows for
-- OVERLOAD_IDLE_MS after it did (OVERLOAD_STOPPED); FAILED NOT_OVERLOADING when nothing
-- flowed at all. The companion verifies both fill levels from the snapshot.
Bridge.PIPE_TIMEOUT_MS = 20000
Bridge.OVERLOAD_IDLE_MS = 15000
Bridge.OVERLOAD_TIMEOUT_MS = 300000

local function dischargeFill(v)
    local node = v:getCurrentDischargeNode()
    if node == nil then return nil end
    return v:getFillUnitFillLevel(node.fillUnitIndex), v:getFillUnitCapacity(node.fillUnitIndex)
end

-- Combines Farmhand keeps powered between ops (pipe out, then the overload): every one is let
-- go again by the op that ends it, by a failed or stopped pipe-out, and when the session drops.
Bridge.powered = {}
function Bridge.unpower(v)
    Bridge.powered[v] = nil
    v.forceIsActive = false
end

local function keepPowered(v)
    Bridge.powered[v] = true
    v.forceIsActive = true
    v:raiseActive()
    local state = v:getMotorState()
    if state ~= Bridge.MotorState.ON and state ~= Bridge.MotorState.STARTING and v:getCanMotorRun() then v:startMotor(true) end
end

function starters.pipe(env, spec)
    if spec.owned() ~= true or spec.tractor.ownerFarmId ~= spec.farmId then return nil, "RESERVATION_LOST" end
    local v = spec.tractor
    local direction = (spec.direction == 1 and 1) or (spec.direction == -1 and -1) or nil
    if direction == nil then return nil, "INVALID_DIRECTION" end
    local pipe = v.spec_pipe
    if pipe == nil or type(pipe.unloadingStates) ~= "table" then return nil, "NO_PIPE" end
    if not stopped(v) then return nil, "NOT_STOPPED" end
    if v:getIsAIActive() == true then return nil, "AI_ACTIVE" end
    local target = direction > 0 and next(pipe.unloadingStates) or 1
    if target == nil then return nil, "NO_UNLOADING_STATE" end
    if pipe.currentState ~= target and v:getIsPipeStateChangeAllowed(target) ~= true then return nil, "PIPE_NOT_ALLOWED" end
    Bridge.MotorState = env.MotorState
    if direction > 0 then keepPowered(v) end
    v:setPipeState(target)
    log("pipe %s to state %s from %s", tostring(env.NetworkUtil.getObjectId(v)), tostring(target), tostring(pipe.currentState))
    return { target = target }
end

function steppers.pipe(_env, task)
    if task.spec.owned() ~= true then return "FAILED", "RESERVATION_LOST" end
    local v = task.spec.tractor
    if v:getCurrentPipeState() == task.target then
        if task.target == 1 then Bridge.unpower(v) end
        return "DONE", task.target == 1 and "PIPE_IN" or "PIPE_OUT"
    end
    if task.target ~= 1 then keepPowered(v) end
    if task.elapsed > Bridge.PIPE_TIMEOUT_MS then return "FAILED", "PIPE_TIMEOUT" end
    return nil
end

-- GIANTS finishes the pipe move it started; nothing drives. A pipe-out that failed or was stopped
-- lets the combine go (nothing will overload from it).
function aborts.pipe(_env, task)
    if task.target ~= 1 then Bridge.unpower(task.spec.tractor) end
end

-- The trailer's cover opened for the discharge the way GIANTS' own AI loading does it
-- (Cover:aiPrepareLoading: getCoverByFillUnitIndex(i) -> setCoverState(cover.index)) and put
-- back afterwards (Cover:aiFinishLoading: setCoverState(0) when it was closed). Live 2026-10-31
-- (save 1, the user): "the cover is still on the trailer so it cant overload". The state it had,
-- or nil (no cover, or already open).
function Bridge.openCover(trailer)
    local spec = trailer.spec_cover
    if spec == nil or spec.hasCovers ~= true or type(trailer.getCoverByFillUnitIndex) ~= "function" then return nil end
    for i in ipairs(trailer:getFillUnits()) do
        local cover = trailer:getCoverByFillUnitIndex(i)
        if cover ~= nil then
            if spec.state == cover.index then return nil end
            local before = spec.state
            trailer:setCoverState(cover.index)
            return before
        end
    end
    return nil
end

local function restoreCover(task)
    local trailer = task.spec.wagon
    if task.coverBefore ~= nil and trailer.spec_cover ~= nil then
        pcall(function() trailer:setCoverState(task.coverBefore) end)
        task.coverBefore = nil
    end
end

local function coverMoving(trailer)
    local spec = trailer.spec_cover
    return spec ~= nil and type(spec.runningAnimations) == "table" and #spec.runningAnimations > 0
end

function starters.overload(env, spec)
    if spec.owned() ~= true or spec.tractor.ownerFarmId ~= spec.farmId then return nil, "RESERVATION_LOST" end
    local v = spec.tractor
    local pipe = v.spec_pipe
    if pipe == nil or type(pipe.unloadingStates) ~= "table" then return nil, "NO_PIPE" end
    if pipe.unloadingStates[v:getCurrentPipeState()] == nil then return nil, "PIPE_NOT_OUT" end
    local level = dischargeFill(v)
    if level == nil then return nil, "NO_DISCHARGE_NODE" end
    Bridge.MotorState = env.MotorState
    keepPowered(v)
    -- A seated player only watches (the user, 2026-10-31: "im sitting in the combine but that
    -- should not stop farmhand"); their own controls take over (Hitch.driverConflict).
    Bridge.Hitch.watchDriver(env, v)
    Bridge.Hitch.watchDriver(env, spec.wagon.rootVehicle or spec.wagon)
    local coverBefore = Bridge.openCover(spec.wagon)
    log("overload %s into %s from %.0f l%s", tostring(env.NetworkUtil.getObjectId(v)),
        tostring(env.NetworkUtil.getObjectId(spec.wagon)), level, coverBefore ~= nil and " (its cover opened)" or "")
    return { start = level, last = level, lastAt = 0, flowed = false, coverBefore = coverBefore }
end

function steppers.overload(env, task)
    local status, code = Bridge.stepOverload(env, task)
    if status ~= nil then restoreCover(task) end
    return status, code
end

function Bridge.stepOverload(_env, task)
    if task.spec.owned() ~= true then return "FAILED", "RESERVATION_LOST" end
    local v, trailer = task.spec.tractor, task.spec.wagon
    -- The player driving the combine or the tractor under its pipe: theirs now (aborts fold the
    -- pipe). Sitting in either is no takeover.
    local rig = trailer.rootVehicle or trailer
    if Bridge.Hitch.takeovers[v] or Bridge.Hitch.takeovers[rig] then return "FAILED", "DRIVER_TOOK_OVER" end
    keepPowered(v)
    if coverMoving(trailer) then task.lastAt = task.elapsed end      -- the cover still opening
    local level = dischargeFill(v)
    if level == nil then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
    if level < task.last - 1 then task.last, task.lastAt, task.flowed = level, task.elapsed, true end
    local free = 0
    for i in ipairs(trailer:getFillUnits()) do
        free = free + math.max(0, trailer:getFillUnitCapacity(i) - trailer:getFillUnitFillLevel(i))
    end
    if level < 1 then Bridge.unpower(v); return "DONE", "EMPTIED" end
    if task.flowed and free < 1 then Bridge.unpower(v); return "DONE", "TRAILER_FULL" end
    if task.elapsed - task.lastAt > Bridge.OVERLOAD_IDLE_MS then
        Bridge.unpower(v)
        log("overload ended at %.0f l (from %.0f l)", level, task.start)
        if task.flowed then return "DONE", "OVERLOAD_STOPPED" end
        return "FAILED", "NOT_OVERLOADING"
    end
    if task.elapsed > Bridge.OVERLOAD_TIMEOUT_MS then Bridge.unpower(v); return "FAILED", "OVERLOAD_TIMEOUT" end
    return nil
end

-- Stopped: the discharge ends as GIANTS' own does, the pipe back to its folded state
-- (Pipe:setPipeState 732-759, as the companion's pipe step); nothing drives.
function aborts.overload(_env, task)
    local v = task.spec.tractor
    Bridge.unpower(v)
    pcall(function() v:setPipeState(1) end)
    restoreCover(task)
end

-- ------------------------------------------------------------------ belts
-- A reserved transport's tension belts through GIANTS' TensionBelts: setAllTensionBeltsActive
-- (912-920; each belt's setTensionBeltsActive 851-907 mounts or frees what lies under it and
-- sets its tensionMountObject). DONE once areAllBeltsFastened (updateFastenState 925-935)
-- says so; what is held is the companion's to verify from the snapshot.
Bridge.BELTS_TIMEOUT_MS = 5000

function starters.belts(_, spec)
    if spec.owned() ~= true or spec.tractor.ownerFarmId ~= spec.farmId then return nil, "RESERVATION_LOST" end
    local v = spec.tractor
    local direction = (spec.direction == 1 and 1) or (spec.direction == -1 and -1) or nil
    if direction == nil then return nil, "INVALID_DIRECTION" end
    local belts = v.spec_tensionBelts
    if belts == nil or not belts.hasTensionBelts then return nil, "NO_BELTS" end
    if not stopped(v) then return nil, "NOT_STOPPED" end
    v:setAllTensionBeltsActive(direction > 0)
    return { fasten = direction > 0 }
end

function steppers.belts(env, task)
    local belts = task.spec.tractor.spec_tensionBelts
    if belts.areAllBeltsFastened == task.fasten then
        local held = {}
        for _, data in pairs(belts.objectsToJoint) do
            held[#held + 1] = tostring(data.object and env.NetworkUtil.getObjectId(data.object))
        end
        log("belts %s: holding %s", task.fasten and "fastened" or "released", table.concat(held, ","))
        return "DONE", task.fasten and "BELTS_FASTENED" or "BELTS_RELEASED"
    end
    if task.elapsed > Bridge.BELTS_TIMEOUT_MS then return "FAILED", "BELTS_NOT_OBSERVED" end
    return nil
end

function aborts.belts() end

-- -------------------------------------------------------------- fieldwork
-- GIANTS' field-work job, started like GOTO (Hitch.startStaging): AIJobTypeManager registers "FIELDWORK" ->
-- AIJobFieldWork (36); applyCurrentState(vehicle, mission, farmId, false) (134-163),
-- positionAngle at a point inside the field (setPosition/setAngle, snapped), setValues
-- (199-211), validate (vehicle only, 215-224), aiSystem:startJob (AISystem 540-559).
-- Task 1 drives there (AITaskDriveTo), task 2 works the field found from that
-- position (AIDriveStrategyFieldCourse:setAIVehicle 47-139). The job ends only through
-- an AI message (AISystem:stopJobInternal publishes AI_JOB_STOPPED (job, aiMessage),
-- 576-581); its class is the reason. An ended job is never "worked": the companion
-- measures the field's state.
Bridge.AI_STOP_CODES = {
    { "AIMessageSuccessFinishedJob", "FINISHED" },
    { "AIMessageSuccessStoppedByUser", "STOPPED_BY_USER" },
    { "AIMessageErrorFieldNotOwned", "FIELD_NOT_OWNED" },
    { "AIMessageErrorNoFieldFound", "NO_FIELD_FOUND" },
    { "AIMessageErrorFieldNotReady", "FIELD_NOT_READY" },
    { "AIMessageErrorOutOfFill", "OUT_OF_FILL" },
    { "AIMessageErrorOutOfMoney", "OUT_OF_MONEY" },
    { "AIMessageErrorOutOfFuel", "OUT_OF_FUEL" },
    { "AIMessageErrorWrongSeason", "WRONG_SEASON" },
    { "AIMessageErrorNotReachable", "NOT_REACHABLE" },
    { "AIMessageErrorBlockedByObject", "BLOCKED_BY_OBJECT" },
    { "AIMessageErrorCouldNotPrepare", "COULD_NOT_PREPARE" },
    { "AIMessageErrorImplementWrongWay", "IMPLEMENT_WRONG_WAY" },
    { "AIMessageErrorVehicleBroken", "VEHICLE_BROKEN" },
}
Bridge.aiStops = setmetatable({}, { __mode = "k" })   -- vehicle -> code of its last ended job
Bridge.handed = setmetatable({}, { __mode = "k" })    -- vehicle -> the op whose job runs on without a command

-- AI_JOB_STOPPED listener: the stopped job's vehicle and why (ClassUtil as
-- AIMessageManager:getMessageIndex 137-145). Diagnostics and the op's end code only.
function Bridge.onAIJobStopped(env, job, aiMessage)
    -- Each read on its own (live 2026-10-21: a combine's helper ended and nothing was
    -- logged, so one failing read must not hide the rest).
    local okV, vehicle = pcall(function() return job.vehicleParameter:getVehicle() end)
    local code = "AI_STOPPED"
    for _, entry in ipairs(Bridge.AI_STOP_CODES) do
        local class = env[entry[1]]
        local ok, is = pcall(function()
            return (type(aiMessage.isa) == "function" and aiMessage:isa(class))
                or env.ClassUtil.getClassObjectByObject(aiMessage) == class
        end)
        if class ~= nil and ok and is then code = entry[2] end
    end
    local okN, name = pcall(function() return env.ClassUtil.getClassNameByObject(aiMessage) end)
    if okV and vehicle ~= nil then Bridge.aiStops[vehicle] = code end
    local okId, id = pcall(function() return env.NetworkUtil.getObjectId(vehicle) end)
    log("AI job ended for vehicle %s: %s (%s)", okId and tostring(id) or "?", code, okN and tostring(name) or "?")
end

-- Diagnostics only, every read protected: where GIANTS' field-work job stands. The job's
-- current task (1 drive-to, 2 field work; AIJob:update 117-164), field work active
-- (getIsFieldWorkActive), the AI preparing its tools (getIsAIPreparingToDrive,
-- AIDrivable 636-648), each attached tool lowered / turned on, the speed.
function Bridge.logFieldWork(_env, v, what)
    local function read(f) local ok, value = pcall(f); return ok and tostring(value) or "?" end
    local tools = {}
    pcall(function()
        for _, impl in ipairs(v:getAttachedImplements()) do
            local o = impl.object
            tools[#tools + 1] = string.format("%s lowered=%s on=%s", read(function() return o:getName() end),
                read(function() return o:getIsLowered() end), read(function() return o:getIsTurnedOn() end))
        end
    end)
    log("fieldwork %s: task %s fieldWorkActive %s preparing %s speed %s aiActive %s tools [%s]", what,
        read(function() return v:getJob().currentTaskIndex end),
        read(function() return v:getIsFieldWorkActive() end),
        read(function() return v:getIsAIPreparingToDrive() end),
        read(function() return v:getLastSpeed() end),
        read(function() return v:getIsAIActive() end), table.concat(tools, "; "))
end

-- The crop to sow: position `seed` in the hitched seeder's own list (spec.seeds, as
-- Observe reports it), set by GIANTS' SowingMachine:setSeedFruitType (432-440, which
-- calls setSeedIndex 398-414: currentSeed and the AI parameters); confirmed read back.
function Bridge.chooseSeed(v, seed)
    if not (finite(seed) and seed % 1 == 0 and seed >= 1) then return "INVALID_SEED" end
    local ok, reason = pcall(function()
        local seeders = {}
        for _, impl in ipairs(v:getAttachedImplements() or {}) do
            if impl.object ~= nil and impl.object.spec_sowingMachine ~= nil then seeders[#seeders + 1] = impl.object end
        end
        if #seeders ~= 1 then return "NO_SEEDER" end
        local o = seeders[1]
        local spec = o.spec_sowingMachine
        local fruit = spec.seeds[seed]
        if fruit == nil then return "SEED_NOT_SUPPORTED" end
        o:setSeedFruitType(fruit)
        if spec.seeds[spec.currentSeed] ~= fruit then return "SEED_NOT_SET" end
        return nil
    end)
    if not ok then return "SEED_ERROR" end
    return reason
end

function starters.fieldwork(env, spec)
    local v = spec.tractor
    local denied = idle(env, v, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if not (finite(spec.x) and finite(spec.z) and finite(spec.yaw)) then return nil, "INVALID_POSE" end
    if spec.area == 0 then spec.area = nil end              -- 0: the whole field
    if spec.area ~= nil and not (finite(spec.area) and spec.area > 0 and spec.area <= Bridge.FIELDWORK_MAX_AREA_M2) then
        return nil, "INVALID_AREA"
    end
    Bridge.handed[v] = nil
    if spec.seed == 0 then spec.seed = nil end              -- 0: the crop the seeder is set to
    -- A combine always lays its straw in a swath where it can (the user 2026-10-21: the straw
    -- sells): Combine:setIsSwathActive(true) (927-950) where spec.swath.isAvailable (750-759).
    local combine = v.spec_combine
    if combine ~= nil and combine.swath ~= nil and combine.swath.isAvailable == true then
        local ok = pcall(function() v:setIsSwathActive(true) end)
        if not ok or combine.isSwathActive ~= true then return nil, "SWATH_NOT_SET" end
        log("fieldwork: straw swath on")
    end
    if spec.seed ~= nil then
        local seedDenied = Bridge.chooseSeed(v, spec.seed)
        if seedDenied ~= nil then return nil, seedDenied end
    end
    local ok, reason = pcall(function()
        local mission = env.g_currentMission
        local job = mission.aiJobTypeManager:createJob(env.AIJobType.FIELDWORK)
        if job == nil then return "NO_FIELDWORK_JOB" end
        if job:getIsAvailableForVehicle(v) ~= true then return "FIELDWORK_UNAVAILABLE" end
        job:applyCurrentState(v, mission, spec.farmId, false)
        job.positionAngleParameter:setPosition(spec.x, spec.z)
        job.positionAngleParameter:setAngle(spec.yaw)
        job:setValues()
        if job:validate(spec.farmId) ~= true then return "FIELDWORK_INVALID" end
        Bridge.aiStops[v] = nil
        pcall(Bridge.Observe.refreshField, env, spec.field)   -- the field as it was before
        mission.aiSystem:startJob(Bridge.farmhandJob(job), spec.farmId)
        return nil
    end)
    if not ok then return nil, "FIELDWORK_ERROR" end
    if reason ~= nil then return nil, reason end
    local cache = Bridge.Observe.fieldCache
    local before = cache and spec.field and cache[spec.field]
    return { seen = false, started = true, before = before and before.record, sinceCheck = 0 }
end

-- The m2 of the field whose state changed since `before`: the larger of the
-- ground-type and the spray-level share that moved (half the summed absolute share
-- differences), times the field's measured m2. nil if either is unmeasured.
function Bridge.changedArea(before, after)
    if type(before) ~= "table" or type(after) ~= "table" or not finite(after.total_m2) then return nil end
    local function moved(a, b)
        local names, sum, restA, restB = {}, 0, 1, 1
        for k, v in pairs(a or {}) do names[k] = true; restA = restA - v end
        for k, v in pairs(b or {}) do names[k] = true; restB = restB - v end
        for k in pairs(names) do sum = sum + math.abs(((b or {})[k] or 0) - ((a or {})[k] or 0)) end
        -- What is not listed (spray level 0, an unlisted ground type) is a share too: without it
        -- a spray's change counted half (companion/executor.py changed_area, the same rule).
        sum = sum + math.abs(math.max(0, restB) - math.max(0, restA))
        return sum / 2
    end
    return math.max(moved(before.ground, after.ground), moved(before.spray, after.spray)) * after.total_m2
end

function steppers.fieldwork(env, task, dt)
    local status, code = stillOurs(task.spec)
    if status ~= nil then return status, code end
    local v = task.spec.tractor
    local active = v:getIsAIActive()
    if active == true then task.seen = true end
    if not task.seen then
        if task.elapsed > Bridge.START_TIMEOUT_MS then return "FAILED", Bridge.aiStops[v] or "FIELDWORK_NOT_STARTED" end
        return nil
    end
    if active ~= false then
        if active ~= true then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
        if task.elapsed > Bridge.FIELDWORK_TIMEOUT_MS then return "FAILED", "TIMEOUT" end
        -- GIANTS' agent BLOCKED and the tractor never left its start, as navigate (live
        -- 9f1e101ef8da48d7-1: the Series 9 unloaded behind the GMC's deck, blocked 5 min).
        local stuck = blockedAtStart(env, task, v, "FIELDWORK_")
        if stuck ~= nil then return "FAILED", stuck end
        local x, _, z = env.getWorldTranslation(v.rootNode)
        if not (finite(x) and finite(z)) then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
        -- Handed off (hand 1, the user 2026-10-21: workers go on while Farmhand does something
        -- else): once GIANTS' helper works the field it is the game's hired worker; the op
        -- ends WORKING and a later stop for this vehicle still ends the helper's job.
        if task.spec.hand == 1 and task.spec.area == nil then
            local working = pcall(function() assert(v:getIsFieldWorkActive() == true) end)
            if working then
                Bridge.handed[v] = "fieldwork"
                log("fieldwork handed off: GIANTS' helper works field %s", tostring(task.spec.field))
                return "DONE", "WORKING"
            end
        end
        -- The worked track (implement working: field work active), for crop samples.
        task.track = task.track or {}
        task.sinceTrack = (task.sinceTrack or 0) + (dt or 0)
        if task.sinceTrack >= 2000 and #task.track < 200 then
            task.sinceTrack = 0
            local working = pcall(function() assert(v:getIsFieldWorkActive() == true) end)
            if working then task.track[#task.track + 1] = { x, z } end
        end
        if not task.workedOnce and pcall(function() assert(v:getIsFieldWorkActive() == true) end) then
            task.workedOnce = true
        end
        if not task.sampled and task.elapsed > 60000 then
            task.sampled = true
            Bridge.logFieldWork(env, v, "at 60 s")
        end
        if task.fromX == nil or math.sqrt((x - task.fromX) ^ 2 + (z - task.fromZ) ^ 2) >= Bridge.NAVIGATE_PROGRESS_M then
            task.fromX, task.fromZ, task.fromAt = x, z, task.elapsed
        elseif not task.workedOnce and task.elapsed - task.fromAt > Bridge.HELPER_PLANNING_MS
                and agentPlanning(env, v)
                and not pcall(function() assert(v:getIsFieldWorkActive() == true) end) then
            -- Its drive to the field never found a way (live e3e4f50db5174dc2-2: the Series 9
            -- just off the Deckover's ramps, the agent "planning" 5 min, unmoved): ended as GIANTS'
            -- NOT_REACHABLE, so the companion takes the pair onto the field by Farmhand's own drive.
            logAgent(env, v, "fieldwork drive never left its start")
            Bridge.aiStops[v] = "NOT_REACHABLE"
            Bridge.Hitch.cancelStaging(env, v)
            task.helperUnreached = true
        elseif task.elapsed - task.fromAt > Bridge.BLOCKED_START_MS and agentBlocked(env, v) then
            -- Unmoved with GIANTS' agent BLOCKED on its way: it waits for an obstacle it knows,
            -- and a parked vehicle never leaves (live 5eb1005df58446f7-1: the MT600 22 m from the
            -- GMC left on the road, 8 min); the companion moves what stands near it.
            logAgent(env, v, "fieldwork blocked on its way")
            return "FAILED", "FIELDWORK_BLOCKED"
        elseif task.elapsed - task.fromAt > Bridge.BLOCKED_START_MS and fieldWorkerBlocked(v) then
            -- GIANTS' helper holds for what is in its collision triggers, the player on foot
            -- among them (AICollisionTriggerHandler:countHitObject; live 87ec310879a546ea-1: the
            -- player 6 m ahead of the cultivating 615, FIELDWORK_STALLED after 5 min). For the
            -- player it waits, as GIANTS does, and says so once; anything else is moved by the
            -- companion, as a blocked agent.
            if not playerOnFootNear(env, x, z, Bridge.HELPER_PLAYER_M) then
                logAgent(env, v, "fieldwork blocked by an obstacle")
                return "FAILED", "FIELDWORK_BLOCKED"
            end
            if not task.toldPlayer then
                task.toldPlayer = true
                Bridge:notify(Bridge.NOTICES.HELPER_WAITS_FOR_PLAYER, false)
            end
        elseif task.elapsed - task.fromAt > Bridge.FIELDWORK_STALL_MS then
            logAgent(env, v, "fieldwork stalled")
            Bridge.logFieldWork(env, v, "stalled")
            -- Still driving to the field yet unmoved (live: stopped 64 m from field 3,
            -- likely a gate GIANTS' map treats as open), as NAVIGATE_STUCK.
            local driving = pcall(function()
                assert(tostring(env.AIDrivable.STATES[v.spec_aiDrivable.lastState]):upper() == "DRIVING")
            end)
            return "FAILED", driving and "FIELDWORK_STUCK" or "FIELDWORK_STALLED"
        end
        -- A bounded test area: once that much of the field changed, GIANTS' own stop.
        if task.spec.area ~= nil and not task.areaReached then
            task.sinceCheck = task.sinceCheck + (dt or 0)
            local working = pcall(function() assert(v:getIsFieldWorkActive() == true) end)
            local every = task.nearArea and 0 or working and Bridge.FIELDWORK_WORKING_CHECK_MS or Bridge.FIELDWORK_CHECK_MS
            if task.sinceCheck >= every then
                task.sinceCheck = 0
                local okShares, shares = pcall(Bridge.Observe.fieldShares, env, task.spec.field)
                local changed = Bridge.changedArea(task.before, okShares and shares or nil)
                -- Near the request, measured every frame and stopped at the first reading inside
                -- it: a stop takes at once (live f5c50ab7: a 750 ms lead stopped a 476 m2/s plow at
                -- 121 m2 of 400), and the measured rate says when the next check would pass it.
                local rate = 0
                if changed ~= nil and task.lastChanged ~= nil and task.elapsed > task.lastCheckAt then
                    rate = math.max(0, (changed - task.lastChanged) / (task.elapsed - task.lastCheckAt))
                end
                if changed ~= nil then task.lastChanged, task.lastCheckAt = changed, task.elapsed end
                local stopNow = changed ~= nil and changed >= task.spec.area * Bridge.FIELDWORK_AREA_SHARE
                if changed ~= nil and changed + rate * Bridge.FIELDWORK_WORKING_CHECK_MS >= task.spec.area then
                    task.nearArea = true
                end
                -- Each check, for the overshoot (live f460004990284cd1-1: 2016 m2 of 400 for a plow).
                local lowered = "?"
                pcall(function()
                    local implement = (v:getAttachedImplements() or {})[1]
                    lowered = tostring(implement and implement.object:getIsLowered())
                end)
                if changed ~= nil and changed ~= task.loggedChanged then
                    task.loggedChanged = changed
                    log("fieldwork check at %.1f s: %.0f m2 changed of %.0f (%.0f m2/s), lowered %s, %.1f km/h",
                        task.elapsed / 1000, changed, task.spec.area, rate * 1000, lowered, v:getLastSpeed())
                end
                if stopNow then
                    log("fieldwork: %.0f m2 of field %s changed (test area %.0f m2); stopping the helper",
                        changed, tostring(task.spec.field), task.spec.area)
                    task.areaReached = true
                    Bridge.Hitch.cancelStaging(env, v)
                end
            end
        end
        return nil
    end
    -- Ended: DONE with GIANTS' reason once at rest; the companion judges the field,
    -- measured again now.
    local ended, endCode = settle(task, task.areaReached and "AREA_REACHED"
        or task.helperUnreached and "NOT_REACHABLE" or Bridge.aiStops[v] or "AI_STOPPED")
    if ended ~= nil then pcall(Bridge.Observe.refreshField, env, task.spec.field, task.track) end
    return ended, endCode
end

aborts.fieldwork = abortGoto

-- ------------------------------------------------------------- open_gates
-- Open the closed gates within a few metres of where a vehicle is stuck, the way GIANTS
-- opens a gate itself (AnimatedObject:hourChanged 694-703: animation.direction = 1,
-- raiseActive; update 548-563 then animates it and, fully open, unblocks its AI region
-- with aiSystem:setBlockingRegionState). Live 2026-10-20: GIANTS' helpers stopped at
-- closed gates (the farm-store road, field 3's entrance) its map treats as open.
-- Gates: placeables' spec_animatedObjects.animatedObjects (PlaceableAnimatedObjects
-- onLoad 66-86), fence gates' animatedObjects (FenceGate:delete 156-166), else any
-- registered object that is an AnimatedObject. Enabled, not yet open ones only.
Bridge.MAX_GATE_RADIUS_M = 30

function gateCandidates(env)
    local found, seen = {}, {}
    local function add(o)
        if type(o) == "table" and o.animation ~= nil and o.nodeId ~= nil and not seen[o] then
            seen[o] = true
            found[#found + 1] = o
        end
    end
    pcall(function()
        for _, pl in pairs(env.g_currentMission.placeableSystem.placeables) do
            local spec = pl.spec_animatedObjects
            for _, o in ipairs(spec and spec.animatedObjects or {}) do add(o) end
            local fence = pl.spec_fence
            for _, gate in pairs(fence and fence.gates or {}) do
                for _, o in ipairs(gate.animatedObjects or {}) do add(o) end
            end
        end
    end)
    pcall(function()
        for _, o in pairs(env.g_server.objects) do
            if type(o) == "table" and type(o.isa) == "function" and o:isa(env.AnimatedObject) then add(o) end
        end
    end)
    return found
end

-- Live bfbc32565ecb4472-1: opening the Large Shed's animated wall was followed by the
-- Super-Liner rig moving about 12.5 m with no AI job and no Farmhand motion (cause
-- unproven). So a door opens only while every vehicle within DOOR_WATCH_M of it is
-- observed at rest, they are all watched until it has opened and settled, and any of
-- them moving DOOR_MOVED_M ends the op FAILED DOOR_MOVED_VEHICLE (the companion then
-- stops: no retry, no UNPARK). The door itself is left as it is.
Bridge.DOOR_WATCH_M, Bridge.DOOR_MOVED_M = 25, 0.25
Bridge.DOOR_OPEN_MS, Bridge.DOOR_SETTLE_MS = 6000, 2000

-- Every vehicle whose root is within `radius` of one of `points` (GIANTS' vehicle list,
-- as FarmhandHitch surroundings). Errors on an unreadable position.
local function vehiclesNear(env, points, radius)
    local mission = env.g_currentMission
    local vehicles = (mission.vehicleSystem and mission.vehicleSystem.vehicles) or mission.vehicles
    assert(type(vehicles) == "table")
    local near = {}
    for _, v in pairs(vehicles) do
        local x, _, z = env.getWorldTranslation(v.rootNode)
        assert(finite(x) and finite(z))
        for _, p in ipairs(points) do
            if math.sqrt((x - p[1]) ^ 2 + (z - p[2]) ^ 2) <= radius then
                near[#near + 1] = { vehicle = v, x = x, z = z }
                break
            end
        end
    end
    return near
end

local function vehicleName(env, v)
    local ok, id = pcall(function() return env.NetworkUtil.getObjectId(v) end)
    return ok and tostring(id) or "?"
end

function starters.open_gates(env, spec)
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if not (finite(spec.x) and finite(spec.z) and finite(spec.area)) or spec.area <= 0
            or spec.area > Bridge.MAX_GATE_RADIUS_M then
        return nil, "INVALID_AREA"
    end
    local doors, points, closest, near = {}, {}, nil, nil
    for _, o in ipairs(gateCandidates(env)) do
        pcall(function()
            local gx, _, gz = env.getWorldTranslation(o.nodeId)
            local d = math.sqrt((gx - spec.x) ^ 2 + (gz - spec.z) ^ 2)
            if closest == nil or d < closest then
                -- Evidence for the next decision: where it is and how open (0 closed, 1 open).
                near = string.format("%s at (%.1f, %.1f) open %.2f", tostring(o.saveId), gx, gz,
                    tonumber(o.animation and o.animation.time) or -1)
            end
            closest = math.min(closest or d, d)
            if d <= spec.area and o.isEnabled ~= false and (o.animation.time or 0) < 1 then
                doors[#doors + 1] = { o = o, x = gx, z = gz, d = d }
                points[#points + 1] = { gx, gz }
            end
        end)
    end
    log("open_gates near (%.1f, %.1f): %d closed; nearest gate %s m%s", spec.x, spec.z, #doors,
        closest and string.format("%.1f", closest) or "none", near and (": " .. near) or "")
    if #doors == 0 then return nil, "NO_CLOSED_GATE" end
    local ok, watched = pcall(vehiclesNear, env, points, Bridge.DOOR_WATCH_M)
    if not ok then return nil, "DOOR_AREA_UNKNOWN" end
    for _, w in ipairs(watched) do
        local okSpeed, still = pcall(stopped, w.vehicle)
        if not okSpeed then return nil, "DOOR_AREA_UNKNOWN" end
        if not still then
            log("open_gates refused: vehicle %s near the door is moving", vehicleName(env, w.vehicle))
            return nil, "DOOR_NEAR_MOVING_VEHICLE"
        end
    end
    for _, door in ipairs(doors) do
        door.o.animation.direction = 1
        door.o:raiseActive()
        log("opening gate %s at (%.1f, %.1f), %.1f m away; watching %d vehicle(s) within %d m",
            tostring(door.o.saveId), door.x, door.z, door.d, #watched, Bridge.DOOR_WATCH_M)
    end
    return { opened = #doors, watched = watched, doors = doors }
end

-- DONE once the doors had their animation time and every watched vehicle is still where
-- it was and at rest; any of them moving is DOOR_MOVED_VEHICLE at once.
function steppers.open_gates(env, task)
    -- Diagnostics only, once a second: how far each door has opened and how far and how
    -- fast each watched vehicle has moved (a push and a roll look different here).
    if task.elapsed >= (task.noteAt or 0) then
        task.noteAt = task.elapsed + 1000
        local parts = {}
        for _, door in ipairs(task.doors) do
            parts[#parts + 1] = string.format("%s %.2f", tostring(door.o.saveId),
                tonumber(door.o.animation and door.o.animation.time) or -1)
        end
        for _, w in ipairs(task.watched) do
            pcall(function()
                local x, _, z = env.getWorldTranslation(w.vehicle.rootNode)
                parts[#parts + 1] = string.format("%s %.2f m %.1f km/h", vehicleName(env, w.vehicle),
                    math.sqrt((x - w.x) ^ 2 + (z - w.z) ^ 2), w.vehicle:getLastSpeed())
            end)
        end
        log("open_gates t=%dms: %s", task.elapsed, table.concat(parts, "; "))
    end
    for _, w in ipairs(task.watched) do
        local ok, x, _, z = pcall(env.getWorldTranslation, w.vehicle.rootNode)
        if not ok or not (finite(x) and finite(z)) then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
        local moved = math.sqrt((x - w.x) ^ 2 + (z - w.z) ^ 2)
        if moved > Bridge.DOOR_MOVED_M then
            log("open_gates: vehicle %s moved %.2f m while the door opened", vehicleName(env, w.vehicle), moved)
            return "FAILED", "DOOR_MOVED_VEHICLE"
        end
    end
    if task.elapsed < Bridge.DOOR_OPEN_MS + Bridge.DOOR_SETTLE_MS then return nil end
    for _, w in ipairs(task.watched) do
        local ok, still = pcall(stopped, w.vehicle)
        if not ok then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
        if not still then return "FAILED", "DOOR_MOVED_VEHICLE" end
    end
    return "DONE", "GATES_OPENED"
end

function aborts.open_gates() end

-- ------------------------------------------------------------------ block
-- Mark a box of ground GIANTS' navigation must route around, the way GIANTS' own
-- placeables mark theirs (PlaceableAI:setObstacleActive 312-324: addObstacle(node,
-- offset, size, 0, isPassable=false)), on a free node (createTransformGroup + link to
-- the root + setTranslation, TreePlantManager 58-60, PlayerMover 139-143); the map is
-- refreshed with aiSystem:setAreaDirty (PlaceableFence 717-718). Only ever takes
-- ground away from GIANTS' planner; nothing it already avoids becomes passable.
Bridge.MAX_BLOCKS, Bridge.MAX_BLOCK_SIZE_M = 32, 20
Bridge.blocks = {}

function starters.block(env, spec)
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if not (finite(spec.x) and finite(spec.z) and finite(spec.area)) or spec.area <= 0
            or spec.area > Bridge.MAX_BLOCK_SIZE_M then
        return nil, "INVALID_AREA"
    end
    if #Bridge.blocks >= Bridge.MAX_BLOCKS then return nil, "TOO_MANY_BLOCKS" end
    local ok = pcall(function()
        local y = env.getTerrainHeightAtWorldPos(env.g_currentMission.terrainRootNode, spec.x, 0, spec.z)
        local node = env.createTransformGroup("farmhandBlockedArea")
        env.link(env.getRootNode(), node)
        env.setTranslation(node, spec.x, y, spec.z)
        local size = spec.area
        env.g_currentMission.aiSystem:addObstacle(node, 0, 2, 0, size, 4, size, 0, false)
        env.g_currentMission.aiSystem:setAreaDirty(spec.x - size, spec.x + size, spec.z - size, spec.z + size)
        table.insert(Bridge.blocks, node)
    end)
    if not ok then return nil, "BLOCK_ERROR" end
    log("blocked %.0f m around (%.1f, %.1f) for GIANTS' navigation", spec.area, spec.x, spec.z)
    return {}
end

function steppers.block() return "DONE", "BLOCKED_AREA" end
function aborts.block() end

-- -------------------------------------------------------------------- pay
-- Farmhand's pay, the player's choice (the user, 2026-10-30): GIANTS' helper wage is off for
-- Farmhand's work and Farmhand charges its own instead, through the
-- wage money type GIANTS charges its helpers with (AIJob:updateCost: addMoney(-cost, farm,
-- MoneyType.AI, true), AIJob.lua 111-126). Modes (the `pay` op): 0 none, 1 per hour (amount per
-- in-game working hour), 2 per job (`charge` after each completed request), 3 per day (once
-- each in-game day Farmhand works), 4 the game's own wages (GIANTS', Farmhand charging
-- nothing). Until the companion sends it: per hour at GIANTS' own helper rate
-- (AIJob:getPricePerMs 0.0004 per ms = 1440 per hour, times EconomyManager.getCostMultiplier).
Bridge.PAY = { HOURLY = 1, PER_JOB = 2, DAILY = 3, GAME = 4, giantsPerHour = 1440 }
Bridge.pay = { mode = 1, amount = nil, pending = 0 }

local function costMultiplier(env)
    local ok, m = pcall(function() return env.EconomyManager.getCostMultiplier() end)
    return ok and finite(m) and m or 1
end

-- A GIANTS helper job Farmhand starts: no GIANTS wage while Farmhand's pay applies.
function Bridge.farmhandJob(job)
    if job ~= nil and Bridge.pay.mode ~= Bridge.PAY.GAME then
        job.getPricePerMs = function() return 0 end
    end
    return job
end

function starters.pay(env, spec)
    local mode = spec.direction
    if not (finite(mode) and mode >= 0 and mode <= 4 and mode % 1 == 0) or not finite(spec.area) or spec.area < 0 then
        return nil, "INVALID_PAY"
    end
    Bridge.pay.mode, Bridge.pay.amount = mode, spec.area > 0 and spec.area or nil
    log("pay: mode %d amount %s", mode, tostring(Bridge.pay.amount))
    return {}
end
function steppers.pay() return "DONE", "PAY_SET" end
function aborts.pay() end

local function charge(env, farmId, amount, why)
    if not (finite(amount) and amount > 0) then return end
    env.g_currentMission:addMoney(-amount, farmId, env.MoneyType.AI, true)
    log("pay: %.0f charged (%s)", amount, why)
end

function starters.charge(env, spec)
    if Bridge.pay.mode ~= Bridge.PAY.PER_JOB then return {} end
    if not (finite(spec.area) and spec.area >= 0) then return nil, "INVALID_PAY" end
    charge(env, spec.farmId, spec.area, "per job")
    return {}
end
function steppers.charge() return "DONE", "CHARGED" end
function aborts.charge() end

-- Each frame Farmhand works (a command running, or a helper it handed a job to): per hour,
-- the time worked; per day, once a day.
function Bridge.payTick(env, dt, working, farmId)
    local P, pay = Bridge.PAY, Bridge.pay
    if not working then return end
    if pay.mode == P.HOURLY then
        local perHour = pay.amount or P.giantsPerHour * costMultiplier(env)
        pay.pending = pay.pending + perHour * dt / 3600000
        if pay.pending > 25 then charge(env, farmId, pay.pending, "per hour"); pay.pending = 0 end
    elseif pay.mode == P.DAILY then
        local ok, day = pcall(function() return env.g_currentMission.environment.currentDay end)
        if ok and finite(day) and pay.paidDay ~= day and pay.amount ~= nil then
            pay.paidDay = day
            charge(env, farmId, pay.amount, "per day")
        end
    end
end

-- ------------------------------------------------------------------- save
-- The game's own save of the running career: g_currentMission:saveSavegame(true), as
-- the installed FS25 mod EasyDevControls saves (EasyDevControls.lua 2462-2473; GIANTS
-- SavegameController 888 -> FSBaseMission:saveSavegame 3006 in the game's own stack).
-- DONE SAVED only once GIANTS' own FSBaseMission:saveSavegame has run to its end
-- after the request: a function appended to it counts completed saves (the pattern
-- Store Deliveries uses, storeDeliveries.lua 120); live, missionInfo's save date only
-- carries the day, so it is no evidence. Else UNVERIFIED.
Bridge.SAVE_TIMEOUT_MS = 60000
Bridge.savesDone = 0

function Bridge.watchSaves(env)
    if Bridge.savesWatched then return end
    env.FSBaseMission.saveSavegame = env.Utils.appendedFunction(env.FSBaseMission.saveSavegame,
        function() Bridge.savesDone = Bridge.savesDone + 1 end)
    Bridge.savesWatched = true
end

function starters.save(env, spec)
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if not Bridge.savesWatched then return nil, "SAVE_NOT_WATCHED" end
    local before = Bridge.savesDone
    local ok = pcall(function() env.g_currentMission:saveSavegame(true) end)
    if not ok then return nil, "SAVE_ERROR" end
    log("save requested")
    return { before = before }
end

function steppers.save(_env, task)
    if Bridge.savesDone > task.before then
        log("save done")
        return "DONE", "SAVED"
    end
    if task.elapsed > Bridge.SAVE_TIMEOUT_MS then return "UNVERIFIED", "SAVE_NOT_OBSERVED" end
    return nil
end

function aborts.save() end

-- --------------------------------------------------------------- contract
-- A field contract as the contracts menu takes and collects it. direction 1: the
-- available (CREATED) contract on `field`, MissionManager:startMission(mission, farmId,
-- false) (445-488: the farm's own equipment, no leased vehicles), DONE once GIANTS shows
-- it RUNNING (AbstractMission:getIsRunning 796; PREPARING first while it readies the
-- field), else FAILED with its MissionStartState name. direction -1: this farm's contract
-- on `field` that GIANTS finished with MissionFinishState.SUCCESS (AbstractMission:finish
-- 476-490) is collected with MissionManager:dismissMission (507-515: dismiss pays the
-- reward to the farm, then deletes it); the companion checks the money arrived.
Bridge.CONTRACT_TIMEOUT_MS = 60000

local function contractOn(env, field, farmId, mine)
    for _, m in ipairs(Bridge.Observe.missionList(env)) do
        if m.id == field and ((mine and m.farmId == farmId) or (not mine and m.status == "CREATED")) then return m end
    end
    return nil
end

function starters.contract(env, spec)
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if not (finite(spec.field) and spec.field % 1 == 0) then return nil, "INVALID_FIELD" end
    if spec.direction == 1 then
        local m = contractOn(env, spec.field, spec.farmId, false)
        if m == nil then return nil, "NO_CONTRACT_ON_FIELD" end
        local ok, state = pcall(function() return env.g_missionManager:startMission(m.mission, spec.farmId, false) end)
        if not ok then return nil, "CONTRACT_ERROR" end
        if state ~= env.MissionStartState.OK then
            for name, value in pairs(env.MissionStartState) do
                if value == state then return nil, "CONTRACT_" .. name end
            end
            return nil, "CONTRACT_NOT_STARTED"
        end
        log("contract on field %d (%s) started", spec.field, m.kind)
        return { mission = m.mission, start = true }
    elseif spec.direction == -1 then
        local m = contractOn(env, spec.field, spec.farmId, true)
        if m == nil then return nil, "NO_CONTRACT_ON_FIELD" end
        local finished = m.status == "FINISHED" and m.mission.finishState == env.MissionFinishState.SUCCESS
        if not finished then return nil, "CONTRACT_NOT_FINISHED" end
        local ok = pcall(function() env.g_missionManager:dismissMission(m.mission) end)
        if not ok then return nil, "CONTRACT_ERROR" end
        log("contract on field %d (%s) collected", spec.field, m.kind)
        return { mission = m.mission, start = false }
    end
    return nil, "INVALID_DIRECTION"
end

function steppers.contract(_env, task)
    if not task.start then return "DONE", "CONTRACT_COLLECTED" end
    local ok, running = pcall(function() return task.mission:getIsRunning() end)
    if not ok then running = nil end
    if running == true then return "DONE", "CONTRACT_RUNNING" end
    if task.elapsed > Bridge.CONTRACT_TIMEOUT_MS then return "UNVERIFIED", "CONTRACT_NOT_RUNNING" end
    return nil
end

function aborts.contract() end

-- -------------------------------------------------------------------- buy
-- A store purchase through GIANTS' own BuyVehicleData (75/608): new, setStoreItem,
-- setConfigurations (the item's defaultConfigurationIds, StoreManager 1018),
-- setOwnerFarmId, then buy(storeSpawnPlaces, usedStorePlaces, ...) (201-212), which
-- spawns at the store's spawn place (and where Store Deliveries moved it: it hooks
-- this very call). Price economyManager:getBuyPrice (127), funds the farm's balance
-- (getBalance, AIJob 175-177), slots slotSystem:hasEnoughSlots (VehicleSystem 571).
-- BuyVehicleEvent is undocumented, so the charge is checked, not assumed: once the
-- new vehicle is seen, the balance is read again; if GIANTS has not charged the
-- price, addMoney(-price, farm, MoneyType.SHOP_VEHICLE_BUY) charges it once.
-- DONE BOUGHT only with a new vehicle of the item's name owned by the farm.
Bridge.BUY_TIMEOUT_MS = 30000

local function ownedVehicles(env, farmId)
    local out = {}
    local vehicles = env.g_currentMission.vehicleSystem and env.g_currentMission.vehicleSystem.vehicles
        or env.g_currentMission.vehicles
    for _, v in pairs(vehicles or {}) do
        if v.ownerFarmId == farmId then out[v] = true end
    end
    return out
end

local function balance(env, farmId)
    local ok, money = pcall(function() return env.g_farmManager:getFarmById(farmId):getBalance() end)
    return ok and finite(money) and money or nil
end

function starters.buy(env, spec)
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local ok, task, reason = pcall(function()
        local item = env.g_storeManager:getItems()[spec.item]
        if item == nil or item.id ~= spec.item or env.StoreItemUtil.getIsVehicle(item) ~= true then
            return nil, "UNKNOWN_STORE_ITEM"
        end
        local configs = {}
        for name, id in pairs(item.defaultConfigurationIds or {}) do configs[name] = id end
        -- The buyer's hitch option (attacherJoint configuration, 1-based); 0 keeps the default.
        if finite(spec.hitch) and spec.hitch > 0 then
            local options = item.configurations and item.configurations.attacherJoint
            if type(options) ~= "table" or options[spec.hitch] == nil then return nil, "UNKNOWN_CONFIGURATION" end
            configs.attacherJoint = spec.hitch
        end
        -- The buyer's engine (motor configuration, 1-based; StoreItemUtil reads
        -- storeItem.configurations["motor"]); 0 keeps the default. Priced below with it.
        if finite(spec.engine) and spec.engine > 0 then
            local options = item.configurations and item.configurations.motor
            if type(options) ~= "table" or options[spec.engine] == nil then return nil, "UNKNOWN_CONFIGURATION" end
            configs.motor = spec.engine
        end
        -- The buyer's other options, by their position in Observe.itemOptions (the catalog's
        -- order): a configuration choice, or a configuration set's configurations.
        local list = (Bridge.Observe and Bridge.Observe.itemOptions) and Bridge.Observe.itemOptions(item) or {}
        for _, key in ipairs({ "opt1", "opt2", "opt3", "opt4" }) do
            local n = spec[key]
            if finite(n) and n > 0 then
                local o = list[n]
                if o == nil then return nil, "UNKNOWN_CONFIGURATION" end
                if o.kind == "config" then
                    configs[o.type] = o.index
                else
                    for name, index in pairs(o.configurations) do configs[name] = index end
                end
            end
        end
        local price = env.g_currentMission.economyManager:getBuyPrice(item, configs, nil)
        local before = balance(env, spec.farmId)
        if not finite(price) or price <= 0 then return nil, "PRICE_UNKNOWN" end
        if before == nil then return nil, "BALANCE_UNKNOWN" end
        if before < price then return nil, "NOT_ENOUGH_MONEY" end
        local slots = env.g_currentMission.slotSystem
        if slots ~= nil and slots:hasEnoughSlots(item) ~= true then return nil, "NO_SLOTS" end
        local data = env.BuyVehicleData.new()
        data:setStoreItem(item)
        data:setConfigurations(configs)
        data:setOwnerFarmId(spec.farmId)
        data:setPrice(price)
        local t = { item = item, price = price, before = before, known = ownedVehicles(env, spec.farmId) }
        log("buy %s (%s) for %.0f: balance %.0f", tostring(item.name), tostring(item.xmlFilename), price, before)
        data:buy(env.g_currentMission.storeSpawnPlaces, env.g_currentMission.usedStorePlaces,
            function(_, vehicles, state) t.loaded, t.state = vehicles, state end, nil, nil)
        return t
    end)
    if not ok then log("buy failed: %s", tostring(task)); return nil, "BUY_ERROR" end
    return task, reason
end

function steppers.buy(env, task)
    if task.vehicle == nil then
        for v in pairs(ownedVehicles(env, task.spec.farmId)) do
            if not task.known[v] and v.configFileName ~= nil and task.item.xmlFilename ~= nil
                    and v.configFileName:lower() == task.item.xmlFilename:lower() then
                task.vehicle = v
            end
        end
        if task.vehicle == nil then
            if task.elapsed > Bridge.BUY_TIMEOUT_MS then return "UNVERIFIED", "BUY_NOT_OBSERVED" end
            return nil
        end
    end
    -- Charged by GIANTS already? Otherwise once, here.
    local now = balance(env, task.spec.farmId)
    if now == nil then return "UNVERIFIED", "BALANCE_UNKNOWN" end
    if task.before - now < task.price * 0.99 then
        env.g_currentMission:addMoney(-task.price, task.spec.farmId, env.MoneyType.SHOP_VEHICLE_BUY, true)
        log("buy: charged %.0f (balance was %.0f, now %.0f)", task.price, task.before, now)
    end
    log("buy: vehicle %s spawned", tostring(env.NetworkUtil.getObjectId(task.vehicle)))
    return "DONE", "BOUGHT"
end

function aborts.buy() end         -- a purchase is not undone

-- ------------------------------------------------------------------- land
-- The farmland parcel a field lies on, bought (direction 1) or sold (-1) through GIANTS' own
-- request, as its farmland screen and FarmlandManager:consoleCommandBuyFarmland /
-- consoleCommandSellFarmland (572-607, 626-659) send it: g_client:getServerConnection():
-- sendEvent(FarmlandStateEvent.new(farmlandId, farmId, price)), here at the parcel's own price
-- (Farmland.price), never the console's 0. Refused unchanged when already owned (buy), not
-- this farm's (sell), not for sale or the balance short. DONE once FarmlandManager:
-- getFarmlandOwner (407-414) shows the change; the companion verifies the money.
Bridge.LAND_TIMEOUT_MS = 15000

local function fieldLand(env, n)
    for _, f in ipairs(env.g_fieldManager.fields or {}) do
        local id = (type(f.getId) == "function" and f:getId()) or f.fieldId
        if id == n then return f.farmland end
    end
    return nil
end

function starters.land(env, spec)
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    if not (finite(spec.field) and spec.field % 1 == 0) then return nil, "INVALID_FIELD" end
    local manager = env.g_farmlandManager
    local land = fieldLand(env, spec.field)
    if land == nil or not finite(land.id) then return nil, "FIELD_NOT_FOUND" end
    local owner = manager:getFarmlandOwner(land.id)
    local price = land.price
    if not finite(price) or price < 0 then return nil, "LAND_PRICE_UNKNOWN" end
    local target
    if spec.direction == 1 then
        if owner == spec.farmId then return nil, "LAND_ALREADY_OWNED" end
        if owner ~= env.FarmlandManager.NO_OWNER_FARM_ID then return nil, "LAND_NOT_FOR_SALE" end
        local money = balance(env, spec.farmId)
        if money == nil then return nil, "BALANCE_UNKNOWN" end
        if money < price then return nil, "NOT_ENOUGH_MONEY" end
        target = spec.farmId
    elseif spec.direction == -1 then
        if owner ~= spec.farmId then return nil, "LAND_NOT_OWNED" end
        target = env.FarmlandManager.NO_OWNER_FARM_ID
    else
        return nil, "INVALID_DIRECTION"
    end
    env.g_client:getServerConnection():sendEvent(env.FarmlandStateEvent.new(land.id, target, price))
    log("land: farmland %d (field %d) %s for %.0f", land.id, spec.field, spec.direction == 1 and "bought" or "sold", price)
    return { farmland = land.id, target = target, buying = spec.direction == 1 }
end

function steppers.land(env, task)
    local owner = env.g_farmlandManager:getFarmlandOwner(task.farmland)
    if owner == task.target then return "DONE", task.buying and "LAND_BOUGHT" or "LAND_SOLD" end
    if task.elapsed > Bridge.LAND_TIMEOUT_MS then return "UNVERIFIED", "LAND_NOT_CHANGED" end
    return nil
end

function aborts.land() end

-- ------------------------------------------------------------------- stop
-- spec.target: the running task on this tractor, if any; it is aborted first.
function starters.stop(env, spec)
    if spec.target ~= nil then Bridge.abort(env, spec.target, "STOPPED") end
    -- A handed-off job of this vehicle: GIANTS' helper ended as the player's stop does.
    local handed = spec.tractor ~= nil and Bridge.handed[spec.tractor] or nil
    if handed == "fieldwork" and spec.tractor:getIsAIActive() == true then
        Bridge.Hitch.cancelStaging(env, spec.tractor)
    end
    if spec.tractor ~= nil then
        Bridge.handed[spec.tractor] = nil
    end
    return { handed = handed }
end

function steppers.stop(env, task)
    local spec, v = task.spec, task.spec.tractor
    local target = spec.target
    if Bridge.lastAlign ~= nil and Bridge.lastAlign.tractor == v then
        Bridge.Hitch.alignStep(env, Bridge.lastAlign, 0)       -- latched: brake + cruise off
    end
    if Bridge.lastManeuver ~= nil and Bridge.lastManeuver.tractor == v then
        Bridge.Hitch.abort(env, Bridge.lastManeuver, "STOPPED")  -- latched: brake + cruise off
    end
    if Bridge.lastPull ~= nil and Bridge.lastPull.tractor == v then
        Bridge.Hitch.abort(env, Bridge.lastPull, "STOPPED")      -- latched: brake + cruise off
    end
    if Bridge.lastUnpark ~= nil and Bridge.lastUnpark.tractor == v then
        Bridge.Hitch.abort(env, Bridge.lastUnpark, "STOPPED")    -- latched: brake + cruise off
    end
    if Bridge.lastDrive ~= nil and Bridge.lastDrive.tractor == v then
        Bridge.Drive.abort(env, Bridge.lastDrive, Bridge.Hitch)
    end
    if Bridge.lastLoad ~= nil and Bridge.lastLoad.tractor == v then
        Bridge.Hitch.abort(env, Bridge.lastLoad, "STOPPED")      -- latched: brake + cruise off
    end
    local ownMotion = Bridge.pendingGoto[v] ~= nil
        or (target ~= nil and target.op == "farm_drive" and not stopped(v))
        or (task.handed == "fieldwork" and v:getIsAIActive() ~= false)
        or (target ~= nil
            and (target.op == "stage" or target.op == "navigate" or target.op == "fieldwork" or target.op == "follow"
                 or target.op == "deliver")
            and target.seen and v:getIsAIActive() ~= false)
    if not ownMotion and stopped(v) then
        Bridge.Hitch.disarm(v)                    -- the job's motion is over: stop watching
        return "DONE", "STOPPED"
    end
    if task.elapsed > Bridge.STOP_TIMEOUT_MS then return "FAILED", "NOT_STOPPED" end
    return nil
end

function aborts.stop() end

-- --------------------------------------------------------------- runner

Bridge.NEEDS_WAGON = { maneuver = true, align = true, attach = true, detach = true, pull_clear = true, load = true,
                       overload = true, alongside = true, backoff = true, escort = true, reattach = true, mount = true }
-- Ops that run beside the others (each keeps its own state on its task): one per machine, many
-- machines at once. Every other op runs alone in the active slot, as before.
Bridge.CONCURRENT = { escort = true, stop = true }
Bridge.NO_VEHICLE = { buy = true, save = true, block = true, open_gates = true, contract = true,
                      land = true, pay = true, charge = true }   -- ops that act on no vehicle

function Bridge.start(env, spec)
    local starter = starters[spec.op]
    if starter == nil then return nil, "UNKNOWN_OP" end
    if spec.op ~= "pull_clear" then Bridge.lastDetach = nil end   -- "straight after" only
    local ok, task, code, status = pcall(starter, env, spec)
    if not ok then return nil, "UNKNOWN_OBSERVATION", "UNVERIFIED" end
    if task == nil then return nil, code, status or "FAILED" end
    task.op, task.spec, task.elapsed = spec.op, spec, 0
    return task
end

-- Every op's vehicle's peak travel speed (root travel over each second), logged at its end:
-- whether a provider kept Farmhand's TRAVEL_MAX_KMH (GIANTS' helpers have no limit Farmhand sets).
local function samplePeak(env, task)
    local v = task.spec and task.spec.tractor
    if v == nil or task.elapsed < (task.peakAt or 0) then return end
    task.peakAt = task.elapsed + 1000
    local ok, x, y, z = pcall(env.getWorldTranslation, v.rootNode)
    if not (ok and finite(x) and finite(z)) then return end
    if task.peakFrom ~= nil and task.elapsed > task.peakFrom.t then
        local d = math.sqrt((x - task.peakFrom.x) ^ 2 + ((y or 0) - task.peakFrom.y) ^ 2 + (z - task.peakFrom.z) ^ 2)
        local kmh = d / (task.elapsed - task.peakFrom.t) * 3600
        if kmh > (task.peakKmh or 0) then task.peakKmh = kmh end
    end
    task.peakFrom = { x = x, y = y or 0, z = z, t = task.elapsed }
end

function Bridge.step(env, task, dt)
    if task.status ~= nil then return task.status, task.code end
    task.elapsed = task.elapsed + dt
    pcall(samplePeak, env, task)
    local ok, status, code = pcall(steppers[task.op], env, task, dt)
    if not ok then status, code = "UNVERIFIED", "UNKNOWN_OBSERVATION" end
    if status ~= nil then
        if status ~= "DONE" then Bridge.abort(env, task, code) end
        task.status, task.code = status, code
        finish(task)
    end
    return status, code
end

-- Stop the Farmhand-owned motion of a task (idempotent; never detaches).
function Bridge.abort(env, task, code)
    if task.aborted then return end
    task.aborted = true
    pcall(aborts[task.op], env, task)
    if task.status == nil then task.status, task.code = "FAILED", code or "STOPPED" end
    finish(task)
end

-- ----------------------------------------------------------- companion link

local function token(s)
    if type(s) ~= "string" or #s < 1 or #s > 64 or s:find("[^%w_%.%-]") then return nil end
    return s
end

-- A failed write means the companion went away (FH Transport.lua:96-105).
function Bridge:writeLine(line)
    if self.pipe == nil then return end
    local ok = pcall(function() self.pipe:write(line .. "\n"); self.pipe:flush() end)
    if not ok then self:closePipe() end
end

function Bridge:send(frame)
    self:writeLine(self.Observe.encode(frame))
end

function Bridge:closePipe()
    if self.pipe ~= nil then pcall(function() self.pipe:close() end) end
    self.pipe = nil
end

function Bridge:openPipe()
    local ok, handle = pcall(self.env.io.open, self.Observe.PIPE_PATH, "w")
    if not ok or handle == nil then return end
    self.pipe = handle
    self:writeLine(self.Observe.hello(self.env))
    local catalog = self.Observe.catalogFrame and self.Observe.catalogFrame()
    for _, line in ipairs(catalog or {}) do self:writeLine(line) end
end

function Bridge:result(session, id, op, status, code)
    log("bridge command id=%s op=%s status=%s code=%s", tostring(id), tostring(op), status, tostring(code))
    -- Only a well-formed (session, id) can be matched by the companion.
    if token(session) == nil or not finite(id) or id % 1 ~= 0 or id < 1 or id > 2147483647 then return end
    local O = self.Observe
    self:send({ event = "result", protocol = O.PROTOCOL, session = session, id = id,
                op = token(op) or O.NULL, status = status, code = token(code) or "UNKNOWN" })
end

-- A pre-empted attach or detach may still complete in GIANTS: its outcome is unknown.
local function preempted(task)
    return (task.op == "attach" or task.op == "detach") and "UNVERIFIED" or "FAILED"
end

function Bridge:companionOwner()
    return self.session ~= nil and Bridge.owner == "companion:" .. self.session
end

-- Drop the companion: stop whatever it started, release ownership.
function Bridge:dropSession(code)
    for _, entry in ipairs(self:entries()) do
        Bridge.abort(self.env, entry.task, code)
        self:result(self.session, entry.id, entry.task.op, preempted(entry.task), code)
        for _, joined in ipairs(entry.joined or {}) do
            self:result(self.session, joined, "stop", "FAILED", code)
        end
    end
    self.active, self.side = nil, {}
    if self:companionOwner() then Bridge.owner = nil end
    -- What spans several of its ops ends with the session: the takeover watch (a later session's
    -- first job would end DRIVER_TOOK_OVER on a drive the player made meanwhile) and combines
    -- kept powered for an overload that will never come.
    for v in pairs(Bridge.Hitch.armed or {}) do Bridge.Hitch.disarm(v) end
    for v in pairs(Bridge.powered) do pcall(Bridge.unpower, v) end
    Bridge.lastAlign, Bridge.lastManeuver, Bridge.lastDetach, Bridge.lastPull = nil, nil, nil, nil
    Bridge.lastUnpark, Bridge.lastLoad, Bridge.lastDrive = nil, nil, nil
    log("bridge session %s dropped: %s", tostring(self.session), code)
    self.dropped[self.session] = true      -- a dropped session is never re-adopted
    self.session = nil
end

function Bridge:checkSession()
    local env = self.env
    local id, beat, notices = nil, nil, {}
    pcall(function()
        local xml = env.XMLFile.loadIfExists("fhAc001Session", self.dir .. Bridge.SESSION_FILE)
        if xml == nil then return end
        id, beat = token(xml:getString("session#id")), xml:getString("session#beat")
        -- "id:TOKEN id:TOKEN": the companion's latest notices, oldest first.
        for n, code in tostring(xml:getString("session#notices") or ""):gmatch("(%d+):([%w_%.%-]+)") do
            if token(code) ~= nil and #notices < 8 then notices[#notices + 1] = { id = tonumber(n), code = code } end
        end
        xml:delete()
    end)
    local newest = 0
    for _, n in ipairs(notices) do newest = math.max(newest, n.id) end
    if id ~= nil and id ~= self.session and not self.dropped[id] then
        if self.session ~= nil then self:dropSession("SESSION_CHANGED") end
        if Bridge.owner == nil then
            self.session, self.lastId, self.beat, self.beatAt = id, 0, beat, self.clock
            self.noticeId = newest            -- only notices issued from now on are shown
            Bridge.owner = "companion:" .. id
            log("bridge session %s adopted", id)
            self:closePipe()
            self:openPipe()
            self:notify(Bridge.NOTICES.CONNECTED, false)
        end
    elseif id ~= nil and id == self.session then
        if beat ~= self.beat then self.beat, self.beatAt = beat, self.clock end
        for _, n in ipairs(notices) do
            if n.id > self.noticeId then
                self.noticeId = n.id
                if n.code == "MENU" then self:openMenu() else self:notify(Bridge.noticeText(n.code)) end
            end
        end
    end
    if self.session ~= nil and self.clock - self.beatAt > Bridge.COMPANION_TIMEOUT_MS then
        self:dropSession("COMPANION_LOST")
    end
end

local ATTRS = { tractor = "tractor", wagon = "wagon", x = "x", z = "z", yaw = "yaw",
                tractorJoint = "tj", wagonJoint = "wj", maxDistanceM = "dist", field = "field", area = "area", seed = "seed", hand = "hand", item = "item", hitch = "hitch", engine = "eng", opt1 = "o1", opt2 = "o2", opt3 = "o3", opt4 = "o4",
                direction = "fd",
                -- 1 only when the player's opt-in scenery setting is on (protocol.ipc GHOST_OPS)
                ghost = "gh" }

function Bridge:pollCommand()
    local env, path = self.env, self.dir .. Bridge.COMMAND_FILE
    local cmd = nil
    pcall(function()
        local xml = env.XMLFile.loadIfExists("fhAc001Command", path)
        if xml == nil then return end
        cmd = { session = xml:getString("command#session"), id = tonumber(xml:getString("command#id")),
                op = xml:getString("command#op"), via = xml:getString("command#via") }
        for key, attr in pairs(ATTRS) do cmd[key] = tonumber(xml:getString("command#" .. attr)) end
        xml:delete()
    end)
    if cmd == nil then return nil end
    pcall(env.deleteFile, path)     -- the file disappearing is receipt, never success
    return cmd
end

local function machines(spec)
    local out = {}
    for _, v in ipairs({ spec.tractor, spec.wagon }) do
        if v ~= nil then out[v.rootVehicle or v] = true end
    end
    return out
end

local function shares(a, b)
    for v in pairs(a) do if b[v] then return true end end
    return false
end

function Bridge:handle(cmd)
    local env = self.env
    if cmd.session ~= self.session or self.session == nil then
        return self:result(cmd.session, cmd.id, cmd.op, "REJECTED", "STALE_SESSION")
    end
    if not finite(cmd.id) or cmd.id % 1 ~= 0 or cmd.id <= self.lastId then
        log("bridge ignored replayed or invalid command id %s", tostring(cmd.id))
        return
    end
    self.lastId = cmd.id
    if starters[cmd.op] == nil then return self:result(cmd.session, cmd.id, cmd.op, "REJECTED", "UNKNOWN_OP") end
    local function object(id)
        local ok, v = pcall(env.NetworkUtil.getObject, id)
        return ok and v or nil
    end
    local spec = { op = cmd.op, farmId = env.g_currentMission:getFarmId(), tractor = object(cmd.tractor) }
    for key in pairs(ATTRS) do
        if key ~= "tractor" and key ~= "wagon" then spec[key] = cmd[key] end
    end
    if cmd.op == "farm_drive" then spec.via = cmd.via end
    if Bridge.NEEDS_WAGON[cmd.op] then spec.wagon = object(cmd.wagon) end
    if (spec.tractor == nil and not Bridge.NO_VEHICLE[cmd.op]) or (Bridge.NEEDS_WAGON[cmd.op] and spec.wagon == nil) then
        return self:result(cmd.session, cmd.id, cmd.op, "REJECTED", "UNKNOWN_VEHICLE")
    end
    local owner = "companion:" .. self.session
    spec.owned = function() return Bridge.owner == owner end
    -- Only a stop may pre-empt what runs on the same machines (an op's tractor and wagon, each
    -- with the rig it hangs on: overload runs on the combine, into the rig's trailer, so the stop
    -- the companion sends for that rig's tractor ends it). Other machines' ops run on.
    local mine, hit = machines(spec), {}
    for _, entry in ipairs(self:entries()) do
        if shares(machines(entry.task.spec), mine) then hit[#hit + 1] = entry end
    end
    if #hit > 0 then
        if cmd.op ~= "stop" then return self:result(cmd.session, cmd.id, cmd.op, "REJECTED", "BUSY") end
        -- A stop while a stop runs joins it: both get its one observed outcome, so
        -- a second stop can never turn the first one's confirmation into FAILED.
        for _, entry in ipairs(hit) do
            if entry.task.op == "stop" then
                self:result(cmd.session, cmd.id, cmd.op, "ACCEPTED", "ACCEPTED")
                entry.joined = entry.joined or {}
                table.insert(entry.joined, cmd.id)
                return
            end
        end
        spec.target = hit[1].task                        -- aborted by the stop's start
        for i, entry in ipairs(hit) do
            if i > 1 then Bridge.abort(env, entry.task, "STOPPED") end
            self:result(self.session, entry.id, entry.task.op, preempted(entry.task), "STOPPED")
            self:remove(entry)
        end
    end
    local concurrent = Bridge.CONCURRENT[cmd.op] == true
    if not concurrent and self.active ~= nil then return self:result(cmd.session, cmd.id, cmd.op, "REJECTED", "BUSY") end
    self:result(cmd.session, cmd.id, cmd.op, "ACCEPTED", "ACCEPTED")
    local task, code, status = Bridge.start(env, spec)
    if task == nil then return self:result(cmd.session, cmd.id, cmd.op, status, code) end
    local entry = { id = cmd.id, task = task }
    self.side = self.side or {}
    if self.active == nil and cmd.op ~= "escort" then self.active = entry else self.side[cmd.id] = entry end
end

-- What runs now: the active op and those beside it.
function Bridge:entries()
    local out = {}
    if self.active ~= nil then out[1] = self.active end
    for _, entry in pairs(self.side or {}) do out[#out + 1] = entry end
    return out
end

function Bridge:remove(entry)
    if self.active == entry then self.active = nil end
    if self.side ~= nil and self.side[entry.id] == entry then self.side[entry.id] = nil end
end

function Bridge:busy() return self.active ~= nil or next(self.side or {}) ~= nil end

-- One step of a running op; true once it ended (its result and its joined stops' sent).
function Bridge:stepEntry(entry, dt)
    local status, code = Bridge.step(self.env, entry.task, dt)
    if status == nil then return false end
    self:result(self.session, entry.id, entry.task.op, status, code)
    for _, joined in ipairs(entry.joined or {}) do
        self:result(self.session, joined, "stop", status, code)
    end
    return true
end

function Bridge:update(dt)
    if self.env == nil then return end
    self.clock = self.clock + dt
    self.screenPollMs = (self.screenPollMs or 0) + dt
    if self.screenPollMs >= 1000 then
        self.screenPollMs = 0
        if self.dir ~= nil and type(self.env.fileExists) == "function" then
            local reload = self.dir .. ".fhReload"
            if self.env.fileExists(reload) then
                if not self.reloadMarkerSeen and not self:busy() and self.env.FarmhandProbe ~= nil then
                    self.reloadMarkerSeen = true
                    self.env.deleteFile(reload)
                    log("%s", self.env.FarmhandProbe:consoleReload())
                end
            else
                self.reloadMarkerSeen = nil
            end
            local marker = self.dir .. ".fhScreen"
            if self.env.fileExists(marker) then
                if type(self.env.saveScreenshot) == "function" then
                    local ok, saved = pcall(self.env.saveScreenshot, self.dir .. ".fhScreen.png")
                    if not ok or not saved then log("background screenshot unavailable") end
                end
                self.env.deleteFile(marker)
            end
        end
    end
    Bridge.watchGoto(self.env, dt)
    if Bridge.lastDetach ~= nil then Bridge.lastDetach.age = Bridge.lastDetach.age + dt end
    if self.pipe == nil and self.session ~= nil then
        self.pipeTimer = self.pipeTimer + dt
        if self.pipeTimer >= Bridge.PIPE_RETRY_MS then self.pipeTimer = 0; self:openPipe() end
    end
    self.sessionTimer = self.sessionTimer + dt
    if self.sessionTimer >= Bridge.SESSION_CHECK_MS then self.sessionTimer = 0; self:checkSession() end
    local cmd = self:pollCommand()
    if cmd ~= nil then self:handle(cmd) end
    if self.active ~= nil and self:stepEntry(self.active, dt) then self.active = nil end
    for id, entry in pairs(self.side or {}) do
        if self:stepEntry(entry, dt) then self.side[id] = nil end
    end
    pcall(Bridge.payTick, self.env, dt, self:busy() or next(Bridge.handed) ~= nil,
          self.env.g_currentMission:getFarmId())
    -- Rigs ghosted past buildings (Hitch.ghost) that no motion drives any more: their
    -- collision with buildings restored once each stands clear of them.
    self.ghostTimer = (self.ghostTimer or 0) + dt
    if self.ghostTimer >= 1000 and next(Bridge.Hitch.ghosted or {}) ~= nil then
        self.ghostTimer = 0
        local running = self:entries()
        pcall(Bridge.Hitch.releaseGhosts, self.env, function(tractor)
            for _, entry in ipairs(running) do
                if entry.task ~= nil and entry.task.spec ~= nil and entry.task.spec.tractor == tractor then return true end
            end
            return false
        end)
    end
    self.snapshotTimer = self.snapshotTimer + dt
    self.Observe.sinceSnapshotMs = self.snapshotTimer   -- Observe.atRest: the sample's real age
    if self.snapshotTimer >= Bridge.SNAPSHOT_MS and self.pipe ~= nil then
        local elapsed = self.snapshotTimer
        self.snapshotTimer = 0
        self.Observe.sinceSnapshotMs = 0
        self.seq = self.seq + 1
        local line = self.Observe.snapshot(self.env, self.seq, self.session, elapsed)
        if line ~= nil then self:writeLine(line) end
        local catalog = self.Observe.tickCatalog and self.Observe.tickCatalog(self.env)
        for _, page in ipairs(catalog or {}) do self:writeLine(page) end
    end
end

-- ------------------------------------------------------- player requests

Bridge.NOTICES = {
    PLACED = "Farmhand: the machine could not reach the implement, so it was teleported in front of it (teleport recovery)",
    ROUTE_RECORDING = "Farmhand: recording your route - drive it, then say \"stop recording and save\"",
    ROUTE_ALREADY_RECORDING = "Farmhand: a route is already being recorded",
    ROUTE_PAUSED = "Farmhand: route recording paused",
    ROUTE_RESUMED = "Farmhand: route recording resumed",
    ROUTE_DISCARDED = "Farmhand: the recording was discarded",
    ROUTE_NOT_RECORDING = "Farmhand: no route is being recorded",
    ROUTE_SAVED = "Farmhand: route saved (recorded; it is validated once a machine drives it to the end)",
    ROUTE_SAVED_CLEAN = "Farmhand: route saved - a clean drive, no unplanned stops",
    ROUTE_TOO_SHORT = "Farmhand: the recording is too short to keep (10 m at least)",
    ROUTE_NOT_SAVED = "Farmhand: the route could not be saved",
    ROUTE_RECORDING_GOES_ON = "Farmhand: nothing could follow you, but your route is still being recorded",
    ROUTE_RENAMED = "Farmhand: route renamed",
    ROUTE_DELETED = "Farmhand: route deleted",
    ROUTE_NOT_FOUND = "Farmhand: no saved route has that name",
    ROUTES_NOT_LINKED = "Farmhand: routes are kept per save - load a save first",
    CONNECTED = "Farmhand: companion connected",
    TELEPORTED = "Farmhand: the rig could not move, so it was teleported to open ground nearby",
    GHOSTED = "Farmhand: passing through scenery beside the rig (your setting); its collision returns once clear",
    HELPER_WAITS_FOR_PLAYER = "Farmhand: the helper has stopped for you - step out of its way and it carries on",
    ACCEPTED = "Farmhand: fetching a tractor and the implement",
    BUSY = "Farmhand: already working on a request",
    QUEUED = "Farmhand: busy; your request is next in line",
    ACCEPTED_JOIN_FIELDS = "Farmhand: joining the fields; plowing the strip between them with field creation on",
    COMPLETE_JOIN_FIELDS = "Farmhand: the fields are joined",
    COMPLETE = "Farmhand: tractor and implement delivered",
    ACCEPTED_MOVE = "Farmhand: on my way",
    ACCEPTED_HITCH = "Farmhand: hitching a tractor to the implement",
    ACCEPTED_MOUNT = "Farmhand: loading the header onto its trailer",
    COMPLETE_MOUNT = "Farmhand: the header is locked on its trailer",
    COMPLETE_MOUNT_NOT_CLEAR = "Farmhand: the header is locked on its trailer; the machine could not back off it",
    COMPLETE_HITCH = "Farmhand: tractor and implement hitched",
    ACCEPTED_DETACH = "Farmhand: unhitching the implement",
    COMPLETE_DETACH = "Farmhand: implement unhitched",
    ACCEPTED_FIELD_WORK = "Farmhand: on it - working the field",
    ACCEPTED_BUY = "Farmhand: going shopping",
    ACCEPTED_SAVE = "Farmhand: saving the game",
    COMPLETE_SAVE = "Farmhand: game saved",
    COMPLETE_BUY = "Farmhand: bought it; it is waiting at the store",
    COMPLETE_FIELD_WORK = "Farmhand: field work done and checked on the field",
    COMPLETE_DETACH_NOT_CLEAR = "Farmhand: implement unhitched; the tractor could not pull clear of it, so drive it away before the next job",
    UNSUPPORTED_TRACTOR_ONLY = "Farmhand: moving a tractor on its own is not supported yet; ask for a tractor with an implement, such as the forage wagon",
    UNSUPPORTED_NAMED_VEHICLE = "Farmhand: choosing a vehicle by its name is not supported yet; name the kind of implement, such as the forage wagon or the cultivator",
    COMPLETE_MOVE = "Farmhand: arrived",
    ACCEPTED_SEQUENCE = "Farmhand: on it, one step at a time",
    ACCEPTED_HAUL = "Farmhand: taking the tractor there on the trailer",
    ACCEPTED_FOLLOW = "Farmhand: following you (FollowMe); stop it with FollowMe or Farmhand: stop",
    COMPLETE_FOLLOW = "Farmhand: stopped following",
    PAUSED ="Farmhand: stopped; say continue to pick up where I left off",
    RESUMING = "Farmhand: picking up where I left off",
    NOTHING_TO_RESUME = "Farmhand: there is nothing to continue",
    RETARGETED = "Farmhand: new destination; carrying on",
    COMPLETE_HAUL = "Farmhand: delivered and unloaded",
    ACCEPTED_CONTRACT = "Farmhand: taking on a contract",
    ACCEPTED_LAND = "Farmhand: dealing in that land",
    WORKER_STARTED = "Farmhand: a helper is working the field; it carries on while I do the next thing",
    WORKERS_STARTED = "Farmhand: the helpers are working; ready for the next job",
    WORKER_DONE = "Farmhand: a helper finished its field (checked on the field); bringing the equipment back",
    WORKER_STOPPED = "Farmhand: a helper stopped before its field was done; bringing the equipment back",
    ACCEPTED_RETURN = "Farmhand: bringing a helper's equipment back",
    COMPLETE_RETURN = "Farmhand: equipment back and unhitched",
    STATUS_HELPERS_WORKING = "Farmhand: free; helpers are working the fields",
    ACCEPTED_UNLOAD_COMBINE = "Farmhand: emptying the combine into a trailer",
    COMPLETE_UNLOAD_COMBINE = "Farmhand: the combine is emptied into the trailer",
    ACCEPTED_GRAIN_CART = "Farmhand: a grain cart is going to the combine to unload it while it harvests",
    COMPLETE_GRAIN_CART = "Farmhand: the grain cart is done with the combine",
    GRAIN_CART_FULL = "Farmhand: the grain cart is full; say sell the grain or store the grain",
    COMBINE_UNLOADED = "Farmhand: combine emptied into the trailer",
    COMBINE_FULL = "Farmhand: the combine is full; emptying it into a trailer, then harvesting on",
    COMBINE_FULL_NO_TRAILER = "Farmhand: the combine is full and no trailer has room; say sell the grain or store the grain, then continue",
    WAGON_FULL = "Farmhand: the forage wagon is full; delivering the load, then collecting on",
    WORKER_BLOCKED = "Farmhand: a worker is blocked; moving what stands in its way",
    COMPLETE_CLEAR_WORKER = "Farmhand: the worker's way is clear",
    WAGON_FULL_NO_DESTINATION = "Farmhand: the forage wagon is full; say sell it or store it, then continue",
    ACCEPTED_STORE = "Farmhand: taking the load to the farm's storage",
    COMPLETE_STORE = "Farmhand: stored; the load is in the farm's storage",
    HARVEST_POLICY_SELL = "Farmhand: from now on harvested crop is sold at the best paid point",
    HARVEST_POLICY_STORE = "Farmhand: from now on harvested crop goes into the farm's storage",
    HARVEST_POLICY_KEEP = "Farmhand: from now on harvested crop stays in the trailer until you say where",
    POLICY_UNAVAILABLE = "Farmhand: settings are unavailable",
    POLICY_NOT_SAVED = "Farmhand: that setting could not be saved",
    NOTHING_TO_HITCH_TOGETHER = "Farmhand: say which tractor and implement to hitch (or buy them in the same sentence)",
    ACCEPTED_SELL = "Farmhand: taking the load to the best paid sell point",
    COMPLETE_SELL = "Farmhand: sold; the load is gone and the money is in",
    COMPLETE_CONTRACT = "Farmhand: contract done and the reward collected",
    COMPLETE_LAND = "Farmhand: done - the land changed hands and the money moved",
    COMPLETE_SEQUENCE = "Farmhand: all done and checked",
    ACCEPTED_LOAD = "Farmhand: loading the tractor onto the trailer",
    COMPLETE_LOAD = "Farmhand: tractor loaded, belted and the ramps up",
    ACCEPTED_UNLOAD = "Farmhand: unloading the tractor",
    COMPLETE_UNLOAD = "Farmhand: tractor unloaded and the ramps up",
    CANCELLED = "Farmhand: stopped",
    STOPPING = "Farmhand: stopping",
    NOTHING_TO_STOP = "Farmhand: nothing to stop",
    NOT_UNDERSTOOD = "Farmhand: sorry, I did not understand that",
    NOT_LINKED = "Farmhand: the companion is not running",
    NO_TEXT_INPUT = "Farmhand: the text field is unavailable",
    STATUS_IDLE = "Farmhand: idle",
    STATUS_PAUSED = "Farmhand: a job is paused; say continue to pick it up",
    LISTENING = "Farmhand: listening...",
    NOT_HEARD = "Farmhand: sorry, I did not hear that",
    VOICE_UNAVAILABLE = "Farmhand: voice is unavailable (no microphone, or Left Alt + N to install speech); type with Left Alt + K",
    VOICE_INSTALLING = "Farmhand: downloading speech recognition...",
    VOICE_READY = "Farmhand: speech recognition installed; hold Left Alt + V to speak",
    AI_NONE_INSTALLED = "Farmhand: no AI model found; install Ollama or llama.cpp and a compatible model",
    AI_VALIDATING = "Farmhand: checking the AI model...",
    AI_BUSY = "Farmhand: still setting up the last AI choice; wait for it, or pick Continue without AI",
    AI_READY = "Farmhand: AI model ready",
    AI_OFF = "Farmhand: AI off; plain commands only",
    AI_UNAVAILABLE = "Farmhand: AI settings are unavailable",
    REPORT_SAVED = "Farmhand: report saved on your Desktop (Farmhand-report-....zip)",
}
-- Critical (red) notices that are not failures of a job.
Bridge.CRITICAL = {
    AI_INCOMPATIBLE = "Farmhand: that AI model is not compatible; choose another",
    AI_MODEL_MISSING = "Farmhand: the chosen AI model is not available; press Left Alt + N to choose",
    AI_UNREACHABLE = "Farmhand: the AI runtime (Ollama) is not answering; plain commands still work",
    VOICE_INSTALL_FAILED = "Farmhand: the speech recognition download failed; typing still works",
    REPORT_FAILED = "Farmhand: the report could not be saved; see companion.log",
}
-- What each executor state means to the player (STATUS_WORKING_<state>).
Bridge.STEPS = {
    FIND = "looking for a tractor and the implement",
    RESERVE = "reserving the equipment",
    TRAVEL_NEAR = "driving to the implement",
    STAGE = "driving to the implement",
    LOCAL_MANEUVER = "lining up with the implement",
    ALIGN_REVERSE = "reversing to the implement",
    ATTACH = "hitching the implement",
    DETACH = "unhitching the implement",
    PULL_CLEAR = "pulling clear of the unhitched implement",
    WAIT_PERSON = "waiting for you to step out of the tractor's way",
    UNPARK = "driving out of the building",
    RAMPS_DOWN = "lowering the ramps",
    APPROACH = "lining up behind the ramps",
    LOAD = "driving up onto the trailer",
    BELTS = "fastening the belts",
    UNBELT = "releasing the belts",
    UNLOAD = "backing off the trailer",
    FOLLOW = "following you",
    RAMPS_UP = "raising the ramps",
    FIELD_WORK = "working the field (GIANTS helper)",
    CONTRACT = "taking the contract",
    WAIT_AHEAD = "waiting for the helper ahead to get going",
    SELL = "driving the load to the sell point",
    STORE = "driving the load to the farm's storage",
    OVERLOAD = "emptying the combine into the trailer",
    ALONGSIDE = "pulling the trailer under the combine's pipe",
    ESCORT = "keeping the grain cart under the combine's pipe while it harvests",
    WAIT_COMBINE = "waiting for the combine to reach the field",
    PIPE_OUT = "swinging the combine's pipe out",
    PIPE_IN = "folding the combine's pipe",
    PASS = "pulling on past the combine",
    COLLECT = "collecting the contract reward",
    VERIFY = "checking the hitch",
    DELIVER = "driving to the destination",
    STOP = "stopping",
    RELEASE = "finishing",
}
Bridge.OUTCOMES = { COMPLETE = "delivered", CANCELLED = "stopped", BLOCKED = "not started",
                    FAILED = "failed", UNVERIFIED = "unclear; check the tractor" }
-- Readable texts for the most common reasons; any other code is shown as is.
Bridge.REASONS = {
    NO_ELIGIBLE_PAIR = "no available tractor and implement",
    NO_HITCH_GEOMETRY = "no tractor that can hitch the implement",
    RESERVATION_CONFLICT = "the equipment is in use",
    INVALID_REQUESTER_POSITION = "your position is unknown",
    DELIVERY_NOT_OBSERVED = "it did not arrive; check the tractor",
    NAVIGATE_STALLED = "the tractor got stuck (parked in a shed or boxed in?)",
    CLEANUP_NOT_CONFIRMED = "the tractor may still be moving; check it",
    NO_HITCHED_PAIR = "no hitched tractor and implement to move",
    NO_ATTACHED_IMPLEMENT = "no hitched implement to unhitch",
    NO_TRANSPORT = "no transport trailer with ramps and belts",
    LAND_ALREADY_OWNED = "the farm already owns that land",
    LAND_NOT_FOR_SALE = "that land is not for sale",
    LAND_NOT_OWNED = "the farm does not own that land",
    TRANSPORT_LOADED = "the transport you named already carries something",
    TRANSPORT_TOO_SMALL = "the tractor and what it carries do not fit the transport you named",
    NO_FREE_TRANSPORT = "every transport already carries something or is too small for this load",
    NO_TRACTOR_TO_LOAD = "no free tractor to load",
    NO_LOADED_TRACTOR = "no tractor on a trailer to unload",
    RAMPS_NOT_OBSERVED = "the ramps did not move; check the trailer",
    LOAD_RAMPS_NOT_DOWN = "the trailer's ramps are not down",
    LOAD_NOT_LINED_UP = "the tractor is not lined up behind the ramps",
    LOAD_NOT_BEHIND_RAMPS = "the tractor is not right behind the ramps",
    LOAD_APPROACH_FAILED = "the tractor could not be lined up behind the ramps",
    LOAD_DOES_NOT_FIT = "the tractor is longer than the deck",
    LOAD_PATH_BLOCKED = "something is in the way on the ramps or the deck",
    LOAD_TRACTOR_NOT_ALONE = "unhitch the tractor's implement first",
    TRANSPORT_MOVED = "the truck or trailer moved; stopped",
    BELTS_NOT_HOLDING = "the belts did not hold the tractor; check it",
    LOADED_NO_DRIVER = "nothing here can drive a trailer with a belted vehicle on it there; drive the truck there yourself, then say continue",
    OVERSPEED = "the tractor went faster than Farmhand's 3 km/h limit, so it stopped; check it before going on",
    ENVELOPE_CONTACT = "something came too close to the tractor or what it carries, so it stopped",
    NO_STORAGE = "the farm has no storage that takes this crop",
    NOT_ON_DECK = "the tractor is not on the deck; check it",
    DETACH_NOT_ALLOWED = "the game does not allow unhitching it right now",
    NOT_ATTACHED = "that implement is not hitched",
    DETACH_TIMEOUT = "unhitching did not finish; check the tractor",
    DESTINATION_NOT_FOUND = "I do not know that place",
    DESTINATION_AMBIGUOUS = "more than one place has that name",
    NOT_LINKED = "the game is not linked to the companion",
    AI_NOT_CLEARED = "an AI job still controls the tractor; check it",
    CLEANUP_AI_NOT_CLEARED = "an AI job still controls the tractor; check it",
    DRIVER_TOOK_OVER = "you took the controls, so Farmhand stopped",
    STAGE_NOT_OBSERVED = "the tractor did not line up with the implement",
    JOINTS_NOT_OBSERVED = "the hitch points could not be read",
    LOCAL_PATH_HITS_WAGON = "there is no room to line up with the implement from here",
    LOCAL_PATH_LEAVES_AREA = "there is no room to line up with the implement from here",
    LOCAL_PATH_TOO_LONG = "there is no room to line up with the implement from here",
    LOCAL_START_AT_WAGON = "the tractor is too close beside the implement to line up",
    OUTSIDE_LOCAL_AREA = "the tractor stopped too far from the implement to hitch it",
    TURN_RADIUS_UNKNOWN = "the tractor's turning circle is unknown",
    TURN_RADIUS_OUT_OF_RANGE = "the tractor turns too wide to line up with the implement",
    KEEP_OUT = "the tractor came too close to the implement while lining up",
    ENVELOPE_EXCEEDED = "the tractor left the area around the implement while lining up",
    LINEUP_MISSED = "the tractor could not line up with the implement",
    LOCAL_PATH_BLOCKED = "another vehicle is in the way of lining up with the implement",
    LOCAL_PATH_OBSTRUCTED = "a building or object is in the way of lining up with the implement",
    LOCAL_PATH_PERSON = "stand clear of the tractor and the implement, then ask again",
    PERSON_TOO_CLOSE = "you came too close to the tractor, so Farmhand stopped",
    OTHER_VEHICLE_MOVED = "another vehicle moved near the tractor, so Farmhand stopped",
    OTHER_VEHICLE_NEAR = "another vehicle came too close to the tractor, so Farmhand stopped",
    LOCAL_AREA_UNKNOWN = "the area around the implement could not be checked",
    TRAVEL_ENDED_TOO_FAR = "the tractor stopped too far from the implement to hitch it",
    NO_SAFE_APPROACH = "the helper finds no way to the implement (parked in a shed or boxed in?); move them into the open and ask again",
    TRAPPED = "the tractor is stuck and cannot drive on; check it",
    ALIGN_WRONG_SIDE = "the tractor stopped on the wrong side of the implement to hitch it",
    ALIGN_HEADING_OUT_OF_RANGE = "the tractor stopped facing the wrong way to hitch the implement",
    ALIGN_LATERAL_OUT_OF_RANGE = "the tractor stopped too far to the side of the implement to hitch it",
    STALLED = "the tractor got stuck at the implement",
    DIVERGED = "the tractor could not follow its line to the implement",
    WAGON_MOVED = "the implement moved",
    AGAINST_IMPLEMENT = "the tractor stands against an unhitched implement; drive it clear first",
    PERSON_IN_THE_WAY = "you are standing in the tractor's way; step aside and ask again",
    IMPLEMENT_MOVED = "the unhitched implement moved, so Farmhand stopped",
    SEPARATION_LOST = "the tractor did not pull away from the implement, so Farmhand stopped",
    NOT_JUST_DETACHED = "the tractor can only pull clear straight after unhitching",
    DISTANCE_LIMIT = "the tractor did not get clear of the implement within its limit",
    NAVIGATE_BLOCKED_AT_START = "the helper cannot start from where it is parked",
    STAGE_BLOCKED_AT_START = "the helper cannot start from where it is parked",
    UNPARK_EXIT_BLOCKED = "it is boxed in: something blocks the straight way out of the building",
    UNPARK_TOO_TIGHT = "it is parked too tightly in the building to drive out safely",
    UNPARK_EXIT_TOO_FAR = "the way out of the building is longer than Farmhand may drive on its own",
    UNPARK_BLOCKED = "another vehicle stands in the way out of the building",
    UNPARK_PERSON = "stand clear of the way out of the building, then ask again",
    UNPARK_START_NOT_CLEAR = "something touches it where it is parked (a wall, cargo or a raised floor)",
    UNPARK_REVERSE_NOT_ALLOWED = "it would have to reverse out with something attached, which Farmhand does not do",
    UNPARK_RIG_NOT_STRAIGHT = "what it pulls is not straight behind it, so Farmhand will not drive it out",
    UNPARK_GEOMETRY_UNKNOWN = "its size could not be read, so Farmhand will not drive it out",
    UNPARK_ARTICULATED_UNSUPPORTED = "Farmhand does not drive articulated vehicles out of buildings",
    UNPARK_QUERY_UNVERIFIED = "the game's collision check could not be confirmed, so Farmhand did not move it",
    UNPARK_QUERY_UNKNOWN = "the way out could not be checked, so Farmhand did not move it",
    DOOR_MOVED_VEHICLE = "a vehicle moved while the door opened, so Farmhand stopped; check it",
    DOOR_NEAR_MOVING_VEHICLE = "a vehicle near the door is moving, so Farmhand did not open it",
    DOOR_AREA_UNKNOWN = "the vehicles near the door could not be checked, so Farmhand did not open it",
    UNPARK_DOOR_CLOSED = "a closed door or wall of the building is in the way; open it and ask again",
    UNPARK_HANDOFF_BLOCKED = "it drove out of the building, but the helper still cannot start from there",
    UNPARK_ALREADY_USED = "it was already driven out once in this job and the helper still cannot start",
    OBSTACLE_AHEAD = "something appeared in the way, so Farmhand stopped",
    EXIT_NOT_CONFIRMED = "the way out was no longer clear at the end, so Farmhand stopped",
    RIG_CHANGED = "what it pulls moved or came off, so Farmhand stopped",
    STUCK_ON_OBSTACLE = "the tractor or what it pulls is caught on something the helper cannot see (a fence?); free it and ask again",
    BLOCKED_BY_PERSON = "you are standing in the tractor's way; step aside and ask again",
    NO_OWNED_FIELD = "the farm owns no field",
    SAVE_NOT_OBSERVED = "the save did not finish; save from the menu",
    SAVE_ERROR = "the game refused to save",
    CATALOG_NOT_READY = "the store list is still loading; ask again in a minute",
    NOT_IN_STORE = "the store does not sell that",
    NO_PLOW_FOR_OUR_TRACTORS = "no plow in the store that our tractors can pull",
    NOT_ENOUGH_MONEY = "the farm cannot afford it",
    OVER_SPENDING_LIMIT = "it costs more than your spending limit (Farmhand settings)",
    NO_SLOTS = "the farm has no free vehicle slots",
    UNKNOWN_STORE_ITEM = "that store item is gone",
    BUY_ERROR = "the store refused the purchase",
    BUY_NOT_OBSERVED = "the purchase did not appear; check the store",
    PURCHASE_NOT_OBSERVED = "the purchase did not appear; check the store",
    PAYMENT_NOT_OBSERVED = "the money was not seen moving; check the finances",
    FIELD_NOT_OWNED = "the farm does not own that field",
    NO_CONTRACT = "there is no contract to take right now",
    NO_CONTRACT_ON_FIELD = "there is no contract for that field",
    CONTRACT_NOT_SUPPORTED = "Farmhand does plow, cultivate and fertilize contracts; none of those is offered",
    CONTRACT_LIMIT_REACHED = "the farm already runs as many contracts as the game allows",
    CONTRACT_PENDING_MISSION = "another contract is still being prepared; ask again in a moment",
    CONTRACT_NOT_AVAILABLE_ANYMORE = "that contract is no longer offered",
    CONTRACT_CANNOT_BE_STARTED_NOW = "the game cannot start that contract right now",
    CONTRACT_NOT_RUNNING = "the game did not start the contract; check the contracts menu",
    CONTRACT_FIELD_NOT_OBSERVED = "the contract started but its field did not appear; check the contracts menu",
    CONTRACT_NOT_FINISHED = "the work is done but the game has not finished the contract yet; say continue in a while",
    CONTRACT_LOST = "the contract is gone; check the contracts menu",
    NO_SEEDER_FOR_CROP = "no seeder on the farm can sow that crop",
    NO_SEEDER = "no seeder is hitched",
    SEED_NOT_SUPPORTED = "the seeder cannot sow that crop",
    SEED_NOT_SET = "the seeder did not take that crop",
    CROP_NOT_OBSERVED = "the field does not show that crop afterwards; check it",
    CROP_NOT_READY = "the crop on that field is not ready to harvest",
    NO_HARVESTER_WITH_HEADER = "no free combine with its header on",
    SWATH_NOT_SET = "the combine did not switch to laying a straw swath",
    NO_RIG_TO_SELL = "no tractor with a loaded trailer",
    NO_COMBINE_HARVESTING = "no helper is harvesting that field",
    SAY_WHICH_FIELD = "helpers are harvesting several fields; say which",
    NO_FULL_COMBINE = "no standing combine with grain in it",
    NO_TRAILER_FOR_THE_GRAIN = "no free tractor with a trailer that takes that grain",
    NOT_UNDER_THE_PIPE = "the trailer could not be put under the combine's pipe",
    GRAIN_NOT_MOVED = "the grain did not go into the trailer; check it",
    PIPE_NOT_OBSERVED = "the combine's pipe did not come out",
    NO_PIPE = "that combine has no pipe",
    PIPE_NOT_OUT = "the combine's pipe is not out",
    PIPE_NOT_ALLOWED = "the combine's pipe cannot move now",
    PIPE_TIMEOUT = "the combine's pipe did not finish moving",
    NOT_OVERLOADING = "nothing came out of the pipe into the trailer",
    DELIVER_UNAVAILABLE = "GIANTS' deliver helper cannot drive that rig",
    DELIVER_INVALID = "GIANTS' deliver helper refused the trip",
    DELIVER_NOT_STARTED = "GIANTS' deliver helper did not start",
    -- PAYMENT_NOT_OBSERVED (above) also ends a purchase and a land deal: one wording for all.
    ALONGSIDE_NOT_LINED_UP = "the trailer did not end up on the pipe's line",
    ALONGSIDE_PATH_BLOCKED = "something stands in the way beside the combine",
    ALONGSIDE_BLOCKED = "another vehicle is too close to the way beside the combine",
    ALONGSIDE_RIG_NOT_STRAIGHT = "the trailer is not straight behind the tractor",
    COMBINE_MOVED = "the combine moved",
    NOTHING_TO_HITCH_TOGETHER = "say which tractor and implement to hitch (or buy them in the same sentence)",
    NOTHING_TO_SELL = "the trailer is empty",
    NO_SELL_POINT = "no sell point buys that",
    FIELD_CROP_UNKNOWN = "the crop on that field could not be read",
    CARGO_NOT_UNLOADED = "the load did not come off at the sell point; check the trailer",
    NO_FIELD_FOUND = "the helper found no field there",
    FIELD_NOT_READY = "the field needs nothing from this implement",
    PARTIAL_COVERAGE = "the helper stopped before the whole field was done",
    OUT_OF_FILL = "the implement ran empty",
    OUT_OF_MONEY = "the farm ran out of money",
    WRONG_SEASON = "it is the wrong season for that crop",
    NEEDS_SEED = "the seeder is empty and the helper does not buy seed; fill it first",
    NEEDS_FERTILIZER = "the sprayer is empty and the helper does not buy fertilizer; fill it first",
    MATERIAL_UNKNOWN = "the implement's fill level could not be read",
    MATERIAL_USE_NOT_OBSERVED = "the field changed but no material use was seen; check it",
    FIELD_NOT_OBSERVED = "the field's state could not be read",
    FIELDWORK_UNAVAILABLE = "the helper cannot work with this tractor and implement",
    FIELDWORK_STALLED = "the helper got stuck on the field",
    FIELDWORK_BLOCKED_AT_START = "the helper cannot start from where it stands",
    FIELDWORK_BLOCKED = "something stands in the helper's way",
    NO_PLOW_CULTIVATING = "the farm has no plow",
    CROP_READY_TO_HARVEST = "the crop on that field is ready to harvest; harvest it first",
    CROP_GROWING = "a crop is growing on that field; soil work would destroy it",
    NO_IMPLEMENT_FOR_PLOW = "the farm has no plow or cultivator",
    NO_IMPLEMENT_FOR_SOW = "the farm has no seeder",
    NO_IMPLEMENT_FOR_CULTIVATE = "the farm has no cultivator or plow",
    NO_IMPLEMENT_FOR_HARVEST = "the farm has no combine with a header",
    NO_IMPLEMENT_FOR_FERTILIZE = "the farm has no sprayer",
    NO_IMPLEMENT_FOR_COLLECT = "the farm has no forage wagon GIANTS' helper can use",
    NO_WINDROW_ON_FIELD = "no straw or grass windrow lies on that field",
    WINDROW_UNKNOWN = "the windrows on that field cannot be measured",
    -- Every other failure the companion or the mod reports, in the player's words (completing
    -- "could not start - ", "stopped short - " or "unsure, check it - "); tests/test_lua_bridge.py
    -- ReasonWordingTests keeps every emitted code worded.
    AI_ACTIVE = "a helper is driving it",
    ALIGN_NOT_OBSERVED = "the tractor was not seen lined up with the implement",
    ALONGSIDE_ARTICULATED_UNSUPPORTED = "an articulated tractor cannot be lined up beside the combine yet",
    ALONGSIDE_GEOMETRY_UNKNOWN = "the rig's size could not be read",
    ALONGSIDE_NOT_ONE_TRAILER = "the tractor must tow exactly one trailer to go beside the combine",
    ALONGSIDE_NOT_UNDER = "the trailer did not end up under the combine's pipe",
    ALONGSIDE_PAST_THE_PIPE = "the trailer is already past the combine's pipe",
    ALONGSIDE_PERSON = "someone on foot is too close",
    ALONGSIDE_QUERY_UNKNOWN = "the way beside the combine could not be checked",
    ALONGSIDE_QUERY_UNVERIFIED = "the way beside the combine could not be checked reliably",
    ALONGSIDE_TOO_FAR = "the trailer is too far behind the combine's pipe",
    COMBINE_NOT_HARVESTING = "the combine's helper is not harvesting",
    COMBINE_NOT_AT_THE_FIELD = "the combine did not get to the field",
    NO_HEADER_FOR_THE_CROP = "no header on the farm cuts that contract's crop",
    REATTACH_NOT_IN_RANGE = "the machine did not stand close enough to attach it again",
    NO_MATCHING_JOINT = "the machine has no free joint that takes it",
    EQUIPMENT_UNAVAILABLE = "the machine or the implement is busy, moving or not this farm's",
    ATTACH_NOT_OBSERVED = "GIANTS was asked to attach it, but it was not seen attached",
    FIELD_NOT_REACHABLE = "GIANTS finds no route to that field for this machine",
    MOVING_AWAY = "it was driving away from where it was going, so it stopped",
    COMBINE_NOT_OWNED = "the combine is not this farm's",
    COMBINE_STOPPED = "the combine's helper stopped",
    ESCORT_AHEAD_OF_COMBINE = "the grain cart is ahead of the combine",
    ESCORT_GEOMETRY_UNKNOWN = "the combine's size could not be read",
    ESCORT_NOT_ONE_TRAILER = "the grain cart tractor does not tow exactly one trailer",
    ESCORT_QUERY_UNKNOWN = "the collision check could not run",
    ESCORT_TOO_FAR = "the grain cart is too far from the combine",
    NO_SAFE_SLOT = "there is no clear spot beside the combine under its pipe",
    TRAILER_FULL = "the trailer is full",
    TRAILER_WRONG_CROP = "the trailer holds or takes another crop",
    ARRIVAL_NOT_OBSERVED = "the rig was not seen arriving at the destination",
    ATTACHED = "it is hitched to something",
    ATTACHMENT_MISMATCH = "the equipment is not hitched as planned; check it",
    ATTACH_ABSENT = "coupling was never requested",
    ATTACH_MISMATCH = "a different implement or joint was coupled; check it",
    ATTACH_REJECTED = "the game does not allow coupling here",
    ATTACH_TIMEOUT = "coupling did not finish",
    BACKOFF_ARTICULATED_UNSUPPORTED = "an articulated tractor cannot back off the transport yet",
    BACKOFF_BLOCKED = "another vehicle is too close behind",
    BACKOFF_GEOMETRY_UNKNOWN = "the rig's size could not be read",
    BACKOFF_NOT_ALONE = "the tractor must be on its own to back off",
    BACKOFF_NOT_AT_THE_TRANSPORT = "the tractor is not right behind the transport's ramps",
    BACKOFF_PATH_BLOCKED = "something stands in the way behind",
    BACKOFF_PERSON = "someone on foot is too close",
    BACKOFF_QUERY_UNKNOWN = "the way back could not be checked",
    BACKOFF_QUERY_UNVERIFIED = "the way back could not be checked reliably",
    BALANCE_UNKNOWN = "the farm's money could not be read",
    BELTS_NOT_OBSERVED = "the straps were not seen changing",
    BELTS_NOT_RELEASED = "the straps did not come off the load",
    BLOCK_ERROR = "an area could not be marked off for the game's driver",
    BOUGHT_ITEM_UNSUITABLE = "what was bought does not take that crop; it was not used",
    CANCELLED = "stopped as you asked",
    CARGO_AMBIGUOUS = "more than one load answers to that name; say which",
    CARGO_NOT_BELTED = "the load did not strap on again after the teleport; check it",
    CARGO_NOT_ON_RIG = "that load is not strapped on the rig",
    CARGO_STATE_UNREADABLE = "what the rig carries could not be read",
    COLLISION_UNREADABLE = "the game's collision check could not be read",
    COMBINE_NOT_STOPPED = "the combine is still moving",
    COMPATIBILITY_CHANGED = "the equipment changed and no longer suits the job",
    CONTRACT_ERROR = "the contract could not be taken or collected",
    CONTRACT_NOT_STARTED = "the contract did not start",
    CRUISE_UNREADABLE = "the vehicle's cruise control could not be read",
    DELIVER_ERROR = "the game's deliver helper could not be set up",
    DELIVER_STALLED = "the game's deliver helper stopped moving",
    DESTINATION_INVALID = "that destination cannot be driven to",
    EGRESS_BLOCKED = "there is no straight way off what it stands against",
    EGRESS_DIVERGED = "it could not keep a straight line off what it touched",
    EGRESS_NOT_CLEAR = "there is no clear way off what it stands against",
    EGRESS_OBSTACLE_MOVED = "what it stood against moved",
    EGRESS_UNREADABLE = "what it stands against could not be located",
    EQUIPMENT_CHANGED = "the equipment was changed or taken while Farmhand worked with it",
    EQUIPMENT_MISSING = "the equipment is gone",
    EQUIPMENT_MOVING = "the equipment is still moving; ask again once it stands",
    FIELDS_NOT_ADJACENT = "those fields do not touch, so they cannot be joined",
    FIELDWORK_ERROR = "the game's field helper could not be set up",
    FIELDWORK_INVALID = "the game's field helper refused this field or implement",
    FIELD_CREATION_DISABLED = "field creation is off in the game's settings",
    FIELD_CREATION_NOT_PERMITTED = "the farm may not create fields",
    FIELD_NOT_MAPPED = "that field has no place Farmhand can drive to",
    FIELD_NOT_FOUND = "that field was not found on this map",
    FOLD_NOT_ALLOWED = "the game does not allow folding it now",
    FOLD_TIMEOUT = "folding did not finish",
    FOLLOWME_UNAVAILABLE = "following needs the FollowMe mod, which is not available for that vehicle",
    FOLLOW_NOT_ALLOWED = "FollowMe will not start for that vehicle now",
    FOLLOW_NOT_STARTED = "FollowMe did not start",
    GOTO_ENDED = "the game's driver ended without getting there",
    GOTO_INVALID = "the game's driver refused that destination",
    GOTO_NOT_STARTED = "the game's driver did not start",
    GRAIN_UNACCOUNTED = "some grain did not arrive in the trailer; check both",
    HAUL_PLAN_TOO_LONG = "the haul needed more steps than expected; check the rig",
    HITCH_GEOMETRY_CHANGED = "the implement moved or changed before it could be hitched",
    IMPLEMENT_CHANGED = "the implement changed (lowered, unfolded or unhitched) on the way",
    INVALID_AREA = "that work area is not valid",
    INVALID_DESTINATION = "that destination is not valid",
    INVALID_DIRECTION = "that request was not valid",
    INVALID_FIELD = "that field number is not valid",
    INVALID_GEOMETRY = "the rig's turning room could not be worked out",
    INVALID_INTENT = "that request was not a valid job",
    INVALID_LIMITS = "the motion's limits were not valid",
    INVALID_PAY = "that pay setting is not valid",
    INVALID_PLACE = "that place is not valid",
    INVALID_POSE = "that position is not valid",
    INVALID_SEED = "that crop is not one the seeder sows",
    INVALID_START = "the rig's position could not be read",
    JOINT_TYPE_MISMATCH = "the tractor's hitch does not fit the implement",
    LAND_NOT_CHANGED = "the field's owner did not change; check the land menu",
    LAND_NOT_OBSERVED = "the field's owner was not seen changing; check the land menu",
    LAND_PRICE_UNKNOWN = "the field's price could not be read",
    LANE_NOT_LINED_UP = "the rig could not line up with its lane",
    LEADER_UNSUPPORTED = "FollowMe cannot lead with the vehicle you are in",
    LEARNED_ROUTE_UNVERIFIED = "the taught route did not end where it should",
    LINK_LOST = "the connection to the game was lost",
    LOAD_ARTICULATED_UNSUPPORTED = "an articulated tractor cannot be loaded yet",
    LOAD_BELTS_FASTENED = "the transport's straps are already fastened",
    LOAD_BLOCKED = "another vehicle is too close to the ramps",
    LOAD_GEOMETRY_UNKNOWN = "the rig's size could not be read",
    LOAD_IMPLEMENT_LOWERED = "the tractor's implement is lowered",
    LOAD_IMPLEMENT_NOT_MOUNTED = "only an implement carried on the three-point hitch can go up with the tractor",
    LOAD_IMPLEMENT_UNFOLDED = "the tractor's implement is unfolded",
    LOAD_NO_BELTS = "the transport has no straps",
    LOAD_PERSON = "someone on foot is too close",
    LOAD_QUERY_UNKNOWN = "the way onto the deck could not be checked",
    LOAD_QUERY_UNVERIFIED = "the way onto the deck could not be checked reliably",
    LOAD_TOO_FAR = "the tractor is too far from the ramps",
    LOAD_WITH_PLAN_TOO_LONG = "loading needed more steps than expected; check the rig",
    MAP_UNKNOWN = "the map could not be identified",
    MOTOR_UNAVAILABLE = "the engine cannot run",
    NAVIGATE_START_CONTACT = "the rig touches something where it stands, so the game's driver cannot start",
    NAVIGATE_STUCK = "the game's driver got stuck on the way",
    NAVIGATE_STUCK_AT_START = "the game's driver could not move the rig from where it stands",
    NOT_ACCEPTED = "the game did not take the command",
    NOT_ALIGNED = "the tractor was not lined up for coupling",
    NOT_FOLDABLE = "it has nothing to fold",
    NOT_LINED_UP_AT_RAMPS = "the tractor is not lined up with the ramps",
    NOT_OFF_RAMPS = "the tractor did not come off the ramps",
    NOT_OWNED = "it belongs to another farm",
    NOT_RESERVED = "the equipment was not held for this job",
    NOT_ROOT_VEHICLE = "it is hitched to another vehicle; name that one",
    NOT_STOPPED = "it was still moving",
    NO_ACCEPTANCE = "the game did not answer; check it is running",
    NO_BELTS = "it has no straps",
    NO_CLEAR_APPROACH = "there is no clear way up to that place",
    NO_CLOSED_GATE = "no closed gate or door is near",
    NO_DELIVER_JOB = "the game's deliver helper is not available",
    NO_DISCHARGE_NODE = "the combine's discharge could not be read",
    NO_ENGINE_OPTION = "that model has no engine by that name in the store",
    NO_FARM_SILO = "the farm has no silo that takes it",
    NO_FIELDWORK_JOB = "the game's field helper is not available",
    NO_GOTO_JOB = "the game's driver is not available",
    NO_HITCH_OPTION = "that model has no such hitch in the store",
    NO_OPEN_SPOT = "there is no open ground nearby",
    NO_CARRIER_FOR_IMPLEMENT = "no trailer here takes that implement",
    NOT_A_CARRIER = "that trailer has nothing to lock an implement on",
    NO_IMPLEMENT_FOR_CARRIER = "the machine carries nothing that trailer takes",
    CARRIER_NOT_READY = "the trailer is not ready to take it (fold it out first)",
    MOUNT_NOT_LINED_UP = "the machine is not lined up with the trailer",
    CARRIER_MOVED = "the trailer moved, so loading stopped before pushing it",
    CARRIED_TOO_LOW = "the implement is carried too low to clear the trailer",
    MOUNT_NOT_OBSERVED = "it was set down but the trailer did not lock it",
    PLACE_NOT_CLEAR = "there is no clear ground in front of the machine for it",
    PLACE_UNREADABLE = "the ground in front of the machine could not be checked",
    NOT_LOOSE = "only a loose implement can be set down",
    INVALID_TARGET = "that position is not valid",
    NO_PLOW_HITCHED = "no tractor with a plow hitched is free",
    NO_RIG_TO_FOLLOW = "no free hitched rig is there to follow you",
    NO_ROUTE = "no way there was found for the rig",
    NO_GOAL_PROGRESS = "it kept driving without getting closer to where it was going, so it stopped",
    NO_SUCH_OPTION = "that model has no such option in the store",
    NO_SUCH_TRACTOR = "no tractor by that name",
    NO_UNLOADING_STATE = "the combine's pipe has no unloading position",
    OTHER_CANDIDATE = "the game offered a different implement to couple",
    OVERLOAD_TIMEOUT = "the grain transfer did not finish",
    PIPE_MOVED = "the combine's pipe moved",
    PLAYER_NOT_IN_VEHICLE = "get into the vehicle it should follow first",
    PRICE_UNKNOWN = "the price could not be read",
    PULL_CLEAR_BLOCKED = "another vehicle is too close ahead",
    PULL_CLEAR_NOT_BEHIND = "the implement is not behind the tractor",
    PULL_CLEAR_NOT_OBSERVED = "the tractor was not seen pulling clear",
    PULL_CLEAR_PERSON = "someone on foot is too close",
    RAMPS_MOVED = "the transport's ramps moved",
    REJECTED_BUSY = "the game was busy with another command",
    REJECTED_STALE_SESSION = "the game is linked to another companion session",
    REJECTED_UNKNOWN_OP = "the game's Farmhand mod is older than the companion; update it",
    REJECTED_UNKNOWN_VEHICLE = "the game no longer has that vehicle",
    RELEASE_NOT_OBSERVED = "the equipment was not seen released",
    REQUEST_ID_CONFLICT = "that request clashed with an earlier one; ask again",
    RESERVATION_LOST = "Farmhand lost hold of the equipment (taken over, or the link dropped)",
    RIG_GEOMETRY_UNKNOWN = "the rig's size could not be read",
    RIG_LINK_UNREADABLE = "what is hitched to the rig could not be read",
    RIG_NOT_STOPPED = "the rig did not come to rest",
    ROUTE_LOST = "the rig lost its route",
    SAVE_NOT_WATCHED = "saving cannot be checked in this game",
    SEATED_IN_FOLLOWER = "get out of the vehicle that should follow",
    SEED_ERROR = "the seeder's crop could not be set",
    STAGE_ERROR = "the game's driver could not be set up",
    STAGE_NOT_ROUTED = "the transport did not reach a road with a route there",
    STALE_SNAPSHOT = "the game stopped reporting; check it is running",
    START_NOT_CLEAR = "the rig touches something where it stands",
    STOP_NOT_CONFIRMED = "the stop was not confirmed; make sure the equipment has stopped",
    STOP_NOT_OBSERVED = "the equipment was not seen stopping; check it",
    TAILGATE_NOT_OBSERVED = "the trailer's tailgate was not seen moving",
    TELEPORT_NOT_OBSERVED = "the teleport was not seen",
    TIMEOUT = "it took too long",
    TOO_MANY_BLOCKS = "too many areas are marked off for the game's driver",
    TRACTOR_AMBIGUOUS = "more than one tractor fits; name it",
    TRACTOR_CARRIES_SOMETHING_ELSE = "the tractor already has another implement hitched",
    TRACTOR_JOINT_OCCUPIED = "the tractor's hitch is already in use",
    TRANSPORT_NOT_STOPPED = "the transport is still moving",
    TURNING_RADIUS_UNKNOWN = "the vehicle's turning circle could not be read",
    UNKNOWN_CONFIGURATION = "that store option is not available",
    UNKNOWN_IMPLEMENT = "Farmhand does not know that implement",
    UNKNOWN_JOINT = "the hitch joints could not be read",
    UNKNOWN_OBSERVATION = "the game reported something Farmhand could not read",
    UNKNOWN_OP = "the game's Farmhand mod is older than the companion; update it",
    UNLOAD_NOT_ON_DECK = "the tractor is not on the deck",
    UNPARK_NOT_OBSERVED = "the tractor was not seen leaving its parking place",
    VEHICLE_AMBIGUOUS = "more than one vehicle answers to that name; say which",
    VEHICLE_UNAVAILABLE = "no free vehicle answers to that",
    VERTICAL_MISMATCH = "the hitch points are at different heights",
    WAGON_ATTACHED = "the implement is already hitched to something else",
    WORKER_NOT_FOUND = "that helper is no longer there",
    WORKTOOL_TIMEOUT = "the plow did not finish lowering or raising",
    WORK_WIDTH_UNKNOWN = "the implement's working width could not be read",
}

-- Text and whether it is critical, for a companion notice token.
function Bridge.noticeText(notice)
    local fixed = Bridge.NOTICES[notice]
    if fixed ~= nil then return fixed, false end
    if Bridge.CRITICAL[notice] ~= nil then return Bridge.CRITICAL[notice], true end
    local working = notice:match("^WORKING_([%u_]+)$")
    if working ~= nil then return "Farmhand: " .. (Bridge.STEPS[working] or working), false end
    local step = notice:match("^STATUS_WORKING_([%u_]+)$")
    if step ~= nil then return "Farmhand: working - " .. (Bridge.STEPS[step] or step), false end
    -- A motion step whose equipment is not observed moving: said so, never "driving".
    local waiting = notice:match("^STATUS_WAITING_([%u_]+)$")
    if waiting ~= nil then
        return "Farmhand: " .. (Bridge.STEPS[waiting] or waiting) .. " - not moving yet", false
    end
    local routes = notice:match("^ROUTES_SAVED_(%d+)$")
    if routes ~= nil then return "Farmhand: " .. routes .. " saved route(s) on this save", false end
    local last = notice:match("^STATUS_LAST_(%u+)$")
    if last ~= nil then return "Farmhand: idle; last job " .. (Bridge.OUTCOMES[last] or last), false end
    local status, reason = notice:match("^(%u+)_([%w_]+)$")
    -- What happened, not "not delivered" for every job: nothing started, it stopped
    -- part-way, or the outcome is not certain.
    local lead = { BLOCKED = "could not start", FAILED = "stopped short", UNVERIFIED = "unsure, check it" }
    if lead[status] ~= nil then
        return "Farmhand: " .. lead[status] .. " - " .. (Bridge.REASONS[reason] or Bridge.readable(reason)), true
    end
    return "Farmhand: " .. notice, false
end

-- A code no wording covers yet, as words rather than a raw token ("NO_SUCH_THING": "no such
-- thing"); the game's own contract start states ("CONTRACT_<MissionStartState name>") said so.
function Bridge.readable(code)
    local state = code:match("^CONTRACT_([%w_]+)$")
    if state ~= nil then
        return "the game would not start the contract (" .. (state:lower():gsub("_", " ")) .. ")"
    end
    return (code:lower():gsub("_", " "))
end

-- GIANTS' own call shape (PlayerInputComponent 1150). INGAME_NOTIFICATION_OK is not
-- evidenced for FS25, so a non-critical notice falls back to the proven kind.
function Bridge:notify(text, critical)
    log("%s", text)
    pcall(function()
        local kinds = self.env.FSBaseMission
        local kind = (not critical and kinds.INGAME_NOTIFICATION_OK) or kinds.INGAME_NOTIFICATION_CRITICAL
        self.env.g_currentMission:addIngameNotification(kind, text)
    end)
end

-- A request is only a wish: the companion interprets and validates it, picks the
-- equipment and answers with a notice. Nothing moves here.
function Bridge:sendRequest(field, value, extra)
    if self.session == nil or self.pipe == nil then return self:notify(Bridge.NOTICES.NOT_LINKED, true) end
    self.requestId = self.requestId + 1
    log("request %d (%s) sent to companion %s", self.requestId, field, self.session)
    local frame = { event = "request", protocol = self.Observe.PROTOCOL, session = self.session,
                    id = self.requestId, [field] = value }
    for key, v in pairs(extra or {}) do frame[key] = v end
    self:send(frame)
end

function Bridge:openSettings()
    if self.env == nil then return end
    self:sendRequest("action", "settings")
end

-- The companion's list (menu.xml, numbered attributes) in GIANTS' option dialog,
-- called as Courseplay does (CourseEditor.lua:165-176: callback(item), title,
-- text, texts; item 0 = cancelled). Not over another dialog; the settings key
-- opens it again. The pick goes back with the menu id, so a stale list is ignored.
function Bridge:openMenu()
    local env = self.env
    local menu = nil
    pcall(function()
        local xml = env.XMLFile.loadIfExists("fhMenu", self.dir .. Bridge.MENU_FILE)
        if xml == nil then return end
        local id, count = tonumber(xml:getString("menu#id")), tonumber(xml:getString("menu#count"))
        if finite(id) and finite(count) and count >= 1 and count <= 9 then
            local texts = {}
            for i = 1, count do texts[i] = xml:getString("menu#o" .. i) or "?" end
            menu = { id = id, title = xml:getString("menu#title") or "Farmhand", texts = texts }
        end
        xml:delete()
    end)
    if menu == nil then return end
    local bridge = self
    local ok = pcall(function()
        if env.g_gui ~= nil and env.g_gui:getIsDialogVisible() then
            bridge:notify("Farmhand: close this window, then press Left Alt + N for Farmhand's settings", false)
            return
        end
        env.OptionDialog.show(function(item)
            if type(item) == "number" and item >= 1 and item <= #menu.texts and item % 1 == 0 then
                bridge:sendRequest("menu", menu.id, { choice = item })
            end
        end, menu.title, "", menu.texts)
    end)
    if not ok then self:notify(Bridge.NOTICES.AI_UNAVAILABLE, true) end
end

function Bridge:requestHere()
    if self.env == nil then return end
    if self.lastRequestAt ~= nil and self.clock - self.lastRequestAt < Bridge.REQUEST_COOLDOWN_MS then return end
    self.lastRequestAt = self.clock
    self:sendRequest("action", "fetch_here")
end

-- STOP is a fixed key, never a sentence to type: no cooldown, it is idempotent.
function Bridge:requestStop()
    if self.env == nil then return end
    self:sendRequest("action", "stop")
end

-- Push-to-talk: the companion records while the key is held. An unlinked press
-- notifies once per press, never every frame.
function Bridge:pushToTalk(inputValue)
    if self.env == nil then return end
    local down = type(inputValue) == "number" and inputValue > 0
    if down and not self.talking then
        self.talking = true
        self:sendRequest("action", "talk_start")
    elseif not down and self.talking then
        self.talking = false
        if self.session ~= nil and self.pipe ~= nil then self:sendRequest("action", "talk_end") end
    end
end

-- Words for the companion: control characters become spaces, surrounding space
-- is trimmed, and the text is cut to MAX_TEXT bytes on a UTF-8 boundary. nil if
-- nothing (valid) is left.
function Bridge.cleanText(text, validUtf8)
    if type(text) ~= "string" then return nil end
    text = text:gsub("%c", " "):gsub("^%s+", ""):gsub("%s+$", "")
    text = text:sub(1, Bridge.MAX_TEXT)
    for _ = 1, 3 do
        if validUtf8(text) then break end
        text = text:sub(1, -2)
    end
    if #text == 0 or not validUtf8(text) then return nil end
    return text
end

-- GIANTS' own text dialog, called as Courseplay does (CustomFieldManager.lua:126-130,
-- CpCourseManagerFrame.lua:636-644): callback(target, text, clickOk), target,
-- default text, prompt, title, max characters, confirm button text.
function Bridge:openTextInput()
    local env = self.env
    if env == nil then return end
    if self.session == nil or self.pipe == nil then return self:notify(Bridge.NOTICES.NOT_LINKED, true) end
    local bridge = self
    local ok = pcall(function()
        local okText = env.g_i18n and env.g_i18n:getText("button_ok") or "OK"
        env.TextInputDialog.show(function(_, text, clickOk)
            if clickOk then
                local clean = Bridge.cleanText(text, bridge.Observe.validUtf8)
                if clean ~= nil then bridge:sendRequest("text", clean) end
            end
        end, bridge, "", "Tell Farmhand what to do, for example: bring me a tractor and forage wagon",
            "Farmhand", Bridge.MAX_TEXT, okText)
    end)
    if not ok then self:notify(Bridge.NOTICES.NO_TEXT_INPUT, true) end
end

-- Add the key to the player's global actions, as Courseplay does
-- (Courseplay.lua:142-151); GIANTS calls the hook for the owning player whenever
-- it registers the on-foot actions (PlayerInputComponent 142-151). The original
-- is restored on unload, so a reloaded savegame never stacks two hooks.
Bridge.KEYS = { FARMHAND_FETCH_HERE = "requestHere", FARMHAND_TALK = "openTextInput",
                FARMHAND_STOP = "requestStop", FARMHAND_PUSH_TO_TALK = "pushToTalk",
                FARMHAND_SETTINGS = "openSettings" }
-- Held keys: GIANTS calls them every frame with the input value, as it registers
-- its own CROUCH (PlayerInputComponent: false, false, true, true; onInputCrouch
-- 1101-1110 reads inputValue 0..1); the press and release edges are sent.
Bridge.HOLD = { pushToTalk = true }

function Bridge:installInput(env)
    local component, actions = env.PlayerInputComponent, env.InputAction
    if component == nil or actions == nil or env.Utils == nil then
        log("no player input; the in-game Farmhand keys are unavailable")
        return
    end
    local bridge, keys = self, {}
    for name, method in pairs(Bridge.KEYS) do
        if actions[name] ~= nil then keys[#keys + 1] = { action = actions[name], method = method }
        else log("input action %s missing; that key is unavailable", name) end
    end
    if #keys == 0 then return end
    table.sort(keys, function(a, b) return a.method < b.method end)
    self.inputOriginal = component.registerGlobalPlayerActionEvents
    component.registerGlobalPlayerActionEvents = env.Utils.overwrittenFunction(self.inputOriginal,
        function(inputComponent, superFunc, ...)
            superFunc(inputComponent, ...)
            -- Re-registered (after a vehicle, menu or dialog): a release delivered
            -- to no one must not swallow the next press. The companion ignores a
            -- talk_end with no recording.
            bridge.talking = false
            for _, key in ipairs(keys) do
                if Bridge.HOLD[key.method] then
                    env.g_inputBinding:registerActionEvent(key.action, inputComponent,
                        function(_, _, inputValue) bridge[key.method](bridge, inputValue) end, false, false, true, true)
                else
                    env.g_inputBinding:registerActionEvent(key.action, inputComponent,
                        function() bridge[key.method](bridge) end, false, true, false, true)
                end
            end
        end)
end

function Bridge:removeInput(env)
    if self.inputOriginal ~= nil then
        env.PlayerInputComponent.registerGlobalPlayerActionEvents = self.inputOriginal
        self.inputOriginal = nil
    end
end

-- Server only; folders and stale-command cleanup as Farmhand Companion init().
function Bridge:open(env)
    self.env, self.clock, self.seq, self.lastId = env, 0, 0, 0
    self.requestId, self.noticeId, self.lastRequestAt = 0, 0, nil
    Bridge.pendingGoto = {}
    self.pipeTimer, self.sessionTimer, self.snapshotTimer = 0, 0, 0
    self.session, self.active, self.side, self.pipe, self.dropped = nil, nil, {}, nil, {}
    local base = env.getUserProfileAppPath()
    self.dir = base .. Bridge.MOD_DIR
    pcall(env.createFolder, base .. "modSettings/")
    pcall(env.createFolder, self.dir)
    pcall(env.deleteFile, self.dir .. Bridge.COMMAND_FILE)
    -- Scenery ghosting runs only with the player's opt-in setting, and never silently: each rig
    -- whose collision with scenery goes off is said in game (AGENTS.md 10).
    if type(Bridge.Hitch) == "table" then
        Bridge.Hitch.onGhost = function() self:notify(Bridge.NOTICES.GHOSTED, false) end
    end
    log("bridge ready; waiting for the companion session in %s", self.dir)
end

function Bridge:loadMap()
    local env = getfenv(0)   -- the game's global table, as AutoDrive register.lua:329
    if env.g_server == nil then return end
    self:open(env)
    self:installInput(env)
    pcall(Bridge.watchSaves, env)
    pcall(function()
        env.g_messageCenter:subscribe(env.MessageType.AI_JOB_STOPPED,
            function(_, job, aiMessage) Bridge.onAIJobStopped(env, job, aiMessage) end, self)
    end)
end

function Bridge:deleteMap()
    if self.env ~= nil and self.session ~= nil then self:dropSession("UNLOADED") end
    if self.env ~= nil then self:removeInput(self.env) end
    self:closePipe()
    self.env = nil
end

-- FS25 discards a sourceFile's return value (see FarmhandObserve.lua).
Bridge.Observe = FarmhandObserve
-- Each vehicle's last AI end, for the snapshot (a helper handed off ends while no op watches it).
if type(FarmhandObserve) == "table" then
    FarmhandObserve.aiStop = function(v) return Bridge.aiStops[v] end
end
Bridge.Hitch = FarmhandHitch
if type(FarmhandHitch) == "table" then FarmhandHitch.onJobStart = Bridge.farmhandJob end   -- its GOTO jobs too
Bridge.Drive = FarmhandDrive
if FarmhandReloading and type(FarmhandObserve) == "table" and type(FarmhandObserve.adoptState) == "function" then
    FarmhandObserve.adoptState(FarmhandBridge, Bridge)            -- see Observe.adoptState
end
FarmhandBridge = Bridge
-- fhReload (FarmhandProbe) re-reads this file into the running game: registered once only.
if type(addModEventListener) == "function" and not FarmhandReloading then addModEventListener(Bridge) end
return Bridge
