from __future__ import annotations

import base64
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

if "tinytuya" not in sys.modules:
    tinytuya_stub = types.SimpleNamespace(
        OutletDevice=object,
        scanner=types.SimpleNamespace(devices=lambda **kwargs: {}),
    )
    sys.modules["tinytuya"] = tinytuya_stub

from solar_ev_controller import (
    AutoScheduleController,
    MySQLSessionStore,
    _rotate_jsonl_log,
    load_energy_debug_trace,
)

try:
    import flask  # noqa: F401
    import web_config
except ModuleNotFoundError:
    web_config = None


def now_local() -> datetime:
    return datetime.now().astimezone()


def make_controller() -> AutoScheduleController:
    return AutoScheduleController.__new__(AutoScheduleController)


def emergency_state(**overrides) -> dict:
    state = {
        "active": True,
        "started_at": now_local().isoformat(),
        "seen_charging": True,
        "expires_at": None,
        "requested_amps": 32,
        "duration_minutes": 120,
        "target_energy_kwh": None,
        "delivered_energy_wh": 0.0,
        "energy_last_sample_at": None,
        "target_completed_at": None,
        "target_completed_kwh": None,
    }
    state.update(overrides)
    return state


class TrackEmergencyEnergyTargetTests(unittest.TestCase):
    def test_accumulates_energy_while_charging(self):
        controller = make_controller()
        now = now_local()
        emergency = emergency_state(
            target_energy_kwh=10.0,
            delivered_energy_wh=100.0,
            energy_last_sample_at=(now - timedelta(minutes=6)).isoformat(),
        )
        state_after = {"emergency": dict(emergency)}
        charger = {"actively_charging": True, "power_w": 5000.0}

        result = controller._track_emergency_energy_target(state_after, emergency, charger, now)

        self.assertTrue(result["active"])
        # 6 minutes at 5 kW adds 500 Wh.
        self.assertAlmostEqual(result["delivered_energy_wh"], 600.0, places=0)
        self.assertEqual(result["energy_last_sample_at"], now.isoformat())

    def test_integration_step_is_capped_against_stale_samples(self):
        controller = make_controller()
        now = now_local()
        emergency = emergency_state(
            target_energy_kwh=10.0,
            delivered_energy_wh=0.0,
            energy_last_sample_at=(now - timedelta(hours=3)).isoformat(),
        )
        state_after = {"emergency": dict(emergency)}
        charger = {"actively_charging": True, "power_w": 4000.0}

        result = controller._track_emergency_energy_target(state_after, emergency, charger, now)

        # Elapsed capped to 0.25 h: at most 1000 Wh from one sample.
        self.assertLessEqual(result["delivered_energy_wh"], 1000.0)

    def test_not_charging_keeps_delivered_energy(self):
        controller = make_controller()
        now = now_local()
        emergency = emergency_state(
            target_energy_kwh=5.0,
            delivered_energy_wh=1234.0,
            energy_last_sample_at=(now - timedelta(minutes=30)).isoformat(),
        )
        state_after = {"emergency": dict(emergency)}
        charger = {"actively_charging": False, "power_w": 0.0}

        result = controller._track_emergency_energy_target(state_after, emergency, charger, now)

        self.assertEqual(result["delivered_energy_wh"], 1234.0)
        self.assertTrue(result["active"])

    def test_clears_override_when_target_reached(self):
        controller = make_controller()
        now = now_local()
        emergency = emergency_state(
            target_energy_kwh=1.0,
            delivered_energy_wh=990.0,
            energy_last_sample_at=(now - timedelta(minutes=6)).isoformat(),
        )
        state_after = {"emergency": dict(emergency)}
        charger = {"actively_charging": True, "power_w": 5000.0}

        result = controller._track_emergency_energy_target(state_after, emergency, charger, now)

        self.assertFalse(result["active"])
        self.assertIsNone(result["target_energy_kwh"])
        self.assertEqual(result["target_completed_at"], now.isoformat())
        self.assertGreaterEqual(result["target_completed_kwh"], 1.0)
        # No force stop: normal automation should resume on its own.
        self.assertNotIn("control", state_after)
        # Night auto-charging is held until the car is unplugged so the night
        # window cannot immediately undo the energy cap.
        self.assertTrue(state_after["night"]["start_blocked_until_disconnect"])

    def test_no_target_is_a_no_op(self):
        controller = make_controller()
        now = now_local()
        emergency = emergency_state(target_energy_kwh=None)
        state_after = {"emergency": dict(emergency)}
        charger = {"actively_charging": True, "power_w": 5000.0}

        result = controller._track_emergency_energy_target(state_after, emergency, charger, now)

        self.assertEqual(result, emergency)


