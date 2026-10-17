# PulseOps — detect, respond, prove recovery

Closed-loop incident response for a **simulated** payment-processing service:
telemetry → anomaly detection → diagnosis → *approved, bounded* remediation → **measured** recovery verification.
Runs on the Python standard library only (no pip installs, no network, no external AI API), so it is safe on
LinuxONE / s390x. Python 3.9+.

> The default demo is **simulated** and labelled as such. Live Service X mode uses telemetry from a separate
> running process. Offline NAB results are kept separate
> (`results/offline_nab.json`) and are never mixed with live-demo numbers.

## Run

```bash
cd pulseops
python -m unittest discover -s tests          # 52 tests
python -m pulseops.server --port 8000         # dashboard: http://127.0.0.1:8000
python scripts/demo_headless.py load_spike 2  # same loop without a browser, prints real measurements
python scripts/eval_nab.py                    # OFFLINE NAB replay -> results/offline_nab.json
python scripts/profile_nab.py                 # descriptive stats of the NAB CSVs
# Or open PulseOps_Demo.ipynb directly in Jupyter Notebook on LinuxONE!
```

Server flags: `--dry-run` (never change anything), `--auto-approve` (bounded actions only),
`--tick 0.5`, and `--live-service-url URL` to monitor an independent Service X JSON API.
It binds to 127.0.0.1; on a shared LinuxONE host use `ssh -L 8000:127.0.0.1:8000 …` instead of `--host 0.0.0.0`.
On LinuxONE: `python3 -c "import platform;print(platform.machine())"` should print `s390x`; nothing here needs a binary wheel.

## Architecture

| Module | Role |
|---|---|
| `pulseops/simulator.py` | Team-owned payment service (queue, workers, latency, errors, simulated CPU). Faults: `load_spike`, `slow_downstream`. |
| `service_x/server.py` | Separate standard-library HTTP service with real worker threads, queue, latency, errors and process CPU measurements. |
| `pulseops/live_service.py` | Service X telemetry and bounded-control adapter used by live mode. |
| `pulseops/detector.py` | Streaming robust detector: median/MAD/IQR baseline with scale floors, spike + sustained-shift tests, warm-up, persistence, cooldown with escalation, missing/NaN/constant handling, optional `SeasonalGuard`. |
| `pulseops/diagnosis.py` | Rule-based explanation: *observed facts* vs *hypotheses* vs *not established*; picks the next bounded action. No LLM needed. |
| `pulseops/remediation.py` | Allowlist (`snapshot_state`, `scale_workers`, `throttle_intake`), bounds, step limit, cooldown, loop prevention, approval, dry-run, audit JSONL. No shell, no model-written commands. |
| `pulseops/verification.py` | Compares post-action samples with `baseline + 3·scale`. Verdicts: recovered / partially_recovered / recovery_failed / insufficient_evidence, with timeout. |
| `pulseops/engine.py` | State machine tying the above together; severity routing. |
| `pulseops/server.py`, `static/index.html` | Stdlib HTTP API + single-file offline dashboard (no CDN). |

### Severity routing
* **warning** → logged (`logs/pulseops.log`, console), shown in the alert table, *no action*.
* **critical on a service-impact metric** (queue depth, latency, error rate) → incident: automatic non-destructive **backup** (`runs/<ts>/backups/*.json`), diagnosis, proposed remediation awaiting human approval.
* Telemetry gaps raise a warning (detector is blind), never an incident by themselves.

### Safety properties (all covered by tests)
* Unknown actions, non-integer/out-of-range workers, throttle outside `[0.5, 1.0)`, extra params → `blocked`.
* Bounds re-checked at execution time; approval required for state-changing actions; rejected actions cannot run.
* Same action+params is never repeated within an incident; per-action cooldown; max actions per incident.
* After a non-recovered verdict only a *different* action can be proposed; if none remains, escalate to a human.
* "Recovered" is only ever set from post-action measurements; dry-run and self-resolution are explicitly **not** credited to an action; recovery achieved by shedding load is annotated with the shed percentage.

## Demo script (≈3 min)
1. Start the server. Healthy badge, flat telemetry with dashed baselines (a 90 s simulated warm-up is pre-run).
2. Click **Load spike** (×2). Queue, latency, errors, CPU climb.
3. Within seconds an alert appears; an incident opens, a backup is taken automatically.
4. Read the explanation: facts (numbers) vs hypotheses vs "not established".
5. Show the proposed `scale_workers` with its rationale; click **Approve & execute** (or toggle dry-run first to show nothing changes).
6. Watch latency/queue fall; banner switches VERIFYING → **RECOVERY VERIFIED** only when the criteria pass.
7. Show the before/after table, timeline and audit table. Optional: **Slow downstream ×3** shows an honest *partially recovered* (latency can't drop below the slowed service time) and an escalation instead of a false success; **Load spike ×4** exceeds max capacity: scaling fails verification, then `throttle_intake` is offered as the next bounded step.

## What the NAB files are (and aren't)
See `scripts/profile_nab.py` output. Key facts: 4,032 points at 5 min (14 days); `24ae8d` is an almost idle instance
with quantised values (0.066 / 0.134 / 0.2…), **a ≈1.5 spike every night at ~03:00–03:40** (benign, recurring) and two
labelled real anomalies (2.344, 0.602); `5f5533` is noisy (~43, sd 4.3) with a mean drift/level change that is only
≈0.7 σ; `53ea38` is ~1.8 with rare spikes. NAB labels are ±10 % wide windows around an anomaly, so scoring here is
window based (hit / delay / false positives), not point F1.

## Limitations / honesty notes
* The simulator is a queueing toy; recovery numbers are properties of that model, not of real infrastructure.
* `service_x` is a demo target for live mode; a real service can replace it by implementing the same `/metrics`, `/snapshot`, and `/control` contract.
* Offline NAB results: configs were compared on the same labelled data (optimistic). `5f5533`'s level change and
  `53ea38`'s windows are essentially undetectable by this detector at an acceptable false-positive rate — see `results/offline_nab.json`.
* One incident at a time; one metric set; no persistence across server restarts besides audit/backups/logs.
* The repository root also contains the original OmniAnomaly (TensorFlow 1.12 / Python 3.5-3.6). It is **not** used.

### Live Service X mode

Run the independent target in one terminal:

```bash
python3 -m service_x.server --port 9100 --rate 12 --workers 2 --delay-ms 80
```

Run PulseOps in another terminal:

```bash
python3 -m pulseops.server --port 8000 --live-service-url http://127.0.0.1:9100
```

The dashboard's load-spike and slow-downstream controls call Service X's bounded
control API. To create an incident directly:

```bash
curl -X POST http://127.0.0.1:9100/control \
  -H 'Content-Type: application/json' -d '{"traffic_multiplier":4}'
```

The default simulator remains available by omitting `--live-service-url`.
