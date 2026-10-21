"""AI 个股/指数分析（DeepSeek，OpenAI 兼容 chat completions + function calling）。

两种用法：
1. ask(question) —— 自由文本问答：模型按需调用工具（程序内报告/接口 + 网络检索），
   结合严谨公开信息给出简洁精要的结论；
2. analyze(keyword) —— 一键结构化分析报告（技术面+基本面+持仓+走势量能+资讯+单日盘面+近期市场趋势）。

工具（function calling）覆盖：
- 程序内：技术面摘要、基本面报告、持仓报告、走势与量能、个股新闻、盘面总结缓存、近期市场趋势、实时报价
- 程序外：search_web（cn.bing.com 检索，标题/摘要/链接，标注来源）

配置（ai_config.json 或环境变量，环境变量优先）：
    {"api_key": "sk-...", "model": "deepseek-flash", "base_url": "https://api.deepseek.com"}
    DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_BASE_URL

数据缺口如实告诉模型「没取到」，不允许模型编造；网络摘要必须标注来源。
"""

from __future__ import annotations

import mini_racer_reuse  # noqa: F401  复用 V8 解码上下文，防 akshare 泄漏 Isolate

import html as html_lib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import requests
from market_timing import format_market_timing_data
from market_cycle_policy import MarketCycleInput, assess_market_cycle, format_market_cycle_assessment
from turnover_tracker import TurnoverTracker

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
CONFIG_FILE = "ai_config.json"
REPORT_FILE = "_ai_analysis.txt"
REPORT_MAX_CHARS = 12000

INDEX_ALIASES = {
    "上证指数": "sh000001",
    "上证": "sh000001",
    "大盘": "sh000001",
    "深证成指": "sz399001",
    "深成指": "sz399001",
    "创业板指": "sz399006",
    "创业板": "sz399006",
    "科创50": "sh000688",
}

SYSTEM_PROMPT = (
    "你是资深证券分析师。请基于用户提供的结构化数据与资讯，对标的做一份综合分析总结。\n"
    "要求：\n"
    "1. 只使用给定数据；事实与推断分开，证据与结论一一对应；数据缺失、可疑或数据矛盾时如实说明，"
    "绝不编造数字、新闻或事实；\n"
    "2. 多空判断只抓主要矛盾，用价格、均线、量能、估值、财务指标、持仓盈亏等关键数据支撑；"
    "证据不足时降低结论强度，不制造虚假精确性；\n"
    "3. 行业地位与护城河只能基于给出的主营业务构成、同业对比与新闻做条件式推断，并写明依据与不确定性；\n"
    "   单日盘面总结只代表一个交易日，必须结合最近几日大盘与板块表现判断市场环境；\n"
    "   - 兑现/资金回笼窗口（周五、月末、节前）只作为弱先验，必须结合成交额、北向和板块宽度，"
    "不得把日期本身写成主力必然兑现；\n"
    "4. 语气客观克制，不夸大，不作收益承诺；结论给出置信度，并指出会改变判断的关键变量。\n"
    "专业术语必须就地用通俗中文解释：M20（20日均线）、ATR（近期正常波动幅度）、"
    "量比（当前成交量相对常态）等，不只写缩写或公式；涉及做T价差时，把价差门槛换算成具体卖出价与接回价示例，"
    "并说明ATR不是保证收益的硬性阈值，还要考虑交易成本和滑点。\n"
    "输出用中文，默认不超过600字；用户明确要求详细分析时才放宽。按以下结构精简输出：\n"
    "【结论】1~2句，直接给出多空倾向和当前判断；\n"
    "【关键依据】3~5条，只保留最有区分度的事实或来源，并说明其含义；\n"
    "【操作建议】给出可执行的条件式建议：有持仓报告时区分已持有场景，无持仓时区分无持仓场景，"
    "说明当前建议、触发条件、失效条件、仓位/风险控制和复查节点；未给风险偏好时按审慎口径并注明前提；"
    "不要为了完整而强行给具体价位；深套持仓必须遵守项目深套策略，不得把无持仓入场失效位直接套用于已有持仓，"
    "也不建议在破位低位直接割肉；逻辑还在（逻辑未破坏）时优先给滚动做T方案：保留底仓，只用小仓在冲高减出，"
    "待回踩接回或重新站回支撑后接回，以降低持仓成本，执行前确认价差、量能和支撑，且不承诺回本；"
    "若再创新低取消接回或补仓计划；只有逻辑/基本面已破坏时，才等反弹到压力区分批离场；"
    "成本或浮亏未取到时，不判断是否深套，不给清仓建议；\n"
    "【风险与观察】列出1~3个主要风险及下一步观察指标。\n"
    "结尾固定附一行：本分析基于公开数据与模型生成，不构成投资建议。\n"
    "最高优先级约束：盘中买卖、补仓、减仓、清仓和做T只服从本地动作守卫；"
    "未收到明确的actionable状态、行情交易日期、15/30分钟新鲜证据和T+1可卖量时，"
    "不得直接下达交易动作，只能写观察、风险事实、停止执行条件或下一交易日复核。"
)


# ──────────────────────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────────────────────


