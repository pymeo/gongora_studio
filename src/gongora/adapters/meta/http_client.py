"""Cliente HTTP para la Graph API de Meta.

Decisiones que este modulo garantiza:

- El token viaja en la cabecera `Authorization: Bearer`, no en la query.
- Timeouts siempre; reintentos acotados solo para errores transitorios.
- Los errores de autorizacion NO se reintentan: cortan de inmediato.
- Se respetan las cabeceras de uso de Meta (`X-App-Usage`,
  `X-Business-Use-Case-Usage`) y `Retry-After`, con backoff exponencial.
- Las URLs de paginacion se validan contra una lista de hosts permitidos y se
  les quita cualquier credencial antes de usarlas.
- Nada de lo que sale de aqui (excepciones, logs) contiene el token.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from gongora import redaction
from gongora.config import MetaCredentials
from gongora.domain.errors import (
    ApiErrorDetail,
    MetaApiError,
    MetricUnsupportedError,
    PermissionMissingError,
    RateLimitedError,
    TokenRejectedError,
    TransientApiError,
    UnsafePaginationError,
)
from gongora.logging_setup import get_logger

GRAPH_HOST = "graph.facebook.com"
ALLOWED_HOSTS = frozenset({GRAPH_HOST})

#: Codigos de Meta que significan "token invalido o caducado".
AUTH_ERROR_CODES = frozenset({102, 190, 463, 467})
#: Codigos de permiso ausente.
PERMISSION_ERROR_CODES = frozenset({10, 200, 201, 202, 203, 204, 205, 206, 207, 210})
#: Codigos de limite de uso.
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80001, 80002, 80003, 80004})
#: Codigos transitorios del lado de Meta.
TRANSIENT_CODES = frozenset({1, 2, 341})

RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})

#: Umbral de uso (%) a partir del cual esperamos antes de seguir pidiendo.
USAGE_COOLDOWN_THRESHOLD = 90.0
USAGE_WARN_THRESHOLD = 75.0


@dataclass
class UsageState:
    """Ultimo estado de consumo de cuota reportado por Meta."""

    call_count: float = 0.0
    total_cputime: float = 0.0
    total_time: float = 0.0
    estimated_time_to_regain_access: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def worst_percentage(self) -> float:
        return max(self.call_count, self.total_cputime, self.total_time)


@dataclass(frozen=True)
class GraphResponse:
    status: int
    payload: dict[str, Any]
    endpoint: str
    usage: UsageState


def _parse_usage(headers: Any) -> UsageState:
    state = UsageState()
    app_usage = headers.get("x-app-usage") or headers.get("X-App-Usage")
    if app_usage:
        try:
            data = json.loads(app_usage)
            state.call_count = float(data.get("call_count", 0))
            state.total_cputime = float(data.get("total_cputime", 0))
            state.total_time = float(data.get("total_time", 0))
            state.raw["app"] = data
        except (ValueError, TypeError):
            pass
    buc = headers.get("x-business-use-case-usage") or headers.get("X-Business-Use-Case-Usage")
    if buc:
        try:
            data = json.loads(buc)
            state.raw["business"] = data
            for entries in data.values():
                for entry in entries:
                    state.call_count = max(state.call_count,
                                           float(entry.get("call_count", 0) or 0))
                    state.total_cputime = max(state.total_cputime,
                                              float(entry.get("total_cputime", 0) or 0))
                    state.total_time = max(state.total_time,
                                           float(entry.get("total_time", 0) or 0))
                    state.estimated_time_to_regain_access = max(
                        state.estimated_time_to_regain_access,
                        float(entry.get("estimated_time_to_regain_access", 0) or 0),
                    )
        except (ValueError, TypeError, AttributeError):
            pass
    return state


def sanitize_graph_url(url: str) -> str:
    """Valida el host de una URL de Meta y le quita cualquier credencial.

    Se usa con `paging.next`: nunca seguimos una URL arbitraria.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise UnsafePaginationError(
            f"URL de paginacion sin https: esquema {parts.scheme!r} rechazado."
        )
    if parts.hostname not in ALLOWED_HOSTS:
        raise UnsafePaginationError(
            f"URL de paginacion con host no permitido: {parts.hostname!r}. "
            f"Permitidos: {', '.join(sorted(ALLOWED_HOSTS))}."
        )
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in ("access_token", "client_secret")
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
    )


