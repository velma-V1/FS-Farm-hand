# FS25 Lua side (`FS25_Farmhand`)

- `FarmhandObserve.lua`: read-only observation. Builds protocol v1 `hello` and
  `snapshot` frames (`protocol/frame.schema.json`) from the game and mod calls
  recorded in `adapters/EVIDENCE.md`, including hitch geometry read from GIANTS'
  own joint offsets. It never changes game state.
- `FarmhandHitch.lua`: HITCH primitives: GIANTS GOTO staging, the bounded
  ALIGN_REVERSE (steering by `AIVehicleUtil.driveToPoint`, Farmhand-local
  attach-assist distance, vertical guard, brake + cruise off on every stop) and
  GIANTS' normal attach with exact-candidate and exact-joint checks.
- `FarmhandBridge.lua`: the AC-001 command bridge. Runs only the five operations
  the companion asks for (`drive`, `stage`, `align`, `attach`, `stop`) for its
  live session, and reports each one's observed end. Server only.
- `FarmhandProbe.lua`: diagnostic console commands that drive the same bridge
  operations by hand (`fhHitchList`, `fhHitchProbe`, `fhHitchStop`).

Each file assigns one mod global (`FarmhandObserve`, `FarmhandHitch`,
`FarmhandBridge`, `FarmhandProbe`) because FS25 keeps no `sourceFile` return
value; CI loads the returned tables. Game globals are passed as `env`
(`getfenv(0)` in game). Nothing moves on load: the bridge waits for a companion
session and the probe for a typed command.

## First full AC-001 run

On the Windows PC with FS25, AutoDrive and Python 3.12:

1. Copy the `FS25_Farmhand` folder from the CI artifact (or this `mod/` folder) to
   `Documents/My Games/FarmingSimulator2025/mods/FS25_Farmhand/` (a folder, not a
   ZIP). In a single-player save, enable it with AutoDrive. Own one tractor and one
   forage wagon on an AutoDrive network, with a marker near the wagon and one near
   you. Get out of the tractor.
2. Open the console (`~`) and run `fhHitchList`; note the tractor's `store=` value.
3. Copy `farmhand.example.json` to `farmhand.json` and put that value in
   `tractor_store_categories`.
4. In a terminal in this repository (`pip install -r requirements.txt` once), run
   `python -m companion.run --config farmhand.json here`. Alternatively use
   `marker "<name>"`, `marker-id <id>` or `field <n>`, with `field_entrances` set.
5. Watch the run. It ends with one JSON line: `status`, `reason`, the two vehicle IDs
   and the stages passed. Ctrl+C at any time stops the companion; the game then
   stops its motion within 10 s.
6. Send the terminal output and the `[Farmhand]` lines of `log.txt`.

If the run fails in HITCH, the diagnostic probe repeats only that part:
`fhHitchProbe <tractorId> <wagonId> [assistRadiusM] [maxVerticalM] [maxSpeedKmh]`
(assist radius ≤ 1.5 m, vertical ≤ 1.0 m, speed 1–5 km/h; `fhHitchStop` aborts).
It ends with one `RESULT PASS|FAILED|UNVERIFIED <reason>` line and refuses while a
companion session owns the bridge.

CI compiles every file under Lua 5.1 and 5.4, lints it with luacheck, and runs all
four modules against stubs that implement only the evidenced calls. That proves our
halves agree; it does not prove FS25 loads this code, that the game returns the same
shapes, or that a tractor drives or attaches. None of these files has run inside FS25.
