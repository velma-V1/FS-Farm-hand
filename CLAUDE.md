## Claude Cloud ↔ FS25 lab reference (2026-11-04)

The production branch has a separate private MCP service `farmhand-live-lab`; see `CLOUD_LAB_ACCESS.md` for the current connection and verified non-focus-stealing hot-reload workflow. An MCP entry existing in `.mcp.json` does NOT prove this particular cloud session is authenticated. Verify with `toolkit_list("notes")` and `live_status()` before citing live/toolkit access. No arbitrary host shell, GUI input, game restarts, or cloud-code deployment is available through the gateway. Source access is for private research; do not publish GIANTS/provider code. Lab test successes are not proof that cloud changes ran in FS25. Preserve Codex's separate laboratory and the player's foreground.

## Current production directive — 2026-11-04

**Production is underway on this cloud branch.** Build the actual, installable, fully integrated standalone Farmhand, not an indefinite proof-of-concept or an improved lab. AGENTS.md's production directive supersedes its historical lab-only section here. Use the laboratory's measured code, results and tests as inputs, and return to targeted research only to resolve a concrete blocker. Own the end-to-end player outcome. Exercise full engineering discretion about implementation, architecture, work ordering and parallelization; optimize for the simplest robust product. Commit coherent validated production improvements. Never claim an unperformed local-game live test.

# CLAUDE.md

Claude Code works under `AGENTS.md`. Read it first. If this file conflicts with `AGENTS.md`, `AGENTS.md` wins.

`BUILD_SPEC.md` is historical foundation documentation. Do not let its AC-001 scope or provider-era non-goals override the current standalone Farmhand mission.

There is no handoff file. Do not create or depend on one.

## Current engineering objective

Use the current repository as a laboratory to identify and live-prove the **smallest reusable systems needed for the final standalone Farmhand product**.

Do not make "repair the legacy planner" the goal.

Do not make one command such as GMC → silo the goal. Specific commands are fixtures used to prove or disprove primitives.

For every change:
1. identify the first wrong runtime decision;
2. identify the smallest underlying primitive responsible;
3. change only that primitive;
4. prove it live under normal physics;
5. preserve the evidence;
6. move on when the primitive is established.

Apply the mandatory **small / dependency / whole-product review** in `AGENTS.md` before selecting work, whenever live evidence reveals a new cause, and before extracting a permanent component. Preempt a downstream feature to correct its broken shared prerequisite, then resume the original end-to-end task. Never build around a false observation, collision box or ineffective actuator while a correct simple shared fix exists.

Workarounds are not substitutes for fixing causes. Reuse existing Farmhand primitives, GIANTS mechanics and the smallest proven adapters before adding a controller or subsystem. Measure request-to-first-action and request-to-verified-completion time; minimize detours, waits, retries, and background cost without bypassing safety or verification.

## Navigation reference — verified regressions and remaining dependencies

Accurate local physical geometry for articulated rigs remains a critical reusable foundation. Choose the next experiment from live dependency evidence rather than automatically repeating this older navigation case.

Live evidence already established:
- the aggregate whole-rig rectangle can reject usable space;
- a 2 m right-turn candidate was blocked by the aggregate box while separate observed-unit boxes were clear;
- a short reverse-left candidate showed the same disagreement;
- the aggregate box spans empty space beside an angled trailer;
- the footprint previously omitted an implement attached to belted vehicle cargo; the generic traversal fix now covers the nested plow in focused tests;
- powered-vehicle next-state prediction must advance from the truck's observed heading, not substitute the trailer heading;
- Deckover hitch-to-active-wheel-group geometry was measured at 9.111 m for the current live configuration;
- that number is a rig measurement, not a universal constant;
- separate rigid unit boxes are not yet a proven future-motion model;
- background/minimized GIANTS screenshots have been verified to show the same scene closely enough for development evidence.

The oversized whole-rig rectangle was a **bad upstream clearance decision**, not evidence that routing required more recovery branches. Correct measured swept geometry before tuning route recovery.

