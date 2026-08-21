import unittest

from app.shot_matcher import fallback_shots, match_shots, normalize_shots


class ShotMatcherTests(unittest.TestCase):
    def test_normalize_model_keywords_split_combined_values(self):
        shots = normalize_shots([{
            "narration": "接口特写",
            "keywords": ["机身侧面 接口 键盘细节", "近景、产品"],
        }])
        self.assertEqual(
            shots[0]["keywords"], ["机身侧面", "接口", "键盘细节", "近景", "产品"]
        )

    def test_fallback_and_candidate_smb_path(self):
        shots = fallback_shots("笔记本放在桌面上。展示侧面接口。")
        assets = [{
            "id": 7, "filename": "接口特写.mp4", "relative_path": "A机/接口特写.mp4",
            "smb_url": "smb://192.0.2.20/素材库", "media_type": "video", "duration": 12,
            "analysis_summary": "笔记本侧面接口特写", "analysis_keywords": '["笔记本","接口"]',
            "analysis_json": '{"timestamps":[1.2,4.5]}',
        }]
        matched = match_shots(shots, assets)
        self.assertTrue(matched[0]["candidates"])
        self.assertEqual(
            matched[1]["candidates"][0]["smb_path"],
            r"\\192.0.2.20\素材库\A机\接口特写.mp4",
        )


if __name__ == "__main__":
    unittest.main()
