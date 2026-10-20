#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块龙头：候选推荐 + 封板/炸板分档状态机。

两个职责刻意分开、频率也分开：

1. **候选推荐**（低频，每日一次）：只在板块数据刷新时按成交额挑「有分量的大票」。
   绝不在 15 秒轮询里扫板块成分股——那是纯浪费接口请求量。
2. **分档状态机**（每 15 秒喂一次已配置龙头的行情）：看跃迁不看状态，
   封板 → 炸板 → 强承接 / 退潮 → 回封，每段各出一次事件。

相位与对持仓做T的影响（详见 docs/strategies/板块龙头封板与做T.md）：

| 相位 | 含义 | 对持仓做T |
|---|---|---|
| NONE    | 当日尚未封板 | 不干预 |
| SEALED  | 封死涨停（首段/回封） | 抑制 |
| BLOWOUT | 当日第 1 次炸板、尚未定性 | 抑制 |
| STRONG  | 强承接（炸板后守住高位、未确认退潮） | 抑制（提示持有） |
| FADED   | 确认退潮，**或当日第 2 次及以后炸板** | 解除（提醒恢复做T） |

放开做T只有两个口子，都要求证据到位：
1. **退潮双确认**：价格跌出涨停下方 2pp **且** 跌破分时均价线，连续 2 轮；
2. **当日炸板 ≥ 2 次**：龙头反复开板，分歧已经摆明（回封不清零、跨日清零）。

第 1 次炸板未确认退潮时按「强承接」口径继续持有；文案沿用「强承接」，判定逻辑以上面两条为准。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Optional

try:
    from intraday_tactics import limit_pct_for_board
except ImportError:  # 单测直接 import 本模块时的兜底
    def limit_pct_for_board(code: str) -> float:
        c = (code or "").lower()
        if c.startswith("bj"):
            return 30.0
        if c.startswith("sz300") or c.startswith("sz301") or c.startswith("sh688"):
            return 20.0
        return 10.0


# ── 常量 ─────────────────────────────────────────────────────────────────────

STRONG_BAND_PCT = 2.0        # 强承接带：涨停价下方 2 个百分点（主板即 ≥8%）
STRONG_HOLD_SECONDS = 300    # 强承接需持续 5 分钟才定性
FADE_CONFIRM_ROUNDS = 2      # 退潮连续 2 轮确认（约30秒），防下影线误触
BLOWOUT_RELEASE_COUNT = 2    # 当日炸板第 2 次起恢复做T（第 1 次未退潮仍持有）
LEADER_TOP_N = 5             # 每板块自动推荐几只龙头
MAX_EVENTS_PER_DAY = 8       # 每只龙头每日相位提醒上限，兜底极端抖动
# 只匹配名称开头的 ST 标记（ST / *ST / S*ST / SST），避免误伤「东方斯特」这类名字
ST_NAME_PATTERN = re.compile(r"^\*?S?\*?ST", re.IGNORECASE)
SPECIAL_LIMITS = {
    # 全市场唯一 ±5%：未完成股改的 S 股（历史遗留特例，不是 ST）
    "sh600182": 5.0,
}


def effective_limit_pct(code: str) -> float:
    """涨跌停幅度（%）：按板块取 10/20/30，S 佳通特例 5%。"""
    normalized = (code or "").lower()
    return SPECIAL_LIMITS.get(normalized, limit_pct_for_board(normalized))


def limit_up_price(prev_close: float, limit_pct: float) -> Optional[float]:
    """涨停价（四舍五入到分）。新股哨兵价、缺昨收时返回 None。"""
    try:
        prev = float(prev_close)
        pct = float(limit_pct)
    except (TypeError, ValueError):
        return None
    if prev <= 0 or pct <= 0:
        return None
    price = round(prev * (1 + pct / 100.0), 2)
    # 新股上市初期涨停价可能为 -1 哨兵值，不能当真实价格
    if price <= 0:
        return None
    return price


