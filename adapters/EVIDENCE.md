# Upstream integration evidence — AC-001

Every row cites exact upstream source so another agent can re-verify it.
DeepWiki was used for discovery only; nothing below rests on it.

**States.** `PROVEN`: the pinned upstream source defines or calls this exactly as
described. That proves the API exists and how a shipped FS25 mod uses it; it does
**not** prove behaviour in this project's FS25 runtime, which has never run.
`UNVERIFIED`: plausible or partially evidenced, or our use differs from upstream's.
`GAP`: no inspected upstream source provides it.

## Pinned sources (inspected 2026-10-14)

| Key | Repository @ commit | Version | License | Use |
| --- | --- | --- | --- | --- |
| AD | Stephan-S/FS25_AutoDrive @ `93cff22f759f` (2026-10-05) | 3.0.1.4 | MIT | Delivery travel, destinations, stop |
| FH | getflowkits/fs25-farmhand-mod @ `2a67b4a5dfd8` (2026-09-28) | 2.19.8.0 | MIT | Local IPC pattern, observation calls |
| CP | Courseplay/Courseplay_FS25 @ `150dcd51f8a1` (2026-09-02) | 8.1.0.3 | GPL-3.0 | API-usage evidence only; no code copied |
| FM | DeckerMMIV/FarmSim_Mod_FollowMe @ `adc555010f90` (2025-05-21) | 1.2.0.0 | no LICENSE file | Not used |
| UAL | loki79uk/FS25_UniversalAutoload | — | — | Not inspected; loading only, not needed to tow a wagon |

No upstream code is vendored or copied. `mod/FarmhandObserve.lua` is original code
that calls the same public game/mod functions; FH's MIT notice is not required
because nothing of FH is reproduced, but FH is credited here for the IPC design.

## Observation (read-only) — implemented in `mod/FarmhandObserve.lua`

| Capability | State | Source | Maps to |
| --- | --- | --- | --- |
| Enumerate vehicles | PROVEN | FH `scripts/Collectors.lua:323-324` `g_currentMission.vehicleSystem.vehicles`, fallback `g_currentMission.vehicles` | snapshot `vehicles` |
| Farm ownership | PROVEN | FH `Collectors.lua:332` `v.ownerFarmId == farmId`; AD `scripts/Specialization.lua:395-396` `getOwnerFarmId()` | `Equipment.farm_id` |
| Requester farm | PROVEN | FH `scripts/Farmhand.lua:245` `g_currentMission:getFarmId()` | snapshot `farm_id` → trusted session farm |
| Opaque stable ID | PROVEN | FH `Collectors.lua:400` `NetworkUtil.getObjectId(v)` (inverse `NetworkUtil.getObject`, FH `Commands.lua:1389`) | `Equipment.id` (stringified) |
| Category | PROVEN call / UNVERIFIED names | FH `Collectors.lua:97-106` `g_storeManager:getItemByXMLFilename(v.configFileName).categoryName`, noted there as the only reliable class signal (`typeName` calls wheel loaders "tractor") | category map → `tractor` / `forage_wagon`; **no store category names are proven**, so the map ships empty |
| Position | PROVEN | FH `Collectors.lua:465` `getWorldTranslation(v.rootNode)` | `Equipment.position` (x, z) |
| Player position | PROVEN | FH `Collectors.lua:592-598` `g_localPlayer:getPosition()` else `getWorldTranslation(p.rootNode)` | requester position |
| Occupied | PROVEN (SP) | AD `scripts/Utils/AutoDriveUtilFuncs.lua:770-786` `getIsEntered() or getIsControlled()`; AD notes this differs on dedicated servers | `Equipment.occupied` |
| Busy (any AI) | PROVEN | FH `Collectors.lua:412` `getIsAIActive()`; AD overrides it to include AutoDrive driving, `Specialization.lua:1794-1800` | `Equipment.busy` |
| AutoDrive present / active | PROVEN | AD `ExternalInterface.lua:116` `vehicle.ad`, `scripts/Modules/StateModule.lua:677` `isActive()` | `has_autodrive`, `ad_active` → busy |
| Attached implements | PROVEN | FH `Collectors.lua:269-274` `getAttachedImplements()` → `imp.object`; AD `Specialization.lua:1795` `getAttacherVehicle()` | `Equipment.attached_to`; claims by either end are merged; >1 link → `*multiple*`; unobserved → `*unknown*` |
| Map identity | PROVEN | CP `scripts/field/CustomFieldManager.lua:284` keys per-map field data on `g_currentMission.missionInfo.mapId` | snapshot `map_id` → `World.map_id()` |
| Moving / stopped | PROVEN call / UNVERIFIED threshold | FH `Collectors.lua:204`, AD `DrivePathModule.lua:222` `getLastSpeed()` (km/h); AD treats `lastSpeedReal*3600 < 1` as not moving, `TrailerModule.lua:502` | `stopped` = \|speed\| < 1 km/h |
| Hitch joint types | PROVEN data / UNVERIFIED rule | CP `ai/controllers/AttacherJointController.lua:11-12` `getAttacherJoints()[i].jointType`; CP `AttachableController.lua:13` `getInputAttacherJoints()`; names via `AttacherJoints.JOINTTYPE_*`, AD `AutoDriveUtilFuncs.lua:953-955` | `Equipment.hitches`; compatible if any type is shared. The game's real attach rule is inside closed GIANTS code; `isAttachAllowed` remains the action-time authority |
| AutoDrive destinations | PROVEN | AD `scripts/ExternalInterface.lua:84-94` `AutoDrive:GetAvailableDestinations()` → `{name,x,y,z,id}` keyed by marker ID | `Destination(id, position, name)` |
| Marker ID stability | PROVEN negative | IDs are array indices (`Manager/GraphManager.lua:365-371`) and are renumbered when a marker is deleted (`:496-499`) | A marker ID is valid only for a live selection; stored config refers to marker **names** |
| Field → marker association | **GAP** | AD markers hold only `name` and a UI `group` (`GraphManager.lua:371`); AD field IDs (`Utils/FieldPointGenerator.lua:40-50`) are not linked to markers | Field targets resolve only via trusted local `field_entrances[map_id][field] = marker name`; no nearest-marker guess |

