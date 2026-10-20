#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""港美股基本面分析测试，用例与 Android 版 GlobalFundamentalsClientTest.kt 一一对应。"""

import unittest

from global_fundamentals import (
    GlobalSecurity,
    SEC_SOURCE_NAME,
    em_indicator_fields,
    fallback_search_results,
    format_report,
    merge_fundamentals,
    parse_search_results,
    parse_sec_facts,
    parse_sec_search_cik,
    parse_sec_ticker_results,
    parse_nasdaq_fundamentals,
    parse_fundamentals,
    parse_sina_hk_quote,
    parse_sina_suggest,
    parse_sina_us_quote,
    money_unit,
    quality_conclusion,
    risk_items,
    valuation_conclusion,
    valuation_range_text,
    _plain_symbol,
    _empty_fundamentals,
)


def security(symbol="NVDA", name="NVIDIA Corporation", exchange="NASDAQ", currency="USD"):
    return GlobalSecurity(symbol, name, exchange, currency)


class SearchParserTests(unittest.TestCase):
    def test_search_parser_keeps_stocks_and_maps_exchange_currency(self):
        results = parse_search_results(
            """{"quotes":[
                {"symbol":"NVDA","shortname":"NVIDIA Corporation","quoteType":"EQUITY","exchange":"NMS","currency":"USD"},
                {"symbol":"NVDA.MX","shortname":"NVIDIA Corp","quoteType":"EQUITY","exchange":"MEX","currency":"MXN"},
                {"symbol":"NVDL","shortname":"GraniteShares 2x Long NVDA Daily ETF","quoteType":"ETF","exchange":"NMS","currency":"USD"}
            ]}"""
        )

        self.assertEqual(2, len(results))
        self.assertEqual("NVDA", results[0].symbol)
        self.assertEqual("NASDAQ", results[0].exchange)
        self.assertEqual("USD", results[0].currency)
        self.assertTrue(any(item.symbol == "NVDA.MX" for item in results))

    def test_fallback_search_resolves_nvidia_without_network(self):
        results = fallback_search_results("英伟达")

        self.assertEqual(1, len(results))
        self.assertEqual("NVDA", results[0].symbol)
        self.assertEqual("NASDAQ", results[0].exchange)

    def test_sina_suggest_extracts_hk_and_us_by_type(self):
        # 实测新浪 suggest 格式：名称,类型,代码,…（31=港股 41=美股/ADR 11=A股 87=期货）
        # 同公司 ADR 常排在港股正股前面（小米 xiacy 在 01810 之前），结果里港股正股优先
        text = (
            'var suggestvalue="小米集团-Ｗ,31,01810,01810,小米集团-Ｗ,,小米集团-Ｗ,99,1,ESG,,;'
            '小米集团,41,xiacy,xiacy,小米集团(ADR),,小米集团(ADR),99,1,ADR|ESG,,;'
            '玉米,87,c0,c0,玉米,,玉米,99,1,,,;高伟达,11,300465,sz300465,高伟达,,高伟达,99,1,,,;"'
        )
        results = parse_sina_suggest(text)

        self.assertEqual(["01810", "XIACY"], [r.symbol for r in results])
        self.assertEqual(["HKEX", "NASDAQ"], [r.exchange for r in results])
        self.assertEqual(["HKD", "USD"], [r.currency for r in results])

    def test_sina_suggest_keeps_api_relevance_across_companies(self):
        # 回归：「博通」的美股 AVGO 排在接口第一位，弱相关的港股「载通」不能被顶到前面
        text = (
            'var suggestvalue="博通,41,avgo,avgo,x;载通,31,00062,00062,y;"'
        )
        results = parse_sina_suggest(text)

        self.assertEqual(["AVGO", "00062"], [r.symbol for r in results])

    def test_sina_suggest_skips_a_share_and_futures(self):
        text = 'var suggestvalue="高伟达,11,300465,sz300465,x;玉米,87,c0,c0,y;"'
        self.assertEqual([], parse_sina_suggest(text))

    def test_fallback_resolves_xiaomi(self):
        results = fallback_search_results("小米集团")

        self.assertEqual("01810", results[0].symbol)
        self.assertEqual("HKEX", results[0].exchange)