## Existing physical proof — retain, do not repeat without regression cause

- The loaded GMC four-unit rig passed the 2 m right turn and short reverse-left under normal physics after articulated clearance replaced aggregate-box vetoes.
- A materially different two-unit rig passed related forward/reverse movement, but tighter-space generality was only PARTIALLY PROVEN.
- Subsequent measured steering-reference work improved a GMC forward-turn endpoint estimate; see current `adapters/EVIDENCE.md` for exact numbers and build evidence.
- These are **short-motion proofs**, not proof of complete destination-directed navigation, field operation, or universal collision clearance.

Next navigation work, when its dependencies make it the highest-value choice: establish representative full-route global/local transitions, accurate geometry for new rigs, measured motion supervision, bounded recovery, and request-to-completion responsiveness. Use the qualification ladder in `AGENTS.md`, not a stale fixed candidate list.

For any new predicted-clear maneuver, record observed rig geometry, actual collision/physics state, predicted-versus-actual poses, and physical outcome. No scenery ghosting, AutoDrive/Courseplay/GIANTS GOTO ownership, teleport, or hidden fallback is accepted as standalone-navigation qualification. GIANTS sensors, controls, joints and physics remain valid building blocks.

If a candidate cannot be executed safely, diagnose its first incorrect physical assumption. Do not loosen tolerances or add retries to manufacture a pass.

## What earns a place in the clean build

Candidate systems must be independently justified by live evidence.

The current candidate set is:
1. measured world/rig geometry;
2. short-horizon articulated predictor;
3. precise local candidate clearance;
4. cheap global destination corridor;
5. closed-loop motion supervisor;
6. bounded local recovery/replan;
7. task/world coordinator.

Do not assume all seven are required.

Navigation remains a major release blocker, but new evidence may justify proving a lower-level shared component through forestry, earthmoving, loading or another domain first. The final clean product should reuse the same small substrate across domains. Map proven work into five cross-product foundations; do not wait for every navigation scenario to finish before identifying a generalizable foundation:
- authoritative world/object graph;
- physical geometry/interaction model;
- verified bounded action contract;
- goal executor with STOP/resume/recovery;
- natural-language/status interface with no physical authority in the model.

Fieldwork, commerce, loading, forestry and heavy equipment should consume those foundations rather than create parallel planners/state systems. Add a new foundation only after a real end-product task proves the existing substrate insufficient.

For each system record:
- observed failure requiring it;
- smallest implementation;
- proof case;
- measured benefit;
- remaining limitations;
- classification: PROVEN / PARTIALLY PROVEN / FAILED / NOT REQUIRED / UNVERIFIED.

## Physical geometry rules

Do not use one long whole-rig rectangle as authoritative local clearance.

Do not replace it with another guessed rectangle.

Prefer:
1. actual loaded collision/physical shapes and live transforms;
2. live joint/wheel/active-wheel-group geometry;
3. observed motion calibration;
4. I3D geometry;
5. XML/store/AI dimensions only as fallback/bootstrap.

Traverse the complete attachment/cargo graph recursively and guard against duplicates/cycles.

A broad aggregate envelope may remain only if it demonstrably saves work as a coarse broad-phase check. It cannot veto a locally clear maneuver.

## Prediction rules

Treat articulated units separately.

Advance the powered vehicle from its own observed pose.

Predict trailer state from its own heading, hitch geometry, effective wheel-group location and short commanded motion.

Use trailer heading as steering/control feedback, not as a replacement vehicle heading.

Prefer short-horizon:
`predict → execute → observe → compare → correct`

Do not build a large vehicle-dynamics model unless live error proves the smaller kinematic predictor insufficient.

## Route architecture rules

Long-distance planning must be cheap enough to begin useful motion quickly.

Preferred shape:
`goal → coarse corridor → start moving → validate near-future → precise local maneuver only where needed → rejoin goal-directed corridor → arrive`

