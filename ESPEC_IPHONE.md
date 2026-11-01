# Servidor para ver y mover la cámara desde el iPhone

Crea `servidor_iphone.py`: un programa para Windows que corre en la PC y sirve, en la red local, una página web para el iPhone con video y controles de movimiento. Interfaz en español. Sin anuncios ni telemetría ni recursos externos. Solo biblioteca estándar más `av` (PyAV) y numpy. Python: `%TEMP%\vc\venv\Scripts\python.exe`. No instales nada. No modifiques `visor_camara.py`.

## Cámara
- IP: usa `--ip` o `ip` de `%APPDATA%\CamaraWifi\config.json`; si no existe, `192.168.1.100`. Sin contraseña. Si no conecta, busca la cámara por ONVIF.
- Video: `rtsp://<ip>:554/0/av1` (baja, por defecto) y `rtsp://<ip>:554/0/av0` (alta). H.265.
- Opciones de `av.open`: `rtsp_transport=tcp, fflags=nobuffer, flags=low_delay, max_delay=0, timeout=5000000, analyzeduration=0, probesize=32768`, con `timeout=(5, 5)`.
- Movimiento: POST SOAP 1.2 a `http://<ip>/onvif/PTZ`, `Content-Type: application/soap+xml; charset=utf-8`, sobre `<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body xmlns:t="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">...</s:Body></s:Envelope>`.
  - Mover: `<t:ContinuousMove><t:ProfileToken>Profile_1</t:ProfileToken><t:Velocity><tt:PanTilt x="0.5" y="0"/></t:Velocity></t:ContinuousMove>`. Derecha x=+0.5, izquierda x=-0.5, arriba y=+0.5, abajo y=-0.5.
  - Parar: `<t:Stop><t:ProfileToken>Profile_1</t:ProfileToken><t:PanTilt>true</t:PanTilt><t:Zoom>true</t:Zoom></t:Stop>`. Reintenta hasta 3 veces.

## Servidor
- `ThreadingHTTPServer` en `0.0.0.0`, puerto 8770 (`--puerto`). Al arrancar imprime en consola la dirección `http://<ip-local>:8770` para el iPhone (IP de la interfaz con salida a la red) y "Cierra esta ventana para detener". Protege los `print` por si `sys.stdout` es None. Silencia el log por petición.
- `GET /`: la página, HTML/CSS/JS en línea dentro del .py.
- `GET /stream?calidad=baja|alta`: `multipart/x-mixed-replace` con JPEG. Un único hilo por calidad decodifica RTSP y codifica a JPEG con `av.CodecContext.create("mjpeg", "w")` (formato yuvj420p, calidad razonable), y guarda solo el último JPEG; cada cliente envía siempre el más reciente, sin acumular atraso. Si no hay clientes de esa calidad durante 5 s, cierra la conexión RTSP y deja de consumir CPU. Reconecta sola con espera creciente de 1 a 10 s. Maneja la desconexión del cliente sin trazas de error. La resolución alta se mantiene: no reescales.
- `GET /estado`: JSON con `conectado`, `segundos_desde_ultimo_fotograma`, `calidad_activa`, `ultimo_error`, `movimiento_activo`.
- `POST /ptz` con cuerpo JSON: `{"x": n, "y": n}` para mover o `{"stop": true}` para parar, o `{"latido": true}` mientras se mantiene pulsado. `x` e `y` solo admiten -0.5, 0 o 0.5; otros valores devuelven 400. Responde 204 de inmediato: las órdenes las ejecuta un único hilo trabajador, que conserva un Stop pendiente y luego el movimiento más reciente, en ese orden, para que un cambio rápido de dirección no se pierda.
- Parada de seguridad en el servidor: durante un movimiento, si pasan 2 s sin `x/y` ni latido, o 30 s desde que empezó, envía Stop. Esto cubre iPhone bloqueado o wifi perdido. Al cerrar el programa (incluido Ctrl+C), envía Stop.
- `GET /manifest.webmanifest` e icono `GET /icono.png` (PNG 180×180 generado con zlib y struct, sin dependencias nuevas, un color sólido sencillo basta) para añadir a la pantalla de inicio. Metas: `apple-mobile-web-app-capable`, `apple-mobile-web-app-title` = "Cámara", `apple-touch-icon`, `viewport` con `viewport-fit=cover` y sin zoom.

## Página (iPhone, vertical y horizontal)
- Video en `<img src="/stream?calidad=...">` a todo el ancho, manteniendo proporción, fondo negro.
- Cruz grande de 4 botones ▲ ▼ ◀ ▶, mínimo 64×64 px, con separación. En horizontal, la cruz se superpone en una esquina con fondo semitransparente.
- Mantener pulsado mueve y soltar detiene. Usa Pointer Events con `setPointerCapture`. Llama a `preventDefault`. Mientras esté pulsado, envía `{"latido":true}` cada 700 ms. En `pointerup`, `pointercancel`, `lostpointercapture`, `blur`, `pagehide` y `visibilitychange` a oculto: Stop inmediato, con `fetch(..., {keepalive: true})`.
- CSS: `touch-action: none` en los botones; `user-select: none`, `-webkit-user-select: none`, `-webkit-touch-callout: none` en toda la página, para que mantener pulsado no abra menús de iOS.
- Selector Baja/Alta (recuerda la elección en localStorage, con try/catch). Baja por defecto.
- Línea de estado que consulta `/estado` cada 2 s: "En vivo", "Imagen detenida hace N s", "Sin conexión con la PC" si falla el fetch.
- Al volver a primer plano (`visibilitychange`, `pageshow`), o si la imagen falla o `/estado` indica que el stream no avanza, recarga el `<img>` con un parámetro nuevo para que no quede congelado.
- La página debe funcionar en Safari y en modo pantalla de inicio. Contraste claro, tema oscuro.

## Prueba sin mover la cámara
`--prueba SEGUNDOS`: sin abrir el puerto HTTP, decodifica la calidad baja durante ese tiempo y codifica JPEG. Imprime JSON con `fotogramas`, `bytes_jpeg_ultimo`, `error`. Sale con 0 si hubo fotogramas.

No ejecutes nada que envíe órdenes de movimiento a la cámara. Comprueba con py_compile y, si tienes red, con `--prueba 6`. Responde en pocas líneas.
