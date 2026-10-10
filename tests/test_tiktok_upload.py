"""Riesgos: publicar en vez de dejar borrador, gastar subidas con reintentos y
enviar el video a un host que no es de TikTok. Sin red."""

from __future__ import annotations

from pathlib import Path

import pytest

from gongora.adapters.tiktok.http_client import TikTokResponse
from gongora.adapters.tiktok.upload import (
    INIT_PATH,
    MB,
    STATUS_PATH,
    TikTokDraftUploader,
    plan_chunks,
    validate_upload_url,
)
from gongora.domain.errors import MetaApiError, UnsafePaginationError

UPLOAD_URL = "https://open-upload.tiktokapis.com/upload/?upload_id=1&upload_token=x"


def test_un_video_pequeno_va_en_un_solo_trozo():
    plan = plan_chunks(3 * MB)
    assert (plan.chunk_size, plan.total_chunk_count) == (3 * MB, 1)
    assert plan.ranges == ((0, 3 * MB - 1),)


def test_un_video_grande_se_trocea_y_el_ultimo_absorbe_el_resto():
    size = 100 * MB + 123
    plan = plan_chunks(size, chunk=32 * MB)
    assert plan.total_chunk_count == size // (32 * MB) == 3
    assert plan.ranges[0] == (0, 32 * MB - 1)
    assert plan.ranges[-1] == (64 * MB, size - 1)
    assert sum(b - a + 1 for a, b in plan.ranges) == size
    assert plan.ranges[-1][1] - plan.ranges[-1][0] + 1 <= 128 * MB


@pytest.mark.parametrize("url", [
    "http://open-upload.tiktokapis.com/x",
    "https://evil.example.com/x",
    "https://tiktokapis.com.evil.com/x",
])
def test_solo_se_sube_a_hosts_de_tiktok_por_https(url):
    with pytest.raises(UnsafePaginationError):
        validate_upload_url(url)
    assert validate_upload_url(UPLOAD_URL) == UPLOAD_URL


class FakeClient:
    def __init__(self, statuses):
        self.calls = []
        self._statuses = list(statuses)

    def post(self, path, *, params=None, body=None, max_attempts=None):
        self.calls.append((path, body, max_attempts))
        if path == INIT_PATH:
            return TikTokResponse(200, {"publish_id": "v_inbox_1", "upload_url": UPLOAD_URL},
                                  f"/v2/{path}")
        assert path == STATUS_PATH
        return TikTokResponse(200, {"status": self._statuses.pop(0)}, f"/v2/{path}")


class FakePutResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, status=201):
        self.requests = []
        self._status = status

    def open(self, request, timeout=None):
        self.requests.append(request)
        return FakePutResponse(self._status)


def _video(tmp_path: Path, size: int = 1000) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * size)
    return path


def test_sube_como_borrador_sin_reintentar_el_inicio(tmp_path):
    client = FakeClient(["PROCESSING_UPLOAD", "SEND_TO_USER_INBOX"])
    opener = FakeOpener()
    result = TikTokDraftUploader(client, opener=opener, sleep=lambda _s: None).upload(
        _video(tmp_path))

    assert result.status == "SEND_TO_USER_INBOX"
    assert result.publish_id == "v_inbox_1"
    path, body, attempts = client.calls[0]
    # Flujo de bandeja (borrador), nunca Direct Post, y sin reintentos.
    assert path == "post/publish/inbox/video/init"
    assert attempts == 1
    assert "post_info" not in body
    assert body["source_info"] == {"source": "FILE_UPLOAD", "video_size": 1000,
                                   "chunk_size": 1000, "total_chunk_count": 1}
    put = opener.requests[0]
    assert put.get_method() == "PUT"
    assert put.get_header("Content-range") == "bytes 0-999/1000"
    assert put.get_header("Authorization") is None


def test_un_fallo_de_tiktok_se_informa_tal_cual(tmp_path):
    client = FakeClient(["FAILED"])
    result = TikTokDraftUploader(client, opener=FakeOpener(),
                                 sleep=lambda _s: None).upload(_video(tmp_path))
    assert result.status == "FAILED"


def test_la_publicacion_directa_lleva_descripcion_y_privacidad(tmp_path):
    from gongora.adapters.tiktok.upload import DIRECT_INIT_PATH

    class DirectClient(FakeClient):
        def post(self, path, *, params=None, body=None, max_attempts=None):
            if path == DIRECT_INIT_PATH:
                self.calls.append((path, body, max_attempts))
                return TikTokResponse(200, {"publish_id": "v_pub_1", "upload_url": UPLOAD_URL},
                                      f"/v2/{path}")
            return super().post(path, params=params, body=body, max_attempts=max_attempts)

    client = DirectClient(["PUBLISH_COMPLETE"])
    info = {"title": "Hola #gongora", "privacy_level": "SELF_ONLY"}
    result = TikTokDraftUploader(client, opener=FakeOpener(), sleep=lambda _s: None).upload(
        _video(tmp_path), post_info=info)
    path, body, attempts = client.calls[0]
    assert path == "post/publish/video/init"
    assert attempts == 1
    assert body["post_info"] == info
    assert result.status == "PUBLISH_COMPLETE"


def test_una_descripcion_demasiado_larga_no_se_envia(tmp_path):
    client = FakeClient([])
    with pytest.raises(ValueError):
        TikTokDraftUploader(client, opener=FakeOpener()).upload(
            _video(tmp_path), post_info={"title": "x" * 2201})
    assert client.calls == []


def test_un_trozo_rechazado_es_un_error(tmp_path):
    with pytest.raises(MetaApiError):
        TikTokDraftUploader(FakeClient([]), opener=FakeOpener(status=500),
                            sleep=lambda _s: None).upload(_video(tmp_path))