class YahooNasdaqParserTests(unittest.TestCase):
    def test_fundamentals_parser_reads_financials_and_nulls_missing(self):
        payload = """
            {
              "quoteSummary":{"result":[{"price":{"regularMarketPrice":{"raw":225.07}},
              "summaryDetail":{"marketCap":{"raw":5420000000000},"trailingPE":{"raw":28.1},"priceToBook":{"raw":23.7}},
              "financialData":{"totalRevenue":{"raw":196000000000},"grossMargins":{"raw":0.75},"operatingMargins":{"raw":0.62},"profitMargins":{"raw":0.49},"returnOnEquity":{"raw":1.03},"operatingCashflow":{"raw":74421000000},"freeCashflow":{"raw":70000000000},"totalCash":{"raw":56586000000},"totalDebt":{"raw":33366000000}},
              "assetProfile":{"sector":"Technology","industry":"Semiconductors","longBusinessSummary":"AI computing platform."}
              }]}
            }
        """
        result = parse_fundamentals(payload, "NVDA", "NVIDIA Corporation")

        self.assertEqual(225.07, result.price)
        self.assertEqual(196_000_000_000.0, result.revenue)
        self.assertEqual(0.75, result.gross_margin)
        self.assertEqual(74_421_000_000.0, result.operating_cash_flow)
        self.assertEqual(28.1, result.pe)
        self.assertEqual("USD", result.currency)
        self.assertIn("AI computing platform", result.summary or "")

    def test_nasdaq_fallback_parser_reads_price_market_cap_and_industry(self):
        info = """{"data":{"symbol":"NVDA","companyName":"NVIDIA Corporation Common Stock","exchange":"NASDAQ-GS","primaryData":{"lastSalePrice":"$228.86"}}}"""
        summary = """{"data":{"summaryData":{"Sector":{"value":"Technology"},"Industry":{"value":"Semiconductors"},"MarketCap":{"value":"5,515,526,000,000"}}}}"""

        result = parse_nasdaq_fundamentals(info, summary, security())

        self.assertEqual(228.86, result.price)
        self.assertEqual(5_515_526_000_000.0, result.market_cap)
        self.assertEqual("Technology", result.sector)
        self.assertEqual("Semiconductors", result.industry)


