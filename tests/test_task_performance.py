"""Offline task-latency baseline; no live-game requests."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.task_performance import build_report, comparable, record_metrics, stats


def example(status="COMPLETE", command="move_pair", duration=12.0):
    return {
        "started": "2026-11-04 08:00:00.000",
        "started_unix": 1793797200.0,
        "ended_unix": 1793797200.0 + duration,
        "status": status,
        "proposal": {"command": command},
        "configuration": {"map": "TestMap", "savegame": 2},
        "companion_build": "build-a",
        "companion_log": [
            "2026-11-04 08:00:00,500 notice 1 ACCEPTED_MOVE",
            "2026-11-04 08:00:01,250 command id=1 op=pay {'direction':0}",
            "2026-11-04 08:00:02,250 command id=2 op=farm_drive {'tractor':1}",
            "2026-11-04 08:00:10,250 result id=2 op=farm_drive status=DONE code=ARRIVED",
        ],
    }


class TaskPerformanceTests(unittest.TestCase):
    def test_dispatch_is_not_marked_physical_motion(self):
        row = record_metrics(example())
        self.assertEqual(row["terminal_s"], 12)
        self.assertEqual(row["first_command_dispatch_s"], 1.25)
        self.assertEqual(row["first_work_command_dispatch_s"], 2.25)
        self.assertIsNone(row["first_verified_physical_progress_s"])

    def test_failed_and_unverified_are_not_claimed_complete(self):
        result = stats([record_metrics(example()), record_metrics(example(status="FAILED", duration=40))])
        self.assertEqual(result["jobs"], 2)
        self.assertEqual(result["complete"], 1)
        self.assertEqual(result["terminal_complete"]["p50_s"], 12)
        self.assertEqual(result["first_verified_physical_progress"]["count"], 0)

    def test_ignores_incomplete_and_respects_map_save_and_time(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for idx, start in enumerate(["2026-11-04 08:00:00.000", "2026-11-03 08:00:00.000"]):
                d = root / str(idx)
                d.mkdir()
                sample = example()
                sample["started"] = start
                (d / "record.json").write_text(json.dumps(sample))
            extra = root / "incomplete"
            extra.mkdir()
            sample = example()
            sample["ended_unix"] = None
            (extra / "record.json").write_text(json.dumps(sample))
            result = build_report(root, "2026-11-04")
            self.assertEqual(result["overall"]["jobs"], 1)
            self.assertEqual(result["cohorts"][0]["map"], "TestMap")
            self.assertEqual(result["cohorts"][0]["save"], "2")

    def test_comparison_refuses_small_or_unmatched_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder) / "sample"
            d.mkdir()
            (d / "record.json").write_text(json.dumps(example()))
            report = build_report(Path(folder))
            self.assertEqual(comparable(report, report), [])

    def test_live_movement_observer_uses_reserved_real_pose_delta(self):
        from companion.livetest import LiveTest
        from threading import Lock
        from types import SimpleNamespace

        class StubWorld:
            _lock = Lock()
            _reservations = {"job-1-sequence": ("1", ("11",))}
            point = (12.0, 4.0)

            def snapshot(self):
                return (SimpleNamespace(id="11", position=self.point),)

        world = StubWorld()
        test = SimpleNamespace(world=world, clock=lambda: 110.0)
        job = SimpleNamespace(owner="job-1")
        origins = {}
        self.assertIsNone(LiveTest._first_equipment_movement(test, job, origins, 100.0))
        world.point = (12.2, 4.0)
        self.assertIsNone(LiveTest._first_equipment_movement(test, job, origins, 100.0))
        world.point = (12.6, 4.0)
        got = LiveTest._first_equipment_movement(test, job, origins, 100.0)
        self.assertEqual(got["elapsed_s"], 10.0)
        self.assertEqual(got["equipment_id"], "11")
        self.assertEqual(got["displacement_m"], 0.6)

    def test_record_uses_only_verified_movement_evidence(self):
        before = example()
        self.assertIsNone(record_metrics(before)["first_verified_equipment_movement_s"])
        before["first_verified_equipment_movement"] = {"elapsed_s": 3.6,
                                                       "source": "live FS25 equipment snapshots"}
        row = record_metrics(before)
        self.assertEqual(row["first_verified_equipment_movement_s"], 3.6)


if __name__ == "__main__":
    unittest.main()
