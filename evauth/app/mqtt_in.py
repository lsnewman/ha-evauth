"""MQTT: subscribes to the Frigate events topic, and publishes only the app's heartbeat."""
from __future__ import annotations

import logging
import os
import threading

import requests

log = logging.getLogger("evauth.mqtt")
SUPERVISOR_MQTT = "http://supervisor/services/mqtt"


def discover(override: dict) -> dict | None:
    """Broker settings: the panel's override if a host is set, else Supervisor service discovery."""
    if override.get("host"):
        return {"host": override["host"], "port": int(override.get("port") or 1883),
                "username": override.get("username") or None, "password": override.get("password") or None}
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        r = requests.get(SUPERVISOR_MQTT, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        d = r.json().get("data") or {}
        return {"host": d["host"], "port": int(d.get("port", 1883)),
                "username": d.get("username"), "password": d.get("password")}
    except Exception as e:      # noqa: BLE001
        log.warning("MQTT discovery failed: %s", e)
        return None


class MqttIn:
    def __init__(self, get_cfg, on_payload, on_stats=None):
        self.get_cfg = get_cfg                    # () -> settings doc
        self.on_payload = on_payload              # (bytes) -> None
        self.on_stats = on_stats                  # (bytes) -> None, Frigate's stats topic (camera names)
        self.status = "starting"
        self.generation = 0                       # counts successful connections
        self._client = None
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        c = self._client
        return bool(c and c.is_connected())

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        c = self._client
        if not c or not c.is_connected():
            return False
        return c.publish(topic, payload, retain=retain).rc == 0

    def start(self):
        threading.Thread(target=self.restart, name="mqtt-start", daemon=True).start()

    def stop(self):
        with self._lock:
            self._stop_client()

    def _stop_client(self):
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:       # noqa: BLE001
                pass
            self._client = None

    def restart(self):
        import paho.mqtt.client as mqtt
        with self._lock:
            self._stop_client()
            cfg = self.get_cfg()
            b = discover(cfg["mqtt"])
            if not b:
                self.status = "no MQTT broker found (install the Mosquitto app, or set one in Settings)"
                return
            topic = cfg["frigate"]["topic"]
            stats_topic = (topic[:-len("/events")] if topic.endswith("/events") else "frigate") + "/stats"
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if b["username"]:
                c.username_pw_set(b["username"], b["password"])

            def on_connect(client, userdata, flags, rc, props=None):
                if rc == 0:
                    client.subscribe(topic)
                    if self.on_stats:
                        client.subscribe(stats_topic)
                    self.generation += 1
                    self.status = f"connected, listening on {topic}"
                else:
                    self.status = f"broker refused the connection ({rc})"

            def on_disconnect(client, userdata, flags, rc, props=None):
                self.status = "disconnected, retrying"

            def on_message(client, userdata, msg):
                try:
                    if msg.topic == stats_topic and self.on_stats:
                        return self.on_stats(msg.payload)
                    self.on_payload(msg.payload)
                except Exception:       # noqa: BLE001 - one bad payload must not kill the loop
                    log.exception("bad frigate payload")

            c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, on_message
            try:
                c.connect_async(b["host"], b["port"])
                c.loop_start()
                self._client = c
                self.status = "connecting"
            except Exception as e:      # noqa: BLE001
                self.status = f"cannot connect: {e}"
