#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓股→板块 手动归属的接线测试。

背景：板块归属原来只认新浪反查表（`_sector_map`），归属不准且用户改不了。
本次改为「手动归属优先，自动反查兜底」。锁住三条：

- 设过手动归属的持仓股，**不再**被自动反查表拉走；
- 没设过的仍走自动反查（含 sh/sz/bj 前缀与裸代码两种写法）；
- 龙头抑制做T跟着**手动板块**走——龙头配在自定义板块名下也要管得住持仓。
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sector_leaders import (
    HoldingSectorStore,
    LeaderGuard,
    Phase,
    SectorLeaderStore,
    parse_holding_sector_config,
    parse_leader_list_text,
)
from stock_menubar import StockMenuBar


class FakeApp:
    """只装板块归属需要的最小状态，不启动 rumps。"""

    _sectors_of_holding = StockMenuBar._sectors_of_holding
    _leader_hold_for = StockMenuBar._leader_hold_for
    _apply_sector_leader_config_records = StockMenuBar._apply_sector_leader_config_records
    _replace_sector_leaders = StockMenuBar._replace_sector_leaders
    _replace_sector_holdings = StockMenuBar._replace_sector_holdings
    _leader_entry_title = StockMenuBar._leader_entry_title
    _check_sector_leaders = StockMenuBar._check_sector_leaders
    _leader_codes = StockMenuBar._leader_codes
    _holding_sector_names_for_leaders = StockMenuBar._holding_sector_names_for_leaders
    _update_leader_menu_labels = StockMenuBar._update_leader_menu_labels

    def __init__(self, tmpdir):
        self.portfolio = {
            "positions": [
                {"code": "sh600118", "name": "中国卫星", "quantity": 1100, "cost": 88.0},
                {"code": "sh600522", "name": "中天科技", "quantity": 1000, "cost": 45.0},
                {"code": "sh600884", "name": "杉杉股份", "quantity": 2700, "cost": 13.4},
            ],
        }
        # 自动反查表故意和用户想的不一样：中天被分到「电线电缆」
        self._sector_map = {
            "sh600118": ["飞机制造"],
            "600522": ["电线电缆"],
            "sh600884": ["电子信息"],
        }
        self._leader_hold_cache = {}
        self._leader_quote_cache = {}
        self._leader_guard = SimpleNamespace(phase_of=lambda code: Phase.NONE)
        self._holding_sector_store = HoldingSectorStore(
            os.path.join(tmpdir, "holding_sectors.json")
        )
        self._holding_sector_store.load()
        self._leader_store = SectorLeaderStore(os.path.join(tmpdir, "sector_leaders.json"))
        self._leader_store.load()
        self.catalog = {
            "中国卫星": ("sh600118", "中国卫星"),
            "中天科技": ("sh600522", "中天科技"),
            "亨通光电": ("sh600487", "亨通光电"),
            "中国船舶": ("sh600150", "中国船舶"),
            "杉杉股份": ("sh600884", "杉杉股份"),
        }

    def _ensure_sector_map(self):
        return self._sector_map

    def log(self, message, level="INFO"):
        return None

    def _resolve_portfolio_stock(self, keyword):
        keyword = (keyword or "").strip()
        hit = self.catalog.get(keyword)
        if hit:
            return hit
        code = keyword.lower()
        for listed_code, _name in self.catalog.values():
            if listed_code == code:
                # 代码解析且拿不到真名：名字按代码返回（真实现的兜底口径）
                return listed_code, code
        return None, None


class SectorsOfHoldingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = FakeApp(self.dir)

    def test_manual_mapping_wins_over_auto_map(self):
        self.app._holding_sector_store.set_sectors("sh600118", ["军工"], "中国卫星")
        self.assertEqual(self.app._sectors_of_holding("sh600118"), ["军工"])

    def test_unmapped_holding_falls_back_to_auto_map(self):
        self.assertEqual(self.app._sectors_of_holding("sh600522"), ["电线电缆"])

    def test_auto_map_lookup_accepts_prefixed_code(self):
        # 反查表存裸代码，查询带前缀也要对得上
        self.assertEqual(self.app._sectors_of_holding("sh600522"), ["电线电缆"])

    def test_unknown_code_yields_empty(self):
        self.assertEqual(self.app._sectors_of_holding("sz999999"), [])
        self.assertEqual(self.app._sectors_of_holding(""), [])


