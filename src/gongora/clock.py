"""Reloj del sistema. Unico punto que llama a datetime.now()."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Madrid")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Reloj fijo para pruebas."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._instant = self._instant + timedelta(seconds=seconds)


def to_local(instant: datetime) -> datetime:
    """Convierte a Europe/Madrid para mostrar al usuario."""
    return instant.astimezone(LOCAL_TZ)


def iso_utc(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat(timespec="seconds")


def human_local(instant: datetime) -> str:
    return to_local(instant).strftime("%Y-%m-%d %H:%M %Z")
