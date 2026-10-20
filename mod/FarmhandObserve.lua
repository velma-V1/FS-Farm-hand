-- Read-only FS25 observation for AC-001: builds protocol v3 frames
-- (protocol/frame.schema.json). It never moves, attaches or changes anything.
--
-- Every game call is taken from upstream FS25 mod source, recorded with file and
-- line in adapters/EVIDENCE.md. None of it has been executed inside FS25 by this
-- project. Game globals are passed in as `env` so CI can run this file against
-- stubs that implement only those recorded calls; in game, pass _G.
--
-- Written for Lua 5.1 (FS25) and Lua 5.4 (CI): no goto, no integer division.

local Observe = {}

Observe.PROTOCOL = 3
-- The build this mod was installed from: "source" in the checkout; replaced with the
-- commit by scripts/package_probe.py in built copies (= companion/buildinfo.py BUILD).
Observe.BUILD = "source"
-- Must equal protocol/ipc.py PIPE_PATH. The companion hosts this pipe; the mod
-- is a write-only client (pattern from Farmhand Companion scripts/Transport.lua).
Observe.PIPE_PATH = "\\\\.\\pipe\\fs_farmhand_ac001"
Observe.MAX_VEHICLES = 200
Observe.MAX_DESTINATIONS = 2000
Observe.MAX_PLACES = 500
Observe.MAX_JOINTS = 32
Observe.JOINT_DRIFT_M = 1.0     -- a joint node this far from its load-time AI offset is not used
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
Observe.validUtf8 = validUtf8

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

-- Every input PTO name GIANTS holds at an implement joint: PowerTakeOffs:
-- getInputPowerTakeOffs (735-737) returns spec.inputPowerTakeOffs, each loaded with
-- its ptoName (570, default DEFAULT_PTO) and inputAttacherJointIndices (563), the
-- same fields getInputPowerTakeOffsByJointDescIndexAndName matches (709-711). Empty
-- means GIANTS connects nothing at that joint.
local function inputPtoNames(v, jointIndex)
    if type(v.getInputPowerTakeOffs) ~= "function" then return Observe.array() end
    local inputs = v:getInputPowerTakeOffs()
    assert(type(inputs) == "table")
    local names, seen = Observe.array(), {}
    for _, input in pairs(inputs) do
        assert(type(input.inputAttacherJointIndices) == "table")
        if input.inputAttacherJointIndices[jointIndex] ~= nil then
            local name = ptoName(input)
            if not seen[name] then seen[name] = true; names[#names + 1] = name end
        end
    end
    assert(#names <= 16)
    table.sort(names)
    return names
end

function Observe.hitchGeometry(env, v)
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
        -- Offsets exist for the joints present at onPostLoad, in the same order; joints a
        -- vehicle adds later (live: the GMC Sierra's bed attachments) have none and are
        -- left out, the others keep their index.
        assert(type(joints) == "table" and type(offsets) == "table" and #offsets <= #joints)
        assert(#joints <= Observe.MAX_JOINTS)
        local result = Observe.array()
        for i, joint in ipairs(joints) do
            if type(v.getAttacherJointByJointDescIndex) == "function" then
                assert(v:getAttacherJointByJointDescIndex(i) == joint)
            end
            local name, offset = byValue[joint.jointType], offsets[i]
            -- The joint's world point must be where its load-time offset puts it from the
            -- AI root's world point and heading (what the companion's plan and the
            -- maneuver both use). Live FourFields 7R (4e9ca0b): joints 2-4 read 24 m away
            -- in world while worldToLocal still gave their offsets: compared in world.
            local drifted = false
            if offset ~= nil and joint.jointTransform ~= nil then
                local ok, gap = pcall(function()
                    local root = v:getAIRootNode()
                    local rx, _, rz = env.getWorldTranslation(root)
                    local fx, _, fz = env.localDirectionToWorld(root, 0, 0, 1)
                    local px, pz = rx + offset.x * fz + offset.z * fx, rz - offset.x * fx + offset.z * fz
                    local wx, _, wz = env.getWorldTranslation(joint.jointTransform)
                    return math.sqrt((wx - px) ^ 2 + (wz - pz) ^ 2)
                end)
                drifted = not (ok and finite(gap) and gap <= Observe.JOINT_DRIFT_M)
            end
            if name ~= nil and offset ~= nil and not drifted and finite(offset.x) and finite(offset.z)
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
                                            return inputPtoNames(v, i)
                                        end)) }
            end
        end
        return result
    end) or Observe.array()

    return heading, aiRoot, outputs, inputs
