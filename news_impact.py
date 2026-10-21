#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""近两日真利好/真利空汇总（持仓报告「七、近两日利好/利空」）。

「真正的利好利空」= 对经营、业绩、股东权益有实质影响的事实。研报评级、
机构目标价、涨跌复盘、盘面描述、概念联想这类噪声不算——这正是要 AI 判
而不是关键词硬匹配新闻的原因。

两个来源 + 一道判定：
1. 公司公告（东财公告 API 标题级，复用 fundamentals 的 URL 口径与关键词分级
   _GOOD/_ALERT/_WATCH）：官方事实，最「真」；
2. 东财个股新闻标题（复用 ai_analysis.fetch_news）：噪声多，只作为 AI 的输入。

判定主路径是 DeepSeek 一次批量调用（逐股输出 利好/利空/无 + 一句判断 + 证据
标题）；AI 不可用（未配 key / 超时 / 返回坏 JSON）降级为公告关键词口径：
GOOD→利好、ALERT→利空、WATCH 中 assess 明确写「偏利空」的（减持/解禁/
减值/诉讼等）→利空，其余 WATCH 不显示。

事件与判定按日缓存（log_dir/news_impact_state.json 原子写）：盘中 30 分钟
一轮的报告槽位不重复打接口，收盘定稿终刷一次收尾盘后公告，重启恢复当日
结果不重打。网络与 AI 全部隔离在 NewsImpactService 内，报告管线任何失败
只丢这一节、不伤主体。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

from fundamentals import (
    ANNOUNCEMENT_PAGE_SIZE,
    _ANNOUNCEMENT_REFERER,
    _clean_title,
    _get_json,
    _parse_date_safe,
    scan_announcements,
)
from fundamentals import Announcement  # noqa: F401  类型标注用

# 每只股票新闻条数上限（喂给 AI 的输入规模，不是展示上限）。
NEWS_LIMIT = 8

# 每只展示的证据条数上限。
EVIDENCE_LIMIT = 2

STATE_FILE_NAME = "news_impact_state.json"

_DIRECTION_POSITIVE = "利好"
_DIRECTION_NEGATIVE = "利空"
_DIRECTION_NONE = "无"

# 遍历公告列表页数上限：只看第 1 页（100 条，最新在前）足够覆盖两日窗口。
_ANNOUNCEMENT_PAGES = 1

_AI_SYSTEM_PROMPT = (
    "你是严谨的证券分析师，任务是判断持仓股近两日的事件对持股人是真利好还是真利空。\n"
    "规则：\n"
    "1. 只认对经营、业绩、股东权益有实质影响的事实（订单/中标/业绩预告/回购/增持/"
    "减持/解禁/处罚/立案/诉讼/重组/分红/政策落地等）；\n"
    "2. 以下一律判「无」：研报观点与评级、机构目标价、涨跌复盘、盘面描述、"
    "概念联想、转载软文、与该公司无直接关系的大盘或行业消息；\n"
    "3. 证据不足宁判「无」，不编造；同一只多空并存时 direction 取风险优先的一方，"
    "note 里说明并存；\n"
    "4. note 用一句中文说清影响，不带「可能上涨」之类的收益承诺。\n"
    "输出严格 JSON（不要 markdown 代码块、不要解释文字）：\n"
    '{"代码": {"direction": "利好|利空|无", "note": "一句话影响判断", '
    '"evidence": ["证据标题1", "证据标题2"]}}\n'
    "每个输入代码都必须出现在输出里；evidence 只能引用输入里给出的标题。"
)


# ── 纯计算（无 IO，可测） ──────────────────────────────────────────

def two_day_start(ref_date: str) -> str:
    """近两日窗口起点：ref_date 前一个自然日。"""
    ref = _parse_date_safe(str(ref_date))
    if ref is None:
        return str(ref_date)[:10]
    return (ref - timedelta(days=1)).strftime("%Y-%m-%d")


def date_in_two_day_window(date_str: str, ref_date: str) -> bool:
    """date_str（YYYY-MM-DD…）是否落在 [ref-1, ref] 两天内。"""
    day = _parse_date_safe(str(date_str))
    ref = _parse_date_safe(str(ref_date))
    if day is None or ref is None:
        return False
    return (ref - timedelta(days=1)) <= day <= ref


# 每只股票公告条数上限：两日的公告大多 <12 条，超出按接口顺序（最新在前）截断。
ANNOUNCEMENT_LIMIT = 12

# 降级口径里不看正文也判负的 WATCH 类 reason（assess 没正文时返回 None，
# 但减持/解禁/诉讼/减值的标题本身就是事实上的利空）。
_WATCH_NEGATIVE_REASONS = ("减持", "解禁", "诉讼", "仲裁", "减值")


