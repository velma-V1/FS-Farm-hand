# Authoritative agent rules

Applies to the entire repository, to Codex, Claude, and every other coding agent.
Read this file and BUILD_SPEC.md before editing. The user's current instructions
have precedence; report conflicts instead of silently changing the scope.

## Mission and boundaries
- Product scope (user-authorized 2026-10-18, replacing the earlier "forage wagon
  only" task): a local autonomous farmhand/dispatcher. From a plain-language goal it
  determines the required capabilities, equipment and materials, finds or buys
  them through the game's own mechanisms, assembles, loads, travels, works, verifies
  from game state, recovers within bounds and finishes. Grow it capability by
  capability; each one qualified in the real game (tests/ACCEPTANCE.md matrix).
- The production farmhand is LOCAL-FIRST. It runs on the user's Windows PC alongside
  Farming Simulator 25. GitHub, hosted CI, and remote coding environments are
  development tools only and must never become runtime dependencies.
- Intended runtime: FS25 + game-side Lua mod + local Windows companion service +
  local IPC. The planner/LLM, speech recognition, and TTS are local components unless
  the user explicitly chooses otherwise later.
- LLMs interpret language and choose high-level intent. They never supply Lua,
  arbitrary commands, vehicle IDs to bypass selection, or physical control loops.
- Deterministic executor: FIND → RESERVE → MOVE → ATTACH → VERIFY → DELIVER.
  Completion requires observed attachment, arrival, and stopped equipment.
- FS25 and existing mods perform physical work: GIANTS' own jobs (GOTO, field
  work helpers), AutoDrive, and other installed mods where evidenced. No navigation,
  pathfinding, driving, field-work, Courseplay, or AutoDrive replacements, and no
  store-UI automation where a game API exists. Purchasing and field work are in
  scope only through the game's own APIs, only for the requested job, and only
  after the requirement, the item's fit and the farm's funds are checked.
