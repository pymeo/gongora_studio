"""OAuth 2.0 de TikTok para aplicaciones de escritorio (Login Kit, Desktop).

Flujo: authorization code + PKCE.

    1. Se genera `code_verifier` (aleatorio) y `state` (anti-CSRF).
    2. Se abre `https://www.tiktok.com/v2/auth/authorize/` con `code_challenge`.
    3. TikTok redirige a la redirect URI local con `code` y `state`.
    4. Se cambia `code` (+ `code_verifier`) por access y refresh token.

PARTICULARIDAD DE TIKTOK: en Desktop, `code_challenge` es el SHA-256 del
verifier codificado en HEXADECIMAL, no en base64url como dice RFC 7636. Si se
usa base64url, TikTok rechaza el intercambio. Fuente:
https://developers.tiktok.com/doc/login-kit-desktop

Este modulo no conoce agentes, base de datos ni ficheros: solo el protocolo.
NO VERIFICADO contra la API real todavia.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import string
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from gongora import redaction
from gongora.domain.errors import GongoraError

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
OAUTH_HOSTS = frozenset({"www.tiktok.com", "open.tiktokapis.com"})
#: TikTok solo admite localhost o 127.0.0.1 como host de redirect en Desktop.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})

_VERIFIER_ALPHABET = string.ascii_letters + string.digits + "-._~"
VERIFIER_LENGTH = 64  # RFC 7636: entre 43 y 128


class OAuthError(GongoraError):
    """Fallo del flujo OAuth. El mensaje ya esta saneado."""

    def __init__(self, message: str, *, error_code: str | None = None,
                 log_id: str | None = None) -> None:
        super().__init__(redaction.redact(message))
        self.error_code = error_code
        self.log_id = log_id


# ------------------------------------------------------------------- PKCE

def code_challenge_for(verifier: str) -> str:
    """SHA-256 del verifier en hexadecimal (variante de TikTok Desktop)."""
    return hashlib.sha256(verifier.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class PkcePair:
    verifier: str = field(repr=False)
    challenge: str
    method: str = "S256"

    @classmethod
    def generate(cls, length: int = VERIFIER_LENGTH) -> "PkcePair":
        if not 43 <= length <= 128:
            raise ValueError("El code_verifier debe tener entre 43 y 128 caracteres.")
        verifier = "".join(secrets.choice(_VERIFIER_ALPHABET) for _ in range(length))
        redaction.register_secret(verifier)
        return cls(verifier=verifier, challenge=code_challenge_for(verifier))


def new_state() -> str:
    return secrets.token_urlsafe(32)


# --------------------------------------------------------- authorize URL

def validate_redirect_uri(uri: str) -> str:
    """Aplica las reglas de TikTok Desktop: loopback, puerto, sin query."""
    parts = urllib.parse.urlsplit(uri)
    if parts.scheme not in ("http", "https"):
        raise OAuthError(f"Redirect URI con esquema no valido: {parts.scheme!r}.")
    if parts.hostname not in LOOPBACK_HOSTS:
        raise OAuthError("La redirect URI de escritorio debe usar localhost o 127.0.0.1.")
    if parts.port is None:
        raise OAuthError("La redirect URI debe llevar puerto explicito (p. ej. :3455).")
    if parts.query or parts.fragment:
        raise OAuthError("La redirect URI no puede llevar parametros ni fragmento.")
    return uri


@dataclass(frozen=True)
class AuthorizationRequest:
    """Todo lo que hay que recordar entre abrir el navegador y recibir el code."""

    url: str
    state: str = field(repr=False)
    pkce: PkcePair
    redirect_uri: str
    scopes: tuple[str, ...]


def build_authorization_request(client_key: str, *, redirect_uri: str,
                                scopes: tuple[str, ...], pkce: PkcePair | None = None,
                                state: str | None = None) -> AuthorizationRequest:
    validate_redirect_uri(redirect_uri)
    if not scopes:
        raise OAuthError("Hay que pedir al menos un scope.")
    pkce = pkce or PkcePair.generate()
    state = state or new_state()
    query = urllib.parse.urlencode({
        "client_key": client_key,
        "response_type": "code",
        "scope": ",".join(scopes),  # TikTok separa scopes con comas
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
    })
    return AuthorizationRequest(url=f"{AUTHORIZE_URL}?{query}", state=state, pkce=pkce,
                                redirect_uri=redirect_uri, scopes=tuple(scopes))


# ----------------------------------------------------------------- tokens

@dataclass(frozen=True)
class TokenSet:
    """Respuesta del token endpoint. Los secretos quedan fuera de repr."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    open_id: str | None
    scopes: tuple[str, ...]
    access_expires_at: datetime
    refresh_expires_at: datetime | None
    token_type: str = "Bearer"

    def __post_init__(self) -> None:
        redaction.register_secret(self.access_token)
        redaction.register_secret(self.refresh_token)

    @classmethod
    def from_response(cls, payload: dict[str, Any], *, now: datetime) -> "TokenSet":
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        if not access or not refresh:
            raise OAuthError("Respuesta de TikTok sin access_token o refresh_token.")
        redaction.register_secret(access)
        redaction.register_secret(refresh)
        expires_in = payload.get("expires_in")
        refresh_in = payload.get("refresh_expires_in")
        if not isinstance(expires_in, (int, float)):
            raise OAuthError("Respuesta de TikTok sin expires_in valido.")
        return cls(
            access_token=access,
            refresh_token=refresh,
            open_id=payload.get("open_id") or None,
            scopes=tuple(s for s in str(payload.get("scope") or "").split(",") if s),
            access_expires_at=now + timedelta(seconds=int(expires_in)),
            refresh_expires_at=(now + timedelta(seconds=int(refresh_in))
                                if isinstance(refresh_in, (int, float)) else None),
            token_type=str(payload.get("token_type") or "Bearer"),
        )


