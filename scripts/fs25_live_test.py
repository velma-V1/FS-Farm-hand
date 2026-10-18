"""Development/qualification only: Claude Code's client for live FS25 tests.

Runs inside the sandbox: it touches nothing outside this checkout. It writes one
request at a time to .fs25-dev/host/requests and reads the answer of the trusted host
runner (scripts/fs25_host_runner.py, started once by the user outside the sandbox),
which alone reaches FS25 and the Farmhand companion.

    python scripts/fs25_live_test.py status     FS25, companion, builds, READY, candidates
    python scripts/fs25_live_test.py install    build this checkout, install it (FS25 closed)
    python scripts/fs25_live_test.py launch     start FS25
    python scripts/fs25_live_test.py restart    stop FS25, start it, wait for Farmhand READY
    python scripts/fs25_live_test.py stop-fs25  close FS25 (refused while a job runs)
    python scripts/fs25_live_test.py hitch      one production hitch_pair; waits for its end
    python scripts/fs25_live_test.py request "bring the forage wagon here"
                                                the player's typed words; waits for the job's end
    python scripts/fs25_live_test.py stop       the production STOP
    python scripts/fs25_live_test.py collect [--test ID]

Output: one JSON object per line. The evidence of a test is also saved under
.fs25-dev/evidence/<test id>/. Exit status: 0 PASS (a COMPLETE test, a READY status,
a successful operation); 1 FAILED; 3 BLOCKED; 4 UNVERIFIED; 5 CANCELLED; 10 the
runner, companion or game could not do what was asked (the "code" says why).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / ".fs25-dev" / "host"
EVIDENCE = ROOT / ".fs25-dev" / "evidence"
BUILD = ROOT / ".fs25-dev" / "build"
RUNNER_STALE_S = 5.0
# How long each op may take on the host (its own bounds plus a margin).
TIMEOUT_S = {"status": 40.0, "install-current-build": 180.0, "launch-fs25": 90.0, "restart-fs25": 1100.0,
             "stop-fs25": 90.0, "run-hitch-test": 30.0, "run-request-test": 30.0, "stop-test": 30.0, "collect": 30.0}
HITCH_WAIT_S = 2400.0          # beyond the longest job: TRAVEL_NEAR alone may take 20 min
EXIT = {"COMPLETE": 0, "FAILED": 1, "BLOCKED": 3, "UNVERIFIED": 4, "CANCELLED": 5}
INFRASTRUCTURE = 10


def fingerprint(token: str) -> str:
    """= scripts/fs25_host_runner.py fingerprint: which runner wrote an answer."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()[:16]


class Unavailable(Exception):
    def __init__(self, code: str, **detail):
        super().__init__(code)
        self.code, self.detail = code, detail


def emit(**fields) -> None:
    print(json.dumps(fields, separators=(",", ":")), flush=True)


