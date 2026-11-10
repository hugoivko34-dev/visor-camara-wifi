"""Video, audio y archivos multimedia de la cámara RTSP."""
from __future__ import annotations

import av
import collections
from fractions import Fraction
import numpy as np
import os
import shutil
import tempfile
import threading
import traceback
import time
from datetime import datetime
from pathlib import Path

URLS = {"baja": "/0/av1", "alta": "/0/av0"}
OPTIONS = {"rtsp_transport": "tcp", "fflags": "nobuffer", "flags": "low_delay", "max_delay": "0", "timeout": "5000000", "analyzeduration": "0", "probesize": "32768"}
BOUNDARY = "fotograma"
_obtener_ip = lambda: "192.168.1.100"
_write_log = print
_streams = {}
_recording = None
_recording_lock = threading.Lock()
_hls = {}
_thumbs = {}


def _is_recording(quality=None):
    return bool(_recording and not _recording.get("done") and (quality is None or _recording["quality"] == quality))


def configurar(obtener_ip=None, write_log=None):
    global _obtener_ip, _write_log
    if obtener_ip is not None:
        _obtener_ip = obtener_ip
    if write_log is not None:
        _write_log = write_log


def carpeta_fotos():
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Pictures" / "CamaraWifi"


def carpeta_clips():
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Videos" / "CamaraWifi"


class GrabacionEnCurso(Exception):
    pass


class Stream:
    def __init__(self, quality):
        self.quality = quality
        self.lock = threading.Condition()
        self.clients = 0
        self.audio_queues = []
        self.jpegs = {}
        self.sequence = 0
        self.last_frame = 0.0
        self.error = ""
        self.connected = False
        self.active = False
        self.stop = threading.Event()
        self.reconnect = threading.Event()
        self.metrics = collections.deque()
        self.sent = collections.deque()
        self.record_audio = collections.deque(maxlen=40000)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        delay = 1
        idle_since = None
        while not self.stop.is_set():
            with self.lock:
                needed = self.clients > 0 or len(self.audio_queues) > 0 or _is_recording(self.quality) or self.quality in _hls
                self.active = needed
                if not needed:
                    if idle_since is None:
                        idle_since = time.monotonic()
                    if time.monotonic() - idle_since < 5:
                        self.lock.wait(timeout=0.5)
                        continue
                    self.jpegs.clear()
                    self.last_frame = 0
                    self.connected = False
                    idle_since = None
                    continue
                idle_since = None
            container = None
            got_frame = False
            try:
                container = av.open(f"rtsp://{_obtener_ip()}:554{URLS[self.quality]}", options=OPTIONS, timeout=(5, 5))
                vin = container.streams.video[0]
                ain = next((s for s in container.streams.audio if s.codec_context.name == "pcm_alaw"), None)
                resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
                last_frame_at = time.monotonic()
                delay = 1
                with self.lock:
                    self.connected = False
                    self.jpegs.clear()
                for packet in container.demux():
                    if self.reconnect.is_set():
                        self.reconnect.clear()
                        break
                    with self.lock:
                        needed = self.clients > 0 or len(self.audio_queues) > 0 or _is_recording(self.quality) or self.quality in _hls
                    if not needed and idle_since is None:
                        idle_since = time.monotonic()
                    if idle_since and time.monotonic() - idle_since >= 5:
                        break
                    if packet.stream == vin:
                        now = time.monotonic()
                        self.metrics.append((now, "frame", 1))
                        self.metrics.append((now, "bytes", packet.size))
                        _record_video(self.quality, packet)
                        _hls_video(self.quality, packet)
                        for frame in packet.decode():
                            last_frame_at = time.monotonic()
                            with self.lock:
                                qs = {q for q, count in _client_qualities(self) if count}
                            for q in qs:
                                ctx = _jpeg_encoder(q, frame.width, frame.height)
                                jpg = b"".join(bytes(p) for p in ctx.encode(frame.reformat(format="yuvj420p")))
                                if jpg:
                                    with self.lock:
                                        self.jpegs[q] = (jpg, self.sequence + 1)
                            with self.lock:
                                self.sequence += 1
                                self.last_frame = time.monotonic()
                                self.connected = True
                                self.error = ""
                                got_frame = True
                                self.lock.notify_all()
                    elif ain is not None and packet.stream == ain:
                        if self.audio_queues or _is_recording(self.quality) or self.quality in _hls:
                            for frame in packet.decode():
                                for pcm in resampler.resample(frame):
                                    raw = bytes(pcm.planes[0])
                                    with self.lock:
                                        for q in tuple(self.audio_queues):
                                            q.extend(raw)
                                        self.record_audio.extend(raw)
                                        self.lock.notify_all()
                                    _record_audio(raw)
                                    _hls_audio(raw)
                    if time.monotonic() - last_frame_at > 12:
                        raise RuntimeError("La cámara dejó de enviar imagen")
                if not self.stop.is_set() and (self.clients or self.audio_queues):
                    raise RuntimeError("La conexión RTSP se cerró")
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
            finally:
                if container is not None:
                    container.close()
                with self.lock:
                    self.connected = bool(got_frame and (self.clients or self.audio_queues))
                    self.lock.notify_all()
            if self.clients or self.audio_queues:
                self.reconnect.wait(delay)
                delay = min(delay + 1, 10)

    def status(self):
        age = None if not self.last_frame else max(0, round(time.monotonic() - self.last_frame, 1))
        return {"conectado": bool(self.connected and self.last_frame), "segundos_desde_ultimo_fotograma": age, "calidad_activa": bool(self.active), "ultimo_error": self.error, "ip_camara": _obtener_ip()}