def load_config(project_dir: str = ".") -> Dict[str, str]:
    """api_key / model / base_url：ai_config.json 为基础，环境变量优先。"""
    config = {
        "api_key": "",
        "model": DEFAULT_MODEL,
        "base_url": DEFAULT_BASE_URL,
    }
    path = os.path.join(project_dir, CONFIG_FILE)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            for key in ("api_key", "model", "base_url"):
                if data.get(key):
                    config[key] = str(data[key])
        except Exception:
            pass
    env_map = {
        "api_key": "DEEPSEEK_API_KEY",
        "model": "DEEPSEEK_MODEL",
        "base_url": "DEEPSEEK_BASE_URL",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value:
            config[key] = value
    return config


# ──────────────────────────────────────────────────────────────────────────────
# 上下文汇总
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class AnalysisContext:
    """喂给模型的全部素材；missing 里列没取到的部分，让模型如实交代。"""
    code: str
    name: str
    kind: str                      # stock / index / global
    price: Optional[float] = None
    technical_text: str = ""
    fundamentals_text: str = ""
    portfolio_text: str = ""
    trend_text: str = ""
    news_lines: List[str] = field(default_factory=list)
    market_text: str = ""
    recent_market_text: str = ""
    market_timing_text: str = ""
    capital_flow_text: str = ""
    missing: List[str] = field(default_factory=list)


def kind_of(code: str) -> str:
    code = code.strip().lower()
    if re.match(r"^(sh|sz)000\d{3}$", code) or re.match(r"^sz399\d{3}$", code):
        return "index"
    if re.match(r"^(sh|sz|bj)\d{6}$", code):
        return "stock"
    return "global"


def resolve_input(keyword: str, search_fn=None) -> Optional[tuple]:
    """把输入解析成 (code, name)。支持指数别名、A股代码/搜索、港美股搜索。"""
    text = keyword.strip()
    if not text:
        return None
    if text.lower() in INDEX_ALIASES:
        return INDEX_ALIASES[text.lower()], text
    if text in INDEX_ALIASES:
        return INDEX_ALIASES[text], text
    if re.match(r"^(sh|sz|bj)\d{6}$", text.lower()):
        return text.lower(), text
    if search_fn:
        try:
            results = search_fn(text)
        except Exception:
            results = None
        if results:
            first = results[0]
            return first.get("code") or first.get("symbol"), first.get("name") or text
    # 指数别名模糊匹配（“上证指数”等已精确命中，这里兜底包含匹配）
    for alias, code in INDEX_ALIASES.items():
        if alias in text:
            return code, alias
    return None


def _series(frame, names):
    for name in names:
        if name in frame.columns:
            return frame[name]
    return None


def trend_overview(history: Any) -> str:
    """走势与量能概览：多周期涨幅、区间位置、量能变化。"""
    close = _series(history, ["close", "收盘", "收盘价"])
    high = _series(history, ["high", "最高", "最高价"])
    low = _series(history, ["low", "最低", "最低价"])
    volume = _series(history, ["volume", "成交量"])
    if close is None or len(close) < 60:
        return ""
    closes = [float(v) for v in close.tolist()]
    last = closes[-1]

    def _change(days: int) -> Optional[float]:
        if len(closes) <= days:
            return None
        base = closes[-1 - days]
        return (last / base - 1.0) * 100.0 if base else None

    window = closes[-60:]
    if high is not None and low is not None:
        highs = [float(v) for v in high.tolist()[-60:]]
        lows = [float(v) for v in low.tolist()[-60:]]
        span = max(highs) - min(lows)
        position = (last - min(lows)) / span * 100.0 if span > 0 else 50.0
        range_text = f"60日区间 {min(lows):.2f} ~ {max(highs):.2f}，现价处于 {position:.0f}% 分位"
    else:
        range_text = "60日区间数据不足"
    parts = []
    for days, label in ((5, "5日"), (20, "20日"), (60, "60日")):
        change = _change(days)
        if change is not None:
            parts.append(f"{label} {change:+.1f}%")
    volume_text = ""
    if volume is not None and len(volume) >= 20:
        vols = [float(v) for v in volume.tolist()]
        avg5 = sum(vols[-5:]) / 5.0
        avg20 = sum(vols[-20:]) / 20.0
        if avg20 > 0:
            ratio = avg5 / avg20
            trend_word = "放量" if ratio >= 1.2 else ("缩量" if ratio <= 0.8 else "平量")
            volume_text = f"近5日均量/20日均量 = {ratio:.2f}（{trend_word}）"
    lines = [f"涨跌幅：{'｜'.join(parts) if parts else '—'}", range_text]
    if volume_text:
        lines.append(volume_text)
    return "\n".join(lines)


def fetch_news(code: str, limit: int = 8) -> List[str]:
    """东财个股新闻标题（最近 limit 条）；失败返回空列表。"""
    try:
        import akshare as ak
        plain = re.sub(r"\D", "", code)
        df = ak.stock_news_em(symbol=plain)
    except Exception:
        return []
    lines: List[str] = []
    if df is None or len(df) == 0:
        return lines
    for _, row in df.head(limit).iterrows():
        title = str(row.get("新闻标题", "") or "").strip()
        when = str(row.get("发布时间", "") or "")[:16]
        if title:
            lines.append(f"{when} {title}".strip())
    return lines


def fetch_history(kind: str, code: str) -> Any:
    """取日线：A股/指数走 akshare，港美股走 global_fundamentals。"""
    import akshare as ak

    if kind == "index":
        return ak.stock_zh_index_daily(symbol=code)
    if kind == "stock":
        return ak.stock_zh_a_daily(
            symbol=code,
            start_date=(datetime.now() - timedelta(days=250)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
            adjust="qfq",
        )
    raise ValueError(f"fetch_history 不支持 {kind}")


def _load_cached_report(project_dir: str, filename: str, max_chars: int = REPORT_MAX_CHARS) -> str:
    path = os.path.join(project_dir, filename)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
        return text[:max_chars]
    except Exception:
        return ""


def load_market_context(project_dir: str) -> str:
    """盘面总结缓存（_market_summary.txt），作为单日市场环境素材。"""
    return _load_cached_report(project_dir, "_market_summary.txt", max_chars=REPORT_MAX_CHARS)


def load_portfolio_context(project_dir: str) -> str:
    """持仓日报缓存（_portfolio_daily_report.txt），作为组合风险素材。"""
    return _load_cached_report(project_dir, "_portfolio_daily_report.txt")


def load_capital_flow_context(
    project_dir: str,
    code: Optional[str] = None,
    now: Optional[datetime] = None,
    max_age_seconds: int = 150,
) -> str:
    """读取当前交易日、未过期的盘中资金流辅助证据。"""
    path = os.path.join(project_dir, "_capital_flow_cache.json")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle) or {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ""

    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")

    def fresh(item):
        if not isinstance(item, dict) or item.get("quote_date") != today:
            return False
        try:
            as_of = datetime.fromisoformat(str(item.get("as_of") or ""))
            net = float(item.get("net_yi"))
        except (TypeError, ValueError):
            return False
        age = (now - as_of).total_seconds()
        return 0 <= age <= max_age_seconds and net == net

    def snapshot_line(prefix, item):
        net = float(item["net_yi"])
        direction = "净流入" if net > 0 else "净流出" if net < 0 else "净流向中性"
        parts = [f"{prefix}：平台口径主力{direction} {net:+.1f}亿"]
        ratio = item.get("net_ratio_pct")
        if ratio is not None:
            parts.append(f"主力净占比 {float(ratio):+.2f}%")
        parts.append(f"数据时间 {str(item.get('as_of')).replace('T', ' ')[:16]}")
        parts.append(f"来源 {item.get('source') or '未标明'}")
        return "｜".join(parts)

    stocks = payload.get("stocks") or {}
    sectors = payload.get("sectors") or {}
    stock_sectors = payload.get("stock_sectors") or {}
    if code:
        stock_items = [(code, stocks.get(code))]
        sector_items = [
            (name, sectors.get(name))
            for name in (stock_sectors.get(code) or [])
        ]
    else:
        stock_items = list(stocks.items())
        sector_items = list(sectors.items())

    lines = []
    for key, item in stock_items:
        if fresh(item):
            lines.append(snapshot_line(f"个股 {item.get('name') or key}", item))
    for key, item in sector_items:
        if fresh(item):
            lines.append(snapshot_line(f"板块 {item.get('name') or key}", item))
    if not lines:
        return ""
    return "\n".join([
        "平台估算口径：按成交单大小分类，不等同于真实机构账户交易；资金流仅作价格、量能和板块趋势的辅助确认。",
        *lines,
    ])


def _format_recent_index(history: Any, label: str, days: int = 5) -> str:
    close = _series(history, ["close", "收盘", "收盘价"])
    if close is None or len(close) < 2:
        return ""
    values = [float(value) for value in close.tolist()]
    actual_days = min(days, len(values) - 1)
    start = len(values) - actual_days - 1
    dates = _series(history, ["date", "日期", "交易日期"])
    changes = []
    for index in range(start + 1, len(values)):
        previous = values[index - 1]
        if not previous:
            continue
        date_text = str(dates.iloc[index])[:10] if dates is not None else f"第{index - start}日"
        changes.append(f"{date_text} {(values[index] / previous - 1) * 100:+.2f}%")
    cumulative = (values[-1] / values[start] - 1) * 100 if values[start] else 0.0
    return f"{label}近{actual_days}个交易日：{'｜'.join(changes)}｜区间累计 {cumulative:+.2f}%"


def fetch_sector_history(sector: str, lookback_days: int = 12) -> Any:
    """读取行业板块日线；失败由上层降级到板块技术缓存。"""
    import akshare as ak

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
    return ak.stock_board_industry_hist_em(
        symbol=sector,
        start_date=start_date,
        end_date=end_date,
        period="日",
        adjust="",
    )


def fetch_northbound_history() -> Any:
    """读取北向资金历史；接口不可用时由上层明确标记未取到。"""
    import akshare as ak

    return ak.stock_hsgt_hist_em("北向资金")


def _read_turnover_history(project_dir: str, quote_date: object = None):
    path = os.path.join(project_dir, "_turnover_tracker.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []
    excluded_date = _parse_date_text(quote_date)
    rows = []
    for day, slots in (data or {}).items():
        normalized_day = _parse_date_text(day)
        if normalized_day is None or normalized_day == excluded_date:
            continue
        values = []
        for slot, value in (slots or {}).items():
            try:
                slot_number = int(slot)
            except (TypeError, ValueError):
                continue
            if slot_number not in (-1, 240):
                continue
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
        if values:
            rows.append((normalized_day, max(values)))
    return sorted(rows)[-5:]


def _full_day_turnover(slots):
    values = []
    for slot, value in (slots or {}).items():
        try:
            slot_number = int(slot)
            number = float(value)
        except (TypeError, ValueError):
            continue
        if slot_number in (-1, 240) and number > 0:
            values.append(number)
    return max(values) if values else None


def _parse_date_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()[:10].replace("/", "-")
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _numeric_yi(value: Any) -> Optional[float]:
    try:
        text = str(value).replace(",", "").strip()
        if not text or text.lower() in {"nan", "none", "--"}:
            return None
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number / 1e8 if abs(number) > 10000 else number


def _read_northbound_latest(frame: Any):
    """按日期取北向最新行，并返回当天、3日、5日净流向（亿元）。"""
    if frame is None or not hasattr(frame, "columns") or len(frame) == 0:
        return None
    candidates = (
        "当日资金净流入", "当日净流入", "净流入", "当日成交净买额",
    )
    column = next((name for name in candidates if name in frame.columns), None)
    date_column = next(
        (name for name in ("日期", "交易日期", "Date", "date") if name in frame.columns),
        None,
    )
    if column is None or date_column is None:
        return None

    rows = []
    for _, row in frame.iterrows():
        date_text = _parse_date_text(row.get(date_column))
        value = _numeric_yi(row.get(column))
        if date_text is not None and value is not None:
            rows.append((date_text, value))
    if not rows:
        return None
    rows.sort(key=lambda item: item[0])
    latest_date, latest_net = rows[-1]
    return {
        "date": latest_date,
        "net": latest_net,
        "sum_3d": sum(value for _, value in rows[-3:]),
        "sum_5d": sum(value for _, value in rows[-5:]),
        "source": "AkShare.stock_hsgt_hist_em",
    }


def _read_turnover_evidence(
    project_dir: str,
    quote_date: str,
    now: Optional[datetime] = None,
):
    """从本地分时采样中生成同口径成交额证据。

    盘中只比较前一交易日同期；收盘后才比较前一交易日全天，避免把
    当日累计成交额与前日全天成交额混在一起。
    """
    path = os.path.join(project_dir, "_turnover_tracker.json")
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle) or {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}

    def slots_for(day):
        result = {}
        for key, value in (raw.get(day) or {}).items():
            try:
                result[int(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return result

    today_slots = slots_for(quote_date)
    if not today_slots:
        return {}
    previous_dates = sorted(day for day in raw if day < quote_date and slots_for(day))
    if not previous_dates:
        return {
            "today": max(today_slots.values()),
            "previous": None,
            "history": _read_turnover_history(project_dir, quote_date),
            "comparison_label": "较前一交易日",
            "session": "未取到前一交易日数据",
            "source": "_turnover_tracker.json",
            "scope": "沪深两市",
        }

    previous_slots = slots_for(previous_dates[-1])
    now = now or datetime.now()
    slot = TurnoverTracker.slot_minute(now)
    after_close = slot == 240 or (
        slot is None and (now.hour * 60 + now.minute) > 900
    )
    if after_close:
        today_value = max(today_slots.values())
        previous_value = _full_day_turnover(previous_slots)
        return {
            "today": today_value,
            "previous": previous_value,
            "history": _read_turnover_history(project_dir, quote_date),
            "comparison_label": "较前一交易日全天",
            "session": "收盘后",
            "source": "_turnover_tracker.json",
            "scope": "沪深两市",
        }

    if slot is None or slot < 0:
        return {
            "today": max(today_slots.values()),
            "previous": None,
            "history": _read_turnover_history(project_dir, quote_date),
            "comparison_label": "较前一交易日同期",
            "session": "非交易时段，未比较",
            "source": "_turnover_tracker.json",
            "scope": "沪深两市",
        }

    current_slots = [key for key in today_slots if key <= slot]
    previous_same_slots = [key for key in previous_slots if key <= slot]
    if not current_slots or not previous_same_slots:
        return {
            "today": max(today_slots.values()),
            "previous": None,
            "history": _read_turnover_history(project_dir, quote_date),
            "comparison_label": "较前一交易日同期",
            "session": f"盘中截至{TurnoverTracker.slot_text(slot)}",
            "source": "_turnover_tracker.json",
            "scope": "沪深两市",
        }
    current_slot = max(current_slots)
    previous_slot = max(previous_same_slots)
    return {
        "today": today_slots[current_slot],
        "previous": previous_slots[previous_slot],
        "history": _read_turnover_history(project_dir, quote_date),
        "comparison_label": "较前一交易日同期",
        "session": f"盘中截至{TurnoverTracker.slot_text(current_slot)}",
        "source": "_turnover_tracker.json",
        "scope": "沪深两市",
    }


def load_market_timing_context(project_dir: str = ".") -> str:
    """读取交给 AI 判断的时点、成交额、北向和板块宽度证据。"""
    index_today = None
    index_recent = None
    quote_date = None
    try:
        history = fetch_history("index", "sh000001")
        close = _series(history, ["close", "收盘", "收盘价"])
        dates = _series(history, ["date", "日期", "交易日期"])
        if close is not None and len(close) >= 2:
            values = [float(value) for value in close.tolist()]
            index_today = (values[-1] / values[-2] - 1) * 100 if values[-2] else None
            if len(values) >= 6 and values[-6]:
                index_recent = (values[-1] / values[-6] - 1) * 100
            if dates is not None:
                quote_date = str(dates.iloc[-1])[:10]
    except Exception:
        pass

    turnover = _read_turnover_evidence(project_dir, quote_date or "")

    northbound = None
    try:
        northbound = _read_northbound_latest(fetch_northbound_history())
    except Exception:
        pass
    northbound_matches_quote = bool(
        northbound and quote_date and northbound.get("date") == quote_date
    )

    breadth = None
    sector_cache_date = None
    cache_path = os.path.join(project_dir, "_sector_tech_cache.json")
    try:
        with open(cache_path, encoding="utf-8") as handle:
            cache = json.load(handle) or {}
        sector_cache_date = _parse_date_text(cache.get("date"))
        if quote_date and sector_cache_date == quote_date:
            techs = cache.get("techs") or {}
            changes = [
                float((item or {}).get("change_pct"))
                for item in techs.values()
                if (item or {}).get("change_pct") is not None
            ]
            if changes:
                breadth = sum(value > 0 for value in changes) / len(changes)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass

    turnover_history = turnover.get("history", [])
    cycle = assess_market_cycle(MarketCycleInput(
        predicted_turnover_yi=turnover.get("today"),
        turnover_history_yi=[value for _, value in turnover_history],
        turnover_history_dates=[day for day, _ in turnover_history],
        turnover_history_as_of_date=quote_date,
        index_today_pct=index_today,
        index_recent_pct=index_recent,
        industry_breadth=breadth,
    ))
    return format_market_timing_data(
        quote_date or "未取到",
        index_today_pct=index_today,
        index_recent_pct=index_recent,
        turnover_today=turnover.get("today"),
        turnover_previous=turnover.get("previous"),
        turnover_history=turnover.get("history"),
        turnover_comparison_label=turnover.get("comparison_label", "较前一交易日"),
        turnover_session=turnover.get("session"),
        turnover_source=turnover.get("source"),
        turnover_scope=turnover.get("scope"),
        sector_breadth=breadth,
        sector_breadth_label="持仓相关板块上涨占比",
        sector_breadth_date=sector_cache_date,
        sector_breadth_source="_sector_tech_cache.json",
        northbound_net=northbound.get("net") if northbound_matches_quote else None,
        northbound_date=(northbound or {}).get("date"),
        northbound_3d=(northbound or {}).get("sum_3d") if northbound_matches_quote else None,
        northbound_5d=(northbound or {}).get("sum_5d") if northbound_matches_quote else None,
        northbound_source=(northbound or {}).get("source"),
        market_cycle_text=format_market_cycle_assessment(cycle),
        holiday_eve=None,
        month_end_near=None,
    )


def load_recent_market_context(project_dir: str = ".") -> str:
    """组合近几日指数与行业表现，避免把单日盘面当成趋势。"""
    lines = []
    latest_index_date = None
    for code, label in (
        ("sh000001", "沪指"),
        ("sz399001", "深成指"),
        ("sz399006", "创业板指"),
    ):
        try:
            history = fetch_history("index", code)
            line = _format_recent_index(history, label)
            if latest_index_date is None:
                dates = _series(history, ["date", "日期", "交易日期"])
                if dates is not None and len(dates):
                    latest_index_date = _parse_date_text(dates.iloc[-1])
        except Exception:
            line = ""
        if line:
            lines.append(line)

    cache_path = os.path.join(project_dir, "_sector_tech_cache.json")
    try:
        with open(cache_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        data = {}
    data_date = _parse_date_text((data or {}).get("date")) if data else None
    if data and data_date and data_date == latest_index_date:
        try:
            techs = data.get("techs") or {}
            ranked = sorted(
                techs.items(),
                key=lambda item: float((item[1] or {}).get("relative_strength") or 0),
                reverse=True,
            )
            selected = ranked[:2] + ranked[-2:] if len(ranked) > 2 else ranked
            seen = set()
            for sector, raw in selected:
                if sector in seen:
                    continue
                seen.add(sector)
                raw = raw or {}
                today = raw.get("change_pct")
                relative = raw.get("relative_strength")
                today_text = f"今日 {float(today):+.2f}%" if today is not None else "今日涨跌未取到"
                relative_text = (
                    f"相对沪指20日 {float(relative):+.2f}pp"
                    if relative is not None else "相对沪指20日未取到"
                )
                position = "站上M20" if raw.get("above_ma20") else "位于M20下方"
                state = "转弱" if raw.get("trend_weak") else "未转弱"
                try:
                    sector_line = _format_recent_index(
                        fetch_sector_history(sector), f"板块{sector}",
                    )
                except Exception:
                    sector_line = ""
                lines.append(
                    f"{sector_line}｜{relative_text}｜{position}｜{state}"
                    if sector_line else
                    f"板块 {sector}：{today_text}｜{relative_text}｜{position}｜{state}"
                )
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    elif data_date:
        lines.append(
            f"板块技术缓存日期：{data_date}，与指数最新行情日期 "
            f"{latest_index_date or '未取到'} 不一致，板块缓存不纳入判断"
        )

    if data_date and data_date == latest_index_date:
        lines.insert(0, f"板块技术缓存日期：{data_date}｜范围：持仓相关板块")

    return "\n".join(lines)


def gather_context(
    code: str,
    name: str,
    project_dir: str = ".",
    search_fn=None,
) -> AnalysisContext:
    """把技术面/基本面/持仓/走势/新闻/盘面汇总成一个上下文。"""
    kind = kind_of(code)
    context = AnalysisContext(code=code, name=name, kind=kind)
    context.market_text = load_market_context(project_dir)
    context.portfolio_text = load_portfolio_context(project_dir)
    context.recent_market_text = load_recent_market_context(project_dir)
    context.market_timing_text = load_market_timing_context(project_dir)
    context.capital_flow_text = load_capital_flow_context(project_dir, code)
    if not context.portfolio_text:
        context.missing.append("持仓报告缓存未取到")
    if not context.recent_market_text:
        context.missing.append("最近几日大盘与板块表现未取到")
    if not context.market_timing_text:
        context.missing.append("时点与资金证据未取到")
    if not context.capital_flow_text:
        context.missing.append("盘中资金流辅助证据未取到或已过期")

    if kind == "global":
        try:
            from global_fundamentals import fetch_and_format
            data, report = fetch_and_format(name if name != code else code)
            context.fundamentals_text = report
            context.price = data.price
        except Exception as error:
            context.missing.append(f"港美股基本面/技术面未取到（{error}）")
        return context

    try:
        history = fetch_history(kind, code)
        context.trend_text = trend_overview(history)
        close = _series(history, ["close", "收盘", "收盘价"])
        if close is not None and len(close) > 0:
            context.price = float(close.iloc[-1])
        try:
            from stock_analysis import analyze_history
            from technical_summary import TechnicalSummary, format_summary
            result = analyze_history(history, code, name)
            context.technical_text = format_summary(TechnicalSummary.from_result(result))
        except Exception as error:
            context.missing.append(f"技术面摘要未取到（{error}）")
    except Exception as error:
        context.missing.append(f"日线数据未取到（{error}）")

    if kind == "stock":
        try:
            from fundamentals import fetch_and_analyze
            _result, report = fetch_and_analyze(code, name)
            context.fundamentals_text = report
        except Exception as error:
            context.missing.append(f"基本面报告未取到（{error}）")
        context.news_lines = fetch_news(code)
        if not context.news_lines:
            context.missing.append("个股新闻未取到")
    return context


# ──────────────────────────────────────────────────────────────────────────────
# 网络检索（cn.bing.com，免费无 key；国内可达）
# ──────────────────────────────────────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _strip_html(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# A股名称常带省市前缀（贵州茅台/上海机场…），搜索引擎容易被前缀带偏，
# 检索时自动剥掉前缀补一次查询，合并两路结果。
_REGION_PREFIX = re.compile(
    r"^(贵州|云南|四川|湖南|湖北|河南|河北|山东|山西|陕西|福建|安徽|江西|江苏|浙江|"
    r"广东|广西|海南|辽宁|吉林|黑龙江|甘肃|青海|新疆|西藏|宁夏|内蒙古|"
    r"北京|上海|天津|重庆|中国|深圳|杭州|南京|武汉|成都|广州)"
)


def _strip_region_prefix(name: str) -> str:
    return _REGION_PREFIX.sub("", name.strip())


def parse_bing_results(html: str, max_results: int = 5) -> List[Dict[str, str]]:
    """cn.bing.com 结果页 → [{title, snippet, url}]（解析失败返回部分/空，不抛异常）。

    结果块是 <li class="b_algo">…</li>，内部可能嵌套 li，不能用 </li> 收尾匹配；
    改按 b_algo 起点切块，标题形如 <h2 class=""><a href=…>标题</a></h2>。
    """
    items: List[Dict[str, str]] = []
    starts = [m.start() for m in re.finditer(r'<li[^>]+class="[^"]*b_algo[^"]*"', html)]
    starts.append(len(html))
    for index in range(len(starts) - 1):
        block = html[starts[index]:starts[index + 1]]
        head = re.search(
            r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.DOTALL
        )
        if not head:
            continue
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.DOTALL)
        items.append({
            "title": _strip_html(head.group(2)),
            "snippet": _strip_html(snippet_match.group(1)) if snippet_match else "",
            "url": head.group(1),
        })
        if len(items) >= max_results:
            break
    return items


def search_web(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """联网检索公开网页（标题/摘要/链接）；失败返回空列表。

    「贵州茅台 批价」这类带省市前缀的词容易被搜索引擎带偏（返回贵州旅游），
    所以剥前缀再补一次查询；剥前缀的结果更对题，排在前面，再按 url 去重合并。
    """
    original = query.strip()
    stripped = _strip_region_prefix(original)
    queries: List[str] = []
    if stripped and stripped != original:
        queries.append(stripped)   # 更对题的优先
    queries.append(original)

    merged: List[Dict[str, str]] = []
    seen_urls = set()
    for q in queries:
        url = f"https://cn.bing.com/search?q={quote(q)}&setlang=zh-CN"
        try:
            response = requests.get(url, headers={"User-Agent": _UA}, timeout=12)
            response.raise_for_status()
            html = response.content.decode("utf-8", errors="replace")
        except Exception:
            continue
        for item in parse_bing_results(html, max_results=max_results):
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            merged.append(item)
            if len(merged) >= max_results:
                return merged
    return merged


# ──────────────────────────────────────────────────────────────────────────────
# 项目自身文档检索（策略/指标/功能的「内部知识库」，回答程序自身的问题用它而不是上网）
# ──────────────────────────────────────────────────────────────────────────────


def _project_doc_files(project_dir: str) -> List[str]:
    """项目里的 .md / .py（文档 + 源码注释就是程序的说明书）。"""
    files: List[str] = []
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "stock"}
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in names:
            if name.endswith((".md", ".py")):
                path = os.path.join(root, name)
                try:
                    if os.path.getsize(path) < 2_000_000:
                        files.append(path)
                except OSError:
                    pass
    return files


def search_project_docs(
    query: str,
    project_dir: str = ".",
    max_hits: int = 6,
    context: int = 2,
) -> str:
    """按查询词检索本项目的文档与源码说明，返回带出处的摘录。

    中文按二元组（bigram）打分：一行命中的查询片段越多越靠前；
    没有分词器也能稳定匹配「513选股策略」这类内部术语。
    """
    text_query = (query or "").strip()
    if len(text_query) < 2:
        return "查询词太短，请给出策略名/指标名/功能名等关键词"
    grams = {text_query[i:i + 2] for i in range(len(text_query) - 1)}
    scored: List[tuple] = []
    for path in _project_doc_files(project_dir):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except Exception:
            continue
        # 文件级权重：文件名与查询的片段重合度（策略文档名 ≈ 策略名，如
        # 「513选股策略」↔「513低位选股策略.md」）+ docs/ 下文档优先，
        # 避免「自述/更新日志里的提及」盖过真正的策略文档
        base_name = os.path.basename(path).replace(" ", "")
        file_bonus = 2 * sum(1 for gram in grams if gram in base_name)
        if text_query.replace(" ", "") in base_name:
            file_bonus += 8
        if f"{os.sep}docs{os.sep}" in path or path.endswith(".md"):
            file_bonus += 3
        for index, line in enumerate(lines):
            # 命中与否只看行内内容；文件权重/标题权重只用于排序，
            # 否则 docs/ 加分会把零命中的行也带进来
            content_score = sum(1 for gram in grams if gram in line)
            if text_query in line:
                content_score += 5
            if content_score < 2:
                continue
            rank = content_score + file_bonus
            if line.lstrip().startswith("#"):
                rank += 2
            scored.append((rank, path, index, lines))
    if not scored:
        return f"项目文档与源码里没有找到与「{text_query}」相关的内容；若是市场/公司信息请用 search_web"
    scored.sort(key=lambda item: -item[0])

    excerpts: List[str] = []
    used = set()
    for score, path, index, lines in scored:
        key = (path, index)
        if key in used:
            continue
        try:
            rel = os.path.relpath(path, project_dir)
        except ValueError:
            rel = path
        start = max(0, index - context)
        end = min(len(lines), index + context + 1)
        for offset in range(start, end):
            used.add((path, offset))
        body = "\n".join(f"    {lines[offset].rstrip()}" for offset in range(start, end))
        excerpts.append(f"[{rel}:{index + 1}]\n{body}")
        if len(excerpts) >= max_hits:
            break
    return "项目内资料检索「{}」命中：\n{}".format(text_query, "\n\n".join(excerpts))


# ──────────────────────────────────────────────────────────────────────────────
# 工具层（DeepSeek function calling 可调用的接口）
# ──────────────────────────────────────────────────────────────────────────────


def _normalize_code(raw: str, search_fn: Optional[Callable] = None) -> Optional[tuple]:
    """工具参数里的代码/名称 → (code, name)。"""
    text = (raw or "").strip()
    if not text:
        return None
    if re.match(r"^\d{6}$", text):
        text = ("sh" if text[0] == "6" else "sz") + text
    return resolve_input(text, search_fn=search_fn)


TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_project_docs",
            "description": "检索本程序自己的文档与源码说明（选股策略规则、指标口径、功能用法、参数含义、更新记录）。回答本程序相关的策略/指标/功能问题时，先用本工具取得项目事实作为回答基础；也可用于确认公司代码等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "策略名/指标名/功能名等关键词，如「513」「量能基线」「基本面评分」"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "联网检索公开网页，返回标题/摘要/链接。只在需要行业背景、近期事件、公司外部信息或验证时效性事实时调用；纯定义、程序规则或已有本地数据足够时不要调用。摘要仅供参考，重要结论需标注来源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词，建议含公司全称或行业关键词"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_quote",
            "description": "获取标的现价与 5/20/60 日涨跌幅（A股/指数/港美股），参数 code 如 sh600519、sh000001、NVDA、00700",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "代码或名称"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_technical_analysis",
            "description": "仅用于用户明确询问某个股票或指数的技术面、趋势、量价、支撑压力时；不要为了回答行业或泛概念问题而调用",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "代码或名称"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals_report",
            "description": "仅用于用户明确询问个股财务、估值、主营构成、治理或基本面时；行业泛问不调用。A股用 sh600519 形式；港美股用代码或名称；生成较慢（约10~30秒）",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "代码或名称"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trend_volume",
            "description": "仅用于用户询问具体股票或指数的走势、量能、区间位置时；不要用于行业泛问或一般概念解释",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "代码或名称"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_news",
            "description": "仅用于用户询问具体个股的近期新闻或事件时；行业新闻可使用 search_web，不需要调用个股新闻工具",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "代码或名称"},
                    "limit": {"type": "integer", "description": "条数，默认 8"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_summary",
            "description": "用于大盘、市场情绪或行业/板块整体动向问题；只问板块时优先使用本工具，不要连带调用个股技术面或基本面",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_report",
            "description": "仅用于用户询问组合、持仓、盈亏归因或逐股处置时；没有持仓问题就不要调用",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_analysis_bundle",
            "description": "仅用于用户明确要求对某个个股或指数做综合分析时，一次读取技术面、基本面、持仓、市场和板块背景；普通问答、纯板块问题或只问单一维度时改用更窄的工具",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "代码或名称"}},
                "required": ["code"],
            },
        },
    },
]


