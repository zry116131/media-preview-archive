#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--asset-id", required=True, type=int)
    args = parser.parse_args()
    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT id, relative_path, analysis_status, analysis_json FROM assets WHERE id=?",
        (args.asset_id,),
    ).fetchone()
    connection.close()
    if not row:
        raise SystemExit("asset_not_found")
    analysis = json.loads(row["analysis_json"] or "{}")
    strategy = analysis.get("strategy") if isinstance(analysis, dict) else None
    print(json.dumps({
        "id": row["id"],
        "path": row["relative_path"],
        "status": row["analysis_status"],
        "strategy": strategy,
        "segment_count": len(analysis.get("segments", [])),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
