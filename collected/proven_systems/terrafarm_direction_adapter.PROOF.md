# TerraFarm low-speed direction adapter — PROVEN for two live tool types

## Purpose

Allow a Farmhand-controlled machine to use TerraFarm's native ground-work mechanics during verified low-speed forward travel. This is an **optional TerraFarm adapter**, not native Farmhand terrain physics.

## Required inputs and outputs

- Inputs: a loaded server-side TerraFarm `spec_machine`, its native activation/state methods, and independently observed forward displacement and speed from the root vehicle.
- Output: activated/available state while moving; `stop()` deactivates the tool and restores its original TerraFarm state. The caller must measure terrain/material change as the actual work postcondition.
- TerraFarm's own `MachineWorkArea` performs the deformation. No Farmhand mesh/terrain solver is added.

## Capabilities and machines physically tested

Save 1, normal FS25 physics, same generic lab adapter logic on two different loaded TerraFarm machine types attached to one D11 root:

| Tool | TerraFarm type | Observed travel | Fixed-world terrain change | Result |
|---|---|---:|---:|---|
| D11 ripper | `ripper` | 0.406 m | −0.165 to −0.248 m at three nodes | active during motion; deactivated afterward |
| D11 blade | `leveler` | 0.406 m | up to −0.503 m at 14 nodes | active during motion; deactivated afterward |

The adapter became available after 0.104 m of physical travel in both cases. The first run observed 0.807 km/h at adapter activation. The blade run reported `fillBefore=0` and `fillAfter=0`; the destination of removed terrain material was **not** established.

## Observed failures that required it

- TerraFarm refused a stationary activated ripper: `getIsAvailable=false`.
- A 1.003 m native AI-controlled D11 run peaked at 1.399 km/h but TerraFarm remained unavailable. A 2.005 m run peaked at 1.406 km/h with the same result. Installed GIANTS `Drivable:getDrivingDirection` returns zero below 1.5 km/h when its player input axis is zero; the native AI drive call does not set that axis.
- TerraFarm's own `DrivingDirectionMode.IGNORE=4` is a supported setting. Switching to it **after real movement is observed** made TerraFarm available and produced measured terrain deformation. It was restored on termination.

## Live evidence and dependencies

- `.fs25-dev/evidence/terrafarm-20261103/ripper-game-log-extract.txt` and `blade-game-log-extract.txt` contain the exact start, adapter, availability, fixed-coordinate terrain samples, and terminal records.
- Installed TerraFarm source snapshot: toolkit `references/provider-sources/TerraFarm/scripts/MachineState.lua`, `MachineWorkArea.lua`, and `specializations/Machine.lua`, snapshot commit `b50e677cdef062d605ab188f8982ae1faa2789e7`; loaded Save 1 mod behavior was observed live.
- The extracted file uses TerraFarm's `spec_machine`, `setMachineActive`, `setMachineState`, and `MachineState:clone`. It has no dependency on Courseplay/AutoDrive or a named vehicle.
- Focused probe tests (38 passed) and Lua module checks cover code shape only; the terrain proof is live FS25 evidence.

## Known limits and refusals

- **PROVEN:** low-speed native TerraFarm ground change on the tested ripper and blade, with physical travel and same-world-coordinate terrain deltas.
- **PARTIALLY PROVEN:** state restoration. The live lab log recorded `stateRestored=true` after the native setter returned and `active=false`; it did not independently log the final state value. The collected `stop()` checks that value. Retest after integration.
- **UNVERIFIED:** another root machine, Cat 395 bucket excavation, terrain volume/material conservation, output material placement, soil fill amount, non-flat terrain, reverse work, and any TerraFarm-free ground behavior.
- Refuses absent TerraFarm capability/permission and unobserved forward movement. The caller must stop on no progress, contact, STOP, timeout, or takeover; `IGNORE` must never be left active as a hidden setting.
- This adapter does not authorize a work success from `getIsAvailable` alone. Farmhand must verify the requested terrain and resource outcome.
- The isolated file was extracted from the live-proven lab sequence; it has not itself been packaged and run in FS25. Retest its interface when integrated.

## Why smaller failed / what not to migrate

Activation alone could not work at the low speeds observed: TerraFarm's direction gate stayed false even while the D11 moved. The smallest extra mechanism is a temporary native direction-mode change linked to observed movement, followed by restoration. Do not migrate the lab marker files, Save 1 IDs/coordinates, repeated reload workflow, full `FarmhandProbe.lua`, or the diagnostic drive loop. Do not copy TerraFarm's deformation engine into Farmhand.

## Complexity

One optional Lua file, no persistent calibration and O(1) state per active operation. External dependency: TerraFarm only for TerraFarm-style terrain work. Normal Farmhand driving and farming must remain independent of it.
