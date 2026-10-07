"""Conexion y esquema SQLite.

Migraciones por version incremental: cada entrada de MIGRATIONS es una lista de
sentencias que se aplica una sola vez y queda registrada en `schema_meta`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_V1 = [
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # ---------------- Cuentas y publicaciones ----------------
    """
    CREATE TABLE IF NOT EXISTS accounts (
        platform      TEXT NOT NULL,
        account_id    TEXT NOT NULL,
        username      TEXT,
        name          TEXT,
        counters_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        PRIMARY KEY (platform, account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media (
        platform       TEXT NOT NULL,
        media_id       TEXT NOT NULL,
        account_id     TEXT NOT NULL,
        media_type     TEXT,
        product_type   TEXT,
        permalink      TEXT,
        published_at   TEXT,
        caption        TEXT,
        like_count     INTEGER,
        comments_count INTEGER,
        first_seen_at  TEXT NOT NULL,
        last_seen_at   TEXT NOT NULL,
        PRIMARY KEY (platform, media_id)
    )
    """,
    # ---------------- Snapshots de metricas ----------------
    # value es REAL: hay metricas de duracion en milisegundos.
    """
    CREATE TABLE IF NOT EXISTS metric_snapshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at TEXT NOT NULL,
        platform    TEXT NOT NULL,
        account_id  TEXT NOT NULL,
        media_id    TEXT,
        metric      TEXT NOT NULL,
        period      TEXT NOT NULL,
        value       REAL NOT NULL,
        run_id      TEXT NOT NULL
    )
    """,
    # Idempotencia: una sola fila por (run, plataforma, cuenta, publicacion, metrica, periodo).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshot_run
        ON metric_snapshots (run_id, platform, account_id,
                             COALESCE(media_id, ''), metric, period)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_snapshot_series
        ON metric_snapshots (platform, account_id, COALESCE(media_id, ''),
                             metric, period, captured_at DESC)
    """,
    # ---------------- Calidad de datos ----------------
    """
    CREATE TABLE IF NOT EXISTS metric_gaps (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id     TEXT NOT NULL,
        platform   TEXT NOT NULL,
        account_id TEXT NOT NULL,
        media_id   TEXT,
        metric     TEXT NOT NULL,
        reason     TEXT NOT NULL,
        detail     TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_quality_issues (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id        TEXT NOT NULL,
        platform      TEXT NOT NULL,
        kind          TEXT NOT NULL,
        severity      TEXT NOT NULL,
        subject       TEXT NOT NULL,
        detail        TEXT NOT NULL,
        observed_json TEXT NOT NULL DEFAULT '{}',
        created_at    TEXT NOT NULL
    )
    """,
    # ---------------- Ejecuciones ----------------
    """
    CREATE TABLE IF NOT EXISTS collection_runs (
        run_id       TEXT PRIMARY KEY,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        status       TEXT NOT NULL,
        trigger      TEXT NOT NULL DEFAULT 'manual',
        summary_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS platform_outcomes (
        run_id            TEXT NOT NULL,
        platform          TEXT NOT NULL,
        status            TEXT NOT NULL,
        connected         INTEGER NOT NULL DEFAULT 0,
        media_seen        INTEGER NOT NULL DEFAULT 0,
        snapshots_written INTEGER NOT NULL DEFAULT 0,
        gaps              INTEGER NOT NULL DEFAULT 0,
        issues            INTEGER NOT NULL DEFAULT 0,
        pages_fetched     INTEGER NOT NULL DEFAULT 0,
        api_calls         INTEGER NOT NULL DEFAULT 0,
        token_state       TEXT NOT NULL DEFAULT 'unknown',
        skipped_reason    TEXT,
        errors_json       TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (run_id, platform)
    )
    """,
    # Respuestas crudas utiles, ya saneadas (sin tokens ni URLs de paginacion).
    """
    CREATE TABLE IF NOT EXISTS raw_responses (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id       TEXT NOT NULL,
        platform     TEXT NOT NULL,
        endpoint     TEXT NOT NULL,
        captured_at  TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id     TEXT,
        kind       TEXT NOT NULL,
        path       TEXT NOT NULL,
        sha256     TEXT,
        created_at TEXT NOT NULL
    )
    """,
    # ---------------- Estado de agentes ----------------
    """
    CREATE TABLE IF NOT EXISTS agent_state (
        agent         TEXT NOT NULL,
        platform      TEXT NOT NULL DEFAULT '*',
        last_run_at   TEXT,
        last_status   TEXT,
        last_error    TEXT,
        last_success_at TEXT,
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (agent, platform)
    )
    """,
    # ---------------- Cola de tareas ----------------
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id               TEXT PRIMARY KEY,
        recipient        TEXT NOT NULL,
        platform         TEXT,
        type             TEXT NOT NULL,
        schema_version   INTEGER NOT NULL,
        payload          TEXT NOT NULL,
        status           TEXT NOT NULL,
        priority         INTEGER NOT NULL DEFAULT 5,
        attempts         INTEGER NOT NULL DEFAULT 0,
        max_attempts     INTEGER NOT NULL DEFAULT 3,
        idempotency_key  TEXT NOT NULL UNIQUE,
        correlation_id   TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        available_at     TEXT NOT NULL,
        claimed_at       TEXT,
        claimed_by       TEXT,
        lease_expires_at TEXT,
        finished_at      TEXT,
        last_error       TEXT,
        CHECK (status IN ('pending','claimed','done','failed','dead','cancelled'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_tasks_claim
        ON tasks (recipient, status, available_at, priority, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_tasks_lease
        ON tasks (status, lease_expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS task_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        at          TEXT NOT NULL,
        kind        TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_task_events_task ON task_events (task_id, at)
    """,
    """
    CREATE TABLE IF NOT EXISTS task_results (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        at             TEXT NOT NULL,
        status         TEXT NOT NULL,
        summary        TEXT NOT NULL DEFAULT '',
        artifacts_json TEXT NOT NULL DEFAULT '[]'
    )
    """,
]

MIGRATIONS: dict[int, list[str]] = {1: _V1}


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly and path.exists():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    else:
        conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if row is None:
        return 0
    got = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    return int(got["value"]) if got else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Aplica migraciones pendientes. Devuelve la version final."""
    version = _current_version(conn)
    for target in sorted(MIGRATIONS):
        if target <= version:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in MIGRATIONS[target]:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(target),),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        version = target
    return version


def open_database(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
