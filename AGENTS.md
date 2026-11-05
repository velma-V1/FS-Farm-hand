## AUTHORITATIVE FINAL PRODUCT SCOPE — OWNER DIRECTIVE, 2026-11-05

Read [FINAL_FARMHAND_PRODUCT_SPEC.md](FINAL_FARMHAND_PRODUCT_SPEC.md) before any substantive work. It records the complete requested target including multiple simultaneous physical workers, moving-combine unloading as a release gate, live fields/contracts/world awareness, specialist vehicles/forestry/earthmoving/building operations, full farm/animal/production goals, fast precise navigation and recovery, natural character roles, and no AutoDrive execution. These are REQUIREMENTS, not claims of existing functionality.

The explicit current owner direction and final product requirements supersede old experimental/lab-only mission statements, narrow release definitions and historical AutoDrive-first behavior anywhere in this repository. This is PRODUCTION DEVELOPMENT, not an alternate-planner proving exercise. Keep qualified functionality, isolate relevant diagnostic experiments, and deliver the complete target with physical evidence.

## Claude Cloud ↔ FS25 lab reference (2026-11-04)

The production branch has a separate private MCP service `farmhand-live-lab`; see `CLOUD_LAB_ACCESS.md` for the current connection and verified non-focus-stealing hot-reload workflow. An MCP entry existing in `.mcp.json` does NOT prove this particular cloud session is authenticated. Verify with `toolkit_list("notes")` and `live_status()` before citing live/toolkit access. No arbitrary host shell, GUI input, game restarts, or cloud-code deployment is available through the gateway. Source access is for private research; do not publish GIANTS/provider code. Lab test successes are not proof that cloud changes ran in FS25. Preserve Codex's separate laboratory and the player's foreground.

## Current user directive — production begins (2026-11-04)

This branch is the **start of the actual standalone Farmhand product**, sourced from a faithful snapshot of the active lab. The lab continues separately as an optional proving ground, not the production milestone or the default workload. This newer explicit user objective supersedes Section 3's former lab-only mission on **this production branch**.

Deliver the working, polished, integrated Farmhand product now. Promote already proven capabilities; repair, simplify or replace unsuitable legacy code when that yields a better outcome. Do not require all possible primitives to be proved in advance of real product construction. Where uncertainty materially affects gameplay correctness, use a narrowly targeted lab experiment or executable test, carry the resulting proven component into production, and continue actual construction. Product-level behavior and trustworthy verification determine priority. A cloud session cannot prove live FS25 behavior without access to real gameplay; distinguish automated checks from actual game proof.

# FARMHAND — AUTHORITATIVE ENGINEERING CONTRACT

This file is the current engineering authority for Farmhand. Read it before substantial work.

Authority order:
1. current explicit user directive;
2. this file;
3. `CLAUDE.md` for agent-specific operating instructions;
4. current git state + current live FS25 evidence;
5. `PRODUCT_WORKING_MEMORY.md` and `RELEASE_BACKLOG.md` for supporting facts only;
6. historical specs, notes, tests, and comments.

There is no handoff file. Do not recreate one. Durable state belongs in code, tests, evidence, git, and the small product-memory/backlog files.

`BUILD_SPEC.md` is the historical foundation specification. It does not restrict the current product or standalone-navigation mission where it conflicts with this file.

# 1. Product mission

Farmhand is a **standalone, universal, autonomous Farming Simulator 25 worker and AI companion**.

The player states the objective in normal typed or spoken language. Farmhand figures out and performs the intermediate work.

Core loop:

`USER GOAL → UNDERSTAND → OBSERVE → RESOLVE → PLAN → ACT → VERIFY → RECOVER/REPLAN → COMPLETE`

The player should not need to know internal commands, vehicle IDs, provider states, route-network names, pathfinder names, or development infrastructure.

Primary engineering rule:

**KEEP CAPABILITY; MINIMIZE IMPLEMENTATION.**

