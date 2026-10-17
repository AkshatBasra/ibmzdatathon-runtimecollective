import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pulseops.engine import PulseOpsEngine  # noqa: E402
from pulseops.remediation import RemediationConfig, RemediationEngine  # noqa: E402
from pulseops.simulator import PaymentServiceSim, SimConfig  # noqa: E402
from pulseops.verification import VerificationConfig, verify  # noqa: E402


class TestSimulator(unittest.TestCase):
    def test_normal_is_healthy_and_deterministic(self):
        def run():
            s = PaymentServiceSim(SimConfig(seed=1))
            return [s.step() for _ in range(300)]
        a, b = run(), run()
        self.assertEqual(a, b)
        self.assertLess(max(x["queue_depth"] for x in a), 15)
        self.assertLess(max(x["latency_ms"] for x in a), 200)
        self.assertTrue(all(x["simulated"] for x in a))

    def test_load_spike_builds_queue(self):
        s = PaymentServiceSim()
        for _ in range(50):
            s.step()
        s.inject_fault("load_spike", 2.0)
        last = [s.step() for _ in range(40)][-1]
        self.assertGreater(last["queue_depth"], 100)
        self.assertGreater(last["latency_ms"], 1000)

    def test_invalid_fault_rejected(self):
        s = PaymentServiceSim()
        for kind, mag in (("rm_rf", 2), ("load_spike", 0), ("load_spike", float("nan")), ("load_spike", 99)):
            with self.assertRaises(ValueError):
                s.inject_fault(kind, mag)


