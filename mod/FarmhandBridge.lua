-- AC-001 game-side command bridge (server only). Two parts:
--
-- 1. Operations, shared by the companion and the console probe:
--      drive    AutoDrive DRIVETO to a map marker  (TRAVEL_NEAR and DELIVER)
--      navigate GIANTS GOTO to a pose, any distance  (TRAVEL_NEAR and DELIVER
--               without AutoDrive or a fitting marker)
--      stage    GIANTS GOTO to the staging pose     (Hitch.startStaging)
--      align  bounded ALIGN_REVERSE               (Hitch.newAlign / alignStep)
--      attach GIANTS attach of the exact candidate (Hitch.attach / attachState)
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
-- Every game and mod call is cited in adapters/EVIDENCE.md. Lua 5.1 compatible.

local Bridge = {}

Bridge.MOD_DIR = "modSettings/FS25_Farmhand/"
Bridge.COMMAND_FILE = "command.xml"
Bridge.SESSION_FILE = "session.xml"
Bridge.SNAPSHOT_MS = 500
Bridge.SESSION_CHECK_MS = 1000
Bridge.COMPANION_TIMEOUT_MS = 10000   -- no heartbeat for this long: stop, drop session
Bridge.PIPE_RETRY_MS = 5000
Bridge.START_TIMEOUT_MS = 5000        -- a job not running by then was refused
Bridge.SETTLE_TIMEOUT_MS = 5000       -- an ended job must come to rest within this
Bridge.STAGE_TIMEOUT_MS = 120000
Bridge.DRIVE_TIMEOUT_MS = 1200000
-- A navigate whose tractor gains no NAVIGATE_PROGRESS_M for NAVIGATE_STALL_MS is
-- stopped: GIANTS keeps a BLOCKED agent waiting (AIDrivable:onUpdate 314-316).
Bridge.NAVIGATE_STALL_MS = 90000
Bridge.NAVIGATE_PROGRESS_M = 2
Bridge.STOP_TIMEOUT_MS = 15000

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

-- Before any motion: owned, unoccupied, no AI job of any kind, at rest.
local function idle(v, farmId)
    if v.ownerFarmId ~= farmId then return "NOT_OWNED" end
    local entered = v:getIsEntered()
    if entered ~= false then return entered == true and "OCCUPIED" or "UNKNOWN_OBSERVATION" end
    local ai = v:getIsAIActive()
    if ai ~= false then return ai == true and "AI_ACTIVE" or "UNKNOWN_OBSERVATION" end
    if not stopped(v) then return "NOT_STOPPED" end
    return nil
end

-- While a job we started runs: still ours and nobody got in.
local function stillOurs(spec)
    local v = spec.tractor
    if spec.owned() ~= true or v.ownerFarmId ~= spec.farmId then return "FAILED", "RESERVATION_LOST" end
    local entered = v:getIsEntered()
    if entered ~= false then
        return entered == true and "FAILED" or "UNVERIFIED", entered == true and "OCCUPIED" or "UNKNOWN_OBSERVATION"
    end
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
    local denied = idle(v, spec.farmId)
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
    local denied = idle(spec.tractor, spec.farmId)
    if denied ~= nil then return nil, denied end
    local ok, reason = Bridge.Hitch.startStaging(env, spec.tractor, spec.farmId, spec.x, spec.z, spec.yaw)
    if not ok then return nil, prefix .. tostring(reason) end
    return { seen = false }
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
                return "FAILED", "NAVIGATE_STALLED"
            end
        end
        return nil
    end
    return settle(task, endCode)
end

-- Only the GOTO this task started and saw running (GIANTS AIJobVehicle 431).
local function abortGoto(env, task)
    if task.seen and task.spec.tractor:getIsAIActive() == true then
        Bridge.Hitch.cancelStaging(env, task.spec.tractor)
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

