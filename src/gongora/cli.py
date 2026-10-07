"""CLI de Gongora Studio.

Comandos:
    gongora doctor                      configuracion, conectividad y capacidades
    gongora collect                     recogida real (la que usa el temporizador)
    gongora report                      informe de una recogida
    gongora agents                      roles, workers y estado
    gongora tasks list|show             cola de tareas entre agentes
    gongora worker --agent X --once     ejecuta una tarea y termina
    gongora schedule status|enable|disable|run
    gongora tiktok config|login|refresh|check   conexion OAuth de TikTok
"""

from __future__ import annotations

import argparse
import json
import getpass
import shutil
import stat
import subprocess
import sys
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

from gongora import __version__, redaction
from gongora.adapters.llm.cli_llm import available_backends
from gongora.adapters.locking import FileLock, LockBusy
from gongora.adapters.meta import token_service
from gongora.agents import BY_NAME, REGISTRY, role_configured, worker_active
from gongora.application.collect import CollectionOptions
from gongora.application.worker import build_worker
from gongora.clock import human_local
from gongora.adapters.tiktok.callback import DEFAULT_TIMEOUT_SECONDS
from gongora.adapters.tiktok.login import TikTokLogin
from gongora.adapters.tiktok.service import TikTokService
from gongora.config import (
    DEFAULT_TIKTOK_SCOPES,
    ConfigError,
    build_paths,
    load_settings,
    load_tiktok_app_config,
)
from gongora.domain.errors import ConnectorNotAuthorized, GongoraError
from gongora.logging_setup import configure
from gongora.container import Container, build
from gongora.domain.platform import ALL_PLATFORMS, Platform
from gongora.domain.tasks import TASK_COLLECTION_REQUEST

TIMER_UNIT = "gongora-collect.timer"
SERVICE_UNIT = "gongora-collect.service"

TICK, CROSS, WARN, DOT = "OK", "FALLO", "AVISO", "--"


# ----------------------------------------------------------------- utilidades

def _print_kv(label: str, value: str, width: int = 34) -> None:
    print(f"  {label:<{width}} {value}")


def _section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def _platforms_arg(values: list[str] | None) -> tuple[Platform, ...] | None:
    if not values:
        return None
    return tuple(Platform.parse(v) for v in values)


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                          text=True, check=False)


def _has_systemd_user() -> bool:
    if shutil.which("systemctl") is None:
        return False
    return _systemctl("is-system-running").returncode in (0, 1)


# -------------------------------------------------------------------- doctor

def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"Gongora Studio {__version__} - diagnostico")
    problems = 0

    _section("1. Configuracion")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"  {CROSS} {exc}")
        return 2
    _print_kv("Fichero de secretos", f"{settings.secrets_file} (modo {settings.secrets_file_mode})")
    if settings.secrets_file_mode not in ("0600", "0400"):
        print(f"  {WARN} El fichero de secretos deberia tener permisos 600. "
              f"Corrige con: chmod 600 {settings.secrets_file}")
        problems += 1
    else:
        print(f"  {TICK} Permisos del fichero de secretos correctos.")
    _print_kv("Flujo de autenticacion", settings.credentials.auth_flow +
              " (Instagram API with Facebook Login)")
    _print_kv("Version de la Graph API", settings.credentials.graph_version)
    _print_kv("Pagina de Facebook", settings.credentials.page_id)
    _print_kv("Cuenta de Instagram", settings.credentials.instagram_account_id)
    _print_kv("Huella del token", f"{settings.credentials.token_fingerprint} "
                                  f"({settings.credentials.token_length} caracteres, nunca se muestra)")
    _print_kv("Secretos registrados para redaccion", str(redaction.registered_count()))
    _print_kv("Datos locales", str(settings.paths.home))
    _print_kv("Base de datos", str(settings.paths.db))
    _print_kv("Credenciales de TikTok",
              str(settings.tiktok_secrets_file) +
              (" (presentes)" if settings.tiktok else " (ausentes: pendiente de conexion)"))

    container = build(settings=settings, verbose=args.verbose)
    try:
        _section("2. Base de datos")
        version = container.connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        _print_kv("Esquema", f"version {version['value'] if version else 'desconocida'}")
        counts = {
            table: container.connection.execute(f"SELECT COUNT(*) AS n FROM {table}")
                                      .fetchone()["n"]
            for table in ("metric_snapshots", "media", "accounts", "collection_runs",
                          "tasks", "metric_gaps", "data_quality_issues")
        }
        _print_kv("Filas", ", ".join(f"{k}={v}" for k, v in counts.items()))

        _section("3. Conectividad por plataforma")
        for platform in ALL_PLATFORMS:
            gateway = container.gateways[platform]
            status = gateway.check_connection()
            mark = TICK if status.connected else (DOT if status.token_state == "absent" else CROSS)
            print(f"  [{mark}] {platform.label}: {status.detail}")
            _print_kv("  token", status.token_state)
            if status.verified_capabilities:
                _print_kv("  capacidades verificadas", ", ".join(status.verified_capabilities))
            if status.pending_capabilities:
                _print_kv("  pendientes (NO operativas)", ", ".join(status.pending_capabilities))
            if not status.connected and status.token_state == "rejected":
                problems += 1

        _section("4. Ultima recogida satisfactoria")
        for platform in ALL_PLATFORMS:
            last = container.runs.last_successful(platform)
            if last is None:
                _print_kv(platform.label, "ninguna todavia")
            else:
                started = datetime.fromisoformat(last["started_at"])
                _print_kv(platform.label, f"{last['run_id']} ({human_local(started)})")

        _section("5. Programador")
        _print_kv("systemd de usuario", "disponible" if _has_systemd_user() else "no disponible")
        state = _systemctl("is-active", TIMER_UNIT).stdout.strip() or "no instalado"
        _print_kv(f"{TIMER_UNIT}", state)

        _section("6. CLI de modelos (para Luna, no para Faro)")
        for name, probe in available_backends().items():
            _print_kv(name, (f"disponible: {probe.version}" if probe.available
                             else f"no disponible: {probe.detail}"))
        print("  Nota: Faro no usa ningun LLM. Ninguna generacion se ha ejecutado aun.")

        print()
        if problems:
            print(f"{WARN} Diagnostico terminado con {problems} aviso(s).")
        else:
            print(f"{TICK} Diagnostico terminado sin problemas que bloqueen la recogida.")
        return 0
    finally:
        container.close()


