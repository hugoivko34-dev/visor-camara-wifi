# Panel de cámara: servidor y página web

Reemplaza la página simple para iPhone por un panel completo, usable desde el iPhone (en casa y fuera, por Tailscale) y desde el navegador de la PC. El servidor corre en la PC con Windows. Sin anuncios, sin telemetría, sin recursos externos (ni CDN, ni fuentes web). Interfaz en español.

Python: `%TEMP%\vc\venv\Scripts\python.exe` (PyAV 19, numpy). Solo biblioteca estándar + PyAV + numpy. Nada nuevo.

## Lo que la cámara sabe hacer (ya comprobado, no investigar de nuevo)

- Video RTSP H.265 sin contraseña: `rtsp://<ip>:554/0/av0` (1920x1080, ~820 kbps) y `/0/av1` (640x360, ~100 kbps, ~12 fps). Audio del micrófono en el mismo RTSP: PCMA 8 kHz mono.
- Movimiento ONVIF en `http://<ip>/onvif/PTZ`, perfil `Profile_1`, solo `ContinuousMove` (PanTilt x,y en {-0.5, 0, 0.5}; las diagonales funcionan) y `Stop`. La velocidad se ignora. No hay zoom. `RelativeMove`, `AbsoluteMove`, `GotoHomePosition`, `SetPreset`, `GetStatus`, `GetPresets` responden 400: NO implementarlos.
- Dispositivo ONVIF en `http://<ip>/onvif/device_service` (namespace `http://www.onvif.org/ver10/device/wsdl`): `GetDeviceInformation`, `GetSystemDateAndTime`, `GetNetworkInterfaces`, `GetUsers` (devuelve usuario y contraseña en claro), `SystemReboot`, `SetSystemDateAndTime` (sin probar). Media en `http://<ip>/onvif/Media` (`http://www.onvif.org/ver10/media/wsdl`): `GetProfiles`, `GetVideoEncoderConfigurations`.
- No existen: detección de movimiento, hablar por la cámara, ajustes de imagen, foto JPEG sin clave, presets. No hace falta ninguna credencial para nada de lo anterior.
- Envoltura SOAP: `<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body xmlns:t="<ns>" xmlns:tt="http://www.onvif.org/ver10/schema">...</s:Body></s:Envelope>`, `Content-Type: application/soap+xml; charset=utf-8`.

Lo que PyAV puede hacer en esta PC: decodificar HEVC, codificar JPEG (`mjpeg`), MP4 sin recodificar el video (remux HEVC), muxer `hls` con `hls_segment_type=fmp4`, codificar `aac`, `libopus`, `libx264`.

## Estructura de archivos

- `servidor_iphone.py`: entrada, vigilante, servidor HTTP y rutas. El nombre se conserva para no romper accesos directos ni el firewall. Se divide en módulos del mismo directorio:
  - `camara.py`: ONVIF (SOAP, movimiento, dispositivo), descubrimiento (multicast + barrido), IP guardada, vigilancia cada 30 s. Mueve aquí lo que ya existe.
  - `video.py`: conexión RTSP compartida por calidad, MJPEG, audio PCM, HLS, grabación de clips, fotos.
  - `panel/`: `index.html`, `app.js`, `style.css`, `manifest.webmanifest`, `icono.png` (generar el PNG con el código ya existente, guardarlo como archivo en el repo), `icono.svg` opcional.
- Rutas estáticas resueltas con `BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))`. `construir.bat` agrega `--add-data "%ROOT%panel;panel"` al empaquetado de `ServidorIphone` (sigue `--onedir --windowed`).
- Config en `%APPDATA%\CamaraWifi\config.json` (ya existe; conservar claves ajenas al guardar). Fotos en `Imágenes\CamaraWifi` (ruta de Windows via `SHGetKnownFolderPath` o `%USERPROFILE%\Pictures` como respaldo), clips en `Vídeos\CamaraWifi` (`%USERPROFILE%\Videos`). Registro en `%APPDATA%\CamaraWifi\servidor.log` (ya existe).

Conservar tal cual lo que ya funciona y está probado: vigilante con mutex y evento de detener, `--servidor`, `--detener`, `--prueba`, puerto 8770 con `ExclusiveHTTPServer`, descubrimiento, parada de seguridad del movimiento (2 s sin latido, 30 s máximo), `Content-Type: application/json` obligatorio en POST, manejo de cierre de consola.

## API HTTP (contrato entre servidor y página)

Todas las respuestas JSON llevan `Cache-Control: no-store`. Errores: JSON `{"error": "<mensaje en español>"}` con 400/404/409/500. Los POST exigen `Content-Type: application/json` (415 si no), salvo que se indique.

