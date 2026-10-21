"""Cross-language contract: mod/FarmhandObserve.lua -> protocol.ipc -> adapters.fs25.

The Lua stubs below implement ONLY the game calls recorded as PROVEN upstream
usage in adapters/EVIDENCE.md, with the shapes those upstream mods rely on.
Passing proves our two halves agree; it does not prove FS25 behaves this way.
"""
import json
from pathlib import Path
import unittest

from lupa import lua51, lua54

from adapters.fs25 import UNKNOWN_ATTACHMENT, ObservationError, observation_from_snapshot
from dataclasses import replace

from companion.executor import compatible, power_sufficient, rejections
from companion.hitch import plan_hitch
from companion.executor import Executor
from protocol.ipc import PIPE_PATH, parse_frame

ROOT = Path(__file__).resolve().parents[1]
# Forage wagons are identified by the GIANTS ForageWagon spec, not by this map.
CATEGORIES = {"STUB_TRACTORS": "tractor"}

STUBS = r"""
local Observe = ...
local stubs = {}

local function node(x, z, yaw) return { x = x, y = 0, z = z, yaw = yaw or 0 } end

-- Build a vehicle exposing only upstream-evidenced members.
function stubs.vehicle(world, t)
    local v = { ownerFarmId = t.farm or 1, rootNode = node(t.x or 0, t.z or 0, t.yaw),
                configFileName = t.file }
    world.ids[v] = t.id
    function v:getLastSpeed() return t.speed or 0 end
    function v:getIsEntered() return t.entered == true end
    function v:getIsControlled() return t.controlled == true end
    function v:getIsAIActive() return t.ai == true end
    if t.joints then
        -- GIANTS returns the same joint tables each call; jointIndex 0 means free
        -- (AttacherJoints lines 1910, 3063). Offsets mirror AIDrivable:onPostLoad.
        local joints, offsets = {}, {}
        for i, name in ipairs(t.joints) do
            joints[i] = { jointType = world.env.AttacherJoints["JOINTTYPE_" .. name],
                          jointIndex = (t.occupied or {})[i] and 1 or 0 }
            offsets[i] = { x = 0, y = 0.5, z = (t.jointZ or {})[i] or -2.5 }
        end
        function v:getAttacherJoints() return joints end
        function v:getAttacherJointByJointDescIndex(i)
            if t.badIndexMap then return joints[i + 1] end
            return joints[i]
        end
        if t.extraOffset then offsets[#offsets + 1] = { x = 0, y = 0, z = -1 } end
        if t.missingOffset then offsets[#offsets] = nil end   -- a joint added after onPostLoad
        if t.aiDrivable then
            v.spec_aiDrivable = { attacherJointOffsets = offsets }
            function v:getAIRootNode() return v.rootNode end
        end
        function v:getAttachedImplements()
            if t.brokenImplements then error("engine error") end
            local list = {}
            for i, other in ipairs(t.implements or {}) do list[i] = { object = world.byId[other] } end
            return list
        end
    end
    -- PTO outputs per tractor joint (PowerTakeOffs:attachPowerTakeOff 749) and every
    -- input with its joints (getInputPowerTakeOffs 735, fields loaded at 563/570).
    if t.ptos then
        function v:getOutputPowerTakeOffsByJointDescIndex(i)
            if t.ptoBroken then error("engine error") end
            local outputs = {}
            for j, name in ipairs(t.ptos[i] or {}) do outputs[j] = { ptoName = name } end
            return outputs
        end
    end
    if t.ptoInputs then
        function v:getInputPowerTakeOffs()
            if t.ptoBroken then error("engine error") end
            local inputs = {}
            for i, names in pairs(t.ptoInputs) do
                for _, name in ipairs(names) do
                    inputs[#inputs + 1] = { ptoName = name, inputAttacherJointIndices = { [i] = true } }
                end
            end
            return inputs
        end
    end
    -- Loaded motor (actual configuration) and power consumer.
    if t.peak then
        function v:getMotor()
            return { getPeakTorque = function() return t.peak end,
                     getPtoMotorRpmRatio = function() return t.ratio or 2.0 end }
        end
    end
    if t.ptoTorque then
        function v:getConsumedPtoTorque(expected, ignoreTurnOnPeak)
            assert(expected == true and ignoreTurnOnPeak == true)
            return t.ptoTorque, 1
        end
    end
    -- Motorized spec table (Motorized:onLoad 367); t.towed adds Attachable.
    if t.motorized then v.spec_motorized = {} end
    if t.towed then v.spec_attachable = {} end
    -- ForageWagon/Attachable spec tables (ForageWagon:onLoad line 93).
    if t.file == "STUB_WAGONS" and t.forageWagon ~= false then
        v.spec_forageWagon, v.spec_attachable = {}, {}
    end
    if t.inputs then
        local inputs = {}
        for i, name in ipairs(t.inputs) do
            local at = (t.inputAt or {})[i] or { t.x or 0, (t.z or 0) + 4 }
            inputs[i] = { jointType = world.env.AttacherJoints["JOINTTYPE_" .. name],
                          node = node(at[1], at[2]) }
        end
        function v:getInputAttacherJoints() return inputs end
        function v:getIsInputAttacherActive(joint)
            for i, candidate in ipairs(inputs) do
                if candidate == joint then return not (t.inactive or {})[i] end
            end
            return nil
        end
        function v:getAttacherVehicle() return world.byId[t.attacher] end
    end
    -- GIANTS GOTO availability (AIJobGoTo:getIsAvailableForVehicle 92-102): agent
    -- functions from AIDrivable, start/support checks from AIJobVehicle.
    if t.gotoJob then
        function v:createAgent() end
        function v:setAITarget() end
        function v:getCanStartAIVehicle() return t.gotoJob.canStart end
        function v:getIsAIJobSupported(name)
            world.jobNames[#world.jobNames + 1] = name
            return t.gotoJob.supported
        end
    end
    -- GIANTS specialization tables by name (e.g. "spec_plow"), as each class reads them.
    for _, name in ipairs(t.specs or {}) do v[name] = {} end
    -- Foldable (GDN 674: getFoldAnimTime 962-964, spec.foldMoveDirection, foldingParts).
    if t.fold then
        v.spec_foldable = { foldingParts = t.fold.parts or { {} }, foldMoveDirection = t.fold.moving and -1 or 0 }
        function v:getFoldAnimTime()
            if t.fold.broken then error("engine error") end
            return t.fold.time
        end
    end
    -- TensionBelts (GDN 808: hasTensionBelts, areAllBeltsFastened, objectsToJoint) and the
    -- belted object's tensionMountObject (setTensionBeltsActive 875-879).
    if t.belts then
        local joints = {}
        for i, other in ipairs(t.belts.held or {}) do joints[1000 + i] = { object = world.byId[other] } end
        local sorted = {}
        for i, pair in ipairs(t.belts.markers or {}) do
            sorted[i] = { id = i, startNode = { x = pair[1], y = pair[2], z = pair[3], yaw = 0 },
                          endNode = { x = pair[4], y = pair[5], z = pair[6], yaw = 0 } }
        end
        v.spec_tensionBelts = { hasTensionBelts = true, areAllBeltsFastened = t.belts.fastened,
                                objectsToJoint = joints, sortedBelts = sorted }
    end

    -- FillUnit (GDN 672): getFillUnits L822 and per-index getters; fill types by index.
    if t.fillUnits then
        function v:getFillUnits()
            if t.fillUnitsBroken then error("engine error") end
            return t.fillUnits
        end
        function v:getFillUnitFillLevel(i) return t.fillUnits[i].level end
        function v:getFillUnitCapacity(i) return t.fillUnits[i].capacity end
        function v:getFillUnitFillType(i) return t.fillUnits[i].fillType end
        function v:getFillUnitSupportedFillTypes(i)
            local set = {}
            for _, ft in ipairs(t.fillUnits[i].supported or {}) do set[ft] = true end
            return set
        end
    end
    if t.seedUnit then function v:getSowingMachineFillUnitIndex() return t.seedUnit end end
    if t.sprayUnit then function v:getSprayerFillUnitIndex() return t.sprayUnit end end
    -- vehicle.ad only exists when the AutoDrive specialization is installed.
    if t.autodrive and world.env.AutoDrive ~= nil then
        v.ad = { stateModule = { isActive = function(self) return t.adActive == true end } }
    end
    -- AutoDrive's scratch table on an implement it handled (SpecialDrivingModule.lua:391,
    -- AutoDriveUtilFuncs.lua:35, 503): no state module, which only its specialization makes.
    if t.adScratch and world.env.AutoDrive ~= nil then v.ad = {} end
    -- Per-method behaviour: absent, returns nil, returns a non-boolean, or raises.
    local behaviours = {
        missing = function() return nil end,
        returns_nil = function() return function() return nil end end,
        returns_string = function() return function() return "true" end end,
        raises = function() return function() error("engine error") end end,
        returns_empty = function() return function() return {} end end,
        returns_unknown_object = function() return function() return {} end end,
        returns_unknown_implement = function() return function() return { { object = {} } } end end,
    }
    for method, behaviour in pairs(t.modes or {}) do
        if method == "stateModule" then
            v.ad.stateModule = nil
        elseif method == "isActive" then
            v.ad.stateModule.isActive = behaviours[behaviour]()
        else
            v[method] = behaviours[behaviour]()
        end
    end
    world.byId[t.id] = v
    world.env.g_currentMission.vehicleSystem.vehicles[#world.env.g_currentMission.vehicleSystem.vehicles + 1] = v
    return v
end

function stubs.world(options)
    options = options or {}
    local world = { ids = {}, byId = {}, jobNames = {} }
    local env = {
        AttacherJoints = { JOINTTYPE_IMPLEMENT = 1, JOINTTYPE_TRAILER = 3,
                           JOINTTYPE_TRAILERLOW = 4, JOINTTYPE_SEMITRAILER = 5 },
        NetworkUtil = { getObjectId = function(object) return world.ids[object] end },
        getWorldTranslation = function(n) return n.x, n.y, n.z end,
        -- Only the forward axis is ever queried; GIANTS yaw convention (EVIDENCE.md).
        localDirectionToWorld = function(n, x, y, z)
            assert(x == 0 and y == 0 and z == 1)
            return math.sin(n.yaw), 0, math.cos(n.yaw)
        end,
        MathUtil = { getYRotationFromDirection = function(dx, dz) return (math.atan2 or math.atan)(dx, dz) end },
        g_storeManager = { getItemByXMLFilename = function(self, file)
            if file == nil then return nil end
            return { categoryName = file, name = "Store " .. file }
        end },
        -- FillTypeManager:getFillTypeNameByIndex (GDN 409 L476): indexToName[index].
        g_fillTypeManager = { getFillTypeNameByIndex = function(self, index)
            return ({ "UNKNOWN", "SEEDS", "FERTILIZER", "LIME", "WHEAT", 'BAD"NAME' })[index]
        end },
        g_currentMission = { getFarmId = function(self) return options.farm or 1 end,
                             missionInfo = { mapId = options.mapId or "MapUS" },
                             vehicleSystem = { vehicles = {} } },
        g_localPlayer = { getPosition = function(self) return 100, 0, 200 end },
        g_modManager = { getModByName = function(self, name) return { version = "0.1.0.0" } end },
        g_currentModName = "FS25_FarmhandAC001",
    }
    if not options.noAutoDrive then
        env.AutoDrive = { version = "3.0.1.4" }
        function env.AutoDrive:GetAvailableDestinations()
            if options.brokenDestinations then error("graph not loaded") end
            return { [1] = { name = "Yard", x = 101, y = 0, z = 199, id = 1 },
                     [7] = { name = 'Field "7" \n', x = 900.5, y = 3, z = -12.25, id = 7 },
                     [8] = { name = "Hof S\195\188d", x = 5, y = 0, z = 6, id = 8 },
                     [9] = { name = "bad \255 utf8", x = 7, y = 0, z = 8, id = 9 } }
        end
    end
    -- Placeables as FH reads them (Collectors.lua:628-637) at their root node.
    if options.places then
        local list = {}
        for i, p in ipairs(options.places) do
            local pl = { ownerFarmId = p.farm or 1, rootNode = (not p.noNode) and node(p.x or 0, p.z or 0) or nil }
            function pl:getName()
                if p.brokenName then error("engine error") end
                return p.name
            end
            list[i] = pl
        end
        env.g_currentMission.placeableSystem = { placeables = list }
    end
    world.env = env
    return world
end

function stubs.standard(options)
    local world = stubs.world(options)
    local v = function(t) return stubs.vehicle(world, t) end
    v { id = 11, file = "STUB_TRACTORS", x = 0, z = 0, joints = { "TRAILER", "IMPLEMENT" }, autodrive = true }
    v { id = 12, file = "STUB_WAGONS", x = 40, z = 20, inputs = { "TRAILER" } }
    v { id = 13, file = "STUB_TRACTORS", joints = { "TRAILER" }, implements = { 14 }, autodrive = true, adActive = true }
    v { id = 14, file = "STUB_WAGONS", inputs = { "TRAILERLOW", "TRAILER" }, attacher = 13 }
    v { id = 15, file = "STUB_TRACTORS", farm = 2, joints = { "TRAILER" } }
    v { id = 16, file = "STUB_TRACTORS", joints = { "TRAILER" }, brokenImplements = true }
    v { id = 17, file = "STUB_TRACTORS", joints = { "TRAILER" }, speed = 0 / 0, entered = false, controlled = true }
    v { id = 18, file = 'BAD"CATEGORY', inputs = { "TRAILER" } }
    v { id = 19, file = "STUB_WAGONS", x = 0 / 0, inputs = { "TRAILER" } }
    return world
end

-- One farm-1 vehicle with the given spec, for method-behaviour matrices.
function stubs.single(spec)
    local world = stubs.world()
    stubs.vehicle(world, spec)
    return world
end

return stubs
"""


