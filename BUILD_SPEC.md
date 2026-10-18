# FS25 autonomous farmhand — foundation build

## Objective
Create the smallest production-usable LOCAL-FIRST autonomous farmhand incrementally,
reusing GIANTS FS25 and existing mods. The finished system runs on the user's
Windows PC alongside Farming Simulator 25. GitHub and hosted CI are development
tools only, not runtime infrastructure.

This build is the complete AC-001 vertical slice on paper: a CI-tested
companion, command bridge and FS25 mod (`FS25_Farmhand`, built by CI) that has
not yet run inside FS25. Normal operation is an in-game key and a windowless
companion that starts at logon (see "Finished-product definition").

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
Three tasks on one capability (plus `hitch_pair`: hitch an available pair where
the wagon stands, no delivery). The requester may ride along in the tractor; only a
conflicting controller (an unrelated AI job, AutoDrive, the seated player taking the
controls, an unreadable seat) blocks or stops it. A tractor alone or a vehicle by
name is answered "not supported yet", never guessed. AC-001: **“Go grab an available tractor and forage
wagon, hook them together, and bring them here.”** AC-002 (`move_pair`): move the
tractor and forage wagon that are already hitched — the pair nearest the requester,
"this" — to the requester ("come here") or a named place ("take this back to the
barn"); it reserves, travels, verifies arrival, stops and releases exactly as
AC-001's delivery does, and never hitches or unhitches. A named place is an
AutoDrive marker of exactly that name (used when the tractor has AutoDrive) or the
one building the farm owns whose name contains all its words (GIANTS GOTO to 15 m
short of it, facing it); none or several block before reservation. One farm, one
requester, one running executor process.
“Here” means the requester's position sampled once when the validated intent is
accepted, not a moving follow target. Coordinates are horizontal metres (x, z).
AutoDrive is optional. Travel is AutoDrive's when the selected tractor has it and a
marker fits (AutoDrive routes only to map markers), otherwise GIANTS' own GOTO job,
whose navigation agent plans for the tractor with its attached wagon
(adapters/EVIDENCE.md). "Here" is the nearest valid AutoDrive marker within 50
metres of that captured position when the tractor has AutoDrive; otherwise, or when
no marker is that close, a GOTO pose 10 metres short of the position on the line
from the wagon, facing the requester, so the tractor never drives at them. No
marker or graph has to be created for "here".
The intent may instead name an explicit destination: an existing AutoDrive marker
(by live ID, or by name matched exactly after whitespace/case normalization), or a
field number, which resolves only through a trusted local
`field_entrances[map_id][field] → marker name` table because no verified
field-to-marker association exists. A field target means travel to that entrance;
it never starts fieldwork. Explicit markers need AutoDrive on the tractor
(`AUTODRIVE_UNAVAILABLE` otherwise). Arbitrary coordinates are unsupported. Missing, ambiguous,
invalid, or unmapped destinations are BLOCKED before reservation, with no fallback
to a nearby marker. Delivery tolerance at the resolved destination is 3 metres for
the tractor (its root at a marker, its AI root at a GOTO pose) and 15 metres for the
wagon (allows trailer length); both must be stopped and mutually attached. These are
provisional contract values to validate against real equipment, AutoDrive marker
accuracy and GIANTS GOTO accuracy in FS25.

Input to the deterministic executor is versioned structured intent. The player
asks in game, on foot: in their own words in GIANTS' text dialog (Left Alt + K),
or with fixed keys (Left Alt + J: bring the pair here; Left Alt + L: stop). The
mod sends a request frame. The companion's language layer (companion/language.py:
deterministic rules, later a local model behind them) turns words into a command
proposal (protocol/command.schema.json) that carries no authority; the
coordinator (companion/coordinator.py), as trusted local service code, validates
it and supplies request/session authority such as request_id and farm_id before
executor validation. STOP and STATUS are deterministic and answered at once, never
through a model; STOP cancels before the next step and pre-empts the running
operation. Speech (push-to-talk, local speech-to-text) feeds the same text path.

No vehicle IDs come from the LLM. Selection checks categories (forage wagons by
GIANTS' ForageWagon specialization; tractors as self-propelled vehicles with a
motor and a PTO output that are not in a loader store category, or by an optional
local category map), same-farm permission, availability, no
driver/job/attachment/reservation, a shared attacher joint type (FS25 tractors
expose several joints), sufficient tractor power by GIANTS' own PTO torque rule, a
shared PTO at the planned joints when the wagon needs PTO power, and
movement/attachment/stop capabilities (GIANTS GOTO available for the tractor). IDs are opaque;
a lexical tie-break provides repeatability but carries no equipment semantics.