| Método y ruta | Qué hace |
| --- | --- |
| `GET /` | `panel/index.html`. `GET /panel/<archivo>` sirve los estáticos (solo nombres sin `..`, tipos: html, js, css, json, png, svg, webmanifest). |
| `GET /stream?calidad=baja|alta&fps=0..15&q=2..12` | MJPEG `multipart/x-mixed-replace`. `fps` limita las imágenes enviadas a ese cliente (0 = todas); `q` es la calidad JPEG (`qscale`, 2 mejor, por defecto 5). Un solo decodificador por calidad compartido por todos los clientes; la conexión RTSP se cierra 5 s después del último cliente. Conserva la lógica actual de último fotograma, reconexión y "la cámara dejó de enviar imagen". |
| `GET /audio` | `audio/L16; rate=8000; channels=1` (PCM s16le mono, chunked, sin fin). El servidor decodifica el audio solo mientras haya algún cliente de audio. Cola por cliente acotada a 0,5 s: si se llena, descarta lo más viejo. `Connection: close`. |
| `GET /hls/<calidad>/index.m3u8`, `GET /hls/<calidad>/<segmento>` | Modo "ahorro de datos" para Safari: HLS fMP4 con el video HEVC sin recodificar (etiqueta `hvc1`: `vout.codec_context.codec_tag = "hvc1"` si PyAV lo acepta; si no, probar la opción `tag` del muxer; si tampoco, dejar `hev1`) y audio AAC 8 kHz mono codificado desde el PCM. Segmentos de 1 s, lista de 4, `hls_flags=delete_segments+independent_segments+omit_endlist`, `hls_fmp4_init_filename=init.mp4`, en un directorio temporal por calidad. Se arranca con el primer pedido del m3u8 y se detiene 20 s después del último pedido. Mientras no exista el primer segmento, responder 503 con `Retry-After: 1`. Tipos: `application/vnd.apple.mpegurl`, `video/mp4`, `video/iso.segment`. |
| `GET /estado` | `{"conectado", "segundos_desde_ultimo_fotograma", "calidad_activa", "ultimo_error", "ip_camara", "movimiento_activo", "error_movimiento", "barrido", "fps", "kbps", "clientes_video", "clientes_audio", "grabando": {"activo", "segundos", "archivo"} o null, "servidor": {"version", "inicio" (ISO), "segundos_activo", "ip_local", "ip_tailscale" (o null), "puerto"}}`. `fps` y `kbps` medidos sobre los últimos 3 s del decodificador de la calidad pedida (`?calidad=`). `ip_tailscale`: primera IPv4 propia dentro de `100.64.0.0/10` (enumerar con `socket.getaddrinfo(socket.gethostname(), None)` y, si existe, `%ProgramFiles%\Tailscale\tailscale.exe ip -4` con timeout 3 s, cacheado 60 s). |
| `POST /ptz` | Como hoy: `{"x","y"}` (valores en {-0.5,0,0.5}, no ambos 0), `{"stop":true}`, `{"latido":true}`. Nuevo: `{"barrido": true, "eje": "x"|"y", "segundos": 2..20}` alterna sentido cada N s hasta `{"barrido": false}` o `{"stop":true}` o cualquier `x,y`. El barrido usa el mismo trabajador y respeta el límite de 30 s por tramo (cada tramo es una orden nueva). |
| `POST /foto` | Guarda el último fotograma de la calidad indicada (`{"calidad":"alta"}`; si esa calidad no está activa, abre la conexión, espera hasta 6 s el primer fotograma y la cierra) como `foto_AAAA-MM-DD_HH-MM-SS.jpg` en Imágenes\CamaraWifi. Responde `{"archivo": nombre, "bytes": n}`. |
| `POST /grabar` | `{"segundos": 5..600, "calidad": "alta"|"baja"}` inicia un clip MP4 (video HEVC remux desde el siguiente fotograma clave, etiqueta `hvc1`, audio AAC desde PCM, `movflags=+faststart`) en Vídeos\CamaraWifi como `clip_AAAA-MM-DD_HH-MM-SS.mp4`. Solo una grabación a la vez (409 si ya hay). `{"detener": true}` termina antes. Responde `{"archivo": nombre}`. Al terminar, el archivo debe abrirse con PyAV y tener duración > 0 (comprobarlo en `--prueba-panel`). |
| `GET /archivos` | Lista de fotos y clips: `[{"nombre","tipo":"foto"|"clip","bytes","fecha":ISO}]` ordenada por fecha descendente, máximo 200. |
| `GET /archivos/<nombre>` | Descarga (`image/jpeg` o `video/mp4`, `Content-Disposition: attachment`, soporta `Range` para que Safari reproduzca el MP4). Solo nombres que empiecen por `foto_` o `clip_`, sin rutas. `?miniatura=1` en una foto devuelve el JPEG reducido a 320 px de ancho (PyAV reformat). |
| `DELETE /archivos/<nombre>` | Borra el archivo. 404 si no existe. |
| `GET /camara/info` | ONVIF cacheado 60 s: `{"fabricante","modelo","firmware","serie","hora_camara" (ISO, UTC), "hora_pc" (ISO UTC), "desfase_segundos", "zona_horaria", "red": [{"nombre","mac","ip","dhcp"}], "perfiles": [{"nombre","codec","ancho","alto"}], "usuarios": [{"usuario","nivel","clave_visible": true}]}`. La contraseña NO se incluye en esta respuesta. |
| `GET /camara/clave` | Devuelve `{"usuarios":[{"usuario","clave"}]}` (lo que la cámara entrega sin autenticar). Solo se llama cuando el usuario pulsa "mostrar". |
| `POST /camara/hora` | `SetSystemDateAndTime` con la hora UTC de la PC, `DateTimeType=Manual`, misma zona horaria que informe la cámara. Responde `{"ok": true, "desfase_segundos": n}` releyendo la hora. Si la cámara lo rechaza, `{"error": ...}` 500. |
| `POST /camara/reiniciar` | `SystemReboot`. Antes envía Stop. Responde `{"ok": true, "mensaje": <texto que devuelva la cámara>}`. |
| `POST /camara/buscar` | Ejecuta la búsqueda ahora (ignorando el límite de 15 s) y responde `{"ip": ip, "encontrada": bool}`. |
| `POST /camara/ip` | `{"ip": "a.b.c.d"}` valida formato, guarda y reconecta. |
| `GET /servidor/registro?lineas=200` | Últimas líneas del registro como `{"lineas":[...]}`. |
| `POST /servidor/reiniciar` | Responde `{"ok": true}` y 0,5 s después termina el proceso con código 3 para que el vigilante lo relance. Antes envía Stop a la cámara. |
| `GET /manifest.webmanifest`, `GET /icono.png` | Como hoy (`display: standalone`, nombre "Cámara"). |

