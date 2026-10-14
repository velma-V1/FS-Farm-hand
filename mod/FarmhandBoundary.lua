-- Internal capability boundary only; no FS25 or external mod integration yet.
local FarmhandBoundary = {}

function FarmhandBoundary.capabilities()
    return { move = false, attach = false, stop = false }
end

function FarmhandBoundary.execute(operation)
    return { ok = false, code = "UNSUPPORTED", operation = operation }
end

return FarmhandBoundary
