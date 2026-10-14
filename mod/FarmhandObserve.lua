-- Read-only FS25 observation for AC-001: builds protocol v1 frames
-- (protocol/frame.schema.json). It never moves, attaches or changes anything.
--
-- Every game call is taken from upstream FS25 mod source, recorded with file and
-- line in adapters/EVIDENCE.md. None of it has been executed inside FS25 by this
-- project. Game globals are passed in as `env` so CI can run this file against
-- stubs that implement only those recorded calls; in game, pass _G.
--
-- Written for Lua 5.1 (FS25) and Lua 5.4 (CI): no goto, no integer division.

local Observe = {}

Observe.PROTOCOL = 1
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
-- Unnamed joint types are dropped, which can only remove hitch options.
function Observe.jointNames(joints, attacherJoints)
    local byValue, names, seen = {}, {}, {}
    for key, value in pairs(attacherJoints or {}) do
        if type(key) == "string" and key:sub(1, 10) == "JOINTTYPE_" and token(key:sub(11)) then
            byValue[value] = key:sub(11)
        end
    end
    for _, joint in ipairs(joints or {}) do
        local name = byValue[joint.jointType]
        if name ~= nil and not seen[name] then
            seen[name] = true
            names[#names + 1] = name
        end
    end
    table.sort(names)
    if #names > Observe.MAX_JOINTS then return nil end
    return names
end

-- Returns one vehicle record, or nil when identity, position or attachment
-- state cannot be observed (the vehicle is then counted as skipped).
function Observe.vehicle(env, v)
    local function objectId(object)
        return env.NetworkUtil.getObjectId(object)
    end

    local id = try(function() return objectId(v) end)
    local farmId = try(function() return v.ownerFarmId end)
    local x, _, z = try(function() return env.getWorldTranslation(v.rootNode) end)
    if not integer(id) or not integer(farmId) or farmId > 255 or not finite(x) or not finite(z) then
        return nil
    end

    -- Attachment must be fully observable: an error here must not read as "free".
    local ok, attacher, implements = pcall(function()
        local attacherId = nil
        if type(v.getAttacherVehicle) == "function" then
            local attacherVehicle = v:getAttacherVehicle()
            if attacherVehicle ~= nil then
                attacherId = objectId(attacherVehicle)
                assert(integer(attacherId))
            end
        end
        local ids, seen = {}, {}
        if type(v.getAttachedImplements) == "function" then
            for _, implement in ipairs(v:getAttachedImplements() or {}) do
                local implementId = objectId(implement.object)
                assert(integer(implementId))
                if not seen[implementId] then
                    seen[implementId] = true
                    ids[#ids + 1] = implementId
                end
            end
        end
        assert(#ids <= 32)
        table.sort(ids)
        return attacherId, ids
    end)
    if not ok then return nil end

    local okJoints, attacherJoints, inputJoints = pcall(function()
        local outputs, inputs = {}, {}
        if type(v.getAttacherJoints) == "function" then
            outputs = assert(Observe.jointNames(v:getAttacherJoints(), env.AttacherJoints))
        end
        if type(v.getInputAttacherJoints) == "function" then
            inputs = assert(Observe.jointNames(v:getInputAttacherJoints(), env.AttacherJoints))
        end
        return outputs, inputs
    end)
    if not okJoints then return nil end

    local category = try(function()
        local item = env.g_storeManager:getItemByXMLFilename(v.configFileName)
        return item and item.categoryName
    end)

    local speed = try(function() return v:getLastSpeed() end)
    if not finite(speed) or math.abs(speed) > 1000 then speed = nil end

    -- AutoDrive's single-player occupancy rule (AutoDriveUtilFuncs.lua:776-782).
    local entered = try(function()
        if type(v.getIsEntered) ~= "function" then return nil end
        local isEntered = v:getIsEntered() == true
        if type(v.getIsControlled) == "function" then
            isEntered = isEntered or v:getIsControlled() == true
        end
        return isEntered
    end)

    local aiActive = try(function()
        if type(v.getIsAIActive) ~= "function" then return nil end
        return v:getIsAIActive() == true
    end)

    local hasAutoDrive = try(function() return v.ad ~= nil and v.ad.stateModule ~= nil end) == true
    local adActive = nil
    if hasAutoDrive then
        adActive = try(function() return v.ad.stateModule:isActive() == true end)
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
        attacher_vehicle_id = nullable(attacher),
        implement_ids = Observe.array(implements),
        attacher_joints = Observe.array(attacherJoints),
        input_joints = Observe.array(inputJoints),
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

-- Returns (line, nil), or (nil, reason) when the farm, the vehicle list or the
-- AutoDrive destination list cannot be observed (nothing is sent in that case).
function Observe.snapshot(env, seq)
    local farmId = try(function() return env.g_currentMission:getFarmId() end)
    if not integer(farmId) or farmId < 1 or farmId > 255 then return nil, "NO_FARM" end
    local vehicles = try(function() return env.g_currentMission.vehicleSystem.vehicles end)
        or try(function() return env.g_currentMission.vehicles end)
    if type(vehicles) ~= "table" then return nil, "NO_VEHICLES" end

    local records, skipped, truncated = Observe.array(), 0, false
    for _, v in pairs(vehicles) do
        local owned = try(function() return v.ownerFarmId == farmId end)
        if owned then
            local record = Observe.vehicle(env, v)
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
                    destinations[#destinations + 1] = { id = marker.id, x = marker.x, z = marker.z }
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
        farm_id = farmId,
        player = playerPosition(env),
        truncated = truncated,
        skipped = skipped,
        vehicles = records,
        destinations = destinations,
    }), nil
end

return Observe
