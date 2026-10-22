"""The game half of the AC-001 bridge (mod/FarmhandBridge.lua), Lua 5.1 and 5.4.

Stubs implement only the evidenced calls: Farmhand Companion's file/XML/pipe
pattern and AutoDrive's external interface. Observe.snapshot is replaced by a
one-field stub because test_lua_contract.py covers it. These tests prove the
session, framing and drive/stop lifecycle, not FS25 or AutoDrive behaviour.
"""
import json
from pathlib import Path
import unittest

from lupa import lua51, lua54

from protocol.ipc import encode_command, encode_session, parse_frame

ROOT = Path(__file__).resolve().parents[1]
DIR = "C:/Users/me/Documents/My Games/FarmingSimulator2025/modSettings/FS25_Farmhand/"

STUBS = r"""
local Bridge, Observe, Hitch = ...
local W = {}

function W.new()
    local w = { files = {}, lines = {}, calls = {}, speed = 0, entered = false, aiActive = false,
                adActive = false, startsAD = true }
    local env = { g_server = {} }
    w.env = env
    local function call(name) table.insert(w.calls, name) end
    local tractor = { ownerFarmId = 1, rootNode = "tractorRoot", ad = { isStoppingWithError = false } }
    w.x, w.z = 0, 0
    env.getWorldTranslation = function(node)
        assert(node == "tractorRoot")
        return w.x, 0, w.z
    end
    tractor.ad.stateModule = {
        setMode = function(_, mode) call("setMode:" .. mode) end,
        isActive = function() return w.adActive end,
        getStartHelper = function() return w.startHelper == true end,   -- StateModule.lua:406-408
    }
    function tractor:getLastSpeed() return w.speed end
    function tractor:getIsEntered() return w.entered end
    function tractor:raiseActive() w.raised = (w.raised or 0) + 1 end
    tractor.rootVehicle = tractor
    function tractor:registerPlayerVehicleControlAllowedFunction(vehicle, func) w.controlFunc = func end
    w.takeOver = function() return w.controlFunc(tractor) end
    function tractor:getIsAIActive() return w.aiActive or w.adActive end   -- AD's override
    function tractor:stopAutoDrive() call("stopAutoDrive"); w.adActive = false end
    function tractor:stopCurrentAIJob() call("stopCurrentAIJob"); w.aiActive = false end
    -- GIANTS GOTO start sequence (as tests/test_lua_hitch.py); startJob runs it
    -- only when w.gotoStarts.
    local job = { positionAngleParameter = { setPosition = function() end, setAngle = function() end } }
    function job:applyCurrentState() end
    function job:setValues() end
    function job:validate() return true end
    env.AIJobType = { GOTO = 1 }
    env.AIMessageSuccessStoppedByUser = { new = function() return {} end }
    w.tractor = tractor
    env.NetworkUtil = { getObject = function(id) if id == 11 then return tractor end return nil end }
    -- GIANTS notification call shape (PlayerInputComponent 1150).
    w.notices = {}
    env.FSBaseMission = { INGAME_NOTIFICATION_CRITICAL = 1, INGAME_NOTIFICATION_OK = 4 }
    -- Player input hook as Courseplay.lua:142-151 uses it; overwrittenFunction
    -- passes the original as the second argument (GIANTS Utils).
    w.registered = {}
    env.InputAction = { FARMHAND_FETCH_HERE = 77, FARMHAND_TALK = 78, FARMHAND_STOP = 79, FARMHAND_PUSH_TO_TALK = 80,
                        FARMHAND_SETTINGS = 81 }
    -- GIANTS option dialog as Courseplay calls it (CourseEditor.lua:165-176).
    env.OptionDialog = { show = function(callback, title, text, texts)
        w.option = { callback = callback, title = title, texts = texts }
    end }
    w.dialogVisible = false
    env.g_gui = { getIsDialogVisible = function() return w.dialogVisible end }
    -- GIANTS text dialog as Courseplay calls it (CustomFieldManager.lua:126-130).
    env.TextInputDialog = { show = function(callback, target, default, prompt, title, maxChars, okText)
        w.dialog = { callback = callback, target = target, default = default, maxChars = maxChars, okText = okText }
    end }
    env.Utils = { overwrittenFunction = function(old, new)
        return function(self, ...) return new(self, old, ...) end
    end }
    env.PlayerInputComponent = { registerGlobalPlayerActionEvents = function() call("vanillaGlobalActions") end }
    env.g_inputBinding = { registerActionEvent = function(_, action, target, callback, up, down, always, active)
        table.insert(w.registered, { action = action, target = target, callback = callback,
                                     flags = tostring(up) .. tostring(down) .. tostring(always) .. tostring(active) })
        return true, #w.registered
    end }
    env.g_currentMission = { addIngameNotification = function(_, kind, text)
                                 table.insert(w.notices, kind .. ":" .. text)
                             end,
        getFarmId = function() return 1 end,
        aiJobTypeManager = { createJob = function() return job end },
        aiSystem = { startJob = function() call("startJob"); if w.gotoStarts then w.aiActive = true end end } }
    env.AutoDrive = { MODE_DRIVETO = 1 }
    function env.AutoDrive:GetAvailableDestinations() return { [3] = { name = "Yard" } } end
    function env.AutoDrive:StartDriving(v, first, second)
        call("StartDriving:" .. first .. ":" .. second)
        if w.startsAD then w.adActive = true end
    end
    env.getUserProfileAppPath = function() return "C:/Users/me/Documents/My Games/FarmingSimulator2025/" end
    env.createFolder = function() end
    env.deleteFile = function(path) w.files[path] = nil end
    -- Engine XML API shape used by Farmhand Companion Transport.lua:166-213.
    env.XMLFile = { loadIfExists = function(_, path)
        local text = w.files[path]
        if text == nil then return nil end
        local attrs = {}
        for k, v in text:gmatch('(%w+)="([^"]*)"') do attrs[k] = v end
        return { getString = function(_, key) return attrs[key:match("#(.+)$")] end, delete = function() end }
    end }
    env.io = { open = function(path, mode)
        if w.pipeDown then return nil end
        return { write = function(_, s) table.insert(w.lines, s) end, flush = function() end,
                 close = function() end }
    end }
    Bridge.Observe = { PROTOCOL = 3, NULL = Observe.NULL, PIPE_PATH = Observe.PIPE_PATH, encode = Observe.encode,
                       validUtf8 = Observe.validUtf8,
                       hello = function() return '{"event":"hello"}' end,
                       snapshot = function(_, seq, session)
                           return Observe.encode({ event = "snapshot", seq = seq, session = session or Observe.NULL })
                       end }
    Bridge.Hitch = Hitch
    Bridge.owner, Bridge.lastAlign = nil, nil
    Bridge:open(env)
    return w
end

function W.run(w, ms, dt)
    dt = dt or 100
    for _ = 1, math.floor(ms / dt) do Bridge:update(dt) end
end

return W
"""