class NightChargeBlockDecisionTests(unittest.TestCase):
    def _decide(self, *, night_charge_blocked: bool, is_charging: bool):
        from energy_controller import (
            ChargerState,
            DeviceStatus,
            EnergyTelemetry,
            decide_energy_action,
            make_energy_config,
        )

        now = datetime.fromisoformat("2026-04-28T22:10:00")
        cfg = make_energy_config(
            {}, charger_cfg={"min_amps": 8, "max_amps": 32, "voltage": 240}, legacy_profile={}
        )
        charger = ChargerState(
            is_enabled=is_charging,
            is_charging=is_charging,
            vehicle_connected=True,
            night_session_active=False,
            current_amps=None,
            current_setpoint_amps=None,
            voltage=240.0,
            ev_min_amps=8,
            solar_max_amps=24,
            hard_max_amps=32,
            emergency_charge_amps=32,
            last_charger_command_at=None,
            last_charger_command_type=None,
            night_charge_blocked=night_charge_blocked,
        )
        telemetry = EnergyTelemetry(0, 0, 80, 0, 0, now)
        return decide_energy_action(telemetry, DeviceStatus(False, None), charger, cfg, now)

    def test_night_window_charges_when_not_blocked(self):
        decision = self._decide(night_charge_blocked=False, is_charging=False)
        self.assertTrue(decision.desired_charger_enabled)
        self.assertEqual(decision.mode, "NIGHT_CHARGING")

    def test_night_window_blocked_after_energy_target(self):
        decision = self._decide(night_charge_blocked=True, is_charging=False)
        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.reason, "night_charge_blocked_energy_target_reached")

    def test_night_window_blocked_stops_active_charging(self):
        decision = self._decide(night_charge_blocked=True, is_charging=True)
        self.assertFalse(decision.desired_charger_enabled)
        self.assertEqual(decision.reason, "night_charge_blocked_energy_target_reached")


class SqlHelperTests(unittest.TestCase):
    def test_sql_number_accepts_numbers(self):
        self.assertEqual(MySQLSessionStore._sql_number(None), "NULL")
        self.assertEqual(MySQLSessionStore._sql_number(5), "5")
        self.assertEqual(MySQLSessionStore._sql_number(5.5), "5.5")
        self.assertEqual(MySQLSessionStore._sql_number(True), "1")
        self.assertEqual(MySQLSessionStore._sql_number("7.25"), "7.25")

    def test_sql_number_rejects_injection_strings(self):
        with self.assertRaises(ValueError):
            MySQLSessionStore._sql_number("1; DROP TABLE charging_sessions")
        with self.assertRaises(ValueError):
            MySQLSessionStore._sql_number("NOW()")

    def test_sql_number_maps_non_finite_to_null(self):
        self.assertEqual(MySQLSessionStore._sql_number(float("nan")), "NULL")
        self.assertEqual(MySQLSessionStore._sql_number(float("inf")), "NULL")

    def test_sql_text_doubles_quotes(self):
        self.assertEqual(MySQLSessionStore._sql_text("O'Brien"), "'O''Brien'")
        self.assertEqual(
            MySQLSessionStore._sql_text("a\\'b"),
            "'a\\\\''b'",
        )


