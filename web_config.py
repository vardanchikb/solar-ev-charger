#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import hmac
import os

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
import yaml

from energy_controller import (
    ABSOLUTE_MAX_AMPS,
    ABSOLUTE_MIN_AMPS,
    EnergyConfig,
    ChargerState,
    DeviceStatus,
    EnergyTelemetry,
    decide_energy_action,
    default_energy_config,
    make_energy_config,
    start_emergency_mode,
    stop_emergency_mode,
    validate_energy_config,
)
from solar_ev_controller import (
    AutoScheduleController,
    DEFAULT_DB_PASSWORD_FILE,
    DEFAULT_DEBUG_TRACE_PATH,
    DEFAULT_SECRET_DIR,
    DEFAULT_STATE_PATH,
    active_automation_config,
    build_status_report,
    clear_force_stop_state,
    current_local_time,
    load_energy_debug_trace,
    load_automation_state,
    load_status_report_cache,
    normalize_automation_config,
    MySQLSessionStore,
    request_temporary_force_stop,
)
from tesla_energy import (
    DEFAULT_TESLA_PARTNER_PUBLIC_KEY_FILE,
    TeslaEnergyMonitor,
    sanitized_tesla_config,
    write_secret_file as write_tesla_secret_file,
)

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

app = Flask(__name__)
DASHBOARD_PASSWORD_FILE = DEFAULT_SECRET_DIR / "dashboard_password"
_auth_warning_logged = False


def _dashboard_password() -> str:
    try:
        return DASHBOARD_PASSWORD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@app.before_request
def _require_dashboard_auth() -> Response | None:
    global _auth_warning_logged
    if app.config.get("TESTING"):
        return None
    expected = _dashboard_password()
    if not expected:
        if not _auth_warning_logged:
            _auth_warning_logged = True
            print(
                f"WARNING: dashboard authentication disabled - create {DASHBOARD_PASSWORD_FILE} "
                "(chmod 600) to require a password",
                flush=True,
            )
        return None
    auth = request.authorization
    if (
        auth is not None
        and auth.password is not None
        and hmac.compare_digest(str(auth.password), expected)
    ):
        return None
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="carcharger", charset="UTF-8"'},
    )


STATUS_CACHE_LIVE_MAX_AGE_SECONDS = 90
CHARGER_PROFILE_NAMES = ("summer", "winter")
PROFILE_TUNING_FIELDS = {
    "automation_min_amps": {"type": "int", "default": 8, "min": 8, "max": 32},
    "energy_down_sustain_seconds": {"type": "int", "default": 180, "min": 0},
    "startup_grace_seconds": {"type": "int", "default": 30, "min": 0},
    "startup_failure_cooldown_seconds": {"type": "int", "default": 900, "min": 0},
    "poll_seconds": {"type": "int", "default": 15, "min": 1},
}


def _status_cache_age_seconds(payload: dict[str, Any]) -> int | None:
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str):
        return None
    try:
        sampled_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    now = datetime.now(sampled_at.tzinfo) if sampled_at.tzinfo else datetime.now()
    return max(0, int((now - sampled_at).total_seconds()))


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml root must be a mapping")
    data.setdefault("charger", {})
    data.setdefault("database", {})
    data["automation"] = normalize_automation_config(data.get("automation"))
    data.setdefault("tesla_energy", {})
    data["database"].setdefault("password_file", str(DEFAULT_DB_PASSWORD_FILE))
    return data


