# TEMPORARY PRODUCT WORKING MEMORY - delete after first production release

Temporary files (RELEASE_BACKLOG.md, PRODUCT_WORKING_MEMORY.md, AGENT_MEMORY_NOTES.md, scripts/reload_save1.sh) are deleted before release only by the human or on an explicit request to a model - never by any automatic flag, queue, script or process.

Purpose: preserve only high-value facts that would be expensive to rediscover after chat/session compaction. This is not a chronological log. Current git/live evidence overrides stale historical notes.

## Final product directive (2026-11-05)

- FINAL_FARMHAND_PRODUCT_SPEC.md now preserves the owner's complete desired end-product; AGENTS.md points to it. Do not shrink the objective to a proving-ground subsystem.
- Mandatory concurrent physical workers, moving combine unloading as release gate, live field/contract/world awareness, full farming cycles, forestry, chainsaw/stump policies, 15–20-second suitable-tree handling target, excavators/dozers/quarries, sensible construction, animals/production/economy, natural language even badly structured, optional role-based characters, all autonomy modes and rapid recovery. Aim at measurable 99% intent/location/outcome accuracy and minimal end-to-end time.
- AutoDrive cannot execute Farmhand operations; only permitted read-only ownership observation avoids taking over independently player-driven AD.
- Player's latest reported seat: Boss Hoss Series 9, but live observation must take precedence on every current job.

## Authority and context recovery
- Read AGENTS.md first; it is authoritative.
- Read CLAUDE.md for the current proving-ground operating method.
- Current explicit user direction + AGENTS.md + current git/live evidence override this memory.
- There is no handoff file. Do not recreate one; durable state belongs in code, tests, evidence, git, this concise memory, or the backlog.
- Active Claude session JSONL:
  /home/xxthatguyxx/.claude/projects/-home-xxthatguyxx-projects-FS-Farm-hand/0f704052-b444-4e73-abda-33a7b4a83abe.jsonl
- That JSONL contains the prior compaction summaries and can be mined to recover older decisions, failures, live evidence and pending work instead of rediscovering them.
- Keep this file concise; use the JSONL as cold archive when necessary.

## Product architecture
Farmhand = FS25 mod + Farmhand companion program.

Standalone means:
- GIANTS native FS25 systems are allowed.
- External gameplay mods are not required for core Farmhand capability.
- AutoDrive is not used by Farmhand at all (removed 2026-11-05: no travel, markers, network edits, sell, unloader, or settings writes). Farmhand only reads whether the player's own AutoDrive is driving a vehicle, so it never takes that vehicle. FollowMe, Courseplay, Universal Autoload and similar providers may be optional accelerators only.
- Provider limitation/failure != Farmhand limitation/failure.
- The companion is not only AI-model communication; it owns deterministic command interpretation, planning/orchestration, state/verification/recovery and IPC. Ollama/llama.cpp AI is optional.

Core behavior:
USER GOAL -> UNDERSTAND -> OBSERVE -> RESOLVE -> PLAN -> ACT -> VERIFY -> RECOVER/REPLAN -> COMPLETE

Do not hardcode map, field, coordinate, vehicle or route details into product logic.

## Engineering discipline
- KEEP CAPABILITY; MINIMIZE IMPLEMENTATION.
- ONLY BETTER, NEVER WORSE.
- Stay on the actual failing user command until fixed.
- Prefer the smallest general fix/adapter that retains capability.
- Do not reset/stash/clean/discard proven dirty work without evidence.
- Do not manually work around a Farmhand capability failure.
- One successful workflow never validates unrelated dirty changes.
- Logical change acceptance: focused tests -> relevant live evidence -> full gate when appropriate -> inspect diff -> isolated commit -> install exact build -> final live verification.
- Use varied equipment/routes/tasks after a regression is proven; do not overfit one field/map/rig.
- During a live run, do not mutate the code under test or issue competing live commands.
- Teleport is bounded last-resort recovery, not normal navigation.
- When physically blocked, behave like a competent worker: move the blocking vehicle/implement safely, then resume the original objective.

## FS25 reference authority
Toolkit:
/mnt/c/Users/Matt/Desktop/FS25 Modding Toolkit

