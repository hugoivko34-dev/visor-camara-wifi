"""Servidor local de video y controles para iPhone."""
from __future__ import annotations

import argparse
import ipaddress
import ctypes
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote
import camara
import video

import av

DEFAULT_IP = "192.168.1.100"
VERSION = "2.0"
INICIO = datetime.now(timezone.utc)
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
URLS = {"baja": "/0/av1", "alta": "/0/av0"}
BOUNDARY = "fotograma"

def say(*values):
    try:
        if sys.stdout is not None:
            print(*values, flush=True)
    except (OSError, ValueError):
        pass


def get_camera_ip():
    return camara.ip_actual()


def set_camera_ip(ip):
    return camara.fijar_ip(ip)


def _reconectar_streams():
    for stream in streams.values():
        stream.reconnect.set()
        with stream.lock:
            stream.lock.notify_all()


camara.configurar(camara.cargar_ip(DEFAULT_IP), _reconectar_streams)


def local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


_tailscale_cache = (0.0, None)


def ip_tailscale():
    global _tailscale_cache
    now = time.monotonic()
    if now - _tailscale_cache[0] < 60:
        return _tailscale_cache[1]
    network = ipaddress.ip_network("100.64.0.0/10")
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidate = result[4][0]
            if ipaddress.ip_address(candidate) in network:
                _tailscale_cache = (now, candidate)
                return candidate
    except OSError:
        pass
    executable = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
    try:
        result = subprocess.run([str(executable), "ip", "-4"], capture_output=True, text=True,
                                timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        for candidate in result.stdout.split():
            try:
                if ipaddress.ip_address(candidate) in network:
                    _tailscale_cache = (now, candidate)
                    return candidate
            except ValueError:
                pass
    except (OSError, subprocess.TimeoutExpired):
        pass
    _tailscale_cache = (now, None)
    return None


def png_icon():
    def chunk(tag, data):
        checksum = zlib.crc32(tag + data) & 0xffffffff
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", checksum)

    width = height = 180
    raw = b"".join(b"\x00" + bytes((30, 120, 210, 255)) * width for _ in range(height))
    header = struct.pack("!2I5B", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


streams = video._streams
URLS = video.URLS
ptz_lock = threading.Condition()
ptz_move = None
ptz_stop = False
movement_active = False
move_touch = 0.0
move_start = 0.0
ptz_error = ""
sweep_state = None
sweep_deadline = 0.0
shutdown = threading.Event()

STOP_EVENT_NAME = "Local\\ServidorIphoneDetener"
MUTEX_NAME = "Local\\ServidorIphoneCamara"
ERROR_ALREADY_EXISTS = 183
LOG_PATH = Path(os.environ.get("APPDATA", str(Path.home()))) / "CamaraWifi" / "servidor.log"
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None


def open_stop_event():
    if os.name != "nt":
        return None
    _kernel32.CreateEventW.restype = ctypes.c_void_p
    _kernel32.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
    return _kernel32.CreateEventW(None, True, False, STOP_EVENT_NAME)


def signal_stop():
    handle = open_stop_event()
    if handle:
        try:
            _kernel32.SetEvent(ctypes.c_void_p(handle))
        finally:
            _kernel32.CloseHandle(ctypes.c_void_p(handle))


def write_log(message):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


video.configurar(obtener_ip=get_camera_ip, write_log=write_log)


def prepare_log():
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1024 * 1024:
            old = LOG_PATH.with_name("servidor.log.1")
            if old.exists():
                old.unlink()
            LOG_PATH.replace(old)
        return LOG_PATH.open("a", encoding="utf-8", buffering=1)
    except OSError:
        return open(os.devnull, "a", encoding="utf-8")


def stop_server_on_event():
    handle = open_stop_event()
    if not handle:
        return
    if _kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0xFFFFFFFF) == 0:
        try:
            camara.soap(None, timeout=.35)
        finally:
            os._exit(0)


def run_watcher(ip, port):
    _kernel32.CreateMutexW.restype = ctypes.c_void_p
    _kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    mutex = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not mutex:
        return 0
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(ctypes.c_void_p(mutex))
        return 0
    event = open_stop_event()
    if not event:
        _kernel32.CloseHandle(ctypes.c_void_p(mutex))
        return 0
    log = prepare_log()
    _kernel32.ResetEvent(ctypes.c_void_p(event))
    wait_seconds = 2
    child = None
    child_started = 0
    try:
        while True:
            try:
                if _kernel32.WaitForSingleObject(ctypes.c_void_p(event), 0) == 0:
                    if child is not None and child.poll() is None:
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.terminate()
                    return 0
                if child is None:
                    command = ([sys.executable, "--servidor"] if getattr(sys, "frozen", False)
                               else [sys.executable, __file__, "--servidor"])
                    if ip:
                        command.extend(["--ip", ip])
                    command.extend(["--puerto", str(port)])
                    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                             creationflags=subprocess.CREATE_NO_WINDOW)
                    child_started = time.monotonic()
                if _kernel32.WaitForSingleObject(ctypes.c_void_p(event), 250) == 0:
                    continue
                code = child.poll()
                if code is not None:
                    lived = time.monotonic() - child_started
                    write_log(f"Servidor terminó con código {code}")
                    child = None
                    wait_seconds = min(30, wait_seconds * 2) if lived < 30 else 2
                    if _kernel32.WaitForSingleObject(ctypes.c_void_p(event), int(wait_seconds * 1000)) == 0:
                        continue
            except Exception as exc:
                write_log(f"Error del vigilante: {exc!r}")
                if _kernel32.WaitForSingleObject(ctypes.c_void_p(event), 1000) == 0:
                    continue
    finally:
        log.close()
        _kernel32.CloseHandle(ctypes.c_void_p(event))
        _kernel32.CloseHandle(ctypes.c_void_p(mutex))


