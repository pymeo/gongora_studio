# Faro - recogida, calidad de datos y analisis

Lee primero [el contexto comun](_shared/context.md). Todo lo que dice ahi
aplica aqui y no se repite.

**Estado: worker implementado y activo.** Es el unico rol con worker en esta
fase.

## Mision

Que los datos de las redes de GONGORA existan, sean fiables y esten fechados.
Faro no decide contenido ni publica: observa, mide, detecta problemas y entrega.

## Responsabilidades

1. Comprobar configuracion y conectividad por plataforma (`gongora doctor`).
2. Leer el perfil de cada cuenta configurada.
3. Recorrer las publicaciones con paginacion, validando el host de cada pagina.
4. Pedir las metricas admitidas segun el tipo de publicacion.
5. Guardar snapshots con fecha UTC, plataforma, cuenta, publicacion, metrica,
   periodo y valor.
6. Conservar las respuestas utiles, saneadas: sin tokens ni URLs de paginacion.
7. Registrar huecos, discrepancias y errores parciales.
8. Generar el informe en Markdown y dejar una tarea para Luna que lo referencie.

## Como se comporta ante fallos

- **Metrica rechazada en bloque**: degrada a peticiones de una sola metrica
  para conservar las que si funcionan. Lo que falte va a huecos con su motivo.
- **Publicacion que falla**: error parcial, continua con la siguiente.
- **Paginacion interrumpida**: conserva lo recorrido y lo registra.
- **Token rechazado**: aborta la plataforma de inmediato, marca el estado
  `rejected` y **no reintenta**. El resto de plataformas sigue su curso.
- **Limite de uso de la plataforma**: backoff con respeto de `Retry-After` y de
  las cabeceras de consumo.

## Lo que Faro NO hace

- No consume ningun LLM. Su recogida programada es codigo puro.
- No publica, no comenta, no responde mensajes.
- No deduce horarios optimos, causalidad ni estrategia ganadora. Con una
  publicacion y pocas recogidas, cualquier patron es ruido.
- No convierte reproducciones en personas ni en escuchas de Spotify.
- No rellena huecos con ceros.
- No explica una discrepancia que no puede demostrar.

## Analisis que Faro si puede hacer

- Variacion de una metrica acumulada entre dos snapshots (resta, nunca suma).
- Deteccion de metricas acumuladas que bajan (anomalia a registrar).
- Comparacion de componentes de interaccion frente al total informado.
- Inventario de que se midio, que falto y cuando.

## Entradas y salidas

- Consume: `data.collection_request`
- Produce: `editorial.brief_request` (para Luna), informe Markdown en
  `var/reports/faro-<run-id>.md`

## Comandos propios

```
gongora doctor
gongora collect [--platform instagram] [--max-media N] [--force]
gongora report [--run <id>] [--stdout]
gongora worker --agent faro --once
```
