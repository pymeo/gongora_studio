"""Catalogo de metricas por plataforma.

Tres reglas del dominio viven aqui:

1. Las metricas acumuladas (`cumulative`) NO se suman entre snapshots: se
   comparan. `summable_across_snapshots` lo deja explicito.
2. Una metrica ausente NO es cero. Si la API no la devuelve se registra como
   hueco (`MetricGap`).
3. Las metricas NO son equivalentes entre redes. `views` de Instagram y
   `view_count` de TikTok se almacenan y se leen por separado, con el nombre
   original de cada API. Nunca se agregan juntas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gongora.domain.platform import Platform

Aggregation = Literal["cumulative", "instant", "average"]


@dataclass(frozen=True)
class MetricSpec:
    platform: Platform
    name: str
    aggregation: Aggregation
    unit: str
    description: str

    @property
    def key(self) -> tuple[str, str]:
        return (str(self.platform), self.name)

    @property
    def summable_across_snapshots(self) -> bool:
        """Ningun snapshot acumulado o instantaneo se suma con otro."""
        return False


@dataclass(frozen=True)
class MetricPlan:
    """Que metricas pedir para una publicacion concreta y en que tandas.

    `core` va en una sola peticion (si falla, se degrada a metrica por metrica).
    `optional` va aparte: su fallo no debe arrastrar al nucleo.
    """

    platform: Platform
    core: tuple[str, ...]
    optional: tuple[str, ...] = ()

    @property
    def all_requested(self) -> tuple[str, ...]:
        return self.core + self.optional


# --------------------------------------------------------------------------
# Instagram  (Instagram API with Facebook Login, graph.facebook.com v26.0)
# --------------------------------------------------------------------------

IG = Platform.INSTAGRAM

#: Nucleo verificado contra la cuenta real (reel 18095103203640376).
IG_CORE_MEDIA_METRICS = (
    "views", "reach", "likes", "comments", "saved", "shares", "total_interactions",
)
#: Componentes que Meta expone por separado, frente al total que calcula Meta.
IG_INTERACTION_COMPONENTS = ("likes", "comments", "saved", "shares")
IG_INTERACTION_TOTAL = "total_interactions"

#: `views`, `likes` y `comments` son solo organicos: no cuentan la actividad de
#: anuncios con la publicacion. Los `total_*` si la incluyen. Solo existen en
#: Instagram API with Facebook Login. Verificado el 2026-10-09 contra el reel
#: 18095103203640376 (views=1808, total_views=5565 con promocion activa).
IG_TOTAL_METRICS = ("total_views", "total_likes", "total_comments")

IG_OPTIONAL_BY_PRODUCT_TYPE: dict[str, tuple[str, ...]] = {
    "REELS": ("ig_reels_avg_watch_time", "ig_reels_video_view_total_time") + IG_TOTAL_METRICS,
    "FEED": ("profile_visits", "follows") + IG_TOTAL_METRICS,
    "AD": (),
    "STORY": (),
}
IG_STORY_METRICS = ("views", "reach", "replies", "shares", "total_interactions", "navigation")

IG_PROFILE_FIELDS = ("id", "username", "name", "followers_count", "media_count")
IG_MEDIA_FIELDS = (
    "id", "caption", "media_type", "media_product_type", "permalink",
    "timestamp", "like_count", "comments_count",
)

# --------------------------------------------------------------------------
# TikTok  (Display API, open.tiktokapis.com v2) - PENDIENTE DE CONEXION
# Nombres de campo segun la Display API. No equivalen a los de Instagram.
# --------------------------------------------------------------------------

TT = Platform.TIKTOK

TT_VIDEO_COUNTERS = ("view_count", "like_count", "comment_count", "share_count")
TT_PROFILE_FIELDS = ("open_id", "union_id", "display_name", "follower_count",
                     "following_count", "likes_count", "video_count")
TT_VIDEO_FIELDS = ("id", "create_time", "video_description", "duration", "share_url",
                   "view_count", "like_count", "comment_count", "share_count")


_SPECS: dict[tuple[str, str], MetricSpec] = {}


def _register(*specs: MetricSpec) -> None:
    for spec in specs:
        _SPECS[spec.key] = spec


_register(
    # --- Instagram: insights de publicacion ---
    MetricSpec(IG, "views", "cumulative", "reproducciones",
               "Instagram: veces que se ha reproducido o mostrado, solo organico (sin "
               "anuncios). No son personas ni escuchas en Spotify."),
    MetricSpec(IG, "reach", "cumulative", "cuentas",
               "Instagram: cuentas unicas alcanzadas, segun Meta."),
    MetricSpec(IG, "likes", "cumulative", "cuenta",
               "Instagram: me gusta acumulados, solo organicos (sin anuncios)."),
    MetricSpec(IG, "comments", "cumulative", "cuenta",
               "Instagram: comentarios acumulados, solo organicos (sin anuncios)."),
    MetricSpec(IG, "total_views", "cumulative", "reproducciones",
               "Instagram: reproducciones incluida la actividad de anuncios y "
               "promociones con la publicacion. No son personas."),
    MetricSpec(IG, "total_likes", "cumulative", "cuenta",
               "Instagram: me gusta incluida la actividad de anuncios."),
    MetricSpec(IG, "total_comments", "cumulative", "cuenta",
               "Instagram: comentarios incluida la actividad de anuncios."),
    MetricSpec(IG, "saved", "cumulative", "cuenta", "Instagram: guardados acumulados."),
    MetricSpec(IG, "shares", "cumulative", "cuenta", "Instagram: compartidos acumulados."),
    MetricSpec(IG, IG_INTERACTION_TOTAL, "cumulative", "cuenta",
               "Instagram: total de interacciones segun Meta (puede no coincidir con "
               "la suma de componentes)."),
    MetricSpec(IG, "ig_reels_avg_watch_time", "average", "milisegundos",
               "Instagram: tiempo medio de visualizacion del reel."),
    MetricSpec(IG, "ig_reels_video_view_total_time", "cumulative", "milisegundos",
               "Instagram: tiempo total acumulado de visualizacion del reel."),
    MetricSpec(IG, "replies", "cumulative", "cuenta", "Instagram: respuestas (historias)."),
    MetricSpec(IG, "navigation", "cumulative", "cuenta", "Instagram: navegacion (historias)."),
    MetricSpec(IG, "profile_visits", "cumulative", "cuenta",
               "Instagram: visitas al perfil atribuidas a la publicacion."),
    MetricSpec(IG, "follows", "cumulative", "cuenta",
               "Instagram: seguimientos atribuidos a la publicacion."),
    # --- Instagram: campos de perfil (no vienen de /insights) ---
    MetricSpec(IG, "followers_count", "instant", "cuentas",
               "Instagram: seguidores en el momento de la lectura."),
    MetricSpec(IG, "media_count", "instant", "publicaciones",
               "Instagram: publicaciones en el momento de la lectura."),
    # --- TikTok: contadores de video (Display API) ---
    MetricSpec(TT, "view_count", "cumulative", "reproducciones",
               "TikTok: reproducciones del video segun la Display API. Definicion "
               "propia de TikTok; no equivale a `views` de Instagram."),
    MetricSpec(TT, "like_count", "cumulative", "cuenta", "TikTok: me gusta del video."),
    MetricSpec(TT, "comment_count", "cumulative", "cuenta", "TikTok: comentarios del video."),
    MetricSpec(TT, "share_count", "cumulative", "cuenta", "TikTok: compartidos del video."),
    # --- TikTok: campos de perfil ---
    MetricSpec(TT, "follower_count", "instant", "cuentas", "TikTok: seguidores."),
    MetricSpec(TT, "following_count", "instant", "cuentas", "TikTok: cuentas seguidas."),
    MetricSpec(TT, "likes_count", "cumulative", "cuenta",
               "TikTok: me gusta totales recibidos por la cuenta."),
    MetricSpec(TT, "video_count", "instant", "videos", "TikTok: videos publicados."),
)


def spec_for(platform: Platform | str, metric: str) -> MetricSpec:
    """Spec conocida, o una generica si la API introduce una metrica nueva."""
    plat = Platform.parse(str(platform))
    found = _SPECS.get((str(plat), metric))
    if found is not None:
        return found
    return MetricSpec(plat, metric, "cumulative", "desconocida",
                      f"Metrica no catalogada de {plat.label}.")


def known_metrics(platform: Platform | str | None = None) -> tuple[str, ...]:
    if platform is None:
        return tuple(sorted(f"{p}:{m}" for p, m in _SPECS))
    plat = str(Platform.parse(str(platform)))
    return tuple(sorted(m for p, m in _SPECS if p == plat))


def plan_for(platform: Platform | str, product_type: str | None = None) -> MetricPlan:
    """Plan de metricas segun plataforma y tipo de publicacion."""
    plat = Platform.parse(str(platform))
    if plat is Platform.INSTAGRAM:
        product = (product_type or "FEED").upper()
        if product == "STORY":
            return MetricPlan(plat, core=IG_STORY_METRICS)
        return MetricPlan(plat, core=IG_CORE_MEDIA_METRICS,
                          optional=IG_OPTIONAL_BY_PRODUCT_TYPE.get(product, ()))
    # TikTok: los contadores llegan como campos del propio video, no de insights.
    return MetricPlan(plat, core=TT_VIDEO_COUNTERS)


def assert_comparable(a: MetricSpec, b: MetricSpec) -> None:
    """Impide comparar o agregar metricas de plataformas distintas."""
    if a.platform is not b.platform:
        raise ValueError(
            f"No se pueden comparar metricas de plataformas distintas: "
            f"{a.platform.label}.{a.name} vs {b.platform.label}.{b.name}. "
            "Sus definiciones no son equivalentes."
        )
