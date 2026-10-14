# Compatibility boundary

`World` (`world.py`) is our internal Python port, not an API claimed to exist in any
mod. `fs25.py` maps validated FS25 snapshot frames into that contract and fails
closed wherever upstream data is unproven. No real `World` adapter performs physical
work yet; `mod/FarmhandBoundary.lua` refuses every physical operation.

**[EVIDENCE.md](EVIDENCE.md) is the authoritative record** of which upstream
project provides each capability, with pinned commits, files, line numbers, and
`PROVEN` / `UNVERIFIED` / `GAP` states. Summary:

| Upstream | Reuse | Status |
| --- | --- | --- |
| GIANTS FS25 API (as used by the mods below) | Inventory, ownership, position, attachment, speed, attach mechanism | Calls PROVEN by upstream use; untested in our FS25 runtime |
| [AutoDrive](https://github.com/Stephan-S/FS25_AutoDrive) (MIT) | Delivery travel to map markers, destinations, stop | API PROVEN; marker-only; not yet called |
| [Farmhand Companion](https://github.com/getflowkits/fs25-farmhand-mod) (MIT) | Local IPC design: named pipe out, XML file in | Design reused; no code copied |
| [Courseplay](https://github.com/Courseplay/Courseplay_FS25) (GPL-3.0) | Evidence of the game's attach API usage | Reference only; never copied |
| [Follow Me](https://github.com/DeckerMMIV/FarmSim_Mod_FollowMe) | Follows a leader vehicle only | Not used |
| [Universal Autoload](https://github.com/loki79uk/FS25_UniversalAutoload) | Loading | Not needed to tow a wagon |

A real adapter must enforce farm authority, reservation ownership, live availability
and command preconditions at action time; reject unsupported operations; await
bounded completion; expose fresh observations; and implement confirmed stop.
Never fall back to returning synthetic state or success when a mod is missing.
