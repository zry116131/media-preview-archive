import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from app.database import Database
from app.server import AppHandler, select_analysis_asset_ids


class AssetAnalysisExceptionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "asset-exceptions.db")
        self.sequence = 0
        with self.db.session() as conn:
            self.project_id = int(conn.execute(
                """INSERT INTO projects(name, root_path, smb_url, last_scan_at)
                   VALUES ('异常素材项目', '/tmp/asset-exceptions',
                           'smb://nas/asset-exceptions', '2026-07-16 10:00:00')"""
            ).lastrowid)

    def tearDown(self):
        self.tempdir.cleanup()

    def create_asset(
        self,
        *,
        analysis_status="failed",
        status="active",
        exempted=False,
        exemption_fingerprint=None,
        analysis_error="无法从素材读取分析画面",
    ):
        self.sequence += 1
        fingerprint = f"asset-fingerprint-{self.sequence}"
        if exemption_fingerprint is None and exempted:
            exemption_fingerprint = fingerprint
        with self.db.session() as conn:
            return int(conn.execute(
                """INSERT INTO assets(
                     project_id, relative_path, filename, extension, media_type,
                     size_bytes, mtime, content_fingerprint, status,
                     analysis_status, analysis_error, analysis_exempted_at,
                     analysis_exempt_reason, analysis_exempt_fingerprint
                   ) VALUES (?, ?, ?, '.mp4', 'video', 1024, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.project_id,
                    f"clip-{self.sequence}.mp4",
                    f"clip-{self.sequence}.mp4",
                    float(self.sequence),
                    fingerprint,
                    status,
                    analysis_status,
                    analysis_error,
                    "2026-07-16 10:30:00" if exempted else None,
                    "文件损坏" if exempted else None,
                    exemption_fingerprint,
                ),
            ).lastrowid)

    def call_post_api(self, path, payload):
        handler = object.__new__(AppHandler)
        response = {}

        def capture(body, status=HTTPStatus.OK):
            response["body"] = body
            response["status"] = status

        handler.send_json = capture
        with patch("app.server.DB", self.db):
            handler.handle_api_post(path, payload)
        return response

    def test_analysis_selection_excludes_only_fingerprint_valid_exemptions(self):
        valid_exempted = self.create_asset(exempted=True)
        stale_exempted = self.create_asset(
            exempted=True, exemption_fingerprint="old-content"
        )
        pending = self.create_asset(analysis_status="pending", analysis_error=None)
        completed = self.create_asset(analysis_status="completed", analysis_error=None)
        missing = self.create_asset(status="missing")

        base_options = {
            "media_type": "all",
            "folder_prefix": "",
            "min_duration": None,
            "max_duration": None,
            "limit": None,
        }
        with patch("app.server.DB", self.db):
            pending_ids = select_analysis_asset_ids(
                self.project_id, {**base_options, "scope": "pending"}
            )
            failed_ids = select_analysis_asset_ids(
                self.project_id, {**base_options, "scope": "failed"}
            )
            all_ids = select_analysis_asset_ids(
                self.project_id, {**base_options, "scope": "all"}
            )

        self.assertNotIn(valid_exempted, pending_ids)
        self.assertNotIn(valid_exempted, failed_ids)
        self.assertNotIn(valid_exempted, all_ids)
        self.assertEqual(pending_ids, [stale_exempted, pending])
        self.assertEqual(failed_ids, [stale_exempted])
        self.assertEqual(all_ids, [stale_exempted, pending, completed])
        self.assertNotIn(missing, all_ids)

    def test_exemption_and_restore_update_gate_without_deleting_asset(self):
        self.create_asset(analysis_status="completed", analysis_error=None)
        failed_asset = self.create_asset()

        excluded = self.call_post_api(
            f"/api/assets/{failed_asset}/analysis-exemption",
            {"exempt": True, "reason": "文件损坏"},
        )

        self.assertEqual(excluded["status"], HTTPStatus.OK)
        self.assertTrue(excluded["body"]["asset"]["analysis_exempted"])
        self.assertEqual(
            excluded["body"]["asset"]["analysis_exempt_reason"], "文件损坏"
        )
        self.assertTrue(excluded["body"]["readiness"]["ready"])
        self.assertEqual(excluded["body"]["readiness"]["counts"]["exempted"], 1)
        with self.db.session() as conn:
            stored = conn.execute(
                "SELECT * FROM assets WHERE id=?", (failed_asset,)
            ).fetchone()
        self.assertIsNotNone(stored)
        self.assertEqual(
            stored["analysis_exempt_fingerprint"], stored["content_fingerprint"]
        )

        restored = self.call_post_api(
            f"/api/assets/{failed_asset}/analysis-exemption", {"exempt": False}
        )

        self.assertEqual(restored["status"], HTTPStatus.OK)
        self.assertFalse(restored["body"]["asset"]["analysis_exempted"])
        self.assertEqual(restored["body"]["asset"]["analysis_status"], "pending")
        self.assertFalse(restored["body"]["readiness"]["ready"])
        self.assertEqual(restored["body"]["readiness"]["state"], "analysis_required")
        with self.db.session() as conn:
            stored = conn.execute(
                "SELECT * FROM assets WHERE id=?", (failed_asset,)
            ).fetchone()
        self.assertIsNone(stored["analysis_exempted_at"])
        self.assertIsNone(stored["analysis_exempt_reason"])
        self.assertIsNone(stored["analysis_exempt_fingerprint"])
        self.assertIsNone(stored["analysis_error"])

    def test_retry_creates_single_asset_analysis_task(self):
        failed_asset = self.create_asset()
        handler = object.__new__(AppHandler)
        response = {}

        def capture(body, status=HTTPStatus.OK):
            response["body"] = body
            response["status"] = status

        handler.send_json = capture
        with (
            patch("app.server.DB", self.db),
            patch("app.server.JOYAI") as joyai,
            patch("app.server.EXECUTOR") as executor,
        ):
            joyai.status.return_value = {"enabled": True, "message": "online"}
            handler.handle_api_post(
                f"/api/assets/{failed_asset}/analysis/retry", {}
            )

        self.assertEqual(response["status"], HTTPStatus.ACCEPTED)
        executor.submit.assert_called_once()
        with self.db.session() as conn:
            asset = conn.execute(
                "SELECT analysis_status FROM assets WHERE id=?", (failed_asset,)
            ).fetchone()
            task = conn.execute(
                "SELECT * FROM tasks WHERE project_id=?", (self.project_id,)
            ).fetchone()
        self.assertEqual(asset["analysis_status"], "pending")
        self.assertEqual(task["task_type"], "analysis")
        self.assertEqual(task["status"], "queued")
        self.assertEqual(json.loads(task["options_json"])["asset_ids"], [failed_asset])

    def test_retry_rejects_an_actively_exempted_asset(self):
        exempted_asset = self.create_asset(exempted=True)
        handler = object.__new__(AppHandler)
        response = {}

        def capture(body, status=HTTPStatus.OK):
            response["body"] = body
            response["status"] = status

        handler.send_json = capture
        with (
            patch("app.server.DB", self.db),
            patch("app.server.JOYAI") as joyai,
            patch("app.server.EXECUTOR") as executor,
        ):
            joyai.status.return_value = {"enabled": True, "message": "online"}
            handler.handle_api_post(
                f"/api/assets/{exempted_asset}/analysis/retry", {}
            )

        self.assertEqual(response["status"], HTTPStatus.CONFLICT)
        self.assertIn("取消", response["body"]["error"])
        executor.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
