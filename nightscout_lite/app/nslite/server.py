"""Options, HTTP listeners and the background threads, wired together."""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import MAX_BODY_BYTES, NightscoutApi
from .ha import CoreApi, Importer, Publisher, SourceConfig
from .store import Store

LOG = logging.getLogger("nightscout_lite")

READ_PORT = 1337
WRITE_PORT = 1338


@dataclass
class Options:
    api_secret: str
    write_secret: str = ""
    source_entity_id: str = ""
    timestamp_attribute: str = ""
    trend_attribute: str = ""
    trend_entity_id: str = ""
    import_days: int = 10
    publish_entity_id: str = "sensor.nightscout_glucose"
    display_units: str = "mg/dl"
    keep_days: int = 0


def load_options(path: str) -> Options:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)

    def text(name: str) -> str:
        return str(raw.get(name) or "").strip()

    options = Options(
        api_secret=str(raw.get("api_secret") or ""),
        write_secret=str(raw.get("write_secret") or ""),
        source_entity_id=text("source_entity_id"),
        timestamp_attribute=text("timestamp_attribute"),
        trend_attribute=text("trend_attribute"),
        trend_entity_id=text("trend_entity_id"),
        import_days=max(1, min(int(raw.get("import_days") or 10), 30)),
        publish_entity_id=text("publish_entity_id"),
        display_units="mmol" if text("display_units").lower().startswith("mmol") else "mg/dl",
        keep_days=max(0, int(raw.get("keep_days") or 0)),
    )
    validate(options)
    return options


def validate(options: Options) -> None:
    if len(options.api_secret) < 12:
        raise SystemExit("api_secret must be at least 12 characters (Nightscout's own minimum)")
    if options.write_secret:
        if len(options.write_secret) < 12:
            raise SystemExit("write_secret must be at least 12 characters, or empty to turn writes off")
        if options.write_secret == options.api_secret:
            raise SystemExit("write_secret must differ from api_secret: the read secret is the one you share")
    if options.publish_entity_id:
        if not options.publish_entity_id.startswith("sensor."):
            raise SystemExit("publish_entity_id must be a sensor.* entity id")
        if options.publish_entity_id in (options.source_entity_id, options.trend_entity_id):
            # The importer would read back what the publisher wrote, forever.
            raise SystemExit("publish_entity_id must not be the source sensor itself")


def make_handler(api: NightscoutApi, writable: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NightscoutLite"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def _respond(self, method: str):
            headers = {key.lower(): value for key, value in self.headers.items()}
            length = int(headers.get("content-length") or 0)
            if length > MAX_BODY_BYTES:
                self._send(413, {"status": 413, "message": "request body too large"}, method)
                self.close_connection = True
                return
            body = self.rfile.read(length) if length > 0 else b""
            try:
                response = api.handle(method, self.path, headers, body, writable)
                code, payload = response.code, response.body
            except Exception as error:  # noqa: BLE001 - one bad request must not end the server
                LOG.exception("Request %s %s failed: %s", method, urllib.parse.urlsplit(self.path).path, error)
                code, payload = 500, {"status": 500, "message": "internal error"}
            self._send(code, payload, method)

        def _send(self, code: int, payload, method: str):
            if isinstance(payload, str):
                data = payload.encode("utf-8")
                content_type = "text/plain; charset=utf-8"
            else:
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "api-secret, authorization, content-type")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(data)

        def do_GET(self):  # noqa: N802 - http.server naming
            self._respond("GET")

        def do_HEAD(self):  # noqa: N802
            self._respond("HEAD")

        def do_POST(self):  # noqa: N802
            self._respond("POST")

        def do_PUT(self):  # noqa: N802
            self._respond("PUT")

        def do_DELETE(self):  # noqa: N802
            self._respond("DELETE")

        def do_PATCH(self):  # noqa: N802
            self._respond("PATCH")

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            methods = "GET, HEAD, POST, PUT, DELETE, OPTIONS" if writable else "GET, HEAD, OPTIONS"
            self.send_header("Access-Control-Allow-Methods", methods)
            self.send_header("Access-Control-Allow-Headers", "api-secret, authorization, content-type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt, *args):
            # Paths only: query strings can carry the token.
            LOG.debug("%s %s", self.command, urllib.parse.urlsplit(self.path).path)

    return Handler


class App:
    def __init__(self, options: Options, store: Store, core: CoreApi | None):
        self.options = options
        self.store = store
        self.stop = threading.Event()
        self.publisher = (
            Publisher(core, store, options.publish_entity_id, options.display_units)
            if core is not None and options.publish_entity_id
            else None
        )
        notify = self.publisher.notify if self.publisher else (lambda: None)
        self.importer = (
            Importer(
                core,
                store,
                SourceConfig(
                    entity_id=options.source_entity_id,
                    timestamp_attribute=options.timestamp_attribute,
                    trend_attribute=options.trend_attribute,
                    trend_entity_id=options.trend_entity_id,
                ),
                options.import_days,
                on_new=notify,
            )
            if core is not None and options.source_entity_id
            else None
        )
        self.api = NightscoutApi(
            store,
            read_secret=options.api_secret,
            write_secret=options.write_secret,
            display_units=options.display_units,
            on_write=notify,
        )
        self.servers: list[ThreadingHTTPServer] = []

    def start(self, host: str = "0.0.0.0", read_port: int = READ_PORT, write_port: int = WRITE_PORT) -> None:
        self.servers.append(ThreadingHTTPServer((host, read_port), make_handler(self.api, writable=False)))
        if self.options.write_secret:
            self.servers.append(ThreadingHTTPServer((host, write_port), make_handler(self.api, writable=True)))
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        if self.importer:
            threading.Thread(target=self.importer.loop, args=(self.stop,), daemon=True).start()
        if self.publisher:
            threading.Thread(target=self.publisher.loop, args=(self.stop,), daemon=True).start()
        if self.options.keep_days:
            threading.Thread(target=self._prune_loop, daemon=True).start()

    def _prune_loop(self) -> None:
        while not self.stop.wait(6 * 3600):
            removed = self.store.prune_entries(self.options.keep_days)
            if removed:
                LOG.info("Removed %d entries older than %d days", removed, self.options.keep_days)

    def shutdown(self) -> None:
        self.stop.set()
        if self.publisher:
            self.publisher.notify()
        for server in self.servers:
            server.shutdown()
            server.server_close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    options = load_options(os.environ.get("OPTIONS_PATH", "/data/options.json"))
    store = Store(os.environ.get("DB_PATH", "/data/nightscout.sqlite3"))
    core = CoreApi() if os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_API_URL") else None
    app = App(options, store, core)
    app.start()
    LOG.info(
        "Nightscout API: read on %d%s; source %s; publishing to %s",
        READ_PORT,
        f", read/write on {WRITE_PORT}" if options.write_secret else " (writes off: no write_secret)",
        (options.source_entity_id or "none") if core else "none (no Home Assistant API)",
        (options.publish_entity_id or "nothing") if core else "nothing (no Home Assistant API)",
    )
    app.stop.wait()
