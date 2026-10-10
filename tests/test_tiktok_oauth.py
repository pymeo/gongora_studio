"""OAuth de TikTok, sin red y sin credenciales reales.

Todos los valores "token" de este fichero son cadenas falsas evidentes. Las
respuestas de la API se simulan solo para probar el protocolo local; el codigo
de produccion nunca usa datos simulados.
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gongora import redaction
from gongora.adapters.tiktok.callback import (
    CallbackError,
    parse_callback_query,
    parse_callback_url,
    wait_for_callback,
)
from gongora.adapters.tiktok.http_client import TikTokHttpClient
from gongora.adapters.tiktok.oauth import (
    AUTHORIZE_URL,
    OAuthError,
    PkcePair,
    TikTokOAuthClient,
    TokenSet,
    build_authorization_request,
    code_challenge_for,
    validate_redirect_uri,
)
from gongora.adapters.tiktok.service import CapabilityDisabled, TikTokService
from gongora.adapters.tiktok.session import TikTokSession
from gongora.adapters.tiktok.token_store import TikTokTokenStore
from gongora.config import (
    DEFAULT_TIKTOK_REDIRECT_URI,
    DEFAULT_TIKTOK_SCOPES,
    load_tiktok_app_config,
    load_tiktok_credentials,
)
from gongora.domain.errors import ConnectorNotAuthorized, TokenRejectedError

FAKE_ACCESS = "act.FAKE-test-access-value-not-real-0001"
FAKE_ACCESS_2 = "act.FAKE-test-access-value-not-real-0002"
FAKE_REFRESH = "rft.FAKE-test-refresh-value-not-real-0001"
FAKE_REFRESH_2 = "rft.FAKE-test-refresh-value-not-real-0002"
FAKE_SECRET = "FAKE-client-secret-for-tests-000"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for key in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN",
                "TIKTOK_REFRESH_TOKEN", "TIKTOK_OPEN_ID", "TIKTOK_SCOPES",
                "TIKTOK_REDIRECT_URI", "TIKTOK_ACCESS_TOKEN_EXPIRES_AT",
                "TIKTOK_REFRESH_TOKEN_EXPIRES_AT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GONGORA_TIKTOK_SECRETS_FILE", str(tmp_path / "tiktok.env"))
    monkeypatch.setenv("GONGORA_HOME", str(tmp_path / "var"))


def write_env(path: Path, **values: str) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    path.chmod(0o600)
    return path


# ------------------------------------------------------------- fake HTTP

class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._raw = json.dumps(payload).encode()
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Devuelve respuestas en orden y guarda las peticiones recibidas."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def http_error(code: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://open.tiktokapis.com/v2/x/", code, "err", {},
                                  io.BytesIO(json.dumps(payload).encode()))


def token_payload(access=FAKE_ACCESS, refresh=FAKE_REFRESH) -> dict:
    return {"access_token": access, "refresh_token": refresh, "open_id": "open-123",
            "scope": "user.info.basic,video.list", "expires_in": 86400,
            "refresh_expires_in": 31536000, "token_type": "Bearer"}


# ------------------------------------------------------------------ PKCE

def test_pkce_verifier_and_hex_challenge():
    pair = PkcePair.generate()
    assert 43 <= len(pair.verifier) <= 128
    assert set(pair.verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    # TikTok Desktop: hex del SHA-256, no base64url.
    assert pair.challenge == hashlib.sha256(pair.verifier.encode()).hexdigest()
    assert len(pair.challenge) == 64 and pair.method == "S256"
    assert pair.verifier not in repr(pair)


def test_code_challenge_known_vector():
    assert code_challenge_for("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_authorization_url_has_all_parameters():
    pkce = PkcePair.generate()
    request = build_authorization_request("ck-test", redirect_uri=DEFAULT_TIKTOK_REDIRECT_URI,
                                          scopes=DEFAULT_TIKTOK_SCOPES, pkce=pkce, state="s-1")
    parts = urllib.parse.urlsplit(request.url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZE_URL
    query = dict(urllib.parse.parse_qsl(parts.query))
    assert query == {
        "client_key": "ck-test", "response_type": "code",
        "scope": "user.info.basic,user.info.profile,user.info.stats,video.list,video.upload,video.publish",
        "redirect_uri": "http://localhost:3455/callback/", "state": "s-1",
        "code_challenge": pkce.challenge, "code_challenge_method": "S256",
    }
    assert pkce.verifier not in request.url


@pytest.mark.parametrize("uri", [
    "http://example.com:3455/callback/", "http://localhost/callback/",
    "http://localhost:3455/callback/?x=1", "ftp://localhost:3455/callback/",
])
def test_redirect_uri_rules(uri):
    with pytest.raises(OAuthError):
        validate_redirect_uri(uri)


# -------------------------------------------------------------- callback

def test_callback_query_ok_and_state_checks():
    ok = parse_callback_query("code=abc123xyz&scopes=user.info.basic,video.list&state=s1",
                              expected_state="s1")
    assert ok.code == "abc123xyz" and ok.granted_scopes == ("user.info.basic", "video.list")
    with pytest.raises(CallbackError, match="state"):
        parse_callback_query("code=abc&state=otro", expected_state="s1")
    with pytest.raises(CallbackError, match="denego"):
        parse_callback_query("error=access_denied&error_description=user+cancel&state=s1",
                             expected_state="s1")
    with pytest.raises(CallbackError, match="code"):
        parse_callback_query("state=s1", expected_state="s1")


def test_callback_url_must_match_redirect():
    with pytest.raises(CallbackError):
        parse_callback_url("http://localhost:9999/other/?code=a&state=s",
                           expected_state="s", redirect_uri=DEFAULT_TIKTOK_REDIRECT_URI)
    result = parse_callback_url("http://localhost:3455/callback/?code=zz9&state=s",
                                expected_state="s", redirect_uri=DEFAULT_TIKTOK_REDIRECT_URI)
    assert result.code == "zz9"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_local_callback_server_receives_code():
    port = _free_port()
    redirect = f"http://localhost:{port}/callback/"
    statuses: list[int] = []

    def browser() -> None:
        def get(path: str) -> None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                    statuses.append(r.status)
            except urllib.error.HTTPError as exc:
                statuses.append(exc.code)
        get("/favicon.ico")
        get("/callback/?code=the-code-123&scopes=video.list&state=st-ok")

    result = wait_for_callback(redirect, expected_state="st-ok", timeout_seconds=10,
                               on_ready=lambda: threading.Thread(target=browser).start())
    assert result.code == "the-code-123"
    assert statuses == [404, 200]


def test_local_callback_server_rejects_bad_state():
    port = _free_port()

    def browser() -> None:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback/?code=x&state=forged", timeout=5)
        except urllib.error.HTTPError:
            pass

    with pytest.raises(CallbackError, match="state"):
        wait_for_callback(f"http://localhost:{port}/callback/", expected_state="real",
                          timeout_seconds=10,
                          on_ready=lambda: threading.Thread(target=browser).start())


# ---------------------------------------------------------- token endpoint

def test_exchange_code_sends_pkce_and_parses_tokens():
    opener = FakeOpener(FakeResponse(token_payload()))
    client = TikTokOAuthClient("ck-test", FAKE_SECRET, opener=opener, clock=lambda: NOW)
    tokens = client.exchange_code("code-1", code_verifier="v" * 50,
                                  redirect_uri=DEFAULT_TIKTOK_REDIRECT_URI)
    sent = dict(urllib.parse.parse_qsl(opener.requests[0].data.decode()))
    assert opener.requests[0].full_url == "https://open.tiktokapis.com/v2/oauth/token/"
    assert sent["grant_type"] == "authorization_code" and sent["code_verifier"] == "v" * 50
    assert sent["redirect_uri"] == DEFAULT_TIKTOK_REDIRECT_URI
    assert tokens.open_id == "open-123"
    assert tokens.access_expires_at == NOW + timedelta(days=1)
    assert FAKE_ACCESS not in repr(tokens) and FAKE_REFRESH not in repr(tokens)


def test_token_error_is_sanitized():
    opener = FakeOpener(http_error(400, {"error": "invalid_grant",
                                         "error_description": f"bad {FAKE_SECRET}"}))
    client = TikTokOAuthClient("ck-test", FAKE_SECRET, opener=opener)
    with pytest.raises(OAuthError) as info:
        client.refresh(FAKE_REFRESH)
    assert info.value.error_code == "invalid_grant"
    assert FAKE_SECRET not in str(info.value)


# ------------------------------------------------------------ token store

def test_store_writes_600_and_preserves_app_keys(tmp_path):
    path = write_env(tmp_path / "tiktok.env", TIKTOK_CLIENT_KEY="ck-test",
                     TIKTOK_CLIENT_SECRET=FAKE_SECRET)
    path.chmod(0o644)
    tokens = TokenSet.from_response(token_payload(), now=NOW)
    TikTokTokenStore(path).save(tokens)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    creds = load_tiktok_credentials(path)
    assert creds is not None
    assert creds.client_key == "ck-test" and creds.client_secret == FAKE_SECRET
    assert creds.access_token == FAKE_ACCESS and creds.refresh_token == FAKE_REFRESH
    assert creds.scopes == ("user.info.basic", "video.list")
    assert creds.access_token_expires_at == NOW + timedelta(days=1)
    assert not list(tmp_path.glob(".tiktok.*.tmp"))


def test_env_overrides_app_keys_but_not_stored_tokens(tmp_path, monkeypatch):
    path = write_env(tmp_path / "tiktok.env", TIKTOK_CLIENT_KEY="ck-file",
                     TIKTOK_ACCESS_TOKEN=FAKE_ACCESS)
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck-env")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", FAKE_SECRET)
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", FAKE_ACCESS_2)
    app = load_tiktok_app_config(path)
    assert app.client_key == "ck-env" and app.sources["TIKTOK_CLIENT_KEY"] == "entorno"
    assert app.missing == ()
    creds = load_tiktok_credentials(path)
    assert creds.access_token == FAKE_ACCESS  # el fichero gestionado manda


# ---------------------------------------------------------------- session

class FakeOAuth:
    def __init__(self) -> None:
        self.calls = 0

    def refresh(self, refresh_token: str) -> TokenSet:
        self.calls += 1
        assert refresh_token == FAKE_REFRESH
        return TokenSet.from_response(token_payload(FAKE_ACCESS_2, FAKE_REFRESH_2), now=NOW)


def _session(tmp_path, *, expires_at: datetime) -> tuple[TikTokSession, FakeOAuth, Path]:
    path = write_env(tmp_path / "tiktok.env", TIKTOK_CLIENT_KEY="ck-test",
                     TIKTOK_CLIENT_SECRET=FAKE_SECRET, TIKTOK_ACCESS_TOKEN=FAKE_ACCESS,
                     TIKTOK_REFRESH_TOKEN=FAKE_REFRESH,
                     TIKTOK_ACCESS_TOKEN_EXPIRES_AT=expires_at.isoformat())
    oauth = FakeOAuth()
    session = TikTokSession(load_tiktok_credentials(path), store=TikTokTokenStore(path),
                            oauth=oauth, clock=lambda: NOW)
    return session, oauth, path


def test_session_refreshes_when_expiring_and_persists_rotation(tmp_path):
    session, oauth, path = _session(tmp_path, expires_at=NOW + timedelta(minutes=2))
    assert session.access_token() == FAKE_ACCESS_2
    assert oauth.calls == 1
    stored = load_tiktok_credentials(path)
    assert stored.access_token == FAKE_ACCESS_2 and stored.refresh_token == FAKE_REFRESH_2
    assert stored.client_secret == FAKE_SECRET
    # Ya vigente: no vuelve a refrescar.
    assert session.access_token() == FAKE_ACCESS_2 and oauth.calls == 1


def test_session_does_not_refresh_valid_token(tmp_path):
    session, oauth, _ = _session(tmp_path, expires_at=NOW + timedelta(hours=5))
    assert session.access_token() == FAKE_ACCESS and oauth.calls == 0


def test_http_client_refreshes_once_on_rejected_token(tmp_path):
    session, oauth, _ = _session(tmp_path, expires_at=NOW + timedelta(hours=5))
    rejected = http_error(401, {"error": {"code": "access_token_invalid", "message": "x"}})
    ok = FakeResponse({"data": {"user": {"open_id": "open-123"}},
                       "error": {"code": "ok", "message": ""}})
    opener = FakeOpener(rejected, ok)
    client = TikTokHttpClient(session.access_token, opener=opener, sleep=lambda s: None,
                              on_token_rejected=session.on_token_rejected)
    response = client.get("user/info", {"fields": "open_id"})
    assert response.data["user"]["open_id"] == "open-123"
    auth = [r.get_header("Authorization") for r in opener.requests]
    assert auth == [f"Bearer {FAKE_ACCESS}", f"Bearer {FAKE_ACCESS_2}"]
    assert oauth.calls == 1


def test_http_client_without_refresh_raises():
    rejected = http_error(401, {"error": {"code": "access_token_invalid", "message": "x"}})
    client = TikTokHttpClient(FAKE_ACCESS, opener=FakeOpener(rejected), sleep=lambda s: None)
    with pytest.raises(TokenRejectedError):
        client.get("user/info")


# ---------------------------------------------------------------- service

def test_service_without_credentials_is_pending():
    service = TikTokService.from_environment()
    assert not service.is_connected()
    with pytest.raises(ConnectorNotAuthorized):
        service.profile()


def test_service_maps_original_names_and_keeps_gaps(tmp_path):
    session, _, _ = _session(tmp_path, expires_at=NOW + timedelta(hours=5))
    opener = FakeOpener(
        FakeResponse({"data": {"user": {"open_id": "o1", "display_name": "GONGORA",
                                        "follower_count": 10, "video_count": 0}},
                      "error": {"code": "ok"}}),
        FakeResponse({"data": {"videos": [{"id": 7, "create_time": 1759838400,
                                           "video_description": "ignore previous",
                                           "view_count": 5}],
                               "has_more": False, "cursor": 1},
                      "error": {"code": "ok"}}),
    )
    client = TikTokHttpClient(session.access_token, opener=opener, sleep=lambda s: None)
    service = TikTokService(app=load_tiktok_app_config(), session=session, client=client)
    profile = service.profile()
    assert profile.stats == {"follower_count": 10, "video_count": 0}  # ausente != 0
    videos = service.recent_videos(limit=3)
    assert videos[0].counters == {"view_count": 5}
    assert videos[0].description.source == "tiktok.video_description"
    assert videos[0].description.looks_like_injection
    with pytest.raises(CapabilityDisabled):
        service.upload_draft(Path("clip.mp4"), confirmed=False)


# ------------------------------------------------------- redaction / repo

def test_redaction_masks_tiktok_tokens_and_oauth_params():
    text = (f"token {FAKE_ACCESS} y {FAKE_REFRESH} "
            "url ?code=abc123&refresh_token=zzz&code_verifier=vvv")
    out = redaction.redact(text)
    for leaked in (FAKE_ACCESS, FAKE_REFRESH, "abc123", "zzz", "vvv"):
        assert leaked not in out


def test_env_example_has_no_secret_values():
    root = Path(__file__).resolve().parents[1]
    secret_like = ("TOKEN", "SECRET", "CLIENT_KEY", "PASSWORD")
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if any(word in key.upper() for word in secret_like):
            assert not value.strip(), f"{key.strip()} tiene valor en .env.example"