class SecParserTests(unittest.TestCase):
    """SEC EDGAR 解析，用例与 Android 新增的 Sec 测试一致。"""

    def test_sec_ticker_parser_finds_company_cik(self):
        result = parse_sec_ticker_results(
            """{"0":{"cik_str":1045810,"ticker":"NVDA","title":"NVIDIA CORP"}}""",
            "NVDA",
        )

        self.assertEqual("0001045810", result.cik)
        self.assertEqual("NVIDIA CORP", result.name)

    def test_sec_facts_parser_builds_margins_cash_flow_and_valuation(self):
        payload = """
            {
              "entityName":"NVIDIA CORP",
              "facts":{"us-gaap":{
                "RevenueFromContractWithCustomerExcludingAssessedTax":{"units":{"USD":[{"filed":"2026-02-20","start":"2025-01-01","end":"2025-12-31","val":1000}]}},
                "CostOfRevenue":{"units":{"USD":[{"filed":"2026-02-20","start":"2025-01-01","end":"2025-12-31","val":400}]}},
                "OperatingIncomeLoss":{"units":{"USD":[{"filed":"2026-02-20","start":"2025-01-01","end":"2025-12-31","val":400}]}},
                "NetIncomeLoss":{"units":{"USD":[{"filed":"2026-02-20","start":"2025-01-01","end":"2025-12-31","val":200}]}},
                "NetCashProvidedByUsedInOperatingActivities":{"units":{"USD":[{"filed":"2026-02-20","start":"2025-01-01","end":"2025-12-31","val":400}]}},
                "PaymentsToAcquirePropertyPlantAndEquipment":{"units":{"USD":[{"filed":"2026-02-20","start":"2025-01-01","end":"2025-12-31","val":150}]}},
                "CashAndCashEquivalentsAtCarryingValue":{"units":{"USD":[{"filed":"2026-02-20","end":"2025-12-31","val":300}]}},
                "LongTermDebtNoncurrent":{"units":{"USD":[{"filed":"2026-02-20","end":"2025-12-31","val":100}]}},
                "StockholdersEquity":{"units":{"USD":[{"filed":"2026-02-20","end":"2025-12-31","val":200}]}},
                "EntityCommonStockSharesOutstanding":{"units":{"shares":[{"filed":"2026-02-20","end":"2025-12-31","val":100}]}},
                "EarningsPerShareDiluted":{"units":{"USD/shares":[{"filed":"2026-02-20","start":"2025-01-01","end":"2025-12-31","val":2.0}]}}
              }}
            }
        """
        result = parse_sec_facts(payload, security(), 100.0)

        self.assertEqual(1000.0, result.revenue)
        self.assertEqual(0.6, result.gross_margin)
        self.assertEqual(0.4, result.operating_margin)
        self.assertEqual(0.2, result.profit_margin)
        self.assertEqual(250.0, result.free_cash_flow)
        self.assertEqual(10_000.0, result.market_cap)
        self.assertEqual(50.0, result.pe)
        self.assertEqual(50.0, result.pb)
        self.assertEqual([SEC_SOURCE_NAME], result.data_sources)
        self.assertEqual("2025-12-31", result.latest_period)

    def test_sec_facts_missing_concepts_yield_nulls_not_errors(self):
        payload = """{"facts":{"us-gaap":{"NetIncomeLoss":{"units":{"USD":[{"end":"2025-12-31","val":-50}]}}}}}"""
        result = parse_sec_facts(payload, security(), 0.0)

        self.assertIsNone(result.revenue)
        self.assertIsNone(result.market_cap)
        self.assertIsNone(result.pe)

    def test_sec_facts_never_mix_periods_across_concepts(self):
        """同一 end 下单季与全年并存、且各概念披露进度不同时，利润率必须整期取数。

        回归场景（英伟达实报）：营收取到某季 269 亿、成本/净利取到全年，
        算出毛利率 -65.5%、营业利润率 435.7% 这种跨期除法。
        """
        payload = """
        {"facts":{"us-gaap":{
          "RevenueFromContractWithCustomerExcludingAssessedTax":{"units":{"USD":[
            {"start":"2025-01-01","end":"2025-12-31","val":1000,"filed":"2026-02-20"},
            {"start":"2025-10-01","end":"2025-12-31","val":300,"filed":"2026-02-20"}
          ]}},
          "CostOfRevenue":{"units":{"USD":[
            {"start":"2025-01-01","end":"2025-12-31","val":400,"filed":"2026-02-20"}
          ]}},
          "OperatingIncomeLoss":{"units":{"USD":[
            {"start":"2025-01-01","end":"2025-12-31","val":400,"filed":"2026-02-20"},
            {"start":"2025-10-01","end":"2025-12-31","val":120,"filed":"2026-02-20"}
          ]}},
          "NetIncomeLoss":{"units":{"USD":[
            {"start":"2025-01-01","end":"2025-12-31","val":200,"filed":"2026-02-20"}
          ]}}
        }}}
        """
        result = parse_sec_facts(payload, security(), 0.0)

        # 单季营收 300 不会和全年成本 400 相除；整期取全年 → (1000-400)/1000
        self.assertEqual(1000.0, result.revenue)
        self.assertEqual(0.6, result.gross_margin)
        self.assertEqual(0.4, result.operating_margin)
        self.assertEqual(0.2, result.profit_margin)
        self.assertEqual("2025-12-31", result.latest_period)

    def test_sec_facts_prefers_latest_end_with_widest_coverage(self):
        """end 更新到新一期时整期切换：营收/成本/净利都用新一期，不和旧期混。"""
        payload = """
        {"facts":{"us-gaap":{
          "RevenueFromContractWithCustomerExcludingAssessedTax":{"units":{"USD":[
            {"start":"2025-01-01","end":"2025-12-31","val":1000,"filed":"2026-02-20"},
            {"start":"2026-01-01","end":"2026-03-31","val":500,"filed":"2026-05-01"}
          ]}},
          "CostOfRevenue":{"units":{"USD":[
            {"start":"2025-01-01","end":"2025-12-31","val":400,"filed":"2026-02-20"},
            {"start":"2026-01-01","end":"2026-03-31","val":100,"filed":"2026-05-01"}
          ]}},
          "NetIncomeLoss":{"units":{"USD":[
            {"start":"2025-01-01","end":"2025-12-31","val":200,"filed":"2026-02-20"},
            {"start":"2026-01-01","end":"2026-03-31","val":300,"filed":"2026-05-01"}
          ]}}
        }}}
        """
        result = parse_sec_facts(payload, security(), 0.0)

        self.assertEqual(500.0, result.revenue)
        self.assertEqual(0.8, result.gross_margin)   # (500-100)/500
        self.assertEqual(0.6, result.profit_margin)  # 300/500
        self.assertEqual("2026-03-31", result.latest_period)

    def test_sec_search_cik_finds_filer_by_ticker(self):
        text = """
        {"hits":{"hits":[
          {"_source":{"display_names":["MICROSOFT CORP (MSFT) (Filer)"],"ciks":["0000789019"]}},
          {"_source":{"display_names":["SOME FUND (Filer)"],"ciks":["0000000001"]}}
        ]}}
        """
        result = parse_sec_search_cik(text, "msft")

        self.assertEqual("0000789019", result.cik)
        self.assertEqual("MICROSOFT CORP", result.name)

    def test_sec_search_cik_ignores_non_matching_filers(self):
        text = """{"hits":{"hits":[{"_source":{"display_names":["SOME FUND (Filer)"],"ciks":["0000000001"]}}]}}"""

        self.assertIsNone(parse_sec_search_cik(text, "MSFT"))


