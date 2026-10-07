"""Puertos (interfaces) que los casos de uso necesitan.

Un adaptador por plataforma implementa `SocialDataGateway`. Anadir TikTok es
registrar otra implementacion: ningun caso de uso cambia.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Protocol, runtime_checkable

from gongora.domain.models import (
    AccountProfile,
    DataQualityIssue,
    MediaItem,
    MetricGap,
    MetricSnapshot,
)
from gongora.domain.platform import Platform


@dataclass(frozen=True)
class InsightsResult:
    """Metricas obtenidas para una publicacion, con sus huecos explicitos."""

    values: dict[str, tuple[float, str]] = field(default_factory=dict)
    """metrica -> (valor, periodo)"""
    gaps: dict[str, tuple[str, str]] = field(default_factory=dict)
    """metrica -> (motivo, detalle saneado)"""
    api_calls: int = 0


@dataclass(frozen=True)
class ConnectionStatus:
    """Estado de conectividad de un adaptador de plataforma."""

    platform: Platform
    connected: bool
    detail: str
    token_state: str = "unknown"
    #: Capacidades verificadas contra la API real en esta comprobacion.
    verified_capabilities: tuple[str, ...] = ()
    #: Capacidades declaradas por el adaptador pero sin verificar.
    pending_capabilities: tuple[str, ...] = ()


@runtime_checkable
class SocialDataGateway(Protocol):
    """Lectura de datos publicos de una cuenta en una plataforma."""

    platform: Platform

    def is_configured(self) -> bool:
        """True si hay credenciales suficientes para intentar la conexion."""
        ...

    def check_connection(self) -> ConnectionStatus:
        """Comprobacion ligera de configuracion y conectividad."""
        ...

    def fetch_profile(self) -> AccountProfile:
        ...

    def iter_media(self, *, max_items: int) -> Iterator[MediaItem]:
        """Recorrido paginado de publicaciones, de mas reciente a mas antigua."""
        ...

    def fetch_insights(self, media: MediaItem) -> InsightsResult:
        """Metricas de una publicacion, segun su tipo y las metricas admitidas."""
        ...


@runtime_checkable
class SnapshotRepository(Protocol):
    def save_profile(self, profile: AccountProfile) -> None: ...
    def save_media(self, media: MediaItem) -> None: ...
    def save_snapshots(self, snapshots: list[MetricSnapshot]) -> int: ...
    def save_gaps(self, gaps: list[MetricGap]) -> int: ...
    def save_issues(self, issues: list[DataQualityIssue]) -> int: ...
    def save_raw_response(
        self, *, run_id: str, platform: Platform, endpoint: str, payload: Any
    ) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Instante actual en UTC, con tzinfo."""
        ...


@runtime_checkable
class LlmPort(Protocol):
    """Puerto para generacion con un LLM.

    Faro NO lo usa: su recogida programada debe funcionar sin consumir modelo.
    Lo usara Luna cuando implementemos su worker.
    """

    name: str

    def is_available(self) -> bool: ...

    def complete(
        self, *, system: str, prompt: str, max_output_chars: int, timeout_seconds: float
    ) -> str:
        """Una sola peticion acotada. Sin conversacion abierta ni reintentos infinitos."""
        ...
