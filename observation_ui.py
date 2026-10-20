"""观察池展示模型：突出已触发买点，区分候选与观察，不参与信号判定。"""

from datetime import datetime
import math


def _today_alert(record, at):
    alert = record.get("last_entry_alert") or {}
    try:
        stamp = datetime.fromisoformat(alert["at"])
        price = float(alert["price"])
        if (alert["date"] == at.date().isoformat() == stamp.date().isoformat()
                and stamp <= at and alert["code"] == record["code"]
                and alert["first_observed_date"] == record["first_observed_date"]
                and math.isfinite(price) and price > 0):
            return alert
    except (ValueError, TypeError, KeyError):
        pass
    return None


def _price(value):
    try:
        return f"{float(value):.2f}" if math.isfinite(float(value)) and float(value) > 0 else "--"
    except (TypeError, ValueError):
        return "--"


def build_observation_view(pool, at=None):
    at = at or datetime.now()
    view = {"focus_lines": [], "badges": {}, "ranks": {}, "buy_count": 0,
            "ready_count": 0, "has_pool": pool is not None}
    if pool is None:
        return view
    if getattr(pool, "load_error", None):
        view["focus_lines"] = [f"⚠️ 买点状态不可用：{pool.load_error}"]
        return view
    buys, ready, withdrawn = [], [], []
    observing = 0
    labels = {"eliminated": "已淘汰", "expired": "观察到期"}
    for record in list(pool.records.values()):
        code = record["code"]
        alert = _today_alert(record, at)
        active = record.get("status") in {"observing", "ready"}
        reviewed = record.get("data_status") == "已复核"
        if active and reviewed and alert:
            badge, rank = f"📋【今日曾提醒 {alert['at'][11:16]}·非当前买点】", 0
            buys.append((record, alert))
        elif not active or not reviewed:
            state = labels.get(record.get("status"), "待复核")
            badge, rank = f"⚠️【{state}】", 3
            if alert:
                withdrawn.append(f"⚠️ 今日曾触发 {record['name']}({code})｜现{state}，请重新确认")
        elif record["status"] == "ready":
            badge, rank = "🟡【日线候选·等待回踩确认】", 1
            ready.append(record)
        else:
            badge, rank = "⚪【观察中】", 2
            observing += 1
        view["badges"][code] = badge
        view["ranks"][code] = rank
    view.update(buy_count=len(buys), ready_count=len(ready))
    lines = [f"📋 今日提醒记录 · {len(buys)}只（非当前买点）"]
    for record, alert in sorted(buys, key=lambda pair: (pair[1]["at"], pair[0]["code"]), reverse=True):
        lines.append(f"📋 {record['name']}({record['code']}) | {alert['at'][11:16]}触发{_price(alert['price'])} | 历史失效参考{_price(alert.get('stop_price'))}")
    if not buys:
        lines.append("⚪ 暂无今日盘中提醒记录")
    lines.append("记录仅表示当时发送过提醒，并非当前仍可买入；以触发时间为准")
    lines.extend(withdrawn)
    lines.append("涨停附近只观察、不发新买点提醒；回落后须重新确认结构与承接，不是开板即买点")
    lines.append(f"🟡 日线候选 · {len(ready)}只（待盘中确认，不代表现在可买）")
    for record in sorted(ready, key=lambda item: (item.get("last_reviewed_date") or "", item["code"]), reverse=True)[:8]:
        lines.append(f"🟡 {record['name']}({record['code']}) | 等待回踩确认 | 日线{record.get('last_reviewed_date') or '--'}")
    if len(ready) > 8:
        lines.append(f"其余{len(ready) - 8}只候选见「🔎 持续观察池」，尚未触发盘中买点")
    lines.append(f"⚪ 观察中 · {observing}只｜筛选命中不等于买点")
    view["focus_lines"] = lines
    return view


def prioritize_stocks(rows, view):
    return sorted(rows, key=lambda stock: view["ranks"].get(stock.get("code"), 2))


def decorate_stock(text, stock, view):
    if not view["has_pool"]:
        return text
    badge = view["badges"].get(stock.get("code"), "⚪【筛选命中·未确认】")
    return f"{badge} {text}"


def indicator_color(text):
    if text.startswith("📋"):
        return "gray"
    if text.startswith("🎯"):
        return "red"
    if text.startswith("🟡"):
        return "blue"
    if text.startswith(("⚪", "⚠️")) or any(indicator in text for indicator in ("RSI", "KDJ", "MACD")):
        return "gray"
    return "white"
