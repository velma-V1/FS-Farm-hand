"""Orchestration tests for the development-only console probe (mod/FarmhandProbe.lua).

The stub "game" teleports for GOTO and slides the tractor straight back for the
reverse; it implements only the evidenced calls the three Lua files make. It
proves the probe's sequencing, gating and result reporting, not FS25 behaviour.
"""
import math
from pathlib import Path
import random
import unittest
import xml.etree.ElementTree as ET

from lupa import lua51, lua54

from adapters.fs25 import equipment_from_vehicle
from companion import executor, hitch

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("FarmhandObserve.lua", "FarmhandHitch.lua", "FarmhandBridge.lua", "FarmhandProbe.lua")

WORLD = r"""
local Probe, Observe, Hitch = ...
local W = {}

local function pose(node)
    if node.parent == nil then return node.x, node.y or 0, node.z, node.yaw or 0 end
    local px, py, pz, yaw = pose(node.parent)
    local s, c = math.sin(yaw), math.cos(yaw)
    return px + node.lx * c + node.lz * s, py + (node.ly or 0), pz - node.lx * s + node.lz * c, yaw
end

function W.new(opts)
    local w = { events = {}, frame = 0, aiActive = false, attacher = nil, implement = nil }
    local env = {}
    w.env = env
    local tractorRoot = { x = opts.tx or 0, z = opts.tz or 20, yaw = 0 }
    local wagonRoot = { x = 0, z = -4, yaw = 0 }
    local tJoint = { jointType = 3, jointIndex = 0, jointTransform = { parent = tractorRoot, lx = 0, lz = -2.5 } }
    local wJoint = { jointType = 3, node = { parent = wagonRoot, lx = 0, lz = 4, ly = opts.wagonDy or 0 } }
    local tractor = { id = 11, ownerFarmId = 1, rootNode = tractorRoot, configFileName = "t.xml",
                      spec_attacherJoints = {},
                      spec_aiDrivable = { attacherJointOffsets = { { x = 0, y = 0, z = -2.5 } } } }
    local wagon = { id = 12, ownerFarmId = opts.wagonFarm or 1, rootNode = wagonRoot, configFileName = "w.xml",
                    spec_forageWagon = {}, spec_attachable = {} }
    local byId = { [11] = tractor, [12] = wagon }
    w.tractor, w.wagon, w.tJoint, w.wJoint, w.tractorRoot, w.wagonRoot = tractor, wagon, tJoint, wJoint,
        tractorRoot, wagonRoot
    local function ev(e) table.insert(w.events, e) end

    function tractor:getFullName() return "Stub Tractor" end
    function tractor:getLastSpeed() return w.tractorSpeed or 0 end
    function tractor:getIsEntered() return false end
    function tractor:getIsAIActive() return w.aiActive end
    function tractor:getAttacherJoints() return { tJoint } end
    function tractor:getAttacherJointByJointDescIndex(i) return ({ tJoint })[i] end
    function tractor:getAIRootNode() return tractorRoot end
    function tractor:getAttachedImplements()
        if w.implement and not w.implement.attachingIsInProgress and not w.hideImplements then
            return { { object = wagon } }
        end
        return {}
    end
    function tractor:getOutputPowerTakeOffsByJointDescIndex(i) return { { ptoName = "rearPto" } } end
    function tractor:getMotor()
        return { getPeakTorque = function() return 1.0 end, getPtoMotorRpmRatio = function() return 2.0 end }
    end
    function tractor:getIsMotorStarted() return true end
    function tractor:getCanMotorRun() return true end
    function tractor:raiseActive() end
    function tractor:getAIReverserNode() return nil end
    function tractor:getAISteeringNode() return tractorRoot end
    function tractor:setCruiseControlState(state) ev("cruise:" .. state) end
    function tractor:getActiveFarm() return 1 end
    function tractor:stopCurrentAIJob() ev("cancelGoto"); w.aiActive = false end
    function tractor:attachImplementFromInfo(info)
        ev("attach")
        w.implement = { object = info.attachable, attachingIsInProgress = true,
                        jointDescIndex = w.wrongJoint or info.attacherVehicleJointDescIndex,
                        inputJointDescIndex = info.attachableJointDescIndex, frames = 0 }
        return true
    end
    function tractor:getImplementByObject(object)
        if w.implement and w.implement.object == object then return w.implement end
        return nil
    end

    function wagon:getFullName() return "Stub Forage Wagon" end
    function wagon:getLastSpeed() return 0 end
    function wagon:getInputAttacherJoints() return { wJoint } end
    function wagon:getIsInputAttacherActive(joint) return joint == wJoint end
    function wagon:getAttacherVehicle() return w.attacher end
    function wagon:getConsumedPtoTorque(a, b) return 1.0 end
    function wagon:getInputPowerTakeOffsByJointDescIndexAndName(i, name)
        if name == "rearPto" then return { { ptoName = name } } end
        return {}
    end
    function wagon:isAttachAllowed(farm, attacher) return farm == 1 and attacher == tractor end

    env.g_server = {}
    env.print = print
    env.NetworkUtil = { getObjectId = function(v) return v.id end, getObject = function(id) return byId[id] end }
    env.g_storeManager = { getItemByXMLFilename = function(self, f)
        return { categoryName = f == "t.xml" and "STUB_TRACTORS" or "FORAGEWAGONS" } end }
    env.getWorldTranslation = function(node) return pose(node) end
    env.localDirectionToWorld = function(node, x, y, z)
        local _, _, _, yaw = pose(node)
        return math.sin(yaw), 0, math.cos(yaw)
    end
    env.worldToLocal = function(node, x, y, z)
        local nx, ny, nz, yaw = pose(node)
        local dx, dz = x - nx, z - nz
        local s, c = math.sin(yaw), math.cos(yaw)
        return dx * c - dz * s, y - ny, dx * s + dz * c
    end
    env.MathUtil = { getYRotationFromDirection = function(dx, dz) return math.atan2 and math.atan2(dx, dz) or math.atan(dx, dz) end }
    env.Drivable = { CRUISECONTROL_STATE_OFF = 0 }
    env.AIJobType = { GOTO = 1 }
    env.AIMessageSuccessStoppedByUser = { new = function() return {} end }
    -- Stub candidate rule (NOT GIANTS'): horizontal distance within maxDistanceSq.
    env.AttacherJoints = { JOINTTYPE_TRAILER = 3, MAX_ATTACH_DISTANCE_SQ = 0.49, MAX_ATTACH_ANGLE = 0.34 }
    function env.AttacherJoints.updateVehiclesInAttachRange(v, maxDistanceSq, maxAngle, full)
        local ax, _, az = pose(tJoint.jointTransform)
        local bx, _, bz = pose(wJoint.node)
        local info = nil
        if (ax - bx) ^ 2 + (az - bz) ^ 2 < maxDistanceSq and tJoint.jointIndex == 0 then
            info = { attacherVehicle = tractor, attachable = wagon,
                     attacherVehicleJointDescIndex = 1, attachableJointDescIndex = 1 }
        end
        v.spec_attacherJoints.attachableInfo = info
    end
    env.AIVehicleUtil = { driveToPoint = function(v, dt, acc, allowed, forwards, lx, lz, maxSpeed)
        ev(allowed and "drive" or "brake")
        if allowed and acc > 0 and not w.stuck then
            local step = maxSpeed / 3.6 * dt / 1000
            tractorRoot.x = tractorRoot.x - math.sin(tractorRoot.yaw) * step
            tractorRoot.z = tractorRoot.z - math.cos(tractorRoot.yaw) * step
        end
    end }
    local job = { positionAngleParameter = {} }
    function job:applyCurrentState() end
    function job.positionAngleParameter:setPosition(x, z) job.x, job.z = x, z end
    function job.positionAngleParameter:setAngle(a) job.yaw = a end
    function job:setValues() end
    function job:validate() return w.gotoInvalid ~= true end
    env.g_currentMission = {
        getFarmId = function() return 1 end,
        vehicleSystem = { vehicles = { tractor, wagon } },
        aiJobTypeManager = { createJob = function() return job end },
        aiSystem = { startJob = function()
            ev("startGoto")
            if not w.gotoNeverStarts then w.aiActive = true; w.job = job end
        end },
    }
    Probe.env = env
    return w
end

-- One game frame: GIANTS' GOTO "arrives" (offset by opts) after two frames, the
-- smooth attach finishes after three; then the probe updates.
function W.tick(w, dt)
    w.frame = w.frame + 1
    if w.job and w.aiActive then
        w.gotoFrames = (w.gotoFrames or 0) + 1
        if w.gotoFrames >= 2 and not w.gotoNeverEnds then
            w.tractorRoot.x = w.job.x + (w.stageOffset or 0)
            w.tractorRoot.z, w.tractorRoot.yaw = w.job.z, w.job.yaw
            w.aiActive = false
            w.job = nil
        end
    end
    if w.implement and w.implement.attachingIsInProgress then
        w.implement.frames = w.implement.frames + 1
        if w.implement.frames >= 3 then
            w.implement.attachingIsInProgress = false
            w.tJoint.jointIndex = 1
            w.attacher = w.attacherOverride or w.tractor
        end
    end
    Probe:update(dt)
end

return W
"""


