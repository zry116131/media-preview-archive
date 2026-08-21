#!/usr/bin/env python3
"""Safely verify NAS File Station login and ranged direct download."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import ssl
import urllib.parse
import urllib.request


def request_json(base_url: str, path: str, fields: dict[str, object]) -> dict:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(f"{base_url}{path}", data=body)
    with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://192.0.2.20:5001")
    parser.add_argument("--account", required=True)
    path_group = parser.add_mutually_exclusive_group(required=True)
    path_group.add_argument("--path")
    path_group.add_argument("--path-base64")
    args = parser.parse_args()
    nas_path = (
        base64.b64decode(args.path_base64).decode("utf-8")
        if args.path_base64
        else args.path
    )

    password = getpass.getpass("NAS password: ")
    info = request_json(
        args.base_url,
        "/webapi/query.cgi",
        {
            "api": "SYNO.API.Info",
            "version": 1,
            "method": "query",
            "query": "SYNO.API.Auth,SYNO.FileStation.Download",
        },
    )
    auth_info = info["data"]["SYNO.API.Auth"]
    download_info = info["data"]["SYNO.FileStation.Download"]
    auth_path = f"/webapi/{auth_info['path']}"
    download_path = f"/webapi/{download_info['path']}"

    login = request_json(
        args.base_url,
        auth_path,
        {
            "api": "SYNO.API.Auth",
            "version": min(int(auth_info["maxVersion"]), 7),
            "method": "login",
            "account": args.account,
            "passwd": password,
            "session": "FileStation",
            "format": "sid",
        },
    )
    if not login.get("success"):
        print(f"login_failed code={login.get('error', {}).get('code', 'unknown')}")
        return 2

    sid = login["data"]["sid"]
    try:
        query = urllib.parse.urlencode(
            {
                "api": "SYNO.FileStation.Download",
                "version": min(int(download_info["maxVersion"]), 2),
                "method": "download",
                "path": json.dumps([nas_path], ensure_ascii=False),
                "mode": json.dumps("open"),
                "_sid": sid,
            }
        )
        request = urllib.request.Request(
            f"{args.base_url}{download_path}?{query}",
            headers={"Range": "bytes=0-1048575"},
        )
        with urllib.request.urlopen(
            request, context=ssl._create_unverified_context(), timeout=30
        ) as response:
            payload = response.read()
            print(
                "download_ok",
                f"status={response.status}",
                f"bytes={len(payload)}",
                f"accept_ranges={response.headers.get('Accept-Ranges', '')}",
                f"content_range={response.headers.get('Content-Range', '')}",
                f"content_type={response.headers.get('Content-Type', '')}",
            )
    finally:
        request_json(
            args.base_url,
            auth_path,
            {
                "api": "SYNO.API.Auth",
                "version": min(int(auth_info["maxVersion"]), 7),
                "method": "logout",
                "session": "FileStation",
                "_sid": sid,
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
