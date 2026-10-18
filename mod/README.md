# FS25 Lua side (`FS25_Farmhand`)

- `FarmhandObserve.lua`: read-only observation. Builds protocol v2 `hello` and
  `snapshot` frames (`protocol/frame.schema.json`) from the game and mod calls
  recorded in `adapters/EVIDENCE.md`, including hitch geometry read from GIANTS'
  own joint offsets. It never changes game state.
- `FarmhandHitch.lua`: HITCH primitives: GIANTS GOTO staging (only from beyond
  25 m), the bounded forward LOCAL_MANEUVER onto the wagon's centreline and the
  bounded ALIGN_REVERSE (both steered by `AIVehicleUtil.driveToPoint`; Farmhand-local
  attach-assist distance, vertical guard, brake + cruise off on every stop) and
  GIANTS' normal attach with exact-candidate and exact-joint checks.
- `FarmhandBridge.lua`: the AC-001 command bridge. Runs only the seven operations
  the companion asks for (`drive`, `navigate`, `stage`, `maneuver`, `align`, `attach`, `stop`)
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

## Live tests driven by Claude Code (development only)

Claude Code runs in a sandbox that cannot write the FS25 folders or start Windows
programs, so two narrow bridges carry a live test:

1. Claude → host runner: `python scripts/fs25_live_test.py <command>` (in the
   sandbox) writes one request at a time to `.fs25-dev/host/requests` in this
   checkout; `scripts/fs25_host_runner.py`, started once by the user outside the
   sandbox, answers in `.fs25-dev/host/responses`. Only seven ops exist (`status`,
   `install-current-build`, `launch-fs25`, `restart-fs25`, `run-hitch-test`,
   `stop-test`, `collect`); none takes a path, program or command text, and every
   request names the runner's per-start token and a fresh, increasing id. One
   runner per checkout: a second one refuses to start (OS lock on
   `.fs25-dev/host/runner.lock`, or another runner's live heartbeat in
   `runner.json`), a runner whose heartbeat another took over stops, a request is
   claimed by one atomic rename, and every answer names its runner, so the client
   never takes another process's answer as the outcome (`ANOTHER_RUNNER_ANSWERED`).
2. Runner → companion: `companion/livetest.py`, active only when the companion is
   started with `--live-test` (install-current-build does; the logon start never
   does), reads `modSettings/FS25_Farmhand/livetest`. `hitch` submits the player's
   own proposal (`hitch_pair`) to the coordinator, so the production executor runs
   FIND → RESERVE → STAGE → LOCAL_MANEUVER → ALIGN_REVERSE → ATTACH → VERIFY;
   `stop` is the STOP key; `status` only reads.

Once, on the PC (WSL, outside Claude's sandbox; the companion package must already
be installed by its Setup.cmd):

    python3 scripts/fs25_host_runner.py install-runner
    python3 ~/.local/share/fs25-farmhand-host/fs25_host_runner.py serve --repo ~/projects/FS-Farm-hand [--savegame N]

Then Claude runs `status`, `install` (FS25 closed), `restart`, `hitch`, `collect`.
The runner copies only the files of `scripts/package_probe.py --build-dev` whose
sha256 its manifest names and whose build is the checkout's commit, to the fixed mod
and companion folders. `--savegame N` passes FS25's `-autoStartSavegameId N`
(UNVERIFIED on the Microsoft Store build). Stop the runner with Ctrl+C; nothing of
it is in the release.

## Hitch retest (do this first)

GIANTS' GOTO is now used only when the tractor's hitch is more than 25 m from the
wagon's drawbar, and only to get it within 25 m. Inside that, Farmhand lines the
tractor up itself: forward only, slowly (3 km/h), on one curve onto the wagon's
centreline a few metres in front of the drawbar, then the bounded reverse.

0. Same setup as the failing test: tractor 170 (nothing attached) and wagon 173,
   open ground of about 25 m around the wagon's drawbar, no other vehicle parked
   within about 10 m of where the tractor will drive (Farmhand refuses a line that
   passes another vehicle, and stops if one moves or comes close). Get out and
   stand on foot well clear of that area, more than 6 m from the tractor's way
   (Farmhand needs an empty cab, and stops if you come within 4 m). Left Alt + K: "hitch the forage wagon". Expect "driving to the wagon"
   (only if farther than 25 m), "lining up with the wagon", "reversing to the
   wagon", "hitching ...", "hitched". Left Alt + L stops it at any time.
   If it fails, the notice names the reason (for example "there is no room to line
   up with the wagon from here" is a refusal before moving, "the tractor could not
   follow its line to the wagon" a stop while moving). Either way type "report"
   and send the zip: log.txt has one `[Farmhand] maneuver ...` line per phase
   (LOCAL_TURN / LOCAL_APPROACH / LOCAL_LINEUP) and at the end, with both joints,
   gap, ahead, lateral, heading, the steering command, speed, time, the
   tractor's turning radius and why.

## Retest after the first live session

1. Sit in an eligible tractor (nothing attached) near the forage wagon, Left Alt + K:
   "take the tractor and attach the forage wagon". Farmhand drives it to the wagon
   and lines it up with you riding along. For the last few metres of reversing
   onto the wagon the cab must be empty: when Farmhand says "step out of the
   tractor for the last metres of the hitch", get out and ask again (it stops and
   releases first). Expect "hitching ..." then "hitched".
