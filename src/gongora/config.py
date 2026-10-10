"""Carga de configuracion y credenciales.

El token se lee programaticamente del fichero de secretos, se registra en el
modulo de redaccion y nunca se expone en repr, logs ni informes.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from gongora import redaction

DEFAULT_SECRETS_FILE = Path.home() / ".config" / "gongora" / "secrets.env"
#: Credenciales de TikTok, separadas de las de Meta a proposito.
DEFAULT_TIKTOK_SECRETS_FILE = Path.home() / ".config" / "gongora" / "tiktok.env"
DEFAULT_GRAPH_VERSION = "v26.0"
EXPECTED_AUTH_FLOW = "facebook_login"

#: Flujo de autenticacion que este sistema NO usa. Si aparece en la config
#: avisamos, porque implica permisos y endpoints distintos.
FORBIDDEN_AUTH_HINTS = ("instagram_login", "instagram_business_login")

REQUIRED_KEYS = (
    "META_ACCESS_TOKEN",
    "META_GRAPH_VERSION",
    "META_PAGE_ID",
    "INSTAGRAM_ACCOUNT_ID",
)

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class ConfigError(RuntimeError):
    """Problema de configuracion resoluble por la persona que opera el sistema."""


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    # Comentario al final de linea solo si no estaba entrecomillado.
    return value.split(" #", 1)[0].strip()


def parse_env_file(path: Path) -> dict[str, str]:
    """Parsea un fichero clave=valor. No registra ni devuelve nada por stdout."""
    data: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE.match(line)
        if match:
            data[match.group(1)] = _unquote(match.group(2))
    return data


@dataclass(frozen=True)
class MetaCredentials:
    """Credenciales de la Graph API. `token` esta fuera de repr a proposito."""

    token: str = field(repr=False)
    graph_version: str
    auth_flow: str
    page_id: str
    instagram_account_id: str

    def __post_init__(self) -> None:
        redaction.register_secret(self.token)

    @property
    def token_fingerprint(self) -> str:
        """Identificador estable y no reversible, para distinguir tokens en logs."""
        import hashlib

        return hashlib.sha256(self.token.encode()).hexdigest()[:12]

    @property
    def token_length(self) -> int:
        return len(self.token)


TIKTOK_REQUIRED_KEYS = ("TIKTOK_CLIENT_KEY", "TIKTOK_ACCESS_TOKEN")
TIKTOK_SECRET_KEYS = ("TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_REFRESH_TOKEN")

#: Redirect URI registrada en TikTok for Developers (Login Kit, Desktop).
DEFAULT_TIKTOK_REDIRECT_URI = "http://localhost:3455/callback/"
#: Scopes que se piden en el login. `video.upload` (borrador) y `video.publish`
#: (publicacion directa) solo se usan con confirmacion explicita por video
#: (TikTokService.upload_draft / publish).
DEFAULT_TIKTOK_SCOPES = (
    "user.info.basic",
    "user.info.profile",
    "user.info.stats",
    "video.list",
    "video.upload",
    "video.publish",
)

#: Datos de la app (TikTok for Developers): el entorno del proceso manda sobre
#: el fichero, para poder inyectarlos sin escribirlos en disco.
_TIKTOK_APP_KEYS = ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REDIRECT_URI")
#: Tokens: los gestiona el sistema (login y refresco los reescriben en el
#: fichero). El fichero manda; el entorno solo rellena huecos. Si el entorno
#: mandase, un token refrescado quedaria tapado por uno caducado.
_TIKTOK_TOKEN_KEYS = (
    "TIKTOK_ACCESS_TOKEN", "TIKTOK_REFRESH_TOKEN", "TIKTOK_OPEN_ID", "TIKTOK_SCOPES",
    "TIKTOK_ACCESS_TOKEN_EXPIRES_AT", "TIKTOK_REFRESH_TOKEN_EXPIRES_AT",
)


def _parse_scopes(raw: str | None) -> tuple[str, ...]:
    return tuple(s.strip() for s in (raw or "").replace(",", " ").split() if s.strip())


def _parse_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class TikTokCredentials:
    """Credenciales de TikTok. Fichero y ciclo de vida independientes de Meta."""

    client_key: str
    access_token: str = field(repr=False)
    client_secret: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    open_id: str | None = None
    scopes: tuple[str, ...] = ()
    #: Caducidades en UTC. None si no constan (p. ej. token puesto a mano).
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for secret in (self.access_token, self.client_secret, self.refresh_token):
            redaction.register_secret(secret)

    @property
    def can_refresh(self) -> bool:
        return bool(self.client_secret and self.refresh_token)


@dataclass(frozen=True)
class TikTokAppConfig:
    """Lo necesario para iniciar el OAuth, antes de que exista ningun token."""

    client_key: str | None
    client_secret: str | None = field(repr=False)
    redirect_uri: str
    secrets_file: Path
    #: variable -> "entorno" | "fichero". Nunca contiene valores.
    sources: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        redaction.register_secret(self.client_secret)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(name for name, value in (("TIKTOK_CLIENT_KEY", self.client_key),
                                              ("TIKTOK_CLIENT_SECRET", self.client_secret))
                     if not value)


def resolve_tiktok_secrets_file() -> Path:
    env_file = os.environ.get("GONGORA_TIKTOK_SECRETS_FILE")
    if env_file:
        return Path(env_file).expanduser()
    return DEFAULT_TIKTOK_SECRETS_FILE


def _tiktok_values(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Fusiona fichero y entorno segun la precedencia de cada clave.

    Devuelve (valores, origen por clave). Solo lee claves TIKTOK_* concretas:
    nunca recorre ni vuelca el entorno completo.
    """
    data = parse_env_file(path) if path.is_file() else {}
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for key in _TIKTOK_APP_KEYS:
        env_value = os.environ.get(key, "").strip()
        if env_value:
            values[key], sources[key] = env_value, "entorno"
        elif data.get(key):
            values[key], sources[key] = data[key], "fichero"
    for key in _TIKTOK_TOKEN_KEYS:
        env_value = os.environ.get(key, "").strip()
        if data.get(key):
            values[key], sources[key] = data[key], "fichero"
        elif env_value:
            values[key], sources[key] = env_value, "entorno"
    return values, sources


