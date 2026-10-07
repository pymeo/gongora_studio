"""Cola de tareas sobre SQLite.

Garantias:
- Reclamacion atomica: un `UPDATE ... WHERE id = (SELECT ...) RETURNING` dentro
  de una transaccion IMMEDIATE. Dos workers nunca obtienen la misma tarea.
- Lease: la tarea reclamada caduca. Si el worker muere, se recupera.
- Idempotencia: `idempotency_key` es UNIQUE; reencolar el mismo trabajo
  devuelve la tarea existente en vez de duplicarla.
- Limite de reintentos: al agotar `max_attempts` la tarea pasa a `dead`,
  nunca vuelve a `pending`. No hay bucles infinitos.
- Trazabilidad: eventos y resultados con referencias a artefactos.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Sequence

from gongora import redaction
from gongora.clock import iso_utc
from gongora.domain.platform import Platform
from gongora.domain.tasks import (
    ArtifactRef,
    PRIORITY_NORMAL,
    PayloadInvalid,
    Task,
    TaskResult,
    latest_version,
    validate_payload,
)

DEFAULT_LEASE_SECONDS = 600
DEFAULT_MAX_ATTEMPTS = 3
#: Espera antes de volver a ofrecer una tarea que fallo (segundos por intento).
RETRY_BACKOFF_SECONDS = (60, 300, 900)


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        recipient=row["recipient"],
        type=row["type"],
        schema_version=row["schema_version"],
        payload=json.loads(row["payload"]),
        status=row["status"],
        priority=row["priority"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        idempotency_key=row["idempotency_key"],
        created_at=_parse_dt(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_parse_dt(row["updated_at"]),  # type: ignore[arg-type]
        available_at=_parse_dt(row["available_at"]),  # type: ignore[arg-type]
        platform=Platform(row["platform"]) if row["platform"] else None,
        correlation_id=row["correlation_id"],
        claimed_at=_parse_dt(row["claimed_at"]),
        claimed_by=row["claimed_by"],
        lease_expires_at=_parse_dt(row["lease_expires_at"]),
        finished_at=_parse_dt(row["finished_at"]),
        last_error=row["last_error"],
    )


class SqliteTaskQueue:
    def __init__(self, conn: sqlite3.Connection, clock) -> None:
        self._conn = conn
        self._clock = clock

    # ------------------------------------------------------------------ write

    def enqueue(
        self,
        *,
        recipient: str,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        platform: Platform | None = None,
        schema_version: int | None = None,
        priority: int = PRIORITY_NORMAL,
        correlation_id: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        available_at: datetime | None = None,
    ) -> tuple[Task, bool]:
        """Encola una tarea. Devuelve (tarea, creada).

        Si `idempotency_key` ya existe, devuelve la tarea existente sin tocarla.
        """
        version = schema_version or latest_version(task_type)
        validate_payload(task_type, version, payload)

        now = self._clock.now()
        existing = self._conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            return _row_to_task(existing), False

        task_id = str(uuid.uuid4())
        when = iso_utc(available_at or now)
        try:
            self._conn.execute(
                """
                INSERT INTO tasks (id, recipient, platform, type, schema_version,
                    payload, status, priority, attempts, max_attempts,
                    idempotency_key, correlation_id, created_at, updated_at,
                    available_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, recipient, str(platform) if platform else None, task_type,
                 version, json.dumps(payload, ensure_ascii=False), priority,
                 max_attempts, idempotency_key, correlation_id,
                 iso_utc(now), iso_utc(now), when),
            )
        except sqlite3.IntegrityError:
            # Carrera: otro proceso inserto la misma clave entre el SELECT y el INSERT.
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row is None:
                raise
            return _row_to_task(row), False

        self.record_event(task_id, "enqueued", {"recipient": recipient, "type": task_type})
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row), True

    def claim(self, *, recipient: str, worker: str,
              lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Task | None:
        """Reclama atomicamente la tarea pendiente de mayor prioridad."""
        now = self._clock.now()
        lease_until = now + timedelta(seconds=lease_seconds)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                """
                UPDATE tasks
                   SET status = 'claimed',
                       claimed_by = ?,
                       claimed_at = ?,
                       lease_expires_at = ?,
                       attempts = attempts + 1,
                       updated_at = ?
                 WHERE id = (
                        SELECT id FROM tasks
                         WHERE recipient = ?
                           AND status = 'pending'
                           AND available_at <= ?
                         ORDER BY priority ASC, created_at ASC
                         LIMIT 1
                 )
                RETURNING *
                """,
                (worker, iso_utc(now), iso_utc(lease_until), iso_utc(now),
                 recipient, iso_utc(now)),
            ).fetchone()
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        if row is None:
            return None
        task = _row_to_task(row)
        self.record_event(task.id, "claimed",
                          {"worker": worker, "attempt": task.attempts})
        return task

    def renew_lease(self, task_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        now = self._clock.now()
        self._conn.execute(
            "UPDATE tasks SET lease_expires_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'claimed'",
            (iso_utc(now + timedelta(seconds=lease_seconds)), iso_utc(now), task_id),
        )

    def complete(self, result: TaskResult) -> None:
        """Cierra una tarea con exito y registra su resultado y artefactos."""
        now = self._clock.now()
        self._conn.execute(
            "UPDATE tasks SET status = 'done', finished_at = ?, updated_at = ?, "
            "lease_expires_at = NULL WHERE id = ?",
            (iso_utc(now), iso_utc(now), result.task_id),
        )
        self._conn.execute(
            "INSERT INTO task_results (task_id, at, status, summary, artifacts_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (result.task_id, iso_utc(now), "done", redaction.redact(result.summary),
             json.dumps([a.as_dict() for a in result.artifacts], ensure_ascii=False)),
        )
        self.record_event(result.task_id, "completed",
                          {"artifacts": [a.path for a in result.artifacts]})

    def fail(self, task_id: str, error: str, *, retry: bool = True) -> str:
        """Marca el intento como fallido. Devuelve el estado resultante.

        Si quedan intentos y `retry`, vuelve a `pending` con backoff.
        Si no, pasa a `dead`: no se reintenta mas.
        """
        safe = redaction.redact(error)[:2000]
        now = self._clock.now()
        row = self._conn.execute(
            "SELECT attempts, max_attempts FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Tarea desconocida: {task_id}")
        attempts, max_attempts = row["attempts"], row["max_attempts"]

        if retry and attempts < max_attempts:
            delay = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            delay = max(delay, 0)
            self._conn.execute(
                "UPDATE tasks SET status = 'pending', available_at = ?, last_error = ?, "
                "updated_at = ?, claimed_by = NULL, claimed_at = NULL, "
                "lease_expires_at = NULL WHERE id = ?",
                (iso_utc(now + timedelta(seconds=delay)), safe, iso_utc(now), task_id),
            )
            status = "pending"
        else:
            self._conn.execute(
                "UPDATE tasks SET status = 'dead', finished_at = ?, last_error = ?, "
                "updated_at = ?, lease_expires_at = NULL WHERE id = ?",
                (iso_utc(now), safe, iso_utc(now), task_id),
            )
            status = "dead"
        self._conn.execute(
            "INSERT INTO task_results (task_id, at, status, summary, artifacts_json) "
            "VALUES (?, ?, 'failed', ?, '[]')",
            (task_id, iso_utc(now), safe[:500]),
        )
        self.record_event(task_id, "failed",
                          {"outcome": status, "attempts": attempts,
                           "max_attempts": max_attempts, "error": safe[:300]})
        return status

    def recover_stale(self, *, lease_grace_seconds: int = 0) -> list[tuple[str, str]]:
        """Recupera tareas cuyo lease ha caducado (worker muerto o interrumpido).

        Devuelve [(task_id, nuevo_estado)].
        """
        now = self._clock.now()
        cutoff = iso_utc(now - timedelta(seconds=lease_grace_seconds))
        rows = self._conn.execute(
            "SELECT id, attempts, max_attempts FROM tasks "
            "WHERE status = 'claimed' AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at < ?",
            (cutoff,),
        ).fetchall()
        recovered: list[tuple[str, str]] = []
        for row in rows:
            status = self.fail(
                row["id"],
                "Lease caducado: el worker no termino la tarea (interrupcion o caida).",
                retry=row["attempts"] < row["max_attempts"],
            )
            self.record_event(row["id"], "lease_expired", {"outcome": status})
            recovered.append((row["id"], status))
        return recovered

    def cancel(self, task_id: str, reason: str) -> None:
        now = self._clock.now()
        self._conn.execute(
            "UPDATE tasks SET status = 'cancelled', finished_at = ?, updated_at = ?, "
            "last_error = ? WHERE id = ? AND status IN ('pending','claimed')",
            (iso_utc(now), iso_utc(now), redaction.redact(reason), task_id),
        )
        self.record_event(task_id, "cancelled", {"reason": redaction.redact(reason)})

    def record_event(self, task_id: str, kind: str, detail: dict[str, Any] | None = None) -> None:
        payload = redaction.redact_value(detail or {})
        self._conn.execute(
            "INSERT INTO task_events (task_id, at, kind, detail_json) VALUES (?, ?, ?, ?)",
            (task_id, iso_utc(self._clock.now()), kind,
             json.dumps(payload, ensure_ascii=False)),
        )

    # ------------------------------------------------------------------- read

    def get(self, task_id: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(
        self,
        *,
        recipient: str | None = None,
        statuses: Sequence[str] | None = None,
        platform: Platform | None = None,
        limit: int = 50,
    ) -> list[Task]:
        sql = ["SELECT * FROM tasks WHERE 1=1"]
        args: list[Any] = []
        if recipient:
            sql.append("AND recipient = ?")
            args.append(recipient)
        if statuses:
            sql.append("AND status IN (" + ",".join("?" * len(statuses)) + ")")
            args.extend(statuses)
        if platform:
            sql.append("AND platform = ?")
            args.append(str(platform))
        sql.append("ORDER BY priority ASC, created_at DESC LIMIT ?")
        args.append(limit)
        rows = self._conn.execute(" ".join(sql), args).fetchall()
        return [_row_to_task(row) for row in rows]

    def counts_by_recipient(self) -> dict[str, dict[str, int]]:
        rows = self._conn.execute(
            "SELECT recipient, status, COUNT(*) AS n FROM tasks GROUP BY recipient, status"
        ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            out.setdefault(row["recipient"], {})[row["status"]] = row["n"]
        return out

    def events(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT at, kind, detail_json FROM task_events WHERE task_id = ? "
            "ORDER BY at ASC, id ASC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [{"at": r["at"], "kind": r["kind"], "detail": json.loads(r["detail_json"])}
                for r in rows]

    def results(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT at, status, summary, artifacts_json FROM task_results "
            "WHERE task_id = ? ORDER BY at ASC, id ASC",
            (task_id,),
        ).fetchall()
        return [{"at": r["at"], "status": r["status"], "summary": r["summary"],
                 "artifacts": json.loads(r["artifacts_json"])} for r in rows]
