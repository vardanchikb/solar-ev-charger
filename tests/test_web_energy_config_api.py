from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

try:
    import flask  # noqa: F401
    import web_config
except ModuleNotFoundError:
    web_config = None


TEST_CONFIG = {
    "charger": {
        "id": "test",
        "key": "test",
        "ip": "127.0.0.1",
        "version": 3.4,
        "switch_dp": 18,
        "current_dp": 4,
        "mode_dp": 14,
        "current_unit": "amp",
        "min_amps": 8,
        "max_amps": 32,
        "voltage": 240,
        "phases": 1,
    },
    "automation": {
        "active_profile": "summer",
        "profiles": {
            "summer": {
                "poll_seconds": 60,
                "no_charge_start": "16:00",
                "night_charge_start": "21:00",
                "emergency_amps": 32,
            }
        },
    },
    "database": {},
    "tesla_energy": {},
}


@unittest.skipIf(web_config is None, "Flask is not installed in the current test interpreter")
class WebEnergyConfigApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = Path(self.tmpdir.name) / "config.yaml"
        self.state_path = Path(self.tmpdir.name) / "automation_state.yaml"
        self.trace_path = Path(self.tmpdir.name) / "energy_decision_trace.jsonl"
        self.config_path.write_text(yaml.safe_dump(TEST_CONFIG, sort_keys=False), encoding="utf-8")
        self.state_path.write_text("{}", encoding="utf-8")
        web_config.app.config["TESTING"] = True
        self.client = web_config.app.test_client()

    def test_get_energy_config_returns_effective_config(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path):
            response = self.client.get("/api/energy/config")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["active_profile"], "summer")
        self.assertIn("ev_solar_max_amps", payload["effective"])

    def test_put_energy_config_validates_and_persists(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path):
            response = self.client.put(
                "/api/energy/config",
                json={"ev_solar_max_amps": 20, "charger_command_min_interval_seconds": 180},
            )
            self.assertEqual(response.status_code, 200)
            saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        energy = saved["automation"]["profiles"]["summer"]["energy_controller"]
        self.assertEqual(energy["ev_solar_max_amps"], 20)

    def test_energy_config_api_exposes_and_saves_profile_tuning_fields(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path):
            get_response = self.client.get("/api/energy/config")
            self.assertEqual(get_response.status_code, 200)
            get_payload = get_response.get_json()
            self.assertIn("profile_settings", get_payload)

            put_response = self.client.put(
                "/api/energy/config",
                json={
                    "energy_down_sustain_seconds": 240,
                    "ev_solar_max_amps": 19,
                },
            )
            self.assertEqual(put_response.status_code, 200)
            saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

        profile = saved["automation"]["profiles"]["summer"]
        self.assertEqual(profile["energy_down_sustain_seconds"], 240)
        self.assertEqual(profile["energy_controller"]["ev_solar_max_amps"], 19)
        self.assertNotIn("energy_down_sustain_seconds", profile["energy_controller"])

    def test_system_form_saves_hardware_database_and_integration_fields(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path):
            response = self.client.post(
                "/system",
                data={
                    "name": "Aimiler",
                    "ip": "127.0.0.1",
                    "id": "test",
                    "key": "test",
                    "version": "3.4",
                    "switch_dp": "18",
                    "current_dp": "4",
                    "mode_dp": "14",
                    "current_unit": "amp",
                    "min_amps": "8",
                    "max_amps": "32",
                    "voltage": "240",
                    "phases": "1",
                    "hvac_status_url": "http://127.0.0.1:8789/api/hvac-status",
                    "hvac_timeout_seconds": "6",
                    "hvac_refresh_seconds": "90",
                    "db_enabled": "true",
                    "db_bootstrap": "true",
                    "db_socket": "/run/mysqld/mysqld.sock",
                    "db_host": "127.0.0.1",
                    "db_port": "3306",
                    "db_user": "carcharger",
                    "db_name": "carcharger",
                    "db_table": "charging_sessions",
                    "tesla_enabled": "false",
                },
            )
            self.assertEqual(response.status_code, 302)
            saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["charger"]["name"], "Aimiler")
        self.assertEqual(saved["database"]["user"], "carcharger")
        self.assertEqual(saved["automation"]["profiles"]["summer"]["hvac_refresh_seconds"], 90)

    def test_charger_page_only_switches_summer_winter_active_profile(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path):
            response = self.client.post("/charger", data={"automation_active_profile": "winter"})
            self.assertEqual(response.status_code, 302)
            saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["automation"]["active_profile"], "winter")

    def test_energy_config_put_switches_profile_without_touching_system_blocks(self):
        original = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        original["database"] = {"user": "unchanged"}
        original["tesla_energy"] = {"client_id": "unchanged"}
        self.config_path.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

        with patch.object(web_config, "CONFIG_PATH", self.config_path):
            response = self.client.put(
                "/api/energy/config",
                json={"active_profile": "winter", "ev_solar_max_amps": 18},
            )
            self.assertEqual(response.status_code, 200)
            saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["automation"]["active_profile"], "winter")
        self.assertEqual(saved["automation"]["profiles"]["winter"]["energy_controller"]["ev_solar_max_amps"], 18)
        self.assertEqual(saved["database"]["user"], "unchanged")
        self.assertEqual(saved["tesla_energy"]["client_id"], "unchanged")

    def test_put_energy_config_rejects_invalid_value(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path):
            response = self.client.put(
                "/api/energy/config",
                json={"charger_command_min_interval_seconds": 60},
            )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertIn("180", payload["error"])

    def test_reset_energy_config_returns_defaults(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path):
            response = self.client.post("/api/energy/config/reset")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["effective"]["night_charge_amps"], 32)

    def test_preview_endpoint_returns_decision(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path):
            response = self.client.post(
                "/api/energy/decision/preview",
                json={
                    "telemetry": {
                        "solar_watts": 8000,
                        "house_consumption_watts": 2000,
                        "powerwall_soc": 90,
                        "grid_import_watts": 0,
                        "grid_export_watts": 0,
                        "timestamp": "2026-04-28T12:00:00",
                    },
                    "charger": {
                        "vehicle_connected": True,
                        "is_enabled": False,
                        "is_charging": False,
                    },
                },
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("decision", payload)

    def test_emergency_start_and_stop_toggle_state_and_config(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path), patch.object(web_config, "DEFAULT_STATE_PATH", self.state_path):
            start_response = self.client.post("/api/energy/emergency/start", json={"amps": 30, "duration_minutes": 45})
            self.assertEqual(start_response.status_code, 200)
            state = yaml.safe_load(self.state_path.read_text(encoding="utf-8"))
            self.assertTrue(state["emergency"]["active"])
            self.assertEqual(state["emergency"]["duration_minutes"], 45)
            stop_response = self.client.post("/api/energy/emergency/stop")
            self.assertEqual(stop_response.status_code, 200)
            state = yaml.safe_load(self.state_path.read_text(encoding="utf-8"))
            self.assertFalse(state["emergency"]["active"])
            self.assertEqual(state["control"]["force_stop_reason"], "emergency_stop")
            self.assertTrue(state["control"]["force_stop_hold_until_disconnect"])
            self.assertTrue(state["control"]["force_stop_expires_at"])

    def test_stop_for_unplug_requests_temporary_stop_without_emergency_mode(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path), patch.object(web_config, "DEFAULT_STATE_PATH", self.state_path):
            response = self.client.post("/api/energy/stop-for-unplug")
            self.assertEqual(response.status_code, 200)
            state = yaml.safe_load(self.state_path.read_text(encoding="utf-8"))

        self.assertFalse(state["emergency"]["active"])
        self.assertEqual(state["control"]["force_stop_reason"], "stop_for_unplug")
        self.assertTrue(state["control"]["force_stop_hold_until_disconnect"])
        self.assertTrue(state["control"]["force_stop_expires_at"])

    def test_emergency_start_without_duration_defaults_and_clears_force_stop(self):
        existing = {
            "control": {
                "force_stop_requested_at": "2026-05-11T10:20:00-07:00",
                "force_stop_reason": "emergency_stop",
                "force_stop_hold_until_disconnect": True,
            }
        }
        self.state_path.write_text(yaml.safe_dump(existing), encoding="utf-8")
        with patch.object(web_config, "CONFIG_PATH", self.config_path), patch.object(web_config, "DEFAULT_STATE_PATH", self.state_path):
            response = self.client.post("/api/energy/emergency/start", json={"amps": 30})
            self.assertEqual(response.status_code, 200)
            state = yaml.safe_load(self.state_path.read_text(encoding="utf-8"))

        self.assertTrue(state["emergency"]["active"])
        self.assertEqual(state["emergency"]["duration_minutes"], 120)
        self.assertIsNone(state["control"]["force_stop_requested_at"])

    def test_debug_trace_endpoint_returns_recent_points_and_commands(self):
        now = web_config.current_local_time()
        entries = [
            {
                "timestamp": (now - timedelta(minutes=4)).isoformat(),
                "desired_amps": 18,
                "actual_setpoint_amps": 18,
                "mode": "POWERWALL_PROTECT_BEFORE_4PM",
                "computed_available_watts": 4100,
            },
            {
                "timestamp": (now - timedelta(minutes=1)).isoformat(),
                "desired_amps": 20,
                "actual_setpoint_amps": 20,
                "mode": "POWERWALL_PROTECT_BEFORE_4PM",
                "command_type": "SET_AMPS",
                "command_detail": "20A",
                "computed_available_watts": 4700,
            },
        ]
        self.trace_path.write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n",
            encoding="utf-8",
        )
        with patch.object(web_config, "DEFAULT_DEBUG_TRACE_PATH", self.trace_path):
            response = self.client.get("/api/energy/debug-trace?hours=2&limit=50")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["points"]), 2)
        self.assertEqual(len(payload["commands"]), 1)
        self.assertEqual(payload["commands"][0]["command_type"], "SET_AMPS")

    def test_debug_trace_endpoint_filters_explicit_timeline_window(self):
        now = web_config.current_local_time()
        target = {
            "timestamp": (now - timedelta(hours=5)).isoformat(),
            "desired_amps": 12,
            "actual_setpoint_amps": 12,
            "mode": "POWERWALL_PROTECT_BEFORE_4PM",
        }
        newer_entries = [
            {
                "timestamp": (now - timedelta(minutes=index)).isoformat(),
                "desired_amps": 18,
                "actual_setpoint_amps": 18,
                "mode": "POWERWALL_PROTECT_BEFORE_4PM",
            }
            for index in reversed(range(205))
        ]
        self.trace_path.write_text(
            "\n".join(json.dumps(entry) for entry in [target, *newer_entries]) + "\n",
            encoding="utf-8",
        )
        start = (now - timedelta(hours=5, minutes=5)).isoformat()
        end = (now - timedelta(hours=4, minutes=55)).isoformat()
        with patch.object(web_config, "DEFAULT_DEBUG_TRACE_PATH", self.trace_path):
            response = self.client.get(
                "/api/energy/debug-trace",
                query_string={"start": start, "end": end, "limit": "50"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["start"], start)
        self.assertEqual(payload["end"], end)
        self.assertEqual(payload["points"], [target])

    def test_debug_trace_endpoint_requires_both_timeline_bounds(self):
        response = self.client.get(
            "/api/energy/debug-trace",
            query_string={"start": web_config.current_local_time().isoformat()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "start and end must be provided together")

    def test_debug_trace_endpoint_samples_full_timeline_and_preserves_commands(self):
        now = web_config.current_local_time()
        entries = [
            {
                "timestamp": (now - timedelta(minutes=30 - index)).isoformat(),
                "desired_amps": 12 + index % 4,
                "actual_setpoint_amps": 12 + index % 4,
                "mode": "POWERWALL_PROTECT_BEFORE_4PM",
                **({"command_type": "SET_AMPS", "command_detail": "14A"} if index == 13 else {}),
            }
            for index in range(30)
        ]
        self.trace_path.write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n",
            encoding="utf-8",
        )
        with patch.object(web_config, "DEFAULT_DEBUG_TRACE_PATH", self.trace_path):
            response = self.client.get(
                "/api/energy/debug-trace",
                query_string={
                    "start": (now - timedelta(hours=1)).isoformat(),
                    "end": now.isoformat(),
                    "limit": "10",
                },
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total_points"], 30)
        self.assertEqual(len(payload["points"]), 10)
        self.assertEqual(payload["points"][0], entries[0])
        self.assertEqual(payload["points"][-1], entries[-1])
        self.assertIn(entries[13], payload["points"])
        self.assertEqual(payload["commands"], [entries[13]])


if __name__ == "__main__":
    unittest.main()
