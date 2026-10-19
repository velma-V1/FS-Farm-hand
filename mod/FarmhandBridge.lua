-- AC-001 game-side command bridge (server only). Two parts:
--
-- 1. Operations, shared by the companion and the console probe:
--      drive    AutoDrive DRIVETO to a map marker  (TRAVEL_NEAR and DELIVER)
--      navigate GIANTS GOTO to a pose, any distance  (TRAVEL_NEAR and DELIVER
--               without AutoDrive or a fitting marker)
--      stage    GIANTS GOTO towards the staging pose, only from outside the
--               local work radius                 (Hitch.startStaging)
--      maneuver Farmhand's bounded forward LOCAL_MANEUVER onto the wagon's
--               centreline                        (Hitch.newManeuver / maneuverStep)
--      align  bounded ALIGN_REVERSE               (Hitch.newAlign / alignStep)
--      attach GIANTS attach of the exact candidate (Hitch.attach / attachState)
--      detach GIANTS detach of one attached implement, where GIANTS allows it
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
Bridge.DRIVE_TIMEOUT_MS = 1200000
-- A navigate whose tractor gains no NAVIGATE_PROGRESS_M for NAVIGATE_STALL_MS is
-- stopped: GIANTS keeps a BLOCKED agent waiting (AIDrivable:onUpdate 314-316).
Bridge.NAVIGATE_STALL_MS = 90000
Bridge.NAVIGATE_PROGRESS_M = 2
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

local function stopped(v)
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
    if not stopped(v) then return "NOT_STOPPED" end
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
Bridge.pendingGoto = {}       -- tractor -> ms left to watch an aborted, unseen GOTO

-- ------------------------------------------------------------------ drive
-- AutoDrive's external interface: StartDriving(vehicle, destinationID,
-- unloadDestinationID) starts the vehicle's current mode once both are valid
-- markers (ExternalInterface.lua:114-131), so DRIVETO is selected first
-- (StateModule.lua:664, AutoDrive.lua:23); DriveToMode drives to the first
-- marker (DriveToMode.lua:16-33). Arrival and failure both end in
-- stopAutoDrive (DriveToMode.lua:42, DriveToDestinationTask.lua:35-37), so
-- "ended" is not "arrived": the companion verifies the position.
function starters.drive(env, spec)
    local v = spec.tractor
    local denied = idle(env, v, spec.farmId)
    if denied ~= nil then return nil, denied end
    if v.ad == nil or env.AutoDrive == nil then return nil, "NO_AUTODRIVE" end
    -- With the vehicle's "start helper" on, AutoDrive's own stop hands the vehicle
    -- to Courseplay or a GIANTS AI job (Specialization.lua:1251-1285), which would
    -- drive off with the pair. That setting is the user's; refuse, never change it.
    if v.ad.stateModule:getStartHelper() ~= false then return nil, "AD_START_HELPER_ON" end
    local markers = env.AutoDrive:GetAvailableDestinations()
    if type(spec.marker) ~= "number" or markers[spec.marker] == nil then return nil, "UNKNOWN_MARKER" end
    v.ad.stateModule:setMode(env.AutoDrive.MODE_DRIVETO)
    env.AutoDrive:StartDriving(v, spec.marker, spec.marker)
    return { seen = false }
end

function steppers.drive(_env, task)
    local spec, v = task.spec, task.spec.tractor
    local status, code = stillOurs(spec)
    if status ~= nil then return status, code end
    local active = v.ad.stateModule:isActive()
    if active == true then task.seen = true end
    if not task.seen then
        if task.elapsed > Bridge.START_TIMEOUT_MS then return "FAILED", "AD_NOT_STARTED" end
        return nil
    end
    if active ~= false then
        if active ~= true then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
        if task.elapsed > Bridge.DRIVE_TIMEOUT_MS then return "FAILED", "TIMEOUT" end
        return nil
    end
    -- Set on AutoDrive's own error stops (DrivePathModule.lua:67,100, TaskModule.lua:142).
    if v.ad.isStoppingWithError == true then return "FAILED", "AD_STOPPED_WITH_ERROR" end
    return settle(task, "AD_ENDED")
end

