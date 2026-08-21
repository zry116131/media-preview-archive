#!/usr/bin/env python3
"""Smoke test Media Workbench direct NAS playback without printing the session URL."""

from __future__ import annotations

import argparse
import json
import ssl
import urllib.parse
import urllib.request


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-url", default="http://127.0.0.1:18080")
    parser.add_argument("--asset-id", type=int, required=True)
    args = parser.parse_args()

    health = get_json(f"{args.app_url}/api/health")
    direct = get_json(f"{args.app_url}/api/assets/{args.asset_id}/direct-play")
    parsed = urllib.parse.urlparse(direct["url"])
    request = urllib.request.Request(direct["url"], headers={"Range": "bytes=0-1048575"})
    with urllib.request.urlopen(
        request, context=ssl._create_unverified_context(), timeout=30
    ) as response:
        payload = response.read()
        print(
            "direct_play_ok",
            f"app_version={health.get('version')}",
            f"nas_enabled={health.get('nas_access', {}).get('enabled')}",
            f"host={parsed.hostname}",
            f"status={response.status}",
            f"bytes={len(payload)}",
            f"content_range={response.headers.get('Content-Range', '')}",
            f"content_type={response.headers.get('Content-Type', '')}",
            f"expires_in={direct.get('expires_in')}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
