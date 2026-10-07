"""Fachada de TikTok para agentes y comandos.

Quien la usa pide "mi perfil", "mis estadisticas" o "mis ultimos videos" y no
sabe nada de OAuth, tokens ni refrescos: eso lo resuelve `TikTokSession` por
debajo.

Reglas del proyecto que aplica:
- nombres de campo originales de la API (`follower_count`, `view_count`...);
- un campo que no llega es un hueco, no un cero: no aparece en `stats`;
- descripciones, titulos y bio son `UntrustedText`;
- nada de datos simulados: sin credenciales, `ConnectorNotAuthorized`.

Relacion con `TikTokDisplayGateway`: el gateway es lo que usa Faro para la
recogida historica (snapshots); este servicio es para consultas puntuales y
para los roles que vengan. Comparten cliente HTTP y sesion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gongora.adapters.tiktok.http_client import TikTokHttpClient
from gongora.adapters.tiktok.session import TikTokSession
from gongora.config import (
    TikTokAppConfig,
    TikTokCredentials,
    load_tiktok_app_config,
    load_tiktok_credentials,
    resolve_tiktok_secrets_file,
)
from gongora.domain.errors import ConnectorNotAuthorized, GongoraError
from gongora.domain.untrusted import UntrustedText

#: Campos de /v2/user/info/ segun el scope que los habilita.
PROFILE_FIELDS_BY_SCOPE: dict[str, tuple[str, ...]] = {
    "user.info.basic": ("open_id", "union_id", "avatar_url", "display_name"),
    "user.info.profile": ("username", "bio_description", "profile_deep_link", "is_verified"),
    "user.info.stats": ("follower_count", "following_count", "likes_count", "video_count"),
}
STAT_FIELDS = PROFILE_FIELDS_BY_SCOPE["user.info.stats"]
VIDEO_FIELDS = ("id", "create_time", "title", "video_description", "duration",
                "share_url", "cover_image_url",
                "view_count", "like_count", "comment_count", "share_count")
VIDEO_COUNTERS = ("view_count", "like_count", "comment_count", "share_count")
#: Maximo que admite /v2/video/list/ por pagina.
VIDEO_PAGE_MAX = 20

NEXT_STEP = ("poner TIKTOK_CLIENT_KEY y TIKTOK_CLIENT_SECRET y ejecutar "
             "`gongora tiktok login` (ver docs/tiktok-conexion.md)")


class CapabilityDisabled(GongoraError):
    """Capacidad prevista pero desactivada por las reglas de esta fase."""


@dataclass(frozen=True)
class TikTokProfile:
    open_id: str | None
    display_name: str | None
    username: str | None
    bio: UntrustedText | None
    avatar_url: str | None
    profile_deep_link: str | None
    is_verified: bool | None
    #: Solo los contadores que la API devolvio, con su nombre original.
    stats: dict[str, int]
    fetched_at: datetime
    requested_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class TikTokVideo:
    id: str
    created_at: datetime | None
    title: UntrustedText
    description: UntrustedText
    duration_seconds: int | None
    share_url: str | None
    cover_image_url: str | None = field(repr=False)
    #: Contadores devueltos por la API, nombre original. Ausente no es cero.
    counters: dict[str, int] = field(default_factory=dict)


class TikTokService:
    def __init__(self, *, app: TikTokAppConfig, session: TikTokSession | None,
                 client: TikTokHttpClient | None) -> None:
        self.app = app
        self._session = session
        self._client = client

    @classmethod
    def from_environment(cls, *, secrets_file: Path | None = None, timeout: float = 20.0,
                         max_attempts: int = 4) -> "TikTokService":
        path = secrets_file or resolve_tiktok_secrets_file()
        app = load_tiktok_app_config(path)
        credentials = load_tiktok_credentials(path)
        if credentials is None:
            return cls(app=app, session=None, client=None)
        session = TikTokSession.from_credentials(credentials, path, timeout=timeout)
        client = TikTokHttpClient(session.access_token, timeout=timeout,
                                  max_attempts=max_attempts,
                                  on_token_rejected=session.on_token_rejected)
        return cls(app=app, session=session, client=client)

    # ------------------------------------------------------------- estado

    def is_connected(self) -> bool:
        """Hay token guardado. No garantiza que TikTok lo acepte: eso lo dice profile()."""
        return self._session is not None

    @property
    def credentials(self) -> TikTokCredentials | None:
        return self._session.credentials if self._session else None

    @property
    def session(self) -> TikTokSession | None:
        """Solo para comandos del operador (p. ej. refresco manual). Los agentes no lo usan."""
        return self._session

    @property
    def api_calls(self) -> int:
        return self._client.call_count if self._client else 0

    def _require(self) -> TikTokHttpClient:
        if self._client is None:
            raise ConnectorNotAuthorized("TikTok", NEXT_STEP)
        return self._client

    def _granted(self) -> tuple[str, ...]:
        return self.credentials.scopes if self.credentials else ()

    # ------------------------------------------------------------ lectura

    def profile(self) -> TikTokProfile:
        client = self._require()
        granted = self._granted()
        fields: list[str] = []
        for scope, scope_fields in PROFILE_FIELDS_BY_SCOPE.items():
            # Sin lista de scopes conocida se piden todos; la API dira cual falta.
            if not granted or scope in granted:
                fields.extend(scope_fields)
        response = client.get("user/info", {"fields": ",".join(fields)})
        user: dict[str, Any] = response.data.get("user") or {}
        bio = user.get("bio_description")
        return TikTokProfile(
            open_id=user.get("open_id") or (self.credentials.open_id if self.credentials
                                            else None),
            display_name=user.get("display_name"),
            username=user.get("username"),
            bio=(UntrustedText(raw=bio, source="tiktok.bio_description")
                 if isinstance(bio, str) else None),
            avatar_url=user.get("avatar_url"),
            profile_deep_link=user.get("profile_deep_link"),
            is_verified=user.get("is_verified") if isinstance(user.get("is_verified"), bool)
                        else None,
            stats={name: int(user[name]) for name in STAT_FIELDS
                   if isinstance(user.get(name), (int, float))},
            fetched_at=datetime.now(UTC),
            requested_fields=tuple(fields),
        )

    def stats(self) -> dict[str, int]:
        """Contadores de la cuenta disponibles (scope user.info.stats)."""
        return self.profile().stats

    def recent_videos(self, limit: int = 10) -> list[TikTokVideo]:
        """Ultimos videos publicados, del mas reciente al mas antiguo."""
        client = self._require()
        videos: list[TikTokVideo] = []
        cursor: int | None = None
        while len(videos) < limit:
            body: dict[str, Any] = {"max_count": min(VIDEO_PAGE_MAX, limit - len(videos))}
            if cursor is not None:
                body["cursor"] = cursor
            response = client.post("video/list", params={"fields": ",".join(VIDEO_FIELDS)},
                                   body=body)
            page = response.data.get("videos") or []
            videos.extend(_video(item) for item in page[: limit - len(videos)])
            cursor = response.data.get("cursor")
            if not page or not response.data.get("has_more") or cursor is None:
                break
        return videos

    # ----------------------------------------------------------- escritura

    def upload_video(self, *_args: Any, **_kwargs: Any) -> None:
        """Prevista (scope video.upload, Content Posting API). Desactivada.

        Esta fase solo lee: no se envia contenido a ninguna plataforma. Cuando
        se autorice, la implementacion ira aqui y la usara Enlace, con
        autorizacion por campana.
        """
        raise CapabilityDisabled(
            "Subir contenido a TikTok esta desactivado en esta fase (solo lectura). "
            "Requiere autorizacion explicita por campana.")


def _video(item: dict[str, Any]) -> TikTokVideo:
    created = item.get("create_time")
    duration = item.get("duration")
    return TikTokVideo(
        id=str(item.get("id")),
        created_at=(datetime.fromtimestamp(created, UTC)
                    if isinstance(created, (int, float)) else None),
        title=UntrustedText(raw=item.get("title") or "", source="tiktok.title"),
        description=UntrustedText(raw=item.get("video_description") or "",
                                  source="tiktok.video_description"),
        duration_seconds=int(duration) if isinstance(duration, (int, float)) else None,
        share_url=item.get("share_url"),
        cover_image_url=item.get("cover_image_url"),
        counters={name: int(item[name]) for name in VIDEO_COUNTERS
                  if isinstance(item.get(name), (int, float))},
    )
