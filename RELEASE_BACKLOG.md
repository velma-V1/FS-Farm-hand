# Release backlog (temporary - delete at release)

Temporary files (RELEASE_BACKLOG.md, PRODUCT_WORKING_MEMORY.md, AGENT_MEMORY_NOTES.md, scripts/reload_save1.sh) are deleted before release only by the human or on an explicit request to a model - never by any automatic flag, queue, script or process.

Purpose: capture work that must be fixed, verified, polished, or explicitly scoped before first production release without derailing the current active task. Remove completed entries instead of accumulating history.

## Complete final-product release contract — owner directive 2026-11-05

Status: REQUIRED / NOT FULLY QUALIFIED
Priority: AUTHORITATIVE FINAL RELEASE CONTRACT — refer to FINAL_FARMHAND_PRODUCT_SPEC.md.

No narrow navigation-only build counts as the finished product. Required:
- Concurrent independently controlled workers and coordinated fleet/resource/space scheduling with named/all STOP, persistence, fast autonomous recovery and performance scaling.
- Moving combine unloading WHILE harvesting, synchronized pipe and cart, multiple carts and outgoing storage/sale logistics. Stationary unload alone does NOT pass.
- Owned field observation including growth, stones, rolling, weeds, fertilizer and ready-to-harvest; contracts listing/accepting/completing with leased/owned equipment and payment.
- One-command individual field tasks and complete farm/field/animal/production workflows, including buy/sell/lease/refill/repair and full material handling.
- Forestry/trees/selectable log lengths, functional fast stacking, chainsaw/stump disposition; 15–20 s target on suitable equipment/timber.
- Specialist vehicles and attachments, Lizard 379/lowboy/Cat 395 logistics, loader/quarry/mines, dozers/excavators/landscaping, sensible structures/placement where real game mechanics permit.
- Short/poorly structured commands, goal-driven Full/Semi/Advisory/Manual modes, optional multiple character roles, fast planning/first action/recovery, 99% measurable task/location precision goals.
- No AutoDrive participation in Farmhand jobs, ordinary standalone operations, truthful physical verification and good frame/CPU performance.

Track REQUIRED, IMPLEMENTED, AUTOMATED-TESTED, LIVE-PROVEN or BLOCKED BY VERIFIED ENGINE/MAP CAPABILITY separately. Do not remove required scope merely to pass tests; use practical engineering and live evidence.

## Towed-rig tight-space recovery
Status: OPEN
Priority: MEDIUM
Found: 2026-11-04 (live 8611351ac09e4a1b-1)

Navigation is GIANTS' engine agent under Farmhand's own loop for routes (proven live with the loaded four-unit GMC rig), Farmhand's own drive for exact local poses. A Farmhand-planned long route for a towed rig is not a release requirement. What remains: a towed rig with its nose against an object and no forward way (the GMC at (-980,-14)) can leave only by the bounded straightening reverse; towed reverses are not planned. Address if a live workflow is blocked by it.

## Turn around in place: live confirmation
Status: OPEN
Priority: LOW
Found: 2026-11-04 (live f460004990284cd1-1)

Implemented (bcc2c60, rigid-rig fix ae86dff): within 40 m, rigs with no towed unit are searched with reverse moves from the start. Unit-tested; the next near-goal live drive should log "turns round in place" instead of a loop.

## Reposition heading: live confirmation
Status: OPEN
Priority: LOW
Found: 2026-11-04 (live 8611351ac09e4a1b-1)

1d9a87f: every reposition now arrives at the heading its spot was found clear at (the exact goal curve plans it). Confirm on the next live reposition: arrival at that heading, no contact.

## Scenery ghosting: prove the need or remove the option
Status: OPEN
Priority: HIGH
Found: 2026-11-04

Pairwise scenery-collision ghosting is now only the player's opt-in setting "Pass through scenery in tight spots" (settings.json `scenery_ghosting`, default OFF). Ordinary hitch steps and drives keep every collision; the command carries `gh="1"` only when the setting is on, `Hitch.ghost` refuses without that permission, and the player is notified when it starts. The lab flag `Drive.PROOF_NO_GHOST` is gone. Not yet live-run.

