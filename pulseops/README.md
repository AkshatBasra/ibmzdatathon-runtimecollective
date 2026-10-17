# PulseOps

PulseOps is a closed-loop incident-response demonstrator for IBM Z/LinuxONE:

```text
telemetry -> anomaly detection -> diagnosis -> bounded remediation -> measured recovery
```

It supports two target-service modes:

1. **Simulation mode** — the original deterministic in-process payment-service
   simulator. This remains the default and is useful for repeatable demos and
   tests.
2. **Live Service X mode** — PulseOps monitors a separate running HTTP service
   and can apply bounded controls to that service. Service X is a realistic
   demo target with real worker threads, a queue, wall-clock processing delay,
   request errors, traffic generation, and process CPU measurements.

Both modes use the same detector, diagnosis, remediation, verification,
dashboard, and approval workflow.

The project uses Python's standard library for its core runtime and is suitable
for native `s390x` LinuxONE environments. Python 3.9+ is required.

## Quick start: simulator

From the repository's `pulseops` directory:

```bash
python3 -m unittest discover -s tests
python3 -m pulseops.server --port 8000
```

Open `http://127.0.0.1:8000`. The simulator is used when
`--live-service-url` is omitted.

Useful options:

```text
--port PORT                 Dashboard port; default 8000
--host HOST                 Bind address; default 127.0.0.1
--tick SECONDS              Wall-clock polling interval; default 0.5
--dry-run                   Never apply remediation
--auto-approve              Skip human approval for bounded actions
--concurrent                Use the in-process multithreaded service
--live-service-url URL      Monitor an independent Service X API
--slm PROVIDER              auto, watsonx, local_endpoint, or embedded
```

The server binds to localhost by default. On a shared LinuxONE host, use SSH
port forwarding rather than exposing the dashboard publicly:

```powershell
ssh -i "$HOME\.ssh\RCKey.pem" `
  -L 8000:127.0.0.1:8000 `
  -L 8080:127.0.0.1:8080 `
  linux1@148.100.108.50
```

Then open `http://127.0.0.1:8000` on the Windows machine.

## Live Service X mode

### 1. Start Service X

Service X is located in `service_x/` and is deliberately independent of the
PulseOps process:

```bash
cd ~/ibmzdatathon-runtimecollective/pulseops
python3 -m service_x.server \
  --port 9100 \
  --rate 12 \
  --workers 2 \
  --delay-ms 80
```

Check that it is running:

```bash
curl -s http://127.0.0.1:9100/health
curl -s http://127.0.0.1:9100/metrics
```

### 2. Start PulseOps against Service X

In another terminal:

```bash
cd ~/ibmzdatathon-runtimecollective/pulseops
python3 -m pulseops.server \
  --port 8000 \
  --live-service-url http://127.0.0.1:9100 \
  --slm local_endpoint
```

Omit `--slm local_endpoint` if SLM generation is not needed. The dashboard
shows `Mode: live` and displays telemetry obtained from Service X rather than
from the in-process simulator.

### Live Service X API contract

The adapter in `pulseops/live_service.py` expects:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Service availability check |
| `GET /metrics` | Live arrival rate, throughput, queue, latency, errors, CPU, service time, shedding, and workers |
| `GET /snapshot` | State snapshot used by the remediation audit |
| `POST /control` | Bounded demo controls |
| `POST /request` | Submit one direct request |

Service X is a demo target, not a production control plane. A real service can
replace it by implementing the telemetry contract and a carefully designed
authorization-controlled remediation endpoint.

## Dashboard controls

### Common controls

The dashboard supports:

- **Normal traffic** / fault clearing
- **Load spike**
- **Slow downstream**
- Dry-run mode
- Auto-approval mode
- Pause/resume telemetry processing
- Reset demo
- Incident approval or rejection
- Live telemetry charts and learned baselines
- Alert history and remediation audit history
- Before/after recovery measurements
- Automated SRE post-mortem report

The simulator uses the fault buttons locally. In live mode, the same buttons
send bounded control requests to Service X.

### Service X live controls

In live mode, an additional **Service X live controls** panel appears. It is
hidden in simulator mode. It controls Service X through PulseOps, so the
browser only needs access to port 8000; it does not need direct access to port
9100.

Available values:

| Control | Range | Meaning |
|---|---:|---|
| Traffic multiplier | `0.1`–`5.0` | Multiplies generated request traffic |
| Delay multiplier | `0.1`–`5.0` | Multiplies Service X processing delay |
| Error rate | `0.0`–`1.0` | Probability that a processed request fails |
| Intake factor | `0.5`–`1.0` | Fraction of incoming work admitted |
| Workers | `1`–`16` | Service X worker count |

The API rejects unknown fields and values outside these ranges.

## Causing a live incident

With the default Service X settings:

```text
rate     = 12 requests/second
workers  = 2
delay    = 80 ms
capacity = approximately 25 requests/second
```

Use the dashboard's live controls:

```text
Traffic multiplier: 4
Delay multiplier:   1
Error rate:         0
Intake factor:      1
Workers:            2
```

Click **Apply to Service X**. Traffic rises to approximately 48 requests per
second while two workers can process only approximately 25 requests per
second. After a few real seconds, queue depth and latency should rise, a
critical incident should open, and `scale_workers` should normally be
recommended.

