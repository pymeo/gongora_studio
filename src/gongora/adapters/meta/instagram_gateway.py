"""Lectura de Instagram via Instagram API with Facebook Login (graph.facebook.com).

Endpoints usados (todos verificados contra la cuenta real):
    GET /me/accounts?fields=id,name,instagram_business_account
    GET /{ig-user-id}?fields=id,username,name,followers_count,media_count
    GET /{ig-user-id}/media?fields=...
    GET /{ig-media-id}/insights?metric=...

Este adaptador NO implementa publicacion, lectura de conversaciones ni gestion
de comentarios: esas capacidades estan pendientes de verificacion.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterator

from gongora.application.ports import ConnectionStatus, InsightsResult
from gongora.config import MetaCredentials, Settings
from gongora.domain import metrics as metrics_catalog
from gongora.domain.errors import (
    MetaApiError,
    MetricUnsupportedError,
    PermissionMissingError,
    TokenRejectedError,
)
from gongora.domain.models import AccountProfile, MediaItem
from gongora.domain.platform import Platform
from gongora.domain.untrusted import UntrustedText
from gongora.adapters.meta.http_client import GraphHttpClient
from gongora.logging_setup import get_logger

RawSink = Callable[[str, Any], None]

#: Tope de paginas por recorrido. Si la API devolviera paginas vacias con
#: enlace "next", esto impide un bucle indefinido.
MAX_PAGES = 40

#: Capacidades que este adaptador declara pero NO ha verificado. No se marcan
#: como operativas aunque se hayan solicitado los permisos.
PENDING_CAPABILITIES = (
    "publicar_contenido",
    "leer_conversaciones",
    "gestionar_comentarios",
)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("+0000", "+00:00"))
    except ValueError:
        return None


def _strip_paging_urls(payload: dict[str, Any]) -> dict[str, Any]:
    """Quita las URLs de paginacion antes de persistir la respuesta.

    `paging.next` y `paging.previous` llevan el token incrustado. Los cursores
    (`paging.cursors`) son opacos y si se conservan.
    """
    if not isinstance(payload, dict):
        return payload
    clean = dict(payload)
    paging = clean.get("paging")
    if isinstance(paging, dict):
        clean["paging"] = {k: v for k, v in paging.items()
                           if k not in ("next", "previous")}
    return clean


class InstagramGateway:
    """Implementa `SocialDataGateway` para Instagram."""

    platform = Platform.INSTAGRAM

    def __init__(
        self,
        credentials: MetaCredentials,
        *,
        client: GraphHttpClient,
        page_size: int = 25,
        raw_sink: RawSink | None = None,
    ) -> None:
        self._credentials = credentials
        self._client = client
        self._page_size = page_size
        self._raw_sink = raw_sink
        self._log = get_logger("instagram")

    @classmethod
    def from_settings(cls, settings: Settings, *, raw_sink: RawSink | None = None,
                      sleep=None) -> "InstagramGateway":
        kwargs: dict[str, Any] = {
            "timeout": settings.http_timeout_seconds,
            "max_attempts": settings.http_max_attempts,
        }
        if sleep is not None:
            kwargs["sleep"] = sleep
        client = GraphHttpClient(settings.credentials, **kwargs)
        return cls(settings.credentials, client=client,
                   page_size=settings.media_page_size, raw_sink=raw_sink)

    # ------------------------------------------------------------ propiedades

    @property
    def api_calls(self) -> int:
        return self._client.call_count

    @property
    def token_rejected(self) -> bool:
        return self._client.token_rejected

    def _emit_raw(self, endpoint: str, payload: dict[str, Any]) -> None:
        if self._raw_sink is not None:
            self._raw_sink(endpoint, _strip_paging_urls(payload))

    # -------------------------------------------------------------- contrato

    def is_configured(self) -> bool:
        creds = self._credentials
        return bool(creds.token and creds.instagram_account_id and creds.page_id)

    def check_connection(self) -> ConnectionStatus:
        """Comprueba token, vinculo pagina-Instagram y capacidades de lectura."""
        if not self.is_configured():
            return ConnectionStatus(
                platform=self.platform, connected=False,
                detail="Configuracion incompleta (falta token, pagina o cuenta de Instagram).",
                token_state="absent", pending_capabilities=PENDING_CAPABILITIES,
            )

        verified: list[str] = []
        try:
            accounts = self._client.get("me/accounts",
                                        {"fields": "id,name,instagram_business_account"})
            self._emit_raw("/me/accounts", accounts.payload)
            pages = accounts.payload.get("data") or []
            verified.append("listar_paginas")

            page = next((p for p in pages if str(p.get("id")) == self._credentials.page_id), None)
            if page is None:
                found = ", ".join(str(p.get("id")) for p in pages) or "ninguna"
                return ConnectionStatus(
                    platform=self.platform, connected=False,
                    detail=(f"META_PAGE_ID={self._credentials.page_id} no aparece entre las "
                            f"paginas del token (encontradas: {found})."),
                    token_state="valid", verified_capabilities=tuple(verified),
                    pending_capabilities=PENDING_CAPABILITIES,
                )

            linked = (page.get("instagram_business_account") or {}).get("id")
            if str(linked) != self._credentials.instagram_account_id:
                return ConnectionStatus(
                    platform=self.platform, connected=False,
                    detail=(f"La pagina {self._credentials.page_id} esta vinculada a la cuenta "
                            f"de Instagram {linked}, no a {self._credentials.instagram_account_id}."),
                    token_state="valid", verified_capabilities=tuple(verified),
                    pending_capabilities=PENDING_CAPABILITIES,
                )
            verified.append("vinculo_pagina_instagram")

            profile = self.fetch_profile()
            verified.append("leer_perfil")

            sample = next(self.iter_media(max_items=1), None)
            if sample is not None:
                verified.append("leer_publicaciones")
                insights = self.fetch_insights(sample)
                if insights.values:
                    verified.append("leer_insights")

            return ConnectionStatus(
                platform=self.platform, connected=True,
                detail=(f"Conectado a @{profile.username} (cuenta {profile.account_id}) "
                        f"via pagina {self._credentials.page_id}."),
                token_state="valid", verified_capabilities=tuple(verified),
                pending_capabilities=PENDING_CAPABILITIES,
            )
        except TokenRejectedError as exc:
            return ConnectionStatus(
                platform=self.platform, connected=False,
                detail=f"Meta rechazo el token: {exc.detail.message}",
                token_state="rejected", verified_capabilities=tuple(verified),
                pending_capabilities=PENDING_CAPABILITIES,
            )
        except PermissionMissingError as exc:
            return ConnectionStatus(
                platform=self.platform, connected=False,
                detail=f"Permiso ausente: {exc.detail.message}",
                token_state="valid", verified_capabilities=tuple(verified),
                pending_capabilities=PENDING_CAPABILITIES,
            )
        except MetaApiError as exc:
            return ConnectionStatus(
                platform=self.platform, connected=False,
                detail=f"Error de la Graph API: {exc.detail.message}",
                token_state="unknown", verified_capabilities=tuple(verified),
                pending_capabilities=PENDING_CAPABILITIES,
            )

    def fetch_profile(self) -> AccountProfile:
        account_id = self._credentials.instagram_account_id
        response = self._client.get(
            account_id, {"fields": ",".join(metrics_catalog.IG_PROFILE_FIELDS)}
        )
        self._emit_raw(f"/{account_id}", response.payload)
        data = response.payload
        counters = {
            field: int(data[field])
            for field in ("followers_count", "media_count")
            if isinstance(data.get(field), (int, float))
        }
        return AccountProfile(
            platform=self.platform,
            account_id=str(data.get("id") or account_id),
            username=data.get("username"),
            name=data.get("name"),
            counters=counters,
            fetched_at=datetime.now().astimezone(),
        )

    def iter_media(self, *, max_items: int) -> Iterator[MediaItem]:
        """Recorre /media paginando. Valida el host de cada pagina siguiente."""
        account_id = self._credentials.instagram_account_id
        params = {
            "fields": ",".join(metrics_catalog.IG_MEDIA_FIELDS),
            "limit": min(self._page_size, max_items),
        }
        response = self._client.get(f"{account_id}/media", params)
        emitted = 0
        page_index = 0
        while True:
            self._emit_raw(f"/{account_id}/media#p{page_index}", response.payload)
            for item in response.payload.get("data") or []:
                yield MediaItem(
                    platform=self.platform,
                    media_id=str(item.get("id")),
                    media_type=item.get("media_type"),
                    product_type=item.get("media_product_type"),
                    permalink=item.get("permalink"),
                    published_at=_parse_timestamp(item.get("timestamp")),
                    like_count=item.get("like_count"),
                    comments_count=item.get("comments_count"),
                    caption=UntrustedText(raw=item.get("caption") or "",
                                          source="instagram.caption"),
                )
                emitted += 1
                if emitted >= max_items:
                    return
            next_url = ((response.payload.get("paging") or {}).get("next"))
            if not next_url:
                return
            page_index += 1
            if page_index >= MAX_PAGES:
                self._log.warning(
                    "Paginacion detenida en %s paginas (tope de seguridad). "
                    "Se conserva lo recorrido.", page_index)
                return
            response = self._client.get_url(
                next_url, endpoint_label=f"/{account_id}/media#p{page_index}"
            )

    def fetch_insights(self, media: MediaItem) -> InsightsResult:
        """Metricas de una publicacion.

        Estrategia: una peticion con el nucleo. Si Meta la rechaza por metricas
        no admitidas, se degrada a peticiones de una metrica para conservar las
        que si funcionan. Los extras van aparte: su fallo no afecta al nucleo.
        """
        plan = metrics_catalog.plan_for(self.platform, media.product_type)
        values: dict[str, tuple[float, str]] = {}
        gaps: dict[str, tuple[str, str]] = {}
        calls_before = self._client.call_count

        self._request_metrics(media, plan.core, values, gaps, degrade=True)
        if plan.optional:
            self._request_metrics(media, plan.optional, values, gaps, degrade=True,
                                  optional=True)

        for metric in plan.all_requested:
            if metric not in values and metric not in gaps:
                gaps[metric] = ("ausente_en_respuesta",
                                "Meta no devolvio esta metrica. Ausente no es cero.")
        return InsightsResult(values=values, gaps=gaps,
                              api_calls=self._client.call_count - calls_before)

    # --------------------------------------------------------------- interno

    def _request_metrics(
        self,
        media: MediaItem,
        wanted: tuple[str, ...],
        values: dict[str, tuple[float, str]],
        gaps: dict[str, tuple[str, str]],
        *,
        degrade: bool,
        optional: bool = False,
    ) -> bool:
        if not wanted:
            return True
        try:
            response = self._client.get(f"{media.media_id}/insights",
                                        {"metric": ",".join(wanted)})
            label = "extras" if optional else "nucleo"
            self._emit_raw(f"/{media.media_id}/insights#{label}", response.payload)
            self._absorb(response.payload, values)
            return True
        except (MetricUnsupportedError, PermissionMissingError, MetaApiError) as exc:
            reason = type(exc).__name__
            message = exc.detail.message if isinstance(exc, MetaApiError) else str(exc)
            if degrade and len(wanted) > 1:
                self._log.info(
                    "Peticion conjunta rechazada para %s (%s). Degradando a metrica "
                    "por metrica para conservar las que funcionen.", media.media_id, reason
                )
                for metric in wanted:
                    self._request_metrics(media, (metric,), values, gaps,
                                          degrade=False, optional=optional)
                return False
            for metric in wanted:
                gaps[metric] = (reason, message)
            return False
        except TokenRejectedError:
            # El token no sirve: no degradamos ni reintentamos, propagamos.
            raise

    @staticmethod
    def _absorb(payload: dict[str, Any], values: dict[str, tuple[float, str]]) -> None:
        """Extrae valores admitiendo las dos formas que usa Meta."""
        for entry in payload.get("data") or []:
            name = entry.get("name")
            if not name:
                continue
            period = entry.get("period") or "lifetime"
            series = entry.get("values")
            if isinstance(series, list) and series:
                raw = series[0].get("value")
            else:
                raw = (entry.get("total_value") or {}).get("value")
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue  # ausente o no numerico: se tratara como hueco
            values[name] = (float(raw), period)