def _client_qualities(stream):
    return getattr(stream, "quality_counts", {}).items()


def _jpeg_encoder(q, width, height):
    cache = getattr(_jpeg_encoder, "cache", {})
    key = (q, width, height)
    if key not in cache:
        ctx = av.CodecContext.create("mjpeg", "w")
        ctx.width = width
        ctx.height = height
        ctx.pix_fmt = "yuvj420p"
        ctx.options = {"qscale": str(q)}
        cache[key] = ctx
        _jpeg_encoder.cache = cache
    return cache[key]


for _quality in URLS:
    _streams[_quality] = Stream(_quality)


def _is_recording(quality=None):
    return bool(_recording and not _recording.get("done") and (quality is None or _recording["quality"] == quality))


def metricas(calidad):
    stream = _streams[calidad]
    now = time.monotonic()
    while stream.metrics and now - stream.metrics[0][0] > 3:
        stream.metrics.popleft()
    while stream.sent and now - stream.sent[0][0] > 3:
        stream.sent.popleft()
    frames = sum(v for t, k, v in stream.metrics if k == "frame")
    byte_count = sum(v for t, k, v in stream.metrics if k == "bytes")
    sent_bytes = sum(v for t, v in stream.sent)
    return {"fps": round(frames / 3, 1), "kbps": round(byte_count * 8 / 3000, 1), "kbps_enviados": round(sent_bytes * 8 / 3000, 1), "clientes_video": stream.clients}


def registrar_cliente(calidad, q):
    stream = _streams[calidad]
    with stream.lock:
        stream.clients += 1
        stream.quality_counts = getattr(stream, "quality_counts", collections.Counter())
        stream.quality_counts[q] += 1
        stream.lock.notify_all()


def quitar_cliente(calidad, q):
    stream = _streams[calidad]
    with stream.lock:
        stream.clients = max(0, stream.clients - 1)
        stream.quality_counts[q] = max(0, stream.quality_counts[q] - 1)
        stream.lock.notify_all()


def fotograma(calidad, q, seen, fps=0, anterior=0):
    stream = _streams[calidad]
    with stream.lock:
        stream.lock.wait_for(lambda: (q in stream.jpegs and stream.jpegs[q][1] > seen) or shutdown_event(), timeout=5)
        return stream.jpegs.get(q, (None, seen))


def shutdown_event():
    return False


def registrar_envio(calidad, n):
    stream = _streams[calidad]
    stream.sent.append((time.monotonic(), n))


def audio_pcm():
    stream = _streams.get("baja") if _streams.get("baja") and _streams["baja"].active else next((s for s in _streams.values() if s.active), _streams["baja"])
    queue = collections.deque(maxlen=8000 * 2)
    with stream.lock:
        stream.audio_queues.append(queue)
        stream.lock.notify_all()
    try:
        while True:
            with stream.lock:
                stream.lock.wait_for(lambda: len(queue) >= 3200, timeout=1)
                if len(queue) < 3200:
                    continue
                chunk = bytes(queue.popleft() for _ in range(3200))
            yield chunk
    finally:
        with stream.lock:
            stream.audio_queues.remove(queue)
            stream.lock.notify_all()


