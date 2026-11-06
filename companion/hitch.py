"""Pure hitch geometry for HITCH: which joints, final pose, staging pose, and
where the bounded reverse can take over.

No game calls and no steering. GIANTS remains the authority on whether the pair
can actually attach (attachableInfo); this only says where to put the tractor.
"""
import math
from dataclasses import dataclass

from protocol.model import Equipment, HitchPlan, Pose

# What a tractor can be hitched to: forage wagons and every other towed implement
# (adapters/fs25.py: category "implement" = a GIANTS work specialization).
IMPLEMENT_CATEGORIES = frozenset({"forage_wagon", "implement"})

# Where STAGE aims GIANTS' GOTO: this far straight ahead of the final hitch pose.
# Only an aim, never a tolerance: GOTO may end anywhere (live: 14 m and 138 degrees
# off), and what follows is decided by the observed joints (align_problem).
# 6 m leaves the agent room in front of the drawbar and slack inside ALIGN_REACH_M.
STAGING_DISTANCE_M = 6.0
SIDE_DRAWBAR_M = 2.0      # a drawbar this far beside an implement's axis (and more than ahead) tows it sideways
# A rear joint: behind the AI root, or up to this far ahead of it, where a pickup's
# gooseneck ball or fifth wheel sits over the rear axle (live: the GMC Sierra's at
# z = +0.0014 m). Front hitches are metres ahead and stay excluded.
REAR_JOINT_MAX_Z = 0.5
MAX_STAGING_DISTANCE_M = 10.0

# Where GIANTS' GOTO is aimed (TRAVEL_NEAR, STAGE): beside the implement, never in
# front of it. Live the agent reported NOT_REACHABLE for the staging pose (8ffafcc
# twice; 3da28a3f24dd450b-1 after 1.2 km, -2 at once): arriving there facing away
# from the implement takes a path across it. GIANTS also refuses a target on
# blocked ground (AIParameterPosition:validate 83-90, aiSystem:getIsPositionReachable;
# live 27539c7115a44b86: both sides of a trailer in a yard), so there are several
# aims. Each puts the planned tractor joint (ahead, aside) of the implement's joint,
# turned this many degrees from its heading towards its axis; from each the bounded
# LOCAL_MANEUVER lines up (mod/FarmhandHitch.lua Hitch.chooseCurve, empty yard:
# 13-22 m at a 4.8 m planning radius; the first also at 6.6 m). Aims only.
APPROACHES = ((-2.0, 10.0, 45.0), (2.0, 12.0, 90.0), (-6.0, 15.0, 0.0))
# First, as a driver does it (the user, 2026-10-31: "pull up in front of the item, turn 90 degrees
# and be pretty close to 0 degrees from the item and within 5 m of the hitch"): across the front
# of the implement, square to its axis, so one 90-degree forward arc of radius r (each of
# PERPENDICULAR_RADII_M, the tractor's own is not known here) after PERPENDICULAR_RUN_M straight
# ends on the axis facing away, the planned joint PERPENDICULAR_END_M ahead of the implement's.
# Live 2026-10-31: from the 45-degree aim every line-up curve met the implement's box
# (LOCAL_PATH_HITS_WAGON) and the re-approach circled for 3 min.
# Replayed through Hitch.chooseCurve for tractors 1.5-3.5 m joint-to-root, 4-8.5 m radius, GIANTS
# ending 2 m / 10 degrees off: r = 9 m with an 8 m run-in left a short line-up in every case.
PERPENDICULAR_RADII_M = (9.0, 7.0)
PERPENDICULAR_END_M = 5.0
PERPENDICULAR_RUN_M = 8.0
APPROACH_CLEARANCE_M = 10.0         # an aim this close to other equipment is tried after the clear ones

