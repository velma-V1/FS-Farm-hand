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

-- start={x,z,yaw}, target={x,z}; radius is GIANTS' observed minimum turning
-- radius or larger. The probe returns true only when the rig's entire swept box
-- and terrain are readable and clear at x,z,yaw. Each arc is sampled every metre.
function Drive.plan(start, target, radius, probe)
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
    local first = { x = sx, z = sz, yaw = syaw, cost = 0, score = distance(sx, sz, gx, gz) }
    local heap, best = {}, { [key(sx, sz, syaw)] = 0 }
    push(heap, first)
    local expanded = 0
    while #heap > 0 and expanded < LIMIT do
        local node = pop(heap)
        if node.cost <= (best[key(node.x, node.z, node.yaw)] or math.huge) then
            expanded = expanded + 1
            if distance(node.x, node.z, gx, gz) < 3 then
                local reversed = {}
                while node ~= nil do
                    reversed[#reversed + 1] = { node.x, node.z, node.yaw }
                    node = node.parent
                end
                local path = {}
                for i = #reversed, 1, -1 do path[#path + 1] = reversed[i] end
                return path, nil
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
                                     score = cost + distance(x, z, gx, gz), parent = node })
                    end
                end
            end
        end
    end
    return nil, "NO_ROUTE"
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

function Drive.lookaheadIndex(path, nextIndex, x, z, speedKmh)
    local lookahead = math.max(5, math.min(22, 5 + speedKmh / 3.6 * 0.8))
    local aim = nextIndex
    local ahead = distance(x, z, path[aim][1], path[aim][2])
    local bends = 0
    while aim < #path and ahead < lookahead do
        local following = aim + 1
        local change = abs((path[following][3] - path[aim][3] + pi) % (2 * pi) - pi)
        if change > 0.08 then
            if bends >= 1 then break end
            bends = bends + 1
        end
        ahead = ahead + distance(path[aim][1], path[aim][2],
                                 path[following][1], path[following][2])
        aim = following
    end
    return aim
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
            print(string.format("[Farmhand] drive bounded departure %.1fm from %s", length,
                                tostring(obstacle.getName and obstacle:getName() or obstacle.typeName)))
        elseif startClear ~= true then
            print("[Farmhand] drive start clearance: " .. tostring(startReason))
            return nil, "START_NOT_CLEAR"
        end
        local path, why
        if egress == nil then
            path, why = Drive.plan({ x, z, yaw }, { args.x, args.z }, radius, clear)
        else
            local px = x + sin(yaw) * egress.plannedM
            local pz = z + cos(yaw) * egress.plannedM
            path, why = Drive.plan({ px, pz, yaw }, { args.x, args.z }, radius, clear)
        end
        if path == nil then return nil, why end
        local cruise = v.spec_drivable and v.spec_drivable.cruiseControl
        local savedCruise = cruise and cruise.speed
        local vehicleMax = v:getCruiseControlMaxSpeed()
        if not (finite(savedCruise) and finite(vehicleMax) and vehicleMax >= 1) then
            return nil, "CRUISE_UNREADABLE"
        end
        local travelLimit = math.min(64, vehicleMax)
        Hitch.watchDriver(env, v)
        local s = { tractor = v, farmId = args.farmId, isReserved = args.isReserved,
                    maxSpeedKmh = egress ~= nil and 3 or 64,
                    commandShare = egress ~= nil and 0.5 or nil, elapsed = 0,
                    cruiseBefore = savedCruise, travelLimit = travelLimit,
                    targetX = args.x, targetZ = args.z,
                    path = path, next = 2, foot = foot, egress = egress, radius = radius,
                    result = nil, movedAt = 0,
                    lastMovedX = x, lastMovedZ = z, replans = 0 }
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
    local here = Hitch.routePoseClear(env, probeFoot, x, z, yaw)
    if here ~= true then return here == nil and "UNKNOWN_OBSERVATION" or "ENVELOPE_CONTACT" end
    if atTarget then return nil end
    -- A reaction corridor in front of the rig, in addition to the planned path.
    local aheadX, aheadZ = x + sin(yaw) * 2, z + cos(yaw) * 2
    local ahead, aheadReason = Hitch.routePoseClear(env, probeFoot, aheadX, aheadZ, yaw)
    if ahead ~= true then
        print(string.format("[Farmhand] drive front blocked at %.2f,%.2f yaw %.2f: %s",
                            aheadX, aheadZ, yaw, tostring(aheadReason)))
        return ahead == nil and "UNKNOWN_OBSERVATION" or "OBSTACLE_AHEAD"
    end
    if s.egress == nil and s.path ~= nil and s.elapsed >= (s.previewAt or 0) then
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
                    print(string.format("[Farmhand] drive preview blocked at %.2f,%.2f yaw %.2f: %s",
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
function Drive.step(env, s, dt, Hitch)
    if s.result ~= nil then halt(env, s, dt, Hitch); return s.result end
    local ok, code = pcall(function()
        s.elapsed = s.elapsed + dt
        local x, z, yaw = pose(env, s.tractor)
        if x == nil then return "UNKNOWN_OBSERVATION" end
        local atTarget = distance(x, z, s.targetX, s.targetZ) < 3
        local denied = guard(env, s, Hitch, x, z, yaw, atTarget)
        if denied ~= nil then return denied end
        if atTarget then return "ARRIVED" end
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
                local path = Drive.plan({ x, z, yaw }, { s.targetX, s.targetZ }, s.radius, clear)
                if path == nil then return "NO_ROUTE" end
                s.path, s.next, s.egress = path, 2, nil
                s.maxSpeedKmh, s.commandShare = 64, nil
                print(string.format("[Farmhand] drive departure clear after %.2fm", along))
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
            if distance(x, z, s.lastMovedX, s.lastMovedZ) < 0.5 then return "STALLED" end
            s.lastMovedX, s.lastMovedZ, s.movedAt = x, z, s.elapsed
        end
        if s.elapsed > 14400000 then return "TIMEOUT" end
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
        s.commandKmh = Drive.travelSpeed(s.path, s.next, x, z, s.radius)
        if s.commandKmh == nil then return "ROUTE_LOST" end
        local aim = Drive.lookaheadIndex(s.path, s.next, x, z, speed)
        point = s.path[aim]
        local lx, _, lz = env.worldToLocal(node, point[1], y, point[2])
        if not (finite(lx) and finite(lz)) then return "UNKNOWN_OBSERVATION" end
        if lz < -1 then return "ROUTE_DIVERGED" end
        local v = s.tractor
        if not v:getIsMotorStarted() then
            if not v:getCanMotorRun() then return "MOTOR_UNAVAILABLE" end
            v:startMotor()
        end
        Hitch.holdActive(s)
        v:raiseActive()
        return Hitch.drive(env, s, dt, true, lx, lz) or "CONTINUE"
    end)
    if not ok then code = "UNKNOWN_OBSERVATION" end
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
