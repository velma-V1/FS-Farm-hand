std = "lua51"
max_line_length = false

globals = {
    "FarmhandObserve",
    "FarmhandHitch",
    "FarmhandDrive",
    "FarmhandBridge",
    "FarmhandProbe",
    "FarmhandReloading",   -- set by fhReload (FarmhandProbe) while it re-reads the files
    "addModEventListener",
    "addConsoleCommand",
    "removeConsoleCommand",
}
read_globals = {
    "g_currentModDirectory",   -- GIANTS: the loading mod's folder
}
