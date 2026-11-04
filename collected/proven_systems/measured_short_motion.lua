-- Candidate root poses for a bounded commanded arc. The steering reference is
-- measured from the loaded vehicle; callers propagate attached units and
-- query live clearance before issuing any control input.
local Motion = {}

local function finite(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

function Motion.sampleRootArc(env, vehicle, length, curvature)
    if vehicle == nil or vehicle.rootNode == nil or vehicle.spec_wheels == nil
            or vehicle.spec_wheels.steeringCenterNode == nil
            or not finite(length) or not finite(curvature)
            or math.abs(length) < 0.05 or math.abs(length) > 3
            or math.abs(curvature) > 0.3 then
        return nil, "REFERENCE_OR_COMMAND_UNREADABLE"
    end
    local root, center = vehicle.rootNode, vehicle.spec_wheels.steeringCenterNode
    local x, _, z = env.getWorldTranslation(root)
    local cx, cy, cz = env.getWorldTranslation(center)
    local side, _, ahead = env.worldToLocal(root, cx, cy, cz)
    local fx, _, fz = env.localDirectionToWorld(root, 0, 0, 1)
    if not (finite(x) and finite(z) and finite(side) and finite(ahead)
            and finite(fx) and finite(fz)) then
        return nil, "REFERENCE_OR_COMMAND_UNREADABLE"
    end
    local yaw = math.atan2 and math.atan2(fx, fz) or math.atan(fx, fz)
    local rx = x + math.cos(yaw) * side + math.sin(yaw) * ahead
    local rz = z - math.sin(yaw) * side + math.cos(yaw) * ahead
    local count = math.ceil(math.abs(length) / 0.25)
    local poses = {}
    for i = 1, count do
        local distance = length * i / count
        local nextYaw = yaw + distance * curvature
        local nx, nz
        if math.abs(curvature) < 0.00001 then
            nx, nz = rx + math.sin(yaw) * distance,
                     rz + math.cos(yaw) * distance
        else
            nx = rx + (math.cos(yaw) - math.cos(nextYaw)) / curvature
            nz = rz + (math.sin(nextYaw) - math.sin(yaw)) / curvature
        end
        poses[i] = {
            distance = distance,
            x = nx - math.cos(nextYaw) * side - math.sin(nextYaw) * ahead,
            z = nz + math.sin(nextYaw) * side - math.cos(nextYaw) * ahead,
            yaw = nextYaw,
        }
    end
    return poses
end

return Motion
