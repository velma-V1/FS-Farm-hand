# FS25 store purchase: what the GIANTS docs show (Script v1.20.0.0)

Source: https://gdn.giants-software.com/documentation_scripting_fs25.php?version=script. I fetched all 915 class
pages on 2026-10-18. "L" means the source line numbers printed in the docs. Class page URL is
`?version=script&category=C&class=K`, given below as (C/K).

**Classes the docs do not cover at all** (so every API on them is a GAP): BuyVehicleEvent, ShopConfigScreen,
ShopController, FarmManager, Farm, FSBaseMission, EconomyManager, PlacementUtil, SlotSystem, StoreSpecies,
VehicleSaleSystem, PalletFiller (only its events are documented), and BuyVehicleData:onBought.

## 1. StoreManager (g_storeManager) and StoreItemUtil
- `g_storeManager:getItems()` returns `self.items`, a list. StoreManager (75/609) L676-678. `addItem` sets `storeItem.id = #self.items` (L620-622).
- `getItemByXMLFilename(xml)` looks up by lower-case filename (L695-701). `getItemByIndex(i)` exists (definition only). `getIsItemUnlocked(item)` L707-713.
- **Item fields** (StoreManager.loadItem):
  - name L933; xmlFilename L938; xmlFilenameLower L939; imageFilename L940; species L941
  - functions L942; `specs = nil` (loaded later) L943; brandIndex L944; brandNameRaw L945
  - canBeSold L951 (default true); showInStore L952; isBundleItem L953; allowLeasing L954; maxItemCount L955
  - rotation, spawnRotationOffset, spawnSizeOffset L956-958; financeCategory L966; isMod / customEnvironment L987-988
  - categoryNames: a list of upper-case names, falling back to "MISC" (L991-1011). `categoryName = categoryNames[1]` (L1014).
  - VEHICLE items only: configurations, defaultConfigurationIds L1018; subConfigurations L1019; configurationSets L1020; hasLicensePlates L1022
  - price L1028; dailyUpkeep L1033; runningLeasingFactor L1034; lifetime L1035; bundleInfo (bundles) L1174
  - species is read from `<storeData><species>`, default `StoreSpecies.VEHICLE` (L867-868).
  - There is no `isVehicle` field. Use the type checks instead: `StoreItemUtil.getIsVehicle(item)` (`species == StoreSpecies.VEHICLE`), StoreItemUtil (90/878) L17-19. Also `getIsObject` L41-43, plus getIsHandTool, getIsPlaceable, getIsAnimal, getIsConfigurable (definitions only). `getIsLeasable` L98-100.
- **Categories:**
  - `getCategoryByName(name)` upper-cases the name and returns the category (L428-433).
  - A category is `{name=UPPER, title, image, type, orderId}` (L390-396). The list is stored in `self.categories` (L41) and the map in `self.categoryByName` (L42).
  - There is no `getCategories()` in the docs, so reading `g_storeManager.categories` directly is UNVERIFIED.
  - The docs do not list the category names (TRACTORSS, CARS, ...): GAP. They come from map/store XML. Enumerate them at runtime from `item.categoryNames`.
- **Specs:**
  - `StoreItemUtil.loadSpecsFromXML(item)` loads `item.specs` lazily, only if nil, and also for bundle items (L215-230). It calls `getSpecsFromXML(g_storeManager:getSpecTypes(), species, xml, ...)`, which fills `specs[specType.name]` (L238-255).
  - The spec type names that are registered:
    - Motorized (78/698) L26-32: `power`, `powerConfig`, `maxSpeed`, `fuel`, `electricCharge`, `methane`, `transmission`
    - Vehicle (91/888) L274-283: `workingWidth`, `workingWidthConfig`, `speedLimit`, `weight`, `additionalWeight`, `combinations`, `dailyUpkeep`, `age`, `operatingTime`, `slots`
    - PowerConsumer (78/778) L18: `neededPower`
    - FillUnit (78/672) L36-37: `capacity`, `fillTypes`
  - What each spec holds:
    - `specs.power` is the number from `vehicle.storeData.specs.power` (Motorized L3724-3726).
    - The real range comes from `Motorized.getSpecValuePower(storeItem, realItem, configs, saleItem, returnValues=true)`, which returns `min, max` from `configurations.motor[i].power` (L3730-3764).
    - `specs.workingWidth = {width, minWidth}` (Vehicle L4552-4560).
    - `specs.neededPower = {base, maxPower, config={...}}` (PowerConsumer L500-520). Use `getSpecValueNeededPower(storeItem, realItem, configs, saleItem, returnValues, returnRange)` (L529).
    - Capacity: `FillUnit.getSpecValueCapacity(storeItem, realItem, configs, saleItem, returnValues, returnRange)` (L2874). Fill types: `FillUnit.getSpecValueFillTypes(storeItem, realItem, configs)` returns fill type indices (L3113-3151).
    - `specs.combinations` is a list of `{xmlFilename, filterCategories, filterSpec, ...}` (Vehicle L4808-4842). `g_storeManager:getItemsByCombinationData(data)` exists (definition only).
