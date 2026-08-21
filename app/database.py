from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL UNIQUE,
    smb_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_scan_at TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    content_fingerprint TEXT NOT NULL,
    duration REAL,
    width INTEGER,
    height INTEGER,
    fps REAL,
    codec TEXT,
    bit_rate INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    analysis_status TEXT NOT NULL DEFAULT 'pending',
    analysis_summary TEXT,
    analysis_keywords TEXT,
    analysis_json TEXT,
    analysis_error TEXT,
    analysis_strategy TEXT,
    analyzed_at TEXT,
    analysis_exempted_at TEXT,
    analysis_exempt_reason TEXT,
    analysis_exempt_fingerprint TEXT,
    thumb_path TEXT,
    thumb_status TEXT NOT NULL DEFAULT 'pending',
    thumb_error TEXT,
    thumb_updated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, relative_path)
);

CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id);
CREATE INDEX IF NOT EXISTS idx_assets_fingerprint ON assets(project_id, content_fingerprint);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(project_id, status);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    pause_requested INTEGER NOT NULL DEFAULT 0,
    paused_at TEXT,
    total_paused_seconds REAL NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS storyboards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    source_format TEXT NOT NULL DEFAULT 'paste',
    source_text TEXT NOT NULL DEFAULT '',
    active_version_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_storyboards_project ON storyboards(project_id, updated_at);

CREATE TABLE IF NOT EXISTS storyboard_shots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storyboard_id INTEGER NOT NULL REFERENCES storyboards(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    narration TEXT NOT NULL DEFAULT '',
    visual_request TEXT NOT NULL DEFAULT '',
    shot_type TEXT NOT NULL DEFAULT '',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    selected_asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(storyboard_id, position)
);

CREATE INDEX IF NOT EXISTS idx_storyboard_shots_board ON storyboard_shots(storyboard_id, position);

CREATE TABLE IF NOT EXISTS storyboard_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storyboard_id INTEGER NOT NULL REFERENCES storyboards(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    planner TEXT NOT NULL DEFAULT '',
    warning TEXT NOT NULL DEFAULT '',
    shots_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(storyboard_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_storyboard_versions_board
ON storyboard_versions(storyboard_id, version_number DESC);

CREATE TABLE IF NOT EXISTS model_settings (
    role TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
            migrations = {
                "analysis_summary": "TEXT",
                "analysis_keywords": "TEXT",
                "analysis_json": "TEXT",
                "analysis_error": "TEXT",
                "analysis_strategy": "TEXT",
                "analyzed_at": "TEXT",
                "analysis_exempted_at": "TEXT",
                "analysis_exempt_reason": "TEXT",
                "analysis_exempt_fingerprint": "TEXT",
                "fps": "REAL",
                "codec": "TEXT",
                "bit_rate": "INTEGER",
                "thumb_status": "TEXT NOT NULL DEFAULT 'pending'",
                "thumb_error": "TEXT",
                "thumb_updated_at": "TEXT",
            }
            for name, column_type in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE assets ADD COLUMN {name} {column_type}")
            task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            task_migrations = {
                "pause_requested": "INTEGER NOT NULL DEFAULT 0",
                "paused_at": "TEXT",
                "total_paused_seconds": "REAL NOT NULL DEFAULT 0",
                "options_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, column_type in task_migrations.items():
                if name not in task_columns:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {column_type}")
            storyboard_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(storyboards)")
            }
            if "active_version_id" not in storyboard_columns:
                conn.execute("ALTER TABLE storyboards ADD COLUMN active_version_id INTEGER")
            conn.execute(
                """UPDATE assets SET thumb_status='completed'
                   WHERE thumb_path IS NOT NULL AND thumb_path != ''
                     AND thumb_status='pending'"""
            )

    def create_task(
        self,
        project_id: int | None,
        task_type: str,
        message: str = "",
        options_json: str = "{}",
    ) -> int:
        with self.session() as conn:
            cur = conn.execute(
                """INSERT INTO tasks(project_id, task_type, message, options_json)
                   VALUES (?, ?, ?, ?)""",
                (project_id, task_type, message, options_json),
            )
            return int(cur.lastrowid)

    def update_task_options(self, task_id: int, options_json: str) -> None:
        with self.session() as conn:
            conn.execute(
                "UPDATE tasks SET options_json=? WHERE id=?", (options_json, task_id)
            )

    def update_task(self, task_id: int, *, status: str | None = None,
                    progress: float | None = None, message: str | None = None) -> None:
        fields: list[str] = []
        values: list[object] = []
        if status is not None:
            fields.append("status=?")
            values.append(status)
            if status == "running":
                fields.append("started_at=COALESCE(started_at, CURRENT_TIMESTAMP)")
            if status in {"completed", "failed"}:
                fields.append("finished_at=CURRENT_TIMESTAMP")
        if progress is not None:
            fields.append("progress=?")
            values.append(max(0.0, min(1.0, progress)))
        if message is not None:
            fields.append("message=?")
            values.append(message)
        if not fields:
            return
        values.append(task_id)
        with self.session() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", values)

    def request_pause(self, task_id: int) -> bool:
        with self.session() as conn:
            task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["status"] not in {"queued", "running", "pausing"}:
                return False
            conn.execute(
                "UPDATE tasks SET pause_requested=1, status='pausing' WHERE id=?",
                (task_id,),
            )
            return True

    def resume_task(self, task_id: int) -> bool:
        with self.session() as conn:
            task = conn.execute(
                "SELECT status, started_at, paused_at FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not task or task["status"] not in {"paused", "pausing"}:
                return False
            next_status = "running" if task["started_at"] else "queued"
            conn.execute(
                """UPDATE tasks SET pause_requested=0, status=?,
                   total_paused_seconds=total_paused_seconds +
                     CASE WHEN paused_at IS NULL THEN 0
                          ELSE (julianday('now') - julianday(paused_at)) * 86400 END,
                   paused_at=NULL WHERE id=?""",
                (next_status, task_id),
            )
            return True
