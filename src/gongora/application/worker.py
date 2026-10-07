"""Workers: ejecutan una tarea concreta y terminan.

Cada ejecucion tiene tarea concreta, limites y criterio de finalizacion. No
hay bucles de conversacion entre agentes: un worker reclama una tarea, la
resuelve o la marca fallida, y sale.

Solo Faro tiene worker en esta fase. Los demas roles devuelven un resultado
explicito de "worker no implementado" y **no reclaman** sus tareas, para que
nadie pueda confundir una tarea pendiente con una tarea atendida.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gongora.agents import BY_NAME
from gongora.application.collect import CollectionOptions
from gongora.domain.platform import Platform
from gongora.domain.tasks import (
    ArtifactRef,
    TASK_COLLECTION_REQUEST,
    TaskResult,
)
from gongora.logging_setup import get_logger


class CollectionFailed(RuntimeError):
    """La tarea se ejecuto, pero la recogida no obtuvo datos de ninguna parte.

    Se separa de un error de programacion: el trabajo se hizo, el resultado fue
    un fallo externo (p. ej. token rechazado). La tarea se marca fallida para
    que se reintente cuando la causa se corrija, con el tope de intentos como
    proteccion frente a bucles.
    """

    def __init__(self, message: str, artifacts: list[ArtifactRef]) -> None:
        super().__init__(message)
        self.artifacts = artifacts


@dataclass
class WorkerOutcome:
    agent: str
    claimed: bool = False
    task_id: str | None = None
    task_type: str | None = None
    status: str = "sin_tareas"
    detail: str = ""
    recovered: list[tuple[str, str]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)


class FaroWorker:
    """Worker de Faro. Atiende peticiones de recogida."""

    agent = "faro"

    def __init__(self, *, queue, collector, report_builder, dispatcher, snapshots,
                 runs, agent_state, paths, clock, lease_seconds: int = 900) -> None:
        self._queue = queue
        self._collector = collector
        self._report_builder = report_builder
        self._dispatcher = dispatcher
        self._snapshots = snapshots
        self._runs = runs
        self._agent_state = agent_state
        self._paths = paths
        self._clock = clock
        self._lease = lease_seconds
        self._log = get_logger("faro.worker")

    def run_once(self, *, worker_id: str = "faro-cli") -> WorkerOutcome:
        outcome = WorkerOutcome(agent=self.agent)
        # 1. Recuperar tareas interrumpidas antes de reclamar nuevas.
        outcome.recovered = self._queue.recover_stale()
        if outcome.recovered:
            self._log.info("Tareas recuperadas: %s", outcome.recovered)

        task = self._queue.claim(recipient=self.agent, worker=worker_id,
                                 lease_seconds=self._lease)
        if task is None:
            outcome.status = "sin_tareas"
            outcome.detail = "No hay tareas pendientes para Faro."
            return outcome

        outcome.claimed = True
        outcome.task_id = task.id
        outcome.task_type = task.type
        try:
            if task.type == TASK_COLLECTION_REQUEST:
                detail, artifacts = self._handle_collection_request(task)
            else:
                raise ValueError(f"Faro no sabe atender tareas de tipo {task.type!r}.")
            self._queue.complete(TaskResult(task_id=task.id, status="done",
                                            summary=detail, artifacts=artifacts))
            outcome.status = "completada"
            outcome.detail = detail
            outcome.artifacts = [a.path for a in artifacts]
            self._agent_state.record(agent=self.agent, platform="*", status="ok")
        except CollectionFailed as exc:
            new_status = self._queue.fail(task.id, str(exc))
            outcome.status = f"recogida fallida -> tarea {new_status}"
            outcome.detail = str(exc)[:500]
            outcome.artifacts = [a.path for a in exc.artifacts]
            self._agent_state.record(agent=self.agent, platform="*", status="failed",
                                     error=str(exc))
            self._log.warning("Tarea %s: la recogida fracaso (%s)", task.id, new_status)
        except Exception as exc:  # noqa: BLE001 - se sanea y se registra
            new_status = self._queue.fail(task.id, f"{type(exc).__name__}: {exc}")
            outcome.status = f"fallida -> {new_status}"
            outcome.detail = str(exc)[:500]
            self._agent_state.record(agent=self.agent, platform="*", status="failed",
                                     error=f"{type(exc).__name__}: {exc}")
            self._log.exception("Tarea %s fallida", task.id)
        return outcome

    def _handle_collection_request(self, task) -> tuple[str, list[ArtifactRef]]:
        payload: dict[str, Any] = task.payload
        raw_platforms = payload.get("platforms") or []
        platforms = tuple(Platform.parse(p) for p in raw_platforms) or None
        options = CollectionOptions(
            platforms=platforms,
            max_media=int(payload.get("max_media") or 200),
            force=bool(payload.get("force")),
            trigger=f"task:{task.id[:8]}",
        )
        report = self._collector.execute(options)
        if report.skipped_reason:
            return f"Recogida omitida: {report.skipped_reason}", []

        run = report.run
        path = self._report_builder.write(run.run_id, self._paths.reports)
        digest = self._snapshots.record_artifact(run_id=run.run_id, kind="report",
                                                 path=path)
        artifacts = [ArtifactRef(kind="report", path=str(path), sha256=digest,
                                 note="Informe Faro en Markdown")]
        if run.status == "failed":
            motivos = "; ".join(
                str(error.get("message", ""))[:200] for error in run.errors) or "sin detalle"
            raise CollectionFailed(
                f"Recogida {run.run_id} sin datos de ninguna plataforma: {motivos}",
                artifacts)

        briefs = self._dispatcher.execute(run, path, digest)
        summary = (
            f"Recogida {run.run_id}: estado {run.status}, "
            f"{run.snapshots_written} snapshots, {run.media_seen} publicaciones. "
            f"Tareas para Luna: {len(briefs)}."
        )
        return summary, artifacts


class UnimplementedWorker:
    """Marcador para los roles sin worker. No reclama tareas a proposito."""

    def __init__(self, agent: str) -> None:
        self.agent = agent

    def run_once(self, *, worker_id: str = "") -> WorkerOutcome:
        definition = BY_NAME.get(self.agent)
        pending = ", ".join(definition.not_implemented) if definition else ""
        return WorkerOutcome(
            agent=self.agent,
            claimed=False,
            status="worker_no_implementado",
            detail=(
                f"El rol '{self.agent}' tiene instrucciones configuradas, pero su worker "
                f"no esta implementado todavia. Sus tareas siguen pendientes en la cola "
                f"sin reclamar: nadie las ha leido ni respondido."
                + (f" Pendiente de implementar: {pending}." if pending else "")
            ),
        )


def build_worker(agent: str, **dependencies) -> FaroWorker | UnimplementedWorker:
    if agent == "faro":
        return FaroWorker(**dependencies)
    return UnimplementedWorker(agent)
