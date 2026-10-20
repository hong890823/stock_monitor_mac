#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块龙头状态机与候选推荐的口径测试。

锁的是几条容易写反的规则：
- 封死 = 贴涨停价 **且** 卖一为空，触板未封不算；
- 退潮是**双确认**（跌出价格带 + 跌破分时均价线），只破一项不得恢复做T；
- 强承接要连续满 5 分钟才定性；
- 回封会开新一段，**再炸板要能二次提醒**（重新武装必须真的可达）；
- 跨日状态清空。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sector_leaders import (
    FADE_CONFIRM_ROUNDS,
    STRONG_HOLD_SECONDS,
    HoldingSectorStore,
    Phase,
    SectorLeaderStore,
    LeaderGuard,
    Tick,
    effective_limit_pct,
    format_leader_menu_label,
    limit_up_price,
    parse_holding_sector_config,
    parse_leader_list_text,
    rank_leader_candidates,
)


def tick(code="sh600487", name="亨通光电", price=50.25, prev_close=45.68,
         ask1_vol=0.0, ask1_price=0.0, amount=8.0e9, volume=1.65e8):
    """默认 vwap ≈ 48.48（贴 11:30 实盘口径），便于构造「站上/跌破均价线」。"""
    return Tick(
        code=code, name=name, price=price, prev_close=prev_close,
        ask1_vol=ask1_vol, ask1_price=ask1_price,
        amount=amount, volume=volume,
    )


class LimitPriceTests(unittest.TestCase):
    def test_main_board_rounds_to_cent(self):
        # 亨通 45.68 × 1.1 = 50.248 → 50.25
        self.assertEqual(limit_up_price(45.68, 10.0), 50.25)

    def test_s_stock_is_the_only_five_pct(self):
        self.assertEqual(effective_limit_pct("sh600182"), 5.0)
        self.assertEqual(effective_limit_pct("sh600487"), 10.0)
        self.assertEqual(effective_limit_pct("sz300059"), 20.0)
        self.assertEqual(effective_limit_pct("bj430047"), 30.0)

    def test_sentinel_or_missing_prev_close_yields_none(self):
        self.assertIsNone(limit_up_price(0, 10.0))
        self.assertIsNone(limit_up_price(-1, 10.0))
        self.assertIsNone(limit_up_price(45.68, 0))


class SealedTests(unittest.TestCase):
    def setUp(self):
        self.guard = LeaderGuard()

    def test_at_limit_with_empty_ask_is_sealed(self):
        self.assertTrue(self.guard._is_sealed(tick()))

    def test_at_limit_but_ask_present_is_not_sealed(self):
        # 触板未封：卖一还挂着货
        self.assertFalse(self.guard._is_sealed(tick(ask1_vol=500, ask1_price=50.26)))

    def test_below_limit_with_empty_ask_is_not_sealed(self):
        self.assertFalse(self.guard._is_sealed(tick(price=50.20)))

    def test_negative_ask_price_is_unknown_not_sealed(self):
        # 异常/哨兵价不能当「卖一为空」
        self.assertFalse(self.guard._is_sealed(tick(ask1_price=-1, ask1_vol=0)))

    def test_price_far_above_limit_is_new_stock_not_sealed(self):
        # 无涨跌幅新股：价格远超按 10% 算出来的涨停价，不当作封板
        self.assertFalse(self.guard._is_sealed(tick(price=60.0)))


