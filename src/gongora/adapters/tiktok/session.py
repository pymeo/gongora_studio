"""Sesion de TikTok: entrega un access token vigente y lo refresca solo.

Es lo unico que el cliente HTTP necesita saber de OAuth: pide un token antes
de cada llamada y avisa si TikTok lo rechaza. La sesion decide si refrescar,
lo hace bajo cerrojo (para que la recogida programada y un comando manual no
roten el refresh token a la vez) y persiste el resultado.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from gongora.adapters.locking import FileLock, LockBusy
from gongora.adapters.tiktok.oauth import OAuthError, TikTokOAuthClient, TokenSet
from gongora.adapters.tiktok.token_store import TikTokTokenStore
from gongora.config import TikTokCredentials, load_tiktok_credentials
from gongora.logging_setup import get_logger

#: Se refresca si al token le queda menos que esto (TikTok: 24 h de vida).
REFRESH_MARGIN = timedelta(minutes=10)
LOCK_WAIT_SECONDS = 30.0


class TikTokSession:
    def __init__(self, credentials: TikTokCredentials, *, store: TikTokTokenStore,
                 oauth: TikTokOAuthClient | None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._credentials = credentials
        self._store = store
        self._oauth = oauth
        self._clock = clock
        self._sleep = sleep
        self._log = get_logger("tiktok.session")
        self.refresh_count = 0

    @classmethod
    def from_credentials(cls, credentials: TikTokCredentials, secrets_file: Path, *,
                         timeout: float = 20.0) -> "TikTokSession":
        oauth = None
        if credentials.client_secret:
            oauth = TikTokOAuthClient(credentials.client_key, credentials.client_secret,
                                      timeout=timeout)
        return cls(credentials, store=TikTokTokenStore(secrets_file), oauth=oauth)

    # ----------------------------------------------------------- consulta

    @property
    def credentials(self) -> TikTokCredentials:
        return self._credentials

    @property
    def can_refresh(self) -> bool:
        return self._oauth is not None and bool(self._credentials.refresh_token)

    def _expiring(self) -> bool:
        expires = self._credentials.access_token_expires_at
        return expires is not None and expires - self._clock() <= REFRESH_MARGIN

    # ------------------------------------------- lo que usa el cliente HTTP

    def access_token(self) -> str:
        """Token para la siguiente llamada; refresca antes si esta por caducar."""
        if self._expiring() and self.can_refresh:
            self.refresh(reason="caducidad proxima")
        return self._credentials.access_token

    def on_token_rejected(self) -> bool:
        """TikTok rechazo el token. True si se obtuvo uno nuevo y procede reintentar."""
        if not self.can_refresh:
            return False
        try:
            self.refresh(reason="token rechazado")
        except OAuthError as exc:
            self._log.warning("Refresco de TikTok fallido: %s", exc.safe_message)
            return False
        return True

    # ------------------------------------------------------------ refresco

    def refresh(self, *, reason: str = "manual") -> TokenSet | None:
        """Refresca y persiste. Devuelve None si otro proceso ya lo habia hecho."""
        if self._oauth is None:
            raise OAuthError("Falta TIKTOK_CLIENT_SECRET: no se puede refrescar el token.")
        if not self._credentials.refresh_token:
            raise OAuthError("No hay refresh token. Ejecuta: gongora tiktok login")

        lock = FileLock(self._store.path.parent / "tiktok.refresh.lock")
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                lock.__enter__()
                break
            except LockBusy:
                if time.monotonic() >= deadline:
                    raise OAuthError("Otro proceso lleva demasiado tiempo refrescando "
                                     "el token de TikTok.") from None
                self._sleep(0.5)
        try:
            # Otro proceso pudo refrescar mientras esperabamos: si el fichero ya
            # tiene un token distinto y vigente, se adopta en lugar de rotar otra vez.
            stored = load_tiktok_credentials(self._store.path)
            if (stored is not None and reason != "manual"
                    and stored.access_token != self._credentials.access_token):
                self._credentials = stored
                if not self._expiring():
                    self._log.info("Token de TikTok ya refrescado por otro proceso.")
                    return None
            tokens = self._oauth.refresh(self._credentials.refresh_token or "")
            self._store.save(tokens)
            self._credentials = replace(
                self._credentials,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                open_id=tokens.open_id or self._credentials.open_id,
                scopes=tokens.scopes or self._credentials.scopes,
                access_token_expires_at=tokens.access_expires_at,
                refresh_token_expires_at=(tokens.refresh_expires_at
                                          or self._credentials.refresh_token_expires_at),
            )
            self.refresh_count += 1
            self._log.info("Token de TikTok refrescado (%s). Caduca: %s",
                           reason, tokens.access_expires_at.isoformat())
            return tokens
        finally:
            lock.__exit__(None, None, None)