**ONLY BETTER, NEVER WORSE.**

Prefer the smallest general primitive that preserves or increases player-visible capability.

# 2. Standalone means Farmhand owns the job

Farmhand must provide its core worker capability without requiring AutoDrive, Courseplay, FollowMe, Universal Autoload, or another gameplay mod.

GIANTS FS25 remains the game engine. Farmhand may and should use native engine data and mechanics such as:
- live transforms;
- physics/collision queries;
- terrain and field data;
- vehicle controls;
- wheels, joints, attachments, belts, implements and specializations;
- store, economy, production and field systems;
- native machine functions.

External mods and open-source implementations are references or optional accelerators. They are not authorities over the player objective and are not required for ordinary advertised Farmhand behavior.

## Owner directive 2026-11-05 — private reuse, temporary mod safety, native operation

- Farmhand is intended for **private, non-distributed use**. Reuse proved mechanisms from other mods where technically suitable, adapting them into Farmhand-owned native modules rather than requiring those source mods at runtime. Preserve provenance and applicable licensing terms; private use does not make all proprietary restrictions disappear. Do not publish or transfer borrowed code/assets without an appropriate rights review.
- First inspect GIANTS' available engine workers/functions and existing Farmhand code, including older bridges to external mods. Preserve working logic and migrate its required behavior to native Farmhand operations. Reimplement only what cannot be correctly reused. Farmhand remains the sole job owner, supervisor, state keeper, recovery orchestrator and completion verifier; it may direct suitable GIANTS helpers.
- Existing installed mods on the player's PC are **protected**. Before installing, enabling, modifying, or cleaning reference mods, inventory current mod directories, filenames, versions and hashes. Do not uninstall, overwrite or delete any pre-existing mod, even if an old Farmhand dependency. Prefer isolated download/source extraction over live installation; install a reference mod into the actual game only when a game-side experiment requires it.
- Maintain a ledger of precisely which mods/files Farmhand development installs, where, and when. After extracting and proving the needed functionality, remove **only** those development-added installations and their own temporary files, not the player's existing mods or settings. Never infer development ownership merely from a filename. Do not remove a temporary mod while still required by an active test.
- The player reports nearly every field on **Save 1** has been purchased and the game saved (2026-11-05). Treat Save 1 as a valuable broad field/map test environment; discover actual ownership live on each load, do not hardcode universal ownership or individual field coordinates, and protect the save from destructive experiments. Back up save data before any risky game-state changes.
- Build a reusable **per-map navigation cache**: road/track route graph plus detailed local navigability/clearance grids around complex sites, with live obstacle updates and vehicle/whole-rig geometry. Reuse applicable AutoDrive/Courseplay algorithms internally without executing their installed mods. Persist map-specific derived data; avoid expensive uniform whole-map high-resolution scans and measure real reliability and latency.


Farmhand owns:
- intent and conversation;
- world observation;
- identity and equipment resolution;
- physical object and rig understanding;
- route and motion planning;
- driving;
- hitching/detaching/configuration;
- loading/unloading/transport;
- field work;
- buying/selling/storage/delivery;
- heavy-equipment, construction and forestry workflows where FS25 supports them;
- recovery/replanning;
- STOP/cancel;
- verification;
- persistent task state;
- user-facing status and conversation.

Only one controller owns a machine at a time.

# 3. Current repository mission: complete the production product

This repository is the active Farmhand product, not an experimental navigation proving ground. Implement, integrate, qualify and release the complete owner requirements in FINAL_FARMHAND_PRODUCT_SPEC.md. Historical standalone-navigation experiments are a targeted diagnostic tool only, not a mandatory alternate-planner release milestone. Each useful primitive must improve a real end-to-end objective; preserve existing working navigation and integrations. Judge success by actual physics, full player goals and concurrent multi-machine tasks.

# 4. Required end-product capabilities

Farmhand's final product must be able to combine these capabilities into complete autonomous jobs.

