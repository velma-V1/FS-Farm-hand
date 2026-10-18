# FS25 native field-work helper: evidence report

Source: GDN LUADOC FS25, **Script v1.20.0.0**, fetched 2026-10-18 from
`https://gdn.giants-software.com/documentation_scripting_fs25.php?version=script&category=C&class=K`.
Line numbers are the ones GDN prints. "C/K" gives the category and class ids.
Caveat: GDN only shows the methods each class *documents*. Some methods (for example
`getCanStartFieldWork`, `startFieldWorker`, `stopCurrentAIJob`, `getJob`, `Field:getCenterOfFieldWorldPosition`)
show up only as registrations or as call sites, with no body. I mark those UNVERIFIED-BODY: the name
and how GIANTS calls it are proven, the implementation is not.
Nothing below has been run in FS25.

## 1. AIJobFieldWork (54/538)

**Creation.** `AIJobTypeManager:loadMapData` registers `"FIELDWORK"` → `AIJobFieldWork` (3/153 l.36), next to
`"GOTO"` (l.35). `AIJobType = self.nameToIndex` (l.33), so `AIJobType.FIELDWORK` is the index.
`createJob(typeIndex)` → `jobType.classObject.new(self.isServer)` and sets `job.jobTypeIndex` (l.92-106).
`getJobTypeIndexByName("FIELDWORK")` upper-cases the name (l.116-124).

**Parameters** (`AIJobFieldWork.new`, l.22-49): `self.vehicleParameter = AIParameterVehicle.new()` (named "vehicle"),
`self.positionAngleParameter = AIParameterPositionAngle.new(math.rad(0))` (named "positionAngle").
Tasks: `self.driveToTask = AITaskDriveTo` (task 1), then `self.fieldWorkTask = AITaskFieldWork` (task 2).
**There is no field parameter.** The field is found at run time from the vehicle position (see "Field detection" below).

**applyCurrentState(vehicle, mission, farmId, isDirectStart)** (l.134-163): calls super (`AIJob` l.418-420 sets
`self.isDirectStart`). Then `vehicleParameter:setVehicle(vehicle)`. If `not isDirectStart` and the vehicle's last job
was an `AIJobFieldWork`, it reuses that job's position and angle. It sets the snapping angle to
`max(vehicle:getDirectionSnapAngle(), pi/max(groundAngleMax+1,4))`. Otherwise the position defaults to the vehicle
rootNode position and the angle to its forward direction. It finishes with `positionAngleParameter:setPosition(x,z)` and `:setAngle(angle)`.
`AIParameterPositionAngle:setAngle` rounds to the snapping angle (65/587 l.68-77). So the angle you pass gets snapped.

**setValues()** (l.199-211): `resetTasks()`, then `setVehicle` on both tasks, and
`driveToTask:setTargetDirection/ setTargetPosition` from positionAngle.

**validate(farmId)** (l.215-224): validates **only the vehicle parameter**. `AIParameterVehicle:validate`
(65/589 l.86-99) fails if the vehicle id is nil, the vehicle is gone, or `vehicle.setAITarget == nil`.
validate does **not** check field, ownership, implement or fill. Those surface later as AI messages at run time.
There is no "AI_NO_FIELD" constant. The relevant message is `AIMessageErrorNoFieldFound`.

**Other gates (not called by startJob itself):**
- `getIsAvailableForVehicle(vehicle)` (l.167-169): `vehicle.getCanStartFieldWork and vehicle:getCanStartFieldWork()
  and vehicle:getIsAIJobSupported("AIJobFieldWork")`. `getCanStartFieldWork` is UNVERIFIED-BODY.
  `getIsAIJobSupported` (78/622 l.528-541) returns true when the XML `vehicle.ai#supportedJobTypes` is nil.
- `getIsStartable(connection)` (l.243-262) returns these errors: `START_ERROR_LIMIT_REACHED`
  (`aiSystem:getAILimitedReached()`, which is `#activeJobVehicles >= maxNumHirables`, 3/154 l.637-639), `START_ERROR_VEHICLE_DELETED`,
  `START_ERROR_NO_PERMISSION` ("hireAssistant"), and `START_ERROR_VEHICLE_IN_USE` (`vehicle:getIsInUse`). Otherwise it returns `AIJob.START_SUCCESS`.
- `AIJobVehicle:getCanStartAIVehicle()` (78/622 l.488-523) checks: disableAIVehicle, owner ≠ EVERYONE, supportsAIJobs,
  isAIStartAllowed, an AI direction node, AI limit, `not getIsAIActive()`, `not isBroken`.

