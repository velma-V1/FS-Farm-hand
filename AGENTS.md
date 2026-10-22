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
     bales and bags only). LOAD: only with the transport trailer's ramps verified
     down through GIANTS' own Foldable state; the tractor alone (nothing attached),
     straight along the trailer's axis up its rear ramps, at most 3 km/h, at most the
     ramp plus deck length; checked with the same GIANTS collision query, guards and
     restore-on-every-exit as UNPARK; it stops when the tractor's body is inside the
     deck box and at rest; the belts are then fastened through GIANTS' TensionBelts
     and the tractor must be observed belted (fastened by the trailer) and at rest.
     UNLOAD: belts released and observed released, ramps down, straight reverse off
     the ramps under the same guards and caps. Nothing is teleported, no collision is
     disabled, and travel with the load stays GIANTS' or AutoDrive's. Extended (the user,
     2026-10-21, "load the tractor and plow on the gooseneck"): the tractor may carry one
     implement mounted on its three-point joint, lifted and, if it folds, folded; only if the
     whole rig fits (the tractor inside the belt area, the rig forward to its front, the
     implement over the trailer's own deck), with the swept volume and every check covering
     the rig, refused unmoved otherwise; it stops if the implement is lowered or unfolded
     on the way. Nothing towed comes along.
  6. ALONGSIDE (the user, 2026-10-21: "combine full, figure this out, quit stopping at every
     small issue"; GIANTS' helper cannot start a full combine, AutoDrive's unloader serves
     only a combine whose AI runs, and GIANTS GOTO takes no spot under the pipe). Only after
     the standing combine's pipe is out (GIANTS Pipe) and GIANTS GOTO has brought the tractor
     and its one towed trailer near the pipe's line behind it (the line through the pipe's
     discharge point along the combine's heading): the tractor forward onto and along that
     line with GIANTS' driveToPoint, at most 3 km/h and 25 m, until the trailer's middle
     reaches the pipe point, and it must end within 0.8 m of it; refused unmoved unless the
     tractor starts within 2.5 m and 15 degrees of the line, the trailer is swung at most 30
     degrees, its middle is behind the point, the rig's swept corridor is clear in GIANTS'
     collision query (the combine is not excluded) and no other vehicle (the combine and its
     header aside) or person is near; LOAD's guards while moving (it may never get farther off
     the line than at the start), and it stops
     if the combine moves or its pipe comes in; restore-on-every-exit as UNPARK. The grain
     moves by GIANTS' own discharge. Nothing is teleported and no collision is disabled.
- Keep Python orchestration in companion/, pure contracts in protocol/ and
  adapters/, game-side Lua in mod/, synthetic state in simulator/, tests in tests/.
  Never import simulator into companion or mod. No simulated success in real adapters.

## Goal-directed operation (the user, 2026-10-21)
- A request is a goal, completed from the observed current state: what is already
  done (hitched, loaded, belted, bought, delivered) is verified and the work continues;
  it is never refused for that, nor repeated. A purchase is never made twice for one goal.
- Autonomous delivery of a loaded transport (a trailer carrying a belted vehicle) is
  AutoDrive's wherever it can do it. Observed (FourFields save 2, GMC 3500HD + 1124 Deckover +
  Deutz Series 9; scoped to that combination until others are tested): GIANTS GOTO is blocked by
  its own belted cargo (build e91fc6c, live d14230aa9fb24d30-3) and AutoDrive 3.0.1.4 waits on it
  as traffic (build 24c7dfb, live 831d6868b9df40b0-1; its source ignores only what is mounted on
  the vehicle itself). Neither is started for it, and AutoDrive's global traffic detection is
  never turned off: the job is kept and the player asked for the smallest help (drive it to the
  destination, then "continue"). FollowMe runs only when the player explicitly asks to be
  followed; it never stands in for a point-to-point delivery. Without a usable AutoDrive route
  the job is kept and the exact route or configuration gap is reported.
- Unloading happens only on observed arrival at the intended destination's unloading
  area. Stopping for traffic, a cancellation, a takeover or a failure never unloads.
- AutoDrive configuration may be completed only through AutoDrive's own supported
  mechanisms, with the minimum destination markers in the test save, the existing
  network preserved and its configuration backed up first. No replacement navigation,
  and creating markers is never made the player's routine job.
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