## Communication and control
- natural typed requests;
- optional voice;
- normal conversation and roleplay;
- status/explanations while working;
- immediate durable STOP/cancel;
- continue/change destination/resume.

## World and object understanding
- discover the map and current world at runtime;
- resolve vehicles, implements, cargo, buildings, fields, productions, stores, sell points and player-created areas;
- preserve exact equipment identity and current assembled rigs;
- understand recursive attachment/cargo relationships;
- distinguish physical state from metadata guesses.

## Navigation and movement
- drive roads, yards, fields, trails, open terrain and last-mile approaches;
- arbitrary reachable destinations without pre-recorded routes;
- forward and reverse motion;
- articulated trailers and long rigs;
- close-quarters maneuvering;
- fast route start and continuous replanning;
- shortest/sensible safe routes rather than needless wandering;
- obstacle/no-progress detection and recovery;
- destination arrival, safe stop and physical verification.

## Equipment handling
- hitch, detach, fold/unfold, connect/configure, straps/belts;
- load/unload vehicles, implements, pallets, bales, grain, seed, fertilizer, liquids, logs and supported bulk cargo;
- preserve and transport already assembled rigs.

## Farming
- plow, cultivate, seed/plant, fertilize/spray, roll, mow, ted, windrow, bale, harvest, collect and transport;
- select suitable equipment;
- refill/unload/store/sell;
- create/join fields where supported.

## Economy and logistics
- inspect compatibility and inventory;
- buy, lease and sell when authorized;
- choose destinations/sell points according to the player's objective;
- move products between field, storage, production and sale.

## Heavy equipment / forestry / construction
- drive and position loaders, telehandlers, skid steers, excavators, dozers, forestry machines, trucks and cranes where supported;
- dig/grade/load/haul material;
- cut/process/load/transport trees and logs.
- for tree felling, physically align the attachment with the lowest native
  cuttable part of a compatible trunk before cutting, so useful wood is kept.

## Persistence and recovery
- preserve objective, exact equipment, attachments, cargo, destination and progress through reasonable interruption;
- never silently substitute another rig/objective;
- recover locally before abandoning a physically possible task.

# 5. Physical measurement is a first-class Farmhand primitive

Metadata is bootstrap information, not physical truth.

Measurement priority:
1. live physical/collision geometry and engine transforms;
2. live joints, wheel positions and active wheel groups;
3. observed motion/calibration;
4. loaded I3D geometry where useful;
5. XML/store/AI dimensions only as fallback/bootstrap.

Farmhand must be able to determine enough real geometry for:
- each powered vehicle;
- each attached trailer/implement;
- belted/loaded vehicle cargo;
- implements attached to cargo;
- recursively attached children;
- hitch/joint positions;
- effective axle/wheel-group references;
- current headings and articulation;
- clearance and swept motion.

Traversal must cover the complete attachment/cargo graph with deduplication/cycle protection.

Do not hardcode dimensions for one named mod or rig.

# 6. Local geometry rule: no oversized whole-rig veto

The current live evidence has proven that a single aggregate long rectangle can reject usable articulated space.

Therefore:

**An oversized aggregate whole-rig box must not be authoritative for local maneuver acceptance.**

A coarse aggregate envelope may exist only as a cheap broad-phase/global warning if it provides measurable value.

Authoritative local clearance must use the smallest sufficiently accurate physical representation of the actual rig.

The minimum candidate representation is:
- separate unit footprints/physical shapes;
- live unit transforms;
- articulated predicted poses;
- complete recursive rig membership;
- scenery included;
- no empty-space rectangle spanning an angled rig.

Do not replace one oversized rectangle with another guessed rectangle.

# 7. Short-horizon articulated prediction

Farmhand must predict how the rig will move, not treat an articulated rig as one rigid car.

Minimum state for an articulated segment should be derived from evidence and may include:
- powered vehicle pose and heading;
- steering command / curvature;
- travel direction;
- hitch/joint transform;
- trailer pose and heading;
- effective hitch-to-wheel-group distance;
- downstream attached/cargo-unit transforms.

