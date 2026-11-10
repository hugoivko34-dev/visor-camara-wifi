"""Funciones ONVIF, descubrimiento y configuración de la cámara."""
from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

NS_DISPOSITIVO = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
NS_SCHEMA = "http://www.onvif.org/ver10/schema"
NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
_ip_lock = threading.Lock()
_ip = None
_ultima_busqueda = 0.0
_lock_busqueda = threading.Lock()
_callback_reconexion = None
_cache_info = (0.0, None)


class ErrorCamara(Exception):
    pass


def configurar(ip_inicial, callback_reconexion=None):
    global _ip, _callback_reconexion
    with _ip_lock:
        _ip = ip_inicial
    _callback_reconexion = callback_reconexion


def ruta_configuracion():
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "CamaraWifi" / "config.json"


def cargar_ip(ip_predeterminada):
    try:
        config = json.loads(ruta_configuracion().read_text(encoding="utf-8"))
        return str(config.get("ip") or ip_predeterminada) if isinstance(config, dict) else ip_predeterminada
    except (OSError, ValueError, TypeError):
        return ip_predeterminada


def guardar_ip(ip):
    ruta = ruta_configuracion()
    try:
        try:
            config = json.loads(ruta.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                config = {}
        except (OSError, ValueError, TypeError):
            config = {}
        config["ip"] = ip
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def ip_actual():
    with _ip_lock:
        return _ip


def fijar_ip(ip):
    global _ip, _cache_info
    try:
        validada = str(ipaddress.IPv4Address(ip))
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, TypeError) as exc:
        raise ValueError("La dirección IP no es válida") from exc
    with _ip_lock:
        _ip = validada
    guardar_ip(validada)
    _cache_info = (0.0, None)
    if _callback_reconexion:
        _callback_reconexion()
    return validada


def limpiar_xml(xml):
    texto = xml.decode("utf-8", "replace") if isinstance(xml, bytes) else str(xml)
    texto = re.sub(r"\s+xmlns(?::[\w.-]+)?=(?:\"[^\"]*\"|'[^']*')", "", texto)
    texto = re.sub(r"(<\s*/?\s*)[\w.-]+:", r"\1", texto)
    texto = re.sub(r"\s+[\w.-]+:[\w.-]+=(\"[^\"]*\"|'[^']*')", "", texto)
    return texto


