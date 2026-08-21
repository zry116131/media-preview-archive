from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request


def file_station_path(smb_url: str, relative_path: str = "") -> str:
    """Convert an SMB project URL and asset-relative path to a File Station path."""
    parsed = urllib.parse.urlparse(str(smb_url or "").strip())
    if parsed.scheme.casefold() != "smb" or not parsed.hostname:
        raise ValueError("项目没有有效的 SMB 地址")
    base_parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    asset_parts = [
        urllib.parse.unquote(part)
        for part in str(relative_path or "").replace("\\", "/").split("/")
        if part
    ]
    if not base_parts or any(part in {".", ".."} for part in base_parts + asset_parts):
        raise ValueError("NAS 素材路径无效")
    return "/" + "/".join(base_parts + asset_parts)


class NasFileStationClient:
    """Create browser-consumable NAS URLs without exposing the NAS password."""

    def __init__(
        self,
        base_url: str | None = None,
        public_base_url: str | None = None,
        account: str | None = None,
        password: str | None = None,
        verify_tls: bool | None = None,
        session_ttl_seconds: int | None = None,
    ):
        self.base_url = (base_url or os.environ.get("NAS_BASE_URL", "")).rstrip("/")
        self.public_base_url = (
            public_base_url or os.environ.get("NAS_PUBLIC_BASE_URL", "") or self.base_url
        ).rstrip("/")
        self.account = account if account is not None else os.environ.get("NAS_ACCOUNT", "")
        self.password = password if password is not None else os.environ.get("NAS_PASSWORD", "")
        self.verify_tls = (
            verify_tls
            if verify_tls is not None
            else os.environ.get("NAS_VERIFY_TLS", "true").casefold() not in {"0", "false", "no"}
        )
        self.session_ttl_seconds = session_ttl_seconds or int(
            os.environ.get("NAS_SESSION_TTL_SECONDS", "2700")
        )
        self._sid = ""
        self._sid_expires_at = 0.0
        self._auth_path = "entry.cgi"
        self._auth_version = 7
        self._download_path = "entry.cgi"
        self._download_version = 2
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.public_base_url and self.account and self.password)

    def public_status(self) -> dict[str, object]:
        host = urllib.parse.urlparse(self.public_base_url).hostname or ""
        return {"enabled": self.enabled, "host": host, "mode": "nas_file_station"}

    def health(self) -> dict[str, object]:
        status = self.public_status()
        status.update({"online": False, "message": "NAS 统一访问账户尚未配置"})
        if not self.enabled:
            return status
        started = time.monotonic()
        try:
            self._session_id()
            status.update(
                {
                    "online": True,
                    "message": "NAS 直连服务在线",
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "session_expires_in": max(
                        1, int(self._sid_expires_at - time.monotonic())
                    ),
                }
            )
        except Exception as exc:
            status.update(
                {
                    "message": str(exc),
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
            )
        return status

    def _ssl_context(self):
        if self.verify_tls:
            return ssl.create_default_context()
        return ssl._create_unverified_context()

    def _request_json(self, path: str, fields: dict[str, object]) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/webapi/{path}",
            data=urllib.parse.urlencode(fields).encode("utf-8"),
        )
        try:
            with urllib.request.urlopen(
                request, context=self._ssl_context(), timeout=20
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("无法连接 NAS 统一访问服务") from exc
        return payload

    def _discover(self) -> None:
        payload = self._request_json(
            "query.cgi",
            {
                "api": "SYNO.API.Info",
                "version": 1,
                "method": "query",
                "query": "SYNO.API.Auth,SYNO.FileStation.Download",
            },
        )
        try:
            auth = payload["data"]["SYNO.API.Auth"]
            download = payload["data"]["SYNO.FileStation.Download"]
            self._auth_path = str(auth["path"])
            self._auth_version = min(int(auth["maxVersion"]), 7)
            self._download_path = str(download["path"])
            self._download_version = min(int(download["maxVersion"]), 2)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("NAS 未提供 File Station 下载接口") from exc

    def _login(self) -> str:
        self._discover()
        payload = self._request_json(
            self._auth_path,
            {
                "api": "SYNO.API.Auth",
                "version": self._auth_version,
                "method": "login",
                "account": self.account,
                "passwd": self.password,
                "session": "FileStation",
                "format": "sid",
            },
        )
        if not payload.get("success") or not payload.get("data", {}).get("sid"):
            code = payload.get("error", {}).get("code", "unknown")
            raise RuntimeError(f"NAS 统一账户登录失败（File Station 错误码 {code}）")
        self._sid = str(payload["data"]["sid"])
        self._sid_expires_at = time.monotonic() + self.session_ttl_seconds
        return self._sid

    def _session_id(self) -> str:
        with self._lock:
            if self._sid and time.monotonic() < self._sid_expires_at:
                return self._sid
            return self._login()

    def direct_url(self, smb_url: str, relative_path: str = "") -> tuple[str, int]:
        if not self.enabled:
            raise RuntimeError("NAS 统一访问账户尚未配置")
        sid = self._session_id()
        query = urllib.parse.urlencode(
            {
                "api": "SYNO.FileStation.Download",
                "version": self._download_version,
                "method": "download",
                "path": json.dumps([file_station_path(smb_url, relative_path)], ensure_ascii=False),
                "mode": json.dumps("open"),
                "_sid": sid,
            }
        )
        remaining = max(1, int(self._sid_expires_at - time.monotonic()))
        return f"{self.public_base_url}/webapi/{self._download_path}?{query}", remaining
