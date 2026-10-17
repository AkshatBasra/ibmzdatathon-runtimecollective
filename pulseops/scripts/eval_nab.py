"""OFFLINE evaluation of the detector on NAB EC2 CPU series using NAB labels.

This is *historical replay*, NOT the live demo. Results are written to results/offline_nab.json.

Scoring (window based, NAB-style, because NAB labels are wide windows around an anomaly point):
  * hit            : >=1 alert whose timestamp falls inside a labelled window
  * detection delay: first in-window alert time minus the labelled anomaly timestamp (negative = early)
  * false positive : alert (after warm-up) outside every labelled window
Alert = a detector event after cooldown, any severity.

Caveat printed with every sweep: configs are compared on the SAME labelled data, so the best
config in a sweep is optimistic. `default` was fixed before looking at results.
"""
import csv
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from pulseops.detector import DetectorConfig, SeasonalGuard, StreamingDetector  # noqa: E402

DATA = os.path.join(HERE, "..", "data", "nab")
SERIES = ["ec2_cpu_utilization_24ae8d", "ec2_cpu_utilization_5f5533", "ec2_cpu_utilization_53ea38"]
FMT = "%Y-%m-%d %H:%M:%S"


def _t(s):
    return datetime.strptime(s[:19], FMT)


def load_series(name, data_dir=DATA):
    ts, vals = [], []
    with open(os.path.join(data_dir, name + ".csv"), newline="") as f:
        for r in csv.DictReader(f):
            ts.append(r["timestamp"][:19])
            try:
                vals.append(float(r["value"]))
            except (TypeError, ValueError):
                vals.append(None)
    return ts, vals


def load_labels(name, data_dir=DATA):
    key = f"realAWSCloudwatch/{name}.csv"
    wins = json.load(open(os.path.join(data_dir, "combined_windows.json")))[key]
    pts = json.load(open(os.path.join(data_dir, "combined_labels.json")))[key]
    return [(_t(a), _t(b)) for a, b in wins], [_t(p) for p in pts]


def run_detector(ts, vals, cfg, seasonal=False):
    guard = SeasonalGuard(period=288, days=3, halfwidth=12, tol=0.3, min_hits=2) if seasonal else None
    d = StreamingDetector("value", cfg, guard=guard)
    alerts = []
    for t, v in zip(ts, vals):
        alerts.extend(d.update(t, v))
    return alerts


def score(alerts, windows, points, ts, warmup):
    warm_end = _t(ts[min(warmup, len(ts) - 1)])
    fp = 0
    per_win = [{"window": [str(a), str(b)], "hit": False, "delay_min": None} for a, b in windows]
    for a in alerts:
        t = _t(a.timestamp)
        hit_any = False
        for i, (w0, w1) in enumerate(windows):
            if w0 <= t <= w1:
                hit_any = True
                if not per_win[i]["hit"]:
                    per_win[i]["hit"] = True
                    per_win[i]["delay_min"] = (t - points[i]).total_seconds() / 60.0
        if not hit_any and t >= warm_end:
            fp += 1
    days = (_t(ts[-1]) - _t(ts[0])).total_seconds() / 86400.0
    hits = sum(w["hit"] for w in per_win)
    return {"alerts": len(alerts), "false_positive_alerts": fp, "fp_per_day": round(fp / days, 3),
            "windows": len(windows), "windows_hit": hits,
            "window_recall": round(hits / len(windows), 3) if windows else None, "per_window": per_win}


_BASE = dict(window=288, warmup=288, spike_persist=1, sustain_window=36, crit_z=12.0, cooldown=36,
             adapt_after=288)
# name -> (DetectorConfig, use_seasonal_guard). "default" fixed before looking at any result.
CONFIGS = {
    "default": (DetectorConfig(**_BASE, spike_z=6.0, sustain_z=3.0, rel_floor=0.05, direction="both"), False),
    "sensitive": (DetectorConfig(**_BASE, spike_z=4.0, sustain_z=1.5, rel_floor=0.02, direction="both"), False),
    "conservative": (DetectorConfig(**{**_BASE, "spike_persist": 2, "sustain_window": 72, "cooldown": 72},
                                    spike_z=8.0, sustain_z=3.0, rel_floor=0.05, direction="both"), False),
    # CPU anomalies of interest are increases; ignore tiny moves (< +50% of baseline); drop daily-recurring spikes
    "up_seasonal": (DetectorConfig(**_BASE, spike_z=6.0, sustain_z=3.0, rel_floor=0.05, min_rel_dev=0.5,
                                   direction="up"), True),
    "up_seasonal_sensitive": (DetectorConfig(**_BASE, spike_z=4.0, sustain_z=2.0, rel_floor=0.02, min_rel_dev=0.05,
                                             direction="up"), True),
}


def main():
    out = {"note": "OFFLINE historical replay on NAB. Not live-demo results. Configs compared on the same "
                   "labelled data => optimistic. Series values are of unspecified unit.", "results": {}}
    for name in SERIES:
        ts, vals = load_series(name)
        windows, points = load_labels(name)
        out["results"][name] = {}
        for cname, (cfg, seasonal) in CONFIGS.items():
            s = score(run_detector(ts, vals, cfg, seasonal), windows, points, ts, cfg.warmup)
            out["results"][name][cname] = s
            delays = [w["delay_min"] for w in s["per_window"] if w["hit"]]
            print(f"{name[-6:]} {cname:22s} alerts={s['alerts']:3d} FP={s['false_positive_alerts']:3d} "
                  f"({s['fp_per_day']:.2f}/day) windows_hit={s['windows_hit']}/{s['windows']} "
                  f"delay_min={[round(d) for d in delays]}")
    os.makedirs(os.path.join(HERE, "..", "results"), exist_ok=True)
    with open(os.path.join(HERE, "..", "results", "offline_nab.json"), "w") as f:
        json.dump(out, f, indent=2)
    print("\nwritten results/offline_nab.json  (config comparison is on the same labelled data: optimistic)")


if __name__ == "__main__":
    main()
