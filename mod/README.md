# FS25 Lua side — not yet an installable mod

- `FarmhandBoundary.lua`: pure capability stub. It reports no physical capability
  and rejects every physical request (move, attach, stop).
- `FarmhandObserve.lua`: read-only observation. Builds protocol v1 `hello` and
  `snapshot` frames (`protocol/frame.schema.json`) using only game and mod calls
  recorded as upstream usage in `adapters/EVIDENCE.md`, including hitch geometry
  read from GIANTS' own joint offsets. It never changes game state.
- `FarmhandHitch.lua`: server-side HITCH primitives — GIANTS GOTO staging, the
  bounded ALIGN_REVERSE (steering by `AIVehicleUtil.driveToPoint`) and GIANTS'
  normal attach with exact-candidate checks. A library only: nothing calls it yet.
  Game globals are injected as `env` (pass `_G` in game).

CI compiles every file under Lua 5.1 and 5.4 and runs the modules against stubs
that implement only the evidenced calls. That proves our Lua and
Python halves agree; it does not prove FS25 loads this code, that the game
returns the same shapes, or that a tractor drives or attaches. None of these files
has ever run inside FS25.

There is no `modDesc.xml`, update loop, pipe writer, or install ZIP yet. Those are
added at the first in-game test, following the write-only pipe client pattern in
Farmhand Companion `scripts/Transport.lua`. The production runtime is local on the
user's Windows PC alongside FS25.
