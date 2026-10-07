"""Riesgo: un comando de credenciales que revienta antes de explicar que falta.

Sin META_APP_ID/META_APP_SECRET, `gongora token renew` debe decirlo y salir
sin hacer ninguna llamada a Meta. El token de prueba es FALSO.
"""

from __future__ import annotations

from gongora import cli
from tests.conftest import FAKE_TOKEN


def test_token_renew_sin_credenciales_de_app_explica_y_no_llama(tmp_path, monkeypatch,
                                                                capsys):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        f"META_ACCESS_TOKEN={FAKE_TOKEN}\nMETA_AUTH_FLOW=facebook_login\n"
        "META_GRAPH_VERSION=v26.0\nMETA_PAGE_ID=1\nINSTAGRAM_ACCOUNT_ID=2\n",
        encoding="utf-8")
    secrets.chmod(0o600)
    monkeypatch.setenv("GONGORA_SECRETS_FILE", str(secrets))
    monkeypatch.setenv("GONGORA_HOME", str(tmp_path / "var"))

    def sin_red(*args, **kwargs):
        raise AssertionError("No debe llamar a Meta sin credenciales de app")

    monkeypatch.setattr(cli.token_service, "exchange_for_long_lived", sin_red)

    assert cli.main(["token", "renew", "--write"]) == 2
    salida = capsys.readouterr().out
    assert "META_APP_ID" in salida and "no se inventan" in salida
    assert FAKE_TOKEN not in salida
    assert secrets.read_text(encoding="utf-8").count(FAKE_TOKEN) == 1  # sin tocar