# A tractor standing with one of its joints on a loose implement's input joint (live
# 5cdd589ebdce4f01-3, 23a8b1d881c7489b-4: just unhitched, joints 0.12 m apart) cannot
# be moved by GIANTS' agent at all: the implement is a navigation obstacle touching it,
# and the agent stays BLOCKED even for a target straight ahead. Straight after its own
# detach Farmhand pulls the tractor forward until the joints are PULL_CLEAR_M apart
# (= mod/FarmhandHitch.lua Hitch.PULL.clearM); a hitch from such a stand is refused.
PULL_CLEAR_JOINT_M = 1.5
PULL_CLEAR_M = 4.0                  # verified on GIANTS' joint offsets
# The mod drives this much further (= mod/FarmhandHitch.lua Hitch.PULL.clearM - PULL_CLEAR_M): its
# measure of a three-point joint differs from GIANTS' offsets (live 0.31 m).
PULL_CLEAR_MARGIN_M = 0.5

# The envelope from which the bounded reverse (mod/FarmhandHitch.lua) can bring the
# planned tractor joint to the wagon's input joint, measured on the observed joints
# in the wagon's frame. Provisional: every check logs the numbers.
ALIGN_REACH_M = 9.0                 # the reverse's 10 m cap (Hitch.LIMITS.maxDistanceM) less 1 m of curve
ALIGN_MAX_HEADING_RAD = math.radians(30)
ALIGN_MAX_LATERAL_M = 2.0

# Inside this distance of the wagon's input joint (the planned tractor joint's gap)
# Farmhand drives the last forward metres itself (LOCAL_MANEUVER, = mod/FarmhandHitch.lua
# Hitch.LOCAL_LIMITS.workRadiusM); GIANTS' GOTO is used only from farther away.
LOCAL_WORK_RADIUS_M = 25.0


def _finite(*values) -> bool:
    return all(type(v) in (int, float) and math.isfinite(v) for v in values)


def _pto_connects(outputs, inputs) -> bool:
    """GIANTS' PowerTakeOffs:attachPowerTakeOff (747-766) connects each output at
    the tractor joint to the wagon inputs at the wagon joint with the same ptoName.
    A wagon joint with no input PTO at all (known empty) has nothing to connect:
    PowerConsumer turns on by torque alone (power_sufficient), never by a shaft."""
    if outputs is None or inputs is None:
        return False
    return not inputs or bool(outputs & inputs)


# An input joint this far behind the implement's root still counts as its front one.
FRONT_JOINT_SLACK_M = 0.5


def plan_hitch(tractor: Equipment, wagon: Equipment,
               staging_distance: float = STAGING_DISTANCE_M) -> HitchPlan | None:
    """Plan a rear hitch of ``wagon`` to ``tractor``, or None if geometry is unusable.

    Joint choice: a free rear tractor joint (z < REAR_JOINT_MAX_Z) and an active
    wagon input joint of the same joint type, not behind the wagon's root (the plan
    reverses along the wagon's axis onto its front; live 8f44d1209a9c4d40-1 the 1124
    Deckover's rear pintle took the place of its gooseneck), which, if the wagon needs
    PTO power, share a PTO name; the lowest (tractor, wagon) index pair wins,
    independent of observation order. Unknown power or PTO data yields no plan. The tractor faces the wagon's
    forward heading, so the last move is a straight reverse along the wagon axis.
    """
    if not _finite(staging_distance) or not 0 < staging_distance <= MAX_STAGING_DISTANCE_M:
        raise ValueError("Invalid staging distance")
    if tractor.category not in ("tractor", "harvester") or wagon.category not in IMPLEMENT_CATEGORIES:
        return None
    root, heading = tractor.ai_root, wagon.heading
    if root is None or not _finite(root.x, root.z, root.yaw, heading):
        return None
    need = wagon.required_pto_torque
    if not _finite(need) or need < 0:
        return None
    for joints in (tractor.output_joints, wagon.input_joints):
        if len({joint.index for joint in joints}) != len(joints):
            return None  # Contradictory observation: joint indices must be unique.
    wx, wz = wagon.position

    def towing(inp) -> float:
        """The way ``inp`` is towed: the implement's heading when the joint lies on its axis, else from
        its root towards the joint (live 2026-11-06: the Colossus 9000's Titan header, folded to its
        trailer end, has its drawbar 10 m to the side of its root; it is towed sideways)."""
        dx, dz = inp.position[0] - wx, inp.position[1] - wz
        along = dx * math.sin(heading) + dz * math.cos(heading)
        lateral = dx * math.cos(heading) - dz * math.sin(heading)
        return math.atan2(dx, dz) if abs(lateral) > max(abs(along), SIDE_DRAWBAR_M) else heading

    def at_front(inp) -> bool:
        yaw = towing(inp)
        return (inp.position[0] - wx) * math.sin(yaw) + (inp.position[1] - wz) * math.cos(yaw) >= -FRONT_JOINT_SLACK_M

    pairs = sorted(((out, inp) for out in tractor.output_joints for inp in wagon.input_joints
                    if out.free and inp.active and out.joint_type == inp.joint_type
                    and _finite(out.x, out.z, *inp.position) and out.z < REAR_JOINT_MAX_Z and at_front(inp)
                    and (need == 0 or _pto_connects(out.pto_names, inp.pto_names))),
                   key=lambda pair: (pair[0].index, pair[1].index))
    if not pairs:
        return None
    joint, inp = pairs[0]
    heading = towing(inp)
    target = tuple(inp.position)
    # Place the AI root so that root + rotate(yaw, joint offset) == target.
    jx, jz = Pose(0.0, 0.0, heading).to_world(joint.x, joint.z)
    final = Pose(target[0] - jx, target[1] - jz, heading)
    staging = Pose(final.x + staging_distance * math.sin(heading),
                   final.z + staging_distance * math.cos(heading), heading)
    return HitchPlan(joint.index, inp.index, joint.joint_type, target, final, staging)


