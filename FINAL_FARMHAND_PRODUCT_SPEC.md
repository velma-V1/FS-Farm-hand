# FARMHAND — FINAL PRODUCT REQUIREMENTS
Owner directive: 2026-11-05. Authoritative desired end product; NOT a claim that these features exist.

This document preserves the full player-required Farmhand product. Read together with AGENTS.md. Current explicit player direction wins. Old experiments, test expectations, provider-era paths and earlier narrow specifications cannot silently remove, weaken or replace any requested capability.

## 1. Mission and game architecture

Farmhand is a standalone, local-first, high-performance, autonomous Farming Simulator 25 world operator, MULTI-WORKER fleet manager and optional conversational character companion. The player gives ordinary, spoken, shorthand, incomplete, messy or broad objectives. Farmhand observes, resolves intent and real resources, chooses equipment, deploys workers, executes, monitors, recovers and verifies actual game outcomes. The player observes rather than performing routine actions manually. Aim to perform nearly any task actually possible with installed games, maps and machinery, including novel compositions of machine capabilities rather than only hardcoded task templates.

Loop: PLAYER OBJECTIVE -> FAST INTENT -> LIVE WORLD/ CAPABILITIES -> JUST-ENOUGH PLAN -> DISPATCH CONCURRENT WORKERS -> PHYSICAL ACTION/ SUPERVISION -> QUICK RECOVERY/ REPLAN -> OBSERVED VERIFIED COMPLETION -> NEXT TASK.

Farmhand owns all work and navigation decisions. Compose GIANTS native physics, vehicle controls, navigation agents, geometry, joints, terrain, collisions, crop/economy systems, character and vehicle interaction, and Farmhand's existing close-quarters driving, manipulation and recovery. Reuse the smallest proven mechanism with the best real outcome; no mandatory redundant global planner, no inherited unnecessary GIANTS AI-job blockers. AutoDrive must never drive, plan, select routes/markers, unload, sell, edit routes/settings or otherwise execute for Farmhand, even as fallback. Only read whether the independently installed player's AutoDrive owns a vehicle to prevent takeover; leave that mod and its configuration alone. No compulsory third-party gameplay mod for advertised core functions.

## 2. Fast, authoritative world observation

Maintain an efficient, incremental, fresh, action-ready model of the real world. Distinguish known, stale, unavailable, unsupported and uncertain states; never fabricate successful observations.

- Owned/accessible fields: polygon/ownership, crop and growth, weather relevance, stones after plowing, soil preparation, lime, weeds, spraying/fertilizer level, needs rolled, seeded, mulched, harvested, harvest readiness and residue, and exact work progress.
- Available and active contracts: operation, reward, land/requirements, progress, leased/rented vs owned machines, acceptance, finishing, settlement and lease return.
- Equipment and fleet: position, heading, speed, category, capabilities, usable controls, fuel/charge, wear/repair, fill and materials, steering, attachments, articulation, belted cargo, worker/control ownership and machine readiness.
- World geometry/interactions: roads/trails, field entrances, obstacles, collision, terrain/slope/height, trees/logs/stumps, gates/doors/ramps/covers, stores, sell points, prices, productions, animal pens, buildings, buildable locations, quarry/mine sites and map-specific mechanisms.
- Supplies/economics: inventory, fill types, stock, money, financing/lease, weather/forecast when actually available, crop calendar, animal needs, factory inputs/outputs, throughput, storage, profitability.
- Automatic capability discovery from installed machine specializations, controls, attachments and map/mod interfaces: drive, steer, reverse, raise/lower, fold, grab, lift, tip, discharge, load, cut, delimb, grapple, chainsaw, dig, grade, place, sell, etc. Verify available actions instead of assuming every map/excavator supports excavation.

Do not hardcode the player's position from old testing. At the time of this directive the player says their character is INSIDE THE BOSS HOSS SERIES 9. Use current live observation thereafter.

## 3. Command comprehension, autonomy modes and planning