def dispatch(
    name: str,
    arguments: Dict[str, Any],
    project_dir: str = ".",
    search_fn: Optional[Callable] = None,
    collector: Optional[Dict[str, Any]] = None,
) -> str:
    """执行一个工具调用，返回给模型的文本；任何失败都返回错误说明而不是抛出。"""
    try:
        return _dispatch(name, arguments, project_dir, search_fn, collector)
    except Exception as error:
        return f"工具 {name} 执行失败：{error}"


def _dispatch(
    name: str,
    arguments: Dict[str, Any],
    project_dir: str,
    search_fn: Optional[Callable],
    collector: Optional[Dict[str, Any]],
) -> str:
    known = {tool["function"]["name"] for tool in TOOLS_SCHEMA}
    if name not in known:
        return f"未知工具 {name}"

    if name == "search_project_docs":
        return search_project_docs(
            str(arguments.get("query") or ""),
            project_dir=project_dir,
        )

    if name == "search_web":
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "检索词为空"
        results = search_web(query)
        if collector is not None:
            for item in results:
                collector.setdefault("web_sources", []).append(item["url"])
        if not results:
            return f"网络检索「{query}」没有取到结果，请换关键词或在回答中说明信息未取到"
        lines = [f"网络检索「{query}」结果（摘要非原文，重要事实请标注来源）："]
        for item in results:
            lines.append(f"- {item['title']}｜{item['url']}\n  {item['snippet']}")
        return "\n".join(lines)

    if name == "get_market_summary":
        text = load_market_context(project_dir)
        return text or "盘面总结缓存未取到"

    if name == "get_portfolio_report":
        text = load_portfolio_context(project_dir)
        return text or "持仓报告缓存未取到"

    code_arg = str(arguments.get("code") or "").strip()
    resolved = _normalize_code(code_arg, search_fn=search_fn)
    if resolved is None:
        return f"没有识别出「{code_arg}」对应的标的，请给出代码（如 sh600519）或名称"
    code, target_name = resolved
    kind = kind_of(code)

    if name == "get_analysis_bundle":
        context = gather_context(
            code, target_name, project_dir=project_dir, search_fn=search_fn,
        )
        return build_context_materials(context)

    if name == "get_quote":
        if kind == "global":
            from global_fundamentals import GlobalFundamentalsClient, GlobalSecurity
            history = GlobalFundamentalsClient().fetch_daily_bars(GlobalSecurity(code, code, "", "USD"))
        else:
            history = fetch_history(kind, code)
        trend = trend_overview(history)
        close = _series(history, ["close", "收盘", "收盘价"])
        price = float(close.iloc[-1]) if close is not None and len(close) > 0 else None
        price_line = f"现价 {price:.2f}" if price is not None else "现价未取到"
        return f"{target_name}（{code}）{price_line}\n{trend}"

    if name == "get_trend_volume":
        history = fetch_history(kind, code)
        return trend_overview(history) or "走势数据不足"

    if name == "get_technical_analysis":
        history = fetch_history(kind, code)
        from stock_analysis import analyze_history
        from technical_summary import TechnicalSummary, format_summary
        result = analyze_history(history, code, target_name)
        return format_summary(TechnicalSummary.from_result(result))

    if name == "get_fundamentals_report":
        if kind == "index":
            return "指数没有个股基本面报告；可用 get_market_summary 看市场环境"
        if kind == "global":
            from global_fundamentals import fetch_and_format
            _data, report = fetch_and_format(target_name if target_name != code else code)
            return report
        from fundamentals import fetch_and_analyze
        _result, report = fetch_and_analyze(code, target_name)
        return report

    if name == "get_recent_news":
        limit = int(arguments.get("limit") or 8)
        lines = fetch_news(code, limit=limit)
        if not lines:
            return "个股新闻未取到"
        return "\n".join(f"- {line}" for line in lines)

    return f"未知工具 {name}"


