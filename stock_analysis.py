#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单股技术分析：只做本地计算，不负责网络请求或 UI。"""

from dataclasses import asdict
from datetime import datetime

import numpy as np
import pandas as pd

from continuation_hold_policy import (
    ContinuationHoldInput,
    assess_continuation_hold,
    continuation_hold_to_dict,
    format_continuation_hold_info,
)
from entry_structure import EntryInput, compute_entry_input, evaluate as evaluate_entry
from high_position_policy import HighPositionInput, assess_high_position, high_position_to_dict
from obv_divergence import evaluate as evaluate_obv_divergence
from price_position import PriceLocation, PricePosition
from volume_ratio import VolumeRatio, trading_minutes_elapsed
from situation_notes import situation_notes
from strong_continuation_policy import (
    StrongContinuationInput,
    assess_strong_continuation,
    strong_continuation_to_dict,
)
from volume_price import VolumePrice, VolumePriceSignal, tag as vp_tag
from trend_assessment import build_trade_permissions, trend_assessment_to_dict, assess_trend


def _find_column(frame, names):
    for name in names:
        if name in frame.columns:
            return name
    return None


def _series(frame, names):
    column = _find_column(frame, names)
    if column is None:
        return None
    return pd.to_numeric(frame[column], errors="coerce")


def _fmt_price(value):
    return f"{value:.2f}" if value < 100 else f"{value:.0f}"


def _fmt_pct(value):
    return f"{value:+.2f}%"


def _calc_indicators(close, high, low, volume):
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_hist = 2 * (dif - dea)

    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)

    lowest = low.rolling(9, min_periods=1).min()
    highest = high.rolling(9, min_periods=1).max()
    rsv = (close - lowest) / (highest - lowest).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    j = 3 * k - 2 * d

    previous_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - previous_close).abs(),
        (low - previous_close).abs(),
    ], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, adjust=False).mean()

    boll_mid = close.rolling(20, min_periods=20).mean()
    boll_std = close.rolling(20, min_periods=20).std(ddof=0)
    boll_upper = boll_mid + 2 * boll_std
    boll_lower = boll_mid - 2 * boll_std

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr14 = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / tr14.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / tr14.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx = dx.ewm(alpha=1 / 14, adjust=False).mean()

    obv_change = (volume.where(close.diff() > 0, 0) - volume.where(close.diff() < 0, 0)).tail(20).sum()
    return {
        "dif": dif, "dea": dea, "macd_hist": macd_hist,
        "rsi": rsi, "k": k, "d": d, "j": j,
        "atr": atr, "boll_mid": boll_mid, "boll_upper": boll_upper,
        "boll_lower": boll_lower, "plus_di": plus_di,
        "minus_di": minus_di, "adx": adx, "obv_change": obv_change,
    }


def _weighted_quantile(values, weights, quantile):
    """计算成交量加权分位数，用于估算筹码覆盖区间。"""
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    order = np.argsort(values)
    values = values[order]
    weights = np.maximum(weights[order], 0)
    total = weights.sum()
    if total <= 0:
        return float(np.quantile(values, quantile))
    cumulative = np.cumsum(weights)
    return float(np.interp(quantile * total, cumulative, values))


def _estimate_chip_peaks(frame, close, volume):
    """用历史收盘价和成交量估算成交密集区，不等同于真实筹码分布。"""
    sample = frame.tail(120).copy()
    prices = pd.to_numeric(sample["close"], errors="coerce")
    weights = pd.to_numeric(sample["volume"], errors="coerce").fillna(0)
    valid = prices.notna()
    prices = prices[valid]
    weights = weights[valid]
    if prices.empty:
        return {
            "avg_cost": None,
            "peaks": [],
            "chip70_lower": None,
            "chip70_upper": None,
            "chip70_width_pct": None,
            "concentration": "数据不足",
            "method": "无有效价格数据",
        }
    if weights.sum() <= 0:
        weights = pd.Series(1.0, index=prices.index)

    chip70_lower = _weighted_quantile(prices.values, weights.values, 0.15)
    chip70_upper = _weighted_quantile(prices.values, weights.values, 0.85)
    chip70_mid = (chip70_lower + chip70_upper) / 2
    chip70_width_pct = ((chip70_upper - chip70_lower) / chip70_mid * 100
                        if chip70_mid else None)
    if chip70_width_pct is None:
        concentration = "数据不足"
    elif chip70_width_pct <= 12:
        concentration = "较集中"
    elif chip70_width_pct <= 25:
        concentration = "中等"
    else:
        concentration = "较分散"
    low_price = float(prices.min())
    high_price = float(prices.max())
    if high_price <= low_price:
        return {
            "avg_cost": float(prices.iloc[-1]),
            "peaks": [(float(prices.iloc[-1]), 100.0)],
            "method": "价格无波动，无法形成峰值区间",
        }

    edges = np.linspace(low_price, high_price, 21)
    indexes = np.clip(np.searchsorted(edges, prices, side="right") - 1, 0, 19)
    buckets = np.zeros(20, dtype=float)
    for index, weight in zip(indexes, weights):
        buckets[int(index)] += float(weight)
    total = buckets.sum()
    ranked = np.argsort(buckets)[::-1]
    peaks = []
    for index in ranked[:3]:
        if buckets[index] <= 0:
            continue
        center = (edges[index] + edges[index + 1]) / 2
        peaks.append((float(center), float(buckets[index] / total * 100)))
    avg_cost = float((prices * weights).sum() / weights.sum())
    return {
        "avg_cost": avg_cost,
        "peaks": peaks,
        "chip70_lower": chip70_lower,
        "chip70_upper": chip70_upper,
        "chip70_width_pct": chip70_width_pct,
        "concentration": concentration,
        "method": "近120个交易日收盘价按成交量加权估算；70%区间取加权15%至85%分位",
    }


