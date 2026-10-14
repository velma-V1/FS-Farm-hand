# Authoritative agent rules

Applies to the entire repository, to Codex, Claude, and every other coding agent.
Read this file and BUILD_SPEC.md before editing. The user's current instructions
have precedence; report conflicts instead of silently changing the scope.

## Mission and boundaries
- Build the smallest FS25 autonomous farmhand. Current task ONLY: find an available
  compatible tractor and forage wagon, attach them, and deliver to the requester.
- LLMs interpret language and choose high-level intent. They never supply Lua,
  arbitrary commands, vehicle IDs to bypass selection, or physical control loops.
- Deterministic executor: FIND → RESERVE → MOVE → ATTACH → VERIFY → DELIVER.
  Completion requires observed attachment, arrival, and stopped equipment.
- FS25 and existing mods perform physical work. No navigation, pathfinding,
  driving, Courseplay, or AutoDrive replacements. No voice, generalized agent
  platform, autonomous purchasing, fieldwork, or unrelated features.
- Keep Python orchestration in companion/, pure contracts in protocol/ and
  adapters/, game-side Lua in mod/, synthetic state in simulator/, tests in tests/.
  Never import simulator into companion or mod. No simulated success in real adapters.

## Safe collaboration
- Work on a dedicated branch. Inspect git status before edits; preserve others'
  changes. Agree file ownership before concurrent edits; do not share a mutable
  checkout between writers. No force pushes, merges, or main-branch product edits.
- Change shared schemas and all consumers together. Version incompatible contracts.
- Do not claim success from command acknowledgement alone. Fail closed on invalid
  intent, missing capability, ownership conflict, stale/busy equipment, or uncertain
  observations. Never take occupied equipment or another farm's assets.
- Reservations must be atomic for both units. Stop before releasing ownership after
  physical work; retain reservations if stopping cannot be confirmed.
- Do not add retries, persistence, network transports, dependencies, or scaffolding
  without a demonstrated requirement. No secrets or paid model calls in tests.

## Evidence and reuse
- Development and CI run entirely in cloud Linux environments; require no software,
  game files, models, mods, or services on the user's PC during development.
- Before adding an external adapter, inspect the exact upstream FS25 version,
  license, API, capabilities, and failure semantics. Record the evidence and version.
  Prefer a thin adapter; no vendoring/forking absent license review and demonstrated need.
- Never invent GIANTS or mod API calls. Unsupported remains unsupported.
- Keep mock contracts explicitly distinct from upstream APIs and physics.
- Add behavior tests before implementation. Vary IDs, inventory order, eligibility,
  compatibility, and failures. Do not weaken tests to hide a failure.
- Run `python scripts/check.py` before proposing a change. Inspect `git diff --check`
  and the final tree. Report commands/results, implemented vs mocked, and limitations.
- Passing cloud tests never proves mod loading, game physics, attach alignment,
  route reachability, networking, multiplayer, or actual FS25 integration.