# ──────────────────────────────────────────────────────────────────────────────
# Prompt 与 DeepSeek 调用
# ──────────────────────────────────────────────────────────────────────────────

QA_SYSTEM_PROMPT = (
    "你是准确、克制、简洁的投资研究助手。先判断用户问题的类型和范围，不要默认问题是个股问题，"
    "也不要为了显得完整而堆砌无关数据。\n"
    "工具使用：只调用与问题直接相关的工具，够用即止；模型自行决定是否调用、调用哪个以及调用几次。\n"
    "- 程序策略、指标、规则或用法问题：优先 search_project_docs；\n"
    "- 通用概念或规则解释可直接回答，只有涉及时效性事实、行业现状或需要核验时才 search_web；\n"
    "- 板块问题优先取板块和市场证据（get_market_summary，必要时 search_web），不要连带调用个股技术面或基本面；\n"
    "- 只问个股某一维度时只取该维度：技术面用 get_technical_analysis/get_trend_volume，"
    "基本面用 get_fundamentals_report，新闻用 get_recent_news；\n"
    "- get_analysis_bundle 仅用于明确要求的个股/指数综合分析，不作为普通问答的默认入口；\n"
    "- 组合或持仓问题才调用 get_portfolio_report。需要外部背景、近期事件或公司信息时再调用 search_web。\n"
    "准确性：事实与推断分开，证据与结论一一对应；本地数据、网络来源和模型判断分开表述；"
    "数据缺失、过期、可疑或数据矛盾时如实说明，绝不编造数字、新闻或事实。网络观点标注来源，且注明不等于事实。\n"
    "回答方式：简单问题直接回答，不要机械套用固定模板；复杂个股、行情或操作问题才按【结论】、"
    "【关键依据】、【操作建议】、【风险与观察】组织。回答默认不超过500字，宁可短而准确，不要啰嗦。\n"
    "专业术语必须就地用通俗中文解释：M20（20日均线）、ATR（近期正常波动幅度）、量比（当前成交量相对常态）等。"
    "涉及做T价差时，把价差门槛换算成具体卖出价与接回价示例，并说明ATR不是保证收益的硬性阈值，还要考虑交易成本和滑点。\n"
    "涉及操作建议时适配已持有或无持仓场景，写明触发条件、失效条件、仓位/风险控制和复查节点；"
    "不要为了完整而强行给具体价位。深套持仓遵守项目深套策略，不得把无持仓入场失效位直接套用于已有持仓，"
    "也不建议在破位低位直接割肉；逻辑还在（逻辑未破坏）时优先给滚动做T方案：保留底仓，只用小仓在冲高减出，"
    "待回踩接回或重新站回支撑后接回，以降低持仓成本；执行前确认价差、量能和支撑，且不承诺回本；"
    "若再创新低取消接回或补仓计划；只有逻辑/基本面已破坏时，才等反弹到压力区分批离场；"
    "成本或浮亏未取到时，不判断是否深套，不给清仓建议。\n"
    "只在涉及具体买卖、仓位或持仓处置时附风险声明：以上基于公开数据与模型生成，不构成投资建议。"
    "最高优先级约束：盘中买卖、补仓、减仓、清仓和做T只服从本地动作守卫；"
    "未收到明确的actionable状态、行情交易日期、15/30分钟新鲜证据和T+1可卖量时，"
    "不得直接下达交易动作，只能写观察、风险事实、停止执行条件或下一交易日复核。"
)


