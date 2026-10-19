# AC-001 — bring a tractor and forage wagon here

User command: “Go grab an available tractor and forage wagon, hook them together,
and bring them here.” The player types it in game (Left Alt + K on foot) or
presses the fixed key (Left Alt + J); the companion turns either into this
structured intent itself; Left Alt + L or the words "stop"/"cancel" stop:

```json
{"version":1,"request_id":"request-001","task":"deliver_tractor_and_forage_wagon","farm_id":"farm-a","target":"player_at_acceptance"}
```

Given a requester on farm-a and an inventory with arbitrary IDs and order,
including an available compatible tractor/wagon pair and misleading decoys:
1. FIND must select by category, same farm, availability, capabilities, power,
   hitch and PTO. Do not assert specific fixture IDs; inspect selected properties.
2. RESERVE owns both units atomically; concurrent requests cannot steal either.
3. MOVE is HITCH: a pure plan picks the joints and the final/staging poses before
   reservation; STAGE (GIANTS GOTO aimed at approaches beside the implement, each
   at most once, those GIANTS refuses as blocked skipped, at most three drives:
   beyond the 25 m local work radius, or after a LOCAL_MANEUVER refused unmoved
   for where the tractor stands),
   LOCAL_MANEUVER (bounded, forward, game-side, only outside the reverse's envelope)
   and ALIGN_REVERSE (bounded, game-side) are each followed by a fresh check that the pair is still reserved, the wagon has not
   moved and the step's outcome is observed. ATTACH re-checks GIANTS' exact
   candidate; VERIFY checks both ends of the attachment from fresh state before any
   delivery movement.
4. DELIVER delegates travel to the resolved destination: by default "here", the
   nearest AutoDrive marker within 50 m of the requester's position captured at
   acceptance when the tractor has AutoDrive, else a GIANTS GOTO pose 10 m short of
   that position facing the requester (no marker needed; AutoDrive is optional); or
   an explicit marker (live ID or exact normalized name; needs AutoDrive); or a field
   entrance from the local map-specific table. Resolution happens before RESERVE.
5. COMPLETE requires mutual attachment, tractor within 3 m and wagon within 15 m of
   that destination, both stopped, and successful reservation release. Inspect
   state, not just trace.

Executable mapping (`python scripts/check.py`):
- `test_selection_is_behavioral`: many deterministic random seeds vary IDs and
  order; decoys include wrong category/farm, busy, occupied, unavailable, reserved,
  attached, underpowered, wrong hitch, missing PTO and missing capabilities.
- `test_no_pair`, `test_pair_search`: no suitable pair blocks without movement;
  search considers alternative tractors and wagons.
- `test_atomic_contention`: only one of simultaneous requests owns a pair.
- `test_action_time_farm_change_prevents_movement` and
  `test_action_time_compatibility_change_prevents_delivery`: changes between a
  snapshot and the physical call are rejected before world positions mutate.
- `test_active_owner_collision_preserves_unselected_equipment` and
  `test_stop_without_owner_is_rejected`: cleanup cannot claim unrelated assets.
- `test_changed_after_reservation`: fresh eligibility prevents stale selection use.
- `test_physical_failures`: stage/align/attach/delivery errors and timeouts stop and
  fail; no later physical step runs; no fabricated success.
- `tests/test_hitch.py`: joint selection among many joints, incompatible/unknown
  geometry, final and staging pose for random poses, orientation, determinism; PTO
  matched on the planned joints only when PTO power is needed; unknown fails closed.
- `PowerAndPtoTests`: GIANTS' torque rule accepts adequate/exact capacity, rejects
  insufficient or unknown power, never rejects a wagon needing no PTO power, and
  matches PTO on the planned joint pair (not any joint).
- `HitchCompositionTests`: steps use the plan exactly; AutoDrive TRAVEL_NEAR only
  when far (and unobserved arrival stops); GOTO (STAGE) aims at the approaches
  (`ApproachTests` in test_hitch: beside the implement, a clear side first), only
  beyond the work radius or after a maneuver refused unmoved, each approach once,
  refused (blocked) aims skipped unmoved, at most MAX_APPROACH_DRIVES drives;
  STAGE ends beyond it on both fail TRAVEL_ENDED_TOO_FAR with no local motion, one outside the reverse's envelope is lined up by LOCAL_MANEUVER,
  a maneuver failure or a maneuver end outside the envelope never reverses;
  unplannable pairs block
  before reservation; staging not observed, wagon moved or rotated, joint occupied,
  reservation lost, no reverse progress, acknowledged-but-absent attach and a moving
  tractor after attach all stop with no further action.
- `tests/test_lua_maneuver.py` (Lua 5.1 and 5.4, kinematic stub, not GIANTS
  physics): the live 8ffafcc geometry, case A (gap 12.9 m, lateral -12.4 m, 86 deg)
  lines up forward and then reverses onto the exact candidate at tractor radii
  4.5-6.5 m, case B (18.8 m, -18.0 m, 163 deg) lines up without ever reversing
  first, and both are refused before moving (a named reason, logged with the
  numbers) for a tractor turning too wide; phases LOCAL_TURN, LOCAL_APPROACH,
  LOCAL_LINEUP are logged once each with joints, gap, errors, steering, direction,
  speed and time; read-only steering samples (`STEER`: commanded curvature,
  rotatedTime before/after each driveToPoint, target rot time and limits, wheel
  angles, articulated axis, node yaws) change no drive command, show a command
  the wheels do not follow and another writer resetting the steering, and log
  unreadable state as nil; the stub steers only an active tractor (GIANTS'
  WheelPhysics rule), and both motions hold `forceIsActive` while driving and
  release it on every end, never clearing one someone else held; lateral offset (forward correction), steering to either side and
  60 random starts all end LINED_UP inside the reverse's envelope or are refused
  unmoved, never leaving the 25 m radius or entering the wagon's box; refusals
  (unknown or out-of-range turning radius, outside the area, beside the wagon,
  limits, joints, seated); mid-maneuver takeover, rider getting in, other AI,
  lost reservation, wagon joint or body moved, unreadable geometry, leaving the
  radius, stall, divergence (a tractor that does not turn, or turns wider than it
  reports), timeout and abort all brake, latch and log; a curve past another
  vehicle or the player is refused unmoved (attached implements and far vehicles
  ignored, an unreadable player refuses), and another vehicle moving or driving
  close, or the player walking up, stops it; a start just beside the wagon may
  still drive away; a rounding-zero turn is never a full circle; a curve end inside
  the reverse's envelope is LINED_UP, not missed.
- `tests/test_lua_hitch.py` (Lua 5.1 and 5.4): GIANTS GOTO start/cancel sequence;
  ALIGN_REVERSE refuses unsafe limits and joints, reverses only, stops the instant
  GIANTS reports the exact pair, and fails closed on timeout, distance limit, stall,
  wagon moved/attached, lost reservation, farm change, occupied tractor, any AI
  job (never cancelled), other candidate (implement or joint), vertical mismatch,
  unreadable state and missing motor; every terminal path brakes and then turns
  cruise control off, and never resumes; speed is bounded 1–5 km/h (default 3);
  the Farmhand assist radius is passed only to Farmhand's candidate call (GIANTS'
  constant unchanged, never smaller than it) and still accepts only the exact
  pair; the vertical guard applies at any radius; ATTACH refuses unless exact,
  stopped, reserved, unoccupied, AI-free and allowed, calls GIANTS once; an
  acknowledged attach, or one on other joints, is not success.