def approach_families(plan: HitchPlan) -> tuple[tuple[float, float, float], ...]:
    """(ahead, aside, turn) of the planned joint for each aim family, in order: the square
    ones across the implement's front (PERPENDICULAR_*), then APPROACHES."""
    reach = math.dist((plan.final.x, plan.final.z), plan.target)      # planned joint behind the AI root
    square = tuple((PERPENDICULAR_END_M + reach - r, r + PERPENDICULAR_RUN_M + reach, 90.0)
                   for r in PERPENDICULAR_RADII_M)
    return square + APPROACHES


def approach_poses(plan: HitchPlan, origin, others) -> tuple[Pose, ...]:
    """GOTO's aims for ``plan``: the AI root pose for each APPROACHES entry on each
    side of the implement. Aims clear of every position in ``others`` (other
    equipment) by APPROACH_CLEARANCE_M come first, then by APPROACHES order, then
    the side nearer ``origin``."""
    heading = plan.final.yaw
    # The planned joint's offset in the AI root frame (plan.final puts it on target).
    back = Pose(0.0, 0.0, -heading).to_world(plan.target[0] - plan.final.x, plan.target[1] - plan.final.z)
    fx, fz = math.sin(heading), math.cos(heading)
    others = [tuple(p) for p in others]
    ranked = []
    for rank, (ahead, aside, turn) in enumerate(approach_families(plan)):
        for side in (1, -1):
            jx = plan.target[0] + ahead * fx + side * aside * fz
            jz = plan.target[1] + ahead * fz - side * aside * fx
            yaw = heading - side * math.radians(turn)       # turned towards the axis
            ox, oz = Pose(0.0, 0.0, yaw).to_world(*back)
            pose = Pose(jx - ox, jz - oz, yaw)
            crowded = any(math.dist((pose.x, pose.z), other) < APPROACH_CLEARANCE_M for other in others)
            ranked.append(((crowded, rank, math.dist((pose.x, pose.z), tuple(origin))), pose))
    ranked.sort(key=lambda entry: entry[0])
    return tuple(pose for _, pose in ranked)


def joint_separation(tractor: Equipment, implement: Equipment) -> float | None:
    """The smallest distance between any of ``tractor``'s joints and any of
    ``implement``'s input joints, or None when unreadable."""
    root = tractor.ai_root
    if root is None or not _finite(root.x, root.z, root.yaw):
        return None
    joints = [root.to_world(j.x, j.z) for j in tractor.output_joints if _finite(j.x, j.z)]
    inputs = [i.position for i in implement.input_joints if _finite(*i.position)]
    if not joints or not inputs:
        return None
    return min(math.dist(j, p) for j in joints for p in inputs)