class LuaBridgeTests(unittest.TestCase):
    runtime = lua54

    def setUp(self):
        self.lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        self.printed = []
        self.lua.globals().print = lambda *a: self.printed.append(" ".join(str(x) for x in a))
        load = lambda n: self.lua.execute((ROOT / "mod" / n).read_text())
        self.observe, self.hitch, self.bridge = (load(n) for n in
                                                 ("FarmhandObserve.lua", "FarmhandHitch.lua", "FarmhandBridge.lua"))
        self.W = self.lua.execute(STUBS, self.bridge, self.observe, self.hitch)
        self.w = self.W.new()

    # helpers
    def write(self, name, text):
        self.w.files[DIR + name] = text

    def adopt(self, session="s1", beat=0):
        self.write("session.xml", encode_session(session, beat))
        self.W.run(self.w, 1000)

    def beating(self, ms, session="s1"):
        for _ in range(ms // 1000):
            self.beat = getattr(self, "beat", 0) + 1
            self.write("session.xml", encode_session(session, self.beat))
            self.W.run(self.w, 1000)

    def send(self, command_id, op, args, session="s1"):
        self.write("command.xml", encode_command(session, command_id, op, args))
        self.W.run(self.w, 100)

    def frames(self):
        return [json.loads(line) for line in self.w.lines.values() if line.startswith('{"')]

    def results(self, command_id=None, session="s1"):
        return [(f["id"], f["status"], f["code"]) for f in self.frames()
                if f.get("event") == "result" and f["session"] == session and command_id in (None, f["id"])]

    def calls(self):
        return list(self.w.calls.values())

    # tests
    def test_inert_until_a_companion_session_exists(self):
        self.send(1, "drive", {"tractor": 11, "marker": 3})
        self.W.run(self.w, 3000)
        self.assertEqual(len(self.w.lines), 0)                    # no pipe without a session
        self.assertEqual(self.calls(), [])
        self.assertNotIn(DIR + "command.xml", self.w.files)       # consumed, never executed

    def test_adoption_opens_the_pipe_and_stamps_snapshots(self):
        self.adopt()
        self.W.run(self.w, 1000)
        frames = self.frames()
        self.assertEqual(frames[0], {"event": "hello"})
        snapshots = [f for f in frames if f.get("event") == "snapshot"]
        self.assertGreaterEqual(len(snapshots), 2)
        self.assertTrue(all(f["session"] == "s1" for f in snapshots))

    def test_drive_reports_acceptance_then_the_observed_end(self):
        self.adopt()
        self.send(1, "drive", {"tractor": 11, "marker": 3})
        self.assertEqual(self.calls(), ["setMode:1", "StartDriving:3:3"])   # DRIVETO, both markers
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])
        self.W.run(self.w, 2000)
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])   # still driving
        # Kept awake every frame while AutoDrive drives it (asleep, AutoDrive's own update
        # never ran: live 2026-10-22, forceIsActive true, isActive false, no task).
        self.assertGreaterEqual(self.w.raised, 19)
        self.w.x = 150.0                                                    # it drove
        self.w.adActive = False
        self.w.speed = 3.0
        self.W.run(self.w, 300)
        self.assertEqual(len(self.results(1)), 1)                           # ended but rolling
        self.w.speed = 0.0
        self.W.run(self.w, 200)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "AD_ENDED"))
        for line in self.w.lines.values():                                  # every frame decodes
            if '"result"' in line:
                parse_frame((line + "\n").encode())

    def test_drive_failures(self):
        cases = [("startsAD", False, "AD_NOT_STARTED", 6000),
                 ("error", True, "AD_STOPPED_WITH_ERROR", 500),
                 ("takeover", True, "DRIVER_TOOK_OVER", 500)]
        for attr, value, code, ms in cases:
            with self.subTest(code=code):
                self.w = self.W.new()
                self.adopt()
                if attr == "startsAD":
                    self.w.startsAD = False
                self.send(1, "drive", {"tractor": 11, "marker": 3})
                if attr == "error":
                    self.w.tractor.ad.isStoppingWithError = True
                    self.w.adActive = False
                if attr == "takeover":
                    self.w.entered = True
                    self.assertTrue(self.w.takeOver())          # the player's input is always allowed
                self.W.run(self.w, ms)
                self.assertEqual(self.results(1)[-1], (1, "FAILED", code))
                self.assertEqual("stopAutoDrive" in self.calls(), attr == "takeover")

    def test_a_drive_that_does_not_move_fails_with_what_autodrive_waits_on(self):
        # Live 831d6868b9df40b0-1: AutoDrive active 6 minutes, the loaded rig never moved, and
        # nothing said why. Its own state names it: planning onto its network, a sensor
        # triggered (what is in its box, and whether AutoDrive counts it as the rig's own), or
        # paused. It is stopped and the drive FAILED with that.
        lua_state = self.lua.eval("""function(w, task, sensorHit)
            local t = w.tractor
            t.ad.stateModule.getCurrentTaskInfo = function() return task end
            function t:getClosestWayPoint() return 560, 41.5 end
            function t:getRootVehicle() return t end
            t.ad.drivePathModule = { isPaused = false, getWayPoints = function() return { {}, {} }, 1 end }
            t.ad.collisionDetectionModule = { detectedObstable = sensorHit, detectedCollision = false }
            local sensor = { triggered = sensorHit, getMask = function() return 7 end,
                             getBoxShape = function() return { x = 1, y = 2, z = 3, rx = 0, ry = 0, size = { 1, 1, 1 } } end }
            t.ad.sensors = { frontSensor = sensor }
            local cargo = { getName = function() return "Series 9" end }
            w.env.getName = function(id) return "series9_main_component1" end
            w.env.getParent = function(id) return 0 end
            w.env.g_currentMission.terrainRootNode = 1
            w.env.g_currentMission.getNodeObject = function(_, id) if id == 615 then return cargo end end
            w.env.overlapBox = function(x, y, z, rx, ry, rz, ex, ey, ez, cb, target)
                target[cb](target, 615); return 1
            end
            function w.env.AutoDrive:checkIsConnected(a, b) return false end
            w.env.AutoDrive.getAllImplementsFolded = function() return true end
        end""")
        self.bridge.AD_STALL_MS = 20000
        cases = (("$l10n_AD_task_pathfinding; 3 / 9 - 400 / 2000", False, "AD_PATHFINDING_STALLED"),
                 ("$l10n_AD_task_drive_to_destination;", True, "AD_WAITING_ON_OBSTACLE"))
        for task, hit, code in cases:
            with self.subTest(code=code):
                self.w = self.W.new()
                self.printed.clear()
                self.adopt()
                lua_state(self.w, task, hit)
                self.send(1, "drive", {"tractor": 11, "marker": 3})
                self.beating(15000)
                self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])     # waiting, not yet given up
                self.beating(15000)
                self.assertEqual(self.results(1)[-1], (1, "FAILED", code))
                self.assertIn("stopAutoDrive", self.calls())
                logged = [line for line in self.printed if "drive stalled" in line]
                self.assertTrue(logged)
                if hit:
                    self.assertIn("frontSensor[series9_main_component1@Series 9]", logged[0])

    def test_a_drive_ended_where_it_started_or_with_no_route_is_said_so(self):
        # Live 2026-10-22: the waypoint nearest the rig was on a dead end; AutoDrive's path
        # finder gave up at once ("Could not calculate path") without its error flag.
        self.adopt()
        self.send(1, "drive", {"tractor": 11, "marker": 3})
        self.W.run(self.w, 500)
        self.w.adActive = False
        self.W.run(self.w, 300)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "AD_ENDED_UNMOVED"))
        # Asked first of AutoDrive's own graph (pathFromTo from its closest waypoint): no route,
        # nothing started.
        self.lua.execute("""local w = ...
            local graph = { getMapMarkerById = function(_, id) return { id = 560 } end,
                            pathFromTo = function(_, a, b) return {} end,
                            createMapMarker = function() end, getMapMarkerByName = function() end,
                            getWayPoints = function() return {} end }
            local env = setmetatable({ ADGraphManager = graph }, { __index = _G })
            w.env.getfenv = function() return env end
            function w.tractor:getClosestWayPoint() return 11802, 30.7 end
        """, self.w)
        self.send(2, "drive", {"tractor": 11, "marker": 3})
        self.assertEqual(self.results(2)[-1], (2, "FAILED", "AD_NO_ROUTE"))
        self.assertEqual(self.calls().count("StartDriving:3:3"), 1)                 # only the first one started
        self.assertTrue(any("AD_NO_ROUTE" in p and "11802" in p for p in self.printed))

    def test_autodrives_dead_end_lane_is_joined_only_to_a_routed_road_continuing_its_way(self):
        # Live 2026-10-22: the store yard's lane 11800-11808 stops 5.5 m short of the road it
        # joins (5767). AutoDrive's own setConnectionBetween(end, road, 1) after the companion's
        # backup; DONE once AutoDrive's own pathFromTo has the route. A road going back the other
        # way, or none within 6 m, is never joined.
        setup = self.lua.eval("""function(w, roadX, roadDir)
            local wps = {
                [1] = { id = 1, x = 0, z = 0, out = { 2 }, incoming = {} },
                [2] = { id = 2, x = 0, z = 40, out = {}, incoming = { 1 } },              -- the dead end
                [10] = { id = 10, x = roadX, z = 44, out = { 12 }, incoming = {} },        -- the road
                [12] = { id = 12, x = roadX, z = 44 + 40 * roadDir, out = {}, incoming = { 10 } },
            }
            local graph = { joined = nil }
            function graph:getWayPoints() return wps end
            function graph:getWayPointById(id) return wps[id] end
            function graph:getMapMarkerById(id) return { id = 12, name = "Farm Barn" } end
            function graph:getMapMarkerByName() return nil end
            function graph:createMapMarker() end
            function graph:pathFromTo(a, b)
                local seen, queue = { [a] = true }, { a }
                while #queue > 0 do
                    local n = table.remove(queue, 1)
                    if n == b then return { wps[a], wps[b] } end
                    for _, o in ipairs(wps[n].out) do if not seen[o] then seen[o] = true; queue[#queue + 1] = o end end
                end
                return {}
            end
            function graph:setConnectionBetween(a, b, direction)
                self.joined = { a.id, b.id, direction }
                table.insert(a.out, b.id)
            end
            w.graph = graph
            local env = setmetatable({ ADGraphManager = graph }, { __index = _G })
            w.env.getfenv = function() return env end
            function w.tractor:getClosestWayPoint() return 1, 3.0 end
        end""")
        for road_x, road_dir, expected in ((0.0, 1, (2, "DONE", "AD_LINKED")), (0.0, -1, (2, "FAILED", "AD_NO_LINK_NEAR")),
                                           (20.0, 1, (2, "FAILED", "AD_NO_LINK_NEAR"))):
            with self.subTest(road_x=road_x, road_dir=road_dir):
                self.w = self.W.new()
                self.adopt()
                setup(self.w, road_x, road_dir)
                self.send(2, "ad_connect", {"tractor": 11, "marker": 3})
                self.W.run(self.w, 300)
                self.assertEqual(self.results(2)[-1], expected)
                joined = self.w.graph.joined
                self.assertEqual(tuple(joined.values()) if joined else None, (2, 10, 1) if expected[1] == "DONE" else None)

    def test_drive_refuses_when_autodrive_would_hand_off_to_another_ai(self):
        self.adopt()
        self.w.startHelper = True
        self.send(1, "drive", {"tractor": 11, "marker": 3})
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "AD_START_HELPER_ON"))
        self.assertEqual(self.calls(), [])                                  # setting untouched, no start

    def test_unknown_marker_and_vehicle_never_move(self):
        self.adopt()
        self.send(1, "drive", {"tractor": 11, "marker": 9})
        self.send(2, "drive", {"tractor": 99, "marker": 3})
        self.assertEqual(self.results(), [(1, "ACCEPTED", "ACCEPTED"), (1, "FAILED", "UNKNOWN_MARKER"),
                                          (2, "REJECTED", "UNKNOWN_VEHICLE")])
        self.assertEqual(self.calls(), [])

    def test_stale_sessions_and_replayed_ids_are_never_executed(self):
        self.adopt()
        self.send(1, "drive", {"tractor": 11, "marker": 3}, session="old-session")
        self.assertEqual(self.results(session="old-session"), [(1, "REJECTED", "STALE_SESSION")])
        self.send(5, "stop", {"tractor": 11})
        self.W.run(self.w, 200)
        self.send(5, "drive", {"tractor": 11, "marker": 3})                  # replayed id
        self.send(4, "drive", {"tractor": 11, "marker": 3})                  # lower id
        self.assertEqual(self.calls(), [])
        self.assertEqual([r for r in self.results() if r[0] in (4, 5)],
                         [(5, "ACCEPTED", "ACCEPTED"), (5, "DONE", "STOPPED")])

    def test_one_command_at_a_time_and_stop_pre_empts(self):
        self.adopt()
        self.send(1, "drive", {"tractor": 11, "marker": 3})
        self.send(2, "drive", {"tractor": 11, "marker": 3})
        self.assertIn((2, "REJECTED", "BUSY"), self.results())
        self.w.speed = 4.0
        self.send(3, "stop", {"tractor": 11})
        self.assertIn((1, "FAILED", "STOPPED"), self.results())
        self.assertIn("stopAutoDrive", self.calls())
        self.assertEqual(self.results(3), [(3, "ACCEPTED", "ACCEPTED")])   # not yet at rest
        self.w.speed = 0.0
        self.W.run(self.w, 200)
        self.assertEqual(self.results(3)[-1], (3, "DONE", "STOPPED"))

    def test_a_stop_during_a_stop_joins_it(self):
        self.adopt()
        self.w.gotoStarts = True
        self.send(1, "navigate", self.POSE)
        self.w.speed = 4.0                                   # still rolling
        self.send(2, "stop", {"tractor": 11})
        self.send(3, "stop", {"tractor": 11})               # a second stop while the first runs
        self.assertNotIn((2, "FAILED", "STOPPED"), self.results())
        self.w.speed = 0.0
        self.W.run(self.w, 200)
        self.assertEqual(self.results(2)[-1], (2, "DONE", "STOPPED"))
        self.assertEqual(self.results(3)[-1], (3, "DONE", "STOPPED"))

    def test_stop_that_cannot_be_observed_fails(self):
        self.adopt()
        self.w.speed = 5.0
        self.send(1, "stop", {"tractor": 11})
        self.beating(16000)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "NOT_STOPPED"))

    def test_lost_companion_stops_its_motion_and_session(self):
        self.adopt()
        self.send(1, "drive", {"tractor": 11, "marker": 3})
        self.W.run(self.w, 11000)                                          # no new beat
        self.assertIn((1, "FAILED", "COMPANION_LOST"), self.results())
        self.assertIn("stopAutoDrive", self.calls())
        self.w.lines = self.lua.table()
        self.W.run(self.w, 2000)                                           # same file: not re-adopted
        snapshots = [f for f in self.frames() if f.get("event") == "snapshot"]
        self.assertTrue(snapshots and all(f["session"] is None for f in snapshots))

    def test_beating_companion_keeps_its_session(self):
        self.adopt()
        self.send(1, "drive", {"tractor": 11, "marker": 3})
        self.beating(12000)
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])
        self.assertNotIn("stopAutoDrive", self.calls())

    def test_new_session_stops_the_old_ones_work(self):
        self.adopt("s1")
        self.send(1, "drive", {"tractor": 11, "marker": 3})
        self.adopt("s2")
        self.assertIn((1, "FAILED", "SESSION_CHANGED"), self.results())
        self.assertIn("stopAutoDrive", self.calls())
        self.send(1, "stop", {"tractor": 11}, session="s2")              # ids restart per session
        self.assertEqual(self.results(1, session="s2"), [(1, "ACCEPTED", "ACCEPTED"), (1, "DONE", "STOPPED")])

    POSE = {"tractor": 11, "x": 1.0, "z": 2.0, "yaw": 0.0}

    def test_navigate_reports_acceptance_then_the_observed_end(self):
        self.adopt()
        self.w.gotoStarts = True
        self.send(1, "navigate", self.POSE)
        self.assertEqual(self.calls(), ["startJob"])                       # GIANTS GOTO, not AutoDrive
        self.beating(60000)
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])   # longer than any stage
        self.w.aiActive = False
        self.w.speed = 2.0
        self.W.run(self.w, 300)
        self.assertEqual(len(self.results(1)), 1)                           # ended but rolling
        self.w.speed = 0.0
        self.W.run(self.w, 200)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "GOTO_ENDED"))
        self.assertNotIn("stopCurrentAIJob", self.calls())
        for line in self.w.lines.values():
            if '"result"' in line:
                parse_frame((line + "\n").encode())

    def test_an_ended_goto_logs_why_giants_ended_it_and_is_still_only_ended(self):
        # 5304adc live: STAGE_ENDED 14 m off. GIANTS leaves the agent's last state
        # (AIDrivable:onUpdate 317-325) for the log; the result stays "ended".
        output = []
        self.lua.globals().print = lambda *a: output.append(" ".join(map(str, a)))
        self.adopt()
        self.w.gotoStarts = True
        self.send(1, "stage", self.POSE)
        self.W.run(self.w, 1000)
        self.w.tractor.spec_aiDrivable = self.lua.table(lastState=5, distanceToTarget=14.0)
        self.lua.execute("local w = ...; w.env.AIDrivable = { STATES = { [5] = 'NOT_REACHABLE' } }", self.w)
        self.w.aiActive = False
        self.W.run(self.w, 300)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "STAGE_ENDED"))
        self.assertTrue(any("stage ended: agent state NOT_REACHABLE, 14.0 m from its target" in line
                            for line in output), output)
        # Unreadable diagnostics never change the outcome.
        self.w.tractor.spec_aiDrivable = None
        self.send(2, "navigate", self.POSE)
        self.W.run(self.w, 1000)
        self.w.aiActive = False
        self.W.run(self.w, 300)
        self.assertEqual(self.results(2)[-1], (2, "DONE", "GOTO_ENDED"))

    def test_navigate_without_progress_is_stopped(self):
        # d7889a7a8879421b-5 live: an active GOTO left the tractor still for 90 s. The
        # stall logs the agent's state (BLOCKED waits forever, AIDrivable:onUpdate).
        output = []
        self.lua.globals().print = lambda *a: output.append(" ".join(map(str, a)))
        self.adopt()
        self.w.gotoStarts = True
        self.w.tractor.spec_aiDrivable = self.lua.table(lastState=3, distanceToTarget=171.5)
        self.lua.execute("local w = ...; w.env.AIDrivable = { STATES = { [3] = 'BLOCKED' } }", self.w)
        self.send(1, "navigate", self.POSE)
        for _ in range(8):                                                 # 3 m every 30 s: progress
            self.beating(30000)
            self.w.x += 3.0
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])
        self.w.x += 1.0                                                    # then less than 2 m in 90 s
        self.beating(92000)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "NAVIGATE_STALLED"))
        self.assertIn("stopCurrentAIJob", self.calls())
        self.assertTrue(any("navigate stalled: agent state BLOCKED, 171.5 m from its target" in line
                            for line in output), output)

    def test_a_navigate_unmoved_while_giants_drives_is_stuck(self):
        # Live 2026-10-20: a closed gate GIANTS' map treats as open road.
        self.adopt()
        self.w.gotoStarts = True
        self.w.tractor.spec_aiDrivable = self.lua.table(lastState=2, distanceToTarget=713.0)
        self.lua.execute("local w = ...; w.env.AIDrivable = { STATES = { [2] = 'driving' } }", self.w)   # live casing
        self.send(1, "navigate", self.POSE)
        self.beating(92000)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "NAVIGATE_STUCK"))

    def test_block_marks_ground_impassable_the_way_giants_placeables_do(self):
        self.adopt()
        calls = self.lua.execute("""
            local w = ...
            local env, calls = w.env, {}
            env.getTerrainHeightAtWorldPos = function() return 12 end
            env.createTransformGroup = function(name) return { name = name } end
            env.getRootNode = function() return "root" end
            env.link = function(parent, node) table.insert(calls, "link:" .. parent) end
            env.setTranslation = function(node, x, y, z) table.insert(calls, string.format("at:%g,%g,%g", x, y, z)) end
            env.g_currentMission.terrainRootNode = {}
            env.g_currentMission.aiSystem.addObstacle = function(_, node, ox, oy, oz, sx, sy, sz, brake, passable)
                table.insert(calls, string.format("obstacle:%g,%g,%s", sx, sz, tostring(passable)))
            end
            env.g_currentMission.aiSystem.setAreaDirty = function() table.insert(calls, "dirty") end
            return calls
        """, self.w)
        self.send(1, "block", {"x": 670.0, "z": -260.0, "area": 8.0})
        self.assertEqual(self.results(1)[-1], (1, "DONE", "BLOCKED_AREA"))
        self.assertEqual(list(calls.values()), ["link:root", "at:670,12,-260", "obstacle:8,8,false", "dirty"])
        self.send(2, "block", {"x": 670.0, "z": -260.0, "area": 25.0})
        self.assertEqual(self.results(2)[-1], (2, "FAILED", "INVALID_AREA"))

    def test_navigate_failures(self):
        cases = [("never_started", "FAILED", "GOTO_NOT_STARTED", False),
                 ("takeover", "FAILED", "DRIVER_TOOK_OVER", True),
                 ("seat_unreadable", "UNVERIFIED", "UNKNOWN_OBSERVATION", True),
                 ("position_unreadable", "UNVERIFIED", "UNKNOWN_OBSERVATION", True)]
        for fault, status, code, cancelled in cases:
            with self.subTest(fault=fault):
                self.w = self.W.new()
                self.adopt()
                self.w.gotoStarts = fault != "never_started"
                self.send(1, "navigate", self.POSE)
                if fault == "takeover":
                    self.w.entered = True
                    self.w.takeOver()
                if fault == "seat_unreadable":
                    self.w.entered = None
                if fault == "position_unreadable":
                    self.w.x = float("nan")
                self.W.run(self.w, 6000)
                self.assertEqual(self.results(1)[-1], (1, status, code))
                self.assertEqual("stopCurrentAIJob" in self.calls(), cancelled)

    def test_the_requester_may_ride_along(self):
        # Seated in the tractor (single player: the requester), nothing conflicting:
        # every motion op runs; only taking the controls ends it.
        for op, args in (("navigate", self.POSE), ("stage", self.POSE), ("drive", {"tractor": 11, "marker": 3})):
            with self.subTest(op=op):
                self.w = self.W.new()
                self.adopt()
                self.w.entered = True
                self.w.gotoStarts = True
                self.send(1, op, args)
                self.beating(3000)
                self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])     # running, not OCCUPIED
                self.w.x = 150.0                                                  # it drove somewhere
                self.w.aiActive, self.w.adActive = False, False
                self.W.run(self.w, 500)
                self.assertEqual(self.results(1)[-1][1], "DONE")
                self.send(2, "stop", {"tractor": 11})                         # STOP while riding
                self.W.run(self.w, 200)
                self.assertEqual(self.results(2)[-1], (2, "DONE", "STOPPED"))

    def test_navigate_refuses_a_busy_or_moving_tractor(self):
        for attr, code in (("aiActive", "AI_ACTIVE"), ("speed", "NOT_STOPPED")):
            with self.subTest(code=code):
                self.w = self.W.new()
                self.adopt()
                setattr(self.w, attr, True if attr == "aiActive" else 4.0)
                self.send(1, "navigate", self.POSE)
                self.assertEqual(self.results(1)[-1], (1, "FAILED", code))
                self.assertEqual(self.calls(), [])

    def test_stop_cancels_only_a_goto_it_started(self):
        for op in ("stage", "navigate"):
            # (job runs at once, AI seen when stopping after ms, cancelled)
            for starts, after, cancelled in ((True, 100, True),      # seen running: ours
                                             (False, 6000, False)):  # start window over: someone else's
                with self.subTest(op=op, goto_started=starts):
                    self.w = self.W.new()
                    self.adopt()
                    self.w.gotoStarts = starts
                    self.send(1, op, self.POSE)
                    self.assertIn("startJob", self.calls())
                    self.W.run(self.w, after)
                    self.w.aiActive = True
                    self.write("command.xml", encode_command("s1", 2, "stop", {"tractor": 11}))
                    self.W.run(self.w, 100)
                    self.assertEqual("stopCurrentAIJob" in self.calls(), cancelled)

    def test_an_aborted_goto_that_starts_late_is_cancelled(self):
        for op in ("stage", "navigate"):
            with self.subTest(op=op):
                self.w = self.W.new()
                self.adopt()
                self.send(1, op, self.POSE)                # GIANTS has not started it yet
                self.send(2, "stop", {"tractor": 11})
                self.W.run(self.w, 1000)
                self.assertEqual(self.results(2), [(2, "ACCEPTED", "ACCEPTED")])  # not DONE inside the window
                self.w.aiActive = True                     # the job starts late
                self.W.run(self.w, 100)
                self.assertIn("stopCurrentAIJob", self.calls())
                self.W.run(self.w, 5000)
                self.assertEqual(self.results(2)[-1], (2, "DONE", "STOPPED"))

    def test_a_dropped_session_also_cancels_a_late_goto(self):
        self.adopt("s1")
        self.send(1, "navigate", self.POSE)                # not started yet
        self.adopt("s2")                                   # s1 dropped inside the start window
        self.assertIn((1, "FAILED", "SESSION_CHANGED"), self.results())
        self.w.aiActive = True                             # the s1 job starts late
        self.W.run(self.w, 100)
        self.assertIn("stopCurrentAIJob", self.calls())

    def notices(self):
        return list(self.w.notices.values())

    def requests(self):
        return [f for f in self.frames() if f.get("event") == "request"]

    def test_request_key_needs_a_linked_companion(self):
        self.bridge.requestHere(self.bridge)
        self.assertEqual(self.notices(), ["1:Farmhand: the companion is not running"])
        self.assertEqual(self.requests(), [])

    def test_request_key_sends_one_valid_request_per_press(self):
        self.adopt()
        self.assertEqual(self.notices(), ["4:Farmhand: companion connected"])
        self.bridge.requestHere(self.bridge)
        self.bridge.requestHere(self.bridge)                 # held key: within the cooldown
        self.W.run(self.w, 2100)
        self.bridge.requestHere(self.bridge)
        requests = self.requests()
        self.assertEqual([(r["id"], r["session"], r["action"]) for r in requests],
                         [(1, "s1", "fetch_here"), (2, "s1", "fetch_here")])
        for line in self.w.lines.values():
            if '"request"' in line:
                self.assertEqual(parse_frame((line + "\n").encode())["event"], "request")
        self.assertEqual(self.calls(), [])                   # a request never moves anything itself

    def test_companion_notices_are_shown_once_each_and_none_is_lost(self):
        self.write("session.xml", encode_session("s1", 0, [(5, "ACCEPTED")]))
        self.W.run(self.w, 1000)                              # adopted: the earlier notice is not replayed
        self.write("session.xml", encode_session("s1", 1, [(5, "ACCEPTED"), (6, "COMPLETE")]))
        self.W.run(self.w, 1000)
        self.write("session.xml", encode_session("s1", 2, [(5, "ACCEPTED"), (6, "COMPLETE")]))
        self.W.run(self.w, 1000)
        # Two notices inside one poll: both shown, in order.
        self.write("session.xml", encode_session("s1", 3, [(6, "COMPLETE"), (7, "STOPPING"),
                                                           (8, "BLOCKED_NO_ELIGIBLE_PAIR")]))
        self.W.run(self.w, 1000)
        self.write("session.xml", encode_session("s1", 4, [(9, "FAILED_SOMETHING_NEW")]))
        self.W.run(self.w, 1000)
        self.assertEqual(self.notices(), [
            "4:Farmhand: companion connected",
            "4:Farmhand: tractor and implement delivered",
            "4:Farmhand: stopping",
            "1:Farmhand: could not start - no available tractor and implement",
            "1:Farmhand: stopped short - SOMETHING_NEW"])

    def test_the_ai_menu_is_shown_and_the_pick_is_sent(self):
        from protocol.ipc import encode_menu
        self.adopt()
        self.write("menu.xml", encode_menu(4, "Farmhand AI model",
                                           ["qwen3:4b (Ollama, 2.5 GB)", "Odd 'name' + (tag)", "Continue without AI"]))
        self.write("session.xml", encode_session("s1", 1, [(1, "MENU")]))
        self.W.run(self.w, 1000)
        option = self.w.option
        self.assertEqual(option.title, "Farmhand AI model")
        self.assertEqual(list(option.texts.values()),
                         ["qwen3:4b (Ollama, 2.5 GB)", "Odd 'name' + (tag)", "Continue without AI"])
        option.callback(0)                                     # cancelled
        option.callback(9)                                     # out of range
        option.callback(3)
        picks = [(r["menu"], r["choice"]) for r in self.requests() if "menu" in r]
        self.assertEqual(picks, [(4, 3)])
        for line in self.w.lines.values():
            if '"request"' in line:
                parse_frame((line + "\n").encode())

    def test_the_menu_never_opens_over_another_dialog_and_the_key_asks_for_it(self):
        from protocol.ipc import encode_menu
        self.adopt()
        self.w.dialogVisible = True
        self.write("menu.xml", encode_menu(1, "Farmhand AI model", ["Continue without AI"]))
        self.write("session.xml", encode_session("s1", 1, [(1, "MENU")]))
        self.W.run(self.w, 1000)
        self.assertFalse(hasattr(self.w, "option") and self.w.option is not None)
        self.assertIn("4:Farmhand: press Left Alt + N to choose the AI model", self.notices())
        self.bridge.openSettings(self.bridge)
        self.assertEqual([r.get("action") for r in self.requests()], ["settings"])

    def test_notice_texts_for_progress_status_and_outcomes(self):
        cases = {"WORKING_DELIVER": ("Farmhand: driving to the destination", False),
                 "STATUS_WORKING_ALIGN_REVERSE": ("Farmhand: working - reversing to the implement", False),
                 "STATUS_LAST_CANCELLED": ("Farmhand: idle; last job stopped", False),
                 "COMPLETE_MOVE": ("Farmhand: arrived", False),
                 "FAILED_NAVIGATE_STALLED": ("Farmhand: stopped short - the tractor got stuck (parked in a shed or boxed in?)", True)}
        for notice, expected in cases.items():
            with self.subTest(notice=notice):
                self.assertEqual(tuple(self.bridge.noticeText(notice)), expected)

    def test_non_critical_notices_fall_back_to_the_evidenced_kind(self):
        self.w.env.FSBaseMission.INGAME_NOTIFICATION_OK = None
        self.adopt()
        self.assertEqual(self.notices(), ["1:Farmhand: companion connected"])

    def test_request_keys_are_added_to_the_players_global_actions_and_removed(self):
        env = self.w.env
        self.bridge.installInput(self.bridge, env)
        component = env.PlayerInputComponent
        component.registerGlobalPlayerActionEvents(component)
        self.assertEqual(self.calls(), ["vanillaGlobalActions"])       # GIANTS' own actions kept
        entries = {e.action: e for e in self.w.registered.values()}
        self.assertEqual(sorted(entries), [77, 78, 79, 80, 81])
        self.assertTrue(all(entries[a].flags == "falsetruefalsetrue" for a in (77, 78, 79)))
        self.assertEqual(entries[80].flags, "falsefalsetruetrue")             # held: every frame, as CROUCH
        self.adopt()
        entries[77].callback()
        entries[79].callback()
        self.assertEqual([r.get("action") for r in self.requests()], ["fetch_here", "stop"])
        entries[78].callback()
        self.assertIsNotNone(self.w.dialog)                          # the text field opened
        self.bridge.deleteMap(self.bridge)
        component.registerGlobalPlayerActionEvents(component)
        self.assertEqual(len(self.w.registered), 5)                   # hook gone after unload

    def test_keys_match_the_moddesc_actions(self):
        from xml.etree import ElementTree
        root = ElementTree.parse(ROOT / "mod/modDesc.xml").getroot()
        declared = sorted(a.attrib["name"] for a in root.findall("actions/action"))
        self.assertEqual(sorted(self.bridge.KEYS.keys()), declared)

    def test_a_missing_action_disables_only_its_key(self):
        env = self.w.env
        env.InputAction.FARMHAND_TALK = None
        self.bridge.installInput(self.bridge, env)
        env.PlayerInputComponent.registerGlobalPlayerActionEvents(env.PlayerInputComponent)
        self.assertEqual(sorted(e.action for e in self.w.registered.values()), [77, 79, 80, 81])

    def test_push_to_talk_sends_only_the_press_and_release_edges(self):
        self.adopt()
        self.bridge.installInput(self.bridge, self.w.env)
        component = self.w.env.PlayerInputComponent
        component.registerGlobalPlayerActionEvents(component)
        hold = next(e for e in self.w.registered.values() if e.action == 80)
        for value in (0, 0, 1, 1, 1, 0.6, 0, 0, 1, 0):         # GIANTS calls it every frame
            hold.callback(component, "FARMHAND_PUSH_TO_TALK", value)
        self.assertEqual([r["action"] for r in self.requests()],
                         ["talk_start", "talk_end", "talk_start", "talk_end"])
        for line in self.w.lines.values():
            if '"request"' in line:
                parse_frame((line + "\n").encode())

    def test_a_release_lost_to_a_menu_does_not_swallow_the_next_press(self):
        self.adopt()
        self.bridge.installInput(self.bridge, self.w.env)
        component = self.w.env.PlayerInputComponent
        component.registerGlobalPlayerActionEvents(component)
        hold = next(e for e in self.w.registered.values() if e.action == 80)
        hold.callback(component, "FARMHAND_PUSH_TO_TALK", 1)       # held, then a menu opens:
        component.registerGlobalPlayerActionEvents(component)      # GIANTS registers again later
        hold = [e for e in self.w.registered.values() if e.action == 80][-1]
        hold.callback(component, "FARMHAND_PUSH_TO_TALK", 1)       # the next press starts again
        self.assertEqual([r["action"] for r in self.requests()], ["talk_start", "talk_start"])

    def test_push_to_talk_without_a_companion_notifies_once_per_press(self):
        for value in (1, 1, 1, 0, 1, 0):
            self.bridge.pushToTalk(self.bridge, value)
        self.assertEqual(self.notices(), ["1:Farmhand: the companion is not running"] * 2)
        self.assertEqual(self.requests(), [])

    def test_stop_key_is_never_held_back(self):
        self.adopt()
        for _ in range(3):
            self.bridge.requestStop(self.bridge)
        self.assertEqual([r["action"] for r in self.requests()], ["stop", "stop", "stop"])

    def test_typed_words_are_sent_as_text(self):
        self.adopt()
        self.bridge.openTextInput(self.bridge)
        dialog = self.w.dialog
        self.assertEqual((dialog.default, dialog.maxChars, dialog.okText), ("", 200, "OK"))
        dialog.callback(dialog.target, "  bring me a tractor\nand forage wagon  ", True)
        dialog.callback(dialog.target, "never sent", False)        # cancelled dialog
        dialog.callback(dialog.target, "   ", True)               # nothing to send
        texts = [r["text"] for r in self.requests()]
        self.assertEqual(texts, ["bring me a tractor and forage wagon"])
        for line in self.w.lines.values():
            if '"request"' in line:
                parse_frame((line + "\n").encode())

    def test_text_is_cut_on_a_utf8_boundary_and_invalid_text_is_dropped(self):
        clean = lambda t: self.bridge.cleanText(t, self.observe.validUtf8)
        self.assertEqual(clean("Hof S\u00fcd"), "Hof S\u00fcd")
        long = "a" * 199 + "\u00fc" + "b"                         # the 2-byte letter straddles byte 200
        self.assertEqual(clean(long), "a" * 199)
        dropped = self.lua.eval("function(clean, valid) return clean('\\255\\254\\253\\252 bad', valid) == nil end")
        self.assertTrue(dropped(self.bridge.cleanText, self.observe.validUtf8))   # never valid UTF-8
        self.assertIsNone(clean(None))

    def test_text_field_needs_a_linked_companion(self):
        self.bridge.openTextInput(self.bridge)
        self.assertEqual(self.notices(), ["1:Farmhand: the companion is not running"])
        self.assertFalse(hasattr(self.w, "dialog") and self.w.dialog is not None)

    def test_without_the_input_action_no_hook_is_installed(self):
        env = self.w.env
        original = env.PlayerInputComponent.registerGlobalPlayerActionEvents
        env.InputAction = self.lua.table()
        self.bridge.installInput(self.bridge, env)
        same = self.lua.eval("function(a, b) return rawequal(a, b) end")
        self.assertTrue(same(env.PlayerInputComponent.registerGlobalPlayerActionEvents, original))

    def test_attach_needs_the_align_that_found_this_candidate(self):
        self.adopt()
        self.w.env.NetworkUtil.getObject = self.lua.eval("function(t) return function(id) return t end end")(
            self.w.tractor)
        self.send(1, "attach", {"tractor": 11, "wagon": 12, "tractorJoint": 1, "wagonJoint": 1})
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "NOT_ALIGNED"))

    # -------------------------------------------------------------- detach
    def hitched(self, allowed=True, implement_speed=0.0):
        """Tractor 11 with implement 12 hitched; GIANTS' calls as AttacherJoints and
        Attachable define them (getImplementByObject 3209-3219, isDetachAllowed
        1398-1434, detachImplementByObject 2820-2831, getAttacherVehicle 1211-1213)."""
        return self.lua.execute(r"""
            local w, allowed, speed = ...
            local tractor = w.tractor
            local implement = { attacherVehicle = tractor }
            function implement:getLastSpeed() return speed end
            function implement:isDetachAllowed()
                table.insert(w.calls, "isDetachAllowed")
                if allowed then return true end
                return false, "Lower the implement first", true
            end
            function implement:getAttacherVehicle() return self.attacherVehicle end
            function tractor:getImplementByObject(o)
                if o == implement and implement.attacherVehicle == tractor then
                    return { object = o, jointDescIndex = 4, inputJointDescIndex = 1 }
                end
                return nil
            end
            function tractor:detachImplementByObject(o)
                table.insert(w.calls, "detachImplementByObject")
                if w.detachLags then return true end
                o.attacherVehicle = nil
                return true
            end
            w.env.NetworkUtil.getObject = function(id)
                if id == 11 then return tractor elseif id == 12 then return implement end
            end
            return implement
        """, self.w, allowed, implement_speed)

    def test_detach_asks_giants_permission_then_detaches_and_reports_the_observed_end(self):
        self.adopt()
        implement = self.hitched()
        self.send(1, "detach", {"tractor": 11, "wagon": 12})
        self.W.run(self.w, 300)
        self.assertEqual(self.calls()[-2:], ["isDetachAllowed", "detachImplementByObject"])
        self.assertEqual(self.results(1)[-1], (1, "DONE", "DETACHED"))
        self.assertIsNone(implement.attacherVehicle)

    def test_detach_refusals_never_call_giants_detach(self):
        cases = ((dict(allowed=False), {}, "DETACH_NOT_ALLOWED"),
                 (dict(implement_speed=3.0), {}, "NOT_STOPPED"),
                 ({}, {"aiActive": True}, "AI_ACTIVE"))
        for kwargs, state, code in cases:
            with self.subTest(code=code):
                self.w = self.W.new()
                self.adopt()
                self.hitched(**kwargs)
                for key, value in state.items():
                    self.w[key] = value
                self.send(1, "detach", {"tractor": 11, "wagon": 12})
                self.assertEqual(self.results(1)[-1], (1, "FAILED", code))
                self.assertNotIn("detachImplementByObject", self.calls())

    def test_a_detach_that_never_completes_is_unverified(self):
        self.adopt()
        self.hitched()
        self.w.detachLags = True
        self.send(1, "detach", {"tractor": 11, "wagon": 12})
        self.beating(12000)
        self.assertEqual(self.results(1)[-1], (1, "UNVERIFIED", "DETACH_TIMEOUT"))

    # ----------------------------------------------------------- fieldwork
    FIELD = {"tractor": 11, "field": 7, "x": 200.0, "z": 300.0, "yaw": 0.5, "area": 0.0, "seed": 0, "hand": 0}

    def fieldwork_stubs(self, available=True):
        """GIANTS' FIELDWORK job shape (AIJobFieldWork 22-49, 134-224) on the GOTO stub."""
        self.lua.execute(r"""
            local w, available = ...
            local env = w.env
            env.AIJobType.FIELDWORK = 2
            local job = env.g_currentMission.aiJobTypeManager:createJob()
            function job:getIsAvailableForVehicle() return available end
            local create = env.g_currentMission.aiJobTypeManager.createJob
            env.g_currentMission.aiJobTypeManager.createJob = function(m, t)
                table.insert(w.calls, "createJob:" .. tostring(t)); return create(m, t)
            end
            job.vehicleParameter = { getVehicle = function() return w.tractor end }
            env.ClassUtil = { getClassObjectByObject = function(o) return o.class end }
            env.AIMessageSuccessFinishedJob = { name = "finished" }
            env.AIMessageErrorOutOfFill = { name = "outOfFill" }
            w.job = job
        """, self.w, available)

    def end_job(self, message):
        self.lua.execute("local w, B, m = ...; w.aiActive = false; B.onAIJobStopped(w.env, w.job, { class = w.env[m] })",
                         self.w, self.bridge, message)

    def test_fieldwork_starts_giants_helper_and_reports_why_it_ended(self):
        output = []
        self.lua.globals().print = lambda *a: output.append(" ".join(map(str, a)))
        for message, code in (("AIMessageSuccessFinishedJob", "FINISHED"), ("AIMessageErrorOutOfFill", "OUT_OF_FILL")):
            with self.subTest(code=code):
                self.w = self.W.new()
                self.adopt()
                self.fieldwork_stubs()
                self.w.gotoStarts = True
                self.send(1, "fieldwork", self.FIELD)
                self.W.run(self.w, 1000)
                self.assertIn("createJob:2", self.calls())
                self.assertIn("startJob", self.calls())
                self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])    # still working
                self.end_job(message)
                self.W.run(self.w, 300)
                self.assertEqual(self.results(1)[-1], (1, "DONE", code))         # never "worked": measured

    def test_a_handed_off_helper_ends_the_op_working_and_a_stop_still_ends_its_job(self):
        self.adopt()
        self.fieldwork_stubs()
        self.w.gotoStarts = True
        self.lua.execute("local w = ...; function w.tractor:getIsFieldWorkActive() return w.working == true end", self.w)
        self.send(1, "fieldwork", {**self.FIELD, "hand": 1})
        self.W.run(self.w, 1000)
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])          # driving there: not yet
        self.w.working = True
        self.W.run(self.w, 300)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "WORKING"))
        self.assertNotIn("stopCurrentAIJob", self.calls())                       # the helper works on
        self.send(2, "stop", {"tractor": 11})
        self.assertIn("stopCurrentAIJob", self.calls())
        self.lua.execute("local w = ...; w.aiActive = false", self.w)
        self.W.run(self.w, 300)
        self.assertEqual(self.results(2)[-1], (2, "DONE", "STOPPED"))

    def test_a_combine_always_lays_a_straw_swath_where_it_can(self):
        for available, honest, result in ((True, True, ("ACCEPTED", "ACCEPTED")), (False, True, ("ACCEPTED", "ACCEPTED")),
                                          (True, False, ("FAILED", "SWATH_NOT_SET"))):
            with self.subTest(available=available, honest=honest):
                self.w = self.W.new()
                self.adopt()
                self.fieldwork_stubs()
                self.w.gotoStarts = True
                self.lua.execute(r"""local w, available, honest = ...
                    w.tractor.spec_combine = { swath = { isAvailable = available }, isSwathActive = false }
                    function w.tractor:setIsSwathActive(on)
                        table.insert(w.calls, "setIsSwathActive:" .. tostring(on))
                        if honest then self.spec_combine.isSwathActive = on end
                    end""", self.w, available, honest)
                self.send(1, "fieldwork", self.FIELD)
                self.assertEqual(self.results(1)[-1][1:], result)
                self.assertEqual("setIsSwathActive:true" in self.calls(), available)
                self.assertEqual("startJob" in self.calls(), result[0] == "ACCEPTED")

    def seeder(self, seeds, current=1, honest=True):
        """A hitched seeder: SowingMachine spec.seeds / currentSeed and setSeedFruitType as
        GIANTS (432-440 -> setSeedIndex 398-414); `honest=False` ignores the call."""
        return self.lua.execute(r"""
            local w, seeds, current, honest = ...
            local list = {}
            for i = 1, #seeds do list[i] = seeds[i] end
            local o = { spec_sowingMachine = { seeds = list, currentSeed = current } }
            function o:setSeedFruitType(fruit)
                table.insert(w.calls, "setSeedFruitType:" .. tostring(fruit))
                if not honest then return end
                for i, v in ipairs(self.spec_sowingMachine.seeds) do
                    if v == fruit then self.spec_sowingMachine.currentSeed = i; break end
                end
            end
            function w.tractor:getAttachedImplements() return { { object = o } } end
            return o
        """, self.w, self.lua.table_from(seeds), current, honest)

    def test_fieldwork_sets_the_crop_chosen_before_giants_starts(self):
        for seeds, seed in (((7, 3, 9), 3), ((4, 12), 1)):
            with self.subTest(seeds=seeds, seed=seed):
                self.w = self.W.new()
                self.adopt()
                self.fieldwork_stubs()
                self.w.gotoStarts = True
                seeder = self.seeder(seeds)
                self.send(1, "fieldwork", {**self.FIELD, "seed": seed})
                calls = self.calls()
                self.assertIn(f"setSeedFruitType:{seeds[seed - 1]}", calls)
                self.assertLess(calls.index(f"setSeedFruitType:{seeds[seed - 1]}"), calls.index("startJob"))
                self.assertEqual(seeder.spec_sowingMachine.currentSeed, seed)
                self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])

    def test_a_crop_the_seeder_cannot_sow_or_does_not_take_starts_nothing(self):
        for seed, honest, code in ((4, True, "SEED_NOT_SUPPORTED"), (2, False, "SEED_NOT_SET")):
            with self.subTest(code=code):
                self.w = self.W.new()
                self.adopt()
                self.fieldwork_stubs()
                self.seeder((7, 3, 9), honest=honest)
                self.send(1, "fieldwork", {**self.FIELD, "seed": seed})
                self.assertEqual(self.results(1)[-1], (1, "FAILED", code))
                self.assertNotIn("startJob", self.calls())
        self.w = self.W.new()
        self.adopt()
        self.fieldwork_stubs()
        self.lua.execute("local w = ...; function w.tractor:getAttachedImplements() return {} end", self.w)
        self.send(1, "fieldwork", {**self.FIELD, "seed": 1})
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "NO_SEEDER"))

    def test_changed_area_is_the_share_that_moved_times_the_field(self):
        f = self.lua.eval("""function(B)
            local before = { ground = { STUBBLE = 1.0 }, spray = {}, total_m2 = 4000 }
            local after = { ground = { STUBBLE = 0.9, CULTIVATED = 0.1 }, spray = { ["1"] = 0.05 }, total_m2 = 4000 }
            return B.changedArea(before, after), B.changedArea(nil, after), B.changedArea(before, { ground = {} })
        end""")
        changed, unknown, unmeasured = f(self.bridge)
        self.assertAlmostEqual(changed, 400.0)
        self.assertIsNone(unknown)
        self.assertIsNone(unmeasured)

    def test_fieldwork_refusals_and_stop(self):
        self.adopt()
        self.fieldwork_stubs(available=False)
        self.send(1, "fieldwork", self.FIELD)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "FIELDWORK_UNAVAILABLE"))
        self.assertNotIn("startJob", self.calls())
        self.w = self.W.new()
        self.adopt()
        self.fieldwork_stubs()
        self.w.gotoStarts = True
        self.send(1, "fieldwork", self.FIELD)
        self.W.run(self.w, 1000)
        self.send(2, "stop", {"tractor": 11})
        self.W.run(self.w, 300)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "STOPPED"))
        self.assertIn("stopCurrentAIJob", self.calls())
        self.assertEqual(self.results(2)[-1], (2, "DONE", "STOPPED"))

    # ---------------------------------------------------------- pull_clear
    PAIR = {"tractor": 11, "wagon": 12}

    def scripted_pull(self, codes):
        """Bridge.Hitch with a scripted PULL_CLEAR (FarmhandHitch's own is covered by
        tests/test_lua_pullclear.py): each step returns the next of `codes`."""
        fake = self.lua.execute(r"""
            local Hitch, codes, calls = ...
            local F = setmetatable({}, { __index = Hitch })
            function F.newPullClear(env, args)
                table.insert(calls, "newPullClear:" .. tostring(args.tractorJoint) .. "/" .. tostring(args.wagonJoint))
                return { tractor = args.tractor, wagon = args.wagon, n = 0 }
            end
            function F.pullClearStep(env, s, dt)
                if s.result then table.insert(calls, "brake"); return s.result end
                s.n = s.n + 1
                local code = codes[math.min(s.n, #codes)]
                if code ~= "CONTINUE" then s.result = code; table.insert(calls, "brake") end
                return code
            end
            function F.abort(env, s, code)
                if s.result == nil then s.result = code end
                table.insert(calls, "abort:" .. code)
            end
            return F
        """, self.hitch, self.lua.table(*codes), self.w.calls)
        self.bridge.Hitch = fake
        return fake

    def detached(self):
        self.adopt()
        self.hitched()
        self.send(1, "detach", self.PAIR)
        self.W.run(self.w, 300)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "DETACHED"))

    def test_pull_clear_straight_after_the_detach_on_the_joints_it_freed(self):
        self.detached()
        self.scripted_pull(["CONTINUE", "CONTINUE", "CLEAR"])
        self.send(2, "pull_clear", self.PAIR)
        self.W.run(self.w, 300)
        self.assertEqual(self.results(2)[-1], (2, "DONE", "CLEAR"))
        self.assertIn("newPullClear:4/1", self.calls())

    def test_pull_clear_only_straight_after_a_detach_of_that_pair(self):
        cases = {
            "no detach": lambda: self.adopt() or self.hitched(),
            "another op in between": lambda: self.detached() or self.send(5, "stop", {"tractor": 11})
            or self.W.run(self.w, 300),
            "too late": lambda: self.detached() or self.beating(31000),
            "a second pull": lambda: self.detached() or self.send(5, "pull_clear", self.PAIR)
            or self.W.run(self.w, 300),
        }
        for name, setup in cases.items():
            with self.subTest(name):
                self.w = self.W.new()
                self.bridge.Hitch = self.hitch
                setup()
                self.scripted_pull(["CLEAR"])
                self.send(9, "pull_clear", self.PAIR)
                self.assertEqual(self.results(9)[-1], (9, "FAILED", "NOT_JUST_DETACHED"))
                self.assertNotIn("newPullClear:4/1", self.calls())      # the fake never ran

    def test_a_failed_pull_clear_is_failed_and_a_stop_aborts_it(self):
        self.detached()
        self.scripted_pull(["CONTINUE", "STALLED"])
        self.send(2, "pull_clear", self.PAIR)
        self.W.run(self.w, 300)
        self.assertEqual(self.results(2)[-1], (2, "FAILED", "STALLED"))
        self.w = self.W.new()
        self.bridge.Hitch = self.hitch
        self.detached()
        self.scripted_pull(["CONTINUE"])
        self.send(2, "pull_clear", self.PAIR)
        self.W.run(self.w, 100)
        self.send(3, "stop", {"tractor": 11})
        self.W.run(self.w, 300)
        self.assertEqual(self.results(2)[-1], (2, "FAILED", "STOPPED"))
        self.assertIn("abort:STOPPED", self.calls())

    # -------------------------------------------------------------- unpark
    def scripted_unpark(self, codes, refuse=None):
        """Bridge.Hitch with a scripted UNPARK (FarmhandHitch's own is covered by
        tests/test_lua_unpark.py)."""
        fake = self.lua.execute(r"""
            local Hitch, codes, refuse, calls = ...
            local F = setmetatable({}, { __index = Hitch })
            function F.newUnpark(env, args)
                table.insert(calls, "newUnpark")
                if refuse then return nil, refuse, { plans = {} } end
                return { tractor = args.tractor, n = 0 }
            end
            function F.unparkStep(env, s, dt)
                if s.result then table.insert(calls, "brake"); return s.result end
                s.n = s.n + 1
                local code = codes[math.min(s.n, #codes)]
                if code ~= "CONTINUE" then s.result = code; table.insert(calls, "brake") end
                return code
            end
            function F.describeUnpark() return "described" end
            function F.abort(env, s, code)
                if s.result == nil then s.result = code end
                table.insert(calls, "abort:" .. code)
            end
            return F
        """, self.hitch, self.lua.table(*codes), refuse, self.w.calls)
        self.bridge.Hitch = fake
        return fake

    def test_unpark_is_done_only_clear_and_at_rest(self):
        self.adopt()
        self.scripted_unpark(["CONTINUE", "CLEAR"])
        self.w.speed = 0.0
        self.send(1, "unpark", {"tractor": 11})
        self.W.run(self.w, 300)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "UNPARKED"))
        self.assertIn("newUnpark", self.calls())

    def test_unpark_refusals_and_failures(self):
        self.adopt()
        self.scripted_unpark(["CLEAR"], refuse="UNPARK_EXIT_BLOCKED")
        self.send(1, "unpark", {"tractor": 11})
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "UNPARK_EXIT_BLOCKED"))
        self.scripted_unpark(["CONTINUE", "OBSTACLE_AHEAD"])
        self.send(2, "unpark", {"tractor": 11})
        self.W.run(self.w, 300)
        self.assertEqual(self.results(2)[-1], (2, "FAILED", "OBSTACLE_AHEAD"))
        self.w.aiActive = True                                  # never under another controller
        self.send(3, "unpark", {"tractor": 11})
        self.assertEqual(self.results(3)[-1], (3, "FAILED", "AI_ACTIVE"))

    def test_a_stop_pre_empts_an_unpark_and_keeps_it_braked(self):
        self.adopt()
        self.scripted_unpark(["CONTINUE"])
        self.send(1, "unpark", {"tractor": 11})
        self.W.run(self.w, 200)
        self.send(2, "stop", {"tractor": 11})
        self.W.run(self.w, 300)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "STOPPED"))
        self.assertEqual(self.results(2)[-1], (2, "DONE", "STOPPED"))
        self.assertIn("abort:STOPPED", self.calls())

    def test_a_goto_blocked_at_its_start_ends_with_that_reason(self):
        # Live 53c77d1e809645b3-3: accepted, agent BLOCKED from the first frame, unmoved.
        output = []
        self.lua.globals().print = lambda *a: output.append(" ".join(map(str, a)))
        for op, code in (("navigate", "NAVIGATE_BLOCKED_AT_START"), ("stage", "STAGE_BLOCKED_AT_START")):
            with self.subTest(op):
                self.w = self.W.new()
                self.adopt()
                self.w.gotoStarts = True
                self.w.tractor.spec_aiDrivable = self.lua.table(lastState=3, distanceToTarget=93.8)
                self.lua.execute("local w = ...; w.env.AIDrivable = { STATES = { [3] = 'blocked' } }", self.w)
                self.send(1, op, self.POSE)
                self.beating(85000)
                self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])   # as long as the stall watch
                self.beating(7000)
                self.assertEqual(self.results(1)[-1], (1, "FAILED", code))
                self.assertIn("stopCurrentAIJob", self.calls())
        self.assertTrue(any("blocked at its start: agent state blocked, 93.8 m" in line for line in output), output)

    def test_a_navigate_planning_first_then_blocked_unmoved_is_blocked_at_its_start(self):
        # Live fc25907b61fb402f-1: the stall watch ended it at 95 s ("agent state blocked")
        # before 90 s of uninterrupted BLOCKED had passed: the agent planned first.
        self.adopt()
        self.w.gotoStarts = True
        self.w.tractor.spec_aiDrivable = self.lua.table(lastState=1, distanceToTarget=93.8)
        self.lua.execute("local w = ...; w.env.AIDrivable = { STATES = { [1] = 'planning', [3] = 'blocked' } }",
                         self.w)
        self.send(1, "navigate", self.POSE)
        self.beating(8000)
        self.w.tractor.spec_aiDrivable.lastState = 3
        self.beating(86000)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "NAVIGATE_BLOCKED_AT_START"))

    def test_a_goto_that_left_its_start_is_never_blocked_at_its_start(self):
        self.adopt()
        self.w.gotoStarts = True
        self.w.tractor.spec_aiDrivable = self.lua.table(lastState=3, distanceToTarget=50.0)
        self.lua.execute("local w = ...; w.env.AIDrivable = { STATES = { [3] = 'BLOCKED' } }", self.w)
        self.send(1, "stage", self.POSE)
        self.beating(10000)
        self.w.x += 3.0                                             # drove off, then blocked on the way
        self.beating(100000)
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])
        self.beating(12000)                                         # only the stage's own 120 s bound
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "STAGE_TIMEOUT"))

    # --------------------------------------------------------- fold, belts
    def transport(self):
        """Vehicle 12, a deckover with GIANTS Foldable ramps (setFoldDirection 811,
        getIsFoldAllowed 987-1009, onUpdate 552-611 moving foldAnimTime to the end and
        stopping foldMoveDirection) and TensionBelts (setAllTensionBeltsActive 912-920,
        updateFastenState 925-935); stepped 25% per 100 ms frame."""
        self.adopt()
        return self.lua.execute(r"""
            local w = ...
            local deck = { ownerFarmId = 1, rootNode = "tractorRoot", allowed = true, speed = 0,
                           spec_foldable = { foldingParts = { {} }, foldMoveDirection = 0, foldAnimTime = 0 },
                           spec_tensionBelts = { hasTensionBelts = true, areAllBeltsFastened = false, objectsToJoint = {} } }
            function deck:getLastSpeed() return self.speed end
            function deck:getFoldAnimTime() return self.spec_foldable.foldAnimTime end
            function deck:getIsFoldAllowed(direction, onAiTurnOn) return self.allowed end
            function deck:setFoldDirection(direction)
                table.insert(w.calls, "setFoldDirection:" .. direction)
                self.spec_foldable.foldMoveDirection = direction
            end
            function deck:setAllTensionBeltsActive(isActive)
                table.insert(w.calls, "setAllTensionBeltsActive:" .. tostring(isActive))
                if not self.beltsStuck then self.spec_tensionBelts.areAllBeltsFastened = isActive end
            end
            w.deck = deck
            local getObject = w.env.NetworkUtil.getObject
            w.env.NetworkUtil.getObject = function(id) if id == 12 then return deck end return getObject(id) end
            -- GIANTS' Foldable:onUpdate, coarsely: the fold time runs to its end, then stops.
            local update = w.step
            w.foldFrame = function()
                local f = deck.spec_foldable
                if f.foldMoveDirection ~= 0 and not deck.foldStuck then
                    f.foldAnimTime = math.max(0, math.min(1, f.foldAnimTime + 0.25 * f.foldMoveDirection))
                    if f.foldAnimTime == 0 or f.foldAnimTime == 1 then f.foldMoveDirection = 0 end
                end
            end
            return w
        """, self.w)

    def run_folding(self, ms):
        for _ in range(ms // 100):
            self.w.foldFrame()
            self.W.run(self.w, 100)

    def test_fold_moves_giants_folding_parts_to_the_asked_end_and_waits_for_it(self):
        w = self.transport()
        self.send(1, "fold", {"tractor": 12, "direction": 1})
        self.assertIn("setFoldDirection:1", self.calls())
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])
        self.run_folding(1000)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "FOLDED"))
        self.assertEqual(w.deck.spec_foldable.foldAnimTime, 1)
        self.send(2, "fold", {"tractor": 12, "direction": -1})
        self.run_folding(1000)
        self.assertEqual(self.results(2)[-1], (2, "DONE", "FOLDED"))
        self.assertEqual(w.deck.spec_foldable.foldAnimTime, 0)

    def test_fold_refusals_and_a_fold_that_never_ends(self):
        cases = {"FOLD_NOT_ALLOWED": lambda w: setattr(w.deck, "allowed", False),
                 "NOT_STOPPED": lambda w: setattr(w.deck, "speed", 4.0),
                 "NOT_FOLDABLE": lambda w: setattr(w.deck, "spec_foldable", None)}
        for code, spoil in cases.items():
            with self.subTest(code):
                self.w = self.W.new()
                w = self.transport()
                spoil(w)
                self.send(1, "fold", {"tractor": 12, "direction": 1})
                self.assertEqual(self.results(1)[-1], (1, "FAILED", code))
                self.assertNotIn("setFoldDirection:1", self.calls())
        self.w = self.W.new()
        w = self.transport()
        w.deck.foldStuck = True
        self.send(1, "fold", {"tractor": 12, "direction": 1})
        self.beating(21000)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "FOLD_TIMEOUT"))

    def test_belts_are_fastened_and_released_through_giants_tension_belts(self):
        w = self.transport()
        self.send(1, "belts", {"tractor": 12, "direction": 1})
        self.W.run(self.w, 200)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "BELTS_FASTENED"))
        self.assertIn("setAllTensionBeltsActive:true", self.calls())
        self.send(2, "belts", {"tractor": 12, "direction": -1})
        self.W.run(self.w, 200)
        self.assertEqual(self.results(2)[-1], (2, "DONE", "BELTS_RELEASED"))
        w.deck.beltsStuck = True
        self.send(3, "belts", {"tractor": 12, "direction": 1})
        self.W.run(self.w, 6000)
        self.assertEqual(self.results(3)[-1], (3, "FAILED", "BELTS_NOT_OBSERVED"))

    # ---------------------------------------------------------- open_gates
    def doors_world(self):
        """A closed door (GIANTS AnimatedObject via a placeable) at (944.1, 772.0), the
        reserved truck 13.8 m from it, a parked trailer 4 m from it, a car 40 m off."""
        self.adopt()
        return self.lua.execute(r"""
            local w = ...
            local door = { animation = { time = 0 }, nodeId = "door", saveId = "wallUp", raised = 0 }
            function door:raiseActive() self.raised = self.raised + 1 end
            w.door = door
            w.env.g_currentMission.placeableSystem = { placeables = {
                { spec_animatedObjects = { animatedObjects = { door } } } } }
            w.speeds = { tractorRoot = 0 }
            local function vehicle(node)
                local v = { rootNode = node }
                function v:getLastSpeed() return w.speeds[node] end
                w.speeds[node] = 0
                return v
            end
            w.pos = { door = { 944.1, 772.0 }, tractorRoot = { 955.6, 779.6 }, trailer = { 948.1, 772.0 },
                      car = { 984.1, 772.0 } }
            w.env.g_currentMission.vehicleSystem = { vehicles = { w.tractor, vehicle("trailer"), vehicle("car") } }
            w.tractor.getLastSpeed = function() return w.speeds.tractorRoot end
            w.env.getWorldTranslation = function(n)
                local p = w.pos[n]
                if p == nil then error("unreadable " .. tostring(n)) end
                return p[1], 0, p[2]
            end
            return w
        """, self.w)

    GATES = {"x": 955.6, "z": 779.6, "area": 15.0}

    def test_a_door_opens_only_with_the_vehicles_near_it_at_rest_and_unmoved(self):
        w = self.doors_world()
        self.send(1, "open_gates", self.GATES)
        self.assertEqual(w.door.animation.direction, 1)
        self.W.run(self.w, 7000)
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])       # still settling
        self.W.run(self.w, 1200)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "GATES_OPENED"))

    def test_a_vehicle_moving_near_the_door_keeps_it_closed(self):
        w = self.doors_world()
        w.speeds.trailer = 2.0
        self.send(1, "open_gates", self.GATES)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "DOOR_NEAR_MOVING_VEHICLE"))
        self.assertIsNone(w.door.animation.direction)
        self.assertEqual(w.door.raised, 0)
        self.w = self.W.new()
        w = self.doors_world()
        w.speeds.car = 9.0                                                       # 40 m off: not watched
        self.send(1, "open_gates", self.GATES)
        self.assertEqual(w.door.animation.direction, 1)

    def test_a_vehicle_moving_while_the_door_opens_fails_the_op(self):
        # Live bfbc32565ecb4472-1: the rig moved after the Large Shed's wall opened.
        def pushed(w):
            w.pos.tractorRoot[2] = 777.0

        def rolling(w):
            w.speeds.tractorRoot = 1.5

        for name, change in (("pushed", pushed), ("rolling at the end", rolling)):
            with self.subTest(name):
                self.w = self.W.new()
                w = self.doors_world()
                self.send(1, "open_gates", self.GATES)
                self.W.run(self.w, 3000)
                change(w)
                self.W.run(self.w, 6000)
                self.assertEqual(self.results(1)[-1], (1, "FAILED", "DOOR_MOVED_VEHICLE"))

    def test_an_unreadable_vehicle_near_the_door_keeps_it_closed(self):
        w = self.doors_world()
        w.pos.trailer = None
        self.send(1, "open_gates", self.GATES)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "DOOR_AREA_UNKNOWN"))
        self.assertIsNone(w.door.animation.direction)

    def test_unpark_never_drives_round_a_closed_door_and_never_opens_it(self):
        # The user, 2026-10-20 (roads only): the Large Shed's wall 13.8 m from the truck.
        w = self.doors_world()
        self.scripted_unpark(["CLEAR"])
        self.send(1, "unpark", {"tractor": 11})
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "UNPARK_DOOR_CLOSED"))
        self.assertNotIn("newUnpark", self.calls())
        self.assertIsNone(w.door.animation.direction)
        w.door.animation.time = 1                                          # open: nothing in the way
        self.send(2, "unpark", {"tractor": 11})
        self.W.run(self.w, 300)
        self.assertEqual(self.results(2)[-1], (2, "DONE", "UNPARKED"))

    def test_no_closed_door_in_reach(self):
        w = self.doors_world()
        w.door.animation.time = 1
        self.send(1, "open_gates", self.GATES)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "NO_CLOSED_GATE"))

    # ------------------------------------------------------------ maneuver
    MANEUVER = {"tractor": 11, "wagon": 12, "tractorJoint": 4, "wagonJoint": 1}

    def scripted_maneuver(self, codes, refuse=None):
        """Bridge.Hitch with a scripted LOCAL_MANEUVER (FarmhandHitch's own is covered by
        tests/test_lua_maneuver.py): newManeuver refuses with `refuse`, else each step
        returns the next of `codes` (then the last); abort latches like the real one."""
        fake = self.lua.execute(r"""
            local Hitch, codes, refuse, calls = ...
            local F = setmetatable({}, { __index = Hitch })
            function F.newManeuver(env, args)
                table.insert(calls, "newManeuver:" .. tostring(args.tractorJoint) .. "/" .. tostring(args.wagonJoint))
                if refuse then F.onNote("REFUSED " .. refuse); return nil, refuse end
                F.onNote("LOCAL_TURN START gap=12.87")
                return { tractor = args.tractor, wagon = args.wagon, n = 0 }
            end
            function F.maneuverStep(env, s, dt)
                if s.result then table.insert(calls, "brake"); return s.result end
                s.n = s.n + 1
                local code = codes[math.min(s.n, #codes)]
                if code ~= "CONTINUE" then s.result = code; table.insert(calls, "brake") end
                return code
            end
            function F.abort(env, s, code)
                if s.result == nil then s.result = code end
                table.insert(calls, "abort:" .. code)
            end
            return F
        """, self.hitch, self.lua.table(*codes), refuse, self.w.calls)
        self.bridge.Hitch = fake
        wagon = self.lua.table(ownerFarmId=1)
        self.w.env.NetworkUtil.getObject = self.lua.eval(
            "function(t, w) return function(id) if id == 11 then return t elseif id == 12 then return w end end end")(
            self.w.tractor, wagon)
        return fake

    def test_maneuver_is_done_only_lined_up_and_at_rest(self):
        self.adopt()
        output = []
        self.lua.globals().print = lambda *a: output.append(" ".join(map(str, a)))
        self.scripted_maneuver(["CONTINUE", "CONTINUE", "LINED_UP"])
        self.w.speed = 2.0                                     # still rolling when it lines up
        self.send(1, "maneuver", self.MANEUVER)
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])
        self.W.run(self.w, 1000)
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])   # braking, not yet at rest
        self.assertIn("brake", self.calls())
        self.w.speed = 0.0
        self.W.run(self.w, 200)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "LINED_UP"))
        self.assertIn("newManeuver:4/1", self.calls())
        self.assertTrue(any("maneuver LOCAL_TURN START gap=12.87" in line for line in output))

    def test_maneuver_that_never_comes_to_rest_fails(self):
        self.adopt()
        self.scripted_maneuver(["LINED_UP"])
        self.w.speed = 3.0
        self.send(1, "maneuver", self.MANEUVER)
        self.beating(7000)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "NOT_STOPPED"))

    def test_maneuver_failures_and_refusals(self):
        for codes, refuse, expected in ((["CONTINUE", "DIVERGED"], None, ("FAILED", "DIVERGED")),
                                        (["KEEP_OUT"], None, ("FAILED", "KEEP_OUT")),
                                        (["UNKNOWN_OBSERVATION"], None, ("UNVERIFIED", "UNKNOWN_OBSERVATION")),
                                        ([], "LOCAL_PATH_HITS_WAGON", ("FAILED", "LOCAL_PATH_HITS_WAGON")),
                                        ([], "SEATED_DURING_MANEUVER", ("FAILED", "SEATED_DURING_MANEUVER"))):
            with self.subTest(codes=codes, refuse=refuse):
                self.setUp()
                self.adopt()
                self.scripted_maneuver(codes, refuse)
                self.send(1, "maneuver", self.MANEUVER)
                self.W.run(self.w, 300)
                self.assertEqual(self.results(1)[-1], (1, *expected))

    def test_maneuver_needs_the_wagon(self):
        self.adopt()
        self.scripted_maneuver(["CONTINUE"])
        self.send(1, "maneuver", {**self.MANEUVER, "wagon": 99})
        self.assertEqual(self.results(1)[-1], (1, "REJECTED", "UNKNOWN_VEHICLE"))
        self.assertNotIn("newManeuver:4/1", self.calls())

    def test_stop_pre_empts_a_maneuver_and_keeps_it_braked(self):
        self.adopt()
        self.scripted_maneuver(["CONTINUE"])
        self.send(1, "maneuver", self.MANEUVER)
        self.w.speed = 2.0
        self.send(2, "stop", {"tractor": 11})
        self.assertIn((1, "FAILED", "STOPPED"), self.results())
        self.assertIn("abort:STOPPED", self.calls())
        self.w.speed = 0.0
        self.W.run(self.w, 200)
        self.assertEqual(self.results(2)[-1], (2, "DONE", "STOPPED"))

    def test_the_rigs_stop_pre_empts_the_combines_overload_into_its_trailer(self):
        # overload runs on the combine (12) into the trailer (13) that the rig's tractor (11)
        # tows; the coordinator's STOP is for the rig's tractor. It must end the discharge.
        self.lua.execute("""local w = ...
            local c = { ownerFarmId = 1, speed = 0, motor = 2, level = 9000, rootNode = "combineRoot" }
            c.rootVehicle = c
            c.spec_pipe = { currentState = 2, unloadingStates = { [2] = true } }
            function c:getLastSpeed() return 0 end
            function c:getIsEntered() return false end
            function c:getIsAIActive() return false end
            function c:setPipeState(s) self.spec_pipe.targetState = s end
            function c:getCurrentPipeState() return self.spec_pipe.currentState end
            function c:getMotorState() return self.motor end
            function c:getCanMotorRun() return true end
            function c:startMotor() end
            function c:raiseActive() end
            function c:getCurrentDischargeNode() return { fillUnitIndex = 2, node = 1 } end
            function c:getFillUnitFillLevel() return self.level end
            function c:getFillUnitCapacity() return 9000 end
            local t = { ownerFarmId = 1, rootVehicle = w.tractor }
            function t:getFillUnits() return { {} } end
            function t:getFillUnitFillLevel() return 0 end
            function t:getFillUnitCapacity() return 20000 end
            w.combine = c
            w.env.MotorState = { OFF = 0, STARTING = 1, ON = 2 }
            local get = w.env.NetworkUtil.getObject
            w.env.NetworkUtil.getObject = function(id) if id == 12 then return c elseif id == 13 then return t end return get(id) end
            w.env.NetworkUtil.getObjectId = function() return 12 end
        """, self.w)
        self.adopt()
        self.send(1, "overload", {"tractor": 12, "wagon": 13})
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])
        self.send(2, "stop", {"tractor": 11})
        self.assertIn((1, "FAILED", "STOPPED"), self.results())
        self.assertEqual(self.w.combine.spec_pipe.targetState, 1)                 # the discharge ended
        self.W.run(self.w, 200)
        self.assertEqual(self.results(2)[-1], (2, "DONE", "STOPPED"))

    def test_a_lost_companion_aborts_the_maneuver(self):
        self.adopt()
        self.scripted_maneuver(["CONTINUE"])
        self.send(1, "maneuver", self.MANEUVER)
        self.W.run(self.w, 11000)
        self.assertIn((1, "FAILED", "COMPANION_LOST"), self.results())
        self.assertIn("abort:STOPPED", self.calls())          # braked and latched, as a stop
        self.assertIsNone(self.bridge.lastManeuver)

    def test_maneuver_notice_texts(self):
        cases = {"WORKING_LOCAL_MANEUVER": ("Farmhand: lining up with the implement", False),
                 "WORKING_STAGE": ("Farmhand: driving to the implement", False),
                 "FAILED_SEATED_DURING_MANEUVER": (
                     "Farmhand: stopped short - step out of the tractor for the last metres of the hitch, "
                     "then ask again", True),
                 "FAILED_LOCAL_PATH_HITS_WAGON": (
                     "Farmhand: stopped short - there is no room to line up with the implement from here", True)}
        for notice, expected in cases.items():
            with self.subTest(notice=notice):
                self.assertEqual(tuple(self.bridge.noticeText(notice)), expected)

    def test_probe_cannot_start_during_a_companion_session(self):
        self.adopt()
        self.assertEqual(self.bridge.owner, "companion:s1")
        probe = self.lua.execute((ROOT / "mod/FarmhandProbe.lua").read_text())
        probe.env, probe.Bridge = self.w.env, self.bridge
        output = []
        self.lua.globals().print = lambda *a: output.append(" ".join(map(str, a)))
        probe.consoleProbe(probe, "11", "12")
        self.assertTrue(any("BUSY" in line for line in output))
        self.assertIsNone(probe.task)