class SinaQuoteValuationTests(unittest.TestCase):
    """新浪行情的估值字段口径（[12]市值 [13]EPS [14]PE / 港股 [13]PE）。"""

    def test_us_quote_reads_market_cap_eps_and_pe(self):
        text = (
            'var hq_str_gb_msft="微软,509.2200,-1.35,2026-09-29 09:39:59,-6.9500,'
            '505.4700,513.3300,502.2200,550.9900,349.2000,19626939,23892921,'
            '3781236274927,16.85,30.220000,0.00,1.03";'
        )
        values = parse_sina_us_quote(text)

        self.assertEqual(3_781_236_274_927.0, values["market_cap"])
        self.assertEqual(30.22, values["pe"])

    def test_hk_quote_reads_pe(self):
        text = (
            'var hq_str_rt_hk00700="TENCENT,腾讯控股,439.400,439.800,439.400,431.600,'
            '434.800,-5.000,-1.137,434.800,435.000,5994928902.692,13823430,15.803";'
        )
        values = parse_sina_hk_quote(text)

        self.assertEqual(15.803, values["pe"])

    def test_malformed_quote_returns_empty(self):
        self.assertEqual({}, parse_sina_us_quote("var hq_str_gb_x=\"\";"))
        self.assertEqual({}, parse_sina_hk_quote("timeout"))


class MoneyUnitTests(unittest.TestCase):
    """大数字自动缩放：万亿/亿/万 + 币种，报告可读。"""

    def test_trillion_billion_wan_scaling(self):
        self.assertEqual("5.56 万亿美元", money_unit(5_562_213_440_000.0, "USD"))
        self.assertEqual("1778.37 亿美元", money_unit(177_837_000_000.0, "USD"))
        self.assertEqual("1234.50 万美元", money_unit(12_345_000.0, "USD"))
        self.assertEqual("228.86 美元", money_unit(228.86, "USD"))

    def test_hkd_and_negative_values(self):
        self.assertEqual("15.77 亿港币", money_unit(1_577_300_000.0, "HKD"))
        self.assertEqual("-6.70 亿美元", money_unit(-670_000_000.0, "USD"))

    def test_report_uses_units(self):
        data = _empty_fundamentals(security(), price=228.86)
        data.currency = "USD"
        data.revenue = 177_837_000_000.0
        data.market_cap = 5_562_213_440_000.0
        report = format_report(data, "2026-09-29")

        self.assertIn("营收：1778.37 亿美元", report)
        self.assertIn("市值：5.56 万亿美元", report)
        self.assertIn("现价：228.86 美元", report)
        self.assertIn("营收：—", format_report(_empty_fundamentals(security(), price=1.0), "2026-09-29"))


class ValuationRangeTests(unittest.TestCase):
    """股价估值：PE 参考带 × TTM EPS 的价格区间与现价位置。"""

    def _data(self, price, pe):
        data = _empty_fundamentals(security(), price=price)
        data.pe = pe
        data.currency = "USD"
        return data

    def test_price_inside_band_reads_as_in_range(self):
        # price=500, PE=25 → TTM EPS 20 → 参考带 400 ~ 700
        text = valuation_range_text(self._data(price=500.0, pe=25.0))

        self.assertIn("合理区间 400.00 ~ 700.00 美元", text)
        self.assertIn("TTM EPS 20.00", text)
        self.assertIn("落在区间内", text)

    def test_price_above_band_reads_as_expensive(self):
        # PE=40 超出 35 倍带上沿 → 偏贵
        text = valuation_range_text(self._data(price=800.0, pe=40.0))

        self.assertIn("合理区间 400.00 ~ 700.00 美元", text)
        self.assertIn("高出区间上沿", text)
        self.assertIn("偏贵", text)

    def test_price_below_band_reads_as_cheap(self):
        # PE=12 → EPS 25 → 带 500 ~ 875，现价 300 低于下沿
        text = valuation_range_text(self._data(price=300.0, pe=12.0))

        self.assertIn("低于区间下沿", text)
        self.assertIn("偏便宜", text)

    def test_missing_pe_cannot_price_the_stock(self):
        text = valuation_range_text(self._data(price=509.22, pe=None))

        self.assertIn("PE/PB暂缺，给不出合理价格区间", text)


