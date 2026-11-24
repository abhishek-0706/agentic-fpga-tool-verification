"""Stage 7: local SQLite defect tracker with deduplication and a human approval gate.

Lifecycle: draft -> filed (only via approve(), which needs a named human) -> closed (fix verified)
           draft -> rejected
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import DefectDraft

SCHEMA = """
CREATE TABLE IF NOT EXISTS defects (
  id TEXT PRIMARY KEY, signature TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
  title TEXT, component TEXT, severity TEXT, category TEXT, body TEXT,
  occurrences INTEGER DEFAULT 1, created_at TEXT, approved_by TEXT, approved_at TEXT, closed_at TEXT,
  first_run TEXT, last_run TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  ts TEXT, defect_id TEXT, action TEXT, actor TEXT, detail TEXT
);
"""


class ApprovalRequired(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Tracker:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def _audit(self, did: str, action: str, actor: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (_now(), did, action, actor, detail))

    def upsert_draft(self, d: DefectDraft, run_id: str) -> tuple[str, bool]:
        """Insert a draft, or (dedup) bump occurrences of the defect with the same signature."""
        row = self.db.execute("SELECT id, status FROM defects WHERE signature=?", (d.signature,)).fetchone()
        if row and row["status"] == "closed":
            self.db.execute(
                "UPDATE defects SET status='draft', title=?, component=?, severity=?, category=?, body=?,"
                " closed_at=NULL, approved_by=NULL, approved_at=NULL, occurrences=occurrences+1, last_run=?"
                " WHERE id=?",
                (d.title, d.component, d.severity, d.category, d.model_dump_json(), run_id, row["id"]))
            self._audit(row["id"], "reopened", "agent", f"fixed defect failed again in {run_id}")
            self.db.commit()
            return row["id"], True
        if row:
            self.db.execute("UPDATE defects SET occurrences=occurrences+1, last_run=? WHERE id=?", (run_id, row["id"]))
            self._audit(row["id"], "duplicate_seen", "agent", run_id)
            self.db.commit()
            return row["id"], False
        n = self.db.execute("SELECT COUNT(*) FROM defects").fetchone()[0] + 1
        did = f"DEF-{n:04d}"
        self.db.execute(
            "INSERT INTO defects (id, signature, status, title, component, severity, category, body, created_at,"
            " first_run, last_run) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (did, d.signature, "draft", d.title, d.component, d.severity, d.category, d.model_dump_json(),
             _now(), run_id, run_id))
        self._audit(did, "drafted", "agent", run_id)
        self.db.commit()
        return did, True

    def approve(self, did: str, by: str) -> None:
        if not by or not by.strip():
            raise ApprovalRequired("approval needs the name of the approving human (--by)")
        row = self.get(did)
        if row is None or row["status"] != "draft":
            raise ValueError(f"{did}: only drafts can be approved (status={row and row['status']})")
        self.db.execute("UPDATE defects SET status='filed', approved_by=?, approved_at=? WHERE id=?",
                        (by.strip(), _now(), did))
        self._audit(did, "approved_and_filed", by.strip())
        self.db.commit()

    def reject(self, did: str, by: str, reason: str) -> None:
        if not by or not by.strip():
            raise ApprovalRequired("rejection needs the name of the rejecting human (--by)")
        cur = self.db.execute("UPDATE defects SET status='rejected' WHERE id=? AND status='draft'", (did,))
        if cur.rowcount != 1:
            self.db.rollback()
            row = self.get(did)
            raise ValueError(f"{did}: only drafts can be rejected (status={row and row['status']})")
        self._audit(did, "rejected", by.strip(), reason)
        self.db.commit()

    def close(self, did: str, evidence: str) -> None:
        row = self.get(did)
        if row is None or row["status"] != "filed":
            raise ApprovalRequired(f"{did}: only filed (human-approved) defects can be closed")
        self.db.execute("UPDATE defects SET status='closed', closed_at=? WHERE id=?", (_now(), did))
        self._audit(did, "closed_fix_verified", "agent", evidence)
        self.db.commit()

    def get(self, did: str):
        return self.db.execute("SELECT * FROM defects WHERE id=?", (did,)).fetchone()

    def by_signature(self, sig: str):
        return self.db.execute("SELECT * FROM defects WHERE signature=?", (sig,)).fetchone()

    def all(self, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM defects" + (" WHERE status=?" if status else "") + " ORDER BY id"
        rows = self.db.execute(q, (status,) if status else ()).fetchall()
        return [dict(r) | {"body": json.loads(r["body"])} for r in rows]

    def audit(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM audit ORDER BY ts").fetchall()]
