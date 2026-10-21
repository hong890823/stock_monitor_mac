#!/usr/bin/env python3
"""Parse authoritative quote timestamps from live quote dictionaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class QuoteClock:
    day: str
    at: datetime


def parse_quote_clock(quote: Any) -> Optional[QuoteClock]:
    if not isinstance(quote, dict):
        return None
    raw_date = str(quote.get("date") or "").strip()
    raw_time = str(quote.get("time") or "").strip()
    candidates = []
    if raw_date and raw_time:
        candidates.append(f"{raw_date[:10]} {raw_time}")
    if len(raw_date) > 10:
        candidates.append(raw_date)
    for value in candidates:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        return QuoteClock(parsed.date().isoformat(), parsed)
    return None
