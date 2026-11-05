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
        -- The HUD's speed limits (StateModule.lua 1180-1214: steps of 1, floor 2).
        speedLimit = 103, fieldSpeedLimit = 30,
        getSpeedLimit = function(self) return self.speedLimit end,
        getFieldSpeedLimit = function(self) return self.fieldSpeedLimit end,
        decreaseSpeedLimit = function(self) if self.speedLimit > 2 then self.speedLimit = self.speedLimit - 1 end end,
        increaseSpeedLimit = function(self) self.speedLimit = self.speedLimit + 1 end,
        decreaseFieldSpeedLimit = function(self) if self.fieldSpeedLimit > 2 then self.fieldSpeedLimit = self.fieldSpeedLimit - 1 end end,
        increaseFieldSpeedLimit = function(self) self.fieldSpeedLimit = self.fieldSpeedLimit + 1 end,
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
    def test_farm_drive_reports_no_route_when_no_section_was_created(self):
        self.bridge.Drive = self.lua.table_from({
            "step": self.lua.eval("function() return 'NO_ROUTE' end"),
            "abort": self.lua.eval("function() end"),
        })
        session = self.lua.table_from({"targetX": 30, "targetZ": 40, "path": None})
        task = self.lua.table_from({"op": "farm_drive", "session": session,
                                    "spec": self.lua.table_from({"tractor": self.w.tractor}),
                                    "elapsed": 0})
        status, code = self.bridge.step(self.w.env, task, 100)
        self.assertEqual((status, code), ("FAILED", "NO_ROUTE"))

    def test_inert_until_a_companion_session_exists(self):
        self.send(1, "navigate", self.POSE)
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

    def test_unknown_vehicle_never_moves(self):
        self.adopt()
        self.send(2, "navigate", {**self.POSE, "tractor": 99})
        self.assertEqual(self.results(), [(2, "REJECTED", "UNKNOWN_VEHICLE")])
        self.assertEqual(self.calls(), [])

    def test_stale_sessions_and_replayed_ids_are_never_executed(self):
        self.adopt()
        self.send(1, "navigate", self.POSE, session="old-session")
        self.assertEqual(self.results(session="old-session"), [(1, "REJECTED", "STALE_SESSION")])
        self.send(5, "stop", {"tractor": 11})
        self.W.run(self.w, 200)
        self.send(5, "navigate", self.POSE)                                  # replayed id
        self.send(4, "navigate", self.POSE)                                  # lower id
        self.assertEqual(self.calls(), [])
        self.assertEqual([r for r in self.results() if r[0] in (4, 5)],
                         [(5, "ACCEPTED", "ACCEPTED"), (5, "DONE", "STOPPED")])

    def test_one_command_at_a_time_and_stop_pre_empts(self):
        self.adopt()
        self.w.gotoStarts = True
        self.send(1, "navigate", self.POSE)
        self.send(2, "navigate", self.POSE)
        self.assertIn((2, "REJECTED", "BUSY"), self.results())
        self.w.speed = 4.0
        self.send(3, "stop", {"tractor": 11})
        self.assertIn((1, "FAILED", "STOPPED"), self.results())
        self.assertIn("stopCurrentAIJob", self.calls())
        self.assertEqual(self.results(3), [(3, "ACCEPTED", "ACCEPTED")])   # not yet at rest
        self.w.speed = 0.0
        self.W.run(self.w, 200)
        self.assertEqual(self.results(3)[-1], (3, "DONE", "STOPPED"))

    def test_a_pre_empted_motion_logs_its_record(self):
        # Live 902c1b52e05742ec-1: STOP ended a LOAD braked, but its speed and envelope record
        # was never logged. Every motion's abort logs it as any other end.
        real = self.bridge.Hitch
        calls = self.lua.table()
        fake = self.lua.eval("""function(real, calls)
            local t = setmetatable({}, { __index = real })
            t.abort = function(env, s, code) table.insert(calls, "abort " .. code) end
            for _, n in ipairs({ "describe", "describePull", "describeUnpark", "describeLoad", "describeBackOff",
                                 "describeAlongside" }) do t[n] = function() return n end end
            t.describeSpeed = function() return "speed[x]" end
            t.describeEnvelope = function() return "envelope[x]" end
            t.speedTrace = function() return { "16:trace" } end
            return t
        end""")(real, calls)
        self.bridge.Hitch = fake
        try:
            for op in ("maneuver", "align", "pull_clear", "unpark", "load", "backoff", "alongside"):
                with self.subTest(op=op):
                    self.printed.clear()
                    task = self.lua.table_from({"op": op, "session": self.lua.table_from({})})
                    self.bridge.abort(self.w.env, task, "STOPPED")
                    ends = [p for p in self.printed if f"{op} end STOPPED" in p]
                    self.assertEqual(len(ends), 1, self.printed)
                    self.assertIn("speed[x]", ends[0])
                    self.assertIn("envelope[x]", ends[0])
                    self.assertTrue(any(f"{op} trace 1 16:trace" in p for p in self.printed))
            self.assertEqual(len(list(calls.values())), 7)
        finally:
            self.bridge.Hitch = real

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
        self.w.gotoStarts = True
        self.send(1, "navigate", self.POSE)
        self.W.run(self.w, 11000)                                          # no new beat
        self.assertIn((1, "FAILED", "COMPANION_LOST"), self.results())
        self.assertIn("stopCurrentAIJob", self.calls())
        self.w.lines = self.lua.table()
        self.W.run(self.w, 2000)                                           # same file: not re-adopted
        snapshots = [f for f in self.frames() if f.get("event") == "snapshot"]
        self.assertTrue(snapshots and all(f["session"] is None for f in snapshots))

    def test_beating_companion_keeps_its_session(self):
        self.adopt()
        self.w.gotoStarts = True
        self.send(1, "navigate", self.POSE)
        self.beating(12000)
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])
        self.assertNotIn("stopCurrentAIJob", self.calls())

    def test_new_session_stops_the_old_ones_work(self):
        self.adopt("s1")
        self.w.gotoStarts = True
        self.send(1, "navigate", self.POSE)
        self.adopt("s2")
        self.assertIn((1, "FAILED", "SESSION_CHANGED"), self.results())
        self.assertIn("stopCurrentAIJob", self.calls())
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

    def agent(self, script):
        """GIANTS' navigation agent on the tractor (AIDrivable createAgent/deleteAgent, the engine's
        setVehicleNavigationAgentTarget / getVehicleNavigationAgentNextCurvature); `script` gives
        each frame's (curvature, m/s, state) until it runs out, then the last one."""
        return self.lua.execute(r"""
            local w, script = ...
            local env, t = w.env, w.tractor
            env.AIDrivable = { STATES = { [1] = "driving", [2] = "blocked", [3] = "planning",
                                          [4] = "not_reachable", [5] = "target_reached" } }
            t.spec_aiDrivable = { agentInfo = { isValid = true }, poseData = {} }
            t.spec_drivable = { cruiseControl = { speed = 30 } }
            t.lastSpeedReal = 0
            function t:createAgent() table.insert(w.calls, "createAgent"); self.spec_aiDrivable.agentId = 7 end
            function t:deleteAgent() table.insert(w.calls, "deleteAgent"); self.spec_aiDrivable.agentId = nil end
            function t:updateAIAgentPoseData() end
            function t:getIsMotorStarted() return true end
            function t:setCruiseControlMaxSpeed(v) table.insert(w.calls, "cruise:" .. v) end
            function t:setCruiseControlState() end
            env.getTerrainHeightAtWorldPos = function() return 0 end
            env.g_currentMission.terrainRootNode = 1
            env.setVehicleNavigationAgentTarget = function(id, x, y, z, dx, dy, dz)
                table.insert(w.calls, string.format("target:%d:%.0f,%.0f", id, x, z)) end
            local n = 0
            env.getVehicleNavigationAgentNextCurvature = function()
                n = n + 1
                local f = script[math.min(n, #script)]
                return f[1], f[2], f[3]
            end
            env.AIVehicleUtil = env.AIVehicleUtil or {}
            env.AIVehicleUtil.driveAlongCurvature = function(v, dt, k, kmh)
                w.drove = (w.drove or 0) + 1; w.kmh = kmh
                w.z = w.z + (w.moves and 0.05 or 0)
            end
            env.AIVehicleUtil.driveToPoint = env.AIVehicleUtil.driveToPoint or function() end
        """, self.w, self.lua.table_from([self.lua.table_from(f) for f in script]))

    def test_navigate_drives_giants_agent_itself_and_ends_where_it_says(self):
        self.adopt()
        self.agent([(0.01, 20.0, 1)] * 30 + [(0, 0, 5)])
        self.w.moves = True
        self.send(1, "navigate", self.POSE)
        self.W.run(self.w, 4000)
        calls = self.calls()
        self.assertIn("createAgent", calls)
        self.assertTrue(any(c.startswith("target:7:") for c in calls), calls)
        self.assertNotIn("startJob", calls)                       # no GIANTS job, no hired helper
        self.assertGreater(self.w.drove, 20)
        self.assertLessEqual(self.w.kmh, 30)                      # the vehicle's own cruise speed caps it
        self.assertEqual(self.results(1)[-1], (1, "DONE", "GOTO_ENDED"))
        self.assertIn("deleteAgent", calls)
        self.assertIn("cruise:30", calls)

    def test_navigate_on_the_agent_ends_stuck_when_driven_but_unmoved_or_blocked(self):
        for script, moves, code in (([(0.0, 10.0, 1)], False, "NAVIGATE_STUCK_AT_START"),
                                    ([(0.0, 0.0, 2)], False, "NAVIGATE_BLOCKED_AT_START")):
            with self.subTest(code=code):
                self.w = self.W.new()
                self.adopt()
                self.agent(script)
                self.w.moves = moves
                self.send(1, "navigate", self.POSE)
                self.beating(25000)
                self.assertEqual(self.results(1)[-1], (1, "FAILED", code))
                self.assertIn("deleteAgent", self.calls())

    def test_stage_drives_the_agent_too_and_a_start_it_cannot_leave_ends_it(self):
        # Live a34770b22e2a46fa-2: planning and driving by turns, unmoved 33 s: ended in 8 s now.
        for script, moves, result in (([(0.0, 8.0, 1)] * 10 + [(0, 0, 5)], True, ("DONE", "STAGE_ENDED")),
                                      ([(0.0, 8.0, 1), (0.0, 0.0, 3)] * 40, False, ("DONE", "STAGE_ENDED"))):
            with self.subTest(result=result):
                self.w = self.W.new()
                self.adopt()
                self.agent(script)
                self.w.moves = moves
                self.send(1, "stage", self.POSE)
                self.beating(12000)
                self.assertEqual(self.results(1)[-1][1:], result)
                self.assertNotIn("startJob", self.calls())
                self.assertIn("deleteAgent", self.calls())

    def test_a_stop_ends_farmhands_drive_on_the_agent(self):
        self.adopt()
        self.agent([(0.0, 10.0, 1)])
        self.w.moves = True
        self.send(1, "navigate", self.POSE)
        self.W.run(self.w, 1000)
        self.send(2, "stop", {"tractor": 11})
        self.W.run(self.w, 300)
        self.assertEqual(self.results(2)[-1], (2, "DONE", "STOPPED"))
        self.assertIn("deleteAgent", self.calls())

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

    def test_a_loaded_rig_unmoved_at_start_yields_quickly_to_recovery(self):
        self.adopt()
        self.w.gotoStarts = True
        self.bridge.beltedCargo = self.lua.eval("""function()
            return {{removeAIVehicleObstacle=function() end,
                     setAIVehicleObstacleStateDirty=function() end}}
        end""")
        self.send(1, "navigate", self.POSE)
        self.beating(7000)
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "NAVIGATE_STUCK_AT_START"))

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
        for op, args in (("navigate", self.POSE), ("stage", self.POSE)):
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
            "1:Farmhand: stopped short - something new"])

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
        self.assertIn("4:Farmhand: close this window, then press Left Alt + N for Farmhand's settings", self.notices())
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

    def test_a_helper_holding_for_the_player_waits_and_says_so_anything_else_is_blocked(self):
        # Live 87ec310879a546ea-1: GIANTS' helper held for the player 6 m ahead (its collision
        # triggers count a player); Farmhand ended it FIELDWORK_STALLED after 5 min.
        for near, result in ((True, (1, "ACCEPTED", "ACCEPTED")), (False, (1, "FAILED", "FIELDWORK_BLOCKED"))):
            with self.subTest(near=near):
                self.w = self.W.new()
                self.adopt()
                self.fieldwork_stubs()
                self.w.gotoStarts = True
                self.bridge.BLOCKED_START_MS = 2000
                self.lua.execute("""local w, near = ...
                    function w.tractor:getAIFieldWorkerIsBlocked() return true end
                    w.env.g_localPlayer = { getIsInVehicle = function() return false end,
                        getPosition = function() if near then return 1, 0, 6 end return 300, 0, 0 end }""",
                                 self.w, near)
                self.send(1, "fieldwork", self.FIELD)
                self.beating(5000)
                self.assertEqual(self.results(1)[-1], result)
                told = [n for n in self.w.notices.values() if "stopped for you" in n]
                self.assertEqual(len(told), 1 if near else 0)

    def test_a_helper_whose_drive_to_the_field_never_finds_a_way_ends_not_reachable(self):
        # Live e3e4f50db5174dc2-2: "planning" for 5 min, unmoved, then FIELDWORK_STALLED.
        self.adopt()
        self.fieldwork_stubs()
        self.w.gotoStarts = True
        self.lua.execute("""local w = ...
            w.env.AIDrivable = { STATES = { [3] = "planning" } }
            w.tractor.spec_aiDrivable = { lastState = 3, distanceToTarget = 50 }
            function w.tractor:getIsFieldWorkActive() return false end""", self.w)
        self.send(1, "fieldwork", self.FIELD)
        self.beating(29000)
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])
        self.beating(3000)
        self.assertIn("stopCurrentAIJob", self.calls())
        self.W.run(self.w, 300)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "NOT_REACHABLE"))

    def test_a_bounded_area_is_measured_often_while_working_and_stopped_ahead_of_its_rate(self):
        # Live fb3522a3e1d64f75-1: 0 m2 at one 3 s check, 1244 m2 of 400 at the next.
        self.adopt()
        self.fieldwork_stubs()
        self.w.gotoStarts = True
        self.lua.execute("""local w, B = ...
            function w.tractor:getIsFieldWorkActive() return true end
            B.Observe.fieldCache = B.Observe.fieldCache or {}
            B.Observe.refreshField = function(env, id)
                B.Observe.fieldCache[id] = { record = { ground = { STUBBLE = 1.0 }, total_m2 = 10000 }, age = 0 }
            end
            w.worked = 0
            B.Observe.fieldShares = function()
                if w.aiActive then w.worked = w.worked + 30 end      -- 30 m2 a reading
                local share = w.worked / 10000
                return { ground = { STUBBLE = 1 - share, PLOWED = share }, total_m2 = 10000 }
            end""", self.w, self.bridge)
        self.send(1, "fieldwork", {**self.FIELD, "area": 400})
        self.beating(6000)
        self.assertEqual(self.results(1)[-1], (1, "DONE", "AREA_REACHED"))
        self.assertGreaterEqual(self.w.worked, 0.97 * 400)
        self.assertLess(self.w.worked, 400 + 30)                # at most one reading's work over

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

    def test_a_pull_clear_refused_for_the_player_may_be_tried_again_inside_its_window(self):
        # Live cc1d2a73fb8243fc-1: refused PULL_CLEAR_PERSON, the retry 4 s later NOT_JUST_DETACHED.
        self.detached()
        fake = self.scripted_pull(["CLEAR"])
        self.lua.execute("""local F, calls = ...
            local made = F.newPullClear
            local refused = false
            F.newPullClear = function(env, args)
                if not refused then refused = true; table.insert(calls, "refused"); return nil, "PULL_CLEAR_PERSON" end
                return made(env, args)
            end""", fake, self.w.calls)
        self.send(2, "pull_clear", self.PAIR)
        self.assertEqual(self.results(2)[-1], (2, "FAILED", "PULL_CLEAR_PERSON"))
        self.W.run(self.w, 3000)
        self.send(3, "pull_clear", self.PAIR)
        self.W.run(self.w, 300)
        self.assertEqual(self.results(3)[-1], (3, "DONE", "CLEAR"))
        self.send(4, "pull_clear", self.PAIR)                   # one pull-clear per detach still
        self.assertEqual(self.results(4)[-1], (4, "FAILED", "NOT_JUST_DETACHED"))

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

    def test_fold_for_transport_goes_away_from_the_end_giants_calls_unfolded(self):
        # Live ef3e350fd9fb4405-1: the plow left unfolded, LOAD refused at the ramps.
        for turn_on, expected in ((1, "setFoldDirection:-1"), (-1, "setFoldDirection:1")):
            with self.subTest(turn_on=turn_on):
                self.w = self.W.new()
                w = self.transport()
                w.deck.spec_foldable.turnOnFoldDirection = turn_on
                self.send(1, "fold", {"tractor": 12, "direction": 2})
                self.assertIn(expected, self.calls())

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
                                        ([], "LOCAL_PATH_HITS_WAGON", ("FAILED", "LOCAL_PATH_HITS_WAGON"))):
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

    def test_an_escort_runs_beside_other_machines_ops_and_only_its_own_machines_stop_it(self):
        # FINAL_FARMHAND_PRODUCT_SPEC.md 4: many machines at once, one controller per machine. The grain
        # cart (tractor 11) under the combine (12) runs on while a stop for another machine (14)
        # runs and ends; an op on the cart is BUSY; a stop for the combine pre-empts the escort.
        self.lua.execute("""local w, Hitch = ...
            local function vehicle(id)
                local v = { ownerFarmId = 1, rootNode = "root" .. id }
                v.rootVehicle = v
                function v:getLastSpeed() return 0 end
                function v:getIsAIActive() return false end
                function v:getIsEntered() return false end
                return v
            end
            w.combine, w.other = vehicle(12), vehicle(14)
            local get = w.env.NetworkUtil.getObject
            w.env.NetworkUtil.getObject = function(id)
                if id == 12 then return w.combine elseif id == 14 then return w.other end
                return get(id)
            end
            w.escortSteps = 0
            Hitch.newEscort = function(env, args)
                table.insert(w.calls, "newEscort")
                return { tractor = args.tractor, combine = args.combine }
            end
            Hitch.escortStep = function() w.escortSteps = w.escortSteps + 1; return "CONTINUE" end
            Hitch.abort = function(env, s, code) table.insert(w.calls, "abort:" .. code) end
        """, self.w, self.hitch)
        self.adopt()
        self.send(1, "escort", {"tractor": 11, "wagon": 12})
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])
        self.send(2, "navigate", {**self.POSE, "tractor": 11})
        self.assertIn((2, "REJECTED", "BUSY"), self.results())
        self.send(3, "stop", {"tractor": 14})
        self.W.run(self.w, 300)
        self.assertEqual(self.results(3), [(3, "ACCEPTED", "ACCEPTED"), (3, "DONE", "STOPPED")])
        self.assertEqual(self.results(1), [(1, "ACCEPTED", "ACCEPTED")])          # still running
        steps = self.w.escortSteps
        self.W.run(self.w, 300)
        self.assertGreater(self.w.escortSteps, steps)
        self.send(4, "stop", {"tractor": 12})
        self.assertIn((1, "FAILED", "STOPPED"), self.results())
        self.assertIn("abort:STOPPED", self.calls())
        self.W.run(self.w, 300)
        self.assertEqual(self.results(4)[-1], (4, "DONE", "STOPPED"))

    def test_a_sale_goes_to_the_station_of_the_farms_harvest_contract_for_that_crop(self):
        # GIANTS credits a delivery to a running harvest mission of the farm at that station
        # (SellingStation:addFillLevelFromTool, HarvestMission:fillSold); the best price elsewhere pays
        # nothing toward the contract.
        pick = self.lua.eval("""function(B)
            local function station(name, price, missions)
                local s = { name = name, missions = missions }
                function s:getIsFillTypeAISupported() return true end
                function s:isa(c) return c == "Selling" end
                function s:getEffectiveFillTypePrice() return price end
                function s:getName() return name end
                return s
            end
            local contract = { fillSold = function() end, fillTypeIndex = 7, farmId = 1 }
            local other = { fillSold = function() end, fillTypeIndex = 9, farmId = 1 }
            local stations = { [1] = station("dear", 2.0, {}), [2] = station("contract", 1.0, { [contract] = contract }),
                               [3] = station("otherCrop", 1.5, { [other] = other }) }
            local env = { SellingStation = "Selling", NetworkUtil = { getObject = function(id) return stations[id] end } }
            local job = { unloadingStationParameter = { unloadingStationIds = { 1, 2, 3 } } }
            local best = B.deliverStation(env, { direction = 1, farmId = 1 }, job, 7, nil)
            local plain = B.deliverStation(env, { direction = 1, farmId = 1 }, job, 8, nil)
            return best:getName(), plain:getName()
        end""")
        self.assertEqual(tuple(pick(self.bridge)), ("contract", "dear"))

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

    def test_a_dropped_session_ends_the_takeover_watch_and_releases_powered_combines(self):
        # Review 2026-11-04: armed stayed set, so a drive the player made meanwhile ended the next
        # session's first job DRIVER_TOOK_OVER; a combine kept powered for an overload stayed so.
        self.adopt()
        combine = self.lua.table(forceIsActive=True)
        self.lua.execute("""local B, H, t, c = ...
            H.armed[t] = true; H.takeovers[t] = true
            B.powered[c] = true""", self.bridge, self.hitch, self.w.tractor, combine)
        self.W.run(self.w, 11000)                                        # no heartbeat: COMPANION_LOST
        self.assertIsNone(self.hitch.armed[self.w.tractor])
        self.assertIsNone(self.hitch.takeovers[self.w.tractor])
        self.assertFalse(combine.forceIsActive)
        self.assertIsNone(self.bridge.powered[combine])

    def test_a_lost_companion_aborts_the_maneuver(self):
        self.adopt()
        self.scripted_maneuver(["CONTINUE"])
        self.send(1, "maneuver", self.MANEUVER)
        self.W.run(self.w, 11000)
        self.assertIn((1, "FAILED", "COMPANION_LOST"), self.results())
        self.assertIn("abort:STOPPED", self.calls())          # braked and latched, as a stop
        self.assertIsNone(self.bridge.lastManeuver)

    def test_a_hitch_step_passes_through_scenery_only_with_the_players_setting(self):
        # AGENTS.md 10: the hitch steps' old building ghosting is the player's opt-in only; the
        # command carries it (gh="1"), and without it the step keeps every collision.
        self.adopt()
        fake = self.scripted_maneuver(["CONTINUE"])
        ghosted = self.lua.execute("""
            local F, w = ...
            local calls = {}
            w.env.localDirectionToWorld = function() return 0, 0, 1 end
            F.routeFootprint = function() return {} end
            F.staticNodesNear = function() return {41, 42} end
            F.ghost = function(env, tractor, why, nodes, permitted)
                calls[#calls + 1] = why .. ":" .. #nodes .. ":" .. tostring(permitted)
                return true, #nodes
            end
            return calls
        """, fake, self.w)
        command = 0
        for args, expected in ((self.MANEUVER, []), ({**self.MANEUVER, "ghost": 0}, []),
                               ({**self.MANEUVER, "ghost": 1}, ["maneuver:2:true"])):
            command += 1
            self.send(command, "maneuver", args)
            self.assertEqual(self.results(command)[0], (command, "ACCEPTED", "ACCEPTED"))
            self.assertEqual(list(ghosted.values()), expected, args)
            command += 1
            self.send(command, "stop", {"tractor": 11})
            self.W.run(self.w, 200)
            self.assertEqual(self.results(command)[-1], (command, "DONE", "STOPPED"))

    def test_a_farm_drive_carries_the_players_scenery_setting_to_the_drive(self):
        self.adopt()
        seen = self.lua.execute("""
            local Bridge = ...
            local seen = {}
            Bridge.Drive = { begin = function(env, args) seen[#seen + 1] = tostring(args.ghost); return nil, "NO_ROUTE" end,
                             step = function() return "NO_ROUTE" end, abort = function() end }
            return seen
        """, self.bridge)
        drive = {"tractor": 11, "x": 5.0, "z": 5.0, "area": 0.0, "yaw": 10.0}
        for command, extra in enumerate(({}, {"ghost": 1}, {"ghost": 0}), start=1):
            self.send(command, "farm_drive", {**drive, **extra})
            self.assertEqual(self.results(command)[-1], (command, "FAILED", "NO_ROUTE"))
        self.assertEqual(list(seen.values()), ["false", "true", "false"])

    def test_the_player_is_told_in_game_when_scenery_ghosting_starts(self):
        self.hitch.onGhost(self.w.tractor, "NEAR_BUILDINGS", 2)        # Bridge:open wired it
        self.assertIn("4:Farmhand: passing through scenery beside the rig (your setting); "
                      "its collision returns once clear", list(self.w.notices.values()))

    def test_maneuver_notice_texts(self):
        cases = {"WORKING_LOCAL_MANEUVER": ("Farmhand: lining up with the implement", False),
                 "WORKING_STAGE": ("Farmhand: driving to the implement", False),
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


class LandTests(unittest.TestCase):
    """land: the field's farmland parcel bought or sold through GIANTS' own request
    (FarmlandStateEvent at the parcel's price, as FarmlandManager's own buy/sell send it),
    DONE once getFarmlandOwner shows the change; and every field's parcel observed."""
    ENV = r"""
        local env = { sent = {}, money = 100000 }
        local owners = { [37] = 0, [31] = 2 }
        env.owners = owners
        env.FarmlandManager = { NO_OWNER_FARM_ID = 0, NOT_BUYABLE_FARM_ID = 255 }
        env.g_farmlandManager = { getFarmlandOwner = function(_, id) return owners[id] or 0 end }
        env.FarmlandStateEvent = { new = function(id, farmId, price) return { id = id, farmId = farmId, price = price } end }
        env.g_client = { getServerConnection = function() return { sendEvent = function(_, e)
            env.sent[#env.sent + 1] = e
            owners[e.id] = e.farmId                     -- GIANTS' server applies it
        end } end }
        env.g_farmManager = { getFarmById = function() return { getBalance = function() return env.money end } end }
        env.g_fieldManager = { fields = {
            { fieldId = 12, posX = 10, posZ = 20, farmland = { id = 37, price = 48000, areaInHa = 1.6 } },
            { fieldId = 11, posX = 30, posZ = 40, farmland = { id = 31, price = 9000, areaInHa = 0.4 } } } }
        return env
    """

    def setUp(self):
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().print = lambda *a: None
        self.observe = self.lua.execute((ROOT / "mod/FarmhandObserve.lua").read_text())
        self.bridge = self.lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        self.bridge.Observe = self.observe
        self.env = self.lua.execute(self.ENV)
        self.run = self.lua.eval("""function(B, env, field, direction)
            local task, code = B.start(env, { op = 'land', field = field, direction = direction, farmId = 1,
                                             owned = function() return true end })
            if task == nil then return nil, code end
            task.elapsed = 0
            return task, B.step(env, task, 16) end""")

    def test_every_op_without_a_vehicle_argument_is_accepted_without_one(self):
        # Live e3e3b0c194914776-1: "buy field 10" reached the mod and was REJECTED_UNKNOWN_VEHICLE:
        # the land op, like contract and buy, acts on no vehicle. The mod's list must follow the
        # protocol's own (protocol/ipc.py COMMAND_ARGS).
        from protocol.ipc import COMMAND_ARGS
        vehicle_free = {op for op, args in COMMAND_ARGS.items() if "tractor" not in args}
        listed = {k for k, v in self.bridge.NO_VEHICLE.items() if v}
        self.assertEqual(vehicle_free - listed, set())

    def test_buying_sends_giants_request_at_the_parcels_price_and_waits_for_the_owner(self):
        task, status, code = self.run(self.bridge, self.env, 12, 1)
        self.assertEqual((status, code), ("DONE", "LAND_BOUGHT"))
        sent = self.env.sent[1]
        self.assertEqual((sent.id, sent.farmId, sent.price), (37, 1, 48000))      # never the console's 0
        task, status, code = self.run(self.bridge, self.env, 12, -1)
        self.assertEqual((status, code), ("DONE", "LAND_SOLD"))
        self.assertEqual((self.env.sent[2].farmId, self.env.sent[2].price), (0, 48000))

    def test_refusals_send_nothing(self):
        self.env.money = 1000
        self.assertEqual(self.run(self.bridge, self.env, 12, 1), (None, "NOT_ENOUGH_MONEY"))
        self.assertEqual(self.run(self.bridge, self.env, 11, 1), (None, "LAND_NOT_FOR_SALE"))    # another farm's
        self.assertEqual(self.run(self.bridge, self.env, 12, -1), (None, "LAND_NOT_OWNED"))
        self.assertEqual(self.run(self.bridge, self.env, 99, 1), (None, "FIELD_NOT_FOUND"))
        self.assertEqual(len(list(self.env.sent.values())), 0)

    def test_every_fields_parcel_is_observed_as_a_buyable(self):
        places = {p.id: p for p in self.observe.fieldPlaces(self.env, 1).values()}
        self.assertEqual((places[12].farmland, places[12].owner, places[12].price, places[12].area_ha), (37, "none", 48000, 1.6))
        self.assertEqual(places[11].owner, "other")


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
                    # Sitting in it is no takeover (the user, 2026-10-31: "im sitting in the combine
                    # but that should not stop farmhand"); driving it is.
                    getattr(self.env, who).entered = True
                    self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16))[:1], (None,))
                    self.lua.eval("function(B, v) B.Hitch.takeovers[v] = true end")(self.bridge, getattr(self.env, who))
                    self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16)), ("FAILED", "DRIVER_TOOK_OVER"))
                    self.lua.eval("function(B, v) B.Hitch.takeovers[v] = nil end")(self.bridge, getattr(self.env, who))
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

    def test_the_trailers_cover_is_opened_for_the_overload_and_closed_again(self):
        # Live 2026-10-31 (save 1, the user): "the cover is still on the trailer so it cant
        # overload". As GIANTS' own AI loading: Cover:aiPrepareLoading opens the fill unit's
        # cover, aiFinishLoading closes it; the idle time counts only once it stands open.
        c, t = self.env.c, self.env.t
        self.lua.execute("""local t = ...
            t.spec_cover = { hasCovers = true, state = 0, runningAnimations = {} }
            t.coverStates = {}
            function t:getCoverByFillUnitIndex(i) return i == 1 and { index = 1 } or nil end
            function t:setCoverState(s) self.spec_cover.state = s; self.coverStates[#self.coverStates + 1] = s
                self.spec_cover.runningAnimations = { {} } end""", t)
        c.spec_pipe.currentState = 2
        task, _ = self.start(self.bridge, self.env, "overload", 0)
        self.assertEqual(t.spec_cover.state, 1)                              # opened before anything flows
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 16000))[:1], (None,))   # still opening
        t.spec_cover.runningAnimations = self.lua.table()
        c.level, t.levels[1] = 0, 9000
        self.assertEqual(tuple(self.step(self.bridge, self.env, task, 1000)), ("DONE", "EMPTIED"))
        self.assertEqual(list(t.coverStates.values()), [1, 0])               # and closed again
        t.spec_cover.state = 1                                               # already open: left as it is
        c.level = 9000
        task, _ = self.start(self.bridge, self.env, "overload", 0)
        self.lua.eval("function(B, env, task) B.abort(env, task, 'STOPPED') end")(self.bridge, self.env, task)
        self.assertEqual(list(t.coverStates.values()), [1, 0])


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
        function v:raiseActive() end
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


