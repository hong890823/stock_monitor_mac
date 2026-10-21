#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓分析口径测试：破位优先于转弱、深套两套剧本、浅亏反抽确认。"""

import unittest

from portfolio_analysis import evaluate_position


def _kline_frames(start="2026-07-01", n=80):
    """造 n 根日线；start=None 表示「到今天为止」。"""
    from datetime import datetime, timedelta
    import pandas as pd

    end = datetime.now() if start is None else datetime.strptime(start, "%Y-%m-%d") + timedelta(days=n - 1)
    days = [(end - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d") for i in range(n)]
    closes = [10.0 + i * 0.05 for i in range(n)]
    return pd.DataFrame({
        "date": days,
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
    })


class _Health:
    def __init__(self, level="ALERT", reasons=None):
        self.level = level
        self.reasons = reasons or ["评分 40（较差）"]


def _technical(price=30.0, support=30.5, resistance=32.5, trend_weak=False,
               price_location=None, continuation_hold=None):
    technical = {
        "price": price,
        "support": support,
        "resistance": resistance,
        "score": -1,
        "volume_ratio": 1.0,
        "price_change": -1.2,
        "atr": 0.9,
        "entry_structure": {
            "state": "NO_STRUCTURE",
            "can_buy": False,
            "trend_weak": trend_weak,
            "bias_pct": 0.0,
            "prior_high": resistance,
            "stop_price": None,
            "state_label": "暂未形成有效入场结构",
            "reasons": [],
            "blockers": [],
        },
    }
    if continuation_hold:
        technical["continuation_hold"] = continuation_hold
    if price_location:
        technical["price_position"] = {"location_key": price_location}
    return technical


def _evaluate(cost=45.0, price=30.0, support=30.5, trend_weak=False,
              health=None, price_location=None, continuation_hold=None):
    return evaluate_position(
        position={"code": "sh600522", "name": "中天科技", "quantity": 500, "cost": cost},
        technical=_technical(
            price=price,
            support=support,
            trend_weak=trend_weak,
            price_location=price_location,
            continuation_hold=continuation_hold,
        ),
        live_data={},
        portfolio={
            "total_capital": 400_000,
            "available_capital": 100_000,
            "initial_entry_pct": 5,
            "max_position_pct": 20,
        },
        fundamental_health=health,
    )


class TestBreakPriority(unittest.TestCase):
    def test_broken_support_wins_over_trend_weak(self):
        # 深套股几乎都跌破 M20：破位剧本必须优先，不能被转弱话术盖住。
        result = _evaluate(trend_weak=True)
        self.assertEqual(result["signal"], "减仓风险")
        self.assertIn("已有效跌破 M20", result["action"])
        self.assertIn("①逻辑/基本面已破坏", result["action"])

    def test_trend_weak_still_used_without_break(self):
        result = _evaluate(price=31.0, support=30.5, trend_weak=True)
        self.assertEqual(result["signal"], "趋势转弱观察")


class TestContinuationHold(unittest.TestCase):
    def test_intact_first_wave_keeps_observation_position(self):
        result = _evaluate(
            price=31.0,
            continuation_hold={
                "state": "HOLD_CONTINUATION",
                "keep_observation_position": True,
                "can_trim": False,
            },
        )
        self.assertEqual(result["continuation_state"], "HOLD_CONTINUATION")
        self.assertIn("保留观察仓", result["action"])
        self.assertNotIn("约减出", result["action"])

    def test_extended_first_wave_suggests_partial_profit_not_full_exit(self):
        result = _evaluate(
            price=32.2,
            continuation_hold={
                "state": "PROFIT_PROTECTION",
                "keep_observation_position": True,
                "can_trim": True,
                "risk_line": "MA5",
                "risk_price": 30.8,
            },
        )
        self.assertEqual(result["continuation_state"], "PROFIT_PROTECTION")
        self.assertIn("部分兑现", result["action"])
        self.assertIn("保留观察仓", result["action"])
        self.assertNotIn("约减出 1/3", result["action"])

    def test_break_defense_wins_over_profit_protection(self):
        result = _evaluate(
            price=31.0,
            continuation_hold={
                "state": "BREAK_DEFENSE",
                "keep_observation_position": False,
                "can_trim": True,
                "risk_line": "MA5",
                "risk_price": 31.2,
            },
        )
        self.assertEqual(result["continuation_state"], "BREAK_DEFENSE")
        self.assertIn("破位防守", result["action"])
        self.assertNotIn("保留观察仓", result["action"])


class TestBreakActions(unittest.TestCase):
    def test_deep_loss_gives_two_scripts(self):
        result = _evaluate()
        self.assertEqual(result["signal"], "减仓风险")
        self.assertIn("割肉摊成本", result["action"])
        self.assertIn("②逻辑还在", result["action"])
        self.assertIn("补仓降成本", result["action"])
        self.assertIn("再创新低", result["action"])

    def test_shallow_loss_keeps_conditional_cut(self):
        result = _evaluate(cost=32.0)
        self.assertIn("不能快速收回", result["action"])
        self.assertIn("分批减仓", result["action"])

    def test_high_price_non_deep_trend_weak_escalates_to_exit_risk(self):
        result = _evaluate(
            cost=32.0,
            price=31.0,
            support=30.5,
            trend_weak=True,
            price_location="HIGH",
        )
        self.assertEqual(result["signal"], "减仓风险")
        self.assertIn("高位", result["action"])
        self.assertIn("清仓离场", result["action"])
        self.assertIn("执行可分批", result["action"])

    def test_high_price_non_deep_broken_support_does_not_wait_for_rebound(self):
        result = _evaluate(
            cost=32.0,
            price=30.0,
            support=30.5,
            trend_weak=True,
            price_location="HIGH",
        )
        self.assertEqual(result["signal"], "减仓风险")
        self.assertIn("清仓离场", result["action"])
        self.assertIn("执行可分批", result["action"])
        self.assertIn("不等反抽", result["action"])
        self.assertNotIn("反抽站不回", result["action"])

    def test_red_fundamentals_prefers_exit_script(self):
        result = _evaluate(health=_Health())
        self.assertIn("倾向剧本①", result["action"])

    def test_no_fundamentals_still_frames_two_scripts(self):
        result = _evaluate()
        self.assertIn("基本面暂无数据", result["action"])


class TestExitDirectiveGate(unittest.TestCase):
    """2026-10-22 招商证券：基本面好、位置低位、小仓位，盘中被「不保留确定观察仓」
    直接劝退。基本面非红灯 + 价格不高 + 仓位小 → 只提示风险事实，不喊撤出。"""

    @staticmethod
    def _portfolio_with_positions():
        return {
            "total_capital": 400_000,
            "available_capital": 100_000,
            "initial_entry_pct": 5,
            "max_position_pct": 20,
            "positions": [
                {"code": "sh600999", "name": "招商证券", "quantity": 1200, "cost": 16.23},
                {"code": "sh600001", "name": "大盘股", "quantity": 5000, "cost": 40.0},
            ],
        }

    def _evaluate_zhaoshang(self, health=None, price_location="LOW",
                            continuation_hold=None):
        # price 站在支撑上方：让持有策略分支（而非破位分支）出文案，
        # 正是报告「持有策略：派发风险…不保留确定观察仓」那条链路。
        return evaluate_position(
            position={"code": "sh600999", "name": "招商证券",
                      "quantity": 1200, "cost": 16.23},
            technical=_technical(
                price=16.5, support=16.08,
                price_location=price_location,
                continuation_hold=continuation_hold or {
                    "state": "DISTRIBUTION_RISK",
                    "state_label": "派发风险",
                    "keep_observation_position": False,
                    "can_trim": False,
                },
            ),
            live_data={},
            portfolio=self._portfolio_with_positions(),
            fundamental_health=health,
        )

    def test_light_low_position_gets_no_exit_directive(self):
        result = self._evaluate_zhaoshang(health=_Health(level="SOUND"))
        self.assertFalse(result["exit_directive_ok"])
        self.assertNotIn("不保留确定观察仓", result["action"])
        self.assertIn("仅提示风险事实", result["action"])
        self.assertIn("不必直接撤出", result["action"])

    def test_missing_fundamentals_still_no_exit_directive(self):
        # 基本面没数据不等于可以喊撤出——缺证据不喊撤出。
        result = self._evaluate_zhaoshang(health=None)
        self.assertFalse(result["exit_directive_ok"])
        self.assertNotIn("不保留确定观察仓", result["action"])

    def test_red_fundamentals_keep_exit_directive(self):
        result = self._evaluate_zhaoshang(health=_Health(level="ALERT"))
        self.assertTrue(result["exit_directive_ok"])
        self.assertIn("不保留确定观察仓", result["action"])

    def test_high_price_keeps_exit_directive(self):
        result = self._evaluate_zhaoshang(
            health=_Health(level="SOUND"), price_location="HIGH",
        )
        self.assertTrue(result["exit_directive_ok"])
        self.assertIn("不保留确定观察仓", result["action"])


class TestPassthroughFields(unittest.TestCase):
    """2026-10-12：result 缺 atr / benchmark_ok / price_change，下游只能空读。

    后果是 _check_intraday_signals 读 result.get("atr") 恒为 None，
    深套阈值静默退回固定 -20%，和 evaluate_position 的 ATR 缩放阈值不一致。
    """

    def test_atr_is_on_the_result(self):
        result = _evaluate()
        self.assertAlmostEqual(result["atr"], 0.9)

    def test_price_change_is_on_the_result(self):
        result = _evaluate()
        self.assertAlmostEqual(result["price_change"], -1.2)

    def test_benchmark_ok_follows_entry_structure(self):
        technical = _technical()
        technical["entry_structure"]["benchmark_ok"] = False
        result = evaluate_position(
            position={"code": "sh600522", "name": "中天科技", "quantity": 500, "cost": 45.0},
            technical=technical,
            live_data={},
            portfolio={"total_capital": 400_000, "available_capital": 0,
                       "initial_entry_pct": 5, "max_position_pct": 20},
        )
        self.assertIs(result["benchmark_ok"], False)

    def test_benchmark_ok_is_none_when_not_judged(self):
        """None 是「没判」，不是「被降级」——组合过滤影响面只数显式 False。"""
        self.assertIsNone(_evaluate()["benchmark_ok"])

    def test_atr_pct_helper_reads_result(self):
        from portfolio_daily_report import atr_pct_of

        result = _evaluate()
        # 0.9 / 30.0 = 3%
        self.assertAlmostEqual(atr_pct_of(result, 30.0), 3.0)

    def test_atr_pct_helper_prefers_volatility_block(self):
        from portfolio_daily_report import atr_pct_of

        result = _evaluate()
        result["volatility"] = {"atr": 9.9, "atr_pct": 4.21}
        self.assertAlmostEqual(atr_pct_of(result), 4.21)


class TestStaleDataLabel(unittest.TestCase):
    """「数据截至」必须是数据自己的最后一天，不能是生成时刻。

    2026-10-12 实测：sh600093 的新浪日线停在 2022-06-16（落后 1579 天），
    而报告抬头写「数据截至：2026-10-12 19:10」，把 2022 年的价当今天的行情分析。
    """

    def test_as_of_is_not_used_as_data_date(self):
        from stock_analysis import analyze_history, format_report

        frames = _kline_frames(start="2022-01-03", n=80)
        result = analyze_history(frames, "sh600093", "东风科技")
        # data_as_of 是序列的**最后**一天（start + n-1 天）
        self.assertEqual(result["data_as_of"], "2022-03-23")
        self.assertGreater(result["data_age_days"], 1000)
        header = format_report(result).splitlines()[1]
        self.assertIn("2022-03-23", header)
        self.assertNotIn(result["as_of"][:10], header)   # 不许拿今天顶替

    def test_stale_data_is_flagged(self):
        from stock_analysis import analyze_history, format_report

        result = analyze_history(_kline_frames(start="2022-01-03", n=80), "sh600093", "东风科技")
        header = format_report(result).splitlines()[1]
        self.assertIn("数据已落后", header)
        self.assertIn("不是今天的", header)

    def test_fresh_data_is_not_flagged(self):
        from stock_analysis import analyze_history, format_report

        result = analyze_history(_kline_frames(start=None, n=80), "sh600118", "中国卫星")
        self.assertEqual(result["data_age_days"], 0)
        header = format_report(result).splitlines()[1]
        self.assertNotIn("数据已落后", header)


class TestGeneratedContinuationHold(unittest.TestCase):
    def test_analyze_history_publishes_unified_hold_state(self):
        from stock_analysis import analyze_history, format_report

        result = analyze_history(_kline_frames(start=None, n=80), "sh600118", "中国卫星")
        self.assertIn("continuation_hold", result["entry_structure"])
        self.assertEqual(
            result["continuation_hold"]["state"],
            result["entry_structure"]["continuation_hold"]["state"],
        )
        self.assertIn(result["continuation_hold"]["state"], {
            "HOLD_CONTINUATION",
            "PROFIT_PROTECTION",
            "SECOND_WAVE_WATCH",
            "SECOND_WAVE_CONFIRMED",
            "BREAK_DEFENSE",
            "DISTRIBUTION_RISK",
            "MISSING",
        })
        self.assertIn("持有策略", format_report(result))


class TestSituationNotes(unittest.TestCase):
    """情境分句：让同一批持仓的应对话术分开。

    2026-10-12 之前 12 只里 11 只挤进同一句模板、只换止损位数字，
    可底层数据（相对强弱跨 17pp、波动率差 3.5 倍、浮亏跨 49pp）区分度很高。
    """

    def test_each_stock_gets_a_distinct_note_set(self):
        """同一批持仓不该产出同一句话术。"""
        from situation_notes import situation_notes
        from volume_price import VolumePriceSignal

        samples = [
            situation_notes(vp_signal=VolumePriceSignal.EXPAND_UP),
            situation_notes(vp_signal=VolumePriceSignal.SHRINK_CRASH,
                            relative_strength=-11.2),
            situation_notes(vp_signal=VolumePriceSignal.SHRINK_DOWN,
                            relative_strength=-3.8),
            situation_notes(vp_signal=VolumePriceSignal.SHRINK_CRASH, relative_strength=1.1),
        ]
        self.assertEqual(len({tuple(s) for s in samples}), 4)
        self.assertTrue(all(s for s in samples))

    def test_volume_price_is_the_primary_differentiator(self):
        from situation_notes import situation_notes
        from volume_price import VolumePriceSignal

        up = situation_notes(vp_signal=VolumePriceSignal.EXPAND_UP)
        down = situation_notes(vp_signal=VolumePriceSignal.EXPAND_DOWN)
        self.assertIn("承接转强", up[0])
        self.assertIn("不接刀", down[0])

    def test_relative_strength_shares_use_the_same_band(self):
        """相对强弱用 ±2pp 分档（与 stock_analysis.signals 同口径）。"""
        from situation_notes import situation_notes

        self.assertEqual(situation_notes(relative_strength=0.5), [])
        self.assertIn("强于大盘2.5", situation_notes(relative_strength=2.5)[0])
        self.assertIn("跑输大盘2.5", situation_notes(relative_strength=-2.5)[0])
        self.assertIn("明显偏弱", situation_notes(relative_strength=-10.0)[0])

    def test_near_deep_loss_is_flagged_but_real_deep_is_not_repeated(self):
        """真深套另有两套剧本，这里只提示「逼近」，避免话术重复。"""
        from situation_notes import situation_notes

        near = situation_notes(floating_pct=-19.0, deep_threshold=-20.0)
        self.assertTrue(any("逼近深套" in n for n in near))
        deep = situation_notes(floating_pct=-40.0, deep_threshold=-20.0)
        self.assertFalse(any("逼近深套" in n for n in deep))

    def test_low_location_is_not_reported(self):
        """低位是常态（12 只里 9 只），写了不区分；高位才写。"""
        from situation_notes import situation_notes

        self.assertFalse(any("低位" in n for n in situation_notes(location="LOW")))
        self.assertTrue(any("高位" in n for n in situation_notes(location="HIGH")))

    def test_notes_are_capped_at_three(self):
        from situation_notes import situation_notes
        from volume_price import VolumePriceSignal

        notes = situation_notes(
            vp_signal=VolumePriceSignal.SHRINK_CRASH,
            relative_strength=-11.0, atr_pct=7.4,
            floating_pct=-19.0, deep_threshold=-20.0, location="HIGH",
        )
        self.assertLessEqual(len(notes), 3)

    def test_empty_context_yields_no_notes(self):
        from situation_notes import situation_notes

        self.assertEqual(situation_notes(), [])

    def test_action_composes_core_plus_notes(self):
        """action = 决策框架 + 情境分句；action_core 保留框架本身供摘要用。"""
        from volume_price import VolumePriceSignal

        technical = _technical()
        technical["relative_strength"] = -11.5
        technical["price_change"] = -4.0
        technical["volume_ratio"] = 0.5
        result = evaluate_position(
            position={"code": "sz002703", "name": "浙江世宝", "quantity": 800, "cost": 18.69},
            technical=technical,
            live_data={},
            portfolio={"total_capital": 400_000, "available_capital": 0,
                       "initial_entry_pct": 5, "max_position_pct": 20},
        )
        self.assertTrue(result["situation_notes"])
        self.assertIn("今日情境：", result["action"])
        self.assertNotIn("今日情境：", result["action_core"])
        self.assertTrue(result["action"].startswith(result["action_core"]))


if __name__ == "__main__":
    unittest.main()