def load_tiktok_app_config(path: Path | None = None) -> TikTokAppConfig:
    """Configuracion de la app de TikTok. No exige tokens ni hace red."""
    target = (path or resolve_tiktok_secrets_file()).expanduser()
    values, sources = _tiktok_values(target)
    return TikTokAppConfig(
        client_key=values.get("TIKTOK_CLIENT_KEY") or None,
        client_secret=values.get("TIKTOK_CLIENT_SECRET") or None,
        redirect_uri=values.get("TIKTOK_REDIRECT_URI") or DEFAULT_TIKTOK_REDIRECT_URI,
        secrets_file=target,
        sources=sources,
    )


def load_tiktok_credentials(path: Path | None = None) -> TikTokCredentials | None:
    """Carga las credenciales de TikTok si existen.

    Devuelve None cuando faltan client key o access token: el conector se
    muestra entonces como "pendiente de conexion", nunca como operativo.
    """
    target = (path or resolve_tiktok_secrets_file()).expanduser()
    data, _ = _tiktok_values(target)
    if any(not data.get(key) for key in TIKTOK_REQUIRED_KEYS):
        return None
    return TikTokCredentials(
        client_key=data["TIKTOK_CLIENT_KEY"],
        access_token=data["TIKTOK_ACCESS_TOKEN"],
        client_secret=data.get("TIKTOK_CLIENT_SECRET") or None,
        refresh_token=data.get("TIKTOK_REFRESH_TOKEN") or None,
        open_id=data.get("TIKTOK_OPEN_ID") or None,
        scopes=_parse_scopes(data.get("TIKTOK_SCOPES")),
        access_token_expires_at=_parse_utc(data.get("TIKTOK_ACCESS_TOKEN_EXPIRES_AT")),
        refresh_token_expires_at=_parse_utc(data.get("TIKTOK_REFRESH_TOKEN_EXPIRES_AT")),
    )


