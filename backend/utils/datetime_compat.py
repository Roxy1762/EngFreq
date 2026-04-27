"""Datetime helpers that produce browser-safe ISO 8601 strings.

Safari (especially on iOS) refuses to parse `datetime.isoformat()` output that
includes 6-digit microseconds and lacks a timezone designator, returning
Invalid Date and triggering "The string did not match the expected pattern."
when the value is later passed to `toLocaleDateString`/`toLocaleTimeString`.

`iso_z` normalises naive UTC datetimes (the project stores `datetime.utcnow()`)
to the form `YYYY-MM-DDTHH:MM:SSZ`, which every modern browser accepts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def iso_z(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