- **Attacher joint types without loading the vehicle:** no spec or helper exists, so this is a GAP. Attachable reads `vehicle.attachable.inputAttacherJoints.inputAttacherJoint` with `#jointType` and maps it through `AttacherJoints.jointTypeNameToInt` (Attachable (78/628) L875-898, key string at txt 1419). That path could be read with `XMLFile.load(name, item.xmlFilename, item.xmlSchema)`, the same pattern as StoreItemUtil L217, but doing it this way is UNVERIFIED.
- **Default configs:**
  - `StoreItemUtil.getDefaultConfigId(item, configName)` calls `ConfigurationUtil.getDefaultConfigIdFromItems(item.configurations[name])` (StoreItemUtil L107-109). That returns the first item that is `isDefault` and selectable, otherwise the first selectable one, otherwise 1 (ConfigurationUtil (?/172) L637-655).
  - `item.defaultConfigurationIds` already holds the defaults (StoreManager L1018).
  - Price: `StoreItemUtil.getDefaultPrice(item, configs)` is the base price plus the prices of the chosen configurations (L116-158).

## 2. Buying a vehicle
- **BuyVehicleData (75/608).** Its own description: "set by the ShopConfigScreen and use in BuyVehicleEvent and ChangeVehicleConfigEvent".
  - `BuyVehicleData.new()` (L20-34) sets these fields:
    - `storeItem=nil`, `isFreeOfCharge=false`, `configurations={}`, `boughtConfigurations={}`, `configurationData={}`
    - `leaseVehicle=false`, `ownerFarmId=AccessHandler.EVERYONE`, `licensePlateData=nil`, `saleItem=nil`, `price=0`
  - Setters:
    - setStoreItem L55; setIsFreeOfCharge L62
    - `setConfigurations(configs, bought)` also marks each chosen config as bought (L70-80)
    - setConfigurationData L86; setLeaseVehicle L93; setOwnerFarmId L100; setLicensePlateData L107; setSaleItem L114; setPrice L121
  - `updatePrice()` sets `price = g_currentMission.economyManager:getBuyPrice(storeItem, configurations, saleItem)`. If leasing, it uses `getInitialLeasingPrice(price)` instead (L127-132).
  - `isValid()` requires `storeItem ~= nil` (L40-49). `getIsLimitedObjectPurchase()` returns `isBale, isPallet` from `vehicle.multipleItemPurchase` (L139-146).
  - `buy(storePlaces, usedStorePlaces, callback, target, args)` (L201-212) builds a VehicleLoadingData and calls these on it, in order:
    1. setStoreItem
    2. setConfigurations
    3. setConfigurationData
    4. setLoadingPlace(storePlaces, usedStorePlaces)
    5. setPropertyState(LEASED or OWNED)
    6. setOwnerFarmId
    7. setSaleItem
    8. `load(self.onBought, self, {callback=..., callbackTarget=..., callbackArguments=...})`
  - **buy() does not touch money in the shown code.** `onBought` is not documented (GAP).
