"""Riesgo: filtracion de secretos."""

from __future__ import annotations

import json

from gongora import redaction
from tests.conftest import FAKE_TOKEN


def test_redacta_token_registrado():
    texto = f"fallo con el token {FAKE_TOKEN} en la peticion"
    assert FAKE_TOKEN not in redaction.redact(texto)
    assert redaction.MASK in redaction.redact(texto)


def test_redacta_access_token_en_query():
    url = "https://graph.facebook.com/v26.0/123/media?access_token=CUALQUIERCOSA&limit=2"
    salida = redaction.redact(url)
    assert "CUALQUIERCOSA" not in salida
    assert "limit=2" in salida


def test_redacta_token_con_forma_de_meta_sin_registrar():
    # Un token que nunca registramos, pero tiene la forma EAA...
    desconocido = "EAAotroTokenQueNadieRegistro1234567890"
    assert desconocido not in redaction.redact(f"error: {desconocido}")


def test_redacta_claves_sensibles_por_nombre():
    datos = {"Authorization": "Bearer lo-que-sea", "client_secret": "x",
             "normal": "visible"}
    salida = redaction.redact_value(datos)
    assert salida["Authorization"] == redaction.MASK
    assert salida["client_secret"] == redaction.MASK
    assert salida["normal"] == "visible"


def test_redacta_estructuras_anidadas():
    datos = {"data": [{"paging": {"next": f"https://x?access_token={FAKE_TOKEN}"}}]}
    serializado = json.dumps(redaction.redact_value(datos))
    assert FAKE_TOKEN not in serializado


def test_contains_secret_detecta_y_descarta():
    assert redaction.contains_secret({"nota": f"mira {FAKE_TOKEN}"}) is True
    assert redaction.contains_secret({"nota": "todo limpio"}) is False


def test_redaccion_resiste_estructuras_ciclicas():
    bucle: dict = {}
    bucle["self"] = bucle
    # No debe desbordar la pila: hay cortafuegos de profundidad.
    assert redaction.redact_value(bucle) is not None
