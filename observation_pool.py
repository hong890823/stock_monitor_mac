"""按完整交易日日线复核选股候选，复用现有入场结构规则。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from continuation_hold_policy import (
    ContinuationHoldInput,
    assess_continuation_hold,
    continuation_hold_to_dict,
    format_continuation_hold_info,
)
import json
import math
import os
from pathlib import Path
import re
import tempfile

from entry_structure import Ma20Slope, VolumeState, compute_entry_input, evaluate
from high_position_policy import HighPositionInput, assess_high_position, high_position_to_dict
from observation_ui import build_observation_view, decorate_stock
from strong_continuation_policy import (
    StrongContinuationInput,
    assess_strong_continuation,
    strong_continuation_to_dict,
)
from trend_assessment import (
    assess_trend,
    build_trade_permissions,
    trend_assessment_to_dict,
)

import pandas as pd


def completed_bars(rows, at):
    bars = {}
    for row in rows or []:
        date = str(row.get("date", ""))[:10]
        day = datetime.strptime(date, "%Y-%m-%d")
        if day.weekday() >= 5 or date > at.strftime("%Y-%m-%d"):
            continue
        if date == at.strftime("%Y-%m-%d") and at.hour < 15:
            continue
        bar = {field: float(row[field]) for field in ("open", "high", "low", "close", "volume")}
        if not all(math.isfinite(value) and value > 0 for value in bar.values()):
            raise ValueError("日线包含无效价格或成交量")
        if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            raise ValueError("日线高低价不一致")
        bars[date] = dict(bar, date=date)
    return [bars[date] for date in sorted(bars)]


class ObservationPool:
    MIN_OBSERVATION_DAYS = 2
    MAX_OBSERVATION_DAYS = 5
    REENTRY_COOLDOWN_DAYS = 5
    SOURCE_LABELS = {
        "strict": "严格", "relaxed": "宽松", "five_confirmed": "513确认",
        "five_waiting": "513观察", "strong": "强势延续",
    }
    ACTIVE_STATUSES = {"observing", "ready"}

    def __init__(self, state_path=None):
        self.state_path = Path(state_path) if state_path else None
        self.records = {}
        self.load_error = None
        if self.state_path and self.state_path.exists():
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
                if payload.get("version") != 2 or not isinstance(payload.get("records"), dict):
                    raise ValueError("观察池文件版本或结构无效")
                for code, record in payload["records"].items():
                    if not isinstance(record, dict) or record.get("code") != code:
                        raise ValueError("观察记录结构无效")
                    if record.get("status") not in self.ACTIVE_STATUSES | {"expired", "eliminated"}:
                        raise ValueError("观察记录状态无效")
                    datetime.strptime(record["first_observed_date"], "%Y-%m-%d")
                    if not isinstance(record.get("history"), list) or not isinstance(record.get("sources"), list):
                        raise ValueError("观察记录历史无效")
                self.records = payload["records"]
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                self.load_error = f"观察池读取失败：{error}"

    def _save(self, records):
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.state_path.parent,
                                             prefix=".observation.", suffix=".tmp", delete=False) as handle:
                temp_path = handle.name
                json.dump({"version": 2, "records": records}, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_path)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    @staticmethod
    def _new_record(hit, date):
        return {"code": hit["code"], "name": hit.get("name") or hit["code"],
                "sources": [], "first_observed_date": date, "last_seen_date": date,
                "last_reviewed_date": None, "observation_days": 0, "status": "observing",
                "reason": "新入池，等待完整日线复核", "data_status": "待复核",
                "price": None, "score": 0, "stop_price": None, "history": []}

    @staticmethod
    def _assess(bars, benchmark, strong_candidate=False):
        date = bars[-1]["date"]
        market = [bar for bar in benchmark if bar["date"] <= date]
        benchmark_ok = None
        if len(market) >= 20 and market[-1]["date"] == date:
            benchmark_ok = market[-1]["close"] >= sum(bar["close"] for bar in market[-20:]) / 20
        entry_input = compute_entry_input(
            [bar["close"] for bar in bars], [bar["high"] for bar in bars],
            [bar["low"] for bar in bars], [bar["volume"] for bar in bars],
            benchmark_ok=benchmark_ok,
        )
        if entry_input is None:
            raise ValueError("日线不足25根")
        entry = evaluate(entry_input)
        trend_result = assess_trend(
            pd.DataFrame(bars),
            price=bars[-1]["close"],
            benchmark_ok=benchmark_ok,
        )
        sample = bars[-60:]
        low60 = min(bar["low"] for bar in sample)
        high60 = max(bar["high"] for bar in sample)
        if high60 > low60:
            location_ratio = (bars[-1]["close"] - low60) / (high60 - low60)
            price_location = (
                "HIGH" if location_ratio >= 0.85
                else "LOW" if location_ratio <= 0.15
                else "MID"
            )
            range_width_pct = (high60 - low60) / low60 * 100
        else:
            price_location = "MID"
            range_width_pct = 0.0
        average_volume20 = sum(bar["volume"] for bar in bars[-20:]) / min(20, len(bars))
        volume_ratio = (
            bars[-1]["volume"] / average_volume20 if average_volume20 > 0 else None
        )
        true_ranges = [
            max(
                bar["high"] - bar["low"],
                abs(bar["high"] - bars[index - 1]["close"]),
                abs(bar["low"] - bars[index - 1]["close"]),
            )
            for index, bar in enumerate(bars[-14:], start=max(1, len(bars) - 14))
        ]
        high_result = assess_high_position(HighPositionInput(
            price=bars[-1]["close"],
            price_location=price_location,
            range_width_pct=range_width_pct,
            atr=sum(true_ranges) / len(true_ranges) if true_ranges else None,
            volume_ratio=volume_ratio,
            close=bars[-1]["close"],
            open=bars[-1]["open"],
            bar_position=(
                (bars[-1]["close"] - bars[-1]["low"])
                / (bars[-1]["high"] - bars[-1]["low"])
                if bars[-1]["high"] > bars[-1]["low"] else None
            ),
            lower_pivots_rising=None,
            support_holds=None,
            support=min(bar["low"] for bar in bars[-20:]),
            resistance=max(bar["high"] for bar in bars[-20:]),
            short_trend=trend_result.short_trend.value,
            market_ok=benchmark_ok,
            sector_ok=None,
            timeframe=None,
        ))
        permissions = build_trade_permissions(
            trend_result,
            entry.can_buy,
            high_result.state,
            benchmark_ok,
            None,
            None,
            "buy",
            0.0,
            0.0,
        )
        ma5 = sum(bar["close"] for bar in bars[-5:]) / 5
        ma10 = sum(bar["close"] for bar in bars[-10:]) / 10
        ma20 = sum(bar["close"] for bar in bars[-20:]) / 20
        ma20_prev = sum(bar["close"] for bar in bars[-21:-1]) / 20
        bias_pct = (bars[-1]["close"] - ma5) / ma5 * 100 if ma5 else 0.0
        breakout_reference = max(bar["close"] for bar in bars[:-5]) if len(bars) > 5 else None
        recent_lows = [bar["low"] for bar in bars[-3:]]
        previous_lows = [bar["low"] for bar in bars[-6:-3]]
        lower_pivots_rising = (
            min(recent_lows) >= min(previous_lows) if previous_lows else None
        )
        support_holds = bars[-1]["low"] >= min(bar["low"] for bar in bars[-20:])
        strong_result = assess_strong_continuation(StrongContinuationInput(
            price=bars[-1]["close"],
            ma5=ma5,
            ma10=ma10,
            ma20=ma20,
            ma20_prev=ma20_prev,
            price_location=price_location,
            bias_pct=bias_pct,
            breakout_reference=breakout_reference,
            volume_ratio=volume_ratio,
            lower_pivots_rising=lower_pivots_rising,
            support_holds=support_holds,
            market_ok=benchmark_ok,
            sector_ok=None,
        ))
        strong_info = strong_continuation_to_dict(strong_result)
        hold_result = assess_continuation_hold(ContinuationHoldInput(
            strong_state=strong_info["state"],
            high_state=high_result.state.name,
            risk_line=strong_info.get("risk_line"),
            risk_price=strong_result.risk_price,
            bias_pct=bias_pct,
            volume_ratio=volume_ratio,
            first_wave=strong_result.state.name in {
                "STRONG_CONTINUATION", "EXTENDED_CONTINUATION",
            },
            timeframe_ok=None,
        ))
        hold_info = continuation_hold_to_dict(hold_result)
        can_enter = (
            strong_candidate
            and entry.can_buy
            and strong_result.eligible_candidate
            and high_result.state.value not in {"高位派发风险", "二波候选"}
        )
        score = sum((entry.ma20_slope == Ma20Slope.UP, entry.volume_state == VolumeState.SHRINK,
                     entry.can_buy, benchmark_ok is True))
        reason = "；".join(filter(None, (
            f"{trend_result.major_trend.value}：{trend_result.summary}",
            "；".join(high_result.reasons or high_result.blockers),
            "；".join(entry.blockers or entry.reasons) or entry.state_label,
            "；".join(strong_result.reasons or strong_result.blockers),
            format_continuation_hold_info(hold_info),
        )))
        if benchmark_ok is None:
            reason = "大盘同日完整日线暂缺，等待复核；" + reason
        return {"date": date, "price": bars[-1]["close"], "score": score,
                "can_enter": can_enter, "trend_weak": entry.trend_weak,
                "structure": entry.state_label, "ma20": entry_input.ma20,
                "volume_ratio": entry_input.volume_ratio, "benchmark_ok": benchmark_ok,
                "stop_price": entry.stop_price, "invalid_rule": entry.invalid_rule, "reason": reason,
                "trend_assessment": trend_assessment_to_dict(trend_result),
                "high_position": high_position_to_dict(high_result),
                "strong_continuation": strong_info,
                "continuation_hold": hold_info,
                "strong_state": strong_info["state"],
                "risk_line": strong_info.get("risk_line"),
                "risk_price": strong_result.risk_price,
                "strong_risk_confirmed": strong_result.risk_confirmed,
                "trade_permissions": {
                    "allow_normal_buy": permissions.allow_normal_buy,
                    "allow_add": permissions.allow_add,
                    "allow_reentry": permissions.allow_reentry,
                    "allow_t": permissions.allow_t,
                    "allow_probe": permissions.allow_probe,
                    "risk_only": permissions.risk_only,
                    "blockers": list(permissions.blockers),
                },
                "trend_mode": trend_result.conflict.name,
                "missing_data": list(trend_result.missing_data) + list(high_result.missing_data)}

    def _set_status(self, record):
        history = record["history"]
        latest = history[-1]
        record.update({key: latest[key] for key in (
            "price", "score", "stop_price", "structure", "invalid_rule",
            "can_enter", "benchmark_ok", "trend_assessment", "high_position",
            "trade_permissions", "trend_mode", "missing_data", "strong_continuation",
            "strong_state", "risk_line", "risk_price", "strong_risk_confirmed",
            "continuation_hold",
        ) if key in latest})
        record["last_reviewed_date"] = latest["date"]
        record["observation_days"] = len(history)
        weak_twice = len(history) >= 2 and all(item["trend_weak"] for item in history[-2:])
        strong_state = latest.get("strong_state")
        dynamic_risk = strong_state in {"M20_BREAK_RISK", "M5_BREAK_RISK"}
        strong_structural_risk = (
            strong_state in {"REJECTED_BREAKOUT", "DISTRIBUTION_RISK"}
            and "strong" in record.get("sources", [])
        )
        if dynamic_risk or strong_structural_risk:
            confirmed = latest.get("strong_risk_confirmed") is True
            status = "eliminated" if confirmed else "observing"
            decision = "破位确认" if confirmed else "风险预警"
            risk_line = latest.get("risk_line") or "动态"
            risk_price = latest.get("risk_price")
            risk_price_text = f" {risk_price:.2f}" if risk_price is not None else ""
            reason = f"{decision}：{strong_state}，参考{risk_line}{risk_price_text}，风险优先于候选晋级"
            record["can_enter"] = False
        elif strong_state == "EXTENDED_CONTINUATION":
            status, reason = "observing", "高位或乖离较大，仅作延伸观察，不进入确定候选"
            record["can_enter"] = False
        elif strong_state == "MISSING":
            status, reason = "observing", "强势延续核心数据不足，不做默认放行，等待复核"
            record["can_enter"] = False
        elif weak_twice:
            status, reason = "eliminated", "连续两个完整交易日趋势转弱，淘汰观察"
        elif latest["benchmark_ok"] is None:
            status, reason = "observing", latest["reason"]
        elif len(history) >= self.MIN_OBSERVATION_DAYS and latest["can_enter"]:
            status, reason = "ready", (
                f"观察{len(history)}个交易日，{latest['structure']}成立，大盘过滤通过；"
                f"{latest['reason']}"
            )
        elif len(history) >= self.MAX_OBSERVATION_DAYS:
            status, reason = "expired", f"观察满{self.MAX_OBSERVATION_DAYS}个交易日，当前未形成可介入结构"
        else:
            status = "observing"
            reason = "首个完整交易日，继续观察；" + latest["reason"] if len(history) == 1 else latest["reason"]
        record.update(status=status, reason=reason, data_status="已复核")
        if status == "observing" and latest["benchmark_ok"] is None:
            record["data_status"] = "待复核：大盘同日完整日线暂缺"
        if status not in self.ACTIVE_STATUSES:
            record["closed_date"] = latest["date"]
        latest.update(status=status, decision=reason)

    def review(self, hits, at=None, *, histories=None, benchmark=None, names=None):
        if self.load_error:
            raise ValueError(self.load_error)
        at = at or datetime.now()
        date = at.strftime("%Y-%m-%d")
        histories, names = histories or {}, names or {}
        records = deepcopy(self.records)
        for hit in hits:
            code = str(hit.get("code") or "").lower()
            if not re.fullmatch(r"(?:sh|sz)\d{6}", code):
                continue
            record = records.setdefault(code, self._new_record(dict(hit, code=code), date))
            if record["status"] not in self.ACTIVE_STATUSES:
                try:
                    closed_date = record.get("closed_date") or record["last_reviewed_date"] or record["first_observed_date"]
                    new_bars = [bar for bar in completed_bars(histories.get(code), at) if bar["date"] > closed_date]
                except (ValueError, TypeError, KeyError):
                    new_bars = []
                if len(new_bars) < self.REENTRY_COOLDOWN_DAYS:
                    continue
                previous = {key: value for key, value in record.items() if key != "previous_rounds"}
                rounds = record.get("previous_rounds", []) + [previous]
                record = self._new_record(dict(hit, code=code), date)
                record["previous_rounds"] = rounds[-5:]
                records[code] = record
            record["name"] = hit.get("name") or record["name"]
            record["last_seen_date"] = date
            source = hit.get("source", "relaxed")
            if source in self.SOURCE_LABELS and source not in record["sources"]:
                record["sources"].append(source)
        try:
            market = completed_bars(benchmark, at)
        except (ValueError, TypeError, KeyError):
            market = []
        for code, record in records.items():
            if record["status"] not in self.ACTIVE_STATUSES:
                continue
            record["name"] = names.get(code) or record["name"]
            if "ST" in record["name"].upper() or "退" in record["name"]:
                record.update(status="eliminated", reason="名称出现ST或退市标记，淘汰观察", data_status="已复核", closed_date=date)
                continue
            try:
                bars = completed_bars(histories.get(code), at)
                if len(bars) < 25:
                    raise ValueError("完整日线暂缺或不足25根")
                for index, bar in enumerate(bars):
                    if bar["date"] < record["first_observed_date"]:
                        continue
                    if record["last_reviewed_date"] and bar["date"] < record["last_reviewed_date"]:
                        continue
                    assessment = self._assess(
                        bars[:index + 1], market,
                        strong_candidate="strong" in record.get("sources", []),
                    )
                    previous_permissions = record.get("trade_permissions") or {}
                    if (
                        previous_permissions.get("allow_normal_buy") is True
                        and assessment.get("strong_state") == "STRONG_CONTINUATION"
                        and not assessment.get("trend_weak")
                        and (assessment.get("high_position") or {}).get("state")
                        not in {"高位派发风险", "二波候选"}
                    ):
                        assessment["trade_permissions"] = dict(
                            previous_permissions,
                            allow_normal_buy=True,
                        )
                    history = record["history"]
                    if history and history[-1]["date"] == bar["date"]:
                        history[-1] = assessment
                    else:
                        history.append(assessment)
                    self._set_status(record)
                    if record["status"] not in self.ACTIVE_STATUSES:
                        break
                if record["status"] in self.ACTIVE_STATUSES:
                    if not record["history"]:
                        record["data_status"] = "待复核：入池后尚无完整日线"
                    elif market and bars[-1]["date"] < market[-1]["date"]:
                        record["data_status"] = f"待复核：个股日线停留在{bars[-1]['date']}"
                        record["status"] = "observing"
            except (ValueError, TypeError, KeyError) as error:
                record["data_status"] = f"待复核：{error}"
                record["status"] = "observing"
        self._save(records)
        self.records = records
        return records

    def active_records(self):
        return [record for record in self.records.values() if record["status"] in self.ACTIVE_STATUSES]

    def record_entry_alert(self, alert):
        if self.load_error:
            raise ValueError(self.load_error)
        record = self.records.get(alert["code"])
        if not record or record["first_observed_date"] != alert["first_observed_date"]:
            return
        records = deepcopy(self.records)
        records[alert["code"]]["last_entry_alert"] = dict(alert)
        self._save(records)
        self.records = records

    def menu_lines(self, limit=20, at=None):
        records = list(self.records.values())
        if self.load_error:
            return [self.load_error]
        view = build_observation_view(self, at)
        lines = view["focus_lines"] + [f"日线候选（等待回踩确认，非当前买点） · {view['ready_count']}只",
                 "至少2个完整交易日 + 有效入场结构；5日未就绪退出",
                 "晋级复用入场规则；数据异常待复核，不按扫描缺席淘汰"]
        labels = {"ready": "日线候选·等待回踩确认", "observing": "继续观察", "eliminated": "已淘汰", "expired": "观察到期"}
        for status, label in labels.items():
            group = sorted((record for record in records if record["status"] == status),
                           key=lambda record: record.get("last_reviewed_date") or record["first_observed_date"], reverse=True)
            lines.append(f"{label} · {len(group)}只")
            for record in group[:limit]:
                price = f"{record['price']:.2f}" if record["price"] is not None else "--"
                sources = "/".join(self.SOURCE_LABELS[source] for source in record["sources"])
                review_date = record["last_reviewed_date"] or "尚未复核"
                lines.append(decorate_stock(f"{record['name']}({record['code']}) | 复核收盘价{price} | 第{record['observation_days']}天 | {record['score']}/4项", record, view))
                lines.append(f"入池{record['first_observed_date']} {sources} | 数据{review_date} | {record['reason']}")
                if record["data_status"] != "已复核":
                    lines.append(record["data_status"])
                if record.get("last_entry_alert"):
                    alert = record["last_entry_alert"]
                    lines.append(f"最近买点提醒 {alert['at']} | 当时触发价{alert['price']:.2f}（非当前买点）")
                if status == "ready" and record["stop_price"]:
                    lines.append(f"失效参考{record['stop_price']:.2f} | {record['invalid_rule']}")
            if len(group) > limit:
                lines.append(f"…其余{len(group) - limit}只，完整记录见观察池报告")
        return lines

    def report(self):
        lines = ["持续观察池（按最近完整交易日复核）",
                 "评分4项：M20上行、缩量、入场结构、大盘过滤；满足项数并非收益概率",
                 "淘汰后至少经过5根新完整日线且重新命中选股，才开始下一轮观察。", ""]
        lines.extend(self.menu_lines(limit=len(self.records)))
        lines.append("\n逐日复核")
        for record in self.records.values():
            lines.append(f"\n{record['name']}({record['code']})")
            if record.get("last_entry_alert"):
                lines.append(record["last_entry_alert"]["body"])
            for snapshot in record["history"]:
                lines.append(f"{snapshot['date']} | 收盘{snapshot['price']:.2f} | {snapshot['score']}/4项 | {snapshot['decision']}")
            for previous in record.get("previous_rounds", []):
                lines.append(f"历史观察轮次 {previous['first_observed_date']}：{previous['reason']}")
        return "\n".join(lines)