-- Server only (AD Specialization.lua:1290); only while the job we saw runs.
function aborts.drive(_env, task)
    local v = task.spec.tractor
    if task.seen and v.ad.stateModule:isActive() == true then v:stopAutoDrive() end
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

-- progress: whether a lack of movement ends the job (navigate only; a stage is
-- short and bounded by its own timeout).
local function stepGoto(env, task, timeoutMs, timeoutCode, endCode, progress)
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
        if progress then
            local x, _, z = env.getWorldTranslation(v.rootNode)
            if not (finite(x) and finite(z)) then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
            if task.fromX == nil or math.sqrt((x - task.fromX) ^ 2 + (z - task.fromZ) ^ 2)
                    >= Bridge.NAVIGATE_PROGRESS_M then
                task.fromX, task.fromZ, task.fromAt = x, z, task.elapsed
            elseif task.elapsed - task.fromAt > Bridge.NAVIGATE_STALL_MS then
                logAgent(env, v, task.op .. " stalled")
                return "FAILED", "NAVIGATE_STALLED"
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

function starters.stage(env, spec) return startGoto(env, spec, "STAGE_") end
function steppers.stage(env, task)
    return stepGoto(env, task, Bridge.STAGE_TIMEOUT_MS, "STAGE_TIMEOUT", "STAGE_ENDED", false)
end
aborts.stage = abortGoto

function starters.navigate(env, spec) return startGoto(env, spec, "NAVIGATE_") end
function steppers.navigate(env, task)
    return stepGoto(env, task, Bridge.DRIVE_TIMEOUT_MS, "TIMEOUT", "GOTO_ENDED", true)
end
aborts.navigate = abortGoto

-- --------------------------------------------------------------- maneuver
-- Hitch.onNote prints each phase line (once per phase and at the end).
function starters.maneuver(env, spec)
    Bridge.Hitch.onNote = function(line) log("maneuver %s", line) end
    -- Read-only steering samples of the local maneuver (diagnostics, 2 per second).
    Bridge.Hitch.onSteer = function(line) log("maneuver %s", line) end
    local session, reason = Bridge.Hitch.newManeuver(env, {
        tractor = spec.tractor, wagon = spec.wagon, tractorJoint = spec.tractorJoint,
        wagonJoint = spec.wagonJoint, farmId = spec.farmId, isReserved = spec.owned,
    })
    if session == nil then return nil, reason end
    Bridge.lastManeuver = session
    return { session = session }
end

function steppers.maneuver(env, task, dt)
    local code = Bridge.Hitch.maneuverStep(env, task.session, dt)
    if code == "CONTINUE" then return nil end
    if code ~= "LINED_UP" then return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code end
    return settle(task, code)       -- braked by the latch; DONE once observed at rest
end

function aborts.maneuver(env, task)
    Bridge.Hitch.abort(env, task.session, "STOPPED")
end

-- ------------------------------------------------------------------ align
function starters.align(env, spec)
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
    log("align end %s %s", code, Bridge.Hitch.describe(task.session))
    if code == "CANDIDATE" then return "DONE", code end
    return code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code
end

function aborts.align(env, task)
    Bridge.Hitch.abort(env, task.session, "STOPPED")
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
    v:detachImplementByObject(implement)
    return {}
end

function steppers.detach(_env, task)
    local status, code = stillOurs(task.spec)
    if status ~= nil then return status, code end
    local v, implement = task.spec.tractor, task.spec.wagon
    if v:getImplementByObject(implement) == nil and implement:getAttacherVehicle() == nil then
        return settle(task, "DETACHED")
    end
    if task.elapsed > Bridge.DETACH_TIMEOUT_MS then return "UNVERIFIED", "DETACH_TIMEOUT" end
    return nil
end

-- GIANTS' detach is not undone.
function aborts.detach() end

-- ------------------------------------------------------------------- stop
-- spec.target: the running task on this tractor, if any; it is aborted first.
function starters.stop(env, spec)
    if spec.target ~= nil then Bridge.abort(env, spec.target, "STOPPED") end
    return {}
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
    local ownMotion = Bridge.pendingGoto[v] ~= nil or (target ~= nil
        and ((target.op == "drive" and v.ad.stateModule:isActive() ~= false)
        or ((target.op == "stage" or target.op == "navigate") and target.seen and v:getIsAIActive() ~= false)))
    if not ownMotion and stopped(v) then
        Bridge.Hitch.disarm(v)                    -- the job's motion is over: stop watching
        return "DONE", "STOPPED"
    end
    if task.elapsed > Bridge.STOP_TIMEOUT_MS then return "FAILED", "NOT_STOPPED" end
    return nil
