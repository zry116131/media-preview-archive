#!/usr/bin/env python3
"""Authenticated LAN proxy for the localhost-only JoyAI vLLM API."""

from __future__ import annotations

import hmac
import http.client
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LISTEN_HOST = os.environ.get("JOYAI_PROXY_HOST", "192.0.2.10")
LISTEN_PORT = int(os.environ.get("JOYAI_PROXY_PORT", "7060"))
UPSTREAM_HOST = os.environ.get("JOYAI_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("JOYAI_UPSTREAM_PORT", "7060"))
API_KEY = os.environ.get("JOYAI_PROXY_API_KEY", "")
MAX_BODY_BYTES = int(os.environ.get("JOYAI_PROXY_MAX_BODY_BYTES", str(256 * 1024 * 1024)))

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class JoyAIProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "JoyAILANProxy/1.0"

    def _json_error(self, status: int, message: str) -> None:
        payload = json.dumps({"error": {"message": message}}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        expected = f"Bearer {API_KEY}"
        return bool(API_KEY) and hmac.compare_digest(authorization, expected)

    def _proxy(self) -> None:
        if not self._authorized():
            self._json_error(401, "Missing or invalid JoyAI API key")
            return

        content_length_header = self.headers.get("Content-Length")
        if content_length_header is None:
            body = None
        else:
            try:
                content_length = int(content_length_header)
            except ValueError:
                self._json_error(400, "Invalid Content-Length")
                return
            if content_length < 0 or content_length > MAX_BODY_BYTES:
                self._json_error(413, "Request body is too large")
                return
            body = self.rfile.read(content_length)

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
            and key.lower() not in {"authorization", "host", "content-length"}
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"

        upstream = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=900)
        try:
            upstream.request(self.command, self.path, body=body, headers=headers)
            response = upstream.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length":
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            self.close_connection = True
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            if not self.wfile.closed:
                self._json_error(502, f"JoyAI upstream unavailable: {exc}")
        finally:
            upstream.close()

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_PATCH = _proxy
    do_DELETE = _proxy
    do_OPTIONS = _proxy
    do_HEAD = _proxy

    def log_message(self, format_string: str, *args: object) -> None:
        super().log_message(format_string, *args)


def main() -> None:
    if not API_KEY:
        raise SystemExit("JOYAI_PROXY_API_KEY must be configured")
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), JoyAIProxyHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