class EastMoneyMappingTests(unittest.TestCase):
    def test_percent_fields_are_converted_to_ratios(self):
        fields = em_indicator_fields({
            "OPERATE_INCOME": 331_839_000_000,
            "GROSS_PROFIT_RATIO": 67.94,
            "NET_PROFIT_RATIO": 40.3,
            "ROE_AVG": 34.04,
        })

        self.assertEqual(331_839_000_000.0, fields["revenue"])
        self.assertAlmostEqual(0.6794, fields["gross_margin"])
        self.assertAlmostEqual(0.403, fields["profit_margin"])
        self.assertAlmostEqual(0.3404, fields["roe"])

    def test_plain_symbol_normalizes_market_suffixes(self):
        self.assertEqual("MSFT", _plain_symbol("us", "MSFT"))
        self.assertEqual("NVDA", _plain_symbol("us", "NVDA.MX"))
        self.assertEqual("MSFT", _plain_symbol("us", "105.MSFT"))
        self.assertEqual("00700", _plain_symbol("hk", "0700.HK"))
        self.assertEqual("00700", _plain_symbol("hk", "00700"))


class MergeTests(unittest.TestCase):
    def test_first_non_null_fields_win_and_sources_merge(self):
        base = _empty_fundamentals(security(), price=10.0)
        base.pe = 28.1
        base.data_sources = [SEC_SOURCE_NAME]
        extra = _empty_fundamentals(security(), price=0.0)
        extra.pe = 99.0
        extra.sector = "Technology"
        extra.data_sources = ["Yahoo Finance"]

        merged = merge_fundamentals(base, extra)

        self.assertEqual(28.1, merged.pe)          # 先取到的优先
        self.assertEqual("Technology", merged.sector)  # 缺失字段由后源补齐
        self.assertEqual(10.0, merged.price)
        self.assertEqual([SEC_SOURCE_NAME, "Yahoo Finance"], merged.data_sources)


class ReportTests(unittest.TestCase):
    def _data(self, **overrides):
        data = _empty_fundamentals(security(), price=225.07)
        for key, value in overrides.items():
            setattr(data, key, value)
        return data

    def test_report_sections_and_source_line_render(self):
        data = self._data(
            revenue=196e9, gross_margin=0.75, profit_margin=0.49, roe=1.03,
            pe=28.1, pb=23.7, summary="AI computing platform.",
            data_sources=[SEC_SOURCE_NAME], latest_period="2025-12-31",
        )
        report = format_report(data, "2026-09-29")

        self.assertIn("港美股基本面分析", report)
        self.assertIn("数据来源：SEC Company Facts｜最新报告期：2025-12-31", report)
        for section in ("一、快速结论", "二、盈利能力", "三、现金流与资产负债",
                        "四、业务与竞争优势", "五、估值", "六、风险提示", "综合结论："):
            self.assertIn(section, report)
        self.assertIn("毛利率：75.0%｜", report)

    def test_missing_fields_render_as_dash_not_zero(self):
        report = format_report(self._data(), "2026-09-29")

        self.assertIn("营收：—", report)
        self.assertIn("TTM PE：—", report)
        self.assertIn("PE/PB暂缺，不能给出可靠估值判断", report)

    def test_risk_items_flag_expensive_negative_fcf_and_debt(self):
        data = self._data(
            pe=50.0, free_cash_flow=-1.0, cash=10.0, debt=20.0, summary="有简介",
        )

        items = risk_items(data)

        self.assertEqual(3, len(items))
        self.assertIn("估值偏高，依赖未来高增长兑现", items[0])

    def test_missing_summary_is_flagged(self):
        self.assertTrue(any(
            "公司业务简介暂缺" in item for item in risk_items(self._data())
        ))

    def test_quality_conclusion_prefers_net_cash_and_fcf(self):
        data = self._data(profit_margin=0.4, free_cash_flow=10.0, cash=30.0, debt=10.0)

        self.assertEqual(
            "盈利能力和现金创造能力较强，资产负债压力较低",
            quality_conclusion(data),
        )

    def test_valuation_steps_by_pe_bands(self):
        self.assertIn("PE不高", valuation_conclusion(self._data(pe=18.0)))
        self.assertIn("成长股常见的中高区间", valuation_conclusion(self._data(pe=30.0)))
        self.assertIn("估值偏高", valuation_conclusion(self._data(pe=40.0)))

    def test_technical_text_is_inserted_before_conclusion(self):
        report = format_report(self._data(), "2026-09-29", technical_text="技术面：偏强。")

        self.assertIn("七、技术面摘要", report)
        self.assertLess(report.index("七、技术面摘要"), report.index("综合结论："))


if __name__ == "__main__":
    unittest.main()
