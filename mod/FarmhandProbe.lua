-- DEVELOPMENT-ONLY probe for the first FS25 HITCH experiment. It is not the
-- production farmhand: no companion, no IPC, nothing automatic. Nothing happens
-- until the player types one of these console commands (server side only):
--
--   fhHitchList
--       read-only: owned vehicles with name, store category, forage-wagon spec,
--       position, attachment, motor/PTO values and hitch joints.
--   fhHitchProbe <tractorId> <wagonId> [assistRadiusM] [maxVerticalM] [maxSpeedKmh]
--       one bounded attempt: validate -> plan -> GIANTS GOTO staging -> verify
--       staging -> ALIGN_REVERSE -> exact candidate -> attach -> verify. Ends
--       with exactly one "RESULT PASS|FAILED|UNVERIFIED <reason>" line.
--   fhHitchStop
--       abort the running attempt (brake, cruise off, cancel its own GOTO).
--
-- Output goes to log.txt prefixed "[Farmhand]". Console and listener hooks follow
-- Courseplay ConsoleCommands.lua:40,47 and Farmhand Companion Farmhand.lua:403.

local Probe = {}

Probe.COMMANDS = {
    { "fhHitchList", "Farmhand dev: list owned vehicles for the hitch probe", "consoleList" },
    { "fhHitchProbe", "Farmhand dev: fhHitchProbe <tractorId> <wagonId> [assistRadiusM] [maxVerticalM] [maxSpeedKmh]",
      "consoleProbe" },
    { "fhHitchStop", "Farmhand dev: abort the running hitch probe", "consoleStop" },
}
-- Mirrors of companion constants (checked by tests/test_lua_probe.py).
Probe.STAGING_DISTANCE_M = 4.0           -- companion/hitch.py STAGING_DISTANCE_M
Probe.STAGE_POSITION_TOLERANCE_M = 1.0   -- executor STAGE_POSITION_TOLERANCE_M
Probe.STAGE_YAW_TOLERANCE_DEG = 10       -- executor STAGE_YAW_TOLERANCE_RAD
Probe.PLAN_TOLERANCE_M = 0.25            -- executor WAGON_MOVED_TOLERANCE_M
Probe.STAGE_TIMEOUT_MS = 120000
Probe.SETTLE_TIMEOUT_MS = 5000

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
    for _, out in ipairs(tractor.output_joints) do
        for _, inp in ipairs(wagon.input_joint_poses) do
            if out.free and inp.active and out.type == inp.type and out.z < 0
                    and (need == 0 or ptoConnects(out.pto_names, inp.pto_names, null))
                    and (best == nil or out.index < best[1].index
                         or (out.index == best[1].index and inp.index < best[2].index)) then
                best = { out, inp }
            end
        end
    end
    if best == nil then return nil end
    local out, inp = best[1], best[2]
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

-- Fresh records for the pair; wagon PTO inputs are probed with the farm's names.
local function observePair(self, tractor, wagon)
    local env, O = self.env, self.Observe
    local names = O.farmPtoNames(vehicles(env), env.g_currentMission:getFarmId())
    return O.vehicle(env, tractor, names), O.vehicle(env, wagon, names)
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

-- Single exit: exactly one RESULT line per attempt, then the probe is idle.
function Probe:finish(status, reason)
    local task = self.task
    self.token = (self.token or 0) + 1        -- every isReserved() is now false
    if task ~= nil and task.session ~= nil then
        self.Hitch.alignStep(self.env, task.session, 0)   -- latched: brake + cruise off only
    end
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
        local names = O.farmPtoNames(vehicles(env), farmId)
        for _, v in pairs(vehicles(env)) do
            if v.ownerFarmId == farmId then
                local r = O.vehicle(env, v, names)
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

-- Idle, stopped, unattached, observed, own farm. Returns nil or a reason.
local function unsafe(r, farmId, null)
    if r.farm_id ~= farmId then return "NOT_YOUR_FARM" end
    if r.entered ~= false then return "OCCUPIED_OR_UNKNOWN" end
    if r.ai_active ~= false or (r.has_autodrive and r.ad_active ~= false) then return "AI_ACTIVE_OR_UNKNOWN" end
    if not r.attachment_observed or r.attacher_vehicle_id ~= null or #r.implement_ids > 0 then
        return "ATTACHED_OR_UNKNOWN"
    end
    if r.speed_kmh == null or math.abs(r.speed_kmh) >= 1 then return "MOVING_OR_UNKNOWN" end
    return nil
end

function Probe:consoleProbe(tractorId, wagonId, radius, vertical, speed)
    if self.task ~= nil then log("fhHitchProbe: BUSY - one attempt at a time (fhHitchStop aborts)") return end
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
        local started, reason = self.Hitch.startStaging(env, tractor, farmId, plan.staging.x, plan.staging.z,
                                                        plan.staging.yaw)
        if not started then return self:finish("FAILED", "STAGE_" .. tostring(reason)) end
        self.task = { phase = "stage", tractor = tractor, wagon = wagon, farmId = farmId, plan = plan,
                      args = args, elapsed = 0, seenActive = false }
    end)
    if not ok then
        log("fhHitchProbe error: %s", tostring(err))
        self:finish("UNVERIFIED", "UNKNOWN_OBSERVATION")
    end
end

function Probe:consoleStop()
    local task = self.task
    if task == nil then log("fhHitchStop: nothing running") return end
    -- Only the GOTO this probe started and saw running is cancelled.
    if task.phase == "stage" and task.seenActive then
        self.Hitch.cancelStaging(self.env, task.tractor)
    end
    self:finish("FAILED", "STOPPED_BY_USER")
end

-- ---------------------------------------------------------- per frame