### Unknown versus not applicable

The mod reports a boolean only when the method exists and returns a real
boolean. A method that exists but errors or returns anything else is **unknown**
(JSON `null`). A **missing** method means "not applicable" only where upstream code
treats it the same way:

| Missing method | Treated as | Upstream rule |
| --- | --- | --- |
| `getIsEntered` | not entered | AD `AutoDriveUtilFuncs.lua:777,785` falls through to `return user ~= nil` (false) |
| `getIsControlled` | `getIsEntered` alone decides | AD `AutoDriveUtilFuncs.lua:778-782` |
| `getIsAIActive` | not AI-active | AD `Manager/HarvestManager.lua:104`; FH `Collectors.lua:457` |
| `getAttacherVehicle` / `getAttachedImplements` | no attachment on that side, **only if** the vehicle also lacks input / attacher joints | AD `Specialization.lua:1795`; FH `Commands.lua:340`. The joint condition is our own conservative addition |
| `vehicle.ad` | no AutoDrive | AD `ExternalInterface.lua:116`. Once `vehicle.ad` exists, an unreadable `stateModule` is unknown |

The Python mapping (`adapters/fs25.py`) turns unknown into `available=False`, and
also into `occupied=True`, `busy=True` or `attached_to="*unknown*"`, so no single
check can read it as free. Whether these upstream rules hold for every vehicle type
on the user's install is UNVERIFIED.
| Version detection | PROVEN | AD `scripts/AutoDrive.lua:2` `AutoDrive.version`; AD `register.lua:153` `g_modManager:getModByName(g_currentModName).version` | `hello` frame |
| Tractor power, implement power need, PTO need | **GAP** | Not read by any inspected source | mapped as 0 kW / PTO required → never compatible |

## Physical operations — all still fail closed (`mod/FarmhandBoundary.lua`)

