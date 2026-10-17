# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Read `AGENTS.md` (authoritative rules for every agent) and `BUILD_SPEC.md` before editing.**
This file adds only orientation; where they differ, AGENTS.md wins. Report conflicts
with the user's request instead of silently changing scope.

## Commands

Python 3.12 with pinned deps (`.venv/` exists locally):

```sh
python -m pip install -r requirements-dev.txt   # once
python scripts/check.py                          # the single gate; run before proposing any change
python -m unittest tests.test_hitch -v           # one module
python -m unittest tests.test_farmhand.ExplicitDestinationTests.<test_name>   # one test
python scripts/package_probe.py --build dist/FS25_Farmhand   # build the mod folder as CI does
luacheck mod                                     # Lua lint (CI job; config in .luacheckrc)
```

Optional voice: `pip install -r requirements-voice.txt`, `python -m companion.voice --install`,
then `FARMHAND_WHISPER_DIR=~/FS25Farmhand/whisper-base.en python -m unittest tests.test_voice`
runs real speech-to-text on the fixtures (skipped otherwise; CI job "voice").

`check.py` runs all unittests (Lua tests run the real `mod/*.lua` under Lua 5.1 and
5.4 via Lupa), compiles Python and Lua, validates the mod and companion package
layouts (the bundle must import on its own), and runs
`git diff --check HEAD`. Also inspect `git diff --check` and the final tree yourself.
CI (`.github/workflows/ci.yml`) runs it on Ubuntu and Windows; Windows named-pipe
tests only execute on Windows. CodeQL must report zero findings.

Normal operation is `companion/service.py` (windowless, logon-started by
`scripts/install_windows.cmd`) answering the in-game key; the diagnostic runner
(`python -m companion.run here|marker|marker-id|field`) runs one request. Both only work on
the user's Windows PC beside FS25 — see `mod/README.md`. The optional, gitignored
`farmhand.json` (`--config`; template `farmhand.example.json`) is needed only for
field targets or a tractor category override.

## Architecture

Single task (AC-001): pick an available compatible tractor + forage wagon, hitch
them, deliver them to the requester. Pipeline: FIND → RESERVE → MOVE (HITCH) →
ATTACH → VERIFY → DELIVER, each physical step re-validated from a fresh snapshot.

- `protocol/` — pure contracts: frozen types (`model.py`), intent and IPC frame
  JSON Schemas, and the untrusted frame decoder/encoder (`ipc.py`). Not an FS25 API.
- `adapters/world.py` — the `World` port the executor depends on; `StepFailed` /
  `StepUnverified` distinguish confirmed failure from unknown outcome.
  `adapters/fs25.py` maps game snapshot frames into `Equipment` and fails closed on
  anything unknown. `adapters/EVIDENCE.md` is the authoritative record of every
  upstream GIANTS/mod call (pinned commits, PROVEN/UNVERIFIED/GAP); never call an
  API that isn't recorded there.
- `companion/executor.py` — deterministic `Executor(world).execute(intent) -> Result`
  (COMPLETE / BLOCKED / FAILED / UNVERIFIED). Destination resolution and hitch
  planning (`companion/hitch.py`, pure) happen before RESERVE so failures there
  are BLOCKED with nothing moved. Replay is cached per executor instance.
- `companion/bridge.py` — the real adapter `FS25World` plus the local IPC:
  game → companion JSON lines over a Windows named pipe hosted by the companion;
  companion → game one command at a time in `modSettings/FS25_Farmhand/command.xml`
  (atomic rename) plus a `session.xml` heartbeat that also carries player notices.
  Only six ops exist: `drive` (AutoDrive), `navigate` (GIANTS GOTO), `stage`,
  `align`, `attach`, `stop`. ACCEPTED/DONE is never success; the game stops a
  session's motion if the heartbeat is lost for 10 s. Player key presses arrive as
  `request` frames (a fixed `action` or typed `text`).
- `companion/language.py` (deterministic rules; a local model may sit behind them)
  turns words into a command proposal (`protocol/command.schema.json`, no
  authority); `companion/coordinator.py` validates it, runs one job at a time on a
  worker thread, answers STOP/STATUS immediately (STOP cancels before the next step
  and pre-empts the running bridge op); `companion/service.py` is the windowless
  process wiring it together. Optional layers, both off unless installed:
  `companion/model.py` (CPU-only llama-server, schema-constrained, behind the rules)
  and `companion/voice.py` (push-to-talk → faster-whisper → the text path).
- `mod/` — FS25 Lua mod `FS25_Farmhand` (load order in `modDesc.xml`):
  `FarmhandObserve` (read-only snapshots), `FarmhandHitch` (GIANTS GOTO staging,
  bounded ALIGN_REVERSE, GIANTS attach), `FarmhandBridge` (executes companion
  commands, the player key and notifications; server only), `FarmhandProbe`
  (`fhHitch*` console diagnostics). Each
  file sets one global; game globals come in as `env`, so tests load them with stubs
  that implement only evidenced calls.
- `simulator/world.py` — in-memory `MockWorld` for tests. Never import it from
  `companion/` or `mod/`.

Cross-language invariants: constants and the hitch plan are mirrored between
`companion/hitch.py` / `companion/bridge.py` and the Lua files (comments like
`= mod/FarmhandBridge.lua Bridge.MOD_DIR`); `tests/test_lua_probe.py` asserts they
match. Change shared schemas, both sides, and all consumers together.

`tests/ACCEPTANCE.md` maps each acceptance clause to specific tests — update it
when behavior changes. Write the behavior test first; vary IDs and inventory order
rather than asserting fixture IDs.

## Status and limits

Nothing has run inside FS25. Passing CI does not prove mod loading, physics, attach
alignment, routing, IPC on Windows, or game integration — say so when reporting.
The only self-driven motion allowed is ALIGN_REVERSE's capped final reverse; all
other travel is delegated to AutoDrive (optional; marker destinations only) or GIANTS
GOTO (`navigate`, `stage`), whose navigation agent plans for the attached wagon.
