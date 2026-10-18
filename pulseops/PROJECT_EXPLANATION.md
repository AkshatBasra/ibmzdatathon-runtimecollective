# PulseOps: Autonomous AI SRE Copilot for IBM Z & Enterprise Cloud

> **Closed-Loop Incident Detection, Root-Cause Diagnosis, Bounded Remediation, and Mathematical Recovery Verification for Mission-Critical Systems.**

---

## 1. Executive Summary & Problem Statement

### The Problem
IBM Z mainframes and LinuxONE enterprise systems process over **70% of the world's global banking transactions** and host mission-critical enterprise workloads. When an incident occurs in high-throughput environments (e.g., sudden transaction surges, database slowdowns, or worker exhaustion):
* **High Mean Time to Detect (MTTD):** Traditional static threshold alerts either flood engineers with false positives or trigger too late.
* **Delayed Root Cause Analysis (MTTR):** On-call Site Reliability Engineers (SREs) spend 30–60 minutes correlating dashboards, logs, and metrics.
* **Risky Manual Remediation:** Operators under pressure frequently issue unbounded or untested shell commands, risking cascading outages.

### The Solution: PulseOps
**PulseOps** is an autonomous, closed-loop SRE AI copilot designed to run natively on **IBM Z / LinuxONE (`s390x`)** and cloud environments. It implements a strict, safety-certified loop:

$$\text{Live Telemetry} \longrightarrow \text{AI Anomaly Detection} \longrightarrow \text{SLM Diagnosis} \longrightarrow \text{Bounded Remediation} \longrightarrow \text{Measured Verification}$$

