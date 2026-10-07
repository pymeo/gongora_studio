"""Riesgo: dar por operativo un conector sin autorizacion."""

from __future__ import annotations

import pytest

from gongora.adapters.tiktok.display_gateway import (
    NOT_IMPLEMENTED_CAPABILITIES,
    REQUIRED_SCOPES,
    TikTokDisplayGateway,
)
from gongora.adapters.tiktok.http_client import TikTokHttpClient, validate_tiktok_url
from gongora.domain.errors import (
    ConnectorNotAuthorized,
    PermissionMissingError,
    TokenRejectedError,
    UnsafePaginationError,
)
from gongora.domain.metrics import plan_for, spec_for
from gongora.domain.platform import Platform
from tests.conftest import FakeResponse


def test_sin_credenciales_esta_pendiente_de_conexion():
    gateway = TikTokDisplayGateway(None)
    assert gateway.is_configured() is False
    estado = gateway.check_connection()
    assert estado.connected is False
    assert estado.token_state == "absent"
    assert "Pendiente de conexion" in estado.detail
    assert estado.verified_capabilities == ()


def test_sin_credenciales_no_devuelve_datos():
    gateway = TikTokDisplayGateway(None)
    for operacion in (gateway.fetch_profile,
                      lambda: list(gateway.iter_media(max_items=5))):
        with pytest.raises(ConnectorNotAuthorized) as exc:
            operacion()
        assert "pendiente de conexion" in str(exc.value)
        assert exc.value.next_step


def test_la_publicacion_no_se_marca_como_operativa():
    estado = TikTokDisplayGateway(None).check_connection()
    assert "publicar_contenido" in estado.pending_capabilities
    assert "publicar_contenido" in NOT_IMPLEMENTED_CAPABILITIES
    assert "publicar_contenido" not in estado.verified_capabilities


def test_declara_los_scopes_oficiales_de_la_display_api():
    assert REQUIRED_SCOPES == ("user.info.basic", "user.info.stats", "video.list")


def test_las_metricas_de_tiktok_conservan_su_nombre_original():
    plan = plan_for(Platform.TIKTOK)
    assert plan.core == ("view_count", "like_count", "comment_count", "share_count")
    # Y no se confunden con las de Instagram.
    assert spec_for(Platform.TIKTOK, "view_count").name == "view_count"
    assert "no equivale a `views` de Instagram" in \
        spec_for(Platform.TIKTOK, "view_count").description


def test_rechaza_hosts_que_imitan_al_oficial():
    for url in ("https://open.tiktokapis.com.evil.net/v2/x",
                "http://open.tiktokapis.com/v2/x",
                "https://otro.test/v2/video/list/"):
        with pytest.raises(UnsafePaginationError):
            validate_tiktok_url(url)


def test_clasifica_el_sobre_de_error_de_tiktok():
    """TikTok devuelve HTTP 200 con error.code: hay que mirar el sobre."""
    class Opener:
        def __init__(self, payload):
            self.payload = payload
        def open(self, request, timeout=None):
            return FakeResponse(self.payload)

    invalido = {"error": {"code": "access_token_invalid", "message": "malo"}}
    cliente = TikTokHttpClient("token-falso", opener=Opener(invalido),
                               sleep=lambda _: None)
    with pytest.raises(TokenRejectedError):
        cliente.get("user/info", {"fields": "open_id"})
    assert cliente.token_rejected is True

    sin_scope = {"error": {"code": "scope_not_authorized", "message": "falta scope"}}
    cliente2 = TikTokHttpClient("token-falso", opener=Opener(sin_scope),
                                sleep=lambda _: None)
    with pytest.raises(PermissionMissingError):
        cliente2.get("video/list", {"fields": "id"})


def test_el_sobre_correcto_devuelve_datos():
    class Opener:
        def open(self, request, timeout=None):
            return FakeResponse({"data": {"videos": [{"id": "7"}], "has_more": False},
                                 "error": {"code": "ok", "message": "",
                                           "log_id": "abc"}})
    cliente = TikTokHttpClient("token-falso", opener=Opener(), sleep=lambda _: None)
    respuesta = cliente.post("video/list", params={"fields": "id"},
                             body={"max_count": 20})
    assert respuesta.data["videos"] == [{"id": "7"}]
    assert respuesta.log_id == "abc"


def test_faltan_scopes_declarados_no_se_da_por_conectado():
    from gongora.config import TikTokCredentials
    creds = TikTokCredentials(client_key="k", access_token="token-falso-largo",
                              scopes=("user.info.basic",))
    gateway = TikTokDisplayGateway(creds, client=None)
    estado = gateway.check_connection()
    assert estado.connected is False
    assert "faltan scopes" in estado.detail
    assert "video.list" in estado.detail
