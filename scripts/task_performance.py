"""Read-only FS25 Farmhand task-latency baseline from existing live-test evidence.

Measured: accepted-to-first-dispatched command and accepted-to-terminal result.
A dispatched command is NOT proof of physical motion; exact first useful physical
progress needs dedicated observed-pose/work-state instrumentation.

Usage:
  .venv/bin/python scripts/task_performance.py --output .fs25-dev/performance/baseline.json
  .venv/bin/python scripts/task_performance.py --since 2026-11-04 --compare .fs25-dev/performance/baseline.json
Does not send requests, control FS25, or change the runtime.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import statistics

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".fs25-dev" / "evidence"
LOG_COMMAND = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d+)\s+command id=\d+ op=([a-z_]+)\b")
NO_WORK = frozenset({"pay", "stop"})
MIN_COMPARABLE = 5


def local_time(value: str) -> datetime:
    return datetime.strptime(value.replace(",", "."), "%Y-%m-%d %H:%M:%S.%f")


def percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent / 100
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (index - low), 2)


def record_metrics(record: dict) -> dict | None:
    try:
        start = float(record["started_unix"])
        end = float(record["ended_unix"])
        anchor = local_time(record["started"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not (0 <= end - start <= 24 * 3600) or record.get("status") == "WORKING":
        return None
    duration = end - start
    first_dispatch = first_work = None
    for line in record.get("companion_log") or []:
        match = LOG_COMMAND.match(line)
        if not match:
            continue
        try:
            seconds = (local_time(match.group(1)) - anchor).total_seconds()
        except ValueError:
            continue
        if not (0 <= seconds <= duration + 1):
            continue
        if first_dispatch is None:
            first_dispatch = round(seconds, 2)
        if first_work is None and match.group(2) not in NO_WORK:
            first_work = round(seconds, 2)
    config = record.get("configuration") or {}
    proposal = record.get("proposal") or {}
    return {
        "task": str(proposal.get("command") or "unknown"),
        "map": str(config.get("map") or "unknown"),
        "save": str(config.get("savegame") if config.get("savegame") is not None else "unknown"),
        "status": record["status"],
        "build": str(record.get("companion_build") or "unknown"),
        "started": record["started"],
        "terminal_s": round(duration, 2),
        "first_command_dispatch_s": first_dispatch,
        "first_work_command_dispatch_s": first_work,
        # The existing live-test record does not timestamp verified physical progress.
        "first_verified_physical_progress_s": None,  # general work-state outcome not yet captured
        "first_verified_equipment_movement_s": (
            record["first_verified_equipment_movement"].get("elapsed_s")
            if isinstance(record.get("first_verified_equipment_movement"), dict) else None
        ),
    }


def stats(rows: list[dict]) -> dict:
    def metric(key):
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return {"count": len(values), "p50_s": percentile(values, 50), "p90_s": percentile(values, 90)}
    succeeded = [r for r in rows if r["status"] == "COMPLETE"]
    return {"jobs": len(rows), "complete": len(succeeded),
            "not_complete": len(rows) - len(succeeded),
            "completion_rate": round(len(succeeded) / len(rows), 3) if rows else None,
            "terminal_all": metric("terminal_s"),
            "terminal_complete": metric("terminal_s") if len(succeeded) == len(rows) else
                {"count": len(succeeded), "p50_s": percentile([r["terminal_s"] for r in succeeded], 50),
                 "p90_s": percentile([r["terminal_s"] for r in succeeded], 90)},
            "first_command_dispatch": metric("first_command_dispatch_s"),
            "first_work_command_dispatch": metric("first_work_command_dispatch_s"),
            "first_verified_physical_progress": {"count": 0, "p50_s": None, "p90_s": None},
            "first_verified_equipment_movement": metric("first_verified_equipment_movement_s")}


def build_report(evidence: Path, since: str | None = None) -> dict:
    groups = defaultdict(list)
    all_rows = []
    for path in sorted(evidence.glob("*/record.json")):
        try:
            row = record_metrics(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            continue
        if row is None or (since and row["started"][:len(since)] < since):
            continue
        all_rows.append(row)
        groups[(row["task"], row["map"], row["save"])].append(row)
    return {
        "source": str(evidence),
        "since": since,
        "scope": "historical mixed-build live evidence; uncontrolled comparison",
        "timing_anchor": "accepted job (not the initial player keypress)",
        "first_work_command_note": "Command dispatch is a latency proxy, not proven physical movement",
        "first_verified_physical_progress": "General work-state progress NOT_CAPTURED by historical live-test records",
        "first_verified_equipment_movement": "Captured in future live qualification records when reserved equipment moves >=0.5 m; incomplete observations remain null",
        "overall": stats(all_rows),
        "cohorts": [
            {"task": task, "map": map_name, "save": save, **stats(rows)}
            for (task, map_name, save), rows in sorted(groups.items())
        ],
    }


def comparable(report: dict, baseline: dict) -> list[dict]:
    base = {(c["task"], c["map"], c["save"]): c for c in baseline.get("cohorts", [])}
    result = []
    for current in report["cohorts"]:
        key = current["task"], current["map"], current["save"]
        old = base.get(key)
        if old is None or min(current["complete"], old["complete"]) < MIN_COMPARABLE:
            continue
        prev = old["terminal_complete"]["p50_s"]
        new = current["terminal_complete"]["p50_s"]
        if not prev:
            continue
        result.append({"task": key[0], "map": key[1], "save": key[2],
                       "previous_n": old["complete"], "current_n": current["complete"],
                       "median_before_s": prev, "median_after_s": new,
                       "median_change_percent": round(100 * (new - prev) / prev, 1),
                       "note": "Non-causal comparison; repeat same scenario/save/rig for an actual regression gate"})
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--evidence", type=Path, default=EVIDENCE)
    ap.add_argument("--since", help="Include records starting on/after YYYY-MM-DD or YYYY-MM-DD HH:MM")
    ap.add_argument("--output", type=Path, help="Write aggregate report as JSON (no raw records copied)")
    ap.add_argument("--compare", type=Path, help="Compare to a saved baseline; not a controlled benchmark")
    args = ap.parse_args(argv)
    report = build_report(args.evidence, args.since)
    if args.compare:
        report["comparison"] = comparable(report, json.loads(args.compare.read_text()))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"overall": report["overall"], "cohorts": len(report["cohorts"]),
                      "comparison": report.get("comparison"), "output": str(args.output) if args.output else None},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