## Architecture and boundaries
| Area | Responsibility | This build |
| --- | --- | --- |
| companion/ | Validate intent, deterministic executor, observed completion; command bridge and real World adapter (bridge.py); windowless request service and logon install (service.py); one-shot diagnostic runner (run.py) | Implemented; Windows pipe/file I/O tested on Windows CI only |
| protocol/ | Versioned intent schema, equipment/result types, local IPC frame schema and decoder | Internal contracts; not an FS25 API |
| adapters/ | World port, FS25 snapshot mapping, upstream evidence (EVIDENCE.md) | Mapping implemented and fail-closed; capability only through the live bridge session |
| mod/ | Observation, HITCH primitives, AC-001 command bridge, diagnostic console probe | Never run in FS25 |
| simulator/ | Synthetic inventory, atomic reservations, simplified movement/coupling | Mock only |
| tests/ | Executable acceptance, negative, schema and Lua checks | CI runnable |

FIND resolves the destination and selects an eligible pair with a hitch plan
(companion/hitch.py), its delivery route and, if the tractor is far, its pickup
route (an AutoDrive marker near the staging pose, else GIANTS GOTO to the staging
pose). RESERVE atomically owns both or neither. MOVE is HITCH: VALIDATE (fresh
reserved, unattached, stopped pair; wagon unmoved; same planned joints) before and
after every step → TRAVEL_NEAR (AutoDrive or GIANTS GOTO, only when far; coarse:
it need only end near the wagon) → STAGE (GIANTS AIJobGoTo aimed 6 m ahead of the
final hitch pose, facing the wagon's heading; coarse too: GOTO may end anywhere, so
its end is never arrival) → OBSERVE (the planned tractor joint relative to the
wagon's input joint, from a fresh snapshot: within 9 m, on the wagon's front side,
within 30 degrees and 2 m of its axis, else a precise failure such as
TRAVEL_ENDED_TOO_FAR, and nothing reverses) → ALIGN_REVERSE (the bounded local
alignment: reverse only, steered onto the wagon's axis from the observed joints,
capped at 10 m, 30 s and 5 km/h, ends on stall, divergence, wagon movement,
takeover or other AI; stops when GIANTS reports the exact planned joints as the
attach candidate). ATTACH uses GIANTS' normal attachment after re-checking
that candidate and isAttachAllowed. VERIFY reads fresh attachment state and a
stopped tractor before DELIVER requests transport to the chosen destination
(AutoDrive `drive` to a marker or GIANTS `navigate` to a pose). One
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
The real adapter is `companion/bridge.py` `FS25World`: every physical step is
one bounded bridge operation (`drive`, `navigate`, `stage`, `align`, `attach`, `stop`;
adapters/EVIDENCE.md) whose terminal result is awaited with a bound, and a step
that ends without a known outcome is UNVERIFIED. The game re-checks ownership,
occupancy and AI state on every frame and stops what a lost or replaced
companion session started.

Results: COMPLETE = all observations verified; BLOCKED = invalid input, unavailable
pair/capability, reservation conflict, or any failure before RESERVE (nothing moved); FAILED = adapter rejected/failed a step
and stop was confirmed; UNVERIFIED = observation mismatch or unconfirmed stop.
Reservations release only after a confirmed stop following attempted physical work.
Failure to stop/release retains ownership conservatively; before each new
request the service tries to confirm that stop (a game `stop` ending DONE and a
fresh snapshot showing both units at rest) and releases only then, so a lost
link mid-run needs no companion restart. Repeated identical
request IDs return the cached outcome within the same executor instance; changed
payloads with the same ID are blocked. Execution is serialized in that instance.
Replay across companion restarts is prevented by the bridge, not by persistence:
a new companion is a new session, and the game never runs another session's or an
already-seen command. There is no crash recovery: a companion crash stops its motion
in game after 10 s, leaves the equipment where it stopped, and needs no replay.

## Non-goals
No generalized game-agent framework, custom navigation replacement, custom
pathfinding replacement, LLM steering, computer vision, multiplayer, or speculative
infrastructure. Do not replace AutoDrive, Courseplay, Follow Me, or Universal
Autoload. Do not vendor upstream code without a demonstrated need and license review.

Voice, local model integration, purchasing, loading, fieldwork, harvesting and policy
are future vertical slices, not part of AC-001.

## Finished-product definition (user, 2026-10-17)
Normal operation of the AC-001 farmhand needs none of: the developer console,
manual vehicle IDs, manual JSON editing, a manually constructed AutoDrive graph,
a Python terminal, or routine developer intervention. Each requirement and how
the source meets it:

