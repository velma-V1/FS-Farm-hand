#!/bin/bash
# TEMPORARY development helper - delete before release (only by the human or on an explicit request).
# Menu reload of savegame slot 1 (save through Farmhand first). Clicks as proven 2026-10-31:
# Esc -> Settings tab -> QUIT GAME -> YES -> CAREER -> slot 1 -> CONTINUE -> START (mods),
# then START on "Done!". Loaded = game frames gone (menus) and then fresh again.
cd /home/xxthatguyxx/projects/FS-Farm-hand || exit 1
T=".venv/bin/python scripts/fs25_live_test.py"
age() { timeout 30 $T status 2>/dev/null | tail -1 | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin); g=((d.get('result') or {}).get('live') or {}).get('game') or {}
    a=g.get('last_frame_age_s'); print(int(a) if g.get('linked') and a is not None else 999)
except Exception: print(999)" 2>/dev/null; }
# Save first (Farmhand's save, ~5 s). Timings per the user 2026-10-31: clicks < 1 s, main menu ~5 s,
# selections 1-2 s, game loading 60 s.
timeout 60 $T request "save the game" >/dev/null 2>&1; timeout 2 tail -f /dev/null
first_key=$($T key escape 2>/dev/null)
if [[ "$first_key" == *'"code":"GAME_NOT_FOREGROUND"'* ]]; then
  echo "loaded=no save=1 reason=GAME_NOT_FOREGROUND"
  exit 1
fi
timeout 2 tail -f /dev/null
$T click 0.035 0.861 >/dev/null 2>&1; timeout 2 tail -f /dev/null
$T click 0.805 0.455 >/dev/null 2>&1; timeout 2 tail -f /dev/null
$T click 0.595 0.59 >/dev/null 2>&1; timeout 6 tail -f /dev/null
$T click 0.07 0.597 >/dev/null 2>&1; timeout 2 tail -f /dev/null
$T click 0.5 0.264 >/dev/null 2>&1; timeout 2 tail -f /dev/null
$T click 0.95 0.981 >/dev/null 2>&1; timeout 2 tail -f /dev/null
$T click 0.975 0.981 >/dev/null 2>&1
gone=0
for i in $(seq 1 20); do
  timeout 4 tail -f /dev/null
  a=$(age)
  [ "$a" -gt 5 ] && gone=1
  [ $((i % 2)) -eq 0 ] && [ "$a" -gt 5 ] && $T click 0.975 0.981 >/dev/null 2>&1   # START on "Done!"
  [ $gone -eq 1 ] && [ "$a" -le 2 ] && break
done
save=$(timeout 30 $T status 2>/dev/null | tail -1 | python3 -c "import json,sys; d=json.load(sys.stdin); print(((d.get('result') or {}).get('live') or {}).get('farm',{}).get('info',{}).get('savegame'))" 2>/dev/null)
echo "loaded=$([ $gone -eq 1 ] && [ "$a" -le 2 ] && echo yes || echo no) save=$save"
