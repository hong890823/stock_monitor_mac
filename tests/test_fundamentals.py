#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基本面分析的接口契约与价格兜底测试。

背景（2026-09-28）：
1. `fetch_and_analyze` 返回 (result, report_text) 元组，调用方按字符串直接
   write() 报 "write() argument must be str, not tuple"；
2. 东财 push2 快照偶发不可达时整个分析直接失败，缺新浪行情兜底。
"""

import unittest
from unittest import mock

import fundamentals
from fundamentals import (
    Announcement,
    AnnouncementSummary,
    FinancialPeriod,
    FundamentalPayload,
    Fundamentals,
    FundamentalsReport,
    InstitutionalPosition,
    ReportMarkup,
    RiskLevel,
    ShareholderAnalysis,
    ShareholderDisclosure,
    _fetch_sina_price,
    fetch_and_analyze,
    parse_shareholder_disclosures,
    parse_shareholder_page,
    scan_announcements,
)


class FetchAndAnalyzeContractTests(unittest.TestCase):
    """fetch_and_analyze 的返回契约：(FundamentalResult, str)，报告文本可直接落盘。"""

    def test_returns_result_and_report_text(self):
        fake_payload = mock.MagicMock()
        fake_payload.price = 12.34
        fake_result = mock.MagicMock()
        fake_report = f"报告{ReportMarkup.red('68/100')}正文"

        with mock.patch.object(fundamentals, "FundamentalsClient") as client_cls, \
                mock.patch.object(fundamentals.Fundamentals, "analyze", return_value=fake_result), \
                mock.patch.object(fundamentals.FundamentalsReport, "format", return_value=fake_report):
            client_cls.return_value.fetch.return_value = fake_payload
            result, report = fetch_and_analyze("sh600487", "亨通光电", today="2026-09-28")

        self.assertIs(result, fake_result)
        self.assertIsInstance(report, str)
        # 报告文本经 plain() 去掉 UI 高亮标记后可直接写文件
        plain = ReportMarkup.plain(report)
        self.assertIsInstance(plain, str)
        self.assertNotIn(ReportMarkup.OPEN, plain)
        self.assertIn("68/100", plain)


class SinaPriceFallbackTests(unittest.TestCase):
    """东财快照失败时的新浪现价兜底。"""

    def _response(self, text):
        resp = mock.MagicMock()
        resp.text = text
        resp.raise_for_status.return_value = None
        return resp

    def test_parses_current_price(self):
        body = 'var hq_str_sh600487="亨通光电,60.10,60.64,60.64,60.80,59.90,60.64,60.65,12345678,3488720613,..."'
        with mock.patch.object(fundamentals.requests, "get", return_value=self._response(body)):
            self.assertAlmostEqual(60.64, _fetch_sina_price("sh600487"))

    def test_zero_price_falls_back_to_prev_close(self):
        # 集合竞价前新浪返回 0 价，用昨收兜底
        body = 'var hq_str_sh600487="亨通光电,0.00,60.64,0.00,0.00,0.00,0.00,0.00,0,0.00,..."'
        with mock.patch.object(fundamentals.requests, "get", return_value=self._response(body)):
            self.assertAlmostEqual(60.64, _fetch_sina_price("sh600487"))

    def test_returns_zero_on_error(self):
        with mock.patch.object(fundamentals.requests, "get", side_effect=RuntimeError("boom")):
            self.assertEqual(0.0, _fetch_sina_price("sh600487"))

    def test_returns_zero_on_empty_payload(self):
        with mock.patch.object(fundamentals.requests, "get", return_value=self._response("")):
            self.assertEqual(0.0, _fetch_sina_price("sh600487"))


class SnapshotFallbackIntegrationTests(unittest.TestCase):
    """东财快照取不到价时，fetch() 应退回新浪兜底而不是抛「没有取到价格」。"""

    def test_price_uses_sina_fallback_when_snapshot_missing(self):
        client = fundamentals.FundamentalsClient()
        fake_period = mock.MagicMock()
        fake_period.date = "2026-06-30"

        with mock.patch.object(client, "_fetch_f10", return_value={"result": {"data": [{"REPORT_DATE": "2026-06-30"}]}}), \
                mock.patch.object(fundamentals, "_parse_periods", return_value=[fake_period]), \
                mock.patch.object(fundamentals, "_parse_balance", return_value=None), \
                mock.patch.object(fundamentals, "_parse_main_business", return_value=[]), \
                mock.patch.object(client, "_fetch_valuations", return_value=([], None)), \
                mock.patch.object(client, "_fetch_announcements", return_value=[]), \
                mock.patch.object(fundamentals, "_fetch_sina_price", return_value=60.64) as sina_price, \
                mock.patch.object(client, "_fetch_snapshot", return_value=None):
            payload = client.fetch("sh600487", "亨通光电")

        self.assertEqual(60.64, payload.price)
        sina_price.assert_called_once_with("sh600487")

    def test_raises_only_when_both_sources_fail(self):
        client = fundamentals.FundamentalsClient()
        fake_period = mock.MagicMock()

        with mock.patch.object(client, "_fetch_f10", return_value={"result": {"data": [{"REPORT_DATE": "2026-06-30"}]}}), \
                mock.patch.object(fundamentals, "_parse_periods", return_value=[fake_period]), \
                mock.patch.object(fundamentals, "_parse_balance", return_value=None), \
                mock.patch.object(fundamentals, "_parse_main_business", return_value=[]), \
                mock.patch.object(client, "_fetch_valuations", return_value=([], None)), \
                mock.patch.object(client, "_fetch_announcements", return_value=[]), \
                mock.patch.object(fundamentals, "_fetch_sina_price", return_value=0.0), \
                mock.patch.object(client, "_fetch_snapshot", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                client.fetch("sh600487", "亨通光电")

        self.assertIn("亨通光电", str(ctx.exception))


class AnnouncementSummaryTests(unittest.TestCase):
    """公告正文 → 股东影响判断（对齐 Android AnnouncementSummary.assess）。"""

    def test_private_placement_reads_stage_use_and_risk(self):
        text = AnnouncementSummary.assess(
            "关于向特定对象发行股票申请获得上海证券交易所受理的公告",
            "定增/再融资事项",
            "<p>本次发行尚需上海证券交易所审核通过及中国证监会注册，募集资金用于项目建设。</p>",
        )
        self.assertIn("中性偏积极", text)
        self.assertIn("审核/注册", text)
        self.assertIn("募集资金用于项目建设等经营安排", text)

    def test_private_placement_termination_is_negative(self):
        text = AnnouncementSummary.assess(
            "关于终止向特定对象发行股票事项的公告", "定增/再融资事项", "公司决定终止本次发行。"
        )
        self.assertTrue(text.startswith("偏利空"))
        self.assertIn("方案已终止", text)

    def test_no_content_yields_no_judgement(self):
        # 正文没取到时不编造结论，宁可留空让报告提示去核对原文
        self.assertIsNone(AnnouncementSummary.assess("关于股东减持股份的公告", "股东减持", None))
        self.assertIsNone(AnnouncementSummary.assess("关于股东减持股份的公告", "股东减持", "   "))

    def test_html_is_stripped_to_plain_text(self):
        self.assertEqual(
            "第一段 第二段",
            AnnouncementSummary.to_plain_text("<p>第一段</p><p>第二段</p>").replace("\n", " "),
        )

    def test_reduction_finished_reads_as_landed(self):
        text = AnnouncementSummary.assess(
            "关于股东减持计划实施完毕的公告", "股东减持", "本次减持计划已减持完毕。"
        )
        self.assertIn("阶段性落地", text)

    def test_pledge_release_is_positive(self):
        text = AnnouncementSummary.assess("关于解除质押的公告", "股东质押", "相关股份已办理解除质押登记。")
        self.assertTrue(text.startswith("偏利好"))

    def test_expansion_project_flags_capacity_buildout_risk(self):
        text = AnnouncementSummary.assess(
            "关于向特定对象发行A股股票募集资金投资项目的公告",
            "定增/再融资事项",
            "<p>项目拟新增光通信产品产能，建设期24个月。</p>"
            "<p>公司将根据市场需求和客户订单推进产能建设。</p>"
            "<p>本次发行尚需交易所审核及中国证监会注册。</p>",
        )
        self.assertIn("扩产", text)
        self.assertIn("产能利用率", text)
        self.assertIn("订单", text)

    def test_non_expansion_project_has_no_capacity_risk(self):
        text = AnnouncementSummary.assess(
            "关于向特定对象发行股票的公告",
            "定增/再融资事项",
            "<p>募集资金用于偿还银行借款，本次发行尚需审核及注册。</p>",
        )
        self.assertNotIn("扩产", text)


class GovernanceScanTests(unittest.TestCase):
    """公告扫描：标题分类、窗口过滤、同类合并（对齐 Android Governance.scan）。"""

    def test_scan_classifies_by_title(self):
        items = [
            Announcement("2026-09-01", "华天科技:关于股东股份质押的公告"),
            Announcement("2026-08-20", "关于收到中国证监会立案告知书的公告"),
            Announcement("2026-07-10", "关于股东部分股份解除质押的公告"),
            Announcement("2026-06-30", "2026年半年度报告"),
            Announcement("2026-06-20", "第八届董事会第十八次会议决议公告"),
            Announcement("2026-06-10", "非经营性资金占用及其他关联资金往来情况汇总表"),
        ]
        scanned = scan_announcements(items, "2026-09-24")
        self.assertEqual(3, len(scanned))
        self.assertEqual(RiskLevel.ALERT, scanned[0].level)
        self.assertIn("立案", scanned[0].title)
        self.assertTrue(any(g.level == RiskLevel.GOOD and g.reason == "质押解除" for g in scanned))
        self.assertTrue(any(g.level == RiskLevel.WATCH and g.reason == "股东质押" for g in scanned))

    def test_scan_skips_items_older_than_window(self):
        items = [
            Announcement("2024-01-05", "关于收到行政处罚决定书的公告"),
            Announcement("2026-09-01", "关于股东减持股份的公告"),
        ]
        scanned = scan_announcements(items, "2026-09-24")
        self.assertEqual(1, len(scanned))
        self.assertEqual(RiskLevel.WATCH, scanned[0].level)

    def test_repeated_reasons_collapse_into_one_entry(self):
        items = [
            Announcement("2026-09-01", "关于重大资产重组的进展公告"),
            Announcement("2026-08-01", "关于重大资产重组的公告"),
            Announcement("2026-07-01", "关于重大资产重组事项的公告"),
        ]
        hits = scan_announcements(items, "2026-09-24")
        self.assertEqual(1, len(hits))
        self.assertEqual(3, hits[0].count)
        self.assertEqual("2026-09-01", hits[0].date)

    def test_private_placement_keeps_assessment(self):
        items = [
            Announcement(
                "2026-09-20",
                "关于向特定对象发行股票申请获得上海证券交易所受理的公告",
                content="<p>本次发行尚需上海证券交易所审核通过及中国证监会注册，募集资金用于项目建设。</p>",
            ),
            Announcement("2026-09-18", "关于2026年度定增预案的公告"),
        ]
        scanned = scan_announcements(items, "2026-09-24")
        self.assertEqual(1, len(scanned))
        self.assertEqual("定增/再融资事项", scanned[0].reason)
        self.assertIn("审核/注册", scanned[0].assessment)


class GovernanceReportTests(unittest.TestCase):
    """第六节渲染：展示判断结论而不是公告正文（对齐 Android appendGovernance）。"""

    @staticmethod
    def _period(date, revenue=100.0, net=10.0, deduct=9.0):
        return FinancialPeriod(
            date=date, revenue=revenue, operate_cost=revenue * 0.8,
            net_profit=net, deduct_net_profit=deduct,
            operating_cash_flow=net, sales_cash=revenue, capex=1.0,
        )

    @staticmethod
    def _payload(announcements):
        return FundamentalPayload(
            code="sh600000", name="测试股", price=20.0, total_shares=100.0,
            periods=[GovernanceReportTests._period("2026-06-30")],
            balance=None, valuations=[], peers=None,
            main_business=[], announcements=announcements,
        )

    @staticmethod
    def _section_six(result):
        report = FundamentalsReport.format("测试股", "sh600000", 20.0, "2026-09-24", result)
        return report.split("六、公告与治理提示", 1)[1].split("七、", 1)[0]

    def test_section_shows_judgement_not_announcement_body(self):
        result = Fundamentals.analyze(self._payload([
            Announcement(
                "2026-09-20",
                "关于向特定对象发行股票申请获得上海证券交易所受理的公告",
                content="<p>本次发行尚需上海证券交易所审核通过及中国证监会注册，募集资金用于项目建设。</p>",
            ),
        ]))
        section = self._section_six(result)
        self.assertNotIn("内容摘要", section)
        self.assertNotIn("本次发行尚需上海证券交易所审核通过及中国证监会注册", section)
        self.assertIn("中性偏积极", section)
        self.assertIn("审核/注册", section)

    def test_missing_announcements_is_not_same_as_no_risk(self):
        result = Fundamentals.analyze(self._payload([]))
        section = self._section_six(result)
        self.assertIn("暂缺（不是因为没问题）", section)

    def test_unmatched_announcements_cannot_prove_no_risk(self):
        result = Fundamentals.analyze(self._payload([
            Announcement("2026-06-30", "2026年半年度报告"),
        ]))
        section = self._section_six(result)
        self.assertIn("不能把这段内容当成「没有风险」的证明", section)

    def test_alert_item_is_promoted_into_risk_list(self):
        result = Fundamentals.analyze(self._payload([
            Announcement("2026-08-20", "关于收到中国证监会立案告知书的公告"),
        ]))
        self.assertTrue(any(
            r.level == RiskLevel.ALERT and "公告提示" in r.text for r in result.risks
        ))
        self.assertEqual(1, result.announcements_scanned)

    def test_conclusion_carries_announcement_impact_and_valuation_judgement(self):
        result = Fundamentals.analyze(self._payload([
            Announcement(
                "2026-09-20",
                "关于向特定对象发行股票申请获得上海证券交易所受理的公告",
                content="<p>本次发行尚需上海证券交易所审核通过及中国证监会注册，募集资金用于项目建设。</p>",
            ),
        ]))
        report = FundamentalsReport.format("测试股", "sh600000", 20.0, "2026-09-24", result)
        conclusion = report.split("十三、综合结论", 1)[1]
        self.assertIn("公告影响：", conclusion)
        self.assertIn("中性偏积极", conclusion)
        self.assertIn("估值判断：", conclusion)


class ValuationComparisonTests(unittest.TestCase):
    """估值对比：自身历史分位 + 行业可比折价/溢价（对齐 Android valuationComparisonShort）。"""

    @staticmethod
    def _result(**kwargs):
        result = mock.MagicMock()
        result.pe_percentile = kwargs.get("pe_percentile")
        result.pb_percentile = kwargs.get("pb_percentile")
        result.current_pe = kwargs.get("current_pe")
        result.current_pb = kwargs.get("current_pb")
        peers = kwargs.get("peers", "auto")
        if peers == "auto":
            result.peers = mock.MagicMock(
                pe_median=kwargs.get("pe_median"),
                pb_median=kwargs.get("pb_median"),
            )
        else:
            result.peers = peers
        return result

    def test_expensive_versus_peers_reads_as_premium(self):
        result = self._result(pe_percentile=80.0, current_pe=40.0, pe_median=20.0)
        text = fundamentals._valuation_comparison_short(result)
        self.assertIn("相对自身历史", text)
        self.assertIn("偏贵", text)
        self.assertIn("相对行业可比", text)
        self.assertIn("高于行业中位", text)
        self.assertIn("溢价", text)

    def test_cheap_versus_peers_reads_as_discount(self):
        result = self._result(
            pe_percentile=10.0, pb_percentile=5.0,
            current_pe=12.0, current_pb=1.0,
            pe_median=20.0, pb_median=2.0,
        )
        text = fundamentals._valuation_comparison_short(result)
        self.assertIn("偏低", text)
        self.assertIn("低于行业中位", text)
        self.assertIn("折价", text)

    def test_close_to_peer_median_reads_as_near(self):
        result = self._result(pb_percentile=50.0, current_pe=20.0, pe_median=20.0)
        text = fundamentals._valuation_comparison_short(result)
        self.assertIn("居中", text)
        self.assertIn("接近行业中位", text)

    def test_missing_peers_falls_back_to_own_history(self):
        result = self._result(pe_percentile=50.0, current_pe=20.0, peers=None)
        text = fundamentals._valuation_comparison_short(result)
        self.assertIn("居中", text)
        self.assertIn("相对行业可比：暂无数据", text)
        self.assertIn("综合以自身历史估值为主", text)

    def test_loss_making_company_skips_pe_comparison(self):
        result = self._result(current_pe=-3.0, current_pb=1.5, pe_median=20.0, pb_median=2.0)
        text = fundamentals._valuation_comparison_short(result)
        self.assertNotIn("PE ", text)
        self.assertIn("PB ", text)

    def test_headline_summarises_own_and_peer_stance(self):
        result = self._result(pe_percentile=80.0, current_pe=40.0, pe_median=20.0)
        self.assertEqual("相对自身历史偏贵", fundamentals._self_valuation_headline(result))
        self.assertEqual("相对行业可比有溢价", fundamentals._peer_valuation_headline(result))
        result = self._result(current_pe=-3.0, peers=None)
        self.assertEqual("行业可比数据不足", fundamentals._peer_valuation_headline(result))


class ForwardValuationTests(unittest.TestCase):
    """前瞻估值：中性 ±15% 情景区间 + 修正系数说明（对齐 Android forwardValuation）。"""

    @staticmethod
    def _result(**kwargs):
        result = mock.MagicMock()
        result.fair_low = kwargs.get("fair_low")
        result.fair_high = kwargs.get("fair_high")
        result.quarters = kwargs.get("quarters", [])
        result.ocf_to_profit = kwargs.get("ocf_to_profit")
        result.deduct_ratio = kwargs.get("deduct_ratio")
        result.net_cash_ratio = kwargs.get("net_cash_ratio")
        return result

    @staticmethod
    def _quarter(yoy):
        q = mock.MagicMock()
        q.profit_yoy = yoy
        return q

    def test_scenarios_are_symmetric_around_base(self):
        result = self._result(fair_low=10.0, fair_high=20.0, quarters=[self._quarter(5.0)] * 4)
        forward = fundamentals._forward_valuation(result)
        self.assertAlmostEqual(forward.base_low * 0.85, forward.bear_low)
        self.assertAlmostEqual(forward.base_high * 1.15, forward.bull_high)

    def test_bad_cash_and_debt_discount_the_range(self):
        weak = fundamentals._forward_valuation(self._result(
            fair_low=10.0, fair_high=20.0,
            quarters=[self._quarter(-10.0)] * 4,
            ocf_to_profit=0.4, deduct_ratio=0.5, net_cash_ratio=-0.2,
        ))
        neutral = fundamentals._forward_valuation(self._result(
            fair_low=10.0, fair_high=20.0,
            quarters=[self._quarter(0.0)] * 4,
            ocf_to_profit=1.0, deduct_ratio=0.8, net_cash_ratio=0.0,
        ))
        self.assertLess(weak.base_low, neutral.base_low)
        self.assertIn("利润趋势修正", weak.basis)
        self.assertIn("净负债状态下调", weak.basis)

    def test_growth_is_damped_and_capped(self):
        hot = fundamentals._forward_valuation(self._result(
            fair_low=10.0, fair_high=20.0, quarters=[self._quarter(200.0)] * 4,
        ))
        self.assertLessEqual(hot.base_high, 20.0 * 1.20 * 1.05)

    def test_no_fair_range_means_no_projection(self):
        self.assertIsNone(fundamentals._forward_valuation(self._result(fair_low=None, fair_high=None)))
        text = fundamentals._future_valuation_summary(self._result(fair_low=None, fair_high=None))
        self.assertIn("暂不外推", text)

    def test_summary_spells_out_all_three_scenarios(self):
        result = self._result(fair_low=10.0, fair_high=20.0, quarters=[self._quarter(5.0)] * 4)
        text = fundamentals._future_valuation_summary(result)
        self.assertIn("未来 6~9 个月", text)
        self.assertIn("中性", text)
        self.assertIn("悲观", text)
        self.assertIn("乐观", text)


class NextQuarterOutlookTests(unittest.TestCase):
    """下季展望：近4季净利同比方向 + 现金/扣非质量（对齐 Android nextQuarterOutlook）。"""

    @staticmethod
    def _result(quarters, ocf_to_profit=None, deduct_ratio=None):
        result = mock.MagicMock()
        result.quarters = [
            mock.MagicMock(profit_yoy=y) for y in quarters
        ]
        result.ocf_to_profit = ocf_to_profit
        result.deduct_ratio = deduct_ratio
        return result

    def test_consistent_growth_reads_as_supportive(self):
        text = fundamentals._next_quarter_outlook(
            self._result([10.0, 12.0, 15.0, 20.0], ocf_to_profit=1.2, deduct_ratio=0.95)
        )
        self.assertIn("利润趋势偏支持", text)
        self.assertIn("经营现金流/归母净利", text)
        self.assertIn("扣非占归母", text)

    def test_consistent_decline_reads_as_pressured(self):
        text = fundamentals._next_quarter_outlook(self._result([-10.0, -12.0, -15.0, -20.0]))
        self.assertIn("利润趋势偏承压", text)

    def test_mixed_signals_stay_stable(self):
        text = fundamentals._next_quarter_outlook(self._result([20.0, -5.0, 3.0, -1.0]))
        self.assertIn("利润趋势基本稳定", text)

    def test_no_yoy_data_declines_to_project(self):
        text = fundamentals._next_quarter_outlook(self._result([]))
        self.assertIn("暂不外推", text)
        self.assertEqual(
            "下一财报期趋势数据不足",
            fundamentals._next_quarter_direction(self._result([])),
        )


class ShareholderAnalysisTests(unittest.TestCase):
    """最新股东数 + 机构持仓变动（对齐 Android ShareholderAnalysis）。"""

    @staticmethod
    def _disclosure(date, holder_count, institutions):
        return ShareholderDisclosure(
            report_date=date,
            holder_count=holder_count,
            institutions=[InstitutionalPosition(n, s) for n, s in institutions],
        )

    def test_holder_change_and_institution_flow_are_compared_across_periods(self):
        latest = self._disclosure(
            "2026-06-30", 80_000,
            [("甲基金", 3_000.0), ("乙基金", 2_000.0), ("丙保险", 4_000.0)],
        )
        previous = self._disclosure(
            "2026-03-31", 100_000,
            [("甲基金", 5_000.0), ("乙基金", 2_000.0), ("丁信托", 1_000.0)],
        )
        analysis = ShareholderAnalysis.from_disclosures(latest, previous, current_price=10.0)

        self.assertEqual(80_000, analysis.retail_holder_count)
        self.assertEqual(-20_000, analysis.retail_holder_change)
        self.assertEqual(-20.0, analysis.retail_holder_change_pct)
        # 甲 -2000、丙 +4000、丁 -1000 → 净 +1000 股；乙持平不计
        self.assertEqual(1_000.0, analysis.institutional_net_shares)
        self.assertEqual(10_000.0, analysis.institutional_net_funds)
        self.assertEqual(["丙保险"], [c.name for c in analysis.inflows])
        self.assertEqual(["甲基金", "丁信托"], [c.name for c in analysis.outflows])

    def test_retail_down_with_institution_inflow_reads_positive(self):
        latest = self._disclosure("2026-06-30", 80_000, [("甲基金", 5_000.0)])
        previous = self._disclosure("2026-03-31", 100_000, [("甲基金", 3_000.0)])
        analysis = ShareholderAnalysis.from_disclosures(latest, previous, current_price=10.0)

        self.assertEqual(
            "散户/中小股东户数下降，披露机构持仓净流入，筹码结构改善，偏利好。",
            analysis.conclusion,
        )

    def test_retail_up_with_institution_outflow_reads_risky(self):
        latest = self._disclosure("2026-06-30", 120_000, [("甲基金", 3_000.0)])
        previous = self._disclosure("2026-03-31", 100_000, [("甲基金", 5_000.0)])
        analysis = ShareholderAnalysis.from_disclosures(latest, previous, current_price=10.0)

        self.assertEqual(
            "散户/中小股东户数增加，披露机构持仓净流出，筹码趋于分散，偏风险。",
            analysis.conclusion,
        )

    def test_only_latest_period_has_no_net_flow(self):
        latest = self._disclosure("2026-06-30", 80_000, [("甲基金", 5_000.0)])
        analysis = ShareholderAnalysis.from_disclosures(latest, None, current_price=10.0)

        self.assertIsNone(analysis.retail_holder_change)
        self.assertIsNone(analysis.retail_holder_change_pct)
        self.assertIsNone(analysis.institutional_net_funds)
        self.assertEqual("股东结构暂无明确改善或恶化信号。", analysis.conclusion)

    def test_none_without_any_disclosure(self):
        self.assertIsNone(ShareholderAnalysis.from_disclosures(None, None, current_price=10.0))

    def test_flat_holder_count_with_institution_inflow_reads_by_flow(self):
        latest = self._disclosure("2026-06-30", 100_000, [("甲基金", 5_000.0)])
        previous = self._disclosure("2026-03-31", 100_000, [("甲基金", 3_000.0)])
        analysis = ShareholderAnalysis.from_disclosures(latest, previous, current_price=10.0)

        self.assertEqual(0.0, analysis.retail_holder_change_pct)
        self.assertEqual("披露机构持仓净流入，机构关注度边际增强，偏利好。", analysis.conclusion)


class ParseShareholderDisclosuresTests(unittest.TestCase):
    """JSON 口径解析，用例与 Android FundamentalsClientTest 一致。"""

    def test_shareholder_rows_are_grouped_by_date_and_keep_institutions_only(self):
        counts = """{"result":{"data":[
            {"END_DATE":"2026-06-30 00:00:00","HOLDER_TOTAL_NUM":80000},
            {"END_DATE":"2026-03-31 00:00:00","HOLDER_TOTAL_NUM":100000}
        ]}}"""
        positions = """{"result":{"data":[
            {"END_DATE":"2026-06-30 00:00:00","HOLDER_NAME":"甲基金","HOLDER_TYPE":"基金","HOLD_NUM":1200000},
            {"END_DATE":"2026-06-30 00:00:00","HOLDER_NAME":"张三","HOLDER_TYPE":"个人","HOLD_NUM":900000},
            {"END_DATE":"2026-03-31 00:00:00","HOLDER_NAME":"甲基金","HOLDER_TYPE":"基金","HOLD_NUM":1000000},
            {"END_DATE":"2026-03-31 00:00:00","HOLDER_NAME":"丙保险","HOLDER_TYPE":"保险","HOLD_NUM":500000}
        ]}}"""

        disclosures = parse_shareholder_disclosures(counts, positions)

        self.assertEqual(["2026-06-30", "2026-03-31"], [d.report_date for d in disclosures])
        self.assertEqual(80000, disclosures[0].holder_count)
        self.assertEqual(["甲基金"], [i.name for i in disclosures[0].institutions])
        self.assertEqual(["甲基金", "丙保险"], [i.name for i in disclosures[1].institutions])


class ParseShareholderPageTests(unittest.TestCase):
    """新浪股东页解析，用例与 Android FundamentalsClientTest 一致。"""

    def test_sina_page_parser_reads_periods_counts_and_institutions(self):
        html = """
            <a name="2026-06-30"></a>
            <td><strong>股东总数</strong></td><td colspan="4">833474</td>
            <table><tr><td>1</td><td><a>甲基金</a></td><td>1200000</td><td>1.2</td></tr>
            <tr><td>2</td><td>张三</td><td>900000</td><td>0.9</td></tr></table>
            <a name="2026-03-31"></a>
            <td><strong>股东总数</strong></td><td colspan="4">853087</td>
            <table><tr><td>1</td><td><a>甲基金</a></td><td>1000000</td><td>1.0</td></tr></table>
        """

        disclosures = parse_shareholder_page(html)

        self.assertEqual(["2026-06-30", "2026-03-31"], [d.report_date for d in disclosures])
        self.assertEqual(833474, disclosures[0].holder_count)
        self.assertEqual(["甲基金"], [i.name for i in disclosures[0].institutions])

    def test_empty_page_returns_no_disclosures(self):
        self.assertEqual([], parse_shareholder_page("<html>登录</html>"))


class ShareholderReportTests(unittest.TestCase):
    """股东结构在快速概览 / 第六节 / 综合结论中的呈现。"""

    @staticmethod
    def _analysis():
        latest = ShareholderDisclosure(
            "2026-06-30", 80_000, [InstitutionalPosition("丙保险", 4_000.0)]
        )
        previous = ShareholderDisclosure(
            "2026-03-31", 100_000, [InstitutionalPosition("丙保险", 1_000.0)]
        )
        return ShareholderAnalysis.from_disclosures(latest, previous, current_price=10.0)

    def _report(self, shareholder):
        payload = FundamentalPayload(
            code="sh600000", name="测试股", price=20.0, total_shares=100.0,
            periods=[FinancialPeriod(
                date="2026-06-30", revenue=100.0, operate_cost=80.0,
                net_profit=10.0, deduct_net_profit=9.0,
                operating_cash_flow=10.0, sales_cash=100.0, capex=1.0,
            )],
            balance=None, valuations=[], peers=None,
            main_business=[], announcements=[],
            shareholder=shareholder,
        )
        result = Fundamentals.analyze(payload)
        return result, FundamentalsReport.format("测试股", "sh600000", 20.0, "2026-09-29", result)

    def test_overview_shows_latest_holder_count_change_and_flow(self):
        _, report = self._report(self._analysis())
        overview = report.split("一、快速概览", 1)[1].split("二、", 1)[0]

        self.assertIn("股东结构：", overview)
        self.assertIn("8.00万户", overview)
        self.assertIn("（-20.0%）", overview)
        self.assertIn("机构资金净流入", overview)

    def test_section_six_shows_shareholder_block(self):
        _, report = self._report(self._analysis())
        section = report.split("六、公告与治理提示", 1)[1].split("七、", 1)[0]

        self.assertIn("股东结构（2026-06-30）", section)
        self.assertIn("较上期 -20.0%", section)
        self.assertIn("机构流入：丙保险", section)
        # 口径科普已并到页脚「口径说明」一次说完，不再逐节重复
        # （2026-10-12：三只股报告里这四行一字不差）
        self.assertNotIn("说明：", section)
        self.assertIn("机构进出不等同于全部机构的真实交易", report)

    def test_conclusion_carries_shareholder_bullet(self):
        _, report = self._report(self._analysis())
        conclusion = report.split("十三、综合结论", 1)[1]

        self.assertIn("· 股东结构：", conclusion)

    def test_missing_shareholder_is_explicit(self):
        _, report = self._report(None)
        overview = report.split("一、快速概览", 1)[1].split("二、", 1)[0]
        section = report.split("六、公告与治理提示", 1)[1].split("七、", 1)[0]
        conclusion = report.split("十三、综合结论", 1)[1]

        self.assertIn("暂无可比股东数据。", overview)
        self.assertIn("暂未取到可比披露数据。", section)
        self.assertNotIn("· 股东结构：", conclusion)

    def test_holder_dispersion_promotes_watch_risk(self):
        latest = ShareholderDisclosure(
            "2026-06-30", 120_000, [InstitutionalPosition("丙保险", 4_000.0)]
        )
        previous = ShareholderDisclosure(
            "2026-03-31", 100_000, [InstitutionalPosition("丙保险", 4_000.0)]
        )
        analysis = ShareholderAnalysis.from_disclosures(latest, previous, current_price=10.0)
        result, _ = self._report(analysis)

        self.assertTrue(any(
            r.level == RiskLevel.WATCH and "股东结构风险" in r.text for r in result.risks
        ))


if __name__ == "__main__":
    unittest.main()