**Start.** Use the same call as GOTO: `g_currentMission.aiSystem:startJob(job, farmId)` (3/154 l.540-550, server only).
It sets the job id, broadcasts `AIJobStartEvent`, then runs `startJobInternal`, which calls `job:start` and `addJob` and
publishes `MessageType.AI_JOB_STARTED, job, farmId` (l.554-559). `AIJobFieldWork:start` (l.109-118) calls
`vehicle:createAgent(helperIndex)` and `vehicle:aiJobStarted(job, helperIndex, farmId)`. `AIJob:start` picks a random
helper and sets `self.isRunning = true` (54/535 l.253-262).
`AIJob:update` (l.117-164) starts `getStartTaskIndex()`. That returns 2 (skip the drive) when `isDirectStart` or when
the vehicle is under 3 m from the target (54/538 l.90-105). Otherwise it returns 1, which drives to the target first.
`AITaskFieldWork:start` calls `vehicle:startFieldWorker()`. `stop` calls `vehicle:stopFieldWorker()`. `update` is
**empty** (81/847 l.33-64). So the field-work task never finishes on its own: the job ends only through `stopCurrentAIJob`/`stopJob`.
Cost: the job charges `0.0005`/ms × cost multiplier (54/538 l.282). It stops with `AIMessageErrorOutOfMoney` when the farm balance goes negative (54/535 l.168-187).

**Field detection** (`AIDriveStrategyFieldCourse:setAIVehicle`, 3/151 l.47-139):
`FieldCourse.findClosestField(nil,nil,nil,nil, vehicle:getAIJobFarmId(), vehicle, 2, fieldCourseSettings)` returns
`x, z, notOwned` (l.112). Then `g_fieldCourseManager:generateFieldCourseAtWorldPos(...)` (l.120). It searches from the
vehicle's position when field work starts. To target a field, put positionAngle inside that field so the drive task
leaves the vehicle there. The body of `findClosestField` is GAP.
Field detection controls course size and start position (`onFieldCourseLoadedCallback` l.143-213).

**How it ends: AI messages, with source lines**
| Message | Raised at |
|---|---|
| `AIMessageSuccessFinishedJob` (getType = `AIMessageType.OK`, 29/245 l.29-31) | `AIFieldWorker:updateAIFieldWorker` when every strategy returns `tX == nil`/NaN, i.e. the course is exhausted (78/620 l.337-340). Also `AIJob:update` when the last task finishes (54/535 l.152) |
| `AIMessageErrorFieldNotOwned` | FieldCourse getDriveData when `fieldNotOwned` (3/151 l.396-397) |
| `AIMessageErrorNoFieldFound` | no detection or course (3/151 l.400, l.211). SowingMachine water or rice mismatch (78/797 l.491,500,545) |
| `AIMessageErrorFieldNotReady` | course has 0 segments, i.e. nothing to work (3/151 l.203-204) |
| `AIMessageErrorVineyardNotSupported` | 3/151 l.158-159 |
| `stopReason or AIMessageErrorUnknown` | `vehicle:getCanAIFieldWorkerContinueWork()` returned stopAI (3/151 l.414-427) |
| `AIMessageErrorWrongSeason` | SowingMachine:getCanAIImplementContinueWork (78/797 l.853-858) |
| `AIMessageErrorOutOfFill` | SowingMachine no seeds and not helperBuySeeds (78/797 l.510-515). Sprayer no spray fill type (78/800 l.415-418) |
| `AIMessageErrorOutOfMoney` | AIJob:updateCost (54/535 l.178) |
| `AIMessageSuccessStoppedByUser` (OK type, 29/247 l.29-31) | toggleAIVehicle (78/622 l.431), onAIModeChanged (l.221) |
| `AIMessageErrorOutOfFuel`, `...BlockedByObject`, `...GraintankIsFull`, `...VehicleBroken`, `...NotReachable`, `...CouldNotPrepare`, `...ImplementWrongWay` | registered in `AIMessageManager:loadMapData` (29/244 l.66-93). **Where they are raised is GAP** in the fetched pages (Motorized has no OutOfFuel raise. AIDriveStrategyCombine only slows down near a full tank, 3/149 l.247-250). |

Blocked-by-obstacle **does not end the job**. FieldCourse returns zero speed while `self.isBlocked` (3/151 l.466-468)
and broadcasts `AIVehicleIsBlockedEvent` (l.129-135).