class JsonlRotationTests(unittest.TestCase):
    def _write_entries(self, path: Path, timestamps: list[datetime]) -> None:
        with path.open("w", encoding="utf-8") as f:
            for ts in timestamps:
                f.write(json.dumps({"timestamp": ts.isoformat(), "v": 1}) + "\n")

    def test_rotates_when_oldest_entry_ages_out(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "trace.jsonl"
            now = now_local()
            self._write_entries(path, [now - timedelta(days=4), now])
            _rotate_jsonl_log(path, now - timedelta(days=3), 10**9)
            self.assertFalse(path.exists())
            self.assertTrue(path.with_name("trace.jsonl.1").exists())

    def test_young_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "trace.jsonl"
            now = now_local()
            self._write_entries(path, [now - timedelta(hours=1), now])
            _rotate_jsonl_log(path, now - timedelta(days=3), 10**9)
            self.assertTrue(path.exists())
            self.assertFalse(path.with_name("trace.jsonl.1").exists())

    def test_trace_reader_spans_rotated_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "trace.jsonl"
            now = now_local()
            self._write_entries(
                path.with_name("trace.jsonl.1"),
                [now - timedelta(minutes=30), now - timedelta(minutes=20)],
            )
            self._write_entries(path, [now - timedelta(minutes=10), now])
            entries = load_energy_debug_trace(path, limit=10)
            self.assertEqual(len(entries), 4)
            timestamps = [entry["timestamp"] for entry in entries]
            self.assertEqual(timestamps, sorted(timestamps))


TEST_CONFIG = {
    "charger": {
        "id": "test",
        "key": "test",
        "ip": "127.0.0.1",
        "version": 3.4,
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
class WebChargeKwhApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = Path(self.tmpdir.name) / "config.yaml"
        self.state_path = Path(self.tmpdir.name) / "automation_state.yaml"
        self.config_path.write_text(yaml.safe_dump(TEST_CONFIG, sort_keys=False), encoding="utf-8")
        self.state_path.write_text("{}", encoding="utf-8")
        web_config.app.config["TESTING"] = True
        self.client = web_config.app.test_client()

    def test_start_with_target_kwh_records_target_and_safety_window(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path), patch.object(
            web_config, "DEFAULT_STATE_PATH", self.state_path
        ):
            response = self.client.post("/api/energy/emergency/start", json={"target_kwh": 10})
        self.assertEqual(response.status_code, 200)
        state = yaml.safe_load(self.state_path.read_text(encoding="utf-8"))
        emergency = state["emergency"]
        self.assertTrue(emergency["active"])
        self.assertEqual(emergency["target_energy_kwh"], 10.0)
        self.assertEqual(emergency["delivered_energy_wh"], 0.0)
        self.assertIsNotNone(emergency["expires_at"])
        self.assertGreaterEqual(emergency["duration_minutes"], 30)

    def test_start_rejects_out_of_range_target(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path), patch.object(
            web_config, "DEFAULT_STATE_PATH", self.state_path
        ):
            response = self.client.post("/api/energy/emergency/start", json={"target_kwh": 0.1})
        self.assertEqual(response.status_code, 400)

    def test_plain_emergency_start_has_no_target(self):
        with patch.object(web_config, "CONFIG_PATH", self.config_path), patch.object(
            web_config, "DEFAULT_STATE_PATH", self.state_path
        ):
            response = self.client.post("/api/energy/emergency/start", json={"amps": 32})
        self.assertEqual(response.status_code, 200)
        state = yaml.safe_load(self.state_path.read_text(encoding="utf-8"))
        self.assertIsNone(state["emergency"]["target_energy_kwh"])


@unittest.skipIf(web_config is None, "Flask is not installed in the current test interpreter")
class DashboardAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.password_file = Path(self.tmpdir.name) / "dashboard_password"
        self.client = web_config.app.test_client()

    def _auth_header(self, password: str) -> dict[str, str]:
        token = base64.b64encode(f"user:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def test_requests_are_rejected_without_password(self):
        self.password_file.write_text("secret-test-password\n", encoding="utf-8")
        web_config.app.config["TESTING"] = False
        try:
            with patch.object(web_config, "DASHBOARD_PASSWORD_FILE", self.password_file):
                response = self.client.get("/")
        finally:
            web_config.app.config["TESTING"] = True
        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response.headers)

    def test_requests_pass_with_correct_password(self):
        self.password_file.write_text("secret-test-password\n", encoding="utf-8")
        web_config.app.config["TESTING"] = False
        try:
            with patch.object(web_config, "DASHBOARD_PASSWORD_FILE", self.password_file):
                response = self.client.get("/", headers=self._auth_header("secret-test-password"))
        finally:
            web_config.app.config["TESTING"] = True
        self.assertEqual(response.status_code, 200)

    def test_wrong_password_is_rejected(self):
        self.password_file.write_text("secret-test-password\n", encoding="utf-8")
        web_config.app.config["TESTING"] = False
        try:
            with patch.object(web_config, "DASHBOARD_PASSWORD_FILE", self.password_file):
                response = self.client.get("/", headers=self._auth_header("wrong"))
        finally:
            web_config.app.config["TESTING"] = True
        self.assertEqual(response.status_code, 401)

    def test_auth_disabled_when_no_password_file(self):
        web_config.app.config["TESTING"] = False
        try:
            with patch.object(
                web_config, "DASHBOARD_PASSWORD_FILE", Path(self.tmpdir.name) / "missing"
            ):
                response = self.client.get("/")
        finally:
            web_config.app.config["TESTING"] = True
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
