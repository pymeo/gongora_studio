"""Registro de los cuatro roles de Gongora Studio.

Distingue tres cosas que no son lo mismo:

- **rol configurado**: existe un fichero de instrucciones para ese papel.
  Un fichero de instrucciones NO es un agente operativo.
- **worker implementado**: existe codigo que ejecuta ese papel.
- **worker activo**: ese worker se ha ejecutado de verdad y hay constancia.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gongora.domain.platform import ALL_PLATFORMS, Platform


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    mission: str
    capabilities: tuple[str, ...]
    #: Capacidades declaradas que todavia NO estan implementadas.
    not_implemented: tuple[str, ...]
    platforms: tuple[Platform, ...]
    worker_implemented: bool
    instructions_file: str
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()


REGISTRY: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        name="faro",
        mission="Recogida de datos, calidad de datos y analisis.",
        capabilities=(
            "comprobar configuracion y conectividad",
            "leer perfil por plataforma",
            "recorrer publicaciones con paginacion",
            "leer insights/contadores segun tipo de publicacion",
            "guardar snapshots historicos",
            "detectar huecos y discrepancias",
            "generar informe en Markdown",
            "encolar informe para Luna",
        ),
        not_implemented=(),
        platforms=ALL_PLATFORMS,
        worker_implemented=True,
        instructions_file="agents/faro.md",
        consumes=("data.collection_request",),
        produces=("editorial.brief_request",),
    ),
    AgentDefinition(
        name="luna",
        mission="Estrategia editorial, calendario y textos, adaptados a cada red.",
        capabilities=(),
        not_implemented=(
            "leer el informe de Faro",
            "proponer calendario editorial",
            "redactar textos por plataforma",
            "pedir clips a Ritmo",
        ),
        platforms=ALL_PLATFORMS,
        worker_implemented=False,
        instructions_file="agents/luna.md",
        consumes=("editorial.brief_request",),
        produces=("production.clip_request", "distribution.publish_proposal"),
    ),
    AgentDefinition(
        name="ritmo",
        mission="Produccion de clips y material audiovisual, por destino.",
        capabilities=(),
        not_implemented=(
            "generar variantes de video por plataforma",
            "ajustar formato y duracion por destino",
        ),
        platforms=ALL_PLATFORMS,
        worker_implemented=False,
        instructions_file="agents/ritmo.md",
        consumes=("production.clip_request",),
        produces=(),
    ),
    AgentDefinition(
        name="enlace",
        mission="Publicacion y comunidad, con capacidades separadas por plataforma.",
        capabilities=(),
        not_implemented=(
            "publicar contenido (requiere autorizacion por campana)",
            "responder comentarios",
            "leer conversaciones",
        ),
        platforms=ALL_PLATFORMS,
        worker_implemented=False,
        instructions_file="agents/enlace.md",
        consumes=("distribution.publish_proposal",),
        produces=(),
    ),
)

BY_NAME = {definition.name: definition for definition in REGISTRY}


def role_configured(definition: AgentDefinition, project_root: Path) -> bool:
    """True si existe el fichero de instrucciones del rol."""
    return (project_root / definition.instructions_file).is_file()


def worker_active(definition: AgentDefinition, state_row) -> bool:
    """True solo si el worker esta implementado Y consta una ejecucion real."""
    if not definition.worker_implemented:
        return False
    return state_row is not None and bool(state_row["last_run_at"])