def tomar_foto(calidad):
    stream = _streams[calidad]
    registrar_cliente(calidad, 3)
    try:
        with stream.lock:
            stream.lock.wait_for(lambda: 3 in stream.jpegs, timeout=12)
            jpg = stream.jpegs.get(3, (None, 0))[0]
        if not jpg:
            raise TimeoutError("No llegó ningún fotograma en 12 segundos")
        name = "foto_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".jpg"
        carpeta_fotos().mkdir(parents=True, exist_ok=True)
        (carpeta_fotos() / name).write_bytes(jpg)
        return name, jpg
    finally:
        quitar_cliente(calidad, 3)


def iniciar_grabacion(segundos, calidad):
    global _recording
    with _recording_lock:
        if _recording and not _recording.get("done"):
            raise GrabacionEnCurso()
        carpeta_clips().mkdir(parents=True, exist_ok=True)
        name = "clip_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".mp4"
        rec = {"archivo": str(carpeta_clips() / name), "nombre": name, "quality": calidad, "segundos": segundos,
               "inicio": time.monotonic(), "done": False, "cerrado": False, "lock": threading.Lock(),
               "out": None, "vout": None, "aout": None, "resampler": None, "last_dts": None,
               "audio_buffer": bytearray(), "audio_pts": 0, "video_packets": 0, "error": None}
        _recording = rec
        threading.Thread(target=_finish_recording, args=(rec,), daemon=True).start()
        _streams[calidad].lock.acquire()
        _streams[calidad].lock.notify_all()
        _streams[calidad].lock.release()
        return name


def _mux_conservando_tiempos(out, packet, vout):
    pts, dts = packet.pts, packet.dts
    time_base = packet.time_base
    stream_original = packet.stream
    packet.stream = vout
    try:
        out.mux(packet)
    finally:
        packet.stream = stream_original
        packet.pts, packet.dts = pts, dts
        try:
            packet.time_base = time_base
        except Exception:
            pass


def _record_video(quality, packet):
    rec = _recording
    if not rec or rec.get("done") or rec["quality"] != quality:
        return
    with rec["lock"]:
        if rec.get("cerrado") or packet.dts is None:
            return
        if rec["last_dts"] is not None and packet.dts <= rec["last_dts"]:
            return
        try:
            if rec["out"] is None:
                if not packet.is_keyframe:
                    return
                out = av.open(rec["archivo"], "w", options={"movflags": "+faststart"})
                vout = out.add_stream_from_template(packet.stream)
                try:
                    vout.codec_context.codec_tag = "hvc1"
                except Exception:
                    pass
                aout = out.add_stream("aac", rate=8000)
                aout.codec_context.format = "fltp"
                aout.codec_context.layout = "mono"
                rec["out"], rec["vout"], rec["aout"] = out, vout, aout
                rec["resampler"] = av.AudioResampler(format="fltp", layout="mono", rate=8000)
            if packet.pts is None:
                packet.pts = packet.dts
            _mux_conservando_tiempos(rec["out"], packet, rec["vout"])
            rec["last_dts"] = packet.dts
            rec["video_packets"] += 1
            if packet.is_keyframe and time.monotonic() - rec["inicio"] >= rec["segundos"]:
                rec["done"] = True
        except Exception:
            rec["error"] = traceback.format_exc()
            rec["done"] = True


def _record_audio(raw):
    rec = _recording
    if not rec or rec.get("done"):
        return
    with rec["lock"]:
        if rec.get("cerrado") or rec["out"] is None or rec["aout"] is None:
            return
        try:
            rec["audio_buffer"].extend(raw)
            while len(rec["audio_buffer"]) >= 2048:
                payload = bytes(rec["audio_buffer"][:2048])
                del rec["audio_buffer"][:2048]
                samples = np.frombuffer(payload, dtype=np.int16).reshape(1, 1024)
                frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
                frame.sample_rate = 8000
                for converted in rec["resampler"].resample(frame):
                    converted.pts = rec["audio_pts"]
                    converted.time_base = Fraction(1, 8000)
                    rec["audio_pts"] += converted.samples
                    for packet in rec["aout"].encode(converted):
                        rec["out"].mux(packet)
        except Exception:
            if rec["aout"] is not None:
                _write_log("Grabación, audio desactivado: " + traceback.format_exc())
            rec["aout"] = None