-- ------------------------------------------------------------------ align
function starters.align(env, spec)
    local session, reason = Bridge.Hitch.newAlign(env, {
        tractor = spec.tractor, wagon = spec.wagon, tractorJoint = spec.tractorJoint,
        wagonJoint = spec.wagonJoint, farmId = spec.farmId, isReserved = spec.owned,
        maxDistanceM = spec.maxDistanceM, assistRadiusM = spec.assistRadiusM,
        maxVerticalM = spec.maxVerticalM, maxSpeedKmh = spec.maxSpeedKmh,
    })
    if session == nil then return nil, reason end
    Bridge.lastAlign = session
    return { session = session }
end

function steppers.align(env, task, dt)
    local code = Bridge.Hitch.alignStep(env, task.session, dt)
    if code == "CONTINUE" then return nil end
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

-- GIANTS' smooth attach is not undone; Farmhand never detaches.
function aborts.attach() end

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
    local ownMotion = target ~= nil and ((target.op == "drive" and v.ad.stateModule:isActive() ~= false)
        or ((target.op == "stage" or target.op == "navigate") and target.seen and v:getIsAIActive() ~= false))
    if not ownMotion and stopped(v) then return "DONE", "STOPPED" end
    if task.elapsed > Bridge.STOP_TIMEOUT_MS then return "FAILED", "NOT_STOPPED" end
    return nil
end

function aborts.stop() end

-- --------------------------------------------------------------- runner

Bridge.NEEDS_WAGON = { align = true, attach = true }

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

-- A pre-empted attach may still complete in GIANTS: its outcome is unknown.
local function preempted(task)
    return task.op == "attach" and "UNVERIFIED" or "FAILED"
end

function Bridge:companionOwner()
    return self.session ~= nil and Bridge.owner == "companion:" .. self.session
end

-- Drop the companion: stop whatever it started, release ownership.
function Bridge:dropSession(code)
    if self.active ~= nil then
        Bridge.abort(self.env, self.active.task, code)
        self:result(self.session, self.active.id, self.active.task.op, preempted(self.active.task), code)
        self.active = nil
    end
    if self:companionOwner() then Bridge.owner = nil end
    Bridge.lastAlign = nil
    log("bridge session %s dropped: %s", tostring(self.session), code)
    self.dropped[self.session] = true      -- a dropped session is never re-adopted
    self.session = nil
end

function Bridge:checkSession()
    local env = self.env
    local id, beat = nil, nil
    pcall(function()
        local xml = env.XMLFile.loadIfExists("fhAc001Session", self.dir .. Bridge.SESSION_FILE)
        if xml == nil then return end
        id, beat = token(xml:getString("session#id")), xml:getString("session#beat")
        xml:delete()
    end)
    if id ~= nil and id ~= self.session and not self.dropped[id] then
        if self.session ~= nil then self:dropSession("SESSION_CHANGED") end
        if Bridge.owner == nil then
            self.session, self.lastId, self.beat, self.beatAt = id, 0, beat, self.clock
            Bridge.owner = "companion:" .. id
            log("bridge session %s adopted", id)
            self:closePipe()
            self:openPipe()
        end
    elseif id ~= nil and beat ~= self.beat then
        self.beat, self.beatAt = beat, self.clock
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

-- Server only; folders and stale-command cleanup as Farmhand Companion init().
function Bridge:open(env)
    self.env, self.clock, self.seq, self.lastId = env, 0, 0, 0
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
end

function Bridge:deleteMap()
    if self.env ~= nil and self.session ~= nil then self:dropSession("UNLOADED") end
    self:closePipe()
    self.env = nil
end

-- FS25 discards a sourceFile's return value (see FarmhandObserve.lua).
Bridge.Observe = FarmhandObserve
Bridge.Hitch = FarmhandHitch
FarmhandBridge = Bridge
if type(addModEventListener) == "function" then addModEventListener(Bridge) end
return Bridge