class LeaderHoldRoutingTests(unittest.TestCase):
    """抑制做T必须跟着手动板块走，否则自定义板块下的龙头形同虚设。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = FakeApp(self.dir)

    def test_leader_on_manual_sector_suppresses_holding(self):
        self.app._holding_sector_store.set_sectors("sh600118", ["军工"])
        self.app._leader_hold_cache = {"军工": (True, "龙头封板")}
        hold, note = self.app._leader_hold_for("sh600118")
        self.assertTrue(hold)
        self.assertEqual(note, "龙头封板")

    def test_manual_sector_blocks_leak_from_auto_sector(self):
        # 自动反查的「飞机制造」在暂缓，但用户已把该持仓改到「军工」——不能被误抑制
        self.app._holding_sector_store.set_sectors("sh600118", ["军工"])
        self.app._leader_hold_cache = {"飞机制造": (True, "龙头封板")}
        self.assertEqual(self.app._leader_hold_for("sh600118"), (False, ""))

    def test_unmapped_holding_still_routed_by_auto_sector(self):
        self.app._leader_hold_cache = {"电线电缆": (True, "龙头强承接")}
        hold, note = self.app._leader_hold_for("sh600522")
        self.assertTrue(hold)
        self.assertEqual(note, "龙头强承接")


class ApplyConfigRecordsTests(unittest.TestCase):
    """配置落地规则：龙头挂第一个板块、`-` 只清归属不碰龙头。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = FakeApp(self.dir)

    def _apply(self, text):
        return self.app._apply_sector_leader_config_records(
            parse_holding_sector_config(text)
        )

    def test_stock_sector_and_leaders_land_in_both_stores(self):
        saved, errors = self._apply("中国卫星, 军工, 中国船舶")
        self.assertEqual(errors, [])
        self.assertEqual(self.app._holding_sector_store.sectors_of("sh600118"), ["军工"])
        leaders = self.app._leader_store.leaders_of("军工")
        self.assertEqual([item["code"] for item in leaders], ["sh600150"])
        self.assertTrue(self.app._leader_store.is_pinned("军工"))
        self.assertIn("军工", saved[0])

    def test_multiple_sectors_map_holding_to_all_but_leader_goes_to_first(self):
        self._apply("中国卫星, 军工/航天, 中国船舶")
        self.assertEqual(self.app._holding_sector_store.sectors_of("sh600118"),
                         ["军工", "航天"])
        self.assertEqual([i["code"] for i in self.app._leader_store.leaders_of("军工")],
                         ["sh600150"])
        self.assertEqual(self.app._leader_store.leaders_of("航天"), [])

    def test_clear_line_removes_manual_mapping_without_touching_leaders(self):
        self._apply("中国卫星, 军工, 中国船舶")
        saved, errors = self._apply("中国卫星, -")
        self.assertEqual(errors, [])
        self.assertFalse(self.app._holding_sector_store.is_manual("sh600118"))
        # 龙头挂在板块上，清持仓归属不顺手删龙头
        self.assertEqual(len(self.app._leader_store.leaders_of("军工")), 1)

    def test_unknown_stock_voids_the_whole_line(self):
        # 持仓股都找不到，整行作废（不必再逐项报龙头）
        saved, errors = self._apply("没这票, 军工, 中国船舶")
        self.assertEqual(saved, [])
        self.assertEqual(errors, ["没这票：未找到"])
        self.assertEqual(self.app._leader_store.leaders_of("军工"), [])

    def test_unknown_leader_keeps_the_rest_of_the_line(self):
        saved, errors = self._apply("中国卫星, 军工, 也没这龙头")
        self.assertEqual(self.app._holding_sector_store.sectors_of("sh600118"), ["军工"])
        self.assertEqual(self.app._leader_store.leaders_of("军工"), [])
        self.assertEqual(errors, ["也没这龙头：龙头未找到"])
        self.assertEqual(len(saved), 1)

    def test_re_adding_same_leader_is_idempotent(self):
        self._apply("中天科技, 通信设备, 亨通光电")
        self._apply("中天科技, 通信设备, 亨通光电")
        leaders = self.app._leader_store.leaders_of("通信设备")
        self.assertEqual([item["code"] for item in leaders], ["sh600487"])


