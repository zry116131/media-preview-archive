import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.server import (
    activate_storyboard_version,
    create_storyboard_version,
    ensure_legacy_storyboard_version,
    insert_storyboard_shots,
    storyboard_detail,
    sync_active_version_selection,
)


class StoryboardVersionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "versions.db")
        with self.db.session() as conn:
            self.project_id = conn.execute(
                "INSERT INTO projects(name, root_path, smb_url) VALUES ('项目', '/tmp/project', 'smb://nas/project')"
            ).lastrowid
            self.storyboard_id = conn.execute(
                "INSERT INTO storyboards(project_id, title, source_text) VALUES (?, '分镜', '文案')",
                (self.project_id,),
            ).lastrowid
            self.asset_id = conn.execute(
                """INSERT INTO assets(
                     project_id, relative_path, filename, extension, media_type,
                     size_bytes, mtime, content_fingerprint
                   ) VALUES (?, 'A001.mp4', 'A001.mp4', '.mp4', 'video', 10, 1, 'fp')""",
                (self.project_id,),
            ).lastrowid

    def tearDown(self):
        self.tempdir.cleanup()

    def shot(self, narration, selected=None):
        return {
            "narration": narration,
            "visual_request": narration,
            "shot_type": "近景",
            "keywords": ["设备"],
            "candidates": [{
                "asset_id": self.asset_id,
                "filename": "A001.mp4",
                "smb_path": "smb://nas/project/A001.mp4",
            }],
            "selected_asset_id": selected,
            "notes": "",
        }

    def test_versions_can_switch_and_preserve_selection(self):
        with self.db.session() as conn:
            first = [self.shot("第一版")]
            insert_storyboard_shots(conn, self.storyboard_id, first)
            version_one = create_storyboard_version(
                conn, self.storyboard_id, first, planner="Qwen3-4B"
            )
            second = [self.shot("第二版")]
            insert_storyboard_shots(conn, self.storyboard_id, second)
            version_two = create_storyboard_version(
                conn, self.storyboard_id, second, planner="deepseek-v4-flash"
            )
            detail = storyboard_detail(conn, self.storyboard_id)
            self.assertEqual(detail["active_version_id"], version_two)
            self.assertEqual([item["version_number"] for item in detail["versions"]], [2, 1])

            self.assertTrue(activate_storyboard_version(conn, self.storyboard_id, version_one))
            current = conn.execute(
                "SELECT id, position FROM storyboard_shots WHERE storyboard_id=?",
                (self.storyboard_id,),
            ).fetchone()
            conn.execute(
                "UPDATE storyboard_shots SET selected_asset_id=? WHERE id=?",
                (self.asset_id, current["id"]),
            )
            sync_active_version_selection(
                conn, self.storyboard_id, current["position"], self.asset_id
            )
            self.assertTrue(activate_storyboard_version(conn, self.storyboard_id, version_two))
            self.assertTrue(activate_storyboard_version(conn, self.storyboard_id, version_one))
            restored = storyboard_detail(conn, self.storyboard_id)
            self.assertEqual(restored["shots"][0]["narration"], "第一版")
            self.assertEqual(restored["shots"][0]["selected_asset_id"], self.asset_id)

    def test_existing_shots_are_backfilled_once(self):
        with self.db.session() as conn:
            insert_storyboard_shots(conn, self.storyboard_id, [self.shot("现有分镜")])
            first_id = ensure_legacy_storyboard_version(conn, self.storyboard_id)
            second_id = ensure_legacy_storyboard_version(conn, self.storyboard_id)
            self.assertEqual(first_id, second_id)
            count = conn.execute(
                "SELECT COUNT(*) FROM storyboard_versions WHERE storyboard_id=?",
                (self.storyboard_id,),
            ).fetchone()[0]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