def _finish_recording(rec):
    while not rec["done"] and time.monotonic() - rec["inicio"] < rec["segundos"] + 3:
        time.sleep(0.25)
    rec["done"] = True
    with rec["lock"]:
        rec["cerrado"] = True
        out = rec["out"]
        try:
            if out is not None:
                if rec["aout"] is not None:
                    try:
                        for packet in rec["aout"].encode(None):
                            out.mux(packet)
                    except Exception:
                        pass
                out.close()
        except Exception:
            rec["error"] = (rec["error"] or "") + traceback.format_exc()
    try:
        if rec["error"]:
            raise RuntimeError(rec["error"].strip().splitlines()[-1])
        if out is None or not rec["video_packets"]:
            raise RuntimeError("La grabación no recibió video")
        with av.open(rec["archivo"]) as check:
            if not check.streams.video or not check.duration:
                raise RuntimeError("El clip no tiene duración")
    except Exception as exc:
        try:
            Path(rec["archivo"]).unlink(missing_ok=True)
        except OSError:
            pass
        _write_log(f"Error al finalizar grabación: {exc}")


def detener_grabacion():
    if _recording:
        _recording["done"] = True


def estado_grabacion():
    if not _recording or _recording.get("cerrado"):
        return None
    return {"activo": True, "segundos": round(time.monotonic() - _recording["inicio"]), "archivo": _recording["nombre"]}


def hls_iniciar(calidad):
    item = _hls.get(calidad)
    if item:
        item["ultimo_pedido"] = time.monotonic()
        return
    directory = Path(tempfile.gettempdir()) / ("camara_hls_" + calidad)
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    _hls[calidad] = {"directorio": directory, "ultimo_pedido": time.monotonic(), "out": None, "vout": None, "lock": threading.Lock(), "cerrado": False, "last_dts": None}
    _streams[calidad].lock.acquire()
    _streams[calidad].lock.notify_all()
    _streams[calidad].lock.release()
    threading.Thread(target=_hls_watch, args=(calidad,), daemon=True).start()


def _hls_watch(calidad):
    while calidad in _hls:
        time.sleep(1)
        if time.monotonic() - _hls[calidad]["ultimo_pedido"] > 20:
            item = _hls.pop(calidad)
            with item["lock"]:
                item["cerrado"] = True
                try:
                    if item["out"]:
                        item["out"].close()
                except Exception:
                    pass
            shutil.rmtree(item["directorio"], ignore_errors=True)
            return


def _hls_video(quality, packet):
    item = _hls.get(quality)
    if not item:
        return
    with item["lock"]:
        if item.get("cerrado") or item.get("failed") or packet.dts is None:
            return
        if item["last_dts"] is not None and packet.dts <= item["last_dts"]:
            return
        if packet.pts is None:
            packet.pts = packet.dts
        try:
            if item["out"] is None:
                item["out"] = av.open(str(item["directorio"] / "index.m3u8"), "w", format="hls", options={"hls_time": "1", "hls_list_size": "4", "hls_flags": "delete_segments+independent_segments+omit_endlist", "hls_segment_type": "fmp4", "hls_fmp4_init_filename": str(item["directorio"] / "init.mp4"), "hls_segment_filename": str(item["directorio"] / "seg%d.m4s")})
                try:
                    item["vout"] = item["out"].add_stream_from_template(packet.stream)
                except (AttributeError, TypeError):
                    item["vout"] = item["out"].add_stream(template=packet.stream)
                try:
                    item["vout"].codec_context.codec_tag = "hvc1"
                except Exception:
                    pass
                item["first_keyframe"] = False
                item["aout"] = None
                item["audio_buffer"] = bytearray()
                item["audio_pts"] = 0
                item["audio_error_logged"] = False
                try:
                    item["aout"] = item["out"].add_stream("aac", rate=8000)
                    item["aout"].codec_context.format = "fltp"
                    item["aout"].codec_context.layout = "mono"
                    item["resampler"] = av.AudioResampler(format="fltp", layout="mono", rate=8000)
                except Exception:
                    item["aout"] = None
                    _write_log("HLS audio " + quality + ": " + traceback.format_exc())
            if not item["first_keyframe"]:
                if not packet.is_keyframe:
                    return
                item["first_keyframe"] = True
            _mux_conservando_tiempos(item["out"], packet, item["vout"])
            item["last_dts"] = packet.dts
        except Exception:
            item["failed"] = True
            item["aout"] = None
            _write_log("HLS " + quality + ": " + traceback.format_exc())
            try:
                if item.get("out"):
                    item["out"].close()
            except Exception:
                pass
            item["out"] = None