- **BuyVehicleEvent is not documented (GAP).** Only these pieces are visible:
  - `BuyVehicleEvent.newServerToClient(BuyVehicleEvent.STATE_TOO_MANY_PALLETS, buyData)`, PalletFillerBuyPalletEvent (?/83) L65-67.
  - It is published on g_messageCenter. VehicleSystem subscribes with `onVehicleBuyEvent(errorCode, leaseVehicle, price)` (VehicleSystem (91/897) L67, L634).
  - The client→server constructor, STATE_SUCCESS, STATE_NO_SPACE and STATE_NOT_ENOUGH_MONEY: GAP. PlaceableVehicleBuyingStation loads failure messages for noSpace, noPermission and noMoney (78/768 L113-117), which suggests those states exist; UNVERIFIED.
- **ShopConfigScreen / ShopController buy path:** GAP (not documented).
- **The server-side spawn is VehicleLoadingData (91/893):**
  - `new()` defaults (L27-66): propertyState=OWNED, ownerFarmId=EVERYONE, isRegistered=true, addToPhysics=true.
  - `setStoreItem` expands bundles into `self.vehicles` (L85-102).
  - `setFilename(xml)` looks up the store item (L71-79).
  - `setLoadingPlace(places, usedPlaces, spawnOffset?, ignoreMinSize?)` returns false and sets `validLocation=false` if `PlacementUtil.getPlace` finds no room (L507-552).
  - `setPosition(x, y, z, offset)` and `setRotation` are an alternative to a loading place.
  - `load(cb, target, args)` (L559-590) registers with `vehicleSystem:addPendingVehicleLoad`. **The shown code does not re-check `validLocation`**, so check the return of setLoadingPlace yourself.
  - Callback signature: `cb(target, loadedVehicles, loadingState, args)` with `VehicleLoadingState.OK` or `ERROR` (L567-577).
  - Proven direct uses with no shop involved:
    - `VehicleSystem:consoleCommandAddPallet`: setFilename, setPosition, setPropertyState(OWNED), setOwnerFarmId, load (L943-952)
    - `AbstractMission:spawnVehicle`: setFilename, setConfigurations, setLoadingPlace(storeSpawnPlaces, usedStorePlaces), setPropertyState(MISSION), setOwnerFarmId, load (AbstractMission (59/560) L560-578)

## 3. Spawn location and finding the new vehicle
- The shop spawn places are `g_currentMission.storeSpawnPlaces` and `g_currentMission.usedStorePlaces`, used by AbstractMission L569 and L975-976. How they are loaded (loadSpawnPlaces) is GAP.
- Space pre-check pattern from `AbstractMission:isSpawnSpaceAvailable` (L971-998):
  - `StoreItemUtil.getSizeValues(xml, "vehicle", item.rotation, configs)`
  - clamp the size to `VehicleLoadingData.MIN_SPAWN_PLACE_*` and add `SPAWN_WIDTH_OFFSET`
  - `PlacementUtil.getPlace(places, size, usedPlaces)`, then `PlacementUtil.markPlaceUsed(usedPlaces, place, width)`
  - PlacementUtil itself is undocumented; it is only seen being called.
- Station-specific spawn places: `spec.spawnPlaces` comes from `PlacementUtil.loadPlaceFromXML` (PlaceableVehicleBuyingStation L100-105).
- **Identifying the new vehicle:** the load callback's `loadedVehicles` list is the most reliable way (VehicleLoadingData L576; AbstractMission:onSpawnedVehicle L582-599).
  - Fallbacks:
    - `vehicleSystem.vehicles` (L22, L238)
    - the unique ID assigned in `addVehicle` (L221-250), and `getVehicleByUniqueId`
    - `MessageType.VEHICLE_ADDED` published at L247
    - `Vehicle:getUniqueId` L4421, `getPropertyState` L4235, `getOwnerFarmId` (VehicleSystem L639)
  - Caveat for multi-pallet items: the bought "root" is a MultipleItemPurchase vehicle. It loads the real pallets as separate vehicles with their own VehicleLoadingData and **deletes itself** on its first non-shop update (MultipleItemPurchase (78/702) L139-145, L183-206, L262-265). So the callback's list holds the root, not the pallets.

## 4. Money
- Balance:
  - `g_farmManager:getFarmById(id):getBalance()` is a proven call (AIJob (53/535) L175-177).
  - `g_currentMission:getFarmId()` (VehicleSystem L635, L943) and `FarmManager.SINGLEPLAYER_FARM_ID` (VehicleSystem L566) are also proven.
  - `g_currentMission:getMoney` and `farm.money` do not appear anywhere in the docs (GAP).
