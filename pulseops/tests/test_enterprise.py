import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pulseops.host_telemetry import get_host_info, get_live_system_metrics
from pulseops.slm import PulseOpsSLM, SLMConfig
from pulseops.concurrent_service import ConcurrentPaymentService
from pulseops.engine import PulseOpsEngine


class TestEnterpriseExtensions(unittest.TestCase):
    def test_host_telemetry(self):
        info = get_host_info()
        self.assertIn("machine", info)
        self.assertIn("system", info)
        self.assertIn("platform_label", info)
        self.assertGreaterEqual(info["cpu_count"], 1)

        sys_metrics = get_live_system_metrics()
        self.assertIn("timestamp", sys_metrics)
        self.assertIn("process_memory_mb", sys_metrics)

    def test_slm_embedded_provider(self):
        slm = PulseOpsSLM(SLMConfig(provider="embedded"))
        fake_inc = {
            "id": "INC-099",
            "opened_t": 100.0,
            "closed_t": 125.0,
            "time_to_detect_s": 2.5,
            "state": "recovered",
            "action_ids": ["A001", "A002"],
            "diagnosis": {
                "summary": "Incoming load spike beyond capacity",
                "primary_cause": "load_spike",
                "facts": ["Queue depth: 35", "Latency: 750ms"]
            },
            "verification": {
                "verdict": "recovered",
                "reason": "all criteria passed",
                "metrics": {
                    "latency_ms": {"before_peak": 750.0, "after_mean": 95.0, "threshold": 120.0, "passed": True}
                }
            }
        }
        exp = slm.explain_incident(fake_inc)
        self.assertIn("INC-099", exp or "")
        self.assertIn("Copilot", exp)

        just = slm.justify_action("scale_workers", {"target_workers": 7}, "high load", "capacity +30")
        self.assertIn("Scaling worker concurrency", just)

        pm = slm.generate_postmortem(fake_inc)
        self.assertIn("# SRE Incident Post-Mortem: INC-099", pm)
        self.assertIn("PASSED", pm)

    def test_concurrent_service_lifecycle(self):
        svc = ConcurrentPaymentService(workers=3, queue_max=50)
        snap = svc.snapshot()
        self.assertEqual(snap["workers"], 3)
        self.assertTrue(snap["concurrent_engine"])

        # Run 3 steps
        step1 = svc.step()
        self.assertIn("latency_ms", step1)
        self.assertIn("queue_depth", step1)

        svc.inject_fault("load_spike", 2.0)
        self.assertEqual(svc.load_mult, 2.0)
        self.assertIsNotNone(svc.fault_started_t)

        svc.set_workers(5)
        self.assertEqual(svc.workers, 5)

        svc.clear_fault()
        self.assertIsNone(svc.fault)
        svc.stop()

    def test_engine_with_slm_and_host_info(self):
        engine = PulseOpsEngine(prewarm=10)
        state = engine.state()
        self.assertIn("host_info", state)
        self.assertIn("live_system", state)
        self.assertIn("slm_provider", state)
        self.assertEqual(state["slm_provider"], "embedded")


if __name__ == "__main__":
    unittest.main()