@dataclass
class Tick:
    """一轮行情快照（只需龙头那几只，不扫板块）。"""
    code: str
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    ask1_vol: float = 0.0      # 卖一量；0 表示卖一为空
    ask1_price: float = 0.0    # 卖一价；0 表示卖一为空
    amount: float = 0.0        # 累计成交额（元）
    volume: float = 0.0        # 累计成交量（股）

    @property
    def change_pct(self) -> float:
        if self.prev_close <= 0 or self.price <= 0:
            return 0.0
        return (self.price / self.prev_close - 1) * 100

    @property
    def vwap(self) -> Optional[float]:
        """分时均价线 = 成交额 ÷ 成交量（现成字段，不用另拉数据）。"""
        if self.volume <= 0 or self.amount <= 0:
            return None
        return self.amount / self.volume


class Phase(str, Enum):
    NONE = "none"
    SEALED = "sealed"
    BLOWOUT = "blowout"
    STRONG = "strong"
    FADED = "faded"


# 相位 → 是否抑制持仓做T
SUPPRESS_PHASES = {Phase.SEALED, Phase.BLOWOUT, Phase.STRONG}

PHASE_LABEL = {
    Phase.NONE: "未封板",
    Phase.SEALED: "封板",
    Phase.BLOWOUT: "炸板观察",
    Phase.STRONG: "强承接",
    Phase.FADED: "转弱",
}


@dataclass
class LeaderEvent:
    """一次相位跃迁事件。

    kind ∈ {sealed, strong, faded, blowout_repeat}：
    - sealed：封死涨停（首段/回封），抑制做T
    - strong：炸板但未确认退潮，继续持有
    - faded：确认退潮，恢复做T
    - blowout_repeat：当日第 2 次及以后炸板，恢复做T
    """
    kind: str
    code: str
    name: str
    phase: Phase
    change_pct: float = 0.0
    gap_to_limit_pp: float = 0.0   # 距涨停还有几个百分点（正数=还没到）
    vwap: Optional[float] = None
    episode: int = 1               # 第几段封板
    blowout_count: int = 0         # 当日累计炸板次数


@dataclass
class _LeaderState:
    phase: Phase = Phase.NONE
    phase_since: float = 0.0
    fade_rounds: int = 0
    strong_since: Optional[float] = None
    episode: int = 0
    blowout_count: int = 0      # 当日累计炸板次数（回封不清零、跨日清零）
    event_count: int = 0
    notified: set = field(default_factory=set)   # (episode, phase) 只通知一次