The powered vehicle advances from its own observed pose.

Trailer heading may guide steering, but it must never replace the powered vehicle's pose/heading for next-state prediction.

Predict only as far ahead as needed to make the next safe motion decision.

Validation:
`predict short segment → execute under normal physics → observe → compare → correct`

Prediction quality is judged by actual pose/heading/clearance error, not by metadata agreement.

# 8. Historical navigation proof methodology (only for relevant diagnostic cases)

Do not assume every item below is needed. Prove each from observed failures.

The smallest high-value candidate stack is:

1. **Measured world/rig geometry**
   - actual units, transforms, joints, wheel groups and local obstacle queries.

2. **Short-horizon articulated motion predictor**
   - forward/reverse next-state prediction for the measured rig.

3. **Local candidate clearance**
   - precise enough to decide the next few metres without false whole-rig empty-space rejection.

4. **Cheap global corridor**
   - low-cost destination-directed route skeleton;
   - no full-map high-resolution search;
   - start movement quickly.

5. **Closed-loop motion supervisor**
   - compare commanded vs actual movement;
   - observe speed, pose delta, heading/articulation and contact;
   - detect true no-progress/contact promptly.

6. **Local recovery/replan**
   - smallest safe maneuver that preserves the original objective;
   - no endless repeated unchanged search;
   - return to destination-directed planning once clear.

7. **Task/world coordinator**
   - sequences navigation with hitch/load/fieldwork/economy/etc.;
   - verifies actual outcome.

A component earns a place in the clean build only when live evidence proves its need and its minimum implementation.

### Cross-product foundations to prove once, then reuse

Do not build separate infrastructure for every feature. The final product should reuse a small shared substrate:

1. **Authoritative world/object graph**
   - stable identity, ownership, location/state, recursive attachments/cargo, fields/places/products and current task relationships.
   - prove by resolving and preserving the same real objects across at least navigation, equipment handling and one non-driving task.

2. **Physical geometry / interaction model**
   - measured bodies, joints, work/attachment points, tool envelopes and predicted short motion.
   - prove that the same representation supports driving clearance plus at least hitch/load positioning; extend only when another machine type demonstrates a missing state.

3. **Verified action contract**
   - every primitive has observable preconditions, bounded execution, postconditions and truthful failure.
   - prove representative native actions: drive, hitch/detach/configure, one field-machine action, and one economy/logistics action without turning acknowledgements into success.

4. **Goal executor**
   - decompose a natural objective into reusable primitives; skip already-satisfied steps; preserve exact equipment/objective; STOP, resume, recover and change destination from fresh state.
   - prove with one multi-step transport job and one field/logistics job.

5. **Human interface**
   - natural text reaches the same goal executor; optional model/voice never owns physical authority; status explains observed state.
   - prove command, STATUS, STOP and resume without developer-only inputs.

Heavy equipment, forestry, construction and additional farming workflows should reuse these foundations. GIANTS supplies physical functions and machine specializations but does not supply complete worker workflows for all such tasks: use bounded lab experiments (e.g., harvester-head reach, moving-tool response, terrain effects) to expose missing general controls. Stop when the transferable principle is proven; do not require a finished machine-specific workflow before advancing. Add a new foundation only when a live task proves the existing substrate cannot represent or control the required state.

# 9. Navigation performance requirements

Farmhand should begin useful movement quickly.

Long-distance navigation must not require a high-resolution scan of the entire map before moving.

Preferred behavior:

`destination → cheap coarse corridor → begin movement → continuously validate near future → precise local planning only where needed → arrive`

The planner must preserve destination progress. A locally clear corridor is not success if it keeps extending away from the goal without a justified route back.

For tight spaces, spend compute locally and temporarily. Discard high-resolution local geometry when it is no longer useful unless a compact verified approach/corridor memory has clear value.

# 10. Collision and no-progress semantics

Keep these states distinct:

