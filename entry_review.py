"""独立买点复盘计算；所有成交为固定参数下的日线假设。"""

from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import math


STRATEGY_VERSION = "observation-pullback-v1"
DEFAULT_POLICY = {
    "version": "daily-paper-v1", "budget": 10000.0, "lot_size": 100,
    "slippage": .001, "commission_rate": .00025, "minimum_commission": 5.0,
    "sell_tax_rate": .0005, "transfer_rate": .00001,
    "max_entry_gap": .03, "reward_risk": 2.0, "exit_day": 10,
}
HORIZONS = (1, 3, 5, 10)


def number(value, positive=True):
    value = float(value)
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError("价格或参数不是有效的有限数值")
    return value


def money(value, rounding=ROUND_HALF_UP):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=rounding))


def validate_event(event):
    number(event["price"])
    stop = number(event["stop_price"])
    if stop >= event["price"]:
        raise ValueError("失效价必须低于触发价")
    number(event["evidence"]["benchmark_price"])
    if datetime.fromisoformat(event["at"]).date().isoformat() != event["date"]:
        raise ValueError("买点时间与日期不匹配")
    policy = event["policy"]
    if policy["version"] != DEFAULT_POLICY["version"]:
        raise ValueError("不支持的模拟规则版本")
    for key in ("budget", "lot_size", "reward_risk", "exit_day"):
        number(policy[key])
    for key in ("slippage", "commission_rate", "minimum_commission", "sell_tax_rate",
                "transfer_rate", "max_entry_gap"):
        number(policy[key], positive=False)
    if any(policy[key] >= 1 for key in ("slippage", "commission_rate", "sell_tax_rate",
                                       "transfer_rate", "max_entry_gap")):
        raise ValueError("模拟费率或滑点必须小于1")
    if policy["lot_size"] != int(policy["lot_size"]) or policy["exit_day"] != 10:
        raise ValueError("模拟手数或持有期限无效")


def completed_rows(rows, at):
    result = {}
    for row in rows:
        date = str(row["date"])[:10]
        day = datetime.strptime(date, "%Y-%m-%d")
        if day.weekday() >= 5 or date > at.date().isoformat():
            continue
        if date == at.date().isoformat() and at.hour < 15:
            continue
        values = {key: number(row[key], positive=key != "volume")
                  for key in ("open", "close", "high", "low", "volume")}
        if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
            raise ValueError("日线高低价不一致")
        item = dict(values, date=date)
        if date in result and item != result[date]:
            raise ValueError("同一天存在冲突日线")
        result[date] = item
    return result


def fee(gross, side, policy):
    commission = money(max(policy["minimum_commission"], gross * policy["commission_rate"]))
    transfer = money(gross * policy["transfer_rate"])
    tax = money(gross * policy["sell_tax_rate"]) if side == "sell" else 0
    return money(commission + transfer + tax)


def _fill_price(reference, side, policy):
    factor = 1 + policy["slippage"] if side == "buy" else 1 - policy["slippage"]
    return money(reference * factor, ROUND_CEILING if side == "buy" else ROUND_FLOOR)


def _simulate(event, stock, sessions):
    policy = event["policy"]
    result = {"status": "pending", "reason": "等待下一个完整交易日", "trades": [],
              "initial_cash": policy["budget"], "cash": policy["budget"], "quantity": 0,
              "realized_pnl": 0.0, "floating_pnl": None, "return_pct": None,
              "execution_model": "日线假设成交"}
    if not sessions:
        return result
    entry = stock.get(sessions[0])
    if not entry or entry["volume"] == 0:
        return dict(result, status="pending_data", reason="下一交易日个股日线缺失/停牌，未假设成交")
    price = _fill_price(entry["open"], "buy", policy)
    if (not entry["low"] < entry["open"] < entry["high"] or price > entry["high"]
            or abs(Decimal(str(entry["open"])) - Decimal(str(event["price"])))
            > Decimal(str(event["price"])) * Decimal(str(policy["max_entry_gap"]))
            or entry["open"] <= event["stop_price"]):
        return dict(result, status="unfilled", reason="开盘区间/偏离幅度/失效价不满足固定模拟条件")
    lot_size = int(policy["lot_size"])
    quantity = int(policy["budget"] / (price * lot_size)) * lot_size
    while quantity > 0 and price * quantity + fee(price * quantity, "buy", policy) > policy["budget"]:
        quantity -= lot_size
    if not quantity:
        return dict(result, status="unfilled", reason="独立模拟资金不足一手及费用")
    buy_fee = fee(price * quantity, "buy", policy)
    cost = money(price * quantity + buy_fee)
    result.update(status="open", reason="等待退出条件", cash=money(policy["budget"] - cost),
                  quantity=quantity, floating_pnl=money(entry["close"] * quantity - cost))
    result["trades"].append(dict(side="buy", date=sessions[0], price=price, quantity=quantity,
                                 fee=buy_fee, reason="next_open"))
    stop = event["stop_price"]
    target = price + policy["reward_risk"] * (price - stop)
    pending_exit = None
    previous_close = entry["close"]
    for offset, date in enumerate(sessions[1:], 2):
        daily = stock.get(date)
        if not daily or daily["volume"] == 0:
            return dict(result, status="needs_review", reason=f"{date}持仓期间日线缺失/停牌，退出结果待核对")
        if abs(daily["open"] / previous_close - 1) > .25:
            return dict(result, status="needs_review", reason=f"{date}价格不连续，需核对除权或异常行情")
        result["floating_pnl"] = money(daily["close"] * quantity - cost)
        if pending_exit:
            reason, reference = pending_exit, daily["open"]
        elif previous_close <= stop or daily["low"] <= stop:
            reason = "stop"
            reference = daily["open"] if previous_close <= stop else min(stop, daily["open"])
        elif daily["high"] >= target:
            reason, reference = "target", max(target, daily["open"])
        elif offset >= policy["exit_day"]:
            reason, reference = "time_exit", daily["close"]
        else:
            previous_close = daily["close"]
            continue
        sell_price = _fill_price(reference, "sell", policy)
        if daily["high"] == daily["low"] or not daily["low"] <= sell_price <= daily["high"]:
            pending_exit = reason
            result["reason"] = f"{date}退出价不可验证，等待后续有效日线开盘"
            previous_close = daily["close"]
            continue
        sell_fee = fee(sell_price * quantity, "sell", policy)
        proceeds = money(sell_price * quantity - sell_fee)
        pnl = money(proceeds - cost)
        result["trades"].append(dict(side="sell", date=date, price=sell_price, quantity=quantity,
                                     fee=sell_fee, reason=reason))
        result.update(status="closed", reason=reason, cash=money(result["cash"] + proceeds),
                      quantity=0, realized_pnl=pnl, floating_pnl=0,
                      return_pct=pnl / policy["budget"] * 100)
        return result
    return result


