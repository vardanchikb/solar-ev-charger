#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Tesla Fleet energy sync worker")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--once", action="store_true", help="Run one sync pass and exit")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    monitor = TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database"))
    if args.once:
        result = monitor.sync_once()
        print(result.get("error") or "sync_ok")
        return 0

    monitor.run_sync_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
