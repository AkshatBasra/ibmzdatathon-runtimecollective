"""Headless end-to-end run of the live loop (simulated). Prints actual measurements; nothing is hard-coded.

usage: python scripts/demo_headless.py [load_spike|slow_downstream] [magnitude] [--dry-run]
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pulseops.engine import PulseOpsEngine  # noqa: E402
from pulseops.remediation import RemediationConfig  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    kind = args[0] if args else "load_spike"
    mag = float(args[1]) if len(args) > 1 else 2.0
    e = PulseOpsEngine(rem_cfg=RemediationConfig(dry_run="--dry-run" in sys.argv))
    print(f"warm-up done at t={e.now:.0f}s, alerts during healthy baseline: {len(e.alerts)}")
    for _ in range(30):
        e.tick()
    print(f"30 more healthy ticks -> alerts: {len(e.alerts)}  health={e.health()}")
    e.inject_fault(kind, mag)
    t_fault = e.now
    inc = None
    for _ in range(120):
        e.tick()
        inc = e.active_incident()
        if inc:
            break
    if not inc:
        print("no incident opened; alerts:", [(a['metric'], a['severity']) for a in e.alerts]); return
    print(f"incident {inc['id']} opened {e.now - t_fault:.0f}s after fault injection")
    print("diagnosis:", inc["diagnosis"]["summary"])
    print("recommend:", inc["diagnosis"]["recommendation"])
    aid = inc["pending_action"]
    e.approve(aid, "demo-operator")
    for _ in range(150):
        e.tick()
        if e.active_incident() is None:
            break
    inc = list(e.incidents.values())[-1]
    print("final state:", inc["state"], "| notes:", inc["notes"])
    v = inc["verification"]
    if v:
        print("verdict:", v["verdict"], "-", v["reason"])
        for m, r in v["metrics"].items():
            fm = lambda x: "n/a" if x is None else f"{x:.3g}"
            print(f"  {m:12s} before_peak={fm(r['before_peak'])} after_mean={fm(r['after_mean'])} "
                  f"threshold={fm(r['threshold'])} passed={r['passed']}")
    print("timeline:")
    for ev in inc["timeline"]:
        print(f"  t={ev['t']:.0f} {ev['event']}: {ev['detail'][:110]}")


if __name__ == "__main__":
    main()