**How a script learns the job ended and why**
- `AISystem:stopJobInternal(job, aiMessage)` calls `job:stop(aiMessage)` and `removeJob`, then
  **`g_messageCenter:publish(MessageType.AI_JOB_STOPPED, job, aiMessage)`** (3/154 l.576-581). On the next update it publishes
  `MessageType.AI_JOB_REMOVED, jobId` (l.453-462). The subscribe pattern `g_messageCenter:subscribe(MessageType.X, fn, target)` is
  proven by FieldManager (17/183 l.321). That callback receives `(job, aiMessage)` follows from publish's arguments (UNVERIFIED-call semantics).
- Telling the reason apart: `ClassUtil.getClassObjectByObject(aiMessage) == AIMessageSuccessFinishedJob`. This is the same call
  `AIMessageManager:getMessageIndex` uses (29/244 l.137-145). `aiMessage:getType()` gives `AIMessageType.OK|INFO|ERROR`
  (base `ERROR`, 29/219 l.43-45). **StoppedByUser is also OK**, so match the class, not the type.
- `job.isRunning` is false after `AIJob:stop` (54/535 l.278). `AIJobFieldWork:stop` calls `vehicle:deleteAgent()` and
  `vehicle:aiJobFinished()` (54/538 l.122-130). AIJobVehicle registers the vehicle events `onAIJobStarted`/`onAIJobFinished`
  (78/622 l.45-46). Example listener: `Motorized:onAIJobFinished(job)` (78/698 l.2488).
- `vehicle:getIsAIActive()` is `superFunc or spec_aiJobVehicle.job ~= nil` (78/622 l.396-398). `vehicle:getJob()` and `getLastJob()`
  are registered (l.75-76, UNVERIFIED-BODY). `aiSystem:getJobById(id)` and `getActiveJobs()` (3/154 l.613-621, 534-536).
- There is **no `job:getIsFinished`** on AIJob. Only tasks have `getIsFinished` (used in 54/535 l.138).
- Progress: no percent API on the job. Workable sources: the FieldCourse segment data is internal (`aiFieldCourse:getActiveSegmentData`,
  3/151 l.409/509, debug only), or poll field state yourself (section 3).

## 2. Helper purchase settings (exact `g_currentMission.missionInfo` fields)
- `helperBuyFuel` (bool): when the AI is active and the fill type is DIESEL/DEF, nothing is consumed. Diesel is charged at
  1.5× the price as `MoneyType.PURCHASE_FUEL` (Motorized:updateConsumers 78/698 l.2182, 2210-2221).
- `helperBuySeeds` (bool): an AI with no seeds does not stop with OutOfFill (78/797 l.510-519). Seeds are not consumed and
  are charged at 1.5× as `PURCHASE_SEEDS` (l.1070-1088).
- `helperBuyFertilizer` (bool): fertilizer, liquid fertilizer, herbicide and lime are bought at 1.5× (`PURCHASE_FERTILIZER`)
  (Sprayer:getExternalFill 78/800 l.558-581). It enables external fill for `isFertilizerSprayer` (l.480).
- `helperSlurrySource` (int): `2` = buy, `>2` = `g_currentMission.liquidManureLoadingStations[value-2]` (l.505-535). `>1` enables (l.478).
- `helperManureSource` (int): `2` = buy, `>2` = `g_currentMission.manureLoadingStations[value-2]` (l.537-557). `>1` enables (l.479).
- External fill only applies while `rootVehicle:getIsFieldWorkActive()` (l.473). If the tank capacity is 0 and there is no source, there is no external fill (l.455-470).
- Value `1` meaning "off" is inferred from `> 1` (UNVERIFIED label). Who sets these fields (settings UI) is GAP.
- Related: `missionInfo.plowingRequiredEnabled` (17/183 l.255), `missionInfo.growthMode` (78/797 l.857).

## 3. Field data
- **FieldManager** (17/183): `self.fields` is an array (`ipairs`, l.124). `g_fieldManager:getFieldById(id)` is used by GIANTS (17/182 users,
  35/403 l.156, 204). No `getFields()` was seen (GAP; iterate `g_fieldManager.fields` = UNVERIFIED access).
  `farmlandIdFieldMapping[farmland.id] = field` (l.166). Each field is linked to the farmland at its centre: `field:setFarmland`
  and `farmland:setField` (l.127-166).
