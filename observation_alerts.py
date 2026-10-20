"""观察池盘中回踩企稳提醒：日线背景、实时结构与资金证据共同确认。"""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import math

from capital_flow import flow_direction, is_fresh
from entry_structure import compute_entry_input, evaluate
from intraday_tactics import limit_pct_for_board
from observation_pool import completed_bars
from volume_ratio import VolumeRatio


def is_entry_session(now):
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and (575 <= minute < 690 or 780 <= minute < 897)


def _fresh_quote(quote, now):
    if not quote or quote.get("date") != now.strftime("%Y-%m-%d"):
        raise ValueError("行情日期不匹配")
    stamp = datetime.fromisoformat(f"{quote['date']}T{quote.get('time', '')}")
    if not 0 <= (now - stamp).total_seconds() <= 150:
        raise ValueError("行情已过期")
    values = {key: float(quote[key]) for key in ("price", "open", "high", "low", "prev_close", "volume")}
    if not all(math.isfinite(value) and value > 0 for value in values.values()):
        raise ValueError("行情字段异常")
    if not values["low"] <= min(values["price"], values["open"]) <= max(values["price"], values["open"]) <= values["high"]:
        raise ValueError("行情高低价异常")
    return values, stamp


class ObservationEntryTracker:
    REQUIRED_SAMPLES = 3
    MIN_CONFIRM_SECONDS = 120
    MAX_SAMPLE_GAP_SECONDS = 180
    NEW_LOW_TOLERANCE = 0.001
    LIMIT_BUFFER = Decimal('0.005')

    def __init__(self):
        self._samples = {}
        self._sent = set()
        self._date = None

    def reset(self, code):
        self._samples.pop(code, None)

    def acknowledge(self, alert):
        self._sent.add((alert["date"], alert["code"]))

    def evaluate(self, *, record, bars, quote, benchmark_bars, benchmark_quote, flow, now):
        code = record["code"]
        date = now.strftime("%Y-%m-%d")
        if self._date != date:
            self._samples.clear()
            self._sent.clear()
            self._date = date
        if not is_entry_session(now):
            self.reset(code)
            return None
        if (record.get("status") not in {"observing", "ready"}
                or record.get("observation_days", 0) < 2 or record.get("data_status") != "已复核"):
            self.reset(code)
            return None
        if (record.get("last_entry_alert", {}).get("date") == date or (date, code) in self._sent):
            return None
        try:
            if "ST" in str((quote or {}).get("name") or record["name"]).upper() or "退" in str((quote or {}).get("name") or record["name"]):
                raise ValueError("股票风险标记")
            live, quote_stamp = _fresh_quote(quote, now)
            estimated_limit = (
                Decimal(str(live['prev_close']))
                * (1 + Decimal(str(limit_pct_for_board(code))) / 100)
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if Decimal(str(live['price'])) >= estimated_limit * (1 - self.LIMIT_BUFFER):
                raise ValueError('涨停附近，仅观察，等待回落后重新确认')
            market_live, _ = _fresh_quote(benchmark_quote, now)
            if (not is_fresh(flow, now) or flow.key != code or flow.kind != "stock"
                    or flow.session != "intraday" or not math.isfinite(flow.net_yi)
                    or flow_direction(flow) != "inflow"):
                raise ValueError("未取得新鲜的个股净流入证据")
            history = completed_bars(bars, now)
            market_history = completed_bars(benchmark_bars, now)
            if len(history) < 25 or len(market_history) < 25:
                raise ValueError("日线不足")
            if history[-1]["date"] != market_history[-1]["date"] or record.get("last_reviewed_date") != history[-1]["date"]:
                raise ValueError("日线复核日期不一致")
            if abs(live["prev_close"] / history[-1]["close"] - 1) > 0.01:
                raise ValueError("日线与实时昨收口径不一致")
            if abs(market_live["prev_close"] / market_history[-1]["close"] - 1) > 0.01:
                raise ValueError("大盘日线与实时昨收不一致")
            market_ma20 = (sum(bar["close"] for bar in market_history[-19:]) + market_live["price"]) / 20
            minute = now.hour * 60 + now.minute
            elapsed = minute - 570 - (90 if minute >= 780 else 0)
            volume_ratio = VolumeRatio.of(live["volume"], VolumeRatio.average_volume(history), elapsed)
            live_bar = dict(date=date, open=live["open"], high=live["high"], low=live["low"],
                            close=live["price"], volume=live["volume"])
            current = history + [live_bar]
            entry_input = compute_entry_input(
                [bar["close"] for bar in current], [bar["high"] for bar in current],
                [bar["low"] for bar in current], [bar["volume"] for bar in current],
                volume_ratio=volume_ratio, benchmark_ok=market_live["price"] >= market_ma20,
            )
            entry = evaluate(entry_input)
            if not entry.can_buy or entry.trend_weak or live["price"] < entry_input.ma20:
                raise ValueError("当前回踩未站稳或入场结构不成立")
        except (ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError):
            self.reset(code)
            return None

        samples = self._samples.setdefault(code, [])
        if samples:
            previous = samples[-1]
            gap = (quote_stamp - previous["quote_stamp"]).total_seconds()
            if gap > self.MAX_SAMPLE_GAP_SECONDS or live["price"] < previous["price"] * (1 - self.NEW_LOW_TOLERANCE):
                samples.clear()
            elif gap <= 0 or flow.as_of <= previous["flow_stamp"]:
                return None
        samples.append({"quote_stamp": quote_stamp, "flow_stamp": flow.as_of,
                        "price": live["price"], "net_yi": flow.net_yi})
        del samples[:-10]
        span = (samples[-1]["quote_stamp"] - samples[0]["quote_stamp"]).total_seconds()
        flow_span = (samples[-1]["flow_stamp"] - samples[0]["flow_stamp"]).total_seconds()
        if (len(samples) < self.REQUIRED_SAMPLES or span < self.MIN_CONFIRM_SECONDS
                or flow_span < self.MIN_CONFIRM_SECONDS or live["price"] < samples[0]["price"]):
            return None
        body = (
            f"已观察{record['observation_days']}个完整交易日；回踩上行M20（{entry_input.ma20:.2f}）后站稳，"
            f"连续{len(samples)}次、跨{int(span // 60)}分钟观察未继续走低。\n"
            f"折算量比{volume_ratio:.2f}；平台分钟资金净流入连续为正，最新{flow.net_yi:.3f}亿，出现承接迹象。\n"
            f"失效参考{entry.stop_price:.2f}；跌回M20下方或资金转弱需重新判断，不追离回踩区。"
        )
        popup_body = (f"回踩M20 {entry_input.ma20:.2f} 后站稳，{len(samples)}次/{int(span // 60)}分钟确认\n"
                      f"资金连续净流入，最新{flow.net_yi:.3f}亿，出现承接迹象\n"
                      f"失效参考{entry.stop_price:.2f}；跌回M20下方需重新判断")
        return {"code": code, "date": date, "at": now.isoformat(timespec="seconds"),
                "name": record["name"], "sources": list(record.get("sources", [])),
                "strategy_version": "observation-pullback-v2",
                "evidence": {
                    "estimated_limit_price": float(estimated_limit),
                    "limit_buffer_pct": float(self.LIMIT_BUFFER * 100),
                    "observation_days": record["observation_days"], "ma20": entry_input.ma20,
                    "volume_ratio": volume_ratio, "net_yi": flow.net_yi,
                    "flow_source": flow.source, "flow_at": flow.as_of.isoformat(timespec="seconds"),
                    "quote_at": quote_stamp.isoformat(timespec="seconds"),
                    "previous_close": live["prev_close"], "history_date": history[-1]["date"],
                    "benchmark_code": "sh000001", "benchmark_price": market_live["price"],
                    "benchmark_ma20": market_ma20,
                    "benchmark_at": f"{benchmark_quote['date']}T{benchmark_quote['time']}",
                    "required_samples": self.REQUIRED_SAMPLES, "min_confirm_seconds": self.MIN_CONFIRM_SECONDS,
                    "confirmation_seconds": span,
                    "samples": [{"quote_at": sample["quote_stamp"].isoformat(timespec="seconds"),
                                 "flow_at": sample["flow_stamp"].isoformat(timespec="seconds"),
                                 "price": sample["price"], "net_yi": sample["net_yi"]} for sample in samples],
                },
                "first_observed_date": record["first_observed_date"], "price": live["price"],
                "title": f"🔔 观察池·{record['name']}回踩企稳",
                "subtitle": f"{record.get('name') or '该股票'} 现价{live['price']:.2f}",
                "body": body, "popup_body": popup_body, "stop_price": entry.stop_price,
                "flow_at": flow.as_of.isoformat(timespec="seconds")}
