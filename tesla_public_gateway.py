#!/usr/bin/env python3
from __future__ import annotations

import argparse
import mimetypes
import ssl
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent


CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


@dataclass
class GatewayConfig:
    partner_domain: str
    public_bind_host: str
    public_http_port: int
    public_https_port: int
    public_static_root: Path
    tls_cert_file: Path
    tls_key_file: Path


def load_gateway_config(config_path: Path) -> GatewayConfig:
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    tesla = dict(data.get("tesla_energy") or {})
    partner_domain = str(tesla.get("partner_domain") or "").strip()
    return GatewayConfig(
        partner_domain=partner_domain,
        public_bind_host=str(tesla.get("public_bind_host") or "0.0.0.0").strip(),
        public_http_port=int(tesla.get("public_http_port") or 8078),
        public_https_port=int(tesla.get("public_https_port") or 9443),
        public_static_root=Path(str(tesla.get("public_static_root") or "/var/www/tesla")),
        tls_cert_file=Path(
            str(tesla.get("tls_cert_file") or BASE_DIR / ".secrets" / "tls" / partner_domain / "fullchain.pem")
        ),
        tls_key_file=Path(
            str(tesla.get("tls_key_file") or BASE_DIR / ".secrets" / "tls" / partner_domain / "privkey.pem")
        ),
    )


def safe_static_path(root: Path, request_path: str) -> Path | None:
    rel = request_path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], config: GatewayConfig, is_https: bool):
        super().__init__(server_address, handler_class)
        self.gateway_config = config
        self.is_https = is_https


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle(head_only=True)

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def _handle(self, head_only: bool = False) -> None:
        # This gateway exists solely to serve Tesla partner validation and
        # Let's Encrypt challenge files, both of which live under /.well-known/.
        # Everything else is refused: the gateway must never proxy arbitrary
        # requests to the internal dashboard, which has no authentication.
        if self.path.startswith("/.well-known/"):
            self._serve_static(head_only=head_only)
            return
        self.send_error(404, "Not Found")

    def _serve_static(self, head_only: bool = False) -> None:
        root = self.server.gateway_config.public_static_root
        target = safe_static_path(root, self.path)
        if target is None or not target.is_file():
            self.send_error(404, "Not Found")
            return
        content_type, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".pem":
            content_type = "application/pem-certificate-chain"
        body = b"" if head_only else target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        prefix = "HTTPS" if self.server.is_https else "HTTP"
        print(f"{prefix} {self.address_string()} - {fmt % args}")


def start_http_server(config: GatewayConfig) -> GatewayServer:
    server = GatewayServer((config.public_bind_host, config.public_http_port), GatewayHandler, config, is_https=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def start_https_server(config: GatewayConfig) -> GatewayServer | None:
    if not config.tls_cert_file.exists() or not config.tls_key_file.exists():
        return None
    server = GatewayServer((config.public_bind_host, config.public_https_port), GatewayHandler, config, is_https=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(config.tls_cert_file), str(config.tls_key_file))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Public HTTP/HTTPS gateway for Tesla partner integration")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()

    config = load_gateway_config(Path(args.config))
    http_server = start_http_server(config)
    https_server = start_https_server(config)

    print(f"HTTP gateway listening on {config.public_bind_host}:{config.public_http_port}")
    if https_server:
        print(f"HTTPS gateway listening on {config.public_bind_host}:{config.public_https_port}")
    else:
        print("HTTPS gateway not started because TLS cert/key are not installed yet")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        http_server.shutdown()
        if https_server:
            https_server.shutdown()


if __name__ == "__main__":
    main()