Métricas: el decodificador cuenta fotogramas y bytes de paquetes por segundo en una ventana deslizante de 3 s.

Versión: constante `VERSION = "2.0"` en `servidor_iphone.py`.

## Página (`panel/`)

Diseño pensado para iPhone en vertical y horizontal, y utilizable en escritorio (ancho > 900 px: video a la izquierda, controles a la derecha). Tema oscuro, contraste alto, botones mínimo 44x44 px. `user-select: none`, `-webkit-touch-callout: none`, `touch-action: manipulation` en body y `none` en los controles de movimiento. `viewport-fit=cover`, metas `apple-mobile-web-app-capable`, `apple-mobile-web-app-title=Cámara`, `apple-touch-icon`. Sin frameworks: JS plano, un solo `app.js`.

### Zona de video
- `<img id="video">` con el MJPEG (`/stream?calidad=&fps=&q=`), fondo negro, proporción 16:9, ocupa el ancho. Botón de pantalla completa (API Fullscreen donde exista; en iPhone, que la propia página ya sea a pantalla completa en modo inicio).
- Superpuestos sobre el video, con fondo semitransparente: estado ("En vivo", "Conectando…", "Imagen detenida hace N s", "Sin conexión con la PC"), y `fps`/`kbps` pequeños.
- Modo "Ahorro de datos (beta)": sustituye el `<img>` por `<video playsinline muted autoplay>` con `src=/hls/<calidad>/index.m3u8`, solo si `video.canPlayType('application/vnd.apple.mpegurl')` es verdadero; si no, el botón aparece deshabilitado con el texto "Solo en Safari". Mientras el m3u8 responda 503, reintentar cada segundo hasta 15 s. En este modo el audio viene en el HLS: el botón de sonido quita el `muted`.
- Arrastrar sobre el video mueve la cámara (activable en Ajustes, apagado por defecto): el desplazamiento desde el punto de contacto decide la dirección (8 direcciones, umbral 24 px); al soltar, Stop. Pointer Events con `setPointerCapture`.
- Recarga automática del `<img>` al volver a primer plano, al fallar o si `/estado` indica imagen detenida ≥ 3 s (como hoy).

### Controles de movimiento
- Cruz de 8 botones (▲ ▼ ◀ ▶ y 4 diagonales) + botón central "Detener". Mantener pulsado mueve, soltar detiene, con latido cada 700 ms (como hoy). Teclado en escritorio: flechas (y combinaciones para diagonales), espacio = Detener.
- "Barrido": botón que alterna; selector de eje (horizontal/vertical) y de segundos por tramo (3, 5, 8, 12). Mientras está activo, la cruz lo cancela al tocarla.
- Parada de seguridad en el cliente: `pointerup`, `pointercancel`, `lostpointercapture`, `blur`, `pagehide`, `visibilitychange` oculto → Stop con `keepalive`.