Understand even 'gmc,seeds,barn', '379/cat/quarry' or 'bring a load of rock': infer the real intended destination, required materials and source, tractor/truck, compatible trailer, loader, equipment location and supporting steps from context. Correctly resolve short vague commands and natural long goals. Ask only when material uncertainty or irreversible consequences truly require it; do not guess spend or ownership. Target 99% measurable task-intent and location accuracy on representative real commands, with meaningful operational tolerances.

Selectable modes:
- Full Auto: autonomously plan and run configured farm objectives whenever gameplay is active; useful unattended operation.
- Semi-Auto: quick approval of the overall plan, then complete all steps autonomously.
- Advisory: observe and recommend, never execute without direction.
- Manual: execute explicit commands plus necessary supporting steps.

Goals may span weeks/months in game: maximize profit, prepare farm for winter, complete profitable contracts, keep animals fed, time planting to actual conditions, maintain productions and equipment. Self-chain independent goals and prepare prerequisites: observe -> resource allocation -> buy/lease/refill -> hitch -> work -> unload/return/store/sell -> repair/park -> next stage. Preserve and resume planned and active jobs across normal interruption/reloads without duplicating purchases, payments, work or physical commands. Provide fast what-if estimates for job time, fuel, leasing expense, crop value, profitability and risk using actual available data; mark unavailable inputs/assumptions. Forecast price, yield, weather or profit only with real support and explicit uncertainty.

## 4. MANDATORY simultaneous workers and fleet logistics

Farmhand must deploy, operate, coordinate and supervise MULTIPLE independently controlled workers/vehicles at any given time, not just a one-worker queue.

- Example: a moving combine harvesting with a grain cart unloading alongside it, another truck hauling grain, a tractor planting another field and a loader working elsewhere SIMULTANEOUSLY.
- Per-worker live identity, job, vehicle/implement and attachments, target, location, material, progress and recovery state. Command one, some or all workers by voice/text/UI.
- Shared reservations and conflicts: one controller per individual physical machine, many independent machines active at once; no duplicate allocation of tractors/trailers/cargo or overlapping workspaces, colliding maneuvers, contention for fill sources or contradictory operations.
- Dispatch ready work immediately, coordinate field traffic and machine swaps; reassign machines when advantageous, keep workflows productive while one worker recovers. STOP for one worker or all must immediately and durably cancel Farmhand-owned motion and stale commands.
- Concurrent state must survive interruption and resume only after reconciling observed world, assignments, cargo, attachments and physical safety. Scale scheduling, sensing and physical control without causing game frame or companion lag.
- Worker count governed by available equipment, map mechanics, sensible player settings, safety and measured performance, not a hardcoded single-job restriction.

## 5. Farming, field lifecycle and moving combine unloading

Support each field task separately (plow, cultivate, seed/sow, fertilize, roll, weed, harvest etc.) AND full field/season production cycles. Read actual field needs and perform only appropriate steps: lime/stone collection/mulch where needed -> preparation -> sowing -> fertilizer -> rolling -> weed control -> harvesting -> straw/bales/grass/silage/forage collection -> loading/transport/storage/sale -> equipment cleanup and next crop. Full workflows automatically choose machinery, buy seed/fertilizer, refill, hitch, move, repair and account for weather/season. Includes stone collection and correct work area measurements.

RELEASE GATE, NOT OPTIONAL: unload combine/harvester WHILE IT IS MOVING AND HARVESTING. A separate worker drives appropriately equipped grain cart/trailer under the moving pipe, matches vehicle speed/path and offset, transfers grain without unnecessary interruption, watches levels, routes full trailer for storage/sale, dispatches a replacement and returns. Verify combine motion, fill transfer, safety and continuing harvest in live gameplay. Stationary unloading is a backup, NOT a release substitute.

## 6. Forestry and configurable stump outcomes

