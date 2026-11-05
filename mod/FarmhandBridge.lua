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
Bridge.FIELDWORK_CHECK_MS = 3000      -- a bounded test area is re-measured this often
Bridge.FIELDWORK_MAX_AREA_M2 = 20000  -- the largest bounded test area
-- Travel (AutoDrive drive and sell, GIANTS GOTO, Deliver) is judged by progress: each has its
-- own unmoved watch (AD_STALL_MS, NAVIGATE_STALL_MS, DELIVER_STALL_MS); this is only a safety
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
local adGraph                 -- AutoDrive's graph manager (ad_marker below); also read by drive

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
    Bridge.restoreTransportCrops(task.cropWheels)
    task.cropWheels = nil
end

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
    local AD = (Bridge.Observe and Bridge.Observe.autoDrive and Bridge.Observe.autoDrive(env)) or env.AutoDrive
    if v.ad == nil or AD == nil then return nil, "NO_AUTODRIVE" end
    -- With the vehicle's "start helper" on, AutoDrive's own stop hands the vehicle
    -- to Courseplay or a GIANTS AI job (Specialization.lua:1251-1285), which would
    -- drive off with the pair. That setting is the user's; refuse, never change it.
    if v.ad.stateModule:getStartHelper() ~= false then return nil, "AD_START_HELPER_ON" end
    local markers = AD:GetAvailableDestinations()
    if type(spec.marker) ~= "number" or markers[spec.marker] == nil then return nil, "UNKNOWN_MARKER" end
    -- AutoDrive drives from the network waypoint nearest the vehicle (getClosestWayPoint; past
    -- 30 m its path finder first plans onto it, DriveToDestinationTask.lua 18-24, and gives up at
    -- once without a route on: PathFinderModule:startPathPlanningToWayPoint 215-226 needs
    -- ADGraphManager:pathFromTo(closest, destination)). Live 2026-10-22 the waypoint nearest the
    -- rig was on a 7-waypoint dead end ("Could not calculate path - shutting down"): asked here,
    -- read only, so a route gap is said for what it is.
    local graph = adGraph(env)
    if graph ~= nil and type(graph.pathFromTo) == "function" and type(graph.getMapMarkerById) == "function" then
        local okRoute, routed, closest, distance = pcall(function()
            local target = graph:getMapMarkerById(spec.marker).id
            local from, d = v:getClosestWayPoint()
            local path = graph:pathFromTo(from, target)
            return path ~= nil and #path >= 1, from, d
        end)
        if okRoute and not routed then
            log("drive refused AD_NO_ROUTE: no route on AutoDrive's network from waypoint %s (%.1f m from the vehicle) to %s",
                tostring(closest), distance or -1, tostring(markers[spec.marker].name))
            return nil, "AD_NO_ROUTE"
        end
    end
    local okAt, x, _, z = pcall(env.getWorldTranslation, v.rootNode)
    local saved = Bridge.capAdSpeed(v)
    v.ad.stateModule:setMode(AD.MODE_DRIVETO)
    AD:StartDriving(v, spec.marker, spec.marker)
    local cropWheels = Bridge.protectTransportCrops(v)
    return { seen = false, startAt = okAt and finite(x) and finite(z) and { x, z } or nil,
             onEnd = function() Bridge.restoreAdSpeed(v, saved); Bridge.restoreTransportCrops(cropWheels) end }
end

-- Farmhand's travel speed cap (the user, 2026-10-22: 40 mph; AutoDrive took the loaded GMC at
-- up to 103 km/h, it lost control over rail tracks and ended against a wall). AutoDrive's own
-- per-vehicle limits (ADStateModule speedLimit / fieldSpeedLimit 48-49, the HUD's) bound every
-- speed it drives (DrivePathModule 286-333 only lowers it for corners, steering, fields), and
-- its own calls step them (decreaseSpeedLimit / decreaseFieldSpeedLimit 1191-1214, floor 2).
-- Lowered to the cap before Farmhand starts it; a drive Farmhand ends gives the player's back.
Bridge.TRAVEL_MAX_KMH = 64           -- AutoDrive's limits step in whole km/h
Bridge.TRAVEL_LIMIT_KMH = 40 * 1.609344   -- the user's 40 mph, what a peak is judged against

local function stepLimit(get, step, want, up)
    for _ = 1, 400 do
        local now = get()
        if not finite(now) or (up and now >= want) or (not up and now <= want) then break end
        step()
    end
    return get()
end

function Bridge.capAdSpeed(v)
    local st = v.ad.stateModule
    local saved = { road = st:getSpeedLimit(), field = st:getFieldSpeedLimit() }
    local road = stepLimit(function() return st:getSpeedLimit() end, function() st:decreaseSpeedLimit() end,
                           Bridge.TRAVEL_MAX_KMH, false)
    local field = stepLimit(function() return st:getFieldSpeedLimit() end, function() st:decreaseFieldSpeedLimit() end,
                            Bridge.TRAVEL_MAX_KMH, false)
    log("AutoDrive speed limits %s/%s -> %s/%s km/h (road/field; Farmhand's cap %d)", tostring(saved.road),
        tostring(saved.field), tostring(road), tostring(field), Bridge.TRAVEL_MAX_KMH)
    return saved
end

function Bridge.restoreAdSpeed(v, saved)
    local st = v.ad.stateModule
    local road = stepLimit(function() return st:getSpeedLimit() end, function() st:increaseSpeedLimit() end, saved.road, true)
    local field = stepLimit(function() return st:getFieldSpeedLimit() end, function() st:increaseFieldSpeedLimit() end,
                            saved.field, true)
    log("AutoDrive speed limits given back: %s/%s km/h (were %s/%s)", tostring(road), tostring(field),
        tostring(saved.road), tostring(saved.field))
end

-- A task's end hook (once): a drive gives AutoDrive's limits back.
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

-- What AutoDrive is doing, read only, for the log and for a drive that does not move: its
-- task text (ADStateModule:getCurrentTaskInfo 586-588; "$l10n_AD_task_pathfinding;" while it
-- plans onto its network from more than 30 m away, DriveToDestinationTask.lua 18-21, 65-70),
-- the rig's distance from the network (getClosestWayPoint, ADGraphManager:
-- getDistanceFromNetwork 230-233), its route (ADDrivePathModule:getWayPoints), paused
-- (DrivePathModule.isPaused 141-147), the collision module's verdict
-- (ADCollisionDetectionModule detectedObstable / detectedCollision 16-36, 69-82), which of its
-- sensors are triggered (ADSensor.triggered 522-528), whether it counts its implements folded
-- (AutoDrive.getAllImplementsFolded, AutoDriveUtilFuncs 529-), and for each triggered
-- collision sensor what its own box holds now: its getBoxShape (VirtualSensors 287-375) and
-- getMask (CollSensor.lua 45-48) asked of GIANTS' overlapBox, each shape's object and
-- whether AutoDrive counts it as part of the rig (AutoDrive:checkIsConnected,
-- AutoDriveUtilFuncs 53-80, as its sensor callback CollSensor.lua 64-86 asks).
Bridge.AD_DIAG_MS = 5000
Bridge.AD_STALL_MS = 180000     -- active this long without moving AD_STALL_M: FAILED with what it waits on
Bridge.AD_STALL_M = 2