Next:
- With the setting OFF, rerun the cases that originally motivated ghosting under normal physics (structure leg 1682e794a2ee4476-1, posts 13f374b86b594744-2, "collisionNonAIBlocking" beside the Titan trailer d053a7e5822a4489-1, hitches near buildings).
- If each completes, or fails for a cause a geometry/approach fix removes, delete the option and `Hitch.ghost`.
- If one genuinely needs it, keep the option, record its exact trigger, and verify restoration on success, failure, STOP, cancel, takeover and link loss.

Acceptance:
- default gameplay never ghosts (verified in the game log: no "collision ... off" lines with the setting off);
- the option is removed, or retained with one recorded qualifying case and verified restoration.

## Heavy cargo secure mounting
Status: OPEN
Priority: HIGH
Found: 2026-10-31

GIANTS TensionBelts.lockTensionBeltObject already creates a joint for vehicle cargo, but the joint uses spring constraints. Heavy tractors/excavators can still creep or move on a trailer.

Next:
- Treat this separately from the GIANTS navigation obstacle fix.
- After loaded-transport navigation is proven, test a zero-play/stronger Farmhand secure-mount using GIANTS JointConstructor/DynamicMount conventions.
- Do not attach cargo through normal AttacherJoints.

Acceptance:
- Heavy equipment remains fixed relative to the trailer during transport.
- Manual/Farmhand unstrap restores normal independent physics.
- Save/load, teleport recovery, selling/deleting, multiplayer/server cleanup, and entering the mounted vehicle behave correctly.

## Final no-external-mod qualification
Status: OPEN
Priority: RELEASE-BLOCKER
Found: 2026-10-31

Farmhand's core product must operate as Farmhand mod + companion + native GIANTS systems. AutoDrive, FollowMe, Courseplay, UAL, and other gameplay mods may accelerate capability but cannot be required.

2026-11-05: AutoDrive removed from Farmhand's execution entirely (travel to markers, loaded-rig delivery, transport staging, marker creation, network linking, sell fallback, combine unloader, speed-limit and wage writes). Farmhand only reads whether the player's own AutoDrive drives a vehicle, so it never takes it. Still to run live: the representative set below with this build.

Acceptance:
- Run representative movement, transport, hitch/unhitch, fieldwork, unload/sell/store, and recovery commands with external provider mods unavailable/bypassed.
- No advertised core command returns BLOCKED solely because another gameplay mod is missing.

## Follow-me standalone scope
Status: OPEN
Priority: HIGH
Found: 2026-10-31

Current "follow me" path uses FollowMe when installed.

Decision before release:
- either provide a Farmhand-native follow implementation,
- or explicitly remove follow-me from the standalone core capability set.

Acceptance:
- no hidden hard dependency remains.

