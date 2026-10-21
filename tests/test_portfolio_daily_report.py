#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓信息报告（菜单入口「📦 持仓信息」）的口径测试。

这份报告的毛病不会是崩，而是**数字对、判断错**——所以锁的是口径不是语法：
1. 组合涨跌幅的分母必须是「昨收持仓市值」，不是 total_capital
2. 优先级是分档元组排序，深套不单独造最高档
3. 交易日判据是行情日期，不是 weekday
4. 缺行情 / 观察股 / 成本未填 都不许被静默算成 0
"""

import unittest
from datetime import datetime

from portfolio_daily_report import (
    EXCESS_NEUTRAL_PP,
    MENU_SUMMARY_MAX_LINES,
    TIER_DEEP_BREAK,
    TIER_DEEP_WEAK,
    TIER_HOLD,
    TIER_SHALLOW_BREAK,
    TIER_T_SELL,
    TIER_TREND_WEAK,
    build_actions,
    build_overview,
    _format_one_position,
    compare_to_index,
    compute_pnl,
    day_return_pct,
    decide_report_mode,
    format_daily_report,
    format_portfolio_advice_section,
    format_t_filter_events,
    format_menu_summary,
    format_news_impact_section,
    is_deep,
    menu_title,
    portfolio_fingerprint,
    priority_sort_key,
    priority_tier,
)


# ── 造数据的小工具 ──────────────────────────────────────

def pos(code="sh600118", name="中国卫星", quantity=1100, cost=88.38):
    return {"code": code, "name": name, "quantity": quantity, "cost": cost}


def quote(price=52.32, prev_close=54.66):
    return {"price": price, "prev_close": prev_close,
            "change_pct": (price - prev_close) / prev_close * 100}


def result(code="sh600118", name="中国卫星", quantity=1100, cost=88.38,
           price=52.32, signal="减仓风险", score=-6, trend_weak=True,
           ma20=52.86, atr_pct=4.21, relative_strength=-2.34,
           entry_state="NO_STRUCTURE", benchmark_ok=None, action="应对…",
           data_as_of="2026-10-12", data_age_days=0):
    """evaluate_position 结果的最小可用替身。"""
    return {
        "code": code, "name": name, "quantity": quantity, "cost": cost,
        "price": price, "signal": signal, "score": score, "trend_weak": trend_weak,
        "ma20": ma20, "support": 52.86, "resistance": 60.12,
        "entry_stop_price": 49.16, "volume_ratio": 0.80,
        "volatility": {"atr": 2.21, "atr_pct": atr_pct},
        "relative_strength": relative_strength,
        "price_position": {"location": "低位", "location_key": "LOW"},
        "volume_price_tag": "缩量阴跌",
        "opening": {"gap_pct": -0.84, "structure": "低开低走（现价较开盘-3.47%）"},
        "entry_state": entry_state,
        "benchmark_ok": benchmark_ok,
        "data_as_of": data_as_of,
        "data_age_days": data_age_days,
        "data_as_of": data_as_of,
        "data_age_days": data_age_days,
        "closes_below_ma20": 3,
        "action": action,
    }


# ── 盈亏 ──────────────────────────────────────────────

class ComputePnlTests(unittest.TestCase):
    def test_today_and_floating(self):
        pnl = compute_pnl(
            [pos(quantity=100, cost=10.0)],
            {"sh600118": quote(price=11.0, prev_close=10.5)},
        )
        self.assertAlmostEqual(pnl["today_total"], 50.0)
        self.assertAlmostEqual(pnl["float_total"], 100.0)

    def test_day_base_is_sum_of_qty_times_prev_close(self):
        """组合涨跌幅的分母：与 today_total 必须是同一集合。"""
        pnl = compute_pnl(
            [pos(code="a", quantity=100, cost=10.0),
             pos(code="b", quantity=200, cost=5.0)],
            {"a": quote(price=11.0, prev_close=10.0),
             "b": quote(price=4.5, prev_close=5.0)},
        )
        # 100*10 + 200*5 = 2000
        self.assertAlmostEqual(pnl["day_base"], 2000.0)
        # today_total = 100*(11-10) + 200*(4.5-5) = +100 -100 = 0
        self.assertAlmostEqual(pnl["today_total"], 0.0)
        # floating = 100*(11-10) + 200*(4.5-5) = +100 -100 = 0
        self.assertAlmostEqual(pnl["float_total"], 0.0)


class TFilterReportTests(unittest.TestCase):
    def test_deferred_t_event_reports_trigger_and_current_outcome(self):
        text = format_t_filter_events([
            {
                "name": "中国卫星",
                "trigger_price": 100.0,
                "current_price": 104.0,
                "predicted_turnover": 22000.0,
                "market_change_pct": 1.1,
                "sector_change_pct": 2.0,
                "environment_status": "高成交额待确认",
                "environment_reasons": ["连续高量天数不足", "概念板块上涨占比68%"],
            }
        ])
        self.assertIn("中国卫星", text)
        self.assertIn("触发价100.00", text)
        self.assertIn("当前104.00", text)
        self.assertIn("+4.00%", text)
        self.assertIn("高成交额待确认", text)
        self.assertIn("概念板块上涨占比68%", text)

    def test_no_deferred_t_events_is_silent(self):
        self.assertEqual(format_t_filter_events([]), "")

    def test_leader_hold_event_reports_leader_evidence(self):
        """龙头类暂缓：环境栏写龙头证据，报告只描述事实、不写策略结论。"""
        text = format_t_filter_events([
            {
                "name": "中天科技",
                "trigger_price": 28.30,
                "current_price": 27.94,
                "predicted_turnover": 16607.0,
                "market_change_pct": 0.38,
                "sector_change_pct": -1.35,
                "environment_status": "龙头封板暂缓",
                "environment_reasons": ["龙头亨通光电封板暂缓"],
            }
        ])
        self.assertIn("中天科技", text)
        self.assertIn("触发价28.30", text)
        self.assertIn("龙头封板暂缓", text)
        self.assertIn("龙头亨通光电封板暂缓", text)

    def test_totals_sum_across_positions(self):
        pnl = compute_pnl(
            [pos(code="a", quantity=100, cost=10.0),
             pos(code="b", quantity=100, cost=10.0)],
            {"a": quote(price=11.0, prev_close=10.0),
             "b": quote(price=9.0, prev_close=10.0)},
        )
        self.assertAlmostEqual(pnl["today_total"], 0.0)    # +100 -100
        self.assertAlmostEqual(pnl["float_total"], 0.0)

    def test_all_missing_quote_reports_missing_not_a_zero_total(self):
        pnl = compute_pnl([pos(quantity=100, cost=10)], {})
        self.assertEqual(pnl["missing"], 1)
        self.assertIsNone(day_return_pct(pnl))
        self.assertIsNone(pnl["float_total"])

    def test_missing_quote_not_counted_into_day_base(self):
        """缺行情若混进分母，组合涨跌幅会被稀释成假的『跑赢』。"""
        pnl = compute_pnl(
            [pos(code="a", quantity=100, cost=10.0),
             pos(code="b", quantity=100, cost=10.0)],
            {"a": quote(price=11.0, prev_close=10.0)},
        )
        self.assertAlmostEqual(pnl["day_base"], 1000.0)   # 只有 a
        self.assertEqual(pnl["missing"], 1)
        self.assertIsNone(pnl["rows"][1]["today"])

    def test_rows_carry_code_for_joining(self):
        """归因列表要和 evaluate_position 结果按 code join，别靠 name 硬凑。"""
        pnl = compute_pnl([pos(code="sz000001", name="平安银行", quantity=100, cost=10)],
                          {"sz000001": quote(price=11, prev_close=10)})
        self.assertEqual(pnl["rows"][0]["code"], "sz000001")
        self.assertEqual(pnl["rows"][0]["quantity"], 100)
        self.assertAlmostEqual(pnl["rows"][0]["market_value"], 1100.0)

    def test_zero_cost_gives_no_floating(self):
        pnl = compute_pnl([pos(quantity=100, cost=0)], {"sh600118": quote(11, 10)})
        self.assertIsNone(pnl["float_total"])
        self.assertIsNone(pnl["rows"][0]["floating"])

    def test_watched_only_excluded(self):
        pnl = compute_pnl(
            [pos(code="a", quantity=100, cost=10), pos(code="b", quantity=0, cost=0)],
            {"a": quote(11, 10), "b": quote(99, 1)},
        )
        self.assertEqual(len(pnl["rows"]), 1)
        self.assertEqual(pnl["watched_only"], 1)

    def test_negative_pnl_is_preserved_not_clamped(self):
        pnl = compute_pnl([pos(quantity=100, cost=10)], {"sh600118": quote(9, 10)})
        self.assertAlmostEqual(pnl["rows"][0]["today"], -100.0)
        self.assertAlmostEqual(pnl["rows"][0]["floating"], -100.0)
        self.assertAlmostEqual(pnl["rows"][0]["pct"], -10.0)

    def test_garbage_fields_do_not_raise(self):
        """行情/配置字段随时段给 None、空串、字符串，不得打断整次报告生成。"""
        pnl = compute_pnl(
            [{"code": "a", "name": "甲", "quantity": "100", "cost": "abc"}],
            {"a": {"price": None, "prev_close": ""}},
        )
        self.assertEqual(pnl["missing"], 1)
        self.assertIsNone(pnl["float_total"])

    def test_empty_portfolio(self):
        pnl = compute_pnl([], {})
        self.assertEqual(pnl["rows"], [])
        self.assertAlmostEqual(pnl["today_total"], 0.0)
        self.assertIsNone(pnl["float_total"])


class DayReturnTests(unittest.TestCase):
    def test_return_is_amount_over_prev_market_value(self):
        pnl = compute_pnl([pos(quantity=100, cost=10)], {"sh600118": quote(price=11, prev_close=10)})
        # today=100, day_base=1000 → 10%
        self.assertAlmostEqual(day_return_pct(pnl), 10.0)

    def test_zero_base_returns_none_not_a_crash(self):
        pnl = compute_pnl([pos(quantity=100, cost=10)], {})   # 全缺行情
        self.assertIsNone(day_return_pct(pnl))

    def test_independent_of_total_capital(self):
        """total_capital=0 时也算得出来——这正是这个口径存在的理由。"""
        pnl = compute_pnl([pos(quantity=100, cost=10)], {"sh600118": quote(price=9, prev_close=10)})
        self.assertAlmostEqual(day_return_pct(pnl), -10.0)


class CompareToIndexTests(unittest.TestCase):
    def test_excess_and_verdict(self):
        got = compare_to_index(-4.8, 0.12)
        self.assertAlmostEqual(got["excess_pp"], -4.92)
        self.assertEqual(got["verdict"], "跑输")

    def test_neutral_band_is_not_called_a_win(self):
        """0.01pp 的噪声不能被讲成跑赢。"""
        got = compare_to_index(1.00, 1.00 + EXCESS_NEUTRAL_PP / 2)
        self.assertEqual(got["verdict"], "基本同步")
        got = compare_to_index(1.00, 1.00 - EXCESS_NEUTRAL_PP * 2)
        self.assertEqual(got["verdict"], "跑赢")

    def test_missing_index_is_uncomparable_not_zero(self):
        got = compare_to_index(-1.0, None)
        self.assertIsNone(got["excess_pp"])
        self.assertIn("无法比较", got["verdict"])

    def test_missing_portfolio_returns_none(self):
        self.assertIsNone(compare_to_index(None, 1.0))


# ── 优先级 ────────────────────────────────────────────

class PriorityTierTests(unittest.TestCase):
    def test_break_goes_to_top_two_tiers(self):
        self.assertEqual(priority_tier(result(signal="减仓风险", cost=100, price=50, atr_pct=4.2)),
                         TIER_DEEP_BREAK)          # 浮亏 -50%，深套
        self.assertEqual(priority_tier(result(signal="减仓风险", cost=100, price=95, atr_pct=4.2)),
                         TIER_SHALLOW_BREAK)       # 浮亏 -5%，不深套

    def test_deep_loss_alone_does_not_reach_tier_zero(self):
        """深套但没破位不得霸占榜首，否则破位的盈利股反而看不见。"""
        tier = priority_tier(result(signal="持有观察", cost=100, price=50, atr_pct=4.2,
                                    trend_weak=True, ma20=60))
        self.assertEqual(tier, TIER_DEEP_WEAK)
        self.assertGreater(tier, TIER_SHALLOW_BREAK)

    def test_trend_weak_is_below_deep_weak(self):
        self.assertEqual(
            priority_tier(result(signal="趋势转弱观察", cost=100, price=95, atr_pct=4.2)),
            TIER_TREND_WEAK)

    def test_t_sell_is_an_opportunity_not_a_risk(self):
        self.assertEqual(
            priority_tier(result(signal="冲高做T", cost=100, price=100, atr_pct=4.2,
                                 trend_weak=False, ma20=90)),
            TIER_T_SELL)

    def test_plain_hold(self):
        self.assertEqual(
            priority_tier(result(signal="持有观察", cost=100, price=100, atr_pct=4.2,
                                 trend_weak=False, ma20=95)),
            TIER_HOLD)

    def test_no_cost_is_never_deep(self):
        """成本未填时 floating_pnl_pct 为 None → 不是深套，不能误判成最急。"""
        self.assertFalse(is_deep(result(cost=0, price=50, atr_pct=4.2)))


class PrioritySortTests(unittest.TestCase):
    def test_within_tier_bigger_today_loss_first(self):
        a = result(name="甲", signal="减仓风险", cost=100, price=50, atr_pct=4.2)
        b = result(name="乙", signal="减仓风险", cost=100, price=50, atr_pct=4.2)
        key_a = priority_sort_key(a, {"today": -100, "floating": -1000})
        key_b = priority_sort_key(b, {"today": -500, "floating": -1000})
        self.assertLess(key_b, key_a)

    def test_name_breaks_ties_deterministically(self):
        """输入顺序来自 json，光靠 Python 稳定排序不够。

        注意 name 比的是 **Unicode 码位**，不是拼音序 —— 别用「甲/乙」来猜顺序。
        """
        a = result(name="AAA", signal="持有观察", cost=100, price=100, atr_pct=4.2,
                   trend_weak=False, ma20=95)
        b = result(name="BBB", signal="持有观察", cost=100, price=100, atr_pct=4.2,
                   trend_weak=False, ma20=95)
        key_a = priority_sort_key(a, {"today": 0, "floating": 0})
        key_b = priority_sort_key(b, {"today": 0, "floating": 0})
        self.assertNotEqual(key_a, key_b)
        self.assertLess(key_a, key_b)
        # 输入顺序反过来，先后仍然一致
        self.assertEqual(sorted([key_b, key_a]), [key_a, key_b])

    def test_tier_outranks_amount(self):
        """小额破位仍要排在大额持有观察之前——风险优先于金额。"""
        broken = result(name="甲", signal="减仓风险", cost=100, price=95, atr_pct=4.2)
        hold = result(name="乙", signal="持有观察", cost=100, price=95, atr_pct=4.2,
                      trend_weak=False, ma20=100)
        self.assertLess(
            priority_sort_key(broken, {"today": -1.0, "floating": -1.0}),
            priority_sort_key(hold, {"today": -9999.0, "floating": -9999.0}),
        )


# ── 聚合 ──────────────────────────────────────────────

class OverviewTests(unittest.TestCase):
    def _overview(self, results, positions, quotes, index_pct=None):
        pnl = compute_pnl(positions, quotes)
        return build_overview(results, pnl, {"index_pct": index_pct, "index_name": "沪指"})

    def test_deep_and_break_cross_count(self):
        """只报『深套 5、破位 2』用户不知道有没有重叠；重叠才是最急的。"""
        ov = self._overview(
            [result(code="a", name="深套破位", cost=100, price=40, atr_pct=4.2, signal="减仓风险"),
             result(code="b", name="浅亏破位", cost=100, price=95, atr_pct=4.2, signal="减仓风险"),
             result(code="c", name="深套未破", cost=100, price=40, atr_pct=4.2, signal="持有观察")],
            [pos(code="a", name="深套破位", cost=100), pos(code="b", name="浅亏破位", cost=100),
             pos(code="c", name="深套未破", cost=100)],
            {"a": quote(40, 41), "b": quote(95, 96), "c": quote(40, 41)},
        )
        t = ov["trend"]
        self.assertEqual(t["deep_loss"], 2)
        self.assertEqual(t["risk_break"], 2)
        self.assertEqual(t["deep_and_break"], 1)

    def test_float_loss_top_is_by_amount_not_count(self):
        """19000 股浮亏 -6.4万 比 400 股浮亏 -1万 重要得多。"""
        ov = self._overview(
            [result(code="a", name="大盘子", quantity=19000, cost=5.59, price=2.23, atr_pct=3.7),
             result(code="b", name="小盘子", quantity=400, cost=46.85, price=41.61, atr_pct=3.0)],
            [pos(code="a", name="大盘子", quantity=19000, cost=5.59),
             pos(code="b", name="小盘子", quantity=400, cost=46.85)],
            {"a": quote(2.23, 2.27), "b": quote(41.61, 42.0)},
        )
        top = ov["pnl"]["top_float_loss"]
        self.assertEqual(top[0]["name"], "大盘子")
        self.assertLess(top[0]["floating"], top[1]["floating"])

    def test_market_weight_uses_market_value_not_total_capital(self):
        """total_capital=0 时 position_pct 全是 None，但市值权重永远可算。"""
        ov = self._overview(
            [result(code="a", name="大", quantity=100, cost=10, price=29),
             result(code="b", name="小", quantity=100, cost=10, price=9)],
            [pos(code="a", name="大", quantity=100, cost=10),
             pos(code="b", name="小", quantity=100, cost=10)],
            {"a": quote(price=29, prev_close=30),   # 市值 2900
             "b": quote(price=9, prev_close=10)},    # 市值 900
        )
        by_name = {it["name"]: it for it in ov["pnl"]["top_drag"]}
        self.assertAlmostEqual(by_name["大"]["weight"], 2900 / 3800 * 100, places=1)
        self.assertAlmostEqual(by_name["小"]["weight"], 900 / 3800 * 100, places=1)

    def test_share_is_of_abs_today_not_signed_total(self):
        """混用正负号时『占比』会算出 130% 这种东西。"""
        pnl = compute_pnl(
            [pos(code="a", name="亏", quantity=100, cost=10),
             pos(code="b", name="赚", quantity=100, cost=10)],
            {"a": quote(price=5, prev_close=10), "b": quote(price=11, prev_close=10)},
        )
        ov = build_overview(
            [result(code="a", name="亏", quantity=100, cost=10, price=5),
             result(code="b", name="赚", quantity=100, cost=10, price=11)],
            pnl, {"index_pct": 0.0},
        )
        shares = [it["share"] for it in ov["pnl"]["top_drag"] + ov["pnl"]["top_boost"]]
        self.assertTrue(all(s is not None and 0 <= s <= 100 for s in shares))

    def test_benchmark_ok_only_counts_explicit_false(self):
        """None 是『没判』，不是『被降级』——混算会夸大过滤影响面。"""
        ov = self._overview(
            [result(code="a", benchmark_ok=False), result(code="b", benchmark_ok=None)],
            [pos(code="a"), pos(code="b")],
            {"a": quote(10, 10), "b": quote(10, 10)},
        )
        self.assertEqual(ov["trend"]["benchmark_filtered"], 1)

    def test_empty_portfolio(self):
        ov = self._overview([], [], {})
        self.assertEqual(ov["n_holdings"], 0)
        self.assertIsNone(ov["pnl"]["today_pct"])
        self.assertEqual(ov["actions"], [])
        self.assertEqual(ov["cautions"], [])

    def test_market_timing_evidence_is_preserved_and_rendered(self):
        evidence = (
            "行情日期：2026-10-12｜时点标签：周五\n"
            "成交额：19000亿｜较前一交易日 +12.0%\n"
            "北向净流向：未取到"
        )
        pnl = compute_pnl([], {})
        overview = build_overview([], pnl, {
            "index_pct": -0.4,
            "market_timing": evidence,
        })

        self.assertEqual(overview["market_timing"], evidence)
        text = format_daily_report(
            overview, [], pnl,
            {"generated_at": "15:02", "quote_date": "2026-10-12"},
        )
        self.assertIn("市场背景", text)
        self.assertIn("市场：行情日期：2026-10-12", text)
        self.assertIn("成交额：19000亿", text)
        self.assertIn("北向净流向：未取到", text)

    def test_decision_review_is_rendered_as_dynamic_evidence(self):
        pnl = compute_pnl([], {})
        overview = build_overview([], pnl, {
            "market_environment": {
                "status": "强势延续",
                "predicted_turnover_yi": 22000,
                "market_change_pct": 1.1,
                "industry_breadth": 0.72,
                "concept_breadth": 0.68,
                "index_near_high": True,
                "persistence_days": 3,
                "persistence_average_yi": 18500,
            },
            "quote_date": "2026-10-14",
        })
        text = format_daily_report(
            overview, [], pnl,
            {"generated_at": "10:02", "quote_date": "2026-10-14"},
        )
        self.assertIn("复核：支持：", text)
        self.assertIn("失效：", text)
        self.assertIn("全天预估成交额", text)
        self.assertIn("证据：", text)
        self.assertIn("来源 TurnoverTracker", text)

    def test_empty_decision_review_does_not_print_empty_headings(self):
        pnl = compute_pnl([], {})
        overview = build_overview([], pnl, {})
        text = format_daily_report(
            overview, [], pnl,
            {"generated_at": "10:02", "quote_date": "2026-10-14"},
        )
        self.assertNotIn("复核：支持：", text)

    def test_capital_flow_evidence_is_preserved_and_rendered(self):
        evidence = "个股：中国卫星｜平台口径主力净流出 -1.2亿｜数据时间 2026-10-12 10:01"
        pnl = compute_pnl([], {})
        overview = build_overview([], pnl, {"capital_flow": evidence})

        self.assertEqual(evidence, overview["capital_flow"])
        text = format_daily_report(
            overview, [], pnl,
            {"generated_at": "10:02", "quote_date": "2026-10-12"},
        )

        self.assertIn("市场背景", text)
        self.assertIn("资金：个股：中国卫星", text)
        self.assertIn("平台口径主力净流出", text)


class NewsImpactSectionTests(unittest.TestCase):
    """「七、近两日利好/利空」：只显示有真利好/真利空的个股。"""

    @staticmethod
    def _impact(**kwargs):
        base = {
            "code": "sh600522", "name": "中天科技",
            "direction": "无", "note": "", "evidence": [], "ai": True,
        }
        base.update(kwargs)
        return base

    def test_only_qualifying_stocks_are_listed(self):
        lines = format_news_impact_section([
            self._impact(),
            self._impact(
                code="sh600999", name="招商证券", direction="利空",
                note="股东减持，短期供给增加",
                evidence=[{"source": "公告", "date": "2026-10-21", "title": "减持计划公告"}],
            ),
        ])
        text = "\n".join(lines)
        self.assertIn("七、近两日利好/利空", text)
        self.assertIn("利空 招商证券：股东减持，短期供给增加", text)
        self.assertNotIn("中天科技", text)
        self.assertIn("公告 2026-10-21「减持计划公告」", text)
        self.assertIn("DeepSeek 判定", text)

    def test_all_none_reports_a_single_line(self):
        lines = format_news_impact_section([self._impact(), self._impact(code="x")])
        text = "\n".join(lines)
        self.assertIn("近两日无明确利好/利空事项。", text)
        self.assertNotIn("中天科技", text)

    def test_degraded_fallback_marks_keyword_scope(self):
        lines = format_news_impact_section([
            self._impact(direction="利空", ai=False, note="公告口径判定为利空"),
        ])
        self.assertIn("公司公告关键词判定", "\n".join(lines))

    def test_negative_sorts_before_positive(self):
        lines = format_news_impact_section([
            self._impact(code="a", name="甲", direction="利好"),
            self._impact(code="b", name="乙", direction="利空"),
        ])
        self.assertLess(
            "\n".join(lines).index("利空 乙"),
            "\n".join(lines).index("利好 甲"),
        )

    def test_report_without_impact_omits_section(self):
        pnl = compute_pnl([], {})
        overview = build_overview([], pnl, {})
        text = format_daily_report(overview, [], pnl, {"generated_at": "10:02"})
        self.assertNotIn("七、近两日利好/利空", text)

    def test_report_with_impact_appends_section_after_detail(self):
        pnl = compute_pnl([], {})
        overview = build_overview([], pnl, {})
        text = format_daily_report(
            overview, [], pnl, {"generated_at": "10:02"},
            news_impact=[self._impact(direction="利好", note="回购护盘")],
        )
        self.assertIn("七、近两日利好/利空", text)
        self.assertIn("利好 中天科技：回购护盘", text)
        # 章节在逐股明细之后、口径脚注之前
        self.assertLess(text.index("六、逐股明细"), text.index("七、近两日利好/利空"))
        self.assertLess(text.index("七、近两日利好/利空"), text.index("口径：今日盈亏"))


class PortfolioAdviceTests(unittest.TestCase):
    def test_high_risk_portfolio_recommends_reducing_total_exposure(self):
        text = "\n".join(format_portfolio_advice_section({
            "n_holdings": 4,
            "trend": {"below_ma20": 4, "risk_break": 3, "deep_loss": 2},
            "conclusion": "组合同步走弱。",
        }))

        self.assertIn("三、持仓整体操作建议", text)
        self.assertIn("整体操作：降低总仓位，暂停新增资金", text)
        self.assertIn("执行顺序：先处置破位和深套持仓", text)
        self.assertIn("不做组合级补仓摊平", text)

    def test_safe_portfolio_without_addition_recommends_observation(self):
        text = "\n".join(format_portfolio_advice_section({
            "n_holdings": 2,
            "trend": {"below_ma20": 0, "risk_break": 0, "deep_loss": 0},
            "conclusion": "组合风险可控。",
        }))

        self.assertIn("整体操作：维持现有仓位，继续观察", text)
        self.assertIn("暂不整体加仓", text)

    def test_partially_risky_portfolio_controls_position_and_stops_addition(self):
        text = "\n".join(format_portfolio_advice_section({
            "n_holdings": 4,
            "trend": {"below_ma20": 1, "risk_break": 1, "deep_loss": 0},
            "conclusion": "风险集中于少数持仓。",
        }))

        self.assertIn("整体操作：控制现有总仓位，暂停整体补仓", text)
        self.assertIn("执行顺序：风险持仓 → 其余持有观察", text)
        self.assertIn("新增仓位保持为0", text)

    def test_empty_portfolio_does_not_invent_position_advice(self):
        text = "\n".join(format_portfolio_advice_section({"n_holdings": 0}))

        self.assertIn("整体操作：暂无实际持仓", text)
        self.assertNotIn("降低总仓位", text)


class ActionListTests(unittest.TestCase):
    def test_missing_quote_and_watched_do_not_enter_actions(self):
        pnl = compute_pnl(
            [pos(code="a", name="有行情", quantity=100, cost=10),
             pos(code="b", name="缺行情", quantity=100, cost=10),
             pos(code="c", name="观察股", quantity=0, cost=0)],
            {"a": quote(11, 10)},
        )
        actions = build_actions(
            [result(code="a", name="有行情", quantity=100, cost=10, price=11),
             result(code="c", name="观察股", quantity=0, cost=0)],
            pnl,
        )
        self.assertEqual([it["name"] for it in actions], ["有行情"])

    def test_all_break_says_systemic(self):
        """N 只同时破位是系统性信号，不是 N 个独立事件。"""
        results, positions, quotes = [], [], []
        for i in range(4):
            code = f"c{i}"
            results.append(result(code=code, name=f"股{i}", cost=100, price=50, atr_pct=4.2,
                                  signal="减仓风险"))
            positions.append(pos(code=code, name=f"股{i}", cost=100))
            quotes.append((code, quote(50, 51)))
        pnl = compute_pnl(positions, dict(quotes))
        ov = build_overview(results, pnl, {"index_pct": -2.0})
        self.assertEqual(ov["trend"]["risk_break"], 4)
        self.assertTrue(any("系统性" in c for c in ov["cautions"]))


# ── 定稿 guard ────────────────────────────────────────

class PortfolioFingerprintTests(unittest.TestCase):
    def test_stable_across_key_order_and_float_noise(self):
        a = [pos(code="a", quantity=100.0000001, cost=10.0000004)]
        b = [{"cost": 10.0, "quantity": 100.0, "code": "a", "name": "换了名字"}]
        self.assertEqual(portfolio_fingerprint(a), portfolio_fingerprint(b))

    def test_quantity_change_changes_fingerprint(self):
        self.assertNotEqual(
            portfolio_fingerprint([pos(quantity=100)]),
            portfolio_fingerprint([pos(quantity=200)]),
        )

    def test_cost_change_changes_fingerprint(self):
        self.assertNotEqual(
            portfolio_fingerprint([pos(cost=10)]),
            portfolio_fingerprint([pos(cost=10.5)]),
        )


class DecideReportModeTests(unittest.TestCase):
    def _at(self, hour, minute, day="2026-10-12"):
        return datetime.strptime(f"{day} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")

    def test_before_close_is_intraday(self):
        self.assertEqual(
            decide_report_mode(self._at(14, 30), "2026-10-12", {}, "fp"), "intraday")

    def test_after_close_first_time_finalizes(self):
        self.assertEqual(
            decide_report_mode(self._at(15, 2), "2026-10-12", {}, "fp"), "finalize")

    def test_already_finalized_with_same_portfolio_skips(self):
        state = {"finalized_date": "2026-10-12", "portfolio_fingerprint": "fp"}
        self.assertEqual(
            decide_report_mode(self._at(16, 0), "2026-10-12", state, "fp"), "skip")

    def test_portfolio_change_after_finalize_reopens(self):
        state = {"finalized_date": "2026-10-12", "portfolio_fingerprint": "old"}
        self.assertEqual(
            decide_report_mode(self._at(16, 0), "2026-10-12", state, "new"), "finalize")

    def test_holiday_quote_date_blocks_finalize(self):
        """节假日时新浪返回上一交易日价格，weekday 仍是工作日。
        拿它去『定稿今天』是错的 —— 唯一可靠判据是行情日期。"""
        self.assertEqual(
            decide_report_mode(self._at(15, 2), "2026-10-09", {}, "fp"), "skip")
        self.assertEqual(
            decide_report_mode(self._at(15, 2), None, {}, "fp"), "skip")

    def test_weekend_rolls_quote_date_back_to_friday(self):
        """周日新浪行情的 date 停在周五，于是 quote_date != today → skip。
        这正是我们想要的；不存在「quote_date 等于周日」的行情。"""
        self.assertEqual(
            decide_report_mode(self._at(15, 2, day="2026-10-11"), "2026-10-09", {}, "fp"),
            "skip")

    def test_quote_date_is_the_sole_trading_day_authority(self):
        """weekday 说工作日、行情说今天 —— 以行情为准，照常定稿。"""
        self.assertEqual(
            decide_report_mode(self._at(15, 2), "2026-10-12", {}, "fp"), "finalize")


# ── 渲染不抛异常 ──────────────────────────────────────

class FormatterRobustnessTests(unittest.TestCase):
    def _render(self, results, positions, quotes, index_pct=None, meta=None):
        pnl = compute_pnl(positions, quotes)
        ov = build_overview(results, pnl, {"index_pct": index_pct})
        return format_daily_report(ov, results, pnl, meta or {"generated_at": "15:02"})

    def test_empty_portfolio_renders(self):
        text = self._render([], [], {})
        self.assertIn("尚未配置实际持仓", text)
        self.assertIn("不构成投资建议", text)

    def test_watched_only_renders(self):
        text = self._render([], [pos(quantity=0, cost=0)], {})
        self.assertIn("观察股", text)

    def test_single_holding_does_not_say_top_three(self):
        text = self._render(
            [result(quantity=100, cost=10)],
            [pos(quantity=100, cost=10)],
            {"sh600118": quote(11, 10)},
        )
        self.assertIn("拖累前1", text)
        self.assertNotIn("拖累前3", text)

    def test_all_break_renders_without_divide_by_zero(self):
        results = [result(code=f"c{i}", name=f"股{i}", cost=100, price=50, atr_pct=4.2,
                          signal="减仓风险") for i in range(3)]
        positions = [pos(code=f"c{i}", name=f"股{i}", cost=100) for i in range(3)]
        quotes = {f"c{i}": quote(50, 51) for i in range(3)}
        text = self._render(results, positions, quotes, index_pct=-2.0)
        self.assertIn("破位", text)
        self.assertIn("系统性", text)

    def test_garbage_fields_do_not_raise(self):
        bad = result()
        bad.update({
            "score": None, "quantity": "abc", "cost": "", "price": None,
            "ma20": "x", "relative_strength": None, "volume_ratio": None,
            "volatility": None, "opening": None, "price_position": None,
            "action": None, "entry_stop_price": None,
        })
        self._render([bad], [pos()], {"sh600118": quote(11, 10)})

    def test_no_action_text_is_fine(self):
        text = self._render(
            [result(action="")], [pos(quantity=100, cost=10)], {"sh600118": quote(11, 10)})
        self.assertNotIn("应对：", text)

    def test_disclaimer_mentions_ex_rights(self):
        text = self._render([], [], {})
        self.assertIn("除权", text)


class CompactPositionDetailTests(unittest.TestCase):
    def test_position_detail_shows_hold_continuation_strategy(self):
        res = result()
        res["continuation_hold"] = {
            "state": "HOLD_CONTINUATION",
            "state_label": "持有延续",
            "keep_observation_position": True,
        }
        res["entry_structure"] = {
            "trend_assessment": {
                "short_trend": "近期向上",
                "major_trend": "大趋势向上",
                "conflict": "长短趋势同步向上",
            },
            "high_position": {"state": "非高位"},
        }
        text = "\n".join(_format_one_position(res, {"today": -120, "pct": -3.0}))
        self.assertIn("持有策略：持有延续", text)
        self.assertIn("保留观察仓", text)

    def test_hold_strategy_exit_copy_follows_exit_directive_flag(self):
        # 2026-10-22 招商证券：持有策略行原样输出「不保留确定观察仓」劝退。
        # exit_directive_ok=False（基本面非红灯+价低+仓小）时必须降级为风险事实。
        res = result()
        res["exit_directive_ok"] = False
        res["continuation_hold"] = {
            "state": "DISTRIBUTION_RISK",
            "state_label": "派发风险",
            "keep_observation_position": False,
        }
        res["entry_structure"] = {
            "trend_assessment": {
                "short_trend": "近期向下",
                "major_trend": "大趋势向下",
                "conflict": "长短趋势同步向下",
            },
            "high_position": {"state": "非高位"},
        }
        text = "\n".join(_format_one_position(res, {"today": -120, "pct": -3.0}))
        self.assertNotIn("不保留确定观察仓", text)
        self.assertIn("仅提示风险事实", text)

    def test_strong_risk_shows_daily_ready_without_trade_execution(self):
        res = result()
        res["entry_structure"] = {
            "can_enter": True,
            "strong_continuation": {
                "state": "M5_BREAK_RISK",
                "risk_line": "MA5",
                "risk_price": 11.0,
                "confirmed": True,
            },
            "trend_assessment": {
                "short_trend": "近期向上",
                "major_trend": "大趋势向上",
                "conflict": "长短趋势同步向上",
            },
            "high_position": {"state": "非高位"},
        }

        text = "\n".join(_format_one_position(res, {"today": -120, "pct": -3.0}))
        self.assertIn("日线就绪（非交易执行）", text)
        self.assertIn("跌破五日线风险", text)
        self.assertNotIn("自动卖出", text)

    def test_trend_and_high_position_precede_action(self):
        res = result()
        res["entry_structure"] = {
            "trend_assessment": {
                "short_trend": "近期向上",
                "major_trend": "大趋势向上",
                "conflict": "长短趋势同步向上",
                "evidence": {
                    "box_upper": 11.2,
                    "box_lower": 9.8,
                    "box_width_pct": 14.3,
                },
            },
            "high_position": {"state": "非高位"},
            "trade_permissions": {"allow_normal_buy": True},
        }

        lines = _format_one_position(res, {"today": -120, "pct": -3.0})

        text = "\n".join(lines)
        self.assertIn("短期趋势：近期向上", text)
        self.assertIn("大趋势：大趋势向上", text)
        self.assertIn("箱体上沿 11.20", text)
        self.assertIn("箱体下沿 9.80", text)
        self.assertIn("高位结构：非高位", text)
        self.assertLess(text.index("大趋势"), text.index("动作："))
        self.assertLess(text.index("高位结构"), text.index("动作："))

    def test_details_use_four_lines_and_drop_repeated_framework(self):
        res = result(
            signal="减仓风险",
            action=(
                "已有效跌破 M20、趋势转弱，不加仓、不摊平。浮亏较深，"
                "基本面亮红灯（经营现金流偏弱）：倾向剧本①；"
                "①逻辑破坏：反弹压力分批离场；②逻辑还在：站回支撑小仓补。"
                " 今日情境：近20日跑输大盘8.0pp。"
            ),
        )
        res["situation_notes"] = ["近20日跑输大盘8.0pp"]
        row = {
            "today": -120, "pct": -3.0, "floating": -420, "weight": 12.5,
        }

        lines = _format_one_position(res, row)

        self.assertEqual(len(lines), 4)
        self.assertIn("基本面亮红灯", lines[2])
        self.assertIn("动作：", lines[3])
        self.assertIn("风险观察", lines[3])
        self.assertIn("不作卖出处置", lines[3])
        self.assertIn("近20日跑输大盘8.0pp", lines[3])
        self.assertNotIn("不加仓、不摊平", "\n".join(lines))


class CompactMarketContextTests(unittest.TestCase):
    def test_market_timing_capital_and_review_share_one_section(self):
        pnl = compute_pnl([], {})
        overview = build_overview([], pnl, {
            "market_timing": (
                "行情日期：2026-10-15｜时点标签：无明确窗口标签\n"
                "沪指今日 -1.59%\n成交额：16330亿｜较前一交易日 +15.5%"
            ),
            "capital_flow": "个股：中国卫星｜主力净流出 -1.2亿",
            "market_environment": {
                "status": "弱势延续",
                "predicted_turnover_yi": 16330,
                "market_change_pct": -1.59,
                "industry_breadth": 0.03,
            },
            "quote_date": "2026-10-15",
        })

        text = format_daily_report(
            overview, [], pnl,
            {"generated_at": "15:02", "quote_date": "2026-10-15"},
        )

        self.assertIn("市场背景", text)
        self.assertIn("市场：行情日期：2026-10-15", text)
        self.assertIn("资金：个股：中国卫星", text)
        self.assertIn("复核：", text)
        self.assertNotIn("市场时点与资金证据", text)
        self.assertNotIn("盘中资金流辅助证据", text)
        self.assertNotIn("市场判断复核", text)
        self.assertNotIn("市场证据卡", text)


class CompactFullReportTests(unittest.TestCase):
    def test_twelve_holdings_stay_complete_but_fit_compact_line_budget(self):
        results, positions, quotes = [], [], {}
        for index in range(12):
            code = f"c{index:02d}"
            name = f"持仓{index + 1}"
            res = result(
                code=code, name=name, quantity=100, cost=100, price=50,
                atr_pct=4.2, signal="减仓风险",
                action=(
                    "已有效跌破 M20、趋势转弱，不加仓、不摊平。"
                    "基本面中性：按风险承受能力选择剧本；"
                    "①逻辑破坏：反弹压力分批离场；②逻辑还在：站回支撑小仓补。"
                ),
            )
            res["situation_notes"] = [f"{name}近20日跑输大盘{index + 1}.0pp"]
            results.append(res)
            positions.append(pos(code=code, name=name, quantity=100, cost=100))
            quotes[code] = quote(50, 51)

        pnl = compute_pnl(positions, quotes)
        overview = build_overview(results, pnl, {"index_pct": -2.0})
        text = format_daily_report(
            overview, results, pnl, {"generated_at": "15:02", "quote_date": "2026-10-15"},
        )

        self.assertLessEqual(len(text.splitlines()), 110)
        for index in range(1, 13):
            self.assertIn(f"持仓{index}", text)
        self.assertLessEqual(text.count("不加仓、不摊平"), 1)
        priority = text.split("五、优先处理", 1)[1].split("六、逐股明细", 1)[0]
        self.assertNotIn("     今日：", priority)


class AttributionTests(unittest.TestCase):
    """今日归因：这堆亏损是不是同一件事。数分布答不了「为什么亏」。"""

    def _attr(self, results, positions, quotes, index_pct=None):
        pnl = compute_pnl(positions, quotes)
        ov = build_overview(results, pnl, {"index_pct": index_pct})
        return ov, ov["attribution"]

    def test_all_down_is_flagged_as_systemic_beta(self):
        results, positions, quotes = [], [], {}
        for i in range(4):
            code = f"c{i}"
            results.append(result(code=code, name=f"股{i}", cost=100, price=50, atr_pct=4.2))
            positions.append(pos(code=code, name=f"股{i}", cost=100))
            quotes[code] = quote(50, 51)
        _, attr = self._attr(results, positions, quotes, index_pct=-1.0)
        self.assertTrue(attr["all_down"])
        self.assertEqual(attr["down"], 4)

    def test_mixed_is_not_called_systemic(self):
        _, attr = self._attr(
            [result(code="a", name="涨", cost=10, price=11, atr_pct=4.2),
             result(code="b", name="跌", cost=10, price=9, atr_pct=4.2)],
            [pos(code="a", name="涨", cost=10), pos(code="b", name="跌", cost=10)],
            {"a": quote(11, 10), "b": quote(9, 10)},
            index_pct=0.0,
        )
        self.assertFalse(attr["all_down"])
        self.assertFalse(attr["mostly_down"])   # 5/5 对半不算系统性
        self.assertEqual(attr["up"], 1)
        self.assertEqual(attr["down"], 1)

    def test_eleven_of_twelve_down_is_systemic_not_divergent(self):
        """11/12 收跌说「个股分化」是把日子讲反了 —— 冒烟抓到的判断错误。"""
        results, positions, quotes = [], [], {}
        for i in range(12):
            code = f"c{i}"
            up = (i == 0)   # 只有第 0 只抗跌
            price = 11 if up else 9
            results.append(result(code=code, name=f"股{i}", cost=10,
                                  price=price, atr_pct=4.2))
            positions.append(pos(code=code, name=f"股{i}", cost=10))
            quotes[code] = quote(price, 10)
        ov, attr = self._attr(results, positions, quotes, index_pct=-1.0)
        self.assertFalse(attr["all_down"])
        self.assertTrue(attr["mostly_down"])
        self.assertEqual(attr["up_names"], ["股0"])
        text = ov["conclusion"]
        self.assertIn("系统性走弱", text)
        self.assertIn("股0", text)
        # 不许把这种日子归成「个股行情」（文案里的「而非个股分化」是反证，不算）
        self.assertNotIn("属个股行情", text)

    def test_beta_alpha_split_uses_two_point_band(self):
        """超跌/跟跌/抗跌按 ±2pp 分档 —— 混进「跌了」就分不开 β 和 α。"""
        _, attr = self._attr(
            [result(code="a", name="超跌", cost=10, price=8, atr_pct=4.2),
             result(code="b", name="跟跌", cost=10, price=9.8, atr_pct=4.2),
             result(code="c", name="抗跌", cost=10, price=11, atr_pct=4.2)],
            [pos(code="a", name="超跌", cost=10), pos(code="b", name="跟跌", cost=10),
             pos(code="c", name="抗跌", cost=10)],
            {"a": quote(8, 10), "b": quote(9.8, 10), "c": quote(11, 10)},
            index_pct=-2.0,
        )
        buckets = attr["vs_index"]
        # 超跌 -20% vs 指数 -2% → -18pp（<-2，lag）
        self.assertEqual([i["name"] for i in buckets["lag"]], ["超跌"])
        # 跟跌 -2% vs 指数 -2% → 0pp（±2 内，inline）
        self.assertEqual([i["name"] for i in buckets["inline"]], ["跟跌"])
        # 抗跌 +10% vs 指数 -2% → +12pp（>+2，lead）
        self.assertEqual([i["name"] for i in buckets["lead"]], ["抗跌"])

    def test_concentration_uses_market_value_not_total_capital(self):
        ov, attr = self._attr(
            [result(code="a", name="大", quantity=100, cost=10, price=30, atr_pct=4.2),
             result(code="b", name="小", quantity=100, cost=10, price=10, atr_pct=4.2)],
            [pos(code="a", name="大", quantity=100, cost=10),
             pos(code="b", name="小", quantity=100, cost=10)],
            {"a": quote(29, 30), "b": quote(9, 10)},
            index_pct=-1.0,
        )
        self.assertAlmostEqual(attr["concentration_top3_pct"], 100.0)  # 只有两只

    def test_conclusion_names_systemic_and_sector_spread(self):
        results, positions, quotes, smap, sq = [], [], {}, {}, {}
        for i in range(8):
            code = f"c{i}"
            results.append(result(code=code, name=f"股{i}", cost=100, price=50, atr_pct=4.2))
            positions.append(pos(code=code, name=f"股{i}", cost=100))
            quotes[code] = quote(50, 51)
            smap[code] = [f"板块{i}"]
            sq[f"板块{i}"] = {"change": -1.0}
        pnl = compute_pnl(positions, quotes)
        ov = build_overview(results, pnl, {
            "index_pct": -1.0, "sector_map": smap, "sector_quotes": sq,
        })
        self.assertTrue(ov["attribution"]["all_down"])
        self.assertEqual(ov["sectors"]["n_sectors"], 8)
        self.assertIn("系统性", ov["conclusion"])
        self.assertIn("分散在 8 个板块", ov["conclusion"])


class SectorContextTests(unittest.TestCase):
    """板块维度：要答「板块踩雷」还是「全市场」。"""

    def test_stocks_group_into_sectors_with_weight(self):
        from portfolio_daily_report import build_portfolio_sectors

        results = [
            result(code="a", name="甲", quantity=100, cost=10, price=30, atr_pct=4.2),
            result(code="b", name="乙", quantity=100, cost=10, price=10, atr_pct=4.2),
            result(code="c", name="丙", quantity=100, cost=10, price=10, atr_pct=4.2),
        ]
        sectors = build_portfolio_sectors(
            results,
            {"a": ["汽车制造"], "b": ["汽车制造"], "c": ["玻璃行业"]},
            {"汽车制造": {"change": -2.0}, "玻璃行业": {"change": 1.0}},
        )
        self.assertEqual(sectors["n_sectors"], 2)
        top = sectors["top_sector"]
        self.assertEqual(top["name"], "汽车制造")
        self.assertEqual(top["count"], 2)
        self.assertAlmostEqual(top["change"], -2.0)
        # 汽车制造 市值 3000+1000=4000 vs 玻璃 1000 → 80%
        self.assertAlmostEqual(top["weight_pct"], 80.0, places=0)
        self.assertTrue(sectors["concentrated"])

    def test_dispersed_portfolio_is_not_concentrated(self):
        from portfolio_daily_report import build_portfolio_sectors

        results = [result(code=f"c{i}", name=f"股{i}", cost=10, price=10, atr_pct=4.2)
                   for i in range(10)]
        sectors = build_portfolio_sectors(
            results, {f"c{i}": [f"板块{i}"] for i in range(10)},
            {f"板块{i}": {"change": -1.0} for i in range(10)},
        )
        self.assertEqual(sectors["n_sectors"], 10)
        self.assertFalse(sectors["concentrated"])

    def test_unclassified_stocks_are_reported_not_dropped(self):
        from portfolio_daily_report import build_portfolio_sectors

        results = [result(code="a", name="甲", cost=10, price=10, atr_pct=4.2)]
        sectors = build_portfolio_sectors(results, {}, {})
        self.assertEqual(sectors["unclassified"], ["甲"])
        self.assertEqual(sectors["n_sectors"], 0)

    def test_missing_sector_quote_does_not_print_none(self):
        from portfolio_daily_report import build_portfolio_sectors, sector_lines

        results = [result(code="a", name="甲", cost=10, price=10, atr_pct=4.2)]
        sectors = build_portfolio_sectors(results, {"a": ["玻璃行业"]}, {})
        text = "\n".join(sector_lines(sectors))
        self.assertNotIn("None", text)
        self.assertIn("--", text)

    def test_two_taxonomies_must_not_be_mixed(self):
        """新浪/同花顺分类法对不齐（49 个去后缀后只对上 7 个）。
        这里锁的是「板块名由调用方给定」——不许内部做模糊匹配。"""
        from portfolio_daily_report import build_portfolio_sectors

        results = [result(code="a", name="甲", cost=10, price=10, atr_pct=4.2)]
        sectors = build_portfolio_sectors(
            results, {"a": ["玻璃行业"]}, {"玻璃": {"change": -3.0}},
        )
        self.assertIsNone(sectors["top_sector"]["change"])  # 名字不同就是没数据


class FormatAmountTests(unittest.TestCase):
    """金额格式化：带正负号与千分位。报告里所有金额都走它。"""

    def test_positive_and_thousands(self):
        from portfolio_daily_report import _fmt_amount

        self.assertEqual(_fmt_amount(1234.4), "+1,234")
        self.assertEqual(_fmt_amount(168.0), "+168")

    def test_negative(self):
        from portfolio_daily_report import _fmt_amount

        self.assertEqual(_fmt_amount(-770.0), "-770")

    def test_zero(self):
        from portfolio_daily_report import _fmt_amount

        self.assertEqual(_fmt_amount(0.0), "+0")


class LabelAccuracyTests(unittest.TestCase):
    """标签必须和事实一致 —— 报告的毛病通常是数字对、话说错。"""

    def _text(self, **kw):
        base = dict(signal="趋势转弱观察", cost=100, price=52.32, atr_pct=4.2,
                    trend_weak=True, ma20=53.0)
        base.update(kw)
        res = result(**base)
        pnl = compute_pnl([pos(quantity=100, cost=100)], {"sh600118": quote(52.32, 54.0)})
        ov = build_overview([res], pnl, {"index_pct": -1.0})
        return format_daily_report(ov, [res], pnl, {"generated_at": "15:02"}), ov

    def test_stale_kline_is_flagged_not_sold_as_today(self):
        """日线停在远古（停牌/退市/数据源给错）时，技术位全是旧的，报告必须说出来。

        2026-10-12 实测 sh600093 的日线停在 2022-06-16（落后 1579 天），
        而报告当成今天的行情分析。
        """
        res = result(data_as_of="2022-06-16", data_age_days=1579)
        pnl = compute_pnl([pos(quantity=100, cost=100)], {"sh600118": quote(52.32, 54.0)})
        ov = build_overview([res], pnl, {"index_pct": -1.0})
        text = format_daily_report(ov, [res], pnl, {"generated_at": "15:02"})
        self.assertIn("日线停在2022-06-16", text)
        self.assertIn("落后1579天", text)

    def test_fresh_kline_is_not_flagged(self):
        res = result(data_as_of="2026-10-12", data_age_days=0)
        pnl = compute_pnl([pos(quantity=100, cost=100)], {"sh600118": quote(52.32, 54.0)})
        ov = build_overview([res], pnl, {"index_pct": -1.0})
        text = format_daily_report(ov, [res], pnl, {"generated_at": "15:02"})
        self.assertNotIn("日线停在", text)

    def test_support_is_not_called_broken_unless_it_actually_broke(self):
        """有支撑位 ≠ 已破位。曾经每行都打「破支撑」，和菜单的「破位 0」自相矛盾。"""
        text, ov = self._text()
        self.assertEqual(ov["trend"]["risk_break"], 0)
        self.assertIn("支撑 52.86", text)
        self.assertNotIn("破支撑", text)

    def test_break_says_broken(self):
        text, ov = self._text(signal="减仓风险")
        self.assertEqual(ov["trend"]["risk_break"], 1)
        self.assertIn("破支撑", text)


class MenuSummaryTests(unittest.TestCase):
    def _summary(self, results, positions, quotes, index_pct=None, meta=None):
        pnl = compute_pnl(positions, quotes)
        ov = build_overview(results, pnl, {"index_pct": index_pct})
        return ov, format_menu_summary(ov, meta or {"mode": "final", "quote_date": "2026-10-12"})

    def test_title_carries_total_and_pct(self):
        ov, _ = self._summary(
            [result(quantity=100, cost=10)], [pos(quantity=100, cost=10)],
            {"sh600118": quote(11, 10)},
        )
        title = menu_title(ov, {})
        self.assertTrue(title.startswith("📦 持仓信息"))
        self.assertIn("+100", title)
        self.assertIn("+10.00%", title)

    def test_line_budget_is_respected(self):
        results = [result(code=f"c{i}", name=f"股{i}", cost=100, price=50, atr_pct=4.2,
                          signal="减仓风险") for i in range(8)]
        positions = [pos(code=f"c{i}", name=f"股{i}", cost=100) for i in range(8)]
        quotes = {f"c{i}": quote(50, 51) for i in range(8)}
        _, lines = self._summary(results, positions, quotes, index_pct=-2.0)
        self.assertLessEqual(len(lines), MENU_SUMMARY_MAX_LINES)

    def test_no_risk_line_says_so(self):
        _, lines = self._summary(
            [result(signal="持有观察", cost=100, price=100, atr_pct=4.2,
                    trend_weak=False, ma20=95)],
            [pos(quantity=100, cost=100)],
            {"sh600118": quote(100, 100)},
        )
        self.assertTrue(any("无破位/深套风险" in line for line in lines))

    def test_empty_portfolio(self):
        _, lines = self._summary([], [], {})
        self.assertEqual(len(lines), 1)
        self.assertIn("未配置", menu_title({}, {}) + lines[0])


if __name__ == "__main__":
    unittest.main()