- `tests/test_bridge.py`: strict command framing (only five operations, exact typed
  arguments, session-bound ids); results from other sessions ignored; ACCEPTED is
  never completion (unread file → NOT_ACCEPTED, read but silent → unverified,
  REJECTED, no terminal → RESULT_TIMEOUT, silent game → LINK_LOST); capability only
  with this companion's live session (an unlinked game blocks before reservation);
  stale snapshots never read as current; the full AC-001 executor over
  `FS25World` against a fake game: near tractor skips AutoDrive pickup, far tractor
  drives first, without AutoDrive the same run uses `navigate` for pickup and
  delivery (stalled, unknown or misplaced GOTO ends stop and fail), explicit
  targets resolve before any command, every step failure
  stops with its own reason and sends nothing further, AutoDrive "ended" away from
  the destination is not arrival, an unconfirmed stop keeps the reservation, moving
  equipment is never released, observation trouble before RESERVE only blocks, a
  replayed request sends no command, and wrong
  reservations never reach the game; atomic command file; Windows pipe server and
  a second companion refused (on Windows CI).
- `ServiceTests` (`tests/test_bridge.py`): a key press runs AC-001 "here" through
  the fake game and the player is told ACCEPTED, the milestones WORKING_STAGE and
  WORKING_DELIVER, then COMPLETE; while a job runs a
  second fetch is BUSY, "what are you doing?" answers STATUS_WORKING_<step> at once
  and "STOP!" pre-empts the running operation (nothing after it), stops, releases
  and reports CANCELLED, after which status reports the last job; replayed or other
  sessions' requests never run; unknown words are NOT_UNDERSTOOD and move nothing,
  stop with nothing running is NOTHING_TO_STOP; typed words fetch the pair; a
  reloaded game restarting its request ids gets fresh work, never a cached result;
  after a fetch, "come here" and "take this back to the barn" move the same pair
  (to the walked-off player, to 15 m short of the barn) and an unknown place
  blocks;
  failing session-file writes never stop the companion; BLOCKED and FAILED results are
  reported with their reason; a reservation kept after an unconfirmed stop is
  released only by a later confirmed stop (DONE and observed at rest; a DONE with
  a moving snapshot keeps it), and the run then completes; a session the game dropped is renewed and a
  request on an unlinked game blocks; notices are strict tokens; a closed link
  never rewrites its session file; the logon command is windowless pythonw rooted
  at the repository and refuses quoted paths.
