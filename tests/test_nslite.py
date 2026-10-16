import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nightscout_lite", "app"))

from nslite import util  # noqa: E402
from nslite.ha import CoreApi, Importer, Publisher, SourceConfig, entity_payload, states_to_entries  # noqa: E402
from nslite.server import App, Options, validate  # noqa: E402
from nslite.store import Store, ValidationError  # noqa: E402

READ_SECRET = "read-secret-123456"
WRITE_SECRET = "write-secret-654321"
NOW_MS = 1_792_138_860_000  # 2026-10-16T08:21:00Z
MIN = 60_000


def sha(text):
    return hashlib.sha1(text.encode()).hexdigest()


def ha_state(value, when, unit="mg/dL", **attributes):
    return {
        "entity_id": "sensor.libreview_glucose",
        "state": str(value),
        "attributes": {"unit_of_measurement": unit, **attributes},
        "last_changed": when,
        "last_updated": when,
    }


OPEN_STORES = []


def temp_store(clock=lambda: NOW_MS):
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    handle.close()
    store = Store(handle.name, clock=clock)
    OPEN_STORES.append(store)
    return store


def tearDownModule():
    for store in OPEN_STORES:
        store.close()


# --------------------------------------------------------------------------- pure helpers


class UtilTests(unittest.TestCase):
    def test_times(self):
        self.assertEqual(util.parse_time_ms("2026-10-16T08:21:00Z"), NOW_MS)
        self.assertEqual(util.parse_time_ms("2026-10-16T10:21:00+02:00"), NOW_MS)
        self.assertEqual(util.parse_time_ms("2026-10-16T08:21:00"), NOW_MS)
        self.assertEqual(util.parse_time_ms("10/16/2026 8:21:00 AM"), NOW_MS)
        self.assertEqual(util.parse_time_ms(NOW_MS // 1000), NOW_MS)
        self.assertEqual(util.parse_time_ms(str(NOW_MS)), NOW_MS)
        self.assertIsNone(util.parse_time_ms("unavailable"))
        self.assertEqual(util.iso_utc(NOW_MS + 5), "2026-10-16T08:21:00.005Z")

    def test_units_and_directions(self):
        self.assertAlmostEqual(util.to_mgdl("6.7", "mmol/L"), 120.72, places=1)
        self.assertIsNone(util.to_mgdl("unknown", "mg/dL"))
        self.assertEqual(util.map_direction(5), "SingleUp")
        self.assertEqual(util.map_direction("FortyFiveDown"), "FortyFiveDown")
        self.assertEqual(util.map_direction("Falling quickly"), "SingleDown")
        self.assertIsNone(util.map_direction("unknown"))

    def test_secrets(self):
        self.assertTrue(util.secret_matches(sha(READ_SECRET), READ_SECRET))
        self.assertTrue(util.secret_matches(sha(READ_SECRET).upper(), READ_SECRET))
        self.assertTrue(util.secret_matches(READ_SECRET, READ_SECRET))
        self.assertFalse(util.secret_matches("", READ_SECRET))
        self.assertFalse(util.secret_matches(READ_SECRET, ""))
        found = util.presented_credentials({"authorization": "Bearer abc"}, {"token": ["t"], "secret": [""]})
        self.assertEqual(found, ["abc", "t"])

    def test_query(self):
        params = urllib.parse.parse_qs(
            "count=5&find%5Bdate%5D%5B%24gte%5D=1792138000000&find[created_at][$lt]=2026-10-16T08:21:00Z&find[eventType]=Meal+Bolus"
        )
        query = util.parse_query(params, 10)
        self.assertEqual((query.count, query.gte, query.lt), (5, 1_792_138_000_000, NOW_MS))
        self.assertEqual(query.equals, {"eventType": "Meal Bolus"})

    def test_options_guard_against_mistakes(self):
        with self.assertRaises(SystemExit):
            validate(Options(api_secret="short"))
        with self.assertRaises(SystemExit):
            validate(Options(api_secret=READ_SECRET, write_secret=READ_SECRET))
        with self.assertRaises(SystemExit):
            validate(Options(api_secret=READ_SECRET, source_entity_id="sensor.x", publish_entity_id="sensor.x"))
        validate(Options(api_secret=READ_SECRET, write_secret=WRITE_SECRET))


# --------------------------------------------------------------------------- store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = temp_store()

    def test_entries_dedupe_and_validate(self):
        first = self.store.insert_entries([{"type": "sgv", "sgv": 120, "date": NOW_MS}], "upload")
        again = self.store.insert_entries([{"type": "sgv", "sgv": 121, "date": NOW_MS}], "upload")
        near = self.store.insert_entries([{"type": "sgv", "sgv": 122, "date": NOW_MS + 10_000}], "home-assistant")
        later = self.store.insert_entries([{"type": "sgv", "sgv": 125, "date": NOW_MS + MIN}], "upload")
        self.assertEqual((first.inserted, again.duplicates, near.duplicates, later.inserted), (1, 1, 1, 1))
        self.assertEqual(again.docs[0]["_id"], first.docs[0]["_id"])
        self.assertEqual([e["sgv"] for e in self.store.query_entries(["sgv"], 10)], [125, 120])
        for bad in (
            {"type": "sgv", "sgv": 5000, "date": NOW_MS},
            {"type": "sgv", "date": NOW_MS},
            {"type": "sgv", "sgv": 100},
            {"type": "sgv", "sgv": 100, "date": NOW_MS + 60 * MIN},
            {"type": "weird", "date": NOW_MS},
            "not an object",
        ):
            with self.assertRaises(ValidationError, msg=bad):
                self.store.insert_entries([bad], "upload")
        # A refused document refuses the whole request: nothing half-written.
        with self.assertRaises(ValidationError):
            self.store.insert_entries(
                [{"type": "sgv", "sgv": 130, "date": NOW_MS + 2 * MIN}, {"type": "sgv", "sgv": 0, "date": NOW_MS}],
                "upload",
            )
        self.assertEqual(len(self.store.query_entries(["sgv"], 10)), 2)

    def test_treatment_post_is_idempotent_and_put_replaces(self):
        doc = {"eventType": "Meal Bolus", "insulin": 3, "carbs": 40, "created_at": "2026-10-16T08:00:00Z",
               "identifier": "JNG-1"}
        first = self.store.write_treatments([doc], upsert=False)
        repeat = self.store.write_treatments([doc], upsert=False)
        self.assertEqual((first.inserted, repeat.duplicates), (1, 1))
        self.assertEqual(repeat.docs[0]["_id"], first.docs[0]["_id"])
        edited = dict(doc, insulin=4)
        put = self.store.write_treatments([edited], upsert=True)
        self.assertEqual(put.docs[0]["_id"], first.docs[0]["_id"])
        stored = self.store.query_treatments(10)
        self.assertEqual((len(stored), stored[0]["insulin"]), (1, 4))
        self.assertEqual(stored[0]["created_at"], "2026-10-16T08:00:00.000Z")
        self.assertEqual(self.store.delete_treatment(first.docs[0]["_id"]), 1)
        self.assertEqual(self.store.query_treatments(10), [])

    def test_treatment_limits(self):
        for bad in ({"eventType": "Bolus", "insulin": 250}, {"eventType": "Carbs", "carbs": -5},
                    {"eventType": "Note", "created_at": "2026-10-20T08:00:00Z"}):
            with self.assertRaises(ValidationError, msg=bad):
                self.store.write_treatments([bad], upsert=False)

    def test_devicestatus_is_pruned(self):
        self.store.insert_devicestatus([{"device": "old", "created_at": "2026-08-01T00:00:00Z"}])
        self.store.insert_devicestatus([{"device": "new", "uploaderBattery": 80}])
        self.assertEqual([d["device"] for d in self.store.query_devicestatus(10)], ["new"])


# --------------------------------------------------------------------------- Home Assistant side


class FakeCore:
    """Answers history like Home Assistant: only the states inside the requested window."""

    def __init__(self, history=None):
        self.history_payload = history or {}
        self.history_calls = []
        self.states = []

    def history(self, start_ms, entity_ids, end_ms):
        self.history_calls.append((start_ms, entity_ids, end_ms))
        return {
            entity: [s for s in states if start_ms <= util.parse_time_ms(s["last_updated"]) < end_ms]
            for entity, states in self.history_payload.items()
        }

    def set_state(self, entity_id, state, attributes):
        self.states.append((entity_id, state, attributes))


class HomeAssistantTests(unittest.TestCase):
    def test_states_to_entries(self):
        config = SourceConfig("sensor.libreview_glucose")
        states = [
            ha_state(110, "2026-10-16T08:10:05Z", timestamp="2026-10-16T08:10:00Z", trend=3),
            ha_state(110, "2026-10-16T08:11:05Z", timestamp="2026-10-16T08:10:00Z", trend=3),
            ha_state(6.6, "2026-10-16T08:21:04Z", unit="mmol/L", timestamp="2026-10-16T08:21:00Z", trend="Rising"),
            ha_state("unavailable", "2026-10-16T08:22:00Z"),
        ]
        docs = states_to_entries(states, config)
        self.assertEqual([d["sgv"] for d in docs], [110, 119])
        self.assertEqual(docs[-1]["date"], NOW_MS)
        self.assertEqual(docs[-1]["direction"], "FortyFiveUp")

    def test_libreview_integration_state(self):
        # The exact attributes of the LibreView integration this add-on is used with.
        state = ha_state(6.8, "2026-10-16T11:23:01+00:00", unit="mmol/L", state_class="measurement",
                         value_mmol_l=6.8, value_mg_dl=122, trend="decreasing", app_unit_of_measurement="mmol/L",
                         measurement_timestamp="2026-10-16T11:22:10+00:00")
        doc = states_to_entries([state], SourceConfig("sensor.tomas_krnac_glucose_level"))[0]
        self.assertEqual((doc["sgv"], doc["direction"]), (122, "FortyFiveDown"))
        self.assertEqual(doc["dateString"], "2026-10-16T11:22:10.000Z")
        self.assertEqual(util.map_direction("decreasing_fast"), "SingleDown")
        self.assertEqual(util.map_direction("increasing_fast"), "SingleUp")
        stale = ha_state(9.0, "2026-10-16T11:23:01+00:00", unit="mmol/L", value_mg_dl=122)
        self.assertEqual(states_to_entries([stale], SourceConfig("x"))[0]["sgv"], 162)

    def test_importer_moves_cursor_and_notifies(self):
        store = temp_store()
        core = FakeCore({"sensor.libreview_glucose": [
            ha_state(100 + i, f"2026-10-16T08:{i:02d}:00Z") for i in range(22)
        ]})
        notified = []
        importer = Importer(core, store, SourceConfig("sensor.libreview_glucose"), 10,
                            on_new=lambda: notified.append(1), clock=lambda: NOW_MS)
        self.assertEqual(importer.run_once(), 21)  # 08:00..08:20; 08:21 is "now", not yet recorded
        first_import = list(core.history_calls)
        # Ten days read in half-day windows that join up exactly and end at now.
        self.assertEqual(len(first_import), 20)
        self.assertEqual(first_import[0][0], NOW_MS - 10 * 24 * 60 * MIN)
        self.assertEqual(first_import[-1][2], NOW_MS)
        self.assertTrue(all(a[2] == b[0] for a, b in zip(first_import, first_import[1:])))
        self.assertEqual(importer.run_once(), 0)
        self.assertEqual(core.history_calls[-1][0], NOW_MS - 5 * MIN)
        self.assertEqual(len(notified), 1)

    def test_stale_measurement_does_not_freeze_the_import(self):
        # A sensor stuck on a reading measured a month ago, then fresh readings today.
        store = temp_store()
        clock = [NOW_MS - 30 * MIN]
        stale = [ha_state(123, f"2026-10-16T07:{m:02d}:00Z", measurement_timestamp="2026-09-14T18:03:20Z")
                 for m in range(40, 50)]
        fresh = [ha_state(130 + m, f"2026-10-16T07:{m:02d}:00Z", measurement_timestamp=f"2026-10-16T07:{m:02d}:00Z")
                 for m in range(51, 60)]
        core = FakeCore({"sensor.libreview_glucose": stale})
        importer = Importer(core, store, SourceConfig("sensor.libreview_glucose"), 10, clock=lambda: clock[0])
        self.assertEqual(importer.run_once(), 1)
        core.history_payload = {"sensor.libreview_glucose": stale + fresh}
        clock[0] = NOW_MS
        self.assertEqual(importer.run_once(), 9)
        self.assertEqual(store.latest_entry()["dateString"], "2026-10-16T07:59:00.000Z")

    def test_publisher_only_sends_news(self):
        store = temp_store()
        core = FakeCore()
        publisher = Publisher(core, store, "sensor.nightscout_glucose", "mmol", clock=lambda: NOW_MS)
        self.assertFalse(publisher.publish_once())
        store.insert_entries([{"type": "sgv", "sgv": 108, "date": NOW_MS - MIN},
                              {"type": "sgv", "sgv": 117, "date": NOW_MS, "direction": "FortyFiveUp"}], "upload")
        self.assertTrue(publisher.publish_once())
        self.assertFalse(publisher.publish_once())
        self.assertTrue(publisher.publish_once(heartbeat=True))
        entity, state, attributes = core.states[0]
        self.assertEqual((entity, state), ("sensor.nightscout_glucose", "6.5"))
        self.assertEqual(attributes["unit_of_measurement"], "mmol/L")
        self.assertEqual(attributes["trend_arrow"], "↗")
        self.assertEqual(attributes["delta"], 0.5)
        self.assertEqual(attributes["measured_at"], "2026-10-16T08:21:00.000Z")
        # Backfill of old readings must not appear in Home Assistant as if it were new.
        old_store = temp_store()
        old_store.insert_entries([{"type": "sgv", "sgv": 100, "date": NOW_MS - 60 * MIN}], "upload")
        self.assertFalse(Publisher(core, old_store, "sensor.x", "mg/dl", clock=lambda: NOW_MS).publish_once())

    def test_payload_mgdl(self):
        state, attributes = entity_payload({"sgv": 120, "date": NOW_MS}, "mg/dl", {"sgv": 115, "date": NOW_MS - 5 * MIN})
        self.assertEqual((state, attributes["delta"], attributes["unit_of_measurement"]), ("120", 5, "mg/dL"))


class SupervisorEnvironmentTests(unittest.TestCase):
    def test_token_from_s6_files_when_environment_is_cleaned(self):
        from nslite import ha

        directory = tempfile.mkdtemp()
        with open(os.path.join(directory, "SUPERVISOR_TOKEN"), "w") as handle:
            handle.write("from-s6\n")
        saved_dirs, saved_env = ha.S6_ENV_DIRS, os.environ.pop("SUPERVISOR_TOKEN", None)
        try:
            ha.S6_ENV_DIRS = ("/nonexistent", directory)
            self.assertEqual(ha.container_env("SUPERVISOR_TOKEN"), "from-s6")
            self.assertEqual(ha.CoreApi().token, "from-s6")
            os.environ["SUPERVISOR_TOKEN"] = "from-env"
            self.assertEqual(ha.container_env("SUPERVISOR_TOKEN"), "from-env")
            self.assertEqual(ha.container_env("NOT_SET_ANYWHERE"), "")
        finally:
            ha.S6_ENV_DIRS = saved_dirs
            os.environ.pop("SUPERVISOR_TOKEN", None)
            if saved_env is not None:
                os.environ["SUPERVISOR_TOKEN"] = saved_env

    def test_disconnect_is_not_logged_as_error(self):
        import socket
        from nslite.server import QuietServer, make_handler, NightscoutApi

        store = temp_store()
        server = QuietServer(("127.0.0.1", 0), make_handler(NightscoutApi(store, READ_SECRET, ""), writable=False))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertNoLogs("nightscout_lite", level="ERROR"):
                # Connect and hang up hard, like the Supervisor's port check.
                sock = socket.create_connection(("127.0.0.1", server.server_port))
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                sock.close()
                time.sleep(0.3)
        finally:
            server.shutdown()
            server.server_close()


# --------------------------------------------------------------------------- websocket push


import base64 as _b64
import socket as _socket
import struct as _struct

from nslite.ha import Watcher  # noqa: E402
from nslite.ws import WebSocket, WebSocketClosed  # noqa: E402


class FakeWsPeer:
    """The server side of one websocket connection, frames written by hand."""

    def __init__(self, conn):
        self.conn = conn
        self.buffer = b""

    def _read(self, count):
        while len(self.buffer) < count:
            chunk = self.conn.recv(4096)
            if not chunk:
                raise ConnectionError("client gone")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def handshake(self):
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self.conn.recv(4096)
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        key = [line.split(":", 1)[1].strip() for line in head.decode().split("\r\n")
               if line.lower().startswith("sec-websocket-key")][0]
        accept = _b64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        return head.decode().split("\r\n")[0]

    def send_frame(self, opcode, payload, fin=True):
        length = len(payload)
        if length < 126:
            header = bytes([(0x80 if fin else 0) | opcode, length])
        elif length < 65536:
            header = bytes([(0x80 if fin else 0) | opcode, 126]) + _struct.pack("!H", length)
        else:
            header = bytes([(0x80 if fin else 0) | opcode, 127]) + _struct.pack("!Q", length)
        self.conn.sendall(header + payload)

    def send_json(self, message):
        self.send_frame(0x1, json.dumps(message).encode())

    def recv_frame(self):
        first, second = self._read(2)
        assert second & 0x80, "client frames must be masked"
        length = second & 0x7F
        if length == 126:
            length = _struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = _struct.unpack("!Q", self._read(8))[0]
        mask = self._read(4)
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(length)))
        return first & 0x0F, payload

    def recv_json(self):
        while True:
            opcode, payload = self.recv_frame()
            if opcode == 0x1:
                return json.loads(payload)


