"""Team-owned, isolated payment-processing service simulation.

Everything here is SIMULATED. No real processes, hosts or shell commands are touched: remediation
acts only through the small `ServiceHandle` surface (set_workers / set_intake / snapshot).

Model (1 tick = `step_seconds` of simulated time):
  arrivals ~ base_arrival * load_mult (noisy)  -> optional intake throttle (shed) -> FIFO queue
  capacity = workers * 1000 / service_time_ms   (requests / second)
  latency  = service_time + queue wait (Little's law), capped at the client timeout
  errors   = queue overflow + timed-out requests + small background error rate
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional

METRICS = ["arrival_rate", "throughput", "queue_depth", "latency_ms", "error_rate",
           "cpu_pct", "service_time_ms", "shed_rate"]


@dataclass
class SimConfig:
    seed: int = 7
    base_arrival: float = 25.0       # requests / s
    arrival_sigma: float = 2.0
    base_service_ms: float = 100.0   # per-request processing time
    workers: int = 4
    queue_max: int = 1500
    timeout_s: float = 4.0
    step_seconds: float = 1.0
    start_epoch: float = 1_760_000_000.0   # arbitrary fixed start so runs are reproducible


FAULTS = {
    "load_spike": "incoming request rate multiplied by `magnitude`",
    "slow_downstream": "per-request processing time multiplied by `magnitude` (e.g. slow database)",
}


class PaymentServiceSim:
    def __init__(self, cfg: Optional[SimConfig] = None):
        self.cfg = cfg or SimConfig()
        self.reset()

    def reset(self) -> None:
        c = self.cfg
        self.rng = random.Random(c.seed)
        self.t = 0.0
        self.tick = 0
        self.workers = c.workers
        self.intake_factor = 1.0
        self.queue = 0.0
        self.fault: Optional[Dict[str, Any]] = None
        self.load_mult = 1.0
        self.service_mult = 1.0
        self.fault_started_t: Optional[float] = None
        self.fault_cleared_t: Optional[float] = None

    # ------------------------------------------------------------------ fault injection (demo controls)
    def inject_fault(self, kind: str, magnitude: float) -> None:
        if kind not in FAULTS:
            raise ValueError(f"unknown fault '{kind}'")
        if not (isinstance(magnitude, (int, float)) and math.isfinite(magnitude) and 1.0 <= magnitude <= 5.0):
            raise ValueError("magnitude must be a finite number in [1, 5]")
        self.clear_fault(record=False)
        self.fault = {"kind": kind, "magnitude": float(magnitude)}
        if kind == "load_spike":
            self.load_mult = float(magnitude)
        else:
            self.service_mult = float(magnitude)
        self.fault_started_t = self.t
        self.fault_cleared_t = None

    def clear_fault(self, record: bool = True) -> None:
        self.load_mult = 1.0
        self.service_mult = 1.0
        if self.fault is not None and record:
            self.fault_cleared_t = self.t
        self.fault = None

    # ------------------------------------------------------------------ ServiceHandle (used by remediation)
    def set_workers(self, n: int) -> None:
        self.workers = int(n)

    def set_intake(self, factor: float) -> None:
        self.intake_factor = float(factor)

    def snapshot(self) -> Dict[str, Any]:
        return {"t": self.t, "workers": self.workers, "intake_factor": self.intake_factor,
                "queue_depth": round(self.queue, 2), "fault": self.fault, "simulated": True}

    # ------------------------------------------------------------------ dynamics
    def step(self) -> Dict[str, Any]:
        c, r = self.cfg, self.rng
        dt = c.step_seconds
        self.tick += 1
        self.t += dt

        offered = max(0.0, c.base_arrival * self.load_mult + r.gauss(0, c.arrival_sigma))
        admitted = offered * self.intake_factor
        shed = offered - admitted

        st = c.base_service_ms * self.service_mult * (1.0 + r.gauss(0, 0.02))
        cap = self.workers * 1000.0 / st                      # requests / s
        self.queue += admitted * dt
        processed = min(self.queue, cap * dt * (1.0 + r.gauss(0, 0.03)))
        processed = max(0.0, processed)
        self.queue -= processed
        overflow = max(0.0, self.queue - c.queue_max)
        self.queue = min(self.queue, float(c.queue_max))

        wait_s = self.queue / cap if cap > 0 else c.timeout_s
        timeout_frac = min(1.0, max(0.0, (wait_s - c.timeout_s) / c.timeout_s))
        timeouts = processed * timeout_frac
        background = processed * abs(r.gauss(0.002, 0.001))
        errors = overflow + timeouts + background
        denom = max(admitted * dt, 1e-9)
        latency = min(st + wait_s * 1000.0 + r.gauss(0, 4.0), c.timeout_s * 1000.0)
        util = min(1.0, processed / (cap * dt)) if cap > 0 else 1.0
        cpu = min(100.0, 8.0 + 85.0 * util + r.gauss(0, 1.5))

        return {
            "t": round(self.t, 3),
            "ts": round(c.start_epoch + self.t, 3),
            "arrival_rate": round(offered, 3),
            "throughput": round(processed / dt, 3),
            "queue_depth": round(self.queue, 3),
            "latency_ms": round(max(0.0, latency), 3),
            "error_rate": round(min(1.0, errors / denom), 5),
            "cpu_pct": round(max(0.0, cpu), 3),
            "service_time_ms": round(st, 3),
            "shed_rate": round(shed / max(offered, 1e-9), 4),
            "workers": self.workers,
            "simulated": True,
        }