| Requirement | Source mechanism | Proven only in FS25 |
| --- | --- | --- |
| No developer console | On-foot keys (modDesc actions): Talk (text dialog), Fetch, Stop; console commands are diagnostics only | Keys register and fire; the dialog opens; notifications show |
| Natural-language text | Typed words → rules (+ local model) → command proposal → coordinator → executor; replies ACCEPTED / BUSY / STATUS_* / STOPPING / CANCELLED / COMPLETE / BLOCKED / FAILED with reason | Text dialog round trip |
| Fast deterministic STOP and STATUS | Stop key and rule path, never a model; cancel before the next step plus a pre-empting bridge `stop` | Pre-emption of a running GIANTS/AutoDrive job |
| AI model, chosen by the user (optional) | `companion/ai.py`: the in-game AI menu (Left Alt + N, "choose AI model", offered once at first connection) lists the user's Ollama models, any Farmhand-installed model, the two Farmhand downloads (Lightweight Qwen3 1.7B, Recommended Qwen3 4B) and "Continue without AI"; the pick is validated (bounded) and only then saved; never substituted. Ollama runs it with its own CPU/GPU behaviour; the managed path is a CPU-only llama-server. Behind the rules, re-validated, off the request loop; down, slow, missing or unsure means "not understood" | Ollama on the user's PC; latency beside FS25 |
| No-AI mode | Every capability above works from the deterministic rules and the keys; words outside them are answered "not understood" at once | — |
| Voice (optional front end) | `companion/voice.py`: hold-to-talk key (Left Alt + V), local faster-whisper `base.en` on the CPU into the text path; spoken replies via Windows' built-in voice; no microphone, packages or model disables only voice (VOICE_UNAVAILABLE). Proven on generated fixtures in CI | A real microphone (not a blocker) |
| No manual vehicle IDs | The executor selects the pair from observations | Equipment detection on the user's farm |
| No manual JSON editing | Tractors recognised from GIANTS data; `farmhand.json` only for optional field targets | The tractor rule matches the user's tractors only |
| No manual AutoDrive graph | GIANTS GOTO travel and delivery; AutoDrive optional and preferred when a marker fits | GOTO with the wagon attached, arrival accuracy |
| No Python, terminal or setup work | One package: `Setup.cmd` runs the bundled official Windows embeddable Python (pinned, sha256 = python.org's signed digest) with the pinned packages inside; it finds the FS25 profile, installs the mod and the companion (`%LOCALAPPDATA%\FS25Farmhand`), registers a windowless logon start (`pythonw`, HKCU Run), starts it and confirms in a Windows dialog; updates keep settings and models; `Uninstall.cmd` removes it. Windows CI installs and uninstalls the built package for real | Logon start, the dialogs |
| No routine intervention | One companion per PC (first pipe instance), session renewal after a drop, results as in-game notices, log in `modSettings/FS25_Farmhand/companion.log` | Recovery after game restart, sleep and companion restart |

The finished product is distributed as one ZIP of that package, built only after
the user approves the release; CI uploads the same folder as a qualification
artifact, never labelled a release. The definition is met only when the single
consolidated live session in the runtime gate below passes on the user's PC.

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

No FS25 binaries/assets are downloaded or packaged. CI builds the exact
`FS25_Farmhand` folder (scripts/package_probe.py); it is installed as a folder,
never a ZIP, only for the first FS25 test.

The upstream feasibility probe is recorded in adapters/EVIDENCE.md. Result: read-only
observation, AutoDrive marker delivery, GIANTS GOTO travel with an attached wagon,
stop, and every HITCH primitive (joint
geometry, GIANTS GOTO navigation, driveToPoint reversing, attach-range detection,
normal attachment, attachment observation) are backed by GIANTS/upstream source.
Their composition on a real tractor and forage wagon is UNVERIFIED until an FS25
test. Power and PTO come from GIANTS data on the loaded vehicles, and tractors are
recognised from GIANTS data (self-propelled with a motor and a PTO output, not a
loader category), so a real pair is selectable with no configuration once the
companion's bridge session is live. The local IPC is the Farmhand Companion
pattern: a companion-hosted Windows named pipe (game → companion) plus an atomically
renamed XML command file and a beating session file (companion → game). Do not
build navigation beyond ALIGN_REVERSE.

The first FS25 test runs the whole of AC-001 the way the player will (install
once, then the in-game key; steps in mod/README.md), with nothing else enabled,
and then the same with AutoDrive. If HITCH fails there, the diagnostic console probe
(`fhHitchList`, `fhHitchProbe`) repeats just that part with the same bridge
operations and logs the geometry, so the assist radius, vertical guard and
reverse speed can be tuned from logged values.

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
