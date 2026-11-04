# Bounded moving-tool rotation — PROVEN for short pulses on three live families

## Purpose

Move one discovered FS25 `Cylindered` rotational tool by a short, bounded angle, observe the actual angle, and stop safely. This is an actuator primitive, not an endpoint solver or a digging/forestry task.

## Required inputs and outputs

- Inputs: live server vehicle, one active `spec_cylindered.movingTools` entry, desired absolute node angle within 0.02 rad of the observed angle, repeated frame `dt` in milliseconds.
- Output: `RUNNING`, `DONE` with actual angle/error, or `FAILED` with the reason/error. `stop(session)` clears the native target and stops only a motor this action started.
- The caller must observe collision, tool endpoint, and world/material effects separately. This module does not claim those outcomes.

## Capabilities and machines physically tested

The same generic lab target-state logic in `mod/FarmhandProbe.lua` controlled:

| Live Save 1 machine | Controlled DOF | Command | Actual | Angle error | Observed downstream motion |
|---|---|---:|---:|---:|---|
| D11 dozer | blade raise/lower node | −0.02000 rad | −0.02011 rad | 0.00011 rad | blade node 0.0204 m |
| Cat 395 excavator | boom | +0.02000 rad | +0.01915 rad | 0.00085 rad | stick 0.1418 m, bucket 0.1067 m |
| Komatsu 951 forestry harvester | arm | +0.02000 rad | +0.02061 rad | 0.00061 rad | cutter node 0.0689 m |

All three had normal loaded FS25 machine physics, no AI owner, and no vehicle travel command. The lab started the engine through native `startMotor`, used the loaded `Cylindered` target fields, observed live node transforms, cleared the target, and stopped the engine it started. The exact `getLastSpeed()` after the D11 result was 0.576; this is not proof of dozer locomotion or perfect stationary root pose.

## Observed failures that required this implementation

1. A target sent with the D11 motor off produced **zero** blade angle change in one second.
2. A raw radian target of `0.02` on the limited D11 blade drove the blade to its +0.08430 rad limit. Installed GIANTS `Cylindered.getMovingToolState` defines limited-tool state as `(angle - rotMin)/(rotMax - rotMin)`; the native target consumes that state.
3. `getMovingToolState` is not registered as a vehicle method in this installed version. Calling it as `vehicle:getMovingToolState(...)` failed on the Cat 395. The collected code computes the same conversion directly from the live tool fields.

## Live evidence

- Installed game dataS: FS25 1.25.0.0, toolkit `references/installed-datas/scripts/vehicles/specializations/Cylindered.lua`, `getMovingToolState` and `onUpdate`. The toolkit source is decompiled bytecode; live behavior qualified the interpretation.
- Save 1 game log, 2026-11-03 09:44:13–09:44:52 local, `machine actuator START/affected/RESULT` records for lab IDs 630, 629, 628. IDs are only evidence locators and do not appear in the collected mechanism.
- Focused probe tests: `python -m unittest tests.test_lua_probe -q` — 38 passed on 2026-11-03. These tests do not establish physical proof.

## Known limits and refusals

- **PROVEN:** one short limited rotation on the three listed machine/control families, with actual angle observed.
- **UNVERIFIED:** multi-DOF endpoint positioning, repeated pulses, slew, translation, loading, grading, digging, cutting, tree mass conservation, TerraFarm interaction, collision-safe tool sweep, and control while driving.
- Refuses inactive/unreadable control, non-server authority, AI-owned or moving vehicle, a target outside limits, a move beyond 0.02 rad, or an already targeted tool. Fails on motor timeout or missed target.
- `Cylindered` and GIANTS native motor methods are required. A mod with a different actuator API needs a small adapter to this control contract; this file does not pretend to control it.
- The isolated file is extracted from the live-proven lab mechanism; it has not yet been loaded as a packaged FS25 mod itself. Retest this isolated module when wiring it into a clean build.

### 2026-11-03 unlimited-rotation boundary correction

**FAILED in the lab:** A 951 cabin rotation began at +0.03725 rad and received
an ordinary target of −0.06275 rad. Installed `Cylindered:setToolRotation`
wraps an unlimited joint into `[0, 2π)`, so native
`getMovingToolState` never approached that negative target. The lab timeout
cleared the target, but only after an apparent 5.41438 rad angle change and
several metres of head movement. The tree stayed standing; no cut command
ran. This exposes a boundary that also existed in the collected code.

