#!/usr/bin/env python3
"""Migrate SMB projects from ephemeral GVFS paths to a stable system mount."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--share", required=True)
    parser.add_argument("--mount-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root))
    os.environ["MEDIA_APP_SMB_SERVER"] = args.server
    os.environ["MEDIA_APP_SMB_SHARE"] = args.share
    os.environ["MEDIA_APP_SMB_MOUNT_ROOT"] = args.mount_root
    from app.paths import resolve_smb_url

    connection = sqlite3.connect(args.database, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        active_tasks = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running','pausing','paused')"
        ).fetchone()[0]
        if active_tasks:
            raise RuntimeError(f"refusing migration while {active_tasks} task(s) are active")

        updates: list[tuple[str, int]] = []
        for project in connection.execute(
            "SELECT id, name, root_path, smb_url FROM projects ORDER BY id"
        ):
            parsed = urlparse(project["smb_url"])
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            if parsed.hostname != args.server or not parts or parts[0] != args.share:
                continue
            stable_root = str(resolve_smb_url(project["smb_url"]))
            updates.append((stable_root, int(project["id"])))
            print(f"project={project['id']} old={project['root_path']} new={stable_root}")

        if not updates:
            raise RuntimeError("no matching SMB projects found")
        if not args.dry_run:
            with connection:
                connection.executemany(
                    "UPDATE projects SET root_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    updates,
                )
        print(f"{'validated' if args.dry_run else 'migrated'}={len(updates)}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