Do not perform full-map high-resolution search before movement.

Do not accept a locally clear corridor as success if it keeps moving away from the destination without a verified connection back.

## Motion supervision

Always distinguish:
- predicted future obstruction;
- confirmed current contact;
- genuine no-progress;
- normal segment stopping tolerance;
- completed arrival.

Compare command with actual pose/speed/heading/articulation changes.

Do not simply shorten timers until the reason for a stop is known.

## Physics policy

Ordinary Farmhand navigation must work with normal scenery collision enabled.

A movement performed with ghosted scenery is not ordinary-navigation proof.

If any temporary physics override is used diagnostically, restore it on every exit path.

## Live-run discipline

Only one movement request may be active.

Before issuing another:
- prior request terminal state confirmed;
- rig observed idle;
- no stale queued request remains.

Never infer movement solely from planner logs. Confirm from live pose deltas.

Do not steal the user's foreground except when a menu operation genuinely requires focus. Use the verified background screenshot path otherwise.

## Development workflow

Use:
`DIAGNOSE → IMPLEMENT → QUALIFY`

During DIAGNOSE:
- reproduce;
- capture the first wrong decision;
- measure live state;
- identify the bad assumption;
- add the smallest regression/instrumentation necessary.

During IMPLEMENT:
- change one general primitive;
- avoid downstream workarounds;
- if hypothesis fails, revert the experimental behavior and return to diagnosis.

During QUALIFY:
- exact original case;
- second/generalization case;
- focused tests;
- full gate only after the live primitive works.

Do not run broad audits or unrelated cleanup while the causal chain is unresolved.

## Commands

Repository:
`/home/xxthatguyxx/projects/FS-Farm-hand`

Python:
`/home/xxthatguyxx/projects/FS-Farm-hand/.venv/bin/python`

Useful checks:
```sh
.venv/bin/python -m unittest tests.test_lua_drive tests.test_lua_hitch tests.test_lua_bridge -q
.venv/bin/python scripts/check.py
git diff --check
git status --short
```

Live interface:
```sh
.venv/bin/python scripts/fs25_live_test.py status
.venv/bin/python scripts/fs25_live_test.py request '<natural player command>'
```

Use the existing install/reload flow. Preserve the running FS25 process when possible.

## FS25 source/reference requirement

Before guessing about engine physics, joints, wheels, transforms, collision, AI behavior or providers, inspect:

`/mnt/c/Users/Matt/Desktop/FS25 Modding Toolkit`

Priority is exact installed dataS first, then LUADOC/reference material, then provider source for algorithm study.

Do not copy an entire provider architecture when one small algorithm or geometric primitive is enough.

## Repository preservation

The worktree is intentionally dirty.

Do not:
- reset;
- stash;
- clean;
- discard unrelated work;
- rewrite unrelated source;
- commit unrelated changes together.

A successful local proof does not validate unrelated dirty changes.

## Durable state

Do not create a chronological handoff file.

Put durable facts in the narrowest correct place:
- engine/provider facts → `adapters/EVIDENCE.md`;
- behavioral acceptance → tests;
- code invariants → code/tests/comments;
- short expensive-to-rediscover product facts → `PRODUCT_WORKING_MEMORY.md`;
- deferred release work → `RELEASE_BACKLOG.md`;
- source history → git.

## Final deliverable of the proving-ground phase

Produce an evidence-backed list:

```text
PROVEN MINIMUM FARMHAND SYSTEMS

1. <primitive>
   Required because:
   Minimum implementation:
   Live proof:
   Generalization:
   Player benefit:

...
```

And:

```text
DO NOT MIGRATE

- legacy mechanisms that add complexity without proven value
- geometry representations proven false
- provider dependencies not required by the final standalone product
- recovery/ghosting/workaround behavior made unnecessary by correct primitives
```

Only after the minimum set is proven should a clean Farmhand navigation implementation be designed.
