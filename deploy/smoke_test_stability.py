#!/usr/bin/env python3
"""Smoke test pagination, storage health, thumbnail jobs and analysis options."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request


def request_json(url: str, payload: dict | None = None) -> dict | list:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_task(app_url: str, task_id: int, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = request_json(f"{app_url}/api/tasks")
        task = next((item for item in tasks if item["id"] == task_id), None)
        if task and task["status"] in {"completed", "failed"}:
            return task
        time.sleep(0.4)
    raise TimeoutError(f"task {task_id} did not finish")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-url", default="http://127.0.0.1:18080")
    args = parser.parse_args()
    app_url = args.app_url.rstrip("/")
    projects = request_json(f"{app_url}/api/projects")
    largest = max(projects, key=lambda item: item.get("active_count") or 0)
    smallest = min(projects, key=lambda item: item.get("active_count") or 0)

    page_one = request_json(
        f"{app_url}/api/projects/{largest['id']}/assets?paged=1&limit=120&offset=0"
    )
    page_two = request_json(
        f"{app_url}/api/projects/{largest['id']}/assets?paged=1&limit=120&offset=120"
    )
    ids_one = {item["id"] for item in page_one["items"]}
    ids_two = {item["id"] for item in page_two["items"]}
    if ids_one & ids_two:
        raise RuntimeError("pagination returned duplicate assets")

    storage = request_json(
        f"{app_url}/api/storage/health?project_id={largest['id']}"
    )
    thumbnail = request_json(
        f"{app_url}/api/projects/{smallest['id']}/thumbnails", {}
    )
    thumbnail_task = wait_task(app_url, thumbnail["task_id"])
    analysis = request_json(
        f"{app_url}/api/projects/{largest['id']}/analysis",
        {
            "scope": "pending",
            "media_type": "all",
            "folder_prefix": "__stability_smoke_no_match__",
            "limit": 1,
        },
    )
    analysis_task = wait_task(app_url, analysis["task_id"])
    health = request_json(f"{app_url}/api/health")
    print(
        "stability_ok",
        f"version={health.get('version')}",
        f"total={page_one['total']}",
        f"page1={len(ids_one)}",
        f"page2={len(ids_two)}",
        f"pages_disjoint={not bool(ids_one & ids_two)}",
        f"storage={storage['status']}",
        f"thumbnail_task={thumbnail_task['status']}",
        f"analysis_task={analysis_task['status']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
