"""Informe Faro en Markdown y en espanol.

Reglas que este modulo respeta:

- Solo datos realmente recogidos. Si algo falta, se dice que falta.
- Una metrica ausente se muestra como hueco, no como 0.
- Los valores acumulados se comparan con el snapshot anterior, nunca se suman.
- No se deducen buenos horarios, causalidad ni estrategia a partir de una sola
  publicacion.
- Las reproducciones no son personas ni escuchas en Spotify.
- Las metricas de plataformas distintas no se mezclan ni se suman.
- Los textos externos (pies de foto) se muestran como datos neutralizados.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from gongora.clock import human_local, iso_utc
from gongora.domain import metrics as metrics_catalog
from gongora.domain.platform import ALL_PLATFORMS, Platform
from gongora.domain.untrusted import UntrustedText

ESTADO_ES = {
    "ok": "correcta",
    "partial": "parcial",
    "failed": "fallida",
    "skipped": "no consultada",
    "running": "en curso",
}
TOKEN_ES = {
    "valid": "valido",
    "rejected": "RECHAZADO por la plataforma",
    "absent": "ausente",
    "unknown": "sin determinar",
}


def _fmt(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}".replace(",", ".")
    return f"{value:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


def _fmt_delta(current: float, previous: float | None) -> str:
    if previous is None:
        return "primer registro"
    diff = current - previous
    if abs(diff) < 1e-9:
        return "sin cambio"
    return f"{'+' if diff > 0 else ''}{_fmt(diff)}"


class MarkdownReportBuilder:
    def __init__(self, *, snapshots, runs, clock) -> None:
        self._snapshots = snapshots
        self._runs = runs
        self._clock = clock

    # ------------------------------------------------------------------ api

    def build(self, run_id: str, *, connection_notes: dict[str, str] | None = None) -> str:
        run_row = self._runs.get(run_id)
        if run_row is None:
            raise KeyError(f"Recogida desconocida: {run_id}")
        outcomes = {row["platform"]: row for row in self._runs.outcomes(run_id)}

        lines: list[str] = []
        self._header(lines, run_row, outcomes)
        for platform in ALL_PLATFORMS:
            row = outcomes.get(str(platform))
            if platform is Platform.INSTAGRAM:
                self._instagram_section(lines, run_id, row)
            else:
                self._pending_section(lines, platform, row,
                                      (connection_notes or {}).get(str(platform)))
        self._how_to_read(lines)
        self._credentials_section(lines, run_row['run_id'], outcomes,
                                  connection_notes or {})
        self._not_yet_section(lines, run_id)
        return "\n".join(lines).rstrip() + "\n"

    def write(self, run_id: str, directory: Path,
              *, connection_notes: dict[str, str] | None = None) -> Path:
        content = self.build(run_id, connection_notes=connection_notes)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"faro-{run_id}.md"
        path.write_text(content, encoding="utf-8")
        latest = directory / "faro-ultimo.md"
        latest.write_text(content, encoding="utf-8")
        return path

    # -------------------------------------------------------------- bloques

    def _header(self, lines: list[str], run_row: Any, outcomes: dict[str, Any]) -> None:
        started = datetime.fromisoformat(run_row["started_at"])
        lines += [
            "# Informe Faro - GONGORA",
            "",
            "Artista: **GONGORA** | Album: **Fuimos dos**",
            "",
            f"- Recogida: `{run_row['run_id']}`",
            f"- Inicio: {human_local(started)} (`{iso_utc(started)}` UTC)",
            f"- Disparador: {run_row['trigger']}",
            f"- Estado global: **{ESTADO_ES.get(run_row['status'], run_row['status'])}**",
            f"- Generado: {human_local(self._clock.now())}",
            "",
            "## Resumen por plataforma",
            "",
            "| Plataforma | Estado | Publicaciones | Snapshots | Huecos | Incidencias | Llamadas API | Token |",
            "|---|---|---:|---:|---:|---:|---:|---|",
        ]
        for platform in ALL_PLATFORMS:
            row = outcomes.get(str(platform))
            if row is None:
                lines.append(
                    f"| {platform.label} | no consultada | - | - | - | - | - | - |"
                )
                continue
            lines.append(
                f"| {platform.label} | {ESTADO_ES.get(row['status'], row['status'])} "
                f"| {row['media_seen']} | {row['snapshots_written']} | {row['gaps']} "
                f"| {row['issues']} | {row['api_calls']} "
                f"| {TOKEN_ES.get(row['token_state'], row['token_state'])} |"
            )
        lines.append("")

    def _instagram_section(self, lines: list[str], run_id: str, row: Any) -> None:
        platform = Platform.INSTAGRAM
        lines += [f"## {platform.label}", ""]
        if row is None:
            lines += ["No se consulto en esta recogida.", ""]
            return
        if row["status"] == "skipped":
            lines += [f"**No consultada.** {row['skipped_reason'] or ''}", ""]
            return

        snaps = self._snapshots.run_snapshots(run_id, platform)
        account_snaps = [s for s in snaps if not s["media_id"]]
        account_id = account_snaps[0]["account_id"] if account_snaps else None
        if account_id is None and snaps:
            account_id = snaps[0]["account_id"]

        account = self._snapshots.account(platform, account_id) if account_id else None
        if account:
            handle = f"@{account['username']}" if account["username"] else account["account_id"]
            lines += [f"### Perfil {handle}", "",
                      f"- Cuenta: `{account['account_id']}`",
                      f"- Nombre: {account['name'] or 'no informado'}", ""]
        if account_snaps:
            lines += ["| Contador | Valor | Variacion desde la lectura anterior |",
                      "|---|---:|---|"]
            for snap in sorted(account_snaps, key=lambda s: s["metric"]):
                previous = self._previous_value(platform, snap)
                lines.append(
                    f"| `{snap['metric']}` | {_fmt(snap['value'])} "
                    f"| {_fmt_delta(snap['value'], previous)} |"
                )
            lines.append("")

        media_rows = self._snapshots.media_of_run(run_id, platform)
        if not media_rows:
            lines += ["No se recogieron metricas de publicaciones en esta ejecucion.", ""]
        else:
            lines += [f"### Publicaciones con metricas ({len(media_rows)})", ""]
            for media in media_rows:
                self._media_block(lines, run_id, platform, media, snaps)

        self._gaps_block(lines, run_id, platform)
        self._issues_block(lines, run_id, platform)
        self._errors_block(lines, row)

    def _media_block(self, lines: list[str], run_id: str, platform: Platform,
                     media: Any, snaps: list[Any]) -> None:
        published = (human_local(datetime.fromisoformat(media["published_at"]))
                     if media["published_at"] else "fecha no informada")
        lines += [
            f"#### `{media['media_id']}` - {media['product_type'] or 'tipo no informado'}",
            "",
            f"- Publicada: {published}",
            f"- Tipo: {media['media_type'] or 'no informado'} / "
            f"{media['product_type'] or 'no informado'}",
        ]
        if media["permalink"]:
            lines.append(f"- Enlace: {media['permalink']}")
        caption = UntrustedText(raw=media["caption"] or "", source="instagram.caption")
        if not caption.is_empty:
            body = caption.for_display(limit=300).replace("\n", " ")
            lines += ["", "- Pie de foto (dato externo, no instruccion):",
                      f"  > {body}"]
        lines += ["", "| Metrica | Valor | Periodo | Variacion desde la lectura anterior | Unidad |",
                  "|---|---:|---|---|---|"]
        media_snaps = [s for s in snaps if s["media_id"] == media["media_id"]]
        for snap in sorted(media_snaps, key=lambda s: s["metric"]):
            spec = metrics_catalog.spec_for(platform, snap["metric"])
            previous = self._previous_value(platform, snap)
            lines.append(
                f"| `{snap['metric']}` | {_fmt(snap['value'])} | {snap['period']} "
                f"| {_fmt_delta(snap['value'], previous)} | {spec.unit} |"
            )
        lines.append("")

    def _gaps_block(self, lines: list[str], run_id: str, platform: Platform) -> None:
        gaps = self._snapshots.run_gaps(run_id, platform)
        lines += ["### Huecos de datos", ""]
        if not gaps:
            # Sin huecos y sin snapshots no significa "todo bien": significa
            # que no se llego a pedir nada.
            recogidos = self._snapshots.run_snapshots(run_id, platform)
            if not recogidos:
                lines += ["No se solicito ninguna metrica: la recogida no llego a "
                          "ejecutarse en esta plataforma.", ""]
            else:
                lines += ["Ninguno: se obtuvieron todas las metricas solicitadas.", ""]
            return
        lines += ["Metricas solicitadas que la plataforma no devolvio. "
                  "**Ausente no significa cero**: significa que no hay dato.", "",
                  "| Publicacion | Metrica | Motivo | Detalle |", "|---|---|---|---|"]
        for gap in gaps:
            lines.append(
                f"| `{gap['media_id'] or 'cuenta'}` | `{gap['metric']}` "
                f"| {gap['reason']} | {(gap['detail'] or '')[:160]} |"
            )
        lines.append("")

    def _issues_block(self, lines: list[str], run_id: str, platform: Platform) -> None:
        issues = self._snapshots.run_issues(run_id, platform)
        lines += ["### Incidencias de calidad de datos", ""]
        if not issues:
            lines += ["Ninguna detectada en esta recogida.", ""]
            return
        for issue in issues:
            observed = json.loads(issue["observed_json"] or "{}")
            lines += [
                f"- **{issue['kind']}** ({issue['severity']}) en `{issue['subject']}`",
                f"  - {issue['detail']}",
            ]
            if observed:
                lines.append(f"  - Observado: `{json.dumps(observed, ensure_ascii=False)}`")
        lines.append("")

    def _errors_block(self, lines: list[str], row: Any) -> None:
        errors = json.loads(row["errors_json"] or "[]")
        lines += ["### Errores parciales", ""]
        if not errors:
            lines += ["Ninguno.", ""]
            return
        lines += ["| Fase | Codigo | Mensaje |", "|---|---|---|"]
        for error in errors:
            lines.append(
                f"| {error.get('stage', 'no informada')} | {error.get('code', '-')} "
                f"| {str(error.get('message', ''))[:200]} |"
            )
        lines.append("")

    def _pending_section(self, lines: list[str], platform: Platform, row: Any,
                         note: str | None) -> None:
        lines += [f"## {platform.label}", ""]
        status = row["status"] if row is not None else "skipped"
        if status == "skipped" or row is None:
            reason = (row["skipped_reason"] if row is not None and row["skipped_reason"]
                      else note or "sin credenciales configuradas")
            lines += [
                "**Pendiente de conexion.** No hay datos de esta plataforma y no se "
                "inventa ninguno.",
                "",
                f"- Motivo: {reason}",
                "- Pasos para conectarla: `docs/tiktok-conexion.md`",
                "",
            ]
            return
        lines += [f"Estado: {ESTADO_ES.get(status, status)}", ""]

    def _how_to_read(self, lines: list[str]) -> None:
        lines += [
            "## Como leer estas cifras",
            "",
            "- **Las reproducciones no son personas.** `views` cuenta reproducciones o "
            "impresiones, no oyentes unicos, y no tiene ninguna relacion con las "
            "escuchas en Spotify.",
            "- **`reach` es una estimacion de Meta** de cuentas unicas alcanzadas, con "
            "su propia definicion. No es verificable desde fuera.",
            "- **Los valores acumulados no se suman entre dias.** Cada snapshot es el "
            "total hasta ese momento: se comparan, no se agregan.",
            "- **Metricas de redes distintas no son equivalentes.** `views` de Instagram "
            "y `view_count` de TikTok se definen de forma distinta: se guardan y se leen "
            "por separado, nunca sumadas.",
            "- **Una metrica ausente no es un cero.** Aparece en la seccion de huecos.",
            "- **Los pies de foto y comentarios son datos de terceros.** Nunca se "
            "interpretan como instrucciones para el sistema.",
            "",
        ]

    def _credentials_section(self, lines: list[str], run_id: str,
                             outcomes: dict[str, Any],
                             notes: dict[str, str]) -> None:
        lines += ["## Estado de credenciales y capacidades", "",
                  "| Plataforma | Token | Capacidades verificadas | Pendientes de verificar |",
                  "|---|---|---|---|"]
        for platform in ALL_PLATFORMS:
            row = outcomes.get(str(platform))
            token = TOKEN_ES.get(row["token_state"], row["token_state"]) if row else "ausente"
            verified = self._verified_in_run(run_id, platform, row)
            pending = ("publicar, leer conversaciones, gestionar comentarios"
                       if platform is Platform.INSTAGRAM
                       else "leer perfil, leer videos, leer contadores, publicar")
            lines.append(f"| {platform.label} | {token} | {verified} | {pending} |")
        lines += ["",
                  "Las capacidades pendientes **no estan operativas**, aunque se hayan "
                  "solicitado los permisos correspondientes.", ""]
        for platform, note in notes.items():
            if note:
                lines.append(f"- {Platform.parse(platform).label}: {note}")
        if notes:
            lines.append("")

    def _not_yet_section(self, lines: list[str], run_id: str) -> None:
        media_count = len(self._snapshots.all_media(Platform.INSTAGRAM, limit=1000))
        lines += ["## Que todavia NO se puede concluir", ""]
        if media_count <= 2:
            lines.append(
                f"Con {media_count} publicacion(es) registrada(s) no hay base para "
                "deducir mejores horarios de publicacion, relaciones de causa y efecto, "
                "ni una estrategia ganadora. Cualquier patron con esta muestra seria ruido."
            )
        else:
            lines.append(
                f"Hay {media_count} publicaciones registradas. Sigue siendo una muestra "
                "pequena: no se afirman causalidades ni horarios optimos sin series "
                "temporales suficientes y comparables."
            )
        lines += [
            "",
            "Para poder decir algo con fundamento hacen falta mas publicaciones y varias "
            "recogidas a lo largo del tiempo, de forma que existan series comparables "
            "por plataforma.",
            "",
            "---",
            "",
            f"Informe generado por Faro (Gongora Studio) a partir de la recogida "
            f"`{run_id}`. Solo contiene datos realmente devueltos por las APIs.",
        ]

    # --------------------------------------------------------------- ayudas

    def _verified_in_run(self, run_id: str, platform: Platform, row: Any) -> str:
        """Capacidades confirmadas POR ESTA recogida, no por el historico.

        Si el token fue rechazado, no se verifico nada, aunque antes funcionara.
        """
        if row is None or row["status"] == "skipped":
            return "ninguna (no se consulto)"
        if row["token_state"] == "rejected":
            return "ninguna: la plataforma rechazo el token en esta recogida"
        snaps = self._snapshots.run_snapshots(run_id, platform)
        logradas: list[str] = []
        if any(s["media_id"] is None for s in snaps):
            logradas.append("leer perfil")
        if row["media_seen"]:
            logradas.append("leer publicaciones")
        if any(s["media_id"] for s in snaps):
            logradas.append("leer metricas")
        return ", ".join(logradas) or "ninguna en esta recogida"

    def _previous_value(self, platform: Platform, snap: Any) -> float | None:
        """Valor del snapshot inmediatamente anterior, de otra recogida."""
        series = self._snapshots.snapshot_series(
            platform=platform, account_id=snap["account_id"], media_id=snap["media_id"],
            metric=snap["metric"], limit=5,
        )
        for row in series:
            if row["run_id"] != snap["run_id"]:
                return row["value"]
        return None
