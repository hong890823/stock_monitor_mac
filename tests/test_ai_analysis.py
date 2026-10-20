#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 分析（DeepSeek）上下文汇总、Prompt 拼装与接口调用测试。网络请求全部 mock。"""

import json
import os
import unittest
from unittest import mock

import pandas as pd

import ai_analysis
from ai_analysis import (
    AnalysisContext,
    TOOLS_SCHEMA,
    ask,
    build_messages,
    build_context_materials,
    chat_completion,
    dispatch,
    format_report,
    kind_of,
    load_config,
    load_recent_market_context,
    load_capital_flow_context,
    parse_bing_results,
    resolve_input,
    search_project_docs,
    trend_overview,
)


class ConfigTests(unittest.TestCase):
    def test_defaults_without_config_file(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = load_config(project_dir="/nonexistent")

        self.assertEqual("", config["api_key"])
        self.assertEqual("deepseek-flash", config["model"])
        self.assertEqual("https://api.deepseek.com", config["base_url"])


class CapitalFlowContextTests(unittest.TestCase):
    def test_loads_only_current_platform_flow_evidence(self):
        import tempfile
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "_capital_flow_cache.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "quote_date": "2026-10-12",
                    "generated_at": "2026-10-12T10:01:00",
                    "stock_sectors": {"sh600519": ["白酒"]},
                    "stocks": {
                        "sh600519": {
                            "key": "sh600519", "name": "贵州茅台", "quote_date": "2026-10-12",
                            "as_of": "2026-10-12T10:01:00", "source": "EastMoney.fflow.kline",
                            "scope": "个股", "session": "intraday", "net_yi": 1.2,
                            "net_ratio_pct": 2.4,
                        }
                    },
                    "sectors": {
                        "白酒": {
                            "key": "白酒", "name": "白酒", "quote_date": "2026-10-12",
                            "as_of": "2026-10-12T10:01:00", "source": "AkShare.stock_fund_flow_industry",
                            "scope": "行业/概念板块", "session": "intraday", "net_yi": 8.0,
                        }
                    },
                }, handle, ensure_ascii=False)

            text = load_capital_flow_context(
                tmp, "sh600519", now=datetime(2026, 10, 12, 10, 2)
            )

        self.assertIn("平台估算口径", text)
        self.assertIn("贵州茅台", text)
        self.assertIn("白酒", text)
        self.assertIn("+1.2亿", text)
        self.assertIn("数据时间 2026-10-12 10:01", text)

    def test_expired_flow_evidence_is_not_sent_to_model(self):
        import tempfile
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "_capital_flow_cache.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "quote_date": "2026-10-12",
                    "stocks": {
                        "sh600519": {
                            "key": "sh600519", "name": "贵州茅台", "quote_date": "2026-10-12",
                            "as_of": "2026-10-12T09:00:00", "source": "test",
                            "scope": "个股", "session": "intraday", "net_yi": 1.2,
                        }
                    },
                    "sectors": {},
                    "stock_sectors": {},
                }, handle)

            text = load_capital_flow_context(
                tmp, "sh600519", now=datetime(2026, 10, 12, 10, 0)
            )

        self.assertEqual("", text)

    def test_context_materials_labels_flow_as_auxiliary_evidence(self):
        context = AnalysisContext(
            code="sh600519", name="贵州茅台", kind="stock",
            capital_flow_text="平台估算口径：个股主力净流入 +1.2亿",
        )

        text = build_context_materials(context)

        self.assertIn("【盘中资金流辅助证据】", text)
        self.assertIn("平台估算口径", text)

    def test_env_overrides_config_file(self):
        with mock.patch("builtins.open", mock.mock_open(
            read_data=json.dumps({"api_key": "sk-file", "model": "deepseek-reasoner"})
        )), mock.patch("os.path.exists", return_value=True), mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "sk-env", "DEEPSEEK_MODEL": "deepseek-flash"},
            clear=False,
        ):
            config = load_config(project_dir=".")

        self.assertEqual("sk-env", config["api_key"])
        self.assertEqual("deepseek-flash", config["model"])