class Lua51BridgeTests(LuaBridgeTests):
    runtime = lua51


class BuyTests(unittest.TestCase):
    """GIANTS' own BuyVehicleData with the buyer's configurations: the hitch option
    (attacherJoint) and the engine (motor, StoreItemUtil storeItem.configurations["motor"]),
    each 1-based and only one the store item has; the price is GIANTS' getBuyPrice for
    those configurations. Stubs only."""

    ENV = r"""
        local env = { bought = {} }
        local series9 = { id = 21, name = "Series 9 2019", xmlFilename = "series9.xml",
            defaultConfigurationIds = { motor = 1, wheel = 2 },
            configurations = { motor = { { name = "9290 TTV" }, { name = "9310 TTV" }, { name = "9340 TTV" },
                { name = "9340 TTV Chipped" }, { name = "1000 HP" }, { name = "Boss Hoss", price = 55000 } },
                attacherJoint = { { name = "Standard" }, { name = "Hitch" } } } }
        env.g_storeManager = { getItems = function() return { [21] = series9 } end }
        env.StoreItemUtil = { getIsVehicle = function(item) return true end }
        env.balance = 1000000
        env.g_farmManager = { getFarmById = function(_, id) return { getBalance = function() return env.balance end } end }
        env.g_currentMission = { storeSpawnPlaces = {}, usedStorePlaces = {}, vehicles = {},
            economyManager = { getBuyPrice = function(_, item, configs)
                local option = item.configurations.motor[configs.motor]
                return 200000 + (option and option.price or 0) end } }
        env.BuyVehicleData = { new = function()
            local d = {}
            function d:setStoreItem(item) self.item = item end
            function d:setConfigurations(c) self.configs = c end
            function d:setOwnerFarmId(f) self.farm = f end
            function d:setPrice(p) self.price = p end
            function d:buy() env.bought[#env.bought + 1] = self end
            return d end }
        return env
    """

    def setUp(self):
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.bridge = self.lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        self.start = self.lua.eval("""function(B, env, hitch, engine)
            local spec = { op = 'buy', item = 21, hitch = hitch, engine = engine, farmId = 1,
                           owned = function() return true end }
            local task, code = B.start(env, spec)
            return task, code end""")

    def test_the_engine_and_hitch_options_are_giants_configurations_and_priced_by_giants(self):
        env = self.lua.execute(self.ENV)
        task, code = self.start(self.bridge, env, 2, 6)
        self.assertIsNotNone(task, code)
        data = env.bought[1]
        self.assertEqual((data.configs.motor, data.configs.attacherJoint, data.configs.wheel), (6, 2, 2))
        self.assertEqual(data.price, 255000)
        env = self.lua.execute(self.ENV)
        task, code = self.start(self.bridge, env, 0, 0)                   # the defaults
        self.assertEqual((env.bought[1].configs.motor, env.bought[1].configs.attacherJoint), (1, None))

    def test_other_options_by_their_place_in_the_catalog_order(self):
        # Observe.itemOptions: the sets first, then configuration types by name (attacherJoint,
        # then motor), colours last (live: the plow's colours filled the cap), each choice in
        # order; a set selects its configurations.
        lua = self.lua
        observe = lua.execute((ROOT / "mod/FarmhandObserve.lua").read_text())
        self.bridge.Observe = observe
        env = lua.execute(self.ENV)
        lua.execute("""local env = ...
            local item = env.g_storeManager:getItems()[21]
            item.configurationSets = { { name = "Default", configurations = { wheel = 1 } },
                                       { name = "25M Unrealistic", configurations = { design7 = 5, wheel = 3 } } }
            item.configurations.baseColor = { { name = "Claas Yellow" }, { name = "Kubota Orange" } }""", env)
        options = [dict(o) for o in observe.itemOptions(env.g_storeManager.getItems(env.g_storeManager)[21]).values()]
        names = [o["name"] for o in options]
        self.assertEqual(names[:4], ["Default", "25M Unrealistic", "Standard", "Hitch"])
        self.assertEqual(names[-2:], ["Claas Yellow", "Kubota Orange"])
        start = lua.eval("""function(B, env, o1, o2)
            local spec = { op = 'buy', item = 21, hitch = 0, engine = 0, opt1 = o1, opt2 = o2, farmId = 1,
                           owned = function() return true end }
            local task, code = B.start(env, spec)
            return task, code end""")
        task, code = start(self.bridge, env, 2, 4)                                   # the set, and "Hitch"
        self.assertIsNotNone(task, code)
        configs = env.bought[1].configs
        self.assertEqual((configs.design7, configs.wheel, configs.attacherJoint), (5, 3, 2))
        env = lua.execute(self.ENV)
        self.assertEqual(start(self.bridge, env, 99, 0)[1], "UNKNOWN_CONFIGURATION")

    def test_an_option_the_item_lacks_or_the_engine_price_unpaid_buys_nothing(self):
        for hitch, engine, balance, reason in ((0, 9, 1e6, "UNKNOWN_CONFIGURATION"), (3, 0, 1e6, "UNKNOWN_CONFIGURATION"),
                                               (0, 6, 240000, "NOT_ENOUGH_MONEY")):
            with self.subTest(reason=reason, engine=engine):
                env = self.lua.execute(self.ENV)
                env.balance = balance
                task, code = self.start(self.bridge, env, hitch, engine)
                self.assertIsNone(task)
                self.assertEqual(code, reason)
                self.assertEqual(len(env.bought), 0)


