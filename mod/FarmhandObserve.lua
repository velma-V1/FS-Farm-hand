-- Read-only FS25 observation for AC-001: builds protocol v2 frames
-- (protocol/frame.schema.json). It never moves, attaches or changes anything.
--
-- Every game call is taken from upstream FS25 mod source, recorded with file and
-- line in adapters/EVIDENCE.md. None of it has been executed inside FS25 by this
-- project. Game globals are passed in as `env` so CI can run this file against
-- stubs that implement only those recorded calls; in game, pass _G.
--
-- Written for Lua 5.1 (FS25) and Lua 5.4 (CI): no goto, no integer division.

local Observe = {}

Observe.PROTOCOL = 2
-- Must equal protocol/ipc.py PIPE_PATH. The companion hosts this pipe; the mod
-- is a write-only client (pattern from Farmhand Companion scripts/Transport.lua).
Observe.PIPE_PATH = "\\\\.\\pipe\\fs_farmhand_ac001"
Observe.MAX_VEHICLES = 200
Observe.MAX_DESTINATIONS = 2000
Observe.MAX_JOINTS = 32
Observe.NULL = setmetatable({}, { __tostring = function() return "null" end })

local ARRAY = {}

function Observe.array(items)
    return setmetatable(items or {}, ARRAY)
end

-- pcall wrapper: a failed or missing game call yields nil, never a default.
local function try(fn)
    local ok, a, b, c = pcall(fn)
    if ok then return a, b, c end
    return nil
end

