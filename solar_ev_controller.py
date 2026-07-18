#!/usr/bin/env python3
import argparse
from collections import deque
import json
import math
import os
import re
import subprocess
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import URLError
from urllib.request import urlopen

import tinytuya
from tinytuya import scanner as tinytuya_scanner
import yaml

from energy_controller import (
    ABSOLUTE_MAX_AMPS,
    ABSOLUTE_MIN_AMPS,
    ChargerState,
    DeviceStatus,
    EnergyTelemetry,
    decide_energy_action,
    default_energy_config,
    make_energy_config,
    start_emergency_mode,
    stop_emergency_mode,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.yaml"
DEFAULT_STATE_PATH = BASE_DIR / "automation_state.yaml"
DEFAULT_STATUS_CACHE_PATH = BASE_DIR / "charger_status_cache.json"
DEFAULT_REPORT_CACHE_PATH = BASE_DIR / "status_report_cache.json"
DEFAULT_DEBUG_TRACE_PATH = BASE_DIR / "energy_decision_trace.jsonl"
DEFAULT_EVENT_LOG_PATH = BASE_DIR / "charger_event_log.jsonl"
DEFAULT_RAW_TUYA_LOG_PATH = BASE_DIR / "tuya_raw_debug.jsonl"
DEFAULT_SECRET_DIR = BASE_DIR / ".secrets"
DEFAULT_DB_PASSWORD_FILE = DEFAULT_SECRET_DIR / "mysql_password"
FORCE_STOP_MAX_DURATION_MINUTES = 60
# How long after our own START command Tesla home-load charging inference and
# external-start (self-start) protection stay armed. Outside this window a
# house-load spike must not be mistaken for EV charging.
START_INFERENCE_WINDOW_SECONDS = 600
ENERGY_TRACE_RETENTION_SECONDS = 3 * 24 * 60 * 60
ENERGY_TRACE_PRUNE_INTERVAL_SECONDS = 6 * 60 * 60
ENERGY_TRACE_MAX_BYTES = 64 * 1024 * 1024
RAW_TUYA_LOG_MAX_BYTES = 32 * 1024 * 1024
_energy_trace_last_prune_monotonic: float | None = None

SEASONAL_PROFILE_NAMES = ("spring", "summer", "fall", "winter")
AUTOMATION_PROFILE_KEYS = {
    "automation_min_amps",
    "min_current_change_interval_seconds",
    "fault_reset_enabled",
    "fault_reset_cooldown_seconds",
    "fault_reset_off_seconds",
    "log_charger_events",
    "charger_event_log_path",
    "hvac_status_url",
    "hvac_timeout_seconds",
    "hvac_refresh_seconds",
    "allow_when_hvac_unavailable",
    "tesla_solar_control_enabled",
    "tesla_solar_max_sample_age_seconds",
    "tesla_solar_generation_cap_watts",
    "tesla_solar_reserve_watts",
    "tesla_solar_margin_boost_watts",
    "tesla_solar_margin_trim_watts",
    "tesla_solar_grid_import_stop_watts",
    "tesla_solar_cloudy_min_amps",
    "tesla_solar_powerwall_full_boost_pct",
    "tesla_solar_powerwall_reserve_pct",
    "energy_down_sustain_seconds",
    "auto_force_charge_now_mode",
    "charge_now_mode",
    "startup_grace_seconds",
    "startup_failure_cooldown_seconds",
    "log_fetch_details",
    "poll_seconds",
    "no_charge_start",
    "no_charge_end",
    "night_charge_start",
    "night_new_start_cutoff",
    "night_charge_amps",
    "night_completion_grace_seconds",
    "night_force_enable_without_connection",
    "emergency_amps",
    "energy_controller",
}


def parse_hhmm(value: str) -> int:
    hh, mm = value.split(":", maxsplit=1)
    return int(hh) * 60 + int(mm)


def current_local_time() -> datetime:
    return datetime.now().astimezone()


def read_secret_file(path: str | Path | None) -> str:
    if not path:
        return ""
    secret_path = Path(path)
    try:
        return secret_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def normalize_automation_config(raw_cfg: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(raw_cfg or {})
    raw_profiles = source.get("profiles")
    profiles_cfg = raw_profiles if isinstance(raw_profiles, dict) else {}

    base_profile = {
        key: deepcopy(value)
        for key, value in source.items()
        if key in AUTOMATION_PROFILE_KEYS
    }
    if not base_profile:
        for name in SEASONAL_PROFILE_NAMES:
            profile_cfg = profiles_cfg.get(name)
            if isinstance(profile_cfg, dict):
                base_profile = {
                    key: deepcopy(value)
                    for key, value in profile_cfg.items()
                    if key in AUTOMATION_PROFILE_KEYS
                }
                if base_profile:
                    break

    profiles: dict[str, dict[str, Any]] = {}
    for name in SEASONAL_PROFILE_NAMES:
        merged = deepcopy(base_profile)
        profile_cfg = profiles_cfg.get(name)
        if isinstance(profile_cfg, dict):
            for key, value in profile_cfg.items():
                if key in AUTOMATION_PROFILE_KEYS:
                    merged[key] = deepcopy(value)
        profiles[name] = merged

    active_profile = str(source.get("active_profile") or "spring").strip().lower()
    if active_profile not in SEASONAL_PROFILE_NAMES:
        active_profile = "spring"

    return {
        "active_profile": active_profile,
        "profiles": profiles,
    }


def active_automation_config(raw_cfg: dict[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_automation_config(raw_cfg)
    active_profile = normalized["active_profile"]
    active_cfg = deepcopy(normalized["profiles"].get(active_profile) or {})
    active_cfg["active_profile"] = active_profile
    active_cfg["profiles"] = normalized["profiles"]
    return active_cfg


def default_automation_state() -> dict[str, Any]:
    return {
        "emergency": {
            "active": False,
            "started_at": None,
            "seen_charging": False,
            "expires_at": None,
            "requested_amps": None,
            "duration_minutes": None,
            "target_energy_kwh": None,
            "delivered_energy_wh": 0.0,
            "energy_last_sample_at": None,
            "target_completed_at": None,
            "target_completed_kwh": None,
        },
        "night": {
            "session_key": None,
            "seen_charging": False,
            "last_seen_charging_at": None,
            "completed_session_key": None,
            "start_blocked_until_disconnect": False,
        },
        "startup": {
            "active": False,
            "started_at": None,
            "phase": None,
            "target_amps": None,
            "blocked_until": None,
            "blocked_phase": None,
            "blocked_reason": None,
        },
        "control": {
            "last_current_change_at": None,
            "last_current_target_amps": None,
            "last_fault_reset_at": None,
            "last_fault_reset_reason": None,
            "last_charger_command_at": None,
            "last_charger_command_type": None,
            "last_charger_command_detail": None,
            "last_start_command_at": None,
            "last_start_command_detail": None,
            "last_start_setpoint_amps": None,
            "last_stop_command_at": None,
            "last_stop_command_detail": None,
            "low_solar_stop_counter": 0,
            "config_last_applied_at": None,
            "setpoint_history": [],
            "last_telemetry_timestamp": None,
            "last_non_ev_watts": None,
            "energy_down_pending_target": None,
            "energy_down_pending_since": None,
            "consecutive_charger_fetch_errors": 0,
            "charger_fetch_error_first_seen_at": None,
            "charger_fetch_error_last_seen_at": None,
            "charger_fetch_error_last_message": None,
            "force_stop_requested_at": None,
            "force_stop_reason": None,
            "force_stop_hold_until_disconnect": False,
            "force_stop_expires_at": None,
            "force_stop_cleared_request_at": None,
        },
        "solar_hysteresis": {
            "pending_amps": None,
            "pending_since": None,
        },
        "device_status": {
            "hvac_running": False,
            "hvac_changed_at": None,
            "hvac_heating": False,
            "hvac_heating_changed_at": None,
        },
        "decision": {
            "last_reason": None,
            "last_mode": None,
            "last_status": None,
            "last_desired_enabled": None,
            "last_desired_amps": None,
            "last_decision_at": None,
        },
        "session": {
            "active": False,
            "db_id": None,
            "started_at": None,
            "last_seen_at": None,
            "energy_wh": 0.0,
            "max_power_w": 0.0,
            "max_actual_amps": 0.0,
            "start_phase": None,
            "start_reason": None,
            "target_amps": None,
            "start_setpoint_amps": None,
            "max_setpoint_amps": None,
            "db_error": None,
        },
    }


def load_automation_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return default_automation_state()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return default_automation_state()

    merged = default_automation_state()
    for section in (
        "emergency",
        "night",
        "startup",
        "control",
        "solar_hysteresis",
        "device_status",
        "decision",
        "session",
    ):
        if isinstance(data.get(section), dict):
            merged[section].update(data[section])
    return merged


def save_automation_state(data: dict[str, Any], path: Path = DEFAULT_STATE_PATH) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def clear_force_stop_state(state: dict[str, Any]) -> None:
    control = state.setdefault("control", {})
    requested_at = control.get("force_stop_requested_at")
    if requested_at:
        control["force_stop_cleared_request_at"] = requested_at
    control["force_stop_requested_at"] = None
    control["force_stop_reason"] = None
    control["force_stop_hold_until_disconnect"] = False
    control["force_stop_expires_at"] = None


def request_temporary_force_stop(
    state: dict[str, Any],
    now: datetime,
    reason: str,
    *,
    hold_until_disconnect: bool = True,
) -> None:
    control = state.setdefault("control", {})
    control["force_stop_requested_at"] = now.isoformat()
    control["force_stop_reason"] = reason
    control["force_stop_hold_until_disconnect"] = bool(hold_until_disconnect)
    control["force_stop_expires_at"] = (
        now + timedelta(minutes=FORCE_STOP_MAX_DURATION_MINUTES)
    ).isoformat()
    control["energy_down_pending_target"] = None
    control["energy_down_pending_since"] = None


def save_config(data: dict[str, Any], path: Path = DEFAULT_CONFIG_PATH) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def save_status_report_cache(data: dict[str, Any], path: Path = DEFAULT_REPORT_CACHE_PATH) -> None:
    try:
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except OSError:
        return


def load_status_report_cache(path: Path = DEFAULT_REPORT_CACHE_PATH) -> dict[str, Any] | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return cached if isinstance(cached, dict) else None


def classify_charger_fetch_error(error_text: str) -> dict[str, Any]:
    normalized = str(error_text or "").lower()

    if "err': '914'" in normalized or '"err": "914"' in normalized or "check device key or version" in normalized:
        return {
            "severity": "error",
            "code": "charger_tuya_auth_error",
            "live_error": "Charger connection error: Tuya key or version mismatch.",
            "message": (
                "The charger rejected local Tuya control with a key/version mismatch. "
                "Check the configured local key, Tuya protocol version, or device binding."
            ),
            "kind": "tuya_auth",
        }

    network_tokens = (
        "network error",
        "unable to connect",
        "timed out",
        "timeout",
        "host unreachable",
        "no route to host",
        "connection refused",
        "connection reset",
        "broken pipe",
        "rediscovery_miss",
    )
    if any(token in normalized for token in network_tokens):
        return {
            "severity": "error",
            "code": "charger_network_error",
            "live_error": "Charger connection error: charger unreachable on Wi-Fi/LAN.",
            "message": (
                "The charger is not reachable on the local network. If Wi-Fi just dropped, wait for it to reconnect. "
                "If it stays offline, restart the charger device and check the LAN connection."
            ),
            "kind": "network",
        }

    return {
        "severity": "error",
        "code": "charger_status_fetch_error",
        "live_error": "Charger status read failed.",
        "message": str(error_text or "Unknown charger status error."),
        "kind": "unknown",
    }


def build_error_status_report(
    error_text: str,
    *,
    now: datetime | None = None,
    cached: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = now or current_local_time()
    classification = classify_charger_fetch_error(error_text)
    report = deepcopy(cached) if isinstance(cached, dict) else {}
    stale_source_timestamp = report.get("timestamp")
    alerts = report.get("alerts")
    if not isinstance(alerts, list):
        alerts = []

    filtered_alerts = [
        alert
        for alert in alerts
        if isinstance(alert, dict)
        and alert.get("code")
        not in {
            "charger_network_error",
            "charger_tuya_auth_error",
            "charger_status_fetch_error",
        }
    ]
    filtered_alerts.insert(
        0,
        {
            "severity": classification["severity"],
            "code": classification["code"],
            "message": classification["message"],
        },
    )
    cached_charger = report.get("charger") if isinstance(report.get("charger"), dict) else {}
    if (
        cached_charger.get("enabled")
        or cached_charger.get("actively_charging")
        or float(cached_charger.get("power_w") or 0.0) >= 100.0
    ):
        filtered_alerts.insert(
            0,
            {
                "severity": "error",
                "code": "charger_control_unavailable_while_active",
                "message": (
                    "Automation cannot currently verify or control the charger. "
                    "Displayed charger values are stale; check the charger directly if it may still be charging."
                ),
            },
        )

    report["timestamp"] = now.isoformat()
    report["source"] = "cache"
    report["stale"] = True
    report["control_unavailable"] = True
    report["stale_source_timestamp"] = stale_source_timestamp
    report["live_error"] = classification["live_error"]
    report["alerts"] = filtered_alerts
    report.setdefault("charger", {})
    report.setdefault("charger_dps", {})
    report.setdefault("hvac", {})
    report.setdefault("automation", {})
    report.setdefault("energy_status", {})
    report.setdefault("energy_config", {})
    report.setdefault("database", {})

    fetch = report.get("fetch")
    if not isinstance(fetch, dict):
        fetch = {}
    fetch["error"] = str(error_text or "")
    fetch["error_kind"] = classification["kind"]
    fetch["failed_at"] = now.isoformat()
    report["fetch"] = fetch

    automation = report.get("automation")
    if not isinstance(automation, dict):
        automation = {}
    automation["phase"] = "control_error"
    automation["base_phase"] = "control_error"
    automation["action"] = "hold"
    automation["reason"] = classification["live_error"]
    automation["target_mode"] = None
    automation["target_amps"] = None
    automation["current_decision_stale"] = True
    report["automation"] = automation

    energy_status = report.get("energy_status")
    if not isinstance(energy_status, dict):
        energy_status = {}
    energy_status.update(
        {
            "mode": "CONTROL_UNAVAILABLE",
            "reason": classification["live_error"],
            "action_status": "CHARGER_CONTROL_UNAVAILABLE",
            "desired_charger_enabled": None,
            "desired_amps": None,
            "command_allowed_now": False,
            "cooldown_remaining_seconds": None,
            "warning": (
                "Charger status/control failed. Any displayed charger state is from the last good cache."
            ),
        }
    )
    report["energy_status"] = energy_status
    return report


def build_energy_debug_trace_entry(report: dict[str, Any]) -> dict[str, Any]:
    energy = report.get("energy_status") or {}
    charger = report.get("charger") or {}
    automation = report.get("automation") or {}
    telemetry = energy.get("telemetry") or {}
    command = energy.get("performed_command") or {}
    return {
        "timestamp": report.get("timestamp"),
        "mode": energy.get("mode") or automation.get("phase"),
        "reason": energy.get("reason") or automation.get("reason"),
        "action_status": energy.get("action_status"),
        "new_vehicle_connection": energy.get("new_vehicle_connection"),
        "external_low_solar_stop": energy.get("external_low_solar_stop"),
        "ev_active_since": energy.get("ev_active_since"),
        "desired_charger_enabled": energy.get("desired_charger_enabled"),
        "desired_amps": energy.get("desired_amps"),
        "actual_setpoint_amps": charger.get("setpoint_amps"),
        "actual_power_w": charger.get("power_w"),
        "vehicle_connected": charger.get("vehicle_connected"),
        "actively_charging": charger.get("actively_charging"),
        "command_type": command.get("type"),
        "command_detail": command.get("detail"),
        "cooldown_remaining_seconds": energy.get("cooldown_remaining_seconds"),
        "computed_available_watts": energy.get("computed_available_watts"),
        "adjusted_consumption_watts": energy.get("adjusted_consumption_watts"),
        "non_ev_consumption_watts": energy.get("non_ev_consumption_watts"),
        "estimated_current_ev_watts": energy.get("estimated_current_ev_watts"),
        "next_low_solar_stop_counter": energy.get("next_low_solar_stop_counter"),
        "solar_watts": telemetry.get("solar_watts"),
        "house_consumption_watts": telemetry.get("house_consumption_watts"),
        "powerwall_soc": telemetry.get("powerwall_soc"),
        "telemetry_timestamp": telemetry.get("timestamp"),
        "hvac_error": report.get("hvac_error"),
    }


def _rotate_jsonl_log(path: Path, cutoff: datetime, max_bytes: int) -> None:
    """Rotate an append-only JSONL log instead of rewriting it in place.

    When the active file's oldest entry ages past the retention cutoff (or the
    file exceeds max_bytes) it is renamed to <name>.1 and appends start a fresh
    file; a previous .1 that has fully aged out is deleted first. Entries thus
    survive between one and two retention windows, at a tiny fraction of the
    SD-card writes the old full-file rewrite cost.
    """
    rotated_path = path.with_name(path.name + ".1")
    try:
        rotated_mtime = datetime.fromtimestamp(rotated_path.stat().st_mtime).astimezone()
        if rotated_mtime < cutoff:
            rotated_path.unlink()
    except OSError:
        pass
    try:
        with path.open("rb") as f:
            first_line = f.readline()
        size = path.stat().st_size
    except OSError:
        return
    rotate = size > max_bytes
    if not rotate and first_line:
        try:
            first_ts = datetime.fromisoformat(str(json.loads(first_line).get("timestamp")))
            rotate = first_ts < cutoff
        except (TypeError, ValueError, json.JSONDecodeError):
            rotate = False
    if not rotate:
        return
    try:
        os.replace(path, rotated_path)
    except OSError:
        pass


def _prune_energy_debug_trace(path: Path, now: datetime) -> None:
    global _energy_trace_last_prune_monotonic
    monotonic_now = time.monotonic()
    if (
        _energy_trace_last_prune_monotonic is not None
        and monotonic_now - _energy_trace_last_prune_monotonic < ENERGY_TRACE_PRUNE_INTERVAL_SECONDS
    ):
        return
    _energy_trace_last_prune_monotonic = monotonic_now
    cutoff = now - timedelta(seconds=ENERGY_TRACE_RETENTION_SECONDS)
    _rotate_jsonl_log(path, cutoff, ENERGY_TRACE_MAX_BYTES)


def append_energy_debug_trace(report: dict[str, Any], path: Path = DEFAULT_DEBUG_TRACE_PATH) -> None:
    entry = build_energy_debug_trace_entry(report)
    try:
        _prune_energy_debug_trace(path, current_local_time())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        return


def load_energy_debug_trace(
    path: Path = DEFAULT_DEBUG_TRACE_PATH,
    *,
    limit: int = 720,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    entries: deque[dict[str, Any]] = deque()

    def reversed_lines() -> Iterator[bytes]:
        chunk_size = 64 * 1024
        # Newest entries live in the active file; the rotated .1 file (if any)
        # holds the older window, so it is walked after the active file.
        for candidate in (path, path.with_name(path.name + ".1")):
            try:
                f = candidate.open("rb")
            except OSError:
                continue
            with f:
                f.seek(0, os.SEEK_END)
                position = f.tell()
                remainder = b""
                while position > 0:
                    read_size = min(chunk_size, position)
                    position -= read_size
                    f.seek(position)
                    lines = (f.read(read_size) + remainder).split(b"\n")
                    remainder = lines[0]
                    for line in reversed(lines[1:]):
                        if line:
                            yield line
                if remainder:
                    yield remainder

    try:
        for line in reversed_lines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if since is not None or until is not None:
                timestamp = entry.get("timestamp")
                if not isinstance(timestamp, str):
                    continue
                try:
                    entry_time = datetime.fromisoformat(timestamp)
                except ValueError:
                    continue
                if until is not None and entry_time > until:
                    continue
                # Trace rows are append-only and chronological, so older rows
                # cannot re-enter the selected window after crossing its start.
                if since is not None and entry_time < since:
                    break
            entries.appendleft(entry)
            if limit > 0 and len(entries) >= limit:
                break
    except OSError:
        return []

    return list(entries)


def discover_device_ip(device_id: str, scan_seconds: int = 10) -> str | None:
    if not device_id:
        return None

    results = tinytuya_scanner.devices(
        verbose=False,
        scantime=max(1, int(scan_seconds)),
        color=False,
        poll=False,
        byID=True,
        wantids=[device_id],
        maxdevices=1,
        assume_yes=True,
    )
    if not isinstance(results, dict):
        return None

    device = results.get(device_id)
    if not isinstance(device, dict):
        return None

    ip = device.get("ip")
    return str(ip) if ip else None


def activate_emergency_override(
    path: Path = DEFAULT_STATE_PATH,
    now: datetime | None = None,
    *,
    target_energy_kwh: float | None = None,
) -> dict[str, Any]:
    now = now or current_local_time()
    state = load_automation_state(path)
    emergency = state.setdefault("emergency", {})
    emergency.update(
        {
            "active": True,
            "started_at": now.isoformat(),
            "seen_charging": False,
            "expires_at": None,
            "requested_amps": None,
            "duration_minutes": None,
            "target_energy_kwh": float(target_energy_kwh) if target_energy_kwh else None,
            "delivered_energy_wh": 0.0,
            "energy_last_sample_at": None,
            "target_completed_at": None,
            "target_completed_kwh": None,
        }
    )
    clear_force_stop_state(state)
    save_automation_state(state, path)
    return state


def deactivate_emergency_override(path: Path = DEFAULT_STATE_PATH, now: datetime | None = None) -> dict[str, Any]:
    now = now or current_local_time()
    state = load_automation_state(path)
    emergency = state.setdefault("emergency", {})
    emergency.update(
        {
            "active": False,
            "started_at": None,
            "seen_charging": False,
            "expires_at": None,
            "requested_amps": None,
            "duration_minutes": None,
            "target_energy_kwh": None,
            "delivered_energy_wh": 0.0,
            "energy_last_sample_at": None,
        }
    )
    request_temporary_force_stop(state, now, "emergency_stop")
    save_automation_state(state, path)
    return state


@dataclass
class HVACStatus:
    hvac_status: str
    is_running: bool
    is_heating: bool
    is_cooling: bool
    thermostat_mode: str | None
    observed_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hvac_status": self.hvac_status,
            "is_running": self.is_running,
            "is_heating": self.is_heating,
            "is_cooling": self.is_cooling,
            "thermostat_mode": self.thermostat_mode,
            "observed_at": self.observed_at,
        }


class HVACStatusClient:
    def __init__(self, cfg: dict[str, Any]):
        self.url = str(cfg.get("hvac_status_url", "http://127.0.0.1:8789/api/hvac-status"))
        self.timeout_seconds = float(cfg.get("hvac_timeout_seconds", 5))

    def read_status(self) -> HVACStatus:
        with urlopen(self.url, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return HVACStatus(
            hvac_status=str(payload.get("hvac_status") or "unknown"),
            is_running=bool(payload.get("is_running")),
            is_heating=bool(payload.get("is_heating")),
            is_cooling=bool(payload.get("is_cooling")),
            thermostat_mode=payload.get("thermostat_mode"),
            observed_at=payload.get("observed_at"),
        )


@dataclass
class TeslaEnergySnapshot:
    sampled_at: datetime
    solar_generation_w: float | None
    home_consumption_w: float | None
    powerwall_level_pct: float | None
    grid_import_w: float | None
    grid_export_w: float | None

    def age_seconds(self, now: datetime) -> int:
        sampled_at = self.sampled_at
        if sampled_at.tzinfo is None:
            sampled_at = sampled_at.replace(tzinfo=timezone.utc)
        return max(0, int((now.astimezone(timezone.utc) - sampled_at.astimezone(timezone.utc)).total_seconds()))

    def to_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "sampled_at": self.sampled_at.isoformat(),
            "age_seconds": self.age_seconds(now),
            "solar_generation_w": self.solar_generation_w,
            "home_consumption_w": self.home_consumption_w,
            "powerwall_level_pct": self.powerwall_level_pct,
            "grid_import_w": self.grid_import_w,
            "grid_export_w": self.grid_export_w,
        }


def estimate_actual_amps(power_w: float, voltage: float, phases: int) -> float | None:
    if power_w <= 0 or voltage <= 0 or phases <= 0:
        return None
    return round(power_w / (voltage * phases), 2)


def decode_aimiler_dp6(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    decoded: dict[str, Any] = {
        "raw": value,
        "encoding": "base64",
        "schema": "aimiler_observed_voltage_current_power_v1",
    }
    try:
        import base64
        import binascii

        payload = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        decoded["valid"] = False
        decoded["error"] = "invalid_base64"
        return decoded
    decoded["raw_hex"] = payload.hex()
    decoded["byte_length"] = len(payload)
    if len(payload) != 8:
        decoded["valid"] = False
        decoded["error"] = "unexpected_length"
        return decoded

    # Observed on Aimiler UA_7KW fw B5_V1.0.0:
    # bytes 0-1 = volts * 10, bytes 2-4 = milliamps, bytes 5-7 = watts.
    voltage_tenths = int.from_bytes(payload[0:2], "big")
    current_ma = int.from_bytes(payload[2:5], "big")
    power_w = int.from_bytes(payload[5:8], "big")
    decoded.update(
        {
            "valid": True,
            "voltage_v": round(voltage_tenths / 10.0, 1),
            "current_a": round(current_ma / 1000.0, 3),
            "power_w": float(power_w),
            "confidence": "observed",
        }
    )
    return decoded


def build_status_alerts(decision: dict[str, Any]) -> list[dict[str, str]]:
    charger = decision.get("charger") or {}
    fetch = decision.get("fetch") or {}
    alerts: list[dict[str, str]] = []

    if fetch.get("degraded_status"):
        missing = fetch.get("missing_required_dps") or []
        missing_note = f" Missing DPs: {', '.join(str(key) for key in missing)}." if missing else ""
        alerts.append(
            {
                "severity": "warning",
                "code": "degraded_charger_status",
                "message": (
                    "Charger returned a live but incomplete Tuya status payload; automation is using the remaining DPs "
                    "plus Tesla load history where possible."
                    f"{missing_note}"
                ),
            }
        )

    if charger.get("status_quality") == "inferred_charging_from_tesla":
        alerts.append(
            {
                "severity": "warning",
                "code": "charger_state_inferred_from_tesla",
                "message": (
                    "Charger DPs reported idle, but Tesla home load after the last START matches EV charging, "
                    "so automation is treating the EV as active."
                ),
            }
        )

    if charger.get("status_quality") == "post_stop_stale_active_suppressed":
        alerts.append(
            {
                "severity": "info",
                "code": "post_stop_stale_charger_power_suppressed",
                "message": (
                    "Charger DPs still reported active after STOP, but Tesla home load no longer showed EV load, "
                    "so automation treated that charger reading as stale."
                ),
            }
        )

    if fetch.get("used_cached_merge"):
        # Tuya devices routinely send only the changed DPs (e.g. just DP 18 after a relay
        # toggle), so a cached merge with a fresh cache is normal and not worth alerting on.
        # Only warn when the underlying cache is old enough that stale values could matter.
        cache_age = fetch.get("cache_age_seconds")
        cache_is_stale = cache_age is None or cache_age > 60
        if cache_is_stale:
            partial = fetch.get("partial_dps_keys") or []
            keys_note = f" Partial keys: {', '.join(str(key) for key in partial)}." if partial else ""
            alerts.append(
                {
                    "severity": "warning",
                    "code": "incomplete_status_payload",
                    "message": (
                        "Charger returned a partial Tuya status payload, so some displayed values may be stale."
                        f"{keys_note}"
                    ),
                }
            )

    state_text = f"{charger.get('state', '')} {charger.get('pilot_state', '')}".lower()
    if any(token in state_text for token in ("fault", "error", "alarm", "alert")):
        alerts.append(
            {
                "severity": "error",
                "code": "charger_fault_state",
                "message": (
                    "The charger reported a fault-like state string. Check the Aimiler app for the exact alert"
                    " and clear the fault before retrying."
                ),
            }
        )

    if (
        decision.get("action") == "enable"
        and not charger.get("enabled")
        and not charger.get("actively_charging")
    ):
        message = "Controller requested charging, but the charger still reports output OFF."
        if charger.get("insert_sensed") and not charger.get("vehicle_connected"):
            message += " The plug looks inserted, but the vehicle is not requesting power."
        else:
            message += " This can happen after a charger fault or interlock."
        alerts.append(
            {
                "severity": "warning" if decision.get("startup_active") else "error",
                "code": "enable_requested_but_off",
                "message": message,
            }
        )
    elif (
        charger.get("insert_sensed")
        and not charger.get("vehicle_connected")
        and not charger.get("actively_charging")
    ):
        alerts.append(
            {
                "severity": "info",
                "code": "vehicle_not_requesting_power",
                "message": "The cable looks inserted, but the vehicle is not currently requesting charge.",
            }
        )

    return alerts


class MySQLSessionStore:
    def __init__(self, cfg: dict[str, Any]):
        db_cfg = cfg.get("database") or {}
        self.enabled = bool(db_cfg.get("enabled", False))
        self.bootstrap = bool(db_cfg.get("bootstrap", True))
        self.socket = str(db_cfg.get("socket", "/run/mysqld/mysqld.sock")).strip()
        self.host = str(db_cfg.get("host", "127.0.0.1")).strip()
        self.port = int(db_cfg.get("port", 3306))
        self.user = str(db_cfg.get("user", "")).strip()
        self.password_file = str(db_cfg.get("password_file", "")).strip()
        self.password = read_secret_file(self.password_file) or str(db_cfg.get("password", ""))
        self.database = self._identifier(str(db_cfg.get("database", "carcharger")))
        self.table = self._identifier(str(db_cfg.get("table", "charging_sessions")))
        self.telemetry_table = self._identifier(
            str(db_cfg.get("telemetry_table", "charger_telemetry_samples"))
        )
        self.last_error: str | None = None
        self._ready = False

    @staticmethod
    def _identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise ValueError(f"Invalid SQL identifier: {value}")
        return value

    @staticmethod
    def _sql_text(value: Any) -> str:
        if value is None:
            return "NULL"
        # Quote-doubling ('') is honored in every sql_mode including
        # NO_BACKSLASH_ESCAPES, so a quote in the data can never terminate the
        # string literal regardless of server configuration.
        escaped = str(value).replace("\x00", "").replace("\\", "\\\\").replace("'", "''")
        return f"'{escaped}'"

    @staticmethod
    def _sql_number(value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return str(value)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid SQL number: {value!r}")
        if not math.isfinite(number):
            return "NULL"
        return repr(number)

    @classmethod
    def _sql_json(cls, value: Any) -> str:
        if value is None:
            return "NULL"
        return cls._sql_text(json.dumps(value, separators=(",", ":"), sort_keys=True))

    def _mysql_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.password:
            env["MYSQL_PWD"] = self.password
        return env

    def _base_cmd(self) -> list[str]:
        cmd = ["mysql", "--batch", "--raw", "--skip-column-names"]
        if self.socket:
            cmd.append(f"--socket={self.socket}")
        else:
            cmd.extend(["-h", self.host, "-P", str(self.port)])
        if self.user:
            cmd.extend(["-u", self.user])
        return cmd

    def _run_sql(self, sql: str) -> str:
        proc = subprocess.run(
            self._base_cmd(),
            input=sql,
            text=True,
            capture_output=True,
            env=self._mysql_env(),
            check=False,
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "mysql command failed").strip()
            raise RuntimeError(message)
        return proc.stdout.strip()

    def ping(self) -> tuple[bool, str]:
        """Cheap connectivity/credential check without touching any tables."""
        if not self.enabled:
            return False, "database disabled"
        try:
            self._run_sql("SELECT 1;")
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def ensure_ready(self) -> bool:
        if not self.enabled:
            return False
        if self._ready:
            return True

        statements = []
        if self.bootstrap:
            statements.append(
                f"CREATE DATABASE IF NOT EXISTS `{self.database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            )
        statements.append(f"USE `{self.database}`;")
        statements.append(
            f"""
CREATE TABLE IF NOT EXISTS `{self.table}` (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  started_at DATETIME NOT NULL,
  ended_at DATETIME NULL,
  timezone_name VARCHAR(64) NOT NULL,
  start_phase VARCHAR(96) NULL,
  end_phase VARCHAR(96) NULL,
  start_reason VARCHAR(255) NULL,
  end_reason VARCHAR(255) NULL,
  target_amps INT NULL,
  start_setpoint_amps INT NULL,
  end_setpoint_amps INT NULL,
  max_setpoint_amps INT NULL,
  max_actual_amps DECIMAL(8,2) NULL,
  max_power_w DECIMAL(12,2) NULL,
  end_power_w DECIMAL(12,2) NULL,
  energy_wh DECIMAL(14,3) NOT NULL DEFAULT 0,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_started_at (started_at),
  KEY idx_status (status)
);
"""
        )
        statements.append(
            f"""
CREATE TABLE IF NOT EXISTS `{self.telemetry_table}` (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sampled_at DATETIME NOT NULL,
  timezone_name VARCHAR(64) NOT NULL,
  charger_state VARCHAR(64) NULL,
  pilot_state VARCHAR(64) NULL,
  enabled TINYINT(1) NULL,
  actively_charging TINYINT(1) NULL,
  status_quality VARCHAR(64) NULL,
  setpoint_amps INT NULL,
  dp9_power_w DECIMAL(12,2) NULL,
  dp6_raw VARCHAR(128) NULL,
  dp6_decoded_json LONGTEXT NULL,
  dp6_voltage_v DECIMAL(8,2) NULL,
  dp6_current_a DECIMAL(8,3) NULL,
  dp6_power_w DECIMAL(12,2) NULL,
  raw_dps_json LONGTEXT NULL,
  automation_phase VARCHAR(96) NULL,
  automation_reason VARCHAR(255) NULL,
  command_type VARCHAR(32) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_sampled_at (sampled_at),
  KEY idx_dp6_raw (dp6_raw),
  KEY idx_charger_state (charger_state)
);
"""
        )
        statements.append(
            f"""
ALTER TABLE `{self.table}`
  MODIFY COLUMN start_phase VARCHAR(96) NULL,
  MODIFY COLUMN end_phase VARCHAR(96) NULL,
  MODIFY COLUMN start_reason VARCHAR(255) NULL,
  MODIFY COLUMN end_reason VARCHAR(255) NULL;
"""
        )

        try:
            self._run_sql("\n".join(statements))
            self._ready = True
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def start_session(self, session: dict[str, Any], timezone_name: str) -> int | None:
        if not self.ensure_ready():
            return None

        started_at = datetime.fromisoformat(str(session["started_at"]))
        sql = f"""
USE `{self.database}`;
INSERT INTO `{self.table}` (
  started_at,
  timezone_name,
  start_phase,
  start_reason,
  target_amps,
  start_setpoint_amps,
  max_setpoint_amps,
  max_actual_amps,
  max_power_w,
  energy_wh,
  status
) VALUES (
  {self._sql_text(started_at.strftime("%Y-%m-%d %H:%M:%S"))},
  {self._sql_text(timezone_name)},
  {self._sql_text(session.get("start_phase"))},
  {self._sql_text(session.get("start_reason"))},
  {self._sql_number(session.get("target_amps"))},
  {self._sql_number(session.get("start_setpoint_amps"))},
  {self._sql_number(session.get("max_setpoint_amps"))},
  {self._sql_number(session.get("max_actual_amps"))},
  {self._sql_number(session.get("max_power_w"))},
  {self._sql_number(round(float(session.get("energy_wh", 0.0)), 3))},
  'active'
);
SELECT LAST_INSERT_ID();
"""
        try:
            output = self._run_sql(sql)
            self.last_error = None
            return int(output.splitlines()[-1])
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def record_charger_telemetry(
        self,
        charger: dict[str, Any],
        decision: dict[str, Any],
        sampled_at: datetime,
    ) -> bool:
        if not self.ensure_ready():
            return False

        raw_dps = charger.get("raw_dps") or {}
        dp6_raw = raw_dps.get("6")
        dp6_decoded = charger.get("dp6_telemetry") or decode_aimiler_dp6(dp6_raw)
        performed_command = decision.get("performed_command") or {}
        sql = f"""
USE `{self.database}`;
INSERT INTO `{self.telemetry_table}` (
  sampled_at,
  timezone_name,
  charger_state,
  pilot_state,
  enabled,
  actively_charging,
  status_quality,
  setpoint_amps,
  dp9_power_w,
  dp6_raw,
  dp6_decoded_json,
  dp6_voltage_v,
  dp6_current_a,
  dp6_power_w,
  raw_dps_json,
  automation_phase,
  automation_reason,
  command_type
) VALUES (
  {self._sql_text(sampled_at.strftime("%Y-%m-%d %H:%M:%S"))},
  {self._sql_text(sampled_at.tzname() or "local")},
  {self._sql_text(charger.get("state"))},
  {self._sql_text(charger.get("pilot_state"))},
  {self._sql_number(1 if charger.get("enabled") else 0)},
  {self._sql_number(1 if charger.get("actively_charging") else 0)},
  {self._sql_text(charger.get("status_quality"))},
  {self._sql_number(charger.get("setpoint_amps"))},
  {self._sql_number(charger.get("power_w"))},
  {self._sql_text(dp6_raw)},
  {self._sql_json(dp6_decoded)},
  {self._sql_number((dp6_decoded or {}).get("voltage_v"))},
  {self._sql_number((dp6_decoded or {}).get("current_a"))},
  {self._sql_number((dp6_decoded or {}).get("power_w"))},
  {self._sql_json(raw_dps)},
  {self._sql_text(decision.get("base_phase") or decision.get("phase"))},
  {self._sql_text(decision.get("reason"))},
  {self._sql_text(performed_command.get("type"))}
);
"""
        try:
            self._run_sql(sql)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def update_session(self, session: dict[str, Any], charger: dict[str, Any]) -> bool:
        if not self.ensure_ready():
            return False
        if not session.get("db_id"):
            return False

        last_seen_at = datetime.fromisoformat(str(session["last_seen_at"]))
        sql = f"""
USE `{self.database}`;
UPDATE `{self.table}`
SET
  end_setpoint_amps = {self._sql_number(charger.get("setpoint_amps"))},
  max_setpoint_amps = GREATEST(COALESCE(max_setpoint_amps, 0), {self._sql_number(charger.get("setpoint_amps"))}),
  max_actual_amps = GREATEST(COALESCE(max_actual_amps, 0), {self._sql_number(session.get("max_actual_amps"))}),
  max_power_w = GREATEST(COALESCE(max_power_w, 0), {self._sql_number(session.get("max_power_w"))}),
  end_power_w = {self._sql_number(charger.get("power_w"))},
  energy_wh = {self._sql_number(round(float(session.get("energy_wh", 0.0)), 3))},
  ended_at = {self._sql_text(last_seen_at.strftime("%Y-%m-%d %H:%M:%S"))}
WHERE id = {int(session["db_id"])};
"""
        try:
            self._run_sql(sql)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def finish_session(
        self,
        session: dict[str, Any],
        charger: dict[str, Any],
        decision: dict[str, Any],
        ended_at: datetime,
    ) -> bool:
        if not self.ensure_ready():
            return False
        if not session.get("db_id"):
            return False

        sql = f"""
USE `{self.database}`;
UPDATE `{self.table}`
SET
  ended_at = {self._sql_text(ended_at.strftime("%Y-%m-%d %H:%M:%S"))},
  end_phase = {self._sql_text(decision.get("base_phase") or decision.get("phase"))},
  end_reason = {self._sql_text(decision.get("reason"))},
  end_setpoint_amps = {self._sql_number(charger.get("setpoint_amps"))},
  max_setpoint_amps = GREATEST(COALESCE(max_setpoint_amps, 0), {self._sql_number(session.get("max_setpoint_amps"))}),
  max_actual_amps = GREATEST(COALESCE(max_actual_amps, 0), {self._sql_number(session.get("max_actual_amps"))}),
  max_power_w = GREATEST(COALESCE(max_power_w, 0), {self._sql_number(session.get("max_power_w"))}),
  end_power_w = {self._sql_number(charger.get("power_w"))},
  energy_wh = {self._sql_number(round(float(session.get("energy_wh", 0.0)), 3))},
  status = 'completed'
WHERE id = {int(session["db_id"])};
"""
        try:
            self._run_sql(sql)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def recent_sessions(self, limit: int = 30) -> list[dict[str, Any]]:
        if not self.ensure_ready():
            return []
        limit = max(1, min(int(limit), 200))
        sql = f"""
USE `{self.database}`;
SELECT
  id,
  DATE_FORMAT(started_at, '%Y-%m-%d %H:%i:%s'),
  COALESCE(DATE_FORMAT(ended_at, '%Y-%m-%d %H:%i:%s'), ''),
  timezone_name,
  COALESCE(start_phase, ''),
  COALESCE(end_phase, ''),
  COALESCE(start_reason, ''),
  COALESCE(end_reason, ''),
  COALESCE(target_amps, 0),
  COALESCE(start_setpoint_amps, 0),
  COALESCE(end_setpoint_amps, 0),
  COALESCE(max_setpoint_amps, 0),
  COALESCE(max_actual_amps, 0),
  COALESCE(max_power_w, 0),
  COALESCE(end_power_w, 0),
  COALESCE(energy_wh, 0),
  status
FROM `{self.table}`
ORDER BY id DESC
LIMIT {limit};
"""
        try:
            output = self._run_sql(sql)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            return []

        rows = []
        for line in output.splitlines():
            if not line.strip():
                continue
            cols = line.split("\t")
            rows.append(
                {
                    "id": int(cols[0]),
                    "started_at": cols[1],
                    "ended_at": cols[2] or None,
                    "timezone_name": cols[3],
                    "start_phase": cols[4] or None,
                    "end_phase": cols[5] or None,
                    "start_reason": cols[6] or None,
                    "end_reason": cols[7] or None,
                    "target_amps": int(cols[8]),
                    "start_setpoint_amps": int(cols[9]),
                    "end_setpoint_amps": int(cols[10]),
                    "max_setpoint_amps": int(cols[11]),
                    "max_actual_amps": float(cols[12]),
                    "max_power_w": float(cols[13]),
                    "end_power_w": float(cols[14]),
                    "energy_wh": float(cols[15]),
                    "status": cols[16],
                }
            )
        return rows

    def session_summary(self) -> dict[str, Any]:
        if not self.ensure_ready():
            return {}
        sql = f"""
USE `{self.database}`;
SELECT
  COUNT(*),
  COALESCE(SUM(energy_wh), 0),
  COALESCE(MAX(max_power_w), 0),
  COALESCE(MAX(max_actual_amps), 0),
  COALESCE(MAX(DATE_FORMAT(started_at, '%Y-%m-%d %H:%i:%s')), ''),
  COALESCE(SUM(CASE WHEN started_at >= NOW() - INTERVAL 7 DAY THEN energy_wh ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN DATE(started_at) = CURRENT_DATE THEN energy_wh ELSE 0 END), 0)
FROM `{self.table}`;
"""
        try:
            output = self._run_sql(sql)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            return {}

        cols = (output.splitlines()[0] if output else "").split("\t")
        if len(cols) < 7:
            return {}
        return {
            "total_sessions": int(cols[0]),
            "total_energy_wh": float(cols[1]),
            "peak_power_w": float(cols[2]),
            "peak_actual_amps": float(cols[3]),
            "latest_start_at": cols[4] or None,
            "last_7d_energy_wh": float(cols[5]),
            "today_energy_wh": float(cols[6]),
        }


class TeslaEnergyLatestStore(MySQLSessionStore):
    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        tesla_cfg = cfg.get("tesla_energy") or {}
        self.table = self._identifier(str(tesla_cfg.get("live_table", "tesla_energy_live_samples")))

    @staticmethod
    def _float_or_none(value: str | None) -> float | None:
        if value in (None, "", "NULL", "\\N"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def ensure_ready(self) -> bool:
        return self.enabled

    def latest_sample(self) -> tuple[TeslaEnergySnapshot | None, str | None]:
        if not self.enabled:
            return None, "database_disabled"
        sql = f"""
USE `{self.database}`;
SELECT
  DATE_FORMAT(sampled_at, '%Y-%m-%dT%H:%i:%s'),
  solar_generation_w,
  home_consumption_w,
  powerwall_level_pct,
  grid_import_w,
  grid_export_w
FROM `{self.table}`
ORDER BY sampled_at DESC
LIMIT 1;
"""
        try:
            output = self._run_sql(sql)
        except Exception as exc:
            return None, str(exc)
        if not output:
            return None, "no_tesla_live_samples"
        cols = output.splitlines()[0].split("\t")
        if len(cols) < 6:
            return None, "invalid_tesla_live_sample"
        try:
            sampled_at = datetime.fromisoformat(cols[0]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None, "invalid_tesla_sample_timestamp"
        return (
            TeslaEnergySnapshot(
                sampled_at=sampled_at,
                solar_generation_w=self._float_or_none(cols[1]),
                home_consumption_w=self._float_or_none(cols[2]),
                powerwall_level_pct=self._float_or_none(cols[3]),
                grid_import_w=self._float_or_none(cols[4]),
                grid_export_w=self._float_or_none(cols[5]),
            ),
            None,
        )


class AimilerCharger:
    def __init__(self, cfg: dict[str, Any]):
        self.device_id = cfg["id"]
        self.local_key = cfg["key"]
        self.ip = cfg["ip"]
        self.version = float(cfg.get("version", 3.3))

        self.switch_dp = int(cfg.get("switch_dp", 18))
        self.current_dp = int(cfg.get("current_dp", 4))
        self.mode_dp = int(cfg.get("mode_dp", 14))
        self.current_unit = str(cfg.get("current_unit", "amp")).lower()

        self.min_amps = int(cfg.get("min_amps", 8))
        self.max_amps = int(cfg.get("max_amps", 32))
        self.voltage = float(cfg.get("voltage", 230))
        self.phases = int(cfg.get("phases", 1))
        self.status_retry_count = max(1, int(cfg.get("status_retry_count", 3)))
        self.status_retry_delay_seconds = float(cfg.get("status_retry_delay_seconds", 0.35))
        self.status_cache_path = Path(
            cfg.get("status_cache_path", DEFAULT_STATUS_CACHE_PATH)
        )
        self.status_cache_max_age_seconds = float(
            cfg.get("status_cache_max_age_seconds", 70)
        )
        self.raw_tuya_log_path = Path(
            cfg.get("raw_tuya_log_path", DEFAULT_RAW_TUYA_LOG_PATH)
        )
        self.debug_tuya_responses = bool(cfg.get("debug_tuya_responses", True))
        self.raw_tuya_log_retention_seconds = max(
            3 * 24 * 60 * 60,
            int(cfg.get("raw_tuya_log_retention_seconds", 3 * 24 * 60 * 60)),
        )
        self._raw_tuya_last_prune_at: datetime | None = None
        self.socket_persistent = bool(cfg.get("socket_persistent", False))
        self.socket_persistent_recycle_seconds = max(
            0,
            int(cfg.get("socket_persistent_recycle_seconds", 120)),
        )
        self.updatedps_refresh = bool(cfg.get("updatedps_refresh", False))
        self._socket_opened_at: datetime | None = None
        self._socket_identity: tuple[Any, ...] | None = None

        self.device = tinytuya.Device(self.device_id, self.ip, self.local_key)
        self.device.set_version(self.version)
        self.device.set_socketPersistent(self.socket_persistent)

    def _close_socket_if_needed(self) -> None:
        if self.socket_persistent:
            return
        try:
            self.device.close()
        except Exception:
            pass
        self._socket_opened_at = None
        self._socket_identity = None

    def _current_socket_identity(self) -> tuple[Any, ...] | None:
        socket_obj = getattr(self.device, "socket", None)
        if socket_obj is None:
            return None
        try:
            local = socket_obj.getsockname()
        except Exception:
            local = None
        try:
            peer = socket_obj.getpeername()
        except Exception:
            peer = None
        return (id(socket_obj), local, peer)

    def _socket_is_open(self) -> bool:
        return getattr(self.device, "socket", None) is not None

    def _note_socket_state_after_call(self, now: datetime | None = None) -> None:
        now = now or current_local_time()
        identity = self._current_socket_identity()
        if identity is not None:
            if self._socket_opened_at is None or identity != self._socket_identity:
                self._socket_opened_at = now
                self._socket_identity = identity
        else:
            self._socket_opened_at = None
            self._socket_identity = None

    def _socket_diagnostics(self) -> dict[str, Any]:
        socket_obj = getattr(self.device, "socket", None)
        diagnostics: dict[str, Any] = {
            "persistent": self.socket_persistent,
            "open": socket_obj is not None,
            "recycle_seconds": self.socket_persistent_recycle_seconds,
        }
        if socket_obj is None:
            return diagnostics
        now = current_local_time()
        if self._socket_opened_at is not None:
            diagnostics["age_seconds"] = round(
                max(0.0, (now - self._socket_opened_at).total_seconds()),
                3,
            )
            diagnostics["opened_at"] = self._socket_opened_at.isoformat()
        try:
            diagnostics["fd"] = socket_obj.fileno()
        except Exception:
            pass
        try:
            diagnostics["local"] = socket_obj.getsockname()
        except Exception:
            pass
        try:
            diagnostics["peer"] = socket_obj.getpeername()
        except Exception:
            pass
        return diagnostics

    def _recycle_persistent_socket_if_due(self, operation: str) -> None:
        if not self.socket_persistent or self.socket_persistent_recycle_seconds <= 0:
            return
        if not self._socket_is_open():
            self._socket_opened_at = None
            self._socket_identity = None
            return
        now = current_local_time()
        if self._socket_opened_at is None:
            self._socket_opened_at = now
            return
        age_seconds = max(0.0, (now - self._socket_opened_at).total_seconds())
        if age_seconds < self.socket_persistent_recycle_seconds:
            return
        try:
            self.device.close()
        finally:
            self._socket_opened_at = None
            self._socket_identity = None
        self._log_raw_tuya_response(
            "socket_recycle",
            {
                "reason": "persistent_socket_age",
                "age_seconds": round(age_seconds, 3),
                "before_operation": operation,
            },
        )

    def _close_persistent_socket_before_write(self, operation: str) -> None:
        if not self.socket_persistent or not self._socket_is_open():
            return
        diagnostics = self._socket_diagnostics()
        try:
            self.device.close()
        finally:
            self._socket_opened_at = None
            self._socket_identity = None
        self._log_raw_tuya_response(
            "socket_recycle",
            {
                "reason": "fresh_socket_for_write",
                "before_operation": operation,
                "previous_socket": diagnostics,
            },
        )

    def _close_persistent_socket_after_error(self, response: Any, operation: str) -> None:
        if not self.socket_persistent:
            return
        if not (isinstance(response, dict) and (response.get("Error") or response.get("Err"))):
            return
        if not self._socket_is_open():
            self._socket_opened_at = None
            self._socket_identity = None
            return
        try:
            self.device.close()
        finally:
            self._socket_opened_at = None
            self._socket_identity = None
        self._log_raw_tuya_response(
            "socket_recycle",
            {
                "reason": "tuya_error_response",
                "operation": operation,
                "error": response.get("Error"),
                "err": response.get("Err"),
            },
        )

    def _request_dps_refresh(self, operation: str) -> None:
        """Ask the device to push fresh DP values on the open persistent socket.

        Some charger firmwares reply with cached DPS values for the lifetime
        of a persistent TCP session; UPDATEDPS (command 18) refreshes them
        in-band so the session does not need to be recycled just to get fresh
        data. Fire-and-forget: devices that don't support command 18 ignore it.
        """
        if not self.socket_persistent or not self.updatedps_refresh:
            return
        if not self._socket_is_open():
            return
        index = sorted(int(key) for key in self._required_dps_keys())
        try:
            self.device.updatedps(index=index, nowait=True)
        except Exception as exc:
            self._log_raw_tuya_response(
                "updatedps_error",
                {"error": str(exc), "operation": operation, "index": index},
            )

    def _required_dps_keys(self) -> set[str]:
        return {
            "3",
            "9",
            "13",
            str(self.switch_dp),
            str(self.current_dp),
            str(self.mode_dp),
        }

    def _observable_dps_keys(self) -> set[str]:
        return {
            "3",
            "9",
            "13",
            str(self.current_dp),
            str(self.mode_dp),
        }

    def _load_cached_status(self) -> dict[str, Any] | None:
        try:
            with self.status_cache_path.open("r", encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, TypeError, ValueError):
            return None

        if not isinstance(cached, dict):
            return None

        dps = cached.get("dps")
        cached_at = cached.get("_cached_at")
        if not isinstance(dps, dict) or not isinstance(cached_at, str):
            return None

        try:
            cached_ts = datetime.fromisoformat(cached_at)
        except ValueError:
            return None

        cache_age_seconds = (current_local_time() - cached_ts).total_seconds()
        if cache_age_seconds < 0 or cache_age_seconds > self.status_cache_max_age_seconds:
            return None
        if not self._required_dps_keys().issubset(dps.keys()):
            return None

        cached["_cache_age_seconds"] = round(cache_age_seconds, 3)
        return cached

    def _save_cached_status(self, response: dict[str, Any]) -> None:
        dps = response.get("dps")
        if not isinstance(dps, dict):
            return

        payload = dict(response)
        payload["_cached_at"] = current_local_time().isoformat()
        try:
            self.status_cache_path.write_text(
                json.dumps(payload, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            return

    def _prune_raw_tuya_log(self, now: datetime) -> None:
        raw_tuya_log_path = getattr(self, "raw_tuya_log_path", None)
        if raw_tuya_log_path is None:
            return
        last_prune_at = getattr(self, "_raw_tuya_last_prune_at", None)
        if (
            last_prune_at is not None
            and (now - last_prune_at).total_seconds() < 3600
        ):
            return
        self._raw_tuya_last_prune_at = now
        cutoff = now - timedelta(seconds=self.raw_tuya_log_retention_seconds)
        _rotate_jsonl_log(raw_tuya_log_path, cutoff, RAW_TUYA_LOG_MAX_BYTES)

    def _log_raw_tuya_response(
        self,
        operation: str,
        response: Any,
        *,
        request: dict[str, Any] | None = None,
        attempt: int | None = None,
        allow_cached_merge: bool | None = None,
        response_kind: str | None = None,
    ) -> None:
        if not getattr(self, "debug_tuya_responses", True):
            return
        if getattr(self, "raw_tuya_log_path", None) is None:
            return
        now = current_local_time()
        self._prune_raw_tuya_log(now)
        entry = {
            "timestamp": now.isoformat(),
            "operation": operation,
            "device_id": self.device_id,
            "ip": self.ip,
            "version": self.version,
            "attempt": attempt,
            "allow_cached_merge": allow_cached_merge,
            "response_kind": response_kind,
            "request": request,
            "socket": self._socket_diagnostics(),
            "response": response,
        }
        try:
            with self.raw_tuya_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
        except OSError:
            return

    def _merge_with_cached_status(self, response: dict[str, Any]) -> dict[str, Any] | None:
        dps = response.get("dps")
        if not isinstance(dps, dict):
            return None

        cached = self._load_cached_status()
        if cached is None:
            return None

        merged_dps = dict(cached["dps"])
        merged_dps.update(dps)
        switch_key = str(self.switch_dp)
        if switch_key in dps and not bool(dps.get(switch_key)):
            if "9" not in dps:
                merged_dps["9"] = 0
            if "3" not in dps and str(merged_dps.get("3")) == "charger_charging":
                merged_dps["3"] = "charger_insert"
            if "13" not in dps and str(merged_dps.get("13")) == "controlpi_6v_pwm":
                merged_dps["13"] = "controlpi_9v"
        if not self._required_dps_keys().issubset(merged_dps.keys()):
            return None

        merged = dict(cached)
        merged.update(response)
        merged["dps"] = merged_dps

        cached_data = cached.get("data")
        response_data = response.get("data")
        merged_data = dict(cached_data) if isinstance(cached_data, dict) else {}
        if isinstance(response_data, dict):
            merged_data.update(response_data)
        merged_data["dps"] = merged_dps
        if merged_data:
            merged["data"] = merged_data

        merged["_cached_merge"] = True
        merged["_cache_age_seconds"] = cached.get("_cache_age_seconds")
        merged["_partial_dps_keys"] = sorted(dps.keys())
        return merged

    def _format_current(self, amps: int) -> int:
        if self.current_unit == "deciamp":
            return amps * 10
        if self.current_unit != "amp":
            raise ValueError("current_unit must be 'amp' or 'deciamp'")
        return amps

    def _decode_current(self, value: Any) -> int | None:
        if value is None:
            return None
        try:
            raw = int(value)
        except (TypeError, ValueError):
            return None
        if self.current_unit == "deciamp":
            return int(round(raw / 10))
        return raw

    def _status_has_required_fields(self, response: dict[str, Any]) -> bool:
        dps = response.get("dps")
        if not isinstance(dps, dict):
            return False
        return self._required_dps_keys().issubset(dps.keys())

    def _status_has_observable_fields(self, response: dict[str, Any]) -> bool:
        dps = response.get("dps")
        if not isinstance(dps, dict):
            return False
        return self._observable_dps_keys().issubset(dps.keys())

    def _status_response_kind(self, response: Any) -> str:
        if not isinstance(response, dict):
            return "non_dict_response"
        if response.get("Error") or response.get("Err"):
            return "tuya_error"
        dps = response.get("dps")
        if not isinstance(dps, dict):
            return "missing_dps"
        if self._status_has_required_fields(response):
            return "full_status"
        if self._status_has_observable_fields(response):
            return "degraded_observable_status"
        return "partial_update"

    def status(self, allow_cached_merge: bool = True) -> dict[str, Any]:
        last_response: Any = None
        for attempt in range(1, self.status_retry_count + 1):
            self._recycle_persistent_socket_if_due("status")
            if attempt == 1:
                self._request_dps_refresh("status")
            try:
                response = self.device.status()
            finally:
                self._close_socket_if_needed()
                self._note_socket_state_after_call()
            self._close_persistent_socket_after_error(response, "status")
            last_response = response
            response_kind = self._status_response_kind(response)
            if isinstance(response, dict) and self._status_has_required_fields(response):
                self._log_raw_tuya_response(
                    "status",
                    response,
                    attempt=attempt,
                    allow_cached_merge=allow_cached_merge,
                    response_kind=response_kind,
                )
                self._save_cached_status(response)
                return response
            if isinstance(response, dict) and self._status_has_observable_fields(response):
                response = dict(response)
                missing = sorted(self._required_dps_keys() - set(response.get("dps", {}).keys()))
                response["_degraded_status"] = True
                response["_missing_required_dps"] = missing
                self._log_raw_tuya_response(
                    "status",
                    response,
                    attempt=attempt,
                    allow_cached_merge=allow_cached_merge,
                    response_kind=response_kind,
                )
                return response
            if allow_cached_merge and isinstance(response, dict):
                merged = self._merge_with_cached_status(response)
                if merged is not None:
                    partial_dps = sorted((response.get("dps") or {}).keys())
                    self._log_raw_tuya_response(
                        "status_raw_partial",
                        response,
                        attempt=attempt,
                        allow_cached_merge=allow_cached_merge,
                        response_kind=response_kind,
                    )
                    self._log_raw_tuya_response(
                        "status_effective",
                        merged,
                        request={"source": "cached_merge", "partial_dps_keys": partial_dps},
                        attempt=attempt,
                        allow_cached_merge=allow_cached_merge,
                        response_kind="cached_merge_effective_status",
                    )
                    self._save_cached_status(merged)
                    return merged
            self._log_raw_tuya_response(
                "status",
                response,
                attempt=attempt,
                allow_cached_merge=allow_cached_merge,
                response_kind=response_kind,
            )
            if attempt < self.status_retry_count:
                time.sleep(self.status_retry_delay_seconds)
        raise RuntimeError(f"Incomplete charger status payload after retries: {last_response}")

    def fresh_status(self) -> dict[str, Any]:
        """Status read on a brand-new socket with no cached-status merging.

        A persistent socket can keep returning stale full-status snapshots
        after a command, so verification reads must not reuse it.
        """
        self._close_persistent_socket_before_write("fresh_status")
        return self.status(allow_cached_merge=False)

    def set_enabled(self, enabled: bool) -> None:
        self._close_persistent_socket_before_write("set_status")
        self._recycle_persistent_socket_if_due("set_status")
        try:
            result = self.device.set_status(bool(enabled), switch=self.switch_dp)
        finally:
            self._close_socket_if_needed()
            self._note_socket_state_after_call()
        self._close_persistent_socket_after_error(result, "set_status")
        self._log_raw_tuya_response(
            "set_status",
            result,
            request={"switch": self.switch_dp, "value": bool(enabled)},
            response_kind="write_response",
        )
        if isinstance(result, dict) and result.get("Error"):
            raise RuntimeError(f"Failed to set charger enable={enabled}: {result}")

    def set_mode(self, mode: str) -> None:
        self._close_persistent_socket_before_write("set_value")
        self._recycle_persistent_socket_if_due("set_value")
        try:
            result = self.device.set_value(self.mode_dp, str(mode))
        finally:
            self._close_socket_if_needed()
            self._note_socket_state_after_call()
        self._close_persistent_socket_after_error(result, "set_value")
        self._log_raw_tuya_response(
            "set_value",
            result,
            request={"dp": self.mode_dp, "value": str(mode), "field": "mode"},
            response_kind="write_response",
        )
        if isinstance(result, dict) and result.get("Error"):
            raise RuntimeError(f"Failed to set charger mode={mode}: {result}")

    def set_amps(self, amps: int) -> int:
        clamped = max(self.min_amps, min(self.max_amps, int(amps)))
        value = self._format_current(clamped)
        self._close_persistent_socket_before_write("set_value")
        self._recycle_persistent_socket_if_due("set_value")
        try:
            result = self.device.set_value(self.current_dp, value)
        finally:
            self._close_socket_if_needed()
            self._note_socket_state_after_call()
        self._close_persistent_socket_after_error(result, "set_value")
        self._log_raw_tuya_response(
            "set_value",
            result,
            request={
                "dp": self.current_dp,
                "value": value,
                "field": "current_amps",
                "amps": clamped,
            },
            response_kind="write_response",
        )
        if isinstance(result, dict) and result.get("Error"):
            raise RuntimeError(f"Failed to set charger current={clamped}A: {result}")
        return clamped

    def summarize_status(
        self,
        status: dict[str, Any] | None = None,
        *,
        allow_cached_merge: bool = True,
    ) -> dict[str, Any]:
        status = status or self.status(allow_cached_merge=allow_cached_merge)
        dps = status.get("dps", {})

        mode = str(dps.get(str(self.mode_dp), "unknown"))
        state = str(dps.get("3", "unknown"))
        pilot_state = str(dps.get("13", "unknown"))
        setpoint_amps = self._decode_current(dps.get(str(self.current_dp)))
        power_w = float(dps.get("9", 0) or 0)
        switch_key = str(self.switch_dp)
        switch_state_known = switch_key in dps
        enabled = bool(dps.get(switch_key, False)) if switch_state_known else False
        connection_hint = dps.get("107")
        pilot_metric = dps.get("24")
        session_metric = dps.get("101")
        aux_metric = dps.get("106")
        fault_bitmap = dps.get("10")
        dp6_telemetry = decode_aimiler_dp6(dps.get("6"))

        actively_charging = (
            state == "charger_charging"
            or power_w >= 100
            or pilot_state == "controlpi_6v_pwm"
        )
        # controlpi_6v (without _pwm) means the car is requesting power but charging
        # hasn't been authorized yet (static 6V, no PWM).  It is an insert signal,
        # not an active-charging signal.
        raw_insert_signal = (
            pilot_state.startswith("controlpi_9v")
            or pilot_state == "controlpi_6v"
            or state in {
                "charger_insert",
                "charger_wait_car",
                "charger_waiting",
                "charger_prepare",
            }
        )
        # Empirical mapping from actual device snapshots (Aimiler UA_7KW, fw B5_V1.0.0):
        # - wall/disconnected idle:  DP106 ≈ 54, DP107 ≈ 54  (high values ~50+)
        # - car-connected idle:      DP106 ≈ 29-32, DP107 ≈ 29-32  (lower values)
        # - actively charging:       DP107 absent or low
        disconnected_idle_pattern = (
            pilot_metric == 27
            and session_metric == 81
            and aux_metric is not None and aux_metric > 50
            and connection_hint is not None and connection_hint > 50
        )
        insert_sensed = raw_insert_signal and not disconnected_idle_pattern
        # Car-connected-idle: insert signal present AND connection_hint in low range (~32)
        # rather than the wall-idle high range (~54).
        idle_connected = (
            insert_sensed
            and connection_hint is not None
            and connection_hint < 50
        )
        vehicle_connected = actively_charging or idle_connected
        if actively_charging and not switch_state_known:
            enabled = True

        return {
            "mode": mode,
            "state": state,
            "pilot_state": pilot_state,
            "enabled": enabled,
            "switch_state_known": switch_state_known,
            "status_quality": "degraded_missing_switch" if not switch_state_known else "complete",
            "setpoint_amps": setpoint_amps,
            "power_w": power_w,
            "actively_charging": actively_charging,
            "insert_sensed": insert_sensed,
            "vehicle_connected": vehicle_connected,
            "idle_connected": idle_connected,
            "pilot_metric": pilot_metric,
            "session_metric": session_metric,
            "aux_metric": aux_metric,
            "fault_bitmap": fault_bitmap,
            "timer_enabled": bool(dps.get("28", 0)),
            "timer_value": dps.get("103"),
            "temperature_raw": dps.get("104"),
            "connection_hint": connection_hint,
            "temp_current_c": dps.get("24"),
            "f_temp": dps.get("101"),
            "charger_time": dps.get("104"),
            "charge_capacity": dps.get("106"),
            "charge_chart": dps.get("107"),
            "mode_set_raw": dps.get("33"),
            "charge_record_raw": dps.get("108"),
            "error_data": dps.get("120"),
            "cp_data": dps.get("121"),
            "dp6_telemetry": dp6_telemetry,
            "raw_dps": dps,
        }


class AutoScheduleController:
    def __init__(
        self,
        cfg: dict[str, Any],
        state_path: Path = DEFAULT_STATE_PATH,
        config_path: Path | None = None,
    ):
        self.state_path = state_path
        self.config_path = config_path
        self.next_rediscovery_at: datetime | None = None
        self._config_mtime_ns: int | None = None
        self._device_state_bootstrapped = False
        self._apply_loaded_config(cfg)
        if self.config_path is not None:
            try:
                self._config_mtime_ns = self.config_path.stat().st_mtime_ns
            except OSError:
                self._config_mtime_ns = None

        self.stop_requested = False

    def _apply_loaded_config(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.charger = AimilerCharger(cfg["charger"])
        self.session_store = MySQLSessionStore(cfg)
        self.tesla_latest_store = TeslaEnergyLatestStore(cfg)
        self._cached_hvac_status: HVACStatus | None = None
        self._cached_hvac_error: str | None = None
        self._cached_hvac_read_at: datetime | None = None
        charger_cfg = cfg["charger"]
        self.rediscovery_enabled = bool(charger_cfg.get("rediscovery_enabled", True))
        self.rediscovery_scan_seconds = max(
            1, int(charger_cfg.get("rediscovery_scan_seconds", 10))
        )
        self.rediscovery_interval = timedelta(
            hours=max(1.0, float(charger_cfg.get("rediscovery_interval_hours", 24)))
        )

        self.automation_settings = normalize_automation_config(cfg.get("automation") or {})
        auto_cfg = active_automation_config(self.automation_settings)
        self.active_profile = str(auto_cfg.get("active_profile") or "spring")
        self.active_profile_cfg = deepcopy(auto_cfg)
        self.hvac = HVACStatusClient(auto_cfg)
        self.poll_seconds = int(auto_cfg.get("poll_seconds", 60))
        self.allow_when_hvac_unavailable = bool(
            auto_cfg.get("allow_when_hvac_unavailable", True)
        )
        self.automation_min_amps = max(
            self.charger.min_amps,
            min(
                self.charger.max_amps,
                int(auto_cfg.get("automation_min_amps", self.charger.min_amps)),
            ),
        )
        self.min_current_change_interval_seconds = max(
            0, int(auto_cfg.get("min_current_change_interval_seconds", 180))
        )
        # Solar fluctuation hysteresis: ignore small solar changes and require a sustained
        # change before actually adjusting the charge amps.
        self.tesla_solar_change_threshold_amps = max(
            0, int(auto_cfg.get("tesla_solar_change_threshold_amps", 2))
        )
        # Wait this long (seconds) before cutting amps when solar drops (handles transients
        # like a coffee maker or cloud shadow — default 5 min).
        self.tesla_solar_change_sustain_down_seconds = max(
            0, int(auto_cfg.get("tesla_solar_change_sustain_down_seconds", 300))
        )
        # Wait this long (seconds) before boosting amps when solar increases (default 3 min).
        self.tesla_solar_change_sustain_up_seconds = max(
            0, int(auto_cfg.get("tesla_solar_change_sustain_up_seconds", 180))
        )
        self.energy_down_sustain_seconds = max(
            0, int(auto_cfg.get("energy_down_sustain_seconds", 180))
        )
        self.fault_reset_enabled = bool(auto_cfg.get("fault_reset_enabled", True))
        self.fault_reset_cooldown_seconds = max(
            0, int(auto_cfg.get("fault_reset_cooldown_seconds", 900))
        )
        self.fault_reset_off_seconds = max(
            0.0, float(auto_cfg.get("fault_reset_off_seconds", 3))
        )
        self.log_charger_events = bool(auto_cfg.get("log_charger_events", True))
        self.charger_event_log_path = Path(
            auto_cfg.get("charger_event_log_path", DEFAULT_EVENT_LOG_PATH)
        )
        self.hvac_refresh_seconds = max(15, int(auto_cfg.get("hvac_refresh_seconds", 120)))
        self.tesla_solar_control_enabled = bool(
            auto_cfg.get("tesla_solar_control_enabled", False)
        )
        self.tesla_solar_max_sample_age_seconds = max(
            60, int(auto_cfg.get("tesla_solar_max_sample_age_seconds", 900))
        )
        self.tesla_solar_generation_cap_watts = max(
            0.0, float(auto_cfg.get("tesla_solar_generation_cap_watts", 0))
        )
        self.tesla_solar_reserve_watts = max(
            0.0, float(auto_cfg.get("tesla_solar_reserve_watts", 500))
        )
        self.tesla_solar_grid_import_stop_watts = max(
            0.0, float(auto_cfg.get("tesla_solar_grid_import_stop_watts", 150))
        )
        cloudy_min_amps = int(auto_cfg.get("tesla_solar_cloudy_min_amps", 0))
        self.tesla_solar_cloudy_min_amps = (
            max(self.automation_min_amps, min(self.charger.max_amps, cloudy_min_amps))
            if cloudy_min_amps > 0
            else 0
        )
        self.tesla_solar_powerwall_full_boost_pct = max(
            0.0,
            min(100.0, float(auto_cfg.get("tesla_solar_powerwall_full_boost_pct", 98))),
        )
        self.tesla_solar_powerwall_reserve_pct = max(
            0.0, min(100.0, float(auto_cfg.get("tesla_solar_powerwall_reserve_pct", 80)))
        )
        # Incremental margin control: if (solar - total_home) > boost threshold, add 1A.
        # 1A = 230W at 230V. Default: boost when >1000W headroom above consumption.
        self.tesla_solar_margin_boost_watts = max(
            0.0, float(auto_cfg.get("tesla_solar_margin_boost_watts", 500.0))
        )
        # If (solar - total_home) < trim threshold, cut 1A. Default: trim when <200W margin.
        self.tesla_solar_margin_trim_watts = float(
            auto_cfg.get("tesla_solar_margin_trim_watts", 200.0)
        )
        self.no_charge_start_minutes = parse_hhmm(str(auto_cfg.get("no_charge_start", "16:00")))
        self.no_charge_end_minutes = parse_hhmm(str(auto_cfg.get("no_charge_end", "21:00")))
        self.night_charge_start_minutes = parse_hhmm(
            str(auto_cfg.get("night_charge_start", "21:00"))
        )
        self.night_new_start_cutoff_minutes = parse_hhmm(
            str(auto_cfg.get("night_new_start_cutoff", "03:00"))
        )
        self.night_charge_amps = int(auto_cfg.get("night_charge_amps", 16))
        self.night_completion_grace_seconds = int(
            auto_cfg.get("night_completion_grace_seconds", 90)
        )
        self.night_force_enable_without_connection = bool(
            auto_cfg.get("night_force_enable_without_connection", True)
        )
        self.emergency_amps = int(auto_cfg.get("emergency_amps", self.charger.max_amps))
        self.charge_now_mode = str(auto_cfg.get("charge_now_mode", "charge_now"))
        self.auto_force_charge_now_mode = bool(auto_cfg.get("auto_force_charge_now_mode", True))
        self.startup_grace_seconds = int(auto_cfg.get("startup_grace_seconds", 30))
        self.startup_failure_cooldown_seconds = max(
            0, int(auto_cfg.get("startup_failure_cooldown_seconds", 900))
        )
        self.log_fetch_details = bool(auto_cfg.get("log_fetch_details", True))
        self.fetch_error_backoff_after_failures = max(
            1, int(auto_cfg.get("fetch_error_backoff_after_failures", 6))
        )
        self.fetch_error_backoff_max_seconds = max(
            0, int(auto_cfg.get("fetch_error_backoff_max_seconds", 600))
        )
        self._last_backoff_logged_seconds: float | None = None
        raw_energy_cfg = auto_cfg.get("energy_controller") or {}
        self.energy_config = make_energy_config(
            raw_energy_cfg,
            charger_cfg=cfg.get("charger"),
            legacy_profile=auto_cfg,
        )
        self.poll_seconds = self.energy_config.control_loop_seconds
        self.charge_now_mode = self.energy_config.charge_now_mode
        self.auto_force_charge_now_mode = self.energy_config.auto_force_charge_now_mode

    def reload_config_if_needed(self) -> bool:
        if self.config_path is None:
            return False

        try:
            current_mtime_ns = self.config_path.stat().st_mtime_ns
        except OSError:
            return False

        if self._config_mtime_ns == current_mtime_ns:
            return False

        try:
            cfg = load_config(self.config_path)
            self._apply_loaded_config(cfg)
            self._config_mtime_ns = current_mtime_ns
            print(
                f"{current_local_time().strftime('%Y-%m-%d %H:%M:%S')} "
                f"config_reloaded active_profile={self.active_profile} updated_at={self.energy_config.updated_at}",
                flush=True,
            )
            return True
        except Exception as exc:
            print(
                f"{current_local_time().strftime('%Y-%m-%d %H:%M:%S')} "
                f"config_reload_failed error={exc}",
                flush=True,
            )
            return False

    def refresh_charger_ip(self, reason: str, now: datetime | None = None) -> bool:
        if not self.rediscovery_enabled:
            return False

        now = now or current_local_time()
        self.next_rediscovery_at = now + self.rediscovery_interval

        device_id = str(self.cfg["charger"].get("id", "")).strip()
        current_ip = str(self.cfg["charger"].get("ip", "")).strip()
        if not device_id:
            print(
                f"{now.strftime('%Y-%m-%d %H:%M:%S')} "
                f"rediscovery_skipped reason={reason} error=missing_device_id",
                flush=True,
            )
            return False

        try:
            discovered_ip = discover_device_ip(device_id, self.rediscovery_scan_seconds)
        except Exception as exc:
            print(
                f"{now.strftime('%Y-%m-%d %H:%M:%S')} "
                f"rediscovery_error reason={reason} error={exc}",
                flush=True,
            )
            return False

        if not discovered_ip:
            print(
                f"{now.strftime('%Y-%m-%d %H:%M:%S')} "
                f"rediscovery_miss reason={reason} device_id={device_id} configured_ip={current_ip}",
                flush=True,
            )
            return False

        if discovered_ip == current_ip:
            print(
                f"{now.strftime('%Y-%m-%d %H:%M:%S')} "
                f"rediscovery_ok reason={reason} ip={discovered_ip}",
                flush=True,
            )
            return False

        self.cfg["charger"]["ip"] = discovered_ip
        self.charger = AimilerCharger(self.cfg["charger"])
        if self.config_path is not None:
            save_config(self.cfg, self.config_path)
        print(
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"rediscovery_update reason={reason} old_ip={current_ip} new_ip={discovered_ip}",
            flush=True,
        )
        return True

    def reconnect_charger(self, reason: str, now: datetime | None = None) -> None:
        now = now or current_local_time()
        try:
            self.charger._close_socket_if_needed()
        except Exception:
            pass
        self.charger = AimilerCharger(self.cfg["charger"])
        print(
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"charger_reconnect reason={reason} ip={self.cfg['charger'].get('ip')}",
            flush=True,
        )

    def _record_charger_fetch_error(self, error_text: str, now: datetime) -> dict[str, Any]:
        state = load_automation_state(self.state_path)
        control = state.setdefault("control", {})
        count = int(control.get("consecutive_charger_fetch_errors") or 0) + 1
        if count == 1 or not control.get("charger_fetch_error_first_seen_at"):
            control["charger_fetch_error_first_seen_at"] = now.isoformat()
        control["consecutive_charger_fetch_errors"] = count
        control["charger_fetch_error_last_seen_at"] = now.isoformat()
        control["charger_fetch_error_last_message"] = str(error_text or "")
        save_automation_state(state, self.state_path)
        return state

    def in_block_window(self, now: datetime) -> bool:
        minutes = now.hour * 60 + now.minute
        start = self.no_charge_start_minutes
        end = self.no_charge_end_minutes
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end

    def night_session_key(self, now: datetime) -> str | None:
        minutes = now.hour * 60 + now.minute
        if minutes >= self.night_charge_start_minutes:
            return now.date().isoformat()
        if minutes < self.night_new_start_cutoff_minutes:
            return (now.date() - timedelta(days=1)).isoformat()
        return None

    def after_night_new_start_cutoff(self, now: datetime) -> bool:
        minutes = now.hour * 60 + now.minute
        return minutes >= self.night_new_start_cutoff_minutes


    def _load_hvac(self) -> tuple[HVACStatus | None, str | None]:
        now = current_local_time()
        if (
            self._cached_hvac_read_at is not None
            and (now - self._cached_hvac_read_at).total_seconds() < self.hvac_refresh_seconds
        ):
            return self._cached_hvac_status, self._cached_hvac_error
        try:
            status = self.hvac.read_status()
            self._cached_hvac_status = status
            self._cached_hvac_error = None
            self._cached_hvac_read_at = now
            return status, None
        except (OSError, URLError, TimeoutError, ValueError) as exc:
            self._cached_hvac_status = None
            self._cached_hvac_error = str(exc)
            self._cached_hvac_read_at = now
            return None, str(exc)

    @staticmethod
    def _state_datetime(raw_value: Any) -> datetime | None:
        if not raw_value:
            return None
        try:
            return datetime.fromisoformat(str(raw_value))
        except ValueError:
            return None

    @staticmethod
    def _clear_emergency_state(state: dict[str, Any]) -> None:
        # target_completed_at/-_kwh survive the clear so the dashboard can show
        # the outcome of the last "charge N kWh" request after it finishes.
        emergency = state.setdefault("emergency", {})
        emergency.update(
            {
                "active": False,
                "started_at": None,
                "seen_charging": False,
                "expires_at": None,
                "requested_amps": None,
                "duration_minutes": None,
                "target_energy_kwh": None,
                "delivered_energy_wh": 0.0,
                "energy_last_sample_at": None,
            }
        )

    def _track_emergency_energy_target(
        self,
        state_after: dict[str, Any],
        emergency_state: dict[str, Any],
        charger: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        """Accumulate delivered energy for a "charge N kWh" override.

        Once the target is reached the override is cleared the same way a
        timed expiry is: no force stop, so normal solar/night automation
        immediately resumes control.
        """
        if not emergency_state.get("active"):
            return emergency_state
        try:
            target_wh = float(emergency_state.get("target_energy_kwh") or 0.0) * 1000.0
        except (TypeError, ValueError):
            target_wh = 0.0
        if target_wh <= 0:
            return emergency_state
        delivered_wh = float(emergency_state.get("delivered_energy_wh") or 0.0)
        last_sample_at = self._state_datetime(emergency_state.get("energy_last_sample_at"))
        if charger.get("actively_charging") and last_sample_at is not None:
            elapsed_hours = (now - last_sample_at).total_seconds() / 3600.0
            # Cap the step so a stalled loop cannot credit a large energy jump
            # from a single stale power reading.
            elapsed_hours = min(max(elapsed_hours, 0.0), 0.25)
            delivered_wh += elapsed_hours * float(charger.get("power_w") or 0.0)
        emergency = state_after.setdefault("emergency", {})
        emergency["delivered_energy_wh"] = round(delivered_wh, 3)
        emergency["energy_last_sample_at"] = now.isoformat()
        if delivered_wh >= target_wh:
            emergency["target_completed_at"] = now.isoformat()
            emergency["target_completed_kwh"] = round(delivered_wh / 1000.0, 3)
            self._clear_emergency_state(state_after)
            # The requested dose is delivered: keep night auto-charging from
            # restarting while the car stays plugged in. Solar charging is
            # deliberately unaffected.
            state_after.setdefault("night", {})["start_blocked_until_disconnect"] = True
        return deepcopy(state_after.get("emergency") or {})

    @staticmethod
    def _request_force_stop(
        state: dict[str, Any],
        now: datetime,
        reason: str,
        *,
        hold_until_disconnect: bool = True,
    ) -> None:
        request_temporary_force_stop(
            state,
            now,
            reason,
            hold_until_disconnect=hold_until_disconnect,
        )

    @staticmethod
    def _clear_force_stop(state: dict[str, Any]) -> None:
        clear_force_stop_state(state)

    def _force_stop_active(
        self,
        state: dict[str, Any],
        vehicle_connected: bool,
        now: datetime | None = None,
    ) -> bool:
        control = state.setdefault("control", {})
        requested_at = self._state_datetime(control.get("force_stop_requested_at"))
        if requested_at is None:
            return False
        expires_at = self._state_datetime(control.get("force_stop_expires_at"))
        if expires_at is None:
            expires_at = requested_at + timedelta(minutes=FORCE_STOP_MAX_DURATION_MINUTES)
            control["force_stop_expires_at"] = expires_at.isoformat()
        if (now or current_local_time()) >= expires_at:
            self._clear_force_stop(state)
            return False
        if bool(control.get("force_stop_hold_until_disconnect")) and vehicle_connected:
            return True
        self._clear_force_stop(state)
        return False

    def _preserve_newer_emergency_state(self, state_after: dict[str, Any]) -> None:
        try:
            latest = load_automation_state(self.state_path)
        except Exception:
            return

        latest_control = latest.get("control") or {}
        state_control = state_after.setdefault("control", {})
        latest_force_stop_at = self._state_datetime(latest_control.get("force_stop_requested_at"))
        state_force_stop_at = self._state_datetime(state_control.get("force_stop_requested_at"))
        cleared_force_stop_at = self._state_datetime(state_control.get("force_stop_cleared_request_at"))
        if latest_force_stop_at is not None and (
            state_force_stop_at is None or latest_force_stop_at > state_force_stop_at
        ) and (
            cleared_force_stop_at is None or latest_force_stop_at > cleared_force_stop_at
        ):
            for key in (
                "force_stop_requested_at",
                "force_stop_reason",
                "force_stop_hold_until_disconnect",
                "force_stop_expires_at",
                "energy_down_pending_target",
                "energy_down_pending_since",
            ):
                state_control[key] = latest_control.get(key)
            if not (latest.get("emergency") or {}).get("active"):
                state_after["emergency"] = deepcopy(latest.get("emergency") or {})

        latest_emergency = latest.get("emergency") or {}
        state_emergency = state_after.get("emergency") or {}
        latest_started_at = self._state_datetime(latest_emergency.get("started_at"))
        state_started_at = self._state_datetime(state_emergency.get("started_at"))
        latest_expires_at = self._state_datetime(latest_emergency.get("expires_at"))
        # Never resurrect an emergency whose window has already elapsed. Clearing
        # an emergency sets started_at=None, which would otherwise always look
        # "older" than the on-disk copy and cause an expired session to be
        # reinstated on every loop (permanent forced-stop deadlock).
        latest_not_expired = (
            latest_expires_at is None or current_local_time() < latest_expires_at
        )
        if (
            latest_emergency.get("active")
            and latest_not_expired
            and latest_started_at is not None
            and (state_started_at is None or latest_started_at > state_started_at)
        ):
            state_after["emergency"] = deepcopy(latest_emergency)
            self._clear_force_stop(state_after)

    @staticmethod
    def _emergency_session_end_reason(
        emergency_state: dict[str, Any],
        charger: dict[str, Any],
        vehicle_connected: bool,
    ) -> str | None:
        if not emergency_state.get("active") or not emergency_state.get("seen_charging"):
            return None
        if not vehicle_connected:
            return "emergency_vehicle_disconnected"
        if not charger.get("enabled"):
            return "emergency_output_disabled"
        if not charger.get("actively_charging") and float(charger.get("power_w") or 0.0) < 100.0:
            return "emergency_charging_stopped"
        return None

    def _clear_absorbed_predictive_timestamps(
        self,
        device_state: dict[str, Any],
        telemetry: EnergyTelemetry,
    ) -> dict[str, Any]:
        if telemetry.timestamp is None:
            return device_state

        for key in (
            "hvac_changed_at",
            "hvac_heating_changed_at",
        ):
            changed_at = self._state_datetime(device_state.get(key))
            if changed_at is not None and changed_at <= telemetry.timestamp + timedelta(seconds=30):
                device_state[key] = None

        return device_state

    def _apply_energy_down_hold(
        self,
        energy_decision: Any,
        charger_state: ChargerState,
        state_after: dict[str, Any],
        now: datetime,
    ) -> Any:
        control = state_after.setdefault("control", {})

        def clear_pending() -> None:
            control["energy_down_pending_target"] = None
            control["energy_down_pending_since"] = None

        sustain = int(getattr(self, "energy_down_sustain_seconds", 180))
        current_target = charger_state.current_setpoint_amps or charger_state.current_amps
        if charger_state.last_charger_command_type == "STOP" and not charger_state.is_enabled:
            clear_pending()
            return energy_decision
        if sustain <= 0 or current_target is None or not (charger_state.is_enabled or charger_state.is_charging):
            clear_pending()
            return energy_decision

        low_solar_stop = (
            not energy_decision.desired_charger_enabled
            and energy_decision.mode == "LOW_SOLAR_STOP"
        )
        predictive_components = getattr(energy_decision, "predicted_load_components", None) or {}
        if predictive_components:
            clear_pending()
            return energy_decision
        lower_amp_target = (
            energy_decision.desired_charger_enabled
            and energy_decision.desired_amps is not None
            and int(energy_decision.desired_amps) < int(current_target)
            and not energy_decision.is_emergency_mode
            and energy_decision.mode
            not in {"NIGHT_CHARGING", "NIGHT_CONTINUING_AFTER_CUTOFF", "NO_CHARGE_WINDOW", "SAFETY_STOP"}
        )

        if not low_solar_stop and not lower_amp_target:
            clear_pending()
            return energy_decision

        pending_target = "stop" if low_solar_stop else str(int(energy_decision.desired_amps))
        pending_since = self._state_datetime(control.get("energy_down_pending_since"))
        previous_target = control.get("energy_down_pending_target")
        if previous_target != pending_target or pending_since is None:
            pending_since = now
            control["energy_down_pending_target"] = pending_target
            control["energy_down_pending_since"] = pending_since.isoformat()

        elapsed = (now - pending_since).total_seconds()
        if elapsed >= sustain:
            clear_pending()
            return energy_decision

        held = replace(
            energy_decision,
            desired_charger_enabled=True,
            desired_amps=int(current_target),
            reason="energy_down_change_pending",
            mode="LOW_SOLAR_GRACE" if low_solar_stop else energy_decision.mode,
            action_status="WAITING_FOR_ENERGY_DOWN_HOLD",
            warning=(
                "Holding charger output while low solar or a lower amp target is "
                "confirmed across the configured sustain window."
            ),
        )
        return held

    def _update_device_state(
        self,
        state: dict[str, Any],
        hvac_status: HVACStatus | None,
        now: datetime,
    ) -> tuple[dict[str, Any], DeviceStatus]:
        device_state = deepcopy((state.get("device_status") or {}))
        hvac_running = (
            bool(device_state.get("hvac_running", False))
            if hvac_status is None
            else self._hvac_active(hvac_status)
        )
        hvac_heating = (
            bool(device_state.get("hvac_heating", False))
            if hvac_status is None
            else self._hvac_heating(hvac_status)
        )
        bootstrap_only = not getattr(self, "_device_state_bootstrapped", False)

        def update_flag(name: str, value: bool, change_key: str) -> None:
            had_previous_value = name in device_state
            prev = bool(device_state.get(name, False))
            if bootstrap_only:
                device_state[change_key] = device_state.get(change_key)
            elif not had_previous_value:
                device_state[change_key] = device_state.get(change_key)
            elif value != prev:
                device_state[change_key] = now.isoformat()
            elif change_key not in device_state:
                device_state[change_key] = None
            device_state[name] = value

        update_flag("hvac_running", hvac_running, "hvac_changed_at")
        update_flag("hvac_heating", hvac_heating, "hvac_heating_changed_at")
        self._device_state_bootstrapped = True

        devices = DeviceStatus(
            hvac_running=bool(device_state.get("hvac_running")),
            hvac_changed_at=self._state_datetime(device_state.get("hvac_changed_at")),
            hvac_heating=bool(device_state.get("hvac_heating")),
            hvac_heating_changed_at=self._state_datetime(device_state.get("hvac_heating_changed_at")),
        )
        return device_state, devices

    def _telemetry_from_snapshot(
        self,
        snapshot: TeslaEnergySnapshot | None,
    ) -> EnergyTelemetry:
        if snapshot is None:
            return EnergyTelemetry(
                solar_watts=None,
                house_consumption_watts=None,
                powerwall_soc=None,
                grid_import_watts=None,
                grid_export_watts=None,
                timestamp=None,
            )
        sampled_at = snapshot.sampled_at
        if sampled_at.tzinfo is None:
            sampled_at = sampled_at.replace(tzinfo=timezone.utc).astimezone()
        else:
            sampled_at = sampled_at.astimezone()
        return EnergyTelemetry(
            solar_watts=snapshot.solar_generation_w,
            house_consumption_watts=snapshot.home_consumption_w,
            powerwall_soc=snapshot.powerwall_level_pct,
            grid_import_watts=snapshot.grid_import_w,
            grid_export_watts=snapshot.grid_export_w,
            timestamp=sampled_at,
        )

    def _suppress_post_stop_stale_active(
        self,
        charger: dict[str, Any],
        state: dict[str, Any],
        telemetry: EnergyTelemetry,
    ) -> None:
        if not charger.get("actively_charging"):
            return
        if charger.get("enabled"):
            return
        control = state.get("control") or {}
        if control.get("last_charger_command_type") == "STOP":
            last_stop_at = self._state_datetime(control.get("last_charger_command_at"))
        else:
            last_stop_at = self._state_datetime(control.get("last_stop_command_at"))
        if last_stop_at is None or telemetry.timestamp is None:
            return
        if telemetry.timestamp < last_stop_at + timedelta(seconds=30):
            return
        if telemetry.house_consumption_watts is None:
            return

        setpoint_amps = charger.get("setpoint_amps") or control.get("last_current_target_amps")
        try:
            setpoint_amps = int(setpoint_amps)
        except (TypeError, ValueError):
            return
        expected_ev_watts = float(setpoint_amps) * float(self.charger.voltage) * float(self.charger.phases)
        try:
            baseline_non_ev_watts = float(control.get("last_non_ev_watts") or 0.0)
        except (TypeError, ValueError):
            baseline_non_ev_watts = 0.0
        observed_delta_watts = float(telemetry.house_consumption_watts) - baseline_non_ev_watts

        ev_load_absent = (
            expected_ev_watts >= 100.0
            and (
                float(telemetry.house_consumption_watts) < expected_ev_watts * 0.75
                or observed_delta_watts < expected_ev_watts * 0.35
            )
        )
        if not ev_load_absent:
            return

        charger["actively_charging"] = False
        charger["vehicle_connected"] = bool(charger.get("insert_sensed"))
        charger["power_w"] = 0.0
        charger["status_quality"] = "post_stop_stale_active_suppressed"
        charger["status_inference"] = {
            "source": "tesla_home_load_after_stop",
            "baseline_non_ev_watts": round(baseline_non_ev_watts, 2),
            "observed_delta_watts": round(observed_delta_watts, 2),
            "expected_ev_watts": round(expected_ev_watts, 2),
            "last_stop_at": last_stop_at.isoformat(),
            "telemetry_timestamp": telemetry.timestamp.isoformat(),
        }

    def _apply_tesla_charging_inference(
        self,
        charger: dict[str, Any],
        state: dict[str, Any],
        telemetry: EnergyTelemetry,
        now: datetime,
    ) -> None:
        if charger.get("actively_charging"):
            self._suppress_post_stop_stale_active(charger, state, telemetry)
            return

        control = state.get("control") or {}
        last_start_at = self._state_datetime(control.get("last_start_command_at"))
        if last_start_at is None and control.get("last_charger_command_type") == "START":
            last_start_at = self._state_datetime(control.get("last_charger_command_at"))
        inferred_switch_missing = charger.get("status_quality") == "degraded_missing_switch"
        offered_output = bool(
            charger.get("enabled")
            and (
                charger.get("vehicle_connected")
                or charger.get("insert_sensed")
                or charger.get("state") in {
                    "charger_insert",
                    "charger_wait_car",
                    "charger_waiting",
                    "charger_prepare",
                }
            )
        )
        recent_start_command = (
            last_start_at is not None
            and (now - last_start_at).total_seconds() <= START_INFERENCE_WINDOW_SECONDS
        )
        if not inferred_switch_missing and not offered_output and not recent_start_command:
            return

        if last_start_at is None or telemetry.timestamp is None:
            return
        if telemetry.timestamp < last_start_at + timedelta(seconds=30):
            return
        if telemetry.house_consumption_watts is None:
            return

        try:
            baseline_non_ev_watts = float(control.get("last_non_ev_watts") or 0.0)
        except (TypeError, ValueError):
            baseline_non_ev_watts = 0.0
        setpoint_amps = (
            charger.get("setpoint_amps")
            or control.get("last_start_setpoint_amps")
            or control.get("last_current_target_amps")
        )
        try:
            setpoint_amps = int(setpoint_amps)
        except (TypeError, ValueError):
            return
        expected_ev_watts = float(setpoint_amps) * float(self.charger.voltage) * float(self.charger.phases)
        observed_delta_watts = float(telemetry.house_consumption_watts) - baseline_non_ev_watts
        if expected_ev_watts < 100.0 or observed_delta_watts < max(800.0, expected_ev_watts * 0.55):
            return

        charger["actively_charging"] = True
        charger["vehicle_connected"] = True
        charger["insert_sensed"] = True
        charger["enabled"] = True
        charger["power_w"] = max(float(charger.get("power_w") or 0.0), expected_ev_watts)
        charger["status_quality"] = "inferred_charging_from_tesla"
        charger["status_inference"] = {
            "source": "tesla_home_load_after_start",
            "baseline_non_ev_watts": round(baseline_non_ev_watts, 2),
            "observed_delta_watts": round(observed_delta_watts, 2),
            "expected_ev_watts": round(expected_ev_watts, 2),
            "last_start_at": last_start_at.isoformat(),
            "telemetry_timestamp": telemetry.timestamp.isoformat(),
            "offered_output": offered_output,
            "switch_missing": inferred_switch_missing,
            "recent_start_command": recent_start_command,
        }

    def _fetch_charger_snapshot(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        started_at = current_local_time()
        started_monotonic = time.monotonic()
        raw_status = self.charger.status()
        duration_ms = int(round((time.monotonic() - started_monotonic) * 1000))
        charger = self.charger.summarize_status(raw_status)
        fetch_info = {
            "started_at": started_at.isoformat(),
            "duration_ms": duration_ms,
        }
        if raw_status.get("_cached_merge"):
            fetch_info["used_cached_merge"] = True
            fetch_info["cache_age_seconds"] = raw_status.get("_cache_age_seconds")
            fetch_info["partial_dps_keys"] = raw_status.get("_partial_dps_keys")
        if raw_status.get("_degraded_status"):
            fetch_info["degraded_status"] = True
            fetch_info["missing_required_dps"] = raw_status.get("_missing_required_dps")
        return raw_status, charger, fetch_info

    def _normalize_state(self, state: dict[str, Any], now: datetime) -> dict[str, Any]:
        normalized = deepcopy(default_automation_state())
        for section in (
            "emergency",
            "night",
            "startup",
            "control",
            "solar_hysteresis",
            "session",
            "device_status",
            "decision",
        ):
            if isinstance(state.get(section), dict):
                normalized[section].update(state[section])

        current_night_key = self.night_session_key(now)
        existing_night_key = normalized["night"].get("session_key")
        existing_seen_charging = bool(normalized["night"].get("seen_charging"))
        if current_night_key is None:
            if existing_seen_charging and existing_night_key:
                normalized["night"]["session_key"] = existing_night_key
                return normalized
            normalized["night"]["session_key"] = None
            normalized["night"]["seen_charging"] = False
            normalized["night"]["last_seen_charging_at"] = None
        elif normalized["night"].get("session_key") != current_night_key:
            normalized["night"]["session_key"] = current_night_key
            normalized["night"]["seen_charging"] = False
            normalized["night"]["last_seen_charging_at"] = None

        return normalized

    def _sync_charging_session(
        self,
        charger: dict[str, Any],
        decision: dict[str, Any],
        now: datetime,
    ) -> None:
        session = decision["state_after"]["session"]
        actual_amps = estimate_actual_amps(
            float(charger.get("power_w") or 0),
            self.charger.voltage,
            self.charger.phases,
        )

        if charger["actively_charging"]:
            if not session.get("active"):
                session.update(
                    {
                        "active": True,
                        "db_id": None,
                        "started_at": now.isoformat(),
                        "last_seen_at": now.isoformat(),
                        "energy_wh": 0.0,
                        "max_power_w": float(charger.get("power_w") or 0.0),
                        "max_actual_amps": float(actual_amps or 0.0),
                        "start_phase": decision.get("base_phase") or decision.get("phase"),
                        "start_reason": decision.get("reason"),
                        "target_amps": decision.get("target_amps"),
                        "start_setpoint_amps": charger.get("setpoint_amps"),
                        "max_setpoint_amps": charger.get("setpoint_amps"),
                        "db_error": None,
                    }
                )
            else:
                last_seen_raw = session.get("last_seen_at")
                if last_seen_raw:
                    try:
                        last_seen_at = datetime.fromisoformat(str(last_seen_raw))
                        elapsed_hours = max(
                            0.0,
                            (now - last_seen_at).total_seconds() / 3600.0,
                        )
                        session["energy_wh"] = round(
                            float(session.get("energy_wh", 0.0))
                            + elapsed_hours * float(charger.get("power_w") or 0.0),
                            3,
                        )
                    except ValueError:
                        pass
                session["last_seen_at"] = now.isoformat()
                session["max_power_w"] = max(
                    float(session.get("max_power_w", 0.0)),
                    float(charger.get("power_w") or 0.0),
                )
                session["max_actual_amps"] = max(
                    float(session.get("max_actual_amps", 0.0)),
                    float(actual_amps or 0.0),
                )
                current_setpoint = charger.get("setpoint_amps")
                session["max_setpoint_amps"] = max(
                    int(session.get("max_setpoint_amps") or 0),
                    int(current_setpoint or 0),
                )

            if not session.get("db_id"):
                db_id = self.session_store.start_session(
                    session,
                    now.tzname() or "local",
                )
                session["db_id"] = db_id
                session["db_error"] = self.session_store.last_error
            elif self.session_store.update_session(session, charger):
                session["db_error"] = None
            else:
                session["db_error"] = self.session_store.last_error
            return

        if not session.get("active"):
            session["db_error"] = self.session_store.last_error
            return

        ended_at = now
        last_seen_raw = session.get("last_seen_at")
        if last_seen_raw:
            try:
                ended_at = datetime.fromisoformat(str(last_seen_raw))
            except ValueError:
                ended_at = now

        if self.session_store.finish_session(session, charger, decision, ended_at):
            session["db_error"] = None
        else:
            session["db_error"] = self.session_store.last_error

        decision["state_after"]["session"] = {
            "active": False,
            "db_id": None,
            "started_at": None,
            "last_seen_at": None,
            "energy_wh": 0.0,
            "max_power_w": 0.0,
            "max_actual_amps": 0.0,
            "start_phase": None,
            "start_reason": None,
            "target_amps": None,
            "start_setpoint_amps": None,
            "max_setpoint_amps": None,
            "db_error": self.session_store.last_error,
        }

    def _startup_remaining_seconds(self, state: dict[str, Any], now: datetime) -> int:
        started_at_raw = state.get("startup", {}).get("started_at")
        if not started_at_raw:
            return 0
        try:
            started_at = datetime.fromisoformat(str(started_at_raw))
        except ValueError:
            return 0
        remaining = self.startup_grace_seconds - int((now - started_at).total_seconds())
        return max(0, remaining)

    def _seconds_since_last_charge_seen(self, state: dict[str, Any], now: datetime) -> int | None:
        last_seen_raw = state.get("session", {}).get("last_seen_at")
        if not last_seen_raw:
            return None
        try:
            last_seen_at = datetime.fromisoformat(str(last_seen_raw))
        except ValueError:
            return None
        return max(0, int((now - last_seen_at).total_seconds()))

    @staticmethod
    def _hvac_active(status: HVACStatus | None) -> bool:
        if status is None:
            return False
        return status.is_running and status.is_cooling

    @staticmethod
    def _hvac_heating(status: HVACStatus | None) -> bool:
        if status is None:
            return False
        return status.is_running and status.is_heating

    @staticmethod
    def _clear_startup_state() -> dict[str, Any]:
        return {
            "active": False,
            "started_at": None,
            "phase": None,
            "target_amps": None,
            "blocked_until": None,
            "blocked_phase": None,
            "blocked_reason": None,
        }

    def _startup_retry_block_seconds(
        self,
        state: dict[str, Any],
        phase: str | None,
        now: datetime,
    ) -> int | None:
        startup = state.get("startup") or {}
        if startup.get("blocked_phase") != phase:
            return None
        blocked_until_raw = startup.get("blocked_until")
        if not blocked_until_raw:
            return None
        try:
            blocked_until = datetime.fromisoformat(str(blocked_until_raw))
        except ValueError:
            return None
        remaining = int((blocked_until - now).total_seconds())
        return max(0, remaining) if remaining > 0 else None

    def _startup_failed_state(
        self,
        phase: str | None,
        target_amps: int | None,
        now: datetime,
    ) -> dict[str, Any]:
        blocked_until = None
        if self.startup_failure_cooldown_seconds > 0:
            blocked_until = (
                now + timedelta(seconds=self.startup_failure_cooldown_seconds)
            ).isoformat()
        return {
            "active": False,
            "started_at": None,
            "phase": phase,
            "target_amps": target_amps,
            "blocked_until": blocked_until,
            "blocked_phase": phase if blocked_until else None,
            "blocked_reason": "startup_grace_expired" if blocked_until else None,
        }

    @staticmethod
    def _state_datetime(raw_value: Any) -> datetime | None:
        if not raw_value:
            return None
        try:
            return datetime.fromisoformat(str(raw_value))
        except ValueError:
            return None

    def _current_change_wait_seconds(self, state: dict[str, Any], now: datetime) -> int:
        if self.min_current_change_interval_seconds <= 0:
            return 0
        last_changed_at = self._state_datetime(
            (state.get("control") or {}).get("last_current_change_at")
        )
        if last_changed_at is None:
            return 0
        elapsed = int((now - last_changed_at).total_seconds())
        return max(0, self.min_current_change_interval_seconds - elapsed)

    def _fault_reset_wait_seconds(self, state: dict[str, Any], now: datetime) -> int:
        if self.fault_reset_cooldown_seconds <= 0:
            return 0
        last_reset_at = self._state_datetime(
            (state.get("control") or {}).get("last_fault_reset_at")
        )
        if last_reset_at is None:
            return 0
        elapsed = int((now - last_reset_at).total_seconds())
        return max(0, self.fault_reset_cooldown_seconds - elapsed)

    def _suspected_fault_or_interlock(self, decision: dict[str, Any]) -> bool:
        charger = decision.get("charger") or {}
        state_text = f"{charger.get('state', '')} {charger.get('pilot_state', '')}".lower()
        if any(token in state_text for token in ("fault", "error", "alarm", "alert")):
            return True
        raw_dps = charger.get("raw_dps") or {}
        try:
            fault_bitmap = int(raw_dps.get("10") or 0)
        except (TypeError, ValueError):
            fault_bitmap = 0
        if fault_bitmap != 0:
            return True
        if charger.get("actively_charging"):
            return False
        if charger.get("state") == "charger_free" and charger.get("pilot_state") == "controlpi_12v":
            return False
        # Tuya Cloud identifies DP 10 as the fault bitmap. DP 6 is phase telemetry,
        # DP 33 is mode_set, and DP 108 is charge_record, so those raw payloads are
        # recorded for diagnosis but are not fault triggers by themselves.
        return False

    def _record_charger_event(
        self,
        name: str,
        decision: dict[str, Any],
        now: datetime,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self.log_charger_events:
            return
        charger = decision.get("charger") or {}
        payload = {
            "timestamp": now.isoformat(),
            "event": name,
            "phase": decision.get("phase"),
            "action": decision.get("action"),
            "reason": decision.get("reason"),
            "target_amps": decision.get("target_amps"),
            "charger": {k: v for k, v in charger.items() if k != "raw_dps"},
            "charger_dps": charger.get("raw_dps"),
        }
        if extra:
            payload.update(extra)
        print(
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} charger_event "
            f"event={name} reason={decision.get('reason')} "
            f"state={charger.get('state')} enabled={charger.get('enabled')} "
            f"setpoint={charger.get('setpoint_amps')} target={decision.get('target_amps')} "
            f"dps={json.dumps(charger.get('raw_dps'), separators=(',', ':'))}",
            flush=True,
        )
        try:
            self.charger_event_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.charger_event_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except OSError:
            return

    def _mark_current_change(
        self,
        decision: dict[str, Any],
        now: datetime,
        target_amps: int,
    ) -> None:
        control = decision["state_after"].setdefault("control", {})
        control["last_current_change_at"] = now.isoformat()
        control["last_current_target_amps"] = target_amps

    def _attempt_fault_reset(
        self,
        decision: dict[str, Any],
        now: datetime,
        dry_run: bool,
    ) -> bool:
        if not self.fault_reset_enabled:
            return False
        if not self._suspected_fault_or_interlock(decision):
            return False

        wait_seconds = self._fault_reset_wait_seconds(decision["state_after"], now)
        decision["fault_reset_wait_seconds"] = wait_seconds
        if wait_seconds > 0:
            return False

        self._record_charger_event("fault_reset_attempt", decision, now)
        if not dry_run:
            self.charger.set_enabled(False)
            if decision["charger"].get("setpoint_amps") != self.automation_min_amps:
                self.charger.set_amps(self.automation_min_amps)
                self._mark_current_change(decision, now, self.automation_min_amps)
            if (
                self.auto_force_charge_now_mode
                and decision["charger"].get("mode") != self.charge_now_mode
            ):
                self.charger.set_mode(self.charge_now_mode)
            if self.fault_reset_off_seconds > 0:
                time.sleep(self.fault_reset_off_seconds)
            self.charger.set_enabled(True)

        control = decision["state_after"].setdefault("control", {})
        control["last_fault_reset_at"] = now.isoformat()
        control["last_fault_reset_reason"] = str(decision.get("reason"))
        decision["fault_reset_attempted"] = True
        return True

    def _apply_automation_minimum(self, decision: dict[str, Any]) -> None:
        if decision.get("action") != "enable":
            return
        if decision.get("base_phase") == "emergency":
            return
        target_amps = decision.get("target_amps")
        if target_amps is None:
            return
        safe_target = max(self.automation_min_amps, int(target_amps))
        if safe_target != int(target_amps):
            decision["target_amps"] = safe_target
            decision["automation_min_amps_applied"] = self.automation_min_amps

    def _apply_shared_limit(
        self,
        decision: dict[str, Any],
        limit_amps: int,
        reason: str,
    ) -> bool:
        if decision.get("action") != "enable":
            return False
        if decision.get("base_phase") == "emergency":
            return False
        target_amps = decision.get("target_amps")
        if target_amps is None:
            return False
        shared_limit = max(self.automation_min_amps, int(limit_amps))
        if int(target_amps) > shared_limit:
            decision["target_amps"] = shared_limit
            energy = decision.get("energy")
            if isinstance(energy, dict):
                energy["desired_amps"] = shared_limit
                energy["reason"] = reason
            decision["reason"] = reason
            return True
        return False

    def _apply_shared_load_policy(
        self,
        decision: dict[str, Any],
        hvac_status: HVACStatus | None,
    ) -> None:
        hvac_active = self._hvac_active(hvac_status)

        if decision.get("action") != "enable":
            return
        if decision.get("base_phase") == "emergency":
            return
        if self.tesla_solar_control_enabled and decision.get("base_phase") == "day_solar":
            return

        if hvac_active:
            self._apply_shared_limit(
                decision,
                limit_amps=self.charger.min_amps,
                reason="hvac_shared_limit",
            )

    def _apply_solar_hysteresis(
        self,
        decision: dict[str, Any],
        state: dict[str, Any],
        now: datetime,
        hvac_status: "HVACStatus | None" = None,
    ) -> None:
        """Suppress small solar fluctuations and require sustained changes before adjusting amps.

        Rules:
        - If new target is within ±threshold_amps of current setpoint → ignore, keep current.
        - If new target is a significant drop   → wait sustain_down_seconds before applying.
        - If new target is a significant increase → wait sustain_up_seconds before applying.
        Once a pending change has been consistent long enough it is applied on the next cycle
        where the interval timer also allows it.

        Bypass: if a known sustained load is active (HVAC running), the change is real —
        not a transient — so apply immediately in either direction.
        """
        if self.tesla_solar_change_threshold_amps <= 0:
            decision["state_after"]["solar_hysteresis"] = {"pending_amps": None, "pending_since": None}
            return
        # Incremental ±1A mode: changes are already throttled by the change interval timer.
        # Applying the threshold check here would suppress every ±1A step (< 2A default).
        if decision.get("solar_incremental"):
            decision["state_after"]["solar_hysteresis"] = {"pending_amps": None, "pending_since": None}
            return
        if decision.get("action") != "enable":
            decision["state_after"]["solar_hysteresis"] = {"pending_amps": None, "pending_since": None}
            return
        if not (decision.get("tesla_solar") or {}).get("enabled"):
            decision["state_after"]["solar_hysteresis"] = {"pending_amps": None, "pending_since": None}
            return

        raw_target = decision.get("target_amps")
        if raw_target is None:
            decision["state_after"]["solar_hysteresis"] = {"pending_amps": None, "pending_since": None}
            return

        charger = decision.get("charger") or {}
        current_setpoint = charger.get("setpoint_amps")
        if current_setpoint is None:
            decision["state_after"]["solar_hysteresis"] = {"pending_amps": None, "pending_since": None}
            return

        threshold = self.tesla_solar_change_threshold_amps
        raw_target = int(raw_target)
        current_setpoint = int(current_setpoint)
        delta = raw_target - current_setpoint  # positive = boost, negative = cut

        # Detect a known sustained load: HVAC actively running. This is a real,
        # sustained load — not a transient — so bypass the sustain timer entirely
        # and let the amp change apply immediately in either direction.
        hvac_active = hvac_status is not None and hvac_status.is_running
        known_load_active = hvac_active

        hy = state.get("solar_hysteresis") or {}
        pending_amps = hy.get("pending_amps")
        pending_since_str = hy.get("pending_since")
        pending_since = self._state_datetime(hy.get("pending_since"))

        # Within threshold: treat as noise, clear any pending change.
        if abs(delta) <= threshold:
            decision["target_amps"] = current_setpoint
            decision["state_after"]["solar_hysteresis"] = {"pending_amps": None, "pending_since": None}
            return

        # Known sustained load active — this is real, not a transient. Apply immediately.
        if known_load_active:
            decision["state_after"]["solar_hysteresis"] = {"pending_amps": None, "pending_since": None}
            decision["solar_known_load_bypass"] = {
                "hvac_active": hvac_active,
            }
            return

        # Significant change. Choose the right sustain window.
        sustain = (
            self.tesla_solar_change_sustain_up_seconds
            if delta > 0
            else self.tesla_solar_change_sustain_down_seconds
        )

        # Check whether the current raw_target is in the same direction as what is pending.
        pending_matches = (
            pending_amps is not None
            and abs(raw_target - int(pending_amps)) <= threshold
        )

        if pending_matches and pending_since is not None:
            elapsed = (now - pending_since).total_seconds()
            if elapsed >= sustain:
                # Sustained long enough — let the change through.
                decision["state_after"]["solar_hysteresis"] = {"pending_amps": None, "pending_since": None}
                # decision["target_amps"] is already raw_target; apply_decision will do set_amps.
                return
            # Still accumulating — hold current setpoint, keep pending state alive.
            decision["target_amps"] = current_setpoint
            decision["state_after"]["solar_hysteresis"] = {
                "pending_amps": raw_target,          # refresh to latest value in same direction
                "pending_since": pending_since_str,  # preserve original start time
            }
            decision["solar_change_pending"] = {
                "pending_amps": raw_target,
                "seconds_elapsed": int(elapsed),
                "seconds_remaining": int(sustain - elapsed),
                "direction": "up" if delta > 0 else "down",
            }
            decision["reason"] = "solar_change_pending"
            return

        # New direction or first significant change — start the timer, hold current.
        decision["target_amps"] = current_setpoint
        decision["state_after"]["solar_hysteresis"] = {
            "pending_amps": raw_target,
            "pending_since": now.isoformat(),
        }
        decision["solar_change_pending"] = {
            "pending_amps": raw_target,
            "seconds_elapsed": 0,
            "seconds_remaining": int(sustain),
            "direction": "up" if delta > 0 else "down",
        }
        decision["reason"] = "solar_change_pending"

    def _apply_summer_tesla_solar_policy(
        self,
        decision: dict[str, Any],
        charger: dict[str, Any],
        hvac_status: HVACStatus | None,
        now: datetime,
    ) -> None:
        if not self.tesla_solar_control_enabled:
            return
        if decision.get("action") != "enable":
            return
        if decision.get("base_phase") != "day_solar":
            return

        snapshot, error = self.tesla_latest_store.latest_sample()
        if snapshot is None:
            decision["action"] = "disable"
            decision["target_mode"] = None
            decision["target_amps"] = None
            decision["reason"] = "tesla_solar_data_unavailable"
            decision["tesla_solar"] = {"enabled": True, "error": error}
            return

        age_seconds = snapshot.age_seconds(now)
        raw_solar_generation_w = float(snapshot.solar_generation_w or 0.0)
        solar_generation_w = raw_solar_generation_w
        if self.tesla_solar_generation_cap_watts > 0.0:
            solar_generation_w = min(solar_generation_w, self.tesla_solar_generation_cap_watts)
        home_consumption_w = float(snapshot.home_consumption_w or 0.0)
        grid_import_w = float(snapshot.grid_import_w or 0.0)
        grid_export_w = float(snapshot.grid_export_w or 0.0)
        watts_per_amp = max(1.0, self.charger.voltage * self.charger.phases)
        charger_power_w = float(charger.get("power_w") or 0.0)
        charger_load_source = "power_w"
        if charger.get("actively_charging") and charger_power_w < 100.0:
            estimated_amps = charger.get("setpoint_amps") or self.automation_min_amps
            charger_power_w = float(estimated_amps) * watts_per_amp
            charger_load_source = "estimated_from_setpoint"
        elif not charger.get("actively_charging"):
            charger_power_w = 0.0
            charger_load_source = "not_charging"
        non_ev_home_w = max(0.0, home_consumption_w - charger_power_w)
        hvac_active = self._hvac_active(hvac_status)
        # Tesla home consumption already includes HVAC and EV load.
        # Subtract only active EV load so the remaining load is true non-EV home use.
        controlled_load_w = non_ev_home_w
        solar_surplus_w = solar_generation_w - controlled_load_w
        if solar_generation_w <= 0.0 and home_consumption_w <= 0.0:
            solar_surplus_w = charger_power_w + grid_export_w - grid_import_w
        usable_solar_after_home_w = max(0.0, solar_surplus_w)
        powerwall_pct = snapshot.powerwall_level_pct
        now_minutes = now.hour * 60 + now.minute
        before_no_charge = now_minutes < self.no_charge_start_minutes
        powerwall_full_boost = (
            powerwall_pct is not None
            and float(powerwall_pct) >= self.tesla_solar_powerwall_full_boost_pct
        )
        powerwall_above_reserve = (
            powerwall_pct is not None
            and float(powerwall_pct) >= self.tesla_solar_powerwall_reserve_pct
        )
        # --- Incremental margin-based control ---
        # margin = solar_generation - home_consumption (home already includes EV load)
        # Target: keep margin >= trim_threshold (200W) at all times.
        # If margin > boost_threshold (1000W): add 1A — we have headroom.
        # If margin < trim_threshold (200W):  cut 1A — too close to grid import.
        # Otherwise: hold current setpoint.
        # 1A = watts_per_amp (230W at 230V 1-phase).
        solar_margin_w = solar_generation_w - home_consumption_w
        current_setpoint_amps = int(charger.get("setpoint_amps") or self.automation_min_amps)

        if solar_margin_w > self.tesla_solar_margin_boost_watts:
            policy_target_amps = min(current_setpoint_amps + 1, self.charger.max_amps)
            incremental_direction = "boost"
        elif solar_margin_w < self.tesla_solar_margin_trim_watts:
            policy_target_amps = max(current_setpoint_amps - 1, self.automation_min_amps)
            incremental_direction = "trim"
        else:
            policy_target_amps = current_setpoint_amps
            incremental_direction = "hold"

        # Can solar sustain even min_amps without violating the trim threshold?
        # gap_at_min = solar - non_ev_home - (min_amps * V)
        # If gap_at_min < trim_threshold, solar is too low for minimum charging.
        gap_at_min_amps_w = (
            solar_generation_w
            - non_ev_home_w
            - (self.automation_min_amps * watts_per_amp)
        )
        insufficient_solar = gap_at_min_amps_w < self.tesla_solar_margin_trim_watts

        # Legacy reference values kept for dashboard display.
        effective_reserve_watts = 0.0 if powerwall_full_boost else self.tesla_solar_reserve_watts
        available_ev_w = solar_surplus_w - effective_reserve_watts
        solar_target_amps = int(available_ev_w // watts_per_amp)

        cloudy_min_allowed = (
            self.tesla_solar_cloudy_min_amps > 0
            and before_no_charge
        )
        grid_import_exceeds_stop = grid_import_w > self.tesla_solar_grid_import_stop_watts

        decision["tesla_solar"] = {
            "enabled": True,
            **snapshot.to_dict(now),
            "max_sample_age_seconds": self.tesla_solar_max_sample_age_seconds,
            "solar_generation_cap_watts": self.tesla_solar_generation_cap_watts,
            "capped_solar_generation_w": round(solar_generation_w, 2),
            "reserve_watts": self.tesla_solar_reserve_watts,
            "effective_reserve_watts": effective_reserve_watts,
            "cloudy_min_amps": self.tesla_solar_cloudy_min_amps,
            "cloudy_min_allowed": cloudy_min_allowed,
            "powerwall_full_boost_pct": self.tesla_solar_powerwall_full_boost_pct,
            "powerwall_full_boost": powerwall_full_boost,
            "powerwall_above_reserve": powerwall_above_reserve,
            "charger_load_w": round(charger_power_w, 2),
            "charger_load_source": charger_load_source,
            "non_ev_home_w": round(non_ev_home_w, 2),
            "hvac_active": hvac_active,
            "hvac_included_in_tesla_home": True,
            "controlled_load_w": round(controlled_load_w, 2),
            "usable_solar_after_home_w": round(usable_solar_after_home_w, 2),
            "solar_surplus_w": round(solar_surplus_w, 2),
            "available_ev_w": round(available_ev_w, 2),
            "calculated_amps": solar_target_amps,
            "grid_import_exceeds_stop": grid_import_exceeds_stop,
            "policy_target_amps": policy_target_amps,
            # Incremental margin control fields
            "solar_margin_w": round(solar_margin_w, 2),
            "margin_boost_threshold_w": self.tesla_solar_margin_boost_watts,
            "margin_trim_threshold_w": self.tesla_solar_margin_trim_watts,
            "incremental_direction": incremental_direction,
            "gap_at_min_amps_w": round(gap_at_min_amps_w, 2),
            "insufficient_solar": insufficient_solar,
            "watts_per_amp": watts_per_amp,
        }

        if age_seconds > self.tesla_solar_max_sample_age_seconds:
            decision["action"] = "disable"
            decision["target_mode"] = None
            decision["target_amps"] = None
            decision["reason"] = "tesla_solar_data_stale"
            return

        # Solar too low to sustain even min charge without violating margin — stop charging.
        if insufficient_solar:
            if cloudy_min_allowed:
                decision["target_amps"] = self.tesla_solar_cloudy_min_amps
                decision["reason"] = "cloudy_minimum_low_solar"
                decision["solar_incremental"] = True
                return
            decision["action"] = "disable"
            decision["target_mode"] = None
            decision["target_amps"] = None
            decision["reason"] = "insufficient_solar_for_min_charging"
            return

        # Apply the incremental ±1A target.
        # Mark as incremental so hysteresis threshold is bypassed (±1A < default 2A threshold).
        desired_amps = max(
            self.automation_min_amps,
            min(policy_target_amps, self.charger.max_amps),
        )
        decision["target_amps"] = desired_amps
        decision["solar_incremental"] = True
        if powerwall_full_boost:
            decision["reason"] = f"solar_margin_{incremental_direction}_pw_full"
        elif powerwall_above_reserve:
            decision["reason"] = f"solar_margin_{incremental_direction}_pw_reserve"
        else:
            decision["reason"] = f"solar_margin_{incremental_direction}"

    def plan(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or current_local_time()
        raw_status, charger, fetch_info = self._fetch_charger_snapshot()
        state = self._normalize_state(load_automation_state(self.state_path), now)
        hvac_status, hvac_error = self._load_hvac()
        state_after = deepcopy(state)
        device_state, device_status = self._update_device_state(
            state_after,
            hvac_status,
            now,
        )
        snapshot, tesla_error = self.tesla_latest_store.latest_sample()
        telemetry = self._telemetry_from_snapshot(snapshot)
        charger_before_inference = deepcopy(charger)
        self._apply_tesla_charging_inference(charger, state_after, telemetry, now)
        device_state = self._clear_absorbed_predictive_timestamps(device_state, telemetry)
        state_after["device_status"] = device_state
        device_status = DeviceStatus(
            hvac_running=bool(device_state.get("hvac_running")),
            hvac_changed_at=self._state_datetime(device_state.get("hvac_changed_at")),
            hvac_heating=bool(device_state.get("hvac_heating")),
            hvac_heating_changed_at=self._state_datetime(device_state.get("hvac_heating_changed_at")),
        )

        night_insert_detected = bool(
            charger.get("vehicle_connected")
            or charger.get("insert_sensed")
            or charger.get("state") in {
                "charger_insert",
                "charger_wait_car",
                "charger_waiting",
                "charger_prepare",
            }
        )
        emergency_state = deepcopy(state_after.get("emergency") or {})
        emergency_state = self._track_emergency_energy_target(state_after, emergency_state, charger, now)
        emergency_expires_at = self._state_datetime(emergency_state.get("expires_at"))
        if (
            emergency_state.get("active")
            and emergency_expires_at is not None
            and now >= emergency_expires_at
        ):
            # Emergency window elapsed: clear it cleanly and let normal solar
            # control resume. Do NOT raise a hold-until-disconnect force stop
            # here (that would block charging until the car is unplugged).
            self._clear_emergency_state(state_after)
            emergency_state = deepcopy(state_after.get("emergency") or {})
        else:
            emergency_end_reason = self._emergency_session_end_reason(
                emergency_state,
                charger,
                night_insert_detected,
            )
            if emergency_end_reason:
                self._clear_emergency_state(state_after)
                self._request_force_stop(state_after, now, emergency_end_reason)
                emergency_state = deepcopy(state_after.get("emergency") or {})

        if (
            state_after.get("night", {}).get("start_blocked_until_disconnect")
            and not night_insert_detected
        ):
            # Vehicle unplugged: re-arm night auto-charging for the next plug-in.
            state_after["night"]["start_blocked_until_disconnect"] = False

        force_stop_active = self._force_stop_active(state_after, night_insert_detected)
        energy_cfg_dict = self.energy_config.to_dict()
        emergency_cfg = start_emergency_mode(
            energy_cfg_dict,
            amps=emergency_state.get("requested_amps"),
            duration_minutes=emergency_state.get("duration_minutes"),
            now=self._state_datetime(emergency_state.get("started_at")) or now,
        ) if emergency_state.get("active") else stop_emergency_mode(energy_cfg_dict, now=now)
        if emergency_state.get("expires_at"):
            emergency_cfg["emergency_mode_expires_at"] = emergency_state.get("expires_at")
            emergency_cfg["emergency_mode_duration_minutes"] = emergency_state.get("duration_minutes")
            emergency_cfg["emergency_charging_enabled"] = bool(emergency_state.get("active"))
        runtime_energy_cfg = make_energy_config(
            emergency_cfg,
            charger_cfg=self.cfg.get("charger"),
            legacy_profile=self.active_profile_cfg,
        )

        control_state = state_after.get("control") or {}
        if (
            self.night_session_key(now) is None
            and state_after.get("night", {}).get("seen_charging")
            and not charger.get("actively_charging")
        ):
            state_after["night"]["seen_charging"] = False
            state_after["night"]["last_seen_charging_at"] = None
        night_last_seen_charging_at = self._state_datetime(
            state_after.get("night", {}).get("last_seen_charging_at")
        )
        night_recently_seen_charging = (
            night_last_seen_charging_at is not None
            and (now - night_last_seen_charging_at).total_seconds()
            <= max(self.night_completion_grace_seconds, self.poll_seconds + 5)
        )
        actual_current_amps = estimate_actual_amps(
            float(charger.get("power_w") or 0.0),
            runtime_energy_cfg.ev_voltage,
            self.charger.phases,
        )
        raw_setpoint_history = control_state.get("setpoint_history") or []
        setpoint_history: list = []
        for _entry in raw_setpoint_history:
            try:
                setpoint_history.append((datetime.fromisoformat(str(_entry[0])), int(_entry[1])))
            except (IndexError, ValueError, TypeError):
                pass
        # Seed with current setpoint if history is empty so _setpoint_at_time always finds
        # an entry that predates any Tesla snapshot (snapshots are at most ~9 min old).
        # Without this, the fallback to current_setpoint_amps causes cascading step-downs:
        # each new command lowers the fallback, inflates non_ev, and triggers another step-down
        # — all on the same stale Tesla snapshot.
        if not setpoint_history:
            initial_setpt = charger.get("setpoint_amps")
            if initial_setpt is not None:
                setpoint_history.append((now - timedelta(hours=1), int(initial_setpt)))

        prior_session_active = bool(state.get("session", {}).get("active", False))
        ev_active_since = None
        if charger.get("actively_charging"):
            if prior_session_active:
                ev_active_since = self._state_datetime((state.get("session") or {}).get("started_at"))
            last_command_at = self._state_datetime(control_state.get("last_charger_command_at"))
            last_start_at = self._state_datetime(control_state.get("last_start_command_at"))
            if last_start_at is None and control_state.get("last_charger_command_type") == "START":
                last_start_at = last_command_at
            if last_start_at is not None and (ev_active_since is None or last_start_at > ev_active_since):
                ev_active_since = last_start_at
            ev_active_since = ev_active_since or now

        charger_state = ChargerState(
            is_enabled=bool(charger.get("enabled")),
            is_charging=bool(charger.get("actively_charging")),
            vehicle_connected=night_insert_detected,
            night_session_active=bool(
                state_after.get("night", {}).get("seen_charging")
                and state_after.get("night", {}).get("session_key")
                and night_recently_seen_charging
            ),
            night_charge_blocked=bool(
                state_after.get("night", {}).get("start_blocked_until_disconnect")
            ),
            current_amps=actual_current_amps,
            current_setpoint_amps=charger.get("setpoint_amps"),
            voltage=runtime_energy_cfg.ev_voltage,
            ev_min_amps=max(ABSOLUTE_MIN_AMPS, runtime_energy_cfg.ev_min_amps),
            solar_max_amps=min(ABSOLUTE_MAX_AMPS, runtime_energy_cfg.ev_solar_max_amps),
            hard_max_amps=min(ABSOLUTE_MAX_AMPS, runtime_energy_cfg.ev_hard_max_amps),
            emergency_charge_amps=min(ABSOLUTE_MAX_AMPS, runtime_energy_cfg.emergency_charge_amps),
            last_charger_command_at=self._state_datetime(control_state.get("last_charger_command_at")),
            last_charger_command_type=control_state.get("last_charger_command_type"),
            setpoint_history=setpoint_history,
            ev_active_since=ev_active_since,
            actual_power_w=float(charger.get("power_w") or 0.0),
        )
        low_solar_stop_counter = int(control_state.get("low_solar_stop_counter") or 0)
        vehicle_present = bool(
            charger.get("actively_charging")
            or charger.get("vehicle_connected")
            or charger.get("insert_sensed")
        )
        vehicle_previously_present = bool(control_state.get("vehicle_present"))
        state_after.setdefault("control", {})["vehicle_present"] = vehicle_present
        # Edge-triggered: a car sitting plugged in without an active session is
        # not a "new" connection on every loop. Only the present transition
        # bypasses the low-solar grace counter.
        new_vehicle_connection = (
            not prior_session_active
            and vehicle_present
            and not vehicle_previously_present
        )
        if new_vehicle_connection:
            low_solar_stop_counter = runtime_energy_cfg.low_solar_stop_grace_loop_count + 1
        energy_decision = decide_energy_action(
            telemetry=telemetry,
            devices=device_status,
            charger=charger_state,
            config=runtime_energy_cfg,
            now=now,
            low_solar_stop_counter=low_solar_stop_counter,
        )

        if force_stop_active:
            control = state_after.setdefault("control", {})
            control["energy_down_pending_target"] = None
            control["energy_down_pending_since"] = None
            energy_decision = replace(
                energy_decision,
                desired_charger_enabled=False,
                desired_amps=None,
                reason=str(control.get("force_stop_reason") or "force_stop_requested"),
                mode="FORCED_STOP",
                action_status="SAFETY_STOP",
                next_low_solar_stop_counter=0,
                warning="Charging held off after emergency mode ended or was stopped.",
            )

        if (
            hvac_error
            and not self.allow_when_hvac_unavailable
            and energy_decision.mode not in {"NIGHT_CHARGING", "NIGHT_WAITING", "NO_CHARGE_WINDOW", "EMERGENCY_CHARGING", "FORCED_STOP"}
        ):
            energy_decision = decide_energy_action(
                telemetry=EnergyTelemetry(None, None, None, None, None, None),
                devices=device_status,
                charger=charger_state,
                config=runtime_energy_cfg,
                now=now,
            )

        last_self_start_at = self._state_datetime(control_state.get("last_start_command_at"))
        if last_self_start_at is None and control_state.get("last_charger_command_type") == "START":
            last_self_start_at = self._state_datetime(control_state.get("last_charger_command_at"))
        recent_self_start = (
            last_self_start_at is not None
            and (now - last_self_start_at).total_seconds() <= START_INFERENCE_WINDOW_SECONDS
        )
        # A session we started ourselves moments ago is never "external", so it
        # keeps the energy_down_sustain hold against one-sample load spikes.
        external_low_solar_stop = (
            new_vehicle_connection
            and not recent_self_start
            and bool(charger.get("actively_charging") or charger.get("enabled"))
            and not energy_decision.desired_charger_enabled
            and energy_decision.mode == "LOW_SOLAR_STOP"
        )
        if external_low_solar_stop:
            control = state_after.setdefault("control", {})
            control["energy_down_pending_target"] = None
            control["energy_down_pending_since"] = None
            energy_decision = replace(
                energy_decision,
                reason=f"external_start_{energy_decision.reason}",
                action_status="READY_TO_COMMAND" if energy_decision.command_allowed_now else energy_decision.action_status,
            )
        else:
            energy_decision = self._apply_energy_down_hold(
                energy_decision,
                charger_state,
                state_after,
                now,
            )

        state_after["control"]["low_solar_stop_counter"] = energy_decision.next_low_solar_stop_counter
        state_after["control"]["config_last_applied_at"] = runtime_energy_cfg.updated_at

        # Spike confirmation: when new Tesla data shows unexplained non-EV load jumped
        # significantly, request a fresh Tesla pull before the down-hold expires. If
        # HVAC already explains the load, avoid spending an extra Tesla call.
        telem_ts_str = telemetry.timestamp.isoformat() if telemetry.timestamp else None
        new_telem = telem_ts_str != control_state.get("last_telemetry_timestamp")
        prev_non_ev = float(control_state.get("last_non_ev_watts") or 0)
        curr_non_ev = float(energy_decision.non_ev_consumption_watts or 0)
        state_after["control"]["last_telemetry_timestamp"] = telem_ts_str
        state_after["control"]["last_non_ev_watts"] = round(curr_non_ev)
        if (
            new_telem
            and curr_non_ev - prev_non_ev > 500
            and not energy_decision.is_emergency_mode
            and telemetry.solar_watts is not None
            and not device_status.hvac_running
        ):
            try:
                refresh_path = BASE_DIR / "tesla_refresh_request.json"
                refresh_path.write_text(
                    json.dumps({
                        "requested_at": (now + timedelta(minutes=2)).isoformat(),
                        "reason": "unexplained_non_ev_spike",
                        "spike_watts": round(curr_non_ev - prev_non_ev),
                        "previous_non_ev_watts": round(prev_non_ev),
                        "current_non_ev_watts": round(curr_non_ev),
                        "hold_seconds": getattr(self, "energy_down_sustain_seconds", 180),
                    }),
                    encoding="utf-8",
                )
            except OSError:
                pass
        state_after["decision"] = {
            "last_reason": energy_decision.reason,
            "last_mode": energy_decision.mode,
            "last_status": energy_decision.action_status,
            "last_desired_enabled": energy_decision.desired_charger_enabled,
            "last_desired_amps": energy_decision.desired_amps,
            "last_decision_at": now.isoformat(),
        }
        if energy_decision.is_emergency_mode and emergency_state.get("active"):
            state_after["emergency"]["seen_charging"] = bool(
                state_after["emergency"].get("seen_charging")
                or charger.get("actively_charging")
            )
        if energy_decision.mode in {"NIGHT_CHARGING", "NIGHT_CONTINUING_AFTER_CUTOFF"} and charger.get("actively_charging"):
            state_after["night"]["seen_charging"] = True
            state_after["night"]["last_seen_charging_at"] = now.isoformat()
        if not energy_decision.is_emergency_mode and emergency_state.get("active") and energy_decision.reason == "emergency_mode_expired":
            state_after["emergency"].update(
                {
                    "active": False,
                    "seen_charging": False,
                    "started_at": None,
                    "expires_at": None,
                    "requested_amps": None,
                    "duration_minutes": None,
                }
            )

        desired_action = "enable" if energy_decision.desired_charger_enabled else "disable"
        decision = {
            "timestamp": now.isoformat(),
            "phase": energy_decision.mode.lower(),
            "base_phase": energy_decision.mode.lower(),
            "action": desired_action,
            "reason": energy_decision.reason,
            "target_mode": self.charge_now_mode if energy_decision.desired_charger_enabled else None,
            "target_amps": energy_decision.desired_amps,
            "state_after": state_after,
            "charger": charger,
            "charger_before_inference": charger_before_inference,
            "fetch": fetch_info,
            "hvac": hvac_status.to_dict() if hvac_status else None,
            "hvac_error": hvac_error,
            "tesla_solar": {
                "solar_watts": telemetry.solar_watts,
                "house_consumption_watts": telemetry.house_consumption_watts,
                "powerwall_soc": telemetry.powerwall_soc,
                "grid_import_watts": telemetry.grid_import_watts,
                "grid_export_watts": telemetry.grid_export_watts,
                "timestamp": telemetry.timestamp.isoformat() if telemetry.timestamp else None,
            },
            "startup_active": False,
            "startup_seconds_remaining": 0,
            "energy": energy_decision.to_dict(),
            "energy_config": runtime_energy_cfg.to_dict(),
            "tesla_snapshot": snapshot.to_dict(now) if snapshot else None,
            "tesla_error": tesla_error,
            "new_vehicle_connection": new_vehicle_connection,
            "ev_active_since": ev_active_since.isoformat() if ev_active_since else None,
            "external_low_solar_stop": external_low_solar_stop,
            "force_stop_active": force_stop_active,
        }
        self._apply_shared_load_policy(decision, hvac_status)
        self._apply_automation_minimum(decision)
        return decision

    def _idle_setpoint_safe_for_enable(
        self,
        decision: dict[str, Any],
        current_setpoint: Any,
        target_amps: int,
        allow_below_target: bool = False,
    ) -> bool:
        if current_setpoint is None:
            return False
        current_amps = int(current_setpoint)
        if current_amps < self.automation_min_amps or current_amps > self.charger.max_amps:
            return False
        if current_amps < target_amps and not allow_below_target:
            return False

        safe_limit_amps = target_amps
        tesla_solar = decision.get("tesla_solar") or {}
        if decision.get("base_phase") == "day_solar" and tesla_solar.get("enabled"):
            calculated_amps = tesla_solar.get("calculated_amps")
            if calculated_amps is not None:
                safe_limit_amps = max(safe_limit_amps, int(calculated_amps))

        return current_amps <= min(self.charger.max_amps, safe_limit_amps)

    def apply_decision(self, decision: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        charger = decision["charger"]
        now = current_local_time()
        energy = decision.get("energy") or {}
        energy_config = decision.get("energy_config") or {}
        state_after = decision["state_after"]
        control = state_after.setdefault("control", {})
        performed_command: dict[str, Any] | None = None
        dry_run = dry_run or bool(energy_config.get("dry_run"))

        def remember_command(command_type: str, detail: str, amps: int | None = None) -> None:
            control["last_charger_command_at"] = now.isoformat()
            control["last_charger_command_type"] = command_type
            control["last_charger_command_detail"] = detail
            if command_type == "START":
                control["last_start_command_at"] = now.isoformat()
                control["last_start_command_detail"] = detail
                control["last_start_setpoint_amps"] = amps
            if command_type == "STOP":
                control["last_stop_command_at"] = now.isoformat()
                control["last_stop_command_detail"] = detail
            if command_type in {"SET_AMPS", "START"} and amps is not None:
                history = list(control.get("setpoint_history") or [])
                history.append([now.isoformat(), amps])
                control["setpoint_history"] = history[-5:]

        def command_retry_due(command_type: str) -> bool:
            if control.get("last_charger_command_type") != command_type:
                return False
            last_command_at = self._state_datetime(control.get("last_charger_command_at"))
            if last_command_at is None:
                return False
            default_interval = getattr(self, "min_current_change_interval_seconds", 180)
            return (now - last_command_at).total_seconds() >= max(
                1,
                int(energy_config.get("charger_command_min_interval_seconds", default_interval) or 0),
            )

        desired_enabled = bool(energy.get("desired_charger_enabled"))
        desired_amps = energy.get("desired_amps")
        cooldown_remaining = int(energy.get("cooldown_remaining_seconds") or 0)
        new_vehicle_connection = bool(decision.get("new_vehicle_connection"))
        force_stop_requested = bool(decision.get("force_stop_active"))
        raw_charger = decision.get("charger_before_inference") or charger
        raw_active_after_stop = bool(
            raw_charger.get("actively_charging")
            or float(raw_charger.get("power_w") or 0.0) >= 100.0
            or raw_charger.get("state") == "charger_charging"
            or raw_charger.get("pilot_state") == "controlpi_6v_pwm"
        )
        repeat_stop_due = (
            not desired_enabled
            and command_retry_due("STOP")
            and raw_active_after_stop
            and (force_stop_requested or bool(energy_config.get("enable_charger_auto_stop", True)))
        )
        repeat_start_due = (
            desired_enabled
            and command_retry_due("START")
            and not charger.get("enabled")
            and not charger.get("actively_charging")
            and not raw_charger.get("enabled")
            and not raw_charger.get("actively_charging")
            and bool(energy_config.get("enable_charger_auto_start", True))
        )
        if repeat_stop_due or repeat_start_due:
            decision["command_retry_due"] = {
                "command_type": "STOP" if repeat_stop_due else "START",
                "last_command_at": control.get("last_charger_command_at"),
                "raw_state": raw_charger.get("state"),
                "raw_pilot_state": raw_charger.get("pilot_state"),
                "raw_enabled": raw_charger.get("enabled"),
                "raw_actively_charging": raw_charger.get("actively_charging"),
                "raw_power_w": raw_charger.get("power_w"),
            }
        force_active_stop = (
            not desired_enabled
            and (
                bool(charger.get("actively_charging"))
                or (force_stop_requested and bool(charger.get("enabled")))
            )
            and (force_stop_requested or bool(energy_config.get("enable_charger_auto_stop", True)))
        )
        if (
            force_active_stop
            and control.get("last_charger_command_type") == "STOP"
            and not charger.get("enabled")
            and cooldown_remaining > 0
        ):
            force_active_stop = False
        if force_active_stop and control.get("last_charger_command_type") == "STOP":
            last_stop_at = self._state_datetime(control.get("last_charger_command_at"))
            if last_stop_at is not None and (now - last_stop_at).total_seconds() <= 180:
                try:
                    started_monotonic = time.monotonic()
                    raw_status = self.charger.fresh_status()
                    verified = self.charger.summarize_status(raw_status)
                    decision["charger_stop_recheck"] = {
                        "checked_at": now.isoformat(),
                        "duration_ms": int(round((time.monotonic() - started_monotonic) * 1000)),
                        "state": verified.get("state"),
                        "pilot_state": verified.get("pilot_state"),
                        "enabled": verified.get("enabled"),
                        "actively_charging": verified.get("actively_charging"),
                        "power_w": verified.get("power_w"),
                    }
                    if not verified.get("actively_charging"):
                        charger = verified
                        decision["charger"] = verified
                        force_active_stop = False
                    else:
                        charger = verified
                        decision["charger"] = verified
                except Exception as exc:
                    decision["charger_stop_recheck"] = {
                        "checked_at": now.isoformat(),
                        "error": str(exc),
                    }
        if cooldown_remaining > 0 and not force_active_stop:
            decision["current_change_deferred"] = {
                "wait_seconds": cooldown_remaining,
                "command_type": "COOLDOWN",
            }
            decision["reason"] = "Charger command suppressed due to cooldown. Will reevaluate next loop."
        else:
            mode_target = decision.get("target_mode")
            standby_setpoint = max(
                self.charger.min_amps,
                min(
                    int(energy_config.get("ev_solar_max_amps", self.charger.min_amps) or self.charger.min_amps),
                    int(energy_config.get("ev_min_amps", self.charger.min_amps) or self.charger.min_amps),
                ),
            )
            if repeat_stop_due or repeat_start_due:
                # The raw status that armed this retry may itself be a stale
                # persistent-socket snapshot, so confirm on a brand-new socket
                # before re-sending the command.
                retry_confirmed = True
                if not dry_run:
                    try:
                        verified = self.charger.summarize_status(self.charger.fresh_status())
                        decision["command_retry_recheck"] = {
                            "checked_at": now.isoformat(),
                            "state": verified.get("state"),
                            "pilot_state": verified.get("pilot_state"),
                            "enabled": verified.get("enabled"),
                            "actively_charging": verified.get("actively_charging"),
                            "power_w": verified.get("power_w"),
                        }
                        if repeat_stop_due:
                            retry_confirmed = bool(
                                verified.get("actively_charging")
                                or float(verified.get("power_w") or 0.0) >= 100.0
                            )
                        else:
                            retry_confirmed = not (
                                verified.get("enabled") or verified.get("actively_charging")
                            )
                        charger = verified
                        decision["charger"] = verified
                    except Exception as exc:
                        decision["command_retry_recheck"] = {
                            "checked_at": now.isoformat(),
                            "error": str(exc),
                        }
                if not retry_confirmed:
                    pass
                elif repeat_stop_due:
                    if not dry_run:
                        self.charger.set_enabled(False)
                    remember_command("STOP", "enabled=false retry_after_stale_status")
                    performed_command = {"type": "STOP", "detail": "enabled=false retry_after_stale_status"}
                else:
                    if not dry_run:
                        if desired_amps is not None and charger.get("setpoint_amps") != desired_amps:
                            self.charger.set_amps(int(desired_amps))
                        self.charger.set_enabled(True)
                    applied = int(desired_amps or charger.get("setpoint_amps") or self.automation_min_amps)
                    detail = (
                        f"set_amps={applied}A enabled=true retry_after_stale_status"
                        if desired_amps is not None and charger.get("setpoint_amps") != desired_amps
                        else "enabled=true retry_after_stale_status"
                    )
                    remember_command("START", detail, amps=applied)
                    performed_command = {"type": "START", "detail": detail}
            elif (
                desired_enabled
                and mode_target
                and self.auto_force_charge_now_mode
                and charger.get("mode") != mode_target
            ):
                if not dry_run:
                    self.charger.set_mode(str(mode_target))
                remember_command("SET_MODE", str(mode_target))
                performed_command = {"type": "SET_MODE", "detail": str(mode_target)}
            elif (
                desired_enabled
                and not charger.get("enabled")
                and desired_amps is not None
                and charger.get("setpoint_amps") != desired_amps
                and decision.get("energy_config", {}).get("enable_charger_auto_start", True)
            ):
                if not dry_run:
                    applied = self.charger.set_amps(int(desired_amps))
                    self.charger.set_enabled(True)
                else:
                    applied = int(desired_amps)
                decision["target_amps"] = applied
                remember_command("START", f"set_amps={applied}A enabled=true", amps=applied)
                performed_command = {"type": "START", "detail": f"set_amps={applied}A enabled=true"}
            elif (
                desired_enabled
                and not charger.get("enabled")
                and decision.get("energy_config", {}).get("enable_charger_auto_start", True)
                and self._idle_setpoint_safe_for_enable(
                    decision,
                    charger.get("setpoint_amps"),
                    int(desired_amps or self.automation_min_amps),
                    allow_below_target=True,
                )
            ):
                if not dry_run:
                    self.charger.set_enabled(True)
                remember_command("START", "enabled=true", amps=charger.get("setpoint_amps"))
                performed_command = {"type": "START", "detail": "enabled=true"}
            elif (
                not desired_enabled
                and not charger.get("enabled")
                and not charger.get("actively_charging")
                and charger.get("setpoint_amps") is not None
                and int(charger.get("setpoint_amps")) > standby_setpoint
                and decision.get("base_phase") not in {"night_charging", "emergency_charging"}
            ):
                if not dry_run:
                    applied = self.charger.set_amps(standby_setpoint)
                else:
                    applied = standby_setpoint
                remember_command("SET_AMPS", f"{applied}A", amps=applied)
                performed_command = {"type": "SET_AMPS", "detail": f"{applied}A"}
            elif (
                desired_amps is not None
                and charger.get("setpoint_amps") != desired_amps
            ):
                if not dry_run:
                    applied = self.charger.set_amps(int(desired_amps))
                else:
                    applied = int(desired_amps)
                decision["target_amps"] = applied
                remember_command("SET_AMPS", f"{applied}A", amps=applied)
                performed_command = {"type": "SET_AMPS", "detail": f"{applied}A"}
            elif desired_enabled and not charger.get("enabled") and energy_config.get("enable_charger_auto_start", True):
                if not dry_run:
                    self.charger.set_enabled(True)
                remember_command("START", "enabled=true", amps=charger.get("setpoint_amps"))
                performed_command = {"type": "START", "detail": "enabled=true"}
            elif (
                not desired_enabled
                and (charger.get("enabled") or charger.get("actively_charging"))
                and (force_stop_requested or energy_config.get("enable_charger_auto_stop", True))
            ):
                if not dry_run:
                    self.charger.set_enabled(False)
                remember_command("STOP", "enabled=false")
                performed_command = {"type": "STOP", "detail": "enabled=false"}

        decision["performed_command"] = performed_command

        if desired_enabled and charger.get("actively_charging"):
            state_after["emergency"]["seen_charging"] = bool(energy.get("is_emergency_mode"))

        if not dry_run:
            self._sync_charging_session(charger, decision, now)
            self.session_store.record_charger_telemetry(charger, decision, now)
            self._preserve_newer_emergency_state(state_after)
            save_automation_state(state_after, self.state_path)

        mode_note = f"mode={charger['mode']}"
        amps_note = (
            f"target={decision['target_amps']}A" if decision["target_amps"] is not None else "target=-"
        )
        if self.log_fetch_details:
            fetch = decision.get("fetch") or {}
            print(
                f"{current_local_time().strftime('%Y-%m-%d %H:%M:%S')} "
                f"fetch status_ms={fetch.get('duration_ms', '-')} "
                f"state={charger['state']} pilot={charger['pilot_state']} "
                f"enabled={charger['enabled']} active={charger['actively_charging']} "
                f"power={int(round(float(charger['power_w'])))}W "
                f"setpoint={charger['setpoint_amps']}A",
                flush=True,
            )
        log_signature = (
            decision.get("phase"),
            decision.get("action"),
            decision.get("reason"),
            repr(performed_command),
        )
        last_signature, last_logged_at = getattr(self, "_last_decision_log", (None, None))
        if (
            performed_command is not None
            or log_signature != last_signature
            or last_logged_at is None
            or (now - last_logged_at).total_seconds() >= 300
        ):
            print(
                f"{current_local_time().strftime('%Y-%m-%d %H:%M:%S')} "
                f"phase={decision['phase']} action={decision['action']} reason={decision['reason']} "
                f"{mode_note} {amps_note} command={performed_command}",
                flush=True,
            )
            self._last_decision_log = (log_signature, now)
        return decision

    def run_cycle(self, dry_run: bool = False) -> dict[str, Any]:
        try:
            self.reload_config_if_needed()
            decision = self.plan()
            control = decision["state_after"].setdefault("control", {})
            control["consecutive_charger_fetch_errors"] = 0
            control["charger_fetch_error_first_seen_at"] = None
            control["charger_fetch_error_last_seen_at"] = None
            control["charger_fetch_error_last_message"] = None
            decision = self.apply_decision(decision, dry_run=dry_run)
            report = _build_status_report_from_decision(self, decision, report_now=current_local_time())
            save_status_report_cache(report)
            append_energy_debug_trace(report)
            return decision
        except Exception as exc:
            now = current_local_time()
            error_text = str(exc)
            state_after_error = self._record_charger_fetch_error(error_text, now)
            print(
                f"{now.strftime('%Y-%m-%d %H:%M:%S')} "
                f"fetch_error error={exc}",
                flush=True,
            )

            retried_after_reconnect = False
            if self.rediscovery_enabled:
                try:
                    refreshed = self.refresh_charger_ip(reason="fetch_error", now=now)
                    if not refreshed:
                        self.reconnect_charger(reason="fetch_error_same_ip", now=now)
                    retried_after_reconnect = True
                except Exception as recovery_exc:
                    print(
                        f"{current_local_time().strftime('%Y-%m-%d %H:%M:%S')} "
                        f"fetch_error_recovery_failed error={recovery_exc}",
                        flush=True,
                    )

            if not retried_after_reconnect:
                self.reconnect_charger(reason="fetch_error", now=now)

            try:
                retry_decision = self.plan()
                retry_decision["state_after"]["control"]["consecutive_charger_fetch_errors"] = 0
                retry_decision["state_after"]["control"]["charger_fetch_error_first_seen_at"] = None
                retry_decision["state_after"]["control"]["charger_fetch_error_last_seen_at"] = None
                retry_decision["state_after"]["control"]["charger_fetch_error_last_message"] = None
                retry_decision = self.apply_decision(retry_decision, dry_run=dry_run)
                retry_report = _build_status_report_from_decision(
                    self,
                    retry_decision,
                    report_now=current_local_time(),
                )
                save_status_report_cache(retry_report)
                append_energy_debug_trace(retry_report)
                return retry_decision
            except Exception as retry_exc:
                print(
                    f"{current_local_time().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"fetch_error_retry_failed error={retry_exc}",
                    flush=True,
                )
                error_text = str(retry_exc)
                state_after_error = self._record_charger_fetch_error(error_text, current_local_time())

            error_report = build_error_status_report(
                error_text,
                now=current_local_time(),
                cached=load_status_report_cache(),
            )
            save_status_report_cache(error_report)
            append_energy_debug_trace(error_report)
            return {
                "timestamp": current_local_time().isoformat(),
                "phase": "error",
                "action": "hold",
                "reason": error_text,
                "state_after": state_after_error,
            }

    def _sleep_seconds_after_cycle(self, decision: dict[str, Any] | None) -> float:
        """Stretch the poll interval while charger fetch errors persist.

        A wedged charger (e.g. Tuya daemon that accepts TCP but never answers
        the session handshake) gets hammered with ~6 handshakes per cycle by
        the retry path; sustained hammering can keep the device from ever
        recovering. Back off exponentially up to a cap so it gets quiet time.
        """
        try:
            control = ((decision or {}).get("state_after") or {}).get("control") or {}
            errors = int(control.get("consecutive_charger_fetch_errors") or 0)
        except (TypeError, ValueError):
            errors = 0
        if (
            errors < self.fetch_error_backoff_after_failures
            or self.fetch_error_backoff_max_seconds <= 0
        ):
            if self._last_backoff_logged_seconds is not None:
                self._last_backoff_logged_seconds = None
                print(
                    f"{current_local_time().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"fetch_error_backoff_cleared poll_seconds={self.poll_seconds}",
                    flush=True,
                )
            return self.poll_seconds
        doublings = min(
            1 + (errors - self.fetch_error_backoff_after_failures) // 2,
            8,
        )
        sleep_seconds = float(
            min(
                self.poll_seconds * (2**doublings),
                max(self.fetch_error_backoff_max_seconds, self.poll_seconds),
            )
        )
        if sleep_seconds != self._last_backoff_logged_seconds:
            self._last_backoff_logged_seconds = sleep_seconds
            print(
                f"{current_local_time().strftime('%Y-%m-%d %H:%M:%S')} "
                f"fetch_error_backoff consecutive_errors={errors} "
                f"sleep_seconds={int(sleep_seconds)}",
                flush=True,
            )
        return sleep_seconds

    def run(self, once: bool = False, dry_run: bool = False) -> None:
        if self.rediscovery_enabled:
            self.refresh_charger_ip(reason="startup")
        while not self.stop_requested:
            if (
                self.rediscovery_enabled
                and self.next_rediscovery_at is not None
                and current_local_time() >= self.next_rediscovery_at
            ):
                self.refresh_charger_ip(reason="interval_24h")
            decision = self.run_cycle(dry_run=dry_run)
            if once:
                return
            time.sleep(self._sleep_seconds_after_cycle(decision))


def _build_status_report_from_decision(
    controller: AutoScheduleController,
    decision: dict[str, Any],
    report_now: datetime | None = None,
) -> dict[str, Any]:
    report_now = report_now or current_local_time()
    db_ready = controller.session_store.ensure_ready() if controller.session_store.enabled else False
    energy = decision.get("energy") or {}
    energy_config = decision.get("energy_config") or controller.energy_config.to_dict()
    telemetry = decision.get("tesla_solar") or {}
    control_state = decision["state_after"].get("control") or {}
    emergency_state = decision["state_after"].get("emergency") or {}
    return {
        "timestamp": decision["timestamp"],
        "source": "live",
        "stale": False,
        "live_error": None,
        "alerts": build_status_alerts(decision),
        "charger": {
            k: v for k, v in decision["charger"].items() if k != "raw_dps"
        },
        "charger_dps": decision["charger"]["raw_dps"],
        "hvac": decision["hvac"],
        "hvac_error": decision["hvac_error"],
        "fetch": decision["fetch"],
        "automation": {
            "phase": decision["phase"],
            "base_phase": decision["base_phase"],
            "action": decision["action"],
            "reason": decision["reason"],
            "target_mode": decision["target_mode"],
            "target_amps": decision["target_amps"],
            "profile": controller.active_profile,
            "profiles": list(SEASONAL_PROFILE_NAMES),
            "automation_min_amps": controller.automation_min_amps,
            "min_current_change_interval_seconds": controller.min_current_change_interval_seconds,
            "fault_reset_enabled": controller.fault_reset_enabled,
            "fault_reset_cooldown_seconds": controller.fault_reset_cooldown_seconds,
            "startup_failure_cooldown_seconds": controller.startup_failure_cooldown_seconds,
            "tesla_solar_control_enabled": controller.tesla_solar_control_enabled,
            "no_charge_start": f"{controller.no_charge_start_minutes // 60:02d}:{controller.no_charge_start_minutes % 60:02d}",
            "no_charge_end": f"{controller.no_charge_end_minutes // 60:02d}:{controller.no_charge_end_minutes % 60:02d}",
            "night_charge_start": f"{controller.night_charge_start_minutes // 60:02d}:{controller.night_charge_start_minutes % 60:02d}",
            "night_new_start_cutoff": f"{controller.night_new_start_cutoff_minutes // 60:02d}:{controller.night_new_start_cutoff_minutes % 60:02d}",
            "startup_active": decision["startup_active"],
            "startup_seconds_remaining": decision["startup_seconds_remaining"],
            "current_change_deferred": decision.get("current_change_deferred"),
            "fault_reset_attempted": decision.get("fault_reset_attempted", False),
            "fault_reset_wait_seconds": decision.get("fault_reset_wait_seconds"),
            "tesla_solar": decision.get("tesla_solar"),
            "state": decision["state_after"],
            "blocked_window_active": controller.in_block_window(report_now),
            "night_session_key": controller.night_session_key(report_now),
        },
        "energy_status": {
            "mode": energy.get("mode"),
            "reason": energy.get("reason"),
            "action_status": energy.get("action_status"),
            "telemetry_state": energy.get("telemetry_state"),
            "new_vehicle_connection": decision.get("new_vehicle_connection"),
            "external_low_solar_stop": decision.get("external_low_solar_stop"),
            "ev_active_since": decision.get("ev_active_since"),
            "desired_charger_enabled": energy.get("desired_charger_enabled"),
            "desired_amps": energy.get("desired_amps"),
            "computed_available_watts": energy.get("computed_available_watts"),
            "adjusted_consumption_watts": energy.get("adjusted_consumption_watts"),
            "predictive_load_added_watts": energy.get("predictive_load_added_watts"),
            "predicted_load_components": energy.get("predicted_load_components"),
            "allowed_total_consumption_watts": energy.get("allowed_total_consumption_watts"),
            "non_ev_consumption_watts": energy.get("non_ev_consumption_watts"),
            "estimated_current_ev_watts": energy.get("estimated_current_ev_watts"),
            "is_emergency_mode": energy.get("is_emergency_mode"),
            "command_allowed_now": energy.get("command_allowed_now"),
            "cooldown_remaining_seconds": energy.get("cooldown_remaining_seconds"),
            "warning": energy.get("warning"),
            "performed_command": decision.get("performed_command"),
            "last_command_type": control_state.get("last_charger_command_type"),
            "last_command_at": control_state.get("last_charger_command_at"),
            "last_command_detail": control_state.get("last_charger_command_detail"),
            "next_low_solar_stop_counter": energy.get("next_low_solar_stop_counter"),
            "telemetry": {
                "solar_watts": telemetry.get("solar_watts"),
                "house_consumption_watts": telemetry.get("house_consumption_watts"),
                "powerwall_soc": telemetry.get("powerwall_soc"),
                "grid_import_watts": telemetry.get("grid_import_watts"),
                "grid_export_watts": telemetry.get("grid_export_watts"),
                "timestamp": telemetry.get("timestamp"),
                "age_seconds": (
                    max(
                        0,
                        int((report_now - datetime.fromisoformat(telemetry["timestamp"])).total_seconds()),
                    )
                    if telemetry.get("timestamp")
                    else None
                ),
            },
            "emergency_mode": {
                "active": emergency_state.get("active", False),
                "expires_at": emergency_state.get("expires_at"),
                "requested_amps": emergency_state.get("requested_amps"),
                "duration_minutes": emergency_state.get("duration_minutes"),
                "target_energy_kwh": emergency_state.get("target_energy_kwh"),
                "delivered_energy_wh": emergency_state.get("delivered_energy_wh"),
                "target_completed_at": emergency_state.get("target_completed_at"),
                "target_completed_kwh": emergency_state.get("target_completed_kwh"),
            },
            "tesla_error": decision.get("tesla_error"),
            "tesla_snapshot": decision.get("tesla_snapshot"),
        },
        "energy_config": energy_config,
        "database": {
            "enabled": controller.session_store.enabled,
            "ready": db_ready,
            "database": controller.session_store.database if controller.session_store.enabled else None,
            "table": controller.session_store.table if controller.session_store.enabled else None,
            "last_error": controller.session_store.last_error or decision["state_after"]["session"].get("db_error"),
            "active_session_id": decision["state_after"]["session"].get("db_id"),
            "active_session_energy_wh": decision["state_after"]["session"].get("energy_wh"),
        },
    }


def build_status_report(config: dict[str, Any], state_path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    controller = AutoScheduleController(config, state_path=state_path)
    try:
        decision = controller.plan()
        report = _build_status_report_from_decision(controller, decision, report_now=current_local_time())
        save_status_report_cache(report)
        return report
    except Exception as exc:
        report = build_error_status_report(
            str(exc),
            now=current_local_time(),
            cached=load_status_report_cache(),
        )
        save_status_report_cache(report)
        return report


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    if not isinstance(data.get("charger"), dict):
        raise ValueError("Config must include a 'charger' mapping")
    return data


def print_status(config: dict[str, Any]) -> int:
    charger = AimilerCharger(config["charger"])
    status = charger.status()
    print(yaml.safe_dump(status, sort_keys=False))
    return 0


def print_status_report(config: dict[str, Any]) -> int:
    print(yaml.safe_dump(build_status_report(config), sort_keys=False))
    return 0


def apply_command(config: dict[str, Any], enable: bool | None, set_amps: int | None) -> int:
    charger = AimilerCharger(config["charger"])

    if set_amps is not None:
        applied = charger.set_amps(set_amps)
        print(f"set_current={applied}A")

    if enable is not None:
        charger.set_enabled(enable)
        print(f"charger={'ON' if enable else 'OFF'}")

    status = charger.status()
    print(yaml.safe_dump(status, sort_keys=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aimiler charger utility with daytime profile, night charging, and emergency override"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to YAML config")
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Read and print raw charger status/DPS once, then exit",
    )
    parser.add_argument(
        "--status-report",
        action="store_true",
        help="Print charger and automation status summary once, then exit",
    )
    parser.add_argument("--on", action="store_true", help="Enable the charger output")
    parser.add_argument("--off", action="store_true", help="Disable the charger output")
    parser.add_argument(
        "--set-amps",
        type=int,
        help="Set charger current in amps, clamped to configured min/max",
    )
    parser.add_argument(
        "--run-auto",
        action="store_true",
        help="Run the automated charging controller",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="When used with --run-auto, evaluate one control cycle and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="When used with --run-auto, print the decision without changing the charger",
    )
    parser.add_argument(
        "--emergency-on",
        action="store_true",
        help="Activate emergency charging override",
    )
    parser.add_argument(
        "--emergency-off",
        action="store_true",
        help="Deactivate emergency charging override",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = load_config(config_path)

    if args.status_only:
        return print_status(cfg)
    if args.status_report:
        return print_status_report(cfg)
    if args.on and args.off:
        parser.error("--on and --off cannot be used together")
    if args.emergency_on and args.emergency_off:
        parser.error("--emergency-on and --emergency-off cannot be used together")
    if args.emergency_on:
        activate_emergency_override()
        print("emergency=ON")
        return 0
    if args.emergency_off:
        deactivate_emergency_override()
        print("emergency=OFF")
        return 0
    if args.on or args.off or args.set_amps is not None:
        enable = True if args.on else False if args.off else None
        return apply_command(cfg, enable=enable, set_amps=args.set_amps)
    if args.run_auto:
        controller = AutoScheduleController(cfg, config_path=config_path)
        controller.run(once=args.once, dry_run=args.dry_run)
        return 0

    parser.error(
        "Specify --status-only, --status-report, --on, --off, --set-amps, "
        "--emergency-on, --emergency-off, or --run-auto"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