class LeaderGuard:
    """封板/炸板分档状态机。

    只认**跃迁**：封板→炸板→强承接/退潮→回封，每段各出一次事件；
    重新武装靠「回封开新一段」，不会「炸过一次就全天静默」。
    """

    def __init__(
        self,
        strong_band_pct: float = STRONG_BAND_PCT,
        strong_hold_seconds: float = STRONG_HOLD_SECONDS,
        fade_confirm_rounds: int = FADE_CONFIRM_ROUNDS,
        blowout_release_count: int = BLOWOUT_RELEASE_COUNT,
        max_events_per_day: int = MAX_EVENTS_PER_DAY,
    ) -> None:
        self.strong_band_pct = strong_band_pct
        self.strong_hold_seconds = strong_hold_seconds
        self.fade_confirm_rounds = max(1, int(fade_confirm_rounds))
        self.blowout_release_count = max(2, int(blowout_release_count))
        self.max_events_per_day = max(1, int(max_events_per_day))
        self._states: dict[str, _LeaderState] = {}
        self._day: Optional[str] = None

    # ── 外部查询 ──────────────────────────────────────────────────────────

    def phase_of(self, code: str) -> Phase:
        state = self._states.get(self._key(code))
        return state.phase if state else Phase.NONE

    def is_suppressing(self, code: str) -> bool:
        """该龙头当前是否要求暂缓持仓做T。"""
        return self.phase_of(code) in SUPPRESS_PHASES

    def hold_note(self, code: str, name: str = "") -> str:
        """给盘中信号用的短标签，拼在标题里要读得通。

        用「龙头封板」「龙头强承接」这类**事实短语**，动作（暂缓做T）放通知正文，
        沿用项目里「事实｜动作」的口径。别压成「龙头封板暂缓」——那会读成
        「龙头的封板暂缓了」。
        """
        phase = self.phase_of(code)
        if phase in (Phase.SEALED, Phase.BLOWOUT):
            return "龙头封板"
        if phase == Phase.STRONG:
            return "龙头强承接"
        return ""

    def states(self) -> dict[str, Phase]:
        return {code: state.phase for code, state in self._states.items()}

    # ── 喂行情 ──────────────────────────────────────────────────────────

    def update(
        self,
        tick: Tick,
        now: float = 0.0,
        day: Optional[str] = None,
    ) -> list[LeaderEvent]:
        """喂一轮行情，返回本轮产生的跃迁事件（可能为空）。"""
        if day and day != self._day:
            self._states.clear()
            self._day = day
        elif self._day is None and day:
            self._day = day

        key = self._key(tick.code)
        state = self._states.get(key)
        if state is None:
            state = _LeaderState()
            self._states[key] = state

        events: list[LeaderEvent] = []
        sealed = self._is_sealed(tick)
        strong_hold = self._is_strong_hold(tick)
        faded = self._is_faded(tick)

        def _emit(kind: str) -> Optional[LeaderEvent]:
            # 每段每相位只通知一次；再加个每日上限兜底极端抖动。
            # 刻意**不加冷却**：封板后约 7 分钟就会定性强承接，冷却会把这条
            # 合法提醒吞掉（2026-10-19 冒烟踩到）。
            mark = (state.episode, kind)
            if mark in state.notified:
                return None
            if state.event_count >= self.max_events_per_day:
                return None
            state.notified.add(mark)
            state.event_count += 1
            limit = limit_up_price(tick.prev_close, effective_limit_pct(tick.code))
            gap = (limit - tick.price) / tick.prev_close * 100 if limit and tick.prev_close > 0 else 0.0
            return LeaderEvent(
                kind=kind,
                code=tick.code,
                name=tick.name,
                phase=state.phase,
                change_pct=tick.change_pct,
                gap_to_limit_pp=gap,
                vwap=tick.vwap,
                episode=state.episode,
                blowout_count=state.blowout_count,
            )

        # ① 封死涨停：首段或回封，都开新一段并重新武装
        if sealed:
            if state.phase != Phase.SEALED:
                state.episode += 1
                state.phase = Phase.SEALED
                state.phase_since = now
                state.fade_rounds = 0
                state.strong_since = None
                event = _emit("sealed")
                if event:
                    events.append(event)
            return events

        # ② 刚从封板打开：当日第 1 次炸板进观察期——未退潮就继续持有、不提示做T；
        #    **当日第 2 次及以后炸板**（回封不清零、跨日清零）→ 龙头反复开板，
        #    分歧已经摆明，直接恢复做T。
        if state.phase == Phase.SEALED:
            state.blowout_count += 1
            state.fade_rounds = 0
            state.strong_since = None
            if state.blowout_count >= self.blowout_release_count:
                state.phase = Phase.FADED
                state.phase_since = now
                event = _emit("blowout_repeat")
                if event:
                    events.append(event)
                return events
            state.phase = Phase.BLOWOUT
            state.phase_since = now

        # ③ 炸板观察 / 强承接：定性 退潮 或 强承接
        if state.phase in (Phase.BLOWOUT, Phase.STRONG):
            if faded:
                state.fade_rounds += 1
                if state.fade_rounds >= self.fade_confirm_rounds:
                    state.phase = Phase.FADED
                    state.phase_since = now
                    event = _emit("faded")
                    if event:
                        events.append(event)
                return events
            state.fade_rounds = 0
            if strong_hold:
                if state.strong_since is None:
                    state.strong_since = now
                if (
                    state.phase == Phase.BLOWOUT
                    and state.strong_since
                    and (now - state.strong_since) >= self.strong_hold_seconds
                ):
                    state.phase = Phase.STRONG
                    state.phase_since = now
                    event = _emit("strong")
                    if event:
                        events.append(event)
            else:
                # 骑墙：强承接计时必须连续，断了就重来
                state.strong_since = None
            return events

        # ④ FADED / NONE：等回封开新一段（①已处理）
        return events

    # ── 判定 ──────────────────────────────────────────────────────────────

    def _is_sealed(self, tick: Tick) -> bool:
        """封死涨停 = 价格贴在涨停价 **且** 卖一为空（触板未封不算）。"""
        limit_pct = effective_limit_pct(tick.code)
        limit = limit_up_price(tick.prev_close, limit_pct)
        if limit is None or tick.price <= 0:
            return False
        # 贴涨停价（留半分钱容差）；远超涨停价的多为无涨跌幅新股，不当作封板
        if not (limit - 0.005 <= tick.price <= limit + 0.005):
            return False
        # 卖一为空才算封死；卖一价为负（异常/哨兵）视为未知，不认定封死
        if tick.ask1_price < 0:
            return False
        return tick.ask1_vol <= 0 or tick.ask1_price <= 0

    def _is_strong_hold(self, tick: Tick) -> bool:
        """强承接：守在涨停价下方 2pp 以内 **且** 站稳分时均价线。"""
        if tick.prev_close <= 0 or tick.price <= 0:
            return False
        limit_pct = effective_limit_pct(tick.code)
        in_band = tick.change_pct >= (limit_pct - self.strong_band_pct)
        vwap = tick.vwap
        above_vwap = vwap is None or tick.price >= vwap   # 均价不可得时只看价格带
        return in_band and above_vwap

    def _is_faded(self, tick: Tick) -> bool:
        """退潮**双确认**：跌出价格带 **且** 跌破分时均价线；缺一不可。"""
        if tick.prev_close <= 0 or tick.price <= 0:
            return False
        limit_pct = effective_limit_pct(tick.code)
        out_of_band = tick.change_pct < (limit_pct - self.strong_band_pct)
        vwap = tick.vwap
        if vwap is None:
            return False          # 均价不可得就不判退潮，避免单边误恢复做T
        return out_of_band and tick.price < vwap

    @staticmethod
    def _key(code: str) -> str:
        return (code or "").lower()


