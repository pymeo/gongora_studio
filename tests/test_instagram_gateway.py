"""Riesgo: paginacion, errores parciales y metricas ausentes."""

from __future__ import annotations

import pytest

from gongora.adapters.meta.http_client import GraphHttpClient
from gongora.adapters.meta.instagram_gateway import InstagramGateway
from gongora.domain.models import MediaItem
from gongora.domain.platform import Platform
from tests.conftest import FAKE_TOKEN, FakeOpener, http_error

PERFIL = {"id": "17841422599648154", "username": "gongora__oficial",
          "name": "GONGORA", "followers_count": 27, "media_count": 1}


def _insights(**metricas):
    return {"data": [{"name": nombre, "period": "lifetime",
                      "values": [{"value": valor}]}
                     for nombre, valor in metricas.items()]}


def _gateway(credentials, rutas, crudas=None):
    cliente = GraphHttpClient(credentials, timeout=1.0, opener=FakeOpener(rutas),
                              sleep=lambda _: None)
    sink = (lambda endpoint, payload: crudas.append((endpoint, payload))) if crudas is not None else None
    return InstagramGateway(credentials, client=cliente, page_size=2, raw_sink=sink)


def test_lee_el_perfil_con_sus_contadores(credentials):
    gateway = _gateway(credentials, {"17841422599648154?": PERFIL})
    perfil = gateway.fetch_profile()
    assert perfil.platform is Platform.INSTAGRAM
    assert perfil.username == "gongora__oficial"
    assert perfil.counters == {"followers_count": 27, "media_count": 1}
    assert perfil.followers == 27


def test_recorre_varias_paginas_validando_el_host(credentials):
    pagina1 = {
        "data": [{"id": "1", "media_product_type": "REELS", "timestamp": "2026-10-01T10:00:00+0000"},
                 {"id": "2", "media_product_type": "FEED", "timestamp": "2026-09-01T10:00:00+0000"}],
        "paging": {"next": f"https://graph.facebook.com/v26.0/x/media?after=AAA&access_token={FAKE_TOKEN}"},
    }
    pagina2 = {"data": [{"id": "3", "media_product_type": "FEED",
                         "timestamp": "2026-08-01T10:00:00+0000"}]}
    gateway = _gateway(credentials, {"after=AAA": pagina2, "/media": pagina1})
    ids = [m.media_id for m in gateway.iter_media(max_items=10)]
    assert ids == ["1", "2", "3"]


def test_la_paginacion_no_sigue_hosts_ajenos(credentials):
    from gongora.domain.errors import UnsafePaginationError
    pagina1 = {"data": [{"id": "1"}],
               "paging": {"next": "https://atacante.test/v26.0/x/media?after=AAA"}}
    gateway = _gateway(credentials, {"/media": pagina1})
    with pytest.raises(UnsafePaginationError):
        list(gateway.iter_media(max_items=10))


def test_respeta_el_tope_de_publicaciones(credentials):
    pagina = {"data": [{"id": str(i)} for i in range(5)],
              "paging": {"next": "https://graph.facebook.com/v26.0/x/media?after=AAA"}}
    gateway = _gateway(credentials, {"/media": pagina})
    assert len(list(gateway.iter_media(max_items=3))) == 3


def test_las_respuestas_crudas_no_guardan_urls_de_paginacion(credentials):
    crudas: list = []
    pagina = {"data": [{"id": "1"}],
              "paging": {"next": f"https://graph.facebook.com/v26.0/x?access_token={FAKE_TOKEN}",
                         "cursors": {"after": "QVFI"}}}
    gateway = _gateway(credentials, {"/media": pagina}, crudas=crudas)
    list(gateway.iter_media(max_items=1))
    endpoint, payload = crudas[0]
    assert "next" not in payload["paging"]
    assert payload["paging"]["cursors"]["after"] == "QVFI"
    assert FAKE_TOKEN not in str(payload)


def test_insights_del_reel_devuelve_el_nucleo(credentials):
    media = MediaItem(platform=Platform.INSTAGRAM, media_id="18095103203640376",
                      media_type="VIDEO", product_type="REELS", permalink=None,
                      published_at=None)
    nucleo = _insights(views=1403, reach=850, likes=20, comments=0, saved=1,
                       shares=9, total_interactions=36)
    gateway = _gateway(credentials, {
        "#nucleo": nucleo, "/insights": nucleo,
    })
    resultado = gateway.fetch_insights(media)
    assert resultado.values["views"] == (1403.0, "lifetime")
    assert resultado.values["total_interactions"] == (36.0, "lifetime")
    assert resultado.values["comments"] == (0.0, "lifetime")  # 0 real, no hueco


