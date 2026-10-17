#!/usr/bin/env python3
"""Train an AI Anomaly Detection Model (Isolation Forest) on IBM Z LinuxONE (s390x).

Trains on real server telemetry from the NAB benchmark (data/nab/ec2_cpu_utilization_*.csv),
extracts rolling temporal feature windows, evaluates against ground-truth anomaly windows,
and saves the trained model to `models/ai_anomaly_detector.pkl`.

Supports:
1. scikit-learn IsolationForest if installed
2. Pure Python Standard-Library IsolationForest fallback (zero external dependencies,
   immune to Ubuntu 24.04 PEP 668 externally-managed-environment restrictions).
"""
from __future__ import annotations

import csv
import json
import math
import os
import pickle
import random
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DATA_DIR = os.path.join(PROJECT_ROOT, "data", "nab")
MODELS_DIR = os.path.join(PROJECT_ROOT, "models")
SERIES = [
    "ec2_cpu_utilization_24ae8d",
    "ec2_cpu_utilization_5f5533",
    "ec2_cpu_utilization_53ea38",
]
FMT = "%Y-%m-%d %H:%M:%S"


def parse_time(ts_str: str) -> datetime:
    return datetime.strptime(ts_str[:19], FMT)


def load_series(name: str) -> Tuple[List[str], List[float]]:
    csv_path = os.path.join(DATA_DIR, f"{name}.csv")
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Dataset file not found: {csv_path}")
    
    timestamps, values = [], []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            timestamps.append(row["timestamp"][:19])
            try:
                values.append(float(row["value"]))
            except (ValueError, TypeError):
                values.append(0.0)
    return timestamps, values


def load_ground_truth(name: str) -> Tuple[List[Tuple[datetime, datetime]], List[datetime]]:
    win_path = os.path.join(DATA_DIR, "combined_windows.json")
    lbl_path = os.path.join(DATA_DIR, "combined_labels.json")
    key = f"realAWSCloudwatch/{name}.csv"

    with open(win_path, "r", encoding="utf-8") as f:
        wins_data = json.load(f).get(key, [])
    with open(lbl_path, "r", encoding="utf-8") as f:
        labels_data = json.load(f).get(key, [])

    windows = [(parse_time(w[0]), parse_time(w[1])) for w in wins_data]
    points = [parse_time(p) for p in labels_data]
    return windows, points


def extract_features(values: List[float], window_size: int = 10) -> List[List[float]]:
    """Generates rolling feature vectors for temporal anomaly detection:
    1. raw_value
    2. rolling_mean
    3. rolling_std
    4. delta_prev (1-step slope)
    5. delta_window (longer-term drift)
    6. z_score (local normalized deviation)
    """
    features: List[List[float]] = []
    for i in range(len(values)):
        val = values[i]
        start_idx = max(0, i - window_size + 1)
        window = values[start_idx : i + 1]

        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / max(1, len(window) - 1)
        std = math.sqrt(variance)

        delta_prev = (val - values[i - 1]) if i > 0 else 0.0
        delta_window = (val - values[start_idx]) if i > 0 else 0.0
        z_score = abs(val - mean) / (std + 1e-5)

        features.append([val, mean, std, delta_prev, delta_window, z_score])
    return features


FEATURE_NAMES = [
    "raw_value",
    "rolling_mean",
    "rolling_std",
    "delta_prev",
    "delta_window",
    "z_score",
]


# ==============================================================================
# Pure Python Isolation Forest Implementation (Zero-dependency fallback)
# ==============================================================================

