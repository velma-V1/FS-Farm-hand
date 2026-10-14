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
[adapters/EVIDENCE.md](adapters/EVIDENCE.md). The FS25 mod (`FS25_Farmhand`:
observation, HITCH primitives, command bridge, diagnostic probe), the local
companion bridge (Windows named pipe in, atomic XML command file and session
heartbeat out), the real World adapter and a one-shot runner
(`python -m companion.run`) exist. LLM integration, speech and a Windows service
are not part of AC-001. First-run steps: [mod/README.md](mod/README.md).

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
| companion/ | Deterministic executor, FS25 command bridge and World adapter, runner |
| protocol/ | Executor contract, shared types, local IPC frame protocol |
| adapters/ | World interface, FS25 snapshot mapping, upstream evidence |
| mod/ | FS25_Farmhand: observation, HITCH, command bridge, diagnostic probe |
| simulator/ | In-memory mock FS25 world |
| tests/ | AC-001 specification and executable checks |
| scripts/check.py | Single CI verification command |

See [AC-001](tests/ACCEPTANCE.md) for behavioral acceptance and
[BUILD_SPEC.md](BUILD_SPEC.md) for what still requires actual FS25.