Operate forestry harvesters, forwarders, grapples, loaders, skidders, winches, log trailers and chainsaw-capable player/character interactions wherever physically supported. Target trees by field/location, fell safely, delimb, cut to player-selectable log lengths, move, SORT, stack neatly and securely enough without spending excessive time making a perfect stack, load, transport, chip, store, sell and replant.

Target work rate: approximately 15–20 seconds to cut, move and stack ONE TREE with suitable positioned compatible equipment under straightforward conditions; measure the achieved time and preserve safety/quality. Do not promise it for inaccessible trees or arbitrary log counts.

Chainsaw must support requested single-cut stump removal where supported. User-selectable outcomes for stumps: delete, restore usable material to remaining tree/log if valid and physically/accounting correct, transform otherwise unrecoverable stump to wood chips, or automatically sell the material when that option is selected. Never invent timber volume, wood chips, money or unobserved game API functionality.

## 7. Specialist machines, rock/earthmoving and construction

Operate trucks, lowboys, trailers, forestry equipment, telehandlers, forklifts, skid steers, loaders, excavators, dozers, cranes, lift/grab/cut/dig/grade implements and other supported vehicle/equipment actions from their actual game controls.

Examples:
- '379/cat/quarry': find Lizard 379, appropriate lowboy/trailer, Cat 395, load and secure Cat, drive it to an accessible quarry/mine/field, unload and deploy it.
- 'bring a load of rock': identify valid material source, loader/excavator, capacity and compatible transport, destination, actual loading/unloading and delivery.
- 'dig a pond/pit/trench in field Y', 'raise/lower/level land', 'grade with dozer', 'load the excavator bucket into the trailer': control the real available machine/terrain interaction and verify changed terrain and material quantities.

Discover whether game/installed maps or optional compatible terrain-mod interfaces expose permanent earthmoving and supported fill types. Base game terrain sculpting is not equivalent to machine-based excavation. Integrate available mechanisms where verified; explicitly report a genuine unsupported feature instead of pretending it happened or imposing a new compulsory dependency.

Operate accessible construction/creation/landscaping features for placing buildings, roads, animal pens, production facilities etc. Site structures intelligently: land ownership, available budget, terrain grade, field productivity, orientation, nearby facilities and buildings, working and turning clearance, ingress/egress, useful spacing and future layout. Do NOT place a pig shed arbitrarily inside productive crops, at odd angles or too close to machinery routes. Verify actual placement and economic effect.

## 8. Contracts, economy, animals, production and full logistics

List, choose, accept and complete contracts with owned or properly leased/rented equipment, including required refills, transport and returns, verified payment and clean contract state.

Buy/lease/sell/trade/select variants and maintain equipment; refuel, charge, refill, repair, wash and reconfigure. Load and transport crops, pallets, logs, bales, vehicles, machinery, tools, minerals, rock, seed, fertilizer, liquids, animal products, production inputs/outputs and supplies. Select sensible sources, destinations and sale/store/processing decisions based on real-world prices/capacity, player goals and constraints.

Continuously manage supported animal feeding, watering, bedding, breeding, welfare, cleaning, manure/slurry, purchasing/transport/sales. Maintain production chains, inventory, delivery loops and forecasts; balance fleet, fields, animals, facilities, contracts and money so no one job starves the rest.

## 9. Characters, voice, HUD and settings

Optional user-installed local language models via Ollama or llama.cpp may play distinct named in-game characters: Farmhand manager, field worker, mechanic, store clerk, contract agent, etc. Each has an appropriate role, speaking style/personality, conversational context and permitted game actions. The same capable model can serve different roles; no mandatory separate model per character. Role conversations can initiate real workflows and report real outcomes rather than fictional success. Optional natural voice, audible status, contextual alerts, NPC physical presence/gestures/seat interaction where compatible. Purely deterministic commands must function without any AI model or ongoing paid service; language-model calls NEVER control per-frame steering.

