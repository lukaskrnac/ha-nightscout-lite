"""Keeps the source integration polling just after each reading becomes available.

A polling integration such as LibreView asks its cloud on a fixed one-minute rhythm, and that
rhythm keeps whatever phase it happened to start with. If it asks fifty seconds after a reading
reaches the cloud, every reading waits those fifty seconds, for good.

Home Assistant reschedules an integration's poll a full interval after any refresh, including
one asked for by `homeassistant.update_entity`. So one refresh at the right moment moves the
whole rhythm there. The right moment is "measurement + D", where D is how long a reading takes
to reach the cloud; D is a setting, because it cannot be told apart from the integration's own
waiting by watching readings arrive.

The aligner only acts when readings keep arriving later than D allows, and backs off when
nudging does not help (D set too short: the poll comes before the reading is there).
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from typing import Callable

LOG = logging.getLogger("nightscout_lite.align")

MINUTE_S = 60.0


class SourceAligner:
    TOLERANCE_S = 10.0  # a reading this much later than D still counts as in step
    LATE_READINGS_TO_ACT = 3  # consecutive late readings before a nudge
    MIN_NUDGE_SPACING_S = 300.0
    FAILED_NUDGES_TO_PAUSE = 2
    PAUSE_S = 3600.0
    LEAD_S = 2.0  # never schedule a nudge closer than this to now
    REPORT_EVERY = 30  # readings between lag summaries in the log

    def __init__(
        self,
        call_service: Callable[[str, str, dict], None],
        entity_id: str,
        delay_seconds: float,
        clock: Callable[[], float] = time.time,
        schedule: Callable[[float, Callable[[], None]], None] | None = None,
    ):
        self.call_service = call_service
        self.entity_id = entity_id
        self.delay_seconds = float(delay_seconds)
        self.clock = clock
        self.schedule = schedule or self._schedule_timer
        self.lags: deque[float] = deque(maxlen=self.REPORT_EVERY)
        self._lock = threading.Lock()
        self._late_streak = 0
        self._last_nudge_at = 0.0
        self._nudge_unconfirmed = False
        self._failed_nudges = 0
        self._paused_until = 0.0
        self._pending = False
        self._seen = 0

    @staticmethod
    def _schedule_timer(delay: float, action: Callable[[], None]) -> None:
        timer = threading.Timer(delay, action)
        timer.daemon = True
        timer.start()

    @property
    def enabled(self) -> bool:
        return self.delay_seconds > 0

    def median_lag(self) -> float | None:
        return statistics.median(self.lags) if self.lags else None

    def observe(self, measured_ms: int, arrived_ms: int) -> float:
        """One new reading: when it was measured and when Home Assistant got it. Returns the lag."""
        lag = (arrived_ms - measured_ms) / 1000.0
        with self._lock:
            self.lags.append(lag)
            self._seen += 1
            if self._seen % self.REPORT_EVERY == 0:
                LOG.info(
                    "Source lag over the last %d readings: median %.0f s, max %.0f s%s",
                    len(self.lags),
                    statistics.median(self.lags),
                    max(self.lags),
                    f" (target {self.delay_seconds:.0f} s)" if self.enabled else "",
                )
            if not self.enabled or self._pending:
                return lag
            now = self.clock()
            if lag <= self.delay_seconds + self.TOLERANCE_S:
                self._late_streak = 0
                if self._nudge_unconfirmed:
                    LOG.info("Source integration in step: reading %.0f s after measurement", lag)
                    self._nudge_unconfirmed = False
                    self._failed_nudges = 0
                return lag
            self._late_streak += 1
            if self._late_streak < self.LATE_READINGS_TO_ACT:
                return lag
            if now < self._paused_until or now - self._last_nudge_at < self.MIN_NUDGE_SPACING_S:
                return lag
            if self._nudge_unconfirmed:
                self._failed_nudges += 1
                if self._failed_nudges >= self.FAILED_NUDGES_TO_PAUSE:
                    LOG.warning(
                        "Nudging %s did not bring readings closer than %.0f s (still %.0f s): "
                        "source_delay_seconds (%.0f) is probably too short, so its poll comes "
                        "before the reading is there. Pausing for an hour; raise the setting.",
                        self.entity_id, self.delay_seconds + self.TOLERANCE_S, lag, self.delay_seconds,
                    )
                    self._paused_until = now + self.PAUSE_S
                    self._failed_nudges = 0
                    self._nudge_unconfirmed = False
                    self._late_streak = 0
                    return lag
            target = measured_ms / 1000.0 + self.delay_seconds
            while target < now + self.LEAD_S:
                target += MINUTE_S
            self._pending = True
            self._late_streak = 0
            delay = target - now
            LOG.info(
                "Readings reach Home Assistant %.0f s after measurement; moving %s's poll to %.0f s "
                "after each measurement (in %.0f s)",
                lag, self.entity_id, self.delay_seconds, delay,
            )
        self.schedule(delay, self._nudge)
        return lag

    def _nudge(self) -> None:
        try:
            self.call_service("homeassistant", "update_entity", {"entity_id": self.entity_id})
        except Exception as error:  # noqa: BLE001 - a failed nudge is retried on the next late run
            LOG.warning("Could not ask %s to refresh: %s", self.entity_id, error)
        finally:
            with self._lock:
                self._pending = False
                self._last_nudge_at = self.clock()
                self._nudge_unconfirmed = True
