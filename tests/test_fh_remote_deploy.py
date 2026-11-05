"""scripts/fh_remote_deploy.py: the gateway's only deployment interface stays narrow and off by default."""
import fcntl
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fh_remote_deploy as remote  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class RemoteDeployTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="fh-remote-"))
        self.saved = {k: getattr(remote, k) for k in ("CLONE", "STATE", "TOOL", "ENABLED", "LOCK", "AUDIT")}
        remote.STATE = base / "state"
        remote.TOOL, remote.ENABLED = remote.STATE / "tool", remote.STATE / "remote_deploy.enabled"
        remote.LOCK, remote.AUDIT = remote.STATE / "remote_deploy.lock", remote.STATE / "remote.jsonl"
        origin, clone = base / "origin.git", base / "clone"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            git(clone, "config", k, v)
        (clone / "f.txt").write_text("a\n")
        git(clone, "add", "f.txt")
        git(clone, "commit", "-q", "-m", "a")
        git(clone, "checkout", "-q", "-b", remote.BRANCH)
        git(clone, "remote", "add", "origin", str(origin))
        git(clone, "push", "-q", "origin", remote.BRANCH)
        self.on_branch = git(clone, "rev-parse", "HEAD")
        git(clone, "checkout", "-q", "-b", "other")
        (clone / "f.txt").write_text("b\n")
        git(clone, "commit", "-q", "-am", "b")
        self.off_branch = git(clone, "rev-parse", "HEAD")
        git(clone, "checkout", "-q", remote.BRANCH)
        remote.CLONE = clone
        self.deploys = []
        self.real_deploy = remote._deploy
        remote._deploy = lambda *args: self.deploys.append(args) or {"args": list(args)}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(remote, k, v)
        remote._deploy = self.real_deploy

    def enable(self):
        remote.STATE.mkdir(parents=True, exist_ok=True)
        remote.ENABLED.write_text("yes\n")

    def test_off_until_the_owner_enables_it_status_still_answers(self):
        self.assertEqual(remote.handle({"action": "switch", "commit": self.on_branch})["refused"],
                         "REMOTE_DEPLOY_DISABLED")
        self.assertEqual(remote.handle({"action": "restore"})["refused"], "REMOTE_DEPLOY_DISABLED")
        answer = remote.handle({"action": "status"})
        self.assertTrue(answer["ok"])
        self.assertFalse(answer["enabled"])
        self.assertEqual(self.deploys, [("status",)])

    def test_only_the_two_fields_and_the_three_actions(self):
        self.enable()
        for request, code in (({"action": "shell", "commit": "abc1234"}, "UNKNOWN_ACTION"),
                              ({"action": "switch"}, "COMMIT_REQUIRED"),
                              ({"action": "switch", "commit": "HEAD; rm -rf ~"}, "COMMIT_REQUIRED"),
                              ({"action": "switch", "commit": "origin/main"}, "COMMIT_REQUIRED"),
                              ({"action": "switch", "commit": self.on_branch, "path": "/"}, "UNEXPECTED_FIELDS"),
                              ({"action": "restore", "commit": self.on_branch}, "UNEXPECTED_FIELDS")):
            self.assertEqual(remote.handle(request).get("refused"), code, request)
        self.assertEqual(self.deploys, [])

    def test_a_commit_off_the_production_branch_is_refused_unchanged(self):
        self.enable()
        answer = remote.handle({"action": "switch", "commit": self.off_branch})
        self.assertEqual(answer["refused"], "NOT_ON_PRODUCTION_BRANCH")
        self.assertEqual(self.deploys, [])
        self.assertEqual(git(remote.CLONE, "rev-parse", "HEAD"), self.on_branch)

    def test_a_branch_commit_is_checked_out_then_staged_and_switched(self):
        self.enable()
        answer = remote.handle({"action": "switch", "commit": self.on_branch[:9]})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["commit"], self.on_branch)
        self.assertEqual(self.deploys, [("stage",), ("switch",)])
        audit = [json.loads(l) for l in remote.AUDIT.read_text().splitlines()]
        self.assertEqual(audit[-1]["action"], "switch")
        self.assertTrue(audit[-1]["ok"])

    def test_a_dirty_clone_or_a_second_request_is_refused(self):
        self.enable()
        (remote.CLONE / "f.txt").write_text("edited\n")
        self.assertEqual(remote.handle({"action": "switch", "commit": self.on_branch})["refused"], "CLONE_DIRTY")
        git(remote.CLONE, "checkout", "--", "f.txt")
        with open(remote.LOCK, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(remote.handle({"action": "restore"})["refused"], "DEPLOY_BUSY")
        self.assertEqual(self.deploys, [])

    def test_the_tools_run_only_as_pinned(self):
        remote._deploy = self.real_deploy
        remote.TOOL.mkdir(parents=True, exist_ok=True)
        here = Path(remote.__file__).resolve().parent
        cwd = Path.cwd()
        import os
        os.chdir(here)
        try:
            remote.install()
        finally:
            os.chdir(cwd)
        for name in remote.PINNED:
            self.assertTrue((remote.TOOL / name).is_file())
        (remote.TOOL / "fh_deploy.py").write_text("print('tampered')\n")
        with self.assertRaises(remote.Refused) as caught:
            remote._deploy("status")
        self.assertEqual(caught.exception.code, "TOOL_NOT_PINNED")


if __name__ == "__main__":
    unittest.main()
