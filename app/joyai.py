from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


class JoyAIClient:
    def __init__(self, endpoint: str | None = None, model: str | None = None):
        self.endpoint = (endpoint or os.environ.get("JOYAI_ENDPOINT", "")).rstrip("/")
        self.model = model or os.environ.get(
            "JOYAI_MODEL", "JoyAI-VL-Interaction-Preview"
        )

    def status(self) -> dict[str, object]:
        if not self.endpoint:
            return {
                "enabled": False,
                "status": "not_configured",
                "model": self.model,
                "endpoint": "",
                "message": "JoyAI 推理服务尚未配置",
            }
        try:
            request = urllib.request.Request(f"{self.endpoint}/models")
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.load(response)
            models = [item.get("id") for item in payload.get("data", [])]
            online = self.model in models
            return {
                "enabled": online,
                "status": "online" if online else "model_unavailable",
                "model": self.model,
                "endpoint": self.endpoint,
                "message": "JoyAI 服务在线，可以分析项目" if online else "接口在线，但目标模型未加载",
            }
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            return {
                "enabled": False,
                "status": "offline",
                "model": self.model,
                "endpoint": self.endpoint,
                "message": f"JoyAI 服务不可用：{exc}",
            }

    def analyze(self, images: list[Path], timestamps: list[float]) -> dict[str, object]:
        if not self.endpoint:
            raise RuntimeError("JoyAI 推理服务尚未配置")
        content: list[dict[str, object]] = [{
            "type": "text",
            "text": (
                "你是现场拍摄素材整理助手。以下画面来自同一条视频，每张图前标有时间点。"
                "请分别理解每个明显不同的画面，不要只描述第一张图。输出纯 JSON，不要 Markdown。"
                "格式必须为 {\"segments\":[...]}，segments 每项必须包含："
                "timestamp（对应秒数）、summary（中文简述，60字内）、"
                "keywords（3到8个中文检索词数组）、shot_type（景别或镜头类型）、"
                "subjects（主体数组）、scene（场景）、actions（动作数组）、"
                "usable（是否建议作为可用素材，布尔值）。不要猜测看不清的品牌或参数。"
            ),
        }]
        for image, timestamp in zip(images, timestamps):
            content.append({
                "type": "text",
                "text": f"时间点：{timestamp:.3f} 秒",
            })
            mime_type = mimetypes.guess_type(image.name)[0] or "image/jpeg"
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
            })
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": content}],
        }
        request = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.load(response)
        raw = result["choices"][0]["message"]["content"].strip()
        parsed = parse_model_json(raw, timestamps)
        parsed["raw"] = raw
        return parsed

    def analyze_video(
        self,
        video: Path,
        *,
        clip_start: float,
        clip_end: float,
        sample_fps: float,
    ) -> dict[str, object]:
        if not self.endpoint:
            raise RuntimeError("JoyAI 推理服务尚未配置")
        duration = max(0.1, float(clip_end) - float(clip_start))
        content: list[dict[str, object]] = [
            {
                "type": "video_url",
                "video_url": {"url": video.resolve().as_uri()},
            },
            {
                "type": "text",
                "text": (
                    "你是现场拍摄素材整理助手。请完整理解这段连续视频中的主体、动作、场景、"
                    "景别和镜头变化。只输出纯 JSON，不要 Markdown。格式必须为 "
                    "{\"summary\":\"整段概述\",\"segments\":[...]}。segments 每项必须包含："
                    "start_time、end_time、timestamp（均为相对于当前片段开头的秒数）、"
                    "summary（中文简述，60字内）、keywords（3到8个中文检索词数组）、"
                    "shot_type、subjects、scene、actions、usable。"
                    "按明显内容变化拆分片段，不要猜测看不清的品牌、人物身份或产品参数。"
                ),
            },
        ]
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 1400,
            "messages": [{"role": "user", "content": content}],
            "mm_processor_kwargs": {
                "fps": round(max(0.25, min(2.0, float(sample_fps))), 3),
                "do_sample_frames": True,
            },
        }
        request = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"JoyAI 视频请求失败（HTTP {exc.code}）：{detail}") from exc
        raw = result["choices"][0]["message"]["content"].strip()
        parsed = parse_model_json(raw, [0.0, duration])
        parsed = offset_video_analysis(parsed, float(clip_start), float(clip_end))
        parsed["raw"] = raw
        return parsed


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return [str(value)] if value else []
    return [str(item).strip() for item in value if str(item).strip()]


