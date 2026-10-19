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
| STOP mid-maneuver | — | — | OPEN | |
| Player takeover | — | — | OPEN | |
| Stall / failure | — | — | OPEN | |
| Incompatible equipment | — | — | OPEN | |

## Observation

| Case | Test id | Build | Result | Observed |
| --- | --- | --- | --- | --- |
| Detached wagon after an AutoDrive tractor | status, 2026-10-18 15:58 | eb1c00a | PASS | wagon 173 no longer AI_STATE_UNKNOWN (AutoDrive scratch table) |

## Navigation, procurement, supplies, jobs

All OPEN; rows are added as each capability lands.
