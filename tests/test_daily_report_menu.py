#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「📦 持仓信息」菜单写入路径的结构测试。

重点锁两件事：
1. **父子 callback 陷阱** —— 带子菜单的 NSMenuItem 点父项不触发 callback
   （macOS 行为，项目里 ⚙️ 持仓设置 就是 callback=None）。所以父项必须
   callback=None、打开全文挂在子项上；写反了菜单看起来正常，但点了一片死寂。
2. **假 rumps 故意不提供 `MenuItem.separator()`** —— 真实 rumps 也没有这个 API
   （分隔线是 `rumps.separator` 哨兵）。代码退回旧写法会像线上一样抛
   AttributeError，而不是被测试替身悄悄接住。
"""

import threading
import unittest
from datetime import datetime
from unittest import mock

import stock_menubar
from portfolio_daily_report import (
    MENU_SUMMARY_MAX_LINES,
    build_overview,
    compute_pnl,
    format_menu_summary,
    portfolio_fingerprint,
)
from stock_menubar import StockMenuBar


class _FakeMenuItem:
    """不碰 AppKit 的菜单项替身；故意没有 `separator()`。"""

    def __init__(self, title=None, callback=None):
        self.title = title
        self.callback = callback
        self.children = []

    def add(self, item):
        self.children.append(item)

    def clear(self):
        self.children = []


class _FakeRumps:
    MenuItem = _FakeMenuItem
    separator = object()


def pos(code="sh600118", name="中国卫星", quantity=1100, cost=88.38):
    return {"code": code, "name": name, "quantity": quantity, "cost": cost}


def quote(price=52.32, prev_close=54.66):
    return {"price": price, "prev_close": prev_close,
            "change_pct": (price - prev_close) / prev_close * 100}


def result(code="sh600118", name="中国卫星", quantity=1100, cost=88.38,
           price=52.32, signal="减仓风险", score=-6, trend_weak=True, ma20=52.86,
           atr_pct=4.21, action="应对…"):
    return {
        "code": code, "name": name, "quantity": quantity, "cost": cost,
        "price": price, "signal": signal, "score": score, "trend_weak": trend_weak,
        "ma20": ma20, "support": 52.86, "resistance": 60.12,
        "entry_stop_price": 49.16, "volume_ratio": 0.80,
        "volatility": {"atr": 2.21, "atr_pct": atr_pct},
        "relative_strength": -2.34,
        "price_position": {"location": "低位", "location_key": "LOW"},
        "volume_price_tag": "缩量阴跌",
        "opening": {"gap_pct": -0.84, "structure": "低开低走"},
        "entry_state": "NO_STRUCTURE",
        "benchmark_ok": None,
        "closes_below_ma20": 3,
        "action": action,
    }


class DailyReportMenuTests(unittest.TestCase):
    def _render(self, results, positions, quotes, index_pct=-1.0, mode="final"):
        app = StockMenuBar.__new__(StockMenuBar)   # 不跑 __init__，避免拉起 AppKit
        app._portfolio_info_menu_item = _FakeMenuItem("📦 持仓信息")
        pnl = compute_pnl(positions, quotes)
        overview = build_overview(results, pnl, {"index_pct": index_pct})
        app._last_daily_report = {
            "overview": overview,
            "meta": {"mode": mode, "generated_at": "2026-10-12 15:02",
                     "quote_date": "2026-10-12"},
            "text": "…",
        }
        with mock.patch.object(stock_menubar, "rumps", _FakeRumps):
            app._update_daily_report_menu()
        return app._portfolio_info_menu_item

    def _one_holding(self):
        return self._render(
            [result(quantity=100, cost=10)],
            [pos(quantity=100, cost=10)],
            {"sh600118": quote(11, 10)},
        )

    def test_parent_is_not_clickable_but_child_is(self):
        """父项 callback 必须是 None（带子菜单点了不触发），打开挂在子项。"""
        item = self._one_holding()
        self.assertIsNone(item.callback)
        opener = item.children[0]
        self.assertEqual(opener.title, "📄 打开完整报告")
        self.assertIsNotNone(opener.callback)

    def test_open_action_always_present(self):
        for kwargs in (
            {"results": [], "positions": [], "quotes": {}},
            {"results": [result()], "positions": [pos()], "quotes": {"sh600118": quote()}},
        ):
            item = self._render(kwargs["results"], kwargs["positions"], kwargs["quotes"])
            titles = [c.title for c in item.children if isinstance(c, _FakeMenuItem)]
            self.assertIn("📄 打开完整报告", titles)
            self.assertEqual(titles[0], "📄 打开完整报告")

    def test_title_carries_total_and_pct(self):
        item = self._one_holding()
        self.assertTrue(item.title.startswith("📦 持仓信息"))
        self.assertIn("+100", item.title)
        self.assertIn("+10.00%", item.title)

    def test_info_lines_respect_budget(self):
        results = [result(code=f"c{i}", name=f"股{i}", cost=100, price=50, atr_pct=4.2,
                          signal="减仓风险") for i in range(8)]
        positions = [pos(code=f"c{i}", name=f"股{i}", cost=100) for i in range(8)]
        quotes = {f"c{i}": quote(50, 51) for i in range(8)}
        item = self._render(results, positions, quotes, index_pct=-2.0)
        info = [c.title for c in item.children
                if isinstance(c, _FakeMenuItem)
                and c.title != "📄 打开完整报告"
                and c.title.strip()]
        self.assertLessEqual(len(info), MENU_SUMMARY_MAX_LINES)

    def test_no_cache_state_has_open_action_and_hint(self):
        app = StockMenuBar.__new__(StockMenuBar)
        app._portfolio_info_menu_item = _FakeMenuItem("📦 持仓信息")
        app._last_daily_report = None
        with mock.patch.object(stock_menubar, "rumps", _FakeRumps):
            app._update_daily_report_menu()
        titles = [c.title for c in app._portfolio_info_menu_item.children
                  if isinstance(c, _FakeMenuItem)]
        self.assertEqual(titles[0], "📄 打开完整报告")
        self.assertTrue(any("等待" in t for t in titles))

    def test_padding_row_present_for_stable_menu_width(self):
        item = self._one_holding()
        self.assertEqual(item.children[-1].title, " " * 40)

    def test_summary_lines_come_from_the_same_formatter(self):
        """菜单摘要必须和报告用同一套聚合，别两处各写一套口径。"""
        pnl = compute_pnl([pos(quantity=100, cost=10)], {"sh600118": quote(11, 10)})
        overview = build_overview([result(quantity=100, cost=10)], pnl, {"index_pct": 0.0})
        expected = format_menu_summary(overview, {"mode": "final"})
        item = self._render([result(quantity=100, cost=10)], [pos(quantity=100, cost=10)],
                            {"sh600118": quote(11, 10)}, index_pct=0.0)
        titles = [c.title for c in item.children if isinstance(c, _FakeMenuItem)]
        for line in expected:
            self.assertIn(line, titles)


class _FakeThread:
    """只记录 spawn 了什么，不真起线程。"""

    spawned = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args
        _FakeThread.spawned.append(self)

    def start(self):
        pass


class FinalizeLockContractTests(unittest.TestCase):
    """收盘定稿补跑路径的锁契约与去重口径。

    2026-10-12 一天里踩了两个坑，都锁住：
    1. `_run_portfolio_analysis` 在 finally 里 `release()`，调用方必须先 `acquire`。
       没拿就起线程 → `RuntimeError: release unlocked lock`。
    2. 合成槽位 `YYYY-MM-DD-0900` 会和 `_last_portfolio_analysis_slot` 撞车，
       于是定稿 state 丢了也永远重出不了报告。去重只该看 state + 持仓指纹。
    """

    def _app(self, state=None):
        app = StockMenuBar.__new__(StockMenuBar)   # 不跑 __init__
        app.portfolio = {"positions": [pos()]}
        app._daily_report_state = state if state is not None else {}
        app._last_finalize_attempt_at = None
        app._portfolio_analysis_lock = threading.Lock()
        app._last_portfolio_analysis_slot = "2026-10-12-0900"   # 故意撞车
        return app

    def _run(self, app, hour, minute, day="2026-10-12"):
        frozen = datetime.strptime(f"{day} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")
        _FakeThread.spawned = []
        with mock.patch.object(stock_menubar, "threading") as fake_threading:
            fake_threading.Thread = _FakeThread
            with mock.patch.object(stock_menubar, "datetime") as fake_dt:
                fake_dt.now.return_value = frozen
                app._maybe_finalize_daily_report()
        return list(_FakeThread.spawned)

    def test_acquires_lock_before_spawning(self):
        """契约：调用方 acquire，_run_portfolio_analysis 在 finally 里 release。"""
        app = self._app()
        spawned = self._run(app, 15, 5)
        self.assertEqual(len(spawned), 1)
        self.assertTrue(app._portfolio_analysis_lock.locked())
        self.assertEqual(spawned[0].args, ("2026-10-12-0900",))

    def test_analysis_slot_collision_does_not_block_refinalize(self):
        """定稿 state 丢了（或持仓变了）必须能重出，不许被 last_slot 挡掉。"""
        app = self._app(state={})   # 无 finalized_date
        self.assertEqual(len(self._run(app, 15, 5)), 1)

        # 同一时刻再试：锁还被占着（FakeThread 没真跑），不该再 spawn
        app._last_finalize_attempt_at = datetime(2026, 10, 12, 15, 5)
        self.assertEqual(len(self._run(app, 15, 6)), 0)

    def test_finalized_today_with_same_fingerprint_is_skipped(self):
        app = self._app(state={
            "finalized_date": "2026-10-12",
            "portfolio_fingerprint": portfolio_fingerprint([pos()]),
        })
        self.assertEqual(self._run(app, 16, 0), [])

    def test_portfolio_change_after_finalize_reopens(self):
        app = self._app(state={
            "finalized_date": "2026-10-12",
            "portfolio_fingerprint": "sha1:stale",
        })
        self.assertEqual(len(self._run(app, 16, 0)), 1)

    def test_before_close_does_not_finalize(self):
        app = self._app()
        self.assertEqual(self._run(app, 14, 59), [])

    def test_retry_backoff_blocks_hammering(self):
        """定稿没写成时（如行情日期异常）别每 15 秒轰一次全量分析。"""
        app = self._app()
        self.assertEqual(len(self._run(app, 15, 5)), 1)
        # 锁被 FakeThread 占着，但即使放掉也要被退避挡住
        app._portfolio_analysis_lock.release()
        app._last_finalize_attempt_at = datetime(2026, 10, 12, 15, 8)
        self.assertEqual(len(self._run(app, 15, 10)), 0)
        # 过了退避窗口才允许重试
        app._last_finalize_attempt_at = datetime(2026, 10, 12, 15, 0)
        self.assertEqual(len(self._run(app, 15, 12)), 1)


if __name__ == "__main__":
    unittest.main()