def announcement_events(announcements: List[Announcement], ref_date: str) -> List[dict]:
    """近两日**全部**公告（标题级），供 AI 输入——「签订重大合同」这类没命中
    关键词的真利好不能整条丢掉，真伪交给 AI。关键词命中的条目额外带
    level/reason/assessment，降级口径只用这些字段。
    """
    ref = str(ref_date)[:10]
    keyword_by_key = {}
    for item in scan_announcements(announcements, ref, window_days=1):
        keyword_by_key[(str(item.date)[:10], item.title)] = item
    in_window = [
        a for a in announcements or []
        if date_in_two_day_window(a.date, ref)
    ]
    events: List[dict] = []
    for item in in_window[:ANNOUNCEMENT_LIMIT]:
        event = {"source": "公告", "date": str(item.date)[:10], "title": item.title}
        matched = keyword_by_key.pop((str(item.date)[:10], item.title), None)
        if matched is not None:
            event["level"] = matched.level.name
            event["reason"] = matched.reason
            event["assessment"] = matched.assessment
        events.append(event)
    # 截断圈之外的 keyword 命中仍然保留——降级口径不能因为条数上限漏掉风险公告。
    for matched in keyword_by_key.values():
        events.append({
            "source": "公告",
            "date": str(matched.date)[:10],
            "title": matched.title,
            "level": matched.level.name,
            "reason": matched.reason,
            "assessment": matched.assessment,
        })
    return events


def news_events_from_lines(lines: List[str], ref_date: str) -> List[dict]:
    """ai_analysis.fetch_news 的「YYYY-MM-DD HH:MM 标题」行 → 窗口内事件字典。"""
    events: List[dict] = []
    for line in lines or []:
        text = str(line).strip()
        if not text:
            continue
        day = text[:10]
        if not date_in_two_day_window(day, ref_date):
            continue
        title = text[16:].strip() if len(text) > 16 else text
        if title:
            events.append({"source": "新闻", "date": day, "title": title})
    return events


def fallback_direction(events: List[dict]) -> Optional[str]:
    """AI 不可用时的公告关键词口径。风险优先：利好与利空并存取利空。

    WATCH 级判负两条路：正文读到且 assess 写明「偏利空」（减持/解禁/减值等），
    或正文没读到但 reason 本身就是事实性利空（assess 无正文返回 None）。
    问询函/公告更正这类纯观察事项不显示。
    """
    has_negative = False
    has_positive = False
    for event in events or []:
        if event.get("source") != "公告":
            continue
        level = str(event.get("level") or "")
        assessment = str(event.get("assessment") or "")
        reason = str(event.get("reason") or "")
        if level == "ALERT":
            has_negative = True
        elif level == "WATCH" and (
            "利空" in assessment
            or any(word in reason for word in _WATCH_NEGATIVE_REASONS)
        ):
            has_negative = True
        elif level == "GOOD":
            has_positive = True
    if has_negative:
        return _DIRECTION_NEGATIVE
    if has_positive:
        return _DIRECTION_POSITIVE
    return None


