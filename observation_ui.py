"""观察池展示模型：突出已触发买点，区分候选与观察，不参与信号判定。"""

from datetime import datetime
import math
from limit_up_policy import rank_limit_down, rank_limit_up


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


def _state_for(record):
    state = record.get("state")
    if state in {"OBSERVING", "NEAR_ENTRY", "BUY_CANDIDATE", "ELIMINATED"}:
        return state
    return {
        "ready": "NEAR_ENTRY",
        "expired": "ELIMINATED",
        "eliminated": "ELIMINATED",
    }.get(record.get("status"), "OBSERVING")


def _canonical_destination(strategy):
    return {
        "PRE_BREAKOUT": "PRE_BREAKOUT / 启动前潜力",
        "LEADER": "LEADER / 强势龙头",
        "LIMIT_UP_CONTINUATION": "LIMIT_UP_CONTINUATION / 涨停持续观察",
    }.get(strategy, f"{strategy} / 未分类")


def _item_for(record, at):
    state = _state_for(record)
    strategy = record.get("strategy") or (
        "LIMIT_UP_CONTINUATION" if "limit" in record.get("sources", [])
        else "LEADER" if "strong" in record.get("sources", [])
        else "PRE_BREAKOUT"
    )
    destination = record.get("entry_destination") or {
        "PRE_BREAKOUT": "PRE_BREAKOUT / 启动前潜力",
        "LEADER": "LEADER / 强势龙头",
        "LIMIT_UP_CONTINUATION": "LIMIT_UP_CONTINUATION / 涨停持续观察",
    }[strategy]
    return {
        "code": record.get("code"),
        "name": record.get("name") or record.get("code"),
        "state": state,
        "strategy": strategy,
        "entry_destination": destination,
        "score": record.get("score", 0),
        "price": record.get("price"),
        "stop_price": record.get("stop_price"),
        "risk_line": record.get("risk_line"),
        "risk_price": record.get("risk_price"),
        "flow_window": record.get("flow_window") or [],
        "structure_evidence": record.get("structure") or record.get("reason"),
        "risk_evidence": record.get("risk_evidence") or [],
        "reason": record.get("reason"),
        "last_reviewed_date": record.get("last_reviewed_date"),
        "expanded": False,
    }


def build_observation_view(pool, at=None, page=1, expanded=False):
    at = at or datetime.now()
    view = {
        "focus_lines": [], "badges": {}, "ranks": {}, "buy_count": 0,
        "ready_count": 0, "has_pool": pool is not None, "items": [],
        "groups": {}, "page": max(1, int(page)), "page_count": 1,
        "expanded": bool(expanded),
    }
    if pool is None:
        return view
    if getattr(pool, "load_error", None):
        view["focus_lines"] = [f"⚠️ 买点状态不可用：{pool.load_error}"]
        return view
    buys, ready, withdrawn, grouped = [], [], [], {}
    observing = 0
    labels = {"eliminated": "已淘汰", "expired": "观察到期"}
    active_items = []
    for record in list(pool.records.values()):
        code = record["code"]
        alert = _today_alert(record, at)
        state = _state_for(record)
        active = state != "ELIMINATED"
        reviewed = record.get("data_status", "已复核") == "已复核"
        item = _item_for(record, at)
        if active and reviewed and alert:
            badge, rank = f"📋【今日曾提醒 {alert['at'][11:16]}·非当前买点】", 0
            buys.append((record, alert))
        elif not active or not reviewed:
            state_label = labels.get(record.get("status"), "待复核")
            badge, rank = f"⚠️【{state_label}】", 4
            if alert:
                withdrawn.append(f"⚠️ 今日曾触发 {record['name']}({code})｜现{state_label}，请重新确认")
        elif state == "BUY_CANDIDATE":
            badge, rank = "🎯【买点已确认】", 1
            active_items.append(item)
            grouped.setdefault(_canonical_destination(item["strategy"]), []).append(item)
        elif state == "NEAR_ENTRY":
            badge, rank = "🟡【日线候选·等待回踩确认】", 2
            ready.append(record)
            active_items.append(item)
            grouped.setdefault(_canonical_destination(item["strategy"]), []).append(item)
        else:
            badge = "⚪【涨停持续观察】" if item["strategy"] == "LIMIT_UP_CONTINUATION" else "⚪【观察中】"
            rank = 3
            observing += 1
            active_items.append(item)
            grouped.setdefault(_canonical_destination(item["strategy"]), []).append(item)
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
    for destination, rows in grouped.items():
        if rows:
            label = _canonical_destination(rows[0]["strategy"]).split(" / ", 1)[-1]
            lines.append(f"策略分组：{label} · {len(rows)}只")
    lines.append(f"⚪ 观察中 · {observing}只｜筛选命中不等于买点")
    view["groups"] = grouped
    total = len(active_items)
    page_size = 8
    view["page_count"] = max(1, math.ceil(total / page_size))
    view["page"] = min(view["page"], view["page_count"])
    start = (view["page"] - 1) * page_size
    view["items"] = active_items[start:start + page_size]
    view["focus_lines"] = lines
    return view


def build_limit_views(up_rows, down_rows):
    return {
        "limit_up": rank_limit_up(up_rows or []),
        "limit_down": rank_limit_down(down_rows or []),
        "expanded": False,
    }


def prioritize_stocks(rows, view):
    return sorted(rows, key=lambda stock: view["ranks"].get(stock.get("code"), 3))


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
