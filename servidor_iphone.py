"""Servidor local de video y controles para iPhone."""
from __future__ import annotations

import argparse
import ctypes
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.request
import re
import xml.etree.ElementTree as ET
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import av

DEFAULT_IP = "192.168.1.100"
URLS = {"baja": "/0/av1", "alta": "/0/av0"}
BOUNDARY = "fotograma"

PAGE = r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Cámara">
<meta name="theme-color" content="#101318">
<link rel="apple-touch-icon" href="/icono.png">
<link rel="manifest" href="/manifest.webmanifest">
<title>Cámara</title>
<style>
* {
  box-sizing: border-box;
  -webkit-user-select: none;
  user-select: none;
  -webkit-touch-callout: none;
}
html, body {
  margin: 0;
  min-height: 100%;
  background: #101318;
  color: #f4f6fa;
  font: 16px system-ui, sans-serif;
}
body {
  padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
  touch-action: manipulation;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
}
select {
  background: #242a33;
  color: white;
  border: 1px solid #687384;
  border-radius: 8px;
  padding: 9px;
  font-size: 16px;
}
#estado {
  font-size: 14px;
  color: #b8c4d3;
}
main { position: relative; }
#video {
  display: block;
  width: 100%;
  height: auto;
  max-height: 78vh;
  object-fit: contain;
  background: #000;
}
.pad {
  display: grid;
  grid-template: repeat(3, 72px) / repeat(3, 72px);
  gap: 9px;
  justify-content: center;
  padding: 18px;
}
.pad button {
  min-width: 64px;
  min-height: 64px;
  border: 1px solid #7e8a9a;
  border-radius: 16px;
  background: #303947;
  color: white;
  font-size: 30px;
  font-weight: 700;
  touch-action: none;
}
.pad button:active { background: #52647c; }
.up { grid-area: 1 / 2; }
.left { grid-area: 2 / 1; }
.right { grid-area: 2 / 3; }
.down { grid-area: 3 / 2; }
@media (orientation: landscape) {
  body { display: flex; flex-direction: column; }
  header { padding: 4px 12px; }
  main { flex: 1; min-height: 0; }
  #video {
    width: 100%;
    height: calc(100vh - 52px);
    max-height: none;
    object-fit: contain;
  }
  .pad {
    position: absolute;
    right: 18px;
    bottom: 10px;
    background: #101318aa;
    border-radius: 20px;
    padding: 12px;
  }
}
</style>
</head>
<body oncontextmenu="event.preventDefault()">
<header>
  <strong>Cámara</strong>
  <select id="calidad" aria-label="Calidad">
    <option value="baja">Baja</option>
    <option value="alta">Alta</option>
  </select>
  <span id="estado">Conectando…</span>
</header>
<main>
  <img id="video" alt="Video de la cámara">
  <div class="pad">
    <button class="up" data-x="0" data-y="0.5">▲</button>
    <button class="left" data-x="-0.5" data-y="0">◀</button>
    <button class="right" data-x="0.5" data-y="0">▶</button>
    <button class="down" data-x="0" data-y="-0.5">▼</button>
  </div>
</main>
<script>
const img = document.querySelector('#video');
const sel = document.querySelector('#calidad');
const estadoEl = document.querySelector('#estado');
let held = false;
let pulse = null;
let lastReload = 0;

function reload() {
  img.src = '/stream?calidad=' + sel.value + '&r=' + Date.now();
}

try {
  sel.value = localStorage.getItem('calidad') || 'baja';
} catch (e) {}
reload();
sel.onchange = () => {
  try { localStorage.setItem('calidad', sel.value); } catch (e) {}
  reload();
};
img.onerror = () => setTimeout(reload, 1200);

function stop() {
  if (!held) return;
  held = false;
  clearInterval(pulse);
  fetch('/ptz', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{"stop":true}',
    keepalive: true
  }).catch(() => {});
}

for (const button of document.querySelectorAll('.pad button')) {
  button.onpointerdown = event => {
    event.preventDefault();
    if (held) return;
    held = true;
    button.setPointerCapture(event.pointerId);
    fetch('/ptz', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({x: Number(button.dataset.x), y: Number(button.dataset.y)})
    }).catch(() => {});
    pulse = setInterval(() => fetch('/ptz', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{"latido":true}'
    }).catch(() => {}), 700);
  };
  for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    button.addEventListener(name, event => {
      event.preventDefault();
      stop();
    });
  }
}

