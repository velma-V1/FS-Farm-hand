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
final hitch pose, facing the wagon's heading, run only while the planned tractor
joint is more than 25 m from the wagon's input joint; coarse: GOTO may end anywhere
(live at 8ffafcc twice NOT_REACHABLE 12.9 and 18.8 m off), so it need only end
within those 25 m, else TRAVEL_ENDED_TOO_FAR; never retried) → OBSERVE (the
planned tractor joint relative to the wagon's input joint, from a fresh snapshot)
→ LOCAL_MANEUVER, only if that is outside the reverse's envelope (within 9 m, on
the wagon's front side, within 30 degrees and 2 m of its axis): Farmhand's own
bounded forward drive, steered by GIANTS' driveToPoint, along one shortest
forward curve of the tractor's own turning radius (its LOCAL_TURN, LOCAL_APPROACH
and LOCAL_LINEUP phases) onto the wagon's centreline 3-8 m ahead of its joint,
chosen before moving to keep clear of a 25 m work radius, of the wagon's body, of
every other vehicle and of the player, re-observed every frame; refused before
moving with a precise reason when no such curve exists, and stopped on divergence,
stall, timeout (120 s), leaving the area, nearing the wagon, wagon movement, any
other vehicle moving or coming close, the player coming close, a rider, takeover
or other AI (static objects are not observed: hitting one shows as a stall) → OBSERVE again
(the envelope must hold, else that precise failure and nothing reverses) →
ALIGN_REVERSE (the bounded local
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
one bounded bridge operation (`drive`, `navigate`, `stage`, `maneuver`, `align`, `attach`, `stop`;
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
build navigation beyond LOCAL_MANEUVER and ALIGN_REVERSE.

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

## Completion plan (user, 2026-10-20; companion requirements 2026-10-21)
Farmhand is the coordinator between GIANTS and proven installed mods (AutoDrive,
Universal Autoload, FollowMe, belts and winch mods), with custom code only for the gaps.
It works on every map: nothing map-specific is hard-coded; destinations come from
observation (GIANTS fields, owned buildings, AutoDrive markers). Status of each stage:
implemented / automated-test-passed / live-qualified are reported separately
(tests/LIVE_QUALIFICATION.md is the live record).

| Stage | Scope | Status (2026-10-21) |
| --- | --- | --- |
| 1 Baseline | every live test records build, map, save, mods | done (livetest records) |
| 2 Language | phrasings reach the same workflow; STOP immediate | rules + optional model; sequences |
| 3 Assembly | routes, GMC gooseneck hitching, tailgates, crowded yards | GMC + 1124 Deckover live-qualified (hitch, move, detach, re-hitch) |
| 4 Farming | plow, cultivate, seed, fertilize, stones, weeds, harvest, refill, unload, tool change | field-work plans; partly live |
| 5 Commerce | buying (configurations), pallets via Universal Autoload | buying with hitch and engine options live-qualified |
| 6 Tractor transport | ramps, LOAD, belts, delivery, UNLOAD | LOAD + belts + ramps live-qualified once (Series 9 on the Deckover); loaded delivery needs an AutoDrive route |
| 7 Qualify and package | single consolidated live session, package | pending |

Final acceptance request, one command: "Take the GMC, hitch the Deckover, load the
tractor, secure it, raise the ramps, deliver it to field 3, and unload it", and the
goal form "Take the Deutz to field 3 and unload it" from an unprepared and from a
partly completed state, with a changed destination, a player takeover and a resume.

### Companion requirements and how each is met (extend what exists)
1. **Goals from observed state** (coordinator plans, executor verifies). A goal is a
   plan of executor steps; before each step its post-condition is checked on a fresh
   snapshot (hitched, loaded and belted, ramps up, at the destination, unloaded) and a
   satisfied step is recorded ALREADY_DONE, not repeated. The executor's own jobs
   continue from partial states (a tractor on the deck but not belted is belted; ramps
   left down are raised).
2. **Loaded delivery** by AutoDrive only (AGENTS.md); a GOTO route for a loaded rig is
   BLOCKED LOADED_NEEDS_AUTODRIVE before reserving. Destinations resolve to an
   AutoDrive marker by name, by the configured field entrance, or the marker nearest the
   destination's unloading area on a route AutoDrive can reach; none: the job is kept,
   the gap reported.
3. **Unloading only on arrival**: UNLOAD runs only after the delivery step observed the
   rig at rest at the destination (tolerance) — never after a stop, cancel or failure.
4. **Pause / change / takeover / resume**: STOP pauses the goal (its plan and the steps
   done are kept); "continue" re-plans from observed state; "take it to field 2 instead"
   replaces the destination of the kept goal; a takeover (DRIVER_TOOK_OVER) pauses it.
5. **Recovery after reloads or companion crashes**: the kept goal is persisted beside the
   companion's settings; on start it is reconciled with the observed world (equipment
   re-identified by name, brand and role, not by session IDs) and offered as "continue";
   a purchase step records what it bought so a repeat finds it instead of buying again.
6. **Names and references**: vehicles by store name and brand ("the Deutz"); persistent
   nicknames; "it", "that trailer", "take it back" refer to the goal's own equipment and
   its origin (move_pair to origin exists).
7. **Spending and permissions**: saved spending limit, equipment preferences, and
   whether buying/selling is allowed, checked before any purchase.
8. **Named destinations**: a destination carries its approach, parking and unloading
   places (AutoDrive markers or GIANTS positions), chosen from observation.
9. **Truthful progress**: notices and STATUS from observed state (moving, at the
   destination); an accepted command is "started", not "driving".
10. **Queued and recurring jobs** with explicit completion and stop conditions.
11. **Human help**: the smallest intervention, said once; the job kept for "continue".
12. **FollowMe**: only on an explicit "follow me" (its AIJobTypeFollowVehicle), qualified
    separately; never a substitute for delivery.
Voice: deferred.

Implementation order: (1) and (3) with the goal form of the transport request; (2)
destination resolution to AutoDrive; (4) pause/resume/change; (6) references; (5)
persistence and reconciliation; (9) progress; (7), (8), (10), (11), (12).

## Product requirements (user, 2026-10-22) — the finished companion
The goal is a fully usable local gaming companion and worker, not a set of individually
demonstrated commands. Completion is judged against these areas; each has release gate
G1-G9 in tests/ACCEPTANCE.md, with implemented / automated-test / live status kept apart.
They extend the completion plan above (stages 1-7) and supersede "Voice: deferred".

1. **Complete natural-language jobs.** "Buy this", "sell that", "use the truck and
   trailer to move the tractor to the farm / field 3 / the store", "plow field 6, seed it
   with wheat, then fertilize it": references, equipment, compatibility, prerequisites,
   supplies, destinations and authorized purchases resolved; the whole job executed and
   verified without the player listing mechanical actions. Selected equipment keeps its
   identity through the job; a change of circumstance is replanned explicitly, never by
   silently switching rigs (live 2026-10-22: the load took another trailer than the one
   staged; fixed: a haul keeps one transport).
2. **Equipment functions.** Observe and operate what GIANTS and the installed equipment
   expose: attach/detach, raise/lower, fold/unfold, turn on/off; tailgates, ramps, belts,
   covers, pipes, discharge; lights, turn signals, beacons where supported; crop/fill
   selection, operating modes, adjustable working widths; configurations. Capabilities
   discovered from the equipment and supported APIs; runtime controls kept apart from
   options needing purchase or workshop customization; no invented controls, forced
   animation states or confused fold actions. Reachable by words and used automatically
   by jobs; unsupported functions reported specifically.
3. **Independent work and riding along.** Farmhand works on while the player walks,
   drives other vehicles, uses menus or does unrelated work; that never cancels a job.
   Passenger occupancy (FS25's own passenger mechanism, where a vehicle has it) is not a
   driver takeover; a vehicle without passenger seats is said so, never silently driven
   from its driver seat. Manual driving of a vehicle Farmhand controls has one explicit
   handoff; never two controllers. STOP by text in Farmhand's chat and by local voice,
   responsive while interpretation or another task is busy; the goal kept for "continue".
   Collision stops and genuine faults pause with the reason shown, the job kept.
4. **Complete travel, large maps.** Departure, road travel, the road-to-yard/field
   transition, final approach, delivery and parking. The end of a road or recorded route is
   not the end of a delivery: provider handoffs (GIANTS GOTO, AutoDrive, Courseplay where
   installed) and reachable final approaches; route connectivity and whole-rig clearance
   inspected before dispatch; an installed mod is not assumed to give a usable route.
   Long journeys on 16x maps: progress judged by actual movement and provider state, not a
   short fixed timeout. 16x support is claimed only after qualification on a 16x map;
   physically inaccessible destinations are never claimed.
5. **Implements returned after each field operation.** Before an implement is collected its
   parking pose, orientation and access space are remembered (kept across pauses and
   reloads); after its operation it is made ready for transport, returned there, detached
   and left usable, then the next implement is fetched. Obstructed: the location is kept and
   reported, or an authorized nearby alternative used. An explicit "take it to ..." overrides
   it; newly bought equipment gets a designated home (the store spawn is not a home).
6. **Supplies and land.** Compatible tools and supplies; filling through the actual station
   interfaces, the fill type and quantity verified; the designated free fieldwork fill
   station after its capabilities and cost are verified (no simulated filling, no assumed
   supply). Explicitly requested land purchases through the game's own mechanism: the right
   parcel, price, funds, ownership change, no duplicate after an interruption.
7. **Visible task control.** An in-game Farmhand chat/task view: the original request, the
   current step and its equipment, observed progress and the next step, queued jobs and
   background workers, any waiting/blocking reason, stop/pause/continue. Never "driving"
   without movement or "complete" when a helper only started; routine messages unobtrusive,
   the player notified when done or needed. Local voice input is in the release scope,
   sharing the text path and STOP.
8. **Final integrated acceptance scenario** (through the player-facing interface on a
   backed-up test save, fully autonomous: no developer commands, manual positioning or
   hidden corrections; assisted runs diagnose but never pass): buy a compatible truck,
   transport trailer, tractor and plow; hitch truck-trailer and tractor-plow; load and secure
   the tractor with its plow, ramps and equipment in travel configuration; transport them to a
   small field far from the store; buy that land parcel; unload and plow; load again and
   transport to the farm; unload, park and unhitch the plow at its designated farm location;
   attach the seeder and fill it at the designated free fieldwork fill station. The
   transport combination must actually carry and secure the tractor with its plow. Separately
   "plow, seed wheat, fertilize" with every implement returned to its pickup place. During
   qualification the player rides as a supported passenger, uses another vehicle and plays
   unrelated, a delivery crosses from roads into the destination area, STOP / continue /
   restart recovery are exercised, and equipment selection and transactions are preserved.
9. **Execution discipline.** Shared identity and language defects first, then workflows in
   dependency order; existing implementations reused; related changes batched; decisive
   diagnostics before retries; valid prior evidence kept. Farmhand is not finished because
   selected examples pass.


## Universal production release (user, 2026-10-22) — extends the product requirements

The same plan, widened: Farmhand is a universal, conversational, world-aware companion for
arbitrary FS25 maps and compatible (including modded) content, built from the smallest
maintainable architecture on what exists (coordinator, planner, local model, game bridge,
GIANTS interfaces, AutoDrive, Courseplay where installed). Gates G10-G13 in
tests/ACCEPTANCE.md; the current stage is finished and verified first.

10. **Universal world discovery.** Vehicles, equipment, locations (fields, farms, shops,
    production, storage, sell points, loading areas, roads, yards, user-named places),
    buyables (vehicles, land, buildings, supplies), the player and passengers, and world
    state (ownership, availability, cargo, fill, pose, activity, current tasks) come from
    runtime observation and supported APIs; nothing assumes a particular object, shop,
    field, route or vehicle exists; new compatible content is discovered without per-item
    logic.
11. **Universal selection under constraints.** The model extracts the objective,
    references and constraints only; the deterministic planner resolves real objects and
    ranks feasible resources: required capabilities, power and towing suitability, hitch
    compatibility, trailer capacity, dimensions, cargo and availability, loading/unloading
    method, ownership, reservations and commitments, access, then distance and time.
    Feasibility and explicit user constraints outrank convenience and proximity. Nothing
    the game does not expose is invented (unknown capability: conservative, said so). A
    named vehicle ("use the GMC", "truck 478", "this tractor") is kept, and a confirmed
    incompatibility is explained, never silently substituted; "a truck", "an empty
    trailer", "another trailer" select generically; contextual destinations ("there", "the
    farm") are resolved from context, asked only when truly ambiguous. The model never
    invents IDs; the executor binds identities and keeps them through the job and restart
    recovery; equal names never cause a silent swap.
12. **Reusable capability operations.** Navigate, approach, align, attach/detach,
    load/secure/carry/unload, operate controls, field operations, refuel/refill, buy,
    sell, collect, store, park, bounded recovery: one capability-discovery and execution
    contract over verified GIANTS/mod interfaces. No business logic per truck, trailer or
    model; a vehicle-specific adapter only for a real API difference.
13. **Universal navigation and field work.** Actual map and world information (no
    hardcoded coordinates or routes): rig dimensions, articulation, terrain, obstacles,
    road-to-field/yard transitions, safe loading/turning/parking places, dynamic blocks.
    Reaching an AutoDrive waypoint is not reaching the destination. Field work from the
    real field geometry, implement width and operation. No teleporting, no ignored
    collisions, no arrival or completion without observation; an unsafe route keeps state
    and reports the blocker. One prepared route working is not universal navigation.
14. **Dependency-aware multi-step plans** from objectives ("a suitable truck and an empty
    trailer, pick up Z from the store, bring it to my new field, unload it, park
    everything"): resolve, select, sequence, execute, observe, adapt within the user's
    constraints, verify; durable job state with exact identities; no recipe per
    vehicle/equipment/map combination.
15. **One conversational companion.** Casual talk, farming and technical questions,
    business advice, roleplay, game-state and job-status questions, work requests and
    mixtures of them, by voice and text. Conversation is kept apart from validated
    executable intents (talk and roleplay never buy or act); responsive while jobs run;
    new messages never silently cancel or replace a job; STOP independent of inference.
16. **Release capabilities to finish:** persistent homes and parking, task queue and
    progress with restart recovery, multi-step load/transport/unload/delivery, land and
    equipment purchase, filling stations and resources, universal equipment controls,
    passengers, field preparation through harvest (weeds, stones), pallets (handle,
    store, sell, transport), long-distance and 16x navigation, farm-business workflows.
    The truck → trailer → equipment → field → farm sequence is an integration test, not a
    hardcoded workflow. Excavator/forestry manipulation stays in its later phase.
17. **Universality verified, not assumed:** two or more vehicles of different capability
    and distance; trailers with different cargo and configurations; explicit versus
    generic selection; duplicate names; different maps, fields, destinations and roads;
    different implements through shared operations; loaded and empty transport; partly
    compatible modded equipment; obstruction, inaccessibility, insufficient capacity,
    restart; conversation during jobs. Automated tests for resolution, feasibility,
    state, identity, recovery and planning; live play for physical work; regressions
    from discovered failures, not hand-authored vehicle rules. Architecture-level
    generality, tested compatibility and unverified scenarios are reported apart.
18. **Production package:** install, update, dependencies, state preservation, recovery,
    offline operation within supported limits, uninstall and FS25 runtime performance
    verified on the real distributable, with no developer-only steps. A fundamental
    failure of selection, planning or operation contracts blocks release.