- User-authorized exceptions, all in mod/FarmhandHitch.lua, all steered by GIANTS'
  AIVehicleUtil.driveToPoint and all hard-capped; never extend them into navigation:
  1. HITCH's ALIGN_REVERSE, a bounded reverse of the last few metres that stops as
     soon as GIANTS reports the exact planned joints attachable.
  2. HITCH's LOCAL_MANEUVER (authorized 2026-10-18 after GIANTS' GOTO ended
     NOT_REACHABLE 12-19 m off twice at 8ffafcc): forward only, inside a 25 m work
     radius of the wagon's joint, from the observed joints onto the wagon's
     centreline so ALIGN_REVERSE can take over. One closed-form curve, no search,
     no retry; GOTO stays the provider for anything farther.
  3. DETACH's PULL_CLEAR (authorized 2026-10-19 after GIANTS' agent stayed BLOCKED in
     every direction next to a just-unhitched implement, live 23a8b1d881c7489b-4):
     forward and straight only, at most 8 m, at most 3 km/h, only straight after
     Farmhand's own successful detach while the joints are still within the close
     separation threshold; stops on player takeover, person or vehicle conflict,
     obstacle (no progress), stall, timeout or loss of reservation; the separation
     from the detached implement must keep growing, and it stops as soon as it is
     clear for normal navigation; every exit restores the temporary vehicle-control
     state. The implement is never made passable and GIANTS' collision handling is
     never weakened.
  4. UNPARK (authorized 2026-10-20 after GIANTS' agent stayed BLOCKED from the first
     frame inside the Large Shed, live 53c77d1e809645b3-3): only after GIANTS accepted
     a target and its agent could not start the vehicle from where it stands (blocked,
     or ended not_reachable, unmoved); never for an invalid destination, with a closed
     door or gate within 15 m of the rig (refused UNPARK_DOOR_CLOSED: building doors are
     never opened by Farmhand, the user 2026-10-20 after the Large Shed's wall shoved
     four vehicles up to 11.7 m, live bfbc32565ecb4472-1), or for a vehicle already in
     the open. Straight only, no turning: forward with whatever is attached, back only
     with nothing attached; at most 20 m total, 3 km/h, 40 s; to the nearest exit only.
     Before moving, GIANTS' own collision query (overlapBox, proven on the rig by its
     own body and the terrain) must show the whole rig's swept volume (length, width,
     height of every unit, 0.5 m either side) clear of buildings, beams, trees,
     vehicles and objects; only the rig's own collision nodes and the terrain are
     excluded, and an unreadable query is never clear. While moving it re-checks past
     the leading end (at least 2 m, more with speed) and keeps PULL_CLEAR's guards
     (takeover, player, vehicles, reservation, divergence, stall, timeout) and stops on
     any change of the rig. One difference (the user, 2026-10-20, after the Open Hall
     live run): PULL_CLEAR's 8 m / 6 m vehicle-root clearance applies to vehicles ahead
     of the rig's leading end in the direction of travel; one beside or behind is
     checked by its body with the margin and stops it if it moves; the player keeps
     6 m / 4 m on every side. It ends braked and at rest only where the whole rig is out
     from under any roof with 1.5 m clear all round and GIANTS' navigation map takes
     the ground ahead; every exit restores the temporary vehicle-control state. Then
     GIANTS gets the original target once, which must move it, else
     UNPARK_HANDOFF_BLOCKED; a hitch re-plans from the exit (the user, 2026-10-20):
     inside the 25 m local radius the bounded LOCAL_MANEUVER / ALIGN_REVERSE take over
     with no GOTO, beyond it the one hand-off GOTO. One extraction per vehicle per parent job (its plan's
     steps and next-tractor attempts included). Nothing is made passable, collisions
     are never disabled, nothing is teleported, and no safety distance is loosened.
  5. LOAD / UNLOAD (authorized 2026-10-20 for tractor transport; no GIANTS job or
     installed mod drives a vehicle onto a deck: Universal Autoload moves pallets,
     bales and bags only). Nothing is teleported, no collision is disabled, and travel
     with the load stays GIANTS' or AutoDrive's. Each motion below is its own permission;
     "live" lists what a live run has shown, nothing more:
     - LOAD straight. Scope: the tractor alone, straight along the transport trailer's
       axis up its rear ramps verified down through GIANTS' Foldable state, at most the
       ramp plus deck length, stopping with its body inside the deck box (the tension
       belts' span) at rest; the belts then fastened through GIANTS' TensionBelts and the
       tractor observed belted and at rest. Start window: steering node within 0.5 m and
       heading within 5 degrees of the axis, its front 0-3 m behind the trailer's rear.
       Geometry assumed: GIANTS' size boxes bound each unit; the ramps and deck are the
       trailer's own collision (excluded), the truck is not; the stub never climbs, so
       physics on the ramps is known only from live runs. Live: d14230aa9fb24d30-2 (Series 9
       alone, 1124 Deckover, 13.12 m) PASS; its speed was not then measured by Farmhand and
       the LOAD runs' end readings were 1.98-4.74 km/h: requalification under the speed rule
       below is pending.
     - LOAD with a mounted implement (the user, 2026-10-21, "load the tractor and plow on the
       gooseneck"): one implement on the three-point joint, lifted and, if it folds, folded;
       only if the whole rig fits (the tractor inside the belt area, the rig forward to its
       front less 0.3 m, the implement no farther back than the trailer's rear plus 0.25 m),
       the swept volume and every check over the rig; it stops if the implement is lowered or
       unfolded on the way. Nothing towed comes along. Geometry assumed: the implement's size
       box contains it lifted (its low point over the ramps' foot is only observed live).
       Live: 5f19b1c28f614266-1 (Series 9 + Titan plow) loaded and belted, but its end speed
       read 4.14 km/h over the 3 km/h cap: not qualified; requalification pending.
     - Run-in (the user, 2026-10-21, "figure this out"; that instruction authorizes trying it,
       it is not evidence that it is safe): LOAD may start up to 12 m behind the ramps' foot,
       within 2.5 m and 25 degrees of the axis, steering onto the axis with driveToPoint
       (20 until live 3389dfbd1b564660-1, where GIANTS' run-up left the Series 9 18.4 degrees
       turned; widened by Farmhand under the same instruction, not by the user);
       its corridor is swept wide from where it stands to the ramps' foot and the rig's own
       straight width on the ramps and deck; at the foot it must be inside the start window
       (0.5 m, 5 degrees) or it stops there braked (NOT_LINED_UP_AT_RAMPS). Geometry assumed:
       a mounted implement swings out behind the tractor while it steers; the wide corridor
       is assumed to cover it (not computed), so the live envelope check below is what shows
       the clearance. Live: 5f19b1c28f614266-1 at the foot 0.01 m off, 4.43 degrees (speed
       not qualified, as above).
     - UNLOAD: belts released and observed released, ramps down, straight back off the ramps
       under the same guards and caps until its front is 3 m behind the trailer's rear, then
       the ramps up. Live: once (Series 9 alone, 2026-10-21, end reading 3.26 km/h): not
       qualified on speed; requalification pending.
     - BACKOFF (the user, 2026-10-21, "figure this out"; as for the run-in): a tractor (alone
       or with that mounted implement) waiting at most 8 m behind the transport's rear, which
       GIANTS' agent will not start there, straight back along its own heading, at most 8 m
       at 3 km/h; refused unmoved unless the swept volume behind is clear in GIANTS' collision
       query, no other vehicle (the transport aside) or person is near and the rig is as LOAD
       allows; the transport watched while it moves; so GIANTS GOTO can line it up from the
       run-up. Geometry assumed: straight back, the implement leading; its swept volume is the
       rig box. Live: 2f6986868b5245f1-2 (Series 9 + plow) 8.01 m, lateral 0.00, end reading
       3.02 km/h: requalification pending.
     Every one of them: GIANTS' collision query proven on the rig before it moves, the swept
     volume clear, takeover, seat, reservation, AI, divergence (0.3 m), distance, stall, timeout,
     other vehicles and the player watched each frame, the trailer and truck unmoved (25 cm),
     the ramps still down, latched braked and restored on every exit, and the speed and
     envelope rules below.
  6. ALONGSIDE (the user, 2026-10-21: "combine full, figure this out, quit stopping at every
     small issue"; that instruction authorizes trying it, it is not evidence that it is safe;
     GIANTS' helper cannot start a full combine, AutoDrive's unloader serves only a combine
     whose AI runs, and GIANTS GOTO takes no spot under the pipe). Scope: only after the
     standing combine's pipe is out (GIANTS Pipe) and GIANTS GOTO has brought the tractor and
     its one towed trailer near the pipe's line behind it (the line through the pipe's
     discharge point along the combine's heading): the tractor forward onto and along that
     line with driveToPoint, at most 3 km/h and 25 m, until the trailer's middle reaches the
     pipe point, ending within 0.8 m of it. Refused unmoved unless the tractor starts within
     2.5 m and 15 degrees of the line, the trailer is swung at most 30 degrees, its middle is
     behind the point, the rig's swept corridor is clear in GIANTS' collision query (the
     combine is not excluded) and no other vehicle (the combine and its header aside) or
     person is near; LOAD's guards while moving (never farther off the line than at the start
     plus 0.3 m), and it stops if the combine moves or its pipe comes in. PASS: after the
     overload, the pipe still out, on along the same line under the same guards and caps
     until the rig's rear is 2 m past the combine's front (GIANTS would not start it from
     beside the combine). Geometry assumed: the trailer's size-box middle is where the grain
     lands; the pipe is above the trailer's top; a swung trailer straightens behind the
     tractor (not modelled: it must end under the pipe or it fails ALONGSIDE_NOT_UNDER). The
     grain moves by GIANTS' own discharge; a stop or takeover ends it and folds the pipe.
     Live: 2bfa1536c9e2497b-1 under the pipe 17.97 m, end 0.06 m beside the point, end
     reading 3.01 km/h; bc801c056bac4966-1 PASS 10.24 m, 2.93 km/h: requalification under the
     speed and envelope rules pending.
- Motion speed (the user, 2026-10-22: the 4.14 km/h LOAD reading broke the 3 km/h cap).
  Every self-driven motion above has its session's limit (3 km/h, ALIGN/LOCAL at most 5 by
  argument, default 3); driveToPoint's maxSpeed is only GIANTS' motor target, so Farmhand
  measures the speed itself (the root's travel over the last 0.25 s) and governs it on the
  higher of that and GIANTS' own per-frame reading: it commands 85 % of the limit (never under
  1 km/h), throttles in proportion to the margin left under a ceiling eased in from 0.3 to full
  over 1.5 s, takes the throttle off above 90 %, brakes a frame above 97 % (downhill above the
  command), and above the limit ends the motion braked (OVERSPEED, latched, never retried by
  itself); both readings are logged with a speed trace (every 0.1 s for 2 s, then 0.5 s). The limit is
  never raised to make a run pass, and a run that went over it is not qualified.
- Motion envelope: while LOAD / UNLOAD, BACKOFF and ALONGSIDE move, every unit of the rig
  (the tractor, a mounted implement, a towed trailer) at its own pose, pitched as it is, is
  asked of GIANTS' collision world every 0.1 s: anything within 0.15 m of a unit (but the rig,
  the trailer it drives onto, and for BACKOFF the transport it backs away from) ends it braked
  (ENVELOPE_CONTACT); what comes within 0.5 m is recorded. Kinematic stub tests prove the
  state machines only; collision clearance is what these live observations show.
- Keep Python orchestration in companion/, pure contracts in protocol/ and
  adapters/, game-side Lua in mod/, synthetic state in simulator/, tests in tests/.
  Never import simulator into companion or mod. No simulated success in real adapters.

## Goal-directed operation (the user, 2026-10-21)
- A request is a goal, completed from the observed current state: what is already
  done (hitched, loaded, belted, bought, delivered) is verified and the work continues;
  it is never refused for that, nor repeated. A purchase is never made twice for one goal.
- Autonomous delivery of a loaded transport (a trailer carrying a belted vehicle) is
  AutoDrive's wherever it can do it. GIANTS GOTO does not start such a rig (build e91fc6c,
  live d14230aa9fb24d30-3: its agent BLOCKED from the first frame by its own cargo). AutoDrive
  counts belted cargo as the rig's own by its source: its sensors skip what checkIsConnected
  calls connected, getHasObjectMounted, which GIANTS' AttacherJoints passes on to the towed
  trailer and whose TensionBelts holds the cargo (an earlier record here said AutoDrive ignores
  only what is mounted on the vehicle itself: that missed AttacherJoints' recursion and was
  wrong). Why live 831d6868b9df40b0-1 (AutoDrive 3.0.1.4, FourFields) never moved is not known:
  its drive now reports what AutoDrive waits on (planning onto its network, a sensor and what is
  in its box, paused) and ends FAILED with it after 3 minutes unmoved. Found live 2026-10-22: a
  vehicle Farmhand hands to AutoDrive must be kept awake (raised active each frame) or AutoDrive's
  own update never runs, and AutoDrive drives only from a network waypoint with a route (the one
  nearest the GMC at Riverbend's store is a dead end). Before loading, an empty transport that has
  no route is taken by GIANTS GOTO onto the nearest waypoint that has one. So the loaded rig goes by
  AutoDrive to a marker (made AutoDrive's own way where there is none: "Field N", or a farm
  building by its own name); only when AutoDrive did not move it, or there is no AutoDrive or
  marker, is the player asked for the smallest help (drive it to the destination, then
  "continue"), recorded as assisted, never as autonomous delivery. AutoDrive's global traffic
  detection is never turned off. FollowMe runs only when the player explicitly asks to be
  followed; it never stands in for a point-to-point delivery.
- One identity per job (the user, 2026-10-22): equipment selected for a job (a transport, a
  truck, a tractor, an implement) is kept for all its steps; a changed circumstance is replanned
  explicitly and said, never answered by silently picking other equipment.
- The player's own play never cancels a job: walking, driving another vehicle, menus. A
  passenger (FS25's passenger seats, where the vehicle has them) is not a takeover; taking a
  Farmhand-controlled vehicle's driver seat is the one explicit manual handoff (the job pauses,
  kept for "continue"), never two controllers.
- Travel is complete only at the destination's usable spot (yard, field, parking): the end of
  a road or recorded route hands over to a final approach; long drives are judged by movement
  and provider state, not short fixed timeouts.
- Implements go back where they were collected after their operation (pose kept across
  reloads) unless the player said otherwise; bought equipment gets a designated home.
- A run is autonomous only with no developer commands, manual positioning or hidden fixes;
  assisted runs diagnose, they never qualify.
- Unloading, storing and selling are separate (the user, 2026-10-22). "Unload the combine"
  transfers its crop into a trailer and nothing more. A harvest's crop is sold or stored only
  when the request says so ("harvest field 5 and sell the grain") or the player's saved harvest
  policy does ("from now on sell the harvest"); otherwise it stays in the trailer, and with no
  trailer room left the harvest waits and the player is asked where it goes. AutoDrive's
  combine unloader (it delivers to a sell point) runs only for a harvest that is to be sold.
- Completion is GIANTS' verdict and the field's measured state together: a harvest is done
  only when GIANTS ended its helper FINISHED and a grid of samples over the whole field (up to
  120, at least 4 m apart, plus the helper's track) shows 95 % cut; soil work, sowing and
  spraying by GIANTS' density maps over the field's whole area; a contract only on GIANTS'
  FINISHED status with finishState SUCCESS (its completion share logged) and the reward in the
  farm's balance.
- Unloading happens only on observed arrival at the intended destination's unloading
  area. Stopping for traffic, a cancellation, a takeover or a failure never unloads.
- AutoDrive configuration may be completed only through AutoDrive's own supported
  mechanisms, with the minimum destination markers in the test save, the existing
  network preserved and its configuration backed up first. No replacement navigation,
  and creating markers is never made the player's routine job. One repair besides markers
  (the user 2026-10-22: "inspect and configure the existing AutoDrive network using supported
  mechanisms, preserving its configuration"): when AutoDrive's own path from the waypoint
  nearest the vehicle ends in a dead end and a waypoint with a route to the destination lies
  within 6 m of that end, continuing its direction (within 60 degrees, its next edge too), the
  two are joined one way with AutoDrive's own setConnectionBetween, after the backup, and the
  route confirmed by AutoDrive's own pathFromTo. Nothing else in the network is edited; a gap
  that does not fit is reported.
- Pause, a changed destination or equipment, a player takeover and resume keep the
  original goal; nothing completed is repeated. After a reload or a companion restart
  the job is reconciled with the observed world and the equipment re-identified.
- Progress is reported from observed game state only: an accepted or pending command
  is not proof that anything moves.
- When the player's help is needed, ask for the smallest intervention and keep the job
  so that "continue" resumes it.
- Report implemented, automated-test-passed and live-qualified results separately;
  one working combination never implies general equipment compatibility.
- Field work goes on while Farmhand does something else (the user, 2026-10-21): once
  GIANTS' helper works the field it is the game's own hired worker, its tractor and
  implement stay reserved for it (no other job takes them, no stale-reservation recovery
  stops it), and only after the helper is observed ended (no AI job, at rest) is the field
  measured, the pair released and its equipment brought back. A next operation on the
  same field follows behind the one ahead; STOP stops every helper Farmhand started, and
  a lost companion heartbeat does not stop a helper already handed off (like a worker the
  player hired).
- Voice stays an optional front end; its further work is deferred.

## Safe collaboration
- Work on a dedicated branch. Inspect git status before edits; preserve others'
  changes. Agree file ownership before concurrent edits; do not share a mutable
  checkout between writers. No force pushes, merges, or main-branch product edits
  unless the user explicitly requests them.
- Change shared schemas and all consumers together. Version incompatible contracts.
- Do not claim success from command acknowledgement alone. Fail closed on invalid
  intent, missing capability, ownership conflict, stale/busy equipment, or uncertain
  observations. Never take occupied equipment or another farm's assets.
- Reservations must be atomic for both units. Stop before releasing ownership after
  physical work; retain reservations if stopping cannot be confirmed.
- Do not add retries, persistence, network transports, dependencies, or scaffolding
  without a demonstrated requirement. No secrets or paid model calls in tests.

## Development workflow and runtime boundary
- Keep unfinished project work off the user's PC until a genuine live FS25 test is
  required. GitHub and remote CI may be used for source control, development,
  review, static checks, mock tests, and release packaging before that point.
- This remote development workflow does NOT change the local runtime architecture.
- The finished farmhand must be able to run locally beside FS25 without GitHub or a
  hosted service being required for ordinary operation.
- Development-only live tests (user-authorized 2026-10-18): the host runner
  `scripts/fs25_host_runner.py`, started by the user outside the agent's sandbox,
  performs only its fixed allowlist (status, install the checkout's build (also under a
  running FS25, never under a running job: the mod takes effect when the save is next
  loaded from the main menu; the user, 2026-10-20: reload save 2 from the menu, relaunch
  FS25 only when unavoidable), launch
  FS25 by Windows' activation of its installed package (fixed AppUserModelID), else
  its package's own gamelaunchhelper.exe, else its exe — fixed paths —,
  restart or stop FS25 — that image only —, a screenshot of the FS25 window to a fixed
  file, one click inside that window or one key of a fixed list (the game's own menu
  controls; never while a job or test runs), OK on the Xbox "Sign In Required" dialog
  (that title and its fixed owner processes only), the production hitch_pair, the player's
  own typed words through the production rules (letters, digits, spaces, light
  punctuation; 160 at most), STOP, collect). Never widen it into a shell, file or
  program interface, and never ship it or `--live-test` as release.
- Live FS25 behavior, Windows service behavior, local IPC, local model execution,
  speech, and game/mod integration must eventually be verified on the user's PC.

## Evidence and reuse
- Before adding an external adapter, inspect the exact upstream FS25 version,
  license, API, capabilities, and failure semantics. Record the evidence and version.
  Prefer a thin adapter; no vendoring/forking absent license review and demonstrated need.
- Never invent GIANTS or mod API calls. Unsupported remains unsupported.
- Keep mock contracts explicitly distinct from upstream APIs and physics.
- Add behavior tests before implementation. Vary IDs, inventory order, eligibility,
  compatibility, and failures. Do not weaken tests to hide a failure.
- Run `python scripts/check.py` before proposing a change. Inspect `git diff --check`
  and the final tree. Report commands/results, implemented vs mocked, and limitations.
- Passing CI tests never proves mod loading, game physics, attach alignment, route
  reachability, local IPC, Windows service behavior, multiplayer, or actual FS25 integration.
