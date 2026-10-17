import unittest

from pulseops.live_service import LiveServiceAdapter, LiveServiceError


class TestLiveServiceAdapter(unittest.TestCase):
    def test_step_marks_remote_telemetry_live(self):
        adapter = LiveServiceAdapter("http://service-x")
        adapter._request = lambda path, method="GET", payload=None: {
            "arrival_rate": 12,
            "throughput": 11,
            "queue_depth": 2,
            "latency_ms": 100,
            "error_rate": 0,
            "cpu_pct": 4,
            "service_time_ms": 80,
            "shed_rate": 0,
            "workers": 2,
        }

        sample = adapter.step()

        self.assertEqual(sample["t"], 1.0)
        self.assertFalse(sample["simulated"])
        self.assertEqual(adapter.workers, 2)

    def test_fault_controls_use_service_x_contract(self):
        calls = []
        adapter = LiveServiceAdapter("http://service-x")
        adapter._request = lambda path, method="GET", payload=None: (
            calls.append((path, method, payload)) or {}
        )

        adapter.inject_fault("load_spike", 4)
        adapter.clear_fault()

        self.assertEqual(calls[0], ("/control", "POST", {"traffic_multiplier": 4.0}))
        self.assertEqual(calls[1], ("/control", "POST",
                                    {"traffic_multiplier": 1.0, "service_delay_multiplier": 1.0}))

    def test_missing_metric_is_rejected(self):
        adapter = LiveServiceAdapter("http://service-x")
        adapter._request = lambda path, method="GET", payload=None: {}

        with self.assertRaisesRegex(LiveServiceError, "missing fields"):
            adapter.step()


if __name__ == "__main__":
    unittest.main()