end

function aborts.stop() end

-- --------------------------------------------------------------- runner

Bridge.NEEDS_WAGON = { maneuver = true, align = true, attach = true, detach = true }

function Bridge.start(env, spec)
    local starter = starters[spec.op]
    if starter == nil then return nil, "UNKNOWN_OP" end
    local ok, task, code, status = pcall(starter, env, spec)
    if not ok then return nil, "UNKNOWN_OBSERVATION", "UNVERIFIED" end
    if task == nil then return nil, code, status or "FAILED" end
    task.op, task.spec, task.elapsed = spec.op, spec, 0
    return task
end

function Bridge.step(env, task, dt)
    if task.status ~= nil then return task.status, task.code end
    task.elapsed = task.elapsed + dt
    local ok, status, code = pcall(steppers[task.op], env, task, dt)
    if not ok then status, code = "UNVERIFIED", "UNKNOWN_OBSERVATION" end
    if status ~= nil then
        if status ~= "DONE" then Bridge.abort(env, task, code) end
        task.status, task.code = status, code
    end
    return status, code
end

-- Stop the Farmhand-owned motion of a task (idempotent; never detaches).
function Bridge.abort(env, task, code)
    if task.aborted then return end
    task.aborted = true
    pcall(aborts[task.op], env, task)
    if task.status == nil then task.status, task.code = "FAILED", code or "STOPPED" end
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
    if self.active ~= nil then
        Bridge.abort(self.env, self.active.task, code)
        self:result(self.session, self.active.id, self.active.task.op, preempted(self.active.task), code)
        for _, joined in ipairs(self.active.joined or {}) do
            self:result(self.session, joined, "stop", "FAILED", code)
        end
        self.active = nil
    end
    if self:companionOwner() then Bridge.owner = nil end
    Bridge.lastAlign, Bridge.lastManeuver = nil, nil
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

local ATTRS = { tractor = "tractor", wagon = "wagon", marker = "marker", x = "x", z = "z", yaw = "yaw",
                tractorJoint = "tj", wagonJoint = "wj", maxDistanceM = "dist" }

function Bridge:pollCommand()
    local env, path = self.env, self.dir .. Bridge.COMMAND_FILE
    local cmd = nil
    pcall(function()
        local xml = env.XMLFile.loadIfExists("fhAc001Command", path)
        if xml == nil then return end
        cmd = { session = xml:getString("command#session"), id = tonumber(xml:getString("command#id")),
                op = xml:getString("command#op") }
        for key, attr in pairs(ATTRS) do cmd[key] = tonumber(xml:getString("command#" .. attr)) end
        xml:delete()
    end)
    if cmd == nil then return nil end
    pcall(env.deleteFile, path)     -- the file disappearing is receipt, never success
    return cmd
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
    if Bridge.NEEDS_WAGON[cmd.op] then spec.wagon = object(cmd.wagon) end
    if spec.tractor == nil or (Bridge.NEEDS_WAGON[cmd.op] and spec.wagon == nil) then
        return self:result(cmd.session, cmd.id, cmd.op, "REJECTED", "UNKNOWN_VEHICLE")
    end
    local owner = "companion:" .. self.session
    spec.owned = function() return Bridge.owner == owner end
    if self.active ~= nil then
        -- Only a stop for the same tractor may pre-empt the running command.
        if cmd.op ~= "stop" or self.active.task.spec.tractor ~= spec.tractor then
            return self:result(cmd.session, cmd.id, cmd.op, "REJECTED", "BUSY")
        end
        -- A stop while a stop runs joins it: both get its one observed outcome, so
        -- a second stop can never turn the first one's confirmation into FAILED.
        if self.active.task.op == "stop" then
            self:result(cmd.session, cmd.id, cmd.op, "ACCEPTED", "ACCEPTED")
            self.active.joined = self.active.joined or {}
            table.insert(self.active.joined, cmd.id)
            return
        end
        spec.target = self.active.task
        self:result(self.session, self.active.id, self.active.task.op, preempted(self.active.task), "STOPPED")
        self.active = nil
    end
    self:result(cmd.session, cmd.id, cmd.op, "ACCEPTED", "ACCEPTED")
    local task, code, status = Bridge.start(env, spec)
    if task == nil then return self:result(cmd.session, cmd.id, cmd.op, status, code) end
    self.active = { id = cmd.id, task = task }
