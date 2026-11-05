#!/usr/bin/env python3
"""Remote, narrowly scoped deployment of a production Farmhand build into the local lab.

DISABLED unless the owner enables it on the PC. It exists so the lab gateway can offer one
bounded tool, `live_deploy(action, commit)`, instead of the owner running fh_deploy.py by
hand for every build. It never runs a shell, never takes a path, a branch, a URL or
arguments beyond these two fields, and never runs code from the fetched commit except the
Farmhand build that a deployment installs by design:

    action  "status" | "switch" | "restore"
    commit  7-40 hex digits, a commit on origin/claude/fs25-farmhand-agent-jy2ds2 (switch only)

The tools that do the work (fh_deploy.py, package_probe.py, fs25_host_runner.py) run from a
PINNED copy the owner installed and reviewed (`install`); the commit's files are read as data.
Every request is serialized (one lock), checked, logged to the deploy state's remote.jsonl,
and goes through fh_deploy's own transaction: verified snapshot, game-side READY check, and
automatic rollback.

On the PC (WSL), in the production clone ~/farmhand-production, once, after review:
    python3 scripts/fh_remote_deploy.py install   # pin the tools; prints their sha256
    python3 scripts/fh_remote_deploy.py enable    # the owner's approval; `disable` revokes
Then register `handle` in the gateway (CLOUD_LAB_ACCESS.md, "Remote deployment").
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

BRANCH = "claude/fs25-farmhand-agent-jy2ds2"
CLONE = Path.home() / "farmhand-production"
STATE = Path.home() / ".local" / "share" / "farmhand-deploy"
TOOL = STATE / "tool"
ENABLED = STATE / "remote_deploy.enabled"
LOCK = STATE / "remote_deploy.lock"
AUDIT = STATE / "remote.jsonl"
PINNED = ("fh_deploy.py", "package_probe.py", "fs25_host_runner.py")
ACTIONS = ("status", "switch", "restore")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
GIT_TIMEOUT_S, DEPLOY_TIMEOUT_S = 120, 900


class Refused(Exception):
    def __init__(self, code: str, **detail):
        super().__init__(code)
        self.code, self.detail = code, detail


def _git(*args: str, timeout: float = GIT_TIMEOUT_S) -> str:
    done = subprocess.run(["git", "-C", str(CLONE), *args], capture_output=True, text=True,
                          timeout=timeout, check=False)
    if done.returncode != 0:
        raise Refused("GIT_FAILED", command=args[0], stderr=done.stderr.strip()[-300:])
    return done.stdout.strip()


def _pinned_tool(name: str) -> Path:
    path = TOOL / name
    manifest = json.loads((TOOL / "PINNED.json").read_text(encoding="utf-8"))
    if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get(name):
        raise Refused("TOOL_NOT_PINNED", tool=name)
    return path


def _deploy(*args: str) -> dict:
    """fh_deploy from the pinned copy, on the clone as data; its own JSON answer."""
    for name in PINNED:
        _pinned_tool(name)
    done = subprocess.run([sys.executable, "-I", str(_pinned_tool("fh_deploy.py")), *args],
                          capture_output=True, text=True, timeout=DEPLOY_TIMEOUT_S, check=False,
                          env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()),
                               "FARMHAND_REPO": str(CLONE)})
    text = (done.stdout or "").strip()
    try:
        answer = json.loads(text[text.index("{"):]) if "{" in text else {}
    except ValueError:
        answer = {"output": text[-2000:]}
    if done.returncode != 0:
        raise Refused("DEPLOY_REFUSED", exit=done.returncode, answer=answer, stderr=(done.stderr or "")[-1500:])
    return answer


def _checkout(commit: str) -> str:
    """The commit, if and only if it is on the production branch; the clone left clean on it."""
    _git("fetch", "--quiet", "origin", BRANCH)
    full = _git("rev-parse", "--verify", "--quiet", commit + "^{commit}")
    if not _COMMIT.fullmatch(full) or len(full) != 40:
        raise Refused("UNKNOWN_COMMIT", commit=commit)
    ancestor = subprocess.run(["git", "-C", str(CLONE), "merge-base", "--is-ancestor", full, "origin/" + BRANCH],
                              capture_output=True, timeout=GIT_TIMEOUT_S, check=False)
    if ancestor.returncode != 0:
        raise Refused("NOT_ON_PRODUCTION_BRANCH", commit=full)
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise Refused("CLONE_DIRTY")
    _git("checkout", "--quiet", "--detach", full)
    return full


def _audit(entry: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(AUDIT, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds"), **entry}) + "\n")


def handle(request: dict) -> dict:
    """The gateway's whole interface: {action, commit?} in, a JSON-safe answer out."""
    action = request.get("action") if isinstance(request, dict) else None
    commit = request.get("commit") if isinstance(request, dict) else None
    try:
        if set(request) - {"action", "commit"}:
            raise Refused("UNEXPECTED_FIELDS")
        if action not in ACTIONS:
            raise Refused("UNKNOWN_ACTION")
        if action == "switch" and not (isinstance(commit, str) and _COMMIT.fullmatch(commit)):
            raise Refused("COMMIT_REQUIRED")
        if action != "switch" and commit is not None:
            raise Refused("UNEXPECTED_FIELDS")
        if action != "status" and not ENABLED.is_file():
            raise Refused("REMOTE_DEPLOY_DISABLED", hint="the owner enables it on the PC")
        STATE.mkdir(parents=True, exist_ok=True)
        with open(LOCK, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused("DEPLOY_BUSY") from None
            if action == "status":
                answer = {"enabled": ENABLED.is_file(), "deploy": _deploy("status")}
            elif action == "restore":
                answer = {"restore": _deploy("restore")}
            else:
                full = _checkout(commit)
                answer = {"commit": full, "stage": _deploy("stage"), "switch": _deploy("switch")}
        _audit({"action": action, "commit": commit, "ok": True})
        return {"ok": True, **answer}
    except Refused as error:
        _audit({"action": action, "commit": commit, "ok": False, "refused": error.code})
        return {"ok": False, "refused": error.code, **error.detail}
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        _audit({"action": action, "commit": commit, "ok": False, "error": type(error).__name__})
        return {"ok": False, "refused": "DEPLOY_ERROR", "error": type(error).__name__}


def install() -> int:
    """Pin the reviewed tools from this clone (rerun, and review the diff, to update them)."""
    here = Path(__file__).resolve().parent
    TOOL.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name in PINNED:
        shutil.copyfile(here / name, TOOL / name)
        manifest[name] = hashlib.sha256((TOOL / name).read_bytes()).hexdigest()
        print(f"pinned {TOOL / name}  sha256 {manifest[name]}")
    (TOOL / "PINNED.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else ""
    if command == "install":
        return install()
    if command == "enable":
        STATE.mkdir(parents=True, exist_ok=True)
        ENABLED.write_text(datetime.datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")
        print(f"remote deployment ENABLED ({ENABLED}); `disable` revokes it")
        return 0
    if command == "disable":
        with contextlib.suppress(FileNotFoundError):
            ENABLED.unlink()
        print("remote deployment disabled")
        return 0
    if command in ACTIONS:                           # the owner's own check of what the gateway would do
        request = {"action": command, **({"commit": argv[1]} if command == "switch" and len(argv) > 1 else {})}
        print(json.dumps(handle(request), indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
