"""Entry point: python -m app.main"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from .ha_client import HAClient
from .mqtt_in import MqttIn
from .service import App
from .web import Api, make_server


def main():
    dev = bool(os.environ.get("EVAUTH_DEV"))
    data_dir = os.environ.get("EVAUTH_DATA", "./.devdata" if dev else "/data")
    app = App(data_dir, dev=dev)
    logging.basicConfig(level=getattr(logging, app.cfg["log_level"].upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("evauth")
    log.info("EVAuth %s starting (%s mode; charging control: %s)",
             os.environ.get("EVAUTH_VERSION", "dev"), "dev" if dev else "app", app.cfg["control"]["mode"])
    if dev:
        app.start()
    else:
        ha = HAClient(app.on_state, on_event=app.on_ha_event, on_connect=app.on_ha_connect,
                      on_disconnect=app.on_ha_disconnect)
        app.start(ha=ha, mqtt=MqttIn(lambda: app.cfg, app.on_frigate, app.on_stats))
    port = int(os.environ.get("EVAUTH_PORT", "8099"))
    srv = make_server(Api(app), port, allow_all=dev)
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("panel on port %s", port)
    stop.wait()
    app.stop()
    srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