def runner(host: Path | None = None, clock=time.time) -> dict:
    host = host or HOST
    try:
        state = json.loads((host / "runner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Unavailable("HOST_RUNNER_NOT_RUNNING",
                          hint="start it once outside the sandbox: see scripts/fs25_host_runner.py") from None
    if not isinstance(state, dict) or clock() - float(state.get("beat", 0)) > RUNNER_STALE_S:
        raise Unavailable("HOST_RUNNER_NOT_RUNNING", hint="runner.json is stale: the runner stopped")
    return state


_last_id = 0


def call(op: str, args: dict | None = None, host: Path | None = None, timeout: float | None = None) -> dict:
    """One request to the host runner and its answer."""
    global _last_id
    host = host or HOST
    state = runner(host)
    _last_id = request_id = max(time.time_ns(), _last_id + 1)
    request = {"version": 1, "id": str(request_id), "token": state["token"], "op": op, "args": args or {},
               "created": time.time()}
    folder = host / "requests"
    temporary = folder / f".request-{request_id}.tmp"
    temporary.write_text(json.dumps(request), encoding="utf-8")
    temporary.replace(folder / f"request-{request_id}.json")
    answer = host / "responses" / f"response-{request_id}.json"
    deadline = time.monotonic() + (timeout or TIMEOUT_S[op])
    while time.monotonic() < deadline:
        if answer.exists():
            try:
                response = json.loads(answer.read_text(encoding="utf-8"))
            except ValueError:               # still being renamed into place
                time.sleep(0.1)
                continue
            answer.unlink(missing_ok=True)
            return checked(response, op, str(request_id), state["token"])
        time.sleep(0.2)
    (folder / f"request-{request_id}.json").unlink(missing_ok=True)
    raise Unavailable("HOST_NO_ANSWER", op=op)


def checked(response, op: str, request_id: str, token: str) -> dict:
    """The answer of the runner this request was sent to, for this request, or
    Unavailable. Any other writer's answer is never taken as the outcome: the request
    may have been taken (and, by an older runner, acted on) elsewhere."""
    if not isinstance(response, dict) or response.get("runner") != fingerprint(token):
        raise Unavailable("ANOTHER_RUNNER_ANSWERED", op=op,
                          hint="a process other than the runner in runner.json answered; stop every other "
                               "runner, then check 'status' (and 'collect' after a hitch)")
    if (response.get("version") != 1 or response.get("id") != request_id
            or response.get("op") not in (op, None) or type(response.get("ok")) is not bool
            or not isinstance(response.get("code"), str) or not isinstance(response.get("result"), dict)
            or (response["ok"] and response["op"] != op)):
        raise Unavailable("HOST_ANSWER_MISMATCH", op=op)
    return response


def save_evidence(result: dict) -> Path | None:
    summary = result.get("summary") or {}
    test_id = summary.get("test_id")
    if not test_id:
        return None
    folder = EVIDENCE / test_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (folder / "record.json").write_text(json.dumps(result.get("record"), indent=1), encoding="utf-8")
    (folder / "game-log.txt").write_text("\n".join(result.get("game_log") or []), encoding="utf-8")
    (folder / "companion-log.txt").write_text("\n".join((result.get("record") or {}).get("companion_log") or []),
                                              encoding="utf-8")
    return folder


def finish_test(response: dict) -> int:
    result = response["result"]
    folder = save_evidence(result)
    summary = result.get("summary") or {}
    emit(event="result", status=summary.get("status"), reason=summary.get("reason"),
         evidence=str(folder) if folder else None, summary=summary)
    return EXIT.get(summary.get("status"), INFRASTRUCTURE)


def command_hitch(wait: float = HITCH_WAIT_S, poll: float = 3.0, text: str | None = None) -> int:
    started = call("run-hitch-test") if text is None else call("run-request-test", {"text": text})
    if not started.get("ok"):
        emit(event="refused", code=started.get("code"), result=started.get("result"))
        return INFRASTRUCTURE
    test_id = started["result"]["test_id"]
    emit(event="started", test_id=test_id)
    deadline, seen = time.monotonic() + wait, 0
    while time.monotonic() < deadline:
        response = call("collect", {"test_id": test_id})
        if not response.get("ok"):
            emit(event="collect_failed", code=response.get("code"))
            return INFRASTRUCTURE
        steps = response["result"]["summary"].get("steps") or []
        for step in steps[seen:]:
            emit(event="step", step=step.get("step"), at=step.get("at"))
        seen = len(steps)
        if response["code"] == "TERMINAL":
            return finish_test(response)
        time.sleep(poll)
    emit(event="timeout", test_id=test_id, hint="the job keeps its own bounds; 'collect' or 'stop'")
    return INFRASTRUCTURE


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "install", "launch", "restart", "stop-fs25", "hitch", "stop"):
        sub.add_parser(name)
    words = sub.add_parser("request")
    words.add_argument("text", help="the words, as a player would type them")
    collect = sub.add_parser("collect")
    collect.add_argument("--test", help="a test id (default: the latest)")
    args = parser.parse_args(argv)
    try:
        if args.command == "hitch":
            return command_hitch()
        if args.command == "request":
            return command_hitch(text=args.text)
        if args.command == "collect":
            response = call("collect", {"test_id": args.test} if args.test else {})
            if not response.get("ok"):
                emit(event="collect", ok=False, code=response.get("code"), result=response.get("result"))
                return INFRASTRUCTURE
            return finish_test(response)
        if args.command == "install":
            subprocess.run([sys.executable, str(ROOT / "scripts" / "package_probe.py"), "--build-dev", str(BUILD)],
                           check=True, stdout=subprocess.DEVNULL)
        op = {"status": "status", "install": "install-current-build", "launch": "launch-fs25",
              "restart": "restart-fs25", "stop-fs25": "stop-fs25", "stop": "stop-test"}[args.command]
        response = call(op)
        emit(event=op, ok=response.get("ok"), code=response.get("code"), result=response.get("result"))
        if op == "status":
            return 0 if response.get("ok") and response["result"].get("farmhand_ready") else INFRASTRUCTURE
        return 0 if response.get("ok") else INFRASTRUCTURE
    except Unavailable as error:
        emit(event="unavailable", code=error.code, **error.detail)
        return INFRASTRUCTURE
    except subprocess.CalledProcessError:
        emit(event="unavailable", code="BUILD_FAILED")
        return INFRASTRUCTURE


if __name__ == "__main__":
    sys.exit(main())
