"""Entrega del trabajo de Faro a Luna.

Faro no interpreta el informe ni habla con Luna: deja una tarea en la cola con
la referencia al informe. Luna la recogera cuando su worker exista. Hasta
entonces la tarea queda `pending`, visible en `gongora tasks list`.

Nada de lo que se encola contiene credenciales.
"""

from __future__ import annotations

from pathlib import Path

from gongora.clock import iso_utc
from gongora.domain.models import CollectionRun
from gongora.domain.platform import Platform
from gongora.domain.tasks import PRIORITY_NORMAL, TASK_EDITORIAL_BRIEF
from gongora.logging_setup import get_logger


class EnqueueEditorialBriefs:
    """Crea una tarea para Luna por cada plataforma que aporto datos."""

    def __init__(self, *, queue, snapshots, clock) -> None:
        self._queue = queue
        self._snapshots = snapshots
        self._clock = clock
        self._log = get_logger("faro.dispatch")

    def execute(self, run: CollectionRun, report_path: Path,
                report_sha256: str | None = None) -> list[tuple[str, bool, Platform]]:
        """Devuelve [(task_id, creada, plataforma)]."""
        created: list[tuple[str, bool, Platform]] = []
        for platform, outcome in run.outcomes.items():
            if outcome.status not in ("ok", "partial") or outcome.snapshots_written == 0:
                self._log.info(
                    "Sin tarea para Luna en %s: estado=%s snapshots=%s",
                    platform.label, outcome.status, outcome.snapshots_written,
                )
                continue

            snaps = self._snapshots.run_snapshots(run.run_id, platform)
            media_ids = sorted({s["media_id"] for s in snaps if s["media_id"]})
            account_ids = sorted({s["account_id"] for s in snaps})
            headline = {
                s["metric"]: s["value"] for s in snaps if s["media_id"] is None
            }
            issues = self._snapshots.run_issues(run.run_id, platform)
            gaps = self._snapshots.run_gaps(run.run_id, platform)
            notes = [f"{row['kind']}: {row['detail']}" for row in issues]
            notes += [f"hueco en {row['metric']} ({row['media_id'] or 'cuenta'}): "
                      f"{row['reason']}" for row in gaps]

            payload = {
                "platform": str(platform),
                "run_id": run.run_id,
                "report_path": str(report_path),
                "window_start": iso_utc(run.started_at),
                "window_end": iso_utc(run.finished_at or self._clock.now()),
                "account_id": account_ids[0] if account_ids else None,
                "media_ids": media_ids,
                "headline_metrics": headline,
                "data_quality_notes": notes[:20],
                "report_sha256": report_sha256,
                "notes": (
                    "Metricas con el nombre original de la plataforma. No compares ni "
                    "sumes con las de otra red. Los valores acumulados no se suman "
                    "entre snapshots."
                ),
            }
            task, was_created = self._queue.enqueue(
                recipient="luna",
                task_type=TASK_EDITORIAL_BRIEF,
                payload=payload,
                idempotency_key=f"luna:brief:{run.run_id}:{platform}",
                platform=platform,
                priority=PRIORITY_NORMAL,
                correlation_id=run.run_id,
            )
            created.append((task.id, was_created, platform))
            self._log.info("Tarea para Luna (%s): %s (creada=%s)",
                           platform.label, task.id, was_created)
        return created
