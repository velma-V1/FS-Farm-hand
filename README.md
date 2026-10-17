# FS-Farm-hand

Local-first foundation for an autonomous Farming Simulator 25 farmhand.

The finished farmhand is intended to run on the user's Windows PC alongside FS25:
a game-side Lua mod communicates through local IPC with a local companion service,
which will host the deterministic executor and later the local LLM, speech
recognition, and TTS components.

GitHub is used only for source control, development, CI, review, and packaging.
It is not part of the farmhand's runtime architecture.

Current vertical slice: select an available compatible tractor and forage wagon,
attach them, and bring them to the requester.

**Status: AC-001 is implemented end to end and tested off-PC only; it has never
run inside FS25.** Upstream capabilities are recorded with source evidence in
[adapters/EVIDENCE.md](adapters/EVIDENCE.md).

How it is used: install once from the CI artifact (copy the `FS25_Farmhand` mod
folder; double-click `FarmhandCompanion/scripts/install_windows.cmd`), then in
game, on foot, press **Left Alt + K** and type what you want ("bring me a tractor
and forage wagon", "what are you doing?", "stop"), or use **Left Alt + J** (bring
the pair here) and **Left Alt + L** (stop). The windowless local companion
interprets the words, selects an available tractor and forage wagon, hitches them
and brings them to you; the game shows its progress as notifications. No
console, vehicle IDs, configuration file, AutoDrive graph or terminal is needed:
GIANTS' own helper (GOTO) drives, and AutoDrive is used when it is installed and
has a marker near you. Optional, chosen at install: a local language model (CPU only) for wording the
built-in rules do not know, and push-to-talk speech (hold **Left Alt + V**; local
speech-to-text, spoken replies). Everything works without them. The
live validation session: [mod/README.md](mod/README.md).

## Pre-game CI checks

In GitHub Actions or another Python 3.12 development checkout:

```sh
python -m pip install -r requirements-dev.txt
python scripts/check.py
```

This runs the unittest suite (including Lua 5.1 and 5.4 contract tests through
Lupa), validates the JSON Schemas, compiles Python and Lua sources, and checks
whitespace. These checks do not
prove anything that requires the actual FS25 runtime.

| Path | Purpose |
| --- | --- |
| AGENTS.md | Authoritative rules for all coding agents |
| BUILD_SPEC.md | Local runtime architecture, scope, acceptance and runtime gates |
| companion/ | Deterministic executor, FS25 command bridge and World adapter, windowless request service, diagnostic runner |
| protocol/ | Executor contract, shared types, local IPC frame protocol |
| adapters/ | World interface, FS25 snapshot mapping, upstream evidence |
| mod/ | FS25_Farmhand: observation, HITCH, command bridge, player key and notices, diagnostic probe |
| simulator/ | In-memory mock FS25 world |
| tests/ | AC-001 specification and executable checks |
| scripts/check.py | Single CI verification command |
| scripts/install_windows.cmd | One-time companion install on the Windows PC |

See [AC-001](tests/ACCEPTANCE.md) for behavioral acceptance and
[BUILD_SPEC.md](BUILD_SPEC.md) for what still requires actual FS25.