- Charging: the proven pattern is `g_currentMission:addMoney(-amount, farmId, MoneyType.X, true[, true])`. Examples: Motorized L2217, PlaceableFence L93, VehicleSystem L572 (`SHOP_VEHICLE_SELL`).
  - `MoneyType.SHOP_VEHICLE_BUY` is never seen in the docs (UNVERIFIED). `SHOP_PROPERTY_BUY` and `PURCHASE_SEEDS` / `PURCHASE_FERTILIZER` / `PURCHASE_CONSUMABLES` are seen.
  - How the shop purchase itself deducts money is inside BuyVehicleEvent or onBought, which are GAP. Because `BuyVehicleData:buy` does not charge, a direct buy() would spawn the vehicle **for free** unless we call addMoney ourselves.
- Pre-checks:
  - Price: `economyManager:getBuyPrice(item, configs, saleItem)` (BuyVehicleData L127).
  - Compare the price with `getBalance()`.
  - Slots: `g_currentMission.slotSystem:hasEnoughSlots(storeItem)` (VehicleSystem L571).
  - Pallet limit: `slotSystem:getCanAddLimitedObjects(SlotSystem.LIMITED_OBJECT_PALLET, 1)` (PalletFillerBuyPalletEvent L62; PalletSpawner L150).
  - Permission: `g_currentMission:getHasPlayerPermission(Farm.PERMISSION.BUY_VEHICLE)` (PlaceableVehicleBuyingStation L124).

## 5. Pallets, big bags and supplies
- Pallets and big bags are **vehicle store items**. They load through VehicleLoadingData like any vehicle and carry `spec_pallet` / `spec_bigBag` (VehicleSystem L643).
  - `fillType.palletFilename` points to the pallet's store item: `g_storeManager:getItemByXMLFilename(fillType.palletFilename)` (PlaceablePalletBuyingStation (78/752) L85-96; VehicleSystem L855-857, L892).
  - BuyVehicleData has a pallet/bale path (`getIsLimitedObjectPurchase`). The shop therefore uses BuyVehicleData/BuyVehicleEvent for pallets too, with the limited-object check; the event part is UNVERIFIED.
- The proven way to spawn a filled pallet is `consoleCommandAddPallet` (VehicleSystem L853-955):
  1. VehicleLoadingData with setFilename(palletFilename), setPosition, setPropertyState(OWNED), setOwnerFarmId, then load.
  2. In the callback: `vehicle:addFillUnitFillLevel(farmId, fillUnitIndex, amount, fillTypeIndex, ToolType.UNDEFINED, nil)`, after checking `getFillUnitSupportsFillType`.
  - A pallet's starting fill comes from the XML `fillUnit#startFillLevel` / `#startFillType` (FillUnit L85-86, L1867-1870). Whether shop-bought pallets arrive full is UNVERIFIED.
- The other buying paths are not shop purchases:
  - PlaceablePalletBuyingStation: `openShop` and a PalletSpawner with `spawnPallet` / `getOrSpawnPallet`, results `RESULT_SUCCESS` / `RESULT_NO_SPACE` (PalletSpawner (67/595) L253, L263). It needs a placed station.
  - BuyPlaceableData (75/607) `buy(cb, target, args)` builds PlaceableLoadingData (L200-209). It is for placeables only.
  - BuyHandToolData (75/606) is for hand tools.
  - BuyObjectEvent: not documented (GAP).

## Summary table

