#!/usr/bin/env python3
"""Write Media Workbench NAS credentials to a protected systemd env file."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path


def quote_env(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("环境变量不能包含换行")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--base-url", default="https://192.0.2.20:5001")
    parser.add_argument("--public-base-url", default="https://192.0.2.20:5001")
    parser.add_argument(
        "--output",
        default=str(Path.home() / ".config/media-workbench/nas.env"),
    )
    parser.add_argument("--no-verify-tls", action="store_true")
    args = parser.parse_args()

    password = getpass.getpass("NAS password: ")
    if not password:
        raise SystemExit("NAS password must not be empty")
    target = Path(args.output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    content = "\n".join(
        [
            f"NAS_BASE_URL={quote_env(args.base_url)}",
            f"NAS_PUBLIC_BASE_URL={quote_env(args.public_base_url)}",
            f"NAS_ACCOUNT={quote_env(args.account)}",
            f"NAS_PASSWORD={quote_env(password)}",
            f"NAS_VERIFY_TLS={'false' if args.no_verify_tls else 'true'}",
            "NAS_SESSION_TTL_SECONDS=2700",
            "",
        ]
    )
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, content.encode("utf-8"))
    finally:
        os.close(descriptor)
    target.chmod(0o600)
    print(f"configured path={target} mode={oct(target.stat().st_mode & 0o777)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
