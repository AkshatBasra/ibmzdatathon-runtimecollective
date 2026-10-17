"""Rule-based incident diagnosis. Explains from measured telemetry only; no LLM required.

Output separates OBSERVED FACTS (numbers from telemetry) from HYPOTHESES (interpretation), and lists
what the telemetry cannot establish. An optional LLM could *rephrase* this output but is not used here.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .remediation import RemediationConfig


def _mean(samples: List[Dict[str, Any]], m: str) -> Optional[float]:
    v = [s[m] for s in samples if isinstance(s.get(m), (int, float)) and math.isfinite(s[m])]
    return sum(v) / len(v) if v else None


def _elev(obs: Optional[float], b: Optional[Dict[str, float]], min_ratio: float = 1.25, z: float = 3.0) -> bool:
    if obs is None or not b:
        return False
    base = b["baseline"]
    return obs >= base + z * b["scale"] and obs >= min_ratio * max(base, 1e-9)


def diagnose(recent: List[Dict[str, Any]], baselines: Dict[str, Dict[str, float]],
             workers: int, intake_factor: float, rcfg: RemediationConfig,
             tried: Set[Tuple[str, str]] = frozenset()) -> Dict[str, Any]:
    """`recent`: the last few samples at/after detection. `tried`: {(action, 'any')} already used in this incident."""
    obs = {m: _mean(recent, m) for m in
           ("arrival_rate", "throughput", "queue_depth", "latency_ms", "error_rate", "cpu_pct", "service_time_ms")}
    facts: List[str] = []
    for m, label in (("queue_depth", "Queue depth"), ("latency_ms", "Latency (ms)"), ("error_rate", "Error rate"),
                     ("cpu_pct", "CPU % (simulated)"), ("arrival_rate", "Arrival rate (req/s)"),
                     ("service_time_ms", "Per-request service time (ms)"), ("throughput", "Throughput (req/s)")):
        o, b = obs[m], baselines.get(m)
        if o is None:
            continue
        if b:
            facts.append(f"{label}: {o:.3g} observed vs baseline {b['baseline']:.3g}")
        else:
            facts.append(f"{label}: {o:.3g} observed (no baseline)")

    arr_up = _elev(obs["arrival_rate"], baselines.get("arrival_rate"))
    svc_up = _elev(obs["service_time_ms"], baselines.get("service_time_ms"))
    cap = None
    if obs["service_time_ms"]:
        cap = workers * 1000.0 / obs["service_time_ms"]
        facts.append(f"Estimated capacity = {workers} workers x {1000.0 / obs['service_time_ms']:.3g} req/s = "
                     f"{cap:.3g} req/s")
    saturated = bool(cap and obs["arrival_rate"] and obs["arrival_rate"] * intake_factor > cap * 0.98)
    if saturated:
        facts.append(f"Admitted arrival rate {obs['arrival_rate'] * intake_factor:.3g} req/s exceeds estimated "
                     f"capacity {cap:.3g} req/s")

    hyps: List[Dict[str, Any]] = []
    if svc_up:
        hyps.append({"cause": "processing_slowdown", "label": "Slower request processing (e.g. slow dependency)",
                     "confidence": "likely" if saturated else "possible",
                     "supporting": [f"service time {obs['service_time_ms']:.3g} ms vs baseline "
                                    f"{baselines['service_time_ms']['baseline']:.3g} ms"] +
                                   ([] if arr_up else ["arrival rate is not elevated"]),
                     "not_established": ["why processing slowed (dependency, resource contention, code change)"]})
    if arr_up and not svc_up:
        hyps.append({"cause": "load_spike", "label": "Incoming load spike beyond capacity",
                     "confidence": "likely" if saturated else "possible",
                     "supporting": [f"arrival rate {obs['arrival_rate']:.3g} req/s vs baseline "
                                    f"{baselines['arrival_rate']['baseline']:.3g}", "service time is normal"],
                     "not_established": ["whether the extra load is legitimate traffic, retries or abuse"]})
    elif arr_up and svc_up:
        hyps.append({"cause": "load_and_slowdown", "label": "Elevated load combined with slower processing",
                     "confidence": "possible", "supporting": ["both arrival rate and service time are elevated"],
                     "not_established": ["which came first"]})
    if not hyps:
        hyps.append({"cause": "unknown_saturation" if saturated else "unknown",
                     "label": "Backlog without an identified driver",
                     "confidence": "insufficient_evidence",
                     "supporting": ["queue/latency degradation observed"],
                     "not_established": ["root cause: arrival rate and service time are not outside baseline"]})

    primary = hyps[0]["cause"]
    reco = recommend(obs, workers, intake_factor, rcfg, saturated, cap, tried)
    cpu_note = "" if obs["cpu_pct"] is None else f", CPU {obs['cpu_pct']:.0f}% (simulated)"
    summary = (f"{hyps[0]['label']} ({hyps[0]['confidence']}). Queue depth {obs['queue_depth']:.0f}"
               if obs["queue_depth"] is not None else hyps[0]["label"])
    if obs["latency_ms"] is not None:
        summary += f", latency {obs['latency_ms']:.0f} ms"
    summary += cpu_note + "."
    return {"summary": summary, "primary_cause": primary, "facts": facts, "hypotheses": hyps,
            "recommendation": reco, "capacity_saturated": saturated}


def recommend(obs: Dict[str, Optional[float]], workers: int, intake: float, rcfg: RemediationConfig,
              saturated: bool, cap: Optional[float], tried: Set[Tuple[str, str]]) -> Optional[Dict[str, Any]]:
    """Ordered options: scale_workers first, throttle_intake next; never repeat an action already tried."""
    arr, st = obs.get("arrival_rate"), obs.get("service_time_ms")
    if not arr or not st:
        return None
    per_worker = 1000.0 / st
    if saturated and ("scale_workers", "any") not in tried and workers < rcfg.max_workers:
        needed = math.ceil(arr * intake / per_worker / 0.75)          # aim for ~75% utilisation
        target = max(workers + 1, min(needed, rcfg.max_workers, workers + rcfg.max_worker_step))
        return {"action": "scale_workers", "params": {"target_workers": int(target)},
                "rationale": (f"Arrival {arr:.3g} req/s at {per_worker:.3g} req/s per worker needs ~{needed} workers "
                              f"for ~75% utilisation; bounded to {target} (limit {rcfg.max_workers})."),
                "expected_effect": f"capacity {workers * per_worker:.3g} -> {target * per_worker:.3g} req/s"}
    if saturated and ("throttle_intake", "any") not in tried:
        cap_now = (cap or workers * per_worker)
        factor = max(rcfg.min_intake, min(0.95, round(0.9 * cap_now / max(arr, 1e-9), 2)))
        if factor < intake - 1e-9 and factor < 1.0:
            return {"action": "throttle_intake", "params": {"factor": factor},
                    "rationale": (f"Capacity ({cap_now:.3g} req/s) is below offered load ({arr:.3g} req/s) and worker "
                                  f"scaling is exhausted/already tried; shed {(1 - factor) * 100:.0f}% of requests "
                                  f"(floor {(1 - rcfg.min_intake) * 100:.0f}%)."),
                    "expected_effect": "admitted load below capacity; shed requests are an explicit trade-off"}
    return None