- **Field** (17/182 l.29-104): `name`, `rootNode`, `polygonPoints` (nodes), `densityMapPolygon`, `areaHa`, `posX/posZ`
  (`MathUtil.getPolygonLabel`, l.91), `angle`, `farmland`, `fieldState = FieldState.new()`. Getters used by GIANTS (UNVERIFIED-BODY):
  `getId()`, `getName()`, `getCenterOfFieldWorldPosition()` (17/183 l.127), `getPolygonPoints()` (l.138), `getDensityMapPolygon()`
  (35/403 l.372), `getAreaHa()`, `getAngle()`, `getHasOwner()`, `getFieldState()` (35/403 l.302).
- **Ownership** (25/214): `g_farmlandManager:getFarmlandOwner(farmlandId)` (l.407-414, returns `NO_OWNER_FARM_ID` when unknown),
  `getFarmlandAtWorldPosition(x,z)` (l.432-435), `getFarmlandIdAtWorldPosition` (l.421-428),
  `getIsOwnedByFarmAtWorldPosition(farmId,x,z)` (l.332-338), `getFarmlandById` (l.492), `getFarmlands()` (l.499). Farmland has
  `id`, `name`, `xWorldPos/zWorldPos`, `areaInHa`, `farmId`, `field` (25/213 l.29-84).
- **FieldState** (35/404 l.33-56) fields: `isValid, fruitTypeIndex, growthState, lastGrowthState, weedState, weedFactor,
  stoneLevel, groundType, sprayLevel, sprayType, limeLevel, rollerLevel, plowLevel, stubbleShredLevel, waterLevel, farmlandId,
  ownerFarmId`. Sampling: `local fs = FieldState.new(); fs:update(x, z)`. Then `fs.groundType == 0` means not field (17/183 l.327-336,
  the GIANTS debug overlay samples a grid exactly this way). The `update` body is GAP (it is not documented).
  The field's cached `field:getFieldState()` is what missions use (35/405 l.129-157).
- **Area sampling, the GIANTS mission pattern** (PlowMission:createModifier 35/405 l.39-47; AbstractFieldMission 35/403
  l.370-487):
  `local id,first,num = g_currentMission.fieldGroundSystem:getDensityMapData(FieldDensityMap.GROUND_TYPE)`;
  `mod = DensityMapModifier.new(id, first, num, g_terrainNode)`; `flt = DensityMapFilter.new(mod)`;
  `flt:setValueCompareParams(DensityValueCompareType.EQUAL, FieldGroundType.getValueByType(FieldGroundType.PLOWED))`;
  `field:getDensityMapPolygon():applyToModifier(mod)`; optional `mod:setPolygonClipRegion(minZ,maxZ)` partitions
  (from `mod:getPolygonMinMaxZ()`); `local sumPixels, area, totalArea = mod:executeGet(flt)`. Fraction done = `area/totalArea`.
  FieldManager uses the same pattern with farmland map + `modifier:addPolygonPointWorldCoords` (17/183 l.119-143).
- **FSDensityMapUtil** (90/869, signatures only, no line numbers): `getFieldDataAtWorldPosition(x,y,z)` → `isOnField,
  densityBits, groundType`; `getFruitTypeIndexAtWorldPos(x,z)` → `index, growthState`; `getIsFieldAtWorldPos(x,z)`;
  `getFieldTypeAtWorldPos(x,z)`; `getFruitArea(fruitIndex, sx,sz,wx,wz,hx,hz, allowPreparing, useMinForageState)` →
  `ret, numPixels, totalNumPixels, growthState`; `getFieldDensity(...)`; `getAreaDensity(id, firstCh, numCh, value, ...)`;
  `getWeedFactor(...)`.
- **What each result looks like**:
  plowed = `groundType == FieldGroundType.PLOWED` (35/405 l.42/54) and `plowLevel` at `getMaxValue(FieldDensityMap.PLOW_LEVEL)`
  (l.140-141). cultivated = `FieldGroundType.CULTIVATED` (17/183 l.242). seeded = `fruitTypeIndex == X` with groundType `SOWN`/
  `PLANTED` (FieldManager uses SOWN for planted fields, l.221; `fruitDesc:getGrowthStateGroundType(gs)` l.238).
  fertilized = `sprayLevel` (max `getMaxValue(FieldDensityMap.SPRAY_LEVEL)`, l.104), `sprayType` = `FieldSprayType.FERTILIZER|MANURE|
  LIQUID_MANURE|LIME|NONE`. limed = `limeLevel` (max `LIME_LEVEL`, l.106; only if `Platform.gameplay.useLimeCounter`).
  harvested = growthState is a cut state: `FruitTypeDesc.cutState` / `cutStates[k]` (38/410 l.307-308), `fruitDesc:getIsCut(gs)`
  (used 35/405 l.156; body GAP). Harvestable = `minHarvestingGrowthState..maxHarvestingGrowthState` (38/410 l.292-295).
  mowed grass: GAP (no evidence for the post-mow state). Baled: field state shows nothing; count bales instead (GAP).
  The numeric FieldGroundType values and the full enum are GAP. Always use `FieldGroundType.getValueByType`.

