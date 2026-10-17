"""Service X: a small independent HTTP service for live PulseOps monitoring.

Endpoints:
  GET  /health
  GET  /metrics
  GET  /snapshot
  POST /request
  POST /control

The control endpoint is intentionally bounded and is suitable for a local demo.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Full, Queue
from typing import Any, Dict


class ServiceX:
    def __init__(self, rate: float = 12.0, workers: int = 2, delay_ms: float = 80.0):
        self.lock = threading.Lock()
        self.queue: Queue[float] = Queue(maxsize=500)
        self.rate = rate
        self.traffic_multiplier = 1.0
        self.intake_factor = 1.0
        self.service_delay_multiplier = 1.0
        self.error_rate = 0.0
        self.workers = workers
        self.delay_ms = delay_ms
        self.processed = 0
        self.errors = 0
        self.dropped = 0
        self.latencies = []
        self.last_metrics_at = time.monotonic()
        self.last_processed = 0
        self.last_errors = 0
        self.last_dropped = 0
        self.last_cpu_at = time.monotonic()
        self.last_cpu_seconds = sum(os.times()[:2])
        self.stop_event = threading.Event()
        self.threads = []
        self._start_workers()
        threading.Thread(target=self._traffic_loop, daemon=True, name="ServiceXTraffic").start()

    def _start_workers(self) -> None:
        for index in range(self.workers):
            thread = threading.Thread(target=self._worker, daemon=True, name=f"ServiceXWorker-{index + 1}")
            self.threads.append(thread)
            thread.start()

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                created = self.queue.get(timeout=0.1)
            except Exception:
                continue
            started = time.perf_counter()
            with self.lock:
                delay = self.delay_ms * self.service_delay_multiplier
                error_rate = self.error_rate
            time.sleep(max(0.001, delay / 1000.0))
            failed = random.random() < error_rate
            with self.lock:
                if failed:
                    self.errors += 1
                else:
                    self.processed += 1
                self.latencies.append((time.perf_counter() - created) * 1000.0)
                self.latencies = self.latencies[-500:]
            self.queue.task_done()

    def submit(self) -> bool:
        with self.lock:
            admitted = random.random() <= self.intake_factor
        if not admitted:
            with self.lock:
                self.dropped += 1
            return False
        try:
            self.queue.put_nowait(time.perf_counter())
            return True
        except Full:
            with self.lock:
                self.dropped += 1
            return False

    def _traffic_loop(self) -> None:
        last = time.monotonic()
        carry = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            elapsed = now - last
            last = now
            with self.lock:
                carry += self.rate * self.traffic_multiplier * elapsed
            while carry >= 1.0:
                self.submit()
                carry -= 1.0
            time.sleep(0.02)

    def metrics(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            lat = list(self.latencies)
            processed = self.processed
            errors = self.errors
            dropped = self.dropped
            delay = self.delay_ms * self.service_delay_multiplier
            rate = self.rate * self.traffic_multiplier
            intake = self.intake_factor
            workers = self.workers
        elapsed = max(0.001, now - self.last_metrics_at)
        processed_delta = max(0, processed - self.last_processed)
        errors_delta = max(0, errors - self.last_errors)
        dropped_delta = max(0, dropped - self.last_dropped)
        self.last_metrics_at = now
        self.last_processed = processed
        self.last_errors = errors
        self.last_dropped = dropped
        cpu_seconds = sum(os.times()[:2])
        cpu_elapsed = max(0.001, now - self.last_cpu_at)
        cpu_pct = min(100.0, max(0.0, (cpu_seconds - self.last_cpu_seconds) / cpu_elapsed /
                                 max(1, os.cpu_count() or 1) * 100.0))
        self.last_cpu_at = now
        self.last_cpu_seconds = cpu_seconds
        total = errors_delta + dropped_delta + processed_delta
        return {
            "arrival_rate": round(rate, 3),
            "throughput": round(processed_delta / elapsed, 3),
            "queue_depth": self.queue.qsize(),
            "latency_ms": round(sum(lat) / len(lat), 3) if lat else round(delay, 3),
            "error_rate": round((errors_delta + dropped_delta) / max(1, total), 5),
            "cpu_pct": round(cpu_pct, 3),
            "service_time_ms": round(delay, 3),
            "shed_rate": round(max(0.0, 1.0 - intake), 4),
            "workers": workers,
            "intake_factor": intake,
            "simulated": False,
        }

    def control(self, body: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if "workers" in body:
                target = int(body["workers"])
                if target < 1 or target > 16:
                    raise ValueError("workers must be in [1, 16]")
                if target > self.workers:
                    old = self.workers
                    self.workers = target
                    for index in range(old, target):
                        thread = threading.Thread(target=self._worker, daemon=True,
                                                  name=f"ServiceXWorker-{index + 1}")
                        self.threads.append(thread)
                        thread.start()
                elif target != self.workers:
                    raise ValueError("workers can only be increased while Service X is running")
            for key, low, high in (("traffic_multiplier", 0.1, 5.0),
                                    ("service_delay_multiplier", 0.1, 5.0),
                                    ("intake_factor", 0.5, 1.0),
                                    ("error_rate", 0.0, 1.0)):
                if key in body:
                    value = float(body[key])
                    if not low <= value <= high:
                        raise ValueError(f"{key} must be in [{low}, {high}]")
                    setattr(self, key, value)
        return self.metrics()


SERVICE: ServiceX


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path == "/metrics":
            return self._send(200, SERVICE.metrics())
        if self.path == "/snapshot":
            return self._send(200, dict(SERVICE.metrics(), service="X"))
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/request":
                accepted = SERVICE.submit()
                self._send(202 if accepted else 503, {"accepted": accepted})
            elif self.path == "/control":
                self._send(200, SERVICE.control(body))
            else:
                self._send(404, {"error": "not found"})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Service X live demo server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--rate", type=float, default=12.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--delay-ms", type=float, default=80.0)
    args = parser.parse_args()
    global SERVICE
    SERVICE = ServiceX(args.rate, args.workers, args.delay_ms)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Service X listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SERVICE.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
