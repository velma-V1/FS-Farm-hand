# FARMHAND — AUTHORITATIVE ENGINEERING CONTRACT

## 0. Required FS25 Reference

Before implementing or changing Farmhand behavior, use the shared FS25 Modding Toolkit as the primary local reference:

- Windows: `C:\\Users\\Matt\\Desktop\\FS25 Modding Toolkit`
- WSL: `/mnt/c/Users/Matt/Desktop/FS25 Modding Toolkit`
- Master guidance: `skills/fs25-master-skill/SKILL.md`

Use the toolkit before guessing about GIANTS behavior, FS25 APIs, provider mechanics, vehicle systems, pathfinding, attachments, fieldwork, cargo, heavy equipment, or reusable mod code.

Reference priority:

1. exact installed FS25 dataS
2. Community LUADOC
3. UsedPlus FS25 AI Coding Reference
4. applicable FS25 skills/guides
5. community dataS
6. provider source
7. web only when local references are insufficient

Agents must inspect the relevant toolkit/source first and reuse proven mechanisms where practical.

---

## 1. Mission

Farmhand is a **standalone, universal, autonomous Farming Simulator 25 worker and AI companion**.

The player communicates naturally. Farmhand determines how to accomplish the requested goal using the current world, available equipment, game mechanics, and its own capabilities.

Primary loop:

`USER GOAL → UNDERSTAND → OBSERVE → RESOLVE → PLAN → ACT → VERIFY → RECOVER/REPLAN → COMPLETE`

Farmhand should feel like another competent person working on the farm with the player.

**KEEP CAPABILITY; MINIMIZE IMPLEMENTATION.**

**ONLY BETTER, NEVER WORSE.**

Every patch, fix, bridge, adapter, workaround, or subsystem change must be the smallest general change that solves the real problem while preserving or improving all existing user-visible Farmhand capability.

Prefer:

`smallest general fix → maximum retained capability → minimum new complexity`

Do not:

- replace a broad working capability with a narrower one
- add policy that blocks previously valid user actions
- duplicate systems when a small bridge or adapter is enough
- introduce a new dependency when a small reusable component solves the gap
- weaken reliability, autonomy, generality, usability, or player control just to make a test pass

A change is not an improvement if it fixes one case by making Farmhand less capable elsewhere.

Do not sacrifice product capability merely to simplify tests or architecture.

---

## 2. Product Standard

The player states **what they want**, not how Farmhand must implement it.

Examples:

- `take the GMC and Series 9 to field 5`
- `unhitch the trailer`
- `bring the roller back to the farm`
- `buy another tractor`
- `sell the old trailer`
- `load these pallets`
- `take this grain to the sell point`
- `harvest field 7`
- `follow me`
- `move that excavator over there`
- `clear those trees`
- `dig this area`
- `what should I plant here?`
- normal questions, conversation and roleplay

Farmhand owns the implementation.

The user must not need to understand:

- internal command names
- provider states
- AutoDrive markers
- Courseplay jobs
- pathfinder names
- recorded homes
- internal IDs
- test infrastructure

---

## 3. Single-Command Autonomous Operation

**Every Farmhand task must be executable autonomously from nothing more than a normal typed or spoken command, prompt, sentence, phrase, or conversational request from the user.**

The user specifies the goal. The user does **not** manually perform or coordinate the intermediate steps.

The AI model is responsible for:

- communicating naturally with the user
- understanding intent and context
- asking only genuinely necessary clarifying questions
- selecting and invoking the appropriate Farmhand capabilities
- explaining progress, results, or real blockers

Farmhand is responsible for:

- converting that intent into executable game actions
- operating FS25's functions, systems, vehicles, equipment, interfaces, and mechanics
- operating the capabilities incorporated into Farmhand from other mods
- sequencing all required intermediate actions
- recovering and replanning when an action fails
- verifying the physical result

The required relationship is:

`USER ↔ AI MODEL → FARMHAND → FS25`

The AI model communicates and decides **what Farmhand needs to accomplish**.

Farmhand determines and executes **how to accomplish it inside the game**.

The user must never be required to manually orchestrate a task that Farmhand has the physical/game capability to perform.

