import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.paths import normalize_smb_url, resolve_smb_url, windows_unc_path


class SmbPathTests(unittest.TestCase):
    def test_normalize_windows_unc_path(self):
        self.assertEqual(
            normalize_smb_url(r"\\192.0.2.20\素材库\摄影组临时素材"),
            "smb://192.0.2.20/%E7%B4%A0%E6%9D%90%E5%BA%93/%E6%91%84%E5%BD%B1%E7%BB%84%E4%B8%B4%E6%97%B6%E7%B4%A0%E6%9D%90",
        )

    def test_normalize_rejects_incomplete_windows_path(self):
        with self.assertRaisesRegex(ValueError, "共享名称"):
            normalize_smb_url(r"\\192.0.2.20")

    def test_windows_unc_path(self):
        self.assertEqual(
            windows_unc_path(
                "smb://192.0.2.20/%E7%B4%A0%E6%9D%90%E5%BA%93/2026",
                "A机/接口特写.mp4",
            ),
            r"\\192.0.2.20\素材库\2026\A机\接口特写.mp4",
        )

    def test_resolve_active_gvfs_mount(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            project = runtime / "gvfs" / "smb-share:server=192.0.2.20,share=素材库" / "摄影组临时素材"
            project.mkdir(parents=True)

            result = resolve_smb_url(
                "smb://192.0.2.20/素材库/摄影组临时素材",
                runtime_dir=runtime,
            )

            self.assertEqual(result, project.resolve())

    def test_resolve_windows_unc_from_active_gvfs_mount(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            project = runtime / "gvfs" / "smb-share:server=192.0.2.20,share=素材库" / "摄影组临时素材"
            project.mkdir(parents=True)

            result = resolve_smb_url(
                r"\\192.0.2.20\素材库\摄影组临时素材",
                runtime_dir=runtime,
            )

            self.assertEqual(result, project.resolve())

    def test_missing_share_reports_mount_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            (runtime / "gvfs").mkdir()

            with self.assertRaisesRegex(ValueError, "尚未挂载"):
                resolve_smb_url("smb://192.0.2.20/不存在/项目", runtime_dir=runtime)

    def test_resolve_static_system_mount(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "content"
            project = root / "2026" / "7月项目" / "项目A"
            project.mkdir(parents=True)
            environment = {
                "MEDIA_APP_SMB_SERVER": "192.0.2.20",
                "MEDIA_APP_SMB_SHARE": "素材库",
                "MEDIA_APP_SMB_MOUNT_ROOT": str(root),
            }

            with patch.dict("os.environ", environment):
                result = resolve_smb_url(
                    "smb://192.0.2.20/素材库/2026/7月项目/项目A",
                    runtime_dir=Path(temp_dir) / "missing-runtime",
                )

            self.assertEqual(result, project.resolve())

    def test_static_mount_reports_missing_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "content"
            root.mkdir()
            environment = {
                "MEDIA_APP_SMB_SERVER": "192.0.2.20",
                "MEDIA_APP_SMB_SHARE": "素材库",
                "MEDIA_APP_SMB_MOUNT_ROOT": str(root),
            }

            with patch.dict("os.environ", environment):
                with self.assertRaisesRegex(ValueError, "项目目录不存在"):
                    resolve_smb_url(
                        "smb://192.0.2.20/素材库/不存在",
                        runtime_dir=Path(temp_dir) / "missing-runtime",
                    )


if __name__ == "__main__":
    unittest.main()