| Step | State | Evidence and conclusion |
| --- | --- | --- |
| DELIVER: long-range travel | PROVEN API, UNVERIFIED use | AD `ExternalInterface.lua:114-161` `AutoDrive:StartDriving(vehicle, destinationID, unloadDestinationID, …)`. It routes **only to map-marker IDs**. It does nothing unless `unloadDestinationID` is a valid marker; it starts the vehicle's *current* mode, so `vehicle.ad.stateModule:setMode(AutoDrive.MODE_DRIVETO)` (`StateModule.lua:664`, `AutoDrive.lua:23`) must precede it; it is ignored if AD is already active and returns nothing. Intended call: `setMode(MODE_DRIVETO)` then `StartDriving(v, marker, marker)`. Not executed anywhere yet. |
| Destination geometry | PROVEN constraint | No ExternalInterface function accepts coordinates, so arbitrary points or map clicks stay unsupported. Targets: "here" = nearest AD marker within 50 m (provisional) of the captured requester position; an explicit marker by live ID or exact (normalized) name; a field via the local map-specific entrance table. All resolve before reservation; arrival is verified at the resolved marker. |
| Completion signal | PROVEN negative | The `callBackFunction` stored at `ExternalInterface.lua:117-119` is never invoked anywhere in AD source. DriveTo ends via `StopAndDisableADTask` → `stopAutoDrive()` (`DriveToMode.lua:42`, `StopAndDisableADTask.lua:22`), **and** a pathfinding failure also calls `stopAutoDrive()` (`DriveToDestinationTask.lua:35-37`). "AD inactive" therefore cannot distinguish arrival from failure: arrival must be observed by position and speed, which the executor already does. |
| Route availability pre-check | UNVERIFIED | `AutoDrive:GetPath` (`ExternalInterface.lua:7-34`) returns a path to a marker only if a waypoint lies 1–20 m from the start in a matching heading; its fallback `GetClosestPointToLocation` only searches when the graph is empty (`:99`), so it never helps. Vehicles >30 m off-network are routed by AD's async pathfinder at drive time (`DriveToDestinationTask.lua:18-21`), with no pre-check. Not implemented. |
| Stop / cancel | PROVEN API, UNVERIFIED use | `vehicle:stopAutoDrive()` registered at AD `Specialization.lua:70`, defined at `:1186`; **server only** (`:1290`). Stop must still be confirmed by observed speed. |
| AD refuses AI-job vehicles | PROVEN | `Specialization.lua:1129-1135`; consistent with treating `getIsAIActive()` as busy. |
| APPROACH + ALIGN to the wagon | **GAP** | AD's ExternalInterface cannot target a vehicle or point. AD's internal `PathFinderModule:startPathPlanningToVehicle` (`PathFinderModule.lua:335-349`) targets a point 35 m **behind** the target, facing its direction (`DriveToVehicleTask.lua:3`), used only by `CombineUnloaderMode.lua:360` — a chase position, not a reverse onto a drawbar. CP's only attach driving is for harvester headers (`AttacherJointController.lua:3`). FM follows a leader **vehicle** only (`AIJobFollowVehicle.lua:16`). No inspected mod reverses a tractor onto a towed implement's hitch. |
| ATTACH | PROVEN API, UNVERIFIED for drawbar wagons | CP `AttacherJointController.lua:44-49,77`: `AttacherJoints.updateVehiclesInAttachRange(vehicle, MAX_ATTACH_DISTANCE_SQ, MAX_ATTACH_ANGLE, true)` fills `spec_attacherJoints.attachableInfo`; then `info.attachable:isAttachAllowed(farmId, info.attacherVehicle)`; then `vehicle:attachImplementFromInfo(info)`. This is the game's normal in-range mechanism and requires prior physical alignment (the GAP above). CP uses it only for cutters. FH's `attach` command just raises the player's ATTACH input action on the vehicle the player is in (`Commands.lua:1615`), so it cannot attach unattended. |
| Observe attachment | PROVEN calls | Verify both ends from a fresh snapshot (`getAttacherVehicle`, `getAttachedImplements`), waiting while `getImplementByObject(...).attachingIsInProgress` (CP `AttacherJointController.lua:49,79`). Command acknowledgement is never evidence. |
| Reservation | Our code | No upstream concept. The companion owns reservations; a future game-side guard must re-check them before any command. |

## Local IPC decision — implemented as a protocol, not yet as a transport

Source: FH `PROTOCOL.md:14,48,157` and `scripts/Transport.lua:16,53,77,175-213`.
The FS25 Lua sandbox has no sockets; `io.open(pipePath, "w")` on a Windows named
pipe works (FH `Transport.lua:5-7` reports sustained 4 Hz in game), reads are only possible through the
engine XML API (`XMLFile.loadIfExists`), and files live under
`getUserProfileAppPath() .. "modSettings/"`.

- **Game → companion (implemented, protocol v1):** the companion hosts
  `\\.\pipe\fs_farmhand_ac001` (not FH's name: one server per name). The mod writes
  newline-delimited JSON (`hello`, `snapshot`) per `protocol/frame.schema.json`.
  `protocol/ipc.py` decodes untrusted frames (bounded lines, no NaN, no duplicate
  keys, nesting deep enough to exhaust the decoder → `TOO_DEEP`, strict schema). Hosting the pipe is Windows-only and not implemented.
- **Companion → game (design only):** an atomically renamed XML command file in
  `modSettings/<mod>/`, with a monotonically increasing ID, deleted by the mod after
  reading; deletion acknowledges receipt only. Plus a session-ID file so the mod
  reopens the pipe after a companion restart, because writes to a dead pipe do not
  reliably fail on Windows (FH `PROTOCOL.md:157`). Not built until a physical
  command can be enabled.

## Rejected claims

- DeepWiki stated AutoDrive's `startPathPlanningToVehicle` is useful "for hitching a
  trailer". Source shows a 35 m chase offset behind the target; rejected.
- Farmhand's `attach` command is player-input simulation, not an unattended attach API.

## Still unresolved (first in-game checks)

1. Real store `categoryName` values for tractors and forage wagons (FH ships a
   `dumpCategories` diagnostic because guessed names failed).
2. That the observation calls behave as in upstream use on the user's FS25 version.
3. Tractor power and implement power/PTO requirements: GAP.
4. Final approach and alignment to the wagon hitch: GAP; no navigation replacement
   will be built. Options for the user: manual pre-positioning, or a proven mod later.
5. Unattended attach of a drawbar wagon via the CP-proven API after alignment.
6. AD arrival accuracy at a marker versus the 3 m / 15 m tolerances.
7. The upstream missing-method rules above, and `missionInfo.mapId` values, on the
   user's install; plus the marker names the user wants in `field_entrances`.
