"""Raiz de composicion: une casos de uso con adaptadores concretos.

Un unico sitio donde se decide "que implementacion usa que puerto". Anadir una
plataforma es registrar otro gateway aqui.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gongora.adapters.meta.instagram_gateway import InstagramGateway
from gongora.adapters.persistence.db import open_database
from gongora.adapters.persistence.queue import SqliteTaskQueue
from gongora.adapters.persistence.repositories import (
    SqliteAgentStateRepository,
    SqliteRunRepository,
    SqliteSnapshotRepository,
)
from gongora.adapters.reporting.markdown import MarkdownReportBuilder
from gongora.adapters.tiktok.display_gateway import TikTokDisplayGateway
from gongora.application.collect import CollectMetrics
from gongora.application.dispatch import EnqueueEditorialBriefs
from gongora.application.ports import SocialDataGateway
from gongora.clock import SystemClock
from gongora.config import Settings, load_settings
from gongora.domain.platform import Platform
from gongora.logging_setup import configure


@dataclass
class Container:
    settings: Settings
    connection: sqlite3.Connection
    clock: Any
    snapshots: SqliteSnapshotRepository
    runs: SqliteRunRepository
    agent_state: SqliteAgentStateRepository
    queue: SqliteTaskQueue
    gateways: dict[Platform, SocialDataGateway]
    collector: CollectMetrics
    report_builder: MarkdownReportBuilder
    dispatcher: EnqueueEditorialBriefs

    def close(self) -> None:
        self.connection.close()

    @property
    def project_root(self) -> Path:
        for candidate in Path(__file__).resolve().parents:
            if (candidate / "pyproject.toml").is_file():
                return candidate
        return Path.cwd()


def build(*, settings: Settings | None = None, verbose: bool = False,
          run_id_holder: dict[str, str] | None = None) -> Container:
    config = settings or load_settings()
    configure(config.paths.logs, verbose=verbose)
    clock = SystemClock()
    connection = open_database(config.paths.db)

    snapshots = SqliteSnapshotRepository(connection, clock)
    runs = SqliteRunRepository(connection, clock)
    agent_state = SqliteAgentStateRepository(connection, clock)
    queue = SqliteTaskQueue(connection, clock)

    # Las respuestas crudas se guardan contra la recogida en curso. El holder
    # permite que el gateway no conozca el ciclo de vida del run.
    holder = run_id_holder if run_id_holder is not None else {}

    def make_sink(platform: Platform):
        def sink(endpoint: str, payload: Any) -> None:
            run_id = holder.get("run_id")
            if not run_id:
                return
            snapshots.save_raw_response(run_id=run_id, platform=platform,
                                        endpoint=endpoint, payload=payload)
        return sink

    gateways: dict[Platform, SocialDataGateway] = {
        Platform.INSTAGRAM: InstagramGateway.from_settings(
            config, raw_sink=make_sink(Platform.INSTAGRAM)),
        Platform.TIKTOK: TikTokDisplayGateway.from_settings(
            config, raw_sink=make_sink(Platform.TIKTOK)),
    }

    collector = CollectMetrics(
        gateways=gateways, snapshots=snapshots, runs=runs, agent_state=agent_state,
        clock=clock, min_interval_seconds=config.min_collection_interval_seconds,
        on_run_start=lambda run_id: holder.__setitem__("run_id", run_id),
    )
    report_builder = MarkdownReportBuilder(snapshots=snapshots, runs=runs, clock=clock)
    dispatcher = EnqueueEditorialBriefs(queue=queue, snapshots=snapshots, clock=clock)

    return Container(
        settings=config, connection=connection, clock=clock, snapshots=snapshots,
        runs=runs, agent_state=agent_state, queue=queue, gateways=gateways,
        collector=collector, report_builder=report_builder, dispatcher=dispatcher,
    )