class PayTests(unittest.TestCase):
    """Farmhand's pay (the user, 2026-10-30): GIANTS' helper wage off for Farmhand's work,
    Farmhand's own charged instead with GIANTS' wage money type. The player's AutoDrive
    settings are never touched."""
    ENV = r"""
        local env = { charged = {}, ad = {} }
        env.MoneyType = { AI = 7 }
        env.EconomyManager = { getCostMultiplier = function() return 2 end }
        env.g_currentMission = { environment = { currentDay = 5 },
            addMoney = function(_, amount, farmId, moneyType) env.charged[#env.charged + 1] = { amount, farmId, moneyType } end }
        env.AutoDrive = { setSettingState = function(name, value) env.ad[name] = value end }
        return env
    """

    def setUp(self):
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().print = lambda *a: None
        self.bridge = self.lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        self.env = self.lua.execute(self.ENV)
        self.start = self.lua.eval("""function(B, env, op, mode, amount)
            local task, code = B.start(env, { op = op, direction = mode, area = amount, farmId = 1,
                                             owned = function() return true end })
            if task == nil then return nil, code end
            task.elapsed = 0
            return B.step(env, task, 16) end""")

    def charged(self):
        return [tuple(c.values()) for c in self.env.charged.values()]

    def test_hourly_by_default_at_giants_rate_charged_with_the_wage_money_type(self):
        for _ in range(60):
            self.bridge.payTick(self.env, 60000, True, 1)        # an hour of Farmhand's work
        self.bridge.payTick(self.env, 60000, False, 1)           # not working: nothing
        total = sum(c[0] for c in self.charged())
        self.assertAlmostEqual(total, -1440 * 2, delta=26)        # GIANTS' 1440 / h x cost multiplier
        self.assertTrue(all(c[1:] == (1, 7) for c in self.charged()))

    def test_a_chosen_mode_zeroes_giants_wage_on_farmhands_jobs_and_leaves_autodrive_alone(self):
        self.assertEqual(self.start(self.bridge, self.env, "pay", 3, 750), ("DONE", "PAY_SET"))
        self.assertEqual(len(self.env.ad), 0)                                        # AutoDrive's own settings
        job = self.lua.eval("function() return { getPricePerMs = function() return 0.0004 end } end")()
        self.assertEqual(self.bridge.farmhandJob(job).getPricePerMs(), 0)
        self.bridge.payTick(self.env, 1000, True, 1)
        self.bridge.payTick(self.env, 1000, True, 1)
        self.assertEqual(self.charged(), [(-750, 1, 7)])                              # once this day
        self.env.g_currentMission.environment.currentDay = 6
        self.bridge.payTick(self.env, 1000, True, 1)
        self.assertEqual(len(self.charged()), 2)                                      # and the next
        # Per job: only the companion's charge after a completed job.
        self.start(self.bridge, self.env, "pay", 2, 300)
        self.bridge.payTick(self.env, 3600000, True, 1)
        self.assertEqual(len(self.charged()), 2)
        self.assertEqual(self.start(self.bridge, self.env, "charge", 0, 300), ("DONE", "CHARGED"))
        self.assertEqual(self.charged()[-1], (-300, 1, 7))
        # The game's own wages: GIANTS' back, Farmhand charges nothing.
        self.start(self.bridge, self.env, "pay", 4, 0)
        self.assertEqual(len(self.env.ad), 0)
        job = self.lua.eval("function() return { getPricePerMs = function() return 0.0004 end } end")()
        self.assertEqual(self.bridge.farmhandJob(job).getPricePerMs(), 0.0004)
        self.assertIsNone(self.start(self.bridge, self.env, "pay", 9, 0)[0])         # no such mode


