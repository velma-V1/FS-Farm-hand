-- Optional TerraFarm adapter for Farmhand-owned low-speed ground work.
-- TerraFarm's own Machine specialization performs the terrain operation.
local Adapter = {}

-- The caller owns and verifies the root vehicle's physical motion separately.
function Adapter.arm(machine)
    if machine == nil or machine.isServer ~= true or machine.spec_machine == nil
            or machine.spec_machine.active
            or type(machine.getCanActivateMachine) ~= "function"
            or not machine:getCanActivateMachine()
            or type(machine.setMachineActive) ~= "function"
            or type(machine.setMachineState) ~= "function"
            or machine.spec_machine.state == nil
            or type(machine.spec_machine.state.clone) ~= "function" then
        return nil, "TERRAFARM_UNAVAILABLE"
    end
    local session = { machine = machine, originalState = machine.spec_machine.state }
    machine:setMachineActive(true)
    return session
end

-- TerraFarm MachineState.lua: DrivingDirectionMode.IGNORE = 4. This native
-- setting lets a tool work below GIANTS Drivable's 1.5 km/h direction threshold.
-- Apply it only after the caller has measured actual forward movement.
function Adapter.motionObserved(session, forwardDistance, forwardSpeed)
    if session == nil or session.stopped or not session.machine.spec_machine.active
            or type(forwardDistance) ~= "number" or forwardDistance <= 0
            or type(forwardSpeed) ~= "number" or forwardSpeed < 0.2 then
        return false, "NO_OBSERVED_FORWARD_MOTION"
    end
    if not session.overridden then
        local state = session.originalState:clone()
        state.drivingDirectionMode = 4
        session.overridden = true
        session.machine:setMachineState(state)
    end
    return session.machine:getIsAvailable()
end

-- Call on success, failure, STOP, timeout, takeover, or map unload.
function Adapter.stop(session)
    if session == nil or session.stopped then return true end
    session.machine:setMachineActive(false)
    if session.overridden then session.machine:setMachineState(session.originalState) end
    session.stopped = true
    return not session.machine.spec_machine.active
        and session.machine.spec_machine.state.drivingDirectionMode
            == session.originalState.drivingDirectionMode
end

return Adapter
