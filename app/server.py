from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.database import Database
    from app.joyai import (
        JoyAIClient, adaptive_analysis_strategy, extract_analysis_frames,
        merge_analysis_results, prepare_analysis_video_clip, trim_analysis_to_window,
        video_segment_ranges,
    )
    from app.paths import normalize_smb_url, resolve_project_root, windows_unc_path
    from app.nas import NasFileStationClient
    from app.scanner import ProjectScanner
    from app.shot_matcher import fallback_shots, match_shots, normalize_shots
    from app.storyboards import (
        export_csv, export_json, export_markdown, parse_script_file, parse_shot_file,
    )
    from app.text_models import TextModelClient
else:
    from .database import Database
    from .joyai import (
        JoyAIClient, adaptive_analysis_strategy, extract_analysis_frames,
        merge_analysis_results, prepare_analysis_video_clip, trim_analysis_to_window,
        video_segment_ranges,
    )
    from .paths import normalize_smb_url, resolve_project_root, windows_unc_path
    from .nas import NasFileStationClient
    from .scanner import ProjectScanner
    from .shot_matcher import fallback_shots, match_shots, normalize_shots
    from .storyboards import (
        export_csv, export_json, export_markdown, parse_script_file, parse_shot_file,
    )
    from .text_models import TextModelClient


BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = Path(os.environ.get("MEDIA_APP_DATA_DIR", BASE_DIR / "data")).expanduser().resolve()
DB = Database(DATA_DIR / "media_workbench.db")
SCANNER = ProjectScanner(DB)
JOYAI = JoyAIClient()
NAS = NasFileStationClient()
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-task")
THUMB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="thumbnail-task")

TEXT_PROVIDER_DEFAULTS = {
    "local": {
        "endpoint": os.environ.get("TEXT_LLM_ENDPOINT", "http://127.0.0.1:7061/v1"),
        "model": os.environ.get("TEXT_LLM_MODEL", "Qwen3-4B"),
    },
    "deepseek": {
        "endpoint": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
    },
}

VALID_ANALYSIS_EXEMPTION_SQL = (
    "analysis_exempted_at IS NOT NULL "
    "AND analysis_exempt_fingerprint IS NOT NULL "
    "AND analysis_exempt_fingerprint=content_fingerprint"
)


def row_to_dict(row):
    return dict(row) if row else None


def public_asset(row) -> dict:
    payload = row_to_dict(row)
    payload["analysis_exempted"] = bool(
        row["analysis_exempted_at"]
        and row["analysis_exempt_fingerprint"]
        and row["analysis_exempt_fingerprint"] == row["content_fingerprint"]
    )
    if not payload["analysis_exempted"]:
        payload["analysis_exempted_at"] = None
        payload["analysis_exempt_reason"] = None
    for field in (
        "content_fingerprint", "analysis_exempt_fingerprint",
        "thumb_path", "analysis_json", "root_path",
    ):
        payload.pop(field, None)
    try:
        payload["analysis_keywords"] = json.loads(payload.get("analysis_keywords") or "[]")
    except json.JSONDecodeError:
        payload["analysis_keywords"] = []
    payload["analysis_segments"] = []
    payload["analysis_profile"] = None
    try:
        analysis = json.loads(row["analysis_json"] or "{}")
        segments = analysis.get("segments", []) if isinstance(analysis, dict) else []
        if isinstance(segments, list):
            allowed = {
                "timestamp", "start_time", "end_time", "summary", "keywords",
                "shot_type", "subjects", "scene", "actions", "usable",
            }
            payload["analysis_segments"] = [
                {key: value for key, value in segment.items() if key in allowed}
                for segment in segments if isinstance(segment, dict)
            ]
        strategy = analysis.get("strategy") if isinstance(analysis, dict) else None
        if isinstance(strategy, dict):
            allowed_strategy = {
                "id", "label", "mode", "reason", "chunk_seconds", "overlap_seconds",
                "sample_fps", "max_frames", "source_duration", "source_resolution",
                "source_fps", "source_codec", "source_bit_rate", "heavy_source",
                "preprocess", "routing_policy_version", "fallback_segments",
                "segment_count", "notes",
            }
            payload["analysis_profile"] = {
                key: value for key, value in strategy.items() if key in allowed_strategy
            }
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return payload


def validate_root(root_path: str) -> Path:
    return resolve_project_root(root_path)


def text_model_config() -> dict[str, object]:
    with DB.session() as conn:
        row = conn.execute(
            "SELECT * FROM model_settings WHERE role='text_planner'"
        ).fetchone()
    if row:
        config = row_to_dict(row)
    else:
        defaults = TEXT_PROVIDER_DEFAULTS["local"]
        config = {
            "role": "text_planner",
            "provider": "local",
            "endpoint": defaults["endpoint"],
            "model": defaults["model"],
            "api_key": os.environ.get("TEXT_LLM_API_KEY", ""),
            "updated_at": None,
        }
    return config


def text_model_client(config: dict[str, object] | None = None) -> TextModelClient:
    current = config or text_model_config()
    return TextModelClient(
        endpoint=str(current.get("endpoint") or ""),
        model=str(current.get("model") or ""),
        api_key=str(current.get("api_key") or ""),
        provider=str(current.get("provider") or "custom"),
    )


def public_text_model_config(config: dict[str, object], status: dict | None = None) -> dict:
    payload = {
        "provider": config.get("provider"),
        "endpoint": config.get("endpoint"),
        "model": config.get("model"),
        "api_key_configured": bool(config.get("api_key")),
        "updated_at": config.get("updated_at"),
    }
    if status:
        payload.update(status)
    return payload


def validate_text_model_payload(payload: dict, *, existing_key: str = "") -> dict[str, str]:
    provider = str(payload.get("provider") or "local").strip().casefold()
    if provider not in {"local", "deepseek", "custom"}:
        raise ValueError("文案模型供应商无效")
    defaults = TEXT_PROVIDER_DEFAULTS.get(provider, {})
    endpoint = str(payload.get("endpoint") or defaults.get("endpoint") or "").strip().rstrip("/")
    model = str(payload.get("model") or defaults.get("model") or "").strip()
    if provider in {"local", "deepseek"}:
        endpoint = str(defaults["endpoint"])
    if not endpoint.startswith(("http://", "https://")):
        raise ValueError("模型接口地址必须以 http:// 或 https:// 开头")
    if not model:
        raise ValueError("模型名称不能为空")
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key and not payload.get("clear_api_key"):
        api_key = existing_key
    if provider == "local":
        api_key = ""
    if provider == "deepseek" and not api_key:
        raise ValueError("DeepSeek API Key 不能为空")
    if len(api_key) > 1024:
        raise ValueError("API Key 长度异常")
    return {"provider": provider, "endpoint": endpoint, "model": model, "api_key": api_key}


def _json_array(value: object) -> list:
    try:
        parsed = json.loads(str(value or "[]"))
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def storyboard_detail(conn, storyboard_id: int) -> dict | None:
    board = conn.execute("SELECT * FROM storyboards WHERE id=?", (storyboard_id,)).fetchone()
    if not board:
        return None
    payload = row_to_dict(board)
    rows = conn.execute(
        "SELECT * FROM storyboard_shots WHERE storyboard_id=? ORDER BY position",
        (storyboard_id,),
    ).fetchall()
    shots = []
    for row in rows:
        shot = row_to_dict(row)
        shot["keywords"] = _json_array(shot.pop("keywords_json", "[]"))
        shot["candidates"] = _json_array(shot.pop("candidates_json", "[]"))
        for candidate in shot["candidates"]:
            if str(candidate.get("smb_path", "")).casefold().startswith("smb://"):
                candidate["smb_path"] = windows_unc_path(candidate["smb_path"])
        shot["selected_candidate"] = next(
            (
                item for item in shot["candidates"]
                if item.get("asset_id") == shot.get("selected_asset_id")
            ),
            None,
        )
        shots.append(shot)
    payload["shots"] = shots
    version_rows = conn.execute(
        """SELECT id, version_number, planner, warning, created_at
           FROM storyboard_versions WHERE storyboard_id=?
           ORDER BY version_number DESC""",
        (storyboard_id,),
    ).fetchall()
    payload["versions"] = [row_to_dict(row) for row in version_rows]
    return payload


def delete_storyboard(conn, storyboard_id: int) -> dict | None:
    """Delete one storyboard and report the data removed by DB cascades.

    This is deliberately independent of material readiness: the scan/analysis
    gate protects authoring and matching, while deleting obsolete historical
    work is a project-management operation.
    """
    board = conn.execute(
        "SELECT id, project_id, title FROM storyboards WHERE id=?",
        (storyboard_id,),
    ).fetchone()
    if not board:
        return None
    shot_count = int(conn.execute(
        "SELECT COUNT(*) FROM storyboard_shots WHERE storyboard_id=?",
        (storyboard_id,),
    ).fetchone()[0])
    version_count = int(conn.execute(
        "SELECT COUNT(*) FROM storyboard_versions WHERE storyboard_id=?",
        (storyboard_id,),
    ).fetchone()[0])
    conn.execute("DELETE FROM storyboards WHERE id=?", (storyboard_id,))
    remaining_count = int(conn.execute(
        "SELECT COUNT(*) FROM storyboards WHERE project_id=?",
        (board["project_id"],),
    ).fetchone()[0])
    return {
        "deleted_id": int(board["id"]),
        "project_id": int(board["project_id"]),
        "title": str(board["title"]),
        "shot_count": shot_count,
        "version_count": version_count,
        "remaining_count": remaining_count,
    }


