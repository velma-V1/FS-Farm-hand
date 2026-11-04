# Codex lab → Claude production: integration guide (2026-11-04)

This repository already contains Codex's implementation and **most** discoveries as a code-and-evidence snapshot from the lab. The cloud producer is **not** starting from scratch. This guide is a discovery index, not a claim that all experiments are production-qualified.

## Reuse the existing components before writing replacements

| Area | Existing source / proof | Qualification |
| --- | --- | --- |
| Live FS25 engine observation and bridge | `mod/FarmhandObserve.lua`, `mod/FarmhandBridge.lua`, `companion/bridge.py`, `protocol/` | Actual installed local lab, but cloud changes still require separate deployment to claim live proof |
| Native Farmhand route generation, preview and controller | `mod/FarmhandDrive.lua`, `tests/test_lua_drive.py`, `adapters/EVIDENCE.md` | Multiple actual FS25 journeys succeeded; specific barn failure still not isolated |
| Bounded moving-tool actuator | `collected/proven_systems/bounded_moving_tool.lua` and `.PROOF.md` | Proven short pulses on three live families; no claims about completing ground work |
| Measured steering-reference short-motion candidate | `collected/proven_systems/measured_short_motion.lua` and `.PROOF.md` | **PARTIALLY PROVEN**; do not silently replace production planner |
| Optional TerraFarm direction adapter | `collected/proven_systems/terrafarm_direction_adapter.lua` and `.PROOF.md` | Two tested tool types; not a requirement for standalone Farmhand |
| Route memory / performance instrumentation | `companion/route_memory.py`, `scripts/task_performance.py` and their tests | Source included; verify semantics and freshness before relying on metrics |
| No-focus hot Lua reload | `mod/FarmhandBridge.lua` `.fhReload` marker and `mod/FarmhandProbe.lua`; `CLOUD_LAB_ACCESS.md` | Tested on live Save 1, no keyboard/mouse or game restart; reloads **installed lab code only** |
| Fieldwork helpers and attached rigs | `companion/executor.py`, `companion/coordinator.py`, `tests/test_fieldwork.py` | At least one live bounded area changed field coverage; see below for unresolved worker entry |

## Final Codex delta incorporated by this integration branch

The original production snapshot ended with 2026-11-04 changes in five files not yet imported. This branch carries only those five code/evidence deltas, with focused tests, including:

- Preview regressions for short reverse arcs.
- Removal of preemptive teleport of unrelated equipment before an actual blockage.
- Preserving an already assembled rig when finishing **bounded** fieldwork.
- Live-navigation and fieldwork evidence in `adapters/EVIDENCE.md`.

**Unresolved:** A pre-stage attempt to skip GIANTS' idle helper wait drove to a near-field pose that was unsuitable; the change was reverted. Do **not** reintroduce it without proving a usable field-entry/worker-start condition. Do **not** represent the failed retry as completion.

## Integration discipline

1. Reuse qualified primitives, tests, and observations before duplicating their engineering work.
2. Treat every `PROOF.md` literally; distinguish code-level tests, partially proved behavior, and real final physical outcomes.
3. Resolve overlaps against Claude's current production edits rather than overwriting them. This is a reviewable PR, **not** an instruction to reset or merge blindly.
4. Cloud remote tests currently operate only on the separate local **Codex lab** Save 1; their success does not verify unpublished cloud production code.
5. Preserve user-observer operation, no focus theft, no silent teleport, and opt-in-only ghosting.
