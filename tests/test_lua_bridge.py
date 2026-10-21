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
        self.lua.globals().print = lambda *a: None
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
            "1:Farmhand: not delivered - no available tractor and implement",
            "1:Farmhand: not delivered - SOMETHING_NEW"])

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
                 "FAILED_NAVIGATE_STALLED": ("Farmhand: not delivered - the tractor got stuck (parked in a shed or boxed in?)", True)}
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
    FIELD = {"tractor": 11, "field": 7, "x": 200.0, "z": 300.0, "yaw": 0.5, "area": 0.0}

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

    # ------------------------------------------------------------ maneuver
    MANEUVER ={"tractor": 11, "wagon": 12, "tractorJoint": 4, "wagonJoint": 1}

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
                     "Farmhand: not delivered - step out of the tractor for the last metres of the hitch, "
                     "then ask again", True),
                 "FAILED_LOCAL_PATH_HITS_WAGON": (
                     "Farmhand: not delivered - there is no room to line up with the implement from here", True)}
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
