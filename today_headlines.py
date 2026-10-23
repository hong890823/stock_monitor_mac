#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable, Optional, Sequence

from market_news_client import NewsItem
from news_digest_policy import HeadlineSelection, SelectedHeadline, select_headlines


REFRESH_INTERVAL = timedelta(minutes=30)
CHANNEL_LABELS = {"EASTMONEY": "东方财富", "SINA": "新浪财经"}


@dataclass
class TodayHeadlinesState:
    items: Sequence[NewsItem] = field(default_factory=tuple)
    updated_at: Optional[datetime] = None
    last_attempt_at: Optional[datetime] = None
    loading: bool = False
    error: Optional[str] = None

    @staticmethod
    def in_refresh_window(now: datetime) -> bool:
        minutes = now.hour * 60 + now.minute
        return 8 * 60 + 30 <= minutes <= 22 * 60

    def should_refresh(self, now: datetime, *, force: bool = False) -> bool:
        if self.loading:
            return False
        if force:
            return True
        if not self.in_refresh_window(now):
            return False
        return (
            self.last_attempt_at is None
            or now - self.last_attempt_at >= REFRESH_INTERVAL
        )

    def display_items(self, now: datetime) -> list[NewsItem]:
        return [item for item in self.items if item.published_at.date() == now.date()]

    def refresh_started(self, now: datetime) -> None:
        self.loading = True
        self.last_attempt_at = now
        self.error = None

    def refresh_succeeded(self, items: Sequence[NewsItem], now: datetime) -> None:
        self.items = tuple(items)
        self.updated_at = now
        self.last_attempt_at = now
        self.loading = False
        self.error = None

    def refresh_failed(self, error: str, now: datetime) -> None:
        self.last_attempt_at = now
        self.loading = False
        self.error = error


def _serialize_item(item: NewsItem) -> dict:
    return {
        "title": item.title,
        "published_at": item.published_at.isoformat(),
        "publisher_source": item.publisher_source,
        "fetch_channel": item.fetch_channel,
        "url": item.url,
        "raw_category": item.raw_category,
    }


def _deserialize_item(row: dict) -> Optional[NewsItem]:
    try:
        title = str(row["title"]).strip()
        published_at = datetime.fromisoformat(str(row["published_at"]))
        if not title:
            return None
        return NewsItem(
            title=title,
            published_at=published_at,
            publisher_source=str(row.get("publisher_source") or "未标注发布来源"),
            fetch_channel=str(row.get("fetch_channel") or "UNKNOWN"),
            url=row.get("url"),
            raw_category=row.get("raw_category"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def save_state(path: Path, state: TodayHeadlinesState) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "items": [_serialize_item(item) for item in state.items],
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        "last_attempt_at": (
            state.last_attempt_at.isoformat() if state.last_attempt_at else None
        ),
        "error": state.error,
    }
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".headlines.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def load_state(path: Path) -> TodayHeadlinesState:
    path = Path(path)
    if not path.exists():
        return TodayHeadlinesState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            return TodayHeadlinesState(error="今日要闻缓存版本不兼容")
        items = tuple(
            item
            for row in payload.get("items", [])
            if isinstance(row, dict) and (item := _deserialize_item(row)) is not None
        )
        return TodayHeadlinesState(
            items=items,
            updated_at=(
                datetime.fromisoformat(payload["updated_at"])
                if payload.get("updated_at") else None
            ),
            last_attempt_at=(
                datetime.fromisoformat(payload["last_attempt_at"])
                if payload.get("last_attempt_at") else None
            ),
            error=payload.get("error"),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return TodayHeadlinesState(error="今日要闻缓存读取失败")


def format_headlines(
    selection: HeadlineSelection,
    updated_at: Optional[datetime],
    *,
    detail: bool = False,
) -> str:
    rows: Sequence[SelectedHeadline] = selection.detail if detail else selection.card
    title = "📰 今日要闻 · 全部" if detail else "📰 今日要闻"
    lines = [title]
    if updated_at:
        lines.append(f"更新时间：{updated_at.strftime('%Y-%m-%d %H:%M')}")
    if not rows:
        lines.append("暂无今日可用要闻")
        return "\n".join(lines)
    for index, selected in enumerate(rows, 1):
        item = selected.item
        channel = CHANNEL_LABELS.get(item.fetch_channel, item.fetch_channel)
        lines.append(
            f"{index}. [{selected.category.label}] {item.title}"
            f"｜来源：{item.publisher_source}"
            f"｜{item.published_at.strftime('%H:%M')}"
        )
        lines.append(f"   抓取渠道：{channel}｜{item.url or '链接暂缺'}")
    return "\n".join(lines)