def ptz_worker():
    global ptz_move, ptz_stop, movement_active, move_start, move_touch, sweep_state, sweep_deadline
    while not shutdown.is_set():
        action = None
        with ptz_lock:
            now = time.monotonic()
            if sweep_state and now >= sweep_deadline:
                sweep_state["sentido"] *= -1
                sweep_deadline = now + sweep_state["segundos"]
                action = ((sweep_state["sentido"] * .5, 0) if sweep_state["eje"] == "x"
                          else (0, sweep_state["sentido"] * .5))
                move_start = now
                move_touch = now
            elif movement_active and not sweep_state and (now - move_touch >= 2 or now - move_start >= 30):
                ptz_stop = True
            if ptz_stop:
                action = "stop"
                ptz_stop = False
            elif action is None and ptz_move is not None:
                action = ptz_move
                ptz_move = None
            elif action is None:
                ptz_lock.wait(.2)
        if action is None:
            continue
        succeeded = camara.soap(None if action == "stop" else action)
        with ptz_lock:
            if action == "stop":
                movement_active = not succeeded
                if not succeeded:
                    ptz_stop = True
            else:
                movement_active = succeeded
                if succeeded:
                    move_start = time.monotonic()
                    move_touch = move_start


threading.Thread(target=ptz_worker, daemon=True).start()


def cliente_desconectado(conexion):
    """True si el navegador cerró la conexión. Sin esto, un video detenido nunca escribe
    y el servidor sigue contando como clientes a los iPhone que ya se fueron."""
    try:
        legible, _, _ = select.select([conexion], [], [], 0)
        if not legible:
            return False
        return conexion.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