class TeleportRigTests(unittest.TestCase):
    """Teleport recovery with belted cargo (the user, 2026-10-30; live: TELEPORT_CARGO for the GMC
    with the Series 9 strapped on its deckover): GIANTS' own teleport sequence, the cargo moved at
    its offset on the deck, the belts released before and fastened after."""
    RIG = r"""
        local events = {}
        local function node(x, y, z, yaw) return { x = x, y = y, z = z, yaw = yaw } end
        local function vehicle(name, n)
            local v = { name = name, rootNode = n, physics = true }
            function v:removeFromPhysics() self.physics = false; events[#events+1] = "out " .. self.name end
            function v:addToPhysics() self.physics = true; events[#events+1] = "in " .. self.name end
            function v:setAbsolutePosition(x, y, z, rx, ry, rz)
                self.rootNode.x, self.rootNode.y, self.rootNode.z, self.rootNode.yaw = x, y, z, ry
            end
            return v
        end
        local truck = vehicle("truck", node(0, 0, 0, 0))
        local deck = vehicle("deck", node(0, 1, -8, 0))
        local cargo = vehicle("cargo", node(0, 2.2, -8, 0))
        local attached = { { object = deck, jointDescIndex = 1 } }
        function truck:getAttachedImplements() return attached end
        function deck:getAttachedImplements() return {} end
        function cargo:getAttachedImplements() return {} end
        function deck:getActiveInputAttacherJointDescIndex() return 2 end
        function deck:getAttacherVehicle() return #attached > 0 and truck or nil end
        function truck:detachImplementByObject(o) attached = {}; events[#events+1] = "detach " .. o.name end
        function truck:attachImplement(o, input, joint) attached = { { object = o, jointDescIndex = joint } }
            events[#events+1] = "attach " .. o.name .. " " .. input .. " " .. joint end
        deck.spec_tensionBelts = { objectsToJoint = { { object = cargo } } }
        function deck:setAllTensionBeltsActive(active) events[#events+1] = (active and "fasten" or "release") end
        local function rot(n, x, z) return x * math.cos(n.yaw) + z * math.sin(n.yaw), -x * math.sin(n.yaw) + z * math.cos(n.yaw) end
        local env = { g_currentMission = { terrainRootNode = 0 },
            getWorldTranslation = function(n) return n.x, n.y, n.z end,
            getWorldRotation = function(n) return 0, n.yaw, 0 end,
            worldToLocal = function(n, x, y, z) local dx, dz = x - n.x, z - n.z
                return dx * math.cos(n.yaw) - dz * math.sin(n.yaw), y - n.y, dx * math.sin(n.yaw) + dz * math.cos(n.yaw) end,
            localToWorld = function(n, x, y, z) local a, b = rot(n, x, z); return n.x + a, n.y + y, n.z + b end,
            worldRotationToLocal = function(n, rx, ry, rz) return 0, ry - n.yaw, 0 end,
            localRotationToWorld = function(n, rx, ry, rz) return 0, ry + n.yaw, 0 end,
            getTerrainHeightAtWorldPos = function() return 10 end }
        return env, truck, deck, cargo, events
    """

    def test_the_cargo_goes_along_at_its_place_on_the_deck_and_is_strapped_again(self):
        import math
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        lua.globals().print = lambda *a: None
        bridge = lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        env, truck, deck, cargo, events = lua.execute(self.RIG)
        moved, units, belted = bridge.teleportRig(env, truck, 100.0, 50.0, math.pi / 2)
        self.assertEqual(moved, 3)
        self.assertEqual(len(units), 3)
        self.assertEqual((truck.rootNode.x, truck.rootNode.z), (100.0, 50.0))
        # 8 m behind the truck at its new heading (east): x 92; the cargo 1.2 m above the deck.
        self.assertAlmostEqual(deck.rootNode.x, 92.0, places=6)
        self.assertAlmostEqual(cargo.rootNode.x, 92.0, places=6)
        self.assertAlmostEqual(cargo.rootNode.y - deck.rootNode.y, 1.2, places=6)
        ev = list(events.values())
        self.assertEqual(ev[0], "release")                       # belts off before anything moves
        self.assertEqual(ev[-1], "fasten")                       # and on again after
        self.assertLess(ev.index("detach deck"), ev.index("out truck"))
        self.assertGreater(ev.index("attach deck 2 1"), ev.index("in cargo"))
        self.assertTrue(truck.physics and deck.physics and cargo.physics)
        # Live 506d0f085bd64e9b-1: fastened in the same frame, the belts held nothing and the
        # Series 9 came off the deck; the stepper checks each object is held again.
        self.assertTrue(bridge.beltsHold(belted))
        deck.spec_tensionBelts.objectsToJoint = lua.table()
        self.assertFalse(bridge.beltsHold(belted))

    def test_a_teleported_rig_lands_with_a_lane_for_giants_helper_to_start(self):
        # Live 5e0f5dc02a834ae2-1: set down 4 m off inside the yard pocket, GIANTS' helper "blocked
        # by an object" at once; the landing box is the rig's own grown by a lane ahead and beside.
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        lua.globals().print = lambda *a: None
        bridge = lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        foot = lua.eval("{lMin = -1.2, lMax = 1.2, aMin = -14, aMax = 3, height = 3, excluded = {}}")
        big = bridge.grown(foot, bridge.TELEPORT_ROOM)
        self.assertGreaterEqual(big.aMax - foot.aMax, 12)                 # a lane ahead
        self.assertGreaterEqual(foot.lMin - big.lMin, 2.5)                # and beside
        self.assertEqual((foot.aMax, foot.lMin), (3, -1.2))               # the rig's own box unchanged
        source = (ROOT / "mod/FarmhandBridge.lua").read_text()
        start = source.index("function starters.teleport")
        self.assertIn("Bridge.grown(own, Bridge.TELEPORT_ROOM)", source[start:source.index("function steppers.teleport")])

    def test_a_rig_teleported_onto_a_road_lane_lands_on_it_at_the_lanes_heading(self):
        # Live 3de53846739b4945-1: aimed at AutoDrive's waypoint, set down 28 m off the road and
        # crosswise; AutoDrive drove in from the grass and jammed. On the lane, or along it.
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        lua.globals().print = lambda *a: None
        bridge = lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        hitch = lua.eval("{TELEPORT = {clearM = 1.0}}")
        bridge.Hitch = hitch
        lua.eval("""function(h, blocked)
            h.routePoseClear = function(env, foot, x, z, yaw) return not blocked(x, z) end
            h.staticNear = function() return false end
        end""")(hitch, lambda x, z: False)
        x, z, yaw = bridge.laneSpot(lua.table(), lua.table(), -795.3, 99.9, 3.13)
        self.assertEqual((x, z, yaw), (-795.3, 99.9, 3.13))                    # on the waypoint itself
        lua.eval("""function(h, blocked)
            h.routePoseClear = function(env, foot, x, z, yaw) return not blocked(x, z) end
        end""")(hitch, lambda x, z: abs(z - 99.9) < 1.0)                       # the waypoint taken
        x, z, yaw = bridge.laneSpot(lua.table(), lua.table(), -795.3, 99.9, 3.13)
        self.assertAlmostEqual(abs(z - 99.9), 3.0, places=1)                    # 3 m along the lane
        self.assertAlmostEqual(yaw, 3.13)
        lua.eval("""function(h, blocked)
            h.routePoseClear = function(env, foot, x, z, yaw) return not blocked(x, z) end
        end""")(hitch, lambda x, z: x > -796.0)                                  # roadside objects east
        x, z, yaw = bridge.laneSpot(lua.table(), lua.table(), -795.3, 99.9, 3.13)
        self.assertAlmostEqual(z, 99.9, places=1)                               # at the waypoint,
        self.assertLessEqual(x, -796.0)                                         # a metre or two across
        self.assertGreaterEqual(x, -797.4)
        lua.eval("""function(h) h.routePoseClear = function() return false end end""")(hitch)
        self.assertIsNone(bridge.laneSpot(lua.table(), lua.table(), -795.3, 99.9, 3.13))


