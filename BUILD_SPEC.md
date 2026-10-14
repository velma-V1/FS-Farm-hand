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
is a development workflow constraint, not a runtime architecture decision.

## Strict scope
One task: **“Go grab an available tractor and forage wagon, hook them together,
and bring them here.”** One farm, one requester, one running executor process.
“Here” means the requester's position sampled once when the validated intent is
accepted, not a moving follow target. Coordinates are horizontal metres (x, z).
AutoDrive routes only to map markers (adapters/EVIDENCE.md), so delivery goes to the
nearest valid routable destination within 50 metres of that captured position.
The intent may instead name an explicit destination: an existing AutoDrive marker
(by live ID, or by name matched exactly after whitespace/case normalization), or a
field number, which resolves only through a trusted local
`field_entrances[map_id][field] → marker name` table because no verified
field-to-marker association exists. A field target means travel to that entrance;
it never starts fieldwork. Arbitrary coordinates are unsupported. Missing, ambiguous,
invalid, or unmapped destinations are BLOCKED before reservation, with no fallback
to a nearby marker. Delivery tolerance at the resolved destination is 3 metres for
the tractor and 15 metres for the wagon (allows trailer length); both
must be stopped and mutually attached. These are provisional contract values to
validate against real equipment and AutoDrive marker accuracy in FS25.

Input to the deterministic executor is versioned structured intent. A future local
language layer interprets the user's speech/text into a task proposal; trusted
local service code supplies request/session authority such as request_id and farm_id
before executor validation. This build uses an explicit fixture and makes no claim
of natural-language understanding.

No vehicle IDs come from the LLM. Selection checks categories (forage wagons by
GIANTS' ForageWagon specialization), same-farm permission, availability, no
driver/job/attachment/reservation, a shared attacher joint type (FS25 tractors
expose several joints), sufficient tractor power by GIANTS' own PTO torque rule, a
shared PTO at the planned joints when the wagon needs PTO power, and
movement/attachment/stop capabilities. IDs are opaque;
a lexical tie-break provides repeatability but carries no equipment semantics.

## Architecture and boundaries
| Area | Responsibility | This build |
| --- | --- | --- |
| companion/ | Validate intent, deterministic executor, observed completion | Implemented Python core; no daemon or transport |
| protocol/ | Versioned intent schema, equipment/result types, local IPC frame schema and decoder | Internal contracts; not an FS25 API |
| adapters/ | World port, FS25 snapshot mapping, upstream evidence (EVIDENCE.md) | Mapping implemented and fail-closed; no physical adapter |
| mod/ | Read-only observation, HITCH primitives, development console probe | No production command path; never run in FS25 |
| simulator/ | Synthetic inventory, atomic reservations, simplified movement/coupling | Mock only |
| tests/ | Executable acceptance, negative, schema and Lua checks | CI runnable |

FIND resolves the destination and selects an eligible pair with a hitch plan
(companion/hitch.py) and, if the tractor is far, an AutoDrive marker near the
staging pose. RESERVE atomically owns both or neither. MOVE is HITCH: VALIDATE (fresh
reserved, unattached, stopped pair; wagon unmoved; same planned joints) before and
after every step → TRAVEL_NEAR (AutoDrive, only when far) → STAGE (GIANTS AIJobGoTo
to a pose a few metres ahead of the final hitch pose, facing the wagon's heading) →
ALIGN_REVERSE (bounded reverse; stops when GIANTS reports the exact planned joints
as the attach candidate). ATTACH uses GIANTS' normal attachment after re-checking
that candidate and isAttachAllowed. VERIFY reads fresh attachment state and a
stopped tractor before DELIVER requests transport to the chosen destination. One
deterministic attempt: no retries, re-angling or alternate hitch search. Final observation verifies attachment, both positions, and stopped state.
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

The upstream feasibility probe is recorded in adapters/EVIDENCE.md. Result: read-only
observation, AutoDrive marker delivery and stop, and every HITCH primitive (joint
geometry, GIANTS GOTO navigation, driveToPoint reversing, attach-range detection,
normal attachment, attachment observation) are backed by GIANTS/upstream source.
Their composition on a real tractor and forage wagon is UNVERIFIED until an FS25
test. Power and PTO now come from GIANTS data on the loaded vehicles; a real pair is
selectable once the user's tractor store category is configured and a command
channel grants move/attach. The local IPC is a companion-hosted Windows named pipe
(game → companion, protocol v1, implemented as a decoder) plus a polled XML command
file (companion → game, design only). Do not build navigation beyond ALIGN_REVERSE.

The first FS25 test proves only the HITCH composition, with the development
console probe (`mod/FarmhandProbe.lua`; steps in mod/README.md): `fhHitchList`,
then `fhHitchProbe <tractorId> <wagonId> [assistRadiusM] [maxVerticalM]
[maxSpeedKmh]`, which validates, plans (a dev-only Lua copy of companion/hitch.py,
checked equal in CI), stages with GIANTS GOTO, checks the staged AI-root pose,
reverses, attaches the exact candidate and verifies exactly that pair and joint,
ending with one `RESULT` line. Tune `STAGING_DISTANCE_M`, the assist radius,
vertical guard and reverse speed from the logged values.

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