# ------------------------------------------------------------------- collect

def cmd_collect(args: argparse.Namespace) -> int:
    container = build(verbose=args.verbose)
    try:
        lock_path = container.settings.paths.lock
        try:
            with FileLock(lock_path):
                return _run_collection(container, args)
        except LockBusy as exc:
            print(f"{WARN} {exc}")
            return 3
    finally:
        container.close()


def _run_collection(container: Container, args: argparse.Namespace) -> int:
    options = CollectionOptions(
        platforms=_platforms_arg(args.platform),
        max_media=args.max_media,
        force=args.force,
        trigger=args.trigger,
    )
    result = container.collector.execute(options)
    if result.skipped_reason:
        print(f"{DOT} Recogida omitida: {result.skipped_reason}")
        return 0

    run = result.run
    print(f"Recogida {run.run_id}: estado {run.status}")
    for platform in ALL_PLATFORMS:
        outcome = run.outcomes.get(platform)
        if outcome is None:
            continue
        if outcome.status == "skipped":
            print(f"  {DOT} {platform.label}: no consultada - {outcome.skipped_reason}")
            continue
        print(f"  [{TICK if outcome.status == 'ok' else WARN}] {platform.label}: "
              f"{outcome.status} | publicaciones={outcome.media_seen} "
              f"snapshots={outcome.snapshots_written} huecos={outcome.gaps} "
              f"incidencias={outcome.issues} llamadas={outcome.api_calls} "
              f"token={outcome.token_state}")
        for error in outcome.errors:
            print(f"      error en {error.get('stage')}: {str(error.get('message'))[:160]}")

    if args.no_report:
        return 0

    path = container.report_builder.write(run.run_id, container.settings.paths.reports)
    digest = container.snapshots.record_artifact(run_id=run.run_id, kind="report", path=path)
    print(f"\nInforme: {path}")

    if args.no_dispatch:
        return 0
    briefs = container.dispatcher.execute(run, path, digest)
    if not briefs:
        print("Sin tareas para Luna: ninguna plataforma aporto snapshots.")
    for task_id, created, platform in briefs:
        verb = "creada" if created else "ya existia (idempotencia)"
        print(f"Tarea para Luna ({platform.label}): {task_id} [{verb}] - pendiente, "
              "sin worker que la atienda todavia.")
    return 0


# -------------------------------------------------------------------- report

def cmd_report(args: argparse.Namespace) -> int:
    container = build(verbose=args.verbose)
    try:
        run_id = args.run or container.runs.latest_run_id()
        if run_id is None:
            print(f"{CROSS} No hay ninguna recogida registrada. Ejecuta: gongora collect")
            return 1
        notes = ({} if container.gateways[Platform.TIKTOK].is_configured() else
                 {str(Platform.TIKTOK): "pendiente de conexion (ver docs/tiktok-conexion.md)"})
        if args.stdout:
            print(container.report_builder.build(run_id, connection_notes=notes))
            return 0
        path = container.report_builder.write(run_id, container.settings.paths.reports,
                                              connection_notes=notes)
        container.snapshots.record_artifact(run_id=run_id, kind="report", path=path)
        print(f"Informe escrito en: {path}")
        print(f"Copia estable:      {path.parent / 'faro-ultimo.md'}")
        print(f"Para abrirlo:       less '{path}'   |   code '{path}'")
        return 0
    finally:
        container.close()