- **predicted obstruction**: a future candidate is unsafe;
- **confirmed standing contact**: current physical state is touching/blocked;
- **no progress**: commanded motion is not producing expected movement;
- **arrival tolerance**: close enough to the planned endpoint that the segment is complete.

A predicted obstruction must never be mislabeled as physical contact.

Normal navigation must not depend on disabling scenery collision.

**Scenery ghosting is historical loading/unloading recovery, not normal driving behavior.** It was introduced to work around building-adjacent loading/unloading obstructions in an earlier implementation. Reassess the specific failure using the now-measured physical geometry and proper loading/approach controls before retaining it. A false planner-box collision requires a geometry fix; genuine physical contact requires a safe normal-physics maneuver. Do not keep ghosting merely to avoid a simpler correct fix.

If real-game evidence still demonstrates an otherwise unsolvable building-adjacent loading/unloading or engine-compatibility case, ghosting may be retained as a narrowly scoped, **user-selectable optional setting, default OFF**. Clearly disclose when it is used; never apply it automatically as a requirement of ordinary Farmhand gameplay. Prove the exact trigger and successful recovery, and restore **every** affected collision pair on success, failure, STOP, cancellation, timeout, takeover, link loss and exception. A ghosted run is not proof of ordinary physical navigation. Remove the option entirely if no qualifying need remains.

# 11. Live proof protocol

Use normal FS25 physics.

For navigation qualification:
- no AutoDrive ownership;
- no Courseplay ownership;
- no GIANTS GOTO ownership;
- no teleport;
- no scenery ghosting;
- no hidden provider fallback.

GIANTS engine sensors, transforms, collision/terrain queries and vehicle controls are allowed.

Only one live movement request may be active at a time.

Before another request:
- prior request reached COMPLETE / FAILED / STOPPED;
- vehicle/rig is observed idle;
- no queued stale movement request can start later.

Background/minimized GIANTS screenshots are valid scene evidence after the verified scene comparison; do not steal user focus except when an actual menu interaction requires it.

# 12. Proof classifications

Every result must be labeled honestly:

- **PROVEN** — live evidence demonstrates the claim under the required conditions;
- **PARTIALLY PROVEN** — some required conditions/cases remain;
- **FAILED** — tested implementation did not meet the criterion;
- **NOT REQUIRED** — evidence shows the system adds no necessary capability;
- **UNVERIFIED** — plausible or test-covered but not live-proven.

Unit tests prove code behavior, not FS25 physics.

A movement performed with ghosted scenery does not prove ordinary physical navigation.

One rig/map/destination never proves universality.

# 13. Historical navigation qualification ladder (not a product release gate)

Prove in this order unless evidence requires a different dependency:

### A. Geometry completeness
- all rig units discovered, including nested attachments/cargo;
- correct live transforms;
- no duplicate/missing unit.

### B. Physical measurement
- usable unit dimensions/shapes;
- hitch and wheel-group geometry measured from live data;
- fallback behavior explicitly identified.

### C. Prediction
- straight, mild turn, tighter turn;
- forward and reverse where practical;
- predicted vs observed position/heading error recorded.

### D. Known false-rejection cases
Use the existing 2 m right-turn and short reverse-left cases:
- old aggregate result;
- new local geometry result;
- actual physical execution.

### E. Local maneuver
- constrained departure/turn under normal collision;
- no false contact classification;
- no ghosting.

### F. Global-to-local handoff
- coarse corridor reaches a physically realizable local transition;
- local planner returns control to goal-directed travel;
- no corridor wandering.

### G. Motion supervision/recovery
- real obstruction/no-progress distinguished from stopping tolerance;
- rapid detection;
- bounded recovery;
- objective preserved.

### H. Generalization
- second materially different rig;
- second destination/layout;
- at least one modded and one different geometry family where available.

### I. Scale/performance
- long trip / large-map case;
- first useful movement latency;
- planning/update cost;
- replan count;
- no frame/heartbeat starvation.

