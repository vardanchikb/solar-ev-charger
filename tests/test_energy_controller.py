from __future__ import annotations

import unittest
from datetime import datetime

from energy_controller import (
    ChargerState,
    DeviceStatus,
    EnergyTelemetry,
    decide_energy_action,
    make_energy_config,
    start_emergency_mode,
    validate_energy_config,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class EnergyControllerTests(unittest.TestCase):
    def make_config(self, **overrides):
        return make_energy_config(overrides, charger_cfg={"min_amps": 8, "max_amps": 32, "voltage": 240}, legacy_profile={})

    def make_devices(self, **overrides):
        base = {
            "hvac_running": False,
            "hvac_changed_at": None,
        }
        base.update(overrides)
        return DeviceStatus(**base)

    def make_charger(self, **overrides):
        base = {
            "is_enabled": False,
            "is_charging": False,
            "vehicle_connected": True,
            "night_session_active": False,
            "current_amps": None,
            "current_setpoint_amps": None,
            "voltage": 240.0,
            "ev_min_amps": 8,
            "solar_max_amps": 24,
            "hard_max_amps": 32,
            "emergency_charge_amps": 32,
            "last_charger_command_at": None,
            "last_charger_command_type": None,
        }
        base.update(overrides)
        return ChargerState(**base)

    def test_before_11_not_enough_solar_powerwall_below_80_stops(self):
        now = dt("2026-04-28T10:00:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(3000, 2600, 70, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(), cfg, now)
        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "LOW_SOLAR_STOP")

    def test_ev_estimation_ignores_ev_for_sample_before_external_start(self):
        now = dt("2026-05-12T09:44:18")
        sample_time = dt("2026-05-12T09:42:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(3000, 2600, 70, 0, 0, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=8,
            current_setpoint_amps=8,
            setpoint_history=[(dt("2026-05-12T03:02:00"), 8)],
            ev_active_since=now,
        )
        decision = decide_energy_action(
            telemetry,
            self.make_devices(),
            charger,
            cfg,
            now,
            low_solar_stop_counter=3,
        )

        self.assertEqual(decision.estimated_current_ev_watts, 0.0)
        self.assertEqual(decision.non_ev_consumption_watts, 2600.0)
        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "LOW_SOLAR_STOP")

    def test_ev_estimation_subtracts_near_simultaneous_late_tuya_active_load(self):
        now = dt("2026-05-23T14:52:37")
        sample_time = dt("2026-05-23T14:51:28")
        cfg = self.make_config(ev_voltage=230)
        telemetry = EnergyTelemetry(5467, 3501, 100, 0, 1966, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=8,
            current_setpoint_amps=8,
            voltage=230,
            actual_power_w=1854,
            setpoint_history=[(dt("2026-05-23T14:44:34"), 8)],
            ev_active_since=now,
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertEqual(decision.estimated_current_ev_watts, 1854.0)
        self.assertEqual(decision.non_ev_consumption_watts, 1647.0)
        self.assertTrue(decision.desired_charger_enabled)

    def test_controller_start_waits_for_fresh_telemetry_before_adjusting(self):
        now = dt("2026-05-20T09:51:51")
        sample_time = dt("2026-05-20T09:46:35")
        cfg = self.make_config(
            ev_voltage=230,
            solar_buffer_min_watts=400,
            solar_buffer_max_watts=900,
        )
        telemetry = EnergyTelemetry(3607, 679, 56.14, 0, 0, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=9,
            current_setpoint_amps=11,
            voltage=230,
            last_charger_command_at=dt("2026-05-20T09:47:44"),
            last_charger_command_type="START",
            setpoint_history=[(dt("2026-05-20T09:47:44"), 11)],
            ev_active_since=dt("2026-05-20T09:47:44"),
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.desired_amps, 11)
        self.assertEqual(decision.reason, "waiting_for_post_start_telemetry")

    def test_before_4pm_low_solar_stop_preserves_counter_after_grace(self):
        now = dt("2026-05-18T08:36:28")
        cfg = self.make_config(
            ev_voltage=230,
            solar_buffer_min_watts=400,
            solar_buffer_max_watts=900,
        )
        telemetry = EnergyTelemetry(1894, 2183, 31.65, 289, 0, now)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=8,
            current_setpoint_amps=8,
            voltage=230,
        )

        decision = decide_energy_action(
            telemetry,
            self.make_devices(),
            charger,
            cfg,
            now,
            low_solar_stop_counter=2,
        )

        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "LOW_SOLAR_STOP")
        self.assertEqual(decision.next_low_solar_stop_counter, 3)

    def test_post_stop_active_reading_does_not_restart_without_start_margin(self):
        now = dt("2026-05-18T08:43:01")
        cfg = self.make_config(
            ev_voltage=230,
            solar_buffer_min_watts=400,
            solar_buffer_max_watts=900,
        )
        telemetry = EnergyTelemetry(2036, 677, 32, 0, 0, now)
        charger = self.make_charger(
            is_enabled=False,
            is_charging=True,
            current_amps=8,
            current_setpoint_amps=8,
            voltage=230,
            last_charger_command_type="STOP",
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertEqual(decision.estimated_current_ev_watts, 0.0)
        self.assertEqual(decision.non_ev_consumption_watts, 677.0)
        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "LOW_SOLAR_STOP")

    def test_before_4pm_starts_at_min_when_start_preserves_min_cap(self):
        now = dt("2026-04-28T12:00:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(6000, 2000, 50, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(), cfg, now)
        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "SOLAR_FOLLOW_BEFORE_4PM")
        self.assertEqual(decision.reason, "before_4pm_solar_margin_above_max")
        self.assertEqual(decision.desired_amps, 8)

    def test_cloudy_fallback_after_11_uses_8a(self):
        now = dt("2026-04-28T11:30:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(2000, 2200, 85, 100, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(), cfg, now)
        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.desired_amps, 8)
        self.assertEqual(decision.mode, "CLOUDY_FORCE_MIN_AFTER_11")

    def test_cloudy_fallback_after_11_ignores_low_powerwall_soc_before_4pm(self):
        now = dt("2026-05-03T12:30:00")
        cfg = self.make_config(pre_4pm_powerwall_protect_soc=90)
        telemetry = EnergyTelemetry(1021, 652, 23.55, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(), cfg, now)

        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.desired_amps, 8)
        self.assertEqual(decision.mode, "CLOUDY_FORCE_MIN_AFTER_11")

    def test_daytime_solar_waits_for_vehicle_connection(self):
        now = dt("2026-05-02T09:05:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(6000, 1000, 85, 0, 0, now)
        decision = decide_energy_action(
            telemetry,
            self.make_devices(),
            self.make_charger(vehicle_connected=False, is_enabled=False, is_charging=False),
            cfg,
            now,
        )
        self.assertFalse(decision.desired_charger_enabled)
        self.assertIsNone(decision.desired_amps)
        self.assertEqual(decision.mode, "WAITING_FOR_VEHICLE_CONNECTION")


    def test_after_11_pre_4pm_keeps_new_8a_start_instead_of_stopping(self):
        now = dt("2026-05-23T14:52:37")
        sample_time = dt("2026-05-23T14:51:28")
        cfg = self.make_config(
            ev_voltage=230,
            solar_buffer_min_watts=400,
            solar_buffer_max_watts=900,
        )
        telemetry = EnergyTelemetry(5467, 3501, 100, 0, 1966, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=8,
            current_setpoint_amps=8,
            voltage=230,
            setpoint_history=[(dt("2026-05-23T14:44:34"), 8)],
            ev_active_since=now,
            actual_power_w=1854,
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now, low_solar_stop_counter=3)

        self.assertEqual(decision.estimated_current_ev_watts, 1854.0)
        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.desired_amps, 8)
        self.assertNotEqual(decision.mode, "LOW_SOLAR_STOP")

    def test_start_timestamp_survives_later_amp_change_and_subtracts_ev_load(self):
        now = dt("2026-05-23T15:42:09")
        sample_time = dt("2026-05-23T15:41:22")
        cfg = self.make_config(
            ev_voltage=230,
            solar_buffer_min_watts=400,
            solar_buffer_max_watts=900,
        )
        telemetry = EnergyTelemetry(4904, 3914, 100, 0, 990, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=11,
            current_setpoint_amps=11,
            voltage=230,
            last_charger_command_at=dt("2026-05-23T15:29:04"),
            last_charger_command_type="SET_AMPS",
            setpoint_history=[
                (dt("2026-05-23T15:26:03"), 8),
                (dt("2026-05-23T15:29:04"), 11),
            ],
            ev_active_since=dt("2026-05-23T15:26:03"),
            actual_power_w=2535,
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now, low_solar_stop_counter=3)

        self.assertEqual(decision.estimated_current_ev_watts, 2530.0)
        self.assertEqual(decision.non_ev_consumption_watts, 1384.0)
        self.assertTrue(decision.desired_charger_enabled)
        self.assertNotEqual(decision.mode, "LOW_SOLAR_STOP")

    def test_predictive_hvac_load_reduces_allowance(self):
        now = dt("2026-04-28T12:00:00")
        sample_time = dt("2026-04-28T11:54:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(8000, 3000, 85, 0, 0, sample_time)
        devices = self.make_devices(hvac_running=True, hvac_changed_at=dt("2026-04-28T11:58:00"))
        decision = decide_energy_action(telemetry, devices, self.make_charger(), cfg, now)
        self.assertEqual(decision.predictive_load_added_watts, 3600.0)
        self.assertEqual(decision.predicted_load_components["hvac_cooling"], 3600.0)

    def test_predictive_hvac_heating_adds_500w(self):
        now = dt("2026-04-28T12:00:00")
        sample_time = dt("2026-04-28T11:54:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(8000, 3000, 85, 0, 0, sample_time)
        devices = self.make_devices(
            hvac_heating=True,
            hvac_heating_changed_at=dt("2026-04-28T11:58:00"),
        )
        decision = decide_energy_action(telemetry, devices, self.make_charger(), cfg, now)
        self.assertEqual(decision.predictive_load_added_watts, 500.0)
        self.assertEqual(decision.predicted_load_components["hvac_heating"], 500.0)

    def test_fresh_tesla_timestamp_avoids_double_counting(self):
        now = dt("2026-04-28T12:00:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(8000, 3000, 85, 0, 0, dt("2026-04-28T11:59:00"))
        devices = self.make_devices(hvac_running=True, hvac_changed_at=dt("2026-04-28T11:50:00"))
        decision = decide_energy_action(telemetry, devices, self.make_charger(), cfg, now)
        self.assertEqual(decision.predictive_load_added_watts, 0.0)

    def test_after_9pm_connected_vehicle_charges_32a(self):
        now = dt("2026-04-28T21:10:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(0, 0, 80, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(vehicle_connected=True), cfg, now)
        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.desired_amps, 32)
        self.assertEqual(decision.mode, "NIGHT_CHARGING")

    def test_after_3am_night_window_is_closed(self):
        now = dt("2026-04-29T03:05:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(0, 500, 80, 100, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(vehicle_connected=True), cfg, now)
        self.assertFalse(decision.desired_charger_enabled)
        self.assertNotEqual(decision.mode, "NIGHT_CHARGING")

    def test_after_3am_active_charging_without_tracked_night_session_does_not_continue(self):
        now = dt("2026-04-29T03:05:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(0, 500, 80, 100, 0, now)
        charger = self.make_charger(
            vehicle_connected=True,
            is_enabled=True,
            is_charging=True,
            night_session_active=False,
        )
        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertFalse(decision.desired_charger_enabled)
        self.assertNotEqual(decision.mode, "NIGHT_CONTINUING_AFTER_CUTOFF")

    def test_after_3am_existing_night_session_continues_while_charging(self):
        now = dt("2026-04-29T03:05:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(0, 500, 80, 100, 0, now)
        charger = self.make_charger(
            vehicle_connected=True,
            is_enabled=True,
            is_charging=True,
            night_session_active=True,
        )
        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.desired_amps, 32)
        self.assertEqual(decision.mode, "NIGHT_CONTINUING_AFTER_CUTOFF")

    def test_no_charge_window_stops_when_powerwall_not_full(self):
        now = dt("2026-04-28T16:30:00")
        cfg = self.make_config(full_soc=100)
        telemetry = EnergyTelemetry(5000, 500, 99, 0, 4000, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(), cfg, now)
        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "NO_CHARGE_WINDOW")



    def test_after_4pm_full_powerwall_continues_above_nearly_full_soc(self):
        now = dt("2026-05-23T16:31:45")
        sample_time = dt("2026-05-23T16:31:15")
        cfg = self.make_config(
            ev_voltage=230,
            full_soc=98,
            after_4pm_powerwall_nearly_full_soc=95,
            after_4pm_full_powerwall_export_buffer_watts=600,
            no_charge_start_time="16:00",
            night_charge_start_time="21:00",
        )
        telemetry = EnergyTelemetry(4069, 4069, 97.81, 0, 0, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            vehicle_connected=True,
            current_amps=10,
            current_setpoint_amps=10,
            voltage=230,
            setpoint_history=[(dt("2026-05-23T16:25:55"), 10)],
            ev_active_since=dt("2026-05-23T16:25:55"),
            actual_power_w=2287,
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertNotEqual(decision.mode, "NO_CHARGE_WINDOW")
        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "AFTER_4PM_FULL_POWERWALL_SOLAR_FOLLOW")

    def test_after_4pm_full_powerwall_does_not_start_below_full_soc_from_idle(self):
        now = dt("2026-05-23T16:31:45")
        sample_time = dt("2026-05-23T16:31:15")
        cfg = self.make_config(
            ev_voltage=230,
            full_soc=98,
            after_4pm_powerwall_nearly_full_soc=95,
            after_4pm_full_powerwall_export_buffer_watts=600,
            no_charge_start_time="16:00",
            night_charge_start_time="21:00",
        )
        telemetry = EnergyTelemetry(4069, 1782, 97.81, 0, 2287, sample_time)
        charger = self.make_charger(
            is_enabled=False,
            is_charging=False,
            vehicle_connected=True,
            current_setpoint_amps=8,
            voltage=230,
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "NO_CHARGE_WINDOW")

    def test_after_4pm_manual_enable_waits_for_vehicle_ramp_before_stopping(self):
        now = dt("2026-05-23T17:13:49")
        sample_time = dt("2026-05-23T17:10:03")
        cfg = self.make_config(
            ev_voltage=230,
            full_soc=98,
            after_4pm_powerwall_nearly_full_soc=95,
            after_4pm_full_powerwall_export_buffer_watts=600,
            no_charge_start_time="16:00",
            night_charge_start_time="21:00",
        )
        telemetry = EnergyTelemetry(3247, 1952, 100, 0, 695, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=False,
            vehicle_connected=True,
            current_setpoint_amps=8,
            voltage=230,
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.desired_amps, 8)
        self.assertEqual(decision.reason, "waiting_for_manual_start_telemetry")
        self.assertEqual(decision.mode, "AFTER_4PM_FULL_POWERWALL_SOLAR_FOLLOW")

    def test_after_4pm_full_powerwall_borderline_shortfall_keeps_min_charge(self):
        now = dt("2026-05-23T16:19:51")
        sample_time = dt("2026-05-23T16:14:37")
        cfg = self.make_config(
            ev_voltage=230,
            full_soc=98,
            after_4pm_full_powerwall_export_buffer_watts=600,
            no_charge_start_time="16:00",
            night_charge_start_time="21:00",
        )
        telemetry = EnergyTelemetry(4388, 3886, 100, 0, 502, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            vehicle_connected=True,
            current_amps=9,
            current_setpoint_amps=9,
            voltage=230,
            setpoint_history=[(dt("2026-05-23T16:09:18"), 8)],
            ev_active_since=dt("2026-05-23T16:09:18"),
            actual_power_w=2095,
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now, low_solar_stop_counter=6)

        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.desired_amps, 8)
        self.assertEqual(decision.mode, "AFTER_4PM_FULL_POWERWALL_SOLAR_FOLLOW")
        self.assertEqual(decision.reason, "after_4pm_full_powerwall_min_charge_buffer_tolerance")

    def test_after_4pm_full_powerwall_large_shortfall_still_stops(self):
        now = dt("2026-05-23T16:07:16")
        sample_time = dt("2026-05-23T16:03:32")
        cfg = self.make_config(
            ev_voltage=230,
            full_soc=98,
            after_4pm_full_powerwall_export_buffer_watts=600,
            no_charge_start_time="16:00",
            night_charge_start_time="21:00",
        )
        telemetry = EnergyTelemetry(4539, 3573, 100, 0, 966, sample_time)
        charger = self.make_charger(
            is_enabled=False,
            is_charging=False,
            vehicle_connected=True,
            current_setpoint_amps=8,
            voltage=230,
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "LOW_SOLAR_STOP")

    def test_after_4pm_waits_for_post_start_telemetry(self):
        now = dt("2026-05-23T16:27:00")
        sample_time = dt("2026-05-23T16:25:42")
        cfg = self.make_config(
            ev_voltage=230,
            full_soc=98,
            after_4pm_full_powerwall_export_buffer_watts=600,
            no_charge_start_time="16:00",
            night_charge_start_time="21:00",
        )
        telemetry = EnergyTelemetry(4172, 1839, 100, 0, 2333, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            vehicle_connected=True,
            current_amps=10,
            current_setpoint_amps=10,
            voltage=230,
            last_charger_command_at=dt("2026-05-23T16:25:55"),
            last_charger_command_type="START",
            setpoint_history=[(dt("2026-05-23T16:25:55"), 10)],
            ev_active_since=dt("2026-05-23T16:25:55"),
            actual_power_w=2095,
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now, low_solar_stop_counter=3)

        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.desired_amps, 10)
        self.assertEqual(decision.reason, "waiting_for_post_start_telemetry")

    def test_no_charge_window_blocks_zero_solar_cloudy_fallback(self):
        now = dt("2026-05-02T19:45:00")
        cfg = self.make_config(full_soc=98)
        telemetry = EnergyTelemetry(0, 2266, 97.08, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(), cfg, now)
        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "NO_CHARGE_WINDOW")
        self.assertEqual(decision.reason, "no_charge_window_16_21")

    def test_no_charge_window_blocks_critical_stale_min_charge(self):
        now = dt("2026-05-02T19:45:00")
        sample_time = dt("2026-05-02T19:10:00")
        cfg = self.make_config(
            critical_stale_action="min_charge",
            tesla_stale_after_seconds=600,
            tesla_critical_stale_after_seconds=1800,
        )
        telemetry = EnergyTelemetry(0, 2266, 97.08, 0, 0, sample_time)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(), cfg, now)
        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "SAFETY_STOP")

    def test_after_4pm_full_powerwall_uses_configured_export_buffer(self):
        now = dt("2026-04-28T16:30:00")
        cfg = self.make_config(full_soc=100, after_4pm_full_powerwall_export_buffer_watts=700)
        telemetry = EnergyTelemetry(5000, 500, 100, 0, 4000, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(), cfg, now)
        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "AFTER_4PM_FULL_POWERWALL_SOLAR_FOLLOW")
        self.assertAlmostEqual(decision.allowed_total_consumption_watts or 0, 4300.0)
        self.assertEqual(decision.desired_amps, 15)

    def test_after_4pm_full_powerwall_uses_settled_actual_power_for_vehicle_cap(self):
        now = dt("2026-05-18T16:16:00")
        sample_time = dt("2026-05-18T16:15:30")
        cfg = self.make_config(
            ev_voltage=230,
            full_soc=100,
            after_4pm_full_powerwall_export_buffer_watts=600,
            solar_gap_trim_watts=200,
            solar_gap_boost_watts=800,
        )
        telemetry = EnergyTelemetry(4627, 4448, 100, 0, 179, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=16.8,
            current_setpoint_amps=19,
            voltage=230,
            actual_power_w=3854,
            last_charger_command_at=dt("2026-05-18T16:12:00"),
            last_charger_command_type="SET_AMPS",
            setpoint_history=[(dt("2026-05-18T16:12:00"), 19)],
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertEqual(decision.estimated_current_ev_watts, 3854.0)
        self.assertEqual(decision.non_ev_consumption_watts, 594.0)
        self.assertEqual(decision.desired_amps, 16)

    def test_after_4pm_full_powerwall_ignores_actual_power_before_settled(self):
        now = dt("2026-05-18T16:13:00")
        sample_time = dt("2026-05-18T16:12:30")
        cfg = self.make_config(
            ev_voltage=230,
            full_soc=100,
            after_4pm_full_powerwall_export_buffer_watts=600,
            solar_gap_trim_watts=200,
            solar_gap_boost_watts=800,
        )
        telemetry = EnergyTelemetry(4627, 4448, 100, 0, 179, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=16.8,
            current_setpoint_amps=19,
            voltage=230,
            actual_power_w=3854,
            last_charger_command_at=dt("2026-05-18T16:12:00"),
            last_charger_command_type="SET_AMPS",
            setpoint_history=[(dt("2026-05-18T16:12:00"), 19)],
        )

        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertEqual(decision.estimated_current_ev_watts, 19 * 230.0)
        self.assertEqual(decision.desired_amps, 18)

    def test_cooldown_blocks_new_command(self):
        now = dt("2026-04-28T12:00:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(8000, 1500, 85, 0, 0, now)
        charger = self.make_charger(
            last_charger_command_at=dt("2026-04-28T11:58:30"),
            last_charger_command_type="SET_AMPS",
        )
        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)
        self.assertFalse(decision.command_allowed_now)
        self.assertGreater(decision.cooldown_remaining_seconds, 0)

    def test_emergency_mode_commands_configured_amps(self):
        now = dt("2026-04-28T12:00:00")
        cfg = self.make_config(emergency_charging_enabled=True, emergency_charge_amps=28)
        telemetry = EnergyTelemetry(1000, 4000, 60, 2000, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(), cfg, now)
        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.desired_amps, 28)
        self.assertEqual(decision.mode, "EMERGENCY_CHARGING")

    def test_emergency_start_without_duration_gets_default_expiration(self):
        now = dt("2026-04-28T12:00:00-07:00")
        updated = start_emergency_mode({}, now=now)

        self.assertTrue(updated["emergency_charging_enabled"])
        self.assertEqual(updated["emergency_mode_duration_minutes"], 120)
        self.assertEqual(
            updated["emergency_mode_expires_at"],
            "2026-04-28T14:00:00-07:00",
        )

    def test_no_rule_commands_below_8a(self):
        now = dt("2026-04-28T12:00:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(3500, 1800, 85, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), self.make_charger(), cfg, now)
        self.assertTrue(decision.desired_amps is None or decision.desired_amps >= 8)

    def test_ev_estimation_uses_setpoint_at_telemetry_time(self):
        # Core temporal-consistency fix: use the setpoint active when Tesla took
        # its snapshot, not the current Tuya actual_power_w or current setpoint.
        # Prevents phantom non_ev when setpoint was raised after the snapshot.
        now = dt("2026-04-28T12:00:00")
        sample_time = dt("2026-04-28T11:54:00")  # Tesla snapshot 6 min ago
        cfg = self.make_config()
        telemetry = EnergyTelemetry(6000, 5000, 100, 0, 0, sample_time)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=10.0,         # Tuya actual draw — still ramping
            current_setpoint_amps=24,  # current commanded setpoint (raised after snapshot)
            setpoint_history=[
                (dt("2026-04-28T11:50:00"), 10),  # was 10A when Tesla snapshot was taken
                (dt("2026-04-28T11:58:00"), 24),  # raised to 24A after snapshot
            ],
        )
        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)
        # Must use 10A×240V=2400W (setpoint at snapshot time), not 24A×240V=5760W (current)
        self.assertEqual(decision.estimated_current_ev_watts, 2400.0)
        self.assertEqual(decision.non_ev_consumption_watts, 2600.0)
        self.assertEqual(decision.desired_amps, 24)

    def test_before_4pm_deadband_holds_between_min_and_max(self):
        now = dt("2026-05-11T12:00:00")
        cfg = self.make_config(
            ev_voltage=230,
            solar_buffer_min_watts=300,
            solar_buffer_max_watts=600,
            solar_gap_boost_watts=500,
            solar_gap_trim_watts=200,
        )
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=21,
            current_setpoint_amps=21,
            voltage=230,
        )

        telemetry = EnergyTelemetry(6000, 5520, 60, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertEqual(decision.desired_amps, 21)
        self.assertEqual(decision.reason, "before_4pm_solar_margin_hold")

    def test_before_4pm_deadband_raises_half_remaining_headroom_above_max(self):
        now = dt("2026-05-11T12:00:00")
        cfg = self.make_config(
            ev_voltage=230,
            solar_buffer_min_watts=300,
            solar_buffer_max_watts=600,
            solar_gap_boost_watts=500,
            solar_gap_trim_watts=200,
        )
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=14,
            current_setpoint_amps=14,
            voltage=230,
        )

        telemetry = EnergyTelemetry(6820, 4220, 60, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertEqual(decision.desired_amps, 19)
        self.assertEqual(decision.reason, "before_4pm_solar_margin_above_max")

    def test_before_4pm_deadband_raises_one_amp_for_small_headroom_above_max(self):
        now = dt("2026-05-11T12:00:00")
        cfg = self.make_config(
            ev_voltage=230,
            solar_buffer_min_watts=300,
            solar_buffer_max_watts=600,
            solar_gap_boost_watts=500,
            solar_gap_trim_watts=200,
        )
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=21,
            current_setpoint_amps=21,
            voltage=230,
        )

        telemetry = EnergyTelemetry(6300, 5340, 60, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertEqual(decision.desired_amps, 22)
        self.assertEqual(decision.reason, "before_4pm_solar_margin_above_max")

    def test_before_4pm_deadband_lowers_one_amp_below_min(self):
        now = dt("2026-05-11T12:00:00")
        cfg = self.make_config(
            ev_voltage=230,
            solar_buffer_min_watts=300,
            solar_buffer_max_watts=600,
            solar_gap_boost_watts=500,
            solar_gap_trim_watts=200,
        )
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=21,
            current_setpoint_amps=21,
            voltage=230,
        )

        telemetry = EnergyTelemetry(5600, 5340, 60, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertEqual(decision.desired_amps, 20)

    def test_before_4pm_deadband_lowers_half_remaining_headroom_below_min(self):
        now = dt("2026-05-11T12:00:00")
        cfg = self.make_config(
            ev_voltage=230,
            solar_buffer_min_watts=300,
            solar_buffer_max_watts=600,
            solar_gap_boost_watts=500,
            solar_gap_trim_watts=200,
        )
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=24,
            current_setpoint_amps=24,
            voltage=230,
        )

        telemetry = EnergyTelemetry(4000, 6460, 60, 0, 0, now)
        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)

        self.assertEqual(decision.desired_amps, 18)
        self.assertEqual(decision.reason, "before_4pm_solar_margin_below_min")

    def test_validate_rejects_pre_4pm_cap_gap_below_300w(self):
        _, errors = validate_energy_config(
            {"solar_buffer_min_watts": 300, "solar_buffer_max_watts": 500},
            charger_cfg={"min_amps": 8, "max_amps": 32, "voltage": 240},
            legacy_profile={},
        )
        self.assertTrue(any("300W" in error for error in errors))

    def test_ev_estimation_falls_back_to_current_setpoint_when_no_history(self):
        # Without setpoint history the system uses current_setpoint_amps as the
        # best available estimate (temporally consistent at startup / first loop).
        now = dt("2026-04-28T12:00:00")
        cfg = self.make_config()
        telemetry = EnergyTelemetry(6000, 5000, 100, 0, 0, now)
        charger = self.make_charger(
            is_enabled=True,
            is_charging=True,
            current_amps=10.0,
            current_setpoint_amps=12,
        )
        decision = decide_energy_action(telemetry, self.make_devices(), charger, cfg, now)
        self.assertEqual(decision.estimated_current_ev_watts, 12 * 240.0)
        self.assertEqual(decision.non_ev_consumption_watts, max(0.0, 5000 - 12 * 240.0))

    def test_validate_rejects_cooldown_below_180(self):
        _, errors = validate_energy_config(
            {"charger_command_min_interval_seconds": 60},
            charger_cfg={"min_amps": 8, "max_amps": 32, "voltage": 240},
            legacy_profile={},
        )
        self.assertTrue(any("180" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