Reference priority:
1. exact installed FS25 dataS 1.25.0.0;
2. Community LUADOC;
3. UsedPlus reference;
4. FS25 skills/guides;
5. community dataS;
6. provider source;
7. web.

Read exact GIANTS/provider source before relying on undocumented behavior. Record durable findings in adapters/EVIDENCE.md when they become implementation dependencies.

## Loaded transport / standalone gap
Live evidence:
- GIANTS helper/navigation agent becomes BLOCKED from the first frame when a vehicle is belted on its own trailer.
- Evidence includes d14230aa9fb24d30-3 and abe8eb41ca6f4b5f-1.

Exact installed FS25 1.25.0.0 findings:
- AIDrivable:createAgent() builds the vehicle navigation agent from the driven root vehicle plus attached implements.
- The Deckover is an attached implement and is therefore represented in the towing rig.
- A Series 9/other vehicle secured by TensionBelts is mounted cargo, not a normal AttacherJoints implement.
- AIVehicleObstacle independently registers each eligible vehicle component with g_currentMission.aiSystem.
- AIVehicleObstacle:getNeedAIVehicleObstacle() defaults to true.
- It exposes removeAIVehicleObstacle(), createAIVehicleObstacle() and setAIVehicleObstacleStateDirty().

Current hypothesis:
The belted cargo vehicle remains an independent AI navigation obstacle touching/inside the towing agent, causing immediate BLOCKED.

First proof/fix:
- for a Farmhand-owned GIANTS transport job only, temporarily suppress the belted cargo vehicle's AI obstacle;
- keep belts/physics unchanged;
- restore it unconditionally on every exit path;
- rerun "take the gmc to field 90" with AutoDrive unavailable/bypassed.

Do not build a new long-distance navigator until this cheaper GIANTS classification fix is disproven.

## TensionBelts / heavy cargo physics
Exact installed source finding:
- TensionBelts.lockTensionBeltObject() already creates a physics joint for vehicle cargo.
- It locks rotation but uses spring constraints; this can permit heavy equipment to creep/move.
- It also manages reduced component mass/dynamic-mount state/sale restrictions while secured.
- TensionBelts.freeTensionBeltObject() removes the joint and restores state.

DynamicMountUtil exists and provides stricter GIANTS mounting primitives.

Important separation:
1. AI-obstacle suppression solves/qualifies GIANTS navigation.
2. Stronger/zero-play mounting solves heavy cargo creep.
Do not bundle these until the navigation cause is proven.

Do not attach cargo as a normal implement. That risks root-vehicle ownership, AI preparation, entering/driving the cargo vehicle and save relationship problems.

Any future secure-mount implementation must handle:
- save/load reconstruction from belt state;
- manual unstrap;
- Farmhand UNLOAD;
- teleport recovery release/restrap;
- selling/deleting;
- server/multiplayer synchronization;
- entering the mounted cargo vehicle;
- mass transfer and braking/curve behavior.

## Current standalone-navigation proof findings
These facts are current evidence for the proving-ground program; they are not a requirement to preserve the legacy planner.

- 2026-11-04 production branch: the hidden `nav_proof.flag` file no longer does anything. The same mode is Settings (Left Alt + N) → Developer options → "Farmhand's own driving only (navigation test)" (settings.json `navigation_proof`); the main list says "Developer options: navigation test on" while it is on. Scenery ghosting is off unless "Pass through scenery in tight spots" is turned on.