class WorkToolTests(unittest.TestCase):
    """worktool: the plow lowered with field creation on, and back (the user, 2026-10-31: "add
    field 4 to field 5 with create fields enabled"). GIANTS' own calls only."""

    ENV = r"""
        local env = { NetworkUtil = { getObjectId = function() return 3 end }, allowed = true }
        env.g_currentMission = { getHasPlayerPermission = function(self, name) return name == "createFields" and env.allowed end }
        local plow = { spec_plow = {}, limit = true, lowered = false, forced = false, calls = {} }
        function plow:setPlowLimitToField(v) self.limit = v; self.calls[#self.calls + 1] = "limit " .. tostring(v) end
        function plow:getPlowForceLimitToField() return self.forced end
        function plow:aiImplementStartLine() self.lowered = true; self.calls[#self.calls + 1] = "start" end
        function plow:aiImplementEndLine() self.lowered = false; self.calls[#self.calls + 1] = "end" end
        function plow:getIsLowered() return self.lowered end
        local t = { ownerFarmId = 1 }
        function t:getAttachedImplements() return { { object = plow } } end
        env.t, env.plow = t, plow
        return env
    """

    def setUp(self):
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().print = lambda *a: None
        for name in ("FarmhandObserve.lua", "FarmhandHitch.lua"):
            self.lua.execute((ROOT / "mod" / name).read_text())
        self.bridge = self.lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        self.env = self.lua.execute(self.ENV)
        self.run = self.lua.eval("""function(B, env, direction)
            local task, code = B.start(env, { op = "worktool", tractor = env.t, direction = direction, farmId = 1,
                                             owned = function() return true end })
            if task == nil then return nil, code end
            task.elapsed = 600
            return B.step(env, task, 16) end""")

    def test_the_plow_goes_down_with_field_creation_on_and_back(self):
        self.assertEqual(tuple(self.run(self.bridge, self.env, 1)), ("DONE", "LOWERED"))
        self.assertEqual((self.env.plow.limit, self.env.plow.lowered), (False, True))
        self.assertEqual(tuple(self.run(self.bridge, self.env, -1)), ("DONE", "RAISED"))
        self.assertEqual((self.env.plow.limit, self.env.plow.lowered), (True, False))
        self.assertEqual(list(self.env.plow.calls.values()), ["limit false", "start", "end", "limit true"])

    def test_refused_where_the_game_forbids_creating_fields(self):
        self.env.allowed = False
        self.assertEqual(tuple(self.run(self.bridge, self.env, 1)), (None, "FIELD_CREATION_NOT_PERMITTED"))
        self.env.allowed, self.env.plow.forced = True, True
        self.assertEqual(tuple(self.run(self.bridge, self.env, 1)), (None, "FIELD_CREATION_DISABLED"))
        self.assertEqual((self.env.plow.limit, self.env.plow.lowered), (True, False))   # untouched


