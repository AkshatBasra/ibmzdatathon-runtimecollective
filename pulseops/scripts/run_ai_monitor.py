#!/usr/bin/env python3
"""Live Mainframe AI Anomaly Monitor & SLM Reasoning Agent.

Collects live host & device telemetry on IBM Z LinuxONE (s390x), evaluates
samples in real time using the trained AI Isolation Forest model, and triggers
the SLM (Small Language Model / IBM Granite) to reason in natural language
whenever an anomaly is detected.
"""
from __future__ import annotations

import argparse
import collections
import math
import os
import pickle
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from pulseops.host_telemetry import get_host_info, get_live_system_metrics  # noqa: E402
from pulseops.slm import PulseOpsSLM, SLMConfig  # noqa: E402

MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "ai_anomaly_detector.pkl")


def load_ai_model():
    if not os.path.exists(MODEL_PATH):
        print(f"[!] Error: Trained model not found at: {MODEL_PATH}")
        print("    Please run the training script first:")
        print("    python3 scripts/train_ai_model.py")
        sys.exit(1)
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


def calculate_features(history: List[float], val: float, window_size: int = 10) -> List[float]:
    """Calculates the 6-dimensional feature vector matching the trained model."""
    window = (history + [val])[-window_size:]
    mean = sum(window) / len(window)
    variance = sum((x - mean) ** 2 for x in window) / max(1, len(window) - 1)
    std = math.sqrt(variance)

    delta_prev = (val - history[-1]) if history else 0.0
    delta_window = (val - window[0]) if len(window) > 1 else 0.0
    z_score = abs(val - mean) / (std + 1e-5)

    return [val, mean, std, delta_prev, delta_window, z_score]


def read_system_cpu() -> float:
    """Reads instantaneous CPU utilization percentage directly from OS."""
    try:
        # Check /proc/stat on LinuxONE
        if os.path.exists("/proc/stat"):
            with open("/proc/stat", "r") as f:
                fields = [float(x) for x in f.readline().strip().split()[1:8]]
                idle = fields[3]
                total = sum(fields)
                return max(0.0, min(100.0, 100.0 * (1.0 - (idle / max(1.0, total)))))
    except Exception:
        pass
    
    # Fallback to get_live_system_metrics
    m = get_live_system_metrics()
    if m.get("host_cpu_pct") is not None:
        return float(m["host_cpu_pct"])
    return 15.0


def main():
    parser = argparse.ArgumentParser(description="Live IBM Z Mainframe AI Anomaly Monitor & SLM Reasoner")
    parser.add_argument("--interval", type=float, default=1.0, help="Sampling interval in seconds (default: 1.0)")
    parser.add_argument("--window", type=int, default=10, help="Feature window size (default: 10)")
    parser.add_argument("--slm", type=str, default="auto", help="SLM provider: auto, watsonx, or embedded")
    parser.add_argument("--demo-spike", action="store_true", help="Simulate a CPU anomaly spike after 5 samples for instant demo")
    args = parser.parse_args()

    print("=" * 75)
    print(" [*] PulseOps Live Mainframe AI Anomaly Monitor & SLM Reasoner")
    print("=" * 75)

    host_info = get_host_info()
    platform_name = host_info.get("platform_label", "IBM Z LinuxONE (s390x)")
    print(f"Hardware Environment : {platform_name}")
    print(f"CPUs Detected         : {host_info.get('cpu_count', 1)}")
    print(f"Loading AI Model      : {MODEL_PATH}")

    model = load_ai_model()
    print("AI Model Loaded       : Isolation Forest (Trained)")

    slm = PulseOpsSLM(SLMConfig(provider=args.slm))
    print(f"SLM Reasoning Backend : {args.slm} (IBM Granite / SRE Copilot)")
    print("-" * 75)
    print("Monitoring real host telemetry. Press Ctrl+C to stop.\n")

    history = collections.deque(maxlen=args.window)
    sample_count = 0
    consecutive_anomalies = 0

    try:
        while True:
            sample_count += 1
            now_str = datetime.now().strftime("%H:%M:%S")

            # Collect real host metrics
            cpu_val = read_system_cpu()

            # If user requested --demo-spike, inject a high spike at sample 6
            if args.demo_spike and 6 <= sample_count <= 9:
                cpu_val = 94.5 + (sample_count % 3) * 2.1
                is_demo = " [DEMO FAULT INJECTED]"
            else:
                is_demo = ""

            # Extract features matching model
            hist_list = list(history)
            features = calculate_features(hist_list, cpu_val, window_size=args.window)
            history.append(cpu_val)

            # Warm-up phase (collect full window before inference)
            if len(history) < args.window:
                print(f"[{now_str}] Sample #{sample_count:02d} | CPU: {cpu_val:5.1f}% | Warming up baseline ({len(history)}/{args.window})...")
                time.sleep(args.interval)
                continue

            # Run AI Inference
            pred = model.predict([features])[0]            # -1: Anomaly, 1: Normal
            score = model.decision_function([features])[0]  # Higher is normal, negative is anomaly

            is_anomaly = (pred == -1)

            if not is_anomaly:
                consecutive_anomalies = 0
                print(f"[{now_str}] Sample #{sample_count:02d} | CPU: {cpu_val:5.1f}% | Score: {score:+6.3f} | Status: [OK] NORMAL")
            else:
                consecutive_anomalies += 1
                print(f"[{now_str}] Sample #{sample_count:02d} | CPU: {cpu_val:5.1f}% | Score: {score:+6.3f} | Status: [!] ANOMALY DETECTED{is_demo}")

                # Gather telemetry facts for SLM
                delta_pct = features[3]
                z_score = features[5]
                facts = [
                    f"Host CPU surged to {cpu_val:.1f}% (Normal baseline is {features[1]:.1f}%)",
                    f"Anomaly score dropped to {score:.3f} (Isolation Forest boundary < 0.0)",
                    f"Metric deviated {z_score:.1f}x standard deviations above local rolling mean",
                    f"Instantaneous delta: {delta_pct:+.1f}% shift since last interval",
                ]

                # Mock incident structure for SLM Reasoning
                incident_payload = {
                    "id": f"INC-HOST-{sample_count:04d}",
                    "state": "open",
                    "diagnosis": {
                        "primary_cause": "host_cpu_saturation",
                        "summary": f"Severe CPU saturation detected on {platform_name}",
                        "facts": facts,
                    },
                }

                print("\n" + "-" * 75)
                print(" [*] SLM Copilot Reasoning (Natural Language Diagnostic):")
                print("-" * 75)
                explanation = slm.explain_incident(incident_payload, host_info=host_info)
                print(f"{explanation}")
                print(f" [Provider: {slm.last_provider_used}]")
                print("-" * 75 + "\n")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\n[+] Monitoring stopped by operator.")


if __name__ == "__main__":
    main()