Use these historical checks selectively for actual observed navigation failures; they do not delay implementation or release qualification of other product capabilities.

# 14. Player-value optimization

Optimize for:
1. accomplish the player's objective;
2. physical correctness;
3. responsiveness;
4. reliability/recovery;
5. shortest sensible safe work;
6. generality across maps/mod equipment;
7. minimum complexity and maintenance;
8. low compute/runtime overhead;
9. maintainability/testability.

Do not optimize for keeping legacy code alive.

Do not make the player manually perform a step Farmhand can physically perform.

## Player-facing settings contract

Provide a **useful, well-organized, low-cost settings menu**, expanding the existing Farmhand settings mechanism rather than creating a competing configuration system. Expose meaningful supported behaviors as inexpensive ON/OFF or short enumerated choices when practical: autonomy and queueing, appropriate navigation/vehicle preferences, recovery options, equipment handling, purchasing approvals/spending limits, notifications/voice, and developer diagnostics. Not every possible internal parameter warrants a setting.

Settings must have truthful labels, clear defaults, validated values, and predictable persistence; show only options that are implemented and actually work. Prefer shared configuration and one simple UI binding per feature; avoid duplicate switches or performance-heavy settings polling. A setting should not force users to choose between functioning and broken behavior or serve as a workaround for defective engineering.

Keep physical safety, STOP reliability, collision restoration, ownership checks, and truthful outcome verification mandatory and not disableable. Optional risky recovery mechanisms such as retained scenery ghosting should default OFF, have precise scope, and never activate silently. Respect existing user-approved preferences unless changing them is necessary and explicitly authorized.

## End-to-end time is a product requirement

Measure **request → first useful action/response** (first meaningful physical progress for work tasks) and **request → verified objective complete**. Both must be as short as practical without weakening physical safety, correctness, STOP, or verification.

Minimize idle planning, unnecessary setup, redundant observation, distant equipment selection, detours, retries, reloads, repeated tool/model calls, and work that was already satisfied. Choose the nearest *suitable* equipment by total job cost, not proximity to the player. Use cheap coarse planning to begin safely; refine only the near future and only where needed. Avoid frame stalls and excessive computation.

For relevant changes, record before/after latency, actual travelled/work distance versus a reasonable direct baseline, replan count, and task outcome. A faster local step that makes the complete job slower, less reliable, or less capable is not an improvement.

## Smallest correct reusable piece

Inspect existing Farmhand primitives and exact GIANTS native mechanics first, then the smallest proven algorithms, patches, bridges or adapters available from references. **Choose the simplest correct and fastest end-to-end mechanism regardless of its origin**; do not prefer legacy Farmhand code over a smaller native fix, or native API calls over a proven small shared controller when they are inadequate. Build a new minimal general controller only when the available pieces cannot meet the measured contract. Reuse each piece across as many compatible workflows, machines and maps as its actual behavior allows.

Optimize for **fewest necessary independent mechanisms and maximum reusable capability**, not maximum components, minimum lines of code, or elegance of a particular planner. One justified shared correction is better than many task-specific exceptions.

# 15. Development method

Use:

`DIAGNOSE → IMPLEMENT → QUALIFY`

## Mandatory three-scale review — before, during, and after each experiment

1. **Small picture / immediate:** What did FS25 physically do? What is the earliest incorrect observation, assumption, state, control or decision? What is the smallest live discriminator and correct fix?
2. **Dependency picture / upstream:** Is the apparent task failure caused by a broken shared prerequisite (geometry, collision classification, world identity, actuator reach, control, verification, routing)? What other tasks use that prerequisite? Fix the prerequisite *before* continuing dependent work.
3. **Big picture / whole Farmhand:** Does the solution advance the end-to-end autonomous objective, reuse the smallest proven native/Farmhand piece, preserve other capabilities, and reduce total task time and complexity? Would it belong in the final clean product?