class ExclusiveHTTPServer(ThreadingHTTPServer):
    # En Windows, SO_REUSEADDR deja que dos programas abran el mismo puerto sin error.
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        exc_type, exc, _ = sys.exc_info()
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        detail = str(exc).replace("\r", " ").replace("\n", " ")
        write_log(f"HTTP {client_address}: {exc_type.__name__ if exc_type else 'Error'}: {detail}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, value):
        return self.send_bytes(status, json.dumps(value, ensure_ascii=False).encode(), "application/json")

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/":
            panel = BASE_DIR / "panel" / "index.html"
            if panel.is_file():
                return self.send_bytes(200, panel.read_bytes(), "text/html; charset=utf-8")
            return self.send_json(503, {"error": "No se encuentra la carpeta panel del programa"})
        if path.path.startswith("/panel/"):
            nombre = path.path[len("/panel/"):]
            tipos = {"html": "text/html; charset=utf-8", "js": "application/javascript; charset=utf-8",
                     "css": "text/css; charset=utf-8", "json": "application/json",
                     "png": "image/png", "svg": "image/svg+xml", "webmanifest": "application/manifest+json"}
            archivo = (BASE_DIR / "panel" / nombre).resolve()
            raiz = (BASE_DIR / "panel").resolve()
            extension = archivo.suffix.lstrip(".")
            if ".." in nombre or archivo.parent != raiz or extension not in tipos or not archivo.is_file():
                return self.send_json(404, {"error": "Archivo no encontrado"})
            return self.send_bytes(200, archivo.read_bytes(), tipos[extension])
        if path.path == "/manifest.webmanifest":
            manifest = {
                "name": "Cámara", "short_name": "Cámara", "start_url": "/",
                "display": "standalone", "background_color": "#101318",
                "theme_color": "#101318",
                "icons": [{"src": "/icono.png", "sizes": "180x180", "type": "image/png"}],
            }
            return self.send_bytes(200, json.dumps(manifest, ensure_ascii=False).encode(),
                                   "application/manifest+json")
        if path.path == "/icono.png":
            return self.send_bytes(200, png_icon(), "image/png")
        if path.path == "/estado":
            quality = parse_qs(path.query).get("calidad", ["baja"])[0]
            state = streams.get(quality, streams["baja"]).status()
            state["movimiento_activo"] = movement_active
            state["error_movimiento"] = ptz_error
            state.update({"barrido": ({"activo": True, "eje": sweep_state["eje"],
                                         "segundos": sweep_state["segundos"]} if sweep_state else None), **video.metricas(quality),
                          "clientes_audio": sum(len(item.audio_queues) for item in streams.values()), "grabando": video.estado_grabacion(),
                          "servidor": {"version": VERSION, "inicio": INICIO.isoformat(),
                                       "segundos_activo": int((datetime.now(timezone.utc) - INICIO).total_seconds()),
                                       "ip_local": local_ip(), "ip_tailscale": ip_tailscale(), "puerto": self.server.server_port}})
            return self.send_json(200, state)
        if path.path == "/camara/info":
            return self.send_json(200, camara.info_dispositivo())
        if path.path == "/camara/clave":
            try:
                return self.send_json(200, {"usuarios": camara.claves()})
            except Exception as exc:
                return self.send_json(500, {"error": str(exc)})
        if path.path == "/servidor/registro":
            try:
                lineas = max(1, min(1000, int(parse_qs(path.query).get("lineas", ["200"])[0])))
                contenido = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-lineas:]
                return self.send_json(200, {"lineas": contenido})
            except (OSError, ValueError):
                return self.send_json(200, {"lineas": []})
        if path.path == "/stream":
            quality = parse_qs(path.query).get("calidad", ["baja"])[0]
            if quality not in streams:
                return self.send_json(400, {"error": "Calidad no válida"})
            stream = streams[quality]
            query = parse_qs(path.query)
            try:
                fps = float(query.get("fps", ["0"])[0])
                q = int(query.get("q", ["5"])[0])
                if fps < 0 or fps > 15 or q < 2 or q > 12:
                    raise ValueError()
            except ValueError:
                return self.send_json(400, {"error": "fps o q fuera de rango"})
            video.registrar_cliente(quality, q)
            self.close_connection = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                seen = -1
                last_sent = 0.0
                while not shutdown.is_set():
                    with stream.lock:
                        stream.lock.wait_for(lambda: (q in stream.jpegs and stream.jpegs[q][1] > seen) or shutdown.is_set(), timeout=5)
                        jpg, sequence = stream.jpegs.get(q, (None, seen))
                    if sequence == seen and cliente_desconectado(self.connection):
                        break
                    if jpg and sequence != seen:
                        now = time.monotonic()
                        if fps and now - last_sent < 1 / fps:
                            seen = sequence
                            continue
                        self.wfile.write(
                            b"--" + BOUNDARY.encode() + b"\r\nContent-Type: image/jpeg\r\n"
                            + b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n"
                            + jpg + b"\r\n"
                        )
                        self.wfile.flush()
                        seen = sequence
                        last_sent = time.monotonic()
                        video.registrar_envio(quality, len(jpg))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                video.quitar_cliente(quality, q)
            return
        if path.path == "/audio":
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "audio/L16; rate=8000; channels=1")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in video.audio_pcm():
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if path.path.startswith("/hls/"):
            parts = path.path.split("/")
            if len(parts) != 4 or parts[2] not in streams:
                return self.send_json(404, {"error": "Archivo HLS no encontrado"})
            quality, name = parts[2], parts[3]
            video.hls_iniciar(quality)
            lista = video.hls_lista(quality) if name == "index.m3u8" else None
            if lista:
                return self.send_bytes(200, lista, "application/vnd.apple.mpegurl")
            file = None if name == "index.m3u8" else video.hls_archivo(quality, name)
            if not file:
                self.send_response(503)
                self.send_header("Retry-After", "1")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            content_type = "application/vnd.apple.mpegurl" if name == "index.m3u8" else "video/mp4" if name == "init.mp4" else "video/iso.segment"
            return self.send_bytes(200, file.read_bytes(), content_type)
        if path.path == "/archivos":
            return self.send_json(200, video.listar_archivos())
        if path.path.startswith("/archivos/"):
            nombre = path.path[len("/archivos/"):]
            file = video.ruta_archivo(nombre)
            if not file:
                return self.send_json(404, {"error": "Archivo no encontrado"})
            if parse_qs(path.query).get("miniatura") == ["1"]:
                if not nombre.startswith("foto_"):
                    return self.send_json(400, {"error": "La miniatura requiere una foto"})
                try:
                    thumbnail = video.miniatura(nombre)
                    if not thumbnail:
                        return self.send_json(500, {"error": "No se pudo generar la miniatura"})
                    return self.send_bytes(200, thumbnail, "image/jpeg")
                except Exception as exc:
                    return self.send_json(500, {"error": str(exc)})
            data = file.read_bytes()
            start, end, status = 0, len(data) - 1, 200
            value = self.headers.get("Range", "")
            if value.startswith("bytes="):
                try:
                    a, b = value[6:].split("-", 1)
                    start = int(a or 0)
                    end = min(int(b) if b else end, end)
                    if start > end or start >= len(data):
                        raise ValueError()
                    status = 206
                    data = data[start:end + 1]
                except ValueError:
                    return self.send_json(416, {"error": "Rango no válido"})
            self.send_response(status)
            self.send_header("Content-Type", "image/jpeg" if nombre.startswith("foto_") else "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{start + len(data) - 1}/{file.stat().st_size}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_DELETE(self):
        route = urlparse(self.path)
        if not route.path.startswith("/archivos/"):
            return self.send_json(404, {"error": "Ruta no encontrada"})
        file = video.ruta_archivo(route.path[len("/archivos/"):])
        if not file:
            return self.send_json(404, {"error": "Archivo no encontrado"})
        try:
            file.unlink()
            video._thumbs.pop(file.name, None)
            return self.send_bytes(204, b"", "application/json")
        except OSError as exc:
            return self.send_json(500, {"error": str(exc)})

    def do_POST(self):
        global ptz_move, ptz_stop, move_touch, sweep_state, sweep_deadline
        route = urlparse(self.path)
        if route.path not in ("/ptz", "/foto", "/grabar", "/camara/hora", "/camara/reiniciar",
                              "/camara/buscar", "/camara/ip", "/servidor/reiniciar"):
            return self.send_json(404, {"error": "Ruta no encontrada"})
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            return self.send_json(415, {"error": "Se requiere Content-Type application/json"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(min(length, 4096)))
            if not isinstance(data, dict):
                return self.send_json(400, {"error": "Solicitud no válida"})
            if route.path == "/servidor/reiniciar":
                camara.soap(None)
                threading.Thread(target=lambda: (time.sleep(.5), os._exit(3)), daemon=True).start()
                return self.send_json(200, {"ok": True})
            if route.path == "/camara/hora":
                try:
                    return self.send_json(200, {"ok": True, "desfase_segundos": camara.sincronizar_hora()})
                except camara.ErrorCamara as exc:
                    return self.send_json(500, {"error": str(exc)})
            if route.path == "/camara/reiniciar":
                try:
                    return self.send_json(200, {"ok": True, "mensaje": camara.reiniciar()})
                except camara.ErrorCamara as exc:
                    return self.send_json(500, {"error": str(exc)})
            if route.path == "/camara/buscar":
                ip, encontrada = camara.buscar_ahora()
                return self.send_json(200, {"ip": ip, "encontrada": encontrada})
            if route.path == "/camara/ip":
                try:
                    ip = camara.fijar_ip(data.get("ip"))
                    return self.send_json(200, {"ok": True, "ip": ip})
                except ValueError as exc:
                    return self.send_json(400, {"error": str(exc)})
            if route.path == "/foto":
                quality = data.get("calidad", "baja")
                if quality not in streams:
                    return self.send_json(400, {"error": "Calidad no válida"})
                try:
                    nombre, raw = video.tomar_foto(quality)
                except Exception as exc:
                    return self.send_json(500, {"error": str(exc)})
                return self.send_json(200, {"archivo": nombre, "bytes": len(raw)})
            if route.path == "/grabar":
                if data.get("detener") is True:
                    video.detener_grabacion()
                    return self.send_json(200, {"ok": True})
                quality = data.get("calidad", "baja")
                seconds = data.get("segundos", 30)
                if quality not in streams or type(seconds) is not int or seconds < 5 or seconds > 600:
                    return self.send_json(400, {"error": "Segundos o calidad fuera de rango"})
                try:
                    name = video.iniciar_grabacion(seconds, quality)
                except video.GrabacionEnCurso:
                    return self.send_json(409, {"error": "Ya hay una grabación en curso"})
                except Exception as exc:
                    return self.send_json(500, {"error": str(exc)})
                return self.send_json(200, {"archivo": name})
            if "barrido" in data:
                if data.get("barrido") is True:
                    eje = data.get("eje")
                    segundos = data.get("segundos")
                    if eje not in ("x", "y") or type(segundos) is not int or not 2 <= segundos <= 20:
                        return self.send_json(400, {"error": "Eje o duración del barrido no válidos"})
                    with ptz_lock:
                        sweep_state = {"eje": eje, "segundos": segundos, "sentido": 1}
                        sweep_deadline = time.monotonic() + segundos
                        ptz_move = ((.5, 0) if eje == "x" else (0, .5))
                        ptz_stop = False
                        move_touch = time.monotonic()
                        ptz_lock.notify_all()
                elif data.get("barrido") is False:
                    with ptz_lock:
                        sweep_state = None
                        ptz_stop = True
                        ptz_move = None
                        ptz_lock.notify_all()
                else:
                    return self.send_json(400, {"error": "Solicitud no válida"})
            elif data.get("stop") is True:
                with ptz_lock:
                    sweep_state = None
                    ptz_stop = True
                    ptz_move = None
                    ptz_lock.notify_all()
            elif data.get("latido") is True:
                with ptz_lock:
                    if movement_active:
                        move_touch = time.monotonic()
            elif {"x", "y"}.issubset(data):
                x, y = data["x"], data["y"]
                if (type(x) not in (int, float) or type(y) not in (int, float)
                        or x not in (-.5, 0, .5) or y not in (-.5, 0, .5)
                        or (x == 0 and y == 0)):
                    return self.send_json(400, {"error": "Movimiento no válido"})
                with ptz_lock:
                    if sweep_state:
                        ptz_stop = True
                    sweep_state = None
                    ptz_move = (x, y)
                    move_touch = time.monotonic()
                    ptz_lock.notify_all()
            else:
                return self.send_json(400, {"error": "Solicitud no válida"})
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json(400, {"error": "JSON no válido"})


def prueba(seconds):
    frames = 0
    last_jpeg = b""
    error = ""
    end = time.monotonic() + seconds
    delay = 1
    while time.monotonic() < end:
        container = None
        try:
            options = {
                "rtsp_transport": "tcp", "fflags": "nobuffer", "flags": "low_delay",
                "max_delay": "0", "timeout": "5000000", "analyzeduration": "0",
                "probesize": "32768",
            }
            container = av.open(f"rtsp://{get_camera_ip()}:554{URLS['baja']}",
                                options=options, timeout=(5, 5))
            stream = container.streams.video[0]
            codec = av.CodecContext.create("mjpeg", "w")
            codec.pix_fmt = "yuvj420p"
            codec.width = stream.codec_context.width
            codec.height = stream.codec_context.height
            codec.options = {"qscale": "5"}
            for packet in container.demux(stream):
                if time.monotonic() >= end:
                    break
                for frame in packet.decode():
                    encoded = codec.encode(frame.reformat(format="yuvj420p"))
                    if encoded:
                        last_jpeg = bytes(encoded[-1])
                        frames += 1
                        delay = 1
                    if time.monotonic() >= end:
                        break
            if time.monotonic() < end:
                raise RuntimeError("La conexión RTSP se cerró")
        except Exception as exc:
            error = str(exc)
        finally:
            if container is not None:
                container.close()
        if time.monotonic() < end:
            time.sleep(min(delay, end - time.monotonic()))
            delay = min(delay + 1, 10)
    say(json.dumps({"fotogramas": frames, "bytes_jpeg_ultimo": len(last_jpeg), "error": error},
                   ensure_ascii=False))
    return 0 if frames else 1


def prueba_panel(seconds, port):
    """Prueba el panel real en un hilo y sin iniciar el vigilante."""
    keys = ("raiz", "app_js", "style_css", "manifest", "icono", "estado", "stream_jpegs",
            "audio_bytes", "foto", "grabar", "archivos", "rango_clip", "clip_hevc",
            "borrar_foto", "borrar_clip", "camara_info", "hls", "hls_init", "hls_segmento",
            "ptz_stop", "ptz_texto_415", "registro")
    resultados = {key: False for key in keys}
    resultados["errores"] = []
    servidor = None
    try:
        servidor = ExclusiveHTTPServer(("127.0.0.1", port), Handler)
        servidor.daemon_threads = True
        hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
        hilo.start()
        base = f"http://127.0.0.1:{port}"
        def get(path, timeout=seconds, headers=None):
            return urllib.request.urlopen(urllib.request.Request(base + path, headers=headers or {}), timeout=timeout)
        def post(path, data, content_type="application/json"):
            req = urllib.request.Request(base + path, data=data,
                headers={"Content-Type": content_type}, method="POST")
            return urllib.request.urlopen(req, timeout=seconds)
        def check(key, fn):
            try: resultados[key] = fn()
            except Exception as exc: resultados["errores"].append(f"{key}: {exc}")
        for key, path, token in (("raiz", "/", b"<title>"), ("app_js", "/panel/app.js", b""),
                                 ("style_css", "/panel/style.css", b""), ("manifest", "/manifest.webmanifest", b"standalone"),
                                 ("icono", "/icono.png", b"PNG"), ("estado", "/estado", b"servidor"),
                                 ("camara_info", "/camara/info", b"fabricante"), ("registro", "/servidor/registro", b"lineas")):
            def fetch(key=key, path=path, token=token):
                with get(path) as response:
                    body = response.read()
                    if key == "camara_info":
                        return response.status == 200 and bool(json.loads(body).get("fabricante"))
                    return response.status == 200 and (not token or token.lower() in body.lower())
            check(key, fetch)
        def stream_check():
            sock = socket.create_connection(("127.0.0.1", port), timeout=seconds)
            sock.settimeout(2)
            sock.sendall(b"GET /stream?calidad=baja&fps=5 HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            data = b""
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(65536)
                    if not chunk: break
                    data += chunk
                except socket.timeout: continue
            sock.close()
            count = data.count(b"image/jpeg")
            resultados["stream_jpegs_n"] = count
            return 15 <= count <= 40
        check("stream_jpegs", stream_check)
        def audio_check():
            with get("/audio", timeout=6) as response:
                body = response.read(16000)
            resultados["audio_bytes"] = len(body)
            return len(body) >= 16000
        check("audio_bytes", audio_check)
        foto = clip = None
        try:
            with post("/foto", b'{"calidad":"baja"}') as r: foto = json.loads(r.read())["archivo"]
            resultados["foto"] = True
        except Exception as exc: resultados["errores"].append(f"foto: {exc}")
        try:
            with post("/grabar", b'{"segundos":5,"calidad":"baja"}') as r: clip = json.loads(r.read())["archivo"]
        except Exception as exc: resultados["errores"].append(f"grabar inicio: {exc}")
        if clip:
            for _ in range(80):
                try:
                    with get("/estado") as r:
                        if json.loads(r.read()).get("grabando") is None:
                            break
                except Exception:
                    pass
                time.sleep(0.25)
            resultados["grabar"] = video.estado_grabacion() is None
        try:
            with get("/archivos") as r: listing = json.loads(r.read())
            resultados["archivos"] = bool(foto and clip and any(x["nombre"] == foto for x in listing) and any(x["nombre"] == clip for x in listing))
        except Exception as exc: resultados["errores"].append(f"archivos: {exc}")
        if clip:
            try:
                with get("/archivos/" + quote(clip), headers={"Range":"bytes=0-99"}) as r:
                    resultados["rango_clip"] = r.status == 206 and len(r.read()) == 100
                path = video.ruta_archivo(clip)
                with av.open(str(path)) as container:
                    stream = container.streams.video[0]
                    resultados["clip_hevc"] = float(container.duration or 0) > 0 and stream.codec_context.name == "hevc"
            except Exception as exc: resultados["errores"].append(f"clip: {exc}")
        for key, name in (("borrar_foto", foto), ("borrar_clip", clip)):
            if name:
                try:
                    req = urllib.request.Request(base + "/archivos/" + quote(name), method="DELETE")
                    with urllib.request.urlopen(req, timeout=seconds) as r: resultados[key] = r.status == 204
                except Exception as exc: resultados["errores"].append(f"{key}: {exc}")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with get("/hls/baja/index.m3u8", timeout=3) as r:
                    playlist = r.read().decode("utf-8", "replace")
                    if r.status == 200:
                        resultados["hls"] = True
                        segment = next((line for line in playlist.splitlines() if line and not line.startswith("#") and line != "init.mp4"), None)
                        for key, path in (("hls_init", "/hls/baja/init.mp4"), ("hls_segmento", "/hls/baja/" + segment if segment else None)):
                            if path:
                                with get(path, timeout=3) as f: resultados[key] = f.status == 200 and len(f.read()) > 0
                        break
            except urllib.error.HTTPError as exc:
                if exc.code != 503: resultados["errores"].append(f"hls: {exc}"); break
            except Exception as exc: resultados["errores"].append(f"hls: {exc}"); break
            time.sleep(1)
        try:
            with post("/ptz", b'{"stop":true}') as r: resultados["ptz_stop"] = r.status == 204
        except Exception as exc: resultados["errores"].append(f"ptz_stop: {exc}")
        try: post("/ptz", b"texto", "text/plain")
        except urllib.error.HTTPError as exc: resultados["ptz_texto_415"] = exc.code == 415
        except Exception as exc: resultados["errores"].append(f"ptz_texto_415: {exc}")
    except Exception as exc:
        resultados["errores"].append(str(exc))
    finally:
        if servidor is not None:
            servidor.shutdown()
            servidor.server_close()
    say(json.dumps(resultados, ensure_ascii=False))
    return 0 if all(resultados[key] for key in keys) else 1


def console_control_handler(control_type):
    if control_type in (0, 2, 5, 6):
        camara.soap(None, timeout=.35)
        return True
    return False


def main():
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip")
    parser.add_argument("--puerto", type=int, default=8770)
    parser.add_argument("--prueba", type=float)
    parser.add_argument("--prueba-panel", type=float)
    parser.add_argument("--servidor", action="store_true")
    parser.add_argument("--detener", action="store_true")
    args = parser.parse_args()
    if args.detener:
        signal_stop()
        return 0
    if os.name == "nt" and args.prueba is None and args.prueba_panel is None and not args.servidor:
        return run_watcher(args.ip, args.puerto)
    if args.ip:
        set_camera_ip(args.ip)
    if args.prueba is not None:
        return prueba(max(0, args.prueba))
    if args.prueba_panel is not None:
        return prueba_panel(max(1, args.prueba_panel), args.puerto)

    if os.name == "nt":
        threading.Thread(target=stop_server_on_event, daemon=True).start()

    try:
        server = ExclusiveHTTPServer(("0.0.0.0", args.puerto), Handler)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in (48, 98, 10048):
            say(f"El puerto {args.puerto} ya está en uso: quizá el servidor ya esté abierto")
            if sys.stdin is not None and sys.stdin.isatty():
                try:
                    input("Presiona Enter para salir")
                except (EOFError, OSError):
                    pass
            return 1
        raise
    server.daemon_threads = True
    callback_reference = None
    if os.name == "nt":
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        callback_reference = callback_type(console_control_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(callback_reference, True)
    address = f"http://{local_ip()}:{args.puerto}"
    say("Abre esta dirección en Safari en tu iPhone, conectado a este mismo wifi:")
    say(address)
    say("Cierra esta ventana para detener")
    threading.Thread(target=camara.camera_watchdog, args=(shutdown, lambda found: say(f"Cámara encontrada en {found}")), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown.set()
        server.shutdown()
        server.server_close()
        camara.soap(None)
        for stream in streams.values():
            stream.stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
