#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from energy_controller import ChargerState, DeviceStatus, EnergyTelemetry, decide_energy_action, make_energy_config


def load_payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview an energy-controller decision from a JSON payload.")
    parser.add_argument("input", help="Path to preview JSON")
    args = parser.parse_args()

    payload = load_payload(Path(args.input))
    now = datetime.fromisoformat(payload.get("now") or datetime.now().astimezone().isoformat())
    config = make_energy_config(payload.get("config"), charger_cfg=payload.get("charger_cfg"), legacy_profile=payload.get("legacy_profile"))
    telemetry = EnergyTelemetry(
        solar_watts=payload["telemetry"].get("solar_watts"),
        house_consumption_watts=payload["telemetry"].get("house_consumption_watts"),
        powerwall_soc=payload["telemetry"].get("powerwall_soc"),
        grid_import_watts=payload["telemetry"].get("grid_import_watts"),
        grid_export_watts=payload["telemetry"].get("grid_export_watts"),
        timestamp=datetime.fromisoformat(payload["telemetry"]["timestamp"]) if payload["telemetry"].get("timestamp") else now,
    )
    devices = DeviceStatus(
        hvac_running=bool(payload["devices"].get("hvac_running")),
        hvac_changed_at=datetime.fromisoformat(payload["devices"]["hvac_changed_at"]) if payload["devices"].get("hvac_changed_at") else None,
    )
    charger = ChargerState(
        is_enabled=bool(payload["charger"].get("is_enabled")),
        is_charging=bool(payload["charger"].get("is_charging")),
        vehicle_connected=bool(payload["charger"].get("vehicle_connected")),
        night_session_active=bool(payload["charger"].get("night_session_active")),
        current_amps=payload["charger"].get("current_amps"),
        current_setpoint_amps=payload["charger"].get("current_setpoint_amps"),
        voltage=float(payload["charger"].get("voltage", config.ev_voltage)),
        ev_min_amps=int(payload["charger"].get("ev_min_amps", config.ev_min_amps)),
        solar_max_amps=int(payload["charger"].get("solar_max_amps", config.ev_solar_max_amps)),
        hard_max_amps=int(payload["charger"].get("hard_max_amps", config.ev_hard_max_amps)),
        emergency_charge_amps=int(payload["charger"].get("emergency_charge_amps", config.emergency_charge_amps)),
        last_charger_command_at=datetime.fromisoformat(payload["charger"]["last_charger_command_at"]) if payload["charger"].get("last_charger_command_at") else None,
        last_charger_command_type=payload["charger"].get("last_charger_command_type"),
    )
    decision = decide_energy_action(
        telemetry=telemetry,
        devices=devices,
        charger=charger,
        config=config,
        now=now,
        low_solar_stop_counter=int(payload.get("low_solar_stop_counter", 0)),
    )
    print(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