def build_ai_messages(events_by_code: Dict[str, dict]) -> List[dict]:
    """逐只拼接公告/新闻事件，产出一次批量判定的 messages。"""
    blocks: List[str] = []
    for code in sorted(events_by_code):
        info = events_by_code[code] or {}
        lines = [f"{info.get('name') or code}（{code}）"]
        for event in info.get("events") or []:
            if event.get("source") == "公告":
                lines.append(
                    f"- [公告 {event.get('date')}] {event.get('title')}"
                    f"（关键词命中：{event.get('reason')}）"
                )
            else:
                lines.append(f"- [新闻 {event.get('date')}] {event.get('title')}")
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    user = (
        "以下是各持仓近两日的公告与新闻事件（新闻含噪声，按系统规则甄别）：\n\n"
        + "\n\n".join(blocks)
        + "\n\n逐只判定并输出 JSON。"
    )
    return [
        {"role": "system", "content": _AI_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_ai_json(raw: str, valid_codes: List[str]) -> Dict[str, dict]:
    """解析模型输出：剥掉可能的 markdown 围栏，只留 JSON；逐代码校验。

    坏 JSON 抛 ValueError（调用方降级）；单只字段异常按「无」处理，不整批作废。
    """
    text = str(raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI 输出中没有 JSON 对象")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("AI 输出不是 JSON 对象")
    result: Dict[str, dict] = {}
    for code in valid_codes:
        entry = data.get(code)
        direction = _DIRECTION_NONE
        note = ""
        evidence: List[str] = []
        if isinstance(entry, dict):
            raw_direction = str(entry.get("direction") or "").strip()
            if raw_direction in (_DIRECTION_POSITIVE, _DIRECTION_NEGATIVE, _DIRECTION_NONE):
                direction = raw_direction
            note = str(entry.get("note") or "").strip()
            raw_evidence = entry.get("evidence")
            if isinstance(raw_evidence, list):
                evidence = [str(item).strip() for item in raw_evidence if str(item).strip()]
        result[code] = {"direction": direction, "note": note, "evidence": evidence[:EVIDENCE_LIMIT]}
    return result


# ── 网络取数（模块级函数，测试可 monkeypatch） ──────────────────────

def fetch_announcements_page(code: str) -> List[Announcement]:
    """东财公告列表第 1 页（最新在前），标题级、不拉正文。"""
    plain = re.sub(r"\D", "", str(code))
    if not plain:
        return []
    url = (
        f"https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1"
        f"&page_size={ANNOUNCEMENT_PAGE_SIZE}&page_index={_ANNOUNCEMENT_PAGES}"
        f"&ann_type=A&client_source=web"
        f"&stock_list={plain}&f_node=0&s_node=0"
    )
    data = _get_json(url, referer=_ANNOUNCEMENT_REFERER, timeout=15)
    rows = (data.get("data") or {}).get("list") or []
    items: List[Announcement] = []
    for row in rows:
        date = (row.get("notice_date") or "")[:10]
        title = _clean_title(row.get("title", ""))
        if len(date) == 10 and title:
            items.append(Announcement(date=date, title=title))
    return items


def fetch_news_events(code: str, ref_date: str) -> List[dict]:
    """东财个股新闻标题（近两日窗口）。ai_analysis 惰性导入，避免报告管线背上重依赖。"""
    from ai_analysis import fetch_news

    return news_events_from_lines(fetch_news(code, limit=NEWS_LIMIT), ref_date)


def _default_chat_fn(messages: List[dict]) -> str:
    from ai_analysis import chat_completion, load_config

    config = load_config(".")
    return chat_completion(messages, config, temperature=0.2, max_tokens=6000, timeout=120.0)


# ── 服务：取数 + 判定 + 按日缓存 ────────────────────────────────────

class NewsImpactService:
    """近两日利好/利空判定服务。get_impacts 是唯一入口，失败不抛、返回降级结果。

    state 结构（log_dir/news_impact_state.json，原子写）：
      day / final_refreshed / events{code: [...]} / impacts[...] / ai_error
    """

    def __init__(
        self,
        log_dir: str,
        log: Optional[Callable[[str, str], Any]] = None,
        chat_fn: Optional[Callable[[List[dict]], str]] = None,
    ) -> None:
        self._state_path = os.path.join(log_dir, STATE_FILE_NAME)
        self._chat_fn = chat_fn or _default_chat_fn
        self._log = log or (lambda message, level="INFO": None)
        self._state = self._load_state()

    # ── 入口 ──

    def get_impacts(
        self,
        positions: List[dict],
        ref_date: Optional[str],
        final_refresh: bool = False,
    ) -> Optional[List[dict]]:
        """返回逐股 impact 列表（含「无」的条目，渲染层负责过滤）。

        None = 整节不可用（无持仓/无行情日），报告直接跳过该节。
        """
        if not positions or not ref_date:
            return None
        day = str(ref_date)[:10]
        wanted = {str(p.get("code") or ""): str(p.get("name") or p.get("code") or "")
                  for p in positions if p.get("code")}
        state = self._state
        cached_day = state.get("day")
        cached_codes = set(state.get("events") or {})
        need_refresh = (
            cached_day != day
            or cached_codes != set(wanted)
            or (final_refresh and not state.get("final_refreshed"))
        )
        if need_refresh:
            state = self._refresh(wanted, day, final_refresh)
            self._state = state
            self._save_state(state)
        impacts = state.get("impacts")
        return list(impacts) if isinstance(impacts, list) else None

    # ── 内部 ──

    def _refresh(self, wanted: Dict[str, str], day: str, final_refresh: bool) -> dict:
        events_by_code: Dict[str, dict] = {}
        previous = (self._state.get("events") or {}) if self._state.get("day") == day else {}
        for code, name in wanted.items():
            cached = previous.get(code)
            if cached is not None and not final_refresh:
                # 同日已取过（如盘中新增持仓）：增量复用，不重复打接口。
                events_by_code[code] = {"name": name, "events": cached}
                continue
            events: List[dict] = []
            try:
                events.extend(announcement_events(fetch_announcements_page(code), day))
            except Exception as error:
                self._log(f"近两日利好利空: {name} 公告取数失败: {error}", "WARNING")
                if cached:
                    events.extend(e for e in cached if e.get("source") == "公告")
            try:
                events.extend(fetch_news_events(code, day))
            except Exception as error:
                self._log(f"近两日利好利空: {name} 新闻取数失败: {error}", "WARNING")
            events_by_code[code] = {"name": name, "events": events}

        impacts, ai_error = self._judge(events_by_code)
        return {
            "day": day,
            "final_refreshed": final_refresh,
            "events": {code: info["events"] for code, info in events_by_code.items()},
            "impacts": impacts,
            "ai_error": ai_error,
        }

    def _judge(self, events_by_code: Dict[str, dict]) -> tuple:
        """AI 批量判定；失败降级为公告关键词口径。返回 (impacts, ai_error)。

        AI 判定成功时完全尊重其结论（含「无」）——否则关键词兜底会把 AI 明确
        判「无」的回购公告又翻成「利好」，两套口径打架。
        """
        impacts: List[dict] = []
        with_events = {
            code: info for code, info in events_by_code.items()
            if info.get("events")
        }
        classified: Dict[str, dict] = {}
        ai_error: Optional[str] = None
        if with_events:
            try:
                raw = self._chat_fn(build_ai_messages(with_events))
                classified = parse_ai_json(raw, list(with_events))
            except Exception as error:
                ai_error = str(error)
                self._log(f"近两日利好利空: AI 判定失败，降级公告口径: {error}", "WARNING")
        ai_ok = ai_error is None and bool(with_events)
        for code, info in sorted(events_by_code.items()):
            events = info.get("events") or []
            if ai_ok and code in with_events:
                entry = classified.get(code) or {}
                direction = str(entry.get("direction") or _DIRECTION_NONE)
                if direction in (_DIRECTION_POSITIVE, _DIRECTION_NEGATIVE):
                    evidence = self._match_evidence(events, entry.get("evidence") or [])
                    impacts.append({
                        "code": code, "name": info.get("name") or code,
                        "direction": direction,
                        "note": str(entry.get("note") or ""),
                        "evidence": evidence,
                        "ai": True,
                    })
                else:
                    impacts.append({
                        "code": code, "name": info.get("name") or code,
                        "direction": _DIRECTION_NONE, "note": "",
                        "evidence": [], "ai": True,
                    })
                continue
            direction = fallback_direction(events)
            if direction is not None:
                impacts.append({
                    "code": code, "name": info.get("name") or code,
                    "direction": direction,
                    "note": self._fallback_note(events, direction),
                    "evidence": [e for e in events if e.get("source") == "公告"][:EVIDENCE_LIMIT],
                    "ai": False,
                })
            else:
                impacts.append({
                    "code": code, "name": info.get("name") or code,
                    "direction": _DIRECTION_NONE, "note": "",
                    "evidence": [], "ai": False,
                })
        return impacts, ai_error

    @staticmethod
    def _match_evidence(events: List[dict], titles: List[str]) -> List[dict]:
        """把 AI 引用的标题对回真实事件（AI 可能微调措辞：按互相包含匹配）。

        一条都对不上时退回前几条真实事件——方向既然立住了，证据不能空着，
        也不能展示 AI 改写过的标题。
        """
        matched: List[dict] = []
        for title in titles[:EVIDENCE_LIMIT]:
            for event in events:
                event_title = str(event.get("title") or "")
                if title == event_title or title in event_title or event_title in title:
                    matched.append({
                        "source": event.get("source"),
                        "date": event.get("date"),
                        "title": event_title,
                    })
                    break
        if not matched:
            matched = [
                {
                    "source": event.get("source"),
                    "date": event.get("date"),
                    "title": str(event.get("title") or ""),
                }
                for event in events[:EVIDENCE_LIMIT]
            ]
        return matched

    @staticmethod
    def _fallback_note(events: List[dict], direction: str) -> str:
        assessments = [
            str(e.get("assessment") or "").strip()
            for e in events if e.get("source") == "公告" and e.get("assessment")
        ]
        if assessments:
            return assessments[0]
        reasons = [str(e.get("reason") or "") for e in events if e.get("reason")]
        return f"公告涉及{'、'.join(reasons[:2])}（关键词口径）" if reasons else f"公告口径判定为{direction}"

    # ── 状态 IO（原子写，参照 _save_tracker_states 模式） ──

    def _load_state(self) -> dict:
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict) -> None:
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(self._state_path) or ".",
                prefix=".news_impact.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._state_path)
        except Exception as error:
            self._log(f"近两日利好利空状态保存失败: {error}", "WARNING")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
