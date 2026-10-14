# Compatibility boundary

`World` is our internal Python port, not an API claimed to exist in any mod.
No external adapter is implemented; `mod/FarmhandBoundary.lua` is fail-closed.
Every real adapter needs version-pinned upstream inspection and runtime evidence.

| Upstream reference | Intended reuse | Current status |
| --- | --- | --- |
| [GIANTS GDN](https://gdn.giants-software.com/) | Authoritative inventory, ownership, attachment and game observations | API mapping unverified |
| [AutoDrive FS25](https://github.com/Stephan-S/FS25_AutoDrive) | Existing transport execution where supported | No integration; approach/coupling and destination mapping unverified |
| [Courseplay FS25](https://github.com/Courseplay/Courseplay_FS25) | Future existing fieldwork delegation | Outside AC-001 |
| [Follow Me](https://github.com/DeckerMMIV/FarmSim_Mod_FollowMe) | Future following delegation | Outside AC-001 |
| [Universal Autoload](https://github.com/loki79uk/FS25_UniversalAutoload) | Future loading delegation | Outside AC-001; not needed to tow a wagon |
| [Farmhand Companion](https://github.com/getflowkits/fs25-farmhand-mod) | Inspect state/action interface concepts | No code copied or dependency added |

References identify projects only; they do not establish supported functions,
compatible versions, or permission to redistribute. Check each exact license and
version before implementation. Do not fork/vendor merely to create a placeholder.

A real adapter must enforce farm authority, reservation ownership, live availability
and command preconditions at action time; reject unsupported operations; await
bounded completion; expose fresh observations; and implement confirmed stop.
Never fall back to returning synthetic state or success when a mod is missing.