# -------------------------------------------------------------------- agents

def cmd_agents(args: argparse.Namespace) -> int:
    container = build(verbose=args.verbose)
    try:
        root = container.project_root
        timer_active = _systemctl("is-active", TIMER_UNIT).stdout.strip() == "active"
        counts = container.queue.counts_by_recipient()
        print("Roles de Gongora Studio\n")
        print("Un fichero de instrucciones NO es un agente operativo. Se distinguen:")
        print("  rol configurado    = existe fichero de instrucciones")
        print("  worker implementado = existe codigo que lo ejecuta")
        print("  worker activo      = ese codigo se ha ejecutado de verdad\n")

        for definition in REGISTRY:
            state = container.agent_state.get(definition.name, "*")
            configured = role_configured(definition, root)
            active = worker_active(definition, state)
            # "no" no es un fallo: un rol sin worker es un estado previsto.
            si_no = lambda valor: "si" if valor else "no"
            print(f"== {definition.name.upper()} ==")
            _print_kv("Mision", definition.mission)
            _print_kv("Rol configurado", f"{si_no(configured)} "
                                         f"({definition.instructions_file})")
            _print_kv("Worker implementado", si_no(definition.worker_implemented))
            _print_kv("Worker activo", si_no(active))
            _print_kv("Plataformas", ", ".join(p.label for p in definition.platforms))
            if definition.capabilities:
                _print_kv("Capacidades operativas", "")
                for capability in definition.capabilities:
                    print(f"      - {capability}")
            if definition.not_implemented:
                _print_kv("Declaradas, NO implementadas", "")
                for capability in definition.not_implemented:
                    print(f"      - {capability}")
            _print_kv("Consume tareas", ", ".join(definition.consumes) or "ninguna")
            _print_kv("Produce tareas", ", ".join(definition.produces) or "ninguna")
            if state is not None:
                _print_kv("Ultima ejecucion",
                          human_local(datetime.fromisoformat(state["last_run_at"]))
                          if state["last_run_at"] else "nunca")
                _print_kv("Ultimo estado", state["last_status"] or "sin datos")
                _print_kv("Ultimo exito",
                          human_local(datetime.fromisoformat(state["last_success_at"]))
                          if state["last_success_at"] else "ninguno")
                _print_kv("Ultimo error", state["last_error"] or "ninguno")
            else:
                _print_kv("Ultima ejecucion", "nunca")
                _print_kv("Ultimo error", "ninguno")
            mine = counts.get(definition.name, {})
            _print_kv("Tareas en cola", ", ".join(f"{k}={v}" for k, v in sorted(mine.items()))
                                        or "ninguna")
            if definition.name == "faro":
                _print_kv("Recogida programada",
                          "activa (systemd)" if timer_active else "no activa")
            print()

        # Estado de plataformas por agente
        print("Conectores por plataforma")
        for platform in ALL_PLATFORMS:
            gateway = container.gateways[platform]
            configured = gateway.is_configured()
            _print_kv(platform.label,
                      "credenciales presentes" if configured else "pendiente de conexion")
        return 0
    finally:
        container.close()


# --------------------------------------------------------------------- tasks

def cmd_tasks_list(args: argparse.Namespace) -> int:
    container = build(verbose=args.verbose)
    try:
        statuses = args.status.split(",") if args.status else None
        tasks = container.queue.list_tasks(
            recipient=args.agent, statuses=statuses,
            platform=Platform.parse(args.platform) if args.platform else None,
            limit=args.limit,
        )
        if not tasks:
            print("No hay tareas que cumplan el filtro.")
            return 0
        print(f"{'ID':36}  {'DEST':7} {'PLATAFORMA':11} {'ESTADO':9} {'PRI':3} "
              f"{'INT':5} {'TIPO':28} CREADA")
        for task in tasks:
            platform = task.platform.label if task.platform else "-"
            print(f"{task.id:36}  {task.recipient:7} {platform:11} {task.status:9} "
                  f"{task.priority:<3} {task.attempts}/{task.max_attempts:<3} "
                  f"{task.type:28} {human_local(task.created_at)}")
        pending_luna = [t for t in tasks if t.recipient == "luna" and t.status == "pending"]
        if pending_luna:
            print(f"\n{len(pending_luna)} tarea(s) pendiente(s) para Luna. Su worker no "
                  "esta implementado: nadie las ha leido ni respondido.")
        return 0
    finally:
        container.close()