## 4. Which vehicles can do field work, and how implements declare their work
- **AIFieldWorker** (78/620) needs `AIJobVehicle` + `Drivable` (l.43-46). It goes on the tractor or harvester. **AIJobVehicle** needs
  `AIVehicle` + `Drivable` (78/622 l.18-21). **AIImplement** (78/621) goes on the tool.
- An implement counts as AI-usable when `getCanImplementBeUsedForAI()` sees left, right and back AI markers (`vehicle.ai.areaMarkers`)
  (l.537-544). It then appears in `addVehicleToAIImplementList` (l.548-554), which the strategy reads through
  `vehicle:getAttachedAIImplements()` (3/151 l.37,171). Drive strategies are chosen per child vehicle from
  `AIFieldWorker.registeredDriveStrategies` (78/620 l.575-600). Combine and Baler have their own strategies (3/149, 3/148).
- Working width: `WorkArea:getAIWorkAreaWidth()` is the max `workWidth` over AI work-area types (78/832 l.616-625).
  `getWorkAreaWidth(idx)` (l.507-510). AI marker width is `calcDistanceFrom(leftMarker,rightMarker)` (78/621 l.629-653).
  `getAIMarkers()` returns `left,right,back,inverted,width,validityOffset` (l.603-625).
- What the AI will work is declared as ground/fruit requirements through `addAIGroundTypeRequirements`, `addAITerrainDetailRequiredRange`,
  `addAIFruitRequirement`, and the prohibitions (78/621 l.858-1022):
  Plow `setPlowAIRequirements` → `Plow.AI_REQUIRED_GROUND_TYPES` (78/776 l.524-536). Cultivator `updateCultivatorAIRequirements`
  → `AI_REQUIRED_GROUND_TYPES_DEEP/FLAT` (78/652 l.244-281). SowingMachine `updateAiParameters` → `AI_REQUIRED_GROUND_TYPES`
  (+`AI_OUTPUT_GROUND_TYPES`) plus fruit prohibitions for the selected seed (78/797 l.1167-1242). Sprayer → `AI_REQUIRED_GROUND_TYPES`
  and weed fruit requirements (78/800 l.251-252, 687-723). Mower: GRASS/SOWN/HARVEST_READY(_OTHER) + input fruit harvest range
  (78/700 l.207-228). Cutter: `addAIFruitRequirement`/`setAIFruitRequirements` (78/653 l.1534, 1107-1117).
  The contents of the `AI_REQUIRED_GROUND_TYPES` constants are GAP.
- Spec tables seen in code: `spec_plow`, `spec_cultivator`, `spec_sowingMachine`, `spec_sprayer` (with `isSlurryTanker/
  isManureSpreader/isFertilizerSprayer`), `spec_mower`, `spec_combine`, `spec_aiImplement`, `spec_workArea`. To detect a type,
  use `SpecializationUtil.hasSpecialization(Cultivator, v.specializations)` as GIANTS does (78/797 l.1182). Seed selection:
  `sowingMachine:setSeedFruitType(fruitTypeIndex, noEventSend)` / `setSeedIndex` (78/797 l.398-440).
- Per-implement continue gates: `getCanAIImplementContinueWork(isTurning)` → `canContinue, stopAI, stopReason` (base 78/621
  l.531-533; Plow l.705-717; SowingMachine l.847-864).