def analyze_history(history, code, name, live_data=None, benchmark_history=None):
    """根据历史日线和可选实时行情生成结构化技术分析结果。"""
    if history is None or len(history) < 60:
        raise ValueError("历史数据不足60个交易日，无法进行稳定分析")

    live_data = live_data or {}
    has_live_price = live_data.get("price") is not None
    frame = history.copy()
    open_price = _series(frame, ["open", "开盘", "开盘价"])
    close = _series(frame, ["close", "收盘", "收盘价"])
    high = _series(frame, ["high", "最高", "最高价"])
    low = _series(frame, ["low", "最低", "最低价"])
    volume = _series(frame, ["volume", "成交量", "成交量(股)"])
    if close is None or high is None or low is None:
        raise ValueError("历史数据缺少收盘价、最高价或最低价")
    if open_price is None:
        open_price = close.copy()
    if volume is None:
        volume = pd.Series(0.0, index=frame.index)
    # 重建 frame 前先取日期列（重建只保留 OHLCV），供 OBV 背离对照点标注；
    # 并进 frame 一起走 dropna，保证与清洗后的行对齐
    date_col = _find_column(history, ["date", "日期", "时间", "day"])
    frame = pd.DataFrame({
        "open": open_price,
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
    })
    if date_col is not None:
        frame["date"] = history[date_col].astype(str)
    frame = frame.dropna(subset=["open", "close", "high", "low"]).reset_index(drop=True)
    dates = list(frame["date"]) if "date" in frame.columns else None
    if "date" in frame.columns:
        frame = frame.drop(columns=["date"])
    if len(frame) < 60:
        raise ValueError("清洗有效历史数据不足60个交易日")

    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    volume = frame["volume"].fillna(0)
    indicators = _calc_indicators(close, high, low, volume)
    dif = indicators["dif"]
    dea = indicators["dea"]
    macd_hist = indicators["macd_hist"]
    rsi = indicators["rsi"]
    k = indicators["k"]
    d = indicators["d"]
    j = indicators["j"]
    latest = len(frame) - 1
    previous = max(0, latest - 1)
    price = float(live_data.get("price", close.iloc[latest]))
    previous_close = float(live_data.get("prev_close", close.iloc[previous]))
    today_open = float(live_data.get("open", frame["open"].iloc[latest]))
    price_change = float(live_data.get("change_pct", ((price / previous_close - 1) * 100 if previous_close else 0)))
    gap_pct = (today_open / previous_close - 1) * 100 if previous_close else 0
    open_to_current_pct = (price / today_open - 1) * 100 if today_open else 0
    if gap_pct >= 1:
        opening_type = "高开"
    elif gap_pct <= -1:
        opening_type = "低开"
    else:
        opening_type = "平开"
    if open_to_current_pct >= 0.3:
        opening_structure = f"{opening_type}后站稳/走强（现价较开盘{open_to_current_pct:+.2f}%）"
    elif open_to_current_pct <= -0.3:
        opening_structure = f"{opening_type}低走（现价较开盘{open_to_current_pct:+.2f}%）"
    else:
        opening_structure = f"{opening_type}后横盘（现价较开盘{open_to_current_pct:+.2f}%）"
    historical_volume20 = float(volume.tail(20).mean())
    live_volume = live_data.get("volume")
    volume20 = historical_volume20
    volume_ratio = float(live_volume / historical_volume20) if live_volume is not None and historical_volume20 > 0 else (float(volume.iloc[latest] / volume20) if volume20 > 0 else None)
    ma5 = float(close.tail(5).mean())
    ma10 = float(close.tail(10).mean())
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())
    recent20_low = float(low.tail(20).min())
    recent20_high = float(high.tail(20).max())

    chip = _estimate_chip_peaks(frame, close, volume)
    avg_cost = chip["avg_cost"]
    dif_v = float(dif.iloc[latest])
    dea_v = float(dea.iloc[latest])
    hist_v = float(macd_hist.iloc[latest])
    prev_hist_v = float(macd_hist.iloc[previous])
    rsi_v = float(rsi.iloc[latest]) if pd.notna(rsi.iloc[latest]) else None
    k_v = float(k.iloc[latest])
    d_v = float(d.iloc[latest])
    j_v = float(j.iloc[latest])
    atr_v = float(indicators["atr"].iloc[latest])
    boll_mid_v = float(indicators["boll_mid"].iloc[latest])
    boll_upper_v = float(indicators["boll_upper"].iloc[latest])
    boll_lower_v = float(indicators["boll_lower"].iloc[latest])
    adx_v = float(indicators["adx"].iloc[latest]) if pd.notna(indicators["adx"].iloc[latest]) else None
    plus_di_v = float(indicators["plus_di"].iloc[latest]) if pd.notna(indicators["plus_di"].iloc[latest]) else None
    minus_di_v = float(indicators["minus_di"].iloc[latest]) if pd.notna(indicators["minus_di"].iloc[latest]) else None
    obv_change_v = float(indicators["obv_change"])
    # OBV 量价背离（顶/底背离）：对照阶段高/低点当日 OBV，回答「价创新高 OBV 跟没跟上」
    obv_div = evaluate_obv_divergence(
        closes=close.tolist(),
        highs=high.tolist(),
        lows=low.tolist(),
        volumes=volume.tolist(),
        dates=dates,
        current_price=price if has_live_price else None,
    )
    obv_divergence_info = {
        "state": obv_div.state.name,
        "state_label": obv_div.state.value,
        "forming": obv_div.forming,
        "score_delta": obv_div.score_delta,
        "reason": obv_div.reason,
        "meaning": obv_div.meaning,
        "short": obv_div.short,
        "method": obv_div.method,
        "obv_confirmed_new_high": obv_div.obv_confirmed_new_high,
        "obv_confirmed_new_low": obv_div.obv_confirmed_new_low,
        "prior": None if obv_div.prior is None else {
            "date": obv_div.prior.date, "price": obv_div.prior.price,
            "obv": obv_div.prior.obv, "obv_date": obv_div.prior.obv_date,
        },
        "current": None if obv_div.current is None else {
            "date": obv_div.current.date, "price": obv_div.current.price,
            "obv": obv_div.current.obv, "obv_date": obv_div.current.obv_date,
        },
    }
    relative_strength = None
    benchmark_above_ma20 = None
    if benchmark_history is not None and len(benchmark_history) >= 21:
        benchmark_close = _series(benchmark_history, ["close", "收盘", "收盘价"])
        if benchmark_close is not None:
            benchmark_close = benchmark_close.dropna()
            if len(benchmark_close) >= 21 and len(close) >= 21:
                stock_return = close.iloc[-1] / close.iloc[-21] - 1
                benchmark_return = benchmark_close.iloc[-1] / benchmark_close.iloc[-21] - 1
                relative_strength = (stock_return - benchmark_return) * 100
                # 大盘过滤用：指数是否位于自身 MA20 上方
                if len(benchmark_close) >= 20:
                    benchmark_ma20 = float(benchmark_close.tail(20).mean())
                    benchmark_above_ma20 = float(benchmark_close.iloc[-1]) > benchmark_ma20

    # ── 入场结构输入（突破前高 / 回踩 M20 / 无结构）──
    # 统一走 entry_structure.compute_entry_input，与历史回测同一套口径；
    # 依赖 unified_volume_ratio，故放在量比计算之后
    entry_input = None
    prior_high = float(high.max())
    ma20_prev = ma20
    closes_above_prior_high = 0
    closes_below_ma20 = 0

    chip_lower = chip.get("chip70_lower")
    chip_upper = chip.get("chip70_upper")
    chip_width = chip.get("chip70_width_pct")
    if chip_lower is None or chip_upper is None:
        chip["price_position"] = "无法判断"
        chip["interpretation"] = "筹码数据不足，无法判断集中度。"
    elif price < chip_lower:
        chip["price_position"] = "位于70%筹码区下方"
        chip["interpretation"] = "筹码主要分布在上方，当前可能面临套牢压力。"
    elif price > chip_upper:
        chip["price_position"] = "位于70%筹码区上方"
        chip["interpretation"] = "价格脱离主要成交区，短线偏强。"
    else:
        chip["price_position"] = "位于70%筹码区内部"
        if chip.get("concentration") == "较集中" and price >= ma20 and obv_change_v > 0:
            chip["interpretation"] = "筹码较集中且价格/量能配合偏强，存在承接改善迹象。"
        elif chip.get("concentration") == "较集中":
            chip["interpretation"] = "筹码较集中，但价格和量能尚未共同确认方向。"
        else:
            chip["interpretation"] = "筹码区间不够集中，支撑和压力可能较分散。"

    chip["control_caution"] = "筹码集中不等于主力控盘，需结合趋势、换手和量价确认。"

    # ── 价格位置（近60日区间）──
    bars_for_position = [
        {"date": str(frame.index[i]) if hasattr(frame.index, '__getitem__') else str(i),
         "high": float(high.iloc[i]), "low": float(low.iloc[i]),
         "close": float(close.iloc[i])}
        for i in range(len(frame))
    ]
    today_str = datetime.now().strftime("%Y-%m-%d")
    price_window = PricePosition.of(bars_for_position, today=today_str)
    if price_window is not None:
        price_ctx = price_window.context_of(price)
        price_position_info = {
            "location": price_ctx.location.value if price_ctx else "中位",
            "location_key": price_ctx.location.name if price_ctx else "MID",
            "rebound_from_low": price_ctx.rebound_from_low if price_ctx else 0.0,
            "range_low": price_window.low,
            "range_high": price_window.high,
            "gain_ratio": price_window.gain_ratio(price),
        }
    else:
        price_position_info = None

    # ── 统一量比（使用 VolumeRatio 模块）──
    elapsed = trading_minutes_elapsed(datetime.now().hour, datetime.now().minute)
    avg_vol_20 = VolumeRatio.average_volume_excluding_today(
        bars_for_position, today=today_str
    )
    if live_volume is not None and avg_vol_20 is not None and avg_vol_20 > 0:
        unified_volume_ratio = VolumeRatio.of(live_volume, avg_vol_20, elapsed)
    else:
        unified_volume_ratio = volume_ratio  # fallback to original

    # ── 入场结构输入（突破前高 / 回踩 M20 / 无结构）──
    # 统一走 entry_structure.compute_entry_input，与历史回测同一套口径
    entry_input = compute_entry_input(
        closes=close.tolist(),
        highs=high.tolist(),
        lows=low.tolist(),
        volumes=volume.tolist(),
        price=price,
        volume_ratio=unified_volume_ratio,
        today_change_pct=price_change,
        holding=False,
        benchmark_ok=benchmark_above_ma20,
    )
    if entry_input is not None:
        prior_high = entry_input.prior_high
        ma20_prev = entry_input.ma20_prev
        closes_above_prior_high = entry_input.closes_above_prior_high
        closes_below_ma20 = entry_input.closes_below_ma20

    # ── 量价信号 ──
    vp_ctx = None
    if price_position_info is not None:
        vp_ctx = {
            "location": price_position_info["location_key"],
            "rebound_from_low": price_position_info["rebound_from_low"],
        }
    vp_signal = VolumePrice.of(unified_volume_ratio, price_change, vp_ctx)
    vp_tag_text = vp_tag(vp_signal)

    score = 0
    signals = []
    if price > ma20 > ma60:
        score += 2
        signals.append("价格在MA20/MA60上方，趋势偏强")
    elif price < ma20 < ma60:
        score -= 2
        signals.append("价格在MA20/MA60下方，趋势偏弱")
    else:
        signals.append("均线交错，趋势仍在整理")
    if dif_v > dea_v:
        score += 1
        signals.append("MACD DIF位于DEA上方")
    else:
        score -= 1
        signals.append("MACD DIF位于DEA下方")
    if hist_v > prev_hist_v:
        signals.append("MACD柱体较前一日改善")
    else:
        signals.append("MACD柱体较前一日走弱")
    if rsi_v is not None:
        if 45 <= rsi_v <= 70:
            score += 1
            signals.append("RSI处于中性偏强区间")
        elif rsi_v > 75:
            score -= 1
            signals.append("RSI偏高，短线有过热风险")
        elif rsi_v < 30:
            signals.append("RSI处于超卖区，等待反转确认")
    if k_v > d_v and k_v < 80:
        score += 1
        signals.append("KDJ中K位于D上方")
    elif k_v < d_v:
        score -= 1
        signals.append("KDJ中K位于D下方")
    if volume_ratio is not None:
        if price_change > 0 and volume_ratio >= 1.3:
            signals.append("上涨伴随放量，资金参与度提高")
            score += 1
        elif price_change < 0 and volume_ratio >= 1.3:
            signals.append("下跌伴随放量，需警惕承接不足")
            score -= 1
        elif abs(price_change) < 1 and volume_ratio < 0.8:
            signals.append("窄幅缩量，方向尚未确认")

    if adx_v is not None and plus_di_v is not None and minus_di_v is not None:
        if adx_v >= 25:
            if plus_di_v > minus_di_v:
                score += 1
                signals.append(f"ADX {adx_v:.1f}，趋势强度较高且多方占优")
            else:
                score -= 1
                signals.append(f"ADX {adx_v:.1f}，趋势强度较高但空方占优")
        elif adx_v < 15:
            signals.append(f"ADX {adx_v:.1f}，趋势强度较弱，震荡概率较高")
    if relative_strength is not None:
        if relative_strength > 2:
            score += 1
            signals.append(f"近20日相对沪指强{relative_strength:+.1f}个百分点")
        elif relative_strength < -2:
            score -= 1
            signals.append(f"近20日相对沪指弱{relative_strength:+.1f}个百分点")
    # OBV 背离按结论加减分（顶背离 -1 / 底背离 +1；同步与中性不加分）
    score += obv_div.score_delta
    signals.append(obv_div.short or "量价背离：暂无背离")
    if obv_change_v > 0:
        signals.append("OBV近20日净变化为正，量能方向改善（仅代表区间流向）")
    elif obv_change_v < 0:
        signals.append("OBV近20日净变化为负，量能方向偏弱（仅代表区间流向）")

    if open_to_current_pct <= -0.3:
        score -= 1
        signals.append("开盘后走弱，短线承接不足")
    elif open_to_current_pct >= 0.3:
        score += 1
        signals.append("开盘后站稳，短线承接较好")

    if score >= 4:
        action = "技术面偏强：不建议追高，优先等待回踩MA20或前高突破确认。"
    elif score >= 2:
        action = "技术面震荡偏强：可观察，不在放量冲高时追入，等待量价共振。"
    elif score <= -2:
        action = "技术面偏弱：无持仓不宜主动介入，等待重新站回MA20并出现放量确认。"
    else:
        action = "技术面中性：暂以观察为主，等待趋势和量能给出更明确方向。"

    # ── 入场结构与择时（三态：放量突破前高 / 回踩上行M20 / 暂无结构）──
    if entry_input is None:
        entry_input = EntryInput(
            price=price,
            prior_high=prior_high,
            ma20=ma20,
            ma20_prev=ma20_prev,
            volume_ratio=unified_volume_ratio,
            closes_above_prior_high=closes_above_prior_high,
            closes_below_ma20=closes_below_ma20,
            today_change_pct=price_change,
            holding=False,
            benchmark_ok=benchmark_above_ma20,
        )
    entry = evaluate_entry(entry_input)
    entry_info = {
        "state": entry.state.name,
        "state_label": entry.state_label,
        "reasons": list(entry.reasons),
        "blockers": list(entry.blockers),
        "ma20_slope": entry.ma20_slope.value,
        "volume_state": entry.volume_state.value,
        "bias_pct": entry.bias_pct,
        "bias_state": entry.bias_state,
        "trend_weak": entry.trend_weak,
        "can_buy": entry.can_buy,
        "stop_price": entry.stop_price,
        "invalid_rule": entry.invalid_rule,
        "action": entry.action,
        "prior_high": prior_high,
        "benchmark_ok": benchmark_above_ma20,
        "ma20": ma20,
        "ma20_prev": ma20_prev,
        "closes_below_ma20": closes_below_ma20,
        "closes_above_prior_high": closes_above_prior_high,
        "volume_ratio": unified_volume_ratio,
        "recent_trend": (
            asdict(entry_input.recent_trend)
            if entry_input.recent_trend is not None
            else None
        ),
    }
    trend_result = assess_trend(
        frame, price=price, now=datetime.now(), benchmark_ok=benchmark_above_ma20
    )
    high_result = assess_high_position(HighPositionInput(
        price=price,
        price_location=(price_position_info or {}).get("location_key", "MID"),
        range_width_pct=(
            ((price_position_info or {}).get("range_high", 0) - (price_position_info or {}).get("range_low", 0))
            / max((price_position_info or {}).get("range_high", 1), 1) * 100
        ) if price_position_info else None,
        atr=atr_v,
        volume_ratio=unified_volume_ratio,
        close=price,
        open=float(frame["open"].iloc[-1]) if len(frame) else None,
        bar_position=(
            (price - float(low.iloc[-1])) / max(float(high.iloc[-1]) - float(low.iloc[-1]), 1e-9)
            if len(high) and len(low) else None
        ),
        upper_shadow_atr=(
            (float(high.iloc[-1]) - price) / max(atr_v, 1e-9)
            if len(high) and atr_v else None
        ),
        lower_pivots_rising=(
            float(low.iloc[-1]) > float(low.iloc[-2])
            if len(low) >= 2 else None
        ),
        support_holds=price >= recent20_low if recent20_low else None,
        support=recent20_low,
        resistance=recent20_high,
        short_trend=trend_result.short_trend.value,
        market_ok=benchmark_above_ma20,
        sector_ok=None,
        timeframe=None,
    ))
    permissions = build_trade_permissions(
        trend_result, bool(entry.can_buy), high_result.state.value,
        benchmark_above_ma20, None, None, "buy", 0, 0,
    )
    strong_result = assess_strong_continuation(StrongContinuationInput(
        price=price,
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        ma20_prev=entry_input.ma20_prev,
        price_location=(price_position_info or {}).get("location_key", "MID"),
        bias_pct=entry.bias_pct,
        breakout_reference=prior_high,
        volume_ratio=unified_volume_ratio,
        lower_pivots_rising=(
            float(low.iloc[-1]) > float(low.iloc[-2])
            if len(low) >= 2 else None
        ),
        support_holds=price >= recent20_low if recent20_low else None,
        market_ok=benchmark_above_ma20,
        sector_ok=None,
    ))
    strong_info = strong_continuation_to_dict(strong_result)
    hold_result = assess_continuation_hold(ContinuationHoldInput(
        strong_state=strong_info["state"],
        high_state=high_result.state.name,
        risk_line=strong_info.get("risk_line"),
        risk_price=strong_result.risk_price,
        bias_pct=entry.bias_pct,
        volume_ratio=unified_volume_ratio,
        first_wave=entry.state.name in {"BREAKOUT", "PULLBACK_M20"},
        timeframe_ok=None,
    ))
    hold_info = continuation_hold_to_dict(hold_result)
    entry_info["trend_assessment"] = trend_assessment_to_dict(trend_result)
    entry_info["high_position"] = high_position_to_dict(high_result)
    entry_info["strong_continuation"] = strong_info
    entry_info["continuation_hold"] = hold_info
    entry_info["trade_permissions"] = {
        "allow_normal_buy": permissions.allow_normal_buy,
        "allow_add": permissions.allow_add,
        "allow_reentry": permissions.allow_reentry,
        "allow_t": permissions.allow_t,
        "allow_probe": permissions.allow_probe,
        "risk_only": permissions.risk_only,
        "blockers": list(permissions.blockers),
    }
    # 操作建议与入场结构对齐：结构优先于笼统的评分话术
    if entry.state.name != "NO_STRUCTURE" or entry.trend_weak:
        action = entry.action
    elif score >= 4:
        action = "技术面偏强但暂未形成有效入场结构：不追高，等回踩上行M20或放量突破前高。"
    elif score >= 2:
        action = "技术面震荡偏强但暂未形成有效入场结构：以观察为主，等入场结构出现。"
    elif score <= -2:
        action = "技术面偏弱且无入场结构：无持仓不宜主动介入，等重新站回MA20并放量确认。"
    else:
        action = "技术面中性且无入场结构：暂以观察为主，等待趋势和量能给出更明确方向。"

    # 数据日期必须如实记录，不能拿 datetime.now() 顶替 ——
    # 2026-10-12 实测：sh600093 的新浪日线停在 2022-06-16（落后 1579 天），
    # 而报告抬头写「数据截至：2026-10-12」，把 2022 年的价当今天的分析。
    # as_of 仍是「生成时刻」，数据截止看 data_as_of。
    data_as_of = (dates[-1] if dates else "") or ""
    data_age_days = None
    if data_as_of:
        try:
            data_age_days = max(0, (datetime.now().date()
                                    - pd.Timestamp(str(data_as_of)[:10]).date()).days)
        except Exception:
            data_age_days = None

    return {
        "code": code,
        "name": name,
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_as_of": str(data_as_of)[:10],
        "data_age_days": data_age_days,
        "rows": len(frame),
        "price": price,
        "price_source": "实时行情" if has_live_price else "历史日线收盘价（非实时）",
        "price_change": price_change,
        "opening": {
            "previous_close": previous_close,
            "open": today_open,
            "gap_pct": gap_pct,
            "open_to_current_pct": open_to_current_pct,
            "type": opening_type,
            "structure": opening_structure,
        },
        "volume_ratio": volume_ratio,
        "unified_volume_ratio": unified_volume_ratio,
        "volume_price_signal": vp_signal.name,
        "volume_price_tag": vp_tag_text,
        "price_position": price_position_info,
        "volatility": {
            "atr": atr_v,
            "atr_pct": atr_v / price * 100 if price else None,
            "boll_mid": boll_mid_v,
            "boll_upper": boll_upper_v,
            "boll_lower": boll_lower_v,
            "boll_position": ((price - boll_lower_v) / (boll_upper_v - boll_lower_v) * 100
                              if boll_upper_v > boll_lower_v else None),
        },
        "trend_strength": {
            "adx": adx_v,
            "plus_di": plus_di_v,
            "minus_di": minus_di_v,
        },
        "obv_change": obv_change_v,
        "obv_divergence": obv_divergence_info,
        "relative_strength": relative_strength,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "support": recent20_low,
        "resistance": recent20_high,
        "macd": {"dif": dif_v, "dea": dea_v, "hist": hist_v, "previous_hist": prev_hist_v},
        "rsi": rsi_v,
        "kdj": {"k": k_v, "d": d_v, "j": j_v},
        "chip": chip,
        "score": score,
        "signals": signals,
        "action": action,
        "entry_structure": entry_info,
        "continuation_hold": hold_info,
    }