def save_config(data: dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def secret_is_configured(path: str) -> bool:
    try:
        return Path(path).exists() and bool(Path(path).read_text(encoding="utf-8").strip())
    except OSError:
        return False


def write_secret_file(path: str, value: str) -> None:
    secret_path = Path(path)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(value.strip() + "\n", encoding="utf-8")
    os.chmod(secret_path, 0o600)


def sanitized_database_config(database: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(database)
    sanitized.pop("password", None)
    sanitized["password_file"] = str(
        sanitized.get("password_file") or DEFAULT_DB_PASSWORD_FILE
    )
    sanitized["password_configured"] = secret_is_configured(sanitized["password_file"])
    return sanitized


def to_int(form: dict[str, str], key: str, default: int) -> int:
    raw = form.get(key, "").strip()
    if raw == "":
        return default
    return int(raw)


def to_float(form: dict[str, str], key: str, default: float) -> float:
    raw = form.get(key, "").strip()
    if raw == "":
        return default
    return float(raw)


def to_bool(form: Any, key: str, default: bool = False) -> bool:
    if key not in form:
        return default
    return form.get(key, "").strip().lower() in {"1", "true", "on", "yes"}


def form_or_keep(key: str, current: Any) -> str:
    """Submitted value, or the existing config value when the field is omitted
    or left blank. Prevents a partial or empty POST from wiping identity and
    credential fields such as the charger id, key, and ip."""
    value = request.form.get(key)
    if value is None:
        return str(current if current is not None else "")
    value = value.strip()
    return value or str(current if current is not None else "")


def _active_profile_name(cfg: dict[str, Any]) -> str:
    automation = normalize_automation_config(cfg.get("automation"))
    return str(automation.get("active_profile") or "spring")


def _charger_profile_name(raw: str | None, fallback: str = "summer") -> str:
    profile = str(raw or "").strip().lower()
    if profile in CHARGER_PROFILE_NAMES:
        return profile
    return fallback if fallback in CHARGER_PROFILE_NAMES else "summer"


def _profile_energy_raw(cfg: dict[str, Any], profile_name: str | None = None) -> tuple[dict[str, Any], str]:
    automation = normalize_automation_config(cfg.get("automation"))
    active_profile = profile_name or str(automation.get("active_profile") or "spring")
    profiles = automation.setdefault("profiles", {})
    profile = profiles.setdefault(active_profile, {})
    energy = profile.get("energy_controller")
    if not isinstance(energy, dict):
        energy = {}
        profile["energy_controller"] = energy
    cfg["automation"] = automation
    return energy, active_profile


def _profile_raw(cfg: dict[str, Any], profile_name: str | None = None) -> tuple[dict[str, Any], str]:
    automation = normalize_automation_config(cfg.get("automation"))
    active_profile = profile_name or str(automation.get("active_profile") or "spring")
    profiles = automation.setdefault("profiles", {})
    profile = profiles.setdefault(active_profile, {})
    cfg["automation"] = automation
    return profile, active_profile


def _profile_tuning_settings(cfg: dict[str, Any], profile_name: str | None = None) -> dict[str, Any]:
    profile, _active_profile = _profile_raw(cfg, profile_name=profile_name)
    settings: dict[str, Any] = {}
    for key, meta in PROFILE_TUNING_FIELDS.items():
        settings[key] = profile.get(key, meta["default"])
    return settings


def _coerce_profile_tuning_value(key: str, value: Any) -> Any:
    meta = PROFILE_TUNING_FIELDS[key]
    field_type = meta["type"]
    if field_type == "bool":
        return bool(value)
    if field_type == "int":
        if value in (None, ""):
            coerced = int(meta["default"])
        else:
            coerced = int(value)
        if "min" in meta:
            coerced = max(int(meta["min"]), coerced)
        if "max" in meta:
            coerced = min(int(meta["max"]), coerced)
        return coerced
    return value


def _save_profile_tuning_settings(
    cfg: dict[str, Any],
    updates: dict[str, Any],
    profile_name: str | None = None,
) -> None:
    profile, _active_profile = _profile_raw(cfg, profile_name=profile_name)
    for key, value in updates.items():
        if key in PROFILE_TUNING_FIELDS:
            profile[key] = _coerce_profile_tuning_value(key, value)


def _effective_energy_config(cfg: dict[str, Any], profile_name: str | None = None) -> dict[str, Any]:
    energy_raw, active_profile = _profile_energy_raw(cfg, profile_name=profile_name)
    effective, errors = validate_energy_config(
        energy_raw,
        charger_cfg=cfg.get("charger"),
        legacy_profile=active_automation_config(cfg.get("automation")),
    )
    if errors:
        raise ValueError("; ".join(errors))
    effective["active_profile"] = active_profile
    return effective


def _save_effective_energy_config(
    cfg: dict[str, Any],
    updates: dict[str, Any],
    profile_name: str | None = None,
) -> dict[str, Any]:
    energy_raw, active_profile = _profile_energy_raw(cfg, profile_name=profile_name)
    merged = dict(energy_raw)
    merged.update(updates)
    merged["updated_at"] = current_local_time().isoformat()
    effective, errors = validate_energy_config(
        merged,
        charger_cfg=cfg.get("charger"),
        legacy_profile=active_automation_config(cfg.get("automation")),
    )
    if errors:
        raise ValueError("; ".join(errors))
    cfg["automation"]["profiles"][active_profile]["energy_controller"] = effective
    save_config(cfg)
    effective["active_profile"] = active_profile
    return effective


def _load_energy_status(cfg: dict[str, Any]) -> dict[str, Any]:
    cached = load_status_report_cache()
    if cached and isinstance(cached, dict) and cached.get("energy_status"):
        age_seconds = _status_cache_age_seconds(cached)
        if age_seconds is None or age_seconds > STATUS_CACHE_LIVE_MAX_AGE_SECONDS:
            return build_status_report(cfg)
        payload = dict(cached)
        payload["source"] = "cache" if payload.get("source") == "cache" else "status_cache"
        payload["status_cache_age_seconds"] = age_seconds
        return payload
    return build_status_report(cfg)


def _parse_local_datetime(value: str | None, reference: datetime) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None and reference.tzinfo is not None:
        parsed = parsed.replace(tzinfo=reference.tzinfo)
    return parsed


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/dashboard")
def dashboard_page() -> str:
    response = app.make_response(render_template("dashboard_status.html"))
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/sessions")
def sessions_page() -> str:
    return render_template("sessions.html")


@app.get("/charger")
def charger_page() -> str:
    cfg = load_config()
    active_profile = _charger_profile_name(_active_profile_name(cfg))
    return render_template(
        "charger_profiles.html",
        active_profile=active_profile,
        automation_profiles=CHARGER_PROFILE_NAMES,
        saved=request.args.get("saved"),
    )


@app.get("/system")
def system_page() -> str:
    cfg = load_config()
    tesla_monitor = TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database"))
    system_profile = active_automation_config(cfg.get("automation"))
    db_ping_ok, db_ping_error = (None, "")
    if (cfg.get("database") or {}).get("enabled"):
        db_ping_ok, db_ping_error = MySQLSessionStore(cfg).ping()
    return render_template(
        "system.html",
        db_ping_ok=db_ping_ok,
        db_ping_error=db_ping_error,
        charger=cfg["charger"],
        automation=cfg["automation"],
        system_profile=system_profile,
        database=sanitized_database_config(cfg["database"]),
        tesla=sanitized_tesla_config(cfg["tesla_energy"]),
        tesla_status=tesla_monitor.status_summary(),
        tesla_authorize_url=tesla_monitor.build_authorize_url(),
        dashboard_password_configured=bool(_dashboard_password()),
        saved=request.args.get("saved"),
        tesla_state=request.args.get("tesla_state"),
        tesla_error=request.args.get("tesla_error"),
        tesla_import_state=request.args.get("tesla_import_state"),
        tesla_import_error=request.args.get("tesla_import_error"),
    )


def _save_system_settings(cfg: dict[str, Any]) -> tuple[TeslaEnergyMonitor, dict[str, Any]]:
    charger = cfg["charger"]
    database = cfg["database"]
    automation = normalize_automation_config(cfg.get("automation"))
    tesla = cfg["tesla_energy"]

    charger["name"] = form_or_keep("name", charger.get("name", ""))
    charger["id"] = form_or_keep("id", charger.get("id", ""))
    charger["key"] = form_or_keep("key", charger.get("key", ""))
    charger["ip"] = form_or_keep("ip", charger.get("ip", ""))
    charger["version"] = to_float(request.form, "version", float(charger.get("version", 3.4)))
    charger["switch_dp"] = to_int(request.form, "switch_dp", int(charger.get("switch_dp", 18)))
    charger["current_dp"] = to_int(request.form, "current_dp", int(charger.get("current_dp", 4)))
    charger["mode_dp"] = to_int(request.form, "mode_dp", int(charger.get("mode_dp", 14)))

    current_unit = request.form.get("current_unit", "amp").strip().lower()
    charger["current_unit"] = "deciamp" if current_unit == "deciamp" else "amp"

    charger["min_amps"] = to_int(request.form, "min_amps", int(charger.get("min_amps", 8)))
    charger["max_amps"] = to_int(request.form, "max_amps", int(charger.get("max_amps", 32)))
    charger["voltage"] = to_float(request.form, "voltage", float(charger.get("voltage", 230)))
    charger["phases"] = to_int(request.form, "phases", int(charger.get("phases", 1)))
    charger["debug_tuya_responses"] = "debug_tuya_responses" in request.form

    database["enabled"] = to_bool(request.form, "db_enabled", bool(database.get("enabled", False)))
    database["bootstrap"] = to_bool(request.form, "db_bootstrap", bool(database.get("bootstrap", True)))
    database["socket"] = request.form.get("db_socket", "").strip() or "/run/mysqld/mysqld.sock"
    database["host"] = request.form.get("db_host", "").strip() or "127.0.0.1"
    database["port"] = to_int(request.form, "db_port", int(database.get("port", 3306) or 3306))
    database["user"] = request.form.get("db_user", "").strip()
    database["database"] = request.form.get("db_name", "").strip() or "carcharger"
    database["table"] = request.form.get("db_table", "").strip() or "charging_sessions"
    database["telemetry_table"] = (
        request.form.get("db_telemetry_table", "").strip() or "charger_telemetry_samples"
    )
    # The secret-file path is deliberately NOT form-controlled: accepting it
    # from the form let any client write a file to an arbitrary path.
    database["password_file"] = str(database.get("password_file") or DEFAULT_DB_PASSWORD_FILE)
    database.pop("password", None)
    password = request.form.get("db_password", "")
    if password.strip():
        write_secret_file(database["password_file"], password)

    dashboard_password = request.form.get("dashboard_password", "")
    if dashboard_password.strip():
        write_secret_file(str(DASHBOARD_PASSWORD_FILE), dashboard_password)

    shared_profile_updates = {
        "hvac_status_url": request.form.get("hvac_status_url", "").strip() or "http://127.0.0.1:8789/api/hvac-status",
        "hvac_timeout_seconds": to_int(request.form, "hvac_timeout_seconds", 5),
        "hvac_refresh_seconds": to_int(request.form, "hvac_refresh_seconds", 120),
    }
    profiles = automation.setdefault("profiles", {})
    for seasonal_profile in profiles.values():
        if not isinstance(seasonal_profile, dict):
            continue
        seasonal_profile.update(shared_profile_updates)
        seasonal_profile.pop("schedule_slots", None)
        seasonal_profile.pop("tesla_solar_cloudy_min_start", None)
        seasonal_profile.pop("tesla_solar_ev_share_pct", None)
        seasonal_profile.pop("tesla_solar_powerwall_full_target_pct", None)
        seasonal_profile.pop("tesla_solar_powerwall_full_by", None)
        seasonal_profile.pop("tesla_solar_powerwall_boost_ev_share_pct", None)
        seasonal_profile.pop("tesla_solar_powerwall_high_ev_share_pct", None)
        seasonal_profile.pop("tesla_solar_powerwall_low_ev_share_pct", None)
        seasonal_profile.pop("tesla_solar_after4pm_ev_share_pct", None)
    cfg["automation"] = automation

    tesla["enabled"] = to_bool(request.form, "tesla_enabled", bool(tesla.get("enabled", False)))
    tesla["client_id"] = request.form.get("tesla_client_id", "").strip()
    tesla["redirect_uri"] = request.form.get("tesla_redirect_uri", "").strip() or "http://localhost:5000/callback"
    tesla["audience"] = request.form.get("tesla_audience", "").strip() or "https://fleet-api.prd.na.vn.cloud.tesla.com"
    tesla["api_base_url"] = request.form.get("tesla_api_base_url", "").strip() or tesla["audience"]
    tesla["energy_site_id"] = request.form.get("tesla_energy_site_id", "").strip()
    tesla["partner_domain"] = request.form.get("tesla_partner_domain", "").strip()
    tesla["partner_public_key_file"] = (
        request.form.get("tesla_partner_public_key_file", "").strip()
        or DEFAULT_TESLA_PARTNER_PUBLIC_KEY_FILE
    )
    tesla["poll_seconds"] = to_int(request.form, "tesla_poll_seconds", int(tesla.get("poll_seconds", 60)))
    tesla["history_hours"] = to_int(request.form, "tesla_history_hours", int(tesla.get("history_hours", 24)))
    tesla["history_max_points"] = to_int(
        request.form,
        "tesla_history_max_points",
        int(tesla.get("history_max_points", 288)),
    )
    tesla["live_table"] = request.form.get("tesla_live_table", "").strip() or "tesla_energy_live_samples"
    tesla["history_table"] = request.form.get("tesla_history_table", "").strip() or "tesla_energy_history"

    tesla_secret = request.form.get("tesla_client_secret", "")
    tesla_monitor = TeslaEnergyMonitor(tesla, database)
    if tesla_secret.strip():
        write_tesla_secret_file(tesla_monitor.client_secret_file, tesla_secret)

    tesla_refresh_token = request.form.get("tesla_refresh_token", "").strip()
    if tesla_refresh_token:
        tesla_monitor.store_refresh_token(tesla_refresh_token)

    save_config(cfg)
    return tesla_monitor, tesla


@app.post("/system")
def save_system() -> Any:
    cfg = load_config()
    tesla_monitor, _tesla = _save_system_settings(cfg)

    tesla_auth_code = request.form.get("tesla_auth_code", "").strip()
    if tesla_auth_code:
        try:
            tesla_monitor.exchange_code(tesla_auth_code)
            return redirect(url_for("system_page", saved="1", tesla_state="authorized"))
        except Exception as exc:
            return redirect(url_for("system_page", saved="1", tesla_error=str(exc)))

    if request.form.get("tesla_register_partner"):
        try:
            tesla_monitor.register_partner_account()
            return redirect(url_for("system_page", saved="1", tesla_state="partner registered"))
        except Exception as exc:
            return redirect(url_for("system_page", saved="1", tesla_error=str(exc)))

    if request.form.get("tesla_import_history"):
        import_days = to_int(request.form, "tesla_import_days", 90)
        try:
            result = tesla_monitor.import_recent_history(days=import_days)
            return redirect(
                url_for(
                    "system_page",
                    saved="1",
                    tesla_import_state=f"imported {result['imported_rows']} rows",
                )
            )
        except Exception as exc:
            return redirect(url_for("system_page", saved="1", tesla_import_error=str(exc)))

    return redirect(url_for("system_page", saved="1"))


@app.post("/charger")
def save_charger() -> Any:
    cfg = load_config()
    profile = _charger_profile_name(request.form.get("automation_active_profile"), _charger_profile_name(_active_profile_name(cfg)))
    cfg["automation"]["active_profile"] = profile
    save_config(cfg)
    return redirect(url_for("charger_page", saved="1"))


@app.get("/api/energy/config")
def api_energy_config_get() -> Any:
    try:
        cfg = load_config()
        active_profile = _charger_profile_name(request.args.get("profile"), _charger_profile_name(_active_profile_name(cfg)))
        effective = _effective_energy_config(cfg, profile_name=active_profile)
        defaults = default_energy_config(
            charger_cfg=cfg.get("charger"),
            legacy_profile=active_automation_config(cfg.get("automation")),
        )
        return jsonify(
            {
                "ok": True,
                "active_profile": active_profile,
                "effective": effective,
                "profile_settings": _profile_tuning_settings(cfg, profile_name=active_profile),
                "defaults": defaults,
                "updated_at": effective.get("updated_at"),
                "config_version": effective.get("config_version"),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.put("/api/energy/config")
def api_energy_config_put() -> Any:
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "JSON object required"}), 400
        cfg = load_config()
        target_profile = _charger_profile_name(payload.pop("active_profile", None), _charger_profile_name(_active_profile_name(cfg)))
        cfg["automation"]["active_profile"] = target_profile
        profile_updates = {
            key: payload.pop(key)
            for key in list(payload.keys())
            if key in PROFILE_TUNING_FIELDS
        }
        if profile_updates:
            _save_profile_tuning_settings(cfg, profile_updates, profile_name=target_profile)
        effective = _save_effective_energy_config(cfg, payload, profile_name=target_profile)
        return jsonify(
            {
                "ok": True,
                "effective": effective,
                "profile_settings": _profile_tuning_settings(cfg, profile_name=target_profile),
                "updated_at": effective.get("updated_at"),
            }
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/energy/config/reset")
def api_energy_config_reset() -> Any:
    try:
        cfg = load_config()
        active_profile = _charger_profile_name(request.args.get("profile"), _charger_profile_name(_active_profile_name(cfg)))
        cfg["automation"]["active_profile"] = active_profile
        defaults = default_energy_config(
            charger_cfg=cfg.get("charger"),
            legacy_profile=active_automation_config(cfg.get("automation")),
        )
        effective = _save_effective_energy_config(cfg, defaults, profile_name=active_profile)
        return jsonify({"ok": True, "effective": effective})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/energy/status")
def api_energy_status() -> Any:
    try:
        cfg = load_config()
        payload = _load_energy_status(cfg)
        payload["tesla_energy"] = TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database")).read_status()
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def _sample_energy_debug_trace(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or len(points) <= limit:
        return points

    command_indices = [index for index, point in enumerate(points) if point.get("command_type")]
    required_indices = {0, len(points) - 1, *command_indices}
    if len(required_indices) > limit:
        required_indices = {0, len(points) - 1}
        command_slots = max(0, limit - len(required_indices))
        if command_slots:
            required_indices.update(command_indices[-command_slots:])

    remaining_slots = limit - len(required_indices)
    if remaining_slots > 0:
        candidates = [index for index in range(len(points)) if index not in required_indices]
        for slot in range(remaining_slots):
            candidate_offset = min(
                len(candidates) - 1,
                int(slot * len(candidates) / remaining_slots),
            )
            required_indices.add(candidates[candidate_offset])

    return [points[index] for index in sorted(required_indices)]


@app.get("/api/energy/debug-trace")
def api_energy_debug_trace() -> Any:
    start_raw = request.args.get("start")
    end_raw = request.args.get("end")
    if bool(start_raw) != bool(end_raw):
        return jsonify({"ok": False, "error": "start and end must be provided together"}), 400

    try:
        hours = max(0.25, min(48.0, float(request.args.get("hours", "6"))))
    except ValueError:
        return jsonify({"ok": False, "error": "hours must be a number"}), 400
    try:
        limit = max(10, min(10000, int(request.args.get("limit", "720"))))
    except ValueError:
        return jsonify({"ok": False, "error": "limit must be an integer"}), 400

    now = current_local_time()
    until = now
    since = now - timedelta(hours=hours)
    if start_raw and end_raw:
        try:
            since = datetime.fromisoformat(start_raw)
            until = datetime.fromisoformat(end_raw)
        except ValueError:
            return jsonify({"ok": False, "error": "start and end must be ISO date-times"}), 400
        if since.tzinfo is None:
            since = since.replace(tzinfo=now.tzinfo)
        if until.tzinfo is None:
            until = until.replace(tzinfo=now.tzinfo)
        if until <= since:
            return jsonify({"ok": False, "error": "end must be after start"}), 400
        hours = (until - since).total_seconds() / 3600.0

    all_points = load_energy_debug_trace(
        DEFAULT_DEBUG_TRACE_PATH,
        limit=0,
        since=since,
        until=until,
    )
    commands = [point for point in all_points if point.get("command_type")]
    points = _sample_energy_debug_trace(all_points, limit)
    return jsonify(
        {
            "ok": True,
            "hours": hours,
            "limit": limit,
            "generated_at": now.isoformat(),
            "start": since.isoformat(),
            "end": until.isoformat(),
            "total_points": len(all_points),
            "points": points,
            "commands": commands[-40:],
        }
    )


@app.post("/api/energy/decision/preview")
def api_energy_decision_preview() -> Any:
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "JSON object required"}), 400
        cfg = load_config()
        effective = _effective_energy_config(cfg)
        overrides = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        base_effective = {k: v for k, v in effective.items() if k != "active_profile"}
        merged_cfg, errors = validate_energy_config(
            {**base_effective, **overrides},
            charger_cfg=cfg.get("charger"),
            legacy_profile=active_automation_config(cfg.get("automation")),
        )
        if errors:
            return jsonify({"ok": False, "error": "; ".join(errors)}), 400
        config_obj = EnergyConfig(**merged_cfg)
        telemetry_payload = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else {}
        devices_payload = payload.get("devices") if isinstance(payload.get("devices"), dict) else {}
        charger_payload = payload.get("charger") if isinstance(payload.get("charger"), dict) else {}
        now = current_local_time()
        telemetry = EnergyTelemetry(
            solar_watts=telemetry_payload.get("solar_watts"),
            house_consumption_watts=telemetry_payload.get("house_consumption_watts"),
            powerwall_soc=telemetry_payload.get("powerwall_soc"),
            grid_import_watts=telemetry_payload.get("grid_import_watts"),
            grid_export_watts=telemetry_payload.get("grid_export_watts"),
            timestamp=_parse_local_datetime(telemetry_payload.get("timestamp"), now) if telemetry_payload.get("timestamp") else now,
        )
        devices = DeviceStatus(
            hvac_running=bool(devices_payload.get("hvac_running")),
            hvac_changed_at=_parse_local_datetime(devices_payload.get("hvac_changed_at"), now),
            hvac_heating=bool(devices_payload.get("hvac_heating")),
            hvac_heating_changed_at=_parse_local_datetime(devices_payload.get("hvac_heating_changed_at"), now),
        )
        charger_state = ChargerState(
            is_enabled=bool(charger_payload.get("is_enabled")),
            is_charging=bool(charger_payload.get("is_charging")),
            vehicle_connected=bool(charger_payload.get("vehicle_connected")),
            night_session_active=bool(charger_payload.get("night_session_active")),
            current_amps=charger_payload.get("current_amps"),
            current_setpoint_amps=charger_payload.get("current_setpoint_amps"),
            voltage=float(charger_payload.get("voltage", config_obj.ev_voltage)),
            ev_min_amps=max(ABSOLUTE_MIN_AMPS, int(charger_payload.get("ev_min_amps", config_obj.ev_min_amps))),
            solar_max_amps=min(ABSOLUTE_MAX_AMPS, int(charger_payload.get("solar_max_amps", config_obj.ev_solar_max_amps))),
            hard_max_amps=ABSOLUTE_MAX_AMPS,
            emergency_charge_amps=min(ABSOLUTE_MAX_AMPS, int(charger_payload.get("emergency_charge_amps", config_obj.emergency_charge_amps))),
            last_charger_command_at=_parse_local_datetime(charger_payload.get("last_charger_command_at"), now),
            last_charger_command_type=charger_payload.get("last_charger_command_type"),
        )
        decision = decide_energy_action(
            telemetry=telemetry,
            devices=devices,
            charger=charger_state,
            config=config_obj,
            now=now,
            low_solar_stop_counter=int(payload.get("low_solar_stop_counter", 0)),
        )
        return jsonify({"ok": True, "decision": decision.to_dict(), "effective_config": merged_cfg})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/energy/emergency/start")
def api_energy_emergency_start() -> Any:
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}
        cfg = load_config()
        current_effective = _effective_energy_config(cfg)
        amps = payload.get("amps")
        duration_minutes = payload.get("duration_minutes")
        target_kwh = payload.get("target_kwh")
        now = current_local_time()

        target_energy_kwh: float | None = None
        if target_kwh not in (None, ""):
            target_energy_kwh = float(target_kwh)
            if not 0.5 <= target_energy_kwh <= 100:
                raise ValueError("target_kwh must be between 0.5 and 100")
            if duration_minutes in (None, ""):
                # Safety-net window: 1.5x the expected time to deliver the
                # target at the requested amps, so a stalled session cannot
                # keep the override armed forever.
                charger_cfg = cfg.get("charger") or {}
                volts = float(charger_cfg.get("voltage", 230) or 230)
                phases = int(charger_cfg.get("phases", 1) or 1)
                amps_for_estimate = (
                    int(amps)
                    if amps not in (None, "")
                    else int(current_effective.get("emergency_charge_amps", 32) or 32)
                )
                expected_watts = max(1.0, amps_for_estimate * volts * phases)
                expected_minutes = target_energy_kwh * 1000.0 / expected_watts * 60.0
                duration_minutes = int(min(24 * 60, max(30, expected_minutes * 1.5 + 15)))

        updated_cfg = start_emergency_mode(
            current_effective,
            amps=int(amps) if amps not in (None, "") else None,
            duration_minutes=int(duration_minutes) if duration_minutes not in (None, "") else None,
            now=now,
        )
        effective = _save_effective_energy_config(cfg, updated_cfg)
        state = load_automation_state(DEFAULT_STATE_PATH)
        emergency = state.setdefault("emergency", {})
        emergency.update(
            {
                "active": True,
                "started_at": now.isoformat(),
                "seen_charging": False,
                "expires_at": effective.get("emergency_mode_expires_at"),
                "requested_amps": effective.get("emergency_charge_amps"),
                "duration_minutes": effective.get("emergency_mode_duration_minutes"),
                "target_energy_kwh": target_energy_kwh,
                "delivered_energy_wh": 0.0,
                "energy_last_sample_at": None,
                "target_completed_at": None,
                "target_completed_kwh": None,
            }
        )
        # A fresh start is an explicit request for charge: release the night
        # hold left behind by a completed "charge N kWh" request.
        state.setdefault("night", {})["start_blocked_until_disconnect"] = False
        clear_force_stop_state(state)
        with DEFAULT_STATE_PATH.open("w", encoding="utf-8") as f:
            yaml.safe_dump(state, f, sort_keys=False)
        return jsonify({"ok": True, "effective": effective})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def _stop_charging_and_disable_emergency(reason: str) -> Any:
    try:
        cfg = load_config()
        current_effective = _effective_energy_config(cfg)
        now = current_local_time()
        updated_cfg = stop_emergency_mode(current_effective, now=now)
        effective = _save_effective_energy_config(cfg, updated_cfg)
        state = load_automation_state(DEFAULT_STATE_PATH)
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
        request_temporary_force_stop(state, now, reason)
        with DEFAULT_STATE_PATH.open("w", encoding="utf-8") as f:
            yaml.safe_dump(state, f, sort_keys=False)
        return jsonify({"ok": True, "effective": effective})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/energy/emergency/stop")
def api_energy_emergency_stop() -> Any:
    return _stop_charging_and_disable_emergency("emergency_stop")


@app.post("/api/energy/stop-for-unplug")
def api_energy_stop_for_unplug() -> Any:
    return _stop_charging_and_disable_emergency("stop_for_unplug")


@app.get("/api/status")
def api_status() -> Any:
    try:
        cfg = load_config()
        cached = load_status_report_cache()
        if cached:
            age_seconds = _status_cache_age_seconds(cached)
            if age_seconds is None or age_seconds > STATUS_CACHE_LIVE_MAX_AGE_SECONDS:
                payload = build_status_report(cfg)
                payload["status_cache_age_seconds"] = _status_cache_age_seconds(payload)
            else:
                payload = dict(cached)
                payload["source"] = "cache"
                payload["status_cache_age_seconds"] = age_seconds
            if not payload.get("stale") and not payload.get("live_error"):
                payload["stale"] = bool(
                    payload.get("status_cache_age_seconds") is not None
                    and payload.get("status_cache_age_seconds") > 60
                )
        else:
            payload = build_status_report(cfg)
        payload["tesla_energy"] = TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database")).read_status()
        return jsonify(payload)
    except Exception as exc:
        cached = load_status_report_cache()
        if cached:
            fallback = dict(cached)
            fallback["source"] = "cache"
            fallback["stale"] = True
            fallback["live_error"] = str(exc)
            try:
                cfg = load_config()
                fallback["tesla_energy"] = TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database")).read_status()
            except Exception:
                fallback["tesla_energy"] = {
                    "enabled": False,
                    "configured": False,
                    "authorized": False,
                    "error": "Tesla energy status unavailable.",
                }
            return jsonify(fallback)
        return jsonify({"error": str(exc)}), 500


@app.get("/api/tesla-energy/status")
def api_tesla_energy_status() -> Any:
    try:
        cfg = load_config()
        return jsonify(TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database")).read_status())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/tesla-energy/history")
def api_tesla_energy_history() -> Any:
    try:
        cfg = load_config()
        monitor = TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database"))
        return jsonify(
            monitor.chart_history(
                range_name=request.args.get("range", "6h"),
                start=request.args.get("start"),
                end=request.args.get("end"),
            )
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/tesla-energy/import-history")
def api_tesla_energy_import_history() -> Any:
    try:
        cfg = load_config()
        monitor = TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database"))
        days = int(request.args.get("days", request.form.get("days", "90")))
        return jsonify(monitor.import_recent_history(days=days))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/tesla-energy/register-partner")
def api_tesla_energy_register_partner() -> Any:
    try:
        cfg = load_config()
        monitor = TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database"))
        return jsonify(monitor.register_partner_account())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/sessions")
def api_sessions() -> Any:
    try:
        cfg = load_config()
        store = MySQLSessionStore(cfg)
        limit = int(request.args.get("limit", "30"))
        summary = store.session_summary() if store.enabled else {}
        sessions = store.recent_sessions(limit=limit) if store.enabled else []
        return jsonify(
            {
                "database": {
                    "enabled": store.enabled,
                    "ready": store.ensure_ready() if store.enabled else False,
                    "database": store.database if store.enabled else None,
                    "table": store.table if store.enabled else None,
                    "last_error": store.last_error,
                },
                "summary": summary,
                "sessions": sessions,
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/emergency/start")
def api_emergency_start() -> Any:
    return api_energy_emergency_start()


@app.post("/api/emergency/stop")
def api_emergency_stop() -> Any:
    return api_energy_emergency_stop()


@app.get("/tesla/callback")
def tesla_callback() -> Any:
    cfg = load_config()
    monitor = TeslaEnergyMonitor(cfg.get("tesla_energy"), cfg.get("database"))
    if request.args.get("error"):
        message = request.args.get("error_description") or request.args.get("error") or "Tesla authorization failed"
        return redirect(url_for("system_page", tesla_error=message))

    code = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip() or None
    if not code:
        return redirect(url_for("system_page", tesla_error="Tesla callback did not include an authorization code"))

    try:
        monitor.exchange_code(code, state=state)
    except Exception as exc:
        return redirect(url_for("system_page", tesla_error=str(exc)))

    return redirect(url_for("system_page", saved="1", tesla_state="authorized"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8788, debug=False)