class LuaContractTests(unittest.TestCase):
    runtime = lua54  # CI's default Lua; FS25's embedded Lua version is UNVERIFIED.

    def setUp(self):
        self.lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        self.observe = self.lua.execute((ROOT / "mod/FarmhandObserve.lua").read_text())
        self.stubs = self.lua.execute(STUBS, self.observe)

    def snapshot(self, world, seq=1):
        line, reason = self.observe.snapshot(world.env, seq)
        self.assertIsNotNone(line, reason)
        return parse_frame(line.encode() + b"\n")

    def test_pipe_path_matches(self):
        self.assertEqual(self.observe.PIPE_PATH, PIPE_PATH)

    def test_hello_frame(self):
        world = self.stubs.standard()
        hello = parse_frame(self.observe.hello(world.env).encode())
        self.assertEqual(hello, {"event": "hello", "protocol": 3, "mod_version": "0.1.0.0", "build": "source",
                                 "autodrive_version": "3.0.1.4"})
        from companion.buildinfo import BUILD
        self.assertEqual(self.observe.BUILD, BUILD)      # both stamped by scripts/package_probe.py
        bare = self.stubs.world(self.lua.table(noAutoDrive=True))
        self.assertIsNone(parse_frame(self.observe.hello(bare.env).encode())["autodrive_version"])

    def test_autodrive_is_found_through_a_vehicles_specializations(self):
        # FS25 keeps each mod's globals in its own environment: AutoDrive is reached
        # through the class table GIANTS keeps on the vehicles it is added to.
        world = self.stubs.world(self.lua.table(noAutoDrive=True))
        self.observe.autoDriveClass = None
        self.lua.execute("""
            local env = ...
            local AD = { version = "3.0.1.4", StartDriving = function() end,
                         GetAvailableDestinations = function() return { [7] = { id = 7, name = "Store", x = 1, z = 2 } } end }
            local v = { ownerFarmId = 1, rootNode = { x = 0, y = 0, z = 0 }, specializationNames = { "motorized", "FS25_AutoDrive.AutoDrive" },
                        specializations = { {}, AD } }
            env.g_currentMission.vehicleSystem = { vehicles = { v } }
        """, world.env)
        hello = parse_frame(self.observe.hello(world.env).encode())
        self.assertEqual(hello["autodrive_version"], "3.0.1.4")
        self.assertEqual(self.snapshot(world)["destinations"], [{"id": 7, "name": "Store", "x": 1, "z": 2}])
        self.observe.autoDriveClass = None

    def test_module_is_exposed_for_fs25(self):
        # FS25 discards a sourceFile's return value; the mod reaches it by name.
        self.assertTrue(self.lua.eval("function(t) return FarmhandObserve == t end")(self.observe))

    def test_snapshot_frame_maps_through_the_contract(self):
        frame = self.snapshot(self.stubs.standard(), seq=42)
        self.assertEqual((frame["seq"], frame["farm_id"], frame["player"]),
                         (42, 1, {"x": 100, "z": 200}))
        self.assertEqual(frame["skipped"], 1)        # vehicle 19: position unobservable
        self.assertFalse(frame["truncated"])
        self.assertEqual(frame["map_id"], "MapUS")
        # Control characters or invalid UTF-8 blank a name, not the frame.
        self.assertEqual(sorted((d["id"], d["name"], d["x"], d["z"]) for d in frame["destinations"]),
                         [(1, "Yard", 101, 199), (7, "", 900.5, -12.25),
                          (8, "Hof S\u00fcd", 5, 6), (9, "", 7, 8)])
        observed = observation_from_snapshot(frame, CATEGORIES)
        items = {e.id: e for e in observed.equipment}
        self.assertEqual(sorted(items), ["11", "12", "13", "14", "16", "17", "18"])  # farm 2 excluded
        # Vehicle 16's getAttachedImplements raises: reported as unknown, never as free.
        self.assertEqual(items["16"].attached_to, UNKNOWN_ATTACHMENT)
        self.assertFalse(items["16"].available)
        self.assertEqual(observed.map_id, "MapUS")
        self.assertEqual((items["11"].category, items["12"].category), ("tractor", "forage_wagon"))
        self.assertEqual(items["11"].output_joints, ())     # no AIDrivable offsets: no geometry
        self.assertEqual([j.joint_type for j in items["14"].input_joints], ["TRAILERLOW", "TRAILER"])
        self.assertEqual(items["12"].position, (40, 20))
        self.assertTrue(items["11"].available and items["11"].stopped)
        self.assertEqual((items["13"].attached_to, items["14"].attached_to), ("14", "13"))
        self.assertTrue(items["13"].busy)                       # AutoDrive active
        self.assertFalse(items["17"].available)                 # NaN speed is unobserved
        self.assertTrue(items["17"].ridden and not items["17"].occupied)   # getIsControlled(): ride-along
        self.assertEqual(items["18"].category, "unknown")       # unsafe category rejected
        self.assertEqual(observed.farm_id, "1")

    def vehicle_record(self, spec):
        frame = self.snapshot(self.stubs.single(self.lua.eval(spec)))
        record, = frame["vehicles"]
        item, = observation_from_snapshot(frame, CATEGORIES).equipment
        return record, item

    def test_occupancy_and_ai_unknown_versus_observed(self):
        tractor = ('{ id = 21, file = "STUB_TRACTORS", joints = { "TRAILER" }, autodrive = true,'
                   '  entered = %s, controlled = %s, ai = %s, adActive = %s, modes = { %s } }')
        cases = [
            # (entered, controlled, ai, adActive, modes) -> (field, frame value)
            (("false", "false", "false", "false", ""), {"entered": False, "ai_active": False, "ad_active": False}),
            (("true", "false", "false", "false", ""), {"entered": True}),
            (("false", "true", "false", "false", ""), {"entered": True}),
            (("false", "false", "true", "false", ""), {"ai_active": True}),
            (("false", "false", "false", "true", ""), {"ad_active": True}),
            (("false", "false", "false", "false", "getIsEntered = 'returns_nil'"), {"entered": None}),
            (("false", "false", "false", "false", "getIsEntered = 'returns_string'"), {"entered": None}),
            (("false", "false", "false", "false", "getIsEntered = 'raises'"), {"entered": None}),
            (("false", "false", "false", "false", "getIsControlled = 'returns_nil'"), {"entered": None}),
            (("true", "false", "false", "false", "getIsControlled = 'returns_nil'"), {"entered": True}),
            (("false", "false", "false", "false", "getIsAIActive = 'returns_nil'"), {"ai_active": None}),
            (("false", "false", "false", "false", "getIsAIActive = 'raises'"), {"ai_active": None}),
            (("false", "false", "false", "false", "isActive = 'returns_nil'"), {"ad_active": None, "has_autodrive": True}),
            (("false", "false", "false", "false", "stateModule = 'missing'"), {"ad_active": None, "has_autodrive": True}),
            # Not applicable by upstream rule (EVIDENCE.md): a missing method is false.
            (("false", "false", "false", "false", "getIsControlled = 'missing'"), {"entered": False}),
            (("false", "false", "false", "false", "getIsEntered = 'missing', getIsControlled = 'missing'"),
             {"entered": False}),
            (("false", "false", "false", "false", "getIsAIActive = 'missing'"), {"ai_active": False}),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                record, item = self.vehicle_record(tractor % args)
                for field, value in expected.items():
                    self.assertIs(record[field], value)
                unknown = any(record[f] is None for f in ("entered", "ai_active", "ad_active"))
                # A seated requester is a ride-along, not a conflict; AI is.
                free = not any(record[f] for f in ("ai_active", "ad_active"))
                self.assertEqual(item.available, not unknown)
                self.assertEqual(not item.occupied and not item.busy, free and not unknown)
                self.assertEqual(item.ridden, record["entered"] is True)

    def test_what_a_vehicle_is_comes_from_giants_specialization_tables(self):
        spec = ('{ id = 41, file = "STUB_WAGONS", inputs = { "IMPLEMENT" }, forageWagon = false,'
                '  specs = { "spec_sowingMachine", "spec_sprayer", "spec_fillUnit", "spec_aiImplement", "spec_unknownMod" } }')
        record, item = self.vehicle_record(spec)
        self.assertEqual(record["kinds"], ["sowing_machine", "sprayer", "fill_unit", "ai_implement"])   # fixed order
        self.assertEqual(record["name"], "Store STUB_WAGONS")
        self.assertEqual(item.category, "implement")
        self.assertEqual(item.kinds, frozenset({"sowing_machine", "sprayer", "fill_unit", "ai_implement"}))
        record, item = self.vehicle_record('{ id = 42, file = "STUB_WAGONS", inputs = { "TRAILER" }, forageWagon = false }')
        self.assertEqual((record["kinds"], item.category), ([], "implement"))    # a towed vehicle with a hitch
        record, item = self.vehicle_record('{ id = 43, file = "STUB_WAGONS", forageWagon = false, specs = { "spec_pallet" } }')
        self.assertEqual((record["kinds"], item.category), (["pallet"], "unknown"))   # pallets are not implements

    def test_fill_units_are_read_from_giants_fill_unit_getters(self):
        spec = ('{ id = 44, file = "STUB_WAGONS", inputs = { "IMPLEMENT" }, forageWagon = false,'
                '  specs = { "spec_sowingMachine", "spec_sprayer", "spec_fillUnit" }, seedUnit = 1, sprayUnit = 2,'
                '  fillUnits = { { level = 1250.5, capacity = 3000, fillType = 2, supported = { 2 } },'
                '                { level = 0, capacity = 1500, fillType = 1, supported = { 3, 4, 6 } },'
                '                { level = 0/0, capacity = -1, fillType = 99, supported = {} } } }')
        record, item = self.vehicle_record(spec)
        self.assertEqual(record["fill_units"], [
            {"index": 1, "fill_type": "SEEDS", "level": 1250.5, "capacity": 3000, "supported": ["SEEDS"], "role": "seed"},
            {"index": 2, "fill_type": "UNKNOWN", "level": 0, "capacity": 1500, "supported": ["FERTILIZER", "LIME"],
             "role": "spray"},                                   # an unsafe fill type name is dropped
            {"index": 3, "fill_type": None, "level": None, "capacity": None, "supported": [], "role": None}])
        seed, spray, _ = item.fill_units
        self.assertEqual((seed.fill_type, seed.level, seed.role), ("SEEDS", 1250.5, "seed"))
        self.assertEqual(spray.supported, frozenset({"FERTILIZER", "LIME"}))
        for broken in ("fillUnitsBroken = true", "x = 0"):            # unreadable, or no FillUnit at all
            with self.subTest(broken=broken):
                record, _ = self.vehicle_record('{ id = 45, file = "STUB_WAGONS", fillUnits = {}, %s }' % broken)
                self.assertEqual(record["fill_units"], [])

    def test_folding_parts_and_belts_are_observed_from_giants_specializations(self):
        # Tailgates, ramps and belts (the user's completion plan, stage 6): what GIANTS reports.
        world = self.stubs.world()
        for spec in ('{ id = 61, file = "STUB_TRACTORS", motorized = true }',
                     '{ id = 62, file = "STUB_WAGONS", forageWagon = false, fold = { time = 1, moving = false },'
                     '  belts = { fastened = true, held = { 61 }, markers = { { 10, 1.2, 4, 13, 1.2, 4 },'
                     '                                                          { 10, 1.2, -2, 13, 1.2, -2 } } } }',
                     '{ id = 63, file = "STUB_WAGONS", forageWagon = false, fold = { time = 0.4, moving = true },'
                     '  belts = { fastened = false } }',
                     '{ id = 64, file = "STUB_WAGONS", forageWagon = false, fold = { time = 0, broken = true } }'):
            self.stubs.vehicle(world, self.lua.eval(spec))
        self.lua.execute("local w = ...; w.byId[61].tensionMountObject = w.byId[62]", world)
        frame = self.snapshot(world)
        records = {r["id"]: r for r in frame["vehicles"]}
        self.assertEqual(records[62]["fold"], {"time": 1, "moving": False})
        self.assertEqual(records[62]["belts"], {"fastened": True, "held": [61],
                                                "markers": [[10, 1.2, 4, 13, 1.2, 4], [10, 1.2, -2, 13, 1.2, -2]]})
        self.assertEqual(records[61]["belted_to"], 62)
        self.assertEqual(records[63]["fold"], {"time": 0.4, "moving": True})
        self.assertEqual(records[63]["belts"], {"fastened": False, "held": [], "markers": []})
        self.assertIsNone(records[64]["fold"])                       # unreadable: unknown, never a state
        self.assertIsNone(records[61]["fold"])                       # nothing that folds
        self.assertIsNone(records[64]["belted_to"])
        items = {i.id: i for i in observation_from_snapshot(frame, CATEGORIES).equipment}
        self.assertEqual((items["62"].fold, items["62"].folding), (1, False))
        self.assertEqual((items["62"].belts_fastened, items["62"].belted), (True, ("61",)))
        self.assertEqual(items["62"].belt_markers, ((10, 1.2, 4, 13, 1.2, 4), (10, 1.2, -2, 13, 1.2, -2)))
        self.assertEqual(items["61"].belted_to, "62")
        self.assertEqual((items["64"].fold, items["64"].belts_fastened, items["64"].belted_to), (None, None, None))

    def test_autodrive_scratch_table_on_a_towed_wagon_is_not_an_autodrive_driver(self):
        # Live 2026-10-18: after one attach/detach with an AutoDrive tractor the wagon
        # kept AD's scratch `ad = {}` and was rejected AI_STATE_UNKNOWN for good.
        wagon = '{ id = 32, file = "STUB_WAGONS", inputs = { "TRAILER" }, %s }'
        record, item = self.vehicle_record(wagon % "adScratch = true")
        self.assertIs(record["has_autodrive"], False)
        self.assertIsNone(record["ad_active"])
        self.assertTrue(item.available and not item.busy)
        self.assertEqual(rejections(item, "1"), [])
        # A towed vehicle whose state module exists but cannot be read stays unknown.
        record, item = self.vehicle_record(wagon % "autodrive = true, modes = { isActive = 'raises' }")
        self.assertIs(record["has_autodrive"], True)
        self.assertIsNone(record["ad_active"])
        self.assertIn("AI_STATE_UNKNOWN", rejections(item, "1"))
        # A towed vehicle that AutoDrive is driving through a state module is busy.
        record, item = self.vehicle_record(wagon % "autodrive = true, adActive = true")
        self.assertIs(record["ad_active"], True)
        self.assertIn("AUTODRIVE_ACTIVE", rejections(item, "1"))
        # A self-propelled vehicle with `ad` but no state module stays unknown.
        tractor = '{ id = 21, file = "STUB_TRACTORS", joints = { "TRAILER" }, motorized = true, adScratch = true }'
        record, item = self.vehicle_record(tractor)
        self.assertIs(record["has_autodrive"], True)
        self.assertIn("AI_STATE_UNKNOWN", rejections(item, "1"))
        # Neither Attachable nor Motorized readable as towed: unknown as before.
        record, item = self.vehicle_record('{ id = 22, file = "STUB_TRACTORS", joints = { "TRAILER" }, adScratch = true }')
        self.assertIn("AI_STATE_UNKNOWN", rejections(item, "1"))

    def test_giants_goto_availability_is_observed(self):
        tractor = '{ id = 21, file = "STUB_TRACTORS", joints = { "TRAILER" } %s }'
        cases = [("", False),                                     # no navigation agent: no GOTO
                 (", gotoJob = { canStart = true, supported = true }", True),
                 # "Can start now" (helper limit, AI running) is not a vehicle property.
                 (", gotoJob = { canStart = false, supported = true }", True),
                 (", gotoJob = { canStart = true, supported = false }", False),
                 (", gotoJob = { canStart = true, supported = 'yes' }", None),
                 (", gotoJob = { canStart = true, supported = true }, modes = { getIsAIJobSupported = 'raises' }",
                  None)]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                record, item = self.vehicle_record(tractor % extra)
                self.assertIs(record["goto_supported"], expected)
                ready = observation_from_snapshot(self.snapshot(self.stubs.single(self.lua.eval(
                    tractor % extra))), CATEGORIES, bridge_ready=True).equipment[0]
                self.assertEqual(ready.capabilities >= {"move", "attach", "stop"}, expected is True)
        world = self.stubs.single(self.lua.eval(tractor % ", gotoJob = { canStart = true, supported = true }"))
        self.snapshot(world)
        self.assertEqual(list(world.jobNames.values()), ["AIJobGoTo"])

    def test_self_propelled_is_observed_from_giants_specializations(self):
        cases = [('{ id = 21, file = "STUB_TRACTORS", motorized = true }', True),
                 ('{ id = 21, file = "STUB_TRACTORS", motorized = true, towed = true }', False),
                 ('{ id = 21, file = "STUB_TRACTORS" }', False),
                 ('{ id = 21, file = "STUB_WAGONS", inputs = { "TRAILER" } }', False)]
        for spec, expected in cases:
            with self.subTest(spec=spec):
                record, _ = self.vehicle_record(spec)
                self.assertIs(record["self_propelled"], expected)

    def test_unconfigured_lua_observed_tractor_is_recognised(self):
        spec = ('{ id = 21, file = "ANY_STORE_CATEGORY", motorized = true, joints = { "TRAILER" }, aiDrivable = true,'
                '  ptos = { { "rearPto" } }, peak = 500 }')
        frame = self.snapshot(self.stubs.single(self.lua.eval(spec)))
        item, = observation_from_snapshot(frame).equipment
        self.assertEqual(item.category, "tractor")
        spec = spec.replace("motorized = true", "motorized = true, towed = true")
        item, = observation_from_snapshot(self.snapshot(self.stubs.single(self.lua.eval(spec)))).equipment
        self.assertEqual(item.category, "unknown")

    def test_owned_named_places_are_observed(self):
        world = self.stubs.world(self.lua.eval("""{ places = {
            { name = "Cow Barn (Medium)", x = -200, z = 50 },
            { name = "Neighbour Barn", x = 5, z = 5, farm = 2 },
            { name = "Broken", brokenName = true },
            { name = "Bad \\1 name" },
            { name = "Nowhere", noNode = true },
            { name = "Machine Shed", x = 300.5, z = -80 } } }"""))
        frame = self.snapshot(world)
        self.assertEqual(frame["places"], [{"name": "Cow Barn (Medium)", "x": -200, "z": 50},
                                           {"name": "Machine Shed", "x": 300.5, "z": -80}])
        observed = observation_from_snapshot(frame)
        self.assertEqual([(p.name, p.position) for p in observed.places],
                         [("Cow Barn (Medium)", (-200, 50)), ("Machine Shed", (300.5, -80))])
        self.assertEqual(self.snapshot(self.stubs.world())["places"], [])     # no placeable system

    FIELD_STUBS = r"""
        local env, observe = ...
        -- GIANTS' field objects with the calls the field missions use (PlowMission
        -- createModifier 40-47, AbstractFieldMission 370-448; FieldState 33-56).
        -- Each field's density map is a table: map kind -> {value -> m2}.
        local function field(id, farmland, x, z, area, maps, fruit)
            local f = { fieldId = id, farmland = { id = farmland }, posX = x, posZ = z, maps = maps,
                        polygonPoints = {}, fruit = fruit }
            function f:getId() return id end
            function f:getAreaHa() return area end
            function f:getDensityMapPolygon()
                return { applyToModifier = function(_, mod) mod.field = f end }
            end
            return f
        end
        local fields = {
            field(7, 70, 100, 200, 0.4, { [1] = { [3] = 3000, [5] = 1000 }, [2] = { [1] = 400 } }, 2),
            field(9, 90, 500, 600, 1.2, { [1] = { [3] = 12000 }, [2] = {} }, 0),
            field(8, 80, -50, -60, 2.0, { [1] = { [3] = 20000 }, [2] = {} }, 0),   -- not ours
        }
        env.g_fieldManager = { fields = fields }
        local owners = { [70] = 1, [90] = 1, [80] = 2 }
        env.g_farmlandManager = { getFarmlandOwner = function(_, id) return owners[id] end }
        env.FieldGroundType = { CULTIVATED = 103, SOWN = 105, getValueByType = function(t) return t - 100 end }
        env.FieldDensityMap = { GROUND_TYPE = 1, SPRAY_LEVEL = 2 }
        env.DensityValueCompareType = { EQUAL = "eq" }
        env.g_currentMission.fieldGroundSystem = {
            getDensityMapData = function(_, kind) return kind, 0, 4 end,
            getMaxValue = function(_, kind) return 2 end }
        env.g_terrainNode = {}
        env.DensityMapModifier = { new = function(kind) return { kind = kind, executeGet = function(mod, flt)
            local total = 0
            for _, m2 in pairs(mod.field.maps[1]) do total = total + m2 end
            return 0, mod.field.maps[mod.kind][flt.value] or 0, total
        end } end }
        env.DensityMapFilter = { new = function() return { setValueCompareParams = function(flt, cmp, v)
            assert(cmp == "eq"); flt.value = v end } end }
        env.FruitType = { UNKNOWN = 0 }
        env.g_fruitTypeManager = { getFruitTypeByIndex = function(_, i) return { name = i == 2 and "WHEAT" or "X" } end }
        env.FieldState = { new = function()
            return { isValid = true, update = function(st, x, z)
                for _, f in ipairs(fields) do if f.posX == x then st.fruitTypeIndex = f.fruit end end
            end }
        end }
        env.g_farmManager = { getFarmById = function() return { money = 125000.5 } end }
        env.g_currentMission.missionInfo = { helperBuySeeds = true, helperBuyFertilizer = false, savegameIndex = 2 }
    """

    def test_owned_fields_and_their_coverage_are_observed(self):
        world = self.stubs.world()
        self.lua.execute(self.FIELD_STUBS, world.env, self.observe)
        frame = self.snapshot(world)
        self.assertEqual([f["id"] for f in frame["fields"]], [7, 9])              # owned only, by id
        self.assertTrue(all(f["coverage"] is None for f in frame["fields"]))      # not measured yet
        self.assertEqual(frame["farm"], {"money": 125000.5, "helper_buy_seeds": True,
                                         "helper_buy_fertilizer": False, "helper_buy_fuel": None,
                                         "savegame": 2})
        self.assertTrue(self.observe.refreshField(world.env, 7))
        observed = observation_from_snapshot(self.snapshot(world))
        field = observed.fields[0]
        self.assertEqual((field.id, field.area_ha, field.center), (7, 0.4, (100, 200)))
        c = field.coverage
        self.assertEqual(c.ground, (("CULTIVATED", 0.75), ("SOWN", 0.25)))
        self.assertEqual((c.share("CULTIVATED"), c.sprayed(), c.total_m2), (0.75, 0.1, 4000))
        self.assertEqual((c.fruit, c.sampled), ((("WHEAT", 1),), 1))
        self.assertEqual((observed.farm.money, observed.farm.helper_buy_seeds), (125000.5, True))

    def test_crop_is_sampled_where_the_work_happened(self):
        # Live: 9 points across field 8 all missed a 400 m2 sown strip.
        world = self.stubs.world()
        self.lua.execute(self.FIELD_STUBS, world.env, self.observe)
        track = self.lua.eval("function(n) local t = {} for i = 1, n do t[i] = { 100, i } end return t end")
        self.assertTrue(self.observe.refreshField(world.env, 7, track(30)))
        c = observation_from_snapshot(self.snapshot(world)).fields[0].coverage
        self.assertEqual(c.fruit, (("WHEAT", 12),))                       # at most 12 points
        self.assertEqual(c.sampled, 12)

    def test_field_coverage_refreshes_one_field_at_a_time(self):
        world = self.stubs.world()
        self.lua.execute(self.FIELD_STUBS, world.env, self.observe)
        self.observe.snapshot(world.env, 1, None, 3000)
        measured = [f["id"] for f in self.snapshot(world)["fields"] if f["coverage"] is not None]
        self.assertEqual(len(measured), 1)
        self.observe.snapshot(world.env, 2, None, 3000)
        self.assertEqual(len([f for f in self.snapshot(world)["fields"] if f["coverage"] is not None]), 2)

    def test_a_joint_whose_node_left_its_load_time_offset_is_not_reported(self):
        # Live FourFields 7R joint 2: 24 m from where its offset puts it, in world;
        # the plan used the offset and the maneuver refused (OUTSIDE_LOCAL_AREA).
        world = self.stubs.single(self.lua.eval(
            '{ id = 21, file = "STUB_TRACTORS", joints = { "IMPLEMENT", "IMPLEMENT", "TRAILER" }, aiDrivable = true }'))
        self.lua.execute("""
            local world = ...
            local joints = world.byId[21]:getAttacherJoints()
            joints[1].jointTransform = { x = 0.2, y = 0.5, z = -2.4 }    -- where its offset says
            joints[2].jointTransform = { x = 8, y = 0.5, z = 24 }        -- far from it in world
            -- joints[3]: no node to compare (stub), reported from its offset
            -- Live: worldToLocal still gave joint 2 its offset; the world point decides.
            world.env.worldToLocal = function() return 0, 0.5, -2.5 end
        """, world)
        record = self.snapshot(world)["vehicles"][0]
        self.assertEqual([j["index"] for j in record["output_joints"]], [1, 3])

    def test_attachment_unknown_versus_empty(self):
        tractor = '{ id = 21, file = "STUB_TRACTORS", joints = { "TRAILER" }, modes = { %s } }'
        wagon = '{ id = 22, file = "STUB_WAGONS", inputs = { "TRAILER" }, modes = { %s } }'
        no_joints = '{ id = 23, file = "STUB_WAGONS", modes = { %s } }'
        cases = [
            (tractor % "", True),
            (tractor % "getAttachedImplements = 'returns_empty'", True),
            (wagon % "", True),                                   # getAttacherVehicle() -> nil
            (no_joints % "", True),                               # no attachment side at all
            (tractor % "getAttachedImplements = 'returns_nil'", False),
            (tractor % "getAttachedImplements = 'returns_string'", False),
            (tractor % "getAttachedImplements = 'raises'", False),
            (tractor % "getAttachedImplements = 'returns_unknown_implement'", False),
            (tractor % "getAttachedImplements = 'missing'", False),  # has joints, no state
            (wagon % "getAttacherVehicle = 'raises'", False),
            (wagon % "getAttacherVehicle = 'returns_unknown_object'", False),
            (wagon % "getAttacherVehicle = 'missing'", False),       # has joints, no state
        ]
        for spec, observed in cases:
            with self.subTest(spec=spec):
                record, item = self.vehicle_record(spec)
                self.assertIs(record["attachment_observed"], observed)
                self.assertEqual((record["attacher_vehicle_id"], record["implement_ids"]), (None, []))
                self.assertEqual(item.attached_to, None if observed else UNKNOWN_ATTACHMENT)
                self.assertEqual(item.available, observed)

    def test_hitch_geometry_is_observed(self):
        tractor = ('{ id = 31, file = "STUB_TRACTORS", x = 5, z = 7, yaw = 0.5, aiDrivable = true,'
                   '  joints = { "TRAILER", "IMPLEMENT", "TRAILER" }, jointZ = { -2.5, 3.0, -2.6 },'
                   '  occupied = { [3] = true } }')
        record, item = self.vehicle_record(tractor)
        self.assertAlmostEqual(record["heading"], 0.5)
        self.assertEqual(record["ai_root"], {"x": 5, "z": 7, "yaw": record["ai_root"]["yaw"]})
        self.assertAlmostEqual(record["ai_root"]["yaw"], 0.5)
        self.assertEqual(record["output_joints"], [
            {"index": 1, "type": "TRAILER", "x": 0, "z": -2.5, "free": True, "pto_names": []},
            {"index": 2, "type": "IMPLEMENT", "x": 0, "z": 3, "free": True, "pto_names": []},
            {"index": 3, "type": "TRAILER", "x": 0, "z": -2.6, "free": False, "pto_names": []}])
        self.assertEqual(item.ai_root.x, 5)
        self.assertEqual([j.index for j in item.output_joints if j.free], [1, 2])

        wagon = ('{ id = 32, file = "STUB_WAGONS", x = 40, z = 20, yaw = -1.0,'
                 '  inputs = { "TRAILERLOW", "TRAILER" }, inputAt = { { 41, 24 }, { 40.5, 24.25 } },'
                 '  inactive = { [1] = true } }')
        record, item = self.vehicle_record(wagon)
        self.assertAlmostEqual(record["heading"], -1.0)
        self.assertIsNone(record["ai_root"])
        self.assertEqual(record["output_joints"], [])
        self.assertEqual(record["input_joint_poses"], [
            {"index": 1, "type": "TRAILERLOW", "x": 41, "z": 24, "active": False, "pto_names": []},
            {"index": 2, "type": "TRAILER", "x": 40.5, "z": 24.25, "active": True, "pto_names": []}])
        self.assertEqual([(j.index, j.position, j.active) for j in item.input_joints],
                         [(1, (41, 24), False), (2, (40.5, 24.25), True)])

    def test_unknown_hitch_geometry_is_never_usable(self):
        tractor = ('{ id = 31, file = "STUB_TRACTORS", aiDrivable = true, joints = { "TRAILER" },'
                   '  modes = { %s } }')
        wagon = '{ id = 32, file = "STUB_WAGONS", inputs = { "TRAILER" }, modes = { %s } }'
        cases = [
            # Index mapping disagrees with GIANTS' own lookup: geometry unknown.
            ('{ id = 31, file = "STUB_TRACTORS", aiDrivable = true, joints = { "TRAILER" }, badIndexMap = true }',
             "output_joints", []),
            # More offsets than joints (joints removed since onPostLoad): unknown.
            ('{ id = 31, file = "STUB_TRACTORS", aiDrivable = true, joints = { "TRAILER" }, extraOffset = true }',
             "output_joints", []),
            # A joint added after onPostLoad (live: the GMC's bed): the others keep their index.
            ('{ id = 31, file = "STUB_TRACTORS", aiDrivable = true, joints = { "TRAILER", "IMPLEMENT" },'
             '  missingOffset = true }', "output_joints",
             [{"index": 1, "type": "TRAILER", "x": 0, "z": -2.5, "free": True, "pto_names": []}]),
            (tractor % "getAIRootNode = 'raises'", "ai_root", None),
            (tractor % "getAIRootNode = 'missing'", "ai_root", None),
            # Without AIDrivable there are no GIANTS offsets, hence no tractor geometry.
            ('{ id = 31, file = "STUB_TRACTORS", joints = { "TRAILER" } }', "output_joints", []),
            (wagon % "getIsInputAttacherActive = 'returns_nil'", "input_joint_poses",
             [{"index": 1, "type": "TRAILER", "x": 0, "z": 4, "active": False, "pto_names": []}]),
            (wagon % "getIsInputAttacherActive = 'raises'", "input_joint_poses",
             [{"index": 1, "type": "TRAILER", "x": 0, "z": 4, "active": False, "pto_names": []}]),
            (wagon % "getIsInputAttacherActive = 'missing'", "input_joint_poses",
             [{"index": 1, "type": "TRAILER", "x": 0, "z": 4, "active": False, "pto_names": []}]),
        ]
        for spec, field, expected in cases:
            with self.subTest(spec=spec, field=field):
                record, _ = self.vehicle_record(spec)
                self.assertEqual(record[field], expected)
        # Unreadable joint lists leave the vehicle observed but with no joints, so
        # it can never be planned; nothing is guessed.
        for spec, field in ((tractor % "getAttacherJoints = 'raises'", "output_joints"),
                            (wagon % "getInputAttacherJoints = 'raises'", "input_joint_poses")):
            with self.subTest(spec=spec):
                record, _ = self.vehicle_record(spec)
                self.assertEqual(record[field], [])
        # A vehicle whose root direction cannot be read has no heading.
        world = self.stubs.single(self.lua.eval(wagon % ""))
        world.env.localDirectionToWorld = self.lua.eval("function() error('engine error') end")
        record, = self.snapshot(world)["vehicles"]
        self.assertIsNone(record["heading"])

    def test_lua_observed_pair_produces_a_hitch_plan(self):
        world = self.stubs.world()
        self.stubs.vehicle(world, self.lua.eval(
            '{ id = 31, file = "STUB_TRACTORS", x = 0, z = 0, aiDrivable = true,'
            '  joints = { "IMPLEMENT", "TRAILER" }, jointZ = { 3.0, -2.5 } }'))
        self.stubs.vehicle(world, self.lua.eval(
            '{ id = 32, file = "STUB_WAGONS", x = 40, z = 20, yaw = 1.2, inputs = { "TRAILER" },'
            '  inputAt = { { 43.7, 21.4 } } }'))
        items = {e.id: e for e in observation_from_snapshot(self.snapshot(world), CATEGORIES).equipment}
        plan = plan_hitch(items["31"], items["32"])
        self.assertEqual((plan.tractor_joint, plan.wagon_joint), (2, 1))
        self.assertEqual(plan.target, (43.7, 21.4))
        self.assertAlmostEqual(plan.final.yaw, 1.2)

    def test_power_pto_and_wagon_class_are_observed(self):
        world = self.stubs.world()
        # The store lists two motor configurations; only the loaded motor is read.
        world.env.g_storeManager = self.lua.eval(
            '{ getItemByXMLFilename = function(self, file) return { categoryName = file,'
            '  configurations = { motor = { { power = 100 }, { power = 300 } } }, specs = { power = 300 } } end }')
        self.stubs.vehicle(world, self.lua.eval(
            '{ id = 31, file = "STUB_TRACTORS", aiDrivable = true, joints = { "IMPLEMENT", "TRAILER" },'
            '  jointZ = { 3.0, -2.5 }, peak = 1.3, ratio = 2.5, ptos = { [1] = { "frontPto" }, [2] = { "rearPto" } } }'))
        self.stubs.vehicle(world, self.lua.eval(
            '{ id = 32, file = "STUB_WAGONS", x = 0, z = 0, inputs = { "TRAILER" }, inputAt = { { 0, 6.5 } },'
            '  ptoTorque = 1.7, ptoInputs = { [1] = { "rearPto", "ownOnly" } } }'))
        frame = self.snapshot(world)
        records = {r["id"]: r for r in frame["vehicles"]}
        self.assertEqual(records[31]["motor"], {"peak_torque": 1.3, "pto_rpm_ratio": 2.5})
        self.assertEqual(records[31]["pto_torque"], 0)          # no PowerConsumer: no need
        self.assertEqual([j["pto_names"] for j in records[31]["output_joints"]], [["frontPto"], ["rearPto"]])
        self.assertEqual(records[32]["pto_torque"], 1.7)
        self.assertTrue(records[32]["forage_wagon"])
        self.assertFalse(records[31]["forage_wagon"])
        # Every input name GIANTS holds at the joint, matched or not.
        self.assertEqual(records[32]["input_joint_poses"][0]["pto_names"], ["ownOnly", "rearPto"])

        items = {e.id: e for e in observation_from_snapshot(frame, CATEGORIES).equipment}
        tractor, wagon = items["31"], items["32"]
        self.assertEqual((tractor.category, wagon.category), ("tractor", "forage_wagon"))
        self.assertAlmostEqual(tractor.pto_torque_capacity, 0.9 * 1.3 * 2.5)
        self.assertTrue(power_sufficient(tractor, wagon))         # 2.925 >= 1.7
        plan = plan_hitch(tractor, wagon)
        self.assertEqual((plan.tractor_joint, plan.wagon_joint), (2, 1))
        # Eligible but for the deliberate gate: no command channel grants move/attach.
        self.assertFalse(compatible(tractor, wagon))
        self.assertTrue(compatible(replace(tractor, capabilities=frozenset({"move", "attach", "stop"})), wagon))
        # A weaker loaded motor is rejected by the same GIANTS rule.
        world.byId[31].getMotor = self.lua.eval(
            'function() return { getPeakTorque = function() return 0.7 end,'
            '                    getPtoMotorRpmRatio = function() return 2.5 end } end')
        items = {e.id: e for e in observation_from_snapshot(self.snapshot(world), CATEGORIES).equipment}
        self.assertFalse(power_sufficient(items["31"], items["32"]))   # 1.575 < 1.7

    def test_unknown_power_or_pto_is_reported_unknown(self):
        tractor = '{ id = 31, file = "STUB_TRACTORS", aiDrivable = true, joints = { "TRAILER" }, peak = %s, %s }'
        wagon = '{ id = 32, file = "STUB_WAGONS", inputs = { "TRAILER" }, %s }'
        cases = [
            (tractor % ("0/0", "ptos = {}"), lambda r: r["motor"], None),
            # No usable PTO ratio (live: the GMC Sierra): the motor is known, its PTO is not.
            (tractor % ("1", "ratio = 0, ptos = {}"), lambda r: r["motor"], {"peak_torque": 1, "pto_rpm_ratio": None}),
            (tractor % ("1", "ptos = {}, modes = { getMotor = 'raises' }"), lambda r: r["motor"], None),
            (tractor % ("1", "ptos = {}, modes = { getMotor = 'missing' }"), lambda r: r["motor"], None),
            (tractor % ("1", "ptos = { [1] = { 'rearPto' } }, ptoBroken = true"),
             lambda r: r["output_joints"][0]["pto_names"], None),
            (tractor % ("1", "ptos = { [1] = { 7 } }"), lambda r: r["output_joints"][0]["pto_names"], None),
            # No PowerTakeOffs output method at all: observed, no PTO outputs.
            (tractor % ("1", "x = 0"), lambda r: r["output_joints"][0]["pto_names"], []),
            (wagon % "ptoTorque = -1", lambda r: r["pto_torque"], None),
            (wagon % "ptoTorque = 0/0", lambda r: r["pto_torque"], None),
            (wagon % "ptoTorque = 1, modes = { getConsumedPtoTorque = 'raises' }", lambda r: r["pto_torque"], None),
            (wagon % "ptoInputs = {}, ptoBroken = true", lambda r: r["input_joint_poses"][0]["pto_names"], None),
            # No PowerConsumer / no PTO inputs: observed absence, not unknown.
            (wagon % "x = 0", lambda r: r["pto_torque"], 0),
            (wagon % "x = 0", lambda r: r["input_joint_poses"][0]["pto_names"], []),
            (wagon % "forageWagon = false", lambda r: r["forage_wagon"], False),
        ]
        for spec, get, expected in cases:
            with self.subTest(spec=spec):
                world = self.stubs.world()
                # A second tractor provides the candidate name "rearPto" for wagons.
                self.stubs.vehicle(world, self.lua.eval(
                    '{ id = 39, file = "STUB_TRACTORS", joints = { "TRAILER" }, ptos = { [1] = { "rearPto" } } }'))
                self.stubs.vehicle(world, self.lua.eval(spec))
                records = {r["id"]: r for r in self.snapshot(world)["vehicles"]}
                record = records[31] if 31 in records else records[32]
                self.assertEqual(get(record), expected)

    def test_without_autodrive_there_are_no_destinations(self):
        world = self.stubs.standard(self.lua.table(noAutoDrive=True))
        frame = self.snapshot(world)
        self.assertEqual(frame["destinations"], [])
        self.assertFalse(any(v["has_autodrive"] for v in frame["vehicles"]))

    def test_unobservable_inputs_send_nothing(self):
        for options, reason in ((dict(farm=0), "NO_FARM"),
                                (dict(brokenDestinations=True), "NO_DESTINATIONS")):
            with self.subTest(reason=reason):
                world = self.stubs.standard(self.lua.table(**options))
                self.assertEqual(tuple(self.observe.snapshot(world.env, 1)), (None, reason))
        world = self.stubs.standard()
        world.env.g_currentMission.vehicleSystem = None
        self.assertEqual(tuple(self.observe.snapshot(world.env, 1)), (None, "NO_VEHICLES"))

    def test_truncated_snapshot_is_not_authoritative(self):
        self.observe.MAX_VEHICLES = 2
        frame = self.snapshot(self.stubs.standard())
        self.assertTrue(frame["truncated"])
        with self.assertRaises(ObservationError):
            observation_from_snapshot(frame, CATEGORIES)

    def test_encoder_round_trips_awkward_values(self):
        value = self.lua.eval('''{ s = 'q"\\\\ \\n\\t\\1 ü', n = -0.5, i = 3, big = 2^40,
                                   t = true, f = false, a = (...).array({ 1, 2 }), e = (...).array() }''',
                              self.observe)
        decoded = json.loads(self.observe.encode(value))
        self.assertEqual(decoded, {"s": 'q"\\ \n\t\x01 ü', "n": -0.5, "i": 3, "big": 2 ** 40,
                                   "t": True, "f": False, "a": [1, 2], "e": []})
        self.assertIsNone(json.loads(self.observe.encode(self.lua.eval("{ x = 0/0 }")))["x"])

    def test_lua_frame_cannot_start_physical_work(self):
        observed = observation_from_snapshot(self.snapshot(self.stubs.standard()), CATEGORIES)

        class FrameWorld:
            calls = []

            def snapshot(self):
                return observed.equipment

            def player_position(self, farm_id):
                return observed.player

            def destinations(self):
                return observed.destinations

            def __getattr__(self, name):
                FrameWorld.calls.append(name)
                raise AssertionError(name)

        intent = {"version": 1, "request_id": "r-1", "task": "deliver_tractor_and_forage_wagon",
                  "farm_id": observed.farm_id, "target": "player_at_acceptance"}
        result = Executor(FrameWorld()).execute(intent)
        self.assertEqual((result.status, result.reason), ("BLOCKED", "NO_ELIGIBLE_PAIR"))
        self.assertEqual(FrameWorld.calls, [])


class Lua51ContractTests(LuaContractTests):
    runtime = lua51
