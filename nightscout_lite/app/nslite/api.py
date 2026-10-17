"""The Nightscout v1 API over the store, without the socket so it can be tested directly.

There are two listeners. The read listener answers reads only and is the one to publish (for
example with Tailscale Funnel). The write listener answers reads and writes, has its own secret,
and is meant to stay inside the network or the tailnet. A leaked read secret therefore cannot
put a single value into the data people follow.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .store import Store, ValidationError
from .util import MINUTE_MS, iso_utc, now_ms, parse_query, presented_credentials, secret_matches

LOG = logging.getLogger("nightscout_lite.api")

REPORTED_VERSION = "15.0.2"
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_DOCS_PER_WRITE = 10_000

ENTRY_TYPE_PATHS = {
    "/api/v1/entries": None,
    "/api/v1/entries/sgv": ["sgv"],
    "/api/v1/entries/mbg": ["mbg"],
    "/api/v1/entries/cal": ["cal"],
    "/api/v1/entries/current": ["sgv"],
}
EMPTY_ARRAY_PATHS = {"/api/v1/profile", "/api/v1/food", "/api/v1/activity"}
HOLD_PATHS = {"/api/v1/entries", "/api/v1/entries/sgv"}
# Holding pays off only when the next reading is never far off; readings further apart than
# this (a five-minute sensor, say) are never waited for.
MINUTE_CADENCE_MAX_MS = 2 * MINUTE_MS

TREATMENT_ID_RE = re.compile(r"^/api/v1/treatments/([A-Za-z0-9_\-:.]{1,128})$")
ENTRY_ID_RE = re.compile(r"^/api/v1/entries/([0-9a-fA-F]{24})$")


@dataclass
class Response:
    code: int
    body: Any


def normalize_path(path: str) -> str:
    path = re.sub(r"/{2,}", "/", path).rstrip("/") or "/"
    return path[:-5] if path.endswith(".json") else path


class NightscoutApi:
    def __init__(
        self,
        store: Store,
        read_secret: str,
        write_secret: str,
        display_units: str = "mg/dl",
        on_write: Callable[[], None] = lambda: None,
        clock=now_ms,
        hold_seconds: float = 0,
        resync_after_seconds: float = 0,
    ):
        self.store = store
        self.read_secret = read_secret
        self.write_secret = write_secret
        self.display_units = display_units
        self.on_write = on_write
        self.clock = clock
        self.hold_seconds = hold_seconds
        self.resync_after_seconds = resync_after_seconds
        self._new_reading = threading.Condition()
        self._generation = 0

    # ------------------------------------------------------------------ holding

    def notify_new_reading(self) -> None:
        """Wakes requests waiting for a reading newer than the one their client has."""
        with self._new_reading:
            self._generation += 1
            self._new_reading.notify_all()

    def hold_reason(self) -> str | None:
        """Whether a follower's poll should wait for the next reading; None answers at once.

        Decided from time alone, whoever is asking: when the newest reading has been here
        longer than `resync_after_seconds`, the next one is due soon or the poll has drifted
        behind the readings. Either way waiting for it, up to `hold_seconds`, moves the
        follower's next poll (counted from when this one is answered) to just after a reading.
        """
        if self.hold_seconds <= 0:
            return None
        latest = self.store.query_entries(["sgv"], 2)
        if len(latest) < 2:
            return None
        newest, previous = latest
        if newest["date"] - previous["date"] > MINUTE_CADENCE_MAX_MS:
            return None
        arrived = newest.get("srvModified") or newest["date"]
        age_ms = self.clock() - arrived
        if age_ms < self.resync_after_seconds * 1000:
            return None
        return f"newest reading {age_ms / 1000:.0f} s old"

    def _hold_until_new(self) -> None:
        """Answers a held poll when the next reading arrives, or after `hold_seconds`.

        A follower that schedules its next poll a fixed interval after the previous one
        finished then lands just after each new reading, rather than up to a minute after it.
        """
        reason = self.hold_reason()
        if reason is None:
            return
        started = time.monotonic()
        deadline = started + self.hold_seconds
        with self._new_reading:
            seen = self._generation
            while self._generation == seen:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._new_reading.wait(remaining)
        LOG.debug("Held a follower poll (%s) for %.1f s", reason, time.monotonic() - started)

    # ------------------------------------------------------------------ dispatch

    def handle(self, method: str, raw_path: str, headers: dict[str, str], body: bytes, writable: bool) -> Response:
        parsed = urllib.parse.urlsplit(raw_path)
        path = normalize_path(parsed.path)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if path == "/":
            return Response(200, "Nightscout Lite for Home Assistant")
        if path == "/api/v1/status" and method in ("GET", "HEAD") and not self._authorized(headers, params, writable):
            # Clients probe status before they have a secret; a bare "ok" tells nothing about the data.
            return Response(200, {"status": "ok", "apiEnabled": True, "authorized": None})
        if not self._authorized(headers, params, writable):
            return Response(401, {"status": 401, "message": "Unauthorized", "description": "api-secret or token required"})

        if method in ("GET", "HEAD"):
            return self._read(path, params, writable)
        if not writable:
            return Response(405, {"status": 405, "message": "This address is read-only"})
        if method in ("POST", "PUT"):
            return self._write(method, path, body)
        if method == "DELETE":
            return self._delete(path)
        return Response(405, {"status": 405, "message": f"{method} is not supported"})

    def _authorized(self, headers: dict[str, str], params: dict[str, list[str]], writable: bool) -> bool:
        secrets = [self.write_secret] if writable else [self.read_secret, self.write_secret]
        for candidate in presented_credentials(headers, params):
            if any(secret and secret_matches(candidate, secret) for secret in secrets):
                return True
        return False

    # ------------------------------------------------------------------ reads

    def _read(self, path: str, params: dict[str, list[str]], writable: bool) -> Response:
        if path in ENTRY_TYPE_PATHS:
            current = path.endswith("/current")
            query = parse_query(params, default_count=1 if current else 10)
            # Followers syncing incrementally ask "everything since X"; a request for a fixed
            # range or a plain "last N" (a browser, a report) is never held.
            if path in HOLD_PATHS and query.lower_bound() is not None and query.upper_bound() is None:
                self._hold_until_new()
            types = ENTRY_TYPE_PATHS[path]
            if types is None:
                wanted = (query.equals or {}).get("type")
                types = [wanted] if wanted else None
            entries = self.store.query_entries(types, query.count, query.lower_bound(), query.upper_bound())
            return Response(200, self._with_deltas(entries))
        by_id = ENTRY_ID_RE.match(path)
        if by_id:
            found = self.store.get_entry(by_id.group(1))
            return Response(200, [found] if found else [])
        if path == "/api/v1/treatments":
            query = parse_query(params, default_count=100)
            return Response(
                200, self.store.query_treatments(query.count, query.lower_bound(), query.upper_bound(), query.equals)
            )
        if path == "/api/v1/devicestatus":
            query = parse_query(params, default_count=10)
            return Response(200, self.store.query_devicestatus(query.count, query.lower_bound(), query.upper_bound()))
        if path in EMPTY_ARRAY_PATHS:
            return Response(200, [])
        if path == "/api/v1/status":
            return Response(200, self.status(writable))
        if path == "/api/v1/verifyauth":
            return Response(
                200,
                {"status": 200, "message": {"canRead": True, "canWrite": writable, "isAdmin": False, "message": "OK"}},
            )
        if path in ("/api/v2/properties", "/api/v2/properties/bgnow"):
            return Response(200, self.properties())
        if path == "/pebble":
            query = parse_query(params, default_count=1, max_count=100)
            return Response(200, self.pebble(query.count))
        return Response(404, {"status": 404, "message": "Not found"})

    def _with_deltas(self, entries: list[dict]) -> list[dict]:
        sgvs = [entry for entry in entries if entry.get("type") == "sgv"]
        for newer, older in zip(sgvs, sgvs[1:]):
            if "delta" not in newer and 0 < newer["date"] - older["date"] <= 15 * MINUTE_MS:
                newer["delta"] = newer["sgv"] - older["sgv"]
        return entries

    def status(self, writable: bool) -> dict:
        now = self.clock()
        return {
            "status": "ok",
            "name": "nightscout",
            "version": REPORTED_VERSION,
            "serverTime": iso_utc(now),
            "serverTimeEpoch": now,
            "apiEnabled": True,
            "careportalEnabled": writable,
            "boluscalcEnabled": False,
            "settings": {"units": self.display_units, "timeFormat": 24, "enable": ["careportal"] if writable else [], "authDefaultRoles": "denied"},
            "extendedSettings": {},
            "authorized": None,
        }

    def properties(self) -> dict:
        latest = self.store.query_entries(["sgv"], 2)
        if not latest:
            return {}
        entry = self._with_deltas(latest)[0]
        return {
            "bgnow": {"mean": entry["sgv"], "last": entry["sgv"], "mills": entry["date"], "sgvs": [entry]},
            "delta": {"mgdl": entry.get("delta"), "mills": entry["date"]},
            "direction": {"value": entry.get("direction", "NONE"), "mills": entry["date"]},
        }

    def pebble(self, count: int) -> dict:
        entries = self._with_deltas(self.store.query_entries(["sgv"], count + 1))[:count]
        mmol = self.display_units == "mmol"
        bgs = []
        for entry in entries:
            sgv = entry["sgv"] / 18.0182 if mmol else entry["sgv"]
            delta = entry.get("delta")
            bgs.append(
                {
                    "sgv": f"{sgv:.1f}" if mmol else str(int(sgv)),
                    "trend": 0,
                    "direction": entry.get("direction", "NONE"),
                    "datetime": entry["date"],
                    "bgdelta": (f"{delta / 18.0182:.1f}" if mmol else str(delta)) if delta is not None else "0",
                }
            )
        return {"status": [{"now": self.clock()}], "bgs": bgs, "cals": []}

    # ------------------------------------------------------------------ writes

    def _parse_body(self, body: bytes) -> list[Any]:
        if len(body) > MAX_BODY_BYTES:
            raise ValidationError("request body too large")
        try:
            payload = json.loads(body.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError(f"body is not JSON: {error}") from None
        docs = payload if isinstance(payload, list) else [payload]
        if not docs or docs == [None]:
            raise ValidationError("empty body")
        if len(docs) > MAX_DOCS_PER_WRITE:
            raise ValidationError("too many documents in one request")
        return docs

    def _write(self, method: str, path: str, body: bytes) -> Response:
        try:
            docs = self._parse_body(body)
            if path == "/api/v1/entries":
                result = self.store.insert_entries(docs, source="upload")
            elif path == "/api/v1/treatments":
                result = self.store.write_treatments(docs, upsert=method == "PUT")
            elif path == "/api/v1/devicestatus":
                result = self.store.insert_devicestatus(docs)
            else:
                return Response(404, {"status": 404, "message": "Not found"})
        except ValidationError as error:
            return Response(400, {"status": 400, "message": str(error)})
        if result.inserted:
            self.on_write()
        return Response(200, result.docs)

    def _delete(self, path: str) -> Response:
        treatment = TREATMENT_ID_RE.match(path)
        if treatment:
            deleted = self.store.delete_treatment(treatment.group(1))
            if deleted:
                self.on_write()
            return Response(200, {"n": deleted, "ok": 1})
        entry = ENTRY_ID_RE.match(path)
        if entry:
            deleted = self.store.delete_entries([entry.group(1)])
            if deleted:
                self.on_write()
            return Response(200, {"n": deleted, "ok": 1})
        return Response(405, {"status": 405, "message": "Bulk deletes are not supported"})