class AutoDriveMarkerTests(unittest.TestCase):
    """ad_marker: AutoDrive's own ADGraphManager:createMapMarker (GraphManager.lua 360-381),
    reached as the environment of its GetAvailableDestinations (getfenv, which AutoDrive
    uses in FS25), on the connected waypoint nearest field N's GIANTS boundary within 30 m.
    Stubs only: AutoDrive's event round trip is stood in for by an immediate marker."""

    ENV = r"""
        local env = { made = {} }
        local graph = { markers = {} }
        graph.points = {
            [1] = { x = -853, z = 420, out = { 2 }, incoming = { 3 } },      -- the road beside the field, 3 m off
            [2] = { x = -855, z = 440, out = { 3 }, incoming = { 1 } },      -- 5 m off: farther from the edge
            [3] = { x = -820, z = 420, out = {}, incoming = { 2 } },         -- inside the field, but a dead end
            [4] = { x = -700, z = 100, out = { 1 }, incoming = { 1 } },      -- far away
        }
        function graph:getWayPoints() return self.points end
        function graph:getMapMarkerByName(name) return self.markers[name] end
        function graph:createMapMarker(id, name) env.made[#env.made + 1] = { id = id, name = name }; self.markers[name] = { id = id } end
        local adEnv = { ADGraphManager = graph }
        local AD = {}
        function AD:GetAvailableDestinations() return {} end
        env.AutoDrive = AD
        env.getfenv = function(fn) if fn == AD.GetAvailableDestinations then return adEnv end return {} end
        local function node(x, z) return { x = x, y = 0, z = z } end
        env.getWorldTranslation = function(n) return n.x, n.y, n.z end
        -- Field 3: a square whose west edge runs along x = -850.
        env.g_fieldManager = { fields = { { fieldId = 3, polygonPoints = { node(-850, 0), node(-850, 800), node(-50, 800), node(-50, 0) } },
                                          { fieldId = 7, polygonPoints = { node(500, 500), node(600, 500), node(600, 600) } } } }
        env.graph = graph
        return env
    """

    def setUp(self):
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.bridge = self.lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        self.start = self.lua.eval("""function(B, env, field)
            local task, code = B.start(env, { op = 'ad_marker', field = field, owned = function() return true end })
            if task == nil then return nil, code end
            task.elapsed = 0
            local status, result = B.step(env, task, 16)
            return task, code, status, result end""")

    def test_the_marker_goes_on_the_connected_waypoint_nearest_the_fields_edge(self):
        env = self.lua.execute(self.ENV)
        task, _, status, code = self.start(self.bridge, env, 3)
        self.assertIsNotNone(task)
        self.assertEqual((status, code), ("DONE", "MARKER_CREATED"))
        self.assertEqual((env.made[1].id, env.made[1].name), (1, "Field 3"))
        task, _, status, code = self.start(self.bridge, env, 3)                 # there now: nothing made
        self.assertEqual((status, code), ("DONE", "MARKER_EXISTS"))
        self.assertEqual(len(env.made), 1)

    def test_no_marker_without_a_network_at_the_field_or_without_autodrive_s_own_call(self):
        env = self.lua.execute(self.ENV)
        self.assertEqual(self.start(self.bridge, env, 7)[1], "AD_NO_WAYPOINT_AT_FIELD")
        self.assertEqual(self.start(self.bridge, env, 9)[1], "FIELD_NOT_FOUND")
        env.getfenv = None
        self.assertEqual(self.start(self.bridge, env, 3)[1], "AD_MARKER_UNSUPPORTED")
        self.assertEqual(len(env.made), 0)


