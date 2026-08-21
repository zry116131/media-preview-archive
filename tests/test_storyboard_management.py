import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from app.database import Database
from app.server import (
    AppHandler,
    create_storyboard_version,
    delete_storyboard,
    insert_storyboard_shots,
)


class StoryboardManagementTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "storyboard-management.db")
        with self.db.session() as conn:
            self.project_id = int(conn.execute(
                """INSERT INTO projects(name, root_path, smb_url)
                   VALUES ('待分析项目', '/tmp/storyboard-management', 'smb://nas/project')"""
            ).lastrowid)
            self.asset_id = int(conn.execute(
                """INSERT INTO assets(
                     project_id, relative_path, filename, extension, media_type,
                     size_bytes, mtime, content_fingerprint, analysis_status
                   ) VALUES (?, 'A001.mp4', 'A001.mp4', '.mp4', 'video',
                             10, 1, 'storyboard-management-asset', 'failed')""",
                (self.project_id,),
            ).lastrowid)
            self.storyboard_id = int(conn.execute(
                """INSERT INTO storyboards(project_id, title, source_text)
                   VALUES (?, '待删除分镜', '测试文案')""",
                (self.project_id,),
            ).lastrowid)
            self.other_storyboard_id = int(conn.execute(
                """INSERT INTO storyboards(project_id, title, source_text)
                   VALUES (?, '保留分镜', '')""",
                (self.project_id,),
            ).lastrowid)
            first = [self.shot("第一版")]
            insert_storyboard_shots(conn, self.storyboard_id, first)
            create_storyboard_version(
                conn, self.storyboard_id, first, planner="Qwen3-4B"
            )
            second = [self.shot("第二版")]
            insert_storyboard_shots(conn, self.storyboard_id, second)
            create_storyboard_version(
                conn, self.storyboard_id, second, planner="deepseek-v4-flash"
            )

    def tearDown(self):
        self.tempdir.cleanup()

    def shot(self, narration):
        return {
            "narration": narration,
            "visual_request": "设备特写",
            "shot_type": "近景",
            "keywords": ["设备"],
            "candidates": [{
                "asset_id": self.asset_id,
                "filename": "A001.mp4",
                "smb_path": "smb://nas/project/A001.mp4",
            }],
            "selected_asset_id": self.asset_id,
            "notes": "",
        }

    def call_delete_api(self, storyboard_id, payload):
        handler = object.__new__(AppHandler)
        response = {}

        def capture(body, status=HTTPStatus.OK):
            response["body"] = body
            response["status"] = status

        handler.send_json = capture
        with patch("app.server.DB", self.db):
            handler.handle_api_delete(
                f"/api/storyboards/{storyboard_id}", payload
            )
        return response

    def test_delete_cascades_shots_and_versions_without_touching_assets(self):
        with self.db.session() as conn:
            result = delete_storyboard(conn, self.storyboard_id)

        self.assertEqual(result["shot_count"], 1)
        self.assertEqual(result["version_count"], 2)
        self.assertEqual(result["remaining_count"], 1)
        with self.db.session() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM storyboards WHERE id=?",
                (self.storyboard_id,),
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM storyboard_shots WHERE storyboard_id=?",
                (self.storyboard_id,),
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM storyboard_versions WHERE storyboard_id=?",
                (self.storyboard_id,),
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM assets WHERE id=?", (self.asset_id,)
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM storyboards WHERE id=?",
                (self.other_storyboard_id,),
            ).fetchone()[0], 1)

    def test_delete_api_requires_matching_confirmation_title(self):
        response = self.call_delete_api(
            self.storyboard_id, {"confirm_title": "错误名称"}
        )

        self.assertEqual(response["status"], HTTPStatus.CONFLICT)
        with self.db.session() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM storyboards WHERE id=?",
                (self.storyboard_id,),
            ).fetchone()[0], 1)

    def test_delete_api_is_available_while_scan_and_analysis_are_not_ready(self):
        with self.db.session() as conn:
            conn.execute(
                """INSERT INTO tasks(project_id, task_type, status)
                   VALUES (?, 'analysis', 'running')""",
                (self.project_id,),
            )

        response = self.call_delete_api(
            self.storyboard_id, {"confirm_title": "待删除分镜"}
        )

        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(response["body"]["deleted_id"], self.storyboard_id)
        self.assertEqual(response["body"]["project_id"], self.project_id)

    def test_delete_api_returns_not_found_for_missing_storyboard(self):
        response = self.call_delete_api(
            999999, {"confirm_title": "不存在"}
        )

        self.assertEqual(response["status"], HTTPStatus.NOT_FOUND)
        self.assertEqual(response["body"]["error"], "分镜册不存在")


if __name__ == "__main__":
    unittest.main()
