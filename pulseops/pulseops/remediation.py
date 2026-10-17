"""Remediation engine: explicit allowlist, bounds, cooldowns, human approval, dry-run, audit trail.

Nothing here executes shell commands or anything model-generated. Each action is a fixed Python
function operating on a `ServiceHandle` (the team-owned simulator). Unknown actions, out-of-bounds
parameters, cooldown violations and repeated actions are rejected and recorded as `blocked`.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class RemediationConfig:
    dry_run: bool = False
    min_workers: int = 2
    max_workers: int = 8
    max_worker_step: int = 4          # max change in a single action
    min_intake: float = 0.5           # never shed more than 50% of traffic
    cooldown_s: float = 30.0          # sim-seconds between executions of the same action name
    max_actions_per_incident: int = 3
    backup_dir: Optional[str] = None  # where snapshot_state writes JSON backups (None = in-memory only)


@dataclass
class ActionSpec:
    name: str
    description: str
    requires_approval: bool
    changes_service: bool


ALLOWLIST: Dict[str, ActionSpec] = {
    "snapshot_state": ActionSpec("snapshot_state", "Back up the simulated service state (read-only, non-destructive)",
                                 requires_approval=False, changes_service=False),
    "scale_workers": ActionSpec("scale_workers", "Set worker count within configured bounds",
                                requires_approval=True, changes_service=True),
    "throttle_intake": ActionSpec("throttle_intake", "Shed a bounded fraction of incoming requests",
                                  requires_approval=True, changes_service=True),
}

# statuses: proposed -> approved -> executed | failed | dry_run ; or rejected / blocked
TERMINAL = {"executed", "failed", "dry_run", "rejected", "blocked"}


@dataclass
class ActionRecord:
    id: str
    incident_id: Optional[str]
    action: str
    params: Dict[str, Any]
    rationale: str
    status: str = "proposed"
    requires_approval: bool = True
    requested_t: float = 0.0
    requested_wall: float = 0.0
    approved_by: Optional[str] = None
    approved_t: Optional[float] = None
    executed_t: Optional[float] = None
    executed_wall: Optional[float] = None
    dry_run: bool = False
    result: Optional[str] = None
    blocked_reason: Optional[str] = None
    state_before: Optional[Dict[str, Any]] = None
    state_after: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return asdict(self)


class RemediationEngine:
    def __init__(self, service: Any, cfg: Optional[RemediationConfig] = None,
                 audit_path: Optional[str] = None, wall_clock: Callable[[], float] = time.time):
        self.service = service
        self.cfg = cfg or RemediationConfig()
        self.audit_path = audit_path
        self.wall = wall_clock
        self.records: Dict[str, ActionRecord] = {}
        self._seq = 0
        self.last_exec_t: Dict[str, float] = {}

    # ------------------------------------------------------------------ validation
    def validate(self, action: str, params: Dict[str, Any], incident_id: Optional[str],
                 now: float, exclude_id: Optional[str] = None) -> Tuple[bool, str]:
        c = self.cfg
        if action not in ALLOWLIST:
            return False, f"action '{action}' is not in the allowlist"
        if not isinstance(params, dict):
            return False, "params must be an object"
        allowed_keys = {"snapshot_state": set(), "scale_workers": {"target_workers"},
                        "throttle_intake": {"factor"}}[action]
        extra = set(params) - allowed_keys
        if extra:
            return False, f"unexpected parameters: {sorted(extra)}"

        # loop prevention: same action+params already executed (or dry-run) for this incident
        if incident_id is not None:
            prior = [r for r in self.records.values() if r.incident_id == incident_id and r.id != exclude_id
                     and r.status in ("executed", "dry_run", "failed")]
            if any(r.action == action and r.params == params for r in prior):
                return False, "identical action already applied for this incident (loop prevention)"
            if len([r for r in prior if ALLOWLIST[r.action].changes_service]) >= c.max_actions_per_incident \
                    and ALLOWLIST[action].changes_service:
                return False, f"max {c.max_actions_per_incident} remediation actions per incident reached"

        last = self.last_exec_t.get(action)
        if ALLOWLIST[action].changes_service and last is not None and (now - last) < c.cooldown_s:
            return False, f"cooldown: '{action}' ran {now - last:.0f}s ago (< {c.cooldown_s:.0f}s)"

        if action == "scale_workers":
            n = params.get("target_workers")
            if isinstance(n, bool) or not isinstance(n, int):
                return False, "target_workers must be an integer"
            if n < c.min_workers or n > c.max_workers:
                return False, f"target_workers={n} outside allowed range [{c.min_workers}, {c.max_workers}]"
            cur = self.service.workers
            if n == cur:
                return False, f"already at {cur} workers"
            if abs(n - cur) > c.max_worker_step:
                return False, f"change of {abs(n - cur)} exceeds max step {c.max_worker_step}"
        elif action == "throttle_intake":
            f = params.get("factor")
            if isinstance(f, bool) or not isinstance(f, (int, float)) or not math.isfinite(f):
                return False, "factor must be a finite number"
            if f < c.min_intake or f >= 1.0:
                return False, f"factor={f} outside allowed range [{c.min_intake}, 1.0)"
            if abs(self.service.intake_factor - f) < 1e-9:
                return False, "intake factor already at requested value"
        return True, "ok"

    # ------------------------------------------------------------------ lifecycle
    def _new_id(self) -> str:
        self._seq += 1
        return f"A{self._seq:03d}"

    def _audit(self, rec: ActionRecord, event: str, now: float) -> None:
        if not self.audit_path:
            return
        os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, "wall": self.wall(), "sim_t": now, "record": rec.to_dict()}) + "\n")

    def propose(self, incident_id: Optional[str], action: str, params: Dict[str, Any],
                rationale: str, now: float) -> ActionRecord:
        spec = ALLOWLIST.get(action)
        rec = ActionRecord(self._new_id(), incident_id, action, dict(params) if isinstance(params, dict) else {},
                           rationale, requires_approval=spec.requires_approval if spec else True,
                           requested_t=now, requested_wall=self.wall(), dry_run=self.cfg.dry_run)
        ok, why = self.validate(action, params, incident_id, now)
        if not ok:
            rec.status, rec.blocked_reason = "blocked", why
        self.records[rec.id] = rec
        self._audit(rec, "proposed" if ok else "blocked", now)
        return rec

    def approve(self, rec_id: str, who: str, now: float) -> ActionRecord:
        rec = self.records[rec_id]
        if rec.status != "proposed":
            raise ValueError(f"action {rec_id} is '{rec.status}', cannot approve")
        rec.status, rec.approved_by, rec.approved_t = "approved", who, now
        self._audit(rec, "approved", now)
        return rec

    def reject(self, rec_id: str, who: str, now: float) -> ActionRecord:
        rec = self.records[rec_id]
        if rec.status not in ("proposed", "approved"):
            raise ValueError(f"action {rec_id} is '{rec.status}', cannot reject")
        rec.status, rec.approved_by, rec.result = "rejected", who, "rejected by operator"
        self._audit(rec, "rejected", now)
        return rec

    def execute(self, rec_id: str, now: float) -> ActionRecord:
        rec = self.records[rec_id]
        if rec.requires_approval and rec.status != "approved":
            raise PermissionError(f"action {rec_id} requires approval (status '{rec.status}')")
        if not rec.requires_approval and rec.status not in ("proposed", "approved"):
            raise ValueError(f"action {rec_id} is '{rec.status}', cannot execute")
        ok, why = self.validate(rec.action, rec.params, rec.incident_id, now, exclude_id=rec.id)
        if not ok:  # state may have changed since proposal; re-check bounds at execution time
            rec.status, rec.blocked_reason = "blocked", why
            self._audit(rec, "blocked", now)
            return rec
        rec.state_before = self.service.snapshot()
        rec.executed_t, rec.executed_wall, rec.dry_run = now, self.wall(), self.cfg.dry_run
        try:
            if self.cfg.dry_run:
                rec.status = "dry_run"
                rec.result = f"DRY-RUN: would apply {rec.action} {rec.params}; no change made"
            else:
                rec.result = self._apply(rec)
                rec.status = "executed"
            if ALLOWLIST[rec.action].changes_service and not self.cfg.dry_run:
                self.last_exec_t[rec.action] = now
        except Exception as e:  # noqa: BLE001 - record any executor failure
            rec.status, rec.result = "failed", f"execution error: {e}"
        rec.state_after = self.service.snapshot()
        self._audit(rec, rec.status, now)
        return rec

    def _apply(self, rec: ActionRecord) -> str:
        a, p = rec.action, rec.params
        if a == "scale_workers":
            old = self.service.workers
            self.service.set_workers(p["target_workers"])
            return f"workers {old} -> {p['target_workers']}"
        if a == "throttle_intake":
            old = self.service.intake_factor
            self.service.set_intake(p["factor"])
            return f"intake factor {old:.2f} -> {p['factor']:.2f} (sheds {(1 - p['factor']) * 100:.0f}% of requests)"
        if a == "snapshot_state":
            snap = self.service.snapshot()
            if self.cfg.backup_dir:
                os.makedirs(self.cfg.backup_dir, exist_ok=True)
                path = os.path.join(self.cfg.backup_dir, f"backup_{rec.id}_{int(self.wall())}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(snap, f, indent=2)
                return f"state backed up to {path}"
            return "state captured (in-memory)"
        raise ValueError("unreachable: action not in allowlist")

    def list_records(self) -> List[dict]:
        return [r.to_dict() for r in self.records.values()]
