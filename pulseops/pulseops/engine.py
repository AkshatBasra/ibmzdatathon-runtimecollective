"""PulseOps engine: ties simulator -> detection -> incident -> diagnosis -> approved remediation -> verification.

Deterministic and tick-driven (`engine.tick()`), so tests and the live server share the same code path.
Not thread-safe by itself; the HTTP server serialises access with a lock.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from .detector import DetectorConfig, MultiMetricDetector
from .diagnosis import diagnose
from .remediation import ALLOWLIST, RemediationConfig, RemediationEngine
from .simulator import PaymentServiceSim, SimConfig
from .verification import VerificationConfig, summarize_before, verify
from .host_telemetry import get_host_info, get_live_system_metrics
from .slm import PulseOpsSLM, SLMConfig
from .concurrent_service import ConcurrentPaymentService
from .live_service import LiveServiceAdapter

log = logging.getLogger("pulseops")

TRIGGER_METRICS = ("queue_depth", "latency_ms", "error_rate")   # service-impact metrics open incidents
ACTIVE_STATES = {"awaiting_approval", "verifying", "dry_run_only", "rejected", "no_safe_action"}
TERMINAL_STATES = {"recovered", "partially_recovered", "recovery_failed", "insufficient_evidence", "self_resolved"}
SERIES_KEYS = ["t", "arrival_rate", "throughput", "queue_depth", "latency_ms", "error_rate", "cpu_pct",
               "service_time_ms", "shed_rate", "workers"]


def live_detector_configs() -> Dict[str, DetectorConfig]:
    base = dict(window=120, warmup=60, spike_z=6.0, spike_persist=3, crit_z=12.0, sustain_window=20,
                sustain_z=3.0, cooldown=60, direction="up")
    return {
        "queue_depth": DetectorConfig(**base, abs_floor=5.0, min_abs_dev=15.0),
        "latency_ms": DetectorConfig(**{**base, "crit_z": 30.0}, abs_floor=10.0, min_rel_dev=0.3),
        "error_rate": DetectorConfig(**base, abs_floor=0.01, min_abs_dev=0.02),
        "cpu_pct": DetectorConfig(**base, abs_floor=3.0, min_abs_dev=10.0),
        "arrival_rate": DetectorConfig(**base, abs_floor=3.0, min_rel_dev=0.3),
        "service_time_ms": DetectorConfig(**base, abs_floor=5.0, min_rel_dev=0.25),
    }


class PulseOpsEngine:
    def __init__(self, sim_cfg: Optional[SimConfig] = None, rem_cfg: Optional[RemediationConfig] = None,
                 ver_cfg: Optional[VerificationConfig] = None, run_dir: Optional[str] = None,
                 auto_approve: bool = False, prewarm: int = 90, incident_cooldown_s: float = 15.0,
                 history: int = 3600, wall_clock: Callable[[], float] = time.time,
                 use_concurrent_service: bool = False, live_service_url: Optional[str] = None,
                 slm_cfg: Optional[SLMConfig] = None):
        self.sim_cfg = sim_cfg or SimConfig()
        self.rem_cfg = rem_cfg or RemediationConfig()
        self.ver_cfg = ver_cfg or VerificationConfig()
        self.run_dir = run_dir
        if run_dir and not self.rem_cfg.backup_dir:
            self.rem_cfg.backup_dir = os.path.join(run_dir, "backups")
        self.auto_approve = auto_approve
        self.prewarm = prewarm
        self.incident_cooldown_s = incident_cooldown_s
        self.history_len = history
        self.wall = wall_clock
        self.use_concurrent_service = use_concurrent_service
        self.live_service_url = live_service_url
        self.host_info = get_host_info()
        self.slm = PulseOpsSLM(slm_cfg)
        self.reset()

    # ------------------------------------------------------------------ lifecycle
    def reset(self) -> None:
        if self.live_service_url:
            self.sim = LiveServiceAdapter(self.live_service_url)
        elif self.use_concurrent_service:
            self.sim = ConcurrentPaymentService(
                base_arrival=self.sim_cfg.base_arrival,
                base_service_ms=self.sim_cfg.base_service_ms,
                workers=self.sim_cfg.workers,
                queue_max=self.sim_cfg.queue_max,
                timeout_s=self.sim_cfg.timeout_s
            )
        else:
            self.sim = PaymentServiceSim(self.sim_cfg)
        self.detector = MultiMetricDetector(live_detector_configs())
        audit = os.path.join(self.run_dir, "audit.jsonl") if self.run_dir else None
        self.rem = RemediationEngine(self.sim, self.rem_cfg, audit_path=audit, wall_clock=self.wall)
        self.history: Deque[Dict[str, Any]] = deque(maxlen=self.history_len)
        self.alerts: List[Dict[str, Any]] = []
        self.incidents: Dict[str, Dict[str, Any]] = {}
        self._inc_seq = 0
        self._alert_seq = 0
        self.last_close_t = -1e9
        self.last_warning_t = -1e9
        for _ in range(self.prewarm):          # warm-up on normal traffic so the demo starts with a baseline
            if self.live_service_url:
                sample = self.sim.warmup_step()
                self.history.append(sample)
                self.detector.update(sample["t"], sample)
            elif self.use_concurrent_service:
                sample = self.sim.warmup_step()
                self.history.append(sample)
                self.detector.update(sample["t"], sample)
            else:
                self.tick()
        self.warmup_end_t = self.sim.t
        self.alerts.clear()

    @property
    def now(self) -> float:
        return self.sim.t

    def set_mode(self, dry_run: Optional[bool] = None, auto_approve: Optional[bool] = None) -> None:
        if dry_run is not None:
            self.rem_cfg.dry_run = bool(dry_run)
        if auto_approve is not None:
            self.auto_approve = bool(auto_approve)

    # ------------------------------------------------------------------ controls
    def inject_fault(self, kind: str, magnitude: float) -> None:
        self.sim.inject_fault(kind, magnitude)
        source = "live Service X" if self.live_service_url else "simulated"
        log.info("FAULT injected (%s): %s x%.2f at t=%.0f", source, kind, magnitude, self.now)

    def clear_fault(self) -> None:
        self.sim.clear_fault()
        source = "live Service X" if self.live_service_url else "simulated"
        log.info("FAULT cleared (%s) at t=%.0f", source, self.now)

    # ------------------------------------------------------------------ main loop
    def tick(self) -> Dict[str, Any]:
        sample = self.sim.step()
        self.history.append(sample)
        for a in self.detector.update(sample["t"], sample):
            self._on_anomaly(a.to_dict(), sample)
        self._advance_incidents(sample)
        return sample

    # ------------------------------------------------------------------ anomalies
    def active_incident(self) -> Optional[Dict[str, Any]]:
        for inc in self.incidents.values():
            if inc["state"] in ACTIVE_STATES:
                return inc
        return None

    def _on_anomaly(self, a: Dict[str, Any], sample: Dict[str, Any]) -> None:
        self._alert_seq += 1
        a["id"], a["t"], a["simulated"] = f"AL{self._alert_seq:04d}", sample["t"], sample.get("simulated", True)
        a["action"] = "logged"
        sev = a["severity"]
        inc = self.active_incident()
        opens = (sev == "critical" and a["metric"] in TRIGGER_METRICS and a["kind"] != "telemetry_gap")
        if sev == "warning":
            self.last_warning_t = sample["t"]
            log.warning("ANOMALY[warning] %s: %s", a["metric"], a["reason"])
        else:
            log.critical("ANOMALY[critical] %s: %s", a["metric"], a["reason"])
        if inc is not None and sev in ("warning", "critical") and a["kind"] != "telemetry_gap":
            inc["anomaly_ids"].append(a["id"])
            a["action"] = f"attached to {inc['id']}"
        elif opens and (sample["t"] - self.last_close_t) >= self.incident_cooldown_s:
            inc = self._open_incident(a, sample)
            a["action"] = f"opened {inc['id']}"
        elif opens:
            a["action"] = "suppressed (incident cooldown)"
        elif sev == "critical":
            a["action"] = "logged (no service-impact metric affected)"
        self.alerts.append(a)
        self.alerts = self.alerts[-300:]

    # ------------------------------------------------------------------ incidents
    def _event(self, inc: Dict[str, Any], what: str, detail: str = "") -> None:
        inc["timeline"].append({"t": self.now, "wall": self.wall(), "event": what, "detail": detail})

    def _open_incident(self, trigger: Dict[str, Any], sample: Dict[str, Any]) -> Dict[str, Any]:
        self._inc_seq += 1
        iid = f"INC-{self._inc_seq:03d}"
        now = sample["t"]
        baselines = self.detector.baselines()
        self.detector.freeze()
        recent = list(self.history)[-3:]
        inc: Dict[str, Any] = {
            "id": iid, "state": "awaiting_approval", "opened_t": now, "opened_wall": self.wall(),
            "severity": "critical", "trigger": trigger, "anomaly_ids": [trigger["id"]],
            "baselines": baselines, "before_open": summarize_before(recent, baselines, self.ver_cfg.metrics),
            "before": None, "verification": None, "timeline": [], "action_ids": [], "pending_action": None,
            "tried": set(), "attempts": [], "exec_t": None, "closed_t": None, "notes": [],
            "fault_started_t": self.sim.fault_started_t,
            "simulated": not bool(self.live_service_url),
        }
        self.incidents[iid] = inc
        self._event(inc, "detected", trigger["reason"])
        # 1) automatic, non-destructive backup of the (simulated) service state
        b = self.rem.propose(iid, "snapshot_state", {}, "Automatic backup on critical anomaly", now)
        self.rem.execute(b.id, now)
        inc["action_ids"].append(b.id)
        self._event(inc, "backup", f"{b.id}: {b.status}: {b.result or b.blocked_reason}")
        self._propose_next(inc)
        return inc

    def _propose_next(self, inc: Dict[str, Any]) -> None:
        recent = list(self.history)[-3:]
        d = diagnose(recent, inc["baselines"], self.sim.workers, self.sim.intake_factor, self.rem_cfg, inc["tried"])
        if self.slm:
            try:
                d["ai_explanation"] = self.slm.explain_incident(inc, self.host_info)
            except Exception:
                pass
        inc["diagnosis"] = d
        self._event(inc, "diagnosed", d["summary"])
        reco = d["recommendation"]
        if not reco:
            inc["state"] = "no_safe_action"
            inc["pending_action"] = None
            self._event(inc, "no_safe_action", "No remaining safe allowlisted action; escalate to a human operator")
            return
        if self.slm:
            try:
                reco["ai_justification"] = self.slm.justify_action(
                    reco["action"], reco["params"], reco["rationale"], reco.get("expected_effect", "")
                )
            except Exception:
                pass
        rec = self.rem.propose(inc["id"], reco["action"], reco["params"], reco["rationale"], self.now)
        inc["action_ids"].append(rec.id)
        if rec.status == "blocked":
            inc["state"] = "no_safe_action"
            inc["pending_action"] = None
            self._event(inc, "recommendation_blocked", f"{rec.action} {rec.params}: {rec.blocked_reason}")
            return
        inc["state"], inc["pending_action"] = "awaiting_approval", rec.id
        self._event(inc, "proposed", f"{rec.id}: {rec.action} {rec.params}")
        if self.auto_approve:
            self.approve(rec.id, "auto-approve")

    def approve(self, action_id: str, who: str = "operator") -> Dict[str, Any]:
        rec = self.rem.records[action_id]
        inc = self.incidents.get(rec.incident_id or "")
        if inc is None or inc["pending_action"] != action_id or inc["state"] != "awaiting_approval":
            raise ValueError("action is not pending approval")
        self.rem.approve(action_id, who, self.now)
        self._event(inc, "approved", f"{action_id} by {who}")
        return self._execute(inc, action_id)

    def reject(self, action_id: str, who: str = "operator") -> Dict[str, Any]:
        rec = self.rem.records[action_id]
        inc = self.incidents.get(rec.incident_id or "")
        if inc is None or inc["pending_action"] != action_id or inc["state"] != "awaiting_approval":
            raise ValueError("action is not pending approval")
        self.rem.reject(action_id, who, self.now)
        inc["state"], inc["pending_action"] = "rejected", None
        self._event(inc, "rejected", f"{action_id} rejected by {who}; incident stays open without remediation")
        return inc

    def _execute(self, inc: Dict[str, Any], action_id: str) -> Dict[str, Any]:
        now = self.now
        last = list(self.history)[-10:]
        inc["before"] = summarize_before(last, inc["baselines"], self.ver_cfg.metrics)
        rec = self.rem.execute(action_id, now)
        inc["pending_action"] = None
        self._event(inc, "executed" if rec.status == "executed" else rec.status,
                    f"{rec.id}: {rec.result or rec.blocked_reason}")
        if rec.status == "executed":
            inc["tried"].add((rec.action, "any"))
            inc["exec_t"], inc["state"] = now, "verifying"
        elif rec.status == "dry_run":
            inc["tried"].add((rec.action, "any"))
            inc["state"] = "dry_run_only"
            inc["notes"].append("Dry-run: nothing was changed, so recovery cannot be verified.")
        else:  # blocked at execution time (state changed since proposal) or failed
            inc["tried"].add((rec.action, "any"))
            if rec.status == "failed":
                inc["state"] = "no_safe_action"
            else:
                self._propose_next(inc)   # picks a different bounded action, or escalates
        return inc

    def _close(self, inc: Dict[str, Any], state: str, now: float) -> None:
        inc["state"], inc["closed_t"] = state, now
        self.last_close_t = now
        self.detector.unfreeze()
        if self.slm:
            try:
                inc["postmortem"] = self.slm.generate_postmortem(inc, self.host_info)
            except Exception:
                pass
        self._event(inc, f"closed:{state}", (inc.get("verification") or {}).get("reason", ""))
        log.info("INCIDENT %s closed: %s", inc["id"], state)

    def _advance_incidents(self, sample: Dict[str, Any]) -> None:
        inc = self.active_incident()
        if inc is None:
            return
        now = sample["t"]
        if inc["state"] == "verifying":
            post = [s for s in self.history if s["t"] >= inc["exec_t"] + self.ver_cfg.settle_s]
            res = verify(inc["before"], post, now - inc["exec_t"], self.ver_cfg)
            inc["verification"] = res
            if res["verdict"] == "verifying":
                return
            shed = [s["shed_rate"] for s in post[-self.ver_cfg.window:]]
            if res["verdict"] == "recovered" and shed and sum(shed) / len(shed) > 0.01:
                inc["notes"].append(f"Recovery achieved with ~{100 * sum(shed) / len(shed):.0f}% of requests "
                                    f"being shed (explicit trade-off).")
            inc["attempts"].append({"exec_t": inc["exec_t"], "verdict": res["verdict"], "reason": res["reason"]})
            if res["verdict"] == "recovered":
                inc["recovered_t"] = now
                inc["time_to_recover_s"] = now - inc["exec_t"]
                self._close(inc, "recovered", now)
            else:
                # not recovered: offer a *different* bounded action if one remains, otherwise escalate
                self._event(inc, res["verdict"], res["reason"])
                self._propose_next(inc)
                if inc["state"] == "no_safe_action":
                    self._close(inc, res["verdict"], now)
                else:
                    inc["notes"].append(f"Previous attempt verdict: {res['verdict']}; follow-up action proposed.")
        elif inc["state"] in ("awaiting_approval", "rejected", "dry_run_only", "no_safe_action"):
            # the fault may clear on its own: only then declare self-resolution (nothing was applied)
            if now - inc["opened_t"] >= 15 and inc["state"] != "verifying":
                changed = any(self.rem.records[a].status == "executed" and ALLOWLIST[self.rem.records[a].action].changes_service
                              for a in inc["action_ids"])
                post = list(self.history)[-self.ver_cfg.window:]
                res = verify(inc["before_open"], post, 0.0, self.ver_cfg)
                if res["verdict"] == "recovered" and not changed:
                    inc["verification"] = res
                    inc["notes"].append("Metrics returned to baseline without remediation.")
                    if inc["pending_action"]:
                        self.rem.reject(inc["pending_action"], "system", now)
                        inc["pending_action"] = None
                    self._close(inc, "self_resolved", now)

    # ------------------------------------------------------------------ views
    def _incident_view(self, inc: Dict[str, Any], with_series: bool) -> Dict[str, Any]:
        v = {k: val for k, val in inc.items() if k not in ("tried",)}
        v["actions"] = [self.rem.records[a].to_dict() for a in inc["action_ids"]]
        if inc.get("fault_started_t") is not None:
            v["time_to_detect_s"] = round(inc["opened_t"] - inc["fault_started_t"], 1)
        if with_series:
            t0 = inc["opened_t"] - 60
            t1 = inc["closed_t"] + 15 if inc["closed_t"] else self.now
            v["series"] = [{k: s[k] for k in SERIES_KEYS} for s in self.history if t0 <= s["t"] <= t1][-600:]
        return v

    def health(self) -> str:
        inc = self.active_incident()
        if inc is not None:
            return "critical"
        if self.now - self.last_warning_t < 30:
            return "degraded"
        return "healthy"

    def state(self, max_points: int = 240) -> Dict[str, Any]:
        recent = list(self.history)[-max_points:]
        inc = self.active_incident()
        if inc is None and self.incidents:
            inc = list(self.incidents.values())[-1]
        return {
            "simulated": not bool(self.live_service_url),
            "now": self.now,
            "health": self.health(),
            "mode": {"dry_run": self.rem_cfg.dry_run, "auto_approve": self.auto_approve},
            "fault": self.sim.fault, "workers": self.sim.workers, "intake_factor": self.sim.intake_factor,
            "fault_started_t": self.sim.fault_started_t, "fault_cleared_t": self.sim.fault_cleared_t,
            "limits": {"min_workers": self.rem_cfg.min_workers, "max_workers": self.rem_cfg.max_workers,
                       "min_intake": self.rem_cfg.min_intake, "cooldown_s": self.rem_cfg.cooldown_s},
            "series": [{k: s[k] for k in SERIES_KEYS} for s in recent],
            "baselines": self.detector.baselines(),
            "alerts": self.alerts[-40:][::-1],
            "incident": self._incident_view(inc, True) if inc else None,
            "incident_list": [{"id": i["id"], "state": i["state"], "opened_t": i["opened_t"],
                               "closed_t": i["closed_t"]} for i in self.incidents.values()][::-1],
            "actions": [r.to_dict() for r in list(self.rem.records.values())[-30:]][::-1],
            "host_info": self.host_info,
            "live_system": get_live_system_metrics(),
            "slm_provider": self.slm.last_provider_used if self.slm else "none",
            "concurrent_engine": self.use_concurrent_service,
            "service_mode": "live" if self.live_service_url else ("concurrent" if self.use_concurrent_service else "simulation"),
            "live_service_url": self.live_service_url,
        }

    def incident_detail(self, iid: str) -> Optional[Dict[str, Any]]:
        inc = self.incidents.get(iid)
        return self._incident_view(inc, True) if inc else None