# ── 候选推荐（纯函数，方便单测）────────────────────────────────────────────

def rank_leader_candidates(
    rows: Iterable[dict],
    top_n: int = LEADER_TOP_N,
) -> list[dict]:
    """按当日成交额取「有分量的大票」，排除 ST——小票涨停不算龙头。

    rows: [{code, name, amount}]，amount 为当日累计成交额（元）。
    返回 [{code, name, turnover_yi}]，按成交额降序。
    """
    picked: list[dict] = []
    for row in rows or []:
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or "").strip()
        if not code:
            continue
        if ST_NAME_PATTERN.search(name):
            continue
        try:
            amount = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            continue
        picked.append({
            "code": code,
            "name": name,
            "source": "auto",
            "turnover_yi": round(amount / 1e8, 2),
        })
    picked.sort(key=lambda item: item["turnover_yi"], reverse=True)
    return picked[: max(0, int(top_n))]


def parse_leader_list_text(text: str) -> list[str]:
    """解析「编辑龙头列表」文本框，返回待解析关键字（保持顺序、去重）。

    一行一只，也接受 `|`、`、`、`;`、`,` 分隔；行里有代码（`sh600487` / `600487`）
    就取代码，否则整行当名称。空行忽略。
    """
    items: list[str] = []
    normalized = (text or "").replace("\r\n", "\n").replace("，", ",")
    for raw_line in re.split(r"[\n;；]", normalized):
        for piece in re.split(r"[|、,]", raw_line):
            piece = piece.strip()
            if not piece:
                continue
            match = re.search(r"(?:sh|sz|bj)\d{6}|\b\d{6}\b", piece, re.IGNORECASE)
            keyword = match.group(0).lower() if match else piece
            if keyword not in items:
                items.append(keyword)
    return items


