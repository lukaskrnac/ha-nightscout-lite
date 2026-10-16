"""Pure helpers: time, units, trend directions, queries and secrets. No I/O."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MGDL_PER_MMOL = 18.0182
MINUTE_MS = 60_000

NS_DIRECTIONS = (
    "DoubleDown",
    "SingleDown",
    "FortyFiveDown",
    "Flat",
    "FortyFiveUp",
    "SingleUp",
    "DoubleUp",
    "NOT COMPUTABLE",
    "RATE OUT OF RANGE",
    "NONE",
)

# LibreLinkUp's 1..5 arrows, mapped the way nightscout-librelink-up maps them.
LLU_ARROWS = {1: "SingleDown", 2: "FortyFiveDown", 3: "Flat", 4: "FortyFiveUp", 5: "SingleUp"}

# Words integrations use for trends, matched as whole phrases on lowercased text, most
# specific first.
TREND_WORDS = (
    (("rising quickly", "rising fast", "rising rapidly", "rapidly rising", "increasing quickly",
      "increasing fast", "increasing rapidly", "rapidly increasing"), "SingleUp"),
    (("falling quickly", "falling fast", "falling rapidly", "rapidly falling", "decreasing quickly",
      "decreasing fast", "decreasing rapidly", "rapidly decreasing"), "SingleDown"),
    (("rising slowly", "slowly rising"), "FortyFiveUp"),
    (("falling slowly", "slowly falling"), "FortyFiveDown"),
    (("rising", "increasing", "up"), "FortyFiveUp"),
    (("falling", "decreasing", "down"), "FortyFiveDown"),
    (("stable", "steady", "flat", "constant"), "Flat"),
)

# Arrow glyphs for the Home Assistant entity, which people read rather than parse.
DIRECTION_ARROWS = {
    "DoubleDown": "⇊",
    "SingleDown": "↓",
    "FortyFiveDown": "↘",
    "Flat": "→",
    "FortyFiveUp": "↗",
    "SingleUp": "↑",
    "DoubleUp": "⇈",
}


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


def parse_time_ms(value: Any) -> int | None:
    """Milliseconds since the epoch from the shapes clients and integrations use, or None.

    Accepts ISO 8601 (a missing zone is taken as UTC, which is what Home Assistant stores and
    what Nightscout assumes), epoch seconds or milliseconds, and LibreLinkUp's
    "10/16/2026 8:21:00 AM" (UTC).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        return int(number if number > 10_000_000_000 else number * 1000)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,13}(\.\d+)?", text):
        return parse_time_ms(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for pattern in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def iso_utc(ms: int) -> str:
    whole = datetime.fromtimestamp(ms // 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{whole}.{ms % 1000:03d}Z"


def to_mgdl(value: Any, unit: str | None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    unit_text = (unit or "").lower()
    if "mmol" in unit_text or (not unit_text and number < 35):
        number *= MGDL_PER_MMOL
    if number < 10 or number > 1000:
        return None
    return number


def map_direction(raw: Any) -> str | None:
    """A Nightscout direction from an arrow number, a Nightscout name or trend words."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        return LLU_ARROWS.get(int(float(raw)))
    text = str(raw).strip()
    if not text:
        return None
    for name in NS_DIRECTIONS:
        if text.lower() == name.lower():
            return name
    lowered = text.lower().replace("_", " ").replace("-", " ")
    for words, direction in TREND_WORDS:
        for word in words:
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                return direction
    return None


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def secret_matches(candidate: str, secret: str) -> bool:
    """A presented credential against a secret, plain or SHA-1 as Nightscout clients send it."""
    if not candidate or not secret:
        return False
    return hmac.compare_digest(candidate.strip().lower(), sha1_hex(secret)) or hmac.compare_digest(
        candidate.strip(), secret
    )


def presented_credentials(headers: dict[str, str], params: dict[str, list[str]]) -> list[str]:
    """Every way Nightscout clients present a secret: api-secret header, bearer, ?token=, ?secret=."""
    found: list[str] = []
    header = headers.get("api-secret")
    if header:
        found.append(header.strip())
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        found.append(authorization[7:].strip())
    for name in ("token", "secret"):
        found.extend(value.strip() for value in params.get(name, []) if value.strip())
    return found


# --------------------------------------------------------------------------- queries

_FIND_RE = re.compile(r"^find\[([A-Za-z_]+)\]\[\$(gte|gt|lte|lt)\]$")
_FIND_EQ_RE = re.compile(r"^find\[([A-Za-z_]+)\](\[\$eq\])?$")
TIME_FIELDS = {"date", "dateString", "sysTime", "created_at", "mills", "srvModified"}


@dataclass
class Query:
    count: int
    gte: int | None = None
    gt: int | None = None
    lte: int | None = None
    lt: int | None = None
    equals: dict[str, str] | None = None

    def lower_bound(self) -> int | None:
        if self.gte is not None:
            return self.gte
        if self.gt is not None:
            return self.gt + 1
        return None

    def upper_bound(self) -> int | None:
        if self.lt is not None:
            return self.lt - 1
        return self.lte


def parse_query(params: dict[str, list[str]], default_count: int, max_count: int = 10_000) -> Query:
    query = Query(count=default_count, equals={})
    raw_count = (params.get("count") or [None])[0]
    if raw_count is not None:
        try:
            query.count = max(1, min(int(float(raw_count)), max_count))
        except ValueError:
            pass
    for key, values in params.items():
        if not values:
            continue
        ranged = _FIND_RE.match(key)
        if ranged and ranged.group(1) in TIME_FIELDS:
            bound = parse_time_ms(values[0])
            if bound is not None:
                setattr(query, ranged.group(2), bound)
            continue
        equal = _FIND_EQ_RE.match(key)
        if equal and equal.group(1) not in TIME_FIELDS:
            query.equals[equal.group(1)] = values[0]
    return query