- `tests/test_lua_bridge.py` (Lua 5.1 and 5.4): the game bridge is inert without a
  session; adoption opens the pipe and stamps snapshots; AutoDrive drive reports
  ACCEPTED, then DONE only once ended and at rest, or AD_NOT_STARTED,
  AD_STOPPED_WITH_ERROR, OCCUPIED (AD stopped); GIANTS GOTO `navigate` reports
  ACCEPTED, then DONE only once the job ended and the tractor is at rest, outlives
  any stage bound while progressing, is stopped and cancelled after 90 s without
  2 m of progress, and fails on GOTO_NOT_STARTED, OCCUPIED, unreadable position,
  AI already active or a moving tractor; unknown marker/vehicle never move;
  stale sessions are rejected and replayed or lower ids never run; one command at a
  time, a stop pre-empts and is DONE only at rest (else NOT_STOPPED); a lost
  heartbeat or a new session stops the old session's motion and is never
  re-adopted; stop cancels only a GOTO it saw running; attach needs the align that
  found the candidate; the probe cannot start during a companion session; drive
  refuses while AutoDrive's start helper would hand the tractor to another AI;
  stop cancels a GOTO it started (and one that starts late inside the start
  window, also after a dropped session), never AI appearing after that window. The
  fetch key needs a linked companion (else a critical "not running" notice), sends
  one schema-valid request per press (2 s cooldown) and never moves anything; the
  stop key always sends; the talk key opens GIANTS' text dialog and sends the
  cleaned words (cancelled or empty dialogs send nothing; text cut on a UTF-8
  boundary; invalid text dropped); companion notices are shown once each, never one
  from before adoption, failures as critical; the three keys are hooked into
  GIANTS' global player actions (originals kept, flags as Courseplay), removed on
  unload, a missing action disables only its key, and they equal modDesc's actions.
- `tests/test_lua_probe.py` (Lua 5.1 and 5.4): the diagnostic console probe loads
  as mod globals in modDesc order and registers its three commands only in
  `loadMap`; nothing runs untyped; `fhHitchList` is read-only; one attempt at a
  time; bad arguments or unsafe vehicles never move anything; staging failure never
  reaches ALIGN; ALIGN failure never reaches ATTACH; only its own observed GOTO is
  ever cancelled; PASS needs a fresh exact-pair, exact-joint observation; its plan
  equals `companion/hitch.py` on random inputs, and mirrored constants match.
- `test_false_observations`: false attach/arrival acknowledgements, moving vehicles,
  wrong attachment, wagon left behind and lost attachment never complete.
