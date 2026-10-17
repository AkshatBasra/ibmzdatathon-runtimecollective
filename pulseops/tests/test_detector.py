import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pulseops.detector import DetectorConfig, MultiMetricDetector, SeasonalGuard, StreamingDetector  # noqa: E402


def noisy(n, mean=50.0, sd=1.0, seed=1):
    r = random.Random(seed)
    return [mean + r.gauss(0, sd) for _ in range(n)]


def run(det, values, start=0):
    alerts = []
    for i, v in enumerate(values):
        for a in det.update(start + i, v):
            alerts.append(a)
    return alerts


class TestDetector(unittest.TestCase):
    def test_normal_does_not_alert_continuously(self):
        d = StreamingDetector("m", DetectorConfig())
        alerts = run(d, noisy(5000, seed=3))
        self.assertLessEqual(len(alerts), 2, [a.reason for a in alerts])

    def test_warmup_suppresses_alerts(self):
        d = StreamingDetector("m", DetectorConfig(warmup=60))
        alerts = run(d, [50.0] * 10 + [500.0] * 20)
        self.assertEqual(alerts, [])

    def test_sudden_spike_detected_with_bounded_delay(self):
        cfg = DetectorConfig(spike_persist=3)
        d = StreamingDetector("m", cfg)
        base = noisy(300, seed=5)
        self.assertEqual(run(d, base), [])
        alerts = run(d, [80.0] * 10, start=300)
        self.assertTrue(alerts)
        a = alerts[0]
        self.assertEqual(a.kind, "spike")
        self.assertEqual(a.severity, "critical")
        self.assertLessEqual(a.timestamp - 300, cfg.spike_persist - 1)
        for k in ("metric", "timestamp", "severity", "baseline", "observed", "reason"):
            self.assertIsNotNone(getattr(a, k))
        self.assertAlmostEqual(a.baseline, 50.0, delta=1.0)

    def test_single_blip_below_persistence_ignored(self):
        d = StreamingDetector("m", DetectorConfig(spike_persist=3))
        run(d, noisy(200, seed=7))
        self.assertEqual(run(d, [90.0, 90.0] + noisy(50, seed=8), start=200), [])

    def test_sustained_moderate_shift_detected(self):
        d = StreamingDetector("m", DetectorConfig())
        run(d, noisy(300, seed=11))
        alerts = run(d, noisy(80, mean=54.0, seed=12), start=300)   # ~+4 sigma: below spike_z but sustained
        self.assertTrue(any(a.kind == "sustained_shift" for a in alerts), [a.kind for a in alerts])

    def test_slow_drift_not_absorbed_before_detection(self):
        d = StreamingDetector("m", DetectorConfig())
        r = random.Random(2)
        vals = [50 + r.gauss(0, 1) for _ in range(200)] + [50 + 0.1 * i + r.gauss(0, 1) for i in range(100)]
        self.assertTrue(run(d, vals))

    def test_missing_and_malformed_values(self):
        d = StreamingDetector("m", DetectorConfig(gap_alert=5))
        run(d, noisy(100))
        bad = [None, float("nan"), "oops", float("inf"), True, [], {}]
        alerts = run(d, bad, start=100)       # must not raise
        self.assertTrue(any(a.kind == "telemetry_gap" for a in alerts))
        self.assertEqual(run(d, noisy(50, seed=4), start=200), [])   # recovers cleanly
        self.assertTrue(run(d, [90.0] * 6, start=300))

    def test_constant_metric_no_crash_no_false_alarm(self):
        d = StreamingDetector("m", DetectorConfig())
        self.assertEqual(run(d, [5.0] * 300), [])
        self.assertTrue(run(d, [50.0] * 5, start=300))

    def test_zero_constant_metric(self):
        d = StreamingDetector("m", DetectorConfig(abs_floor=0.5))
        self.assertEqual(run(d, [0.0] * 300), [])
        self.assertTrue(run(d, [10.0] * 5, start=300))

    def test_scale_invariance(self):
        for scale in (1e-3, 1.0, 1e6):
            d = StreamingDetector("m", DetectorConfig())
            run(d, [v * scale for v in noisy(300, seed=9)])
            self.assertTrue(run(d, [80.0 * scale] * 5, start=300), f"scale {scale}")

    def test_cooldown_limits_repeats(self):
        d = StreamingDetector("m", DetectorConfig(cooldown=60))
        run(d, noisy(200, seed=13))
        alerts = run(d, [80.0] * 100, start=200)
        self.assertLessEqual(len(alerts), 3)

    def test_downward_direction(self):
        d = StreamingDetector("m", DetectorConfig(direction="down"))
        run(d, noisy(200, seed=14))
        self.assertEqual(run(d, [80.0] * 5, start=200), [])
        self.assertTrue(run(d, [10.0] * 5, start=205))

    def test_seasonal_guard_suppresses_recurring_spike_only(self):
        period = 50
        vals = []
        r = random.Random(3)
        for day in range(6):
            for k in range(period):
                v = 10 + r.gauss(0, 0.2)
                if k == 25:
                    v = 30.0                       # nightly job spike
                vals.append(v)
        vals[5 * period + 40] = 30.0               # genuine unexpected spike on last day
        cfg = DetectorConfig(window=100, warmup=60, spike_persist=1, cooldown=5)
        g = SeasonalGuard(period=period, days=3, halfwidth=3, tol=0.3, min_hits=2)
        alerts = run(StreamingDetector("m", cfg, guard=g), vals)
        phases = [a.timestamp % period for a in alerts if a.timestamp >= 3 * period]
        self.assertNotIn(25, phases)
        self.assertIn(40, phases)

    def test_multi_metric(self):
        md = MultiMetricDetector({"a": DetectorConfig(), "b": DetectorConfig()})
        for i in range(200):
            md.update(i, {"a": 1.0 + 0.01 * (i % 3), "b": 5.0 + 0.01 * (i % 2)})
        out = []
        for i in range(200, 205):
            out += md.update(i, {"a": 1.0, "b": 100.0})
        self.assertEqual({a.metric for a in out}, {"b"})


if __name__ == "__main__":
    unittest.main()