```text
  ┌────────────────────────────────────────────────────────────────────────┐
  │                         1. TELEMETRY INGESTION                         │
  │   • Host Metrics: Real IBM Z kernel stats (/proc/stat, /proc/loadavg) │
  │   • Service Metrics: Arrival rate, queue depth, latency, error rate   │
  │   • Historical Benchmark Data: Real AWS EC2 NAB production logs       │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │                      2. AI ANOMALY DETECTION ENGINE                    │
  │   • Streaming Statistical Detector: Rolling Median, MAD, and IQR      │
  │   • Machine Learning Model: Isolation Forest trained on IBM Z CPU     │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      │ Anomaly Flagged
                                      ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │                   3. SLM REASONING & DIAGNOSIS LAYER                   │
  │   • Powered by IBM Granite / watsonx (with offline expert fallback)    │
  │   • Translates raw telemetry shifts into plain-English root causes    │
  │   • Formulates safe, bounded remediation proposals                    │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      │ Proposes Action
                                      ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │                 4. BOUNDED REMEDIATION & HUMAN APPROVAL                │
  │   • Strict allowlist: scale_workers, throttle_intake, snapshot_state  │
  │   • Hard safety bounds (e.g., max 16 workers, min 50% intake)         │
  │   • Operator Approval Gateway (prevents unauthorized system changes)   │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      │ Action Executed
                                      ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │                   5. MATHEMATICAL RECOVERY VERIFICATION                │
  │   • Compares post-action telemetry against learned baselines           │
  │   • Mathematically certifies recovery before closing incident          │
  │   • Generates automated, audit-ready Markdown SRE Post-Mortem Report   │
  └────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Architecture & Components

| Module | File Location | Purpose & Role |
|---|---|---|
| **AI Model Trainer** | `scripts/train_ai_model.py` | Trains an unsupervised **Isolation Forest** on 12,096 samples of real production server logs (`data/nab`). Runs natively on IBM Z CPU in 0.66s. |
| **Live AI Monitor** | `scripts/run_ai_monitor.py` | Continuous CLI monitor that pulls live LinuxONE host metrics and runs inference with SLM reasoning. |
| **Host Telemetry** | `pulseops/host_telemetry.py` | Reads real Linux kernel metrics (`/proc/stat`, `/proc/loadavg`, `/proc/meminfo`) and detects IBM Z mainframe facilities. |
| **Streaming Detector** | `pulseops/detector.py` | Statistical anomaly detector using rolling Median Absolute Deviation (MAD) to learn dynamic baselines without static thresholds. |
| **SLM Reasoner** | `pulseops/slm.py` | Integrates **IBM Granite** (via watsonx or local endpoint) to synthesize executive incident explanations and action justifications. |
| **Remediation Engine** | `pulseops/remediation.py` | Enforces allowlisted actions, bounds checking, cooldowns, idempotency, and immutable audit logs. |
| **Verification Engine** | `pulseops/verification.py` | Calculates before-and-after improvement ratios, certifying that metrics genuinely returned to baseline. |
| **Concurrent Engine** | `pulseops/concurrent_service.py` | Multi-threaded payment processing engine with real POSIX threads, queue backpressure, and SHA-256 cryptographic work. |
| **Service X** | `service_x/server.py` | Standalone HTTP REST microservice acting as an independent external monitoring and remediation target. |
| **Web Dashboard** | `static/index.html` | High-end dark SRE observability console with real-time canvas area-gradient charts and incident controls. |

---

## 3. Data Flow & Telemetry: What is Real vs. Synthetic?

### A. The Datasets
1. **NAB (Numenta Anomaly Benchmark) Dataset (`data/nab/*.csv`):**
   * **Source:** Real-world Amazon AWS EC2 cloud server production metric logs.
   * **Usage:** 
     1. Used as the ground-truth training set for the Isolation Forest machine learning model.
     2. Used by `scripts/eval_nab.py` to benchmark detection recall and false positive rates.
2. **Real Mainframe Host Telemetry (`/proc/stat`, `/proc/loadavg`):**
   * **Source:** The actual Linux kernel running on the IBM Z mainframe (`s390x`).
   * **Usage:** Fed into the AI model and dashboard to monitor host CPU and memory in real time.

### B. The Workload Modes
PulseOps supports three execution targets:

1. **Simulation Mode (Default):**
   * Evaluates mathematical queuing models (Little's Law) in memory.
   * Ideal for unit testing, CI/CD, and offline verification.
2. **Concurrent Mode (`--concurrent`):**
   * Launches **real operating system worker threads (`pthread`)**.
   * Spawns real in-memory queues (`queue.Queue`).
   * Executes **500 iterative SHA-256 cryptographic hashes per transaction** to simulate IBM Z CPACF crypto operations.
   * Dynamically spawns new living OS threads upon operator approval.
3. **Live Service X Mode (`--live-service-url`):**
   * Monitors an independent HTTP microservice over network sockets on port `9100`.
   * Accepts real external HTTP requests from tools like `curl` or ApacheBench (`ab`).

---

## 4. Anomaly Detection & AI Intelligence

PulseOps utilizes a **two-tier intelligence system**:

### Tier 1: Machine Learning & Statistical Detection
* **Isolation Forest Model (`models/ai_anomaly_detector.pkl`):**
  * Unsupervised ensemble of isolation trees.
  * Evaluates 6-dimensional rolling feature vectors:
    $$\text{Features} = [\text{raw\_val}, \text{rolling\_mean}, \text{rolling\_std}, \Delta_{\text{prev}}, \Delta_{\text{window}}, Z\text{-score}]$$
  * Yields an anomaly decision score ($< 0$ indicates outlier behavior).
* **Streaming MAD Baseline:**
  * Uses median and Median Absolute Deviation (MAD) instead of mean and standard deviation, making it resilient to noisy outliers.

### Tier 2: Small Language Model (SLM) Reasoning
* Powered by **IBM Granite** (`ibm/granite-3-2b-instruct` via IBM watsonx / local endpoint, with an embedded zero-dependency expert fallback).
* When an anomaly occurs, the SLM is fed the structured telemetry facts:
  * Baseline drift magnitude
  * Anomaly decision score
  * Standard deviation deviation ($Z$-score)
* The SLM generates:
  1. **Executive Operational Summary:** Plain-English diagnosis of the failure mode.
  2. **Action Justification:** Technical explanation of why the proposed remediation is safe.
  3. **Automated SRE Post-Mortem Report:** Full Markdown post-mortem document complete with Time to Detect (TTD), Time to Recover (TTR), and mathematical recovery proofs.

---

## 5. Enterprise Safety Properties

PulseOps is strictly engineered to prevent runaway automation:

1. **Allowlisted Actions Only:** PulseOps will **never** execute arbitrary shell commands. It can only execute pre-compiled, audited functions (`scale_workers`, `throttle_intake`, `snapshot_state`).
2. **Hard Parameter Clamping:** Parameters are enforced twice—at proposal time and execution time (e.g., worker count is clamped to $[1, 16]$).
3. **Human-in-the-Loop Approval:** Remediation actions remain in `awaiting_approval` until an operator clicks **Approve** (unless `--auto-approve` is explicitly enabled).
4. **Action Idempotency & Cooldowns:** The engine enforces cooldown timers to prevent flapping loops.
5. **No False Self-Resolution Credits:** If a metric recovers on its own without remediation, PulseOps audits the recovery as `self_resolved` rather than falsely crediting the AI.
6. **Air-Gapped & Offline Ready:** The entire platform has zero CDN dependencies and requires no external internet access, ensuring security compliance in banking data centers.

---

## 6. How to Run the Project

### Option A: On the IBM Z Mainframe (Native s390x)

1. **Connect with SSH Port Forwarding (from local PowerShell):**
   ```powershell
   ssh -i "$env:USERPROFILE\Desktop\IBMZ-Datathon\RCKey.pem" `
     -L 8000:127.0.0.1:8000 `
     linux1@148.100.108.50
   ```

2. **Train the AI Model on the Mainframe:**
   ```bash
   cd ~/ibmzdatathon-runtimecollective/pulseops
   python3 scripts/train_ai_model.py
   ```

3. **Start PulseOps in Concurrent Mode:**
   ```bash
   python3 -m pulseops.server --port 8000 --concurrent
   ```

4. **Access the Dashboard:**
   Open `http://127.0.0.1:8000` in your web browser.

---

### Option B: On a Local Laptop (Windows / Mac / Linux)

PulseOps is completely architecture-agnostic. You can run it locally without SSH:

```bash
cd pulseops

# 1. Train the model locally
python scripts/train_ai_model.py

# 2. Start the server
python -m pulseops.server --port 8000 --concurrent
```
Open `http://127.0.0.1:8000` in your browser.

---

## 7. Datathon Judge Defense & Q&A Cheat Sheet

| Judge Question | Winning Technical Answer |
|---|---|
| *"Is this just a simulation?"* | "No. While the payment transaction payloads are synthetic (as standard in SRE benchmarking), the execution layer runs **real POSIX threads, real cryptographic CPU workloads, and real memory queues**. Furthermore, the AI model is trained on **real AWS production server failure logs (NAB)**, and the hardware monitor measures **real Linux kernel metrics (`/proc/stat`)** directly from this IBM Z machine." |
| *"Why did you choose an Isolation Forest?"* | "Isolation Forests are mathematically ideal for time-series infrastructure telemetry because they isolate anomalies near the roots of random decision trees without assuming a Gaussian distribution. On IBM Z, it trains in under 0.7 seconds with zero GPU requirements, running natively on LinuxONE CPU cores." |
| *"How do you prevent the AI from breaking the system?"* | "PulseOps implements a **bounded SRE control plane**. It cannot execute arbitrary bash scripts. All actions are allowlisted, parameter-bounded, cooled-down, and gated behind human operator approval." |
| *"What role does IBM Granite / SLM play?"* | "Raw alerts give numbers; IBM Granite gives understanding. The SLM translates multidimensional telemetry shifts into an executive diagnosis, explains the operational justification for remediation, and automatically drafts the post-mortem report." |
| *"How is recovery proven?"* | "PulseOps does not trust the return status of a command. Instead, the **Verification Engine** samples post-remediation telemetry over a settlement window and mathematically certifies that queue depth and latency have returned to baseline before closing the incident." |