window.addEventListener('blur', stop);
window.addEventListener('pagehide', stop);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stop();
  else reload();
});
window.addEventListener('pageshow', reload);
setInterval(async () => {
  try {
    const response = await fetch('/estado?calidad=' + sel.value, {cache: 'no-store'});
    const state = await response.json();
    if (state.error_movimiento) {
      estadoEl.textContent = 'Error de movimiento: ' + state.error_movimiento;
      return;
    }
    if (state.segundos_desde_ultimo_fotograma === null) {
      estadoEl.textContent = state.ultimo_error
        ? 'Sin conexión con la cámara'
        : 'Conectando…';
      return;
    }
    const seconds = Math.floor(state.segundos_desde_ultimo_fotograma);
    estadoEl.textContent = seconds < 3 ? 'En vivo' : `Imagen detenida hace ${seconds} s`;
    if (seconds >= 3 && Date.now() - lastReload > 3000) {
      lastReload = Date.now();
      reload();
    }
  } catch (e) {
    estadoEl.textContent = 'Sin conexión con la PC';
  }
}, 2000);
</script>
</body>
</html>'''


def say(*values):
    try:
        if sys.stdout is not None:
            print(*values, flush=True)
    except (OSError, ValueError):
        pass


def load_camera_ip():
    path = Path(os.environ.get("APPDATA", str(Path.home()))) / "CamaraWifi" / "config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return str(value.get("ip") or DEFAULT_IP) if isinstance(value, dict) else DEFAULT_IP
    except (OSError, ValueError, TypeError):
        return DEFAULT_IP


camera_ip_lock = threading.Lock()
camera_ip = load_camera_ip()
discovery_lock = threading.Lock()
last_discovery = time.monotonic() - 15


def get_camera_ip():
    with camera_ip_lock:
        return camera_ip


def set_camera_ip(ip):
    global camera_ip
    with camera_ip_lock:
        camera_ip = ip


def save_camera_ip(ip):
    path = Path(os.environ.get("APPDATA", str(Path.home()))) / "CamaraWifi" / "config.json"
    try:
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                config = {}
        except (OSError, ValueError, TypeError):
            config = {}
        if config.get("ip") == ip:
            return
        config["ip"] = ip
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def discover_camera(timeout=3.5):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        start = time.monotonic()
        end = start + min(timeout, 3.5)
        next_probe = start
        probes_sent = 0
        while time.monotonic() < end:
            now = time.monotonic()
            if probes_sent < 4 and now >= next_probe:
                message_id = "uuid:" + os.urandom(16).hex()
                probe = (f'<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
                         'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
                         'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
                         'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
                         '<e:Header><w:MessageID>' + message_id + '</w:MessageID>'
                         '<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
                         '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
                         '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>'
                         '</d:Probe></e:Body></e:Envelope>').encode()
                sock.sendto(probe, ("239.255.255.250", 3702))
                probes_sent += 1
                next_probe = start + probes_sent * 0.6
            sock.settimeout(max(0.01, min(0.2, end - time.monotonic(), next_probe - time.monotonic()
                                         if probes_sent < 4 else end - time.monotonic())))
            try:
                data, peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                root = ET.fromstring(data)
                if "NetworkVideoTransmitter" not in data.decode("utf-8", "ignore"):
                    continue
                for elem in root.iter():
                    if elem.tag.endswith("XAddrs") and elem.text:
                        for address in re.findall(r"https?://([^/:]+)", elem.text):
                            try:
                                return socket.gethostbyname(address)
                            except OSError:
                                pass
                return peer[0]
            except ET.ParseError:
                continue
    except OSError:
        pass
    finally:
        sock.close()
    return None


def scan_for_camera():
    try:
        own_ip = local_ip()
        if own_ip == "127.0.0.1":
            return None
        subnet = ".".join(own_ip.split(".")[:3])
        addresses = [f"{subnet}.{host}" for host in range(1, 255)
                     if f"{subnet}.{host}" != own_ip]

        def check(ip):
            try:
                with socket.create_connection((ip, 554), timeout=1.0) as conn:
                    conn.settimeout(1.5)
                    conn.sendall(f"OPTIONS rtsp://{ip}:554/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
                    return ip if conn.recv(1024).startswith(b"RTSP/1.0") else None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=64) as executor:
            futures = [executor.submit(check, ip) for ip in addresses]
            for future in as_completed(futures):
                found = future.result()
                if found:
                    for pending in futures:
                        pending.cancel()
                    return found
    except Exception:
        return None
    return None


def find_camera():
    return discover_camera() or scan_for_camera()


def camera_port_responds(ip):
    try:
        with socket.create_connection((ip, 554), timeout=1.5):
            return True
    except Exception:
        return False


def discover_camera_if_due():
    global last_discovery
    if camera_port_responds(get_camera_ip()):
        return None
    with discovery_lock:
        now = time.monotonic()
        if now - last_discovery < 15:
            return None
        last_discovery = now
    found = find_camera()
    if not found or found == get_camera_ip():
        return None
    set_camera_ip(found)
    save_camera_ip(found)
    say(f"Cámara encontrada en {found}")
    for stream in streams.values():
        stream.reconnect.set()
        with stream.lock:
            stream.lock.notify_all()
    return found


def local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def png_icon():
    def chunk(tag, data):
        checksum = zlib.crc32(tag + data) & 0xffffffff
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", checksum)

    width = height = 180
    raw = b"".join(b"\x00" + bytes((30, 120, 210, 255)) * width for _ in range(height))
    header = struct.pack("!2I5B", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Stream:
    def __init__(self, quality):
        self.quality = quality
        self.lock = threading.Condition()
        self.clients = 0
        self.jpeg = None
        self.sequence = 0
        self.last_frame = 0.0
        self.error = ""
        self.connected = False
        self.active = False
        self.stop = threading.Event()
        self.reconnect = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        delay = 1
        idle_since = None
        while not self.stop.is_set():
            with self.lock:
                if self.clients == 0:
                    self.active = False
                    if idle_since is None:
                        idle_since = time.monotonic()
                    remaining = 5 - (time.monotonic() - idle_since)
                    if remaining > 0:
                        self.lock.wait(timeout=min(remaining, 1))
                        continue
                    self.jpeg = None
                    self.last_frame = 0
                    self.connected = False
                    idle_since = None
                    close_idle = True
                else:
                    idle_since = None
                    close_idle = False
                    self.active = True
            if close_idle:
                continue

            container = None
            got_frame = False
            try:
                options = {
                    "rtsp_transport": "tcp", "fflags": "nobuffer",
                    "flags": "low_delay", "max_delay": "0",
                    "timeout": "5000000", "analyzeduration": "0",
                    "probesize": "32768",
                }
                container = av.open(
                    f"rtsp://{get_camera_ip()}:554{URLS[self.quality]}",
                    options=options,
                    timeout=(5, 5),
                )
                stream = container.streams.video[0]
                codec = av.CodecContext.create("mjpeg", "w")
                codec.pix_fmt = "yuvj420p"
                codec.width = stream.codec_context.width
                codec.height = stream.codec_context.height
                codec.options = {"qscale": "5"}
                last_frame_at = time.monotonic()
                delay = 1
                with self.lock:
                    self.jpeg = None
                    self.last_frame = 0
                    self.connected = False

                idle_since = None
                for packet in container.demux(stream):
                    if self.reconnect.is_set():
                        self.reconnect.clear()
                        break
                    with self.lock:
                        if self.clients == 0:
                            if idle_since is None:
                                idle_since = time.monotonic()
                            if time.monotonic() - idle_since >= 5:
                                break
                        else:
                            idle_since = None
                    if time.monotonic() - last_frame_at > 12:
                        raise RuntimeError("La cámara dejó de enviar imagen")
                    for frame in packet.decode():
                        last_frame_at = time.monotonic()
                        encoded = codec.encode(frame.reformat(format="yuvj420p"))
                        if not encoded:
                            continue
                        with self.lock:
                            self.jpeg = bytes(encoded[-1])
                            self.sequence += 1
                            self.last_frame = time.monotonic()
                            self.connected = True
                            self.error = ""
                            got_frame = True
                            self.lock.notify_all()
                if not self.stop.is_set() and self.clients > 0:
                    raise RuntimeError("La conexión RTSP se cerró")
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
                if not self.stop.is_set():
                    discover_camera_if_due()
            finally:
                if container is not None:
                    try:
                        container.close()
                    except Exception:
                        pass
                with self.lock:
                    self.connected = bool(got_frame and self.clients > 0)
                    if (self.clients == 0 or not got_frame
                            or self.error == "La cámara dejó de enviar imagen"):
                        self.jpeg = None
                        self.last_frame = 0
                    self.lock.notify_all()
            with self.lock:
                if self.clients == 0:
                    continue
            if self.reconnect.wait(delay):
                self.reconnect.clear()
                delay = 1
                continue
            delay = min(delay + 1, 10)

    def status(self):
        age = None if not self.last_frame else max(0, round(time.monotonic() - self.last_frame, 1))
        return {
            "conectado": bool(self.connected and self.last_frame),
            "segundos_desde_ultimo_fotograma": age,
            "calidad_activa": bool(self.active),
            "ultimo_error": self.error,
            "ip_camara": get_camera_ip(),
        }


streams = {quality: Stream(quality) for quality in URLS}
ptz_lock = threading.Condition()
ptz_move = None
ptz_stop = False
movement_active = False
move_touch = 0.0
move_start = 0.0
ptz_error = ""
shutdown = threading.Event()


def soap(move=None, timeout=3):
    global ptz_error
    if move is None:
        body = ('<t:Stop><t:ProfileToken>Profile_1</t:ProfileToken>'
                '<t:PanTilt>true</t:PanTilt><t:Zoom>true</t:Zoom></t:Stop>')
    else:
        x, y = move
        body = (f'<t:ContinuousMove><t:ProfileToken>Profile_1</t:ProfileToken>'
                f'<t:Velocity><tt:PanTilt x="{x}" y="{y}"/></t:Velocity></t:ContinuousMove>')
    envelope = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        '<s:Body xmlns:t="http://www.onvif.org/ver20/ptz/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        + body + '</s:Body></s:Envelope>'
    ).encode()
    request = urllib.request.Request(
        f"http://{get_camera_ip()}/onvif/PTZ",
        data=envelope,
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        method="POST",
    )
    last_error = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                ptz_error = ""
                return True
        except Exception as exc:
            last_error = str(exc)
            if attempt < 2:
                time.sleep(min(.15, timeout / 10))
    ptz_error = last_error
    return False


def ptz_worker():
    global ptz_move, ptz_stop, movement_active, move_start, move_touch
    while not shutdown.is_set():
        action = None
        with ptz_lock:
            now = time.monotonic()
            if movement_active and (now - move_touch >= 2 or now - move_start >= 30):
                ptz_stop = True
            if ptz_stop:
                action = "stop"
                ptz_stop = False
            elif ptz_move is not None:
                action = ptz_move
                ptz_move = None
            else:
                ptz_lock.wait(.2)
        if action is None:
            continue
        succeeded = soap(None if action == "stop" else action)
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


class ExclusiveHTTPServer(ThreadingHTTPServer):
    # En Windows, SO_REUSEADDR deja que dos programas abran el mismo puerto sin error.
    allow_reuse_address = False


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

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/":
            return self.send_bytes(200, PAGE.encode(), "text/html; charset=utf-8")
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
            return self.send_bytes(200, json.dumps(state).encode(), "application/json")
        if path.path == "/stream":
            quality = parse_qs(path.query).get("calidad", ["baja"])[0]
            if quality not in streams:
                return self.send_error(400)
            stream = streams[quality]
            with stream.lock:
                stream.clients += 1
                stream.lock.notify_all()
            self.close_connection = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                seen = -1
                while not shutdown.is_set():
                    with stream.lock:
                        stream.lock.wait_for(
                            lambda: (stream.jpeg is not None and stream.sequence > seen) or shutdown.is_set(), timeout=5
                        )
                        jpg = stream.jpeg
                        sequence = stream.sequence
                    if jpg:
                        self.wfile.write(
                            b"--" + BOUNDARY.encode() + b"\r\nContent-Type: image/jpeg\r\n"
                            + b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n"
                            + jpg + b"\r\n"
                        )
                        self.wfile.flush()
                        seen = sequence
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with stream.lock:
                    stream.clients = max(0, stream.clients - 1)
                    stream.lock.notify_all()
            return
        self.send_error(404)

    def do_POST(self):
        global ptz_move, ptz_stop, move_touch
        if urlparse(self.path).path != "/ptz":
            return self.send_error(404)
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            return self.send_error(415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(min(length, 4096)))
            if not isinstance(data, dict):
                return self.send_error(400)
            if data.get("stop") is True:
                with ptz_lock:
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
                    return self.send_error(400)
                with ptz_lock:
                    ptz_move = (x, y)
                    move_touch = time.monotonic()
                    ptz_lock.notify_all()
            else:
                return self.send_error(400)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_error(400)


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


def console_control_handler(control_type):
    if control_type in (0, 2, 5, 6):
        soap(None, timeout=.35)
        return True
    return False


def camera_watchdog():
    while not shutdown.is_set():
        try:
            if not camera_port_responds(get_camera_ip()):
                discover_camera_if_due()
        except Exception:
            pass
        shutdown.wait(30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip")
    parser.add_argument("--puerto", type=int, default=8770)
    parser.add_argument("--prueba", type=float)
    args = parser.parse_args()
    if args.ip:
        set_camera_ip(args.ip)
    if args.prueba is not None:
        return prueba(max(0, args.prueba))

    try:
        server = ExclusiveHTTPServer(("0.0.0.0", args.puerto), Handler)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in (48, 98, 10048):
            say(f"El puerto {args.puerto} ya está en uso: quizá el servidor ya esté abierto")
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
    threading.Thread(target=camera_watchdog, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown.set()
        server.shutdown()
        server.server_close()
        soap(None)
        for stream in streams.values():
            stream.stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
