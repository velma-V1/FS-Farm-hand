-- One short rotation of an FS25 Cylindered moving tool. The caller chooses a
-- discovered tool and must call step until terminal, or stop on cancellation.
local Control = {}
local tau, pi = 2 * math.pi, math.pi

local function circularDelta(from, to)
    return (to - from + pi) % tau - pi
end

local function angle(tool)
    return select(tool.rotationAxis, getRotation(tool.node))
end

local function stateForAngle(tool, value)
    if tool.rotMin ~= nil and tool.rotMax ~= nil then
        return (value - tool.rotMin) / (tool.rotMax - tool.rotMin)
    end
    return value % tau
end

function Control.stop(session)
    if session == nil or session.stopped then return end
    session.stopped = true
    session.tool.curTargetPosition = nil
    session.tool.curTargetDirection = nil
    if session.startedMotor then session.vehicle:stopMotor() end
end

-- One pulse is limited to 0.02 rad, the increment tested live on three
-- different machine families. Larger moves require observed repeated pulses.
function Control.begin(vehicle, tool, targetAngle)
    if vehicle == nil or tool == nil or type(targetAngle) ~= "number"
            or targetAngle ~= targetAngle or vehicle.isServer ~= true
            or type(vehicle.getIsMovingToolActive) ~= "function"
            or not vehicle:getIsMovingToolActive(tool)
            or type(vehicle.getIsAIActive) ~= "function" or vehicle:getIsAIActive() ~= false
            or type(vehicle.getLastSpeed) ~= "function"
            or math.abs(vehicle:getLastSpeed()) >= 0.5
            or type(vehicle.getIsMotorStarted) ~= "function"
            or type(vehicle.startMotor) ~= "function"
            or type(vehicle.stopMotor) ~= "function"
            or type(vehicle.raiseActive) ~= "function"
            or tool.rotSpeed == nil or tool.rotationAxis == nil
            or tool.curTargetPosition ~= nil then
        return nil, "CONTROL_UNAVAILABLE"
    end
    local start = angle(tool)
    local unlimited = tool.rotMin == nil and tool.rotMax == nil
    if (tool.rotMin == nil) ~= (tool.rotMax == nil) then
        return nil, "ROTATION_LIMIT_UNREADABLE"
    end
    local delta = unlimited and circularDelta(start, targetAngle) or targetAngle - start
    if math.abs(delta) < 0.001 or math.abs(delta) > 0.020001
            or (tool.rotMin ~= nil and targetAngle < tool.rotMin)
            or (tool.rotMax ~= nil and targetAngle > tool.rotMax) then
        return nil, "TARGET_OUT_OF_BOUNDS"
    end
    local startedMotor = not vehicle:getIsMotorStarted()
    if startedMotor then vehicle:startMotor() end
    return { vehicle = vehicle, tool = tool, start = start, target = targetAngle,
             delta = delta, unlimited = unlimited,
             targetState = stateForAngle(tool, targetAngle), startedMotor = startedMotor,
             elapsed = 0, motionElapsed = 0, commanded = false }
end

function Control.step(session, dt)
    if session == nil or session.stopped then return "FAILED", "STOPPED" end
    local vehicle, tool = session.vehicle, session.tool
    session.elapsed = session.elapsed + dt
    vehicle:raiseActive()
    if not vehicle:getIsMotorStarted() then
        if session.elapsed < 5000 then return "RUNNING" end
        Control.stop(session)
        return "FAILED", "POWER_TIMEOUT"
    end
    if session.settling then
        session.settleElapsed = session.settleElapsed + dt
        local actual = angle(tool)
        local change = session.unlimited
            and math.abs(circularDelta(session.lastAngle, actual))
            or math.abs(actual - session.lastAngle)
        session.lastAngle = actual
        session.quietMs = change < 0.0002 and session.quietMs + dt or 0
        if session.quietMs < 300 and session.settleElapsed < 4000 then
            return "RUNNING"
        end
        Control.stop(session)
        if session.quietMs < 300 then
            return "FAILED", { reason = "SETTLE_TIMEOUT", actual = actual }
        end
        local error = session.unlimited and math.abs(circularDelta(actual, session.target))
            or math.abs(actual - session.target)
        if error <= 0.002 then return "DONE", { actual = actual, error = error } end
        return "FAILED", { reason = "TARGET_MISSED", actual = actual, error = error }
    end
    if not session.commanded then
        tool.curTargetPosition = session.targetState
        tool.curTargetDirection = session.delta > 0 and 1 or -1
        session.commanded = true
    end
    session.motionElapsed = session.motionElapsed + dt
    local actual = angle(tool)
    local moved = session.unlimited and circularDelta(session.start, actual)
        or actual - session.start
    local remaining = session.delta - moved
    local speed, acceleration = tool.lastRotSpeed, tool.rotAcceleration
    local braking = type(speed) == "number" and type(acceleration) == "number"
        and acceleration > 0 and speed * session.delta > 0
        and speed * speed / (2 * acceleration) + math.abs(speed) * dt * 0.5 or nil
    if session.motionElapsed < 4000 and math.abs(remaining) > 0.001
            and remaining * session.delta > 0
            and (braking == nil or math.abs(remaining) > braking) then
        return "RUNNING"
    end
    tool.curTargetPosition = nil
    tool.curTargetDirection = nil
    session.settling = true
    session.settleElapsed = 0
    session.quietMs = 0
    session.lastAngle = actual
    return "RUNNING"
end

return Control
