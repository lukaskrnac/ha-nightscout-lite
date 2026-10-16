"""Home Assistant on both sides of the store.

In: the history of an existing glucose sensor (e.g. from the LibreView integration) is
imported into the store, so its readings are served like uploaded ones.

Out: the newest reading is written to an entity of its own, so Home Assistant's recorder keeps
the history, whichever sensor or uploader the reading came from.

Both go through the Supervisor's proxy to the core API with the token the Supervisor gives the
add-on. That token can do anything the core API can; this module calls exactly two endpoints:
reading one entity's history and setting the state of the add-on's own entity.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .store import Store, ValidationError
from .util import DIRECTION_ARROWS, MGDL_PER_MMOL, MINUTE_MS, iso_utc, map_direction, now_ms, parse_time_ms, to_mgdl

LOG = logging.getLogger("nightscout_lite.ha")

SOURCE_HA = "home-assistant"
DEVICE_HA = "home-assistant"

TIMESTAMP_ATTRIBUTE_CANDIDATES = (
    "timestamp",
    "measurement_timestamp",
    "measurement_time",
    "measured_at",
    "last_measurement",
    "FactoryTimestamp",
    "factory_timestamp",
    "time",
)
TREND_ATTRIBUTE_CANDIDATES = ("trend", "trend_arrow", "TrendArrow", "trendArrow", "direction")
# Integrations that show mmol/L often keep the sensor's own mg/dL value beside it. That one is
# exact; converting the rounded mmol/L back can land a unit off.
MGDL_ATTRIBUTE_CANDIDATES = ("value_mg_dl", "value_mgdl", "mg_dl", "mgdl", "ValueInMgPerDl", "sgv")

# A reading older than this is history, not news: it goes to the store but not to the entity,
# whose recorder would file it under the time it arrived.
PUBLISH_MAX_AGE_MS = 15 * MINUTE_MS
HEARTBEAT_SECONDS = 300


@dataclass
class SourceConfig:
    entity_id: str
    timestamp_attribute: str = ""
    trend_attribute: str = ""
    trend_entity_id: str = ""


class CoreApi:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or os.environ.get("HA_API_URL") or "http://supervisor/core/api").rstrip("/")
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else None

    def history(self, start_ms: int, entity_ids: list[str]) -> dict[str, list[dict]]:
        start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()
        query = urllib.parse.urlencode(
            {
                "filter_entity_id": ",".join(entity_ids),
                # Attribute-only updates (a new measurement time, same value) are not
                # "significant" to the recorder, but they are readings to a follower.
                "significant_changes_only": "0",
            }
        )
        payload = self._request("GET", f"/history/period/{urllib.parse.quote(start, safe='')}?{query}")
        grouped: dict[str, list[dict]] = {}
        for series in payload or []:
            if series and series[0].get("entity_id"):
                grouped.setdefault(series[0]["entity_id"], []).extend(series)
        return grouped

    def set_state(self, entity_id: str, state: str, attributes: dict) -> None:
        self._request("POST", f"/states/{entity_id}", {"state": state, "attributes": attributes})


# --------------------------------------------------------------------------- in


def _first_attribute(attributes: dict, configured: str, candidates: tuple[str, ...]) -> Any:
    if configured:
        return attributes.get(configured)
    for name in candidates:
        if name in attributes:
            return attributes[name]
    return None


def measurement_time(state: dict, config: SourceConfig) -> int | None:
    attributes = state.get("attributes") or {}
    names = (config.timestamp_attribute,) if config.timestamp_attribute else TIMESTAMP_ATTRIBUTE_CANDIDATES
    for name in names:
        parsed = parse_time_ms(attributes.get(name))
        if parsed is not None:
            return parsed
    # last_updated moves with attribute changes too, so it follows a poll that brought the
    # same value with a new measurement time better than last_changed does.
    return parse_time_ms(state.get("last_updated")) or parse_time_ms(state.get("last_changed"))


def states_to_entries(states: list[dict], config: SourceConfig, trend_states: list[dict] | None = None) -> list[dict]:
    """Nightscout sgv documents from Home Assistant states, oldest first, one per time."""
    trend_points: list[tuple[int, str]] = []
    for state in trend_states or []:
        when = parse_time_ms(state.get("last_updated")) or parse_time_ms(state.get("last_changed"))
        direction = map_direction(state.get("state"))
        if when is not None and direction is not None:
            trend_points.append((when, direction))
    trend_points.sort()

    def trend_at(date_ms: int) -> str | None:
        chosen = None
        for when, direction in trend_points:
            if when > date_ms + MINUTE_MS:
                break
            if date_ms - when <= 5 * MINUTE_MS:
                chosen = direction
        return chosen

    by_time: dict[int, dict] = {}
    for state in states:
        attributes = state.get("attributes") or {}
        mgdl = to_mgdl(state.get("state"), attributes.get("unit_of_measurement"))
        if mgdl is None:
            continue
        for name in MGDL_ATTRIBUTE_CANDIDATES:
            exact = to_mgdl(attributes.get(name), "mg/dL")
            # Only when it agrees with the state, so a stale attribute cannot replace a value.
            if exact is not None and abs(exact - mgdl) <= 3:
                mgdl = exact
                break
        date_ms = measurement_time(state, config)
        if date_ms is None:
            continue
        direction = map_direction(_first_attribute(attributes, config.trend_attribute, TREND_ATTRIBUTE_CANDIDATES))
        if direction is None and trend_points:
            direction = trend_at(date_ms)
        doc = {
            "type": "sgv",
            "sgv": int(round(mgdl)),
            "date": date_ms,
            "dateString": iso_utc(date_ms),
            "device": DEVICE_HA,
        }
        if direction:
            doc["direction"] = direction
        by_time[date_ms] = doc  # the newest state for a time carries the fullest attributes
    return [by_time[key] for key in sorted(by_time)]


class Importer:
    """Copies the source sensor's history into the store, once a minute."""

    CURSOR_KEY = "ha_import_cursor"

    def __init__(self, api: CoreApi, store: Store, config: SourceConfig, lookback_days: int,
                 on_new: Callable[[], None] = lambda: None, clock=now_ms):
        self.api = api
        self.store = store
        self.config = config
        self.lookback_days = lookback_days
        self.on_new = on_new
        self.clock = clock

    def run_once(self) -> int:
        now = self.clock()
        oldest = now - self.lookback_days * 24 * 60 * MINUTE_MS
        cursor = int(self.store.get_meta(self.CURSOR_KEY, "0") or 0)
        # A little overlap: a state can be recorded a moment after the one before it was read.
        start = max(oldest, cursor - 5 * MINUTE_MS) if cursor else oldest
        entity_ids = [self.config.entity_id]
        if self.config.trend_entity_id:
            entity_ids.append(self.config.trend_entity_id)
        grouped = self.api.history(start, entity_ids)
        docs = states_to_entries(
            grouped.get(self.config.entity_id, []),
            self.config,
            grouped.get(self.config.trend_entity_id) if self.config.trend_entity_id else None,
        )
        inserted = 0
        for doc in docs:
            try:
                inserted += self.store.insert_entries([doc], SOURCE_HA).inserted
            except ValidationError as error:
                LOG.debug("Skipped a source reading: %s", error)
        if docs:
            self.store.set_meta(self.CURSOR_KEY, str(max(doc["date"] for doc in docs)))
        if inserted:
            self.on_new()
        return inserted

    def loop(self, stop: threading.Event, interval_seconds: int = 60) -> None:
        while not stop.is_set():
            try:
                count = self.run_once()
                if count:
                    LOG.info("Imported %d reading(s) from %s", count, self.config.entity_id)
            except Exception as error:  # noqa: BLE001 - keep importing after a bad minute
                LOG.warning("Import from %s failed: %s", self.config.entity_id, error)
            stop.wait(interval_seconds)


