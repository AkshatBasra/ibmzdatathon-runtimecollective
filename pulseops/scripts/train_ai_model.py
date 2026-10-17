#!/usr/bin/env python3
"""Train an AI Anomaly Detection Model (Isolation Forest) on IBM Z LinuxONE (s390x).

Trains on real server telemetry from the NAB benchmark (data/nab/ec2_cpu_utilization_*.csv),
extracts rolling temporal feature windows, evaluates against ground-truth anomaly windows,
and saves the trained model to `models/ai_anomaly_detector.pkl`.
"""
from __future__ import annotations

import csv
import json
import math
import os
import pickle
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple

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


def check_and_import_sklearn():
    """Ensure scikit-learn is available or prompt user."""
    try:
        from sklearn.ensemble import IsolationForest
        return IsolationForest
    except ImportError:
        print("[!] scikit-learn is not installed. Installing via pip...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "scikit-learn", "numpy"])
        from sklearn.ensemble import IsolationForest
        return IsolationForest


def train():
    print("=" * 70)
    print(" [*] IBM Z / LinuxONE AI Anomaly Detection Model Training")
    print("=" * 70)

    IsolationForest = check_and_import_sklearn()

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
    print("\n[2/4] Training Isolation Forest Model on IBM Z CPU...")
    # 2.5% contamination matches standard server anomaly rates in NAB
    model = IsolationForest(
        n_estimators=120,
        max_samples="auto",
        contamination=0.025,
        random_state=42,
        n_jobs=-1,
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
        "model_type": "IsolationForest",
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
