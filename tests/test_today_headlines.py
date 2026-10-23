#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from market_news_client import NewsItem, fetch_today, parse_eastmoney, parse_sina
from news_digest_policy import NewsCategory, select_headlines
from today_headlines import TodayHeadlinesState, format_headlines, load_state, save_state


def item(title, minute, source="第一财经", channel="EASTMONEY", day=23):
    return NewsItem(
        title=title,
        published_at=datetime(2026, 10, day, 10, minute),
        publisher_source=source,
        fetch_channel=channel,
        url="https://example.com/news",
    )


class NewsSelectionTests(unittest.TestCase):
    def test_select_filters_today_deduplicates_and_ranks(self):
        rows = [
            item("市场快讯", 1),
            item("央行发布政策", 2),
            item("半导体行业订单增加", 3),
            item("公司披露业绩", 4),
            item("央行发布政策", 2, source="其他媒体"),
            item("旧闻", 0, source="新华社", channel="SINA", day=22),
        ]
        selection = select_headlines(rows, datetime(2026, 10, 23, 12, 0))
        self.assertEqual(selection.detail[0].category, NewsCategory.MACRO_POLICY)
        self.assertEqual(len(selection.detail), 4)
        self.assertEqual(selection.card[0].item.publisher_source, "第一财经")

    def test_card_and_detail_limits(self):
        rows = [item(f"央行新闻{i}", i) for i in range(25)]
        selection = select_headlines(
            rows, datetime(2026, 10, 23, 12, 0), card_limit=5, detail_limit=20
        )
        self.assertEqual(len(selection.card), 5)
        self.assertEqual(len(selection.detail), 20)

    def test_format_keeps_publisher_and_fetch_channel_separate(self):
        selection = select_headlines([item("央行发布政策", 2)], datetime(2026, 10, 23, 12))
        text = format_headlines(selection, datetime(2026, 10, 23, 12), detail=True)
        self.assertIn("来源：第一财经", text)
        self.assertIn("抓取渠道：东方财富", text)
        self.assertIn("https://example.com/news", text)


class NewsClientTests(unittest.TestCase):
    def test_parse_eastmoney_preserves_publisher_and_url(self):
        payload = json.dumps({"data": {"list": [{
            "title": "央行发布政策",
            "showtime": "2026-10-23 10:02:00",
            "media": "第一财经",
            "url": "//example.com/east",
        }]}})
        rows = parse_eastmoney(payload)
        self.assertEqual(rows[0].publisher_source, "第一财经")
        self.assertEqual(rows[0].fetch_channel, "EASTMONEY")
        self.assertEqual(rows[0].url, "https://example.com/east")

    def test_parse_sina_jsonp_preserves_separate_channel(self):
        payload = 'callback({"result":{"data":[{"title":"行业订单增加","ctime":"2026-10-23 11:00:00","media_name":"财联社","url":"https://example.com/sina"}]}})'
        rows = parse_sina(payload)
        self.assertEqual(rows[0].publisher_source, "财联社")
        self.assertEqual(rows[0].fetch_channel, "SINA")

    def test_fetch_uses_sina_only_when_eastmoney_has_no_today_items(self):
        east = json.dumps({"data": {"list": [{
            "title": "旧闻", "showtime": "2026-10-22 10:00:00", "media": "A"
        }]}})
        sina = json.dumps({"result": {"data": [{
            "title": "今日新闻", "ctime": "2026-10-23 10:00:00", "media_name": "B"
        }]}})
        calls = []

        def fetcher(url, **kwargs):
            calls.append(url)
            return east if "eastmoney" in url else sina

        rows = fetch_today(datetime(2026, 10, 23, 12), fetcher=fetcher)
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows[0].fetch_channel, "SINA")
        self.assertEqual(rows[0].publisher_source, "B")


class TodayHeadlinesStateTests(unittest.TestCase):
    def test_refresh_window_and_thirty_minute_interval(self):
        state = TodayHeadlinesState(last_attempt_at=datetime(2026, 10, 23, 9, 0))
        self.assertFalse(state.should_refresh(datetime(2026, 10, 23, 9, 15)))
        self.assertTrue(state.should_refresh(datetime(2026, 10, 23, 9, 30)))
        self.assertFalse(state.should_refresh(datetime(2026, 10, 23, 8, 29)))
        self.assertFalse(state.should_refresh(datetime(2026, 10, 23, 22, 1)))
        self.assertTrue(state.should_refresh(datetime(2026, 10, 23, 22, 1), force=True))

    def test_display_items_hide_older_news(self):
        state = TodayHeadlinesState(items=[item("今日", 1), item("昨日", 1, day=22)])
        self.assertEqual([row.title for row in state.display_items(datetime(2026, 10, 23))], ["今日"])

    def test_state_round_trip_retains_last_valid_items(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "headlines.json"
            state = TodayHeadlinesState(
                items=[item("今日", 1)],
                updated_at=datetime(2026, 10, 23, 12),
                last_attempt_at=datetime(2026, 10, 23, 12),
            )
            save_state(path, state)
            loaded = load_state(path)
            self.assertEqual(loaded.items[0].title, "今日")
            self.assertEqual(loaded.updated_at, datetime(2026, 10, 23, 12))


if __name__ == "__main__":
    unittest.main()
