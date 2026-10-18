# FS25 supply and logistics APIs: what the evidence shows

Sources: GDN FS25 script docs (fetched 2026-10-18). They are cited as `GDN <Class> (cat/class) L<n>`, where `<n>` is the
**source line number GDN prints** next to each function. AutoDrive 3.0.1.4 (modDesc.xml:44) at
`/tmp/claude-1000/ad_src`, cited as `file:line` relative to `ad_src/scripts/`. "PROVEN" here means
the code is shown in the docs or source. Nothing below has been run in FS25.

**GDN has no class pages for any of these:** LoadTrigger, UnloadTrigger, SellingStation, UnloadingStation,
LoadingStation, BuyingStation, Storage, StorageSystem, ProductionPoint, FillActivatable, ItemSystem.
They appear only where documented code calls them. Every member listed below comes from such a call
site and cites it. Anything without a call site is GAP.

## 1. FillUnit (GDN FillUnit 78/672)
- Fill units: `vehicle:getFillUnits()` returns `spec_fillUnit.fillUnits` (L822). Each entry has `.capacity`,
  `.fillLevel`, `.fillType`, `.supportedFillTypes` (a set: `[fillType]=true`), `.supportedToolTypes`,
  `.lastValidFillType` and `.fillUnitIndex` (L846-1013, L1230-1234).
- `getFillUnitFillLevel(i)` L884. `getFillUnitCapacity(i)` L846. `getFillUnitFillLevelPercentage(i)` L894.
  `getFillUnitFillType(i)` L910. `getFillUnitSupportedFillTypes(i)` L997 returns the set.
  `getFillUnitSupportsFillType(i, ft)` L970. `getFillUnitAllowsFillType(i, ft)` L1017.
  `getFillUnitCanBeFilled(i, ft, ignoreCap)` L1059 (allows the type and free capacity > 0).
  `getFillUnitFreeCapacity(i, ft, farmId)` L859: capacity minus fillLevel, or 0 when the trailerFillLimit mass
  limit is reached. The ft and farmId arguments are documented as "currently ignored/unused".
  `getFillUnitExists(i)` L839. `getFirstValidFillUnitToFill(ft, ignoreCap)` L1044.
  `debugGetSupportedFillTypesPerFillUnit()` L2770.
- Writing: `addFillUnitFillLevel(farmId, i, delta, ft, toolType, fillPositionData)` L1485 returns the
  applied delta. A negative delta is refused when the farm has no access (L1491). This is a direct
  level change, not a trigger flow.
- Seeder: `SowingMachine:getSowingMachineFillUnitIndex()` (GDN SowingMachine 78/797 L456) returns
  `spec_sowingMachine.fillUnitIndex`, read from XML `vehicle.sowingMachine#fillUnitIndex` (default 1, L240).
  `getSowingMachineSeedFillTypeIndex()` L462 returns the fill type of the currently selected seed fruit.
  `SowingMachine:addFillUnitFillLevel` (L763) turns any accepted type in that unit into `spec.seedFillType`.
  When AI is active and `missionInfo.helperBuySeeds` is set, seeds are bought rather than consumed (L1070-1085).
