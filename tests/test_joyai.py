import unittest

from app.joyai import (
    adaptive_analysis_strategy, analysis_frame_timestamps, merge_analysis_results,
    offset_video_analysis, parse_model_json, recommended_frame_count,
    trim_analysis_to_window, video_segment_ranges,
)


class JoyAIResponseTests(unittest.TestCase):
    def test_parse_json_code_fence(self):
        result = parse_model_json(
            '```json\n{"summary":"产品特写","keywords":["笔记本","接口"]}\n```'
        )
        self.assertEqual(result["summary"], "产品特写")
        self.assertEqual(result["keywords"], ["笔记本", "接口"])

    def test_plain_text_falls_back_to_summary(self):
        result = parse_model_json("一台笔记本电脑放在桌面上")
        self.assertEqual(result["summary"], "一台笔记本电脑放在桌面上")
        self.assertEqual(result["keywords"], [])

    def test_unwrap_single_item_array(self):
        result = parse_model_json(
            '```json\n[{"summary":"接口特写","keywords":["USB-C","侧面"]}]\n```'
        )
        self.assertEqual(result["summary"], "接口特写")
        self.assertEqual(result["keywords"], ["USB-C", "侧面"])

    def test_multiple_items_are_preserved_and_aggregated(self):
        result = parse_model_json(
            '[{"summary":"人物讲解","keywords":["人物"]},'
            '{"summary":"跑分图表","keywords":["跑分","图表"]}]',
            [12.0, 42.0],
        )
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(result["segments"][1]["timestamp"], 42.0)
        self.assertIn("人物讲解", result["summary"])
        self.assertIn("跑分图表", result["summary"])
        self.assertEqual(result["keywords"], ["人物", "跑分", "图表"])

    def test_segment_wrapper_is_supported(self):
        result = parse_model_json(
            '{"segments":[{"timestamp":5,"summary":"接口特写","keywords":["接口"]}]}',
            [5.0],
        )
        self.assertEqual(result["segments"][0]["summary"], "接口特写")

    def test_adaptive_frame_count_for_long_video(self):
        self.assertEqual(recommended_frame_count(20), 4)
        self.assertEqual(recommended_frame_count(360), 12)
        self.assertEqual(recommended_frame_count(870), 18)
        self.assertEqual(recommended_frame_count(3600), 24)
        timestamps = analysis_frame_timestamps(360)
        self.assertEqual(len(timestamps), 12)
        self.assertGreater(timestamps[0], 0)
        self.assertLess(timestamps[-1], 360)

    def test_batch_results_keep_all_segments(self):
        merged = merge_analysis_results([
            {"segments": [{"timestamp": 10, "summary": "人物", "keywords": ["人物"]}], "timestamps": [10]},
            {"segments": [{"timestamp": 90, "summary": "产品", "keywords": ["产品"]}], "timestamps": [90]},
        ])
        self.assertEqual(len(merged["segments"]), 2)
        self.assertEqual(merged["keywords"], ["人物", "产品"])

    def test_adaptive_routes_by_media_duration(self):
        image = adaptive_analysis_strategy({"media_type": "image", "duration": None})
        short = adaptive_analysis_strategy({
            "media_type": "video", "duration": 20, "width": 3840,
            "height": 2160, "fps": 60, "codec": "hevc",
        })
        medium = adaptive_analysis_strategy({"media_type": "video", "duration": 90})
        long = adaptive_analysis_strategy({"media_type": "video", "duration": 360})
        extra_long = adaptive_analysis_strategy({"media_type": "video", "duration": 900})
        self.assertEqual(image["id"], "image_direct")
        self.assertEqual(short["id"], "video_whole_short")
        self.assertIn("4K源临时缩放", short["notes"])
        self.assertIn("高帧率源降采样", short["notes"])
        self.assertEqual(medium["id"], "video_whole_medium")
        self.assertEqual(long["chunk_seconds"], 60.0)
        self.assertEqual(extra_long["chunk_seconds"], 90.0)

    def test_heavy_medium_video_uses_chunked_route(self):
        normal = adaptive_analysis_strategy({
            "media_type": "video", "duration": 90,
            "width": 1920, "height": 1080, "fps": 25, "codec": "h264",
        })
        heavy = adaptive_analysis_strategy({
            "media_type": "video", "duration": 90,
            "width": 3840, "height": 2160, "fps": 60, "codec": "hevc",
            "bit_rate": 120_000_000,
        })
        self.assertEqual(normal["id"], "video_whole_medium")
        self.assertEqual(heavy["id"], "video_chunked_medium_heavy")
        self.assertEqual(heavy["chunk_seconds"], 60.0)
        self.assertEqual(heavy["decode_fps"], 3.0)
        self.assertTrue(heavy["heavy_source"])
        self.assertIn("高码率源轻量化", heavy["notes"])

    def test_duration_route_boundaries(self):
        expected = {
            0: "frame_fallback",
            30: "video_whole_short",
            30.001: "video_whole_medium",
            120: "video_whole_medium",
            120.001: "video_chunked_long",
            600: "video_chunked_long",
            600.001: "video_chunked_xlong",
        }
        for duration, route_id in expected.items():
            with self.subTest(duration=duration):
                strategy = adaptive_analysis_strategy({
                    "media_type": "video", "duration": duration,
                })
                self.assertEqual(strategy["id"], route_id)

    def test_video_segments_overlap_without_tiny_tail(self):
        ranges = video_segment_ranges(130, 60, 2)
        self.assertEqual(ranges, [(0.0, 60.0), (58.0, 118.0), (116.0, 130.0)])
        self.assertEqual(video_segment_ranges(20, 30, 0), [(0.0, 20.0)])

    def test_video_timestamps_are_offset_to_source(self):
        result = offset_video_analysis({
            "segments": [
                {"timestamp": 1, "start_time": 1, "end_time": 5, "summary": "动作"},
                {"timestamp": 8, "start_time": 8, "end_time": 20, "summary": "特写"},
            ]
        }, 60, 75)
        self.assertEqual(result["segments"][0]["start_time"], 61.0)
        self.assertEqual(result["segments"][1]["end_time"], 75.0)
        self.assertEqual(result["analysis_version"], 3)

    def test_overlap_window_trims_duplicate_boundary_segments(self):
        result = {
            "segments": [
                {"timestamp": 58, "start_time": 58, "end_time": 62, "summary": "边界画面"},
                {"timestamp": 70, "start_time": 70, "end_time": 80, "summary": "新画面"},
            ]
        }
        trimmed = trim_analysis_to_window(result, 59, 117)
        self.assertEqual(
            [item["summary"] for item in trimmed["segments"]],
            ["边界画面", "新画面"],
        )
        self.assertEqual(trimmed["segments"][0]["start_time"], 59.0)

    def test_merge_sorts_and_deduplicates_adjacent_overlap(self):
        merged = merge_analysis_results([
            {"segments": [{"timestamp": 61, "start_time": 61, "end_time": 63, "summary": "同一动作", "keywords": ["人物"]}]},
            {"segments": [{"timestamp": 59, "start_time": 59, "end_time": 61, "summary": "同一动作", "keywords": ["动作"]}]},
        ])
        self.assertEqual(len(merged["segments"]), 1)
        self.assertEqual(merged["segments"][0]["timestamp"], 59.0)
        self.assertEqual(merged["segments"][0]["keywords"], ["动作", "人物"])

    def test_merge_keeps_repeated_but_discontinuous_shots(self):
        merged = merge_analysis_results([{
            "segments": [
                {"timestamp": 10, "start_time": 10, "end_time": 11, "summary": "人物讲话"},
                {"timestamp": 13, "start_time": 13, "end_time": 14, "summary": "人物讲话"},
            ]
        }])
        self.assertEqual(len(merged["segments"]), 2)

    def test_trimmed_empty_result_does_not_leak_clip_summary(self):
        empty = trim_analysis_to_window({
            "summary": "已被裁掉的边界内容",
            "segments": [{"timestamp": 58.2, "summary": "已被裁掉的边界内容"}],
        }, 59, 117)
        merged = merge_analysis_results([
            empty,
            {"segments": [{"timestamp": 70, "summary": "保留内容"}]},
        ])
        self.assertEqual(merged["summary"], "保留内容")

    def test_offset_sorts_model_segments_before_inferred_end_times(self):
        result = offset_video_analysis({
            "segments": [
                {"timestamp": 10, "start_time": 10, "summary": "后一个"},
                {"timestamp": 5, "start_time": 5, "summary": "前一个"},
            ]
        }, 60, 80)
        self.assertEqual([item["summary"] for item in result["segments"]], ["前一个", "后一个"])
        self.assertEqual(result["segments"][0]["end_time"], 70.0)


if __name__ == "__main__":
    unittest.main()
