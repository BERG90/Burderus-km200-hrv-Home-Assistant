#!/usr/bin/env python3
"""KM200 HRV -> MQTT Bridge fuer Buderus Logavent HRV2 (Logamatic web KM200 HRV).

Liest alle Datenpunkte unterhalb der konfigurierten Wurzeln (Standard: /ventilation),
legt sie per MQTT-Discovery in Home Assistant an und erlaubt das Schreiben
beschreibbarer Werte (z. B. Lueftungsstufe / Betriebsart).
"""
import base64
import hashlib
import json
import logging
import os
import queue
import re
import sys
import time

import paho.mqtt.client as mqtt
import requests
from Crypto.Cipher import AES

# Bekanntes Salt der Buderus-KM-Gateways (siehe FHEM 73_km200 / ioBroker.km200)
SALT = bytes.fromhex("867845e97c4e29dce522b9a7d3a3e07b152bffadddbed7f5ffd842e9895ad1e4")
BASE = "km200hrv"
DISCOVERY = "homeassistant"
UNITS = {"C": "°C", "K": "K", "m3/h": "m³/h", "%": "%", "Pa": "Pa", "ppm": "ppm",
         "rpm": "rpm", "W": "W", "kWh": "kWh", "mins": "min", "h": "h", "d": "d"}

log = logging.getLogger("km200hrv")


