# FS25 Lua side

- `FarmhandObserve.lua`: read-only observation. Builds protocol v1 `hello` and
  `snapshot` frames (`protocol/frame.schema.json`) from the game and mod calls
  recorded in `adapters/EVIDENCE.md`, including hitch geometry read from GIANTS'
  own joint offsets. It never changes game state.
- `FarmhandHitch.lua`: server-side HITCH primitives: GIANTS GOTO staging, the
  bounded ALIGN_REVERSE (steering by `AIVehicleUtil.driveToPoint`, Farmhand-local
  attach-assist distance, vertical guard, brake + cruise off on every stop) and
  GIANTS' normal attach with exact-candidate and exact-joint checks.
- `FarmhandProbe.lua` + `modDesc.xml`: a **development-only** folder mod for the
  first live HITCH experiment. Console commands only; nothing runs on load.

Each file assigns one mod global (`FarmhandObserve`, `FarmhandHitch`,
`FarmhandProbe`) because FS25 keeps no `sourceFile` return value; CI loads the
returned tables. Game globals are passed as `env` (`getfenv(0)` in game).

## First live experiment (the probe)

1. Copy this `mod/` folder to `Documents/My Games/FarmingSimulator2025/mods/FS25_FarmhandProbe/`
   (a folder, not a ZIP) and enable "Farmhand hitch probe (development)" for a
   single-player save in which you own one tractor and one forage wagon.
2. Park the wagon on open, level ground with about 15 m clear in front of its
   drawbar. Get out of the tractor and open the console (`~`).
3. `fhHitchList`: note the two IDs and check the tractor's `out=` and the wagon's
   `in=` joints.
4. `fhHitchProbe <tractorId> <wagonId>`: one attempt with GIANTS' own attach
   distance. Watch it; `fhHitchStop` aborts at any time.
5. If it ends `FAILED` with the tractor stopped short, detach/reset and repeat with
   `fhHitchProbe <tractorId> <wagonId> 1.0`, then `1.25`, then `1.5` (Farmhand-only
   attach-assist radius in metres). Optional further arguments: `maxVerticalM`
   (default 0.5, max 1.0) and `maxSpeedKmh` (default 3, 1–5).
6. Send `log.txt` (every line starting `[Farmhand]`).

Each attempt ends with exactly one `RESULT PASS|FAILED|UNVERIFIED <reason>` line.
`PASS` means a fresh observation shows exactly this wagon on exactly the planned
tractor joint, after GIANTS' own implement entry named the planned joint pair.

CI compiles every file under Lua 5.1 and 5.4 and runs all three modules against
stubs that implement only the evidenced calls. That proves our halves agree; it
does not prove FS25 loads this code, that the game returns the same shapes, or
that a tractor drives or attaches. None of these files has run inside FS25.

There is no production command channel, pipe writer or install ZIP. The pipe
client is added later following Farmhand Companion `scripts/Transport.lua`.
