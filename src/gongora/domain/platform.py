"""Plataformas sociales soportadas.

Cada cuenta, publicacion, snapshot y tarea lleva su plataforma. No existe un
modelo "unificado" de metricas: los nombres y definiciones de cada red se
conservan tal cual los publica su API.
"""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"

    @property
    def label(self) -> str:
        return {"instagram": "Instagram", "tiktok": "TikTok"}[self.value]

    @classmethod
    def parse(cls, value: str) -> "Platform":
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            valid = ", ".join(p.value for p in cls)
            raise ValueError(f"Plataforma desconocida: {value!r}. Validas: {valid}") from exc


ALL_PLATFORMS: tuple[Platform, ...] = (Platform.INSTAGRAM, Platform.TIKTOK)
