"""Descriptive profile of the NAB EC2 CPU series (stdlib only). Prints facts, makes no claims."""
import csv, glob, json, os, statistics as st
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "..", "data", "nab")
labels = json.load(open(os.path.join(D, "combined_windows.json")))

for path in sorted(glob.glob(os.path.join(D, "ec2_cpu_utilization_*.csv"))):
    name = os.path.basename(path)
    rows = list(csv.DictReader(open(path)))
    ts = [r["timestamp"] for r in rows]
    v = [float(r["value"]) for r in rows]
    s = sorted(v)
    q = lambda p: s[int(p * (len(s) - 1))]
    mode, mc = Counter(v).most_common(1)[0]
    half = len(v) // 2
    print(f"\n== {name}: n={len(v)} {ts[0]} -> {ts[-1]}")
    print(f"min={s[0]:.3f} p50={q(.5):.3f} p95={q(.95):.3f} p99={q(.99):.3f} max={s[-1]:.3f}")
    print(f"mean={st.mean(v):.3f} std={st.pstdev(v):.3f} distinct={len(set(v))} mode={mode} (x{mc})")
    print(f"mean 1st half={st.mean(v[:half]):.3f}  2nd half={st.mean(v[half:]):.3f}")
    steps = Counter()
    for a, b in zip(ts, ts[1:]):
        steps[(int(b[11:13]) * 60 + int(b[14:16])) - (int(a[11:13]) * 60 + int(a[14:16]))] += 1
    print("top minute-deltas (incl. day wrap):", steps.most_common(3))
    key = "realAWSCloudwatch/" + name[:-4] + ".csv"
    for w in labels.get(key, []):
        idx = [i for i, t in enumerate(ts) if w[0][:19] <= t <= w[1][:19]]
        seg = v[idx[0]:idx[-1] + 1]
        print(f"label window {w[0][:16]}..{w[1][:16]}: {len(seg)} pts, mean={st.mean(seg):.3f} max={max(seg):.3f}")
