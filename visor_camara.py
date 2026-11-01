"""Visor RTSP sencillo para cámaras WiFi."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from collections import deque
import re
import socket
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import av
import numpy as np
import sounddevice as sd
from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal, QObject, QPoint, QEvent
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QShortcut, QKeyEvent
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget)

APP = "CamaraWifi"
DEFAULT_IP = "192.168.1.100"
URLS = {"alta": "/0/av0", "baja": "/0/av1"}
BUFFER_SAMPLES = 2400


def say_json(data):
    try:
        sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except (AttributeError, OSError):
        pass


def config_path():
    return Path(os.environ.get("APPDATA", str(Path.home()))) / APP / "config.json"


def read_config():
    defaults = {"ip": DEFAULT_IP, "calidad": "baja", "sonido": False,
                "volumen": 0.5, "geometria": None}
    try:
        value = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(value, dict):
            defaults.update(value)
    except (OSError, ValueError, TypeError):
        pass
    if defaults["calidad"] not in URLS:
        defaults["calidad"] = "baja"
    try:
        defaults["volumen"] = min(1.0, max(0.0, float(defaults["volumen"])))
    except (ValueError, TypeError):
        defaults["volumen"] = 0.5
    return defaults


def save_config(cfg):
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def rtsp_url(ip, quality):
    return f"rtsp://{ip}:554{URLS[quality]}"


def discover_camera(timeout=2.5):
    """Busca emisores ONVIF y devuelve la primera dirección IPv4 anunciada."""
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
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.settimeout(0.35)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.sendto(probe, ("239.255.255.250", 3702))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
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


class Decoder(threading.Thread):
    def __init__(self, ip, quality, stop_event, frame_cb=None, audio_cb=None,
                 discover=True, audio_enabled=False):
        super().__init__(daemon=True)
        self.ip = ip
        self.quality = quality
        self.stop_event = stop_event
        self.frame_cb = frame_cb
        self.audio_cb = audio_cb
        self.discover = discover
        self.audio_enabled = audio_enabled
        self.status_cb = None
        self.ip_cb = None
        self.error = ""
        self.frames = 0
        self.audio_packets = 0
        self.audio_samples = 0
        self.first_frame = None
        self.width = 0
        self.height = 0
        self.codec = ""

    def status(self, text):
        if self.status_cb:
            try:
                self.status_cb(text)
            except RuntimeError:
                pass

    def run(self):
        delay = 1.0
        while not self.stop_event.is_set():
            self.status("Conectando…")
            try:
                options = {"rtsp_transport": "tcp", "fflags": "nobuffer",
                           "flags": "low_delay", "max_delay": "0",
                           "timeout": "5000000", "analyzeduration": "0",
                           "probesize": "32768"}
                with av.open(rtsp_url(self.ip, self.quality), options=options,
                             timeout=(5, 5)) as container:
                    self.error = ""
                    got_frame = False
                    last_frame_at = time.monotonic()
                    streams = [stream for stream in container.streams
                               if stream.type == "video" or
                               stream.type == "audio"]
                    audio_streams = [stream for stream in streams if stream.type == "audio"]
                    resampler = (av.AudioResampler(format="s16", layout="mono", rate=8000)
                                 if audio_streams else None)
                    for packet in container.demux(streams):
                        if self.stop_event.is_set():
                            break
                        if packet.stream.type == "audio" and not (self.audio_enabled or self.frame_cb is None):
                            continue
                        if time.monotonic() - last_frame_at > 12:
                            raise RuntimeError("La cámara dejó de enviar imagen")
                        for frame in packet.decode():
                            if isinstance(frame, av.VideoFrame):
                                last_frame_at = time.monotonic()
                                self.width = frame.width
                                self.height = frame.height
                                self.codec = packet.stream.codec_context.name or "desconocido"
                                self.frames += 1
                                if not got_frame:
                                    got_frame = True
                                    self.status("En vivo")
                                    delay = 1.0
                                if self.first_frame is None:
                                    self.first_frame = time.monotonic()
                                if self.frame_cb:
                                    self.frame_cb(frame)
                            elif isinstance(frame, av.AudioFrame):
                                if self.audio_enabled or self.frame_cb is None:
                                    self.audio_packets += 1
                                if resampler is not None and (self.audio_enabled or self.frame_cb is None):
                                    for output in resampler.resample(frame):
                                        samples = output.to_ndarray().reshape(-1).copy()
                                        self.audio_samples += samples.size
                                        if self.audio_cb:
                                            self.audio_cb(samples)
                if not self.stop_event.is_set():
                    raise RuntimeError("La conexión RTSP se cerró")
            except Exception as exc:
                self.error = str(exc)
                self.status(f"Sin conexión, reintentando… {self.error}")
                if self.discover and not self.stop_event.wait(min(delay, 2)):
                    found = discover_camera()
                    if found and found != self.ip:
                        self.ip = found
                        if self.ip_cb:
                            self.ip_cb(found)
                self.stop_event.wait(delay)
                delay = min(10.0, delay * 1.7)


def run_test(seconds, quality, ip):
    stop = threading.Event()
    decoder = Decoder(ip, quality, stop, discover=False)
    start = time.monotonic()
    decoder.start()
    try:
        time.sleep(max(0, seconds))
    except KeyboardInterrupt:
        pass
    stop.set()
    decoder.join(timeout=1)
    elapsed = (time.monotonic() - decoder.first_frame
               if decoder.first_frame is not None else 0)
    data = {"conectado": decoder.frames > 0,
            "segundos_hasta_primer_fotograma": round(decoder.first_frame - start, 3)
                if decoder.first_frame else None,
            "fotogramas": decoder.frames,
            "fps": round(decoder.frames / max(0.001, elapsed), 2),
            "ancho": decoder.width, "alto": decoder.height, "codec": decoder.codec,
            "paquetes_audio": decoder.audio_packets,
            "muestras_audio": decoder.audio_samples, "error": decoder.error or None}
    say_json(data)
    return 0 if decoder.frames else 1


class VideoWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = None
        self.message = "Conectando…"
        self.setMinimumSize(1, 1)

    def set_image(self, image):
        self.image = image
        self.update()

    def set_message(self, message):
        if self.message != message:
            self.message = message
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if self.image is not None:
            scaled = self.image.scaled(self.size(), Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
            if self.message != "En vivo":
                metrics = painter.fontMetrics()
                rect = metrics.boundingRect(self.message).adjusted(-8, -5, 8, 5)
                rect.moveTopLeft(self.rect().topLeft() + QPoint(12, 12))
                painter.fillRect(rect, QColor(0, 0, 0, 150))
                painter.setPen(Qt.white)
                painter.drawText(rect.adjusted(8, 5, -8, -5), Qt.AlignLeft | Qt.AlignVCenter, self.message)
        else:
            painter.setPen(Qt.lightGray)
            painter.drawText(self.rect(), Qt.AlignCenter, self.message)


class CameraDialog(QDialog):
    def __init__(self, ip, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cámara")
        layout = QFormLayout(self)
        self.edit = QLineEdit(ip)
        layout.addRow("Dirección IP:", self.edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)


class UiSignals(QObject):
    status = Signal(int, str)
    ip = Signal(int, str)
    ptz_error = Signal(str)


class PtzController(threading.Thread):
    def __init__(self, ip_getter, error_cb):
        super().__init__(daemon=True)
        self.ip_getter = ip_getter
        self.error_cb = error_cb
        self.condition = threading.Condition()
        self.pending_stop = False
        self.pending_move = None
        self.closed = False
        self.order_lock = threading.Lock()
        self.ordenes_ok = 0
        self.ordenes_error = 0
        self.ultimo_error = None
        self.start()

    @staticmethod
    def envelope(operation):
        return ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                '<s:Body xmlns:t="http://www.onvif.org/ver20/ptz/wsdl" '
                'xmlns:tt="http://www.onvif.org/ver10/schema">' + operation +
                '</s:Body></s:Envelope>').encode("utf-8")

    def submit_move(self, x, y):
        with self.condition:
            if self.closed:
                return
            self.pending_move = (x, y)
            self.condition.notify()

    def submit_stop(self):
        with self.condition:
            if self.closed:
                return
            self.pending_stop = True
            self.pending_move = None
            self.condition.notify()

    def _send(self, payload):
        request = urllib.request.Request(
            f"http://{self.ip_getter()}/onvif/PTZ", data=payload,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"}, method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read()

    def _report(self, error):
        self.ultimo_error = str(error)
        self.ordenes_error += 1
        self.error_cb(self.ultimo_error)

    def run(self):
        while True:
            with self.condition:
                while not self.pending_stop and self.pending_move is None and not self.closed:
                    self.condition.wait()
                if self.pending_stop:
                    self.pending_stop = False
                    command = "stop"
                elif self.pending_move is not None:
                    command = "move"
                    x, y = self.pending_move
                    self.pending_move = None
                else:
                    return
            if command == "move":
                body = (f'<t:ContinuousMove><t:ProfileToken>Profile_1</t:ProfileToken>'
                        f'<t:Velocity><tt:PanTilt x="{x}" y="{y}"/></t:Velocity>'
                        f'</t:ContinuousMove>')
                try:
                    self._send(self.envelope(body))
                    self.ordenes_ok += 1
                except Exception as exc:
                    self._report(f"Movimiento: {exc}")
            else:
                body = ('<t:Stop><t:ProfileToken>Profile_1</t:ProfileToken>'
                        '<t:PanTilt>true</t:PanTilt><t:Zoom>true</t:Zoom></t:Stop>')
                for attempt in range(3):
                    try:
                        self._send(self.envelope(body))
                        self.ordenes_ok += 1
                        break
                    except Exception as exc:
                        if attempt == 2:
                            self._report(f"No se pudo detener la cámara: {exc}")

    def shutdown(self, timeout=1.5):
        self.submit_stop()
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.join(timeout=timeout)


class MainWindow(QMainWindow):
    def __init__(self, test_mode=False):
        super().__init__()
        self.test_mode = test_mode
        self.held_directions = set()
        self.last_motion_at = None
        self.cfg = read_config()
        self.stop = threading.Event()
        self.decoder = None
        self.decoder_generation = 0
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.converted_image = None
        self.last_painted_frame = None
        self.last_frame_at = None
        self.audio_queue = deque(maxlen=BUFFER_SAMPLES)
        self.connection_status = "Conectando…"
        self.audio_stream = None
        self.audio_error = ""
        self.samples_played = 0
        self.painted_frames = 0
        self.fullscreen = False
        self.signals = UiSignals(self)
        self.signals.status.connect(self.set_status)
        self.signals.ip.connect(self.update_ip)
        self.signals.ptz_error.connect(self.show_ptz_error)
        self.setWindowTitle("Visor de cámara WiFi")

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        self.video = VideoWidget()
        layout.addWidget(self.video, 1)
        self.status = QLabel("Conectando…")
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.controls = QWidget()
        controls = QHBoxLayout(self.controls)
        controls.setContentsMargins(6, 2, 6, 2)
        self.quality = QComboBox()
        self.quality.addItem("Alta", "alta")
        self.quality.addItem("Baja", "baja")
        self.quality.setCurrentIndex(0 if self.cfg["calidad"] == "alta" else 1)
        self.quality.currentIndexChanged.connect(self.change_quality)
        self.sound = QPushButton()
        self.sound.setCheckable(True)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(self.cfg["volumen"] * 100))
        self.volume.setFixedWidth(85)
        self.volume.valueChanged.connect(self.change_volume)
        photo = QPushButton("Foto")
        photo.clicked.connect(self.take_photo)
        full = QPushButton("Pantalla completa")
        full.clicked.connect(self.toggle_fullscreen)
        camera = QPushButton("Cámara")
        camera.clicked.connect(self.change_camera)
        self.ptz_buttons = {}
        pad = QWidget()
        pad_layout = QGridLayout(pad)
        pad_layout.setContentsMargins(0, 0, 0, 0)
        pad_layout.setSpacing(0)
        for direction, glyph, row, column in (("up", "▲", 0, 1), ("left", "◀", 1, 0),
                                               ("down", "▼", 1, 1), ("right", "▶", 1, 2)):
            button = QPushButton(glyph)
            button.setFocusPolicy(Qt.NoFocus)
            button.setToolTip({"up": "Arriba", "down": "Abajo", "left": "Izquierda", "right": "Derecha"}[direction])
            button.pressed.connect(lambda direction=direction: self.set_direction(direction, True))
            button.released.connect(lambda direction=direction: self.set_direction(direction, False))
            self.ptz_buttons[direction] = button
            pad_layout.addWidget(button, row, column)
        for widget in (self.quality, self.sound, self.volume, pad, photo, full, camera):
            controls.addWidget(widget)
        layout.addWidget(self.controls)
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self.sound.clicked.connect(self.toggle_sound)
        self.audio_enabled = False
        force_audio = test_mode and self.audio_device_available()
        self.sound.setChecked(force_audio or (not test_mode and bool(self.cfg["sonido"])))
        self.toggle_sound(initial=True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.paint_frame)
        self.timer.start(30)
        QShortcut(QKeySequence("F11"), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("Escape"), self, self.leave_fullscreen)
        QShortcut(QKeySequence("S"), self, self.sound.click)
        QShortcut(QKeySequence("F"), self, self.take_photo)
        if self.cfg.get("geometria") and not test_mode:
            try:
                self.restoreGeometry(bytes.fromhex(self.cfg["geometria"]))
            except (ValueError, TypeError):
                self.resize(960, 600)
        else:
            self.resize(960, 600)
        self.start_decoder(self.cfg["ip"], self.cfg["calidad"])
        self.ptz = PtzController(lambda: self.cfg["ip"], self.signals.ptz_error.emit)
        app = QApplication.instance()
        app.installEventFilter(self)
        self.safety_timer = QTimer(self)
        self.safety_timer.timeout.connect(self.check_motion_timeout)
        self.safety_timer.start(250)

    def show_ptz_error(self, message):
        self.status.setText(message)

    def set_direction(self, direction, pressed):
        before = set(self.held_directions)
        if pressed:
            self.held_directions.add(direction)
        else:
            self.held_directions.discard(direction)
        if before == self.held_directions:
            return
        if not self.held_directions:
            self.last_motion_at = None
            self.ptz.submit_stop()
            return
        if self.last_motion_at is None:
            self.last_motion_at = time.monotonic()
        x = 0.5 * (int("right" in self.held_directions) - int("left" in self.held_directions))
        y = 0.5 * (int("up" in self.held_directions) - int("down" in self.held_directions))
        self.ptz.submit_move(x, y)

    def stop_for_safety(self):
        self.held_directions.clear()
        self.last_motion_at = None
        self.ptz.submit_stop()

    def check_motion_timeout(self):
        if self.last_motion_at is not None and time.monotonic() - self.last_motion_at >= 30:
            self.stop_for_safety()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.KeyPress, QEvent.KeyRelease):
            key_map = {Qt.Key_Right: "right", Qt.Key_Left: "left",
                       Qt.Key_Up: "up", Qt.Key_Down: "down"}
            direction = key_map.get(event.key())
            app = QApplication.instance()
            if direction and app and app.activeWindow() is self:
                if not event.isAutoRepeat():
                    self.set_direction(direction, event.type() == QEvent.KeyPress)
                return True
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow():
            self.stop_for_safety()

    @staticmethod
    def audio_device_available():
        try:
            return sd.default.device[1] is not None
        except Exception:
            return False

    def start_decoder(self, ip, quality):
        if self.decoder and self.decoder.is_alive():
            self.stop.set()
        self.decoder_generation += 1
        generation = self.decoder_generation
        self.stop = threading.Event()
        self.cfg["ip"] = ip
        self.cfg["calidad"] = quality
        decoder = Decoder(ip, quality, self.stop,
                          lambda frame: self.store_frame(generation, frame),
                          lambda samples: self.audio_queue.extend(samples.tolist()),
                          audio_enabled=self.audio_enabled)
        decoder.status_cb = lambda text: self.signals.status.emit(generation, text)
        decoder.ip_cb = lambda found: self.signals.ip.emit(generation, found)
        self.decoder = decoder
        decoder.start()

    def store_frame(self, generation, frame):
        if generation != self.decoder_generation:
            return
        with self.frame_lock:
            self.latest_frame = frame

    def set_status(self, generation, text):
        if generation != self.decoder_generation:
            return
        self.connection_status = text
        self.refresh_stall_status()

    def update_ip(self, generation, ip):
        if generation != self.decoder_generation:
            return
        self.cfg["ip"] = ip
        if not self.test_mode:
            save_config(self.cfg)
        self.set_status(generation, "Cámara encontrada: " + ip)

    def audio_callback(self, outdata, frames, time_info, status):
        data = np.zeros(frames, dtype=np.float32)
        count = 0
        for index in range(frames):
            try:
                data[index] = self.audio_queue.popleft() / 32768.0
                count += 1
            except IndexError:
                break
        data *= self.cfg["volumen"]
        outdata[:, 0] = data
        self.samples_played += count

    def paint_frame(self):
        with self.frame_lock:
            frame = self.latest_frame
            is_new = frame is not None and frame is not self.last_painted_frame
            if is_new:
                self.last_painted_frame = frame
        if frame is None:
            return
        if is_new:
            array = frame.to_ndarray(format="rgb24")
            height, width, _ = array.shape
            self.converted_image = QImage(array.data, width, height, width * 3,
                                          QImage.Format_RGB888).copy()
            self.last_frame_at = time.monotonic()
            self.painted_frames += 1
        if is_new:
            self.video.set_image(self.converted_image)
        self.refresh_stall_status()

    def refresh_stall_status(self, connection_status=None):
        connection_status = self.connection_status
        if self.last_frame_at is None:
            self.set_message(connection_status)
            self.status.setText(connection_status)
            return
        elapsed = time.monotonic() - self.last_frame_at
        if elapsed > 3:
            message = f"Imagen detenida hace {int(elapsed)} s"
            if connection_status != "En vivo":
                message += " · " + connection_status
        elif connection_status != "En vivo":
            message = connection_status
        else:
            message = "En vivo"
        self.status.setText(message)
        self.set_message(message)

    def set_message(self, message):
        if self.video.message != message:
            self.video.set_message(message)

    def change_quality(self, index):
        self.cfg["calidad"] = self.quality.currentData()
        if not self.test_mode:
            save_config(self.cfg)
        self.start_decoder(self.cfg["ip"], self.cfg["calidad"])

    def toggle_sound(self, checked=None, initial=False):
        enabled = self.sound.isChecked()
        if enabled == self.audio_enabled and not initial:
            return
        if enabled:
            try:
                self.audio_stream = sd.OutputStream(samplerate=8000, channels=1,
                    dtype="float32", callback=self.audio_callback, blocksize=0)
                self.audio_stream.start()
                self.audio_error = ""
            except Exception as exc:
                self.audio_error = str(exc)
                self.audio_enabled = False
                self.sound.setChecked(False)
                self.sound.setText("Sonido: error")
                self.status.setText("Audio: " + self.audio_error)
                return
        else:
            self.close_audio_stream()
        self.audio_enabled = enabled
        self.sound.setText("Sonido: encendido" if enabled else "Sonido: apagado")
        if not initial and not self.test_mode:
            self.cfg["sonido"] = enabled
            save_config(self.cfg)
        if self.decoder:
            self.decoder.audio_enabled = enabled

    def close_audio_stream(self):
        if self.audio_stream:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except Exception:
                pass
            self.audio_stream = None
        self.audio_queue.clear()

    def change_volume(self, value):
        self.cfg["volumen"] = value / 100
        if not self.test_mode:
            save_config(self.cfg)

    def take_photo(self):
        if self.converted_image is None:
            self.status.setText("Aún no hay imagen para guardar")
            return
        pictures = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
        folder = Path(pictures) / APP
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".png")
            if not self.converted_image.save(str(path), "PNG"):
                self.status.setText("No se pudo guardar la foto: error al escribir PNG")
                return
            self.status.setText("Foto guardada: " + str(path))
        except OSError as exc:
            self.status.setText("No se pudo guardar la foto: " + str(exc))

    def change_camera(self):
        dialog = CameraDialog(self.cfg["ip"], self)
        if dialog.exec() == QDialog.Accepted and dialog.edit.text().strip():
            self.cfg["ip"] = dialog.edit.text().strip()
            save_config(self.cfg)
            self.start_decoder(self.cfg["ip"], self.cfg["calidad"])

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self.controls.hide()
            self.status.hide()
            self.showFullScreen()
        else:
            self.controls.show()
            self.status.show()
            self.showNormal()

    def leave_fullscreen(self):
        if self.fullscreen:
            self.toggle_fullscreen()

    def mouseDoubleClickEvent(self, event):
        self.toggle_fullscreen()
        super().mouseDoubleClickEvent(event)

    def closeEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        if hasattr(self, "ptz"):
            self.ptz.shutdown(1.5)
        self.stop.set()
        if self.decoder:
            self.decoder.join(timeout=1)
        self.close_audio_stream()
        if not self.test_mode:
            self.cfg["geometria"] = bytes(self.saveGeometry()).hex()
            save_config(self.cfg)
        event.accept()


def run_window_test(seconds):
    app = QApplication(sys.argv)
    window = MainWindow(test_mode=True)
    window.show()
    QTimer.singleShot(max(0, int(seconds * 1000)), window.close)
    app.exec()
    say_json({"fotogramas_pintados": window.painted_frames,
              "estado_final": window.status.text(),
              "audio_activo": window.audio_enabled,
              "muestras_reproducidas": window.samples_played,
              "error_audio": window.audio_error or None})
    return 0 if window.painted_frames else 1


def run_ptz_test():
    app = QApplication(sys.argv)
    window = MainWindow(test_mode=True)
    window.show()
    steps = []
    for direction in ("right", "left", "up", "down"):
        button = window.ptz_buttons[direction]
        steps.extend(((0, button.pressed.emit), (400, button.released.emit),
                      (1500, lambda: None)))
    for key in (Qt.Key_Right, Qt.Key_Left):
        press = QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)
        release = QKeyEvent(QEvent.KeyRelease, key, Qt.NoModifier)
        steps.extend(((0, lambda event=press: app.sendEvent(app, event)),
                      (400, lambda event=release: app.sendEvent(app, event)),
                      (1500, lambda: None)))
    steps.extend(((0, lambda: window.set_direction("right", True)),
                  (400, window.stop_for_safety),
                  (0, lambda: window.set_direction("left", True)),
                  (400, lambda: window.set_direction("left", False))))

    def run_step(index=0):
        if index >= len(steps):
            QTimer.singleShot(3500, finish)
            return
        delay, action = steps[index]
        def perform():
            action()
            run_step(index + 1)
        QTimer.singleShot(delay, perform)

    def finish():
        data = {"ordenes_ok": window.ptz.ordenes_ok,
                "ordenes_error": window.ptz.ordenes_error,
                "ultimo_error": window.ptz.ultimo_error,
                "sostenidas_al_final": sorted(window.held_directions)}
        window.close()
        say_json(data)
        app.setProperty("ptz_test_result", 0 if not data["ordenes_error"] and
                        not data["sostenidas_al_final"] else 1)
        app.exit()

    QTimer.singleShot(0, run_step)
    app.exec()
    return app.property("ptz_test_result") or 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prueba", type=float, metavar="SEGUNDOS")
    parser.add_argument("--prueba-ventana", type=float, metavar="SEGUNDOS")
    parser.add_argument("--prueba-ptz", action="store_true")
    parser.add_argument("--calidad", choices=URLS)
    parser.add_argument("--ip")
    args = parser.parse_args()
    if args.prueba is not None:
        cfg = read_config()
        return run_test(args.prueba, args.calidad or cfg["calidad"], args.ip or cfg["ip"])
    if args.prueba_ventana is not None:
        return run_window_test(args.prueba_ventana)
    if args.prueba_ptz:
        return run_ptz_test()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
