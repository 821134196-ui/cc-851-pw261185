from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from backup_chain.core import BackupChain
from backup_chain.demo import seed_demo
from backup_chain.planner import (
    DeletionBlocked,
    apply_prune,
    build_timeline,
    delete_segment,
    deletion_plan,
)
from backup_chain.server import BackupServer


class BackupChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        self.repo = seed_demo(self.root, base_ts=1_000)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def rows(self, result):
        return {row["id"]: row for row in result["rows"]}

    def test_second_level_restore_shows_actual_row_versions(self):
        at_full = self.repo.restore(1_000)
        self.assertTrue(at_full["ok"])
        self.assertEqual(3, at_full["row_count"])
        self.assertEqual("free", self.rows(at_full)["A-1001"]["plan"])

        at_1003 = self.repo.restore(1_003)
        self.assertTrue(at_1003["ok"])
        rows = self.rows(at_1003)
        self.assertEqual(4, len(rows))
        self.assertEqual("pro", rows["A-1001"]["plan"])
        self.assertEqual(120, rows["A-1001"]["balance"])
        self.assertIn("A-1004", rows)

        at_1004 = self.repo.restore(1_004)
        self.assertTrue(at_1004["ok"])
        self.assertNotIn("A-1003", self.rows(at_1004))

        latest = self.repo.restore(1_010)
        self.assertTrue(latest["ok"])
        latest_rows = self.rows(latest)
        self.assertEqual(4, len(latest_rows))
        self.assertEqual("enterprise", latest_rows["A-1001"]["plan"])
        self.assertEqual(180, latest_rows["A-1001"]["balance"])
        self.assertEqual(480, latest_rows["A-1005"]["balance"])

    def test_deletion_plan_lists_exact_dependent_seconds(self):
        plan = deletion_plan(self.repo, 1)
        self.assertEqual(6, plan["dependent_point_count"])
        self.assertEqual([{"from": 1_001, "to": 1_006}], [
            {"from": r["from"], "to": r["to"]} for r in plan["dependent_ranges"]
        ])
        self.assertFalse(plan["safe_without_retention"])
        self.assertEqual(6, len(plan["recovery_points"]))

        retained = deletion_plan(self.repo, 1, retain_from=1_007)
        self.assertEqual(0, retained["retained_dependent_point_count"])
        self.assertTrue(retained["delete_allowed_under_retention"])

        segment_two = deletion_plan(self.repo, 2, retain_from=1_007)
        self.assertEqual(3, segment_two["dependent_point_count"])
        self.assertEqual(0, segment_two["retained_dependent_point_count"])

    def test_retention_delete_preserves_points_after_new_full(self):
        result = apply_prune(self.repo, 1_007)
        deleted_seqs = {item["segment_seq"] for item in result["deleted"]}
        self.assertEqual({1, 2}, deleted_seqs)

        before_segment = self.repo.restore(1_000)
        self.assertTrue(before_segment["ok"])
        self.assertEqual(3, before_segment["row_count"])

        broken = self.repo.restore(1_002)
        self.assertFalse(broken["ok"])
        self.assertEqual("SEGMENT_DELETED", broken["error"]["code"])
        self.assertEqual(1_000, broken["last_trusted"]["at"])
        self.assertEqual(3, broken["row_count"])

        after_full = self.repo.restore(1_008)
        self.assertTrue(after_full["ok"])
        self.assertEqual("full-000002", after_full["base_full"]["id"])
        self.assertEqual(3, after_full["row_count"])

        timeline = build_timeline(self.repo)
        deleted_spans = [s for s in timeline["spans"] if not s["ok"]]
        self.assertEqual(
            [(1_001, 1_006, "SEGMENT_DELETED", "seg-000001")],
            [
                (s["from"], s["to"], s["error_code"], s.get("error_segment_id"))
                for s in deleted_spans
            ],
        )
        # Historical structural dependencies remain visible after deletion.
        deleted_plan = deletion_plan(self.repo, 1)
        self.assertEqual(6, deleted_plan["dependent_point_count"])
        self.assertTrue(deleted_plan["already_deleted"])

    def test_missing_segment_stops_at_last_verified_prefix(self):
        self.repo.hide_segment(2)

        before_missing = self.repo.restore(1_003)
        self.assertTrue(before_missing["ok"])
        self.assertEqual(4, before_missing["row_count"])

        result = self.repo.restore(1_005)
        self.assertFalse(result["ok"])
        self.assertEqual("MISSING_SEGMENT", result["error"]["code"])
        self.assertEqual(1_003, result["last_trusted"]["at"])
        self.assertEqual(3, result["last_trusted"]["lsn"])
        rows = self.rows(result)
        self.assertIn("A-1004", rows)
        self.assertEqual("free", rows["A-1004"]["plan"])
        self.assertEqual(["seg-000001"], [s["id"] for s in result["used_segments"]])

    def test_corrupt_segment_stops_instead_of_reading_later_segments(self):
        self.repo.inject_corruption(segment_seq=1, mode="checksum")
        result = self.repo.restore(1_006)
        self.assertFalse(result["ok"])
        self.assertEqual("CHECKSUM_MISMATCH", result["error"]["code"])
        self.assertEqual("seg-000001", result["error"]["segment_id"])
        self.assertEqual(1_003, result["last_trusted"]["at"])
        self.assertEqual(3, result["last_trusted"]["lsn"])
        self.assertEqual(4, result["row_count"])
        self.assertEqual(["seg-000001"], [s["id"] for s in result["used_segments"]])

        # The newer verified full is self-contained and bypasses the old fault.
        after_new_full = self.repo.restore(1_010)
        self.assertTrue(after_new_full["ok"])
        self.assertEqual("full-000002", after_new_full["base_full"]["id"])

    def test_overlapping_segment_is_rejected(self):
        self.repo.inject_overlap(target_seq=2, source_seq=1)
        result = self.repo.restore(1_005)
        self.assertFalse(result["ok"])
        self.assertEqual("OVERLAPPING_SEGMENT", result["error"]["code"])
        self.assertEqual(1, result["error"]["record_lsn"])
        self.assertEqual(4, result["error"]["expected_lsn"])
        self.assertEqual(1_003, result["last_trusted"]["at"])
        self.assertEqual(4, result["row_count"])

        at_overlap_boundary = self.repo.restore(1_003)
        self.assertFalse(at_overlap_boundary["ok"])
        self.assertEqual("OVERLAPPING_SEGMENT", at_overlap_boundary["error"]["code"])

        early = self.repo.restore(1_002)
        self.assertTrue(early["ok"])

    def test_timeline_and_structural_plan_survive_missing_fault(self):
        self.repo.hide_segment(2)

        timeline = build_timeline(self.repo)
        missing_spans = [
            span for span in timeline["spans"] if span.get("error_code") == "MISSING_SEGMENT"
        ]
        self.assertEqual(
            [(1_004, 1_006, "seg-000002")],
            [
                (span["from"], span["to"], span.get("error_segment_id"))
                for span in missing_spans
            ],
        )

        # Dependency planning is structural, not derived from how far the
        # currently faulty replay happened to progress.
        plan = deletion_plan(self.repo, 2)
        self.assertEqual(3, plan["dependent_point_count"])
        self.assertEqual([{"from": 1_004, "to": 1_006}], [
            {"from": r["from"], "to": r["to"]} for r in plan["dependent_ranges"]
        ])
        self.assertFalse(plan["safe_without_retention"])

        self.repo.inject_corruption(segment_seq=1, mode="checksum")
        plan_after_earlier_fault = deletion_plan(self.repo, 2)
        self.assertEqual(3, plan_after_earlier_fault["dependent_point_count"])

    def test_delete_after_hide_removes_hidden_file(self):
        hidden = self.root / "segments" / "seg-000002.jsonl.hidden"
        self.repo.hide_segment(2)
        self.assertTrue(hidden.exists())

        delete_segment(self.repo, 2, retain_from=1_007)
        self.assertFalse(hidden.exists())

    def test_deletion_force_and_active_segment_gates(self):
        with self.assertRaises(DeletionBlocked):
            delete_segment(self.repo, 1)
        with self.assertRaises(DeletionBlocked):
            delete_segment(self.repo, 1, force=True, acknowledge_dependents=False)

        forced = delete_segment(
            self.repo, 1, force=True, acknowledge_dependents=True
        )
        self.assertTrue(forced["deleted"])
        again = delete_segment(self.repo, 1)
        self.assertFalse(again["deleted"])
        self.assertTrue(again["already_deleted"])

        with self.assertRaises(DeletionBlocked):
            delete_segment(
                self.repo, 3, force=True, acknowledge_dependents=True
            )

    def test_deletion_plan_point_limit_truncates_points(self):
        plan = deletion_plan(self.repo, 1, point_limit=2)
        self.assertTrue(plan["points_truncated"])
        self.assertIsNone(plan["recovery_points"])
        self.assertEqual(6, plan["dependent_point_count"])

    def test_empty_rolled_segment_can_be_deleted_without_breaking_restore(self):
        empty_root = Path(self._tmp.name) / "empty-repo"
        empty_repo = BackupChain.initialize(empty_root, created_at=1_000)
        empty_repo.create_full(ts=1_000, rows=[{"id": "A-1"}])
        empty_repo.roll_segment()  # Seal the provably empty seg-000001.

        plan = deletion_plan(empty_repo, 1)
        self.assertEqual(0, plan["dependent_point_count"])
        self.assertTrue(plan["safe_without_retention"])

        result = delete_segment(empty_repo, 1)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["record"]["empty_verified"])

        restored = empty_repo.restore(1_000)
        self.assertTrue(restored["ok"], restored.get("error"))
        self.assertEqual(1, restored["row_count"])

    def test_unregistered_segment_file_can_be_deleted_without_500(self):
        orphan = self.root / "segments" / "seg-000009.jsonl"
        orphan.write_text("", encoding="utf-8")

        plan = deletion_plan(self.repo, 9)
        self.assertTrue(plan["status"]["unregistered"])
        self.assertTrue(plan["safe_without_retention"])

        result = delete_segment(self.repo, 9)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["unregistered"])
        self.assertFalse(orphan.exists())

    def test_later_full_rejects_caller_supplied_rows(self):
        with self.assertRaises(ValueError):
            self.repo.create_full(ts=1_011, rows=[{"id": "EVIL"}])

        manifest = self.repo.manifest()
        self.assertEqual(2, len(manifest["fulls"]))
        self.assertEqual(3, manifest["next_full_seq"])

    def test_http_timeline_and_actual_rows(self):
        server = BackupServer(("127.0.0.1", 0), self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(base + "/api/timeline", timeout=5) as response:
                timeline_payload = json.load(response)
            self.assertTrue(timeline_payload["ok"])
            self.assertEqual(11, timeline_payload["timeline"]["seconds"])

            url = base + "/api/restore?at=1010"
            with urllib.request.urlopen(url, timeout=5) as response:
                restore_payload = json.load(response)
            self.assertTrue(restore_payload["ok"])
            self.assertEqual(4, restore_payload["result"]["row_count"])

            with urllib.request.urlopen(base + "/", timeout=5) as response:
                html = response.read().decode("utf-8")
            self.assertIn("秒级恢复点", html)

            bad_request = urllib.request.Request(
                base + "/api/faults/hide",
                data=json.dumps({"segment": None}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as error_context:
                urllib.request.urlopen(bad_request, timeout=5)
            self.assertEqual(400, error_context.exception.code)

            evil_full = urllib.request.Request(
                base + "/api/fulls",
                data=json.dumps({"ts": 1_011, "rows": [{"id": "EVIL"}]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as full_error:
                urllib.request.urlopen(evil_full, timeout=5)
            self.assertEqual(400, full_error.exception.code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
