from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from math import ceil, floor
from typing import Any


ABSOLUTE_MIN_AMPS = 8
ABSOLUTE_MAX_AMPS = 32
DEFAULT_ENERGY_CONFIG_VERSION = 1
DEFAULT_EMERGENCY_MODE_DURATION_MINUTES = 120


def parse_hhmm_minutes(value: str) -> int:
    hh, mm = str(value).split(":", maxsplit=1)
    hours = int(hh)
    minutes = int(mm)
    if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
        raise ValueError(f"Invalid HH:MM value: {value}")
    return hours * 60 + minutes


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    return int(value)


def _float(value: Any, default: float) -> float:
    if value in (None, ""):
        return default
    return float(value)


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass
class EnergyTelemetry:
    solar_watts: float | None
    house_consumption_watts: float | None
    powerwall_soc: float | None
    grid_import_watts: float | None
    grid_export_watts: float | None
    timestamp: datetime | None

    def age_seconds(self, now: datetime) -> int | None:
        if self.timestamp is None:
            return None
        return max(0, int((now - self.timestamp).total_seconds()))


@dataclass
class DeviceStatus:
    hvac_running: bool
    hvac_changed_at: datetime | None
    hvac_heating: bool = False
    hvac_heating_changed_at: datetime | None = None


@dataclass
class ChargerState:
    is_enabled: bool
    is_charging: bool
    vehicle_connected: bool
    night_session_active: bool
    current_amps: float | int | None
    current_setpoint_amps: int | None
    voltage: float
    ev_min_amps: int
    solar_max_amps: int
    hard_max_amps: int
    emergency_charge_amps: int
    last_charger_command_at: datetime | None
    last_charger_command_type: str | None
    setpoint_history: list = field(default_factory=list)  # [(datetime, int)] chronological
    ev_active_since: datetime | None = None
    actual_power_w: float | None = None
    # True after a "charge N kWh" request completed while the car stayed
    # plugged in: suppresses night-window auto-charging until disconnect.
    night_charge_blocked: bool = False