def against_loose_implement(tractor: Equipment, others) -> bool:
    """Whether one of ``tractor``'s joints is within PULL_CLEAR_JOINT_M of an input
    joint of an implement in ``others`` that is attached to nothing."""
    for other in others:
        if other.id != tractor.id and other.attached_to is None:
            gap = joint_separation(tractor, other)
            if gap is not None and gap <= PULL_CLEAR_JOINT_M:
                return True
    return False


def hitch_problem(tractor: Equipment, wagon: Equipment) -> str:
    """Why plan_hitch finds no plan, for diagnostics only (never a decision)."""
    if tractor.ai_root is None or not _finite(tractor.ai_root.x, tractor.ai_root.z, tractor.ai_root.yaw):
        return "TRACTOR_GEOMETRY_UNKNOWN"
    if not _finite(wagon.heading):
        return "WAGON_HEADING_UNKNOWN"
    if not _finite(wagon.required_pto_torque):
        return "PTO_NEED_UNKNOWN"
    rear = [j for j in tractor.output_joints if j.free and _finite(j.x, j.z) and j.z < REAR_JOINT_MAX_Z]
    active = [j for j in wagon.input_joints if j.active]
    if not rear:
        return "NO_FREE_REAR_JOINT"
    if not active:
        return "NO_ACTIVE_WAGON_JOINT"
    shared = [(o, i) for o in rear for i in active if o.joint_type == i.joint_type]
    if not shared:
        return "INCOMPATIBLE_HITCH_TYPE"
    if wagon.required_pto_torque > 0 and not any(_pto_connects(o.pto_names, i.pto_names) for o, i in shared):
        return "NO_MATCHING_PTO"
    return "NO_VALID_HITCH_GEOMETRY"


@dataclass(frozen=True)
class JointError:
    """The planned tractor joint relative to the wagon's input joint, in the wagon's
    frame: ``ahead`` along its forward axis (the side a tractor hitches from),
    ``lateral`` sideways (its local +x), ``heading`` the tractor's turn off the wagon's
    heading (radians, signed). ``joint`` and ``eye`` are the world points."""
    gap: float
    ahead: float
    lateral: float
    heading: float
    joint: tuple
    eye: tuple

    def describe(self) -> str:
        return ("tractor joint (%.2f, %.2f) wagon joint (%.2f, %.2f) gap %.2f m ahead %.2f m "
                "lateral %.2f m heading %.1f deg" % (*self.joint, *self.eye, self.gap, self.ahead,
                                                     self.lateral, math.degrees(self.heading)))


def joint_error(tractor: Equipment, wagon: Equipment, plan: HitchPlan) -> JointError | None:
    """The observed error of the planned joint pair, or None if unobservable."""
    root = tractor.ai_root
    joint = next((j for j in tractor.output_joints if j.index == plan.tractor_joint), None)
    eye = next((j for j in wagon.input_joints if j.index == plan.wagon_joint), None)
    if root is None or joint is None or eye is None or not _finite(
            root.x, root.z, root.yaw, joint.x, joint.z, *eye.position, plan.final.yaw):
        return None
    jx, jz = root.to_world(joint.x, joint.z)
    dx, dz = jx - eye.position[0], jz - eye.position[1]
    fx, fz = math.sin(plan.final.yaw), math.cos(plan.final.yaw)
    heading = (root.yaw - plan.final.yaw + math.pi) % (2 * math.pi) - math.pi
    return JointError(math.hypot(dx, dz), dx * fx + dz * fz, dx * fz - dz * fx, heading,
                      (jx, jz), tuple(eye.position))


def align_problem(error: JointError | None) -> str | None:
    """None when the bounded reverse can take over from here, else why not. The
    reverse only reverses: it never turns a tractor around or crosses the wagon."""
    if error is None:
        return "UNKNOWN_OBSERVATION"
    if error.gap > ALIGN_REACH_M:
        return "TRAVEL_ENDED_TOO_FAR"
    if error.ahead <= 0:
        return "ALIGN_WRONG_SIDE"
    if abs(error.heading) > ALIGN_MAX_HEADING_RAD:
        return "ALIGN_HEADING_OUT_OF_RANGE"
    if abs(error.lateral) > ALIGN_MAX_LATERAL_M:
        return "ALIGN_LATERAL_OUT_OF_RANGE"
    return None


