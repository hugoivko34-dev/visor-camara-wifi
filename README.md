# Visor de cámara WiFi

Alternativa sin anuncios a la app YCC365 Plus, para ver y mover una cámara con RTSP y ONVIF. Funciona en la red local y no usa la nube del fabricante.

- `visor_camara.py`: programa de Windows con video, sonido, fotos y control de movimiento.
- `servidor_iphone.py`: servidor que corre en la PC y ofrece al iPhone una página con video y flechas de movimiento, en el puerto 8770.
- `construir.bat`: genera los ejecutables en `dist\`. Usa un entorno de Python en `%TEMP%\vc`, porque las rutas largas rompen la carga de algunas DLL.
- `ESPEC.md` y `ESPEC_IPHONE.md`: especificaciones originales.

## Configurar la cámara
La IP de ejemplo es `192.168.1.100`. Para usar la tuya:

- En el visor, pulsa **Cámara** y escríbela. También intenta encontrarla sola por ONVIF si no conecta.
- El servidor usa `--ip` o la IP guardada por el visor. Si no conecta, busca la cámara por ONVIF y, si no responde, recorre la red local.

## Datos de la cámara probada
- Video H.265: `rtsp://<ip>:554/0/av0` (alta) y `rtsp://<ip>:554/0/av1` (baja).
- Movimiento: ONVIF `ContinuousMove` y `Stop` en `http://<ip>/onvif/PTZ`, perfil `Profile_1`.
- No pide contraseña para RTSP ni ONVIF. Otros modelos pueden usar rutas distintas o pedirla, y este programa no la admite por ahora.

**Seguridad:** el servidor no tiene contraseña y estas cámaras suelen no tenerla. No abras sus puertos a internet: cualquiera podría ver y mover la cámara.

## Pruebas
- `visor_camara.py --prueba 10 --calidad baja`: conecta y decodifica, sin ventana.
- `visor_camara.py --prueba-ventana 10`: abre la ventana real.
- `visor_camara.py --prueba-ptz`: mueve la cámara.
- `servidor_iphone.py --prueba 6`: decodifica y codifica JPEG, sin abrir el puerto.

## Pendiente
- Acceso desde fuera de casa por VPN.
- Correr el servidor en un equipo siempre encendido, sin depender de la PC.
- Sonido en el iPhone y menor consumo de datos, reenviando el video sin convertirlo.

El servidor corre sin ventana, se reabre solo si se cierra y se detiene con `servidor_iphone.py --detener`.
