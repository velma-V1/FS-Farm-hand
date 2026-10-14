# FS25 Lua boundary — not yet an installable mod

FarmhandBoundary.lua contains only a pure Lua capability stub. It makes no GIANTS
or third-party calls and always reports unsupported physical execution. CI executes
the Lua boundary, but FS25 loading has not been tested. No modDesc.xml, assets,
transport, or installation ZIP is supplied until there is a real integration to load.

The intended production runtime is local on the user's Windows PC alongside FS25.
