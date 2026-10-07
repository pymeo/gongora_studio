"""Riesgo: configuracion mal cargada o credenciales expuestas."""

from __future__ import annotations

import pytest

from gongora.config import (
    ConfigError,
    load_settings,
    load_tiktok_credentials,
    parse_env_file,
)
from tests.conftest import FAKE_TOKEN


def _escribe(path, contenido: str):
    path.write_text(contenido, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_parsea_formatos_habituales(tmp_path):
    fichero = _escribe(tmp_path / "s.env", (
        '# comentario\n'
        'export META_ACCESS_TOKEN="con-comillas"\n'
        "META_PAGE_ID='1234'\n"
        "META_GRAPH_VERSION=v26.0 # al final\n"
        "INSTAGRAM_ACCOUNT_ID=999"
    ))
    datos = parse_env_file(fichero)
    assert datos["META_ACCESS_TOKEN"] == "con-comillas"
    assert datos["META_PAGE_ID"] == "1234"
    assert datos["META_GRAPH_VERSION"] == "v26.0"
    assert datos["INSTAGRAM_ACCOUNT_ID"] == "999"


def test_falla_si_falta_una_variable(tmp_path):
    fichero = _escribe(tmp_path / "s.env", "META_ACCESS_TOKEN=x\n")
    with pytest.raises(ConfigError) as exc:
        load_settings(secrets_file=fichero, home=tmp_path / "var")
    assert "Faltan variables" in str(exc.value)


def test_rechaza_el_flujo_de_instagram_login(tmp_path):
    """No se mezcla Instagram Login con Instagram API with Facebook Login."""
    fichero = _escribe(tmp_path / "s.env", (
        f"META_ACCESS_TOKEN={FAKE_TOKEN}\n"
        "META_AUTH_FLOW=instagram_login\n"
        "META_GRAPH_VERSION=v26.0\n"
        "META_PAGE_ID=1\nINSTAGRAM_ACCOUNT_ID=2\n"
    ))
    with pytest.raises(ConfigError) as exc:
        load_settings(secrets_file=fichero, home=tmp_path / "var")
    assert "Instagram Login" in str(exc.value)


def test_el_token_no_aparece_en_repr(tmp_path):
    fichero = _escribe(tmp_path / "s.env", (
        f"META_ACCESS_TOKEN={FAKE_TOKEN}\n"
        "META_AUTH_FLOW=facebook_login\nMETA_GRAPH_VERSION=v26.0\n"
        "META_PAGE_ID=1\nINSTAGRAM_ACCOUNT_ID=2\n"
    ))
    settings = load_settings(secrets_file=fichero, home=tmp_path / "var")
    assert FAKE_TOKEN not in repr(settings.credentials)
    assert FAKE_TOKEN not in repr(settings)
    assert FAKE_TOKEN not in str(settings)
    # La huella identifica el token sin revelarlo.
    assert len(settings.credentials.token_fingerprint) == 12
    assert FAKE_TOKEN not in settings.credentials.token_fingerprint


def test_tiktok_sin_fichero_es_pendiente(tmp_path):
    assert load_tiktok_credentials(tmp_path / "no-existe.env") is None


def test_tiktok_incompleto_es_pendiente(tmp_path):
    fichero = _escribe(tmp_path / "tt.env", "TIKTOK_CLIENT_KEY=abc\n")
    assert load_tiktok_credentials(fichero) is None


def test_tiktok_completo_carga_y_oculta_secretos(tmp_path):
    fichero = _escribe(tmp_path / "tt.env", (
        "TIKTOK_CLIENT_KEY=clave\n"
        "TIKTOK_ACCESS_TOKEN=token-tiktok-falso-123\n"
        "TIKTOK_CLIENT_SECRET=secreto-falso-456\n"
        "TIKTOK_SCOPES=user.info.basic,user.info.stats,video.list\n"
    ))
    creds = load_tiktok_credentials(fichero)
    assert creds is not None
    assert creds.scopes == ("user.info.basic", "user.info.stats", "video.list")
    assert "token-tiktok-falso-123" not in repr(creds)
    assert "secreto-falso-456" not in repr(creds)
