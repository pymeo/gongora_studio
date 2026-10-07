"""Cliente HTTP para la Display API de TikTok.

Mismas defensas que el cliente de Meta: token en cabecera, host en lista
blanca, timeouts, reintentos acotados, redaccion de secretos y sin reintentos
ante errores de autorizacion.

TikTok devuelve HTTP 200 con un sobre `{"data": ..., "error": {"code": ...}}`,
asi que la clasificacion de errores mira `error.code`, no solo el estado HTTP.

NO VERIFICADO contra la API real: falta autorizacion OAuth.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from gongora.domain.errors import (
    ApiErrorDetail,
    MetaApiError,
    PermissionMissingError,
    RateLimitedError,
    TokenRejectedError,
    TransientApiError,
    UnsafePaginationError,
)
from gongora.logging_setup import get_logger

TIKTOK_HOST = "open.tiktokapis.com"
ALLOWED_HOSTS = frozenset({TIKTOK_HOST})
API_BASE = f"https://{TIKTOK_HOST}/v2"

#: `error.code` de TikTok que invalidan el token: no se reintentan.
AUTH_ERROR_CODES = frozenset({
    "access_token_invalid", "access_token_expired", "invalid_access_token",
    "token_not_found",
})
#: Permiso/scope no concedido.
PERMISSION_ERROR_CODES = frozenset({
    "scope_not_authorized", "scope_permission_missed", "permission_denied",
})
RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "spam_risk_too_many_requests"})
TRANSIENT_CODES = frozenset({"internal_error", "service_unavailable"})
RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class TikTokResponse:
    status: int
    data: dict[str, Any]
    endpoint: str
    log_id: str | None = None


def validate_tiktok_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise UnsafePaginationError(f"URL de TikTok sin https: {parts.scheme!r}.")
    if parts.hostname not in ALLOWED_HOSTS:
        raise UnsafePaginationError(
            f"Host no permitido para TikTok: {parts.hostname!r}. "
            f"Permitidos: {', '.join(sorted(ALLOWED_HOSTS))}."
        )
    return url


class TikTokHttpClient:
    """`access_token` puede ser el token o una funcion que lo entrega (sesion).

    `on_token_rejected`, si se da, se llama cuando TikTok rechaza el token; si
    devuelve True (hay token nuevo) la peticion se repite UNA vez.
    """

    def __init__(self, access_token: str | Callable[[], str], *, timeout: float = 20.0,
                 max_attempts: int = 4, sleep=time.sleep, opener: Any | None = None,
                 on_token_rejected: Callable[[], bool] | None = None) -> None:
        self._token_provider = (access_token if callable(access_token)
                                else (lambda: access_token))
        self._on_token_rejected = on_token_rejected
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._opener = opener or urllib.request.build_opener()
        self._log = get_logger("tiktok.http")
        self.call_count = 0
        self.token_rejected = False

    def _headers(self, *, json_body: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "Accept": "application/json",
            "User-Agent": "gongora-studio/0.1 (+local)",
        }
        if json_body:
            headers["Content-Type"] = "application/json; charset=UTF-8"
        return headers

    def get(self, path: str, params: dict[str, Any] | None = None) -> TikTokResponse:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items()
                                        if v is not None})
        url = f"{API_BASE}/{path.strip('/')}/" + (f"?{query}" if query else "")
        return self._request(url, endpoint=f"/v2/{path.strip('/')}", body=None)

    def post(self, path: str, *, params: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None) -> TikTokResponse:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items()
                                        if v is not None})
        url = f"{API_BASE}/{path.strip('/')}/" + (f"?{query}" if query else "")
        return self._request(url, endpoint=f"/v2/{path.strip('/')}", body=body or {})

    def _request(self, url: str, *, endpoint: str,
                 body: dict[str, Any] | None) -> TikTokResponse:
        try:
            return self._send(url, endpoint=endpoint, body=body)
        except TokenRejectedError:
            if self._on_token_rejected is None:
                raise
            if not self._on_token_rejected():
                # Un solo intento de refresco por ejecucion: sin bucles.
                self._on_token_rejected = None
                raise
            self.token_rejected = False
            self._log.info("Token de TikTok renovado; se repite %s una vez.", endpoint)
            return self._send(url, endpoint=endpoint, body=body)

    def _send(self, url: str, *, endpoint: str,
              body: dict[str, Any] | None) -> TikTokResponse:
        validate_tiktok_url(url)
        if self.token_rejected:
            raise TokenRejectedError(ApiErrorDetail(
                http_status=None, code=None, subcode=None, error_type="tiktok_auth",
                message="Token de TikTok ya rechazado en esta ejecucion.",
                endpoint=endpoint))

        payload_bytes = (json.dumps(body).encode("utf-8") if body is not None else None)
        last: MetaApiError | None = None
        for attempt in range(1, self._max_attempts + 1):
            request = urllib.request.Request(
                url, data=payload_bytes,
                headers=self._headers(json_body=payload_bytes is not None),
                method="POST" if payload_bytes is not None else "GET",
            )
            self.call_count += 1
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    envelope = json.loads(raw) if raw else {}
                    error = self._check_envelope(envelope, endpoint, response.status)
                    if error is None:
                        return TikTokResponse(
                            status=response.status,
                            data=envelope.get("data") or {},
                            endpoint=endpoint,
                            log_id=(envelope.get("error") or {}).get("log_id"),
                        )
                    last = error
                    if isinstance(error, TokenRejectedError):
                        self.token_rejected = True
                        raise error
                    if not error.retryable or attempt >= self._max_attempts:
                        raise error
                    self._backoff(attempt, endpoint, error.detail.message)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                try:
                    envelope = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    envelope = {}
                error = self._check_envelope(envelope, endpoint, exc.code) or TransientApiError(
                    ApiErrorDetail(http_status=exc.code, code=None, subcode=None,
                                   error_type="http", message=f"HTTP {exc.code}",
                                   endpoint=endpoint))
                last = error
                if isinstance(error, (TokenRejectedError, PermissionMissingError)):
                    if isinstance(error, TokenRejectedError):
                        self.token_rejected = True
                    raise error
                if exc.code not in RETRYABLE_HTTP or attempt >= self._max_attempts:
                    raise error
                self._backoff(attempt, endpoint, error.detail.message)
            except (urllib.error.URLError, TimeoutError) as exc:
                reason = getattr(exc, "reason", exc)
                last = TransientApiError(ApiErrorDetail(
                    http_status=None, code=None, subcode=None, error_type="transport",
                    message=f"Fallo de red hacia TikTok: {reason}", endpoint=endpoint))
                if attempt >= self._max_attempts:
                    raise last
                self._backoff(attempt, endpoint, str(reason))
        assert last is not None
        raise last

    def _check_envelope(self, envelope: dict[str, Any], endpoint: str,
                        status: int) -> MetaApiError | None:
        error = envelope.get("error") or {}
        code = (error.get("code") or "").lower()
        if not code or code == "ok":
            return None
        detail = ApiErrorDetail(
            http_status=status, code=None, subcode=None, error_type=code,
            message=error.get("message") or code, endpoint=endpoint,
        )
        if code in AUTH_ERROR_CODES:
            return TokenRejectedError(detail)
        if code in PERMISSION_ERROR_CODES:
            return PermissionMissingError(detail)
        if code in RATE_LIMIT_CODES:
            return RateLimitedError(detail)
        if code in TRANSIENT_CODES or status in RETRYABLE_HTTP:
            return TransientApiError(detail)
        return MetaApiError(detail)

    def _backoff(self, attempt: int, endpoint: str, reason: str) -> None:
        delay = min(2 ** attempt, 30) + random.uniform(0, 0.5)
        self._log.warning("Reintento %s para %s en %.1fs (%s)",
                          attempt, endpoint, delay, reason[:200])
        self._sleep(delay)
