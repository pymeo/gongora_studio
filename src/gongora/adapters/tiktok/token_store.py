"""Persistencia local de los tokens de TikTok.

Los tokens se guardan en el mismo fichero de credenciales de TikTok
(`~/.config/gongora/tiktok.env`), que es el sitio que el proyecto ya reserva
para ellos. Reglas:

- escritura atomica (fichero temporal en el mismo directorio + `os.replace`),
  para que un corte nunca deje el fichero a medias;
- permisos 600 en el fichero y 700 en el directorio si hay que crearlo;
- se conservan intactas las demas lineas (client key, comentarios...);
- el client secret no se escribe nunca desde aqui: si vino por entorno, se
  queda en el entorno.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC
from pathlib import Path

from gongora.adapters.tiktok.oauth import TokenSet

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class TikTokTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def save(self, tokens: TokenSet) -> None:
        updates = {
            "TIKTOK_ACCESS_TOKEN": tokens.access_token,
            "TIKTOK_REFRESH_TOKEN": tokens.refresh_token,
            "TIKTOK_OPEN_ID": tokens.open_id or "",
            "TIKTOK_SCOPES": ",".join(tokens.scopes),
            "TIKTOK_ACCESS_TOKEN_EXPIRES_AT": _iso(tokens.access_expires_at),
            "TIKTOK_REFRESH_TOKEN_EXPIRES_AT": (_iso(tokens.refresh_expires_at)
                                                if tokens.refresh_expires_at else ""),
        }
        self._write(updates)

    def _write(self, updates: dict[str, str]) -> None:
        for key, value in updates.items():
            if any(ch in value for ch in "\n\r\"' #"):
                raise ValueError(f"Valor no serializable de forma segura para {key}.")
        lines: list[str] = []
        if self.path.is_file():
            lines = self.path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
            while lines and not lines[-1].strip():
                lines.pop()
        pending = dict(updates)
        for index, line in enumerate(lines):
            match = _LINE.match(line)
            if match and match.group(1) in pending:
                key = match.group(1)
                lines[index] = f"{key}={pending.pop(key)}"
        if not lines:
            lines.append("# Credenciales de TikTok de Gongora Studio (chmod 600). "
                         "Nunca en el repositorio.")
        lines.extend(f"{key}={value}" for key, value in pending.items())

        directory = self.path.parent
        if not directory.exists():
            directory.mkdir(mode=0o700, parents=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tiktok.", suffix=".tmp", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        os.chmod(self.path, 0o600)


def _iso(value) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat()