- Sprayer, fertilizer spreader and lime spreader: all three use the Sprayer specialization ("fertilizer, lime,
  herbicide", GDN Sprayer 78/800). `getSprayerFillUnitIndex()` L756 returns the active spray type's fillUnitIndex,
  falling back to `spec_sprayer.fillUnitIndex` (XML `vehicle.sprayer#fillUnitIndex`, default 1, L257).
  `spec.supportedSprayTypes` is rebuilt in `onStateChange` (L1327-1345) from `getFillUnitSupportedFillTypes(getSprayerFillUnitIndex())`
  and also covers sources on attached vehicles (`FillUnit.addFillTypeSources`, L2780).
  `getActiveSprayType()` L816. `getExternalFill(fillType, dt)` L488-579 handles AI "helper buys" for
  FERTILIZER, LIQUIDFERTILIZER, HERBICIDE and LIME (L558-561), charged through `economyManager:getCostPerLiter`.
  There is a separate `SaltSpreader` spec (78/790) with `vehicle.saltSpreader#fillUnitIndex`.
  `ExtendedSprayer`, `FertilizingSowingMachine` and `ExtendedSowingMachine` exist (78/666, /670, /665); I did not read them in detail.

## 2. Filling
### (i) From a pallet or big bag: FillTriggerVehicle + FillTrigger (PROVEN in code)
- A pallet with `vehicle.fillTriggerVehicle#triggerNode` builds `FillTrigger.new(triggerNode, self, fillUnitIndex, lps)`
  in `FillTriggerVehicle:onLoad` (GDN FillTriggerVehicle 78/671 L52-67). That makes the pallet the trigger's `sourceObject`.
- `FillTrigger:fillTriggerCallback` (GDN FillTrigger 88/852 L173-221): when a vehicle's fill node enters
  the trigger and `getFillUnitCanBeFilled` passes, it calls `vehicle:addFillUnitTrigger(self, fillType, fillUnitIndex)`.
  `FillUnit:addFillUnitTrigger` (L2371) adds a FillActivatable, which is the player prompt. Filling starts
  automatically only on platforms with `Platform.gameplay.automaticFilling` (L2378).
- Starting the fill: `vehicle:setFillUnitIsFilling(true[, noEventSend])` (FillUnit L2450). It is a registered
  vehicle function (L261) and does not check whether the vehicle is entered. It picks the first trigger in
  `spec_fillUnit.fillTrigger.triggers` whose `getIsActivatable(vehicle)` is true. For FillTrigger (L156) that
  means the source has level > 0 and `accessHandler:canFarmAccess(vehicle:getActiveFarm(), source)`.
  On the server, `FillUnit:onUpdateTick` (L684-697) calls `trigger:fillVehicle(self, lps*dt)` and stops filling when delta <= 0.
  `FillTrigger:fillVehicle` (L98-150) moves level from source to vehicle. With no `sourceObject` it charges
  money at `economyManager:getPricePerLiter` (L143-145).
- AutoDrive starts this for an unentered vehicle: `AutoDrive.startFillTrigger` (Utils/TrailerUtil.lua:942-985)
  loops over `trailer.spec_fillUnit.fillTrigger.triggers`, checks `trigger:getCurrentFillType()` against the
  selected fill types and `trigger:getIsActivatable(rootVehicle)`, then calls `spec:setFillUnitIsFilling(true)`
  at :970. AD calls this on the spec table, not on the vehicle. Whether that dispatches correctly is UNVERIFIED.
- State to read: `spec_fillUnit.fillTrigger.isFilling`, `.currentTrigger` and `.triggers` (FillUnit L344-351).
- Completion check: `getFillUnitFillLevel` goes up, and `isFilling` returns to false when the source is empty
  or the vehicle is full (L695-696).
- Big bags: the `BigBag` spec (78/639) has no prerequisites (L17). Whether a given big bag uses
  FillTriggerVehicle depends on its XML: UNVERIFIED per asset. A seeder parked under a pallet or bag is the case covered above.
- "ObjectFillTrigger" / `FillUnit:onFillTriggerEnter`: no page and no call site found. GAP.

### (ii) LoadTrigger at a silo, shop or production (no GDN class page: UNVERIFIED API)
- Discovery (AD, Manager/TriggerManager.lua:28-42): `placeable.spec_silo.loadingStation`,
  `spec_husbandry.loadingStation`, `spec_buyingStation.buyingStation`, `spec_productionPoint.productionPoint.loadingStation`,
  then `.loadTriggers[]`. AD also hooks `LoadTrigger.load` and `LoadTrigger.delete` (AutoDrive.lua:247,249).
- GIANTS player path: `FillUnit:getAllowLoadTriggerActivation` (L1331-1341) returns true only when
  `self.rootVehicle == g_localPlayer:getCurrentVehicle()`, so the UI path needs an entered vehicle.
- AD's script path for an unentered vehicle (Modules/TrailerModule.lua:609-631) sets `trigger.autoStart = true`
  and `trigger.selectedFillType = ft`, then calls `trigger:onFillTypeSelection(ft)`. AD appends
  `AutoDrive.onFillTypeSelection` (AutoDrive.lua:242; Utils/UtilFuncs.lua:1044-1132). That hook walks
  `self.fillableObjects` (`{object, fillUnitIndex}`) and calls `self:setIsLoading(true, object, fillUnitIndex, ft)`
  (:1079, :1121). Its comment at :1064 says this is done "even without entering vehicle".
  Fields read: `trigger.isLoading`, `fillableObjects`, `validFillableObject`/`validFillableFillUnitIndex`
  (TrailerUtil.lua:867-889), `trigger.source:getAllFillLevels(farmId)` (:804), `trigger.hasInfiniteCapacity`.
- GIANTS AI path: `AITaskLoading:onTargetReached` (GDN AITaskLoading 81/848 L109-117) calls
  `loadVehicle:aiPrepareLoading(fillUnitIndex, task)` and then `loadVehicle:aiStartLoadingFromTrigger(loadTrigger, fillUnitIndex, fillType, task)`.
  The trigger comes from `loadingStation:getAITargetPositionAndDirection(fillType)`, which returns
  `x, z, dirX, dirZ, trigger` (AIJobLoadAndDeliver 54/540 L153). The spec that defines
  `aiStartLoadingFromTrigger` has no page: GAP.
- Completion: fill level rises and `trigger.isLoading` becomes false. AD treats it as stopped after a
  300 ms timer (`stoppedTimer`, TrailerModule.lua:621).

### (iii) Buying at a trigger
- `PlaceableBuyingStation:getBuyingStation()` (78/714 L138) registers through `storageSystem:addLoadingStation(buyingStation, owningPlaceable)`
  (L444 in the file text), so buying stations are LoadingStations and load through LoadTriggers.
  The price charged per litre lives in the undocumented BuyingStation/LoadTrigger: GAP.
- FillTrigger without a source charges `delta * economyManager:getPricePerLiter(ft)` (FillTrigger L143-145).
  `FillTrigger.onCreate` (L13-18) registers the money type "finance_purchaseFuel". That trigger is fuel (default `FillType.DIESEL`, L56).
  Note `FillTrigger:getIsActivatable` (L156-164) returns false when `sourceObject` is nil, which would block
  `setFillUnitIsFilling` from choosing a sourceless trigger. A subclass may override this: UNVERIFIED.
- `PlaceableSilo:refillAmount(fillTypeIndex, amount, price)` (78/761 L429) adds to silo storages. It is a silo refill, not a vehicle fill.

## 3. Unloading and selling
- Listing: `g_currentMission.storageSystem:getUnloadingStations()` (PlaceableSilo L330, AD TriggerManager.lua:381)
  and `:getLoadingStations()` (AIJobLoadAndDeliver L245, AD :411). SellingStations are registered as unloading
  stations (`PlaceableSellingStation` 78/759, `storageSystem:addUnloadingStation(spec.sellingStation, self)`;
  `economyManager:addSellingStation`, L95).
- Station members used in documented code: `station.owningPlaceable` (Silo L331; set at SellingStation L75,
  ProductionPoint L104), `station.isSellingPoint` and `station.acceptedFillTypes[ft]` (Silo L332-333),
  `station:getEffectiveFillTypePrice(ft)` (Silo L334; AD :385), `station:getName()` (AIJobLoadAndDeliver L241),
  `station:getAISupportedFillTypes()` (L235/L247), `station:getAITargetPositionAndDirection(ft)` returning
  `x, z, dirX, dirZ, trigger` (L153/L161), `station:isa(UnloadingStation)` (L234), `station.unloadTriggers[]` with
  `.exactFillRootNode` (SellingStation L130-131). AD also uses `isa(SellingStation)`, `hideFromPricesMenu` and
  `supportedFillTypes[ft]` (AD :383).
  LoadingStation: `getFillLevel(ft, farmId)` (AIJobLoadAndDeliver L267). Access: `g_currentMission.accessHandler:canFarmAccess(farmId, station)` (AD :412).
- Position: no station position getter is documented. Use `getAITargetPositionAndDirection`, or the engine call
  `getWorldTranslation(unloadTrigger.exactFillRootNode)` (AD :455-457). The owner farm comes from
  `station.owningPlaceable:getOwnerFarmId()`, which is the Placeable method used at ProductionChainManager L98 for production points.
  Calling it on stations is UNVERIFIED.
- Discharging a trailer (GDN Dischargeable 78/657):
  - The server tick (L482-560) scans `dischargeNode.trigger.objects`, which are fill-unit-like objects such as
    UnloadTriggers. It picks the nearest one that supports and allows the fill type, accepts `ToolType.TRIGGER`
    and has free capacity, and stores it as `dischargeNode.dischargeObject`/`dischargeFillUnitIndex`.
  - Failure reasons are recorded: `DISCHARGE_REASON_FILLTYPE_NOT_SUPPORTED`, `TOOLTYPE_NOT_SUPPORTED`, `NO_ACCESS`, `NO_FREE_CAPACITY`.
  - Readers: `getCurrentDischargeNode()` L911, `getDischargeTargetObject(node)` L929 returns `obj, fillUnitIndex`,
    `getCanDischargeToObject(node)` L1270-1316, `getDischargeState()` L1144.
  - Start and stop: `setDischargeState(Dischargeable.DISCHARGE_STATE_OBJECT | _GROUND | _OFF, noEventSend)` L1095
    (sends an event and raises "onDischargeStateChanged"). `setManualDischargeState` L1089 is overridden by
    `Trailer:setManualDischargeState` (78/812 L1184). Trailer also has `startTipping(tipSideIndex)` L707,
    `stopTipping` L746 and `getTipState` L865.
  - Transfer: `dischargeToObject(node, liters, object, targetFillUnitIndex)` L1066-1085 calls
    `object:addFillUnitFillLevel(...)` and removes the same amount from the trailer.
  - AD uses exactly this: it checks `getCanDischargeToObject(getCurrentDischargeNode())` (TrailerModule.lua:649),
    then `setDischargeState(DISCHARGE_STATE_OBJECT)` (:686) and `DISCHARGE_STATE_OFF` to stop (:457, :514).
  - Completion: the trailer's fill level drops to 0, or the state returns to OFF (`handleDischargeOnEmpty`, L1610).
  - Sale value is not reported back. Watch farm money (GAP: no documented getter was read).
- GIANTS AI unload: `AITaskDischarge` (81/845) uses `unloadTrigger:getAITargetPositionAndDirection()` (L73).
  `Trailer:startAIDischarge(superFunc, dischargeNode, task)` L1131. `finishedDischarge` L105 sets `isFinished`.

## 4. Productions (GDN ProductionChainManager 58/554)
- `g_currentMission.productionChainManager` (PlaceableProductionPoint L486/L625). `.productionPoints` (list, L92).
  `getProductionPointsForFarmId(farmId)` L237. `getUnownedProductionPoints()` L255. `getNumOfProductionPoints()` L249.
  `farmIds[farmId].inputTypeToProductionPoints[ft]` L116-125. `productionPoint.inputFillTypeIds` (set, L120).
  `productionPoint:getOwnerFarmId()` L98.
- `productionPoint.loadingStation` and `.unloadingStation.unloadTriggers` (PlaceableProductionPoint L244, L253).
  Events: "onOutputFillTypesChanged" and "onProductionStatusChanged" (L25-26, L171-178).
- The ProductionPoint class (outputs, recipes, storage, fill levels, running state) has no page: GAP.
  Storage objects as used by PlaceableSilo (`spec_silo.storages[]`, L440-468): `storage:getFillLevels()`,
  `getFillLevel(ft)`, `getFreeCapacity(ft)`, `setFillLevel(level, ft)`. `PlaceableSilo:getFillLevels()` L463.

## 5. Pallets and big bags
- Pallets are vehicles. `Pallet:onPreLoad` sets `self.isPallet = true` (GDN Pallet 78/703 L88), and they have
  `spec_pallet`. `VehicleSystem:consoleCommandVehicleRemoveAll` skips `vehicle.isPallet` while iterating
  `self.vehicles` (GDN VehicleSystem 91/897 L1439-1441).
- Listing: `g_currentMission.vehicleSystem.vehicles` (AD Utils/AutoDriveUtilFuncs.lua:986-987; `VehicleSystem.vehicles`
  is filled by `addVehicle` L221-250). `getVehicleByUniqueId` L271.
  Filter on `v.isPallet` / `v.spec_pallet` / `v.spec_bigBag`.
- Fill type and level: the pallet's FillUnit, e.g. `v:getFillUnitFillType(1)` and `getFillUnitFillLevel(1)`. Index 1 is a convention: UNVERIFIED per pallet.
  Owner: `v:getOwnerFarmId()` (used in Vehicle 91/888 L1513, L4312). Position: `getWorldTranslation(v.rootNode)`
  or `v.components[1].node` (AD TrailerUtil.lua:778).
- The pallet spawn console command exists (`consoleCommandAddPallet`, L853). Development use only.
  `g_currentMission.itemSystem` and object storage contents were not verified (`PlaceableObjectStorage` 78/751 exists, not read): GAP.

## 6. AutoDrive 3.0.1.4 logistics
- Mode constants (AutoDrive.lua:23-28): DRIVETO=1, PICKUPANDDELIVER=2, DELIVERTO=3, LOAD=4,
  UNLOAD=5 (combine unloader), BGA=6. Mode objects are built at Specialization.lua:206-212
  (DELIVERTO→UnloadAtMode, UNLOAD→CombineUnloaderMode). `ADStateModule.HIGHEST_MODE = 5` (Modules/StateModule.lua:15).
  `setMode` will not select 6 (BGA) (:664-675).
- Markers:
  - `vehicle.ad.stateModule:setMode(m)` :664. `setFirstMarker(markerId)` :881 and `setSecondMarker(markerId)` :932 take map marker IDs
    (`ADGraphManager:getMapMarkerById`). `...ByName` variants are at :895/:946.
  - Marker roles by mode:
    - PICKUPANDDELIVER: first = load (Modes/PickupAndDeliverMode.lua:197), second = unload (:212).
    - LOAD: second = load (Modes/LoadMode.lua:124).
    - DELIVERTO: first = unload (Modes/UnloadAtMode.lua:69).
    - UNLOAD (combine): first = field/start (Modes/CombineUnloaderMode.lua:222), second = unload (:279).
  - Every `start()` returns without doing anything when a required marker is nil (PickupAndDeliverMode:37, LoadMode:30, UnloadAtMode:26, CombineUnloaderMode:52).
- Fill type: `stateModule:setFillType(fillTypeIndex)` :987 (only if supported by the vehicle).
  `setSelectedFillTypes(list)` :964 must come after setFillType. `getSelectedFillTypes()` :959 is what LoadTrigger and FillTrigger loading uses.
- Starting: `AutoDrive:StartDriving(vehicle, destinationID, unloadDestinationID, cbObj, cbFunc, cbArg)` (ExternalInterface.lua:114-161).
  - It acts only if AD is not already active, and it does not set the mode. Set the mode first.
  - It sets the first marker only when both IDs are valid map markers (:121-124). It sets the second marker
    and calls `getCurrentMode():start()` (:126-128).
  - `unloadDestinationID == -3` parks and `-2` refuels (:129-158).
  - Each `mode:start()` calls `vehicle:startAutoDrive()` (server only, Specialization.lua:1116-1117), which resets
    `ad.isStoppingWithError = false` (:1141).
  - `GetAvailableDestinations()` (:84) returns `{id, name, x, y, z}` for each marker.
- Combine unloading (MODE_UNLOAD):
  - Set the UNLOAD mode, set both markers, then call start (or `StartDriving`).
  - `CombineUnloaderMode:start` (:46-63): on the field it calls `setToWaitForCall()`, and `WaitForCallTask`
    registers the vehicle with `ADHarvestManager:registerAsUnloader` (Tasks/WaitForCallTask.lua:10).
  - Harvesters register on load or attach when `AutoDrive.setCombineType` accepts them (Specialization.lua:290-292, 708-711).
  - `ADHarvestManager:update` (Manager/HarvestManager.lua:97-) moves a harvester to active only when it is
    `getIsAIActive()` or entered with its pipe out (:104). It fires the unloader when the harvester's
    `stateModule:getFirstMarker()` differs from the unloader's (:118). So the combine must be running a helper
    and both vehicles need the same first marker.
  - Stopping: `ADHarvestManager:fireUnloader(unloader, harvester)` :84 queues `StopAndDisableADTask`.
    Generic stop: `vehicle:stopAutoDrive()` (Specialization.lua:1186, server only). `AutoDrive:HoldDriving(vehicle)` pauses (ExternalInterface.lua:283).
- Finish and error reporting:
  - `callBackObject/Function/Arg` are stored (ExternalInterface.lua:116-119) but **nothing in AD 3.0.1.4 ever calls them**.
    A grep for `callBack` finds only the storage lines and an unrelated dialog. Callback completion is GAP.
  - Things that can be observed:
    - `vehicle.ad.stateModule:isActive()` (StateModule.lua:677) becomes false in `stopAutoDrive` (:1235).
    - `vehicle.ad.isStoppingWithError` is set true on path, refuel or setup failures (DrivePathModule.lua:67,100;
      TaskModule.lua:142; InputManager.lua:269) and stays set until the next start.
    - The event "onAutoDriveParked" (Specialization.lua:93, :1217).
    - `AutoDriveMessageEvent` error messages (e.g. LoadAtDestinationTask.lua:57).
  - Fill or unload success has to be checked through FillUnit levels.

## Summary table
| capability | exact call | source | status |
|---|---|---|---|
| list fill units | `v:getFillUnits()` -> `spec_fillUnit.fillUnits` | GDN FillUnit L822 | PROVEN |
| level / capacity / type | `getFillUnitFillLevel(i)`, `getFillUnitCapacity(i)`, `getFillUnitFillType(i)` | FillUnit L884/L846/L910 | PROVEN |
| supported types | `getFillUnitSupportedFillTypes(i)` (set), `getFillUnitSupportsFillType(i,ft)`, `getFillUnitAllowsFillType` | FillUnit L997/L970/L1017 | PROVEN |
| free capacity | `getFillUnitFreeCapacity(i)` (ft and farmId ignored) | FillUnit L859 | PROVEN |
| seeder unit / seed type | `getSowingMachineFillUnitIndex()`, `getSowingMachineSeedFillTypeIndex()` | SowingMachine L456/L462 | PROVEN |
| sprayer/spreader unit / types | `getSprayerFillUnitIndex()`, `spec_sprayer.supportedSprayTypes` | Sprayer L756, L1327-1345 | PROVEN |
| fill from pallet (unentered) | pallet FillTrigger -> `v:setFillUnitIsFilling(true)`; server tick `trigger:fillVehicle` | FillTriggerVehicle L52-67; FillTrigger L98-221; FillUnit L2450, L684; AD TrailerUtil.lua:942-985 | PROVEN code / UNVERIFIED in game |
| LoadTrigger at silo/shop (unentered) | `trigger.selectedFillType=ft; trigger:onFillTypeSelection(ft)` -> `setIsLoading(true,obj,i,ft)`; read `trigger.isLoading` | AD TrailerModule.lua:609-631, UtilFuncs.lua:1044-1132 | UNVERIFIED (no GDN LoadTrigger page) |
| LoadTrigger via GIANTS UI | needs `rootVehicle == g_localPlayer:getCurrentVehicle()` | FillUnit L1331-1341 | PROVEN (entered only) |
| GIANTS AI load from trigger | `aiStartLoadingFromTrigger(trigger,i,ft,task)` | AITaskLoading L109-117 | GAP (defining spec undocumented) |
| buy-at-trigger price | sourceless FillTrigger: `economyManager:getPricePerLiter(ft)`; BuyingStation price | FillTrigger L143; BuyingStation undocumented | PROVEN (fuel) / GAP (BuyingStation) |
| list unloading/loading stations | `g_currentMission.storageSystem:getUnloadingStations()` / `:getLoadingStations()` | PlaceableSilo L330; AIJobLoadAndDeliver L245 | PROVEN |
| station sell info | `isSellingPoint`, `acceptedFillTypes[ft]`, `getEffectiveFillTypePrice(ft)`, `getName()`, `owningPlaceable` | PlaceableSilo L331-334; AIJobLoadAndDeliver L241 | PROVEN (fields used by GIANTS) |
| station position | `getAITargetPositionAndDirection(ft)` -> x,z,dx,dz,trigger | AIJobLoadAndDeliver L153/L161 | PROVEN |
| station stock | `loadingStation:getFillLevel(ft, farmId)`, `getAISupportedFillTypes()` | AIJobLoadAndDeliver L266-267 | PROVEN |
| station owner | `owningPlaceable:getOwnerFarmId()` | inferred | UNVERIFIED |
| discharge into trigger | `getCanDischargeToObject(getCurrentDischargeNode())`; `setDischargeState(Dischargeable.DISCHARGE_STATE_OBJECT/OFF)` | Dischargeable L1270/L911/L1095; AD TrailerModule.lua:649,686 | PROVEN code / UNVERIFIED in game |
| discharge target | `getDischargeTargetObject(node)` -> obj, fillUnitIndex | Dischargeable L929 | PROVEN |
| production points | `productionChainManager.productionPoints`, `getProductionPointsForFarmId(id)`, `.inputFillTypeIds`, `.loadingStation/.unloadingStation` | ProductionChainManager L92/L237/L120; PlaceableProductionPoint L244/L253 | PROVEN |
| production outputs/storage/state | ProductionPoint members | none | GAP |
| silo contents | `placeable:getFillLevels()` (silo); `storage:getFillLevels()` | PlaceableSilo L463-475 | PROVEN |
| list pallets | `g_currentMission.vehicleSystem.vehicles`, filter `v.isPallet` | VehicleSystem L238/L1441; Pallet L88; AD AutoDriveUtilFuncs.lua:987 | PROVEN |
| pallet owner/pos | `v:getOwnerFarmId()`, `getWorldTranslation(v.rootNode)` | Vehicle L1513 (call site); engine | UNVERIFIED (doc call site only) |
| AD start | `stateModule:setMode(m)` then `AutoDrive:StartDriving(v, first, second, ...)` | AutoDrive.lua:23-28; StateModule.lua:664; ExternalInterface.lua:114-161 | PROVEN |
| AD markers / fill type | `setFirstMarker(id)`, `setSecondMarker(id)`, `setFillType(ft)`, `setSelectedFillTypes(list)` | StateModule.lua:881/932/987/964 | PROVEN |
| AD combine unloader | MODE_UNLOAD + markers + start; harvester must be AI-active and share first marker | CombineUnloaderMode.lua:46-63; HarvestManager.lua:104,118 | PROVEN code / UNVERIFIED in game |
| AD stop | `vehicle:stopAutoDrive()` (server); `ADHarvestManager:fireUnloader` | Specialization.lua:1186; HarvestManager.lua:84 | PROVEN |
| AD finished/error | poll `ad.stateModule:isActive()`, `ad.isStoppingWithError`; event "onAutoDriveParked" | StateModule.lua:677; Specialization.lua:1141,1217,1235 | PROVEN |
| AD callback | `callBackFunction` stored, never invoked | ExternalInterface.lua:116-119 | GAP (does not fire) |
| GIANTS load-and-deliver AI job | `AIJobLoadAndDeliver` params vehicle/loadingStation/fillType/unloadingStation/looping | GDN AIJobLoadAndDeliver 54/540 L22-69, L285-293 | PROVEN class / start-by-script path UNVERIFIED |