@dataclass
class EnergyConfig:
    config_version: int
    updated_at: str | None
    hvac_load_watts: float
    ev_voltage: float
    ev_min_amps: int
    ev_solar_max_amps: int
    ev_hard_max_amps: int
    emergency_charging_enabled: bool
    emergency_charge_amps: int
    emergency_mode_duration_minutes: int | None
    emergency_mode_expires_at: str | None
    pre_4pm_powerwall_protect_soc: float
    after_4pm_powerwall_nearly_full_soc: float
    after_4pm_full_powerwall_export_buffer_watts: float
    full_soc: float
    emergency_low_soc_stop_threshold: float | None
    solar_buffer_watts: float
    solar_buffer_min_watts: float
    solar_buffer_max_watts: float
    solar_gap_boost_watts: float
    solar_gap_trim_watts: float
    cloudy_day_force_charge_time: str
    no_charge_start_time: str
    night_charge_start_time: str
    night_charge_end_time: str
    night_charge_amps: int
    tesla_stale_after_seconds: int
    tesla_critical_stale_after_seconds: int
    control_loop_seconds: int
    charger_command_min_interval_seconds: int
    min_amp_delta_before_update: int
    ramp_up_amps_per_loop: int
    ramp_down_amps_per_loop: int
    low_solar_stop_grace_loop_count: int
    emergency_reduction_bypasses_grace_period: bool
    dry_run: bool
    enable_cloudy_day_fallback: bool
    enable_predictive_hvac_adjustment: bool
    enable_powerwall_protection_before_4pm: bool
    enable_after_4pm_recovery_behavior: bool
    enable_after_4pm_full_powerwall_solar_charging: bool
    enable_charger_auto_start: bool
    enable_charger_auto_stop: bool
    enable_emergency_charging_mode: bool
    critical_stale_action: str
    charge_now_mode: str
    auto_force_charge_now_mode: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnergyDecision:
    desired_charger_enabled: bool
    desired_amps: int | None
    reason: str
    computed_available_watts: float | None
    adjusted_consumption_watts: float | None
    predictive_load_added_watts: float
    predicted_load_components: dict[str, float]
    is_emergency_mode: bool
    command_allowed_now: bool
    cooldown_remaining_seconds: int
    action_status: str
    mode: str
    telemetry_state: str
    allowed_total_consumption_watts: float | None
    non_ev_consumption_watts: float | None
    estimated_current_ev_watts: float
    next_low_solar_stop_counter: int
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_energy_config(
    charger_cfg: dict[str, Any] | None = None,
    legacy_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    charger_cfg = charger_cfg or {}
    legacy_profile = legacy_profile or {}
    hard_max = min(
        ABSOLUTE_MAX_AMPS,
        max(ABSOLUTE_MIN_AMPS, _int(charger_cfg.get("max_amps"), ABSOLUTE_MAX_AMPS)),
    )
    return {
        "config_version": DEFAULT_ENERGY_CONFIG_VERSION,
        "updated_at": None,
        "hvac_load_watts": 3600.0,
        "ev_voltage": _float(charger_cfg.get("voltage"), 240.0),
        "ev_min_amps": max(
            ABSOLUTE_MIN_AMPS,
            _int(charger_cfg.get("min_amps"), ABSOLUTE_MIN_AMPS),
        ),
        "ev_solar_max_amps": min(24, hard_max),
        "ev_hard_max_amps": hard_max,
        "emergency_charging_enabled": False,
        "emergency_charge_amps": min(
            hard_max,
            max(ABSOLUTE_MIN_AMPS, _int(legacy_profile.get("emergency_amps"), hard_max)),
        ),
        "emergency_mode_duration_minutes": None,
        "emergency_mode_expires_at": None,
        "pre_4pm_powerwall_protect_soc": 80.0,
        "after_4pm_powerwall_nearly_full_soc": 95.0,
        "after_4pm_full_powerwall_export_buffer_watts": 300.0,
        "full_soc": 100.0,
        "emergency_low_soc_stop_threshold": None,
        "solar_buffer_watts": 300.0,
        "solar_buffer_min_watts": 200.0,
        "solar_buffer_max_watts": 500.0,
        "solar_gap_boost_watts": 500.0,
        "solar_gap_trim_watts": 200.0,
        "cloudy_day_force_charge_time": "11:00",
        "no_charge_start_time": str(legacy_profile.get("no_charge_start", "16:00")),
        "night_charge_start_time": str(legacy_profile.get("night_charge_start", "21:00")),
        "night_charge_end_time": "03:00",
        "night_charge_amps": ABSOLUTE_MAX_AMPS,
        "tesla_stale_after_seconds": 600,
        "tesla_critical_stale_after_seconds": 1800,
        "control_loop_seconds": max(15, _int(legacy_profile.get("poll_seconds"), 60)),
        "charger_command_min_interval_seconds": max(
            180,
            _int(legacy_profile.get("min_current_change_interval_seconds"), 180),
        ),
        "min_amp_delta_before_update": 1,
        "ramp_up_amps_per_loop": 2,
        "ramp_down_amps_per_loop": 4,
        "low_solar_stop_grace_loop_count": 2,
        "emergency_reduction_bypasses_grace_period": False,
        "dry_run": False,
        "enable_cloudy_day_fallback": True,
        "enable_predictive_hvac_adjustment": True,
        "enable_powerwall_protection_before_4pm": True,
        "enable_after_4pm_recovery_behavior": True,
        "enable_after_4pm_full_powerwall_solar_charging": True,
        "enable_charger_auto_start": True,
        "enable_charger_auto_stop": True,
        "enable_emergency_charging_mode": True,
        "critical_stale_action": "stop",
        "charge_now_mode": str(legacy_profile.get("charge_now_mode", "charge_now")),
        "auto_force_charge_now_mode": _bool(
            legacy_profile.get("auto_force_charge_now_mode"), True
        ),
    }


def validate_energy_config(
    raw_cfg: dict[str, Any] | None,
    charger_cfg: dict[str, Any] | None = None,
    legacy_profile: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    defaults = default_energy_config(charger_cfg=charger_cfg, legacy_profile=legacy_profile)
    raw_cfg = raw_cfg or {}
    cfg = dict(defaults)
    cfg.update(raw_cfg)
    cfg.pop("pre_4pm_consumption_cap_pct", None)           # removed — silently drop from old configs
    cfg.pop("after_4pm_full_powerwall_generation_pct", None)  # removed — silently drop from old configs
    cfg.pop("active_profile", None)  # UI/API context, not an EnergyConfig field
    errors: list[str] = []

    try:
        cfg["hvac_load_watts"] = max(0.0, _float(cfg.get("hvac_load_watts"), defaults["hvac_load_watts"]))
        cfg["ev_voltage"] = _float(cfg.get("ev_voltage"), defaults["ev_voltage"])
        if not 110.0 <= cfg["ev_voltage"] <= 250.0:
            errors.append("EV voltage must be between 110 and 250.")

        cfg["ev_hard_max_amps"] = min(
            ABSOLUTE_MAX_AMPS,
            max(ABSOLUTE_MIN_AMPS, _int(cfg.get("ev_hard_max_amps"), defaults["ev_hard_max_amps"])),
        )
        cfg["ev_min_amps"] = _int(cfg.get("ev_min_amps"), defaults["ev_min_amps"])
        cfg["ev_solar_max_amps"] = _int(cfg.get("ev_solar_max_amps"), defaults["ev_solar_max_amps"])
        cfg["emergency_charge_amps"] = _int(cfg.get("emergency_charge_amps"), defaults["emergency_charge_amps"])
        cfg["night_charge_amps"] = _int(cfg.get("night_charge_amps"), defaults["night_charge_amps"])

        if cfg["ev_min_amps"] < ABSOLUTE_MIN_AMPS:
            errors.append("EV min amps must be at least 8.")
        if cfg["ev_min_amps"] > cfg["ev_hard_max_amps"]:
            errors.append("EV min amps must be less than or equal to the hard max.")
        if cfg["ev_solar_max_amps"] < ABSOLUTE_MIN_AMPS or cfg["ev_solar_max_amps"] > ABSOLUTE_MAX_AMPS:
            errors.append("EV solar-following max amps must be between 8 and 32.")
        if cfg["emergency_charge_amps"] < ABSOLUTE_MIN_AMPS or cfg["emergency_charge_amps"] > ABSOLUTE_MAX_AMPS:
            errors.append("Emergency charge amps must be between 8 and 32.")
        if cfg["night_charge_amps"] < ABSOLUTE_MIN_AMPS or cfg["night_charge_amps"] > ABSOLUTE_MAX_AMPS:
            errors.append("Night charge amps must be between 8 and 32.")
        if cfg["ev_solar_max_amps"] > cfg["ev_hard_max_amps"]:
            errors.append("EV solar-following max amps must be less than or equal to the hard max.")

        cfg["pre_4pm_powerwall_protect_soc"] = _float(
            cfg.get("pre_4pm_powerwall_protect_soc"), defaults["pre_4pm_powerwall_protect_soc"]
        )
        cfg["after_4pm_powerwall_nearly_full_soc"] = _float(
            cfg.get("after_4pm_powerwall_nearly_full_soc"), defaults["after_4pm_powerwall_nearly_full_soc"]
        )
        cfg["after_4pm_full_powerwall_export_buffer_watts"] = max(
            0.0,
            _float(
                cfg.get("after_4pm_full_powerwall_export_buffer_watts"),
                defaults["after_4pm_full_powerwall_export_buffer_watts"],
            ),
        )
        cfg["full_soc"] = _float(cfg.get("full_soc"), defaults["full_soc"])
        threshold = cfg.get("emergency_low_soc_stop_threshold")
        cfg["emergency_low_soc_stop_threshold"] = (
            None if threshold in (None, "") else _float(threshold, 0.0)
        )
        for key in (
            "pre_4pm_powerwall_protect_soc",
            "after_4pm_powerwall_nearly_full_soc",
            "full_soc",
        ):
            if cfg[key] < 0 or cfg[key] > 100:
                errors.append(f"{key} must be between 0 and 100.")
        if (
            cfg["emergency_low_soc_stop_threshold"] is not None
            and not 0 <= cfg["emergency_low_soc_stop_threshold"] <= 100
        ):
            errors.append("Emergency low SOC stop threshold must be between 0 and 100.")
        if cfg["after_4pm_full_powerwall_export_buffer_watts"] % 100 != 0:
            errors.append("After-4PM full Powerwall export buffer must be a 100W increment.")
        if cfg["after_4pm_powerwall_nearly_full_soc"] < cfg["pre_4pm_powerwall_protect_soc"]:
            errors.append("After-4PM recovery SOC must be greater than or equal to the pre-4PM protection SOC.")

        cfg["solar_buffer_watts"] = max(0.0, _float(cfg.get("solar_buffer_watts"), defaults["solar_buffer_watts"]))
        cfg["solar_buffer_min_watts"] = max(0.0, _float(cfg.get("solar_buffer_min_watts"), defaults["solar_buffer_min_watts"]))
        cfg["solar_buffer_max_watts"] = max(0.0, _float(cfg.get("solar_buffer_max_watts"), defaults["solar_buffer_max_watts"]))
        cfg["solar_gap_boost_watts"] = max(0.0, _float(cfg.get("solar_gap_boost_watts"), defaults["solar_gap_boost_watts"]))
        cfg["solar_gap_trim_watts"] = max(0.0, _float(cfg.get("solar_gap_trim_watts"), defaults["solar_gap_trim_watts"]))
        if cfg["solar_buffer_min_watts"] > cfg["solar_buffer_max_watts"]:
            errors.append("Solar buffer minimum watts must be less than or equal to the maximum.")
        if cfg["solar_buffer_max_watts"] - cfg["solar_buffer_min_watts"] < 300:
            errors.append("Solar buffer max must be at least 300W above solar buffer min.")
        if cfg["solar_gap_trim_watts"] > cfg["solar_gap_boost_watts"]:
            errors.append("Solar gap trim watts must be less than or equal to boost watts.")

        for key in (
            "cloudy_day_force_charge_time",
            "no_charge_start_time",
            "night_charge_start_time",
            "night_charge_end_time",
        ):
            parse_hhmm_minutes(str(cfg.get(key)))

        cfg["tesla_stale_after_seconds"] = max(1, _int(cfg.get("tesla_stale_after_seconds"), defaults["tesla_stale_after_seconds"]))
        cfg["tesla_critical_stale_after_seconds"] = max(
            cfg["tesla_stale_after_seconds"],
            _int(cfg.get("tesla_critical_stale_after_seconds"), defaults["tesla_critical_stale_after_seconds"]),
        )
        cfg["control_loop_seconds"] = max(1, _int(cfg.get("control_loop_seconds"), defaults["control_loop_seconds"]))
        cfg["charger_command_min_interval_seconds"] = _int(
            cfg.get("charger_command_min_interval_seconds"),
            defaults["charger_command_min_interval_seconds"],
        )
        if cfg["charger_command_min_interval_seconds"] < 180:
            errors.append("Charger command minimum interval must be at least 180 seconds.")
        cfg["min_amp_delta_before_update"] = max(0, _int(
            cfg.get("min_amp_delta_before_update"),
            defaults["min_amp_delta_before_update"],
        ))
        cfg["ramp_up_amps_per_loop"] = max(1, _int(cfg.get("ramp_up_amps_per_loop"), defaults["ramp_up_amps_per_loop"]))
        cfg["ramp_down_amps_per_loop"] = max(1, _int(cfg.get("ramp_down_amps_per_loop"), defaults["ramp_down_amps_per_loop"]))
        cfg["low_solar_stop_grace_loop_count"] = max(
            0,
            _int(cfg.get("low_solar_stop_grace_loop_count"), defaults["low_solar_stop_grace_loop_count"]),
        )

        cfg["emergency_mode_duration_minutes"] = (
            None
            if cfg.get("emergency_mode_duration_minutes") in (None, "")
            else max(1, _int(cfg.get("emergency_mode_duration_minutes"), 1))
        )
        expires_at = cfg.get("emergency_mode_expires_at")
        if expires_at not in (None, "") and _parse_datetime(expires_at) is None:
            errors.append("Emergency mode expiration must be a valid ISO timestamp.")

        for key in (
            "emergency_charging_enabled",
            "emergency_reduction_bypasses_grace_period",
            "dry_run",
            "enable_cloudy_day_fallback",
            "enable_predictive_hvac_adjustment",
            "enable_powerwall_protection_before_4pm",
            "enable_after_4pm_recovery_behavior",
            "enable_after_4pm_full_powerwall_solar_charging",
            "enable_charger_auto_start",
            "enable_charger_auto_stop",
            "enable_emergency_charging_mode",
            "auto_force_charge_now_mode",
        ):
            cfg[key] = _bool(cfg.get(key), defaults[key])

        critical_action = str(cfg.get("critical_stale_action") or defaults["critical_stale_action"]).strip().lower()
        if critical_action not in {"stop", "min_charge"}:
            errors.append("critical_stale_action must be 'stop' or 'min_charge'.")
        cfg["critical_stale_action"] = critical_action
        cfg["charge_now_mode"] = str(cfg.get("charge_now_mode") or defaults["charge_now_mode"]).strip() or "charge_now"
        cfg["config_version"] = max(1, _int(cfg.get("config_version"), DEFAULT_ENERGY_CONFIG_VERSION))
        cfg["updated_at"] = str(cfg.get("updated_at")) if cfg.get("updated_at") else None
    except ValueError as exc:
        errors.append(str(exc))

    return cfg, errors


def make_energy_config(
    raw_cfg: dict[str, Any] | None,
    charger_cfg: dict[str, Any] | None = None,
    legacy_profile: dict[str, Any] | None = None,
) -> EnergyConfig:
    cfg, errors = validate_energy_config(raw_cfg, charger_cfg=charger_cfg, legacy_profile=legacy_profile)
    if errors:
        raise ValueError("; ".join(errors))
    # Drop any keys that are no longer EnergyConfig fields (e.g. removed Jackery
    # settings still present in an older config.yaml) so a stale config does not
    # crash construction.
    known_fields = {f.name for f in fields(EnergyConfig)}
    cfg = {key: value for key, value in cfg.items() if key in known_fields}
    return EnergyConfig(**cfg)


def _in_window(now_minutes: int, start_minutes: int, end_minutes: int) -> bool:
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes


def _telemetry_state(telemetry: EnergyTelemetry, config: EnergyConfig, now: datetime) -> str:
    age_seconds = telemetry.age_seconds(now)
    if telemetry.timestamp is None:
        return "missing"
    if age_seconds is None:
        return "missing"
    if age_seconds > config.tesla_critical_stale_after_seconds:
        return "critical_stale"
    if age_seconds > config.tesla_stale_after_seconds:
        return "stale"
    return "fresh"


def _predictive_extra_load(
    telemetry: EnergyTelemetry,
    devices: DeviceStatus,
    config: EnergyConfig,
) -> tuple[float, dict[str, float]]:
    if telemetry.timestamp is None:
        return 0.0, {}
    predictive = 0.0
    components: dict[str, float] = {}
    if config.enable_predictive_hvac_adjustment:
        hvac_changed_after = (
            devices.hvac_changed_at is not None
            and devices.hvac_changed_at > telemetry.timestamp
        )
        if devices.hvac_running and hvac_changed_after:
            predictive += config.hvac_load_watts
            components["hvac_cooling"] = config.hvac_load_watts
        elif hvac_changed_after:
            predictive -= config.hvac_load_watts
            components["hvac_cooling_stopped"] = -config.hvac_load_watts

        heating_changed_after = (
            devices.hvac_heating_changed_at is not None
            and devices.hvac_heating_changed_at > telemetry.timestamp
        )
        if devices.hvac_heating and heating_changed_after:
            predictive += 500.0
            components["hvac_heating"] = 500.0
        elif heating_changed_after:
            predictive -= 500.0
            components["hvac_heating_stopped"] = -500.0
    return predictive, components


def _cooldown_remaining(
    charger: ChargerState,
    config: EnergyConfig,
    now: datetime,
) -> int:
    if charger.last_charger_command_at is None:
        return 0
    elapsed = int((now - charger.last_charger_command_at).total_seconds())
    return max(0, config.charger_command_min_interval_seconds - elapsed)


def _solar_deadband_margin(config: "EnergyConfig") -> tuple[float, float]:
    """Return before-4PM solar margin deadband as (trim_below, boost_above)."""
    return config.solar_buffer_min_watts, config.solar_buffer_max_watts


def _setpoint_at_time(
    history: list,
    t: datetime | None,
) -> int | None:
    """Return the most recent setpoint from history that was set at or before time t.
    Falls back to the oldest history entry if all entries post-date t — this handles
    the edge case where commands were sent after the snapshot but before the entry
    could be seeded with a pre-snapshot timestamp."""
    if not history or t is None:
        return None
    result = None
    for entry_time, amps in history:
        if entry_time <= t:
            result = amps
    return result if result is not None else history[0][1]


def _settled_actual_ev_watts(
    charger: ChargerState,
    telemetry_timestamp: datetime | None,
    now: datetime,
    config: EnergyConfig,
) -> float | None:
    if not charger.is_charging:
        return None
    if charger.actual_power_w is None or charger.actual_power_w < 100.0:
        return None
    if charger.current_setpoint_amps is None:
        return None
    if charger.last_charger_command_at is None:
        return None
    if charger.last_charger_command_type not in {"SET_AMPS", "START"}:
        return None
    if telemetry_timestamp is not None and telemetry_timestamp < charger.last_charger_command_at:
        return None
    if (now - charger.last_charger_command_at).total_seconds() < config.charger_command_min_interval_seconds:
        return None

    expected_watts = float(charger.current_setpoint_amps) * charger.voltage
    actual_watts = float(charger.actual_power_w)
    if actual_watts >= expected_watts:
        return None
    return actual_watts


def _current_ev_watts(
    charger: ChargerState,
    telemetry_timestamp: datetime | None = None,
    now: datetime | None = None,
    config: EnergyConfig | None = None,
) -> float:
    if not charger.is_charging:
        return 0.0
    if charger.last_charger_command_type == "STOP" and not charger.is_enabled:
        return 0.0
    if (
        telemetry_timestamp is not None
        and charger.ev_active_since is not None
        and telemetry_timestamp < charger.ev_active_since
    ):
        late_tuya_tolerance_seconds = 120
        if config is not None:
            late_tuya_tolerance_seconds = max(
                late_tuya_tolerance_seconds,
                int(config.control_loop_seconds) * 2,
            )
        late_tuya_seconds = (charger.ev_active_since - telemetry_timestamp).total_seconds()
        if 0 <= late_tuya_seconds <= late_tuya_tolerance_seconds:
            if charger.actual_power_w is not None and charger.actual_power_w >= 100.0:
                return float(charger.actual_power_w)
            amps = charger.current_setpoint_amps or charger.current_amps or charger.ev_min_amps
            return max(0.0, float(amps) * charger.voltage)
        return 0.0
    if now is not None and config is not None:
        actual_watts = _settled_actual_ev_watts(charger, telemetry_timestamp, now, config)
        if actual_watts is not None:
            return actual_watts
    # Use the setpoint that was active at the Tesla snapshot time to avoid mixing
    # current Tuya data with stale Tesla data (temporal mismatch causes phantom non_ev).
    amps = (
        _setpoint_at_time(charger.setpoint_history, telemetry_timestamp)
        or charger.current_setpoint_amps
        or charger.current_amps
        or charger.ev_min_amps
    )
    return max(0.0, float(amps) * charger.voltage)


def _apply_ramp_and_delta(
    desired_amps: int,
    charger: ChargerState,
    config: EnergyConfig,
) -> int:
    current = charger.current_setpoint_amps or charger.current_amps or desired_amps
    if desired_amps > current:
        desired_amps = min(desired_amps, current + config.ramp_up_amps_per_loop)
    elif desired_amps < current:
        desired_amps = max(desired_amps, current - config.ramp_down_amps_per_loop)
    return desired_amps


def _half_step_toward(current: int, target: int) -> int:
    if target == current:
        return current
    step = max(1, ceil(abs(target - current) / 2))
    if target > current:
        return current + step
    return current - step


def _before_4pm_deadband_target(
    *,
    solar_margin_watts: float,
    available_for_ev_watts: float,
    charger: ChargerState,
    config: EnergyConfig,
    require_start_margin: bool = False,
) -> int | None:
    trim_below_watts, boost_above_watts = _solar_deadband_margin(config)
    current = charger.current_setpoint_amps or charger.current_amps
    stop_pending = charger.last_charger_command_type == "STOP" and not charger.is_enabled
    if require_start_margin or stop_pending or current is None or not (charger.is_enabled or charger.is_charging):
        start_margin = solar_margin_watts - (config.ev_min_amps * config.ev_voltage)
        return config.ev_min_amps if start_margin >= trim_below_watts else None

    current = int(current)
    max_target = min(config.ev_solar_max_amps, config.ev_hard_max_amps)
    available_target = floor(available_for_ev_watts / max(1.0, config.ev_voltage))
    desired_target = max(config.ev_min_amps, min(max_target, available_target))
    if solar_margin_watts < trim_below_watts:
        if available_target < config.ev_min_amps and current <= config.ev_min_amps:
            return None
        if desired_target >= current:
            desired_target = max(config.ev_min_amps, current - 1)
        return max(config.ev_min_amps, _half_step_toward(current, desired_target))
    if solar_margin_watts > boost_above_watts:
        if desired_target <= current:
            return current
        return min(max_target, _half_step_toward(current, desired_target))
    return current


def _apply_solar_gap_deadband(
    desired_amps: int,
    available_for_ev_watts: float,
    charger: ChargerState,
    config: EnergyConfig,
) -> int:
    current = charger.current_setpoint_amps or charger.current_amps
    if current is None or not (charger.is_enabled or charger.is_charging):
        return desired_amps

    current = int(current)
    gap_watts = available_for_ev_watts - (current * config.ev_voltage)
    if gap_watts < config.solar_gap_trim_watts:
        available_target = floor(available_for_ev_watts / max(1.0, config.ev_voltage))
        target = max(config.ev_min_amps, min(current, available_target))
        return max(config.ev_min_amps, _half_step_toward(current, target))
    if desired_amps == current:
        return desired_amps

    if desired_amps > current and gap_watts < config.solar_gap_boost_watts:
        return current
    if desired_amps < current:
        return current
    return desired_amps


def decide_energy_action(
    telemetry: EnergyTelemetry,
    devices: DeviceStatus,
    charger: ChargerState,
    config: EnergyConfig,
    now: datetime,
    low_solar_stop_counter: int = 0,
) -> EnergyDecision:
    telemetry_state = _telemetry_state(telemetry, config, now)
    cooldown_remaining_seconds = _cooldown_remaining(charger, config, now)
    command_allowed_now = cooldown_remaining_seconds == 0
    now_minutes = now.hour * 60 + now.minute
    cloudy_minutes = parse_hhmm_minutes(config.cloudy_day_force_charge_time)
    no_charge_start_minutes = parse_hhmm_minutes(config.no_charge_start_time)
    night_charge_start_minutes = parse_hhmm_minutes(config.night_charge_start_time)
    night_charge_end_minutes = parse_hhmm_minutes(config.night_charge_end_time)
    before_no_charge_window = now_minutes < no_charge_start_minutes
    in_no_charge_window = _in_window(now_minutes, no_charge_start_minutes, night_charge_start_minutes)

    predictive_load_added_watts, predictive_components = _predictive_extra_load(
        telemetry,
        devices,
        config,
    )
    baseline_consumption = float(telemetry.house_consumption_watts or 0.0)
    adjusted_consumption = baseline_consumption + predictive_load_added_watts
    estimated_current_ev_watts = _current_ev_watts(charger, telemetry.timestamp, now, config)
    non_ev_consumption = max(0.0, adjusted_consumption - estimated_current_ev_watts)

    def decision(
        *,
        enabled: bool,
        amps: int | None,
        reason: str,
        mode: str,
        action_status: str,
        allowed_total_consumption_watts: float | None = None,
        computed_available_watts: float | None = None,
        warning: str | None = None,
        next_low_solar_stop_counter: int = 0,
        is_emergency_mode: bool = False,
    ) -> EnergyDecision:
        if amps is not None:
            amps = max(ABSOLUTE_MIN_AMPS, min(ABSOLUTE_MAX_AMPS, int(amps)))
        return EnergyDecision(
            desired_charger_enabled=enabled,
            desired_amps=amps,
            reason=reason,
            computed_available_watts=computed_available_watts,
            adjusted_consumption_watts=adjusted_consumption,
            predictive_load_added_watts=predictive_load_added_watts,
            predicted_load_components=predictive_components,
            is_emergency_mode=is_emergency_mode,
            command_allowed_now=command_allowed_now,
            cooldown_remaining_seconds=cooldown_remaining_seconds,
            action_status=action_status,
            mode=mode,
            telemetry_state=telemetry_state,
            allowed_total_consumption_watts=allowed_total_consumption_watts,
            non_ev_consumption_watts=non_ev_consumption,
            estimated_current_ev_watts=estimated_current_ev_watts,
            next_low_solar_stop_counter=next_low_solar_stop_counter,
            warning=warning,
        )

    def daytime_enable_decision(
        *,
        amps: int,
        reason: str,
        mode: str,
        action_status: str,
        allowed_total_consumption_watts: float | None = None,
        computed_available_watts: float | None = None,
        warning: str | None = None,
        next_low_solar_stop_counter: int = 0,
    ) -> EnergyDecision:
        if not (charger.vehicle_connected or charger.is_enabled or charger.is_charging):
            return decision(
                enabled=False,
                amps=None,
                reason="waiting_for_vehicle_connection",
                mode="WAITING_FOR_VEHICLE_CONNECTION",
                action_status="NO_CHANGE_NEEDED",
                allowed_total_consumption_watts=allowed_total_consumption_watts,
                computed_available_watts=computed_available_watts,
                warning=warning,
                next_low_solar_stop_counter=next_low_solar_stop_counter,
            )
        return decision(
            enabled=True,
            amps=amps,
            reason=reason,
            mode=mode,
            action_status=action_status,
            allowed_total_consumption_watts=allowed_total_consumption_watts,
            computed_available_watts=computed_available_watts,
            warning=warning,
            next_low_solar_stop_counter=next_low_solar_stop_counter,
        )

    if config.enable_emergency_charging_mode and config.emergency_charging_enabled:
        expires_at = _parse_datetime(config.emergency_mode_expires_at)
        if expires_at is not None and expires_at <= now:
            return decision(
                enabled=False,
                amps=None,
                reason="emergency_mode_expired",
                mode="EMERGENCY_EXPIRED",
                action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
            )
        if (
            config.emergency_low_soc_stop_threshold is not None
            and telemetry.powerwall_soc is not None
            and float(telemetry.powerwall_soc) < config.emergency_low_soc_stop_threshold
        ):
            return decision(
                enabled=False,
                amps=None,
                reason="emergency_mode_blocked_low_soc",
                mode="SAFETY_STOP",
                action_status="SAFETY_STOP",
                is_emergency_mode=True,
            )
        return decision(
            enabled=True,
            amps=max(ABSOLUTE_MIN_AMPS, min(ABSOLUTE_MAX_AMPS, config.emergency_charge_amps)),
            reason=f"Emergency charging mode active: commanding {max(ABSOLUTE_MIN_AMPS, min(ABSOLUTE_MAX_AMPS, config.emergency_charge_amps))} amps.",
            mode="EMERGENCY_CHARGING",
            action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
            is_emergency_mode=True,
        )

    if _in_window(now_minutes, night_charge_start_minutes, night_charge_end_minutes):
        if charger.night_charge_blocked:
            return decision(
                enabled=False,
                amps=None,
                reason="night_charge_blocked_energy_target_reached",
                mode="NIGHT_WAITING",
                action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
            )
        if charger.vehicle_connected or charger.is_charging:
            return decision(
                enabled=True,
                amps=config.night_charge_amps,
                reason="night_charge_window_connected_vehicle",
                mode="NIGHT_CHARGING",
                action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
            )
        return decision(
            enabled=False,
            amps=None,
            reason="night_charge_window_waiting_for_connection",
            mode="NIGHT_WAITING",
            action_status="NO_CHANGE_NEEDED",
        )

    if charger.night_session_active and charger.is_charging and not charger.night_charge_blocked:
        return decision(
            enabled=True,
            amps=config.night_charge_amps,
            reason="night_charge_continuing_after_cutoff",
            mode="NIGHT_CONTINUING_AFTER_CUTOFF",
            action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
        )

    if (
        config.emergency_low_soc_stop_threshold is not None
        and telemetry.powerwall_soc is not None
        and float(telemetry.powerwall_soc) < config.emergency_low_soc_stop_threshold
    ):
        return decision(
            enabled=False,
            amps=None,
            reason="powerwall_soc_below_emergency_stop_threshold",
            mode="SAFETY_STOP",
            action_status="SAFETY_STOP",
        )

    if telemetry_state == "missing":
        return decision(
            enabled=False,
            amps=None,
            reason="tesla_telemetry_missing",
            mode="SAFETY_STOP",
            action_status="SAFETY_STOP",
        )

    if telemetry_state == "critical_stale":
        if (
            config.critical_stale_action == "min_charge"
            and now_minutes >= cloudy_minutes
            and not in_no_charge_window
        ):
            return daytime_enable_decision(
                amps=ABSOLUTE_MIN_AMPS,
                reason="tesla_telemetry_critical_stale_min_charge",
                mode="CRITICAL_STALE_MIN_CHARGE",
                action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
            )
        return decision(
            enabled=False,
            amps=None,
            reason="tesla_telemetry_critical_stale_stop",
            mode="SAFETY_STOP",
            action_status="SAFETY_STOP",
        )

    if telemetry.solar_watts is None or telemetry.house_consumption_watts is None:
        return decision(
            enabled=False,
            amps=None,
            reason="tesla_telemetry_incomplete",
            mode="SAFETY_STOP",
            action_status="SAFETY_STOP",
        )

    solar_watts = max(0.0, float(telemetry.solar_watts))
    powerwall_soc = float(telemetry.powerwall_soc or 0.0)
    solar_margin_watts = solar_watts - adjusted_consumption
    ev_started_after_telemetry = (
        charger.is_charging
        and charger.ev_active_since is not None
        and telemetry.timestamp is not None
        and telemetry.timestamp < charger.ev_active_since
    )

    if in_no_charge_window:
        full_powerwall_start_allowed = powerwall_soc >= config.full_soc
        full_powerwall_continue_allowed = (
            (charger.is_enabled or charger.is_charging)
            and powerwall_soc >= config.after_4pm_powerwall_nearly_full_soc
        )
        if (
            config.enable_after_4pm_full_powerwall_solar_charging
            and solar_watts > 0.0
            and (full_powerwall_start_allowed or full_powerwall_continue_allowed)
        ):
            allowed_total_consumption = max(
                0.0,
                solar_watts - config.after_4pm_full_powerwall_export_buffer_watts,
            )
            mode_name = "AFTER_4PM_FULL_POWERWALL_SOLAR_FOLLOW"
        else:
            return decision(
                enabled=False,
                amps=None,
                reason="no_charge_window_16_21",
                mode="NO_CHARGE_WINDOW",
                action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
            )
    else:
        allowed_total_consumption = max(0.0, solar_watts - config.solar_buffer_min_watts)
        mode_name = "SOLAR_FOLLOW_BEFORE_4PM"

    if solar_watts <= 0.0:
        return decision(
            enabled=False,
            amps=None,
            reason="solar_generation_zero",
            mode="LOW_SOLAR_STOP",
            action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
        )

    if before_no_charge_window:
        mode_name = "SOLAR_FOLLOW_BEFORE_4PM"
        allowed_total_consumption = max(0.0, solar_watts - config.solar_buffer_min_watts)
        available_for_ev_watts = allowed_total_consumption - non_ev_consumption
        if (
            ev_started_after_telemetry
            and charger.last_charger_command_type == "START"
            and charger.is_enabled
            and charger.current_setpoint_amps is not None
        ):
            return daytime_enable_decision(
                amps=int(charger.current_setpoint_amps),
                reason="waiting_for_post_start_telemetry",
                mode=mode_name,
                action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
                allowed_total_consumption_watts=allowed_total_consumption,
                computed_available_watts=available_for_ev_watts,
            )
        if predictive_components and solar_margin_watts < config.solar_buffer_min_watts:
            predictive_amps = floor(available_for_ev_watts / max(1.0, config.ev_voltage))
            if predictive_amps < ABSOLUTE_MIN_AMPS:
                return decision(
                    enabled=False,
                    amps=None,
                    reason="before_4pm_known_load_margin_below_min",
                    mode="LOW_SOLAR_STOP",
                    action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
                    allowed_total_consumption_watts=allowed_total_consumption,
                    computed_available_watts=available_for_ev_watts,
                )
            return daytime_enable_decision(
                amps=max(config.ev_min_amps, min(predictive_amps, config.ev_solar_max_amps, config.ev_hard_max_amps)),
                reason="before_4pm_known_load_margin_below_min",
                mode=mode_name,
                action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
                allowed_total_consumption_watts=allowed_total_consumption,
                computed_available_watts=available_for_ev_watts,
            )
        deadband_amps = _before_4pm_deadband_target(
            solar_margin_watts=solar_margin_watts,
            available_for_ev_watts=available_for_ev_watts,
            charger=charger,
            config=config,
            require_start_margin=ev_started_after_telemetry,
        )
        if deadband_amps is None:
            if (
                config.enable_cloudy_day_fallback
                and now_minutes >= cloudy_minutes
                and not predictive_components
            ):
                return daytime_enable_decision(
                    amps=config.ev_min_amps,
                    reason="Cloudy fallback: keeping EV at 8A after 11:00 before the no-charge window.",
                    mode="CLOUDY_FORCE_MIN_AFTER_11",
                    action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
                    allowed_total_consumption_watts=allowed_total_consumption,
                    computed_available_watts=available_for_ev_watts,
                )
            next_counter = low_solar_stop_counter + 1 if charger.is_enabled or charger.is_charging else 0
            stop_pending = (
                charger.last_charger_command_type == "STOP"
                and (cooldown_remaining_seconds > 0 or not charger.is_enabled)
            )
            if (
                not stop_pending
                and (charger.is_enabled or charger.is_charging)
                and next_counter <= config.low_solar_stop_grace_loop_count
                and not predictive_components
            ):
                return decision(
                    enabled=True,
                    amps=config.ev_min_amps,
                    reason="before_4pm_solar_margin_low_grace",
                    mode="LOW_SOLAR_GRACE",
                    action_status="NO_CHANGE_NEEDED",
                    allowed_total_consumption_watts=allowed_total_consumption,
                    computed_available_watts=available_for_ev_watts,
                    next_low_solar_stop_counter=next_counter,
                )
            return decision(
                enabled=False,
                amps=None,
                reason="before_4pm_solar_margin_below_min",
                mode="LOW_SOLAR_STOP",
                action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
                allowed_total_consumption_watts=allowed_total_consumption,
                computed_available_watts=available_for_ev_watts,
                next_low_solar_stop_counter=next_counter,
            )
        return daytime_enable_decision(
            amps=deadband_amps,
            reason="before_4pm_solar_margin_above_max" if solar_margin_watts > config.solar_buffer_max_watts else (
                "before_4pm_solar_margin_below_min" if solar_margin_watts < config.solar_buffer_min_watts else "before_4pm_solar_margin_hold"
            ),
            mode=mode_name,
            action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
            allowed_total_consumption_watts=allowed_total_consumption,
            computed_available_watts=available_for_ev_watts,
        )
    elif not in_no_charge_window:
        if config.enable_after_4pm_recovery_behavior:
            if powerwall_soc < config.after_4pm_powerwall_nearly_full_soc:
                available_for_ev_watts = allowed_total_consumption - non_ev_consumption
                if available_for_ev_watts >= ABSOLUTE_MIN_AMPS * config.ev_voltage:
                    return daytime_enable_decision(
                        amps=ABSOLUTE_MIN_AMPS,
                        reason="after_4pm_powerwall_recovery_min_charge",
                        mode="AFTER_4PM_POWERWALL_RECOVERY",
                        action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
                        allowed_total_consumption_watts=allowed_total_consumption,
                        computed_available_watts=available_for_ev_watts,
                    )
                return decision(
                    enabled=False,
                    amps=None,
                    reason="after_4pm_powerwall_recovery_stop",
                    mode="AFTER_4PM_POWERWALL_RECOVERY",
                    action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
                    allowed_total_consumption_watts=allowed_total_consumption,
                    computed_available_watts=available_for_ev_watts,
                )
            mode_name = "AFTER_4PM_SOLAR_FOLLOW" if powerwall_soc < config.full_soc else "AFTER_4PM_FULL_POWERWALL"
            allowed_total_consumption = max(0.0, solar_watts - config.solar_buffer_watts)

    available_for_ev_watts = allowed_total_consumption - non_ev_consumption
    if (
        ev_started_after_telemetry
        and charger.last_charger_command_type == "START"
        and charger.is_enabled
        and charger.current_setpoint_amps is not None
    ):
        return daytime_enable_decision(
            amps=int(charger.current_setpoint_amps),
            reason="waiting_for_post_start_telemetry",
            mode=mode_name,
            action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
            allowed_total_consumption_watts=allowed_total_consumption,
            computed_available_watts=available_for_ev_watts,
        )

    manual_start_wait_allowed = (
        charger.is_enabled
        and not charger.is_charging
        and charger.vehicle_connected
        and charger.current_setpoint_amps is not None
        and charger.last_charger_command_type != "STOP"
        and (
            mode_name == "AFTER_4PM_FULL_POWERWALL_SOLAR_FOLLOW"
            or (before_no_charge_window and now_minutes >= cloudy_minutes)
        )
    )
    if manual_start_wait_allowed:
        return daytime_enable_decision(
            amps=int(charger.current_setpoint_amps),
            reason="waiting_for_manual_start_telemetry",
            mode=mode_name,
            action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
            allowed_total_consumption_watts=allowed_total_consumption,
            computed_available_watts=available_for_ev_watts,
        )

    desired_amps = floor(available_for_ev_watts / max(1.0, config.ev_voltage))

    if desired_amps < ABSOLUTE_MIN_AMPS:
        full_powerwall_min_shortfall = (ABSOLUTE_MIN_AMPS * config.ev_voltage) - available_for_ev_watts
        if (
            in_no_charge_window
            and mode_name == "AFTER_4PM_FULL_POWERWALL_SOLAR_FOLLOW"
            and solar_watts > 0.0
            and (charger.vehicle_connected or charger.is_enabled or charger.is_charging)
            and full_powerwall_min_shortfall <= config.ev_voltage
        ):
            return daytime_enable_decision(
                amps=config.ev_min_amps,
                reason="after_4pm_full_powerwall_min_charge_buffer_tolerance",
                mode=mode_name,
                action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
                allowed_total_consumption_watts=allowed_total_consumption,
                computed_available_watts=available_for_ev_watts,
            )
        if (
            config.enable_cloudy_day_fallback
            and now_minutes >= cloudy_minutes
            and before_no_charge_window
        ):
            return daytime_enable_decision(
                amps=ABSOLUTE_MIN_AMPS,
                reason="Cloudy fallback: starting EV at 8A after 11:00 despite insufficient solar.",
                mode="CLOUDY_FORCE_MIN_AFTER_11",
                action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
                allowed_total_consumption_watts=allowed_total_consumption,
                computed_available_watts=available_for_ev_watts,
            )

        next_counter = low_solar_stop_counter + 1 if charger.is_enabled or charger.is_charging else 0
        # Don't restart grace if a STOP command was already sent and is still within cooldown.
        # The charger just hasn't responded yet — issuing more grace loops would cause
        # repeated stop attempts and the counter-reset oscillation.
        stop_pending = (
            charger.last_charger_command_type == "STOP"
            and cooldown_remaining_seconds > 0
        )
        if (
            not stop_pending
            and (charger.is_enabled or charger.is_charging)
            and next_counter <= config.low_solar_stop_grace_loop_count
            and not predictive_components
        ):
            return decision(
                enabled=True,
                amps=ABSOLUTE_MIN_AMPS,
                reason="low_solar_stop_grace_period",
                mode="LOW_SOLAR_GRACE",
                action_status="NO_CHANGE_NEEDED",
                allowed_total_consumption_watts=allowed_total_consumption,
                computed_available_watts=available_for_ev_watts,
                next_low_solar_stop_counter=next_counter,
            )

        return decision(
            enabled=False,
            amps=None,
            reason="insufficient_solar_for_min_charging",
            mode="LOW_SOLAR_STOP",
            action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
            allowed_total_consumption_watts=allowed_total_consumption,
            computed_available_watts=available_for_ev_watts,
            next_low_solar_stop_counter=next_counter,
        )

    desired_amps = max(config.ev_min_amps, min(desired_amps, config.ev_solar_max_amps, config.ev_hard_max_amps))
    if not predictive_components:
        desired_amps = _apply_solar_gap_deadband(
            desired_amps,
            available_for_ev_watts,
            charger,
            config,
        )
        desired_amps = _apply_ramp_and_delta(desired_amps, charger, config)
    current_target = charger.current_setpoint_amps or charger.current_amps or desired_amps
    if abs(desired_amps - current_target) < config.min_amp_delta_before_update:
        desired_amps = current_target

    return daytime_enable_decision(
        amps=desired_amps,
        reason="solar_following_target_computed",
        mode=mode_name,
        action_status="READY_TO_COMMAND" if command_allowed_now else "WAITING_FOR_CHARGER_COOLDOWN",
        allowed_total_consumption_watts=allowed_total_consumption,
        computed_available_watts=available_for_ev_watts,
    )


def start_emergency_mode(
    current_cfg: dict[str, Any],
    *,
    amps: int | None = None,
    duration_minutes: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    updated = dict(current_cfg)
    updated["emergency_charging_enabled"] = True
    if amps is not None:
        updated["emergency_charge_amps"] = max(ABSOLUTE_MIN_AMPS, min(ABSOLUTE_MAX_AMPS, int(amps)))
    duration = (
        DEFAULT_EMERGENCY_MODE_DURATION_MINUTES
        if duration_minutes in (None, "")
        else max(1, int(duration_minutes))
    )
    updated["emergency_mode_duration_minutes"] = duration
    updated["emergency_mode_expires_at"] = _iso_or_none(now + timedelta(minutes=duration))
    updated["updated_at"] = _iso_or_none(now)
    return updated


def stop_emergency_mode(current_cfg: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    updated = dict(current_cfg)
    updated["emergency_charging_enabled"] = False
    updated["emergency_mode_duration_minutes"] = None
    updated["emergency_mode_expires_at"] = None
    updated["updated_at"] = _iso_or_none(now)
    return updated
