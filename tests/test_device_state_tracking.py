from __future__ import annotations

import unittest
from datetime import datetime
import sys
import tempfile
import types
from pathlib import Path

from energy_controller import ChargerState, EnergyDecision, EnergyTelemetry, make_energy_config

if "tinytuya" not in sys.modules:
    tinytuya_stub = types.SimpleNamespace(
        OutletDevice=object,
        scanner=types.SimpleNamespace(devices=lambda **kwargs: {}),
    )
    sys.modules["tinytuya"] = tinytuya_stub

from solar_ev_controller import (
    AimilerCharger,
    AutoScheduleController,
    HVACStatus,
    decode_aimiler_dp6,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class DeviceStateTrackingTests(unittest.TestCase):
    def test_switch_off_partial_status_does_not_inherit_cached_charging_dps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "charger_status_cache.json"
            cache_path.write_text(
                '{"dps":{"3":"charger_charging","4":8,"9":1755,"13":"controlpi_6v_pwm","14":"charge_now","18":true},"_cached_at":"%s"}'
                % datetime.now().astimezone().isoformat(),
                encoding="utf-8",
            )
            charger = AimilerCharger.__new__(AimilerCharger)
            charger.switch_dp = 18
            charger.current_dp = 4
            charger.mode_dp = 14
            charger.current_unit = "amp"
            charger.status_cache_path = cache_path
            charger.status_cache_max_age_seconds = 70

            merged = charger._merge_with_cached_status({"dps": {"18": False}})
            summary = charger.summarize_status(merged)

        self.assertIsNotNone(merged)
        self.assertFalse(summary["enabled"])
        self.assertEqual(summary["power_w"], 0.0)
        self.assertFalse(summary["actively_charging"])
        self.assertEqual(summary["state"], "charger_insert")
        self.assertEqual(summary["pilot_state"], "controlpi_9v")

    def test_missing_switch_status_is_live_degraded_not_failed(self):
        charger = AimilerCharger.__new__(AimilerCharger)
        charger.switch_dp = 18
        charger.current_dp = 4
        charger.mode_dp = 14
        charger.current_unit = "amp"
        charger.status_retry_count = 1
        charger.status_retry_delay_seconds = 0
        charger.socket_persistent = False
        charger._close_socket_if_needed = lambda: None
        charger.device = types.SimpleNamespace(
            status=lambda: {
                "dps": {
                    "3": "charger_free",
                    "4": 8,
                    "9": 0,
                    "13": "controlpi_12v",
                    "14": "charge_now",
                }
            }
        )

        status = charger.status()
        summary = charger.summarize_status(status)

        self.assertTrue(status["_degraded_status"])
        self.assertEqual(status["_missing_required_dps"], ["18"])
        self.assertFalse(summary["switch_state_known"])
        self.assertEqual(summary["status_quality"], "degraded_missing_switch")

    def test_raw_tuya_debug_switch_disables_response_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "tuya_raw_debug.jsonl"
            charger = AimilerCharger.__new__(AimilerCharger)
            charger.debug_tuya_responses = False
            charger.raw_tuya_log_path = log_path

            charger._log_raw_tuya_response("status", {"dps": {"18": True}})

            self.assertFalse(log_path.exists())

    def test_raw_tuya_debug_switch_allows_response_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "tuya_raw_debug.jsonl"
            charger = AimilerCharger.__new__(AimilerCharger)
            charger.debug_tuya_responses = True
            charger.raw_tuya_log_path = log_path
            charger.raw_tuya_log_retention_seconds = 3 * 24 * 60 * 60
            charger._raw_tuya_last_prune_at = None
            charger.device_id = "device-id"
            charger.ip = "192.0.2.10"
            charger.version = 3.4
            charger.socket_persistent = False
            charger.socket_persistent_recycle_seconds = 900
            charger._socket_opened_at = None
            charger.device = types.SimpleNamespace(socket=None)

            charger._log_raw_tuya_response("status", {"dps": {"18": True}})

            self.assertTrue(log_path.exists())
            self.assertIn('"operation":"status"', log_path.read_text(encoding="utf-8"))

    def test_decodes_observed_dp6_voltage_current_power_payload(self):
        decoded = decode_aimiler_dp6("CVoAL6gAC2g=")

        self.assertTrue(decoded["valid"])
        self.assertEqual(decoded["raw_hex"], "095a002fa8000b68")
        self.assertEqual(decoded["voltage_v"], 239.4)
        self.assertEqual(decoded["current_a"], 12.2)
        self.assertEqual(decoded["power_w"], 2920.0)

    def test_summarize_status_surfaces_dp6_telemetry(self):
        charger = AimilerCharger.__new__(AimilerCharger)
        charger.switch_dp = 18
        charger.current_dp = 4
        charger.mode_dp = 14
        charger.current_unit = "amp"

        summary = charger.summarize_status(
            {
                "dps": {
                    "3": "charger_insert",
                    "4": 8,
                    "9": 0,
                    "13": "controlpi_6v",
                    "14": "charge_now",
                    "18": False,
                    "6": "CVoAAAAAAAA=",
                    "10": 0,
                    "24": 27,
                    "101": 81,
                    "104": 4,
                    "106": 11,
                    "107": 11,
                    "108": "GgUYCSIaBRgJJwAEAAs=",
                    "120": "V1:0_0V, A1:0_0.0A, T1:0_45.0, G1:0_499, CP1:1_0.4V",
                }
            }
        )

        self.assertEqual(summary["dp6_telemetry"]["voltage_v"], 239.4)
        self.assertEqual(summary["dp6_telemetry"]["current_a"], 0.0)
        self.assertEqual(summary["dp6_telemetry"]["power_w"], 0.0)
        self.assertEqual(summary["fault_bitmap"], 0)
        self.assertEqual(summary["temp_current_c"], 27)
        self.assertEqual(summary["f_temp"], 81)
        self.assertEqual(summary["charger_time"], 4)
        self.assertEqual(summary["charge_capacity"], 11)
        self.assertEqual(summary["charge_chart"], 11)
        self.assertEqual(summary["charge_record_raw"], "GgUYCSIaBRgJJwAEAAs=")
        self.assertIn("CP1", summary["error_data"])

    def test_dp6_alone_is_not_suspected_fault_or_interlock(self):
        controller = self.make_controller()
        decision = {
            "charger": {
                "state": "charger_insert",
                "pilot_state": "controlpi_6v",
                "actively_charging": False,
                "raw_dps": {"6": "CVoAAAAAAAA="},
            }
        }

        self.assertFalse(controller._suspected_fault_or_interlock(decision))

    def test_dp33_mode_set_does_not_mark_suspected_fault_or_interlock(self):
        controller = self.make_controller()
        decision = {
            "charger": {
                "state": "charger_insert",
                "pilot_state": "controlpi_6v",
                "actively_charging": False,
                "raw_dps": {"33": "AQAAAQEAAAA="},
            }
        }

        self.assertFalse(controller._suspected_fault_or_interlock(decision))

    def test_dp10_fault_bitmap_marks_suspected_fault_or_interlock(self):
        controller = self.make_controller()
        decision = {
            "charger": {
                "state": "charger_insert",
                "pilot_state": "controlpi_6v",
                "actively_charging": False,
                "raw_dps": {"10": 1},
            }
        }

        self.assertTrue(controller._suspected_fault_or_interlock(decision))

    def test_tesla_load_can_infer_charging_when_switch_dp_missing(self):
        controller = self.make_controller()
        controller.charger = types.SimpleNamespace(voltage=230.0, phases=1)
        charger = {
            "status_quality": "degraded_missing_switch",
            "actively_charging": False,
            "vehicle_connected": False,
            "insert_sensed": False,
            "enabled": False,
            "power_w": 0.0,
            "setpoint_amps": 8,
        }
        state = {
            "control": {
                "last_charger_command_type": "START",
                "last_charger_command_at": "2026-05-18T09:18:38-07:00",
                "last_non_ev_watts": 685,
            }
        }
        telemetry = EnergyTelemetry(
            solar_watts=3538.0,
            house_consumption_watts=2264.0,
            powerwall_soc=37.8,
            grid_import_watts=0.0,
            grid_export_watts=0.0,
            timestamp=dt("2026-05-18T09:34:53-07:00"),
        )

        controller._apply_tesla_charging_inference(
            charger,
            state,
            telemetry,
            dt("2026-05-18T09:35:00-07:00"),
        )

        self.assertTrue(charger["actively_charging"])
        self.assertTrue(charger["enabled"])
        self.assertTrue(charger["vehicle_connected"])
        self.assertEqual(charger["status_quality"], "inferred_charging_from_tesla")
        self.assertEqual(charger["power_w"], 1840.0)


    def test_tesla_load_can_infer_charging_when_output_offered_and_tuya_delays(self):
        controller = self.make_controller()
        controller.charger = types.SimpleNamespace(voltage=230.0, phases=1)
        charger = {
            "status_quality": "complete",
            "state": "charger_insert",
            "actively_charging": False,
            "vehicle_connected": True,
            "insert_sensed": True,
            "enabled": True,
            "power_w": 0.0,
            "setpoint_amps": 11,
        }
        state = {
            "control": {
                "last_start_command_at": "2026-05-23T15:26:03-07:00",
                "last_start_setpoint_amps": 8,
                "last_charger_command_type": "SET_AMPS",
                "last_charger_command_at": "2026-05-23T15:29:04-07:00",
                "last_non_ev_watts": 1423,
            }
        }
        telemetry = EnergyTelemetry(
            solar_watts=5060.0,
            house_consumption_watts=3953.0,
            powerwall_soc=100.0,
            grid_import_watts=0.0,
            grid_export_watts=1107.0,
            timestamp=dt("2026-05-23T15:30:16-07:00"),
        )

        controller._apply_tesla_charging_inference(
            charger,
            state,
            telemetry,
            dt("2026-05-23T15:31:05-07:00"),
        )

        self.assertTrue(charger["actively_charging"])
        self.assertTrue(charger["enabled"])
        self.assertTrue(charger["vehicle_connected"])
        self.assertEqual(charger["status_quality"], "inferred_charging_from_tesla")
        self.assertEqual(charger["power_w"], 2530.0)
        self.assertTrue(charger["status_inference"]["offered_output"])

    def test_tesla_load_can_infer_charging_after_recent_start_when_tuya_stays_disabled(self):
        controller = self.make_controller()
        controller.charger = types.SimpleNamespace(voltage=230.0, phases=1)
        charger = {
            "status_quality": "complete",
            "state": "charger_free",
            "pilot_state": "controlpi_12v",
            "actively_charging": False,
            "vehicle_connected": False,
            "insert_sensed": False,
            "enabled": False,
            "power_w": 0.0,
            "setpoint_amps": 8,
        }
        state = {
            "control": {
                "last_charger_command_type": "START",
                "last_charger_command_at": "2026-05-23T15:26:03-07:00",
                "last_start_command_at": "2026-05-23T15:26:03-07:00",
                "last_start_setpoint_amps": 8,
                "last_non_ev_watts": 685,
            }
        }
        telemetry = EnergyTelemetry(
            solar_watts=3500.0,
            house_consumption_watts=2525.0,
            powerwall_soc=60.0,
            grid_import_watts=0.0,
            grid_export_watts=0.0,
            timestamp=dt("2026-05-23T15:29:16-07:00"),
        )

        controller._apply_tesla_charging_inference(
            charger,
            state,
            telemetry,
            dt("2026-05-23T15:30:05-07:00"),
        )

        self.assertTrue(charger["actively_charging"])
        self.assertTrue(charger["enabled"])
        self.assertEqual(charger["status_quality"], "inferred_charging_from_tesla")
        self.assertFalse(charger["status_inference"]["offered_output"])
        self.assertTrue(charger["status_inference"]["recent_start_command"])

    def test_tesla_load_suppresses_post_stop_stale_charging_dps(self):
        controller = self.make_controller()
        controller.charger = types.SimpleNamespace(voltage=230.0, phases=1)
        charger = {
            "status_quality": "complete",
            "actively_charging": True,
            "vehicle_connected": True,
            "insert_sensed": False,
            "enabled": False,
            "power_w": 1653.0,
            "setpoint_amps": 8,
        }
        state = {
            "control": {
                "last_charger_command_type": "STOP",
                "last_charger_command_at": "2026-05-18T09:39:34-07:00",
                "last_non_ev_watts": 2264,
            }
        }
        telemetry = EnergyTelemetry(
            solar_watts=3651.0,
            house_consumption_watts=691.0,
            powerwall_soc=39.33,
            grid_import_watts=0.0,
            grid_export_watts=0.0,
            timestamp=dt("2026-05-18T09:40:23-07:00"),
        )

        controller._apply_tesla_charging_inference(
            charger,
            state,
            telemetry,
            dt("2026-05-18T09:40:48-07:00"),
        )

        self.assertFalse(charger["actively_charging"])
        self.assertFalse(charger["vehicle_connected"])
        self.assertEqual(charger["power_w"], 0.0)
        self.assertEqual(charger["status_quality"], "post_stop_stale_active_suppressed")

    def test_tesla_load_suppresses_post_stop_after_standby_amp_cleanup(self):
        controller = self.make_controller()
        controller.charger = types.SimpleNamespace(voltage=230.0, phases=1)
        charger = {
            "status_quality": "complete",
            "actively_charging": True,
            "vehicle_connected": True,
            "insert_sensed": False,
            "enabled": False,
            "power_w": 2124.0,
            "setpoint_amps": 8,
        }
        state = {
            "control": {
                "last_charger_command_type": "SET_AMPS",
                "last_charger_command_at": "2026-05-20T09:44:43-07:00",
                "last_charger_command_detail": "8A",
                "last_stop_command_at": "2026-05-20T09:41:42-07:00",
                "last_stop_command_detail": "enabled=false",
                "last_non_ev_watts": 2712,
            }
        }
        telemetry = EnergyTelemetry(
            solar_watts=3607.0,
            house_consumption_watts=679.0,
            powerwall_soc=56.14,
            grid_import_watts=0.0,
            grid_export_watts=0.0,
            timestamp=dt("2026-05-20T09:46:35-07:00"),
        )

        controller._apply_tesla_charging_inference(
            charger,
            state,
            telemetry,
            dt("2026-05-20T09:46:44-07:00"),
        )

        self.assertFalse(charger["actively_charging"])
        self.assertFalse(charger["vehicle_connected"])
        self.assertEqual(charger["power_w"], 0.0)
        self.assertEqual(charger["status_quality"], "post_stop_stale_active_suppressed")

    def make_controller(self) -> AutoScheduleController:
        controller = AutoScheduleController.__new__(AutoScheduleController)
        controller.energy_config = make_energy_config(
            {},
            charger_cfg={"min_amps": 8, "max_amps": 32, "voltage": 230},
            legacy_profile={},
        )
        controller._device_state_bootstrapped = False
        controller.night_charge_start_minutes = 21 * 60
        controller.night_new_start_cutoff_minutes = 3 * 60
        controller.energy_down_sustain_seconds = 180
        controller.tesla_solar_control_enabled = True
        controller.auto_force_charge_now_mode = True
        controller.log_fetch_details = False
        return controller

    def test_first_observed_hvac_on_does_not_stamp_change_time(self):
        controller = self.make_controller()
        now = dt("2026-04-29T10:11:59-07:00")
        hvac = HVACStatus(
            hvac_status="COOLING",
            is_running=True,
            is_heating=False,
            is_cooling=True,
            thermostat_mode="COOL",
            observed_at="2026-04-29T17:11:59+00:00",
        )

        device_state, devices = controller._update_device_state({}, hvac, now)

        self.assertTrue(devices.hvac_running)
        self.assertIsNone(devices.hvac_changed_at)
        self.assertIsNone(device_state["hvac_changed_at"])

    def test_bootstrap_does_not_stamp_saved_false_to_live_true_transition(self):
        controller = self.make_controller()
        now = dt("2026-04-29T10:17:55-07:00")
        hvac = HVACStatus(
            hvac_status="COOLING",
            is_running=True,
            is_heating=False,
            is_cooling=True,
            thermostat_mode="COOL",
            observed_at="2026-04-29T17:17:55+00:00",
        )
        prior_state = {
            "device_status": {
                "hvac_running": False,
                "hvac_changed_at": None,
            }
        }

        device_state, devices = controller._update_device_state(prior_state, hvac, now)

        self.assertTrue(devices.hvac_running)
        self.assertIsNone(devices.hvac_changed_at)
        self.assertIsNone(device_state["hvac_changed_at"])

    def test_hvac_unavailable_keeps_prior_state_without_fake_transition(self):
        controller = self.make_controller()
        controller._device_state_bootstrapped = True
        prior_changed_at = "2026-04-29T10:06:00-07:00"
        prior_state = {
            "device_status": {
                "hvac_running": True,
                "hvac_changed_at": prior_changed_at,
            }
        }

        device_state, devices = controller._update_device_state(
            prior_state,
            None,
            dt("2026-04-29T10:25:00-07:00"),
        )

        self.assertTrue(devices.hvac_running)
        self.assertEqual(device_state["hvac_changed_at"], prior_changed_at)
        self.assertEqual(devices.hvac_changed_at, dt(prior_changed_at))

    def test_hvac_heating_transition_is_tracked_separately(self):
        controller = self.make_controller()
        controller._device_state_bootstrapped = True
        now = dt("2026-04-29T10:11:59-07:00")
        hvac = HVACStatus(
            hvac_status="HEATING",
            is_running=True,
            is_heating=True,
            is_cooling=False,
            thermostat_mode="HEAT",
            observed_at="2026-04-29T17:11:59+00:00",
        )
        prior_state = {
            "device_status": {
                "hvac_running": False,
                "hvac_changed_at": None,
                "hvac_heating": False,
                "hvac_heating_changed_at": None,
            }
        }

        device_state, devices = controller._update_device_state(prior_state, hvac, now)

        self.assertFalse(devices.hvac_running)
        self.assertTrue(devices.hvac_heating)
        self.assertIsNone(device_state["hvac_changed_at"])
        self.assertEqual(device_state["hvac_heating_changed_at"], now.isoformat())

    def test_repeated_hvac_heating_on_keeps_original_transition_time(self):
        controller = self.make_controller()
        controller._device_state_bootstrapped = True
        prior_changed_at = "2026-04-29T10:06:00-07:00"
        hvac = HVACStatus(
            hvac_status="HEATING",
            is_running=True,
            is_heating=True,
            is_cooling=False,
            thermostat_mode="HEAT",
            observed_at="2026-04-29T17:11:59+00:00",
        )
        prior_state = {
            "device_status": {
                "hvac_heating": True,
                "hvac_heating_changed_at": prior_changed_at,
            }
        }

        device_state, devices = controller._update_device_state(
            prior_state,
            hvac,
            dt("2026-04-29T10:25:00-07:00"),
        )

        self.assertTrue(devices.hvac_heating)
        self.assertEqual(device_state["hvac_heating_changed_at"], prior_changed_at)
        self.assertEqual(devices.hvac_heating_changed_at, dt(prior_changed_at))

    def test_tesla_catchup_clears_absorbed_predictive_timestamps(self):
        controller = self.make_controller()
        device_state = {
            "hvac_running": True,
            "hvac_changed_at": "2026-04-29T10:06:00-07:00",
            "hvac_heating": True,
            "hvac_heating_changed_at": "2026-04-29T10:06:30-07:00",
        }
        telemetry = EnergyTelemetry(
            solar_watts=4500.0,
            house_consumption_watts=2000.0,
            powerwall_soc=50.0,
            grid_import_watts=0.0,
            grid_export_watts=0.0,
            timestamp=dt("2026-04-29T10:07:00-07:00"),
        )

        cleared = controller._clear_absorbed_predictive_timestamps(device_state, telemetry)

        self.assertIsNone(cleared["hvac_changed_at"])
        self.assertIsNone(cleared["hvac_heating_changed_at"])

    def test_energy_down_hold_delays_low_solar_stop_for_three_minutes(self):
        controller = self.make_controller()
        state = {"control": {}}
        charger = ChargerState(
            is_enabled=True,
            is_charging=True,
            vehicle_connected=True,
            night_session_active=False,
            current_amps=10,
            current_setpoint_amps=10,
            voltage=230,
            ev_min_amps=8,
            solar_max_amps=24,
            hard_max_amps=32,
            emergency_charge_amps=32,
            last_charger_command_at=None,
            last_charger_command_type=None,
            setpoint_history=[],
        )
        decision = EnergyDecision(
            desired_charger_enabled=False,
            desired_amps=None,
            reason="before_4pm_solar_margin_below_min",
            computed_available_watts=600.0,
            adjusted_consumption_watts=5100.0,
            predictive_load_added_watts=0.0,
            predicted_load_components={},
            is_emergency_mode=False,
            command_allowed_now=True,
            cooldown_remaining_seconds=0,
            action_status="READY_TO_COMMAND",
            mode="LOW_SOLAR_STOP",
            telemetry_state="fresh",
            allowed_total_consumption_watts=3860.0,
            non_ev_consumption_watts=3260.0,
            estimated_current_ev_watts=1840.0,
            next_low_solar_stop_counter=3,
        )

        first = controller._apply_energy_down_hold(
            decision,
            charger,
            state,
            dt("2026-05-11T10:18:55-07:00"),
        )
        still_holding = controller._apply_energy_down_hold(
            decision,
            charger,
            state,
            dt("2026-05-11T10:20:55-07:00"),
        )
        released = controller._apply_energy_down_hold(
            decision,
            charger,
            state,
            dt("2026-05-11T10:21:55-07:00"),
        )

        self.assertTrue(first.desired_charger_enabled)
        self.assertEqual(first.desired_amps, 10)
        self.assertEqual(first.action_status, "WAITING_FOR_ENERGY_DOWN_HOLD")
        self.assertTrue(still_holding.desired_charger_enabled)
        self.assertFalse(released.desired_charger_enabled)
        self.assertEqual(released.reason, "before_4pm_solar_margin_below_min")

    def test_energy_down_hold_bypasses_known_predictive_load(self):
        controller = self.make_controller()
        state = {"control": {}}
        charger = ChargerState(
            is_enabled=True,
            is_charging=True,
            vehicle_connected=True,
            night_session_active=False,
            current_amps=20,
            current_setpoint_amps=20,
            voltage=230,
            ev_min_amps=8,
            solar_max_amps=24,
            hard_max_amps=32,
            emergency_charge_amps=32,
            last_charger_command_at=None,
            last_charger_command_type=None,
            setpoint_history=[],
        )
        decision = EnergyDecision(
            desired_charger_enabled=True,
            desired_amps=15,
            reason="solar_following_target_computed",
            computed_available_watts=3600.0,
            adjusted_consumption_watts=5800.0,
            predictive_load_added_watts=1000.0,
            predicted_load_components={"hvac_cooling": 1000.0},
            is_emergency_mode=False,
            command_allowed_now=True,
            cooldown_remaining_seconds=0,
            action_status="READY_TO_COMMAND",
            mode="POWERWALL_PROTECT_BEFORE_4PM",
            telemetry_state="fresh",
            allowed_total_consumption_watts=5600.0,
            non_ev_consumption_watts=2200.0,
            estimated_current_ev_watts=4600.0,
            next_low_solar_stop_counter=0,
        )

        result = controller._apply_energy_down_hold(
            decision,
            charger,
            state,
            dt("2026-05-11T15:24:00-07:00"),
        )

        self.assertEqual(result.desired_amps, 15)
        self.assertEqual(result.reason, "solar_following_target_computed")
        self.assertIsNone(state["control"]["energy_down_pending_target"])

    def test_energy_down_hold_preserves_low_solar_counter_while_holding(self):
        controller = self.make_controller()
        state = {"control": {}}
        charger = ChargerState(
            is_enabled=True,
            is_charging=True,
            vehicle_connected=True,
            night_session_active=False,
            current_amps=10,
            current_setpoint_amps=10,
            voltage=230,
            ev_min_amps=8,
            solar_max_amps=24,
            hard_max_amps=32,
            emergency_charge_amps=32,
            last_charger_command_at=None,
            last_charger_command_type=None,
            setpoint_history=[],
        )
        decision = EnergyDecision(
            desired_charger_enabled=False,
            desired_amps=None,
            reason="before_4pm_solar_margin_below_min",
            computed_available_watts=600.0,
            adjusted_consumption_watts=5100.0,
            predictive_load_added_watts=0.0,
            predicted_load_components={},
            is_emergency_mode=False,
            command_allowed_now=True,
            cooldown_remaining_seconds=0,
            action_status="READY_TO_COMMAND",
            mode="LOW_SOLAR_STOP",
            telemetry_state="fresh",
            allowed_total_consumption_watts=3860.0,
            non_ev_consumption_watts=3260.0,
            estimated_current_ev_watts=1840.0,
            next_low_solar_stop_counter=3,
        )

        held = controller._apply_energy_down_hold(
            decision,
            charger,
            state,
            dt("2026-05-11T10:18:55-07:00"),
        )

        self.assertTrue(held.desired_charger_enabled)
        self.assertEqual(held.next_low_solar_stop_counter, 3)

    def test_energy_down_hold_does_not_reenable_during_post_stop_settling(self):
        controller = self.make_controller()
        state = {"control": {"energy_down_pending_target": "stop", "energy_down_pending_since": "2026-05-11T10:18:55-07:00"}}
        charger = ChargerState(
            is_enabled=False,
            is_charging=True,
            vehicle_connected=True,
            night_session_active=False,
            current_amps=8,
            current_setpoint_amps=8,
            voltage=230,
            ev_min_amps=8,
            solar_max_amps=24,
            hard_max_amps=32,
            emergency_charge_amps=32,
            last_charger_command_at=dt("2026-05-11T10:20:00-07:00"),
            last_charger_command_type="STOP",
            setpoint_history=[],
        )
        decision = EnergyDecision(
            desired_charger_enabled=False,
            desired_amps=None,
            reason="before_4pm_solar_margin_below_min",
            computed_available_watts=600.0,
            adjusted_consumption_watts=5100.0,
            predictive_load_added_watts=0.0,
            predicted_load_components={},
            is_emergency_mode=False,
            command_allowed_now=False,
            cooldown_remaining_seconds=120,
            action_status="WAITING_FOR_CHARGER_COOLDOWN",
            mode="LOW_SOLAR_STOP",
            telemetry_state="fresh",
            allowed_total_consumption_watts=3860.0,
            non_ev_consumption_watts=5100.0,
            estimated_current_ev_watts=0.0,
            next_low_solar_stop_counter=0,
        )

        result = controller._apply_energy_down_hold(
            decision,
            charger,
            state,
            dt("2026-05-11T10:21:00-07:00"),
        )

        self.assertFalse(result.desired_charger_enabled)
        self.assertEqual(result.reason, "before_4pm_solar_margin_below_min")
        self.assertIsNone(state["control"]["energy_down_pending_target"])

    def test_executor_waits_during_post_stop_active_settling(self):
        controller = self.make_controller()
        controller.charger = types.SimpleNamespace(min_amps=8, max_amps=32)
        decision = {
            "phase": "low_solar_stop",
            "base_phase": "low_solar_stop",
            "action": "disable",
            "reason": "before_4pm_solar_margin_below_min",
            "target_mode": None,
            "target_amps": None,
            "charger": {
                "mode": "charge_now",
                "state": "charger_charging",
                "pilot_state": "controlpi_6v_pwm",
                "enabled": False,
                "actively_charging": True,
                "power_w": 1751.0,
                "setpoint_amps": 8,
            },
            "state_after": {
                "control": {
                    "last_charger_command_type": "STOP",
                    "last_charger_command_at": "2026-05-11T10:20:00-07:00",
                    "last_charger_command_detail": "enabled=false",
                },
                "emergency": {},
            },
            "energy": {
                "desired_charger_enabled": False,
                "desired_amps": None,
                "cooldown_remaining_seconds": 120,
                "is_emergency_mode": False,
            },
            "energy_config": {
                "dry_run": True,
                "enable_charger_auto_stop": True,
                "ev_solar_max_amps": 24,
                "ev_min_amps": 8,
            },
            "new_vehicle_connection": False,
        }

        result = controller.apply_decision(decision, dry_run=True)

        self.assertIsNone(result["performed_command"])
        self.assertEqual(result["current_change_deferred"]["command_type"], "COOLDOWN")

    def test_executor_waits_to_stop_recent_start_when_output_enabled_but_not_charging(self):
        controller = self.make_controller()
        controller.charger = types.SimpleNamespace(min_amps=8, max_amps=32)
        decision = {
            "phase": "low_solar_stop",
            "base_phase": "low_solar_stop",
            "action": "disable",
            "reason": "external_start_before_4pm_solar_margin_below_min",
            "target_mode": None,
            "target_amps": None,
            "charger": {
                "mode": "charge_now",
                "state": "charger_insert",
                "pilot_state": "controlpi_9v",
                "enabled": True,
                "actively_charging": False,
                "power_w": 0.0,
                "setpoint_amps": 8,
            },
            "state_after": {
                "control": {
                    "last_charger_command_type": "START",
                    "last_charger_command_at": "2026-05-11T10:20:00-07:00",
                    "last_charger_command_detail": "enabled=true",
                },
                "emergency": {},
            },
            "energy": {
                "desired_charger_enabled": False,
                "desired_amps": None,
                "cooldown_remaining_seconds": 90,
                "is_emergency_mode": False,
            },
            "energy_config": {
                "dry_run": True,
                "enable_charger_auto_stop": True,
                "ev_solar_max_amps": 24,
                "ev_min_amps": 8,
            },
            "new_vehicle_connection": True,
            "force_stop_active": False,
        }

        result = controller.apply_decision(decision, dry_run=True)

        self.assertIsNone(result["performed_command"])
        self.assertEqual(result["current_change_deferred"]["command_type"], "COOLDOWN")

    def test_executor_retries_stop_when_raw_status_remains_active_after_interval(self):
        controller = self.make_controller()
        controller.charger = types.SimpleNamespace(min_amps=8, max_amps=32)
        decision = {
            "phase": "low_solar_stop",
            "base_phase": "low_solar_stop",
            "action": "disable",
            "reason": "before_4pm_solar_margin_below_min",
            "target_mode": None,
            "target_amps": None,
            "charger": {
                "mode": "charge_now",
                "state": "charger_insert",
                "pilot_state": "controlpi_9v",
                "enabled": False,
                "actively_charging": False,
                "power_w": 0.0,
                "setpoint_amps": 8,
            },
            "charger_before_inference": {
                "mode": "charge_now",
                "state": "charger_charging",
                "pilot_state": "controlpi_6v_pwm",
                "enabled": False,
                "actively_charging": True,
                "power_w": 1751.0,
                "setpoint_amps": 8,
            },
            "state_after": {
                "control": {
                    "last_charger_command_type": "STOP",
                    "last_charger_command_at": "2026-05-11T10:20:00-07:00",
                    "last_charger_command_detail": "enabled=false",
                },
                "emergency": {},
            },
            "energy": {
                "desired_charger_enabled": False,
                "desired_amps": None,
                "cooldown_remaining_seconds": 0,
                "is_emergency_mode": False,
            },
            "energy_config": {
                "dry_run": True,
                "enable_charger_auto_stop": True,
                "ev_solar_max_amps": 24,
                "ev_min_amps": 8,
                "charger_command_min_interval_seconds": 180,
            },
            "new_vehicle_connection": False,
        }

        result = controller.apply_decision(decision, dry_run=True)

        self.assertEqual(
            result["performed_command"],
            {"type": "STOP", "detail": "enabled=false retry_after_stale_status"},
        )
        self.assertEqual(result["command_retry_due"]["command_type"], "STOP")

    def test_executor_does_not_retry_start_when_tesla_inferred_charging(self):
        controller = self.make_controller()
        controller.charger = types.SimpleNamespace(min_amps=8, max_amps=32)
        decision = {
            "phase": "day_solar",
            "base_phase": "day_solar",
            "action": "enable",
            "reason": "solar_following_target_computed",
            "target_mode": "charge_now",
            "target_amps": 8,
            "charger": {
                "mode": "charge_now",
                "state": "charger_free",
                "pilot_state": "controlpi_12v",
                "enabled": True,
                "actively_charging": True,
                "power_w": 1840.0,
                "setpoint_amps": 8,
                "status_quality": "inferred_charging_from_tesla",
            },
            "charger_before_inference": {
                "mode": "charge_now",
                "state": "charger_free",
                "pilot_state": "controlpi_12v",
                "enabled": False,
                "actively_charging": False,
                "power_w": 0.0,
                "setpoint_amps": 8,
            },
            "state_after": {
                "control": {
                    "last_charger_command_type": "START",
                    "last_charger_command_at": "2026-05-11T10:20:00-07:00",
                    "last_charger_command_detail": "enabled=true",
                },
                "emergency": {},
            },
            "energy": {
                "desired_charger_enabled": True,
                "desired_amps": 8,
                "cooldown_remaining_seconds": 0,
                "is_emergency_mode": False,
            },
            "energy_config": {
                "dry_run": True,
                "enable_charger_auto_start": True,
                "ev_solar_max_amps": 24,
                "ev_min_amps": 8,
                "charger_command_min_interval_seconds": 180,
            },
            "new_vehicle_connection": False,
        }

        result = controller.apply_decision(decision, dry_run=True)

        self.assertIsNone(result["performed_command"])
        self.assertNotIn("command_retry_due", result)

    def test_force_stop_bypasses_cooldown_and_stops_enabled_charger(self):
        controller = self.make_controller()
        commands = []
        controller.charger = types.SimpleNamespace(
            min_amps=8,
            max_amps=32,
            set_enabled=lambda enabled: commands.append(("set_enabled", enabled)),
        )
        controller._sync_charging_session = lambda *args, **kwargs: None
        controller.session_store = types.SimpleNamespace(
            record_charger_telemetry=lambda *args, **kwargs: None
        )
        decision = {
            "phase": "forced_stop",
            "base_phase": "forced_stop",
            "action": "disable",
            "reason": "emergency_stop",
            "target_mode": None,
            "target_amps": None,
            "charger": {
                "mode": "charge_now",
                "state": "charger_insert",
                "pilot_state": "controlpi_9v",
                "enabled": True,
                "actively_charging": False,
                "power_w": 0.0,
                "setpoint_amps": 32,
            },
            "state_after": {
                "control": {
                    "last_charger_command_type": "START",
                    "last_charger_command_at": "2026-05-11T10:20:00-07:00",
                },
                "emergency": {"active": False},
            },
            "energy": {
                "desired_charger_enabled": False,
                "desired_amps": None,
                "cooldown_remaining_seconds": 120,
                "is_emergency_mode": False,
            },
            "energy_config": {
                "dry_run": False,
                "enable_charger_auto_stop": False,
                "ev_solar_max_amps": 24,
                "ev_min_amps": 8,
            },
            "new_vehicle_connection": False,
            "force_stop_active": True,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            controller.state_path = Path(tmpdir) / "automation_state.yaml"
            result = controller.apply_decision(decision)

        self.assertEqual(commands, [("set_enabled", False)])
        self.assertEqual(result["performed_command"], {"type": "STOP", "detail": "enabled=false"})

    def test_force_stop_hold_persists_until_disconnect(self):
        controller = self.make_controller()
        state = {
            "control": {
                "force_stop_requested_at": "2026-05-11T10:20:00-07:00",
                "force_stop_reason": "emergency_stop",
                "force_stop_hold_until_disconnect": True,
            }
        }

        self.assertTrue(
            controller._force_stop_active(
                state,
                vehicle_connected=True,
                now=dt("2026-05-11T10:30:00-07:00"),
            )
        )
        self.assertTrue(state["control"]["force_stop_requested_at"])
        self.assertTrue(state["control"]["force_stop_expires_at"])
        self.assertFalse(
            controller._force_stop_active(
                state,
                vehicle_connected=False,
                now=dt("2026-05-11T10:31:00-07:00"),
            )
        )
        self.assertIsNone(state["control"]["force_stop_requested_at"])

    def test_force_stop_hold_expires_after_one_hour_when_disconnect_was_missed(self):
        controller = self.make_controller()
        state = {
            "control": {
                "force_stop_requested_at": "2026-05-11T10:20:00-07:00",
                "force_stop_reason": "emergency_stop",
                "force_stop_hold_until_disconnect": True,
            }
        }

        self.assertFalse(
            controller._force_stop_active(
                state,
                vehicle_connected=True,
                now=dt("2026-05-11T11:21:00-07:00"),
            )
        )
        self.assertIsNone(state["control"]["force_stop_requested_at"])
        self.assertEqual(
            state["control"]["force_stop_cleared_request_at"],
            "2026-05-11T10:20:00-07:00",
        )

    def test_cleared_force_stop_is_not_restored_from_stale_disk_state(self):
        controller = self.make_controller()
        state_after = {
            "control": {
                "force_stop_requested_at": "2026-05-11T10:20:00-07:00",
                "force_stop_reason": "emergency_stop",
                "force_stop_hold_until_disconnect": True,
            }
        }
        controller._clear_force_stop(state_after)

        with tempfile.TemporaryDirectory() as tmpdir:
            controller.state_path = Path(tmpdir) / "automation_state.yaml"
            controller.state_path.write_text(
                "control:\n"
                "  force_stop_requested_at: '2026-05-11T10:20:00-07:00'\n"
                "  force_stop_reason: emergency_stop\n"
                "  force_stop_hold_until_disconnect: true\n",
                encoding="utf-8",
            )
            controller._preserve_newer_emergency_state(state_after)

        self.assertIsNone(state_after["control"]["force_stop_requested_at"])

    def test_emergency_session_end_detects_manual_stop_after_charging_seen(self):
        reason = AutoScheduleController._emergency_session_end_reason(
            {"active": True, "seen_charging": True},
            {"enabled": False, "actively_charging": False, "power_w": 0.0},
            vehicle_connected=True,
        )

        self.assertEqual(reason, "emergency_output_disabled")

    def test_executor_does_not_send_standby_amp_cleanup_while_tuya_reports_active(self):
        controller = self.make_controller()
        controller.charger = types.SimpleNamespace(min_amps=8, max_amps=32)
        decision = {
            "phase": "low_solar_stop",
            "base_phase": "low_solar_stop",
            "action": "disable",
            "reason": "before_4pm_solar_margin_below_min",
            "target_mode": None,
            "target_amps": None,
            "charger": {
                "mode": "charge_now",
                "state": "charger_charging",
                "pilot_state": "controlpi_6v_pwm",
                "enabled": False,
                "actively_charging": True,
                "power_w": 1751.0,
                "setpoint_amps": 10,
            },
            "state_after": {
                "control": {
                    "last_charger_command_type": "STOP",
                    "last_charger_command_at": "2026-05-11T10:20:00-07:00",
                    "last_charger_command_detail": "enabled=false",
                },
                "emergency": {},
            },
            "energy": {
                "desired_charger_enabled": False,
                "desired_amps": None,
                "cooldown_remaining_seconds": 0,
                "is_emergency_mode": False,
            },
            "energy_config": {
                "dry_run": True,
                "enable_charger_auto_stop": False,
                "ev_solar_max_amps": 24,
                "ev_min_amps": 8,
            },
            "new_vehicle_connection": False,
        }

        result = controller.apply_decision(decision, dry_run=True)

        self.assertIsNone(result["performed_command"])

    def test_shared_load_policy_updates_energy_target_used_by_executor(self):
        controller = self.make_controller()
        controller.automation_min_amps = 8
        controller.charger = types.SimpleNamespace(min_amps=8, max_amps=32, voltage=230, phases=1)
        decision = {
            "action": "enable",
            "base_phase": "powerwall_protect_before_4pm",
            "target_amps": 17,
            "reason": "solar_following_target_computed",
            "energy": {
                "desired_charger_enabled": True,
                "desired_amps": 17,
                "reason": "solar_following_target_computed",
            },
        }
        hvac = HVACStatus(
            hvac_status="COOLING",
            is_running=True,
            is_heating=False,
            is_cooling=True,
            thermostat_mode="COOL",
            observed_at="2026-05-11T22:24:00+00:00",
        )

        controller._apply_shared_load_policy(decision, hvac)

        self.assertEqual(decision["target_amps"], 8)
        self.assertEqual(decision["energy"]["desired_amps"], 8)
        self.assertEqual(decision["reason"], "hvac_shared_limit")
        self.assertEqual(decision["energy"]["reason"], "hvac_shared_limit")

    def test_normalize_state_preserves_saved_device_status(self):
        controller = self.make_controller()
        state = {
            "device_status": {
                "hvac_running": True,
                "hvac_changed_at": "2026-04-29T10:05:00-07:00",
                "hvac_heating": False,
                "hvac_heating_changed_at": None,
            },
            "decision": {
                "last_reason": "test_reason",
                "last_mode": "LOW_SOLAR_STOP",
            },
        }

        normalized = controller._normalize_state(state, dt("2026-04-29T10:25:00-07:00"))

        self.assertTrue(normalized["device_status"]["hvac_running"])
        self.assertEqual(
            normalized["device_status"]["hvac_changed_at"],
            "2026-04-29T10:05:00-07:00",
        )
        self.assertEqual(normalized["decision"]["last_reason"], "test_reason")


class UpdateDpsRefreshTests(unittest.TestCase):
    @staticmethod
    def make_charger(*, persistent: bool, refresh: bool, socket_open: bool) -> AimilerCharger:
        charger = AimilerCharger.__new__(AimilerCharger)
        charger.switch_dp = 18
        charger.current_dp = 4
        charger.mode_dp = 14
        charger.current_unit = "amp"
        charger.status_retry_count = 1
        charger.status_retry_delay_seconds = 0
        charger.socket_persistent = persistent
        charger.socket_persistent_recycle_seconds = 900
        charger.updatedps_refresh = refresh
        charger.debug_tuya_responses = False
        charger.status_cache_path = Path("/nonexistent/status-cache.json")
        charger._socket_opened_at = None
        charger._socket_identity = None
        calls: list[dict] = []

        def fake_updatedps(index=None, nowait=False):
            calls.append({"index": index, "nowait": nowait})

        charger.device = types.SimpleNamespace(
            socket=object() if socket_open else None,
            updatedps=fake_updatedps,
            status=lambda: {
                "dps": {
                    "3": "charger_free",
                    "4": 8,
                    "9": 0,
                    "13": "controlpi_12v",
                    "14": "charge_now",
                    "18": False,
                }
            },
        )
        charger._updatedps_calls = calls
        return charger

    def test_status_sends_updatedps_on_open_persistent_socket(self):
        charger = self.make_charger(persistent=True, refresh=True, socket_open=True)
        charger.status()
        self.assertEqual(len(charger._updatedps_calls), 1)
        call = charger._updatedps_calls[0]
        self.assertEqual(call["index"], [3, 4, 9, 13, 14, 18])
        self.assertTrue(call["nowait"])

    def test_no_updatedps_when_socket_closed(self):
        charger = self.make_charger(persistent=True, refresh=True, socket_open=False)
        charger.status()
        self.assertEqual(charger._updatedps_calls, [])

    def test_no_updatedps_when_flag_disabled(self):
        charger = self.make_charger(persistent=True, refresh=False, socket_open=True)
        charger.status()
        self.assertEqual(charger._updatedps_calls, [])

    def test_no_updatedps_when_not_persistent(self):
        charger = self.make_charger(persistent=False, refresh=True, socket_open=True)
        charger._close_socket_if_needed = lambda: None
        charger.status()
        self.assertEqual(charger._updatedps_calls, [])

    def test_updatedps_exception_does_not_break_status(self):
        charger = self.make_charger(persistent=True, refresh=True, socket_open=True)

        def boom(index=None, nowait=False):
            raise OSError("socket gone")

        charger.device.updatedps = boom
        charger.raw_tuya_log_path = Path("/nonexistent/never-written.jsonl")
        status = charger.status()
        self.assertIn("dps", status)


class FetchErrorBackoffTests(unittest.TestCase):
    def make_controller(self) -> AutoScheduleController:
        controller = AutoScheduleController.__new__(AutoScheduleController)
        controller.poll_seconds = 15
        controller.fetch_error_backoff_after_failures = 6
        controller.fetch_error_backoff_max_seconds = 600
        controller._last_backoff_logged_seconds = None
        return controller

    @staticmethod
    def decision_with_errors(errors: int) -> dict:
        return {"state_after": {"control": {"consecutive_charger_fetch_errors": errors}}}

    def test_no_backoff_below_threshold(self):
        controller = self.make_controller()
        for errors in (0, 1, 5):
            self.assertEqual(
                controller._sleep_seconds_after_cycle(self.decision_with_errors(errors)),
                15,
            )

    def test_backoff_doubles_and_caps(self):
        controller = self.make_controller()
        self.assertEqual(controller._sleep_seconds_after_cycle(self.decision_with_errors(6)), 30)
        self.assertEqual(controller._sleep_seconds_after_cycle(self.decision_with_errors(8)), 60)
        self.assertEqual(controller._sleep_seconds_after_cycle(self.decision_with_errors(10)), 120)
        self.assertEqual(controller._sleep_seconds_after_cycle(self.decision_with_errors(100)), 600)

    def test_backoff_clears_after_success(self):
        controller = self.make_controller()
        controller._sleep_seconds_after_cycle(self.decision_with_errors(20))
        self.assertEqual(controller._sleep_seconds_after_cycle(self.decision_with_errors(0)), 15)
        self.assertIsNone(controller._last_backoff_logged_seconds)

    def test_backoff_disabled_with_zero_max(self):
        controller = self.make_controller()
        controller.fetch_error_backoff_max_seconds = 0
        self.assertEqual(controller._sleep_seconds_after_cycle(self.decision_with_errors(50)), 15)

    def test_malformed_decision_defaults_to_poll_seconds(self):
        controller = self.make_controller()
        self.assertEqual(controller._sleep_seconds_after_cycle(None), 15)
        self.assertEqual(controller._sleep_seconds_after_cycle({"state_after": None}), 15)


if __name__ == "__main__":
    unittest.main()
