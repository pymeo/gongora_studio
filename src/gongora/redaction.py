"""Redaccion de secretos.

Unico punto del sistema que sabe como ocultar credenciales. Se usa en logs,
mensajes de error, respuestas crudas persistidas, payloads de tareas e informes.

Regla: ningun valor registrado aqui puede aparecer en texto que salga del
proceso (stdout, SQLite, ficheros, excepciones).
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"

# Valores literales registrados en tiempo de ejecucion (p. ej. el access token).
_registered: set[str] = set()

# Patrones estructurales que enmascaramos aunque el valor no este registrado:
# util cuando Meta nos devuelve una URL de paginacion con el token incrustado.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(access_token=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(client_secret=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    # Tokens de Meta: empiezan por EAA y son largos.
    re.compile(r"\bEAA[A-Za-z0-9._\-]{20,}"),
    # OAuth de TikTok: refresh token, verifier PKCE y code del redirect.
    re.compile(r"(refresh_token=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(code_verifier=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"([?&]code=)[^&\s\"']+"),
    # Tokens de TikTok: access "act." y refresh "rft.".
    re.compile(r"\b(?:act|rft)\.[A-Za-z0-9._\-!*]{20,}"),
)

_SENSITIVE_KEY = re.compile(r"(token|secret|password|authorization|credential)", re.IGNORECASE)

# Longitud minima para registrar un literal: evita enmascarar cadenas triviales.
_MIN_SECRET_LEN = 8


def register_secret(value: str | None) -> None:
    """Marca un valor como secreto para toda la vida del proceso."""
    if value and len(value) >= _MIN_SECRET_LEN:
        _registered.add(value)


def registered_count() -> int:
    """Numero de secretos registrados (para diagnostico; nunca los valores)."""
    return len(_registered)


def redact(text: str) -> str:
    """Devuelve el texto con cualquier secreto conocido o sospechoso enmascarado."""
    if not text:
        return text
    out = text
    # Primero los literales conocidos: los mas largos antes, por si uno contiene a otro.
    for secret in sorted(_registered, key=len, reverse=True):
        if secret in out:
            out = out.replace(secret, MASK)
    for pattern in _PATTERNS:
        if pattern.groups:
            out = pattern.sub(lambda m: m.group(1) + MASK, out)
        else:
            out = pattern.sub(MASK, out)
    return out


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Redacta recursivamente una estructura JSON-like.

    Las claves cuyo nombre sugiere una credencial se enmascaran enteras,
    independientemente de su contenido.
    """
    if _depth > 32:  # cortafuegos ante estructuras ciclicas o absurdamente profundas
        return MASK
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY.search(key):
                out[key] = MASK
            else:
                out[key] = redact_value(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_value(item, _depth=_depth + 1) for item in value]
    return value


def contains_secret(value: Any) -> bool:
    """True si la estructura contiene algun secreto conocido o con forma de token.

    Se usa como ultima barrera antes de persistir payloads y eventos.
    """
    return redact_value(value) != value
