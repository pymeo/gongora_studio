# Arquitectura

DDD pragmatico: tres capas, sin ceremonia de mas.

```
src/gongora/
  domain/            tipos y reglas, sin infraestructura
    platform.py        Platform (instagram, tiktok)
    metrics.py         catalogo por plataforma, nombres originales
    models.py          AccountProfile, MediaItem, MetricSnapshot, MetricGap...
    tasks.py           contratos de tarea con version de esquema
    untrusted.py       UntrustedText: texto externo como dato
    errors.py          taxonomia de errores
  application/       casos de uso y puertos
    ports.py           SocialDataGateway, SnapshotRepository, LlmPort, Clock
    collect.py         recogida + calidad de datos
    dispatch.py        entrega del informe a Luna
    worker.py          FaroWorker, UnimplementedWorker
  adapters/          infraestructura concreta
    meta/              Instagram via graph.facebook.com v26.0
    tiktok/            Display API v2 (pendiente de conexion)
    persistence/       SQLite: esquema, repositorios, cola
    reporting/         informe Markdown
    llm/               CLI de claude y codex (para Luna)
  redaction.py       redaccion de secretos (transversal)
  container.py       raiz de composicion
  cli.py             interfaz de linea de comandos
```

La dependencia va siempre hacia dentro: `adapters` → `application` → `domain`.
El dominio no importa nada de infraestructura.

## Por que esta division

El valor esta en los **puertos**: `SocialDataGateway` es lo que hace que
anadir TikTok no toque ningun caso de uso. `CollectMetrics` no sabe si detras
hay Meta, TikTok o un doble de prueba.

No hay repositorios genericos, ni unidad de trabajo, ni event bus: con una
base SQLite local serian ceremonia sin beneficio.

## Anadir una plataforma

1. Registrar sus metricas en `domain/metrics.py` **con los nombres de su
   propia API**. No traducir ni normalizar: `views` y `view_count` son cosas
   distintas y el codigo lo impide (`assert_comparable`).
2. Escribir el adaptador en `adapters/<plataforma>/` cumpliendo
   `SocialDataGateway`: `is_configured`, `check_connection`, `fetch_profile`,
   `iter_media`, `fetch_insights`.
3. Cargar sus credenciales en un fichero propio (`config.py`), nunca mezcladas
   con las de otra plataforma.
4. Registrarlo en `container.build()`.

Eso es todo: `collect`, `report`, la cola y el CLI ya la tratan. No se duplican
agentes: los cuatro roles trabajan sobre todas las plataformas.

## La plataforma es parte de la identidad

Cada fila de `accounts`, `media`, `metric_snapshots`, `metric_gaps`,
`data_quality_issues`, `raw_responses`, `platform_outcomes` y `tasks` lleva su
`platform`. Las claves primarias son compuestas (`platform`, `id`): un
identificador de Instagram y uno de TikTok nunca colisionan ni se confunden.

## Decisiones y sus motivos

| Decision | Motivo |
|---|---|
| Cero dependencias de ejecucion | reproducibilidad y superficie de supply-chain nula |
| SQLite con WAL | una sola maquina, lecturas concurrentes, cero administracion |
| Cola en la misma base | las tareas se ven en el mismo sitio que los datos que las originan |
| `UPDATE ... RETURNING` para reclamar | atomico en SQLite 3.35+; sin carreras entre workers |
| Token en cabecera `Authorization` | no queda en URLs, logs ni historiales |
| Lista blanca de hosts | `paging.next` es una URL que viene de fuera |
| `UntrustedText` como tipo | obliga a decidir que se hace con el texto externo |
| Redaccion centralizada | un solo sitio que sabe ocultar secretos, usado en todas las salidas |
| `Persistent=false` en el timer | evita una rafaga de recogidas tras un apagado largo |
| `flock` en la recogida | el temporizador no puede pisarse a si mismo |

## Flujo de una recogida

```
gongora collect
  -> FileLock (sin solapamiento)
  -> CollectMetrics.execute
       guardia de intervalo minimo
       por cada plataforma:
         gateway.is_configured()?  no -> outcome "skipped", sin datos
         fetch_profile   -> snapshots de contadores (period=instant)
         iter_media      -> media + paginacion validada
         fetch_insights  -> snapshots (period=lifetime) + huecos
         calidad: suma de interacciones, metricas decrecientes, texto sospechoso
  -> MarkdownReportBuilder.write  -> var/reports/faro-<run>.md
  -> EnqueueEditorialBriefs       -> tarea pendiente para Luna por plataforma
```

Si Meta falla, el error saneado se conserva en `platform_outcomes.errors_json`
y aparece en el informe. Nunca se sustituye por datos simulados.
