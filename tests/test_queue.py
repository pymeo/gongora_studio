"""Riesgo: idempotencia, reclamacion concurrente y bucles de reintento."""

from __future__ import annotations

import threading

import pytest

from gongora.adapters.persistence.db import open_database
from gongora.adapters.persistence.queue import SqliteTaskQueue
from gongora.clock import SystemClock
from gongora.domain.platform import Platform
from gongora.domain.tasks import (
    ArtifactRef,
    PayloadInvalid,
    TASK_EDITORIAL_BRIEF,
    TaskResult,
)
from tests.conftest import FAKE_TOKEN

PAYLOAD = {
    "platform": "instagram", "run_id": "run-1", "report_path": "var/reports/a.md",
    "window_start": "2026-10-07T00:00:00+00:00",
    "window_end": "2026-10-07T09:00:00+00:00",
}


def _encola(queue, key="luna:brief:run-1"):
    return queue.enqueue(recipient="luna", task_type=TASK_EDITORIAL_BRIEF,
                         payload=PAYLOAD, idempotency_key=key,
                         platform=Platform.INSTAGRAM, correlation_id="run-1")


def test_idempotencia_no_duplica(repos):
    queue = repos["queue"]
    primera, creada1 = _encola(queue)
    segunda, creada2 = _encola(queue)
    assert creada1 is True and creada2 is False
    assert primera.id == segunda.id
    assert len(queue.list_tasks()) == 1


def test_payload_con_token_en_campo_declarado_se_rechaza(repos):
    """La barrera de secretos actua aunque el campo este permitido."""
    queue = repos["queue"]
    envenenado = {**PAYLOAD, "notes": f"usa este token {FAKE_TOKEN}"}
    with pytest.raises(PayloadInvalid) as exc:
        queue.enqueue(recipient="luna", task_type=TASK_EDITORIAL_BRIEF,
                      payload=envenenado, idempotency_key="luna:veneno")
    assert "credencial" in str(exc.value)
    assert len(queue.list_tasks()) == 0


def test_version_de_esquema_desconocida_se_rechaza(repos):
    with pytest.raises(PayloadInvalid):
        repos["queue"].enqueue(recipient="luna", task_type=TASK_EDITORIAL_BRIEF,
                               payload=PAYLOAD, idempotency_key="k",
                               schema_version=99)


def test_reclamacion_concurrente_solo_un_ganador(paths, repos):
    _encola(repos["queue"])
    ganadores: list[str] = []
    cerrojo = threading.Lock()

    def worker(n: int) -> None:
        conn = open_database(paths.db)
        try:
            tarea = SqliteTaskQueue(conn, SystemClock()).claim(
                recipient="luna", worker=f"w{n}")
            if tarea is not None:
                with cerrojo:
                    ganadores.append(tarea.id)
        finally:
            conn.close()

    hilos = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    assert len(ganadores) == 1


def test_recupera_tarea_con_lease_caducado(repos, clock):
    queue = repos["queue"]
    tarea, _ = _encola(queue)
    reclamada = queue.claim(recipient="luna", worker="w1", lease_seconds=600)
    assert reclamada is not None
    assert queue.claim(recipient="luna", worker="w2") is None  # ya reclamada

    clock.advance(601)
    recuperadas = queue.recover_stale()
    assert recuperadas == [(tarea.id, "pending")]
    assert queue.get(tarea.id).status == "pending"
    # Vuelve a estar disponible tras el backoff.
    clock.advance(3600)
    assert queue.claim(recipient="luna", worker="w3") is not None


def test_tope_de_reintentos_evita_bucles(repos, clock):
    queue = repos["queue"]
    tarea, _ = _encola(queue)
    estados = []
    for _ in range(5):
        clock.advance(2000)
        reclamada = queue.claim(recipient="luna", worker="w")
        if reclamada is None:
            estados.append(None)
            continue
        estados.append(queue.fail(reclamada.id, "fallo simulado"))
    final = queue.get(tarea.id)
    assert final.status == "dead"
    assert final.attempts == final.max_attempts
    assert estados.count(None) >= 1  # deja de ofrecerse


def test_error_guardado_sin_token(repos, clock):
    queue = repos["queue"]
    tarea, _ = _encola(queue)
    reclamada = queue.claim(recipient="luna", worker="w")
    queue.fail(reclamada.id, f"peticion rechazada con {FAKE_TOKEN}")
    guardado = queue.get(tarea.id)
    assert FAKE_TOKEN not in (guardado.last_error or "")
    assert "[REDACTED]" in guardado.last_error


def test_completar_registra_artefactos_y_eventos(repos):
    queue = repos["queue"]
    _encola(queue)
    reclamada = queue.claim(recipient="luna", worker="w")
    queue.complete(TaskResult(task_id=reclamada.id, status="done", summary="hecho",
                              artifacts=[ArtifactRef("report", "var/reports/a.md", "abc")]))
    assert queue.get(reclamada.id).status == "done"
    resultados = queue.results(reclamada.id)
    assert resultados[0]["artifacts"][0]["path"] == "var/reports/a.md"
    assert [e["kind"] for e in queue.events(reclamada.id)] == \
        ["enqueued", "claimed", "completed"]


def test_filtra_por_destinatario_y_plataforma(repos):
    queue = repos["queue"]
    _encola(queue)
    assert len(queue.list_tasks(recipient="luna", statuses=["pending"])) == 1
    assert len(queue.list_tasks(recipient="faro")) == 0
    assert len(queue.list_tasks(platform=Platform.INSTAGRAM)) == 1
    assert len(queue.list_tasks(platform=Platform.TIKTOK)) == 0