### Botones de acción (barra bajo el video)
- **Sonido**: enciende el audio con Web Audio: `fetch('/audio')` leyendo el cuerpo en streaming, convierte s16le a Float32, crea `AudioBuffer` de 8000 Hz y los programa encadenados con 0,25 s de adelanto; si el atraso supera 1 s, descarta. Control de volumen (GainNode). Debe funcionar en Safari iOS tras el toque del usuario (crear/`resume()` el `AudioContext` dentro del manejador del click). Al apagar, aborta el fetch.
- **Foto**: `POST /foto` con la calidad alta; muestra aviso "Foto guardada" con enlace de descarga.
- **Grabar**: menú con 15 s, 30 s, 1 min, 5 min, y "hasta que detenga". Mientras graba: contador y botón "Detener". Al terminar, aviso con enlace.
- **Calidad**: Baja/Alta. **Datos**: selector "Fluido" (fps 0, q 5), "Medio" (fps 6, q 6), "Mínimo" (fps 3, q 8), y "Ahorro (beta)" (HLS). Mostrar debajo una estimación de MB por hora calculada con el `kbps` real de `/estado`.

### Secciones (pestañas bajo los controles)
1. **Archivos**: cuadrícula de fotos (miniaturas) y clips (icono + duración/tamaño). Tocar una foto la abre; un clip se reproduce con `<video controls playsinline>`. Botones: Descargar (enlace directo; en iPhone, Safari permite guardar en Fotos desde Compartir) y Borrar (con confirmación). Actualizar al terminar una foto/grabación.
2. **Cámara**: tarjeta de información (fabricante, modelo, firmware, serie, resoluciones, red). Hora de la cámara con el desfase respecto a la PC y botón "Sincronizar hora". IP actual, botón "Buscar cámara" y campo para fijar IP a mano. Aviso de seguridad: "La cámara entrega su usuario y contraseña a cualquiera de la red sin pedir nada" con botón "mostrar" que llama a `/camara/clave`. Botón "Reiniciar cámara" con confirmación (avisar que tarda alrededor de un minuto).
3. **Servidor**: direcciones para abrir el panel (local y Tailscale, con botón copiar), versión, tiempo activo, clientes, últimas líneas del registro (botón actualizar), botón "Reiniciar servidor" con confirmación.
4. **Ayuda**: cómo añadir a la pantalla de inicio en iPhone, qué hace Tailscale, qué significa cada modo de datos, atajos de teclado.

### Ajustes persistentes (localStorage, con try/catch)
calidad, modo de datos, volumen, arrastrar-para-mover, pestaña activa, eje y segundos del barrido.

### Comportamiento general
- `/estado` cada 2 s (pausado mientras la página está oculta). Si falla, "Sin conexión con la PC".
- Todo texto en español, sin anglicismos innecesarios. Avisos (toasts) que desaparecen a los 4 s.
- Accesibilidad básica: `aria-label` en botones de icono, foco visible.

## Modo de prueba `--prueba-panel SEGUNDOS [--puerto N]`

Arranca el servidor real (sin vigilante) en el puerto dado y, desde un hilo, ejercita con `urllib` y sockets: `GET /` (contiene `<title>`), `/panel/app.js`, `/panel/style.css`, `/manifest.webmanifest`, `/icono.png`, `/estado`, `/stream?calidad=baja&fps=5` (cuenta JPEG durante 6 s y comprueba que no pase de ~6 fps), `/audio` (recibe ≥ 16000 bytes en 4 s), `POST /foto`, `POST /grabar {"segundos":4}` y espera, `GET /archivos` (contiene ambos), descarga del clip con `Range: bytes=0-99` (206), apertura del clip con PyAV (duración > 0, códec hevc), `DELETE` de ambos, `GET /camara/info` (fabricante no vacío), `/hls/baja/index.m3u8` hasta que responda 200 (máx. 15 s) y descarga de `init.mp4` y del primer segmento (ambos > 0 bytes), `POST /ptz {"stop":true}` (204), `POST /ptz` con texto plano (415), `/servidor/registro`. NO llama a reiniciar, hora, ni mueve la cámara. Imprime un JSON con un campo por comprobación (true/false o número) y `errores: [...]`; sale con 0 si todo pasó. Termina el servidor al final.

`--prueba` existente se conserva.

## Entregables y comprobación

- Comprueba `py_compile` de todos los `.py` y que `--prueba-panel 20 --puerto 8799` pase si hay red con la cámara. No ejecutes nada que mueva la cámara ni la reinicie.
- README.md: describe el panel, sus funciones, los modos de datos, el acceso remoto con Tailscale (instalar en PC e iPhone, misma cuenta, abrir `http://<ip-tailscale>:8770`), y las limitaciones reales de la cámara (sin zoom, sin presets, sin detección de movimiento, sin hablar). Sin datos personales: IP de ejemplo `192.168.1.100`.