# 数据落后多少天算「不新鲜」。节假日/周末 1-3 天属正常，这里只标明显过期的。
STALE_DATA_DAYS = 10


def _data_line(result) -> str:
    """报告的数据截止行。

    「数据截至」必须是**数据自己的最后一天**，不能是生成时刻 ——
    2026-10-12 实测 sh600093 的日线停在 2022-06-16，报告却写「数据截至：2026-10-12」，
    把 2022 年的价当今天的行情分析。
    """
    data_as_of = str(result.get("data_as_of") or "").strip()
    age = result.get("data_age_days")
    rows = result.get("rows", 0)
    if not data_as_of:
        return f"数据截至：{result.get('as_of', '')}｜有效日线：{rows}个交易日"
    base = f"数据截至：{data_as_of}｜有效日线：{rows}个交易日"
    if age is not None and age > STALE_DATA_DAYS:
        return (
            f"{base}｜⚠️ 数据已落后 {age} 天（个股可能停牌/退市/数据源异常），"
            f"下面的分析是 {data_as_of} 的行情，不是今天的"
        )
    return f"{base}｜生成于 {result.get('as_of', '')}"


def format_report(result):
    """将结构化分析结果格式化为用户可读报告。"""
    volume_text = "暂无成交量"
    if result["volume_ratio"] is not None:
        volume_text = f"最近一日/20日均量 {result['volume_ratio']:.2f} 倍"
    vp_tag_text = result.get("volume_price_tag", "")
    if vp_tag_text:
        volume_text += f"  {vp_tag_text}"
    price_pos = result.get("price_position")
    if price_pos:
        volume_text += f"  位置：{price_pos['location']}"
    macd = result["macd"]
    kdj = result["kdj"]
    chip = result["chip"]
    peak_text = "、".join(f"{_fmt_price(price)}附近({pct:.1f}%)" for price, pct in chip["peaks"])
    if not peak_text:
        peak_text = "暂无明显密集区"
    rsi_text = f"{result['rsi']:.1f}" if result["rsi"] is not None else "暂缺"
    # 各指标的「含义」只留**这只股的状态**，砍掉教科书释义与告诫
    # （「ATR表示波动幅度，不判断涨跌方向」「不代表一定立即下跌」这类
    # 每次打开任何股票都一字不差）。2026-10-12：两只差异很大的股，
    # 10 行里 6 行完全相同，就是这些尾巴造成的。
    volume_ratio = result["volume_ratio"]
    if volume_ratio is None:
        volume_meaning = "量比暂缺"
    elif volume_ratio >= 1.3:
        volume_meaning = "量能明显放大"
    elif volume_ratio <= 0.8:
        volume_meaning = "量能低于近期均值"
    else:
        volume_meaning = "量能接近近期均值"
    if result["price"] > result["ma20"] > result["ma60"]:
        ma_meaning = "价格位于 MA20、MA60 上方"
    elif result["price"] < result["ma20"] < result["ma60"]:
        ma_meaning = "价格位于 MA20、MA60 下方"
    else:
        ma_meaning = "均线交错，方向未定"
    volatility = result["volatility"]
    if volatility["boll_upper"] > volatility["boll_lower"]:
        boll_pos = volatility.get("boll_position")
        boll_meaning = "靠近布林上轨" if boll_pos and boll_pos >= 80 else (
            "靠近布林下轨" if boll_pos and boll_pos <= 20 else "位于布林中部"
        )
    else:
        boll_meaning = "布林带区间暂不可用"
    adx = result["trend_strength"].get("adx")
    if adx is None:
        adx_meaning = "ADX 暂缺"
    elif adx >= 25:
        adx_meaning = "ADX 偏高，有趋势"
    elif adx < 15:
        adx_meaning = "ADX 偏低，更像震荡"
    else:
        adx_meaning = "ADX 过渡区"
    if result["relative_strength"] is None:
        relative_meaning = "缺沪指基准"
    elif result["relative_strength"] > 2:
        relative_meaning = "强于沪指"
    elif result["relative_strength"] < -2:
        relative_meaning = "弱于沪指"
    else:
        relative_meaning = "与沪指接近"
    macd_meaning = "柱体改善" if macd["hist"] > macd["previous_hist"] else "柱体走弱"
    kdj_meaning = "K>D" if kdj["k"] > kdj["d"] else "K<D"
    if result["rsi"] is None:
        rsi_meaning = "RSI 暂缺"
    elif result["rsi"] >= 70:
        rsi_meaning = "RSI 偏高"
    elif result["rsi"] <= 30:
        rsi_meaning = "RSI 偏低"
    else:
        rsi_meaning = "RSI 中性区间"
    obv_div = result.get("obv_divergence") or {}
    obv_pair = obv_div.get("prior"), obv_div.get("current")
    if all(obv_pair):
        prior_pt, current_pt = obv_pair
        obv_pair_text = (
            f"阶段高/低点对照：前点 {_fmt_price(prior_pt['price'])}（{prior_pt['date']}，"
            f"段内OBV峰值 {prior_pt['obv']:+.0f}）→ 后点 {_fmt_price(current_pt['price'])}（{current_pt['date']}，"
            f"段内OBV峰值 {current_pt['obv']:+.0f}）"
        )
    else:
        obv_pair_text = "阶段高/低点对照：暂无两个可比的阶段高/低点"
    obv_judge_text = f"量价背离判定：{obv_div.get('reason', '数据不足 —— 有效日线不足，无法判定背离')}"
    obv_meaning_text = f"含义：{obv_div['meaning']}" if obv_div.get("meaning") else "含义：有效日线不足，暂无背离含义可给。"
    obv_method_text = f"口径：{obv_div.get('method', '')}"
    entry_info = result.get("entry_structure") or {
        "state_label": "暂未形成有效入场结构",
        "prior_high": result.get("resistance", 0),
        "ma20_slope": "暂缺",
        "volume_state": "暂缺",
        "bias_pct": 0.0,
        "bias_state": "正常",
        "reasons": [],
        "blockers": ["入场结构数据暂缺"],
        "invalid_rule": "",
        "stop_price": None,
        "benchmark_ok": None,
        "action": "暂以观察为主。",
    }
    hold_info = result.get("continuation_hold") or entry_info.get("continuation_hold") or {}

    # 今日情境：把指标读数翻成「这只股现在发生了什么」。
    # 个股分析是「无持仓假设」，不传浮亏/深套口径。
    situation = situation_notes(
        vp_signal=VolumePriceSignal[result["volume_price_signal"]]
        if result.get("volume_price_signal") in VolumePriceSignal.__members__ else None,
        relative_strength=result.get("relative_strength"),
        atr_pct=(result.get("volatility") or {}).get("atr_pct"),
        location=(result.get("price_position") or {}).get("location_key"),
    )
    situation_text = "；".join(situation) if situation else "无特别情境"

    lines = [
        f"📈 {result['name']}（{result['code']}）技术分析",
        _data_line(result),
        "",
        "一、综合结论",
        f"技术评分：{result['score']:+d}｜当前价：{_fmt_price(result['price'])}（{result['price_source']}）｜较前一日：{_fmt_pct(result['price_change'])}",
        f"今日情境：{situation_text}",
        f"入场结构：{entry_info['state_label']}",
        (
            f"持有策略：{hold_info.get('state_label') or hold_info.get('state')}｜"
            f"{format_continuation_hold_info(hold_info)}"
            if hold_info else "持有策略：证据不足"
        ),
        f"操作建议（无持仓假设）：{result['action']}",
        "",
        "二、量价关系",
        f"{volume_text}",
        f"状态：{volume_meaning}",
        "；".join(result["signals"][-3:]),
        "",
        "三、开盘结构",
        f"昨收 {_fmt_price(result['opening']['previous_close'])}｜今开 {_fmt_price(result['opening']['open'])}｜跳空 {result['opening']['gap_pct']:+.2f}%",
        f"当前价相对开盘 {result['opening']['open_to_current_pct']:+.2f}%｜判断：{result['opening']['structure']}",
        "",
        "四、均线与趋势",
        f"MA5 {_fmt_price(result['ma5'])}｜MA10 {_fmt_price(result['ma10'])}｜MA20 {_fmt_price(result['ma20'])}｜MA60 {_fmt_price(result['ma60'])}",
        f"近20日支撑参考：{_fmt_price(result['support'])}｜压力参考：{_fmt_price(result['resistance'])}",
        f"状态：{ma_meaning}",
        "",
        "五、波动与趋势强度",
        f"ATR14 {_fmt_price(result['volatility']['atr'])}（约占现价{result['volatility']['atr_pct']:.2f}%）",
        f"布林带：下轨 {_fmt_price(result['volatility']['boll_lower'])}｜中轨 {_fmt_price(result['volatility']['boll_mid'])}｜上轨 {_fmt_price(result['volatility']['boll_upper'])}",
        f"状态：{boll_meaning}",
        f"ADX {result['trend_strength']['adx']:.1f}｜+DI {result['trend_strength']['plus_di']:.1f}｜-DI {result['trend_strength']['minus_di']:.1f}" if result['trend_strength']['adx'] is not None else "ADX数据暂缺",
        f"状态：{adx_meaning}",
        f"相对沪指（近20日）：{result['relative_strength']:+.2f}个百分点（{relative_meaning}）" if result['relative_strength'] is not None else "相对沪指：数据暂缺",
        f"OBV近20日净变化：{result['obv_change']:+.0f}（仅表示区间累计资金流向，不等于跟上最新股价高点）",
        obv_pair_text,
        obv_judge_text,
        obv_meaning_text,
        obv_method_text,
        "",
        "六、MACD",
        f"DIF {macd['dif']:.3f}｜DEA {macd['dea']:.3f}｜柱体 {macd['hist']:.3f}（前值 {macd['previous_hist']:.3f}）",
        f"判断：{'DIF在DEA上方' if macd['dif'] > macd['dea'] else 'DIF在DEA下方'}｜{macd_meaning}",
        "",
        "七、KDJ / RSI",
        f"K {kdj['k']:.1f}｜D {kdj['d']:.1f}｜J {kdj['j']:.1f}｜RSI {rsi_text}",
        f"状态：{kdj_meaning}｜{rsi_meaning}",
        "",
        "八、筹码峰结构（估算）",
        f"70%筹码区间：{_fmt_price(chip['chip70_lower'])}～{_fmt_price(chip['chip70_upper'])}" if chip['chip70_lower'] is not None else "70%筹码区间：暂缺",
        f"集中度区间宽度：{chip['chip70_width_pct']:.2f}%（{chip['concentration']}；数值越小表示区间越集中）" if chip['chip70_width_pct'] is not None else "集中度区间宽度：暂缺",
        f"当前价格位置：{chip.get('price_position', '暂缺')}",
        f"分析结论：{chip.get('interpretation', '暂缺')}",
        f"控盘提示：{chip.get('control_caution', '筹码集中不等于主力控盘。')}",
        f"估算成本中枢：{_fmt_price(chip['avg_cost']) if chip['avg_cost'] is not None else '暂缺'}",
        f"成交密集区：{peak_text}",
        "",
        "九、入场结构与择时",
        f"当前状态：{entry_info['state_label']}",
        f"前高参考：{_fmt_price(entry_info['prior_high'])}｜M20 方向：{entry_info['ma20_slope']}"
        f"｜量能：{entry_info['volume_state']}｜乖离：{entry_info['bias_pct']:+.2f}%（{entry_info['bias_state']}）",
        "成立依据：" + ("；".join(entry_info["reasons"]) if entry_info["reasons"] else "暂无"),
        "未满足/失效：" + ("；".join(entry_info["blockers"]) if entry_info["blockers"] else "无"),
        f"失效定义：{entry_info['invalid_rule'] or '当前无入场结构，不涉及失效位'}",
        f"止损参考位：{_fmt_price(entry_info['stop_price']) if entry_info['stop_price'] else '暂无'}",
        f"大盘过滤：{'指数位于 M20 上方，信号可信度正常' if entry_info['benchmark_ok'] else ('指数位于 M20 下方，个股信号降级为仅观察' if entry_info['benchmark_ok'] is False else '指数数据暂缺，未做大盘过滤')}",
        f"应对：{entry_info['action']}",
        "",
        "十、综合行动计划",
        f"综合判断：{result['action']}",
        f"上方突破：有效站上{_fmt_price(entry_info['prior_high'])}并放量，且 M20 不下行，才算「放量突破前高」；",
        f"回踩买点：M20 上行时回落到 {_fmt_price(result['ma20'])} 附近且缩量、未有效跌破，才算「回踩上行 M20」；",
        f"下方风险：有效跌破 M20（连续2日收于 {_fmt_price(result['ma20'])} 下方或单日破位2%）→ 趋势转弱观察，不等同于立即清仓；",
        f"突破失效：跌回前高 {_fmt_price(entry_info['prior_high'])} 下方2%或2日内收回，突破失效离场；",
        "",
        "说明：入场三态只描述「当前是否具备可执行的入场结构」，不代表涨跌概率，无结构时观望本身就是操作；"
        "先看入场结构是否成立（放量突破前高 / 回踩上行 M20），再看量价配合，不要因单一指标金叉就追入。",
        "筹码峰为成交量加权估算，不是券商真实持仓筹码分布；集中度判定：≤12%较集中、12%～25%中等、>25%较分散"
        "（本程序内部口径，不与其他软件直接比较）；成交密集区仅作潜在支撑/压力参考，不是必然支撑。",
        "⚠️ 本报告基于历史行情技术计算，任何单一指标都不构成买卖依据，不构成投资建议。",
    ]
    return "\n".join(lines)