def _unique(items: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.casefold()
        if not item or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def aggregate_analysis_segments(
    segments: list[dict[str, object]],
    timestamps: list[float] | None = None,
    *,
    summary: str = "",
) -> dict[str, object]:
    normalized: list[dict[str, object]] = []
    sample_times = [round(float(value), 3) for value in (timestamps or [])]
    for index, item in enumerate(segments):
        if not isinstance(item, dict):
            continue
        segment = dict(item)
        fallback_time = sample_times[min(index, len(sample_times) - 1)] if sample_times else 0.0
        try:
            timestamp = round(float(segment.get("timestamp", fallback_time)), 3)
        except (TypeError, ValueError):
            timestamp = fallback_time
        segment["timestamp"] = timestamp
        segment["summary"] = str(segment.get("summary") or "").strip()[:300]
        segment["keywords"] = _unique(_string_list(segment.get("keywords")), 12)
        segment["subjects"] = _unique(_string_list(segment.get("subjects")), 12)
        segment["actions"] = _unique(_string_list(segment.get("actions")), 12)
        segment["shot_type"] = str(segment.get("shot_type") or "").strip()[:80]
        segment["scene"] = str(segment.get("scene") or "").strip()[:120]
        segment["usable"] = bool(segment.get("usable", True))
        normalized.append(segment)

    if len(normalized) == 1 and len(sample_times) > 1:
        normalized[0]["start_time"] = sample_times[0]
        normalized[0]["end_time"] = sample_times[-1]
    summaries = _unique(
        [str(item.get("summary") or "") for item in normalized],
        8,
    )
    merged_summary = summary.strip() or "；".join(summaries)
    keywords = _unique(
        [keyword for item in normalized for keyword in _string_list(item.get("keywords"))],
        24,
    )
    subjects = _unique(
        [subject for item in normalized for subject in _string_list(item.get("subjects"))],
        20,
    )
    actions = _unique(
        [action for item in normalized for action in _string_list(item.get("actions"))],
        20,
    )
    scenes = _unique([str(item.get("scene") or "") for item in normalized], 8)
    shot_types = _unique([str(item.get("shot_type") or "") for item in normalized], 8)
    return {
        "analysis_version": 2,
        "summary": merged_summary[:600],
        "keywords": keywords,
        "shot_type": shot_types[0] if len(shot_types) == 1 else "多镜头",
        "subjects": subjects,
        "scene": scenes[0] if len(scenes) == 1 else "多场景",
        "actions": actions,
        "usable": any(bool(item.get("usable", True)) for item in normalized),
        "segments": normalized,
        "timestamps": sample_times or [float(item["timestamp"]) for item in normalized],
    }


def merge_analysis_results(results: list[dict[str, object]]) -> dict[str, object]:
    segments: list[dict[str, object]] = []
    timestamps: list[float] = []
    raw_batches: list[str] = []
    for result in results:
        value = result.get("segments")
        if isinstance(value, list):
            segments.extend(item for item in value if isinstance(item, dict))
        times = result.get("timestamps")
        if isinstance(times, list):
            timestamps.extend(float(item) for item in times)
        raw = str(result.get("raw") or "")
        if raw:
            raw_batches.append(raw[:6000])
    segments.sort(key=lambda item: float(item.get("start_time", item.get("timestamp", 0)) or 0))
    deduplicated: list[dict[str, object]] = []
    for segment in segments:
        current = dict(segment)
        current_summary = str(current.get("summary") or "").strip().casefold()
        current_time = float(current.get("start_time", current.get("timestamp", 0)) or 0)
        if deduplicated:
            previous = deduplicated[-1]
            previous_summary = str(previous.get("summary") or "").strip().casefold()
            previous_time = float(previous.get("start_time", previous.get("timestamp", 0)) or 0)
            previous_end = float(previous.get("end_time", previous_time) or previous_time)
            touches_previous = current_time <= previous_end + 0.25
            if current_summary and current_summary == previous_summary and touches_previous:
                previous["end_time"] = max(
                    previous_end,
                    float(current.get("end_time", current_time) or current_time),
                )
                previous["keywords"] = _unique(
                    _string_list(previous.get("keywords")) + _string_list(current.get("keywords")),
                    12,
                )
                continue
        deduplicated.append(current)

    unique_times = sorted({round(float(item), 3) for item in timestamps})
    merged = aggregate_analysis_segments(
        deduplicated,
        unique_times,
    )
    merged["timestamps"] = [float(item["timestamp"]) for item in merged["segments"]]
    merged["raw_batches"] = raw_batches
    return merged


def parse_model_json(raw: str, timestamps: list[float] | None = None) -> dict[str, object]:
    try:
        payload = parse_json_value(raw)
    except ValueError:
        return aggregate_analysis_segments(
            [{"summary": raw[:500], "keywords": []}], timestamps
        )
    explicit_summary = ""
    if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
        explicit_summary = str(payload.get("summary") or "")
        items = payload["segments"]
        return aggregate_analysis_segments(items, timestamps, summary=explicit_summary)
    if isinstance(payload, list):
        return aggregate_analysis_segments(
            [item for item in payload if isinstance(item, dict)], timestamps
        )
    if isinstance(payload, dict):
        payload.setdefault("summary", raw[:300])
        return aggregate_analysis_segments([payload], timestamps)
    return aggregate_analysis_segments(
        [{"summary": raw[:500], "keywords": []}], timestamps
    )


def parse_json_value(raw: str) -> object:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        for opening, closing in (("[", "]"), ("{", "}")):
            start = cleaned.find(opening)
            end = cleaned.rfind(closing)
            if start >= 0 and end > start:
                try:
                    return json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    continue
    raise ValueError("JoyAI 未返回有效的分镜 JSON")


def adaptive_analysis_strategy(asset: object) -> dict[str, object]:
    def value(name: str, default: object = None) -> object:
        try:
            return asset[name]  # type: ignore[index]
        except (KeyError, TypeError, IndexError):
            return getattr(asset, name, default)

    media_type = str(value("media_type", "image") or "image")
    duration = max(0.0, float(value("duration", 0) or 0))
    width = int(value("width", 0) or 0)
    height = int(value("height", 0) or 0)
    fps = float(value("fps", 0) or 0)
    codec = str(value("codec", "") or "")
    size_bytes = int(value("size_bytes", 0) or 0)
    bit_rate = int(value("bit_rate", 0) or 0)
    if bit_rate <= 0 and duration > 0 and size_bytes > 0:
        bit_rate = round(size_bytes * 8 / duration)
    source_notes: list[str] = []
    if max(width, height) >= 3840:
        source_notes.append("4K源临时缩放")
    if fps >= 50:
        source_notes.append("高帧率源降采样")
    if codec:
        source_notes.append(codec.upper())
    if bit_rate >= 80_000_000:
        source_notes.append("高码率源轻量化")
    heavy_codecs = {"hevc", "h265", "av1", "prores", "dnxhd", "dnxhr"}
    heavy_source = (
        max(width, height) >= 3840
        or fps >= 50
        or bit_rate >= 80_000_000
        or codec.casefold() in heavy_codecs
    )

    base = {
        "analysis_version": 3,
        "routing_policy_version": 2,
        "source_duration": round(duration, 3),
        "source_resolution": f"{width}x{height}" if width and height else "unknown",
        "source_fps": round(fps, 3) if fps else None,
        "source_codec": codec or None,
        "source_bit_rate": bit_rate or None,
        "heavy_source": heavy_source,
        "preprocess": "512px H.264 临时片段" if media_type == "video" else "原图缩放",
        "max_pixels": 262144,
        "decode_fps": 3.0 if heavy_source else 4.0,
        "notes": source_notes,
    }
    if media_type != "video":
        return {
            **base,
            "id": "image_direct",
            "label": "单图直接理解",
            "mode": "image",
            "reason": "图片直接送入视觉模型",
            "chunk_seconds": None,
            "overlap_seconds": 0.0,
            "sample_fps": None,
            "max_frames": 1,
        }
    if duration <= 0:
        return {
            **base,
            "id": "frame_fallback",
            "label": "抽帧兼容模式",
            "mode": "frames",
            "reason": "素材时长不可用",
            "chunk_seconds": None,
            "overlap_seconds": 0.0,
            "sample_fps": None,
            "max_frames": 12,
        }
    if duration <= 30:
        return {
            **base,
            "id": "video_whole_short",
            "label": "短视频整段理解",
            "mode": "video",
            "reason": "30秒以内保留完整动作连续性",
            "chunk_seconds": round(duration, 3),
            "overlap_seconds": 0.0,
            "sample_fps": 2.0,
            "max_frames": min(64, max(4, math.ceil(duration * 2))),
        }
    if duration <= 120 and not heavy_source:
        sample_fps = min(1.5, max(0.5, 96 / duration))
        return {
            **base,
            "id": "video_whole_medium",
            "label": "中短视频限帧理解",
            "mode": "video",
            "reason": "2分钟以内整段理解并限制视觉帧数",
            "chunk_seconds": round(duration, 3),
            "overlap_seconds": 0.0,
            "sample_fps": round(sample_fps, 3),
            "max_frames": min(96, max(24, math.ceil(duration * sample_fps))),
        }
    if duration <= 120:
        return {
            **base,
            "id": "video_chunked_medium_heavy",
            "label": "高规格中视频60秒分段",
            "mode": "video",
            "reason": "4K、高帧率、高码率或复杂编码素材按60秒轻量化分段",
            "chunk_seconds": 60.0,
            "overlap_seconds": 2.0,
            "sample_fps": 1.0,
            "max_frames": 60,
        }
    if duration <= 600:
        return {
            **base,
            "id": "video_chunked_long",
            "label": "长视频60秒分段",
            "mode": "video",
            "reason": "2到10分钟按60秒分段理解",
            "chunk_seconds": 60.0,
            "overlap_seconds": 2.0,
            "sample_fps": 1.0,
            "max_frames": 60,
        }
    return {
        **base,
        "id": "video_chunked_xlong",
        "label": "超长视频90秒分段",
        "mode": "video",
        "reason": "10分钟以上按90秒低采样分段理解",
        "chunk_seconds": 90.0,
        "overlap_seconds": 3.0,
        "sample_fps": 0.75,
        "max_frames": 68,
    }


def video_segment_ranges(
    duration: float,
    chunk_seconds: float,
    overlap_seconds: float = 0.0,
) -> list[tuple[float, float]]:
    total = max(0.0, float(duration))
    chunk = max(1.0, float(chunk_seconds))
    overlap = max(0.0, min(float(overlap_seconds), chunk / 3))
    if total <= chunk:
        return [(0.0, round(total, 3))]
    ranges: list[tuple[float, float]] = []
    start = 0.0
    while start < total:
        end = min(total, start + chunk)
        if total - end < max(5.0, overlap * 2):
            end = total
        ranges.append((round(start, 3), round(end, 3)))
        if end >= total:
            break
        start = max(start + 0.1, end - overlap)
    return ranges


def offset_video_analysis(
    result: dict[str, object],
    clip_start: float,
    clip_end: float,
) -> dict[str, object]:
    start = float(clip_start)
    end = max(start, float(clip_end))
    duration = end - start
    segments = result.get("segments")
    normalized: list[dict[str, object]] = []
    if isinstance(segments, list):
        for item in segments:
            if not isinstance(item, dict):
                continue
            segment = dict(item)
            for field in ("timestamp", "start_time", "end_time"):
                if field != "timestamp" and field not in segment:
                    continue
                try:
                    relative = float(segment.get(field, 0) or 0)
                except (TypeError, ValueError):
                    relative = 0.0
                segment[field] = round(start + min(duration, max(0.0, relative)), 3)
            normalized.append(segment)
    normalized.sort(
        key=lambda item: float(item.get("start_time", item.get("timestamp", start)) or start)
    )
    for index, segment in enumerate(normalized):
        segment_start = float(segment.get("start_time", segment.get("timestamp", start)) or start)
        next_start = (
            float(normalized[index + 1].get("start_time", normalized[index + 1].get("timestamp", end)) or end)
            if index + 1 < len(normalized) else end
        )
        segment_end = float(segment.get("end_time", next_start) or next_start)
        segment["start_time"] = round(max(start, min(end, segment_start)), 3)
        segment["end_time"] = round(max(float(segment["start_time"]), min(end, segment_end)), 3)
        segment["timestamp"] = round(float(segment["start_time"]), 3)
    result["segments"] = normalized
    result["timestamps"] = [float(item["timestamp"]) for item in normalized]
    result["analysis_version"] = 3
    return result


def trim_analysis_to_window(
    result: dict[str, object],
    keep_start: float,
    keep_end: float,
    *,
    include_end: bool = False,
) -> dict[str, object]:
    """Assign an overlapped video result to its non-overlapping ownership window."""
    start = float(keep_start)
    end = max(start, float(keep_end))
    segments = result.get("segments")
    if not isinstance(segments, list):
        return result
    kept: list[dict[str, object]] = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        segment = dict(item)
        timestamp = float(segment.get("timestamp", segment.get("start_time", start)) or start)
        segment_start = float(segment.get("start_time", timestamp) or timestamp)
        segment_end = float(segment.get("end_time", segment_start) or segment_start)
        is_interval = segment_end > segment_start
        if is_interval:
            if segment_end <= start or segment_start >= end:
                continue
        elif timestamp < start or timestamp > end or (timestamp == end and not include_end):
            continue
        owned_start = max(start, min(end, segment_start))
        segment["timestamp"] = round(owned_start if is_interval else timestamp, 3)
        if "start_time" in segment:
            segment["start_time"] = round(owned_start, 3)
        if "end_time" in segment:
            segment["end_time"] = round(max(
                owned_start,
                min(end, segment_end),
            ), 3)
        kept.append(segment)
    result["segments"] = kept
    result["timestamps"] = [float(item["timestamp"]) for item in kept]
    return result


def prepare_analysis_video_clip(
    source: Path,
    target: Path,
    *,
    start_time: float,
    end_time: float,
    ffmpeg: str = "ffmpeg",
    decode_fps: float = 4.0,
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    clip_duration = max(0.1, float(end_time) - float(start_time))
    software_filter = (
        f"fps={max(2.0, min(8.0, float(decode_fps))):.3f},"
        "scale='min(512,iw)':'min(512,ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    common = [
        ffmpeg, "-y", "-loglevel", "error", "-ss", str(max(0.0, float(start_time))),
        "-i", str(source), "-t", str(clip_duration), "-map", "0:v:0", "-an",
    ]
    software_command = common + [
        "-vf", software_filter, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target),
    ]
    commands: list[list[str]] = []
    vaapi_device = os.environ.get("FFMPEG_VAAPI_DEVICE", "").strip()
    if vaapi_device and Path(vaapi_device).exists():
        hardware_filter = (
            f"fps={max(2.0, min(8.0, float(decode_fps))):.3f},"
            "scale_vaapi=w=512:h=512:force_original_aspect_ratio=decrease:format=nv12"
        )
        commands.append([
            ffmpeg, "-y", "-loglevel", "error", "-hwaccel", "vaapi",
            "-hwaccel_device", vaapi_device, "-hwaccel_output_format", "vaapi",
            "-ss", str(max(0.0, float(start_time))), "-i", str(source),
            "-t", str(clip_duration), "-map", "0:v:0", "-an", "-vf", hardware_filter,
            "-c:v", "h264_vaapi", "-qp", "28", "-movflags", "+faststart", str(target),
        ])
    commands.append(software_command)
    timeout = int(min(900, max(120, clip_duration * 5)))
    last_error: Exception | None = None
    for command in commands:
        target.unlink(missing_ok=True)
        try:
            subprocess.run(command, timeout=timeout, check=True, capture_output=True)
            if target.is_file() and target.stat().st_size:
                break
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
    else:
        target.unlink(missing_ok=True)
        raise RuntimeError("无法生成临时视频分析片段") from last_error
    if not target.is_file() or not target.stat().st_size:
        raise RuntimeError("临时视频分析片段为空")
    return target


def recommended_frame_count(duration: float | None) -> int:
    seconds = max(0.0, float(duration or 0))
    if seconds <= 30:
        return 4
    if seconds <= 120:
        return 6
    if seconds <= 300:
        return 10
    if seconds <= 900:
        return min(18, max(12, math.ceil(seconds / 60) + 4))
    return min(24, max(18, math.ceil(seconds / 90) + 8))


def analysis_frame_timestamps(duration: float | None, frame_count: int | None = None) -> list[float]:
    seconds = float(duration or 0)
    if seconds <= 1:
        return [0.0]
    count = max(1, min(frame_count or recommended_frame_count(seconds), 24))
    if count == 1:
        return [round(seconds * 0.5, 3)]
    start = max(0.2, seconds * 0.04)
    end = min(seconds - 0.2, seconds * 0.96)
    step = (end - start) / (count - 1)
    return [round(start + step * index, 3) for index in range(count)]


def extract_analysis_frames(
    source: Path,
    media_type: str,
    duration: float | None,
    *,
    ffmpeg: str = "ffmpeg",
    frame_count: int | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
) -> tuple[tempfile.TemporaryDirectory[str], list[Path], list[float]]:
    temp_dir = tempfile.TemporaryDirectory(prefix="framefind-joyai-")
    output_dir = Path(temp_dir.name)
    if media_type == "video" and duration and duration > 1:
        if start_time is not None or end_time is not None:
            segment_start = max(0.0, float(start_time or 0))
            segment_end = min(float(duration), float(end_time if end_time is not None else duration))
            segment_duration = max(0.1, segment_end - segment_start)
            relative = analysis_frame_timestamps(segment_duration, frame_count or 4)
            timestamps = [round(segment_start + value, 3) for value in relative]
        else:
            timestamps = analysis_frame_timestamps(duration, frame_count)
    else:
        timestamps = [0.0]

    images: list[Path] = []
    successful_timestamps: list[float] = []
    for index, timestamp in enumerate(timestamps):
        target = output_dir / f"{index:02d}.jpg"
        command = [ffmpeg, "-y", "-loglevel", "error"]
        if media_type == "video":
            command += ["-ss", str(timestamp)]
        command += [
            "-i", str(source),
            "-vf", "scale='min(512,iw)':'min(512,ih)':force_original_aspect_ratio=decrease",
            "-frames:v", "1", "-q:v", "3", str(target),
        ]
        try:
            subprocess.run(command, timeout=90, check=True, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            continue
        if target.is_file() and target.stat().st_size:
            images.append(target)
            successful_timestamps.append(timestamp)
    if not images:
        temp_dir.cleanup()
        raise RuntimeError("无法从素材读取分析画面")
    return temp_dir, images, successful_timestamps
