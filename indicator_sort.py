"""指标选股榜单的排序规则，与 Android 版 IndicatorScreener 保持一致。

严格档三项条件相同，按 RSI 升序（越靠前越超卖）；
宽松档满足的是 2/3 或 3/3 项，先按满足项数降序，同分再按 RSI 升序。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List


def _rsi_key(item: Dict[str, Any]) -> float:
    """RSI 升序键；RSI 缺失或为 NaN（平均跌幅为 0 时算不出）时排在同分末尾。"""
    try:
        value = float(item.get("rsi"))
    except (TypeError, ValueError):
        return math.inf
    return math.inf if math.isnan(value) else value


def order_strict_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """严格档按 RSI 升序。"""
    return sorted(hits, key=lambda item: (bool(item.get("limit_up_shrink_acceleration")), _rsi_key(item)))


def order_relaxed_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """宽松档先按满足项数降序（3/3 在前），同分再按 RSI 升序。"""
    return sorted(
        hits,
        key=lambda item: (
            bool(item.get("limit_up_shrink_acceleration")),
            -float(item.get("relaxed_score") or 0),
            _rsi_key(item),
        ),
    )