class FakeWsServer:
    def __init__(self, session):
        self.session = session
        self.sock = _socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.sessions = 0
        self.paths = []
        self.done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self.done.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.sessions += 1
            peer = FakeWsPeer(conn)
            try:
                self.paths.append(peer.handshake())
                self.session(peer, self.sessions)
            except Exception:  # noqa: BLE001 - the client hanging up ends a session
                pass
            finally:
                conn.close()

    def close(self):
        self.done.set()
        self.sock.close()


class WebSocketTests(unittest.TestCase):
    def test_frames_of_all_sizes_fragments_and_pings(self):
        big = "x" * 70_000
        def session(peer, _):
            peer.send_frame(0x9, b"hi")  # ping first: the client must answer pong
            opcode, payload = peer.recv_frame()
            assert (opcode, payload) == (0xA, b"hi")
            peer.send_json({"small": 1})
            peer.send_frame(0x1, json.dumps({"mid": "y" * 300}).encode())
            peer.send_frame(0x1, json.dumps({"big": big}).encode())
            text = json.dumps({"parts": "abc"}).encode()
            peer.send_frame(0x1, text[:5], fin=False)
            peer.send_frame(0x0, text[5:])
            echoed = peer.recv_json()
            peer.send_json({"echo": echoed})
            peer.send_frame(0x8, _struct.pack("!H", 1000))
            time.sleep(0.2)
        server = FakeWsServer(session)
        try:
            ws = WebSocket.connect(f"ws://127.0.0.1:{server.port}/core/websocket")
            self.assertEqual(json.loads(ws.recv_text()), {"small": 1})
            self.assertEqual(len(json.loads(ws.recv_text())["mid"]), 300)
            self.assertEqual(len(json.loads(ws.recv_text())["big"]), 70_000)
            self.assertEqual(json.loads(ws.recv_text()), {"parts": "abc"})
            ws.send_text(json.dumps({"long": "z" * 70_000}))
            self.assertEqual(len(json.loads(ws.recv_text())["echo"]["long"]), 70_000)
            with self.assertRaises(WebSocketClosed):
                ws.recv_text()
            ws.close()
            self.assertEqual(server.paths[0], "GET /core/websocket HTTP/1.1")
        finally:
            server.close()

    def test_idle_timeout_is_a_timeout(self):
        def session(peer, _):
            time.sleep(1)
        server = FakeWsServer(session)
        try:
            ws = WebSocket.connect(f"ws://127.0.0.1:{server.port}/websocket")
            ws.settimeout(0.2)
            with self.assertRaises(_socket.timeout):
                ws.recv_text()
            ws.close()
        finally:
            server.close()


