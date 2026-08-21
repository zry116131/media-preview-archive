import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.server import normalize_analysis_options


class TaskControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "tasks.db")
        with self.db.session() as conn:
            self.project_id = conn.execute(
                "INSERT INTO projects(name, root_path, smb_url) VALUES ('测试', '/tmp/test', 'smb://nas/test')"
            ).lastrowid

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pause_and_resume_running_task(self):
        task_id = self.db.create_task(self.project_id, "analysis")
        self.db.update_task(task_id, status="running", progress=0.25)
        self.assertTrue(self.db.request_pause(task_id))
        with self.db.session() as conn:
            paused = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(paused["status"], "pausing")
        self.assertEqual(paused["pause_requested"], 1)

        self.assertTrue(self.db.resume_task(task_id))
        with self.db.session() as conn:
            resumed = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["pause_requested"], 0)

    def test_completed_task_cannot_pause(self):
        task_id = self.db.create_task(self.project_id, "scan")
        self.db.update_task(task_id, status="completed", progress=1)
        self.assertFalse(self.db.request_pause(task_id))

    def test_task_options_are_persisted_for_restart_resume(self):
        task_id = self.db.create_task(
            self.project_id,
            "analysis",
            options_json='{"asset_ids":[1,2],"processed_ids":[1]}',
        )
        with self.db.session() as conn:
            task = conn.execute("SELECT options_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertIn('"processed_ids":[1]', task["options_json"])
        self.db.update_task_options(task_id, '{"asset_ids":[1,2],"processed_ids":[1,2]}')
        with self.db.session() as conn:
            updated = conn.execute("SELECT options_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertIn('"processed_ids":[1,2]', updated["options_json"])

    def test_analysis_scope_validation_and_batch_limit(self):
        options = normalize_analysis_options({
            "scope": "failed",
            "media_type": "video",
            "folder_prefix": r"2.原始素材\A机",
            "limit": 999,
            "min_duration": "5",
            "max_duration": "30",
        })
        self.assertEqual(options["folder_prefix"], "2.原始素材/A机")
        self.assertEqual(options["limit"], 500)
        self.assertEqual(options["min_duration"], 5.0)
        with self.assertRaisesRegex(ValueError, "最大时长"):
            normalize_analysis_options({"min_duration": 30, "max_duration": 5})


if __name__ == "__main__":
    unittest.main()