# ── 菜单文案（纯函数，方便单测）──────────────────────────────────────────

def format_leader_menu_label(
    name: str,
    code: str = "",
    change_pct: Optional[float] = None,
    phase_label: str = "",
    turnover_yi: Optional[float] = None,
) -> str:
    """龙头菜单条目：`名称 +涨跌幅 · 成交额X亿 · [相位]`。

    只显示名称——菜单里代码是噪音（用户 2026-10-19 要求去掉）；名称缺失时退到代码。
    涨跌幅是用户扫一眼最想要的数字，紧跟名称放；拿不到行情就不硬凑 0%。
    """
    head = str(name or "").strip() or str(code or "").strip()
    if change_pct is not None:
        head += f" {change_pct:+.2f}%"
    suffix = ""
    if turnover_yi is not None:
        suffix += f" · 成交额{turnover_yi}亿"
    if phase_label:
        suffix += f" · [{phase_label}]"
    return head + suffix


def parse_holding_sector_config(text: str) -> list[dict]:
    """解析「持仓股, 所属板块, 龙头1|龙头2」多行配置。

    - 持仓股：代码或名称（换 code 由调用方解析）；多只用 `|` 或 `、` 分隔，共用板块与龙头
    - 所属板块：多个用 `/` 分隔；填 `-` 或留空 = 清除手动归属、恢复自动识别
    - 龙头：可省略；多只用 `|` 或 `、` 分隔，挂在**第一个**板块上

    返回 [{"stock", "sectors", "leaders", "clear"}]；空行与 `#` 注释行忽略。
    """
    records: list[dict] = []
    normalized = (text or "").replace("\r\n", "\n").replace("，", ",")
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # 一行一记录；`;` 也当行分隔，兼容「持仓」批量输入的习惯
        for piece in re.split(r"[;；]", line):
            piece = piece.strip()
            if not piece:
                continue
            fields = [item.strip() for item in piece.split(",", 2)]
            stock_field = fields[0]
            if not stock_field:
                continue
            sector_field = fields[1] if len(fields) > 1 else ""
            leader_field = fields[2] if len(fields) > 2 else ""
            clear = sector_field in ("", "-", "—", "清除", "clear", "auto")
            sectors = [
                name for name in re.split(r"[/、]", sector_field)
                if name.strip() and not clear
            ]
            leaders = [
                item.strip()
                for item in re.split(r"[|、/]", leader_field)
                if item.strip()
            ]
            # 持仓股可写多只（`|`、`、` 分隔）：一个板块挂多只持仓时不用重复整行
            stocks = [item for item in re.split(r"[|、]", stock_field) if item.strip()]
            for stock in stocks or [stock_field]:
                records.append({
                    "stock": stock.strip(),
                    "sectors": [name.strip() for name in sectors],
                    "leaders": leaders,
                    "clear": clear,
                })
    return records


# ── 持久化 ───────────────────────────────────────────────────────────────────

