#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持久化持仓加仓评估所需的基本面快照。"""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Dict, Optional

from portfolio_addition import FundamentalSnapshot


_CODE_PATTERN = re.compile(r"^(?:sh|sz|bj)\d{6}$")
_HEALTH_LEVELS = {"ALERT", "NEUTRAL", "SOUND"}
_OPTIONAL_FLOAT_FIELDS = {
    "revenue_yoy", "profit_yoy", "deduct_ratio", "gross_margin",
    "net_margin", "ocf_to_profit", "roe", "current_pe", "current_pb",
    "pe_percentile", "pb_percentile", "fair_low", "fair_high",
}


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_snapshot(record: Any) -> FundamentalSnapshot:
    if not isinstance(record, dict):
        raise ValueError("基本面记录必须是对象")
    expected = {field.name for field in fields(FundamentalSnapshot)}
    missing = sorted(expected - set(record))
    extra = sorted(set(record) - expected)
    if missing:
        raise ValueError(f"基本面记录缺少字段：{', '.join(missing)}")
    if extra:
        raise ValueError(f"基本面记录包含未知字段：{', '.join(extra)}")
    if not _valid_date(record["analyzed_at"]):
        raise ValueError("分析日期无效")
    score = record["score"]
    if not isinstance(score, int) or isinstance(score, bool):
        raise ValueError("基本面评分必须是整数")
    if record["health_level"] not in _HEALTH_LEVELS:
        raise ValueError("基本面健康等级无效")
    for name in _OPTIONAL_FLOAT_FIELDS:
        value = record[name]
        if value is not None and not _finite_number(value):
            raise ValueError(f"{name} 数值无效")
    for name in ("risks", "notes"):
        value = record[name]
        if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{name} 必须是字符串列表")
    trends = record["quarter_trends"]
    if not isinstance(trends, (list, tuple)):
        raise ValueError("quarter_trends 必须是列表")
    normalized_trends = []
    for trend in trends:
        if not isinstance(trend, (list, tuple)) or len(trend) != 2:
            raise ValueError("quarter_trends 每项必须包含两个数值")
        if not all(_finite_number(value) for value in trend):
            raise ValueError("quarter_trends 包含无效数值")
        normalized_trends.append((float(trend[0]), float(trend[1])))
    normalized = dict(record)
    for name in _OPTIONAL_FLOAT_FIELDS:
        normalized[name] = None if normalized[name] is None else float(normalized[name])
    normalized["risks"] = tuple(record["risks"])
    normalized["notes"] = tuple(record["notes"])
    normalized["quarter_trends"] = tuple(normalized_trends)
    return FundamentalSnapshot(**normalized)


class PortfolioAdditionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.last_error: Optional[str] = None
        self._lock = threading.RLock()

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "records": {}}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            self.last_error = f"缓存文件不可读：{error}"
            return {"version": 1, "records": {}}
        if not isinstance(payload, dict):
            self.last_error = "缓存顶层必须是对象"
            return {"version": 1, "records": {}}
        if payload.get("version") != 1:
            self.last_error = "缓存版本无效"
            return {"version": 1, "records": {}}
        records = payload.get("records")
        if not isinstance(records, dict):
            self.last_error = "缓存 records 必须是对象"
            return {"version": 1, "records": {}}
        return {"version": 1, "records": records}

    def save(self, code: str, snapshot: FundamentalSnapshot) -> None:
        if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
            raise ValueError("股票代码无效")
        if not isinstance(snapshot, FundamentalSnapshot):
            raise ValueError("snapshot 必须是 FundamentalSnapshot")
        normalized = _validate_snapshot(asdict(snapshot))
        with self._lock:
            payload = self._read()
            payload["records"][code] = asdict(normalized)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=str(self.path.parent),
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, self.path)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
            self.last_error = None

    def load(self, code: str) -> Optional[FundamentalSnapshot]:
        if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
            self.last_error = "股票代码无效"
            return None
        with self._lock:
            payload = self._read()
            record = payload["records"].get(code)
            if record is None:
                self.last_error = None
                return None
            try:
                return _validate_snapshot(record)
            except (TypeError, ValueError) as error:
                self.last_error = f"{code} 基本面缓存无效：{error}"
                return None

    def is_fresh(self, snapshot: Optional[FundamentalSnapshot], now: datetime) -> bool:
        if not isinstance(snapshot, FundamentalSnapshot):
            self.last_error = "待检查的基本面快照无效"
            return False
        try:
            analyzed_at = datetime.fromisoformat(snapshot.analyzed_at)
            current = datetime.fromisoformat(now.isoformat())
        except (TypeError, ValueError):
            self.last_error = "基本面分析日期无效"
            return False
        if analyzed_at.tzinfo is not None:
            analyzed_at = analyzed_at.replace(tzinfo=None)
        if current.tzinfo is not None:
            current = current.replace(tzinfo=None)
        if analyzed_at > current:
            self.last_error = "基本面分析日期在未来"
            return False
        if (current - analyzed_at).total_seconds() > 30 * 24 * 60 * 60:
            self.last_error = "基本面缓存已超过30天"
            return False
        self.last_error = None
        return True
