#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from market_news_client import NewsItem
from stock_menubar import StockMenuBar
from today_headlines import TodayHeadlinesState


NOW = datetime(2026, 10, 23, 10)


class TodayHeadlinesServiceTests(unittest.TestCase):
    def app(self, directory):
        app = StockMenuBar.__new__(StockMenuBar)
        app.log_dir = directory
        app.today_headlines_file = str(Path(directory) / "today_headlines.json")
        app._today_headlines_report_file = str(Path(directory) / "_today_headlines.txt")
        app._today_headlines_state = TodayHeadlinesState()
        app._today_headlines_lock = threading.Lock()
        app._today_headlines_refreshing = False
        app.log = lambda *args: None
        return app

    def test_refresh_persists_current_items_and_keeps_fetch_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory)
            item = NewsItem(
                title="央行发布政策",
                published_at=NOW,
                publisher_source="第一财经",
                fetch_channel="EASTMONEY",
                url="https://example.com/news",
            )
            with patch("stock_menubar.fetch_today", return_value=[item]):
                app._refresh_today_headlines(at=NOW)
            self.assertEqual(app._today_headlines_state.items[0].publisher_source, "第一财经")
            self.assertEqual(app._today_headlines_state.items[0].fetch_channel, "EASTMONEY")
            self.assertTrue(Path(app.today_headlines_file).exists())
            text = app.today_headlines_text(detail=True)
            self.assertIn("来源：第一财经", text)
            self.assertIn("抓取渠道：东方财富", text)

    def test_failed_refresh_keeps_last_valid_items(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory)
            item = NewsItem(
                title="昨日缓存",
                published_at=NOW,
                publisher_source="第一财经",
                fetch_channel="EASTMONEY",
            )
            app._today_headlines_state.refresh_succeeded([item], NOW)
            with patch("stock_menubar.fetch_today", side_effect=RuntimeError("offline")):
                app._refresh_today_headlines(at=NOW.replace(hour=11))
            self.assertEqual(app._today_headlines_state.items[0].title, "昨日缓存")
            self.assertIn("offline", app._today_headlines_state.error)

    def test_limit_stocks_prefers_eastmoney_pool_and_marks_source(self):
        app = StockMenuBar.__new__(StockMenuBar)
        app._limit_stocks = {}
        app._build_board_map = lambda: {"浦发银行": ["银行"]}
        app.log = lambda *args: None
        app._sina_batch_quote = lambda codes: self.fail("东财成功时不应扫描新浪")
        row = {
            "code": "sh600000", "name": "浦发银行", "pct": 10.0,
            "source": "EASTMONEY_LIMIT_POOL",
        }
        result = {
            "up": [row], "down": [], "source": "EASTMONEY_LIMIT_POOL",
        }
        with patch("stock_menubar.fetch_pools", return_value=result) as fetch:
            stocks = app.get_limit_stocks(at=NOW)
        fetch.assert_called_once()
        self.assertEqual(stocks["source"], "EASTMONEY_LIMIT_POOL")
        self.assertEqual(stocks["up"][0]["boards"], ["银行"])
        self.assertEqual(stocks["up_boards"], {"银行": ["浦发银行"]})


if __name__ == "__main__":
    unittest.main()