local function adSensorHits(env, AD, v, sensor)
    local box = sensor:getBoxShape()
    local mission = env.g_currentMission
    local q, seen = { names = {} }, 0
    function q.onAdDiagOverlap(_, nodeId)
        seen = seen + 1
        if nodeId ~= mission.terrainRootNode and #q.names < 4 then
            local object = mission:getNodeObject(nodeId) or mission:getNodeObject(env.getParent(nodeId))
            local name = tostring(select(2, pcall(env.getName, nodeId)))
            if object == nil then
                q.names[#q.names + 1] = name .. "@-"
            else
                local own = object == v or AD:checkIsConnected(v:getRootVehicle(), object)
                local label = type(object.getName) == "function" and object:getName() or tostring(object.typeName)
                q.names[#q.names + 1] = string.format("%s@%s%s", name, tostring(label), own and "(own)" or "")
            end
        end
        return true
    end
    env.overlapBox(box.x, box.y, box.z, box.rx, box.ry, 0, box.size[1], box.size[2], box.size[3], "onAdDiagOverlap", q,
                   sensor:getMask(), true, true, true, true)
    return table.concat(q.names, ",")
end

function Bridge.adState(env, v)
    local d = {}
    local AD = (Bridge.Observe and Bridge.Observe.autoDrive and Bridge.Observe.autoDrive(env)) or env.AutoDrive
    pcall(function() d.task = tostring(v.ad.stateModule:getCurrentTaskInfo()) end)
    pcall(function() local _, dist = v:getClosestWayPoint(); d.network = dist end)
    pcall(function() local wps, cur = v.ad.drivePathModule:getWayPoints(); d.route = wps and #wps or 0; d.at = cur end)
    -- Where it is steering to (live 344392c02cd942ff-2: after a STOP and "continue" it crept at
    -- 0.1-0.4 km/h for 3 min at route point 2 of 467): that waypoint in the vehicle's own frame
    -- (ahead, to its side), and the steering state (rotatedTime of its limits).
    pcall(function()
        local wps, cur = v.ad.drivePathModule:getWayPoints()
        local wp = wps[cur]
        local x, y, z = wp.x, wp.y or 0, wp.z
        local lx, _, lz = env.worldToLocal(v.rootNode, x, y, z)
        d.wpAhead, d.wpSide = lz, lx
    end)
    pcall(function()
        local limit = v.rotatedTime >= 0 and v.maxRotTime or -v.minRotTime
        d.steer = limit ~= 0 and v.rotatedTime / limit or nil
    end)
    pcall(function() d.paused = v.ad.drivePathModule.isPaused == true end)
    pcall(function()
        d.obstacle = v.ad.collisionDetectionModule.detectedObstable == true
        d.collision = v.ad.collisionDetectionModule.detectedCollision == true
    end)
    pcall(function() d.folded = AD.getAllImplementsFolded(v) == true end)
    -- Whether its task ever began (live 2026-10-22 "take the deutz to the farm": no task text, no route, nothing in the
    -- way): its first marker (ADStateModule:getFirstMarkerName 873-879), the active task and the
    -- queue (ADTaskModule:getActiveTask 34-36, getNumberOfTasks 198-200), the fold wait
    -- (Specialization.lua 446: foldStartTime + AutoDrive.foldTimeout against g_time), and whether
    -- GIANTS updates the vehicle at all (forceIsActive, onStartAutoDrive 1294-1295; isActive).
    pcall(function() d.marker = tostring(v.ad.stateModule:getFirstMarkerName()) end)
    pcall(function() d.active = v.ad.taskModule:getActiveTask() ~= nil end)
    pcall(function() d.queued = v.ad.taskModule:getNumberOfTasks() end)
    pcall(function() d.foldWaitMs = (v.ad.foldStartTime or 0) + AD.foldTimeout - env.g_time end)
    pcall(function() d.forced = v.forceIsActive == true; d.updated = v.isActive == true end)
    pcall(function() d.motor = v:getIsMotorStarted() == true end)
    pcall(function() d.speed = v:getLastSpeed() end)
    local triggered, seen = {}, {}
    pcall(function()
        for name, sensor in pairs(v.ad.sensors or {}) do
            if type(sensor) == "table" and sensor.triggered == true then
                local hit = ""
                if type(sensor.getMask) == "function" and type(sensor.getBoxShape) == "function" then
                    local ok, names = pcall(adSensorHits, env, AD, v, sensor)
                    hit = ok and names or "?"
                end
                triggered[#triggered + 1] = name .. "[" .. hit .. "]"
                seen[#seen + 1] = name
            end
        end
    end)
    table.sort(triggered)
    d.sensors = table.concat(triggered, " ")
    return d
end

local function describeAd(d)
    local function f(n) return type(n) == "number" and string.format("%.1f", n) or "?" end
    return string.format("task=%s network=%s route=%s/%s paused=%s obstacle=%s collision=%s folded=%s speed=%s sensors=%s "
        .. "marker=%s activeTask=%s queued=%s foldWaitMs=%s forceIsActive=%s isActive=%s motor=%s wp=(%s,%s) steer=%s",
        tostring(d.task), f(d.network), tostring(d.at), tostring(d.route), tostring(d.paused), tostring(d.obstacle),
        tostring(d.collision), tostring(d.folded), f(d.speed), d.sensors ~= "" and d.sensors or "-", tostring(d.marker),
        tostring(d.active), tostring(d.queued), f(d.foldWaitMs), tostring(d.forced), tostring(d.updated), tostring(d.motor),
        f(d.wpAhead), f(d.wpSide), f(d.steer))
end

-- What a drive that does not move waits on: AutoDrive's own path planning onto its network
-- (a route problem), its collision module or a sensor (something in the way, the rig's own
-- cargo included: the sensors' hits say whose), paused, or none of these.
local function adStallCode(d)
    if type(d.task) == "string" and d.task:find("pathfinding", 1, true) then return "AD_PATHFINDING_STALLED" end
    if d.obstacle or d.collision or (d.sensors or "") ~= "" then return "AD_WAITING_ON_OBSTACLE" end
    if d.paused then return "AD_PAUSED" end
    return "AD_NOT_MOVING"
end

-- GIANTS updates a vehicle nobody sits in only while something raises it active
-- (Vehicle:raiseActive, as Farmhand's own motions and AutoDrive inside its update do): AutoDrive
-- started by its external interface sets forceIsActive (onStartAutoDrive 1294-1295) but its own
-- update, which plans and drives, never ran on an asleep vehicle (live 2026-10-22 "take the deutz
-- to the farm": forceIsActive true, isActive false, no task, motor off, 180 s unmoved). Raised
-- every frame while AutoDrive runs a job Farmhand started, and for one handed off.
local function keepAwake(v)
    if v.ad ~= nil and v.ad.stateModule:isActive() == true then v:raiseActive() end
end

-- AutoDrive's own speed limits from before a run Farmhand handed on to it (ad_unload): the run
-- keeps Farmhand's cap while it works and the player's limits come back once it ends, is stopped
-- or never starts (AutoDrive saves them per vehicle, so a cap left on is lost to the player).
Bridge.handedSpeed = {}
function Bridge.giveBackAdSpeed(v)
    local h = Bridge.handedSpeed[v]
    if h == nil then return end
    Bridge.handedSpeed[v] = nil
    pcall(Bridge.restoreAdSpeed, v, h.saved)
end

function Bridge.keepHandedAwake()
    for v, op in pairs(Bridge.handed) do
        if op == "drive" then pcall(keepAwake, v) end
    end
    for v, h in pairs(Bridge.handedSpeed) do        -- seen running, then ended: limits back
        local ok, active = pcall(function() return v.ad.stateModule:isActive() end)
        if ok and active == true then h.seen = true
        elseif not ok or h.seen then Bridge.giveBackAdSpeed(v) end
    end
end

function steppers.drive(env, task)
    local spec, v = task.spec, task.spec.tractor
    local status, code = stillOurs(spec)
    if status ~= nil then return status, code end
    local active = v.ad.stateModule:isActive()
    if active == true then task.seen = true; v:raiseActive() end
    if not task.seen then
        if task.elapsed > Bridge.START_TIMEOUT_MS then return "FAILED", "AD_NOT_STARTED" end
        return nil
    end
    if active == true and task.elapsed >= (task.diagAt or 0) then
        task.diagAt = task.elapsed + Bridge.AD_DIAG_MS
        local okAt, x, _, z = pcall(env.getWorldTranslation, v.rootNode)
        local ok, d = pcall(Bridge.adState, env, v)
        if okAt and finite(x) and finite(z) and ok then
            if task.movedFrom == nil or math.sqrt((x - task.movedFrom[1]) ^ 2 + (z - task.movedFrom[2]) ^ 2) > Bridge.AD_STALL_M then
                task.movedFrom, task.movedAt = { x, z }, task.elapsed
            end
            log("drive AutoDrive (%.0f, %.0f) %s", x, z, describeAd(d))
            if task.elapsed - task.movedAt > Bridge.AD_STALL_MS then
                local stall = adStallCode(d)
                local okC, contacts = pcall(Bridge.Hitch.rigContacts, env, v)
                log("drive stalled %s: unmoved %.0f s; %s; touching %s", stall, (task.elapsed - task.movedAt) / 1000,
                    describeAd(d), okC and contacts or "?")
                return "FAILED", stall
            end
        end
    end
    if active ~= false then
        if active ~= true then return "UNVERIFIED", "UNKNOWN_OBSERVATION" end
        if task.elapsed > Bridge.DRIVE_TIMEOUT_MS then return "FAILED", "TIMEOUT" end
        return nil
    end
    -- Set on AutoDrive's own error stops (DrivePathModule.lua:67,100, TaskModule.lua:142).
    if v.ad.isStoppingWithError == true then return "FAILED", "AD_STOPPED_WITH_ERROR" end
    -- Ended where it started: AutoDrive gave up (its path finder's "could not calculate path"
    -- stops it without the error flag, DriveToDestinationTask.lua 35-37).
    local okAt, x, _, z = pcall(env.getWorldTranslation, v.rootNode)
    if task.startAt ~= nil and okAt and finite(x) and finite(z)
            and math.sqrt((x - task.startAt[1]) ^ 2 + (z - task.startAt[2]) ^ 2) < Bridge.AD_STALL_M then
        return "FAILED", "AD_ENDED_UNMOVED"
    end
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

-- Whether GIANTS' agent reports BLOCKED (live casing is lower case).
local function agentBlocked(env, v)
    return pcall(function()
        assert(tostring(env.AIDrivable.STATES[v.spec_aiDrivable.lastState]):upper() == "BLOCKED")
    end)
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

function starters.stage(env, spec) return startGoto(env, spec, "STAGE_") end
function steppers.stage(env, task)
    return stepGoto(env, task, Bridge.STAGE_TIMEOUT_MS, "STAGE_TIMEOUT", "STAGE_ENDED", false, "STAGE_")
end
aborts.stage = abortGoto

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
    local task, code = startGoto(env, spec, "NAVIGATE_")
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
function steppers.navigate(env, task)
    pcall(traceNavigateSurface, env, task)
    cargoObstaclesOff(task)
    local status, code = stepGoto(env, task, Bridge.DRIVE_TIMEOUT_MS, "TIMEOUT", "GOTO_ENDED", true, "NAVIGATE_")
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
    if session == nil then return nil, reason end
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
    local big = Bridge.grown(foot, v.spec_pipe ~= nil and Bridge.REPOSITION or Bridge.CLEAR_AWAY)
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
    -- At its heading only for a combine (a trailer comes along its pipe line); anything moved out
    -- of the way arrives at any heading (live 21a5e70740024850-1: the 3650 2 m from its spot, 24
    -- degrees off that heading, corrected until OBSTACLE_AHEAD).
    return starters.farm_drive(env, { tractor = v, x = tx, z = tz, area = 0,
                                      yaw = v.spec_pipe ~= nil and tyaw or 10, farmId = spec.farmId,
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

-- A named spot on a road lane (AutoDrive's waypoint, at the lane's heading): the rig set down on
-- it, or a few metres along the lane, with only its own box clear (the road is the lane; live
-- 3de53846739b4945-1: the room rule for GIANTS' helper put it 28 m off the road, crosswise, and
-- AutoDrive drove in from the grass and jammed at the yard edge). x, z, yaw, or nil.
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
    -- Near (spec.x, spec.z) at spec.yaw when the companion names a spot (a loaded rig onto
    -- AutoDrive's road: live 2026-10-31, AutoDrive would not drive it the 51 m out of the yard);
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

-- -------------------------------------------------------------- ad_marker
-- An AutoDrive destination "Field N" made AutoDrive's own way, so a loaded transport can be
-- delivered there (GIANTS GOTO does not start one, AGENTS.md): ADGraphManager:createMapMarker
-- (GraphManager.lua 360-381, which its HUD and name dialog call; it broadcasts
-- AutoDriveCreateMapMarkerEvent and AutoDrive saves it with the game) on the network waypoint
-- nearest field N's own boundary (GIANTS Field polygonPoints, as Observe samples them), at
-- most AD_FIELD_EDGE_M from it and connected (incoming and outgoing). AutoDrive keeps
-- ADGraphManager in its own environment: it is the environment of its own
-- GetAvailableDestinations (ExternalInterface.lua 84-94 reads it), reached with getfenv, which
-- AutoDrive itself uses in FS25 (register.lua 329). DONE once AutoDrive lists the marker.
Bridge.AD_FIELD_EDGE_M = 30
Bridge.AD_MARKER_TIMEOUT_MS = 5000

function adGraph(env)
    local AD = (Bridge.Observe and Bridge.Observe.autoDrive and Bridge.Observe.autoDrive(env)) or env.AutoDrive
    if type(AD) ~= "table" or type(AD.GetAvailableDestinations) ~= "function" or type(env.getfenv) ~= "function" then
        return nil
    end
    local ok, adEnv = pcall(env.getfenv, AD.GetAvailableDestinations)
    local graph = ok and type(adEnv) == "table" and adEnv.ADGraphManager or nil
    if type(graph) ~= "table" or type(graph.createMapMarker) ~= "function" or type(graph.getMapMarkerByName) ~= "function"
            or type(graph.getWayPoints) ~= "function" then
        return nil
    end
    return graph
end

local function segmentDistance(px, pz, ax, az, bx, bz)
    local dx, dz = bx - ax, bz - az
    local l2 = dx * dx + dz * dz
    local t = l2 > 0 and math.max(0, math.min(1, ((px - ax) * dx + (pz - az) * dz) / l2)) or 0
    local cx, cz = ax + t * dx, az + t * dz
    return math.sqrt((px - cx) ^ 2 + (pz - cz) ^ 2)
end

-- Field 0: the farm's own building whose root is within AD_PLACE_AT_M of (x, z) (as Observe
-- lists places: placeableSystem placeables, getName, rootNode), the marker named as it, on the
-- connected waypoint nearest it within AD_PLACE_M (buildings stand back from the roads).
Bridge.AD_PLACE_AT_M = 1
Bridge.AD_PLACE_M = 60

local function placeMarker(env, spec, graph)
    if not (finite(spec.x) and finite(spec.z)) then return nil, "INVALID_PLACE" end
    local name
    for _, pl in pairs(env.g_currentMission.placeableSystem.placeables or {}) do
        if pl.ownerFarmId == spec.farmId then
            local px, _, pz = env.getWorldTranslation(pl.rootNode)
            if finite(px) and finite(pz) and math.sqrt((px - spec.x) ^ 2 + (pz - spec.z) ^ 2) <= Bridge.AD_PLACE_AT_M then
                name = pl:getName()
            end
        end
    end
    if type(name) ~= "string" or name == "" then return nil, "AD_PLACE_NOT_FOUND" end
    if graph:getMapMarkerByName(name) ~= nil then return { name = name, existed = true } end
    local best, bestD
    for id, wp in pairs(graph:getWayPoints() or {}) do
        if type(wp) == "table" and finite(wp.x) and finite(wp.z) and #(wp.out or {}) > 0 and #(wp.incoming or {}) > 0 then
            local d = math.sqrt((wp.x - spec.x) ^ 2 + (wp.z - spec.z) ^ 2)
            if d <= Bridge.AD_PLACE_M and (bestD == nil or d < bestD) then best, bestD = id, d end
        end
    end
    if best == nil then return nil, "AD_NO_WAYPOINT_AT_PLACE" end
    log("ad_marker %s on waypoint %s, %.1f m from the building", name, tostring(best), bestD)
    graph:createMapMarker(best, name)
    return { name = name, graph = graph }
end

function starters.ad_marker(env, spec)
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local graph = adGraph(env)
    if graph == nil then return nil, "AD_MARKER_UNSUPPORTED" end
    local n = spec.field
    if n == 0 then return placeMarker(env, spec, graph) end
    if not finite(n) or n < 1 or n ~= math.floor(n) then return nil, "INVALID_FIELD" end
    local name = string.format("Field %d", n)
    if graph:getMapMarkerByName(name) ~= nil then return { name = name, existed = true } end
    local field
    for _, f in ipairs(env.g_fieldManager.fields or {}) do
        local id = (type(f.getId) == "function" and f:getId()) or f.fieldId
        if id == n then field = f end
    end
    if field == nil or type(field.polygonPoints) ~= "table" or #field.polygonPoints < 3 then return nil, "FIELD_NOT_FOUND" end
    local corners = {}
    for i, node in ipairs(field.polygonPoints) do
        local x, _, z = env.getWorldTranslation(node)
        if not (finite(x) and finite(z)) then return nil, "FIELD_NOT_FOUND" end
        corners[i] = { x, z }
    end
    local best, bestD
    for id, wp in pairs(graph:getWayPoints() or {}) do
        if type(wp) == "table" and finite(wp.x) and finite(wp.z) and #(wp.out or {}) > 0 and #(wp.incoming or {}) > 0 then
            local d = math.huge
            for i = 1, #corners do
                local a, b = corners[i], corners[i % #corners + 1]
                d = math.min(d, segmentDistance(wp.x, wp.z, a[1], a[2], b[1], b[2]))
            end
            if d <= Bridge.AD_FIELD_EDGE_M and (bestD == nil or d < bestD) then best, bestD = id, d end
        end
    end
    if best == nil then return nil, "AD_NO_WAYPOINT_AT_FIELD" end
    log("ad_marker %s on waypoint %s, %.1f m from field %d's edge", name, tostring(best), bestD, n)
    graph:createMapMarker(best, name)
    return { name = name, graph = graph }
end

-- ad_connect (tractor, marker): the user 2026-10-22, "inspect and configure the existing
-- AutoDrive network using supported mechanisms, preserving its configuration". Only one repair:
-- AutoDrive's own path from the waypoint nearest the vehicle (getClosestWayPoint, out edges)
-- runs to a dead end, and a waypoint that has a route to the marker (ADGraphManager:pathFromTo)
-- lies within AD_LINK_M of that end and continues its direction (within AD_LINK_DEG, its own
-- next edge too): the two joined one way, as AutoDrive's route editor joins them
-- (ADGraphManager:setConnectionBetween(start, end, 1), GraphManager.lua 600-, which broadcasts
-- AutoDriveSetConnectionEvent and AutoDrive saves with the game), after the companion's backup.
-- Live 2026-10-22 (Riverbend Springs): the store yard's lane 11800-11808 stops 5.5 m short of
-- the road it joins (5767). DONE AD_LINKED once AutoDrive's own pathFromTo has the route.
Bridge.AD_LINK_M = 6
Bridge.AD_LINK_DEG = 60

local function heading(a, b) return atan2(b.x - a.x, b.z - a.z) end
local function turnDeg(a, b)
    local d = (b - a + math.pi) % (2 * math.pi) - math.pi
    return math.abs(math.deg(d))
end

function starters.ad_connect(env, spec)
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local graph = adGraph(env)
    local v = spec.tractor
    if graph == nil or type(graph.setConnectionBetween) ~= "function" or type(graph.pathFromTo) ~= "function"
            or type(graph.getWayPointById) ~= "function" or type(v.getClosestWayPoint) ~= "function" then
        return nil, "AD_LINK_UNSUPPORTED"
    end
    local ok, result, why = pcall(function()
        local marker = graph:getMapMarkerById(spec.marker)
        if marker == nil then return nil, "UNKNOWN_MARKER" end
        local target = marker.id
        local from = v:getClosestWayPoint()
        local function routed(id)
            local path = graph:pathFromTo(id, target)
            return path ~= nil and #path >= 1
        end
        if routed(from) then return { linked = true } end
        local wps = graph:getWayPoints()
        local seen, prev, at = {}, nil, wps[from]
        while at ~= nil and #(at.out or {}) > 0 and not seen[at.id] do
            seen[at.id] = true
            prev, at = at, wps[at.out[1]]
        end
        if at == nil or prev == nil or #(at.out or {}) > 0 then return nil, "AD_NO_DEAD_END" end
        local lane = heading(prev, at)
        local best, bestD
        for id, wp in pairs(wps) do
            if id ~= at.id and finite(wp.x) and finite(wp.z) and #(wp.out or {}) > 0 then
                local d = math.sqrt((wp.x - at.x) ^ 2 + (wp.z - at.z) ^ 2)
                local onward = wps[wp.out[1]]
                if d <= Bridge.AD_LINK_M and (bestD == nil or d < bestD) and onward ~= nil
                        and turnDeg(lane, heading(at, wp)) <= Bridge.AD_LINK_DEG
                        and turnDeg(lane, heading(wp, onward)) <= Bridge.AD_LINK_DEG and routed(id) then
                    best, bestD = wp, d
                end
            end
        end
        if best == nil then return nil, "AD_NO_LINK_NEAR" end
        log("ad_connect: AutoDrive's lane from waypoint %s ends at %s; joined to %s (%.1f m on, routed to %s)",
            tostring(from), tostring(at.id), tostring(best.id), bestD, tostring(marker.name))
        graph:setConnectionBetween(at, best, 1)
        return { from = from, target = target, graph = graph }
    end)
    if not ok then return nil, "AD_LINK_ERROR" end
    if result == nil then return nil, why end
    return result
end

function steppers.ad_connect(_env, task)
    if task.linked then return "DONE", "AD_ROUTED" end
    local okR, path = pcall(function() return task.graph:pathFromTo(task.from, task.target) end)
    if okR and path ~= nil and #path >= 1 then return "DONE", "AD_LINKED" end
    if task.elapsed > Bridge.AD_MARKER_TIMEOUT_MS then return "FAILED", "AD_LINK_NOT_OBSERVED" end
    return nil
end

function aborts.ad_connect() end

function steppers.ad_marker(_env, task)
    if task.existed then return "DONE", "MARKER_EXISTS" end
    if task.graph:getMapMarkerByName(task.name) ~= nil then return "DONE", "MARKER_CREATED" end
    if task.elapsed > Bridge.AD_MARKER_TIMEOUT_MS then return "FAILED", "MARKER_NOT_OBSERVED" end
    return nil
end

function aborts.ad_marker() end

-- ------------------------------------------------------------ AutoDrive sell / unload
-- The best paid sell point for a fill type and the AutoDrive marker at it, as AutoDrive's
-- own "automatic unload target" finds them (UnloadAtMode.lua getNextTask:
-- ADTriggerManager:getHighestPayingSellStation(fillType) (TriggerManager.lua 377-394),
-- getMarkerAtStation(station, vehicle) 444- (a map marker's waypoint on the path over the
-- trigger, else -1), ADStateModule:setFirstMarkerByWayPointId 886-893 (the marker whose
-- waypoint it is)). AutoDrive's globals come in as its own function environment, as for
-- ad_marker (adGraph). Returns the marker id (GetAvailableDestinations' key) or nil, why.
local function adManagers(env)
    local AD = (Bridge.Observe and Bridge.Observe.autoDrive and Bridge.Observe.autoDrive(env)) or env.AutoDrive
    if type(AD) ~= "table" or type(AD.GetAvailableDestinations) ~= "function" or type(env.getfenv) ~= "function" then
        return nil
    end
    local ok, adEnv = pcall(env.getfenv, AD.GetAvailableDestinations)
    if not ok or type(adEnv) ~= "table" or type(adEnv.ADTriggerManager) ~= "table"
            or type(adEnv.ADGraphManager) ~= "table" then
        return nil
    end
    return AD, adEnv.ADTriggerManager, adEnv.ADGraphManager
end

function Bridge.bestSellMarker(env, v, fillType)
    local AD, triggers, graph = adManagers(env)
    if AD == nil then return nil, "AD_SELL_UNSUPPORTED" end
    local ok, marker, why = pcall(function()
        local station = triggers:getHighestPayingSellStation(fillType)
        if station == nil then return nil, "NO_SELL_POINT" end
        local wp = triggers:getMarkerAtStation(station, v)
        if wp == nil or wp < 0 then return nil, "AD_NO_MARKER_AT_SELL_POINT" end
        for id, m in pairs(graph:getMapMarkers()) do
            if m.id == wp then return id end
        end
        return nil, "AD_NO_MARKER_AT_SELL_POINT"
    end)
    if not ok then return nil, "AD_SELL_ERROR" end
    return marker, why
end

-- The fill type a field's crop is harvested as (FieldState at GIANTS' field position, as
-- Observe samples it; g_fruitTypeManager:getFillTypeIndexByFruitTypeIndex, as
-- SowingMachine:setSeedIndex 405 uses it).
local function fieldFillType(env, n)
    for _, f in ipairs(env.g_fieldManager.fields or {}) do
        local id = (type(f.getId) == "function" and f:getId()) or f.fieldId
        if id == n then
            local state = env.FieldState.new()
            state:update(f.posX, f.posZ)
            if state.fruitTypeIndex == nil or state.fruitTypeIndex == env.FruitType.UNKNOWN then return nil end
            return env.g_fruitTypeManager:getFillTypeIndexByFruitTypeIndex(state.fruitTypeIndex)
        end
    end
    return nil
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

-- The farm's own silo for a fill type and the AutoDrive marker at it: the silo station as the
-- store delivery picks it (GIANTS' Deliver job's unloading stations, deliverStation direction
-- -1), then getMarkerAtStation as for selling. Returns the marker id or nil, why.
function Bridge.storeMarker(env, v, fillType, farmId)
    local AD, triggers, graph = adManagers(env)
    if AD == nil then return nil, "AD_SELL_UNSUPPORTED" end
    local ok, marker, why = pcall(function()
        local job = env.g_currentMission.aiJobTypeManager:createJob(env.AIJobType.DELIVER)
        if job == nil then return nil, "NO_DELIVER_JOB" end
        local station = Bridge.deliverStation(env, { direction = -1, farmId = farmId }, job, fillType, v)
        if station == nil then return nil, "NO_FARM_SILO" end
        local wp = triggers:getMarkerAtStation(station, v)
        if wp == nil or wp < 0 then return nil, "AD_NO_MARKER_AT_SILO" end
        for id, m in pairs(graph:getMapMarkers()) do
            if m.id == wp then return id end
        end
        return nil, "AD_NO_MARKER_AT_SILO"
    end)
    if not ok then return nil, "AD_STORE_ERROR" end
    return marker, why
end

-- ad_unload (tractor, marker, field, direction): AutoDrive's own combine unloader
-- (CombineUnloaderMode, MODE_UNLOAD 5, Specialization.lua:212) for the rig: it waits at
-- `marker` (the field's), is called by AutoDrive's harvest manager to the combines that
-- need it (HarvestManager.lua 239-300, GIANTS helpers included), and empties its trailer at
-- the best paid sell point for the field's crop. AutoDrive:StartDriving(v, marker, unload)
-- (ExternalInterface.lua:114-161) after setMode. DONE UNLOADING once AutoDrive runs it:
-- handed off (it goes on until stopped).
function starters.ad_unload(env, spec)
    local v = spec.tractor
    local denied = idle(env, v, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local AD = adManagers(env)
    if v.ad == nil or AD == nil then return nil, "NO_AUTODRIVE" end
    if v.ad.stateModule:getStartHelper() ~= false then return nil, "AD_START_HELPER_ON" end
    if type(spec.marker) ~= "number" or AD:GetAvailableDestinations()[spec.marker] == nil then return nil, "UNKNOWN_MARKER" end
    local ok, fillType = pcall(fieldFillType, env, spec.field)
    if not ok or fillType == nil then return nil, "FIELD_CROP_UNKNOWN" end
    -- Where a full trailer goes, as the harvest's destination (the user, 2026-10-31: unloading
    -- while harvesting at speed, whatever the grain is for): the best paid sell point (1), the
    -- farm's silo (-1), or parked at the field's own marker with the grain kept (0).
    local unload, why
    if spec.direction == 0 then unload = spec.marker
    elseif spec.direction == -1 then unload, why = Bridge.storeMarker(env, v, fillType, spec.farmId)
    else unload, why = Bridge.bestSellMarker(env, v, fillType) end
    if unload == nil then return nil, why end
    -- Handed on to AutoDrive: it keeps Farmhand's cap while it runs (Bridge.giveBackAdSpeed after).
    Bridge.handedSpeed[v] = Bridge.handedSpeed[v] or { saved = Bridge.capAdSpeed(v), seen = false }
    v.ad.stateModule:setMode(AD.MODE_UNLOAD)
    AD:StartDriving(v, spec.marker, unload)
    log("ad_unload: waiting at marker %s, unloading at marker %s", tostring(spec.marker), tostring(unload))
    return { seen = false }
end

function steppers.ad_unload(_env, task)
    local status, code = stillOurs(task.spec)
    if status ~= nil then return status, code end
    local v = task.spec.tractor
    if v.ad.stateModule:isActive() == true then
        Bridge.handed[v] = "drive"
        return "DONE", "UNLOADING"
    end
    if task.elapsed > Bridge.START_TIMEOUT_MS then return "FAILED", "AD_NOT_STARTED" end
    return nil
end

aborts.ad_unload = function(_env, task)
    local v = task.spec.tractor
    if v.ad.stateModule:isActive() == true then v:stopAutoDrive() end
    Bridge.giveBackAdSpeed(v)
end

-- ad_sell (tractor): the rig's cargo delivered by AutoDrive (MODE_DELIVERTO: UnloadAtMode
-- drives to the first marker, unloads there, parks; UnloadAtMode.lua 1-80) to the best paid
-- sell point for what it carries; the drive as `drive` (AutoDrive ends it). The companion
-- checks the cargo went and the money came.
function starters.ad_sell(env, spec)
    local v = spec.tractor
    local denied = idle(env, v, spec.farmId)
    if denied ~= nil then return nil, denied end
    if spec.owned() ~= true then return nil, "RESERVATION_LOST" end
    local AD = adManagers(env)
    if v.ad == nil or AD == nil then return nil, "NO_AUTODRIVE" end
    if v.ad.stateModule:getStartHelper() ~= false then return nil, "AD_START_HELPER_ON" end
    local ok, fillType = pcall(cargoFillType, v)
    if not ok or fillType == nil then return nil, "NOTHING_TO_SELL" end
    local marker, why = Bridge.bestSellMarker(env, v, fillType)
    if marker == nil then return nil, why end
    local saved = Bridge.capAdSpeed(v)
    v.ad.stateModule:setMode(AD.MODE_DELIVERTO)
    AD:StartDriving(v, marker, marker)
    log("ad_sell: to marker %s", tostring(marker))
    return { seen = false, onEnd = function() Bridge.restoreAdSpeed(v, saved) end }
end

steppers.ad_sell = steppers.drive
aborts.ad_sell = aborts.drive

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
    local direction = (spec.direction == 1 and 1) or (spec.direction == -1 and -1) or nil
    if direction == nil then return nil, "INVALID_DIRECTION" end
    local foldable = v.spec_foldable
    if foldable == nil or next(foldable.foldingParts or {}) == nil then return nil, "NOT_FOLDABLE" end
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
            if not store and selling then
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
-- user 2026-10-21: the combine was full and its helper would not work on; AutoDrive's
-- unloader serves only a combine whose helper runs, HarvestManager.lua update).
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
-- GIANTS' field-work job, started like GOTO (Hitch.startStaging; AutoDrive
-- Specialization.lua:1518-1527): AIJobTypeManager registers "FIELDWORK" ->
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
        local names, sum = {}, 0
        for k in pairs(a or {}) do names[k] = true end
        for k in pairs(b or {}) do names[k] = true end
        for k in pairs(names) do sum = sum + math.abs(((b or {})[k] or 0) - ((a or {})[k] or 0)) end
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
        if not task.sampled and task.elapsed > 60000 then
            task.sampled = true
            Bridge.logFieldWork(env, v, "at 60 s")
        end
        if task.fromX == nil or math.sqrt((x - task.fromX) ^ 2 + (z - task.fromZ) ^ 2) >= Bridge.NAVIGATE_PROGRESS_M then
            task.fromX, task.fromZ, task.fromAt = x, z, task.elapsed
        elseif task.elapsed - task.fromAt > Bridge.BLOCKED_START_MS and agentBlocked(env, v) then
            -- Unmoved with GIANTS' agent BLOCKED on its way: it waits for an obstacle it knows,
            -- and a parked vehicle never leaves (live 5eb1005df58446f7-1: the MT600 22 m from the
            -- GMC left on the road, 8 min); the companion moves what stands near it.
            logAgent(env, v, "fieldwork blocked on its way")
            return "FAILED", "FIELDWORK_BLOCKED"
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
            if task.sinceCheck >= Bridge.FIELDWORK_CHECK_MS then
                task.sinceCheck = 0
                pcall(Bridge.Observe.refreshField, env, task.spec.field)
                local now = (Bridge.Observe.fieldCache or {})[task.spec.field]
                local changed = Bridge.changedArea(task.before, now and now.record)
                if changed ~= nil and changed >= task.spec.area then
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
    local ended, endCode = settle(task, task.areaReached and "AREA_REACHED" or Bridge.aiStops[v] or "AI_STOPPED")
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
-- Farmhand's pay, the player's choice (the user, 2026-10-30): GIANTS' helper wage and
-- AutoDrive's are off for Farmhand's work and Farmhand charges its own instead, through the
-- wage money type GIANTS charges its helpers with (AIJob:updateCost: addMoney(-cost, farm,
-- MoneyType.AI, true), AIJob.lua 111-126). Modes (the `pay` op): 0 none, 1 per hour (amount per
-- in-game working hour), 2 per job (`charge` after each completed request), 3 per day (once
-- each in-game day Farmhand works), 4 the game's own wages (GIANTS' and AutoDrive's, Farmhand
-- charging nothing). Until the companion sends it: per hour at GIANTS' own helper rate
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

local function payAutoDrive(env)
    -- AutoDrive's own setting (Settings.lua 536-545, 1920): driverWages index 1 is 0 %.
    local ad = env.AutoDrive
    if ad ~= nil and type(ad.setSettingState) == "function" then
        pcall(ad.setSettingState, "driverWages", Bridge.pay.mode == Bridge.PAY.GAME and 3 or 1)
    end
end

function starters.pay(env, spec)
    local mode = spec.direction
    if not (finite(mode) and mode >= 0 and mode <= 4 and mode % 1 == 0) or not finite(spec.area) or spec.area < 0 then
        return nil, "INVALID_PAY"
    end
    Bridge.pay.mode, Bridge.pay.amount = mode, spec.area > 0 and spec.area or nil
    payAutoDrive(env)
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
    -- A handed-off job of this vehicle: GIANTS' helper ended as the player's stop does,
    -- AutoDrive by its own stop (as aborts.drive).
    local handed = spec.tractor ~= nil and Bridge.handed[spec.tractor] or nil
    if handed == "fieldwork" and spec.tractor:getIsAIActive() == true then
        Bridge.Hitch.cancelStaging(env, spec.tractor)
    elseif handed == "drive" and spec.tractor.ad.stateModule:isActive() == true then
        spec.tractor:stopAutoDrive()
    end
    if spec.tractor ~= nil then
        Bridge.handed[spec.tractor] = nil
        Bridge.giveBackAdSpeed(spec.tractor)
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
        or (task.handed == "drive" and v.ad.stateModule:isActive() ~= false) or (target ~= nil
        and ((target.op == "drive" and v.ad.stateModule:isActive() ~= false)
        or ((target.op == "stage" or target.op == "navigate" or target.op == "fieldwork" or target.op == "follow"
             or target.op == "deliver")
            and target.seen and v:getIsAIActive() ~= false)))
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
                       overload = true, alongside = true, backoff = true }
Bridge.NO_VEHICLE = { buy = true, save = true, block = true, open_gates = true, ad_marker = true, contract = true,
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
    if self.active ~= nil then
        Bridge.abort(self.env, self.active.task, code)
        self:result(self.session, self.active.id, self.active.task.op, preempted(self.active.task), code)
        for _, joined in ipairs(self.active.joined or {}) do
            self:result(self.session, joined, "stop", "FAILED", code)
        end
        self.active = nil
    end
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

local ATTRS = { tractor = "tractor", wagon = "wagon", marker = "marker", x = "x", z = "z", yaw = "yaw",
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
    if self.active ~= nil then
        -- Only a stop may pre-empt the running command: one for its vehicle, or for the rig its
        -- wagon hangs on (overload runs on the combine, into the rig's trailer: the stop the
        -- companion sends for that rig's tractor must end it).
        local running = self.active.task.spec
        local wagon = running.wagon
        local ofRig = wagon ~= nil and spec.tractor ~= nil and (wagon.rootVehicle or wagon) == (spec.tractor.rootVehicle or spec.tractor)
        if cmd.op ~= "stop" or (running.tractor ~= spec.tractor and not ofRig) then
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
    self.screenPollMs = (self.screenPollMs or 0) + dt
    if self.screenPollMs >= 1000 then
        self.screenPollMs = 0
        if self.dir ~= nil and type(self.env.fileExists) == "function" then
            local reload = self.dir .. ".fhReload"
            if self.env.fileExists(reload) then
                if not self.reloadMarkerSeen and self.active == nil and self.env.FarmhandProbe ~= nil then
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
    Bridge.keepHandedAwake()
    if Bridge.lastDetach ~= nil then Bridge.lastDetach.age = Bridge.lastDetach.age + dt end
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
    pcall(Bridge.payTick, self.env, dt, self.active ~= nil or next(Bridge.handed) ~= nil,
          self.env.g_currentMission:getFarmId())
    -- Rigs ghosted past buildings (Hitch.ghost) that no motion drives any more: their
    -- collision with buildings restored once each stands clear of them.
    self.ghostTimer = (self.ghostTimer or 0) + dt
    if self.ghostTimer >= 1000 and next(Bridge.Hitch.ghosted or {}) ~= nil then
        self.ghostTimer = 0
        local active = self.active
        pcall(Bridge.Hitch.releaseGhosts, self.env, function(tractor)
            return active ~= nil and active.task ~= nil and active.task.spec ~= nil and active.task.spec.tractor == tractor
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
    CONNECTED = "Farmhand: companion connected",
    TELEPORTED = "Farmhand: the rig could not move, so it was teleported to open ground nearby",
    GHOSTED = "Farmhand: passing through scenery beside the rig (your setting); its collision returns once clear",
    ACCEPTED = "Farmhand: fetching a tractor and the implement",
    BUSY = "Farmhand: already working on a request",
    QUEUED = "Farmhand: busy; your request is next in line",
    ACCEPTED_JOIN_FIELDS = "Farmhand: joining the fields; plowing the strip between them with field creation on",
    COMPLETE_JOIN_FIELDS = "Farmhand: the fields are joined",
    COMPLETE = "Farmhand: tractor and implement delivered",
    ACCEPTED_MOVE = "Farmhand: on my way",
    ACCEPTED_HITCH = "Farmhand: hitching a tractor to the implement",
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
    UNLOADER_STARTED = "Farmhand: an AutoDrive tractor and trailer unloads the combine and sells at the best paid point (this harvest is to be sold)",
    UNLOADER_STOPPED = "Farmhand: AutoDrive stopped unloading the combine; check the unloader",
    NO_UNLOADER = "Farmhand: no AutoDrive tractor with an empty trailer to unload the combine; it waits when full",
    ACCEPTED_UNLOAD_COMBINE = "Farmhand: emptying the combine into a trailer",
    COMPLETE_UNLOAD_COMBINE = "Farmhand: the combine is emptied into the trailer",
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
    UNLOADER = "sending the unloader (AutoDrive)",
    SELL = "driving the load to the sell point",
    STORE = "driving the load to the farm's storage",
    OVERLOAD = "emptying the combine into the trailer",
    ALONGSIDE = "pulling the trailer under the combine's pipe",
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
    LOADED_NEEDS_AUTODRIVE = "a loaded trailer needs an AutoDrive destination (the game's own driver will not start with cargo)",
    LOADED_NO_DRIVER = "the game's helper does not drive a trailer with a belted vehicle on it and AutoDrive has no destination there; drive the truck there yourself, then say continue",
    OVERSPEED = "the tractor went faster than Farmhand's 3 km/h limit, so it stopped; check it before going on",
    ENVELOPE_CONTACT = "something came too close to the tractor or what it carries, so it stopped",
    AD_PATHFINDING_STALLED = "AutoDrive could not find its way onto its road network from there",
    AD_WAITING_ON_OBSTACLE = "AutoDrive waited on something in its way and never moved",
    AD_NOT_MOVING = "AutoDrive was driving but never moved",
    AD_PAUSED = "AutoDrive paused and never moved on",
    AD_NO_ROUTE = "AutoDrive's road network has no route from where the vehicle stands to there",
    AD_ENDED_UNMOVED = "AutoDrive gave up without moving",
    AD_NO_WAYPOINT_AT_PLACE = "AutoDrive has no road near that building",
    AD_PLACE_NOT_FOUND = "no farm building there for AutoDrive",
    NO_STORAGE = "the farm has no storage that takes this crop",
    NOT_ON_DECK = "the tractor is not on the deck; check it",
    DETACH_NOT_ALLOWED = "the game does not allow unhitching it right now",
    NOT_ATTACHED = "that implement is not hitched",
    DETACH_TIMEOUT = "unhitching did not finish; check the tractor",
    DESTINATION_NOT_FOUND = "I do not know that place",
    DESTINATION_AMBIGUOUS = "more than one place has that name",
    AUTODRIVE_UNAVAILABLE = "that destination needs AutoDrive",
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
    NO_RIG_TO_SELL = "no AutoDrive tractor with a loaded trailer",
    NO_UNLOADER = "no free AutoDrive tractor with a trailer that has room",
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
    AD_NO_MARKER_AT_SELL_POINT = "AutoDrive has no destination at the best sell point; add one there in AutoDrive",
    AD_SELL_UNSUPPORTED = "this AutoDrive version does not offer its sell point lookup",
    AD_SELL_ERROR = "AutoDrive's sell point lookup failed",
    FIELD_CROP_UNKNOWN = "the crop on that field could not be read",
    CARGO_NOT_UNLOADED = "the load did not come off at the sell point; check the trailer",
    AD_NO_FIELD_MARKER = "AutoDrive has no destination at that field",
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
    AD_CONFIG_BACKUP_FAILED = "AutoDrive's route network could not be backed up first, so it was not changed",
    AD_LINK_ERROR = "joining AutoDrive's lane to the road failed",
    AD_LINK_NOT_OBSERVED = "AutoDrive did not show the lane joined to the road",
    AD_LINK_UNSUPPORTED = "this AutoDrive version cannot join lanes for Farmhand",
    AD_MARKER_NOT_ON_NETWORK = "that AutoDrive destination is not on AutoDrive's road network",
    AD_MARKER_UNSUPPORTED = "this AutoDrive version cannot add destinations for Farmhand",
    AD_NETWORK_UNREADABLE = "AutoDrive's road network could not be read",
    AD_NOT_STARTED = "AutoDrive did not start driving",
    AD_NO_DEAD_END = "the vehicle is not at the end of an AutoDrive lane",
    AD_NO_LINK_NEAR = "no AutoDrive road is near the lane's end to join it to",
    AD_NO_MARKER_AT_SILO = "AutoDrive has no destination at the farm's silo",
    AD_NO_ROUTED_WAYPOINT_NEAR = "no AutoDrive road with a route to there is near the transport",
    AD_NO_WAYPOINT_AT_FIELD = "AutoDrive has no road near that field",
    AD_START_HELPER_ON = "AutoDrive's start helper is on for that vehicle; turn it off in its AutoDrive menu",
    AD_STOPPED_WITH_ERROR = "AutoDrive stopped with an error",
    AD_STORE_ERROR = "AutoDrive could not set up the trip to the silo",
    AI_ACTIVE = "a helper or AutoDrive is driving it",
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
    INVALID_MARKER_PLACE = "there is no valid spot for an AutoDrive destination there",
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
    MARKER_NOT_OBSERVED = "AutoDrive did not show the new destination",
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
    NO_AUTODRIVE = "AutoDrive is not available for that vehicle",
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
    UNKNOWN_MARKER = "that AutoDrive destination no longer exists",
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
    self.session, self.active, self.pipe, self.dropped = nil, nil, nil, {}
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