class ResolveInputTests(unittest.TestCase):
    def test_index_aliases(self):
        self.assertEqual(("sh000001", "大盘"), resolve_input("大盘"))
        self.assertEqual(("sz399006", "创业板"), resolve_input("创业板"))

    def test_stock_code_passthrough(self):
        self.assertEqual(("sh600519", "sh600519"), resolve_input("sh600519"))
        self.assertEqual(("sz000001", "SZ000001"), resolve_input("SZ000001"))

    def test_search_fn_resolves_name(self):
        results = [{"code": "sh600519", "name": "贵州茅台"}]

        self.assertEqual(
            ("sh600519", "贵州茅台"),
            resolve_input("茅台", search_fn=lambda kw: results),
        )

    def test_global_symbol_goes_through_search(self):
        results = [{"symbol": "NVDA", "name": "NVIDIA Corporation"}]
        code, name = resolve_input("英伟达", search_fn=lambda kw: results)

        self.assertEqual("NVDA", code)
        self.assertEqual("global", kind_of(code))

    def test_unknown_keyword_returns_none(self):
        self.assertIsNone(resolve_input("不存在的东西xyz", search_fn=lambda kw: []))


class KindOfTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual("index", kind_of("sh000001"))
        self.assertEqual("index", kind_of("sz399001"))
        self.assertEqual("stock", kind_of("sh600519"))
        self.assertEqual("global", kind_of("NVDA"))
        self.assertEqual("global", kind_of("00700"))


class TrendOverviewTests(unittest.TestCase):
    def test_reports_changes_range_and_volume(self):
        closes = [10.0] * 55 + [10.5] * 5
        history = pd.DataFrame({
            "close": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "volume": [1000.0] * 55 + [2000.0] * 5,  # 近5日均量/20日均量 = 1.60，放量
        })

        text = trend_overview(history)

        self.assertIn("60日", text)
        self.assertIn("60日区间", text)
        self.assertIn("放量", text)

    def test_short_history_returns_empty(self):
        history = pd.DataFrame({"close": [1.0, 2.0]})
        self.assertEqual("", trend_overview(history))


class MarketEvidenceTests(unittest.TestCase):
    def test_northbound_reader_uses_latest_dated_row_and_returns_trends(self):
        frame = pd.DataFrame({
            "日期": ["2026-10-08", "2026-10-09", "2026-10-12"],
            "当日资金净流入": [10.0, -5.0, -12.0],
        })

        evidence = ai_analysis._read_northbound_latest(frame)

        self.assertEqual("2026-10-12", evidence["date"])
        self.assertEqual(-12.0, evidence["net"])
        self.assertEqual(-7.0, evidence["sum_3d"])
        self.assertEqual(-7.0, evidence["sum_5d"])

    def test_turnover_reader_compares_intraday_with_previous_same_slot(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "_turnover_tracker.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "2026-10-09": {"0": 1000, "30": 3000, "45": 5000},
                    "2026-10-12": {"0": 1200, "30": 3300},
                }, handle)

            evidence = ai_analysis._read_turnover_evidence(
                directory,
                "2026-10-12",
                now=ai_analysis.datetime(2026, 10, 12, 10, 0),
            )

        self.assertEqual("较前一交易日同期", evidence["comparison_label"])
        self.assertEqual(3300.0, evidence["today"])
        self.assertEqual(3000.0, evidence["previous"])
        self.assertEqual("盘中截至10:00", evidence["session"])

    def test_turnover_reader_compares_full_days_after_close(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "_turnover_tracker.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "2026-10-09": {"0": 1000, "240": 17000},
                    "2026-10-12": {"0": 1200, "240": 19000},
                }, handle)

            evidence = ai_analysis._read_turnover_evidence(
                directory,
                "2026-10-12",
                now=ai_analysis.datetime(2026, 10, 12, 15, 30),
            )

        self.assertEqual("较前一交易日全天", evidence["comparison_label"])
        self.assertEqual(19000.0, evidence["today"])
        self.assertEqual(17000.0, evidence["previous"])
        self.assertEqual("收盘后", evidence["session"])


