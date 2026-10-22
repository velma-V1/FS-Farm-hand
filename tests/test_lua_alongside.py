"""ALONGSIDE in mod/FarmhandHitch.lua (Lua 5.1 and 5.4; AGENTS.md exception 6) on the
kinematic stub of tests/test_lua_unpark.py (NOT GIANTS physics: boxes stand in for collision
shapes). It proves the bounded state machine: a tractor with one towed trailer, straight
forward along its own heading, at most 3 km/h and 25 m, until the trailer's middle is under
the standing combine's pipe point; refused unmoved unless the pipe is out, its point on the
trailer's middle line and ahead, the rig straight, the swept volume clear and no other
vehicle or person near; every guard while moving; braked and restored on every exit.

Live motive: the MF 8570 full on field 3 (Riverbend Springs save 1, 2026-10-21); its helper
cannot start full (COULD_NOT_PREPARE) and GIANTS GOTO takes no spot under the pipe.
"""
from pathlib import Path
import unittest

from lupa import lua51, lua54

from tests.test_lua_unpark import STUBS

ROOT = Path(__file__).resolve().parents[1]
COMMAND_KMH = 3 * 0.85         # what a motion commands under its 3 km/h limit (Hitch.SPEED.commandShare)

TRAILER = dict(back=7.0, width=2.5, length=6.0, height=2.6)

# The combine in the tractor's start frame: its root `side` metres to the left, `ahead` metres
# ahead; its pipe point at (pipeA, pipeL), 4 m up; its body a vehicle shape.
COMBINE = r"""
local W, w, o = ...
local yaw = w.yaw
local s, c = math.sin(yaw), math.cos(yaw)
local x0, z0 = w.steer.x, w.steer.z
local function at(a, l) return x0 + s * a + c * l, z0 + c * a - s * l end
local rx, rz = at(o.ahead or 5, o.side or 6)
local px, pz = at(o.pipeA or 5, o.pipeL or 0)
local combine = { ownerFarmId = 1, rootNode = { x = rx, y = 0, z = rz, yaw = yaw + (o.cyaw or 0) },
                  size = { width = 3.5, length = 8, height = 3.8, widthOffset = 0, lengthOffset = 0, heightOffset = 0 } }
combine.rootVehicle = combine
w.pipeState, w.combineSpeed = o.pipeState or 2, 0
combine.spec_pipe = { unloadingStates = { [2] = true } }
function combine:getCurrentPipeState() return w.pipeState end
function combine:getCurrentDischargeNode() return { node = { x = px, y = 4, z = pz, yaw = yaw } } end
function combine:getLastSpeed() return w.combineSpeed end
w.combine = combine
w.objects[400] = combine
w.names[400] = "combine"
table.insert(w.shapes, { id = 400, group = W.F.VEHICLE, rect = function()
    local r = combine.rootNode
    return { x = r.x, z = r.z, yaw = r.yaw, hx = 1.65, hz = 3.9, y0 = 0.2, y1 = 3.7 }
end })
table.insert(w.env.g_currentMission.vehicleSystem.vehicles, combine)
"""

RUN = r"""
local Hitch = ...
return function(w, s, maxSteps, hook)
    for i = 1, maxSteps do
        if hook then hook(i) end
        local code = Hitch.alongsideStep(w.env, s, 50)
        if code ~= "CONTINUE" then return code, i end
    end
    return "CONTINUE", maxSteps
end
"""


