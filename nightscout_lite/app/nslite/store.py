"""SQLite store for entries, treatments and device status, with Nightscout's document shapes.

Documents are kept as the JSON the client sent, plus the fields Nightscout itself adds
(`_id`, `created_at`, `srvModified`), so a client reading back sees what it wrote. A few
columns are lifted out of the document for querying and de-duplication.

Validation lives here, at the single place every write passes through: a value that could
mislead someone following the data (an impossible glucose, a 900 U bolus, a reading from
next week) is refused rather than stored.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from .util import MINUTE_MS, iso_utc, now_ms, parse_time_ms

ENTRY_TYPES = {"sgv", "mbg", "cal", "etc", "sensor"}
SGV_RANGE = (10, 1000)  # Nightscout's LOW/HIGH placeholders (39, 401) stay inside.
MBG_RANGE = (10, 1000)
MAX_INSULIN_UNITS = 100.0
MAX_CARBS_GRAMS = 1000.0
MAX_FUTURE_ENTRY_MS = 15 * MINUTE_MS
MAX_FUTURE_TREATMENT_MS = 24 * 60 * MINUTE_MS
DEVICESTATUS_KEEP_DAYS = 30


class ValidationError(ValueError):
    """A write the store refuses; the message is meant for the client."""


@dataclass
class WriteResult:
    docs: list[dict]
    inserted: int
    duplicates: int


def new_id() -> str:
    # 24 hex characters, the shape of a Mongo ObjectId, which some clients expect.
    return uuid.uuid4().hex[:24]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN is not a number here


class Store:
    def __init__(self, path: str, sgv_dedupe_ms: int = 30_000, clock=now_ms):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.sgv_dedupe_ms = sgv_dedupe_ms
        self.clock = clock
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    date INTEGER NOT NULL,
                    sgv INTEGER,
                    source TEXT NOT NULL,
                    srv_modified INTEGER NOT NULL,
                    doc TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS entries_type_date ON entries(type, date);
                CREATE INDEX IF NOT EXISTS entries_date ON entries(date);

                CREATE TABLE IF NOT EXISTS treatments (
                    id TEXT PRIMARY KEY,
                    created INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    identifier TEXT,
                    srv_modified INTEGER NOT NULL,
                    doc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS treatments_created ON treatments(created);
                CREATE INDEX IF NOT EXISTS treatments_identifier ON treatments(identifier);

                CREATE TABLE IF NOT EXISTS devicestatus (
                    id TEXT PRIMARY KEY,
                    created INTEGER NOT NULL,
                    doc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS devicestatus_created ON devicestatus(created);

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------------ entries

    def _normalize_entry(self, raw: Any, now: int) -> dict:
        if not isinstance(raw, dict):
            raise ValidationError("entry must be a JSON object")
        doc = {key: value for key, value in raw.items() if not str(key).startswith("$")}
        entry_type = str(doc.get("type") or ("sgv" if "sgv" in doc else "mbg" if "mbg" in doc else "")).strip()
        if entry_type not in ENTRY_TYPES:
            raise ValidationError(f"unsupported entry type {entry_type!r}")
        date = (
            parse_time_ms(doc.get("date"))
            or parse_time_ms(doc.get("mills"))
            or parse_time_ms(doc.get("dateString"))
            or parse_time_ms(doc.get("sysTime"))
        )
        if date is None:
            raise ValidationError("entry needs a date")
        if date > now + MAX_FUTURE_ENTRY_MS:
            raise ValidationError("entry dated in the future")
        if entry_type == "sgv":
            sgv = _number(doc.get("sgv"))
            if sgv is None or not SGV_RANGE[0] <= sgv <= SGV_RANGE[1]:
                raise ValidationError("sgv missing or outside 10..1000 mg/dL")
            doc["sgv"] = int(round(sgv))
        if entry_type == "mbg":
            mbg = _number(doc.get("mbg"))
            if mbg is None or not MBG_RANGE[0] <= mbg <= MBG_RANGE[1]:
                raise ValidationError("mbg missing or outside 10..1000 mg/dL")
            doc["mbg"] = int(round(mbg))
        doc["type"] = entry_type
        doc["date"] = date
        doc["dateString"] = doc.get("dateString") or iso_utc(date)
        doc["sysTime"] = iso_utc(date)
        doc.setdefault("utcOffset", 0)
        return doc

    def insert_entries(self, raws: Iterable[Any], source: str) -> WriteResult:
        now = self.clock()
        docs = [self._normalize_entry(raw, now) for raw in raws]
        stored: list[dict] = []
        inserted = duplicates = 0
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for doc in docs:
                    existing = self._find_duplicate_entry(doc)
                    if existing is not None:
                        stored.append(existing)
                        duplicates += 1
                        continue
                    doc["_id"] = str(doc.get("_id") or new_id())
                    if self._db.execute("SELECT 1 FROM entries WHERE id=?", (doc["_id"],)).fetchone():
                        # A client-chosen id already used by another reading: keep both readings.
                        doc["_id"] = new_id()
                    doc["srvModified"] = now
                    self._db.execute(
                        "INSERT INTO entries(id, type, date, sgv, source, srv_modified, doc) VALUES(?,?,?,?,?,?,?)",
                        (doc["_id"], doc["type"], doc["date"], doc.get("sgv"), source, now, json.dumps(doc)),
                    )
                    stored.append(doc)
                    inserted += 1
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return WriteResult(stored, inserted, duplicates)

    def _find_duplicate_entry(self, doc: dict) -> dict | None:
        row = self._db.execute(
            "SELECT doc FROM entries WHERE type=? AND date=?", (doc["type"], doc["date"])
        ).fetchone()
        if row is None and doc["type"] == "sgv" and self.sgv_dedupe_ms > 0:
            # Two sources reporting the same reading (an uploader and the Home Assistant
            # sensor) land seconds apart; within the window the first one stands.
            row = self._db.execute(
                "SELECT doc FROM entries WHERE type='sgv' AND date>? AND date<? LIMIT 1",
                (doc["date"] - self.sgv_dedupe_ms, doc["date"] + self.sgv_dedupe_ms),
            ).fetchone()
        return json.loads(row["doc"]) if row else None

    def query_entries(
        self,
        types: Iterable[str] | None,
        count: int,
        gte: int | None = None,
        lte: int | None = None,
    ) -> list[dict]:
        clauses, args = [], []
        type_list = list(types or [])
        if type_list:
            clauses.append(f"type IN ({','.join('?' * len(type_list))})")
            args.extend(type_list)
        if gte is not None:
            clauses.append("date>=?")
            args.append(gte)
        if lte is not None:
            clauses.append("date<=?")
            args.append(lte)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT doc FROM entries {where} ORDER BY date DESC LIMIT ?", (*args, count)
            ).fetchall()
        return [json.loads(row["doc"]) for row in rows]

    def get_entry(self, entry_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT doc FROM entries WHERE id=?", (entry_id,)).fetchone()
        return json.loads(row["doc"]) if row else None

    def latest_entry(self, entry_type: str = "sgv") -> dict | None:
        found = self.query_entries([entry_type], 1)
        return found[0] if found else None

    def delete_entries(self, ids: Iterable[str]) -> int:
        deleted = 0
        with self._lock:
            for entry_id in ids:
                deleted += self._db.execute("DELETE FROM entries WHERE id=?", (entry_id,)).rowcount
        return deleted

    def latest_entry_date(self, source: str) -> int | None:
        with self._lock:
            row = self._db.execute("SELECT MAX(date) AS d FROM entries WHERE source=?", (source,)).fetchone()
        return row["d"] if row and row["d"] is not None else None

    # ------------------------------------------------------------------ treatments

    def _normalize_treatment(self, raw: Any, now: int) -> dict:
        if not isinstance(raw, dict):
            raise ValidationError("treatment must be a JSON object")
        doc = {key: value for key, value in raw.items() if not str(key).startswith("$")}
        created = (
            parse_time_ms(doc.get("created_at"))
            or parse_time_ms(doc.get("date"))
            or parse_time_ms(doc.get("mills"))
            or parse_time_ms(doc.get("timestamp"))
            or now
        )
        if created > now + MAX_FUTURE_TREATMENT_MS:
            raise ValidationError("treatment dated more than a day ahead")
        insulin = _number(doc.get("insulin"))
        if doc.get("insulin") is not None and (insulin is None or not 0 <= insulin <= MAX_INSULIN_UNITS):
            raise ValidationError(f"insulin outside 0..{MAX_INSULIN_UNITS:g} U")
        carbs = _number(doc.get("carbs"))
        if doc.get("carbs") is not None and (carbs is None or not 0 <= carbs <= MAX_CARBS_GRAMS):
            raise ValidationError(f"carbs outside 0..{MAX_CARBS_GRAMS:g} g")
        glucose = _number(doc.get("glucose"))
        if doc.get("glucose") is not None and (glucose is None or glucose <= 0 or glucose > 1000):
            raise ValidationError("glucose outside 0..1000")
        doc["eventType"] = str(doc.get("eventType") or "<none>")
        doc["created_at"] = iso_utc(created)
        doc.setdefault("date", created)
        doc.setdefault("utcOffset", 0)
        doc["mills"] = created
        return doc

    def write_treatments(self, raws: Iterable[Any], upsert: bool) -> WriteResult:
        """POST semantics (insert, skipping exact repeats) or PUT semantics (replace or insert).

        A repeat is the same `_id`, the same client `identifier` at the same time, or the same
        event type at the same time. PUT replaces such a match; POST hands back the stored one
        so a retried upload gets the id it got the first time instead of a second copy.
        """
        now = self.clock()
        docs = [self._normalize_treatment(raw, now) for raw in raws]
        stored: list[dict] = []
        inserted = duplicates = 0
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for doc in docs:
                    match_id = self._match_treatment(doc)
                    if match_id is not None and not upsert:
                        row = self._db.execute("SELECT doc FROM treatments WHERE id=?", (match_id,)).fetchone()
                        stored.append(json.loads(row["doc"]))
                        duplicates += 1
                        continue
                    doc["_id"] = match_id or str(doc.get("_id") or new_id())
                    doc["srvModified"] = now
                    created = doc["mills"]
                    self._db.execute(
                        "INSERT INTO treatments(id, created, event_type, identifier, srv_modified, doc) "
                        "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET created=excluded.created, "
                        "event_type=excluded.event_type, identifier=excluded.identifier, "
                        "srv_modified=excluded.srv_modified, doc=excluded.doc",
                        (doc["_id"], created, doc["eventType"], doc.get("identifier"), now, json.dumps(doc)),
                    )
                    stored.append(doc)
                    inserted += 1
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return WriteResult(stored, inserted, duplicates)

    def _match_treatment(self, doc: dict) -> str | None:
        if doc.get("_id"):
            row = self._db.execute("SELECT id FROM treatments WHERE id=?", (str(doc["_id"]),)).fetchone()
            if row:
                return row["id"]
        identifier = doc.get("identifier")
        if identifier:
            row = self._db.execute(
                "SELECT id FROM treatments WHERE identifier=? AND ABS(created-?)<=?",
                (identifier, doc["mills"], MINUTE_MS),
            ).fetchone()
            if row:
                return row["id"]
            # A known client identifier at another time is a different document (a re-upload
            # after an edit), so it does not fall through to the time match below.
            return None
        row = self._db.execute(
            "SELECT id FROM treatments WHERE event_type=? AND created=? AND identifier IS NULL",
            (doc["eventType"], doc["mills"]),
        ).fetchone()
        return row["id"] if row else None

    def query_treatments(
        self,
        count: int,
        gte: int | None = None,
        lte: int | None = None,
        equals: dict[str, str] | None = None,
    ) -> list[dict]:
        clauses, args = [], []
        if gte is not None:
            clauses.append("created>=?")
            args.append(gte)
        if lte is not None:
            clauses.append("created<=?")
            args.append(lte)
        for field, column in (("eventType", "event_type"), ("identifier", "identifier"), ("_id", "id")):
            if equals and field in equals:
                clauses.append(f"{column}=?")
                args.append(equals[field])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT doc FROM treatments {where} ORDER BY created DESC LIMIT ?", (*args, count)
            ).fetchall()
        return [json.loads(row["doc"]) for row in rows]

    def delete_treatment(self, treatment_id: str) -> int:
        with self._lock:
            return self._db.execute(
                "DELETE FROM treatments WHERE id=? OR identifier=?", (treatment_id, treatment_id)
            ).rowcount

    # ------------------------------------------------------------------ devicestatus

    def insert_devicestatus(self, raws: Iterable[Any]) -> WriteResult:
        now = self.clock()
        stored = []
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for raw in raws:
                    if not isinstance(raw, dict):
                        raise ValidationError("devicestatus must be a JSON object")
                    doc = {key: value for key, value in raw.items() if not str(key).startswith("$")}
                    created = parse_time_ms(doc.get("created_at")) or parse_time_ms(doc.get("date")) or now
                    if created > now + MAX_FUTURE_ENTRY_MS:
                        raise ValidationError("devicestatus dated in the future")
                    doc["_id"] = new_id()
                    doc["created_at"] = iso_utc(created)
                    doc["mills"] = created
                    doc["srvModified"] = now
                    self._db.execute(
                        "INSERT INTO devicestatus(id, created, doc) VALUES(?,?,?)",
                        (doc["_id"], created, json.dumps(doc)),
                    )
                    stored.append(doc)
                cutoff = now - DEVICESTATUS_KEEP_DAYS * 24 * 60 * MINUTE_MS
                self._db.execute("DELETE FROM devicestatus WHERE created<?", (cutoff,))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return WriteResult(stored, len(stored), 0)

    def query_devicestatus(self, count: int, gte: int | None = None, lte: int | None = None) -> list[dict]:
        clauses, args = [], []
        if gte is not None:
            clauses.append("created>=?")
            args.append(gte)
        if lte is not None:
            clauses.append("created<=?")
            args.append(lte)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT doc FROM devicestatus {where} ORDER BY created DESC LIMIT ?", (*args, count)
            ).fetchall()
        return [json.loads(row["doc"]) for row in rows]

    # ------------------------------------------------------------------ housekeeping

    def prune_entries(self, keep_days: int) -> int:
        if keep_days <= 0:
            return 0
        cutoff = self.clock() - keep_days * 24 * 60 * MINUTE_MS
        with self._lock:
            return self._db.execute("DELETE FROM entries WHERE date<?", (cutoff,)).rowcount
