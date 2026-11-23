"""Approval / lock state and per-stage status.

Status lifecycle:
  draft     spec has gaps (unanswered questions, no approach, no tests, ...)
  ready     spec complete, waiting for the researcher to approve it
  approved  researcher approved; agent may implement the stage's files
  failing   implemented but last `arx verify` failed; agent may keep fixing
  verified  all checks passed; files are LOCKED (agent can no longer edit)
  stale     was verified, but its spec or an upstream stage changed -> re-verify
  tampered  locked files changed on disk -> someone edited verified code
  blocked   failed verification `max_attempts` times -> the researcher decides what's next
  archived  removed from the pipeline by the researcher (kept for reference)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path

from .spec import Project, canonical_hash

EDITABLE = {"approved", "failing"}


def now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


class State:
    def __init__(self, project: Project):
        self.project = project
        self.path = project.arx_dir / "state.json"
        self.results_dir = project.arx_dir / "results"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.data.setdefault("approvals", {})
        self.data.setdefault("locks", {})
        self.data.setdefault("attempts", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    @property
    def approvals(self) -> dict:
        return self.data["approvals"]

    @property
    def locks(self) -> dict:
        return self.data["locks"]

    # ---------- results ----------
    def result(self, sid: str) -> dict | None:
        p = self.results_dir / f"{sid}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def save_result(self, sid: str, result: dict) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.results_dir / f"{sid}.json").write_text(json.dumps(result, indent=2, default=str))

    @property
    def attempts(self) -> dict:
        return self.data["attempts"]

    def reset_attempts(self, sid: str) -> None:
        self.attempts[sid] = 0
        self.save()

    # ---------- approve / lock ----------
    def approve(self, sid: str) -> None:
        self.approvals[sid] = {"spec_hash": self.project.stage_hash(sid), "at": now()}
        self.attempts[sid] = 0
        self.save()

    def lock_token(self, sid: str) -> str | None:
        lock = self.locks.get(sid)
        return canonical_hash([lock["files"], lock["spec_hash"]]) if lock else None

    def lock(self, sid: str) -> None:
        p = self.project
        self.locks[sid] = {
            "files": {f: file_hash(p.root / f) for f in p.stage_files(sid)},
            "spec_hash": p.stage_hash(sid),
            "upstream": {d: self.lock_token(d) for d in p.deps(sid)},
            "at": now(),
        }
        self.save()

    def unlock(self, sid: str) -> None:
        self.locks.pop(sid, None)
        self.attempts[sid] = 0
        self.save()

    # ---------- status ----------
    def status(self, sid: str) -> dict:
        p = self.project
        if p.is_archived(sid):
            return {"status": "archived", "reasons": ["archived by the researcher"]}
        h = p.stage_hash(sid)
        lock = self.locks.get(sid)
        if lock:
            changed = [f for f, fh in lock["files"].items() if file_hash(p.root / f) != fh]
            if changed:
                return {"status": "tampered", "reasons": [f"locked file changed after verification: {f}" for f in changed]}
            if lock["spec_hash"] != h:
                return {"status": "stale", "reasons": ["spec changed after verification; re-run `arx verify`"]}
            ups = [d for d in p.deps(sid) if lock["upstream"].get(d) != self.lock_token(d)
                   or self.status(d)["status"] != "verified"]
            if ups:
                return {"status": "stale", "reasons": [f"upstream '{d}' changed or is no longer verified" for d in ups]}
            return {"status": "verified", "reasons": []}

        issues = p.issues(sid)
        if issues:
            return {"status": "draft", "reasons": issues}
        appr = self.approvals.get(sid)
        if not appr:
            return {"status": "ready", "reasons": ["spec complete; waiting for the researcher to approve it"]}
        if appr["spec_hash"] != h:
            return {"status": "ready", "reasons": ["spec changed since approval; the researcher must re-approve"]}
        res = self.result(sid)
        if res and not res.get("passed") and res.get("spec_hash") == h:
            failed = [f"failing check: {c['name']}" for c in res.get("checks", []) if not c.get("passed")]
            n, cap = self.attempts.get(sid, 0), int(p.policy["max_attempts"])
            if cap and n >= cap:
                return {"status": "blocked", "reasons": [f"failed verification {n} times; the researcher decides "
                                                         f"what's next"] + failed}
            return {"status": "failing", "reasons": failed + [f"{n} of {cap} attempts used"]}
        return {"status": "approved", "reasons": []}

    def can_edit(self, sid: str) -> tuple[bool, list[str]]:
        st = self.status(sid)
        reasons = []
        if st["status"] not in EDITABLE:
            reasons.append(f"stage '{sid}' is {st['status'].upper()}")
            reasons += st["reasons"]
        for d in self.project.deps(sid):
            if d in self.project.stages and self.status(d)["status"] != "verified":
                reasons.append(f"upstream stage '{d}' is not verified yet (build the pipeline in order)")
        return (not reasons, reasons)
