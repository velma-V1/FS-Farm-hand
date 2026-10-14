# AC-001 — bring a tractor and forage wagon here

User command: “Go grab an available tractor and forage wagon, hook them together,
and bring them here.” Language interpretation is a fixture in this build:

```json
{"version":1,"request_id":"request-001","task":"deliver_tractor_and_forage_wagon","farm_id":"farm-a","target":"player_at_acceptance"}
```

Given a requester on farm-a and an inventory with arbitrary IDs and order,
including an available compatible tractor/wagon pair and misleading decoys:
1. FIND must select by category, same farm, availability, capabilities, power,
   hitch and PTO. Do not assert specific fixture IDs; inspect selected properties.
2. RESERVE owns both units atomically; concurrent requests cannot steal either.
3. MOVE delegates approach; ATTACH delegates coupling; VERIFY checks both ends
   of the attachment from fresh state before any delivery movement.
4. DELIVER delegates travel to the requester's position captured at acceptance.
5. COMPLETE requires mutual attachment, tractor within 3 m, wagon within 15 m,
   both stopped, and successful reservation release. Inspect state, not just trace.

Executable mapping (`python scripts/check.py`):
- `test_selection_is_behavioral`: many deterministic random seeds vary IDs and
  order; decoys include wrong category/farm, busy, occupied, unavailable, reserved,
  attached, underpowered, wrong hitch, missing PTO and missing capabilities.
- `test_no_pair`, `test_pair_search`: no suitable pair blocks without movement;
  search considers alternative tractors and wagons.
- `test_atomic_contention`: only one of simultaneous requests owns a pair.
- `test_changed_after_reservation`: fresh eligibility prevents stale selection use.
- `test_physical_failures`: approach/attach/delivery errors and timeouts stop and
  fail; no fabricated success.
- `test_false_observations`: false attach/arrival acknowledgements, moving vehicles,
  wrong attachment, wagon left behind and lost attachment never complete.
- `test_stop_failure_retains_reservation`: uncertain cleanup remains UNVERIFIED.
- `test_here_is_snapshot`: player movement does not change accepted destination.
- `test_replay`: same ID+intent does not execute twice; changed payload is blocked.
- `test_schema_rejects_bad_intent`: malformed intent yields BLOCKED before action.
- `test_lua_boundary`: pure Lua advertises no physical capabilities and rejects
  all physical requests. This is not an upstream integration test.

Synthetic movement sets coordinates and coupling sets relations. The simulator
has no physics, terrain, route graph, collision, trigger, animation, network or
actual GIANTS API. Failure injections test orchestration only. The actual game
checks in BUILD_SPEC.md remain mandatory before a production claim.
