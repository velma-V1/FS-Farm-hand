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
    local tractor = { ownerFarmId = 1, ad = { isStoppingWithError = false } }
    tractor.ad.stateModule = {
        setMode = function(_, mode) call("setMode:" .. mode) end,
        isActive = function() return w.adActive end,
        getStartHelper = function() return w.startHelper == true end,   -- StateModule.lua:406-408
    }
    function tractor:getLastSpeed() return w.speed end
    function tractor:getIsEntered() return w.entered end
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
    env.g_currentMission = { getFarmId = function() return 1 end,
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
    Bridge.Observe = { PROTOCOL = 1, NULL = Observe.NULL, PIPE_PATH = Observe.PIPE_PATH, encode = Observe.encode,
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
                 ("entered", True, "OCCUPIED", 500)]
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
                if attr == "entered":
                    self.w.entered = True
                self.W.run(self.w, ms)
                self.assertEqual(self.results(1)[-1], (1, "FAILED", code))
                self.assertEqual("stopAutoDrive" in self.calls(), attr == "entered")

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

    def test_stop_cancels_only_a_goto_it_saw_running(self):
        for starts, cancelled in ((True, True), (False, False)):
            with self.subTest(goto_started=starts):
                self.w = self.W.new()
                self.adopt()
                self.w.gotoStarts = starts
                self.send(1, "stage", {"tractor": 11, "x": 1.0, "z": 2.0, "yaw": 0.0})
                self.assertIn("startJob", self.calls())
                self.w.aiActive = True                 # ours, or someone else's job if ours never ran
                self.write("command.xml", encode_command("s1", 2, "stop", {"tractor": 11}))
                self.W.run(self.w, 100)
                self.assertEqual("stopCurrentAIJob" in self.calls(), cancelled)

    def test_attach_needs_the_align_that_found_this_candidate(self):
        self.adopt()
        self.w.env.NetworkUtil.getObject = self.lua.eval("function(t) return function(id) return t end end")(
            self.w.tractor)
        self.send(1, "attach", {"tractor": 11, "wagon": 12, "tractorJoint": 1, "wagonJoint": 1})
        self.assertEqual(self.results(1)[-1], (1, "FAILED", "NOT_ALIGNED"))

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
