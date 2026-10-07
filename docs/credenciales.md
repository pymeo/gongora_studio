# Credenciales y acceso duradero

## Donde viven

| Plataforma | Fichero | Permisos |
|---|---|---|
| Meta / Instagram | `~/.config/gongora/secrets.env` | `600` |
| TikTok | `~/.config/gongora/tiktok.env` | `600` |

Estan **fuera del repositorio** a proposito. `.gitignore` bloquea `*.env` por
si alguna vez aparece una copia dentro del proyecto.

El token se carga programaticamente (`gongora.config`), se registra en
`gongora.redaction` y no aparece en `repr`, logs, excepciones, payloads de
tareas ni informes. `gongora doctor` muestra solo una huella SHA-256 truncada.

## Estado del token de Meta

```bash
gongora token status
```

Consulta `GET /debug_token` y muestra validez, caducidad, app y permisos
concedidos, sin revelar el token.

## El problema: los tokens de usuario caducan

El token con el que arranco este proyecto era de **corta duracion** y caduco
el 2026-10-07 a las 11:00 CEST, unos minutos despues de la primera lectura
correcta. Eso no es un fallo del sistema: es como funcionan los tokens del
Explorador de la Graph API.

`gongora doctor` lo detecta, marca el token como `rejected`, **no reintenta** y
lo deja visible en el informe y en `gongora agents`.

## Camino a un acceso duradero (flujo oficial de Meta)

Verificado en la documentacion de Meta (octubre 2026):
*Facebook Login > Access Tokens > Get Long-Lived Tokens*.

### Paso 1 - token de usuario de larga duracion (~60 dias)

```
GET https://graph.facebook.com/v26.0/oauth/access_token
    ?grant_type=fb_exchange_token
    &client_id={app-id}
    &client_secret={app-secret}
    &fb_exchange_token={token-de-corta-duracion}
```

Debe hacerse **en servidor**, porque incluye el app secret.

### Paso 2 - token de Pagina (sin caducidad)

```
GET https://graph.facebook.com/v26.0/me/accounts
```

con el token de usuario de larga duracion. Segun Meta, los tokens de Pagina
asi obtenidos **no tienen fecha de caducidad** y solo se invalidan en ciertas
condiciones (cambio de contrasena, retirada de permisos, cambios en la app).

### Como ejecutarlo aqui

El mecanismo esta implementado y esperando las credenciales de la app:

```bash
# 1. Anade al fichero de secretos (sin comillas):
#      META_APP_ID=<id de la app>
#      META_APP_SECRET=<secreto de la app>
#    Se obtienen en developers.facebook.com -> tu app -> Configuracion -> Basica

# 2. Pon un token de usuario reciente en META_ACCESS_TOKEN y ejecuta:
gongora token renew --write

# 3. Comprueba:
gongora token status
gongora doctor
```

`--write` guarda el token resultante en el fichero de secretos de forma
atomica y con permisos `600`, **sin mostrarlo por pantalla**.

> **No esta completado.** Sin `META_APP_ID` y `META_APP_SECRET` el comando
> explica que faltan y no hace nada. No se inventan credenciales y no se
> promete renovacion automatica: el paso 1 exige un token de usuario valido
> obtenido con interaccion humana.

## Permisos de Meta para este flujo

Instagram API with Facebook Login, sobre `graph.facebook.com`:

| Permiso | Para que |
|---|---|
| `instagram_basic` | leer perfil y publicaciones de Instagram |
| `instagram_manage_insights` | leer insights |
| `pages_show_list` | listar las paginas que gestiona la persona |
| `pages_read_engagement` | leer metadatos y seguidores de la pagina |

Para capacidades que **todavia no estan verificadas**:

| Permiso | Capacidad | Estado |
|---|---|---|
| `instagram_content_publish` | publicar en el feed | sin verificar |
| `instagram_manage_comments` | crear, borrar u ocultar comentarios | sin verificar |
| `instagram_manage_messages` | leer y responder mensajes directos | sin verificar |

Los permisos `instagram_business_*` pertenecen a **Instagram API with Business
Login for Instagram**, que es otro flujo. Este proyecto **no** los usa, y
`gongora.config` rechaza la configuracion si `META_AUTH_FLOW` apunta a el.

## Credenciales de TikTok

Ver [`tiktok-conexion.md`](tiktok-conexion.md). Van en un fichero aparte: su
ciclo de vida es distinto (token de acceso de 24 horas, refresh de 365 dias) y
no debe mezclarse con el de Meta.
