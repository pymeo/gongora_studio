"""Entidades y valores del dominio de datos de GONGORA.

Toda cuenta, publicacion, snapshot, hueco e incidencia lleva su `platform`.
No hay identificadores compartidos entre redes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from gongora.domain.platform import Platform
from gongora.domain.untrusted import UntrustedText

RunStatus = Literal["running", "ok", "partial", "failed"]
#: Las plataformas pueden ademas quedar sin consultar (sin credenciales).
OutcomeStatus = Literal["running", "ok", "partial", "failed", "skipped"]
Severity = Literal["info", "warning", "error"]
TokenState = Literal["unknown", "valid", "rejected", "absent"]


@dataclass(frozen=True)
class AccountProfile:
    platform: Platform
    account_id: str
    username: str | None
    name: str | None
    counters: dict[str, int]
    fetched_at: datetime

    @property
    def followers(self) -> int | None:
        """Seguidores con el nombre de campo propio de cada red."""
        key = "followers_count" if self.platform is Platform.INSTAGRAM else "follower_count"
        return self.counters.get(key)


@dataclass(frozen=True)
class MediaItem:
    """Publicacion. `product_type` conserva el vocabulario de cada API.

    Instagram: media_product_type (REELS, FEED, STORY) y media_type.
    TikTok: tipo unico de video; se marca como "VIDEO".
    """

    platform: Platform
    media_id: str
    media_type: str | None
    product_type: str | None
    permalink: str | None
    published_at: datetime | None
    like_count: int | None = None
    comments_count: int | None = None
    caption: UntrustedText = field(
        default_factory=lambda: UntrustedText("", "instagram.caption")
    )

    @property
    def ref(self) -> str:
        return f"{self.platform}:{self.media_id}"


@dataclass(frozen=True)
class MetricSnapshot:
    """Valor de una metrica en un instante, con su plataforma de origen."""

    captured_at: datetime
    platform: Platform
    account_id: str
    media_id: str | None
    metric: str
    period: str
    value: float
    run_id: str

    @property
    def is_account_level(self) -> bool:
        return self.media_id is None


@dataclass(frozen=True)
class MetricGap:
    """Metrica solicitada que la API no devolvio. NO equivale a valor 0."""

    platform: Platform
    account_id: str
    media_id: str | None
    metric: str
    reason: str
    detail: str
    run_id: str


@dataclass(frozen=True)
class DataQualityIssue:
    """Incoherencia observada. Se registra el hecho, no una causa inventada."""

    platform: Platform
    kind: str
    severity: Severity
    subject: str
    detail: str
    observed: dict[str, Any]
    run_id: str


@dataclass
class PlatformOutcome:
    """Resultado de la recogida para una plataforma concreta."""

    platform: Platform
    status: OutcomeStatus = "running"
    connected: bool = False
    media_seen: int = 0
    snapshots_written: int = 0
    gaps: int = 0
    issues: int = 0
    pages_fetched: int = 0
    api_calls: int = 0
    token_state: TokenState = "unknown"
    errors: list[dict[str, Any]] = field(default_factory=list)
    #: Motivo por el que la plataforma no se consulto (p. ej. sin credenciales).
    skipped_reason: str | None = None


@dataclass
class CollectionRun:
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: RunStatus = "running"
    outcomes: dict[Platform, PlatformOutcome] = field(default_factory=dict)

    def outcome(self, platform: Platform) -> PlatformOutcome:
        return self.outcomes.setdefault(platform, PlatformOutcome(platform=platform))

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def snapshots_written(self) -> int:
        return sum(o.snapshots_written for o in self.outcomes.values())

    @property
    def media_seen(self) -> int:
        return sum(o.media_seen for o in self.outcomes.values())

    @property
    def errors(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for outcome in self.outcomes.values():
            for error in outcome.errors:
                out.append({"platform": str(outcome.platform), **error})
        return out
