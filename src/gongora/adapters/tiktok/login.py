"""Orquestacion del login de TikTok (accion del operador, no de un agente).

    TikTokLogin(app).start()        -> AuthorizationRequest (URL a abrir)
    .complete_with_server(request)  -> espera el redirect en localhost
    .complete_with_url(request, u)  -> o usa la URL pegada a mano
Ambos terminan cambiando el code por tokens y guardandolos.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from gongora.adapters.tiktok.callback import (
    CallbackResult,
    parse_callback_url,
    wait_for_callback,
)
from gongora.adapters.tiktok.oauth import (
    AuthorizationRequest,
    OAuthError,
    TikTokOAuthClient,
    TokenSet,
    build_authorization_request,
)
from gongora.adapters.tiktok.token_store import TikTokTokenStore
from gongora.config import DEFAULT_TIKTOK_SCOPES, TikTokAppConfig


@dataclass(frozen=True)
class LoginResult:
    tokens: TokenSet
    requested_scopes: tuple[str, ...]

    @property
    def missing_scopes(self) -> tuple[str, ...]:
        return tuple(s for s in self.requested_scopes if s not in self.tokens.scopes)


class TikTokLogin:
    def __init__(self, app: TikTokAppConfig, *, oauth: TikTokOAuthClient | None = None,
                 store: TikTokTokenStore | None = None) -> None:
        if app.missing:
            raise OAuthError("Faltan " + ", ".join(app.missing) + " (en el entorno o en "
                             f"{app.secrets_file}). Ver docs/tiktok-conexion.md.")
        self.app = app
        self._oauth = oauth or TikTokOAuthClient(app.client_key or "", app.client_secret or "")
        self._store = store or TikTokTokenStore(app.secrets_file)

    def start(self, scopes: tuple[str, ...] = DEFAULT_TIKTOK_SCOPES) -> AuthorizationRequest:
        return build_authorization_request(self.app.client_key or "",
                                           redirect_uri=self.app.redirect_uri, scopes=scopes)

    def complete_with_server(self, request: AuthorizationRequest, *, timeout_seconds: float,
                             bind_host: str = "127.0.0.1",
                             on_ready: Callable[[], None] | None = None) -> LoginResult:
        callback = wait_for_callback(request.redirect_uri, expected_state=request.state,
                                     timeout_seconds=timeout_seconds, bind_host=bind_host,
                                     on_ready=on_ready)
        return self._finish(request, callback)

    def complete_with_url(self, request: AuthorizationRequest, url: str) -> LoginResult:
        callback = parse_callback_url(url, expected_state=request.state,
                                      redirect_uri=request.redirect_uri)
        return self._finish(request, callback)

    def _finish(self, request: AuthorizationRequest, callback: CallbackResult) -> LoginResult:
        tokens = self._oauth.exchange_code(callback.code, code_verifier=request.pkce.verifier,
                                           redirect_uri=request.redirect_uri)
        self._store.save(tokens)
        return LoginResult(tokens=tokens, requested_scopes=request.scopes)
