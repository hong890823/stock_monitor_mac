#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macOS 菜单栏股票监控
常驻显示，菜单栏每15秒刷新
"""

import mini_racer_reuse  # noqa: F401  复用 V8 解码上下文，防 akshare 泄漏 Isolate
import rumps
import socket
import urllib.request
import urllib.parse
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
import traceback
import os
import sys
import subprocess
import json
import math
import tempfile
import fcntl
import warnings
import pandas as pd
import numpy as np
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from indicator_refresh import IndicatorRefreshScheduler
from indicator_sort import order_relaxed_hits, order_strict_hits
from limit_up_acceleration import evaluate_limit_up_acceleration
from continuation_hold_policy import (
    ContinuationHoldInput,
    assess_continuation_hold,
    continuation_hold_to_dict,
)
from sector_leaders import (
    HoldingSectorStore,
    LeaderGuard,
    SectorLeaderStore,
    Tick as LeaderTick,
    Phase as LeaderPhase,
    PHASE_LABEL as LeaderPhaseLabel,
    effective_limit_pct,
    format_leader_menu_label,
    parse_holding_sector_config,
    parse_leader_list_text,
    rank_leader_candidates,
)
from limit_seal_events import SealEventTracker, SealTick
from intraday_ignition import IgnitionSample, IgnitionTracker
from alert_repeat_guard import AlertRepeatGuard
from five_one_three import (
    FiveOneThreeStage,
    inspect as inspect_five_one_three,
    order_five_one_three_hits,
)
from portfolio_analysis import evaluate_position, format_portfolio_report
from portfolio_addition import assess_holding, select_addition_candidate
from portfolio_addition_service import PortfolioAdditionService
from portfolio_addition_store import PortfolioAdditionStore
from portfolio_daily_report import (
    FINALIZE_MINUTE,
    FINALIZE_RETRY_SECONDS,
    atr_pct_of,
    build_overview,
    compute_pnl,
    decide_report_mode,
    format_daily_report,
    format_menu_summary,
    menu_title,
    portfolio_fingerprint,
)
from deep_loss_policy import suggested_add_quantity
from news_impact import NewsImpactService
from intraday_bars import build_timeframe_evidence, fetch_intraday_bars
from intraday_trade_policy import (
    DecisionAction,
    PositionEligibility,
    evaluate_trade_action,
)
from trade_action_guard import guard_actionable_copy, guard_intraday_signal
from market_evidence import update_t_filter_event_outcome
from market_timing import format_market_timing_data
from market_cycle_policy import MarketCycleInput, assess_market_cycle, format_market_cycle_assessment
from market_cycle_state import MarketCycleStateStore
from menu_item_utils import replace_menu_item_title
from stock_analysis import analyze_history, format_report
from strong_continuation_policy import (
    StrongContinuationInput,
    assess_strong_continuation,
)
from trade_calculator import (
    calculate_trade,
    format_trade_report,
    format_target_cost_report,
    plan_target_cost,
)
from turnover_tracker import TurnoverTracker, format_turnover_lines, fetch_previous_turnover_curve, TurnoverCurve
from market_alert import MarketAlertEngine, IndexSnapshot, AlertLevel, format_alert_notification, MarketAlert
from typing import Optional
from intraday_tactics import (
    COOLDOWN_SECONDS,
    IntradayTactics,
    IntradaySignal,
    IntradaySignalType,
    STRONG_MARKET_PCT,
    STRONG_SECTOR_PCT,
    STRONG_TURNOVER_YI,
    limit_pct_for_board,
)
from market_environment import MarketEnvironmentStatus, evaluate_market_environment
from volume_price import VolumePrice, tag as vp_tag
from volume_ratio import VolumeRatio, trading_minutes_elapsed
from price_position import PricePosition
from technical_summary import TechnicalSummary, format_summary as format_technical_summary
from signal_confidence import SignalConfidence
from trade_ledger import TradeLedger, parse_trade_text
from market_levels import compute_levels, merge_live_bar
from observation_pool import ObservationPool
from observation_alerts import ObservationEntryTracker, is_entry_session
from quote_clock import parse_quote_clock
from observation_ui import build_observation_view, decorate_stock, prioritize_stocks, indicator_color
try:
    from entry_review_service import EntryReviewService
    _ENTRY_REVIEW_IMPORT_ERROR = None
except Exception as error:
    EntryReviewService = None
    _ENTRY_REVIEW_IMPORT_ERROR = str(error)
from capital_flow import (
    FlowCache,
    FlowDirectionTracker,
    fetch_sector_flow,
    fetch_stock_flow_snapshot,
    flow_confirmation_state,
    flow_direction,
)
warnings.filterwarnings("ignore")


def market_rebound_guidance(change_pct: float) -> Optional[str]:
    """根据指数涨跌幅给出盘面反弹/偏强环境的简短提示。"""
    if change_pct < -0.3:
        return (
            '下跌后的反弹先看能否收回支撑并有量能承接；确认前不追高，'
            '持仓根据仓位和风险承受能力控制风险。'
        )
    if change_pct > 0.3:
        return '指数偏强但需量能继续配合，优先观察主线是否扩散，避免追高。'
    return None


try:
    import objc
    from PyObjCTools import AppHelper
    NSApplication = objc.lookUpClass("NSApplication")
    _NSApp = None
    _HAS_OBJC = True
    from AppKit import (
        NSWindow, NSView, NSColor, NSTextView, NSFont,
        NSFloatingWindowLevel, NSBorderlessWindowMask,
        NSScreen, NSMakeRect, NSBackingStoreBuffered,
        NSTextField, NSCenterTextAlignment,
        NSBezelBorder, NSTimer, NSMutableAttributedString,
        NSTableView, NSTableColumn, NSTableHeaderView,
        NSTableViewSelectionHighlightStyleNone,
        NSTextFieldCell, NSScrollView, NSButton,
        NSScreenSaverWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
    )
    from Foundation import NSMakeSize, NSMakeRange, NSObject
except ImportError:
    _HAS_OBJC = False


def _get_ns_app():
    """按需创建并缓存 NSApplication，避免导入模块时触发 AppKit 初始化。"""
    global _NSApp
    if not _HAS_OBJC:
        return None
    if _NSApp is None:
        _NSApp = NSApplication.sharedApplication()
    return _NSApp


INDICATOR_API_LOCK = threading.Lock()

# 系统级 socket 超时兜底：任何没显式带 timeout 的网络调用（含 akshare 内部、
# urllib）最多阻塞 20 秒，避免一个挂死的连接把后台线程永久卡住（2026-09-30
# 取数循环被全市场深扫里的网络调用卡死、菜单栏冻结约 1 小时）。
socket.setdefaulttimeout(20)

# urllib3 v2 在 LibreSSL 环境下的 OpenSSL 兼容性提示是纯噪音（每次启动灌进 crash.log）
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

# akshare 不暴露 timeout，只能在调用期间临时接管 Session.request。
# 补丁只安装一次，超时值放在 thread-local 里按线程取用，这样选股线程的超时
# 不会踩到后台行情线程的请求，退出时也不再「还原」出竞态。
# 注意不能复用 INDICATOR_API_LOCK：调用形式是 `with INDICATOR_API_LOCK, requests_timeout(...)`,
# 补丁安装发生在锁内，用同一把非重入锁会直接死锁。
_REQUEST_TIMEOUT_LOCAL = threading.local()
_REQUEST_TIMEOUT_INSTALLED = False
_REQUEST_TIMEOUT_PATCH_LOCK = threading.Lock()


def _install_request_timeout_patch():
    global _REQUEST_TIMEOUT_INSTALLED
    if _REQUEST_TIMEOUT_INSTALLED:
        return
    with _REQUEST_TIMEOUT_PATCH_LOCK:
        if _REQUEST_TIMEOUT_INSTALLED:
            return
        original_request = requests.sessions.Session.request

        def request_with_timeout(session, method, url, **kwargs):
            timeout = getattr(_REQUEST_TIMEOUT_LOCAL, "timeout", None)
            if timeout is not None:
                kwargs.setdefault("timeout", timeout)
            return original_request(session, method, url, **kwargs)

        requests.sessions.Session.request = request_with_timeout
        _REQUEST_TIMEOUT_INSTALLED = True

# 关注的概念板块关键词
CONCEPT_KEYWORDS = ["机器人", "航天", "半导体", "电网", "芯片", "新能源", "人工智能", "光伏", "锂电"]

# ============ 指标计算函数 ============

def calc_macd(close, fast=12, slow=26, signal=9):
    """计算MACD指标
    返回: (dif, dea, macd_hist)
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd_hist = 2 * (dif - dea)
    return dif, dea, macd_hist

def calc_rsi(close, period=14):
    """计算RSI指标
    返回: RSI值序列
    """
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calc_kdj(high, low, close, n=9, m1=3, m2=3):
    """计算KDJ指标
    返回: (K, D, J)
    """
    lowest_low = low.rolling(window=n, min_periods=1).min()
    highest_high = high.rolling(window=n, min_periods=1).max()
    rsv = (close - lowest_low) / (highest_high - lowest_low).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1/m1, adjust=False).mean()
    d = k.ewm(alpha=1/m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


if _HAS_OBJC:
    class _ClickView(NSView):
        """透明覆盖层，区分点击（切换模式）与拖动（移动窗口）"""

        def initWithFrame_(self, frame):
            self = objc.super(_ClickView, self).initWithFrame_(frame)
            if self is None:
                return None
            self._down = (0, 0)
            self._callback = None
            return self

        def acceptsFirstMouse_(self, event):
            return True

        @objc.typedSelector(b'v@:@')
        def mouseDown_(self, event):
            loc = event.locationInWindow()
            self._down = (loc.x, loc.y)

        @objc.typedSelector(b'v@:@')
        def mouseUp_(self, event):
            loc = event.locationInWindow()
            dx = loc.x - self._down[0]
            dy = loc.y - self._down[1]
            if dx * dx + dy * dy < 25:       # 移动 <5px 视为点击
                if self._callback:
                    self._callback()

        def mouseDragged_(self, event):
            pass                             # 不传递，避免干扰

    class _FWTableViewDelegate(NSObject):
        """表格点击回调代理"""

        def init(self):
            self = objc.super(_FWTableViewDelegate, self).init()
            if self is None:
                return None
            self._callback = None
            return self

        def tableViewSelectionDidChange_(self, notification):
            tv = notification.object()
            row = tv.selectedRow()
            if row >= 0 and self._callback:
                self._callback(row)
            tv.deselectAll_(None)

    class _FWDataSource(NSObject):
        """表格数据源，持有菜单项列表和着色回调"""

        def init(self):
            self = objc.super(_FWDataSource, self).init()
            if self is None:
                return None
            self._items = []
            self._color_fn = None
            return self

        def numberOfRowsInTableView_(self, tv):
            return len(self._items)

        def tableView_objectValueForTableColumn_row_(self, tv, col, row):
            if row < len(self._items):
                return self._items[row].get("label", "")
            return ""

        def tableView_willDisplayCell_forTableColumn_row_(self, tv, cell, col, row):
            if self._color_fn and row < len(self._items):
                self._color_fn(cell, self._items[row])

# 板块等权代理指数每板块取几只成分股。8 只足够反映板块方向，
# 再多就把「每天重建一次」的成本抬到分钟级。
PROXY_CONS_CAP = 8


class FloatingWindow:
    """桌面浮动股票窗口，支持级联菜单导航

    紧凑模式：点击展开菜单
    菜单模式：可点击进入子菜单（板块→子板块），点击返回上级
    """

    WIDTH = 250
    MENU_WIDTH = 320
    PADDING = 14
    CORNER_RADIUS = 12
    ROW_HEIGHT = 28

    def __init__(self, on_close=None, on_mode_change=None):
        self._window = None
        self._text_view = None
        self._close_btn = None
        self._click_view = None
        self._table_view = None
        self._table_delegate = None
        self._visible = False
        self._expanded = False        # False=紧凑  True=菜单模式
        self._on_close = on_close
        self._on_mode_change = on_mode_change
        self._compact_text = ""
        self._compact_colored = None
        # 菜单数据：{section_key: {"title": ..., "items": [...]}}
        self._menu_data = {}
        self._menu_path = []          # 导航路径，如 ["sector", "top"]
        self._menu_items = []         # 当前页面的项目列表
        if _HAS_OBJC:
            self._create_window()

    # ========== 窗口构建 ==========
    def _create_window(self):
        screen = NSScreen.mainScreen()
        frame = screen.visibleFrame()
        w = self.MENU_WIDTH
        x = frame.origin.x + frame.size.width - w - 16
        y = frame.origin.y + frame.size.height - 120

        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, 120),
            NSBorderlessWindowMask,
            NSBackingStoreBuffered,
            False,
        )
        self._window.setLevel_(NSFloatingWindowLevel)
        self._window.setOpaque_(False)
        hud_color = NSColor.colorWithRed_green_blue_alpha_(0.055, 0.065, 0.085, 0.94)
        # 窗口本身保持透明，避免矩形背景溢出圆角内容层。
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setHasShadow_(True)
        self._window.setMovableByWindowBackground_(True)
        self._window.setIgnoresMouseEvents_(False)
        self._window.setTitlebarAppearsTransparent_(True)

        content = self._window.contentView()
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(self.CORNER_RADIUS)
        content.layer().setMasksToBounds_(True)
        try:
            content.layer().setBackgroundColor_(hud_color.CGColor())
            content.layer().setBorderWidth_(1.0)
            content.layer().setBorderColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.45, 0.55, 0.70, 0.32).CGColor()
            )
        except Exception:
            pass

        # 紧凑模式：文本 + 点击遮罩
        self._click_view = _ClickView.alloc().initWithFrame_(
            NSMakeRect(0, 0, w, 120)
        )
        self._click_view._callback = self._on_click

        self._text_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(self.PADDING, self.PADDING,
                       w - 2 * self.PADDING, 96)
        )
        self._text_view.setEditable_(False)
        self._text_view.setSelectable_(False)
        self._text_view.setDrawsBackground_(False)
        self._text_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11.5, 0))
        self._text_view.setTextColor_(NSColor.colorWithWhite_alpha_(0.94, 1.0))
        self._text_view.setTextContainerInset_((5, 5))
        content.addSubview_(self._text_view)
        content.addSubview_(self._click_view)

        # 菜单模式：表格 + 滚动视图（默认隐藏）
        self._table_delegate = _FWTableViewDelegate.alloc().init()
        self._table_delegate._callback = self._on_table_click

        self._data_source = _FWDataSource.alloc().init()
        self._data_source._color_fn = self._apply_cell_style

        self._table_view = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, w, 120))
        self._table_view.setRowHeight_(self.ROW_HEIGHT)
        self._table_view.setIntercellSpacing_((0, 0))
        self._table_view.setSelectionHighlightStyle_(NSTableViewSelectionHighlightStyleNone)
        self._table_view.setDelegate_(self._table_delegate)
        self._table_view.setDataSource_(self._data_source)
        self._table_view.setBackgroundColor_(NSColor.clearColor())

        col = NSTableColumn.alloc().initWithIdentifier_("menu")
        col.setWidth_(w)
        cell = NSTextFieldCell.alloc().initTextCell_("")
        cell.setFont_(NSFont.systemFontOfSize_(13))
        cell.setTextColor_(NSColor.colorWithWhite_alpha_(0.94, 1.0))
        cell.setLineBreakMode_(4)  # NSLineBreakByTruncatingTail
        cell.setDrawsBackground_(False)
        col.setDataCell_(cell)
        self._table_view.addTableColumn_(col)
        header = self._table_view.headerView()
        if header:
            header.setHidden_(True)

        self._scroll_view = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, w, 120))
        self._scroll_view.setDocumentView_(self._table_view)
        self._scroll_view.setHasVerticalScroller_(True)
        self._scroll_view.setDrawsBackground_(False)
        self._scroll_view.setBackgroundColor_(NSColor.clearColor())
        self._scroll_view.setBorderType_(0)  # no border
        self._scroll_view.setAutohidesScrollers_(True)
        self._scroll_view.setHidden_(True)
        content.addSubview_(self._scroll_view)

        # 关闭按钮（NSTextField 视觉 + _ClickView 点击，始终在最上层）
        btn_size = 24
        self._close_btn = NSTextField.alloc().initWithFrame_(
            NSMakeRect(w - btn_size - 6, 0, btn_size, btn_size)
        )
        self._close_btn.setStringValue_("✕")
        self._close_btn.setFont_(NSFont.systemFontOfSize_(13))
        self._close_btn.setTextColor_(NSColor.colorWithWhite_alpha_(0.78, 1.0))
        self._close_btn.setDrawsBackground_(True)
        self._close_btn.setBackgroundColor_(NSColor.colorWithWhite_alpha_(0.22, 0.72))
        self._close_btn.setBordered_(False)
        self._close_btn.setEditable_(False)
        self._close_btn.setSelectable_(False)
        self._close_btn.setAlignment_(NSCenterTextAlignment)
        self._close_btn.setWantsLayer_(True)
        self._close_btn.layer().setCornerRadius_(btn_size / 2)
        self._close_btn.layer().setMasksToBounds_(True)
        content.addSubview_(self._close_btn)

        # 按钮点击层（放在最上面，覆盖按钮区域）
        self._close_click = _ClickView.alloc().initWithFrame_(
            NSMakeRect(w - btn_size - 6, 0, btn_size, btn_size)
        )
        self._close_click._callback = self._on_close_btn
        content.addSubview_(self._close_click)

    # ========== 关闭按钮回调（隐藏窗口） ==========
    def _on_close_btn(self):
        self.hide()
        if self._on_close:
            self._on_close()

    # ========== 紧凑模式点击 ==========
    def _on_click(self):
        self.toggle_mode()

    def toggle_mode(self):
        self._expanded = not self._expanded
        if self._expanded:
            self._enter_menu_mode()
        else:
            self._enter_compact_mode()
        if self._on_mode_change:
            self._on_mode_change()

    # ========== 模式切换 ==========
    def _enter_menu_mode(self):
        """进入菜单模式"""
        self._click_view.setHidden_(True)
        self._text_view.setHidden_(True)
        self._scroll_view.setHidden_(False)
        self._menu_path = []
        self._render_menu()

    def _enter_compact_mode(self):
        """回到紧凑模式"""
        self._scroll_view.setHidden_(True)
        self._click_view.setHidden_(False)
        self._text_view.setHidden_(False)
        self._render_compact()

    # ========== 菜单数据设置 ==========
    def set_menu_data(self, data):
        """设置菜单数据。格式：
        {"section_key": {"title": "显示名", "items": [
            {"name": "名称", "value": "显示值", "color": "red/green/gray/white"},
            ...
        ]}}
        """
        self._menu_data = data
        if self._expanded:
            self._render_menu()

    # ========== 菜单渲染 ==========
    def _render_menu(self):
        """根据当前导航路径渲染菜单页面"""
        if not self._window:
            return
        items = []

        if not self._menu_path:
            # 根页面：列出所有 section
            for key, sec in self._menu_data.items():
                count = len(sec.get("items", []))
                if count > 0:
                    items.append({
                        "type": "nav", "target": key,
                        "label": f"  {sec['title']}  ·  {count}",
                        "color": "blue",
                    })
            if not items:
                items.append({"type": "info", "label": "  暂无可用数据 · 等待刷新", "color": "gray"})
            # 根页面顶部加标题和「收起」回到紧凑模式
            items.insert(0, {"type": "collapse", "label": "  ← 收起浮窗", "color": "gray"})
            items.insert(1, {"type": "header", "label": "  📈 A股实时监控", "color": "header"})
        else:
            # 子页面：显示 section 的 items
            section_key = self._menu_path[0]
            sub_key = self._menu_path[1] if len(self._menu_path) > 1 else None
            sec = self._menu_data.get(section_key, {})
            raw_items = sec.get("items", [])

            if sub_key and isinstance(raw_items, dict):
                raw_items = raw_items.get(sub_key, [])

            for it in raw_items:
                items.append({
                    "type": "info",
                    "label": f"  {it['name']}    {it['value']}",
                    "color": it.get("color", "white"),
                })
            if not items:
                items.append({"type": "info", "label": "  暂无数据 · 等待下一次刷新", "color": "gray"})

        self._menu_items = items

        # 需要在 items 前面加一行「← 返回」（非根页面时）
        display_items = list(items)
        if self._menu_path:
            display_items.insert(0, {
                "type": "back",
                "label": "  ← 返回",
                "color": "white",
            })

        self._data_source._items = display_items
        self._table_view.reloadData()
        self._resize_window(len(display_items) + 1, self.MENU_WIDTH)

    # ========== 单元格着色 ==========
    def _apply_cell_style(self, cell, item):
        colors = {
            "red": NSColor.colorWithRed_green_blue_alpha_(0.98, 0.38, 0.42, 1.0),
            "green": NSColor.colorWithRed_green_blue_alpha_(0.35, 0.88, 0.52, 1.0),
            "gray": NSColor.colorWithWhite_alpha_(0.62, 1.0),
            "white": NSColor.colorWithWhite_alpha_(0.94, 1.0),
            "blue": NSColor.colorWithRed_green_blue_alpha_(0.58, 0.75, 1.0, 1.0),
            "header": NSColor.colorWithRed_green_blue_alpha_(0.70, 0.84, 1.0, 1.0),
        }
        cell.setTextColor_(colors.get(item.get("color", "white"), colors["white"]))
        if item["type"] == "header":
            cell.setFont_(NSFont.boldSystemFontOfSize_(12.5))
        elif item["type"] == "nav":
            cell.setFont_(NSFont.boldSystemFontOfSize_(12.5))
        elif item["type"] in ("back", "collapse"):
            cell.setFont_(NSFont.systemFontOfSize_(11.5))
        else:
            cell.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, 0))

    # ========== 表格点击 → 导航 ==========
    def _on_table_click(self, row):
        items = self._data_source._items
        if row < 0 or row >= len(items):
            return
        item = items[row]
        if item["type"] == "collapse":
            self.toggle_mode()       # 回到紧凑模式
        elif item["type"] == "back":
            self._menu_path = []
            self._render_menu()
        elif item["type"] == "nav":
            self._menu_path = [item["target"]]
            self._render_menu()

    # ========== 公共接口 ==========
    def set_compact(self, text, colored=None):
        """设置紧凑模式内容"""
        self._compact_text = text
        self._compact_colored = colored
        if not self._expanded:
            self._render_compact()

    def update(self, text, colored_lines=None):
        """兼容旧接口"""
        self.set_compact(text, colored_lines)

    @property
    def expanded(self):
        return self._expanded

    @staticmethod
    def _utf16_length(value):
        """返回 NSString/NSAttributedString 使用的 UTF-16 单元长度。"""
        return len(value.encode("utf-16-le")) // 2

    def _render_compact(self):
        text = self._compact_text
        colored_lines = self._compact_colored
        if not self._window or not text:
            return
        try:
            colors = {
                "red": NSColor.colorWithRed_green_blue_alpha_(0.98, 0.38, 0.42, 1.0),
                "green": NSColor.colorWithRed_green_blue_alpha_(0.35, 0.88, 0.52, 1.0),
                "gray": NSColor.colorWithWhite_alpha_(0.62, 1.0),
                "white": NSColor.colorWithWhite_alpha_(0.94, 1.0),
                "header": NSColor.colorWithRed_green_blue_alpha_(0.70, 0.84, 1.0, 1.0),
            }
            attr_str = NSMutableAttributedString.alloc().initWithString_(text)
            full_range = NSMakeRange(0, self._utf16_length(text))
            attr_str.addAttribute_value_range_(
                "NSFont", NSFont.monospacedSystemFontOfSize_weight_(11.5, 0), full_range
            )
            attr_str.addAttribute_value_range_(
                "NSColor", colors["white"], full_range
            )
            if colored_lines:
                search_offset = 0
                for line, cname in colored_lines:
                    idx = text.find(line, search_offset)
                    if idx >= 0:
                        start = self._utf16_length(text[:idx])
                        length = self._utf16_length(line)
                        c = colors.get(cname, NSColor.whiteColor())
                        attr_str.addAttribute_value_range_(
                            "NSColor", c, NSMakeRange(start, length)
                        )
                        search_offset = idx + len(line)
            self._text_view.textStorage().setAttributedString_(attr_str)
            self._resize_window(text.count('\n') + 2, self.WIDTH)
        except Exception:
            pass

    def _resize_window(self, line_count, width):
        """统一调整窗口大小"""
        h = max(88, line_count * self.ROW_HEIGHT + self.PADDING * 2)
        screen_h = NSScreen.mainScreen().visibleFrame().size.height
        h = min(h, screen_h - 80)
        frame = self._window.frame()
        new_x = frame.origin.x + frame.size.width - width
        self._window.setFrame_display_(
            NSMakeRect(new_x, frame.origin.y + frame.size.height - h, width, h), True
        )
        self._text_view.setFrame_(
            NSMakeRect(self.PADDING, self.PADDING,
                       width - 2 * self.PADDING, h - 2 * self.PADDING)
        )
        # 表格内容高度（可能超出窗口，由 scroll view 处理滚动）
        content_h = line_count * self.ROW_HEIGHT
        self._table_view.setFrame_(NSMakeRect(0, 0, width - 14, content_h))
        col = self._table_view.tableColumns()[0] if self._table_view.numberOfColumns() > 0 else None
        if col:
            col.setWidth_(width - 14)
        if self._scroll_view:
            self._scroll_view.setFrame_(NSMakeRect(0, 0, width, h))
        if self._click_view:
            self._click_view.setFrame_(NSMakeRect(0, 0, width, h))
        # 关闭按钮始终在右上角
        btn_size = 24
        btn_x = width - btn_size - 6
        btn_y = h - btn_size - 4
        if self._close_btn:
            self._close_btn.setFrame_(NSMakeRect(btn_x, btn_y, btn_size, btn_size))
        if self._close_click:
            self._close_click.setFrame_(NSMakeRect(btn_x, btn_y, btn_size, btn_size))

    # ========== 显隐 ==========
    def show(self):
        if self._window:
            self._window.orderFront_(None)
            self._visible = True

    def hide(self):
        if self._window:
            self._window.orderOut_(None)
            self._visible = False

    def toggle_visible(self):
        if self._visible:
            self.hide()
        else:
            self.show()

    @property
    def visible(self):
        return self._visible

    def close(self):
        if self._window:
            self._window.close()
            self._window = None


class StockMenuBar(rumps.App):
    _SUMMARY_TIMEOUT = 90  # 盘面总结总超时（秒）

    def __init__(self):
        super(StockMenuBar, self).__init__(
            name="📊 股票监控",
            title="加载中…",
            quit_button=None
        )

        # 配置
        self.update_interval = 15  # 每15秒更新

        # 日志和配置文件路径
        self.log_dir = os.path.dirname(os.path.abspath(__file__))
        self.crash_log = os.path.join(self.log_dir, "crash.log")
        self.debug_log = os.path.join(self.log_dir, "debug.log")
        self.config_file = os.path.join(self.log_dir, "watchlist.json")
        self.portfolio_file = os.path.join(self.log_dir, "portfolio.json")
        self.portfolio_analysis_file = os.path.join(
            self.log_dir, "_portfolio_analysis.txt"
        )
        self.portfolio_analysis_state_file = os.path.join(
            self.log_dir, "portfolio_analysis_state.json"
        )
        self.portfolio_addition_cache_file = os.path.join(
            self.log_dir, "_portfolio_addition_fundamentals.json"
        )
        # 今日报告：菜单上唯一入口「📦 持仓信息」。
        # _portfolio_analysis.txt 仍在写，但不再进菜单 —— 留作排查用的原始 dump。
        self.daily_report_file = os.path.join(self.log_dir, "_portfolio_daily_report.txt")
        self.daily_report_state_file = os.path.join(
            self.log_dir, "daily_report_state.json"
        )
        # 个股→板块反查表（新浪口径）。49 次成分股扫描很贵，缓存到天。
        self.sector_map_file = os.path.join(self.log_dir, "_sector_map.json")
        self.sector_tech_file = os.path.join(self.log_dir, "_sector_tech_cache.json")
        self.indicator_refresh_state_file = os.path.join(
            self.log_dir, "indicator_refresh_state.json"
        )
        # 三个盘中事件 tracker（下探回升/封板/点火）的抑制状态：重启防重报。
        self.tracker_state_file = os.path.join(
            self.log_dir, "intraday_trackers_state.json"
        )
        self.five_one_three_state_file = os.path.join(
            self.log_dir, "five_one_three_state.json"
        )
        self.screen_results_state_file = os.path.join(
            self.log_dir, "screen_results_state.json"
        )
        self.observation_pool_file = os.path.join(
            self.log_dir, "screen_observation_state.json"
        )
        self._instance_lock_handle = self._acquire_instance_lock()
        self._log_lock = threading.Lock()

        # 设置异常处理
        sys.excepthook = self.handle_exception

        # 默认股票列表
        self.default_watchlist = [
            {"code": "sh000001", "name": "上证", "show_in_bar": True},
            {"code": "sz399006", "name": "创业", "show_in_bar": False},
            {"code": "sh000688", "name": "科创", "show_in_bar": False},
            {"code": "sh600522", "name": "中天", "show_in_bar": True},
            {"code": "sh600884", "name": "杉杉", "show_in_bar": True},
        ]

        # 加载股票列表（从文件或使用默认）
        self.watchlist = self.load_watchlist()

        # 板块监控配置
        self.show_sector = True  # 是否显示板块
        self.sector_count = 5    # 显示前N个热门板块
        self.sector_update_count = 59  # 行业板块更新计数器（首次立即获取，之后每60次=15分钟）

        # 数据存储
        self.stock_data = {}
        self.market_turnover_data = {}
        self.market_turnover = {
            "today": 0,
            "previous": None,
            "date": None,
        }
        self.turnover_tracker = TurnoverTracker(
            os.path.join(self.log_dir, "_turnover_tracker.json")
        )
        self.market_cycle_state_store = MarketCycleStateStore(
            os.path.join(self.log_dir, "_market_cycle_state.json")
        )
        self._turnover_snapshot = None
        self._prev_curve: Optional[TurnoverCurve] = None
        self._prev_curve_date: str | None = None
        self._intraday_tactics = IntradayTactics()
        self._last_portfolio_results = []
        self._t_filter_events = {}
        self._signal_confidence = SignalConfidence()
        # 板块龙头：封板/炸板分档，用来抑制或恢复冲高做T（见 sector_leaders.py）
        self.sector_leaders_file = os.path.join(self.log_dir, "sector_leaders.json")
        self._leader_store = SectorLeaderStore(self.sector_leaders_file)
        self._leader_store.load()
        # 持仓股→板块手动归属：反查表（_sector_map）归属不准时以它为准
        self.holding_sectors_file = os.path.join(self.log_dir, "holding_sectors.json")
        self._holding_sector_store = HoldingSectorStore(self.holding_sectors_file)
        self._holding_sector_store.load()
        self._leader_guard = LeaderGuard()
        self._leader_auto_checked = None   # 当日已做候选推荐的日期，避免反复扫板块
        self._leader_hold_cache = {}       # code -> (leader_hold, leader_note)，每轮刷新
        self._leader_quote_cache = {}      # code -> 行情行（涨跌幅进菜单）
        self._leader_menu_item_refs = {}   # (板块, code) -> MenuItem，盘中只改标题不重建
        self._leader_menu_tick = 0         # 菜单涨跌幅刷新节流（每 4 轮 ≈ 1 分钟）
        self.sector_data = []  # 板块数据
        self.concept_data = []  # 概念板块数据
        self.concept_update_count = 59  # 概念板块更新计数器（首次立即获取，之后每60次=15分钟）
        self.indicator_data = []  # 指标选股结果
        self.relaxed_indicator_data = []  # 宽松预警结果
        self.strong_candidate_data = []  # 强势突破延续观察结果
        self.five_one_three_data = []  # 513低位形态结果
        # 指标两档 / 趋势+513 两组各自的「上次真正刷新」时间（保质期判定用）
        self._indicator_group_at = None
        self._pattern_group_at = None
        self.five_one_three_stats = {  # 513全市场漏斗统计（与Android版FiveOneThreeStats一致）
            'total_stocks': 0,
            'history_available': 0,
            'historical_five_bullish': 0,
            'recent_five_bullish': 0,
            'waiting_confirmation': 0,
            'confirmed': 0,
        }
        # 513全市场深扫日期（每天一次）；落盘保存，重启/崩溃自拉起后不重复深扫
        self._five_one_three_scan_date = self._load_five_one_three_scan_date()
        # 选股结果落盘：盘中不自动扫描后，重启靠它恢复（超保质期的组不回填）
        self._load_screen_results()
        self.observation_pool = ObservationPool(self.observation_pool_file)
        self._observation_refresh_lock = threading.Lock()
        self._observation_next_refresh_at = None
        self._observation_last_error = self.observation_pool.load_error
        self._observation_entry_lock = threading.Lock()
        self._observation_entry_tracker = ObservationEntryTracker()
        self._observation_entry_next_at = None
        self._observation_entry_context_date = None
        self._observation_entry_histories = {}
        self._observation_entry_benchmark = []
        self._observation_entry_last_error = None
        self._init_entry_review()
        self.indicator_results_stale = False
        self.indicator_results_stale_at = None
        self.indicator_refresh_scheduler = IndicatorRefreshScheduler(
            self.indicator_refresh_state_file
        )
        self.indicator_screen_lock = threading.Lock()
        self._last_intraday_indicator_slot = None

        # ── 大盘指数预警系统（优化版） ──
        self.market_alert_enabled = True
        self.market_alert_indices = ['sh000001', 'sz399001', 'sz399006']  # 上证、深证、创业板

        # 初始化预警引擎（采用 Android 优化建议）
        self.market_alert_engine = MarketAlertEngine({
            # 绝对跌幅阈值（分级）
            'absolute_thresholds': {
                AlertLevel.WARNING: -0.8,   # 转弱
                AlertLevel.DANGER: -1.2,     # 风险
                AlertLevel.CRASH: -2.5,      # 严重风险
            },
            # 速度预警阈值（5分钟窗口）
            'speed_thresholds': {
                AlertLevel.WARNING: -0.5,
                AlertLevel.DANGER: -0.8,
                AlertLevel.CRASH: -1.2,
            },
            'speed_window': 300,  # 5分钟

            # 量能阈值（相对同时段基线）
            'volume_thresholds': {
                1.5: '放量',
                2.0: '巨量',
                3.0: '天量',
            },

            # 反弹（弱势反抽）阈值
            'recovery_threshold': 0.5,
            'recovery_min_drop': -0.8,

            # 多指数共振条件
            'correlation_conditions': {
                'min_falling_count': 2,
                'min_falling_pct': -0.8,
                'min_avg_pct': -0.7,
                'severe_single_pct': -1.2,
            },

            # 重新武装阈值
            'rearm_threshold': -0.6,

            # 确认轮次
            'confirm_rounds': 2,
        })
        self._last_intraday_indicator_at = None
        self._technical_analysis_lock = threading.Lock()
        self._technical_analysis_menu_item = None
        self.portfolio = self._load_portfolio()
        self.trade_ledger_file = os.path.join(self.log_dir, "trade_ledger.json")
        try:
            self._trade_ledger = TradeLedger(self.trade_ledger_file)
            self._trade_ledger.sync_legacy_positions(self.portfolio.get("positions", []))
        except ValueError as error:
            self._trade_ledger = TradeLedger()
            self._trade_ledger_error = str(error)
            self.log(f"交易批次账本暂不可用: {error}", "WARNING")
        self._intraday_bar_fetcher = fetch_intraday_bars
        self._portfolio_analysis_lock = threading.Lock()
        self._last_portfolio_analysis_slot = self._load_portfolio_state()
        self._last_addition_assessment = {
            "recommendation": None,
            "evaluations": (),
        }
        self.addition_service = PortfolioAdditionService(
            history_fetcher=self.get_stock_analysis_history,
            store=PortfolioAdditionStore(self.portfolio_addition_cache_file),
        )
        self._portfolio_signals = {}
        # 今日报告：内存缓存供菜单渲染（后台线程只写它，UI 只在主线程读）
        self._daily_report_state = self._load_daily_report_state()
        today_key = datetime.now().strftime("%Y-%m-%d")
        saved_events = self._daily_report_state.get("t_filter_events", [])
        self._t_filter_events = {
            event.get("code"): event for event in saved_events
            if isinstance(event, dict) and event.get("date") == today_key and event.get("code")
        }
        self._last_daily_report = None
        self._last_finalize_attempt_at = None
        self._sector_quotes = {}
        self._sector_constituents = {}
        self._sector_map = self._load_sector_map()
        self._sector_map_refreshing = False
        self._sector_tech = self._load_sector_tech()
        self._sector_tech_refreshing = False
        self._capital_flow_file = os.path.join(self.log_dir, "_capital_flow_cache.json")
        self._capital_flow_cache = FlowCache(max_age_seconds=150)
        self._capital_flow_stock_tracker = FlowDirectionTracker(required_count=2)
        self._capital_flow_sector_tracker = FlowDirectionTracker(required_count=2)
        self._capital_flow_lock = threading.Lock()
        self._capital_flow_refresh_lock = threading.Lock()
        self._capital_flow_refreshing = False
        self._capital_flow_next_refresh_monotonic = 0.0
        self._capital_flow_refresh_interval_seconds = 60
        self._capital_flow_fail_backoff_seconds = 120
        self._capital_flow_last_error = None
        self._capital_flow_concept_map = {}
        self._capital_flow_concept_map_names = set()
        self._capital_flow_concept_map_refreshing = False
        self._fetch_lock = threading.Lock()
        self._summary_lock = threading.Lock()
        self._summary_running = False
        self._summary_auto_date = None
        self._summary_afternoon_date = None
        self.indicator_last_error = None
        self._summary_text = None
        self._last_summary_date = None  # 上次生成盘面总结的日期
        self._last_summary_night = None # 夜间窗口是否已生成
        self._summary_timer = None      # 计时器
        self._summary_elapsed = 0       # 已用秒数
        self._concept_summary_cache = None
        self._summary_cancel = threading.Event()  # 取消信号
        self._limit_stocks = {'up': [], 'down': []}  # 涨跌停缓存
        self._kline_fail_cache = None   # K线失败缓存（5分钟内不重试）
        self._kline_fail_time = None

        # 浮动窗口
        self.floating_window = FloatingWindow(
            on_close=self._on_floating_closed,
            on_mode_change=self._on_floating_mode_change,
        )
        self.floating_window.show()

        self.indicator_menu = rumps.MenuItem("📈 指标策略", callback=None)
        self.indicator_menu.add(rumps.MenuItem("⏳ 加载指标策略…", callback=None))

        self.limit_menu = rumps.MenuItem("📊 涨跌停", callback=None)
        self.limit_menu.add(rumps.MenuItem("⏳ 加载中…", callback=None))

        self.top_sector_menu = rumps.MenuItem("🔥 涨幅榜", callback=None)
        self.top_sector_menu.add(rumps.MenuItem("⏳ 加载中…", callback=None))

        self.bottom_sector_menu = rumps.MenuItem("💧 跌幅榜", callback=None)
        self.bottom_sector_menu.add(rumps.MenuItem("⏳ 加载中…", callback=None))

        self.concept_menu = rumps.MenuItem("🤖 概念板块", callback=None)
        self.concept_menu.add(rumps.MenuItem("⏳ 加载中…", callback=None))

        self._market_overview_menu_item = rumps.MenuItem(
            "📊 市场概览", callback=None
        )
        self._summary_menu_item = rumps.MenuItem(
            "📋 盘面总结", callback=self.show_market_summary
        )
        self._technical_analysis_menu_item = rumps.MenuItem(
            "🔍 个股分析", callback=self.show_stock_technical_analysis
        )
        self._fundamentals_menu_item = rumps.MenuItem(
            "📊 基本面分析", callback=self.show_fundamentals_analysis
        )
        self._global_menu_item = rumps.MenuItem(
            "🌎 港美股分析", callback=self.show_global_analysis
        )
        self._ai_menu_item = rumps.MenuItem(
            "🤖 AI 分析", callback=self.show_ai_analysis
        )
        # 「📦 持仓信息」：父项是容器，点了不触发 callback（带子菜单的 NSMenuItem
        # 行为，项目里 ⚙️ 持仓设置 同款 callback=None）。打开全文挂在第一个子项上。
        self._portfolio_info_menu_item = rumps.MenuItem(
            "📦 持仓信息", callback=None
        )
        self._portfolio_info_menu_item.add(
            rumps.MenuItem("📄 打开完整报告", callback=self.show_daily_report)
        )
        self._portfolio_settings_menu_item = rumps.MenuItem(
            "⚙️ 持仓设置", callback=None
        )
        self._portfolio_settings_menu_item.add(
            rumps.MenuItem("➕ 追加监控股票", callback=self.show_portfolio_add)
        )
        self._portfolio_settings_menu_item.add(
            rumps.MenuItem("➖ 删除监控股票", callback=self.show_portfolio_delete)
        )
        self._portfolio_update_menu_item = rumps.MenuItem(
            "✏️ 修改数量/成本", callback=None
        )
        self._portfolio_settings_menu_item.add(self._portfolio_update_menu_item)
        self._portfolio_settings_menu_item.add(
            rumps.MenuItem("💰 设置总资金", callback=self.show_portfolio_capital_settings)
        )
        self._portfolio_settings_menu_item.add(
            rumps.MenuItem("🧾 交易记录（T+1）", callback=self.show_trade_ledger)
        )
        self._refresh_portfolio_update_menu()
        self._trade_calculator_menu_item = rumps.MenuItem(
            "💰 买卖成本计算", callback=self.show_trade_calculator
        )
        self._target_cost_menu_item = rumps.MenuItem(
            "🎯 目标成本策略", callback=self.show_target_cost_strategy
        )
        # 板块龙头：显示每个持仓板块的龙头与关联持仓，可手动增删
        self._sector_leader_menu_item = rumps.MenuItem("📊 板块龙头", callback=None)
        self._refresh_sector_leader_menu()

        # 设置菜单
        self._settings_menu = rumps.MenuItem("⚙️ 设置", callback=None)
        self._settings_menu.add(
            rumps.MenuItem("🔔 测试通知", callback=self.test_notification)
        )
        self._settings_menu.add(
            rumps.MenuItem("📊 测试盘中信号", callback=self.test_intraday_signals)
        )

        # 浮动窗口菜单项
        self._float_menu_item = rumps.MenuItem("📌 浮动窗口", callback=self._toggle_floating)

        # 创建菜单
        self.menu = [
            self._market_overview_menu_item,
            None,  # 分隔线
            self.top_sector_menu,
            self.bottom_sector_menu,
            self.concept_menu,
            self.limit_menu,
            self._summary_menu_item,
            self.indicator_menu,
            self._technical_analysis_menu_item,
            self._fundamentals_menu_item,
            self._global_menu_item,
            self._ai_menu_item,
            self._portfolio_info_menu_item,
            self._portfolio_settings_menu_item,
            self._trade_calculator_menu_item,
            self._target_cost_menu_item,
            self._sector_leader_menu_item,
            self._settings_menu,
            None,  # 分隔线
            self._float_menu_item,
            rumps.MenuItem("➕ 添加股票", callback=self.add_stock_dialog),
            rumps.MenuItem("➖ 删除股票", callback=self.delete_stock_dialog),
            rumps.MenuItem("🔄 立即刷新", callback=self.refresh_now),
            None,  # 分隔线
            rumps.MenuItem("❌ 退出", callback=self.quit_app),
        ]

        # 启动定时更新
        self.start_update_timer()

    def _acquire_instance_lock(self):
        """获取菜单栏单实例锁，避免 launchd 和手动启动同时运行。"""
        lock_path = os.path.join(self.log_dir, ".stock_menubar.lock")
        lock_handle = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_handle.close()
            raise RuntimeError("股票菜单栏已经在运行，跳过重复启动") from exc
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(str(os.getpid()))
        lock_handle.flush()
        return lock_handle

    @staticmethod
    def _focus_app():
        """将应用拉到前台，确保弹窗不会被遮挡"""
        if not _HAS_OBJC:
            return
        try:
            app = _get_ns_app()
            if app is None:
                return
            app.setActivationPolicy_(0)   # NSApplicationActivationPolicyRegular
            app.activateIgnoringOtherApps_(True)
        except Exception:
            pass

    def _request_dock_attention(self, critical=False):
        """让 Dock 图标跳动；普通提醒短跳，危险提醒持续跳。"""
        if not _HAS_OBJC:
            return

        def _request():
            import AppKit
            app = _get_ns_app()
            if app is None:
                return
            # rumps 默认是无 Dock 图标的 accessory app。这里只恢复 regular
            # policy 让图标存在，不抢占用户当前焦点。
            app.setActivationPolicy_(0)
            request_type = (
                AppKit.NSCriticalRequest
                if critical
                else AppKit.NSInformationalRequest
            )
            app.requestUserAttention_(request_type)

        self._run_on_main_once(_request, tag="Dock图标弹跳")

    @staticmethod
    def _fmt_pct(val):
        """格式化涨跌幅：正数带+号，负数不重复，右对齐8字符"""
        s = f"+{val:.2f}%" if val > 0 else f"{val:.2f}%"
        return f"{s:>8}"

    @staticmethod
    def _safe_float(value, default=0.0):
        """把任意来源的数值字段收敛成 float。

        行情/成交额字段随时段和数据源可能给出 None 或空串，直接参与
        比较或格式化会抛 TypeError，把整次菜单刷新打断。
        """
        try:
            if value is None:
                return default
            result = float(value)
            if result != result:  # NaN
                return default
            return result
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _format_price(price):
        """价格显示：百元以上取整，以下保留两位。"""
        price = StockMenuBar._safe_float(price)
        return f"{price:.0f}" if price >= 100 else f"{price:.2f}"

    @staticmethod
    def _format_bar_quote(name, price, pct):
        """生成紧凑的菜单栏行情标签，不使用涨跌箭头图标。"""
        pct = StockMenuBar._safe_float(pct)
        return f"{name}{StockMenuBar._format_price(price)} {pct:+.1f}%"

    @staticmethod
    def _clip_alert_message(message, limit=300):
        """弹窗正文统一转字符串并截断。

        正文过长时 NSAlert 会被撑到比屏幕还高，OK 按钮顶出屏幕外点不到，
        弹窗就再也关不掉（2026-09-28 基本面分析传入 tuple 报告时踩过）。
        """
        text = message if isinstance(message, str) else str(message)
        return text if len(text) <= limit else text[:limit] + "…"

    def _alert(self, title, message):
        """弹窗（自动聚焦）。必须在主线程创建，后台线程建的 NSAlert 不响应点击。"""
        clipped = self._clip_alert_message(message)

        def _show():
            self._focus_app()
            rumps.alert(title=str(title), message=clipped)

        if threading.current_thread() is threading.main_thread():
            _show()
        else:
            self._dispatch_to_main(_show)

    def _window(self, **kwargs):
        """输入窗口（自动聚焦）"""
        self._focus_app()
        return rumps.Window(**kwargs)

    @staticmethod
    def _rotate_log_file(path, max_bytes=5 * 1024 * 1024):
        """日志超过上限时保留一个旧文件，避免长期运行无限增长。"""
        try:
            if os.path.getsize(path) > max_bytes:
                backup_path = f"{path}.1"
                if os.path.exists(backup_path):
                    os.unlink(backup_path)
                os.replace(path, backup_path)
        except OSError:
            pass

    def log(self, message, level="INFO"):
        """记录日志，并限制单个调试日志文件大小。"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[{timestamp}] [{level}] {message}\n"

        try:
            with self._log_lock:
                self._rotate_log_file(self.debug_log)
                with open(self.debug_log, "a", encoding="utf-8") as f:
                    f.write(log_message)
        except Exception:
            pass

    def load_watchlist(self):
        """从文件加载股票列表"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and len(data) > 0:
                        self.log(f"从配置文件加载 {len(data)} 只股票", "INFO")
                        return data
        except Exception as e:
            self.log(f"加载配置文件失败: {e}", "ERROR")
        # 使用默认列表
        self.log("使用默认股票列表", "INFO")
        return self.default_watchlist.copy()

    def save_watchlist(self):
        """原子保存股票列表，避免进程中断留下损坏的 JSON。"""
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.log_dir,
                prefix=".watchlist.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(self.watchlist, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.config_file)
            self.log(f"股票列表已保存，共 {len(self.watchlist)} 只", "INFO")
        except Exception as e:
            self.log(f"保存配置文件失败: {e}", "ERROR")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def handle_exception(self, exc_type, exc_value, exc_traceback):
        """处理未捕获的异常"""
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))

        # 写入崩溃日志
        try:
            with self._log_lock:
                self._rotate_log_file(self.crash_log)
                with open(self.crash_log, "a", encoding="utf-8") as f:
                    f.write("=" * 50 + "\n")
                    f.write(f"[{datetime.now()}] 未捕获的异常\n")
                    f.write(error_msg)
                    f.write("=" * 50 + "\n\n")
        except Exception:
            pass

        # 也写入调试日志
        self.log(f"未捕获的异常: {exc_value}", "ERROR")
        self.log(error_msg, "ERROR")

    def get_stock_data(self, code):
        """获取股票数据"""
        try:
            url = f"https://hq.sinajs.cn/list={code}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
                'Referer': 'https://finance.sina.com.cn'
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                data = response.read().decode('gbk')
                match = re.search(r'"([^"]+)"', data)
                if match:
                    parts = match.group(1).split(',')
                    if len(parts) >= 32:
                        price = float(parts[3])
                        prev_close = float(parts[2])
                        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
                        open_price = float(parts[1]) if parts[1] else price
                        high_price = float(parts[4]) if parts[4] else price
                        low_price = float(parts[5]) if parts[5] else price
                        volume = float(parts[8]) if parts[8] else 0    # 成交量（股）
                        turnover = float(parts[9]) if parts[9] else 0  # 成交额（元）
                        return {
                            'name': parts[0],
                            'price': price,
                            'change_pct': change_pct,
                            'open': open_price,
                            'prev_close': prev_close,
                            'high': high_price,
                            'low': low_price,
                            'volume': volume,
                            'turnover': turnover,
                            # 盘口：封死涨停看卖一是否为空（报文里本来就有，只是没解析）
                            'ask1_price': float(parts[7]) if parts[7] else 0.0,
                            'ask1_vol': float(parts[20]) if len(parts) > 20 and parts[20] else 0.0,
                            'bid1_price': float(parts[11]) if len(parts) > 11 and parts[11] else 0.0,
                            'bid1_vol': float(parts[10]) if len(parts) > 10 and parts[10] else 0.0,
                            'date': parts[30] if len(parts) > 30 else '',
                            'time': parts[31] if len(parts) > 31 else '',
                        }
        except Exception as e:
            self.log(f"获取 {code} 失败: {e}", "ERROR")
        return None

    def search_stock(self, keyword):
        """搜索股票（支持名称、拼音首字母）"""
        try:
            encoded_keyword = urllib.parse.quote(keyword)
            url = f"https://suggest3.sinajs.cn/suggest/type=11,12&key={encoded_keyword}"
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.sina.com.cn'
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                data = response.read().decode('gbk')

                # 解析数据
                # 格式: name,type,code,market_code,...
                # 示例: 飞龙股份,11,002536,sz002536,...
                match = data.split('"')[1] if '"' in data else ""
                if match:
                    results = []
                    items = match.split(';')
                    for item in items:
                        parts = item.split(',')
                        if len(parts) >= 4 and parts[0] and parts[2]:
                            # parts[0] = name, parts[2] = code
                            # parts[3] = market+code (如 "sz002536")
                            name = parts[0]
                            code = parts[2]
                            # 从 parts[3] 获取正确的市场前缀
                            market_code = parts[3] if len(parts[3]) >= 8 else ""
                            if market_code.startswith(('sh', 'sz')):
                                full_code = market_code[:2] + code
                            else:
                                # 备用方案：根据type判断
                                market = "sh" if parts[1] == "11" else "sz"
                                full_code = f"{market}{code}"
                            results.append({
                                'code': full_code,
                                'name': name
                            })
                    return results
        except Exception as e:
            self.log(f"搜索股票失败: {e}", "ERROR")
        return []

    def get_sector_data(self):
        """获取热门板块数据（涨幅前N和跌幅前N）"""
        result = {'top': [], 'bottom': []}

        # 排除的板块
        exclude_sectors = ['次新股']
        # 最小成分股数量（大盘块）
        min_stocks = 30

        try:
            url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Referer': 'https://finance.sina.com.cn'
            }

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                data = response.read().decode('gbk')

                # 解析板块数据
                items = re.findall(r'"([^"]+)":"([^"]+)"', data)
                sectors = []

                for key, value in items:
                    parts = value.split(',')
                    if len(parts) >= 13:
                        name = parts[1]
                        stock_count = int(parts[2]) if parts[2] else 0

                        # 排除指定板块和小盘块
                        if name in exclude_sectors:
                            continue
                        if stock_count < min_stocks:
                            continue

                        change = float(parts[5]) if parts[5] else 0      # 涨跌幅(%)
                        volume = int(parts[7]) if parts[7] else 0        # 成交额(元)
                        lead_name = parts[12]                             # 领涨股名称
                        lead_pct = parts[9]                               # 领涨股涨跌幅
                        sectors.append({
                            'name': name,
                            'change': change,
                            'count': stock_count,
                            'volume': volume,
                            'lead': lead_name,
                            'lead_pct': lead_pct,
                        })

                # 按涨幅排序
                sectors.sort(key=lambda x: x['change'], reverse=True)

                # 涨幅榜只展示真实上涨板块
                rising_sectors = [s for s in sectors if s['change'] > 0]
                falling_sectors = [s for s in sectors if s['change'] < 0]
                result['top'] = rising_sectors[:self.sector_count]

                # 跌幅榜只展示真实下跌板块，按跌幅从大到小排序
                result['bottom'] = sorted(
                    falling_sectors,
                    key=lambda x: x['change'],
                )[:self.sector_count]

                # 全部板块数据（用于统计涨跌家数）
                result['_all'] = sectors

                self.log(f"板块数据获取成功: 共{len(sectors)}个大盘块（成分股>={min_stocks}）")

        except Exception as e:
            self.log(f"获取板块数据失败: {e}", "ERROR")
        return result

    def get_concept_data(self):
        """获取概念板块数据（使用东方财富API）"""
        result = {'top': [], 'bottom': [], '_fresh': False}

        try:
            import requests

            # 创建session
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Accept': '*/*',
                'Accept-Language': 'zh-CN,zh;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
            })

            # 先访问主页获取cookies
            session.get("https://data.eastmoney.com/", timeout=10)

            # 访问概念板块API
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "cb": "jQuery",
                "pn": "1",
                "pz": "200",
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": "m:90+t:3",
                "fields": "f2,f3,f12,f14",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "_": str(int(datetime.now().timestamp() * 1000))
            }

            session.headers.update({
                'Referer': 'https://data.eastmoney.com/',
                'Sec-Fetch-Dest': 'script',
                'Sec-Fetch-Mode': 'no-cors',
                'Sec-Fetch-Site': 'same-site',
            })

            resp = session.get(url, params=params, timeout=15)

            # 解析JSONP
            text = resp.text
            if text.startswith('jQuery'):
                json_start = text.index('(') + 1
                json_end = text.rindex(')')
                json_str = text[json_start:json_end]
                json_data = json.loads(json_str)

                if json_data.get('data') and json_data['data'].get('diff'):
                    all_sectors = json_data['data']['diff']

                    # 筛选关注的概念板块
                    concept_data = []
                    for item in all_sectors:
                        name = item.get('f14', '')
                        # 检查是否包含关注的关键词
                        for kw in CONCEPT_KEYWORDS:
                            if kw in name:
                                concept_data.append({
                                    'name': name,
                                    'code': item.get('f12', ''),
                                    'change': item.get('f3', 0)
                                })
                                break

                    # 按涨跌幅排序
                    concept_data.sort(key=lambda x: x['change'], reverse=True)

                    # 涨幅榜
                    result['top'] = concept_data[:self.sector_count]

                    # 跌幅榜
                    result['bottom'] = concept_data[-self.sector_count:]
                    result['_fresh'] = True

                    self.log(f"概念板块数据获取成功: 共{len(concept_data)}个板块")

        except Exception as e:
            self.log(f"获取概念板块数据失败: {e}", "ERROR")
        return result


    def _fetch_with_retry(self, fetch_fn, retries=3, delay=1, label="数据", cancel_event=None):
        """通用重试封装：失败后等待 delay 秒再重试
        cancel_event: 可选的 threading.Event，等待期间若被 set 则立即放弃
        """
        import time
        last_err = None
        for attempt in range(retries):
            try:
                result = fetch_fn()
                if result:  # 非空即视为成功
                    return result
                last_err = "返回空数据"
            except Exception as e:
                last_err = str(e)
                self.log(f"{label} 第{attempt+1}次尝试失败: {e}", "WARNING")
            if attempt < retries - 1:
                if cancel_event:
                    if cancel_event.wait(delay):
                        self.log(f"{label} 被取消", "INFO")
                        return None
                else:
                    time.sleep(delay)
        self.log(f"{label} 全部{retries}次尝试均失败: {last_err}", "ERROR")
        return None

    def _fetch_kline_once(self):
        """单次获取K线数据（内部方法，失败时抛异常）"""
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Connection': 'keep-alive',
        })
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": "1.000001",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": "101", "fqt": "1", "end": "20500101", "lmt": "5",
        }
        session.headers.update({
            'Referer': 'https://data.eastmoney.com/',
            'Sec-Fetch-Dest': 'script',
            'Sec-Fetch-Mode': 'no-cors',
            'Sec-Fetch-Site': 'same-site',
        })
        resp = session.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        klines = data.get('data', {}).get('klines', [])
        if not klines:
            raise ValueError("K线数据为空")
        result = []
        for line in klines:
            parts = line.split(',')
            if len(parts) >= 7:
                result.append({
                    'date': parts[0],
                    'open': float(parts[1]),
                    'close': float(parts[2]),
                    'high': float(parts[3]),
                    'low': float(parts[4]),
                    'amount': float(parts[6]),
                })
        return result

    def _fetch_kline_backup(self):
        """备用策略：通过东方财富日内行情API获取近5日K线（跳过cookie预热）"""
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": "1.000001",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": "101", "fqt": "1", "end": "20500101", "lmt": "5",
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Referer': 'https://quote.eastmoney.com/',
        }
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        klines = data.get('data', {}).get('klines', [])
        if not klines:
            raise ValueError("K线数据为空")
        result = []
        for line in klines:
            parts = line.split(',')
            if len(parts) >= 7:
                result.append({
                    'date': parts[0],
                    'open': float(parts[1]),
                    'close': float(parts[2]),
                    'high': float(parts[3]),
                    'low': float(parts[4]),
                    'amount': float(parts[6]),
                })
        self.log(f"[备用] 东方财富K线获取成功: {len(result)}条", "INFO")
        return result

    def _fetch_kline_sina(self):
        """使用新浪日线接口获取沪指近5个交易日OHLCV。"""
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        response = requests.get(
            url,
            params={"symbol": "sh000001", "scale": 240, "ma": "no", "datalen": 5},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
            timeout=8,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise ValueError("新浪K线数据为空")
        result = []
        for row in rows:
            result.append({
                "date": row["day"],
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": float(row.get("volume", 0) or 0),
                "amount": None,
            })
        self.log(f"[新浪] 沪指K线获取成功: {len(result)}条", "INFO")
        return result

    def get_kline_data(self):
        """获取近5个交易日K线；优先新浪，东方财富作为备用。"""
        now = datetime.now()
        # 失败后5分钟内不再重试，直接返回缓存
        if (self._kline_fail_cache and self._kline_fail_time
                and (now - self._kline_fail_time).total_seconds() < 300):
            self.log("K线数据使用失败缓存（5分钟内不重试）", "INFO")
            return self._kline_fail_cache

        try:
            result = self._fetch_kline_sina()
            if result:
                self._kline_fail_cache = None
                self._kline_fail_time = None
                return result
        except Exception as error:
            self.log(f"新浪K线失败，尝试东方财富备用接口: {error}", "WARNING")

        # 备用策略：东方财富主接口
        result = self._fetch_with_retry(self._fetch_kline_once, retries=2, delay=1,
                                        label="东方财富K线", cancel_event=self._summary_cancel)
        if result:
            self._kline_fail_cache = None
            self._kline_fail_time = None
            self.log(f"东方财富K线获取成功: {len(result)}条", "INFO")
            return result
        try:
            result = self._fetch_kline_backup()
            if result:
                self._kline_fail_cache = None
                self._kline_fail_time = None
                return result
        except Exception as error:
            self.log(f"东方财富备用K线也失败: {error}", "ERROR")
        self._kline_fail_cache = []
        self._kline_fail_time = now
        return []

    def _fetch_concept_summary_once(self):
        """单次获取概念板块完整数据（内部方法）"""
        url = "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Referer': 'https://finance.sina.com.cn'
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            raw = response.read().decode('gbk', errors='replace')
            match = re.search(r'=\s*(\{.*\})', raw)
            if not match:
                raise ValueError("概念板块数据解析失败")
            data = json.loads(match.group(1))
            sectors = []
            rising = falling = 0
            for key, val in data.items():
                parts = val.split(',')
                if len(parts) < 13:
                    continue
                name = parts[1]
                change = float(parts[5]) if parts[5] else 0
                volume = int(parts[7]) if parts[7] else 0
                lead = parts[12]
                lead_pct = parts[9]
                sectors.append({
                    'name': name, 'change': change, 'volume': volume,
                    'lead': lead, 'lead_pct': lead_pct,
                })
                if change > 0:
                    rising += 1
                elif change < 0:
                    falling += 1
            if not sectors:
                raise ValueError("概念板块数据为空")
            sectors.sort(key=lambda x: x['change'], reverse=True)
            return {'sectors': sectors, 'rising': rising, 'falling': falling}

    def _fetch_concept_summary_backup(self):
        """备用策略：从东方财富获取概念板块数据（与主策略返回格式一致）"""
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Referer': 'https://data.eastmoney.com/',
            'Sec-Fetch-Dest': 'script',
            'Sec-Fetch-Mode': 'no-cors',
            'Sec-Fetch-Site': 'same-site',
        })
        session.get("https://data.eastmoney.com/", timeout=5)
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "cb": "jQuery", "pn": "1", "pz": "200", "po": "1", "np": "1",
            "fltt": "2", "invt": "2", "fid": "f3", "fs": "m:90+t:3",
            "fields": "f2,f3,f12,f14",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "_": str(int(datetime.now().timestamp() * 1000)),
        }
        resp = session.get(url, params=params, timeout=10)
        text = resp.text
        if text.startswith('jQuery'):
            json_start = text.index('(') + 1
            json_end = text.rindex(')')
            json_data = json.loads(text[json_start:json_end])
        else:
            json_data = resp.json()
        items = json_data.get('data', {}).get('diff', [])
        if not items:
            raise ValueError("东方财富概念板块数据为空")
        sectors = []
        rising = falling = 0
        for item in items:
            change = item.get('f3', 0) or 0
            name = item.get('f14', '')
            code = item.get('f12', '')
            sectors.append({
                'name': name, 'change': float(change), 'volume': 0,
                'lead': code, 'lead_pct': str(change),
            })
            if change > 0:
                rising += 1
            elif change < 0:
                falling += 1
        sectors.sort(key=lambda x: x['change'], reverse=True)
        self.log(f"[备用] 东方财富概念板块获取成功: 共{len(sectors)}个，涨{rising}跌{falling}", "INFO")
        return {'sectors': sectors, 'rising': rising, 'falling': falling}

    def get_concept_summary_data(self):
        """获取概念板块完整数据（涨跌幅排名、涨跌家数），主策略+备用"""
        # 主策略：Sina
        result = self._fetch_with_retry(
            self._fetch_concept_summary_once, retries=2, delay=1,
            label="概念板块", cancel_event=self._summary_cancel
        )
        if result:
            self._concept_summary_cache = result
            self.log(f"概念板块数据获取成功: 共{len(result['sectors'])}个，"
                     f"涨{result['rising']}跌{result['falling']}")
            return result
        # 备用策略：东方财富
        self.log("Sina概念板块失败，尝试备用策略（东方财富）", "WARNING")
        try:
            result = self._fetch_concept_summary_backup()
            if result and result.get('sectors'):
                self._concept_summary_cache = result
                return result
        except Exception as e:
            self.log(f"备用策略也失败: {e}", "ERROR")
        # 都失败，使用缓存
        if hasattr(self, '_concept_summary_cache') and self._concept_summary_cache:
            self.log("概念板块使用缓存数据", "WARNING")
            return self._concept_summary_cache
        return {'sectors': [], 'rising': 0, 'falling': 0}

    # 新浪A股代码范围（批量扫描用）
    _SINA_CODE_RANGES = [
        ('sh', 600000, 606000),   # 沪市主板
        ('sh', 688000, 690000),   # 科创板
        ('sz', 0, 4000),          # 深市主板
        ('sz', 300000, 302000),   # 创业板
    ]

    @staticmethod
    def _limit_pct_for(code):
        """根据代码判断涨跌停阈值"""
        num = code[2:] if len(code) > 2 else code
        if num.startswith('30') or num.startswith('688'):
            return 20.0
        elif num.startswith('8') or num.startswith('4'):
            return 30.0
        return 10.0

    def _sina_batch_quote(self, code_list):
        """新浪hq接口批量获取实时行情（800只/批，约0.2秒）"""
        url = f"https://hq.sinajs.cn/list={','.join(code_list)}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
            'Referer': 'https://finance.sina.com.cn',
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode('gbk')
        result = []
        for line in data.strip().split('\n'):
            match = re.search(r'hq_str_(\w+)="([^"]*)"', line)
            if not match or not match.group(2).strip():
                continue
            code = match.group(1)
            parts = match.group(2).split(',')
            if len(parts) < 4:
                continue
            try:
                prev_close = float(parts[2])
                price = float(parts[3])
            except (ValueError, IndexError):
                continue
            if prev_close <= 0 or price <= 0:
                continue
            row = {
                'code': code,
                'name': parts[0],
                'price': price,
                'prev': prev_close,
                'pct': (price - prev_close) / prev_close * 100,
            }
            # 盘口与量额：报文里本来就有，取出来供「封死/炸板」判定和成交额排序用
            # 索引口径：[7]=卖一价 [8]=成交量(股) [9]=成交额(元) [10]=买一量 [11]=买一价 [20]=卖一量
            if len(parts) >= 32:
                row['volume'] = self._safe_float(parts[8], 0.0)
                row['amount'] = self._safe_float(parts[9], 0.0)
                row['bid1_vol'] = self._safe_float(parts[10], 0.0)
                row['bid1_price'] = self._safe_float(parts[11], 0.0)
                row['ask1_price'] = self._safe_float(parts[7], 0.0)
                row['ask1_vol'] = self._safe_float(parts[20], 0.0)
                row['date'] = parts[30] if len(parts) > 30 else ''
                row['time'] = parts[31] if len(parts) > 31 else ''
            result.append(row)
        return result

    def get_limit_stocks(self):
        """获取涨跌停个股（新浪hq批量接口），按所属板块归类
        失败或结果为0时保留上次有效数据
        """
        try:
            limit_up, limit_down = [], []

            for prefix, start, end in self._SINA_CODE_RANGES:
                batch_size = 800
                for i in range(start, end, batch_size):
                    codes = [f"{prefix}{c:06d}" for c in range(i, min(i + batch_size, end))]
                    try:
                        stocks = self._sina_batch_quote(codes)
                    except Exception:
                        continue
                    for s in stocks:
                        name = s['name']
                        if 'ST' in name or '退' in name or 'N ' in name:
                            continue
                        lim = self._limit_pct_for(s['code'])
                        if s['pct'] >= lim - 0.15:
                            limit_up.append(s)
                        elif s['pct'] <= -lim + 0.15:
                            limit_down.append(s)

            if not limit_up and not limit_down:
                self.log("涨跌停：结果为0，可能非交易时段，保留上次数据", "INFO")
                return self._limit_stocks

            # 标注所属板块
            board_map = self._build_board_map()
            up_boards = {}
            for s in limit_up:
                boards = board_map.get(s['name'], [])
                s['boards'] = boards
                for b in boards:
                    up_boards.setdefault(b, []).append(s['name'])
            down_boards = {}
            for s in limit_down:
                boards = board_map.get(s['name'], [])
                s['boards'] = boards
                for b in boards:
                    down_boards.setdefault(b, []).append(s['name'])

            self.log(f"涨跌停统计（新浪）：涨停{len(limit_up)}只，跌停{len(limit_down)}只")
            return {
                'up': sorted(limit_up, key=lambda x: x['pct'], reverse=True),
                'down': sorted(limit_down, key=lambda x: x['pct']),
                'up_boards': dict(sorted(up_boards.items(), key=lambda x: len(x[1]), reverse=True)),
                'down_boards': dict(sorted(down_boards.items(), key=lambda x: len(x[1]), reverse=True)),
            }

        except Exception as e:
            self.log(f"获取涨跌停数据失败: {e}，保留上次结果", "ERROR")
            return self._limit_stocks

    def _build_board_map(self):
        """从概念板块缓存构建 {股票名: [板块1, 板块2, ...]} 映射"""
        board_map = {}
        cache = self._concept_summary_cache
        if cache and cache.get('sectors'):
            for s in cache['sectors']:
                lead = s.get('lead', '')
                board = s.get('name', '')
                if lead and board:
                    board_map.setdefault(lead, []).append(board)
        return board_map

    @staticmethod
    def _make_table(headers, rows, col_widths=None):
        """生成 box-drawing 表格"""
        if not rows:
            return ''
        if col_widths is None:
            col_widths = []
            for i, h in enumerate(headers):
                w = len(h)
                for r in rows:
                    w = max(w, len(str(r[i])) if i < len(r) else 0)
                col_widths.append(w + 2)
        def fmt_row(cells):
            parts = []
            for i, c in enumerate(cells):
                s = str(c)
                pad = col_widths[i] - len(s)
                parts.append(f' {s}{" " * max(pad - 1, 0)}')
            return '│' + '│'.join(parts) + '│'
        sep = '┼'.join('─' * w for w in col_widths)
        top = '┌' + '┬'.join('─' * w for w in col_widths) + '┐'
        mid = '├' + sep + '┤'
        bot = '└' + '┴'.join('─' * w for w in col_widths) + '┘'
        lines = [top, fmt_row(headers), mid]
        for r in rows:
            lines.append(fmt_row(r))
        lines.append(bot)
        return '\n'.join(lines)

    def generate_market_summary(self):
        """生成盘面总结报告（按模板格式）。"""
        today = datetime.now()
        date_str = today.strftime('%-m.%-d')
        R = []

        sh = self.stock_data.get('sh000001')
        sz = self.stock_data.get('sz399001')
        cy = self.stock_data.get('sz399006')

        # ── 并行获取概念板块 + K线 ──
        concept_result = [None]
        kline_result = [None]
        t1 = threading.Thread(target=lambda: concept_result.__setitem__(0, self.get_concept_summary_data()), daemon=True)
        t2 = threading.Thread(target=lambda: kline_result.__setitem__(0, self.get_kline_data()), daemon=True)
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)
        concept = concept_result[0] or {'sectors': [], 'rising': 0, 'falling': 0}
        klines = kline_result[0] or []
        # 日线接口可能未含当日K线，并入实时行情后再算区间/支撑压力，
        # 否则会把已跌破的旧低点写成“下方支撑/多头防线”
        bars = merge_live_bar(klines, sh)[-5:] if sh else list(klines)
        levels = compute_levels(sh['price'], bars, today=(sh.get('date') or '')) if sh else None

        # ── 辅助数据 ──
        all_sectors = self.sector_data.get('_all', []) if self.sector_data else []
        sec_rising = sum(1 for s in all_sectors if s.get('change', 0) > 0)
        sec_falling = sum(1 for s in all_sectors if s.get('change', 0) < 0)
        sec_flat = sum(1 for s in all_sectors if s.get('change', 0) == 0)
        sec_total = len(all_sectors)
        con_rising = concept.get('rising', 0)
        con_falling = concept.get('falling', 0)
        con_flat = sum(1 for s in concept.get('sectors', []) if s.get('change', 0) == 0)
        con_total = len(concept.get('sectors', [])) or con_rising + con_falling
        market_turnover_lines = self._market_turnover_lines()
        environment = evaluate_market_environment(
            predicted_turnover_yi=(
                self._turnover_snapshot.predicted_full_day
                if self._turnover_snapshot and self._turnover_snapshot.predicted_full_day
                else None
            ),
            market_change_pct=sh.get("change_pct") if sh else None,
            market_price=sh.get("price") if sh else None,
            market_high_price=sh.get("high") if sh else None,
            industry_breadth=(sec_rising / sec_total if sec_total else None),
            concept_breadth=(con_rising / con_total if con_total else None),
            recent_full_day_turnovers_yi=(
                self.turnover_tracker.recent_full_day_turnovers(
                    datetime.now().strftime("%Y-%m-%d"), limit=5
                )
            ),
        )
        cycle_history = (
            self.turnover_tracker.recent_full_day_turnovers(
                datetime.now().strftime("%Y-%m-%d"), limit=5
            )
            if self.turnover_tracker else ()
        )
        cycle_assessment = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=environment.predicted_turnover_yi,
            turnover_history_yi=cycle_history,
            index_today_pct=sh.get("change_pct") if sh else None,
            industry_breadth=(sec_rising / sec_total if sec_total else None),
            concept_breadth=(con_rising / con_total if con_total else None),
        ))
        cycle_text = (
            f"量能活跃度={cycle_assessment.regime.value}；"
            + format_market_cycle_assessment(cycle_assessment)
        )

        def _lead_fmt(s):
            name = s.get('lead', '')
            pct = s.get('lead_pct', '')
            if not name:
                return ''
            try:
                v = float(pct)
                if v >= 9.9:
                    return f'{name}涨停'
                return f'{name}{v:+.2f}%'
            except (ValueError, TypeError):
                return name

        def _breadth_sentence(label, rising, falling, flat=0):
            total = rising + falling + flat
            if total <= 0:
                return f'{label}涨跌数据暂缺'
            ratio = rising / total * 100
            if ratio >= 70:
                mood = '赚钱效应偏强'
            elif ratio >= 50:
                mood = '市场情绪中性偏暖'
            elif ratio >= 30:
                mood = '赚钱效应偏弱'
            else:
                mood = '亏钱效应明显'
            flat_text = f'，平{flat}' if flat else ''
            return f'{label}涨{rising} : 跌{falling}{flat_text}，上涨占比{ratio:.0f}%，{mood}'

        up_limit = self._limit_stocks.get('up', []) if self._limit_stocks else []
        down_limit = self._limit_stocks.get('down', []) if self._limit_stocks else []

        # ═══════════════════════════════════════
        # 一、大盘观点
        # ═══════════════════════════════════════
        R.append(f'📊 {date_str} 复盘')
        R.append('')
        R.append('一、大盘观点')
        R.append('')
        if sh and sz and cy:
            sp, szp, cyp = sh['change_pct'], sz['change_pct'], cy['change_pct']
            # 走势判断
            if sp > 0.5 and szp > 0.5:
                trend = '震荡走高，全线飘红'
            elif sp > 0 and szp > 0:
                trend = '小幅反弹，分化不大'
            elif abs(sp) < 0.2 and abs(szp) < 0.3:
                trend = '窄幅震荡，方向不明'
            elif sp < -1.0:
                trend = '放量杀跌，恐慌蔓延' if self._market_turnover_expanded() else '急跌承压，需观察承接'
            elif sp < -0.3 and szp < -0.3:
                trend = '震荡走低，全线承压'
            else:
                trend = '弱势整理'
            # 定性
            if sp < -1.0:
                qual = '偏空，防守为主'
            elif sp < -0.3:
                qual = '偏空，防御为主'
            elif sp < 0:
                qual = '偏弱，观望为主'
            elif sp > 0.5:
                qual = '偏多，可适度参与'
            else:
                qual = '中性，结构性行情'
            breadth_parts = [
                _breadth_sentence('行业板块', sec_rising, sec_falling, sec_flat),
                _breadth_sentence('概念板块', con_rising, con_falling, con_flat),
            ]
            breadth_text = '；'.join(breadth_parts)
            turnover_text = '；'.join(market_turnover_lines)
            R.append(
                f'{trend}。沪指{sp:+.2f}%、深成指{szp:+.2f}%、创业板{cyp:+.2f}%。'
                f'定性：{qual}。{breadth_text}。量能：{turnover_text}。'
            )
            R.append(f'环境判断：{environment.label}。{environment.summary()}。')
            R.append(f'行情节奏：{cycle_text}')
        elif sh:
            R.append(f'沪指{sh["change_pct"]:+.2f}%。')
        else:
            R.append('指数数据暂未获取。')
        R.append('')

        # ═══════════════════════════════════════
        # 二、板块异动
        # ═══════════════════════════════════════
        R.append('---')
        R.append('🟠 二、板块异动')
        R.append('')
        R.append('涨：')
        R.append('')
        if self.sector_data and self.sector_data.get('top'):
            rows = []
            for s in self.sector_data['top'][:5]:
                volume_text = f'成交{s.get("volume", 0) / 1e8:.0f}亿' if s.get('volume') else '成交额暂缺'
                rows.append([s['name'], f'{s["change"]:+.2f}%', volume_text])
            R.append(self._make_table(['板块', '涨幅', '成交额'], rows))
            R.append('说明：当前新浪行业接口的领涨股归属存在错配风险，暂不显示未经独立核验的领涨股。')
        R.append('')
        R.append('跌：')
        R.append('')
        if self.sector_data and self.sector_data.get('bottom'):
            rows = []
            for s in self.sector_data['bottom'][:8]:
                vol_str = f'成交{s["volume"]/1e8:.0f}亿' if s.get('volume') else ''
                desc = vol_str
                rows.append([s['name'], f'{s["change"]:+.2f}%', desc])
            R.append(self._make_table(['板块', '跌幅', '说明'], rows))
        else:
            R.append('（当前没有检测到下跌行业板块）')
        if concept.get('sectors'):
            concept_top = concept['sectors'][:3]
            concept_text = '、'.join(
                f"{s['name']}{s['change']:+.2f}%" for s in concept_top
            )
            R.append(f'概念线：{concept_text}。仅表示涨跌排名，不代表资金净流入。')
        R.append('')

        # ═══════════════════════════════════════
        # 三、资金流向
        # ═══════════════════════════════════════
        R.append('---')
        R.append('🟡 三、资金与成交额')
        R.append('')
        R.append('成交额 TOP 行业（不等同于资金净流向）：')
        R.append('')
        if all_sectors:
            vol_sorted = sorted(all_sectors, key=lambda x: x.get('volume', 0), reverse=True)[:8]
            rows = []
            for s in vol_sorted:
                rows.append([s['name'], f'{s["change"]:+.2f}%', f'{s.get("volume", 0)/1e8:.0f}亿'])
            R.append(self._make_table(['板块', '涨跌', '成交额'], rows))
            rising_volume = [s for s in vol_sorted if s.get('change', 0) > 0]
            falling_volume = [s for s in vol_sorted if s.get('change', 0) < 0]
            if rising_volume and falling_volume:
                R.append(
                    f'点评：成交额靠前板块中，{rising_volume[0]["name"]}等上涨，'
                    f'{falling_volume[0]["name"]}等下跌；这里只能说明成交活跃，'
                    '不能直接等同于资金净流入或净流出。'
                )
            elif rising_volume:
                R.append('点评：成交额靠前板块整体偏强，但仍需结合涨停扩散和后续量能确认。')
            elif falling_volume:
                R.append('点评：成交额靠前板块整体偏弱，放量下跌需警惕资金承接不足。')
        else:
            R.append('成交额数据暂缺。')
        R.append('')

        # ═══════════════════════════════════════
        # 四、涨跌家数比
        # ═══════════════════════════════════════
        R.append('---')
        R.append('🔵 四、涨跌家数比')
        R.append('')
        if sec_total > 0:
            R.append(f'- {_breadth_sentence("行业板块", sec_rising, sec_falling, sec_flat)}')
        else:
            R.append('- 行业板块：涨跌数据暂缺')
        if con_total > 0:
            R.append(f'- {_breadth_sentence("概念板块", con_rising, con_falling, con_flat)}')
        else:
            R.append('- 概念板块：涨跌数据暂缺')
        if up_limit or down_limit:
            up_names = '、'.join(s.get('name', '') for s in up_limit[:12])
            down_names = '、'.join(s.get('name', '') for s in down_limit[:8])
            R.append(f'- 涨停{len(up_limit)}只，跌停{len(down_limit)}只')
            if up_names:
                R.append(f'  涨停代表：{up_names}')
            if down_names:
                R.append(f'  跌停代表：{down_names}')
        else:
            R.append('- 涨跌停数据暂缺')
        if con_total > 0:
            con_ratio = con_rising / con_total
            if con_ratio >= 0.7:
                R.append('- 📌 市场宽度偏暖，赚钱效应较强，但需防止情绪过热。')
            elif con_ratio >= 0.5:
                R.append('- 📌 市场宽度中性偏暖，板块轮动仍需量能确认。')
            elif con_ratio >= 0.3:
                R.append('- 📌 市场宽度偏弱，只有少数方向获得资金关注。')
            else:
                R.append('- 📌 市场宽度较窄，亏钱效应仍在扩散。')
        R.append('')

        # ═══════════════════════════════════════
        # 五、量价关系
        # ═══════════════════════════════════════
        R.append('---')
        R.append('🟣 五、量价关系')
        R.append('')
        if market_turnover_lines:
            R.append('；'.join(market_turnover_lines))
            R.append('')
        # 添加 TurnoverTracker 同期对比信息
        if self._turnover_snapshot:
            snap = self._turnover_snapshot
            if snap.previous_same_time and snap.previous_same_time > 0:
                delta = snap.today - snap.previous_same_time
                delta_pct = delta / snap.previous_same_time * 100
                R.append(f'较昨日同期（{snap.slot}）：{delta:+,.0f}亿（{delta_pct:+.1f}%）')
            if snap.predicted_full_day and snap.predicted_full_day > 0:
                R.append(f'全天预估：{snap.predicted_full_day:,.0f}亿')
            R.append('')
        if klines:
            value_key = 'amount' if all(k.get('amount') is not None for k in klines) else 'volume'
            value_label = '沪指成交额' if value_key == 'amount' else '沪指成交量'
            value_unit = '亿' if value_key == 'amount' else '亿股'
            R.append('')
            rows = []
            for i, k in enumerate(klines):
                value = (k.get(value_key) or 0) / 1e8
                if i == 0:
                    change_str = '—'
                else:
                    previous_value = (klines[i - 1].get(value_key) or 0) / 1e8
                    change_str = f'{value - previous_value:+.0f}{value_unit}'
                rows.append([k['date'][5:], f'{value:.0f}{value_unit}', change_str])
            R.append(self._make_table(['日期', value_label, '变化'], rows))
            R.append('')
            if len(klines) >= 2:
                first_value = (klines[0].get(value_key) or 0) / 1e8
                last_value = (klines[-1].get(value_key) or 0) / 1e8
                if first_value > 0:
                    total_pct = (last_value - first_value) / first_value * 100
                    if total_pct < -10:
                        R.append(f'→ 较{len(klines)}个交易日前{value_label}减少{abs(total_pct):.0f}%')
                        if sh and sh['change_pct'] < 0:
                            R.append('→ 缩量下跌，承接力度偏弱，仍需观察后续量能。')
                    elif total_pct > 10:
                        R.append(f'→ 较{len(klines)}个交易日前{value_label}增加{total_pct:.0f}%')
        else:
            R.append('  ⚠️ K线数据获取失败，量价分析暂缺')
        R.append('')

        # ═══════════════════════════════════════
        # 六、技术看盘
        # ═══════════════════════════════════════
        R.append('---')
        R.append('📈 六、技术看盘')
        R.append('')
        if sh:
            price = sh['price']
            R.append(f'- 沪指{price:.0f}点')
            if levels and len(bars) >= 2:
                R.append(f'- 近{len(bars)}日震荡区间：{levels.low:.0f}~{levels.high:.0f}')
                if levels.broke_down:
                    R.append(f'  ⚠️ 已跌破箱体下沿{levels.prior_low:.0f}，短线转弱，该位转为压力')
                elif levels.broke_up:
                    R.append(f'  ⚠️ 突破箱体上沿{levels.prior_high:.0f}，短线转强')
                elif levels.pos > 0.8:
                    R.append('  ⚠️ 处于近期高位，接近压力区')
                elif levels.pos < 0.2:
                    R.append('  ⚠️ 逼近箱体下沿，若失守将打开向下空间')
                else:
                    R.append('  → 处于区间中部，箱体震荡格局')
                sup_txt = '、'.join(f'{v:.0f}附近' for v in levels.supports[:2]) or '暂无明显参考'
                res_txt = '、'.join(f'{v:.0f}附近' for v in levels.resistances[:2]) or '暂无明显参考'
                R.append(f'- 下方支撑：{sup_txt}，上方压力：{res_txt}')
        R.append('')

        # ═══════════════════════════════════════
        # 七、后市推演
        # ═══════════════════════════════════════
        R.append('---')
        R.append('🟢 七、后市推演')
        R.append('')
        if sh:
            pct = sh['change_pct']
            if con_total > 0 and con_rising / con_total < 0.1:
                R.append('- 存量博弈到极致——概念板块上涨占比极低，几乎所有题材都在亏钱')
            if klines and len(klines) >= 2:
                value_key = 'amount' if all(k.get('amount') is not None for k in klines) else 'volume'
                first_value = (klines[0].get(value_key) or 0) / 1e8
                last_value = (klines[-1].get(value_key) or 0) / 1e8
                if first_value > 0 and (last_value - first_value) / first_value < -0.1:
                    value_label = '成交额' if value_key == 'amount' else '成交量'
                    R.append(f'- 量能较{len(klines)}个交易日前{value_label}减少，增量资金尚未得到确认')
            if levels and levels.broke_down:
                next_sup = f'{levels.supports[0]:.0f}' if levels.supports else '更低位置'
                R.append(f'- 短期看：{levels.prior_low:.0f}已失守并转为压力，{next_sup}是短线观察位，破则可能继续下探')
            elif levels and levels.supports:
                R.append(f'- 短期看：{levels.supports[0]:.0f}是多头防线。守住则箱体延续，破则可能加速下行')
            rebound_guidance = market_rebound_guidance(pct)
            if rebound_guidance:
                R.append(f'- 📌 {rebound_guidance}')
        R.append('')

        # ═══════════════════════════════════════
        # 八、主线与轮动
        # ═══════════════════════════════════════
        R.append('---')
        R.append('🔴 八、机会与风险 / 主线与轮动')
        R.append('')
        if self.sector_data and self.sector_data.get('top'):
            top3 = self.sector_data['top'][:3]
            # 主线①
            s1 = top3[0] if len(top3) > 0 else None
            if s1:
                vol1 = f'成交{s1["volume"]/1e8:.0f}亿' if s1.get('volume') else '成交额暂缺'
                R.append(f'主线①：{s1["name"]}（{s1["change"]:+.2f}%，{vol1}）')
                R.append(f'  板块涨幅{s1["change"]:+.2f}%，领涨股字段暂不核验展示')
            # 主线②
            s2 = top3[1] if len(top3) > 1 else None
            if s2:
                vol2 = f'成交{s2["volume"]/1e8:.0f}亿' if s2.get('volume') else '成交额暂缺'
                R.append(f'主线②：{s2["name"]}（{s2["change"]:+.2f}%，{vol2}）')
                R.append(f'  板块涨幅{s2["change"]:+.2f}%')
        # 风险方向
        if self.sector_data and self.sector_data.get('bottom'):
            bot3 = self.sector_data['bottom'][:3]
            names = [s['name'] for s in bot3]
            R.append(f'风险方向：{"、".join(names)}（远离）')
            for s in bot3:
                R.append(f'  {s["name"]}{s["change"]:+.2f}%')
        if self.sector_data and self.sector_data.get('top'):
            leader = self.sector_data['top'][0]
            R.append(f'机会：{leader["name"]}当前强度最高，关注涨幅能否延续并扩散。')
        if self.sector_data and self.sector_data.get('bottom'):
            weakest = self.sector_data['bottom'][0]
            R.append(f'风险：{weakest["name"]}跌幅靠前，若放量下跌需回避。')
        if environment.status == MarketEnvironmentStatus.DIVERGENCE:
            R.append('📌 核心结论：成交额较高但指数或板块扩散不匹配，按放量分歧观察，不据此确认趋势延续。')
        elif environment.status == MarketEnvironmentStatus.STRONG_SUSTAINED:
            R.append('📌 核心结论：量能、指数与板块扩散暂时同向；连续性只统计此前完整收盘日，今日预估仍需收盘验证，不代表个股必涨。')
        elif environment.status == MarketEnvironmentStatus.STRONG_SINGLE_DAY:
            R.append('📌 核心结论：当前高成交额与指数、板块扩散同向，但连续性证据尚不足，先观察而非确认趋势。')
        elif environment.status == MarketEnvironmentStatus.UNKNOWN:
            R.append('📌 核心结论：环境证据不足，不把缺数据解释成放量分歧或强势延续。')
        elif con_total > 0:
            breadth_ratio = con_rising / con_total
            if breadth_ratio >= 0.7:
                R.append('📌 核心结论：市场偏暖，重点观察量能和主线持续性，防止情绪过热。')
            elif breadth_ratio < 0.3:
                R.append('📌 核心结论：市场宽度偏弱，优先防守，等待量能和主线扩散确认。')
            else:
                R.append('📌 核心结论：市场处于结构性轮动，暂不追逐单日强势。')
        R.append('')

        # ═══════════════════════════════════════
        # 下一步关注
        # ═══════════════════════════════════════
        R.append('---')
        R.append('🎯 下一步关注')
        R.append('')
        if sh:
            pct = sh['change_pct']
            if pct < -0.3:
                R.append('- 应对：控制仓位，等待量能回升和主线重新确认。')
            elif pct > 0.5:
                R.append('- 应对：不追单日涨幅，等待主线回踩或量能确认后再考虑增加暴露。')
            else:
                R.append('- 应对：保持中性仓位，优先观察确定性较高且有成交额配合的方向。')
        if con_total > 0:
            R.append(f'- 市场宽度：概念上涨占比{con_rising / con_total * 100:.0f}%')
        R.append('')
        R.append(f'--- 数据截至 {today.strftime("%Y-%m-%d %H:%M")}，仅供参考 ---')

        return '\n'.join(R)

    @staticmethod
    def _is_limit_up(pct_str):
        """判断是否涨停（涨幅接近10%或20%）"""
        try:
            pct = abs(float(pct_str))
            return 9.9 <= pct <= 10.1 or 19.9 <= pct <= 20.1 or 29.9 <= pct <= 30.1
        except (ValueError, TypeError):
            return False

    def _show_summary_in_textedit(self, text):
        """用系统文本编辑器显示盘面总结"""
        try:
            tmp = os.path.join(self.log_dir, '_market_summary.txt')
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(text)

            subprocess.Popen(
                ["open", "-a", "TextEdit", tmp],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.log("盘面总结已生成并打开")
        except Exception as e:
            self.log(f"显示盘面总结失败: {e}", "ERROR")
            self._alert(title="盘面总结", message=text[:500])

    def _tick_summary_timer(self, _):
        """每秒更新计时显示"""
        self._summary_elapsed += 1
        replace_menu_item_title(
            self._summary_menu_item,
            f"⏳ 正在生成盘面总结... ({self._summary_elapsed}s)",
        )

    def _start_summary_timer(self):
        """启动计时器"""
        self._summary_elapsed = 0
        self._summary_timer = rumps.Timer(self._tick_summary_timer, 1)
        self._summary_timer.start()
        replace_menu_item_title(self._summary_menu_item, "⏳ 正在生成盘面总结... (0s)")

    def _stop_summary_timer(self):
        """停止计时器"""
        if self._summary_timer:
            self._summary_timer.stop()
            self._summary_timer = None

    @staticmethod
    def _is_market_open():
        """判断A股是否在交易时段（9:30-11:30, 13:00-15:00）"""
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.hour * 60 + now.minute
        return (570 <= t <= 690) or (780 <= t <= 900)

    @staticmethod
    def _trading_minutes_elapsed():
        """计算从开盘到现在已过的交易分钟数（总交易时段240分钟：9:30-11:30 + 13:00-15:00）"""
        now = datetime.now()
        t = now.hour * 60 + now.minute  # 当前分钟数
        if t < 570:      # 9:30之前
            return 0
        elif t <= 690:    # 9:30-11:30
            return t - 570
        elif t < 780:     # 11:30-13:00 午休
            return 120
        elif t <= 900:    # 13:00-15:00
            return 120 + (t - 780)
        else:             # 15:00之后
            return 240

    def _fetch_index_daily_amounts(self, secid):
        """读取单个市场指数最近几日成交额，供市场总成交对比使用。"""
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": "101",
            "fqt": "1",
            "end": "20500101",
            "lmt": "5",
        }
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://quote.eastmoney.com/",
        })
        last_error = None
        for attempt in range(3):
            try:
                response = session.get(url, params=params, timeout=8)
                response.raise_for_status()
                data = response.json().get("data") or {}
                amounts = {}
                for line in data.get("klines") or []:
                    parts = line.split(",")
                    if len(parts) >= 7:
                        try:
                            amounts[parts[0]] = float(parts[6])
                        except (TypeError, ValueError):
                            continue
                return amounts
            except Exception as error:
                last_error = error
                if attempt < 2:
                    time.sleep(1)
        raise RuntimeError(f"市场指数K线请求失败: {last_error}")

    def _load_previous_market_turnover(self):
        """取上证和深证指数最近共同交易日的前一日成交额。"""
        try:
            sh_amounts = self._fetch_index_daily_amounts("1.000001")
            sz_amounts = self._fetch_index_daily_amounts("0.399001")
            common_dates = sorted(set(sh_amounts) & set(sz_amounts))
            if len(common_dates) < 2:
                return None
            previous_date = common_dates[-2]
            return sh_amounts[previous_date] + sz_amounts[previous_date]
        except Exception as error:
            self.log(f"读取上一交易日市场成交额失败: {error}", "WARNING")
            return None

    def _refresh_market_turnover(self):
        """刷新沪深市场总成交额，不受自选股列表影响。"""
        for code in ("sh000001", "sz399001"):
            data = self.stock_data.get(code)
            if data is None or code == "sz399001":
                data = self.get_stock_data(code)
            if data:
                self.market_turnover_data[code] = data

        today = datetime.now().strftime("%Y-%m-%d")
        if self.market_turnover["date"] != today:
            self.market_turnover["previous"] = self._load_previous_market_turnover()
            self.market_turnover["date"] = today

        total = sum(
            data.get("turnover", 0)
            for data in self.market_turnover_data.values()
        )
        if total > 0:
            self.market_turnover["today"] = total
            # 与 Android 一致：每次刷新都用最新数据计算预估
            # 但只在15分钟槽位记录历史数据
            try:
                prev_curve = self._get_previous_turnover_curve()
                # 使用实时数据调用 sample（预估始终用最新值）
                self._turnover_snapshot = self.turnover_tracker.sample(
                    total / 1e8, datetime.now(), previous_curve=prev_curve
                )
            except Exception as e:
                self.log(f"TurnoverTracker采样失败: {e}", "WARNING")

    def _get_previous_turnover_curve(self) -> Optional[TurnoverCurve]:
        """获取昨日分时成交额曲线（与 Android 策略一致）。

        - 只缓存成功结果，失败不缓存
        - 失败后下次 tick（15秒）立即重试
        - 每天只请求一次 API（成功后）
        """
        today = datetime.now().strftime("%Y-%m-%d")

        # 已成功获取过，直接复用（内存缓存）
        if self._prev_curve_date == today and self._prev_curve:
            return self._prev_curve

        # 检查磁盘缓存（程序重启后可复用）
        from turnover_tracker import _curve_cache
        if today in _curve_cache and _curve_cache[today] is not None:
            self._prev_curve = _curve_cache[today]
            self._prev_curve_date = today
            self.log(f"✅ 从缓存加载昨日分时曲线: {self._prev_curve.date}")
            return self._prev_curve

        # 失败后立即重试（与 Android 一致，每 15 秒 tick 重试）
        try:
            self._prev_curve = fetch_previous_turnover_curve(today)
            if self._prev_curve:
                self._prev_curve_date = today
                self.log(f"✅ 已获取昨日分时曲线: {self._prev_curve.date}, "
                         f"全天{self._prev_curve.full_day / 1e8:.0f}亿")
            else:
                # 失败不缓存，下次 tick 会重试（不设冷却时间）
                self.log("⚠️ 昨日分时曲线获取失败，下次 tick 重试", "DEBUG")
        except Exception as e:
            self.log(f"❌ 获取昨日分时曲线异常: {e}", "WARNING")
            self._prev_curve = None
        return self._prev_curve

    def _market_turnover_lines(self):
        """生成市场总成交额及较上一交易日同一时刻变化的展示行。"""
        current = self._safe_float(self.market_turnover.get("today"), 0.0)
        if current <= 0:
            return ["暂无市场成交数据"]

        # 优先使用 TurnoverTracker 的同期对比
        if self._turnover_snapshot:
            return format_turnover_lines(self._turnover_snapshot)

        # 没有 TurnoverTracker 同期数据时，只显示当前成交额，不做错误对比
        # （盘中累计 vs 昨天全天的对比毫无意义；收盘后可以比较全天）
        if self._trading_minutes_elapsed() >= 240:
            previous = self._safe_float(self.market_turnover.get("previous"), 0.0)
            if previous > 0:
                delta = current - previous
                delta_pct = delta / previous * 100
                return [
                    f"市场总成交  {current / 1e8:,.0f}亿",
                    f"较昨日全天 {delta / 1e8:+,.0f}亿 ({delta_pct:+.1f}%)",
                ]
        predicted, elapsed_pct = self._predict_daily_turnover(current)
        today_line = f"市场总成交  {current / 1e8:,.0f}亿"
        if predicted and elapsed_pct and elapsed_pct < 1.0:
            today_line += f"  ·  预估{predicted / 1e8:,.0f}亿"
        lines = [today_line]
        lines.append("昨日同期数据暂缺，已开始记录，明天起可对比")
        return lines

    def _market_turnover_expanded(self, threshold=1.10):
        """判断成交额是否相对可比基准明显放大。

        盘中用昨日同期，收盘后用昨日全天；缺少可比数据时返回 False，
        不把大跌幅度本身误称为「放量杀跌」。
        """
        snapshot = self._turnover_snapshot
        if snapshot:
            if snapshot.slot_minute >= 240 and snapshot.previous_full_day:
                return snapshot.today >= snapshot.previous_full_day * threshold
            if snapshot.previous_same_time:
                return snapshot.today >= snapshot.previous_same_time * threshold

        if self._trading_minutes_elapsed() < 240:
            return False
        current = self._safe_float(self.market_turnover.get("today"), 0.0)
        previous = self._safe_float(self.market_turnover.get("previous"), 0.0)
        return previous > 0 and current >= previous * threshold

    def _format_market_overview_title(self, index_items):
        """生成市场概览父菜单的一行紧凑摘要。"""
        parts = []
        for item in index_items:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("code") or "?"
            pct = self._safe_float(item.get("pct"))
            parts.append(f"{name}{self._format_price(item.get('price'))}{pct:+.1f}%")

        current = self._safe_float(self.market_turnover.get("today"), 0.0)
        if current > 0:
            parts.append(f"总{current / 1e8:,.0f}亿")
            # 优先使用 TurnoverTracker 同期对比
            if self._turnover_snapshot and self._turnover_snapshot.previous_same_time:
                # _turnover_snapshot 的数据已经是亿元，不需要再除 1e8
                delta = self._turnover_snapshot.today - self._turnover_snapshot.previous_same_time
                delta_pct = delta / self._turnover_snapshot.previous_same_time * 100
                parts.append(f"昨同期{delta:+,.0f}亿({delta_pct:+.1f}%)")
                # 全天预估
                predicted = self._turnover_snapshot.predicted_full_day
                if predicted and predicted > 0 and self._turnover_snapshot.slot_minute < 240:
                    parts.append(f"预估{predicted:,.0f}亿")
            elif self._trading_minutes_elapsed() >= 240:
                previous = self._safe_float(self.market_turnover.get("previous"), 0.0)
                if previous > 0:
                    delta = current - previous
                    parts.append(
                        f"昨全天{delta / 1e8:+,.0f}亿"
                        f"({delta / previous * 100:+.1f}%)"
                    )
            # 盘中没有同期数据时不比较昨天全天，避免口径混用
        return " ".join(parts) if parts else "市场概览"

    def _predict_daily_turnover(self, current_turnover):
        """根据当前成交额和已过交易时间占比，预估全天成交额
        返回 (predicted, elapsed_pct) 或 (None, None) 表示不预判

        优先使用 TurnoverTracker 的昨日节奏推算（更准确），
        没有昨日数据时才用线性外推（会偏高，因为成交额非线性分布）。
        """
        now = datetime.now()
        if now.weekday() >= 5:
            return None, None
        elapsed = self._trading_minutes_elapsed()
        if elapsed <= 5:
            # 开盘前5分钟数据波动大，不做预判
            return None, None
        if elapsed >= 240:
            # 已收盘，返回实际值
            return current_turnover, 1.0

        # 优先使用 TurnoverTracker 的预估（基于昨日节奏，更准确）
        if self._turnover_snapshot and self._turnover_snapshot.predicted_full_day:
            predicted = self._turnover_snapshot.predicted_full_day * 1e8  # 转为元
            pct = elapsed / 240.0
            return predicted, pct

        # 退回线性外推（会偏高，因为成交额非线性分布）
        # 上午成交额通常占全天 60-65%，而非 50%
        pct = elapsed / 240.0
        # 使用经验修正：上午权重更高
        if elapsed <= 120:  # 上午
            # 上午 120 分钟约占全天 60%
            adjusted_pct = pct * 1.2
        else:  # 下午
            adjusted_pct = pct

        predicted = current_turnover / adjusted_pct if adjusted_pct > 0 else current_turnover
        return predicted, pct

    def show_market_summary(self, _):
        """显示盘面总结"""
        today = datetime.now().strftime('%Y-%m-%d')
        # 生成中：再次点击 → 取消
        if self._summary_timer:
            self._summary_cancel.set()
            return
        # 有今天的缓存，直接显示
        if self._summary_text and self._last_summary_date == today:
            self._show_summary_in_textedit(self._summary_text)
            return
        # 盘中：有缓存则显示上次的，无缓存则提示等18:00
        if self._is_market_open():
            if self._summary_text and self._last_summary_date:
                self._show_summary_in_textedit(self._summary_text)
            else:
                self._alert(
                    title="📋 盘面总结",
                    message="市场尚未收盘，数据仍在变动中。\n\n"
                            "盘面总结将在每天 18:00 自动生成，届时直接点击即可查看。"
                )
            return
        # 休市后无缓存：后台生成
        if self._summary_running:
            self._summary_cancel.set()
            return
        self._start_summary_generation(show_window=True)

    def _start_summary_generation(self, show_window):
        """以单实例任务启动盘面总结，防止手动和自动任务重入。"""
        with self._summary_lock:
            if self._summary_running:
                self.log("盘面总结仍在生成，跳过重复任务", "INFO")
                return False
            self._summary_running = True

        self._summary_cancel.clear()
        if show_window:
            self._start_summary_timer()
        threading.Thread(
            target=self._run_summary_task,
            args=(show_window,),
            daemon=True,
        ).start()
        return True

    def _run_summary_task(self, show_window):
        try:
            self._fetch_and_show_summary(show_window=show_window)
        finally:
            with self._summary_lock:
                self._summary_running = False

    def _fetch_and_show_summary(self, show_window=True):
        """后台获取数据并生成盘面总结
        show_window=True: 手动触发，生成后打开 TextEdit
        show_window=False: 定时触发，只缓存不弹窗
        """
        self.log(f"_fetch_and_show_summary 启动: show_window={show_window}", "INFO")
        import time as _time
        start_time = _time.time()

        def _cancelled():
            return self._summary_cancel.is_set()

        def _timed_out():
            return _time.time() - start_time >= self._SUMMARY_TIMEOUT

        def _check():
            """返回 'cancel' / 'timeout' / None"""
            if _cancelled():
                return 'cancel'
            if _timed_out():
                return 'timeout'
            return None

        def _notify_main(fn):
            self.log(f"_notify_main 调度回调: {fn.__name__ if hasattr(fn, '__name__') else 'lambda'}", "INFO")
            rumps.Timer(lambda t: fn(t), 0.001).start()

        try:
            # 如果缓存为空（刚启动），先主动获取基础数据
            if not self.stock_data:
                self.log("盘面总结：缓存为空，先获取指数和板块数据", "INFO")
                for item in self.watchlist:
                    status = _check()
                    if status == 'cancel':
                        _notify_main(self._on_summary_cancelled)
                        return
                    if status == 'timeout':
                        _notify_main(self._on_summary_timeout)
                        return
                    try:
                        data = self.get_stock_data(item['code'])
                        if data:
                            self.stock_data[item['code']] = data
                    except Exception:
                        pass

            status = _check()
            if status == 'cancel':
                _notify_main(self._on_summary_cancelled)
                return
            if status == 'timeout':
                _notify_main(self._on_summary_timeout)
                return

            if not self.sector_data or not self.sector_data.get('top'):
                try:
                    self.sector_data = self.get_sector_data()
                except Exception:
                    pass

            status = _check()
            if status == 'cancel':
                _notify_main(self._on_summary_cancelled)
                return
            if status == 'timeout':
                _notify_main(self._on_summary_timeout)
                return

            self.log("盘面总结：开始生成报告...", "INFO")
            summary = self.generate_market_summary()
            self.log(f"盘面总结：报告生成完成，长度={len(summary) if summary else 0}", "INFO")
            if summary:
                self._summary_text = summary
                self._last_summary_date = datetime.now().strftime('%Y-%m-%d')
            # 停止计时器，恢复菜单标题
            try:
                if self._summary_timer:
                    self._summary_timer.stop()
                self._summary_timer = None
                replace_menu_item_title(self._summary_menu_item, "📋 盘面总结")
            except Exception:
                pass
            if show_window and summary:
                self._show_summary_in_textedit(summary)
                self.log("盘面总结已生成并打开", "INFO")
            elif not show_window:
                self.log("盘面总结已定时生成并缓存", "INFO")
            else:
                self.log("盘面总结生成失败，无内容", "ERROR")
        except Exception as e:
            self.log(f"生成盘面总结失败，保留上次有效结果: {e}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")
            self._dispatch_to_main(
                lambda error_message=str(e): self._on_summary_failed(error_message)
            )

    def _on_summary_failed(self, error_message):
        """主线程：盘面总结生成失败，恢复菜单状态并提示原因。"""
        self._stop_summary_timer()
        replace_menu_item_title(self._summary_menu_item, "📋 盘面总结")
        self._alert(
            title="盘面总结生成失败",
            message=f"本次总结未完成：{error_message}\n\n请稍后重试。",
        )

    def _on_summary_cancelled(self, timer):
        """主线程：盘面总结被用户取消"""
        timer.stop()
        self._stop_summary_timer()
        replace_menu_item_title(self._summary_menu_item, "📋 盘面总结")
        self.log("盘面总结生成已取消", "INFO")

    def _on_summary_timeout(self, timer):
        """主线程：盘面总结生成超时"""
        timer.stop()
        self._stop_summary_timer()
        self._summary_cancel.set()  # 通知后台线程尽快退出
        replace_menu_item_title(self._summary_menu_item, "📋 盘面总结")
        self.log("盘面总结生成超时，部分数据源无响应", "WARNING")
        self._alert(
            title="📋 盘面总结",
            message="生成超时（超过90秒），部分数据源无响应。\n请稍后重试。"
        )

    def _on_summary_ready(self, timer, show_window=True):
        """主线程：盘面总结生成完成"""
        self.log(f"_on_summary_ready 被调用: show_window={show_window}, "
                 f"summary_text={'有' if self._summary_text else '无'}", "INFO")
        timer.stop()
        self._stop_summary_timer()
        replace_menu_item_title(self._summary_menu_item, "📋 盘面总结")

        if show_window and self._summary_text:
            # 手动触发，打开 TextEdit
            self._show_summary_in_textedit(self._summary_text)
        elif not show_window:
            # 定时触发，只缓存不弹窗，菜单加上日期提示
            replace_menu_item_title(
                self._summary_menu_item,
                f"📋 盘面总结（{self._last_summary_date}）",
            )
            self.log("盘面总结已定时生成并缓存", "INFO")
        elif not self._summary_text:
            self._alert(title="盘面总结", message="生成失败，请查看日志。")

    def _auto_refresh_summary(self):
        """每天16:00、18:00和21:00各生成一次盘面总结（不弹窗）。"""
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        # 16:00 收盘后第一版总结
        if now.hour == 16 and self._summary_afternoon_date != today:
            if self._start_summary_generation(show_window=False):
                self._summary_afternoon_date = today
        # 18:00 生成第二版总结
        elif now.hour == 18 and self._summary_auto_date != today:
            if self._start_summary_generation(show_window=False):
                self._summary_auto_date = today
        # 21:00 再生成一次夜间更新，覆盖18:00缓存
        elif now.hour == 21 and self._last_summary_night != today:
            self._last_summary_date = None
            if self._start_summary_generation(show_window=False):
                self._last_summary_night = today

    def get_indicator_history(self, ak, code, start_date, end_date):
        """获取并统一指标策略所需的日线字段"""
        symbol = code[2:] if re.match(r"^(sh|sz)\d{6}$", code) else code
        try:
            with INDICATOR_API_LOCK, self.requests_timeout(15):
                history_df = ak.stock_zh_a_daily(
                    symbol=code,
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",
                )
        except Exception as error:
            self.log(f"{code} 使用新浪历史接口失败: {error}", "WARNING")
            return None

        history_df = history_df.rename(columns={
            "日期": "date",
            "交易日期": "date",
            "开盘": "open",
            "开盘价": "open",
            "收盘": "close",
            "收盘价": "close",
            "最高": "high",
            "最高价": "high",
            "最低": "low",
            "最低价": "low",
            "成交量": "volume",
        })
        # 513 形态还要用到开盘价和成交量
        required_columns = {"close", "high", "low", "open", "volume"}
        if not required_columns.issubset(history_df.columns):
            return None

        for column_name in required_columns:
            history_df[column_name] = pd.to_numeric(
                history_df[column_name], errors="coerce"
            )
        history_df = history_df.dropna(subset=list(required_columns))
        if "date" in history_df.columns:
            history_df["date"] = pd.to_datetime(history_df["date"], errors="coerce")
            history_df = history_df.sort_values("date")
        return history_df.reset_index(drop=True)

    def get_indicator_spot_data(self, ak, universe_limit=200):
        """通过新浪财经API获取A股活跃股行情（按成交量排序，速度快、不易限流）。

        universe_limit=None 时翻页取全部沪深A股（与 Android 版 allAStocks 一致，
        最多 100 页 × 80 只），供 513 形态做全市场扫描。
        """
        import time as _time
        page_size = 80
        max_pages = 100  # 与 Android 版 MAX_ACTIVE_STOCK_PAGES 一致
        if universe_limit is None:
            pages_needed = max_pages
        else:
            pages_needed = min(max(1, (universe_limit + page_size - 1) // page_size), max_pages)
        all_rows = []
        headers = {"Referer": "https://finance.sina.com.cn"}
        for page in range(1, pages_needed + 1):
            url = (
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                "json_v2.php/Market_Center.getHQNodeData"
                f"?page={page}&num={page_size}&sort=volume&asc=0&node=hs_a"
            )
            for attempt in range(3):
                try:
                    # 这里 requests.get 已显式带 timeout，不需要再注入超时补丁。
                    with INDICATOR_API_LOCK:
                        resp = requests.get(url, headers=headers, timeout=20)
                        resp.raise_for_status()
                        data = resp.json()
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    _time.sleep(2)
            if not data:
                break
            all_rows.extend(data)
            if universe_limit is None and len(data) < page_size:
                break
            _time.sleep(0.5)

        if not all_rows:
            raise RuntimeError("新浪API未返回数据")

        records = []
        seen = set()
        for item in all_rows:
            symbol = item.get("symbol", "").lower()
            name = item.get("name", "")
            trade = item.get("trade", "0")
            volume = item.get("volume", "0")
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            records.append({
                "代码": symbol,
                "名称": name,
                "最新价": float(trade) if trade and trade != "0" else 0.0,
                "成交量": float(volume) if volume else 0.0,
            })
        return pd.DataFrame(records)

    @staticmethod
    @contextmanager
    def requests_timeout(timeout):
        """只对当前线程的 requests 调用注入超时。

        akshare 不暴露 timeout 参数，只能在调用期间接管 Session.request。
        补丁一次性安装、超时值走 thread-local，退出时不再「还原」——原先
        全局替换再还原的做法，两个线程同时进入时会互相踩掉对方的补丁。
        """
        _install_request_timeout_patch()
        previous = getattr(_REQUEST_TIMEOUT_LOCAL, "timeout", None)
        _REQUEST_TIMEOUT_LOCAL.timeout = timeout
        try:
            yield
        finally:
            _REQUEST_TIMEOUT_LOCAL.timeout = previous

    def screen_by_indicators(self, universe_limit=200, full_market=False):
        """执行严格低位反转、宽松预警和513低位形态三档筛选。

        full_market=True 时 513 形态扫全部沪深A股（与 Android 版一致），
        严格/宽松仍只算成交前 universe_limit 只；返回本轮结果字典，失败返回 None。
        """
        result = []
        relaxed_result = []
        strong_result = []
        five_one_three_result = []
        stats = {
            'total_stocks': 0,
            'history_available': 0,
            'historical_five_bullish': 0,
            'recent_five_bullish': 0,
            'waiting_confirmation': 0,
            'confirmed': 0,
        }
        successful_history = 0
        failed_history = 0
        self.indicator_last_error = None
        try:
            import akshare as ak

            self.log(f"开始指标选股({'全市场513' if full_market else '活跃股'})...", "INFO")
            try:
                spot_df = self.get_indicator_spot_data(
                    ak, universe_limit=None if full_market else universe_limit
                )
                spot_df['成交量'] = pd.to_numeric(
                    spot_df['成交量'], errors='coerce'
                ).fillna(0)
                spot_df = spot_df[spot_df['成交量'] > 0]
                spot_df['代码'] = spot_df['代码'].astype(str).str.lower()
                spot_df = spot_df[
                    spot_df['代码'].str.match(r'^(sh|sz)\d{6}$', na=False)
                ]
                spot_df = spot_df[~spot_df['名称'].str.contains('ST|退', na=False)]
                spot_df = spot_df.sort_values('成交量', ascending=False)
                # 严格/宽松只算成交前 N 只；513 全市场模式下其余股票只做形态检查
                top_codes = set(spot_df.head(universe_limit)['代码'])
                if not full_market:
                    spot_df = spot_df.head(universe_limit)
                stats['total_stocks'] = len(spot_df)
                self.log(
                    f"筛选{'全市场' if full_market else '活跃股'}: {len(spot_df)} 只",
                    "INFO",
                )
            except Exception as e:
                self.log(f"获取A股行情失败: {e}", "ERROR")
                self.indicator_last_error = f"获取A股行情失败: {e}"
                return None

            def check_stock(row, include_indicators=True):
                code = row['代码']
                name = row['名称']
                try:
                    hist_df = self.get_indicator_history(
                        ak,
                        code,
                        # 513 形态要求 60 根完整日线，90 个自然日遇长假会不够，取 120 天（与 Android 版同量级）
                        (datetime.now() - pd.Timedelta(days=120)).strftime("%Y%m%d"),
                        datetime.now().strftime("%Y%m%d"),
                    )
                    if hist_df is None or len(hist_df) < 35:
                        return None

                    close = hist_df['close']
                    high = hist_df['high']
                    low = hist_df['low']
                    limit_up_acceleration = evaluate_limit_up_acceleration(
                        code,
                        close.tolist(),
                        hist_df['volume'].tolist(),
                    )

                    # 513低位形态（与Android版FiveOneThreeScreener一致）：
                    # 排除未完成的当日K线，5连阳 → 倍量阳 → 3天缩量；
                    # 全市场模式下成交前200以外的股票只做形态检查，跳过MACD/RSI/KDJ。
                    five_bars = [
                        {
                            "date": str(bar_row.get("date"))[:10],
                            "open": float(bar_row["open"]),
                            "high": float(bar_row["high"]),
                            "low": float(bar_row["low"]),
                            "close": float(bar_row["close"]),
                            "volume": float(bar_row["volume"]),
                        }
                        for _, bar_row in hist_df.iterrows()
                    ]
                    inspection = inspect_five_one_three(
                        code=code,
                        name=name,
                        bars=five_bars,
                        today=datetime.now().strftime("%Y-%m-%d"),
                        current_price=float(close.iloc[-1]),
                    )
                    strong_candidate = self._strong_candidate_from_bars(
                        code, name, five_bars,
                    )
                    if not include_indicators:
                        return {
                            'data': None,
                            'strict': False,
                            'relaxed': False,
                            'five_one_three': inspection['hit'],
                            'strong': None,
                            'inspection': inspection,
                        }

                    dif, dea, _ = calc_macd(close)
                    rsi = calc_rsi(close)
                    k, d, _ = calc_kdj(high, low, close)

                    latest_dif = dif.iloc[-1]
                    latest_dea = dea.iloc[-1]
                    latest_rsi = rsi.iloc[-1]
                    latest_k = k.iloc[-1]
                    latest_d = d.iloc[-1]
                    latest_price = close.iloc[-1]
                    previous_dif = dif.iloc[-2]
                    previous_dea = dea.iloc[-2]
                    previous_k = k.iloc[-2]
                    previous_d = d.iloc[-2]
                    latest_macd_hist = latest_dif - latest_dea
                    previous_macd_hist = previous_dif - previous_dea

                    strict_macd_ok = (
                        previous_dif <= previous_dea
                        and latest_dif > latest_dea
                        and latest_dif > 0
                        and latest_dea > 0
                    )
                    strict_rsi_ok = pd.notna(latest_rsi) and latest_rsi < 30
                    strict_kdj_ok = (
                        previous_k <= previous_d
                        and latest_k > latest_d
                        and pd.notna(latest_k)
                        and pd.notna(latest_d)
                        and latest_k < 30
                    )
                    # 宽松MACD：低位反转信号（满足任一子条件）
                    # 条件A：柱体由负转零轴（强反转信号）
                    macd_cross_zero = (
                        previous_macd_hist < 0 and latest_macd_hist >= 0
                    )
                    # 条件B：柱体仍在负值区但持续收窄 + DIF向上（早期反转信号）
                    macd_narrow_rising = (
                        latest_macd_hist < 0
                        and latest_macd_hist > previous_macd_hist
                        and latest_dif > previous_dif
                    )
                    # 条件C：柱体在零轴上方持续放大（多头动能增强）
                    macd_expand_positive = (
                        latest_macd_hist > 0
                        and latest_macd_hist > previous_macd_hist
                    )
                    relaxed_macd_ok = (
                        pd.notna(latest_macd_hist)
                        and pd.notna(previous_macd_hist)
                        and (macd_cross_zero or macd_narrow_rising or macd_expand_positive)
                    )
                    # 记录具体触发的MACD信号类型
                    if macd_cross_zero:
                        macd_detail = "柱体转正"
                    elif macd_narrow_rising:
                        macd_detail = "柱体收窄+DIF↑"
                    elif macd_expand_positive:
                        macd_detail = "柱体放大"
                    else:
                        macd_detail = ""

                    relaxed_rsi_ok = pd.notna(latest_rsi) and latest_rsi < 45
                    relaxed_kdj_ok = (
                        pd.notna(latest_k)
                        and pd.notna(latest_d)
                        and latest_k > latest_d
                        and latest_k < 50
                    )
                    relaxed_score = sum(
                        (relaxed_macd_ok, relaxed_rsi_ok, relaxed_kdj_ok)
                    )
                    stock_result = {
                        'code': code,
                        'name': name,
                        'price': latest_price,
                        'rsi': latest_rsi,
                        'k': latest_k,
                        'd': latest_d,
                        'dif': latest_dif,
                        'relaxed_score': relaxed_score,
                        'macd_detail': macd_detail if relaxed_macd_ok else "",
                        'rsi_ok': relaxed_rsi_ok,
                        'kdj_ok': relaxed_kdj_ok,
                        'limit_up_shrink_acceleration': limit_up_acceleration['flagged'],
                        'limit_up_acceleration_tag': limit_up_acceleration['tag'],
                        'limit_up_volume_ratio': limit_up_acceleration['volume_ratio'],
                    }
                    return {
                        'data': stock_result,
                        'strict': strict_macd_ok and strict_rsi_ok and strict_kdj_ok,
                        'relaxed': relaxed_score >= 2,
                        'five_one_three': inspection['hit'],
                        'strong': strong_candidate,
                        'inspection': inspection,
                    }
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(check_stock, row, row['代码'] in top_codes): row
                    for row in spot_df.to_dict('records')
                }
                for future in as_completed(futures):
                    try:
                        screen_result = future.result()
                        if screen_result:
                            successful_history += 1
                            inspection = screen_result.get('inspection') or {}
                            if inspection.get('history_available'):
                                stats['history_available'] += 1
                            if inspection.get('historical_five_bullish'):
                                stats['historical_five_bullish'] += 1
                            if inspection.get('recent_five_bullish'):
                                stats['recent_five_bullish'] += 1
                            if screen_result['data'] is not None:
                                if screen_result['strict']:
                                    result.append(screen_result['data'])
                                if screen_result['relaxed']:
                                    relaxed_result.append(screen_result['data'])
                            five_hit = screen_result.get('five_one_three')
                            if five_hit:
                                five_one_three_result.append(five_hit)
                                if five_hit['stage'] == FiveOneThreeStage.WAITING_CONFIRMATION:
                                    stats['waiting_confirmation'] += 1
                                elif five_hit['stage'] == FiveOneThreeStage.CONFIRMED:
                                    stats['confirmed'] += 1
                            strong_candidate = screen_result.get('strong')
                            if strong_candidate:
                                strong_result.append(strong_candidate)
                        else:
                            failed_history += 1
                    except Exception:
                        failed_history += 1

            total_history = successful_history + failed_history
            coverage = successful_history / total_history if total_history else 0
            self.log(
                f"指标历史数据覆盖率: {successful_history}/{total_history} "
                f"({coverage:.0%})",
                "INFO",
            )
            if total_history == 0 or coverage < 0.5:
                self.indicator_last_error = (
                    f"历史数据成功率过低: {successful_history}/{total_history}"
                )
                self.log(self.indicator_last_error, "ERROR")
                return None

            # 排序规则与 Android 版一致：严格档按 RSI 升序；
            # 宽松档按满足项数降序（3/3 在前），同分再按 RSI 升序；
            # 513档按阶段倒序（已确认在前），同阶段按近60日涨幅降序。
            result = order_strict_hits(result)
            relaxed_result = order_relaxed_hits(relaxed_result)
            five_one_three_result = order_five_one_three_hits(five_one_three_result)
            strong_result.sort(key=lambda item: (
                not item.get("eligible_candidate", False),
                -float(item.get("price") or 0),
            ))
            self.log(
                f"指标选股完成: 严格 {len(result)} 只，宽松预警 {len(relaxed_result)} 只，"
                f"强势延续 {len(strong_result)} 只，"
                f"513形态 {len(five_one_three_result)} 只"
                f"（历史5连阳{stats['historical_five_bullish']} / "
                f"当前5连阳{stats['recent_five_bullish']}）",
                "INFO",
            )
        except Exception as e:
            self.log(f"指标选股异常: {e}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")
            self.indicator_last_error = f"指标选股异常: {e}"
            return None
        return {
            'strict': result,
            'relaxed': relaxed_result,
            'strong': strong_result,
            'five_one_three': five_one_three_result,
            'stats': stats,
            'scanned_codes': set(spot_df['代码']),
        }

    @staticmethod
    def _menu_rows(rows, limit=30):
        """菜单命中行上限：行情普涨时可能一次涌入大量「5连阳观察」，只展示前 limit 只。"""
        rows = list(rows)
        if len(rows) <= limit:
            return rows, 0
        return rows[:limit], len(rows) - limit

    def indicator_menu_titles(self):
        """构建指标策略子菜单的逐行内容"""
        view = build_observation_view(getattr(self, "observation_pool", None), datetime.now())
        titles = view["focus_lines"] + [f"状态  ·  {self.indicator_refresh_status()}"]

        titles.append(f"第一档 · 严格策略（{len(self.indicator_data)}只）")
        if self.indicator_data:
            shown_strict, hidden_strict = self._menu_rows(prioritize_stocks(self.indicator_data, view))
            titles.extend(
                decorate_stock(
                    f"{stock['name']} | {stock['price']:.2f} | "
                    f"RSI {stock['rsi']:.0f} | K {stock['k']:.0f} > D {stock['d']:.0f}"
                    + (f" | {stock['limit_up_acceleration_tag']}"
                       if stock.get('limit_up_acceleration_tag') else ""),
                    stock,
                    view,
                )
                for stock in shown_strict
            )
            if hidden_strict:
                titles.append(f"  …其余 {hidden_strict} 只省略")
        else:
            titles.append("暂无符合严格条件的股票")

        titles.append(f"第二档 · 宽松预警（{len(self.relaxed_indicator_data)}只）")
        if self.relaxed_indicator_data:
            shown_relaxed, hidden_relaxed = self._menu_rows(prioritize_stocks(self.relaxed_indicator_data, view))
            for stock in shown_relaxed:
                tags = []
                if stock.get('macd_detail'):
                    tags.append(f"MACD:{stock['macd_detail']}")
                if stock.get('rsi_ok'):
                    tags.append("RSI")
                if stock.get('kdj_ok'):
                    tags.append("KDJ金叉")
                tag_str = " | ".join(tags) if tags else ""
                if stock.get('limit_up_acceleration_tag'):
                    tag_str = f"{tag_str} | {stock['limit_up_acceleration_tag']}" if tag_str else stock['limit_up_acceleration_tag']
                titles.append(decorate_stock(
                    f"{stock['name']} | {stock['price']:.2f} | "
                    f"{stock['relaxed_score']}/3分 | {tag_str}", stock, view
                ))
            if hidden_relaxed:
                titles.append(f"  …其余 {hidden_relaxed} 只省略")
        else:
            titles.append("暂无符合宽松预警的股票")

        titles.append(f"513形态（{len(self.five_one_three_data)}只）")
        if self.five_one_three_data:
            shown_five, hidden_five = self._menu_rows(prioritize_stocks(self.five_one_three_data, view))
            titles.extend(
                decorate_stock(f"{stock['name']} | {stock['price']:.2f} | "
                               f"{stock['progress']} | {stock['detail']} | 60日{stock['gain60']:+.1f}%", stock, view)
                for stock in shown_five
            )
            if hidden_five:
                titles.append(f"  …其余 {hidden_five} 只省略")
        else:
            titles.append("暂无符合513形态的股票")

        stats = self.five_one_three_stats or {}
        if stats.get('total_stocks'):
            titles.extend([
                f"  全市场扫描{stats['total_stocks']}只｜有效日线{stats['history_available']}只",
                f"  历史5连阳{stats['historical_five_bullish']}只｜当前5连阳{stats['recent_five_bullish']}只",
                f"  倍量阳确认{stats['waiting_confirmation']}只｜完整513确认{stats['confirmed']}只",
            ])

        if self.indicator_results_stale:
            stale_at = self.indicator_results_stale_at.strftime('%m-%d %H:%M') if self.indicator_results_stale_at else '最近一次刷新'
            titles.append(f"提示  ·  本次无新命中，保留{stale_at}结果")

        observation_pool = getattr(self, "observation_pool", None)
        if observation_pool is not None:
            titles.append("  逐日记录与淘汰原因见「🔎 持续观察池」子菜单")
            if getattr(self, "_observation_last_error", None):
                titles.append(f"观察池复核提示：{self._observation_last_error}")

        if self._last_intraday_indicator_at:
            titles.append(
                f"最近盘中成功：{self._last_intraday_indicator_at:%m-%d %H:%M}"
            )
        last_success_at = self.indicator_refresh_scheduler.last_success_at()
        if last_success_at:
            titles.append(f"本窗口成功：{last_success_at}")
        last_error = self.indicator_refresh_scheduler.last_error()
        if last_error:
            titles.append(f"最近失败：{last_error}")
        return titles

    @staticmethod
    def _intraday_indicator_slot(now):
        """返回当前盘中半小时刷新槽位；非交易时段返回 None。"""
        if now.weekday() >= 5:
            return None
        minute = now.hour * 60 + now.minute
        if 570 <= minute < 690:       # 09:30-11:30
            slot_minute = 570 + ((minute - 570) // 30) * 30
        elif 780 <= minute <= 960:     # 13:00-16:00，含15:00/15:30/16:00
            slot_minute = 780 + ((minute - 780) // 30) * 30
        else:
            return None
        return f"{now:%Y-%m-%d}-{slot_minute:04d}"

    def _load_five_one_three_scan_date(self):
        try:
            with open(self.five_one_three_state_file, encoding="utf-8") as handle:
                return json.load(handle).get("last_full_scan_date") or None
        except Exception:
            return None

    def _save_five_one_three_scan_date(self, date_text):
        try:
            with open(self.five_one_three_state_file, "w", encoding="utf-8") as handle:
                json.dump({"last_full_scan_date": date_text}, handle)
        except Exception as error:
            self.log(f"保存513深扫日期失败: {error}", "WARNING")

    def indicator_refresh_status(self):
        if self.indicator_screen_lock.locked():
            return "正在刷新"
        now = datetime.now()
        intraday_slot = self._intraday_indicator_slot(now)
        if intraday_slot:
            if self._last_intraday_indicator_slot == intraday_slot:
                return "盘中本时段已刷新"
            return "盘中等待刷新"
        return self.indicator_refresh_scheduler.status(now)

    def update_limit_menu(self):
        """将涨跌停数据填充到二级子菜单"""
        self.limit_menu.clear()

        up_list = self._limit_stocks.get('up', [])
        down_list = self._limit_stocks.get('down', [])

        # 汇总行
        self.limit_menu.add(rumps.MenuItem(
            f"涨停 {len(up_list)} 只 | 跌停 {len(down_list)} 只", callback=None
        ))
        self.limit_menu.add(rumps.MenuItem("--", callback=None))

        # 涨停个股明细
        if up_list:
            self.limit_menu.add(rumps.MenuItem("▲ 涨停个股", callback=None))
            for s in up_list:
                boards = '、'.join(s.get('boards', [])[:3])
                suffix = f" [{boards}]" if boards else ""
                self.limit_menu.add(rumps.MenuItem(
                    f"  {s['name']}({s['code']}) {s['pct']:+.2f}%{suffix}", callback=None
                ))

        self.limit_menu.add(rumps.MenuItem("--", callback=None))

        # 跌停个股明细
        if down_list:
            self.limit_menu.add(rumps.MenuItem("▼ 跌停个股", callback=None))
            for s in down_list:
                boards = '、'.join(s.get('boards', [])[:3])
                suffix = f" [{boards}]" if boards else ""
                self.limit_menu.add(rumps.MenuItem(
                    f"  {s['name']}({s['code']}) {s['pct']:.2f}%{suffix}", callback=None
                ))

        # 空行保持菜单宽度
        self.limit_menu.add(rumps.MenuItem("                                        ", callback=None))

    def update_indicator_menu(self):
        """将指标策略结果逐行写入子菜单；顶部是两套刷新的入口（盘中不自动扫描）。"""
        indicator_menu = self.menu["📈 指标策略"]
        indicator_menu.clear()
        titles = self.indicator_menu_titles()
        focus_end = next((index for index, title in enumerate(titles) if title.startswith("状态  ·")), 0)
        for title in titles[:focus_end]:
            indicator_menu.add(rumps.MenuItem(title, callback=None))
        if focus_end:
            indicator_menu.add(rumps.separator)
        indicator_menu.add(rumps.MenuItem("🔄 重扫指标（前200，秒级）", callback=self.scan_indicators_now))
        indicator_menu.add(rumps.MenuItem("🔭 深扫全市场（趋势+513）", callback=self.scan_patterns_now))
        if getattr(self, "observation_pool", None) is not None:
            pool_menu = rumps.MenuItem("🔎 持续观察池", callback=None)
            pool_menu.add(rumps.MenuItem("🔄 立即复核观察池", callback=self.review_observation_pool_now))
            pool_menu.add(rumps.MenuItem("📋 查看完整观察报告", callback=self.show_observation_pool_report))
            pool_menu.add(rumps.MenuItem("🔔 盘中买点提醒已开启：回踩企稳 + 连续资金确认", callback=None))
            pool_menu.add(rumps.MenuItem("至少3次/跨2分钟确认；同股每天一次", callback=None))
            review = getattr(self, "_entry_review", None)
            if review is not None:
                pool_menu.add(rumps.MenuItem("📒 查看买点复盘报告", callback=self.show_entry_review_report))
                pool_menu.add(rumps.MenuItem("🔄 后台更新买点复盘", callback=self.refresh_entry_review_now))
                toggle_title = "⏸ 暂停买点复盘（选股提醒继续）" if review.enabled else "▶ 恢复买点复盘"
                pool_menu.add(rumps.MenuItem(toggle_title, callback=self.toggle_entry_review))
                pool_menu.add(rumps.MenuItem(review.status, callback=None))
            if getattr(self, "_entry_review_error", None):
                pool_menu.add(rumps.MenuItem(f"复盘模块提示：{self._entry_review_error}", callback=None))
            for title in self.observation_pool.menu_lines(at=datetime.now()):
                pool_menu.add(rumps.MenuItem(title, callback=None))
            if self._observation_last_error:
                pool_menu.add(rumps.MenuItem(f"复核提示：{self._observation_last_error}", callback=None))
            if getattr(self, "_observation_entry_last_error", None):
                pool_menu.add(rumps.MenuItem(f"提醒检测提示：{self._observation_entry_last_error}", callback=None))
            indicator_menu.add(pool_menu)
        # rumps.MenuItem.separator() 不存在（本机 rumps 是 rumps.separator 哨兵）；
        # 这里曾经抛 AttributeError，导致下面的指标结果永远加不进菜单（2026-10-12）。
        indicator_menu.add(rumps.separator)
        for title in titles[focus_end:]:
            indicator_menu.add(rumps.MenuItem(title, callback=None))

    def _build_menu_data(self):
        """构建统一的菜单数据结构，供下拉菜单和浮动窗口共用"""
        sections = {}

        # 1. 指数
        idx_items = []
        for item in self.watchlist:
            code = item['code']
            if not (code.startswith('sh000') or code.startswith('sz399')):
                continue
            if code not in self.stock_data:
                continue
            d = self.stock_data[code]
            idx_items.append({
                "name": item['name'],
                "price": d['price'],
                "pct": d['change_pct'],
                "turnover": d.get('turnover', 0),
            })
        sections["index"] = {"title": "📊 指数", "items": idx_items}

        # 2. 个股（show_in_bar）
        stk_items = []
        for item in self.watchlist:
            if not item.get('show_in_bar', False):
                continue
            if item['code'] in self.stock_data:
                d = self.stock_data[item['code']]
                stk_items.append({
                    "name": item['name'],
                    "price": d['price'],
                    "pct": d['change_pct'],
                })
        sections["stock"] = {"title": "📈 个股", "items": stk_items}

        # 3. 涨幅榜
        top = []
        if self.show_sector and self.sector_data and self.sector_data.get('top'):
            for s in self.sector_data['top']:
                top.append({"name": s['name'], "pct": s['change']})
        sections["top_sector"] = {"title": "🔥 涨幅榜", "items": top}

        # 4. 跌幅榜
        bottom = []
        if self.show_sector and self.sector_data and self.sector_data.get('bottom'):
            for s in self.sector_data['bottom']:
                bottom.append({"name": s['name'], "pct": s['change']})
        sections["bottom_sector"] = {"title": "💧 跌幅榜", "items": bottom}

        # 5. 概念板块
        concepts = []
        if self.show_sector and self.concept_data and self.concept_data.get('top'):
            for s in self.concept_data['top']:
                concepts.append({"name": s['name'], "pct": s['change']})
        sections["concept"] = {"title": "🤖 概念板块", "items": concepts}

        # 6. 涨跌停
        up_list = self._limit_stocks.get('up', [])
        down_list = self._limit_stocks.get('down', [])
        limit_items = [{"name": f"涨停 {len(up_list)} 只 | 跌停 {len(down_list)} 只", "pct": 0}]
        for s in up_list:
            boards = '、'.join(s.get('boards', [])[:3])
            limit_items.append({
                "name": f"▲{s['name']}({s['code']})",
                "pct": s['pct'],
                "boards": boards,
            })
        for s in down_list:
            boards = '、'.join(s.get('boards', [])[:3])
            limit_items.append({
                "name": f"▼{s['name']}({s['code']})",
                "pct": s['pct'],
                "boards": boards,
            })
        sections["limit"] = {"title": "📊 涨跌停", "items": limit_items}

        # 7. 指标策略
        view = build_observation_view(getattr(self, "observation_pool", None), datetime.now())
        sections["indicator"] = {
            "title": f"📈 指标策略 · 📋 今日曾提醒{view['buy_count']} · 🟡 日线候选{view['ready_count']}",
            "buy_count": view["buy_count"],
            "items": self.indicator_menu_titles(),
        }

        # 8. 盘面总结
        summary = self._summary_text or ""
        if summary:
            summary_items = [{"name": line, "pct": 0} for line in summary.split("\n") if line.strip()]
        else:
            summary_items = [{"name": "🕘 暂未生成  ·  请稍后刷新", "pct": 0}]
        sections["summary"] = {"title": "📋 盘面总结", "items": summary_items}

        return sections

    def _update_daily_report_menu(self):
        """把「📦 持仓信息」的摘要写进子菜单。

        菜单行只回答「今天亏了多少 / 比大盘如何 / 最危险的是谁」；
        点开才看得懂的分布统计不进菜单。第一子项恒为可点的「打开完整报告」。
        """
        cached = self._last_daily_report
        if not cached:
            replace_menu_item_title(self._portfolio_info_menu_item, "📦 持仓信息")
            self._portfolio_info_menu_item.clear()
            self._portfolio_info_menu_item.add(
                rumps.MenuItem("📄 打开完整报告", callback=self.show_daily_report)
            )
            self._portfolio_info_menu_item.add(
                rumps.MenuItem("等待盘中刷新槽位（约每半小时）", callback=None)
            )
            self._portfolio_info_menu_item.add(rumps.MenuItem(" " * 40, callback=None))
            return

        overview = cached.get("overview") or {}
        meta = cached.get("meta") or {}
        replace_menu_item_title(self._portfolio_info_menu_item, menu_title(overview, meta))

        self._portfolio_info_menu_item.clear()
        self._portfolio_info_menu_item.add(
            rumps.MenuItem("📄 打开完整报告", callback=self.show_daily_report)
        )
        for line in format_menu_summary(overview, meta):
            self._portfolio_info_menu_item.add(rumps.MenuItem(line, callback=None))
        # rumps 菜单宽度按最长行自适应，末行补空格防换行后被截断
        self._portfolio_info_menu_item.add(rumps.MenuItem(" " * 40, callback=None))

    def update_display(self):
        """更新菜单栏显示 + 浮动窗口"""
        try:
            menu = self._build_menu_data()

            # ── 菜单栏标题（show_in_bar 的股票） ──
            try:
                display_parts = []
                for item in self.watchlist:
                    if item.get('show_in_bar', False) and item['code'] in self.stock_data:
                        d = self.stock_data[item['code']]
                        display_parts.append(self._format_bar_quote(
                            item['name'], d['price'], d['change_pct']))
                if display_parts:
                    # 菜单栏保留全部 show_in_bar 个股，不使用分隔符或 +N 省略。
                    self.title = "".join(display_parts)
                else:
                    self.title = "加载行情中…"
            except Exception as e:
                self.log(f"标题更新失败: {e}", "ERROR")

            # ── 📊 市场概览父菜单横向摘要 ──
            # 生成与写入分开捕获：前者是纯 Python 格式化，后者是 AppKit 调用，
            # 失败原因完全不同。这里曾经一天报 8000+ 次却只留了 {e}，无法定位。
            try:
                idx = menu.get("index", {})
                index_items = idx.get("items", [])
                overview_title = self._format_market_overview_title(index_items)
            except Exception as e:
                self.log(
                    f"指数菜单更新失败: 标题生成 {e}\n{traceback.format_exc()}",
                    "ERROR",
                )
            else:
                try:
                    replace_menu_item_title(self._market_overview_menu_item, overview_title)
                except Exception as e:
                    self.log(
                        f"指数菜单更新失败: 标题写入 {e}\n{traceback.format_exc()}",
                        "ERROR",
                    )

            # ── 🔥 涨幅榜 ──
            try:
                self.top_sector_menu.clear()
                top_items = menu.get("top_sector", {}).get("items", [])
                if top_items:
                    for s in top_items:
                        self.top_sector_menu.add(rumps.MenuItem(
                            f"{s['name']}   {self._fmt_pct(s['pct'])}", callback=None))
                else:
                    self.top_sector_menu.add(rumps.MenuItem("暂无数据 · 等待更新", callback=None))
            except Exception as e:
                self.log(f"涨幅榜更新失败: {e}", "ERROR")

            # ── 💧 跌幅榜 ──
            try:
                self.bottom_sector_menu.clear()
                bottom_items = menu.get("bottom_sector", {}).get("items", [])
                if bottom_items:
                    for s in bottom_items:
                        self.bottom_sector_menu.add(rumps.MenuItem(
                            f"{s['name']}   {self._fmt_pct(s['pct'])}", callback=None))
                else:
                    self.bottom_sector_menu.add(rumps.MenuItem("暂无数据 · 等待更新", callback=None))
            except Exception as e:
                self.log(f"跌幅榜更新失败: {e}", "ERROR")

            # ── 🤖 概念板块 ──
            try:
                self.concept_menu.clear()
                concept_items = menu.get("concept", {}).get("items", [])
                if concept_items:
                    for s in concept_items:
                        self.concept_menu.add(rumps.MenuItem(
                            f"{s['name']}   {self._fmt_pct(s['pct'])}", callback=None))
                else:
                    self.concept_menu.add(rumps.MenuItem("暂无数据 · 等待更新", callback=None))
            except Exception as e:
                self.log(f"概念板块更新失败: {e}", "ERROR")

            # ── 📊 涨跌停（用 _build_menu_data 数据） ──
            try:
                self.limit_menu.clear()
                limit = menu.get("limit", {})
                if not limit.get("items"):
                    self.limit_menu.add(rumps.MenuItem("暂无数据 · 等待更新", callback=None))
                else:
                    for it in limit.get("items", []):
                        pct = it.get("pct", 0)
                        name = it["name"]
                        boards = it.get("boards", "")
                        suffix = f" [{boards}]" if boards else ""
                        if pct == 0:
                            self.limit_menu.add(rumps.MenuItem(name, callback=None))
                        else:
                            self.limit_menu.add(rumps.MenuItem(
                                f"  {name}  {pct:+.2f}%{suffix}", callback=None))
            except Exception as e:
                self.log(f"涨跌停更新失败: {e}", "ERROR")

            # ── 📈 指标策略 ──
            try:
                self.update_indicator_menu()
            except Exception as e:
                self.log(f"指标策略更新失败: {e}", "ERROR")

            # ── 📦 持仓信息 ──
            try:
                self._update_daily_report_menu()
            except Exception as e:
                self.log(f"今日报告菜单更新失败: {e}\n{traceback.format_exc()}", "ERROR")

            # ── 浮动窗口 ──
            try:
                self._update_floating_window(menu)
            except Exception as e:
                self.log(f"浮动窗口更新失败: {e}", "ERROR")

        except Exception as e:
            self.log(f"更新显示失败: {e}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")

    def _update_floating_window(self, menu):
        """用 _build_menu_data 的数据更新浮动窗口"""
        def _fmt_price(p):
            return f"{p:.0f}" if p >= 100 else f"{p:.2f}"

        def _pct_color(pct):
            if pct > 0:
                return "▲", "red"
            elif pct < 0:
                return "▼", "green"
            return "─", "gray"

        # ── 紧凑模式：指数 + 个股 + 量能 ──
        clines = [" 📈 A股实时监控", " ─────────────────"]
        ccol = [(clines[0], "header"), (clines[1], "gray")]
        buy_count = menu.get("indicator", {}).get("buy_count", 0)
        if buy_count:
            buy_line = f" 📋 今日曾提醒{buy_count}只（非当前买点）｜展开→指标策略"
            clines.append(buy_line)
            ccol.append((buy_line, "red"))

        idx_items = menu.get("index", {}).get("items", [])
        if not idx_items:
            empty_line = " 暂无行情数据 · 等待刷新"
            clines.append(empty_line)
            ccol.append((empty_line, "gray"))
        for it in idx_items:
            arrow, color = _pct_color(it['pct'])
            l = f" {it['name']}  {_fmt_price(it['price'])}  {arrow}{abs(it['pct']):.2f}%"
            clines.append(l); ccol.append((l, color))

        stk = menu.get("stock", {}).get("items", [])
        if stk:
            clines.append(" · · · · · · · · · · · · · ·")
            ccol.append((" · · · · · · · · · · · · · ·", "gray"))
            for it in stk:
                arrow, color = _pct_color(it['pct'])
                l = f" {it['name']}  {_fmt_price(it['price'])}  {arrow}{abs(it['pct']):.2f}%"
                clines.append(l); ccol.append((l, color))

        for line in self._market_turnover_lines():
            display_line = f" {line}"
            clines.append(display_line)
            ccol.append((display_line, "gray"))

        compact_text = "\n".join(clines) if clines else " 等待数据..."
        self.floating_window.set_compact(compact_text, ccol)

        # ── 菜单数据：把 menu 转成浮动窗口需要的格式 ──
        fw_menu = {}

        def _to_fw_items(items, name_key="name", pct_key="pct"):
            result = []
            for it in items:
                pct = it[pct_key]
                arrow, color = _pct_color(pct)
                result.append({
                    "name": it[name_key],
                    "value": f"{arrow}{abs(pct):.2f}%",
                    "color": color,
                })
            return result

        # 指数
        if idx_items:
            fw_idx = []
            for it in idx_items:
                arrow, color = _pct_color(it['pct'])
                fw_idx.append({
                    "name": it['name'],
                    "value": f"{_fmt_price(it['price'])} {arrow}{abs(it['pct']):.2f}%",
                    "color": color,
                })
            fw_menu["index"] = {"title": "📊 指数", "items": fw_idx}

        # 个股
        if stk:
            fw_stk = []
            for it in stk:
                arrow, color = _pct_color(it['pct'])
                fw_stk.append({
                    "name": it['name'],
                    "value": f"{_fmt_price(it['price'])} {arrow}{abs(it['pct']):.2f}%",
                    "color": color,
                })
            fw_menu["stock"] = {"title": "📈 个股", "items": fw_stk}

        market_turnover_lines = self._market_turnover_lines()
        fw_menu["volume"] = {
            "title": "💰 量能",
            "items": [{
                "name": "市场总成交",
                "value": "  ·  ".join(market_turnover_lines),
                "color": "white",
            }],
        }

        # 涨幅榜
        top = menu.get("top_sector", {}).get("items", [])
        if top:
            fw_menu["top_sector"] = {"title": "🔥 涨幅榜", "items": _to_fw_items(top)}

        # 跌幅榜
        bottom = menu.get("bottom_sector", {}).get("items", [])
        if bottom:
            fw_menu["bottom_sector"] = {"title": "💧 跌幅榜", "items": _to_fw_items(bottom)}

        # 概念板块
        concepts = menu.get("concept", {}).get("items", [])
        if concepts:
            fw_menu["concept"] = {"title": "🤖 概念板块", "items": _to_fw_items(concepts)}

        # 涨跌停
        limit = menu.get("limit", {})
        limit_items = []
        for it in limit.get("items", []):
            pct = it.get("pct", 0)
            arrow, color = _pct_color(pct)
            name = it["name"]
            boards = it.get("boards", "")
            suffix = f" [{boards}]" if boards else ""
            limit_items.append({
                "name": name,
                "value": f"{arrow}{abs(pct):.2f}%{suffix}" if pct != 0 else "",
                "color": color if pct != 0 else "white",
            })
        if limit_items:
            fw_menu["limit"] = {"title": "📊 涨跌停", "items": limit_items}

        # 指标策略
        ind_titles = menu.get("indicator", {}).get("items", [])
        if ind_titles:
            ind_items = []
            for t in ind_titles:
                ind_items.append({"name": t, "value": "", "color": indicator_color(t)})
            fw_menu["indicator"] = {"title": menu.get("indicator", {}).get("title", "📈 指标策略"), "items": ind_items}

        # 盘面总结
        summary_items = menu.get("summary", {}).get("items", [])
        if summary_items:
            fw_menu["summary"] = {"title": "📋 盘面总结", "items": [
                {"name": it["name"], "value": "", "color": "white"}
                for it in summary_items
            ]}

        self.floating_window.set_menu_data(fw_menu)

    def start_update_timer(self):
        """启动定时更新"""
        # 使用 rumps.Timer 确保回调在主线程执行
        self.timer = rumps.Timer(self.on_timer_tick, self.update_interval)
        self.timer.start()
        # rumps 只把 NSTimer 注册进 NSDefaultRunLoopMode：NSAlert.runModal() 期间
        # run loop 跑在 modal 模式，定时器全部停摆——2026-10-22 13:25 大盘 crash
        # 弹窗一开就是 52 分钟，行情/预警整段冻结，正赶上中天科技 27.4→28.2 的
        # V 型反弹，一条提醒都没发。补注册进 common modes，弹窗期间监控继续跑。
        try:
            from Foundation import NSRunLoop, NSRunLoopCommonModes
            NSRunLoop.mainRunLoop().addTimer_forMode_(
                self.timer._nstimer, NSRunLoopCommonModes)
        except Exception as error:
            self.log(f"Timer 注册 common modes 失败（弹窗期间监控会冻结）: {error}", "WARNING")

        # 首次数据加载在后台线程（只获取数据，不更新UI）
        threading.Thread(target=self._fetch_data, daemon=True).start()

    def on_timer_tick(self, _):
        """定时器回调（在主线程执行）"""
        # 先用已有数据更新显示（主线程安全）
        self.update_display()
        # 然后在后台线程获取新数据
        threading.Thread(target=self._fetch_data, daemon=True).start()

    def _on_initial_data_loaded(self, _):
        """首次数据加载完成后，在主线程更新显示"""
        if _ is not None:
            _.stop()
        self.update_display()

    def _first_indicator_screen(self):
        """首次启动时执行一次指标选股"""
        self.maybe_refresh_indicators()

    def _on_indicator_screen_done(self, _):
        """指标选股完成后刷新显示"""
        if _ is not None:
            _.stop()
        self.update_display()

    # 旧命中的保留上限：最后一次真正刷新起超过 3 天，「无命中则保留」不再生效。
    SCREEN_STALE_RETENTION_DAYS = 3

    def maybe_refresh_indicators(self, mode="auto"):
        """选股扫描入口。

        mode:
        - "auto"：仅夜间窗口（18:00–06:00）自动全市场深扫一次，成功即停；**盘中不自动**。
        - "indicators"：手动快扫（成交前200，只更新严格/宽松），秒级。
        - "patterns"：手动深扫（全市场趋势+513，顺带全量刷新指标），数分钟。
        """
        now = datetime.now()
        if mode == "auto" and not self.indicator_refresh_scheduler.can_attempt(now):
            return
        if not self.indicator_screen_lock.acquire(blocking=False):
            self.log("选股扫描仍在执行，跳过重复任务", "INFO")
            return
        try:
            full_market = mode != "indicators"
            if mode == "auto":
                self.indicator_refresh_scheduler.begin_attempt(now)
                attempt_count = self.indicator_refresh_scheduler.state["attempt_count"]
                self.log(f"夜间全市场深扫第 {attempt_count}/8 次尝试开始", "INFO")
            else:
                self.log(
                    f"手动选股扫描开始（{'全市场深扫' if full_market else '指标快扫'}）", "INFO"
                )

            round_result = self.screen_by_indicators(full_market=full_market)
            completed_at = datetime.now()
            if round_result is None:
                error = self.indicator_last_error or "指标筛选未返回结果"
                if mode == "auto":
                    self.indicator_refresh_scheduler.record_failure(completed_at, error)
                    self.log(f"选股扫描失败，夜间窗口内重试: {error}", "ERROR")
                else:
                    self.log(f"手动选股扫描失败: {error}", "ERROR")
                return

            self._merge_screen_results(
                strict=round_result["strict"],
                relaxed=round_result["relaxed"],
                strong=round_result.get("strong", []),
                five=round_result["five_one_three"],
                stats=round_result.get("stats"),
                include_patterns=full_market,
                completed_at=completed_at,
            )
            self._refresh_observation_pool(
                self._observation_hits(round_result["strict"], round_result["relaxed"],
                                       round_result["five_one_three"],
                                       round_result.get("strong", [])),
                completed_at,
            )
            if mode == "auto":
                self.indicator_refresh_scheduler.record_success(completed_at)
                self.log("选股深扫已成功刷新，本窗口不再重复请求", "INFO")
            else:
                self.log("手动选股扫描刷新成功", "INFO")
        finally:
            self.indicator_screen_lock.release()
            rumps.Timer(self._on_indicator_screen_done, 0.001).start()

    def _screen_group_fresh(self, at):
        """该组内容还在保质期内吗？未知时间按不保留处理（宁可空着不拿旧命中断章取义）。"""
        if not at:
            return False
        return (datetime.now() - at).total_seconds() <= self.SCREEN_STALE_RETENTION_DAYS * 86400

    def _merge_screen_results(self, *, strict, relaxed, strong=(), five, stats, include_patterns, completed_at):
        """按列表各自保留 + 按组记时间 + 落盘。

        某档本次为空、上次非空**且还在保质期内**就沿用上次；指标快扫不动趋势/513/统计。
        哪组没保留任何旧列表才算完整刷新，该组的时间才更新。
        """
        prev_strict = list(self.indicator_data)
        prev_relaxed = list(self.relaxed_indicator_data)
        prev_strong = list(getattr(self, "strong_candidate_data", []))
        prev_five = list(self.five_one_three_data)
        indicator_retained = self._screen_group_fresh(self._indicator_group_at)
        pattern_retained = self._screen_group_fresh(self._pattern_group_at)

        strict_kept = bool(prev_strict) and not strict and indicator_retained
        relaxed_kept = bool(prev_relaxed) and not relaxed and indicator_retained
        strong_kept = bool(prev_strong) and not strong and indicator_retained
        five_kept = include_patterns and bool(prev_five) and not five and pattern_retained

        self.indicator_data = prev_strict if strict_kept else list(strict)
        self.relaxed_indicator_data = prev_relaxed if relaxed_kept else list(relaxed)
        self.strong_candidate_data = prev_strong if strong_kept else list(strong)
        if include_patterns:
            self.five_one_three_data = prev_five if five_kept else list(five)
            if stats is not None:
                self.five_one_three_stats = stats

        if not strict_kept and not relaxed_kept and not strong_kept:
            self._indicator_group_at = completed_at
        if include_patterns and not five_kept:
            self._pattern_group_at = completed_at

        kept_any = strict_kept or relaxed_kept or strong_kept or five_kept
        self.indicator_results_stale = kept_any
        kept_times = [
            at
            for at, kept in (
                (self._indicator_group_at, strict_kept or relaxed_kept or strong_kept),
                (self._pattern_group_at, five_kept),
            )
            if kept and at
        ]
        # 保留下来的是旧结果，时间用保留内容自己的生成时间（取更早的那个）。
        self.indicator_results_stale_at = min(kept_times) if kept_times else None
        self._save_screen_results()
        return kept_any

    @staticmethod
    def _strong_candidate_from_bars(code, name, bars):
        if len(bars) < 25:
            return None
        closes = [float(bar["close"]) for bar in bars]
        highs = [float(bar["high"]) for bar in bars]
        lows = [float(bar["low"]) for bar in bars]
        volumes = [float(bar["volume"]) for bar in bars]
        if not all(math.isfinite(value) and value > 0 for value in closes + highs + lows + volumes):
            return None
        if any(not lows[index] <= min(float(bars[index]["open"]), closes[index]) <= max(float(bars[index]["open"]), closes[index]) <= highs[index]
               for index in range(len(bars))):
            return None
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        ma20_prev = sum(closes[-21:-1]) / 20
        sample = bars[-60:]
        low60 = min(float(bar["low"]) for bar in sample)
        high60 = max(float(bar["high"]) for bar in sample)
        if high60 > low60:
            location_ratio = (closes[-1] - low60) / (high60 - low60)
            price_location = (
                "HIGH" if location_ratio >= 0.85
                else "LOW" if location_ratio <= 0.15
                else "MID"
            )
        else:
            price_location = "MID"
        average_volume20 = sum(volumes[-20:]) / min(20, len(volumes))
        recent_lows = lows[-3:]
        previous_lows = lows[-6:-3]
        result = assess_strong_continuation(StrongContinuationInput(
            price=closes[-1],
            ma5=ma5,
            ma10=ma10,
            ma20=ma20,
            ma20_prev=ma20_prev,
            price_location=price_location,
            bias_pct=(closes[-1] - ma5) / ma5 * 100 if ma5 else 0.0,
            breakout_reference=max(closes[:-5]) if len(closes) > 5 else None,
            volume_ratio=volumes[-1] / average_volume20 if average_volume20 else None,
            lower_pivots_rising=(
                min(recent_lows) >= min(previous_lows) if previous_lows else None
            ),
            support_holds=lows[-1] >= min(lows[-20:]),
            market_ok=None,
            sector_ok=None,
        ))
        if result.state.name not in {"STRONG_CONTINUATION", "EXTENDED_CONTINUATION"}:
            return None
        hold_result = assess_continuation_hold(ContinuationHoldInput(
            strong_state=result.state.name,
            high_state="HIGH_PENDING" if price_location == "HIGH" else "NOT_HIGH",
            risk_line=result.risk_line.name if result.risk_line else None,
            risk_price=result.risk_price,
            bias_pct=(closes[-1] - ma5) / ma5 * 100 if ma5 else 0.0,
            volume_ratio=volumes[-1] / average_volume20 if average_volume20 else None,
            first_wave=True,
            timeframe_ok=None,
        ))
        hold_info = continuation_hold_to_dict(hold_result)
        return {
            "code": code,
            "name": name,
            "price": closes[-1],
            "strong_state": result.state.name,
            "strong_state_label": result.state.value,
            "eligible_candidate": result.eligible_candidate,
            "risk_line": result.risk_line.name if result.risk_line else None,
            "risk_price": result.risk_price,
            "breakout_reference": result.breakout_reference,
            "price_location": price_location,
            "bias_pct": (closes[-1] - ma5) / ma5 * 100 if ma5 else 0.0,
            "volume_ratio": volumes[-1] / average_volume20 if average_volume20 else None,
            "reasons": list(result.reasons),
            "blockers": list(result.blockers),
            "continuation_hold": hold_info,
            "continuation_state": hold_info["state"],
        }

    @staticmethod
    def _observation_hits(strict, relaxed, five, strong=None):
        """把选股结果归一成观察池所需的信号来源。"""
        hits = []
        for stock in strict or []:
            hits.append({
                "code": stock.get("code"),
                "name": stock.get("name"),
                "price": stock.get("price"),
                "source": "strict",
            })
        for stock in relaxed or []:
            hits.append({
                "code": stock.get("code"),
                "name": stock.get("name"),
                "price": stock.get("price"),
                "source": "relaxed",
            })
        for stock in five or []:
            stage = stock.get("stage")
            if stage == FiveOneThreeStage.CONFIRMED or str(stage).endswith("CONFIRMED"):
                source = "five_confirmed"
            else:
                source = "five_waiting"
            hits.append({
                "code": stock.get("code"),
                "name": stock.get("name"),
                "price": stock.get("price"),
                "source": source,
            })
        for stock in strong or []:
            hits.append({
                "code": stock.get("code"),
                "name": stock.get("name"),
                "price": stock.get("price"),
                "source": "strong",
                "strong_state": stock.get("strong_state"),
                "risk_line": stock.get("risk_line"),
                "risk_price": stock.get("risk_price"),
                "continuation_state": stock.get("continuation_state"),
                "continuation_hold": stock.get("continuation_hold"),
            })
        return hits

    @staticmethod
    def _json_safe(value):
        """NaN/Infinity 不是合法 JSON（部分读取端会炸），落盘前归一成 None。"""

        def _clean(item):
            if isinstance(item, float) and (item != item or item in (float("inf"), float("-inf"))):
                return None
            if isinstance(item, dict):
                return {key: _clean(val) for key, val in item.items()}
            if isinstance(item, (list, tuple)):
                return [_clean(val) for val in item]
            return item

        return _clean(value)

    def _save_screen_results(self):
        """选股结果落盘：盘中不自动扫描后，重启靠它恢复，不再空白。"""
        def _text(at):
            return at.strftime("%Y-%m-%d %H:%M") if at else None

        payload = {
            "indicator_at": _text(self._indicator_group_at),
            "pattern_at": _text(self._pattern_group_at),
            "strict": self._json_safe(self.indicator_data),
            "relaxed": self._json_safe(self.relaxed_indicator_data),
            "strong": self._json_safe(getattr(self, "strong_candidate_data", [])),
            "five_one_three": self._json_safe(self.five_one_three_data),
            "stats": self._json_safe(self.five_one_three_stats),
        }
        try:
            with open(self.screen_results_state_file, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
        except Exception as error:
            self.log(f"选股结果落盘失败: {error}", "WARNING")

    def _load_screen_results(self):
        try:
            with open(self.screen_results_state_file, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return

        def _parse(text):
            try:
                return datetime.strptime(text, "%Y-%m-%d %H:%M") if text else None
            except Exception:
                return None

        self._indicator_group_at = _parse(payload.get("indicator_at"))
        self._pattern_group_at = _parse(payload.get("pattern_at"))
        # 超过保质期的组不回填——几天前的命中断章取义，不如空着等新扫描。
        if self._screen_group_fresh(self._indicator_group_at):
            self.indicator_data = payload.get("strict") or []
            self.relaxed_indicator_data = payload.get("relaxed") or []
            self.strong_candidate_data = payload.get("strong") or []
        if self._screen_group_fresh(self._pattern_group_at):
            self.five_one_three_data = payload.get("five_one_three") or []
            self.five_one_three_stats = payload.get("stats") or self.five_one_three_stats

    def scan_indicators_now(self, _):
        """手动快扫：前200、只更新严格/宽松两档。"""
        threading.Thread(target=self.maybe_refresh_indicators, args=("indicators",), daemon=True).start()

    def scan_patterns_now(self, _):
        """手动深扫：全市场趋势+513（顺带全量刷新指标）。"""
        threading.Thread(target=self.maybe_refresh_indicators, args=("patterns",), daemon=True).start()

    def _refresh_observation_pool(self, hits=(), at=None):
        at = at or datetime.now()
        with self._observation_refresh_lock:
            self._observation_next_refresh_at = at + timedelta(minutes=90)
            try:
                targets = {record["code"] for record in self.observation_pool.active_records()}
                targets.update(hit["code"] for hit in hits if re.fullmatch(r"(?:sh|sz)\d{6}", str(hit.get("code", ""))))
                histories, names, errors = {}, {}, []
                benchmark = []
                if targets:
                    try:
                        benchmark = self.get_stock_analysis_history("sh000001", data_length=400).to_dict("records")
                    except Exception as error:
                        errors.append(f"大盘日线：{error}")

                    def fetch_candidate(code):
                        issues = []
                        try:
                            quote = self.get_stock_data(code) or {}
                        except Exception as error:
                            quote = {}
                            issues.append(f"名称获取失败：{error}")
                        try:
                            bars = self.get_stock_analysis_history(code, data_length=400).to_dict("records")
                        except Exception as error:
                            bars = []
                            issues.append(str(error))
                        return quote.get("name"), bars, issues

                    with ThreadPoolExecutor(max_workers=4) as executor:
                        futures = {executor.submit(fetch_candidate, code): code for code in targets}
                        for future in as_completed(futures):
                            code = futures[future]
                            try:
                                name, bars, issues = future.result()
                                histories[code] = bars
                                if name:
                                    names[code] = name
                                errors.extend(f"{code}：{issue}" for issue in issues)
                            except Exception as error:
                                errors.append(f"{code}：{error}")
                self.observation_pool.review(hits, at, histories=histories, benchmark=benchmark, names=names)
                self._observation_entry_histories = histories
                self._observation_entry_benchmark = benchmark
                self._observation_entry_context_date = at.strftime("%Y-%m-%d")
                pending = [record for record in self.observation_pool.active_records()
                           if record["data_status"].startswith("待复核")]
                if pending:
                    errors.append(f"{len(pending)}只等待有效日线复核")
                self._observation_last_error = "；".join(errors[:3]) or None
                if not errors:
                    next_review = at.replace(hour=15, minute=10, second=0, microsecond=0)
                    if next_review <= at:
                        next_review += timedelta(days=1)
                    self._observation_next_refresh_at = next_review
                report_path = os.path.join(self.log_dir, "_screen_observation.txt")
                with open(report_path, "w", encoding="utf-8") as handle:
                    handle.write(self.observation_pool.report())
                self.log(f"观察池复核完成：{len(self.observation_pool.active_records())}只跟踪中", "INFO")
            except Exception as error:
                self._observation_last_error = str(error)
                self.log(f"观察池复核失败：{error}", "WARNING")

    def maybe_refresh_observation_pool(self, now=None):
        now = now or datetime.now()
        if not self.observation_pool.active_records() or self._observation_refresh_lock.locked():
            return
        if self._observation_next_refresh_at and now < self._observation_next_refresh_at:
            return
        if now.weekday() >= 5 or 6 <= now.hour < 15 or (now.hour == 15 and now.minute < 10):
            return
        self._observation_next_refresh_at = now + timedelta(minutes=90)
        threading.Thread(target=self._refresh_observation_pool, args=((), now), daemon=True).start()

    def _maybe_check_observation_entries(self, now=None):
        now = now or datetime.now()
        if not is_entry_session(now) or not self.observation_pool.active_records():
            return
        if self._observation_entry_lock.locked():
            return
        if self._observation_entry_next_at and now < self._observation_entry_next_at:
            return
        self._observation_entry_next_at = now + timedelta(seconds=60)
        threading.Thread(target=self._check_observation_entry_alerts, daemon=True).start()

    def _check_observation_entry_alerts(self, at=None):
        now = at or datetime.now()
        if not is_entry_session(now):
            return
        if not self._observation_entry_lock.acquire(blocking=False):
            return
        try:
            if (self._observation_entry_context_date != now.strftime("%Y-%m-%d")
                    or not self._observation_entry_benchmark
                    or any(record.get("observation_days", 0) >= 2 and record.get("data_status", "").startswith("待复核")
                           for record in self.observation_pool.active_records())):
                self._refresh_observation_pool(at=now)
            with self._observation_refresh_lock:
                records = [record for record in self.observation_pool.active_records()
                           if record.get("observation_days", 0) >= 2
                           and record.get("last_entry_alert", {}).get("date") != now.strftime("%Y-%m-%d")]
                histories = dict(self._observation_entry_histories)
                benchmark = list(self._observation_entry_benchmark)
            if not records:
                return
            benchmark_quote = self.get_stock_data("sh000001")
            errors = []

            def fetch_evidence(record):
                code = record["code"]
                bars = histories.get(code)
                if not bars:
                    bars = self.get_stock_analysis_history(code, data_length=400).to_dict("records")
                quote = self.get_stock_data(code)
                snapshot = fetch_stock_flow_snapshot(code)
                return bars, quote, snapshot

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {executor.submit(fetch_evidence, record): record for record in records}
                for future in as_completed(futures):
                    record = futures[future]
                    code = record["code"]
                    try:
                        bars, quote, snapshot = future.result()
                        checked_at = at or datetime.now()
                        alert = self._observation_entry_tracker.evaluate(
                            record=record, bars=bars, quote=quote, benchmark_bars=benchmark,
                            benchmark_quote=benchmark_quote, flow=snapshot, now=checked_at,
                        )
                        if not quote or snapshot is None:
                            errors.append(f"{code}行情或资金数据暂缺，等待确认")
                        if alert:
                            with self._observation_refresh_lock:
                                current = self.observation_pool.records.get(code) or {}
                                if (current.get("status") not in {"observing", "ready"}
                                        or current.get("first_observed_date") != alert["first_observed_date"]
                                        or current.get("data_status") != "已复核"
                                        or current.get("last_entry_alert", {}).get("date") == alert["date"]):
                                    continue
                                review_recorded = self._entry_review_call("record", alert)
                                sent = False
                                try:
                                    sent = self._notify(alert["title"], alert["subtitle"], alert["body"], level="info", sound=True)
                                finally:
                                    if review_recorded:
                                        self._entry_review_call("delivery", alert, sent)
                                if not sent:
                                    errors.append(f"{code}通知发送失败，下一轮重试")
                                    continue
                                self._observation_entry_tracker.acknowledge(alert)
                                self.observation_pool.record_entry_alert(alert)
                                with open(os.path.join(self.log_dir, "_screen_observation.txt"), "w", encoding="utf-8") as handle:
                                    handle.write(self.observation_pool.report())
                                self._dispatch_to_main(lambda event=alert: self._show_observation_entry_popup(event))
                                self.log(f"观察池买点提醒：{code} {alert['body']}", "INFO")
                    except Exception as error:
                        self._observation_entry_tracker.reset(code)
                        errors.append(f"{code}：{error}")
            self._observation_entry_last_error = "；".join(errors[:3]) or None
            if errors:
                self._observation_entry_next_at = now + timedelta(seconds=120)
        except Exception as error:
            for record in self.observation_pool.active_records():
                self._observation_entry_tracker.reset(record["code"])
            self._observation_entry_last_error = str(error)
            self._observation_entry_next_at = now + timedelta(seconds=120)
            self.log(f"观察池买点检测失败：{error}", "WARNING")
        finally:
            self._observation_entry_lock.release()

    def _init_entry_review(self):
        self._entry_review = None
        self._entry_review_error = None
        try:
            if EntryReviewService is None:
                raise RuntimeError(_ENTRY_REVIEW_IMPORT_ERROR or "买点复盘模块未加载")
            self._entry_review = EntryReviewService(self.log_dir, self.get_stock_analysis_history)
        except Exception as error:
            self._entry_review_error = str(error)

    def _entry_review_call(self, method, *args):
        service = getattr(self, "_entry_review", None)
        if service is None:
            return False
        try:
            return getattr(service, method)(*args)
        except Exception as error:
            self._entry_review_error = str(error)
            return False

    def refresh_entry_review_now(self, _):
        self._entry_review_call("request_review")

    def toggle_entry_review(self, _):
        service = getattr(self, "_entry_review", None)
        if service is not None:
            self._entry_review_call("set_enabled", not service.enabled)
            self.update_indicator_menu()

    def show_entry_review_report(self, _):
        service = getattr(self, "_entry_review", None)
        if service is None:
            return
        try:
            if service.report_path.exists():
                subprocess.Popen(["open", "-a", "TextEdit", str(service.report_path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self._entry_review_call("request_review")
                self._alert(title="买点复盘", message="报告正在后台生成，稍后可再次打开。暂停时需先恢复复盘。")
        except Exception as error:
            self._entry_review_error = str(error)

    def _show_observation_entry_popup(self, alert):
        """观察池买点提醒的屏幕打断：统一走告警对话框（旧置顶横幅已废弃）。

        受同一套 180s 冷却/级别抬升节流：刚弹过风险对话框时不打断
        （即旧「不覆盖已有风险横幅」的语义）。
        """
        if not self._popup_should_show('info'):
            self.log("观察提醒对话框冷却中，本轮跳过", "INFO")
            return
        self._show_alert_dialog(
            alert["title"], alert["subtitle"],
            alert.get("popup_body", alert["body"]), "info",
        )

    def review_observation_pool_now(self, _):
        if self._observation_refresh_lock.locked():
            return
        threading.Thread(target=self._refresh_observation_pool, daemon=True).start()

    def show_observation_pool_report(self, _):
        path = os.path.join(self.log_dir, "_screen_observation.txt")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.observation_pool.report())
            subprocess.Popen(["open", "-a", "TextEdit", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as error:
            self._alert("观察池报告", str(error))

    @staticmethod
    def _is_trading_hours():
        """判断当前是否在数据刷新时段（开盘前1小时 ~ 休市后1小时，仅工作日）"""
        now = datetime.now()
        if now.weekday() >= 5:  # 周六日
            return False
        hour_min = now.hour * 60 + now.minute
        # 8:30(510) ~ 16:00(960)
        return 510 <= hour_min <= 960

    def _fetch_data(self):
        """在后台线程中获取数据（不更新UI，单实例串行执行）"""
        if not self._fetch_lock.acquire(blocking=False):
            self.log("上一轮数据刷新仍在执行，跳过本轮", "INFO")
            return

        try:
            self.maybe_refresh_observation_pool()
            self._maybe_check_observation_entries()
            # 首次加载不受时间限制，之后仅在交易时段刷新
            is_first_load = not hasattr(self, '_first_load_done')
            if not is_first_load and not self._is_trading_hours():
                # 非交易时段：跳过行情刷新，但仍执行定时任务
                threading.Thread(target=self.maybe_refresh_indicators, daemon=True).start()
                self._auto_refresh_summary()
                # 15:00 后才启动的场景就是从这里进来 —— 必须补一份收盘定稿，
                # 否则报告会停在最后一次盘中槽位。
                self._maybe_finalize_daily_report()
                # 菜单里的龙头涨跌幅/相位仍要出得来：冷启动时板块反查表要扫几十次
                # 接口，首轮可能还没配到龙头代码 → 行情缓存空。只在缓存空时补拉，
                # 拿到就停，不在非交易时段轮询行情。
                if not self._leader_quote_cache:
                    self._check_sector_leaders()
                return

            # 获取A股数据（自选股 ∪ 持仓股）。
            # 盘中信号检测每 15 秒读 stock_data 缓存，持仓股若不在自选里
            # 永远拿不到实时价 → 信号被静默跳过（2026-09-30 体检发现）。
            quote_targets = {item['code']: item for item in self.watchlist}
            for position in self.portfolio.get("positions", []):
                code = position.get("code")
                if code and code not in quote_targets:
                    quote_targets[code] = {"code": code, "name": position.get("name", code)}
            for code in quote_targets:
                try:
                    data = self.get_stock_data(code)
                    if data:
                        self.stock_data[code] = data
                except Exception as e:
                    self.log(f"获取 {code} 数据失败: {e}", "ERROR")
            # 行情带回真名后，把「名称=代码」的持仓修回可读名称并落盘。
            # _fetch_data 在后台线程，菜单重建必须回主线程（本方法约定不更新 UI）。
            if self._heal_position_names():
                self._save_portfolio()
                self._dispatch_to_main(self._refresh_portfolio_update_menu)

            # 获取市场级成交额（上证 + 深证），不依赖自选股列表
            try:
                self._refresh_market_turnover()
            except Exception as e:
                self.log(f"刷新市场总成交额失败，保留上次结果: {e}", "WARNING")

            # 获取板块数据
            if self.show_sector:
                # 行业板块：每60次更新一次（15分钟）
                self.sector_update_count += 1
                if self.sector_update_count >= 60:
                    self.sector_update_count = 0
                    try:
                        new_sector_data = self.get_sector_data()
                        # 请求失败时返回结构没有 _all，保留上次有效数据。
                        if '_all' in new_sector_data:
                            self.sector_data = new_sector_data
                        else:
                            self.log("行业板块请求失败，保留上次有效数据", "WARNING")
                    except Exception as e:
                        self.log(f"获取板块数据失败，保留上次结果: {e}", "ERROR")

                # 概念板块：每60次更新一次（15分钟）
                self.concept_update_count += 1
                if self.concept_update_count >= 60:
                    self.concept_update_count = 0
                    try:
                        new_concept_data = self.get_concept_data()
                        if new_concept_data.get('_fresh'):
                            self.concept_data = new_concept_data
                        else:
                            self.log("概念板块请求失败，保留上次有效数据", "WARNING")
                    except Exception as e:
                        self.log(f"获取概念板块数据失败，保留上次结果: {e}", "ERROR")
                    # 先确保概念缓存已建立，再统计涨跌停
                    if not self._concept_summary_cache:
                        try:
                            self.get_concept_summary_data()  # 建立缓存
                        except Exception:
                            pass
                    try:
                        self._limit_stocks = self.get_limit_stocks()
                    except Exception as e:
                        self.log(f"获取涨跌停数据失败: {e}", "ERROR")

            # 指标选股/持仓分析都可能跑数十分钟（尤其全市场深扫），独立线程执行，
            # 取数锁只管行情刷新本身（2026-09-30 菜单栏冻结事故）
            threading.Thread(target=self.maybe_refresh_indicators, daemon=True).start()
            threading.Thread(target=self._maybe_refresh_portfolio_analysis, daemon=True).start()
            self._maybe_refresh_capital_flow()
            # 板块龙头封板/炸板状态（每15秒，只拉已配置龙头）
            # 各检测独立兜底：任何一个抛异常都不能连累后面的预警饿死
            # （2026-10-22 上午 check_signal 参数错误连抛 772 次，
            # 下探回升/大盘预警整段被跳过）。
            for name, fn in (
                ("板块龙头检测", self._check_sector_leaders),
                ("盘中信号检测", self._check_intraday_signals),
                ("大盘预警检测", self._check_market_alerts),
                ("下探回升检测", self._check_dip_rebound),
                ("封板事件检测", self._check_seal_events),
                ("盘中点火检测", self._check_ignition),
            ):
                try:
                    fn()
                except Exception as error:
                    self.log(f"{name}异常（本轮跳过，不影响其余检测）: {error}", "ERROR")
            # 抑制状态落盘：重启后不重报当日已触发的提醒（每轮一次小原子写）。
            try:
                self._save_tracker_states()
            except Exception as error:
                self.log(f"盘中事件状态落盘失败（不影响检测）: {error}", "WARNING")

            # 每天18:00自动生成盘面总结
            self._auto_refresh_summary()
            # 15:00 后补跑当日定稿（含程序崩溃重启、晚启动）
            self._maybe_finalize_daily_report()

            # 首次加载完成后，在主线程立即更新显示，并触发首次指标选股
            if not hasattr(self, '_first_load_done'):
                self._first_load_done = True
                rumps.Timer(self._on_initial_data_loaded, 0.001).start()

        except Exception as e:
            self.log(f"_fetch_data 异常: {e}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")
        finally:
            self._fetch_lock.release()

    @rumps.clicked("🔄 立即刷新")
    def refresh_now(self, _):
        """手动刷新"""
        self.title = "刷新中…"
        threading.Thread(target=self._fetch_data_and_refresh, daemon=True).start()

    def _fetch_data_and_refresh(self):
        """后台获取数据后，在主线程更新显示"""
        self._fetch_data()
        # 使用 rumps.Timer 的一次性回调在主线程更新显示
        rumps.Timer(self._do_refresh_display, 0.001).start()

    def _do_refresh_display(self, timer):
        """主线程更新显示（由 _fetch_data_and_refresh 触发）"""
        timer.stop()
        self.update_display()

    def _capital_flow_sector_names_for(self, code):
        self._ensure_capital_flow_concept_map()
        mapping = self._sector_map or {}
        concept_mapping = getattr(self, "_capital_flow_concept_map", {}) or {}
        text = str(code or "").lower()
        candidates = [text]
        if len(text) > 2 and text[:2] in ("sh", "sz", "bj"):
            candidates.append(text[2:])
        names = []
        for candidate in candidates:
            for name in mapping.get(candidate) or []:
                if name and name not in names:
                    names.append(name)
            for name in concept_mapping.get(candidate) or []:
                if name and name not in names:
                    names.append(name)
        return names

    def _capital_flow_concept_names(self):
        data = getattr(self, "concept_data", None)
        if not isinstance(data, dict):
            return []
        names = []
        for group in ("top", "bottom"):
            for item in data.get(group) or []:
                name = str((item or {}).get("name") or "").strip()
                if name and name not in names:
                    names.append(name)
        return names

    def _ensure_capital_flow_concept_map(self):
        names = self._capital_flow_concept_names()
        if not names:
            return getattr(self, "_capital_flow_concept_map", {}) or {}
        mapped_names = getattr(self, "_capital_flow_concept_map_names", set())
        if set(names).issubset(mapped_names):
            return getattr(self, "_capital_flow_concept_map", {}) or {}
        if getattr(self, "_capital_flow_concept_map_refreshing", False):
            return getattr(self, "_capital_flow_concept_map", {}) or {}
        self._capital_flow_concept_map_refreshing = True
        threading.Thread(
            target=self._refresh_capital_flow_concept_map,
            args=(names,),
            daemon=True,
        ).start()
        return getattr(self, "_capital_flow_concept_map", {}) or {}

    def _refresh_capital_flow_concept_map(self, names):
        mapping = {}
        try:
            import akshare as ak
            for name in names:
                try:
                    with self.requests_timeout(20):
                        frame = ak.stock_board_concept_cons_em(symbol=name)
                    for _, row in frame.iterrows():
                        raw_code = str(row.get("代码") or "").strip().lower()
                        if not raw_code:
                            continue
                        code = raw_code
                        if not raw_code.startswith(("sh", "sz", "bj")):
                            prefix = "sh" if raw_code.startswith(("6", "68")) else "bj" if raw_code.startswith(("4", "8")) else "sz"
                            code = prefix + raw_code
                        mapping.setdefault(code, []).append(name)
                except Exception as error:
                    self.log(f"获取概念成分失败 {name}: {error}", "WARNING")
            self._capital_flow_concept_map = mapping
            self._capital_flow_concept_map_names = set(names)
        except Exception as error:
            self.log(f"刷新盘中资金流概念映射失败: {error}", "WARNING")
        finally:
            self._capital_flow_concept_map_refreshing = False

    def _capital_flow_targets(self):
        self._ensure_sector_map()
        codes = []
        sectors = []
        for position in self.portfolio.get("positions", []):
            code = str(position.get("code") or "").strip()
            if code and code not in codes:
                codes.append(code)
            for name in self._capital_flow_sector_names_for(code):
                if name not in sectors:
                    sectors.append(name)
        return codes, sectors

    def _maybe_refresh_capital_flow(self, now_monotonic=None):
        if not self.portfolio.get("positions"):
            return False
        if not self._is_trading_hours():
            return False
        now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        with self._capital_flow_refresh_lock:
            if self._capital_flow_refreshing:
                return False
            if now_monotonic < self._capital_flow_next_refresh_monotonic:
                return False
            codes, sectors = self._capital_flow_targets()
            if not codes and not sectors:
                return False
            self._capital_flow_refreshing = True
            self._capital_flow_next_refresh_monotonic = (
                now_monotonic + self._capital_flow_refresh_interval_seconds
            )
        threading.Thread(
            target=self._refresh_capital_flow,
            args=(codes, sectors),
            daemon=True,
        ).start()
        return True

    def _refresh_capital_flow(self, codes, sectors, now_monotonic=None):
        now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        stock_snapshots = {}
        sector_snapshots = {}
        sector_errors = []
        try:
            for code in codes:
                snapshot = fetch_stock_flow_snapshot(code)
                if snapshot is not None:
                    stock_snapshots[code] = snapshot
            if sectors:
                for kind in ("industry", "concept"):
                    try:
                        with self.requests_timeout(20):
                            candidates = fetch_sector_flow(kind)
                        for name, snapshot in candidates.items():
                            if name in sectors and name not in sector_snapshots:
                                sector_snapshots[name] = snapshot
                    except Exception as error:
                        sector_errors.append(error)
                if sector_errors and not sector_snapshots and not stock_snapshots:
                    raise sector_errors[-1]
            now = datetime.now()
            with self._capital_flow_lock:
                for snapshot in stock_snapshots.values():
                    self._capital_flow_cache.put(snapshot)
                    fresh = self._capital_flow_cache.get(snapshot.key, now=now)
                    if fresh is not None:
                        self._capital_flow_stock_tracker.observe(fresh)
                for snapshot in sector_snapshots.values():
                    self._capital_flow_cache.put(snapshot)
                    fresh = self._capital_flow_cache.get(snapshot.key, now=now)
                    if fresh is not None:
                        self._capital_flow_sector_tracker.observe(fresh)
                self._save_capital_flow_cache_locked(now)
                self._capital_flow_last_error = None
        except Exception as error:
            self._capital_flow_last_error = str(error)
            self._capital_flow_next_refresh_monotonic = (
                now_monotonic + self._capital_flow_fail_backoff_seconds
            )
            self.log(f"盘中资金流刷新失败，保持原提醒逻辑: {error}", "WARNING")
        finally:
            self._capital_flow_refreshing = False

    @staticmethod
    def _capital_flow_snapshot_dict(snapshot):
        return {
            "key": snapshot.key,
            "name": snapshot.name,
            "kind": snapshot.kind,
            "quote_date": snapshot.quote_date,
            "as_of": snapshot.as_of.isoformat(timespec="seconds"),
            "source": snapshot.source,
            "scope": snapshot.scope,
            "session": snapshot.session,
            "net_yi": snapshot.net_yi,
            "net_ratio_pct": snapshot.net_ratio_pct,
            "inflow_yi": snapshot.inflow_yi,
            "outflow_yi": snapshot.outflow_yi,
            "large_net_yi": snapshot.large_net_yi,
            "super_large_net_yi": snapshot.super_large_net_yi,
        }

    @staticmethod
    def _capital_flow_evidence(snapshot):
        """格式化可选资金字段；缺字段时只保留净额。"""
        if snapshot is None or snapshot.net_yi is None:
            return ""
        parts = [f"{snapshot.net_yi:+.1f}亿"]
        if snapshot.net_ratio_pct is not None:
            parts.append(f"主力净占比 {snapshot.net_ratio_pct:+.2f}%")
        if snapshot.super_large_net_yi is not None:
            parts.append(f"超大单 {snapshot.super_large_net_yi:+.1f}亿")
        if snapshot.large_net_yi is not None:
            parts.append(f"大单 {snapshot.large_net_yi:+.1f}亿")
        return "，".join(parts)

    def _save_capital_flow_cache_locked(self, now):
        stocks = {}
        sectors = {}
        for position in self.portfolio.get("positions", []):
            code = str(position.get("code") or "").strip()
            snapshot = self._capital_flow_cache.get(code, now=now)
            if snapshot is not None:
                stocks[code] = self._capital_flow_snapshot_dict(snapshot)
        for code in stocks:
            for name in self._capital_flow_sector_names_for(code):
                snapshot = self._capital_flow_cache.get(name, now=now)
                if snapshot is not None:
                    sectors[name] = self._capital_flow_snapshot_dict(snapshot)
        payload = {
            "generated_at": now.isoformat(timespec="seconds"),
            "quote_date": now.strftime("%Y-%m-%d"),
            "stock_sectors": {
                code: self._capital_flow_sector_names_for(code)
                for code in stocks
            },
            "stocks": stocks,
            "sectors": sectors,
        }
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.log_dir,
                prefix=".capital_flow.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._capital_flow_file)
        except Exception as error:
            self.log(f"保存盘中资金流缓存失败: {error}", "WARNING")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _capital_flow_report_text(self, now=None):
        now = now or datetime.now()
        lines = []
        seen_sectors = set()
        with self._capital_flow_lock:
            for position in self.portfolio.get("positions", []):
                code = str(position.get("code") or "").strip()
                if not code:
                    continue
                stock = self._capital_flow_cache.get(code, now=now)
                if stock is not None:
                    direction = flow_direction(stock)
                    label = {
                        "inflow": "净流入",
                        "outflow": "净流出",
                        "neutral": "净流向中性",
                    }.get(direction, "净流向未取到")
                    lines.append(
                        f"个股 {stock.name}：平台口径主力{label} {stock.net_yi:+.1f}亿"
                        f"｜数据时间 {stock.as_of:%Y-%m-%d %H:%M}｜来源 {stock.source}"
                    )
                for sector_name in self._capital_flow_sector_names_for(code):
                    if sector_name in seen_sectors:
                        continue
                    seen_sectors.add(sector_name)
                    sector = self._capital_flow_cache.get(sector_name, now=now)
                    if sector is None:
                        continue
                    direction = flow_direction(sector)
                    label = {
                        "inflow": "净流入",
                        "outflow": "净流出",
                        "neutral": "净流向中性",
                    }.get(direction, "净流向未取到")
                    lines.append(
                        f"板块 {sector.name}：平台口径主力{label} {sector.net_yi:+.1f}亿"
                        f"｜数据时间 {sector.as_of:%Y-%m-%d %H:%M}｜来源 {sector.source}"
                    )
        if not lines:
            return "当前无当日且未过期的盘中资金流快照，未纳入持仓判断"
        return "\n".join([
            "口径：平台按成交单大小估算的主力资金，不等同于真实机构账户交易；仅作辅助确认。",
            *lines,
        ])

    def _capital_flow_signal_context(self, code, signal_type, now=None):
        now = now or datetime.now()
        with self._capital_flow_lock:
            stock = self._capital_flow_cache.get(code, now=now)
            sector = None
            sector_name = None
            for name in self._capital_flow_sector_names_for(code):
                candidate = self._capital_flow_cache.get(name, now=now)
                if candidate is not None:
                    sector = candidate
                    sector_name = name
                    break
            if stock is None or sector is None:
                return None, "", None
            stock_direction = flow_direction(stock)
            sector_direction = flow_direction(sector)
            stock_confirmed = self._capital_flow_stock_tracker.confirmed(
                code, stock_direction
            )
            sector_confirmed = self._capital_flow_sector_tracker.confirmed(
                sector.key, sector_direction
            )
            state = flow_confirmation_state(
                signal_type,
                stock,
                sector,
                stock_confirmed=stock_confirmed,
                sector_confirmed=sector_confirmed,
            )
        if state == "risk_confirmed":
            note = (
                f"资金确认：个股与{sector_name}连续净流出（平台口径主力净流出），"
                f"风险等级上调；个股 {self._capital_flow_evidence(stock)}，"
                f"板块 {self._capital_flow_evidence(sector)}"
            )
            return state, note, "danger"
        if state == "breakout_confirmed":
            note = (
                f"资金确认：个股与{sector_name}连续净流入（平台口径主力净流入）；"
                f"个股 {self._capital_flow_evidence(stock)}，"
                f"板块 {self._capital_flow_evidence(sector)}"
            )
            return state, note, None
        if (
            signal_type == "放量突破"
            and stock_confirmed
            and sector_confirmed
            and stock_direction == sector_direction == "outflow"
        ):
            note = (
                f"资金观察：价格放量突破，但个股与{sector_name}连续净流出，"
                f"上涨缺乏资金确认；个股 {self._capital_flow_evidence(stock)}，"
                f"板块 {self._capital_flow_evidence(sector)}"
            )
            return "divergence_observation", note, None
        return None, "", None

    def _load_portfolio(self):
        default = {
            "total_capital": 0,
            "available_capital": 0,
            "initial_entry_pct": 5,
            "max_position_pct": 20,
            "positions": [],
        }
        try:
            with open(self.portfolio_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                default.update(data)
                if not isinstance(default.get("positions"), list):
                    default["positions"] = []
                return default
        except (OSError, json.JSONDecodeError):
            pass
        return default

    def _resolve_trade_stock(self, keyword):
        raw = str(keyword or "").strip()
        if re.match(r"^(sh|sz|bj)\d{6}$", raw.lower()):
            return raw.lower(), raw
        results = self.search_stock(raw)
        if not results:
            raise ValueError(f"未找到股票: {raw}")
        return results[0]["code"], results[0]["name"]

    def _trade_ledger_report(self):
        lines = ["A股交易批次账本（T+1）", ""]
        if getattr(self, "_trade_ledger_error", ""):
            lines.append(f"⚠ 当前账本异常：{self._trade_ledger_error}")
        positions = {
            str(item.get("code") or "").lower(): item
            for item in self.portfolio.get("positions", [])
        }
        codes = list(positions)
        for trade in self._trade_ledger.trades:
            if trade.code not in codes:
                codes.append(trade.code)
        for code in codes:
            live = self.stock_data.get(code) or {}
            trade_date = self._current_trade_date(code, live)
            name = (positions.get(code) or {}).get("name") or code
            if not trade_date:
                lines.append(f"{name}：行情日期缺失，今日可卖量不判定。")
                continue
            summary = self._trade_ledger.position_summary(code, trade_date)
            if summary["quantity"] <= 0 and code not in positions:
                continue
            lines.append(
                f"{name}：总{summary['quantity']}股｜"
                f"今日可卖{summary['sellable_quantity']}股｜"
                f"今日新增锁定{summary['today_locked_quantity']}股"
            )
        lines.extend(["", "最近交易："])
        if not self._trade_ledger.trades:
            lines.append("尚无逐笔记录；现有持仓已迁移为历史可卖底仓。")
        for trade in self._trade_ledger.trades[-20:]:
            side = "买入" if trade.side == "buy" else "卖出"
            lines.append(
                f"{trade.trade_date} {trade.name} {side}{trade.quantity}股@{trade.price:.2f}"
            )
        return "\n".join(lines)

    def _sync_portfolio_from_trade_ledger(self):
        positions = {
            str(item.get("code") or "").lower(): item
            for item in self.portfolio.get("positions", [])
        }
        seen = set()
        for trade in self._trade_ledger.trades:
            seen.add(trade.code)
        for code in sorted(set(positions) | seen):
            stock_data = getattr(self, "stock_data", {}) or {}
            live = stock_data.get(code) or {}
            trade_date = self._current_trade_date(code, live)
            if not trade_date:
                trade_dates = [
                    trade.trade_date for trade in self._trade_ledger.trades
                    if trade.code == code
                ]
                trade_date = max(trade_dates, default="1970-01-01")
            summary = self._trade_ledger.position_summary(code, trade_date)
            if code in positions:
                positions[code]["quantity"] = summary["quantity"]
                positions[code]["cost"] = round(summary["average_cost"], 4)
            elif summary["quantity"] > 0:
                name = next(
                    (item.name for item in reversed(self._trade_ledger.trades)
                     if item.code == code),
                    code,
                )
                self.portfolio.setdefault("positions", []).append({
                    "code": code,
                    "name": name,
                    "quantity": summary["quantity"],
                    "cost": round(summary["average_cost"], 4),
                })
        self._save_portfolio()

    def _apply_portfolio_quantity_change(
        self, code, new_quantity, cost, effective_date, reason,
        refresh=True, persist=True,
    ):
        if not hasattr(self, "_trade_ledger"):
            self._trade_ledger = TradeLedger()
            self._trade_ledger.sync_legacy_positions(
                self.portfolio.get("positions", [])
            )
        normalized_code = str(code or "").lower().strip()
        if not normalized_code:
            raise ValueError("股票代码不能为空")
        try:
            target_quantity = int(new_quantity)
        except (TypeError, ValueError) as error:
            raise ValueError("持仓数量必须是非负整数") from error
        if target_quantity < 0 or target_quantity != new_quantity:
            raise ValueError("持仓数量必须是非负整数")
        summary = self._trade_ledger.position_summary(normalized_code, effective_date)
        delta = target_quantity - int(summary.get("quantity") or 0)
        positions = self.portfolio.setdefault("positions", [])
        item = next(
            (row for row in positions if str(row.get("code") or "").lower() == normalized_code),
            None,
        )
        name = (item or {}).get("name") or normalized_code
        self._trade_ledger.record_adjustment(
            normalized_code,
            name,
            effective_date,
            delta,
            cost,
            reason,
        )
        if item is None:
            item = {"code": normalized_code, "name": name}
            positions.append(item)
        previous_quantity = item.get("quantity")
        previous_cost = item.get("cost")
        item["quantity"] = target_quantity
        if float(cost or 0) > 0:
            item["cost"] = float(cost)
        if persist and not self._save_portfolio():
            item["quantity"] = previous_quantity
            item["cost"] = previous_cost
            raise ValueError("保存失败：持仓配置写入失败，本次修改未生效")
        if refresh:
            self._refresh_portfolio_update_menu()
            self._last_portfolio_analysis_slot = None
            self._maybe_refresh_portfolio_analysis()
        return self._trade_ledger.position_summary(normalized_code, effective_date)

    def show_trade_ledger(self, _):
        response = self._window(
            title="交易记录（A股T+1）",
            message=(
                "每行一笔：方向,代码或名称,YYYY-MM-DD,数量,价格\n"
                "示例：买入,北方铜业,2026-10-20,100,12.13\n"
                "当日买入不可当日卖出；只新增记录，留空表示不修改。\n\n"
                + self._trade_ledger_report()
            ),
            default_text="",
            ok="记录", cancel="关闭", dimensions=(460, 230),
        ).run()
        if not response.clicked or not response.text.strip():
            return
        try:
            drafts = parse_trade_text(response.text, self._resolve_trade_stock)
            self._trade_ledger.record_many(drafts)
            self._trade_ledger_error = ""
            self._sync_portfolio_from_trade_ledger()
        except Exception as error:
            self.log(f"记录交易失败: {error}", "WARNING")
            self._alert(title="交易记录失败", message=str(error))
            return
        self._alert(
            title="交易记录已保存",
            message=(
                f"已记录{len(drafts)}笔交易。\n"
                "盘中卖出、减仓和做T只使用今日可卖批次，当日新增仓位T+1。"
            ),
        )

    def _save_portfolio(self):
        temp_path = None
        saved = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.log_dir,
                prefix=".portfolio.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(self.portfolio, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.portfolio_file)
            # 持仓变更后解锁当日定稿。正确性其实靠 decide_report_mode 里的指纹比较，
            # 这里是双保险 —— 五处持仓写入都走本方法，比在每个 handler 里各记一次可靠。
            if (self._daily_report_state.get("portfolio_fingerprint")
                    != portfolio_fingerprint(self.portfolio.get("positions", []))):
                self._daily_report_state.pop("finalized_date", None)
            self.log(f"持仓配置已保存，共{len(self.portfolio['positions'])}只", "INFO")
            saved = True
        except Exception as error:
            self.log(f"保存持仓配置失败: {error}", "ERROR")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        return saved

    def _load_portfolio_state(self):
        try:
            with open(self.portfolio_analysis_state_file, "r", encoding="utf-8") as handle:
                return json.load(handle).get("last_slot")
        except (OSError, json.JSONDecodeError, AttributeError):
            return None

    def _save_portfolio_state(self, slot):
        try:
            with open(self.portfolio_analysis_state_file, "w", encoding="utf-8") as handle:
                json.dump({"last_slot": slot, "updated_at": datetime.now().isoformat()}, handle)
        except OSError as error:
            self.log(f"保存持仓分析状态失败: {error}", "WARNING")

    def _load_daily_report_state(self):
        try:
            with open(self.daily_report_state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError, AttributeError):
            return {}

    def _save_tracker_states(self):
        """落盘三个盘中事件 tracker 的抑制状态（重启防重报）。

        内存状态随进程消失，重启后当日已触发的提醒会重报
        （2026-10-22 15:52 重启重报「反弹观察」实录）。原子写，同 _save_daily_report_state。
        """
        payload = {}
        engine = getattr(self, "_dip_rebound_engine", None)
        if engine is not None:
            payload["dip_rebound"] = engine.export_state()
        seal = getattr(self, "_seal_tracker", None)
        if seal is not None:
            payload["seal_events"] = seal.export_state()
        ignition = getattr(self, "_ignition_tracker", None)
        if ignition is not None:
            payload["ignition"] = ignition.export_state()
        if not payload:
            return
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.log_dir,
                prefix=".intraday_trackers.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.tracker_state_file)
        except Exception as error:
            self.log(f"保存盘中事件状态失败: {error}", "WARNING")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _restore_tracker_state(self, kind, tracker):
        """从落盘状态恢复单个 tracker；文件缺失/损坏按空状态起步。"""
        try:
            with open(self.tracker_state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, AttributeError):
            return tracker
        if not isinstance(data, dict):
            return tracker
        try:
            tracker.restore_state(data.get(kind))
        except Exception as error:
            self.log(f"恢复盘中事件状态失败({kind}): {error}", "WARNING")
        return tracker

    def _save_daily_report_state(self, state):
        """原子写：坏 json 会让定稿锁丢失或重复定稿，不能直接 open 覆盖。

        参照 _save_portfolio 的 tempfile + os.replace。
        """
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.log_dir,
                prefix=".daily_report.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.daily_report_state_file)
        except Exception as error:
            self.log(f"保存今日报告状态失败: {error}", "WARNING")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _load_sector_map(self):
        """个股→板块反查表 + 板块今日涨跌。缓存到天：49 次成分股扫描太贵。

        映射和涨跌**必须同源**（同一次 stock_sector_spot）——
        get_sector_data 会过滤掉成分股 <30 的板块，拿它当行情源会让
        飞机制造/石油行业这类小板块查不到涨跌（2026-10-12 冒烟抓到）。
        """
        try:
            with open(self.sector_map_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get("date") == datetime.now().strftime("%Y-%m-%d"):
                self._sector_quotes = data.get("quotes") or {}
                self._sector_constituents = data.get("constituents") or {}
                return data.get("map") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        self._sector_quotes = {}
        self._sector_constituents = {}
        return {}

    def _save_sector_map(self, mapping, quotes, constituents=None):
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.log_dir,
                prefix=".sector_map.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(
                    {"date": datetime.now().strftime("%Y-%m-%d"),
                     "map": mapping, "quotes": quotes,
                     "constituents": constituents or {}},
                    handle, ensure_ascii=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.sector_map_file)
        except Exception as error:
            self.log(f"保存板块映射失败: {error}", "WARNING")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _ensure_sector_map(self):
        """确保反查表可用；缺失/过期就后台刷一次，立刻返回手头的（不阻塞报告）。

        成分股名单（`_sector_constituents`）必须一并有 —— 只有 code→板块 的反查表
        不够，板块技术面要靠成分股等权合成。旧缓存缺这个字段时必须重建，
        否则 `_refresh_sector_technicals` 拿不到成分股，静默产出 0 个板块。
        """
        ready = bool(self._sector_map) and bool(self._sector_constituents)
        if ready and not self._sector_map_refreshing:
            return self._sector_map
        if not self._sector_map_refreshing:
            self._sector_map_refreshing = True
            threading.Thread(target=self._refresh_sector_map, daemon=True).start()
        return self._sector_map

    def _refresh_sector_map(self):
        """扫新浪各板块成分股，建 code → [板块名] 反查表（约 49 次调用）。

        只用新浪口径 —— 同花顺板块分类法与它对不齐（2026-10-12 实测 49 个板块
        去后缀后只对得上 7 个），混用会把个股归到错误板块。
        """
        mapping = {}
        quotes = {}
        constituents = {}
        try:
            import akshare as ak
            with self.requests_timeout(30):
                spot = ak.stock_sector_spot()
            for _, row in spot.iterrows():
                label, name = row.get("label"), row.get("板块")
                if not label or not name:
                    continue
                change = row.get("涨跌幅")
                if change is not None and change == change:  # 排除 NaN
                    quotes[name] = {"change": float(change)}
                try:
                    with self.requests_timeout(20):
                        cons = ak.stock_sector_detail(sector=label)
                except Exception as error:
                    self.log(f"获取板块成分失败 {name}: {error}", "WARNING")
                    continue
                codes = []
                for _, item in cons.iterrows():
                    code = str(item.get("symbol") or "")
                    if not code:
                        continue
                    mapping.setdefault(code, []).append(name)
                    codes.append(code)
                constituents[name] = codes
            self._sector_map = mapping
            self._sector_quotes = quotes
            self._sector_constituents = constituents
            self._save_sector_map(mapping, quotes, constituents)
            self.log(
                f"板块映射已更新: {len(mapping)} 只成分股 / {len(quotes)} 个板块",
                "INFO",
            )
            # 冷启动时序：报告可能在映射就绪前就生成了，那时 holding_sectors 是空的、
            # 板块技术面根本没起建。映射好了立刻补建，否则要等下一个槽位。
            self._ensure_sector_tech(self._holding_sector_names())
        except Exception as error:
            self.log(f"板块映射刷新失败: {error}\n{traceback.format_exc()}", "WARNING")
        finally:
            self._sector_map_refreshing = False

    # ── 板块技术面：成分股等权代理指数 ──────────────────────────────
    #
    # 同花顺有 975 根行业指数日线，但它和新浪是两套分类法（2026-10-12 实测 49 vs 90
    # 个板块，去后缀后只对上 7 个），混用会归错板块 —— 所以指数自己等权合成。
    # 代理指数每天重建一次就够（M20 方向不会盘中翻），重建成本约 10 板块 × 8 根日线。

    def _load_sector_tech(self):
        try:
            with open(self.sector_tech_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get("date") == datetime.now().strftime("%Y-%m-%d"):
                return data.get("techs") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return {}

    def _save_sector_tech(self, techs):
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.log_dir,
                prefix=".sector_tech.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(
                    {"date": datetime.now().strftime("%Y-%m-%d"), "techs": techs},
                    handle, ensure_ascii=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.sector_tech_file)
        except Exception as error:
            self.log(f"保存板块技术面失败: {error}", "WARNING")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _sectors_of_holding(self, code):
        """持仓股所属板块：**手动归属优先**，没设过的才走新浪自动反查。

        自动反查表（`_sector_map`）的归属经常不准，用户在「📊 板块龙头 →
        自定义配置 / 编辑关联持仓」里设过的归属一旦存在就以它为准（**空名单也算
        设过**——表示明确无板块，自动反查不得再塞回来），反查表只当兜底。
        """
        key = str(code or "").lower().strip()
        if not key:
            return []
        store = self._holding_sector_store
        if store.has_entry(key):
            return list(store.sectors_of(key))
        self._ensure_sector_map()
        sectors = (self._sector_map or {}).get(key) or []
        if not sectors and key.startswith(("sh", "sz", "bj")):
            sectors = (self._sector_map or {}).get(key[2:]) or []
        return list(sectors)

    def _holding_sector_names(self):
        """持仓涉及的板块名（去重、保持顺序）。手动归属优先。"""
        out = []
        for position in self.portfolio.get("positions", []):
            for name in self._sectors_of_holding(position.get("code")):
                if name not in out:
                    out.append(name)
        return out

    def _ensure_sector_tech(self, sector_names):
        """确保相关板块的技术面可用；缺了就后台重建，立刻返回手头的。"""
        wanted = [n for n in (sector_names or []) if n]
        if not wanted:
            return self._sector_tech
        if self._sector_tech and self._sector_tech_refreshing:
            return self._sector_tech
        if self._sector_tech and all(n in self._sector_tech for n in wanted):
            return self._sector_tech
        if not self._sector_tech_refreshing:
            self._sector_tech_refreshing = True
            threading.Thread(
                target=self._refresh_sector_technicals,
                args=(wanted,), daemon=True,
            ).start()
        return self._sector_tech

    def _refresh_sector_technicals(self, sector_names):
        """对含持仓的板块建等权代理指数并跑 analyze_history。

        成分股日线取新浪原始价（与个股分析同口径）；每板块封顶 PROXY_CONS_CAP 只，
        优先取已有行情缓存的（盘中能带上最新价）。代理指数本身覆盖 ≥60 日才有效。
        """
        from sector_technicals import MIN_PROXY_ROWS, build_proxy_bars, summarize

        techs = dict(self._sector_tech or {})
        constituents = self._sector_constituents or {}
        try:
            # 大盘基准：relative_strength = 板块20日 − 沪指20日，
            # 不给 benchmark 的话 rs 恒为 None，「个股 vs 自己板块」算不出来。
            benchmark = None
            try:
                benchmark = self.get_stock_analysis_history("sh000001", data_length=120)
            except Exception as error:
                self.log(f"板块技术面取大盘基准失败: {error}", "WARNING")
            for name in sector_names:
                codes = constituents.get(name) or []
                if not codes:
                    continue
                # 优先取行情缓存里有的（能带上最新价），不足再补
                cached = [c for c in codes if c in self.stock_data]
                picked = (cached + [c for c in codes if c not in cached])[:PROXY_CONS_CAP]
                frames = []
                for code in picked:
                    try:
                        frames.append(self.get_stock_analysis_history(code, data_length=120))
                    except Exception as error:
                        self.log(f"板块成分日线失败 {code}: {error}", "WARNING")
                proxy = build_proxy_bars(frames, min_rows=MIN_PROXY_ROWS)
                if proxy is None:
                    self.log(f"板块代理指数不足 {MIN_PROXY_ROWS} 日，跳过 {name}", "WARNING")
                    continue
                try:
                    result = analyze_history(
                        proxy, f"sector_{name}", name, benchmark_history=benchmark,
                    )
                    techs[name] = summarize(result)
                except Exception as error:
                    self.log(f"板块技术分析失败 {name}: {error}", "WARNING")
            self._sector_tech = techs
            self._save_sector_tech(techs)
            self.log(f"板块技术面已更新: {len(techs)} 个板块", "INFO")
            # 冷启动：报告可能在技术面就绪前就生成了（那时只写了「缺代理指数」）。
            # 解锁让下一轮 tick 重出，把板块技术面补进去。
            if techs:
                cached = (self._last_daily_report or {}).get("overview") or {}
                if not (cached.get("sector_tech") or {}).get("covered"):
                    self.log("板块技术面就绪，重出今日报告以补入", "INFO")
                    self._daily_report_state.pop("finalized_date", None)
                    self._last_finalize_attempt_at = None
        except Exception as error:
            self.log(f"板块技术面刷新失败: {error}\n{traceback.format_exc()}", "WARNING")
        finally:
            self._sector_tech_refreshing = False

    def show_daily_report(self, _):
        """打开「📦 持仓信息」全文。

        这是 2026-10-12 取代 show_portfolio_info 的入口：菜单上只留这一份报告，
        原「📦 持仓信息」的逐股技术细节收进报告第四节（每股 14 行压到 5 行）。
        原始 dump `_portfolio_analysis.txt` 仍在写，但不进菜单，只作排查用。
        """
        if not self.portfolio.get("positions"):
            self._alert(title="📦 持仓信息",
                        message="尚未配置监控股票，请先点击“⚙️ 持仓设置”。")
            return
        if not os.path.exists(self.daily_report_file):
            self._alert(
                title="📦 持仓信息",
                message="还没有报告。盘中每半小时生成一次，收盘后出定稿；"
                        "刚改过持仓也会在下一轮重算。",
            )
            return
        # 抄 _show_trade_calculation_in_textedit 的兜底：失败要弹窗，
        # 原 show_portfolio_info 只打日志，用户点了一片死寂。
        try:
            subprocess.Popen(
                ["open", "-a", "TextEdit", self.daily_report_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as error:
            self.log(f"打开今日报告失败: {error}\n{traceback.format_exc()}", "ERROR")
            self._alert(title="📦 持仓信息",
                        message=f"打开报告失败：{error}")

    def show_portfolio_capital_settings(self, _):
        """只设置总资金，不改变现有股票列表。"""
        response = self._window(
            title="设置总资金",
            message="请输入总资金（可留空，单位：元）：",
            default_text=str(self.portfolio.get("total_capital") or ""),
            ok="保存", cancel="取消", dimensions=(220, 28),
        ).run()
        if not response.clicked:
            return
        try:
            new_total_capital = float(response.text.strip() or 0)
        except ValueError:
            self._alert(title="格式错误", message="总资金必须是数字。")
            return
        previous_total_capital = self.portfolio.get("total_capital")
        self.portfolio["total_capital"] = new_total_capital
        if not self._save_portfolio():
            self.portfolio["total_capital"] = previous_total_capital
            self._alert(
                title="保存失败",
                message="保存失败：持仓配置写入失败，总资金未更新。",
            )
            return
        self._alert(title="总资金已保存", message="总资金已更新，现有持仓列表未改变。")

    def _resolve_portfolio_stock(self, keyword):
        keyword = (keyword or "").strip()
        code = keyword.lower()
        matched = re.match(r"^(sh|sz|bj)?(\d{6})$", code)
        if matched:
            bare = matched.group(2)
            # 带前缀以用户写的为准；裸代码按首位补市场（6/9→沪，4/8→北，其余→深）
            prefix = matched.group(1) or (
                "sh" if bare[0] in ("6", "9") else "bj" if bare[0] in ("4", "8") else "sz"
            )
            full = prefix + bare
            known = (self.stock_data.get(full) or {}).get("name")
            if known:
                return full, known
            # 拿代码搜一次真名：菜单里光秃秃的代码不好认（搜不到就用代码兜底）
            results = self.search_stock(bare) or []
            for item in results:
                if str(item.get("code") or "").lower() == full:
                    return full, item.get("name") or keyword
            return full, keyword
        for item in self.portfolio.get("positions", []):
            if item.get("name") == keyword or item.get("code", "").lower() == code:
                return item["code"], item.get("name", keyword)
        results = self.search_stock(keyword)
        if not results:
            return None, None
        return results[0]["code"], results[0]["name"]

    def _display_stock_name(self, code, name=None):
        """展示用的股票名称：名称缺失或等于代码时回落到行情缓存里的真名。

        纯代码录入遇搜索接口不可达时，name 会先落成代码（见 _resolve_portfolio_stock），
        菜单和盘中提醒就会显示一串代码。这里统一兜底，保证看到的是可读名称。
        """
        normalized = str(code or "").strip().lower()
        candidate = str(name or "").strip()
        if candidate and candidate.lower() != normalized:
            return candidate
        live = ((getattr(self, "stock_data", None) or {}).get(normalized) or {})
        live_name = str(live.get("name") or "").strip()
        if live_name and live_name.lower() != normalized:
            return live_name
        return candidate or normalized

    def _heal_position_names(self):
        """把「名称=代码」的持仓名补成真名并落盘，返回是否有改动。

        行情每轮都会带回真名，顺手修一次配置，菜单/提醒下次就不再显示代码。
        """
        changed = False
        for item in self.portfolio.get("positions", []):
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            current = str(item.get("name") or "").strip()
            if current and current.lower() != code.lower():
                continue
            healed = self._display_stock_name(code, current)
            if healed and healed.lower() != code.lower():
                item["name"] = healed
                changed = True
        return changed

    @staticmethod
    def _split_portfolio_input(text):
        normalized = (
            text.replace("；", ";")
            .replace("，", ",")
            .replace("\r\n", "\n")
            .replace("\n", ";")
        )
        return [item.strip() for item in normalized.split(";") if item.strip()]

    @staticmethod
    def _parse_portfolio_numbers(quantity_value, cost_value):
        try:
            quantity = float(quantity_value)
            cost = float(cost_value)
        except (TypeError, ValueError) as error:
            raise ValueError from error
        if not (
            np.isfinite(quantity)
            and np.isfinite(cost)
            and quantity >= 0
            and cost >= 0
        ):
            raise ValueError
        return quantity, cost

    @staticmethod
    def _format_portfolio_input_number(value):
        value = float(value)
        if value.is_integer():
            return str(int(value))
        return repr(value)

    def _refresh_portfolio_update_menu(self):
        if getattr(self, "_portfolio_update_menu_ready", False):
            self._portfolio_update_menu_item.clear()
        self._portfolio_update_menu_ready = True
        positions = self.portfolio.get("positions", [])
        if not positions:
            self._portfolio_update_menu_item.add(
                rumps.MenuItem("暂无监控股票", callback=None)
            )
        for item in positions:
            code = str(item.get("code", ""))
            quantity = self._safe_float(item.get("quantity"))
            cost = self._safe_float(item.get("cost"))
            display_name = self._display_stock_name(code, item.get("name"))
            menu_item = rumps.MenuItem(
                f"{display_name} ({code}) · {quantity:,.0f}股 @ {cost:.2f}",
                callback=lambda sender, position_code=code: (
                    self.show_portfolio_position_update(sender, position_code)
                ),
            )
            self._portfolio_update_menu_item.add(menu_item)
        self._portfolio_update_menu_item.add(
            rumps.MenuItem("📋 批量修改", callback=self.show_portfolio_update)
        )
        self._portfolio_update_menu_item.add(
            rumps.MenuItem(
                "🛑 移出监控（保留账本）",
                callback=self.show_portfolio_unmonitor,
            )
        )

    # ── 板块龙头菜单（体现与持仓股的关联）────────────────────────────────

    def _leader_entry_title(self, entry, code):
        """龙头条目标题：名称 +涨跌幅 · 成交额X亿 · [相位]（不显示股票代码）。

        涨跌幅与成交额都取**最近一轮批量行情**（手动加的龙头也能有）；
        行情缺失时成交额回退推荐时存的快照，涨跌幅干脆不写、不硬凑 0%。
        """
        quote = (self._leader_quote_cache or {}).get(code) or {}
        change = quote.get("pct")
        try:
            change = float(change) if change is not None else None
        except (TypeError, ValueError):
            change = None
        turnover_yi = entry.get("turnover_yi")
        try:
            amount = float(quote.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount > 0:
            turnover_yi = round(amount / 1e8, 2)   # 累计成交额（元）→ 亿，盘中实时
        phase = self._leader_guard.phase_of(code) if code else LeaderPhase.NONE
        # NONE 不标「未封板」——菜单里十个条目九个未封板是纯噪音
        phase_label = "" if phase == LeaderPhase.NONE else (LeaderPhaseLabel.get(phase) or "")
        return format_leader_menu_label(
            entry.get("name") or code,
            code,
            change_pct=change,
            phase_label=phase_label,
            turnover_yi=turnover_yi,
        )

    def _update_leader_menu_labels(self):
        """盘中只改条目标题（涨跌幅/相位），不 clear 重建——不打扰展开中的菜单。"""
        try:
            refs = self._leader_menu_item_refs or {}
            if not refs:
                self.log("龙头菜单标题跳过：无可更新条目", "DEBUG")
                return
            sample = ""
            updated = 0
            for (sector_name, code), item in refs.items():
                entry = None
                for candidate in self._leader_store.leaders_of(sector_name):
                    if str(candidate.get("code") or "").lower() == code:
                        entry = candidate
                        break
                if entry is None:
                    continue
                title = self._leader_entry_title(entry, code)
                replace_menu_item_title(item, title)
                updated += 1
                if not sample:
                    sample = title
            # 首轮必记一条（收盘后可能只有这一轮），之后每 4 次留痕、不刷屏
            if self._leader_menu_tick == 1 or self._leader_menu_tick % 4 == 0:
                self.log(f"龙头菜单标题已更新 {updated}/{len(refs)} 项，例: {sample}", "DEBUG")
        except Exception as error:
            self.log(f"更新龙头菜单标题失败: {error}", "WARNING")

    def _refresh_sector_leader_menu(self):
        """只列持仓相关板块；每板块显示龙头涨跌幅/相位与关联持仓。"""
        try:
            if getattr(self, "_sector_leader_menu_ready", False):
                self._sector_leader_menu_item.clear()
            self._sector_leader_menu_ready = True
            self._leader_menu_item_refs = {}

            sectors = self._holding_sector_names_for_leaders()
            if not sectors:
                self._sector_leader_menu_item.add(
                    rumps.MenuItem("暂无持仓板块（先在下方自定义配置归属）", callback=None)
                )
            else:
                for sector_name in sectors:
                    sector_item = rumps.MenuItem(sector_name, callback=None)
                    leaders = self._leader_store.leaders_of(sector_name)
                    if not leaders:
                        sector_item.add(rumps.MenuItem("（未配置龙头）", callback=None))
                    for entry in leaders:
                        code = str(entry.get("code") or "").lower()
                        # 纯展示、不可点击：改动走「✏️ 编辑龙头列表…」，
                        # 点条目即删太容易误触（用户 2026-10-19 要求）
                        menu_item = rumps.MenuItem(
                            self._leader_entry_title(entry, code),
                            callback=None,
                        )
                        if code:
                            self._leader_menu_item_refs[(sector_name, code)] = menu_item
                        sector_item.add(menu_item)
                    # 关联持仓：一个板块可挂多只、各占一行，同样纯展示不可点击
                    for position in self.portfolio.get("positions", []):
                        pcode = str(position.get("code") or "").lower()
                        if not pcode or sector_name not in self._sectors_of_holding(pcode):
                            continue
                        quantity = self._safe_float(position.get("quantity"), 0)
                        manual = "·手动" if self._holding_sector_store.is_manual(pcode) else ""
                        sector_item.add(rumps.MenuItem(
                            f"📎 {position.get('name') or pcode}（{quantity:.0f}股{manual}）",
                            callback=None,
                        ))
                    sector_item.add(rumps.MenuItem(
                        "✏️ 编辑关联持仓…",
                        callback=lambda sender, s=sector_name: self.show_sector_holding_edit(sender, s),
                    ))
                    sector_item.add(rumps.MenuItem(
                        "✏️ 编辑龙头列表…",
                        callback=lambda sender, s=sector_name: self.show_sector_leader_edit(sender, s),
                    ))
                    sector_item.add(rumps.MenuItem(
                        "➕ 手动添加龙头…",
                        callback=lambda sender, s=sector_name: self.show_sector_leader_add(sender, s),
                    ))
                    sector_item.add(rumps.MenuItem(
                        "🔄 重新自动推荐",
                        callback=lambda sender, s=sector_name: self.show_sector_leader_reauto(sender, s),
                    ))
                    self._sector_leader_menu_item.add(sector_item)

            # 配置入口始终可见：没有持仓板块时也能先配归属
            self._sector_leader_menu_item.add(rumps.MenuItem(
                "⚙️ 自定义配置（持仓→板块→龙头）…",
                callback=self.show_sector_leader_config,
            ))
            self._sector_leader_menu_item.add(rumps.MenuItem(
                "口径：手动归属优先；每日10:00后按成交额推荐top5（排除ST），手动改过即锁定",
                callback=None,
            ))
        except Exception as error:
            self.log(f"刷新板块龙头菜单失败: {error}", "WARNING")

    def _apply_sector_leader_config_records(self, records):
        """把解析后的配置落到两个 store（可单测）。返回 (saved 描述列表, 未处理列表)。

        规则：龙头挂在行里**第一个**板块上（多板块时避免重复/歧义）；
        所属板块填 `-` 的行只清手动归属，不碰龙头。
        """
        saved, errors = [], []
        for record in records:
            stock_keyword = record["stock"]
            code, name = self._resolve_portfolio_stock(stock_keyword)
            if not code:
                errors.append(f"{stock_keyword}：未找到")
                continue
            code = code.lower()
            if record["clear"]:
                self._holding_sector_store.clear(code)
                saved.append(f"{name or code}：已清除手动归属")
                continue
            if not record["sectors"]:
                errors.append(f"{stock_keyword}：缺所属板块")
                continue
            primary = record["sectors"][0]
            leader_names = []
            for keyword in record["leaders"]:
                lcode, lname = self._resolve_portfolio_stock(keyword)
                if not lcode:
                    errors.append(f"{keyword}：龙头未找到")
                    continue
                self._leader_store.add_leader(primary, lcode.lower(), lname or keyword)
                leader_names.append(lname or keyword)
            self._holding_sector_store.set_sectors(code, record["sectors"], name or stock_keyword)
            suffix = f"（龙头：{'、'.join(leader_names)}）" if leader_names else ""
            saved.append(f"{name or code} → {primary}{suffix}")
        return saved, errors

    def _replace_sector_leaders(self, sector_name, keywords):
        """整体替换某板块的龙头个股（「✏️ 编辑龙头列表」用）。返回 (names, 未处理)。

        输入框清空 = 用户就是要删光；但**全部**关键字都解析失败时保留原列表，
        不能让一个打错的输入静默清空配置。预填的「名称 代码」行保存后只丢代码，
        名称优先沿用原列表里的，别让菜单退化成一串代码。
        """
        previous = {
            str(item.get("code") or "").lower(): item.get("name")
            for item in self._leader_store.leaders_of(sector_name)
        }
        leaders, names, errors = [], [], []
        seen = set()
        for keyword in keywords or []:
            code, name = self._resolve_portfolio_stock(keyword)
            if not code:
                errors.append(f"{keyword}：未找到")
                continue
            code = code.lower()
            if code in seen:
                continue
            seen.add(code)
            resolved = (name or "").strip()
            if not resolved or resolved.lower() == code:
                resolved = previous.get(code) or ""
            display = resolved or keyword
            leaders.append({"code": code, "name": display, "source": "manual"})
            names.append(display)
        if keywords and not leaders and errors:
            return [], errors          # 全解析失败：不动原列表
        self._leader_store.set_leaders(sector_name, leaders, pinned=True)
        return names, errors

    def _replace_sector_holdings(self, sector_name, keywords):
        """编辑某板块的**关联持仓**（一个板块可挂多只）。返回 (留下, 摘掉, 未处理)。

        只动本板块的归属，不碰每只持仓在其它板块的归属：
        - 名单里的：并入本板块；
        - 名单外、当前挂在本板块的：摘掉本板块（摘空 = 明确无板块，自动反查不许塞回来）；
        - 关键字**全部**解析失败时不动现有归属（打错字不许清空配置）。
        """
        store = self._holding_sector_store

        def effective(code):
            # 手动记录优先（含空名单）；没有记录才拿自动反查的当前值做底
            if store.has_entry(code):
                return list(store.sectors_of(code))
            return self._sectors_of_holding(code)

        resolved, errors = [], []
        seen = set()
        for keyword in keywords or []:
            code, name = self._resolve_portfolio_stock(keyword)
            if not code:
                errors.append(f"{keyword}：未找到")
                continue
            code = code.lower()
            if code in seen:
                continue
            seen.add(code)
            resolved.append((code, name or keyword))
        if keywords and not resolved and errors:
            return [], [], errors

        kept = []
        for code, name in resolved:
            names = effective(code)
            if sector_name not in names:
                names.append(sector_name)
            store.set_sectors(code, names, name)
            kept.append(name)

        dropped = []
        for position in self.portfolio.get("positions", []):
            pcode = str(position.get("code") or "").lower()
            if not pcode or pcode in seen:
                continue
            current = effective(pcode)
            if sector_name not in current:
                continue
            remaining = [name for name in current if name != sector_name]
            store.set_sectors(pcode, remaining, position.get("name") or "")
            dropped.append(position.get("name") or pcode)
        return kept, dropped, errors

    def show_sector_holding_edit(self, _, sector_name):
        """编辑该板块关联的持仓股：预填当前名单，一个板块可挂多只、也可摘掉。"""
        current = []
        for position in self.portfolio.get("positions", []):
            pcode = str(position.get("code") or "").lower()
            if not pcode or sector_name not in self._sectors_of_holding(pcode):
                continue
            name = str(position.get("name") or "").strip()
            current.append(f"{name} {pcode}" if name and name.lower() != pcode else pcode)
        response = self._window(
            title=f"编辑关联持仓 · {sector_name}",
            message=(
                "每行一只持仓股，可写名称或代码；一个板块可挂多只持仓\n"
                "保存后：名单里的并入本板块，名单外的从本板块摘掉\n"
                "只动本板块的归属，不影响该持仓在其它板块的归属"
            ),
            default_text="\n".join(current),
            ok="保存", cancel="取消", dimensions=(320, 140),
        ).run()
        if not response.clicked:
            return
        keywords = parse_leader_list_text(response.text or "")
        kept, dropped, errors = self._replace_sector_holdings(sector_name, keywords)
        if not self._holding_sector_store.save():
            self._alert(title="保存失败", message="持仓归属写入失败，改动未生效。")
            return
        self._refresh_sector_leader_menu()
        lines = []
        if kept:
            lines.append(f"{sector_name} 关联持仓：" + "、".join(kept))
        elif not keywords:
            lines.append(f"{sector_name} 已无关联持仓。")
        if dropped:
            lines.append("已从本板块摘掉：" + "、".join(dropped))
        if errors:
            lines.append("未处理：" + "、".join(errors))
        self._alert(title="关联持仓已更新", message="\n".join(lines))
        self.log(f"编辑关联持仓: {sector_name} 留下{kept} 摘掉{dropped}", "INFO")

    def show_sector_leader_edit(self, _, sector_name):
        """编辑该板块的龙头个股列表：预填当前名单，改完整体替换（板块转手动维护）。"""
        current = []
        for entry in self._leader_store.leaders_of(sector_name):
            code = str(entry.get("code") or "").lower()
            name = str(entry.get("name") or "").strip()
            current.append(f"{name} {code}".strip() if name and name != code else code)
        response = self._window(
            title=f"编辑龙头列表 · {sector_name}",
            message=(
                "每行一只龙头，可写名称或代码（如 亨通光电 sh600487）\n"
                "增删改完点保存即整体替换；清空保存表示删光龙头\n"
                "保存后该板块转手动维护，自动推荐不再覆盖"
            ),
            default_text="\n".join(current),
            ok="保存", cancel="取消", dimensions=(320, 140),
        ).run()
        if not response.clicked:
            return
        keywords = parse_leader_list_text(response.text or "")
        names, errors = self._replace_sector_leaders(sector_name, keywords)
        if not self._leader_store.save():
            self._alert(title="保存失败", message="龙头列表写入失败，改动未生效。")
            return
        self._refresh_sector_leader_menu()
        lines = []
        if names:
            lines.append(f"已设为 {sector_name} 龙头：" + "、".join(names))
        elif not keywords:
            lines.append(f"{sector_name} 龙头列表已清空。")
        if errors:
            lines.append("未处理：" + "、".join(errors))
            lines.append("（未处理的行不影响已保存部分）")
        self._alert(title="龙头列表已更新", message="\n".join(lines))
        self.log(f"编辑龙头列表: {sector_name} → {names or '（空）'}", "INFO")

    def show_sector_leader_config(self, _):
        """一次性自定义：持仓股、所属板块、板块中的龙头（手动归属优先于自动反查）。"""
        response = self._window(
            title="自定义板块龙头",
            message=(
                "每行：持仓股, 所属板块, 龙头1|龙头2\n"
                "· 持仓股可多只（| 分隔），龙头可省略（多只用 | 分隔）\n"
                "· 所属板块填 - 表示清除手动归属（恢复自动识别）\n"
                "示例：中国卫星, 军工, 中国船舶|中航沈飞\n"
                "　　　中天科技|东方电缆, 通信设备, 亨通光电"
            ),
            default_text="",
            ok="保存", cancel="取消", dimensions=(380, 120),
        ).run()
        if not response.clicked:
            return
        records = parse_holding_sector_config(response.text or "")
        if not records:
            self._alert(
                title="没有可保存的内容",
                message="请按「持仓股, 所属板块, 龙头」格式输入，一行一条。",
            )
            return

        saved, errors = self._apply_sector_leader_config_records(records)
        holdings_ok = self._holding_sector_store.save()
        leaders_ok = self._leader_store.save()
        self._refresh_sector_leader_menu()
        if not holdings_ok or not leaders_ok:
            self._alert(title="保存失败", message="配置写入失败，改动未生效。")
            return
        lines = []
        if saved:
            lines.append("已保存：")
            lines.extend(f"· {item}" for item in saved)
        if errors:
            lines.append("未处理：")
            lines.extend(f"· {item}" for item in errors)
        self._alert(title="板块龙头配置已更新", message="\n".join(lines))
        self.log(f"板块龙头自定义配置: 保存{len(saved)}条 / 未处理{len(errors)}条", "INFO")

    def show_sector_leader_add(self, _, sector_name):
        """手动把某只股票加为该板块龙头（会把板块标记为手动锁定）。"""
        response = self._window(
            title=f"添加龙头 · {sector_name}",
            message="请输入龙头股票代码或名称（如 sh600487 或 亨通光电）：",
            default_text="",
            ok="添加", cancel="取消", dimensions=(280, 28),
        ).run()
        if not response.clicked:
            return
        keyword = (response.text or "").strip()
        if not keyword:
            return
        code, name = self._resolve_portfolio_stock(keyword)
        if not code:
            self._alert(title="未找到", message=f"没有匹配「{keyword}」的股票。")
            return
        self._leader_store.add_leader(sector_name, code.lower(), name or keyword)
        if self._leader_store.save():
            self._refresh_sector_leader_menu()
            self._alert(
                title="龙头已添加",
                message=f"{sector_name}：{name or code} 已设为龙头参考（该板块改为手动维护）。",
            )
        else:
            self._alert(title="保存失败", message="龙头列表写入失败，改动未生效。")

    def show_sector_leader_reauto(self, _, sector_name):
        """解除手动锁定，重新按成交额自动推荐。"""
        entry = self._leader_store.sector(sector_name)
        entry["pinned"] = False
        self._leader_auto_checked = None   # 允许本轮重新跑推荐
        if self._leader_store.save():
            self._maybe_propose_sector_leaders()
            self._refresh_sector_leader_menu()
            self._alert(
                title="已重新推荐",
                message=f"{sector_name} 龙头已按当日成交额重新推荐。",
            )
        else:
            self._alert(title="保存失败", message="龙头列表写入失败。")

    def show_portfolio_add(self, _):
        """追加监控股票，不覆盖已有列表。"""
        response = self._window(
            title="追加监控股票",
            message=(
                "每项：代码或名称,数量,成本[,生效日期]\n"
                "支持中文/英文标点和换行；观察股数量填0\n"
                "示例：中国卫星；特变电工,1000,18.2,2026-10-20"
            ),
            default_text="",
            ok="追加", cancel="取消", dimensions=(360, 80),
        ).run()
        if not response.clicked:
            return

        existing = {item.get("code", "").lower() for item in self.portfolio.get("positions", [])}
        original_positions = list(self.portfolio.get("positions", []))
        additions = []
        added = []
        skipped = []
        for raw_item in self._split_portfolio_input(response.text):
            fields = [field.strip() for field in raw_item.split(",")]
            if len(fields) not in (1, 3, 4):
                self._alert(title="格式错误", message=f"无法解析：{raw_item}")
                return
            code, name = self._resolve_portfolio_stock(fields[0])
            if not code:
                self._alert(title="未找到股票", message=f"没有找到：{fields[0]}")
                return
            if code.lower() in existing:
                skipped.append(name)
                continue
            try:
                quantity, cost = self._parse_portfolio_numbers(
                    fields[1] if len(fields) >= 3 else 0,
                    fields[2] if len(fields) >= 3 else 0,
                )
            except ValueError:
                self._alert(
                    title="格式错误",
                    message=f"数量和成本必须是非负数字：{raw_item}",
                )
                return
            effective_date = (
                fields[3]
                if len(fields) == 4
                else self._current_trade_date(
                    code, (getattr(self, "stock_data", {}) or {}).get(code)
                ) or datetime.now().date().isoformat()
            )
            additions.append({
                "code": code,
                "name": name,
                "quantity": quantity,
                "cost": cost,
                "effective_date": effective_date,
            })
            existing.add(code.lower())
            added.append(name)

        if added:
            if not hasattr(self, "_trade_ledger"):
                self._trade_ledger = TradeLedger()
                self._trade_ledger.sync_legacy_positions(original_positions)
            ledger_snapshot = self._trade_ledger.snapshot_state()
            try:
                for item in additions:
                    self._apply_portfolio_quantity_change(
                        item["code"],
                        item["quantity"],
                        item["cost"],
                        item["effective_date"],
                        "manual_add",
                        refresh=False,
                        persist=False,
                    )
                if not self._save_portfolio():
                    raise ValueError("保存失败：持仓配置写入失败，本次追加未生效。")
            except ValueError as error:
                self._trade_ledger.restore_state(ledger_snapshot)
                self.portfolio["positions"][:] = original_positions
                self._alert(title="保存失败", message=str(error))
                return
            self._refresh_portfolio_update_menu()
        message = f"已追加{len(added)}只股票。"
        if skipped:
            message += f"\n已跳过已存在：{'、'.join(skipped)}。"
        self._alert(title="追加监控股票", message=message)

    def show_portfolio_position_update(self, _, code=None):
        """只修改指定股票的数量和成本，避免每次重复输入股票标识。"""
        positions = self.portfolio.get("positions", [])
        normalized_code = str(code or "").lower()
        item = next(
            (
                position for position in positions
                if str(position.get("code", "")).lower() == normalized_code
            ),
            None,
        )
        if item is None:
            self._alert(title="修改数量/成本", message="没有找到要修改的监控股票。")
            return

        name = self._display_stock_name(item.get("code"), item.get("name"))
        quantity = self._safe_float(item.get("quantity"))
        cost = self._safe_float(item.get("cost"))
        response = self._window(
            title=f"修改{name}",
            message=(
                f"{name}（{item.get('code', '')}）\n"
                f"当前：{quantity:,.0f}股 @ {cost:.2f}\n\n"
                "输入：数量,参考成本,生效日期（YYYY-MM-DD）\n"
                "数量增加当天锁定，受A股T+1限制；仅修成本可填原数量。"
            ),
            default_text=(
                f"{self._format_portfolio_input_number(quantity)},"
                f"{self._format_portfolio_input_number(cost)},"
                f"{self._current_trade_date(item.get('code'), (getattr(self, 'stock_data', {}) or {}).get(item.get('code'))) or datetime.now().date().isoformat()}"
            ),
            ok="保存", cancel="取消", dimensions=(320, 100),
        ).run()
        if not response.clicked:
            return

        fields = [
            field.strip()
            for field in response.text.replace("，", ",").split(",")
        ]
        if len(fields) not in (2, 3):
            self._alert(
                title="格式错误",
                message="请输入“数量,参考成本,生效日期”，例如：500,18.20,2026-10-20",
            )
            return
        try:
            quantity, cost = self._parse_portfolio_numbers(fields[0], fields[1])
        except ValueError:
            self._alert(
                title="格式错误",
                message="数量和成本必须是非负数字，例如：500,18.20",
            )
            return
        effective_date = (
            fields[2]
            if len(fields) == 3
            else self._current_trade_date(
                item.get("code"),
                (getattr(self, "stock_data", {}) or {}).get(item.get("code")),
            ) or datetime.now().date().isoformat()
        )
        try:
            self._apply_portfolio_quantity_change(
                item.get("code"), quantity, cost, effective_date, "manual_edit",
            )
        except ValueError as error:
            self._alert(title="保存失败", message=str(error))
            return
        self._alert(
            title="修改数量/成本",
            message=(
                f"已更新 {name}：{quantity:,.0f}股 @ {cost:.2f}\n"
                f"生效日期：{effective_date}；当日新增数量受A股T+1限制。"
            ),
        )

    def show_portfolio_update(self, _):
        """修改已有持仓的数量/成本（观察股数量填 0）。

        「追加监控股票」对已存在的股票会跳过，改数量只能靠本入口或直接改配置。
        数量影响盘中信号判定（跌破支撑/冲高做T/回踩接回需要 quantity>0）。
        """
        positions = self.portfolio.get("positions", [])
        if not positions:
            self._alert(title="修改数量/成本", message="当前没有已配置的监控股票。")
            return
        current = "\n".join(
            f"- {self._display_stock_name(item.get('code'), item.get('name'))} ({item.get('code')}) "
            f"{float(item.get('quantity', 0) or 0):.0f}股@{float(item.get('cost', 0) or 0):.2f}"
            for item in positions
        )
        response = self._window(
            title="修改数量/成本",
            message=f"当前持仓：\n{current}\n\n"
                    "每项：名称或代码,数量[,参考成本[,生效日期]]\n"
                    "不写成本则保持不变；生效日期默认取该股行情日期；观察股数量填 0\n"
                    "示例：中天科技,500,18.2,2026-10-20",
            default_text="",
            ok="修改", cancel="取消", dimensions=(380, 140),
        ).run()
        if not response.clicked:
            return

        by_code = {
            item.get("code", "").lower(): item for item in positions
        }
        staged = {}
        missing = []
        for raw_item in self._split_portfolio_input(response.text):
            fields = [field.strip() for field in raw_item.split(",")]
            if len(fields) not in (2, 3, 4):
                self._alert(
                    title="格式错误",
                    message=f"无法解析（需 名称或代码,数量[,参考成本[,生效日期]]）：{raw_item}",
                )
                return
            code, name = self._resolve_portfolio_stock(fields[0])
            item = by_code.get(code.lower()) if code else None
            if item is None:
                missing.append(fields[0] if not code else f"{name}（未持有）")
                continue
            try:
                quantity, cost = self._parse_portfolio_numbers(
                    fields[1],
                    fields[2] if len(fields) >= 3 else item.get("cost", 0),
                )
            except ValueError:
                self._alert(
                    title="格式错误",
                    message=f"数量和成本必须是非负数字：{raw_item}",
                )
                return
            effective_date = (
                fields[3]
                if len(fields) == 4
                else self._current_trade_date(
                    item.get("code"),
                    (getattr(self, "stock_data", {}) or {}).get(item.get("code")),
                ) or datetime.now().date().isoformat()
            )
            staged[id(item)] = (item, quantity, cost, effective_date)

        updated = []
        previous_values = []
        if not hasattr(self, "_trade_ledger"):
            self._trade_ledger = TradeLedger()
            self._trade_ledger.sync_legacy_positions(positions)
        ledger_snapshot = self._trade_ledger.snapshot_state()
        try:
            for item, quantity, cost, effective_date in staged.values():
                previous_values.append(
                    (item, item.get("quantity"), item.get("cost"))
                )
                self._apply_portfolio_quantity_change(
                    item.get("code"), quantity, cost, effective_date,
                    "manual_batch_edit", refresh=False, persist=False,
                )
                updated.append(
                    f"{item.get('name', item.get('code'))} {quantity:.0f}股@{cost:.2f}"
                )
            if updated and not self._save_portfolio():
                raise ValueError("保存失败：持仓配置写入失败，本次批量修改未生效")
        except ValueError as error:
            self._trade_ledger.restore_state(ledger_snapshot)
            for item, old_quantity, old_cost in previous_values:
                item["quantity"] = old_quantity
                item["cost"] = old_cost
            self._alert(title="保存失败", message=str(error))
            return
        if updated:
            self._refresh_portfolio_update_menu()
            # 数量影响盘中信号判定，立即触发一轮持仓分析（交易时段内约1分钟生效）
            self._last_portfolio_analysis_slot = None
            self._maybe_refresh_portfolio_analysis()
            message = f"已更新 {len(updated)} 只：\n" + "\n".join(updated)
            message += "\n\n数量调整按生效日期执行，当日新增数量受A股T+1限制。"
        else:
            message = "没有更新任何持仓。"
        if missing:
            message += f"\n未找到/未持有：{'、'.join(missing)}"
        self._alert(title="修改数量/成本", message=message)

    def show_portfolio_delete(self, _):
        """删除指定监控股票，不影响其他列表项。"""
        positions = self.portfolio.get("positions", [])
        if not positions:
            self._alert(title="删除监控股票", message="当前没有已配置的监控股票。")
            return
        current = "\n".join(
            f"- {item.get('name')} ({item.get('code')})" for item in positions
        )
        response = self._window(
            title="删除监控股票",
            message=f"当前列表：\n{current}\n\n请输入要删除的名称或代码，可用分号分隔：",
            default_text="",
            ok="下一步", cancel="取消", dimensions=(360, 100),
        ).run()
        if not response.clicked:
            return
        targets = self._split_portfolio_input(response.text)
        if not targets:
            return
        target_codes = set()
        not_found = []
        for target in targets:
            code, _ = self._resolve_portfolio_stock(target)
            if code and any(item.get("code", "").lower() == code.lower() for item in positions):
                target_codes.add(code.lower())
            else:
                not_found.append(target)
        if not target_codes:
            self._alert(title="删除监控股票", message="没有匹配到可删除的股票。")
            return

        names = [item.get("name", item.get("code")) for item in positions if item.get("code", "").lower() in target_codes]
        confirm = self._window(
            title="确认删除",
            message=(
                f"将删除：{'、'.join(names)}\n"
                "删除等价于按生效日期卖出全部数量；当日买入仍受A股T+1限制。\n"
                "输入“确认”后执行："
            ),
            default_text="",
            ok="删除", cancel="取消", dimensions=(300, 60),
        ).run()
        if not confirm.clicked or confirm.text.strip() not in ("确认", "删除"):
            return
        original_positions = list(positions)
        if not hasattr(self, "_trade_ledger"):
            self._trade_ledger = TradeLedger()
            self._trade_ledger.sync_legacy_positions(positions)
        ledger_snapshot = self._trade_ledger.snapshot_state()
        try:
            for item in positions:
                code = str(item.get("code") or "").lower()
                if code not in target_codes:
                    continue
                effective_date = self._current_trade_date(
                    code, (getattr(self, "stock_data", {}) or {}).get(code)
                ) or datetime.now().date().isoformat()
                self._apply_portfolio_quantity_change(
                    code,
                    0,
                    item.get("cost"),
                    effective_date,
                    "manual_delete",
                    refresh=False,
                    persist=False,
                )
            self.portfolio["positions"] = [
                item for item in positions
                if item.get("code", "").lower() not in target_codes
            ]
            if not self._save_portfolio():
                raise ValueError("保存失败：持仓配置写入失败，本次删除未生效。")
        except ValueError as error:
            self._trade_ledger.restore_state(ledger_snapshot)
            self.portfolio["positions"] = original_positions
            self._alert(title="保存失败", message=str(error))
            return
        self._refresh_portfolio_update_menu()
        message = f"已删除{len(names)}只股票。"
        if not_found:
            message += f"\n未匹配：{'、'.join(not_found)}。"
        self._alert(title="删除监控股票", message=message)

    def show_portfolio_unmonitor(self, _):
        """只停止监控，保留交易账本和T+1批次，不生成卖出记录。"""
        positions = self.portfolio.get("positions", [])
        if not positions:
            self._alert(title="移出监控", message="当前没有已配置的监控股票。")
            return
        current = "\n".join(
            f"- {item.get('name')} ({item.get('code')})"
            for item in positions
        )
        response = self._window(
            title="移出监控（保留账本）",
            message=(
                f"当前列表：\n{current}\n\n"
                "请输入要停止监控的名称或代码，可用分号分隔。\n"
                "此操作不卖出、不删除交易账本，也不改变T+1可卖量。"
            ),
            default_text="",
            ok="下一步", cancel="取消", dimensions=(380, 120),
        ).run()
        if not response.clicked:
            return
        target_codes = set()
        for target in self._split_portfolio_input(response.text):
            code, _ = self._resolve_portfolio_stock(target)
            if code:
                target_codes.add(code.lower())
        if not target_codes:
            self._alert(title="移出监控", message="没有匹配到可移出的股票。")
            return
        names = [
            item.get("name", item.get("code"))
            for item in positions
            if str(item.get("code") or "").lower() in target_codes
        ]
        confirm = self._window(
            title="确认移出监控",
            message=(
                f"将停止监控：{'、'.join(names)}\n"
                "交易账本、持仓数量和T+1批次保持不变。\n"
                "输入“确认”后执行："
            ),
            default_text="",
            ok="移出", cancel="取消", dimensions=(340, 90),
        ).run()
        if not confirm.clicked or confirm.text.strip() not in ("确认", "移出"):
            return
        original_positions = list(positions)
        self.portfolio["positions"] = [
            item for item in positions
            if str(item.get("code") or "").lower() not in target_codes
        ]
        if not self._save_portfolio():
            self.portfolio["positions"] = original_positions
            self._alert(
                title="保存失败",
                message="保存失败：移出监控未生效，交易账本未改变。",
            )
            return
        self._refresh_portfolio_update_menu()
        self._alert(
            title="移出监控",
            message=f"已停止监控：{'、'.join(names)}。交易账本和T+1批次保持不变。",
        )

    def show_portfolio_settings(self, _):
        """兼容旧入口：只进入总资金设置，不覆盖持仓列表。"""
        self.show_portfolio_capital_settings(_)
        return

        # 以下旧的整体覆盖流程保留为不可达代码，避免旧调用路径破坏持仓列表。
        capital_response = self._window(
            title="持仓设置",
            message="请输入总资金（可留空，单位：元）：",
            default_text=str(self.portfolio.get("total_capital") or ""),
            ok="下一步", cancel="取消", dimensions=(220, 24),
        ).run()
        if not capital_response.clicked:
            return
        try:
            total_capital = float(capital_response.text.strip() or 0)
        except ValueError:
            self._alert(title="格式错误", message="总资金必须是数字。")
            return

        positions_response = self._window(
            title="监控股票列表",
            message="每项格式：代码或名称,数量,成本\n"
                    "支持中文/英文分号、逗号或换行分隔；观察股数量填0\n"
                    "示例：sh600089,1000,18.2；贵州茅台，0，0\n"
                    "也可只输入名称：\n中国卫星；中天科技；特变电工；北方稀土",
            default_text="",
            ok="保存", cancel="取消", dimensions=(360, 80),
        ).run()
        if not positions_response.clicked:
            return

        positions = []
        normalized_input = (
            positions_response.text
            .replace("；", ";")
            .replace("，", ",")
            .replace("\r\n", "\n")
            .replace("\n", ";")
        )
        for raw_item in normalized_input.split(";"):
            fields = [field.strip() for field in raw_item.split(",")]
            if not fields or not fields[0]:
                continue
            if len(fields) not in (1, 3):
                self._alert(title="格式错误", message=f"无法解析：{raw_item}")
                return
            keyword = fields[0].lower()
            code = keyword
            name = fields[0]
            if not re.match(r"^(sh|sz)\d{6}$", code):
                results = self.search_stock(fields[0])
                if not results:
                    self._alert(title="未找到股票", message=f"没有找到：{fields[0]}")
                    return
                code = results[0]["code"]
                name = results[0]["name"]
            try:
                quantity = float(fields[1]) if len(fields) == 3 else 0
                cost = float(fields[2]) if len(fields) == 3 else 0
            except ValueError:
                self._alert(title="格式错误", message=f"数量和成本必须是数字：{raw_item}")
                return
            positions.append({"code": code, "name": name, "quantity": quantity, "cost": cost})

        self.portfolio.update({
            "total_capital": total_capital,
            "positions": positions,
        })
        self._save_portfolio()
        self._alert(
            title="持仓设置已保存",
            message=f"已保存{len(positions)}只股票。\n盘中每30分钟自动分析，点击“持仓信息”查看最近缓存。",
        )

    def _show_trade_calculation_in_textedit(self, text):
        path = os.path.join(self.log_dir, "_trade_calculation.txt")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            subprocess.Popen(
                ["open", "-a", "TextEdit", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.log(f"买卖成本计算已生成并打开: {path}", "INFO")
        except Exception as error:
            self.log(f"打开买卖成本计算失败: {error}", "ERROR")
            self._alert(title="买卖成本计算", message=text[:500])

    @rumps.clicked("💰 买卖成本计算")
    def show_trade_calculator(self, _):
        """计算单次买卖后的持仓成本；输入股票时附带当前浮盈/浮亏。"""
        stock_response = self._window(
            title="买卖成本计算",
            message="股票名称或代码（可留空；留空不计算浮盈/浮亏）：\n"
                    "示例：特变电工、sh600089",
            default_text="",
            ok="下一步", cancel="取消", dimensions=(260, 32),
        ).run()
        if not stock_response.clicked:
            return

        stock_label = stock_response.text.strip()
        stock_code = None
        live_data = None
        if stock_label:
            stock_code = stock_label.lower()
            stock_name = stock_label
            if not re.match(r"^(sh|sz)\d{6}$", stock_code):
                results = self.search_stock(stock_label)
                if not results:
                    self._alert(title="未找到股票", message=f"没有找到“{stock_label}”。")
                    return
                stock_code = results[0]["code"]
                stock_name = results[0]["name"]
            else:
                stock_name = stock_label
            try:
                live_data = self.get_stock_data(stock_code)
            except Exception:
                live_data = None
            stock_label = f"{stock_name}（{stock_code}）"

        holding_response = self._window(
            title="操作前持仓",
            message="请输入：持仓股数,平均成本\n示例：1000,18.20",
            default_text="0,0",
            ok="下一步", cancel="取消", dimensions=(240, 32),
        ).run()
        if not holding_response.clicked:
            return
        try:
            holding_fields = [item.strip() for item in holding_response.text.replace("，", ",").split(",")]
            if len(holding_fields) != 2:
                raise ValueError
            holding_shares, average_cost = map(float, holding_fields)
        except ValueError:
            self._alert(title="格式错误", message="持仓格式应为：股数,平均成本，例如 1000,18.20")
            return

        operation_response = self._window(
            title="本次操作",
            message="请输入：买入或卖出,操作股数,操作价格\n示例：买入,500,17.80",
            default_text="买入,0,0",
            ok="下一步", cancel="取消", dimensions=(280, 32),
        ).run()
        if not operation_response.clicked:
            return
        try:
            operation_fields = [item.strip() for item in operation_response.text.replace("，", ",").split(",")]
            if len(operation_fields) != 3:
                raise ValueError
            side, trade_shares, trade_price = operation_fields[0], float(operation_fields[1]), float(operation_fields[2])
        except ValueError:
            self._alert(title="格式错误", message="操作格式应为：买入/卖出,股数,价格")
            return

        fee_response = self._window(
            title="交易费用",
            message="本次交易总费用（可留空，默认0元）：",
            default_text="0",
            ok="计算", cancel="取消", dimensions=(220, 28),
        ).run()
        if not fee_response.clicked:
            return
        try:
            fees = float(fee_response.text.strip() or 0)
            result = calculate_trade(
                holding_shares, average_cost, side, trade_shares, trade_price,
                fees=fees,
                current_price=live_data.get("price") if live_data else None,
            )
        except ValueError as error:
            self._alert(title="计算失败", message=str(error))
            return

        fee_note = "已按输入的总费用计算"
        if not stock_label:
            fee_note += "；未输入股票，未获取实时价格"
        elif not live_data:
            fee_note += "；实时价格获取失败，未计算浮盈/浮亏"
        report = format_trade_report(result, stock_label or None, fee_note)
        self._show_trade_calculation_in_textedit(report)

    @rumps.clicked("🎯 目标成本策略")
    def show_target_cost_strategy(self, _):
        """结合实时行情和日线技术分析，规划达到更低目标持仓成本的条件式策略。"""
        stock_response = self._window(
            title="目标成本策略",
            message="请输入股票名称或代码：\n"
                    "目标价指操作后的平均持仓成本，不是单次买入价。\n"
                    "示例：特变电工、sh600089",
            default_text="",
            ok="下一步", cancel="取消", dimensions=(300, 46),
        ).run()
        if not stock_response.clicked:
            return

        stock_label = stock_response.text.strip()
        if not stock_label:
            self._alert(title="目标成本策略", message="请输入股票名称或代码。")
            return
        stock_code = stock_label.lower()
        stock_name = stock_label
        if not re.match(r"^(sh|sz)\d{6}$", stock_code):
            results = self.search_stock(stock_label)
            if not results:
                self._alert(title="未找到股票", message=f"没有找到“{stock_label}”。")
                return
            stock_code = results[0]["code"]
            stock_name = results[0]["name"]
        if not re.match(r"^(sh|sz)\d{6}$", stock_code):
            self._alert(title="格式错误", message="请输入正确的股票代码，例如 sh600519。")
            return

        configured = next(
            (
                item for item in self.portfolio.get("positions", [])
                if str(item.get("code") or "").lower() == stock_code
            ),
            None,
        )
        default_holding = "0,0"
        if configured:
            default_holding = (
                f"{float(configured.get('quantity') or 0):.0f},"
                f"{float(configured.get('cost') or 0):.4f}"
            )
        holding_response = self._window(
            title=f"{stock_name}的持仓",
            message="请输入：持仓股数,当前平均成本\n"
                    "目标成本必须低于当前平均成本，例如：1000,18.20",
            default_text=default_holding,
            ok="下一步", cancel="取消", dimensions=(290, 38),
        ).run()
        if not holding_response.clicked:
            return
        try:
            holding_fields = [
                item.strip()
                for item in holding_response.text.replace("，", ",").split(",")
            ]
            if len(holding_fields) != 2:
                raise ValueError
            holding_shares, average_cost = map(float, holding_fields)
        except ValueError:
            self._alert(title="格式错误", message="持仓格式应为：股数,平均成本，例如 1000,18.20")
            return

        target_response = self._window(
            title="目标持仓成本",
            message="请输入希望达到的更低平均持仓成本：\n"
                    "程序会结合支撑、压力、趋势和量价给出分批策略。",
            default_text=(f"{average_cost:.2f}" if average_cost > 0 else "0"),
            ok="分析", cancel="取消", dimensions=(290, 38),
        ).run()
        if not target_response.clicked:
            return
        try:
            target_cost = float(target_response.text.strip())
        except ValueError:
            self._alert(title="格式错误", message="目标持仓成本必须是数字，例如 16.80。")
            return

        fee_response = self._window(
            title="交易费用",
            message="预计用于补仓的交易费用（可留空，默认0元）：",
            default_text="0",
            ok="生成策略", cancel="取消", dimensions=(260, 32),
        ).run()
        if not fee_response.clicked:
            return
        try:
            fees = float(fee_response.text.strip() or 0)
            live_data = self.get_stock_data(stock_code)
            if not live_data or live_data.get("price") is None:
                raise ValueError("实时行情获取失败，无法结合当前行情制定策略")
            history = self.get_stock_analysis_history(stock_code, data_length=240)
            benchmark = None
            if stock_code != "sh000001":
                try:
                    benchmark = self.get_stock_analysis_history("sh000001", data_length=240)
                except Exception as error:
                    self.log(f"目标成本策略获取大盘基准失败: {error}", "WARNING")
            technical = analyze_history(
                history,
                stock_code,
                stock_name,
                live_data=live_data,
                benchmark_history=benchmark,
            )
            trade_date = self._current_trade_date(stock_code, live_data)
            sellable_quantity = 0
            if trade_date:
                summary = self._trade_ledger.position_summary(stock_code, trade_date)
                sellable_quantity = int(summary.get("sellable_quantity") or 0)
            result = plan_target_cost(
                holding_shares,
                average_cost,
                target_cost,
                live_data["price"],
                technical=technical,
                fees=fees,
                sellable_quantity=sellable_quantity,
                capital_constraints=self.portfolio,
            )
        except (ValueError, TypeError, KeyError) as error:
            self._alert(title="目标成本策略失败", message=str(error))
            return
        except Exception as error:
            self.log(f"目标成本策略分析失败 {stock_code}: {error}\n{traceback.format_exc()}", "ERROR")
            self._alert(title="目标成本策略失败", message=f"行情或技术分析失败：{error}")
            return

        report = format_target_cost_report(
            result,
            f"{stock_name}（{stock_code}）",
        )
        self._show_trade_calculation_in_textedit(report)

    # ═══════════════════════════════════════
    # 通知测试
    # ═══════════════════════════════════════

    @staticmethod
    def _escape_applescript(text):
        return str(text).replace("\\", "\\\\").replace('"', '\\"')

    def _notify(self, title, subtitle=None, body=None, sound=True, level=None,
                force_popup=False, popup_body=None):
        """发 macOS 系统通知。

        通道优先级（2026-09-30 实测定型）：
        1. UserNotifications（PyObjC）—— 以程序本体 Python.app 身份走系统新接口，
           该身份已获系统完全授权（auth=7），最可靠；
        2. terminal-notifier —— 独立 .app 身份，需单独授权（macOS 15 可能拒签）；
        3. osascript —— 兜底，其身份（脚本编辑器）常未授权而被静默丢弃。
        背景：菜单栏版原用 rumps.notification（NSUserNotificationCenter）在 macOS 15
        静默失效，2026-09-30 排查后改为上述链路。

        所有提醒都会让 Dock 图标跳动：普通提醒短跳，danger/crash 持续跳。
        危险级别还会额外：
        - 用 TimeSensitive 中断等级，穿透专注模式（否则只进通知列表不弹横幅）
        - 播放独立醒目音效
        - 弹「知道了，我去处理」告警对话框（框内标注级别），完全不依赖通知系统。

        popup_body：屏幕打断层（告警对话框）的正文，缺省复用
        body。真预警时两边本就该显示同一份告警内容；只有自测这类"验通道
        本身"的文案才需要分流，否则通知和对话框各弹一份同样的字，用户看不出
        自己在验哪一层。body 归系统通知，popup_body 归屏幕打断。
        """
        title = str(title)[:120]
        body = str(body or "")[:1000]
        # 横幅正文缺省复用通知正文；显式传了才分流
        popup_body = body if popup_body is None else str(popup_body)[:1000]
        subtitle = str(subtitle)[:200] if subtitle else ""
        level_name = getattr(level, 'value', level) or 'info'
        level_name = str(level_name).lower()
        urgent = level_name in ('danger', 'crash')
        if not urgent:
            self._request_dock_attention(critical=False)

        # 1) UserNotifications（Python.app 身份）
        try:
            ok, channel = self._notify_via_usernotifications(
                title, subtitle, body, sound, urgent=urgent
            )
            if ok:
                self.log(f"通知已发送 [{channel}] level={level_name}: {title}", "INFO")
                if urgent:
                    self._raise_urgency(title, subtitle, body, popup_body, level_name,
                                        force_popup=force_popup)
                return True
        except Exception as error:
            self.log(f"UserNotifications 通知失败，继续兜底: {error}", "WARNING")

        # 2) terminal-notifier
        try:
            import shutil
            notifier = shutil.which("terminal-notifier")
            if notifier:
                cmd = [notifier, "-title", title, "-message", body]
                if subtitle:
                    cmd += ["-subtitle", subtitle]
                if sound:
                    cmd += ["-sound", "Submarine"]
                subprocess.run(
                    cmd, check=True, timeout=15,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self.log(f"通知已发送 [terminal-notifier] level={level_name}: {title}", "INFO")
                if urgent:
                    self._raise_urgency(title, subtitle, body, popup_body, level_name,
                                        force_popup=force_popup)
                return True
        except Exception as error:
            self.log(f"terminal-notifier 发送失败，退回 osascript: {error}", "WARNING")

        # 3) osascript
        try:
            script = (
                f'display notification "{self._escape_applescript(body)}" '
                f'with title "{self._escape_applescript(title)}"'
            )
            if subtitle:
                script += f' subtitle "{self._escape_applescript(subtitle)}"'
            if sound:
                script += ' sound name "Submarine"'
            subprocess.run(
                ["osascript", "-e", script], check=True, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.log(f"通知已发送 [osascript] level={level_name}: {title}", "INFO")
            if urgent:
                self._raise_urgency(title, subtitle, body, popup_body, level_name,
                                    force_popup=force_popup)
            return True
        except Exception as error:
            self.log(f"通知发送失败（三个通道均不可用）: {error}", "ERROR")
            return False

    # 告警对话框冷却（秒）与级别升序。级别抬升时立即弹，同级或降级则节流。
    _POPUP_COOLDOWN = 180
    _LEVEL_ORDER = {'info': 0, 'warning': 1, 'danger': 2, 'crash': 3}
    # 对话框标题前缀：级别直接写在框里（2026-10-22 用户要求）。
    _LEVEL_LABELS = {
        'crash': '系统性风险',
        'danger': '危险提醒',
        'warning': '风险提醒',
        'info': '观察提醒',
    }

    def _popup_should_show(self, level_name, force=False):
        """决定这一轮要不要弹告警对话框。

        强制弹出的情形：手动测试；或级别比上次弹的更严重（必须让人知道恶化）。
        其余情形按冷却时间节流，避免三个指数轮流触发导致每 15 秒弹一次模态框。
        """
        if force:
            self._popup_last_time = time.time()
            self._popup_last_level = level_name
            return True
        now = time.time()
        last_level = getattr(self, '_popup_last_level', None)
        last_time = getattr(self, '_popup_last_time', 0)
        rank = self._LEVEL_ORDER.get(level_name, 0)
        last_rank = self._LEVEL_ORDER.get(last_level, -1)
        if rank > last_rank:
            self._popup_last_time = now
            self._popup_last_level = level_name
            return True
        if now - last_time >= self._POPUP_COOLDOWN:
            self._popup_last_time = now
            self._popup_last_level = level_name
            return True
        return False

    def _run_on_main_once(self, fn, tag='task'):
        """把 fn 调度到主线程执行一次（AppKit 要求 UI 操作在主线程）。

        rumps.Timer.start() 固定把 NSTimer 加到**当前线程**的 run loop；
        从后台扫描线程调用时，定时器不会进入 AppKit 主 run loop，回调既不
        执行也不报错。必须用 PyObjCTools.AppHelper.callAfter 显式回到主线程。
        """
        if threading.current_thread() is threading.main_thread():
            try:
                fn()
                self.log(f"{tag}: 已在主线程，直接执行", "INFO")
            except Exception as error:
                self.log(f"{tag} 执行失败: {error}", "ERROR")
            return

        state = {'done': False}

        def _cb():
            if state['done']:
                return
            state['done'] = True
            try:
                fn()
                self.log(f"{tag}: 已切换到主线程执行", "INFO")
            except Exception as error:
                self.log(f"{tag} 执行失败: {error}", "ERROR")

        try:
            AppHelper.callAfter(_cb)
            self.log(f"{tag}: 已通过 AppHelper 调度到主线程", "INFO")
        except Exception as error:
            self.log(f"{tag} 调度失败: {error}", "ERROR")

    def _show_alert_dialog(self, title, subtitle, body, level_name):
        """统一的告警对话框（NSAlert 模态，按钮「知道了，我去处理」）。

        2026-10-22 起取代置顶横幅卡片：danger/crash 与观察类打断都走它，
        级别写进标题前缀（【危险提醒】【系统性风险】等）。
        必须在主线程弹（后台线程建的 NSAlert 不响应点击 / 会触发线程断言）。
        """
        label = self._LEVEL_LABELS.get(level_name, str(level_name))
        informative = f"{subtitle}\n\n{body}" if subtitle else str(body or "")

        def _modal():
            import AppKit
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(f"【{label}】{title}")
            alert.setInformativeText_(informative)
            if level_name == 'crash':
                alert.setAlertStyle_(AppKit.NSAlertStyleCritical)
            elif level_name == 'info':
                alert.setAlertStyle_(AppKit.NSAlertStyleInformational)
            else:
                alert.setAlertStyle_(AppKit.NSAlertStyleWarning)
            alert.addButtonWithTitle_('知道了，我去处理')
            self.log(f"告警对话框已弹出 level={level_name}，等待用户确认", "INFO")
            alert.runModal()

        self._run_on_main_once(_modal, tag=f"告警对话框[{level_name}]")

    def _raise_urgency(self, title, subtitle, body, popup_body, level_name,
                       force_popup=False):
        """危险级别的额外打断手段，不再只依赖通知系统。

        com.apple.python3 的 timeSensitiveSetting=0，TimeSensitive 会被降级，
        专注模式下通知只进列表不弹横幅。所以这里叠三层，第一层是主通道：
        1. 告警对话框（NSAlert「知道了，我去处理」，框内标注级别；
           2026-10-22 起取代置顶横幅卡片，danger/crash 统一用它）
        2. 独立醒目音效 + Dock 图标弹跳
        对话框受 180s 冷却 / 级别抬升规则节流——盘中连环触发时不能
        每 15 秒弹一次模态框，根本没法用。
        """
        # 1) 告警对话框（主线程、节流判定在 _popup_should_show）
        if self._popup_should_show(level_name, force=force_popup):
            self._show_alert_dialog(title, subtitle, popup_body, level_name)
        else:
            self.log(f"告警对话框冷却中，本轮跳过 level={level_name}", "INFO")

        # 2) 音效 + Dock 弹跳
        try:
            sound_file = '/System/Library/Sounds/Basso.aiff'
            if level_name == 'crash':
                sound_file = '/System/Library/Sounds/Sosumi.aiff'
            if os.path.exists(sound_file):
                subprocess.Popen(
                    ['afplay', sound_file],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

        try:
            self._request_dock_attention(critical=True)
        except Exception:
            pass

    @staticmethod
    def _notify_via_usernotifications(title, subtitle, body, sound, urgent=False):
        """UserNotifications 新接口（需 pyobjc-framework-UserNotifications）。

        返回 (是否成功, 通道描述)。urgent=True 时用 TimeSensitive 中断等级：
        普通 Active 级通知在专注模式/勿扰下只进通知列表、不弹横幅，
        正是「通知列表里有，但没有明显弹出」的根因。
        """
        import time as _time
        try:
            import UserNotifications as UN
        except ImportError:
            return False, "unavailable"
        center = UN.UNUserNotificationCenter.currentNotificationCenter()
        # 授权请求幂等；系统已有授权时立即回调 granted=True
        result = {"auth": None, "added": None}

        def _auth(granted, _error):
            result["auth"] = bool(granted)

        def _added(error):
            result["added"] = str(error) if error else ""

        options = UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound
        # TimeSensitive 需单独申请，否则系统按普通通知降级处理
        if urgent and hasattr(UN, 'UNAuthorizationOptionTimeSensitive'):
            options |= UN.UNAuthorizationOptionTimeSensitive
        center.requestAuthorizationWithOptions_completionHandler_(options, _auth)

        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        if subtitle:
            content.setSubtitle_(subtitle)
        content.setBody_(body)
        if sound:
            content.setSound_(UN.UNNotificationSound.defaultSound())

        # 中断等级：0=Passive(只进列表) 1=Active(横幅，被专注模式压掉)
        #           2=TimeSensitive(穿透专注) 3=Critical(需额外授权)
        if hasattr(content, 'setInterruptionLevel_'):
            if urgent and hasattr(UN, 'UNNotificationInterruptionLevelTimeSensitive'):
                content.setInterruptionLevel_(
                    UN.UNNotificationInterruptionLevelTimeSensitive
                )
            elif hasattr(UN, 'UNNotificationInterruptionLevelActive'):
                content.setInterruptionLevel_(UN.UNNotificationInterruptionLevelActive)
        # 提高在通知中心/摘要里的排序权重
        if hasattr(content, 'setRelevanceScore_'):
            content.setRelevanceScore_(100.0 if urgent else 10.0)

        trigger = UN.UNTimeIntervalNotificationTrigger.triggerWithTimeInterval_repeats_(
            1.0, False
        )
        request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            f"stock-monitor-{_time.time()}", content, trigger
        )
        center.addNotificationRequest_withCompletionHandler_(request, _added)

        # XPC 回调需要短暂等待。注意：超时不能算成功——旧实现把 None 当成
        # 「已入队」直接放行，导致静默失败被吞掉，日志里永远看不到问题。
        deadline = _time.time() + 3.0
        while _time.time() < deadline and (result["auth"] is None or result["added"] is None):
            _time.sleep(0.05)

        if result["auth"] is False:
            return False, "denied"
        if result["added"] not in (None, ""):
            return False, f"add-error:{result['added']}"
        if result["added"] is None:
            # 回调超时：按失败处理并走兜底通道，宁可重发也不要漏报
            return False, "timeout"
        level_tag = "TimeSensitive" if urgent else "Active"
        return True, f"UserNotifications/{level_tag}"

    def test_notification(self, _):
        """测试告警打断链路。

        必须用 danger 级走完整路径：普通级只测通知，而通知在专注模式下
        只进列表不弹横幅，会误判成"功能正常"。这里连置顶横幅一起验。

        两个界面各带各的文案：通知只讲通知，横幅只讲横幅。同一串字塞给
        两处的话，用户看到两份一样的内容，反而看不出自己在验哪一层。
        """
        try:
            self._notify(
                "🚨 告警打断测试",
                "danger 级 · 与盘中暴跌同一链路",
                "系统通知：应弹出提醒。若只落进通知列表，是被专注模式压掉了。",
                level='danger', force_popup=True,
                popup_body="置顶横幅：走盘中暴跌那条 danger 级打断链路，必定置顶可见。\n"
                           "点「知道了」关闭。音效 + Dock 弹跳同属这层。",
            )
            self.log("测试通知已发送（danger 级完整链路）", "INFO")
        except Exception as error:
            self.log(f"测试通知失败: {error}", "ERROR")
            self._alert(title="通知测试失败", message=str(error))

    def test_intraday_signals(self, _):
        """测试盘中买卖点信号通知。"""
        from intraday_tactics import IntradayTactics

        # 模拟信号场景（每种各一条）
        scenarios = [
            {
                "name": "跌破支撑",
                "params": dict(
                    code="sh600519", name="贵州茅台", price=1750.0,
                    change_pct=-2.0, volume_ratio=1.0, limit_pct=10.0,
                    support=1780.0, resistance=1850.0, score=3, quantity=100,
                ),
            },
            {
                "name": "放量突破",
                "params": dict(
                    code="sz300750", name="宁德时代", price=210.0,
                    change_pct=3.5, volume_ratio=1.8, limit_pct=20.0,
                    support=190.0, resistance=205.0, score=4, quantity=0,
                ),
            },
            {
                "name": "冲高做T",
                "params": dict(
                    code="sh600522", name="中天科技", price=18.5,
                    change_pct=4.0, volume_ratio=1.2, limit_pct=10.0,
                    support=17.0, resistance=18.2, score=3, quantity=300,
                ),
            },
            {
                "name": "回踩接回",
                "params": dict(
                    code="sh600522", name="中天科技", price=17.3,
                    change_pct=-3.5, volume_ratio=0.8, limit_pct=10.0,
                    support=17.2, resistance=18.5, score=3, quantity=300,
                ),
            },
            {
                "name": "回踩上行M20",
                "params": dict(
                    code="sh600884", name="杉杉股份", price=15.2,
                    change_pct=-1.0, volume_ratio=0.9, limit_pct=10.0,
                    support=15.0, resistance=16.5, score=3, quantity=0,
                    ma20=15.1, ma20_prev=14.9,
                ),
            },
            {
                "name": "趋势转弱观察",
                "params": dict(
                    code="sh600519", name="贵州茅台", price=1750.0,
                    change_pct=-1.5, volume_ratio=0.9, limit_pct=10.0,
                    support=1700.0, resistance=1850.0, score=3, quantity=100,
                    ma20=1800.0, ma20_prev=1810.0, closes_below_ma20=3,
                ),
            },
        ]

        sent_count = 0
        for scenario in scenarios:
            # 每个场景用独立实例，避免信号跃迁/待确认状态互相干扰
            signal = IntradayTactics().check_signal(**scenario["params"])
            if signal:
                guarded = guard_intraday_signal(
                    signal.signal_type,
                    f"{signal.title or ''} {signal.body or ''}",
                    trade_date=None,
                )
                try:
                    self._notify(
                        f"🧪 模拟信号（不执行）｜{scenario['name']}",
                        f"现价 {scenario['params']['price']:.2f}",
                        guarded.text,
                        )
                    sent_count += 1
                    self.log(f"测试盘中信号: {scenario['name']}", "INFO")
                    self.log(
                        f"盘中信号详情: {signal.body}"
                        f"｜原始提示：{signal.notification_body or ''}",
                        "DEBUG",
                    )
                except Exception as error:
                    self.log(f"发送测试信号失败: {error}", "WARNING")

        self._alert(
            title="盘中信号测试",
            message=f"已发送 {sent_count} 条测试通知，请检查通知中心。",
        )

    def _send_portfolio_notification(self, result):
        """只在信号状态变化时发一次 macOS 提示。"""
        # ── 盘中买卖点信号通知（优先级更高）──
        intraday = result.get("intraday_signal")
        if intraday:
            intraday_key = f"{result['code']}_{intraday['type']}"
            if intraday_key not in self._portfolio_signals:
                self._portfolio_signals[intraday_key] = True
                trade_date = (
                    result.get("trade_date")
                    or result.get("data_as_of")
                    or self._current_trade_date(
                        result["code"],
                        (getattr(self, "stock_data", {}) or {}).get(result["code"]),
                    )
                )
                guarded_intraday = guard_intraday_signal(
                    intraday.get("type"),
                    f"{intraday.get('title') or ''} "
                    f"{intraday.get('notification_body') or intraday.get('body') or ''}",
                    PositionEligibility(
                        total_quantity=int(result.get("quantity") or 0),
                        sellable_quantity=int(result.get("sellable_quantity") or 0),
                        today_locked_quantity=int(result.get("today_locked_quantity") or 0),
                    ),
                    evidence=None,
                    trade_date=trade_date,
                    invalidation_price=result.get("support") or 0.0,
                )
                notification_body = guarded_intraday.text
                try:
                    self._notify(
                        intraday["title"],
                        f"现价 {result['price']:.2f}",
                        notification_body,
                        )
                    self.log(
                        f"盘中信号通知: {intraday['title']} - {notification_body}",
                        "INFO",
                    )
                    self.log(
                        f"盘中信号详情: {intraday.get('body') or ''}"
                        f"｜原始提示：{intraday.get('notification_body') or ''}",
                        "DEBUG",
                    )
                except Exception as error:
                    self.log(f"发送盘中信号提醒失败: {error}", "WARNING")

        # ── 持仓状态变化通知 ──
        signal = result["signal"]
        previous = self._portfolio_signals.get(result["code"])
        self._portfolio_signals[result["code"]] = signal
        # 「买入确认」已随突破信号一并退役（回测显示突破负超额，不再产生买入确认）
        if signal == previous or signal not in ("买入观察", "减仓风险", "趋势转弱观察", "冲高做T"):
            return
        short_actions = {
            "买入观察": "回踩上行 M20 企稳，等待确认后分批观察",
            "减仓风险": "高位趋势走坏或确认破位，清仓离场（可分批执行）",
            "趋势转弱观察": "M20失守｜暂不加仓，等收回确认",
            "冲高做T": "乖离过大，可分批做 T，回踩接回",
        }
        if signal == "减仓风险" and not result.get("exit_directive_ok", True):
            # 基本面未亮红灯 + 价格不高 + 仓位小：不喊「清仓离场」，
            # 只提示风险事实（2026-10-22 招商证券被直接劝退）。
            short_actions["减仓风险"] = (
                "确认破位：基本面未亮红灯、价格不高、仓位轻，仅提示风险事实；"
                "是否处置等盘中确认，不必直接撤出"
            )
        title = f"📦 {result['name']}·{signal}"
        subtitle = f"现价 {result['price']:.2f}"
        raw_message = short_actions.get(signal, "请查看持仓分析报告")
        trade_date = (
            result.get("trade_date")
            or result.get("data_as_of")
            or self._current_trade_date(
                result["code"],
                (getattr(self, "stock_data", {}) or {}).get(result["code"]),
            )
        )
        guarded_state = guard_intraday_signal(
            signal,
            raw_message,
            PositionEligibility(
                total_quantity=int(result.get("quantity") or 0),
                sellable_quantity=int(result.get("sellable_quantity") or 0),
                today_locked_quantity=int(result.get("today_locked_quantity") or 0),
            ),
            evidence=None,
            trade_date=trade_date,
            invalidation_price=result.get("support") or 0.0,
        )
        message = guarded_state.text
        try:
            self._notify(
                title,
                subtitle,
                message,
            )
        except Exception as error:
            self.log(f"发送持仓提醒失败: {error}", "WARNING")

    def _fundamental_health_for(self, code, name, price=None, cost=0.0, support=0.0):
        """读基本面健康缓存；对浮亏较深或已破位的持仓按需后台补拉（有缓存用缓存）。

        提醒只读缓存、从不在提醒时拉接口；缓存约半个月有效，见 fundamental_health。
        """
        import fundamental_health as fh

        health = fh.health_of(code)
        try:
            price = float(price or 0)
            cost = float(cost or 0)
            support = float(support or 0)
        except (TypeError, ValueError):
            price, cost, support = 0.0, 0.0, 0.0
        pnl = (price / cost - 1) * 100 if cost > 0 and price > 0 else None
        broken = support > 0 and price > 0 and price < support
        watch = pnl is not None and pnl <= -10.0
        if (broken or watch) and fh.needs_fetch(code):
            fh.mark_attempt(code)
            threading.Thread(
                target=self._fetch_fundamental_health, args=(code, name), daemon=True
            ).start()
        return health

    def _fetch_fundamental_health(self, code, name):
        """后台补拉基本面并折成健康分级；失败只记日志（重试节流由 mark_attempt 控制）。"""
        import fundamental_health as fh

        try:
            from fundamentals import fetch_and_analyze

            result, _report = fetch_and_analyze(code, name)
            analyzed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            fh.publish(code, fh.grade_result(result, analyzed_at=analyzed_at))
            self.log(f"基本面健康判断已更新: {name}", "INFO")
        except Exception as error:
            self.log(f"基本面补拉失败 {name}: {error}", "WARNING")

    def _check_dip_rebound(self):
        """下探回升（V 型收复）提醒：跌出恐慌坑又被买回来，量能不缩才强提醒。

        个股两级信号：回升刚起步发「反弹观察」（info 级、带诱多/抛压警示），
        确认门槛达成后升级「深跌回升」；再创新低静默作废观察并重新武装。
        一段下探只提醒一次、创更深新低才重新武装；个股限持仓股（补仓/做T类提示）。
        「资金承接」看回升段成交节奏，不看全天累计——上午恐慌量会把缩量反弹也抬成放量。
        """
        from dip_rebound import DipReboundEngine, Sample

        def _quote_day_low_pct(data):
            """行情快照的日低对昨收涨跌%；没有日低/昨收时返回 None。"""
            low_price = data.get("low")
            prev_close = data.get("prev_close")
            try:
                low_price = float(low_price) if low_price else None
                prev_close = float(prev_close) if prev_close else None
            except (TypeError, ValueError):
                return None
            if not low_price or not prev_close:
                return None
            return (low_price - prev_close) / prev_close * 100

        if not hasattr(self, "_dip_rebound_engine"):
            self._dip_rebound_engine = self._restore_tracker_state(
                "dip_rebound", DipReboundEngine(),
            )
        samples = []
        for code in self.market_alert_indices:
            data = self.stock_data.get(code)
            if not data or data.get("price") is None:
                continue
            clock = parse_quote_clock(data)
            if clock is None:
                continue
            samples.append((clock.day, Sample(
                code=code,
                name=data.get("name", code),
                price=float(data["price"]),
                change_pct=float(data.get("change_pct", 0) or 0),
                cumulative_turnover=float(data.get("turnover", 0) or 0),
                timestamp=clock.at.timestamp(),
                stock=False,
                day_low_change_pct=_quote_day_low_pct(data),
            )))
        position_codes = {p.get("code") for p in self.portfolio.get("positions", [])}
        for code in position_codes:
            data = self.stock_data.get(code)
            if not data or data.get("price") is None:
                continue
            clock = parse_quote_clock(data)
            if clock is None:
                continue
            result = next(
                (r for r in getattr(self, "_last_portfolio_results", []) if r.get("code") == code),
                None,
            )
            price = float(data["price"])
            atr_value = (result or {}).get("atr")
            trend = ((result or {}).get("entry_structure") or {}).get("trend_assessment") or {}
            samples.append((clock.day, Sample(
                code=code,
                name=data.get("name", code),
                price=price,
                change_pct=float(data.get("change_pct", 0) or 0),
                volume_ratio=data.get("volume_ratio") or None,
                cumulative_turnover=float(data.get("turnover", 0) or 0),
                timestamp=clock.at.timestamp(),
                atr_pct=(float(atr_value) / price * 100) if atr_value and price > 0 else None,
                stock=True,
                major_trend=trend.get("major_trend"),
                trend_conflict=trend.get("conflict"),
                day_low_change_pct=_quote_day_low_pct(data),
            )))
        if not samples:
            return
        quote_days = {day for day, _sample in samples}
        if len(quote_days) != 1:
            self.log(f"下探回升行情日期不一致: {sorted(quote_days)}，本轮跳过", "WARNING")
            return
        quote_day = next(iter(quote_days))
        for alert in self._dip_rebound_engine.update(
            [sample for _day, sample in samples], quote_day,
        ):
            try:
                self._notify(
                    alert.title,
                    "下探回升",
                    alert.body,
                    level="danger" if alert.vibrate else "info",
                )
                self.log(f"下探回升提醒(阶段{alert.stage}): {alert.title}", "INFO")
            except Exception as error:
                self.log(f"下探回升提醒发送失败: {error}", "WARNING")

    def _iter_holding_ticks(self):
        """持仓行情 → (name, data)；无行情的跳过。只扫持仓，不扫观察池。"""
        position_codes = {p.get("code") for p in self.portfolio.get("positions", [])}
        for code in position_codes:
            data = self.stock_data.get(code)
            if not data or data.get("price") is None:
                continue
            yield code, self._display_stock_name(code, data.get("name")), data

    def _check_seal_events(self):
        """持仓封板/炸板/回封事件：只报事实，不带买卖指令。

        2026-10-22 华天科技 14:00 封板、15:00 尾盘炸板全程零提醒——
        封板类事件此前只有板块龙头在管。判定复用龙头封板口径（贴涨停价
        且卖一为空）。
        """
        if not hasattr(self, "_seal_tracker"):
            self._seal_tracker = self._restore_tracker_state(
                "seal_events", SealEventTracker(),
            )
        for code, name, data in self._iter_holding_ticks():
            try:
                clock = parse_quote_clock(data)
                if clock is None:
                    continue
                tick = SealTick(
                    code=code,
                    name=name,
                    price=float(data.get("price") or 0),
                    prev_close=float(data.get("prev_close") or 0),
                    ask1_price=self._safe_float(data.get("ask1_price"), None),
                    ask1_vol=self._safe_float(data.get("ask1_vol"), None),
                    bid1_vol=float(data.get("bid1_vol") or 0),
                )
                for event in self._seal_tracker.update(tick, clock.day):
                    self._notify_seal_event(event)
            except Exception as error:
                self.log(f"封板事件检测失败 {name}: {error}", "WARNING")

    def _notify_seal_event(self, event):
        """封板/炸板/回封通知：事实 + 盘口快照，无处置指令。"""
        limit_text = f"{event.limit_price:.2f}"
        if event.kind == "封板":
            title = f"🔔 {event.name} 封板"
            body = (
                f"封死涨停 {limit_text}，封单 {event.seal_volume / 10000:.0f} 万股。"
                "炸板会再提醒。"
            )
        elif event.kind == "回封":
            title = f"🔔 {event.name} 回封"
            body = (
                f"重新封死涨停 {limit_text}，封单 {event.seal_volume / 10000:.0f} 万股。"
                f"当日已炸板 {event.blowout_count} 次。"
            )
        else:
            title = f"💥 {event.name} 炸板"
            body = (
                f"涨停打开，现价 {event.price:.2f}。"
                f"当日第 {event.blowout_count} 次炸板；若回封会再提醒。"
            )
        try:
            self._notify(title, "封板事件", body, level="info")
            self.log(f"封板事件: {title} - {body}", "INFO")
        except Exception as error:
            self.log(f"封板事件通知失败: {error}", "WARNING")

    def _check_ignition(self):
        """持仓盘中点火：短窗量能突增 + 突破整理平台上沿 → 强提醒弹窗。

        2026-10-22 华天科技 13:50–13:55 量能 4 倍、破平台 14.06，约 5 分钟后
        封板——这是封板前盘面唯一硬线索。回测显示突破信号跑输无条件买入，
        所以只提示异动事实，不给买卖指令。
        """
        if not hasattr(self, "_ignition_tracker"):
            self._ignition_tracker = self._restore_tracker_state(
                "ignition", IgnitionTracker(),
            )
        for code, name, data in self._iter_holding_ticks():
            try:
                clock = parse_quote_clock(data)
                if clock is None:
                    continue
                sample = IgnitionSample(
                    code=code,
                    name=name,
                    price=float(data.get("price") or 0),
                    cumulative_volume=float(data.get("volume") or 0),
                    timestamp=clock.at.timestamp(),
                )
                alert = self._ignition_tracker.update(sample, clock.day)
                if alert is not None:
                    self._notify_ignition(alert)
            except Exception as error:
                self.log(f"盘中点火检测失败 {name}: {error}", "WARNING")

    def _notify_ignition(self, alert):
        """盘中点火强提醒：通知 + 弹窗双通道（用户明确要求弹窗）。"""
        body = (
            f"5 分钟量能约为前 30 分钟均量的 {alert.volume_ratio:.1f} 倍，"
            f"现价 {alert.price:.2f} 突破整理平台上沿 {alert.platform_top:.2f}。"
            "盘面异动事实，非买入指令。"
        )
        title = f"🔥 {alert.name} 盘中点火"
        try:
            self._notify(title, f"现价 {alert.price:.2f}", body, level="warning")
            self.log(f"盘中点火: {title} - {body}", "INFO")
        except Exception as error:
            self.log(f"盘中点火通知失败: {error}", "WARNING")
        # 强提醒弹窗：走告警对话框，强制弹出（不理会冷却——点火是罕见事件，
        # 每只每天最多一次，由 tracker 保证不刷屏）。
        try:
            self._show_alert_dialog(
                title, f"现价 {alert.price:.2f}", body, "warning",
            )
        except Exception as error:
            self.log(f"盘中点火弹窗失败: {error}", "WARNING")

    @staticmethod
    def _intraday_policy_intent(signal):
        text = " ".join((
            signal.title or "",
            signal.body or "",
            signal.notification_body or "",
        ))
        if signal.signal_type == IntradaySignalType.TRADE_WAIT:
            return "hold"
        if signal.signal_type in {
            IntradaySignalType.BREAK_SUPPORT,
            IntradaySignalType.EXIT_RISK,
        }:
            return "risk_exit"
        if signal.signal_type == IntradaySignalType.T_SELL or "做T" in text:
            return "t_sell"
        if signal.signal_type == IntradaySignalType.EXIT_WINDOW or any(
            phrase in text for phrase in ("分批离场", "清仓离场", "减仓控风险")
        ):
            return "normal_exit"
        if any(phrase in text for phrase in ("小仓补", "补仓", "接回", "买入")):
            return "buy"
        return "hold"

    @staticmethod
    def _current_trade_date(code, live=None):
        del code
        if not isinstance(live, dict):
            return None
        raw = str(live.get("date") or "").strip()[:10]
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None

    def _apply_intraday_trade_policy(
        self,
        signal,
        code,
        name,
        price,
        support,
        resistance,
        now=None,
        buy_capacity_quantity=0,
        trade_date=None,
        permissions=None,
        trend=None,
        high_position=None,
    ):
        intent = self._intraday_policy_intent(signal)
        if intent == "hold":
            return signal
        now = now or datetime.now()
        if trade_date is None:
            stock_data = getattr(self, "stock_data", {}) or {}
            trade_date = self._current_trade_date(
                code, stock_data.get(code) or {}
            )
        if not trade_date:
            message = (
                "行情日期缺失，仅提示风险事实；次一交易日按15/30分钟"
                "及T+1可卖量复核，不作今日卖出处置。"
                if intent == "risk_exit"
                else "行情日期缺失，无法核验T+1交易日；暂停买入、卖出和做T，仅作观察。"
            )
            if intent == "risk_exit":
                return IntradaySignal(
                    signal_type=IntradaySignalType.NEXT_DAY_REVIEW,
                    title=f"🕒 {name} 行情日期待确认",
                    body=f"{message}\n原始盘口条件已记录，等待有效行情日期后复核。",
                    notification_body=message,
                )
            tactics = getattr(self, "_intraday_tactics", None)
            if tactics is not None:
                tactics.defer_signal(
                    code,
                    signal.signal_type,
                    retry_after_seconds=60 if intent == "risk_exit" else COOLDOWN_SECONDS,
                )
            return IntradaySignal(
                signal_type=IntradaySignalType.TRADE_WAIT,
                title=f"🔎 {name} 行情日期待确认",
                body=f"{message}\n原始盘口条件已记录，等待有效行情日期后复核。",
                notification_body=message,
            )
        try:
            bars15 = self._intraday_bar_fetcher(code, 15, 80, now)
            bars30 = (
                self._intraday_bar_fetcher(code, 30, 40, now)
                if intent != "risk_exit"
                else []
            )
            evidence = build_timeframe_evidence(
                bars15, bars30, price=price, support=support,
                resistance=resistance, now=now, quote_date=trade_date,
            )
        except Exception as error:
            self.log(f"盘中双周期证据获取失败 {name}: {error}", "WARNING")
            bars15, bars30 = [], []
            evidence = build_timeframe_evidence(
                bars15, bars30, price=price, support=support,
                resistance=resistance, now=now, quote_date=trade_date,
            )

        try:
            summary = self._trade_ledger.position_summary(code, trade_date)
        except Exception as error:
            self.log(f"T+1可卖数量计算失败 {name}: {error}", "WARNING")
            summary = {
                "quantity": 0,
                "sellable_quantity": 0,
                "today_locked_quantity": 0,
            }
        eligibility = PositionEligibility(
            total_quantity=int(summary.get("quantity") or 0),
            sellable_quantity=int(summary.get("sellable_quantity") or 0),
            today_locked_quantity=int(summary.get("today_locked_quantity") or 0),
            buy_capacity_quantity=int(buy_capacity_quantity or 0),
        )
        invalidation = support if intent == "buy" else 0.0
        decision = evaluate_trade_action(
            intent, evidence, eligibility, invalidation_price=invalidation,
            permissions=permissions, trend=trend, high_position=high_position,
        )

        if decision.action == DecisionAction.WAIT:
            guarded = guard_actionable_copy(
                intent, decision.message, eligibility, evidence,
                trade_date=trade_date, invalidation_price=invalidation,
            )
            tactics = getattr(self, "_intraday_tactics", None)
            if tactics is not None:
                tactics.defer_signal(
                    code,
                    signal.signal_type,
                    retry_after_seconds=60 if intent == "risk_exit" else COOLDOWN_SECONDS,
                )
            return IntradaySignal(
                signal_type=IntradaySignalType.TRADE_WAIT,
                title=f"🔎 {name} 条件观察",
                body=f"{guarded.text}\n原始盘口条件已记录，等待确认后再评估。",
                notification_body=guarded.text,
            )
        if decision.action == DecisionAction.NEXT_DAY_REVIEW:
            guarded = guard_actionable_copy(
                intent, decision.message, eligibility, evidence,
                trade_date=trade_date, invalidation_price=invalidation,
            )
            return IntradaySignal(
                signal_type=IntradaySignalType.NEXT_DAY_REVIEW,
                title=f"🕒 {name} T+1次日处置",
                body=guarded.text,
                notification_body=guarded.text,
            )
        if decision.action == DecisionAction.BUY_SMALL:
            guarded = guard_actionable_copy(
                "buy", decision.message, eligibility, evidence,
                trade_date=trade_date, invalidation_price=invalidation,
            )
            return IntradaySignal(
                signal_type=signal.signal_type,
                title=f"📗 {name} 确认买入窗口",
                body=guarded.text,
                notification_body=(
                    f"15/30确认｜小仓买{decision.max_buy_quantity}股"
                    f"｜跌破{decision.invalidation_price:.2f}停止"
                ),
            )
        if decision.action == DecisionAction.RISK_EXIT:
            guarded = guard_actionable_copy(
                "risk_exit", decision.message, eligibility, evidence,
                trade_date=trade_date,
            )
            return IntradaySignal(
                signal_type=signal.signal_type,
                title=f"🔴 {name} 风险离场",
                body=guarded.text,
                notification_body=(
                    f"15分钟风险确认｜今日可卖{decision.max_sell_quantity}股"
                    "｜新仓T+1不纳入"
                ),
            )
        if decision.action == DecisionAction.NORMAL_EXIT:
            guarded = guard_actionable_copy(
                "normal_exit", decision.message, eligibility, evidence,
                trade_date=trade_date,
            )
            return IntradaySignal(
                signal_type=IntradaySignalType.EXIT_WINDOW,
                title=f"🔴 {name} 分批卖出窗口",
                body=guarded.text,
                notification_body=(
                    f"15/30确认｜原有可卖{decision.max_sell_quantity}股"
                    "｜新仓T+1"
                ),
            )
        if decision.action == DecisionAction.T_SELL:
            guarded = guard_actionable_copy(
                "t_sell", decision.message, eligibility, evidence,
                trade_date=trade_date,
            )
            return IntradaySignal(
                signal_type=IntradaySignalType.T_SELL,
                title=f"🔥 {name} 做T减仓窗口",
                body=guarded.text,
                notification_body=(
                    f"15/30确认｜原有可卖最多{decision.max_sell_quantity}股"
                    "｜新仓T+1"
                ),
            )
        return signal

    def _check_intraday_signals(self):
        """轻量级盘中信号检测（每15秒调用一次）。

        只检查价格/量能条件，不做完整技术分析。
        使用上次持仓分析的支撑/压力位。
        基于历史数据动态调整确认策略。
        """
        if not self.portfolio.get("positions"):
            return

        # 获取上次分析结果中的支撑/压力位
        if not hasattr(self, '_last_portfolio_results'):
            return

        for result in self._last_portfolio_results:
            code = result.get("code")
            if not code:
                continue

            # 获取实时行情
            live = self.stock_data.get(code)
            if not live or live.get("price") is None:
                continue
            trade_date = self._current_trade_date(code, live)

            price = live["price"]
            change_pct = live.get("change_pct", 0)
            volume_ratio = result.get("volume_ratio", 0)
            support = result.get("support", 0)
            resistance = result.get("resistance", 0)
            score = result.get("score", 0)
            quantity = result.get("quantity", 0)
            name = self._display_stock_name(code, result.get("name"))
            limit_pct = limit_pct_for_board(code)
            vr = volume_ratio if volume_ratio > 0 else None
            market_context = self._intraday_market_context(code)
            market_context["values"]["daily_high_price"] = self._safe_float(
                live.get("high"), None
            )
            market_context["values"]["recent_market_turnovers"] = (
                self.turnover_tracker.recent_full_day_turnovers(trade_date, limit=5)
                if self.turnover_tracker else []
            )

            existing_event = self._t_filter_events.get(code)
            if existing_event and existing_event.get("date") != trade_date:
                self._t_filter_events.pop(code, None)
                existing_event = None
            if existing_event:
                update_t_filter_event_outcome(existing_event, price, datetime.now().strftime("%Y-%m-%d %H:%M"))
                self._daily_report_state["t_filter_events"] = list(
                    self._t_filter_events.values()
                )
                self._save_daily_report_state(self._daily_report_state)

            # ── 深套决策框架的上下文：成本 / ATR / 资金 / 基本面健康 ──
            cost = float(result.get("cost", 0) or 0)
            # 深套阈值的 ATR% 口径集中在 portfolio_daily_report.atr_pct_of（优先
            # volatility.atr_pct，退回 atr/现价）。这里曾经读 result.get("atr") ——
            # 而 result 里当时根本没有这个键，于是 atr_pct 恒为 None，深套阈值
            # 静默退回固定 -20%，和 evaluate_position 的 ATR 缩放阈值对不上。
            atr_pct = atr_pct_of(result, price)
            health = self._fundamental_health_for(
                code, name, price=price, cost=cost, support=support,
            )
            deep_context = dict(
                cost=cost,
                atr_pct=atr_pct,
                total_capital=float(self.portfolio.get("total_capital", 0) or 0),
                available_capital=float(self.portfolio.get("available_capital", 0) or 0),
                initial_entry_pct=float(self.portfolio.get("initial_entry_pct", 5) or 5),
                max_position_pct=float(self.portfolio.get("max_position_pct", 20) or 20),
                fundamental_health=health,
            )

            # ── M20 上下文（来自上一次持仓分析，15秒轮询不重算日线指标） ──
            entry = result.get("entry_structure") or {}
            ma20 = entry.get("ma20") or result.get("ma20")
            ma20_prev = entry.get("ma20_prev")
            closes_below_ma20 = entry.get("closes_below_ma20", 0)
            recent_trend = entry.get("recent_trend")
            trend_weak = bool(result.get("trend_weak") or entry.get("trend_weak"))
            price_location = (result.get("price_position") or {}).get("location_key")
            recent_change_5d = (recent_trend or {}).get("change_5d_pct")
            recent_up_days_5d = (recent_trend or {}).get("up_days_5d")
            recent_weak = (
                recent_change_5d is not None
                and recent_change_5d <= 0
                and recent_up_days_5d is not None
                and recent_up_days_5d <= 1
            )
            trend_policy = {
                "trade_permissions": entry.get("trade_permissions"),
                "trend": entry.get("trend_assessment"),
                "high_position": entry.get("high_position"),
            }

            # ── 记录tick数据用于置信度计算 ──
            conditions = {
                "跌破支撑": (
                    quantity > 0 and support > 0 and
                    price < support * (1 - 0.005)
                ),
                "放量突破": (
                    resistance > 0 and price > resistance and
                    vr is not None and vr >= 1.3
                ),
                "冲高做T": (
                    quantity > 0 and
                    change_pct >= limit_pct * 0.3
                ),
                    "强势延续观察": (
                    quantity > 0 and
                    change_pct >= limit_pct * 0.3 and
                        market_context["quality"].strong
                ),
                "龙头封板暂缓": (
                    quantity > 0 and
                    change_pct >= limit_pct * 0.3 and
                    bool(market_context["values"].get("leader_hold"))
                ),
                "回踩接回": (
                    quantity > 0 and
                    change_pct <= -limit_pct * 0.3 and
                    support > 0 and
                    support <= price <= support * (1 + 0.02) and
                    not recent_weak and
                    (vr is None or vr <= 1.0)
                ),
                "回踩支撑": (
                    quantity == 0 and score >= 2 and
                    support > 0 and
                    support <= price <= support * (1 + 0.02)
                ),
                "趋势转弱观察": (
                    quantity > 0 and ma20 is not None and ma20 > 0 and
                    (price < ma20 * (1 - 0.02) or closes_below_ma20 >= 2)
                ),
            }
            # ── 更新之前信号的结果 ──
            from intraday_tactics import IntradaySignalType
            for signal_type in IntradaySignalType:
                self._signal_confidence.update_signal_outcome(
                    code, signal_type, price
                )

            # ── 计算动态确认轮数 ──
            # 先用evaluate检查是否有潜在信号
            potential_signal = self._intraday_tactics.evaluate(
                code=code, name=name, price=price,
                change_pct=change_pct,
                volume_ratio=vr,
                limit_pct=limit_pct, support=support,
                resistance=resistance, score=score,
                quantity=quantity,
                ma20=ma20, ma20_prev=ma20_prev,
                closes_below_ma20=closes_below_ma20,
                recent_trend=recent_trend,
                trend_weak=trend_weak,
                price_location=price_location,
                **trend_policy,
                **market_context["values"],
                **deep_context,
            )
            if potential_signal is not None:
                conditions[potential_signal.signal_type.value] = True
            self._signal_confidence.record_tick(
                code, price, change_pct, vr, conditions
            )
            repeat_guard = getattr(self, "_intraday_repeat_guard", None)
            if repeat_guard is None:
                repeat_guard = self._intraday_repeat_guard = AlertRepeatGuard()
            if potential_signal is None:
                repeat_guard.clear(code)

            confirmation_rounds = 2  # 默认2轮
            if potential_signal and potential_signal.signal_type not in {
                IntradaySignalType.BREAK_SUPPORT,
                IntradaySignalType.EXIT_RISK,
            }:
                # 根据置信度动态调整确认轮数
                confirmation_rounds = self._signal_confidence.get_confirmation_rounds(
                    code, potential_signal.signal_type, price, conditions
                )
                self.log(
                    f"信号置信度调整: {name} {potential_signal.signal_type.value} "
                    f"确认轮数={confirmation_rounds}",
                    "DEBUG"
                )

            # ── 检测信号 ──
            signal = self._intraday_tactics.check_signal(
                code=code, name=name, price=price,
                change_pct=change_pct,
                volume_ratio=vr,
                limit_pct=limit_pct, support=support,
                resistance=resistance, score=score,
                quantity=quantity,
                confirmation_rounds=confirmation_rounds,
                ma20=ma20, ma20_prev=ma20_prev,
                closes_below_ma20=closes_below_ma20,
                recent_trend=recent_trend,
                trend_weak=trend_weak,
                price_location=price_location,
                **trend_policy,
                **market_context["values"],
                **deep_context,
            )

            if signal:
                candidate_signal_type = signal.signal_type
                self._signal_confidence.record_signal_trigger(
                    code, candidate_signal_type, price
                )
                buy_capacity_quantity = suggested_add_quantity(
                    price=price,
                    holding_quantity=quantity,
                    total_capital=deep_context["total_capital"],
                    available_capital=deep_context["available_capital"],
                    initial_entry_pct=deep_context["initial_entry_pct"],
                    max_position_pct=deep_context["max_position_pct"],
                )
                signal = self._apply_intraday_trade_policy(
                    signal, code, name, price, support, resistance,
                    buy_capacity_quantity=buy_capacity_quantity,
                    trade_date=trade_date,
                    permissions=entry.get("trade_permissions"),
                    trend=entry.get("trend_assessment"),
                    high_position=entry.get("high_position"),
                )
                notification_body = signal.notification_body or signal.body
                if signal.signal_type == IntradaySignalType.TRADE_WAIT:
                    self._intraday_tactics.release_signal_attempt(
                        code, candidate_signal_type
                    )
                    signature = (candidate_signal_type.value, notification_body)
                    if not repeat_guard.accept(code, signature):
                        continue
                else:
                    repeat_guard.clear(code)
                if signal.signal_type in {
                    IntradaySignalType.T_HOLD,
                    IntradaySignalType.T_HOLD_CAUTION,
                    IntradaySignalType.T_HOLD_LEADER,
                }:
                    self._t_filter_events.setdefault(
                        code,
                        {
                            "date": trade_date,
                            "code": code,
                            "name": name,
                            "trigger_price": price,
                            "trigger_change_pct": change_pct,
                            "predicted_turnover": market_context["values"].get(
                                "predicted_market_turnover"
                            ),
                            "market_change_pct": market_context["values"].get(
                                "market_change_pct"
                            ),
                            "sector_change_pct": market_context["values"].get(
                                "sector_change_pct"
                            ),
                            "current_price": price,
                            "current_change_pct": change_pct,
                            # 龙头类暂缓把龙头证据写进环境栏，报告只描述事实
                            "environment_status": (
                                signal.signal_type.value
                                if signal.signal_type == IntradaySignalType.T_HOLD_LEADER
                                else market_context["quality"].label
                            ),
                            "environment_reasons": (
                                [market_context["values"].get("leader_note") or "板块龙头未转弱"]
                                if signal.signal_type == IntradaySignalType.T_HOLD_LEADER
                                else list(market_context["quality"].reasons)
                            ),
                            "industry_breadth": market_context["quality"].industry_breadth,
                            "concept_breadth": market_context["quality"].concept_breadth,
                            "outcome_status": "观察中",
                            "observations": [],
                        },
                    )
                    update_t_filter_event_outcome(
                        self._t_filter_events[code],
                        price,
                        datetime.now().strftime("%Y-%m-%d %H:%M"),
                    )
                    self._daily_report_state["t_filter_events"] = list(
                        self._t_filter_events.values()
                    )
                    self._save_daily_report_state(self._daily_report_state)
                _, flow_note, flow_level = self._capital_flow_signal_context(
                    code, signal.signal_type.value
                )
                if flow_note:
                    signal.body = f"{signal.body}\n{flow_note}"
                    if flow_level == "danger":
                        notification_body += "｜资金确认风险"
                # 发送通知
                try:
                    self._notify(
                        signal.title,
                        f"现价 {price:.2f}",
                        notification_body,
                        level=flow_level,
                        )
                    self.log(
                        f"盘中信号通知: {signal.title} - {notification_body}",
                        "INFO",
                    )
                    self.log(f"盘中信号详情: {signal.body}", "DEBUG")
                except Exception as error:
                    self.log(f"发送盘中信号提醒失败: {error}", "WARNING")

    def _intraday_market_context(self, code):
        """返回做T环境过滤所需的统一市场、板块上下文。"""
        snapshot = self._turnover_snapshot
        predicted = snapshot.predicted_full_day if snapshot else None
        index_quote = self.stock_data.get("sh000001") or {}
        market_change = index_quote.get("change_pct")

        sector_change = None
        try:
            sector_names = self._sectors_of_holding(code)
            # 手动归属的板块名可能是新浪反查表里没有的自定义名，逐个找有行情的；
            # 全都没有再退回自动反查的板块（至少还能给出涨跌环境）
            quotes = self._sector_quotes or {}
            for sector_name in sector_names:
                quote = quotes.get(sector_name)
                if isinstance(quote, dict) and quote.get("change") is not None:
                    sector_change = self._safe_float(quote.get("change"), None)
                    break
            if sector_change is None and self._holding_sector_store.is_manual(code):
                self._ensure_sector_map()
                key = str(code or "").lower()
                auto_names = (self._sector_map or {}).get(key) or (
                    (self._sector_map or {}).get(key[2:]) if key.startswith(("sh", "sz", "bj")) else []
                ) or []
                for sector_name in auto_names:
                    quote = quotes.get(sector_name)
                    if isinstance(quote, dict) and quote.get("change") is not None:
                        sector_change = self._safe_float(quote.get("change"), None)
                        break
        except Exception as error:
            self.log(f"获取{code}板块环境失败: {error}", "DEBUG")
        all_sectors = self.sector_data.get("_all", []) if isinstance(self.sector_data, dict) else []
        industry_total = sum(1 for item in all_sectors if isinstance(item, dict) and item.get("change") is not None)
        industry_up = sum(
            1 for item in all_sectors
            if isinstance(item, dict) and self._safe_float(item.get("change"), 0.0) > 0
        )
        industry_breadth = industry_up / industry_total if industry_total else None
        concept = self._concept_summary_cache or {}
        concept_sectors = concept.get("sectors") or []
        concept_total = len(concept_sectors) or int(concept.get("rising", 0) or 0) + int(concept.get("falling", 0) or 0)
        concept_up = sum(
            1 for item in concept_sectors
            if isinstance(item, dict) and self._safe_float(item.get("change"), 0.0) > 0
        )
        if not concept_sectors and concept_total:
            concept_up = int(concept.get("rising", 0) or 0)
        concept_breadth = concept_up / concept_total if concept_total else None
        recent_turnovers = (
            self.turnover_tracker.recent_full_day_turnovers(
                datetime.now().strftime("%Y-%m-%d"), limit=5
            ) if self.turnover_tracker else []
        )
        predicted_value = self._safe_float(predicted, None)
        market_change_value = self._safe_float(market_change, None)
        quality = evaluate_market_environment(
            predicted_turnover_yi=predicted_value,
            market_change_pct=market_change_value,
            market_price=self._safe_float(index_quote.get("price"), None),
            market_high_price=self._safe_float(index_quote.get("high"), None),
            industry_breadth=industry_breadth,
            concept_breadth=concept_breadth,
            recent_full_day_turnovers_yi=recent_turnovers,
        )
        values = {
            "predicted_market_turnover": predicted_value,
            "market_change_pct": market_change_value,
            "sector_change_pct": sector_change,
            "leader_hold": False,
            "leader_note": "",
            "market_index_price": self._safe_float(index_quote.get("price"), None),
            "market_index_high_price": self._safe_float(index_quote.get("high"), None),
            "industry_breadth": industry_breadth,
            "concept_breadth": concept_breadth,
            "recent_market_turnovers": recent_turnovers,
        }
        # 板块龙头封板/强承接：抑制该股的冲高做T（见 sector_leaders.LeaderGuard）
        try:
            values["leader_hold"], values["leader_note"] = self._leader_hold_for(code)
        except Exception as error:
            self.log(f"获取{code}龙头状态失败: {error}", "DEBUG")
        return {
            "strong": quality.strong,
            "quality": quality,
            "values": values,
        }

    def _check_market_alerts(self):
        """检测大盘指数并触发多维度预警（合并通知）。"""
        if not self.market_alert_enabled:
            return

        now = datetime.now()
        now_hour = now.hour
        now_minute = now.minute

        # 跨日必须重置预警状态，否则昨天触发过的级别今天不会再报。
        # reset_daily 此前定义了却从未被调用，属于潜伏故障。
        today = now.date()
        if getattr(self, '_market_alert_date', None) != today:
            self.market_alert_engine.reset_daily()
            self._market_alert_date = today
            self.log("大盘预警状态已按新交易日重置", "INFO")

        # 只在交易时段检测（9:30-11:30, 13:00-15:00）
        is_trading_time = (
            (now_hour == 9 and now_minute >= 30) or
            (now_hour == 10) or
            (now_hour == 11 and now_minute <= 30) or
            (now_hour == 13) or
            (now_hour == 14) or
            (now_hour == 15 and now_minute == 0)
        )
        if not is_trading_time:
            return

        # 收集所有指数快照
        snapshots = []
        for code in self.market_alert_indices:
            data = self.stock_data.get(code)
            if not data or data.get('price') is None:
                continue

            snapshots.append(IndexSnapshot(
                code=code,
                name=data.get('name', code),
                price=data['price'],
                change_pct=data.get('change_pct', 0),
                volume=data.get('turnover', 0) / 1e8,  # 转为亿
                timestamp=now,
            ))

        if not snapshots:
            return

        # ── 更新预警引擎（收集预警到队列） ──
        for snapshot in snapshots:
            self.market_alert_engine.update(snapshot)

        # ── 检查多指数共振 ──
        correlation_alert = self.market_alert_engine.check_correlation(snapshots)
        if correlation_alert:
            self.market_alert_engine._pending_alerts.append(correlation_alert)

        # ── 获取合并后的预警（只发一条通知） ──
        merged_alert = self.market_alert_engine.flush_alerts()

        if merged_alert:
            try:
                title, subtitle, body = format_alert_notification(merged_alert)
                self._notify(
                    title, subtitle, body, sound=True,
                    level=merged_alert.level.value,
                )

                # 记录日志
                reasons = merged_alert.extra_info.get('reasons', [])
                self.log(
                    f"大盘预警 [{merged_alert.level.value}]: "
                    f"{merged_alert.change_pct:+.2f}% "
                    f"(原因: {', '.join(reasons)})",
                    "WARNING"
                )
            except Exception as e:
                self.log(f"发送大盘预警失败: {e}", "WARNING")

    # ── 板块龙头：封板/炸板分档（抑制或恢复冲高做T）────────────────────────
    #
    # 两个频率严格分开，省接口请求量：
    # - 候选推荐：每日一次、10:00 后，每板块 1 次批量行情（绝不进 15 秒轮询）
    # - 封板状态：每轮只拉**已配置龙头**那几只，1 次批量行情

    def _holding_sector_names_for_leaders(self):
        """持仓股所属板块名（去重）。龙头只对持仓股有意义；手动归属优先。"""
        self._ensure_sector_map()
        names = []
        for position in self.portfolio.get("positions", []):
            code = str(position.get("code") or "").lower()
            if not code:
                continue
            for name in self._sectors_of_holding(code):
                if name not in names:
                    names.append(name)
        return names

    def _maybe_propose_sector_leaders(self):
        """每日一次、10:00 后按成交额推荐龙头候选（手动改过的板块不覆盖）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._leader_auto_checked == today:
            return
        now = datetime.now()
        if (now.hour, now.minute) < (10, 0):
            return   # 开盘头半小时成交额失真，不排名
        self._leader_auto_checked = today
        sectors = self._holding_sector_names_for_leaders()
        if not sectors:
            return
        constituents = self._sector_constituents or {}
        updated = 0
        for sector_name in sectors:
            codes = constituents.get(sector_name) or []
            if not codes:
                continue
            try:
                rows = self._sina_batch_quote(codes)
            except Exception as error:
                self.log(f"推荐{sector_name}龙头候选失败: {error}", "WARNING")
                continue
            ranked = rank_leader_candidates(rows)
            if ranked and self._leader_store.apply_auto(sector_name, ranked, today):
                updated += 1
        if updated and self._leader_store.save():
            self.log(f"板块龙头候选已更新: {updated} 个板块", "INFO")

    def _leader_codes(self):
        """全部已配置龙头的代码（去重）。"""
        codes = []
        for sector_name in self._holding_sector_names_for_leaders():
            for item in self._leader_store.leaders_of(sector_name):
                code = str(item.get("code") or "").lower()
                if code and code not in codes:
                    codes.append(code)
        return codes

    def _check_sector_leaders(self):
        """喂龙头行情给状态机，处理封板/强承接/退潮事件，并刷新抑制缓存。"""
        try:
            self._maybe_propose_sector_leaders()
        except Exception as error:
            self.log(f"龙头候选推荐失败: {error}", "WARNING")

        codes = self._leader_codes()
        if not codes:
            self._leader_hold_cache = {}
            return

        # 只拉已配置龙头（1 次批量 HTTP），绝不扫板块成分股
        quotes = {}
        try:
            for row in self._sina_batch_quote(codes):
                quotes[row.get("code")] = row
        except Exception as error:
            self.log(f"获取龙头行情失败: {error}", "WARNING")
            return

        # 行情留一份给菜单显示涨跌幅（每轮都是最新的）
        self._leader_quote_cache = quotes

        events = []
        for code in codes:
            row = quotes.get(code)
            if not row:
                continue
            clock = parse_quote_clock(row)
            if clock is None:
                continue
            tick = LeaderTick(
                code=code,
                name=row.get("name", code),
                price=float(row.get("price") or 0),
                prev_close=float(row.get("prev") or 0),
                ask1_vol=float(row.get("ask1_vol") or 0),
                ask1_price=float(row.get("ask1_price") or 0),
                amount=float(row.get("amount") or 0),
                volume=float(row.get("volume") or 0),
            )
            try:
                events.extend(self._leader_guard.update(
                    tick, now=clock.at.timestamp(), day=clock.day,
                ))
            except Exception as error:
                self.log(f"龙头状态机出错 {code}: {error}", "WARNING")

        self._refresh_leader_hold_cache()
        for event in events:
            self._handle_leader_event(event)

        # 菜单里的龙头涨跌幅要跟上盘口，但不能每 15 秒 clear/重建菜单——
        # 每轮在主线程原地改标题（只改几十个 NSMenuItem 的标题，开销可忽略），
        # 不打扰展开中的菜单。首轮就要改：非交易时段 `_check_sector_leaders`
        # 可能只跑这一次，等 tick%4 等于 0 等到天亮也不会来（2026-10-19 排查）。
        self._leader_menu_tick += 1
        self._dispatch_to_main(self._update_leader_menu_labels)

    def _refresh_leader_hold_cache(self):
        """按板块聚合：该板块任一龙头处于 封板/炸板观察/强承接 → 暂缓做T。"""
        cache = {}
        for sector_name in self._holding_sector_names_for_leaders():
            hold = False
            note = ""
            for item in self._leader_store.leaders_of(sector_name):
                code = str(item.get("code") or "").lower()
                if not code or not self._leader_guard.is_suppressing(code):
                    continue
                hold = True
                note = self._leader_guard.hold_note(code, item.get("name") or code)
                break
            cache[sector_name] = (hold, note)
        self._leader_hold_cache = cache

    def _leader_hold_for(self, code):
        """返回 (leader_hold, leader_note)：该股所在板块的龙头是否要求暂缓做T。"""
        for sector_name in self._sectors_of_holding(code):
            hold, note = self._leader_hold_cache.get(sector_name) or (False, "")
            if hold:
                return True, note
        return False, ""

    def _handle_leader_event(self, event):
        """相位跃迁：强承接 → 提示持有；退潮/反复开板 → 提醒恢复做T（带具体动作）。"""
        code, name = event.code, event.name or event.code
        try:
            limit_pct = effective_limit_pct(code)
        except Exception:
            limit_pct = 10.0

        # 该龙头**配置在哪些板块下**，再找这些板块的关联持仓（手动归属优先）。
        # 不能反过来用龙头自己的反查板块——手动指定的龙头未必自动属于该板块，
        # 按反查交集会匹配不到任何持仓、通知发不出去（2026-10-19 复核发现）。
        leader_sectors = []
        for sector_name in self._holding_sector_names_for_leaders():
            for item in self._leader_store.leaders_of(sector_name):
                if str(item.get("code") or "").lower() == code:
                    leader_sectors.append(sector_name)
                    break
        holdings = []
        if leader_sectors:
            wanted = set(leader_sectors)
            for position in self.portfolio.get("positions", []):
                if wanted & set(self._sectors_of_holding(position.get("code"))):
                    holdings.append(position)

        if event.kind == "strong":
            # 炸板后守住高位：文案沿用「强承接」，判定逻辑见 sector_leaders.LeaderGuard。
            body = (
                f"{name} 炸板后守在 {event.change_pct:+.1f}%"
                f"（距涨停{event.gap_to_limit_pp:.1f}pp）｜强承接，"
                "继续持有暂缓做T；当日再炸板会提醒你恢复"
            )
            for position in holdings:
                stock_name = position.get("name") or position.get("code")
                try:
                    self._notify(
                        f"📈 {stock_name} 龙头强承接",
                        f"现价 {self._safe_float((self.stock_data.get(position.get('code')) or {}).get('price'), 0):.2f}",
                        body,
                    )
                except Exception as error:
                    self.log(f"发送龙头强承接提醒失败: {error}", "WARNING")
            self.log(f"龙头强承接: {name} {event.change_pct:+.2f}%，暂缓做T", "INFO")
            return

        if event.kind in ("faded", "blowout_repeat"):
            reason = (
                "转弱"
                if event.kind == "faded"
                else f"当日第{getattr(event, 'blowout_count', 2)}次炸板"
            )
            lines = []
            for position in holdings:
                stock_name = position.get("name") or position.get("code")
                pcode = position.get("code")
                live = self.stock_data.get(pcode) or {}
                price = self._safe_float(live.get("price"), 0)
                change = self._safe_float(live.get("change_pct"), 0)
                quantity = self._safe_float(position.get("quantity"), 0)
                threshold = (limit_pct or 10.0) * 0.3
                if change >= threshold:
                    shares = int(quantity * (1 / 3) / 100) * 100
                    action = (
                        f"可按冲高做T减约{shares:.0f}股，回踩接回"
                        if shares > 0
                        else "可按冲高做T减少量，回踩接回"
                    )
                else:
                    action = "暂未达做T阈值，后续冲高仍按原规则提示"
                trade_date = self._current_trade_date(pcode, live)
                guarded = guard_actionable_copy(
                    "t_sell", action, trade_date=trade_date,
                )
                lines.append(
                    f"{stock_name} 现价 {price:.2f}（{change:+.1f}%）｜{guarded.text}"
                )
            # 同板块另有龙头仍未转弱时，说明不是全面恢复
            still_holding = []
            for sector_name in self._holding_sector_names_for_leaders():
                for item in self._leader_store.leaders_of(sector_name):
                    lcode = str(item.get("code") or "").lower()
                    if lcode and lcode != code and self._leader_guard.is_suppressing(lcode):
                        still_holding.append(item.get("name") or lcode)
            if still_holding:
                lines.append(f"另有 {'、'.join(still_holding[:2])} 未转弱，继续暂缓")
            body = (
                "\n".join(lines)
                if lines
                else "板块龙头转弱，做T条件已释放；仍须通过15/30分钟与T+1确认。"
            )
            try:
                self._notify(
                    f"📉 {name} {reason}｜做T条件恢复待确认",
                    f"现价 {event.change_pct:+.1f}%",
                    body,
                )
            except Exception as error:
                self.log(f"发送恢复做T提醒失败: {error}", "WARNING")
            self.log(f"龙头{reason}: {name} {event.change_pct:+.2f}%，恢复做T", "INFO")
            return

        if event.kind == "sealed":
            self.log(f"龙头封板: {name} {event.change_pct:+.2f}%，暂缓做T", "INFO")

    def _maybe_refresh_portfolio_analysis(self):
        """盘中每个半小时槽位最多分析一次。"""
        if not self.portfolio.get("positions"):
            return
        slot = self._intraday_indicator_slot(datetime.now())
        if slot is None or slot == self._last_portfolio_analysis_slot:
            return
        if not self._portfolio_analysis_lock.acquire(blocking=False):
            return
        self._last_portfolio_analysis_slot = slot
        threading.Thread(
            target=self._run_portfolio_analysis,
            args=(slot,),
            daemon=True,
        ).start()

    def _run_portfolio_analysis(self, slot):
        results = []
        technical_by_code = {}
        try:
            for position in self.portfolio.get("positions", []):
                try:
                    live = self.get_stock_data(position["code"])
                    if not live or live.get("price") is None:
                        raise ValueError("实时行情获取失败，跳过本次持仓判断")
                    # 写回行情缓存：晚启动/收盘后 _fetch_data 不刷行情，
                    # 不写的话 _quote_date_of() 读不到 date，今日报告会永远定不了稿。
                    self.stock_data[position["code"]] = live
                    # 名称落成代码的持仓在此补真名：盘中信号标题直接取 position["name"]
                    live_name = str((live or {}).get("name") or "").strip()
                    if live_name and str(position.get("name") or "").strip().lower() in (
                        "", str(position.get("code") or "").strip().lower()
                    ):
                        position["name"] = live_name
                    history = self.get_stock_analysis_history(position["code"], data_length=240)
                    benchmark = None
                    if position["code"] != "sh000001":
                        try:
                            benchmark = self.get_stock_analysis_history("sh000001", data_length=240)
                        except Exception as benchmark_error:
                            self.log(f"获取相对大盘基准失败: {benchmark_error}", "WARNING")
                    technical = analyze_history(
                        history,
                        position["code"],
                        position["name"],
                        live_data=live,
                        benchmark_history=benchmark,
                    )
                    technical_by_code[position["code"]] = technical
                    leader_hold, leader_note = self._leader_hold_for(position["code"])
                    result = evaluate_position(
                        position, technical, live or {}, self.portfolio,
                        fundamental_health=self._fundamental_health_for(
                            position["code"], position.get("name", position["code"]),
                            price=technical.get("price"),
                            cost=float(position.get("cost", 0) or 0),
                            support=technical.get("support", 0),
                        ),
                        leader_hold=leader_hold,
                        leader_note=leader_note,
                    )
                    results.append(result)
                    self._send_portfolio_notification(result)
                except Exception as error:
                    self.log(f"持仓分析失败 {position.get('code')}: {error}", "WARNING")
            # 保存结果供轻量级盘中信号检测使用
            # ⚠ 赋值必须在报告生成之前，且报告要独立 try/except —— 抛异常不能
            # 跳过下面的 _save_portfolio_state(slot)，否则槽位去重失效、下一轮重复跑。
            self._last_portfolio_results = results
            self.risk_results = results

            # 原始逐股 dump：不进菜单，只作排查用（紧凑版砍掉的字段都在这里）
            try:
                report = format_portfolio_report(results, self.portfolio)
                with open(self.portfolio_analysis_file, "w", encoding="utf-8") as handle:
                    handle.write(report)
            except Exception:
                self.log(f"持仓原始 dump 写入失败:\n{traceback.format_exc()}", "ERROR")

            try:
                now = datetime.now()
                addition_inputs = self.addition_service.prepare_inputs(
                    self.portfolio,
                    technical_by_code,
                    now,
                )
                addition_evaluations = tuple(
                    assess_holding(item, now) for item in addition_inputs
                )
                addition_recommendation = select_addition_candidate(
                    addition_inputs,
                    now,
                )
                addition_dates = [
                    item.fundamental.analyzed_at[:10]
                    for item in addition_inputs
                    if item.fundamental is not None
                    and isinstance(item.fundamental.analyzed_at, str)
                    and item.fundamental.analyzed_at
                ]
                self._last_addition_assessment = {
                    "recommendation": addition_recommendation,
                    "evaluations": addition_evaluations,
                    "data_date": max(addition_dates) if addition_dates else "",
                }
            except Exception as error:
                self._last_addition_assessment = {"error": str(error)}
                self.log(f"加仓评估失败: {error}", "WARNING")

            # 今日报告：菜单上「📦 持仓信息」的正文
            try:
                self._update_daily_report(results, slot)
            except Exception:
                self.log(f"今日报告生成失败:\n{traceback.format_exc()}", "ERROR")

            self._save_portfolio_state(slot)
        except Exception as error:
            self.log(f"持仓批量分析失败: {error}", "ERROR")
        finally:
            self._portfolio_analysis_lock.release()

    def _quote_date_of(self):
        """行情日期：交易日判据。节假日时它会停在上一交易日，别拿去定稿今天。

        多只票的 date 不一致时取最早的（最保守：宁可不定稿），并记一条 WARNING。
        """
        dates = set()
        for position in self.portfolio.get("positions", []):
            live = self.stock_data.get(position.get("code", "")) or {}
            raw = str(live.get("date") or "")[:10]
            if raw:
                dates.add(raw)
        if not dates:
            return None
        if len(dates) > 1:
            self.log(f"持仓行情日期不一致: {sorted(dates)}，取最早的", "WARNING")
        return sorted(dates)[0]

    def _update_daily_report(self, results, slot):
        """生成「📦 持仓信息」。盘中覆盖重写，收盘后定稿不再动。"""
        positions = self.portfolio.get("positions", [])
        pnl = compute_pnl(positions, self.stock_data)
        fingerprint = portfolio_fingerprint(positions)
        quote_date = self._quote_date_of()
        now = datetime.now()

        mode = decide_report_mode(now, quote_date, self._daily_report_state, fingerprint)
        if mode == "skip":
            return

        def _read_pct(quote):
            # 0% 是合法值（平盘）—— 不能用 `or None`，那会把 0 变成「缺数据」
            raw = (quote or {}).get("change_pct")
            return None if raw is None else self._safe_float(raw, 0.0)

        index_pct = _read_pct(self.stock_data.get("sh000001"))
        if index_pct is None:
            # 行情缓存里没有指数时补取一次；失败就让报告写「无法比较」
            try:
                index_quote = self.get_stock_data("sh000001") or {}
                if index_quote:
                    self.stock_data["sh000001"] = index_quote
                    index_pct = _read_pct(index_quote)
            except Exception as error:
                self.log(f"获取沪指涨跌幅失败，报告不做超额比较: {error}", "WARNING")

        all_sectors = self.sector_data.get("_all", []) if isinstance(self.sector_data, dict) else []
        sector_total = 0
        sector_up = 0
        for sector in all_sectors:
            change = sector.get("change") if isinstance(sector, dict) else None
            if change is None:
                continue
            sector_total += 1
            if self._safe_float(change, 0.0) > 0:
                sector_up += 1
        sector_breadth = sector_up / sector_total if sector_total else None

        turnover = self.market_turnover or {}

        def _turnover_yi(value):
            try:
                amount = float(value)
            except (TypeError, ValueError):
                return None
            return amount / 1e8 if amount > 0 else None

        snapshot = self._turnover_snapshot
        if snapshot:
            turnover_today = snapshot.today
            if snapshot.slot_minute >= 240:
                turnover_previous = snapshot.previous_full_day or snapshot.previous_same_time
                turnover_label = "较前一交易日全天"
                turnover_session = "收盘后"
            else:
                turnover_previous = snapshot.previous_same_time
                turnover_label = "较前一交易日同期"
                turnover_session = f"盘中截至{snapshot.slot}"
        else:
            turnover_today = _turnover_yi(turnover.get("today"))
            if self._trading_minutes_elapsed() >= 240:
                turnover_previous = _turnover_yi(turnover.get("previous"))
                turnover_label = "较前一交易日全天"
                turnover_session = "收盘后"
            else:
                turnover_previous = None
                turnover_label = "较前一交易日同期"
                turnover_session = "同期成交额未取到"

        cycle_history_rows = (
            self.turnover_tracker.recent_full_day_turnover_history(
                quote_date, limit=5
            )
            if self.turnover_tracker else ()
        )
        cycle_history = [value for _, value in cycle_history_rows]
        cycle_history_dates = [day for day, _ in cycle_history_rows]
        preliminary_cycle = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=turnover_today,
            turnover_history_yi=cycle_history,
            turnover_history_dates=cycle_history_dates,
            turnover_history_as_of_date=quote_date,
            index_today_pct=index_pct,
            industry_breadth=sector_breadth,
        ))
        state_store = getattr(self, "market_cycle_state_store", None)
        cycle_state = (
            state_store.observe(
                preliminary_cycle.phase,
                quote_date,
                finalized=mode == "finalize",
                evidence=preliminary_cycle.reasons[:2],
            )
            if state_store and quote_date else None
        )
        cycle_assessment = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=turnover_today,
            turnover_history_yi=cycle_history,
            turnover_history_dates=cycle_history_dates,
            turnover_history_as_of_date=quote_date,
            index_today_pct=index_pct,
            industry_breadth=sector_breadth,
            phase_hint=cycle_state.phase if cycle_state else preliminary_cycle.phase,
            phase_age_days=cycle_state.phase_age_days if cycle_state else None,
            divergence_age_days=(
                cycle_state.divergence_age_days if cycle_state else None
            ),
            down_age_days=cycle_state.down_age_days if cycle_state else None,
        ))
        market_timing = format_market_timing_data(
            quote_date,
            index_today_pct=index_pct,
            turnover_today=turnover_today,
            turnover_previous=turnover_previous,
            turnover_comparison_label=turnover_label,
            turnover_session=turnover_session,
            turnover_source="TurnoverTracker",
            turnover_scope="沪深两市",
            market_cycle_text=format_market_cycle_assessment(cycle_assessment),
            sector_breadth=sector_breadth,
            sector_breadth_label="全市场行业板块上涨占比",
            sector_breadth_date=quote_date,
            sector_breadth_source="stock_menubar.sector_data[_all]",
            northbound_net=None,
            holiday_eve=None,
        )

        sector_map = self._ensure_sector_map()
        overview = build_overview(results, pnl, {
            "index_pct": index_pct,
            "index_name": "沪指",
            "quote_date": quote_date,
            "market_environment": self._intraday_market_context(
                (positions[0] if positions else {}).get("code")
            ).get("quality") if positions else None,
            "predicted_turnover_yi": (
                self._turnover_snapshot.predicted_full_day
                if self._turnover_snapshot else None
            ),
            "market_change_pct": index_pct,
            "industry_breadth": sector_breadth,
            "breadth_source": "stock_menubar.sector_data[_all]",
            "market_timing": market_timing,
            "capital_flow": self._capital_flow_report_text(now=now),
            "sector_map": sector_map,
            "sector_quotes": self._sector_quotes,
            "t_filter_events": list(self._t_filter_events.values()),
            # 等权代理指数重建较贵，后台跑、拿手头缓存
            "sector_tech": self._ensure_sector_tech(self._holding_sector_names()),
        })
        meta = {
            "mode": "final" if mode == "finalize" else "intraday",
            "generated_at": now.strftime("%Y-%m-%d %H:%M"),
            "quote_date": quote_date,
            "slot": slot,
        }
        addition_assessment = self._last_addition_assessment or {}
        addition = addition_assessment.get("recommendation")
        if addition is None and addition_assessment.get("error"):
            addition = addition_assessment
        evaluations = addition_assessment.get("evaluations") or ()
        meta["addition_data_date"] = (
            addition_assessment.get("data_date")
            or self._addition_data_date(evaluations)
        )
        # 七、近两日利好/利空：独立兜底，任何失败只丢这一节、不伤报告主体。
        news_impact = None
        try:
            if getattr(self, "_news_impact_service", None) is None:
                self._news_impact_service = NewsImpactService(self.log_dir, log=self.log)
            news_impact = self._news_impact_service.get_impacts(
                positions, quote_date, final_refresh=(mode == "finalize"),
            )
        except Exception as error:
            self.log(f"近两日利好利空汇总失败: {error}", "WARNING")
        text = format_daily_report(
            overview,
            results,
            pnl,
            meta,
            addition=addition,
            evaluations=evaluations,
            news_impact=news_impact,
        )

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.log_dir,
                prefix=".daily_report.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                handle.write(text)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.daily_report_file)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        # 文件写成功后再更新缓存，菜单不会显示一份没落地的数字
        self._last_daily_report = {
            "overview": overview,
            "meta": meta,
            "text": text,
            "generated_at": meta["generated_at"],
        }

        if mode == "finalize":
            self._daily_report_state.update({
                "finalized_date": now.strftime("%Y-%m-%d"),
                "finalized_at": now.isoformat(timespec="seconds"),
                "portfolio_fingerprint": fingerprint,
                "report_date": quote_date,
                "is_final": True,
                "t_filter_events": list(self._t_filter_events.values()),
            })
            self._save_daily_report_state(self._daily_report_state)

    def _addition_data_date(self, evaluations):
        dates = [
            str(getattr(item, "data_date", "") or "")
            for item in (evaluations or ())
        ]
        dates = [item for item in dates if item]
        return max(dates) if dates else ""

    def _maybe_finalize_daily_report(self):
        """15:00 后若当日尚未定稿（含程序晚启动、崩溃重启），补跑一轮并定稿。

        锁的契约与 _maybe_refresh_portfolio_analysis 相同：**调用方 acquire**，
        _run_portfolio_analysis 在 finally 里 release。没拿就起线程会
        `RuntimeError: release unlocked lock`（2026-10-12 实测在 crash.log 里）。

        去重只看 daily_report_state 的 finalized_date + 持仓指纹，
        **不要拿 _last_portfolio_analysis_slot 去重** —— 合成槽位会和它撞车，
        于是定稿 state 丢了也永远重出不了报告（同一坑第二次踩）。
        """
        now = datetime.now()
        if now.hour * 60 + now.minute < FINALIZE_MINUTE:
            return
        if not self.portfolio.get("positions"):
            return
        fingerprint = portfolio_fingerprint(self.portfolio.get("positions", []))
        state = self._daily_report_state or {}
        if (state.get("finalized_date") == now.strftime("%Y-%m-%d")
                and state.get("portfolio_fingerprint") == fingerprint):
            return
        # 定稿没写成（例如行情日期异常）时的重试退避：别每 15 秒轰一次全量分析
        last = self._last_finalize_attempt_at
        if last is not None and (now - last).total_seconds() < FINALIZE_RETRY_SECONDS:
            return
        if not self._portfolio_analysis_lock.acquire(blocking=False):
            return
        self._last_finalize_attempt_at = now
        synthetic_slot = f"{now:%Y-%m-%d}-0900"
        threading.Thread(
            target=self._run_portfolio_analysis,
            args=(synthetic_slot,),
            daemon=True,
        ).start()

    def get_stock_analysis_history(self, code, data_length=240):
        """使用新浪日线接口获取单股分析数据，避免启动慢的批量 AkShare 筛选链路。"""
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        response = requests.get(
            url,
            params={
                "symbol": code,
                "scale": 240,
                "ma": "no",
                "datalen": data_length,
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            },
            timeout=10,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or len(rows) < 60:
            raise ValueError("新浪日线数据不足60个交易日")
        return pd.DataFrame(rows).rename(columns={
            "day": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        })

    def _show_technical_analysis_in_textedit(self, text):
        """将个股分析写入独立文件并用 TextEdit 打开。"""
        path = os.path.join(self.log_dir, "_technical_analysis.txt")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            subprocess.Popen(
                ["open", "-a", "TextEdit", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.log(f"个股分析已生成并打开: {path}", "INFO")
        except Exception as error:
            self.log(f"打开个股分析失败: {error}", "ERROR")
            self._alert(title="个股分析", message=text[:500])

    @rumps.clicked("🔍 个股分析")
    def show_stock_technical_analysis(self, _):
        """输入股票名称或代码，按需生成单股技术分析。"""
        if self._technical_analysis_lock.locked():
            self._alert(title="个股分析", message="已有分析任务正在执行，请稍候。")
            return

        response = self._window(
            title="个股分析",
            message="请输入股票名称、拼音或代码：\n\n"
                    "示例：贵州茅台、茅台、mt、sh600519",
            default_text="",
            ok="分析",
            cancel="取消",
            dimensions=(220, 24),
        ).run()
        if not response.clicked:
            return
        keyword = response.text.strip()
        if not keyword:
            return

        code = keyword.lower()
        name = keyword
        if not re.match(r"^(sh|sz)\d{6}$", code):
            results = self.search_stock(keyword)
            if not results:
                self._alert(title="未找到股票", message=f"没有找到“{keyword}”对应的股票。")
                return
            if len(results) > 1:
                choices = "\n".join(
                    f"{index}. {item['name']} ({item['code']})"
                    for index, item in enumerate(results[:6], 1)
                )
                selected = self._window(
                    title="选择股票",
                    message=f"找到多个结果：\n\n{choices}\n\n请输入代码：",
                    default_text=results[0]["code"],
                    ok="分析",
                    cancel="取消",
                    dimensions=(220, 24),
                ).run()
                if not selected.clicked:
                    return
                code = selected.text.strip().lower()
                matched = next((item for item in results if item["code"] == code), None)
                if matched:
                    name = matched["name"]
            else:
                code = results[0]["code"]
                name = results[0]["name"]

        if not re.match(r"^(sh|sz)\d{6}$", code):
            self._alert(title="格式错误", message="请输入正确的股票代码，例如 sh600519。")
            return
        self._start_stock_technical_analysis(code, name)

    def _start_stock_technical_analysis(self, code, name):
        if not self._technical_analysis_lock.acquire(blocking=False):
            self._alert(title="个股分析", message="已有分析任务正在执行，请稍候。")
            return
        replace_menu_item_title(self._technical_analysis_menu_item, f"🔍 分析中 · {name}")
        threading.Thread(
            target=self._run_stock_technical_analysis,
            args=(code, name),
            daemon=True,
        ).start()

    @staticmethod
    def _dispatch_to_main(callback):
        """把 UI 回调可靠地投递到 macOS 主线程。"""
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(callback)
            return
        except Exception:
            pass

        def fallback(timer):
            timer.stop()
            callback()

        rumps.Timer(fallback, 0.001).start()

    def _run_stock_technical_analysis(self, code, name):
        report = None
        error_message = None
        try:
            live = self.get_stock_data(code)
            if not live or live.get("price") is None:
                raise ValueError("实时行情获取失败，已停止分析，不使用历史收盘价代替当前价")
            history = self.get_stock_analysis_history(code, data_length=240)
            benchmark = None
            if code != "sh000001":
                try:
                    benchmark = self.get_stock_analysis_history("sh000001", data_length=240)
                except Exception as benchmark_error:
                    self.log(f"获取相对大盘基准失败: {benchmark_error}", "WARNING")
            result = analyze_history(
                history,
                code,
                name,
                live_data=live,
                benchmark_history=benchmark,
            )
            report = format_report(result)
            # 添加结构化技术摘要
            try:
                vp_ctx = None
                pp = result.get("price_position")
                if pp:
                    vp_ctx = {
                        "location": pp.get("location_key", "MID"),
                        "rebound_from_low": pp.get("rebound_from_low", 0.0),
                    }
                summary = TechnicalSummary.from_result(
                    result, price_context=vp_ctx,
                    volume_ratio=result.get("unified_volume_ratio"),
                )
                report += "\n\n" + format_technical_summary(summary)
            except Exception as summary_err:
                self.log(f"TechnicalSummary生成失败: {summary_err}", "WARNING")
        except Exception as error:
            error_message = str(error)
            self.log(f"个股分析失败 {code}: {error}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")

        def _finish():
            self._technical_analysis_lock.release()
            replace_menu_item_title(self._technical_analysis_menu_item, "🔍 个股分析")
            if report:
                self._show_technical_analysis_in_textedit(report)
            else:
                self._alert(
                    title="个股分析失败",
                    message=f"{name}（{code}）分析失败：{error_message or '未知错误'}",
                )

        self._dispatch_to_main(_finish)

    # ═══════════════════════════════════════
    # 基本面分析
    # ═══════════════════════════════════════

    @rumps.clicked("📊 基本面分析")
    def show_fundamentals_analysis(self, _):
        """输入股票名称或代码，生成基本面分析报告。"""
        response = self._window(
            title="基本面分析",
            message="请输入股票名称、拼音或代码：\n\n"
                    "示例：贵州茅台、茅台、mt、sh600519",
            default_text="",
            ok="分析",
            cancel="取消",
            dimensions=(220, 24),
        ).run()
        if not response.clicked:
            return
        keyword = response.text.strip()
        if not keyword:
            return

        code = keyword.lower()
        name = keyword
        if not re.match(r"^(sh|sz)\d{6}$", code):
            results = self.search_stock(keyword)
            if not results:
                self._alert(title="未找到股票", message=f"没有找到“{keyword}”对应的股票。")
                return
            if len(results) > 1:
                choices = "\n".join(
                    f"{index}. {item['name']} ({item['code']})"
                    for index, item in enumerate(results[:6], 1)
                )
                selected = self._window(
                    title="选择股票",
                    message=f"找到多个结果：\n\n{choices}\n\n请输入代码：",
                    default_text=results[0]["code"],
                    ok="分析",
                    cancel="取消",
                    dimensions=(220, 24),
                ).run()
                if not selected.clicked:
                    return
                code = selected.text.strip().lower()
                matched = next((item for item in results if item["code"] == code), None)
                if matched:
                    name = matched["name"]
            else:
                code = results[0]["code"]
                name = results[0]["name"]

        if not re.match(r"^(sh|sz)\d{6}$", code):
            self._alert(title="格式错误", message="请输入正确的股票代码，例如 sh600519。")
            return

        replace_menu_item_title(self._fundamentals_menu_item, f"📊 分析中 · {name}")
        threading.Thread(
            target=self._run_fundamentals_analysis,
            args=(code, name),
            daemon=True,
        ).start()

    def _run_fundamentals_analysis(self, code, name):
        report = None
        error_message = None
        try:
            from fundamentals import ReportMarkup, fetch_and_analyze
            from stock_analysis import analyze_history
            from entry_structure import EntryInput, evaluate as evaluate_entry

            # 先跑一次技术分析，拿到入场结构，供基本面报告做「基本面×买点」组合判断。
            # 技术侧失败不应拖垮基本面报告——降级为不带入场结构的版本。
            entry = None
            try:
                live = self.get_stock_data(code) or {}
                history = self.get_stock_analysis_history(code, data_length=240)
                benchmark = None
                if code != "sh000001":
                    try:
                        benchmark = self.get_stock_analysis_history("sh000001", data_length=240)
                    except Exception as benchmark_error:
                        self.log(f"基本面分析取大盘基准失败: {benchmark_error}", "WARNING")
                technical = analyze_history(
                    history, code, name, live_data=live, benchmark_history=benchmark,
                )
                entry_info = technical.get("entry_structure") or {}
                entry = evaluate_entry(EntryInput(
                    price=technical["price"],
                    prior_high=entry_info.get("prior_high", technical.get("resistance", 0)),
                    ma20=entry_info.get("ma20", technical.get("ma20", 0)),
                    ma20_prev=entry_info.get("ma20_prev", technical.get("ma20", 0)),
                    volume_ratio=entry_info.get("volume_ratio") or technical.get("unified_volume_ratio"),
                    closes_above_prior_high=entry_info.get("closes_above_prior_high", 0),
                    closes_below_ma20=entry_info.get("closes_below_ma20", 0),
                    today_change_pct=technical.get("price_change", 0),
                    holding=False,
                    benchmark_ok=entry_info.get("benchmark_ok"),
                ))
            except Exception as tech_error:
                self.log(f"基本面分析附带的技术分析失败（降级为无入场结构）: {tech_error}", "WARNING")

            # fetch_and_analyze 返回 (FundamentalResult, report_text) 元组，取报告文本；
            # «» 是 UI 高亮标记，写进 TextEdit 纯文本前先剥掉
            _result, report = fetch_and_analyze(code, name, entry=entry)
            report = ReportMarkup.plain(report)
        except Exception as error:
            error_message = str(error)
            self.log(f"基本面分析失败 {code}: {error}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")

        def _finish():
            replace_menu_item_title(self._fundamentals_menu_item, "📊 基本面分析")
            if report:
                path = os.path.join(self.log_dir, "_fundamentals_report.txt")
                try:
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(report)
                    subprocess.Popen(
                        ["open", "-a", "TextEdit", path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self.log(f"基本面分析已生成并打开: {path}", "INFO")
                except Exception as error:
                    self.log(f"打开基本面分析失败: {error}", "ERROR")
                    self._alert(title="基本面分析", message=report[:500])
            else:
                self._alert(
                    title="基本面分析失败",
                    message=f"{name}（{code}）分析失败：{error_message or '未知错误'}",
                )

        self._dispatch_to_main(_finish)

    # ═══════════════════════════════════════
    # 港美股基本面分析
    # ═══════════════════════════════════════

    @rumps.clicked("🌎 港美股分析")
    def show_global_analysis(self, _):
        """输入美股/港股名称或代码，生成基本面 + 技术面报告。"""
        response = self._window(
            title="港美股分析",
            message="请输入美股/港股名称或代码：\n\n"
                    "示例：英伟达、MSFT、腾讯、00700",
            default_text="",
            ok="分析",
            cancel="取消",
            dimensions=(220, 24),
        ).run()
        if not response.clicked:
            return
        keyword = response.text.strip()
        if not keyword:
            return
        replace_menu_item_title(self._global_menu_item, "🌎 分析中…")
        threading.Thread(
            target=self._run_global_analysis,
            args=(keyword,),
            daemon=True,
        ).start()

    def _run_global_analysis(self, keyword):
        report = None
        name = keyword
        error_message = None
        try:
            from global_fundamentals import fetch_and_format
            data, report = fetch_and_format(keyword)
            name = f"{data.security.name}({data.security.symbol})"
        except Exception as error:
            error_message = str(error)
            self.log(f"港美股分析失败 {keyword}: {error}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")

        def _finish():
            replace_menu_item_title(self._global_menu_item, "🌎 港美股分析")
            if report:
                path = os.path.join(self.log_dir, "_global_fundamentals_report.txt")
                try:
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(report)
                    subprocess.Popen(
                        ["open", "-a", "TextEdit", path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self.log(f"港美股分析已生成并打开: {name} → {path}", "INFO")
                except Exception as error:
                    self.log(f"打开港美股分析失败: {error}", "ERROR")
                    self._alert(title="港美股分析", message=report[:500])
            else:
                self._alert(
                    title="港美股分析失败",
                    message=f"「{keyword}」分析失败：{error_message or '未知错误'}",
                )

        self._dispatch_to_main(_finish)

    # ═══════════════════════════════════════
    # AI 分析（DeepSeek）
    # ═══════════════════════════════════════

    @rumps.clicked("🤖 AI 分析")
    def show_ai_analysis(self, _):
        """自由提问：模型按需调用程序报告/接口与网络检索，给出简洁结论。"""
        response = self._window(
            title="AI 分析",
            message="请输入你的问题（可自由提问）：\n\n"
                    "示例：茅台的护城河还在吗？\n"
                    "      全面分析一下 sh600519\n"
                    "      大盘现在能加仓吗？",
            default_text="",
            ok="提问",
            cancel="取消",
            dimensions=(260, 24),
        ).run()
        if not response.clicked:
            return
        keyword = response.text.strip()
        if not keyword:
            return
        replace_menu_item_title(self._ai_menu_item, "🤖 思考中…")
        threading.Thread(
            target=self._run_ai_analysis,
            args=(keyword,),
            daemon=True,
        ).start()

    def _ai_search_fn(self, keyword):
        """AI 分析的标的解析：先 A 股，后港美股。"""
        results = self.search_stock(keyword)
        if results:
            return results
        try:
            from global_fundamentals import GlobalFundamentalsClient
            matches = GlobalFundamentalsClient().search(keyword)
            if matches:
                return [{"code": item.symbol, "name": item.name} for item in matches]
        except Exception as error:
            self.log(f"港美股搜索失败（AI分析）: {error}", "WARNING")
        return []

    def _run_ai_analysis(self, question):
        report = None
        error_message = None
        try:
            import ai_analysis
            report = ai_analysis.ask(
                question,
                project_dir=self.log_dir,
                search_fn=self._ai_search_fn,
            )
        except Exception as error:
            error_message = str(error)
            self.log(f"AI问答失败 {question}: {error}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")

        def _finish():
            replace_menu_item_title(self._ai_menu_item, "🤖 AI 分析")
            if report:
                path = os.path.join(self.log_dir, ai_analysis.REPORT_FILE)
                try:
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(report)
                    subprocess.Popen(
                        ["open", "-a", "TextEdit", path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self.log(f"AI问答已生成并打开: {path}", "INFO")
                except Exception as error:
                    self.log(f"打开AI问答失败: {error}", "ERROR")
                    self._alert(title="AI 分析", message=report[:500])
            else:
                self._alert(
                    title="AI 分析失败",
                    message=f"「{question}」：{error_message or '未知错误'}",
                )

        self._dispatch_to_main(_finish)

    @rumps.clicked("➕ 添加股票")
    def add_stock_dialog(self, _):
        """添加股票对话框（支持代码、名称、拼音）"""
        # 创建输入窗口
        window = self._window(
            title="添加股票",
            message="请输入股票代码、名称或拼音首字母:\n\n"
                    "示例:\n"
                    "  代码: sh600522\n"
                    "  名称: 中天科技\n"
                    "  拼音: ztgj",
            default_text="",
            ok="搜索",
            cancel="取消",
            dimensions=(200, 24)
        )

        response = window.run()

        if response.clicked:  # 用户点击了"搜索"
            keyword = response.text.strip()

            if not keyword:
                return

            # 判断输入类型
            if re.match(r'^(sh|sz)\d{6}$', keyword.lower()):
                # 直接输入了股票代码
                code = keyword.lower()
                self.log(f"用户直接输入股票代码: {code}", "INFO")
                try:
                    self._add_stock_by_code(code)
                except Exception as e:
                    self.log(f"添加股票 {code} 异常: {e}\n{traceback.format_exc()}", "ERROR")
                    self._alert(title="添加失败", message=f"添加股票时出错：{e}")
            else:
                # 输入了名称或拼音，进行搜索
                self.log(f"用户搜索股票: {keyword}", "INFO")
                results = self.search_stock(keyword)

                if not results:
                    self.log(f"搜索 '{keyword}' 无结果", "INFO")
                    self._alert(
                        title="未找到",
                        message=f"未找到与 \"{keyword}\" 相关的股票！\n\n"
                                "请尝试其他关键词。"
                    )
                    return

                self.log(f"搜索 '{keyword}' 返回 {len(results)} 个结果: {[r['code'] for r in results[:5]]}", "INFO")

                # 显示搜索结果
                result_text = f"搜索 \"{keyword}\" 的结果:\n\n"
                for i, item in enumerate(results[:5], 1):
                    result_text += f"{i}. {item['name']} ({item['code']})\n"

                result_text += "\n请输入股票代码添加（如 sh600522）:"

                # 创建第二个输入窗口
                window2 = self._window(
                    title="选择股票",
                    message=result_text,
                    default_text=results[0]['code'] if results else "",
                    ok="添加",
                    cancel="取消",
                    dimensions=(200, 24)
                )

                response2 = window2.run()

                if response2.clicked:
                    code = response2.text.strip().lower()
                    self.log(f"用户选择添加: {code}", "INFO")
                    if re.match(r'^(sh|sz)\d{6}$', code):
                        try:
                            self._add_stock_by_code(code)
                        except Exception as e:
                            self.log(f"添加股票 {code} 异常: {e}\n{traceback.format_exc()}", "ERROR")
                            self._alert(title="添加失败", message=f"添加股票时出错：{e}")
                    else:
                        self.log(f"用户输入格式错误: {code}", "INFO")
                        self._alert(
                            title="格式错误",
                            message="请输入正确的股票代码格式！\n\n"
                                    "示例：sh600522 或 sz000858"
                        )

    def _add_stock_by_code(self, code):
        """根据代码添加股票"""
        self.log(f"开始添加股票: {code}", "INFO")

        # 获取股票数据
        try:
            data = self.get_stock_data(code)
        except Exception as e:
            self.log(f"获取 {code} 数据异常: {e}", "ERROR")
            data = None

        if not data:
            self.log(f"获取 {code} 数据失败，无法添加", "ERROR")
            self._alert(
                title="股票不存在",
                message=f"无法获取 {code} 的数据，请检查代码是否正确！"
            )
            return

        self.log(f"获取 {code} 数据成功: 价格={data['price']}, 涨跌={data['change_pct']:.2f}%", "INFO")

        # 存入缓存，这样 update_display 才能找到
        self.stock_data[code] = data

        # 获取股票名称（从数据中）
        stock_name = code
        try:
            url = f"https://hq.sinajs.cn/list={code}"
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.sina.com.cn'
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_data = resp.read().decode('gbk')
                match = re.search(r'"([^"]+)"', raw_data)
                if match:
                    parts = match.group(1).split(',')
                    stock_name = parts[0] if parts[0] else code
        except Exception as e:
            self.log(f"获取 {code} 名称失败: {e}", "ERROR")
            stock_name = code

        self.log(f"股票名称: {stock_name}", "INFO")

        # 检查是否已存在
        for item in self.watchlist:
            if item['code'] == code:
                # 如果已存在但未显示，设置为显示
                item['show_in_bar'] = True
                self.log(f"{code} 已在列表中，设为显示", "INFO")
                self._alert(
                    title="已存在",
                    message=f"{stock_name} ({code}) 已在列表中，已设为显示！"
                )
                self.update_display()
                return

        # 添加新股票
        # 使用简短名称（最多2个字）
        short_name = stock_name[:2] if len(stock_name) >= 2 else stock_name

        self.watchlist.append({
            "code": code,
            "name": short_name,
            "show_in_bar": True
        })

        self.save_watchlist()
        self.log(f"股票 {stock_name} ({code}) 已添加到列表，当前共 {len(self.watchlist)} 只", "INFO")

        # 更新显示（已在主线程，直接更新UI）
        self.update_display()

        self._alert(
            title="添加成功",
            message=f"已添加 {stock_name} ({code})\n\n"
                    f"价格: {data['price']:.2f}\n"
                    f"涨跌: {data['change_pct']:+.2f}%"
        )

    @rumps.clicked("➖ 删除股票")
    def delete_stock_dialog(self, _):
        """删除股票对话框（支持代码、名称、拼音）"""
        # 获取当前显示的个股列表
        stocks_to_delete = []
        for item in self.watchlist:
            if item.get('show_in_bar', False):
                # 检查是否是个股（不是指数）
                if not (item['code'].startswith('sh000') or item['code'].startswith('sz399')):
                    stocks_to_delete.append(item)

        if not stocks_to_delete:
            self._alert(
                title="没有可删除的股票",
                message="当前没有显示在通知栏的个股！\n\n"
                        "指数无法删除，只能删除个股。"
            )
            return

        # 构建股票列表字符串
        stock_list = ""
        for i, item in enumerate(stocks_to_delete, 1):
            stock_list += f"{i}. {item['name']} ({item['code']})\n"

        # 创建输入窗口
        window = self._window(
            title="删除股票",
            message=f"当前显示的个股:\n\n{stock_list}\n"
                    "请输入股票代码、名称或拼音首字母:",
            default_text="",
            ok="搜索",
            cancel="取消",
            dimensions=(200, 24)
        )

        response = window.run()

        if response.clicked:  # 用户点击了"搜索"
            keyword = response.text.strip()

            if not keyword:
                return

            # 判断输入类型
            if re.match(r'^(sh|sz)\d{6}$', keyword.lower()):
                # 直接输入了股票代码
                code = keyword.lower()
                self._delete_stock_by_code(code)
            else:
                # 输入了名称或拼音，从当前通知栏个股中筛选
                matched = [
                    item for item in stocks_to_delete
                    if keyword in item['name'] or keyword.lower() in item['code'].lower()
                ]

                if not matched:
                    self._alert(
                        title="未找到",
                        message=f"通知栏中未找到与 \"{keyword}\" 相关的股票！\n\n"
                                "请尝试其他关键词。"
                    )
                    return

                if len(matched) == 1:
                    # 只匹配到一只，直接删除
                    self._delete_stock_by_code(matched[0]['code'])
                else:
                    # 匹配到多只，让用户选择
                    result_text = f"匹配 \"{keyword}\" 的股票:\n\n"
                    for i, item in enumerate(matched, 1):
                        result_text += f"{i}. {item['name']} ({item['code']})\n"

                    result_text += "\n请输入股票代码删除（如 sh600522）:"

                    window2 = self._window(
                        title="选择股票",
                        message=result_text,
                        default_text=matched[0]['code'],
                        ok="删除",
                        cancel="取消",
                        dimensions=(200, 24)
                    )

                    response2 = window2.run()

                    if response2.clicked:
                        code = response2.text.strip().lower()
                        if re.match(r'^(sh|sz)\d{6}$', code):
                            self._delete_stock_by_code(code)
                        else:
                            self._alert(
                                title="格式错误",
                                message="请输入正确的股票代码格式！\n\n"
                                        "示例：sh600522 或 sz000858"
                            )

    def _delete_stock_by_code(self, code):
        """根据代码删除股票"""
        # 查找并删除股票
        for i, item in enumerate(self.watchlist):
            if item['code'] == code:
                # 检查是否是个股（不是指数）
                if item['code'].startswith('sh000') or item['code'].startswith('sz399'):
                    self._alert(
                        title="无法删除",
                        message="指数无法删除！\n\n"
                                "只能删除个股，不能删除指数。"
                    )
                    return

                # 删除股票
                stock_name = item['name']
                self.watchlist.pop(i)
                self.save_watchlist()

                # 更新显示（已在主线程，直接更新UI）
                self.update_display()

                self._alert(
                    title="删除成功",
                    message=f"已删除 {stock_name} ({code})\n\n"
                            f"当前还有 {len(self.watchlist)} 只股票/指数"
                )
                return

        self._alert(
            title="未找到",
            message=f"未找到股票代码 {code}\n\n"
                    "请检查代码是否正确。"
        )

    @rumps.clicked("⚙️ 设置")
    def show_settings(self, _):
        """显示设置"""
        self._alert(
            title="⚙️ 设置",
            message=f"当前配置:\n\n"
                    f"• 更新间隔: {self.update_interval}秒\n"
                    f"• 监控股票: {len(self.watchlist)}只\n"
                    f"• KOSPI: 暂未启用（仅保留代码记录）\n\n"
                    f"刷新时段: 工作日 08:30-16:00\n"
                    f"修改配置请编辑 stock_menubar.py"
        )

    def _toggle_floating(self, _):
        """切换浮动窗口显隐"""
        self.floating_window.toggle_visible()
        self._update_float_menu_title()

    def _on_floating_closed(self):
        """浮动窗口被 × 关闭时的回调"""
        self._update_float_menu_title()

    def _on_floating_mode_change(self):
        """浮动窗口模式切换时的回调"""
        self._update_float_menu_title()

    def _update_float_menu_title(self):
        mode = "菜单" if self.floating_window.expanded else "紧凑"
        if self.floating_window.visible:
            replace_menu_item_title(self._float_menu_item, f"📌 浮动窗口 ✓ ({mode})")
        else:
            replace_menu_item_title(self._float_menu_item, "📌 浮动窗口")

    @rumps.clicked("❌ 退出")
    def quit_app(self, _):
        """退出应用"""
        self.floating_window.close()
        rumps.quit_application()

def main():
    try:
        app = StockMenuBar()
    except RuntimeError as error:
        # stderr 由启动脚本重定向进 crash.log；stdout 在 nohup 下是 /dev/null，print 看不到
        print(f"[股票监控] {error}", file=sys.stderr)
        # 退出码 2 = 已有实例在运行。supervisor 会记录原因并退出，
        # 而不是误判成「程序正常退出」——否则重启看起来成功了，跑的还是卡死的旧进程。
        sys.exit(2)
    app.run()

if __name__ == "__main__":
    main()