class TikTokOAuthClient:
    """Cliente del token endpoint de TikTok.

    Sin reintentos automaticos a proposito: un `code` es de un solo uso y un
    refresh puede rotar el refresh token, asi que repetir a ciegas puede dejar
    la sesion invalidada.
    """

    def __init__(self, client_key: str, client_secret: str, *, timeout: float = 20.0,
                 opener: Any | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        redaction.register_secret(client_secret)
        self._client_key = client_key
        self._client_secret = client_secret
        self._timeout = timeout
        self._opener = opener or urllib.request.build_opener()
        self._clock = clock

    def exchange_code(self, code: str, *, code_verifier: str, redirect_uri: str) -> TokenSet:
        redaction.register_secret(code)
        return self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        })

    def refresh(self, refresh_token: str) -> TokenSet:
        """Ojo: el refresh token devuelto puede ser distinto. Hay que guardarlo."""
        return self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })

    def _token_request(self, form: dict[str, str]) -> TokenSet:
        if urllib.parse.urlsplit(TOKEN_URL).hostname not in OAUTH_HOSTS:
            raise OAuthError("Host del token endpoint fuera de la lista blanca.")
        body = urllib.parse.urlencode({
            "client_key": self._client_key,
            "client_secret": self._client_secret,
            **form,
        }).encode("utf-8")
        request = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cache-Control": "no-cache",
            "Accept": "application/json",
            "User-Agent": "gongora-studio/0.1 (+local)",
        })
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            payload = _safe_json(raw)
            raise _oauth_error(payload, fallback=f"HTTP {exc.code} del token endpoint") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise OAuthError(f"Fallo de red hacia TikTok: {reason}") from None

        payload = _safe_json(raw)
        # TikTok responde plano; si algun dia lo envuelve en "data", lo aceptamos.
        if isinstance(payload.get("data"), dict) and "access_token" in payload["data"]:
            payload = payload["data"]
        if payload.get("error") and payload.get("error") != "ok":
            raise _oauth_error(payload, fallback="Error del token endpoint")
        return TokenSet.from_response(payload, now=self._clock())


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _oauth_error(payload: dict[str, Any], *, fallback: str) -> OAuthError:
    error = payload.get("error")
    if isinstance(error, dict):  # forma de sobre de la API v2
        code = error.get("code")
        description = error.get("message")
        log_id = error.get("log_id")
    else:
        code = error
        description = payload.get("error_description")
        log_id = payload.get("log_id")
    message = f"{code}: {description}" if code and description else (code or fallback)
    return OAuthError(f"TikTok OAuth: {str(message)[:300]}",
                      error_code=str(code) if code else None, log_id=log_id)
