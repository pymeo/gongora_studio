"""Lectura de TikTok via Display API v2.

ESTADO: PENDIENTE DE CONEXION.

El codigo implementa la forma documentada de la Display API, pero no se ha
ejecutado contra la API real porque todavia no hay autorizacion OAuth. Sin
credenciales:
  - `is_configured()` devuelve False,
  - `check_connection()` devuelve "pendiente de conexion" con el siguiente paso,
  - las lecturas lanzan `ConnectorNotAuthorized`.
Nunca devuelve datos simulados.

Diferencia importante con Instagram: en la Display API los contadores
(`view_count`, `like_count`, `comment_count`, `share_count`) llegan como campos
del propio video en `/v2/video/list/`, no desde un endpoint de insights
separado. `fetch_insights` los lee de lo recogido en `iter_media`.

Publicacion: NO implementada aqui a proposito. La Content Posting API es un
producto distinto, con su propia revision, y "Direct Post" requiere aprobacion
que no damos por supuesta. Crear contenido y publicarlo son capacidades
separadas en este sistema.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable, Iterator

from gongora.application.ports import ConnectionStatus, InsightsResult
from gongora.config import TikTokCredentials, resolve_tiktok_secrets_file
from gongora.domain import metrics as metrics_catalog
from gongora.domain.errors import (
    ConnectorNotAuthorized,
    MetaApiError,
    PermissionMissingError,
    TokenRejectedError,
)
from gongora.domain.models import AccountProfile, MediaItem
from gongora.domain.platform import Platform
from gongora.domain.untrusted import UntrustedText
from gongora.adapters.tiktok.http_client import TikTokHttpClient
from gongora.adapters.tiktok.session import TikTokSession
from gongora.logging_setup import get_logger

RawSink = Callable[[str, Any], None]

#: Scopes de la Display API necesarios para lo que Faro necesita leer.
REQUIRED_SCOPES = ("user.info.basic", "user.info.stats", "video.list")

#: Capacidades declaradas y no verificadas. Publicacion aparte: otro producto.
PENDING_CAPABILITIES = (
    "leer_perfil",
    "leer_videos",
    "leer_contadores",
)
NOT_IMPLEMENTED_CAPABILITIES = (
    "publicar_contenido",
    "gestionar_comentarios",
)

NEXT_STEP = (
    "poner TIKTOK_CLIENT_KEY y TIKTOK_CLIENT_SECRET en ~/.config/gongora/tiktok.env "
    "y ejecutar `gongora tiktok login` (ver docs/tiktok-conexion.md)"
)


class TikTokDisplayGateway:
    """Implementa `SocialDataGateway` para TikTok. Pendiente de autorizacion."""

    platform = Platform.TIKTOK

    def __init__(self, credentials: TikTokCredentials | None, *,
                 client: TikTokHttpClient | None = None,
                 page_size: int = 20, raw_sink: RawSink | None = None) -> None:
        self._credentials = credentials
        self._client = client
        self._page_size = page_size
        self._raw_sink = raw_sink
        self._log = get_logger("tiktok")
        #: media_id -> {metrica: valor}. Lo rellena iter_media.
        self._counters: dict[str, dict[str, float]] = {}

    @classmethod
    def from_settings(cls, settings, *, raw_sink: RawSink | None = None,
                      sleep=None) -> "TikTokDisplayGateway":
        credentials = settings.tiktok
        client = None
        if credentials is not None:
            kwargs: dict[str, Any] = {
                "timeout": settings.http_timeout_seconds,
                "max_attempts": settings.http_max_attempts,
            }
            if sleep is not None:
                kwargs["sleep"] = sleep
            # La sesion entrega el token vigente y lo refresca; el gateway no
            # sabe nada de OAuth.
            session = TikTokSession.from_credentials(
                credentials, settings.tiktok_secrets_file or resolve_tiktok_secrets_file(),
                timeout=settings.http_timeout_seconds)
            client = TikTokHttpClient(session.access_token,
                                      on_token_rejected=session.on_token_rejected, **kwargs)
        return cls(credentials, client=client, raw_sink=raw_sink)

    # ------------------------------------------------------------ propiedades

    @property
    def api_calls(self) -> int:
        return self._client.call_count if self._client else 0

    @property
    def token_rejected(self) -> bool:
        return bool(self._client and self._client.token_rejected)

    def _emit_raw(self, endpoint: str, payload: Any) -> None:
        if self._raw_sink is not None:
            self._raw_sink(endpoint, payload)

    def _require_client(self) -> TikTokHttpClient:
        if self._credentials is None or self._client is None:
            raise ConnectorNotAuthorized("TikTok", NEXT_STEP)
        return self._client

    def _missing_scopes(self) -> tuple[str, ...]:
        if self._credentials is None or not self._credentials.scopes:
            return ()
        return tuple(s for s in REQUIRED_SCOPES if s not in self._credentials.scopes)

    # -------------------------------------------------------------- contrato

    def is_configured(self) -> bool:
        return self._credentials is not None

    def check_connection(self) -> ConnectionStatus:
        if not self.is_configured():
            return ConnectionStatus(
                platform=self.platform, connected=False,
                detail=f"Pendiente de conexion. Siguiente paso: {NEXT_STEP}.",
                token_state="absent",
                verified_capabilities=(),
                pending_capabilities=PENDING_CAPABILITIES + NOT_IMPLEMENTED_CAPABILITIES,
            )
        missing = self._missing_scopes()
        if missing:
            return ConnectionStatus(
                platform=self.platform, connected=False,
                detail=("Credenciales presentes pero faltan scopes declarados: "
                        + ", ".join(missing)),
                token_state="unknown",
                pending_capabilities=PENDING_CAPABILITIES,
            )
        try:
            profile = self.fetch_profile()
            return ConnectionStatus(
                platform=self.platform, connected=True,
                detail=(f"Conectado a TikTok como {profile.username or profile.account_id}. "
                        "Primera conexion: verifica los datos antes de confiar en ellos."),
                token_state="valid",
                verified_capabilities=("leer_perfil",),
                pending_capabilities=("leer_videos", "leer_contadores")
                                     + NOT_IMPLEMENTED_CAPABILITIES,
            )
        except TokenRejectedError as exc:
            return ConnectionStatus(
                platform=self.platform, connected=False,
                detail=f"TikTok rechazo el token: {exc.detail.message}",
                token_state="rejected", pending_capabilities=PENDING_CAPABILITIES,
            )
        except PermissionMissingError as exc:
            return ConnectionStatus(
                platform=self.platform, connected=False,
                detail=f"Scope no autorizado: {exc.detail.message}",
                token_state="valid", pending_capabilities=PENDING_CAPABILITIES,
            )
        except MetaApiError as exc:
            return ConnectionStatus(
                platform=self.platform, connected=False,
                detail=f"Error de la Display API: {exc.detail.message}",
                token_state="unknown", pending_capabilities=PENDING_CAPABILITIES,
            )

    def fetch_profile(self) -> AccountProfile:
        client = self._require_client()
        response = client.get("user/info",
                              {"fields": ",".join(metrics_catalog.TT_PROFILE_FIELDS)})
        self._emit_raw("/v2/user/info", response.data)
        user = response.data.get("user") or {}
        counters = {
            field: int(user[field])
            for field in ("follower_count", "following_count", "likes_count", "video_count")
            if isinstance(user.get(field), (int, float))
        }
        return AccountProfile(
            platform=self.platform,
            account_id=str(user.get("open_id") or self._credentials.open_id or "desconocido"),
            username=user.get("display_name"),
            name=user.get("display_name"),
            counters=counters,
            fetched_at=datetime.now(UTC),
        )

    def iter_media(self, *, max_items: int) -> Iterator[MediaItem]:
        """Recorre /v2/video/list/ paginando por cursor.

        Los contadores del video se guardan para `fetch_insights`.
        """
        client = self._require_client()
        cursor: int | None = None
        emitted = 0
        page = 0
        while emitted < max_items:
            body: dict[str, Any] = {"max_count": min(self._page_size, max_items - emitted)}
            if cursor is not None:
                body["cursor"] = cursor
            response = client.post(
                "video/list",
                params={"fields": ",".join(metrics_catalog.TT_VIDEO_FIELDS)},
                body=body,
            )
            self._emit_raw(f"/v2/video/list#p{page}", response.data)
            videos = response.data.get("videos") or []
            if not videos:
                return
            for video in videos:
                media_id = str(video.get("id"))
                self._counters[media_id] = {
                    metric: float(video[metric])
                    for metric in metrics_catalog.TT_VIDEO_COUNTERS
                    if isinstance(video.get(metric), (int, float))
                }
                created = video.get("create_time")
                yield MediaItem(
                    platform=self.platform,
                    media_id=media_id,
                    media_type="VIDEO",
                    product_type="VIDEO",
                    permalink=video.get("share_url"),
                    published_at=(datetime.fromtimestamp(created, UTC)
                                  if isinstance(created, (int, float)) else None),
                    like_count=video.get("like_count"),
                    comments_count=video.get("comment_count"),
                    caption=UntrustedText(raw=video.get("video_description") or "",
                                          source="tiktok.video_description"),
                )
                emitted += 1
                if emitted >= max_items:
                    return
            if not response.data.get("has_more"):
                return
            cursor = response.data.get("cursor")
            if cursor is None:
                return
            page += 1

    def fetch_insights(self, media: MediaItem) -> InsightsResult:
        """Contadores del video, tomados de lo que devolvio /video/list/."""
        if not self.is_configured():
            raise ConnectorNotAuthorized("TikTok", NEXT_STEP)
        plan = metrics_catalog.plan_for(self.platform)
        found = self._counters.get(media.media_id, {})
        values = {metric: (value, "lifetime") for metric, value in found.items()}
        gaps = {
            metric: ("ausente_en_respuesta",
                     "La Display API no devolvio este contador. Ausente no es cero.")
            for metric in plan.core if metric not in values
        }
        return InsightsResult(values=values, gaps=gaps, api_calls=0)
