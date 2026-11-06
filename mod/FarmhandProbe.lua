-- DIAGNOSTIC console probe for the HITCH part of AC-001. Production AC-001 runs
-- from the companion (companion/run.py) through mod/FarmhandBridge.lua; this
-- probe drives the very same bridge operations by hand, one attempt at a time,
-- and refuses while a companion session owns the bridge. Nothing happens until
-- the player types one of these console commands (server side only):
--
--   fhHitchList
--       read-only: owned vehicles with name, store category, forage-wagon spec,
--       position, attachment, motor/PTO values and hitch joints.
--   fhHitchProbe <tractorId> <wagonId> [assistRadiusM] [maxVerticalM] [maxSpeedKmh]
--       one bounded attempt, in the companion's order: validate -> plan ->
--       [GIANTS GOTO staging, only from beyond the local work radius] ->
--       [LOCAL_MANEUVER, unless already in the reverse's envelope] ->
--       ALIGN_REVERSE -> exact candidate -> attach -> verify. Ends with exactly
--       one "RESULT PASS|FAILED|UNVERIFIED <reason>" line.
--   fhHitchStop
--       abort the running attempt (brake, cruise off, cancel its own GOTO).
--
-- Output goes to log.txt prefixed "[Farmhand]". Console and listener hooks follow
-- Courseplay ConsoleCommands.lua:40,47 and Farmhand Companion Farmhand.lua:403.

local Probe = {}

Probe.COMMANDS = {
    { "fhHitchList", "Farmhand dev: list owned vehicles for the hitch probe", "consoleList" },
    { "fhRigGeometry", "Farmhand dev: read loaded rig geometry for vehicle ID", "consoleRigGeometry" },
    { "fhHitchProbe", "Farmhand dev: fhHitchProbe <tractorId> <wagonId> [assistRadiusM] [maxVerticalM] [maxSpeedKmh]",
      "consoleProbe" },
    { "fhHitchStop", "Farmhand dev: abort the running hitch probe", "consoleStop" },
    { "fhReload", "Farmhand dev: re-read Farmhand's Lua into the running game (no command running)", "consoleReload" },
}
-- fhReload: the files re-read (by GIANTS' source(), as Gui.lua 1145 loads its scripts), in modDesc order,
-- each module's global kept: its functions and UPPER-case constants replaced in place, state kept.
Probe.DIR = g_currentModDirectory
Probe.RELOAD = { { "FarmhandObserve.lua", "FarmhandObserve" }, { "FarmhandHitch.lua", "FarmhandHitch" },
                 { "FarmhandDrive.lua", "FarmhandDrive" }, { "FarmhandBridge.lua", "FarmhandBridge" },
                 { "FarmhandProbe.lua", "FarmhandProbe" } }

-- The fresh module's functions and constants into the live one; the fresh table then forwards to it
-- (its own functions' upvalue), so one table holds the state.
function Probe.merge(old, new)
    local keys = {}
    for k, v in pairs(new) do
        keys[#keys + 1] = k
        if type(v) == "function" or (type(k) == "string" and k:match("^%u")) then old[k] = v end
    end
    for _, k in ipairs(keys) do new[k] = nil end
    setmetatable(new, { __index = old, __newindex = old })
end

function Probe:consoleReload()
    local env = self.env or getfenv(0)
    local bridge = env.FarmhandBridge
    if (bridge ~= nil and bridge.active ~= nil) or self.task ~= nil then
        return "fhReload: a Farmhand command is running; stop it first"
    end
    local bridgeDir = bridge ~= nil and bridge.dir or nil
    if type(Probe.DIR) ~= "string" or type(bridgeDir) ~= "string"
            or type(env.copyFile) ~= "function" or type(env.source) ~= "function"
            or type(env.fileExists) ~= "function" or type(env.deleteFile) ~= "function"
            or type(env.getTimeSec) ~= "function" then return "fhReload: unavailable" end
    env.FarmhandReloading = true
    local done = {}
    local generation = math.floor(env.getTimeSec() * 1000)
    for index, file in ipairs(Probe.RELOAD) do
        local old = env[file[2]]
        -- The original source path reported success without executing an installed
        -- edit. Use a unique path in the writable settings folder for this read.
        local path = bridgeDir .. "reload_" .. generation .. "_" .. index .. ".lua"
        local copied = pcall(env.copyFile, Probe.DIR .. file[1], path, true)
        if not copied or not env.fileExists(path) then
            env.FarmhandReloading = nil
            return string.format("fhReload: %s copy failed; reloaded %s", file[1], table.concat(done, ", "))
        end
        local ok, err = pcall(env.source, path)
        env.deleteFile(path)
        local new = env[file[2]]
        env[file[2]] = old
        if not ok or type(new) ~= "table" or type(old) ~= "table" then
            env.FarmhandReloading = nil
            return string.format("fhReload: %s failed (%s); reloaded %s", file[1], tostring(err), table.concat(done, ", "))
        end
        if new ~= old then Probe.merge(old, new) end
        if file[2] == "FarmhandObserve" then
            -- The new JSON functions close over a new private ARRAY marker.
            -- Previously cached catalog/field records contain arrays from the
            -- old marker; rebuild these observations on the next ticks.
            old.catalog = nil
            old.fieldCache = {}
        end
        done[#done + 1] = file[1]
    end
    env.FarmhandReloading = nil
    print("[Farmhand] fhReload: reloaded " .. table.concat(done, ", "))
    return "fhReload: reloaded " .. table.concat(done, ", ")
end
-- Mirrors of companion constants (checked by tests/test_lua_probe.py).
Probe.STAGING_DISTANCE_M = 6.0           -- companion/hitch.py STAGING_DISTANCE_M
Probe.REAR_JOINT_MAX_Z = 0.5             -- companion/hitch.py REAR_JOINT_MAX_Z
Probe.FRONT_JOINT_SLACK_M = 0.5          -- companion/hitch.py FRONT_JOINT_SLACK_M
Probe.SIDE_DRAWBAR_M = 2.0               -- companion/hitch.py SIDE_DRAWBAR_M
Probe.ALIGN_REACH_M = 9.0                -- companion/hitch.py ALIGN_REACH_M
Probe.ALIGN_MAX_HEADING_DEG = 30         -- companion/hitch.py ALIGN_MAX_HEADING_RAD
Probe.ALIGN_MAX_LATERAL_M = 2.0          -- companion/hitch.py ALIGN_MAX_LATERAL_M
Probe.LOCAL_WORK_RADIUS_M = 25.0         -- companion/hitch.py LOCAL_WORK_RADIUS_M
Probe.PLAN_TOLERANCE_M = 0.25            -- executor WAGON_MOVED_TOLERANCE_M

local function log(fmt, ...)
    local ok, msg = pcall(string.format, fmt, ...)
    print("[Farmhand] " .. (ok and msg or tostring(fmt)))
end

local function finite(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

-- --------------------------------------------------------- plan (dev only)
-- DEVELOPMENT-ONLY copy of companion/hitch.py plan_hitch, which stays the single
-- production authority. It exists only because the probe runs without the
-- companion; tests/test_lua_probe.py requires identical answers on random inputs.

local function ptoConnects(outputs, inputs, null)
    if outputs == null or inputs == null then return false end
    if #inputs == 0 then return true end                   -- no input PTO: nothing to connect
    for _, a in ipairs(outputs) do
        for _, b in ipairs(inputs) do
            if a == b then return true end
        end
    end
    return false
end

local function uniqueIndices(joints)
    local seen = {}
    for _, joint in ipairs(joints) do
        if seen[joint.index] then return false end
        seen[joint.index] = true
    end
    return true
end

function Probe.plan(tractor, wagon, stagingDistance, null)
    local root, heading, need = tractor.ai_root, wagon.heading, wagon.pto_torque
    if root == null or not finite(root.x) or not finite(root.z) or not finite(root.yaw)
            or not finite(heading) or not finite(need) or need < 0
            or not uniqueIndices(tractor.output_joints) or not uniqueIndices(wagon.input_joint_poses) then
        return nil
    end
    local best = nil
    -- The way an input is towed (companion/hitch.py plan_hitch towing): the heading, else root -> joint
    -- for a drawbar beside the implement's axis.
    local function towing(inp)
        local dx, dz = inp.x - wagon.x, inp.z - wagon.z
        local along = dx * math.sin(heading) + dz * math.cos(heading)
        local lateral = dx * math.cos(heading) - dz * math.sin(heading)
        if math.abs(lateral) > math.max(math.abs(along), Probe.SIDE_DRAWBAR_M) then
            return (math.atan2 or math.atan)(dx, dz)
        end
        return heading
    end
    for _, out in ipairs(tractor.output_joints) do
        for _, inp in ipairs(wagon.input_joint_poses) do
            -- Never an input behind the wagon's root (companion/hitch.py plan_hitch).
            local yaw = towing(inp)
            local front = (inp.x - wagon.x) * math.sin(yaw) + (inp.z - wagon.z) * math.cos(yaw) >= -Probe.FRONT_JOINT_SLACK_M
            if out.free and inp.active and out.type == inp.type and out.z < Probe.REAR_JOINT_MAX_Z and front
                    and (need == 0 or ptoConnects(out.pto_names, inp.pto_names, null))
                    and (best == nil or out.index < best[1].index
                         or (out.index == best[1].index and inp.index < best[2].index)) then
                best = { out, inp }
            end
        end
    end
    if best == nil then return nil end
    local out, inp = best[1], best[2]
    heading = towing(inp)
    -- Place the AI root so that root + rotate(heading, joint offset) == target.
    local sin, cos = math.sin(heading), math.cos(heading)
    local fx = inp.x - (out.x * cos + out.z * sin)
    local fz = inp.z - (-out.x * sin + out.z * cos)
    return {
        tractorJoint = out.index, wagonJoint = inp.index, jointType = out.type,
        target = { x = inp.x, z = inp.z },
        final = { x = fx, z = fz, yaw = heading },
        staging = { x = fx + stagingDistance * sin, z = fz + stagingDistance * cos, yaw = heading },
    }
end

-- ------------------------------------------------------------- helpers

local function vehicles(env)
    local m = env.g_currentMission
    return (m.vehicleSystem and m.vehicleSystem.vehicles) or m.vehicles or {}
end

-- Fresh records for the pair.
local function observePair(self, tractor, wagon)
    local env, O = self.env, self.Observe
    return O.vehicle(env, tractor), O.vehicle(env, wagon)
end

local function pto(names, null)
    if names == null then return "?" end
    return table.concat(names, "+")
end

local function joints(list, flag, null)
    local parts = {}
    for _, j in ipairs(list) do
        parts[#parts + 1] = string.format("%d:%s:%s:%s", j.index, j.type, tostring(j[flag]), pto(j.pto_names, null))
    end
    return "[" .. table.concat(parts, " ") .. "]"
end

local function fmtPose(p)
    return string.format("x=%.3f z=%.3f yaw=%.4f", p.x, p.z, p.yaw)
end

-- Single exit: exactly one RESULT line per attempt, then the probe is idle and
-- releases the bridge. A running operation is aborted first (its own motion only).
function Probe:finish(status, reason)
    local task, Bridge = self.task, self.Bridge
    if task ~= nil and task.op ~= nil then Bridge.abort(self.env, task.op, reason) end
    if Bridge.owner == self.owner then Bridge.owner = nil end
    Bridge.lastAlign, Bridge.lastManeuver = nil, nil
    if task ~= nil and task.tractor ~= nil then self.Hitch.disarm(task.tractor) end   -- this attempt's watch
    self.task = nil
    log("RESULT %s %s", status, reason)
end

-- ------------------------------------------------------------- commands

function Probe:consoleList()
    local env, O = self.env, self.Observe
    local ok, err = pcall(function()
        local j = env.AttacherJoints
        log("GIANTS MAX_ATTACH_DISTANCE_SQ=%s MAX_ATTACH_ANGLE=%s", tostring(j.MAX_ATTACH_DISTANCE_SQ),
            tostring(j.MAX_ATTACH_ANGLE))
        local farmId = env.g_currentMission:getFarmId()
        for _, v in pairs(vehicles(env)) do
            if v.ownerFarmId == farmId then
                local r = O.vehicle(env, v)
                local item = env.g_storeManager:getItemByXMLFilename(v.configFileName)
                if r ~= nil then
                    local motor = r.motor ~= O.NULL and string.format("%.4f/%.4f", r.motor.peak_torque,
                                                                      r.motor.pto_rpm_ratio) or "?"
                    log("id=%d name=%q store=%s forageWagon=%s x=%.1f z=%.1f attacher=%s implements=%s "
                        .. "motor(peakTorque/ptoRpmRatio)=%s ptoTorque=%s out=%s in=%s",
                        r.id, tostring(v.getFullName and v:getFullName()), tostring(item and item.categoryName),
                        tostring(r.forage_wagon), r.x, r.z, tostring(r.attacher_vehicle_id),
                        O.encode(r.implement_ids), motor, tostring(r.pto_torque),
                        joints(r.output_joints, "free", O.NULL), joints(r.input_joint_poses, "active", O.NULL))
                end
            end
        end
    end)
    if not ok then log("fhHitchList failed: %s", tostring(err)) end
end

-- Read-only geometry capture. The AABBs report loaded physics extent; they do
-- not claim that the empty corners of an angled box are occupied.
function Probe:consoleRigGeometry(vehicleId)
    local env = self.env
    if env == nil or env.g_server == nil then return "fhRigGeometry: server only" end
    local okVehicle, vehicle = pcall(env.NetworkUtil.getObject, tonumber(vehicleId))
    if not okVehicle or vehicle == nil or vehicle.rootNode == nil then
        return "usage: fhRigGeometry <vehicleId> (ID from fhHitchList)"
    end
    local inventory = self.Hitch.physicalInventory(env, vehicle)
    if inventory == nil then return "fhRigGeometry: rig unreadable" end
    log("rig geometry units=%d", #inventory)
    for index, item in ipairs(inventory) do
        local unit = item.unit
        local x, _, z = env.getWorldTranslation(item.rootNode)
        local fx, _, fz = env.localDirectionToWorld(item.rootNode, 0, 0, 1)
        local s = item.size or {}
        log("unit%d id=%s name=%s pose=(%.3f,%.3f,%.4f) size=(%s,%s,%s) bodies=%d shapes=%d",
            index, tostring(env.NetworkUtil.getObjectId(unit)),
            tostring(unit.getFullName and unit:getFullName() or unit.typeName),
            x, z, math.atan2 and math.atan2(fx, fz) or math.atan(fx, fz),
            tostring(s.width), tostring(s.length), tostring(s.height), #item.bodies, #item.shapes)
        for _, body in ipairs(item.bodies) do
            log("unit%d body=%s aabb=(%.3f,%.3f,%.3f,%.3f,%.3f,%.3f)",
                index, tostring(body.node), body.minX, body.maxX, body.minY, body.maxY,
                body.minZ, body.maxZ)
        end
        for _, shape in ipairs(item.shapes) do
            local name = type(env.getName) == "function" and env.getName(shape.node) or "?"
            log("unit%d shape=%s name=%s sphereWorld=(%.3f,%.3f,%.3f; r=%.3f) group=%s geometry=%s",
                index, tostring(shape.node), tostring(name), shape.worldX, shape.worldY, shape.worldZ,
                shape.radius, tostring(shape.group), tostring(shape.usesGeometry))
        end
        local joint = unit.getActiveInputAttacherJoint and unit:getActiveInputAttacherJoint() or nil
        if joint ~= nil and joint.node ~= nil then
            local jx, _, jz = env.getWorldTranslation(joint.node)
            log("unit%d inputJoint=(%.3f,%.3f)", index, jx, jz)
        end
        for wheelIndex, wheel in ipairs(unit.spec_wheels and unit.spec_wheels.wheels or {}) do
            if wheel.repr ~= nil then
                local wx, _, wz = env.getWorldTranslation(wheel.repr)
                log("unit%d wheel%d=(%.3f,%.3f) active=%s", index, wheelIndex, wx, wz,
                    tostring(wheel.isActive))
            end
        end
    end
    return "fhRigGeometry: recorded"
end

-- Idle, stopped, unattached, observed, own farm. Returns nil or a reason.
local function unsafe(r, farmId, null)
    if r.farm_id ~= farmId then return "NOT_YOUR_FARM" end
    if r.entered ~= true and r.entered ~= false then return "OCCUPIED_OR_UNKNOWN" end   -- ride-along allowed
    if r.ai_active ~= false or (r.has_autodrive and r.ad_active ~= false) then return "AI_ACTIVE_OR_UNKNOWN" end
    if not r.attachment_observed or r.attacher_vehicle_id ~= null or #r.implement_ids > 0 then
        return "ATTACHED_OR_UNKNOWN"
    end
    if r.speed_kmh == null or math.abs(r.speed_kmh) >= 1 then return "MOVING_OR_UNKNOWN" end
    return nil
end

function Probe:consoleProbe(tractorId, wagonId, radius, vertical, speed)
    local Bridge = self.Bridge
    if self.task ~= nil or Bridge.owner ~= nil then
        log("fhHitchProbe: BUSY - one attempt at a time and never during a companion session")
        return
    end
    local env, O = self.env, self.Observe
    if env.g_server == nil then log("fhHitchProbe: server only") return end
    local args = { maxDistanceM = math.min(Probe.STAGING_DISTANCE_M + 2, self.Hitch.LIMITS.maxDistanceM),
                   assistRadiusM = tonumber(radius), maxVerticalM = tonumber(vertical),
                   maxSpeedKmh = tonumber(speed) }
    local limits = self.Hitch.limits(args)
    local function getObject(id)
        local ok, v = pcall(env.NetworkUtil.getObject, tonumber(id))
        return ok and v or nil
    end
    local tractor, wagon = getObject(tractorId), getObject(wagonId)
    if tractor == nil or wagon == nil or tractor == wagon or limits == nil
            or (radius ~= nil and args.assistRadiusM == nil) or (vertical ~= nil and args.maxVerticalM == nil)
            or (speed ~= nil and args.maxSpeedKmh == nil) then
        log("usage: fhHitchProbe <tractorId> <wagonId> [assistRadiusM<=%.2f] [maxVerticalM<=%.2f] "
            .. "[maxSpeedKmh %.0f-%.0f] (IDs from fhHitchList)", self.Hitch.LIMITS.maxAssistRadiusM,
            self.Hitch.LIMITS.maxVerticalM, self.Hitch.LIMITS.minSpeedKmh, self.Hitch.LIMITS.maxSpeedKmh)
        return
    end
    self.attempt = (self.attempt or 0) + 1
    self.owner = "probe:" .. self.attempt
    Bridge.owner = self.owner
    local owner = self.owner
    self.task = { phase = "validate" }
    local ok, err = pcall(function()
        local farmId = env.g_currentMission:getFarmId()
        local t, w = observePair(self, tractor, wagon)
        if t == nil or w == nil then return self:finish("FAILED", "NOT_OBSERVABLE") end
        log("tractor %s", O.encode(t))
        log("wagon %s", O.encode(w))
        local denied = unsafe(t, farmId, O.NULL) or unsafe(w, farmId, O.NULL)
        if denied ~= nil then return self:finish("FAILED", denied) end
        if not w.forage_wagon then return self:finish("FAILED", "NOT_A_FORAGE_WAGON") end
        local plan = Probe.plan(t, w, Probe.STAGING_DISTANCE_M, O.NULL)
        if plan == nil then return self:finish("FAILED", "NO_HITCH_GEOMETRY") end
        log("plan joints tractor=%d wagon=%d type=%s target x=%.3f z=%.3f", plan.tractorJoint,
            plan.wagonJoint, plan.jointType, plan.target.x, plan.target.z)
        log("plan final %s staging %s", fmtPose(plan.final), fmtPose(plan.staging))
        log("limits assistRadiusM=%s maxVerticalM=%.2f maxSpeedKmh=%.1f maxDistanceM=%.1f",
            tostring(args.assistRadiusM or "GIANTS default"), limits.maxVerticalM, limits.maxSpeedKmh,
            limits.maxDistanceM)
        local task = { phase = "validate", tractor = tractor, wagon = wagon, farmId = farmId, plan = plan,
                       args = args, owned = function() return Bridge.owner == owner end }
        self.task = task
        self:advance(task)
    end)
    if not ok then
        log("fhHitchProbe error: %s", tostring(err))
        self:finish("UNVERIFIED", "UNKNOWN_OBSERVATION")
    end
end

function Probe:consoleStop()
    if self.task == nil then log("fhHitchStop: nothing running") return end
    self:finish("FAILED", "STOPPED_BY_USER")
end

-- ---------------------------------------------------------- per frame

-- Start one bridge operation for the attempt, or finish with its refusal.
function Probe:startOp(task, spec)
    spec.tractor, spec.wagon, spec.farmId, spec.owned = task.tractor, task.wagon, task.farmId, task.owned
    local op, code, status = self.Bridge.start(self.env, spec)
    if op == nil then return self:finish(status, code) end
    task.op = op
end

-- As the companion (companion/hitch.py align_problem): a GOTO end is never
-- arrival; the observed planned joints must lie where the bounded reverse can
-- take over. Returns nil (inside) or the reason, and the gap for the reverse.
local function jointProblem(self, task)
    local env, plan = self.env, task.plan
    local node = task.tractor:getAIRootNode()
    local x, _, z = env.getWorldTranslation(node)
    local dx, _, dz = env.localDirectionToWorld(node, 0, 0, 1)
    local yaw = env.MathUtil.getYRotationFromDirection(dx, dz)
    local joint = task.tractor:getAttacherJoints()[plan.tractorJoint]
    local eye = task.wagon:getInputAttacherJoints()[plan.wagonJoint]
    local tx, _, tz = env.getWorldTranslation(joint.jointTransform)
    local wx, _, wz = env.getWorldTranslation(eye.node)
    local fx, fz = math.sin(plan.final.yaw), math.cos(plan.final.yaw)
    local ahead = (tx - wx) * fx + (tz - wz) * fz
    local lateral = (tx - wx) * fz - (tz - wz) * fx
    local gap = math.sqrt((tx - wx) ^ 2 + (tz - wz) ^ 2)
    local heading = math.deg(math.abs((yaw - plan.final.yaw + math.pi) % (2 * math.pi) - math.pi))
    log("%s end root %s joint gap=%.2f ahead=%.2f lateral=%.2f heading=%.1f deg", task.phase,
        fmtPose({ x = x, z = z, yaw = yaw }), gap, ahead, lateral, heading)
    if gap > Probe.ALIGN_REACH_M then return "TRAVEL_ENDED_TOO_FAR", gap end
    if ahead <= 0 then return "ALIGN_WRONG_SIDE", gap end
    if heading > Probe.ALIGN_MAX_HEADING_DEG then return "ALIGN_HEADING_OUT_OF_RANGE", gap end
    if math.abs(lateral) > Probe.ALIGN_MAX_LATERAL_M then return "ALIGN_LATERAL_OUT_OF_RANGE", gap end
    return nil, gap
end

-- The wagon and joints must still be exactly what was planned.
local function planUnchanged(self, task)
    local t, w = observePair(self, task.tractor, task.wagon)
    local fresh = t and w and Probe.plan(t, w, Probe.STAGING_DISTANCE_M, self.Observe.NULL)
    local plan = task.plan
    return fresh ~= nil and fresh.tractorJoint == plan.tractorJoint and fresh.wagonJoint == plan.wagonJoint
        and math.sqrt((fresh.target.x - plan.target.x) ^ 2 + (fresh.target.z - plan.target.z) ^ 2)
            <= Probe.PLAN_TOLERANCE_M
end

-- PASS only from a fresh observation of exactly this pair on the planned tractor
-- joint, after Hitch.attachState confirmed GIANTS' own joint indices.
local function verify(self, task)
    local plan, null = task.plan, self.Observe.NULL
    local implement = task.tractor:getImplementByObject(task.wagon)
    local t, w = observePair(self, task.tractor, task.wagon)
    local used = nil
    for _, j in ipairs(t.output_joints) do
        if j.index == plan.tractorJoint then used = j end
    end
    log("verify joints=%s/%s implements=%s attacher=%s plannedJointFree=%s",
        tostring(implement and implement.jointDescIndex), tostring(implement and implement.inputJointDescIndex),
        self.Observe.encode(t.implement_ids), tostring(w.attacher_vehicle_id), tostring(used and used.free))
    if #t.implement_ids == 1 and t.implement_ids[1] == w.id
            and w.attacher_vehicle_id ~= null and w.attacher_vehicle_id == t.id
            and used ~= nil and used.free == false then
        return self:finish("PASS", "EXACT_PAIR_ATTACHED")
    end
    self:finish("UNVERIFIED", "ATTACHMENT_MISMATCH")
end

-- The next physical step from a fresh observation, as companion/executor.py:
-- GOTO only from beyond the work radius (and once), the local maneuver only
-- outside the reverse's envelope (and once), then the bounded reverse.
function Probe:advance(task)
    local problem, gap = jointProblem(self, task)
    local plan = task.plan
    if gap > Probe.LOCAL_WORK_RADIUS_M then
        if task.phase ~= "validate" then return self:finish("FAILED", "TRAVEL_ENDED_TOO_FAR") end
        task.phase = "stage"
        return self:startOp(task, { op = "stage", x = plan.staging.x, z = plan.staging.z, yaw = plan.staging.yaw })
    end
    if problem ~= nil then
        if task.phase == "maneuver" then return self:finish("FAILED", problem) end
        task.phase = "maneuver"
        return self:startOp(task, { op = "maneuver", tractorJoint = plan.tractorJoint, wagonJoint = plan.wagonJoint })
    end
    task.phase = "align"
    local spec = { op = "align", tractorJoint = plan.tractorJoint, wagonJoint = plan.wagonJoint }
    for k, v in pairs(task.args) do spec[k] = v end
    -- The reverse is sized to the gap actually left, as the companion does.
    spec.maxDistanceM = math.min(self.Hitch.LIMITS.maxDistanceM, math.max(Probe.STAGING_DISTANCE_M, gap) + 2)
    self:startOp(task, spec)
    if self.task == task then
        local s = task.op.session
        log("reverse started attachDistanceSq=%.4f maxVerticalM=%.2f maxSpeedKmh=%.1f",
            s.attachDistanceSq, s.maxVerticalM, s.maxSpeedKmh)
    end
end

local function step(self, task, dt)
    local status, code = self.Bridge.step(self.env, task.op, dt)
    if status == nil then return end
    local s = task.op.session
    if s ~= nil and task.phase == "align" then
        log("reverse result=%s elapsedMs=%d gap=%.3f bestGap=%.3f dy=%s", code, s.elapsed, s.lastGap,
            s.bestGap, tostring(s.lastDy))
    end
    if status ~= "DONE" then return self:finish(status, code) end
    task.op = nil
    if task.phase == "stage" or task.phase == "maneuver" then
        if not planUnchanged(self, task) then return self:finish("FAILED", "PLAN_CHANGED") end
        self:advance(task)
    elseif task.phase == "align" then
        task.phase = "attach"
        log("attach: requested")
        self:startOp(task, { op = "attach", tractorJoint = task.plan.tractorJoint, wagonJoint = task.plan.wagonJoint })
    elseif task.phase == "attach" then
        log("attach state: %s", code)
        return verify(self, task)
    end
end

function Probe:update(dt)
    local env, bridge = self.env, self.Bridge
    if env ~= nil and bridge ~= nil and type(bridge.dir) == "string" then
        self.motionMarkerMs = (self.motionMarkerMs or 0) + dt
        if self.motionMarkerMs >= 500 then
            self.motionMarkerMs = 0
            local hot = bridge.dir .. "probe_reload.flag"
            if env.fileExists(hot) and self.motion == nil and self.actuator == nil
                    and self.terraPulse == nil and self.task == nil and bridge.active == nil then
                env.deleteFile(hot)
                local ok, result = pcall(Probe.consoleReload, self)
                if not ok then log("probe reload UNVERIFIED %s", tostring(result)) end
            end
            local inventory = bridge.dir .. "machine_inventory.flag"
            if env.fileExists(inventory) then
                env.deleteFile(inventory)
                local ok, err = pcall(Probe.logMachineInventory, self)
                if not ok then log("machine inventory UNVERIFIED %s", tostring(err)) end
            end
            local specs = bridge.dir .. "spec_inventory.flag"
            if env.fileExists(specs) then
                env.deleteFile(specs)
                local ok, err = pcall(Probe.logSpecInventory, self)
                if not ok then log("spec inventory UNVERIFIED %s", tostring(err)) end
            end
            local treeInventory = bridge.dir .. "tree_inventory.flag"
            if env.fileExists(treeInventory) then
                env.deleteFile(treeInventory)
                local ok, err = pcall(Probe.logTreeInventory, self)
                if not ok then log("tree inventory UNVERIFIED %s", tostring(err)) end
            end
            local treeHead = bridge.dir .. "tree_head.flag"
            if env.fileExists(treeHead) then
                env.deleteFile(treeHead)
                local ok, err = pcall(Probe.logTreeHeadReach, self)
                if not ok then log("tree head UNVERIFIED %s", tostring(err)) end
            end
            local treeLowest = bridge.dir .. "tree_lowest.flag"
            if env.fileExists(treeLowest) then
                env.deleteFile(treeLowest)
                local ok, err = pcall(Probe.logLowestCuttableTree, self)
                if not ok then log("tree lowest UNVERIFIED %s", tostring(err)) end
            end
            local treeTeleport = bridge.dir .. "tree_teleport_preview.flag"
            if env.fileExists(treeTeleport) then
                env.deleteFile(treeTeleport)
                local ok, err = pcall(Probe.previewTreeTeleport, self)
                if not ok then log("tree teleport preview UNVERIFIED %s", tostring(err)) end
            end
            local treeExecute = bridge.dir .. "tree_teleport_execute.flag"
            if env.fileExists(treeExecute) then
                env.deleteFile(treeExecute)
                local ok, err = pcall(Probe.executeTreeTeleport, self)
                if not ok then log("tree teleport UNVERIFIED %s", tostring(err)) end
            end
            local treeAlign = bridge.dir .. "tree_native_align.flag"
            if env.fileExists(treeAlign) then
                env.deleteFile(treeAlign)
                local ok, err = pcall(Probe.startNativeTreeAlign, self)
                if not ok then log("tree native align UNVERIFIED %s", tostring(err)) end
            end
            local treeAlignStop = bridge.dir .. "tree_native_align_stop.flag"
            if env.fileExists(treeAlignStop) then
                env.deleteFile(treeAlignStop)
                if self.treeAlign ~= nil then self:finishNativeTreeAlign("STOPPED") end
            end
            local treeCut = bridge.dir .. "tree_cut_once.flag"
            if env.fileExists(treeCut) then
                env.deleteFile(treeCut)
                local ok, err = pcall(Probe.startOneTreeCut, self)
                if not ok then log("tree cut UNVERIFIED %s", tostring(err)) end
            end
            local treeProcess = bridge.dir .. "tree_process_once.flag"
            if env.fileExists(treeProcess) then
                env.deleteFile(treeProcess)
                local ok, err = pcall(Probe.startOneTreeProcess, self)
                if not ok then log("tree process UNVERIFIED %s", tostring(err)) end
            end
            local woodAudit = bridge.dir .. "tree_wood_audit.flag"
            if env.fileExists(woodAudit) then
                env.deleteFile(woodAudit)
                local ok, err = pcall(Probe.auditNearbyWood, self)
                if not ok then log("wood audit UNVERIFIED %s", tostring(err)) end
            end
            local stackTrial = bridge.dir .. "tree_stack_trial.flag"
            if env.fileExists(stackTrial) then
                env.deleteFile(stackTrial)
                local ok, err = pcall(Probe.startTreeStackTrial, self)
                if not ok then log("tree stack UNVERIFIED %s", tostring(err)) end
            end
            local turnReference = bridge.dir .. "turn_reference.flag"
            if env.fileExists(turnReference) then
                env.deleteFile(turnReference)
                local ok, err = pcall(Probe.logTurnReferences, self)
                if not ok then log("turn reference UNVERIFIED %s", tostring(err)) end
            end
            local physicalPair = bridge.dir .. "physical_pair.flag"
            if env.fileExists(physicalPair) then
                env.deleteFile(physicalPair)
                for _, id in ipairs({615, 623, 630}) do
                    local ok, err = pcall(Probe.consoleRigGeometry, self, tostring(id))
                    if not ok then log("physical pair UNVERIFIED id=%d %s", id, tostring(err)) end
                    local unit = env.NetworkUtil.getObject(id)
                    if unit ~= nil then
                        local foot = self.Hitch.routeFootprint(env, unit)
                        local contactOk, contacts = pcall(self.Hitch.rigContacts, env, unit, foot)
                        log("physical pair contacts id=%d %s", id,
                            contactOk and tostring(contacts) or "UNREADABLE")
                        if id == 615 then
                            local x,_,z=env.getWorldTranslation(unit.rootNode)
                            local fx,_,fz=env.localDirectionToWorld(unit.rootNode,0,0,1)
                            local yaw=(math.atan2 or math.atan)(fx,fz)
                            for _, offset in ipairs({0,1,2,-1,-2}) do
                                local px,pz=x+fx*offset,z+fz*offset
                                local okClear,clear,reason=pcall(self.Hitch.routePoseClear,
                                    env,foot,px,pz,yaw,0,offset<0)
                                log("physical pair candidate id=%d step=%d xyz=(%.3f,%.3f) clear=%s reason=%s",
                                    id,offset,px,pz,tostring(okClear and clear),
                                    tostring(okClear and reason or "UNREADABLE"))
                            end
                            local center=unit.spec_wheels and unit.spec_wheels.steeringCenterNode
                            if center~=nil then
                                local cx,cy,cz=env.getWorldTranslation(center)
                                local side,_,ahead=env.worldToLocal(unit.rootNode,cx,cy,cz)
                                local rx=x+math.cos(yaw)*side+math.sin(yaw)*ahead
                                local rz=z-math.sin(yaw)*side+math.cos(yaw)*ahead
                                for _,curve in ipairs({-0.18,0.18}) do
                                    for d=1,5 do
                                        local nyaw=yaw+d*curve
                                        local dx=(math.cos(yaw)-math.cos(nyaw))/curve
                                        local dz=(math.sin(nyaw)-math.sin(yaw))/curve
                                        local oldX,oldZ=x+dx,z+dz
                                        local newX=rx+dx-math.cos(nyaw)*side-math.sin(nyaw)*ahead
                                        local newZ=rz+dz+math.sin(nyaw)*side-math.cos(nyaw)*ahead
                                        local ok0,c0,r0=pcall(self.Hitch.routePoseClear,
                                            env,foot,oldX,oldZ,nyaw,curve,false)
                                        local ok1,c1,r1=pcall(self.Hitch.routePoseClear,
                                            env,foot,newX,newZ,nyaw,curve,false)
                                        log("physical pair arc id=%d curve=%.2f d=%d old=(%.3f,%.3f,%s,%s) measured=(%.3f,%.3f,%s,%s) center=(%.3f,%.3f)",
                                            id,curve,d,oldX,oldZ,tostring(ok0 and c0),
                                            tostring(ok0 and r0),newX,newZ,
                                            tostring(ok1 and c1),tostring(ok1 and r1),
                                            side,ahead)
                                    end
                                end
                            end
                        end
                    end
                end
            end
            for _, pair in ipairs({ {630, 1}, {629, 2}, {628, 3}, {628, 4}, {628, 5}, {628, 7},
                    {628, 3, 0.10, "unwind"}, {628, 3, 0.02, "cross"},
                    {629, 5, -0.02, "cross"}, {629, 5, 0.02, "uncross"} }) do
                local marker = bridge.dir .. string.format("machine_actuator_%d_%d%s.flag",
                    pair[1], pair[2], pair[4] and "_" .. pair[4] or "")
                if env.fileExists(marker) then
                    env.deleteFile(marker)
                    local ok, err = pcall(Probe.startActuatorProof, self,
                        pair[1], pair[2], pair[3])
                    if not ok then log("machine actuator UNVERIFIED setup %s", tostring(err)) end
                    break
                end
            end
            for _, terraId in ipairs({ 609, 610 }) do
                local terraMarker = bridge.dir .. string.format("terra_pulse_%d.flag", terraId)
                if env.fileExists(terraMarker) then
                    env.deleteFile(terraMarker)
                    local ok, err = pcall(Probe.startTerraProof, self, terraId)
                    if not ok then log("terra pulse UNVERIFIED setup %s", tostring(err)) end
                    break
                end
            end
            if not self.motionListenerSeen then
                self.motionListenerSeen = true
                log("motion proof listener ticking dir=%s", bridge.dir)
            end
            for _, mode in ipairs({ "proof", "reverse", "second_forward", "second_reverse" }) do
                local marker = bridge.dir .. "motion_" .. mode .. ".flag"
                if env.fileExists(marker) then
                    env.deleteFile(marker)
                    local ok, err = pcall(Probe.startMotionProof, self, mode)
                    if not ok then log("motion proof FAILED setup %s", tostring(err)) end
                    break
                end
            end
            for _, trial in ipairs({ {615, -0.5, 0}, {615, -0.5, 0, false, "point"},
                    {615, -0.5, 0, false, "point_low"},
                    {615, -0.5, 0, false, "production"},
                    {628, -0.5, 0, true}, {629, -0.5, -0.08},
                    {630, 0.5, 0.08} }) do
                local tracked = bridge.dir .. string.format(
                    trial[5] == "point" and "point_reverse_%d.flag"
                        or trial[5] == "point_low" and "point_reverse_low_%d.flag"
                        or trial[5] == "production" and "production_reverse_%d.flag"
                        or "tracked_motion_%d.flag", trial[1])
                if env.fileExists(tracked) then
                    env.deleteFile(tracked)
                    local ok, err = pcall(Probe.startTrackedMotionProof, self,
                        trial[1], trial[2], trial[3], trial[4], trial[5])
                    if not ok then log("tracked motion UNVERIFIED setup %s", tostring(err)) end
                    break
                end
            end
        end
        local stop = bridge.dir .. "motion_stop.flag"
        if env.fileExists(stop) then
            env.deleteFile(stop)
            if self.motion ~= nil then self:finishMotionProof("STOPPED") end
        end
    end
    if self.motion ~= nil then
        local ok, err = pcall(Probe.stepMotionProof, self, dt)
        if not ok then
            log("motion proof UNVERIFIED step %s", tostring(err))
            self:finishMotionProof("UNVERIFIED")
        end
    end
    if self.actuator ~= nil then
        local ok, err = pcall(Probe.stepActuatorProof, self, dt)
        if not ok then
            log("machine actuator UNVERIFIED step %s", tostring(err))
            if self.actuator.tool ~= nil then
                self.actuator.tool.curTargetPosition = nil
                self.actuator.tool.curTargetDirection = nil
            end
            if self.actuator.startedMotor and type(self.actuator.vehicle.stopMotor) == "function" then
                pcall(self.actuator.vehicle.stopMotor, self.actuator.vehicle)
            end
            self.actuator = nil
        end
    end
    if self.treeAlign ~= nil then
        local ok, err = pcall(Probe.stepNativeTreeAlign, self, dt)
        if not ok then
            log("tree native align UNVERIFIED step %s", tostring(err))
            self:finishNativeTreeAlign("UNVERIFIED")
        end
    end
    if self.treeCut ~= nil then
        local ok, err = pcall(Probe.stepOneTreeCut, self, dt)
        if not ok then
            log("tree cut UNVERIFIED step %s", tostring(err))
            self.treeCut = nil
        end
    end
    if self.treeProcess ~= nil then
        local ok, err = pcall(Probe.stepOneTreeProcess, self, dt)
        if not ok then
            log("tree process UNVERIFIED step %s", tostring(err))
            self.treeProcess = nil
        end
    end
    if self.treeStack ~= nil then
        local ok, err = pcall(Probe.stepTreeStackTrial, self, dt)
        if not ok then
            log("tree stack UNVERIFIED step %s", tostring(err))
            if self.treeStack.vehicle ~= nil then
                for _, tool in ipairs(self.treeStack.vehicle.spec_cylindered.movingTools) do
                    tool.externalMove = 0
                end
            end
            self.treeStack = nil
        end
    end
    if self.terraPulse ~= nil then
        local ok, err = pcall(Probe.stepTerraProof, self, dt)
        if not ok then
            log("terra pulse UNVERIFIED step %s", tostring(err))
            pcall(self.terraPulse.vehicle.setMachineActive, self.terraPulse.vehicle, false)
            if self.terraPulse.originalState ~= nil then
                pcall(self.terraPulse.vehicle.setMachineState,
                    self.terraPulse.vehicle, self.terraPulse.originalState)
            end
            pcall(self.Hitch.halt, self.env, self.terraPulse, dt)
            if self.terraPulse.startedMotor then
                pcall(self.terraPulse.tractor.stopMotor, self.terraPulse.tractor)
            end
            self.terraPulse = nil
        end
    end
    local task = self.task
    if task == nil or task.op == nil then return end
    local ok, err = pcall(step, self, task, dt)
    if not ok then
        log("probe error: %s", tostring(err))
        self:finish("UNVERIFIED", "UNKNOWN_OBSERVATION")
    end
end

local function terrainSamples(env, area)
    local samples = {}
    for i, node in ipairs(area.areaNodes or {}) do
        local x, y, z = env.getWorldTranslation(node)
        samples[i] = { x = x, y = y, z = z,
            terrain = env.getTerrainHeightAtWorldPos(env.g_terrainNode, x, 0, z) }
    end
    return samples
end

local function terrainChanges(env, before, label)
    local largest = 0
    for i, sample in ipairs(before or {}) do
        local after = env.getTerrainHeightAtWorldPos(env.g_terrainNode,
            sample.x, 0, sample.z)
        local delta = after - sample.terrain
        largest = math.max(largest, math.abs(delta))
        log("terra pulse sample %s node=%d xyz=(%.3f,%.3f) before=%.4f after=%.4f delta=%.4f",
            label, i, sample.x, sample.z, sample.terrain, after, delta)
    end
    return largest
end

function Probe:startTerraProof(id)
    local env = self.env
    if self.terraPulse ~= nil or self.actuator ~= nil or self.motion ~= nil
            or self.task ~= nil or self.Bridge.active ~= nil then
        log("terra pulse REFUSED busy") return
    end
    local vehicle
    for _, candidate in pairs(vehicles(env)) do
        if env.NetworkUtil.getObjectId(candidate) == id then vehicle = candidate; break end
    end
    local spec = vehicle and vehicle.spec_machine
    if spec == nil or spec.active or spec.inputMode ~= 2
            or type(vehicle.getCanActivateMachine) ~= "function"
            or not vehicle:getCanActivateMachine()
            or type(vehicle.setMachineActive) ~= "function"
            or spec.workArea == nil or #(spec.workArea.areaNodes or {}) == 0 then
        log("terra pulse REFUSED capability/precondition id=%s", tostring(id)) return
    end
    local root = type(vehicle.findRootVehicle) == "function" and vehicle:findRootVehicle() or nil
    if root == nil or root:getIsAIActive() ~= false or math.abs(root:getLastSpeed()) >= 0.5 then
        log("terra pulse REFUSED root not idle id=%s", tostring(id)) return
    end
    local before = terrainSamples(env, spec.workArea)
    vehicle:setMachineActive(true)
    local x, _, z = env.getWorldTranslation(root.rootNode)
    local startedMotor = not root:getIsMotorStarted()
    if startedMotor then root:startMotor() end
    local pulse = { vehicle = vehicle, root = root, tractor = root,
        before = before, elapsed = 0, brakeElapsed = 0,
        startX = x, startZ = z, maxSpeedKmh = 2,
        startedMotor = startedMotor, seenAvailable = false, commandFrames = 0,
        startFill = spec.fillUnit and spec.fillUnit.fillLevel or nil,
        peakSpeed = 0, seenDirection = false }
    self.terraPulse = pulse
    self.Hitch.watchDriver(env, root)
    self.Hitch.holdActive(pulse)
    log("terra pulse START id=%d rootId=%s mode=%s nodes=%d interval=%s areaActive=%s motor=%s",
        id, tostring(env.NetworkUtil.getObjectId(root)),
        tostring(spec.inputMode), #before, tostring(spec.updateInterval),
        tostring(spec.workArea.isAreaNodeActive), tostring(root:getIsMotorStarted()))
end

function Probe:stepTerraProof(dt)
    local p, env = self.terraPulse, self.env
    p.elapsed = p.elapsed + dt
    p.root:raiseActive()
    if not p.finishing then
        local x, _, z = env.getWorldTranslation(p.root.rootNode)
        local distance = math.sqrt((x-p.startX)^2 + (z-p.startZ)^2)
        p.distance = distance
        p.peakSpeed = math.max(p.peakSpeed, math.abs(p.root:getLastSpeed()))
        p.seenDirection = p.seenDirection or p.root:getDrivingDirection() > 0
        if p.originalState == nil and distance >= 0.1
                and p.root:getLastSpeed() >= 0.2
                and p.vehicle.spec_machine.state ~= nil then
            local original = p.vehicle.spec_machine.state
            local override = original:clone()
            -- TerraFarm MachineState.lua in the installed mod: IGNORE = 4.
            override.drivingDirectionMode = 4
            p.originalState = original
            p.vehicle:setMachineState(override)
            log("terra pulse adapter IGNORE after observed distance=%.3f speed=%.3f",
                distance, p.root:getLastSpeed())
        end
        if p.vehicle:getIsAvailable() and not p.seenAvailable then
            p.seenAvailable = true
            p.availableAt = distance
            p.workBefore = terrainSamples(env, p.vehicle.spec_machine.workArea)
            log("terra pulse AVAILABLE distance=%.3f speed=%.3f direction=%s",
                distance, p.root:getLastSpeed(), tostring(p.root:getDrivingDirection()))
        end
        if distance >= 2.0 or p.elapsed >= 10000
                or (p.availableAt ~= nil and distance >= p.availableAt + 0.3)
                or p.root:getIsAIActive() ~= false or self.Bridge.active ~= nil then
            p.finishing = true
            p.vehicle:setMachineActive(false)
            if p.originalState ~= nil then
                p.vehicle:setMachineState(p.originalState)
                p.originalState = nil
                p.stateRestored = true
            end
        elseif p.root:getIsMotorStarted() then
            p.commandFrames = p.commandFrames + 1
            env.AIVehicleUtil.driveAlongCurvature(p.root, dt, 0, 3, 0.4)
            return
        else
            return
        end
    end
    p.brakeElapsed = p.brakeElapsed + dt
    self.Hitch.halt(env, p, dt)
    if p.brakeElapsed < 500 or (math.abs(p.root:getLastSpeed()) >= 0.5
            and p.brakeElapsed < 4000) then return end
    local motorAtEnd = p.root:getIsMotorStarted()
    if p.startedMotor then p.root:stopMotor() end
    local largest = terrainChanges(env, p.before, "start")
    largest = math.max(largest, terrainChanges(env, p.workBefore, "work"))
    log("terra pulse RESULT id=%s elapsedMs=%d distance=%.3f commandFrames=%d peakSpeed=%.3f seenForward=%s motor=%s driveDirection=%s axisForward=%s availableWhileMoving=%s stateRestored=%s maxTerrainDelta=%.4f fillBefore=%s fillAfter=%s active=%s rootSpeed=%s",
        tostring(env.NetworkUtil.getObjectId(p.vehicle)), p.elapsed, p.distance or 0,
        p.commandFrames, p.peakSpeed, tostring(p.seenDirection), tostring(motorAtEnd),
        tostring(p.root:getDrivingDirection()),
        tostring(p.root.spec_drivable and p.root.spec_drivable.axisForward),
        tostring(p.seenAvailable), tostring(p.stateRestored), largest,
        tostring(p.startFill), tostring(p.vehicle.spec_machine.fillUnit and p.vehicle.spec_machine.fillUnit.fillLevel),
        tostring(p.vehicle.spec_machine.active), tostring(p.root:getLastSpeed()))
    self.Hitch.disarm(p.root)
    self.terraPulse = nil
end

local function circularDelta(from, to)
    return (to - from + math.pi) % (2 * math.pi) - math.pi
end

function Probe:startActuatorProof(id, index, requestedDelta)
    local env = self.env
    if self.actuator ~= nil or self.motion ~= nil or self.task ~= nil
            or self.Bridge.active ~= nil then
        log("machine actuator REFUSED busy") return
    end
    local vehicle
    for _, candidate in pairs(vehicles(env)) do
        if env.NetworkUtil.getObjectId(candidate) == id then vehicle = candidate; break end
    end
    local spec = vehicle and vehicle.spec_cylindered
    local tool = spec and spec.movingTools[index]
    if tool == nil or vehicle.isServer ~= true or vehicle:getIsAIActive() ~= false
            or math.abs(vehicle:getLastSpeed()) >= 0.5
            or not vehicle:getIsMovingToolActive(tool) or tool.rotSpeed == nil
            or tool.rotationAxis == nil or tool.curTargetPosition ~= nil then
        log("machine actuator REFUSED unreadable/inactive/busy id=%s index=%s", tostring(id), tostring(index))
        return
    end
    local start = select(tool.rotationAxis, env.getRotation(tool.node))
    local direction = (tool.rotMax ~= nil and tool.rotMax - start < 0.02) and -1 or 1
    local delta = requestedDelta or direction * 0.02
    if math.abs(delta) > 0.100001 then
        log("machine actuator REFUSED pulse exceeds 0.10 rad id=%s index=%s", tostring(id), tostring(index))
        return
    end
    local target = math.min(tool.rotMax or math.huge,
        math.max(tool.rotMin or -math.huge, start + delta))
    local unlimited = tool.rotMin == nil and tool.rotMax == nil
    if (tool.rotMin == nil) ~= (tool.rotMax == nil) then
        log("machine actuator REFUSED incomplete limits id=%s index=%s", tostring(id), tostring(index))
        return
    end
    if math.abs(target - start) < 0.01 then
        log("machine actuator REFUSED at limit id=%s index=%s", tostring(id), tostring(index)) return
    end
    local roots = {}
    for i, item in ipairs(spec.movingTools) do
        local x, y, z = env.getWorldTranslation(item.node)
        roots[i] = { x, y, z }
    end
    local startedMotor = false
    if type(vehicle.getIsMotorStarted) == "function" and not vehicle:getIsMotorStarted() then
        if type(vehicle.startMotor) ~= "function" then
            log("machine actuator REFUSED power unavailable id=%s", tostring(id)) return
        end
        vehicle:startMotor()
        startedMotor = true
    end
    local stateTarget = unlimited and target % (2 * math.pi) or target
    if tool.rotMin ~= nil and tool.rotMax ~= nil then
        stateTarget = (target - tool.rotMin) / (tool.rotMax - tool.rotMin)
    end
    local workNode = vehicle.spec_woodHarvester and vehicle.spec_woodHarvester.cutNode
    local workX, workY, workZ
    if workNode ~= nil then workX, workY, workZ = env.getWorldTranslation(workNode) end
    self.actuator = { vehicle = vehicle, tool = tool, id = id, index = index,
        start = start, target = target, stateTarget = stateTarget,
        delta = target - start, unlimited = unlimited,
        roots = roots, elapsed = 0, motionElapsed = 0,
        workNode = workNode, workX = workX, workY = workY, workZ = workZ,
        startedMotor = startedMotor }
    log("machine actuator START id=%d index=%d node=%s start=%.5f target=%.5f normalizedTarget=%.5f speed=%s acceleration=%s lastSpeed=%s motor=%s",
        id, index, tostring(env.getName(tool.node)), start, target, stateTarget,
        tostring(tool.rotSpeed), tostring(tool.rotAcceleration), tostring(tool.lastRotSpeed),
        tostring(vehicle.getIsMotorStarted and vehicle:getIsMotorStarted()))
end

function Probe:stepActuatorProof(dt)
    local p, env = self.actuator, self.env
    p.elapsed = p.elapsed + dt
    p.vehicle:raiseActive()
    if p.startedMotor and not p.vehicle:getIsMotorStarted() then
        if p.elapsed < 5000 then return end
        log("machine actuator REFUSED motor did not start id=%d", p.id)
        p.tool.curTargetPosition = nil
        p.tool.curTargetDirection = nil
        p.vehicle:stopMotor()
        self.actuator = nil
        return
    end
    if p.tool.curTargetPosition == nil and p.motionElapsed == 0 then
        p.tool.curTargetPosition = p.stateTarget
        p.tool.curTargetDirection = p.delta > 0 and 1 or -1
    end
    local current = select(p.tool.rotationAxis, env.getRotation(p.tool.node))
    if p.settling then
        p.settleElapsed = p.settleElapsed + dt
        local change = p.unlimited and math.abs(circularDelta(p.lastAngle, current))
            or math.abs(current - p.lastAngle)
        p.lastAngle = current
        p.quietMs = change < 0.0002 and p.quietMs + dt or 0
        if p.quietMs < 300 and p.settleElapsed < 4000 then return end
    else
        p.motionElapsed = p.motionElapsed + dt
        local progress = p.unlimited and circularDelta(p.start, current) or current - p.start
        local remaining = p.delta - progress
        local speed, acceleration = p.tool.lastRotSpeed, p.tool.rotAcceleration
        local braking = type(speed) == "number" and type(acceleration) == "number"
            and acceleration > 0 and speed * p.delta > 0
            and speed * speed / (2 * acceleration) + math.abs(speed) * dt * 0.5 or nil
        if p.motionElapsed < 4000 and math.abs(remaining) > 0.001
                and remaining * p.delta > 0
                and (braking == nil or math.abs(remaining) > braking) then return end
        p.tool.curTargetPosition = nil
        p.tool.curTargetDirection = nil
        p.settling, p.settleElapsed, p.quietMs, p.lastAngle = true, 0, 0, current
        log("machine actuator BRAKE id=%d index=%d angle=%.5f requested=%.5f remaining=%.5f speed=%s acceleration=%s estimatedCoast=%s",
            p.id, p.index, current, p.target, remaining, tostring(speed),
            tostring(acceleration), tostring(braking))
        return
    end
    local progress = p.unlimited and circularDelta(p.start, current) or current - p.start
    if p.startedMotor and type(p.vehicle.stopMotor) == "function" then p.vehicle:stopMotor() end
    for i, item in ipairs(p.vehicle.spec_cylindered.movingTools) do
        local x, y, z = env.getWorldTranslation(item.node)
        local before = p.roots[i]
        local delta = math.sqrt((x-before[1])^2 + (y-before[2])^2 + (z-before[3])^2)
        if delta > 0.001 then
            log("machine actuator affected id=%d source=%d tool=%d node=%s endpointDelta=%.4f",
                p.id, p.index, i, tostring(env.getName(item.node)), delta)
        end
    end
    if p.workNode ~= nil then
        local wx, wy, wz = env.getWorldTranslation(p.workNode)
        log("machine actuator workEndpoint id=%d source=%d before=(%.3f,%.3f,%.3f) after=(%.3f,%.3f,%.3f) delta=(%.3f,%.3f,%.3f)",
            p.id, p.index, p.workX, p.workY, p.workZ, wx, wy, wz,
            wx-p.workX, wy-p.workY, wz-p.workZ)
    end
    local angleError = p.unlimited and math.abs(circularDelta(current, p.target))
        or math.abs(current-p.target)
    local outcome = p.quietMs < 300 and "SETTLE_TIMEOUT"
        or (angleError > 0.002 and "TARGET_MISSED" or "DONE")
    log("machine actuator RESULT id=%d index=%d status=%s commandedDelta=%.5f actualDelta=%.5f error=%.5f elapsedMs=%d settleMs=%d rootSpeed=%s",
        p.id, p.index, outcome, p.delta, progress,
        angleError, p.elapsed, p.settleElapsed, tostring(p.vehicle:getLastSpeed()))
    self.actuator = nil
end

-- Read-only lab inventory. It deliberately discovers controls by loaded
-- specialization state rather than a machine/model name.
-- Read-only tree census. Only split-shape trunks are recorded; ordinary tree
-- collision children are ignored so one standing tree is not counted twice.
function Probe:logTreeInventory()
    local env = self.env
    local player = env.g_localPlayer
    if player == nil then return log("tree inventory REFUSED no local player") end
    local px, py, pz = player:getPosition()
    local farmId = player:getFarmId()
    local found, seen, hits = {}, {}, 0
    local target = {}
    function target.onTree(_, node)
        hits = hits + 1
        if seen[node] or not env.getHasClassId(node, env.ClassIds.MESH_SPLIT_SHAPE)
                or env.getSplitType(node) == 0 or env.getRigidBodyType(node) ~= env.RigidBodyType.STATIC
                or env.getIsSplitShapeSplit(node) then return end
        seen[node] = true
        local x, y, z = env.getWorldTranslation(node)
        local allowed = env.g_splitShapeManager:getIsShapeCutAllowed(x, z, node, farmId)
        found[#found + 1] = { node=node, x=x, y=y, z=z, volume=env.getVolume(node), allowed=allowed,
            distance=math.sqrt((x-px)^2 + (z-pz)^2) }
    end
    env.overlapSphere(px, py, pz, 100, "onTree", target, env.CollisionFlag.TREE, false, false, true, false)
    table.sort(found, function(a, b) return a.distance < b.distance end)
    log("tree inventory player=(%.2f,%.2f,%.2f) farm=%s overlapHits=%d standing=%d",
        px, py, pz, tostring(farmId), hits, #found)
    for i = 1, math.min(#found, 20) do
        local t = found[i]
        log("tree inventory candidate node=%s xyz=(%.2f,%.2f,%.2f) distance=%.2f volume=%.6f cutAllowed=%s",
            tostring(t.node), t.x, t.y, t.z, t.distance, t.volume, tostring(t.allowed))
    end
    log("tree inventory RESULT read-only")
end

-- Read-only feasibility check before attempting a native harvester cut.
function Probe:logTreeHeadReach()
    local env, count = self.env, 0
    for _, vehicle in pairs(vehicles(env)) do
        local spec = vehicle.spec_woodHarvester
        if spec ~= nil and spec.cutNode ~= nil then
            local id = env.NetworkUtil.getObjectId(vehicle)
            local x, y, z = env.getWorldTranslation(spec.cutNode)
            local rootX, rootY, rootZ = env.getWorldTranslation(vehicle.rootNode)
            local localX, localY, localZ = env.worldToLocal(vehicle.rootNode, x, y, z)
            local nx, ny, nz = env.localDirectionToWorld(spec.cutNode, 1, 0, 0)
            local yx, yy, yz = env.localDirectionToWorld(spec.cutNode, 0, 1, 0)
            local shape = env.findSplitShape(x, y, z, nx, ny, nz, yx, yy, yz,
                spec.cutSizeY, spec.cutSizeZ)
            local nearest, seen, found = nil, {}, {}
            local target = {}
            function target.onTree(_, node)
                if seen[node] or not env.getHasClassId(node, env.ClassIds.MESH_SPLIT_SHAPE)
                        or env.getSplitType(node) == 0
                        or env.getRigidBodyType(node) ~= env.RigidBodyType.STATIC
                        or env.getIsSplitShapeSplit(node) then return end
                seen[node] = true
                local tx, ty, tz = env.getWorldTranslation(node)
                local distance = math.sqrt((tx-x)^2 + (tz-z)^2)
                found[#found + 1] = { node=node, distance=distance,
                    x=tx, y=ty, z=tz, volume=env.getVolume(node) }
                if nearest == nil or distance < nearest.distance then
                    nearest = { node=node, distance=distance,
                        volume=env.getVolume(node), x=tx, y=ty, z=tz }
                end
            end
            env.overlapSphere(x, y, z, 30, "onTree", target, env.CollisionFlag.TREE,
                false, false, true, false)
            log("tree head id=%s cut=(%.2f,%.2f,%.2f) shape=%s current=%s attached=%s nearest=%s nearestXYZ=(%s,%s,%s) dist=%s volume=%s cutMaxRadius=%s",
                tostring(id), x, y, z, tostring(shape), tostring(spec.curSplitShape),
                tostring(spec.attachedSplitShape), tostring(nearest and nearest.node),
                tostring(nearest and nearest.x), tostring(nearest and nearest.y),
                tostring(nearest and nearest.z),
                tostring(nearest and nearest.distance), tostring(nearest and nearest.volume),
                tostring(spec.cutMaxRadius))
            log("tree head frame id=%s root=(%.3f,%.3f,%.3f) cutLocal=(%.3f,%.3f,%.3f) speed=%.4f",
                tostring(id), rootX, rootY, rootZ, localX, localY, localZ,
                vehicle:getLastSpeed())
            local auto = vehicle.spec_automaticArmControlHarvester
            if auto ~= nil then
                local function attempt(name)
                    local method = vehicle[name]
                    if type(method) ~= "function" then return "unavailable" end
                    local ok, value = pcall(method, vehicle)
                    return ok and tostring(value) or "error:" .. tostring(value)
                end
                local detectX, detectY, detectZ
                if auto.treeDetectionNode ~= nil then
                    detectX, detectY, detectZ = env.getWorldTranslation(auto.treeDetectionNode)
                end
                log("tree auto id=%s supported=%s allowed=%s controlled=%s valid=%s state=%s target=%s detection=(%s,%s,%s) radius=(%s,%s) easy=%s turnedOn=%s",
                    tostring(id), attempt("getSupportsAutoTreeAlignment"),
                    attempt("getIsAutoTreeAlignmentAllowed"), attempt("getIsControlled"),
                    tostring(auto.foundValidTargetServer), tostring(auto.state),
                    tostring(auto.lastTargetTrans and table.concat(auto.lastTargetTrans, ",")),
                    tostring(detectX), tostring(detectY), tostring(detectZ),
                    tostring(auto.treeDetectionNodeMinRadius), tostring(auto.treeDetectionNodeMaxRadius),
                    tostring(vehicle.spec_cylindered.easyArmControl and
                        vehicle.spec_cylindered.easyArmControl.state),
                    attempt("getIsTurnedOn"))
                if nearest ~= nil and auto.treeDetectionNode ~= nil
                        and type(vehicle.getBestTreeToAutoAlign) == "function" then
                    local nearby, compatible, allCompatible, closestCompatible = {}, 0, 0, nil
                    for _, tree in ipairs(found) do
                        local canHarvest = env.g_splitShapeManager:getSplitShapeAllowsHarvester(tree.node)
                        if canHarvest then
                            allCompatible = allCompatible + 1
                            if closestCompatible == nil
                                    or tree.distance < closestCompatible.distance then
                                closestCompatible = tree
                            end
                        end
                        local dx, dz = tree.x - detectX, tree.z - detectZ
                        local detectionDistance = math.sqrt(dx*dx + dz*dz)
                        if detectionDistance > (auto.treeDetectionNodeMinRadius or 0)
                                and detectionDistance < (auto.treeDetectionNodeMaxRadius or 0) then
                            nearby[#nearby + 1] = tree.node
                            if canHarvest then
                                compatible = compatible + 1
                            end
                        end
                    end
                    local okBest, best, invalid, invalidReason = pcall(
                        vehicle.getBestTreeToAutoAlign, vehicle,
                        auto.treeDetectionNode, nearby)
                    log("tree auto candidate id=%s nearest=%s inRange=%d compatible=%d best=%s invalid=%s reason=%s validCall=%s xTool=%s yTool=%s zTool=%s",
                        tostring(id), tostring(nearest.node), #nearby, compatible,
                        tostring(best), tostring(invalid),
                        tostring(invalidReason), tostring(okBest),
                        tostring(auto.xAlignment and auto.xAlignment.movingTool ~= nil),
                        tostring(auto.yAlignment and auto.yAlignment.movingTool ~= nil),
                        tostring(auto.zAlignment and auto.zAlignment.movingTool ~= nil))
                    log("tree auto vicinity id=%s standing=%d harvesterCompatible=%d nearestCompatible=%s distance=%s xyz=(%s,%s,%s)",
                        tostring(id), #found, allCompatible,
                        tostring(closestCompatible and closestCompatible.node),
                        tostring(closestCompatible and closestCompatible.distance),
                        tostring(closestCompatible and closestCompatible.x),
                        tostring(closestCompatible and closestCompatible.y),
                        tostring(closestCompatible and closestCompatible.z))
                    if best ~= nil then
                        local bx, by, bz = env.getWorldTranslation(best)
                        log("tree auto best id=%s tree=%s xyz=(%.3f,%.3f,%.3f) volume=%.6f",
                            tostring(id), tostring(best), bx, by, bz, env.getVolume(best))
                    end
                end
            end
            count = count + 1
        end
    end
    log("tree head RESULT read-only machines=%d", count)
end

-- Read-only native tree target census. SplitShapeUtil resolves the trunk offset
-- and the loaded arm's cut-height data supplies a machine-safe target height.
-- Physical head alignment and native cut acceptance remain separate proofs.
function Probe:logLowestCuttableTree()
    local env = self.env
    for _, vehicle in pairs(vehicles(env)) do
        local spec = vehicle.spec_woodHarvester
        local auto = vehicle.spec_automaticArmControlHarvester
        if spec ~= nil and spec.cutNode ~= nil and auto ~= nil then
            local id = env.NetworkUtil.getObjectId(vehicle)
            local hx, hy, hz = env.getWorldTranslation(spec.cutNode)
            local seen, trees = {}, {}
            local target = {}
            function target.onTree(_, node)
                if seen[node] or not env.getHasClassId(node, env.ClassIds.MESH_SPLIT_SHAPE)
                        or env.getSplitType(node) == 0
                        or env.getRigidBodyType(node) ~= env.RigidBodyType.STATIC
                        or env.getIsSplitShapeSplit(node) then return end
                seen[node] = true
                local x, y, z = env.getWorldTranslation(node)
                trees[#trees + 1] = {node=node, x=x, y=y, z=z,
                    distance=math.sqrt((x-hx)^2 + (z-hz)^2)}
            end
            env.overlapSphere(hx, hy, hz, 30, "onTree", target,
                env.CollisionFlag.TREE, false, false, true, false)
            table.sort(trees, function(a, b) return a.distance < b.distance end)
            local compatible = 0
            for _, tree in ipairs(trees) do
                if env.g_splitShapeManager:getSplitShapeAllowsHarvester(tree.node) then
                    compatible = compatible + 1
                    if compatible <= 2 then
                        local ground = env.getTerrainHeightAtWorldPos(
                            env.g_terrainNode, tree.x, tree.y, tree.z)
                        local requestedY = math.max(tree.y, ground
                            + auto.treeDetectionNodeCutHeight
                            + auto.treeDetectionNodeCutHeightSafetyOffset)
                        local cx, cy, cz, nx, ny, nz, radius =
                            env.SplitShapeUtil.getTreeOffsetPosition(
                                tree.node, tree.x, requestedY, tree.z, 3)
                        local allowed = vehicle:getCanSplitShapeBeAccessed(
                            tree.x, tree.z, tree.node)
                        log("tree native target id=%s tree=%s distance=%.2f typeAllowed=true access=%s requestedY=%.3f center=(%s,%s,%s) axis=(%s,%s,%s) radius=%s maxRadius=%s cutHeight=%s safety=%s head=(%.3f,%.3f,%.3f)",
                            tostring(id), tostring(tree.node), tree.distance,
                            tostring(allowed), requestedY, tostring(cx),
                            tostring(cy), tostring(cz), tostring(nx),
                            tostring(ny), tostring(nz), tostring(radius),
                            tostring(spec.cutMaxRadius),
                            tostring(auto.treeDetectionNodeCutHeight),
                            tostring(auto.treeDetectionNodeCutHeightSafetyOffset),
                            hx, hy, hz)
                    end
                end
            end
            log("tree native target RESULT id=%s read-only standing=%d compatible=%d",
                tostring(id), #trees, compatible)
        end
    end
end

-- Lab-only teleport placement preview. The tree and the machine are selected
-- from live capability/state; no saved ID or map coordinate is used.
function Probe:previewTreeTeleport()
    local env = self.env
    for _, vehicle in pairs(vehicles(env)) do
        local spec = vehicle.spec_woodHarvester
        local auto = vehicle.spec_automaticArmControlHarvester
        if spec ~= nil and auto ~= nil and auto.treeDetectionNode ~= nil then
            local id = env.NetworkUtil.getObjectId(vehicle)
            local rx, _, rz = env.getWorldTranslation(vehicle.rootNode)
            local dx, dy, dz = env.getWorldTranslation(auto.treeDetectionNode)
            local _, rootYaw, _ = env.getWorldRotation(vehicle.rootNode)
            local localDx, _, localDz = env.worldToLocal(vehicle.rootNode, dx, dy, dz)
            local hx, hy, hz = env.getWorldTranslation(spec.cutNode)
            local localHx, _, localHz = env.worldToLocal(vehicle.rootNode, hx, hy, hz)
            local seen, candidates = {}, {}
            local overlap = {}
            function overlap.onTree(_, node)
                if seen[node] or not env.getHasClassId(node, env.ClassIds.MESH_SPLIT_SHAPE)
                        or env.getRigidBodyType(node) ~= env.RigidBodyType.STATIC
                        or env.getIsSplitShapeSplit(node) then return end
                seen[node] = true
                if not env.g_splitShapeManager:getSplitShapeAllowsHarvester(node) then return end
                local x, _, z = env.getWorldTranslation(node)
                local distance = math.sqrt((x-dx)^2 + (z-dz)^2)
                candidates[#candidates + 1] = {node=node, x=x, z=z, distance=distance}
            end
            env.overlapSphere(dx, dy, dz, 30, "onTree", overlap,
                env.CollisionFlag.TREE, false, false, true, false)
            table.sort(candidates, function(a,b) return a.distance < b.distance end)
            if #candidates == 0 then return log("tree teleport preview REFUSED no compatible tree") end
            local foot = self.Hitch.routeFootprint(env, vehicle)
            local clear, total, best, tree = 0, 0, nil, nil
            for _, candidate in ipairs(candidates) do
              if vehicle:getCanSplitShapeBeAccessed(
                      candidate.x,candidate.z,candidate.node) then
                for radius = 5, 10, 0.5 do
                  for sector = 0, 15 do
                    local angle = sector * 2 * math.pi / 16
                    local x = candidate.x - math.sin(angle)*radius
                    local z = candidate.z - math.cos(angle)*radius
                    local yaw = angle
                    local verdict = self.Hitch.routePoseClear(env, foot, x, z, yaw, 0)
                    total = total + 1
                    if verdict == true then
                        local detX = x + math.cos(yaw)*localDx + math.sin(yaw)*localDz
                        local detZ = z - math.sin(yaw)*localDx + math.cos(yaw)*localDz
                        local dd = math.sqrt((candidate.x-detX)^2 + (candidate.z-detZ)^2)
                        if dd >= auto.treeDetectionNodeMinRadius
                                and dd <= auto.treeDetectionNodeMaxRadius then
                            clear = clear + 1
                            local fromCurrent = math.sqrt((x-rx)^2 + (z-rz)^2)
                            local headX=x+math.cos(yaw)*localHx+math.sin(yaw)*localHz
                            local headZ=z-math.sin(yaw)*localHx+math.cos(yaw)*localHz
                            local headDistance=math.sqrt((candidate.x-headX)^2+
                                (candidate.z-headZ)^2)
                            if best == nil or headDistance < best.headDistance then
                                best = {x=x,z=z,yaw=yaw,detectDistance=dd,
                                    fromCurrent=fromCurrent,
                                    headDistance=headDistance}
                                tree = candidate
                            end
                        end
                    end
                  end
                end
              end
            end
            log("tree teleport preview id=%s tree=%s treeXZ=(%s,%s) currentRoot=(%.3f,%.3f) currentYaw=%.3f detectionLocal=(%.3f,%.3f) headLocal=(%.3f,%.3f) trees=%d candidates=%d clearInDetection=%d best=(%s,%s,%s) bestDetection=%s predictedHeadDistance=%s move=%s",
                tostring(id), tostring(tree and tree.node),
                tostring(tree and tree.x),tostring(tree and tree.z),
                rx, rz, rootYaw,
                localDx, localDz,localHx,localHz,#candidates,total,clear,
                tostring(best and best.x), tostring(best and best.z),
                tostring(best and best.yaw), tostring(best and best.detectDistance),
                tostring(best and best.headDistance),
                tostring(best and best.fromCurrent))
            self.treeTeleportPreview = best and {vehicle=vehicle, tree=tree, pose=best}
        end
    end
end

function Probe:executeTreeTeleport()
    local env, preview = self.env, self.treeTeleportPreview
    if preview == nil or self.motion ~= nil or self.actuator ~= nil
            or self.task ~= nil or self.Bridge.active ~= nil then
        return log("tree teleport REFUSED no fresh preview or control busy")
    end
    self.treeTeleportPreview = nil
    local v, tree, pose = preview.vehicle, preview.tree, preview.pose
    if v.isServer ~= true or v:getIsAIActive() ~= false
            or math.abs(v:getLastSpeed()) > 0.5
            or env.getIsSplitShapeSplit(tree.node)
            or not env.g_splitShapeManager:getSplitShapeAllowsHarvester(tree.node)
            or not v:getCanSplitShapeBeAccessed(tree.x, tree.z, tree.node) then
        return log("tree teleport REFUSED vehicle or tree changed")
    end
    local foot = self.Hitch.routeFootprint(env, v)
    if self.Hitch.routePoseClear(env, foot, pose.x, pose.z, pose.yaw, 0) ~= true then
        return log("tree teleport REFUSED destination no longer clear")
    end
    local id = env.NetworkUtil.getObjectId(v)
    local beforeX, beforeY, beforeZ = env.getWorldTranslation(v.rootNode)
    log("tree teleport START id=%s tree=%s before=(%.3f,%.3f,%.3f) target=(%.3f,%.3f,%.3f) detectionDistance=%.3f",
        tostring(id), tostring(tree.node), beforeX, beforeY, beforeZ,
        pose.x, pose.z, pose.yaw, pose.detectDistance)
    env.g_currentMission:teleportVehicle(v, pose.x, pose.z, pose.yaw)
    local afterX, afterY, afterZ = env.getWorldTranslation(v.rootNode)
    local hx, hy, hz = env.getWorldTranslation(v.spec_woodHarvester.cutNode)
    local dx, dy, dz = env.getWorldTranslation(
        v.spec_automaticArmControlHarvester.treeDetectionNode)
    local detected = math.sqrt((tree.x-dx)^2 + (tree.z-dz)^2)
    log("tree teleport RESULT id=%s tree=%s after=(%.3f,%.3f,%.3f) head=(%.3f,%.3f,%.3f) detect=(%.3f,%.3f,%.3f) detectionDistance=%.3f idle=%s",
        tostring(id), tostring(tree.node), afterX, afterY, afterZ,
        hx, hy, hz, dx, dy, dz, detected, tostring(v:getLastSpeed()))
end

function Probe:startNativeTreeAlign()
    local env = self.env
    if self.treeAlign ~= nil or self.motion ~= nil or self.actuator ~= nil
            or self.task ~= nil or self.Bridge.active ~= nil then
        return log("tree native align REFUSED control busy")
    end
    for _, v in pairs(vehicles(env)) do
        local spec, auto = v.spec_woodHarvester, v.spec_automaticArmControlHarvester
        if spec ~= nil and auto ~= nil and auto.treeDetectionNode ~= nil then
            if v.isServer ~= true or v:getIsAIActive() ~= false
                    or math.abs(v:getLastSpeed()) > 0.5 then
                return log("tree native align REFUSED authority or vehicle motion")
            end
            local dx, dy, dz = env.getWorldTranslation(auto.treeDetectionNode)
            local seen, found = {}, {}
            local target = {}
            function target.onTree(_, node)
                if not seen[node] and env.getHasClassId(node, env.ClassIds.MESH_SPLIT_SHAPE)
                        and env.getRigidBodyType(node) == env.RigidBodyType.STATIC
                        and not env.getIsSplitShapeSplit(node) then
                    seen[node] = true
                    found[#found + 1] = node
                end
            end
            env.overlapSphere(dx, dy, dz, auto.treeDetectionNodeMaxRadius,
                "onTree", target, env.CollisionFlag.TREE,
                false, false, true, false)
            local best = v:getBestTreeToAutoAlign(auto.treeDetectionNode, found)
            if best == nil then return log("tree native align REFUSED no native compatible target") end
            local tx, ty, tz = env.getWorldTranslation(best)
            local terrain = env.getTerrainHeightAtWorldPos(env.g_terrainNode, tx, ty, tz)
            ty = math.max(ty, terrain + auto.treeDetectionNodeCutHeight
                + auto.treeDetectionNodeCutHeightSafetyOffset)
            local cx, cy, cz, _, _, _, radius =
                env.SplitShapeUtil.getTreeOffsetPosition(best, tx, ty, tz, 3)
            if cx == nil or radius > spec.cutMaxRadius then
                return log("tree native align REFUSED target geometry/radius")
            end
            local hx, hy, hz = env.getWorldTranslation(spec.cutNode)
            local startedMotor = false
            if not v:getIsMotorStarted() then
                v:startMotor()
                startedMotor = true
            end
            self.treeAlign = {vehicle=v, tree=best, target={cx,cy,cz},
                radius=radius, oldRadius=auto.lastRadius, elapsed=0, nextLog=2000,
                head={hx,hy,hz}, startedMotor=startedMotor}
            auto.lastRadius = radius
            log("tree native align START id=%s tree=%s target=(%.3f,%.3f,%.3f) radius=%.3f head=(%.3f,%.3f,%.3f) motor=%s",
                tostring(env.NetworkUtil.getObjectId(v)), tostring(best),
                cx, cy, cz, radius, hx, hy, hz, tostring(v:getIsMotorStarted()))
            return
        end
    end
    log("tree native align REFUSED no harvester")
end

function Probe:finishNativeTreeAlign(outcome)
    local p = self.treeAlign
    if p == nil then return end
    local v, env = p.vehicle, self.env
    for _, tool in ipairs(v.spec_cylindered.movingTools) do tool.externalMove = 0 end
    v:setTreeArmAlignmentInput(0)
    v.spec_automaticArmControlHarvester.lastRadius = p.oldRadius
    local hx, hy, hz = env.getWorldTranslation(v.spec_woodHarvester.cutNode)
    local nx, ny, nz = env.localDirectionToWorld(v.spec_woodHarvester.cutNode, 1, 0, 0)
    local yx, yy, yz = env.localDirectionToWorld(v.spec_woodHarvester.cutNode, 0, 1, 0)
    local shape = env.findSplitShape(hx, hy, hz, nx, ny, nz, yx, yy, yz,
        v.spec_woodHarvester.cutSizeY, v.spec_woodHarvester.cutSizeZ)
    log("tree native align RESULT status=%s tree=%s head=(%.3f,%.3f,%.3f) headDelta=(%.3f,%.3f,%.3f) nativeShape=%s state=%s elapsedMs=%d",
        outcome, tostring(p.tree), hx, hy, hz,
        hx-p.head[1], hy-p.head[2], hz-p.head[3], tostring(shape),
        tostring(v.spec_automaticArmControlHarvester.state), p.elapsed)
    self.treeAlign = nil
    if outcome == "FINISHED" and env.entityExists(p.tree)
            and env.g_splitShapeManager:getSplitShapeAllowsHarvester(p.tree) then
        self.treeCut = {vehicle=v, tree=p.tree,
            beforeVolume=env.getVolume(p.tree), elapsed=0,
            phase="power", startedMotor=p.startedMotor}
        log("tree cut HANDOFF tree=%s originalVolume=%.6f powered=%s curShape=%s",
            tostring(p.tree), self.treeCut.beforeVolume,
            tostring(v:getIsMotorStarted()), tostring(v.spec_woodHarvester.curSplitShape))
    elseif p.startedMotor then
        v:stopMotor()
    end
end

function Probe:stepNativeTreeAlign(dt)
    local p = self.treeAlign
    p.elapsed = p.elapsed + dt
    local v, auto = p.vehicle, p.vehicle.spec_automaticArmControlHarvester
    if v:getIsAIActive() ~= false
            or not self.env.entityExists(p.tree)
            or self.env.getIsSplitShapeSplit(p.tree) then
        return self:finishNativeTreeAlign("STOPPED_AUTHORITY_OR_TREE_CHANGED")
    end
    v:raiseActive()
    auto.lastRadius = p.radius
    v:doTreeArmAlignment(p.target[1], p.target[2], p.target[3], 1)
    if p.elapsed >= p.nextLog then
        p.nextLog = p.nextLog + 2000
        local x, y, z = self.env.getWorldTranslation(v.spec_woodHarvester.cutNode)
        local tool = auto.zAlignment and auto.zAlignment.movingTool
        local angle = tool and tool.curRot and tool.curRot[tool.rotationAxis]
        local trans = tool and tool.curTrans and tool.curTrans[tool.translationAxis]
        log("tree native align progress tree=%s head=(%.3f,%.3f,%.3f) state=%s zToolAngle=%s zToolMin=%s zToolMax=%s zToolTrans=%s zToolTransMin=%s zToolTransMax=%s zToolAnim=%s zToolAnimSpeed=%s zToolAnimRange=(%s,%s) zToolInput=%s elapsedMs=%d",
            tostring(p.tree), x, y, z, tostring(auto.state),
            tostring(angle),tostring(tool and tool.rotMin),
            tostring(tool and tool.rotMax),tostring(trans),
            tostring(tool and tool.transMin),tostring(tool and tool.transMax),
            tostring(tool and tool.curAnimTime),
            tostring(tool and tool.animSpeed),
            tostring(tool and tool.animMinTime),
            tostring(tool and tool.animMaxTime),
            tostring(tool and tool.externalMove),
            p.elapsed)
    end
    if auto.state == self.env.AutomaticArmControlHarvester.STATE_FINISHED then
        return self:finishNativeTreeAlign("FINISHED")
    end
    if p.elapsed >= 8000 then self:finishNativeTreeAlign("TIMEOUT") end
end

function Probe:startOneTreeCut()
    local env = self.env
    if self.treeCut ~= nil or self.treeAlign ~= nil or self.motion ~= nil
            or self.actuator ~= nil or self.task ~= nil or self.Bridge.active ~= nil then
        return log("tree cut REFUSED control busy")
    end
    for _, v in pairs(vehicles(env)) do
        local spec = v.spec_woodHarvester
        if spec ~= nil and spec.cutNode ~= nil then
            local tree = spec.curSplitShape
            if v.isServer ~= true or v:getIsAIActive() ~= false
                    or math.abs(v:getLastSpeed()) > 0.5
                    or tree == nil or not env.entityExists(tree)
                    or env.getRigidBodyType(tree) ~= env.RigidBodyType.STATIC
                    or not env.g_splitShapeManager:getSplitShapeAllowsHarvester(tree) then
                return log("tree cut REFUSED native tree/authority not ready")
            end
            local x, y, z = env.getWorldTranslation(spec.cutNode)
            if not v:getCanSplitShapeBeAccessed(x,z,tree) then
                return log("tree cut REFUSED native access denied")
            end
            local volume = env.getVolume(tree)
            local startedMotor = not v:getIsMotorStarted()
            if startedMotor then v:startMotor() end
            self.treeCut = {vehicle=v, tree=tree, beforeVolume=volume,
                elapsed=0, phase="power", startedMotor=startedMotor}
            log("tree cut START id=%s tree=%s originalVolume=%.6f cut=(%.3f,%.3f,%.3f) readyShape=%s",
                tostring(env.NetworkUtil.getObjectId(v)), tostring(tree),
                volume, x, y, z, tostring(spec.curSplitShape))
            return
        end
    end
    log("tree cut REFUSED no harvester")
end

function Probe:stepOneTreeCut(dt)
    local p = self.treeCut
    local v, env = p.vehicle, self.env
    local spec = v.spec_woodHarvester
    p.elapsed = p.elapsed + dt
    v:raiseActive()
    if p.phase == "power" then
        if not v:getIsTurnedOn() and v:getCanBeTurnedOn() then
            v:setIsTurnedOn(true)
        end
        if not v:getIsTurnedOn() then
            if p.elapsed < 3000 then return end
            log("tree cut REFUSED head did not turn on")
            self.treeCut = nil
            return
        end
        if spec.curSplitShape ~= p.tree then
            if p.elapsed < 3000 then return end
            log("tree cut REFUSED native cut target changed to %s", tostring(spec.curSplitShape))
            self.treeCut = nil
            return
        end
        p.phase = "cutting"
        v:cutTree(0)
        log("tree cut COMMANDED tree=%s timer=%s", tostring(p.tree), tostring(spec.cutTimer))
        return
    end
    if p.elapsed >= 10000 or spec.attachedSplitShape ~= nil then
        local sourceExists = env.entityExists(p.tree)
        local sourceVolume = sourceExists and env.getVolume(p.tree) or nil
        log("tree cut RESULT tree=%s sourceExists=%s sourceVolume=%s originalVolume=%.6f split=%s attached=%s previous=%s cutTimer=%s elapsedMs=%d",
            tostring(p.tree), tostring(sourceExists), tostring(sourceVolume),
            p.beforeVolume, tostring(sourceExists and env.getIsSplitShapeSplit(p.tree)),
            tostring(spec.attachedSplitShape), tostring(spec.prevSplitShape),
            tostring(spec.cutTimer), p.elapsed)
        self.treeCut = nil
    end
end

function Probe:startOneTreeProcess()
    local env = self.env
    if self.treeProcess ~= nil or self.treeCut ~= nil or self.treeAlign ~= nil
            or self.motion ~= nil or self.actuator ~= nil or self.task ~= nil
            or self.Bridge.active ~= nil then
        return log("tree process REFUSED control busy")
    end
    for _, v in pairs(vehicles(env)) do
        local spec = v.spec_woodHarvester
        if spec ~= nil and spec.attachedSplitShape ~= nil then
            local shape = spec.attachedSplitShape
            local volume = env.getVolume(shape)
            local length = spec.currentCutLength
            if v.isServer ~= true or v:getIsAIActive() ~= false
                    or not env.entityExists(shape) or not v:getIsTurnedOn()
                    or type(length) ~= "number" or length <= 0 then
                return log("tree process REFUSED native head/log not ready")
            end
            self.treeProcess = {vehicle=v, original=shape,
                initialVolume=volume, length=length, elapsed=0,
                phase="waitInitial"}
            log("tree process START shape=%s volume=%.6f nativeLength=%.3f moving=%s onlyMove=%s",
                tostring(shape), volume, length,
                tostring(spec.isAttachedSplitShapeMoving),
                tostring(spec.attachedSplitShapeOnlyMove))
            return
        end
    end
    log("tree process REFUSED no held tree")
end

function Probe:stepOneTreeProcess(dt)
    local p = self.treeProcess
    local v, env = p.vehicle, self.env
    local spec = v.spec_woodHarvester
    p.elapsed = p.elapsed + dt
    v:raiseActive()
    if p.phase == "waitInitial" then
        if spec.attachedSplitShape ~= p.original then
            log("tree process REFUSED held tree changed")
            self.treeProcess = nil
            return
        end
        if spec.isAttachedSplitShapeMoving or spec.attachedSplitShapeOnlyMove then
            if p.elapsed < 8000 then return end
            log("tree process REFUSED initial native motion did not settle")
            self.treeProcess = nil
            return
        end
        v:cutTree(p.length)
        p.phase = "feeding"
        log("tree process FEED shape=%s length=%.3f targetY=%s",
            tostring(p.original), p.length, tostring(spec.attachedSplitShapeTargetY))
        return
    end
    if p.phase == "feeding" then
        if spec.attachedSplitShape == nil then
            log("tree process RESULT native released entire tree while feeding")
            self.treeProcess = nil
            return
        end
        if spec.isAttachedSplitShapeMoving then
            if p.elapsed < 25000 then return end
            log("tree process REFUSED feed timeout")
            self.treeProcess = nil
            return
        end
        local before = env.getVolume(spec.attachedSplitShape)
        p.beforeLogCutVolume = before
        if not spec.automaticCuttingEnabled then v:cutTree(0) end
        p.phase = "logCut"
        log("tree process CUT_LOG held=%s beforeVolume=%.6f timer=%s automatic=%s",
            tostring(spec.attachedSplitShape), before,
            tostring(spec.cutTimer), tostring(spec.automaticCuttingEnabled))
        return
    end
    if p.phase == "logCut" then
        if p.elapsed < 30000 and spec.cutTimer ~= -1 then return end
        local held = spec.attachedSplitShape
        log("tree process RESULT original=%s originalExists=%s originalVolume=%s held=%s heldVolume=%s previous=%s elapsedMs=%d",
            tostring(p.original), tostring(env.entityExists(p.original)),
            tostring(env.entityExists(p.original) and env.getVolume(p.original)),
            tostring(held), tostring(held and env.getVolume(held)),
            tostring(spec.prevSplitShape), p.elapsed)
        self.treeProcess = nil
    end
end

function Probe:auditNearbyWood()
    local env = self.env
    for _, v in pairs(vehicles(env)) do
        local spec = v.spec_woodHarvester
        if spec ~= nil then
            local hx, hy, hz = env.getWorldTranslation(spec.cutNode)
            local fx, fy, fz = env.localDirectionToWorld(spec.cutNode, 1, 0, 0)
            local zx, zy, zz = env.localDirectionToWorld(spec.cutNode, 0, 0, 1)
            log("wood audit head xyz=(%.3f,%.3f,%.3f) feedAxis=(%.3f,%.3f,%.3f) zAxis=(%.3f,%.3f,%.3f) cutAttachDirection=%s feedY=%s",
                hx, hy, hz, fx, fy, fz, zx, zy, zz,
                tostring(spec.cutAttachDirection), tostring(spec.attachedSplitShapeY))
            local seen, shapes = {}, {}
            local target = {}
            function target.onTree(_, node)
                if seen[node] or not env.getHasClassId(node, env.ClassIds.MESH_SPLIT_SHAPE)
                        or env.getSplitType(node) == 0 then return end
                seen[node] = true
                local x, y, z = env.getWorldTranslation(node)
                local distance = math.sqrt((x-hx)^2+(z-hz)^2)
                shapes[#shapes+1] = {node=node, x=x,y=y,z=z,
                    volume=env.getVolume(node), distance=distance,
                    body=env.getRigidBodyType(node)}
            end
            env.overlapSphere(hx, hy, hz, 20, "onTree", target,
                env.CollisionFlag.TREE + env.CollisionFlag.DYNAMIC_OBJECT
                    + env.CollisionFlag.STATIC_OBJECT,
                false, false, true, false)
            env.overlapSphere(hx, hy, hz, 20, "onTree", target,
                env.CollisionFlag.TREE + env.CollisionFlag.DYNAMIC_OBJECT,
                true, true, false, true)
            table.sort(shapes, function(a,b) return a.distance < b.distance end)
            local dynamicCount, dynamicVolume = 0, 0
            for _, shape in ipairs(shapes) do
                if shape.body ~= env.RigidBodyType.STATIC then
                    dynamicCount = dynamicCount + 1
                    dynamicVolume = dynamicVolume + shape.volume
                    local ax, ay, az = env.localDirectionToWorld(shape.node, 0, 1, 0)
                    local below, above = env.getSplitShapePlaneExtents(
                        shape.node, shape.x, shape.y, shape.z, ax, ay, az)
                    local vx, vy, vz = env.getLinearVelocity(shape.node)
                    log("wood audit dynamic node=%s xyz=(%.3f,%.3f,%.3f) distance=%.3f volume=%.6f body=%s held=%s",
                        tostring(shape.node), shape.x, shape.y, shape.z,
                        shape.distance, shape.volume, tostring(shape.body),
                        tostring(spec.attachedSplitShape == shape.node))
                    log("wood audit geometry node=%s axis=(%.3f,%.3f,%.3f) extents=(%s,%s) speed=%.4f endpoints=(%s,%s,%s)->(%s,%s,%s)",
                        tostring(shape.node), ax, ay, az,
                        tostring(below), tostring(above),
                        math.sqrt(vx*vx+vy*vy+vz*vz),
                        tostring(below and shape.x-ax*below),
                        tostring(below and shape.y-ay*below),
                        tostring(below and shape.z-az*below),
                        tostring(above and shape.x+ax*above),
                        tostring(above and shape.y+ay*above),
                        tostring(above and shape.z+az*above))
                end
            end
            log("wood audit RESULT id=%s dynamic=%d volume=%.6f held=%s heldVolume=%s nearbyShapes=%d",
                tostring(env.NetworkUtil.getObjectId(v)), dynamicCount,
                dynamicVolume, tostring(spec.attachedSplitShape),
                tostring(spec.attachedSplitShape and env.getVolume(spec.attachedSplitShape)),
                #shapes)
        end
    end
end

-- One bounded, teleport-assisted stacking trial using the held remainder.
function Probe:startTreeStackTrial()
    local env = self.env
    if self.treeStack ~= nil or self.treeAlign ~= nil or self.treeCut ~= nil
            or self.treeProcess ~= nil or self.motion ~= nil or self.actuator ~= nil
            or self.task ~= nil or self.Bridge.active ~= nil then
        return log("tree stack REFUSED control busy")
    end
    for _, v in pairs(vehicles(env)) do
        local spec, auto = v.spec_woodHarvester, v.spec_automaticArmControlHarvester
        if spec ~= nil and auto ~= nil and spec.attachedSplitShape ~= nil then
            if v.isServer ~= true or v:getIsAIActive() ~= false
                    or not v:getIsTurnedOn() or math.abs(v:getLastSpeed()) > 0.5 then
                return log("tree stack REFUSED harvester not ready")
            end
            local hx, hy, hz = env.getWorldTranslation(spec.cutNode)
            local held = spec.attachedSplitShape
            local heldVolume = env.getVolume(held)
            if heldVolume < 0.1 then
                return log("tree stack REFUSED held volume %.6f too small for placement trial", heldVolume)
            end
            local fx, _, fz = env.localDirectionToWorld(spec.cutNode, 1, 0, 0)
            local feedHorizontal = math.sqrt(fx*fx+fz*fz)
            if feedHorizontal < 0.8 then return log("tree stack REFUSED feed axis too vertical") end
            fx, fz = fx/feedHorizontal, fz/feedHorizontal
            local best, seen = nil, {}
            local target = {}
            function target.onWood(_, node)
                if seen[node] or node == held
                        or not env.getHasClassId(node, env.ClassIds.MESH_SPLIT_SHAPE)
                        or env.getRigidBodyType(node) == env.RigidBodyType.STATIC then return end
                seen[node] = true
                local x, y, z = env.getWorldTranslation(node)
                local distance = math.sqrt((x-hx)^2+(z-hz)^2)
                if distance < 15 then
                    local ax, ay, az = env.localDirectionToWorld(node, 0, 1, 0)
                    local along = math.sqrt(ax*ax+az*az)
                    local vx, vy, vz = env.getLinearVelocity(node)
                    if along > 0.8 and vx*vx+vy*vy+vz*vz < 0.01 then
                        local alignment = math.abs((ax*fx+az*fz)/along)
                        local below, above = env.getSplitShapePlaneExtents(
                            node, x, y, z, ax, ay, az)
                        if below ~= nil and above ~= nil and alignment > 0.9 then
                            local a = {x-ax*below,y-ay*below,z-az*below}
                            local b = {x+ax*above,y+ay*above,z+az*above}
                            local mid = {(a[1]+b[1])*0.5,
                                (a[2]+b[2])*0.5,(a[3]+b[3])*0.5}
                            local _, _, _, _, _, _, radius =
                                env.SplitShapeUtil.getTreeOffsetPosition(
                                    node,mid[1],mid[2],mid[3],1.5)
                            local length = math.sqrt((b[1]-a[1])^2+
                                (b[3]-a[3])^2)
                            if radius ~= nil and length > 1 then
                                local ap=a[1]*fx+a[3]*fz
                                local bp=b[1]*fx+b[3]*fz
                                local cutEnd = ap > bp and a or b
                                local top = math.max(a[2],b[2])+radius
                                local ground = env.getTerrainHeightAtWorldPos(
                                    env.g_terrainNode,mid[1],mid[2],mid[3])
                                local score = alignment - distance*0.01
                                if best == nil or score > best.score then
                                    best={node=node,x=x,y=y,z=z,distance=distance,
                                        volume=env.getVolume(node),axis={ax,ay,az},
                                        a=a,b=b,top=math.max(top,ground),
                                        radius=radius,cutEnd=cutEnd,score=score,
                                        length=length}
                                end
                            end
                        end
                    end
                end
            end
            env.overlapSphere(hx, hy, hz, 18, "onWood", target,
                env.CollisionFlag.TREE + env.CollisionFlag.DYNAMIC_OBJECT,
                true, true, false, true)
            if best == nil then return log("tree stack REFUSED no aligned settled log with measurable top") end
            local targetY = best.top + 0.875
            self.treeStack = {vehicle=v,held=held,base=best,
                target={best.cutEnd[1],targetY,best.cutEnd[3]},elapsed=0,
                phase="position",nextLog=2000,
                initialVolume=heldVolume,
                head={hx,hy,hz},radius=auto.lastRadius or 0.25,
                beforeNodes=seen,feedHorizontal={fx,fz}}
            log("tree stack START held=%s heldVolume=%.6f base=%s baseVolume=%.6f baseXYZ=(%.3f,%.3f,%.3f) baseExtents=(%.3f,%.3f,%.3f)->(%.3f,%.3f,%.3f) baseTop=%.3f baseRadius=%.3f feedHorizontal=(%.3f,%.3f) targetHead=(%.3f,%.3f,%.3f) headBefore=(%.3f,%.3f,%.3f)",
                tostring(held),self.treeStack.initialVolume,tostring(best.node),
                best.volume,best.x,best.y,best.z,
                best.a[1],best.a[2],best.a[3],
                best.b[1],best.b[2],best.b[3],
                best.top,best.radius,fx,fz,
                best.cutEnd[1],targetY,best.cutEnd[3],hx,hy,hz)
            return
        end
    end
    log("tree stack REFUSED no held remainder")
end

function Probe:stepTreeStackTrial(dt)
    local p, env = self.treeStack, self.env
    local v, spec = p.vehicle, p.vehicle.spec_woodHarvester
    p.elapsed = p.elapsed + dt
    v:raiseActive()
    if v:getIsAIActive() ~= false then
        for _, tool in ipairs(v.spec_cylindered.movingTools) do tool.externalMove=0 end
        log("tree stack STOPPED authority changed")
        self.treeStack=nil
        return
    end
    if p.phase == "position" then
        if spec.attachedSplitShape ~= p.held then
            log("tree stack REFUSED held shape changed before feed")
            self.treeStack=nil
            return
        end
        local auto = v.spec_automaticArmControlHarvester
        auto.lastRadius = p.radius
        v:doTreeArmAlignment(p.target[1],p.target[2],p.target[3],1)
        if p.elapsed >= p.nextLog then
            p.nextLog=p.nextLog+2000
            local x,y,z=env.getWorldTranslation(spec.cutNode)
            log("tree stack POSITION head=(%.3f,%.3f,%.3f) target=(%.3f,%.3f,%.3f) state=%s elapsedMs=%d",
                x,y,z,p.target[1],p.target[2],p.target[3],tostring(auto.state),p.elapsed)
        end
        if auto.state ~= env.AutomaticArmControlHarvester.STATE_FINISHED
                and p.elapsed < 8000 then return end
        for _, tool in ipairs(v.spec_cylindered.movingTools) do tool.externalMove=0 end
        v:setTreeArmAlignmentInput(0)
        local x,y,z=env.getWorldTranslation(spec.cutNode)
        local horizontalError=math.sqrt((x-p.target[1])^2+(z-p.target[3])^2)
        local clearance=y-p.base.top
        log("tree stack POSITION_RESULT head=(%.3f,%.3f,%.3f) stackClearance=%.3f horizontalError=%.3f status=%s",
            x,y,z,clearance,horizontalError,
            p.elapsed < 8000 and "FINISHED" or "TIMEOUT")
        if p.elapsed >= 8000 or horizontalError > 0.5
                or clearance < 0.75 or clearance > 1.0 then
            log("tree stack REFUSED measured placement not reached")
            self.treeStack=nil
            return
        end
        p.phase="feed"
    end
    if p.phase == "feed" then
        if spec.isAttachedSplitShapeMoving then return end
        local x,y,z=env.getWorldTranslation(spec.cutNode)
        local fx,fy,fz=env.localDirectionToWorld(spec.cutNode,1,0,0)
        local horizontal=math.sqrt(fx*fx+fz*fz)
        if horizontal < 0.8
                or (fx*p.feedHorizontal[1]+fz*p.feedHorizontal[2])/horizontal < 0.9 then
            log("tree stack REFUSED feed direction changed before cut")
            self.treeStack=nil
            return
        end
        p.feedHead={x,y,z}
        p.feedY=spec.attachedSplitShapeY
        p.feedDirection=spec.cutAttachDirection
        v:cutTree(spec.currentCutLength)
        p.phase="feeding"
        log("tree stack FEED held=%s head=(%.3f,%.3f,%.3f) clearance=%.3f axis=(%.3f,%.3f,%.3f) nativeLength=%.3f direction=%s feedY=%s targetY=%s",
            tostring(p.held),x,y,z,y-p.base.top,fx,fy,fz,spec.currentCutLength,
            tostring(p.feedDirection),tostring(p.feedY),
            tostring(spec.attachedSplitShapeTargetY))
        return
    end
    if p.phase == "feeding" then
        if p.elapsed >= p.nextLog then
            p.nextLog=p.nextLog+2000
            local x,y,z=env.getWorldTranslation(spec.cutNode)
            log("tree stack FEED_PROGRESS head=(%.3f,%.3f,%.3f) feedY=%s targetY=%s moving=%s held=%s elapsedMs=%d",
                x,y,z,tostring(spec.attachedSplitShapeY),
                tostring(spec.attachedSplitShapeTargetY),
                tostring(spec.isAttachedSplitShapeMoving),
                tostring(spec.attachedSplitShape),p.elapsed)
        end
        if spec.isAttachedSplitShapeMoving and p.elapsed < 25000 then return end
        if not spec.automaticCuttingEnabled and spec.attachedSplitShape ~= nil then
            v:cutTree(0)
        end
        p.phase="settle"
        p.settleMs=0
        log("tree stack FEED_END held=%s feedY=%s originalHeld=%s timer=%s",
            tostring(spec.attachedSplitShape),tostring(spec.attachedSplitShapeY),
            tostring(p.held),tostring(spec.cutTimer))
        return
    end
    if p.phase == "settle" then
        p.settleMs=p.settleMs+dt
        if p.settleMs < 4000 then return end
        local logs, seen = {}, {}
        local target = {}
        function target.onWood(_, node)
            if seen[node] or not env.getHasClassId(node,env.ClassIds.MESH_SPLIT_SHAPE)
                    or env.getRigidBodyType(node)==env.RigidBodyType.STATIC then return end
            seen[node]=true
            local x,y,z=env.getWorldTranslation(node)
            if math.sqrt((x-p.base.x)^2+(z-p.base.z)^2)<15 then
                local ax,ay,az=env.localDirectionToWorld(node,0,1,0)
                local below,above=env.getSplitShapePlaneExtents(node,x,y,z,ax,ay,az)
                if below~=nil and above~=nil then
                    local a={x-ax*below,y-ay*below,z-az*below}
                    local b={x+ax*above,y+ay*above,z+az*above}
                    local mid={(a[1]+b[1])*0.5,(a[2]+b[2])*0.5,
                        (a[3]+b[3])*0.5}
                    local _,_,_,_,_,_,radius=
                        env.SplitShapeUtil.getTreeOffsetPosition(
                            node,mid[1],mid[2],mid[3],1.5)
                    local vx,vy,vz=env.getLinearVelocity(node)
                    logs[#logs+1]={node=node,x=x,y=y,z=z,
                        volume=env.getVolume(node),a=a,b=b,
                        mid=mid,radius=radius or 0,
                        speed=math.sqrt(vx*vx+vy*vy+vz*vz)}
                end
            end
        end
        env.overlapSphere(p.base.x,p.target[2],p.base.z,18,"onWood",target,
            env.CollisionFlag.TREE+env.CollisionFlag.DYNAMIC_OBJECT,
            true,true,false,true)
        local base, released, held, totalVolume = nil,nil,nil,0
        for _, item in ipairs(logs) do
            if item.node==p.base.node then base=item end
            if item.node==spec.attachedSplitShape then held=item end
            if not p.beforeNodes[item.node] and item.node~=spec.attachedSplitShape then
                if released==nil or item.volume>released.volume then released=item end
            end
            if item.node==p.base.node or item.node==spec.attachedSplitShape
                    or not p.beforeNodes[item.node] then
                totalVolume=totalVolume+item.volume
            end
            log("tree stack WOOD node=%s xyz=(%.3f,%.3f,%.3f) volume=%.6f extents=(%.3f,%.3f,%.3f)->(%.3f,%.3f,%.3f) radius=%.3f speed=%.4f held=%s new=%s",
                tostring(item.node),item.x,item.y,item.z,item.volume,
                item.a[1],item.a[2],item.a[3],
                item.b[1],item.b[2],item.b[3],item.radius,item.speed,
                tostring(item.node==spec.attachedSplitShape),
                tostring(not p.beforeNodes[item.node]))
        end
        if base~=nil and released~=nil then
            local bx,bz=p.base.axis[1],p.base.axis[3]
            local bl=math.sqrt(bx*bx+bz*bz)
            bx,bz=bx/bl,bz/bl
            local function along(pt) return pt[1]*bx+pt[3]*bz end
            local overlap=math.max(0,math.min(
                    math.max(along(base.a),along(base.b)),
                    math.max(along(released.a),along(released.b)))
                -math.max(math.min(along(base.a),along(base.b)),
                    math.min(along(released.a),along(released.b))))
            local lateral=math.abs((released.mid[1]-base.mid[1])*bz
                -(released.mid[3]-base.mid[3])*bx)
            local baseMove=math.sqrt((base.x-p.base.x)^2+(base.z-p.base.z)^2)
            local baseTop=math.max(base.a[2],base.b[2])+base.radius
            local releasedBottom=math.min(released.a[2],released.b[2])
                -released.radius
            local stacked=overlap>1 and lateral<base.radius+released.radius
                and math.abs(releasedBottom-baseTop)<0.35
                and baseMove<0.3 and base.speed<0.1 and released.speed<0.1
            log("tree stack RESULT stacked=%s overlap=%.3f lateral=%.3f baseMove=%.3f baseTop=%.3f releasedBottom=%.3f finalTop=%.3f volumeBefore=%.6f volumeAfter=%.6f volumeDelta=%.6f logs=%d held=%s elapsedMs=%d",
                tostring(stacked),overlap,lateral,baseMove,baseTop,
                releasedBottom,math.max(baseTop,
                    math.max(released.a[2],released.b[2])+released.radius),
                p.base.volume+p.initialVolume,totalVolume,
                totalVolume-(p.base.volume+p.initialVolume),
                #logs,tostring(spec.attachedSplitShape),p.elapsed)
        else
            log("tree stack RESULT UNVERIFIED base=%s released=%s held=%s logs=%d elapsedMs=%d",
                tostring(base~=nil),tostring(released~=nil),
                tostring(held~=nil),#logs,p.elapsed)
        end
        self.treeStack=nil
    end
end

-- Read-only discriminator for the powered-root arc model. All coordinates are
-- measured in each loaded vehicle's root frame; no Save 1 IDs enter selection.
function Probe:logTurnReferences()
    local env, count = self.env, 0
    for _, vehicle in pairs(vehicles(env)) do
        if vehicle.rootNode ~= nil and vehicle.spec_motorized ~= nil
                and vehicle.spec_wheels ~= nil then
            local ok, err = pcall(function()
                local id = env.NetworkUtil.getObjectId(vehicle)
                local root = vehicle.rootNode
                local function relative(node)
                    if node == nil then return "unavailable" end
                    local x, y, z = env.getWorldTranslation(node)
                    local side, _, ahead = env.worldToLocal(root, x, y, z)
                    return string.format("(%.3f,%.3f)", side, ahead)
                end
                local center = vehicle.spec_wheels and vehicle.spec_wheels.steeringCenterNode
                local aiNode = type(vehicle.getAISteeringNode) == "function"
                    and vehicle:getAISteeringNode() or nil
                local wheels = vehicle.spec_wheels and vehicle.spec_wheels.wheels or {}
                log("turn reference id=%s name=%s center=%s ai=%s rootSpeed=%s wheels=%d",
                    tostring(id), tostring(vehicle.getFullName and vehicle:getFullName() or vehicle.typeName),
                    relative(center), relative(aiNode), tostring(vehicle:getLastSpeed()), #wheels)
                for i, wheel in ipairs(wheels) do
                    log("turn reference id=%s wheel=%d repr=%s active=%s rotSpeed=%s",
                        tostring(id), i, relative(wheel.repr), tostring(wheel.isActive),
                        tostring(wheel.physics and wheel.physics.rotSpeed))
                end
                count = count + 1
            end)
            if not ok then log("turn reference unit UNVERIFIED %s", tostring(err)) end
        end
    end
    log("turn reference RESULT read-only rigs=%d", count)
end

-- Read-only: every vehicle's specializations and the fields of those that carry or mount
-- another vehicle (what a header trailer, a deck or a cutter joint actually exposes).
function Probe:logSpecInventory()
    local env = self.env
    local function fields(t, depth)
        local out = {}
        for k, v in pairs(t or {}) do
            local kind = type(v)
            if kind == "number" or kind == "boolean" or kind == "string" then
                out[#out + 1] = string.format("%s=%s", tostring(k), tostring(v))
            elseif kind == "table" and depth > 0 then
                out[#out + 1] = string.format("%s={%s}", tostring(k), fields(v, depth - 1))
            elseif kind == "table" then
                local n = 0
                for _ in pairs(v) do n = n + 1 end
                out[#out + 1] = string.format("%s=<%d>", tostring(k), n)
            end
        end
        table.sort(out)
        return table.concat(out, " ")
    end
    for _, vehicle in pairs(vehicles(env)) do
        local id = env.NetworkUtil.getObjectId(vehicle)
        local names = {}
        for _, spec in ipairs(vehicle.specializationNames or {}) do names[#names + 1] = spec end
        log("spec inventory id=%s name=%s config=%s specs=%s", tostring(id), tostring(vehicle:getName()),
            tostring(vehicle.configFileName), table.concat(names, ","))
        local function pose(node)
            if node == nil or not env.entityExists(node) then return "?" end
            local x, y, z = env.getWorldTranslation(node)
            local fx, _, fz = env.localDirectionToWorld(node, 0, 0, 1)
            return string.format("(%.3f,%.3f,%.3f yaw %.3f)", x, y, z, math.atan2(fx, fz))
        end
        local okReady, ready, blocker = pcall(function() return vehicle:getIsAIReadyToDrive() end)
        local okPrep, preparing = pcall(function() return vehicle:getIsAIPreparingToDrive() end)
        local fold = vehicle.spec_foldable
        local pipe = vehicle.spec_pipe
        log("spec inventory id=%s aiReady=%s blocker=%s preparing=%s fold=%s turnOnDir=%s pipe=%s/%s turnedOn=%s",
            tostring(id), okReady and tostring(ready) or "?",
            okReady and blocker ~= nil and tostring(env.NetworkUtil.getObjectId(blocker)) or "-",
            okPrep and tostring(preparing) or "?", fold and tostring(fold.foldAnimTime) or "-",
            fold and tostring(fold.turnOnFoldDirection) or "-",
            pipe and tostring(pipe.currentState) or "-", pipe and tostring(pipe.targetState) or "-",
            tostring(type(vehicle.getIsTurnedOn) == "function" and vehicle:getIsTurnedOn()))
        log("spec inventory id=%s root=%s mountedTo=%s", tostring(id), pose(vehicle.rootNode),
            tostring(type(vehicle.getDynamicMountObject) == "function" and vehicle:getDynamicMountObject() ~= nil
                     and env.NetworkUtil.getObjectId(vehicle:getDynamicMountObject()) or nil))
        local dma = vehicle.spec_dynamicMountAttacher
        if dma ~= nil then
            for i, lock in ipairs(dma.lockPositions or {}) do
                log("spec inventory id=%s lock %d %s joint=%s state=%s w/l/h=%s/%s/%s", tostring(id), i,
                    tostring(lock.xmlFilename), pose(lock.jointNode), tostring(lock.state),
                    tostring(lock.width), tostring(lock.length), tostring(lock.height))
            end
            local trigger = dma.dynamicMountAttacherTrigger
            if trigger ~= nil then
                log("spec inventory id=%s trigger node=%s joint=%s root=%s allowed=%s", tostring(id), pose(trigger.triggerNode),
                    pose(trigger.jointNode), pose(trigger.rootNode),
                    tostring(type(vehicle.getAllowDynamicMountObjects) == "function" and vehicle:getAllowDynamicMountObjects()))
            end
        end
        for i, joint in ipairs(type(vehicle.getAttacherJoints) == "function" and vehicle:getAttacherJoints() or {}) do
            log("spec inventory id=%s output %d type=%s at %s", tostring(id), i, tostring(joint.jointType), pose(joint.jointTransform))
        end
        local attachable = vehicle.spec_attachable
        for i, input in ipairs(attachable and attachable.inputAttacherJoints or {}) do
            log("spec inventory id=%s input %d type=%s at %s", tostring(id), i, tostring(input.jointType), pose(input.node))
        end
        for _, key in ipairs({ "spec_dynamicMountAttacher", "spec_dynamicMountable", "spec_attacherJoints",
                               "spec_tensionBelts", "spec_cutter", "spec_attachable", "spec_foldable" }) do
            local spec = vehicle[key]
            if spec ~= nil and (key ~= "spec_attachable" or vehicle.spec_cutter ~= nil) then
                log("spec inventory id=%s %s %s", tostring(id), key, string.sub(fields(spec, 2), 1, 3000))
            end
        end
    end
    log("spec inventory RESULT read-only")
end

function Probe:logMachineInventory()
    local env = self.env
    for _, vehicle in pairs(vehicles(env)) do
        local machine = vehicle.spec_machine
        if machine ~= nil then
            log("terra inventory id=%s config=%s type=%s enabled=%s active=%s available=%s canActivate=%s inputMode=%s interval=%s areaActive=%s fill=%s",
                tostring(env.NetworkUtil.getObjectId(vehicle)), tostring(vehicle.configFileName),
                tostring(machine.machineTypeId), tostring(machine.enabled), tostring(machine.active),
                tostring(type(vehicle.getIsAvailable) == "function" and vehicle:getIsAvailable()),
                tostring(type(vehicle.getCanActivateMachine) == "function" and vehicle:getCanActivateMachine()),
                tostring(machine.inputMode), tostring(machine.updateInterval),
                tostring(machine.workArea and machine.workArea.isAreaNodeActive),
                tostring(machine.fillUnit and machine.fillUnit.fillLevel))
            for i, node in ipairs(machine.workArea and machine.workArea.areaNodes or {}) do
                local x, y, z = env.getWorldTranslation(node)
                local terrain = env.getTerrainHeightAtWorldPos(env.g_terrainNode, x, 0, z)
                log("terra area id=%s node=%d xyz=(%.3f,%.3f,%.3f) terrain=%.3f delta=%.3f",
                    tostring(env.NetworkUtil.getObjectId(vehicle)), i, x, y, z, terrain, y-terrain)
            end
        end
        local spec = vehicle.spec_cylindered
        local tools = spec and spec.movingTools or nil
        if tools ~= nil and #tools >= 3 then
            local id = env.NetworkUtil.getObjectId(vehicle)
            local x, _, z = env.getWorldTranslation(vehicle.rootNode)
            log("machine inventory id=%s config=%s pos=(%.2f,%.2f) speed=%s ai=%s tools=%d",
                tostring(id), tostring(vehicle.configFileName), x, z,
                tostring(vehicle:getLastSpeed()), tostring(vehicle:getIsAIActive()), #tools)
            for index, tool in ipairs(tools) do
                local rx, ry, rz = env.getRotation(tool.node)
                local tx, ty, tz = env.getTranslation(tool.node)
                local wx, wy, wz = env.getWorldTranslation(tool.node)
                local active = type(vehicle.getIsMovingToolActive) == "function"
                    and vehicle:getIsMovingToolActive(tool) or nil
                log("machine tool id=%s index=%d node=%s active=%s axis=%s rotAxis=%s transAxis=%s rot=%.4f,%.4f,%.4f trans=%.3f,%.3f,%.3f world=%.3f,%.3f,%.3f limits=%s,%s speed=%s target=%s",
                    tostring(id), index, tostring(env.getName(tool.node)), tostring(active),
                    tostring(tool.axis), tostring(tool.rotationAxis), tostring(tool.translationAxis),
                    rx, ry, rz, tx, ty, tz, wx, wy, wz,
                    tostring(tool.rotMin), tostring(tool.rotMax), tostring(tool.rotSpeed),
                    tostring(tool.curTargetPosition))
            end
        end
    end
    log("machine inventory RESULT read-only")
end

-- One development-only physical discriminator. A fixed short right arc is
-- offered at the nearest idle powered vehicle; only the measured articulated
-- prediction, never the aggregate rectangle, can authorize its execution.
-- Shadow-only: advance the native steering reference on the commanded arc and
-- derive the root pose from its measured offset. This cannot authorize motion.
function Probe.predictRootFromTurnReference(x, z, yaw, side, ahead, length, curvature)
    local sn, cs = math.sin(yaw), math.cos(yaw)
    local cx, cz = x + cs * side + sn * ahead, z - sn * side + cs * ahead
    local endYaw = yaw + length * curvature
    local endX, endZ
    if math.abs(curvature) < 0.00001 then
        endX, endZ = cx + sn * length, cz + cs * length
    else
        endX = cx + (cs - math.cos(endYaw)) / curvature
        endZ = cz + (math.sin(endYaw) - sn) / curvature
    end
    return endX - math.cos(endYaw) * side - math.sin(endYaw) * ahead,
           endZ + math.sin(endYaw) * side - math.cos(endYaw) * ahead, endYaw
end

function Probe:startMotionProof(mode)
    local env, bridge, H = self.env, self.Bridge, self.Hitch
    if self.motion ~= nil or self.task ~= nil or bridge.active ~= nil then
        log("motion proof REFUSED busy") return
    end
    local second = mode == "second_forward" or mode == "second_reverse"
    local wantedUnits = second and 2 or 4
    local best, count = nil, 0
    for _, vehicle in pairs(vehicles(env)) do
        if vehicle.rootNode ~= nil and type(vehicle.getAttachedImplements) == "function"
                and #(vehicle:getAttachedImplements() or {}) > 0 then
            local candidate = H.routeFootprint(env, vehicle)
            log("motion proof candidate id=%s motor=%s ai=%s speed=%s units=%s wheelReprMeasured=%s",
                tostring(env.NetworkUtil.getObjectId(vehicle)), tostring(vehicle.spec_motorized ~= nil),
                tostring(vehicle:getIsAIActive()), tostring(vehicle:getLastSpeed()),
                tostring(candidate and #candidate.units), tostring(candidate and candidate.towMeasured))
            if candidate ~= nil and #candidate.units == wantedUnits and candidate.towMeasured == true then
                best, count = vehicle, count + 1
            end
        end
    end
    if count ~= 1 then
        log("motion proof REFUSED expected one measured %d-unit rig, found %d", wantedUnits, count) return
    end
    if best.spec_motorized == nil or best:getIsAIActive() ~= false
            or math.abs(best:getLastSpeed()) >= 0.5 then
        log("motion proof REFUSED selected rig is not an idle powered vehicle") return
    end
    if H.ghosted[best] ~= nil then log("motion proof REFUSED scenery collision disabled") return end
    local foot = H.routeFootprint(env, best)
    if foot == nil or #foot.units < 2 or foot.towMeasured ~= true then
        log("motion proof REFUSED rig geometry unreadable") return
    end
    local length, curve = 2, 0.08991666162548993
    if mode == "reverse" or mode == "second_reverse" then
        length, curve = -0.5, -0.08991666162548993
    end
    local samples = H.predictShortMotion(env, foot, length, curve)
    local clear, reason = H.predictedUnitsClear(env, foot, samples)
    local x, _, z = env.getWorldTranslation(best.rootNode)
    local fx, _, fz = env.localDirectionToWorld(best.rootNode, 0, 0, 1)
    local yaw = math.atan2 and math.atan2(fx, fz) or math.atan(fx, fz)
    local endpoint = samples and samples[#samples].poses or nil
    local shadow
    local turnNode = best.spec_wheels and best.spec_wheels.steeringCenterNode
    if turnNode ~= nil then
        local nx, ny, nz = env.getWorldTranslation(turnNode)
        local side, _, ahead = env.worldToLocal(best.rootNode, nx, ny, nz)
        local sx, sz, syaw = Probe.predictRootFromTurnReference(
            x, z, yaw, side, ahead, length, curve)
        shadow = { x = sx, z = sz, yaw = syaw, side = side, ahead = ahead,
            poses = H.predictFromRootPose(env, foot, sx, sz, syaw) }
        local shadowSamples = {}
        local steps = math.max(1, math.ceil(math.abs(length) / 0.25))
        for i = 1, steps do
            local d = length * i / steps
            local px, pz, pyaw = Probe.predictRootFromTurnReference(
                x, z, yaw, side, ahead, d, curve)
            local poses = H.predictFromRootPose(env, foot, px, pz, pyaw)
            if poses == nil then break end
            shadowSamples[#shadowSamples + 1] = { distance = d, poses = poses }
        end
        if #shadowSamples == steps then
            local candidateClear, candidateReason = H.predictedUnitsClear(env, foot, shadowSamples)
            shadow.clear, shadow.reason = candidateClear, candidateReason
        end
        log("motion proof turnShadow reference=(%.3f,%.3f) root=(%.3f,%.3f,%.5f) units=%s",
            side, ahead, sx, sz, syaw, tostring(shadow.poses and #shadow.poses))
        log("motion proof turnCandidate samples=%d clear=%s reason=%s",
            #shadowSamples, tostring(shadow.clear), tostring(shadow.reason))
    else
        log("motion proof turnShadow UNAVAILABLE no steering center")
    end
    local aggregate, aggregateReason
    local localClear, localReason
    if endpoint ~= nil then
        aggregate, aggregateReason = H.routePoseClear(env, foot,
            endpoint[1].x, endpoint[1].z, endpoint[1].yaw, curve, length < 0)
        local localDrive = env.FarmhandDrive
        if localDrive ~= nil then
            localClear, localReason = localDrive.routeProbe(env, foot, H,
                endpoint[1].x, endpoint[1].z, endpoint[1].yaw, curve,
                length < 0 and -1 or 1)
        end
    end
    log("motion proof pre mode=%s id=%s root=(%.3f,%.3f,%.5f) units=%d wheelReprMean=%.3f aggregate=%s:%s articulated=%s:%s",
        mode, tostring(env.NetworkUtil.getObjectId(best)), x, z, yaw, #foot.units, foot.towLength,
        tostring(aggregate), tostring(aggregateReason), tostring(clear), tostring(reason))
    log("motion proof localRouteProbe=%s:%s", tostring(localClear), tostring(localReason))
    if endpoint ~= nil then
        for i, pose in ipairs(endpoint) do
            local ux, _, uz = env.getWorldTranslation(pose.unit.rootNode)
            local ufx, _, ufz = env.localDirectionToWorld(pose.unit.rootNode, 0, 0, 1)
            log("motion proof unit%d id=%s observed=(%.3f,%.3f,%.5f) predicted=(%.3f,%.3f,%.5f)",
                i, tostring(env.NetworkUtil.getObjectId(pose.unit)), ux, uz,
                math.atan2 and math.atan2(ufx, ufz) or math.atan(ufx, ufz),
                pose.x, pose.z, pose.yaw)
        end
    end
    if clear ~= true or localClear ~= true or (shadow ~= nil and shadow.clear ~= true)
            or (not second and aggregate ~= false) then
        log("motion proof REFUSED requires articulated/local/turnCandidate CLEAR%s",
            second and "" or " and aggregate BLOCKED") return
    end
    if endpoint == nil then log("motion proof REFUSED no articulated endpoint") return end
    local motion = { tractor = best, foot = foot, expected = endpoint,
        shadow = shadow,
        x = x, z = z, yaw = yaw, length = length, curvature = curve,
        elapsed = 0, maxSpeedKmh = 3, lastProgress = 0, lastProgressAt = 0,
        mode = mode }
    self.motion = motion
    H.watchDriver(env, best)
    H.holdActive(motion)
    log("motion proof EXECUTE mode=%s length=%.2f curve=%.5f normal physics, no navigation provider",
        mode, length, curve)
end

function Probe:finishMotionProof(code)
    local m = self.motion
    if m == nil then return end
    if m.mode == "tracked" and m.finishing == nil then
        log("tracked motion finish requested code=%s yaw=%s progress=%.3f",
            tostring(code), tostring(m.yaw), m.lastProgress or -1)
    end
    self.Hitch.halt(self.env, m, 16)
    if m.finishing == nil then
        m.finishing, m.finishCode, m.brakeElapsed = true, code, 0
    end
end

function Probe:completeMotionProof()
    local m = self.motion
    if m == nil then return end
    local env = self.env
    if m.mode == "tracked" and type(m.yaw) ~= "number" then
        log("tracked motion UNVERIFIED completion state lost yaw; forcing halt and cleanup")
        self.Hitch.halt(env, m, 16)
        if m.startedMotor and type(m.tractor.stopMotor) == "function" then
            pcall(m.tractor.stopMotor, m.tractor)
        end
        self.Hitch.disarm(m.tractor)
        self.motion = nil
        return
    end
    if m.mode == "tracked" then
        local x, _, z = env.getWorldTranslation(m.tractor.rootNode)
        local fx, _, fz = env.localDirectionToWorld(m.tractor.rootNode, 0, 0, 1)
        local yaw = math.atan2 and math.atan2(fx, fz) or math.atan(fx, fz)
        local dx, dz = x - m.x, z - m.z
        local along = dx * math.sin(m.yaw) + dz * math.cos(m.yaw)
        local lateral = dx * math.cos(m.yaw) - dz * math.sin(m.yaw)
        log("tracked motion displacement id=%s start=(%.3f,%.3f,%.5f) end=(%.3f,%.3f,%.5f) distance=%.3f along=%.3f lateral=%.3f yawDelta=%.5f",
            tostring(env.NetworkUtil.getObjectId(m.tractor)), m.x, m.z, m.yaw,
            x, z, yaw, math.sqrt(dx^2+dz^2), along, lateral, yaw-m.yaw)
        log("tracked motion terminal motor=%s state=%s rpm=%s cruise=%s speedReal=%s firstUpdate=%s",
            tostring(m.tractor:getIsMotorStarted()),
            tostring(m.tractor:getMotorState()),
            tostring(m.tractor:getMotorRpmReal()),
            tostring(m.tractor:getCruiseControlState()),
            tostring(m.tractor.lastSpeedReal),
            tostring(m.tractor.finishedFirstUpdate))
    end
    for i, pose in ipairs(m.expected or {}) do
        local x, _, z = env.getWorldTranslation(pose.unit.rootNode)
        local fx, _, fz = env.localDirectionToWorld(pose.unit.rootNode, 0, 0, 1)
        local yaw = math.atan2 and math.atan2(fx, fz) or math.atan(fx, fz)
        log("motion proof result unit%d id=%s actual=(%.3f,%.3f,%.5f) predicted=(%.3f,%.3f,%.5f) posError=%.3f yawError=%.5f",
            i, tostring(env.NetworkUtil.getObjectId(pose.unit)), x, z, yaw,
            pose.x, pose.z, pose.yaw, math.sqrt((x-pose.x)^2+(z-pose.z)^2), yaw-pose.yaw)
        local shadow = m.shadow and m.shadow.poses and m.shadow.poses[i]
        if shadow ~= nil then
            log("motion proof turnShadowResult unit%d id=%s predicted=(%.3f,%.3f,%.5f) posError=%.3f yawError=%.5f",
                i, tostring(env.NetworkUtil.getObjectId(pose.unit)), shadow.x, shadow.z, shadow.yaw,
                math.sqrt((x-shadow.x)^2+(z-shadow.z)^2), yaw-shadow.yaw)
        end
    end
    log("motion proof RESULT mode=%s %s envelopeOverlap=%s speed=%.3f", m.mode, m.finishCode,
        tostring(self.Hitch.rigContacts(env, m.tractor, m.foot)),
        m.tractor:getLastSpeed())
    if m.startedMotor and type(m.tractor.stopMotor) == "function" then
        m.tractor:stopMotor()
    end
    self.Hitch.disarm(m.tractor)
    self.motion = nil
end

-- Save 1 diagnostic only: a bounded native drive command on a different
-- locomotion family. It reuses the motion supervisor and normal collision.
function Probe:startTrackedMotionProof(id, length, curve, contactDeparture, method)
    local env, H = self.env, self.Hitch
    if self.motion ~= nil or self.actuator ~= nil or self.terraPulse ~= nil
            or self.task ~= nil or self.Bridge.active ~= nil then
        log("tracked motion REFUSED busy") return
    end
    local vehicle = env.NetworkUtil.getObject(id)
    if vehicle == nil or vehicle.rootNode == nil or vehicle.spec_motorized == nil
            or vehicle.spec_wheels == nil or vehicle:getIsAIActive() ~= false
            or math.abs(vehicle:getLastSpeed()) >= 0.5 or H.ghosted[vehicle] ~= nil then
        log("tracked motion REFUSED capability/idle/physics id=%s", tostring(id)) return
    end
    local foot = H.routeFootprint(env, vehicle)
    if foot == nil then log("tracked motion REFUSED unreadable rig") return end
    if id == 628 then
        local size = vehicle.size or {}
        log("tracked motion footprint width=%s length=%s widthOffset=%s lengthOffset=%s bounds=(%.2f,%.2f,%.2f,%.2f)",
            tostring(size.width), tostring(size.length), tostring(size.widthOffset),
            tostring(size.lengthOffset), foot.aMin, foot.aMax, foot.lMin, foot.lMax)
    end
    local x, _, z = env.getWorldTranslation(vehicle.rootNode)
    local fx, _, fz = env.localDirectionToWorld(vehicle.rootNode, 0, 0, 1)
    local yaw = math.atan2 and math.atan2(fx, fz) or math.atan(fx, fz)
    local startContact = H.rigContacts(env, vehicle, foot)
    if id == 628 then
        log("tracked motion surface ahead %s", tostring(H.surfaceScan(env, x, z, yaw, 4, { -2, 0, 2 })))
    end
    if contactDeparture and not startContact:find("standing", 1, true) then
        log("tracked motion REFUSED departure has no standing envelope overlap") return
    end
    local other, cleared = nil, false
    local steps = contactDeparture and math.max(2, math.ceil(math.abs(length) / 0.25)) or 2
    for i = 1, steps do
        local d = length * i / steps
        local px, pz, pyaw = Probe.predictRootFromTurnReference(x, z, yaw, 0, 0, d, curve)
        local clear, reason, hits = H.routePoseClear(env, foot, px, pz, pyaw, curve, length < 0)
        log("tracked motion preflight d=%.2f clear=%s reason=%s", d, tostring(clear), tostring(reason))
        if clear == true then
            cleared = true
        elseif contactDeparture and clear == false and not cleared and hits ~= nil then
            for _, node in ipairs(hits) do
                local object = env.g_currentMission:getNodeObject(node)
                local root = object and (object.rootVehicle or object)
                local obstacle = root or node
                local obstacleNode = root and root.rootNode or node
                if obstacle == vehicle or obstacleNode == nil
                        or (other ~= nil and other ~= obstacle) then
                    log("tracked motion REFUSED new/unowned obstacle d=%.2f", d) return
                end
                other = obstacle
                local ox, _, oz = env.getWorldTranslation(obstacleNode)
                if (px-ox)^2 + (pz-oz)^2 <= (x-ox)^2 + (z-oz)^2 + 0.01 then
                    log("tracked motion REFUSED not departing contact d=%.2f", d) return
                end
            end
        else
            if id == 628 and hits ~= nil then
                for _, node in ipairs(hits) do
                    local object = env.g_currentMission:getNodeObject(node)
                    local ox, oy, oz = env.getWorldTranslation(node)
                    log("tracked motion blocked node=%s object=%s at=(%.3f,%.3f,%.3f) startContact=%s",
                        tostring(node), tostring(object), ox, oy, oz,
                        tostring(startContact))
                end
                for _, shrink in ipairs({ 0.25, 0.5, 0.75 }) do
                    local small = {}
                    for k, v in pairs(foot) do small[k] = v end
                    small.aMin, small.aMax = foot.aMin + shrink, foot.aMax - shrink
                    small.lMin, small.lMax = foot.lMin + shrink, foot.lMax - shrink
                    local narrower, why = H.routePoseClear(env, small, px, pz, pyaw, curve, length < 0)
                    log("tracked motion margin discriminator shrink=%.2f clear=%s reason=%s",
                        shrink, tostring(narrower), tostring(why))
                end
            end
            log("tracked motion REFUSED predicted d=%.2f clear=%s reason=%s", d,
                tostring(clear), tostring(reason)) return
        end
    end
    if contactDeparture and not cleared then
        if other == nil then log("tracked motion REFUSED no confirmed departure obstacle") return end
        log("tracked motion guarded departure: broad footprint still overlaps same standing obstacle")
    end
    if not vehicle:getIsMotorStarted() and not vehicle:getCanMotorRun() then
        log("tracked motion REFUSED motor unavailable") return
    end
    local startedMotor = not vehicle:getIsMotorStarted()
    if startedMotor then vehicle:startMotor() end
    log("tracked motion startMotor id=%s startedByProbe=%s state=%s started=%s rpm=%s firstUpdate=%s",
        tostring(id), tostring(startedMotor), tostring(vehicle:getMotorState()),
        tostring(vehicle:getIsMotorStarted()), tostring(vehicle:getMotorRpmReal()),
        tostring(vehicle.finishedFirstUpdate))
    self.motion = { tractor=vehicle, foot=foot, expected={}, x=x, z=z, yaw=yaw,
        length=length, curvature=curve, elapsed=0,
        maxSpeedKmh=method=="point_low" and 1.5
            or method=="production" and 3 or 2,
        lastProgress=0, lastProgressAt=0, mode="tracked", startedMotor=startedMotor,
        contactDeparture=contactDeparture, method=method,
        commandShare=method=="production" and 0.5 or nil }
    H.watchDriver(env, vehicle)
    H.holdActive(self.motion)
    log("tracked motion EXECUTE id=%s units=%d length=%.2f curve=%.3f normal physics",
        tostring(id), #foot.units, length, curve)
end

function Probe:stepMotionProof(dt)
    local env, H, m = self.env, self.Hitch, self.motion
    m.elapsed = m.elapsed + dt
    if m.finishing then
        m.brakeElapsed = m.brakeElapsed + dt
        H.halt(env, m, dt)
        if (math.abs(m.tractor:getLastSpeed()) < 0.5 and m.brakeElapsed > 500)
                or m.brakeElapsed > 4000 then self:completeMotionProof() end
        return
    end
    if m.elapsed > 12000 or self.Bridge.active ~= nil or m.tractor:getIsAIActive() ~= false
            or H.driverConflict(m.tractor) ~= nil then
        self:finishMotionProof("FAILED authority/timeout") return
    end
    local x, _, z = env.getWorldTranslation(m.tractor.rootNode)
    local dx, dz = x - m.x, z - m.z
    local travelled = math.sqrt(dx^2+dz^2)
    local lateral = 0
    if m.mode == "tracked" then
        local along = dx * math.sin(m.yaw) + dz * math.cos(m.yaw)
        lateral = dx * math.cos(m.yaw) - dz * math.sin(m.yaw)
        travelled = (m.length < 0 and -1 or 1) * along
        if m.contactDeparture and math.abs(lateral) > 0.25 + 0.25 * math.max(0, travelled) then
            self:finishMotionProof("FAILED lateral departure") return
        end
    end
    if travelled >= math.abs(m.length) - 0.1 then self:finishMotionProof("DONE") return end
    if travelled > m.lastProgress + 0.05 then
        m.lastProgress, m.lastProgressAt = travelled, m.elapsed
    elseif m.elapsed - m.lastProgressAt > 3500 then
        self:finishMotionProof("FAILED no progress") return
    end
    local speed = m.tractor:getLastSpeed()
    if m.mode == "tracked" and m.elapsed >= (m.nextDiagnosticAt or 0) then
        m.nextDiagnosticAt = m.elapsed + 1000
        local rootX, rootY, rootZ = env.getWorldTranslation(m.tractor.rootNode)
        local groundY = env.getTerrainHeightAtWorldPos(env.g_terrainNode, rootX, 0, rootZ)
        log("tracked motion telemetry t=%d progress=%.3f lateral=%.3f speed=%.3f motor=%s state=%s rpm=%s cruise=%s speedReal=%s rootAboveTerrain=%.3f",
            m.elapsed, travelled, lateral, speed, tostring(m.tractor:getIsMotorStarted()),
            tostring(m.tractor:getMotorState()), tostring(m.tractor:getMotorRpmReal()),
            tostring(m.tractor:getCruiseControlState()),
            tostring(m.tractor.lastSpeedReal), rootY-groundY)
    end
    local magnitude = m.mode == "tracked" and 1 or 0.22
    local throttle = speed > 2.3 and 0 or (m.length < 0 and -magnitude or magnitude)
    -- GIANTS curvature sign is opposite the +yaw arc convention used above.
    -- Save 1 live arc: requested -0.30 moved the truck +0.21 rad over 1.4 m.
    if m.method == "production" then
        H.drive(env,m,dt,false,0,-10)
    elseif m.method == "point" or m.method == "point_low" then
        env.AIVehicleUtil.driveToPoint(
            m.tractor,dt,math.abs(throttle),true,m.length>0,0,
            m.length>0 and 10 or -10,m.maxSpeedKmh)
    else
        env.AIVehicleUtil.driveAlongCurvature(
            m.tractor, dt, -m.curvature, m.maxSpeedKmh, throttle)
    end
end

-- -------------------------------------------------------- mod lifecycle

function Probe:loadMap()
    self.env = getfenv(0)   -- the game's global table, as AutoDrive register.lua:329
    for _, c in ipairs(Probe.COMMANDS) do addConsoleCommand(c[1], c[2], c[3], self) end
    log("hitch probe loaded; nothing runs until fhHitchList / fhHitchProbe is typed")
    log("motion proof listener loaded")
end

function Probe:deleteMap()
    if self.terraPulse ~= nil then
        pcall(self.terraPulse.vehicle.setMachineActive, self.terraPulse.vehicle, false)
        if self.terraPulse.originalState ~= nil then
            pcall(self.terraPulse.vehicle.setMachineState,
                self.terraPulse.vehicle, self.terraPulse.originalState)
        end
        pcall(self.Hitch.halt, self.env, self.terraPulse, 16)
        if self.terraPulse.startedMotor then
            pcall(self.terraPulse.tractor.stopMotor, self.terraPulse.tractor)
        end
        self.Hitch.disarm(self.terraPulse.tractor)
        self.terraPulse = nil
    end
    if self.actuator ~= nil then
        self.actuator.tool.curTargetPosition = nil
        self.actuator.tool.curTargetDirection = nil
        if self.actuator.startedMotor and type(self.actuator.vehicle.stopMotor) == "function" then
            pcall(self.actuator.vehicle.stopMotor, self.actuator.vehicle)
        end
        self.actuator = nil
    end
    if self.motion ~= nil then
        pcall(Probe.finishMotionProof, self, "STOPPED map unload")
        pcall(Probe.completeMotionProof, self)
    end
    if self.task ~= nil then pcall(Probe.consoleStop, self) end
    for _, c in ipairs(Probe.COMMANDS) do removeConsoleCommand(c[1]) end
end

-- FS25 discards a sourceFile's return value (see FarmhandObserve.lua); the
-- modDesc loads FarmhandObserve, FarmhandHitch and FarmhandBridge before this.
Probe.Observe = FarmhandObserve
Probe.Hitch = FarmhandHitch
Probe.Bridge = FarmhandBridge
local previousProbe = FarmhandProbe
if FarmhandReloading and type(FarmhandObserve) == "table" and type(FarmhandObserve.adoptState) == "function" then
    FarmhandObserve.adoptState(FarmhandProbe, Probe)            -- see Observe.adoptState
end
FarmhandProbe = Probe
if previousProbe == nil and type(addModEventListener) == "function" then addModEventListener(Probe) end
return Probe
