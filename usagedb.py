"""SQLite storage for kubmonitor usage accounting.

Written by `kubmonitor collect` (one-shot snapshots), read by
`kubmonitor report`. The schema is append/upsert-only so concurrent
readers are safe; WAL mode keeps a cron-driven writer from blocking them.
"""

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS workloads (
    uid          TEXT PRIMARY KEY,   -- k8s metadata.uid
    project      TEXT NOT NULL,
    namespace    TEXT NOT NULL,
    kind         TEXT NOT NULL,      -- Job | Pod
    name         TEXT NOT NULL,
    account      TEXT,               -- resolved cluster account, NULL if unknown
    attribution  TEXT NOT NULL,      -- label | name | image | none
    purpose      TEXT,               -- batch | interactive | serving | NULL
    gpu_count    INTEGER NOT NULL DEFAULT 0,
    gpu_model    TEXT,
    cpu_request  REAL,
    mem_request_gb REAL,
    node         TEXT,
    created_at   TEXT,               -- ISO8601 UTC
    started_at   TEXT,
    completed_at TEXT,
    phase        TEXT,               -- Pending|Running|Succeeded|Failed|Unknown
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workloads_window
    ON workloads (project, started_at, completed_at);

CREATE TABLE IF NOT EXISTS quota_snapshots (
    ts       TEXT NOT NULL,
    project  TEXT NOT NULL,
    resource TEXT NOT NULL,          -- cpu | mem | gpu
    used     REAL,
    hard     REAL
);
CREATE INDEX IF NOT EXISTS idx_quota_ts ON quota_snapshots (project, ts);

CREATE TABLE IF NOT EXISTS util_samples (
    ts           TEXT NOT NULL,
    project      TEXT NOT NULL,
    pod_uid      TEXT NOT NULL,
    pod_name     TEXT NOT NULL,
    account      TEXT,
    gpu_index    INTEGER,
    util_pct     REAL,
    mem_used_mb  REAL,
    mem_total_mb REAL
);
CREATE INDEX IF NOT EXISTS idx_util_pod ON util_samples (project, pod_uid);

CREATE TABLE IF NOT EXISTS collect_runs (
    ts      TEXT NOT NULL,
    project TEXT NOT NULL,
    ok      INTEGER NOT NULL,
    pods    INTEGER,
    jobs    INTEGER,
    note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_ts ON collect_runs (project, ts);
"""


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def upsert_workload(conn, w):
    """Insert or refresh one workload snapshot dict (keys = column names).

    Timestamps/phase advance monotonically: an already-recorded
    completed_at is never erased by a later partial observation, and
    first_seen is preserved.
    """
    now = utcnow_iso()
    existing = conn.execute(
        "SELECT first_seen, completed_at, started_at, created_at "
        "FROM workloads WHERE uid = ?", (w["uid"],)).fetchone()
    first_seen = existing["first_seen"] if existing else now
    if existing:
        w = dict(w)
        w["completed_at"] = w.get("completed_at") or existing["completed_at"]
        w["started_at"] = w.get("started_at") or existing["started_at"]
        w["created_at"] = w.get("created_at") or existing["created_at"]
    conn.execute(
        """INSERT INTO workloads (uid, project, namespace, kind, name,
               account, attribution, purpose, gpu_count, gpu_model,
               cpu_request, mem_request_gb, node, created_at, started_at,
               completed_at, phase, first_seen, last_seen)
           VALUES (:uid, :project, :namespace, :kind, :name, :account,
               :attribution, :purpose, :gpu_count, :gpu_model, :cpu_request,
               :mem_request_gb, :node, :created_at, :started_at,
               :completed_at, :phase, :first_seen, :last_seen)
           ON CONFLICT(uid) DO UPDATE SET
               account = excluded.account,
               attribution = excluded.attribution,
               purpose = excluded.purpose,
               gpu_count = excluded.gpu_count,
               gpu_model = COALESCE(excluded.gpu_model, workloads.gpu_model),
               cpu_request = excluded.cpu_request,
               mem_request_gb = excluded.mem_request_gb,
               node = COALESCE(excluded.node, workloads.node),
               created_at = excluded.created_at,
               started_at = excluded.started_at,
               completed_at = excluded.completed_at,
               phase = excluded.phase,
               last_seen = excluded.last_seen""",
        {**w, "first_seen": first_seen, "last_seen": now})


def add_quota_snapshot(conn, ts, project, resource, used, hard):
    conn.execute(
        "INSERT INTO quota_snapshots (ts, project, resource, used, hard) "
        "VALUES (?, ?, ?, ?, ?)", (ts, project, resource, used, hard))


def add_util_sample(conn, ts, project, pod_uid, pod_name, account,
                    gpu_index, util_pct, mem_used_mb, mem_total_mb):
    conn.execute(
        "INSERT INTO util_samples (ts, project, pod_uid, pod_name, account, "
        "gpu_index, util_pct, mem_used_mb, mem_total_mb) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, project, pod_uid, pod_name, account, gpu_index, util_pct,
         mem_used_mb, mem_total_mb))


def add_collect_run(conn, ts, project, ok, pods, jobs, note=""):
    conn.execute(
        "INSERT INTO collect_runs (ts, project, ok, pods, jobs, note) "
        "VALUES (?, ?, ?, ?, ?, ?)", (ts, project, int(ok), pods, jobs, note))


def last_collect_ts(conn, project):
    row = conn.execute(
        "SELECT MAX(ts) AS ts FROM collect_runs WHERE project = ? AND ok = 1",
        (project,)).fetchone()
    return row["ts"] if row and row["ts"] else None
