# Authoritative agent rules

Applies to the entire repository, to Codex, Claude, and every other coding agent.
Read this file and BUILD_SPEC.md before editing. The user's current instructions
have precedence; report conflicts instead of silently changing the scope.

## Mission and boundaries
- Product scope (user-authorized 2026-10-18, replacing the earlier "forage wagon
  only" task): a local autonomous farmhand/dispatcher. From a plain-language goal it
  determines the required capabilities, equipment and materials, finds or buys
  them through the game's own mechanisms, assembles, loads, travels, works, verifies
  from game state, recovers within bounds and finishes. Grow it capability by
  capability; each one qualified in the real game (tests/ACCEPTANCE.md matrix).
- The production farmhand is LOCAL-FIRST. It runs on the user's Windows PC alongside
  Farming Simulator 25. GitHub, hosted CI, and remote coding environments are
  development tools only and must never become runtime dependencies.
- Intended runtime: FS25 + game-side Lua mod + local Windows companion service +
  local IPC. The planner/LLM, speech recognition, and TTS are local components unless
  the user explicitly chooses otherwise later.
- LLMs interpret language and choose high-level intent. They never supply Lua,
  arbitrary commands, vehicle IDs to bypass selection, or physical control loops.
- Deterministic executor: FIND → RESERVE → MOVE → ATTACH → VERIFY → DELIVER.
  Completion requires observed attachment, arrival, and stopped equipment.
- FS25 and existing mods perform physical work: GIANTS' own jobs (GOTO, field
  work helpers), AutoDrive, and other installed mods where evidenced. No navigation,
  pathfinding, driving, field-work, Courseplay, or AutoDrive replacements, and no
  store-UI automation where a game API exists. Purchasing and field work are in
  scope only through the game's own APIs, only for the requested job, and only
  after the requirement, the item's fit and the farm's funds are checked.
- User-authorized exceptions, both in mod/FarmhandHitch.lua, both steered by GIANTS'
  AIVehicleUtil.driveToPoint and both hard-capped; never extend them into navigation:
  1. HITCH's ALIGN_REVERSE, a bounded reverse of the last few metres that stops as
     soon as GIANTS reports the exact planned joints attachable.
  2. HITCH's LOCAL_MANEUVER (authorized 2026-10-18 after GIANTS' GOTO ended
     NOT_REACHABLE 12-19 m off twice at 8ffafcc): forward only, inside a 25 m work
     radius of the wagon's joint, from the observed joints onto the wagon's
     centreline so ALIGN_REVERSE can take over. One closed-form curve, no search,
     no retry; GOTO stays the provider for anything farther.
- Keep Python orchestration in companion/, pure contracts in protocol/ and
  adapters/, game-side Lua in mod/, synthetic state in simulator/, tests in tests/.
  Never import simulator into companion or mod. No simulated success in real adapters.

## Safe collaboration
- Work on a dedicated branch. Inspect git status before edits; preserve others'
  changes. Agree file ownership before concurrent edits; do not share a mutable
  checkout between writers. No force pushes, merges, or main-branch product edits
  unless the user explicitly requests them.
- Change shared schemas and all consumers together. Version incompatible contracts.
- Do not claim success from command acknowledgement alone. Fail closed on invalid
  intent, missing capability, ownership conflict, stale/busy equipment, or uncertain
  observations. Never take occupied equipment or another farm's assets.
- Reservations must be atomic for both units. Stop before releasing ownership after
  physical work; retain reservations if stopping cannot be confirmed.
- Do not add retries, persistence, network transports, dependencies, or scaffolding
  without a demonstrated requirement. No secrets or paid model calls in tests.

## Development workflow and runtime boundary
- Keep unfinished project work off the user's PC until a genuine live FS25 test is
  required. GitHub and remote CI may be used for source control, development,
  review, static checks, mock tests, and release packaging before that point.
- This remote development workflow does NOT change the local runtime architecture.
- The finished farmhand must be able to run locally beside FS25 without GitHub or a
  hosted service being required for ordinary operation.
- Development-only live tests (user-authorized 2026-10-18): the host runner
  `scripts/fs25_host_runner.py`, started by the user outside the agent's sandbox,
  performs only its fixed allowlist (status, install the checkout's build, launch
  FS25 by its package's own gamelaunchhelper.exe or else its exe — fixed paths —,
  restart or stop FS25 — that image only —, the production hitch_pair, the player's
  own typed words through the production rules (letters, digits, spaces, light
  punctuation; 160 at most), STOP, collect). Never widen it into a shell, file or
  program interface, and never ship it or `--live-test` as release.
- Live FS25 behavior, Windows service behavior, local IPC, local model execution,
  speech, and game/mod integration must eventually be verified on the user's PC.

## Evidence and reuse
- Before adding an external adapter, inspect the exact upstream FS25 version,
  license, API, capabilities, and failure semantics. Record the evidence and version.
  Prefer a thin adapter; no vendoring/forking absent license review and demonstrated need.
- Never invent GIANTS or mod API calls. Unsupported remains unsupported.
- Keep mock contracts explicitly distinct from upstream APIs and physics.
- Add behavior tests before implementation. Vary IDs, inventory order, eligibility,
  compatibility, and failures. Do not weaken tests to hide a failure.
- Run `python scripts/check.py` before proposing a change. Inspect `git diff --check`
  and the final tree. Report commands/results, implemented vs mocked, and limitations.
- Passing CI tests never proves mod loading, game physics, attach alignment, route
  reachability, local IPC, Windows service behavior, multiplayer, or actual FS25 integration.
