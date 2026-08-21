import io
import json
import unittest
import zipfile

from app.storyboards import (
    clean_srt,
    export_csv,
    export_json,
    export_markdown,
    parse_script_file,
    parse_shot_file,
)


class StoryboardImportTests(unittest.TestCase):
    def test_txt_and_srt_script_import(self):
        parsed = parse_script_file("脚本.txt", "第一段。\n第二段。".encode())
        self.assertEqual(parsed["source_format"], "txt")
        self.assertIn("第二段", parsed["source_text"])
        srt = "1\n00:00:01,000 --> 00:00:03,000\n开场画面\n\n2\n00:00:04,000 --> 00:00:06,000\n产品特写"
        self.assertEqual(clean_srt(srt), "开场画面\n产品特写")

    def test_docx_script_import(self):
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>第一段文案</w:t></w:r></w:p>
          <w:p><w:r><w:t>第二段文案</w:t></w:r></w:p></w:body>
        </w:document>'''.encode("utf-8")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("word/document.xml", xml)
        parsed = parse_script_file("现场文案.docx", output.getvalue())
        self.assertEqual(parsed["source_text"], "第一段文案\n第二段文案")

    def test_csv_and_json_shot_import_preserve_notes(self):
        csv_data = "文案,画面需求,景别,关键词,备注\n这是旁白,室内产品特写,近景,产品、桌面,优先使用A机位\n".encode("utf-8")
        parsed = parse_shot_file("清单.csv", csv_data)
        self.assertEqual(parsed["shots"][0]["notes"], "优先使用A机位")
        payload = {"title": "导入版", "shots": [{"narration": "旁白", "notes": "保留"}]}
        parsed_json = parse_shot_file("清单.json", json.dumps(payload, ensure_ascii=False).encode())
        self.assertEqual(parsed_json["title"], "导入版")
        self.assertEqual(parsed_json["shots"][0]["notes"], "保留")

    def test_json_round_trip_preserves_selection(self):
        payload = {
            "title": "回灌版",
            "shots": [{
                "narration": "接口特写",
                "candidates": [{"asset_id": 8, "filename": "A008.mp4"}],
                "selected_asset_id": 8,
            }],
        }
        parsed = parse_shot_file("回灌.json", json.dumps(payload, ensure_ascii=False).encode())
        self.assertEqual(parsed["shots"][0]["selected_asset_id"], 8)
        self.assertEqual(parsed["shots"][0]["candidates"][0]["filename"], "A008.mp4")


class StoryboardExportTests(unittest.TestCase):
    def test_exports_include_selected_smb_path(self):
        board = {"title": "测试分镜", "source_text": "旁白"}
        shots = [{
            "position": 1, "narration": "旁白", "visual_request": "产品特写",
            "shot_type": "近景", "keywords": ["产品"], "notes": "备注",
            "selected_candidate": {"filename": "A001.mp4", "smb_path": "smb://nas/项目/A001.mp4"},
        }]
        self.assertIn("smb://nas/项目/A001.mp4", export_markdown(board, shots).decode("utf-8-sig"))
        self.assertIn("A001.mp4", export_csv(board, shots).decode("utf-8-sig"))
        exported = json.loads(export_json(board, shots))
        self.assertEqual(exported["shots"][0]["selected_candidate"]["filename"], "A001.mp4")


if __name__ == "__main__":
    unittest.main()