class PromptTests(unittest.TestCase):
    def _context(self):
        return AnalysisContext(
            code="sh600519", name="贵州茅台", kind="stock", price=1400.0,
            technical_text="📊 技术分析摘要\n评分：70分（偏强）",
            fundamentals_text="一、快速概览…",
            trend_text="涨跌幅：20日 +3.2%",
            news_lines=["2026-09-28 茅台发布三季报预告"],
            market_text="大盘缩量震荡",
            missing=["公司业务简介未取到"],
        )

    def test_messages_include_all_sections_and_gaps(self):
        messages = build_messages(self._context())
        user = messages[1]["content"]

        self.assertEqual("system", messages[0]["role"])
        self.assertIn("贵州茅台（sh600519，A股个股）", user)
        self.assertIn("【技术面数据】", user)
        self.assertIn("【基本面报告】", user)
        self.assertIn("【走势与量能】", user)
        self.assertIn("【近期资讯（标题）】", user)
        self.assertIn("【市场环境（盘面总结缓存）】", user)
        self.assertIn("【数据缺口】", user)
        self.assertIn("公司业务简介未取到", user)

    def test_messages_include_portfolio_report(self):
        context = self._context()
        context.portfolio_text = "持仓报告：组合今日下跌，11/12 收跌。"

        user = build_messages(context)[1]["content"]

        self.assertIn("【持仓报告】", user)
        self.assertIn("11/12 收跌", user)

    def test_messages_include_recent_market_context(self):
        context = self._context()
        context.recent_market_text = "沪指近5个交易日：10-12 +0.50%｜区间累计 +1.20%"

        user = build_messages(context)[1]["content"]

        self.assertIn("【近期大盘与板块表现】", user)
        self.assertIn("沪指近5个交易日", user)

    def test_messages_include_timing_and_flow_evidence(self):
        context = self._context()
        context.market_timing_text = "行情日期：2026-10-30｜时点标签：周五、月末附近\n北向净流向：-55.0亿"

        user = build_messages(context)[1]["content"]

        self.assertIn("【时点与资金证据】", user)
        self.assertIn("北向净流向：-55.0亿", user)

    def test_system_prompt_forbids_fabrication_and_advice(self):
        system = build_messages(self._context())[0]["content"]

        self.assertIn("不构成投资建议", system)
        self.assertIn("绝不编造", system)
        self.assertIn("持仓", system)
        self.assertIn("最近几日", system)
        self.assertIn("成交额", system)
        self.assertIn("日期本身", system)

    def test_system_prompt_requires_concise_actionable_analysis(self):
        system = build_messages(self._context())[0]["content"]

        self.assertIn("默认不超过600字", system)
        self.assertIn("【关键依据】", system)
        self.assertIn("【操作建议】", system)
        self.assertIn("无持仓", system)
        self.assertIn("已持有", system)
        self.assertIn("触发条件", system)
        self.assertIn("失效条件", system)

    def test_system_prompt_requires_evidence_discipline(self):
        system = build_messages(self._context())[0]["content"]

        self.assertIn("事实与推断分开", system)
        self.assertIn("证据与结论一一对应", system)
        self.assertIn("数据矛盾", system)
        self.assertIn("不要为了完整而强行给具体价位", system)

    def test_system_prompt_protects_deep_loss_holdings_from_panic_exit(self):
        system = build_messages(self._context())[0]["content"]

        self.assertIn("深套持仓", system)
        self.assertIn("不得把无持仓入场失效位直接套用于已有持仓", system)
        self.assertIn("不建议在破位低位直接割肉", system)
        self.assertIn("逻辑/基本面已破坏", system)
        self.assertIn("等反弹", system)
        self.assertIn("逻辑还在", system)
        self.assertIn("重新站回支撑", system)
        self.assertIn("成本或浮亏未取到", system)

    def test_system_prompt_prefers_rolling_t_for_deep_loss_holdings(self):
        system = build_messages(self._context())[0]["content"]

        self.assertIn("滚动做T", system)
        self.assertIn("保留底仓", system)
        self.assertIn("冲高减出", system)
        self.assertIn("回踩接回", system)
        self.assertIn("降低持仓成本", system)
        self.assertIn("不承诺回本", system)
        self.assertIn("再创新低取消", system)

    def test_system_prompt_requires_plain_language_for_trading_indicators(self):
        system = build_messages(self._context())[0]["content"]

        self.assertIn("专业术语必须就地用通俗中文解释", system)
        self.assertIn("M20（20日均线）", system)
        self.assertIn("ATR（近期正常波动幅度）", system)
        self.assertIn("量比（当前成交量相对常态）", system)
        self.assertIn("把价差门槛换算成具体卖出价与接回价示例", system)
        self.assertIn("不是保证收益的硬性阈值", system)
        self.assertIn("交易成本", system)


