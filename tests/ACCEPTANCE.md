# AC-001 — bring a tractor and forage wagon here

User command: “Go grab an available tractor and forage wagon, hook them together,
and bring them here.” The player says it with the in-game key (Left Alt + J on
foot); the companion turns that into this structured intent itself (no language
interpretation in this build):

```json
{"version":1,"request_id":"request-001","task":"deliver_tractor_and_forage_wagon","farm_id":"farm-a","target":"player_at_acceptance"}
```

Given a requester on farm-a and an inventory with arbitrary IDs and order,
including an available compatible tractor/wagon pair and misleading decoys:
1. FIND must select by category, same farm, availability, capabilities, power,
   hitch and PTO. Do not assert specific fixture IDs; inspect selected properties.
2. RESERVE owns both units atomically; concurrent requests cannot steal either.
3. MOVE is HITCH: a pure plan picks the joints and the final/staging poses before
   reservation; STAGE (GIANTS GOTO) and ALIGN_REVERSE (bounded, game-side) are each
   followed by a fresh check that the pair is still reserved, the wagon has not
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
  when far (and unobserved arrival stops); unplannable pairs block
  before reservation; staging not observed, wagon moved or rotated, joint occupied,
  reservation lost, no reverse progress, acknowledged-but-absent attach and a moving
  tractor after attach all stop with no further action.
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
  the fake game and the player is told ACCEPTED then COMPLETE; presses while busy
  are answered BUSY and never run, replayed or other sessions' presses never run,
  taking a request marks the link busy atomically; BLOCKED and FAILED results are
  reported with their reason; a session the game dropped is renewed and a
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
  stop cancels only a stage or navigate GOTO it saw running. The request key needs
  a linked companion (else a critical "not running" notice), sends one schema-valid
  request per press (2 s cooldown) and never moves anything; companion notices are
  shown once each, never one from before adoption, failures as critical; the key
  is hooked into GIANTS' global player actions (originals kept, flags as
  Courseplay), removed on unload, and not hooked without the input action.
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
  supported under the name `AIJobGoTo`).

Synthetic movement sets coordinates and coupling sets relations. The simulator
has no physics, terrain, route graph, collision, trigger, animation, network or
actual GIANTS API. Failure injections test orchestration only. The actual game
checks in BUILD_SPEC.md remain mandatory before a production claim.
