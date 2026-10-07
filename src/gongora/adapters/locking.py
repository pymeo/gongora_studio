"""Cerrojo de fichero para evitar ejecuciones solapadas.

`flock` no bloqueante: si otra recogida esta en curso, la segunda no espera,
avisa y termina. Esto es lo que impide que el temporizador se pise a si mismo.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from types import TracebackType


class LockBusy(RuntimeError):
    """Otro proceso tiene el cerrojo."""


class FileLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                holder = self._read_holder()
                raise LockBusy(
                    f"Ya hay una recogida en curso{holder}. No se lanza otra."
                ) from None
            raise
        os.truncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode())
        os.fsync(self._fd)
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def _read_holder(self) -> str:
        try:
            pid = self._path.read_text(encoding="utf-8").strip()
            return f" (PID {pid})" if pid else ""
        except OSError:
            return ""