def cmd_tasks_show(args: argparse.Namespace) -> int:
    container = build(verbose=args.verbose)
    try:
        task = container.queue.get(args.task_id)
        if task is None:
            print(f"{CROSS} Tarea desconocida: {args.task_id}")
            return 1
        _print_kv("ID", task.id)
        _print_kv("Destinatario", task.recipient)
        _print_kv("Plataforma", task.platform.label if task.platform else "-")
        _print_kv("Tipo", f"{task.type} v{task.schema_version}")
        _print_kv("Estado", task.status)
        _print_kv("Prioridad", str(task.priority))
        _print_kv("Intentos", f"{task.attempts}/{task.max_attempts}")
        _print_kv("Clave de idempotencia", task.idempotency_key)
        _print_kv("Correlacion (recogida)", task.correlation_id or "-")
        _print_kv("Creada", human_local(task.created_at))
        _print_kv("Disponible desde", human_local(task.available_at))
        _print_kv("Lease expira", human_local(task.lease_expires_at)
                  if task.lease_expires_at else "-")
        _print_kv("Ultimo error", task.last_error or "ninguno")
        _section("Payload")
        print(json.dumps(task.payload, ensure_ascii=False, indent=2))
        _section("Eventos")
        for event in container.queue.events(task.id):
            print(f"  {event['at']}  {event['kind']:16} "
                  f"{json.dumps(event['detail'], ensure_ascii=False)}")
        results = container.queue.results(task.id)
        if results:
            _section("Resultados")
            for result in results:
                print(f"  {result['at']}  {result['status']}  {result['summary']}")
                for artifact in result["artifacts"]:
                    print(f"      artefacto: {artifact.get('kind')} -> {artifact.get('path')}")
        return 0
    finally:
        container.close()


def cmd_tasks_request_collection(args: argparse.Namespace) -> int:
    """Encola una peticion de recogida para el worker de Faro."""
    container = build(verbose=args.verbose)
    try:
        platforms = [str(p) for p in (_platforms_arg(args.platform) or ())]
        payload = {
            "platforms": platforms,
            "max_media": args.max_media,
            "force": args.force,
            "requested_by": "cli",
            "reason": args.reason or "peticion manual",
        }
        key = args.idempotency_key or f"faro:collect:{container.clock.now():%Y%m%dT%H%M%S}"
        task, created = container.queue.enqueue(
            recipient="faro", task_type=TASK_COLLECTION_REQUEST, payload=payload,
            idempotency_key=key,
            platform=_platforms_arg(args.platform)[0] if args.platform else None,
        )
        print(f"Tarea {'creada' if created else 'ya existente'}: {task.id}")
        print(f"Ejecutala con: gongora worker --agent faro --once")
        return 0
    finally:
        container.close()


# --------------------------------------------------------------------- token

def cmd_token_status(args: argparse.Namespace) -> int:
    """Estado del token de Meta: validez, caducidad y permisos concedidos."""
    settings = load_settings()
    creds = settings.credentials
    _print_kv("Huella del token", f"{creds.token_fingerprint} "
                                  f"({creds.token_length} caracteres, nunca se muestra)")
    info = token_service.inspect_token(creds.token, creds.graph_version)
    _print_kv("Valido", TICK if info.is_valid else CROSS)
    _print_kv("Caducidad", info.remaining_description())
    _print_kv("Tipo", info.token_type or "no informado")
    _print_kv("App", f"{info.application or 'no informada'} ({info.app_id or 's/id'})")
    if info.data_access_expires_at:
        _print_kv("Acceso a datos caduca",
                  f"{info.data_access_expires_at:%Y-%m-%d %H:%M UTC}")
    _print_kv("Permisos concedidos", ", ".join(info.scopes) or "no informados")
    if info.error_message:
        _print_kv("Error", info.error_message)

    print()
    if not info.is_valid:
        print(f"{CROSS} El token no sirve. Para recuperar el acceso:")
        print("   1. Genera un token de usuario nuevo en el Explorador de la Graph API")
        print("      con los permisos: instagram_basic, instagram_manage_insights,")
        print("      pages_show_list, pages_read_engagement.")
        print(f"   2. Guardalo en {settings.secrets_file} (META_ACCESS_TOKEN=...).")
        print("   3. Para que dure: gongora token renew  (necesita META_APP_ID y")
        print("      META_APP_SECRET en el mismo fichero).")
        return 1
    if info.never_expires:
        print(f"{TICK} El token no tiene fecha de caducidad (probablemente de Pagina).")
    else:
        print(f"{WARN} Este token caduca. Para acceso duradero: gongora token renew")
    return 0


