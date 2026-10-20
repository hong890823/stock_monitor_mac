#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为持仓多周期加仓评估准备行情与基本面输入。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import math
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

from fundamental_health import grade_result
from fundamentals import fetch_and_analyze
from portfolio_addition import FundamentalSnapshot, HoldingAssessmentInput
from portfolio_addition_store import PortfolioAdditionStore


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _snapshot_from_result(
    result: Any,
    analyzed_at: str,
    health_level: str,
) -> FundamentalSnapshot:
    quarters = list(getattr(result, "quarters", []) or [])
    quarter_trends = []
    for quarter in quarters:
        revenue = getattr(quarter, "revenue_yoy", None)
        profit = getattr(quarter, "profit_yoy", None)
        if profit is None:
            profit = getattr(quarter, "net_profit_yoy", None)
        if revenue is not None and profit is not None:
            quarter_trends.append((float(revenue), float(profit)))
    latest = quarters[-1] if quarters else None
    revenue_yoy = getattr(latest, "revenue_yoy", None) if latest else None
    profit_yoy = getattr(latest, "profit_yoy", None) if latest else None
    if latest and profit_yoy is None:
        profit_yoy = getattr(latest, "net_profit_yoy", None)
    risks = []
    for risk in list(getattr(result, "risks", []) or []):
        text = str(getattr(risk, "text", "") or "")
        if text:
            risks.append(text)
    return FundamentalSnapshot(
        analyzed_at=analyzed_at,
        score=int(getattr(result, "score", 0)),
        health_level=health_level,
        revenue_yoy=None if revenue_yoy is None else float(revenue_yoy),
        profit_yoy=None if profit_yoy is None else float(profit_yoy),
        deduct_ratio=getattr(result, "deduct_ratio", None),
        gross_margin=getattr(result, "gross_margin", None),
        net_margin=getattr(result, "net_margin", None),
        ocf_to_profit=getattr(result, "ocf_to_profit", None),
        roe=getattr(result, "roe", None),
        current_pe=getattr(result, "current_pe", None),
        current_pb=getattr(result, "current_pb", None),
        pe_percentile=getattr(result, "pe_percentile", None),
        pb_percentile=getattr(result, "pb_percentile", None),
        fair_low=getattr(result, "fair_low", None),
        fair_high=getattr(result, "fair_high", None),
        risks=tuple(risks),
        notes=tuple(str(note) for note in list(getattr(result, "notes", []) or [])),
        quarter_trends=tuple(quarter_trends),
    )


class PortfolioAdditionService:
    def __init__(
        self,
        *,
        history_fetcher,
        store: PortfolioAdditionStore,
        fundamentals_fetcher=fetch_and_analyze,
        health_grader=grade_result,
        max_workers: int = 2,
    ):
        self.history_fetcher = history_fetcher
        self.store = store
        self.fundamentals_fetcher = fundamentals_fetcher
        self.health_grader = health_grader
        self.max_workers = max(1, min(int(max_workers), 2))

    @staticmethod
    def _positions(positions) -> List[Mapping[str, Any]]:
        if isinstance(positions, Mapping):
            candidates = positions.get("positions", [])
        else:
            candidates = positions or []
        return [item for item in candidates if _finite(item.get("quantity"), 0.0) > 0]

    def _fundamental(
        self,
        code: str,
        name: str,
        now: datetime,
        missing: List[str],
    ) -> Optional[FundamentalSnapshot]:
        cached = self.store.load(code)
        if cached is not None and self.store.is_fresh(cached, now):
            return cached
        try:
            result, _report = self.fundamentals_fetcher(code, name)
            analyzed_at = now.strftime("%Y-%m-%d")
            health = self.health_grader(result, analyzed_at=analyzed_at)
            snapshot = _snapshot_from_result(result, analyzed_at, health.level)
            self.store.save(code, snapshot)
            return snapshot
        except Exception as error:
            reason = f"基本面更新失败：{error}"
            missing.append(reason)
            return cached

    def _prepare_one(
        self,
        position: Mapping[str, Any],
        technical_by_code: Mapping[str, Any],
        now: datetime,
    ) -> HoldingAssessmentInput:
        code = str(position.get("code", "") or "")
        name = str(position.get("name", "") or code)
        technical = technical_by_code.get(code) or {}
        missing: List[str] = []
        try:
            history = self.history_fetcher(code, data_length=1250)
            if not isinstance(history, pd.DataFrame):
                history = pd.DataFrame(history or [])
        except Exception as error:
            history = pd.DataFrame()
            missing.append(f"长周期行情获取失败：{error}")
        fundamental = self._fundamental(code, name, now, missing)
        price = _finite(technical.get("price"), _finite(position.get("price")))
        return HoldingAssessmentInput(
            code=code,
            name=name,
            quantity=_finite(position.get("quantity")),
            cost=_finite(position.get("cost")),
            price=price,
            total_capital=_finite(position.get("total_capital")),
            available_capital=_finite(position.get("available_capital")),
            max_position_pct=_finite(position.get("max_position_pct"), 20.0),
            daily_history=history,
            technical=dict(technical),
            fundamental=fundamental,
            missing=tuple(missing),
        )

    def prepare_inputs(
        self,
        positions,
        technical_by_code: Mapping[str, Any],
        now: datetime,
    ) -> List[HoldingAssessmentInput]:
        holdings = self._positions(positions)
        if isinstance(positions, Mapping):
            defaults = {
                "total_capital": positions.get("total_capital"),
                "available_capital": positions.get("available_capital"),
                "max_position_pct": positions.get("max_position_pct", 20),
            }
            holdings = [
                {**defaults, **position}
                for position in holdings
            ]
        results: Dict[int, HoldingAssessmentInput] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._prepare_one, position, technical_by_code, now): index
                for index, position in enumerate(holdings)
            }
            for future in as_completed(futures):
                index = futures[future]
                position = holdings[index]
                try:
                    results[index] = future.result()
                except Exception as error:
                    code = str(position.get("code", "") or "")
                    results[index] = HoldingAssessmentInput(
                        code=code,
                        name=str(position.get("name", "") or code),
                        quantity=_finite(position.get("quantity")),
                        cost=_finite(position.get("cost")),
                        price=_finite(position.get("price")),
                        total_capital=_finite(position.get("total_capital")),
                        available_capital=_finite(position.get("available_capital")),
                        max_position_pct=_finite(position.get("max_position_pct"), 20),
                        daily_history=pd.DataFrame(),
                        technical=dict(technical_by_code.get(code) or {}),
                        fundamental=self.store.load(code),
                        missing=(f"加仓数据准备失败：{error}",),
                    )
        return [results[index] for index in range(len(holdings))]