def build_messages(context: AnalysisContext) -> List[Dict[str, str]]:
    """把上下文拼成 user 消息；缺什么写什么，让模型如实交代。"""
    kind_word = {"stock": "A股个股", "index": "指数", "global": "港美股"}.get(context.kind, "标的")
    sections = [
        f"标的：{context.name}（{context.code}，{kind_word}）",
        f"现价：{context.price:.2f}" if context.price else "现价：未取到",
        "",
    ]
    materials = build_context_materials(context)
    if materials:
        sections += [materials, ""]
    sections.append("请按系统提示的结构输出综合分析总结。")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(sections)},
    ]


def build_context_materials(context: AnalysisContext) -> str:
    """按固定顺序拼装报告素材，供结构化分析和自由问答复用。"""
    sections = []
    if context.technical_text:
        sections += ["【技术面数据】", context.technical_text, ""]
    if context.fundamentals_text:
        label = "基本面报告" if context.kind == "stock" else "港美股基本面/技术面报告"
        sections += [f"【{label}】", context.fundamentals_text, ""]
    if context.portfolio_text:
        sections += ["【持仓报告】", context.portfolio_text, ""]
    if context.trend_text:
        sections += ["【走势与量能】", context.trend_text, ""]
    if context.news_lines:
        sections += ["【近期资讯（标题）】"]
        sections += [f"- {line}" for line in context.news_lines]
        sections.append("")
    if context.market_text:
        sections += ["【市场环境（盘面总结缓存）】", context.market_text, ""]
    if context.recent_market_text:
        sections += ["【近期大盘与板块表现】", context.recent_market_text, ""]
    if context.market_timing_text:
        sections += ["【时点与资金证据】", context.market_timing_text, ""]
    if context.capital_flow_text:
        sections += ["【盘中资金流辅助证据】", context.capital_flow_text, ""]
    if context.missing:
        sections += ["【数据缺口】以下内容本次未取到，请在分析中如实说明而不是补全："]
        sections += [f"- {item}" for item in context.missing]
        sections.append("")
    return "\n".join(sections).strip()