end

-- What a vehicle is, by GIANTS' own specialization tables: each class's code
-- reads its state as self.spec_<name> (GDN Specializations, read 2026-10-18:
-- Plow 776, Cultivator 652, SowingMachine 797, Sprayer 800 (also on fertilizing
-- seeders and cultivators, 670/669), Mower 700, Tedder 806, Windrower 828, Baler
-- 636, BaleWrapper 637, BaleLoader 635, ForageWagon 676, Trailer 812, Combine 644,
-- Cutter 653, Roller 787, Weeder 824, Mulcher 701, StonePicker 801, ManureBarrel
-- 694, Pallet 703, BigBag 639, TreePlanter 813, FruitPreparer 678, Leveler 689,
-- MixerWagon 696, SaltSpreader 790, Shovel 793, Pickup 704, WaterTrailer 822,
-- LivestockTrailer 692, HookLiftTrailer 684, Dischargeable 657, FillUnit 672,
-- AIImplement 621, AIFieldWorker 620). A table present = that capability; the
-- list is fixed, in this order.
Observe.KINDS = {
    { "plow", "spec_plow" }, { "cultivator", "spec_cultivator" }, { "sowing_machine", "spec_sowingMachine" },
    { "sprayer", "spec_sprayer" }, { "mower", "spec_mower" }, { "tedder", "spec_tedder" },
    { "windrower", "spec_windrower" }, { "baler", "spec_baler" }, { "bale_wrapper", "spec_baleWrapper" },
    { "bale_loader", "spec_baleLoader" }, { "forage_wagon", "spec_forageWagon" }, { "trailer", "spec_trailer" },
    { "combine", "spec_combine" }, { "cutter", "spec_cutter" }, { "roller", "spec_roller" },
    { "weeder", "spec_weeder" }, { "mulcher", "spec_mulcher" }, { "stone_picker", "spec_stonePicker" },
    { "manure_barrel", "spec_manureBarrel" }, { "pallet", "spec_pallet" }, { "big_bag", "spec_bigBag" },
    { "tree_planter", "spec_treePlanter" }, { "fruit_preparer", "spec_fruitPreparer" },
    { "leveler", "spec_leveler" }, { "mixer_wagon", "spec_mixerWagon" }, { "salt_spreader", "spec_saltSpreader" },
    { "shovel", "spec_shovel" }, { "pickup", "spec_pickup" }, { "water_trailer", "spec_waterTrailer" },
    { "livestock_trailer", "spec_livestockTrailer" }, { "hook_lift_trailer", "spec_hookLiftTrailer" },
    { "dischargeable", "spec_dischargeable" }, { "fill_unit", "spec_fillUnit" },
    { "ai_implement", "spec_aiImplement" }, { "ai_field_worker", "spec_aiFieldWorker" },
}