def onvif(ruta, namespace, cuerpo, timeout=5):
    envelope = (f'<s:Envelope xmlns:s="{NS_SOAP}"><s:Body xmlns:t="{namespace}" '
                f'xmlns:tt="{NS_SCHEMA}">{cuerpo}</s:Body></s:Envelope>').encode()
    request = urllib.request.Request(f"http://{ip_actual()}{ruta}", data=envelope,
                                     headers={"Content-Type": "application/soap+xml; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _llamar(ruta, namespace, operacion, contenido="", timeout=5):
    estado, xml = onvif(ruta, namespace, f"<t:{operacion}>{contenido}</t:{operacion}>", timeout)
    if not 200 <= estado < 300:
        raise ErrorCamara(f"La cámara respondió HTTP {estado}")
    return ET.fromstring(limpiar_xml(xml))


def _local(elemento, nombre):
    if elemento is None:
        return None
    return next((item for item in elemento.iter() if item.tag.rsplit("}", 1)[-1] == nombre), None)


def _texto(elemento, nombre, default=None):
    item = _local(elemento, nombre)
    return (item.text or "").strip() if item is not None else default


def soap(move=None, timeout=3):
    if move is None:
        body = '<t:Stop><t:ProfileToken>Profile_1</t:ProfileToken><t:PanTilt>true</t:PanTilt><t:Zoom>true</t:Zoom></t:Stop>'
    else:
        x, y = move
        body = (f'<t:ContinuousMove><t:ProfileToken>Profile_1</t:ProfileToken>'
                f'<t:Velocity><tt:PanTilt x="{x}" y="{y}"/></t:Velocity></t:ContinuousMove>')
    try:
        status, _ = onvif("/onvif/PTZ", NS_PTZ, body, timeout)
        return 200 <= status < 300
    except Exception:
        return False


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
                probe = (f'<e:Envelope xmlns:e="{NS_SOAP}" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
                         'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
                         'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>' + message_id
                         + '</w:MessageID><w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
                         '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
                         '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>').encode()
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
                if b"NetworkVideoTransmitter" not in data:
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


def camera_port_responds(ip):
    try:
        with socket.create_connection((ip, 554), timeout=1.5):
            return True
    except Exception:
        return False


def scan_for_camera():
    try:
        own_ip = socket.gethostbyname(socket.gethostname())
        if own_ip == "127.0.0.1":
            return None
        subnet = ".".join(own_ip.split(".")[:3])
        candidates = [f"{subnet}.{host}" for host in range(1, 255) if f"{subnet}.{host}" != own_ip]

        def check(candidate):
            try:
                with socket.create_connection((candidate, 554), timeout=1.0) as conn:
                    conn.settimeout(1.5)
                    conn.sendall(f"OPTIONS rtsp://{candidate}:554/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
                    return candidate if conn.recv(1024).startswith(b"RTSP/1.0") else None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=64) as executor:
            futures = [executor.submit(check, candidate) for candidate in candidates]
            for future in as_completed(futures):
                found = future.result()
                if found:
                    for pending in futures:
                        pending.cancel()
                    return found
    except OSError:
        pass
    return None


def find_camera():
    return discover_camera() or scan_for_camera()


def buscar_ahora():
    global _ultima_busqueda
    with _lock_busqueda:
        _ultima_busqueda = time.monotonic()
    encontrada = find_camera()
    if encontrada:
        fijar_ip(encontrada)
    return encontrada, bool(encontrada)


def discover_camera_if_due():
    global _ultima_busqueda
    if camera_port_responds(ip_actual()):
        return None
    with _lock_busqueda:
        now = time.monotonic()
        if now - _ultima_busqueda < 15:
            return None
        _ultima_busqueda = now
    found = find_camera()
    if not found or found == ip_actual():
        return None
    fijar_ip(found)
    return found


def camera_watchdog(stop_event, on_found=None):
    while not stop_event.is_set():
        try:
            if not camera_port_responds(ip_actual()):
                found = discover_camera_if_due()
                if found and on_found:
                    on_found(found)
        except Exception:
            pass
        stop_event.wait(30)


def info_dispositivo():
    global _cache_info
    now = time.monotonic()
    if _cache_info[1] is not None and now - _cache_info[0] < 60:
        return dict(_cache_info[1])
    info = {"fabricante": None, "modelo": None, "firmware": None, "serie": None,
            "hora_camara": None, "hora_pc": datetime.now(timezone.utc).isoformat(),
            "desfase_segundos": None, "zona_horaria": None, "red": None,
            "perfiles": None, "usuarios": None}
    try:
        root = _llamar("/onvif/device_service", NS_DISPOSITIVO, "GetDeviceInformation")
        info.update({"fabricante": _texto(root, "Manufacturer"), "modelo": _texto(root, "Model"),
                     "firmware": _texto(root, "FirmwareVersion"), "serie": _texto(root, "SerialNumber")})
    except Exception:
        pass
    try:
        root = _llamar("/onvif/device_service", NS_DISPOSITIVO, "GetSystemDateAndTime")
        info["zona_horaria"] = _texto(root, "TZ")
        utc = _local(root, "UTCDateTime")
        if utc is not None:
            date = _local(utc, "Date")
            clock = _local(utc, "Time")
            values = [int(_texto(date, name, "0")) for name in ("Year", "Month", "Day")]
            values += [int(_texto(clock, name, "0")) for name in ("Hour", "Minute", "Second")]
            camera_time = datetime(*values, tzinfo=timezone.utc)
            info["hora_camara"] = camera_time.isoformat()
            info["desfase_segundos"] = (camera_time - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        pass
    try:
        root = _llamar("/onvif/device_service", NS_DISPOSITIVO, "GetNetworkInterfaces")
        interfaces = []
        for item in (entry for entry in root.iter() if entry.tag.rsplit("}", 1)[-1] in ("NetworkInterface", "NetworkInterfaces")):
            ipv4 = _local(item, "IPv4")
            manual = _local(ipv4, "Manual") if ipv4 is not None else None
            dhcp_item = _local(ipv4, "FromDHCP") if ipv4 is not None else None
            address = _texto(manual, "Address") or _texto(dhcp_item, "Address") or ip_actual()
            interfaces.append({"nombre": _texto(item, "Name", ""), "mac": _texto(item, "HwAddress", ""),
                               "ip": address, "dhcp": (_texto(ipv4, "DHCP", "false").lower() == "true") if ipv4 is not None else None})
        info["red"] = interfaces
    except Exception:
        pass
    try:
        root = _llamar("/onvif/Media", NS_MEDIA, "GetProfiles")
        profiles = []
        for item in (entry for entry in root.iter() if entry.tag.rsplit("}", 1)[-1] == "Profiles"):
            config = _local(item, "VideoEncoderConfiguration")
            resolution = _local(config, "Resolution") if config is not None else None
            profiles.append({"nombre": _texto(item, "Name", ""), "codec": _texto(config, "Encoding") if config is not None else None,
                             "ancho": int(_texto(resolution, "Width", "0")) if resolution is not None else None,
                             "alto": int(_texto(resolution, "Height", "0")) if resolution is not None else None})
        info["perfiles"] = profiles
    except Exception:
        pass
    try:
        root = _llamar("/onvif/device_service", NS_DISPOSITIVO, "GetUsers")
        info["usuarios"] = [{"usuario": _texto(item, "Username", ""), "nivel": _texto(item, "UserLevel", ""),
                             "clave_visible": True}
                            for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "User"]
    except Exception:
        pass
    _cache_info = (now, info)
    return dict(info)


def claves():
    root = _llamar("/onvif/device_service", NS_DISPOSITIVO, "GetUsers")
    return [{"usuario": _texto(item, "Username", ""), "clave": _texto(item, "Password", "")}
            for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "User"]


def sincronizar_hora():
    try:
        zona = info_dispositivo().get("zona_horaria") or "UTC"
        now = datetime.now(timezone.utc)
        body = ("<t:DateTimeType>Manual</t:DateTimeType><t:DaylightSavings>false</t:DaylightSavings>"
                f"<t:TimeZone><tt:TZ>{zona}</tt:TZ></t:TimeZone><t:UTCDateTime><tt:Time><tt:Hour>{now.hour}</tt:Hour>"
                f"<tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time><tt:Date>"
                f"<tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day>"
                "</tt:Date></t:UTCDateTime>")
        status, _ = onvif("/onvif/device_service", NS_DISPOSITIVO, f"<t:SetSystemDateAndTime>{body}</t:SetSystemDateAndTime>")
        if not 200 <= status < 300:
            raise ErrorCamara(f"La cámara respondió HTTP {status}")
        global _cache_info
        _cache_info = (0.0, None)
        return info_dispositivo().get("desfase_segundos")
    except Exception as exc:
        raise ErrorCamara(str(exc)) from exc


def reiniciar():
    if not soap(None):
        raise ErrorCamara("No se pudo detener el movimiento")
    try:
        root = _llamar("/onvif/device_service", NS_DISPOSITIVO, "SystemReboot")
        return _texto(root, "Message", "") or ""
    except Exception as exc:
        raise ErrorCamara(str(exc)) from exc