# ------------------------------------------------------------ tractor transport
# Pure geometry for tractor transport (AGENTS.md exception 5): where GIANTS' GOTO puts the
# tractor so LOAD can start (right behind the transport trailer's ramps, on its axis, facing
# along it), the deck box LOAD stops in (GIANTS' own cargo area: the span of the trailer's
# tension belts, half a belt spacing longer at each end), and whether a tractor stands on
# the deck or off the ramps. Everything comes from observation (positions, headings,
# GIANTS' size boxes, belt markers): nothing here knows a map, a place or a model.
#
# Frames as mod/FarmhandHitch.lua's LOAD: the trailer's root, ``a`` along its heading,
# ``l`` to its local +x side.
RAMP_M = 3.0                     # = mod/FarmhandHitch.lua Hitch.LOAD.rampM
LATERAL_START_M = 0.5            # = Hitch.LOAD.lateralStartM
HEADING_START_RAD = math.radians(7.0)   # = Hitch.LOAD.headingStartDeg
# The tractor's front this far behind the trailer's rear (inside LOAD's 0..RAMP_M window),
# in the order GIANTS' GOTO is asked for them.
FRONT_GAPS_M = (1.5, 2.25, 0.75)
# First the front this far behind the trailer's rear (on the axis, facing along it), so the last
# GOTO is a short straight run: live f41b44f422974e15-5, GIANTS turned in from the side with a
# plow on and ended 0.68 m and 12.4 deg off, and would not start again that close to the ramps.
# The middle of LOAD's run-in window (6.5-15.5 m behind the ramps' foot, RAMP_M behind the rear):
# GIANTS' GOTO ends on position alone, 0.9-3.1 m off its aim (live 0788b0f2b8af4c34-1 stopped
# 3.1 m short; 95b84283873043d7-1, at the old 8 m from the rear, its front stood only 4.1 m
# behind the foot, 19 deg turned, and LOAD reached the foot still 10.5 deg off).
RUN_UP_GAP_M = 11.0 + RAMP_M
# LOAD's run-in (= Hitch.LOAD.runInM, minRunInM, lateralRunInM, headingRunInDeg): from this far
# behind the ramps' foot and this close to the axis LOAD steers onto it itself; not closer than
# MIN_RUN_IN_M unless already inside the start window.
RUN_IN_M, MIN_RUN_IN_M = 16.0, 6.0
LATERAL_RUN_IN_M = 2.5
HEADING_RUN_IN_RAD = math.radians(25.0)
DECK_SLACK_M = 0.25              # = the mod's UNLOAD_NOT_ON_DECK slack
DECK_LATERAL_M = 0.75            # a tractor centred on the deck within this


def _unit(heading: float) -> tuple[float, float]:
    return math.sin(heading), math.cos(heading)


def to_frame(trailer: Equipment, point) -> tuple[float, float]:
    """(a, l) of a world point in the trailer's frame."""
    fx, fz = _unit(trailer.heading)
    dx, dz = point[0] - trailer.position[0], point[1] - trailer.position[1]
    return dx * fx + dz * fz, dx * fz - dz * fx


def _ends(vehicle: Equipment) -> tuple[float, float] | None:
    """The vehicle's rear and front along its own heading, relative to its root."""
    if vehicle.size is None:
        return None
    length, _, _, offset = vehicle.size
    return offset - length / 2, offset + length / 2


def is_transport(item: Equipment) -> bool:
    """A trailer with GIANTS Foldable ramps, at least two tension belts and a size box."""
    return (item.fold is not None and len(item.belt_markers) >= 2 and item.size is not None
            and item.heading is not None and item.belts_fastened is not None)


def carrying(trailer: Equipment, items) -> bool:
    """Something already on the transport: belted to it, or a vehicle (not its own truck)
    standing on its deck."""
    if trailer.belted:
        return True
    return any(i.id != trailer.id and i.id != trailer.attached_to and i.size is not None and i.heading is not None
               and on_deck(i, trailer) for i in items)