class ReplaceSectorLeadersTests(unittest.TestCase):
    """「编辑龙头列表」：整体替换板块里的个股；解析全挂时不要误清空原列表。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = FakeApp(self.dir)
        self.app._replace_sector_leaders("通信设备", ["亨通光电"])

    def test_replace_swaps_the_stock_list(self):
        names, errors = self.app._replace_sector_leaders("通信设备", ["中天科技", "东方电缆"])
        # 东方电缆不在目录里 → 报错，中天替换成功
        self.assertEqual(errors, ["东方电缆：未找到"])
        self.assertEqual(names, ["中天科技"])
        self.assertEqual(
            [i["code"] for i in self.app._leader_store.leaders_of("通信设备")],
            ["sh600522"],
        )

    def test_empty_text_clears_the_list(self):
        # 输入框被清空 = 用户就是要删光
        names, errors = self.app._replace_sector_leaders("通信设备", [])
        self.assertEqual((names, errors), ([], []))
        self.assertEqual(self.app._leader_store.leaders_of("通信设备"), [])

    def test_all_unresolved_keeps_previous_list(self):
        # 全是打错的关键字：列表不该被静默清空
        names, errors = self.app._replace_sector_leaders("通信设备", ["没这票"])
        self.assertEqual(names, [])
        self.assertEqual(errors, ["没这票：未找到"])
        self.assertEqual(
            [i["code"] for i in self.app._leader_store.leaders_of("通信设备")],
            ["sh600487"],
        )

    def test_edit_pins_sector_against_auto_recommend(self):
        self.app._replace_sector_leaders("通信设备", ["中天科技"])
        self.assertTrue(self.app._leader_store.is_pinned("通信设备"))

    def test_prefilled_name_code_line_keeps_readable_name(self):
        # 编辑框预填「亨通光电 sh600487」→ 解析只留代码，名称要沿用原列表，别退化成代码
        self.app._replace_sector_leaders("通信设备", ["亨通光电"])
        names, errors = self.app._replace_sector_leaders(
            "通信设备", parse_leader_list_text("亨通光电 sh600487")
        )
        self.assertEqual(errors, [])
        self.assertEqual(names, ["亨通光电"])
        self.assertEqual(self.app._leader_store.leaders_of("通信设备")[0]["name"], "亨通光电")


class ReplaceSectorHoldingsTests(unittest.TestCase):
    """「编辑关联持仓」：一个板块可挂多只持仓，名单外的从本板块摘掉。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = FakeApp(self.dir)

    def test_multiple_holdings_can_share_one_sector(self):
        kept, dropped, errors = self.app._replace_sector_holdings(
            "通信设备", ["中天科技", "杉杉股份"]
        )
        self.assertEqual((kept, dropped, errors), (["中天科技", "杉杉股份"], [], []))
        for code in ("sh600522", "sh600884"):
            self.assertIn("通信设备", self.app._sectors_of_holding(code))

    def test_dropped_holding_keeps_its_other_sectors(self):
        self.app._replace_sector_holdings("通信设备", ["中天科技", "杉杉股份"])
        self.app._holding_sector_store.set_sectors("sh600884", ["通信设备", "光伏"])
        kept, dropped, errors = self.app._replace_sector_holdings("通信设备", ["中天科技"])
        self.assertEqual(dropped, ["杉杉股份"])
        self.assertEqual(self.app._sectors_of_holding("sh600884"), ["光伏"])

    def test_dropping_auto_linked_holding_blocks_auto_from_readding(self):
        # 杉杉原来靠自动反查挂在「电子信息」；编辑删掉后不能自己回来
        self.assertEqual(self.app._sectors_of_holding("sh600884"), ["电子信息"])
        kept, dropped, errors = self.app._replace_sector_holdings("电子信息", [])
        self.assertEqual(dropped, ["杉杉股份"])
        self.assertEqual(self.app._sectors_of_holding("sh600884"), [])
        self.assertTrue(self.app._holding_sector_store.has_entry("sh600884"))

    def test_adding_holding_keeps_its_other_sectors(self):
        # 中国卫星手动在「军工」，再挂进「航天」不应把军工挤掉
        self.app._holding_sector_store.set_sectors("sh600118", ["军工"])
        self.app._replace_sector_holdings("航天", ["中国卫星"])
        self.assertEqual(self.app._sectors_of_holding("sh600118"), ["军工", "航天"])

    def test_all_unresolved_keeps_existing_links(self):
        self.app._replace_sector_holdings("电子信息", ["杉杉股份"])
        kept, dropped, errors = self.app._replace_sector_holdings("电子信息", ["没这票"])
        self.assertEqual((kept, dropped), ([], []))
        self.assertEqual(errors, ["没这票：未找到"])
        self.assertIn("电子信息", self.app._sectors_of_holding("sh600884"))


