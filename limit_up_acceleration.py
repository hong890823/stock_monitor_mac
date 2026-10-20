"""Detect the high-risk combination of a limit-up close and shrinking volume."""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from intraday_tactics import limit_pct_for_board


SHRINKING_VOLUME_RATIO = 0.8
MIN_HISTORY = 2
VOLUME_LOOKBACK = 5
LIMIT_TOLERANCE_PCT = 0.2


def _finite_positive(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def evaluate_limit_up_acceleration(
    code: str,
    closes: Sequence[Any],
    volumes: Sequence[Any],
) -> Dict[str, Any]:
    """Return a conservative flag for a limit-up close with shrinking volume.

    The signal is deliberately descriptive only. It must not be used as a
    standalone buy condition.
    """
    result = {
        "limit_up": False,
        "shrinking": False,
        "flagged": False,
        "volume_ratio": None,
        "tag": "",
    }
    if len(closes) < MIN_HISTORY or len(volumes) < MIN_HISTORY:
        return result

    current_close = _finite_positive(closes[-1])
    previous_close = _finite_positive(closes[-2])
    current_volume = _finite_positive(volumes[-1])
    if not all((current_close, previous_close, current_volume)):
        return result

    limit_pct = limit_pct_for_board(str(code))
    change_pct = (current_close / previous_close - 1) * 100
    limit_up = change_pct >= limit_pct - LIMIT_TOLERANCE_PCT
    result["limit_up"] = limit_up
    if not limit_up:
        return result

    prior_volumes = [
        volume for volume in (_finite_positive(item) for item in volumes[:-1])
        if volume is not None
    ][-VOLUME_LOOKBACK:]
    if not prior_volumes:
        return result
    average_volume = sum(prior_volumes) / len(prior_volumes)
    volume_ratio = current_volume / average_volume if average_volume else None
    result["volume_ratio"] = volume_ratio
    shrinking = volume_ratio is not None and volume_ratio <= SHRINKING_VOLUME_RATIO
    result["shrinking"] = shrinking
    if shrinking:
        result["flagged"] = True
        result["tag"] = "⚠️涨停缩量加速·不追高"
    return result
