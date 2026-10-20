"""买点证据与追加式复盘快照；仅由独立复盘线程访问。"""

from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from entry_review import completed_rows, evaluate_event, validate_event


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def event_key(event):
    identity = [event[key] for key in ("strategy_version", "code", "first_observed_date", "date")]
    return hashlib.sha256(encode(identity).encode()).hexdigest()


class EntryReviewStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=.2)
        self.connection.row_factory = sqlite3.Row
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("买点账本版本不支持")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    attempted_at TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id TEXT NOT NULL REFERENCES events(id), at TEXT NOT NULL,
                    sent INTEGER NOT NULL, PRIMARY KEY(event_id, at, sent)
                );
                CREATE TABLE IF NOT EXISTS bars (
                    event_id TEXT NOT NULL REFERENCES events(id), kind TEXT NOT NULL,
                    date TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(event_id, kind, date)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id),
                    at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reviews_event ON reviews(event_id, id);
                PRAGMA user_version=1;
            """)
        except Exception:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.connection.close()

    def record_event(self, event):
        validate_event(event)
        payload = encode(event)
        key = event_key(event)
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO events(id,payload,created_at) VALUES(?,?,?)",
                                    (key, payload, datetime.now().isoformat(timespec="seconds")))
        return key

    def record_delivery(self, key, sent, at):
        if not self.connection.execute("SELECT 1 FROM events WHERE id=?", (key,)).fetchone():
            raise ValueError("通知回执对应的事件不存在")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,?)", (key, at, int(sent)))

    def events(self):
        rows = self.connection.execute("""
            SELECT events.*, (SELECT payload FROM reviews WHERE event_id=events.id
                ORDER BY id DESC LIMIT 1) AS review,
                (SELECT MAX(sent) FROM deliveries WHERE event_id=events.id) AS sent
            FROM events ORDER BY created_at, rowid
        """).fetchall()
        return [dict(key=row["id"], event=json.loads(row["payload"]),
                     review=json.loads(row["review"]) if row["review"] else None,
                     delivery="sent" if row["sent"] == 1 else "failed" if row["sent"] == 0 else "unknown",
                     attempted_at=row["attempted_at"], error=row["error"]) for row in rows]

    def pending_events(self):
        pending = []
        for row in self.events():
            review = row["review"] or {}
            if review.get("status") != "complete" or review.get("simulation", {}).get("status") not in {"closed", "unfilled"}:
                pending.append(row)
        return sorted(pending, key=lambda row: (row["attempted_at"] or "", row["event"]["at"], row["key"]))

    def record_error(self, key, error, at):
        with self.connection:
            self.connection.execute("UPDATE events SET error=?,attempted_at=? WHERE id=?",
                                    (str(error), at.isoformat(timespec="seconds"), key))

    def review(self, key, stock_bars, benchmark_bars, at):
        row = self.connection.execute("SELECT payload FROM events WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("买点事件不存在")
        event = json.loads(row[0])
        with self.connection:
            histories = {}
            for kind, rows in (("stock", stock_bars), ("benchmark", benchmark_bars)):
                for date, daily in completed_rows(rows, at).items():
                    if date < event["date"]:
                        continue
                    payload = encode(daily)
                    previous = self.connection.execute(
                        "SELECT payload FROM bars WHERE event_id=? AND kind=? AND date=?", (key, kind, date)
                    ).fetchone()
                    if previous and previous[0] != payload:
                        raise ValueError(f"{date} {kind}历史日线变更，保留原记录并等待核对")
                    self.connection.execute("INSERT OR IGNORE INTO bars VALUES(?,?,?,?)", (key, kind, date, payload))
                histories[kind] = [json.loads(item[0]) for item in self.connection.execute(
                    "SELECT payload FROM bars WHERE event_id=? AND kind=? ORDER BY date", (key, kind))]
            result = evaluate_event(event, histories["stock"], histories["benchmark"], at)
            payload = encode(result)
            previous = self.connection.execute("SELECT payload FROM reviews WHERE event_id=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            if not previous or previous[0] != payload:
                self.connection.execute("INSERT INTO reviews(event_id,at,payload) VALUES(?,?,?)",
                                        (key, at.isoformat(timespec="seconds"), payload))
            self.connection.execute("UPDATE events SET error=NULL,attempted_at=? WHERE id=?",
                                    (at.isoformat(timespec="seconds"), key))
        return result

    def report(self):
        rows = self.events()
        lines = ["买点事件账本与模拟复盘", f"总事件{len(rows)}｜基准：触发同时刻上证指数",
                 "观察池退出不删除事件；通知失败也保留合格信号；缺失数据不计为亏损或盈利。",
                 "1/3/5/10日按指数完整交易日计数；最大涨跌仅统计触发次日起前10日有效日线。",
                 "模拟成交均为日线假设成交，独立样本，不是共享资金组合收益。",
                 "每事件默认1万元；次日开盘尝试，T+1退出，固定失效价/2倍风险止盈/第10日退出。",
                 "默认滑点0.1%，佣金0.025%最低5元，卖出税费假设0.05%，过户费假设0.001%；各事件参数独立冻结。",
                 "无逐笔/盘口成交验证，分红送转及价格异常需核对。\n"]
        groups = defaultdict(list)
        labels = {"strict": "严格", "relaxed": "宽松", "five_confirmed": "513确认", "five_waiting": "513观察"}
        for horizon in (1, 3, 5, 10):
            count = sum(bool(row["review"] and row["review"]["horizons"][str(horizon)]["status"] == "available") for row in rows)
            lines.append(f"全部事件：{horizon}日有效{count}/{len(rows)}")
        for row in rows:
            order = {source: index for index, source in enumerate(labels)}
            sources = "+".join(labels.get(source, source) for source in sorted(set(row["event"].get("sources", [])), key=lambda source: (order.get(source, 99), source))) or "未知来源"
            groups[(row["event"]["strategy_version"], sources)].append(row)
        for (version, source), members in sorted(groups.items()):
            lines.append(f"策略 {version}｜来源 {source}｜样本{len(members)}（按来源组合分组，不重复计数）")
            for horizon in (1, 3, 5, 10):
                available = [row["review"]["horizons"][str(horizon)] for row in members
                             if row["review"] and row["review"]["horizons"][str(horizon)]["status"] == "available"]
                text = f"  {horizon}日有效{len(available)}/{len(members)}"
                if available:
                    mean = sum(item["return_pct"] for item in available) / len(available)
                    excess = sum(item["excess_pct"] for item in available) / len(available)
                    wins = sum(item["return_pct"] > 0 for item in available)
                    text += f"｜触发价收益均值{mean:+.2f}%｜超额均值{excess:+.2f}个百分点｜上涨{wins}/{len(available)}"
                lines.append(text)
            simulations = [row["review"]["simulation"] for row in members if row["review"]]
            closed = [item for item in simulations if item["status"] == "closed"]
            lines.append(f"  模拟已平仓{len(closed)}/{len(members)}｜盈利{sum(item['realized_pnl'] > 0 for item in closed)}/{len(closed)}")
            if closed:
                mean = sum(item["return_pct"] for item in closed) / len(closed)
                lines.append(f"  独立样本净收益率均值{mean:+.2f}%（含费用滑点；样本不足不推断策略有效）")
        lines.append("\n最近200个事件（完整历史保存在SQLite账本）")
        statuses = {"pending": "等待后续交易日", "pending_data": "数据待补", "tracking": "观察中",
                    "complete": "10日观察完成", "unfilled": "未模拟成交", "open": "模拟持仓中",
                    "closed": "模拟已平仓", "needs_review": "待核对"}
        deliveries = {"sent": "通知已发送", "failed": "通知失败", "unknown": "通知结果未知"}
        for row in reversed(rows[-200:]):
            event, review = row["event"], row["review"]
            lines.append(f"\n{event['at']} {event.get('name', '')} {event['code']}｜触发{event['price']:.2f}｜{deliveries[row['delivery']]}")
            evidence = event["evidence"]
            lines.append(f"  观察{evidence.get('observation_days', '?')}日｜失效价{event['stop_price']:.2f}｜来源{','.join(event.get('sources', []))}")
            lines.append(f"  MA20={evidence.get('ma20', '暂缺')}｜量比={evidence.get('volume_ratio', '暂缺')}｜资金净流入亿={evidence.get('net_yi', '暂缺')}")
            if row["error"]:
                lines.append(f"  复盘错误：{row['error']}")
            if not review:
                lines.append("  待复盘")
                continue
            lines.append(f"  {statuses[review['status']]}｜数据截至{review['as_of_date']}｜有效日线{review['observed_sessions']}/{review['expected_sessions']}")
            if review.get("quality_warning"):
                lines.append(f"  口径提示：{review['quality_warning']}")
            for horizon in ("1", "3", "5", "10"):
                item = review["horizons"][horizon]
                text = f"  {horizon}日 {item['date'] or '尚未到期'}："
                text += (f"触发价收益{item['return_pct']:+.2f}%｜相对指数{item['excess_pct']:+.2f}个百分点"
                         if item["status"] == "available" else "数据缺失" if item["status"] == "missing"
                         else "价格口径待核对" if item["status"] == "needs_review" else "等待")
                lines.append(text)
            if review["max_favorable_pct"] is not None:
                lines.append(f"  有效日线最大上涨{review['max_favorable_pct']:+.2f}% / 最大下跌{review['max_adverse_pct']:+.2f}%（不含触发当日）")
            simulation = review["simulation"]
            lines.append(f"  {statuses[simulation['status']]}：{simulation['reason']}｜模拟现金{simulation['cash']:.2f}｜股数{simulation['quantity']}")
            for trade in simulation["trades"]:
                side = "买入" if trade["side"] == "buy" else "卖出"
                lines.append(f"    {trade['date']} 假设{side}{trade['quantity']}股@{trade['price']:.2f}｜费用{trade['fee']:.2f}｜{trade['reason']}")
            if simulation["status"] == "closed":
                lines.append(f"  已实现净盈亏{simulation['realized_pnl']:+.2f}｜独立资金净收益率{simulation['return_pct']:+.2f}%")
        return "\n".join(lines) + "\n"