# The mod's LOAD fit (= Hitch.LOAD.frontMarginM, rearMarginM): the tractor inside the belt
# area, a mounted implement no farther back than the trailer's rear plus this.
FIT_FRONT_MARGIN_M, FIT_REAR_MARGIN_M = 0.3, 0.25


def fits(tractor: Equipment, implement: Equipment | None, trailer: Equipment) -> bool | None:
    """Whether the tractor (with the implement on its three-point joint) fits the deck as
    LOAD requires, from GIANTS' size boxes and the belts; None when a size is not observed.
    Conservative: the implement's whole length is added behind the tractor."""
    deck, trailer_ends = deck_box(trailer), _ends(trailer)
    if deck is None or trailer_ends is None or tractor.size is None:
        return None
    length = tractor.size[0]
    if length > deck[1] - deck[0]:
        return False
    if implement is None:
        return True
    if implement.size is None:
        return None
    return length + implement.size[0] <= (deck[1] - FIT_FRONT_MARGIN_M) - (trailer_ends[0] + FIT_REAR_MARGIN_M)


def deck_box(trailer: Equipment) -> tuple[float, float, float, float] | None:
    """(aMin, aMax, lMin, lMax) of the deck in the trailer's frame, from its belts."""
    if trailer.heading is None or len(trailer.belt_markers) < 2:
        return None
    along, lateral = [], []
    for m in trailer.belt_markers:
        a1, l1 = to_frame(trailer, (m[0], m[2]))
        a2, l2 = to_frame(trailer, (m[3], m[5]))
        along.append((a1 + a2) / 2)
        lateral += [l1, l2]
    along.sort()
    pad = (along[-1] - along[0]) / (len(along) - 1) / 2
    return along[0] - pad, along[-1] + pad, min(lateral), max(lateral)


def load_aims(tractor: Equipment, trailer: Equipment, gaps=FRONT_GAPS_M) -> tuple[Pose, ...]:
    """GIANTS GOTO poses (of the tractor's AI root) that put its front ``gaps`` behind the
    trailer's rear, on its axis, facing along it; () when the geometry is unknown."""
    tractor_ends, trailer_ends = _ends(tractor), _ends(trailer)
    if (tractor_ends is None or trailer_ends is None or tractor.ai_root is None or tractor.heading is None
            or trailer.heading is None):
        return ()
    tx, tz = _unit(tractor.heading)
    # The tractor's front ahead of its AI root (GIANTS' agent steers that node).
    front = ((tractor.position[0] - tractor.ai_root.x) * tx + (tractor.position[1] - tractor.ai_root.z) * tz
             + tractor_ends[1])
    fx, fz = _unit(trailer.heading)
    aims = []
    for gap in gaps:
        a = trailer_ends[0] - gap - front
        aims.append(Pose(trailer.position[0] + fx * a, trailer.position[1] + fz * a, trailer.heading))
    return tuple(aims)


def _angle(a: float, b: float) -> float:
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


def can_run_in(tractor: Equipment, trailer: Equipment) -> bool:
    """Inside LOAD's run-in window: its front up to RUN_IN_M behind the ramps' foot, within
    LATERAL_RUN_IN_M and HEADING_RUN_IN_RAD of the axis, and at least MIN_RUN_IN_M behind it
    unless lined up straight, then anywhere behind the rear (LOAD decides at the start)."""
    tractor_ends, trailer_ends = _ends(tractor), _ends(trailer)
    if tractor_ends is None or trailer_ends is None or tractor.heading is None or trailer.heading is None:
        return False
    a, l = to_frame(trailer, tractor.position)
    front, foot = a + tractor_ends[1], trailer_ends[0] - RAMP_M
    turn = _angle(tractor.heading, trailer.heading)
    straight = abs(l) <= LATERAL_START_M - 0.1 and turn <= HEADING_START_RAD - math.radians(1.0)
    if straight:
        # Lined up, LOAD starts from anywhere short of the deck, the ramps included, as the mod
        # does (live after 7209dc3bcab34075-1: UNLOAD left the Series 9 lined up at the ramps'
        # foot; 902c1b52e05742ec-1: a STOP left it straight with its front over the trailer's rear).
        deck = deck_box(trailer)
        rear = a + tractor_ends[0]
        return foot - RUN_IN_M + 0.5 <= front and (deck is None and front <= trailer_ends[0] - 0.25
                                                  or deck is not None and rear < deck[0] - 0.25)
    return (foot - RUN_IN_M + 0.5 <= front <= foot - MIN_RUN_IN_M - 0.5 and abs(l) <= LATERAL_RUN_IN_M - 0.3
            and turn <= HEADING_RUN_IN_RAD - math.radians(2.0))


