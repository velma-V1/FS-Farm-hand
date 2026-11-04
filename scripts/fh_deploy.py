#!/usr/bin/env python3
"""Reversible test deployment of a Farmhand build into the local FS25 lab.

Runs on the PC (WSL), from its own clone of the production branch, never from the
lab checkout. It changes exactly two installed folders and three companion state
files, and keeps every byte it replaces:

    <profile>/mods/FS25_Farmhand/          the mod the game runs
    <data>/app/                            the companion the runner starts
    <data>/settings.json goal.json routes.json

    python3 scripts/fh_deploy.py stage     build this clone, keep the build
    python3 scripts/fh_deploy.py status    what is installed, staged and kept
    python3 scripts/fh_deploy.py switch    keep the installed lab build, install the staged one
    python3 scripts/fh_deploy.py restore   put the kept lab build back, byte for byte
    python3 scripts/fh_deploy.py selftest  the whole round trip on a throwaway copy

switch and restore refuse while a Farmhand job or test runs or a reload is pending.
With FS25 running, the mod's Lua is re-read in the background by the existing
.fhReload marker (FarmhandProbe:consoleReload): no key, click, focus change, game
restart or save reload. The companion is restarted windowless with the runner's own
stop/start. Any failed step puts back what was there before it. State lives in
~/.local/share/farmhand-deploy (kept builds, snapshots, active.json, log.jsonl).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fs25_host_runner as runner  # noqa: E402  (the runner's fixed targets and companion control)

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path.home() / ".local" / "share" / "farmhand-deploy"
LAB_CHECKOUT = Path.home() / "projects" / "FS-Farm-hand"     # Codex's lab: never built or written here
STATE_FILES = ("settings.json", "goal.json", "routes.json")
SKIP_DIRS = {"__pycache__"}
RELOADED = ("FarmhandObserve.lua", "FarmhandHitch.lua", "FarmhandDrive.lua", "FarmhandBridge.lua", "FarmhandProbe.lua")
RELOAD_CONSUME_S, RELOAD_LOG_S = 30.0, 15.0
# After the companion starts: the game must report the new build, linked with fresh frames,
# twice in a row this far apart, within the bounded wait, with no Farmhand Lua error logged.
READY_WAIT_S, READY_SPACING_S = 60.0, 3.0
_LUA_ERROR = re.compile(r"(reload_\d+_\d+\.lua|Farmhand\w*\.lua):\d+:")
_STAMP = re.compile(r'BUILD = "([A-Za-z0-9_.-]+)"')


class DeployError(RuntimeError):
    def __init__(self, code: str, **detail):
        super().__init__(code)
        self.code, self.detail = code, detail


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_tree(root: Path) -> dict[str, bytes]:
    """Every file under root (no links; Python's caches skipped), by relative path."""
    if root.is_symlink():
        raise DeployError("SYMLINK_REFUSED", path=str(root))
    if not root.exists():
        return {}
    files = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if SKIP_DIRS & set(rel.parts):
            continue
        if path.is_symlink():
            raise DeployError("SYMLINK_REFUSED", path=str(path))
        if path.is_file():
            files[rel.as_posix()] = path.read_bytes()
    return files


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    """Make root hold exactly files: each replaced in place (temp + rename), extras and
    Python caches removed. The folder itself is never renamed (the game may hold it)."""
    if root.is_symlink():
        raise DeployError("SYMLINK_REFUSED", path=str(root))
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        out = root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.is_file() and not out.is_symlink() and out.read_bytes() == data:
            continue
        temporary = out.with_name(out.name + ".fhdeploy.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, out)
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = path.relative_to(root)
        if SKIP_DIRS & set(rel.parts):
            if path.is_dir() and path.name in SKIP_DIRS:
                shutil.rmtree(path, ignore_errors=True)
            continue
        if path.is_file() and rel.as_posix() not in files:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def build_of(app: dict[str, bytes]) -> str | None:
    found = _STAMP.search(app.get("companion/buildinfo.py", b"").decode("utf-8", "replace"))
    return found.group(1) if found else None


class Install:
    """What one deployment consists of: the mod, the companion app and its state."""

    def __init__(self, mod: dict, app: dict, state: dict):
        self.mod, self.app, self.state = mod, app, state        # state: name -> bytes | None (absent)

    def digest(self) -> dict:
        return {"mod": {k: _sha(v) for k, v in self.mod.items()},
                "app": {k: _sha(v) for k, v in self.app.items()},
                "state": {k: (None if v is None else _sha(v)) for k, v in self.state.items()}}


class Host:
    """The live side, through the runner's own code: process list, companion stop and
    windowless start, the companion's job answer, and the .fhReload marker."""

    def __init__(self, targets: runner.Targets):
        self.targets = targets
        self.runner = runner.Runner(ROOT, targets)
        self.log_offset = 0

    def fs25_running(self) -> bool:
        return bool(self.runner.system.processes(runner.FS25_IMAGE))

    def job_running(self) -> bool:
        """True only when the companion says no job or test runs; an answer that cannot be
        had or read is unknown and refuses (unlike the runner's own, more lenient check)."""
        if self.runner._companion_pid() is None:
            if not self.fs25_running():
                return False                                  # no game, no companion: nothing can drive
            raise DeployError("JOB_STATUS_UNKNOWN", hint="the companion is not running; start it or close FS25")
        try:
            result = self.runner.companion.ask("status").get("result")
        except runner.Refused as error:
            raise DeployError("JOB_STATUS_UNKNOWN", companion=error.code) from None
        job = result.get("job") if isinstance(result, dict) else None
        if not isinstance(job, dict) or "running" not in job or "test_active" not in result:
            raise DeployError("JOB_STATUS_UNKNOWN", companion="unreadable status")
        return bool(result["test_active"] or job["running"])

    def reload_pending(self) -> bool:
        return (self.targets.settings / ".fhReload").exists()

    def stop_companion(self) -> None:
        self.runner._stop_companion()

    def start_companion(self, build: str) -> int:
        return self.runner._start_companion(build)

    def _log_since(self, offset: int) -> str:
        log = self.targets.game_log
        if not log.is_file():
            return ""
        size = log.stat().st_size
        with open(log, "rb") as handle:
            handle.seek(offset if size >= offset else 0)
            return handle.read(4_000_000).decode("utf-8", "replace")

    def game_ready(self, build: str) -> tuple[bool, dict]:
        """The game side, after the companion started: linked, fresh, running `build` on
        both sides, no Lua error from Farmhand since the reload. Two healthy answers
        READY_SPACING_S apart (a per-frame error stales the snapshot within that)."""
        deadline, healthy, last = time.monotonic() + READY_WAIT_S, 0, {}
        while time.monotonic() < deadline:
            errors = [l.strip()[-240:] for l in self._log_since(self.log_offset).splitlines() if _LUA_ERROR.search(l)]
            if errors:
                return False, {"lua_errors": errors[:5]}
            try:
                result = self.runner.companion.ask("status").get("result") or {}
            except runner.Refused as error:
                result = {"problems": [error.code]}
            game = result.get("game") or {}
            problems = [p for p in result.get("problems") or []
                        if p.startswith("GAME_") or p in ("MOD_BUILD_MISMATCH", "COMPANION_BUILD_UNSTAMPED")
                        or p.startswith("COMPANION_")]
            last = {"linked": game.get("linked"), "mod_build": game.get("mod_build"),
                    "companion_build": (result.get("companion") or {}).get("build"), "problems": problems}
            if (not problems and game.get("linked") is True and game.get("mod_build") == build
                    and last["companion_build"] == build):
                healthy += 1
                if healthy >= 2:
                    return True, last
            else:
                healthy = 0
            time.sleep(READY_SPACING_S)
        return False, {"timeout_s": READY_WAIT_S, **last}

    def reload(self) -> tuple[bool, str]:
        """Ask the running game to re-read the installed Lua; True once its log says
        all five files were reloaded. Nothing is pressed or focused."""
        marker, log = self.targets.settings / ".fhReload", self.targets.game_log
        offset = log.stat().st_size if log.is_file() else 0
        self.log_offset = offset                              # game_ready reads errors from here on
        marker.write_text("reload\n", encoding="ascii")
        deadline = time.monotonic() + RELOAD_CONSUME_S
        while marker.exists():
            if time.monotonic() > deadline:
                marker.unlink(missing_ok=True)
                return False, "marker not consumed (game not updating Farmhand?)"
            time.sleep(0.5)
        deadline = time.monotonic() + RELOAD_LOG_S
        while time.monotonic() < deadline:
            size = log.stat().st_size if log.is_file() else 0
            with open(log, "rb") as handle:
                handle.seek(offset if size >= offset else 0)
                text = handle.read().decode("utf-8", "replace")
            lines = [l for l in text.splitlines() if "fhReload:" in l]
            if lines:
                line = lines[-1].strip()
                ok = "fhReload: reloaded" in line and all(n in line for n in RELOADED) and "failed" not in line
                return ok, line[-300:]
            time.sleep(0.5)
        return False, "no fhReload line in the game log"


class Deployer:
    def __init__(self, targets: runner.Targets, host, state_dir: Path = STATE_DIR, say=print):
        self.targets, self.host, self.state_dir, self.say = targets, host, state_dir, say
        self.mod_dir, self.app_dir = targets.mod, targets.local_data / "app"
        for folder in ("builds", "snapshots"):
            (state_dir / folder).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- record keeping
    def _log(self, event: str, **detail) -> None:
        entry = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "event": event, **detail}
        with open(self.state_dir / "log.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        self.say(json.dumps(entry))

    def _active(self) -> dict | None:
        path = self.state_dir / "active.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _set_active(self, value: dict | None) -> None:
        path = self.state_dir / "active.json"
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(value), encoding="utf-8")

    def installed(self) -> Install:
        state = {}
        for name in STATE_FILES:
            path = self.targets.local_data / name
            state[name] = path.read_bytes() if path.is_file() else None
        return Install(read_tree(self.mod_dir), read_tree(self.app_dir), state)

    # ---------------------------------------------------------------- snapshots
    def snapshot(self, label: str) -> str:
        """Copy the installed deployment aside and prove the copy byte-identical."""
        install = self.installed()
        sid = f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{label}-{(build_of(install.app) or 'unstamped')[:12]}"
        folder = self.state_dir / "snapshots" / sid
        n = 1
        while folder.exists():
            n += 1
            folder = self.state_dir / "snapshots" / f"{sid}-{n}"
        write_tree(folder / "mod", install.mod)
        write_tree(folder / "app", install.app)
        for name, data in install.state.items():
            if data is not None:
                (folder / "state").mkdir(exist_ok=True)
                (folder / "state" / name).write_bytes(data)
        meta = {"id": folder.name, "label": label, "build": build_of(install.app), **install.digest()}
        (folder / "SNAPSHOT.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        if self.load_snapshot(folder.name).digest() != install.digest():
            raise DeployError("SNAPSHOT_COPY_MISMATCH", snapshot=folder.name)
        self._log("snapshot", snapshot=folder.name, build=meta["build"], files=len(install.mod) + len(install.app))
        return folder.name

    def load_snapshot(self, sid: str) -> Install:
        folder = self.state_dir / "snapshots" / sid
        meta = json.loads((folder / "SNAPSHOT.json").read_text(encoding="utf-8"))
        state = {name: ((folder / "state" / name).read_bytes() if meta["state"].get(name) else None)
                 for name in STATE_FILES}
        install = Install(read_tree(folder / "mod"), read_tree(folder / "app"), state)
        if install.digest() != {k: meta[k] for k in ("mod", "app", "state")}:
            raise DeployError("SNAPSHOT_CORRUPT", snapshot=sid)
        return install

    # ---------------------------------------------------------------- builds
    def keep_build(self, manifest: dict, contents: dict) -> str:
        build = manifest["build"]
        folder = self.state_dir / "builds" / build
        write_tree(folder, {**contents, "MANIFEST.json": json.dumps(manifest, indent=1).encode()})
        self.load_build(build)
        self._log("staged", build=build)
        return build

    def load_build(self, build: str) -> Install:
        folder = self.state_dir / "builds" / build
        manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
        files = read_tree(folder)
        files.pop("MANIFEST.json", None)
        if set(files) != set(runner.BUILD_FILES) or set(manifest["files"]) != set(runner.BUILD_FILES):
            raise DeployError("BUILD_FILES_NOT_ALLOWED", build=build)
        for name, data in files.items():
            if _sha(data) != manifest["files"][name]:
                raise DeployError("BUILD_HASH_MISMATCH", build=build, file=name)
        prefix = f"mod/{runner.MOD_NAME}/"
        return Install({k[len(prefix):]: v for k, v in files.items() if k.startswith(prefix)},
                       {k[4:]: v for k, v in files.items() if k.startswith("app/")}, {})

    def latest_build(self) -> str:
        builds = sorted((self.state_dir / "builds").iterdir(), key=lambda p: p.stat().st_mtime)
        if not builds:
            raise DeployError("NOTHING_STAGED", hint="run stage first")
        return builds[-1].name

    # ---------------------------------------------------------------- switching
    def _preflight(self) -> bool:
        if self.host.reload_pending():
            raise DeployError("RELOAD_PENDING")
        if self.host.job_running():
            raise DeployError("JOB_RUNNING", hint="wait for the test to end or STOP it")
        return self.host.fs25_running()

    def _write_state(self, state: dict) -> None:
        for name, data in state.items():
            path = self.targets.local_data / name
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)

    def _check(self, expected: Install, with_state: bool) -> None:
        """The build files must match exactly; the mutable state only before the companion
        starts (it may rewrite settings.json, goal.json and routes.json once running)."""
        now = self.installed().digest()
        want = expected.digest()
        for part in ("mod", "app") + (("state",) if with_state else ()):
            if now[part] != want[part]:
                raise DeployError("INSTALLED_MISMATCH", part=part,
                                  files=sorted(set(now[part].items()) ^ set(want[part].items()))[:6])

    def _apply(self, target: Install, running: bool, rollback: Install) -> dict:
        """Install target and verify it, all inside one transaction: any failure, the final
        verification included, puts rollback back and says whether that fully worked."""
        build = build_of(target.app)
        if build is None:
            raise DeployError("COMPANION_BUILD_UNSTAMPED")
        done = {"build": build, "reloaded": None}
        self.host.stop_companion()                            # nothing is changed before this succeeds
        try:
            write_tree(self.mod_dir, target.mod)
            if running:
                ok, line = self.host.reload()
                done["reloaded"] = line
                if not ok:
                    raise DeployError("RELOAD_FAILED", line=line)
            write_tree(self.app_dir, target.app)
            self._write_state(target.state)
            self._check(target, with_state=True)
            done["companion_pid"] = self.host.start_companion(build)
            if running:
                ok, seen = self.host.game_ready(build)
                done["game"] = seen
                if not ok:
                    raise DeployError("GAME_NOT_READY", **seen)
            self._check(target, with_state=False)
        except Exception as error:
            code = getattr(error, "code", type(error).__name__)
            self._log("apply_failed", build=build, code=code, detail=getattr(error, "detail", str(error)))
            back = self._put_back(rollback, running)
            raise DeployError("ROLLED_BACK" if back["ok"] else "ROLLBACK_INCOMPLETE",
                              failed=code, restored=back) from error
        return done

    def _put_back(self, previous: Install, running: bool) -> dict:
        """Every step is attempted; ok only when the files and state are identical again,
        the game reloaded them (if running) and the previous companion started."""
        result, problems = {"build": build_of(previous.app)}, []

        def step(name, action):
            try:
                return action()
            except Exception as error:
                problems.append(f"{name}: {getattr(error, 'code', error)}")
                return None

        step("stop", self.host.stop_companion)
        step("mod", lambda: write_tree(self.mod_dir, previous.mod))
        if running:
            reload = step("reload", self.host.reload)
            result["reload"] = reload
            if reload is not None and not reload[0]:
                problems.append(f"reload: {reload[1]}")
        step("app", lambda: write_tree(self.app_dir, previous.app))
        step("state", lambda: self._write_state(previous.state))
        step("verify", lambda: self._check(previous, with_state=True))
        result["companion_pid"] = step("start", lambda: self.host.start_companion(result["build"]))
        if running and result["companion_pid"] is not None:
            ready = step("game", lambda: self.host.game_ready(result["build"]))
            result["game"] = ready
            if ready is not None and not ready[0]:
                problems.append(f"game: {ready[1]}")
        result["problems"], result["ok"] = problems, not problems
        self._log("put_back", **result)
        return result

    def switch(self, build: str | None = None) -> dict:
        build = build or self.latest_build()
        target = self.load_build(build)
        running = self._preflight()
        active = self._active()
        if active is None:
            baseline = self.snapshot("baseline")             # the lab's install, before anything changes
        else:
            baseline = active["baseline"]
            self.snapshot("pretest")                          # the previous test build's state, kept too
        current = self.installed()
        target.state = dict(current.state)                    # the player's settings, goal and routes carry over
        # Recorded first: whatever happens next, restore knows the way back to the baseline.
        self._set_active({"baseline": baseline, "build": build, "pending": True})
        try:
            done = self._apply(target, running, rollback=current)
        except DeployError as error:
            if error.code == "ROLLED_BACK":
                self._set_active(active)                       # exactly as before this switch
            else:
                self._set_active({"baseline": baseline, "build": build, "incomplete": error.code})
            raise
        self._set_active({"baseline": baseline, "build": build})
        self._log("switched", baseline=baseline, fs25_running=running, **done)
        return done

    def restore(self, sid: str | None = None) -> dict:
        active = self._active()
        sid = sid or (active or {}).get("baseline")
        if sid is None:
            raise DeployError("NOTHING_TO_RESTORE", hint="no switch is active; name a snapshot")
        baseline = self.load_snapshot(sid)
        running = self._preflight()
        self.snapshot("posttest")                             # what the test build left, kept for evidence
        current = self.installed()
        try:
            done = self._apply(baseline, running, rollback=current)
        except DeployError as error:
            if error.code != "ROLLED_BACK" and active is not None:
                self._set_active({**active, "incomplete": error.code})
            raise
        self._set_active(None)
        self._log("restored", snapshot=sid, identical=True, fs25_running=running, **done)
        return {**done, "snapshot": sid, "identical": True}

    def status(self) -> dict:
        install, active = self.installed(), self._active()
        result = {"installed_build": build_of(install.app), "active": active,
                  "staged": sorted(p.name for p in (self.state_dir / "builds").iterdir()),
                  "snapshots": sorted(p.name for p in (self.state_dir / "snapshots").iterdir())[-6:]}
        if active:
            base = self.load_snapshot(active["baseline"]).digest()
            result["baseline_intact"] = True
            result["installed_is_baseline"] = install.digest() == base
        return result


# ---------------------------------------------------------------- the round trip, rehearsed
class RehearsalHost:
    """A stand-in for the live side: no game, no companion; can make a reload fail."""

    def __init__(self, fail_reload: bool = False, running: bool = True):
        self.fail_reload, self.running, self.events = fail_reload, running, []

    def fs25_running(self): return self.running
    def job_running(self): return False
    def reload_pending(self): return False
    def stop_companion(self): self.events.append("stop")

    def start_companion(self, build):
        self.events.append(f"start {build}")
        return 1

    def game_ready(self, build):
        self.events.append(f"ready {build}")
        return True, {"mod_build": build}

    def reload(self):
        self.events.append("reload")
        if self.fail_reload:
            self.fail_reload = False                          # the put-back reload works
            return False, "fhReload: FarmhandDrive.lua failed (rehearsal)"
        return True, "fhReload: reloaded " + ", ".join(RELOADED)


def rehearse(base: Path, say=print) -> dict:
    """snapshot -> switch -> restore on a fake profile holding a dirty lab install with
    extra files, then a failing reload; every byte of the lab install must come back."""
    profile, data = base / "profile", base / "data"
    targets = runner.Targets(str(profile), str(profile), str(data), local=lambda p: Path(p))
    lab_mod = {n: f"-- lab {n}\n".encode() for n in runner.MOD_FILES} | {"notes/lab-only.txt": b"dirty lab file\n"}
    lab_app = {n: f"# lab {n}\n".encode() for n in runner.APP_FILES} | {"companion/lab_extra.py": b"# lab only\n"}
    lab_app["companion/buildinfo.py"] = b'BUILD = "2f3d1f6-dirty"\n'
    write_tree(targets.mod, lab_mod)
    write_tree(data / "app", lab_app)
    (data / "app" / "companion" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (data / "app" / "companion" / "__pycache__" / "x.pyc").write_bytes(b"cache")
    (data / "settings.json").write_text('{"pay": 10}', encoding="utf-8")
    (data / "routes.json").write_text('{"lab": 1}', encoding="utf-8")
    contents = {f"mod/{runner.MOD_NAME}/{n}": f"-- prod {n}\n".encode() for n in runner.MOD_FILES}
    contents |= {f"app/{n}": f"# prod {n}\n".encode() for n in runner.APP_FILES}
    contents["app/companion/buildinfo.py"] = b'BUILD = "abc1234"\n'
    manifest = {"version": 1, "build": "abc1234", "files": {k: _sha(v) for k, v in contents.items()}}
    lab = Deployer(targets, RehearsalHost(), base / "state", say=lambda *_: None)
    before = lab.installed().digest()
    lab.keep_build(manifest, contents)
    lab.switch("abc1234")
    checks = {"switched": build_of(lab.installed().app) == "abc1234",
              "settings_carried": (data / "settings.json").read_text() == '{"pay": 10}',
              "lab_extra_removed": not (targets.mod / "notes" / "lab-only.txt").exists()}
    (data / "routes.json").write_text('{"prod": 2}', encoding="utf-8")   # the test build learns a route
    lab.restore()
    checks["restored_identical"] = lab.installed().digest() == before
    failing = Deployer(targets, RehearsalHost(fail_reload=True), base / "state", say=lambda *_: None)
    try:
        failing.switch("abc1234")
        checks["failed_reload_rolled_back"] = False
    except DeployError as error:
        checks["failed_reload_rolled_back"] = (error.code == "ROLLED_BACK"
                                               and failing.installed().digest() == before
                                               and failing._active() is None)
    checks["ok"] = all(checks.values())
    say(json.dumps(checks))
    return checks


# ---------------------------------------------------------------- command line
def stage(targets: runner.Targets, allow_dirty: bool) -> Deployer:
    if ROOT.resolve() == LAB_CHECKOUT.resolve():
        raise DeployError("LAB_CHECKOUT", hint="clone the production branch elsewhere; never build in the lab")
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.strip()
    if dirty and not allow_dirty:
        raise DeployError("CLONE_DIRTY", hint="commit or pass --allow-dirty")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "package_probe.py"), "--build-dev",
                    str(ROOT / runner.SHARED / runner.BUILD_DIR)], check=True, stdout=subprocess.DEVNULL)
    host = Host(targets)
    manifest, contents = host.runner._validated_build()
    deployer = Deployer(targets, host)
    deployer.keep_build(manifest, contents)
    return deployer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=runner.FS25_PROFILE)
    parser.add_argument("--data", default=runner.FARMHAND_DATA)
    sub = parser.add_subparsers(dest="action", required=True)
    s = sub.add_parser("stage")
    s.add_argument("--allow-dirty", action="store_true")
    sub.add_parser("status")
    sub.add_parser("snapshot")
    w = sub.add_parser("switch")
    w.add_argument("build", nargs="?")
    r = sub.add_parser("restore")
    r.add_argument("snapshot", nargs="?")
    sub.add_parser("selftest")
    args = parser.parse_args(argv)
    try:
        if args.action == "selftest":
            with tempfile.TemporaryDirectory(prefix="fh-deploy-") as base:
                return 0 if rehearse(Path(base))["ok"] else 1
        targets = runner.Targets(runner.FS25_EXE, args.profile, args.data)
        if args.action == "stage":
            stage(targets, args.allow_dirty)
            return 0
        deployer = Deployer(targets, Host(targets))
        result = {"status": deployer.status, "snapshot": lambda: deployer.snapshot("manual"),
                  "switch": lambda: deployer.switch(args.build),
                  "restore": lambda: deployer.restore(args.snapshot)}[args.action]()
        print(json.dumps(result, indent=1, default=str))
        return 0
    except (DeployError, runner.Refused) as error:
        print(json.dumps({"refused": error.code, **error.detail}, default=str), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
