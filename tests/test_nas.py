import unittest
from unittest.mock import patch

from app.nas import NasFileStationClient, file_station_path


class NasAccessTests(unittest.TestCase):
    def test_file_station_path_decodes_smb_url(self):
        self.assertEqual(
            file_station_path(
                "smb://192.0.2.20/%E7%B4%A0%E6%9D%90%E5%BA%93/2026",
                "A机/接口特写.mp4",
            ),
            "/素材库/2026/A机/接口特写.mp4",
        )

    def test_file_station_path_rejects_traversal(self):
        with self.assertRaisesRegex(ValueError, "路径无效"):
            file_station_path("smb://192.0.2.20/素材库/项目", "../私有/文件.mp4")

    def test_direct_url_uses_sid_without_password(self):
        client = NasFileStationClient(
            base_url="https://192.0.2.20:5001",
            account="reader",
            password="do-not-leak",
            verify_tls=False,
            session_ttl_seconds=120,
        )
        with patch.object(client, "_session_id", return_value="temporary-sid"):
            client._download_path = "entry.cgi"
            client._download_version = 2
            client._sid_expires_at = 999999999999.0
            url, remaining = client.direct_url("smb://192.0.2.20/素材库/项目", "片尾.mp4")
        self.assertIn("SYNO.FileStation.Download", url)
        self.assertIn("temporary-sid", url)
        self.assertNotIn("do-not-leak", url)
        self.assertGreater(remaining, 0)

    def test_health_does_not_expose_password_or_sid(self):
        client = NasFileStationClient(
            base_url="https://192.0.2.20:5001",
            account="reader",
            password="do-not-leak",
            verify_tls=False,
        )
        with patch.object(client, "_session_id", return_value="temporary-sid"):
            client._sid_expires_at = 999999999999.0
            status = client.health()
        serialized = str(status)
        self.assertTrue(status["online"])
        self.assertNotIn("do-not-leak", serialized)
        self.assertNotIn("temporary-sid", serialized)


if __name__ == "__main__":
    unittest.main()