def test_degrada_a_metrica_por_metrica_si_la_peticion_conjunta_falla(credentials):
    """Si una metrica rompe la peticion, se conservan las que si funcionan."""
    media = MediaItem(platform=Platform.INSTAGRAM, media_id="1", media_type="VIDEO",
                      product_type="REELS", permalink=None, published_at=None)
    fallo = http_error(400, {"error": {"code": 100, "type": "OAuthException",
                                       "message": "(#100) metric must be one of"}})

    class RutasSelectivas(FakeOpener):
        def open(self, request, timeout=None):
            url = request.full_url
            self.requests.append((url, dict(request.headers)))
            if "metric=views%2Creach" in url or url.count("%2C") >= 2:
                raise fallo                      # peticion conjunta: rechazada
            if "metric=saved" in url:
                raise fallo                      # esta metrica concreta no existe
            nombre = url.split("metric=")[1].split("&")[0]
            from tests.conftest import FakeResponse
            return FakeResponse(_insights(**{nombre: 7}))

    cliente = GraphHttpClient(credentials, timeout=1.0,
                              opener=RutasSelectivas({}), sleep=lambda _: None)
    gateway = InstagramGateway(credentials, client=cliente, page_size=2)
    resultado = gateway.fetch_insights(media)
    assert resultado.values["views"] == (7.0, "lifetime")
    assert resultado.values["likes"] == (7.0, "lifetime")
    assert "saved" in resultado.gaps              # lo que falla queda como hueco
    assert "saved" not in resultado.values        # y NO como cero


def test_metrica_ausente_en_la_respuesta_es_hueco_no_cero(credentials):
    media = MediaItem(platform=Platform.INSTAGRAM, media_id="1", media_type="VIDEO",
                      product_type="REELS", permalink=None, published_at=None)
    # Meta devuelve solo 2 de las 7 del nucleo.
    gateway = _gateway(credentials, {"/insights": _insights(views=100, reach=50)})
    resultado = gateway.fetch_insights(media)
    assert set(resultado.values) == {"views", "reach"}
    for ausente in ("likes", "comments", "saved", "shares", "total_interactions"):
        assert ausente in resultado.gaps
        assert resultado.gaps[ausente][0] == "ausente_en_respuesta"
        assert "no es cero" in resultado.gaps[ausente][1]


def test_admite_la_forma_total_value(credentials):
    media = MediaItem(platform=Platform.INSTAGRAM, media_id="1", media_type="IMAGE",
                      product_type="FEED", permalink=None, published_at=None)
    payload = {"data": [{"name": "views", "period": "lifetime",
                         "total_value": {"value": 321}}]}
    gateway = _gateway(credentials, {"/insights": payload})
    resultado = gateway.fetch_insights(media)
    assert resultado.values["views"] == (321.0, "lifetime")


def test_comprueba_el_vinculo_pagina_instagram(credentials):
    """Si la pagina apunta a otra cuenta de Instagram, no se da por conectado."""
    pagina = {"id": "1327434437128534", "name": "GONGORA",
              "instagram_business_account": {"id": "otra-cuenta"}}
    gateway = _gateway(credentials, {"/1327434437128534?": pagina})
    estado = gateway.check_connection()
    assert estado.connected is False
    assert "vinculada a la cuenta de Instagram otra-cuenta" in estado.detail


def test_token_rechazado_deja_estado_visible(credentials):
    error = http_error(400, {"error": {"code": 190, "type": "OAuthException",
                                       "message": "Session has expired"}})
    gateway = _gateway(credentials, {"/1327434437128534?": error})
    estado = gateway.check_connection()
    assert estado.connected is False
    assert estado.token_state == "rejected"
    assert "rechazo el token" in estado.detail
    assert FAKE_TOKEN not in estado.detail
    # Las capacidades pendientes nunca se marcan como operativas.
    assert "publicar_contenido" in estado.pending_capabilities
    assert "publicar_contenido" not in estado.verified_capabilities


def test_paginas_vacias_con_next_no_provocan_bucle(credentials):
    """Defensa: una API que devuelva paginas vacias con enlace siguiente."""
    vacia = {"data": [],
             "paging": {"next": "https://graph.facebook.com/v26.0/x/media?after=AAA"}}
    gateway = _gateway(credentials, {"/media": vacia})
    assert list(gateway.iter_media(max_items=100)) == []
    # El tope de paginas corta el recorrido en lugar de girar sin fin.
    assert gateway.api_calls <= 41


def test_conexion_con_token_de_pagina_no_usa_me_accounts(credentials):
    """Caso real: con token de Pagina, /me/accounts no existe (#100).

    La comprobacion consulta la pagina configurada, que vale para token de
    usuario y de Pagina.
    """
    pagina = {"id": "1327434437128534", "name": "GONGORA",
              "instagram_business_account": {"id": "17841422599648154"}}
    perfil = {"id": "17841422599648154", "username": "gongora__oficial",
              "followers_count": 10, "media_count": 0}
    no_existe = http_error(400, {"error": {"code": 100, "type": "OAuthException",
                                           "message": "Tried accessing nonexisting field (accounts)"}})
    gateway = _gateway(credentials, {
        "/me/accounts": no_existe,
        "/1327434437128534?": pagina,
        "/17841422599648154/media": {"data": []},
        "/17841422599648154?": perfil,
    })
    estado = gateway.check_connection()
    assert estado.connected is True, estado.detail
    assert "leer_pagina" in estado.verified_capabilities
    assert "vinculo_pagina_instagram" in estado.verified_capabilities
