"""Development only: score a local language model behind Farmhand's rules on labelled wording
(tests/fixtures/model_eval.json, plus tests/fixtures/phrasings.json, which the rules alone
must already reach). The model is reached exactly as in production (companion/model.py
ModelInterpreter: llama-server, schema-constrained, the result counting only when the rules
reach the same command from its rewording). Each sentence goes to the rules first, as the
companion does; only what they miss goes to the model.

    python scripts/model_eval.py --url http://127.0.0.1:39231
    python scripts/model_eval.py --server PATH/llama-server --model PATH/model.gguf

Scores: correct, abstained (nothing done: safe), wrong (an executable proposal other than the
label: the dangerous kind), and the model's median seconds.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion.language import RuleInterpreter  # noqa: E402
from companion.model import CONTEXT, LOCAL, ModelInterpreter, THREADS  # noqa: E402


def matches(got, want) -> bool:
    if isinstance(want, dict):
        return isinstance(got, dict) and all(matches(got.get(k), v) for k, v in want.items())
    if isinstance(want, list) and want and isinstance(want[0], dict):
        return isinstance(got, list) and len(got) == len(want) and all(map(matches, got, want))
    return got == want


def wait_ready(url: str, seconds: float = 300.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with LOCAL(url + "/health", timeout=3) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(2)
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:39231")
    parser.add_argument("--server", help="a llama-server binary to start for the run")
    parser.add_argument("--model", help="the GGUF model for --server")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per sentence (CPU)")
    args = parser.parse_args(argv)
    process = None
    if args.server:
        process = subprocess.Popen([args.server, "-m", args.model, "--host", "127.0.0.1",
                                    "--port", args.url.rsplit(":", 1)[1], "-t", str(THREADS), "-c", str(CONTEXT),
                                    "-ngl", "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_ready(args.url):
            print("server not ready", file=sys.stderr)
            process.kill()
            return 2
    try:
        rules, model = RuleInterpreter(), ModelInterpreter(args.url, timeout=args.timeout)
        cases = json.loads((ROOT / "tests" / "fixtures" / "model_eval.json").read_text())["cases"]
        cases += [{"say": p["say"], "expect": p["expect"]}
                  for p in json.loads((ROOT / "tests" / "fixtures" / "phrasings.json").read_text())["phrasings"]]
        tally, times, rows = {"correct": 0, "abstained": 0, "wrong": 0}, [], []
        for case in cases:
            by = "rules"
            got = rules.interpret(case["say"])
            if got is None:
                by, started = "model", time.monotonic()
                got = model.interpret(case["say"])
                times.append(time.monotonic() - started)
            want = case["expect"]
            if want is None:
                verdict = "correct" if got is None or got.get("command") in ("unsupported", "status") else "wrong"
            elif got is None:
                verdict = "abstained"
            else:
                verdict = "correct" if matches(got, want) else "wrong"
            tally[verdict] += 1
            rows.append({"say": case["say"], "by": by, "verdict": verdict, "got": got, "want": want})
            print(f"{verdict:9} {by:5} {case['say']!r} -> {json.dumps(got)[:160]}", flush=True)
        total = sum(tally.values())
        summary = {**tally, "total": total, "median_model_s": round(statistics.median(times), 2) if times else None,
                   "model": args.model or args.url}
        print(json.dumps(summary))
        out = ROOT / ".fs25-dev" / "model_eval"
        out.mkdir(parents=True, exist_ok=True)
        (out / time.strftime("%Y%m%d-%H%M%S.json")).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
        return 0 if tally["wrong"] == 0 else 1
    finally:
        if process is not None:
            process.kill()


if __name__ == "__main__":
    sys.exit(main())