2. Unhitch, sit in again, Left Alt + K "bring me a tractor and forage wagon", and
   press Left Alt + L while it moves (expect "stopping", "stopped"). Then give a
   short pedal or steering input during another run: Farmhand stops and says you
   took the controls.
3. After any failed pickup, ask once more: the same tractor must be used again.
4. Type "take a tractor to the wagon" (hitch), "bring a tractor to me" (expect "a
   tractor on its own is not supported yet") and "bring the Boss Hoss to the farm"
   (expect "choosing a vehicle by its name is not supported yet").
5. If you want: Left Alt + N, pick the xLAM model again; it is loaded first now.
6. Type "report" and send the zip. If all of this passes, continue below.

## The live validation session (one sitting, on the Windows PC)

Everything below is the normal player path plus the checks that only FS25 can
answer; one sitting covers them. Needs only FS25 (start it once beforehand so its
profile folder exists). No microphone is needed: step 6 only confirms that voice
says it is unavailable.

1. **Install once.** Download the CI artifact `FS25Farmhand-qualification-<commit>`
   (a test package, not a release), extract it anywhere and double-click
   `Setup.cmd`. It installs the mod and the companion, starts the companion now
   and at every logon, and says so in a window. At the first FS25 connection
   Farmhand offers its AI menu (Left Alt + N later): pick your Ollama model if you
   have one, or a Farmhand download, or "Continue without AI"; try both "without AI"
   and a model during the session.
2. **Fetch, without AutoDrive.** Disable AutoDrive (if installed) and load a
   single-player save with the Farmhand mod. Own one tractor with nothing attached
   (no front weight either) and one forage wagon. Expect "Farmhand: companion
   connected". Get out of the tractor, walk somewhere the tractor can drive to,
   press **Left Alt + K** and type "bring me a tractor and forage wagon". Expect
   "fetching a tractor and forage wagon", "lining up with the wagon", the reverse
   and hitch, "driving to the destination", the pair stopping about 10 m in front
   of you, facing you, and "tractor and forage wagon delivered". During the run,
   Left Alt + K "what are you doing?" names the current step.
3. **Move it.** Walk 50 m away and type "come here" (expect "on my way", the
   hitched pair driving to you, "arrived"). Type "take this back to the barn" (or
   the word for one of your buildings, e.g. "shed"): it drives to about 15 m short
   of that building. Type "take this to the moon" (expect "I do not know that place";
   nothing moves).
4. **Far pickup.** Unhitch (by hand), park the tractor more than 60 m from the
   wagon and fetch again (GIANTS GOTO travels to the wagon first).
5. **Stop, refusals and recovery.** Start a fetch with **Left Alt + J**, press it
   again ("already working on a request"), then press **Left Alt + L** ("stopping",
   the tractor stopping where it is, "stopped"); during another run type "stop"
   for the same. Type something unrelated ("did not understand"), and something the
   rules do not know but the model should, e.g. "could you get the forage wagon
   hooked up and over to me". Attach a front weight and fetch ("no available
   tractor and forage wagon"; nothing moves), then remove it. Quit to the main
   menu, load the save again, expect "companion connected" and fetch once more.
6. **Voice.** Hold **Left Alt + V**. Without a microphone expect "voice is
   unavailable…" and nothing else; that is correct. With one: say "bring me a
   tractor and forage wagon" and release; expect "listening..." and the same run,
   with spoken replies.
7. **With AutoDrive.** Enable AutoDrive with a marker within 50 m of you and leave
   "start helper" off in the tractor's AutoDrive HUD (Farmhand refuses to drive
   while AutoDrive would hand the tractor to another AI). Fetch: delivery goes to
   that marker.
8. **Report.** Type **report** (Left Alt + K): Farmhand saves
   `Farmhand-report-<time>.zip` on your Desktop (its logs, the `[Farmhand]` lines of
   the game's log, your settings and the last game snapshot; nothing is sent). Send
   that zip, and note anything you saw that it cannot show, such as whether the game
   stayed smooth while the model answered (step 5).

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
