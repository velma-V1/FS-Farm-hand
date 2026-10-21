# FS-Farm-hand

Local-first foundation for an autonomous Farming Simulator 25 farmhand.

The finished farmhand is intended to run on the user's Windows PC alongside FS25:
a game-side Lua mod communicates through local IPC with a local companion service,
which will host the deterministic executor and later the local LLM, speech
recognition, and TTS components.

GitHub is used only for source control, development, CI, review, and packaging.
It is not part of the farmhand's runtime architecture.

It is a coordinator: GIANTS' own jobs (GOTO, field work, buying, Foldable,
TensionBelts) and installed mods (AutoDrive, FollowMe) do the work, with Farmhand's
own bounded motions only where nothing else can (the final hitch reverse, pulling
clear, leaving a shed, driving a tractor onto a transport). Plain words in game
become jobs: fetching and hitching pairs, field work, buying with configuration
options, truck and gooseneck hitching, loading, belting, hauling and unloading
tractors, several jobs in one sentence, stop / continue / change of destination.

**Status:** many of these are qualified live on the user's PC (FourFields save 2;
see [tests/LIVE_QUALIFICATION.md](tests/LIVE_QUALIFICATION.md)); the rest pass the
automated suite only, and one working combination is never taken to mean all
equipment works. Open items are listed in
[tests/ACCEPTANCE.md](tests/ACCEPTANCE.md) (completion criteria) and
[BUILD_SPEC.md](BUILD_SPEC.md) (completion plan). Upstream capabilities are recorded
with source evidence in [adapters/EVIDENCE.md](adapters/EVIDENCE.md).

How it is used: install once by double-clicking `Setup.cmd` in the Farmhand package
(it carries its own Python; nothing else to install), then in
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
| companion/setup.py | Setup / Uninstall (the package's Setup.cmd, Uninstall.cmd) |
| scripts/package_probe.py | Builds the mod, the app and the qualification package |

See [AC-001](tests/ACCEPTANCE.md) for behavioral acceptance and
[BUILD_SPEC.md](BUILD_SPEC.md) for what still requires actual FS25.
