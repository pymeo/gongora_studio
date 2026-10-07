"""Recepcion del redirect de OAuth en localhost.

Servidor HTTP de un solo uso: escucha en el puerto de la redirect URI, atiende
UNA peticion a la ruta esperada, valida `state` y se apaga. No registra las
peticiones (el `code` viaja en la query) y no sirve nada mas.

Alternativa sin servidor: `parse_callback_url`, para pegar a mano la URL a la
que redirigio el navegador (util en WSL si el puerto no llega a Linux).
"""

from __future__ import annotations

import hmac
import html
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from gongora import redaction
from gongora.domain.errors import GongoraError

DEFAULT_TIMEOUT_SECONDS = 300


class CallbackError(GongoraError):
    """El redirect no trajo un code valido (error de TikTok, state, timeout...)."""

    def __init__(self, message: str) -> None:
        super().__init__(redaction.redact(message))


@dataclass(frozen=True)
class CallbackResult:
    code: str = field(repr=False)
    granted_scopes: tuple[str, ...] = ()


def parse_callback_query(query: str, *, expected_state: str) -> CallbackResult:
    """Valida la query del redirect y extrae el authorization code."""
    params = urllib.parse.parse_qs(query, keep_blank_values=True)

    def first(name: str) -> str:
        values = params.get(name) or [""]
        return values[0]

    if first("code"):
        redaction.register_secret(first("code"))
    if first("error"):
        # Texto que viene de fuera: se acota y se sanea; no se interpreta.
        description = first("error_description")[:200]
        raise CallbackError(f"TikTok denego la autorizacion: {first('error')[:80]}"
                            + (f" - {description}" if description else ""))
    state = first("state")
    if not state or not hmac.compare_digest(state, expected_state):
        raise CallbackError("El parametro state no coincide: se descarta el redirect "
                            "(posible peticion ajena o login antiguo).")
    code = first("code")
    if not code:
        raise CallbackError("El redirect no trae authorization code.")
    scopes = tuple(s for s in first("scopes").split(",") if s)
    return CallbackResult(code=code, granted_scopes=scopes)


def parse_callback_url(url: str, *, expected_state: str, redirect_uri: str) -> CallbackResult:
    """Igual que el servidor, pero con la URL completa pegada por la persona."""
    got = urllib.parse.urlsplit(url.strip())
    want = urllib.parse.urlsplit(redirect_uri)
    if (got.port, got.path.rstrip("/")) != (want.port, want.path.rstrip("/")):
        raise CallbackError("La URL pegada no corresponde a la redirect URI configurada.")
    return parse_callback_query(got.query, expected_state=expected_state)


_PAGE = """<!doctype html><html lang="es"><meta charset="utf-8">
<title>Gongora Studio</title>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem">
<h1 style="font-size:1.3rem">Gongora Studio</h1><p>{message}</p></body></html>"""


def wait_for_callback(redirect_uri: str, *, expected_state: str,
                      timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                      bind_host: str = "127.0.0.1",
                      on_ready: Callable[[], None] | None = None) -> CallbackResult:
    """Escucha hasta recibir el redirect o agotar el tiempo.

    `on_ready` se llama cuando el puerto ya esta abierto: es el momento de
    abrir el navegador, para que el redirect no llegue antes que el servidor.
    """
    parts = urllib.parse.urlsplit(redirect_uri)
    expected_path = parts.path or "/"
    outcome: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (API de http.server)
            request = urllib.parse.urlsplit(self.path)
            if request.path.rstrip("/") != expected_path.rstrip("/"):
                self._reply(404, "No encontrado.")
                return
            try:
                outcome["result"] = parse_callback_query(request.query,
                                                         expected_state=expected_state)
                self._reply(200, "Cuenta de TikTok autorizada. Ya puedes cerrar esta "
                                 "pestana y volver a la terminal.")
            except CallbackError as exc:
                outcome["error"] = exc
                self._reply(400, "No se completo la autorizacion. Revisa la terminal.")

        def _reply(self, status: int, message: str) -> None:
            body = _PAGE.format(message=html.escape(message)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            # La linea de peticion contiene el code: no se escribe en ningun sitio.
            return

    try:
        server = HTTPServer((bind_host, parts.port or 80), Handler)
    except OSError as exc:
        raise CallbackError(
            f"No se pudo escuchar en {bind_host}:{parts.port} ({exc.strerror}). "
            "Cierra lo que use ese puerto o usa `gongora tiktok login --paste`."
        ) from None

    server.timeout = 1.0
    deadline = time.monotonic() + timeout_seconds
    try:
        if on_ready is not None:
            on_ready()
        while "result" not in outcome and "error" not in outcome:
            if time.monotonic() >= deadline:
                raise CallbackError(
                    f"No llego el redirect en {int(timeout_seconds)} s. Vuelve a lanzar "
                    "el login (o usa --paste si el navegador no alcanza el puerto).")
            server.handle_request()
    finally:
        server.server_close()

    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["result"]  # type: ignore[return-value]
