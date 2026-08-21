from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from .database import Database


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts", ".mxf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic"}
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
CHUNK_SIZE = 64 * 1024


def media_type_for(path: Path) -> str:
    return "video" if path.suffix.lower() in VIDEO_EXTENSIONS else "image"


def fast_fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(CHUNK_SIZE))
        if stat.st_size > CHUNK_SIZE * 2:
            handle.seek(max(0, stat.st_size // 2 - CHUNK_SIZE // 2))
            digest.update(handle.read(CHUNK_SIZE))
        if stat.st_size > CHUNK_SIZE:
            handle.seek(max(0, stat.st_size - CHUNK_SIZE))
            digest.update(handle.read(CHUNK_SIZE))
    return digest.hexdigest()


def parse_frame_rate(value: object) -> float | None:
    text = str(value or "").strip()
    if not text or text in {"0/0", "N/A"}:
        return None
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            rate = float(numerator) / float(denominator)
        else:
            rate = float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return round(rate, 3) if rate > 0 else None


def has_positive_number(value: object) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def probe_media(path: Path) -> dict[str, float | int | str | None]:
    command = os.environ.get("FFPROBE_BIN", "ffprobe")
    try:
        result = subprocess.run(
            [command, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {
            "duration": None, "width": None, "height": None,
            "fps": None, "codec": None, "bit_rate": None,
        }

    video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    duration_raw = data.get("format", {}).get("duration") or video_stream.get("duration")
    try:
        duration = round(float(duration_raw), 3) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None
    bit_rate_raw = video_stream.get("bit_rate") or data.get("format", {}).get("bit_rate")
    try:
        bit_rate = int(bit_rate_raw) if bit_rate_raw is not None else None
    except (TypeError, ValueError):
        bit_rate = None
    return {
        "duration": duration,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "fps": parse_frame_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
        "codec": str(video_stream.get("codec_name") or "") or None,
        "bit_rate": bit_rate,
    }


class ProjectScanner:
    def __init__(self, db: Database):
        self.db = db

    def scan(self, project_id: int, progress: Callable[[float, str], None] | None = None) -> dict[str, int]:
        with self.db.session() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ValueError("项目不存在")

        root = Path(project["root_path"]).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"素材目录不可用：{root}")

        files: list[Path] = []
        for path in root.rglob("*"):
            if path.name.startswith(".") or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                if path.is_file() and path.stat().st_size > 0:
                    files.append(path)
            except OSError:
                # SMB 内容可能在扫描期间被摄影或剪辑人员移动，下一次扫描再处理。
                continue
        files.sort()
        seen: set[str] = set()
        stats = {"created": 0, "updated": 0, "unchanged": 0, "missing": 0, "total": len(files)}

        for index, path in enumerate(files):
            relative_path = path.relative_to(root).as_posix()
            seen.add(relative_path)
            stat = path.stat()
            if progress:
                progress(index / max(1, len(files)), f"正在读取 {relative_path}")

            with self.db.session() as conn:
                existing = conn.execute(
                    "SELECT * FROM assets WHERE project_id=? AND relative_path=?",
                    (project_id, relative_path),
                ).fetchone()

            fingerprint: str | None = None
            unchanged = existing and existing["size_bytes"] == stat.st_size and abs(existing["mtime"] - stat.st_mtime) < 0.001
            if unchanged and existing["analysis_exempted_at"]:
                # An exemption is tied to the actual bytes, not only the path or the
                # cheap size/mtime shortcut. This extra sampled hash is paid only for
                # the small number of manually exempted assets.
                fingerprint = fast_fingerprint(path)
                unchanged = fingerprint == existing["analysis_exempt_fingerprint"]
            needs_metadata = bool(
                existing and media_type_for(path) == "video"
                and (
                    any(
                        not has_positive_number(existing[field])
                        for field in ("duration", "width", "height", "fps")
                    )
                    or not str(existing["codec"] or "").strip()
                )
            )
            if unchanged:
                with self.db.session() as conn:
                    if needs_metadata:
                        metadata = probe_media(path)
                        recovered_duration = bool(
                            not has_positive_number(existing["duration"])
                            and has_positive_number(metadata["duration"])
                            and str(existing["analysis_strategy"] or "")
                            in {"抽帧兼容模式", "frame_fallback"}
                        )
                        conn.execute(
                            """UPDATE assets SET duration=COALESCE(?, duration), width=COALESCE(?, width),
                               height=COALESCE(?, height), fps=COALESCE(?, fps),
                               codec=COALESCE(?, codec), bit_rate=COALESCE(?, bit_rate),
                               analysis_status=CASE WHEN ? THEN 'pending' ELSE analysis_status END,
                               analysis_exempted_at=CASE WHEN ? THEN NULL ELSE analysis_exempted_at END,
                               analysis_exempt_reason=CASE WHEN ? THEN NULL ELSE analysis_exempt_reason END,
                               analysis_exempt_fingerprint=CASE WHEN ? THEN NULL ELSE analysis_exempt_fingerprint END,
                               status='active',
                               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                            (
                                metadata["duration"], metadata["width"], metadata["height"],
                                metadata["fps"], metadata["codec"], metadata["bit_rate"],
                                recovered_duration, recovered_duration, recovered_duration,
                                recovered_duration, existing["id"],
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE assets SET status='active', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (existing["id"],),
                        )
                stats["unchanged"] += 1
                continue

            fingerprint = fingerprint or fast_fingerprint(path)
            metadata = probe_media(path)
            media_type = media_type_for(path)

            if existing:
                with self.db.session() as conn:
                    conn.execute(
                        """UPDATE assets SET filename=?, extension=?, media_type=?, size_bytes=?, mtime=?,
                           content_fingerprint=?, duration=?, width=?, height=?, fps=?, codec=?, bit_rate=?, status='active',
                           analysis_status='pending', analysis_summary=NULL, analysis_keywords=NULL,
                           analysis_json=NULL, analysis_error=NULL, analysis_strategy=NULL, analyzed_at=NULL,
                           analysis_exempted_at=NULL, analysis_exempt_reason=NULL,
                           analysis_exempt_fingerprint=NULL,
                           thumb_path=NULL, thumb_status='pending', thumb_error=NULL,
                           thumb_updated_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (path.name, path.suffix.lower(), media_type, stat.st_size, stat.st_mtime,
                         fingerprint, metadata["duration"], metadata["width"], metadata["height"],
                         metadata["fps"], metadata["codec"], metadata["bit_rate"], existing["id"]),
                    )
                stats["updated"] += 1
                continue

            with self.db.session() as conn:
                candidates = conn.execute(
                    """SELECT * FROM assets WHERE project_id=? AND content_fingerprint=?
                       ORDER BY CASE WHEN status='missing' THEN 0 ELSE 1 END, updated_at DESC""",
                    (project_id, fingerprint),
                ).fetchall()
                moved = next(
                    (
                        candidate for candidate in candidates
                        if candidate["status"] == "missing"
                        or not (root / candidate["relative_path"]).exists()
                    ),
                    None,
                )
                if moved:
                    conn.execute(
                        """UPDATE assets SET relative_path=?, filename=?, extension=?, media_type=?, size_bytes=?,
                           mtime=?, duration=?, width=?, height=?, fps=?, codec=?, bit_rate=?,
                           status='active', updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (relative_path, path.name, path.suffix.lower(), media_type, stat.st_size, stat.st_mtime,
                         metadata["duration"], metadata["width"], metadata["height"], metadata["fps"],
                         metadata["codec"], metadata["bit_rate"], moved["id"]),
                    )
                    stats["updated"] += 1
                else:
                    conn.execute(
                        """INSERT INTO assets(project_id, relative_path, filename, extension, media_type,
                           size_bytes, mtime, content_fingerprint, duration, width, height, fps, codec, bit_rate)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (project_id, relative_path, path.name, path.suffix.lower(), media_type,
                         stat.st_size, stat.st_mtime, fingerprint, metadata["duration"], metadata["width"],
                         metadata["height"], metadata["fps"], metadata["codec"], metadata["bit_rate"]),
                    )
                    stats["created"] += 1

        with self.db.session() as conn:
            active_rows = conn.execute(
                "SELECT id, relative_path FROM assets WHERE project_id=? AND status='active'",
                (project_id,),
            ).fetchall()
            missing_ids = [row["id"] for row in active_rows if row["relative_path"] not in seen]
            if missing_ids:
                conn.executemany(
                    "UPDATE assets SET status='missing', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    [(asset_id,) for asset_id in missing_ids],
                )
                stats["missing"] = len(missing_ids)
            conn.execute(
                "UPDATE projects SET last_scan_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (project_id,),
            )

        if progress:
            progress(1.0, f"扫描完成：{len(files)} 个素材")
        return stats
