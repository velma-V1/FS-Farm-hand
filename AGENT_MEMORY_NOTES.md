# TEMPORARY AGENT MEMORY NOTES - delete before first production release

**COLD HISTORICAL ARCHIVE — NOT CURRENT ENGINEERING DIRECTION.** Do not proactively use these old notes to choose architecture or next work. AGENTS.md and CLAUDE.md define the current mission. Use entries here only when recovering a specific historical fact, failure, or live result.

Copied from Claude's private memory (2026-10-31) so any agent (Codex, Claude, others) has the same
working rules and live facts. Not authority over AGENTS.md. Delete only when the human deletes it or
explicitly asks a model to delete it - never by any automatic flag, queue, script or process.

Reload helper (temporary): scripts/reload_save1.sh (save, quit to menu, load save 1; clicks 2 s,
menu 5 s, loading up to 60 s). Run reloads yourself; never ask the user to reload.

### alongside-box-false-hits.md

User (2026-10-31, watching "unload the combine" at field 2, save 1): "it is the box around the equipment it is not colliding with the harvester". ALONGSIDE_PATH_BLOCKED named `mf8570_main_component1` every time while the trailer was lined up fine beside the combine; the rig "lined up 5 or 6 times" and kept moving stuff around.

**Why:** the planning sweep is built from size boxes (whole rig width, margins), which are bigger than the real bodies; it refused good line-ups and the retry loop then circled.

Same for LOAD/UNLOAD (2026-10-31): the MT635 3 m off the deck's line refused every load (size-box width 5.46 m, then an 8 m vehicle-distance rule). User: "disable the blockers while loading and unloading"; "the only distance that should matter is the centerlines from the trailer deck to the tractor/equipment center, not what is in eye sight of the trailer". Now: load pre-check static obstacles onl

**How to apply:** when a pre-motion box check contradicts what the user sees, fix the check (exclude what the motion is meant to pass beside, rely on the live envelope-contact watch during the motion) instead of adding retries or moving equipment around. Keep retries bounded (one per side). Related: [[reuse-working-pieces]], [[whole-processes-first]].
### autonomous-reload-timing.md

2026-10-31 user: "you are starting to depend on me to reload it everytime again" — run the reload autonomously. Timings: menu clicks < 1 s apart; back to the main menu ~5 s; game loading screen ~45 s (allow a full 60 s); game save ~5 s. "The old process was too slow."

**Why:** waiting on the user for each reload stalls the loop; the user supplied the timings to make it fast.

**How to apply:** after installing a mod change, save, quit to the main menu and load save 1 by the reload script with those waits, then continue the live test; still one reload per batch ([[batch-reloads]]). This supersedes the earlier "don't drive my screen" note for reloads. Related: [[live-loop-autonomy]], [[live-fs25-test-loop]].
### batch-reloads.md

Fix everything possible before one reload of the saved game: collect the failures from a live run, fix them all (field work, vehicle control, driving, unfinished pieces) and install them as one mod update, then reload once. Never reload for one simple change.

**Why:** reloads cost minutes each; the user wants the existing large pieces fitted together as a whole, not shipped piece by piece.
Repeated forcefully 2026-10-31 after one-fix-per-reload loading attempts: "this one small change at a time has to stop ... i need to be able to use my pc without having my screen ripped from my use or constantly being asked to reload". Don't drive the user's screen with menu clicks while they use the PC; ask for one reload per batch. When a pre-check refuses something the user says is fine, remove

**How to apply:** after a reload, run every live step the loaded build can do (keep going past failures where safe), note all problems, then batch the fixes plus pending features into one commit set, run the gate, install, reload once. Companion-only changes still need the build to match, so batch them too. See [[live-fs25-test-loop]], [[reuse-working-pieces]].
### combine-long-trips-transported.md

User (2026-10-31), stopping "take the mf 8570 to field 90" (about 800 m across the river on save 1): "do not drive the combine to field 90 it only travels at 16mph that is to far and should be transported".

**Why:** slow self-propelled harvesters on long road trips waste time; a real farm hauls them on a transport.

**How to apply:** don't send a combine on long self-driven trips. For tests, make a nearby field harvest-ready instead (plow/seed/fertilize, then advance days by sleeping). Combine transport (truck + lowboy/header trailer) is a future capability: build it from the existing haul (load/unload onto a deck) rather than new driving. Related: [[reuse-working-pieces]], [[put-equipment-away]].
### field-arrival-edge.md