def _hls_audio(raw):
    for item in tuple(_hls.values()):
        with item["lock"]:
            if item.get("cerrado") or item.get("aout") is None:
                continue
            if not item.get("first_keyframe"):
                item["audio_buffer"].clear()
                continue
            try:
                item["audio_buffer"].extend(raw)
                while len(item["audio_buffer"]) >= 2048:
                    payload = bytes(item["audio_buffer"][:2048])
                    del item["audio_buffer"][:2048]
                    samples = np.frombuffer(payload, dtype=np.int16).reshape(1, 1024)
                    frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
                    frame.sample_rate = 8000
                    for converted in item["resampler"].resample(frame):
                        converted.pts = item["audio_pts"]
                        converted.time_base = Fraction(1, 8000)
                        item["audio_pts"] += converted.samples
                        for packet in item["aout"].encode(converted):
                            item["out"].mux(packet)
            except Exception:
                if not item.get("audio_error_logged"):
                    _write_log("HLS audio: " + traceback.format_exc())
                    item["audio_error_logged"] = True
                item["aout"] = None


def hls_lista(calidad):
    """Devuelve index.m3u8 con las rutas absolutas convertidas en nombres relativos."""
    item = _hls.get(calidad)
    if not item or item.get("failed"):
        return None
    item["ultimo_pedido"] = time.monotonic()
    path = item["directorio"] / "index.m3u8"
    if not path.is_file():
        return None
    texto = path.read_text(encoding="utf-8", errors="replace")
    base = str(item["directorio"])
    for prefijo in (base + "\\", base + "/", base.replace("\\", "/") + "/"):
        texto = texto.replace(prefijo, "")
    return texto.encode("utf-8")


def hls_archivo(calidad, nombre):
    if nombre not in ("index.m3u8", "init.mp4") and not nombre.endswith(".m4s"):
        return None
    item = _hls.get(calidad)
    if not item or item.get("failed"):
        return None
    if nombre == "index.m3u8":
        item["ultimo_pedido"] = time.monotonic()
    path = item["directorio"] / nombre
    return path if path.is_file() else None


def listar_archivos():
    rows = []
    for folder, pattern, kind in ((carpeta_fotos(), "foto_*.jpg", "foto"), (carpeta_clips(), "clip_*.mp4", "clip")):
        if folder.exists():
            for file in folder.glob(pattern):
                try:
                    stat = file.stat()
                    rows.append({"nombre": file.name, "tipo": kind, "bytes": stat.st_size, "fecha": datetime.fromtimestamp(stat.st_mtime).isoformat()})
                except OSError:
                    pass
    return sorted(rows, key=lambda x: x["fecha"], reverse=True)[:200]


def ruta_archivo(nombre):
    if "/" in nombre or "\\" in nombre or ".." in nombre:
        return None
    if nombre.startswith("foto_") and nombre.endswith(".jpg"):
        path = carpeta_fotos() / nombre
    elif nombre.startswith("clip_") and nombre.endswith(".mp4"):
        path = carpeta_clips() / nombre
    else:
        return None
    return path if path.is_file() else None


def miniatura(nombre):
    if nombre in _thumbs:
        return _thumbs[nombre]
    path = ruta_archivo(nombre)
    if not path:
        return None
    try:
        with av.open(str(path), format="image2") as container:
            frame = next(container.decode(video=0))
        alto = max(2, int(round((frame.height * 320 / frame.width) / 2) * 2))
        frame = frame.reformat(width=320, height=alto, format="yuvj420p")
        enc = av.CodecContext.create("mjpeg", "w")
        enc.pix_fmt = "yuvj420p"
        enc.width = frame.width
        enc.height = frame.height
        enc.options = {"qscale": "6"}
        enc.open()
        data = b"".join(bytes(p) for p in enc.encode(frame))
    except Exception:
        _write_log("Error miniatura: " + traceback.format_exc())
        raise
    _thumbs[nombre] = data
    return data
