"""HTTP adapter for a separately running Service X instance."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class LiveServiceError(RuntimeError):
    """Raised when Service X cannot be reached or returns invalid telemetry."""


class LiveServiceAdapter:
    """ServiceHandle-compatible adapter backed by Service X's JSON API."""

    def __init__(self, base_url: str, timeout_s: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._t = 0.0
        self.fault_started_t: Optional[float] = None
        self.fault_cleared_t: Optional[float] = None
        self.fault: Optional[Dict[str, Any]] = None

    def _request(self, path: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                detail = None
            message = detail.get("error") if isinstance(detail, dict) else str(exc)
            raise LiveServiceError(f"Service X rejected {path}: {message}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise LiveServiceError(f"Service X request failed: {exc}") from exc
        if not isinstance(result, dict):
            raise LiveServiceError("Service X returned a non-object response")
        return result

    def _metrics(self) -> Dict[str, Any]:
        result = self._request("/metrics")
        required = ("arrival_rate", "throughput", "queue_depth", "latency_ms",
                    "error_rate", "cpu_pct", "service_time_ms", "shed_rate", "workers")
        missing = [key for key in required if key not in result]
        if missing:
            raise LiveServiceError(f"Service X telemetry missing fields: {', '.join(missing)}")
        return result

    @property
    def workers(self) -> int:
        return int(self._metrics()["workers"])

    @property
    def intake_factor(self) -> float:
        return float(self._metrics().get("intake_factor", 1.0))

    @property
    def t(self) -> float:
        return self._t

    def warmup_step(self) -> Dict[str, Any]:
        return self.step()

    def step(self) -> Dict[str, Any]:
        self._t += 1.0
        sample = self._metrics()
        sample["t"] = self._t
        sample["simulated"] = False
        return sample

    def snapshot(self) -> Dict[str, Any]:
        result = self._request("/snapshot")
        result["simulated"] = False
        return result

    def _control(self, **payload: Any) -> Dict[str, Any]:
        return self._request("/control", method="POST", payload=payload)

    def control(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"traffic_multiplier", "service_delay_multiplier", "error_rate", "intake_factor", "workers"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unsupported Service X controls: {', '.join(unknown)}")
        return self._control(**payload)

    def set_workers(self, n: int) -> None:
        self._control(workers=int(n))

    def set_intake(self, factor: float) -> None:
        self._control(intake_factor=float(factor))

    def inject_fault(self, kind: str, magnitude: float) -> None:
        if kind == "load_spike":
            result = self._control(traffic_multiplier=float(magnitude))
        elif kind == "slow_downstream":
            result = self._control(service_delay_multiplier=float(magnitude))
        else:
            raise ValueError(f"unsupported live fault '{kind}'")
        self.fault = {"kind": kind, "magnitude": float(magnitude)}
        self.fault_started_t = self._t
        self.fault_cleared_t = None
        return result

    def clear_fault(self, record: bool = True) -> None:
        self._control(traffic_multiplier=1.0, service_delay_multiplier=1.0)
        if self.fault is not None and record:
            self.fault_cleared_t = self._t
        self.fault = None

    def reset_service(self) -> None:
        self._control(reset=True)
        self.fault = None
        self.fault_started_t = None
        self.fault_cleared_t = None

    def stop(self) -> None:
        return None