def cmd_token_renew(args: argparse.Namespace) -> int:
    """Flujo oficial: token corto -> token de usuario largo -> token de Pagina."""
    settings = load_settings()
    creds = settings.credentials
    data = parse_env_file(settings.secrets_file)
    app_id = data.get("META_APP_ID")
    app_secret = data.get("META_APP_SECRET")

    if not app_id or not app_secret:
        print(f"{CROSS} Faltan META_APP_ID y/o META_APP_SECRET en "
              f"{settings.secrets_file}.")
        print()
        print("Este paso NO se puede completar sin ellos, y no se inventan.")
        print("Donde obtenerlos: developers.facebook.com -> tu app -> "
              "Configuracion -> Basica.")
        print("Anade al fichero de secretos (sin comillas):")
        print("    META_APP_ID=<id de la app>")
        print("    META_APP_SECRET=<secreto de la app>")
        print()
        print("Flujo que se ejecutara despues (documentacion oficial de Meta):")
        print("  1. GET /oauth/access_token?grant_type=fb_exchange_token")
        print("     -> token de usuario de larga duracion (~60 dias)")
        print("  2. GET /me/accounts con ese token")
        print("     -> token de Pagina SIN fecha de caducidad")
        return 2

    print("Paso 1: cambiando el token corto por uno de larga duracion...")
    exchanged = token_service.exchange_for_long_lived(
        short_token=creds.token, app_id=app_id, app_secret=app_secret,
        graph_version=creds.graph_version)
    print(f"  {TICK if exchanged.ok else CROSS} {exchanged.detail}")
    if not exchanged.ok:
        return 1

    print("Paso 2: obteniendo el token de Pagina (sin caducidad)...")
    page = token_service.fetch_page_token(
        user_token=exchanged.token, page_id=creds.page_id,
        graph_version=creds.graph_version)
    print(f"  {TICK if page.ok else CROSS} {page.detail}")

    final = page if page.ok else exchanged
    if args.write:
        token_service.write_secret(settings.secrets_file, "META_ACCESS_TOKEN", final.token)
        print(f"\n{TICK} Token guardado en {settings.secrets_file} (no se ha mostrado).")
        print("Comprueba con: gongora token status && gongora doctor")
    else:
        print(f"\n{WARN} Token obtenido pero NO guardado. Repite con --write para "
              "escribirlo en el fichero de secretos sin mostrarlo por pantalla.")
    return 0 if final.ok else 1


# -------------------------------------------------------------------- worker

def cmd_worker(args: argparse.Namespace) -> int:
    if args.agent not in BY_NAME:
        print(f"{CROSS} Agente desconocido: {args.agent}. "
              f"Validos: {', '.join(BY_NAME)}")
        return 2
    if not args.once:
        print(f"{CROSS} Esta fase solo admite --once. No hay bucles de agentes "
              "sin criterio de finalizacion.")
        return 2

    container = build(verbose=args.verbose)
    try:
        if args.agent != "faro":
            worker = build_worker(args.agent)
            outcome = worker.run_once()
            print(f"[{args.agent}] {outcome.status}: {outcome.detail}")
            return 0

        with FileLock(container.settings.paths.lock):
            worker = build_worker(
                "faro",
                queue=container.queue, collector=container.collector,
                report_builder=container.report_builder, dispatcher=container.dispatcher,
                snapshots=container.snapshots, runs=container.runs,
                agent_state=container.agent_state, paths=container.settings.paths,
                clock=container.clock,
            )
            outcome = worker.run_once()
        for task_id, status in outcome.recovered:
            print(f"{WARN} Tarea recuperada por lease caducado: {task_id} -> {status}")
        print(f"[faro] {outcome.status}")
        if outcome.task_id:
            print(f"  tarea: {outcome.task_id} ({outcome.task_type})")
        if outcome.detail:
            print(f"  {outcome.detail}")
        for artifact in outcome.artifacts:
            print(f"  artefacto: {artifact}")
        return 0
    except LockBusy as exc:
        print(f"{WARN} {exc}")
        return 3
    finally:
        container.close()


# ------------------------------------------------------------------ schedule

def cmd_schedule(args: argparse.Namespace) -> int:
    action = args.action
    if not _has_systemd_user():
        print(f"{WARN} systemd de usuario no disponible. Usa la alternativa de cron: "
              "ops/install-cron.sh")
        return 1
    if action == "status":
        for unit in (TIMER_UNIT, SERVICE_UNIT):
            result = _systemctl("status", unit, "--no-pager")
            print(f"=== {unit} ===")
            print(result.stdout.strip() or result.stderr.strip())
            print()
        print("=== Proximas ejecuciones ===")
        print(_systemctl("list-timers", TIMER_UNIT, "--all", "--no-pager").stdout)
        return 0
    if action in ("enable", "disable", "start", "stop"):
        commands = {
            "enable": [("enable", "--now")],
            "disable": [("disable", "--now")],
            "start": [("start",)],
            "stop": [("stop",)],
        }[action]
        for extra in commands:
            result = _systemctl(*extra[:1], TIMER_UNIT, *extra[1:])
            print(result.stdout.strip() or result.stderr.strip() or
                  f"{action} aplicado a {TIMER_UNIT}")
        return 0
    if action == "run":
        result = _systemctl("start", SERVICE_UNIT)
        print(result.stdout.strip() or result.stderr.strip() or
              f"{SERVICE_UNIT} lanzado ahora")
        return 0
    if action == "logs":
        result = subprocess.run(
            ["journalctl", "--user", "-u", SERVICE_UNIT, "-n", "50", "--no-pager"],
            capture_output=True, text=True, check=False)
        print(result.stdout or result.stderr)
        return 0
    print(f"{CROSS} Accion desconocida: {action}")
    return 2