The user (2026-10-29): "anywhere along the edge of field is a win; it does not have to reach a point unless it is at the farm, the store, sell points, productions, barns etc." Then: keep field arrival on the actual field boundary/inside-field result with only a small rig-aware tolerance, never a map-specific point.

**Why:** requiring GIANTS' field position made a loaded rig fail 3.04 m short at Field 8 after a correct drive.
**How to apply:** field goals use GIANTS Field polygonPoints: aim at the nearest edge point; complete when any rig unit is inside or within 3 m of the edge (companion/standalone.py `reached`). Places keep a 3 m point. Related: [[map-agnostic]].
### field-creation-and-merging.md

User (2026-10-31): "farmhand needs to be able to combine fields with the plow and create field enabled also" — "i mean both adding 2 fields together as one and plowing new land to create new fields".

GIANTS mechanism (read from installed-datas `vehicles/specializations/Plow.lua`): `Plow:setPlowLimitToField(false, noEventSend)` (316-) is the in-game ALLOW CREATE FIELDS toggle; it creates field ground only with `g_currentMission:getHasPlayerPermission("createFields", ...)` (onStartWorkAreaProcessing 576-581); `getPlowForceLimitToField` true when `Platform.gameplay.canCreateFields` is false. Cult

2026-10-31 live: joining fields 4 and 5 is ONE ~6-10 m strip; the plow is 25 m working width (shown in the vehicle and AI setup menus). User: "always go by working width" — never the size box (it said 1.67 m and gave nine lanes); never plow a curve ("looks like a boomerang"): lower only lined up on a straight lane.

**How to apply:** GIANTS' field-work helper only works inside a field's polygon, so plowing the strip between two fields / new land is a gap: plan it as a bounded Farmhand drive pattern (or a provider if one exists, e.g. Courseplay is not installed) with the plow's limit-to-field off, then restore it. Record the API in adapters/EVIDENCE.md before calling it. Not started yet; ask the user for the t
### fs25-no-coroutines.md