class TestRemediation(unittest.TestCase):
    def setUp(self):
        self.sim = PaymentServiceSim()
        self.rem = RemediationEngine(self.sim, RemediationConfig(), wall_clock=lambda: 1.0)

    def test_unknown_action_blocked(self):
        for a in ("rm -rf /", "restart_host", "shell", ""):
            r = self.rem.propose("I", a, {}, "x", 0)
            self.assertEqual(r.status, "blocked", a)
        with self.assertRaises(PermissionError):
            self.rem.execute(r.id, 0)

    def test_out_of_bounds_rejected(self):
        for p in ({"target_workers": 99}, {"target_workers": 1}, {"target_workers": 0}, {"target_workers": 4},
                  {"target_workers": True}, {"target_workers": 4.5}, {"target_workers": "8"}, {},
                  {"target_workers": 6, "cmd": "ls"}):
            r = self.rem.propose("I", "scale_workers", p, "x", 0)
            self.assertEqual(r.status, "blocked", p)
        for f in (0.1, 1.0, 1.5, float("nan"), "0.7", True):
            r = self.rem.propose("I", "throttle_intake", {"factor": f}, "x", 0)
            self.assertEqual(r.status, "blocked", f)
        self.assertEqual(self.sim.workers, 4)
        self.assertEqual(self.sim.intake_factor, 1.0)

    def test_step_limit(self):
        self.rem.cfg.max_worker_step = 2
        self.assertEqual(self.rem.propose("I", "scale_workers", {"target_workers": 8}, "x", 0).status, "blocked")
        self.assertEqual(self.rem.propose("I", "scale_workers", {"target_workers": 6}, "x", 0).status, "proposed")

    def test_requires_approval_then_executes(self):
        r = self.rem.propose("I", "scale_workers", {"target_workers": 6}, "x", 0)
        with self.assertRaises(PermissionError):
            self.rem.execute(r.id, 1)
        self.assertEqual(self.sim.workers, 4)
        self.rem.approve(r.id, "alice", 2)
        r = self.rem.execute(r.id, 3)
        self.assertEqual((r.status, self.sim.workers, r.approved_by), ("executed", 6, "alice"))
        self.assertEqual((r.state_before["workers"], r.state_after["workers"]), (4, 6))
        self.assertIsNotNone(r.executed_t)

    def test_rejected_action_never_runs(self):
        r = self.rem.propose("I", "scale_workers", {"target_workers": 6}, "x", 0)
        self.rem.reject(r.id, "alice", 1)
        with self.assertRaises(PermissionError):
            self.rem.execute(r.id, 2)
        self.assertEqual(self.sim.workers, 4)

    def test_dry_run_changes_nothing(self):
        self.rem.cfg.dry_run = True
        r = self.rem.propose("I", "scale_workers", {"target_workers": 6}, "x", 0)
        self.rem.approve(r.id, "a", 1)
        r = self.rem.execute(r.id, 2)
        self.assertEqual((r.status, self.sim.workers), ("dry_run", 4))
        self.assertIn("DRY-RUN", r.result)

    def test_cooldown_and_loop_prevention(self):
        r = self.rem.propose("I", "scale_workers", {"target_workers": 6}, "x", 0)
        self.rem.approve(r.id, "a", 1)
        self.rem.execute(r.id, 2)
        again = self.rem.propose("I", "scale_workers", {"target_workers": 6}, "x", 3)
        self.assertEqual(again.status, "blocked")               # same params/already there
        other = self.rem.propose("J", "scale_workers", {"target_workers": 8}, "x", 10)
        self.assertEqual(other.status, "blocked")
        self.assertIn("cooldown", other.blocked_reason)
        later = self.rem.propose("J", "scale_workers", {"target_workers": 8}, "x", 100)
        self.assertEqual(later.status, "proposed")

    def test_max_actions_per_incident(self):
        self.rem.cfg.max_actions_per_incident = 1
        self.rem.cfg.cooldown_s = 0
        r = self.rem.propose("I", "scale_workers", {"target_workers": 6}, "x", 0)
        self.rem.approve(r.id, "a", 1)
        self.rem.execute(r.id, 2)
        self.assertEqual(self.rem.propose("I", "throttle_intake", {"factor": 0.7}, "x", 5).status, "blocked")

    def test_executor_failure_recorded(self):
        def boom(n):
            raise RuntimeError("worker pool unreachable")
        self.sim.set_workers = boom
        r = self.rem.propose("I", "scale_workers", {"target_workers": 6}, "x", 0)
        self.rem.approve(r.id, "a", 1)
        r = self.rem.execute(r.id, 2)
        self.assertEqual(r.status, "failed")
        self.assertIn("worker pool unreachable", r.result)

    def test_state_change_between_proposal_and_execution_is_rechecked(self):
        r = self.rem.propose("I", "scale_workers", {"target_workers": 6}, "x", 0)
        self.rem.approve(r.id, "a", 1)
        self.sim.workers = 6                                   # someone else already scaled
        r = self.rem.execute(r.id, 2)
        self.assertEqual(r.status, "blocked")

    def test_audit_log_written(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "audit.jsonl")
            rem = RemediationEngine(self.sim, RemediationConfig(), audit_path=p)
            r = rem.propose("I", "scale_workers", {"target_workers": 6}, "x", 0)
            rem.approve(r.id, "a", 1)
            rem.execute(r.id, 2)
            with open(p) as fh:
                events = [json.loads(l)["event"] for l in fh]
            self.assertEqual(events, ["proposed", "approved", "executed"])

    def test_backup_written(self):
        with tempfile.TemporaryDirectory() as d:
            rem = RemediationEngine(self.sim, RemediationConfig(backup_dir=d))
            r = rem.propose("I", "snapshot_state", {}, "x", 0)
            r = rem.execute(r.id, 1)
            self.assertEqual(r.status, "executed")
            self.assertEqual(len(os.listdir(d)), 1)


def S(q, lat, err, n=1, **kw):
    return [dict({"t": i, "queue_depth": q, "latency_ms": lat, "error_rate": err}, **kw) for i in range(n)]


BEFORE = {"queue_depth": {"baseline": 1.0, "scale": 5.0, "peak": 400.0, "mean": 300.0},
          "latency_ms": {"baseline": 100.0, "scale": 10.0, "peak": 5000.0, "mean": 4000.0},
          "error_rate": {"baseline": 0.002, "scale": 0.01, "peak": 0.3, "mean": 0.2}}