BACKOFF_NEAR_M = 10.0            # = mod/FarmhandHitch.lua Hitch.BACKOFF.nearM


def close_behind(tractor: Equipment, trailer: Equipment) -> bool:
    """Waiting right behind the ramps, near the axis: where BACKOFF applies (its front within
    BACKOFF_NEAR_M behind the trailer's rear, within the run-in's lateral reach)."""
    tractor_ends, trailer_ends = _ends(tractor), _ends(trailer)
    if tractor_ends is None or trailer_ends is None or trailer.heading is None:
        return False
    a, l = to_frame(trailer, tractor.position)
    front = a + tractor_ends[1]
    return trailer_ends[0] - BACKOFF_NEAR_M <= front <= trailer_ends[0] and abs(l) <= LATERAL_RUN_IN_M


def lined_up(tractor: Equipment, trailer: Equipment, share: float = 1.0) -> bool:
    """Inside ``share`` of LOAD's start window (the mod decides at the start; this chooses
    whether GOTO is needed and whether its end will do)."""
    tractor_ends, trailer_ends = _ends(tractor), _ends(trailer)
    if tractor_ends is None or trailer_ends is None or tractor.heading is None or trailer.heading is None:
        return False
    a, l = to_frame(trailer, tractor.position)
    front = a + tractor_ends[1]
    return (abs(l) <= LATERAL_START_M * share and _angle(tractor.heading, trailer.heading) <= HEADING_START_RAD * share
            and trailer_ends[0] - RAMP_M + 0.25 <= front <= trailer_ends[0] - 0.25)


def on_deck(tractor: Equipment, trailer: Equipment) -> bool:
    """The tractor's body inside the deck box (with the slack) and centred across it."""
    deck, ends = deck_box(trailer), _ends(tractor)
    if deck is None or ends is None:
        return False
    a, l = to_frame(trailer, tractor.position)
    return (deck[0] - DECK_SLACK_M <= a + ends[0] and a + ends[1] <= deck[1] + DECK_SLACK_M
            and abs(l - (deck[2] + deck[3]) / 2) <= DECK_LATERAL_M)


ON_RAMPS_M = 0.5                 # = mod/FarmhandHitch.lua newLoad's onRamps (its front this far over the trailer)


def on_ramps(tractor: Equipment, trailer: Equipment) -> bool:
    """Stopped on the ramps on its way on or off (live ac71c4b45aea40dd-1: UNLOAD ended
    ENVELOPE_CONTACT half down): its front up over the trailer's rear, on the trailer's axis,
    not yet on the deck."""
    tractor_ends, trailer_ends = _ends(tractor), _ends(trailer)
    if tractor_ends is None or trailer_ends is None or trailer.heading is None or on_deck(tractor, trailer):
        return False
    a, l = to_frame(trailer, tractor.position)
    deck = deck_box(trailer)
    middle = (deck[2] + deck[3]) / 2 if deck is not None else 0.0
    return a + tractor_ends[1] > trailer_ends[0] + ON_RAMPS_M and abs(l - middle) <= DECK_LATERAL_M


def off_ramps(tractor: Equipment, trailer: Equipment) -> bool:
    """The tractor's front behind the ramps lying on the ground."""
    tractor_ends, trailer_ends = _ends(tractor), _ends(trailer)
    if tractor_ends is None or trailer_ends is None or trailer.heading is None:
        return False
    a, _ = to_frame(trailer, tractor.position)
    return a + tractor_ends[1] <= trailer_ends[0] - RAMP_M + DECK_SLACK_M