FS25's Lua does not support coroutine.yield: Courseplay's FS25 source says so and stubs it (toolkit `references/provider-sources/Courseplay/scripts/pathfinder/HybridAStar.lua:46`, commit 150dcd5). Courseplay instead keeps its open list across updates and returns "not done" after 20 ms (`HybridAStar.lua:632`); GIANTS does the same with `getTimeSec()` budgets (`OrchardBuildRequest.BUILD_TIME_BUDGET 

**Why:** 2026-10-29, Farmhand's synchronous route search (mod/FarmhandDrive.lua) stalled the game 9.5 s; the companion's STOP was lost and the rig drove on with no job (live bb8dc55381b14a54-1).
**How to apply:** never fix a game-frame stall with coroutines; make the search resumable (persist heap/state on the session, budget per frame) like Courseplay. Related: [[mod-functions]].
### live-fs25-test-loop.md

Live FS25 tests work end to end since 2026-10-18: `python scripts/fs25_live_test.py status|install|launch|restart|hitch|collect` through the user's host runner (started by the user outside the sandbox; reinstall with `install-runner` after runner changes — the client rejects an older runner's answers).

Facts learned live:
- `install` needs FS25 closed and no op can close it alone; the user must close it (or a stop-fs25 op would be needed). `launch` of the GDK exe works; the user then loads the save (no --savegame autoload used yet).
- /mnt/c is readable from the sandbox: companion.log and FS25 log.txt can be read directly for diagnosis; mods zips (AutoDrive, FollowMe) can be unzipped to $TMPDIR to read their source.
- The user's save loads FS25_AutoDrive and FS25_FollowMe; AutoDrive leaves `ad = {}` on implements (fixed in 8f518db).
- A seated player makes LOCAL_MANEUVER refuse (SEATED_DURING_MANEUVER); the selector prefers the ridden tractor. Ask the user to be on foot.
- Tractor 170 never turned during LOCAL_MANEUVER (DIVERGED). Proven 2026-10-18 with STEER diagnostics (a19bdee): rotatedTime follows driveToPoint, but vehicle.isActive stays false (Vehicle:update sets isActive = getIsActive(), false for an unentered vehicle without AI job or forceIsActive), and WheelPhysics:serverUpdate only calls updateSteeringAngle when isActive, so wheels stay at 0 while the mo
- FIXED in a6d874d (forceIsActive held while LOCAL_MANEUVER/ALIGN_REVERSE drive): live test aa3b6a85654042b6-1 COMPLETE from the failing pose — wheels steered to -33 deg, heading turned 85 deg, LINED_UP lateral 0.12 m, reverse to gap 0.60 m, ATTACHED, VERIFY.
- Each relaunch without saving reverts the save; the user must drive tractor 170 back out to recreate a LOCAL_MANEUVER geometry.
- stop-fs25 now exists (ce64540); stop-fs25 → install → launch runs without the user, who then only loads the save.
### live-loop-autonomy.md

Keep the host runner persistent. Do not ask the user to reinstall/restart it unless the runner itself or its protocol actually changes (and even then, batch it). For normal Farmhand edits run the whole cycle alone: edit → test → stop-fs25 → install → launch (save 3 autoloads) → poll status for fresh READY → live request → collect → repair → repeat. Save 3 is the qualification bas

**Why:** user said so 2026-10-18 after repeated reinstall/reload requests.
**How to apply:** see [[live-fs25-test-loop]] for the mechanics; runner-only fixes can wait in the repo until a protocol change forces a reinstall.
### map-agnostic.md

The mod must not be map specific: it has to work on every map, even if the user records routes with FollowMe (installed) where GIANTS GOTO / AutoDrive cannot route. Transport trailers' ramps are always lowered and raised by Farmhand (GIANTS Foldable), verified.

**Why:** the user plays several maps (FourFields save 2, Chipping Norton save 3); a coordinator tied to one map's places is useless.
**How to apply:** never hard-code coordinates, place names, field ids or map-specific vehicle names; derive everything from observation (snapshots, belts, Foldable, GIANTS' navigation). Treat FollowMe as a candidate route provider (evidence it in adapters/EVIDENCE.md before use). See [[reuse-working-pieces]], [[batch-reloads]].
### mod-functions.md

Everything Farmhand does (hauling, loading with implements, buying with options, follow me, continue, ...) has to be a function of the mod (mod + companion), reachable by the player's words or keys in game. Live tests must go through that same request path; never deliver a capability as a scripted sequence I run from the sandbox.

**Why:** the user said "these all have to be functions of the mod" while I was building the multi-part transport/purchase request (2026-10-21).
**How to apply:** when a request spans several steps, make sure the language layer understands it as one command (goal or sequence) and the coordinator/executor runs it; qualify live with `fs25_live_test.py request "<the player's words>"`. See [[reuse-working-pieces]], [[map-agnostic]].
### move-the-blocker.md

User (2026-10-30): "if you have problems again stop, review the issue(s), search for a simple GIANTS fix/patch to make the feature work as a whole; if you can not find an easy simple solution to farmhand being blocked in by other vehicles, equipment, trailers, etc then simply have farmhand move the item blocking the way, if its a tractor have farmhand get in it and move it somewhere out of the way

**Why:** hours were lost on routing/egress tweaks around parked rigs (GMC against the 3650, the dropped seeder in the yard lane).
**How to apply:** on a blocked drive, first look for a GIANTS/provider fix; otherwise identify the owned blocker: a drivable vehicle (with its attached rig) is driven to open ground (`reposition`), a loose implement/trailer is hitched by a free tractor and parked out of the way; then resume the original task. Never leave the blocker in a lane. Related: [[reuse-working-pieces]], [[put-equipment-awa
### no-chained-live-requests.md

2026-10-31: a background script sent "load the series 9 onto the gmc" then "take the gmc to field 90" unconditionally; the load failed and the GMC drove off empty. User: "you did not load the series 9 you made the gmc start going".

**Why:** a later step assumes the earlier one's result; running it anyway moves equipment the wrong way in the user's live save.

**How to apply:** in live test scripts, gate each next request on the previous result being `"status":"COMPLETE"` (e.g. `grep -q '"status":"COMPLETE"' && next`). Related: [[live-fs25-test-loop]], [[batch-reloads]].
### one-trailer-is-plenty.md

User (2026-10-30): "every trailer fits under the pipe on the harvester but one trailer is plenty and it will work."

**Why:** Farmhand ranked trailers by free room, sent the 3650 across the river to the Titan trailer under a loading structure, and bought three extra Brantner trailers ($26,000 each) when none could be reached.
**How to apply:** for unloading a combine, pick the nearest usable grain trailer (nearest the tractor/combine), not the biggest; never buy a trailer while a usable one exists; only buy when the farm has none that takes the crop. Related: [[whole-processes-first]], [[riverbend-farm-bridges]].
### player-is-observer.md

While I develop, the user is not playing: a visible, nearby, seated or riding avatar is an observer, never a reason to stop, wait, or ask them to move. Only evidence of conflicting control (steering/throttle/brake input, taking over the machine, another controller, the avatar physically blocking a maneuver's collision envelope) interrupts Farmhand-owned motion. Being seated is not taking control; 

**Why:** the user, 2026-10-22: "My avatar's presence is not a reason to stop development"; I had stopped and asked about a stuck rig while they sat in the Challenger.

**How to apply:** never end a turn or pick a test around the avatar's position; prefer another tractor when the preferred one holds the avatar. Routine save reloads are mine to do (see [[live-fs25-test-loop]]); the save the observations show is authoritative.
### proceed-on-evidence.md

The user (2026-10-29), approving the resumable route search: "Going forward, do not stop to ask me when the evidence establishes a necessary engineering change to achieve the existing Farmhand goal. If a larger internal change is required because smaller fixes have been disproven, proceed autonomously with the smallest correct implementation and preserve existing capability. Only ask me when there

**Why:** I stopped to ask before the Courseplay-style resumable search although live evidence (9.5 s stall, lost STOP) and disproven smaller fixes already made it necessary.
**How to apply:** when smaller fixes are disproven, implement the larger internal change, verify, and report. Still ask for product behaviour choices, destructive/irreversible actions, or goal changes. Related: [[live-loop-autonomy]], [[whole-processes-first]].
### put-equipment-away.md

Farmhand must never detach an implement or leave a tractor just anywhere (fields, roads, field edges). Equipment goes back where it came from, or at least out of the way and put up somewhere (yard, shed, parking). When something is in the road, Farmhand goes around it (GIANTS' navigation plans around obstacles; AutoDrive alone just waits).

**Why:** the user, 2026-10-23, after my recovery commands unhitched the seeder in field 4 (it then blocked GIANTS' roller helper) and the roller in the field: "you can not just detach implements and leave them anywhere or the tractors, they need to be put away back where they came from or at least out of the roads, fields, etc." and "if there is something in the road have farmhand go around it".

**How to apply:** an "unhitch"/detach in a field or away from the item's home first moves the pair home (origin, else the farm yard), then detaches; origins must survive companion restarts; in live recovery sequences stop at the first failed step instead of running the next command blindly. Related: [[whole-processes-first]], [[player-is-observer]].
### reuse-working-pieces.md

Do not reinvent working pieces or parts (user, 2026-10-20). Farmhand is the coordinator between GIANTS and proven installed mods (AutoDrive, Universal Autoload, FollowMe, Courseplay if installed, belts/winch mods), plus Farmhand's own already-working code (bridge, executor, hitch, PULL_CLEAR, UNPARK, field work, buying, live-test loop). Custom code only for the gaps between them.

**Why:** the user's completion plan (2026-10-20) and direct instruction; rebuilding working parts wastes effort and risks regressions.
Repeated 2026-10-30, sharper: "you waste time trying to build pieces that are already made... not rebuild farmhand every other fix"; "stop playing games with the routing". Concretely: long travel = GIANTS GOTO / AutoDrive (they know roads and bridges); Farmhand's own drive only for the short tight parts (leaving a cramped start, final approach, hitch line-up). Unloading a combine = AutoDrive's unl

**How to apply:** before writing anything, check for an existing Farmhand function, a GIANTS mechanism, or an installed mod interface that already does it; extend or call it. Save 2 (FourFields) loads 109 mods incl. UAL, FollowMe, AutoDrive, GMC 3500HD, FS25_UGR1124, Autoload_Belt, StiffBelts, winch, towing chain; Courseplay is NOT installed. See [[live-fs25-test-loop]].
### riverbend-farm-bridges.md

2026-10-30: on save 1 (Riverbend Springs) the farm yard (~(-730,140)) reaches the Titan trailer (~(-815,455)), the shop (~(-130,-160)) and the EDK 650 (~(-240,-558)) only over river bridges (decks ~50.0 m over terrain 33-48 m). GIANTS GOTO reports not_reachable from the yard; AutoDrive's saved network has no farm-to-shop path.

Fixed in 6163425 (live: 3650 ARRIVED at the Titan trailer over the bridge): road-deck surface read by a downward ROAD-only raycast, coarse grid tries points across blocked cells, a due departure drives first, steered reverse for a tractor alone in shuffles, shuffles may start inside their margin.

**Why:** these were the root causes of "no trailer reachable" in the unload-the-combine workflow.
**How to apply:** rerun "unload the combine" on save 1 after drive changes; the 3650 starts boxed between a tree (-731,144) and its dropped SOLID STAND 1500. Next open item there: hitching at the Titan trailer (LOCAL_PATH_HITS_WAGON, STAGE_GOTO_INVALID). Related: [[riverbend-yard-pocket]], [[riverbend-yard-unreachable]].
### riverbend-yard-pocket.md

2026-10-29: the GMC rig (radius ~11.2 m) used to end NO_ROUTE boxed in the Riverbend farm yard (pocket at about (-720,107)) and at the red farm-entrance arch west of the barn (lane at about (-786,134), straight motion only). Barn trips aimed at one 20 m standoff on the barn's west side, which is fenced in.

Resolved in 5e1b838 (live COMPLETE): places use any clear approach point within 40 m (one multi-goal corridor search); bounded forward/reverse shuffles; with no way onto the corridor, avoid where it led and shuffle onto another or back straight out; inner-side-only turn widening. Runs: pocket+gate to Field 2 7fd526481ca24c1c-2, Field 2 to barn 30d8c834efe54a3c-2, barn to Field 4 3703bb62ebd94f1b-1

Still open: by Field 2 at about (-858,129) heading south, something no probe sees holds the rig fast (stopped dead at 6.6 km/h; the user could not drive it out either and reset the save). The held-fast mark now handles a stall, but recovery from one is not yet proven live.

**Why:** these are the live confined-place regressions for routing on save 1.
**How to apply:** rerun them after planner changes; don't treat the Field 2 catch as a planner bug without new evidence. Related: [[riverbend-yard-unreachable]], [[field-arrival-edge]].
### riverbend-yard-unreachable.md

Live 2026-10-23 on save 1: from the west road by the "Farm Barn" AutoDrive marker (-799,120), GIANTS GOTO ended NOT_REACHABLE (or GOTO_INVALID) for every spot tried around the Shed (-743,160), the fill station and the petrol tank, for both the 3650+seeder and MT600+roller rigs. AutoDrive's network has no lane into the yard (only an isolated 4-point stub 12092-12095 by the silo). Farmhand's `ad_lan

**Why:** explains why "put away" from the west road fails on this save; not a code bug in spot selection.

**How to apply:** don't re-run the same GOTO/lane attempts from the west road; either the player records an AutoDrive route into the yard, or integrate AutoDrive's PathFinderModule (startPathPlanningTo) to compute a path and record it. Related: [[put-equipment-away]], [[reuse-working-pieces]].

Out of the yard too (live 2026-10-31, "take the gmc to field 90", GMC + deckover + Series 9 at about -740,100): GIANTS GOTO "agent state blocked" after 90 s every time, even after moving the 3650 and after teleport recovery set the rig down 4 m away. `getIsPositionReachable` says yes inside the pocket, so it is no sign GIANTS can start there. The teleport itself worked (rig + belted cargo moved an
### speed-limit-40mph.md

Farmhand must never drive faster than 40 mph (64 km/h) on any travel leg it starts (AutoDrive, GIANTS GOTO/Deliver helpers), slower where the provider allows for corners and bumps.

**Why:** the user, 2026-10-22: AutoDrive took the loaded GMC + Deckover at up to 103 km/h, it lost control over train tracks and ended stuck against a wall (live b8e4fcf886464f0a-1 / 344392c02cd942ff).

**How to apply:** set the provider's own speed limit through its supported setting before starting it, verify it from the observed speed in the drive diagnostics, and record it in AGENTS.md. Related: [[live-fs25-test-loop]].
### whole-processes-first.md

Stop repairing/hardening pieces that already work and stop building features from scratch. Find the easiest, smallest, fastest fix/bridge/patch; the game (GIANTS hired workers, AI jobs, shop/sell APIs) and the installed mods (AutoDrive, FollowMe, UAL, ...) already provide nearly everything. Farmhand is the dispatcher that strings them into whole processes and gets them working as a unit.

Whole processes the user expects from one request:
- Field cycle: harvest if ready, collect/forage straw if any, plow/cultivate, sow, fertilize (skip if the seeder applies fertilizer itself), roll, fertilize again if needed.
- Hauling: buy if missing, hitch, load, travel, unload, do the job(s), park tool and tractor where they belong, load the trailer back and return it to the farm / its parking spot.
- Buy AND sell for products, equipment, vehicles, land.
- Dispatch other workers (GIANTS helpers) for field work and anything else the game provides, in parallel.

**Why:** the user, 2026-10-23, after a session spent re-fixing STOP/AutoDrive details and hand-building a back-up feature while barely testing: "you are still barely testing anything... stop wasting time repairing everything that already works, finish the implementation of the remaining parts and get everything to work as a unit."

**How to apply:** before coding, check what GIANTS/mods already do and call that; prioritize missing glue for the end-to-end processes over fixing edge cases; batch, install, test live through the player's words, move on. Related: [[reuse-working-pieces]], [[mod-functions]], [[batch-reloads]].
