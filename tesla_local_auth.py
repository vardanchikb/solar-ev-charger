#!/usr/bin/env python3
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from tesla_energy import TeslaEnergyMonitor


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml root must be a mapping")
    data.setdefault("database", {})
    data.setdefault("tesla_energy", {})
    return data


def make_handler(monitor: TeslaEnergyMonitor):
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                authorize_url = monitor.build_authorize_url()
                if not authorize_url:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"Tesla auth is not configured.")
                    return
                self.send_response(302)
                self.send_header("Location", authorize_url)
                self.end_headers()
                return

            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(parsed.query)
            code = (params.get("code") or [""])[0].strip()
            state = (params.get("state") or [""])[0].strip() or None
            error = (params.get("error_description") or params.get("error") or [""])[0].strip()

            if error:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f"Tesla authorization failed: {error}".encode("utf-8"))
                return
            if not code:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Tesla callback did not include a code.")
                return

            try:
                monitor.exchange_code(code, state=state)
            except Exception as exc:  # noqa: BLE001
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Tesla token exchange failed: {exc}".encode("utf-8"))
                return

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Tesla authorization complete. You can close this tab.")

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

    return CallbackHandler


def main() -> int:
    parser = argparse.ArgumentParser(description="Localhost Tesla OAuth helper")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    monitor = TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database"))
    server = HTTPServer((args.host, args.port), make_handler(monitor))
    print(f"Open http://{args.host}:{args.port}/ in a browser on this same machine to authorize Tesla.")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