def _c_factor(n: int) -> float:
    """Average path length of unsuccessful search in a Binary Search Tree (BST)."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1) + 0.5772156649) - (2.0 * (n - 1) / n)


class PureIsolationTree:
    def __init__(self, depth: int, max_depth: int):
        self.depth = depth
        self.max_depth = max_depth
        self.left: Optional[PureIsolationTree] = None
        self.right: Optional[PureIsolationTree] = None
        self.split_feat: Optional[int] = None
        self.split_val: Optional[float] = None
        self.size = 0

    def fit(self, X: List[List[float]], rng: random.Random) -> PureIsolationTree:
        self.size = len(X)
        if self.depth >= self.max_depth or self.size <= 1:
            return self

        n_features = len(X[0])
        feat_indices = list(range(n_features))
        rng.shuffle(feat_indices)

        for f in feat_indices:
            vals = [row[f] for row in X]
            min_v, max_v = min(vals), max(vals)
            if min_v < max_v:
                self.split_feat = f
                self.split_val = rng.uniform(min_v, max_v)
                left_X = [row for row in X if row[f] < self.split_val]
                right_X = [row for row in X if row[f] >= self.split_val]
                self.left = PureIsolationTree(self.depth + 1, self.max_depth).fit(left_X, rng)
                self.right = PureIsolationTree(self.depth + 1, self.max_depth).fit(right_X, rng)
                return self
        return self

    def path_length(self, x: List[float]) -> float:
        if self.left is None or self.right is None or self.split_feat is None:
            return self.depth + _c_factor(self.size)
        if x[self.split_feat] < self.split_val:
            return self.left.path_length(x)
        else:
            return self.right.path_length(x)


class PureIsolationForest:
    """Pure Python Standard Library implementation of Isolation Forest."""

    def __init__(
        self,
        n_estimators: int = 100,
        max_samples: int = 256,
        contamination: float = 0.025,
        random_state: int = 42,
    ):
        self.n_estimators = n_estimators
        self.max_samples = max_samples
        self.contamination = contamination
        self.random_state = random_state
        self.trees: List[PureIsolationTree] = []
        self.c_subsample = _c_factor(max_samples)
        self.threshold = 0.5

    def fit(self, X: List[List[float]]) -> PureIsolationForest:
        rng = random.Random(self.random_state)
        n_samples = len(X)
        sub_size = min(self.max_samples, n_samples)
        max_depth = math.ceil(math.log2(max(2, sub_size)))
        self.c_subsample = _c_factor(sub_size)

        self.trees = []
        for _ in range(self.n_estimators):
            subsample = [X[i] for i in rng.sample(range(n_samples), sub_size)]
            tree = PureIsolationTree(depth=0, max_depth=max_depth).fit(subsample, rng)
            self.trees.append(tree)

        # Calibrate threshold based on contamination
        scores = self._raw_scores(X)
        sorted_scores = sorted(scores, reverse=True)
        idx = int(self.contamination * len(sorted_scores))
        self.threshold = sorted_scores[min(idx, len(sorted_scores) - 1)]
        return self

    def _raw_scores(self, X: List[List[float]]) -> List[float]:
        c = self.c_subsample if self.c_subsample > 0 else 1.0
        scores = []
        for x in X:
            avg_path = sum(t.path_length(x) for t in self.trees) / len(self.trees)
            anomaly_score = 2.0 ** (- (avg_path / c))
            scores.append(anomaly_score)
        return scores

    def decision_function(self, X: List[List[float]]) -> List[float]:
        # Positive = normal, Negative = anomaly (matching scikit-learn convention)
        scores = self._raw_scores(X)
        return [self.threshold - s for s in scores]

    def predict(self, X: List[List[float]]) -> List[int]:
        # -1 = anomaly, 1 = normal
        scores = self._raw_scores(X)
        return [-1 if s >= self.threshold else 1 for s in scores]


def get_model_class():
    """Detect scikit-learn or fall back smoothly to Pure Python Isolation Forest."""
    try:
        from sklearn.ensemble import IsolationForest
        print("  [Backend] Using accelerated scikit-learn IsolationForest")
        return IsolationForest, "scikit-learn"
    except ImportError:
        print("  [Backend] Using native zero-dependency Pure Python IsolationForest (PEP 668 safe)")
        return PureIsolationForest, "pure_python"


def train():
    print("=" * 70)
    print(" [*] IBM Z / LinuxONE AI Anomaly Detection Model Training")
    print("=" * 70)

    ModelClass, backend_type = get_model_class()

    all_features: List[List[float]] = []
    dataset_stats = []

    print("\n[1/4] Loading and engineering features from NAB dataset...")
    for s_name in SERIES:
        ts, vals = load_series(s_name)
        feats = extract_features(vals)
        all_features.extend(feats)
        dataset_stats.append({
            "series": s_name,
            "samples": len(vals),
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "mean": round(sum(vals) / len(vals), 2),
        })
        print(f"  + {s_name}: {len(vals)} rows loaded")

    print(f"\nTotal training matrix size: {len(all_features)} samples x {len(FEATURE_NAMES)} features")

    # [2/4] Train Isolation Forest
    print(f"\n[2/4] Training Isolation Forest ({backend_type}) on IBM Z CPU...")
    if backend_type == "scikit-learn":
        model = ModelClass(
            n_estimators=100,
            max_samples="auto",
            contamination=0.025,
            random_state=42,
            n_jobs=-1,
        )
    else:
        model = ModelClass(
            n_estimators=60,
            max_samples=256,
            contamination=0.025,
            random_state=42,
        )

    t0 = datetime.now()
    model.fit(all_features)
    train_duration = (datetime.now() - t0).total_seconds()
    print(f"  + Training completed in {train_duration:.2f} seconds!")

    # [3/4] Evaluate against Ground-Truth Anomaly Windows
    print("\n[3/4] Validating Model against Ground-Truth Anomaly Windows...")
    total_windows = 0
    total_hit = 0
    total_fp = 0

    for s_name in SERIES:
        ts, vals = load_series(s_name)
        wins, points = load_ground_truth(s_name)
        feats = extract_features(vals)
        preds = model.predict(feats)  # -1 for anomaly, 1 for normal

        hit_flags = [False] * len(wins)
        fp_count = 0

        for i, (t_str, pred) in enumerate(zip(ts, preds)):
            t_dt = parse_time(t_str)
            if pred == -1:  # model flagged anomaly
                in_window = False
                for w_idx, (w_start, w_end) in enumerate(wins):
                    if w_start <= t_dt <= w_end:
                        hit_flags[w_idx] = True
                        in_window = True
                        break
                # Only count false positive if outside any window and after warmup (first 100 samples)
                if not in_window and i > 100:
                    fp_count += 1

        hits = sum(hit_flags)
        total_windows += len(wins)
        total_hit += hits
        total_fp += fp_count

        recall = (hits / len(wins)) * 100 if wins else 100.0
        print(f"  - {s_name}:")
        print(f"      Windows: {len(wins)} | Windows Hit: {hits} ({recall:.1f}% Recall) | False Alerts: {fp_count}")

    overall_recall = (total_hit / total_windows) * 100 if total_windows else 0
    print("\n" + "-" * 70)
    print(f"Validation Summary: {total_hit}/{total_windows} Anomaly Windows Hit ({overall_recall:.1f}% Recall)")
    print("-" * 70)

    # [4/4] Save Model and Metadata
    print("\n[4/4] Serializing Model to Disk...")
    os.makedirs(MODELS_DIR, exist_ok=True)
    model_path = os.path.join(MODELS_DIR, "ai_anomaly_detector.pkl")
    meta_path = os.path.join(MODELS_DIR, "model_metadata.json")

    with open(model_path, "wb") as f:
        pickle.dump(model, f)

    metadata = {
        "model_type": f"IsolationForest ({backend_type})",
        "backend": backend_type,
        "features": FEATURE_NAMES,
        "window_size": 10,
        "contamination": 0.025,
        "trained_samples": len(all_features),
        "trained_at": datetime.now().isoformat(),
        "training_duration_s": round(train_duration, 3),
        "validation_recall_pct": round(overall_recall, 1),
        "total_windows_evaluated": total_windows,
        "total_windows_hit": total_hit,
    }

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    print(f"  + Saved model:    {model_path}")
    print(f"  + Saved metadata: {meta_path}")
    print("\n[+] Model training complete and ready for live IBM Z inference!")


if __name__ == "__main__":
    train()
