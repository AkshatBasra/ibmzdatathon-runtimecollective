"""Interpretable streaming anomaly detector (stdlib only -> safe on s390x / LinuxONE).

Design
------
* Baseline = rolling *robust* location/scale (median, MAD, IQR) over recently seen "normal" samples.
* Scale has floors so constant / quantised metrics (MAD == 0) do not divide by ~0.
* Two detection modes:
    - spike:            robust z >= spike_z for `spike_persist` consecutive samples
    - sustained shift:  mean of (clipped) z over `sustain_window` samples >= sustain_z
* Samples are admitted into the baseline only after a lag and only if they looked normal, so a
  slow drift or a long incident is not silently absorbed before it can be detected.
* Warm-up, persistence, cooldown (with severity escalation), missing-value and telemetry-gap handling.
* Every alert carries metric, timestamp, severity, baseline, observed value and a reason string.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, asdict
from typing import Any, Deque, Dict, List, Optional

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class DetectorConfig:
    window: int = 120            # baseline window length (samples)
    warmup: int = 60             # baseline samples required before any alert
    direction: str = "up"        # "up" | "down" | "both"
    spike_z: float = 6.0         # robust z needed to count as a spike sample
    spike_persist: int = 3       # consecutive spike samples required (minimum persistence)
    sustain_window: int = 30     # window for sustained-change test
    sustain_z: float = 3.0       # mean clipped z over window needed for "sustained shift"
    crit_z: float = 12.0         # peak (spike) / mean (sustained) z that makes an alert critical
    min_abs_dev: float = 0.0     # |x - baseline| must also exceed this (ignores tiny absolute moves)
    min_rel_dev: float = 0.0     # ... and this fraction of |baseline|
    abs_floor: float = 1e-6      # floor for the robust scale
    rel_floor: float = 0.02      # scale floor as a fraction of |baseline|
    cooldown: int = 60           # samples to suppress repeats of the same/lower severity
    exclude_z: float = 3.0       # samples with z above this are not learned into the baseline
    adapt_after: int = 0         # >0: after N consecutive deviating samples accept the new level
    gap_alert: int = 5           # consecutive missing samples that raise a telemetry_gap warning


@dataclass
class Anomaly:
    metric: str
    timestamp: Any
    severity: str
    kind: str                    # spike | sustained_shift | telemetry_gap
    baseline: Optional[float]
    observed: Optional[float]
    score: Optional[float]       # robust z (or mean z for sustained)
    scale: Optional[float]
    reason: str
    index: int = 0               # sample index in the stream

    def to_dict(self) -> dict:
        return asdict(self)


def _is_valid(x: Any) -> bool:
    if isinstance(x, bool) or x is None:
        return False
    if isinstance(x, (int, float)):
        return math.isfinite(x)
    return False


def _fmt(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x:.4g}"


class StreamingDetector:
    """Univariate streaming detector. Feed values with `update(timestamp, value)`."""

    def __init__(self, metric: str, cfg: Optional[DetectorConfig] = None,
                 guard: Optional["SeasonalGuard"] = None):
        self.metric = metric
        self.cfg = cfg or DetectorConfig()
        self.guard = guard
        self.reset()

    # ------------------------------------------------------------------ state
    def reset(self) -> None:
        c = self.cfg
        self._base: Deque[float] = deque(maxlen=c.window)
        self._pending: Deque[tuple] = deque()       # (value, z) waiting to be admitted to baseline
        self._zs: Deque[float] = deque(maxlen=c.sustain_window)
        self._n = 0                                  # all updates (valid or not)
        self._run = 0                                # consecutive spike samples
        self._run_peak = 0.0
        self._dev_run = 0                            # consecutive deviating samples (for adapt_after)
        self._missing = 0
        self._last_alert_n = -10 ** 9
        self._last_alert_sev = 0
        self._last_alert_score = 0.0
        self._frozen = False
        self._stats_cache: Optional[tuple] = None

    def freeze(self) -> None:
        """Stop learning (used while an incident is open)."""
        self._frozen = True

    def unfreeze(self) -> None:
        self._frozen = False

    @property
    def ready(self) -> bool:
        return len(self._base) >= self.cfg.warmup

    # ------------------------------------------------------------------ stats
    def baseline_stats(self) -> Optional[Dict[str, float]]:
        """Return {'baseline','scale'} or None if no data."""
        if not self._base:
            return None
        if self._stats_cache is None:
            v = sorted(self._base)
            med = statistics.median(v)
            mad = statistics.median(abs(x - med) for x in v)
            if len(v) >= 4:
                q = statistics.quantiles(v, n=4, method="inclusive")
                iqr = q[2] - q[0]
            else:
                iqr = 0.0
            c = self.cfg
            scale = max(1.4826 * mad, iqr / 1.349, c.rel_floor * abs(med), c.abs_floor)
            self._stats_cache = (med, scale)
        med, scale = self._stats_cache
        return {"baseline": med, "scale": scale}

    def _dz(self, x: float, med: float, scale: float) -> float:
        z = (x - med) / scale
        d = self.cfg.direction
        if d == "up":
            return z
        if d == "down":
            return -z
        return abs(z)

    def _gate(self, x: float, med: float) -> bool:
        dev = abs(x - med)
        c = self.cfg
        return dev >= c.min_abs_dev and dev >= c.min_rel_dev * abs(med)

    # ------------------------------------------------------------------ update
    def update(self, timestamp: Any, value: Any) -> List[Anomaly]:
        c = self.cfg
        self._n += 1
        out: List[Anomaly] = []
        if self.guard is not None:
            self.guard.observe(value)

        if not _is_valid(value):
            self._missing += 1
            self._run = 0
            self._run_peak = 0.0
            if self._missing == c.gap_alert:
                out.append(Anomaly(self.metric, timestamp, "warning", "telemetry_gap", None, None, None, None,
                                   f"{self._missing} consecutive missing/invalid telemetry samples for "
                                   f"'{self.metric}'; detection is blind for this metric", self._n))
            return out
        self._missing = 0
        x = float(value)

        # --- warm-up: learn only
        if len(self._base) < c.warmup:
            self._base.append(x)
            self._stats_cache = None
            return out

        st = self.baseline_stats()
        med, scale = st["baseline"], st["scale"]
        z = self._dz(x, med, scale)
        gated_ok = self._gate(x, med)
        eff_z = z if gated_ok else min(z, 0.0)
        self._zs.append(max(-c.crit_z, min(c.crit_z, eff_z)))

        # --- spike persistence
        if eff_z >= c.spike_z:
            self._run += 1
            self._run_peak = max(self._run_peak, eff_z)
        else:
            self._run = 0
            self._run_peak = 0.0

        # --- adapt_after bookkeeping (accept a permanent new level)
        if eff_z >= c.exclude_z:
            self._dev_run += 1
        else:
            self._dev_run = 0

        cand: Optional[Anomaly] = None
        if self._run >= c.spike_persist:
            sev = "critical" if self._run_peak >= c.crit_z else "warning"
            cand = Anomaly(self.metric, timestamp, sev, "spike", med, x, round(self._run_peak, 2), scale,
                           f"{self.metric}={_fmt(x)} is {_fmt(self._run_peak)} robust-sigma "
                           f"{'above' if c.direction != 'down' else 'below'} baseline {_fmt(med)} "
                           f"(scale {_fmt(scale)}) for {self._run} consecutive samples", self._n)
        elif len(self._zs) == c.sustain_window:
            mz = sum(self._zs) / len(self._zs)
            if mz >= c.sustain_z:
                sev = "critical" if mz >= c.crit_z else "warning"
                cand = Anomaly(self.metric, timestamp, sev, "sustained_shift", med, x, round(mz, 2), scale,
                               f"{self.metric} mean over last {c.sustain_window} samples is {_fmt(mz)} "
                               f"robust-sigma off baseline {_fmt(med)} (latest {_fmt(x)}): sustained level change",
                               self._n)

        if cand is not None and self.guard is not None and cand.observed is not None \
                and self.guard.explains(cand.observed):
            cand = None  # explained by a recurring pattern; must not consume the cooldown
        if cand is not None:
            rank = SEVERITY_RANK[cand.severity]
            in_cd = (self._n - self._last_alert_n) < c.cooldown
            # inside the cooldown only an *escalation* is reported: higher severity or >=3x larger score
            escalated = rank > self._last_alert_sev or (cand.score or 0.0) >= 3.0 * self._last_alert_score
            if (not in_cd) or escalated:
                out.append(cand)
                if not in_cd:
                    self._last_alert_n = self._n
                self._last_alert_sev = rank
                self._last_alert_score = cand.score or 0.0

        # --- learning (lagged + only normal-looking samples)
        if not self._frozen:
            self._pending.append((x, eff_z))
            lag = c.sustain_window
            if len(self._pending) > lag:
                ox, oz = self._pending.popleft()
                recent_mean = sum(self._zs) / max(1, len(self._zs))
                if oz < c.exclude_z and recent_mean < c.sustain_z:
                    self._base.append(ox)
                    self._stats_cache = None
            if c.adapt_after and self._dev_run >= c.adapt_after:
                vals = [p[0] for p in self._pending]
                self._base.clear()
                self._base.extend(vals[-c.window:])
                self._pending.clear()
                self._zs.clear()
                self._dev_run = 0
                self._run = 0
                self._stats_cache = None
        return out


class MultiMetricDetector:
    """Runs one StreamingDetector per metric."""

    def __init__(self, configs: Dict[str, DetectorConfig]):
        self.detectors: Dict[str, StreamingDetector] = {m: StreamingDetector(m, c) for m, c in configs.items()}

    def update(self, timestamp: Any, sample: Dict[str, Any]) -> List[Anomaly]:
        out: List[Anomaly] = []
        for m, d in self.detectors.items():
            out.extend(d.update(timestamp, sample.get(m)))
        return out

    def baselines(self) -> Dict[str, Dict[str, float]]:
        res = {}
        for m, d in self.detectors.items():
            s = d.baseline_stats()
            if s:
                res[m] = s
        return res

    def freeze(self) -> None:
        for d in self.detectors.values():
            d.freeze()

    def unfreeze(self) -> None:
        for d in self.detectors.values():
            d.unfreeze()

    def reset(self) -> None:
        for d in self.detectors.values():
            d.reset()


class SeasonalGuard:
    """Optional false-positive guard for *periodic* behaviour (e.g. a nightly cron/backup spike).

    A value is "explained" if, at the same phase (+-`halfwidth` samples) in each of the last `days`
    periods, a comparably high value already occurred (>= (1 - tol) * observed) in at least `min_hits`
    of those periods. Needs `days` full periods of history before it can suppress anything.
    """

    def __init__(self, period: int, days: int = 3, halfwidth: int = 12, tol: float = 0.3, min_hits: int = 2):
        self.period, self.days, self.halfwidth, self.tol, self.min_hits = period, days, halfwidth, tol, min_hits
        self._h: List[Optional[float]] = []

    def observe(self, value: Any) -> None:
        self._h.append(float(value) if _is_valid(value) else None)

    def explains(self, value: float) -> bool:
        """Call *after* observe(value) for the current sample (current sample is excluded from lookup)."""
        i = len(self._h) - 1
        hits = 0
        for d in range(1, self.days + 1):
            lo, hi = i - d * self.period - self.halfwidth, i - d * self.period + self.halfwidth
            if lo < 0:
                return False
            seg = [x for x in self._h[lo:hi + 1] if x is not None]
            if seg and max(seg) >= (1.0 - self.tol) * value:
                hits += 1
        return hits >= self.min_hits
