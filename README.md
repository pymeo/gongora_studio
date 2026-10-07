# Gongora Studio

Sistema de agentes para el proyecto musical **GONGORA** (album *Fuimos dos*).

Fase 1: **Faro** operativo sobre Instagram, con almacenamiento historico,
informe y recogida periodica. Arquitectura preparada para TikTok.

- Contexto completo para agentes: [`agents/_shared/context.md`](agents/_shared/context.md)
- Credenciales y acceso duradero: [`docs/credenciales.md`](docs/credenciales.md)
- Conectar TikTok: [`docs/tiktok-conexion.md`](docs/tiktok-conexion.md)
- Arquitectura: [`docs/arquitectura.md`](docs/arquitectura.md)

## Estado real

| Rol | Rol configurado | Worker implementado | Worker activo |
|---|---|---|---|
| **Faro** | si | si | si |
| **Luna** | si | **no** | no |
| **Ritmo** | si | **no** | no |
| **Enlace** | si | **no** | no |

Un fichero de instrucciones no es un agente operativo. `gongora agents`
distingue los tres estados.

| Plataforma | Lectura de perfil | Publicaciones | Metricas | Publicar |
|---|---|---|---|---|
| **Instagram** | verificada | verificada | verificada | sin verificar |
| **TikTok** | pendiente de conexion | pendiente | pendiente | no implementada |

## Instalacion

Python 3.12+ y SQLite. **Sin dependencias de ejecucion**: solo biblioteca
estandar (`urllib`, `sqlite3`, `json`). Eso hace la instalacion reproducible y
reduce a cero la superficie de supply-chain.

```bash
cd ~/proyectos/gongora
python3 -m venv .venv            # si falta ensurepip: ver "Problemas conocidos"
.venv/bin/pip install -e ".[dev]"
source .venv/bin/activate
```

Credenciales en `~/.config/gongora/secrets.env` (`chmod 600`), a partir de
[`.env.example`](.env.example).

## Comandos

```bash
gongora doctor                            # configuracion, conectividad, capacidades
gongora collect                           # recogida real (la que usa el temporizador)
gongora collect --platform instagram --force --max-media 50
gongora report                            # informe de la ultima recogida
gongora report --run <run-id> --stdout
gongora agents                            # roles, workers, estado, errores
gongora tasks list                        # cola de tareas
gongora tasks list --agent luna --status pending
gongora tasks show <task-id>              # payload, eventos, artefactos
gongora tasks request-collection          # encola una recogida para Faro
gongora worker --agent faro --once        # atiende una tarea y termina
gongora token status                      # validez y caducidad del token
gongora token renew --write               # acceso duradero (requiere app secret)
gongora schedule status|enable|disable|start|stop|run|logs
```

Datos locales en `var/` (configurable con `GONGORA_HOME`):

```
var/gongora.sqlite3     snapshots, tareas, ejecuciones, calidad de datos
var/reports/            informes en Markdown (faro-ultimo.md siempre apunta al mas reciente)
var/logs/gongora.log    registro con redaccion de secretos
```

## Recogida periodica

Cada 6 horas con systemd de usuario:

```bash
./ops/install-scheduler.sh        # instala y activa
gongora schedule status           # estado y proxima ejecucion
gongora schedule stop             # parar
gongora schedule enable           # reanudar
gongora schedule run              # ejecutar ahora
gongora schedule logs             # ultimas lineas del journal
```

Si no hay systemd de usuario: `./ops/install-cron.sh` (cron + `flock`).

**El equipo y WSL deben estar en marcha para que se ejecute.** Un temporizador
de usuario corre mientras hay sesion de usuario. Para que siga tras cerrar la
sesion: `sudo loginctl enable-linger $USER`.

Dos protecciones deliberadas:

- **Sin solapamiento**: cada recogida toma un `flock`. Si ya hay una en curso,
  la segunda sale sin lanzarse.
- **Sin rafaga tras un apagado**: el temporizador usa `Persistent=false`, asi
  que no recupera las ejecuciones perdidas; y la propia recogida se omite si la
  ultima satisfactoria fue hace menos de 2 horas (`--force` lo salta).

## Garantias

**Seguridad.** El token se carga programaticamente, se registra en el modulo de
redaccion y no puede salir: ni en logs, ni en errores, ni en payloads de
tareas, ni en informes, ni en las respuestas crudas almacenadas. Las URLs de
paginacion se validan contra una lista blanca de hosts y se les quitan las
credenciales. Los textos externos (pies de foto, comentarios) se envuelven en
`UntrustedText` y nunca se interpretan como instrucciones.

**Datos.** Una metrica ausente es un hueco, nunca un cero. Los valores
acumulados se comparan, nunca se suman entre snapshots. Las metricas conservan
el nombre de su API y no se mezclan entre redes. Las discrepancias se registran
con ambos numeros y sin inventar la causa.

**Cola.** Reclamacion atomica (`UPDATE ... RETURNING` en transaccion
`IMMEDIATE`), lease con recuperacion de tareas interrumpidas, clave de
idempotencia unica, tope de reintentos (`dead`, sin bucles), eventos y
resultados con referencias a artefactos.

**Modelos.** La recogida de Faro no consume ningun LLM. El puerto para Luna
esta preparado sobre las CLI ya instaladas (`claude -p`, `codex exec`), sin
claves de API ni gasto nuevo.

## Pruebas

```bash
.venv/bin/python -m pytest          # 123 pruebas
```

Cubren los riesgos relevantes: filtracion de secretos, idempotencia y
reclamacion concurrente, metricas ausentes y discrepantes, token rechazado,
paginacion y errores parciales, conector pendiente de conexion, y que un rol
configurado no cuente como worker activo.

## Problemas conocidos

**`python3 -m venv` falla con "ensurepip is not available"** (Ubuntu sin
`python3.14-venv`). Opciones:

```bash
sudo apt install python3.14-venv          # si tienes privilegios
# o, sin privilegios:
python3 -m venv --without-pip .venv
curl -sS -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py
.venv/bin/python /tmp/get-pip.py
```

## Alcance de esta fase

Autorizado: lecturas de Meta, trabajo local, programacion de la recogida.

No autorizado: publicar contenido, enviar mensajes o responder a personas,
activar servicios de pago. La publicacion futura exigira autorizacion por
campana y reglas explicitas.
