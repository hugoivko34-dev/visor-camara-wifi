# Visor de cámara WiFi para Windows

Programa de escritorio, sin anuncios ni telemetría, para ver una cámara ONVIF/RTSP en la red local.
Interfaz en español. Un solo archivo de código: `visor_camara.py`. Dependencias ya instaladas en `.venv`:
PyAV (`av`), PySide6-Essentials, numpy, sounddevice, pyinstaller. No instales nada.

## Cámara
- IP por defecto: `192.168.1.100`, sin usuario ni contraseña.
- Calidad alta: `rtsp://<ip>:554/0/av0`. Calidad baja: `rtsp://<ip>:554/0/av1`.
- Video H.265, audio PCMA 8 kHz mono.

## Requisitos
1. Abre y empieza a conectar de inmediato, sin pantallas previas. Prioridad: el menor tiempo hasta ver imagen.
2. RTSP por TCP con baja latencia (`rtsp_transport=tcp`, `fflags=nobuffer`, `flags=low_delay`, timeout de socket de unos 5 s).
3. Decodifica en un hilo aparte. La interfaz solo pinta el último fotograma; los atrasados se descartan, para que la imagen no se retrase con el tiempo. La imagen mantiene su proporción y se adapta al tamaño de la ventana.
4. Reconexión automática sin límite con espera creciente (1 s a 10 s). Estado visible: "Conectando…", "En vivo", "Sin conexión, reintentando…", con el mensaje de error real.
5. Si falla la conexión, busca la cámara por WS-Discovery ONVIF (multicast UDP `239.255.255.250:3702`, tipo `dn:NetworkVideoTransmitter`) y, si responde otra IP, la usa y la guarda. Esta búsqueda no bloquea la interfaz.
6. Barra de controles: selector Alta/Baja, botón de sonido (apagado por defecto, con volumen), Foto, Pantalla completa y Cámara (diálogo para cambiar la IP).
7. Audio: decodifica el audio, lo remuestrea a s16 mono y lo reproduce con sounddevice con un búfer corto. Si falla el audio, el video sigue funcionando.
8. Foto: guarda el fotograma actual en PNG en `Imágenes\CamaraWifi\` con fecha y hora, y muestra la ruta en el estado.
9. Teclado: F11 o doble clic para pantalla completa, Esc para salir de ella, S para sonido, F para foto. En pantalla completa se oculta la barra.
10. Configuración en `%APPDATA%\CamaraWifi\config.json`: ip, calidad, sonido, volumen, geometría de ventana. Un archivo dañado o ausente no debe impedir el arranque.
11. Cierre limpio: al cerrar la ventana se detienen hilos y conexiones, sin procesos colgados.
12. El proceso funciona también empaquetado con PyInstaller `--windowed` (donde `sys.stdout` es None): no uses `print` sin protegerlo.

## Modo de prueba
`visor_camara.py --prueba SEGUNDOS [--calidad alta|baja] [--ip IP]`: sin ventana, conecta, decodifica durante ese tiempo e imprime una línea JSON con
`conectado`, `segundos_hasta_primer_fotograma`, `fotogramas`, `fps`, `ancho`, `alto`, `codec`, `paquetes_audio`, `error`. Código de salida 0 si hubo fotogramas, 1 si no. No guarda imágenes.

## Empaquetado
`construir.bat`: usa `.venv\Scripts\pyinstaller.exe` para generar `dist\VisorCamara.exe` (`--onefile --windowed --name VisorCamara`), incluyendo lo necesario de `av` con `--collect-all av` y de sounddevice con `--collect-all sounddevice`.

## No hacer
Sin ejecutar la interfaz gráfica de forma interactiva. Sin dependencias nuevas. Sin control de movimiento ni grabación en esta versión.

Al terminar, responde en pocas líneas qué archivos creaste y qué ejecutaste para comprobarlo. Comprueba al menos que compila con `.venv\Scripts\python.exe -m py_compile visor_camara.py`. Es posible que no tengas acceso de red a la cámara; si es así, dilo.

## Corrección de rutas (tiene prioridad sobre lo anterior)
Las dependencias NO están en `.venv`: están en `%TEMP%\vc\venv`, porque la ruta de esta carpeta es demasiado larga para Windows.
Usa ese Python para comprobar. `construir.bat` debe usar `%TEMP%\vc\venv\Scripts\pyinstaller.exe`, con `--workpath "%TEMP%\vc\build"` y `--specpath "%TEMP%\vc"`, y dejar el resultado en `dist\VisorCamara.exe` de esta carpeta (`--distpath` con ruta absoluta basada en `%~dp0`).
