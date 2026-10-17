"""PulseOps HTTP server (stdlib only: no pip installs, safe on LinuxONE/s390x).

    python -m pulseops.server --port 8000          # then open http://127.0.0.1:8000
Binds to 127.0.0.1 by default. Use SSH port-forwarding on a shared LinuxONE host rather than 0.0.0.0.
All remediation acts on the in-process payment-service SIMULATION only.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from .engine import PulseOpsEngine
from .remediation import RemediationConfig
from .simulator import SimConfig

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC = os.path.join(ROOT, "static")
MAX_BODY = 4096


class App:
    def __init__(self, engine: PulseOpsEngine, tick_interval: float):
        self.engine = engine
        self.lock = threading.RLock()
        self.tick_interval = tick_interval
        self.paused = False
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            if not self.paused:
                with self.lock:
                    try:
                        self.engine.tick()
                    except Exception:  # noqa: BLE001 - keep the loop alive, surface in logs
                        logging.getLogger("pulseops").exception("tick failed")
            self._stop.wait(max(0.0, self.tick_interval - (time.time() - t0)))

    # ------------------------------------------------------------------ API
    def state(self) -> Dict[str, Any]:
        with self.lock:
            s = self.engine.state()
            s["paused"], s["tick_interval"] = self.paused, self.tick_interval
            return s

    def handle_post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        e = self.engine
        with self.lock:
            if path == "/api/fault":
                kind = body.get("kind")
                mag = body.get("magnitude", 2.0)
                if not isinstance(kind, str) or isinstance(mag, bool) or not isinstance(mag, (int, float)):
                    raise ValueError("kind (string) and magnitude (number) required")
                e.inject_fault(kind, float(mag))
            elif path == "/api/fault/clear":
                e.clear_fault()
            elif path in ("/api/approve", "/api/reject"):
                aid = str(body.get("action_id", ""))
                if aid not in e.rem.records:
                    raise ValueError(f"unknown action id '{aid[:20]}'")
                who = str(body.get("who", "operator"))[:40]
                (e.approve if path == "/api/approve" else e.reject)(aid, who)
            elif path == "/api/mode":
                e.set_mode(dry_run=body.get("dry_run"), auto_approve=body.get("auto_approve"))
            elif path == "/api/pause":
                self.paused = bool(body.get("paused", True))
            elif path == "/api/reset":
                e.reset()
            else:
                raise KeyError(path)
        return {"ok": True}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PulseOps/1.0"

        def log_message(self, fmt, *args):  # quiet access log
            pass

        def _send(self, code: int, payload: Any, ctype: str = "application/json") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, app.state())
            if path.startswith("/api/incident/"):
                with app.lock:
                    d = app.engine.incident_detail(path.rsplit("/", 1)[-1])
                return self._send(200 if d else 404, d or {"error": "not found"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._send(413, {"error": "body too large"})
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("JSON object expected")
                return self._send(200, app.handle_post(path, body))
            except KeyError:
                return self._send(404, {"error": "not found"})
            except (ValueError, PermissionError, json.JSONDecodeError) as ex:
                return self._send(400, {"error": str(ex)})

    return Handler


def setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    root = logging.getLogger("pulseops")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.FileHandler(os.path.join(log_dir, "pulseops.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(logging.WARNING)
    root.addHandler(fh)
    root.addHandler(sh)


def main() -> None:
    ap = argparse.ArgumentParser(description="PulseOps live demo server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--tick", type=float, default=0.5, help="wall seconds per simulated second")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true", help="never apply remediation; record what would happen")
    ap.add_argument("--auto-approve", action="store_true", help="skip human approval (bounded actions only)")
    args = ap.parse_args()

    run_dir = os.path.join(ROOT, "runs", time.strftime("%Y%m%d_%H%M%S"))
    setup_logging(os.path.join(ROOT, "logs"))
    engine = PulseOpsEngine(SimConfig(seed=args.seed), RemediationConfig(dry_run=args.dry_run), run_dir=run_dir,
                            auto_approve=args.auto_approve)
    app = App(engine, args.tick)
    app.start()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"PulseOps on http://{args.host}:{args.port}  (simulated service; audit+backups in {run_dir})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
