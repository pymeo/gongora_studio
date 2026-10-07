"""Utilidades compartidas por las pruebas.

Ningun fixture contiene un token real. El token de prueba es una cadena
inventada con forma de token de Meta, para comprobar que la redaccion funciona.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gongora import redaction
from gongora.adapters.persistence.db import open_database
from gongora.adapters.persistence.queue import SqliteTaskQueue
from gongora.adapters.persistence.repositories import (
    SqliteAgentStateRepository,
    SqliteRunRepository,
    SqliteSnapshotRepository,
)
from gongora.clock import FrozenClock
from gongora.config import MetaCredentials, Paths, Settings

#: Token FALSO, solo para pruebas. Tiene la forma de uno real a proposito.
FAKE_TOKEN = "EAATESTtokenFALSOdepruebas1234567890abcdef"
FAKE_APP_SECRET = "secretoFALSOdepruebas0123456789"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 10, 7, 9, 0, 0, tzinfo=UTC))


@pytest.fixture
def credentials() -> MetaCredentials:
    return MetaCredentials(
        token=FAKE_TOKEN, graph_version="v26.0", auth_flow="facebook_login",
        page_id="1327434437128534", instagram_account_id="17841422599648154",
    )


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    paths = Paths(
        home=tmp_path, db=tmp_path / "gongora.sqlite3", reports=tmp_path / "reports",
        raw=tmp_path / "raw", logs=tmp_path / "logs", lock=tmp_path / "collect.lock",
    )
    paths.ensure()
    return paths


@pytest.fixture
def settings(credentials: MetaCredentials, paths: Paths, tmp_path: Path) -> Settings:
    return Settings(credentials=credentials, paths=paths,
                    secrets_file=tmp_path / "secrets.env", secrets_file_mode="0600")


@pytest.fixture
def db(paths: Paths):
    conn = open_database(paths.db)
    yield conn
    conn.close()


@pytest.fixture
def repos(db, clock):
    return {
        "snapshots": SqliteSnapshotRepository(db, clock),
        "runs": SqliteRunRepository(db, clock),
        "agent_state": SqliteAgentStateRepository(db, clock),
        "queue": SqliteTaskQueue(db, clock),
    }


# ----------------------------------------------------------- HTTP simulado

class FakeResponse:
    def __init__(self, payload, status: int = 200, headers: dict | None = None) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def http_error(status: int, payload: dict, headers: dict | None = None):
    """Construye un HTTPError como el que levanta urllib."""
    body = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        url="https://graph.facebook.com/v26.0/x", code=status, msg="error",
        hdrs=headers or {}, fp=io.BytesIO(body),
    )


class FakeOpener:
    """Opener de urllib simulado.

    `routes` empareja por subcadena de la URL. Cada entrada puede ser un valor
    o una lista de valores que se consumen en orden (para simular reintentos).
    Un valor puede ser un dict (respuesta 200), un FakeResponse o una excepcion.
    """

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = {key: (list(value) if isinstance(value, list) else [value])
                       for key, value in routes.items()}
        self.requests: list[tuple[str, dict]] = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.requests.append((url, dict(request.headers)))
        for fragment, queue in self.routes.items():
            if fragment in url:
                value = queue[0] if len(queue) == 1 else queue.pop(0)
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, FakeResponse):
                    return value
                return FakeResponse(value)
        raise AssertionError(f"Ruta no simulada: {url}")

    def header_of(self, index: int, name: str) -> str | None:
        headers = self.requests[index][1]
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
        return None


@pytest.fixture(autouse=True)
def register_fake_secret():
    """Registra el token falso para que la redaccion lo trate como secreto."""
    redaction.register_secret(FAKE_TOKEN)
    redaction.register_secret(FAKE_APP_SECRET)
    yield
