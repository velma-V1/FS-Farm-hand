# FS25 Lua side (`FS25_Farmhand`)

- `FarmhandObserve.lua`: read-only observation. Builds protocol v2 `hello` and
  `snapshot` frames (`protocol/frame.schema.json`) from the game and mod calls
  recorded in `adapters/EVIDENCE.md`, including hitch geometry read from GIANTS'
  own joint offsets. It never changes game state.
- `FarmhandHitch.lua`: HITCH primitives: GIANTS GOTO staging, the bounded
  ALIGN_REVERSE (steering by `AIVehicleUtil.driveToPoint`, Farmhand-local
  attach-assist distance, vertical guard, brake + cruise off on every stop) and
  GIANTS' normal attach with exact-candidate and exact-joint checks.
- `FarmhandBridge.lua`: the AC-001 command bridge. Runs only the six operations
  the companion asks for (`drive`, `navigate`, `stage`, `align`, `attach`, `stop`)
  for its live session, and reports each one's observed end. Server only. It also
  owns the player's key (`FARMHAND_FETCH_HERE`, a `request` frame) and shows the
  companion's notices as in-game notifications.
- `FarmhandProbe.lua`: diagnostic console commands that drive the same bridge
  operations by hand (`fhHitchList`, `fhHitchProbe`, `fhHitchStop`).
- `translations/translation_en.xml`: the key's name in the game's controls.

Each file assigns one mod global (`FarmhandObserve`, `FarmhandHitch`,
`FarmhandBridge`, `FarmhandProbe`) because FS25 keeps no `sourceFile` return
value; CI loads the returned tables. Game globals are passed as `env`
(`getfenv(0)` in game). Nothing moves on load: the bridge waits for a companion
session and the probe for a typed command.

## The live validation session (one sitting, on the Windows PC)

Everything below is the normal player path plus the checks that only FS25 can
answer; one sitting covers them. Needs FS25 and Python 3.12 (`py` launcher).

1. **Install once.** The CI artifact `FS25_Farmhand-<commit>` holds two folders.
   Copy `FS25_Farmhand` to `Documents/My Games/FarmingSimulator2025/mods/FS25_Farmhand/`
   (a folder, not a ZIP). Put `FarmhandCompanion` anywhere (for example
   `Documents/FarmhandCompanion`) and double-click its
   `scripts/install_windows.cmd`: it installs the companion and starts it,
   windowless, now and at every logon. (From a repository checkout, the same
   script and this `mod/` folder work too.)
2. **Without AutoDrive.** Disable AutoDrive (if installed) and load a
   single-player save with the Farmhand mod. Own one tractor with nothing attached
   (no front weight either) and one forage wagon. Expect the notification
   "Farmhand: companion connected". Get out of the tractor, walk somewhere the
   tractor can drive to and press **Left Alt + J**. Expect "fetching a tractor and
   forage wagon", the tractor driving to the wagon, reversing, hitching, then
   driving to about 10 m in front of you and "tractor and forage wagon delivered".
3. **Far pickup.** Park the tractor more than 60 m from the wagon and repeat (GIANTS
   GOTO travels to the wagon first).
4. **Refusals and recovery.** Press the key again during a run (expect "already
   working on a request"). Attach a front weight to the tractor and press (expect
   "not delivered - no available tractor and forage wagon"; nothing moves), then
   remove it. Quit to the main menu, load the save again, expect "companion
   connected" and press once more.
5. **With AutoDrive.** Enable AutoDrive with a marker within 50 m of you and leave
   "start helper" off in the tractor's AutoDrive HUD (Farmhand refuses to drive
   while AutoDrive would hand the tractor to another AI). Press the key: delivery
   goes to that marker.
6. **Send** `Documents/My Games/FarmingSimulator2025/modSettings/FS25_Farmhand/companion.log`,
   the `[Farmhand]` lines of `log.txt`, and, if no pair was found, the output of
   `fhHitchList` (console, `~`). Note what you saw at each step.

`python -m companion.run here` (or `marker "<name>"`, `marker-id <id>`, or
`--config farmhand.json field <n>` with `field_entrances` set) runs one request from
a terminal instead of the key, for diagnostics; stop the installed companion first
(Task Manager, `pythonw.exe`), since only one companion can host the pipe.
`python -m companion.service --uninstall` removes the logon start.

If the run fails in HITCH, the diagnostic probe repeats only that part:
`fhHitchProbe <tractorId> <wagonId> [assistRadiusM] [maxVerticalM] [maxSpeedKmh]`
(assist radius ≤ 1.5 m, vertical ≤ 1.0 m, speed 1–5 km/h; `fhHitchStop` aborts).
It ends with one `RESULT PASS|FAILED|UNVERIFIED <reason>` line and refuses while a
companion session owns the bridge.

CI compiles every file under Lua 5.1 and 5.4, lints it with luacheck, and runs all
four modules against stubs that implement only the evidenced calls. That proves our
halves agree; it does not prove FS25 loads this code, that the game returns the same
shapes, or that a tractor drives or attaches. None of these files has run inside FS25.