# -------------------------------------------------------------------- tiktok
#
# Comandos del operador para conectar TikTok. Ninguno imprime secretos: de los
# tokens solo se muestra si existen, su origen y su caducidad.

def _tiktok_logging(args: argparse.Namespace) -> None:
    paths = build_paths()
    paths.logs.mkdir(parents=True, exist_ok=True)
    configure(paths.logs, verbose=args.verbose)


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _open_browser(url: str) -> bool:
    """Abre la URL en el navegador. En WSL, en el navegador de Windows."""
    if _is_wsl():
        if shutil.which("wslview"):
            return subprocess.run(["wslview", url], check=False,
                                  capture_output=True).returncode == 0
        if shutil.which("powershell.exe") and "'" not in url:
            return subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"],
                check=False, capture_output=True).returncode == 0
        return False
    try:
        return webbrowser.open(url)
    except webbrowser.Error:
        return False


def cmd_tiktok_config(args: argparse.Namespace) -> int:
    """Que hay configurado. Sin red."""
    app = load_tiktok_app_config()
    service = TikTokService.from_environment()
    credentials = service.credentials
    print("TikTok - configuracion local (sin llamadas a la API)\n")
    path = app.secrets_file
    if path.is_file():
        mode = format(stat.S_IMODE(path.stat().st_mode), "04o")
        _print_kv("Fichero de credenciales", f"{path} (modo {mode})")
        if mode not in ("0600", "0400"):
            print(f"  {WARN} Deberia tener permisos 600: chmod 600 {path}")
    else:
        _print_kv("Fichero de credenciales", f"{path} (no existe)")
    for key in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN",
                "TIKTOK_REFRESH_TOKEN", "TIKTOK_OPEN_ID"):
        source = app.sources.get(key)
        _print_kv(key, f"presente ({source})" if source else "ausente")
    _print_kv("Redirect URI", app.redirect_uri)
    _print_kv("Scopes que se pediran", ",".join(DEFAULT_TIKTOK_SCOPES))
    if credentials is not None:
        _print_kv("Scopes concedidos", ",".join(credentials.scopes) or "desconocidos")
        _print_kv("open_id", credentials.open_id or "desconocido")
        expires = credentials.access_token_expires_at
        _print_kv("Access token caduca", human_local(expires) if expires else "desconocido")
        refresh = credentials.refresh_token_expires_at
        _print_kv("Refresh token caduca", human_local(refresh) if refresh else "desconocido")
        _print_kv("Refresco automatico",
                  "posible" if credentials.can_refresh else
                  "NO posible (falta client secret o refresh token)")
    print()
    if app.missing:
        print(f"{DOT} Siguiente paso: rellenar {', '.join(app.missing)} y ejecutar "
              "`gongora tiktok login`.")
    elif credentials is None:
        print(f"{DOT} App configurada, cuenta sin conectar. Siguiente paso: "
              "`gongora tiktok login`.")
    else:
        print(f"{TICK} Hay tokens guardados. Comprueba que TikTok los acepta con: "
              "`gongora tiktok check`.")
    return 0


def cmd_tiktok_login(args: argparse.Namespace) -> int:
    _tiktok_logging(args)
    try:
        login = TikTokLogin(load_tiktok_app_config())
        scopes = tuple(s for s in (args.scopes or ",".join(DEFAULT_TIKTOK_SCOPES))
                       .split(",") if s)
        request = login.start(scopes)
        print("Login de TikTok (OAuth Desktop con PKCE)\n")
        _print_kv("Redirect URI", request.redirect_uri)
        _print_kv("Scopes", ",".join(request.scopes))
        print("\nURL de autorizacion (abrela si el navegador no se abre solo):\n")
        print(f"  {request.url}\n")

        if args.paste:
            if not args.no_browser:
                _open_browser(request.url)
            print("Autoriza en el navegador. Te llevara a una pagina de localhost que "
                  "puede no cargar: es normal. Copia la URL COMPLETA de la barra de "
                  "direcciones y pegala aqui (no se mostrara en pantalla).")
            pasted = getpass.getpass("URL de redireccion: ")
            result = login.complete_with_url(request, pasted)
        else:
            def ready() -> None:
                port = urllib.parse.urlsplit(request.redirect_uri).port
                print(f"Esperando el redirect en {args.bind}:{port} "
                      f"(maximo {args.timeout} s, Ctrl+C para cancelar)...")
                if not args.no_browser and not _open_browser(request.url):
                    print(f"{WARN} No se pudo abrir el navegador: abre la URL a mano.")
            result = login.complete_with_server(request, timeout_seconds=args.timeout,
                                                bind_host=args.bind, on_ready=ready)
    except GongoraError as exc:
        print(f"{CROSS} {exc.safe_message}", file=sys.stderr)
        return 1

    tokens = result.tokens
    print(f"\n{TICK} Cuenta autorizada. Tokens guardados en {login.app.secrets_file} (600).")
    _print_kv("open_id", tokens.open_id or "no devuelto")
    _print_kv("Scopes concedidos", ",".join(tokens.scopes) or "no devueltos")
    _print_kv("Access token caduca", human_local(tokens.access_expires_at))
    if tokens.refresh_expires_at:
        _print_kv("Refresh token caduca", human_local(tokens.refresh_expires_at))
    if result.missing_scopes:
        print(f"{WARN} TikTok no concedio: {', '.join(result.missing_scopes)}. Revisa los "
              "productos y scopes de la app en TikTok for Developers.")
    print("\nSiguiente paso: gongora tiktok check")
    return 0