## Summary table
| API | Exact call | Source (class + lines) | Status |
|---|---|---|---|
| Job type | `g_currentMission.aiJobTypeManager:createJob(AIJobType.FIELDWORK)` | AIJobTypeManager 33,36,92-106 | PROVEN |
| Apply state | `job:applyCurrentState(vehicle, mission, farmId, isDirectStart)` | AIJobFieldWork 134-163 | PROVEN |
| Target | `job.positionAngleParameter:setPosition(x,z)`; `:setAngle(rad)` (snapped) | AIJobFieldWork 34,161-162; AIParameterPositionAngle 68-77 | PROVEN |
| Values/validate | `job:setValues()`; `job:validate(farmId)` (vehicle only) | AIJobFieldWork 199-224; AIParameterVehicle 86-99 | PROVEN |
| Startable check | `job:getIsStartable(connection)` | AIJobFieldWork 243-262 | PROVEN |
| Vehicle capable | `job:getIsAvailableForVehicle(v)` → `v:getCanStartFieldWork()` | AIJobFieldWork 167-169 | PROVEN call / UNVERIFIED-BODY |
| Start | `g_currentMission.aiSystem:startJob(job, farmId)` | AISystem 540-559 | PROVEN |
| Skip drive | `isDirectStart` or <3 m from target → task 2 | AIJobFieldWork 90-105 | PROVEN |
| Field pick | `FieldCourse.findClosestField(...)` from vehicle pos | AIDriveStrategyFieldCourse 109-123 | PROVEN call / GAP body |
| End event | `g_messageCenter:subscribe(MessageType.AI_JOB_STOPPED, fn, self)` → `(job, aiMessage)` | AISystem 576-581; FieldManager 321 | PROVEN publish / UNVERIFIED callback args |
| Finished | `ClassUtil.getClassObjectByObject(msg) == AIMessageSuccessFinishedJob` | AIFieldWorker 337-340; AIMessageManager 137-145 | PROVEN |
| Errors | FieldNotOwned/NoFieldFound/FieldNotReady/OutOfFill/WrongSeason/OutOfMoney | FieldCourse 396-400,203,211; SowingMachine 510-515,858; Sprayer 415-418; AIJob 178 | PROVEN |
| OutOfFuel/Blocked/GraintankFull raise sites | — | AIMessageManager 66-93 (registered only) | GAP |
| Running | `v:getIsAIActive()`; `job.isRunning`; `aiSystem:getJobById(id)` | AIJobVehicle 396-398; AIJob 257,278; AISystem 613-621 | PROVEN |
| Stop | `v:stopCurrentAIJob(AIMessageSuccessStoppedByUser.new())` | AIJobVehicle 54,431 | PROVEN call / UNVERIFIED-BODY |
| Helper buy | `missionInfo.helperBuyFuel/helperBuySeeds/helperBuyFertilizer/helperSlurrySource/helperManureSource` | Motorized 2212; SowingMachine 510,1070; Sprayer 478-480,505-563 | PROVEN |
| Fields | `g_fieldManager:getFieldById(id)`; `g_fieldManager.fields` | AbstractFieldMission 156; FieldManager 72,124 | PROVEN call / UNVERIFIED-BODY |
| Field geometry | `field.posX/posZ`, `field.polygonPoints`, `field:getCenterOfFieldWorldPosition()` | Field 35,43-44,81-91; FieldManager 127 | PROVEN fields / UNVERIFIED-BODY getter |
| Ownership | `g_farmlandManager:getFarmlandOwner(field.farmland.id)`; `getIsOwnedByFarmAtWorldPosition(farmId,x,z)` | FarmlandManager 407-414,332-338; Field 38 | PROVEN |
| Point state | `local s=FieldState.new(); s:update(x,z)` → groundType, fruitTypeIndex, growthState, sprayLevel, plowLevel, limeLevel, weedState | FieldState 33-56; FieldManager 327-336 | PROVEN fields / GAP update body |
| Area fraction | `DensityMapModifier/Filter` + `densityMapPolygon:applyToModifier` + `executeGet` | PlowMission 39-47; AbstractFieldMission 370-448 | PROVEN |
| Ground values | `FieldGroundType.getValueByType(FieldGroundType.PLOWED/CULTIVATED/SOWN/...)` | PlowMission 42; FieldManager 101,242 | PROVEN names / GAP numeric enum |
| Harvested | `fruitDesc.cutState`, `fruitDesc:getIsCut(gs)` | FruitTypeDesc 307-308; PlowMission 156 | PROVEN field / UNVERIFIED-BODY |
| AI implement | `impl:getCanImplementBeUsedForAI()`; `v:getAttachedAIImplements()` | AIImplement 537-554; FieldCourse 37,171 | PROVEN / UNVERIFIED-BODY (2nd) |
| Width | `impl:getAIWorkAreaWidth()`; `getAIMarkers()` 5th return | WorkArea 616-625; AIImplement 603-625 | PROVEN |
| Seed choice | `sower:setSeedFruitType(fruitTypeIndex)` | SowingMachine 432-440 | PROVEN |
| Progress % | — (no job API; poll area fraction) | — | GAP |
