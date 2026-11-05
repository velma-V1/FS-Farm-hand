# FS-Farm-hand

Farmhand is a local-first autonomous Farming Simulator 25 worker and AI companion.

The player gives a normal typed or spoken objective. Farmhand observes the live game state, resolves the exact equipment/world objects involved, plans the work, acts through FS25, verifies the physical result, and recovers/replans when reality differs from prediction.

Core product loop:

`USER GOAL → UNDERSTAND → OBSERVE → RESOLVE → PLAN → ACT → VERIFY → RECOVER/REPLAN → COMPLETE`

## Current engineering phase

Active production development of the complete autonomous FS25 product. The authoritative target is [FINAL_FARMHAND_PRODUCT_SPEC.md](FINAL_FARMHAND_PRODUCT_SPEC.md), read with [AGENTS.md](AGENTS.md). Farmhand must be able to deploy multiple workers at once; moving combine unloading while harvesting is a release gate. Scope includes world/field/contracts observation, every supported equipment category, full farming/forestry/construction/earthmoving objectives, animals, production, logistics, voice/characters, high accuracy and fast planning/execution/recovery. These are TARGET REQUIREMENTS, not claims of current implementation.

Navigation experiments are tools for correcting real gaps, not the product mission. Historical BUILD_SPEC.md does not narrow final scope. Claude engineering guidance: [CLAUDE.md](CLAUDE.md).

## Standalone definition

Farmhand owns the player objective and core worker behavior.

GIANTS FS25 remains the engine and supplies the real world, physics, transforms, collisions, terrain, wheels, joints, attachments, vehicle controls, fields, economy, store and machine systems.

AutoDrive does not participate in Farmhand execution, even as fallback, and independently installed settings remain untouched. Other gameplay mods may be researched or supported by specific verified capability adapters where needed, but the core cannot require them.

## End product

Farmhand is intended to complete as many actual supported FS25 objectives as possible with **multiple independently controlled workers running simultaneously**, efficiently coordinated. User requirements are authoritative in [FINAL_FARMHAND_PRODUCT_SPEC.md](FINAL_FARMHAND_PRODUCT_SPEC.md), including:

- live field, weather, crop, contract, animal, production, fleet and world awareness;
- fast, resilient navigation, hitching, equipment selection, loading, hauling, unloading and precise real-world operation;
- individual field operations and autonomous full farm processes, including needed stone removal, rolling, fertilizer, weed control and harvest;
- **moving combine unloading while harvesting**, a required live-proven release gate;
- forestry cutting, selectable log lengths, practical stacking, transport and stump outcome selection;
- mining/quarries, earthmoving, excavators, dozers, cranes, forklifts and sensible construction/placement where available mechanics permit;
- multi-machine logistics, contracts with rented/leased equipment, buying/selling/storage, animals and production;
- interpreting badly structured commands, goal-driven autonomy modes, optional local AI character roles, voices, settings and real game verification.

The player should not need internal IDs, provider knowledge, route-network setup, console commands, or routine intervention. No claims of implementation or release qualification are implied by this intended scope.

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
