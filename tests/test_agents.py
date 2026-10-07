"""Riesgo: confundir un fichero de instrucciones con un agente operativo."""

from __future__ import annotations

from pathlib import Path

from gongora.agents import BY_NAME, REGISTRY, role_configured, worker_active
from gongora.application.worker import UnimplementedWorker
from gongora.domain.tasks import known_task_types

RAIZ = Path(__file__).resolve().parents[1]


def test_los_cuatro_roles_estan_registrados():
    assert [d.name for d in REGISTRY] == ["faro", "luna", "ritmo", "enlace"]


def test_los_cuatro_roles_tienen_instrucciones():
    for definition in REGISTRY:
        assert role_configured(definition, RAIZ), definition.instructions_file


def test_solo_faro_tiene_worker_implementado():
    implementados = [d.name for d in REGISTRY if d.worker_implemented]
    assert implementados == ["faro"]


def test_rol_configurado_no_implica_worker_activo():
    """Tener instrucciones no convierte a un rol en agente operativo."""
    luna = BY_NAME["luna"]
    assert role_configured(luna, RAIZ) is True
    assert luna.worker_implemented is False
    assert worker_active(luna, None) is False
    # Ni aun habiendo una fila de estado con ejecucion registrada.
    assert worker_active(luna, {"last_run_at": "2026-10-07T09:00:00+00:00"}) is False


def test_worker_de_faro_activo_solo_tras_ejecutarse():
    faro = BY_NAME["faro"]
    assert worker_active(faro, None) is False
    assert worker_active(faro, {"last_run_at": None}) is False
    assert worker_active(faro, {"last_run_at": "2026-10-07T09:00:00+00:00"}) is True


def test_worker_no_implementado_no_reclama_tareas(repos):
    """Luna no puede parecer que ha leido nada."""
    from gongora.domain.platform import Platform
    from gongora.domain.tasks import TASK_EDITORIAL_BRIEF
    payload = {"platform": "instagram", "run_id": "r", "report_path": "p",
               "window_start": "2026-10-07T00:00:00+00:00",
               "window_end": "2026-10-07T09:00:00+00:00"}
    tarea, _ = repos["queue"].enqueue(recipient="luna", task_type=TASK_EDITORIAL_BRIEF,
                                      payload=payload, idempotency_key="k",
                                      platform=Platform.INSTAGRAM)
    resultado = UnimplementedWorker("luna").run_once()
    assert resultado.claimed is False
    assert resultado.status == "worker_no_implementado"
    assert "sin reclamar" in resultado.detail
    # La tarea sigue intacta: sin intentos, sin reclamar.
    sigue = repos["queue"].get(tarea.id)
    assert sigue.status == "pending"
    assert sigue.attempts == 0
    assert sigue.claimed_by is None


def test_los_tipos_de_tarea_cuadran_con_el_registro():
    declarados = set()
    for definition in REGISTRY:
        declarados.update(definition.consumes)
        declarados.update(definition.produces)
    assert declarados == set(known_task_types())


def test_tarea_de_recogida_fallida_no_se_cierra_como_exito(repos, clock, tmp_path):
    """Si la recogida no obtiene datos, la tarea se marca fallida y se reintenta."""
    from gongora.application.worker import FaroWorker
    from gongora.config import Paths
    from gongora.domain.models import CollectionRun
    from gongora.domain.tasks import TASK_COLLECTION_REQUEST

    class ColectorQueFracasa:
        def execute(self, options):
            from gongora.application.collect import CollectionReport
            run = CollectionRun(run_id="run-x", started_at=clock.now(),
                                finished_at=clock.now(), status="failed")
            outcome = run.outcome(__import__(
                "gongora.domain.platform", fromlist=["Platform"]).Platform.INSTAGRAM)
            outcome.status = "failed"
            outcome.token_state = "rejected"
            outcome.errors.append({"stage": "perfil", "message": "Session has expired"})
            repos["runs"].start(run, trigger="test")
            repos["runs"].finish(run)
            return CollectionReport(run=run)

    class ConstructorDeInforme:
        def write(self, run_id, directory, **kwargs):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"faro-{run_id}.md"
            path.write_text("# informe de un fallo", encoding="utf-8")
            return path

    class DispatcherQueNoDebeUsarse:
        def execute(self, *args, **kwargs):
            raise AssertionError("No debe entregarse nada a Luna si no hay datos")

    paths = Paths(home=tmp_path, db=tmp_path / "d.sqlite3",
                  reports=tmp_path / "reports", raw=tmp_path / "raw",
                  logs=tmp_path / "logs", lock=tmp_path / "l.lock")
    tarea, _ = repos["queue"].enqueue(
        recipient="faro", task_type=TASK_COLLECTION_REQUEST,
        payload={"platforms": ["instagram"]}, idempotency_key="faro:test")

    worker = FaroWorker(
        queue=repos["queue"], collector=ColectorQueFracasa(),
        report_builder=ConstructorDeInforme(), dispatcher=DispatcherQueNoDebeUsarse(),
        snapshots=repos["snapshots"], runs=repos["runs"],
        agent_state=repos["agent_state"], paths=paths, clock=clock)
    resultado = worker.run_once()

    assert "recogida fallida" in resultado.status
    # El informe del fallo SI se conserva como artefacto.
    assert resultado.artifacts and resultado.artifacts[0].endswith(".md")
    # La tarea queda reintentable, no cerrada como exito.
    final = repos["queue"].get(tarea.id)
    assert final.status == "pending"
    assert final.attempts == 1
    assert "Session has expired" in final.last_error