class ContractOpTests(unittest.TestCase):
    """contract: GIANTS' field contracts as the menu takes and collects them. Stubs in
    the documented shapes only: MissionManager getMissions / missionTypes {name,
    classObject} / startMission(mission, farmId, spawnVehicles) -> MissionStartState /
    dismissMission; AbstractMission status, farmId, finishState, getIsRunning, getReward;
    AbstractFieldMission:getField."""

    ENV = r"""
        local env = { started = {}, dismissed = {} }
        env.MissionStatus = { CREATED = 0, PREPARING = 1, RUNNING = 2, FINISHED = 3, DISMISSED = 4 }
        env.MissionStartState = { OK = 0, LIMIT_REACHED = 1, ALREADY_STARTED = 2 }
        env.MissionFinishState = { NONE = 0, SUCCESS = 1, FAILED = 2 }
        local Plow, Harvest = {}, {}
        local function mission(class, field, reward)
            local m = { class = class, status = env.MissionStatus.CREATED, farmId = nil,
                        finishState = env.MissionFinishState.NONE, field = { fieldId = field } }
            function m:isa(c) return self.class == c end
            function m:getField() return self.field end
            function m:getReward() return reward end
            function m:getIsRunning() return self.status == env.MissionStatus.RUNNING end
            return m
        end
        local missions = { mission(Harvest, 20, 9000), mission(Plow, 21, 3000), mission(Plow, 22, 4000) }
        env.missions = missions
        env.startState = env.MissionStartState.OK
        env.g_missionManager = {
            missionTypes = { { name = "plow", classObject = Plow }, { name = "harvest", classObject = Harvest } },
            getMissions = function() return missions end,
            startMission = function(_, m, farmId, spawn)
                env.started[#env.started + 1] = { field = m.field.fieldId, farmId = farmId, spawn = spawn }
                if env.startState ~= env.MissionStartState.OK then return env.startState end
                m.farmId = farmId; m.status = env.MissionStatus.PREPARING
                return env.MissionStartState.OK
            end,
            dismissMission = function(_, m) env.dismissed[#env.dismissed + 1] = m.field.fieldId; m.status = env.MissionStatus.DISMISSED end,
        }
        return env
    """

    def setUp(self):
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().print = lambda *a: None
        observe = self.lua.execute((ROOT / "mod/FarmhandObserve.lua").read_text())
        self.bridge = self.lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        self.bridge.Observe = observe
        self.observe = observe
        self.env = self.lua.execute(self.ENV)
        self.run = self.lua.eval("""function(B, env, field, direction)
            local task, code = B.start(env, { op = 'contract', field = field, direction = direction, farmId = 1,
                                             owned = function() return true end })
            if task == nil then return nil, code end
            task.elapsed = 0
            return task, B.step(env, task, 16) end""")

    def contracts(self):
        listed = self.observe.contracts(self.env, 1)
        return [(c.field, c.kind, c.status, c.mine, c.reward) for c in listed.values()]

    def test_contracts_are_listed_best_paid_first_with_their_type_and_state(self):
        self.assertEqual(self.contracts(), [(20, "harvest", "CREATED", False, 9000), (22, "plow", "CREATED", False, 4000),
                                            (21, "plow", "CREATED", False, 3000)])

    def test_taking_a_contract_starts_it_with_the_farms_own_equipment_and_waits_for_it_running(self):
        task, status, code = self.run(self.bridge, self.env, 21, 1)
        self.assertEqual((self.env.started[1].field, self.env.started[1].farmId, self.env.started[1].spawn), (21, 1, False))
        self.assertIsNone(status)                                   # PREPARING: not running yet
        self.env.missions[2].status = self.env.MissionStatus.RUNNING
        self.assertEqual(self.bridge.step(self.env, task, 16), ("DONE", "CONTRACT_RUNNING"))
        self.assertIn((21, "plow", "RUNNING", True, 3000), self.contracts())
        # Its field (another farm's land) is now one Farmhand may work.
        self.lua.execute("""local env = ...
            local f = env.missions[2].field
            f.posX, f.posZ, f.areaHa, f.farmland = 400, 500, 1.5, { id = 9 }
            env.g_fieldManager = { fields = { f, { fieldId = 22, posX = 1, posZ = 1, areaHa = 2, farmland = { id = 8 } } } }
            env.g_farmlandManager = { getFarmlandOwner = function() return 0 end }""", self.env)
        self.assertEqual([f.id for f in self.observe.fields(self.env, 1).values()], [21])

    def test_a_refused_start_or_a_missing_contract_says_why(self):
        self.env.startState = self.env.MissionStartState.LIMIT_REACHED
        self.assertEqual(self.run(self.bridge, self.env, 21, 1), (None, "CONTRACT_LIMIT_REACHED"))
        self.assertEqual(self.run(self.bridge, self.env, 40, 1), (None, "NO_CONTRACT_ON_FIELD"))

    def test_collecting_needs_the_game_to_have_finished_it_successfully(self):
        m = self.env.missions[2]
        m.farmId, m.status = 1, self.env.MissionStatus.RUNNING
        self.assertEqual(self.run(self.bridge, self.env, 21, -1), (None, "CONTRACT_NOT_FINISHED"))
        m.status, m.finishState = self.env.MissionStatus.FINISHED, self.env.MissionFinishState.SUCCESS
        task, status, code = self.run(self.bridge, self.env, 21, -1)
        self.assertEqual((status, code), ("DONE", "CONTRACT_COLLECTED"))
        self.assertEqual(self.env.dismissed[1], 21)
        self.assertEqual(self.run(self.bridge, self.env, 22, -1), (None, "NO_CONTRACT_ON_FIELD"))    # not ours