# ------------------------------------------------------------ joining two fields
# The user (2026-10-31): "add field 4 to field 5 with create fields enabled". GIANTS' field
# work finds a field's boundary from the worked ground around where its helper starts
# (FieldCourseField:detectAtPosition -> BoundaryDetectionTask), so once the strip between two
# fields is plowed with field creation on, its helper works them as one. The strip: between
# the two outlines' nearest points, over the length both fields share, reaching STRIP_INTO_M
# into each field; covered by straight lanes along that length, one plow width apart,
# alternating direction (back and forth, as a player plows a headland strip).
STRIP_INTO_M = 0.5              # each lane set reaches this far into both fields (just touching them)
STRIP_MAX_GAP_M = 60.0          # fields farther apart than this are not joined


def _nearest_on_segment(p, a, b):
    ax, az = a
    dx, dz = b[0] - ax, b[1] - az
    n = dx * dx + dz * dz
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - az) * dz) / n))
    return ax + dx * t, az + dz * t


def _nearest_on_outline(p, outline):
    best = None
    for i, a in enumerate(outline):
        q = _nearest_on_segment(p, a, outline[(i + 1) % len(outline)])
        d = math.dist(p, q)
        if best is None or d < best[0]:
            best = (d, q)
    return best


def strip_lanes(outline_a, outline_b, width: float):
    """The straight lanes (start, end) that plow the strip joining field A to field B, or ()
    when they are not near each other. ``width``: the plow's working width."""
    if len(outline_a) < 3 or len(outline_b) < 3 or not width or width <= 0:
        return ()
    pairs = [(d, p, q) for p in outline_a for d, q in [_nearest_on_outline(p, outline_b)]]
    pairs += [(d, q, p) for p in outline_b for d, q in [_nearest_on_outline(p, outline_a)]]
    gap, pa, pb = min(pairs, key=lambda x: x[0])
    if gap > STRIP_MAX_GAP_M:
        return ()
    if gap > 1e-6:
        nx, nz = (pb[0] - pa[0]) / gap, (pb[1] - pa[1]) / gap
    else:                                           # touching: across the shared edge
        cx = sum(x for x, _ in outline_b) / len(outline_b) - sum(x for x, _ in outline_a) / len(outline_a)
        cz = sum(z for _, z in outline_b) / len(outline_b) - sum(z for _, z in outline_a) / len(outline_a)
        n = math.hypot(cx, cz) or 1.0
        nx, nz = cx / n, cz / n
    tx, tz = -nz, nx

    def facing(poly, other):
        """Where the boundary of ``poly`` faces ``other`` across the gap: its points (every metre)
        no farther from ``other`` than the gap plus a metre, as positions along the strip."""
        out = []
        for i, a in enumerate(poly):
            b = poly[(i + 1) % len(poly)]
            steps = max(1, int(math.dist(a, b)))
            for k in range(steps + 1):
                q = (a[0] + (b[0] - a[0]) * k / steps, a[1] + (b[1] - a[1]) * k / steps)
                if _nearest_on_outline(q, other)[0] <= gap + 1.0:
                    out.append((q[0] - pa[0]) * tx + (q[1] - pa[1]) * tz)
        return out

    fa, fb = facing(outline_a, outline_b), facing(outline_b, outline_a)
    if not fa or not fb:
        return ()
    lo, hi = max(min(fa), min(fb)), min(max(fa), max(fb))
    if hi - lo < width:
        return ()
    depth = gap + 2 * STRIP_INTO_M
    count = max(1, math.ceil(depth / width))
    lanes = []
    for k in range(count):
        o = -STRIP_INTO_M + (k + 0.5) * depth / count
        bx, bz = pa[0] + nx * o, pa[1] + nz * o
        a = (bx + tx * lo, bz + tz * lo)
        b = (bx + tx * hi, bz + tz * hi)
        lanes.append((a, b) if k % 2 == 0 else (b, a))
    return tuple(lanes)