local function stageEnded(self, task)
    local env = self.env
    local node = task.tractor:getAIRootNode()
    local x, _, z = env.getWorldTranslation(node)
    local dx, _, dz = env.localDirectionToWorld(node, 0, 0, 1)
    local root = { x = x, z = z, yaw = env.MathUtil.getYRotationFromDirection(dx, dz) }
    local p = task.plan.staging
    local err = math.sqrt((root.x - p.x) ^ 2 + (root.z - p.z) ^ 2)
    local yawErr = math.deg(math.abs((root.yaw - p.yaw + math.pi) % (2 * math.pi) - math.pi))
    log("stage observed %s error=%.3f m %.2f deg", fmtPose(root), err, yawErr)
    if not (err <= Probe.STAGE_POSITION_TOLERANCE_M and yawErr <= Probe.STAGE_YAW_TOLERANCE_DEG) then
        return self:finish("FAILED", "STAGE_NOT_OBSERVED")
    end
    task.phase, task.elapsed = "settle", 0
end

local function startAlign(self, task)
    -- The wagon and joints must still be exactly what was planned.
    local t, w = observePair(self, task.tractor, task.wagon)
    local fresh = t and w and Probe.plan(t, w, Probe.STAGING_DISTANCE_M, self.Observe.NULL)
    local plan = task.plan
    if fresh == nil or fresh.tractorJoint ~= plan.tractorJoint or fresh.wagonJoint ~= plan.wagonJoint
            or math.sqrt((fresh.target.x - plan.target.x) ^ 2 + (fresh.target.z - plan.target.z) ^ 2)
               > Probe.PLAN_TOLERANCE_M then
        return self:finish("FAILED", "PLAN_CHANGED")
    end
    local token = self.token
    local args = {
        tractor = task.tractor, wagon = task.wagon, tractorJoint = plan.tractorJoint,
        wagonJoint = plan.wagonJoint, farmId = task.farmId,
        isReserved = function() return self.token == token and self.task == task end,
    }
    for k, v in pairs(task.args) do args[k] = v end
    local session, reason = self.Hitch.newAlign(self.env, args)
    if session == nil then return self:finish("FAILED", "ALIGN_" .. tostring(reason)) end
    log("reverse started attachDistanceSq=%.4f maxVerticalM=%.2f maxSpeedKmh=%.1f",
        session.attachDistanceSq, session.maxVerticalM, session.maxSpeedKmh)
    task.phase, task.session = "align", session
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

local function step(self, task, dt)
    local env, Hitch = self.env, self.Hitch
    if task.phase == "stage" then
        task.elapsed = task.elapsed + dt
        local active = task.tractor:getIsAIActive()
        if active == true then task.seenActive = true end
        if task.seenActive and active == false then return stageEnded(self, task) end
        if task.elapsed > Probe.STAGE_TIMEOUT_MS then
            if task.seenActive then Hitch.cancelStaging(env, task.tractor) end
            return self:finish("FAILED", task.seenActive and "STAGE_TIMEOUT" or "GOTO_NOT_STARTED")
        end
    elseif task.phase == "settle" then
        task.elapsed = task.elapsed + dt
        local speed = task.tractor:getLastSpeed()
        if finite(speed) and math.abs(speed) < 1 then return startAlign(self, task) end
        if task.elapsed > Probe.SETTLE_TIMEOUT_MS then return self:finish("FAILED", "NOT_STOPPED") end
    elseif task.phase == "align" then
        local s = task.session
        local code = Hitch.alignStep(env, s, dt)
        if code == "CONTINUE" then return end
        log("reverse result=%s elapsedMs=%d gap=%.3f bestGap=%.3f dy=%s", code, s.elapsed, s.lastGap,
            s.bestGap, tostring(s.lastDy))
        if code ~= "CANDIDATE" then
            return self:finish(code == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", code)
        end
        local attach = Hitch.attach(env, s)
        log("attach: %s", attach)
        if attach ~= "ATTACH_REQUESTED" then
            return self:finish(attach == "UNKNOWN_OBSERVATION" and "UNVERIFIED" or "FAILED", attach)
        end
        task.phase = "attach"
    elseif task.phase == "attach" then
        local state = Hitch.attachState(env, task.session, dt)
        if state == "ATTACH_IN_PROGRESS" then return end
        log("attach state: %s", state)
        if state ~= "ATTACHED" then return self:finish("UNVERIFIED", state) end
        return verify(self, task)
    end
end

function Probe:update(dt)
    local task = self.task
    if task == nil or task.phase == "validate" then return end
    local ok, err = pcall(step, self, task, dt)
    if not ok then
        log("probe error: %s", tostring(err))
        if task.phase == "stage" and task.seenActive then pcall(self.Hitch.cancelStaging, self.env, task.tractor) end
        self:finish("UNVERIFIED", "UNKNOWN_OBSERVATION")
    end
end

-- -------------------------------------------------------- mod lifecycle

function Probe:loadMap()
    self.env = getfenv(0)   -- the game's global table, as AutoDrive register.lua:329
    for _, c in ipairs(Probe.COMMANDS) do addConsoleCommand(c[1], c[2], c[3], self) end
    log("hitch probe loaded; nothing runs until fhHitchList / fhHitchProbe is typed")
end

function Probe:deleteMap()
    if self.task ~= nil then pcall(Probe.consoleStop, self) end
    for _, c in ipairs(Probe.COMMANDS) do removeConsoleCommand(c[1]) end
end

-- FS25 discards a sourceFile's return value (see FarmhandObserve.lua); the
-- modDesc loads FarmhandObserve.lua and FarmhandHitch.lua before this file.
Probe.Observe = FarmhandObserve
Probe.Hitch = FarmhandHitch
FarmhandProbe = Probe
if type(addModEventListener) == "function" then addModEventListener(Probe) end
return Probe