class ChatCompletionTests(unittest.TestCase):
    def _config(self):
        return {"api_key": "sk-test", "model": "deepseek-flash", "base_url": "https://api.deepseek.com"}

    def test_posts_openai_compatible_payload_and_reads_content(self):
        response = mock.MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": "【综合结论】看多"}}]
        }
        with mock.patch("ai_analysis.requests.post", return_value=response) as post:
            content = chat_completion([{"role": "user", "content": "hi"}], self._config())

        self.assertEqual("【综合结论】看多", content)
        url = post.call_args[0][0]
        payload = post.call_args[1]["json"]
        headers = post.call_args[1]["headers"]
        self.assertEqual("https://api.deepseek.com/chat/completions", url)
        self.assertEqual("deepseek-flash", payload["model"])
        self.assertEqual("Bearer sk-test", headers["Authorization"])

    def test_base_url_with_v1_suffix_is_normalized(self):
        response = mock.MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        config = dict(self._config(), base_url="https://api.deepseek.com/v1")
        with mock.patch("ai_analysis.requests.post", return_value=response) as post:
            chat_completion([], config)

        self.assertEqual("https://api.deepseek.com/chat/completions", post.call_args[0][0])

    def test_missing_api_key_raises_with_instructions(self):
        config = {"api_key": "", "model": "deepseek-flash", "base_url": "https://api.deepseek.com"}
        with self.assertRaises(RuntimeError) as ctx:
            chat_completion([], config)

        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))

    def test_empty_content_from_reasoning_model_raises(self):
        # 推理模型的思考吃光 max_tokens 时 content 为空，必须报错而不是出空报告
        response = mock.MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{
                "message": {"role": "assistant", "content": "", "reasoning_content": "思考…"},
                "finish_reason": "length",
            }]
        }
        with mock.patch("ai_analysis.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                chat_completion([], self._config())

        self.assertIn("空正文", str(ctx.exception))

    def test_unexpected_response_shape_raises(self):
        response = mock.MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"weird": True}
        with mock.patch("ai_analysis.requests.post", return_value=response):
            with self.assertRaises(RuntimeError):
                chat_completion([], self._config())


class FormatReportTests(unittest.TestCase):
    def test_report_header_and_source_line(self):
        context = AnalysisContext(
            code="sh000001", name="上证指数", kind="index", price=3800.0,
            technical_text="技术", trend_text="走势",
        )
        report = format_report(context, "【综合结论】…", "deepseek-flash")

        self.assertIn("🤖 上证指数（sh000001）AI 分析总结", report)
        self.assertIn("模型 deepseek-flash", report)
        self.assertIn("输入素材：技术面、走势与量能", report)
        self.assertIn("【综合结论】", report)


class AnalyzeFlowTests(unittest.TestCase):
    def test_analyze_happy_path_composes_pipeline(self):
        context = AnalysisContext(
            code="sh600519", name="贵州茅台", kind="stock", price=1400.0,
            technical_text="技术",
        )
        with mock.patch.object(ai_analysis, "load_config", return_value={
            "api_key": "sk-test", "model": "deepseek-flash", "base_url": "https://api.deepseek.com",
        }), mock.patch.object(
            ai_analysis, "resolve_input", return_value=("sh600519", "贵州茅台")
        ), mock.patch.object(ai_analysis, "gather_context", return_value=context), \
                mock.patch.object(ai_analysis, "chat_completion", return_value="【综合结论】…") as chat:
            report = ai_analysis.analyze("贵州茅台", project_dir=".")

        self.assertIn("AI 分析总结", report)
        self.assertEqual(1, chat.call_count)

    def test_analyze_without_key_raises(self):
        with mock.patch.object(ai_analysis, "load_config", return_value={
            "api_key": "", "model": "deepseek-flash", "base_url": "https://api.deepseek.com",
        }):
            with self.assertRaises(RuntimeError) as ctx:
                ai_analysis.analyze("贵州茅台")

        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))