Apply all three scales when choosing work, after new failure evidence, and before retaining/migrating a component. Keep the actual experiment primary; do not turn this into a broad audit on every iteration.

**Dependency preemption:** If a current feature cannot work correctly because an underlying shared piece is wrong, suspend the downstream fix, repair and physically prove that upstream piece, then resume the original player command and its end-to-end test. Example: inaccurate whole-rig collision boxes must be corrected before treating navigation's false obstructions as routing/recovery failures. This is not scope drift.

**No workaround-first development:** Do not hide a broken cause with extra retries, tolerances, arbitrary boxes, teleport, collision ghosting, provider handoff, timeout tweaks, special-case coordinates, or parallel planners. First inspect available GIANTS behavior, existing Farmhand code and proven source references for the smallest correct patch/controller. A bounded fallback is considered only if evidence shows no viable simpler correct mechanism for the required capability; document its trigger, constraints, measured benefit, remaining limitation and removal condition. Never silently weaken physics, STOP, or verification, and never promote diagnostic shortcuts as production proof.

**Stop rule for exploration:** When a bounded experiment has produced a reusable finding and the remaining machine-specific behavior is not a dependency for the next foundation, record the unresolved case accurately and move on. Do not confuse an isolated proof with an integrated player workflow.

Record only findings that change the causal dependency graph, capability, shared contract, proof status, task time, or clean-build design. Resume the proving loop without repetitive historical summaries.

## DIAGNOSE
Before changing behavior:
- reproduce the live failure;
- identify the **first wrong decision**;
- measure the relevant live state;
- identify the bad assumption;
- create the smallest regression test possible.

During diagnosis, production behavior changes are limited to necessary instrumentation.

## IMPLEMENT
- change the smallest general primitive that fixes the proven bad assumption;
- do not stack workarounds when the hypothesis fails;
- return to diagnosis instead.

## QUALIFY
- rerun the exact original case;
- run an unrelated/generalization case when relevant;
- verify provider-disabled/normal-physics conditions when relevant;
- run focused tests;
- then run broader gates only when the behavior has earned them.

Do not perform broad audits while the current causal chain is unresolved.

Do not let passing tests validate an assumption that has not been checked against FS25 reality.

# 16. FS25 reference authority

Before relying on undocumented FS25 behavior, inspect the shared toolkit:

- Windows: `C:\Users\Matt\Desktop\FS25 Modding Toolkit`
- WSL: `/mnt/c/Users/Matt/Desktop/FS25 Modding Toolkit`
- master guidance: `skills/fs25-master-skill/SKILL.md`

Reference priority:
1. exact installed FS25 dataS;
2. Community LUADOC;
3. UsedPlus FS25 AI Coding Reference;
4. applicable toolkit skills/guides;
5. community dataS;
6. provider/open-source implementation;
7. web when local references are insufficient.

Courseplay/AutoDrive/etc. may be studied for algorithms and geometry, but copy only the minimum useful mechanism and do not import competing coordinators/job managers.

# 17. Product invariants

- Any normal map; no map-specific product coordinates.
- Exact named equipment means that equipment.
- Preserve an existing assembled rig unless the user asks to change it.
- Provider failure is not destination failure.
- Command acceptance is not completion.
- Actual world outcome must be verified.
- STOP must terminate Farmhand-owned motion and invalidate stale queued movement.
- No silent teleport.
- No silent physics weakening.
- No hidden external-mod dependency for advertised core capability.
- No release claim from mocks/tests alone.
- No destructive reset/stash/clean of the dirty working tree unless explicitly directed.

# 18. Final definition

Farmhand is complete when it behaves like a reliable additional worker and companion:

**The player tells Farmhand what needs done. Farmhand observes the real FS25 world, deploys and coordinates multiple concurrent workers when useful, physically performs complete farming and specialist objectives with the smallest correct plan, verifies the results, and recovers quickly when reality differs from prediction. Full release scope is in FINAL_FARMHAND_PRODUCT_SPEC.md.**
