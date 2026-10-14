# FS25 autonomous farmhand — foundation build

## Objective
Create the smallest production-usable LOCAL-FIRST autonomous farmhand incrementally,
reusing GIANTS FS25 and existing mods. The finished system runs on the user's
Windows PC alongside Farming Simulator 25. GitHub and hosted CI are development
tools only, not runtime infrastructure.

This foundation is a CI-tested control contract, not a production-ready or
installable FS25 mod.

## Runtime architecture
The intended finished runtime is local:

1. Farming Simulator 25 runs on the user's Windows PC.
2. A small FS25 Lua integration mod runs inside the game.
3. A local Windows companion service performs deterministic orchestration.
4. The game mod and companion service communicate through local IPC.
5. The planner/LLM, speech recognition, and TTS run locally unless the user
   explicitly chooses a different provider later.
6. GitHub is used for source control, development, CI, review, and packaging only.
   Normal farmhand operation must not depend on GitHub or a hosted service.

The repository may remain remote during development so unfinished code does not
need to be installed on the user's PC before live game testing is justified. That
is a development workflow constraint, not a cloud-runtime architecture.

## Strict scope
One task: **“Go grab an available tractor and forage wagon, hook them together,
and bring them here.”** One farm, one requester, one running executor process.
“Here” means the requester's position sampled once when the validated intent is
accepted, not a moving follow target. Coordinates are horizontal metres (x, z).
Delivery tolerance is 3 metres for the tractor and 15 metres for the wagon (allows
trailer length); both must be stopped and mutually attached. These are provisional
contract tolerances to validate against real equipment lengths in FS25.

Input to the deterministic executor is versioned structured intent. A future local
language layer interprets the user's speech/text into a task proposal; trusted
local service code supplies request/session authority such as request_id and farm_id
before executor validation. This build uses an explicit fixture and makes no claim
of natural-language understanding.

No vehicle IDs come from the LLM. Selection checks categories, same-farm permission,
availability, no driver/job/attachment/reservation, hitch match, sufficient tractor
power, PTO when required, and movement/attachment/stop capabilities. IDs are opaque;
a lexical tie-break provides repeatability but carries no equipment semantics.

## Architecture and boundaries
| Area | Responsibility | This build |
| --- | --- | --- |
| companion/ | Validate intent, deterministic executor, observed completion | Implemented Python core; no daemon or transport |
| protocol/ | Versioned intent JSON Schema and equipment/result types | Internal contract; not an FS25 API |
| adapters/ | Typed physical-world boundary; reuse/version evidence | Protocol only; upstream adapters deferred |
| mod/ | Game-side capability boundary | Pure Lua unsupported stub, no game hooks |
| simulator/ | Synthetic inventory, atomic reservations, simplified movement/coupling | Mock only |
| tests/ | Executable acceptance, negative, schema and Lua checks | CI runnable |

FIND selects an eligible pair. RESERVE atomically owns both or neither. MOVE
requests approach to the wagon through the adapter. ATTACH requests coupling.
VERIFY reads fresh attachment state before DELIVER requests transport to captured
“here”. Final observation verifies attachment, both positions, and stopped state.
Each physical step rechecks reservation and equipment eligibility. Failures do not
advance the task. No automatic retry or fallback to custom driving.

Only the adapter can mutate world state. It must recheck authority and physical
preconditions atomically at the point of action. Production adapters must provide
bounded operations and fresh authoritative snapshots; acknowledgements are not
completion evidence. The in-process mock enforces this with a lock and snapshots.
Reservations bind an unused owner ID to the exact equipment pair and its farm.
The mock rechecks this binding and compatibility atomically before movement;
stop/release cannot sweep unrelated equipment sharing a request ID.
The synchronous executor cannot preempt a hung adapter: timeout injection tests
error handling, not a working production watchdog.

Results: COMPLETE = all observations verified; BLOCKED = invalid input, unavailable
pair/capability or reservation conflict; FAILED = adapter rejected/failed a step
and stop was confirmed; UNVERIFIED = observation mismatch or unconfirmed stop.
Reservations release only after a confirmed stop following attempted physical work.
Failure to stop/release retains ownership conservatively. Repeated identical
request IDs return the cached outcome within the same executor instance; changed
payloads with the same ID are blocked. Execution is serialized in that instance.
There is no durable replay prevention or crash recovery; do not connect this core
to a live game until a transport/authority/recovery design is validated.

## Non-goals
No generalized game-agent framework, custom navigation replacement, custom
pathfinding replacement, LLM steering, computer vision, multiplayer, or speculative
infrastructure. Do not replace AutoDrive, Courseplay, Follow Me, or Universal
Autoload. Do not vendor upstream code without a demonstrated need and license review.

Voice, local model integration, purchasing, loading, fieldwork, harvesting, policy,
and background-service installation are future vertical slices, not part of AC-001.

## Acceptance criteria
- Executable scenarios in tests/ACCEPTANCE.md pass with varied opaque IDs and
  inventory orders, decoys, busy/occupied/foreign equipment, and incompatible pairs.
- Selection considers the entire pair, not merely the first tractor found.
- Contention cannot partially reserve a pair or steal another request's assets.
- Unsupported capability, movement/attachment failure, false acknowledgement,
  lost attachment, and uncertain stop cannot produce COMPLETE.
- Valid/invalid inputs are checked against the actual JSON Schema; extra fields,
  unknown versions, invalid IDs and unsupported intents are rejected before action.
- All Python tests and actual Lua execution checks pass in CI.
- No external source code, game assets, secrets, paid model calls, or premature
  installation on the user's PC is required for this foundation.
- Review includes final tree inspection and explicit implemented/mocked report.

## Pre-game development gate and local runtime gate
Use Python 3.12 and the pinned dependencies. Run `python scripts/check.py` in
GitHub Actions or another development checkout. Lua is checked through the pinned
Lua runtime binding in Python; that is not proof of the FS25 embedded runtime.

No FS25 binaries/assets are downloaded or packaged in this foundation. mod/
deliberately omits a modDesc.xml and install ZIP because no functioning game
integration exists yet. The first PC installation should occur only when a real
FS25 integration test is necessary.

Next justified work is a version-pinned GIANTS/AutoDrive integration feasibility
probe: inventory extraction, authority, a LOCAL communication mechanism, safe
approach/coupling capability, bounded execution and stop/cancel semantics. Do not
assume arbitrary coordinates or automated coupling are exposed by any upstream mod.
If not supported, report the gap; do not build a navigation replacement.

Before claiming production usability, an actual FS25 runtime on the user's PC must
prove mod load, current game/mod versions, ownership enforcement, equipment detection,
feasible approach and attachment, real routing to a safe location near the requester,
stop, arrival, local IPC, and failure recovery. Crash/restart and disconnect must not
replay unsafe commands or abandon moving equipment. Multiplayer is outside this first
release.

## Foundation implementation plan
1. Write behavioral tests in tests/test_farmhand.py against
   `Executor(world).execute(intent) -> Result`; write schema/Lua boundary tests.
   Observe missing implementation failures before adding the core.
2. Implement frozen Equipment/Result types in protocol/model.py, the World port
   in adapters/world.py, the executor in companion/executor.py, and MockWorld in
   simulator/world.py. Tests cover stale state, conflicting reservation, false
   acknowledgement, invalid intent and uncertain stop as primary review risks.
3. Add one CI check entry point and GitHub Actions. Run the full suite, review all
   files, remove redundant scaffolding, and publish a dedicated review branch.
