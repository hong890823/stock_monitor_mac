#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Iterable, Sequence

from market_news_client import NewsItem


class NewsCategory(str, Enum):
    MACRO_POLICY = "MACRO_POLICY"
    SECTOR_INDUSTRY = "SECTOR_INDUSTRY"
    COMPANY_EVENT = "COMPANY_EVENT"
    MARKET_GENERAL = "MARKET_GENERAL"

    @property
    def label(self) -> str:
        return {
            NewsCategory.MACRO_POLICY: "宏观/政策",
            NewsCategory.SECTOR_INDUSTRY: "行业板块",
            NewsCategory.COMPANY_EVENT: "公司事件",
            NewsCategory.MARKET_GENERAL: "市场快讯",
        }[self]

    @property
    def priority(self) -> int:
        return {
            NewsCategory.MACRO_POLICY: 3,
            NewsCategory.SECTOR_INDUSTRY: 2,
            NewsCategory.COMPANY_EVENT: 1,
            NewsCategory.MARKET_GENERAL: 0,
        }[self]


@dataclass(frozen=True)
class SelectedHeadline:
    item: NewsItem
    category: NewsCategory


@dataclass(frozen=True)
class HeadlineSelection:
    card: tuple[SelectedHeadline, ...]
    detail: tuple[SelectedHeadline, ...]


MACRO_KEYWORDS = (
    "央行", "国务院", "财政部", "发改委", "证监会", "降准", "降息", "lpr", "pmi", "cpi", "ppi",
    "社融", "信贷", "关税", "汇率", "货币政策", "财政政策", "宏观经济", "稳增长", "逆周期",
)
SECTOR_KEYWORDS = (
    "行业", "板块", "产业", "补贴", "涨价", "订单", "新能源", "半导体", "人工智能", "机器人",
    "券商", "保险", "银行", "房地产", "汽车", "医药", "消费", "有色", "化工", "军工", "科技",
)
COMPANY_KEYWORDS = (
    "业绩", "财报", "年报", "季报", "并购", "重组", "回购", "减持", "增持", "中标", "项目",
    "停牌", "复牌", "退市", "立案", "处罚", "股东", "定增", "配股", "澄清",
)


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "")).strip()


def classify_title(title: str) -> NewsCategory:
    normalized = _normalize_title(title).lower()
    if any(keyword.lower() in normalized for keyword in MACRO_KEYWORDS):
        return NewsCategory.MACRO_POLICY
    if any(keyword.lower() in normalized for keyword in SECTOR_KEYWORDS):
        return NewsCategory.SECTOR_INDUSTRY
    if any(keyword.lower() in normalized for keyword in COMPANY_KEYWORDS):
        return NewsCategory.COMPANY_EVENT
    return NewsCategory.MARKET_GENERAL


def select_headlines(
    items: Iterable[NewsItem],
    now: datetime,
    *,
    card_limit: int = 5,
    detail_limit: int = 20,
) -> HeadlineSelection:
    by_title = {}
    current_date = now.date()
    for item in items:
        if not item or not _normalize_title(item.title):
            continue
        if item.published_at.date() != current_date:
            continue
        key = _normalize_title(item.title).casefold()
        existing = by_title.get(key)
        if existing is None or item.published_at > existing.published_at:
            by_title[key] = item

    selected = [
        SelectedHeadline(item=item, category=classify_title(item.title))
        for item in by_title.values()
    ]
    selected.sort(
        key=lambda row: (
            -row.category.priority,
            -row.item.published_at.timestamp(),
            row.item.title,
        )
    )
    detail = tuple(selected[:max(0, detail_limit)])
    return HeadlineSelection(
        card=detail[:max(0, card_limit)],
        detail=detail,
    )
