-- Farmhand-owned, map-independent route search. The caller supplies a clearance
-- probe for the whole observed rig. No marker, provider job or vehicle ID is
-- selected here. Search success is only a candidate route, never a drive verdict.
-- GIANTS' AIVehicleUtil.driveToPoint and collision world will be the executor.
local Drive = {}

local pi, sin, cos, sqrt, floor, abs = math.pi, math.sin, math.cos, math.sqrt, math.floor, math.abs
local STEP, BIN, CELL = 4, pi / 8, 2
local LIMIT = 6000

local function finite(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
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
local function planned(holder, clock, fn, ...)
    local ep = holder.episode or { jobs = {}, printed = 0 }
    ep.n, ep.said, ep.clock = 0, 0, clock
    ep.deadline = clock ~= nil and clock() + Drive.FRAME_BUDGET_S or nil
    episode = ep
    local results = pack(pcall(fn, ...))
    episode = nil
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

local function globalPlan(start, target, probe, margin, step, offsetX, offsetZ, radius, avoided, navHint)
    local sx, sz, gx, gz = start[1], start[2], target[1], target[2]
    return resumable(signature("global", sx, sz, gx, gz, margin, step, offsetX, offsetZ, #(avoided or {})),
                     function()
        local heap = {}
        push(heap, { i=0, j=0, x=sx, z=sz, cost=0, score=distance(sx,sz,gx,gz) })
        return { heap = heap, best = { [gridKey(0,0)] = 0 }, expanded = 0, rejected = {} }
    end, function(job)
    local dirs = DIRS
    local minX, maxX = math.min(sx,gx)-margin, math.max(sx,gx)+margin
    local minZ, maxZ = math.min(sz,gz)-margin, math.max(sz,gz)+margin
    local heap, best, rejected = job.heap, job.best, job.rejected
    while #heap > 0 and job.expanded < 7000 do
        if overBudget() then return false end
        local node = pop(heap)
        if node.cost <= (best[gridKey(node.i,node.j)] or math.huge) then
            job.expanded = job.expanded + 1
            if distance(node.x,node.z,gx,gz) < step * 1.4 then
                local route = { {gx,gz} }
                while node ~= nil do
                    table.insert(route, 1, {node.x,node.z})
                    node = node.parent
                end
                job.result = pack(route)
                return true
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
                    local yaw = math.atan2 and math.atan2(dir[1],dir[2]) or math.atan(dir[1],dir[2])
                    local clear = true
                    for sample = 3, length, 3 do
                        local fraction = sample / length
                        local pass, why = probe(node.x+(x-node.x)*fraction,
                                                node.z+(z-node.z)*fraction, yaw, 0)
                        if pass ~= true then
                            rejected[why or "unreadable"] = (rejected[why or "unreadable"] or 0) + 1
                            clear = false
                            break
                        end
                    end
                    local pass, why = clear and probe(x,z,yaw,0)
                    if clear and pass ~= true then
                        rejected[why or "unreadable"] = (rejected[why or "unreadable"] or 0) + 1
                    end
                    if clear and pass == true then
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
                                        score=cost+distance(x,z,gx,gz),parent=node})
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
    print(string.format("[Farmhand] drive global search cell %.0f offset %.1f,%.1f margin %.0f expanded %d open %d blocked %s:%d",
                        step,offsetX,offsetZ,margin,job.expanded,#heap,tostring(dominant), count))
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
    local direct = distance(start[1],start[2],target[1],target[2])
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
function Drive.plan(start, target, radius, probe, guide, goalYaw, localOnly, maxExpanded)
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
    if probe(sx, sz, syaw) ~= true then return nil, "START_NOT_CLEAR" end
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
    local found, expanded = resumable(signature("plan", sx, sz, syaw, gx, gz, radius, goalYaw, limit,
                                                guide and #guide or 0), function()
        local heap = {}
        push(heap, { x = sx, z = sz, yaw = syaw, cost = 0,
                     score = guide and corridorHeuristic(sx,sz,guide)*1.5 or distance(sx,sz,gx,gz) })
        return { heap = heap, best = { [key(sx, sz, syaw)] = 0 }, expanded = 0 }
    end, function(job)
    local heap, best = job.heap, job.best
    while #heap > 0 and job.expanded < limit do
        if overBudget() then return false end
        local node = pop(heap)
        if node.cost <= (best[key(node.x, node.z, node.yaw)] or math.huge) then
            job.expanded = job.expanded + 1
            local headingError = goalYaw and abs((node.yaw-goalYaw+pi)%(2*pi)-pi) or 0
            -- The final goal is planned inside the 3 m arrival check less the
            -- follower's 1 m endpoint tolerance; corridor waypoints keep 3 m.
            if distance(node.x, node.z, gx, gz) < (goalYaw and 3 or 2) and headingError < pi/12 then
                local reversed = {}
                while node ~= nil do
                    reversed[#reversed + 1] = { node.x, node.z, node.yaw }
                    node = node.parent
                end
                local path = {}
                for i = #reversed, 1, -1 do path[#path + 1] = reversed[i] end
                job.result = pack(path, job.expanded)
                return true
            end
            for _, steer in ipairs({ 0, -1, 1 }) do
                local curve = steer / radius
                local clear = true
                for d = 1, STEP do
                    local x, z, yaw = advance(node.x, node.z, node.yaw, d, curve)
                    if x < minX or x > maxX or z < minZ or z > maxZ or probe(x, z, yaw, curve) ~= true then
                        clear = false
                        break
                    end
                end
                if clear then
                    local x, z, yaw = advance(node.x, node.z, node.yaw, STEP, curve)
                    local cost = node.cost + STEP + (steer ~= 0 and 0.3 or 0)
                    local k = key(x, z, yaw)
                    if cost < (best[k] or math.huge) then
                        best[k] = cost
                        push(heap, { x = x, z = z, yaw = yaw, cost = cost,
                                     score = cost + (guide and corridorHeuristic(x,z,guide)*1.5
                                                   or distance(x,z,gx,gz)), parent = node })
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
            for _, steer in ipairs({0,-1,1}) do
                local px,pz,pyaw = advance(sx,sz,syaw,1,steer/radius)
                local pass, why = probe(px,pz,pyaw,steer/radius)
                say(string.format("[Farmhand] drive departure forward steer=%d clear=%s reason=%s",
                                    steer,tostring(pass),tostring(why)))
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

-- One bounded rig-aware refinement attempt. A long route is refined only as
-- the vehicle reaches each section, so search never monopolizes a game frame
-- for an entire map crossing.
function Drive.section(start, radius, probe, corridor, reached, attempt)
    local gridStep = #corridor > 2 and distance(corridor[2][1],corridor[2][2],
                                                corridor[3][1],corridor[3][2]) or 10
    local stride = gridStep < 8 and 8 or 4
    local span = SECTION_SPANS[attempt]
    if span == nil then return nil,nil,"SECTION_EXHAUSTED" end
    if span ~= 1 then span = span * stride / 4 end
    local i = math.min(reached + span,#corridor)
    local waypoint = corridor[i]
    local guide = {}
    for n = reached,i do guide[#guide+1] = corridor[n] end
    local goalYaw
    if i < #corridor then
        goalYaw = (math.atan2 or math.atan)(waypoint[1]-corridor[i-1][1],
                                            waypoint[2]-corridor[i-1][2])
    end
    local section, why = Drive.plan(start,{waypoint[1],waypoint[2]},radius,probe,
                                    guide,goalYaw,true,4000)
    if section == nil then
        say(string.format("[Farmhand] drive section attempt %d from=%d to=%d radius=%.1f reason=%s",
                            attempt,reached,i,radius,tostring(why)))
    end
    return section,i,section and nil or "SECTION_BLOCKED"
end

function Drive.activateSection(s, section, reached, x, z)
    s.path,s.next,s.reached,s.sectionAttempt = section,2,reached,1
    s.previewAt = s.elapsed + 250
    s.driveStartMs = nil -- each stopped handoff reuses Hitch's steering/throttle ramp
    s.lastMovedX,s.lastMovedZ,s.movedAt = x,z,s.elapsed
end

-- A parked obstacle can overlap the conservative box at the current pose even
-- when the vehicle can pull straight away from it. This bounded departure is
-- considered only for a stationary object behind the driver; the collision
-- engine stays active. Every metre ahead must be clear of all other objects.
function Drive.planEgress(start, obstacle, ordinary, ignoring)
    local sx, sz, yaw = start[1], start[2], start[3]
    if not (finite(sx) and finite(sz) and finite(yaw) and finite(obstacle[1])
            and finite(obstacle[2])) then return nil, "EGRESS_UNREADABLE" end
    local fx, fz = sin(yaw), cos(yaw)
    if (obstacle[1] - sx) * fx + (obstacle[2] - sz) * fz >= -0.5 then
        return nil, "EGRESS_NOT_BEHIND"
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
    for d = 1, 12 do
        local x,z = sx-sin(yaw)*d,sz-cos(yaw)*d
        if probe(x,z,yaw,0) ~= true then return nil end
        if d >= 4 and d % 2 == 0 and forwardOpen(x,z) then return d end
    end
    return nil
end

-- A narrow forward pocket may have a few clear motion primitives but no
-- usable continuation. Before backing out, prove that a candidate backed
-- pose has both a clear swept retreat and a rig-aware forward first section.
function Drive.planRetreatRoute(start, target, radius, probe, navHint)
    local sx,sz,yaw = start[1],start[2],start[3]
    local maxBack = math.min(64,math.max(16,radius*4))
    for d = 1, maxBack do
        local x,z = sx-sin(yaw)*d,sz-cos(yaw)*d
        if probe(x,z,yaw,0) ~= true then return nil end
        if d % 4 == 0 then
            local backed = {x,z,yaw}
            local path
            if distance(x,z,target[1],target[2]) <= 200 then
                path = Drive.plan(backed,target,radius,probe,nil,nil,true)
            end
            if path == nil then
                local corridor = guidedRoute(backed,target,probe,radius,nil,navHint)
                if corridor ~= nil then
                    path = Drive.section(backed,radius,probe,corridor,1,1)
                end
            end
            if path ~= nil then return d end
        end
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

-- The planner probes each four-metre arc at its intermediate yaw. A drive
-- preview must trace that same arc, especially for a long trailer on a turn.
function Drive.routeSample(path, index, metres)
    local from, to = path[index - 1], path[index]
    if from == nil or to == nil or not finite(metres) or metres < 0 or metres > STEP then return nil end
    local change = (to[3] - from[3] + pi) % (2 * pi) - pi
    local curvature = change / STEP
    local x, z, yaw = advance(from[1], from[2], from[3], metres, curvature)
    return x, z, yaw, curvature
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

local atan2 = math.atan2 or math.atan

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

-- The first route of a drive, as a replayable planning step (Drive.begin, and
-- Drive.step while it continues over frames). nil, or the failure code.
local function initialPlan(env, s, Hitch)
    local function clear(px, pz, pyaw, curve)
        return Hitch.routePoseClear(env, s.foot, px, pz, pyaw, curve)
    end
    local start, target = s.initial.start, { s.targetX, s.targetZ }
    local path, why, corridor, reached, sectionAttempt, retreat
    if distance(start[1],start[2],target[1],target[2]) <= 200 then
        path, why = Drive.plan(start,target,s.radius,clear,nil,nil,true)
    end
    if path == nil and s.egress == nil then
        retreat = Drive.planRetreat(start,s.radius,clear)
        if retreat ~= nil then
            say(string.format("[Farmhand] drive collision-checked reverse departure %.1fm", retreat))
        end
    end
    if path == nil and retreat == nil then
        corridor = guidedRoute(start,target,clear,s.radius,nil,s.navHint)
        if corridor == nil then return "NO_ROUTE" end
        path,reached = Drive.section(start,s.radius,clear,corridor,1,1)
        if path == nil then
            if s.egress == nil then
                retreat = Drive.planRetreatRoute(start,target,s.radius,clear,s.navHint)
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
        s.retreat = { x = s.initial.x, z = s.initial.z, yaw = s.initial.yaw, length = retreat }
        s.maxSpeedKmh, s.commandShare = 3, 0.5
    end
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
        local function clear(px, pz, pyaw, curve)
            return Hitch.routePoseClear(env, foot, px, pz, pyaw, curve)
        end
        local startClear, startReason, hits = Hitch.routePoseClear(env, foot, x, z, yaw)
        local egress
        if startClear == false and hits ~= nil and #hits > 0 then
            local obstacle
            for _, node in ipairs(hits) do
                local object = env.g_currentMission:getNodeObject(node)
                object = object ~= nil and (object.rootVehicle or object) or nil
                if object == nil or (obstacle ~= nil and obstacle ~= object) then
                    return nil, "START_NOT_CLEAR"
                end
                obstacle = object
            end
            if obstacle == nil or obstacle.rootNode == nil or obstacle.ownerFarmId ~= args.farmId
                    or not stopped(obstacle) then return nil, "START_NOT_CLEAR" end
            local ox, _, oz = env.getWorldTranslation(obstacle.rootNode)
            local ignoredFoot, excluded = {}, {}
            for key, value in pairs(foot) do ignoredFoot[key] = value end
            for key, value in pairs(foot.excluded) do excluded[key] = value end
            excluded[obstacle] = true
            ignoredFoot.excluded = excluded
            local function ignoring(px, pz, pyaw)
                return Hitch.routePoseClear(env, ignoredFoot, px, pz, pyaw)
            end
            local length, why = Drive.planEgress({ x, z, yaw }, { ox, oz }, clear, ignoring)
            if length == nil then return nil, why end
            egress = { obstacle = obstacle, ox = ox, oz = oz, initialDistance = distance(x, z, ox, oz),
                       lastDistance = distance(x, z, ox, oz), x = x, z = z, yaw = yaw,
                       ignoredFoot = ignoredFoot, plannedM = length }
            say(string.format("[Farmhand] drive bounded departure %.1fm from %s", length,
                                tostring(obstacle.getName and obstacle:getName() or obstacle.typeName)))
        elseif startClear ~= true then
            say("[Farmhand] drive start clearance: " .. tostring(startReason))
            return nil, "START_NOT_CLEAR"
        end
        local navHint = Drive.navHint(env)
        local planStart
        if egress == nil then
            planStart = {x,z,yaw}
        else
            local px = x + sin(yaw) * egress.plannedM
            local pz = z + cos(yaw) * egress.plannedM
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
                    targetX = args.x, targetZ = args.z,
                    path = nil, next = 2, foot = foot, egress = egress, radius = radius,
                    sectionAttempt = 1, navHint = navHint,
                    result = nil, movedAt = 0,
                    lastMovedX = x, lastMovedZ = z, replans = 0,
                    initial = { start = planStart, x = x, z = z, yaw = yaw } }
        local saved = snapshot(s)
        local code = planned(s, gameClock(env), initialPlan, env, s, Hitch)
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

local function guard(env, s, Hitch, x, z, yaw, atTarget)
    if s.isReserved() ~= true or s.tractor.ownerFarmId ~= s.farmId then return "RESERVATION_LOST" end
    local conflict = Hitch.driverConflict(s.tractor)
    if conflict ~= nil then return conflict end
    if s.tractor:getIsAIActive() ~= false then return "AI_ACTIVE" end
    if not sameRig(env, s, Hitch) then return "RIG_CHANGED" end
    local seat = s.tractor:getIsEntered()
    if seat ~= true and seat ~= false then return "UNKNOWN_OBSERVATION" end
    local playerX, playerZ
    if seat == false then
        local player = env.g_localPlayer
        local px, _, pz
        if type(player.getPosition) == "function" then px, _, pz = player:getPosition()
        else px, _, pz = env.getWorldTranslation(player.rootNode) end
        if not (finite(px) and finite(pz)) then return "UNKNOWN_OBSERVATION" end
        if distance(px, pz, x, z) < 6 then return "PERSON_TOO_CLOSE" end
        playerX, playerZ = px, pz
    end
    local probeFoot = s.egress ~= nil and s.egress.ignoredFoot or s.foot
    local here, hereReason = Hitch.routePoseClear(env, probeFoot, x, z, yaw)
    if here ~= true then
        -- The route box is one rigid box at the root's heading; in a bend an
        -- articulated rig's units are not there. Each unit's own observed box
        -- (Hitch.rigContacts, as LOAD's envelope watch) decides actual contact.
        local contacts = "?"
        if here == false and s.egress == nil and type(Hitch.rigContacts) == "function" then
            local ok, observed = pcall(Hitch.rigContacts, env, s.tractor)
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
        local aheadX, aheadZ = x + sin(yaw) * 2, z + cos(yaw) * 2
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
            for metres = 1, STEP do
                local px, pz, pyaw, turn = Drive.routeSample(s.path, i, metres)
                if px == nil then return "UNKNOWN_OBSERVATION" end
                if playerX ~= nil and distance(px, pz, playerX, playerZ) < 6 then
                    return "PERSON_TOO_CLOSE"
                end
                local clear, why = Hitch.routePoseClear(env, s.foot, px, pz, pyaw, turn)
                if clear ~= true then
                    say(string.format("[Farmhand] drive preview blocked at %.2f,%.2f yaw %.2f: %s",
                                        px, pz, pyaw, tostring(why)))
                    return clear == nil and "UNKNOWN_OBSERVATION" or "OBSTACLE_AHEAD"
                end
            end
            traveled = traveled + STEP
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

-- One frame of Farmhand's own route follower, under the existing measured-speed
-- governor, takeover watch, brake and vehicle-control restoration. Any end latches.
local function frame(env, s, dt, Hitch, x, z, yaw)
    local atTarget = distance(x, z, s.targetX, s.targetZ) < 3
    local denied = guard(env, s, Hitch, x, z, yaw, atTarget)
    if denied == "OBSTACLE_AHEAD" then s.recovering = true
    elseif denied ~= nil then return denied end
    if s.initial ~= nil then
        -- The first route is still being searched: the rig waits braked.
        Hitch.halt(env, s, dt)
        return initialPlan(env, s, Hitch) or "CONTINUE"
    end
    if s.retreat ~= nil then
        local r = s.retreat
        local along = (x-r.x)*sin(r.yaw)+(z-r.z)*cos(r.yaw)
        local lateral = abs((x-r.x)*cos(r.yaw)-(z-r.z)*sin(r.yaw))
        if lateral > 0.75 or abs((yaw-r.yaw+pi)%(2*pi)-pi) > pi/12 then
            return "RETREAT_DIVERGED"
        end
        if along <= -r.length+0.25 then
            Hitch.halt(env,s,dt)
            if not stopped(s.tractor) then return "CONTINUE" end
            local function clear(px,pz,pyaw,curve)
                return Hitch.routePoseClear(env,s.foot,px,pz,pyaw,curve)
            end
            local start = {x,z,yaw}
            local path
            if distance(x,z,s.targetX,s.targetZ) <= 200 then
                path = Drive.plan(start,{s.targetX,s.targetZ},s.radius,
                                  clear,nil,nil,true)
            end
            if path == nil then
                s.corridor = guidedRoute(start,{s.targetX,s.targetZ},clear,s.radius,nil,s.navHint)
                if s.corridor == nil then return "NO_ROUTE" end
                path,s.reached = Drive.section(start,s.radius,clear,s.corridor,1,1)
                -- A failed first span still returns its index; alternates start at 1.
                if path == nil then s.reached = 1 end
                s.sectionAttempt = path and 1 or 2
            end
            s.path,s.next,s.retreat,s.replans = path,2,nil,0
            s.maxSpeedKmh,s.commandShare,s.driveStartMs = 64,nil,nil
            s.lastMovedX,s.lastMovedZ,s.movedAt = x,z,s.elapsed
            say(string.format("[Farmhand] drive reverse departure complete %.1fm; route search resumed", -along))
            return "CONTINUE"
        end
        local backX,backZ = x-sin(yaw),z-cos(yaw)
        local clear = Hitch.routePoseClear(env,s.foot,backX,backZ,yaw,0)
        if clear ~= true then return clear == nil and "UNKNOWN_OBSERVATION" or "OBSTACLE_AHEAD" end
        local v = s.tractor
        if not v:getIsMotorStarted() then
            if not v:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
            v:startMotor()
        end
        Hitch.holdActive(s)
        v:raiseActive()
        return Hitch.drive(env,s,dt,false,0,-10) or "CONTINUE"
    end
    if atTarget then return "ARRIVED" end
    if s.recovering or (s.corridor ~= nil and (s.path == nil
            or Drive.sectionReached(s.path,x,z,yaw))) then
        Hitch.halt(env,s,dt)
        if not stopped(s.tractor) then return "CONTINUE" end
        local function clear(px,pz,pyaw,curve)
            return Hitch.routePoseClear(env,s.foot,px,pz,pyaw,curve)
        end
        local start = {x,z,yaw}
        -- A pose with no forward continuation is recovered by the same
        -- rig-clear straight reverse used at departure, before NO_ROUTE.
        local function escape(after)
            local retreat = (s.retreats or 0) < 2 and Drive.planRetreatRoute(
                start,{s.targetX,s.targetZ},s.radius,clear,s.navHint) or nil
            if retreat == nil then return "NO_ROUTE" end
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
            s.corridor = guidedRoute(start,{s.targetX,s.targetZ},clear,s.radius,nil,s.navHint)
            s.reached,s.sectionAttempt,s.recovering = 1,1,false
            s.path = nil
            s.lastMovedX,s.lastMovedZ,s.movedAt = x,z,s.elapsed
            if s.corridor == nil then return escape("no corridor on replan") end
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
                s.corridor = guidedRoute(start,{s.targetX,s.targetZ},clear,
                                         s.radius,s.avoided,s.navHint)
                if s.corridor == nil then return escape("no alternate corridor") end
                s.path,s.reached,s.sectionAttempt = nil,1,1
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
        local ox, _, oz = env.getWorldTranslation(e.obstacle.rootNode)
        if not (finite(ox) and finite(oz)) or distance(ox, oz, e.ox, e.oz) > 0.25 then
            return "EGRESS_OBSTACLE_MOVED"
        end
        local separation = distance(x, z, ox, oz)
        local along = (x - e.x) * sin(e.yaw) + (z - e.z) * cos(e.yaw)
        local lateral = abs((x - e.x) * cos(e.yaw) - (z - e.z) * sin(e.yaw))
        if separation < e.lastDistance - 0.05 or lateral > 0.3
                or abs((yaw - e.yaw + pi) % (2 * pi) - pi) > pi / 36 then
            return "EGRESS_DIVERGED"
        end
        e.lastDistance = separation
        if along > 8 or s.elapsed > 30000 then return "EGRESS_LIMIT" end
        if Hitch.routePoseClear(env, s.foot, x, z, yaw) == true then
            local function clear(px, pz, pyaw)
                return Hitch.routePoseClear(env, s.foot, px, pz, pyaw)
            end
            local start = {x,z,yaw}
            local path
            if distance(x,z,s.targetX,s.targetZ) <= 200 then
                path = Drive.plan(start,{s.targetX,s.targetZ},s.radius,
                                  clear,nil,nil,true)
            end
            if path == nil then
                s.corridor = guidedRoute(start,{s.targetX,s.targetZ},clear,s.radius,nil,s.navHint)
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
            return Hitch.drive(env, s, dt, true, 0, 3) or "CONTINUE"
        end
    end
    if s.elapsed - s.movedAt >= 8000 then
        if distance(x, z, s.lastMovedX, s.lastMovedZ) < 0.5 then
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
    local lookahead = math.max(6,math.min(12,speed/3.6*0.6))
    local aimX,aimZ = Drive.goalPoint(s.path,s.next,x,z,lookahead)
    if aimX == nil then return "ROUTE_LOST" end
    local lx, _, lz = env.worldToLocal(node, aimX, y, aimZ)
    if not (finite(lx) and finite(lz)) then return "UNKNOWN_OBSERVATION" end
    if s.elapsed >= (s.trackAt or 0) then
        s.trackAt = s.elapsed + 1000
        local length2 = lx*lx+lz*lz
        local curve = length2 > 0.00001 and 2*lx/length2 or 0
        local rotOk, wantedRot = pcall(s.tractor.getSteeringRotTimeByCurvature,
                                       s.tractor,curve)
        local revOk, reverser = pcall(s.tractor.getReverserDirection,s.tractor)
        say(string.format("[Farmhand] drive track pos %.1f,%.1f yaw %.2f speed %.1f target %.1f/%.1f error %.2f next %d/%d point %.1f,%.1f aim %.1f,%.1f local %.1f,%.1f steer %s/%s mode %s reverse %s",
                            x,z,yaw,speed,routeSpeed,s.commandKmh,pathError,s.next,#s.path,
                            point[1],point[2],aimX,aimZ,lx,lz,
                            tostring(s.tractor.rotatedTime),tostring(rotOk and wantedRot),
                            tostring(s.lastSpeedMode),tostring(revOk and reverser)))
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
        halt(env, s, dt, Hitch)
    end
    return code
end

function Drive.abort(env, s, Hitch)
    if s ~= nil then
        s.result = s.result or "STOPPED"
        halt(env, s, 0, Hitch)
    end
end

FarmhandDrive = Drive
return Drive
