-- Farmhand-owned, map-independent route search. The caller supplies a clearance
-- probe for the whole observed rig. No marker, provider job or vehicle ID is
-- selected here. Search success is only a candidate route, never a drive verdict.
-- GIANTS' AIVehicleUtil.driveToPoint and collision world will be the executor.
local Drive = {}
-- Scenery ghosting (Hitch.ghost) is never part of an ordinary drive: only a drive whose command
-- carries the player's opt-in setting (args.ghost, Farmhand settings, default off) may use it.

local pi, sin, cos, sqrt, floor, abs, atan2 = math.pi, math.sin, math.cos,
    math.sqrt, math.floor, math.abs, math.atan2 or math.atan
local STEP, BIN, CELL = 4, pi / 8, 2
local LIMIT = 6000

local function finite(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

-- Keep clearance separate from ground preference: a planted field is passable
-- when the task genuinely needs it, but transport searches pay for crossing it.
function Drive.routeProbe(env, foot, Hitch, x, z, yaw, curve, gear)
    local clear, reason, hits
    -- The live 2 m turn passed with this short articulated check while the
    -- aggregate rectangle reported scenery in empty space. Use it only for a
    -- candidate arc rooted at the vehicle's current observed pose; other route
    -- states need a fresh physical observation before this test can own them.
    if foot ~= nil and foot.tractor ~= nil and foot.towMeasured == true
            and type(Hitch.predictShortMotion) == "function"
            and type(Hitch.predictedUnitsClear) == "function"
            and finite(curve) and finite(x) and finite(z) and finite(yaw) then
        local ok, localClear, localReason, localHits = pcall(function()
            local tx, _, tz = env.getWorldTranslation(foot.tractor.rootNode)
            local fx, _, fz = env.localDirectionToWorld(foot.tractor.rootNode, 0, 0, 1)
            local startYaw = atan2(fx, fz)
            local length = abs(curve) < 0.00001
                and ((x - tx) * sin(startYaw) + (z - tz) * cos(startYaw))
                or atan2(sin(yaw - startYaw), cos(yaw - startYaw)) / curve
            if length ~= nil and abs(length) > 0.05 and abs(length) <= 2.5
                    and ((gear == -1 and length < 0) or (gear ~= -1 and length > 0)) then
                local samples = Hitch.predictShortMotion(env, foot, length, curve)
                local endpoint = samples ~= nil and samples[#samples].poses[1] or nil
                if endpoint ~= nil and sqrt((endpoint.x-x)^2 + (endpoint.z-z)^2) < 0.05
                        and abs(atan2(sin(endpoint.yaw - yaw), cos(endpoint.yaw - yaw))) < 0.02 then
                    return Hitch.predictedUnitsClear(env, foot, samples)
                end
            end
        end)
        if ok then clear, reason, hits = localClear, localReason, localHits end
    end
    if clear == nil and reason == nil then
        clear, reason, hits = Hitch.routePoseClear(env, foot, x, z, yaw, curve, gear == -1)
    end
    if clear == true and type(Hitch.routeSurface) == "function" then
        return true, Hitch.routeSurface(env, x, z)
    end
    return clear, reason, hits
end

local function distance(x, z, gx, gz)
    return sqrt((x - gx) ^ 2 + (z - gz) ^ 2)
end

local function advance(x, z, yaw, length, curvature)
    if abs(curvature) < 0.00001 then
        return x + sin(yaw) * length, z + cos(yaw) * length, yaw
    end
    local endYaw = yaw + length * curvature
    return x + (cos(yaw) - cos(endYaw)) / curvature,
           z + (sin(endYaw) - sin(yaw)) / curvature, endYaw
end

local function key(x, z, yaw)
    local angle = yaw % (2 * pi)
    return floor(x / CELL + 0.5) .. ":" .. floor(z / CELL + 0.5) .. ":" .. floor(angle / BIN + 0.5) % 16
end

local function push(heap, item)
    local n = #heap + 1
    while n > 1 do
        local parent = floor(n / 2)
        if heap[parent].score <= item.score then break end
        heap[n], n = heap[parent], parent
    end
    heap[n] = item
end

local function pop(heap)
    local root, last = heap[1], table.remove(heap)
    if #heap == 0 then return root end
    local n = 1
    while n * 2 <= #heap do
        local child = n * 2
        if child + 1 <= #heap and heap[child + 1].score < heap[child].score then child = child + 1 end
        if heap[child].score >= last.score then break end
        heap[n], n = heap[child], child
    end
    heap[n] = last
    return root
end

-- Courseplay's pathfinder runs a bounded share of a search per update and
-- resumes from its kept open list (Courseplay HybridAStar.lua:625-637): FS25 has
-- no coroutine.yield (HybridAStar.lua:46). GIANTS budgets work by getTimeSec()
-- the same way (OrchardBuildRequest.BUILD_TIME_BUDGET). A planning step that
-- spends its frame budget unwinds with PENDING and is replayed next frame from
-- the state it began with: finished searches answer from their kept results,
-- the unfinished one resumes. Without a game clock every search runs to its end.
Drive.FRAME_BUDGET_S = 0.010
local PENDING = setmetatable({}, { __tostring = function() return "PENDING" end })
Drive.PENDING = PENDING
local unpack = unpack or table.unpack
local episode -- the planning step running in this frame, if any

local function pack(...) return { n = select("#", ...), ... } end

local function overBudget()
    return episode ~= nil and episode.deadline ~= nil and episode.clock() >= episode.deadline
end

-- A search kept across frames: make() builds its state, advance(job) runs it
-- until it sets job.result and returns true, or returns false when over budget.
-- A replay that asks for a different search in the same place discards the rest.
local function resumable(signature, make, advance)
    local job
    if episode ~= nil then
        episode.n = episode.n + 1
        job = episode.jobs[episode.n]
        if job ~= nil and job.signature ~= signature then
            for n = episode.n, #episode.jobs do episode.jobs[n] = nil end
            job = nil
        end
        if job == nil then
            job = make()
            job.signature = signature
            episode.jobs[episode.n] = job
        end
    else
        job = make()
    end
    if not job.done then
        if not advance(job) then error(PENDING, 0) end
        job.done = true
    end
    return unpack(job.result, 1, job.result.n)
end

-- A replayed planning step does not repeat the lines it already logged.
local function say(text)
    if episode ~= nil then
        episode.said = episode.said + 1
        if episode.said <= episode.printed then return end
        episode.printed = episode.said
    end
    print(text)
end

local function signature(...)
    local parts = {}
    for i = 1, select("#", ...) do
        local value = select(i, ...)
        parts[i] = type(value) == "number" and string.format("%.3f", value) or tostring(value)
    end
    return table.concat(parts, "|")
end

-- fn(...) as one replayable planning step kept on holder.episode: PENDING, or
-- fn's own results. clock is GIANTS' getTimeSec, or nil (run to the end).
-- holder.budget (seconds) overrides the frame budget; a planning step may run inside
-- another (Drive.planAhead inside a frame), each in its own episode.
local function planned(holder, clock, fn, ...)
    local ep = holder.episode or { jobs = {}, printed = 0 }
    ep.n, ep.said, ep.clock = 0, 0, clock
    ep.deadline = clock ~= nil and clock() + (holder.budget or Drive.FRAME_BUDGET_S) or nil
    local outer = episode
    episode = ep
    local results = pack(pcall(fn, ...))
    episode = outer
    if not results[1] and results[2] == PENDING then
        holder.episode = ep
        return PENDING
    end
    holder.episode = nil
    if not results[1] then error(results[2], 0) end
    return unpack(results, 2, results.n)
end

Drive.planned = planned

local function gameClock(env)
    return type(env.getTimeSec) == "function" and env.getTimeSec or nil
end

-- Courseplay's HybridAStarWithAStarInTheMiddle first finds a fast 2D route
-- around large obstacles, then applies vehicle constraints. Here the 2D
-- search supplies a corridor to Farmhand's existing whole-rig Hybrid A*.
-- Every edge is sampled in GIANTS' terrain/collision world; grid cells are
-- generated from the current pose, never a stored map or provider network.
-- navHint(x,z) optionally returns false where GIANTS' live navigation map
-- (static placeables, fences, trees, map no-go layers) is blocking; it only
-- raises cost so corridors keep clearance. The rig probe alone decides passage.
local DIRS = { {0,1}, {1,1}, {1,0}, {1,-1}, {0,-1}, {-1,-1}, {-1,0}, {-1,1} }
local function gridKey(i,j) return i .. ':' .. j end
-- Across a cell (fractions of a step, perpendicular to the move) where its centre is blocked.
local CELL_SHIFTS = { 0, -0.25, 0.25, -0.45, 0.45 }

-- A target is a point {x,z}, or {goals={{x,z,extra},...}}: any one of several
-- approach points, the search ending at the cheapest it reaches (extra metres
-- added for a less preferred one).
local function goalsOf(target)
    return target.goals or { { target[1], target[2], 0 } }
end

local function toGoals(goals, x, z)
    local nearest = math.huge
    for _, goal in ipairs(goals) do
        nearest = math.min(nearest, distance(x, z, goal[1], goal[2]) + goal[3])
    end
    return nearest
end

local function globalPlan(start, target, probe, margin, step, offsetX, offsetZ, radius, avoided, navHint)
    local goals = goalsOf(target)
    local sx, sz, gx, gz = start[1], start[2], goals[1][1], goals[1][2]
    return resumable(signature("global", sx, sz, gx, gz, #goals, margin, step, offsetX, offsetZ,
                               #(avoided or {})), function()
        local heap = {}
        push(heap, { i=0, j=0, x=sx, z=sz, cost=0, score=toGoals(goals,sx,sz) })
        return { heap = heap, best = { [gridKey(0,0)] = 0 }, expanded = 0, rejected = {} }
    end, function(job)
    local dirs = DIRS
    local minX, maxX, minZ, maxZ = sx, sx, sz, sz
    for _, goal in ipairs(goals) do
        minX, maxX = math.min(minX,goal[1]), math.max(maxX,goal[1])
        minZ, maxZ = math.min(minZ,goal[2]), math.max(maxZ,goal[2])
    end
    minX, maxX, minZ, maxZ = minX-margin, maxX+margin, minZ-margin, maxZ+margin
    local heap, best, rejected = job.heap, job.best, job.rejected
    while #heap > 0 and job.expanded < 7000 do
        if overBudget() then return false end
        local node = pop(heap)
        if node.goal ~= nil then
            local route = { {node.goal[1],node.goal[2]} }
            node = node.parent
            while node ~= nil do
                table.insert(route, 1, {node.x,node.z})
                node = node.parent
            end
            job.result = pack(route)
            return true
        end
        if node.cost <= (best[gridKey(node.i,node.j)] or math.huge) then
            job.expanded = job.expanded + 1
            local left = toGoals(goals, node.x, node.z)
            if job.nearest == nil or left < job.nearest[1] then job.nearest = { left, node.x, node.z } end
            for _, goal in ipairs(goals) do
                local d = distance(node.x,node.z,goal[1],goal[2])
                local open = d < step * 1.4
                -- An approach point's hop is probed too: unchecked, it crossed a fence
                -- into a closed pen. (Each point is clear at some heading of its own.)
                if open and target.goals ~= nil then
                    local yaw = (math.atan2 or math.atan)(goal[1]-node.x, goal[2]-node.z)
                    for sample = 3, d - 1, 3 do
                        local f = sample / d
                        if probe(node.x+(goal[1]-node.x)*f, node.z+(goal[2]-node.z)*f, yaw, 0) ~= true then
                            open = false
                            break
                        end
                    end
                end
                if open then
                    push(heap, { goal=goal, cost=node.cost+d+goal[3], score=node.cost+d+goal[3], parent=node })
                end
            end
            for _, dir in ipairs(dirs) do
                local i, j = node.i+dir[1], node.j+dir[2]
                local x, z = sx+i*step+offsetX, sz+j*step+offsetZ
                local k = gridKey(i,j)
                local length = distance(node.x,node.z,x,z)
                local cost = node.cost + length
                local avoidedHere = false
                for _, area in ipairs(avoided or {}) do
                    -- An area around the rig itself would forbid every departure.
                    if distance(sx,sz,area[1],area[2]) >= area[3]
                            and distance(x,z,area[1],area[2]) < area[3] then avoidedHere = true; break end
                end
                if x >= minX and x <= maxX and z >= minZ and z <= maxZ
                        and not avoidedHere and cost < (best[k] or math.huge) then
                    -- The cell's centre, else a point across it: a narrow way (a bridge between
                    -- railings, live c5b7ddb950a846d9-1) passes between centres at most angles.
                    -- The edge is probed at its own heading, from where its node really is.
                    local nx, nz = dir[2], -dir[1]
                    local nl = math.sqrt(nx*nx + nz*nz)
                    local cx, cz, yaw, pass, surface = x, z, 0, false, nil
                    for _, shift in ipairs(CELL_SHIFTS) do
                        x, z = cx + nx/nl*shift*step, cz + nz/nl*shift*step
                        length = distance(node.x,node.z,x,z)
                        yaw = (math.atan2 or math.atan)(x-node.x, z-node.z)
                        local clear, why = true, nil
                        for sample = 3, length, 3 do
                            local fraction = sample / length
                            local ok, reason = probe(node.x+(x-node.x)*fraction,
                                                     node.z+(z-node.z)*fraction, yaw, 0)
                            if ok ~= true then clear, why = false, reason; break end
                        end
                        -- Both results ("and" would keep only the first: every block logged unreadable).
                        if clear then clear, why = probe(x,z,yaw,0) end
                        if clear == true then pass, surface = true, why; break end
                        if shift == 0 then rejected[why or "unreadable"] = (rejected[why or "unreadable"] or 0) + 1 end
                    end
                    cost = node.cost + length
                    if pass then
                        cost = cost + (surface == "CROP" and step * 40
                                       or surface == "FIELD" and step * 8 or 0)
                        if radius ~= nil and radius > 0 then
                            local side = radius * 0.5
                            local dx, dz = cos(yaw)*side, -sin(yaw)*side
                            if probe(x+dx,z+dz,yaw,0) ~= true then cost = cost + step*2 end
                            if probe(x-dx,z-dz,yaw,0) ~= true then cost = cost + step*2 end
                        end
                        if navHint ~= nil then
                            local side = math.max(2, (radius or 4) * 0.25)
                            local dx, dz = cos(yaw)*side, -sin(yaw)*side
                            for _, point in ipairs({{x,z},{x+dx,z+dz},{x-dx,z-dz}}) do
                                if navHint(point[1],point[2]) == false then cost = cost + step end
                            end
                        end
                        if cost < (best[k] or math.huge) then
                            best[k] = cost
                            push(heap, {i=i,j=j,x=x,z=z,cost=cost,
                                        score=cost+toGoals(goals,x,z),parent=node})
                        end
                    end
                end
            end
        end
    end
    local dominant, count = "none", 0
    for why, n in pairs(rejected) do
        if n > count then dominant, count = why, n end
    end
    local near = job.nearest or { 0, sx, sz }
    -- What stops the search there, a step each way (the GRADE read, or what is in the way).
    local around = {}
    for _, dir in ipairs(dirs) do
        local yaw = (math.atan2 or math.atan)(dir[1], dir[2])
        local ok, pass, why, detail = pcall(probe, near[2] + dir[1]*step, near[3] + dir[2]*step, yaw, 0)
        around[#around + 1] = not ok and "error" or pass == true and "clear"
            or (tostring(why) .. (type(detail) == "string" and " " .. detail or ""))
    end
    print(string.format("[Farmhand] drive global search cell %.0f offset %.1f,%.1f margin %.0f expanded %d open %d blocked %s:%d nearest %.0fm at (%.1f,%.1f)",
                        step,offsetX,offsetZ,margin,job.expanded,#heap,tostring(dominant), count, near[1], near[2], near[3]))
    print("[Farmhand] drive global search nearest point, N NE E SE S SW W NW: " .. table.concat(around, "; "))
    local scan = Drive.scanSurface         -- set by Drive.begin (diagnostics only)
    for d, text in ipairs(around) do
        if scan ~= nil and (text:find("d/") or text:find("d>")) then
            local yaw = (math.atan2 or math.atan)(dirs[d][1], dirs[d][2])
            print(string.format("[Farmhand] drive surface %s of (%.1f,%.1f): %s", ({"N","NE","E","SE","S","SW","W","NW"})[d],
                                near[2], near[3], tostring(scan(near[2], near[3], yaw, 16, {-6,-3,0,3,6}))))
        end
    end
    job.result = pack(nil)
    return true
    end)
end

local function corridorHeuristic(x,z,guide)
    local closest = math.huge
    for i = 1, #guide do
        local point = guide[i]
        closest = math.min(closest, distance(x,z,point[1],point[2]) + point[3])
    end
    return closest
end

local function guidedRoute(start,target,probe,radius,avoided,navHint)
    local direct = toGoals(goalsOf(target),start[1],start[2])
    local grids = {{10,0,0}, {10,5,5}, {5,0,0}, {5,2.5,2.5}}
    for _, grid in ipairs(grids) do
        for _, margin in ipairs({math.max(90,direct*0.8), math.max(220,direct*1.5),
                                  math.max(450,direct*2)}) do
            local route = globalPlan(start,target,probe,margin,grid[1],grid[2],grid[3],
                                     radius,avoided,navHint)
            if route ~= nil then
            say(string.format("[Farmhand] drive global corridor %d nodes cell %.0f offset %.1f,%.1f margin %.0f",
                                #route,grid[1],grid[2],grid[3], margin))
            local remaining = 0
            for i = #route, 1, -1 do
                route[i][3] = remaining
                if i > 1 then
                    remaining = remaining + distance(route[i][1],route[i][2],
                                                      route[i-1][1],route[i-1][2])
                end
            end
            return route
            end
        end
    end
    return nil
end

Drive.globalRoute = guidedRoute

-- GIANTS keeps its vehicle navigation map current as placeables, fences,
-- trees and landscaping change (AISystem:setAreaDirty). Its own wrapper
-- getIsPositionReachable (AIParameterPosition:validate) answers per point; an
-- absent map, a client, or an error gives no hint rather than a refusal.
function Drive.navHint(env)
    local mission = env.g_currentMission
    local ai = mission and mission.aiSystem
    if ai == nil or type(ai.getIsPositionReachable) ~= "function"
            or ai.navigationMap == nil or ai.isServer ~= true then return nil end
    return function(x, z)
        local ok, reachable = pcall(ai.getIsPositionReachable, ai, x, 0, z)
        if ok and reachable == false then return false end
        return nil
    end
end

-- start={x,z,yaw}, target={x,z}; radius is GIANTS' observed minimum turning
-- radius or larger. The probe returns true only when the rig's entire swept box
-- and terrain are readable and clear at x,z,yaw. Each arc is sampled every metre.
-- With `switches` the search may also reverse straight (a towed rig's only
-- controlled reverse, Drive.reverseAim) and change gear at most that often: the
-- bounded forward/reverse shuffle that turns a long rig out of a confined place.
-- With `steeredReverse` (a tractor with nothing towed) a reverse may also turn at the
-- turning radius: a pocket with an obstacle ahead and room behind is left by backing
-- out to one side (live 9d309797b6744336-1: the 3650 between a tree and its planter).
-- Its nodes carry their gear (-1 reversing) and a reverse its curvature; it always
-- ends driving forward.
function Drive.plan(start, target, radius, probe, guide, goalYaw, localOnly, maxExpanded, switches, steeredReverse)
    if type(start) ~= "table" or not (finite(start[1]) and finite(start[2]) and finite(start[3])) then
        return nil, "INVALID_START"
    end
    if type(target) ~= "table" or not (finite(target[1]) and finite(target[2])) then
        return nil, "INVALID_DESTINATION"
    end
    if not finite(radius) or radius < 3 or type(probe) ~= "function" then
        return nil, "INVALID_GEOMETRY"
    end
    local sx, sz, syaw, gx, gz = start[1], start[2], start[3], target[1], target[2]
    -- A shuffle may begin where the rig stands inside its own margin of something (live
    -- f13bb95c295c4a4b-1: stopped off a turning reverse by its planter, every move forward
    -- clear); each move it makes is probed as before.
    if probe(sx, sz, syaw) ~= true and (switches or 0) <= 0 then return nil, "START_NOT_CLEAR" end
    if distance(sx, sz, gx, gz) < 3 then return { { sx, sz, syaw } }, nil end
    local margin = math.max(45, math.min(120, distance(sx, sz, gx, gz) * 0.6))
    local minX, maxX = math.min(sx, gx) - margin, math.max(sx, gx) + margin
    local minZ, maxZ = math.min(sz, gz) - margin, math.max(sz, gz) + margin
    if guide ~= nil then
        for _, point in ipairs(guide) do
            minX, maxX = math.min(minX,point[1]-45), math.max(maxX,point[1]+45)
            minZ, maxZ = math.min(minZ,point[2]-45), math.max(maxZ,point[2]+45)
        end
    end
    local limit = maxExpanded or (guide and LIMIT*3 or LIMIT)
    switches = switches or 0
    local function nodeKey(node)
        local k = key(node.x, node.z, node.yaw)
        return node.gear < 0 and k .. ":r" or k
    end
    local found, expanded = resumable(signature("plan", sx, sz, syaw, gx, gz, radius, goalYaw, limit,
                                                guide and #guide or 0, switches, steeredReverse and 1 or 0), function()
        local heap = {}
        local first = { x = sx, z = sz, yaw = syaw, cost = 0, gear = 1, switches = 0,
                        score = guide and corridorHeuristic(sx,sz,guide)*1.5 or distance(sx,sz,gx,gz) }
        push(heap, first)
        return { heap = heap, best = { [nodeKey(first)] = 0 }, expanded = 0 }
    end, function(job)
    local heap, best = job.heap, job.best
    local moves = { {1,0}, {1,-1}, {1,1} }
    if switches > 0 then moves[4] = {-1,0} end
    if switches > 0 and steeredReverse then moves[5], moves[6] = {-1,-1}, {-1,1} end
    while #heap > 0 and job.expanded < limit do
        if overBudget() then return false end
        local node = pop(heap)
        if node.cost <= (best[nodeKey(node)] or math.huge) then
            job.expanded = job.expanded + 1
            local headingError = goalYaw and abs((node.yaw-goalYaw+pi)%(2*pi)-pi) or 0
            -- The final goal is planned inside the 3 m arrival check less the
            -- follower's 1 m endpoint tolerance; corridor waypoints keep 3 m.
            if node.gear > 0 and distance(node.x, node.z, gx, gz) < (goalYaw and 3 or 2)
                    and headingError < pi/12 then
                local reversed = {}
                while node ~= nil do
                    reversed[#reversed + 1] = { node.x, node.z, node.yaw, node.gear < 0 and -1 or nil,
                                                node.gear < 0 and node.curve or nil }
                    node = node.parent
                end
                local path = {}
                for i = #reversed, 1, -1 do path[#path + 1] = reversed[i] end
                job.result = pack(path, job.expanded)
                return true
            end
            -- Full STEP moves; where none fits, half ones from this node: in a tight spot the rig
            -- creeps out (live 140a6b52320b4587-1: the long GMC rig among parked rigs, every 2 m
            -- move clear, every 4 m one clipped, so no search ever left its start).
            local children = 0
            for _, len in ipairs({ STEP, STEP / 2 }) do
            if len < STEP and children > 0 then break end
            for _, move in ipairs(moves) do
                local gear, curve = move[1], move[2] / radius
                local switched = gear ~= node.gear and node.parent ~= nil
                local clear = not switched or node.switches < switches
                local surfaceCost = 0
                for d = 1, len do
                    if not clear then break end
                    local x, z, yaw = advance(node.x, node.z, node.yaw, d * gear, curve)
                    local pass, surface = probe(x, z, yaw, curve, gear)
                    if x < minX or x > maxX or z < minZ or z > maxZ or pass ~= true then
                        clear = false
                    else
                        surfaceCost = surfaceCost + (surface == "CROP" and 40
                                                     or surface == "FIELD" and 8 or 0)
                    end
                end
                if clear then
                    children = children + 1
                    local x, z, yaw = advance(node.x, node.z, node.yaw, len * gear, curve)
                    -- Reversing is slower and less sure; each gear change costs a stop.
                    local cost = node.cost + len * (gear < 0 and 2 or 1) + surfaceCost
                                 + (curve ~= 0 and 0.3 or 0)
                                 + (switched and STEP * 3 or 0)
                    local child = { x = x, z = z, yaw = yaw, cost = cost, gear = gear, curve = curve,
                                    switches = node.switches + (switched and 1 or 0), parent = node,
                                    score = cost + (guide and corridorHeuristic(x,z,guide)*1.5
                                                  or distance(x,z,gx,gz)) }
                    local k = nodeKey(child)
                    if cost < (best[k] or math.huge) then
                        best[k] = cost
                        push(heap, child)
                    end
                end
            end
            end
        end
    end
    job.result = pack(nil, job.expanded)
    return true
    end)
    if found ~= nil then return found, nil end
    if guide == nil and not localOnly then
        local avoided = {}
        for attempt = 1, 4 do
        local global = guidedRoute(start,target,probe,radius,avoided)
        if global ~= nil then
            local gridStep = #global > 2 and distance(global[2][1],global[2][2],
                                                       global[3][1],global[3][2]) or 10
            local stride = gridStep < 8 and 8 or 4
            for firstIndex = 1, #global, 8 do
                local pieces = {}
                for n = firstIndex, math.min(#global,firstIndex+7) do
                    pieces[#pieces+1] = string.format("%d:(%.0f,%.0f)",n,global[n][1],global[n][2])
                end
                say("[Farmhand] drive corridor " .. table.concat(pieces," "))
            end
            local path, current, reached = {}, start, 1
            local failed = false
            while reached < #global do
                local section, chosen
                -- A grid corner is only a corridor hint. A rig may need to
                -- start turning before it, so try farther targets if the
                -- nearest waypoint cannot be reached with its turn radius.
                for _, span in ipairs({stride,stride*2,stride*3,stride*4,
                                       math.max(2,math.floor(stride/2)),1}) do
                    local i = math.min(reached + span, #global)
                    local waypoint = global[i]
                    local guide = {}
                    for n = reached, i do guide[#guide+1] = global[n] end
                    local goalYaw
                    if i < #global then
                        goalYaw = (math.atan2 or math.atan)(waypoint[1]-global[i-1][1],
                                                            waypoint[2]-global[i-1][2])
                    end
                    section = Drive.plan(current, {waypoint[1],waypoint[2]},
                                         radius, probe, guide, goalYaw)
                    if section ~= nil then chosen = i; break end
                end
                if section == nil then
                    local bad = global[math.min(#global,reached+math.max(2,math.floor(stride/2)))]
                    avoided[#avoided+1] = {bad[1],bad[2],math.max(12,radius*1.5)}
                    say(string.format("[Farmhand] drive corridor refinement failed after %d/%d (%.1f,%.1f yaw %.2f); avoiding %.1f,%.1f for next corridor",
                                        reached, #global, current[1],current[2],current[3],bad[1],bad[2]))
                    failed = true
                    break
                end
                for n = path[1] and 2 or 1, #section do path[#path+1] = section[n] end
                current, reached = section[#section], chosen
            end
            if not failed then return path, nil end
        else
            say(string.format("[Farmhand] drive no global corridor from %.1f,%.1f to %.1f,%.1f",
                                sx,sz,gx,gz))
            break
        end
        end
    end
    if localOnly then
        say(string.format("[Farmhand] drive local search exhausted expanded=%d start=(%.1f,%.1f,%.2f) goal=(%.1f,%.1f) radius=%.1f goalYaw=%s guide=%d",
                            expanded,sx,sz,syaw,gx,gz,radius,tostring(goalYaw),guide and #guide or 0))
        if expanded == 1 and guide == nil then
            -- Each metre of the first full move (live 140a6b52320b4587-1: 1 m clear every way, yet
            -- no move of the search's own length left the start; which metre, and what blocks it).
            for _, steer in ipairs({0,-1,1}) do
                local at, pass, why = 0, true, nil
                for d = 1, STEP do
                    local px,pz,pyaw = advance(sx,sz,syaw,d,steer/radius)
                    pass, why = probe(px,pz,pyaw,steer/radius)
                    if pass ~= true then at = d break end
                end
                say(string.format("[Farmhand] drive departure forward steer=%d clear=%s%s reason=%s",
                                    steer,tostring(pass),at > 0 and (" at " .. at .. " m") or "",tostring(why)))
            end
            for d = 1, 8 do
                local px,pz = sx-sin(syaw)*d,sz-cos(syaw)*d
                local pass, why = probe(px,pz,syaw,0)
                say(string.format("[Farmhand] drive departure reverse %dm clear=%s reason=%s",
                                    d,tostring(pass),tostring(why)))
                if pass ~= true then break end
            end
        end
    end
    return nil, "NO_ROUTE"
end

local SECTION_SPANS = {4,8,12,16,2,1}
Drive.SHUFFLE_SWITCHES = 6      -- gear changes in one planned shuffle
Drive.FIRST_ROUTE_LIMIT_MS = 120000   -- the first route's search, at most
Drive.LEARNED_ROUTE_LIMIT_MS = 180000 -- a taught short line must not loop for minutes before provider recovery
Drive.LEAVE_M = 6                      -- a touched object left out this near the start
Drive.SHUFFLE_LEGS = 16         -- legs driven in one drive's shuffles
local SHUFFLE_LIMIT = 8000
Drive.AHEAD_BUDGET_S = 0.004    -- per frame for the next section, searched while driving
Drive.MAX_APPROACH_M = 100      -- = companion/standalone.py MAX_APPROACH_M

-- Where a section attempt aims on the corridor: its index, point, the corridor
-- points it is guided by and the heading it must arrive with (at the end, the
-- drive's own goal heading or none: corridor.goalYaw).
local function sectionGoal(corridor, reached, attempt)
    local gridStep = #corridor > 2 and distance(corridor[2][1],corridor[2][2],
                                                corridor[3][1],corridor[3][2]) or 10
    local stride = gridStep < 8 and 8 or 4
    local span = SECTION_SPANS[attempt]
    if span == nil then return nil end
    if span ~= 1 then span = span * stride / 4 end
    local i = corridor.learned and #corridor or math.min(reached + span,#corridor)
    local waypoint = corridor[i]
    local guide = {}
    for n = reached,i do guide[#guide+1] = corridor[n] end
    local goalYaw = corridor.goalYaw
    if i < #corridor then
        goalYaw = (math.atan2 or math.atan)(waypoint[1]-corridor[i-1][1],
                                            waypoint[2]-corridor[i-1][2])
    end
    return i, {waypoint[1],waypoint[2]}, guide, goalYaw
end

-- One bounded rig-aware refinement attempt. A long route is refined only as
-- the vehicle reaches each section, so search never monopolizes a game frame
-- for an entire map crossing. With `switches`, a forward/reverse shuffle.
function Drive.section(start, radius, probe, corridor, reached, attempt, switches, steeredReverse)
    local i, waypoint, guide, goalYaw = sectionGoal(corridor, reached, attempt)
    if i == nil then return nil,nil,"SECTION_EXHAUSTED" end
    local limit = corridor.learned and 12000 or switches and SHUFFLE_LIMIT or 4000
    local section, why = Drive.plan(start,waypoint,radius,probe,guide,goalYaw,true,
                                    limit,switches,steeredReverse)
    if section == nil then
        say(string.format("[Farmhand] drive section attempt %d from=%d to=%d radius=%.1f%s reason=%s",
                            attempt,reached,i,radius,switches and " shuffle" or "",tostring(why)))
    end
    return section,i,section and nil or "SECTION_BLOCKED"
end

-- A place (a building's centre) is approached anywhere around it within `area`
-- where the rig's box fits at some heading, nearer rings preferred; the route
-- search then ends at the cheapest one the rig can reach (live 9315e713c7e3430f-3:
-- the one standoff beside the barn was fenced in).
function Drive.approaches(cx, cz, area, probe)
    local goals, inner = {}, area * 0.3
    for _, share in ipairs({0.3, 0.5, 0.75, 1}) do
        local r = area * share
        for n = 0, 15 do
            local bearing = n * pi / 8
            local x, z = cx + sin(bearing) * r, cz + cos(bearing) * r
            local facing = bearing + pi
            for _, yaw in ipairs({facing, facing + pi/2, facing - pi/2, bearing}) do
                if probe(x, z, yaw, 0) == true then
                    goals[#goals+1] = {x, z, r - inner}
                    break
                end
            end
        end
    end
    return goals
end

function Drive.activateSection(s, section, reached, x, z)
    s.path,s.next,s.reached,s.sectionAttempt = section,2,reached,1
    s.ahead,s.aheadAttempt = nil,1
    s.previewAt = s.elapsed + 250
    s.driveStartMs = nil -- each stopped handoff reuses Hitch's steering/throttle ramp
    s.lastMovedX,s.lastMovedZ,s.movedAt = x,z,s.elapsed
end

-- The next corridor section searched while the rig drives the current one, from that
-- one's planned end pose, a few milliseconds a frame, and joined on when found: the
-- route goes on without braking to a stop at every section end (Courseplay plans the
-- next part of a course while driving the current one). Only on an ordinary forward
-- section with more corridor after it; a shuffle, reverse, straightening or recovery,
-- and the last section, keep their stops. Not found in time: the stop and search at
-- the section end as before. Every joined section was planned by the same probe.
function Drive.planAhead(env, s, Hitch)
    if s.corridor == nil or s.path == nil or #s.path < 2 or s.shuffle ~= nil or s.legKmh ~= nil
            or s.recovering or s.retreat ~= nil or s.egress ~= nil or s.straightenForward ~= nil
            or s.reached == nil or s.reached >= #s.corridor
            or (s.aheadAttempt or 1) > #SECTION_SPANS then return nil end
    local last = s.path[#s.path]
    if (last[4] or 1) < 0 then return nil end
    local function clear(px, pz, pyaw, curve, gear)
        return Drive.routeProbe(env, s.foot, Hitch, px, pz, pyaw, curve, gear)
    end
    s.ahead = s.ahead or { budget = Drive.AHEAD_BUDGET_S }
    local section, reached = planned(s.ahead, gameClock(env), Drive.section,
                                     { last[1], last[2], last[3] }, s.radius, clear,
                                     s.corridor, s.reached, s.aheadAttempt or 1)
    if section == PENDING then return nil end
    s.ahead = nil
    if section == nil then
        s.aheadAttempt = (s.aheadAttempt or 1) + 1
        return nil
    end
    local keep = math.max(1, s.next - 2)
    local path = {}
    for n = keep, #s.path do path[#path+1] = s.path[n] end
    for n = 2, #section do path[#path+1] = section[n] end
    s.next = s.next - keep + 1
    s.path, s.reached, s.aheadAttempt = path, reached, 1
    s.joined = (s.joined or 0) + 1
    say(string.format("[Farmhand] drive next section to %d/%d joined ahead at %.1f,%.1f (%d steps), no stop",
                      reached, #s.corridor, last[1], last[2], #section))
    return true
end

-- A parked obstacle can overlap the conservative box at the current pose even
-- when the vehicle can pull straight away from it. This bounded departure is
-- considered only for a stationary object behind the driver; the collision
-- engine stays active. Every metre ahead must be clear of all other objects.
-- `dir` -1: the obstacle touches the rig ahead (live 2d946758a8054123-1: the 3650's nose
-- against a tree after a pull-clear) and the departure is straight back.
function Drive.planEgress(start, obstacle, ordinary, ignoring, dir)
    local sx, sz, yaw = start[1], start[2], start[3]
    dir = dir or 1
    if not (finite(sx) and finite(sz) and finite(yaw) and finite(obstacle[1])
            and finite(obstacle[2])) then return nil, "EGRESS_UNREADABLE" end
    local fx, fz = sin(yaw) * dir, cos(yaw) * dir
    if (obstacle[1] - sx) * fx + (obstacle[2] - sz) * fz >= -0.5 then
        return nil, dir > 0 and "EGRESS_NOT_BEHIND" or "EGRESS_NOT_AHEAD"
    end
    for d = 0, 8 do
        local x, z = sx + fx * d, sz + fz * d
        if ignoring(x, z, yaw) ~= true then return nil, "EGRESS_BLOCKED" end
        if d > 0 and ordinary(x, z, yaw) == true then return d, nil end
    end
    return nil, "EGRESS_NOT_CLEAR"
end

-- When every short forward arc is blocked at a parked pose, back straight
-- only as far as needed to make a forward arc possible. The same full-rig
-- probe checks every metre of the retreat; no object is ignored.
function Drive.planRetreat(start, radius, probe)
    local sx,sz,yaw = start[1],start[2],start[3]
    local function forwardOpen(x,z)
        for _, steer in ipairs({0,-1,1}) do
            local clear = true
            for d = 1, STEP do
                local px,pz,pyaw = advance(x,z,yaw,d,steer/radius)
                if probe(px,pz,pyaw,steer/radius) ~= true then clear = false; break end
            end
            if clear then return true end
        end
        return false
    end
    if forwardOpen(sx,sz) then return nil end
    local lastClear = 0
    for d = 1, 12 do
        local x,z = sx-sin(yaw)*d,sz-cos(yaw)*d
        if probe(x,z,yaw,0) ~= true then return lastClear > 0 and lastClear or nil end
        lastClear = d
        if d >= 4 and d % 2 == 0 and forwardOpen(x,z) then return d end
    end
    return lastClear > 0 and lastClear or nil
end

-- A narrow forward pocket may have a few clear motion primitives but no
-- usable continuation. Before backing out, prove that a candidate backed
-- pose has both a clear swept retreat and a rig-aware forward first section.
function Drive.planRetreatRoute(start, target, radius, probe, navHint, avoided)
    local sx,sz,yaw = start[1],start[2],start[3]
    local maxBack = math.min(64,math.max(16,radius*4))
    for d = 1, maxBack do
        local x,z = sx-sin(yaw)*d,sz-cos(yaw)*d
        if probe(x,z,yaw,0) ~= true then return nil end
        if d % 4 == 0 then
            local backed = {x,z,yaw}
            local path
            if target[1] ~= nil and distance(x,z,target[1],target[2]) <= 200 then
                path = Drive.plan(backed,target,radius,probe,nil,nil,true)
            end
            if path == nil then
                local corridor = guidedRoute(backed,target,probe,radius,avoided,navHint)
                if corridor ~= nil then
                    path = Drive.section(backed,radius,probe,corridor,1,1)
                end
            end
            if path ~= nil then return d end
        end
    end
    return nil
end

-- Straight back out of a dead end: the farthest clear reverse (as planRetreatRoute
-- bounds it) from whose end a corridor avoiding `avoided` begins; the rig turns
-- onto it there by a shuffle. Corridors are searched from the far end inwards.
function Drive.planBackOut(start, target, radius, probe, navHint, avoided)
    local sx,sz,yaw = start[1],start[2],start[3]
    local farthest = 0
    for d = 1, math.min(64,math.max(16,radius*4)) do
        if probe(sx-sin(yaw)*d,sz-cos(yaw)*d,yaw,0) ~= true then break end
        farthest = d
    end
    for d = farthest - farthest % 4, 8, -4 do
        local backed = {sx-sin(yaw)*d,sz-cos(yaw)*d,yaw}
        if guidedRoute(backed,target,probe,radius,avoided,navHint) ~= nil then return d end
    end
    return nil
end

-- The local aim point (in the tractor's frame) that reverses the rig straight
-- back along `line` ({x,z,yaw} it started from), and the towed unit's hitch angle.
-- With a straight wheel that angle grows on its own (live dee63f6c79b24196-4: the
-- GMC's deckover jackknifed on a 32 m escape). A towed wheeled unit is held in line
-- with Courseplay AIReverseDriver's structure (150dcd5, 247-280; Ridley and Corke
-- 2003): a wanted hitch angle from the unit's orientation and cross-track errors,
-- the tractor turned by its difference from the current one, aimed at Courseplay's
-- lookahead (PurePursuitController 64: the turning radius, at most 6 m). Its gains
-- suit a semi-trailer on this straight line (tests: tow 6-15 m, radius 6-12 m).
function Drive.reverseAim(env, foot, line, yaw, radius)
    local towed = foot ~= nil and foot.towed or nil
    if towed == nil then return 0,-10 end
    local ok, tx,_,tz = pcall(env.getWorldTranslation, towed.steeringAxleNode or towed.rootNode)
    local okD, dx,_,dz = pcall(env.localDirectionToWorld, towed.steeringAxleNode or towed.rootNode, 0,0,1)
    if not (ok and okD and finite(tx) and finite(tz) and finite(dx) and finite(dz)) then return nil end
    local function wrap(a) return (a+pi)%(2*pi)-pi end
    local unitYaw = (math.atan2 or math.atan)(dx,dz)
    -- Courseplay's errors are in the travel direction (line.yaw + pi).
    local crossTrack = -((tx-line.x)*cos(line.yaw)-(tz-line.z)*sin(line.yaw))
    local orientation = wrap(line.yaw-unitYaw)
    local hitch = wrap(unitYaw-yaw)
    local limit = math.rad(35)
    local wanted = math.max(-limit, math.min(limit, 1.5*orientation-0.05*crossTrack))
    local turn = math.max(-math.rad(75), math.min(math.rad(75), -2*(wanted-hitch)))
    local ahead = math.min(6, finite(radius) and radius or 6)
    ahead = math.min(ahead+abs(crossTrack), 2*ahead)
    return -sin(turn)*ahead, -cos(turn)*ahead, hitch, crossTrack, orientation
end

-- The straight line a towed rig reverses on: the towed unit's own axis through it
-- ({x, z, yaw}), or nil with nothing towed or nothing readable. Measured from the
-- tractor's line, a trailer left angled starts off it and the controller could not
-- settle it (tests: 23 degrees on 6 m reached a 72 degree hitch); on its own axis it
-- leads straight back and the tractor swings in behind it.
function Drive.towedLine(env, foot)
    local towed = foot ~= nil and foot.towed or nil
    if towed == nil then return nil end
    local node = towed.steeringAxleNode or towed.rootNode
    local ok, tx,_,tz = pcall(env.getWorldTranslation, node)
    local okD, dx,_,dz = pcall(env.localDirectionToWorld, node, 0,0,1)
    if not (ok and okD and finite(tx) and finite(tz) and finite(dx) and finite(dz)) then return nil end
    return { x = tx, z = tz, yaw = (math.atan2 or math.atan)(dx,dz) }
end

-- A rig left bent (its towed unit angled) whose route box, planned straight behind,
-- touches something its units do not (live 07d720c908304dc4-2: START_NOT_CLEAR beside
-- a tree after a straightening reverse stopped): first straight back along the unit's
-- own line, about its length and every metre of it clear there, so the rig comes into
-- line and a route can be planned from it. The line and the length, or nil.
function Drive.planStraighten(env, foot, x, z, probe)
    local line = Drive.towedLine(env, foot)
    if line == nil or not finite(foot.towLength) or foot.towLength <= 0 then return nil end
    local t = (x-line.x)*sin(line.yaw)+(z-line.z)*cos(line.yaw)
    local length = math.ceil(foot.towLength) + 2
    for d = 1, length do
        if probe(line.x+sin(line.yaw)*(t-d), line.z+cos(line.yaw)*(t-d), line.yaw, 0) ~= true then
            return nil, nil, d
        end
    end
    return line, length
end

-- The tractor's own part of the route box (from 1 m behind its root to its front): what
-- a forward straightening move sweeps first; the towed unit follows inside its path.
function Drive.frontFoot(foot)
    local front = {}
    for k, v in pairs(foot) do front[k] = v end
    front.aMin = math.max(foot.aMin, -1)
    front.towLength = 0
    return front
end

-- A rig left bent (its towed unit angled) whose route box, planned straight behind, touches
-- something none of its units does (Hitch.rigContacts): the move that brings it into line
-- first, back along the unit's line (Drive.planStraighten) or else forward with the
-- tractor's own box ahead clear (live 45b4ea8c443d4f3d-2: the tree was behind the
-- trailer). A straighten table for s.retreat or s.straightenForward, or nil.
function Drive.planUnbend(env, foot, Hitch, vehicle, x, z, yaw, clear, why)
    if type(Hitch.rigContacts) ~= "function" then return nil end
    local okC, contacts = pcall(Hitch.rigContacts, env, vehicle, foot)
    if not okC then return nil end
    if contacts ~= "-" then
        say("[Farmhand] drive rig: units in contact " .. tostring(contacts))
        return nil
    end
    local line, length, backBlocked = Drive.planStraighten(env, foot, x, z, clear)
    if line ~= nil then
        say(string.format("[Farmhand] drive rig left bent: the route box touches %s, every unit's own box is clear; straightening back %dm along the trailer first",
                          tostring(why), length))
        return { x = x, z = z, yaw = yaw, length = length, line = line, onTowed = true }
    end
    if Drive.towedLine(env, foot) == nil then return nil end
    local front, ahead = Drive.frontFoot(foot), math.ceil(foot.towLength) + 2
    local open = true
    for d = 1, ahead do
        if Hitch.routePoseClear(env, front, x + sin(yaw)*d, z + cos(yaw)*d, yaw, 0) ~= true then
            open = false
            break
        end
    end
    say(string.format("[Farmhand] drive rig left bent: the route box touches %s, every unit's own box is clear; back along the trailer blocked after %sm; %s",
                      tostring(why), tostring(backBlocked),
                      open and string.format("straightening forward up to %dm first", ahead) or "forward blocked too"))
    if not open then return nil end
    return { forward = true, x = x, z = z, yaw = yaw, length = ahead, front = front }
end

-- Why a reverse has left its checked line (nil while it holds it). With a towed unit
-- the line is the unit's (Drive.towedLine) and the unit is what must track it (live:
-- every reverse with an angled trailer stopped on the tractor's 15 degrees after
-- 0.2-7.6 m, hitch 0.08-0.15): it may not get further off or turn further away than
-- it began, the hitch stays inside the controller's 35 degrees (the jackknife guard),
-- and the tractor, measured from the same line, inside a wider bound. `trailer` =
-- {cross=, orient=, hitch=}, or nil; lateral and yawError are the tractor's.
function Drive.reverseOffLine(r, lateral, yawError, trailer)
    if trailer == nil then
        if lateral > 0.75 then return "lateral" end
        if abs(yawError) > pi/12 then return "heading" end
        return nil
    end
    local start = r.started or { cross = abs(trailer.cross), orient = abs(trailer.orient),
                                 hitch = abs(trailer.hitch), lateral = lateral, yaw = abs(yawError) }
    r.started = start
    if abs(trailer.cross) > math.max(0.75, start.cross + 0.25) then return "trailer lateral" end
    if abs(trailer.orient) > math.max(pi/12, start.orient + math.rad(5)) then return "trailer heading" end
    if abs(trailer.hitch) > math.max(trailer.limit or math.rad(35), start.hitch + math.rad(5)) then return "hitch" end
    if lateral > math.max(2, start.lateral + 1) or abs(yawError) > math.max(math.rad(35), start.yaw + math.rad(5)) then
        return "tractor"
    end
    return nil
end

-- A pivoting trailer cuts inside the tractor's arc. Requiring the tractor's
-- arc to preserve at least its own solo radius for the trailer gives this
-- geometric radius (Courseplay AIUtil uses the same axle relationship).
function Drive.turnRadius(vehicleRadius, towLength)
    if not (finite(vehicleRadius) and vehicleRadius >= 3 and finite(towLength)
            and towLength >= 0) then return nil end
    return sqrt(vehicleRadius * vehicleRadius + towLength * towLength)
end

-- Speed is a ceiling for this part of the route. A turn is approached at a
-- speed that can brake to its lateral-acceleration limit before the bend.
-- Arrival is approached separately. The overall travel ceiling is 64 km/h.
function Drive.travelSpeed(path, nextIndex, x, z, radius)
    if path == nil or #path == 0 or not (finite(x) and finite(z) and finite(radius)) then return nil end
    local final = path[#path]
    local remaining = distance(x, z, final[1], final[2])
    local target = math.min(64, math.max(1.5, sqrt(2 * math.max(0, remaining - 2)) * 3.6))
    if remaining < 15 then target = math.min(target, math.max(1.5, (remaining - 2) * 1.2)) end
    local ahead = distance(x, z, path[nextIndex][1], path[nextIndex][2])
    local turnKmh = sqrt(1.4 * radius) * 3.6
    for i = nextIndex, #path do
        if i > nextIndex then
            ahead = ahead + distance(path[i - 1][1], path[i - 1][2], path[i][1], path[i][2])
        end
        if i > 1 then
            local change = abs((path[i][3] - path[i - 1][3] + pi) % (2 * pi) - pi)
            if change > 0.08 then
                local brakingRoom = math.max(0, ahead - 4)
                local allowed = sqrt((turnKmh / 3.6) ^ 2 + 2 * brakingRoom) * 3.6
                target = math.min(target, allowed)
            end
        end
        if ahead > 100 then break end
    end
    return math.max(1.5, target)
end

-- Courseplay's pure pursuit projects the controlled node onto the relevant
-- segment, then aims a short distance along the path. A discrete waypoint
-- lookahead cut across bends and let the loaded rig oscillate off its route.
function Drive.goalPoint(path, nextIndex, x, z, lookahead)
    local bestD, bestI, bestT = math.huge, nil, nil
    for i = math.max(2,nextIndex-1), math.min(#path,nextIndex+2) do
        local a,b = path[i-1],path[i]
        local dx,dz = b[1]-a[1],b[2]-a[2]
        local length2 = dx*dx+dz*dz
        if length2 > 0 then
            local t = math.max(0,math.min(1,((x-a[1])*dx+(z-a[2])*dz)/length2))
            local d = distance(x,z,a[1]+dx*t,a[2]+dz*t)
            if d < bestD then bestD,bestI,bestT = d,i,t end
        end
    end
    if bestI == nil then return nil end
    local remaining = lookahead
    for i = bestI, #path do
        local a,b = path[i-1],path[i]
        local length = distance(a[1],a[2],b[1],b[2])
        local from = i == bestI and bestT or 0
        local available = length*(1-from)
        if remaining <= available then
            local t = from+remaining/length
            return a[1]+(b[1]-a[1])*t,a[2]+(b[2]-a[2])*t
        end
        remaining = remaining-available
    end
    return path[#path][1],path[#path][2]
end

-- The search also emits half-length escape segments. Recover the actual arc
-- length from its chord and yaw change; previewing all of them as four metres
-- probes past a validated endpoint and can reject an otherwise clear route.
function Drive.routeSample(path, index, metres)
    local from, to = path[index - 1], path[index]
    if from == nil or to == nil or not finite(metres) or metres < 0 or metres > STEP then return nil end
    local change = (to[3] - from[3] + pi) % (2 * pi) - pi
    local chord = distance(from[1], from[2], to[1], to[2])
    if chord < 0.05 then return nil end
    local length = abs(change) < 0.00001 and chord
        or chord * abs(change) / (2 * abs(sin(change / 2)))
    if not finite(length) or length > STEP + 0.1 then return nil end
    local gear = to[4] == -1 and -1 or 1
    local curvature = change / (gear * length)
    local x, z, yaw = advance(from[1], from[2], from[3], gear * math.min(metres, length), curvature)
    return x, z, yaw, curvature, length
end

function Drive.pathError(path, nextIndex, x, z)
    if path == nil or #path < 2 then return math.huge end
    local nearest = math.huge
    for i = math.max(2,nextIndex-2), math.min(#path,nextIndex+2) do
        local a,b = path[i-1],path[i]
        local dx,dz = b[1]-a[1],b[2]-a[2]
        local length2 = dx*dx+dz*dz
        if length2 > 0 then
            local t = math.max(0,math.min(1,((x-a[1])*dx+(z-a[2])*dz)/length2))
            nearest = math.min(nearest,distance(x,z,a[1]+dx*t,a[2]+dz*t))
        end
    end
    return nearest
end

function Drive.sectionReached(path, x, z, yaw)
    if path == nil or #path == 0 then return false end
    local endpoint = path[#path]
    return distance(x,z,endpoint[1],endpoint[2]) < 1
       and abs((yaw - endpoint[3] + pi) % (2*pi) - pi) < pi/12
end

-- GIANTS' steering actuator can keep rotating after the pursuit target has
-- changed sides. Reduce travel speed while observed cross-track or heading
-- error grows, giving the selected rig time to follow its clearance corridor.
function Drive.trackingSpeed(base, error, limit, headingError)
    if not (finite(base) and finite(error) and finite(limit) and limit > 0
            and finite(headingError)) then return nil end
    local remaining = math.max(0, 1 - error / limit)
    return math.min(base, math.max(3, base * remaining * remaining
                                     / (1 + 3 * headingError)))
end


-- The worst terrain a drive has driven over, measured once a second (Hitch.terrainProfile)
-- and logged at its end: the data the slope limits are to be set from.
local function profileText(p)
    return string.format("grade %.1f climb %.1f cross %.1f step %.2fm pitch %s roll %s",
                         p.grade, p.climb, p.cross, p.step,
                         p.pitch and string.format("%.1f", p.pitch) or "?",
                         p.roll and string.format("%.1f", p.roll) or "?")
end

local function measureTerrain(env, s, Hitch, x, z, yaw)
    if type(Hitch.terrainProfile) ~= "function" or s.elapsed < (s.terrainAt or 0) then return end
    s.terrainAt = s.elapsed + 1000
    local p = Hitch.terrainProfile(env, s.foot, x, z, yaw, s.tractor.rootNode)
    if p == nil then return end
    local worst = s.terrain or { samples = 0 }
    worst.samples = worst.samples + 1
    for _, k in ipairs({ "climb", "cross", "step" }) do
        if worst[k] == nil or p[k] > worst[k][1] then worst[k] = { p[k], x, z } end
    end
    for _, k in ipairs({ "grade", "pitch", "roll" }) do
        if p[k] ~= nil and (worst[k] == nil or math.abs(p[k]) > math.abs(worst[k][1])) then
            worst[k] = { p[k], x, z }
        end
    end
    s.terrain = worst
end

local function terrainSummary(s)
    local w = s.terrain
    if w == nil then return nil end
    local parts = {}
    for _, k in ipairs({ "grade", "climb", "cross", "step", "pitch", "roll" }) do
        if w[k] ~= nil then
            parts[#parts+1] = string.format("%s %.2f at %.0f,%.0f", k, w[k][1], w[k][2], w[k][3])
        end
    end
    return string.format("[Farmhand] drive terrain worst over %d samples: %s", w.samples, table.concat(parts, "; "))
end

local function pose(env, tractor)
    local x, y, z = env.getWorldTranslation(tractor.rootNode)
    local fx, _, fz = env.localDirectionToWorld(tractor.rootNode, 0, 0, 1)
    if not (finite(x) and finite(y) and finite(z) and finite(fx) and finite(fz)) then return nil end
    return x, z, atan2(fx, fz)
end

local function stopped(tractor)
    local speed = tractor:getLastSpeed()
    return finite(speed) and math.abs(speed) < 1
end

local function sameRig(env, s, Hitch)
    local fresh = Hitch.routeFootprint(env, s.tractor)
    if fresh == nil or #fresh.units ~= #s.foot.units then return false end
    for i, unit in ipairs(s.foot.units) do
        if fresh.units[i] ~= unit or unit.ownerFarmId ~= s.farmId then return false end
    end
    return true
end

-- A player's walked centre line is a continuous corridor. Its samples are
-- guidance for the rig-aware section planner, never separate stop targets.
function Drive.learnedCorridor(start, target, encoded)
    if type(encoded) ~= "string" or #encoded > 2048 then return nil end
    local points = {}
    for pair in encoded:gmatch("[^;]+") do
        local a, b = pair:match("^(-?%d+%.?%d*),(-?%d+%.?%d*)$")
        local x, z = tonumber(a), tonumber(b)
        if not finite(x) or not finite(z) or abs(x) > 100000 or abs(z) > 100000
                or #points >= 128 then return nil end
        points[#points+1] = {x,z}
    end
    if #points == 0 or encoded:sub(-1) == ";" then return nil end
    local nearest, best = 1, math.huge
    for i, point in ipairs(points) do
        local d = distance(start[1],start[2],point[1],point[2])
        if d < best then nearest, best = i, d end
    end
    local route = {{start[1],start[2]}}
    for i = nearest,#points do
        local point = points[i]
        if distance(route[#route][1],route[#route][2],point[1],point[2]) > 2 then
            route[#route+1] = point
        end
    end
    if distance(route[#route][1],route[#route][2],target[1],target[2]) > 2 then
        route[#route+1] = {target[1],target[2]}
    end
    local remaining = 0
    for i = #route,1,-1 do
        route[i][3] = remaining
        if i > 1 then remaining = remaining + distance(route[i][1],route[i][2],route[i-1][1],route[i-1][2]) end
    end
    route.learned = true
    return route
end

-- The drive's target as the searches take it: its current point and, for a
-- place, every approach point the corridor search may end at instead.
local function destination(s)
    return { s.targetX, s.targetZ, goals = s.goals }
end

-- A corridor from `start`; for a place, the approach point it reaches becomes
-- the drive's target.
local function corridorFrom(s, start, clear, avoided)
    local route = s.learned and Drive.learnedCorridor(start, {s.targetX,s.targetZ}, s.learned)
                  or guidedRoute(start, destination(s), clear, s.radius, avoided, s.navHint)
    if route ~= nil then route.goalYaw = s.goalYaw end
    if route ~= nil and s.goals ~= nil then
        local last = route[#route]
        if distance(last[1], last[2], s.targetX, s.targetZ) > 0.01 then
            say(string.format("[Farmhand] drive approaches the place at %.1f,%.1f, %.0fm from its centre (reachable of %d clear points)",
                              last[1], last[2], distance(last[1], last[2], s.placeX, s.placeZ), #s.goals))
            s.targetX, s.targetZ = last[1], last[2]
        end
    end
    return route
end

-- A short turning reverse is replanned from the observed rig after each leg.
-- A towed rig needs a readable steering axle and enough length to measure its
-- hitch angle; the reverse frame checks that angle before every control step.
local function canSteerReverse(s)
    if s.foot == nil then return false end
    if (s.foot.towLength or 0) <= 0 and s.foot.towed == nil then return true end
    return s.foot.towed ~= nil and (s.foot.towLength or 0) > 3
        and (s.foot.towed.steeringAxleNode or s.foot.towed.rootNode) ~= nil
end

-- One leg of a shuffle: forward by the route follower, slowly, or straight back
-- by the collision-checked reverse. The rest is planned again from the pose each
-- leg stops in; the last leg ends on the corridor and the route goes on from there.
local function beginLeg(s, path, x, z, yaw)
    local gear = path[2] ~= nil and path[2][4] or 1
    local curve = gear < 0 and path[2][5] or 0
    local last = math.min(2, #path)
    while path[last+1] ~= nil and (path[last+1][4] or 1) == gear
            and (gear > 0 or (path[last+1][5] or 0) == curve) do last = last + 1 end
    s.shuffleLegs = (s.shuffleLegs or 0) + 1
    if gear < 0 then
        local length = (last-1)*STEP
        if s.foot.towed ~= nil and curve ~= 0 then length = math.min(length, 2) end
        s.retreat, s.path = {x=x,z=z,yaw=yaw,length=length,curve=curve ~= 0 and curve or nil}, nil
        s.maxSpeedKmh,s.commandShare,s.driveStartMs = 3,0.5,nil
        say(string.format("[Farmhand] drive shuffle leg %d: reverse %.0fm%s from %.1f,%.1f yaw %.2f",
                          s.shuffleLegs,(last-1)*STEP,curve ~= 0 and string.format(" turning %+.3f",curve) or "",x,z,yaw))
        return
    end
    local leg = {}
    for i = 1, last do leg[i] = path[i] end
    local final = path[last+1] == nil
    Drive.activateSection(s, leg, final and s.shuffle.reached or s.reached, x, z)
    -- A leg ending in a gear change aims at 5 km/h under an 8 km/h overspeed cap
    -- (live 4e62355bbe684190-4: aimed at its own 5 km/h cap, 5.01 latched OVERSPEED).
    s.maxSpeedKmh, s.commandShare, s.legKmh = final and 64 or 8, nil, not final and 5 or nil
    say(string.format("[Farmhand] drive shuffle leg %d: forward %.0fm from %.1f,%.1f yaw %.2f to %.1f,%.1f yaw %.2f%s",
                      s.shuffleLegs,(last-1)*STEP,x,z,yaw,leg[last][1],leg[last][2],leg[last][3],
                      final and "; back on the corridor" or ""))
    if final then s.shuffle = nil end
end

-- The next legs of a running shuffle from the observed pose; false when none.
local function shuffleOn(s, start, clear)
    local sh = s.shuffle
    s.shuffle = nil
    s.maxSpeedKmh, s.legKmh = 64, nil       -- the route goes on at its own speeds, else the next leg sets them
    if (s.shuffleLegs or 0) >= Drive.SHUFFLE_LEGS then
        say(string.format("[Farmhand] drive shuffle ended after %d legs", s.shuffleLegs))
        return false
    end
    local path = Drive.plan(start, sh.waypoint, s.radius, clear, sh.guide, sh.goalYaw, true,
                            SHUFFLE_LIMIT, Drive.SHUFFLE_SWITCHES, canSteerReverse(s))
    if path == nil then
        say(string.format("[Farmhand] drive shuffle has no next leg from %.1f,%.1f yaw %.2f",
                          start[1],start[2],start[3]))
        return false
    end
    s.shuffle = sh
    beginLeg(s, path, start[1], start[2], start[3])
    return true
end

-- A rig with no forward continuation and no straight escape turns out of the
-- place by a planned forward/reverse shuffle onto its corridor.
local function startShuffle(s, start, clear, after)
    if (s.shuffleLegs or 0) >= Drive.SHUFFLE_LEGS then return "NO_ROUTE" end
    local corridor = s.corridor or corridorFrom(s, start, clear)
    if corridor == nil then return "NO_ROUTE" end
    local reached = s.corridor ~= nil and s.reached or 1
    local function onto(corridor, reached)
        for attempt = 1, 2 do
            local path, i = Drive.section(start,s.radius,clear,corridor,reached,attempt,Drive.SHUFFLE_SWITCHES,
                                          canSteerReverse(s))
            if path ~= nil then
                local _, waypoint, guide, goalYaw = sectionGoal(corridor, reached, attempt)
                s.corridor, s.reached, s.recovering, s.replans = corridor, reached, false, 0
                s.shuffle = { waypoint = waypoint, guide = guide, goalYaw = goalYaw, reached = i }
                local gears = 0
                for n = 3, #path do
                    if (path[n][4] or 1) ~= (path[n-1][4] or 1) then gears = gears + 1 end
                end
                say(string.format("[Farmhand] drive shuffle after %s: %d steps, %d gear changes, to corridor %d/%d",
                                  after, #path, gears, i, #corridor))
                beginLeg(s, path, start[1], start[2], start[3])
                return true
            end
        end
        return false
    end
    if onto(corridor, reached) then return "CONTINUE" end
    -- The way this corridor goes on is closed to the rig: avoid its next point, inside
    -- the rig's own distance to it (an area around the rig is never avoided), and
    -- shuffle onto another corridor (live 47d6feb676f24e32-2: a gate the rig could
    -- face but not turn through, every alternate corridor through it).
    for n = reached + 1, #corridor do
        local point = corridor[n]
        local d = distance(start[1], start[2], point[1], point[2])
        if d >= 7 then
            local avoided = {}
            for _, area in ipairs(s.avoided or {}) do avoided[#avoided+1] = area end
            avoided[#avoided+1] = {point[1], point[2], math.min(math.max(12, s.radius*1.5), d - 3)}
            local alternate = corridorFrom(s, start, clear, avoided)
            if alternate ~= nil then
                say(string.format("[Farmhand] drive shuffle tries another corridor (%d nodes) avoiding %.1f,%.1f",
                                  #alternate, point[1], point[2]))
                s.avoided = avoided
                if onto(alternate, 1) then return "CONTINUE" end
            end
            -- Not even another corridor from here (a lane the grid's headings miss,
            -- live 33e1238ca71b481d-2): straight back out, where one avoiding it begins.
            local back = Drive.planBackOut(start, destination(s), s.radius, clear, s.navHint, avoided)
            if back ~= nil then
                s.avoided = avoided
                s.retreat = {x=start[1],z=start[2],yaw=start[3],length=back,backOut=true}
                s.shuffleLegs = (s.shuffleLegs or 0) + 1
                s.path,s.corridor,s.recovering = nil,nil,false
                s.maxSpeedKmh,s.commandShare,s.driveStartMs = 3,0.5,nil
                say(string.format("[Farmhand] drive backs %.0fm out to a corridor avoiding %.1f,%.1f",
                                  back, point[1], point[2]))
                return "CONTINUE"
            end
            break
        end
    end
    for _, move in ipairs({ {1,0}, {1,-1}, {1,1}, {-1,0} }) do
        local px, pz, pyaw = advance(start[1], start[2], start[3], 2 * move[1], move[2] / s.radius)
        local pass, why = clear(px, pz, pyaw, move[2] / s.radius)
        say(string.format("[Farmhand] drive no shuffle from %.1f,%.1f yaw %.2f: %s 2m steer %d clear=%s %s",
                          start[1], start[2], start[3], move[1] > 0 and "forward" or "reverse", move[2],
                          tostring(pass), tostring(why)))
    end
    -- A route can be unreachable from a rig's current pose while the first
    -- reverse metres are clear. Move those metres instead of ending NO_ROUTE
    -- at the exact contact point, then search again from the observed pose.
    local backClear = true
    for metres = 1, 2 do
        local bx, bz = advance(start[1], start[2], start[3], -metres, 0)
        if clear(bx, bz, start[3], 0, -1) ~= true then backClear = false; break end
    end
    local length, curve = backClear and 2 or nil, 0
    if length == nil and s.foot.leaving ~= nil then
        -- At actual contact, a long rig may have less than a metre before its
        -- empty bounding-box margin meets scenery. Probe 0.5 m arcs against
        -- each real unit and steer the reverse toward the clearest side.
        local lateral = (s.targetX-start[1])*cos(start[3]) - (s.targetZ-start[2])*sin(start[3])
        local preferred = lateral >= 0 and 1 or -1
        s.foot.departureNudge = true
        local best = 0
        for _, turn in ipairs({preferred, -preferred, 0}) do
            local candidate = turn / s.radius
            local bx,bz,byaw = advance(start[1],start[2],start[3],-0.5,candidate)
            local pass, why = clear(bx,bz,byaw,candidate,-1)
            say(string.format("[Farmhand] drive reverse contact turn %d half-metre clear=%s %s",
                              turn,tostring(pass),tostring(why)))
            if pass == true then
                local fartherX,fartherZ,fartherYaw = advance(start[1],start[2],start[3],-1,candidate)
                local far = clear(fartherX,fartherZ,fartherYaw,candidate,-1) == true and 1 or 0.5
                if far > best then best,length,curve = far,0.5,candidate end
            end
        end
        if length == nil then s.foot.departureNudge = nil end
    end
    if length ~= nil then
        s.retreat = {x=start[1],z=start[2],yaw=start[3],length=length,curve=curve ~= 0 and curve or nil,backOut=true}
        s.shuffleLegs = (s.shuffleLegs or 0) + 1
        s.path,s.corridor,s.recovering = nil,nil,false
        s.maxSpeedKmh,s.commandShare,s.driveStartMs = 2,0.5,nil
        say(string.format("[Farmhand] drive backs %.1fm from tight contact with turn %+.3f, then replans (%s)",
                          length,curve,tostring(after)))
        return "CONTINUE"
    end
    return "NO_ROUTE"
end

-- The first route of a drive, as a replayable planning step (Drive.begin, and
-- Drive.step while it continues over frames). nil, or the failure code.
local function initialPlan(env, s, Hitch)
    local function clear(px, pz, pyaw, curve, gear)
        return Drive.routeProbe(env, s.foot, Hitch, px, pz, pyaw, curve, gear)
    end
    local start, target = s.initial.start, { s.targetX, s.targetZ }
    local path, why, corridor, reached, sectionAttempt, retreat, backOut
    if s.learned == nil and distance(start[1],start[2],target[1],target[2]) <= 200 then
        path, why = Drive.plan(start,target,s.radius,clear,nil,s.goalYaw,true)
    end
    if path == nil and s.egress == nil then
        retreat = not s.shortRetreatUsed and Drive.planRetreat(start,s.radius,clear) or nil
        if retreat ~= nil then
            if retreat < 4 then s.shortRetreatUsed = true end
            say(string.format("[Farmhand] drive collision-checked reverse departure %.1fm", retreat))
        end
    end
    if path == nil and retreat == nil then
        corridor = corridorFrom(s,start,clear)
        -- No corridor from where it was left (live 7353315fb9eb453a-3: parked at the
        -- barn's approach point between a shed and a tree): straight back out to where
        -- one begins, and turn onto it there.
        if corridor == nil and s.egress == nil then
            retreat = Drive.planBackOut(start,destination(s),s.radius,clear,s.navHint,nil)
            if retreat == nil then return "NO_ROUTE" end
            backOut = true
            say(string.format("[Farmhand] drive no corridor from here; backs %.0fm out to where one begins", retreat))
        elseif corridor == nil then
            return "NO_ROUTE"
        end
    end
    if path == nil and retreat == nil then
        path,reached = Drive.section(start,s.radius,clear,corridor,1,1)
        if path == nil then
            if s.egress == nil then
                retreat = Drive.planRetreatRoute(start,destination(s),s.radius,clear,s.navHint)
            end
            if retreat ~= nil then
                corridor,reached = nil,nil
                say(string.format("[Farmhand] drive rig-clear reverse escape %.1fm before route", retreat))
            else
                reached,sectionAttempt = 1,2
                say("[Farmhand] drive first corridor section blocked; trying alternate spans")
            end
        else
            say(string.format("[Farmhand] drive starting corridor section 1-%d/%d from %.1f,%.1f yaw %.2f to %.1f,%.1f yaw %.2f steps %d",
                                reached,#corridor,start[1],start[2],start[3],
                                path[#path][1],path[#path][2],path[#path][3],#path))
        end
    end
    s.path, s.corridor, s.reached, s.sectionAttempt = path, corridor, reached, sectionAttempt or 1
    if retreat ~= nil then
        s.retreat = { x = s.initial.x, z = s.initial.z, yaw = s.initial.yaw, length = retreat,
                      backOut = backOut }
        s.maxSpeedKmh, s.commandShare = 3, 0.5
    end
    -- The search time is not time spent stalled (live 2268b28b08994e3b-2).
    s.lastMovedX, s.lastMovedZ, s.movedAt = s.initial.x, s.initial.z, s.elapsed
    s.initial = nil
    return nil
end

local function snapshot(s)
    local copy = {}
    for k, v in pairs(s) do copy[k] = v end
    return copy
end

-- Back to the state a replayed planning step began with (its clock keeps running).
local function restore(s, saved)
    for k in pairs(s) do
        if saved[k] == nil and k ~= "elapsed" and k ~= "episode" then s[k] = nil end
    end
    for k, v in pairs(saved) do
        if k ~= "elapsed" and k ~= "episode" then s[k] = v end
    end
end

-- Start only from an observed, reserved, stopped rig. The planner uses a
-- conservative whole-rig collision box; execution checks the live rig again.
function Drive.begin(env, args, Hitch)
    if type(args) ~= "table" or type(args.isReserved) ~= "function" or args.isReserved() ~= true then
        return nil, "RESERVATION_LOST"
    end
    Drive.scanSurface = type(Hitch.surfaceScan) == "function"
        and function(...) return Hitch.surfaceScan(env, ...) end or nil
    local ok, session, reason = pcall(function()
        local v = args.tractor
        if v == nil or v.ownerFarmId ~= args.farmId then return nil, "NOT_OWNED" end
        if v.rootVehicle ~= nil and v.rootVehicle ~= v then return nil, "NOT_ROOT_VEHICLE" end
        if not stopped(v) then return nil, "NOT_STOPPED" end
        if v:getIsAIActive() ~= false then return nil, "AI_ACTIVE" end
        local seat = v:getIsEntered()
        if seat ~= true and seat ~= false then return nil, "UNKNOWN_OBSERVATION" end
        if not (finite(args.x) and finite(args.z)) then return nil, "INVALID_DESTINATION" end
        local x, z, yaw = pose(env, v)
        if x == nil then return nil, "UNKNOWN_OBSERVATION" end
        local radius = v:getAITurningRadius(v.maxTurningRadius)
        if not finite(radius) or radius < 3 then return nil, "TURNING_RADIUS_UNKNOWN" end
        local foot = Hitch.routeFootprint(env, v)
        if foot == nil then return nil, "RIG_GEOMETRY_UNKNOWN" end
        radius = Drive.turnRadius(radius, foot.towLength or 0)
        if radius == nil then return nil, "TURNING_RADIUS_UNKNOWN" end
        if args.via ~= nil and Drive.learnedCorridor({x,z,yaw}, {args.x,args.z}, args.via) == nil then
            return nil, "INVALID_DESTINATION"
        end
        local function clear(px, pz, pyaw, curve, gear)
            return Drive.routeProbe(env, foot, Hitch, px, pz, pyaw, curve, gear)
        end
        local startClear, startReason, hits = Hitch.routePoseClear(env, foot, x, z, yaw)
        local egress, ghostedAtStart
        local straighten = startClear == false and hits ~= nil and #hits > 0
                           and Drive.planUnbend(env, foot, Hitch, v, x, z, yaw, clear, startReason) or nil
        if straighten ~= nil then
            -- planned after the straightening reverse, from the pose it ends in
        elseif startClear == false and hits ~= nil and #hits > 0 then
            say("[Farmhand] drive start clearance: " .. tostring(startReason))
            -- One contacted placeable can expose several collision nodes. Leave
            -- its nodes out only while departing the initial contact area.
            local vehicle, fixed, ox, oz, n = nil, false, 0, 0, 0
            local fixedObjects = {}
            for _, node in ipairs(hits) do
                local object = env.g_currentMission:getNodeObject(node)
                local other = object ~= nil and object.rootVehicle or nil
                if other ~= nil then
                    if vehicle ~= nil and vehicle ~= other then return nil, "START_NOT_CLEAR" end
                    vehicle = other
                else
                    fixed = true
                    if object ~= nil then fixedObjects[object] = true end
                end
                local okAt, hx, _, hz = pcall(env.getWorldTranslation, node)
                if not (okAt and finite(hx) and finite(hz)) then return nil, "START_NOT_CLEAR" end
                ox, oz, n = ox + hx, oz + hz, n + 1
            end
            if vehicle ~= nil and fixed then return nil, "START_NOT_CLEAR" end
            ox, oz = ox / n, oz / n
            if vehicle ~= nil then
                if vehicle.rootNode == nil or vehicle.ownerFarmId ~= args.farmId or not stopped(vehicle) then
                    return nil, "START_NOT_CLEAR"
                end
                ox, _, oz = env.getWorldTranslation(vehicle.rootNode)
            end
            local ignoredFoot, excluded = {}, {}
            for key, value in pairs(foot) do ignoredFoot[key] = value end
            for key, value in pairs(foot.excluded) do excluded[key] = value end
            if vehicle ~= nil then
                excluded[vehicle] = true
            else
                for _, node in ipairs(hits) do excluded[node] = true end
                for object in pairs(fixedObjects) do excluded[object] = true end
            end
            ignoredFoot.excluded = excluded
            local function ignoring(px, pz, pyaw)
                return Hitch.routePoseClear(env, ignoredFoot, px, pz, pyaw)
            end
            local dir = ((ox - x) * sin(yaw) + (oz - z) * cos(yaw)) > 0 and -1 or 1
            local length, why = Drive.planEgress({ x, z, yaw }, { ox, oz }, clear, ignoring, dir)
            if vehicle == nil then
                -- Scenery's node is no measure of where it touches (live: a prop's origin 20 m
                -- from the pole the 3650 touched; "6 m ahead" through it, EGRESS_DIVERGED): the
                -- way that clears soonest, ahead or back, measured; its side behind the move.
                local best
                for _, d in ipairs({ -1, 1 }) do
                    local away = { x - sin(yaw) * d * 10, z - cos(yaw) * d * 10 }
                    local m = Drive.planEgress({ x, z, yaw }, away, clear, ignoring, d)
                    if m ~= nil and (best == nil or m < best[1]) then best = { m, d, away } end
                end
                if best ~= nil then
                    length, why, dir = best[1], nil, best[2]
                    ox, oz = best[3][1], best[3][2]
                end
            end
            if length == nil then
                -- No straight way off it (live 8570c6316cb9406f-2..4: the GMC's trailer against the
                -- 3650 parked beside it, EGRESS_NOT_CLEAR, every "take the gmc to field 90" refused):
                -- the route is planned as ever, the touched object alone left out while the rig is
                -- within Drive.LEAVE_M of where it stood, so its first moves are the ones away from it.
                foot.leaving = { x = x, z = z, radius = Drive.LEAVE_M, excluded = excluded }
                say(string.format("[Farmhand] drive starts touching %s, no straight way off it (%s): leaving it by the route",
                                  vehicle ~= nil and tostring(vehicle.getName and vehicle:getName() or vehicle.typeName)
                                  or tostring(startReason), tostring(why)))
            end
            local touching = false
            if vehicle == nil and type(Hitch.rigContacts) == "function" then
                local observed, contacts = pcall(Hitch.rigContacts, env, v, foot)
                touching = observed and type(contacts) == "string"
                    and contacts:find("standing", 1, true) ~= nil
            end
            if vehicle == nil and touching and args.ghost == true
                    and type(Hitch.ghost) == "function" then
                -- Against scenery, with the player's opt-in setting: the departure is not held by
                -- it (live 1682e794a2ee4476-1: every straight back-off from a structure's leg
                -- stopped dead after 0.5 m). Without the setting the measured departure below
                -- leaves it under normal collision.
                if Hitch.ghost(env, v, "START_AGAINST_SCENERY", hits, true) then
                    ghostedAtStart = true
                    say("[Farmhand] drive starts against scenery: the rig's collision with buildings off")
                end
            end
            if length ~= nil then
                egress = { obstacle = vehicle, ox = ox, oz = oz, initialDistance = distance(x, z, ox, oz),
                           lastDistance = distance(x, z, ox, oz), x = x, z = z, yaw = yaw, dir = dir,
                           ignoredFoot = ignoredFoot, plannedM = length }
                say(string.format("[Farmhand] drive bounded departure %.1fm %s from %s", length,
                                    dir > 0 and "ahead" or "back",
                                    vehicle ~= nil and tostring(vehicle.getName and vehicle:getName() or vehicle.typeName)
                                    or tostring(startReason)))
            end
        elseif startClear ~= true then
            say("[Farmhand] drive start clearance: " .. tostring(startReason))
            return nil, "START_NOT_CLEAR"
        end
        -- area > 0: (x, z) is a place's centre, reached at any clear approach point.
        local goals, targetX, targetZ = nil, args.x, args.z
        if args.area ~= nil and args.area ~= 0 then
            if not (finite(args.area) and args.area > 0 and args.area <= Drive.MAX_APPROACH_M) then
                return nil, "INVALID_DESTINATION"
            end
            goals = Drive.approaches(args.x, args.z, args.area, clear)
            if #goals == 0 then return nil, "NO_CLEAR_APPROACH" end
            local nearest = math.huge
            for _, goal in ipairs(goals) do
                local d = distance(x, z, goal[1], goal[2]) + goal[3]
                if d < nearest then nearest, targetX, targetZ = d, goal[1], goal[2] end
            end
            say(string.format("[Farmhand] drive place at %.1f,%.1f: %d clear approach points within %.0fm",
                              args.x, args.z, #goals, args.area))
        end
        local navHint = Drive.navHint(env)
        local planStart
        if egress == nil then
            planStart = {x,z,yaw}
        else
            local px = x + sin(yaw) * egress.plannedM * (egress.dir or 1)
            local pz = z + cos(yaw) * egress.plannedM * (egress.dir or 1)
            planStart = {px,pz,yaw}
        end
        local cruise = v.spec_drivable and v.spec_drivable.cruiseControl
        local savedCruise = cruise and cruise.speed
        local vehicleMax = v:getCruiseControlMaxSpeed()
        if not (finite(savedCruise) and finite(vehicleMax) and vehicleMax >= 1) then
            return nil, "CRUISE_UNREADABLE"
        end
        local travelLimit = math.min(64, vehicleMax)
        local s = { tractor = v, farmId = args.farmId, isReserved = args.isReserved,
                    maxSpeedKmh = egress ~= nil and 3 or 64,
                    commandShare = egress ~= nil and 0.5 or nil, elapsed = 0,
                    cruiseBefore = savedCruise, travelLimit = travelLimit,
                    targetX = targetX, targetZ = targetZ, goals = goals,
                    learned = args.via,
                    placeX = args.x, placeZ = args.z,
                    -- yaw: the heading to arrive at (a staging pose); beyond +-2 pi none.
                    goalYaw = goals == nil and finite(args.yaw) and abs(args.yaw) <= 2 * pi and args.yaw or nil,
                    path = nil, next = 2, foot = foot, egress = egress, radius = radius,
                    sectionAttempt = 1, navHint = navHint,
                    result = nil, movedAt = 0,
                    lastMovedX = x, lastMovedZ = z, replans = 0, ghostedHere = ghostedAtStart,
                    ghostUses = ghostedAtStart and 1 or nil, ghost = args.ghost == true,
                    initial = { start = planStart, x = x, z = z, yaw = yaw } }
        if straighten ~= nil then
            s.initial = nil
            if straighten.forward then s.straightenForward = straighten else s.retreat = straighten end
            s.maxSpeedKmh, s.commandShare = 3, 0.5
        end
        -- The checked departure is the first maneuver. Search from its measured
        -- end pose, not a predicted endpoint that may still touch the obstacle.
        if egress ~= nil then s.initial = nil end
        if foot.leaving ~= nil and s.retreat == nil then
            -- The rig is already against something. Its first physical move
            -- must be a measured reverse, not a forward route or shuffle.
            foot.departureNudge = true
            local lateral = (targetX-x)*cos(yaw) - (targetZ-z)*sin(yaw)
            local preferred = lateral >= 0 and 1 or -1
            if foot.towed ~= nil then
                local line = Drive.towedLine(env, foot)
                if line ~= nil then
                    local _, _, hitch = Drive.reverseAim(env, foot, line, yaw, radius)
                    if finite(hitch) and abs(hitch) > math.rad(5) then
                        preferred = hitch > 0 and -1 or 1
                    end
                end
            end
            for _, turn in ipairs({preferred,-preferred,0}) do
                local curve = turn / radius
                local bx,bz,byaw = advance(x,z,yaw,-0.5,curve)
                if clear(bx,bz,byaw,curve,-1) == true then
                    s.retreat = {x=x,z=z,yaw=yaw,length=Drive.LEAVE_M,
                                 curve=curve ~= 0 and curve or nil,backOut=true,contact=true}
                    s.initial = nil
                    s.maxSpeedKmh,s.commandShare = 2,0.5
                    say(string.format("[Farmhand] drive contact: reversing first with turn %d before route search",turn))
                    break
                end
            end
            if s.retreat == nil then
                foot.departureNudge = nil
                return nil, "START_NOT_CLEAR"
            end
        end
        local saved = snapshot(s)
        local code = s.initial ~= nil and planned(s, gameClock(env), initialPlan, env, s, Hitch) or nil
        if code == PENDING then
            s.episode.saved, s.episode.pose = saved, { x, z, yaw }
            print("[Farmhand] drive route search continues over the next frames; the rig waits braked")
        elseif code ~= nil then
            return nil, code
        end
        Hitch.watchDriver(env, v)
        v:setCruiseControlMaxSpeed(travelLimit)
        return s
    end)
    if not ok then return nil, "UNKNOWN_OBSERVATION" end
    return session, reason
end

-- Only with the player's opt-in setting (s.ghost, from the drive's command): near buildings the
-- rig stops colliding with the scenery obstacles near it (Hitch.ghost, pair by pair), back once
-- it is clear of them; checked twice a second, at once when held fast. Planning keeps its
-- clearances. Without the setting a drive keeps the game's real collisions throughout.
local function ghostWatch(env, s, Hitch, x, z, yaw, why)
    if s.ghost ~= true then return end
    if type(Hitch.ghost) ~= "function" then return end
    local ghosted = Hitch.ghosted[s.tractor] ~= nil
    if why == nil then
        if s.elapsed < (s.ghostAt or 0) then return end
        s.ghostAt = s.elapsed + 500
    end
    local nodes = Hitch.staticNodesNear(env, s.foot, x, z, yaw, Hitch.GHOST.nearM)
    if nodes == nil then return end
    if #nodes == 0 then
        if ghosted and s.ghostedHere and Hitch.staticNear(env, s.foot, x, z, yaw, Hitch.GHOST.clearM) == false then
            Hitch.unghost(env, s.tractor)
            s.ghostedHere = nil
            say(string.format("[Farmhand] drive clear of buildings at %.1f,%.1f: collision with them restored", x, z))
        end
        return
    end
    local ok, added = Hitch.ghost(env, s.tractor, why or "NEAR_BUILDINGS", nodes, true)
    if ok and (added or 0) > 0 then
        s.ghostedHere = true
        s.ghostUses = (s.ghostUses or 0) + 1
        say(string.format("[Farmhand] drive %s at %.1f,%.1f: the rig's collision with %d scenery obstacle(s) off",
                          why or "NEAR_BUILDINGS", x, z, added))
    elseif not ok and not s.ghostRefusedLogged and why ~= nil then
        s.ghostRefusedLogged = true
        say(string.format("[Farmhand] drive %s at %.1f,%.1f: collision kept (%s)", why, x, z, tostring(added)))
    end
end
Drive.ghostWatch = ghostWatch

local function guard(env, s, Hitch, x, z, yaw, atTarget)
    if s.isReserved() ~= true or s.tractor.ownerFarmId ~= s.farmId then return "RESERVATION_LOST" end
    local conflict = Hitch.driverConflict(s.tractor)
    if conflict ~= nil then return conflict end
    if s.tractor:getIsAIActive() ~= false then return "AI_ACTIVE" end
    if not sameRig(env, s, Hitch) then return "RIG_CHANGED" end
    local seat = s.tractor:getIsEntered()
    if seat ~= true and seat ~= false then return "UNKNOWN_OBSERVATION" end
    local playerX, playerZ
    -- A player seated in any vehicle is no person on foot (GIANTS' Player:getIsInVehicle, as
    -- Hitch's surroundings; live 46b3e43ac1da4902-1: the player in the combine, the trailer's
    -- approach beside it ended PERSON_TOO_CLOSE).
    local player = env.g_localPlayer
    local inVehicle = player ~= nil and type(player.getIsInVehicle) == "function" and player:getIsInVehicle() == true
    if seat == false and not inVehicle then
        local px, _, pz
        if type(player.getPosition) == "function" then px, _, pz = player:getPosition()
        else px, _, pz = env.getWorldTranslation(player.rootNode) end
        if not (finite(px) and finite(pz)) then return "UNKNOWN_OBSERVATION" end
        if distance(px, pz, x, z) < 6 then return "PERSON_TOO_CLOSE" end
        playerX, playerZ = px, pz
    end
    ghostWatch(env, s, Hitch, x, z, yaw)
    local probeFoot = s.egress ~= nil and s.egress.ignoredFoot or s.foot
    if Hitch.ghosted ~= nil and Hitch.ghosted[s.tractor] ~= nil then
        -- Ghosted: the scenery it no longer collides with does not hold it; the rest counts.
        probeFoot = Hitch.withGhosted(env, probeFoot, s.tractor)
    end
    local here, hereReason = Hitch.routePoseClear(env, probeFoot, x, z, yaw)
    if here ~= true then
        -- The route box is one rigid box at the root's heading; in a bend an
        -- articulated rig's units are not there. Each unit's own observed box
        -- (Hitch.rigContacts, as LOAD's envelope watch) decides actual contact.
        local contacts = "?"
        if here == false and s.egress == nil and type(Hitch.rigContacts) == "function" then
            local ok, observed = pcall(Hitch.rigContacts, env, s.tractor, s.foot)
            contacts = ok and observed or "?"
        end
        if contacts ~= "-" then
            say(string.format("[Farmhand] drive current rig pose blocked at %.1f,%.1f yaw %.2f: %s; units %s",
                                x,z,yaw,tostring(hereReason),tostring(contacts)))
            return here == nil and "UNKNOWN_OBSERVATION" or "ENVELOPE_CONTACT"
        end
        if not s.rigidBoxLogged then
            s.rigidBoxLogged = true
            say(string.format("[Farmhand] drive rigid route box touches %s at %.1f,%.1f yaw %.2f; every unit's own box is clear",
                                tostring(hereReason),x,z,yaw))
        end
    end
    if atTarget then return nil end
    -- A bounded straight departure has no path preview. Ordinary driving
    -- previews the actual rig-aware arcs below; checking a point straight in
    -- front would reject a valid turn around an object beside that arc.
    if s.egress ~= nil then
        local dir = s.egress.dir or 1
        local aheadX, aheadZ = x + sin(yaw) * 2 * dir, z + cos(yaw) * 2 * dir
        local ahead, aheadReason = Hitch.routePoseClear(env, probeFoot, aheadX, aheadZ, yaw)
        if ahead ~= true then
            say(string.format("[Farmhand] drive front blocked at %.2f,%.2f yaw %.2f: %s",
                                aheadX, aheadZ, yaw, tostring(aheadReason)))
            return ahead == nil and "UNKNOWN_OBSERVATION" or "OBSTACLE_AHEAD"
        end
    end
    if s.egress == nil and s.retreat == nil and s.path ~= nil and s.elapsed >= (s.previewAt or 0) then
        s.previewAt = s.elapsed + 250
        local kmh = s.measuredKmh or math.abs(s.tractor:getLastSpeed())
        if not finite(kmh) then return "UNKNOWN_OBSERVATION" end
        local metresPerSecond = kmh / 3.6
        local horizon = math.max(8, metresPerSecond * 0.5 + metresPerSecond * metresPerSecond / 4 + 5)
        local traveled = 0
        for i = s.next, #s.path do
            local segmentLength
            for metres = 1, STEP do
                local px, pz, pyaw, turn, length = Drive.routeSample(s.path, i, metres)
                if px == nil then return "UNKNOWN_OBSERVATION" end
                segmentLength = length
                if playerX ~= nil and distance(px, pz, playerX, playerZ) < 6 then
                    return "PERSON_TOO_CLOSE"
                end
                local clear, why = Hitch.routePoseClear(env, s.foot, px, pz, pyaw, turn)
                if clear ~= true then
                    say(string.format("[Farmhand] drive preview blocked at %.2f,%.2f yaw %.2f: %s",
                                        px, pz, pyaw, tostring(why)))
                    return clear == nil and "UNKNOWN_OBSERVATION" or "OBSTACLE_AHEAD"
                end
                if metres >= length then break end
            end
            traveled = traveled + segmentLength
            if traveled >= horizon then break end
        end
    end
    return nil
end

local function halt(env, s, dt, Hitch)
    Hitch.halt(env, s, dt)
    if s.cruiseBefore ~= nil then
        pcall(s.tractor.setCruiseControlMaxSpeed, s.tractor, s.cruiseBefore)
        s.cruiseBefore = nil
    end
end

-- At the target: within 3 m, and at its heading (within 20 degrees) when one was asked.
function Drive.arrived(s, x, z, yaw)
    return distance(x, z, s.targetX, s.targetZ) < 3
        and (s.goalYaw == nil or abs((yaw - s.goalYaw + pi) % (2 * pi) - pi) < pi / 9)
end

-- One frame of Farmhand's own route follower, under the existing measured-speed
-- governor, takeover watch, brake and vehicle-control restoration. Any end latches.
local function frame(env, s, dt, Hitch, x, z, yaw)
    local leaving = s.foot and s.foot.leaving
    if leaving ~= nil and distance(x, z, leaving.x, leaving.z) > leaving.radius then
        s.foot.leaving = nil               -- clear of what it started against: everything counts
    end
    local atTarget = Drive.arrived(s, x, z, yaw)
    local denied = guard(env, s, Hitch, x, z, yaw, atTarget)
    if denied == "OBSTACLE_AHEAD" then s.recovering = true
    elseif denied ~= nil then return denied end
    if s.initial ~= nil then
        -- The first route is still being searched: the rig waits braked, at most
        -- Drive.FIRST_ROUTE_LIMIT_MS (live 8570c6316cb9406f-1: 12 minutes to NO_ROUTE, the job
        -- waiting all the while; a route not found by then is not found by searching on).
        s.planSince = s.planSince or s.elapsed
        if s.elapsed - s.planSince > Drive.FIRST_ROUTE_LIMIT_MS then
            say(string.format("[Farmhand] drive no route found in %.0f s; ending NO_ROUTE",
                              (s.elapsed - s.planSince) / 1000))
            s.episode = nil                       -- the search is abandoned
            return "NO_ROUTE"
        end
        Hitch.halt(env, s, dt)
        return initialPlan(env, s, Hitch) or "CONTINUE"
    end
    if s.retreat ~= nil then
        local r = s.retreat
        local along = (x-r.x)*sin(r.yaw)+(z-r.z)*cos(r.yaw)
        local lateral = abs((x-r.x)*cos(r.yaw)-(z-r.z)*sin(r.yaw))
        local arc
        if r.curve ~= nil and s.foot.towed == nil then
            -- A turning reverse without a trailer: measured on its circle, centre (r.x, r.z)
            -- + (cos yaw, -sin yaw) / curve as `advance` sweeps it.
            local c = r.curve
            local cx, cz = r.x + cos(r.yaw)/c, r.z - sin(r.yaw)/c
            local onYaw = (math.atan2 or math.atan)((z-cz)*c, -(x-cx)*c)
            local t = -(((onYaw-r.yaw+pi)%(2*pi)-pi)/c)
            along, lateral = -t, abs(distance(x,z,cx,cz) - 1/abs(c))
            arc = { t = t, yaw = onYaw }
        end
        -- Off its checked straight line it stops there and the route search resumes
        -- from that pose (the guard still checks every unit), as a forward divergence does.
        if r.line == nil then
            r.line = Drive.towedLine(env,s.foot)
            r.onTowed = r.line ~= nil
            r.line = r.line or { x = r.x, z = r.z, yaw = r.yaw }
        end
        local line = r.line
        local yawError = (yaw-(arc and arc.yaw or r.yaw)+pi)%(2*pi)-pi
        local _,_,hitch,cross,orient = Drive.reverseAim(env,s.foot,line,yaw,s.radius)
        local trailer = cross ~= nil and { cross = cross, orient = orient, hitch = hitch,
                                           limit = s.foot.hitchLimit } or nil
        local why
        if not r.diverged then
            if trailer ~= nil then
                why = Drive.reverseOffLine(r, abs((x-line.x)*cos(line.yaw)-(z-line.z)*sin(line.yaw)),
                                           (yaw-line.yaw+pi)%(2*pi)-pi, trailer)
            else
                why = Drive.reverseOffLine(r, lateral, yawError, nil)
            end
        end
        if why then
            say(string.format("[Farmhand] drive reverse diverged at %.1f,%.1f yaw %.2f after %.1f of %.1fm: lateral %.2f yaw error %.2f hitch %s (%s%s)",
                              x,z,yaw,-along,r.length,lateral,yawError,
                              hitch and string.format("%.2f",hitch) or "-", why,
                              trailer and string.format("; trailer lateral %.2f heading %.2f",cross,orient) or ""))
            r.diverged = true
        end
        -- Reversing without moving is held fast too (live 2268b28b08994e3b-2: five
        -- minutes at 0.2 km/h): it stops there and the search goes on from it.
        if not r.diverged then
            if r.markAt == nil or distance(x,z,r.markX,r.markZ) >= 0.5 then
                r.markX,r.markZ,r.markAt = x,z,s.elapsed
            elseif s.elapsed - r.markAt >= 8000 then
                say(string.format("[Farmhand] drive held fast reversing at %.1f,%.1f after %.1f of %.1fm",
                                  x,z,-along,r.length))
                ghostWatch(env, s, Hitch, x, z, yaw, "HELD_FAST")
                r.diverged = true
            end
        end
        if r.diverged or along <= -r.length+0.25 then
            Hitch.halt(env,s,dt)
            if not stopped(s.tractor) then return "CONTINUE" end
            local fresh = Hitch.routeFootprint(env, s.tractor)
            if fresh == nil or #fresh.units ~= #s.foot.units then return "RIG_GEOMETRY_UNKNOWN" end
            for i, unit in ipairs(s.foot.units) do
                if fresh.units[i] ~= unit then return "RIG_GEOMETRY_UNKNOWN" end
            end
            fresh.held, fresh.leaving = s.foot.held, s.foot.leaving
            s.foot = fresh
            if r.contact and type(Hitch.rigContacts) == "function" then
                local observed, contacts = pcall(Hitch.rigContacts, env, s.tractor, s.foot)
                if not observed or type(contacts) ~= "string" then return "UNKNOWN_OBSERVATION" end
                if contacts:find("standing", 1, true) then return "START_NOT_CLEAR" end
                if s.foot.leaving ~= nil then
                    -- Contact is over: plan the turn with the kiosk/tree/building
                    -- counted again, even while the rig is inside its start radius.
                    s.foot.leaving.excluded = s.foot.excluded
                end
            end
            s.foot.departureNudge = nil
            local function clear(px,pz,pyaw,curve,gear)
                return Drive.routeProbe(env,s.foot,Hitch,px,pz,pyaw,curve,gear)
            end
            local start = {x,z,yaw}
            if s.shuffle ~= nil then
                s.retreat = nil
                if shuffleOn(s,start,clear) then return "CONTINUE" end
                s.retreat = r
            end
            local path
            if distance(x,z,s.targetX,s.targetZ) <= 200 then
                path = Drive.plan(start,{s.targetX,s.targetZ},s.radius,
                                  clear,nil,s.goalYaw,true)
            end
            if path == nil then
                s.corridor = corridorFrom(s,start,clear,s.avoided)
                if s.corridor == nil then return "NO_ROUTE" end
                path,s.reached = Drive.section(start,s.radius,clear,s.corridor,1,1)
                -- A failed first span still returns its index; alternates start at 1.
                if path == nil then s.reached = 1 end
                s.sectionAttempt = path and 1 or 2
                -- Backed out of a dead end: turn onto the corridor from here.
                if path == nil and r.backOut then
                    s.retreat = nil
                    if startShuffle(s,start,clear,"backing out") == "CONTINUE" then return "CONTINUE" end
                    s.retreat = r
                end
            end
            s.path,s.next,s.retreat,s.replans = path,2,nil,0
            s.maxSpeedKmh,s.commandShare,s.driveStartMs,s.legKmh = 64,nil,nil,nil
            s.lastMovedX,s.lastMovedZ,s.movedAt = x,z,s.elapsed
            say(string.format("[Farmhand] drive reverse departure %s %.1fm; route search resumed",
                              r.diverged and "stopped off its line after" or "complete", -along))
            return "CONTINUE"
        end
        -- The next metre behind, as the rig sweeps it: along the towed unit's line, the
        -- tractor's place projected onto it (at the tractor's own heading the rigid box
        -- of a rig bent while it straightens touched a tree beside it, live
        -- e44e3f6ba43348a2-2). Blocked, the reverse stops there and the search goes on.
        local probeStep = math.min(1, math.max(0.25, r.length + along))
        local backX,backZ,backYaw = x-sin(yaw)*probeStep,z-cos(yaw)*probeStep,yaw
        if arc ~= nil then
            backX,backZ,backYaw = advance(r.x,r.z,r.yaw,-(arc.t+probeStep),r.curve)
        elseif r.curve ~= nil then
            -- The trailer's line guides steering; it is not the truck's pose.
            -- Rotating the whole rig to that line at once invents a collision.
            backX,backZ,backYaw = advance(x,z,yaw,-probeStep,r.curve)
        end
        local clear, blocked = Hitch.routePoseClear(env,s.foot,backX,backZ,backYaw,r.curve or 0,true)
        if clear == nil then return "UNKNOWN_OBSERVATION" end
        if clear ~= true then
            say(string.format("[Farmhand] drive reverse blocked behind at %.1f,%.1f after %.1f of %.1fm: %s; stopping to search on",
                              x,z,-along,r.length,tostring(blocked)))
            r.diverged = true
            Hitch.halt(env,s,dt)
            return "CONTINUE"
        end
        local v = s.tractor
        if not v:getIsMotorStarted() then
            if not v:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
            v:startMotor()
        end
        Hitch.holdActive(s)
        v:raiseActive()
        measureTerrain(env, s, Hitch, x, z, yaw)
        -- A forward command left from before would aim at the reverse's own cap.
        s.commandKmh = nil
        local lx,lz = Drive.reverseAim(env,s.foot,r.line,yaw,s.radius)
        if arc ~= nil then
            -- 3 m on along the circle, in the tractor's own frame.
            local ax, az = advance(r.x,r.z,r.yaw,-(arc.t+3),r.curve)
            lx = (ax-x)*cos(yaw) - (az-z)*sin(yaw)
            lz = (ax-x)*sin(yaw) + (az-z)*cos(yaw)
        end
        if lx == nil then return "UNKNOWN_OBSERVATION" end
        return Hitch.drive(env,s,dt,false,lx,lz) or "CONTINUE"
    end
    if s.straightenForward ~= nil then
        local f = s.straightenForward
        local along = (x-f.x)*sin(f.yaw)+(z-f.z)*cos(f.yaw)
        local lateral = abs((x-f.x)*cos(f.yaw)-(z-f.z)*sin(f.yaw))
        local inLine = Hitch.routePoseClear(env,s.foot,x,z,yaw) == true
        local ahead = Hitch.routePoseClear(env,f.front,x+sin(yaw)*2,z+cos(yaw)*2,yaw,0)
        if ahead == nil then return "UNKNOWN_OBSERVATION" end
        -- The whole length: stopped as soon as the box cleared, the rig stood with the
        -- tree at its box's edge and no room to reverse (live a44780ff73c84857-2).
        if along >= f.length or ahead ~= true or lateral > 0.75
                or abs((yaw-f.yaw+pi)%(2*pi)-pi) > pi/12 then
            Hitch.halt(env,s,dt)
            if not stopped(s.tractor) then return "CONTINUE" end
            say(string.format("[Farmhand] drive straightened forward %.1fm (%s, %s); route search from here",
                              along, ahead ~= true and "front blocked" or "its length",
                              inLine and "in line" or "the route box still touching"))
            s.straightenForward = nil
            s.initial = { start = {x,z,yaw}, x = x, z = z, yaw = yaw }
            s.maxSpeedKmh,s.commandShare,s.driveStartMs = 64,nil,nil
            return "CONTINUE"
        end
        local v = s.tractor
        if not v:getIsMotorStarted() then
            if not v:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
            v:startMotor()
        end
        Hitch.holdActive(s)
        v:raiseActive()
        s.commandKmh = nil
        return Hitch.drive(env,s,dt,true,0,6) or "CONTINUE"
    end
    if atTarget then return "ARRIVED" end
    -- A bounded departure due is driven first; its end plans on from the clear pose (live
    -- 940689f3466445c2-1: sections planned from where it would end were START_NOT_CLEAR, and
    -- the drive ended NO_ROUTE without moving, the 3650 still against the tree).
    if s.egress == nil and (s.recovering or (s.corridor ~= nil and (s.path == nil
            or Drive.sectionReached(s.path,x,z,yaw)))) then
        Hitch.halt(env,s,dt)
        if not stopped(s.tractor) then return "CONTINUE" end
        local function clear(px,pz,pyaw,curve,gear)
            return Drive.routeProbe(env,s.foot,Hitch,px,pz,pyaw,curve,gear)
        end
        local start = {x,z,yaw}
        -- Bent by a turn (live 125141b03ef949ff-2: a 48 m leg ended in a turn beside a fill
        -- station; every plan from there was START_NOT_CLEAR): into line first.
        if (s.unbends or 0) < 4 and s.foot.towed ~= nil then
            local here, why = Hitch.routePoseClear(env,s.foot,x,z,yaw)
            local straighten = here == false and Drive.planUnbend(env,s.foot,Hitch,s.tractor,x,z,yaw,clear,why) or nil
            if straighten ~= nil then
                s.unbends = (s.unbends or 0) + 1
                s.path,s.shuffle,s.recovering = nil,nil,false
                if straighten.forward then s.straightenForward = straighten else s.retreat = straighten end
                s.maxSpeedKmh,s.commandShare,s.driveStartMs,s.legKmh = 3,0.5,nil,nil
                return "CONTINUE"
            end
        end
        if s.shuffle ~= nil then
            s.recovering = false
            if shuffleOn(s,start,clear) then return "CONTINUE" end
            s.recovering = true
        end
        -- A pose with no forward continuation is recovered by the same
        -- rig-clear straight reverse used at departure, before NO_ROUTE.
        local function escape(after)
            local retreat = (s.retreats or 0) < 2 and Drive.planRetreatRoute(
                start,destination(s),s.radius,clear,s.navHint) or nil
            if retreat == nil then return startShuffle(s,start,clear,after) end
            s.retreat = {x=x,z=z,yaw=yaw,length=retreat}
            s.retreats = (s.retreats or 0)+1
            s.path,s.corridor,s.recovering = nil,nil,false
            s.maxSpeedKmh,s.commandShare,s.driveStartMs = 3,0.5,nil
            say(string.format("[Farmhand] drive rig-clear reverse escape %.1fm after %s", retreat, after))
            return "CONTINUE"
        end
        if s.recovering then
            s.replans = s.replans + 1
            if s.replans > 4 then return "OBSTACLE_AHEAD" end
            local fresh = corridorFrom(s,start,clear)
            s.recovering,s.path = false,nil
            s.lastMovedX,s.lastMovedZ,s.movedAt = x,z,s.elapsed
            -- Without a new corridor the last one still guides a shuffle out.
            if fresh == nil then return escape("no corridor on replan") end
            s.corridor,s.reached,s.sectionAttempt = fresh,1,1
            say(string.format("[Farmhand] drive replan %d retains target %.1f,%.1f",
                                s.replans,s.targetX,s.targetZ))
        end
        if s.reached >= #s.corridor then
            -- The corridor is spent but the rig stopped short: plan again
            -- from the observed pose, within the same replan bound.
            s.recovering = true
            return "CONTINUE"
        end
        local section,nextReached = Drive.section(start,s.radius,clear,s.corridor,
                                                   s.reached,s.sectionAttempt)
        if section == nil then
            s.sectionAttempt = s.sectionAttempt + 1
            if s.sectionAttempt > #SECTION_SPANS then
                s.replans = s.replans + 1
            if s.replans > 4 then return escape("blocked sections") end
                local bad = s.corridor[math.min(#s.corridor,s.reached+2)]
                local avoided = {}
                for _, area in ipairs(s.avoided or {}) do avoided[#avoided+1] = area end
                avoided[#avoided+1] = {bad[1],bad[2],math.max(12,s.radius*1.5)}
                s.avoided = avoided
                local alternate = corridorFrom(s,start,clear,s.avoided)
                if alternate == nil then return escape("no alternate corridor") end
                s.corridor,s.path,s.reached,s.sectionAttempt = alternate,nil,1,1
                say(string.format("[Farmhand] drive found alternate corridor %d after blocked turn",
                                    s.replans))
            end
            return "CONTINUE"
        end
        Drive.activateSection(s,section,nextReached,x,z)
        say(string.format("[Farmhand] drive corridor section to %d/%d from %.1f,%.1f yaw %.2f to %.1f,%.1f yaw %.2f steps %d",
                            nextReached,#s.corridor,x,z,yaw,
                            section[#section][1],section[#section][2],section[#section][3],#section))
        return "CONTINUE"
    end
    if s.egress ~= nil then
        local e = s.egress
        local ox, oz = e.ox, e.oz
        if e.obstacle ~= nil then
            ox, _, oz = env.getWorldTranslation(e.obstacle.rootNode)
            if not (finite(ox) and finite(oz)) or distance(ox, oz, e.ox, e.oz) > 0.25 then
                return "EGRESS_OBSTACLE_MOVED"
            end
        end
        local separation = distance(x, z, ox, oz)
        local dir = e.dir or 1
        local along = ((x - e.x) * sin(e.yaw) + (z - e.z) * cos(e.yaw)) * dir
        local lateral = abs((x - e.x) * cos(e.yaw) - (z - e.z) * sin(e.yaw))
        if separation < e.lastDistance - 0.05 or lateral > 0.3
                or abs((yaw - e.yaw + pi) % (2 * pi) - pi) > pi / 36 then
            return "EGRESS_DIVERGED"
        end
        e.lastDistance = separation
        -- Its 30 s from when it begins (live 3c91e65573db41ff-2: counted from the drive's start,
        -- the route search to a far field used them up and it ended EGRESS_LIMIT unmoved).
        e.startedAt = e.startedAt or s.elapsed
        if along > 8 or s.elapsed - e.startedAt > 30000 then
            -- Its 8 m not enough to come clear (live 0d44db5189854c18-1: the long GMC rig beside the
            -- 3650 and its trailer): from where it stands, leaving that object by the route as a
            -- start with no straight way off does (Drive.begin), never EGRESS_LIMIT.
            Hitch.halt(env, s, dt)
            if not stopped(s.tractor) then return "CONTINUE" end
            s.foot.leaving = { x = x, z = z, radius = Drive.LEAVE_M,
                               excluded = (e.ignoredFoot or s.foot).excluded }
            s.egress, s.path, s.corridor = nil, nil, nil
            s.initial = { start = { x, z, yaw }, x = x, z = z, yaw = yaw }
            s.planSince, s.maxSpeedKmh, s.commandShare = nil, 64, nil
            say(string.format("[Farmhand] drive departure not clear after %.1fm; leaving it by the route from %.1f,%.1f",
                              along, x, z))
            return "CONTINUE"
        end
        local clear = Hitch.routePoseClear(env, s.foot, x, z, yaw) == true
        if clear and type(Hitch.rigContacts) == "function" then
            local observed, contacts = pcall(Hitch.rigContacts, env, s.tractor, s.foot)
            if not observed or type(contacts) ~= "string" then return "UNKNOWN_OBSERVATION" end
            clear = not contacts:find("standing", 1, true)
        end
        if clear then
            Hitch.halt(env, s, dt)
            if not stopped(s.tractor) then return "CONTINUE" end
            local function clear(px, pz, pyaw)
                return Drive.routeProbe(env, s.foot, Hitch, px, pz, pyaw)
            end
            local start = {x,z,yaw}
            local path
            if distance(x,z,s.targetX,s.targetZ) <= 200 then
                path = Drive.plan(start,{s.targetX,s.targetZ},s.radius,
                                  clear,nil,s.goalYaw,true)
            end
            if path == nil then
                s.corridor = corridorFrom(s,start,clear)
                if s.corridor == nil then return "NO_ROUTE" end
                path,s.reached = Drive.section(start,s.radius,clear,s.corridor,1,1)
                if path == nil then s.reached = 1 end
                s.sectionAttempt = path and 1 or 2
            end
            s.path, s.next, s.egress = path, 2, nil
            s.driveStartMs = nil
            s.maxSpeedKmh, s.commandShare = 64, nil
            say(string.format("[Farmhand] drive departure clear after %.2fm", along))
            if path == nil then return "CONTINUE" end
        else
            local v = s.tractor
            if not v:getIsMotorStarted() then
                if not v:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
                v:startMotor()
            end
            Hitch.holdActive(s)
            v:raiseActive()
            if dir < 0 then return Hitch.drive(env, s, dt, false, 0, -10) or "CONTINUE" end
            return Hitch.drive(env, s, dt, true, 0, 3) or "CONTINUE"
        end
    end
    Drive.planAhead(env, s, Hitch)
    if s.elapsed - s.movedAt >= 8000 then
        if distance(x, z, s.lastMovedX, s.lastMovedZ) < 0.5 then
            -- Held fast under throttle by something the route box did not see (live
            -- 7fd526481ca24c1c-3: stopped dead from 6.6 km/h, every replan drove at it
            -- again): just past the rig's front is blocked for the rest of this drive.
            if s.lastSpeedMode == "drive" then
                local held = s.foot.held or {}
                local ahead = s.foot.aMax + 0.75
                for _, side in ipairs({ s.foot.lMin + 0.3, (s.foot.lMin + s.foot.lMax) / 2, s.foot.lMax - 0.3 }) do
                    if #held < 12 then
                        held[#held+1] = { x + sin(yaw)*ahead + cos(yaw)*side, z + cos(yaw)*ahead - sin(yaw)*side }
                    end
                end
                s.foot.held = held
                local ok, contacts = pcall(Hitch.rigContacts, env, s.tractor)
                if type(Hitch.terrainProfile) == "function" then
                    local here = Hitch.terrainProfile(env, s.foot, x, z, yaw, s.tractor.rootNode)
                    local on = Hitch.terrainProfile(env, s.foot, x + sin(yaw)*2, z + cos(yaw)*2, yaw)
                    say(string.format("[Farmhand] drive held-fast terrain here: %s; 2 m on: %s",
                                      here and profileText(here) or "?", on and profileText(on) or "?"))
                end
                say(string.format("[Farmhand] drive held fast at %.1f,%.1f yaw %.2f under throttle; %.1f,%.1f ahead counts as blocked; units %s",
                                  x, z, yaw, held[#held-1][1], held[#held-1][2], ok and tostring(contacts) or "?"))
            end
            ghostWatch(env, s, Hitch, x, z, yaw, "HELD_FAST")
            s.recovering = true
            Hitch.halt(env,s,dt)
            return "CONTINUE"
        end
        s.lastMovedX, s.lastMovedZ, s.movedAt = x, z, s.elapsed
    end
    if s.elapsed > 14400000 then return "TIMEOUT" end
    local pathError = Drive.pathError(s.path,s.next,x,z)
    local maxError = math.max(3,(s.foot.lMax-s.foot.lMin)/2+1)
    if pathError > maxError then
        say(string.format("[Farmhand] drive %.1fm off route at %.1f,%.1f (limit %.1f); braking to replan",
                            pathError,x,z,maxError))
        s.recovering = true
        Hitch.halt(env,s,dt)
        return "CONTINUE"
    end
    local node = s.tractor:getAISteeringNode()
    local _, y = env.getWorldTranslation(node)
    local speed = s.measuredKmh or math.abs(s.tractor:getLastSpeed())
    if not finite(speed) then return "UNKNOWN_OBSERVATION" end
    while s.next < #s.path do
        local current = s.path[s.next]
        local px, _, pz = env.worldToLocal(node, current[1], y, current[2])
        if not (finite(px) and finite(pz)) then return "UNKNOWN_OBSERVATION" end
        if pz >= 0 and distance(x, z, current[1], current[2]) >= math.max(2, speed / 18) then break end
        s.next = s.next + 1
    end
    local point = s.path[s.next]
    if point == nil then return "ROUTE_LOST" end
    local routeSpeed = Drive.travelSpeed(s.path, s.next, x, z, s.radius)
    if routeSpeed == nil then return "ROUTE_LOST" end
    local headingError = abs((yaw - point[3] + pi) % (2 * pi) - pi)
    s.commandKmh = Drive.trackingSpeed(routeSpeed,pathError,maxError,headingError)
    if s.commandKmh == nil then return "ROUTE_LOST" end
    if s.legKmh ~= nil then s.commandKmh = math.min(s.commandKmh, s.legKmh) end
    local lookahead = math.max(6,math.min(12,speed/3.6*0.6))
    local aimX,aimZ = Drive.goalPoint(s.path,s.next,x,z,lookahead)
    if aimX == nil then return "ROUTE_LOST" end
    local lx, _, lz = env.worldToLocal(node, aimX, y, aimZ)
    if not (finite(lx) and finite(lz)) then return "UNKNOWN_OBSERVATION" end
    measureTerrain(env, s, Hitch, x, z, yaw)
    if s.elapsed >= (s.trackAt or 0) then
        s.trackAt = s.elapsed + 1000
        local length2 = lx*lx+lz*lz
        local curve = length2 > 0.00001 and 2*lx/length2 or 0
        local rotOk, wantedRot = pcall(s.tractor.getSteeringRotTimeByCurvature,
                                       s.tractor,curve)
        local revOk, reverser = pcall(s.tractor.getReverserDirection,s.tractor)
        local surface = type(Hitch.routeSurface) == "function" and Hitch.routeSurface(env,x,z) or nil
        say(string.format("[Farmhand] drive track pos %.1f,%.1f yaw %.2f speed %.1f target %.1f/%.1f error %.2f next %d/%d point %.1f,%.1f aim %.1f,%.1f local %.1f,%.1f steer %s/%s mode %s reverse %s surface %s",
                            x,z,yaw,speed,routeSpeed,s.commandKmh,pathError,s.next,#s.path,
                            point[1],point[2],aimX,aimZ,lx,lz,
                            tostring(s.tractor.rotatedTime),tostring(rotOk and wantedRot),
                            tostring(s.lastSpeedMode),tostring(revOk and reverser),tostring(surface or "NON_FIELD")))
    end
    if lz < -1 then
        say(string.format("[Farmhand] drive route diverged at %.1f,%.1f yaw %.2f aim %.1f,%.1f local %.1f,%.1f next %d/%d; braking to replan to %.1f,%.1f",
                            x,z,yaw,aimX,aimZ,lx,lz,s.next,#s.path,s.targetX,s.targetZ))
        s.recovering = true
        Hitch.halt(env,s,dt)
        return "CONTINUE"
    end
    local v = s.tractor
    if not v:getIsMotorStarted() then
        if not v:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
        v:startMotor()
    end
    Hitch.holdActive(s)
    v:raiseActive()
    return Hitch.drive(env, s, dt, true, lx, lz) or "CONTINUE"
end

-- A route search longer than the frame budget continues next frame from its kept
-- state while the rig waits braked; the guard still runs every frame.
function Drive.step(env, s, dt, Hitch)
    if s.result ~= nil then halt(env, s, dt, Hitch); return s.result end
    local ok, code = pcall(function()
        s.elapsed = s.elapsed + dt
        if s.learned ~= nil and s.elapsed > Drive.LEARNED_ROUTE_LIMIT_MS then
            say("[Farmhand] drive learned corridor exhausted its bounded turn search; trying another route")
            return "NO_ROUTE"
        end
        local x, z, yaw = pose(env, s.tractor)
        if x == nil then return "UNKNOWN_OBSERVATION" end
        local saved
        if s.episode ~= nil then
            saved = s.episode.saved
            restore(s, saved)
            x, z, yaw = s.episode.pose[1], s.episode.pose[2], s.episode.pose[3]
        else
            saved = snapshot(s)
        end
        local result = planned(s, gameClock(env), frame, env, s, dt, Hitch, x, z, yaw)
        if result == PENDING then
            s.episode.saved = s.episode.saved or saved
            s.episode.pose = s.episode.pose or { x, z, yaw }
            Hitch.halt(env, s, dt)
            return "CONTINUE"
        end
        return result
    end)
    if not ok then
        say("[Farmhand] drive step error: " .. tostring(code))
        code = "UNKNOWN_OBSERVATION"
    end
    if code ~= "CONTINUE" then
        s.result = code
        Drive.releaseGhost(env, s, Hitch)
        local summary = terrainSummary(s)
        if summary ~= nil then say(summary .. " (" .. tostring(code) .. ")") end
        halt(env, s, dt, Hitch)
    end
    return code
end

function Drive.abort(env, s, Hitch)
    if s ~= nil then
        if s.result == nil then
            local summary = terrainSummary(s)
            if summary ~= nil then say(summary .. " (STOPPED)") end
        end
        s.result = s.result or "STOPPED"
        halt(env, s, 0, Hitch)
        Drive.releaseGhost(env, s, Hitch)
    end
end

-- A drive's end: its rig's collision with buildings back if it stands clear of them; else
-- the bridge's watcher (Hitch.releaseGhosts) restores it once it is.
function Drive.releaseGhost(env, s, Hitch)
    if not s.ghostedHere or type(Hitch.unghost) ~= "function" then return end
    local ok, x, _, z = pcall(env.getWorldTranslation, s.tractor.rootNode)
    local fx, _, fz = env.localDirectionToWorld(s.tractor.rootNode, 0, 0, 1)
    local yaw = (math.atan2 or math.atan)(fx, fz)
    if ok and Hitch.staticNear(env, s.foot, x, z, yaw, Hitch.GHOST.clearM) == false then
        Hitch.unghost(env, s.tractor)
        say(string.format("[Farmhand] drive end clear of buildings: collision with them restored (used %d times)",
                          s.ghostUses or 0))
    else
        say("[Farmhand] drive end beside buildings: collision with them stays off until the rig is clear")
    end
    s.ghostedHere = nil
end

FarmhandDrive = Drive
return Drive