## Curve speed and long-rig road behavior
Status: OPEN
Priority: MEDIUM (long legs: GIANTS' agent's own curve speed, maxCentripetalAcceleration, under Farmhand's cap)
Found: 2026-10-31

Farmhand's standalone driver must reduce speed before curves using path curvature/turn radius and whole-rig geometry. Slowing is separate from wide-turn path generation.

Acceptance:
- high speed on safe straights;
- progressive deceleration before bends;
- truck/tractor length, trailer length, articulation, load, steering demand and terrain/collision limits constrain target speed;
- acceleration resumes after the full rig clears the bend;
- loaded GMC/Deckover/Series 9 is a hard regression case.

## Live-verify uncommitted fieldwork pieces
Status: OPEN
Priority: HIGH
Found: 2026-10-31

Current dirty-tree/tests report several changes that still need their own live evidence and isolated commits.

Items:
- unloading while harvesting / harvest transport policy;
- request queue (busy -> QUEUED, max 5, STOP clears it);
- join fields ("add field 4 to field 5");
- worker-watch/recovery for stalled GIANTS helpers;
- unreachable implement teleport-to-field rescue;
- loose-implement field cleanup before hand-off;
- earlier FarmhandDrive search changes, maneuver retries, trailer pass-over codes.

Acceptance:
- each item gets relevant live evidence before its logical commit;
- one workflow never validates unrelated dirty changes.

## Preferred equipment configuration
Status: OPEN
Priority: MEDIUM
Found: 2026-10-31

Settings/product intent includes preferred equipment and variant selection.

Examples:
- GMC and Series 9 variants;
- power, capacity, color, wheels, add-ons, lighting;
- rules such as "cheapest with X" and "most power under Z".

Acceptance:
- preference affects equipment selection without hardcoded save/map IDs;
- buying remains only when explicitly instructed unless product policy is changed.

2026-11-04: nothing saved or read preferences, so the settings screen's "Preferred equipment: N saved" entry was removed (only implemented settings are listed). Re-add it with the feature. The per-purchase spending limit (settings `spending_limit`) is implemented and applies to every store and land purchase.

## Shortest sensible routes
Status: OPEN
Priority: RELEASE-BLOCKER
Found: 2026-10-31

User: "farmhand needs to be able to find the shortest path to complete the task instead of these random routes that are always over complex and long or unnecessary". Seen live: hitch approaches circling, 7-min own-drive wander near the plow, cross-map tractor choice for a nearby job.

2026-11-04 night: long legs now go by GIANTS' agent (map routes, curve speed) under Farmhand's loop; near poses by Farmhand's own drive with the exact goal curve; the AutoDrive staging detour is gone. Remaining evidence of wandering: the load approach searches when the lane behind the ramps is cluttered (relocation now answers it).

Next:
- Choose equipment by total trip (nearest usable unit to the work, not to the player).
- Prefer the straight / direct move when the target is close and lined up (as LOAD now does); cap Farmhand's own drive length/time against the straight-line distance (e.g. > 3x or > 2 min near a target: stop and take the next option, never wander).
- Log planned vs straight-line length for every own drive to find the worst cases.

Acceptance:
- near targets are reached by the direct move; no loops; equipment picked near the job.

2026-11-01 live barn transport: the player's walked gate centre line was selected but the loaded GMC/Deckover/Series 9/plow could not refine the gate section with whole-rig fixed-scenery and grade checks. The line is preserved as route `c996ab0558084c21aea35b93aa026fbc`, unverified and suspended after repeated failures. A later native GIANTS leg delivered the intact rig to the barn (`d7e14850a1a34a21-1`). Do not promote the walked line without a successful full traversal. The gate approach needs rig-aware wide-turn geometry or a different safe route; repeated local reverses and long detours are a release blocker.

## Transport surface preference in live play
Status: OPEN
Priority: RELEASE-BLOCKER
Found: 2026-11-01

Installed GIANTS ground/fruit data let Farmhand distinguish field/crop from non-field ground, and its own planner penalizes field/crop. Native GIANTS GOTO remains a black-box route that may cut fields. Farmhand temporarily sets the current transport rig's native care-wheel flags and restores each original flag on every exit; the live barn run restored 13/13 after `NAVIGATE_STALLED`, and focused tests cover mixed original flags. Live route samples are still needed to establish whether native and Farmhand driving prefer non-field ground when available, and whether any crop was actually affected.

Acceptance:
- representative loaded trips on a crop-destruction-enabled save prefer traversable non-field ground when available;
- unavoidable field transit preserves crop and other ground state;
- completion, failure, STOP, cancellation, link loss and provider handoff restore the exact original wheel flags;
- no hardcoded map/route coordinates or external gameplay-mod requirement.

## Sell equipment
Status: OPEN
Priority: MEDIUM
Found: 2026-10-31

User: sell vehicles / implements / attachments "the same as buying: only when told unless enabled in the settings". Today "sell" is crops only and a named vehicle is refused.

Next:
- Read installed dataS for GIANTS' own sell path (shop SellVehicleEvent / mission vehicle sale) and record it in adapters/EVIDENCE.md before calling it.
- "sell the <name>" -> command `sell_equipment` (named, exact match, never a reserved/working/attached-in-use unit; confirm price in the notice); a settings toggle like the buy one, off by default, for Farmhand-initiated sales.

Acceptance:
- sells only the named item when told; never on its own unless the setting is on; works from Alt+K.

## Own drive cannot hold a slow speed downhill
Status: OPEN
Priority: HIGH (safety, all machines)
Found: 2026-11-05

Live: the MF 8570 reached 17 km/h downhill on a 10-13 degree slope while Farmhand's own drive was in its 3 km/h final approach; it ended OVERSPEED, 56 m from where it was meant to stop. Hitch.drive brakes above its limit but cannot hold a heavy machine on a steep grade.

Acceptance:
- On a slope the commanded speed is held: stronger braking, and refusing or slowing the approach before the grade instead of after the overspeed. Verified live on that slope with a heavy machine.

## Combine transport for long trips
Status: IN PROGRESS (owner priority 2026-11-05)
Priority: HIGH (also the way to the moving-unload gate in save 1)
Found: 2026-10-31 ("do not drive the combine to field 90 ... it should be transported")

Live facts (build 9b9e8b2, save 1):
- MF 8570: 8.2 m long, 3.99 m wide, 3.9 m tall.
- 1124 Deckover belt box: 8.41 m, a narrow fit.
- LB 56 26 lowboy behind the 379: 16.2 m, 12 belts, a comfortable fit.
- MF 8570 Trailer (header trailer): reports no joints and no belts, so most likely GIANTS' DynamicMountAttacher (set the header down in its trigger and unhitch it).
- The combine's cutter joint is dropped by the observer's JOINT_DRIFT_M filter (the feeder house moves it).

Done:
- Hauling picks any driven machine as cargo.
- detach_pair accepts any driven machine.
- The 379 now counts as a driven machine.

Plan (reuse detach_pair, unpark, load_tractor, move_pair, unload_tractor, the haul goal):
1. Record the combine-to-header geometry (the AI root relative to the header's input joint) when detaching. Re-attaching returns to that pose by Farmhand's own drive, then does check-only align plus GIANTS attach. This avoids planning from a joint that drifts.
2. After a front detach, the combine backs clear with UNPARK (nothing attached, so it may reverse).
3. In the haul, a header on a cutter joint becomes a "detach first" step instead of LOAD_IMPLEMENT_NOT_MOUNTED.
4. Optional later: the header onto its MF 8570 Trailer (dynamic mount).

To verify live: isDetachAllowed for the header (lowered?); the height gap at re-attach (Farmhand caps it at 0.5 m); the load fit; belts.

## Unload a combine on the move
Status: IMPLEMENTED / AUTOMATED-TESTED / NOT YET LIVE-PROVEN
Priority: RELEASE GATE (FINAL_FARMHAND_PRODUCT_SPEC.md 5)
Found: 2026-11-05

ESCORT (mod/FarmhandHitch.lua Hitch.newEscort/escortStep) holds a tractor's trailer under the pipe of a combine harvesting under GIANTS' helper, at its speed, and behind it on its trail while it turns or is not threshing. It keeps the cart clear of the combine and header boxes, shifting the slot within the trailer, or NO_SAFE_SLOT. GIANTS' own combine strategy swings the pipe and discharges. The bridge runs escort beside other ops (one controller per machine). The coordinator sends a grain cart after every harvest it starts and for "unload the combine" while harvesting (pause + stationary transfer stays as recovery). A full cart's load goes where the harvest goes, then it returns; on a crop change it ends and another cart is made.

Acceptance (live, still open):
- The combine moving and threshing while grain flows into the cart (game log `escort end ... moving=<litres>` > 0, standing ~0), several lanes including headland turns, no contact, full cart delivered and back.
- First live attempts (2026-11-05, save 1): field 7 was already harvested (Farmhand rightly refused). Field 90: GIANTS' helper could not leave because the MT635 + RZK 300H stood against the MF 8570's header; Farmhand's reposition only reversed it 4 m (see "Equipment left against a combine").

## GIANTS' planner refuses the MF 8570
Status: RECORDED; NOT A PRIORITY (owner direction 2026-11-05: stop fighting GIANTS' planner for one machine)
Found: 2026-11-05

The REACH probe (diagnostic; it never drives) shows GIANTS' agent planner refuses every target for the MF 8570, within 33 ms. That held from three spots, for the combine alone and with the header, at its own width and at 6 m, with room at both ends. The Series 9 plans normally (551 ms). The combine's agent front offset is -3.70 m (the Series 9's is +3.26 m), a likely cause, but taking it as ahead did not change the answer. The REACH probe stays to pick a field start GIANTS can route. Planning narrower than the rig was removed from navigation (owner: never represent equipment narrower than it is).

The proven way stays: GIANTS' helper, then Farmhand's own drive onto the field (_to_field). For long trips the owner's standing instruction applies: transport a slow combine rather than drive it across the map (see "Combine transport for long trips").

## Harvest contract left in the save
Status: NEEDS PLAYER DECISION
Found: 2026-11-05

Before the crop check existed, Farmhand took field 9's harvest contract. Its crop is spinach, and no header on the farm cuts it. The contract is still RUNNING at 0% (field 64's canola contract is also ours and running). Cancelling it may cost a GIANTS penalty, so it is left for the player.

## Equipment left against a combine
Status: OPEN
Priority: HIGH
Found: 2026-11-05

Live 33dfbf22977c4dfa-1: "harvest field 90" FIELDWORK_BLOCKED twice. The MT635 with its sprayer stood nose against the MF 8570 header in its path, and reposition's drive found only a 4 m rig-clear reverse. Earlier the same morning, the stationary under-pipe approach for that combine circled for 16 minutes (a static object at -840,170 and the header), which is exactly the circling the user rejected; it was stopped.

Acceptance:
- What stands in a helper's way is moved far enough for GIANTS' agent to start (its own route, not only a fixed lane), or Farmhand says which machine blocks it.
- A stationary unload gives up quickly with a reason, instead of repeating route searches beside the combine.

## Park dropped implements away from active work
Status: OPEN
Priority: MEDIUM
Found: 2026-10-31

Dropped implements should not obstruct active fieldwork or later worker routes.

Acceptance:
- Farmhand places them in a sensible safe area outside the active work path and can find them later.

## New-field creation
Status: OPEN
Priority: MEDIUM
Found: 2026-10-31

Joining existing fields is being implemented. Creating a new field from plowable land remains a separate later capability.

Acceptance:
- natural-language request creates the intended new field area using GIANTS/Farmhand mechanics without map-specific coordinates.

## Release cleanup and polish
Status: OPEN
Priority: LOW
Found: 2026-10-31

Before first production release:
- remove temporary diagnostics that are no longer useful;
- remove stale/historical guidance that conflicts with AGENTS.md;
- verify installer/setup/uninstall behavior and documentation;
- ensure temporary test/development files are not shipped;
- promote only permanent facts from PRODUCT_WORKING_MEMORY.md into durable docs/code comments/EVIDENCE.md;
- delete RELEASE_BACKLOG.md and PRODUCT_WORKING_MEMORY.md after release.

## Tire track lifetime
Status: OPEN
Priority: MEDIUM
Found: 2026-11-01

Provide a configurable short lifetime for tire tracks. Preserve tracks while fresh instead of disabling them outright; verify cleanup does not change field state or other vehicle effects.

## Chainsaw whole-tree cleanup and reach
Status: OPEN
Priority: HIGH
Found: 2026-11-01

For Farmhand and manual chainsaw work, support whole-tree cleanup: auto-delimb, auto-remove the stump, and preserve equivalent stump wood. Support at least 10 m reach, effectively instant cutting, and trees at least 6 m in diameter. Reuse GIANTS forestry mechanics and verify wood output and cleanup physically.

## Giant sequoia / redwood forestry asset
Status: OPEN
Priority: MEDIUM
Found: 2026-11-01

Add a giant sequoia/redwood forestry asset using GIANTS' existing GIANTSEQUOIA split type where practical. Verify placement, cutting, splitting, transport, and sale without map-specific assumptions.
