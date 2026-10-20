# Live FS25 qualification matrix

The real game is the final authority. Each row names the live test (its id is the
folder under `.fs25-dev/evidence/` and the record in the companion's
`modSettings/FS25_Farmhand/livetest`), the build it ran on, and the observed
result. Unit tests and CI are supporting evidence only (tests/ACCEPTANCE.md).

Save: the user's FS25_Chipping_Norton test save (farm 1), mods loaded: AutoDrive
3.0.1.4, FollowMe, FarmFillStations, VisibleFarmlandBuildings, colossusPack,
Deutz Series 9. Courseplay is not installed; Universal Autoload is installed but
not enabled in this save.

Status: PASS (the required outcome, observed), REFUSED-OK (a correct safe
refusal), FAIL (a defect; see the row), OPEN (not yet run).

## Hitch / attachment

| Case | Test id | Build | Result | Observed |
| --- | --- | --- | --- | --- |
| Aligned start (reverse only) | job 6 of session b60e0143759c44e5 (player key, not a live test) | a85ff0a | PASS | ALIGN_REVERSE gap 4.49 → 0.71 m, ATTACH, VERIFY |
| ~90° heading, ~20 m lateral | aa3b6a85654042b6-1 | a6d874d | PASS | LOCAL_MANEUVER 26.9 s → LINED_UP 7.4 m ahead, 0.12 m lateral, 5.0°; ALIGN_REVERSE → 0.60 m; ATTACHED; both attached and stopped |
| Same start before the forceIsActive fix | 0376974ad9bc40b9-3, a8ee676433224912-1 | eb1c00a, ce64540 | FAIL (fixed) | wheels never steered (isActive false): DIVERGED; root cause in adapters/EVIDENCE.md |
| GOTO stage → local maneuver | 0376974ad9bc40b9-2 | eb1c00a | REFUSED-OK | STAGE 32.9 → 24.1 m, then SEATED_DURING_MANEUVER (player in the cab) |
| Player seated in the tractor | 0376974ad9bc40b9-1 | eb1c00a | REFUSED-OK | SEATED_DURING_MANEUVER before any motion; stop and release |
| Cultivator in open ground, 150 m (generalized hitch, approach aim) | 2ded907e5cb34b6c-3 | 004cd54 | PASS | "hitch the cultivator": tractor 170, PROLANDER 7500 165, joints 1/1. TRAVEL_NEAR GOTO to the first approach: target_reached 0.4 m off (35 s); LOCAL_MANEUVER from gap 10.2 m / lateral 9.9 m / -43.5 deg, 13.4 m S, LINED_UP 7.09 m ahead, -0.08 m lateral, -5.0 deg (16.7 s); ALIGN_REVERSE 7.3 m to gap 0.56 m, CANDIDATE; ATTACHED; VERIFY both ends on joint 1, stopped |
| Other implement class: cultivator, 1.2 km away | 3da28a3f24dd450b-1 | 702e468 | REFUSED-OK (reason fixed) | "hitch the cultivator": tractor 170 and PROLANDER 7500 165 selected, joints 1/1; GOTO travel 4.5 min, ended NOT_REACHABLE 15.6 m from staging, facing the implement (-165.5 deg, gap 20.6 m); LOCAL refused unmoved. Logged LOCAL_PATH_LEAVES_AREA; the real planner replayed offline shows a header and trolley parked 6 m beside the cultivator block every short curve: LOCAL_PATH_BLOCKED (reason fixed in c12fc52) |
| Other implement class: trailer, staging | 3da28a3f24dd450b-2 | 702e468 | FAIL (aim changed) | "hitch the trailer": HALFPIPE 159, joints 4/1; STAGE ended NOT_REACHABLE 50 ms after start, 41.7 m off, unmoved. GOTO now aims beside the implement (adapters/EVIDENCE.md) |
| Trailer, approach aim, 1.2 km away | 27539c7115a44b86-1 | 8dae29e | FAIL (fixed) | GOTO to the first approach (804.5, 312.1, west of HALFPIPE 159) ended NOT_REACHABLE 50 ms after start, 1248 m off, unmoved: a blocked aim. The other side was never tried; GOTO travel now aims at the next approach once |
| Trailer, other approach | 27539c7115a44b86-2 | 6be7e0b | FAIL (fixed) | the second approach (830.5, 312.3, east of HALFPIPE 159) was refused by GIANTS' validate: NAVIGATE_GOTO_INVALID (blocked ground). Aims are now three families per side; refused ones are skipped unmoved |
| Trailer, stalled approach | 4167b7fd4e8143db-1 | c6d4496 | FAIL (fixed) | GOTO 1.2 km to the first approach beside HALFPIPE 159, then no progress 12 m short: NAVIGATE_STALLED ended the job. A stalled approach drive is now judged by the observation |
| Trailer, from the stall | 2ded907e5cb34b6c-1 | 004cd54 | REFUSED-OK | stalled again at (795.6, 313.3), facing away (177 deg, 22 m aside); LOCAL refused LOCAL_PATH_HITS_WAGON (offline: only a 63 m curve exists in an empty yard; parked vehicles north); both re-approach STAGEs NOT_REACHABLE at once; stopped after 3 drives |
| Water trailer, from that spot | 2ded907e5cb34b6c-2 | 004cd54 | REFUSED-OK | MKS 8 171, joints 3/1: all three aims NOT_REACHABLE at once from (795.6, 313.3): GIANTS' agent cannot start from where the stall left the tractor; capped at 3 drives, nothing moved |
| Sprayer in the yard, different tractor (Series 9 172), 1.2 km | 2ded907e5cb34b6c-4 | 004cd54 | REFUSED-OK (retries wasteful) | Vantage 4300 161, joints 3/1. Aim 1 NOT_REACHABLE at once, aim 2 GOTO_INVALID, aim 3 drove ~560 m then NAVIGATE_STALLED 718 m short; aim 4 from there NOT_REACHABLE at once; capped at 3 drives. A stall far away should end the job, and unmoved attempts should not count as drives |
| Moderate offset | — | — | OPEN | |
| ~45° heading | — | — | OPEN | |
| Large heading error | — | — | OPEN | |
| Different tractor | — | — | OPEN | |
| Cultivator in open ground, repeated from save 3 | ec4422f2a4484f9e-1 | 01d0eae | PASS | same chain and numbers as 2ded907e5cb34b6c-3 (LINED_UP 7.26 m / -0.14 m / -3.7 deg, gap 0.55 m, ATTACHED, VERIFY) |
| Detach ("unhitch the cultivator") | ec4422f2a4484f9e-2 | 01d0eae | PASS | FIND, RESERVE, DETACH (GIANTS isDetachAllowed, detachImplementByObject), VERIFY: both ends unattached and stopped; nothing moved |
| Reattach right after the detach | ec4422f2a4484f9e-3 | 01d0eae | FAIL (fixed) | joints 0.12 m apart, tractor joint 0.11 m on the far side: ALIGN_WRONG_SIDE sent it to LOCAL_MANEUVER (refused), then a re-approach STAGE hung (STAGE_TIMEOUT, nothing moved). Joints within 0.5 m now take a look-only ALIGN_REVERSE (GIANTS' candidate, never a drive), then ATTACH |
| Cultivator from save 3, third run | d7889a7a8879421b-1 | 84f55ee | PASS | FIND, RESERVE, TRAVEL_NEAR, LOCAL_MANEUVER, ALIGN_REVERSE, ATTACH, VERIFY again (69 s) |
| Detach, second run | d7889a7a8879421b-2 | 84f55ee | PASS | DETACH_OBSERVED; both ends free and stopped |
| Reattach right after the detach | d7889a7a8879421b-3 | 84f55ee | PASS | the same geometry that failed in ec4422f2a4484f9e-3 (gap 0.12 m, ahead -0.11 m, lateral -0.05 m, -0.8 deg): look-only align (maxDistanceM 0) returned CANDIDATE, joints identical before and after (no motion), ATTACHED, VERIFY in 7 s |
| Seeder in the yard, from beside the just-detached cultivator | d7889a7a8879421b-5 | 84f55ee | FAILED-PRECISE (TRAPPED) | GL 860 163, joints 3/1, tractor 170 at (794.8, 450.3) in open ground, the cultivator's joint 0.12 m behind it, facing away from the yard. Two GOTOs ran 90 s each with the job active and the tractor unmoved (NAVIGATE_STALLED), then TRAPPED; no third drive. The same spot hung a STAGE in ec4422f2a4484f9e-3. Cause not yet observed: the stall now logs GIANTS' agent state (BLOCKED waits forever) |
| Re-hitch the cultivator after that | d7889a7a8879421b-6 | 84f55ee | PASS | look-only align, ATTACHED, VERIFY |
| Sprayer in the yard, Series 9 172, 1.2 km | d7889a7a8879421b-7 | 84f55ee | FAILED-PRECISE (NAVIGATE_STALLED) | aim 1 ended at once unmoved and aim 2 GOTO_INVALID (neither counted), aim 3 drove, then no progress for 90 s at (309.3, -281.7), 718 m short: ended there, one drive. Same stall point as 2ded907e5cb34b6c-4 (repeatable on this route) |
| Cultivator, detach, then seeder (save 3 reloaded) | 5cdd589ebdce4f01-1/-2/-3 | 076c71d | PASS / PASS / FAILED-PRECISE (TRAPPED) | hitch and detach as before; then the trap reproduced, and the stall log names it: `navigate stalled: agent state blocked` both times (165.5 m, 178.7 m from the target). GIANTS' agent will not drive off a just-unhitched implement towards a target behind. Fix: PULL_CLEAR (GIANTS GOTO 12 m straight ahead first) |
| Cultivator, detach, seeder with PULL_CLEAR (new runner, launch helper) | 23a8b1d881c7489b-2/-3/-4 | 3b2563e | PASS / PASS / FAILED-PRECISE (TRAPPED, 90 s) | PULL_CLEAR navigate to 12 m straight ahead in open ground: `navigate stalled: agent state blocked, 12.0 m from its target`, 0.0 m moved, TRAPPED at once (no approach tried). The direction does not matter: GIANTS' agent will not start next to a just-unhitched implement. Needs a user decision (a bounded pull-off driven by Farmhand), see EVIDENCE |
| Layout changed (trailers moved out of the yard; cultivator 165 now in the yard 5 m from header 162) | 4e9c443e19bd4de1-1 / -3 | ba5752c | REFUSED-OK (LOCAL_PATH_BLOCKED) | cultivator and seeder 163 (joints 3/1): every line-up curve passes within 8 m of another vehicle (header 162; DPW 160 8 m and sprayer 161 14 m from the seeder); approaches capped, nothing driven by Farmhand. Seeder and sprayer need open ground |
| Water trailer MKS 8 171 in open ground (new class: trailer) | 4e9c443e19bd4de1-4 | ba5752c | PASS | TRAVEL_NEAR, LOCAL_MANEUVER, ALIGN_REVERSE, ATTACH, VERIFY |
| Unhitch with PULL_CLEAR (first live) | 4e9c443e19bd4de1-5 | ba5752c | PASS | DETACH, VERIFY, PULL_CLEAR: 4.02 m straight forward, lateral 0.00 m, 5.1 s, CLEAR at separation 4.01 m (mod); fresh snapshot 3.82 m apart, both at rest |
| Re-hitch after PULL_CLEAR | 4e9c443e19bd4de1-6 | ba5752c | PASS | joints 4.18 m apart, on the axis: straight ALIGN_REVERSE, ATTACH, VERIFY |
| GOTO from a pull-cleared stand | 4e9c443e19bd4de1-7 / -8 | ba5752c | PASS (PULL_CLEAR) / REFUSED-OK (cultivator) | after the second PULL_CLEAR, GIANTS' agent started and drove ~85 m (TRAVEL_NEAR aim 3, ended 13.8 m off): no longer BLOCKED. The cultivator then refused LOCAL_PATH_BLOCKED in the yard as -1 |
| Seeder GL 860 163 in open ground (new save 3 layout; class: sowing machine, three-point) | 4e9c443e19bd4de1-9 | ba5752c | PASS | TRAVEL_NEAR, LOCAL_MANEUVER, ALIGN_REVERSE, ATTACH, VERIFY |
| Unhitch the seeder | 4e9c443e19bd4de1-10 / -12 | ba5752c | FAIL (fixed) | DETACH and VERIFY passed; PULL_CLEAR stopped itself SEPARATION_LOST within 0.2-0.4 s (separation 0.19 -> 0.14 m, tractor 0.01-0.07 m forward): GIANTS' lower links carried the jointTransform back towards the seeder after the detach. Separation now measured from the joint's start point fixed to the tractor's body |
| Re-hitch the seeder | 4e9c443e19bd4de1-11 | ba5752c | PASS | joints 0.17 m apart: look-only align, ATTACH, VERIFY |
| Sprayer Vantage 4300 161, open ground, asked by the player in game (Farmhand key) | 8d1f4ada61c14239-1 | 20d7c16 | PASS | production request from the player: TRAVEL_NEAR, LOCAL_MANEUVER, ALIGN_REVERSE, ATTACH, VERIFY (class: sprayer, drawbar) |
| Unhitch the sprayer (x2) | 8d1f4ada61c14239-3 / -5 | 20d7c16 | UNVERIFIED (fixed) | PULL_CLEAR 3.90 m straight, CLEAR at 4.01 m by the mod; the fresh snapshot (GIANTS' joint offsets) saw 3.70 / 3.69 m, under the verified 3.75 m. The mod now pulls to 4.5 m; the companion still verifies 3.75 m |
| Re-hitch the sprayer after the pull | 8d1f4ada61c14239-4 | 20d7c16 | PASS | straight ALIGN_REVERSE from ~3.7 m, ATTACH, VERIFY |
| Seeder again (hitch, unhitch, re-hitch, unhitch) | 8d1f4ada61c14239-6..-9 | 20d7c16 | PASS / FAIL (fixed) / PASS / FAIL (fixed) | the body-fixed joint point holds, but the seeder itself settled 6 cm towards the tractor after GIANTS' detach (separation 0.19 -> 0.14 m, tractor +0.01 m): SEPARATION_LOST. Growth is now judged from the implement's start position; its own movement stays bounded by IMPLEMENT_MOVED (25 cm) |
| Seeder: hitch, unhitch, re-hitch, unhitch (save 3 reloaded) | 4137e562059b4e84-1..-4 | 8b5ce5e | PASS x4 | hitch by TRAVEL_NEAR, LOCAL_MANEUVER, ALIGN_REVERSE; both unhitches PULL_CLEAR 4.55 / 4.56 m straight (lateral -0.02 m, 5.6-5.9 s), CLEAR at 4.50 m, snapshot 4.32 / 4.36 m; re-hitch a straight reverse |
| Sprayer: hitch, unhitch, re-hitch, unhitch | 4137e562059b4e84-5..-8 | 8b5ce5e | PASS x4 | hitch by STAGE, LOCAL_MANEUVER, ALIGN_REVERSE; PULL_CLEAR 4.40 m (lateral 0.00 / 0.01 m), CLEAR at 4.51 m, snapshot 4.20 / 4.21 m; re-hitch a straight reverse |
| STOP mid-maneuver | — | — | OPEN | |
| Player takeover | — | — | OPEN | |
| Stall / failure | — | — | OPEN | |
| Incompatible equipment | — | — | OPEN | |

## Observation

| Case | Test id | Build | Result | Observed |
| --- | --- | --- | --- | --- |
| Detached wagon after an AutoDrive tractor | status, 2026-10-18 15:58 | eb1c00a | PASS | wagon 173 no longer AI_STATE_UNKNOWN (AutoDrive scratch table) |

## Field work (gate: one owned small test plot, cultivate/plow -> seed -> fertilize, small test areas)

| Case | Test id | Build | Result | Observed |
| --- | --- | --- | --- | --- |
| Owned fields and their state observed | status, 2026-10-19 13:48 | 6c74538 | PASS (observation) | 8 owned fields; smallest field 3, 0.54 ha (21,907 m2) at (715, 882); every field 100 % CULTIVATED, no crop (9/9 samples NONE), no spray. GIANTS helper buys seeds, fertilizer and fuel (all three missionInfo settings on); money 999,808 |
| "cultivate a small area of the smallest field" | 1600bdcae60f4499-1 | 5117813 | FAILED (cause found) | field 3 chosen; step 1 hitch cultivator 165 by tractor 170: GOTO stalled, `agent state blocked`, 4.7 m from the player standing on the yard road (825, 344). Now reported BLOCKED_BY_PERSON. Also: cultivating an already 100 % cultivated field changes nothing measurable; such an operation is now ALREADY_DONE with nothing moved |

## Procurement, delivery, saving

| Case | Test id | Build | Result | Observed |
| --- | --- | --- | --- | --- |
| "buy a large plow, the gmc truck and a gooseneck" | 3bc4afddc91d4e14-1, f7df830605a24f8a-1 | 44a12e7 / d79baca | PASS (after fixes) | catalog -> GIANTS BuyVehicleData; John Deere 980 (4 m, 60 hp of the MF 5700 S's 95), 2024 GMC Sierra Denali 3500HD, Gooseneck Trailer; each a new owned vehicle at the store and the money down by its price. Earlier runs bought a GMC truck bed (no motor) and the forestry gooseneck, and a 300 hp plow by a pickup's power: fixed (trucks drive themselves, own-name matches win, plow power from three-point tractors) |
| GMC as a towing vehicle | status | 5629acc / edff1fc | PASS | default configuration "No Attachments" had no hitch; with the gooseneck configuration (saved by the user) its joints SEMITRAILERCAR/SEMITRAILER at z = +0.0014 m count as rear joints. Buying now chooses the hitch option (0ad6d1c) |
| "bring the plow to the farm house" | fdfd2ed8c88e4d70-2 | edff1fc | PASS | MF 5700 S from the gate (671, -255) to the store, STAGE x3, LOCAL_MANEUVER LINED_UP, ALIGN_REVERSE, ATTACH JD 980, DELIVER to the Farm house (0.2 m), STOP, RELEASE |
| Earlier: the closed gate on the farm-store road | 3bc4afddc91d4e14-2/-3, 4c7070bc7ff64378-2, 4d82677b45374da7-2 | 382f676-5629acc | FAILED (fixed) | GOTO stuck at (671, -255) with the agent `driving` (a closed gate GIANTS' map treats as open). Detour by GIANTS' own obstacle (block op) added; the stuck test first missed GIANTS' lower-case state name (fixed) |
| "bring the trailer to the farm house with the gmc" | fdfd2ed8c88e4d70-1 | edff1fc | FAILED (fixed, unverified) | the store's spawn yard: every line-up curve within 8 m of another vehicle (clearance kept); a stage hung. GOTO's last aim is now the staging pose straight ahead of the drawbar |
| "save the game" | 07:47 2026-10-20 | edff1fc | SAVED, reported UNVERIFIED (fixed) | the game logged "Game saved successfully." 0.4 s after the request; the check (save date, day only) could not see it. Now: completion of GIANTS' saveSavegame itself |

## Field work, 2026-10-20 (build 902bed7)

| Case | Test id | Result | Observed |
| --- | --- | --- | --- |
| Plow 400 m2 of field 3 (MF 5700 S + JD 980) | 82cf8000835e474c-2/-3, f5726787ab834af4-1/-2 | FAILED (cause found) | GIANTS' helper drove the pair ~550 m from the Farm house to field 3's edge (task 1, plow raised, 6.4 km/h), then pressed against a map fence GIANTS' map does not show: `agent state driving`, unmoved (FIELDWORK_STUCK). Marking the spot impassable left the agent BLOCKED (it cannot reverse); the user freed the tractor; it stuck again further along the same fence. Fixed: gates near a stuck vehicle are opened, otherwise STUCK_ON_OBSTACLE without marks |
| Plow 400 m2 of field 8 from there | f5726787ab834af4-3 | FAILED (setup) | the tractor still against field 3's fence among the session's marks: BLOCKED from the start |
| Plow a small area of field 8 (MF 5700 S + JD 980) | earlier session, build 3b5586a+ | COMPLETE (AREA_WORK_OBSERVED) | 575 m2 PLOWED on the field's density map (0 -> 0.012 of 47,142 m2); detach + PULL_CLEAR verified |
| "seed and fertilize a small area of field 8": seed | 5454f21977df465b-2 (build 9016f52) | sow COMPLETE; plan BLOCKED (fixed) | GL 860 hitched to the MF; 410 m2 RIDGE_SOWN (0 -> 0.009); the helper bought the seed (money -62.67). Next step `detach_pair` found no pair at rest (MF `MOVING` 1.5 s after the helper stopped): NO_ATTACHED_IMPLEMENT. Fixed in 497752f: each planned step waits up to 30 s for the farm's tractors at rest. Crop identity unconfirmed: all 9 fixed samples missed the strip (NONE); 497752f samples along the worked track |
| "unhitch the seeder" | 5454f21977df465b-3 (9016f52) | COMPLETE (DETACH_OBSERVED) | 164 off 171, PULL_CLEAR, both at rest |
| "fertilize a small area of field 8" | 5454f21977df465b-4 (9016f52) | COMPLETE (FIELD_WORK_OBSERVED) | plan hitch Vantage 4300 (162) -> fertilize -> detach, each verified; spray level 1 0 -> 0.028 (the 3 s area check stopped GIANTS' helper at 660 m2 changed, 11.5 km/h with a wide boom; 1,320 m2 measured once at rest); helper bought the fertilizer (money -32.94) |

## Navigation, supplies, jobs

All OPEN; rows are added as each capability lands.