| capability | exact call | source (class + lines) | status |
|---|---|---|---|
| list store items | `g_storeManager:getItems()` | StoreManager L676-678 | PROVEN |
| item by xml | `g_storeManager:getItemByXMLFilename(xml)` | StoreManager L695-701 | PROVEN |
| item fields (name, price, species, categoryNames, canBeSold, showInStore, configurations, defaultConfigurationIds, bundleInfo) | field reads | StoreManager L933-1035, L1174 | PROVEN |
| is vehicle | `StoreItemUtil.getIsVehicle(item)` | StoreItemUtil L17-19 | PROVEN |
| category lookup | `g_storeManager:getCategoryByName(n)` | StoreManager L428-433 | PROVEN |
| list all categories | `g_storeManager.categories` (no getter) | StoreManager L41 | UNVERIFIED |
| category name constants | none in docs | n/a | GAP |
| load specs | `StoreItemUtil.loadSpecsFromXML(item)`, then `item.specs.<name>` | StoreItemUtil L215-230 | PROVEN |
| power | `item.specs.power`; `Motorized.getSpecValuePower(item,nil,nil,nil,true)` returns min,max | Motorized L26-32, L3724-3764 | PROVEN |
| working width | `item.specs.workingWidth.width` | Vehicle L277, L4552-4560 | PROVEN |
| needed power | `item.specs.neededPower.base` / `.maxPower` | PowerConsumer L18, L500-520 | PROVEN |
| capacity / fill types | `FillUnit.getSpecValueCapacity(...)` / `getSpecValueFillTypes(...)` | FillUnit L36-37, L2874, L3113 | PROVEN |
| attacher joint types without loading | read xml `vehicle.attachable.inputAttacherJoints.inputAttacherJoint#jointType` | Attachable L875-898 | GAP (no API) |
| default config ids | `item.defaultConfigurationIds` / `StoreItemUtil.getDefaultConfigId(item,name)` | StoreManager L1018; StoreItemUtil L107-109; ConfigurationUtil L637-655 | PROVEN |
| price | `g_currentMission.economyManager:getBuyPrice(item, configs, saleItem)` | BuyVehicleData L127 | PROVEN (usage) |
| buy data | `BuyVehicleData.new()` + setters | BuyVehicleData L20-121 | PROVEN |
| spawn via buy data | `buyData:buy(storePlaces, usedStorePlaces, cb, target, args)` (no money) | BuyVehicleData L201-212 | PROVEN |
| spawn directly | `VehicleLoadingData.new()`, setFilename / setStoreItem, setLoadingPlace or setPosition, setOwnerFarmId, setPropertyState, `load(cb,t,a)` | VehicleLoadingData L27-102, L507-590; AbstractMission L560-578 | PROVEN |
| load callback | `cb(target, vehicles, VehicleLoadingState.OK/ERROR, args)` | VehicleLoadingData L576 | PROVEN |
| shop spawn places | `g_currentMission.storeSpawnPlaces`, `.usedStorePlaces` | AbstractMission L569, L975 | PROVEN (usage) |
| BuyVehicleEvent ctor / STATE_* codes | only `newServerToClient(STATE_TOO_MANY_PALLETS, data)` seen | PalletFillerBuyPalletEvent L67 | GAP |
| ShopConfigScreen / ShopController buy | n/a | n/a | GAP |
| find new vehicle | callback list; `vehicleSystem.vehicles`; `getVehicleByUniqueId`; `MessageType.VEHICLE_ADDED` | VehicleSystem L221-250 | PROVEN |
| balance | `g_farmManager:getFarmById(id):getBalance()` | AIJob L175-177 | PROVEN (usage) |
| getMoney / farm.money | n/a | n/a | GAP |
| charge | `g_currentMission:addMoney(-p, farmId, MoneyType.X, true)` | Motorized L2217; VehicleSystem L572 | PROVEN (usage) |
| MoneyType.SHOP_VEHICLE_BUY | not seen | n/a | UNVERIFIED |
| slot / pallet limit | `slotSystem:hasEnoughSlots(item)`; `getCanAddLimitedObjects(SlotSystem.LIMITED_OBJECT_PALLET,1)` | VehicleSystem L571; PalletFillerBuyPalletEvent L62 | PROVEN (usage) |
| pallet store item for fill type | `g_storeManager:getItemByXMLFilename(fillType.palletFilename)` | PlaceablePalletBuyingStation L85-96 | PROVEN |
| fill a spawned pallet | `vehicle:addFillUnitFillLevel(farmId, fu, amt, ft, ToolType.UNDEFINED, nil)` | VehicleSystem L919-941 | PROVEN |
| multi-item pallet purchase | root deletes itself; pallets load separately | MultipleItemPurchase L139-265 | PROVEN |
| BuyObjectEvent | n/a | n/a | GAP |
