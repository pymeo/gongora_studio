"""Riesgo: token rechazado, paginacion insegura, reintentos sin control."""

from __future__ import annotations

import pytest

from gongora.adapters.meta.http_client import GraphHttpClient, sanitize_graph_url
from gongora.domain.errors import (
    MetricUnsupportedError,
    PermissionMissingError,
    RateLimitedError,
    TokenRejectedError,
    TransientApiError,
    UnsafePaginationError,
)
from tests.conftest import FAKE_TOKEN, FakeOpener, http_error


def _cliente(credentials, routes, **kwargs):
    dormidas: list[float] = []
    cliente = GraphHttpClient(credentials, timeout=1.0, opener=FakeOpener(routes),
                              sleep=dormidas.append, **kwargs)
    return cliente, dormidas


def test_el_token_viaja_en_la_cabecera_authorization(credentials):
    cliente, _ = _cliente(credentials, {"/me/accounts": {"data": []}})
    cliente.get("me/accounts")
    opener = cliente._opener
    assert opener.header_of(0, "Authorization") == f"Bearer {FAKE_TOKEN}"
    # Y nunca en la query.
    assert "access_token" not in opener.requests[0][0]


def test_token_rechazado_no_se_reintenta(credentials):
    error = http_error(400, {"error": {"code": 190, "type": "OAuthException",
                                       "message": "Session has expired"}})
    cliente, dormidas = _cliente(credentials, {"/me/accounts": error})
    with pytest.raises(TokenRejectedError):
        cliente.get("me/accounts")
    assert cliente.call_count == 1       # un solo intento
    assert dormidas == []                 # ningun backoff
    assert cliente.token_rejected is True


def test_tras_token_rechazado_corta_las_siguientes_llamadas(credentials):
    error = http_error(400, {"error": {"code": 190, "type": "OAuthException",
                                       "message": "expired"}})
    cliente, _ = _cliente(credentials, {"/": error})
    with pytest.raises(TokenRejectedError):
        cliente.get("me/accounts")
    llamadas = cliente.call_count
    with pytest.raises(TokenRejectedError):
        cliente.get("otra/cosa")
    assert cliente.call_count == llamadas  # no se llamo a la API otra vez


def test_permiso_ausente_no_se_confunde_con_token(credentials):
    error = http_error(403, {"error": {"code": 10, "type": "OAuthException",
                                       "message": "requires permission"}})
    cliente, _ = _cliente(credentials, {"/insights": error})
    with pytest.raises(PermissionMissingError):
        cliente.get("123/insights")
    assert cliente.token_rejected is False


def test_metrica_no_admitida_se_clasifica_aparte(credentials):
    error = http_error(400, {"error": {
        "code": 100, "type": "OAuthException",
        "message": "(#100) metric[0] must be one of the following values"}})
    cliente, _ = _cliente(credentials, {"/insights": error})
    with pytest.raises(MetricUnsupportedError):
        cliente.get("123/insights", {"metric": "inventada"})


def test_error_transitorio_se_reintenta_y_acaba_saliendo(credentials):
    rutas = {"/me/accounts": [http_error(503, {"error": {"message": "temporal"}}),
                              http_error(503, {"error": {"message": "temporal"}}),
                              {"data": [{"id": "1"}]}]}
    cliente, dormidas = _cliente(credentials, rutas, max_attempts=4)
    respuesta = cliente.get("me/accounts")
    assert respuesta.payload["data"] == [{"id": "1"}]
    assert cliente.call_count == 3
    assert len(dormidas) == 2


def test_reintentos_acotados(credentials):
    error = http_error(500, {"error": {"message": "siempre falla"}})
    cliente, dormidas = _cliente(credentials, {"/me/accounts": [error] * 10},
                                 max_attempts=3)
    with pytest.raises(TransientApiError):
        cliente.get("me/accounts")
    assert cliente.call_count == 3      # no mas de max_attempts
    assert len(dormidas) == 2


def test_limite_de_uso_respeta_retry_after(credentials):
    error = http_error(429, {"error": {"code": 4, "message": "rate limited"}},
                       headers={"Retry-After": "7"})
    cliente, dormidas = _cliente(credentials, {"/me/accounts": [error, {"data": []}]},
                                 max_attempts=3)
    cliente.get("me/accounts")
    assert dormidas and 7.0 <= dormidas[0] < 8.0


def test_rate_limit_persistente_lanza_su_error(credentials):
    error = http_error(429, {"error": {"code": 4, "message": "rate limited"}})
    cliente, _ = _cliente(credentials, {"/me/accounts": [error] * 5}, max_attempts=2)
    with pytest.raises(RateLimitedError):
        cliente.get("me/accounts")


def test_cuota_alta_provoca_espera_antes_de_seguir(credentials):
    from tests.conftest import FakeResponse
    alta = FakeResponse({"data": []},
                        headers={"x-app-usage": '{"call_count":95,"total_time":10,'
                                                '"total_cputime":10}'})
    cliente, dormidas = _cliente(credentials, {"/me/accounts": [alta, {"data": []}]})
    cliente.get("me/accounts")   # devuelve uso 95%
    assert dormidas == []
    cliente.get("me/accounts")   # antes de esta, debe esperar
    assert dormidas and dormidas[0] > 0


def test_el_mensaje_de_error_no_lleva_el_token(credentials):
    error = http_error(400, {"error": {"code": 190,
                                       "message": f"bad token {FAKE_TOKEN}"}})
    cliente, _ = _cliente(credentials, {"/me/accounts": error})
    with pytest.raises(TokenRejectedError) as exc:
        cliente.get("me/accounts")
    assert FAKE_TOKEN not in str(exc.value)
    assert FAKE_TOKEN not in exc.value.detail.message


@pytest.mark.parametrize("url", [
    "https://evil.example.com/v26.0/x?after=1",
    "https://graph.facebook.com.evil.net/v26.0/x",
    "http://graph.facebook.com/v26.0/x",
    "ftp://graph.facebook.com/v26.0/x",
])
def test_rechaza_urls_de_paginacion_no_permitidas(url):
    with pytest.raises(UnsafePaginationError):
        sanitize_graph_url(url)


def test_limpia_credenciales_de_la_url_de_paginacion():
    limpia = sanitize_graph_url(
        f"https://graph.facebook.com/v26.0/1/media?access_token={FAKE_TOKEN}&after=ABC")
    assert FAKE_TOKEN not in limpia
    assert "after=ABC" in limpia


def test_get_url_valida_el_host_antes_de_pedir(credentials):
    cliente, _ = _cliente(credentials, {"/media": {"data": []}})
    with pytest.raises(UnsafePaginationError):
        cliente.get_url("https://attacker.test/v26.0/1/media")
    assert cliente.call_count == 0  # no se hizo ninguna peticion
