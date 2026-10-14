# FS-Farm-hand

Cloud-first foundation for one FS25 autonomous farmhand task: select an available
compatible tractor and forage wagon, attach, and bring them to the requester.

**Status: executable mock acceptance harness, not an installable or game-verified
mod.** LLM interpretation, real transport, GIANTS/AutoDrive adapters, and actual
movement/attachment are not implemented. No PC setup is required for development.

## Cloud checks

In a Python 3.12 cloud checkout:

```sh
python -m pip install -r requirements-dev.txt
python scripts/check.py
```

This runs the entire unittest suite, validates the JSON Schema, executes the Lua
boundary using Lupa, compiles Python sources, and checks whitespace. CI runs the
same command. No game install, API keys, model downloads or external mod code.

| Path | Purpose |
| --- | --- |
| AGENTS.md | Authoritative rules for all coding agents |
| BUILD_SPEC.md | Scope, architecture, acceptance and runtime gates |
| companion/ | Deterministic one-task executor |
| protocol/ | Structured intent schema and shared data types |
| adapters/ | World interface and upstream reuse boundaries |
| mod/ | Fail-closed Lua boundary for future FS25 integration |
| simulator/ | In-memory mock FS25 world |
| tests/ | AC-001 specification and executable checks |
| scripts/check.py | Single cloud verification command |

See [AC-001](tests/ACCEPTANCE.md) for behavioral acceptance and
[BUILD_SPEC.md](BUILD_SPEC.md) for what still requires actual FS25.