end

function Bridge:update(dt)
    if self.env == nil then return end
    self.clock = self.clock + dt
    Bridge.watchGoto(self.env, dt)
    if self.pipe == nil and self.session ~= nil then
        self.pipeTimer = self.pipeTimer + dt
        if self.pipeTimer >= Bridge.PIPE_RETRY_MS then self.pipeTimer = 0; self:openPipe() end
    end
    self.sessionTimer = self.sessionTimer + dt
    if self.sessionTimer >= Bridge.SESSION_CHECK_MS then self.sessionTimer = 0; self:checkSession() end
    local cmd = self:pollCommand()
    if cmd ~= nil then self:handle(cmd) end
    if self.active ~= nil then
        local status, code = Bridge.step(self.env, self.active.task, dt)
        if status ~= nil then
            self:result(self.session, self.active.id, self.active.task.op, status, code)
            for _, joined in ipairs(self.active.joined or {}) do
                self:result(self.session, joined, "stop", status, code)
            end
            self.active = nil
        end
    end
    self.snapshotTimer = self.snapshotTimer + dt
    if self.snapshotTimer >= Bridge.SNAPSHOT_MS and self.pipe ~= nil then
        self.snapshotTimer = 0
        self.seq = self.seq + 1
        local line = self.Observe.snapshot(self.env, self.seq, self.session)
        if line ~= nil then self:writeLine(line) end
    end
end

-- ------------------------------------------------------- player requests