class TestVerification(unittest.TestCase):
    cfg = VerificationConfig(window=10, timeout_s=60)

    def test_recovered(self):
        r = verify(BEFORE, S(0, 105, 0.002, 12), 20, self.cfg)
        self.assertEqual(r["verdict"], "recovered")

    def test_waits_until_timeout_when_not_yet_recovered(self):
        r = verify(BEFORE, S(300, 3000, 0.1, 12), 20, self.cfg)
        self.assertEqual(r["verdict"], "verifying")

    def test_failed_at_timeout(self):
        r = verify(BEFORE, S(400, 5000, 0.3, 12), 60, self.cfg)
        self.assertEqual(r["verdict"], "recovery_failed")

    def test_worse_than_before_is_failed(self):
        r = verify(BEFORE, S(900, 9000, 0.9, 12), 60, self.cfg)
        self.assertEqual(r["verdict"], "recovery_failed")

    def test_partial(self):
        r = verify(BEFORE, S(0, 400, 0.002, 12), 60, self.cfg)
        self.assertEqual(r["verdict"], "partially_recovered")
        self.assertFalse(r["metrics"]["latency_ms"]["passed"])

    def test_insufficient_evidence_too_few_samples(self):
        self.assertEqual(verify(BEFORE, S(0, 100, 0.002, 3), 60, self.cfg)["verdict"], "insufficient_evidence")

    def test_insufficient_evidence_missing_telemetry(self):
        post = S(None, float("nan"), None, 12)
        self.assertEqual(verify(BEFORE, post, 60, self.cfg)["verdict"], "insufficient_evidence")

    def test_no_baseline_never_claims_recovery(self):
        r = verify({"queue_depth": {}, "latency_ms": {}, "error_rate": {}}, S(0, 100, 0, 12), 60, self.cfg)
        self.assertEqual(r["verdict"], "insufficient_evidence")