def storyboard_readiness(conn, project_id: int) -> dict | None:
    """Return the project-level prerequisites for storyboard authoring.

    Historical storyboards stay readable while this gate is closed, but every
    authoring mutation that can change the working storyboard must use this
    result as the source of truth. Deleting an obsolete storyboard is an
    independent management operation and remains available.
    """
    project = conn.execute(
        "SELECT id, last_scan_at FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if not project:
        return None

    counts_row = conn.execute(
        f"""SELECT
             COUNT(*) AS active,
             SUM(CASE WHEN analysis_status='completed'
                       AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL}) THEN 1 ELSE 0 END) AS completed,
             SUM(CASE WHEN analysis_status='pending'
                       AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL}) THEN 1 ELSE 0 END) AS pending,
             SUM(CASE WHEN analysis_status='processing'
                       AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL}) THEN 1 ELSE 0 END) AS processing,
             SUM(CASE WHEN analysis_status='failed'
                       AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL}) THEN 1 ELSE 0 END) AS failed,
             SUM(CASE WHEN {VALID_ANALYSIS_EXEMPTION_SQL} THEN 1 ELSE 0 END) AS exempted
           FROM assets WHERE project_id=? AND status='active'""",
        (project_id,),
    ).fetchone()
    counts = {
        key: int(counts_row[key] or 0)
        for key in (
            "active", "completed", "pending", "processing", "failed", "exempted"
        )
    }
    counts["required"] = max(0, counts["active"] - counts["exempted"])
    counts["remaining"] = max(
        0, counts["active"] - counts["completed"] - counts["exempted"]
    )
    counts["other"] = max(
        0,
        counts["remaining"]
        - counts["pending"]
        - counts["processing"]
        - counts["failed"],
    )

    task_rows = conn.execute(
        """SELECT id, task_type, status FROM tasks
           WHERE project_id=? AND task_type IN ('scan','analysis')
             AND status IN ('queued','running','pausing','paused')
           ORDER BY id""",
        (project_id,),
    ).fetchall()
    active_tasks = {"scan": [], "analysis": []}
    for task in task_rows:
        active_tasks[task["task_type"]].append(int(task["id"]))

    latest_scan = conn.execute(
        """SELECT id, status, created_at, finished_at FROM tasks
           WHERE project_id=? AND task_type='scan' ORDER BY id DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    last_scan_at = project["last_scan_at"]
    scan_idle = not active_tasks["scan"]
    analysis_idle = not active_tasks["analysis"]
    latest_scan_failed = bool(latest_scan and latest_scan["status"] == "failed")
    scanned = bool(last_scan_at) and not latest_scan_failed
    has_assets = counts["active"] > 0
    all_assets_analyzed = (
        has_assets and counts["completed"] > 0 and counts["remaining"] == 0
    )

    requirements = {
        "scan_completed": scanned,
        "scan_idle": scan_idle,
        "analysis_idle": analysis_idle,
        "has_assets": has_assets,
        "all_assets_analyzed": all_assets_analyzed,
    }
    blockers: list[dict[str, str]] = []
    if active_tasks["scan"]:
        blockers.append({
            "code": "scan_running",
            "message": "素材扫描尚未完成。",
        })
    elif latest_scan_failed:
        blockers.append({
            "code": "scan_failed",
            "message": "最近一次素材扫描失败，请重新扫描。",
        })
    elif not last_scan_at:
        blockers.append({
            "code": "scan_required",
            "message": "项目还没有完成首次素材扫描。",
        })
    elif not has_assets:
        blockers.append({
            "code": "no_assets",
            "message": "扫描已完成，但没有发现可用素材。",
        })

    if last_scan_at and scan_idle and not latest_scan_failed and has_assets:
        if active_tasks["analysis"]:
            blockers.append({
                "code": "analysis_running",
                "message": "JoyAI 素材分析尚未完成。",
            })
        if counts["failed"]:
            blockers.append({
                "code": "analysis_failed",
                "message": f"仍有 {counts['failed']} 条素材分析失败。",
            })
        if counts["remaining"] > counts["failed"]:
            blockers.append({
                "code": "analysis_required",
                "message": f"仍有 {counts['remaining']} 条素材尚未完成分析。",
            })
        if counts["completed"] == 0 and counts["exempted"] == counts["active"]:
            blockers.append({
                "code": "no_analyzed_assets",
                "message": "全部素材都已排除，没有可用于分镜匹配的分析结果。",
            })

    ready = all(requirements.values())
    if ready:
        state = "ready"
        next_action = None
        message = (
            f"素材准备完成：{counts['completed']} 条已分析、"
            f"{counts['exempted']} 条异常素材已人工排除，可以生成并匹配分镜。"
            if counts["exempted"] else
            "素材扫描与分析均已完成，可以生成并匹配分镜。"
        )
    elif active_tasks["scan"]:
        state = "scan_running"
        next_action = "tasks"
        message = "素材扫描正在进行，完成后才能使用分镜工作区。"
    elif latest_scan_failed:
        state = "scan_failed"
        next_action = "scan"
        message = "最近一次素材扫描失败，请重新扫描后再使用分镜工作区。"
    elif not last_scan_at:
        state = "scan_required"
        next_action = "scan"
        message = "请先刷新素材，完成项目扫描后才能使用分镜工作区。"
    elif not has_assets:
        state = "no_assets"
        next_action = "scan"
        message = "没有发现可用素材，请检查项目路径后重新扫描。"
    elif active_tasks["analysis"]:
        state = "analysis_running"
        next_action = "tasks"
        message = (
            f"JoyAI 正在分析素材：已完成 {counts['completed']} / "
            f"{counts['active']}，结束后自动解锁。"
        )
    elif counts["failed"]:
        state = "analysis_failed"
        next_action = "analysis"
        message = (
            f"已有 {counts['completed']} 条素材完成分析，"
            f"{counts['exempted']} 条已排除，仍有 {counts['failed']} 条失败待处理。"
        )
    elif counts["completed"] == 0 and counts["exempted"] == counts["active"]:
        state = "no_analyzed_assets"
        next_action = "analysis"
        message = "全部素材都已排除，请至少恢复并成功分析一条素材后再使用分镜匹配。"
    else:
        state = "analysis_required"
        next_action = "analysis"
        message = (
            f"已有 {counts['completed']} / {counts['active']} 条素材完成分析，"
            f"还需分析 {counts['remaining']} 条。"
        )

    return {
        "ready": ready,
        "state": state,
        "message": message,
        "next_action": next_action,
        "last_scan_at": last_scan_at,
        "requirements": requirements,
        "counts": counts,
        "blockers": blockers,
        "active_tasks": active_tasks,
        "latest_scan": row_to_dict(latest_scan),
    }


def storyboard_gate_error(readiness: dict) -> dict:
    return {
        "error": readiness["message"],
        "code": "storyboard_not_ready",
        "readiness": readiness,
    }


def insert_storyboard_shots(conn, storyboard_id: int, shots: list[dict]) -> None:
    conn.execute("DELETE FROM storyboard_shots WHERE storyboard_id=?", (storyboard_id,))
    for index, shot in enumerate(shots, 1):
        conn.execute(
            """INSERT INTO storyboard_shots(
                 storyboard_id, position, narration, visual_request, shot_type,
                 keywords_json, candidates_json, selected_asset_id, notes
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                storyboard_id,
                index,
                str(shot.get("narration", "")),
                str(shot.get("visual_request", "")),
                str(shot.get("shot_type", "")),
                json.dumps(shot.get("keywords", []), ensure_ascii=False),
                json.dumps(shot.get("candidates", []), ensure_ascii=False),
                shot.get("selected_asset_id"),
                str(shot.get("notes", "")),
            ),
        )
    conn.execute(
        "UPDATE storyboards SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (storyboard_id,)
    )


def snapshot_storyboard_shots(shots: list[dict]) -> list[dict]:
    defaults = {
        "narration": "", "visual_request": "", "shot_type": "",
        "keywords": [], "candidates": [], "selected_asset_id": None, "notes": "",
    }
    return [
        {field: shot.get(field, default) for field, default in defaults.items()}
        for shot in shots
    ]


def create_storyboard_version(
    conn,
    storyboard_id: int,
    shots: list[dict],
    *,
    planner: str,
    warning: str = "",
    activate: bool = True,
) -> int:
    next_number = int(conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) + 1 FROM storyboard_versions WHERE storyboard_id=?",
        (storyboard_id,),
    ).fetchone()[0])
    cur = conn.execute(
        """INSERT INTO storyboard_versions(
             storyboard_id, version_number, planner, warning, shots_json
           ) VALUES (?, ?, ?, ?, ?)""",
        (
            storyboard_id, next_number, planner, warning,
            json.dumps(snapshot_storyboard_shots(shots), ensure_ascii=False),
        ),
    )
    version_id = int(cur.lastrowid)
    if activate:
        conn.execute(
            "UPDATE storyboards SET active_version_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (version_id, storyboard_id),
        )
    return version_id


def ensure_legacy_storyboard_version(conn, storyboard_id: int) -> int | None:
    existing = conn.execute(
        "SELECT id FROM storyboard_versions WHERE storyboard_id=? LIMIT 1", (storyboard_id,)
    ).fetchone()
    if existing:
        return int(existing["id"])
    board = storyboard_detail(conn, storyboard_id)
    if not board or not board["shots"]:
        return None
    return create_storyboard_version(
        conn, storyboard_id, board["shots"], planner="历史分镜", activate=True
    )


