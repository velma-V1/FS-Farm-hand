# Installed-capability inventory (the user's FS25 profile)

Source: each mod's own `modDesc.xml` in the profile's `mods` folder (version, title,
script/specialization/store-item counts) and the game log's `Load mod:` lines for
save 3, read 2026-10-19 (build 6c74538). 114 mods are installed; save 3 loads only
five. A mod that is installed but not loaded in the save offers nothing in it:
enabling it is the user's own savegame mod selection, which Farmhand never changes.

Status key: LOADED (active in save 3), INSTALLED (present, not active in save 3).
"Adapter" names Farmhand's integration; "Qualification" is the live result.

## Active in save 3

| Mod | Version | Capability it provides | Farmhand adapter | Compatibility | Qualification |
| --- | --- | --- | --- | --- | --- |
| GIANTS FS25 (native) | 1.24.1.0 (MS Store) | GOTO navigation, field-work helper (AIJobFieldWork), attach/detach, fill units, field state, farmlands, money, helper purchase of seed/fertilizer/fuel | `mod/FarmhandHitch.lua`, `mod/FarmhandBridge.lua` (navigate, stage, fieldwork, attach, detach), `mod/FarmhandObserve.lua` | GDN script 1.20 evidence (adapters/EVIDENCE.md) | NAVIGATE, ATTACH, DETACH: REAL-FS25-PASS; FIELD_WORK: in qualification |
| FS25_AutoDrive | 3.0.1.4 | Marker-to-marker driving, destinations (markers with names) | `drive` op; destinations in every snapshot | `AutoDrive:GetAvailableDestinations`, `StartDriving` present (checked per call) | marker travel: UNVERIFIED live in save 3 (no markers used yet) |
| FS25_FollowMe | 1.2.0.0 | One vehicle follows another / the player | none yet (FOLLOW) | not inspected | none |
| FS25_Chipping_Norton | 1.0.0.2 | The map (fields, farmlands, store, sell points) | observation | n/a | fields observed live |
| FS25_Deutz_Series_9_2019 | 1.0.0.2 | Tractor (vehicle 172) | equipment | n/a | GOTO PASS / stalls on its 1.2 km route |

## Installed, not active in save 3 (relevant to the product scope)

| Mod | Version | Capability | Needed for | Status |
| --- | --- | --- | --- | --- |
| FS25_UniversalAutoload | 1.1.1.0 | Pallet / big bag / bale autoloading on trailers | LOAD, UNLOAD of pallets and bales | INSTALLED, not in save 3 |
| FS25_FarmFillStations | 1.1.0.0 | Placeable fill stations (16 store items) | FILL (seed, fertilizer, fuel) | INSTALLED, not in save 3 |
| FS25_TowingChain | 1.0.0.0 | Towing chain with hook (2 specializations, 3 items) | RECOVER | INSTALLED, not in save 3 |
| FS25_0_THBetterWinch | 1.2.0.3 | Winch | RECOVER ("bring something with a winch") | INSTALLED, not in save 3 |
| FS25_storeDeliveries | 1.0.7.0 | Store purchases delivered to a chosen place | BUY / retrieval | INSTALLED, not in save 3 |
| Hashy_GMC3500HD | 1.2.0.1 | 2024 GMC Sierra Denali 2500-3500HD | "take the GMC" | INSTALLED, not in save 3 |
| FS25_JJ_GooseneckTrailer | 1.0.0.0 | Gooseneck trailer (14 specializations) | "the GMC and gooseneck" | INSTALLED, not in save 3 |
| FS25_LizardComboTrailer | 1.0.0.7 | Autoloading bale/combo trailer | LOAD bales | INSTALLED, not in save 3 |
| FS25_autoloadWood_crossplay | 1.0.0.0 | Auto load wood | LOAD logs | INSTALLED, not in save 3 |
| FS25_BulkFill | 1.0.0.0 | Bulk filling | FILL | INSTALLED, not in save 3 |
| FS25_sellEverything | 1.0.0.2 | Sell points accepting more fill types | SELL | INSTALLED, not in save 3 |
| FS25_UsedEquipmentYards | 1.0.1.3 | Used-equipment dealers | BUY | INSTALLED, not in save 3 |
| FS25_ProductionPointLeasing | 1.0.0.0 | Production leasing | productions | INSTALLED, not in save 3 |
| FS25_interactiveControl | 1.2.0.1 | Interactive vehicle controls | none | INSTALLED, not in save 3 |

No installed mod provides field-work courses (no Courseplay): FIELD_WORK uses GIANTS'
own helper. Installed trailers, trucks and pickups (Lizard, Landoll, Kenworth, Ford,
Chevrolet, Superduty, TrailTech, Brandt, Pitts) are equipment, not capabilities;
they become usable when the save loads them.

## What this means for the product scope

- Field work, supplies for it (GIANTS helper buys seed and fertilizer: all three
  helperBuy settings are on in save 3) and tool changes need nothing beyond save 3.
- Pallet/bale logistics (UAL), farm fill stations, towing/winch recovery and the
  GMC + gooseneck requests need those mods active in the save used. That is the
  user's savegame mod selection; until then Farmhand reports the capability as
  unavailable rather than rebuilding it.