- Live local checks proved the aggregate whole-rig rectangle can falsely reject usable articulated space: a 2 m right turn and a short reverse-left were BLOCKED by the aggregate box while separate observed-unit boxes were clear.
- Separate rigid unit boxes are not yet a proven future-motion model; future clearance must account for articulation through the candidate motion.
- The rig footprint traversal previously stopped at belted vehicle cargo and omitted implements attached to that cargo. A focused regression reproduced three units instead of four; the generic recursive traversal fix includes the nested plow.
- A live next-frame preview incorrectly substituted the angled trailer heading for the GMC heading, rotating the predicted rig by roughly 43 degrees and creating a false collision. Advancing the truck from its observed heading removed that live blocker.
- For the current Deckover configuration, live wheel/joint geometry measured hitch-to-active-wheel-group center at 9.111 m. Hitch-to-root (7.314 m) is not the trailer wheelbase. The 9.111 m value is a measured property of this rig, not a universal constant.
- A simple short-horizon articulation estimate using the 9.111 m geometry matched one recorded turn to about 1 degree, but that sample used scenery ghosting and therefore is only provisional prediction evidence.
- 2026-11-02 Save 1 current-state reproduction: with `nav_proof.flag` and no scenery ghosting, the complete GMC rig moved under Farmhand-owned control. Read-only shadow probes at 22:37:18.637 and 22:37:34.413 showed aggregate OBSTACLE while separate observed-unit boxes were clear for nearby curved candidates. The particular candidates were not executed, so this proves the false decision disagreement but not articulated clearance. Evidence: `.fs25-dev/evidence/2426101921fc4f71-2/`.
- 2026-11-02 bounded 2 m right-turn proof from a reproduced Save 1 pose: aggregate BLOCKED, sampled four-unit articulated clearance CLEAR, integrated `Drive.routeProbe` CLEAR, then two normal-physics executions from the same saved pose moved the intact rig 1.938 m without a final contact-query hit. Truck prediction error 0.346–0.347 m / 0.02094 rad; Deckover 0.371–0.375 m / about 0.018 rad. Evidence: `.fs25-dev/evidence/articulated-turn-20261102/game-log.txt` and `adapters/EVIDENCE.md`. The 9.156 m hitch-to-wheel-representation average is experimental; active wheel-group centre remains unproven. Original 22:37 pose and exact clearance margin remain unverified.
- 2026-11-03 reverse-left from that saved GMC pose: -0.5 m / -0.0899167 curvature, aggregate BLOCKED, four articulated units and `Drive.routeProbe` CLEAR, physical reverse completed without a final contact-query hit. GMC error 0.108 m / 0.01109 rad, Deckover 0.079 m / 0.00779 rad. Same unchanged predictor on the materially different 379 + LB 56 26 two-unit rig physically passed 2 m forward and 0.5 m reverse turns with clear contact queries; its forward truck endpoint error was 0.586 m, mostly lateral (0.553 m), before trailer propagation. Classify GMC maneuver class PROVEN and second-rig generalization PARTIALLY PROVEN; root-centred arc needs a measured turn-centre/steering-response check before claiming narrow-gap universality. Evidence in `.fs25-dev/evidence/articulated-reverse-20261103/` and `.fs25-dev/evidence/articulated-second-rig-20261103/`.
- Read-only physical inventory of the same live four-unit rig found 48/33/8/2 collision-enabled shape nodes for GMC/Deckover/Series 9/plow, many of them triggers; filter collision groups before treating a shape as solid. `getRigidBodyAABB` returned reversed coordinate pairs on this angled rig, so raw values are not yet usable as authoritative footprints. The all-wheel-representation hitch-to-centre measurement was 9.152 m in this pose, with active-wheel status unreadable.
- With scenery collision left normal/no-ghost, the complete GMC + Deckover + Series 9 + plow rig physically moved at least 12 m in one monitored run. Later movement also showed that safe motion alone does not prove correct route selection.
- Background/minimized GIANTS screenshot capture was verified against a foreground capture: after scaling, the central scene had mean RGB difference 2.87/255 and 99.2% of pixels within 20 RGB levels. Use background capture for development evidence unless a menu action genuinely requires focus.
- Ordinary navigation proof excludes scenery ghosting. Predicted obstruction, confirmed contact, no-progress and stopping tolerance must remain separate states.
- Single-flight live requests are mandatory; a queued request previously ran behind STOP and contaminated route evidence.

## 2026-11-04 evening live findings (production builds 5613bd1 → f8355da)
- NOT_STOPPED deadlock cleared live (87ec310879a546ea-1): one at-rest rule (Observe.atRest) with the sample's real age.
- Drive.plan lattice turns heading in STEP/radius steps (1 rad at r=4); a goal heading within pi/12 was met only by luck or a near-2pi loop. Fixed with an exact Dubins goal shot (757f4bd).
- GIANTS' field helper (AICollisionTriggerHandler) holds for the player on foot; AIFieldWorker:getAIFieldWorkerIsBlocked() exposes it. In save 1 the player often stands AFK in field 4 near (-703,237): expect person refusals there.
- The whole-rig rectangle also falsely blocked the search and the straightening reverse of a bent towed rig; the search now carries the trailer heading (159896e, f8355da). See RELEASE_BACKLOG "Articulated route search".
- Loaded GMC haul to field 5 PASS by GIANTS' navigation from (-980,-14) (5d29c5999c864530-1); from (-875,-60) GIANTS crept and AutoDrive had no route.

