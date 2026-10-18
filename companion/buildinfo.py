"""The build this companion was installed from.

"source" when run from a checkout; scripts/package_probe.py replaces it in built copies
with the commit (plus "-dirty" for uncommitted tracked changes). The mod carries the
same value (mod/FarmhandObserve.lua Observe.BUILD) in its hello frame, so a game still
running an older mod is visible (companion/livetest.py status).
"""
BUILD = "source"
