# Release backlog (temporary - delete at release)

Temporary files (RELEASE_BACKLOG.md, PRODUCT_WORKING_MEMORY.md, AGENT_MEMORY_NOTES.md, scripts/reload_save1.sh) are deleted before release only by the human or on an explicit request to a model - never by any automatic flag, queue, script or process.

Purpose: capture work that must be fixed, verified, polished, or explicitly scoped before first production release without derailing the current active task. Remove completed entries instead of accumulating history.

## Standalone navigation minimum proof
Status: OPEN
Priority: CURRENT RELEASE-BLOCKER

The current repository is a proving ground. The goal is to identify and live-prove the smallest reusable navigation/physical-world primitives worth carrying into a clean Farmhand build, not to finish the legacy planner.

Minimum proof ladder:
1. complete recursive rig/object geometry;
2. live physical measurement with explicit fallbacks;
3. short-horizon articulated prediction;
4. known aggregate-box false-rejection cases executed under normal physics;
5. constrained local maneuver;
6. cheap global corridor + correct local handoff;
7. command-vs-motion supervision and bounded recovery;
8. second materially different rig/destination;
9. large-route responsiveness and compute/frame budget.

Acceptance:
- no oversized aggregate whole-rig box is authoritative for local clearance;
- no AutoDrive/Courseplay/GIANTS GOTO ownership, teleport or scenery ghosting in standalone navigation qualification;
- movement begins without whole-map high-resolution preplanning;
- destination progress, physical arrival, STOP and verification are measured from live state;
- every retained clean-build primitive is backed by a specific observed failure and live proof;
- legacy mechanisms without proven player benefit are explicitly left behind.

## Articulated route search: live proof and tight-space recovery
Status: OPEN
Priority: RELEASE-BLOCKER (loaded/towed rigs by Farmhand's own drive)
Found: 2026-11-04 (live 48192ed2fe274fc3-1, 8611351ac09e4a1b-1)

159896e/f8355da carry the trailer's heading in Drive.plan, the preview and the straightening reverse, judged by each unit's box (Hitch.rigModel / modelPoses, Drive.trailerStep, matching predictFromRootPose within 2 cm). Live, the search now reports per-unit blocks (`unit1:6`) instead of the rectangle's phantom scenery, but no Farmhand-driven move of a towed rig has yet been planned and completed by it: the qualifying GMC haul (5d29c5999c864530-1) was driven by GIANTS' navigation.

Next:
- A towed rig by Farmhand's own drive (navigation-test mode on) over a bend and into a field entrance: planned vs observed trailer heading along the path, contacts, frame cost of four unit queries a metre.
- Towed rigs plan no reverse moves: with the nose against an object and the trailer behind (the GMC at (-980,-14)), only the bounded straightening reverse can leave. Decide from live evidence whether planned towed reverses (the reverse kinematics are the same step) are needed.
- The Dubins goal shot (757f4bd) is unit-tested and its steps rebuild as follower arcs; a live hitch approach by Farmhand's own drive with it is still to be observed.

## Reposition "open ground" arrives at any heading
Status: OPEN
Found: 2026-11-04 (live 8611351ac09e4a1b-1)

starters.reposition proves a spot clear for the rig at `tyaw` but sends anything but a combine there at any heading; the GMC rig ARRIVED 18 m on with the truck's nose touching LOD0(-985,-12). Candidate: arrive at `tyaw` (the exact goal shot now plans a heading without lattice loops) or prove the spot at the heading the drive will arrive with. 21a5e70740024850-1 (heading correction at arrival under the old planner) must be re-checked first.

## One controlled point for planning, clearance and following
Status: OPEN
Priority: RELEASE-BLOCKER (narrow-gap navigation)
Found: 2026-11-04

The lab-proven steering-reference predictor (collected/proven_systems/measured_short_motion.lua) is not in production. Code reading shows why promoting it alone is not enough: the follower aims GIANTS' steering node at the planned root path, while planning, prediction and the aggregate probe assume the root follows it - an expected ≈a²/(2R) outward root offset in bends (≈0.2 m GMC, ≈0.5–0.7 m 379; adapters/EVIDENCE.md "2026-11-04 production follower reference mismatch").

Next:
- Run the single-bend discriminator in that entry on the GMC and 379 rigs (normal physics, no ghosting).
- If confirmed, change candidate generation, articulated prediction, probe poses and the follower's aim/progress together to one controlled point; qualify on both rigs and the 2 m / reverse-left fixtures.

Acceptance: planned-versus-observed unit poses through a bend within the articulated clearance pad on both rigs; no regression of the proven local maneuvers.

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

## Loaded transport standalone
Status: OPEN
Priority: RELEASE-BLOCKER
Found: 2026-10-31

GIANTS GOTO reports BLOCKED from the first frame when a vehicle is belted on its own trailer (live d14230aa9fb24d30-3, abe8eb41ca6f4b5f-1).

2026-10-31: proven with the cargo obstacle removed — GMC + Deckover + Series 9 (no plow) to field 90 by GIANTS navigation COMPLETE (1e27b06900b64385-2). Left: isolate/commit the FarmhandBridge cargo hunks; repeat with the plow mounted.

Next:
- Prove the cause with the smallest change: while Farmhand owns the GIANTS transport job, temporarily suppress only the belted cargo vehicle's own AIVehicleObstacle registration.
- Keep straps, physics, and trailer attachment unchanged.
- Restore the cargo obstacle on every exit path: completion, failure, STOP, cancellation, timeout, user takeover, deletion/unload cleanup.

Acceptance:
- AutoDrive unavailable/bypassed.
- "take the gmc to field 90" completes with GMC + Deckover + Series 9 + plow intact and strapped.
- No core transport command fails solely because AutoDrive is absent.

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
Priority: HIGH
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

## Bounded-area measurement

Live abf1610fc62543be-1 (2026-11-04): the mod stopped GIANTS' helper for a 400 m2 test area
when its measure (any ground/spray share change x field area, `Bridge.changedArea`) reached
~414 m2; the companion then measured the operation's own share growth (192 m2) and reported
PARTIAL_COVERAGE although the work was done. Two different measures decide stop and acceptance,
both from ~112-124 field samples (~200 m2 each), the after-reading with extra track samples
(another denominator). Needed: one measure for both, at a resolution finer than the asked area
(e.g. samples along the worked track compared before/after on the same points). Do not widen
COVERAGE_REQUIRED to hide it.

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

## Combine transport for long trips
Status: OPEN
Priority: MEDIUM
Found: 2026-10-31

Do not drive a slow combine long-distance when transport is appropriate. Haul it using suitable transport equipment and preserve headers/attachments safely.

Acceptance:
- Farmhand can select/prepare/load/haul/unload the combine for a long trip without manual user work.

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
