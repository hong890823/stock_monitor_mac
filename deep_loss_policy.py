#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深套持仓的应对口径。

浅亏时跌破支撑减仓控风险是标准动作；但浮亏已经很深时，破位那一刻往往伴随恐慌盘和
短期超卖，再喊「先割一部分」容易割在低点、把浮亏变成实亏。深套场景给的是**决策框架**
而不是单一动作：
- 逻辑 / 基本面已破坏 → 等反弹到压力位分批离场；
- 逻辑还在 → 破位处既不割肉摊成本也不补仓接飞刀，等缩量止跌或站回支撑后小仓补仓
  降成本，反弹冲高做 T，再创新低则取消补仓计划。

来源：Android 版 DeepLossPolicy.kt
"""

from __future__ import annotations

import math
from typing import Optional

# 浮亏（相对成本）到这个百分比算深套；没有 ATR 数据时的兜底阈值。
DEEP_LOSS_PCT = -20.0

# 深套阈值按 ATR 缩放的系数：约 8 个 ATR 日的跌幅才算「伤筋动骨」。
DEEP_LOSS_ATR_MULT = 8.0

# 缩放后的深套阈值上下限：银行股 -8% 太敏感，题材股 -40% 太迟钝。
DEEP_LOSS_MIN_PCT = -30.0
DEEP_LOSS_MAX_PCT = -12.0

# 深套补仓只给初始仓位的一半：下跌中加仓比开新仓风险更高，数量要比试仓更小。
ADD_ENTRY_RATIO = 0.5

# 建议数量按手取整。
LOT = 100


def floating_pnl_pct(cost: float, price: float) -> Optional[float]:
    """相对持仓成本的浮亏幅度（百分比）；成本未填或无效时返回 None。"""
    try:
        cost = float(cost)
        price = float(price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(cost) or not math.isfinite(price) or cost <= 0 or price <= 0:
        return None
    return (price / cost - 1) * 100


def deep_loss_threshold_pct(atr_pct: Optional[float]) -> float:
    """深套阈值（负百分比）：默认 -20%，有 ATR% 就按波动率缩放。

    银行股日均波动 1%，亏 12% 就算伤筋动骨；题材股日均波动 4%，
    要亏到 30% 才和银行股的 12% 等「疼」。
    """
    try:
        value = float(atr_pct) if atr_pct is not None else None
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value) or value <= 0:
        return DEEP_LOSS_PCT
    scaled = -DEEP_LOSS_ATR_MULT * value
    return max(DEEP_LOSS_MIN_PCT, min(DEEP_LOSS_MAX_PCT, scaled))


def is_deep_loss(cost: float, price: float, atr_pct: Optional[float] = None) -> bool:
    """是否深套（浮亏不超过按波动率缩放后的阈值）。"""
    pnl = floating_pnl_pct(cost, price)
    if pnl is None:
        return False
    return pnl <= deep_loss_threshold_pct(atr_pct)


def suggested_add_quantity(
    price: float,
    holding_quantity: float,
    total_capital: float,
    available_capital: float,
    initial_entry_pct: float,
    max_position_pct: float,
) -> float:
    """深套补仓的参考股数：空仓试仓口径的一半（ADD_ENTRY_RATIO）。

    再受最大仓位和可用资金约束，按手向下取整。没有总资金、可用资金不足一手
    或仓位已顶到上限都返回 0，调用方退回文字建议。
    """
    try:
        price = float(price)
        holding_quantity = float(holding_quantity or 0)
        total_capital = float(total_capital)
        available_capital = float(available_capital or 0)
        initial_entry_pct = float(initial_entry_pct)
        max_position_pct = float(max_position_pct)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(price) or price <= 0:
        return 0.0
    if not math.isfinite(total_capital) or total_capital <= 0:
        return 0.0
    if not all(math.isfinite(x) for x in (initial_entry_pct, max_position_pct)):
        return 0.0
    if initial_entry_pct <= 0 or max_position_pct <= 0:
        return 0.0
    initial_shares = total_capital * initial_entry_pct * ADD_ENTRY_RATIO / 100.0 / price
    headroom = total_capital * max_position_pct / 100.0 - holding_quantity * price
    if headroom <= 0:
        return 0.0
    shares = min(initial_shares, headroom / price)
    # 可用资金没填（0）视为未知，不拿它卡数量；填了就按实际购买力封顶。
    if math.isfinite(available_capital) and available_capital > 0:
        shares = min(shares, available_capital / price)
    return math.floor(shares / LOT) * LOT
