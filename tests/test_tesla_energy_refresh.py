from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import tesla_energy
from tesla_energy import TeslaEnergyMonitor


class TeslaEnergyRefreshTests(unittest.TestCase):
    def make_monitor(self) -> TeslaEnergyMonitor:
        return TeslaEnergyMonitor(
            {
                "enabled": True,
                "client_id": "client",
                "client_secret": "secret",
                "token_file": "/tmp/unused_tokens.json",
            },
            {"enabled": False},
        )

    def test_early_refresh_due_at_reads_request_time_and_consume_waits_until_due(self):
        monitor = self.make_monitor()
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            due = datetime.now().astimezone() + timedelta(seconds=60)
            (base / "tesla_refresh_request.json").write_text(
                json.dumps({"requested_at": due.isoformat()}),
                encoding="utf-8",
            )

            with patch.object(tesla_energy, "BASE_DIR", base):
                self.assertEqual(monitor._early_refresh_due_at(), due)
                self.assertFalse(monitor._consume_early_refresh_request())
                self.assertTrue((base / "tesla_refresh_request.json").exists())

    def test_early_refresh_consume_clears_due_request(self):
        monitor = self.make_monitor()
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            due = datetime.now().astimezone() - timedelta(seconds=1)
            request_path = base / "tesla_refresh_request.json"
            request_path.write_text(
                json.dumps({"requested_at": due.isoformat()}),
                encoding="utf-8",
            )

            with patch.object(tesla_energy, "BASE_DIR", base):
                self.assertTrue(monitor._consume_early_refresh_request())
                self.assertFalse(request_path.exists())

    def test_temporary_live_poll_override_bypasses_local_budget_guard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sync_state_path = Path(tmpdir) / "tesla_energy_sync_state.json"
            sync_state_path.write_text(
                json.dumps({"month_key": datetime.now().strftime("%Y-%m"), "requests_used": 9}),
                encoding="utf-8",
            )
            monitor = TeslaEnergyMonitor(
                {
                    "enabled": True,
                    "client_id": "client",
                    "client_secret": "secret",
                    "token_file": "/tmp/unused_tokens.json",
                    "sync_state_file": str(sync_state_path),
                    "monthly_request_budget": 10,
                    "monthly_request_reserve": 1,
                    "temporary_live_poll_interval_seconds": 30,
                    "temporary_allow_request_budget_overrun": True,
                },
                {"enabled": False},
            )

            budget = monitor.request_budget_status()

        self.assertEqual(budget["requests_remaining"], 0)
        self.assertEqual(budget["recommended_spacing_seconds"], 30)
        self.assertEqual(budget["temporary_live_poll_interval_seconds"], 30)
        self.assertTrue(budget["temporary_allow_request_budget_overrun"])
        self.assertTrue(monitor._can_spend_request())

    def test_temporary_live_poll_override_has_thirty_second_floor(self):
        monitor = TeslaEnergyMonitor(
            {
                "enabled": True,
                "client_id": "client",
                "client_secret": "secret",
                "token_file": "/tmp/unused_tokens.json",
                "temporary_live_poll_interval_seconds": 5,
            },
            {"enabled": False},
        )

        self.assertEqual(monitor._temporary_live_poll_interval_seconds(), 30)


if __name__ == "__main__":
    unittest.main()
