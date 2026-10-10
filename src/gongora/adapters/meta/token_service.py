"""Diagnostico y renovacion de tokens de Meta.

Flujo oficial verificado en la documentacion de Meta (octubre 2026):

1. Token de usuario de corta duracion -> larga duracion (~60 dias):
       GET /oauth/access_token
           ?grant_type=fb_exchange_token
           &client_id={app-id}
           &client_secret={app-secret}
           &fb_exchange_token={token-corto}
   Debe hacerse en servidor, porque incluye el app secret.

2. Con ese token de usuario de larga duracion:
       GET /{user-id}/accounts
   devuelve tokens de Pagina que **no tienen fecha de caducidad** y solo se
   invalidan en ciertas condiciones (cambio de contrasena, retirada de
   permisos, etc.).

Este modulo implementa ambos pasos. NO inventa credenciales: si falta
META_APP_ID o META_APP_SECRET, lo dice y no hace nada.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gongora import redaction
from gongora.adapters.meta.http_client import GRAPH_HOST
from gongora.logging_setup import get_logger

_log = get_logger("meta.token")


@dataclass(frozen=True)
class TokenInfo:
    """Resultado de /debug_token, ya saneado."""

    is_valid: bool
    app_id: str | None
    application: str | None
    token_type: str | None
    expires_at: datetime | None
    data_access_expires_at: datetime | None
    scopes: tuple[str, ...]
    error_message: str | None = None

    @property
    def never_expires(self) -> bool:
        return self.is_valid and self.expires_at is None

    def remaining_description(self) -> str:
        if not self.is_valid:
            return "token no valido"
        if self.expires_at is None:
            return "sin fecha de caducidad"
        delta = self.expires_at - datetime.now(UTC)
        if delta.total_seconds() <= 0:
            return f"caducado el {self.expires_at:%Y-%m-%d %H:%M UTC}"
        days, rest = divmod(int(delta.total_seconds()), 86400)
        hours = rest // 3600
        return f"caduca el {self.expires_at:%Y-%m-%d %H:%M UTC} (en {days}d {hours}h)"


def _get_json(path: str, params: dict[str, str], *, timeout: float = 20.0) -> dict:
    """GET sin cabecera de autorizacion: las credenciales van en la query.

    Se usa solo para los endpoints de OAuth, que lo exigen asi. El host esta
    fijado y la respuesta y los errores pasan por redaccion.
    """
    query = urllib.parse.urlencode(params)
    url = f"https://{GRAPH_HOST}/{path.lstrip('/')}?{query}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "gongora-studio/0.1 (+local)"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"error": {"message": redaction.redact(body[:300])}}
        return parsed
    except urllib.error.URLError as exc:
        return {"error": {"message": f"Fallo de red: {exc.reason}"}}


def inspect_token(token: str, graph_version: str) -> TokenInfo:
    """Consulta /debug_token usando el propio token como credencial de lectura."""
    payload = _get_json(f"{graph_version}/debug_token",
                        {"input_token": token, "access_token": token})
    if "error" in payload:
        return TokenInfo(
            is_valid=False, app_id=None, application=None, token_type=None,
            expires_at=None, data_access_expires_at=None, scopes=(),
            error_message=redaction.redact(str(payload["error"].get("message", ""))),
        )
    data = payload.get("data") or {}

    def _stamp(key: str) -> datetime | None:
        raw = data.get(key)
        if not isinstance(raw, (int, float)) or raw <= 0:
            return None  # 0 significa "sin caducidad"
        return datetime.fromtimestamp(raw, UTC)

    return TokenInfo(
        is_valid=bool(data.get("is_valid")),
        app_id=str(data.get("app_id")) if data.get("app_id") else None,
        application=data.get("application"),
        token_type=data.get("type"),
        expires_at=_stamp("expires_at"),
        data_access_expires_at=_stamp("data_access_expires_at"),
        scopes=tuple(data.get("scopes") or ()),
        error_message=(None if data.get("is_valid")
                       else redaction.redact(str((data.get("error") or {}).get("message", ""))
                                             or "el token no es valido")),
    )


@dataclass(frozen=True)
class ExchangeResult:
    ok: bool
    detail: str
    #: El token NUNCA se imprime. Solo se escribe en el fichero de secretos.
    token: str | None = None
    expires_in_seconds: int | None = None


def exchange_for_long_lived(*, short_token: str, app_id: str, app_secret: str,
                            graph_version: str) -> ExchangeResult:
    """Paso 1 del flujo oficial: token de usuario de larga duracion (~60 dias)."""
    redaction.register_secret(app_secret)
    payload = _get_json(f"{graph_version}/oauth/access_token", {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_token,
    })
    if "error" in payload:
        return ExchangeResult(
            ok=False,
            detail=redaction.redact(str(payload["error"].get("message", "error desconocido"))),
        )
    token = payload.get("access_token")
    if not token:
        return ExchangeResult(ok=False, detail="Meta no devolvio ningun access_token.")
    redaction.register_secret(token)
    expires = payload.get("expires_in")
    return ExchangeResult(
        ok=True, token=token,
        expires_in_seconds=int(expires) if isinstance(expires, (int, float)) else None,
        detail=("Token de usuario de larga duracion obtenido"
                + (f" (~{int(expires) // 86400} dias)." if isinstance(expires, (int, float))
                   else " (Meta no informo la duracion).")),
    )


def fetch_page_token(*, user_token: str, page_id: str,
                     graph_version: str) -> ExchangeResult:
    """Paso 2: token de Pagina, que no caduca si el de usuario es de larga duracion.

    Si el token recibido YA es de Pagina, `/me/accounts` no existe (el nodo
    `/me` es la propia pagina). En ese caso no hay nada que canjear: se informa
    y se conserva el token tal cual.
    """
    quien = _get_json(f"{graph_version}/me", {"fields": "id,name",
                                              "access_token": user_token})
    if str(quien.get("id")) == str(page_id):
        return ExchangeResult(
            ok=True, token=user_token,
            detail=(f"El token ya es de la pagina {page_id} "
                    f"({quien.get('name') or 'sin nombre'}): no hace falta canjearlo. "
                    "Los tokens de Pagina no tienen fecha de caducidad."))
    payload = _get_json(f"{graph_version}/me/accounts",
                        {"fields": "id,name,access_token", "access_token": user_token})
    if "error" in payload:
        return ExchangeResult(
            ok=False,
            detail=redaction.redact(str(payload["error"].get("message", "error desconocido"))))
    for page in payload.get("data") or []:
        if str(page.get("id")) == str(page_id):
            token = page.get("access_token")
            if not token:
                return ExchangeResult(
                    ok=False,
                    detail=(f"La pagina {page_id} aparece pero sin access_token: "
                            "revisa el permiso pages_show_list."))
            redaction.register_secret(token)
            return ExchangeResult(
                ok=True, token=token,
                detail=(f"Token de la pagina {page_id} obtenido. Los tokens de Pagina "
                        "derivados de un token de usuario de larga duracion no tienen "
                        "fecha de caducidad."))
    found = ", ".join(str(p.get("id")) for p in (payload.get("data") or [])) or "ninguna"
    return ExchangeResult(ok=False,
                          detail=f"La pagina {page_id} no aparece en /me/accounts ({found}).")


def write_secret(path: Path, key: str, value: str) -> None:
    """Actualiza una clave del fichero de secretos sin imprimir su valor.

    Preserva el resto de lineas y mantiene permisos 600.
    """
    lines: list[str] = []
    replaced = False
    if path.is_file():
        for line in path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n"):
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
                if not replaced:
                    lines.append(f"{key}={value}")
                    replaced = True
                continue
            lines.append(line)
    if not replaced:
        lines.append(f"{key}={value}")
    content = "\n".join(line for line in lines if line is not None).strip() + "\n"

    # Escritura atomica con permisos restringidos desde el primer momento.
    temp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    _log.info("Actualizada la clave %s en el fichero de secretos (valor no registrado).", key)
