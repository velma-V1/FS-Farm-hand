"""Fail qualification if CodeQL produced any actionable SARIF result."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_codeql_sarif.py <sarif-file-or-directory>")

    target = Path(sys.argv[1])
    files = [target] if target.is_file() else sorted(target.rglob("*.sarif"))
    if not files:
        raise SystemExit(f"no SARIF files found under {target}")

    findings: list[tuple[str, str, str]] = []
    for path in files:
        document = json.loads(path.read_text(encoding="utf-8"))
        for run in document.get("runs", []):
            rules = {
                rule.get("id"): rule
                for rule in run.get("tool", {}).get("driver", {}).get("rules", [])
                if rule.get("id")
            }
            for result in run.get("results", []):
                rule_id = result.get("ruleId", "<unknown>")
                rule = rules.get(rule_id, {})
                properties = rule.get("properties", {})
                kind = str(properties.get("kind", "")).lower()
                tags = [str(tag).lower() for tag in properties.get("tags", [])]

                # CodeQL diagnostic/metric queries support extraction and reporting;
                # they are not source findings. Any other result is qualification
                # relevant, including security and quality problem/path-problem rules.
                if kind in {"diagnostic", "metric"}:
                    continue
                if "/diagnostics/" in rule_id.lower() or "/summary/" in rule_id.lower():
                    continue

                message = result.get("message", {}).get("text", "")
                level = result.get("level", "warning")
                findings.append((rule_id, level, message))

    if findings:
        for rule_id, level, message in findings:
            print(f"CodeQL finding: {rule_id} [{level}] {message}")
        raise SystemExit(f"{len(findings)} actionable CodeQL finding(s)")

    print(f"CodeQL qualification clean: 0 actionable findings across {len(files)} SARIF file(s).")


if __name__ == "__main__":
    main()