class KM200:
    def __init__(self, host, gateway_password="", private_password="", aes_key_hex="", timeout=15):
        self.base = f"http://{host}"
        if aes_key_hex:
            self.key = bytes.fromhex(aes_key_hex.strip())
        else:
            gw = gateway_password.replace("-", "").replace(" ", "").encode()
            self.key = (hashlib.md5(gw + SALT).digest()
                        + hashlib.md5(SALT + private_password.encode()).digest())
        if len(self.key) != 32:
            raise ValueError("AES-Schluessel muss 32 Byte (64 Hex-Zeichen) lang sein")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "TeleHeater/2.2.3", "Accept": "application/json"})
        self.timeout = timeout

    def _decrypt(self, body: bytes):
        raw = base64.b64decode(body.strip())
        raw = raw[: len(raw) - len(raw) % 16]
        pt = AES.new(self.key, AES.MODE_ECB).decrypt(raw).rstrip(b"\x00")
        if pt and pt[-1] <= 16 and pt.endswith(bytes([pt[-1]]) * pt[-1]):
            pt = pt[: -pt[-1]]
        return json.loads(pt.decode("utf-8"))

    def _encrypt(self, data: bytes) -> bytes:
        pad = 16 - len(data) % 16
        data += bytes([pad]) * pad
        return base64.b64encode(AES.new(self.key, AES.MODE_ECB).encrypt(data))

    def get(self, path):
        try:
            r = self.s.get(self.base + path, timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("GET %s fehlgeschlagen: %s", path, e)
            return None
        if r.status_code != 200 or not r.content:
            log.debug("GET %s -> HTTP %s", path, r.status_code)
            return None
        try:
            return self._decrypt(r.content)
        except Exception:  # noqa: BLE001
            raise PermissionError(
                "Antwort nicht entschluesselbar - Geraete-/Privatpasswort (oder AES-Key) pruefen")

    def set(self, path, value) -> bool:
        body = self._encrypt(json.dumps({"value": value}).encode())
        try:
            r = self.s.post(self.base + path, data=body, timeout=self.timeout,
                            headers={"Content-Type": "application/json"})
        except requests.RequestException as e:
            log.error("POST %s fehlgeschlagen: %s", path, e)
            return False
        ok = r.status_code in (200, 204)
        (log.info if ok else log.error)("SET %s = %r -> HTTP %s", path, value, r.status_code)
        return ok

    def walk(self, path, out, depth=0):
        if depth > 8:
            return
        d = self.get(path)
        time.sleep(0.2)
        if not d:
            return
        if d.get("type") == "refEnum":
            for ref in d.get("references", []):
                self.walk(ref.get("id", ""), out, depth + 1)
        else:
            out[path] = d


def objid(path):
    return re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_").lower()


def nice_name(path, roots):
    p = path
    for r in roots:
        if p.startswith(r + "/"):
            p = p[len(r) + 1:]
            break
    return " ".join(p.strip("/").split("/"))


def entity_config(path, d, roots):
    oid = objid(path)
    typ = d.get("type")
    writeable = bool(d.get("writeable"))
    cfg = {
        "name": nice_name(path, roots),
        "unique_id": f"{BASE}_{oid}",
        "state_topic": f"{BASE}/state/{oid}",
        "availability_topic": f"{BASE}/status",
        "device": {"identifiers": [BASE], "name": "Logavent HRV2",
                   "manufacturer": "Buderus", "model": "Logavent HRV2 (KM200 HRV)"},
    }
    if typ == "stringValue":
        if writeable and d.get("allowedValues"):
            cfg.update(command_topic=f"{BASE}/set/{oid}", options=[str(v) for v in d["allowedValues"]])
            return "select", oid, cfg
        return "sensor", oid, cfg
    if typ == "floatValue":
        unit = UNITS.get(d.get("unitOfMeasure", ""), d.get("unitOfMeasure") or None)
        if unit:
            cfg["unit_of_measurement"] = unit
        if unit == "°C":
            cfg["device_class"] = "temperature"
        if writeable:
            cfg.update(command_topic=f"{BASE}/set/{oid}", mode="box",
                       min=d.get("minValue", -1e6), max=d.get("maxValue", 1e6),
                       step=d.get("stepSize", 1))
            return "number", oid, cfg
        cfg["state_class"] = "measurement"
        return "sensor", oid, cfg
    return None, oid, None


def supervisor_mqtt():
    tok = os.environ.get("SUPERVISOR_TOKEN")
    if not tok:
        return {}
    try:
        r = requests.get("http://supervisor/services/mqtt",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        return r.json().get("data", {}) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("MQTT-Daten vom Supervisor nicht abrufbar: %s", e)
        return {}


def make_client():
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=BASE)
    except AttributeError:  # paho-mqtt < 2
        return mqtt.Client(client_id=BASE)


def main():
    opts_file = os.environ.get("KM200_OPTIONS", "/data/options.json")
    with open(opts_file, encoding="utf-8") as f:
        o = json.load(f)
    logging.basicConfig(level=getattr(logging, o.get("log_level", "info").upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    roots = [r.rstrip("/") for r in (o.get("roots") or ["/ventilation"])]
    interval = int(o.get("scan_interval", 60))

    if not o.get("host") or not (o.get("aes_key") or (o.get("gateway_password") and o.get("private_password"))):
        log.error("Bitte in der Konfiguration host, gateway_password und private_password eintragen")
        sys.exit(1)

    km = KM200(o["host"], o.get("gateway_password", ""), o.get("private_password", ""),
               o.get("aes_key", ""))

    # ---- Datenpunkte suchen ----
    leaves = {}
    try:
        top = km.get("/")
        if top:
            log.info("Wurzel-Endpunkte: %s", [r.get("id") for r in top.get("references", [])])
        for r in roots:
            km.walk(r, leaves)
    except PermissionError as e:
        log.error(str(e))
        sys.exit(1)
    if not leaves:
        log.error("Keine Datenpunkte gefunden - IP und Wurzeln %s pruefen", roots)
        sys.exit(1)

    log.info("===== %d Datenpunkte gefunden =====", len(leaves))
    for p, d in sorted(leaves.items()):
        log.info("%-55s %-12s value=%r writeable=%s %s", p, d.get("type"), d.get("value"),
                 d.get("writeable"), d.get("allowedValues", ""))

    entities, by_oid = {}, {}
    for p, d in leaves.items():
        comp, oid, cfg = entity_config(p, d, roots)
        if comp:
            entities[p] = (comp, oid, cfg)
            by_oid[oid] = p

    # ---- MQTT ----
    m = supervisor_mqtt()
    host = o.get("mqtt_host") or m.get("host", "core-mosquitto")
    port = int(o.get("mqtt_port") or m.get("port", 1883))
    user = o.get("mqtt_user") or m.get("username")
    pw = o.get("mqtt_password") or m.get("password")

    cmds = queue.Queue()
    c = make_client()
    if user:
        c.username_pw_set(user, pw)
    c.will_set(f"{BASE}/status", "offline", retain=True)

    def publish_discovery():
        for comp, oid, cfg in entities.values():
            c.publish(f"{DISCOVERY}/{comp}/{BASE}/{oid}/config", json.dumps(cfg), retain=True)
        c.publish(f"{BASE}/status", "online", retain=True)

    def on_connect(client, *args):
        log.info("MQTT verbunden (%s:%s)", host, port)
        client.subscribe(f"{BASE}/set/#")
        client.subscribe(f"{DISCOVERY}/status")
        publish_discovery()

    def on_message(client, userdata, msg):
        if msg.topic == f"{DISCOVERY}/status":
            if msg.payload.decode() == "online":
                publish_discovery()
            return
        cmds.put((msg.topic.rsplit("/", 1)[-1], msg.payload.decode()))

    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(host, port, 60)
    c.loop_start()

    def publish_value(path, d):
        v = d.get("value")
        if v is None:
            return
        c.publish(f"{BASE}/state/{objid(path)}", str(v), retain=True)

    def poll(path):
        try:
            d = km.get(path)
        except PermissionError as e:
            log.error(str(e))
            return
        if d:
            publish_value(path, d)

    for p, d in leaves.items():
        if p in entities:
            publish_value(p, d)

    next_poll = time.time() + interval
    while True:
        try:
            oid, payload = cmds.get(timeout=max(0.5, next_poll - time.time()))
            path = by_oid.get(oid)
            if path:
                comp = entities[path][0]
                val = payload
                if comp == "number":
                    f = float(payload)
                    val = int(f) if f.is_integer() else f
                if km.set(path, val):
                    time.sleep(2)
                    poll(path)
        except queue.Empty:
            pass
        if time.time() >= next_poll:
            for p in entities:
                poll(p)
                time.sleep(0.2)
            next_poll = time.time() + interval


if __name__ == "__main__":
    main()
