#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import requests


EASTMONEY_URL = (
    "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
    "?client=web&biz=web_news&column=350&order=1&needInteractData=0"
    "&page_index=1&page_size=50"
)
SINA_URL = (
    "https://feed.mix.sina.com.cn/api/roll/get"
    "?pageid=153&lid=2516&k=&num=50&page=1"
)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class MarketNewsError(RuntimeError):
    pass


@dataclass(frozen=True)
class NewsItem:
    title: str
    published_at: datetime
    publisher_source: str
    fetch_channel: str
    url: Optional[str] = None
    raw_category: Optional[str] = None


Fetcher = Callable[..., str]


def _json_payload(text: str) -> Any:
    trimmed = str(text or "").strip()
    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("新闻响应不是 JSON/JSONP")
    return json.loads(trimmed[start:end + 1])


def _walk_path(payload: Any, path: str) -> Any:
    current = payload
    for key in path.split("."):
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _first_array(payload: Any, paths: Sequence[str]) -> list[Mapping[str, Any]]:
    for path in paths:
        value = _walk_path(payload, path)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _first_value(row: Mapping[str, Any], keys: Iterable[str]) -> Optional[Any]:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() not in {"", "-", "None", "null"}:
            return value
    return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        raw = float(text)
        if raw >= 100_000_000_000:
            return datetime.fromtimestamp(raw / 1000, tz=SHANGHAI).replace(tzinfo=None)
        if raw > 0:
            return datetime.fromtimestamp(raw, tz=SHANGHAI).replace(tzinfo=None)
    except (ValueError, OverflowError, OSError):
        pass
    for pattern in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _normalize_url(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("//"):
        return "https:" + text
    return text


def _items_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    fetch_channel: str,
    title_keys: Sequence[str],
    time_keys: Sequence[str],
    source_keys: Sequence[str],
    url_keys: Sequence[str],
    category_keys: Sequence[str],
) -> list[NewsItem]:
    items = []
    for row in rows:
        title = str(_first_value(row, title_keys) or "").strip()
        published_at = _parse_timestamp(_first_value(row, time_keys))
        if not title or published_at is None:
            continue
        publisher = str(_first_value(row, source_keys) or "").strip()
        items.append(
            NewsItem(
                title=re.sub(r"\s+", " ", title),
                published_at=published_at,
                publisher_source=publisher or "未标注发布来源",
                fetch_channel=fetch_channel,
                url=_normalize_url(_first_value(row, url_keys)),
                raw_category=(
                    str(_first_value(row, category_keys)).strip()
                    if _first_value(row, category_keys) is not None
                    else None
                ),
            )
        )
    return items


def parse_eastmoney(text: str) -> list[NewsItem]:
    payload = _json_payload(text)
    rows = _first_array(
        payload,
        ("data.list", "data.newslist", "data.items", "list", "newslist"),
    )
    return _items_from_rows(
        rows,
        fetch_channel="EASTMONEY",
        title_keys=("title", "name"),
        time_keys=("showtime", "time", "pubtime", "date", "publishTime"),
        source_keys=("media", "source", "mediaName"),
        url_keys=("url", "link", "wapurl"),
        category_keys=("category", "column", "type"),
    )


def parse_sina(text: str) -> list[NewsItem]:
    payload = _json_payload(text)
    rows = _first_array(
        payload,
        ("result.data", "result.newslist", "data", "newslist"),
    )
    return _items_from_rows(
        rows,
        fetch_channel="SINA",
        title_keys=("title", "name"),
        time_keys=("ctime", "time", "pubtime", "date"),
        source_keys=("media_name", "mediaName", "source"),
        url_keys=("url", "link"),
        category_keys=("category", "type"),
    )


def _local_date(value: datetime, now: datetime) -> bool:
    current = now.astimezone(SHANGHAI) if now.tzinfo else now
    candidate = value.astimezone(SHANGHAI) if value.tzinfo else value
    return candidate.date() == current.date()


def _default_fetcher(url: str, **kwargs: Any) -> str:
    response = requests.get(url, timeout=12, **kwargs)
    response.raise_for_status()
    return response.text


def fetch_today(
    now: Optional[datetime] = None,
    *,
    fetcher: Optional[Fetcher] = None,
) -> list[NewsItem]:
    now = now or datetime.now(SHANGHAI)
    fetcher = fetcher or _default_fetcher
    errors = []

    try:
        east_items = parse_eastmoney(
            fetcher(EASTMONEY_URL, headers={"Referer": "https://finance.eastmoney.com/"})
        )
        today_items = [item for item in east_items if _local_date(item.published_at, now)]
        if today_items:
            return today_items
    except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as error:
        errors.append(error)

    try:
        sina_items = parse_sina(
            fetcher(SINA_URL, headers={"Referer": "https://finance.sina.com.cn/"})
        )
        return [item for item in sina_items if _local_date(item.published_at, now)]
    except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as error:
        errors.append(error)

    if errors:
        raise MarketNewsError("今日要闻请求失败") from errors[-1]
    return []