---

## 4. Standalone Architecture

Farmhand must provide its **core worker capability without requiring other gameplay mods**.

Other mods are source material or optional accelerators.

Farmhand owns:

- natural-language intent
- conversation/context
- world observation
- equipment identification
- persistent identity
- attachment/cargo relationships
- equipment selection
- planning
- task sequencing
- driving
- hitch/detach
- loading/unloading
- equipment operation
- purchasing/selling
- storage/delivery
- recovery/replanning
- STOP/cancel
- verification
- persistent task state
- user-facing responses

Farmhand remains the single coordinator.

Only one controller owns a vehicle or machine at a time.

---

## 5. Reuse Existing Proven Work

Do not unnecessarily recreate systems already solved well.

Use the local FS25 Modding Toolkit and available source from:

### Courseplay
Useful for:

- Hybrid A*
- A*
- JPS
- Dubins
- Reeds-Shepp
- field course generation
- vehicle dimensions
- collision/path constraints
- proximity
- pure pursuit
- field operation logic

### AutoDrive
Useful for:

- road routing
- pathfinding concepts
- road network information
- collision/slope/water checks
- speed control
- turn behavior
- obstacle/stuck detection

### GIANTS FS25
Use native:

- vehicle controls
- world state
- AI/navigation data
- traffic/road splines
- terrain information
- attachment mechanics
- production/economy/store systems
- field systems
- equipment specializations

### Other useful sources
- FollowMe → following/convoys
- Universal Autoload → cargo/loading
- TerraFarm → digging/material/terrain operations
- GPS/WayPoint systems → road and last-mile routing concepts
- forestry mods/tools → specialized forestry behavior

Port/adapt only useful pieces.

Do not import unnecessary:

- menus
- configuration frameworks
- duplicate coordinators
- complete competing job managers
- redundant state systems

External mods may still be supported optionally, but **Farmhand must not become dependent on them for ordinary worker behavior**.

---

## 6. Universal World and Map Requirement

Farmhand must work on **any normal FS25 map**, including:

- base-game maps
- mod maps
- sandbox maps
- maps with roads
- maps with poor road networks
- maps with no predetermined routes
- farms created entirely by the player

Do not hardcode Riverbend, specific fields, fixed coordinates, named roads, specific equipment, or known layouts.

Discover the world at runtime.

Farmhand must reason from:

- terrain
- roads/splines when available
- traversable ground
- obstacles
- buildings
- fields
- productions
- sell points
- shops
- placeables
- equipment
- player-created areas

---

## 7. Universal Driving Contract

Authoritative rule:

> If a competent player can drive the SAME selected vehicle or rig from its current position to the requested destination using normal FS25 controls, Farmhand should ultimately be capable of doing the same autonomously.

Routes must not require:

- predefined AutoDrive routes
- AutoDrive markers
- roads
- predetermined paths
- predetermined passages

Farmhand must support:

- road driving
- farmyards
- fields
- trails
- open terrain
- off-road travel
- arbitrary destinations
- player-created farms
- last-mile movement
- reversing
- trailers
- articulated/long equipment where practical

Navigation:

`DESTINATION`
→ determine usable operational pose
→ use road route where helpful
→ generate missing route
→ create off-road/last-mile connection
→ account for current rig
→ drive
→ observe
→ recover/replan
→ arrive
→ stop
→ verify

Provider failure is not destination failure.

These alone must never terminate the objective:

- `AD_NO_PATH`
- AutoDrive has no destination
- AutoDrive has no marker
- GIANTS helper refuses the rig
- Courseplay has no applicable job
- another provider is unavailable

Only actual physical limitations or genuinely exhausted Farmhand capability justify terminal navigation failure.

Never tell the player to drive it manually merely because one provider failed.

---

## 8. Equipment and Rig Understanding

Farmhand must understand the **current assembled physical configuration**.

Example:

`GMC → Deckover → belted Series 9`

is an existing transport rig.

If the player says:

`take the GMC and Series 9 to field 5`

Farmhand preserves and moves that existing rig.

Do not incorrectly convert it into:

- find tractor
- find implement
- fetch pair
- detach
- rehitch
- rebuild rig

Explicit names mean exact equipment.

Generic terms may select suitable equipment:

- tractor
- truck
- trailer
- cultivator
- harvester
- loader
- excavator

Contextual terms must work when unambiguous:

- it
- this
- that
- rig
- trailer
- machine
- tractor

Ask only the smallest necessary clarification when genuinely ambiguous.

---

## 9. Hitching / Detaching / Equipment Configuration

Farmhand owns:

- finding compatible equipment
- approaching
- alignment
- reversing
- hitching
- attachment verification
- PTO/connection state where applicable
- detaching
- folding/unfolding
- straps/belts
- configuration sequencing

Explicit user intent overrides convenience policy.

Example:

`unhitch the trailer`

means:

`identify → stop safely → detach here → verify → COMPLETE`

A recorded home is **not required**.

Homes are useful for autonomous parking/put-away behavior only.

---

## 10. Loading / Unloading / Transport

Farmhand must support transportation workflows including:

- trailers
- flatbeds
- vehicles on trailers
- implements
- pallets
- bales
- grain
- seed
- fertilizer
- liquids
- logs
- supported bulk/material cargo

General loop:

`find/load cargo`
→ `secure/configure`
→ `transport`
→ `position`
→ `unload`
→ `verify`

Use existing proven loading systems where useful, but Farmhand owns the objective and verification.

---

## 11. Farming and Field Work

Farmhand should ultimately support:

- cultivating
- plowing
- seeding
- planting
- fertilizing
- spraying
- rolling
- mowing
- tedding
- windrowing
- baling
- harvesting
- collecting
- transporting
- field preparation
- appropriate implement selection

Use proven field-course and machine-control algorithms rather than recreating them unnecessarily.

---

## 12. Economy / Store / Business Operations

Farmhand must be able to reason about and, when authorized, perform economic actions involving:

- vehicles
- tractors
- trucks
- trailers
- implements
- attachments
- equipment
- products
- seed
- fertilizer
- crops
- pallets
- bales
- materials
- livestock-related products where supported

Required classes of actions:

- inspect available equipment
- determine compatibility
- buy
- lease where supported
- sell
- deliver purchases
- transport products
- choose sell points
- sell products
- move goods into/out of storage
- use productions
- assist with farm/business decisions

Irreversible or expensive actions must respect authorization policy, but Farmhand should not create unnecessary confirmation loops.

---

## 13. Heavy Equipment / Construction / Forestry

Farmhand is not limited to tractors.

It should ultimately operate supported:

- excavators
- dozers
- wheel loaders
- skid steers
- telehandlers
- forestry harvesters
- forwarders
- loaders
- trucks
- cranes
- other controllable equipment

Tasks may include:

- driving
- positioning
- digging
- grading
- loading
- unloading
- moving material
- tree cutting
- processing
- log handling
- transport

Use TerraFarm/forestry/provider mechanics where valuable, but integrate required capability into Farmhand's autonomous worker model.

---

## 14. AI Companion

Farmhand is also a companion, not only an executor.

It must support:

- normal conversation
- farming questions
- equipment questions
- explanations
- planning
- recommendations
- farm/business assistance
- context-aware dialogue
- roleplay/personality
- discussion while work is occurring

Execution and conversation share context.

The player should be able to ask things such as:

- `what are you doing?`
- `why did you stop?`
- `what should we plant next?`
- `how much grain do we have?`
- `what tractor would work here?`
- casual conversation/roleplay

Conversation must not corrupt active task state.

---

## 15. Explicit Intent and Internal Policy

User intent is the objective.

Internal policies exist to help accomplish it, not to block reasonable explicit commands.

Examples:

- no equipment home → does not block explicit detach
- no AutoDrive route → does not block driving
- provider refuses → does not end objective
- currently assembled rig → preserve it unless change requested

Do not create arbitrary restrictions merely because they simplify implementation.

---

## 16. Failure Semantics

Keep failures layered.

### Interpretation
Farmhand genuinely cannot determine requested action.

### Resolution
Requested equipment/place/object cannot be uniquely resolved.

