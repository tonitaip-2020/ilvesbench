from __future__ import annotations

import sys
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ilvesbench.benchmark.netio import NetioEnergyMonitor, NetioReading
from ilvesbench.config import NetioConfig


class NetioEnergyMonitorTests(unittest.TestCase):
    def test_no_url_reports_no_hardware(self) -> None:
        measurement = NetioEnergyMonitor(NetioConfig()).stop()
        self.assertEqual(measurement["status"], "no_hardware")
        self.assertIn("No energy measurement hardware was found", measurement["summary"])

    def test_measurement_averages_load_and_uses_energy_counters(self) -> None:
        monitor = NetioEnergyMonitor(NetioConfig(url="http://netio.local"))
        readings = [
            NetioReading(20, {"1": 12, "2": 8}, {"1": 100, "2": 50}, "NETIO 4KF"),
            NetioReading(30, {"1": 18, "2": 12}, {"1": 100.1, "2": 50.05}, "NETIO 4KF"),
        ]
        with patch.object(monitor, "_fetch_reading", side_effect=readings):
            monitor._capture()
            monitor._capture()
        measurement = monitor.stop()
        self.assertEqual(measurement["status"], "measured")
        self.assertEqual(measurement["average_watts"], 25.0)
        self.assertAlmostEqual(measurement["energy_delta_wh"], 0.15, places=5)
        self.assertEqual(measurement["sockets"]["1"]["average_load_watts"], 15.0)


if __name__ == "__main__":
    unittest.main()
