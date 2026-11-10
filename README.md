# Visor de cámara WiFi

Alternativa sin anuncios a la app YCC365 Plus para una cámara con RTSP y ONVIF. Todo corre en tu red local y en tu PC: nada pasa por la nube del fabricante.

## Qué incluye

- **Panel de cámara** (`servidor_iphone.py` + `panel/`): página web servida por la PC, pensada para el iPhone y usable desde cualquier navegador. Es la interfaz principal.
- **Visor de Windows** (`visor_camara.py`): programa de escritorio con video, sonido, fotos y control de movimiento.
- `camara.py`: ONVIF (movimiento, información, hora, reinicio) y búsqueda de la cámara en la red.
- `video.py`: conexión RTSP compartida, MJPEG, audio, HLS, fotos y clips.
- `construir.bat`: genera los ejecutables en `dist\` con PyInstaller, usando un entorno de Python en `%TEMP%\vc` (las rutas largas rompen la carga de algunas DLL).
- `ESPEC*.md`: especificaciones con las que se construyó cada parte.

## Panel de cámara

Abre `http://<ip-de-la-pc>:8770` desde el iPhone o desde la PC. En el iPhone, en Safari, toca Compartir → **Añadir a pantalla de inicio** para usarlo como una app.

Funciones:

- Video en calidad alta (1080p) o baja (360p), con tres niveles de consumo de datos: Fluido, Medio (6 imágenes por segundo) y Mínimo (3 por segundo). La página muestra una estimación de MB por hora.
- Modo **Ahorro (beta)**: HLS con el video original de la cámara sin recodificar y audio AAC. Solo en Safari. Tarda unos 10 s en arrancar y consume lo mismo que la cámara emite (~100 kbps en baja).
- Sonido del micrófono de la cámara.
- Fotos (guardadas en `Imágenes\CamaraWifi`) y clips MP4 de 15 s a 10 min (`Vídeos\CamaraWifi`), con galería para ver, descargar y borrar.
- Movimiento en 8 direcciones manteniendo pulsado, arrastrando sobre el video (opcional) o con las flechas del teclado; barrido automático horizontal o vertical.
- Información de la cámara, sincronización de su hora, reinicio, búsqueda automática de su IP y fijación manual.
- Estado del servidor, direcciones local y Tailscale, registro y reinicio del servidor.

El servidor corre sin ventana. Un proceso vigilante lo vuelve a abrir si se cierra; `--detener` lo para del todo. Registro en `%APPDATA%\CamaraWifi\servidor.log`.

## Acceso desde fuera de casa

Instala [Tailscale](https://tailscale.com) en la PC y en el iPhone, inicia sesión con la misma cuenta en los dos y abre `http://<ip-tailscale-de-la-pc>:8770` (la IP empieza por `100.`). Escribe siempre `http://`: el servidor no usa HTTPS y el navegador puede intentar cambiarlo. El firewall de Windows debe permitir el programa en redes privadas; Tailscale cuenta como red privada. No abras puertos en el router: ni la cámara ni el servidor piden contraseña.

## Lo que la cámara probada no puede hacer

Comprobado orden por orden con ONVIF: no tiene zoom, ni posiciones guardadas, ni velocidades de movimiento (acepta el valor y lo ignora), ni detección de movimiento por red, ni audio hacia la cámara, ni ajustes de imagen. Entrega su usuario y contraseña de administrador a cualquiera de la red sin autenticar. Otros modelos pueden diferir.

## Datos de la cámara probada

- Video H.265: `rtsp://<ip>:554/0/av0` (alta) y `rtsp://<ip>:554/0/av1` (baja). Audio PCMA 8 kHz en el mismo flujo.
- Movimiento: ONVIF `ContinuousMove` y `Stop` en `http://<ip>/onvif/PTZ`, perfil `Profile_1`.
- IP de ejemplo en el código: `192.168.1.100`. Si no responde, el servidor busca la cámara por ONVIF y, si hace falta, recorre la red local; la IP encontrada se guarda en `%APPDATA%\CamaraWifi\config.json`.

## Pruebas

- `servidor_iphone.py --prueba-panel 25 --puerto 8796`: arranca el servidor y ejercita todas las rutas contra la cámara real (video, audio, foto, clip, HLS, información). No mueve la cámara.
- `servidor_iphone.py --prueba 6`: decodifica y codifica JPEG sin abrir el puerto.
- `visor_camara.py --prueba 10 --calidad baja`, `--prueba-ventana 10`, `--prueba-ptz` (este sí mueve la cámara).