class WebSearchParserTests(unittest.TestCase):
    def test_parses_bing_result_blocks(self):
        html = """
        <li class="b_algo"><h2 class=""><a href="https://example.com/a">贵州茅台护城河分析</a></h2>
        <p>茅台的品牌与渠道构成宽护城河，毛利率常年高于90%。</p>
        <ul><li>嵌套 li 不应截断结果块</li></ul></li>
        <li class="b_algo"><h2 class=""><a href="https://example.com/b">另一条结果</a></h2>
        <p>简要摘要<b>加粗</b>文本。</p></li>
        <li class="b_algo"><h2 class=""><span>没有链接的块</span></h2><p>应被跳过</p></li>
        """
        results = parse_bing_results(html, max_results=5)

        self.assertEqual(2, len(results))
        self.assertEqual("贵州茅台护城河分析", results[0]["title"])
        self.assertIn("宽护城河", results[0]["snippet"])
        self.assertEqual("https://example.com/a", results[0]["url"])

    def test_malformed_html_returns_empty(self):
        self.assertEqual([], parse_bing_results("<html>验证</html>"))


class ProjectDocsSearchTests(unittest.TestCase):
    """程序内部知识检索：问自身策略/规则时靠它，而不是上网搜。"""

    def _project(self, tmp):
        os.makedirs(os.path.join(tmp, "docs"), exist_ok=True)
        with open(os.path.join(tmp, "docs", "513.md"), "w", encoding="utf-8") as f:
            f.write("# 513 选股策略\n5 连阳 → 倍量阳 → 3 天缩量\n量能基线取窗口前 20 日均量\n")
        with open(os.path.join(tmp, "five_one_three.py"), "w", encoding="utf-8") as f:
            f.write('"""513 低位形态选股。"""\nMIN_HISTORY_DAYS = 60\n')
        with open(os.path.join(tmp, "other.md"), "w", encoding="utf-8") as f:
            f.write("无关内容，天气不错\n")

    def test_finds_strategy_doc_with_source_and_context(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp)
            text = search_project_docs("513选股策略", project_dir=tmp)

        self.assertIn("docs/513.md", text)
        self.assertIn("5 连阳 → 倍量阳 → 3 天缩量", text)

    def test_source_code_docstring_is_searchable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp)
            text = search_project_docs("MIN_HISTORY_DAYS", project_dir=tmp)

        self.assertIn("five_one_three.py", text)
        self.assertIn("MIN_HISTORY_DAYS = 60", text)

    def test_no_hit_explains_and_suggests_web(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp)
            text = search_project_docs("量子引力护城河", project_dir=tmp)

        self.assertIn("没有找到", text)
        self.assertIn("search_web", text)


class DispatchTests(unittest.TestCase):
    def test_portfolio_report_tool_reads_cached_report(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "_portfolio_daily_report.txt"), "w", encoding="utf-8") as handle:
                handle.write("持仓报告：组合今日下跌，11/12 收跌。")

            text = dispatch("get_portfolio_report", {}, project_dir=tmp)

        self.assertIn("11/12 收跌", text)

    def test_recent_market_context_combines_index_history_and_sector_cache(self):
        import tempfile

        history = pd.DataFrame({
            "date": ["2026-10-03", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12"],
            "close": [99, 100, 101, 100, 102, 103],
        })
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "_sector_tech_cache.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "date": "2026-10-12",
                    "techs": {
                        "发电设备": {
                            "change_pct": 1.74,
                            "relative_strength": 2.59,
                            "above_ma20": True,
                            "trend_weak": False,
                        }
                    },
                }, handle, ensure_ascii=False)

            with mock.patch.object(ai_analysis, "fetch_history", return_value=history), \
                    mock.patch.object(ai_analysis, "fetch_sector_history", return_value=history):
                text = load_recent_market_context(tmp)

        self.assertIn("沪指近5个交易日", text)
        self.assertIn("区间累计", text)
        self.assertIn("板块发电设备近5个交易日", text)
        self.assertIn("相对沪指20日 +2.59pp", text)

    def test_project_docs_tool_is_dispatched(self):
        with mock.patch("ai_analysis.search_project_docs", return_value="命中") as search:
            text = dispatch("search_project_docs", {"query": "513"})

        self.assertEqual("命中", text)
        search.assert_called_once()
    def test_search_web_records_sources_for_report(self):
        collector = {"web_sources": []}
        results = [{"title": "T", "snippet": "S", "url": "https://x.example/1"}]
        with mock.patch("ai_analysis.search_web", return_value=results):
            text = dispatch("search_web", {"query": "护城河"}, collector=collector)

        self.assertIn("https://x.example/1", text)
        self.assertEqual(["https://x.example/1"], collector["web_sources"])

    def test_unknown_tool_returns_error_text(self):
        self.assertIn("未知工具", dispatch("no_such_tool", {}))

    def test_unresolvable_code_is_explained(self):
        text = dispatch("get_quote", {"code": "不存在xyz"}, search_fn=lambda kw: [])
        self.assertIn("没有识别出", text)


