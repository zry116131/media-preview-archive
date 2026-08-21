#!/usr/bin/env python3
"""Create a root-only mount.cifs credentials file from the existing NAS env."""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        parsed = shlex.split(raw_value, posix=True)
        values[key.strip()] = parsed[0] if parsed else ""
    return values


def validate(value: str, label: str) -> str:
    if not value:
        raise ValueError(f"{label} must not be empty")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(f"{label} contains unsupported characters")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        default=str(Path.home() / ".config/media-workbench/nas.env"),
    )
    parser.add_argument(
        "--output",
        default="/etc/media-workbench/smb-credentials",
    )
    args = parser.parse_args()

    values = parse_env(Path(args.source))
    username = validate(values.get("NAS_ACCOUNT", ""), "NAS_ACCOUNT")
    password = validate(values.get("NAS_PASSWORD", ""), "NAS_PASSWORD")
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    content = f"username={username}\npassword={password}\n"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, content.encode("utf-8"))
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    print(f"configured path={target} mode={oct(target.stat().st_mode & 0o777)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