def cmd_tiktok_refresh(args: argparse.Namespace) -> int:
    _tiktok_logging(args)
    service = TikTokService.from_environment()
    session = service.session
    if session is None:
        print(f"{CROSS} No hay tokens guardados. Ejecuta: gongora tiktok login")
        return 1
    try:
        session.refresh(reason="manual")
    except GongoraError as exc:
        print(f"{CROSS} {exc.safe_message}", file=sys.stderr)
        return 1
    expires = session.credentials.access_token_expires_at
    print(f"{TICK} Token refrescado y guardado. Caduca: "
          f"{human_local(expires) if expires else 'desconocido'}")
    return 0


def cmd_tiktok_check(args: argparse.Namespace) -> int:
    """Prueba real: perfil, estadisticas y ultimos videos de la cuenta conectada."""
    _tiktok_logging(args)
    service = TikTokService.from_environment()
    try:
        profile = service.profile()
        _section("Perfil de TikTok")
        _print_kv("open_id", profile.open_id or "-")
        _print_kv("display_name", profile.display_name or "-")
        _print_kv("username", profile.username or "(sin scope user.info.profile)")
        _print_kv("is_verified", "-" if profile.is_verified is None else str(profile.is_verified))
        _print_kv("profile_deep_link", profile.profile_deep_link or "-")
        if profile.bio is not None and not profile.bio.is_empty:
            _print_kv("bio_description (dato externo)", profile.bio.for_display(limit=120))

        _section("Estadisticas (user.info.stats)")
        if not profile.stats:
            print("  Sin contadores en la respuesta (hueco, no cero).")
        for name in ("follower_count", "following_count", "likes_count", "video_count"):
            _print_kv(name, str(profile.stats[name]) if name in profile.stats
                      else "ausente en la respuesta")

        videos = service.recent_videos(limit=args.videos) if args.videos > 0 else []
        if args.videos > 0:
            _section(f"Ultimos {args.videos} videos (video.list)")
            if not videos:
                print("  La API no devolvio videos.")
            for video in videos:
                when = human_local(video.created_at) if video.created_at else "fecha desconocida"
                counters = " ".join(f"{k}={v}" for k, v in video.counters.items()) or "sin contadores"
                print(f"  - {video.id} | {when} | {counters}")
                text = (video.title if not video.title.is_empty else video.description)
                if not text.is_empty:
                    print(f"      texto (dato externo): {text.for_display(limit=90)}")
                if video.share_url:
                    print(f"      {video.share_url}")
    except ConnectorNotAuthorized as exc:
        print(f"{DOT} {exc.safe_message}")
        return 1
    except GongoraError as exc:
        print(f"{CROSS} {exc.safe_message}", file=sys.stderr)
        return 1
    print(f"\n{TICK} Cuenta de TikTok conectada y respondiendo. "
          f"Llamadas a la API: {service.api_calls}.")
    return 0


