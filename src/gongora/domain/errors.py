"""Taxonomia de errores.

Todos los mensajes que salen de aqui pasan por redaccion antes de almacenarse
o mostrarse. La clasificacion determina si se reintenta y si el estado del
token debe marcarse como rechazado.
"""

from __future__ import annotations

from dataclasses import dataclass

from gongora import redaction


class GongoraError(RuntimeError):
    """Raiz de los errores propios."""

    @property
    def safe_message(self) -> str:
        return redaction.redact(str(self))


@dataclass(frozen=True)
class ApiErrorDetail:
    """Detalle saneado de un error de la Graph API."""

    http_status: int | None
    code: int | None
    subcode: int | None
    error_type: str | None
    message: str
    endpoint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", redaction.redact(self.message))
        object.__setattr__(self, "endpoint", redaction.redact(self.endpoint))

    def as_dict(self) -> dict[str, object]:
        return {
            "http_status": self.http_status,
            "code": self.code,
            "subcode": self.subcode,
            "type": self.error_type,
            "message": self.message,
            "endpoint": self.endpoint,
        }


class MetaApiError(GongoraError):
    """Error devuelto por Meta o por el transporte hacia Meta."""

    retryable = False

    def __init__(self, detail: ApiErrorDetail) -> None:
        super().__init__(f"[{detail.endpoint}] {detail.message}")
        self.detail = detail


class TransientApiError(MetaApiError):
    """Fallo pasajero (5xx, timeout, corte de conexion). Se reintenta."""

    retryable = True


class RateLimitedError(TransientApiError):
    """Limite de uso de Meta alcanzado. Se reintenta con backoff mayor."""

    def __init__(self, detail: ApiErrorDetail, retry_after_seconds: float | None = None) -> None:
        super().__init__(detail)
        self.retry_after_seconds = retry_after_seconds


class TokenRejectedError(MetaApiError):
    """Token invalido, caducado o sin permisos. Nunca se reintenta."""

    retryable = False


class PermissionMissingError(MetaApiError):
    """La capacidad existe en la API pero el token no tiene el permiso."""

    retryable = False


class MetricUnsupportedError(MetaApiError):
    """La metrica no esta admitida para este tipo de publicacion."""

    retryable = False


class UnsafePaginationError(GongoraError):
    """Una URL de paginacion apunta fuera de los hosts permitidos."""


class ConfigurationProblem(GongoraError):
    """Configuracion incompleta o incoherente."""


class ConnectorNotAuthorized(GongoraError):
    """El conector de una plataforma existe pero no tiene autorizacion todavia.

    No es un fallo: es el estado "pendiente de conexion". Nunca se sustituye
    por datos simulados.
    """

    def __init__(self, platform: str, next_step: str) -> None:
        super().__init__(
            f"{platform}: pendiente de conexion. Siguiente paso: {next_step}"
        )
        self.platform = platform
        self.next_step = next_step