class TestEndToEnd(unittest.TestCase):
    def engine(self, **kw):
        return PulseOpsEngine(**kw)

    def run_until(self, e, pred, n=300):
        for _ in range(n):
            e.tick()
            if pred():
                return True
        return False

    def test_healthy_baseline_no_alerts(self):
        e = self.engine()
        for _ in range(400):
            e.tick()
        self.assertEqual(e.alerts, [])
        self.assertEqual(e.health(), "healthy")
        self.assertIsNone(e.active_incident())

    def test_critical_incident_backup_diagnosis_approval_recovery(self):
        e = self.engine()
        for _ in range(20):
            e.tick()
        e.inject_fault("load_spike", 2.0)
        self.assertTrue(self.run_until(e, lambda: e.active_incident() is not None, 60))
        inc = e.active_incident()
        # automatic non-destructive backup happened without approval; remediation awaits approval
        actions = {a["action"]: a for a in inc["actions"]} if "actions" in inc else {}
        recs = [e.rem.records[i] for i in inc["action_ids"]]
        self.assertEqual(recs[0].action, "snapshot_state")
        self.assertEqual(recs[0].status, "executed")
        self.assertEqual(inc["state"], "awaiting_approval")
        self.assertEqual(e.rem.records[inc["pending_action"]].action, "scale_workers")
        self.assertEqual(e.sim.workers, 4)                       # nothing changed before approval
        d = inc["diagnosis"]
        self.assertEqual(d["primary_cause"], "load_spike")
        self.assertTrue(d["facts"] and d["hypotheses"][0]["not_established"])
        e.approve(inc["pending_action"], "tester")
        self.assertGreater(e.sim.workers, 4)
        self.assertTrue(self.run_until(e, lambda: e.active_incident() is None, 150))
        inc = e.incidents[inc["id"]]
        self.assertEqual(inc["state"], "recovered")
        v = inc["verification"]
        self.assertEqual(v["verdict"], "recovered")
        for m in v["metrics"].values():
            self.assertTrue(m["passed"])
            self.assertLess(m["after_mean"], m["before_peak"])

    def test_slow_downstream_diagnosed_differently(self):
        e = self.engine()
        e.inject_fault("slow_downstream", 2.0)
        self.assertTrue(self.run_until(e, lambda: e.active_incident() is not None, 60))
        self.assertEqual(e.active_incident()["diagnosis"]["primary_cause"], "processing_slowdown")

    def test_fault_can_remain_after_recovery_and_is_verified_on_measurements(self):
        e = self.engine()
        e.inject_fault("load_spike", 2.0)
        self.run_until(e, lambda: e.active_incident() is not None, 60)
        e.approve(e.active_incident()["pending_action"])
        self.run_until(e, lambda: e.active_incident() is None, 150)
        self.assertIsNotNone(e.sim.fault)                          # fault still injected; capacity absorbed it

    def test_failed_remediation_not_claimed_recovered_and_no_loop(self):
        e = self.engine()
        e.inject_fault("load_spike", 4.0)                           # 100 req/s > max capacity 80 req/s
        self.run_until(e, lambda: e.active_incident() is not None, 60)
        inc = e.active_incident()
        first = inc["pending_action"]
        e.approve(first)
        self.assertEqual(e.sim.workers, 8)
        self.assertTrue(self.run_until(e, lambda: e.incidents[inc["id"]]["attempts"], 150))
        att = e.incidents[inc["id"]]["attempts"][0]
        self.assertIn(att["verdict"], ("recovery_failed", "partially_recovered"))
        self.assertNotEqual(att["verdict"], "recovered")
        self.assertGreaterEqual(e.now - e.incidents[inc["id"]]["exec_t"], e.ver_cfg.timeout_s)  # waited for timeout
        # follow-up must be a different action: scale_workers is never proposed twice
        self.assertEqual(e.incidents[inc["id"]]["state"], "awaiting_approval")
        nxt = e.rem.records[e.incidents[inc["id"]]["pending_action"]]
        self.assertEqual(nxt.action, "throttle_intake")
        self.assertEqual(e.rem.propose(inc["id"], "scale_workers", {"target_workers": 8}, "x", e.now).status,
                         "blocked")

    def test_timeout_without_telemetry_gives_insufficient_evidence(self):
        e = self.engine()
        e.inject_fault("load_spike", 2.0)
        self.run_until(e, lambda: e.active_incident() is not None, 60)
        inc = e.active_incident()
        e.approve(inc["pending_action"])
        orig = e.sim.step

        def blind():
            s = orig()
            for k in ("queue_depth", "latency_ms", "error_rate"):
                s[k] = None
            return s
        e.sim.step = blind
        self.run_until(e, lambda: e.incidents[inc["id"]]["attempts"], 200)
        self.assertEqual(e.incidents[inc["id"]]["attempts"][0]["verdict"], "insufficient_evidence")

    def test_dry_run_mode_never_claims_recovery(self):
        e = self.engine(rem_cfg=RemediationConfig(dry_run=True))
        e.inject_fault("load_spike", 2.0)
        self.run_until(e, lambda: e.active_incident() is not None, 60)
        inc = e.active_incident()
        e.approve(inc["pending_action"])
        for _ in range(60):
            e.tick()
        self.assertEqual(e.sim.workers, 4)
        self.assertEqual(e.incidents[inc["id"]]["state"], "dry_run_only")
        self.assertNotEqual(e.incidents[inc["id"]]["state"], "recovered")

    def test_reject_leaves_system_untouched(self):
        e = self.engine()
        e.inject_fault("load_spike", 2.0)
        self.run_until(e, lambda: e.active_incident() is not None, 60)
        inc = e.active_incident()
        aid = inc["pending_action"]
        e.reject(aid)
        for _ in range(30):
            e.tick()
        self.assertEqual(e.sim.workers, 4)
        self.assertEqual(e.incidents[inc["id"]]["state"], "rejected")
        with self.assertRaises(ValueError):
            e.approve(aid)

    def test_self_resolution_is_not_credited_to_an_action(self):
        e = self.engine()
        e.inject_fault("load_spike", 2.0)
        self.run_until(e, lambda: e.active_incident() is not None, 60)
        inc = e.active_incident()
        e.clear_fault()
        self.assertTrue(self.run_until(e, lambda: e.active_incident() is None, 200))
        self.assertEqual(e.incidents[inc["id"]]["state"], "self_resolved")
        self.assertEqual(e.sim.workers, 4)

    def test_mild_degradation_warns_without_incident_or_action(self):
        e = self.engine()
        for _ in range(20):
            e.tick()
        e.inject_fault("slow_downstream", 1.3)       # 130 ms service: capacity 30.8 vs 25 req/s (still OK)
        for _ in range(120):
            e.tick()
        self.assertTrue(any(a["severity"] == "warning" for a in e.alerts))   # warn-level logs raised
        self.assertEqual(e.incidents, {})                                    # but no incident ...
        self.assertFalse(any(r.action != "snapshot_state" for r in e.rem.records.values()))  # ... and no action
        self.assertEqual(e.sim.workers, 4)

    def test_auto_approve_mode_still_bounded(self):
        e = self.engine(auto_approve=True)
        e.inject_fault("load_spike", 2.0)
        self.run_until(e, lambda: e.active_incident() is not None or e.incidents, 60)
        self.run_until(e, lambda: e.active_incident() is None, 200)
        self.assertLessEqual(e.sim.workers, e.rem_cfg.max_workers)

    def test_state_is_json_serialisable(self):
        e = self.engine()
        e.inject_fault("load_spike", 2.0)
        self.run_until(e, lambda: e.active_incident() is not None, 60)
        json.dumps(e.state())


if __name__ == "__main__":
    unittest.main()
