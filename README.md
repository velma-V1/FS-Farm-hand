# FS-Farm-hand

Farmhand is a local-first autonomous Farming Simulator 25 worker and AI companion.

The player gives a normal typed or spoken objective. Farmhand observes the live game state, resolves the exact equipment/world objects involved, plans the work, acts through FS25, verifies the physical result, and recovers/replans when reality differs from prediction.

Core product loop:

`USER GOAL → UNDERSTAND → OBSERVE → RESOLVE → PLAN → ACT → VERIFY → RECOVER/REPLAN → COMPLETE`

## Current engineering phase

This repository is currently being used as an **experimental proving ground** for the smallest standalone systems Farmhand needs.

The mission is to prove the **smallest reusable foundations for the complete Farmhand product**, not endlessly repair the legacy architecture. Standalone navigation remains a major unresolved dependency, especially:

- live physical object/rig measurement;
- complete recursive attachment/cargo understanding;
- articulated short-horizon motion prediction;
- accurate local clearance without oversized whole-rig false rejections;
- cheap destination-directed global routing;
- closed-loop motion/no-progress/contact supervision;
- bounded local recovery and replan;
- reliable handoff between local maneuvering and destination travel.

For every experiment, review the **immediate physical cause**, **broken upstream dependencies**, and **whole-product benefit**. Repair shared prerequisites before downstream workarounds; reuse the simplest correct mechanism across workflows. Measure request-to-first-useful-action and request-to-verified-completion time. Experimental machine control (including forestry and heavy equipment where GIANTS has no complete worker workflow) may establish reusable foundations without completing every specialized job.

The authoritative rules, mandatory three-scale review and proof ladder are in [AGENTS.md](AGENTS.md). Agent-specific operating guidance is in [CLAUDE.md](CLAUDE.md).

`BUILD_SPEC.md` is retained as historical foundation documentation and is not current engineering authority where it conflicts with AGENTS.md.

## Standalone definition

Farmhand owns the player objective and core worker behavior.

GIANTS FS25 remains the engine and supplies the real world, physics, transforms, collisions, terrain, wheels, joints, attachments, vehicle controls, fields, economy, store and machine systems.

AutoDrive, Courseplay, FollowMe, Universal Autoload and other gameplay mods may be studied or supported as optional accelerators, but advertised core Farmhand capability must not require them.

## End product

Farmhand is intended to autonomously combine:

- navigation and driving;
- hitching/detaching/configuration;
- loading, securing, hauling and unloading;
- field work;
- buying/leasing/selling and logistics;
- storage, productions and sell-point delivery;
- heavy equipment, construction and forestry workflows where FS25 supports them;
- natural conversation, status and optional voice;
- persistent task state, STOP, recovery and resume.

The player should not need internal IDs, provider knowledge, route-network setup, console commands, or routine developer intervention.

## Development and verification

Automated checks:

```sh
python -m pip install -r requirements-dev.txt
python scripts/check.py
git diff --check
```

Focused navigation checks are run before the full gate when appropriate.

For task latency, the read-only baseline command is:
```sh
.venv/bin/python scripts/task_performance.py --output .fs25-dev/performance/baseline.json
```
It groups recorded live outcomes by task/map/save and measures accepted-to-command dispatch and accepted-to-terminal result. New live qualifications also sample reserved equipment displacement to record first verified equipment motion when actually observed; historical logs cannot supply that metric. Compare like-for-like scenarios on the same save, rig, and conditions before attributing improvements to a change.

Live FS25 evidence is required for claims about physics, movement, collision, path following, attachment behavior, runtime integration or real task completion. Passing unit tests alone is not gameplay proof.

Useful live interface:

```sh
.venv/bin/python scripts/fs25_live_test.py status
.venv/bin/python scripts/fs25_live_test.py request '<natural player command>'
```

## Repository map

| Path | Purpose |
| --- | --- |
| `AGENTS.md` | Authoritative product/engineering contract and minimum proof program |
| `CLAUDE.md` | Claude Code operating guidance for the current proving-ground phase |
| `PRODUCT_WORKING_MEMORY.md` | Concise expensive-to-rediscover supporting facts, not architecture authority |
| `RELEASE_BACKLOG.md` | Deferred release work, not current-task authority |
| `adapters/EVIDENCE.md` | Durable upstream FS25/provider/API evidence |
| `companion/` | Intent, coordination, state, execution and local companion logic |
| `protocol/` | Shared contracts and IPC |
| `mod/` | FS25 Lua integration, observation, driving and physical control |
| `tests/` | Executable behavioral/regression checks |
| `scripts/` | Verification, packaging and live-test tooling |
| `BUILD_SPEC.md` | Historical foundation specification |

GitHub is development infrastructure only. Normal Farmhand operation is intended to remain local beside FS25.