class AskAgentTests(unittest.TestCase):
    def _config(self):
        return {"api_key": "sk-test", "model": "deepseek-flash", "base_url": "https://api.deepseek.com"}

    def _response(self, message, finish_reason="stop"):
        resp = mock.MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{"message": message, "finish_reason": finish_reason}]}
        return resp

    def test_qa_prompt_requires_concise_actionable_market_answers(self):
        final_msg = {"role": "assistant", "content": "结论：先观察。"}
        with mock.patch.object(ai_analysis, "load_config", return_value=self._config()), \
                mock.patch("ai_analysis.requests.post", return_value=self._response(final_msg)) as post:
            ask("贵州茅台现在怎么操作？", project_dir=".")

        system_content = post.call_args[1]["json"]["messages"][0]["content"]
        self.assertIn("默认不超过500字", system_content)
        self.assertIn("【关键依据】", system_content)
        self.assertIn("【操作建议】", system_content)
        self.assertIn("触发条件", system_content)
        self.assertIn("失效条件", system_content)

    def test_qa_prompt_protects_deep_loss_holdings_from_panic_exit(self):
        final_msg = {"role": "assistant", "content": "结论：先观察。"}
        with mock.patch.object(ai_analysis, "load_config", return_value=self._config()), \
                mock.patch("ai_analysis.requests.post", return_value=self._response(final_msg)) as post:
            ask("特变电工怎么操作？", project_dir=".")

        system_content = post.call_args[1]["json"]["messages"][0]["content"]
        self.assertIn("深套持仓", system_content)
        self.assertIn("不得把无持仓入场失效位直接套用于已有持仓", system_content)
        self.assertIn("不建议在破位低位直接割肉", system_content)
        self.assertIn("逻辑/基本面已破坏", system_content)
        self.assertIn("逻辑还在", system_content)
        self.assertIn("成本或浮亏未取到", system_content)

    def test_qa_prompt_prefers_rolling_t_for_deep_loss_holdings(self):
        final_msg = {"role": "assistant", "content": "结论：先观察。"}
        with mock.patch.object(ai_analysis, "load_config", return_value=self._config()), \
                mock.patch("ai_analysis.requests.post", return_value=self._response(final_msg)) as post:
            ask("特变电工怎么操作？", project_dir=".")

        system_content = post.call_args[1]["json"]["messages"][0]["content"]
        self.assertIn("滚动做T", system_content)
        self.assertIn("保留底仓", system_content)
        self.assertIn("冲高减出", system_content)
        self.assertIn("回踩接回", system_content)
        self.assertIn("降低持仓成本", system_content)
        self.assertIn("不承诺回本", system_content)
        self.assertIn("再创新低取消", system_content)

    def test_qa_prompt_requires_plain_language_for_trading_indicators(self):
        final_msg = {"role": "assistant", "content": "结论：先观察。"}
        with mock.patch.object(ai_analysis, "load_config", return_value=self._config()), \
                mock.patch("ai_analysis.requests.post", return_value=self._response(final_msg)) as post:
            ask("特变电工现在怎么操作？", project_dir=".")

        system_content = post.call_args[1]["json"]["messages"][0]["content"]
        self.assertIn("专业术语必须就地用通俗中文解释", system_content)
        self.assertIn("M20（20日均线）", system_content)
        self.assertIn("ATR（近期正常波动幅度）", system_content)
        self.assertIn("量比（当前成交量相对常态）", system_content)
        self.assertIn("把价差门槛换算成具体卖出价与接回价示例", system_content)
        self.assertIn("不是保证收益的硬性阈值", system_content)
        self.assertIn("交易成本", system_content)

    def test_agent_loop_calls_tools_then_answers(self):
        tool_call_msg = {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "get_quote", "arguments": json.dumps({"code": "sh600519"})},
            }],
        }
        final_msg = {"role": "assistant", "content": "结论：现价偏高，观望。"}
        responses = [
            self._response(tool_call_msg, finish_reason="tool_calls"),
            self._response(final_msg),
        ]
        with mock.patch.object(ai_analysis, "load_config", return_value=self._config()), \
                mock.patch("ai_analysis.requests.post", side_effect=responses) as post, \
                mock.patch("ai_analysis.dispatch", return_value="现价 1400.00") as disp:
            report = ask("茅台现在贵吗？", project_dir=".")

        self.assertIn("结论：现价偏高，观望。", report)
        self.assertIn("调用工具：get_quote", report)
        self.assertEqual(2, post.call_count)
        self.assertEqual(1, disp.call_count)
        # 第二轮请求必须带上工具结果
        second_payload = post.call_args_list[1][1]["json"]
        roles = [m.get("role") for m in second_payload["messages"]]
        self.assertIn("tool", roles)

    def test_stock_question_preloads_all_four_reports(self):
        final_msg = {"role": "assistant", "content": "综合结论：先观察。"}
        bundle = (
            "【技术面数据】技术报告\n"
            "【基本面报告】基本面报告\n"
            "【持仓报告】持仓报告\n"
            "【市场环境（盘面总结缓存）】盘面总结"
        )
        with mock.patch.object(ai_analysis, "load_config", return_value=self._config()), \
                mock.patch.object(ai_analysis, "resolve_input", return_value=("sh600519", "贵州茅台")), \
                mock.patch.object(ai_analysis, "dispatch", return_value=bundle) as dispatch_mock, \
                mock.patch("ai_analysis.requests.post", return_value=self._response(final_msg)) as post:
            report = ask("全面分析贵州茅台", project_dir=".")

        self.assertIn("综合结论：先观察。", report)
        user_content = post.call_args[1]["json"]["messages"][1]["content"]
        self.assertIn("【技术面数据】技术报告", user_content)
        self.assertIn("【基本面报告】基本面报告", user_content)
        self.assertIn("【持仓报告】持仓报告", user_content)
        self.assertIn("【市场环境（盘面总结缓存）】盘面总结", user_content)
        dispatch_mock.assert_called_once()
        self.assertEqual("get_analysis_bundle", dispatch_mock.call_args.args[0])

    def test_agent_tools_schema_is_sent(self):
        final_msg = {"role": "assistant", "content": "好的"}
        with mock.patch.object(ai_analysis, "load_config", return_value=self._config()), \
                mock.patch("ai_analysis.requests.post", return_value=self._response(final_msg)) as post:
            ask("你好", project_dir=".")

        payload = post.call_args[1]["json"]
        self.assertEqual(TOOLS_SCHEMA, payload["tools"])

    def test_max_rounds_guard_raises(self):
        tool_call_msg = {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "call_x", "type": "function",
                "function": {"name": "get_market_summary", "arguments": "{}"},
            }],
        }
        with mock.patch.object(ai_analysis, "load_config", return_value=self._config()), \
                mock.patch("ai_analysis.requests.post", return_value=self._response(tool_call_msg, "tool_calls")), \
                mock.patch("ai_analysis.dispatch", return_value="数据"):
            with self.assertRaises(RuntimeError) as ctx:
                ask("一直调工具", project_dir=".", max_rounds=2)

        self.assertIn("最大工具调用轮数", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