Bridge.NOTICES = {
    CONNECTED = "Farmhand: companion connected",
    ACCEPTED = "Farmhand: fetching a tractor and the implement",
    BUSY = "Farmhand: already working on a request",
    COMPLETE = "Farmhand: tractor and implement delivered",
    ACCEPTED_MOVE = "Farmhand: on my way",
    ACCEPTED_HITCH = "Farmhand: hitching a tractor to the implement",
    COMPLETE_HITCH = "Farmhand: tractor and implement hitched",
    ACCEPTED_DETACH = "Farmhand: unhitching the implement",
    COMPLETE_DETACH = "Farmhand: implement unhitched",
    UNSUPPORTED_TRACTOR_ONLY = "Farmhand: moving a tractor on its own is not supported yet; ask for a tractor with an implement, such as the forage wagon",
    UNSUPPORTED_NAMED_VEHICLE = "Farmhand: choosing a vehicle by its name is not supported yet; name the kind of implement, such as the forage wagon or the cultivator",
    COMPLETE_MOVE = "Farmhand: arrived",
    CANCELLED = "Farmhand: stopped",
    STOPPING = "Farmhand: stopping",
    NOTHING_TO_STOP = "Farmhand: nothing to stop",
    NOT_UNDERSTOOD = "Farmhand: sorry, I did not understand that",
    NOT_LINKED = "Farmhand: the companion is not running",
    NO_TEXT_INPUT = "Farmhand: the text field is unavailable",
    STATUS_IDLE = "Farmhand: idle",
    LISTENING = "Farmhand: listening...",
    NOT_HEARD = "Farmhand: sorry, I did not hear that",
    VOICE_UNAVAILABLE = "Farmhand: voice is unavailable (no microphone, or Left Alt + N to install speech); type with Left Alt + K",
    VOICE_INSTALLING = "Farmhand: downloading speech recognition...",
    VOICE_READY = "Farmhand: speech recognition installed; hold Left Alt + V to speak",
    AI_INSTALLING = "Farmhand: downloading the AI model; plain commands work meanwhile",
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
    AI_INSTALL_FAILED = "Farmhand: the AI model download failed; plain commands still work",
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
    NAVIGATE_STALLED = "the tractor got stuck",
    CLEANUP_NOT_CONFIRMED = "the tractor may still be moving; check it",
    NO_HITCHED_PAIR = "no hitched tractor and implement to move",
    NO_ATTACHED_IMPLEMENT = "no hitched implement to unhitch",
    DETACH_NOT_ALLOWED = "the game does not allow unhitching it right now",
    NOT_ATTACHED = "that implement is not hitched",
    DETACH_TIMEOUT = "unhitching did not finish; check the tractor",
    DESTINATION_NOT_FOUND = "I do not know that place",
    DESTINATION_AMBIGUOUS = "more than one place has that name",
    AUTODRIVE_UNAVAILABLE = "that destination needs AutoDrive",
    NOT_LINKED = "the game is not linked to the companion",
    SEATED_DURING_REVERSE = "step out of the tractor for the last metres of the hitch, then ask again",
    SEATED_DURING_MANEUVER = "step out of the tractor for the last metres of the hitch, then ask again",
    REJECTED_SEATED_DURING_REVERSE = "step out of the tractor for the last metres of the hitch, then ask again",
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
    LOCAL_PATH_PERSON = "stand clear of the tractor and the implement, then ask again",
    PERSON_TOO_CLOSE = "you came too close to the tractor, so Farmhand stopped",
    OTHER_VEHICLE_MOVED = "another vehicle moved near the tractor, so Farmhand stopped",
    OTHER_VEHICLE_NEAR = "another vehicle came too close to the tractor, so Farmhand stopped",
    LOCAL_AREA_UNKNOWN = "the area around the implement could not be checked",
    TRAVEL_ENDED_TOO_FAR = "the tractor stopped too far from the implement to hitch it",
    NO_SAFE_APPROACH = "there is no way to drive up to the implement from here; give it more room",
    TRAPPED = "the tractor is stuck and cannot drive on; check it",
    ALIGN_WRONG_SIDE = "the tractor stopped on the wrong side of the implement to hitch it",
    ALIGN_HEADING_OUT_OF_RANGE = "the tractor stopped facing the wrong way to hitch the implement",
    ALIGN_LATERAL_OUT_OF_RANGE = "the tractor stopped too far to the side of the implement to hitch it",
    STALLED = "the tractor got stuck at the implement",
    DIVERGED = "the tractor could not follow its line to the implement",
    WAGON_MOVED = "the implement moved",
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
    local last = notice:match("^STATUS_LAST_(%u+)$")
    if last ~= nil then return "Farmhand: idle; last job " .. (Bridge.OUTCOMES[last] or last), false end
    local status, reason = notice:match("^(%u+)_([%w_]+)$")
    if status == "BLOCKED" or status == "FAILED" or status == "UNVERIFIED" then
        return "Farmhand: not delivered - " .. (Bridge.REASONS[reason] or reason), true
    end
    return "Farmhand: " .. notice, false
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
            bridge:notify("Farmhand: press Left Alt + N to choose the AI model", false)
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
    self.session, self.active, self.pipe, self.dropped = nil, nil, nil, {}
    local base = env.getUserProfileAppPath()
    self.dir = base .. Bridge.MOD_DIR
    pcall(env.createFolder, base .. "modSettings/")
    pcall(env.createFolder, self.dir)
    pcall(env.deleteFile, self.dir .. Bridge.COMMAND_FILE)
    log("bridge ready; waiting for the companion session in %s", self.dir)
end

function Bridge:loadMap()
    local env = getfenv(0)   -- the game's global table, as AutoDrive register.lua:329
    if env.g_server == nil then return end
    self:open(env)
    self:installInput(env)
end

function Bridge:deleteMap()
    if self.env ~= nil and self.session ~= nil then self:dropSession("UNLOADED") end
    if self.env ~= nil then self:removeInput(self.env) end
    self:closePipe()
    self.env = nil
end

-- FS25 discards a sourceFile's return value (see FarmhandObserve.lua).
Bridge.Observe = FarmhandObserve
Bridge.Hitch = FarmhandHitch
FarmhandBridge = Bridge
if type(addModEventListener) == "function" then addModEventListener(Bridge) end
return Bridge