class AutoDriveSellTests(unittest.TestCase):
    """ad_unload / ad_sell: AutoDrive's own combine unloader and deliver mode to the best
    paid sell point as AutoDrive finds it (ADTriggerManager getHighestPayingSellStation /
    getMarkerAtStation, the map marker of that waypoint), reached as the environment of
    its GetAvailableDestinations. Stubs in the pinned source's shapes only."""

    ENV = r"""
        local env = { started = {} }
        local AD = { MODE_DRIVETO = 1, MODE_DELIVERTO = 3, MODE_UNLOAD = 5 }
        local markers = { [4] = { id = 40, name = "Field 7" }, [9] = { id = 90, name = "Grain Elevator" } }
        function AD:GetAvailableDestinations() return markers end
        function AD:StartDriving(v, first, second)
            env.started[#env.started + 1] = { mode = v.mode, first = first, second = second }
            v.active = true
        end
        local triggers = { best = { name = "elevator" }, wp = 90 }
        function triggers:getHighestPayingSellStation(fillType) env.askedFillType = fillType; return self.best end
        function triggers:getMarkerAtStation(station, v) return self.wp end
        local graph = {}
        function graph:getMapMarkers() return markers end
        env.AutoDrive = AD
        env.getfenv = function(fn) if fn == AD.GetAvailableDestinations then return { ADTriggerManager = triggers, ADGraphManager = graph } end return {} end
        env.triggers = triggers
        env.FruitType = { UNKNOWN = 0 }
        env.FieldState = { new = function() return { update = function(s) s.fruitTypeIndex = 3 end } end }
        env.g_fruitTypeManager = { getFillTypeIndexByFruitTypeIndex = function(_, i) return 100 + i end }
        env.g_fieldManager = { fields = { { fieldId = 7, posX = 1, posZ = 2 } } }
        local trailer = { levels = { 0 } }
        function trailer:getFillUnits() return { {} } end
        function trailer:getFillUnitFillLevel(i) return self.levels[i] end
        function trailer:getFillUnitFillType(i) return 7 end
        local v = { ownerFarmId = 1, active = false, ad = {} }
        function v:raiseActive() self.raised = (self.raised or 0) + 1 end
        v.ad.stateModule = { getStartHelper = function() return false end, setMode = function(_, m) v.mode = m end,
                             isActive = function() return v.active end }
        function v:getIsEntered() return false end
        function v:getIsAIActive() return false end
        function v:getLastSpeed() return 0 end
        function v:getAttachedImplements() return { { object = trailer } } end
        function v:stopAutoDrive() self.active = false end
        env.v, env.trailer = v, trailer
        return env
    """

    def setUp(self):
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().print = lambda *a: None
        for name in ("FarmhandObserve.lua", "FarmhandHitch.lua"):
            self.lua.execute((ROOT / "mod" / name).read_text())
        self.bridge = self.lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        self.env = self.lua.execute(self.ENV)
        self.run = self.lua.eval("""function(B, env, op, marker, field)
            local task, code = B.start(env, { op = op, tractor = env.v, marker = marker, field = field, farmId = 1,
                                             owned = function() return true end })
            if task == nil then return nil, code end
            task.elapsed = 0
            return task, B.step(env, task, 16) end""")

    def test_the_unloader_waits_at_the_field_and_unloads_at_the_best_paid_sell_point(self):
        task, status, code = self.run(self.bridge, self.env, "ad_unload", 4, 7)
        self.assertEqual((status, code), ("DONE", "UNLOADING"))
        started = self.env.started[1]
        self.assertEqual((started.mode, started.first, started.second), (5, 4, 9))
        self.assertEqual(self.env.askedFillType, 103)                      # the field's crop
        self.assertEqual(self.bridge.handed[self.env.v], "drive")          # a stop still ends it

    def test_selling_delivers_the_cargo_to_the_best_paid_sell_point(self):
        self.assertEqual(self.run(self.bridge, self.env, "ad_sell", 0, 0), (None, "NOTHING_TO_SELL"))
        self.env.trailer.levels[1] = 5000
        task, status, code = self.run(self.bridge, self.env, "ad_sell", 0, 0)
        started = self.env.started[1]
        self.assertEqual((started.mode, started.first, started.second), (3, 9, 9))
        self.assertEqual(self.env.askedFillType, 7)                        # what it carries
        self.env.v.active = False
        self.assertEqual(self.bridge.step(self.env, task, 16), ("DONE", "AD_ENDED"))

    def test_no_sell_point_or_no_marker_there_starts_nothing(self):
        self.env.trailer.levels[1] = 5000
        self.env.triggers.wp = -1
        self.assertEqual(self.run(self.bridge, self.env, "ad_sell", 0, 0), (None, "AD_NO_MARKER_AT_SELL_POINT"))
        self.env.triggers.best = None
        self.assertEqual(self.run(self.bridge, self.env, "ad_unload", 4, 7), (None, "NO_SELL_POINT"))
        self.assertEqual(len(self.env.started), 0)