- `test_stop_failure_retains_reservation`: uncertain cleanup remains UNVERIFIED.
- `test_here_is_snapshot`: player movement does not change accepted destination.
- `test_destination_is_nearest_routable_marker`, `test_destination_tie_break_is_order_independent`,
  `test_no_marker_near_requester_delivers_by_giants_goto`, `test_unknown_destination_fails_closed`:
  "here" resolves deterministically to a valid marker in range, else to the GOTO
  stand-off pose; a marker that vanishes fails closed.
- `GiantsNavigationTests`: without AutoDrive (random requester positions) "here" is
  delivered by one GOTO to the stand-off pose, facing the requester, decided before
  RESERVE; a far tractor without a marker near the wagon travels by GOTO to the
  staging pose; a GOTO travel or delivery that ends elsewhere, fails, times out,
  falsely reports arrival or leaves the tractor moving stops and fails closed;
  explicit markers without AutoDrive block before reservation; AutoDrive is used
  whenever its marker fits; the mock refuses unreserved or invalid GOTO.
- `test_multi_joint_tractor_matches_any_shared_joint`: joint types are compared
  per joint pair across all of a tractor's rear joints; the matching one is used.
- `test_attachment_through_another_joint_is_not_verified`: both ends naming each
  other is not enough; the planned tractor joint must be the one in use.
- `ExplicitDestinationTests`: explicit markers by ID/name, field → entrance via the
  map-specific table; not-found, ambiguous, invalid, unmapped and unknown-map targets
  block before reservation with no nearby substitution; arrival is verified at the
  resolved marker; field targets perform only delivery steps; work operations,
  coordinates and mixed selectors are rejected by the schema.
- `test_replay`: same ID+intent does not execute twice; changed payload is blocked.
- `CancellationTests`: a cancel before start reserves and moves nothing; a cancel
  between steps stops before the next one and releases (CANCELLED); a step the
  requester's stop pre-empted is CANCELLED, not FAILED; a cancelled pair that cannot
  be stopped stays reserved (UNVERIFIED); states are reported as entered.
- `MovePairTests` (AC-002): "come here" moves the hitched pair nearest the requester
  (random IDs and order) and leaves a far pair and an unhitched pair untouched;
  nothing hitched, occupied, busy, moving, one-sided, incapable or foreign blocks
  before reservation; a named building is reached by GOTO 15 m short, facing it;
  an exact marker wins with AutoDrive, the building without; ambiguous, unknown,
  partial-word or AutoDrive-only places block; fetch can deliver to a building;
  an unobserved arrival is UNVERIFIED and a cancel before travel moves nothing;
  reservations accept only unattached units or a pair hitched to each other.
- Hitch travel and local alignment (5304adc live): `AlignEnvelopeTests` (joint error in the
  wagon frame for any heading; the staging aim inside the envelope; each reason),
  `test_goto_travel_or_delivery_not_observed_stops_and_fails_closed` (coarse travel 4 m off
  continues, 100 m off TRAVEL_ENDED_TOO_FAR), `test_a_stage_end_outside_the_reverse_envelope_never_reverses`,
  the bridge failure table (STALLED, WAGON_MOVED, DRIVER_TOOK_OVER, AI_ACTIVE, DIVERGED;
  ATTACH_TIMEOUT and the wrong joint stay UNVERIFIED), `test_a_failed_local_alignment_logs_the_joints`,
  Lua `test_steering_follows_the_wagon_axis_from_the_observed_joints`, the DIVERGED guard,
  `test_the_end_of_a_reverse_describes_its_progress`,
  `test_an_ended_goto_logs_why_giants_ended_it_and_is_still_only_ended`.
