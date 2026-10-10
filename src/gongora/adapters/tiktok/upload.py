"""Subida de videos a TikTok (Content Posting API).

Dos modos, cada uno con su scope:
- BORRADOR (`video.upload`, flujo inbox): el video llega a la bandeja de la app
  de TikTok. Esta API no admite descripcion.
- PUBLICACION DIRECTA (`video.publish`, Direct Post): publica con descripcion
  (`post_info.title`). Antes se consulta `creator_info` y se usa uno de sus
  `privacy_level_options`. Sin auditoria de TikTok solo se permite `SELF_ONLY`
  (privado) y en cuentas privadas
  (error `unaudited_client_can_only_post_to_private_accounts`).

Flujo:
1. POST /v2/post/publish/inbox/video/init/ (borrador)
   o    /v2/post/publish/video/init/       (directa)  -> publish_id, upload_url
2. PUT  upload_url por trozos (Content-Range)          -> 206 ... 201
3. POST /v2/post/publish/status/fetch/ hasta un estado final

Limite de TikTok: pocas subidas pendientes por cuenta en 24 h. Por eso el
paso 1 no se reintenta nunca.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from gongora.adapters.tiktok.http_client import TikTokHttpClient
from gongora.domain.errors import ApiErrorDetail, MetaApiError, UnsafePaginationError
from gongora.logging_setup import get_logger

MB = 1024 * 1024
#: Reglas de troceado de TikTok (media transfer guide).
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
#: Trozo usado cuando el video no cabe en uno solo. El ultimo trozo absorbe el
#: resto, asi que queda por debajo de 2 * CHUNK (TikTok admite hasta 128 MB).
CHUNK = 32 * MB

INIT_PATH = "post/publish/inbox/video/init"
DIRECT_INIT_PATH = "post/publish/video/init"
CREATOR_INFO_PATH = "post/publish/creator_info/query"
STATUS_PATH = "post/publish/status/fetch"
#: Maximo de la descripcion, en unidades UTF-16.
MAX_TITLE_UTF16 = 2200
#: Estados finales con exito (borrador entregado o publicado).
DONE_STATUSES = frozenset({"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"})
FAILED_STATUS = "FAILED"
UPLOAD_HOST_SUFFIX = ".tiktokapis.com"


@dataclass(frozen=True)
class ChunkPlan:
    video_size: int
    chunk_size: int
    total_chunk_count: int
    #: (primer_byte, ultimo_byte) inclusive, como en Content-Range.
    ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class DraftUploadResult:
    publish_id: str
    status: str
    fail_reason: str | None
    chunks_sent: int
    #: Solo en publicacion directa publica: ids de los posts visibles.
    post_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CreatorInfo:
    nickname: str | None
    privacy_level_options: tuple[str, ...]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int | None


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def plan_chunks(video_size: int, chunk: int = CHUNK) -> ChunkPlan:
    if video_size <= 0:
        raise ValueError("El video esta vacio.")
    if video_size <= MAX_CHUNK:
        # Menos de 5 MB es obligatorio un solo trozo; hasta 64 MB es lo mas simple.
        return ChunkPlan(video_size, video_size, 1, ((0, video_size - 1),))
    if not MIN_CHUNK <= chunk <= MAX_CHUNK:
        raise ValueError("El trozo debe medir entre 5 y 64 MB.")
    count = video_size // chunk
    ranges = [(i * chunk, (i + 1) * chunk - 1) for i in range(count - 1)]
    ranges.append(((count - 1) * chunk, video_size - 1))
    return ChunkPlan(video_size, chunk, count, tuple(ranges))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(MB), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_upload_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host.endswith(UPLOAD_HOST_SUFFIX):
        raise UnsafePaginationError(f"URL de subida no permitida: host {host!r}.")
    return url


def _upload_error(message: str, http_status: int | None = None) -> MetaApiError:
    return MetaApiError(ApiErrorDetail(
        http_status=http_status, code=None, subcode=None, error_type="tiktok_upload",
        message=message, endpoint="upload_url"))


class TikTokDraftUploader:
    def __init__(self, client: TikTokHttpClient, *, opener: Any | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 poll_interval: float = 5.0, poll_timeout: float = 1200.0,
                 put_timeout: float = 300.0) -> None:
        self._client = client
        self._opener = opener or urllib.request.build_opener()
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._put_timeout = put_timeout
        self._log = get_logger("tiktok.upload")

    def creator_info(self) -> CreatorInfo:
        """Obligatorio antes de publicar: opciones de privacidad de la cuenta."""
        data = self._client.post(CREATOR_INFO_PATH).data
        duration = data.get("max_video_post_duration_sec")
        return CreatorInfo(
            nickname=data.get("creator_nickname"),
            privacy_level_options=tuple(data.get("privacy_level_options") or ()),
            comment_disabled=bool(data.get("comment_disabled")),
            duet_disabled=bool(data.get("duet_disabled")),
            stitch_disabled=bool(data.get("stitch_disabled")),
            max_video_post_duration_sec=(int(duration) if isinstance(duration, int)
                                         else None),
        )

    def upload(self, path: Path, *, post_info: dict[str, Any] | None = None,
               on_progress: Callable[[str], None] = lambda _msg: None) -> DraftUploadResult:
        """Sin `post_info`: borrador. Con `post_info`: publicacion directa."""
        plan = plan_chunks(path.stat().st_size)
        body: dict[str, Any] = {"source_info": {
            "source": "FILE_UPLOAD",
            "video_size": plan.video_size,
            "chunk_size": plan.chunk_size,
            "total_chunk_count": plan.total_chunk_count,
        }}
        if post_info is not None:
            if utf16_len(str(post_info.get("title") or "")) > MAX_TITLE_UTF16:
                raise ValueError(f"La descripcion supera {MAX_TITLE_UTF16} caracteres.")
            body["post_info"] = post_info
        init_path = DIRECT_INIT_PATH if post_info is not None else INIT_PATH
        response = self._client.post(init_path, max_attempts=1, body=body)
        publish_id = str(response.data.get("publish_id") or "")
        upload_url = str(response.data.get("upload_url") or "")
        if not publish_id or not upload_url:
            raise _upload_error("TikTok no devolvio publish_id/upload_url al iniciar la subida.")
        validate_upload_url(upload_url)
        on_progress(f"Subida iniciada (publish_id {publish_id}), "
                    f"{plan.total_chunk_count} trozo(s).")

        sent = 0
        with path.open("rb") as handle:
            for index, (first, last) in enumerate(plan.ranges, 1):
                handle.seek(first)
                self._put(upload_url, handle.read(last - first + 1), first, last,
                          plan.video_size, final=index == plan.total_chunk_count)
                sent += 1
                on_progress(f"Trozo {index}/{plan.total_chunk_count} enviado.")

        status, reason, post_ids = self._wait(publish_id, on_progress)
        return DraftUploadResult(publish_id, status, reason, sent, post_ids)

    def _put(self, url: str, data: bytes, first: int, last: int, total: int, *,
             final: bool) -> None:
        # Sin cabecera Authorization: la upload_url ya va firmada por TikTok.
        request = urllib.request.Request(url, data=data, method="PUT", headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(len(data)),
            "Content-Range": f"bytes {first}-{last}/{total}",
        })
        try:
            with self._opener.open(request, timeout=self._put_timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise _upload_error("La upload_url de TikTok ha caducado (403).", 403) from None
            raise _upload_error(f"TikTok rechazo un trozo: HTTP {exc.code}.", exc.code) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise _upload_error(f"Fallo de red subiendo a TikTok: {reason}") from None
        expected = 201 if final else 206
        if status not in (expected, 200, 201):
            raise _upload_error(f"Respuesta inesperada al subir un trozo: HTTP {status}.", status)

    def _wait(self, publish_id: str, on_progress: Callable[[str], None]
              ) -> tuple[str, str | None, tuple[str, ...]]:
        waited, last_status = 0.0, ""
        while True:
            data = self._client.post(STATUS_PATH, body={"publish_id": publish_id}).data
            status = str(data.get("status") or "")
            reason = data.get("fail_reason") or None
            # Nombre literal de la API (con su errata).
            post_ids = tuple(str(i) for i in data.get("publicaly_available_post_id") or ())
            if status != last_status:
                on_progress(f"Estado: {status or 'desconocido'}")
                last_status = status
            if status in DONE_STATUSES or status == FAILED_STATUS:
                return status, reason, post_ids
            if waited >= self._poll_timeout:
                return status or "DESCONOCIDO", reason, post_ids
            self._sleep(self._poll_interval)
            waited += self._poll_interval
