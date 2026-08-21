import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.database import Database
from app.scanner import ProjectScanner, parse_frame_rate


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.media = self.base / "media"
        self.media.mkdir()
        self.db = Database(self.base / "test.db")
        with self.db.session() as conn:
            cur = conn.execute(
                "INSERT INTO projects(name, root_path, smb_url) VALUES (?, ?, ?)",
                ("测试项目", str(self.media), "smb://nas/测试项目"),
            )
            self.project_id = int(cur.lastrowid)
        self.scanner = ProjectScanner(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_scan_create_rename_and_missing(self):
        original = self.media / "A001.png"
        original.write_bytes(PNG_1X1)

        first = self.scanner.scan(self.project_id)
        self.assertEqual(first["created"], 1)
        with self.db.session() as conn:
            asset = conn.execute("SELECT * FROM assets").fetchone()
            original_id = asset["id"]
            self.assertEqual(asset["relative_path"], "A001.png")

        original.rename(self.media / "精选画面.png")
        renamed = self.scanner.scan(self.project_id)
        self.assertEqual(renamed["updated"], 1)
        self.assertEqual(renamed["created"], 0)
        with self.db.session() as conn:
            asset = conn.execute("SELECT * FROM assets WHERE status='active'").fetchone()
            self.assertEqual(asset["id"], original_id)
            self.assertEqual(asset["relative_path"], "精选画面.png")

        (self.media / "精选画面.png").unlink()
        missing = self.scanner.scan(self.project_id)
        self.assertEqual(missing["missing"], 1)
        with self.db.session() as conn:
            status = conn.execute("SELECT status FROM assets WHERE id=?", (original_id,)).fetchone()["status"]
            self.assertEqual(status, "missing")

    def test_scan_ignores_zero_byte_media_and_marks_existing_asset_missing(self):
        empty = self.media / "剪辑临时文件.m4v"
        empty.touch()

        first = self.scanner.scan(self.project_id)
        self.assertEqual(first["total"], 0)
        self.assertEqual(first["created"], 0)

        empty.write_bytes(b"not-empty")
        second = self.scanner.scan(self.project_id)
        self.assertEqual(second["created"], 1)

        empty.write_bytes(b"")
        third = self.scanner.scan(self.project_id)
        self.assertEqual(third["missing"], 1)
        with self.db.session() as conn:
            status = conn.execute(
                "SELECT status FROM assets WHERE relative_path=?",
                (empty.name,),
            ).fetchone()["status"]
        self.assertEqual(status, "missing")

    def test_unchanged_video_reprobes_when_any_required_metadata_is_missing(self):
        complete_metadata = {
            "duration": 12.5,
            "width": 1920,
            "height": 1080,
            "fps": 25.0,
            "codec": "h264",
            "bit_rate": 8_000_000,
        }
        missing_fields = ("duration", "width", "height", "fps", "codec")
        for index, field in enumerate(missing_fields):
            (self.media / f"clip-{index}.mp4").write_bytes(f"video-{index}".encode())

        with patch("app.scanner.probe_media", return_value=complete_metadata):
            first = self.scanner.scan(self.project_id)
        self.assertEqual(first["created"], len(missing_fields))

        with self.db.session() as conn:
            for index, field in enumerate(missing_fields):
                conn.execute(
                    f"UPDATE assets SET {field}=NULL WHERE relative_path=?",
                    (f"clip-{index}.mp4",),
                )

        with patch("app.scanner.probe_media", return_value=complete_metadata) as mocked_probe:
            second = self.scanner.scan(self.project_id)

        self.assertEqual(second["unchanged"], len(missing_fields))
        self.assertEqual(mocked_probe.call_count, len(missing_fields))
        with self.db.session() as conn:
            assets = conn.execute("SELECT * FROM assets ORDER BY relative_path").fetchall()
        for asset in assets:
            with self.subTest(asset=asset["relative_path"]):
                for field in missing_fields:
                    self.assertIsNotNone(asset[field])

    def test_metadata_reprobe_coalesces_and_requeues_frame_fallback(self):
        video = self.media / "fallback.mp4"
        video.write_bytes(b"mock-video")
        initial_metadata = {
            "duration": 10.0,
            "width": 3840,
            "height": 2160,
            "fps": 60.0,
            "codec": "hevc",
            "bit_rate": 80_000_000,
        }
        with patch("app.scanner.probe_media", return_value=initial_metadata):
            self.scanner.scan(self.project_id)

        with self.db.session() as conn:
            conn.execute(
                """UPDATE assets SET duration=NULL, analysis_status='completed',
                   analysis_strategy='frame_fallback' WHERE relative_path=?""",
                (video.name,),
            )

        partial_metadata = {
            "duration": 18.0,
            "width": None,
            "height": None,
            "fps": None,
            "codec": None,
            "bit_rate": None,
        }
        with patch("app.scanner.probe_media", return_value=partial_metadata) as mocked_probe:
            result = self.scanner.scan(self.project_id)

        self.assertEqual(result["unchanged"], 1)
        mocked_probe.assert_called_once()
        self.assertEqual(mocked_probe.call_args.args[0].resolve(), video.resolve())
        with self.db.session() as conn:
            asset = conn.execute(
                "SELECT * FROM assets WHERE relative_path=?", (video.name,)
            ).fetchone()
        self.assertEqual(asset["duration"], 18.0)
        self.assertEqual(asset["width"], initial_metadata["width"])
        self.assertEqual(asset["height"], initial_metadata["height"])
        self.assertEqual(asset["fps"], initial_metadata["fps"])
        self.assertEqual(asset["codec"], initial_metadata["codec"])
        self.assertEqual(asset["bit_rate"], initial_metadata["bit_rate"])
        self.assertEqual(asset["analysis_status"], "pending")

    def test_unchanged_exempted_asset_preserves_fingerprint_bound_exemption(self):
        media = self.media / "damaged.png"
        media.write_bytes(PNG_1X1)
        self.scanner.scan(self.project_id)
        with self.db.session() as conn:
            asset = conn.execute(
                "SELECT * FROM assets WHERE relative_path=?", (media.name,)
            ).fetchone()
            conn.execute(
                """UPDATE assets SET analysis_status='failed',
                   analysis_error='无法读取画面',
                   analysis_exempted_at='2026-07-16 10:30:00',
                   analysis_exempt_reason='文件损坏',
                   analysis_exempt_fingerprint=? WHERE id=?""",
                (asset["content_fingerprint"], asset["id"]),
            )

        result = self.scanner.scan(self.project_id)

        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["updated"], 0)
        with self.db.session() as conn:
            rescanned = conn.execute(
                "SELECT * FROM assets WHERE id=?", (asset["id"],)
            ).fetchone()
        self.assertEqual(rescanned["analysis_status"], "failed")
        self.assertEqual(rescanned["analysis_exempt_reason"], "文件损坏")
        self.assertEqual(
            rescanned["analysis_exempt_fingerprint"],
            rescanned["content_fingerprint"],
        )

    def test_same_size_and_mtime_content_replacement_revokes_exemption(self):
        media = self.media / "repaired.png"
        media.write_bytes(b"original-image-bytes")
        self.scanner.scan(self.project_id)
        original_stat = media.stat()
        with self.db.session() as conn:
            asset = conn.execute(
                "SELECT * FROM assets WHERE relative_path=?", (media.name,)
            ).fetchone()
            old_fingerprint = asset["content_fingerprint"]
            conn.execute(
                """UPDATE assets SET analysis_status='failed',
                   analysis_error='无法读取画面',
                   analysis_exempted_at='2026-07-16 10:30:00',
                   analysis_exempt_reason='文件损坏',
                   analysis_exempt_fingerprint=? WHERE id=?""",
                (old_fingerprint, asset["id"]),
            )

        replacement = b"repaired-image-bytes"
        self.assertEqual(len(replacement), len(b"original-image-bytes"))
        media.write_bytes(replacement)
        os.utime(
            media,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

        result = self.scanner.scan(self.project_id)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["unchanged"], 0)
        with self.db.session() as conn:
            rescanned = conn.execute(
                "SELECT * FROM assets WHERE id=?", (asset["id"],)
            ).fetchone()
        self.assertNotEqual(rescanned["content_fingerprint"], old_fingerprint)
        self.assertEqual(rescanned["analysis_status"], "pending")
        self.assertIsNone(rescanned["analysis_error"])
        self.assertIsNone(rescanned["analysis_exempted_at"])
        self.assertIsNone(rescanned["analysis_exempt_reason"])
        self.assertIsNone(rescanned["analysis_exempt_fingerprint"])

    def test_same_content_rename_preserves_exemption_and_asset_identity(self):
        original = self.media / "bad-name.png"
        original.write_bytes(PNG_1X1)
        self.scanner.scan(self.project_id)
        with self.db.session() as conn:
            asset = conn.execute(
                "SELECT * FROM assets WHERE relative_path=?", (original.name,)
            ).fetchone()
            conn.execute(
                """UPDATE assets SET analysis_status='failed',
                   analysis_exempted_at='2026-07-16 10:30:00',
                   analysis_exempt_reason='文件损坏',
                   analysis_exempt_fingerprint=? WHERE id=?""",
                (asset["content_fingerprint"], asset["id"]),
            )

        renamed = self.media / "renamed.png"
        original.rename(renamed)
        result = self.scanner.scan(self.project_id)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["created"], 0)
        with self.db.session() as conn:
            rescanned = conn.execute(
                "SELECT * FROM assets WHERE status='active'"
            ).fetchone()
        self.assertEqual(rescanned["id"], asset["id"])
        self.assertEqual(rescanned["relative_path"], renamed.name)
        self.assertEqual(rescanned["analysis_status"], "failed")
        self.assertEqual(rescanned["analysis_exempt_reason"], "文件损坏")
        self.assertEqual(
            rescanned["analysis_exempt_fingerprint"],
            rescanned["content_fingerprint"],
        )

    def test_frame_rate_parser_handles_fractional_rates(self):
        self.assertEqual(parse_frame_rate("30000/1001"), 29.97)
        self.assertEqual(parse_frame_rate("25"), 25.0)
        self.assertIsNone(parse_frame_rate("0/0"))


if __name__ == "__main__":
    unittest.main()