class PhaseTransitionTests(unittest.TestCase):
    def setUp(self):
        self.guard = LeaderGuard(strong_hold_seconds=300, fade_confirm_rounds=2)

    def _seal(self, now=0.0):
        return self.guard.update(tick(), now=now, day="2026-10-19")

    def test_first_seal_emits_event_and_suppresses(self):
        events = self._seal(now=0.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "sealed")
        self.assertEqual(self.guard.phase_of("sh600487"), Phase.SEALED)
        self.assertTrue(self.guard.is_suppressing("sh600487"))

    def test_blowout_is_silent_and_keeps_suppressing(self):
        self._seal(now=0.0)
        # 开板、但还贴着涨停（强承接区）→ 只进观察，不定性
        events = self.guard.update(tick(price=49.71, ask1_vol=500, ask1_price=49.72),
                                   now=30.0, day="2026-10-19")
        self.assertEqual(events, [])
        self.assertEqual(self.guard.phase_of("sh600487"), Phase.BLOWOUT)
        self.assertTrue(self.guard.is_suppressing("sh600487"))

    def test_strong_hold_needs_full_five_minutes(self):
        self._seal(now=0.0)
        strong_tick = tick(price=49.71, ask1_vol=500, ask1_price=49.72)
        self.guard.update(strong_tick, now=30.0, day="2026-10-19")
        # 4 分钟：还没到 5 分钟，不定性
        events = self.guard.update(strong_tick, now=30.0 + 240, day="2026-10-19")
        self.assertEqual(events, [])
        self.assertEqual(self.guard.phase_of("sh600487"), Phase.BLOWOUT)
        # 第 5 分钟：定性强承接
        events = self.guard.update(strong_tick, now=30.0 + 300, day="2026-10-19")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "strong")
        self.assertEqual(self.guard.phase_of("sh600487"), Phase.STRONG)
        self.assertTrue(self.guard.is_suppressing("sh600487"))

    def test_strong_hold_timer_resets_when_band_is_left(self):
        self._seal(now=0.0)
        strong_tick = tick(price=49.71, ask1_vol=500, ask1_price=49.72)
        self.guard.update(strong_tick, now=30.0, day="2026-10-19")
        # 中途跌出价格带但仍在均价线上方（骑墙）→ 强承接计时清零
        rider = tick(price=47.00, ask1_vol=500, ask1_price=47.01,
                     amount=46.50 * 1.65e8, volume=1.65e8)   # vwap=46.50
        self.guard.update(rider, now=120.0, day="2026-10-19")
        events = self.guard.update(strong_tick, now=30.0 + 300, day="2026-10-19")
        self.assertEqual(events, [])
        self.assertEqual(self.guard.phase_of("sh600487"), Phase.BLOWOUT)

    def test_fade_requires_both_price_and_vwap(self):
        self._seal(now=0.0)
        self.guard.update(tick(price=49.71, ask1_vol=500, ask1_price=49.72),
                          now=30.0, day="2026-10-19")
        # 只跌出价格带、但仍在均价线上方 → 不算退潮
        only_band = tick(price=47.00, ask1_vol=500, ask1_price=47.01,
                         amount=46.50 * 1.65e8, volume=1.65e8)   # vwap=46.50
        self.guard.update(only_band, now=60.0, day="2026-10-19")
        self.guard.update(only_band, now=75.0, day="2026-10-19")
        self.assertNotEqual(self.guard.phase_of("sh600487"), Phase.FADED)

        # 双确认：跌出价格带 + 跌破均价线
        faded_tick = tick(price=47.00, ask1_vol=500, ask1_price=47.01,
                          amount=48.50 * 1.65e8, volume=1.65e8)   # vwap=48.50
        events = self.guard.update(faded_tick, now=90.0, day="2026-10-19")
        self.assertEqual(events, [])          # 第一轮还不确认
        events = self.guard.update(faded_tick, now=105.0, day="2026-10-19")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "faded")
        self.assertFalse(self.guard.is_suppressing("sh600487"))

    def test_fade_skipped_when_vwap_unavailable(self):
        # 成交量为 0 → 均价不可得 → 不判退潮，避免单边误恢复做T
        self._seal(now=0.0)
        self.guard.update(tick(price=49.71, ask1_vol=500, ask1_price=49.72),
                          now=30.0, day="2026-10-19")
        no_vwap = tick(price=47.00, ask1_vol=500, ask1_price=47.01, amount=0, volume=0)
        self.guard.update(no_vwap, now=60.0, day="2026-10-19")
        self.guard.update(no_vwap, now=75.0, day="2026-10-19")
        self.assertNotEqual(self.guard.phase_of("sh600487"), Phase.FADED)

    def test_immediate_deep_fade_fires_without_strong_phase(self):
        # 炸板就深回：不该先喊「强承接」
        self._seal(now=0.0)
        faded_tick = tick(price=46.00, ask1_vol=500, ask1_price=46.01,
                          amount=48.50 * 1.65e8, volume=1.65e8)   # vwap=48.50
        self.guard.update(faded_tick, now=30.0, day="2026-10-19")
        events = self.guard.update(faded_tick, now=45.0, day="2026-10-19")
        self.assertEqual([e.kind for e in events], ["faded"])
        self.assertEqual(self.guard.phase_of("sh600487"), Phase.FADED)
        self.assertFalse(self.guard.is_suppressing("sh600487"))

    def test_reseal_rearms_so_next_blowout_can_notify_again(self):
        """回封开新一段，再炸板要能二次提醒——不能「炸过一次全天静默」。"""
        self._seal(now=0.0)
        faded_tick = tick(price=46.00, ask1_vol=500, ask1_price=46.01,
                          amount=48.50 * 1.65e8, volume=1.65e8)   # vwap=48.50
        self.guard.update(faded_tick, now=30.0, day="2026-10-19")
        self.guard.update(faded_tick, now=45.0, day="2026-10-19")
        self.assertEqual(self.guard.phase_of("sh600487"), Phase.FADED)

        # 回封 → 开新一段
        events = self.guard.update(tick(), now=1200.0, day="2026-10-19")
        self.assertEqual([e.kind for e in events], ["sealed"])
        self.assertEqual(events[0].episode, 2)

        # 再炸板 → 当日第 2 次炸板，直接恢复做T（不必等退潮双确认）
        events = self.guard.update(faded_tick, now=1230.0, day="2026-10-19")
        self.assertEqual([e.kind for e in events], ["blowout_repeat"])
        self.assertFalse(self.guard.is_suppressing("sh600487"))

    def test_first_blowout_without_fade_keeps_suppressing(self):
        """当日只炸一次板且未退潮 → 不提示做T，继续持有。"""
        self._seal(now=0.0)
        strong_tick = tick(price=49.71, ask1_vol=500, ask1_price=49.72)
        self.guard.update(strong_tick, now=30.0, day="2026-10-19")   # 炸#1
        events = self.guard.update(strong_tick, now=330.0, day="2026-10-19")
        self.assertEqual([e.kind for e in events], ["strong"])       # 只提示持有
        self.assertTrue(self.guard.is_suppressing("sh600487"))
        self.assertEqual(self.guard._states["sh600487"].blowout_count, 1)

    def test_second_blowout_releases_even_when_not_faded(self):
        """当日第 2 次炸板：即使还守在高位，也提示做T（回封不清零）。"""
        self._seal(now=0.0)
        strong_tick = tick(price=49.71, ask1_vol=500, ask1_price=49.72)
        self.guard.update(strong_tick, now=30.0, day="2026-10-19")        # 炸#1
        self.guard.update(strong_tick, now=330.0, day="2026-10-19")       # → 未退潮，持有
        self.assertTrue(self.guard.is_suppressing("sh600487"))

        self.guard.update(tick(), now=600.0, day="2026-10-19")            # 回封
        self.assertEqual(self.guard._states["sh600487"].blowout_count, 1)

        events = self.guard.update(strong_tick, now=900.0, day="2026-10-19")  # 炸#2
        self.assertEqual([e.kind for e in events], ["blowout_repeat"])
        self.assertEqual(events[0].blowout_count, 2)
        self.assertFalse(self.guard.is_suppressing("sh600487"))

    def test_blowout_count_resets_next_day(self):
        """跨日清零：昨日炸过板不影响今天的第 1 次炸板口径。"""
        self._seal(now=0.0)
        strong_tick = tick(price=49.71, ask1_vol=500, ask1_price=49.72)
        self.guard.update(strong_tick, now=30.0, day="2026-10-19")   # 昨日炸#1
        self.guard.update(strong_tick, now=330.0, day="2026-10-19")

        self._guard_reseal_next_day = self.guard.update(tick(), now=10.0, day="2026-10-20")
        self.assertEqual(self.guard._states["sh600487"].blowout_count, 0)
        events = self.guard.update(strong_tick, now=20.0, day="2026-10-20")
        self.assertEqual(self.guard._states["sh600487"].blowout_count, 1)
        # 今日第 1 次炸板：不该直接恢复做T
        self.assertNotIn("blowout_repeat", [e.kind for e in events])
        self.assertTrue(self.guard.is_suppressing("sh600487"))

    def test_same_phase_notifies_only_once_per_episode(self):
        self._seal(now=0.0)
        strong_tick = tick(price=49.71, ask1_vol=500, ask1_price=49.72)
        self.guard.update(strong_tick, now=30.0, day="2026-10-19")
        self.guard.update(strong_tick, now=330.0, day="2026-10-19")   # → STRONG
        self.assertEqual(self.guard.phase_of("sh600487"), Phase.STRONG)
        # 一直强承接：不再重复发「强承接」
        events = self.guard.update(strong_tick, now=600.0, day="2026-10-19")
        self.assertEqual(events, [])

    def test_seal_then_strong_within_ten_minutes_still_notifies(self):
        """回归：封板后约 7 分钟就定性强承接，冷却会吞掉这条合法提醒（2026-10-19 冒烟踩到）。"""
        self._seal(now=0.0)
        strong_tick = tick(price=49.71, ask1_vol=500, ask1_price=49.72)
        self.guard.update(strong_tick, now=60.0, day="2026-10-19")     # 炸板，进观察
        events = self.guard.update(strong_tick, now=60.0 + 300, day="2026-10-19")
        self.assertEqual([e.kind for e in events], ["strong"])

    def test_daily_event_cap_blocks_endless_oscillation(self):
        """回封再炸板每段各提醒一次，但每日有上限兜底极端抖动。"""
        guard = LeaderGuard(strong_hold_seconds=0, fade_confirm_rounds=1,
                            max_events_per_day=3)
        for i in range(5):
            guard.update(tick(), now=i * 100.0, day="2026-10-19")                    # 封板
            guard.update(tick(price=46.0, ask1_vol=500, ask1_price=46.01,
                              amount=48.50 * 1.65e8, volume=1.65e8),
                         now=i * 100.0 + 10, day="2026-10-19")                       # 退潮
        self.assertLessEqual(guard._states["sh600487"].event_count, 3)

    def test_day_change_clears_state(self):
        self._seal(now=0.0, )
        self.guard.update(tick(price=49.71, ask1_vol=500, ask1_price=49.72),
                          now=30.0, day="2026-10-19")
        self.assertEqual(self.guard.phase_of("sh600487"), Phase.BLOWOUT)
        events = self.guard.update(tick(price=47.00, ask1_vol=500, ask1_price=47.01),
                                   now=40.0, day="2026-10-20")
        self.assertEqual(events, [])
        self.assertEqual(self.guard.phase_of("sh600487"), Phase.NONE)
        self.assertFalse(self.guard.is_suppressing("sh600487"))


