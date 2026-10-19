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
| Other implement class: cultivator, 1.2 km away | 3da28a3f24dd450b-1 | 702e468 | REFUSED-OK (reason fixed) | "hitch the cultivator": tractor 170 and PROLANDER 7500 165 selected, joints 1/1; GOTO travel 4.5 min, ended NOT_REACHABLE 15.6 m from staging, facing the implement (-165.5 deg, gap 20.6 m); LOCAL refused unmoved. Logged LOCAL_PATH_LEAVES_AREA; the real planner replayed offline shows a header and trolley parked 6 m beside the cultivator block every short curve: LOCAL_PATH_BLOCKED (reason fixed in c12fc52) |
| Other implement class: trailer, staging | 3da28a3f24dd450b-2 | 702e468 | FAIL (aim changed) | "hitch the trailer": HALFPIPE 159, joints 4/1; STAGE ended NOT_REACHABLE 50 ms after start, 41.7 m off, unmoved. GOTO now aims beside the implement (adapters/EVIDENCE.md) |
| Trailer, approach aim, 1.2 km away | 27539c7115a44b86-1 | 8dae29e | FAIL (fixed) | GOTO to the first approach (804.5, 312.1, west of HALFPIPE 159) ended NOT_REACHABLE 50 ms after start, 1248 m off, unmoved: a blocked aim. The other side was never tried; GOTO travel now aims at the next approach once |
| Moderate offset | — | — | OPEN | |
| ~45° heading | — | — | OPEN | |
| Large heading error | — | — | OPEN | |
| Different tractor | — | — | OPEN | |
| Detach / reattach | — | — | OPEN | |
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
