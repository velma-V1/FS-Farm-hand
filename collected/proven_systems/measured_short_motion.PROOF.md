# Measured short motion — PARTIALLY PROVEN candidate

## Capability and need

Predict a powered vehicle's root poses for a short commanded turn using its
loaded native steering reference. A root-centered arc made the first wrong
candidate endpoint on two different articulated trucks. The wrong endpoint
prevented the measured articulated clearance check from owning route decisions.

## Interface and dependencies

`sampleRootArc(env, vehicle, signedLength, curvature)` returns root poses at
intervals no greater than 0.25 m, or `nil, reason`. It reads only live root and
`spec_wheels.steeringCenterNode` transforms. It stores no calibration or state.
The caller must propagate each pose through the observed rig, check actual
clearance, execute a bounded native control input, and compare actual motion.
This file does none of those tasks and never authorizes motion by itself.

## Physical evidence

Save 1, normal FS25 physics, no external navigation owner or scenery ghosting:

| Rig | Trial | Old powered-root error | Measured-reference error | Actual result |
|---|---|---:|---:|---|
| GMC + deckover + loaded tractor/implement | 2 m forward turn | 0.350 m | 0.092 m | DONE, no final contact hit |
| GMC + deckover + loaded tractor/implement | 0.5 m reverse turn | about 0.16 m | 0.088 m | DONE, no final contact hit |
| 379 + LB trailer | 2 m forward turn | 0.576 m | 0.210 m | DONE, no final contact hit |
| 379 + LB trailer | 0.5 m reverse turn | 0.161 m | 0.088 m | DONE, no final contact hit |

The sampled reference-based candidates and articulated propagation were
prospective before each physical run. Installed FS25 1.25.0.0
`Wheels:getSteeringRotTimeByCurvature` uses `steeringCenterNode`. Live nodes
were about 2.226 m behind the GMC root and 3.960 m behind the 379 root.
Evidence: `.fs25-dev/evidence/reference-candidate-20261103/` and
`.fs25-dev/evidence/second-rig-reference-20261103/` plus
`adapters/EVIDENCE.md`.
The extracted module was loaded with Lua 5.4 via Lupa and its endpoints
matched all four logged lab candidate endpoints within 0.001 m at log
precision. This checks extraction fidelity; it is not another physics trial.

## Limits and refusals

- **PROVEN:** the reference-based local candidate chain completed the listed
  maneuvers on two materially different wheeled articulated rigs.
- **PARTIALLY PROVEN:** this predictor as a generally useful physical model.
  Steering transients and slip remain; the 379 forward error was 0.210 m.
- **UNVERIFIED:** arbitrary wheel/track configurations, narrow-gap margin,
  multi-trailer propagation, long route search, and this extracted file inside
  the game package. The live probe used the same formula, not this file.
- A read-only census found native `(0,0)` steering centers on three tracked
  families and a missing center on the zero-wheel train. The D11's 0.5 m
  reverse realized only +0.01195 rad yaw against +0.04 nominal, so center
  availability alone cannot justify tracked predictive clearance. The caller
  needs bounded observed-response correction and a safe error margin.
- Refuses a missing native reference, invalid command, more than 3 m, or
  curvature magnitude over 0.3. The caller must refuse when rig propagation or
  collision measurement fails. A predicted clear state is never completion.

## Complexity removed and why smaller failed

This replaces root-centered candidate arcs and per-machine root-to-steering
offsets. One root-centered arc was insufficient on both tested trucks; the
offsets differed materially. The native node supplies the needed value at
runtime. Do not migrate the lab probe, Save 1 IDs, or the legacy route search.
