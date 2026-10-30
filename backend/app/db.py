"""SQLite job store. One row per uploaded photo; the worker updates it as the job moves along."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

# queued -> classifying -> generating -> rigging (characters only) -> downloading -> done | failed
ACTIVE_STATUSES = ("queued", "classifying", "generating", "rigging", "downloading")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    requested_category TEXT NOT NULL,
    category TEXT,
    category_source TEXT,
    classifier_note TEXT,
    original_name TEXT,
    quality TEXT,
    meshy_task_id TEXT,
    rig_task_id TEXT,
    rig_status TEXT,
    files TEXT NOT NULL DEFAULT '{}',
    error TEXT
)
"""

_JSON_FIELDS = ("quality", "files")


class JobStore:
    def __init__(self, path: Path) -> None:
        # Called as listener(job_id, job) after every change; job is None once deleted (see firebase_sync.py)
        self.listeners: list = []
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def _row(self, row: Optional[sqlite3.Row]) -> Optional[dict]:
        if row is None:
            return None
        job = dict(row)
        for field in _JSON_FIELDS:
            job[field] = json.loads(job[field]) if job[field] else ({} if field == "files" else None)
        return job

    def _notify(self, job_id: str, job: Optional[dict]) -> None:
        for listener in self.listeners:
            listener(job_id, job)

    def create(self, requested_category: str, original_name: str, quality: dict, job_id: Optional[str] = None) -> dict:
        now = time.time()
        job_id = job_id or uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, created_at, updated_at, status, requested_category, original_name, quality)"
                " VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (job_id, now, now, requested_category, original_name, json.dumps(quality)),
            )
            self._conn.commit()
        job = self.get(job_id)
        self._notify(job_id, job)
        return job

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row(row)

    def list(self, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        query, args = "SELECT * FROM jobs", []
        if status:
            query += " WHERE status = ?"
            args.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [self._row(r) for r in rows]

    def update(self, job_id: str, **fields) -> dict:
        for field in _JSON_FIELDS:
            if field in fields:
                fields[field] = json.dumps(fields[field])
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))
            self._conn.commit()
        job = self.get(job_id)
        self._notify(job_id, job)
        return job

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._conn.commit()
        self._notify(job_id, None)

    def unfinished(self) -> list[dict]:
        marks = ",".join("?" * len(ACTIVE_STATUSES))
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM jobs WHERE status IN ({marks})", ACTIVE_STATUSES).fetchall()
        return [self._row(r) for r in rows]