The collected implementation now wraps unlimited target angles into
`[0, 2π)`, preserves the requested movement direction, and compares
circular progress. It refuses a joint with only one readable limit. A Lua
runtime regression test simulates the installed GIANTS wrap behavior in
both directions. Subsequent live qualification is recorded below.

The live 951 recovery pulse exposed a second general issue: an unlimited
joint requested +0.100 rad and was observed at +0.10378 rad when the target
was cleared, but its settled angle advanced about +0.202 rad from the start.
Installed `Cylindered:onUpdate` continues decelerating `lastRotSpeed` after
input goes to zero. The extracted module now keeps the action open until the
observed angle stays quiet for 300 ms, then reports `DONE` only if the settled
target error is within 0.001 rad; otherwise it returns a measured failure.
A Lua regression simulates post-target coasting. Joint target attainment
is not the settled physical postcondition, and the caller must additionally
observe the work point and world effect. Subsequent live qualification of
the same lab rule is recorded below. This changes no machine-specific
knowledge or persistent state.

### 2026-11-03 settled braking and unlimited wrap qualification

Installed `Cylindered:onUpdate` continues `lastRotSpeed` after its target
clears. A live settle-only discriminator found D11 blade error 0.00030 rad,
Cat 395 boom error **0.01909 rad**, and 951 cabin error **0.01701 rad**. The
original immediate-angle results therefore did not prove settled accuracy.

The minimum shared correction reads the loaded joint's `lastRotSpeed` and
`rotAcceleration` and clears the target when the remaining angle reaches
`speed² / (2 × acceleration) + |speed| × dt / 2`. It then waits until the
angle is quiet for 300 ms (4 s cap) and reports the measured settled angle.
The caller receives DONE only within 0.002 rad; larger errors remain
truthful failures. No machine name, calibration table, or persisted state is
used. A simulated native acceleration/coast regression passes; it is not
physical proof.

| Live FS25 1.25.1.0 trial | Direction | Settled error | Result |
|---|---:|---:|---|
| D11 blade from changed pose | −0.020 rad | 0.00115 rad | DONE |
| Cat 395 boom from changed pose | +0.020 rad | 0.00126 rad | DONE |
| Komatsu 951 cabin from changed pose | +0.020 rad | 0.00165 rad | DONE |
| Cat 395 unlimited hook across zero | −0.020 rad | 0.00192 rad | DONE |
| Same hook across zero in reverse direction | +0.020 rad | 0.00013 rad | DONE |

These were bounded native-tool motions under ordinary physics, with the
motor started/stopped by the lab only when needed. The first positive hook
pulse stopped just below `2π`; a second independently started at 6.28317
rad, passed through zero, and settled after a true positive wrap. Raw log:
`.fs25-dev/evidence/settled-moving-tool-20261103/game-log-extract.txt`.

**PROVEN:** the lab's generic short-angle target, native-speed braking,
circular comparison, and settled-result logic on the listed joint families.
**PARTIALLY PROVEN:** the extracted module uses the same logic and passes
four Lupa regressions, but it has not been loaded as a packaged mod. Work
point placement, collision-safe tool sweep, multi-DOF correction, tree
cutting, and material change remain **UNVERIFIED**. A 0.002 rad joint result
is never by itself a successful physical task.

## Why smaller failed / what not to migrate

A bare `curTargetPosition = physicalAngle` was physically wrong on limited tools. Clearing a valid target only after the angle arrived was also wrong on accelerating joints: Cat and 951 coasted far beyond it. The minimum extra state is the live min/max interval, angle, native instantaneous speed/acceleration when available, motor readiness, and a settled result check. Without readable native acceleration, the module may still return a truthful target miss; that branch has no broad live qualification. Do not migrate the Save 1 IDs, marker files, full `FarmhandProbe.lua`, log inventory, reload workflow, or its experimental failure branches.

## Complexity

One Lua file, no persistent calibration or machine-specific table, O(1) work per frame during a bounded pulse. No external gameplay mod dependency for this core control path.