class WatcherTests(unittest.TestCase):
    def make(self, session):
        server = FakeWsServer(session)
        store = temp_store(clock=lambda: int(time.time() * 1000))
        core = CoreApi(base_url=f"http://127.0.0.1:{server.port}/api", token="supervisor-token")
        notified = []
        importer = Importer(core, store, SourceConfig("sensor.tomas_krnac_glucose_level"), 10,
                            on_new=lambda: notified.append(1))
        watcher = Watcher(core, importer)
        watcher.PING_AFTER_SECONDS = 0.3
        return server, store, importer, watcher, notified

    def test_push_stores_readings_and_slows_the_poll(self):
        received = {}
        now = int(time.time() * 1000)

        def session(peer, number):
            peer.send_json({"type": "auth_required"})
            received["auth"] = peer.recv_json()
            peer.send_json({"type": "auth_ok"})
            received["subscribe"] = peer.recv_json()
            peer.send_json({"id": 1, "type": "result", "success": True})
            to_state = {
                "entity_id": "sensor.tomas_krnac_glucose_level", "state": "6.8",
                "attributes": {"unit_of_measurement": "mmol/L", "value_mg_dl": 122, "trend": "decreasing",
                               "measurement_timestamp": util.iso_utc(now - 20_000)},
                "last_changed": util.iso_utc(now), "last_updated": util.iso_utc(now),
            }
            peer.send_json({"id": 1, "type": "event", "event": {"variables": {"trigger": {"to_state": to_state}}}})
            # The watcher pings when idle; answer it, then drop the connection.
            ping = peer.recv_json()
            peer.send_json({"id": ping["id"], "type": "pong"})
            received["ping"] = ping
            time.sleep(0.2)

        server, store, importer, watcher, notified = self.make(session)
        stop = threading.Event()
        thread = threading.Thread(target=watcher.loop, args=(stop,), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if store.latest_entry() and "ping" in received:
                    break
                time.sleep(0.05)
            entry = store.latest_entry()
            self.assertEqual((entry["sgv"], entry["direction"]), (122, "FortyFiveDown"))
            self.assertEqual(entry["date"], now - 20_000)
            self.assertEqual(received["auth"], {"type": "auth", "access_token": "supervisor-token"})
            self.assertEqual(received["subscribe"]["type"], "subscribe_trigger")
            self.assertEqual(received["subscribe"]["trigger"],
                             {"platform": "state", "entity_id": "sensor.tomas_krnac_glucose_level"})
            self.assertEqual(received["ping"]["type"], "ping")
            self.assertEqual(server.paths[0], "GET /websocket HTTP/1.1")
            self.assertEqual(notified, [1])
            # After the drop the history poll returns to every minute until reconnected.
            for _ in range(60):
                if not watcher.connected.is_set():
                    break
                time.sleep(0.05)
            self.assertEqual(importer.poll_seconds, Watcher.FALLBACK_POLL_SECONDS)
        finally:
            stop.set()
            thread.join(5)
            server.close()

    def test_poll_is_slow_only_while_subscribed(self):
        def session(peer, number):
            peer.send_json({"type": "auth_required"})
            peer.recv_json()
            peer.send_json({"type": "auth_ok"})
            peer.recv_json()
            peer.send_json({"id": 1, "type": "result", "success": True})
            time.sleep(1)

        server, store, importer, watcher, _ = self.make(session)
        watcher.PING_AFTER_SECONDS = 5
        stop = threading.Event()
        thread = threading.Thread(target=watcher.loop, args=(stop,), daemon=True)
        thread.start()
        try:
            self.assertTrue(watcher.connected.wait(3))
            self.assertEqual(importer.poll_seconds, Watcher.PUSH_POLL_SECONDS)
            self.assertTrue(importer.wake.is_set())  # catch-up read requested on connect
        finally:
            stop.set()
            thread.join(5)
            server.close()

    def test_refused_token_falls_back_to_polling(self):
        def session(peer, _):
            peer.send_json({"type": "auth_required"})
            peer.recv_json()
            peer.send_json({"type": "auth_invalid", "message": "Invalid access token"})

        server, store, importer, watcher, _ = self.make(session)
        try:
            with self.assertRaises(ConnectionError) as caught:
                watcher.run_session(threading.Event())
            self.assertIn("Invalid access token", str(caught.exception))
            self.assertEqual(importer.poll_seconds, 60)
        finally:
            server.close()

    def test_url_from_supervisor_api(self):
        self.assertEqual(Watcher(CoreApi(base_url="http://supervisor/core/api", token="t"), None).url,
                         "ws://supervisor/core/websocket")

    def test_pushed_state_for_another_entity_is_ignored(self):
        store = temp_store()
        importer = Importer(FakeCore(), store, SourceConfig("sensor.a"), 10, clock=lambda: NOW_MS)
        self.assertEqual(importer.ingest_state(ha_state(120, "2026-10-16T08:21:00Z") | {"entity_id": "sensor.b"}), 0)
        self.assertEqual(importer.ingest_state(ha_state(120, "2026-10-16T08:21:00Z") | {"entity_id": "sensor.a"}), 1)


# --------------------------------------------------------------------------- end to end


class FakeHomeAssistant(BaseHTTPRequestHandler):
    history = []
    posted = []

    def do_GET(self):  # noqa: N802
        body = json.dumps([FakeHomeAssistant.history] if FakeHomeAssistant.history else []).encode()
        self._send(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        FakeHomeAssistant.posted.append((self.path, json.loads(self.rfile.read(length)), self.headers.get("Authorization")))
        self._send(b"{}")

    def _send(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class EndToEndTests(unittest.TestCase):
    """The requests JugglucoNG, xDrip+ and watch faces send, against the real listeners."""

    def setUp(self):
        FakeHomeAssistant.history = []
        FakeHomeAssistant.posted = []
        self.ha = ThreadingHTTPServer(("127.0.0.1", 0), FakeHomeAssistant)
        threading.Thread(target=self.ha.serve_forever, daemon=True).start()
        core = CoreApi(base_url=f"http://127.0.0.1:{self.ha.server_port}/api", token="supervisor-token")
        options = Options(api_secret=READ_SECRET, write_secret=WRITE_SECRET,
                          publish_entity_id="sensor.nightscout_glucose")
        self.store = Store(os.path.join(tempfile.mkdtemp(), "ns.sqlite3"))
        self.app = App(options, self.store, core)
        self.app.start(host="127.0.0.1", read_port=0, write_port=0)
        read_server, write_server = self.app.servers
        self.read = f"http://127.0.0.1:{read_server.server_port}"
        self.write = f"http://127.0.0.1:{write_server.server_port}"

    def tearDown(self):
        self.app.shutdown()
        self.ha.shutdown()
        self.ha.server_close()
        self.store.close()

    def call(self, base, path, method="GET", body=None, secret=None):
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["api-secret"] = sha(secret)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def jugglucong_entry(self, sgv, date_ms):
        # What JugglucoNG's native uploader sends, field for field.
        return {"type": "sgv", "device": "SYAI-123", "dateString": util.iso_utc(date_ms), "date": date_ms,
                "sgv": sgv, "delta": 1.5, "direction": "Flat", "noise": 1, "filtered": sgv * 1000,
                "unfiltered": sgv * 1000, "rssi": 100, "identifier": f"sgv-{date_ms}"}

    def test_upload_then_follow(self):
        now = int(time.time() * 1000)
        batch = [self.jugglucong_entry(100 + i, now - (5 - i) * MIN) for i in range(6)]
        code, stored = self.call(self.write, "/api/v1/entries", "POST", batch, WRITE_SECRET)
        self.assertEqual(code, 200)
        self.assertTrue(all(len(doc["_id"]) == 24 for doc in stored))
        # A retried upload is accepted and changes nothing.
        self.assertEqual(self.call(self.write, "/api/v1/entries", "POST", batch, WRITE_SECRET)[0], 200)

        gte = urllib.parse.quote("find[date][$gte]")
        code, entries = self.call(self.read, f"/api/v1/entries/sgv.json?count=1000&{gte}={now - 3 * MIN}", secret=READ_SECRET)
        self.assertEqual(code, 200)
        self.assertEqual([e["sgv"] for e in entries], [105, 104, 103, 102])  # $gte is inclusive
        self.assertEqual(entries[0]["identifier"], f"sgv-{now}")

        code, pebble = self.call(self.read, "/pebble?count=2", secret=READ_SECRET)
        self.assertEqual((code, [b["sgv"] for b in pebble["bgs"]]), (200, ["105", "104"]))

        for _ in range(50):
            if FakeHomeAssistant.posted:
                break
            time.sleep(0.05)
        path, payload, auth = FakeHomeAssistant.posted[-1]
        self.assertEqual(path, "/api/states/sensor.nightscout_glucose")
        self.assertEqual((payload["state"], auth), ("105", "Bearer supervisor-token"))

    def test_treatment_lifecycle_like_jugglucong(self):
        treatment = {"eventType": "Meal Bolus", "insulin": 2.5, "carbs": 30, "date": NOW_MS,
                     "created_at": util.iso_utc(NOW_MS), "identifier": "JNG-42", "enteredBy": "JugglucoNG"}
        code, created = self.call(self.write, "/api/v1/treatments", "POST", treatment, WRITE_SECRET)
        self.assertEqual(code, 200)
        remote_id = created[0]["_id"]
        # The native uploader upserts with PUT.
        code, _ = self.call(self.write, "/api/v1/treatments", "PUT", dict(treatment, insulin=3), WRITE_SECRET)
        self.assertEqual(code, 200)
        code, listed = self.call(self.read, "/api/v1/treatments.json?count=512", secret=READ_SECRET)
        self.assertEqual([(t["_id"], t["insulin"], t["identifier"]) for t in listed], [(remote_id, 3, "JNG-42")])
        code, result = self.call(self.write, f"/api/v1/treatments/{remote_id}", "DELETE", secret=WRITE_SECRET)
        self.assertEqual((code, result["n"]), (200, 1))
        self.assertEqual(self.call(self.read, "/api/v1/treatments.json", secret=READ_SECRET), (200, []))

    def test_secrets_and_ports_are_separate(self):
        entry = [self.jugglucong_entry(120, NOW_MS)]
        # The read port refuses writes even with the write secret.
        self.assertEqual(self.call(self.read, "/api/v1/entries", "POST", entry, WRITE_SECRET)[0], 405)
        # The write port refuses the read secret entirely.
        self.assertEqual(self.call(self.write, "/api/v1/entries", "POST", entry, READ_SECRET)[0], 401)
        self.assertEqual(self.call(self.write, "/api/v1/entries.json", secret=READ_SECRET)[0], 401)
        self.assertEqual(self.call(self.read, "/api/v1/entries.json")[0], 401)
        self.assertEqual(self.store.query_entries(None, 10), [])
        # Status answers without a secret but gives nothing away.
        code, status = self.call(self.read, "/api/v1/status.json")
        self.assertEqual((code, set(status)), (200, {"status", "apiEnabled", "authorized"}))
        code, auth = self.call(self.write, "/api/v1/verifyauth", secret=WRITE_SECRET)
        self.assertTrue(auth["message"]["canWrite"])

    def test_bad_upload_is_explained(self):
        code, body = self.call(self.write, "/api/v1/entries", "POST", [{"type": "sgv", "sgv": 4000, "date": NOW_MS}],
                               WRITE_SECRET)
        self.assertEqual(code, 400)
        self.assertIn("outside", body["message"])


if __name__ == "__main__":
    unittest.main()
