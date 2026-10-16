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
    def __init__(self, history=None):
        self.history_payload = history or {}
        self.history_calls = []
        self.states = []

    def history(self, start_ms, entity_ids):
        self.history_calls.append((start_ms, entity_ids))
        return self.history_payload

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
        self.assertEqual(importer.run_once(), 22)
        self.assertEqual(importer.run_once(), 0)
        self.assertEqual(len(notified), 1)
        first_start, second_start = core.history_calls[0][0], core.history_calls[1][0]
        self.assertEqual(first_start, NOW_MS - 10 * 24 * 60 * MIN)
        self.assertEqual(second_start, NOW_MS - 5 * MIN)

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