class PipeOverloadTests(unittest.TestCase):
    """pipe / overload: a standing combine emptied into the trailer under its pipe the way
    GIANTS does it (Pipe:setPipeState to an unloading state; automaticDischarge while
    powered: Motorized:startMotor). Stubs in GDN's shapes only."""

    ENV = r"""
        local env = { MotorState = { OFF = 0, STARTING = 1, ON = 2 }, NetworkUtil = { getObjectId = function() return 7 end } }
        local c = { ownerFarmId = 1, speed = 0, motor = 0, level = 9000, cap = 9000, activeRaised = 0 }
        c.spec_pipe = { currentState = 1, targetState = 1, unloadingStates = { [2] = true } }
        function c:getLastSpeed() return self.speed end
        function c:getIsAIActive() return false end
        function c:getIsPipeStateChangeAllowed(s) return true end
        function c:setPipeState(s) self.spec_pipe.targetState = s end
        function c:getCurrentPipeState() return self.spec_pipe.currentState end
        function c:getMotorState() return self.motor end
        function c:getCanMotorRun() return true end
        function c:startMotor() self.motor = 2 end
        function c:raiseActive() self.activeRaised = self.activeRaised + 1 end
        function c:getCurrentDischargeNode() return { fillUnitIndex = 2, node = 1 } end
        function c:getFillUnitFillLevel(i) return self.level end
        function c:getFillUnitCapacity(i) return self.cap end
        function c:getIsEntered() return self.entered == true end
        local rig = { entered = false }
        function rig:getIsEntered() return self.entered end
        local t = { levels = { 0 }, caps = { 20000 }, rootVehicle = rig }
        function t:getFillUnits() return { {} } end
        function t:getFillUnitFillLevel(i) return self.levels[i] end
        function t:getFillUnitCapacity(i) return self.caps[i] end
        env.c, env.t, env.rig = c, t, rig
        return env
    """

    def setUp(self):
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().print = lambda *a: None
        for name in ("FarmhandObserve.lua", "FarmhandHitch.lua"):
            self.lua.execute((ROOT / "mod" / name).read_text())
        self.bridge = self.lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        self.env = self.lua.execute(self.ENV)
        self.start = self.lua.eval("""function(B, env, op, direction)
            local task, code = B.start(env, { op = op, tractor = env.c, wagon = env.t, direction = direction, farmId = 1,
                                             owned = function() return true end })
            if task ~= nil then task.elapsed = 0 end
            return task, code end""")
        self.step = self.lua.eval("function(B, env, task, ms) task.elapsed = task.elapsed + ms; return B.step(env, task, ms) end")

    def test_a_takeover_or_a_stop_ends_the_overload_and_folds_the_pipe(self):
        # The player climbing into the combine or into the tractor under its pipe takes over;
        # either way, and on a stop, GIANTS' discharge ends with the pipe folded (state 1).
        for who in ("c", "rig", None):
            with self.subTest(who=who):
                self.env = self.lua.execute(self.ENV)
                self.env.c.spec_pipe.currentState = 2
                task, code = self.start(self.bridge, self.env, "overload", None)
                self.assertIsNotNone(task, code)
                self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16))[:1], (None,))
                if who is None:
                    self.lua.eval("function(B, env, task) B.abort(env, task, 'STOPPED') end")(self.bridge, self.env, task)
                else:
                    getattr(self.env, who).entered = True
                    self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16)), ("FAILED", "DRIVER_TOOK_OVER"))
                self.assertEqual(self.env.c.spec_pipe.targetState, 1)
                self.assertNotEqual(self.env.c.forceIsActive, True)

    def test_the_pipe_goes_out_powered_and_comes_back_in(self):
        c = self.env.c
        task, _ = self.start(self.bridge, self.env, "pipe", 1)
        self.assertEqual((c.spec_pipe.targetState, c.motor), (2, 2))         # unloading state, motor started
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16))[:1], (None,))
        c.spec_pipe.currentState = 2
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16)), ("DONE", "PIPE_OUT"))
        task, _ = self.start(self.bridge, self.env, "pipe", -1)
        c.spec_pipe.currentState = 1
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16)), ("DONE", "PIPE_IN"))
        c.speed = 4
        self.assertEqual(tuple(self.start(self.bridge, self.env, "pipe", 1))[1], "NOT_STOPPED")

    def test_overload_waits_for_giants_discharge_and_reports_how_it_ended(self):
        c, t = self.env.c, self.env.t
        self.assertEqual(tuple(self.start(self.bridge, self.env, "overload", 0))[1], "PIPE_NOT_OUT")
        c.spec_pipe.currentState = 2
        task, _ = self.start(self.bridge, self.env, "overload", 0)
        self.assertTrue(c.forceIsActive)
        c.level, t.levels[1] = 5000, 4000
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 1000))[:1], (None,))
        c.level, t.levels[1] = 0, 9000
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 1000)), ("DONE", "EMPTIED"))
        self.assertFalse(c.forceIsActive)
        c.level = 9000                                         # nothing flows: not under the pipe
        task, _ = self.start(self.bridge, self.env, "overload", 0)
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16000)), ("FAILED", "NOT_OVERLOADING"))


