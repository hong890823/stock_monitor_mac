"""按完整交易日日线复核选股候选，复用现有入场结构规则。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from capital_flow import CapitalFlowSnapshot
from capital_flow_policy import assess_window
from continuation_hold_policy import (
    ContinuationHoldInput,
    assess_continuation_hold,
    continuation_hold_to_dict,
    format_continuation_hold_info,
)
from candidate_state_policy import (
    CandidateReviewInput,
    CandidateState,
    CandidateStrategy,
    advance_candidate,
    required_confirmation_rounds,
    route_source,
)
import json
import math
import os
from pathlib import Path
import re
import tempfile

from entry_structure import Ma20Slope, VolumeState, compute_entry_input, evaluate
from high_position_policy import HighPositionInput, assess_high_position, high_position_to_dict
from promotion_environment_policy import PromotionEnvironmentInput, evaluate_environment
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
    MAX_OBSERVATION_DAYS = 10
    REENTRY_COOLDOWN_DAYS = 5
    STATE_VERSION = 3
    ACTIVE_STATUSES = {"observing", "ready"}
    STRATEGY_DESTINATIONS = {
        CandidateStrategy.PRE_BREAKOUT.value: "PRE_BREAKOUT / 启动前潜力",
        CandidateStrategy.LEADER.value: "LEADER / 强势龙头",
        CandidateStrategy.LIMIT_UP_CONTINUATION.value: "LIMIT_UP_CONTINUATION / 涨停持续观察",
    }
    SOURCE_LABELS = {
        "strict": "严格", "relaxed": "宽松", "five_confirmed": "513确认",
        "five_waiting": "513观察", "strong": "强势延续",
    }
    def __init__(self, state_path=None):
        self.state_path = Path(state_path) if state_path else None
        self.records = {}
        self.load_error = None
        self.config = {"promotion_environment_gate_enabled": False}
        if self.state_path and self.state_path.exists():
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
                if payload.get("version") not in {2, self.STATE_VERSION}:
                    raise ValueError("观察池文件版本无效")
                if not isinstance(payload.get("records"), dict):
                    raise ValueError("观察池文件版本或结构无效")
                config = payload.get("config") or {}
                if isinstance(config.get("promotion_environment_gate_enabled"), bool):
                    self.config["promotion_environment_gate_enabled"] = (
                        config["promotion_environment_gate_enabled"]
                    )
                for code, record in payload["records"].items():
                    if not isinstance(record, dict) or record.get("code") != code:
                        raise ValueError("观察记录结构无效")
                    self.records[code] = self._normalize_record(record)
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                self.load_error = f"观察池读取失败：{error}"

    @classmethod
    def _normalize_record(cls, record):
        normalized = deepcopy(record)
        if normalized.get("status") not in cls.ACTIVE_STATUSES | {"expired", "eliminated"}:
            raise ValueError("观察记录状态无效")
        datetime.strptime(normalized["first_observed_date"], "%Y-%m-%d")
        if not isinstance(normalized.get("history"), list) or not isinstance(normalized.get("sources"), list):
            raise ValueError("观察记录历史无效")

        sources = normalized.get("sources") or []
        strategy = normalized.get("strategy")
        if strategy not in cls.STRATEGY_DESTINATIONS:
            strategy = route_source(
                ",".join(str(source) for source in sources),
                strong_candidate="strong" in sources,
                limit_candidate="limit" in sources,
            ).value
        old_status = normalized.get("status")
        state = normalized.get("state")
        if state not in {item.value for item in CandidateState}:
            state = {
                "ready": CandidateState.NEAR_ENTRY.value,
                "expired": CandidateState.ELIMINATED.value,
                "eliminated": CandidateState.ELIMINATED.value,
            }.get(old_status, CandidateState.OBSERVING.value)

        defaults = {
            "strategy": strategy,
            "state": state,
            "entry_destination": cls.STRATEGY_DESTINATIONS[strategy],
            "confirmation_rounds": 0,
            "last_confirmation_at": None,
            "confirmation_started_at": None,
            "limit_streak": None,
            "seal_quality_score": None,
            "seal_quality_coverage_pct": None,
            "data_coverage": None,
            "related_sectors": [],
            "flow_window": [],
            "risk_evidence": [],
            "breakout_risk": None,
            "last_limit_seal_event": None,
            "eliminated_reason": None,
            "exit_date": None,
            "reentry_cooldown_until": None,
            "observation_dates": [],
            "weak_dates": [],
            "continuous_observation": False,
            "limit_up_blocked": False,
            "environment_evidence": [],
            "environment_gate_reason": None,
            "_environment_blocked": False,
        }
        for key, value in defaults.items():
            normalized.setdefault(key, value)
        normalized["status"] = {
            CandidateState.OBSERVING.value: "observing",
            CandidateState.NEAR_ENTRY.value: "ready",
            CandidateState.BUY_CANDIDATE.value: "ready",
            CandidateState.ELIMINATED.value: "eliminated",
        }[normalized["state"]]
        return normalized

    def _save(self, records):
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.state_path.parent,
                                             prefix=".observation.", suffix=".tmp", delete=False) as handle:
                temp_path = handle.name
                json.dump(
                    {
                        "version": self.STATE_VERSION,
                        "config": self.config,
                        "records": records,
                    },
                    handle,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_path)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    @staticmethod
    def _new_record(hit, date):
        strategy = route_source(
            hit.get("source", "relaxed"),
            strong_candidate=hit.get("strong_candidate", False),
            limit_candidate=hit.get("limit_candidate", False),
        ).value
        return {
            "code": hit["code"], "name": hit.get("name") or hit["code"],
            "sources": [], "first_observed_date": date, "last_seen_date": date,
            "last_reviewed_date": None, "observation_days": 0, "status": "observing",
            "reason": "新入池，等待完整日线复核", "data_status": "待复核",
            "price": None, "score": 0, "stop_price": None, "history": [],
            "strategy": strategy,
            "state": CandidateState.OBSERVING.value,
            "entry_destination": ObservationPool.STRATEGY_DESTINATIONS[strategy],
            "confirmation_rounds": 0, "last_confirmation_at": None,
            "confirmation_started_at": None, "limit_streak": hit.get("limit_streak"),
            "seal_quality_score": hit.get("seal_quality_score"),
            "seal_quality_coverage_pct": hit.get("seal_quality_coverage_pct"),
            "data_coverage": hit.get("data_coverage"),
            "related_sectors": list(hit.get("related_sectors") or []),
            "flow_window": list(hit.get("flow_window") or []),
            "risk_evidence": list(hit.get("risk_evidence") or []),
            "breakout_risk": hit.get("breakout_risk"),
            "last_limit_seal_event": hit.get("last_limit_seal_event"),
            "eliminated_reason": None, "exit_date": None,
            "reentry_cooldown_until": None,
            "observation_dates": [], "weak_dates": [],
            "continuous_observation": bool(hit.get("continuous_observation", False)),
            "limit_up_blocked": bool(hit.get("limit_up_blocked", False)),
            "environment_evidence": [], "environment_gate_reason": None,
            "_environment_blocked": False,
        }

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

    @staticmethod
    def required_confirmation_rounds(limit_streak):
        return required_confirmation_rounds(limit_streak)

    @staticmethod
    def _legacy_status(state):
        return {
            CandidateState.OBSERVING.value: "observing",
            CandidateState.NEAR_ENTRY.value: "ready",
            CandidateState.BUY_CANDIDATE.value: "ready",
            CandidateState.ELIMINATED.value: "eliminated",
        }[state]

    def apply_promotion_environment(
        self,
        record,
        environment,
        *,
        enabled=None,
        at=None,
    ):
        if self.load_error:
            raise ValueError(self.load_error)
        enabled = (
            self.config.get("promotion_environment_gate_enabled", False)
            if enabled is None else bool(enabled)
        )
        decision = evaluate_environment(
            PromotionEnvironmentInput(
                shanghai_change_pct=environment.get("shanghai_change_pct"),
                shenzhen_change_pct=environment.get("shenzhen_change_pct"),
                limit_up_count=environment.get("limit_up_count"),
                limit_down_count=environment.get("limit_down_count"),
                board_changes=list(environment.get("board_changes") or []),
            ),
            enabled=enabled,
        )
        record.update(
            environment_evidence=list(decision.evidence),
            environment_gate_reason=decision.reason,
            _environment_blocked=decision.blocked,
        )
        if at:
            record["environment_evaluated_at"] = at.isoformat()
        return {
            "blocked": decision.blocked,
            "reason": decision.reason,
            "evidence": list(decision.evidence),
        }

    def observe_candidate_sample(
        self,
        code,
        at,
        *,
        ordinary_gates_passed,
        price_trend_valid,
        trend_weak=False,
        sealed=False,
        environment_blocked=False,
        cumulative_volume_increase=False,
        limit_streak=None,
    ):
        if self.load_error:
            raise ValueError(self.load_error)
        code = str(code or "").lower()
        if code not in self.records:
            raise KeyError(f"观察记录不存在：{code}")
        records = deepcopy(self.records)
        record = records[code]
        transition = advance_candidate(
            record,
            CandidateReviewInput(
                at=at,
                ordinary_gates_passed=ordinary_gates_passed,
                trend_weak=trend_weak,
                price_trend_valid=price_trend_valid,
                fresh_sample=True,
                sealed=sealed,
                limit_streak=(
                    record.get("limit_streak")
                    if limit_streak is None else limit_streak
                ),
                environment_blocked=environment_blocked,
                cumulative_volume_increase=cumulative_volume_increase,
            ),
        )
        record.clear()
        record.update(transition.record)
        record["status"] = self._legacy_status(record["state"])
        record["last_reviewed_date"] = at.strftime("%Y-%m-%d")
        record["data_status"] = "已复核"
        if transition.blockers:
            record["reason"] = "；".join(transition.blockers)
        elif record["state"] == CandidateState.BUY_CANDIDATE.value:
            record["reason"] = "买点已确认，仅代表观察状态升级，不代表自动下单"
        elif record["state"] == CandidateState.NEAR_ENTRY.value:
            record["reason"] = "买点待确认，继续等待时间分离的新行情样本"
        elif record["state"] == CandidateState.ELIMINATED.value:
            record["reason"] = record.get("eliminated_reason") or "候选已淘汰"
        else:
            record["reason"] = "继续观察"
        if record["status"] not in self.ACTIVE_STATUSES:
            record["closed_date"] = at.strftime("%Y-%m-%d")
        self._save(records)
        self.records = records
        return deepcopy(record)

    def _set_status(self, record, review_at=None):
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
        strong_state = latest.get("strong_state")
        dynamic_risk = strong_state in {"M20_BREAK_RISK", "M5_BREAK_RISK"}
        strong_structural_risk = (
            strong_state in {"REJECTED_BREAKOUT", "DISTRIBUTION_RISK"}
            and "strong" in record.get("sources", [])
        )
        if dynamic_risk or strong_structural_risk:
            confirmed = latest.get("strong_risk_confirmed") is True
            state = CandidateState.ELIMINATED.value if confirmed else CandidateState.OBSERVING.value
            decision = "破位确认" if confirmed else "风险预警"
            risk_line = latest.get("risk_line") or "动态"
            risk_price = latest.get("risk_price")
            risk_price_text = f" {risk_price:.2f}" if risk_price is not None else ""
            reason = f"{decision}：{strong_state}，参考{risk_line}{risk_price_text}，风险优先于候选晋级"
            record["can_enter"] = False
        elif strong_state == "EXTENDED_CONTINUATION":
            state, reason = CandidateState.OBSERVING.value, "高位或乖离较大，仅作延伸观察，不进入确定候选"
            record["can_enter"] = False
        elif strong_state == "MISSING":
            state, reason = CandidateState.OBSERVING.value, "强势延续核心数据不足，不做默认放行，等待复核"
            record["can_enter"] = False
        else:
            review_at = review_at or datetime.fromisoformat(f"{latest['date']}T15:00:00")
            transition = advance_candidate(
                record,
                CandidateReviewInput(
                    at=review_at,
                    ordinary_gates_passed=(
                        latest.get("can_enter") is True
                        and latest.get("benchmark_ok") is True
                    ),
                    trend_weak=latest.get("trend_weak", False),
                    price_trend_valid=not latest.get("trend_weak", False),
                    fresh_sample=True,
                    sealed=bool(
                        latest.get("sealed")
                        or (record.get("last_limit_seal_event") or {}).get("sealed")
                    ),
                    limit_streak=record.get("limit_streak"),
                    environment_blocked=bool(record.get("_environment_blocked")),
                    cumulative_volume_increase=bool(
                        latest.get("cumulative_volume_increase")
                    ),
                ),
            )
            record.update(transition.record)
            latest = record["history"][-1]
            state = record["state"]
            if transition.blockers:
                reason = "；".join(transition.blockers) + "；" + latest["reason"]
            elif state == CandidateState.BUY_CANDIDATE.value:
                reason = "买点已确认，仅代表观察状态升级，不代表自动下单"
            elif state == CandidateState.NEAR_ENTRY.value:
                reason = f"买点待确认：{latest['reason']}"
            else:
                reason = latest["reason"]

        record["state"] = state
        status = self._legacy_status(state)
        if state == CandidateState.OBSERVING.value and len(history) == 1:
            reason = "首个完整交易日，继续观察；" + reason
        if state == CandidateState.ELIMINATED.value and not record.get("eliminated_reason"):
            record["eliminated_reason"] = reason
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
                    self._set_status(
                        record,
                        datetime.fromisoformat(f"{bar['date']}T15:00:00"),
                    )
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

    @staticmethod
    def _flow_snapshot(row):
        as_of = row.get("as_of")
        return CapitalFlowSnapshot(
            key=str(row.get("key") or row.get("code") or ""),
            name=str(row.get("name") or ""),
            kind=str(row.get("kind") or "stock"),
            quote_date=str(row.get("quote_date") or ""),
            as_of=(
                as_of if isinstance(as_of, datetime)
                else datetime.fromisoformat(str(as_of))
            ),
            source=str(row.get("source") or ""),
            scope=str(row.get("scope") or ""),
            session=str(row.get("session") or ""),
            net_yi=row.get("net_yi"),
            net_ratio_pct=row.get("net_ratio_pct"),
            inflow_yi=row.get("inflow_yi"),
            outflow_yi=row.get("outflow_yi"),
            large_net_yi=row.get("large_net_yi"),
            super_large_net_yi=row.get("super_large_net_yi"),
            stale=bool(row.get("stale", False)),
            turnover_yi=row.get("turnover_yi"),
        )

    def record_fund_flow_snapshot(self, code, snapshot, change_pct=None):
        if self.load_error:
            raise ValueError(self.load_error)
        code = str(code or "").lower()
        if code not in self.records:
            raise KeyError(f"观察记录不存在：{code}")
        if not isinstance(snapshot, CapitalFlowSnapshot):
            raise TypeError("资金流快照类型无效")
        records = deepcopy(self.records)
        record = records[code]
        row = asdict(snapshot)
        row["as_of"] = snapshot.as_of.isoformat()
        by_date = {
            item.get("quote_date"): item
            for item in record.get("flow_window", [])
            if isinstance(item, dict) and item.get("quote_date")
        }
        by_date[snapshot.quote_date] = row
        flow_window = [by_date[key] for key in sorted(by_date)[-5:]]
        assessment = assess_window(
            [self._flow_snapshot(item) for item in flow_window],
            change_pct,
        )
        assessment_dict = asdict(assessment)
        assessment_dict["tone"] = assessment.tone.value
        assessment_dict["sample_dates"] = list(assessment.sample_dates)
        record["flow_window"] = flow_window
        record["flow_window_assessment"] = assessment_dict
        record["_flow_blocks_promotion"] = assessment.blocks_promotion
        self._save(records)
        self.records = records
        return deepcopy(record)

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
                decision = snapshot.get("decision") or snapshot.get("reason") or record.get("reason") or "已复核"
                lines.append(f"{snapshot['date']} | 收盘{snapshot['price']:.2f} | {snapshot['score']}/4项 | {decision}")
            for previous in record.get("previous_rounds", []):
                lines.append(f"历史观察轮次 {previous['first_observed_date']}：{previous['reason']}")
        return "\n".join(lines)
