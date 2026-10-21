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
    { "fhHitchProbe", "Farmhand dev: fhHitchProbe <tractorId> <wagonId> [assistRadiusM] [maxVerticalM] [maxSpeedKmh]",
      "consoleProbe" },
    { "fhHitchStop", "Farmhand dev: abort the running hitch probe", "consoleStop" },
}
-- Mirrors of companion constants (checked by tests/test_lua_probe.py).
Probe.STAGING_DISTANCE_M = 6.0           -- companion/hitch.py STAGING_DISTANCE_M
Probe.REAR_JOINT_MAX_Z = 0.5             -- companion/hitch.py REAR_JOINT_MAX_Z
Probe.FRONT_JOINT_SLACK_M = 0.5          -- companion/hitch.py FRONT_JOINT_SLACK_M
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
    local hx, hz = math.sin(heading), math.cos(heading)
    for _, out in ipairs(tractor.output_joints) do
        for _, inp in ipairs(wagon.input_joint_poses) do
            -- Never an input behind the wagon's root (companion/hitch.py plan_hitch).
            local front = (inp.x - wagon.x) * hx + (inp.z - wagon.z) * hz >= -Probe.FRONT_JOINT_SLACK_M
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
    local task = self.task
    if task == nil or task.op == nil then return end
    local ok, err = pcall(step, self, task, dt)
    if not ok then
        log("probe error: %s", tostring(err))
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
-- modDesc loads FarmhandObserve, FarmhandHitch and FarmhandBridge before this.
Probe.Observe = FarmhandObserve
Probe.Hitch = FarmhandHitch
Probe.Bridge = FarmhandBridge
FarmhandProbe = Probe
if type(addModEventListener) == "function" then addModEventListener(Probe) end
return Probe