local function finite(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

local function integer(n)
    return finite(n) and math.floor(n) == n and n >= 0 and n <= 2147483647
end

local function token(s)
    if type(s) ~= "string" or #s < 1 or #s > 64 or s:find("[^%w_%.%-]") then return nil end
    return s
end

-- Strict UTF-8 (no overlongs or surrogates), so one bad name cannot make the
-- whole frame undecodable by the companion.
local function validUtf8(s)
    local i, n = 1, #s
    while i <= n do
        local c = s:byte(i)
        local len, low, high = 1, 0x80, 0xBF
        if c >= 0xC2 and c <= 0xDF then len = 2
        elseif c >= 0xE0 and c <= 0xEF then
            len = 3
            if c == 0xE0 then low = 0xA0 elseif c == 0xED then high = 0x9F end
        elseif c >= 0xF0 and c <= 0xF4 then
            len = 4
            if c == 0xF0 then low = 0x90 elseif c == 0xF4 then high = 0x8F end
        elseif c >= 0x80 then return false end
        if i + len - 1 > n then return false end
        for j = i + 1, i + len - 1 do
            local b = s:byte(j)
            if j == i + 1 and (b < low or b > high) then return false end
            if b < 0x80 or b > 0xBF then return false end
        end
        i = i + len
    end
    return true
end

-- Free text (map IDs, marker names): non-empty, bounded, no control characters.
local function text(s)
    if type(s) ~= "string" or #s < 1 or #s > 128 or s:find("%c") or not validUtf8(s) then
        return nil
    end
    return s
end

local function nullable(v)
    if v == nil then return Observe.NULL end
    return v
end

-- ------------------------------------------------------------------ JSON
local encode

local function encodeString(s)
    return '"' .. s:gsub('[%c"\\]', function(c)
        return string.format("\\u%04x", c:byte())
    end) .. '"'
end

encode = function(value)
    local kind = type(value)
    if value == nil or value == Observe.NULL then
        return "null"
    elseif kind == "boolean" then
        return tostring(value)
    elseif kind == "number" then
        if not finite(value) then return "null" end
        if math.floor(value) == value and math.abs(value) < 2 ^ 53 then
            return string.format("%.0f", value)
        end
        return string.format("%.4f", value)
    elseif kind == "string" then
        return encodeString(value)
    elseif kind == "table" then
        local parts = {}
        if getmetatable(value) == ARRAY then
            for i = 1, #value do parts[i] = encode(value[i]) end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        local keys = {}
        for key in pairs(value) do keys[#keys + 1] = key end
        table.sort(keys)
        for i, key in ipairs(keys) do
            parts[i] = encodeString(key) .. ":" .. encode(value[key])
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    error("unencodable " .. kind)
end

Observe.encode = encode

-- ------------------------------------------------------------- vehicles

-- Reverse-map numeric joint types through AttacherJoints.JOINTTYPE_* keys
-- (AutoDrive scripts/Utils/AutoDriveUtilFuncs.lua:952-955 uses the same keys).
-- A joint whose type has no name is not reported, which can only remove options.
local function jointTypeNames(attacherJoints)
    local byValue = {}
    for key, value in pairs(attacherJoints or {}) do
        if type(key) == "string" and key:sub(1, 10) == "JOINTTYPE_" and token(key:sub(11)) then
            byValue[value] = key:sub(11)
        end
    end
    return byValue
end

-- Observation states: a method that exists and returns a real boolean is
-- OBSERVED; one that exists but errors or returns anything else is UNKNOWN (nil,
-- encoded as JSON null). A MISSING method is NOT APPLICABLE only where upstream
-- code treats it that way (adapters/EVIDENCE.md); unknown is never coerced.
local function observedBool(v, method)
    local ok, value = pcall(function() return v[method](v) end)
    if ok and type(value) == "boolean" then return value end
    return nil
end

-- Returns attacherId|nil, implementIds, observed. Missing methods mean "no such
-- attachment side" (AD Specialization.lua:1795, FH Commands.lua:340) only when
-- the vehicle also has no joints for that side; otherwise attachment is unknown.
function Observe.attachment(env, v)
    local function objectId(object)
        local id = env.NetworkUtil.getObjectId(object)
        assert(integer(id))
        return id
    end
    local ok, attacher, implements = pcall(function()
        local attacherId = nil
        if type(v.getAttacherVehicle) == "function" then
            local attacherVehicle = v:getAttacherVehicle()
            if attacherVehicle ~= nil then attacherId = objectId(attacherVehicle) end
        else
            assert(type(v.getInputAttacherJoints) ~= "function")
        end
        local ids, seen = {}, {}
        if type(v.getAttachedImplements) == "function" then
            local list = v:getAttachedImplements()
            assert(type(list) == "table")
            for _, implement in ipairs(list) do
                local implementId = objectId(implement.object)
                if not seen[implementId] then
                    seen[implementId] = true
                    ids[#ids + 1] = implementId
                end
            end
        else
            assert(type(v.getAttacherJoints) ~= "function")
        end
        assert(#ids <= 32)
        table.sort(ids)
        return attacherId, ids
    end)
    if not ok then return nil, {}, false end
    return attacher, implements, true
end

-- Hitch geometry, read only through GIANTS' own transforms (adapters/EVIDENCE.md).
-- Anything unreadable is reported as absent (nil / empty), which makes the vehicle
-- unhitchable; it is never filled in with a guess.
local function yawOf(env, node)
    local dx, _, dz = env.localDirectionToWorld(node, 0, 0, 1)
    assert(finite(dx) and finite(dz) and (dx ~= 0 or dz ~= 0))
    local yaw = env.MathUtil.getYRotationFromDirection(dx, dz)
    assert(finite(yaw))
    return yaw
end

-- PTO names, matched exactly as GIANTS' PowerTakeOffs:attachPowerTakeOff does
-- (lines 747-766): outputs at the tractor joint, inputs at the implement joint by
-- ptoName. A missing method means no PTO on that side (GIANTS line 752; Manual
-- Attach PowerTakeOffExtension.lua:20-26); an unreadable one is unknown (nil).
local function ptoName(output)
    local name = output.ptoName
    assert(text(name) ~= nil and #name <= 64)
    return name
end

function Observe.outputPtoNames(v, jointIndex)
    if type(v.getOutputPowerTakeOffsByJointDescIndex) ~= "function" then return Observe.array() end
    local outputs = v:getOutputPowerTakeOffsByJointDescIndex(jointIndex)
    assert(type(outputs) == "table")
    local names, seen = Observe.array(), {}
    for _, output in ipairs(outputs) do
        local name = ptoName(output)
        if not seen[name] then seen[name] = true; names[#names + 1] = name end
    end
    assert(#names <= 16)
    table.sort(names)
    return names
end

local function inputPtoNames(v, jointIndex, candidates)
    if type(v.getInputPowerTakeOffsByJointDescIndexAndName) ~= "function" then return Observe.array() end
    local names = Observe.array()
    for _, name in ipairs(candidates) do
        local inputs = v:getInputPowerTakeOffsByJointDescIndexAndName(jointIndex, name)
        assert(type(inputs) == "table")
        if #inputs > 0 then names[#names + 1] = name end
    end
    assert(#names <= 16)
    return names
end

-- `ptoNames`: every output PTO name on the farm's vehicles (sorted), the only
-- names an input could be matched against.
function Observe.hitchGeometry(env, v, ptoNames)
    local byValue = jointTypeNames(env.AttacherJoints)
    local heading = try(function() return yawOf(env, v.rootNode) end)

    local aiRoot = try(function()
        local node = v:getAIRootNode()
        local x, _, z = env.getWorldTranslation(node)
        assert(finite(x) and finite(z))
        return { x = x, z = z, yaw = yawOf(env, node) }
    end)

    -- Tractor joints: GIANTS' own offsets of each jointTransform in the AI root
    -- frame, built 1:1 in getAttacherJoints() order (AIDrivable:onPostLoad 186-196).
    -- The list position must be GIANTS' joint desc index, or nothing is reported.
    local outputs = try(function()
        local joints, offsets = v:getAttacherJoints(), v.spec_aiDrivable.attacherJointOffsets
        assert(type(joints) == "table" and type(offsets) == "table" and #joints == #offsets)
        assert(#joints <= Observe.MAX_JOINTS)
        local result = Observe.array()
        for i, joint in ipairs(joints) do
            if type(v.getAttacherJointByJointDescIndex) == "function" then
                assert(v:getAttacherJointByJointDescIndex(i) == joint)
            end
            local name, offset = byValue[joint.jointType], offsets[i]
            if name ~= nil and finite(offset.x) and finite(offset.z)
                    and math.abs(offset.x) <= 50 and math.abs(offset.z) <= 50 then
                -- jointIndex == 0 is GIANTS' "joint free" test (AttacherJoints 1910, 3063).
                result[#result + 1] = { index = i, type = name, x = offset.x, z = offset.z,
                                        free = joint.jointIndex == 0,
                                        pto_names = nullable(try(function() return Observe.outputPtoNames(v, i) end)) }
            end
        end
        return result
    end) or Observe.array()

    -- Implement input joints at their world positions. An activity check that is
    -- missing or unreadable reports inactive (unusable), never active.
    local inputs = try(function()
        local joints = v:getInputAttacherJoints()
        assert(type(joints) == "table" and #joints <= Observe.MAX_JOINTS)
        local result = Observe.array()
        for i, joint in ipairs(joints) do
            local name = byValue[joint.jointType]
            local x, _, z = env.getWorldTranslation(joint.node)
            if name ~= nil and finite(x) and finite(z) then
                local okActive, active = pcall(function() return v:getIsInputAttacherActive(joint) end)
                result[#result + 1] = { index = i, type = name, x = x, z = z,
                                        active = okActive and active == true,
                                        pto_names = nullable(try(function()
                                            return inputPtoNames(v, i, ptoNames or {})
                                        end)) }
            end
        end
        return result
    end) or Observe.array()

    return heading, aiRoot, outputs, inputs
end

-- Returns one vehicle record, or nil when identity or position cannot be
-- observed (the vehicle is then counted as skipped).
function Observe.vehicle(env, v, ptoNames)
    local id = try(function() return env.NetworkUtil.getObjectId(v) end)
    local farmId = try(function() return v.ownerFarmId end)
    local x, _, z = try(function() return env.getWorldTranslation(v.rootNode) end)
    if not integer(id) or not integer(farmId) or farmId > 255 or not finite(x) or not finite(z) then
        return nil
    end

    local attacher, implements, attachmentObserved = Observe.attachment(env, v)

    local category = try(function()
        local item = env.g_storeManager:getItemByXMLFilename(v.configFileName)
        return item and item.categoryName
    end)

    local speed = try(function() return v:getLastSpeed() end)
    if not finite(speed) or math.abs(speed) > 1000 then speed = nil end

    -- AutoDrive's single-player occupancy rule (AutoDriveUtilFuncs.lua:770-786):
    -- entered or controlled; a missing getIsEntered means not entered, and a
    -- missing getIsControlled means getIsEntered alone decides.
    local entered = false
    if type(v.getIsEntered) == "function" then
        entered = observedBool(v, "getIsEntered")
        if type(v.getIsControlled) == "function" then
            local controlled = observedBool(v, "getIsControlled")
            if entered == true or controlled == true then
                entered = true
            elseif entered == nil or controlled == nil then
                entered = nil
            end
        end
    end

    -- A missing getIsAIActive means not AI-active (AD HarvestManager.lua:104,
    -- FH Collectors.lua:457).
    local aiActive = false
    if type(v.getIsAIActive) == "function" then
        aiActive = observedBool(v, "getIsAIActive")
    end

    -- vehicle.ad is AutoDrive's own presence test (ExternalInterface.lua:116).
    -- Once present, an unreadable state module is unknown, not "no AutoDrive".
    local hasAutoDrive = try(function() return v.ad ~= nil end) == true
    local adActive = nil
    if hasAutoDrive then
        adActive = try(function()
            local value = v.ad.stateModule:isActive()
            if type(value) == "boolean" then return value end
            return nil
        end)
    end

    local heading, aiRoot, outputJoints, inputJointPoses = Observe.hitchGeometry(env, v, ptoNames)

    -- The vehicle parts of GIANTS' availability rule for its GOTO job
    -- (AIJobGoTo:getIsAvailableForVehicle 92-102): a navigation agent and support
    -- for the job type. Every movement step uses this job, so it gates capability.
    -- getCanStartAIVehicle is left out on purpose: it is also false at the global
    -- helper limit and while any AI runs, which says nothing about the vehicle; a
    -- start refused for such a reason ends the step as GOTO_NOT_STARTED.
    local gotoSupported = try(function()
        if v.createAgent == nil or v.setAITarget == nil then return false end
        local supported = v:getIsAIJobSupported("AIJobGoTo")
        if type(supported) ~= "boolean" then return nil end
        return supported
    end)

    -- Self-propelled: GIANTS' Motorized specialization table (Motorized:onLoad 367;
    -- AutoDrive's motor test, AutoDriveUtilFuncs.lua:162) on a vehicle that is not
    -- itself towed (no Attachable table; AD AutoDriveUtilFuncs.lua:951).
    local selfPropelled = try(function() return v.spec_motorized ~= nil and v.spec_attachable == nil end)

    -- GIANTS' ForageWagon specialization (ForageWagon:onLoad line 93, as Courseplay
    -- reads it) on a towed implement identifies a forage wagon.
    local forageWagon = try(function() return v.spec_forageWagon ~= nil and v.spec_attachable ~= nil end) == true

    -- Power in GIANTS' own terms (PowerConsumer:getCanBeTurnedOn 271-294), read
    -- from the loaded vehicle, so the actual motor/consumer configuration applies.
    local motor = try(function()
        local m = v:getMotor()
        local peak, ratio = m:getPeakTorque(), m:getPtoMotorRpmRatio()
        assert(finite(peak) and peak > 0 and finite(ratio) and ratio > 0)
        return { peak_torque = peak, pto_rpm_ratio = ratio }
    end)
    -- Without PowerConsumer GIANTS sets no power condition: no PTO torque needed.
    local ptoTorque = 0
    if type(v.getConsumedPtoTorque) == "function" then
        ptoTorque = try(function()
            local torque = v:getConsumedPtoTorque(true, true)
            assert(finite(torque) and torque >= 0)
            return torque
        end)
    end

    return {
        id = id,
        farm_id = farmId,
        category = nullable(token(category)),
        x = x,
        z = z,
        speed_kmh = nullable(speed),
        entered = nullable(entered),
        ai_active = nullable(aiActive),
        has_autodrive = hasAutoDrive,
        ad_active = nullable(adActive),
        attachment_observed = attachmentObserved,
        attacher_vehicle_id = nullable(attacher),
        implement_ids = Observe.array(implements),
        heading = nullable(heading),
        forage_wagon = forageWagon,
        goto_supported = nullable(gotoSupported),
        self_propelled = nullable(selfPropelled),
        motor = nullable(motor),
        pto_torque = nullable(ptoTorque),
        ai_root = nullable(aiRoot),
        output_joints = outputJoints,
        input_joint_poses = inputJointPoses,
    }
end

-- ------------------------------------------------------------- frames

function Observe.hello(env)
    local modVersion = try(function() return env.g_modManager:getModByName(env.g_currentModName).version end)
    local autoDriveVersion = try(function() return env.AutoDrive.version end)
    return encode({
        event = "hello",
        protocol = Observe.PROTOCOL,
        mod_version = token(modVersion) or "unknown",
        autodrive_version = nullable(token(autoDriveVersion)),
    })
end

local function playerPosition(env)
    local x, _, z = try(function()
        local player = env.g_localPlayer
        if type(player.getPosition) == "function" then return player:getPosition() end
        return env.getWorldTranslation(player.rootNode)
    end)
    if finite(x) and finite(z) then return { x = x, z = z } end
    return Observe.NULL
end

-- The output PTO names of the farm's vehicles (sorted): the only names an
-- implement input could be matched against. Unreadable ones are simply not
-- candidates; their joints report unknown PTO names.
function Observe.farmPtoNames(vehicles, farmId)
    local seen, names = {}, {}
    for _, v in pairs(vehicles) do
        if try(function() return v.ownerFarmId == farmId end) then
            local joints = try(function() return v:getAttacherJoints() end)
            for i = 1, type(joints) == "table" and #joints or 0 do
                for _, name in ipairs(try(function() return Observe.outputPtoNames(v, i) end) or {}) do
                    if not seen[name] then seen[name] = true; names[#names + 1] = name end
                end
            end
        end
    end
    table.sort(names)
    return names
end

-- Returns (line, nil), or (nil, reason) when the farm, the vehicle list or the
-- AutoDrive destination list cannot be observed (nothing is sent in that case).
function Observe.snapshot(env, seq, session)
    local farmId = try(function() return env.g_currentMission:getFarmId() end)
    if not integer(farmId) or farmId < 1 or farmId > 255 then return nil, "NO_FARM" end
    local vehicles = try(function() return env.g_currentMission.vehicleSystem.vehicles end)
        or try(function() return env.g_currentMission.vehicles end)
    if type(vehicles) ~= "table" then return nil, "NO_VEHICLES" end

    local ptoNames = Observe.farmPtoNames(vehicles, farmId)
    local records, skipped, truncated = Observe.array(), 0, false
    for _, v in pairs(vehicles) do
        local owned = try(function() return v.ownerFarmId == farmId end)
        if owned then
            local record = Observe.vehicle(env, v, ptoNames)
            if record == nil then
                skipped = skipped + 1
            elseif #records >= Observe.MAX_VEHICLES then
                truncated = true
            else
                records[#records + 1] = record
            end
        end
    end

    local destinations = Observe.array()
    if env.AutoDrive ~= nil and type(env.AutoDrive.GetAvailableDestinations) == "function" then
        local markers = try(function() return env.AutoDrive:GetAvailableDestinations() end)
        if type(markers) ~= "table" then return nil, "NO_DESTINATIONS" end
        for _, marker in pairs(markers) do
            if integer(marker.id) and finite(marker.x) and finite(marker.z) then
                if #destinations >= Observe.MAX_DESTINATIONS then
                    truncated = true
                else
                    -- An unusable name leaves the marker selectable by ID only.
                    destinations[#destinations + 1] = { id = marker.id, name = text(marker.name) or "",
                                                        x = marker.x, z = marker.z }
                end
            else
                skipped = skipped + 1
            end
        end
    end

    return encode({
        event = "snapshot",
        protocol = Observe.PROTOCOL,
        seq = seq,
        -- The companion session this game currently accepts commands from.
        session = nullable(token(session)),
        farm_id = farmId,
        -- CP keys per-map field data on missionInfo.mapId (CustomFieldManager.lua:284).
        map_id = nullable(text(try(function() return env.g_currentMission.missionInfo.mapId end))),
        player = playerPosition(env),
        truncated = truncated,
        skipped = skipped,
        vehicles = records,
        destinations = destinations,
    }), nil
end

-- FS25 discards a sourceFile's return value, so the mod reaches this table
-- through a mod-environment global; CI loads the returned table instead.
FarmhandObserve = Observe
return Observe
