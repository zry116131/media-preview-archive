from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


def normalize_smb_url(value: str) -> str:
    """Normalize an SMB URL or Windows UNC path to an smb:// URL."""
    raw = str(value or "").strip()
    if raw.casefold().startswith("smb://"):
        parsed = urlparse(raw)
        if not parsed.hostname:
            raise ValueError("SMB 地址格式无效")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("SMB 地址必须包含共享名称")
        return raw

    if raw.startswith("\\\\") or raw.startswith("//"):
        parts = [part for part in raw.lstrip("\\/").replace("\\", "/").split("/") if part]
        if len(parts) < 2:
            raise ValueError("Windows 地址必须包含服务器和共享名称")
        server, *path_parts = parts
        encoded_path = "/".join(quote(unquote(part), safe="") for part in path_parts)
        return f"smb://{server}/{encoded_path}"

    raise ValueError("请输入 Windows 共享地址（\\\\服务器\\共享）或 smb:// 地址")


def windows_unc_path(smb_url: str, relative_path: str = "") -> str:
    """Convert an smb:// URL and optional relative path to a Windows UNC path."""
    value = str(smb_url or "").strip()
    if value.startswith("\\\\"):
        base = value.rstrip("\\/")
        suffix = str(relative_path or "").replace("/", "\\").lstrip("\\")
        return f"{base}\\{suffix}" if suffix else base
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "smb" or not parsed.hostname:
        return str(relative_path or value).replace("/", "\\")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    parts.extend(part for part in str(relative_path or "").replace("\\", "/").split("/") if part)
    suffix = "\\".join(parts)
    return f"\\\\{parsed.hostname}\\{suffix}" if suffix else f"\\\\{parsed.hostname}"


def _parse_gvfs_mount_name(name: str) -> dict[str, str]:
    if not name.startswith("smb-share:"):
        return {}
    fields: dict[str, str] = {}
    for item in name.removeprefix("smb-share:").split(","):
        key, separator, value = item.partition("=")
        if separator:
            fields[key.casefold()] = value
    return fields


def _resolve_static_smb_mount(
    server: str,
    share: str,
    relative_parts: list[str],
) -> Path | None:
    mount_root = os.environ.get("MEDIA_APP_SMB_MOUNT_ROOT", "").strip()
    configured_server = os.environ.get("MEDIA_APP_SMB_SERVER", "").strip()
    configured_share = os.environ.get("MEDIA_APP_SMB_SHARE", "").strip()
    if not (mount_root and configured_server and configured_share):
        return None
    if configured_server.casefold() != server.casefold():
        return None
    if configured_share.casefold() != share.casefold():
        return None

    root = Path(mount_root).expanduser().resolve()
    if not root.is_dir() or not os.access(root, os.R_OK):
        raise ValueError(f"SMB 固定挂载不可读：{root}")
    target = root.joinpath(*relative_parts).resolve()
    if target != root and root not in target.parents:
        raise ValueError("SMB 项目路径超出固定挂载范围")
    if not target.is_dir():
        raise ValueError(f"SMB 项目目录不存在：{'/'.join(relative_parts) or share}")
    return target


def resolve_smb_url(value: str, runtime_dir: Path | None = None) -> Path:
    value = normalize_smb_url(value)
    parsed = urlparse(value)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    share, *relative_parts = parts
    static_mount = _resolve_static_smb_mount(
        parsed.hostname,
        share,
        relative_parts,
    )
    if static_mount is not None:
        return static_mount

    runtime = runtime_dir or Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    gvfs_root = runtime / "gvfs"

    if not gvfs_root.is_dir():
        raise ValueError("当前服务节点没有可用的桌面 SMB 挂载")

    matched_mount: Path | None = None
    for mount in gvfs_root.iterdir():
        if not mount.is_dir():
            continue
        fields = _parse_gvfs_mount_name(mount.name)
        if (
            fields.get("server", "").casefold() == parsed.hostname.casefold()
            and fields.get("share", "").casefold() == share.casefold()
        ):
            matched_mount = mount
            break

    if matched_mount is None:
        raise ValueError(f"SMB 共享尚未挂载：smb://{parsed.hostname}/{share}")

    target = matched_mount.joinpath(*relative_parts).resolve()
    if not target.is_dir():
        raise ValueError(f"SMB 项目目录不存在：{'/'.join(relative_parts) or share}")
    return target


def resolve_project_root(value: str) -> Path:
    if value.casefold().startswith("smb://") or value.startswith("\\\\") or value.startswith("//"):
        root = resolve_smb_url(value)
    else:
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("素材目录不存在或不是目录")

    allowed = [p for p in os.environ.get("MEDIA_APP_ALLOWED_ROOTS", "").split(os.pathsep) if p]
    if allowed:
        allowed_paths = [Path(p).expanduser().resolve() for p in allowed]
        if not any(root == allowed_root or allowed_root in root.parents for allowed_root in allowed_paths):
            raise ValueError("素材目录不在允许的根目录内")
    return root