For a stronger combined incident:

```text
Traffic multiplier: 4
Delay multiplier:   2
Error rate:         0
Intake factor:      1
Workers:            2
```

To demonstrate elevated errors:

```text
Traffic multiplier: 1
Delay multiplier:   1
Error rate:         0.5
Intake factor:      1
Workers:            2
```

The direct equivalent of a load spike is:

```bash
curl -s -X POST http://127.0.0.1:9100/control \
  -H 'Content-Type: application/json' \
  -d '{"traffic_multiplier":4}'
```

## Incident-response workflow

1. PulseOps collects telemetry on each tick.
2. The detector learns a robust baseline using median, MAD, and IQR.
3. Persistent deviations produce warning or critical alerts.
4. Critical queue, latency, or error anomalies open an incident.
5. PulseOps records a non-destructive snapshot and diagnoses the likely cause.
6. A bounded action is proposed.
7. An operator approves or rejects the action unless auto-approval is enabled.
8. PulseOps executes only allowlisted actions.
9. Post-action telemetry is compared with the learned baseline.
10. The incident is marked recovered only when measured verification passes.

The main allowlisted actions are:

- `snapshot_state`
- `scale_workers`
- `throttle_intake`

If recovery fails, PulseOps can propose a different bounded action. If no safe
action remains, it escalates rather than inventing a shell command or
unbounded remediation.

## Reset behavior

Clicking **Reset demo** resets PulseOps state, including detector baselines,
alerts, incidents, action history for the current engine, and fault metadata.

In live mode it also sends a reset to Service X and restores its startup
configuration:

- Original worker count
- Traffic multiplier: `1`
- Delay multiplier: `1`
- Error rate: `0`
- Intake factor: `1`
- Normal traffic state

This allows a complete repeatable demo without manually restarting Service X.

The **Restore normal** button only restores normal traffic controls. It does
not reset the detector history or worker count; use **Reset demo** for a clean
run.

## Recent implementation details

- Added a separate `service_x/` package rather than embedding the target
  service in PulseOps.
- Added `LiveServiceAdapter` so the existing remediation engine can operate
  against Service X without duplicating remediation logic.
- Added live-mode selection with `--live-service-url`; simulation remains the
  default.
- Added live/simulation mode badges and live measurement labels to the
  dashboard.
- Added dashboard Service X controls with server-side range validation.
- Added detailed Service X error propagation so rejected controls explain the
  actual validation failure.
- Fixed Service X control handling so returning control metrics cannot
  deadlock.
- Added live worker reset support; excess worker threads retire safely.
- Preserved the automated SRE report's open/closed state across dashboard
  refreshes.
- Fixed concurrent-service warm-up so accelerated warm-up does not saturate
  the queue before the baseline is learned.
- Added live adapter and concurrent warm-up regression coverage.

## Architecture

| Module | Role |
|---|---|
| `pulseops/simulator.py` | Original deterministic payment-service simulator |
| `pulseops/concurrent_service.py` | In-process multithreaded simulation |
| `service_x/server.py` | Independent live demo target |
| `pulseops/live_service.py` | HTTP adapter for Service X telemetry and controls |
| `pulseops/detector.py` | Streaming robust anomaly detector |
| `pulseops/diagnosis.py` | Evidence-based diagnosis and recommendation |
| `pulseops/remediation.py` | Allowlist, bounds, approval, cooldown, audit |
| `pulseops/verification.py` | Measured before/after recovery verification |
| `pulseops/engine.py` | Incident state machine |
| `pulseops/server.py` | Local HTTP API and tick loop |
| `static/index.html` | Single-file offline dashboard |

## Safety properties

- Unknown actions are blocked.
- Worker and intake values are range-checked.
- State-changing remediation requires approval unless explicitly configured
  for auto-approval.
- Bounds are checked again at execution time.
- The same action and parameters are not repeated within one incident.
- Per-action cooldowns and maximum actions per incident prevent loops.
- Dry-run actions do not change the target.
- Self-resolution is not falsely credited to remediation.
- Recovery is based on post-action measurements, not on a success-shaped
  response from the control endpoint.
- Telemetry gaps raise a warning and never open an incident by themselves.

## Tests and supporting scripts

```bash
python3 -m unittest discover -s tests
python3 scripts/demo_headless.py load_spike 2
python3 scripts/eval_nab.py
python3 scripts/profile_nab.py
```

The test suite covers simulator behavior, concurrent warm-up, HTTP API flow,
remediation safety, live adapter contracts, and Service X control behavior.

## Offline NAB data

The NAB scripts are separate from the live dashboard. Their results are stored
in `results/offline_nab.json` and are never mixed with live-demo measurements.
NAB scoring is window-based because labels represent time windows rather than
single exact points.

## Limitations

- Service X is a realistic demo target, not a production service.
- The generic control endpoint should be replaced with an authenticated,
  service-specific remediation API before connecting a real workload.
- PulseOps currently handles one incident at a time.
- Detector state and incidents are not persisted across server restarts;
  audit logs, backups, and runtime logs are persisted.
- The original OmniAnomaly directory in the repository is not used by the
  PulseOps runtime.
