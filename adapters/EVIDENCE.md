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
| MA | stijnwop/manualAttach @ `6e1536f576e1` (2026-09-13) | 1.1.1.0 | GPL-3.0 | Evidence of the attach-range rule's shape only; no code copied |
| GDN | GIANTS FS25 LUADOC, Script v1.20.0.0 ([index](https://gdn.giants-software.com/documentation_scripting_fs25.php?version=script)) | FS25 1.20 | proprietary docs | Authoritative game-script source, read via the official pages |

GDN class pages cited below: `AIDrivable` (`&category=78&class=619`), `PowerConsumer`
(`&class=778`), `PowerTakeOffs` (`&class=779`), `ForageWagon` (`&class=676`),
`Motorized` (`&class=698`), `VehicleMotor` (`&category=91&class=896`),
`VehicleConfigurationItemMotor` (`&category=15&class=178`), `AIJobVehicle`
(`&class=622`), `Attachable` (`&class=628`), `AttacherJoints` (`&class=630`),
`AIJobGoTo` (`&category=54&class=539`), `AITaskDriveTo` (`&category=81&class=846`),
`AIJobTypeManager` (`&category=3&class=153`), `AISystem` (`&category=3&class=154`),
`AIVehicleUtil` (`&category=91&class=881`), `MathUtil` (`&category=90&class=871`).
`AIDrivable` (`&category=78&class=619`), `AIJobGoTo`, `AITaskDriveTo` and
`AIJobVehicle` were re-read on 2026-10-17 for GIANTS-only travel (`navigate`).
The GDN pages are long and the official renderer truncates late, alphabetically
sorted functions; where a body could not be read it is marked "index only", and the
call shape is taken from GIANTS' own callers or from AD/CP usage instead.

No upstream code is vendored or copied. `mod/FarmhandObserve.lua` and
`mod/FarmhandHitch.lua` are original code that call the same public game/mod
functions; FH's MIT notice is not required because nothing of FH is reproduced,
but FH is credited here for the IPC design. CP and MA are GPL-3.0 and were read
for API usage only.

## Observation (read-only) — implemented in `mod/FarmhandObserve.lua`

| Capability | State | Source | Maps to |
| --- | --- | --- | --- |
| Enumerate vehicles | PROVEN | FH `scripts/Collectors.lua:323-324` `g_currentMission.vehicleSystem.vehicles`, fallback `g_currentMission.vehicles` | snapshot `vehicles` |
| Farm ownership | PROVEN | FH `Collectors.lua:332` `v.ownerFarmId == farmId`; AD `scripts/Specialization.lua:395-396` `getOwnerFarmId()` | `Equipment.farm_id` |
| Requester farm | PROVEN | FH `scripts/Farmhand.lua:245` `g_currentMission:getFarmId()` | snapshot `farm_id` → trusted session farm |
| Opaque stable ID | PROVEN | FH `Collectors.lua:400` `NetworkUtil.getObjectId(v)` (inverse `NetworkUtil.getObject`, FH `Commands.lua:1389`) | `Equipment.id` (stringified) |
| Category: tractor | PROVEN call / UNVERIFIED names | FH `Collectors.lua:97-106` `g_storeManager:getItemByXMLFilename(v.configFileName).categoryName`, noted there as the only reliable class signal (`typeName` calls wheel loaders "tractor") | category map → `tractor` only; **no store category names are proven**, so the map ships empty |
| Category: forage wagon | PROVEN | GIANTS `ForageWagon` specialization ("Specialization for a forage wagon"; `onLoad` line 93 `self.spec_forageWagon`); Courseplay identifies forage wagons the same way (`ForageWagonController.lua:10`, `FillLevelUtil.lua:132`) | `forage_wagon` = `spec_forageWagon` on an attachable (`spec_attachable`) vehicle; no store category needed. A store category mapping such a vehicle to `tractor` is contradictory → unknown |
| Position | PROVEN | FH `Collectors.lua:465` `getWorldTranslation(v.rootNode)` | `Equipment.position` (x, z) |
| Player position | PROVEN | FH `Collectors.lua:592-598` `g_localPlayer:getPosition()` else `getWorldTranslation(p.rootNode)` | requester position |
| Occupied | PROVEN (SP) | AD `scripts/Utils/AutoDriveUtilFuncs.lua:770-786` `getIsEntered() or getIsControlled()`; AD notes this differs on dedicated servers | `Equipment.occupied` |
| Busy (any AI) | PROVEN | FH `Collectors.lua:412` `getIsAIActive()`; AD overrides it to include AutoDrive driving, `Specialization.lua:1794-1800` | `Equipment.busy` |
| AutoDrive present / active | PROVEN | AD `ExternalInterface.lua:116` `vehicle.ad`, `scripts/Modules/StateModule.lua:677` `isActive()` | `has_autodrive`, `ad_active` → busy |
| Attached implements | PROVEN | FH `Collectors.lua:269-274` `getAttachedImplements()` → `imp.object`; AD `Specialization.lua:1795` `getAttacherVehicle()` | `Equipment.attached_to`; claims by either end are merged; >1 link → `*multiple*`; unobserved → `*unknown*` |
| Map identity | PROVEN | CP `scripts/field/CustomFieldManager.lua:284` keys per-map field data on `g_currentMission.missionInfo.mapId` | snapshot `map_id` → `World.map_id()` |
| Moving / stopped | PROVEN call / UNVERIFIED threshold | FH `Collectors.lua:204`, AD `DrivePathModule.lua:222` `getLastSpeed()` (km/h); AD treats `lastSpeedReal*3600 < 1` as not moving, `TrailerModule.lua:502` | `stopped` = \|speed\| < 1 km/h |
| Hitch joint types | PROVEN data / UNVERIFIED rule | CP `ai/controllers/AttacherJointController.lua:11-12` `getAttacherJoints()[i].jointType`; CP `AttachableController.lua:13` `getInputAttacherJoints()`; names via `AttacherJoints.JOINTTYPE_*`, AD `AutoDriveUtilFuncs.lua:953-955` | Per-joint `type` on `OutputJoint`/`InputJoint` only (no separate name lists); the plan pairs joints of equal type. GIANTS' own candidate search and `isAttachAllowed` remain the action-time authority |
| GIANTS GOTO available | PROVEN rule / UNVERIFIED job name | GDN `AIJobGoTo:getIsAvailableForVehicle` 92-102: `vehicle.createAgent ~= nil and vehicle.setAITarget ~= nil and vehicle:getCanStartAIVehicle()` and `getIsAIJobSupported(className)`. `AIJobVehicle:getCanStartAIVehicle` 488-523 (false while any AI runs, at the helper limit, for a broken vehicle or without AI support); `getIsAIJobSupported` 528-541 returns true when the vehicle lists no `supportedJobTypes`, else compares names case-insensitively. We pass `"AIJobGoTo"`, assumed to be what `ClassUtil.getClassNameByObject` returns for the job (UNVERIFIED; it only matters for vehicles that list their supported jobs) | `goto_supported` → `move`/`attach`/`stop` capability with the live bridge; unknown is null and grants nothing |
| AutoDrive destinations | PROVEN | AD `scripts/ExternalInterface.lua:84-94` `AutoDrive:GetAvailableDestinations()` → `{name,x,y,z,id}` keyed by marker ID | `Destination(id, position, name)` |
| Marker ID stability | PROVEN negative | IDs are array indices (`Manager/GraphManager.lua:365-371`) and are renumbered when a marker is deleted (`:496-499`) | A marker ID is valid only for a live selection; stored config refers to marker **names** |
| Field → marker association | **GAP** | AD markers hold only `name` and a UI `group` (`GraphManager.lua:371`); AD field IDs (`Utils/FieldPointGenerator.lua:40-50`) are not linked to markers | Field targets resolve only via trusted local `field_entrances[map_id][field] = marker name`; no nearest-marker guess |
| Version detection | PROVEN | AD `scripts/AutoDrive.lua:2` `AutoDrive.version`; AD `register.lua:153` `g_modManager:getModByName(g_currentModName).version` | `hello` frame |
| Power sufficiency | PROVEN | GIANTS' own gate is `PowerConsumer:getCanBeTurnedOn` 271-294: refuse when `getConsumedPtoTorque(true)` (+ other implements, 277) `/ rootMotor:getPtoMotorRpmRatio()` exceeds `0.9 * rootMotor:getPeakTorque()` (279-282). `getConsumedPtoTorque` 236-260 is `neededPtoPower / (ptoRpm*pi/30)`, 0 when `ptoRpm <= 0.001`; `neededMin/MaxPtoPower` are "in kW at ptoRpm" (`loadPowerSetup` 197-198) for the vehicle's actual `powerConsumer` configuration (`onLoad` 111-129) | Same rule as two numbers: wagon `required_pto_torque` = `getConsumedPtoTorque(true, true)`; tractor `pto_torque_capacity` = `0.9 * getPeakTorque() * getPtoMotorRpmRatio()` of the **loaded** motor (actual configuration). GIANTS units on both sides, so no hp/kW conversion. Other implements' load is 0 because only unattached tractors are selected. No PowerConsumer → required 0 (GIANTS sets no condition) |
| Power value semantics | PROVEN detail / UNVERIFIED in game | `getConsumedPtoTorque` uses the current consuming load (`ForageWagon:getConsumingLoad` 396-403), i.e. the value GIANTS uses at turn-on; `VehicleMotor:getPeakTorque` and `getPtoMotorRpmRatio` bodies are index only (page truncated) but GIANTS calls them at 279-282 | Sufficiency means "GIANTS would let it switch on now", not full-load performance |
| Shop power numbers | PROVEN, not used | `VehicleConfigurationItemMotor:loadFromXML` line 31 `self.power = #hp`, registered as "Horse power to be shown in the shop" (43); `PowerConsumer.getSpecValueNeededPower` 529-556 formats `storeItem.specs.neededPower` (base or per-configuration, `loadSpecValueNeededPower` 500-520) for display; `Motorized.getSpecValuePower` body index only | Display values, not GIANTS' power gate; comparing shop hp with PTO kW would invent a model |
| PTO compatibility | PROVEN | `PowerTakeOffs` ("both output (e.g. tractor) and input (e.g. tool)"): `attachPowerTakeOff` 747-766 connects each output of `getOutputPowerTakeOffsByJointDescIndex(jointDescIndex)` (749) to `getInputPowerTakeOffsByJointDescIndexAndName(inputJointDescIndex, output.ptoName)` (753); missing input method → nothing connected (752); same check in Manual Attach `PowerTakeOffExtension.lua:20-48` | Per-joint `pto_names` (tractor outputs; implement inputs probed with the farm's output names). The plan requires a shared name at the planned joints when `required_pto_torque > 0`; unreadable → unknown → no plan |
| PTO connected by normal attach | UNVERIFIED (strong indirect) | Manual Attach's `onPostAttach` detaches the PTO right after a normal attach (`ManualAttachPowerTakeOff.lua:187-197`); `PowerTakeOffs:onPostAttachImplement` body is index only | Not needed for selection or towing. With Manual Attach installed the PTO stays disconnected and its `getCanBeTurnedOn` override (line 34) blocks switching on |
| Yaw convention | PROVEN | GDN `MathUtil` `eulerToDirection` line 246 (`x = sin(yaw)`, `z = cos(yaw)`), `directionToPitchYaw` line 257 (`atan2(dx, dz)`); AD `ExternalInterface.lua:23` uses the same | `Pose.yaw`, `Pose.to_world` |
| Heading | PROVEN call / UNVERIFIED meaning | `localDirectionToWorld(rootNode, 0, 0, 1)` → `MathUtil.getYRotationFromDirection` (GDN, index only); a towed implement's root forward is the approach heading in CP `AIDriveStrategyAttachHeader.lua:228-230` | `Equipment.heading`. That every forage wagon's root faces its drawbar is UNVERIFIED |
| AI root pose | PROVEN | GDN `AIDrivable:getAIRootNode` 515-516 returns `components[1].node` | `Equipment.ai_root` |
| Tractor joint offsets | PROVEN | GDN `AIDrivable:onPostLoad` 186-196: for each `getAttacherJoints()` joint, `localToLocal(attacherJoint.jointTransform, aiRootNode, 0, 0, 0)` and direction vectors into `spec.attacherJointOffsets` | `OutputJoint.x/z`, read from GIANTS' own table, not recomputed |
| Joint free | PROVEN | GDN `AttacherJoints` 1910 (`attacherJoints[i].jointIndex == 0` before attaching) and 3063 (`jointIndex ~= 0` = in use) | `OutputJoint.free` |
| Joint desc index | UNVERIFIED (runtime-checked) | `attachImplementFromInfo` indexes `spec_attacherJoints.attacherJoints[info.attacherVehicleJointDescIndex]` (GDN 1909-1910); `getAttacherJoints`/`getAttacherJointByJointDescIndex` bodies are index only | Index = list position; the mod checks `getAttacherJointByJointDescIndex(i) == joint` and reports no joints on mismatch |
| Wagon input joints | PROVEN | GDN `Attachable:getInputAttacherJoints` 1222-1223 (`spec_attachable.inputAttacherJoints`); joint `.node`, `.jointType` in CP `AttachableController.lua:13-25` and MA `VehicleAttachmentHandler.lua:494-533`; `getIsInputAttacherActive` GDN `Attachable` 1458-1459 (default `true`) | `InputJoint` at `getWorldTranslation(joint.node)`; unreadable activity → inactive |

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

## Physical operations — through the AC-001 command bridge

Every physical step is one bridge operation (`mod/FarmhandBridge.lua`: `drive`,
`navigate`, `stage`, `align`, `attach`, `stop`) sent by the companion
(`companion/bridge.py`) and executed with the primitives below. `adapters/fs25.py`
grants `move`, `attach`, `stop` only for a tractor GIANTS' GOTO job is available
for, in a snapshot that names this companion's live session, plus `autodrive`
when the tractor also has AutoDrive; otherwise no capability, so no pair is
selectable. AutoDrive is optional.
The diagnostic console probe (`mod/FarmhandProbe.lua`) drives the same operations.

| Step | State | Evidence and conclusion | Farmhand use |
| --- | --- | --- | --- |
| DELIVER and TRAVEL_NEAR: long-range travel | PROVEN API, UNVERIFIED use | AD `ExternalInterface.lua:114-161` `AutoDrive:StartDriving(vehicle, destinationID, unloadDestinationID, …)`. It routes **only to map-marker IDs**. It does nothing unless `unloadDestinationID` is a valid marker; it starts the vehicle's *current* mode, so `vehicle.ad.stateModule:setMode(AutoDrive.MODE_DRIVETO)` (`StateModule.lua:664`, `AutoDrive.lua:23`) must precede it; it is ignored if AD is already active and returns nothing. `DriveToMode:start` drives to the first marker (`DriveToMode.lua:16-33`); `startAutoDrive` refuses vehicles in an AI job (`Specialization.lua:1129-1135`) | Bridge `drive`: idle check (owned, unentered, no AI, at rest); refused (`AD_START_HELPER_ON`) while the vehicle's AutoDrive "start helper" is on, because AD's own stop would then hand the vehicle to Courseplay or a GIANTS AI job (`Specialization.lua:1251-1285`, `StateModule.lua:406-408`) — the user's setting is never changed; marker present in `GetAvailableDestinations`, `setMode(MODE_DRIVETO)`, `StartDriving(v, marker, marker)`. Then: not active within 5 s → `AD_NOT_STARTED`; active → wait (20 min bound); inactive → `AD_STOPPED_WITH_ERROR` if `vehicle.ad.isStoppingWithError` (set on AD's own error stops: `DrivePathModule.lua:67,100`, `TaskModule.lua:142`; reset by `startAutoDrive`), else `DONE AD_ENDED` once at rest. Entering the tractor or losing ownership stops AD (`stopAutoDrive`) and fails |
| DELIVER and TRAVEL_NEAR without AutoDrive | PROVEN API, UNVERIFIED use | GIANTS' own GOTO job, the same start sequence as STAGE (below). `AIJobGoTo:start` 48-58 calls `vehicle:createAgent`; `AIDrivable:createAgent` 354-397 first runs `updateAIAgentAttachments` (871-886: collects attached implements, trailer offset data) and passes that `trailerData` to `createVehicleNavigationAgent` on `aiSystem:getNavigationMap()`, so the path is planned for the coupled combination. `AITaskDriveTo:start`/`update` 88-119 prepare the implements for transport (`prepareForAIDriving` → `onAIImplementPrepareForTransport`, AIDrivable 1029) and stop the job with `AIMessageErrorCouldNotPrepare` after `PREPARE_TIMEOUT`. `AIDrivable:onUpdate` 256-349 follows the agent: `DRIVING`, `PLANNING` (brakes), `BLOCKED` (stops driving and keeps waiting, broadcasting `AIVehicleIsBlockedEvent`), `TARGET_REACHED` → `reachedAITarget` → `AITaskDriveTo:onTargetReached` 155-163 finishes, `NOT_REACHABLE` → `stopCurrentAIJob(AIMessageErrorNotReachable)` | Bridge `navigate`: idle check, GOTO start as STAGE; not running within 5 s → `GOTO_NOT_STARTED`; while running, less than 2 m of root movement in 90 s → `NAVIGATE_STALLED` (GIANTS would wait forever on `BLOCKED`); 20 min bound → `TIMEOUT`; entered/ownership lost → stop and fail; job ended → `DONE GOTO_ENDED` once at rest. Ended never means arrived: a `NOT_REACHABLE` end looks the same, so the executor verifies the AI-root pose (3 m) and the wagon (15 m) |
| Destination geometry | PROVEN constraint | AutoDrive's ExternalInterface accepts no coordinates, so explicit targets stay AD markers. GIANTS GOTO takes any position (`AIJobGoTo:setValues` 147-157). Targets: "here" = the nearest AD marker within 50 m (provisional) of the captured requester position when the selected tractor has AutoDrive, else a GOTO pose 10 m (provisional) short of that position on the line from the wagon, facing the requester; an explicit marker by live ID or exact (normalized) name (needs AutoDrive on the tractor, else BLOCKED `AUTODRIVE_UNAVAILABLE`); a field via the local map-specific entrance table. All resolve before reservation; arrival is verified at the resolved marker or pose. Whether GIANTS' agent treats the on-foot requester as an obstacle is UNVERIFIED, hence the stand-off |
| Completion signal | PROVEN negative | The `callBackFunction` stored at `ExternalInterface.lua:117-119` is never invoked anywhere in AD source. DriveTo ends via `StopAndDisableADTask` → `stopAutoDrive()` (`DriveToMode.lua:42`, `StopAndDisableADTask.lua:22`), **and** a pathfinding failure also calls `stopAutoDrive()` (`DriveToDestinationTask.lua:35-37`) without setting `isStoppingWithError`. "AD ended" therefore never means "arrived": the executor verifies arrival by position (3 m tractor, 15 m wagon), attachment and speed from a fresh snapshot. |
| Route availability pre-check | UNVERIFIED | `AutoDrive:GetPath` (`ExternalInterface.lua:7-34`) returns a path to a marker only if a waypoint lies 1–20 m from the start in a matching heading; its fallback `GetClosestPointToLocation` only searches when the graph is empty (`:99`), so it never helps. Vehicles >30 m off-network are routed by AD's async pathfinder at drive time (`DriveToDestinationTask.lua:18-21`), with no pre-check. Not implemented. |
| Stop / cancel | PROVEN API, UNVERIFIED use | `vehicle:stopAutoDrive()` registered at AD `Specialization.lua:70`, defined at `:1186`; **server only** (`:1290`). GIANTS job cancel `stopCurrentAIJob` (AIJobVehicle 431) | Bridge `stop`: aborts the running operation on that tractor — `stopAutoDrive` only for the AD drive it saw running, GOTO cancel only for the GOTO it saw running, reverse latched (brake + cruise off) — never detaches, never touches another controller's job; `DONE STOPPED` only once the tractor is observed at rest and its own motion has ended (15 s bound, else `NOT_STOPPED`) |
| AD refuses AI-job vehicles | PROVEN | `Specialization.lua:1129-1135`; consistent with treating `getIsAIActive()` as busy. |
| Reservation | Our code | No upstream concept | `FS25World` (single companion process) owns reservations of the exact pair from RESERVE to RELEASE; every command re-checks it. In game, each operation's `owned()` is "the bridge is still owned by this companion session", re-checked every frame with farm, occupancy and AI state |

### HITCH: VALIDATE → TRAVEL_NEAR → STAGE → ALIGN_REVERSE → ATTACH → VERIFY

The pattern is Courseplay's header pickup (`AIDriveStrategyAttachHeader.lua`): target
the implement's joint position and heading (228-230), pathfind to an offset pose
(244-245), make a short straight final approach (292-295), ask GIANTS whether the
joint can attach (170), attach (178), wait for the animation (185) and check the
result (297). CP does this forwards, for cutters only, with GPL code; Farmhand
re-implements only the composition, for a rear drawbar, on GIANTS primitives.

| Primitive | State | Source | Farmhand use |
| --- | --- | --- | --- |
| Hitch geometry | PROVEN API | Tractor joint offsets in the AI root frame (GIANTS `AIDrivable:onPostLoad` 186-196); wagon input joint `node` world position; yaw convention (`MathUtil`) | `companion/hitch.py` `plan_hitch`: final AI-root pose puts the tractor joint on the wagon joint, facing the wagon's heading; staging is `STAGING_DISTANCE_M` (4 m, provisional) straight ahead. Pure arithmetic, no transform engine |
| TRAVEL_NEAR | PROVEN API, UNVERIFIED use | AutoDrive `StartDriving` to a marker, or GIANTS GOTO (`navigate`) to the staging pose (rows above) | Only when the tractor is farther than 60 m (provisional) from staging: AutoDrive when the tractor has it and a marker lies within 60 m of staging, else `navigate` to the staging pose itself. AutoDrive stays long-range only; it exposes no reliable hitch-scale mechanism and its graph is not modified |
| Local pose navigation (STAGE) | PROVEN API | GDN `AIJobTypeManager` 35 registers `"GOTO"` → `AIJobGoTo`; `createJob` 92-106. `AIJobGoTo.new` 22-44 (`AITaskDriveTo`, `positionAngleParameter`), `setValues` 147-157 (`getDirectionFromYRotation(angle)` → `setTargetDirection`, `setTargetPosition`). `AITaskDriveTo:startDriving` 135-151 → `vehicle:setAITarget(task, x, y, z, dirX, 0, dirZ)`; `stop` 123-131 → `unsetAITarget()`. Start sequence `createJob → applyCurrentState → setValues → validate → aiSystem:startJob` from AD `Specialization.lua:1518-1527`. Running ⇔ `getIsAIActive()` (GDN `AIJobVehicle` 396-397). Cancel: `stopCurrentAIJob(AIMessageSuccessStoppedByUser.new())` (GDN `AIJobVehicle` 431) | `Hitch.startStaging`, `Hitch.cancelStaging`. GIANTS' navigation agent owns the collision-aware path. `AIDrivable:setAITarget`'s body is index only, so which vehicle node the target pose refers to and the arrival accuracy are UNVERIFIED; the executor observes the AI-root pose (1 m, 10°) |
| Reverse point-driving primitive (ALIGN_REVERSE) | PROVEN API | GDN `AIVehicleUtil.driveToPoint(self, dt, acceleration, allowedToDrive, moveForwards, tX, tZ, maxSpeed, doNotSteer)` line 26: local target; steering inverted under the reverser (47-48); `moveForwards=false` negates acceleration (90-91); `allowedToDrive=false` gives zero acceleration and handbrake (82-94). Local target via `worldToLocal(getAIReverserNode() or getAISteeringNode(), …)` as CP `CpAIWorker.lua:396-409` (functions registered in GDN `AIJobVehicle` 67-69). Motor/active handling as AD `DrivePathModule.lua:380-381`, `Specialization.lua:1295` | `Hitch.alignStep`: the only custom physical behaviour. Starts only with the tractor stopped and free of any AI job. 3 km/h default, bounded 1–5 km/h; ≤ staging + margin metres (cap 10), 30 s timeout (cap 60), stall after 4 s without 5 cm progress, wagon may not move 25 cm; stops on lost reservation, farm change, entered tractor, any AI job (GIANTS or AutoDrive), unknown observation, any other candidate or a vertical mismatch. Steering is GIANTS'; Farmhand only picks a target 3 m past the joint on the wagon axis, at the wagon joint's own height (no terrain query) |
| Reverse speed floor | PROVEN | GDN `AIVehicleUtil.driveToPoint` 69-73: `speedReduction = 1 - (abs(steerDiff)/maxRot)^0.25`; `if maxSpeed * speedReduction < 1` no acceleration | A fixed 2 km/h stops accelerating whenever steering is >~6% off target; 3 km/h tolerates ~20%. Below 1 km/h it never moves, so 1 km/h is the floor. The default is provisional until FS25 shows the real stall rate |
| Reverse stop lifecycle | PROVEN | `driveToPoint` writes `maxSpeed` to `getMotor():setSpeedLimit` (82) and sets cruise control ACTIVE (83-84). AD `stopAutoDrive` sets `setCruiseControlState(Drivable.CRUISECONTROL_STATE_OFF)` (`Specialization.lua:1213`); CP `stopCpDriver` the same (`CpAIWorker.lua:481`) | `Hitch.halt`, run on every terminal result and on every later call: brake via `driveToPoint(allowedToDrive=false)` with the session speed (not 0, so no zero speed limit is left), then cruise control OFF. A terminal session never drives again |
| Attach candidate / range detection | PROVEN API | CP `AttacherJointController.lua:77` `AttacherJoints.updateVehiclesInAttachRange(vehicle, MAX_ATTACH_DISTANCE_SQ, MAX_ATTACH_ANGLE, true)` → `spec_attacherJoints.attachableInfo` (44); field names from GDN `attachImplementFromInfo` 1907-1934. MA `VehicleAttachmentHandler.lua:447-533` shows the rule's shape (equal joint types, horizontal/vertical distance, angle on `attachAngleLimitAxis`, active and free input) | Not recreated. The reverse stops the instant `attachableInfo` names exactly (tractor, planned joint, wagon, planned input joint); any other candidate is terminal |
| Caller-supplied attach distance | PROVEN call shape / UNVERIFIED body | CP passes `(vehicle, maxDistanceSq, maxAngle, fullUpdate)` explicitly (`AttacherJointController.lua:77`); the GIANTS body is index only (GDN page truncated). MA's equivalent rule takes the same parameters and uses them for horizontal distance (`VehicleAttachmentHandler.lua:475,521`), vertical distance `dy² < 4·maxDistanceSq` (462, 521) and the angle as a direction-component threshold, not radians (529-536); MA itself uses a caller-specific 0.7 m (15) | **Farmhand attach-assist envelope**: `assistRadiusM` (≤ 1.5 m cap; unset = GIANTS' `MAX_ATTACH_DISTANCE_SQ`, never smaller) is passed as Farmhand's own `maxDistanceSq` to this call only. `AttacherJoints.MAX_ATTACH_DISTANCE_SQ` and `MAX_ATTACH_ANGLE` are never written, so normal play is unchanged; the angle threshold is GIANTS' own. Whether GIANTS honours the caller's distance is UNVERIFIED until the probe shows candidates appearing earlier |
| Vertical guard | Our code | The attach-range rule's vertical tolerance grows with the supplied distance (row above) | Before accepting, and again before attaching: \|y(tractor jointTransform) − y(wagon input joint node)\| ≤ `maxVerticalM` (0.5 m default, cap 1.0 m), the same two points the rule measures (MA 460, 472). Applied whatever the radius; provisional until the probe logs real `dy` |
| Smooth attach of the remaining offset | PROVEN | GDN `AttacherJoints:createAttachmentJoint` 2334-2357: the joint is created with translation/rotation limits equal to the current offset between input node and `jointTransform`, closed over `smoothAttachTime` (default `SMOOTH_ATTACH_TIME`), `attachingIsInProgress = true`; server only (2282) | GIANTS pulls the wagon over the remaining gap; no source cap on that gap exists, which is why the envelope is capped and the vertical guard exists |
| Attach operation | PROVEN API | GDN `AttacherJoints:attachImplementFromInfo` 1907-1940 → `info.attacherVehicle:attachImplement(info.attachable, info.attachableJointDescIndex, info.attacherVehicleJointDescIndex)`, returns `true`; `Attachable:isAttachAllowed` (GDN index only; call shape `isAttachAllowed(vehicle:getActiveFarm(), info.attacherVehicle)` from CP `AttacherJointController.lua:36,46`) | `Hitch.attach`: fresh exact candidate, stopped, reserved, farm match, `isAttachAllowed == true`, then one `attachImplementFromInfo` call. `true` is only "requested" |
| Attachment observation | PROVEN API | `getImplementByObject(...).attachingIsInProgress` (CP `AttacherJointController.lua:49,79`); `getAttacherVehicle` (GDN `Attachable` 1211-1212); `getAttachedImplements` (FH `Collectors.lua:269-274`) | `Hitch.attachState` waits for the smooth attach (15 s bound) and requires GIANTS' implement entry to carry exactly the planned `jointDescIndex` / `inputJointDescIndex` (GDN `createAttachmentJoint` 2279-2280), else `ATTACH_MISMATCH`. The executor's VERIFY then requires both ends from a fresh snapshot, a stopped tractor, and the planned tractor joint observed in use (`free == false`); attachment through any other joint is `UNVERIFIED` |
| Complete unattended tractor → forage-wagon composition | **UNVERIFIED until FS25 test** | — | Mock and Lua-stub tests prove orchestration and guards only, not driving, alignment or GIANTS' acceptance |

### Development probe (`mod/FarmhandProbe.lua`, `mod/modDesc.xml`)

| Item | State | Source | Use |
| --- | --- | --- | --- |
| Module visibility | PROVEN pattern | FS25 runs a mod's `sourceFile`s in one mod environment and later files use earlier files' globals (FH modDesc order `Transport, Collectors, Commands, Farmhand`; `Farmhand.lua` uses `FarmHandTransport`). A file's `return` value is not kept | Each module assigns one global (`FarmhandObserve`, `FarmhandHitch`, `FarmhandProbe`) and still returns its table for CI; the modDesc loads them in that order |
| Game globals | PROVEN | AD reads the game's global table as `getfenv(0)` (`register.lua:329`, `RoutesManager.lua:75`) | `env` for all three modules in game |
| Console commands, update | PROVEN | `addConsoleCommand(name, desc, funcName, target)` / `removeConsoleCommand(name)` (CP `ConsoleCommands.lua:40,47`); `addModEventListener` with `loadMap`/`update(dt)`/`deleteMap` (FH `Farmhand.lua:259-263, 387, 403`) | Commands are registered in `loadMap`; nothing runs until one is typed |
| Folder mod without icon | PROVEN (FH comment) | FH `modDesc.xml` comment: a folder mod loads without `iconFilename`; only a ZIP is rejected | The probe is installed as a folder, never zipped |
| `descVersion="113"` | UNVERIFIED for the user's patch | AD 3.0.1.4 and MA 1.1.1.0 use 113 | If the game reports an unknown descVersion, use the one its log names |
| Plan in Lua | Our code, diagnostic only | Same rule as `companion/hitch.py`, which stays the only production planner | `Probe.plan`; a CI test compares both on random inputs. The probe's physical steps are the bridge's own operations |

FH's `attach` command only raises the player's ATTACH input action on the vehicle the
player is in (`Commands.lua:1615`), so it cannot attach unattended and is not used.

## Local IPC — the AC-001 command bridge (implemented)

Source pattern: FH `PROTOCOL.md:14,48,157` and `scripts/Transport.lua:16,53,77,166-213`
(MIT; the design is reused, no code is copied). The FS25 Lua sandbox has no
sockets; `io.open(pipePath, "w")` on a Windows named pipe works (FH
`Transport.lua:5-7`: sustained 4 Hz in game); files can be read only through the
engine XML API (`XMLFile.loadIfExists`, `getString`), and live under
`getUserProfileAppPath() .. "modSettings/"` (`createFolder`, `deleteFile`, `fileExists`).

| Part | State | Design |
| --- | --- | --- |
| Game → companion | Implemented, UNVERIFIED on Windows/FS25 | The companion hosts `\\.\pipe\fs_farmhand_ac001` (`companion/bridge.py` `PipeServer`, Win32 `CreateNamedPipeW`/`ReadFile`); the mod writes `hello`, a `snapshot` every 0.5 s and `result` frames (`protocol/frame.schema.json`), one JSON object per line, in one single-threaded `update()`, so frames arrive in the order they were produced. `protocol/ipc.py` decodes untrusted lines |
| Companion → game | Implemented, UNVERIFIED in FS25 | `modSettings/FS25_Farmhand/command.xml`, written to a temporary name and renamed into place (retried briefly while the game holds it), one command at a time. `encode_command` allows only the five operations with exactly their typed arguments. The mod reads it every frame and deletes it: deletion is receipt only |
| Session | Implemented | `session.xml` (`id`, `beat`) is rewritten every second. The mod adopts a new id (stopping and failing anything an older session started), stamps it into every snapshot, accepts only that session's commands with strictly increasing ids (lower or repeated ids are ignored, so a re-read or replayed file never runs twice), and if the beat stops for 10 s stops this session's motion (`COMPANION_LOST`) and never re-adopts that id |
| Results | Implemented | `ACCEPTED` after validation, then exactly one of `DONE` (the game's observed end of the operation), `FAILED`, `UNVERIFIED`; `REJECTED` (stale session, unknown op/vehicle, busy). The companion: no result in 5 s → removes the file (`NOT_ACCEPTED` if it was still there, else `NO_ACCEPTANCE`, unverified); no terminal result within the operation's bound, or no frame for 9 s → `RESULT_TIMEOUT` / `LINK_LOST`, unverified. `DONE` is never completion: the executor verifies every step from a snapshot received after it |
| Capability | Implemented | `move`/`attach`/`stop` only when the snapshot's `session` equals this companion's; stale snapshots (none within 3 s) end the step as `STALE_SNAPSHOT` (unverified) and are never read as current |
| Exclusivity | Implemented | One owner of the bridge at a time: the adopted companion session or one diagnostic probe attempt; the probe refuses while a session exists |

## Rejected claims

- DeepWiki stated AutoDrive's `startPathPlanningToVehicle` is useful "for hitching a
  trailer". Source shows a 35 m chase offset behind the target; rejected.
- Farmhand's `attach` command is player-input simulation, not an unattended attach API.
- "Hitching is unavailable" (previous classification here): superseded. Each
  primitive exists in GIANTS; only their composition is unverified.
- "Tractor power, implement power need and PTO need: GAP" (previous classification
  here): wrong. GIANTS loads them on the vehicle (`PowerConsumer`, `VehicleMotor`,
  `PowerTakeOffs`), and its own turn-on rule and PTO pairing use exactly them.
- "Store motor-configuration `power` is the tractor's kW rating": rejected. It is
  shop horsepower (`#hp`, "to be shown in the shop").

## Still unresolved (first in-game checks)

1. Real store `categoryName` values for tractors (FH ships a `dumpCategories`
   diagnostic because guessed names failed). Forage wagons no longer need one.
2. That the observation calls behave as in upstream use on the user's FS25 version.
3. Live values of `getPeakTorque`, `getPtoMotorRpmRatio`, `getConsumedPtoTorque`
   and PTO names on the user's equipment, and the switch-on result GIANTS gives.
4. The HITCH composition on real equipment: observed joint transforms, the
   computed final/staging pose, GOTO arrival accuracy and which node it targets,
   GIANTS naming the exact pair as candidate, and the normal attach completing.
   See BUILD_SPEC.md for the test.
5. Whether `driveToPoint` moves an unentered tractor outside any AI job, or whether
   Drivable/Motorized per-frame control overrides it (their `onUpdate` bodies are
   past the GDN page cut-off). If overridden, the probe ends `STALLED` with the
   tractor braked; that is the evidence for the next change.
6. Whether GIANTS' `updateVehiclesInAttachRange` honours a caller distance larger
   than its constant, and whether a stale `attachableInfo` survives a call that
   finds nothing; the probe's logged gap at `CANDIDATE` per radius shows both.
7. Real `dy` at the hitch, the reverse stall rate at the chosen speed, and smooth
   attach behaviour from the larger gap.
8. Whether a forage wagon's root forward is its approach heading, and whether
   the lowest-index joint pair is the one GIANTS picks when several match.
9. AD arrival accuracy at a marker versus the 3 m / 15 m tolerances.
10. The upstream missing-method rules above, and `missionInfo.mapId` values, on the
   user's install; plus the marker names the user wants in `field_entrances`.
11. The bridge on the user's PC: the mod's `io.open` on the companion's pipe, the
   engine reading `command.xml`/`session.xml` while the companion renames them,
   whether `update()` (and so the heartbeat clock) runs while the game is paused,
   AutoDrive driving an unentered tractor with the wagon attached, and whether an
   unentered tractor stays braked between the end of ALIGN and the attach.
12. GIANTS GOTO over long distances with the wagon attached: that the agent plans
   for the combination on the user's map, its arrival accuracy against the 3 m /
   15 m tolerances, how it behaves around the on-foot requester (the 10 m stand-off
   is provisional), whether `getIsAIJobSupported("AIJobGoTo")` names the job
   correctly, and whether 90 s without 2 m of progress is a fair stall bound.