class VwapTests(unittest.TestCase):
    def test_vwap_is_amount_over_volume(self):
        t = tick(amount=7.99e9, volume=1.43e8)
        self.assertAlmostEqual(t.vwap, 7.99e9 / 1.43e8, places=4)

    def test_vwap_none_when_volume_zero(self):
        self.assertIsNone(tick(volume=0).vwap)


class RankCandidatesTests(unittest.TestCase):
    def test_top_n_by_turnover(self):
        rows = [
            {"code": "sh600522", "name": "中天科技", "amount": 3.31e9},
            {"code": "sh600487", "name": "亨通光电", "amount": 7.99e9},
            {"code": "sh600000", "name": "浦发银行", "amount": 1.2e9},
        ]
        ranked = rank_leader_candidates(rows, top_n=2)
        self.assertEqual([item["code"] for item in ranked], ["sh600487", "sh600522"])

    def test_st_and_zero_turnover_excluded(self):
        rows = [
            {"code": "sz000001", "name": "ST某某", "amount": 9e9},
            {"code": "sz000002", "name": "*ST某某", "amount": 8e9},
            {"code": "sz000003", "name": "S*ST某某", "amount": 7e9},
            {"code": "sh600001", "name": "某小票", "amount": 0},
            {"code": "sh600002", "name": "某大票", "amount": 2e9},
        ]
        ranked = rank_leader_candidates(rows, top_n=5)
        self.assertEqual([item["code"] for item in ranked], ["sh600002"])

    def test_st_filter_does_not_hit_st_substring_names(self):
        # 「东方斯特」这类含 st 字样的正常股票不能被误杀
        ranked = rank_leader_candidates(
            [{"code": "sh600003", "name": "东方斯特", "amount": 2e9}], top_n=5
        )
        self.assertEqual([item["code"] for item in ranked], ["sh600003"])

    def test_s_stock_is_not_filtered_as_st(self):
        # S 佳通是未股改 S 股、不是 ST，不按 ST 口径排除；
        # 它的 ±5% 特例由 effective_limit_pct 单独处理（见 LimitPriceTests）。
        ranked = rank_leader_candidates(
            [{"code": "sh600182", "name": "S佳通", "amount": 5e8}], top_n=5
        )
        self.assertEqual([item["code"] for item in ranked], ["sh600182"])

    def test_turnover_reported_in_yi(self):
        ranked = rank_leader_candidates(
            [{"code": "sh600487", "name": "亨通光电", "amount": 7.99e9}]
        )
        self.assertEqual(ranked[0]["turnover_yi"], 79.9)
        self.assertEqual(ranked[0]["source"], "auto")


class StoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sector_leaders.json")
        self.store = SectorLeaderStore(self.path)

    def test_pinned_sector_is_not_overwritten_by_auto(self):
        self.store.add_leader("电子信息", "sh600487", "亨通光电")
        applied = self.store.apply_auto("电子信息", [{"code": "sh999999", "name": "别的"}])
        self.assertFalse(applied)
        self.assertEqual(self.store.leaders_of("电子信息")[0]["code"], "sh600487")

    def test_unpinned_sector_takes_auto_leaders(self):
        applied = self.store.apply_auto(
            "电子信息",
            [{"code": "sh600487", "name": "亨通光电", "source": "auto", "turnover_yi": 79.9}],
        )
        self.assertTrue(applied)
        self.assertEqual(self.store.leaders_of("电子信息")[0]["code"], "sh600487")

    def test_add_and_remove_leader_pin_the_sector(self):
        self.store.apply_auto("电子信息", [{"code": "sh600487", "name": "亨通光电"}])
        self.assertFalse(self.store.is_pinned("电子信息"))
        self.store.add_leader("电子信息", "sh600522", "中天科技")
        self.assertTrue(self.store.is_pinned("电子信息"))
        self.assertEqual(len(self.store.leaders_of("电子信息")), 2)
        self.store.remove_leader("电子信息", "sh600487")
        self.assertEqual([i["code"] for i in self.store.leaders_of("电子信息")], ["sh600522"])

    def test_save_and_reload_roundtrip(self):
        self.store.add_leader("电子信息", "sh600487", "亨通光电")
        self.assertTrue(self.store.save())
        reloaded = SectorLeaderStore(self.path).load()
        self.assertEqual(reloaded["sectors"]["电子信息"]["leaders"][0]["code"], "sh600487")

    def test_load_missing_file_yields_empty(self):
        store = SectorLeaderStore(os.path.join(self.dir, "nope.json"))
        data = store.load()
        self.assertEqual(data.get("sectors"), {})