# --------------------------------------------------------------------------- out


def entity_payload(entry: dict, display_units: str, previous: dict | None) -> tuple[str, dict]:
    sgv = entry["sgv"]
    direction = entry.get("direction") or ""
    mmol = display_units == "mmol"
    value = f"{sgv / MGDL_PER_MMOL:.1f}" if mmol else str(sgv)
    delta = entry.get("delta")
    if delta is None and previous is not None and 0 < entry["date"] - previous["date"] <= 15 * MINUTE_MS:
        delta = sgv - previous["sgv"]
    attributes = {
        "unit_of_measurement": "mmol/L" if mmol else "mg/dL",
        "device_class": "blood_glucose_concentration",
        "state_class": "measurement",
        "friendly_name": "Glucose (Nightscout)",
        "icon": "mdi:water",
        "measured_at": iso_utc(entry["date"]),
        "sgv_mgdl": sgv,
        "direction": direction or None,
        "trend_arrow": DIRECTION_ARROWS.get(direction),
        "delta": (round(delta / MGDL_PER_MMOL, 2) if mmol else delta) if delta is not None else None,
        "device": entry.get("device"),
    }
    return value, attributes


class Publisher:
    """Keeps one Home Assistant entity at the newest reading."""

    LAST_KEY = "ha_published_date"

    def __init__(self, api: CoreApi, store: Store, entity_id: str, display_units: str, clock=now_ms):
        self.api = api
        self.store = store
        self.entity_id = entity_id
        self.display_units = display_units
        self.clock = clock
        self.wake = threading.Event()

    def publish_once(self, heartbeat: bool = False) -> bool:
        latest = self.store.query_entries(["sgv"], 2)
        if not latest:
            return False
        entry = latest[0]
        published = int(self.store.get_meta(self.LAST_KEY, "0") or 0)
        if self.clock() - entry["date"] > PUBLISH_MAX_AGE_MS:
            return False
        if entry["date"] <= published and not heartbeat:
            return False
        # A heartbeat resends the same state and attributes, which the recorder does not file
        # as a change; it only brings the entity back after Home Assistant restarted.
        value, attributes = entity_payload(entry, self.display_units, latest[1] if len(latest) > 1 else None)
        self.api.set_state(self.entity_id, value, attributes)
        self.store.set_meta(self.LAST_KEY, str(entry["date"]))
        return True

    def notify(self) -> None:
        self.wake.set()

    def loop(self, stop: threading.Event) -> None:
        heartbeat = True  # the first round also restores the entity after a restart
        while not stop.is_set():
            try:
                self.publish_once(heartbeat=heartbeat)
            except Exception as error:  # noqa: BLE001
                LOG.warning("Publishing %s failed: %s", self.entity_id, error)
            woke = self.wake.wait(HEARTBEAT_SECONDS)
            self.wake.clear()
            heartbeat = not woke