def chat_completion(
    messages: List[Dict[str, str]],
    config: Dict[str, str],
    temperature: float = 0.4,
    max_tokens: int = 8000,
    timeout: float = 240.0,
) -> str:
    """DeepSeek chat completions（OpenAI 兼容），返回正文文本。

    deepseek-flash 是推理模型：返回里 reasoning_content（思考过程）同样计入
    max_tokens，所以额度要给足；思考吃光额度会导致 content 为空，这里显式报错。
    """
    message, finish_reason = _chat_request(messages, config, temperature=temperature,
                                           max_tokens=max_tokens, timeout=timeout)
    content = (message.get("content") or "").strip()
    if not content:
        raise RuntimeError(
            f"DeepSeek 返回了空正文（finish_reason={finish_reason}）；"
            "推理模型的思考过程会占用 max_tokens，可调大 ai_analysis.chat_completion 的 max_tokens 后重试"
        )
    return content


def _chat_request(
    messages: List[Dict[str, Any]],
    config: Dict[str, str],
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: float = 0.4,
    max_tokens: int = 8000,
    timeout: float = 240.0,
) -> tuple:
    """发一次 chat/completions，返回 (message 字典, finish_reason)。"""
    api_key = (config.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError(
            "未配置 DeepSeek API Key："
            f"在项目目录 {CONFIG_FILE} 写入 {json.dumps({'api_key': 'sk-...'}, ensure_ascii=False)}"
            "，或设置环境变量 DEEPSEEK_API_KEY"
        )
    base = (config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = f"{base}/chat/completions"
    payload: Dict[str, Any] = {
        "model": (config.get("model") or DEFAULT_MODEL).strip(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
        return message, choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"DeepSeek 返回结构异常：{str(data)[:200]}")


def format_report(context: AnalysisContext, answer: str, model: str) -> str:
    """拼成落盘报告：抬头 + 数据来源 + AI 正文。"""
    as_of = datetime.now().strftime("%Y-%m-%d %H:%M")
    price_line = f"现价 {context.price:.2f}" if context.price else "现价 —"
    source_bits = []
    if context.technical_text:
        source_bits.append("技术面")
    if context.fundamentals_text:
        source_bits.append("基本面")
    if context.portfolio_text:
        source_bits.append("持仓报告")
    if context.trend_text:
        source_bits.append("走势与量能")
    if context.news_lines:
        source_bits.append(f"资讯{len(context.news_lines)}条")
    if context.market_text:
        source_bits.append("盘面总结")
    if context.recent_market_text:
        source_bits.append("近几日大盘/板块")
    if context.market_timing_text:
        source_bits.append("时点与资金证据")
    if context.capital_flow_text:
        source_bits.append("盘中资金流辅助证据")
    return "\n".join([
        f"🤖 {context.name}（{context.code}）AI 分析总结",
        f"{price_line}｜生成于 {as_of}｜模型 {model}",
        f"输入素材：{'、'.join(source_bits) if source_bits else '无'}",
        "",
        answer,
        "",
    ])


def analyze(keyword: str, project_dir: str = ".", search_fn=None) -> str:
    """一键：解析输入 → 汇总上下文 → 调 DeepSeek → 返回报告文本。"""
    config = load_config(project_dir)
    if not config.get("api_key"):
        raise RuntimeError(
            "未配置 DeepSeek API Key："
            f"在 {os.path.join(project_dir, CONFIG_FILE)} 写入 {json.dumps({'api_key': 'sk-...'}, ensure_ascii=False)}"
            "，或设置环境变量 DEEPSEEK_API_KEY"
        )
    resolved = resolve_input(keyword, search_fn=search_fn)
    if resolved is None:
        raise RuntimeError(f"没有识别出「{keyword}」对应的股票或指数")
    code, name = resolved
    context = gather_context(code, name, project_dir=project_dir)
    messages = build_messages(context)
    answer = chat_completion(messages, config)
    return format_report(context, answer, config.get("model") or DEFAULT_MODEL)


# ──────────────────────────────────────────────────────────────────────────────
# 自由问答（function calling 循环）
# ──────────────────────────────────────────────────────────────────────────────


def format_qa_report(
    question: str,
    answer: str,
    used_tools: List[str],
    web_sources: List[str],
    model: str,
) -> str:
    as_of = datetime.now().strftime("%Y-%m-%d %H:%M")
    tool_text = "、".join(dict.fromkeys(used_tools)) if used_tools else "无"
    lines = [
        f"🤖 AI 问答｜生成于 {as_of}｜模型 {model}",
        f"问题：{question}",
        f"调用工具：{tool_text}",
    ]
    if web_sources:
        lines.append("网络来源：" + "、".join(list(dict.fromkeys(web_sources))[:6]))
    lines += ["", answer, ""]
    return "\n".join(lines)


def ask(
    question: str,
    project_dir: str = ".",
    search_fn: Optional[Callable] = None,
    max_rounds: int = 8,
    max_tokens: int = 6000,
) -> str:
    """自由文本问答：模型按需调用工具（程序报告/接口 + 网络检索），给出简洁结论。"""
    config = load_config(project_dir)
    if not config.get("api_key"):
        raise RuntimeError(
            "未配置 DeepSeek API Key："
            f"在 {os.path.join(project_dir, CONFIG_FILE)} 写入 {json.dumps({'api_key': 'sk-...'}, ensure_ascii=False)}"
            "，或设置环境变量 DEEPSEEK_API_KEY"
        )
    text = (question or "").strip()
    if not text:
        raise RuntimeError("问题为空")

    # 问题里若能识别出标的，给模型一个锚点（不强求，模型可自行用工具确认）
    hint = ""
    resolved = resolve_input(text, search_fn=search_fn)
    if resolved:
        hint = f"\n\n（辅助信息：问题里识别到标的 {resolved[1]}（{resolved[0]}），需要时可直接用该代码调用工具。）"

    collector: Dict[str, Any] = {"web_sources": []}

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": QA_SYSTEM_PROMPT},
        {"role": "user", "content": text + hint},
    ]
    used_tools: List[str] = []

    for _ in range(max_rounds):
        message, finish_reason = _chat_request(
            messages, config, tools=TOOLS_SCHEMA, max_tokens=max_tokens
        )
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            # 记录模型已说的话（可能为空），再逐个执行工具
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except Exception:
                    arguments = {}
                used_tools.append(name)
                result = dispatch(
                    name, arguments,
                    project_dir=project_dir,
                    search_fn=search_fn,
                    collector=collector,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": result[:8000],
                })
            continue

        content = (message.get("content") or "").strip()
        if not content:
            raise RuntimeError(
                f"DeepSeek 返回了空正文（finish_reason={finish_reason}）；"
                "推理模型的思考会占用 max_tokens，可调大后重试"
            )
        return format_qa_report(
            text, content, used_tools,
            collector.get("web_sources") or [],
            config.get("model") or DEFAULT_MODEL,
        )

    raise RuntimeError(f"超过最大工具调用轮数（{max_rounds}），未得到最终回答")
