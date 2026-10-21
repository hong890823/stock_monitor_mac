#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""近两日真利好/真利空判定（news_impact）的口径测试。

不联网：网络取数全部 monkeypatch 模块函数，AI 判定注入假 chat_fn。
锁的行为：
1. 两日窗口的边界（前一日/当日命中，更早剔除）；
2. 降级口径的映射（GOOD→利好、ALERT→利空、WATCH 偏利空→利空、纯观察不显示、
   多空并存风险优先）；
3. AI 判定成功时「无」结论必须被尊重——不能让关键词兜底把 AI 判无的回购
   又翻成利好；
4. AI 失败降级不抛异常；
5. 按日缓存：同一天第二轮不打接口，定稿终刷一轮，重启恢复当日结果。
"""

import json
import tempfile
import unittest

import news_impact as ni
from fundamentals import Announcement
from news_impact import (
    NewsImpactService,
    announcement_events,
    build_ai_messages,
    date_in_two_day_window,
    fallback_direction,
    news_events_from_lines,
    parse_ai_json,
    two_day_start,
)

REF = "2026-10-22"


def _ann(date, title):
    return Announcement(date=date, title=title)


class TwoDayWindowTests(unittest.TestCase):
    def test_start_is_previous_calendar_day(self):
        self.assertEqual(two_day_start("2026-10-22"), "2026-10-21")

    def test_window_keeps_today_and_yesterday(self):
        self.assertTrue(date_in_two_day_window("2026-10-22", REF))
        self.assertTrue(date_in_two_day_window("2026-10-21", REF))
        self.assertFalse(date_in_two_day_window("2026-10-20", REF))
        self.assertFalse(date_in_two_day_window("2026-10-23", REF))
        self.assertFalse(date_in_two_day_window("垃圾", REF))


class AnnouncementEventTests(unittest.TestCase):
    def test_keywords_scan_two_day_window_only(self):
        events = announcement_events([
            _ann("2026-10-22", "关于回购股份的进展公告"),
            _ann("2026-10-21", "关于股东减持计划的提示性公告"),
            _ann("2026-10-15", "关于收到立案调查通知的公告"),  # 窗口外，必须剔除
        ], REF)
        titles = [e["title"] for e in events]
        self.assertIn("关于回购股份的进展公告", titles)
        self.assertIn("关于股东减持计划的提示性公告", titles)
        self.assertNotIn("关于收到立案调查通知的公告", titles)
        self.assertTrue(all(e["source"] == "公告" for e in events))

    def test_non_keyword_announcements_still_reach_ai_input(self):
        # 「签订重大合同」不命中任何关键词，但它是 AI 要看的真利好候选，
        # 不能在进 AI 之前就被关键词过滤掉。
        events = announcement_events([
            _ann("2026-10-22", "关于签订重大合同的公告"),
        ], REF)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "关于签订重大合同的公告")
        self.assertNotIn("level", events[0])


class NewsEventTests(unittest.TestCase):
    def test_lines_filtered_to_window(self):
        lines = [
            "2026-10-22 14:30 中天科技中标海缆大单",
            "2026-10-21 09:05 某券商给予买入评级",
            "2026-10-18 10:00 旧闻不应出现",
        ]
        events = news_events_from_lines(lines, REF)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["title"], "中天科技中标海缆大单")
        self.assertEqual(events[1]["source"], "新闻")

    def test_garbage_lines_are_dropped(self):
        self.assertEqual(news_events_from_lines(["", "短文本", None], REF), [])


class FallbackDirectionTests(unittest.TestCase):
    def test_good_is_positive(self):
        events = [{"source": "公告", "level": "GOOD", "assessment": "偏利好：回购"}]
        self.assertEqual(fallback_direction(events), "利好")

    def test_alert_is_negative(self):
        events = [{"source": "公告", "level": "ALERT", "assessment": "偏利空"}]
        self.assertEqual(fallback_direction(events), "利空")

    def test_watch_with_negative_assessment_is_negative(self):
        events = [{"source": "公告", "level": "WATCH", "assessment": "偏利空：股东减持"}]
        self.assertEqual(fallback_direction(events), "利空")

    def test_plain_watch_is_not_shown(self):
        # 问询函/公告更正这类纯观察事项：不是真利空，不显示。
        events = [{"source": "公告", "level": "WATCH", "assessment": "需结合进展判断"}]
        self.assertIsNone(fallback_direction(events))

    def test_risk_wins_when_both_directions_present(self):
        events = [
            {"source": "公告", "level": "GOOD", "assessment": "偏利好"},
            {"source": "公告", "level": "ALERT", "assessment": "偏利空"},
        ]
        self.assertEqual(fallback_direction(events), "利空")

    def test_news_only_events_have_no_fallback_direction(self):
        # 降级口径只认公告——新闻噪声没有关键词兜底的资格。
        events = [{"source": "新闻", "title": "某研报看多"}]
        self.assertIsNone(fallback_direction(events))


class ParseAiJsonTests(unittest.TestCase):
    def test_plain_json(self):
        raw = json.dumps({
            "sh600522": {"direction": "利好", "note": "回购", "evidence": ["A"]},
        }, ensure_ascii=False)
        parsed = parse_ai_json(raw, ["sh600522"])
        self.assertEqual(parsed["sh600522"]["direction"], "利好")
        self.assertEqual(parsed["sh600522"]["evidence"], ["A"])

    def test_markdown_fence_is_stripped(self):
        raw = "```json\n{\"sz002185\": {\"direction\": \"利空\", \"note\": \"减持\", \"evidence\": []}}\n```"
        parsed = parse_ai_json(raw, ["sz002185"])
        self.assertEqual(parsed["sz002185"]["direction"], "利空")

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_ai_json("我觉得都不错", ["sh600522"])

    def test_invalid_direction_becomes_none(self):
        raw = '{"sh600522": {"direction": "大涨", "note": "x", "evidence": []}}'
        parsed = parse_ai_json(raw, ["sh600522"])
        self.assertEqual(parsed["sh600522"]["direction"], "无")

    def test_missing_code_entry_defaults_to_none(self):
        parsed = parse_ai_json("{}", ["sh600522"])
        self.assertEqual(parsed["sh600522"]["direction"], "无")

    def test_evidence_capped(self):
        raw = '{"c": {"direction": "利好", "note": "", "evidence": ["1", "2", "3"]}}'
        self.assertEqual(len(parse_ai_json(raw, ["c"])["c"]["evidence"]), 2)


class BuildAiMessagesTests(unittest.TestCase):
    def test_messages_carry_events_and_rules(self):
        events_by_code = {
            "sh600522": {"name": "中天科技", "events": [
                {"source": "公告", "date": "2026-10-22", "title": "回购公告", "reason": "股份回购"},
                {"source": "新闻", "date": "2026-10-21", "title": "某研报看多"},
            ]},
        }
        messages = build_ai_messages(events_by_code)
        self.assertEqual(len(messages), 2)
        self.assertIn("中天科技", messages[1]["content"])
        self.assertIn("[公告 2026-10-22] 回购公告", messages[1]["content"])
        self.assertIn("[新闻 2026-10-21] 某研报看多", messages[1]["content"])
        self.assertIn("JSON", messages[0]["content"])


def _positions():
    return [{"code": "sh600522", "name": "中天科技"}]


def _patch_fetches(case, announcements=None, news_lines=None):
    """替换模块级取数函数；返回 fetch 计数器。"""
    counter = {"ann": 0, "news": 0}

    def fake_ann(code):
        counter["ann"] += 1
        return announcements or []

    def fake_news(code, ref_date):
        counter["news"] += 1
        return news_events_from_lines(news_lines or [], ref_date)

    case.patch_ann = None  # 占位：仅防 lint 报未使用属性
    original_ann = ni.fetch_announcements_page
    original_news = ni.fetch_news_events
    ni.fetch_announcements_page = fake_ann
    ni.fetch_news_events = fake_news
    case.addCleanup(setattr, ni, "fetch_announcements_page", original_ann)
    case.addCleanup(setattr, ni, "fetch_news_events", original_news)
    return counter


class NewsImpactServiceTests(unittest.TestCase):
    def _service(self, chat_fn=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return NewsImpactService(self._tmp.name, chat_fn=chat_fn)

    def test_ai_success_respects_none_verdict(self):
        # AI 判「无」时关键词兜底不得翻案：公告是 GOOD（回购），AI 说是研报噪声 → 无。
        counter = _patch_fetches(
            self,
            announcements=[_ann("2026-10-22", "关于回购股份的进展公告")],
            news_lines=["2026-10-22 10:00 某研报给予买入评级"],
        )
        chat = lambda messages: json.dumps(
            {"sh600522": {"direction": "无", "note": "", "evidence": []}},
            ensure_ascii=False,
        )
        service = self._service(chat_fn=chat)
        impacts = service.get_impacts(_positions(), REF)
        self.assertEqual(len(impacts), 1)
        self.assertEqual(impacts[0]["direction"], "无")
        self.assertTrue(impacts[0]["ai"])
        self.assertEqual(counter["ann"], 1)

    def test_ai_positive_with_evidence_matching(self):
        _patch_fetches(
            self,
            announcements=[_ann("2026-10-22", "关于签订重大合同的公告")],
            news_lines=["2026-10-22 11:00 中天科技中标海缆项目"],
        )
        chat = lambda messages: json.dumps({
            "sh600522": {
                "direction": "利好",
                "note": "签订重大合同，订单落地",
                "evidence": ["关于签订重大合同的公告", "中天科技中标海缆项目"],
            },
        }, ensure_ascii=False)
        impacts = self._service(chat_fn=chat).get_impacts(_positions(), REF)
        self.assertEqual(impacts[0]["direction"], "利好")
        self.assertEqual(impacts[0]["note"], "签订重大合同，订单落地")
        titles = [e["title"] for e in impacts[0]["evidence"]]
        self.assertIn("关于签订重大合同的公告", titles)

    def test_ai_failure_falls_back_to_announcement_keywords(self):
        _patch_fetches(
            self,
            announcements=[_ann("2026-10-21", "关于股东减持计划的提示性公告")],
            news_lines=["2026-10-21 09:00 噪声新闻"],
        )

        def broken_chat(messages):
            raise RuntimeError("DeepSeek 未配置")

        service = self._service(chat_fn=broken_chat)
        impacts = service.get_impacts(_positions(), REF)
        self.assertEqual(impacts[0]["direction"], "利空")
        self.assertFalse(impacts[0]["ai"])

    def test_cache_hits_do_not_refetch(self):
        counter = _patch_fetches(
            self, announcements=[_ann("2026-10-22", "关于回购股份的进展公告")],
        )
        chat = lambda messages: json.dumps(
            {"sh600522": {"direction": "利好", "note": "n", "evidence": []}},
            ensure_ascii=False,
        )
        service = self._service(chat_fn=chat)
        service.get_impacts(_positions(), REF)
        service.get_impacts(_positions(), REF)
        self.assertEqual(counter["ann"], 1)
        self.assertEqual(counter["news"], 1)

    def test_final_refresh_refetches_once(self):
        counter = _patch_fetches(
            self, announcements=[_ann("2026-10-22", "关于回购股份的进展公告")],
        )
        chat = lambda messages: json.dumps(
            {"sh600522": {"direction": "利好", "note": "n", "evidence": []}},
            ensure_ascii=False,
        )
        service = self._service(chat_fn=chat)
        service.get_impacts(_positions(), REF, final_refresh=False)
        service.get_impacts(_positions(), REF, final_refresh=True)
        service.get_impacts(_positions(), REF, final_refresh=True)  # 终刷只刷一次
        self.assertEqual(counter["ann"], 2)

    def test_state_survives_restart(self):
        _patch_fetches(
            self, announcements=[_ann("2026-10-22", "关于回购股份的进展公告")],
        )
        chat = lambda messages: json.dumps(
            {"sh600522": {"direction": "利好", "note": "n", "evidence": []}},
            ensure_ascii=False,
        )
        first = self._service(chat_fn=chat)
        impacts = first.get_impacts(_positions(), REF)
        # 重启：新实例、chat_fn 不可用也不该被打到
        second = NewsImpactService(self._tmp.name, chat_fn=lambda m: (_ for _ in ()).throw(AssertionError("不应再调 AI")))
        self.assertEqual(second.get_impacts(_positions(), REF), impacts)

    def test_new_day_refreshes(self):
        counter = _patch_fetches(
            self, announcements=[_ann("2026-10-22", "关于回购股份的进展公告")],
        )
        chat = lambda messages: json.dumps(
            {"sh600522": {"direction": "利好", "note": "n", "evidence": []}},
            ensure_ascii=False,
        )
        service = self._service(chat_fn=chat)
        service.get_impacts(_positions(), REF)
        service.get_impacts(_positions(), "2026-10-23")
        self.assertEqual(counter["ann"], 2)

    def test_positions_change_triggers_refresh(self):
        counter = _patch_fetches(self)
        chat = lambda messages: json.dumps({}, ensure_ascii=False)
        service = self._service(chat_fn=chat)
        service.get_impacts(_positions(), REF)
        service.get_impacts(_positions() + [{"code": "sz002185", "name": "华天科技"}], REF)
        self.assertEqual(counter["ann"], 2)

    def test_missing_inputs_return_none(self):
        service = self._service(chat_fn=lambda m: "")
        self.assertIsNone(service.get_impacts([], REF))
        self.assertIsNone(service.get_impacts(_positions(), None))
        self.assertIsNone(service.get_impacts(_positions(), ""))

    def test_announcement_fetch_failure_uses_no_events_not_crash(self):
        original = ni.fetch_announcements_page
        ni.fetch_announcements_page = lambda code: (_ for _ in ()).throw(RuntimeError("网络断"))
        self.addCleanup(setattr, ni, "fetch_announcements_page", original)
        _patch_fetches(self, news_lines=["2026-10-22 10:00 中标大单"])
        chat = lambda messages: json.dumps(
            {"sh600522": {"direction": "利好", "note": "中标", "evidence": ["中标大单"]}},
            ensure_ascii=False,
        )
        impacts = self._service(chat_fn=chat).get_impacts(_positions(), REF)
        self.assertEqual(impacts[0]["direction"], "利好")


if __name__ == "__main__":
    unittest.main()