class GraphHttpClient:
    """Cliente minimo y defensivo sobre urllib (sin dependencias externas)."""

    def __init__(
        self,
        credentials: MetaCredentials,
        *,
        timeout: float = 20.0,
        max_attempts: int = 4,
        sleep=time.sleep,
        opener: Any | None = None,
    ) -> None:
        self._credentials = credentials
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._opener = opener or urllib.request.build_opener()
        self._log = get_logger("meta.http")
        self.call_count = 0
        self.usage = UsageState()
        #: Se pone a True en cuanto Meta rechaza el token. Corta el resto del run.
        self.token_rejected = False

    # ------------------------------------------------------------------ api

    def get(self, path: str, params: dict[str, Any] | None = None) -> GraphResponse:
        """GET /<version>/<path> con parametros. El token va en la cabecera."""
        clean_path = path.lstrip("/")
        version = self._credentials.graph_version
        if not clean_path.startswith(version):
            clean_path = f"{version}/{clean_path}"
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None}
        )
        url = f"https://{GRAPH_HOST}/{clean_path}" + (f"?{query}" if query else "")
        return self._request(url, endpoint=f"/{clean_path}")

    def get_url(self, url: str, *, endpoint_label: str = "paging") -> GraphResponse:
        """GET de una URL de paginacion, previa validacion y limpieza."""
        safe_url = sanitize_graph_url(url)
        return self._request(safe_url, endpoint=endpoint_label)

    # -------------------------------------------------------------- interno

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._credentials.token}",
            "Accept": "application/json",
            "User-Agent": "gongora-studio/0.1 (+local)",
        }

    def _request(self, url: str, *, endpoint: str) -> GraphResponse:
        if self.token_rejected:
            raise TokenRejectedError(ApiErrorDetail(
                http_status=None, code=190, subcode=None, error_type="OAuthException",
                message="Token ya rechazado en esta ejecucion: no se repiten llamadas.",
                endpoint=endpoint,
            ))

        last_error: MetaApiError | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._respect_quota()
            request = urllib.request.Request(url, headers=self._headers(), method="GET")
            self.call_count += 1
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    usage = _parse_usage(response.headers)
                    self.usage = usage
                    payload = self._decode(body, endpoint, response.status)
                    self._log.debug(
                        "GET %s -> %s (intento %s, uso %.1f%%)",
                        endpoint, response.status, attempt, usage.worst_percentage,
                    )
                    return GraphResponse(status=response.status, payload=payload,
                                         endpoint=endpoint, usage=usage)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                usage = _parse_usage(exc.headers or {})
                self.usage = usage
                error = self._classify_http_error(exc, body, endpoint)
                retry_after = self._retry_after(exc.headers)
                last_error = error
                if isinstance(error, (TokenRejectedError, PermissionMissingError,
                                      MetricUnsupportedError)):
                    if isinstance(error, TokenRejectedError):
                        self.token_rejected = True
                        self._log.error("Token rechazado por Meta en %s: %s",
                                        endpoint, error.detail.message)
                    raise error
                if not error.retryable or attempt >= self._max_attempts:
                    raise error
                self._backoff(attempt, retry_after, endpoint, error.detail.message)
            except urllib.error.URLError as exc:
                detail = ApiErrorDetail(
                    http_status=None, code=None, subcode=None, error_type="transport",
                    message=f"Fallo de red: {exc.reason}", endpoint=endpoint,
                )
                last_error = TransientApiError(detail)
                if attempt >= self._max_attempts:
                    raise last_error
                self._backoff(attempt, None, endpoint, detail.message)
            except TimeoutError:
                detail = ApiErrorDetail(
                    http_status=None, code=None, subcode=None, error_type="timeout",
                    message=f"Timeout de {self._timeout}s", endpoint=endpoint,
                )
                last_error = TransientApiError(detail)
                if attempt >= self._max_attempts:
                    raise last_error
                self._backoff(attempt, None, endpoint, detail.message)

        assert last_error is not None
        raise last_error

    def _decode(self, body: str, endpoint: str, status: int) -> dict[str, Any]:
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise TransientApiError(ApiErrorDetail(
                http_status=status, code=None, subcode=None, error_type="decode",
                message=f"Respuesta no es JSON valido: {exc.msg}", endpoint=endpoint,
            )) from None
        if not isinstance(payload, dict):
            raise TransientApiError(ApiErrorDetail(
                http_status=status, code=None, subcode=None, error_type="decode",
                message="Respuesta JSON inesperada (no es objeto).", endpoint=endpoint,
            ))
        return payload

    def _classify_http_error(self, exc: urllib.error.HTTPError, body: str,
                             endpoint: str) -> MetaApiError:
        code = subcode = None
        error_type = None
        message = f"HTTP {exc.code}"
        try:
            parsed = json.loads(body) if body else {}
            error = parsed.get("error") or {}
            code = error.get("code")
            subcode = error.get("error_subcode")
            error_type = error.get("type")
            message = error.get("message") or message
            if error.get("error_user_msg"):
                message = f"{message} | {error['error_user_msg']}"
        except (json.JSONDecodeError, AttributeError):
            message = f"HTTP {exc.code}: {body[:300]}"

        detail = ApiErrorDetail(http_status=exc.code, code=code, subcode=subcode,
                                error_type=error_type, message=message, endpoint=endpoint)

        # El orden importa: Meta devuelve type=OAuthException tambien para
        # parametros invalidos (codigo 100), que no son problemas de token.
        if code in AUTH_ERROR_CODES:
            return TokenRejectedError(detail)
        if code in PERMISSION_ERROR_CODES:
            return PermissionMissingError(detail)
        if code in RATE_LIMIT_CODES or exc.code == 429:
            return RateLimitedError(detail, self._retry_after(exc.headers))
        if code == 100:
            if self._looks_like_metric_problem(message):
                return MetricUnsupportedError(detail)
            return MetaApiError(detail)  # parametro invalido: no se reintenta
        if error_type == "OAuthException":
            # Cualquier otro OAuthException sin codigo reconocido: tratamos el
            # token como rechazado, que es la interpretacion prudente.
            return TokenRejectedError(detail)
        if code in TRANSIENT_CODES or exc.code in RETRYABLE_HTTP:
            return TransientApiError(detail)
        return MetaApiError(detail)

    @staticmethod
    def _looks_like_metric_problem(message: str) -> bool:
        lowered = message.lower()
        return any(
            hint in lowered
            for hint in ("metric", "metrica", "does not support", "not supported",
                         "invalid parameter", "incompatible")
        )

    @staticmethod
    def _retry_after(headers: Any) -> float | None:
        if not headers:
            return None
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if not raw:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _backoff(self, attempt: int, retry_after: float | None, endpoint: str,
                 reason: str) -> None:
        base = retry_after if retry_after is not None else min(2 ** attempt, 30)
        delay = base + random.uniform(0, 0.5)  # jitter: evita sincronizar reintentos
        self._log.warning(
            "Reintento %s para %s en %.1fs (%s)",
            attempt, endpoint, delay, redaction.redact(reason)[:200],
        )
        self._sleep(delay)

    def _respect_quota(self) -> None:
        """Si Meta avisa de consumo alto, esperamos antes de seguir."""
        worst = self.usage.worst_percentage
        if worst >= USAGE_COOLDOWN_THRESHOLD:
            wait = self.usage.estimated_time_to_regain_access or 60.0
            wait = min(wait, 300.0)
            self._log.warning(
                "Cuota de Meta al %.1f%%: esperando %.0fs antes de continuar.", worst, wait
            )
            self._sleep(wait)
        elif worst >= USAGE_WARN_THRESHOLD:
            self._log.warning("Cuota de Meta al %.1f%%.", worst)