## Routing and movement lessons already learned
- 2026-11-01 Save 1: exact natural command `take the GMC to the farm barn` physically COMPLETE (`d7e14850a1a34a21-1`) with GMC 611 + Deckover 620 + belted Series 9 622 + plow 621 stopped near the barn. It used native GIANTS navigation and final Farmhand arrival; the walked gate route did not qualify. That route remains in `routes.json` as `c996ab0558084c21aea35b93aa026fbc`, `verified=false`, `failures=2`, so selection skips it without deleting the user-taught line. The game log showed crop care-wheel restoration 13/13 at native end and own-drive handoff. Full `scripts/check.py` then passed 1,363 tests (6 skips) after the stale AutoDrive-first test was corrected. Native transport surface preference is still unproven; see backlog.
- Farmhand has/has had a frame-budgeted resumable route search so route planning does not freeze FS25.
- FS25 Lua does not provide coroutine.yield for this use; Courseplay-style resumable search keeps search state and returns after a frame budget.
- Field arrival should be based on the actual field boundary/inside-field result with a small rig-aware tolerance, not an arbitrary marker point.
- Whole-rig geometry matters: tractor/truck + trailer/implements + belted cargo. Do not judge routes from the root vehicle alone.
- Trailer reversing needs articulation-aware control; treating a long trailer as a rigid car is not sufficient.
- Wide-turn path generation and curve-speed control are separate.
- Pairwise scenery collision ghosting was proven safer than broad collision-mask changes; broad ghosting previously caused ground/wheel problems.
- Road/bridge surfaces and tight egress need runtime geometry, not Riverbend-specific coordinates.
- STOP must remain durable/unread until accepted; an earlier withdrawn STOP allowed orphaned motion.
- Search/planning must stay responsive enough that heartbeat/link state is preserved.
- Moving blockers is an approved recovery when a real worker would move them and resume the task.

## Fieldwork findings
- GIANTS FieldCourse derives a field/course from worked ground (BoundaryDetectionTask), not only from the map polygon.
- Plowing the strip between adjacent fields with setPlowLimitToField(false) can make GIANTS subsequently treat them as one worked field.
- GIANTS can retain hired helper jobs through a save.
- Current dirty fieldwork work includes worker-watch/recovery, field cleanup, request queue, join-fields, inaccessible-implement rescue and unloading while harvesting; use current git/live evidence and the backlog for status rather than assuming qualification.

## Model policy
Committed 178b8d1:
- Farmhand downloads/recommends no AI model.
- No managed llama.cpp runtime download.
- Settings lists only user-installed Ollama models and user GGUF/llama.cpp options.
- Rule-based commands continue working without an AI model.
- Farmhand uninstall never deletes user-owned models.

Do not reopen without new evidence.

## Major committed product landmarks
- 178b8d1 - user-owned Ollama/llama.cpp models only; no Farmhand model downloads.
- 1627034 - named vehicle movement, blocker handling, bounded teleport/provider path. Loaded long-haul currently exposes the standalone AutoDrive gap; do not interpret this commit as standalone loaded-transport qualification.
- 4b44ede - settings screen from Farmhand command window/settings key; buys only when told.
- d73fc92 - combine unloading workflow from current position/approach, cover handling and seated-player observation.

Use git log and live evidence for exact current qualification. Do not reopen independently proven commits merely because adjacent dirty work exists.

## Authority reminder
This file is supporting memory only. It must not redirect work away from the current minimum-system proving program in AGENTS.md/CLAUDE.md. Historical provider-era routing notes are evidence, not architecture requirements.

## Release lifecycle for these temporary files
During development:
- add only durable high-value findings here;
- put deferred work in RELEASE_BACKLOG.md;
- remove resolved backlog entries instead of keeping a history.

At first production release:
- promote permanent facts to AGENTS.md, EVIDENCE.md, README/code comments only where they belong;
- delete PRODUCT_WORKING_MEMORY.md and RELEASE_BACKLOG.md.