# ---------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gongora",
        description="Gongora Studio: sistema de agentes del proyecto musical GONGORA.",
    )
    parser.add_argument("--version", action="version", version=f"gongora {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Muestra el detalle de la ejecucion por consola.")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Comprueba configuracion, conectividad y capacidades.")
    doctor.set_defaults(func=cmd_doctor)

    collect = sub.add_parser("collect", help="Ejecuta una recogida real de datos.")
    collect.add_argument("--platform", action="append",
                         choices=[str(p) for p in ALL_PLATFORMS],
                         help="Limita la recogida a una plataforma (repetible).")
    collect.add_argument("--max-media", type=int, default=200,
                         help="Tope de publicaciones a recorrer (por defecto 200).")
    collect.add_argument("--force", action="store_true",
                         help="Ignora el intervalo minimo entre recogidas.")
    collect.add_argument("--trigger", default="manual",
                         help="Etiqueta del disparador (manual, timer, task...).")
    collect.add_argument("--no-report", action="store_true",
                         help="No genera informe.")
    collect.add_argument("--no-dispatch", action="store_true",
                         help="No crea la tarea para Luna.")
    collect.set_defaults(func=cmd_collect)

    report = sub.add_parser("report", help="Genera el informe de una recogida.")
    report.add_argument("--run", help="ID de recogida (por defecto, la ultima).")
    report.add_argument("--stdout", action="store_true",
                        help="Escribe el informe por consola en vez de a fichero.")
    report.set_defaults(func=cmd_report)

    agents = sub.add_parser("agents", help="Roles, workers, estado y errores.")
    agents.set_defaults(func=cmd_agents)

    tasks = sub.add_parser("tasks", help="Cola de tareas entre agentes.")
    tasks_sub = tasks.add_subparsers(dest="tasks_command", required=True)

    tasks_list = tasks_sub.add_parser("list", help="Lista tareas.")
    tasks_list.add_argument("--agent", help="Filtra por destinatario (luna, faro...).")
    tasks_list.add_argument("--status", help="Filtra por estados separados por comas.")
    tasks_list.add_argument("--platform", choices=[str(p) for p in ALL_PLATFORMS])
    tasks_list.add_argument("--limit", type=int, default=50)
    tasks_list.set_defaults(func=cmd_tasks_list)

    tasks_show = tasks_sub.add_parser("show", help="Detalle de una tarea con eventos.")
    tasks_show.add_argument("task_id")
    tasks_show.set_defaults(func=cmd_tasks_show)

    tasks_request = tasks_sub.add_parser(
        "request-collection", help="Encola una peticion de recogida para Faro.")
    tasks_request.add_argument("--platform", action="append",
                               choices=[str(p) for p in ALL_PLATFORMS])
    tasks_request.add_argument("--max-media", type=int, default=200)
    tasks_request.add_argument("--force", action="store_true")
    tasks_request.add_argument("--reason")
    tasks_request.add_argument("--idempotency-key")
    tasks_request.set_defaults(func=cmd_tasks_request_collection)

    worker = sub.add_parser("worker", help="Ejecuta una tarea de un agente y termina.")
    worker.add_argument("--agent", required=True, choices=list(BY_NAME))
    worker.add_argument("--once", action="store_true",
                        help="Obligatorio: una tarea y salir.")
    worker.set_defaults(func=cmd_worker)

    token = sub.add_parser("token", help="Estado y renovacion del token de Meta.")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_status = token_sub.add_parser("status", help="Validez, caducidad y permisos.")
    token_status.set_defaults(func=cmd_token_status)
    token_renew = token_sub.add_parser(
        "renew", help="Token de larga duracion y token de Pagina (requiere app secret).")
    token_renew.add_argument("--write", action="store_true",
                             help="Escribe el token resultante en el fichero de secretos.")
    token_renew.set_defaults(func=cmd_token_renew)

    schedule = sub.add_parser("schedule", help="Estado y control de la recogida programada.")
    schedule.add_argument("action",
                          choices=["status", "enable", "disable", "start", "stop",
                                   "run", "logs"])
    schedule.set_defaults(func=cmd_schedule)

    tiktok = sub.add_parser("tiktok", help="Conexion OAuth de TikTok y prueba de lectura.")
    tiktok_sub = tiktok.add_subparsers(dest="tiktok_command", required=True)

    tiktok_config = tiktok_sub.add_parser(
        "config", help="Muestra que hay configurado (sin red, sin secretos).")
    tiktok_config.set_defaults(func=cmd_tiktok_config)

    tiktok_login = tiktok_sub.add_parser(
        "login", help="Inicia el OAuth (PKCE) y guarda los tokens.")
    tiktok_login.add_argument("--no-browser", action="store_true",
                              help="No intenta abrir el navegador; solo imprime la URL.")
    tiktok_login.add_argument("--paste", action="store_true",
                              help="Sin servidor local: pega a mano la URL de redireccion.")
    tiktok_login.add_argument("--bind", default="127.0.0.1",
                              help="Interfaz donde escucha el callback (por defecto 127.0.0.1).")
    tiktok_login.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                              help="Segundos de espera del redirect (por defecto 300).")
    tiktok_login.add_argument("--scopes",
                              help="Scopes separados por comas (por defecto los del proyecto).")
    tiktok_login.set_defaults(func=cmd_tiktok_login)

    tiktok_refresh = tiktok_sub.add_parser("refresh", help="Fuerza el refresco del access token.")
    tiktok_refresh.set_defaults(func=cmd_tiktok_refresh)

    tiktok_check = tiktok_sub.add_parser(
        "check", help="Prueba real: perfil, estadisticas y ultimos videos.")
    tiktok_check.add_argument("--videos", type=int, default=5,
                              help="Cuantos videos recientes listar (0 = ninguno).")
    tiktok_check.set_defaults(func=cmd_tiktok_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"{CROSS} {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrumpido.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