class HoldingSectorStoreTests(unittest.TestCase):
    """手动持仓归属：设过就以它为准，清掉就回到自动反查。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "holding_sectors.json")
        self.store = HoldingSectorStore(self.path)

    def test_set_and_read_sectors(self):
        self.store.set_sectors("sh600118", ["军工"], "中国卫星")
        self.assertEqual(self.store.sectors_of("sh600118"), ["军工"])
        # 代码大小写/多余空白不影响查表
        self.assertEqual(self.store.sectors_of(" SH600118 "), ["军工"])
        self.assertTrue(self.store.is_manual("sh600118"))
        self.assertFalse(self.store.is_manual("sh600522"))

    def test_set_empty_sectors_means_explicitly_none(self):
        # 从板块里摘空 ≠ 恢复自动识别：空名单是「明确无板块」，
        # 自动反查不得再把这只股塞回去（否则编辑删掉的会自己回来）。
        self.store.set_sectors("sh600118", ["军工"])
        self.store.set_sectors("sh600118", [])
        self.assertEqual(self.store.sectors_of("sh600118"), [])
        self.assertTrue(self.store.has_entry("sh600118"))
        self.assertTrue(self.store.is_manual("sh600118"))

    def test_clear_is_what_restores_auto_mapping(self):
        self.store.set_sectors("sh600118", [])
        self.assertTrue(self.store.clear("sh600118"))
        self.assertFalse(self.store.has_entry("sh600118"))
        self.assertFalse(self.store.is_manual("sh600118"))

    def test_clear_returns_whether_something_was_removed(self):
        self.store.set_sectors("sh600118", ["军工"])
        self.assertTrue(self.store.clear("sh600118"))
        self.assertFalse(self.store.clear("sh600118"))
        self.assertEqual(self.store.sectors_of("sh600118"), [])

    def test_save_and_reload_roundtrip(self):
        self.store.set_sectors("sh600118", ["军工", "航天"], "中国卫星")
        self.assertTrue(self.store.save())
        reloaded = HoldingSectorStore(self.path)
        reloaded.load()
        self.assertEqual(reloaded.sectors_of("sh600118"), ["军工", "航天"])
        self.assertEqual(reloaded.items()[0]["name"], "中国卫星")

    def test_load_missing_or_broken_file_yields_empty(self):
        self.assertEqual(HoldingSectorStore(os.path.join(self.dir, "nope.json")).load(), {"map": {}})
        broken = os.path.join(self.dir, "broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(HoldingSectorStore(broken).load(), {"map": {}})


class ParseHoldingSectorConfigTests(unittest.TestCase):
    """「持仓股, 所属板块, 龙头1|龙头2」批量配置的解析口径。"""

    def test_full_line_with_leaders(self):
        records = parse_holding_sector_config("中国卫星, 军工, 中国船舶|中航沈飞")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["stock"], "中国卫星")
        self.assertEqual(records[0]["sectors"], ["军工"])
        self.assertEqual(records[0]["leaders"], ["中国船舶", "中航沈飞"])
        self.assertFalse(records[0]["clear"])

    def test_line_without_leaders_keeps_empty_list(self):
        records = parse_holding_sector_config("中天科技, 通信设备")
        self.assertEqual(records[0]["sectors"], ["通信设备"])
        self.assertEqual(records[0]["leaders"], [])

    def test_dash_sector_clears_manual_mapping(self):
        for raw in ("中国卫星, -", "中国卫星,", "中国卫星, 清除"):
            records = parse_holding_sector_config(raw)
            self.assertTrue(records[0]["clear"], raw)
            self.assertEqual(records[0]["sectors"], [], raw)

    def test_multiple_sectors_attach_leaders_to_first_only(self):
        # 板块多个用 / 分隔：持仓挂到全部板块，龙头只认第一个（避免重复配置歧义）
        records = parse_holding_sector_config("中国卫星, 军工/航天, 中国船舶")
        self.assertEqual(records[0]["sectors"], ["军工", "航天"])
        self.assertEqual(records[0]["leaders"], ["中国船舶"])

    def test_multiple_holdings_on_one_line_share_sector_and_leaders(self):
        # 一个板块可以挂多只持仓：`中天科技|东方电缆, 通信设备, 亨通光电`
        records = parse_holding_sector_config("中天科技|东方电缆, 通信设备, 亨通光电")
        self.assertEqual([item["stock"] for item in records], ["中天科技", "东方电缆"])
        for item in records:
            self.assertEqual(item["sectors"], ["通信设备"])
            self.assertEqual(item["leaders"], ["亨通光电"])

    def test_chinese_comma_semicolon_and_comments(self):
        text = (
            "# 手动归属\n"
            "中国卫星，军工，中国船舶\n"
            "中天科技, 通信设备；东方电缆, 通信设备, 亨通光电\n"
            "\n"
        )
        records = parse_holding_sector_config(text)
        self.assertEqual([item["stock"] for item in records],
                         ["中国卫星", "中天科技", "东方电缆"])
        self.assertEqual(records[2]["leaders"], ["亨通光电"])

    def test_blank_input_yields_nothing(self):
        self.assertEqual(parse_holding_sector_config(""), [])
        self.assertEqual(parse_holding_sector_config("   \n\n"), [])


class ParseLeaderListTextTests(unittest.TestCase):
    """「编辑龙头列表」文本框：一行一只，有代码取代码、没代码整行当名称。"""

    def test_name_and_code_and_both(self):
        self.assertEqual(parse_leader_list_text("亨通光电 sh600487"), ["sh600487"])
        self.assertEqual(parse_leader_list_text("sh600487"), ["sh600487"])
        self.assertEqual(parse_leader_list_text("600487"), ["600487"])
        self.assertEqual(parse_leader_list_text("中国船舶"), ["中国船舶"])

    def test_separators_and_newlines(self):
        text = "亨通光电 sh600487\n中天科技|东方电缆、sh600150"
        self.assertEqual(parse_leader_list_text(text),
                         ["sh600487", "中天科技", "东方电缆", "sh600150"])

    def test_keeps_order_and_drops_duplicates(self):
        self.assertEqual(parse_leader_list_text("中国船舶\n中国船舶\nsh600150"),
                         ["中国船舶", "sh600150"])

    def test_blank_yields_nothing(self):
        self.assertEqual(parse_leader_list_text("  \n\n"), [])
        self.assertEqual(parse_leader_list_text(""), [])


class LeaderMenuLabelTests(unittest.TestCase):
    """菜单条目：只显示名称不显示代码；涨跌幅跟在名称后面，拿不到行情别硬写 +0.00%。"""

    def test_full_label_shows_change_turnover_and_phase(self):
        label = format_leader_menu_label(
            "亨通光电", "sh600487", change_pct=10.0,
            phase_label="封板", turnover_yi=97.87,
        )
        self.assertEqual(label, "亨通光电 +10.00% · 成交额97.87亿 · [封板]")

    def test_code_is_not_shown_when_name_known(self):
        # 菜单里代码是噪音（用户 2026-10-19 要求去掉）
        label = format_leader_menu_label("亨通光电", "sh600487", change_pct=10.0)
        self.assertNotIn("sh600487", label)

    def test_missing_quote_omits_change_not_fake_zero(self):
        label = format_leader_menu_label("亨通光电", "sh600487", change_pct=None)
        self.assertEqual(label, "亨通光电")
        self.assertNotIn("%", label)

    def test_negative_change_keeps_sign(self):
        label = format_leader_menu_label("中天科技", "sh600522", change_pct=-1.23)
        self.assertEqual(label, "中天科技 -1.23%")

    def test_missing_name_falls_back_to_code(self):
        self.assertEqual(format_leader_menu_label("", "sh600487"), "sh600487")


if __name__ == "__main__":
    unittest.main()
