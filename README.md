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

**Status: executable mock acceptance harness, not an installable or game-verified
mod.** Real GIANTS/AutoDrive adapters, local IPC, actual movement/attachment,
LLM integration, speech, and Windows service behavior are not implemented yet.
The project can remain off the user's PC until a real FS25 integration test is needed.

## Pre-game CI checks

In GitHub Actions or another Python 3.12 development checkout:

```sh
python -m pip install -r requirements-dev.txt
python scripts/check.py
```

This runs the unittest suite, validates the JSON Schema, executes the Lua boundary
using Lupa, compiles Python sources, and checks whitespace. These checks do not
prove anything that requires the actual FS25 runtime.

| Path | Purpose |
| --- | --- |
| AGENTS.md | Authoritative rules for all coding agents |
| BUILD_SPEC.md | Local runtime architecture, scope, acceptance and runtime gates |
| companion/ | Deterministic one-task executor |
| protocol/ | Structured executor contract and shared data types |
| adapters/ | World interface and upstream reuse boundaries |
| mod/ | Fail-closed Lua boundary for future FS25 integration |
| simulator/ | In-memory mock FS25 world |
| tests/ | AC-001 specification and executable checks |
| scripts/check.py | Single CI verification command |

See [AC-001](tests/ACCEPTANCE.md) for behavioral acceptance and
[BUILD_SPEC.md](BUILD_SPEC.md) for what still requires actual FS25.
