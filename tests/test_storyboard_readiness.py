from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.server import storyboard_readiness


class StoryboardReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "storyboard-readiness.db")
        self.sequence = 0

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_project(self, *, scanned: bool = False) -> int:
        self.sequence += 1
        last_scan_at = "2026-07-16 10:00:00" if scanned else None
        with self.db.session() as conn:
            cursor = conn.execute(
                """INSERT INTO projects(name, root_path, smb_url, last_scan_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    f"项目 {self.sequence}",
                    f"/tmp/storyboard-readiness-{self.sequence}",
                    f"smb://nas/storyboard-readiness-{self.sequence}",
                    last_scan_at,
                ),
            )
            return int(cursor.lastrowid)

    def create_asset(
        self,
        project_id: int,
        *,
        status: str = "active",
        analysis_status: str = "pending",
        thumb_status: str = "pending",
        exempted: bool = False,
        exemption_fingerprint: str | None = None,
    ) -> int:
        self.sequence += 1
        fingerprint = f"fingerprint-{self.sequence}"
        exempted_at = "2026-07-16 10:30:00" if exempted else None
        exempted_fingerprint = (
            exemption_fingerprint
            if exemption_fingerprint is not None
            else (fingerprint if exempted else None)
        )
        with self.db.session() as conn:
            cursor = conn.execute(
                """INSERT INTO assets(
                     project_id, relative_path, filename, extension, media_type,
                     size_bytes, mtime, content_fingerprint, status,
                     analysis_status, thumb_status, analysis_exempted_at,
                     analysis_exempt_reason, analysis_exempt_fingerprint
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    f"素材-{self.sequence}.mp4",
                    f"素材-{self.sequence}.mp4",
                    ".mp4",
                    "video",
                    1024,
                    float(self.sequence),
                    fingerprint,
                    status,
                    analysis_status,
                    thumb_status,
                    exempted_at,
                    "文件损坏" if exempted else None,
                    exempted_fingerprint,
                ),
            )
            return int(cursor.lastrowid)

    def create_task(self, project_id: int, task_type: str, status: str) -> int:
        with self.db.session() as conn:
            cursor = conn.execute(
                """INSERT INTO tasks(project_id, task_type, status)
                   VALUES (?, ?, ?)""",
                (project_id, task_type, status),
            )
            return int(cursor.lastrowid)

    def readiness(self, project_id: int):
        with self.db.session() as conn:
            return storyboard_readiness(conn, project_id)

    def test_missing_project_returns_none(self):
        self.assertIsNone(self.readiness(999999))

    def test_project_must_complete_its_first_scan(self):
        project_id = self.create_project(scanned=False)

        result = self.readiness(project_id)

        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "scan_required")
        self.assertFalse(result["requirements"]["scan_completed"])
        self.assertEqual(
            [blocker["code"] for blocker in result["blockers"]],
            ["scan_required"],
        )

    def test_scanned_project_without_active_assets_is_blocked(self):
        project_id = self.create_project(scanned=True)

        result = self.readiness(project_id)

        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "no_assets")
        self.assertEqual(result["counts"]["active"], 0)
        self.assertFalse(result["requirements"]["has_assets"])
        self.assertIn("no_assets", {item["code"] for item in result["blockers"]})

    def test_all_active_assets_completed_is_ready(self):
        project_id = self.create_project(scanned=True)
        self.create_asset(project_id, analysis_status="completed")
        self.create_asset(project_id, analysis_status="completed")

        result = self.readiness(project_id)

        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["counts"]["active"], 2)
        self.assertEqual(result["counts"]["completed"], 2)
        self.assertEqual(result["counts"]["remaining"], 0)
        self.assertEqual(result["blockers"], [])
        self.assertTrue(all(result["requirements"].values()))

    def test_missing_pending_asset_is_ignored(self):
        project_id = self.create_project(scanned=True)
        self.create_asset(project_id, analysis_status="completed")
        self.create_asset(project_id, status="missing", analysis_status="pending")

        result = self.readiness(project_id)

        self.assertTrue(result["ready"])
        self.assertEqual(result["counts"]["active"], 1)
        self.assertEqual(result["counts"]["pending"], 0)
        self.assertEqual(result["counts"]["remaining"], 0)

    def test_valid_failed_exemption_allows_ready_with_real_analysis(self):
        project_id = self.create_project(scanned=True)
        self.create_asset(project_id, analysis_status="completed")
        self.create_asset(
            project_id, analysis_status="failed", exempted=True
        )

        result = self.readiness(project_id)

        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["counts"]["active"], 2)
        self.assertEqual(result["counts"]["completed"], 1)
        self.assertEqual(result["counts"]["exempted"], 1)
        self.assertEqual(result["counts"]["failed"], 0)
        self.assertEqual(result["counts"]["required"], 1)
        self.assertEqual(result["counts"]["remaining"], 0)
        self.assertEqual(result["blockers"], [])

    def test_stale_exemption_fingerprint_still_blocks(self):
        project_id = self.create_project(scanned=True)
        self.create_asset(project_id, analysis_status="completed")
        self.create_asset(
            project_id,
            analysis_status="failed",
            exempted=True,
            exemption_fingerprint="stale-fingerprint",
        )

        result = self.readiness(project_id)

        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "analysis_failed")
        self.assertEqual(result["counts"]["exempted"], 0)
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertEqual(result["counts"]["remaining"], 1)
        self.assertIn(
            "analysis_failed", {item["code"] for item in result["blockers"]}
        )

    def test_all_active_assets_exempted_still_requires_one_real_analysis(self):
        project_id = self.create_project(scanned=True)
        self.create_asset(project_id, analysis_status="failed", exempted=True)
        self.create_asset(project_id, analysis_status="failed", exempted=True)

        result = self.readiness(project_id)

        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "no_analyzed_assets")
        self.assertEqual(result["counts"]["active"], 2)
        self.assertEqual(result["counts"]["completed"], 0)
        self.assertEqual(result["counts"]["exempted"], 2)
        self.assertEqual(result["counts"]["required"], 0)
        self.assertEqual(result["counts"]["remaining"], 0)
        self.assertIn(
            "no_analyzed_assets", {item["code"] for item in result["blockers"]}
        )

    def test_missing_exempted_asset_is_ignored(self):
        project_id = self.create_project(scanned=True)
        self.create_asset(project_id, analysis_status="completed")
        self.create_asset(
            project_id,
            status="missing",
            analysis_status="failed",
            exempted=True,
        )

        result = self.readiness(project_id)

        self.assertTrue(result["ready"])
        self.assertEqual(result["counts"]["active"], 1)
        self.assertEqual(result["counts"]["completed"], 1)
        self.assertEqual(result["counts"]["exempted"], 0)
        self.assertEqual(result["counts"]["failed"], 0)

    def test_each_incomplete_or_unknown_active_analysis_status_blocks(self):
        expectations = {
            "pending": ("analysis_required", "analysis_required", "pending"),
            "processing": ("analysis_required", "analysis_required", "processing"),
            "failed": ("analysis_failed", "analysis_failed", "failed"),
            "unexpected": ("analysis_required", "analysis_required", "other"),
        }
        for analysis_status, (state, blocker, count_key) in expectations.items():
            with self.subTest(analysis_status=analysis_status):
                project_id = self.create_project(scanned=True)
                self.create_asset(project_id, analysis_status=analysis_status)

                result = self.readiness(project_id)

                self.assertFalse(result["ready"])
                self.assertEqual(result["state"], state)
                self.assertEqual(result["counts"]["active"], 1)
                self.assertEqual(result["counts"]["completed"], 0)
                self.assertEqual(result["counts"]["remaining"], 1)
                self.assertEqual(result["counts"][count_key], 1)
                self.assertIn(
                    blocker,
                    {item["code"] for item in result["blockers"]},
                )

    def test_each_active_scan_and_analysis_task_status_blocks(self):
        for task_type in ("scan", "analysis"):
            for status in ("queued", "running", "pausing", "paused"):
                with self.subTest(task_type=task_type, status=status):
                    project_id = self.create_project(scanned=True)
                    self.create_asset(project_id, analysis_status="completed")
                    task_id = self.create_task(project_id, task_type, status)

                    result = self.readiness(project_id)

                    expected_state = (
                        "scan_running" if task_type == "scan" else "analysis_running"
                    )
                    self.assertFalse(result["ready"])
                    self.assertEqual(result["state"], expected_state)
                    self.assertFalse(result["requirements"][f"{task_type}_idle"])
                    self.assertEqual(result["active_tasks"][task_type], [task_id])
                    self.assertIn(
                        f"{task_type}_running",
                        {item["code"] for item in result["blockers"]},
                    )

    def test_thumbnail_status_never_blocks_completed_analysis(self):
        project_id = self.create_project(scanned=True)
        for thumb_status in ("pending", "processing", "failed", "completed"):
            self.create_asset(
                project_id,
                analysis_status="completed",
                thumb_status=thumb_status,
            )

        result = self.readiness(project_id)

        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["counts"]["completed"], 4)

    def test_latest_failed_scan_blocks_even_when_last_scan_at_and_analysis_exist(self):
        project_id = self.create_project(scanned=True)
        self.create_asset(project_id, analysis_status="completed")
        self.create_task(project_id, "scan", "completed")
        failed_task_id = self.create_task(project_id, "scan", "failed")

        result = self.readiness(project_id)

        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "scan_failed")
        self.assertFalse(result["requirements"]["scan_completed"])
        self.assertEqual(result["latest_scan"]["id"], failed_task_id)
        self.assertIn("scan_failed", {item["code"] for item in result["blockers"]})

    def test_completed_scan_after_failure_restores_readiness(self):
        project_id = self.create_project(scanned=True)
        self.create_asset(project_id, analysis_status="completed")
        self.create_task(project_id, "scan", "failed")
        completed_task_id = self.create_task(project_id, "scan", "completed")

        result = self.readiness(project_id)

        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["requirements"]["scan_completed"])
        self.assertEqual(result["latest_scan"]["id"], completed_task_id)
        self.assertEqual(result["latest_scan"]["status"], "completed")
        self.assertEqual(result["blockers"], [])


if __name__ == "__main__":
    unittest.main()