class HoldingSectorStore:
    """持仓股 → 所属板块（手动归属）。

    新浪反查表（`_sector_map`）的板块归属经常不准，**手动归属一旦设置就以它为准**；
    没设过的持仓股继续走自动反查。字段刻意只存板块名，不存龙头——
    龙头挂在板块上（`SectorLeaderStore`），换手/调仓后自动跟上。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.data: dict = {"map": {}}

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("map"), dict):
                self.data = data
        except (OSError, json.JSONDecodeError, AttributeError):
            self.data = {"map": {}}
        return self.data

    def save(self) -> bool:
        temp_path = None
        try:
            directory = os.path.dirname(self.path) or "."
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory,
                prefix=".holding_sectors.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            return True
        except OSError:
            return False
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    @staticmethod
    def _key(code: str) -> str:
        return (code or "").lower().strip()

    def _map(self) -> dict:
        # 每次都拿到**真正的**内部 dict：`data.get("map") or {}` 会把 setdefault
        # 写进临时空字典（2026-10-19 单测抓到）。
        if not isinstance(self.data.get("map"), dict):
            self.data["map"] = {}
        return self.data["map"]

    def sectors_of(self, code: str) -> list[str]:
        entry = self._map().get(self._key(code)) or {}
        names = entry.get("sectors") if isinstance(entry, dict) else None
        return [str(name) for name in (names or []) if str(name).strip()]

    def has_entry(self, code: str) -> bool:
        """是否有手动记录（含**空名单**——那表示「明确无板块」）。"""
        return self._key(code) in self._map()

    def is_manual(self, code: str) -> bool:
        return self.has_entry(code)

    def set_sectors(self, code: str, names: list, display_name: str = "") -> None:
        """写入手动归属。**空名单 = 明确无板块**（挡掉自动反查），
        要恢复自动识别请用 `clear()`（删掉记录）。"""
        key = self._key(code)
        if not key:
            return
        cleaned = [str(name).strip() for name in (names or []) if str(name).strip()]
        entry = self._map().setdefault(key, {})
        entry["sectors"] = cleaned
        if display_name:
            entry["name"] = display_name

    def clear(self, code: str) -> bool:
        removed = self._map().pop(self._key(code), None)
        return removed is not None

    def items(self) -> list[dict]:
        out = []
        for code, entry in self._map().items():
            if not isinstance(entry, dict):
                continue
            out.append({
                "code": code,
                "name": entry.get("name") or code,
                "sectors": [str(name) for name in entry.get("sectors") or []],
            })
        return out


class SectorLeaderStore:
    """板块 → 龙头列表（可多只）的持久化。手动改过的板块加 pinned，自动推荐不覆盖。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.data: dict = {"sectors": {}}

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("sectors"), dict):
                self.data = data
        except (OSError, json.JSONDecodeError, AttributeError):
            self.data = {"sectors": {}}
        return self.data

    def save(self) -> bool:
        temp_path = None
        try:
            directory = os.path.dirname(self.path) or "."
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory,
                prefix=".sector_leaders.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            return True
        except OSError:
            return False
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def sector(self, name: str) -> dict:
        sectors = self.data.setdefault("sectors", {})
        entry = sectors.get(name)
        if not isinstance(entry, dict):
            entry = {"pinned": False, "leaders": []}
            sectors[name] = entry
        entry.setdefault("pinned", False)
        entry.setdefault("leaders", [])
        return entry

    def leaders_of(self, name: str) -> list[dict]:
        return list(self.sector(name).get("leaders") or [])

    def is_pinned(self, name: str) -> bool:
        return bool(self.sector(name).get("pinned"))

    def set_leaders(self, name: str, leaders: list[dict], pinned: bool = True) -> None:
        entry = self.sector(name)
        entry["leaders"] = list(leaders or [])
        entry["pinned"] = bool(pinned)

    def add_leader(self, name: str, code: str, display_name: str = "") -> bool:
        entry = self.sector(name)
        leaders = [item for item in entry.get("leaders") or [] if item.get("code") != code]
        leaders.append({
            "code": code,
            "name": display_name or code,
            "source": "manual",
        })
        entry["leaders"] = leaders
        entry["pinned"] = True
        return True

    def remove_leader(self, name: str, code: str) -> bool:
        entry = self.sector(name)
        before = len(entry.get("leaders") or [])
        entry["leaders"] = [item for item in entry.get("leaders") or [] if item.get("code") != code]
        entry["pinned"] = True
        return len(entry["leaders"]) != before

    def apply_auto(self, name: str, leaders: list[dict], updated_at: str = "") -> bool:
        """写入自动推荐结果；pinned 板块不覆盖。"""
        if self.is_pinned(name):
            return False
        entry = self.sector(name)
        entry["leaders"] = list(leaders or [])
        entry["auto_updated_at"] = updated_at
        return True
