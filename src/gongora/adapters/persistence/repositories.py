"""Repositorios SQLite para datos de recogida, ejecuciones y estado de agentes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from gongora import redaction
from gongora.clock import iso_utc
from gongora.domain.models import (
    AccountProfile,
    CollectionRun,
    DataQualityIssue,
    MediaItem,
    MetricGap,
    MetricSnapshot,
    PlatformOutcome,
)
from gongora.domain.platform import Platform


class SqliteSnapshotRepository:
    """Escritura y lectura de los datos recogidos."""

    def __init__(self, conn: sqlite3.Connection, clock) -> None:
        self._conn = conn
        self._clock = clock

    # ----------------------------------------------------------- escritura

    def save_profile(self, profile: AccountProfile) -> None:
        now = iso_utc(profile.fetched_at)
        self._conn.execute(
            """
            INSERT INTO accounts (platform, account_id, username, name,
                                  counters_json, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, account_id) DO UPDATE SET
                username = excluded.username,
                name = excluded.name,
                counters_json = excluded.counters_json,
                last_seen_at = excluded.last_seen_at
            """,
            (str(profile.platform), profile.account_id, profile.username, profile.name,
             json.dumps(profile.counters, ensure_ascii=False), now, now),
        )

    def save_media(self, media: MediaItem, *, account_id: str) -> None:
        now = iso_utc(self._clock.now())
        self._conn.execute(
            """
            INSERT INTO media (platform, media_id, account_id, media_type, product_type,
                               permalink, published_at, caption, like_count,
                               comments_count, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, media_id) DO UPDATE SET
                media_type = excluded.media_type,
                product_type = excluded.product_type,
                permalink = excluded.permalink,
                published_at = excluded.published_at,
                caption = excluded.caption,
                like_count = excluded.like_count,
                comments_count = excluded.comments_count,
                last_seen_at = excluded.last_seen_at
            """,
            (str(media.platform), media.media_id, account_id, media.media_type,
             media.product_type, media.permalink,
             iso_utc(media.published_at) if media.published_at else None,
             media.caption.raw, media.like_count, media.comments_count, now, now),
        )

    def save_snapshots(self, snapshots: list[MetricSnapshot]) -> int:
        """Inserta snapshots. Reejecutar el mismo run no duplica filas."""
        written = 0
        for snap in snapshots:
            cursor = self._conn.execute(
                """
                INSERT INTO metric_snapshots (captured_at, platform, account_id,
                       media_id, metric, period, value, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (iso_utc(snap.captured_at), str(snap.platform), snap.account_id,
                 snap.media_id, snap.metric, snap.period, float(snap.value), snap.run_id),
            )
            written += cursor.rowcount if cursor.rowcount > 0 else 0
        return written

    def save_gaps(self, gaps: list[MetricGap]) -> int:
        now = iso_utc(self._clock.now())
        for gap in gaps:
            self._conn.execute(
                "INSERT INTO metric_gaps (run_id, platform, account_id, media_id, "
                "metric, reason, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (gap.run_id, str(gap.platform), gap.account_id, gap.media_id, gap.metric,
                 gap.reason, redaction.redact(gap.detail)[:1000], now),
            )
        return len(gaps)

    def save_issues(self, issues: list[DataQualityIssue]) -> int:
        now = iso_utc(self._clock.now())
        for issue in issues:
            self._conn.execute(
                "INSERT INTO data_quality_issues (run_id, platform, kind, severity, "
                "subject, detail, observed_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (issue.run_id, str(issue.platform), issue.kind, issue.severity,
                 issue.subject, redaction.redact(issue.detail)[:2000],
                 json.dumps(redaction.redact_value(issue.observed), ensure_ascii=False), now),
            )
        return len(issues)

    def save_raw_response(self, *, run_id: str, platform: Platform, endpoint: str,
                          payload: Any) -> None:
        """Guarda la respuesta ya saneada: sin tokens ni URLs de paginacion."""
        safe = redaction.redact_value(payload)
        self._conn.execute(
            "INSERT INTO raw_responses (run_id, platform, endpoint, captured_at, "
            "payload_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, str(platform), redaction.redact(endpoint),
             iso_utc(self._clock.now()), json.dumps(safe, ensure_ascii=False)),
        )

    def record_artifact(self, *, run_id: str | None, kind: str, path: Path) -> str | None:
        digest = None
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self._conn.execute(
            "INSERT INTO artifacts (run_id, kind, path, sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, kind, str(path), digest, iso_utc(self._clock.now())),
        )
        return digest

    # ------------------------------------------------------------ lecturas

    def latest_snapshot(self, *, platform: Platform, account_id: str,
                        media_id: str | None, metric: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM metric_snapshots WHERE platform = ? AND account_id = ? "
            "AND COALESCE(media_id,'') = ? AND metric = ? "
            "ORDER BY captured_at DESC, id DESC LIMIT 1",
            (str(platform), account_id, media_id or "", metric),
        ).fetchone()

    def snapshot_series(self, *, platform: Platform, account_id: str,
                        media_id: str | None, metric: str, limit: int = 10) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT captured_at, value, period, run_id FROM metric_snapshots "
            "WHERE platform = ? AND account_id = ? AND COALESCE(media_id,'') = ? "
            "AND metric = ? ORDER BY captured_at DESC, id DESC LIMIT ?",
            (str(platform), account_id, media_id or "", metric, limit),
        ).fetchall()

    def run_snapshots(self, run_id: str, platform: Platform) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM metric_snapshots WHERE run_id = ? AND platform = ? "
            "ORDER BY COALESCE(media_id,''), metric",
            (run_id, str(platform)),
        ).fetchall()

    def run_gaps(self, run_id: str, platform: Platform) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM metric_gaps WHERE run_id = ? AND platform = ? ORDER BY id",
            (run_id, str(platform)),
        ).fetchall()

    def run_issues(self, run_id: str, platform: Platform) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM data_quality_issues WHERE run_id = ? AND platform = ? ORDER BY id",
            (run_id, str(platform)),
        ).fetchall()

    def account(self, platform: Platform, account_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM accounts WHERE platform = ? AND account_id = ?",
            (str(platform), account_id),
        ).fetchone()

    def media_of_run(self, run_id: str, platform: Platform) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT DISTINCT m.* FROM media m
              JOIN metric_snapshots s
                ON s.platform = m.platform AND s.media_id = m.media_id
             WHERE s.run_id = ? AND m.platform = ?
             ORDER BY m.published_at DESC
            """,
            (run_id, str(platform)),
        ).fetchall()

    def all_media(self, platform: Platform, limit: int = 100) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM media WHERE platform = ? ORDER BY published_at DESC LIMIT ?",
            (str(platform), limit),
        ).fetchall()


class SqliteRunRepository:
    """Ciclo de vida de las ejecuciones de recogida."""

    def __init__(self, conn: sqlite3.Connection, clock) -> None:
        self._conn = conn
        self._clock = clock

    def start(self, run: CollectionRun, *, trigger: str = "manual") -> None:
        self._conn.execute(
            "INSERT INTO collection_runs (run_id, started_at, status, trigger) "
            "VALUES (?, ?, 'running', ?)",
            (run.run_id, iso_utc(run.started_at), trigger),
        )

    def finish(self, run: CollectionRun, summary: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            "UPDATE collection_runs SET finished_at = ?, status = ?, summary_json = ? "
            "WHERE run_id = ?",
            (iso_utc(run.finished_at or self._clock.now()), run.status,
             json.dumps(redaction.redact_value(summary or {}), ensure_ascii=False),
             run.run_id),
        )
        for outcome in run.outcomes.values():
            self.save_outcome(run.run_id, outcome)

    def save_outcome(self, run_id: str, outcome: PlatformOutcome) -> None:
        self._conn.execute(
            """
            INSERT INTO platform_outcomes (run_id, platform, status, connected,
                media_seen, snapshots_written, gaps, issues, pages_fetched, api_calls,
                token_state, skipped_reason, errors_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, platform) DO UPDATE SET
                status = excluded.status, connected = excluded.connected,
                media_seen = excluded.media_seen,
                snapshots_written = excluded.snapshots_written,
                gaps = excluded.gaps, issues = excluded.issues,
                pages_fetched = excluded.pages_fetched, api_calls = excluded.api_calls,
                token_state = excluded.token_state,
                skipped_reason = excluded.skipped_reason,
                errors_json = excluded.errors_json
            """,
            (run_id, str(outcome.platform), outcome.status, int(outcome.connected),
             outcome.media_seen, outcome.snapshots_written, outcome.gaps, outcome.issues,
             outcome.pages_fetched, outcome.api_calls, outcome.token_state,
             outcome.skipped_reason,
             json.dumps(redaction.redact_value(outcome.errors), ensure_ascii=False)),
        )

    def get(self, run_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM collection_runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    def latest(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def latest_run_id(self) -> str | None:
        row = self._conn.execute(
            "SELECT run_id FROM collection_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return row["run_id"] if row else None

    def last_successful(self, platform: Platform | None = None) -> sqlite3.Row | None:
        """Ultima recogida satisfactoria (global o de una plataforma)."""
        if platform is None:
            return self._conn.execute(
                "SELECT * FROM collection_runs WHERE status IN ('ok','partial') "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return self._conn.execute(
            """
            SELECT r.* FROM collection_runs r
              JOIN platform_outcomes p ON p.run_id = r.run_id
             WHERE p.platform = ? AND p.status IN ('ok','partial')
             ORDER BY r.started_at DESC LIMIT 1
            """,
            (str(platform),),
        ).fetchone()

    def outcomes(self, run_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM platform_outcomes WHERE run_id = ? ORDER BY platform",
            (run_id,),
        ).fetchall()

    def artifacts(self, run_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at DESC", (run_id,)
        ).fetchall()

    def latest_artifact(self, kind: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM artifacts WHERE kind = ? ORDER BY created_at DESC LIMIT 1",
            (kind,),
        ).fetchone()


class SqliteAgentStateRepository:
    """Estado observable de cada agente: ultima ejecucion, estado y error."""

    def __init__(self, conn: sqlite3.Connection, clock) -> None:
        self._conn = conn
        self._clock = clock

    def record(self, *, agent: str, platform: Platform | str = "*", status: str,
               error: str | None = None, at: datetime | None = None) -> None:
        moment = iso_utc(at or self._clock.now())
        success = moment if status in ("ok", "partial") else None
        self._conn.execute(
            """
            INSERT INTO agent_state (agent, platform, last_run_at, last_status,
                                     last_error, last_success_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent, platform) DO UPDATE SET
                last_run_at = excluded.last_run_at,
                last_status = excluded.last_status,
                last_error = excluded.last_error,
                last_success_at = COALESCE(excluded.last_success_at,
                                           agent_state.last_success_at),
                updated_at = excluded.updated_at
            """,
            (agent, str(platform), moment, status,
             redaction.redact(error)[:1000] if error else None, success, moment),
        )

    def all(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM agent_state ORDER BY agent, platform"
        ).fetchall()

    def get(self, agent: str, platform: Platform | str = "*") -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM agent_state WHERE agent = ? AND platform = ?",
            (agent, str(platform)),
        ).fetchone()
