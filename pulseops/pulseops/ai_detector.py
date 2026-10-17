"""Trained AI Anomaly Detector & SLM Host Reasoner Integration for PulseOps.

Loads `models/ai_anomaly_detector.pkl` (trained on LinuxONE s390x) and evaluates
live mainframe host CPU & hardware metrics on every dashboard tick.
"""
from __future__ import annotations

import collections
import math
import os
import pickle
import sys
import time
from typing import Any, Dict, List, Optional

from .host_telemetry import get_host_info, get_live_system_metrics
from .slm import PulseOpsSLM, SLMConfig


def _c_factor(n: int) -> float:
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1) + 0.5772156649) - (2.0 * (n - 1) / n)


class PureIsolationTree:
    def __init__(self, depth: int, max_depth: int):
        self.depth = depth
        self.max_depth = max_depth
        self.left = None
        self.right = None
        self.split_feat = None
        self.split_val = None
        self.size = 0

    def path_length(self, x: List[float]) -> float:
        if self.left is None or self.right is None or self.split_feat is None:
            return self.depth + _c_factor(self.size)
        if x[self.split_feat] < self.split_val:
            return self.left.path_length(x)
        else:
            return self.right.path_length(x)


class PureIsolationForest:
    def __init__(self, n_estimators=100, max_samples=256, contamination=0.025, random_state=42):
        self.n_estimators = n_estimators
        self.max_samples = max_samples
        self.contamination = contamination
        self.random_state = random_state
        self.trees: List[PureIsolationTree] = []
        self.c_subsample = _c_factor(max_samples)
        self.threshold = 0.5

    def _raw_scores(self, X: List[List[float]]) -> List[float]:
        c = self.c_subsample if self.c_subsample > 0 else 1.0
        scores = []
        for x in X:
            avg_path = sum(t.path_length(x) for t in self.trees) / len(self.trees)
            scores.append(2.0 ** (- (avg_path / c)))
        return scores

    def decision_function(self, X: List[List[float]]) -> List[float]:
        scores = self._raw_scores(X)
        return [self.threshold - s for s in scores]

    def predict(self, X: List[List[float]]) -> List[int]:
        scores = self._raw_scores(X)
        return [-1 if s >= self.threshold else 1 for s in scores]


class AIHostDetector:
    def __init__(self, models_dir: str, slm: Optional[PulseOpsSLM] = None):
        self.models_dir = models_dir
        self.model_path = os.path.join(models_dir, "ai_anomaly_detector.pkl")
        self.slm = slm or PulseOpsSLM(SLMConfig())
        self.model = None
        self.window_size = 10
        self.history = collections.deque(maxlen=self.window_size)
        self.host_info = get_host_info()
        self.spike_ticks_remaining = 0
        self.last_status = "NORMAL"
        self.last_score = 0.0
        self.last_cpu = 0.0
        self.last_slm_explanation = None
        self.consecutive_anomalies = 0
        self.total_samples = 0
        self._load_model()

    def _load_model(self):
        if os.path.exists(self.model_path):
            try:
                # Ensure classes are in sys.modules so unpickler finds them
                sys.modules["scripts.train_ai_model"] = sys.modules[__name__]
                with open(self.model_path, "rb") as f:
                    self.model = pickle.load(f)
            except Exception:
                self.model = None

    def trigger_spike(self, duration_ticks: int = 8):
        self.spike_ticks_remaining = duration_ticks

    def _read_cpu(self) -> float:
        if self.spike_ticks_remaining > 0:
            self.spike_ticks_remaining -= 1
            return round(94.5 + (self.spike_ticks_remaining % 3) * 2.1, 1)

        try:
            if os.path.exists("/proc/stat"):
                with open("/proc/stat", "r") as f:
                    fields = [float(x) for x in f.readline().strip().split()[1:8]]
                    idle = fields[3]
                    total = sum(fields)
                    return round(max(0.0, min(100.0, 100.0 * (1.0 - (idle / max(1.0, total))))), 1)
        except Exception:
            pass

        metrics = get_live_system_metrics()
        if metrics.get("host_cpu_pct") is not None:
            return float(metrics["host_cpu_pct"])
        return 1.2

    def tick(self) -> Dict[str, Any]:
        self.total_samples += 1
        if self.model is None:
            self._load_model()

        cpu = self._read_cpu()
        self.last_cpu = cpu

        hist_list = list(self.history)
        window = (hist_list + [cpu])[-self.window_size:]
        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / max(1, len(window) - 1)
        std = math.sqrt(variance)
        delta_prev = (cpu - hist_list[-1]) if hist_list else 0.0
        delta_window = (cpu - window[0]) if len(window) > 1 else 0.0
        z_score = abs(cpu - mean) / (std + 1e-5)

        features = [cpu, mean, std, delta_prev, delta_window, z_score]
        self.history.append(cpu)

        if len(self.history) < self.window_size or self.model is None:
            self.last_status = "WARMING_UP"
            self.last_score = 0.0
            return self.get_state()

        pred = self.model.predict([features])[0]
        score = self.model.decision_function([features])[0]
        self.last_score = round(score, 3)

        if pred == -1 or score < 0.0:
            self.last_status = "ANOMALY"
            self.consecutive_anomalies += 1
            platform_name = self.host_info.get("platform_label", "IBM Z LinuxONE")
            facts = [
                f"Host CPU surged to {cpu:.1f}% (Normal rolling baseline is {mean:.1f}%)",
                f"Trained Isolation Forest decision score dropped to {score:.3f} (< 0.0 threshold)",
                f"Normalized deviation: {z_score:.1f}x local standard deviations above baseline",
                f"Rate of change: {delta_prev:+.1f}% shift since last interval",
            ]
            incident_payload = {
                "id": f"INC-HOST-{self.total_samples:04d}",
                "state": "open",
                "diagnosis": {
                    "primary_cause": "host_cpu_saturation",
                    "summary": f"Severe CPU saturation detected on {platform_name}",
                    "facts": facts,
                },
            }
            try:
                self.last_slm_explanation = self.slm.explain_incident(incident_payload, host_info=self.host_info)
            except Exception:
                pass
        else:
            self.last_status = "NORMAL"
            self.consecutive_anomalies = 0

        return self.get_state()

    def get_state(self) -> Dict[str, Any]:
        return {
            "model_loaded": self.model is not None,
            "platform": self.host_info.get("platform_label", "IBM Z"),
            "cpu_pct": self.last_cpu,
            "anomaly_score": self.last_score,
            "status": self.last_status,
            "is_anomaly": self.last_status == "ANOMALY",
            "slm_explanation": self.last_slm_explanation,
            "recent_cpu": list(self.history),
        }
