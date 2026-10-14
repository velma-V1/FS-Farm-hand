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
mod.** Upstream capabilities are recorded with source evidence in
[adapters/EVIDENCE.md](adapters/EVIDENCE.md). A read-only Lua observation module,
the local IPC frame protocol, fail-closed FS25 data mapping, and the HITCH
composition (pure geometry plan, executor steps, game-side GIANTS GOTO staging,
bounded reverse and normal attach) exist and are tested off-PC only. The command
channel to the game, the Windows pipe host, any executed movement/attachment, LLM
integration, speech, and Windows service behavior are not implemented yet.
The project can remain off the user's PC until a real FS25 integration test is needed.

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
| companion/ | Deterministic one-task executor |
| protocol/ | Executor contract, shared types, local IPC frame protocol |
| adapters/ | World interface, FS25 snapshot mapping, upstream evidence |
| mod/ | Fail-closed Lua boundary and read-only FS25 observation |
| simulator/ | In-memory mock FS25 world |
| tests/ | AC-001 specification and executable checks |
| scripts/check.py | Single CI verification command |

See [AC-001](tests/ACCEPTANCE.md) for behavioral acceptance and
[BUILD_SPEC.md](BUILD_SPEC.md) for what still requires actual FS25.
