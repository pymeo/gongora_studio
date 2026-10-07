# Conectar TikTok: pasos concretos

Estado actual: **pendiente de conexion**. El adaptador existe y esta escrito
contra la Display API documentada, pero no se ha ejecutado nunca contra la API
real porque falta la autorizacion OAuth. Mientras falte:

- `gongora doctor` muestra TikTok como "pendiente de conexion",
- la recogida lo marca `skipped` y **no produce ningun dato**,
- cualquier lectura lanza `ConnectorNotAuthorized`.

Datos contrastados con la documentacion oficial de TikTok for Developers
(consultada el 2026-10-07).

## 1. Crear la app y anadir productos

En <https://developers.tiktok.com> → *Manage apps* → crear app.

Productos necesarios, **separados a proposito**:

| Producto | Para que | Lo necesitamos |
|---|---|---|
| **Login Kit** | obtener la autorizacion OAuth del usuario | si |
| **Display API** | leer perfil y videos con sus contadores | si |
| **Content Posting API** | publicar | **no en esta fase** |

Mantener lectura y publicacion separadas es deliberado: Faro y Luna solo
necesitan leer. Publicar es otra capacidad, de otro producto, con otra revision.

## 2. Scopes

Los que necesita Faro (nombres y descripciones oficiales):

| Scope | Da acceso a |
|---|---|
| `user.info.basic` | perfil basico: `open_id`, `avatar_url`, `display_name` |
| `user.info.stats` | `follower_count`, `following_count`, `likes_count`, `video_count` |
| `video.list` | videos publicos del usuario, con `view_count`, `like_count`, `comment_count`, `share_count` |

`user.info.stats` es imprescindible: TikTok **movio** los contadores de cuenta
desde `user.info.basic` a este scope. Pedir solo `user.info.basic` devuelve el
perfil sin numeros.

`user.info.profile` (bio, enlaces, verificado) es opcional: no lo pedimos.

## 3. Redirect URI y OAuth

Configurar un *redirect URI* en los ajustes de Login Kit. Para una herramienta
local sirve algo como `http://localhost:8765/callback`.

**Autorizacion** (navegador, interaccion humana obligatoria):

```
https://www.tiktok.com/v2/auth/authorize/
    ?client_key={client_key}
    &scope=user.info.basic,user.info.stats,video.list
    &response_type=code
    &redirect_uri={redirect_uri}
    &state={valor-aleatorio}
```

**Canje del codigo por tokens**:

```
POST https://open.tiktokapis.com/v2/oauth/token/
Content-Type: application/x-www-form-urlencoded

client_key={client_key}
&client_secret={client_secret}
&code={codigo-del-callback}
&grant_type=authorization_code
&redirect_uri={redirect_uri}
```

### Ciclo de vida de los tokens (importante)

| Token | Validez oficial |
|---|---|
| `access_token` | **24 horas** |
| `refresh_token` | **365 dias** |

Renovacion:

```
POST https://open.tiktokapis.com/v2/oauth/token/
client_key=...&client_secret=...&grant_type=refresh_token&refresh_token=...
```

> Consecuencia de diseno: a diferencia de Meta, en TikTok **no existe** un
> token que no caduque. Una recogida cada 6 horas obliga a renovar el token de
> acceso. Ese renovador **no esta implementado todavia**: es el primer trabajo
> al conectar TikTok de verdad. `TikTokCredentials.can_refresh` ya indica si
> hay material para hacerlo.

## 4. Guardar las credenciales

En `~/.config/gongora/tiktok.env`, con permisos `600`, **separado de Meta**:

```bash
install -m 600 /dev/null ~/.config/gongora/tiktok.env
cat > ~/.config/gongora/tiktok.env <<'FIN'
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_ACCESS_TOKEN=
TIKTOK_REFRESH_TOKEN=
TIKTOK_OPEN_ID=
TIKTOK_SCOPES=user.info.basic,user.info.stats,video.list
FIN
chmod 600 ~/.config/gongora/tiktok.env
```

Con `TIKTOK_CLIENT_KEY` y `TIKTOK_ACCESS_TOKEN` presentes, el conector deja de
estar "pendiente" e intenta conectar. Si falta algun scope declarado, avisa en
vez de dar por buena la conexion.

Comprobar:

```bash
gongora doctor
gongora collect --platform tiktok
```

## 5. Endpoints que usa el adaptador

Host: `https://open.tiktokapis.com`, en lista blanca.

| Que | Peticion |
|---|---|
| Perfil | `GET /v2/user/info/?fields=open_id,union_id,display_name,follower_count,following_count,likes_count,video_count` |
| Videos | `POST /v2/video/list/?fields=id,create_time,video_description,duration,share_url,view_count,like_count,comment_count,share_count` con cuerpo `{"max_count": 20, "cursor": ...}` |

`max_count` admite como maximo **20**. La respuesta trae `data.videos`,
`data.cursor` y `data.has_more`.

TikTok responde HTTP 200 con un sobre `{"data": ..., "error": {"code": "ok", ...}}`:
el cliente clasifica por `error.code`, no solo por el estado HTTP.

## 6. Revision de la app

Los scopes de lectura requieren aprobacion para salir del modo sandbox (con
app sin auditar, solo usuarios de prueba). Hay que enviar la app a revision
describiendo el uso real: analitica interna de una cuenta propia.

## 7. Publicacion: lo que NO se da por hecho

La Content Posting API es otro producto. Dos modalidades:

| Scope | Que hace |
|---|---|
| `video.publish` | *Direct Post*: publica directamente en el perfil |
| `video.upload` | sube como **borrador** para editar y publicar en la app |

Dato oficial importante: **todo el contenido publicado por clientes sin
auditar queda restringido a visibilidad privada.** Para levantar esa
restriccion hay que pasar una auditoria de cumplimiento de los Terms of
Service.

Por eso este proyecto **no presupone** que una herramienta privada obtendra
aprobacion de Direct Post. Si se implementa publicacion en TikTok, la ruta
realista es `video.upload` (borrador que una persona publica), y Direct Post
solo despues de una auditoria concedida.

Crear contenido y publicarlo seguiran siendo capacidades separadas
(Ritmo produce, Enlace publica), con autorizacion por campana.

## Resumen: lista de pasos

1. [ ] Crear app en TikTok for Developers.
2. [ ] Anadir **Login Kit** y **Display API** (no Content Posting).
3. [ ] Configurar redirect URI.
4. [ ] Solicitar `user.info.basic`, `user.info.stats`, `video.list`.
5. [ ] Enviar la app a revision para salir de sandbox.
6. [ ] Completar el OAuth en el navegador y canjear el codigo.
7. [ ] Guardar las credenciales en `~/.config/gongora/tiktok.env` (`600`).
8. [ ] `gongora doctor` → TikTok debe aparecer conectado.
9. [ ] `gongora collect --platform tiktok` → primera recogida real.
10. [ ] **Implementar el renovador del token de 24 horas** (sin el, la
       recogida programada dejara de funcionar al dia siguiente).
