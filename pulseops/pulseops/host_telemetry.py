"""Host and hardware telemetry collector with native IBM Z / LinuxONE (s390x) detection.

Reads real OS and hardware metrics without requiring external binary wheels.
Gracefully detects:
  - IBM Z Mainframe architecture (s390x, machine types z13/z14/z15/z16)
  - /proc/cpuinfo and /proc/meminfo on Linux
  - Real process memory and CPU utilization (via psutil if installed, or OS primitives)
"""
from __future__ import annotations

import os
import platform
import sys
import time
from typing import Any, Dict, Optional


def get_host_info() -> Dict[str, Any]:
    """Inspects the runtime environment and host hardware."""
    machine = platform.machine().lower()
    system = platform.system()
    is_s390x = "s390" in machine or "ibm" in machine
    
    info: Dict[str, Any] = {
        "machine": platform.machine(),
        "system": system,
        "release": platform.release(),
        "python_version": sys.version.split()[0],
        "is_linuxone": is_s390x,
        "platform_label": "IBM Z / LinuxONE (s390x)" if is_s390x else f"{system} ({platform.machine()})",
        "cpu_count": os.cpu_count() or 1,
        "mainframe_details": None,
    }

    # On Linux / LinuxONE, inspect /proc/cpuinfo for IBM Z machine details
    if system == "Linux" and os.path.exists("/proc/cpuinfo"):
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                details: Dict[str, str] = {}
                for line in content.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k, v = k.strip().lower(), v.strip()
                        if k in ("vendor_id", "machine", "processor", "features", "facilities"):
                            details[k] = v
                if details:
                    info["mainframe_details"] = details
                    if "vendor_id" in details and "ibm" in details["vendor_id"].lower():
                        info["is_linuxone"] = True
                        info["platform_label"] = f"IBM Z ({details.get('machine', 's390x')})"
        except Exception:
            pass

    return info


def get_live_system_metrics() -> Dict[str, Any]:
    """Captures real host CPU, memory, and process statistics."""
    metrics: Dict[str, Any] = {
        "timestamp": time.time(),
        "host_cpu_pct": None,
        "process_memory_mb": None,
        "load_avg": None,
    }

    # Load average (Linux/Unix/LinuxONE)
    if hasattr(os, "getloadavg"):
        try:
            metrics["load_avg"] = list(os.getloadavg())
        except Exception:
            pass

    # Try psutil for process memory & CPU if available
    try:
        import psutil
        proc = psutil.Process()
        metrics["host_cpu_pct"] = round(psutil.cpu_percent(interval=None), 1)
        mem = proc.memory_info()
        metrics["process_memory_mb"] = round(mem.rss / (1024 * 1024), 2)
    except Exception:
        # Fallback to standard library / Linux /proc
        if os.path.exists("/proc/self/statm"):
            try:
                with open("/proc/self/statm", "r") as f:
                    pages = int(f.read().split()[0])
                    page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
                    metrics["process_memory_mb"] = round((pages * page_size) / (1024 * 1024), 2)
            except Exception:
                pass

    return metrics
