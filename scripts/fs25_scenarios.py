"""Development/qualification only: run Farmhand's acceptance scenarios (tests/scenarios.json)
live, in the player's own words, through the live-test client (scripts/fs25_live_test.py) and
the user's host runner. Each scenario runs only on a save it fits ("saves"; "any" for every
map); its requests go in order, each waiting for its job's end; a concurrent scenario's
background helpers must then finish too (the companion's own log says so). Results go to
.fs25-dev/scenarios/<time>.json and a table on stdout. Nothing here drives the game itself:
every action is Farmhand's, from the words.

    python scripts/fs25_scenarios.py --list
    python scripts/fs25_scenarios.py                 every scenario that fits the loaded save
    python scripts/fs25_scenarios.py --only land_buy_sell,save_game
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "tests" / "scenarios.json"
CLIENT = ROOT / "scripts" / "fs25_live_test.py"
REPORTS = ROOT / ".fs25-dev" / "scenarios"
REQUEST_TIMEOUT_S = 4 * 3600          # a long haul on a large map
HELPER_TIMEOUT_S = 3 * 3600           # background helpers of a concurrent scenario
COMPANION_LOG = Path("/mnt/c/Users/Matt/AppData/Local/Packages/GIANTSSoftware.FarmingSimulator25PC_fa8jxm5fj0esw"
                     "/LocalCache/Local/modSettings/FS25_Farmhand/companion.log")


def client(*args: str, timeout: float) -> list[dict]:
    """The live client's JSON lines (one object per line)."""
    done = subprocess.run([sys.executable, str(CLIENT), *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=timeout)
    out = []
    for line in done.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def loaded_save() -> int | None:
    for event in client("status", timeout=120):
        info = (((event.get("result") or {}).get("live") or {}).get("farm") or {}).get("info") or {}
        if isinstance(info.get("savegame"), int):
            return info["savegame"]
    return None


def request(say: str) -> dict:
    """One request in the player's words, to its job's end: {status, reason, test_id}."""
    if say.strip().casefold() == "stop":
        events = client("stop", timeout=120)
        ok = any(e.get("ok") for e in events)
        return {"status": "COMPLETE" if ok else "FAILED", "reason": "STOP_SENT" if ok else "STOP_NOT_SENT"}
    events = client("request", say, timeout=REQUEST_TIMEOUT_S)
    result = next((e for e in reversed(events) if e.get("event") == "result"), None)
    if result is None:
        refused = next((e for e in events if e.get("event") in ("refused", "timeout")), {})
        return {"status": "INFRASTRUCTURE", "reason": refused.get("code") or refused.get("event") or "NO_RESULT"}
    return {"status": result.get("status"), "reason": result.get("reason"),
            "test_id": (result.get("summary") or {}).get("test_id")}


def helpers_done(since: float, timeout: float = HELPER_TIMEOUT_S) -> bool:
    """A concurrent scenario's background helpers: every WORKER_STARTED after ``since`` matched by
    a worker's end in the companion's log (its notices)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = COMPANION_LOG.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        lines = [l for l in text.splitlines() if l[:19] >= time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))]
        started = sum("notice" in l and "WORKER_STARTED" in l for l in lines)
        ended = sum("notice" in l and ("WORKER_DONE" in l or "WORKER_FAILED" in l or "WORKER_STOPPED" in l)
                    for l in lines)
        if started and ended >= started:
            return True
        time.sleep(30)
    return False


STOP_AFTER_S = 60.0                  # a request followed by "stop" is stopped this long after it starts


def run(scenario: dict) -> dict:
    began, outcomes, verdict = time.time(), [], "PASS"
    pairs = list(zip(scenario["say"], scenario["expect"]))
    pending = {}
    for n, (say, expect) in enumerate(pairs):
        if n in pending:
            got = pending.pop(n)
        elif n + 1 < len(pairs) and pairs[n + 1][0].strip().casefold() == "stop":
            # The job must be running when STOP comes: it runs in the background meanwhile.
            box = {}
            worker = threading.Thread(target=lambda: box.update(request(say)), daemon=True)
            worker.start()
            time.sleep(STOP_AFTER_S)
            stopped = request("stop")
            worker.join(REQUEST_TIMEOUT_S)
            got = box or {"status": "INFRASTRUCTURE", "reason": "NO_RESULT"}
            pending[n + 1] = stopped
        else:
            got = request(say)
        outcomes.append({"say": say, "expect": expect, **got})
        if got["status"] != expect["status"] or ("reason" in expect and got.get("reason") != expect["reason"]):
            verdict = "FAIL"
            if not scenario.get("concurrent"):
                break
    if verdict == "PASS" and scenario.get("concurrent") and not helpers_done(began):
        verdict = "FAIL"
        outcomes.append({"say": "(background helpers)", "status": "NOT_FINISHED"})
    return {"id": scenario["id"], "title": scenario["title"], "verdict": verdict, "outcomes": outcomes,
            "seconds": round(time.time() - began)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list the scenarios and exit")
    parser.add_argument("--only", help="comma-separated scenario ids")
    args = parser.parse_args(argv)
    scenarios = json.loads(SCENARIOS.read_text())["scenarios"]
    if args.list:
        for s in scenarios:
            print(f"{s['id']:28} saves={s['saves']!s:8} implemented={s['implemented']!s:5} {s['title']}")
        return 0
    wanted = set(args.only.split(",")) if args.only else None
    save = loaded_save()
    report = {"save": save, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "results": []}
    for scenario in scenarios:
        if wanted is not None and scenario["id"] not in wanted:
            continue
        if scenario["saves"] != "any" and save not in scenario["saves"]:
            report["results"].append({"id": scenario["id"], "verdict": "OTHER_MAP", "saves": scenario["saves"]})
            continue
        if not scenario["implemented"]:
            report["results"].append({"id": scenario["id"], "verdict": "NOT_BUILT"})
            continue
        result = run(scenario)
        report["results"].append(result)
        print(json.dumps(result), flush=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / time.strftime("%Y%m%d-%H%M%S.json")
    path.write_text(json.dumps(report, indent=2))
    for r in report["results"]:
        print(f"{r['verdict']:10} {r['id']}")
    print(f"report: {path}")
    return 0 if all(r["verdict"] in ("PASS", "OTHER_MAP") for r in report["results"]) else 1


if __name__ == "__main__":
    sys.exit(main())