def evaluate_event(event, stock_bars, benchmark_bars, at):
    validate_event(event)
    stock = completed_rows(stock_bars, at)
    benchmark = {date: daily for date, daily in completed_rows(benchmark_bars, at).items()
                 if daily["volume"] > 0}
    anchor_present = event["date"] in benchmark
    sessions = sorted(date for date in benchmark if date > event["date"]) if anchor_present else []
    calendar_gaps = sorted(date for date, daily in stock.items()
                           if daily["volume"] > 0 and event["date"] < date <= max(benchmark, default="")
                           and date not in benchmark)
    if calendar_gaps:
        sessions = [date for date in sessions if date < calendar_gaps[0]]
    price_gap_date = None
    previous_close = stock.get(event["date"], {}).get("close", event["price"])
    for date in sessions:
        daily = stock.get(date)
        if not daily or daily["volume"] == 0:
            continue
        if abs(daily["open"] / previous_close - 1) > .25:
            price_gap_date = date
            break
        previous_close = daily["close"]
    horizons = {}
    for horizon in HORIZONS:
        item = {"status": "pending", "date": None, "return_pct": None,
                "benchmark_pct": None, "excess_pct": None}
        if len(sessions) >= horizon:
            date = sessions[horizon - 1]
            item.update(date=date, status="missing")
            item["benchmark_pct"] = (benchmark[date]["close"] / event["evidence"]["benchmark_price"] - 1) * 100
            if price_gap_date and date >= price_gap_date:
                item["status"] = "needs_review"
            elif date in stock and stock[date]["volume"] > 0:
                item["return_pct"] = (stock[date]["close"] / event["price"] - 1) * 100
                item["excess_pct"] = item["return_pct"] - item["benchmark_pct"]
                item["status"] = "available"
        horizons[str(horizon)] = item
    observed = [stock[date] for date in sessions[:10] if date in stock and stock[date]["volume"] > 0
                and (price_gap_date is None or date < price_gap_date)]
    safe_sessions = [date for date in sessions if price_gap_date is None or date < price_gap_date]
    simulation = _simulate(event, stock, safe_sessions)
    quality_warning = f"{price_gap_date}价格不连续，之后的收益与极值暂不纳入统计，需核对除权或异常行情" if price_gap_date else None
    if price_gap_date and simulation["status"] not in {"closed", "unfilled"}:
        simulation.update(status="needs_review" if simulation["quantity"] else "pending_data", reason=quality_warning)
    status = "complete" if len(sessions) >= 10 else "tracking"
    if not anchor_present:
        status = "pending_data"
        simulation.update(status="pending_data", reason="缺少触发日指数日线，无法确定后续交易日序号")
    elif calendar_gaps:
        status = "pending_data"
        if simulation["status"] not in {"closed", "unfilled"}:
            simulation.update(status="pending_data", reason=f"{calendar_gaps[0]}指数日线缺口，后续交易日序号待核对")
    elif len(observed) < min(len(sessions), 10):
        status = "pending_data"
    return {"status": status, "as_of_date": max(benchmark) if benchmark else None,
            "quality_warning": quality_warning,
            "horizons": horizons, "observed_sessions": len(observed),
            "expected_sessions": min(len(sessions), 10),
            "max_favorable_pct": max(0, (max(row["high"] for row in observed) / event["price"] - 1) * 100) if observed else None,
            "max_adverse_pct": min(0, (min(row["low"] for row in observed) / event["price"] - 1) * 100) if observed else None,
            "simulation": simulation}