class LuaAlongsideTests(unittest.TestCase):
    runtime = lua54

    def setUp(self):
        self.lua = self.runtime.LuaRuntime(unpack_returned_tuples=True)
        self.hitch = self.lua.execute((ROOT / "mod/FarmhandHitch.lua").read_text())
        self.W = self.lua.execute(STUBS, self.hitch)
        loader = "load" if self.runtime is lua54 else "loadstring"
        self.combine = self.lua.eval(f"function(src, W, w, o) return {loader}(src)(W, w, o) end")
        self.runner = self.lua.execute(RUN, self.hitch)

    def world(self, trailer=None, **opts):
        w = self.W.new(self.lua.table(trailer=self.lua.table(**(trailer or TRAILER))))
        self.combine(COMBINE, self.W, w, self.lua.table(**opts))
        return w

    def start(self, w, past=False):
        out = self.hitch.newAlongside(w.env, self.W.args(w, self.lua.table(combine=w.combine, **({"pass": True} if past else {}))))
        out = out if isinstance(out, tuple) else (out,)
        return out + (None,) * (3 - len(out))

    def drives(self, w):
        return [dict(d) for d in w.drives.values()]

    def assert_refused(self, w, reason):
        session, why, refused = self.start(w)
        self.assertIsNone(session)
        self.assertEqual(why, reason, self.hitch.describeAlongside(refused))
        self.assertEqual(self.drives(w), [])

    def test_drives_straight_until_the_trailer_middle_is_under_the_pipe(self):
        w = self.world()
        x0, z0 = w.steer.x, w.steer.z
        session, why, _ = self.start(w)
        self.assertIsNotNone(session, why)
        self.assertAlmostEqual(session.travelM, 12.0, places=6)        # the middle 7 m behind, the pipe 5 m ahead
        code, _ = self.runner(w, session, 3000, None)
        self.assertEqual(code, "UNDER_PIPE", self.hitch.describeAlongside(session))
        along, lateral = self.W.travel(w, x0, z0)
        self.assertGreater(along, 11.99)
        self.assertLess(along, 12.2)
        self.assertAlmostEqual(lateral, 0.0, places=6)
        moving = [d for d in self.drives(w) if d["allowed"]]
        self.assertTrue(moving and all(d["forwards"] and d["maxSpeed"] == COMMAND_KMH for d in moving))
        self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])
        self.assertNotEqual(w.tractor.forceIsActive, True)
        self.assertEqual(self.hitch.alongsideStep(w.env, session, 50), "UNDER_PIPE")    # latched: brakes only

    def test_a_trailer_left_swung_by_giants_starts_and_must_end_under_the_pipe(self):
        # Live 6c66894767044432-1: GIANTS ended with the trailer 19-21 deg off; it may start.
        # The stub's trailer stays swung (a real one is pulled in behind): its middle on the
        # line ends UNDER_PIPE, a middle off the line ends ALONGSIDE_NOT_UNDER, braked.
        for offset, expected in ((0.0, "UNDER_PIPE"), (2.5, "ALONGSIDE_NOT_UNDER")):
            w = self.world(trailer=dict(TRAILER, turn=0.45))
            w.trailer.size.lengthOffset = offset
            session, why, _ = self.start(w)
            self.assertIsNotNone(session, why)
            code, _ = self.runner(w, session, 3000, None)
            self.assertEqual(code, expected, self.hitch.describeAlongside(session))
            self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])

    def test_it_steers_onto_the_pipes_line_and_must_end_under_the_pipe(self):
        # Live 75ef60d7f5ad4735-1: GIANTS reached the aim but about 7 deg turned, the pipe 1.9 m
        # aside. The stub cannot steer (it drives along its heading), so the aim is checked: every
        # drive point lies toward the line; it ends beside the pipe, refused as not under it.
        w = self.world(pipeL=1.2)
        session, why, _ = self.start(w)
        self.assertIsNotNone(session, why)
        code, _ = self.runner(w, session, 3000, None)
        self.assertEqual(code, "ALONGSIDE_NOT_UNDER", self.hitch.describeAlongside(session))
        moving = [d for d in self.drives(w) if d["allowed"]]
        self.assertTrue(moving and all(d["lx"] >= 0 and d["forwards"] for d in moving) and moving[-1]["lx"] > 0)   # toward the line, eased in
        self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])

    def test_after_the_overload_it_passes_the_combine_and_stops_clear_of_its_front(self):
        # Live 15dbb6c7eb994721-1: GIANTS' deliver helper would not start the rig from beside
        # the combine. The combine's front 4 m past the pipe point, the trailer's rear 15 m behind
        # it: on 4 + 2 + 15 = 21 m.
        w = self.world()
        x0, z0 = w.steer.x, w.steer.z
        session, why, _ = self.start(w, past=True)
        self.assertIsNotNone(session, why)
        self.assertAlmostEqual(session.travelM, 21.0, places=6)
        code, _ = self.runner(w, session, 3000, None)
        self.assertEqual(code, "PASSED", self.hitch.describeAlongside(session))
        along, _ = self.W.travel(w, x0, z0)
        self.assertGreater(along, 20.99)
        self.assertLess(along, 21.2)
        self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])
        far = self.world(ahead=12)                                       # its front 11 m on: 28 m > 25
        self.assertEqual(tuple(self.start(far, past=True))[1], "ALONGSIDE_TOO_FAR")

    def test_refused_unmoved_unless_everything_holds(self):
        self.assert_refused(self.world(pipeState=1), "PIPE_NOT_OUT")
        self.assert_refused(self.world(pipeL=3.0), "ALONGSIDE_NOT_LINED_UP")             # 3 m off the pipe's line
        self.assert_refused(self.world(cyaw=0.35), "ALONGSIDE_NOT_LINED_UP")             # 20 deg off its heading
        self.assert_refused(self.world(pipeA=-9), "ALONGSIDE_PAST_THE_PIPE")
        self.assert_refused(self.world(pipeA=20), "ALONGSIDE_TOO_FAR")   # 20 ahead + 7 behind > 25
        self.assert_refused(self.world(trailer=dict(TRAILER, turn=0.6)), "ALONGSIDE_RIG_NOT_STRAIGHT")   # 34 deg
        w = self.world()
        w.box(4.0, 5.0, -1.0, 1.0, 0, 2.0, None, "bale")                 # in the way
        self.assert_refused(w, "ALONGSIDE_PATH_BLOCKED")
        w = self.world(side=2.2)                                          # the combine's body in the swept volume
        self.assert_refused(w, "ALONGSIDE_PATH_BLOCKED")
        w = self.world()
        w.vehicle(6.0, -5.0)                                              # another vehicle beside the way
        self.assert_refused(w, "ALONGSIDE_BLOCKED")
        w = self.world()
        w.entered = True
        self.assert_refused(w, "SEATED_DURING_ALONGSIDE")

    def test_a_takeover_or_a_stop_while_it_moves_ends_it_braked(self):
        for hook, code in ((lambda w, i: i == 15 and w.takeOver(), "DRIVER_TOOK_OVER"),
                           (lambda w, i: i == 15 and setattr(w, "entered", True), "SEATED_DURING_ALONGSIDE"),
                           (lambda w, i: i == 15 and self.hitch.abort(w.env, self.session, "STOPPED"), "STOPPED")):
            with self.subTest(code=code):
                w = self.world()
                self.session, why, _ = self.start(w)
                self.assertIsNotNone(self.session, why)
                result, _ = self.runner(w, self.session, 3000, lambda i, w=w, hook=hook: hook(w, i))
                self.assertEqual(result, code)
                self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])
                self.assertNotEqual(w.tractor.forceIsActive, True)

    def test_over_the_limit_or_beside_the_trailer_it_stops_braked(self):
        w = self.world()
        session, why, _ = self.start(w)
        self.assertIsNotNone(session, why)
        code, _ = self.runner(w, session, 3000, lambda i: i == 20 and setattr(w, "runawayKmh", 3.6))
        self.assertEqual(code, "OVERSPEED", self.hitch.describeSpeed(session))
        self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])
        # Something beside the towed trailer (the combine is not excluded either): contact.
        w = self.world()
        x0, z0 = w.steer.x, w.steer.z
        session, why, _ = self.start(w)
        self.assertIsNotNone(session, why)

        def post(i):
            if i == 20:
                a, _ = self.W.travel(w, x0, z0)
                w.box(a - 8.0, a - 6.0, -1.5, -1.3, 0, 2.0, None, "post")
        code, _ = self.runner(w, session, 3000, post)
        self.assertEqual(code, "ENVELOPE_CONTACT", self.hitch.describeEnvelope(session))
        self.assertIn("units=2", self.hitch.describeEnvelope(session))
        # Clear: it ends under the pipe with no contact, under the limit.
        w = self.world()
        session, why, _ = self.start(w)
        self.assertEqual(self.runner(w, session, 3000, None)[0], "UNDER_PIPE")
        self.assertIsNone(session.envelopeHits)
        self.assertLessEqual(session.peakKmh, 3.0)

    def test_stops_braked_if_the_combine_moves_or_its_pipe_comes_in(self):
        for hook in (lambda w, i: i == 10 and setattr(w.combine.rootNode, "x", w.combine.rootNode.x + 1.0),
                     lambda w, i: i == 10 and setattr(w, "pipeState", 1)):
            w = self.world()
            session, why, _ = self.start(w)
            self.assertIsNotNone(session, why)
            code, _ = self.runner(w, session, 3000, lambda i, w=w, hook=hook: hook(w, i))
            self.assertIn(code, ("COMBINE_MOVED", "PIPE_MOVED"))
            self.assertEqual(list(w.events.values())[-2:], ["brake", "cruise:0"])
            self.assertNotEqual(w.tractor.forceIsActive, True)


class Lua51AlongsideTests(LuaAlongsideTests):
    runtime = lua51


if __name__ == "__main__":
    unittest.main()
