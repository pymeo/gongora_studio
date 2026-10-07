"""Caso de uso: recogida de datos de Faro.

Orquesta, por cada plataforma configurada: perfil -> publicaciones ->
insights -> snapshots -> calidad de datos. Un fallo en una publicacion o en
una metrica no cancela el resto: se registra como error parcial o hueco.

No consume ningun LLM. Es la ruta que ejecuta el temporizador.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from gongora.application.ports import SocialDataGateway
from gongora.domain import metrics as metrics_catalog
from gongora.domain.errors import (
    ConnectorNotAuthorized,
    GongoraError,
    MetaApiError,
    TokenRejectedError,
)
from gongora.domain.models import (
    CollectionRun,
    DataQualityIssue,
    MediaItem,
    MetricGap,
    MetricSnapshot,
)
from gongora.domain.platform import Platform
from gongora.logging_setup import get_logger

#: Periodo con el que se guardan los contadores de perfil (no son insights).
PROFILE_PERIOD = "instant"


@dataclass
class CollectionOptions:
    platforms: tuple[Platform, ...] | None = None
    max_media: int = 200
    force: bool = False
    trigger: str = "manual"


@dataclass
class CollectionReport:
    """Resultado de la ejecucion, para el CLI y el informe."""

    run: CollectionRun
    skipped_reason: str | None = None
    notes: list[str] = field(default_factory=list)


class CollectMetrics:
    def __init__(
        self,
        *,
        gateways: dict[Platform, SocialDataGateway],
        snapshots,
        runs,
        agent_state,
        clock,
        min_interval_seconds: int = 7200,
        on_run_start=None,
    ) -> None:
        self._gateways = gateways
        self._snapshots = snapshots
        self._runs = runs
        self._agent_state = agent_state
        self._clock = clock
        self._min_interval = min_interval_seconds
        #: Aviso de inicio de run: lo usa el contenedor para etiquetar las
        #: respuestas crudas sin que el gateway conozca el ciclo de vida.
        self._on_run_start = on_run_start
        self._log = get_logger("faro.collect")

    # ------------------------------------------------------------------ api

    def execute(self, options: CollectionOptions | None = None) -> CollectionReport:
        opts = options or CollectionOptions()
        now = self._clock.now()

        guard = self._too_soon(now, opts)
        if guard is not None:
            self._log.info("Recogida omitida: %s", guard)
            return CollectionReport(
                run=CollectionRun(run_id="", started_at=now, finished_at=now, status="ok"),
                skipped_reason=guard,
            )

        run = CollectionRun(run_id=f"run-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}",
                            started_at=now)
        self._runs.start(run, trigger=opts.trigger)
        if self._on_run_start is not None:
            self._on_run_start(run.run_id)
        self._log.info("Recogida %s iniciada (disparador=%s)", run.run_id, opts.trigger)

        targets = opts.platforms or tuple(self._gateways)
        for platform in targets:
            gateway = self._gateways.get(platform)
            if gateway is None:
                outcome = run.outcome(platform)
                outcome.status = "skipped"
                outcome.skipped_reason = "Sin adaptador registrado para esta plataforma."
                continue
            self._collect_platform(run, platform, gateway, opts)

        run.finished_at = self._clock.now()
        run.status = self._overall_status(run)
        self._runs.finish(run, summary={
            "platforms": {str(p): o.status for p, o in run.outcomes.items()},
            "snapshots": run.snapshots_written,
            "media": run.media_seen,
        })
        for platform, outcome in run.outcomes.items():
            self._agent_state.record(
                agent="faro", platform=platform, status=outcome.status,
                error=(outcome.errors[0].get("message") if outcome.errors else None),
            )
        self._agent_state.record(agent="faro", platform="*", status=run.status,
                                 error=(run.errors[0].get("message") if run.errors else None))
        self._log.info("Recogida %s terminada: %s (%s snapshots)",
                       run.run_id, run.status, run.snapshots_written)
        return CollectionReport(run=run)

    # ------------------------------------------------------------- interno

    def _too_soon(self, now, opts: CollectionOptions) -> str | None:
        """Evita una rafaga de recogidas tras un periodo apagado."""
        if opts.force:
            return None
        last = self._runs.last_successful()
        if last is None or not last["started_at"]:
            return None
        started = datetime.fromisoformat(last["started_at"])
        elapsed = (now - started).total_seconds()
        if elapsed < self._min_interval:
            remaining = int(self._min_interval - elapsed)
            return (
                f"la ultima recogida satisfactoria fue hace {int(elapsed // 60)} min "
                f"(minimo {self._min_interval // 60} min). Faltan {remaining // 60} min. "
                "Usa --force para forzarla."
            )
        return None

    def _collect_platform(self, run: CollectionRun, platform: Platform,
                          gateway: SocialDataGateway, opts: CollectionOptions) -> None:
        outcome = run.outcome(platform)

        if not gateway.is_configured():
            status = gateway.check_connection()
            outcome.status = "skipped"
            outcome.connected = False
            outcome.token_state = status.token_state  # type: ignore[assignment]
            outcome.skipped_reason = status.detail
            self._log.info("%s omitida: %s", platform.label, status.detail)
            return

        snapshots: list[MetricSnapshot] = []
        gaps: list[MetricGap] = []
        issues: list[DataQualityIssue] = []

        try:
            profile = gateway.fetch_profile()
            self._snapshots.save_profile(profile)
            outcome.connected = True
            outcome.token_state = "valid"
            for metric, value in profile.counters.items():
                snapshots.append(MetricSnapshot(
                    captured_at=profile.fetched_at, platform=platform,
                    account_id=profile.account_id, media_id=None, metric=metric,
                    period=PROFILE_PERIOD, value=float(value), run_id=run.run_id,
                ))
        except ConnectorNotAuthorized as exc:
            outcome.status = "skipped"
            outcome.skipped_reason = str(exc)
            outcome.token_state = "absent"
            return
        except TokenRejectedError as exc:
            outcome.status = "failed"
            outcome.token_state = "rejected"
            outcome.errors.append({"stage": "perfil", **exc.detail.as_dict()})
            self._log.error("%s: token rechazado, se aborta la plataforma.", platform.label)
            return
        except MetaApiError as exc:
            outcome.status = "failed"
            outcome.errors.append({"stage": "perfil", **exc.detail.as_dict()})
            return

        account_id = profile.account_id
        media_items: list[MediaItem] = []
        try:
            for media in gateway.iter_media(max_items=opts.max_media):
                media_items.append(media)
                self._snapshots.save_media(media, account_id=account_id)
                outcome.media_seen += 1
                if media.caption.looks_like_injection:
                    issues.append(DataQualityIssue(
                        platform=platform, kind="texto_externo_sospechoso",
                        severity="info", subject=media.ref,
                        detail=("El pie de la publicacion contiene frases con forma de "
                                "instruccion. Se trata como dato, nunca como instruccion."),
                        observed={"media_id": media.media_id}, run_id=run.run_id,
                    ))
        except TokenRejectedError as exc:
            outcome.status = "failed"
            outcome.token_state = "rejected"
            outcome.errors.append({"stage": "publicaciones", **exc.detail.as_dict()})
        except MetaApiError as exc:
            # Paginacion interrumpida: conservamos lo recorrido hasta aqui.
            outcome.status = "partial"
            outcome.errors.append({"stage": "publicaciones", **exc.detail.as_dict()})
            self._log.warning("%s: paginacion interrumpida tras %s publicaciones.",
                              platform.label, outcome.media_seen)
        except GongoraError as exc:
            outcome.status = "partial"
            outcome.errors.append({"stage": "publicaciones", "message": exc.safe_message})

        if outcome.token_state != "rejected":
            for media in media_items:
                self._collect_insights(run, platform, gateway, media, account_id,
                                       snapshots, gaps, issues, outcome)

        outcome.snapshots_written = self._snapshots.save_snapshots(snapshots)
        outcome.gaps = self._snapshots.save_gaps(gaps)
        outcome.issues = self._snapshots.save_issues(issues)
        outcome.api_calls = getattr(gateway, "api_calls", 0)

        if outcome.status == "running":
            if outcome.errors:
                outcome.status = "partial"
            elif gaps:
                outcome.status = "partial"
            else:
                outcome.status = "ok"

    def _collect_insights(self, run, platform, gateway, media, account_id,
                          snapshots, gaps, issues, outcome) -> None:
        try:
            result = gateway.fetch_insights(media)
        except TokenRejectedError as exc:
            outcome.token_state = "rejected"
            outcome.errors.append({"stage": f"insights:{media.media_id}",
                                   **exc.detail.as_dict()})
            return
        except MetaApiError as exc:
            outcome.status = "partial"
            outcome.errors.append({"stage": f"insights:{media.media_id}",
                                   **exc.detail.as_dict()})
            return

        captured_at = self._clock.now()
        for metric, (value, period) in result.values.items():
            snapshots.append(MetricSnapshot(
                captured_at=captured_at, platform=platform, account_id=account_id,
                media_id=media.media_id, metric=metric, period=period,
                value=value, run_id=run.run_id,
            ))
            self._check_monotonic(run, platform, account_id, media, metric, value, issues)

        for metric, (reason, detail) in result.gaps.items():
            gaps.append(MetricGap(
                platform=platform, account_id=account_id, media_id=media.media_id,
                metric=metric, reason=reason, detail=detail, run_id=run.run_id,
            ))

        self._check_interaction_sum(run, platform, media, result, issues)

    # -------------------------------------------------- calidad de datos

    def _check_interaction_sum(self, run, platform: Platform, media: MediaItem,
                               result, issues: list[DataQualityIssue]) -> None:
        """Compara los componentes con el total que da la plataforma.

        Si no cuadran, se conservan AMBOS numeros y se registra la
        discrepancia. No se infiere la causa.
        """
        if platform is not Platform.INSTAGRAM:
            return
        total_entry = result.values.get(metrics_catalog.IG_INTERACTION_TOTAL)
        if total_entry is None:
            return
        total = total_entry[0]
        components = {m: result.values[m][0] for m in metrics_catalog.IG_INTERACTION_COMPONENTS
                      if m in result.values}
        missing = [m for m in metrics_catalog.IG_INTERACTION_COMPONENTS
                   if m not in result.values]
        if missing:
            issues.append(DataQualityIssue(
                platform=platform, kind="verificacion_de_total_imposible",
                severity="info", subject=media.ref,
                detail=("No se puede comprobar el total de interacciones porque faltan "
                        f"componentes: {', '.join(missing)}. Una metrica ausente no es cero."),
                observed={"total_interactions": total, "componentes_presentes": components,
                          "componentes_ausentes": missing},
                run_id=run.run_id,
            ))
            return
        suma = sum(components.values())
        if abs(suma - total) > 1e-9:
            issues.append(DataQualityIssue(
                platform=platform, kind="suma_interacciones_discrepante",
                severity="warning", subject=media.ref,
                detail=(f"Los componentes suman {suma:g} y la plataforma informa "
                        f"total_interactions={total:g} (diferencia {total - suma:+g}). "
                        "Se conservan ambos valores tal cual. La causa no se infiere: "
                        "haria falta documentacion o confirmacion de Meta."),
                observed={"componentes": components, "suma_componentes": suma,
                          "total_informado": total, "diferencia": total - suma},
                run_id=run.run_id,
            ))

    def _check_monotonic(self, run, platform: Platform, account_id: str, media: MediaItem,
                         metric: str, value: float, issues: list[DataQualityIssue]) -> None:
        """Una metrica acumulada no deberia bajar entre snapshots."""
        spec = metrics_catalog.spec_for(platform, metric)
        if spec.aggregation != "cumulative":
            return
        previous = self._snapshots.latest_snapshot(
            platform=platform, account_id=account_id, media_id=media.media_id, metric=metric
        )
        if previous is None:
            return
        if value < previous["value"] - 1e-9:
            issues.append(DataQualityIssue(
                platform=platform, kind="metrica_acumulada_decreciente",
                severity="warning", subject=f"{media.ref}:{metric}",
                detail=(f"{metric} paso de {previous['value']:g} ({previous['captured_at']}) "
                        f"a {value:g}. Una metrica acumulada no deberia bajar. Se registra "
                        "el hecho sin asignarle causa."),
                observed={"anterior": previous["value"], "actual": value,
                          "anterior_captured_at": previous["captured_at"]},
                run_id=run.run_id,
            ))

    @staticmethod
    def _overall_status(run: CollectionRun) -> str:
        statuses = {o.status for o in run.outcomes.values()}
        attempted = statuses - {"skipped"}
        if not attempted:
            return "failed" if not statuses else "ok"
        if attempted == {"ok"}:
            return "ok"
        if "ok" in attempted or "partial" in attempted:
            return "partial"
        return "failed"