class FirstRoundLabelUpdateTests(unittest.TestCase):
    """非交易时段 `_check_sector_leaders` 一轮行情就要把涨跌幅写进菜单。

    回归（2026-10-19）：旧逻辑等 `tick % 4 == 0` 才刷新标题，而收盘后取数走早退、
    `_check_sector_leaders` 全天只跑首轮（tick=1）→ 涨跌幅永远进不了菜单。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = FakeApp(self.dir)
        import rumps
        self.dispatched = []
        self.app._maybe_propose_sector_leaders = lambda: None
        self.app._refresh_leader_hold_cache = lambda: None
        self.app._handle_leader_event = lambda event: None
        self.app._dispatch_to_main = lambda fn: (self.dispatched.append(fn), fn())
        self.app._sina_batch_quote = lambda codes: [{
            "code": "sh600487", "name": "亨通光电", "price": 50.25, "prev": 45.68,
            "pct": 10.0, "amount": 9.8e9, "volume": 1.9e8,
        }]
        self.app._leader_guard = LeaderGuard()
        self.app._leader_menu_tick = 0
        self.app._holding_sector_store.set_sectors("sh600522", ["通信设备"])
        self.app._leader_store.set_leaders(
            "通信设备", [{"code": "sh600487", "name": "亨通光电", "source": "manual"}]
        )
        item = rumps.MenuItem("亨通光电 sh600487", callback=None)
        self.app._leader_menu_item_refs = {("通信设备", "sh600487"): item}
        self.item = item

    def test_single_round_puts_change_pct_into_menu_title(self):
        StockMenuBar._check_sector_leaders(self.app)
        self.assertTrue(self.dispatched, "首轮必须触发一次菜单标题刷新")
        self.assertIn("+10.00%", self.item.title)
        self.assertIn("成交额98.0亿", self.item.title)
        self.assertNotIn("sh600487", self.item.title)   # 条目不显示股票代码

    def test_quote_cache_is_filled_for_later_menu_rebuilds(self):
        StockMenuBar._check_sector_leaders(self.app)
        self.assertIn("sh600487", self.app._leader_quote_cache)


class LeaderEntryTitleTests(unittest.TestCase):
    """菜单条目标题：涨跌幅与成交额都来自行情缓存（实时优先），相位来自状态机。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = FakeApp(self.dir)
        self.entry = {"code": "sh600487", "name": "亨通光电", "turnover_yi": 97.87}

    def test_shows_live_change_pct_from_quote_cache(self):
        self.app._leader_quote_cache = {"sh600487": {"pct": 10.0, "price": 50.25}}
        self.assertEqual(
            self.app._leader_entry_title(self.entry, "sh600487"),
            "亨通光电 +10.00% · 成交额97.87亿",
        )

    def test_manual_leader_without_stored_turnover_still_shows_live_amount(self):
        # 手动加的龙头没有 turnover_yi，成交额从行情缓存的累计成交额（元）现算
        self.app._leader_quote_cache = {
            "sh600150": {"pct": 6.52, "amount": 3.21e9},
        }
        entry = {"code": "sh600150", "name": "中国船舶"}
        self.assertEqual(
            self.app._leader_entry_title(entry, "sh600150"),
            "中国船舶 +6.52% · 成交额32.1亿",
        )

    def test_live_amount_wins_over_stale_recommend_snapshot(self):
        # 推荐时存的成交额是 10:00 的快照，盘中显示必须用最新累计成交额
        self.app._leader_quote_cache = {"sh600487": {"pct": 10.0, "amount": 1.234e10}}
        self.assertIn("成交额123.4亿", self.app._leader_entry_title(self.entry, "sh600487"))
        self.assertNotIn("97.87", self.app._leader_entry_title(self.entry, "sh600487"))

    def test_no_quote_yet_omits_change_without_zero_fill(self):
        self.assertNotIn("%", self.app._leader_entry_title(self.entry, "sh600487"))

    def test_phase_label_appears_once_state_machine_reports(self):
        self.app._leader_guard = SimpleNamespace(
            phase_of=lambda code: Phase.SEALED
        )
        self.assertIn("[封板]", self.app._leader_entry_title(self.entry, "sh600487"))


if __name__ == "__main__":
    unittest.main()
