# PulseOps

### An AI SRE copilot for IBM Z, LinuxONE, and enterprise workloads

This project is made by the team Runtime Collection for IBM Z Datathon SSF2026.

PulseOps is a hackathon-ready, closed-loop incident-response demonstrator. It
turns raw service telemetry into a safe, explainable recovery workflow:

```text
telemetry -> anomaly detection -> diagnosis -> bounded remediation -> verified recovery
```

Instead of stopping at an alert, PulseOps demonstrates what an operator needs
next: an evidence-based diagnosis, a constrained action proposal, an approval
gate, and measured proof that the service recovered.

## Why it matters

Mission-critical systems need more than static thresholds and unbounded
automation. PulseOps addresses three common SRE challenges:

- **Faster detection:** learns dynamic baselines for queue depth, latency,
  error rate, throughput, and host CPU.
- **Explainable response:** turns telemetry changes into a human-readable
  incident diagnosis and remediation rationale.
- **Safe automation:** only proposes allowlisted, bounded actions and records
  every step in an audit trail.

## What the demo shows

1. A healthy payment workload is warmed up and monitored.
2. A controlled fault, such as a load spike or slow downstream dependency, is
   injected.
3. The streaming detector identifies a persistent or sustained anomaly.
4. PulseOps opens an incident and captures the pre-incident baseline.
5. Diagnosis recommends a bounded action such as scaling workers or throttling
   intake.
6. An operator approves or rejects the proposal.
7. The engine measures post-action telemetry and closes the incident only when
   recovery criteria are met.
8. The incident timeline, action history, and post-mortem remain available for
   review.

## Architecture

```text
                 +----------------------+
                 | Simulator or         |
                 | Live Service X      |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Telemetry +          |
                 | streaming detection |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Diagnosis + optional |
                 | IBM Granite SLM      |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Allowlist, bounds,  |
                 | approval, audit      |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Recovery verification |
                 | and post-mortem       |
                 +----------------------+
```

### Detection layers

- **Streaming detector:** rolling median, MAD/IQR, robust z-scores, persistence,
  cooldowns, warm-up handling, and telemetry-gap detection.
- **Host AI detector:** an Isolation Forest model for live CPU anomaly
  monitoring, with a zero-dependency Python implementation available.
- **SLM reasoning:** optional IBM Granite integration through watsonx or a
  local endpoint, with an embedded expert fallback for offline demos.

### Safety by design

- Strict remediation allowlist
- Hard bounds on worker and intake changes
- Human approval by default
- Dry-run mode
- Cooldowns and duplicate-action prevention
- Execution-time validation
- JSONL audit logging
- Recovery based on measurements, not success responses
- Simulation mode that cannot modify a real production service

## Quick start

The core runtime uses Python's standard library and requires Python 3.9 or
newer.

### Run the automated tests

```bash
cd pulseops
python3 -m unittest discover -s tests
```

### Start the dashboard in simulator mode

```bash
cd pulseops
python3 -m pulseops.server --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Useful options:

```text
--dry-run             Propose actions without changing the simulator
--auto-approve        Skip the operator approval step for a demo
--concurrent          Use the in-process multithreaded service
--live-service-url    Monitor an independent Service X instance
--slm                 auto, watsonx, local_endpoint, or embedded
```

### Run a headless incident demo

```bash
cd pulseops
python3 scripts/demo_headless.py load_spike 2
python3 scripts/demo_headless.py slow_downstream 2 --dry-run
```

### Run Live Service X mode

Start the independent demo service:

```bash
cd pulseops
python3 -m service_x.server --port 9100 --rate 12 --workers 2 --delay-ms 80
```

Start PulseOps against it in another terminal:

```bash
cd pulseops
python3 -m pulseops.server \
  --port 8000 \
  --live-service-url http://127.0.0.1:9100
```

Service X exposes health, metrics, snapshot, request, and bounded control
endpoints. It is intentionally a demo target rather than a production control
plane.

## Repository map

| Path | Role |
| --- | --- |
| [`pulseops/pulseops/engine.py`](pulseops/pulseops/engine.py) | Main detection, incident, remediation, and verification workflow |
| [`pulseops/pulseops/detector.py`](pulseops/pulseops/detector.py) | Robust streaming anomaly detection |
| [`pulseops/pulseops/ai_detector.py`](pulseops/pulseops/ai_detector.py) | Host CPU Isolation Forest monitoring |
| [`pulseops/pulseops/diagnosis.py`](pulseops/pulseops/diagnosis.py) | Evidence-based diagnosis and action recommendation |
| [`pulseops/pulseops/remediation.py`](pulseops/pulseops/remediation.py) | Allowlisted, bounded, auditable actions |
| [`pulseops/pulseops/verification.py`](pulseops/pulseops/verification.py) | Before/after recovery verification |
| [`pulseops/pulseops/slm.py`](pulseops/pulseops/slm.py) | Optional SLM explanations and post-mortems |
| [`pulseops/service_x/server.py`](pulseops/service_x/server.py) | Independent live HTTP demo service |
| [`pulseops/static/index.html`](pulseops/static/index.html) | Offline-friendly SRE dashboard |
| [`pulseops/tests/`](pulseops/tests/) | Unit, API, integration, and safety tests |
| [`pulseops/PROJECT_EXPLANATION.md`](pulseops/PROJECT_EXPLANATION.md) | Detailed architecture and hackathon presentation notes |

## Data and AI model workflow

The repository includes a small NAB-derived dataset under
[`pulseops/data/nab/`](pulseops/data/nab/). The training and evaluation tools
are separate from the core dashboard:

```bash
cd pulseops
python3 scripts/train_ai_model.py
python3 scripts/eval_nab.py
python3 scripts/profile_nab.py
```

The trained model is written to `pulseops/models/`. Runtime operation does not
require the legacy OmniAnomaly research tree; PulseOps uses its own streaming
detector and Isolation Forest implementation.

## Demo notes

- The default dashboard binds to `127.0.0.1`.
- On a remote LinuxONE host, use SSH local port forwarding rather than exposing
  the dashboard publicly:

  ```bash
  ssh -L 8000:127.0.0.1:8000 user@your-linuxone-host
  ```

- The simulator and Service X are demonstrations. Replace the live control
  endpoint with an authenticated, service-specific control plane before
  connecting a real workload.
- Detector state and active incidents are held in memory; audit and runtime
  logs are persisted when a run directory is configured.

## Built for the hackathon

PulseOps combines observability, interpretable anomaly detection, IBM Granite
reasoning, and guarded remediation into one demonstrable operator experience.
It is designed to make the full SRE loop visible: detect the problem, explain
it, act carefully, and prove the outcome.