Where FS25 supports it, allow visible companion/NPC presence: characters can walk to equipment, appear in vehicle cabs, use gestures/tablets and react to events, while visual roleplay is never falsely presented as physical task completion. Provide responsive companion HUD/tablet and useful settings: full multi-worker overview, work queues, fields, contracts, fleet, animal/production needs, plans, stock, markets, estimates, alerts and actions; voice/button 'do the obvious next thing'. Support player/farm ownership permissions and multiplayer status/control where game APIs permit. User-accessible inexpensive ON/OFF/select choices for autonomy mode, concurrency, spending/approval, vehicle preferences, log lengths, stump outcome, recovery/teleport/ghosting, voice/personality, alerts and diagnostics. Show only implemented settings. Teleport and scenery ghosting default OFF and are NOT normal navigation; physical safety, truthful verification and STOP remain mandatory.

## 10. Navigation, precision, fast execution and fast recovery

Use Farmhand-controlled GIANTS engine navigation agent as physically demonstrated for long-range routes, real GIANTS transforms/wheels/physics/collisions/terrain and Farmhand's precise local handling for target poses, trailer articulation, reversing, docking, hitching, cutting, loading and obstacle avoidance. Avoid unnecessary long searches and route planning before motion, circular wandering, slow corrective loops, repeated impossible retries, arbitrary timeouts and cosmetic over-alignment.

Supervise actual command-vs-movement and obstacles continuously; use fast measured local replans, alternative approaches, real equipment movement and safe backoff/reposition to recover efficiently. Protect the actual cargo, player, buildings, field and game. Use narrow verified provider interactions only where they are internal GIANTS primitives, NEVER AutoDrive. Maintain safe speeds appropriate to complete rig/terrain/curve. All recovery and precision requirements are about effective end-to-end work, not adding complexity.

User asks for 99% precision for task intent, destinations, unloading/moving/placing equipment, forestry stack/cutting and other operations. Define relevant physical tolerances, accuracy and realistic task-success measures; verify against representative live game runs, not fabricated percentages. Optimize command->first-useful-action (p50/p95), completion duration, real distance/time, cost, recovery seconds, frame/CPU cost and success rate. Cache stable observations, incrementally update changing state; replan only affected tasks; no model in the physical control loop.

## 11. Release qualification — do not silently shrink the user's desired product

Required live demonstrations (representative maps/machines where capabilities exist):
1. Concurrent independent physical workers, useful parallel work, coordination and STOP/restart/recovery without task or equipment conflicts.
2. MOVING combine harvesting + independent moving vehicle unloading + separate logistics cycle.
3. Complete observed owned-field single-operation and full-lifecycle tasks, including stones/rolling/weeds/fertilizer/growth.
4. Contracts viewed/accepted/completed/paid with owned and leased equipment.
5. Specialist lowboy haul (e.g. Lizard 379 hauling Cat 395), unload and supported excavation/other specialist task.
6. Forestry selectable log lengths, practical fast stacking/transport, chainsaw/stumps and measured suitable-tree work speed.
7. Supported grading/earthmoving/building placement with physically verified terrain/structures and clearance.
8. Animals, factories, full material logistics and buying/selling/storage, multi-stage autonomous goals.
9. Badly structured voice/text commands and selectable autonomous modes, optional multiple named companion characters.
10. Exact equipment/goal/ownership preservation, durable cancel, no AutoDrive participation or player handholding, high real-world accuracy and quick reliable action/recovery with bounded frame costs.

Keep capability status explicit: REQUIRED / NOT IMPLEMENTED / IMPLEMENTED / TESTED / LIVE-PROVEN / BLOCKED BY VERIFIED ENGINE/MAP LIMITATION. A simulated test is not physical proof. A missing engine capability is a real gap to document/adapt when possible, not permission to pretend success. Do not redefine release to exclude specified requirements.

FINAL TARGET: The player can request almost any actual FS25 objective, simple or complex. Farmhand observes the real map, understands intent, composes available capabilities, deploys multiple workers concurrently, acts quickly, uses machines properly, recovers rapidly, and verifies completion with minimal intervention and minimal overhead.