- First live session (2026-10-17), as regressions: `test_live_case_1_the_requester_rides_along`
  (tractor seated: the fetch completes), `test_live_case_2_stage_end_is_judged_by_the_joints_not_the_pose`
  (GOTO is long-range only: the 8ffafcc ends A and B and the 5304adc end 14 m /
  138 degrees off are lined up by LOCAL_MANEUVER and hitch; ends inside the envelope
  reverse directly; an end beyond 25 m is FAILED TRAVEL_ENDED_TOO_FAR with nothing
  more moved; a refused or diverging maneuver, or one whose end is off the axis,
  never reverses; all logged, stopped, released),
  `test_a_tractor_starting_inside_the_work_radius_never_uses_goto`,
  `test_live_case_3_lingering_ai_is_proven_then_the_tractor_is_reusable` (STAGE ends,
  STOP, AI still active: CLEANUP_AI_NOT_CLEARED and kept reserved; the next request is
  NO_ELIGIBLE_PAIR with "candidate ... rejected: RESERVED, AI_ACTIVE" logged; once idle
  it is recovered and completes), `test_live_session_language_cases`, the ride-along
  Lua tests (seated: every AI-driven op runs; a takeover yields, also between
  steps; an unreadable seat fails closed; the last-metres reverse needs an empty cab;
  another mod's control function is never replaced), the bridge's
  REJECTED_SEATED_DURING_REVERSE case (stop, released),
  `test_equipment_is_never_a_destination_and_statements_are_not_orders`, `RideAlongTests` (the ridden tractor first) and `HitchOnlyTests`
  (hitch_pair hitches in place, delivers nothing, a failed hitch is never COMPLETE);
  `test_a_slow_cold_load_is_not_counted_as_wrong_answers` (Ollama loads the model
  before the unchanged validation).
- `tests/test_language.py`: the deterministic rules map many phrasings (address
  words, punctuation, case) to fetch_pair, move_pair ("come here", "take this back
  to the barn"; the canonical AC-001 sentence with "hook them together" stays a
  fetch), stop and status, extract a marker or
  field destination only when named (a marker called "bus stop" is not a stop), and
  return nothing for anything else; STOP is decided by the rules before any model,
  and a failing model never breaks text input; the command schema admits no IDs,
  farm, coordinates or unknown commands.
- `tests/test_voice.py`: each generated fixture (`tests/fixtures/voice`) goes through
  the microphone abstraction and becomes the expected command, with the farm's
  place names offered to the recogniser; a missing microphone or speech model
  disables only voice, and a microphone plugged in later is found on the next
  press; too-short, failing, empty or crashing audio is NOT_HEARD; holding never
  starts a second recording and a lost release still ends at 15 s; only spoken
  requests get spoken replies; speech text travels on stdin only; the speech model
  files are pinned and verified; `RealSpeechToTextTests` (CI job "voice", or set
  FARMHAND_WHISPER_DIR) transcribes the fixtures with faster-whisper and hears
  nothing in silence. `ServiceTests`: push-to-talk fetches the pair with spoken
  replies; without voice set up it is VOICE_UNAVAILABLE and typing works. Lua:
  the hold key is registered as GIANTS' CROUCH and sends only the edges.
- `tests/test_ai.py` (fake Ollama): no choice until the user makes one, remembered,
  a broken settings file asks again; the list shows Ollama models, installed
  Farmhand models, the downloads not yet installed and "Continue without AI"
  (labels at most 60 characters), and only downloads or no AI without Ollama;
  requests carry the schema, temperature 0, `think: false` and nothing about GPU
  layers, an older Ollama rejecting `think` still works, a vanished model is
  reported and malformed output is not understood; validation passes 6/6, fails at
  4/6, on an unsupported sentence turned into movement, when too slow and when the
  model is missing; picking no AI saves it and switches AI off; a validated pick is
  saved and used; a failing pick is never saved, the list returns and the earlier
  working model stays; a download is installed then validated; a failed download
  changes nothing; a saved model that disappears is reported and never replaced by
  another; stale or out-of-range picks do nothing; a candidate model is never used
  before it passed (the working one answers meanwhile); "Continue without AI"
  during a download wins and another pick while busy is reported; the menu never
  exceeds nine lines; a saved model missing at start is kept and shown at the
  first link. Service: a model answer that arrives after a STOP (or a new
  session) is void. Setup: a pid file left by a crash does not block it (Windows),
  it waits for the old companion process to exit, retries locked removals, and
  starts the companion in its install folder. `ServiceTests`: the menu is
  offered once at first link, stale and cancelled picks are ignored, a pick is
  validated and saved, words and the key reopen it; with AI off unknown words are
  answered at once and the rules still fetch. Lua: the MENU notice opens GIANTS'
  option dialog with the companion's lines, a pick sends (menu, line), cancel and
  out-of-range send nothing, never over another dialog, the key asks for it.
- `tests/test_setup.py`: Setup installs the mod and the companion and registers the
  windowless logon start; an update replaces the program and keeps settings and
  downloaded models (and leaves no stale files); a missing FS25 profile or an
  incomplete package changes nothing; a running companion is asked to quit first,
  and one that does not quit stops the install; uninstall removes everything it
  installed; quoted paths are refused. `WindowsInstallTests` (CI job "package"):
  the built package installs into a temporary profile, the companion starts (pid
  file, log), the logon entry points at the installed runtime, a second install
  updates over the running companion, and uninstall stops it and removes the mod
  and the entry. The AI menu also offers the speech model while it is missing.
  "report" (typed) saves one zip with the companion logs, the game log's
  [Farmhand]/error lines, settings and a fresh snapshot.
- `tests/test_model.py` (fake llama-server, no model): requests are local,
  temperature 0 and schema-constrained with the schema described in the prompt;
  answers map to validated proposals; malformed, off-schema, extra-field, too-long,
  control-character, empty, HTML and error replies, a slow or unreachable server and
  an unready one are all "not understood"; rules answer first and STOP never
  reaches the model; the server command is CPU-only (-ngl 0, no device), local,
  one slot, at most 4 threads; not installed means rules only; downloads are
  verified (size, sha256) before use and never left half-written; archive entries
  cannot escape the install folder; the pinned artifacts are the verified ones.
- `test_schema_rejects_bad_intent`: malformed intent yields BLOCKED before action.
- `tests/test_ipc.py`: untrusted frame decoding (chunking, oversized lines, NaN,
  duplicate keys, nesting that exhausts the decoder followed by valid frames,
  schema) and fail-closed FS25 snapshot mapping: unknown occupancy/AI/attachment is
  unavailable and never free, observed false and observed-empty stay valid,
  attachment claims by either vehicle are honoured, tractors are recognised with
  no configuration (self-propelled, motor, PTO output, readable non-loader store
  category; the optional map still overrides), and movement capability needs
  GIANTS GOTO support plus the live bridge, never AutoDrive (which only adds the
  `autodrive` capability).
- `tests/test_lua_contract.py` (Lua 5.1 and 5.4): `mod/FarmhandObserve.lua` against
  stubs implementing only upstream-evidenced calls, decoded and mapped by Python,
  including every method missing / returning nil / non-boolean / raising, and
  GIANTS GOTO availability by its own rule (agent functions, start allowed, job
  supported under the name `AIJobGoTo`). AutoDrive's scratch `ad = {}` on a towed
  wagon (no state module) is no AutoDrive driver, so a detached wagon is eligible
  again; an unreadable state module, or one missing on any other vehicle, stays
  AI_STATE_UNKNOWN (`test_autodrive_scratch_table_on_a_towed_wagon_is_not_an_autodrive_driver`).

- `tests/test_livetest.py`: the development-only live-test bridges. A hitch request
  runs the real serve() loop, coordinator and executor over FakeGame and sends only
  the production ops (stage, maneuver, align, attach, stop); STOP cancels it through
  the production stop; malformed, unknown, foreign-session, stale, replayed,
  oversized, duplicate-key and non-finite requests are refused with nothing sent;
  the host runner refuses every op outside its allowlist, every argument naming a
  path or program, links out of the checkout, and any build not exactly the
  checkout's; the sandbox client → runner → companion → executor path ends with the
  test's status, reason, states, joints and attach lines. One runner per checkout:
  a second refuses to start (lock or live heartbeat) and removes nothing, a
  superseded runner stops, a request racing two runners is answered once, and the
  client takes no answer that is not its runner's for its request and op
  (`HostRunnerSingleInstanceTests`, `ClientAnswerIdentityTests`). `stop-fs25` takes no
  argument, ends only FarmingSimulator2025Game.exe (asked, then forced), and never
  under a running job or test.

Synthetic movement sets coordinates and coupling sets relations. The simulator
has no physics, terrain, route graph, collision, trigger, animation, network or
actual GIANTS API. Failure injections test orchestration only. The actual game
checks in BUILD_SPEC.md remain mandatory before a production claim.