@dataclass(frozen=True)
class Paths:
    home: Path
    db: Path
    reports: Path
    raw: Path
    logs: Path
    lock: Path

    def ensure(self) -> None:
        for directory in (self.home, self.reports, self.raw, self.logs, self.db.parent):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    credentials: MetaCredentials
    paths: Paths
    secrets_file: Path
    secrets_file_mode: str
    tiktok: TikTokCredentials | None = None
    tiktok_secrets_file: Path | None = None
    timezone: str = "Europe/Madrid"
    #: Minimo entre recogidas reales; protege de rafagas tras un apagado largo.
    min_collection_interval_seconds: int = 2 * 3600
    http_timeout_seconds: float = 20.0
    http_max_attempts: int = 4
    #: Maximo de publicaciones a recorrer en una ejecucion (cortafuegos de cuota).
    media_page_size: int = 25
    media_max_items: int = 200


def _project_root() -> Path | None:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def resolve_home() -> Path:
    """Directorio de datos locales: GONGORA_HOME, o <proyecto>/var, o XDG."""
    env_home = os.environ.get("GONGORA_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    root = _project_root()
    if root is not None:
        return root / "var"
    return Path.home() / ".local" / "share" / "gongora"


def resolve_secrets_file() -> Path:
    env_file = os.environ.get("GONGORA_SECRETS_FILE")
    if env_file:
        return Path(env_file).expanduser()
    return DEFAULT_SECRETS_FILE


def build_paths(home: Path | None = None) -> Paths:
    base = home or resolve_home()
    return Paths(
        home=base,
        db=base / "gongora.sqlite3",
        reports=base / "reports",
        raw=base / "raw",
        logs=base / "logs",
        lock=base / "collect.lock",
    )


def load_settings(*, secrets_file: Path | None = None, home: Path | None = None) -> Settings:
    """Carga configuracion completa. Lanza ConfigError con mensajes accionables."""
    path = (secrets_file or resolve_secrets_file()).expanduser()
    if not path.is_file():
        raise ConfigError(
            f"No existe el fichero de secretos: {path}. "
            "Crealo a partir de .env.example y protegelo con chmod 600."
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    data = parse_env_file(path)

    missing = [key for key in REQUIRED_KEYS if not data.get(key)]
    if missing:
        raise ConfigError(
            "Faltan variables en el fichero de secretos: " + ", ".join(missing)
        )

    auth_flow = data.get("META_AUTH_FLOW", EXPECTED_AUTH_FLOW).strip().lower()
    for hint in FORBIDDEN_AUTH_HINTS:
        if hint in auth_flow:
            raise ConfigError(
                f"META_AUTH_FLOW={auth_flow!r} apunta a Instagram Login. "
                "Este sistema usa Instagram API with Facebook Login sobre "
                "graph.facebook.com; los permisos y endpoints no son compatibles."
            )

    credentials = MetaCredentials(
        token=data["META_ACCESS_TOKEN"],
        graph_version=data.get("META_GRAPH_VERSION") or DEFAULT_GRAPH_VERSION,
        auth_flow=auth_flow or EXPECTED_AUTH_FLOW,
        page_id=data["META_PAGE_ID"],
        instagram_account_id=data["INSTAGRAM_ACCOUNT_ID"],
    )
    paths = build_paths(home)
    paths.ensure()
    tiktok_file = resolve_tiktok_secrets_file()
    return Settings(
        credentials=credentials,
        paths=paths,
        secrets_file=path,
        secrets_file_mode=format(mode, "04o"),
        tiktok=load_tiktok_credentials(tiktok_file),
        tiktok_secrets_file=tiktok_file,
    )