def activate_storyboard_version(conn, storyboard_id: int, version_id: int) -> bool:
    version = conn.execute(
        "SELECT shots_json FROM storyboard_versions WHERE id=? AND storyboard_id=?",
        (version_id, storyboard_id),
    ).fetchone()
    if not version:
        return False
    try:
        shots = json.loads(version["shots_json"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("该分镜版本数据已损坏")
    if not isinstance(shots, list):
        raise ValueError("该分镜版本格式无效")
    insert_storyboard_shots(conn, storyboard_id, shots)
    conn.execute(
        "UPDATE storyboards SET active_version_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (version_id, storyboard_id),
    )
    return True


def sync_active_version_selection(
    conn, storyboard_id: int, position: int, selected_asset_id: int
) -> None:
    board = conn.execute(
        "SELECT active_version_id FROM storyboards WHERE id=?", (storyboard_id,)
    ).fetchone()
    if not board or not board["active_version_id"]:
        return
    version = conn.execute(
        "SELECT shots_json FROM storyboard_versions WHERE id=? AND storyboard_id=?",
        (board["active_version_id"], storyboard_id),
    ).fetchone()
    if not version:
        return
    shots = json.loads(version["shots_json"])
    if not isinstance(shots, list) or position < 1 or position > len(shots):
        return
    shots[position - 1]["selected_asset_id"] = selected_asset_id
    conn.execute(
        "UPDATE storyboard_versions SET shots_json=? WHERE id=?",
        (json.dumps(shots, ensure_ascii=False), board["active_version_id"]),
    )


def wait_if_paused(task_id: int) -> None:
    while True:
        with DB.session() as conn:
            task = conn.execute(
                "SELECT pause_requested, status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not task or not task["pause_requested"]:
                return
            if task["status"] != "paused":
                conn.execute(
                    """UPDATE tasks SET status='paused',
                       paused_at=COALESCE(paused_at, CURRENT_TIMESTAMP) WHERE id=?""",
                    (task_id,),
                )
        time.sleep(0.5)


def task_payload(row) -> dict:
    payload = row_to_dict(row)
    payload["estimated_finished_at"] = None
    payload["estimated_seconds_remaining"] = None
    progress = float(payload.get("progress") or 0)
    started_at = payload.get("started_at")
    if not started_at or progress <= 0 or progress >= 1:
        return payload
    try:
        started = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        reference = now
        if payload.get("paused_at"):
            reference = datetime.strptime(
                payload["paused_at"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        elapsed = max(
            0.0,
            (reference - started).total_seconds()
            - float(payload.get("total_paused_seconds") or 0),
        )
        if elapsed < 1:
            return payload
        remaining = max(0, elapsed * (1 - progress) / progress)
        payload["estimated_seconds_remaining"] = round(remaining)
        if payload["status"] == "running":
            payload["estimated_finished_at"] = datetime.fromtimestamp(
                now.timestamp() + remaining, timezone.utc
            ).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        pass
    return payload


def load_task_options(task_id: int) -> dict[str, object]:
    with DB.session() as conn:
        task = conn.execute(
            "SELECT options_json FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    try:
        options = json.loads(task["options_json"] or "{}") if task else {}
    except (TypeError, json.JSONDecodeError):
        options = {}
    return options if isinstance(options, dict) else {}


def save_task_options(task_id: int, options: dict[str, object]) -> None:
    DB.update_task_options(task_id, json.dumps(options, ensure_ascii=False))


def normalize_analysis_options(payload: dict | None = None) -> dict[str, object]:
    raw = payload or {}
    scope = str(raw.get("scope") or ("all" if raw.get("reanalyze") else "pending")).casefold()
    if scope not in {"pending", "failed", "all"}:
        raise ValueError("分析范围无效")
    media_type = str(raw.get("media_type") or "all").casefold()
    if media_type not in {"all", "video", "image"}:
        raise ValueError("素材类型无效")
    folder_prefix = str(raw.get("folder_prefix") or "").strip().replace("\\", "/").strip("/")
    if any(part in {".", ".."} for part in folder_prefix.split("/") if part):
        raise ValueError("文件夹范围无效")

    def optional_number(name: str) -> float | None:
        value = raw.get(name)
        if value in {None, ""}:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("时长范围必须是秒数") from exc

    min_duration = optional_number("min_duration")
    max_duration = optional_number("max_duration")
    if min_duration is not None and max_duration is not None and max_duration < min_duration:
        raise ValueError("最大时长不能小于最小时长")
    raw_limit = raw.get("limit")
    if raw_limit in {None, "", 0, "0"}:
        limit = None
    else:
        try:
            limit = max(1, min(500, int(raw_limit)))
        except (TypeError, ValueError) as exc:
            raise ValueError("批次大小必须是整数") from exc
    return {
        "scope": scope,
        "media_type": media_type,
        "folder_prefix": folder_prefix,
        "min_duration": min_duration,
        "max_duration": max_duration,
        "limit": limit,
    }


def select_analysis_asset_ids(project_id: int, options: dict[str, object]) -> list[int]:
    clauses = [
        "project_id=?", "status='active'",
        f"NOT ({VALID_ANALYSIS_EXEMPTION_SQL})",
    ]
    values: list[object] = [project_id]
    scope = options.get("scope", "pending")
    if scope == "pending":
        clauses.append("analysis_status IN ('pending','failed','processing')")
    elif scope == "failed":
        clauses.append("analysis_status IN ('failed','processing')")
    media_type = options.get("media_type", "all")
    if media_type in {"video", "image"}:
        clauses.append("media_type=?")
        values.append(media_type)
    folder_prefix = str(options.get("folder_prefix") or "")
    if folder_prefix:
        clauses.append("(relative_path=? OR relative_path LIKE ?)")
        values.extend([folder_prefix, f"{folder_prefix}/%"])
    min_duration = options.get("min_duration")
    max_duration = options.get("max_duration")
    if min_duration is not None or max_duration is not None:
        clauses.append("media_type='video'")
    if min_duration is not None:
        clauses.append("COALESCE(duration, 0)>=?")
        values.append(min_duration)
    if max_duration is not None:
        clauses.append("COALESCE(duration, 0)<=?")
        values.append(max_duration)
    sql = f"SELECT id FROM assets WHERE {' AND '.join(clauses)} ORDER BY id"
    if options.get("limit"):
        sql += " LIMIT ?"
        values.append(options["limit"])
    with DB.session() as conn:
        rows = conn.execute(sql, values).fetchall()
    return [int(row["id"]) for row in rows]


def project_scan_job(task_id: int, project_id: int) -> None:
    DB.update_task(task_id, status="running", message="正在建立素材清单")
    try:
        def on_progress(value: float, message: str) -> None:
            DB.update_task(task_id, progress=value, message=message)
            wait_if_paused(task_id)

        result = SCANNER.scan(project_id, on_progress)
        message = (
            f"扫描完成：新增 {result['created']}，更新 {result['updated']}，"
            f"未变化 {result['unchanged']}，缺失 {result['missing']}"
        )
        DB.update_task(task_id, status="completed", progress=1.0, message=message)
        enqueue_thumbnail_task(project_id, reason="扫描完成，正在预热预览图")
    except Exception as exc:
        DB.update_task(task_id, status="failed", message=str(exc))


def analyze_frame_range(
    source: Path,
    asset,
    *,
    start_time: float | None = None,
    end_time: float | None = None,
    frame_count: int | None = None,
) -> dict[str, object]:
    temp_dir = None
    try:
        temp_dir, images, timestamps = extract_analysis_frames(
            source,
            asset["media_type"],
            asset["duration"],
            ffmpeg=os.environ.get("FFMPEG_BIN", "ffmpeg"),
            frame_count=frame_count,
            start_time=start_time,
            end_time=end_time,
        )
        batch_results = []
        for batch_start in range(0, len(images), 4):
            batch_images = images[batch_start:batch_start + 4]
            batch_timestamps = timestamps[batch_start:batch_start + 4]
            batch_results.append(JOYAI.analyze(batch_images, batch_timestamps))
        return merge_analysis_results(batch_results)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def analyze_asset_adaptively(
    source: Path,
    asset,
    on_segment=None,
    *,
    strategy: dict[str, object] | None = None,
) -> dict[str, object]:
    strategy = dict(strategy or adaptive_analysis_strategy(asset))
    if strategy["mode"] != "video":
        result = analyze_frame_range(source, asset)
        strategy["segment_count"] = 1
        strategy["fallback_segments"] = 0
        result["analysis_version"] = 3
        result["strategy"] = strategy
        return result

    duration = float(asset["duration"] or 0)
    ranges = video_segment_ranges(
        duration,
        float(strategy["chunk_seconds"] or duration),
        float(strategy["overlap_seconds"] or 0),
    )
    analysis_root = Path(
        os.environ.get("ANALYSIS_TEMP_DIR", DATA_DIR / "analysis-inputs")
    ).expanduser().resolve()
    analysis_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    fallback_errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix=f"asset-{asset['id']}-", dir=analysis_root) as directory:
        temp_root = Path(directory)
        for index, (segment_start, segment_end) in enumerate(ranges, 1):
            if on_segment:
                on_segment(index, len(ranges), segment_start, segment_end)
            target = temp_root / f"segment-{index:03d}.mp4"
            try:
                prepare_analysis_video_clip(
                    source,
                    target,
                    start_time=segment_start,
                    end_time=segment_end,
                    ffmpeg=os.environ.get("FFMPEG_BIN", "ffmpeg"),
                    decode_fps=float(strategy["decode_fps"] or 4),
                )
                result = JOYAI.analyze_video(
                    target,
                    clip_start=segment_start,
                    clip_end=segment_end,
                    sample_fps=min(
                        float(strategy["sample_fps"] or 1),
                        float(strategy["max_frames"] or 96)
                        / max(0.1, segment_end - segment_start),
                    ),
                )
            except Exception as exc:
                fallback_errors.append(f"{segment_start:.1f}-{segment_end:.1f}s: {exc}"[:500])
                result = analyze_frame_range(
                    source,
                    asset,
                    start_time=segment_start,
                    end_time=segment_end,
                    frame_count=4,
                )
            finally:
                target.unlink(missing_ok=True)
            keep_start = (
                (segment_start + ranges[index - 2][1]) / 2
                if index > 1 else segment_start
            )
            keep_end = (
                (segment_end + ranges[index][0]) / 2
                if index < len(ranges) else segment_end
            )
            trim_analysis_to_window(
                result,
                keep_start,
                keep_end,
                include_end=index == len(ranges),
            )
            results.append(result)

    merged = merge_analysis_results(results)
    strategy["segment_count"] = len(ranges)
    strategy["fallback_segments"] = len(fallback_errors)
    if fallback_errors:
        strategy["fallback_reason"] = fallback_errors[:8]
    merged["analysis_version"] = 3
    merged["strategy"] = strategy
    return merged


def project_analysis_job(
    task_id: int,
    project_id: int,
) -> None:
    DB.update_task(task_id, status="running", message="正在检查 JoyAI 服务")
    wait_if_paused(task_id)
    status = JOYAI.status()
    if not status["enabled"]:
        DB.update_task(task_id, status="failed", message=str(status["message"]))
        return
    try:
        options = load_task_options(task_id)
        if not options.get("asset_ids"):
            normalized = normalize_analysis_options(options)
            options.update(normalized)
            options["asset_ids"] = select_analysis_asset_ids(project_id, options)
            options["processed_ids"] = []
            save_task_options(task_id, options)
        asset_ids = [int(item) for item in options.get("asset_ids", [])]
        processed_ids = {int(item) for item in options.get("processed_ids", [])}
        remaining_ids = [asset_id for asset_id in asset_ids if asset_id not in processed_ids]
        if not asset_ids:
            DB.update_task(task_id, status="completed", progress=1.0, message="所选范围内没有待分析素材")
            return
        if not remaining_ids:
            DB.update_task(task_id, status="completed", progress=1.0, message="该批素材已全部处理")
            return
        remaining_set = set(remaining_ids)
        selected_set = set(asset_ids)
        with DB.session() as conn:
            project_assets = conn.execute(
                f"""SELECT a.*, p.root_path FROM assets a
                    JOIN projects p ON p.id=a.project_id
                    WHERE a.project_id=? AND a.status='active'
                      AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL})
                    ORDER BY a.id""",
                (project_id,),
            ).fetchall()
        selected_assets = [
            asset for asset in project_assets if int(asset["id"]) in selected_set
        ]
        assets = [
            asset for asset in selected_assets if int(asset["id"]) in remaining_set
        ]
        if not assets:
            DB.update_task(task_id, status="completed", progress=1.0, message="所选素材当前不可用")
            return

        saved_route_plan = (
            options.get("route_plan")
            if options.get("routing_policy_version") == 2
            and isinstance(options.get("route_plan"), dict)
            else {}
        )
        route_plan: dict[int, dict[str, object]] = {}
        route_fields = (
            "id", "label", "mode", "reason", "chunk_seconds",
            "overlap_seconds", "sample_fps", "max_frames", "decode_fps",
        )
        for item in selected_assets:
            item_strategy = adaptive_analysis_strategy(item)
            saved = saved_route_plan.get(str(item["id"]), {})
            if isinstance(saved, dict):
                for field in route_fields:
                    if field in saved:
                        item_strategy[field] = saved[field]
            item_ranges = (
                video_segment_ranges(
                    float(item["duration"] or 0),
                    float(item_strategy["chunk_seconds"] or item["duration"] or 1),
                    float(item_strategy["overlap_seconds"] or 0),
                )
                if item_strategy["mode"] == "video" else [(0.0, 0.0)]
            )
            computed_units = max(1, len(item_ranges))
            try:
                planned_units = max(1, int(saved.get("work_units", computed_units)))
            except (TypeError, ValueError, AttributeError):
                planned_units = computed_units
            route_plan[int(item["id"])] = {
                "strategy": item_strategy,
                "work_units": planned_units,
            }
        def planned_units_for(asset_id: int) -> int:
            if asset_id in route_plan:
                return int(route_plan[asset_id]["work_units"])
            saved = saved_route_plan.get(str(asset_id), {})
            try:
                return max(1, int(saved.get("work_units", 1)))
            except (TypeError, ValueError, AttributeError):
                return 1

        total_units = sum(planned_units_for(asset_id) for asset_id in asset_ids) or 1
        completed_units = sum(
            planned_units_for(asset_id) for asset_id in processed_ids
            if asset_id in selected_set
        )
        options["routing_policy_version"] = 2
        current_route_plan = {
            str(asset_id): {
                **{
                    field: plan["strategy"].get(field)
                    for field in route_fields
                },
                "work_units": int(plan["work_units"]),
            }
            for asset_id, plan in route_plan.items()
        }
        options["route_plan"] = {**saved_route_plan, **current_route_plan}
        save_task_options(task_id, options)

        completed = 0
        failed = 0
        total_assets = len(asset_ids)
        for asset in assets:
            wait_if_paused(task_id)
            asset_id = int(asset["id"])
            plan = route_plan[asset_id]
            strategy = plan["strategy"]
            asset_units = int(plan["work_units"])
            done_before = len(processed_ids)
            DB.update_task(
                task_id,
                progress=completed_units / total_units,
                message=(
                    f"{strategy['label']} · {asset['relative_path']}"
                    f"（{done_before + 1}/{total_assets}）"
                ),
            )
            with DB.session() as conn:
                conn.execute(
                    """UPDATE assets SET analysis_status='processing', analysis_error=NULL,
                       analysis_exempted_at=NULL, analysis_exempt_reason=NULL,
                       analysis_exempt_fingerprint=NULL WHERE id=?""",
                    (asset["id"],),
                )
            source = (Path(asset["root_path"]) / asset["relative_path"]).resolve()
            try:
                if not source.is_file():
                    raise RuntimeError("SMB 素材当前不可读取")

                def on_segment(index: int, segment_total: int, start: float, end: float) -> None:
                    wait_if_paused(task_id)
                    DB.update_task(
                        task_id,
                        progress=(completed_units + index - 1) / total_units,
                        message=(
                            f"{strategy['label']} · {asset['relative_path']} · 片段 {index}/{segment_total} "
                            f"（{start:.0f}-{end:.0f}秒）"
                        ),
                    )

                result = analyze_asset_adaptively(
                    source,
                    asset,
                    on_segment,
                    strategy=strategy,
                )
                keywords = result.get("keywords", [])
                if not isinstance(keywords, list):
                    keywords = [str(keywords)]
                strategy = result.get("strategy") if isinstance(result, dict) else {}
                strategy_label = str(strategy.get("label") or "自适应分析") if isinstance(strategy, dict) else "自适应分析"
                with DB.session() as conn:
                    conn.execute(
                        """UPDATE assets SET analysis_status='completed', analysis_summary=?,
                           analysis_keywords=?, analysis_json=?, analysis_error=NULL, analysis_strategy=?,
                           analyzed_at=CURRENT_TIMESTAMP, analysis_exempted_at=NULL,
                           analysis_exempt_reason=NULL, analysis_exempt_fingerprint=NULL,
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (
                            str(result.get("summary", "")),
                            json.dumps(keywords, ensure_ascii=False),
                            json.dumps(result, ensure_ascii=False),
                            strategy_label,
                            asset["id"],
                        ),
                    )
                completed += 1
            except Exception as exc:
                with DB.session() as conn:
                    conn.execute(
                        """UPDATE assets SET analysis_status='failed', analysis_error=?,
                           analysis_exempted_at=NULL, analysis_exempt_reason=NULL,
                           analysis_exempt_fingerprint=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (str(exc), asset["id"]),
                    )
                failed += 1
            finally:
                processed_ids.add(int(asset["id"]))
                completed_units += asset_units
                options["processed_ids"] = sorted(processed_ids)
                save_task_options(task_id, options)
                DB.update_task(
                    task_id,
                    progress=completed_units / total_units,
                )
        message = f"本批分析完成：成功 {completed}，失败 {failed}，共 {total_assets} 条"
        DB.update_task(task_id, status="completed", progress=1.0, message=message)
    except Exception as exc:
        DB.update_task(task_id, status="failed", message=str(exc))


def recover_interrupted_tasks() -> None:
    with DB.session() as conn:
        tasks = conn.execute(
            """SELECT id, project_id, task_type, status FROM tasks
               WHERE status IN ('queued','running','pausing','paused') ORDER BY id"""
        ).fetchall()
    for task in tasks:
        DB.update_task(task["id"], message="服务已恢复，正在从上次进度继续")
        if task["task_type"] == "scan":
            EXECUTOR.submit(project_scan_job, task["id"], task["project_id"])
        elif task["task_type"] == "analysis":
            EXECUTOR.submit(project_analysis_job, task["id"], task["project_id"])
        elif task["task_type"] == "thumbnail":
            THUMB_EXECUTOR.submit(project_thumbnail_job, task["id"], task["project_id"])
        else:
            DB.update_task(task["id"], status="failed", message="不支持恢复此任务类型")


def thumbnail_target(asset_id: int) -> Path:
    return DATA_DIR / "thumbs" / f"{asset_id}.jpg"


def cached_thumbnail(asset_id: int) -> Path | None:
    with DB.session() as conn:
        asset = conn.execute(
            """SELECT a.*, p.root_path FROM assets a JOIN projects p ON p.id=a.project_id
               WHERE a.id=?""", (asset_id,),
        ).fetchone()
    if not asset or asset["status"] != "active":
        return None
    source = (Path(asset["root_path"]) / asset["relative_path"]).resolve()
    if not source.is_file():
        return None
    target = Path(asset["thumb_path"]) if asset["thumb_path"] else thumbnail_target(asset_id)
    try:
        if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
            if asset["thumb_status"] != "completed" or asset["thumb_path"] != str(target):
                with DB.session() as conn:
                    conn.execute(
                        """UPDATE assets SET thumb_path=?, thumb_status='completed',
                           thumb_error=NULL, thumb_updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (str(target), asset_id),
                    )
            return target
    except OSError:
        return None
    return None


def generate_thumbnail(asset_id: int) -> Path | None:
    cached = cached_thumbnail(asset_id)
    if cached:
        return cached
    with DB.session() as conn:
        asset = conn.execute(
            """SELECT a.*, p.root_path FROM assets a JOIN projects p ON p.id=a.project_id
               WHERE a.id=?""", (asset_id,),
        ).fetchone()
        if asset:
            conn.execute(
                """UPDATE assets SET thumb_status='processing', thumb_error=NULL
                   WHERE id=?""", (asset_id,)
            )
    if not asset or asset["status"] != "active":
        return None
    source = (Path(asset["root_path"]) / asset["relative_path"]).resolve()
    if not source.is_file():
        with DB.session() as conn:
            conn.execute(
                """UPDATE assets SET thumb_status='failed', thumb_error=?,
                   thumb_updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                ("SMB 素材当前不可读取", asset_id),
            )
        return None

    thumb_dir = DATA_DIR / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    target = thumbnail_target(asset_id)
    temporary = thumb_dir / f"{asset_id}.part.jpg"

    ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
    command = [ffmpeg, "-y", "-loglevel", "error"]
    if asset["media_type"] == "video":
        seek = min(max((asset["duration"] or 5) * 0.15, 0.5), 15)
        command += ["-ss", str(seek)]
    command += ["-i", str(source), "-vf", "scale='min(640,iw)':-2", "-frames:v", "1", str(temporary)]
    error = "缩略图生成失败"
    for _attempt in range(2):
        try:
            subprocess.run(command, timeout=30, check=True, capture_output=True)
            temporary.replace(target)
            error = ""
            break
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc)[:500] or "缩略图生成失败"
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    if error:
        with DB.session() as conn:
            conn.execute(
                """UPDATE assets SET thumb_status='failed', thumb_error=?,
                   thumb_updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (error, asset_id),
            )
        return None
    with DB.session() as conn:
        conn.execute(
            """UPDATE assets SET thumb_path=?, thumb_status='completed', thumb_error=NULL,
               thumb_updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (str(target), asset_id),
        )
    return target


def project_thumbnail_job(task_id: int, project_id: int) -> None:
    with DB.session() as conn:
        task = conn.execute("SELECT message FROM tasks WHERE id=?", (task_id,)).fetchone()
    force_retry = bool(task and str(task["message"] or "").startswith("手动"))
    DB.update_task(task_id, status="running", message="正在检查预览图缓存")
    try:
        with DB.session() as conn:
            rows = conn.execute(
                """SELECT id FROM assets
                   WHERE project_id=? AND status='active'
                     AND (
                       ? OR thumb_status!='failed' OR thumb_updated_at IS NULL
                       OR thumb_updated_at <= datetime('now', '-15 minutes')
                     )
                   ORDER BY id""",
                (project_id, force_retry),
            ).fetchall()
        pending_ids = [int(row["id"]) for row in rows if not cached_thumbnail(int(row["id"]))]
        if not pending_ids:
            DB.update_task(task_id, status="completed", progress=1.0, message="预览图缓存已是最新")
            return
        completed = 0
        failed = 0
        total = len(pending_ids)
        for index, asset_id in enumerate(pending_ids):
            wait_if_paused(task_id)
            DB.update_task(
                task_id,
                progress=index / max(1, total),
                message=f"正在生成预览图（{index + 1}/{total}）",
            )
            if generate_thumbnail(asset_id):
                completed += 1
            else:
                failed += 1
            DB.update_task(task_id, progress=(index + 1) / max(1, total))
        DB.update_task(
            task_id,
            status="completed",
            progress=1.0,
            message=f"预览图完成：成功 {completed}，失败 {failed}",
        )
    except Exception as exc:
        DB.update_task(task_id, status="failed", message=f"预览图任务失败：{exc}")


def enqueue_thumbnail_task(project_id: int, reason: str = "等待生成预览图") -> int:
    with DB.session() as conn:
        running = conn.execute(
            """SELECT id FROM tasks WHERE project_id=? AND task_type='thumbnail'
               AND status IN ('queued','running','pausing','paused') ORDER BY id DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
    if running:
        return int(running["id"])
    task_id = DB.create_task(project_id, "thumbnail", reason)
    THUMB_EXECUTOR.submit(project_thumbnail_job, task_id, project_id)
    return task_id


def storage_health(project_id: int | None = None) -> dict[str, object]:
    project_payload: dict[str, object] | None = None
    if project_id is not None:
        with DB.session() as conn:
            project = conn.execute(
                "SELECT id, name, root_path, smb_url FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if not project:
            raise ValueError("项目不存在")
        root = Path(project["root_path"])
        started = time.monotonic()
        try:
            mounted = root.is_dir() and os.access(root, os.R_OK)
            message = "SMB 项目目录可读取" if mounted else "SMB 项目目录不可读取"
        except OSError as exc:
            mounted = False
            message = f"SMB 检查失败：{exc}"
        project_payload = {
            "id": int(project["id"]),
            "name": project["name"],
            "mounted": mounted,
            "message": message,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "windows_path": windows_unc_path(project["smb_url"]),
        }
    nas = NAS.health()
    online = bool(nas.get("online")) and (
        project_payload is None or bool(project_payload.get("mounted"))
    )
    return {
        "status": "online" if online else "degraded",
        "nas": nas,
        "project": project_payload,
    }


class AppHandler(BaseHTTPRequestHandler):
    server_version = "MediaWorkbench/0.11.5"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_download(self, body: bytes, content_type: str, filename: str):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}"
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 35 * 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/"):
                return self.handle_api_get(parsed.path, parse_qs(parsed.query))
            return self.serve_static(parsed.path)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": f"服务器错误：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            return self.handle_api_post(parsed.path, self.read_json())
        except json.JSONDecodeError:
            self.send_json({"error": "JSON 格式无效"}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": f"服务器错误：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        try:
            return self.handle_api_delete(parsed.path, self.read_json())
        except json.JSONDecodeError:
            self.send_json({"error": "JSON 格式无效"}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": f"服务器错误：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_api_get(self, path: str, query: dict[str, list[str]]):
        if path == "/api/health":
            return self.send_json({
                "ok": True,
                "version": "0.11.5",
                "phase": "演示界面硬件信息脱敏",
                "nas_access": NAS.public_status(),
            })

        if path == "/api/storage/health":
            raw_project_id = query.get("project_id", [""])[0]
            try:
                project_id = int(raw_project_id) if raw_project_id else None
            except ValueError as exc:
                raise ValueError("项目编号无效") from exc
            return self.send_json(storage_health(project_id))

        if path == "/api/ai/status":
            status = JOYAI.status()
            config = text_model_config()
            status["text_planner"] = public_text_model_config(
                config, text_model_client(config).status()
            )
            return self.send_json(status)

        if path == "/api/projects":
            with DB.session() as conn:
                rows = conn.execute(
                    """SELECT p.*,
                       COUNT(a.id) AS asset_count,
                       SUM(CASE WHEN a.status='active' THEN 1 ELSE 0 END) AS active_count
                       FROM projects p LEFT JOIN assets a ON a.project_id=p.id
                       GROUP BY p.id ORDER BY p.updated_at DESC"""
                ).fetchall()
            projects = [row_to_dict(row) for row in rows]
            for project in projects:
                project.pop("root_path", None)
                project["windows_path"] = windows_unc_path(project.get("smb_url", ""))
            return self.send_json(projects)

        readiness_match = re.fullmatch(
            r"/api/projects/(\d+)/storyboard-readiness", path
        )
        if readiness_match:
            project_id = int(readiness_match.group(1))
            with DB.session() as conn:
                readiness = storyboard_readiness(conn, project_id)
            if readiness is None:
                return self.send_json({"error": "项目不存在"}, HTTPStatus.NOT_FOUND)
            return self.send_json(readiness)

        storyboards_match = re.fullmatch(r"/api/projects/(\d+)/storyboards", path)
        if storyboards_match:
            project_id = int(storyboards_match.group(1))
            with DB.session() as conn:
                project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
                rows = conn.execute(
                    """SELECT s.*, COUNT(ss.id) AS shot_count
                       FROM storyboards s LEFT JOIN storyboard_shots ss ON ss.storyboard_id=s.id
                       WHERE s.project_id=? GROUP BY s.id
                       ORDER BY s.updated_at DESC, s.id DESC""",
                    (project_id,),
                ).fetchall()
            if not project:
                return self.send_json({"error": "项目不存在"}, HTTPStatus.NOT_FOUND)
            return self.send_json([row_to_dict(row) for row in rows])

        storyboard_export_match = re.fullmatch(r"/api/storyboards/(\d+)/export", path)
        if storyboard_export_match:
            storyboard_id = int(storyboard_export_match.group(1))
            export_format = query.get("format", ["json"])[0].casefold()
            with DB.session() as conn:
                board = storyboard_detail(conn, storyboard_id)
            if not board:
                return self.send_json({"error": "分镜册不存在"}, HTTPStatus.NOT_FOUND)
            safe_title = re.sub(r"[\\/:*?\"<>|]+", "_", board["title"]).strip() or "分镜清单"
            exporters = {
                "json": (export_json, "application/json; charset=utf-8", ".json"),
                "csv": (export_csv, "text/csv; charset=utf-8", ".csv"),
                "md": (export_markdown, "text/markdown; charset=utf-8", ".md"),
                "markdown": (export_markdown, "text/markdown; charset=utf-8", ".md"),
            }
            if export_format not in exporters:
                raise ValueError("导出格式支持 JSON、CSV 和 Markdown")
            exporter, content_type, suffix = exporters[export_format]
            return self.send_download(
                exporter(board, board["shots"]), content_type, safe_title + suffix
            )

        storyboard_match = re.fullmatch(r"/api/storyboards/(\d+)", path)
        if storyboard_match:
            with DB.session() as conn:
                board = storyboard_detail(conn, int(storyboard_match.group(1)))
            if not board:
                return self.send_json({"error": "分镜册不存在"}, HTTPStatus.NOT_FOUND)
            return self.send_json(board)

        project_match = re.fullmatch(r"/api/projects/(\d+)/dashboard", path)
        if project_match:
            project_id = int(project_match.group(1))
            with DB.session() as conn:
                project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                stats = conn.execute(
                    f"""SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
                       SUM(CASE WHEN status='missing' THEN 1 ELSE 0 END) AS missing,
                       SUM(CASE WHEN media_type='video' AND status='active' THEN 1 ELSE 0 END) AS videos,
                       SUM(CASE WHEN media_type='image' AND status='active' THEN 1 ELSE 0 END) AS images,
                       SUM(CASE WHEN status='active' AND analysis_status IN ('pending','processing')
                                 AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL}) THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN status='active' AND analysis_status='processing'
                                 AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL}) THEN 1 ELSE 0 END) AS processing,
                       SUM(CASE WHEN status='active' AND analysis_status='completed'
                                 AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL}) THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status='active' AND analysis_status='failed'
                                 AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL}) THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status='active' AND {VALID_ANALYSIS_EXEMPTION_SQL}
                                THEN 1 ELSE 0 END) AS exempted,
                       SUM(CASE WHEN status='active' AND thumb_status='completed' THEN 1 ELSE 0 END) AS thumbnails,
                       SUM(CASE WHEN status='active' AND thumb_status='failed' THEN 1 ELSE 0 END) AS thumbnail_failed,
                       COALESCE(SUM(CASE WHEN status='active' THEN size_bytes ELSE 0 END), 0) AS bytes
                       FROM assets WHERE project_id=?""", (project_id,),
                ).fetchone()
                readiness = storyboard_readiness(conn, project_id)
            if not project:
                return self.send_json({"error": "项目不存在"}, HTTPStatus.NOT_FOUND)
            project_payload = row_to_dict(project)
            project_payload.pop("root_path", None)
            project_payload["windows_path"] = windows_unc_path(project_payload.get("smb_url", ""))
            project_payload["smb_online"] = Path(project["root_path"]).is_dir()
            return self.send_json({
                "project": project_payload,
                "stats": row_to_dict(stats),
                "storyboard_readiness": readiness,
            })

        assets_match = re.fullmatch(r"/api/projects/(\d+)/assets", path)
        if assets_match:
            project_id = int(assets_match.group(1))
            media_type = query.get("type", [""])[0]
            analysis_filter = query.get("analysis", [""])[0].casefold()
            search = query.get("q", [""])[0].strip()
            paged = query.get("paged", ["0"])[0] == "1"
            try:
                limit = max(1, min(200, int(query.get("limit", ["120"])[0])))
                offset = max(0, int(query.get("offset", ["0"])[0]))
            except ValueError as exc:
                raise ValueError("分页参数无效") from exc
            clauses = ["project_id=?", "status='active'"]
            values: list[object] = [project_id]
            if media_type in {"video", "image"}:
                clauses.append("media_type=?")
                values.append(media_type)
            if analysis_filter == "failed":
                clauses.append("analysis_status='failed'")
                clauses.append(f"NOT ({VALID_ANALYSIS_EXEMPTION_SQL})")
            elif analysis_filter == "exempted":
                clauses.append(VALID_ANALYSIS_EXEMPTION_SQL)
            elif analysis_filter == "pending":
                clauses.append("analysis_status IN ('pending','processing')")
                clauses.append(f"NOT ({VALID_ANALYSIS_EXEMPTION_SQL})")
            elif analysis_filter == "completed":
                clauses.append("analysis_status='completed'")
                clauses.append(f"NOT ({VALID_ANALYSIS_EXEMPTION_SQL})")
            elif analysis_filter:
                raise ValueError("分析状态筛选无效")
            if search:
                clauses.append("(filename LIKE ? OR relative_path LIKE ? OR analysis_summary LIKE ? OR analysis_keywords LIKE ?)")
                values.extend([f"%{search}%"] * 4)
            with DB.session() as conn:
                total = int(conn.execute(
                    f"SELECT COUNT(*) FROM assets WHERE {' AND '.join(clauses)}", values,
                ).fetchone()[0])
                rows = conn.execute(
                    f"""SELECT * FROM assets WHERE {' AND '.join(clauses)}
                        ORDER BY mtime DESC, id DESC LIMIT ? OFFSET ?""",
                    [*values, limit if paged else 500, offset if paged else 0],
                ).fetchall()
            items = [public_asset(row) for row in rows]
            if not paged:
                return self.send_json(items)
            next_offset = offset + len(items)
            return self.send_json({
                "items": items,
                "total": total,
                "offset": offset,
                "next_offset": next_offset,
                "has_more": next_offset < total,
            })

        asset_match = re.fullmatch(r"/api/assets/(\d+)", path)
        if asset_match:
            with DB.session() as conn:
                row = conn.execute(
                    """SELECT a.*, p.name AS project_name, p.root_path, p.smb_url
                       FROM assets a JOIN projects p ON p.id=a.project_id WHERE a.id=?""",
                    (int(asset_match.group(1)),),
                ).fetchone()
            if not row:
                return self.send_json({"error": "素材不存在"}, HTTPStatus.NOT_FOUND)
            payload = public_asset(row)
            payload["smb_path"] = windows_unc_path(
                payload.get("smb_url", ""), payload.get("relative_path", "")
            )
            payload["direct_play_available"] = NAS.enabled
            return self.send_json(payload)

        direct_play_match = re.fullmatch(r"/api/assets/(\d+)/direct-play", path)
        if direct_play_match:
            with DB.session() as conn:
                asset = conn.execute(
                    """SELECT a.relative_path, a.status, p.smb_url
                       FROM assets a JOIN projects p ON p.id=a.project_id WHERE a.id=?""",
                    (int(direct_play_match.group(1)),),
                ).fetchone()
            if not asset or asset["status"] != "active":
                return self.send_json({"error": "素材不存在"}, HTTPStatus.NOT_FOUND)
            url, expires_in = NAS.direct_url(asset["smb_url"], asset["relative_path"])
            return self.send_json({
                "url": url,
                "expires_in": expires_in,
                "source": "nas_direct",
            })

        thumbnail_match = re.fullmatch(r"/api/assets/(\d+)/thumbnail", path)
        if thumbnail_match:
            asset_id = int(thumbnail_match.group(1))
            thumb = cached_thumbnail(asset_id)
            if not thumb:
                with DB.session() as conn:
                    asset = conn.execute(
                        """SELECT project_id FROM assets
                           WHERE id=? AND status='active'
                             AND (
                               thumb_status!='failed' OR thumb_updated_at IS NULL
                               OR thumb_updated_at <= datetime('now', '-15 minutes')
                             )""",
                        (asset_id,),
                    ).fetchone()
                if asset:
                    enqueue_thumbnail_task(int(asset["project_id"]), "发现缺失预览图，正在后台补齐")
                return self.send_error(HTTPStatus.NOT_FOUND)
            return self.serve_file(thumb, "image/jpeg", cache=True)

        stream_match = re.fullmatch(r"/api/assets/(\d+)/stream", path)
        if stream_match:
            with DB.session() as conn:
                asset = conn.execute(
                    """SELECT a.*, p.root_path FROM assets a JOIN projects p ON p.id=a.project_id WHERE a.id=?""",
                    (int(stream_match.group(1)),),
                ).fetchone()
            if not asset:
                return self.send_error(HTTPStatus.NOT_FOUND)
            source = (Path(asset["root_path"]) / asset["relative_path"]).resolve()
            return self.serve_file(source, mimetypes.guess_type(source.name)[0] or "application/octet-stream", ranges=True)

        if path == "/api/tasks":
            with DB.session() as conn:
                rows = conn.execute(
                    """SELECT t.*, p.name AS project_name FROM tasks t
                       LEFT JOIN projects p ON p.id=t.project_id ORDER BY t.id DESC LIMIT 100"""
                ).fetchall()
            return self.send_json([task_payload(row) for row in rows])

        self.send_json({"error": "API 不存在"}, HTTPStatus.NOT_FOUND)

    def handle_api_post(self, path: str, payload: dict):
        if path in {
            "/api/ai/settings/text-planner",
            "/api/ai/settings/text-planner/test",
        }:
            current = text_model_config()
            requested_provider = str(payload.get("provider") or "local").strip().casefold()
            reusable_key = (
                str(current.get("api_key") or "")
                if requested_provider == current.get("provider")
                else ""
            )
            config = validate_text_model_payload(
                payload, existing_key=reusable_key
            )
            if path.endswith("/test"):
                status = text_model_client(config).status()
                return self.send_json(public_text_model_config(config, status))
            with DB.session() as conn:
                conn.execute(
                    """INSERT INTO model_settings(
                         role, provider, endpoint, model, api_key, updated_at
                       ) VALUES ('text_planner', ?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(role) DO UPDATE SET
                         provider=excluded.provider,
                         endpoint=excluded.endpoint,
                         model=excluded.model,
                         api_key=excluded.api_key,
                         updated_at=CURRENT_TIMESTAMP""",
                    (
                        config["provider"], config["endpoint"],
                        config["model"], config["api_key"],
                    ),
                )
            saved = text_model_config()
            status = text_model_client(saved).status()
            return self.send_json(public_text_model_config(saved, status))

        if path == "/api/projects":
            name = str(payload.get("name", "")).strip()
            submitted_path = str(payload.get("smb_url", "")).strip()
            if not name or not submitted_path:
                raise ValueError("项目名称和项目地址不能为空")
            smb_url = normalize_smb_url(submitted_path)
            root_path = str(payload.get("root_path", "")).strip() or smb_url
            root = validate_root(root_path)
            with DB.session() as conn:
                cur = conn.execute(
                    "INSERT INTO projects(name, root_path, smb_url) VALUES (?, ?, ?)",
                    (name, str(root), smb_url),
                )
                project_id = int(cur.lastrowid)
                row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            created = row_to_dict(row)
            created.pop("root_path", None)
            return self.send_json(created, HTTPStatus.CREATED)

        create_storyboard_match = re.fullmatch(r"/api/projects/(\d+)/storyboards", path)
        if create_storyboard_match:
            project_id = int(create_storyboard_match.group(1))
            title = str(payload.get("title") or "未命名分镜").strip()[:120]
            source_text = str(payload.get("source_text") or "")[:200000]
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                readiness = storyboard_readiness(conn, project_id)
                if readiness is None:
                    return self.send_json({"error": "项目不存在"}, HTTPStatus.NOT_FOUND)
                if not readiness["ready"]:
                    return self.send_json(
                        storyboard_gate_error(readiness), HTTPStatus.CONFLICT
                    )
                cur = conn.execute(
                    """INSERT INTO storyboards(project_id, title, source_format, source_text)
                       VALUES (?, ?, 'paste', ?)""",
                    (project_id, title, source_text),
                )
                board = storyboard_detail(conn, int(cur.lastrowid))
            return self.send_json(board, HTTPStatus.CREATED)

        import_storyboard_match = re.fullmatch(r"/api/projects/(\d+)/storyboards/import", path)
        if import_storyboard_match:
            project_id = int(import_storyboard_match.group(1))
            filename = str(payload.get("filename") or "").strip()
            encoded = str(payload.get("content_base64") or "")
            mode = str(payload.get("mode") or "script")
            if not filename or not encoded:
                raise ValueError("请选择要导入的文件")
            try:
                data = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise ValueError("导入文件编码无效") from exc
            parsed = parse_shot_file(filename, data) if mode == "shots" else parse_script_file(filename, data)
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                readiness = storyboard_readiness(conn, project_id)
                if readiness is None:
                    return self.send_json({"error": "项目不存在"}, HTTPStatus.NOT_FOUND)
                if not readiness["ready"]:
                    return self.send_json(
                        storyboard_gate_error(readiness), HTTPStatus.CONFLICT
                    )
                cur = conn.execute(
                    """INSERT INTO storyboards(
                         project_id, title, source_name, source_format, source_text
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        project_id, parsed["title"][:120], parsed["source_name"],
                        parsed["source_format"], parsed["source_text"][:200000],
                    ),
                )
                storyboard_id = int(cur.lastrowid)
                if parsed.get("shots"):
                    valid_asset_ids = {
                        row["id"] for row in conn.execute(
                            f"""SELECT id FROM assets WHERE project_id=? AND status='active'
                                AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL})""",
                            (project_id,),
                        ).fetchall()
                    }
                    for shot in parsed["shots"]:
                        shot["candidates"] = [
                            candidate for candidate in shot.get("candidates", [])
                            if candidate.get("asset_id") in valid_asset_ids
                        ]
                        candidate_ids = {
                            candidate.get("asset_id") for candidate in shot["candidates"]
                        }
                        if shot.get("selected_asset_id") not in candidate_ids:
                            shot["selected_asset_id"] = None
                    insert_storyboard_shots(conn, storyboard_id, parsed["shots"])
                    create_storyboard_version(
                        conn, storyboard_id, parsed["shots"], planner="导入分镜"
                    )
                board = storyboard_detail(conn, storyboard_id)
            return self.send_json(board, HTTPStatus.CREATED)

        update_storyboard_match = re.fullmatch(r"/api/storyboards/(\d+)", path)
        if update_storyboard_match:
            storyboard_id = int(update_storyboard_match.group(1))
            fields = []
            values: list[object] = []
            if "title" in payload:
                title = str(payload.get("title") or "未命名分镜").strip()[:120]
                fields.append("title=?")
                values.append(title)
            if "source_text" in payload:
                fields.append("source_text=?")
                values.append(str(payload.get("source_text") or "")[:200000])
            if not fields:
                raise ValueError("没有需要保存的内容")
            fields.append("updated_at=CURRENT_TIMESTAMP")
            values.append(storyboard_id)
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT project_id FROM storyboards WHERE id=?", (storyboard_id,)
                ).fetchone()
                if not existing:
                    return self.send_json(
                        {"error": "分镜册不存在"}, HTTPStatus.NOT_FOUND
                    )
                readiness = storyboard_readiness(conn, int(existing["project_id"]))
                if not readiness["ready"]:
                    return self.send_json(
                        storyboard_gate_error(readiness), HTTPStatus.CONFLICT
                    )
                cur = conn.execute(
                    f"UPDATE storyboards SET {', '.join(fields)} WHERE id=?", values
                )
                board = storyboard_detail(conn, storyboard_id)
            return self.send_json(board)

        generate_storyboard_match = re.fullmatch(r"/api/storyboards/(\d+)/generate", path)
        if generate_storyboard_match:
            storyboard_id = int(generate_storyboard_match.group(1))
            with DB.session() as conn:
                board = conn.execute(
                    "SELECT * FROM storyboards WHERE id=?", (storyboard_id,)
                ).fetchone()
                readiness = (
                    storyboard_readiness(conn, int(board["project_id"]))
                    if board else None
                )
            if not board:
                return self.send_json({"error": "分镜册不存在"}, HTTPStatus.NOT_FOUND)
            if not readiness["ready"]:
                return self.send_json(
                    storyboard_gate_error(readiness), HTTPStatus.CONFLICT
                )
            script = str(board["source_text"] or "").strip()
            if not script:
                raise ValueError("请先粘贴或导入文案")
            planner = "规则拆分"
            planner_warning = "独立文本模型尚未启用，当前按标点拆分文案"
            shots = fallback_shots(script)
            text_client = text_model_client()
            if text_client.enabled:
                try:
                    planned = normalize_shots(text_client.plan_shots(script))
                    if not planned:
                        raise ValueError("模型没有返回可用分镜")
                    shots = planned
                    planner = text_client.model
                    planner_warning = ""
                except Exception as exc:
                    print(
                        f"Storyboard {storyboard_id} planner {text_client.model} failed: {exc}",
                        flush=True,
                    )
                    raise ValueError(f"文本模型调用失败，已保留当前版本：{exc}") from exc
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current_board = conn.execute(
                    "SELECT project_id FROM storyboards WHERE id=?", (storyboard_id,)
                ).fetchone()
                if not current_board:
                    return self.send_json(
                        {"error": "分镜册不存在"}, HTTPStatus.NOT_FOUND
                    )
                readiness = storyboard_readiness(
                    conn, int(current_board["project_id"])
                )
                if not readiness["ready"]:
                    return self.send_json(
                        storyboard_gate_error(readiness), HTTPStatus.CONFLICT
                    )
                asset_rows = conn.execute(
                    f"""SELECT a.*, p.smb_url FROM assets a JOIN projects p ON p.id=a.project_id
                       WHERE a.project_id=? AND a.status='active'
                         AND a.analysis_status='completed'
                         AND NOT ({VALID_ANALYSIS_EXEMPTION_SQL})
                       ORDER BY a.id LIMIT 5000""",
                    (board["project_id"],),
                ).fetchall()
                matched = match_shots(shots, [row_to_dict(row) for row in asset_rows])
                ensure_legacy_storyboard_version(conn, storyboard_id)
                insert_storyboard_shots(conn, storyboard_id, matched)
                create_storyboard_version(
                    conn, storyboard_id, matched,
                    planner=planner, warning=planner_warning,
                )
                result = storyboard_detail(conn, storyboard_id)
            result["planner"] = planner
            result["planner_warning"] = planner_warning
            return self.send_json(result)

        activate_version_match = re.fullmatch(
            r"/api/storyboards/(\d+)/versions/(\d+)/activate", path
        )
        if activate_version_match:
            storyboard_id = int(activate_version_match.group(1))
            version_id = int(activate_version_match.group(2))
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT project_id FROM storyboards WHERE id=?", (storyboard_id,)
                ).fetchone()
                if not existing:
                    return self.send_json(
                        {"error": "分镜册不存在"}, HTTPStatus.NOT_FOUND
                    )
                readiness = storyboard_readiness(conn, int(existing["project_id"]))
                if not readiness["ready"]:
                    return self.send_json(
                        storyboard_gate_error(readiness), HTTPStatus.CONFLICT
                    )
                if not activate_storyboard_version(conn, storyboard_id, version_id):
                    return self.send_json({"error": "分镜版本不存在"}, HTTPStatus.NOT_FOUND)
                board = storyboard_detail(conn, storyboard_id)
            return self.send_json(board)

        select_shot_match = re.fullmatch(r"/api/storyboards/(\d+)/shots/(\d+)/select", path)
        if select_shot_match:
            storyboard_id = int(select_shot_match.group(1))
            shot_id = int(select_shot_match.group(2))
            asset_id = int(payload.get("asset_id") or 0)
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                shot = conn.execute(
                    """SELECT ss.*, s.project_id FROM storyboard_shots ss
                       JOIN storyboards s ON s.id=ss.storyboard_id
                       WHERE ss.id=? AND ss.storyboard_id=?""",
                    (shot_id, storyboard_id),
                ).fetchone()
                if not shot:
                    return self.send_json({"error": "分镜不存在"}, HTTPStatus.NOT_FOUND)
                readiness = storyboard_readiness(conn, int(shot["project_id"]))
                if not readiness["ready"]:
                    return self.send_json(
                        storyboard_gate_error(readiness), HTTPStatus.CONFLICT
                    )
                asset = conn.execute(
                    "SELECT id FROM assets WHERE id=? AND project_id=? AND status='active'",
                    (asset_id, shot["project_id"]),
                ).fetchone()
                if not asset:
                    raise ValueError("所选素材不属于当前项目或已不可用")
                candidates = _json_array(shot["candidates_json"])
                if asset_id not in {item.get("asset_id") for item in candidates}:
                    raise ValueError("所选素材不在当前候选列表中")
                conn.execute(
                    "UPDATE storyboard_shots SET selected_asset_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (asset_id, shot_id),
                )
                sync_active_version_selection(
                    conn, storyboard_id, int(shot["position"]), asset_id
                )
                conn.execute(
                    "UPDATE storyboards SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (storyboard_id,),
                )
                board = storyboard_detail(conn, storyboard_id)
            return self.send_json(board)

        scan_match = re.fullmatch(r"/api/projects/(\d+)/scan", path)
        if scan_match:
            project_id = int(scan_match.group(1))
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
                running = conn.execute(
                    "SELECT id FROM tasks WHERE project_id=? AND task_type='scan' AND status IN ('queued','running','pausing','paused')",
                    (project_id,),
                ).fetchone()
                analyzing = conn.execute(
                    """SELECT id FROM tasks WHERE project_id=? AND task_type='analysis'
                       AND status IN ('queued','running','pausing','paused')""",
                    (project_id,),
                ).fetchone()
                if project and not running and not analyzing:
                    cur = conn.execute(
                        """INSERT INTO tasks(project_id, task_type, message)
                           VALUES (?, 'scan', '等待扫描')""",
                        (project_id,),
                    )
                    task_id = int(cur.lastrowid)
            if not project:
                return self.send_json({"error": "项目不存在"}, HTTPStatus.NOT_FOUND)
            if running:
                return self.send_json({"task_id": running["id"], "message": "扫描任务已在运行"})
            if analyzing:
                return self.send_json(
                    {
                        "error": "JoyAI 分析尚未完成，暂停或完成分析后才能重新扫描素材。",
                        "code": "scan_blocked_by_analysis",
                    },
                    HTTPStatus.CONFLICT,
                )
            EXECUTOR.submit(project_scan_job, task_id, project_id)
            return self.send_json({"task_id": task_id, "message": "扫描任务已创建"}, HTTPStatus.ACCEPTED)

        thumbnail_job_match = re.fullmatch(r"/api/projects/(\d+)/thumbnails", path)
        if thumbnail_job_match:
            project_id = int(thumbnail_job_match.group(1))
            with DB.session() as conn:
                project = conn.execute(
                    "SELECT id FROM projects WHERE id=?", (project_id,)
                ).fetchone()
            if not project:
                return self.send_json({"error": "项目不存在"}, HTTPStatus.NOT_FOUND)
            task_id = enqueue_thumbnail_task(project_id, "手动检查并补齐预览图")
            return self.send_json(
                {"task_id": task_id, "message": "预览图任务已创建或正在运行"},
                HTTPStatus.ACCEPTED,
            )

        asset_exemption_match = re.fullmatch(
            r"/api/assets/(\d+)/analysis-exemption", path
        )
        if asset_exemption_match:
            asset_id = int(asset_exemption_match.group(1))
            exempt = payload.get("exempt")
            if not isinstance(exempt, bool):
                raise ValueError("请明确选择排除或恢复素材")
            reason = str(payload.get("reason") or "").strip()[:200]
            if exempt and not reason:
                raise ValueError("请选择或填写排除原因")
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                asset = conn.execute(
                    """SELECT a.*, p.smb_url FROM assets a
                       JOIN projects p ON p.id=a.project_id WHERE a.id=?""",
                    (asset_id,),
                ).fetchone()
                if not asset:
                    return self.send_json(
                        {"error": "素材不存在"}, HTTPStatus.NOT_FOUND
                    )
                if asset["status"] != "active":
                    return self.send_json(
                        {"error": "该素材已不在当前项目目录中"}, HTTPStatus.CONFLICT
                    )
                busy = conn.execute(
                    """SELECT id, task_type FROM tasks
                       WHERE project_id=? AND task_type IN ('scan','analysis')
                         AND status IN ('queued','running','pausing','paused')
                       ORDER BY id LIMIT 1""",
                    (asset["project_id"],),
                ).fetchone()
                if busy:
                    return self.send_json(
                        {
                            "error": "扫描或分析任务进行中，完成后再调整异常素材。",
                            "task_id": int(busy["id"]),
                        },
                        HTTPStatus.CONFLICT,
                    )
                currently_exempted = bool(
                    asset["analysis_exempted_at"]
                    and asset["analysis_exempt_fingerprint"]
                    == asset["content_fingerprint"]
                )
                if exempt:
                    if asset["analysis_status"] != "failed" and not currently_exempted:
                        return self.send_json(
                            {"error": "只有分析失败的素材可以标记为异常并排除"},
                            HTTPStatus.CONFLICT,
                        )
                    conn.execute(
                        """UPDATE assets SET analysis_exempted_at=CURRENT_TIMESTAMP,
                           analysis_exempt_reason=?, analysis_exempt_fingerprint=content_fingerprint,
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (reason, asset_id),
                    )
                else:
                    if not asset["analysis_exempted_at"]:
                        return self.send_json(
                            {"error": "该素材未处于人工排除状态"},
                            HTTPStatus.CONFLICT,
                        )
                    conn.execute(
                        """UPDATE assets SET
                           analysis_status=CASE WHEN analysis_status='failed'
                                                THEN 'pending' ELSE analysis_status END,
                           analysis_exempted_at=NULL, analysis_exempt_reason=NULL,
                           analysis_exempt_fingerprint=NULL, analysis_error=NULL,
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (asset_id,),
                    )
                updated = conn.execute(
                    """SELECT a.*, p.smb_url FROM assets a
                       JOIN projects p ON p.id=a.project_id WHERE a.id=?""",
                    (asset_id,),
                ).fetchone()
                readiness = storyboard_readiness(conn, int(asset["project_id"]))
            asset_payload = public_asset(updated)
            asset_payload["smb_path"] = windows_unc_path(
                asset_payload.get("smb_url", ""),
                asset_payload.get("relative_path", ""),
            )
            asset_payload["direct_play_available"] = NAS.enabled
            return self.send_json({
                "asset": asset_payload,
                "readiness": readiness,
                "message": (
                    "异常素材已排除，不再阻塞分镜工作区"
                    if exempt else
                    "已恢复为待分析素材，分镜工作区会重新等待分析完成"
                ),
            })

        asset_retry_match = re.fullmatch(r"/api/assets/(\d+)/analysis/retry", path)
        if asset_retry_match:
            asset_id = int(asset_retry_match.group(1))
            status = JOYAI.status()
            if not status["enabled"]:
                return self.send_json(
                    {"error": status["message"]}, HTTPStatus.SERVICE_UNAVAILABLE
                )
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                asset = conn.execute(
                    "SELECT * FROM assets WHERE id=?", (asset_id,)
                ).fetchone()
                if not asset:
                    return self.send_json(
                        {"error": "素材不存在"}, HTTPStatus.NOT_FOUND
                    )
                if asset["status"] != "active":
                    return self.send_json(
                        {"error": "该素材已不在当前项目目录中"}, HTTPStatus.CONFLICT
                    )
                if (
                    asset["analysis_exempted_at"]
                    and asset["analysis_exempt_fingerprint"]
                    == asset["content_fingerprint"]
                ):
                    return self.send_json(
                        {"error": "请先取消该素材的人工排除，再重新分析"},
                        HTTPStatus.CONFLICT,
                    )
                if asset["analysis_status"] not in {"failed", "pending"}:
                    return self.send_json(
                        {"error": "当前素材状态不需要重试"}, HTTPStatus.CONFLICT
                    )
                readiness = storyboard_readiness(conn, int(asset["project_id"]))
                if readiness["active_tasks"]["scan"] or readiness["active_tasks"]["analysis"]:
                    return self.send_json(
                        {
                            "error": "扫描或分析任务进行中，请在当前任务完成后重试。",
                            "readiness": readiness,
                        },
                        HTTPStatus.CONFLICT,
                    )
                if (
                    not readiness["requirements"]["scan_completed"]
                    or not readiness["requirements"]["scan_idle"]
                    or not readiness["requirements"]["has_assets"]
                ):
                    return self.send_json(
                        {"error": "请先完成素材扫描。", "readiness": readiness},
                        HTTPStatus.CONFLICT,
                    )
                options = {
                    "scope": "failed",
                    "media_type": "all",
                    "folder_prefix": "",
                    "min_duration": None,
                    "max_duration": None,
                    "limit": 1,
                    "asset_ids": [asset_id],
                    "processed_ids": [],
                }
                cur = conn.execute(
                    """INSERT INTO tasks(
                         project_id, task_type, message, options_json
                       ) VALUES (?, 'analysis', '等待重试异常素材', ?)""",
                    (asset["project_id"], json.dumps(options, ensure_ascii=False)),
                )
                task_id = int(cur.lastrowid)
                conn.execute(
                    "UPDATE assets SET analysis_status='pending', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (asset_id,),
                )
            EXECUTOR.submit(project_analysis_job, task_id, int(asset["project_id"]))
            return self.send_json(
                {"task_id": task_id, "message": "已创建单条素材重试任务"},
                HTTPStatus.ACCEPTED,
            )

        analysis_match = re.fullmatch(r"/api/projects/(\d+)/analysis", path)
        if analysis_match:
            project_id = int(analysis_match.group(1))
            with DB.session() as conn:
                readiness = storyboard_readiness(conn, project_id)
            if readiness is None:
                return self.send_json({"error": "项目不存在"}, HTTPStatus.NOT_FOUND)
            if readiness["active_tasks"]["analysis"]:
                return self.send_json({
                    "task_id": readiness["active_tasks"]["analysis"][0],
                    "message": "分析任务已在运行",
                })
            if (
                not readiness["requirements"]["scan_completed"]
                or not readiness["requirements"]["scan_idle"]
                or not readiness["requirements"]["has_assets"]
            ):
                return self.send_json(
                    {
                        "error": "请先完成素材扫描并确认项目中有可用素材。",
                        "code": "analysis_not_ready",
                        "readiness": readiness,
                    },
                    HTTPStatus.CONFLICT,
                )
            status = JOYAI.status()
            if not status["enabled"]:
                return self.send_json({"error": status["message"]}, HTTPStatus.SERVICE_UNAVAILABLE)
            options = normalize_analysis_options(payload)
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                readiness = storyboard_readiness(conn, project_id)
                if readiness is None:
                    return self.send_json({"error": "项目不存在"}, HTTPStatus.NOT_FOUND)
                if readiness["active_tasks"]["analysis"]:
                    return self.send_json({
                        "task_id": readiness["active_tasks"]["analysis"][0],
                        "message": "分析任务已在运行",
                    })
                if (
                    not readiness["requirements"]["scan_completed"]
                    or not readiness["requirements"]["scan_idle"]
                    or not readiness["requirements"]["has_assets"]
                ):
                    return self.send_json(
                        {
                            "error": "请先完成素材扫描并确认项目中有可用素材。",
                            "code": "analysis_not_ready",
                            "readiness": readiness,
                        },
                        HTTPStatus.CONFLICT,
                    )
                cur = conn.execute(
                    """INSERT INTO tasks(
                         project_id, task_type, message, options_json
                       ) VALUES (?, 'analysis', '等待 JoyAI 分析', ?)""",
                    (project_id, json.dumps(options, ensure_ascii=False)),
                )
                task_id = int(cur.lastrowid)
            EXECUTOR.submit(project_analysis_job, task_id, project_id)
            return self.send_json({"task_id": task_id, "message": "JoyAI 分析任务已创建"}, HTTPStatus.ACCEPTED)

        pause_match = re.fullmatch(r"/api/tasks/(\d+)/pause", path)
        if pause_match:
            task_id = int(pause_match.group(1))
            if not DB.request_pause(task_id):
                return self.send_json({"error": "当前任务不能暂停"}, HTTPStatus.CONFLICT)
            return self.send_json({"task_id": task_id, "message": "将在当前处理单元结束后暂停"})

        resume_match = re.fullmatch(r"/api/tasks/(\d+)/resume", path)
        if resume_match:
            task_id = int(resume_match.group(1))
            if not DB.resume_task(task_id):
                return self.send_json({"error": "当前任务不能继续"}, HTTPStatus.CONFLICT)
            return self.send_json({"task_id": task_id, "message": "任务已继续"})

        self.send_json({"error": "API 不存在"}, HTTPStatus.NOT_FOUND)

    def handle_api_delete(self, path: str, payload: dict):
        storyboard_match = re.fullmatch(r"/api/storyboards/(\d+)", path)
        if storyboard_match:
            storyboard_id = int(storyboard_match.group(1))
            with DB.session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                board = conn.execute(
                    "SELECT title FROM storyboards WHERE id=?", (storyboard_id,)
                ).fetchone()
                if not board:
                    return self.send_json(
                        {"error": "分镜册不存在"}, HTTPStatus.NOT_FOUND
                    )
                if str(payload.get("confirm_title") or "") != str(board["title"]):
                    return self.send_json(
                        {"error": "删除确认信息已变化，请重新打开确认窗口"},
                        HTTPStatus.CONFLICT,
                    )
                deleted = delete_storyboard(conn, storyboard_id)
            return self.send_json(deleted)

        self.send_json({"error": "API 不存在"}, HTTPStatus.NOT_FOUND)

    def serve_static(self, request_path: str):
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path.lstrip("/"))
        target = (WEB_DIR / relative).resolve()
        if WEB_DIR not in target.parents and target != WEB_DIR:
            return self.send_error(HTTPStatus.FORBIDDEN)
        if not target.is_file():
            target = WEB_DIR / "index.html"
        return self.serve_file(target, mimetypes.guess_type(target.name)[0] or "application/octet-stream", cache=False)

    def serve_file(self, path: Path, content_type: str, *, ranges=False, cache=False):
        if not path.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if ranges and self.headers.get("Range"):
            match = re.match(r"bytes=(\d*)-(\d*)", self.headers["Range"])
            if match:
                if match.group(1):
                    start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-store")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器读取视频 metadata 后会主动取消剩余 Range 数据，这是正常结束。
            return


def main():
    parser = argparse.ArgumentParser(description="AI NAS 素材工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    recover_interrupted_tasks()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Media Workbench listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        EXECUTOR.shutdown(wait=False, cancel_futures=True)
        THUMB_EXECUTOR.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
