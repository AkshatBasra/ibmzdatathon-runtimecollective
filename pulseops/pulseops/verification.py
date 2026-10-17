"""Recovery verification. Never trusts that an action worked: decides from post-action measurements.

Verdicts: verifying (still collecting) | recovered | partially_recovered | recovery_failed | insufficient_evidence
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class VerificationConfig:
    metrics: Sequence[str] = ("queue_depth", "latency_ms", "error_rate")
    window: int = 10                 # post-action samples compared with the recovery threshold
    settle_s: float = 5.0            # ignore the first seconds after the action
    timeout_s: float = 90.0          # give up after this long (sim seconds)
    recovery_z: float = 3.0          # recovered = within baseline + recovery_z * scale
    partial_improvement: float = 0.5 # partially recovered = improved >= 50% of the way back to baseline
    min_valid_fraction: float = 0.7  # of the window, how many samples must be valid numbers


def _valid(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def summarize_before(samples: List[Dict[str, Any]], baselines: Dict[str, Dict[str, float]],
                     metrics: Sequence[str]) -> Dict[str, Dict[str, Optional[float]]]:
    """State captured when the incident opens: peak/mean over the supplied samples vs. learned baseline."""
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for m in metrics:
        vals = [s[m] for s in samples if _valid(s.get(m))]
        b = baselines.get(m, {})
        out[m] = {"baseline": b.get("baseline"), "scale": b.get("scale"),
                  "peak": max(vals) if vals else None,
                  "mean": sum(vals) / len(vals) if vals else None}
    return out


def verify(before: Dict[str, Dict[str, Optional[float]]], post: List[Dict[str, Any]], elapsed_s: float,
           cfg: Optional[VerificationConfig] = None) -> Dict[str, Any]:
    cfg = cfg or VerificationConfig()
    rows: Dict[str, Dict[str, Any]] = {}
    insufficient = False
    need = max(1, int(math.ceil(cfg.window * cfg.min_valid_fraction)))
    for m in cfg.metrics:
        b = before.get(m) or {}
        base, scale, peak = b.get("baseline"), b.get("scale"), b.get("peak")
        window = [s.get(m) for s in post[-cfg.window:]]
        vals = [float(v) for v in window if _valid(v)]
        row: Dict[str, Any] = {"baseline": base, "scale": scale, "before_peak": peak, "before_mean": b.get("mean"),
                               "n_valid": len(vals)}
        if base is None or scale is None or peak is None or len(post) < cfg.window or len(vals) < need:
            row.update(after_mean=(sum(vals) / len(vals) if vals else None), threshold=None, passed=None,
                       improvement=None)
            insufficient = True
        else:
            thr = base + cfg.recovery_z * scale
            mean = sum(vals) / len(vals)
            tail = vals[-3:]
            tail_mean = sum(tail) / len(tail)
            passed = mean <= thr and tail_mean <= thr + scale
            span = peak - base
            improvement = 1.0 if span <= 0 else max(-1.0, min(1.0, (peak - mean) / span))
            row.update(after_mean=mean, threshold=thr, passed=passed, improvement=improvement)
        rows[m] = row

    timed_out = elapsed_s >= cfg.timeout_s
    if insufficient:
        verdict = "insufficient_evidence" if timed_out else "verifying"
        reason = ("not enough valid post-action samples to judge" +
                  ("; timeout reached" if timed_out else " yet"))
    else:
        if all(r["passed"] for r in rows.values()):
            verdict, reason = "recovered", "all recovery criteria met on post-action measurements"
        elif timed_out:
            imps = [r["improvement"] for r in rows.values()]
            failing = [m for m, r in rows.items() if not r["passed"]]
            if min(imps) >= cfg.partial_improvement or (sum(imps) / len(imps)) >= cfg.partial_improvement:
                verdict = "partially_recovered"
                reason = f"improved but still outside the normal band for: {', '.join(failing)}"
            else:
                verdict = "recovery_failed"
                reason = f"insufficient improvement; still failing: {', '.join(failing)}"
        else:
            verdict, reason = "verifying", "criteria not yet met; waiting until timeout"
    return {"verdict": verdict, "reason": reason, "elapsed_s": elapsed_s, "timeout_s": cfg.timeout_s,
            "window": cfg.window, "metrics": rows}