class LuaProbeTests(unittest.TestCase):
    runtime = lua54

    def setUp(self):
        self.lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        self.output = []
        self.lua.globals().print = lambda *args: self.output.append(" ".join(str(a) for a in args))
        load = lambda name: self.lua.execute((ROOT / "mod" / name).read_text())
        self.observe, self.hitch, self.bridge, self.probe = (load(n) for n in MODULES)
        self.W = self.lua.execute(WORLD, self.probe, self.observe, self.hitch)

    def world(self, **opts):
        return self.W.new(self.lua.table(**opts))

    def run_probe(self, w, *args, frames=400, between=None, dt=16):
        self.output.clear()
        self.probe.consoleProbe(self.probe, *[str(a) for a in args])
        for i in range(frames):
            if self.probe.task is None:
                break
            if between:
                between(i)
            self.W.tick(w, dt)
        return self.results()

    def results(self):
        return [line.split("RESULT ", 1)[1] for line in self.output if "RESULT " in line]

    def events(self, w):
        return list(w.events.values())

    # ----------------------------------------------------------- loading
    def test_modules_are_mod_globals_and_nothing_auto_runs(self):
        same = self.lua.eval("function(a, b, c, d) return FarmhandObserve == a and FarmhandHitch == b "
                             "and FarmhandBridge == c and FarmhandProbe == d and d.Observe == a "
                             "and d.Hitch == b and d.Bridge == c and c.Hitch == b and c.Observe == a end")
        self.assertTrue(same(self.observe, self.hitch, self.bridge, self.probe))
        # Loading with the game's listener hook only registers the listener.
        lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        lua.execute("listeners, commands = {}, {}\n"
                    "function addModEventListener(l) listeners[#listeners + 1] = l end\n"
                    "function addConsoleCommand(name) commands[#commands + 1] = name end\n"
                    "function removeConsoleCommand(name) end\n"
                    "function getfenv() return {} end")
        for name in MODULES:
            lua.execute((ROOT / "mod" / name).read_text())
        g = lua.globals()
        self.assertEqual(len(g.listeners), 2)                # bridge, probe
        self.assertEqual(len(g.commands), 0)
        bridge, probe = g.listeners[1], g.listeners[2]
        for listener in (bridge, probe):                     # nothing loaded yet: does nothing
            listener.update(listener, 16)
        self.assertIsNone(probe.task)
        bridge.loadMap(bridge)                               # client (no g_server): stays inert
        self.assertIsNone(bridge.env)
        probe.loadMap(probe)
        self.assertEqual(list(g.commands.values()), ["fhHitchList", "fhHitchProbe", "fhHitchStop"])

    def test_moddesc_loads_the_dependencies_first(self):
        desc = ET.parse(ROOT / "mod/modDesc.xml").getroot()
        files = [f.get("filename") for f in desc.iter("sourceFile")]
        self.assertEqual(tuple(files), MODULES)
        self.assertTrue(all((ROOT / "mod" / f).is_file() for f in files))
        self.assertEqual(desc.find("multiplayer").get("supported"), "false")

    def test_mirrored_constants_match_the_companion(self):
        self.assertEqual(self.probe.STAGING_DISTANCE_M, hitch.STAGING_DISTANCE_M)
        self.assertEqual(self.probe.STAGE_POSITION_TOLERANCE_M, executor.STAGE_POSITION_TOLERANCE_M)
        self.assertAlmostEqual(math.radians(self.probe.STAGE_YAW_TOLERANCE_DEG), executor.STAGE_YAW_TOLERANCE_RAD)
        self.assertEqual(self.probe.PLAN_TOLERANCE_M, executor.WAGON_MOVED_TOLERANCE_M)
        self.assertEqual(self.hitch.LIMITS.maxAssistRadiusM, executor.ALIGN_PROGRESS_TOLERANCE_M)

    # ------------------------------------------------------------ list
    def test_list_is_read_only_and_names_what_selection_needs(self):
        w = self.world()
        self.probe.consoleList(self.probe)
        text = "\n".join(self.output)
        for fragment in ("MAX_ATTACH_DISTANCE_SQ=0.49", "id=11", 'name="Stub Tractor"', "store=STUB_TRACTORS",
                         "id=12", "forageWagon=true", "motor(peakTorque/ptoRpmRatio)=1.0000/2.0000",
                         "ptoTorque=1", "out=[1:TRAILER:true:rearPto]", "in=[1:TRAILER:true:rearPto]"):
            self.assertIn(fragment, text)
        self.assertEqual(self.events(w), [])
        self.assertIsNone(self.probe.task)

    # ---------------------------------------------------------- sequence
    def test_full_attempt_passes_only_on_the_exact_pair_and_joints(self):
        w = self.world()
        self.assertEqual(self.run_probe(w, 11, 12), ["PASS EXACT_PAIR_ATTACHED"])
        events = self.events(w)
        self.assertEqual(events[0], "startGoto")
        self.assertEqual(events.count("attach"), 1)
        # The reverse ended braked with cruise control off before attaching.
        cut = events.index("attach")
        self.assertEqual(events[cut - 2:cut], ["brake", "cruise:0"])
        self.assertNotIn("drive", events[cut:])
        self.assertIsNone(self.probe.task)

    def test_assist_radius_and_limits_are_used_and_logged(self):
        w = self.world()
        self.assertEqual(self.run_probe(w, 11, 12, 1.25, 0.4, 4), ["PASS EXACT_PAIR_ATTACHED"])
        text = "\n".join(self.output)
        self.assertIn("attachDistanceSq=1.5625 maxVerticalM=0.40 maxSpeedKmh=4.0", text)
        self.assertEqual(w.env.AttacherJoints.MAX_ATTACH_DISTANCE_SQ, 0.49)

    def test_bad_arguments_never_start_anything(self):
        for args in ((11,), (11, 11), (11, 99), (11, 12, 2.0), (11, 12, "x"), (11, 12, 1.0, 1.5),
                     (11, 12, 1.0, 0.5, 0.5), (11, 12, 1.0, 0.5, 9)):
            with self.subTest(args=args):
                self.output.clear()
                w = self.world()
                self.assertEqual(self.run_probe(w, *args), [])
                self.assertEqual(self.events(w), [])
                self.assertIsNone(self.probe.task)

    def test_one_attempt_at_a_time(self):
        w = self.world()
        self.probe.consoleProbe(self.probe, "11", "12")
        self.probe.consoleProbe(self.probe, "11", "12")
        self.assertTrue(any("BUSY" in line for line in self.output))
        self.assertEqual(self.events(w).count("startGoto"), 1)

    def test_unsafe_selection_fails_before_moving(self):
        for opts, reason in (({"wagonFarm": 2}, "NOT_YOUR_FARM"),):
            with self.subTest(reason=reason):
                w = self.world(**opts)
                self.assertEqual(self.run_probe(w, 11, 12), [f"FAILED {reason}"])
                self.assertEqual(self.events(w), [])
        w = self.world()
        w.aiActive = True
        self.assertEqual(self.run_probe(w, 11, 12), ["FAILED AI_ACTIVE_OR_UNKNOWN"])
        self.assertNotIn("cancelGoto", self.events(w))       # never cancels another job

    def test_stage_failure_never_reaches_align(self):
        cases = (("stageOffset", 1.5, "FAILED STAGE_NOT_OBSERVED"),
                 ("gotoInvalid", True, "FAILED STAGE_GOTO_INVALID"))
        for attr, value, expected in cases:
            with self.subTest(expected=expected):
                w = self.world()
                setattr(w, attr, value)
                self.assertEqual(self.run_probe(w, 11, 12), [expected])
                self.assertFalse({"drive", "brake", "attach"} & set(self.events(w)))

    def test_goto_that_never_started_is_not_cancelled(self):
        w = self.world()
        w.gotoNeverStarts = True
        self.assertEqual(self.run_probe(w, 11, 12, dt=1000, frames=200), ["FAILED GOTO_NOT_STARTED"])
        self.assertNotIn("cancelGoto", self.events(w))
        w = self.world()
        w.gotoNeverEnds = True
        self.assertEqual(self.run_probe(w, 11, 12, dt=1000, frames=200), ["FAILED STAGE_TIMEOUT"])
        self.assertIn("cancelGoto", self.events(w))          # its own job, seen running

    def test_align_failure_never_reaches_attach(self):
        for attr, value, expected in (("stuck", True, "FAILED STALLED"),
                                      ("wagonDy", 0.8, "FAILED VERTICAL_MISMATCH")):
            with self.subTest(expected=expected):
                w = self.world(**({attr: value} if attr == "wagonDy" else {}))
                if attr == "stuck":
                    w.stuck = True
                self.assertEqual(self.run_probe(w, 11, 12, frames=2000), [expected])
                events = self.events(w)
                self.assertNotIn("attach", events)
                self.assertEqual(events[-2:], ["brake", "cruise:0"])

    def test_pass_requires_fresh_exact_verification(self):
        w = self.world()
        w.wrongJoint = 2
        self.assertEqual(self.run_probe(w, 11, 12), ["UNVERIFIED ATTACH_MISMATCH"])
        w = self.world()
        w.attacherOverride = w.wagon                          # wagon does not name the tractor
        self.assertEqual(self.run_probe(w, 11, 12), ["UNVERIFIED ATTACH_ABSENT"])
        # GIANTS' implement entry looks right, but the fresh observation disagrees.
        w = self.world()
        w.hideImplements = True
        self.assertEqual(self.run_probe(w, 11, 12), ["UNVERIFIED ATTACHMENT_MISMATCH"])

    def test_stop_cancels_only_its_own_goto_and_brakes_the_reverse(self):
        w = self.world()
        self.probe.consoleProbe(self.probe, "11", "12")
        self.W.tick(w, 16)                                    # GOTO running, seen active
        w.gotoNeverEnds = True
        self.probe.consoleStop(self.probe)
        self.assertEqual(self.results(), ["FAILED STOPPED_BY_USER"])
        self.assertIn("cancelGoto", self.events(w))
        # During the reverse: braked, cruise off, never resumes.
        self.output.clear()
        w = self.world()
        self.probe.consoleProbe(self.probe, "11", "12")
        while not self.probe.task or self.probe.task.phase != "align":
            self.W.tick(w, 16)
        self.W.tick(w, 16)
        self.probe.consoleStop(self.probe)
        self.assertEqual(self.results(), ["FAILED STOPPED_BY_USER"])
        events = self.events(w)
        self.assertEqual(events[-2:], ["brake", "cruise:0"])
        for _ in range(5):
            self.W.tick(w, 16)
        self.assertEqual(self.events(w), events)

    # -------------------------------------------- plan equals companion
    def test_dev_plan_matches_companion_plan_on_random_inputs(self):
        rng = random.Random(4)
        null = self.observe.NULL
        def lua_value(v):
            if v is None:
                return null
            if isinstance(v, dict):
                return self.lua.table_from({k: lua_value(x) for k, x in v.items()})
            if isinstance(v, list):
                return self.lua.table_from([lua_value(x) for x in v])
            return v

        def names():
            return rng.choice([None, [], ["rearPto"], ["frontPto"], ["rearPto", "frontPto"]])

        compared = 0
        for _ in range(1500):
            tractor = {"id": 1, "farm_id": 1, "category": "T", "x": 0.0, "z": 0.0, "speed_kmh": 0.0,
                       "entered": False, "ai_active": False, "has_autodrive": False, "ad_active": None,
                       "attachment_observed": True, "attacher_vehicle_id": None, "implement_ids": [],
                       "heading": 0.0, "forage_wagon": False, "motor": None, "pto_torque": 0.0,
                       "ai_root": None if rng.random() < 0.1 else {"x": rng.uniform(-50, 50),
                                                                   "z": rng.uniform(-50, 50),
                                                                   "yaw": rng.uniform(-3.2, 3.2)},
                       "output_joints": [{"index": rng.randint(1, 4), "type": rng.choice(["A", "A", "B"]),
                                          "x": rng.uniform(-2, 2), "z": rng.uniform(-4, 1),
                                          "free": rng.random() < 0.9, "pto_names": names()}
                                         for _ in range(rng.randint(0, 3))],
                       "input_joint_poses": []}
            wagon = {**tractor, "id": 2, "category": None, "forage_wagon": True, "ai_root": None,
                     "output_joints": [], "heading": None if rng.random() < 0.1 else rng.uniform(-3.2, 3.2),
                     "pto_torque": rng.choice([None, 0.0, 0.0, 1.2, 1.2]),
                     "input_joint_poses": [{"index": rng.randint(1, 3), "type": rng.choice(["A", "A", "B"]),
                                            "x": rng.uniform(-50, 50), "z": rng.uniform(-50, 50),
                                            "active": rng.random() < 0.9, "pto_names": names()}
                                           for _ in range(rng.randint(0, 3))]}
            expected = hitch.plan_hitch(equipment_from_vehicle(tractor, {"T": "tractor"}),
                                        equipment_from_vehicle(wagon, {}), hitch.STAGING_DISTANCE_M)
            got = self.probe.plan(lua_value(tractor), lua_value(wagon), hitch.STAGING_DISTANCE_M, null)
            if expected is None:
                self.assertIsNone(got, (tractor, wagon))
                continue
            compared += 1
            self.assertEqual((got.tractorJoint, got.wagonJoint, got.jointType),
                             (expected.tractor_joint, expected.wagon_joint, expected.joint_type))
            for name in ("final", "staging"):
                pose = getattr(expected, name)
                lua_pose = got[name]
                for field in ("x", "z", "yaw"):
                    self.assertAlmostEqual(lua_pose[field], getattr(pose, field), places=9)
        self.assertGreater(compared, 80)


class Lua51ProbeTests(LuaProbeTests):
    runtime = lua51
