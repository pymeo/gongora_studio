"""Contratos de las tareas que los agentes se pasan entre si.

Cada tipo de tarea declara su version de esquema y los campos obligatorios.
La validacion es deliberadamente simple (sin dependencias) y cumple tres
invariantes:

1. Ningun payload puede contener un secreto (barrera de redaccion).
2. Ningun payload puede contener texto que se vaya a ejecutar como
   instruccion: los textos externos se marcan como datos.
3. Un payload con version de esquema desconocida se rechaza, no se adivina.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from gongora import redaction
from gongora.domain.platform import Platform

TaskStatus = Literal["pending", "claimed", "done", "failed", "dead", "cancelled"]

#: Prioridades: 1 = maxima. Se ordena ascendente al reclamar.
PRIORITY_HIGH = 1
PRIORITY_NORMAL = 5
PRIORITY_LOW = 9

# Tipos de tarea conocidos ------------------------------------------------

#: Operador/agente -> Faro: ejecuta una recogida.
TASK_COLLECTION_REQUEST = "data.collection_request"
#: Faro -> Luna: hay una recogida nueva y un informe que leer.
TASK_EDITORIAL_BRIEF = "editorial.brief_request"
#: Luna -> Ritmo: hace falta material audiovisual para una pieza.
TASK_CLIP_REQUEST = "production.clip_request"
#: Luna -> Enlace: hay contenido aprobado listo para programar (NO publica).
TASK_PUBLISH_PROPOSAL = "distribution.publish_proposal"


@dataclass(frozen=True)
class TaskSchema:
    type: str
    version: int
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    description: str = ""

    def validate(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise PayloadInvalid(f"El payload de {self.type} debe ser un objeto JSON.")
        missing = [key for key in self.required if key not in payload]
        if missing:
            raise PayloadInvalid(
                f"Faltan campos obligatorios en {self.type} v{self.version}: "
                + ", ".join(missing)
            )
        allowed = set(self.required) | set(self.optional)
        extra = sorted(set(payload) - allowed)
        if extra:
            raise PayloadInvalid(
                f"Campos no declarados en {self.type} v{self.version}: " + ", ".join(extra)
            )
        if redaction.contains_secret(payload):
            raise PayloadInvalid(
                f"El payload de {self.type} contiene algo con forma de credencial. "
                "Las tareas nunca transportan tokens."
            )


class PayloadInvalid(ValueError):
    """Payload que no cumple su contrato."""


_SCHEMAS: dict[tuple[str, int], TaskSchema] = {}


def _register(schema: TaskSchema) -> TaskSchema:
    _SCHEMAS[(schema.type, schema.version)] = schema
    return schema


COLLECTION_REQUEST_V1 = _register(TaskSchema(
    type=TASK_COLLECTION_REQUEST,
    version=1,
    required=("platforms",),
    optional=("max_media", "force", "reason", "requested_by"),
    description="Peticion de recogida para Faro. `platforms` es una lista de "
                "plataformas; vacia significa todas las configuradas.",
))

EDITORIAL_BRIEF_V1 = _register(TaskSchema(
    type=TASK_EDITORIAL_BRIEF,
    version=1,
    required=("platform", "run_id", "report_path", "window_start", "window_end"),
    optional=("account_id", "media_ids", "headline_metrics", "data_quality_notes",
              "report_sha256", "notes"),
    description="Faro entrega una recogida y su informe para que Luna proponga "
                "contenido adaptado a esa plataforma.",
))

CLIP_REQUEST_V1 = _register(TaskSchema(
    type=TASK_CLIP_REQUEST,
    version=1,
    required=("platform", "source_media_id", "target_format", "brief"),
    optional=("duration_seconds", "aspect_ratio", "notes", "correlation_run_id"),
    description="Luna pide a Ritmo una variante del video para un destino concreto.",
))

PUBLISH_PROPOSAL_V1 = _register(TaskSchema(
    type=TASK_PUBLISH_PROPOSAL,
    version=1,
    required=("platform", "caption_draft", "asset_ref", "campaign_id"),
    optional=("scheduled_for", "notes", "approval_ref", "correlation_run_id"),
    description="Propuesta de publicacion para Enlace. Requiere autorizacion "
                "explicita de campana: por si sola no publica nada.",
))


def schema_for(task_type: str, version: int) -> TaskSchema:
    try:
        return _SCHEMAS[(task_type, version)]
    except KeyError:
        known = ", ".join(f"{t} v{v}" for t, v in sorted(_SCHEMAS))
        raise PayloadInvalid(
            f"Tipo/version de tarea desconocido: {task_type} v{version}. Conocidos: {known}"
        ) from None


def latest_version(task_type: str) -> int:
    versions = [v for t, v in _SCHEMAS if t == task_type]
    if not versions:
        raise PayloadInvalid(f"Tipo de tarea desconocido: {task_type}")
    return max(versions)


def known_task_types() -> tuple[str, ...]:
    return tuple(sorted({t for t, _ in _SCHEMAS}))


def validate_payload(task_type: str, version: int, payload: dict[str, Any]) -> None:
    schema_for(task_type, version).validate(payload)
    platform = payload.get("platform")
    if platform is not None:
        Platform.parse(str(platform))  # lanza ValueError si no es valida


@dataclass
class Task:
    """Tarea tal como vive en la cola."""

    id: str
    recipient: str
    type: str
    schema_version: int
    payload: dict[str, Any]
    status: TaskStatus
    priority: int
    attempts: int
    max_attempts: int
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    platform: Platform | None = None
    correlation_id: str | None = None
    claimed_at: datetime | None = None
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None

    @property
    def attempts_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "dead", "cancelled")


@dataclass(frozen=True)
class ArtifactRef:
    """Referencia a un artefacto producido por una tarea (informe, clip...)."""

    kind: str
    path: str
    sha256: str | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": self.path, "sha256": self.sha256,
                "note": self.note}


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: Literal["done", "failed"]
    summary: str = ""
    artifacts: list[ArtifactRef] = field(default_factory=list)