function Observe.kinds(v)
    local kinds = {}
    for _, kind in ipairs(Observe.KINDS) do
        if try(function() return v[kind[2]] ~= nil end) == true then kinds[#kinds + 1] = kind[1] end
    end
    return Observe.array(kinds)
end

-- Fill units (GDN FillUnit 672): getFillUnits L822; per unit getFillUnitFillLevel
-- L884, getFillUnitCapacity L846, getFillUnitFillType L910 and
-- getFillUnitSupportedFillTypes L997 (a set, [fillType] = true); names by
-- g_fillTypeManager:getFillTypeNameByIndex (FillTypeManager 409 L476). The seed
-- unit is getSowingMachineFillUnitIndex (SowingMachine 797 L456), the spray unit
-- getSprayerFillUnitIndex (Sprayer 800 L756: fertilizer, lime, herbicide). At most
-- 8 units and 32 supported types each; an unreadable value is nil.
Observe.MAX_FILL_UNITS, Observe.MAX_FILL_TYPES = 8, 32

function Observe.fillUnits(env, v)
    local result = {}
    local ok, units = pcall(function() return v:getFillUnits() end)
    if not ok or type(units) ~= "table" then return Observe.array(result) end
    local seed = try(function() return v:getSowingMachineFillUnitIndex() end)
    local spray = try(function() return v:getSprayerFillUnitIndex() end)
    local function amount(f)
        return try(function()
            local x = f()
            assert(finite(x) and x >= 0 and x < 1e9)
            return x
        end)
    end
    local function name(index)
        return token(env.g_fillTypeManager:getFillTypeNameByIndex(index))
    end
    for i = 1, math.min(#units, Observe.MAX_FILL_UNITS) do
        local supported = {}
        pcall(function()
            for fillType, on in pairs(v:getFillUnitSupportedFillTypes(i)) do
                local n = on and name(fillType)
                if n and #supported < Observe.MAX_FILL_TYPES then supported[#supported + 1] = n end
            end
        end)
        table.sort(supported)
        result[#result + 1] = {
            index = i,
            fill_type = nullable(try(function() return name(v:getFillUnitFillType(i)) end)),
            level = nullable(amount(function() return v:getFillUnitFillLevel(i) end)),
            capacity = nullable(amount(function() return v:getFillUnitCapacity(i) end)),
            supported = Observe.array(supported),
            role = nullable((i == seed and "seed") or (i == spray and "spray") or nil),
        }
    end
    return Observe.array(result)
end

-- Returns one vehicle record, or nil when identity or position cannot be
-- observed (the vehicle is then counted as skipped).
function Observe.vehicle(env, v)
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

    local storeName = try(function()
        local item = env.g_storeManager:getItemByXMLFilename(v.configFileName)
        return item and item.name
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
    -- Except AutoDrive's scratch table on implements it handled
    -- (SpecialDrivingModule.lua:391, AutoDriveUtilFuncs.lua:35, 503): only its
    -- specialization creates the state module (Specialization.lua:194), only on
    -- drivable vehicles (prerequisitesPresent), and AutoDrive itself reports a
    -- vehicle without one as not AutoDrive-active (Specialization.lua:1799). So a
    -- towed vehicle (Attachable, not Motorized) without a state module has no
    -- AutoDrive driver; on any other vehicle a missing state module stays unknown.
    local hasAutoDrive = try(function() return v.ad ~= nil end) == true
    if hasAutoDrive and try(function()
        return v.ad.stateModule == nil and v.spec_attachable ~= nil and v.spec_motorized == nil
    end) == true then
        hasAutoDrive = false
    end
    local adActive = nil
    if hasAutoDrive then
        adActive = try(function()
            local value = v.ad.stateModule:isActive()
            if type(value) == "boolean" then return value end
            return nil
        end)
    end

    local heading, aiRoot, outputJoints, inputJointPoses = Observe.hitchGeometry(env, v)

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
    -- Self-propelled: Motorized and Drivable (a seat and controls). Live 2026-10-19 the
    -- GMC Sierra mod also has Attachable (it can itself be towed), so "not Attachable"
    -- alone left it out; an implement is never Drivable.
    local selfPropelled = try(function()
        return v.spec_motorized ~= nil and (v.spec_drivable ~= nil or v.spec_attachable == nil)
    end)

    -- GIANTS' ForageWagon specialization (ForageWagon:onLoad line 93, as Courseplay
    -- reads it) on a towed implement identifies a forage wagon.
    local forageWagon = try(function() return v.spec_forageWagon ~= nil and v.spec_attachable ~= nil end) == true

    -- Power in GIANTS' own terms (PowerConsumer:getCanBeTurnedOn 271-294), read
    -- from the loaded vehicle, so the actual motor/consumer configuration applies.
    local motor = try(function()
        local m = v:getMotor()
        local peak = m:getPeakTorque()
        assert(finite(peak) and peak > 0)
        -- A vehicle without a PTO (live: the GMC Sierra) has no usable ratio: the motor
        -- is still known (it tows), its PTO capacity is not.
        local okRatio, ratio = pcall(function() return m:getPtoMotorRpmRatio() end)
        if not (okRatio and finite(ratio) and ratio > 0) then ratio = nil end
        return { peak_torque = peak, pto_rpm_ratio = nullable(ratio) }
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
        kinds = Observe.kinds(v),
        fill_units = Observe.fillUnits(env, v),
        name = nullable(text(storeName)),
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

-- AutoDrive's class table. FS25 runs each mod in its own environment and AutoDrive
-- never writes `AutoDrive` to the shared globals (live: no version seen with it
-- loaded), so it is found where GIANTS keeps it: in a vehicle's specializations, the
-- class tables aligned with specializationNames (AutoDrive registers its spec under
-- the name "AutoDrive", register.lua 159-175). Its ExternalInterface functions
-- (StartDriving, GetAvailableDestinations, ExternalInterface.lua 114+) live on it.
Observe.autoDriveClass = nil

function Observe.autoDrive(env)
    if env.AutoDrive ~= nil then return env.AutoDrive end
    if Observe.autoDriveClass ~= nil then return Observe.autoDriveClass end
    local vehicles = try(function() return env.g_currentMission.vehicleSystem.vehicles end) or {}
    for _, v in pairs(vehicles) do
        local names, specs = v.specializationNames, v.specializations
        if type(names) == "table" and type(specs) == "table" then
            for i, name in ipairs(names) do
                local class = specs[i]
                if type(name) == "string" and name:match("AutoDrive$") and type(class) == "table"
                        and type(class.StartDriving) == "function" then
                    Observe.autoDriveClass = class
                    return class
                end
            end
        end
    end
    return nil
end

function Observe.hello(env)
    local modVersion = try(function() return env.g_modManager:getModByName(env.g_currentModName).version end)
    local autoDriveVersion = try(function() return Observe.autoDrive(env).version end)
    return encode({
        event = "hello",
        protocol = Observe.PROTOCOL,
        mod_version = token(modVersion) or "unknown",
        build = token(Observe.BUILD) or "unknown",
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
-- ------------------------------------------------------------------ fields
-- The farm's own fields and how much of each is in which state: GIANTS' field
-- mission pattern (PlowMission:createModifier 40-47, AbstractFieldMission
-- initializeModifier 370-421, getPartitionCompletion 439-448): a DensityMapModifier
-- on the ground-type (or spray-level) map, the field's density-map polygon applied,
-- one EQUAL filter per value, executeGet -> (sumPixels, area, totalArea). Every
-- ground-type name the running game defines is measured (FieldGroundType, values
-- through getValueByType), so no enum value is assumed. Crop identity comes from
-- FieldState point samples (FieldState.new 33-56; FieldManager 327-336 samples the
-- same way). Read-only; recomputed one field per FIELD_REFRESH_MS.
Observe.MAX_FIELDS = 64
Observe.FIELD_REFRESH_MS = 3000
Observe.fieldCache = {}          -- field id -> last coverage record
Observe.fieldTimer = 0
Observe.fieldCursor = 0

local function fieldId(f)
    local id = try(function() return f:getId() end) or try(function() return f.fieldId end)
    return integer(id) and id or nil
end

local function fieldOwned(env, f, farmId)
    return try(function()
        return env.g_farmlandManager:getFarmlandOwner(f.farmland.id) == farmId
    end) == true
end

local function ownedFields(env, farmId)
    local fields = try(function() return env.g_fieldManager.fields end)
    local out = {}
    if type(fields) ~= "table" then return out end
    for _, f in ipairs(fields) do
        if fieldId(f) ~= nil and fieldOwned(env, f, farmId) then out[#out + 1] = f end
    end
    table.sort(out, function(a, b) return fieldId(a) < fieldId(b) end)
    return out
end

-- {name -> fraction} of `values` ({name -> value}) on map `kind`, and the total m2.
local function measure(env, f, kind, values)
    local m = env.g_currentMission
    local mapId, first, num = m.fieldGroundSystem:getDensityMapData(kind)
    local mod = env.DensityMapModifier.new(mapId, first, num, env.g_terrainNode)
    f:getDensityMapPolygon():applyToModifier(mod)
    local flt = env.DensityMapFilter.new(mod)
    local out, total = {}, nil
    for name, value in pairs(values) do
        flt:setValueCompareParams(env.DensityValueCompareType.EQUAL, value)
        local _, area, totalArea = mod:executeGet(flt)
        assert(finite(area) and finite(totalArea))
        total = totalArea
        if totalArea > 0 and area > 0 then out[name] = area / totalArea end
    end
    return out, total
end

-- Up to 9 FieldState samples: the field's centre and points 30% of the way from it
-- to its polygon corners. Counts of {fruit name, ground type value} pairs.
local function sampleFruit(env, f, track)
    local cx, cz = try(function() return f.posX end), try(function() return f.posZ end)
    if not (finite(cx) and finite(cz)) then return nil end
    local points = { { cx, cz } }
    -- Where the work happened (the field-work job's track, live: 9 points across the
    -- field all missed a 400 m2 strip), else across the field.
    if type(track) == "table" and #track > 0 then
        points = {}
        local step = math.max(1, math.floor(#track / 12))
        for i = 1, #track, step do
            if #points < 12 then points[#points + 1] = track[i] end
        end
    end
    local corners = try(function() return f.polygonPoints end)
    if type(corners) == "table" and #points == 1 then
        local step = math.max(1, math.floor(#corners / 8))
        for i = 1, #corners, step do
            local px, _, pz = try(function() return env.getWorldTranslation(corners[i]) end)
            if finite(px) and finite(pz) and #points < 9 then
                points[#points + 1] = { cx + 0.7 * (px - cx), cz + 0.7 * (pz - cz) }
            end
        end
    end
    local counts = {}
    for _, p in ipairs(points) do
        local state = env.FieldState.new()
        state:update(p[1], p[2])
        if state.isValid ~= false then
            local name = "NONE"
            if state.fruitTypeIndex ~= nil and state.fruitTypeIndex ~= env.FruitType.UNKNOWN then
                local desc = env.g_fruitTypeManager:getFruitTypeByIndex(state.fruitTypeIndex)
                name = token(desc and desc.name) or tostring(state.fruitTypeIndex)
            end
            counts[name] = (counts[name] or 0) + 1
        end
    end
    return counts, #points
end

function Observe.fieldCoverage(env, f, track)
    local ground = {}
    for name, v in pairs(env.FieldGroundType) do
        if type(v) == "number" and token(name) ~= nil then
            local value = try(function() return env.FieldGroundType.getValueByType(v) end)
            if integer(value) then ground[name] = value end
        end
    end
    local groundShare, total = measure(env, f, env.FieldDensityMap.GROUND_TYPE, ground)
    local maxSpray = env.g_currentMission.fieldGroundSystem:getMaxValue(env.FieldDensityMap.SPRAY_LEVEL)
    local levels = {}
    for level = 1, (integer(maxSpray) and maxSpray or 0) do levels[tostring(level)] = level end
    local sprayShare = measure(env, f, env.FieldDensityMap.SPRAY_LEVEL, levels)
    local fruit, sampled = sampleFruit(env, f, track)
    return { ground = groundShare, spray = sprayShare, total_m2 = total or Observe.NULL, fruit = fruit or Observe.NULL,
             sampled = sampled or 0 }
end

-- The owned fields' records; one stale field's coverage is recomputed per call
-- once FIELD_REFRESH_MS have passed (Observe.tickFields).
function Observe.fields(env, farmId)
    local out = Observe.array()
    for _, f in ipairs(ownedFields(env, farmId)) do
        if #out >= Observe.MAX_FIELDS then break end
        local id = fieldId(f)
        local x, z = try(function() return f.posX end), try(function() return f.posZ end)
        local area = try(function() return f:getAreaHa() end) or try(function() return f.areaHa end)
        if finite(x) and finite(z) and finite(area) then
            local c = Observe.fieldCache[id]
            out[#out + 1] = { id = id, area_ha = area, x = x, z = z,
                              coverage = c and c.record or Observe.NULL, age_ms = c and c.age or Observe.NULL }
        end
    end
    return out
end

function Observe.tickFields(env, farmId, dt)
    for _, c in pairs(Observe.fieldCache) do c.age = c.age + dt end
    Observe.fieldTimer = Observe.fieldTimer + dt
    if Observe.fieldTimer < Observe.FIELD_REFRESH_MS then return end
    Observe.fieldTimer = 0
    local fields = ownedFields(env, farmId)
    if #fields == 0 then return end
    Observe.fieldCursor = Observe.fieldCursor % #fields + 1
    local f = fields[Observe.fieldCursor]
    local ok, record = pcall(Observe.fieldCoverage, env, f)
    Observe.fieldCache[fieldId(f)] = { record = ok and record or nil, age = 0 }
end

-- Refresh one field now (the companion asks before and after field work).
function Observe.refreshField(env, id, track)
    local fields = try(function() return env.g_fieldManager.fields end)
    for _, f in ipairs(type(fields) == "table" and fields or {}) do
        if fieldId(f) == id then
            local ok, record = pcall(Observe.fieldCoverage, env, f, track)
            Observe.fieldCache[id] = { record = ok and record or nil, age = 0 }
            return ok
        end
    end
    return false
end

-- ----------------------------------------------------------------- store
-- The store's vehicle items for procurement (adapters/research/store.md):
-- g_storeManager:getItems() (StoreManager 676-678), StoreItemUtil.getIsVehicle
-- (17-19), item fields name/brandNameRaw/categoryNames/price/showInStore/isMod
-- (StoreManager 933-1035); specs loaded lazily by StoreItemUtil.loadSpecsFromXML
-- (215-230): workingWidth {width, minWidth} (Vehicle 4552-4560), neededPower
-- {base, ...} (PowerConsumer 500-520), power (Motorized 3724-3726). Built a few items
-- per snapshot so the game never stalls; sent once complete as one catalog frame.
Observe.MAX_CATALOG = 1500
Observe.CATALOG_PER_TICK = 25
Observe.catalog = nil            -- { cursor, items = array, done }

local function specNumber(v, key)
    if type(v) == "number" then return finite(v) and v or nil end
    if type(v) == "table" then
        local n = v[key] or v[1]
        if type(n) == "number" and finite(n) then return n end
    end
    return nil
end

function Observe.storeItem(env, item)
    local ok, vehicle = pcall(env.StoreItemUtil.getIsVehicle, item)
    if not ok or vehicle ~= true or item.showInStore == false or not integer(item.id) then return nil end
    if not finite(item.price) or item.price <= 0 then return nil end
    pcall(env.StoreItemUtil.loadSpecsFromXML, item)
    local specs = type(item.specs) == "table" and item.specs or {}
    local categories = Observe.array()
    for _, name in ipairs(type(item.categoryNames) == "table" and item.categoryNames or {}) do
        if #categories < 4 and token(name) then categories[#categories + 1] = name end
    end
    -- The hitch options a buyer chooses from (StoreManager item.configurations,
    -- 1018-1020; ConfigurationUtil items carry .name): live, the GMC Sierra defaults to
    -- "No Attachments" and needs "Gooseneck" to pull a gooseneck.
    local hitches = Observe.array()
    local options = type(item.configurations) == "table" and item.configurations.attacherJoint or nil
    for i, option in ipairs(type(options) == "table" and options or {}) do
        if i > 12 then break end
        hitches[#hitches + 1] = text(type(option) == "table" and option.name or nil) or ""
    end
    return { id = item.id, name = text(item.name) or Observe.NULL, brand = text(item.brandNameRaw) or Observe.NULL,
             categories = categories, price = item.price, mod = item.isMod == true, hitches = hitches,
             width = nullable(specNumber(specs.workingWidth, "width")),
             needed_power = nullable(specNumber(specs.neededPower, "base")),
             power = nullable(specNumber(specs.power, "power")) }
end

-- One step of building the catalog; returns the finished frame once, else nil.
function Observe.tickCatalog(env)
    local c = Observe.catalog
    if c == nil then
        local items = try(function() return env.g_storeManager:getItems() end)
        if type(items) ~= "table" then return nil end
        c = { cursor = 0, source = items, items = Observe.array(), done = false, sent = false }
        Observe.catalog = c
    end
    if c.done then return nil end
    for _ = 1, Observe.CATALOG_PER_TICK do
        c.cursor = c.cursor + 1
        local item = c.source[c.cursor]
        if item == nil or #c.items >= Observe.MAX_CATALOG then
            c.done, c.truncated = true, item ~= nil
            return Observe.catalogFrame()
        end
        local ok, record = pcall(Observe.storeItem, env, item)
        if ok and record ~= nil then c.items[#c.items + 1] = record end
    end
    return nil
end

-- The finished catalog frame again (a new companion session needs it too).
function Observe.catalogFrame()
    local c = Observe.catalog
    if c == nil or not c.done then return nil end
    return encode({ event = "catalog", protocol = Observe.PROTOCOL, items = c.items, truncated = c.truncated })
end

-- Money, GIANTS' helper purchase settings (missionInfo helperBuy*, Motorized
-- 2212, SowingMachine 510/1070, Sprayer 478-480) and the loaded savegame slot
-- (missionInfo.savegameIndex, UNVERIFIED): read only, never changed.
local function farmInfo(env, farmId)
    local info = try(function() return env.g_currentMission.missionInfo end) or {}
    local money = try(function() return env.g_farmManager:getFarmById(farmId).money end)
    local function flag(v) if v == true or v == false then return v end return Observe.NULL end
    return { money = finite(money) and money or Observe.NULL,
             helper_buy_seeds = flag(info.helperBuySeeds), helper_buy_fertilizer = flag(info.helperBuyFertilizer),
             helper_buy_fuel = flag(info.helperBuyFuel),
             savegame = (finite(info.savegameIndex) and info.savegameIndex == math.floor(info.savegameIndex)
                         and info.savegameIndex >= 1 and info.savegameIndex <= 99) and info.savegameIndex
                         or Observe.NULL }
end

function Observe.snapshot(env, seq, session, dt)
    local farmId = try(function() return env.g_currentMission:getFarmId() end)
    if not integer(farmId) or farmId < 1 or farmId > 255 then return nil, "NO_FARM" end
    pcall(Observe.tickFields, env, farmId, dt or 0)
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
    local AD = Observe.autoDrive(env)
    if AD ~= nil and type(AD.GetAvailableDestinations) == "function" then
        local markers = try(function() return AD:GetAvailableDestinations() end)
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

    -- The farm's own named placeables (FH Collectors.lua:628-637) at their root node
    -- (AutoDrive AutoDrivePlaceableData.lua:331): named destinations without
    -- AutoDrive. An unreadable one is left out, so it can never be a target.
    local places = Observe.array()
    local placeables = try(function() return env.g_currentMission.placeableSystem.placeables end)
    if type(placeables) == "table" then
        for _, pl in pairs(placeables) do
            if try(function() return pl.ownerFarmId == farmId end) then
                local name = text(try(function() return pl:getName() end))
                local px, _, pz = try(function() return env.getWorldTranslation(pl.rootNode) end)
                if name ~= nil and finite(px) and finite(pz) then
                    if #places >= Observe.MAX_PLACES then
                        truncated = true
                    else
                        places[#places + 1] = { name = name, x = px, z = pz }
                    end
                end
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
        places = places,
        fields = try(function() return Observe.fields(env, farmId) end) or Observe.array(),
        farm = farmInfo(env, farmId),
    }), nil
end

-- FS25 discards a sourceFile's return value, so the mod reaches this table
-- through a mod-environment global; CI loads the returned table instead.
FarmhandObserve = Observe
return Observe