class CargoObstacleTests(unittest.TestCase):
    """While Farmhand's GOTO drives a loaded rig, the belted cargo's own AI obstacle is out
    (AIVehicleObstacle): RELEASE_BACKLOG "Loaded transport standalone"."""

    def test_the_belted_cargo_on_the_towed_trailer_is_found(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        lua.globals().print = lambda *a: None
        bridge = lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        cargo, truck = lua.execute("""
            local cargo = { removeAIVehicleObstacle = function() end }
            local deck = { spec_tensionBelts = { objectsToJoint = { { object = cargo } } } }
            function deck:getAttachedImplements() return {} end
            local truck = {}
            function truck:getAttachedImplements() return { { object = deck } } end
            return cargo, truck""")
        found = bridge.beltedCargo(truck)
        self.assertEqual(len(found), 1)
        self.assertTrue(lua.eval("function(a, b) return a == b end")(found[1], cargo))

    def test_navigate_at_fixed_contact_yields_to_farmhand_departure(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        lua.globals().print = lambda *a: None
        bridge = lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        bridge.Hitch = lua.table_from({"rigContacts": lua.eval(
            "function() return 'unit1:collision@kiosk standing' end")})
        vehicle = lua.table_from({"getLastSpeed": lua.eval("function() return 0 end")})
        task, code, status = bridge.start(lua.table(), lua.table_from({"op": "navigate", "tractor": vehicle}))
        self.assertIsNone(task)
        self.assertEqual(code, "NAVIGATE_START_CONTACT")
        self.assertEqual(status, "FAILED")

    def test_transport_wheels_keep_crop_intact_and_restore_their_own_flags(self):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        lua.globals().print = lambda *a: None
        bridge = lua.execute((ROOT / "mod/FarmhandBridge.lua").read_text())
        truck, deck, cargo = lua.execute("""
            local function wheel(care)
                local w = { destruction = { isCareWheel = care } }
                function w:setIsCareWheel(value) self.destruction.isCareWheel = value end
                return w
            end
            local cargo = { spec_wheels = { wheels = {wheel(false)} } }
            local deck = { spec_wheels = { wheels = {wheel(true)} },
                           spec_tensionBelts = { objectsToJoint = { {object=cargo} } } }
            function deck:getAttachedImplements() return {} end
            local truck = { spec_wheels = { wheels = {wheel(false),wheel(true)} } }
            function truck:getAttachedImplements() return { {object=deck} } end
            return truck, deck, cargo
        """)
        flags = lua.eval("""function(t,d,c) return t.spec_wheels.wheels[1].destruction.isCareWheel,
            t.spec_wheels.wheels[2].destruction.isCareWheel,
            d.spec_wheels.wheels[1].destruction.isCareWheel,
            c.spec_wheels.wheels[1].destruction.isCareWheel end""")
        saved = bridge.protectTransportCrops(truck)
        self.assertEqual(len(saved), 4)
        self.assertEqual(flags(truck, deck, cargo), (True, True, True, True))
        bridge.restoreTransportCrops(saved)
        self.assertEqual(flags(truck, deck, cargo), (False, True, True, False))


class ReasonWordingTests(unittest.TestCase):
    """Every failure the companion or the mod can report reaches the player in words: "could not
    start - no way there was found for the rig", never "could not start - NO_ROUTE". The codes
    are read from the source, so a new one without wording fails here (AGENTS.md: status
    explains observed state; the human interface needs no developer knowledge)."""

    # Codes that are never a failure reason the player is shown: success and phase codes, the
    # route probe's own reasons, ghosting's internal answers, and codes the companion handles.
    INTERNAL = frozenset({
        "ARRIVED", "NO_AGENT", "LINED_UP", "CANDIDATE", "CONTINUE", "PENDING", "ATTACH_REQUESTED", "ATTACH_IN_PROGRESS",
        "BACKED_OFF", "PASSED", "UNDER_PIPE", "CLEAR", "ALREADY_CLEAR", "ALONGSIDE_ALREADY_CLEAR",
        "UNPARK_NOT_BOXED", "NO_CANDIDATE", "LOCAL_TURN", "LOCAL_APPROACH", "LOCAL_LINEUP",
        "CROP", "FIELD", "GRADE", "WATER", "UNKNOWN", "PREDICTION_UNREADABLE", "UNIT_SIZE_UNREADABLE",
        "GHOSTING_OFF", "NOTHING_TO_GHOST", "GHOST_UNREADABLE",
        "HAULED", "LOADED",                                           # a haul's own progress
        "CONTRACT_", "REJECTED_", "NO_IMPLEMENT_FOR_",                # prefixes, worded below
    })

    @classmethod
    def setUpClass(cls):
        lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        lua.globals().print = lambda *a: None
        cls.bridge = lua.execute((ROOT / "mod" / "FarmhandBridge.lua").read_text())

    @staticmethod
    def emitted():
        import re
        codes = {}
        python = re.compile(r'(?:Result\(\s*"(?:BLOCKED|FAILED|UNVERIFIED)"\s*,\s*|return None, |'
                            r'(?:GoalError|ObservationMismatch|StepFailed|StepUnverified)\(\s*)"([A-Z][A-Z0-9_]+)"')
        for path in sorted((ROOT / "companion").glob("*.py")):
            for match in python.finditer(path.read_text()):
                codes.setdefault(match.group(1), path.name)
        lua_failure = re.compile(r'return\s+(?:nil|"FAILED"|"UNVERIFIED"|false)\s*,\s*"([A-Z][A-Z0-9_]{2,})"')
        lua_step = re.compile(r'return\s+"([A-Z][A-Z0-9_]{2,})"\s*(?:end|$)')
        for name in ("FarmhandBridge.lua", "FarmhandDrive.lua", "FarmhandHitch.lua"):
            for line in (ROOT / "mod" / name).read_text().split("\n"):
                if "return" in line:
                    for match in list(lua_failure.finditer(line)) + list(lua_step.finditer(line)):
                        codes.setdefault(match.group(1), name)
        return codes

    def test_every_reported_failure_has_words(self):
        reasons = set(self.bridge.REASONS.keys())
        codes = self.emitted()
        self.assertGreater(len(codes), 150)                                   # the scan still finds them
        missing = sorted(f"{code} ({where})" for code, where in codes.items()
                         if code not in reasons and code not in self.INTERNAL)
        self.assertEqual(missing, [], "add wording to Bridge.REASONS (or INTERNAL if never shown)")

    def test_the_wording_completes_the_players_sentence(self):
        for code, text in self.bridge.REASONS.items():
            with self.subTest(code=code):
                self.assertTrue(text and text == text.strip() and not text.endswith("."), text)
        # A reason is a sentence's end, never a whole notice: it belongs in REASONS, not NOTICES.
        notices = {key: text for key, text in self.bridge.NOTICES.items()}
        self.assertEqual(sorted(k for k, text in notices.items() if not text.startswith("Farmhand")), [])

    def test_every_field_operation_says_which_implement_is_missing(self):
        from companion.executor import OPERATIONS
        for name in OPERATIONS:
            self.assertIn(f"NO_IMPLEMENT_FOR_{name.upper()}", set(self.bridge.REASONS.keys()), name)

    def test_the_follow_refusals_reach_the_player_in_words(self):
        self.assertEqual(tuple(self.bridge.noticeText("BLOCKED_PLAYER_NOT_IN_VEHICLE")),
                         ("Farmhand: could not start - get into the vehicle it should follow first", True))
        self.assertIn("FollowMe mod", self.bridge.noticeText("BLOCKED_FOLLOWME_UNAVAILABLE")[0])

    def test_notices_read_as_words_even_for_a_code_not_yet_worded(self):
        cases = {"FAILED_NO_ROUTE": "Farmhand: stopped short - no way there was found for the rig",
                 "BLOCKED_VEHICLE_AMBIGUOUS": "Farmhand: could not start - more than one vehicle answers to "
                                              "that name; say which",
                 "BLOCKED_OVER_SPENDING_LIMIT": "Farmhand: could not start - it costs more than your spending "
                                                "limit (Farmhand settings)",
                 "FAILED_SOMETHING_NEW": "Farmhand: stopped short - something new",
                 "BLOCKED_CONTRACT_NO_PERMISSION": "Farmhand: could not start - the game would not start the "
                                                   "contract (no permission)"}
        for notice, expected in cases.items():
            with self.subTest(notice=notice):
                self.assertEqual(tuple(self.bridge.noticeText(notice)), (expected, True))