### Authorization
Action requires authorization that is absent.

### Planning
No valid plan has yet been constructed.

### Execution/provider
A chosen execution method failed.

### Physical
Actual world geometry/physics prevents the action.

Do not report downstream failures as:

`NOT_UNDERSTOOD`

Do not expose provider implementation details unnecessarily.

Farmhand should attempt recovery/replanning before returning failure.

---

## 17. Verification

Command acceptance is not completion.

Farmhand verifies actual world outcome.

Examples:

- vehicle physically arrived
- trailer is actually detached
- implement actually attached
- cargo actually loaded/unloaded
- product actually sold
- purchase actually exists
- field operation actually occurred
- machine actually stopped safely

Insufficient proof means incomplete/unverified, not success.

---

## 18. Persistence and Recovery

A task must survive reasonable interruption/restart without becoming a different task.

Persist enough identity/state to preserve:

- objective
- exact equipment
- attachments
- cargo
- destination
- progress
- authorization
- reservations

Recovery must never silently substitute the wrong vehicle/rig/objective.

---

## 19. Teleport

Teleport may exist as an explicit optional capability.

Modes may include:

- Drive only
- Ask before teleport
- Allow teleport recovery

Normal autonomous behavior uses physical gameplay.

Never silently teleport because navigation became difficult.

---

## 20. Development Method

Do not perform broad audits while an important player-facing behavior is broken.

Fix vertically through the actual product.

Required loop:

`REAL Alt+K FAILURE`
→ `ROOT CAUSE`
→ `SMALLEST GENERAL FIX`
→ `FOCUSED TEST`
→ `REAL Alt+K RETEST`

Once live behavior works:

→ relevant regression suite
→ full `scripts/check.py`
→ `git diff --check`
→ inspect
→ one logical commit
→ install exact build
→ final live verification

Do not burn model usage repeatedly running full gates after tiny edits.

Do not abandon the current failing command to work on unrelated easier features.

Do not create phrase-specific fixes where a general semantic/mechanical fix is possible.

---

## 21. Live Acceptance

The actual FS25 Farmhand interface is authoritative.

A capability is not LIVE-QUALIFIED merely because:

- unit tests pass
- mocked tests pass
- interpreter JSON is correct
- runner request succeeds
- provider accepts the command

LIVE-QUALIFIED means the player requests the action through Farmhand and the correct physical result occurs in FS25.

---

## 22. Immediate Blocking Acceptance Gate

Before unrelated expansion, make these work through actual Alt+K:

### A
`unhitch the trailer`

Expected:

`stop → detach current trailer here → verify → COMPLETE`

### B
`take the GMC to the farm barn`

Expected:

`resolve exact GMC/current rig → route → physically drive → stop → verify → COMPLETE`

### C
`take the GMC and Series 9 to field 5`

Expected:

`resolve existing GMC + Deckover + belted Series 9 → preserve rig → autonomously route anywhere traversable → drive → stop → verify → COMPLETE`

No:

- fetch/re-hitch mistake
- home requirement
- AutoDrive marker requirement
- `drive it yourself`

---

## 23. Runtime Discipline

Preserve a known-working Xbox/Game Pass FS25 process.

Do not kill/restart FS25 merely to activate normal Farmhand changes.

Prefer:

`build/install → unload save → reload save → verify exact build`

Codex/Claude must manage Farmhand development-runner restarts themselves when possible.

Do not make the user perform routine terminal work the agent can perform.

---

## 24. Engineering Priorities

In priority order:

1. usable end-to-end behavior
2. correctness
3. recovery/reliability
4. generality
5. reuse of proven code
6. maintainability
7. test coverage
8. documentation/polish

Tests serve the product.

The product does not serve the tests.

---

## 25. Final Definition

Farmhand is complete when it behaves like a reliable additional farm worker and companion rather than a collection of automation providers.

The player should be able to give **one natural typed or spoken request** and then let Farmhand perform the entire task autonomously.

The AI model communicates with the player and directs Farmhand.

Farmhand operates FS25 and its integrated standalone capabilities.

**The player tells Farmhand what needs done. Farmhand figures out and performs everything required to do it.**