class GiantsDeliverTests(unittest.TestCase):
    """deliver: GIANTS' own Deliver helper (AIJobDeliver) to the best paid SellingStation its
    AI can unload at for what the rig carries, no looping; DONE once its job ended at rest.
    Stubs in GDN's shapes only."""

    ENV = r"""
        local env = { AIJobType = { DELIVER = 4 }, started = {} }
        local Selling, Other = {}, {}
        local function station(name, kind, price, ai)
            return { name = name, kind = kind, price = price, ai = ai,
                     isa = function(self, c) return self.kind == c end,
                     getIsFillTypeAISupported = function(self, ft) return self.ai end,
                     getEffectiveFillTypePrice = function(self, ft) return self.price end,
                     getName = function(self) return self.name end }
        end
        env.SellingStation = Selling
        local stations = { [11] = station("silo", Other, 9, true), [12] = station("mill", Selling, 1.2, true),
                           [13] = station("elevator", Selling, 1.5, true), [14] = station("port", Selling, 2.0, false) }
        env.NetworkUtil = { getObject = function(id) return stations[id] end, getObjectId = function() return 5 end }
        local job = { unloadingStationParameter = { unloadingStationIds = { 11, 12, 13, 14 } },
                      loopingParameter = { setIsLooping = function(self, b) self.looping = b end } }
        function job.unloadingStationParameter:setUnloadingStation(st) self.chosen = st end
        function job:getIsAvailableForVehicle(v) return true end
        function job:applyCurrentState(v, mission, farmId, direct) self.applied = true end
        function job:setValues() end
        function job:validate(farmId) return true end
        env.job = job
        env.g_currentMission = { aiJobTypeManager = { createJob = function(_, t) return t == 4 and job or nil end },
                                 aiSystem = { startJob = function(_, j, farmId) env.started[#env.started + 1] = j end } }
        local trailer = { levels = { 8000 } }
        function trailer:getFillUnits() return { {} } end
        function trailer:getFillUnitFillLevel(i) return self.levels[i] end
        function trailer:getFillUnitFillType(i) return 7 end
        local v = { ownerFarmId = 1, ai = false, speed = 0 }
        function v:getIsEntered() return false end
        function v:getIsAIActive() return self.ai end
        function v:getLastSpeed() return self.speed end
        function v:getAttachedImplements() return { { object = trailer } } end
        env.v, env.trailer = v, trailer
        return env
    """

    def setUp(self):
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().print = lambda *a: None
        for name in ("FarmhandObserve.lua", "FarmhandHitch.lua"):
            self.lua.execute((ROOT / "mod" / name).read_text())
        self.bridge = self.lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        self.env = self.lua.execute(self.ENV)
        self.start = self.lua.eval("""function(B, env)
            local task, code = B.start(env, { op = "deliver", tractor = env.v, farmId = 1, owned = function() return true end })
            if task ~= nil then task.elapsed = 0 end
            return task, code end""")
        self.step = self.lua.eval("function(B, env, task, ms) task.elapsed = task.elapsed + ms; return B.step(env, task, ms) end")

    def test_the_best_paid_selling_station_giants_ai_can_unload_at_no_looping(self):
        task, code = self.start(self.bridge, self.env)
        self.assertIsNotNone(task, code)
        self.assertEqual(self.env.job.unloadingStationParameter.chosen.name, "elevator")   # not the port (no AI), not the silo
        self.assertEqual(self.env.job.loopingParameter.looping, False)
        self.assertEqual(len(self.env.started), 1)
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16))[:1], (None,))   # not yet seen working
        self.env.v.ai = True
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16))[:1], (None,))
        self.env.v.ai = False
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16)), ("DONE", "AI_STOPPED"))

    def test_nothing_to_sell_moves_nothing(self):
        self.env.trailer.levels[1] = 0
        self.assertEqual(tuple(self.start(self.bridge, self.env))[1], "NOTHING_TO_SELL")
        self.assertEqual(len(self.env.started), 0)

    def test_storing_takes_the_farms_own_storage_nearest_the_rig_never_a_sell_point(self):
        # direction -1: a silo's own unloading station (its owningPlaceable's spec_silo, as
        # PlaceableSilo:onLoad sets it) on a placeable the farm owns, taking the cargo; the
        # nearest to the rig. A neighbour's silo, a pig feeder (no silo) and every sell point
        # are skipped.
        self.lua.execute("""local env = ...
            env.getWorldTranslation = function(n) return n.x, 0, n.z end
            env.v.rootNode = { x = 0, z = 0 }
            local Other = env.NetworkUtil.getObject(11).kind
            local function own(st, farm, x, notSilo)
                st.owningPlaceable = { ownerFarmId = farm, rootNode = { x = x, z = 0 },
                                       spec_silo = (not notSilo) and { unloadingStation = st } or nil }
                return st
            end
            own(env.NetworkUtil.getObject(11), 1, 300)
            for id, x in pairs({ [12] = 5, [13] = 6, [14] = 7 }) do own(env.NetworkUtil.getObject(id), 1, x) end
            local near = { name = "bin", kind = Other, price = 0, ai = true, isa = function(self, c) return self.kind == c end,
                           getIsFillTypeAISupported = function() return true end, getName = function(self) return self.name end }
            own(near, 1, 80)
            local neighbour = { name = "their silo", kind = Other, price = 0, ai = true, isa = near.isa,
                                getIsFillTypeAISupported = near.getIsFillTypeAISupported, getName = near.getName }
            own(neighbour, 2, 10)
            local feeder = { name = "pig feeder", kind = Other, ai = true, isa = near.isa,
                             getIsFillTypeAISupported = near.getIsFillTypeAISupported, getName = near.getName }
            own(feeder, 1, 3, true)
            local get = env.NetworkUtil.getObject
            env.NetworkUtil.getObject = function(id)
                if id == 15 then return near elseif id == 16 then return neighbour elseif id == 17 then return feeder end
                return get(id) end
            for _, id in ipairs({ 15, 16, 17 }) do table.insert(env.job.unloadingStationParameter.unloadingStationIds, id) end""", self.env)
        store = self.lua.eval("""function(B, env)
            local task, code = B.start(env, { op = "deliver", tractor = env.v, farmId = 1, direction = -1,
                                              owned = function() return true end })
            return task, code end""")
        task, code = store(self.bridge, self.env)
        self.assertIsNotNone(task, code)
        self.assertEqual(self.env.job.unloadingStationParameter.chosen.name, "bin")
        self.lua.execute("local env = ...; env.job.unloadingStationParameter.unloadingStationIds = { 12, 13, 16, 17 }", self.env)
        self.assertEqual(tuple(store(self.bridge, self.env))[1], "NO_STORAGE")


class FollowTests(unittest.TestCase):
    """follow: FollowMe's registered vehicle functions (initiateFollowVehicle, getIsFollowVehicleActive,
    getCanStartFollowVehicle; FollowVehicle.lua 38-75, 556-571), the leader the vehicle the player
    sits in; ended by FollowMe or by GIANTS' stopCurrentAIJob (AIJobVehicle 431). Stubs only."""

    ENV = r"""
        local env = { calls = {} }
        local follower = { ownerFarmId = 1, active = false, can = true, entered = false }
        function follower:getIsEntered() return self.entered end
        function follower:getIsFollowVehicleActive() return self.active end
        function follower:getCanStartFollowVehicle() return self.can end
        function follower:initiateFollowVehicle(leader) env.calls[#env.calls + 1] = "follow"; self.leader = leader; self.active = true end
        function follower:stopCurrentAIJob() env.calls[#env.calls + 1] = "stopAI"; self.active = false end
        function follower:getLastSpeed() return 0 end
        local car = { entered = true }
        function car:getIsEntered() return self.entered end
        function car:addTrailDrop() end
        env.follower, env.car = follower, car
        env.g_currentMission = { vehicleSystem = { vehicles = { follower, car } } }
        env.AIMessageSuccessStoppedByUser = { new = function() return {} end }
        return env
    """

    def setUp(self):
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.bridge = self.lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        self.bridge.Hitch = self.lua.execute((ROOT / "mod/FarmhandHitch.lua").read_text())
        self.start = self.lua.eval("""function(B, env)
            local task, code = B.start(env, { op = 'follow', tractor = env.follower, farmId = 1,
                                              owned = function() return true end })
            if task ~= nil then task.elapsed = 0 end
            return task, code end""")
        self.step = self.lua.eval("function(B, env, task, ms) task.elapsed = task.elapsed + ms; return B.step(env, task, ms) end")

    def test_it_follows_the_players_vehicle_until_followme_ends(self):
        env = self.lua.execute(self.ENV)
        task, code = self.start(self.bridge, env)
        self.assertIsNotNone(task, code)
        self.assertEqual(list(env.calls.values()), ["follow"])
        self.assertTrue(self.lua.eval("function(a, b) return a == b end")(env.follower.leader, env.car))
        self.assertIsNone(self.step(self.bridge, env, task, 1000)[0])            # following
        env.follower.active = False                                          # the player ended it
        self.assertEqual(tuple(self.step(self.bridge, env, task, 100)), ("DONE", "FOLLOW_ENDED"))

    def test_refusals_and_stop(self):
        for spoil, reason in (("env.car.entered = false", "PLAYER_NOT_IN_VEHICLE"),
                              ("env.car.addTrailDrop = nil", "LEADER_UNSUPPORTED"),
                              ("env.follower.can = false", "FOLLOW_NOT_ALLOWED"),
                              ("env.follower.initiateFollowVehicle = nil", "FOLLOWME_UNAVAILABLE"),
                              ("env.follower.entered = true", "SEATED_IN_FOLLOWER")):
            with self.subTest(reason):
                env = self.lua.execute(self.ENV)
                self.lua.execute("local env = ...; " + spoil, env)
                self.assertEqual(self.start(self.bridge, env)[1], reason)
                self.assertNotIn("follow", list(env.calls.values()))
        env = self.lua.execute(self.ENV)
        task, _ = self.start(self.bridge, env)
        self.lua.eval("function(B, env, task) B.abort(env, task) end")(self.bridge, env, task)
        self.assertIn("stopAI", list(env.calls.values()))


class CatalogFrameTests(unittest.TestCase):
    def test_the_catalog_goes_in_small_pages(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        observe = lua.execute((ROOT / "mod/FarmhandObserve.lua").read_text())
        lua.execute("""local O = ...
            O.catalog = { done = true, truncated = false, items = O.array() }
            for i = 1, 120 do O.catalog.items[i] = { id = i, name = "item", options = O.array() } end""", observe)
        lines = list(observe.catalogFrame().values())
        self.assertEqual(len(lines), 3)                                     # 50, 50, 20
        frames = [json.loads(line) for line in lines]
        self.assertEqual([(f["page"], f["pages"], len(f["items"])) for f in frames], [(0, 3, 50), (1, 3, 50), (2, 3, 20)])


class SaveTests(unittest.TestCase):
    """The game's own save; complete only once GIANTS' saveSavegame ran to its end."""

    def test_save_is_done_when_giants_save_function_completed(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        bridge = lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        env = lua.execute("""
            local env = { Utils = { appendedFunction = function(old, new)
                return function(...) old(...); new(...) end end } }
            env.saved = 0
            env.FSBaseMission = { saveSavegame = function(self, flag) env.saved = env.saved + 1 end }
            env.g_currentMission = { saveSavegame = function(self, flag) env.FSBaseMission.saveSavegame(self, flag) end }
            return env
        """)
        spec = lua.eval("function() return { owned = function() return true end } end")()
        start = lua.eval("function(B, env, spec) return B.start(env, setmetatable({ op = 'save' }, { __index = spec })) end")
        code = lua.eval("function(B, env, spec) local t, c = B.start(env, setmetatable({ op = 'save' }, { __index = spec })); return c end")
        self.assertEqual(code(bridge, env, spec), "SAVE_NOT_WATCHED")             # nothing to judge it by
        bridge.watchSaves(env)
        task = start(bridge, env, spec)
        self.assertEqual(tuple(bridge.step(env, task, 16)), ("DONE", "SAVED"))
        self.assertEqual(env.saved, 1)
