"""
A-share stock fundamental analysis module.

Python port of Android Fundamentals.kt / FundamentalsReport.kt / FundamentalsClient.kt.
Fetches data from East Money datacenter APIs, scores financial quality across 5 dimensions,
estimates fair valuation, scans announcements for governance signals, and generates a
multi-section text report.

Data sources (all East Money public endpoints):
- RPT_F10_FINANCE_GINCOME / GCASHFLOW / GBALANCE: income, cashflow, balance sheet
- RPT_VALUEANALYSIS_DET: daily PE/PB history
- RPT_F10_FN_MAINOP: main business breakdown
- np-anotice-stock.eastmoney.com: recent announcements

Usage:
    from fundamentals import FundamentalsClient, Fundamentals, FundamentalsReport

    client = FundamentalsClient()
    payload = client.fetch("sh600519", "贵州茅台")
    result = Fundamentals.analyze(payload)
    report = FundamentalsReport.format("贵州茅台", "sh600519", payload.price, "2026-09-24", result)
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import requests

from entry_structure import (
    EntryStructure,
    FundamentalBucket,
    combine_fundamental_and_technical,
    fundamental_bucket_from_score,
)

# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class FinancialPeriod:
    """Single report period's cumulative financial data (amounts in yuan)."""
    date: str
    revenue: float
    operate_cost: float
    net_profit: float
    deduct_net_profit: float
    operating_cash_flow: float
    sales_cash: float
    capex: float


@dataclass
class BalanceSheetSnapshot:
    """Balance sheet key items (amounts in yuan)."""
    date: str
    monetary_funds: float
    accounts_receivable: float
    other_receivable: float
    inventory: float
    goodwill: float
    short_loan: float
    long_loan: float
    bond_payable: float
    lease_liability: float
    total_assets: float
    total_liabilities: float
    parent_equity: float
    minority_equity: float

    @property
    def interest_bearing_debt(self) -> float:
        return self.short_loan + self.long_loan + self.bond_payable + self.lease_liability

    @property
    def net_cash(self) -> float:
        return self.monetary_funds - self.interest_bearing_debt


@dataclass
class QuarterPoint:
    """Single-quarter data derived from cumulative values (amounts in yuan, ratios as percent)."""
    label: str
    revenue: float
    net_profit: float
    deduct_net_profit: float = 0.0
    revenue_yoy: Optional[float] = None
    profit_yoy: Optional[float] = None
    deduct_yoy: Optional[float] = None
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    roe: Optional[float] = None


@dataclass
class MainBusinessItem:
    """One row from main business breakdown (income/ratio/gross_margin)."""
    date: str
    type: int  # 1=industry, 2=product, 3=region
    name: str
    income: float
    ratio: float
    gross_margin: Optional[float] = None


@dataclass
class ValuationPoint:
    """Daily valuation snapshot."""
    date: str
    pe_ttm: Optional[float] = None
    pb_mrq: Optional[float] = None


@dataclass
class PeerContext:
    """Peer comparison within same board/sector."""
    board_name: str
    total: int
    sample_size: int
    market_cap_rank: Optional[int] = None
    pe_median: Optional[float] = None
    pb_median: Optional[float] = None
    market_cap: Optional[float] = None


class RiskLevel(Enum):
    GOOD = "GOOD"
    WATCH = "WATCH"
    ALERT = "ALERT"


@dataclass
class RiskItem:
    level: RiskLevel
    text: str


class AnchorKind(Enum):
    SELF_HISTORY = "SELF_HISTORY"
    PEER = "PEER"


@dataclass
class ValuationAnchor:
    name: str
    low: float
    high: float
    note: str
    kind: AnchorKind


@dataclass
class Announcement:
    """一条公告；正文只对筛选出的重点公告按需获取。"""
    date: str
    title: str
    art_code: Optional[str] = None
    content: Optional[str] = None


@dataclass
class GovernanceItem:
    """Governance signal from announcement scanning.

    count 是同一原因在窗口内的条数（同一事项连发多条公告时只列最新一条）。
    assessment 是阅读正文后提炼的对股东影响判断；正文没取到时为 None，不编造。
    """
    date: str
    title: str
    level: RiskLevel
    reason: str
    count: int = 1
    assessment: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# 股东结构（对应 Android ShareholderAnalysis / ShareholderDisclosure）
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class InstitutionalPosition:
    """一家披露机构在某报告期的持仓股数。"""
    name: str
    shares: float


@dataclass
class ShareholderDisclosure:
    """单个报告期的公开披露：股东总户数 + 部分机构持仓。"""
    report_date: str
    holder_count: Optional[int]
    institutions: List[InstitutionalPosition]


@dataclass
class InstitutionalChange:
    """两期之间的机构持仓变动；estimated_funds 按当前价估算，不是真实成交额。"""
    name: str
    shares_delta: float
    estimated_funds: Optional[float]


@dataclass
class ShareholderAnalysis:
    """最新两期股东户数与公开披露机构持仓变化。"""
    latest_report_date: Optional[str]
    retail_holder_count: Optional[int]
    previous_report_date: Optional[str]
    previous_retail_holder_count: Optional[int]
    institutional_net_shares: Optional[float]
    institutional_net_funds: Optional[float]
    inflows: List[InstitutionalChange]
    outflows: List[InstitutionalChange]

    @property
    def retail_holder_change(self) -> Optional[int]:
        if self.retail_holder_count is not None and self.previous_retail_holder_count is not None:
            return self.retail_holder_count - self.previous_retail_holder_count
        return None

    @property
    def retail_holder_change_pct(self) -> Optional[float]:
        change = self.retail_holder_change
        if change is not None and self.previous_retail_holder_count:
            return change / self.previous_retail_holder_count * 100.0
        return None

    @property
    def conclusion(self) -> str:
        pct = self.retail_holder_change_pct
        retail_down = pct is not None and pct <= -5.0
        retail_up = pct is not None and pct >= 5.0
        funds = self.institutional_net_funds
        institution_in = funds is not None and funds > 0.0
        institution_out = funds is not None and funds < 0.0
        if retail_down and institution_in:
            return "散户/中小股东户数下降，披露机构持仓净流入，筹码结构改善，偏利好。"
        if retail_up and institution_out:
            return "散户/中小股东户数增加，披露机构持仓净流出，筹码趋于分散，偏风险。"
        if institution_in:
            return "披露机构持仓净流入，机构关注度边际增强，偏利好。"
        if institution_out:
            return "披露机构持仓净流出，机构关注度边际减弱，需警惕资金撤出风险。"
        if retail_down:
            return "散户/中小股东户数下降，筹码集中度有所改善，但机构流向不明。"
        if retail_up:
            return "散户/中小股东户数增加，筹码趋于分散，但机构流向不明。"
        return "股东结构暂无明确改善或恶化信号。"

    @classmethod
    def from_disclosures(
        cls,
        latest: Optional[ShareholderDisclosure],
        previous: Optional[ShareholderDisclosure],
        current_price: float,
    ) -> Optional["ShareholderAnalysis"]:
        if latest is None and previous is None:
            return None
        latest_positions = {
            inst.name.strip(): inst for inst in (latest.institutions if latest else [])
        }
        previous_positions = {
            inst.name.strip(): inst for inst in (previous.institutions if previous else [])
        }
        names = {
            name for name in set(latest_positions) | set(previous_positions) if name
        }
        changes: List[InstitutionalChange] = []
        for name in names:
            delta = (
                (latest_positions[name].shares if name in latest_positions else 0.0)
                - (previous_positions[name].shares if name in previous_positions else 0.0)
            )
            if abs(delta) < 0.0001:
                continue
            changes.append(
                InstitutionalChange(
                    name=name,
                    shares_delta=delta,
                    estimated_funds=delta * current_price if current_price > 0.0 else None,
                )
            )
        both_periods = latest is not None and previous is not None
        net_shares = sum(c.shares_delta for c in changes) if both_periods else None
        net_funds = (
            sum(c.estimated_funds or 0.0 for c in changes)
            if both_periods and current_price > 0.0
            else None
        )
        return cls(
            latest_report_date=latest.report_date if latest else None,
            retail_holder_count=latest.holder_count if latest else None,
            previous_report_date=previous.report_date if previous else None,
            previous_retail_holder_count=previous.holder_count if previous else None,
            institutional_net_shares=net_shares,
            institutional_net_funds=net_funds,
            inflows=sorted(
                [c for c in changes if c.shares_delta > 0.0],
                key=lambda c: -c.shares_delta,
            ),
            outflows=sorted(
                [c for c in changes if c.shares_delta < 0.0],
                key=lambda c: c.shares_delta,
            ),
        )


@dataclass
class FundamentalResult:
    """Complete analysis output."""
    quarters: List[QuarterPoint]
    deduct_ratio: Optional[float]
    gross_margin: Optional[float]
    net_margin: Optional[float]
    ocf_to_profit: Optional[float]
    cash_to_revenue: Optional[float]
    net_cash_ratio: Optional[float]
    roe: Optional[float]
    eps: Optional[float]
    ttm_net_profit: Optional[float]
    ttm_deduct_net_profit: Optional[float]
    bps: Optional[float]
    current_pe: Optional[float]
    current_pb: Optional[float]
    latest: Optional[FinancialPeriod]
    balance: Optional[BalanceSheetSnapshot]
    main_business: List[MainBusinessItem]
    governance: List[GovernanceItem]
    announcements_scanned: int
    announcement_from: Optional[str]
    announcement_to: Optional[str]
    peers: Optional[PeerContext]
    score: int
    verdict: str
    risks: List[RiskItem]
    anchors: List[ValuationAnchor]
    fair_low: Optional[float]
    fair_high: Optional[float]
    pe_percentile: Optional[float]
    pb_percentile: Optional[float]
    notes: List[str]
    shareholder: Optional[ShareholderAnalysis] = None


@dataclass
class FundamentalPayload:
    """Raw data bundle fetched from East Money."""
    code: str
    name: str
    price: float
    total_shares: float
    periods: List[FinancialPeriod]
    balance: Optional[BalanceSheetSnapshot]
    valuations: List[ValuationPoint]
    peers: Optional[PeerContext]
    main_business: List[MainBusinessItem]
    announcements: List[Announcement]
    shareholder: Optional[ShareholderAnalysis] = None


# ──────────────────────────────────────────────────────────────────────────────
# ReportMarkup — «» markers for UI highlighting
# ──────────────────────────────────────────────────────────────────────────────


class ReportMarkup:
    """Wrap key numbers in << >> markers for UI highlighting."""
    OPEN = "«"
    CLOSE = "»"

    @staticmethod
    def red(value: str) -> str:
        return ReportMarkup.OPEN + value + ReportMarkup.CLOSE

    @staticmethod
    def plain(text: str) -> str:
        return text.replace(ReportMarkup.OPEN, "").replace(ReportMarkup.CLOSE, "")


# ──────────────────────────────────────────────────────────────────────────────
# Governance — announcement keyword scanning
# ──────────────────────────────────────────────────────────────────────────────

_WINDOW_DAYS = 180

_ALERT_KEYWORDS = [
    ("立案", "被立案调查"), ("处罚", "行政处罚"), ("违规", "违规事项"),
    ("冻结", "股份被冻结"), ("终止上市", "终止上市"), ("退市", "退市相关"),
    ("无法表示意见", "审计意见异常"), ("否定意见", "审计意见异常"),
    ("保留意见", "审计意见非标"), ("监管措施", "被采取监管措施"),
    ("公开谴责", "公开谴责"), ("警示函", "监管警示函"),
    ("纪律处分", "纪律处分"), ("会计差错", "会计差错更正"),
    # 业绩变脸的最早信号通常在这里，比财报快一到两个季度。
    # 关键词必须带「业绩」语境：裸「下修」会把可转债「向下修正转股价格」误判成业绩下修。
    ("预亏", "业绩预亏"), ("预减", "业绩预减"), ("预降", "业绩预降"),
    ("业绩下修", "业绩下修"), ("下修业绩", "业绩下修"),
    ("业绩修正", "业绩预告修正"), ("业绩预告修正", "业绩预告修正"),
]

_GOOD_KEYWORDS = [
    ("解除质押", "质押解除"), ("回购", "股份回购"), ("增持", "股东增持"),
    ("分红", "分红"), ("权益分派", "权益分派"), ("股权激励", "股权激励"),
    ("预增", "业绩预增"),
]

_WATCH_KEYWORDS = [
    ("向特定对象发行", "定增/再融资事项"), ("非公开发行", "定增/再融资事项"),
    ("定向增发", "定增/再融资事项"), ("定增", "定增/再融资事项"),
    ("再融资", "定增/再融资事项"),
    ("质押", "股东质押"), ("减持", "股东减持"), ("问询", "交易所问询"),
    ("关注函", "交易所关注函"), ("诉讼", "涉及诉讼"), ("仲裁", "涉及仲裁"),
    ("减值", "计提减值"), ("商誉", "商誉事项"), ("重组", "重大资产重组"),
    ("风险提示", "风险提示"), ("更正", "公告更正"), ("解禁", "限售股解禁"),
    ("变更会计师", "更换年审会计师"), ("延期回复", "延期回复问询"),
]


def _match_keyword(title: str, keywords: List[Tuple[str, str]]) -> Optional[str]:
    for kw, reason in keywords:
        if kw in title:
            return reason
    return None


def _severity(level: RiskLevel) -> int:
    return {RiskLevel.ALERT: 0, RiskLevel.WATCH: 1, RiskLevel.GOOD: 2}[level]


class AnnouncementSummary:
    """阅读公告正文后提炼对股东的影响判断；不把原文直接展示给用户。

    对应 Android AnnouncementSummary.kt。正文只用于内部判断，报告里
    展示的是结论，不是公告原文。
    """

    @staticmethod
    def assess(title: str, reason: str, content: Optional[str]) -> Optional[str]:
        plain = AnnouncementSummary.to_plain_text(content)
        if plain is None:
            return None
        if reason == "定增/再融资事项":
            return AnnouncementSummary._private_placement(title, plain)
        if any(k in reason for k in ("处罚", "立案", "监管", "纪律", "违规")):
            return AnnouncementSummary._negative_compliance(plain)
        if reason == "股东质押":
            return AnnouncementSummary._pledge(plain)
        if reason == "股东减持":
            return AnnouncementSummary._reduction(plain)
        if "诉讼" in reason or "仲裁" in reason:
            return AnnouncementSummary._litigation()
        if "减值" in reason or "商誉" in reason:
            return AnnouncementSummary._impairment()
        if reason in ("股份回购", "股东增持", "分红", "权益分派", "股权激励", "业绩预增"):
            return AnnouncementSummary._positive(plain)
        return AnnouncementSummary._generic(reason, plain)

    @staticmethod
    def to_plain_text(content: Optional[str]) -> Optional[str]:
        """去掉 HTML 标签得到纯文本；空白内容返回 None（表示正文没取到）。"""
        if not content or not content.strip():
            return None
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", content)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
            text = text.replace(a, b)
        text = re.sub(r"\s+", " ", text).strip()
        return text or None

    @staticmethod
    def _private_placement(title: str, plain: str) -> str:
        if "终止" in plain or "终止" in title:
            stage = "方案已终止"
        elif "注册生效" in plain or "注册获批" in plain:
            stage = "发行注册已获批"
        elif "获得上海证券交易所受理" in plain or "已获受理" in plain:
            stage = "发行申请已获交易所受理"
        elif "尚需" in plain and ("审核" in plain or "注册" in plain):
            stage = "发行仍处于审核/注册推进阶段"
        elif "董事会" in plain or "股东大会" in plain:
            stage = "公司已进入发行方案审议阶段"
        else:
            stage = "公司正在推进发行方案"

        if "补充流动资金" in plain and ("项目" in plain or "募投" in plain):
            use = "募集资金兼顾项目建设和补充流动资金"
        elif "偿还" in plain and "债务" in plain:
            use = "募集资金包含偿还债务用途"
        elif "补充流动资金" in plain:
            use = "募集资金包含补充流动资金用途"
        elif "募投" in plain or "募集资金" in plain:
            use = "募集资金用于项目建设等经营安排"
        else:
            use = "正文未明确披露具体资金用途"

        if any(k in plain for k in ("摊薄", "股本", "发行数量", "发行价格")):
            risk = "需关注发行价格、股本摊薄及审核/注册未通过风险"
        else:
            risk = "仍需关注发行价格、股本变化及审核/注册不确定性"

        expansion_risk = ""
        if AnnouncementSummary._is_expansion_project(plain):
            expansion_risk = (
                "；项目涉及扩产，需关注订单兑现、产能利用率、行业供需和投资回报，"
                "扩产过快可能导致盈利不及预期"
            )
        direction = "偏利空" if stage == "方案已终止" else "中性偏积极"
        return f"{direction}：{stage}；{use}；{risk}{expansion_risk}。"

    @staticmethod
    def _is_expansion_project(plain: str) -> bool:
        """募投方向是扩大产能：这类项目兑现慢，供给放量还可能压价。"""
        keywords = ("产能", "扩产", "产线", "建设期", "生产基地", "项目建设", "新增产能")
        return any(k in plain for k in keywords)

    @staticmethod
    def _negative_compliance(plain: str) -> str:
        if "信息披露" in plain:
            subject = "信息披露合规和处罚事项"
        elif "立案" in plain:
            subject = "立案调查事项"
        elif "整改" in plain:
            subject = "违规整改事项"
        else:
            subject = "监管或处罚事项"
        return f"偏利空：涉及{subject}，可能带来罚款、整改或追责影响，需关注后续处理结果。"

    @staticmethod
    def _pledge(plain: str) -> str:
        if "解除质押" in plain:
            return "偏利好：相关股份质押已解除，短期平仓压力下降；仍需关注股东后续融资安排。"
        return "风险提示：股东存在股份质押，需关注质押比例、平仓线及是否影响控制权稳定。"

    @staticmethod
    def _reduction(plain: str) -> str:
        if "完成减持" in plain or "减持完毕" in plain:
            return ("偏利空影响已阶段性落地：股东减持计划已完成，"
                    "后续仍需关注是否有新的减持安排。")
        return "偏利空：股东存在减持安排，可能增加短期供给和股价压力，需关注减持规模与期限。"

    @staticmethod
    def _litigation() -> str:
        return "风险提示：公司涉及诉讼或仲裁，需关注涉案金额、进展及是否对经营和资产造成实质影响。"

    @staticmethod
    def _impairment() -> str:
        return "偏利空：公告涉及减值或商誉事项，可能压低当期利润和净资产，需核对减值规模及可逆性。"

    @staticmethod
    def _positive(plain: str) -> str:
        if "回购" in plain:
            return "偏利好：公司推进股份回购，通常有助于改善市场预期；仍需关注回购规模和实际执行。"
        if "增持" in plain:
            return "偏利好：股东或管理层增持，体现一定信心；仍需关注增持规模和持续性。"
        if "分红" in plain or "权益分派" in plain:
            return ("偏利好：公司实施分红或权益分派，股东回报有所体现；"
                    "需结合盈利和现金流看持续性。")
        if "预增" in plain:
            return "偏利好：公告预计业绩增长，基本面预期改善；最终仍以正式财报为准。"
        return "偏利好：公告涉及公司激励或正面经营安排，需结合执行进度和财务影响跟踪。"

    @staticmethod
    def _generic(reason: str, plain: str) -> str:
        if any(k in reason for k in ("风险", "问询", "关注")):
            direction = "风险提示"
        else:
            direction = "中性"
        return f"{direction}：公告涉及{reason}，需结合后续进展判断对经营和股东权益的实际影响。"


def _parse_date_safe(value: str) -> Optional[datetime]:
    if len(value) < 10:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


def scan_announcements(
    items: List[Announcement],
    reference_date: str,
    window_days: int = _WINDOW_DAYS,
    limit: int = 6,
) -> List[GovernanceItem]:
    """Scan announcement titles for governance keywords."""
    ref = _parse_date_safe(reference_date)
    by_reason: Dict[str, List[GovernanceItem]] = {}

    for item in items:
        dt = _parse_date_safe(item.date)
        if dt is None:
            continue
        if ref is not None and (ref - dt).days > window_days:
            continue

        alert = _match_keyword(item.title, _ALERT_KEYWORDS)
        good = _match_keyword(item.title, _GOOD_KEYWORDS)
        watch = _match_keyword(item.title, _WATCH_KEYWORDS)

        if alert:
            level = RiskLevel.ALERT
            reason = alert
        elif good:
            level = RiskLevel.GOOD
            reason = good
        elif watch:
            level = RiskLevel.WATCH
            reason = watch
        else:
            continue

        by_reason.setdefault(reason, []).append(
            GovernanceItem(
                date=item.date,
                title=item.title,
                level=level,
                reason=reason,
                assessment=AnnouncementSummary.assess(item.title, reason, item.content),
            )
        )

    result: List[GovernanceItem] = []
    for group in by_reason.values():
        newest = max(group, key=lambda g: g.date)
        newest.count = len(group)
        result.append(newest)

    result.sort(key=lambda g: (_severity(g.level), g.date), reverse=False)
    # secondary sort: within same severity, newest first
    result.sort(key=lambda g: (_severity(g.level), g.date[:10]))
    # re-sort: severity first, then newest within same severity
    result.sort(key=lambda g: _severity(g.level))
    # Within same level, newest first
    from itertools import groupby as _groupby
    final: List[GovernanceItem] = []
    for _, grp in _groupby(result, key=lambda g: g.level):
        items_grp = sorted(grp, key=lambda g: g.date, reverse=True)
        final.extend(items_grp)

    return final[:limit]


# ──────────────────────────────────────────────────────────────────────────────
# FundamentalsClient — data fetching from East Money
# ──────────────────────────────────────────────────────────────────────────────

_HEADERS = {"Referer": "https://emweb.securities.eastmoney.com/"}
_SNAPSHOT_REFERER = "https://quote.eastmoney.com/"
_ANNOUNCEMENT_REFERER = "https://data.eastmoney.com/"

REPORT_INCOME = "RPT_F10_FINANCE_GINCOME"
REPORT_CASHFLOW = "RPT_F10_FINANCE_GCASHFLOW"
REPORT_BALANCE = "RPT_F10_FINANCE_GBALANCE"
REPORT_MAINOP = "RPT_F10_FN_MAINOP"
REPORT_VALUATION = "RPT_VALUEANALYSIS_DET"

INCOME_COLUMNS = "SECUCODE,REPORT_DATE,TOTAL_OPERATE_INCOME,OPERATE_COST,PARENT_NETPROFIT,DEDUCT_PARENT_NETPROFIT"
CASHFLOW_COLUMNS = "SECUCODE,REPORT_DATE,NETCASH_OPERATE,SALES_SERVICES,CONSTRUCT_LONG_ASSET"
BALANCE_COLUMNS = (
    "SECUCODE,REPORT_DATE,MONETARYFUNDS,NOTE_ACCOUNTS_RECE,ACCOUNTS_RECE,"
    "TOTAL_OTHER_RECE,OTHER_RECE,INVENTORY,GOODWILL,SHORT_LOAN,LONG_LOAN,"
    "BOND_PAYABLE,LEASE_LIAB,TOTAL_ASSETS,TOTAL_LIABILITIES,TOTAL_PARENT_EQUITY,MINORITY_EQUITY"
)
MAINOP_COLUMNS = "SECUCODE,REPORT_DATE,MAINOP_TYPE,ITEM_NAME,MAIN_BUSINESS_INCOME,MBI_RATIO,GROSS_RPOFIT_RATIO"

PERIOD_PAGE_SIZE = 16
ANNOUNCEMENT_PAGE_SIZE = 100
MAX_ANNOUNCEMENT_PAGES = 4
MAX_ANNOUNCEMENT_DETAIL_PAGES = 8
VALUATION_PAGE_SIZE = 3000
MAX_VALUATION_PAGES = 3


def _secucode(code: str) -> str:
    """Convert 'sh600519' → '600519.SH'."""
    plain = code[-6:]
    if code.startswith("sh"):
        suffix = "SH"
    elif code.startswith("bj"):
        suffix = "BJ"
    else:
        suffix = "SZ"
    return f"{plain}.{suffix}"


def _secid(code: str) -> str:
    """Convert 'sh600519' → '1.600519'."""
    return ("1." if code.startswith("sh") else "0.") + code[-6:]


def _short_date(raw: str) -> str:
    return raw[:10]


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        v = float(val)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


def _safe_optional_float(val: Any) -> Optional[float]:
    try:
        v = float(val)
        return None if math.isnan(v) or v <= 0 else v
    except (TypeError, ValueError):
        return None


def _get_json(url: str, referer: str = "", timeout: int = 20, retries: int = 2, delay: float = 0.8) -> Any:
    """Fetch JSON with retry logic."""
    headers = {"Referer": referer} if referer else {}
    last_exc: Optional[Exception] = None
    for _ in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_exc = e
            import time
            time.sleep(delay)
    raise RuntimeError(f"HTTP request failed after {retries + 1} attempts: {last_exc}")


def _get_html_text(
    url: str,
    referer: str = "",
    encoding: str = "utf-8",
    timeout: int = 20,
    retries: int = 2,
    delay: float = 0.8,
) -> str:
    """Fetch an HTML/text page and decode with the given charset（新浪股东页是 GB18030）。"""
    headers = {"Referer": referer} if referer else {}
    last_exc: Optional[Exception] = None
    for _ in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.content.decode(encoding, errors="replace")
        except Exception as e:
            last_exc = e
            import time
            time.sleep(delay)
    raise RuntimeError(f"HTTP request failed after {retries + 1} attempts: {last_exc}")


def _fetch_sina_price(code: str) -> float:
    """新浪实时行情现价，东财 push2 快照失败时的兜底（对应 Android SinaClient.quote）。

    集合竞价前新浪会返回 0 价，此时退回昨收，避免分析直接失败。
    """
    try:
        resp = requests.get(
            f"https://hq.sinajs.cn/list={code}",
            headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        resp.raise_for_status()
        resp.encoding = "gbk"
        match = re.search(r'"([^"]*)"', resp.text)
        if not match:
            return 0.0
        parts = match.group(1).split(",")
        if len(parts) < 4:
            return 0.0
        price = _safe_float(parts[3], 0.0)
        if price > 0:
            return price
        return _safe_float(parts[2], 0.0)  # 昨收兜底
    except Exception:
        return 0.0


def _data_array(json_obj: Any) -> Optional[list]:
    """Extract result.data array from East Money response."""
    try:
        result = json_obj.get("result") or {}
        return result.get("data")
    except (AttributeError, TypeError):
        return None


def _total_count(json_obj: Any) -> int:
    try:
        result = json_obj.get("result") or {}
        return result.get("count", 0)
    except (AttributeError, TypeError):
        return 0


class FundamentalsClient:
    """Fetch fundamental data from East Money APIs."""

    def fetch(self, code: str, name: str) -> FundamentalPayload:
        secucode = _secucode(code)
        plain = code[-6:]

        # Income + cashflow → periods
        income_json = self._fetch_f10(REPORT_INCOME, INCOME_COLUMNS, secucode, PERIOD_PAGE_SIZE)
        cash_json = self._fetch_f10(REPORT_CASHFLOW, CASHFLOW_COLUMNS, secucode, PERIOD_PAGE_SIZE)
        periods = _parse_periods(income_json, cash_json)
        if not periods:
            raise RuntimeError(f"没有取到 {name} 的财报数据")

        # Balance sheet
        balance = None
        try:
            balance_json = self._fetch_f10(REPORT_BALANCE, BALANCE_COLUMNS, secucode, 1)
            balance = _parse_balance(balance_json)
        except Exception:
            pass

        # Valuations + board
        valuations: List[ValuationPoint] = []
        board_info: Optional[Tuple[str, str]] = None
        try:
            valuations, board_info = self._fetch_valuations(plain)
        except Exception:
            pass

        # Peers
        peers = None
        if board_info is not None:
            try:
                peers = self._fetch_peers(plain, board_info[0], board_info[1])
            except Exception:
                pass

        # Snapshot (price + shares)
        snapshot = None
        try:
            snapshot = self._fetch_snapshot(code)
        except Exception:
            pass

        price = 0.0
        total_shares = 0.0
        if snapshot and snapshot.get("price", 0) > 0:
            price = snapshot["price"]
            total_shares = snapshot.get("shares", 0.0)
        if price <= 0:
            # 东财快照挂掉时退回新浪实时行情（与 Android 版一致），不要直接失败
            price = _fetch_sina_price(code)
        if price <= 0:
            raise RuntimeError(f"没有取到 {name} 的当前价格")
        if total_shares <= 0 and peers and peers.market_cap and peers.market_cap > 0:
            total_shares = peers.market_cap / price

        # Main business
        main_business: List[MainBusinessItem] = []
        try:
            mb_json = self._fetch_f10(REPORT_MAINOP, MAINOP_COLUMNS, secucode, 40)
            main_business = _parse_main_business(mb_json)
        except Exception:
            pass

        # Announcements
        announcements: List[Announcement] = []
        try:
            announcements = self._fetch_announcements(plain)
        except Exception:
            pass

        # 股东结构（新浪股东页；机构资金估算需要现价，放在 price 解析之后）
        shareholder = None
        try:
            shareholder = self._fetch_shareholder_analysis(plain, price)
        except Exception:
            pass

        return FundamentalPayload(
            code=code,
            name=name,
            price=price,
            total_shares=total_shares,
            periods=periods,
            balance=balance,
            valuations=valuations,
            peers=peers,
            main_business=main_business,
            announcements=announcements,
            shareholder=shareholder,
        )

    def _fetch_shareholder_analysis(
        self, plain: str, price: float
    ) -> Optional[ShareholderAnalysis]:
        """最新两期股东户数 + 公开披露机构持仓变化（对应 Android fetchShareholderAnalysis）。"""
        html = _get_html_text(
            f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_StockHolder/stockid/{plain}.phtml",
            referer="https://vip.stock.finance.sina.com.cn/",
            encoding="gb18030",
        )
        disclosures = parse_shareholder_page(html)
        return ShareholderAnalysis.from_disclosures(
            latest=disclosures[0] if disclosures else None,
            previous=disclosures[1] if len(disclosures) > 1 else None,
            current_price=price,
        )

    def _fetch_f10(self, report: str, columns: str, secucode: str, page_size: int) -> Any:
        url = (
            f"https://datacenter.eastmoney.com/securities/api/data/v1/get"
            f"?reportName={report}&columns={columns}"
            f"&filter=(SECUCODE%3D%22{secucode}%22)"
            f"&pageSize={page_size}&sortColumns=REPORT_DATE&sortTypes=-1"
            f"&source=HSF10&client=PC"
        )
        return _get_json(url, referer=_HEADERS["Referer"])

    def _fetch_snapshot(self, code: str) -> Optional[Dict[str, Any]]:
        url = (
            f"https://push2.eastmoney.com/api/qt/stock/get"
            f"?secid={_secid(code)}&fltt=2&fields=f43,f58,f84,f127"
        )
        data = _get_json(url, referer=_SNAPSHOT_REFERER, timeout=8, retries=2, delay=0.8)
        d = data.get("data")
        if not d:
            return None
        price = _safe_float(d.get("f43"), float("nan"))
        if math.isnan(price) or price <= 0:
            return None
        shares = _safe_float(d.get("f84"), 0.0)
        return {"name": d.get("f58", ""), "price": price, "shares": shares}

    def _fetch_valuations(self, plain: str) -> Tuple[List[ValuationPoint], Optional[Tuple[str, str]]]:
        all_points: List[ValuationPoint] = []
        board: Optional[Tuple[str, str]] = None
        count = float("inf")
        page = 1
        while page <= MAX_VALUATION_PAGES and len(all_points) < count:
            url = (
                f"https://datacenter-web.eastmoney.com/api/data/v1/get"
                f"?reportName={REPORT_VALUATION}"
                f"&columns=TRADE_DATE,PE_TTM,PB_MRQ,TOTAL_MARKET_CAP,BOARD_CODE,BOARD_NAME"
                f"&filter=(SECURITY_CODE%3D%22{plain}%22)"
                f"&pageSize={VALUATION_PAGE_SIZE}&page={page}"
                f"&sortColumns=TRADE_DATE&sortTypes=-1"
            )
            json_data = _get_json(url, referer=_HEADERS["Referer"])
            rows = _data_array(json_data)
            if not rows:
                break
            if board is None and rows:
                bcode = rows[0].get("BOARD_CODE", "")
                bname = rows[0].get("BOARD_NAME", "")
                if bcode:
                    board = (bcode, bname)
            for row in rows:
                date = _short_date(row.get("TRADE_DATE", ""))
                if len(date) != 10:
                    continue
                pe = row.get("PE_TTM")
                pb = row.get("PB_MRQ")
                all_points.append(ValuationPoint(
                    date=date,
                    pe_ttm=pe if isinstance(pe, (int, float)) and not math.isnan(pe) and pe > 0 else None,
                    pb_mrq=pb if isinstance(pb, (int, float)) and not math.isnan(pb) and pb > 0 else None,
                ))
            count = _total_count(json_data)
            page += 1
        all_points.sort(key=lambda p: p.date)
        return all_points, board

    def _fetch_peers(self, plain: str, board_code: str, board_name: str) -> Optional[PeerContext]:
        # Get latest valuation date for this stock
        url_latest = (
            f"https://datacenter-web.eastmoney.com/api/data/v1/get"
            f"?reportName={REPORT_VALUATION}"
            f"&columns=TRADE_DATE,PE_TTM,PB_MRQ,TOTAL_MARKET_CAP,BOARD_CODE,BOARD_NAME"
            f"&filter=(SECURITY_CODE%3D%22{plain}%22)"
            f"&pageSize=1&page=1&sortColumns=TRADE_DATE&sortTypes=-1"
        )
        json_data = _get_json(url_latest, referer=_HEADERS["Referer"])
        rows = _data_array(json_data)
        if not rows:
            return None
        latest_date = _short_date(rows[0].get("TRADE_DATE", ""))
        if len(latest_date) != 10:
            return None

        # Fetch board snapshot
        url = (
            f"https://datacenter-web.eastmoney.com/api/data/v1/get"
            f"?reportName={REPORT_VALUATION}"
            f"&columns=SECURITY_CODE,PE_TTM,PB_MRQ,TOTAL_MARKET_CAP"
            f"&filter=(BOARD_CODE%3D%22{board_code}%22)(TRADE_DATE%3D%27{latest_date}%27)"
            f"&pageSize=500&page=1&sortColumns=TOTAL_MARKET_CAP&sortTypes=-1"
        )
        json_data = _get_json(url, referer=_HEADERS["Referer"])
        rows = _data_array(json_data)
        if not rows:
            return None

        pe_list: List[float] = []
        pb_list: List[float] = []
        rank: Optional[int] = None
        market_cap: Optional[float] = None

        for i, row in enumerate(rows):
            row_pe = row.get("PE_TTM")
            if isinstance(row_pe, (int, float)) and not math.isnan(row_pe) and row_pe > 0:
                pe_list.append(row_pe)
            row_pb = row.get("PB_MRQ")
            if isinstance(row_pb, (int, float)) and not math.isnan(row_pb) and row_pb > 0:
                pb_list.append(row_pb)
            if row.get("SECURITY_CODE") == plain:
                rank = i + 1
                mc = row.get("TOTAL_MARKET_CAP")
                if isinstance(mc, (int, float)) and not math.isnan(mc):
                    market_cap = mc

        total = max(_total_count(json_data), len(rows))
        return PeerContext(
            board_name=board_name,
            total=total,
            sample_size=len(pe_list),
            market_cap_rank=rank,
            pe_median=_median(pe_list),
            pb_median=_median(pb_list),
            market_cap=market_cap,
        )

    def _fetch_announcements(self, plain: str) -> List[Announcement]:
        all_items: List[Announcement] = []
        for page in range(1, MAX_ANNOUNCEMENT_PAGES + 1):
            url = (
                f"https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1"
                f"&page_size={ANNOUNCEMENT_PAGE_SIZE}&page_index={page}"
                f"&ann_type=A&client_source=web"
                f"&stock_list={plain}&f_node=0&s_node=0"
            )
            try:
                data = _get_json(url, referer=_ANNOUNCEMENT_REFERER, timeout=15)
            except Exception:
                break
            try:
                rows = (data.get("data") or {}).get("list") or []
            except (AttributeError, TypeError):
                break
            if not rows:
                break
            for row in rows:
                date = (row.get("notice_date") or "")[:10]
                title = _clean_title(row.get("title", ""))
                if len(date) == 10 and title:
                    all_items.append(Announcement(
                        date=date, title=title, art_code=_first_present(
                            row, ("art_code", "artCode", "notice_id", "id")
                        ),
                    ))
            if len(rows) < ANNOUNCEMENT_PAGE_SIZE:
                break
            # Check window coverage
            if _announcement_window_covered(all_items):
                break

        # Deduplicate and sort
        seen = set()
        deduped: List[Announcement] = []
        for a in all_items:
            key = (a.date, a.title)
            if key not in seen:
                seen.add(key)
                deduped.append(a)
        deduped.sort(key=lambda a: a.date, reverse=True)

        # 只为标题筛选出的重点公告请求正文，避免一次基本面分析请求数百条详情
        if deduped:
            reference_date = max(a.date for a in deduped)
            detail_keys = {
                (g.date, g.title) for g in scan_announcements(deduped, reference_date)
            }
            for i, item in enumerate(deduped):
                if (item.date, item.title) in detail_keys:
                    deduped[i] = Announcement(
                        date=item.date,
                        title=item.title,
                        art_code=item.art_code,
                        content=self._fetch_announcement_content(item),
                    )
        return deduped

    def _fetch_announcement_content(self, item: Announcement) -> Optional[str]:
        """只对重点公告分页取正文；取不到返回 None，不编造内容。"""
        art_code = item.art_code
        if not art_code:
            return None
        pages: List[str] = []
        for page in range(1, MAX_ANNOUNCEMENT_DETAIL_PAGES + 1):
            url = (
                f"https://np-cnotice-wap.eastmoney.com/api/content/ann/rich"
                f"?client_source=wap&page_index={page}&is_rich=1"
                f"&art_code={art_code}&business=WapGonggao"
            )
            try:
                data = _get_json(url, referer=_ANNOUNCEMENT_REFERER, timeout=12,
                                 retries=1)
                content = _parse_announcement_content(data)
            except Exception:
                content = None
            if not content:
                if pages:
                    break
                continue
            pages.append(content)
        joined = "\n".join(pages)
        return joined if joined.strip() else None


def _clean_title(raw: str) -> str:
    title = raw.strip()
    colon_idx = -1
    for i, ch in enumerate(title):
        if ch in (":", "："):
            colon_idx = i
            break
    if 0 < colon_idx <= 12:
        return title[colon_idx + 1:].strip()
    return title


def _first_present(row: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    """按候选键名取第一个非空字段；公告 id 在不同接口里字段名不一致。"""
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return None


def _parse_announcement_content(data: Any) -> Optional[str]:
    """从公告正文接口响应里抽出正文文本；解析不出返回 None。"""
    raw = _find_content(data)
    return _extract_pages(raw)


def _find_content(value: Any) -> Optional[str]:
    """递归找正文字段（notice_content/content/...），取第一个长度足够的。"""
    if isinstance(value, dict):
        for key in ("notice_content", "notice_content_html", "content",
                    "content_html", "body"):
            text = value.get(key)
            if isinstance(text, str) and len(text) >= 8:
                return text
        for child in value.values():
            found = _find_content(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_content(child)
            if found:
                return found
    return None


def _extract_pages(raw: Optional[str]) -> Optional[str]:
    """正文可能是 {"page1": ..., "page2": ...} 形式的 JSON 串，按页序拼接。"""
    if not raw or not raw.strip():
        return None
    try:
        rich = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if isinstance(rich, dict):
        page_keys = sorted(
            (k for k in rich if re.fullmatch(r"page\d+", k)),
            key=lambda k: int(k[4:]) if k[4:].isdigit() else 0,
        )
        pages = [rich[k] for k in page_keys if isinstance(rich[k], str) and rich[k]]
        if pages:
            return "\n".join(pages)
    return raw


def _announcement_window_covered(items: List[Announcement]) -> bool:
    dates = [a.date for a in items if len(a.date) == 10]
    if len(dates) < 2:
        return False
    newest = max(dates)
    oldest = min(dates)
    try:
        d1 = datetime.strptime(oldest, "%Y-%m-%d")
        d2 = datetime.strptime(newest, "%Y-%m-%d")
        return (d2 - d1).days >= _WINDOW_DAYS
    except ValueError:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Parsing helpers (static, shared)
# ──────────────────────────────────────────────────────────────────────────────

def _parse_periods(income_json: Any, cash_json: Any) -> List[FinancialPeriod]:
    income_rows = _data_array(income_json)
    if not income_rows:
        return []

    cash_by_date: Dict[str, dict] = {}
    cash_rows = _data_array(cash_json)
    if cash_rows:
        for row in cash_rows:
            d = _short_date(row.get("REPORT_DATE", ""))
            if len(d) == 10:
                cash_by_date[d] = row

    periods: List[FinancialPeriod] = []
    for row in income_rows:
        date = _short_date(row.get("REPORT_DATE", ""))
        if len(date) != 10:
            continue
        cash_row = cash_by_date.get(date)
        if cash_row is None:
            continue
        periods.append(FinancialPeriod(
            date=date,
            revenue=_safe_float(row.get("TOTAL_OPERATE_INCOME")),
            operate_cost=_safe_float(row.get("OPERATE_COST")),
            net_profit=_safe_float(row.get("PARENT_NETPROFIT")),
            deduct_net_profit=_safe_float(row.get("DEDUCT_PARENT_NETPROFIT")),
            operating_cash_flow=_safe_float(cash_row.get("NETCASH_OPERATE")),
            sales_cash=_safe_float(cash_row.get("SALES_SERVICES")),
            capex=_safe_float(cash_row.get("CONSTRUCT_LONG_ASSET")),
        ))
    periods.sort(key=lambda p: p.date)
    return periods


def _parse_balance(json_data: Any) -> Optional[BalanceSheetSnapshot]:
    rows = _data_array(json_data)
    if not rows:
        return None
    row = rows[0]
    receivable = _safe_float(row.get("NOTE_ACCOUNTS_RECE"), float("nan"))
    if math.isnan(receivable):
        receivable = _safe_float(row.get("ACCOUNTS_RECE"))
    other_receivable = _safe_float(row.get("TOTAL_OTHER_RECE"), float("nan"))
    if math.isnan(other_receivable):
        other_receivable = _safe_float(row.get("OTHER_RECE"))
    return BalanceSheetSnapshot(
        date=_short_date(row.get("REPORT_DATE", "")),
        monetary_funds=_safe_float(row.get("MONETARYFUNDS")),
        accounts_receivable=receivable,
        other_receivable=other_receivable,
        inventory=_safe_float(row.get("INVENTORY")),
        goodwill=_safe_float(row.get("GOODWILL")),
        short_loan=_safe_float(row.get("SHORT_LOAN")),
        long_loan=_safe_float(row.get("LONG_LOAN")),
        bond_payable=_safe_float(row.get("BOND_PAYABLE")),
        lease_liability=_safe_float(row.get("LEASE_LIAB")),
        total_assets=_safe_float(row.get("TOTAL_ASSETS")),
        total_liabilities=_safe_float(row.get("TOTAL_LIABILITIES")),
        parent_equity=_safe_float(row.get("TOTAL_PARENT_EQUITY")),
        minority_equity=_safe_float(row.get("MINORITY_EQUITY")),
    )


def _parse_main_business(json_data: Any) -> List[MainBusinessItem]:
    rows = _data_array(json_data)
    if not rows:
        return []
    items: List[MainBusinessItem] = []
    for row in rows:
        date = _short_date(row.get("REPORT_DATE", ""))
        name = row.get("ITEM_NAME", "")
        if len(date) != 10 or not name:
            continue
        gm = row.get("GROSS_RPOFIT_RATIO")
        items.append(MainBusinessItem(
            date=date,
            type=int(row.get("MAINOP_TYPE", 0) or 0),
            name=name,
            income=_safe_float(row.get("MAIN_BUSINESS_INCOME")),
            ratio=_safe_float(row.get("MBI_RATIO")),
            gross_margin=gm if isinstance(gm, (int, float)) and not math.isnan(gm) else None,
        ))
    return items


# ──────────────────────────────────────────────────────────────────────────────
# 股东结构解析（对应 Android FundamentalsClient.parseShareholderPage / parseShareholderDisclosures）
# ──────────────────────────────────────────────────────────────────────────────

_INSTITUTION_NAME_KEYWORDS = (
    "基金", "证券", "保险", "信托", "资管", "社保", "QFII", "银行", "投资",
    "资产管理", "结算", "有限公司", "股份有限公司", "集团",
)

_INSTITUTION_TYPE_KEYWORDS = (
    "基金", "证券", "保险", "信托", "资管", "社保", "QFII", "银行", "投资", "资产管理",
)

_HOLDER_DATE_KEYS = ("END_DATE", "REPORT_DATE", "HOLDER_DATE", "STATISTICS_DATE")


def _html_text(raw: str) -> str:
    """去掉 HTML 标签与实体，与 Android FundamentalsClient.htmlText 一致。"""
    text = re.sub(r"<br\s*/?>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .strip()
    )


def _is_institution_name(name: str) -> bool:
    return any(k.lower() in name.lower() for k in _INSTITUTION_NAME_KEYWORDS)


def _is_institution(row: Dict[str, Any], name: str) -> bool:
    holder_type = str(row.get("HOLDER_TYPE", "") or "").strip()
    if "个人" in holder_type or "自然人" in holder_type:
        return False
    return any(
        k.lower() in holder_type.lower() or k.lower() in name.lower()
        for k in _INSTITUTION_TYPE_KEYWORDS
    )


def _holder_date(row: Dict[str, Any]) -> Optional[str]:
    for key in _HOLDER_DATE_KEYS:
        date = _short_date(str(row.get(key, "") or ""))
        if len(date) == 10:
            return date
    return None


def _first_number(row: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    for key in keys:
        value = row.get(key)
        if isinstance(value, (int, float)) and not (isinstance(value, float) and math.isnan(value)):
            return float(value)
    return None


def parse_shareholder_page(html: str) -> List[ShareholderDisclosure]:
    """解析新浪股东页（vCI_StockHolder）：按报告期锚点切块，取股东总数与机构持仓。"""
    anchors = list(
        re.finditer(
            r"""<a\s+name=["'](\d{4}-\d{2}-\d{2})["']\s*>\s*</a>""",
            html,
            flags=re.IGNORECASE,
        )
    )
    if not anchors:
        return []
    disclosures: List[ShareholderDisclosure] = []
    for index, anchor in enumerate(anchors):
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(html)
        block = html[anchor.start():end]
        date = anchor.group(1)
        count_match = re.search(
            r"股东总数.*?<td[^>]*>\s*([0-9,]+)",
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        holder_count: Optional[int] = None
        if count_match:
            digits = count_match.group(1).replace(",", "")
            holder_count = int(digits) if digits.isdigit() else None
        institutions: List[InstitutionalPosition] = []
        for row_match in re.finditer(
            r"<tr\b[^>]*>(.*?)</tr>", block, flags=re.IGNORECASE | re.DOTALL
        ):
            cells = [
                _html_text(m.group(1))
                for m in re.finditer(
                    r"<td\b[^>]*>(.*?)</td>", row_match.group(1),
                    flags=re.IGNORECASE | re.DOTALL,
                )
            ]
            if len(cells) < 3 or not cells[0].isdigit():
                continue
            shares_text = re.sub(r"[^0-9.]", "", cells[2].replace(",", ""))
            try:
                shares = float(shares_text)
            except ValueError:
                continue
            name = cells[1].strip()
            if not _is_institution_name(name):
                continue
            institutions.append(InstitutionalPosition(name, shares))
        disclosures.append(ShareholderDisclosure(date, holder_count, institutions))
    return disclosures


def parse_shareholder_disclosures(
    holder_count_text: str, positions_text: str
) -> List[ShareholderDisclosure]:
    """JSON 口径的股东披露解析（东财 datacenter 字段），与 Android 同名方法一致。"""

    def _rows(raw: str) -> List[Dict[str, Any]]:
        try:
            data = json.loads(raw)
        except Exception:
            return []
        arr = _data_array(data)
        return [r for r in (arr or []) if isinstance(r, dict)]

    counts: Dict[str, Optional[int]] = {}
    for row in _rows(holder_count_text):
        date = _holder_date(row)
        if not date:
            continue
        count = _first_number(row, ("HOLDER_TOTAL_NUM", "HOLDER_NUM", "TOTAL_HOLDER_NUM"))
        counts[date] = int(count) if count is not None else None

    positions_by_date: Dict[str, List[InstitutionalPosition]] = {}
    for row in _rows(positions_text):
        date = _holder_date(row)
        if not date:
            continue
        name = next(
            (
                str(row.get(key, "") or "").strip()
                for key in ("HOLDER_NAME", "HOLDERNAME", "SHAREHOLDER_NAME")
                if str(row.get(key, "") or "").strip()
            ),
            "",
        )
        if not name or not _is_institution(row, name):
            continue
        shares = _first_number(row, ("HOLD_NUM", "HOLDNUM", "HOLD_NUM_NEW", "FREE_HOLDNUM"))
        if shares is None:
            continue
        positions_by_date.setdefault(date, []).append(InstitutionalPosition(name, shares))

    return [
        ShareholderDisclosure(date, counts.get(date), positions_by_date.get(date, []))
        for date in sorted(set(counts) | set(positions_by_date), reverse=True)
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Utility functions
# ──────────────────────────────────────────────────────────────────────────────

def _ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or den is None or den == 0.0:
        return None
    return num / den


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _percentile(values: List[float], p: float) -> Optional[float]:
    """Linear interpolation percentile; only positive values."""
    s = sorted(v for v in values if v > 0)
    if not s:
        return None
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * p
    lower = math.floor(idx)
    upper = math.ceil(idx)
    if lower == upper:
        return s[lower]
    return s[lower] + (s[upper] - s[lower]) * (idx - lower)


def _current_percentile(history: List[float], current: Optional[float]) -> Optional[float]:
    if current is None or current <= 0:
        return None
    values = [v for v in history if v > 0]
    if len(values) < 20:  # MIN_VALUATION_POINTS
        return None
    return sum(1 for v in values if v <= current) / len(values) * 100


def _num(v: float) -> str:
    return f"{v:.2f}"


def _price_fmt(v: float) -> str:
    return f"{v:.2f}"


def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _signed_pct(v: float) -> str:
    return f"{v:+.1f}%"


def _yi(v: float) -> str:
    """Amount in 亿 (100M yuan)."""
    return f"{v / 1e8:.2f}"


def _money(v: float) -> str:
    return f"{v:.2f}"


def _pct1(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}%"


def _num_or_dash(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}"


def _pe_text(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v <= 0:
        return "亏损（PE 不适用）"
    return f"{v:.2f} 倍"


def _mark(level: RiskLevel) -> str:
    return {RiskLevel.GOOD: "✅ ", RiskLevel.WATCH: "⚠️ ", RiskLevel.ALERT: "⛔ "}[level]


# ──────────────────────────────────────────────────────────────────────────────
# Fundamentals — analysis engine
# ──────────────────────────────────────────────────────────────────────────────

_QUARTERS = 8
_MIN_VALUATION_POINTS = 20
_MIN_YOY_POINTS = 4


def _consecutive_quarter(prev: Optional[FinancialPeriod], current: FinancialPeriod) -> bool:
    """Is prev the immediately preceding quarter of current (same year, month diff = 3)?"""
    if prev is None:
        return False
    try:
        cy = int(current.date[:4])
        cm = int(current.date[5:7])
        py = int(prev.date[:4])
        pm = int(prev.date[5:7])
        return cy == py and pm + 3 == cm
    except (ValueError, IndexError):
        return False


def _quarter_of(month: str) -> int:
    return {"03": 1, "06": 2, "09": 3}.get(month, 4)


def _single_quarter_periods(periods: List[FinancialPeriod], limit: int = _QUARTERS) -> List[QuarterPoint]:
    """Convert cumulative periods to single-quarter data."""
    sorted_p = sorted(periods, key=lambda p: p.date)
    if not sorted_p:
        return []

    singles: List[Dict[str, Any]] = []
    for i, p in enumerate(sorted_p):
        prev = sorted_p[i - 1] if i > 0 else None
        chained = _consecutive_quarter(prev, p)
        singles.append({
            "date": p.date,
            "revenue": p.revenue - prev.revenue if chained and prev else p.revenue,
            "net_profit": p.net_profit - prev.net_profit if chained and prev else p.net_profit,
            "deduct_net_profit": p.deduct_net_profit - prev.deduct_net_profit if chained and prev else p.deduct_net_profit,
            "cost": p.operate_cost - prev.operate_cost if chained and prev else p.operate_cost,
            "ocf": p.operating_cash_flow - prev.operating_cash_flow if chained and prev else p.operating_cash_flow,
            "sales_cash": p.sales_cash - prev.sales_cash if chained and prev else p.sales_cash,
        })

    # Build lookup by year+month for YoY
    by_slot: Dict[str, Dict[str, Any]] = {}
    for s in singles:
        key = s["date"][:4] + s["date"][5:7]
        by_slot[key] = s

    result: List[QuarterPoint] = []
    for s in singles[-limit:]:
        month = s["date"][5:7]
        label = s["date"][2:4] + "Q" + str(_quarter_of(month))
        last_year_key = str(int(s["date"][:4]) - 1) + month
        last_year = by_slot.get(last_year_key)

        rev = s["revenue"]
        np_ = s["net_profit"]
        deduct = s["deduct_net_profit"]
        cost = s["cost"]

        rev_yoy = None
        if last_year and last_year["revenue"] > 0:
            rev_yoy = (rev / last_year["revenue"] - 1) * 100

        profit_yoy = None
        if last_year and last_year["net_profit"] > 0 and np_ > 0:
            profit_yoy = (np_ / last_year["net_profit"] - 1) * 100

        deduct_yoy = None
        if last_year and last_year["deduct_net_profit"] > 0 and deduct > 0:
            deduct_yoy = (deduct / last_year["deduct_net_profit"] - 1) * 100

        gm = _ratio(rev - cost, rev)
        nm = _ratio(np_, rev)

        result.append(QuarterPoint(
            label=label,
            revenue=rev,
            net_profit=np_,
            deduct_net_profit=deduct,
            revenue_yoy=rev_yoy,
            profit_yoy=profit_yoy,
            deduct_yoy=deduct_yoy,
            gross_margin=gm * 100 if gm is not None else None,
            net_margin=nm * 100 if nm is not None else None,
        ))

    return result


def _ttm_of(periods: List[FinancialPeriod]) -> Optional[FinancialPeriod]:
    """TTM (trailing twelve months) from cumulative periods."""
    if not periods:
        return None
    sorted_p = sorted(periods, key=lambda p: p.date)
    count = min(4, len(sorted_p))

    def ttm(pick):
        singles = []
        for i, p in enumerate(sorted_p):
            prev = sorted_p[i - 1] if i > 0 else None
            if _consecutive_quarter(prev, p) and prev:
                singles.append(pick(p) - pick(prev))
            else:
                singles.append(pick(p))
        return sum(singles[-count:])

    return FinancialPeriod(
        date=sorted_p[-1].date,
        revenue=ttm(lambda p: p.revenue),
        operate_cost=ttm(lambda p: p.operate_cost),
        net_profit=ttm(lambda p: p.net_profit),
        deduct_net_profit=ttm(lambda p: p.deduct_net_profit),
        operating_cash_flow=ttm(lambda p: p.operating_cash_flow),
        sales_cash=ttm(lambda p: p.sales_cash),
        capex=ttm(lambda p: p.capex),
    )


def _main_business_of(items: List[MainBusinessItem]) -> List[MainBusinessItem]:
    """Latest period's business breakdown, preferring product > industry > region."""
    if not items:
        return []
    latest = max(i.date for i in items)
    current = [i for i in items if i.date == latest]
    product = [i for i in current if i.type == 2]
    if product:
        picked = product
    else:
        industry = [i for i in current if i.type == 1]
        if industry:
            picked = industry
        else:
            picked = [i for i in current if i.type == 3] or current
    return sorted(picked, key=lambda i: i.income, reverse=True)


def _growth_of(periods: List[FinancialPeriod], pick) -> Optional[float]:
    """YoY growth of latest period vs same period last year."""
    sorted_p = sorted(periods, key=lambda p: p.date)
    last = sorted_p[-1] if sorted_p else None
    if last is None:
        return None
    prev = None
    for p in reversed(sorted_p):
        if p.date[:4] == str(int(last.date[:4]) - 1) and p.date[5:7] == last.date[5:7]:
            prev = p
            break
    if prev is None:
        return None
    base = pick(prev)
    if base <= 0:
        return None
    return (pick(last) / base - 1) * 100


# ── Scoring ──────────────────────────────────────────────────────────────────

def _score(
    quarters: List[QuarterPoint],
    deduct_ratio: Optional[float],
    ocf_to_profit: Optional[float],
    cash_to_revenue: Optional[float],
    net_cash_ratio: Optional[float],
    balance: Optional[BalanceSheetSnapshot],
) -> int:
    score = 0

    # 1. Deduct ratio (profit quality)
    score += (
        20 if deduct_ratio is not None and deduct_ratio >= 0.95 else
        14 if deduct_ratio is not None and deduct_ratio >= 0.85 else
        8 if deduct_ratio is not None and deduct_ratio >= 0.70 else
        0 if deduct_ratio is not None else
        10
    )

    # 2. Cash quality
    ocf = ocf_to_profit
    cash = cash_to_revenue
    if ocf is None or cash is None:
        score += 8
    elif ocf >= 1.0 and cash >= 1.0:
        score += 20
    elif ocf >= 0.8:
        score += 14
    elif ocf >= 0.5:
        score += 8
    else:
        score += 0

    # 3. Asset safety (net cash ratio)
    score += (
        20 if net_cash_ratio is not None and net_cash_ratio >= 0.20 else
        14 if net_cash_ratio is not None and net_cash_ratio >= 0.0 else
        8 if net_cash_ratio is not None and net_cash_ratio >= -0.50 else
        0 if net_cash_ratio is not None else
        10
    )

    # 4. Growth
    rev_yoy = [q.revenue_yoy for q in quarters if q.revenue_yoy is not None][-_MIN_YOY_POINTS:]
    profit_yoy = [q.profit_yoy for q in quarters if q.profit_yoy is not None][-_MIN_YOY_POINTS:]
    if not rev_yoy or not profit_yoy:
        score += 10
    else:
        rev_med = _median(rev_yoy) or 0
        profit_med = _median(profit_yoy) or 0
        if rev_med >= 15 and profit_med >= 15:
            score += 20
        elif rev_med >= 5 and profit_med >= 5:
            score += 14
        elif rev_med >= 0 and profit_med >= 0:
            score += 8
        else:
            score += 0

    # 5. Asset cleanliness
    if balance is None or balance.parent_equity <= 0:
        score += 10
    else:
        gw_ratio = balance.goodwill / balance.parent_equity
        or_ratio = (balance.other_receivable / balance.total_assets) if balance.total_assets > 0 else 0.0
        if gw_ratio < 0.05 and or_ratio < 0.05:
            score += 20
        elif gw_ratio < 0.20 and or_ratio < 0.05:
            score += 14
        elif gw_ratio < 0.30:
            score += 8
        else:
            score += 0

    return score


def _verdict(score: int) -> str:
    if score >= 85:
        return "优秀"
    if score >= 70:
        return "良好"
    if score >= 55:
        return "中性"
    if score >= 40:
        return "偏弱"
    return "较差"


# ── Valuation ────────────────────────────────────────────────────────────────

def _valuation_anchors(
    price: float,
    eps: Optional[float],
    bps: Optional[float],
    pe_history: List[float],
    pb_history: List[float],
    peers: Optional[PeerContext],
    notes: List[str],
) -> List[ValuationAnchor]:
    anchors: List[ValuationAnchor] = []

    if eps is not None and eps <= 0:
        notes.append("TTM 归母净利为负，PE 类估值不适用，只保留 PB 与同业参考")
    elif eps is None:
        notes.append("拿不到总股本或 TTM 净利，算不出每股指标，PE/PB 类估值不适用")
    elif len(pe_history) < _MIN_VALUATION_POINTS:
        notes.append(f"PE 历史不足 {_MIN_VALUATION_POINTS} 个交易日，无法给估值分位")

    # Self PE anchor
    if eps is not None and eps > 0 and len(pe_history) >= _MIN_VALUATION_POINTS:
        low_pe = _percentile(pe_history, 0.30)
        high_pe = _percentile(pe_history, 0.60)
        if low_pe is not None and high_pe is not None and high_pe > 0:
            anchors.append(ValuationAnchor(
                name=f"自身 PE 分位（{len(pe_history)} 个交易日）",
                low=low_pe * eps,
                high=high_pe * eps,
                note=f"历史 PE 的 30%~60% 分位对应 EPS-TTM {_price_fmt(eps)}",
                kind=AnchorKind.SELF_HISTORY,
            ))

    # Self PB anchor
    if bps is not None and bps > 0 and len(pb_history) >= _MIN_VALUATION_POINTS:
        low_pb = _percentile(pb_history, 0.30)
        high_pb = _percentile(pb_history, 0.60)
        if low_pb is not None and high_pb is not None and high_pb > 0:
            anchors.append(ValuationAnchor(
                name=f"自身 PB 分位（{len(pb_history)} 个交易日）",
                low=low_pb * bps,
                high=high_pb * bps,
                note=f"历史 PB 的 30%~60% 分位对应 BPS {_price_fmt(bps)}",
                kind=AnchorKind.SELF_HISTORY,
            ))
    elif bps is not None and bps <= 0:
        notes.append("净资产为负，PB 估值不适用")

    # Peer PE anchor
    if peers and eps is not None and eps > 0 and peers.pe_median and peers.pe_median > 0:
        anchors.append(ValuationAnchor(
            name=f"同业 PE 中位（{peers.sample_size} 只）",
            low=peers.pe_median * eps * 0.85,
            high=peers.pe_median * eps * 1.15,
            note=f"{peers.board_name}板块 PE 中位 {_num(peers.pe_median)} 倍，上下留 15% 区间",
            kind=AnchorKind.PEER,
        ))

    # Peer PB anchor
    if peers and bps is not None and bps > 0 and peers.pb_median and peers.pb_median > 0:
        anchors.append(ValuationAnchor(
            name=f"同业 PB 中位（{peers.sample_size} 只）",
            low=peers.pb_median * bps * 0.85,
            high=peers.pb_median * bps * 1.15,
            note=f"{peers.board_name}板块 PB 中位 {_num(peers.pb_median)} 倍，上下留 15% 区间",
            kind=AnchorKind.PEER,
        ))

    return anchors


def _combine_anchors(anchors: List[ValuationAnchor], notes: List[str]) -> Tuple[Optional[float], Optional[float]]:
    if not anchors:
        return None, None

    self_anchors = [a for a in anchors if a.kind == AnchorKind.SELF_HISTORY]
    used = self_anchors if self_anchors else anchors

    low = _median([a.low for a in used]) or 0
    high = _median([a.high for a in used]) or 0

    center = (low + high) / 2
    peer_centers = [(a.low + a.high) / 2 for a in anchors if a.kind == AnchorKind.PEER]
    peer_center = _median(peer_centers)

    if self_anchors and peer_center is not None and center > 0:
        gap = (peer_center - center) / center
        if abs(gap) >= 0.40:
            notes.append(
                f"同业口径的中枢是 {_price_fmt(peer_center)} 元，与自身历史口径的 {_price_fmt(center)} 元差 {_pct(abs(gap))}；"
                "板块估值与个股自身历史脱节时，合理区间以自身历史为准，同业只作参考"
            )

    if len(used) > 1:
        centers = [(a.low + a.high) / 2 for a in used]
        center_median = _median(centers) or 0
        if center_median > 0:
            spread = (max(centers) - min(centers)) / center_median
            if spread >= 0.40:
                notes.append(f"同口径各方法分歧 {_pct(spread)}，说明盈利波动大或可比性差，合理区间参考价值下降")

    return low, high


# ── Risk scanning ────────────────────────────────────────────────────────────

def _risk_scan(
    periods: List[FinancialPeriod],
    balance: Optional[BalanceSheetSnapshot],
    deduct_ratio: Optional[float],
    ocf_to_profit: Optional[float],
    cash_to_revenue: Optional[float],
    net_cash_ratio: Optional[float],
    roe: Optional[float],
    main_business: List[MainBusinessItem],
) -> List[RiskItem]:
    items: List[RiskItem] = []

    if balance:
        equity = balance.parent_equity
        if equity > 0:
            gw_ratio = balance.goodwill / equity
            if gw_ratio >= 0.30:
                items.append(RiskItem(RiskLevel.ALERT, f"商誉占净资产 {_pct(gw_ratio)}，减值风险高"))
            elif gw_ratio >= 0.10:
                items.append(RiskItem(RiskLevel.WATCH, f"商誉占净资产 {_pct(gw_ratio)}，留意减值"))

        if balance.total_assets > 0:
            or_ratio = balance.other_receivable / balance.total_assets
            if or_ratio >= 0.10:
                items.append(RiskItem(RiskLevel.ALERT, f"其他应收款占总资产 {_pct(or_ratio)}，存在资金占用嫌疑"))
            elif or_ratio >= 0.05:
                items.append(RiskItem(RiskLevel.WATCH, f"其他应收款占总资产 {_pct(or_ratio)}，偏高"))

            cash_ratio = balance.monetary_funds / balance.total_assets
            debt_ratio_val = balance.interest_bearing_debt / balance.total_assets
            if cash_ratio >= 0.20 and debt_ratio_val >= 0.20:
                items.append(RiskItem(RiskLevel.ALERT, "货币资金与有息负债同时很高（存贷双高），需要核实资金真实性"))

            receivables = balance.accounts_receivable + balance.inventory
            if receivables / balance.total_assets >= 0.50:
                items.append(RiskItem(RiskLevel.WATCH, f"应收+存货占总资产 {_pct(receivables / balance.total_assets)}，资产偏重"))

        # Net cash assessment
        if net_cash_ratio is not None:
            if net_cash_ratio >= 0.0:
                text = (
                    f"净现金占净资产 {_pct(net_cash_ratio)}，账面很安全"
                    if net_cash_ratio >= 0.20
                    else "净现金为正，有息负债低于账面货币资金"
                )
                items.append(RiskItem(RiskLevel.GOOD, text))
            elif balance.parent_equity > 0 and balance.interest_bearing_debt / balance.parent_equity >= 1.0:
                items.append(RiskItem(RiskLevel.ALERT, "有息负债超过净资产，偿债压力大"))
            else:
                items.append(RiskItem(RiskLevel.WATCH, f"净负债 {_pct(-net_cash_ratio)} 净资产"))

    # Deduct ratio
    if deduct_ratio is not None:
        if deduct_ratio >= 0.95:
            items.append(RiskItem(RiskLevel.GOOD, f"扣非占归母 {_pct(deduct_ratio)}，利润几乎全部来自主营"))
        elif deduct_ratio >= 0.85:
            pass  # neutral
        elif deduct_ratio >= 0.70:
            items.append(RiskItem(RiskLevel.WATCH, f"扣非只占归母 {_pct(deduct_ratio)}，非经常性损益贡献不小"))
        else:
            items.append(RiskItem(RiskLevel.ALERT, f"扣非仅占归母 {_pct(deduct_ratio)}，利润主要靠非经常性损益"))

    # OCF to profit
    if ocf_to_profit is not None:
        if ocf_to_profit >= 1.0:
            items.append(RiskItem(RiskLevel.GOOD, f"经营现金流是净利润的 {_num(ocf_to_profit)} 倍，利润落袋"))
        elif ocf_to_profit >= 0.8:
            pass
        elif ocf_to_profit >= 0.5:
            items.append(RiskItem(RiskLevel.WATCH, f"经营现金流只有净利润的 {_num(ocf_to_profit)} 倍，回款偏慢"))
        else:
            items.append(RiskItem(RiskLevel.ALERT, f"经营现金流远低于净利润（{_num(ocf_to_profit)} 倍），利润没变成钱"))

    # Cash to revenue
    if cash_to_revenue is not None:
        if cash_to_revenue >= 1.0:
            pass
        elif cash_to_revenue >= 0.85:
            pass  # watch for some industries but keep neutral
        elif cash_to_revenue >= 0.5:
            items.append(RiskItem(RiskLevel.WATCH, f"销售收现比 {_num(cash_to_revenue)}，低于含税口径的 1.0~1.13，留意票据与应收是否变多"))
        else:
            items.append(RiskItem(RiskLevel.ALERT, f"销售收现比只有 {_num(cash_to_revenue)}，收入没收到钱"))

    # ROE
    if roe is not None:
        if roe >= 0.15:
            pass  # good but not adding risk item for it
        elif roe >= 0.08:
            pass
        elif roe >= 0.0:
            items.append(RiskItem(RiskLevel.WATCH, f"ROE 只有 {_pct(roe)}，股东回报偏弱"))
        else:
            items.append(RiskItem(RiskLevel.ALERT, f"ROE 为负（{_pct(roe)}），净资产在亏钱"))

    # Cost growth vs revenue growth
    if len(periods) >= 5:
        rev_growth = _growth_of(periods, lambda p: p.revenue)
        cost_growth = _growth_of(periods, lambda p: p.operate_cost)
        if rev_growth is not None and cost_growth is not None and cost_growth - rev_growth >= 25:
            items.append(RiskItem(
                RiskLevel.WATCH,
                f"营业成本增速比营收快 {_num(cost_growth - rev_growth)} 个百分点，毛利在被压缩",
            ))

    # Business concentration (product type only)
    products = [i for i in _main_business_of(main_business) if i.type == 2]
    if len(products) >= 2:
        top = max(products, key=lambda p: p.ratio)
        if top.ratio >= 0.90 and top.ratio < 0.995:
            items.append(RiskItem(RiskLevel.WATCH, f"「{top.name}」一项就占主营收入 {_pct(top.ratio)}，单一业务依赖度高"))
        bleeding = [p for p in products if (p.gross_margin or 0.0) < 0 and p.ratio >= 0.10]
        if bleeding:
            names = "、".join(p.name for p in bleeding)
            items.append(RiskItem(RiskLevel.WATCH, f"{names} 毛利率为负，在拖累整体利润"))

    return items


def _add_growth_notes(quarters: List[QuarterPoint], notes: List[str]) -> None:
    yoy = [q.profit_yoy for q in quarters if q.profit_yoy is not None][-2:]
    if len(yoy) < 2:
        return
    if all(v < 0 for v in yoy):
        notes.append(
            f"最近两季归母净利同比都是负的（{_signed_pct(yoy[0])}、{_signed_pct(yoy[1])}），"
            "盈利在往下走，历史估值分位看着便宜可能是陷阱"
        )
    elif yoy[-1] < 0:
        notes.append(f"最近一季归母净利同比 {_signed_pct(yoy[-1])}，盈利出现下滑，历史估值分位的参考价值要打折")


def _add_timing_notes(
    pe_percentile: Optional[float],
    quarters: List[QuarterPoint],
    ttm: Optional[FinancialPeriod],
    notes: List[str],
) -> None:
    if pe_percentile is None or ttm is None:
        return
    profit_yoy = [q.profit_yoy for q in quarters if q.profit_yoy is not None][-4:]
    if len(profit_yoy) < _MIN_YOY_POINTS:
        return
    med = _median(profit_yoy) or 0
    if pe_percentile <= 30 and med >= 50:
        notes.append(f"当前 PE 处于历史低位，但净利同比 +{_num(med)}%：低 PE 可能是盈利高点撑出来的，先确认是不是周期股")
    if pe_percentile >= 80:
        notes.append(f"当前 PE 处于历史 {_num(pe_percentile)}% 分位，估值本身不便宜")


def _add_deduct_note(
    price: float,
    ttm: Optional[FinancialPeriod],
    eps: Optional[float],
    shares: Optional[float],
    notes: List[str],
) -> None:
    if ttm is None or eps is None or shares is None or shares <= 0 or eps <= 0:
        return
    dr = _ratio(ttm.deduct_net_profit, ttm.net_profit)
    if dr is None or dr >= 0.80:
        return
    deduct_eps = ttm.deduct_net_profit / shares
    if deduct_eps <= 0:
        notes.append(f"扣非后 TTM 是亏损的（扣非净利 {_yi(ttm.deduct_net_profit)} 亿），现在的利润靠非经常性损益撑着")
        return
    deduct_pe = price / deduct_eps
    reported_pe = price / eps
    notes.append(
        f"按扣非算 EPS-TTM 只有 {_price_fmt(deduct_eps)} 元（归母口径 {_price_fmt(eps)} 元）："
        f"PE 从 {_num(reported_pe)} 倍变成 {_num(deduct_pe)} 倍，扣非口径下更贵"
    )


# ── Main analysis entry point ────────────────────────────────────────────────

class Fundamentals:
    """5-dimension financial quality scoring and valuation."""

    @staticmethod
    def analyze(payload: FundamentalPayload) -> FundamentalResult:
        notes: List[str] = []
        periods = payload.periods
        quarters = _single_quarter_periods(periods, _QUARTERS)
        ttm = _ttm_of(periods)
        balance = payload.balance
        shares = payload.total_shares if payload.total_shares > 0 else None
        price = payload.price

        deduct_ratio = _ratio(ttm.deduct_net_profit, ttm.net_profit) if ttm else None
        gross_margin = (_ratio(ttm.revenue - ttm.operate_cost, ttm.revenue) * 100) if ttm else None
        net_margin = (_ratio(ttm.net_profit, ttm.revenue) * 100) if ttm else None
        ocf_to_profit = _ratio(ttm.operating_cash_flow, ttm.net_profit) if ttm else None
        cash_to_revenue = _ratio(ttm.sales_cash, ttm.revenue) if ttm else None

        net_cash_ratio = None
        if balance and balance.parent_equity > 0:
            net_cash_ratio = balance.net_cash / balance.parent_equity

        roe = None
        if balance and balance.parent_equity > 0 and ttm:
            roe = ttm.net_profit / balance.parent_equity

        # Governance scan
        ref_date = payload.valuations[-1].date if payload.valuations else ""
        governance = scan_announcements(payload.announcements, ref_date)

        # Risk scan
        risks = _risk_scan(
            periods, balance, deduct_ratio, ocf_to_profit, cash_to_revenue,
            net_cash_ratio, roe, payload.main_business,
        )
        # Governance ALERT items go into risk list
        for g in governance:
            if g.level == RiskLevel.ALERT:
                risks.append(RiskItem(RiskLevel.ALERT, f"公告提示：{g.title}（{g.date}）"))

        # 股东结构风险：机构净流出或散户户数明显增加（≥+5%）时提示
        shareholder = payload.shareholder
        if shareholder is not None:
            institution_outflow = (
                shareholder.institutional_net_funds is not None
                and shareholder.institutional_net_funds < 0.0
            )
            holder_dispersion = (
                shareholder.retail_holder_change_pct is not None
                and shareholder.retail_holder_change_pct >= 5.0
            )
            if institution_outflow or holder_dispersion:
                risks.append(RiskItem(RiskLevel.WATCH, f"股东结构风险：{shareholder.conclusion}"))

        score = _score(quarters, deduct_ratio, ocf_to_profit, cash_to_revenue, net_cash_ratio, balance)
        verdict = _verdict(score)

        if len(quarters) < _QUARTERS:
            notes.append(f"可用单季只有 {len(quarters)} 个，同比点不足，成长性仅供参考")

        eps = (ttm.net_profit / shares) if shares and ttm else None
        bps = (balance.parent_equity / shares) if shares and balance else None

        pe_history = [v.pe_ttm for v in payload.valuations if v.pe_ttm is not None]
        pb_history = [v.pb_mrq for v in payload.valuations if v.pb_mrq is not None]
        current_pe = None
        current_pb = None
        for v in reversed(payload.valuations):
            if current_pe is None and v.pe_ttm is not None:
                current_pe = v.pe_ttm
            if current_pb is None and v.pb_mrq is not None:
                current_pb = v.pb_mrq
            if current_pe is not None and current_pb is not None:
                break

        pe_pct = _current_percentile(pe_history, current_pe)
        pb_pct = _current_percentile(pb_history, current_pb)

        anchors = _valuation_anchors(price, eps, bps, pe_history, pb_history, payload.peers, notes)
        fair_low, fair_high = _combine_anchors(anchors, notes)

        _add_timing_notes(pe_pct, quarters, ttm, notes)
        _add_growth_notes(quarters, notes)
        _add_deduct_note(price, ttm, eps, shares, notes)

        if balance and ttm and balance.date != ttm.date:
            notes.append(
                f"资产负债表报告期（{balance.date}）与利润表 TTM 截止期（{ttm.date}）不同期，"
                "ROE、净现金比例是跨期口径"
            )

        return FundamentalResult(
            quarters=quarters,
            deduct_ratio=deduct_ratio,
            gross_margin=gross_margin,
            net_margin=net_margin,
            ocf_to_profit=ocf_to_profit,
            cash_to_revenue=cash_to_revenue,
            net_cash_ratio=net_cash_ratio,
            roe=roe,
            eps=eps,
            ttm_net_profit=ttm.net_profit if ttm else None,
            ttm_deduct_net_profit=ttm.deduct_net_profit if ttm else None,
            bps=bps,
            current_pe=current_pe,
            current_pb=current_pb,
            latest=max(periods, key=lambda p: p.date) if periods else None,
            balance=balance,
            main_business=_main_business_of(payload.main_business),
            governance=governance,
            announcements_scanned=len(payload.announcements),
            announcement_from=min((a.date for a in payload.announcements), default=None),
            announcement_to=max((a.date for a in payload.announcements), default=None),
            peers=payload.peers,
            score=score,
            verdict=verdict,
            risks=risks,
            anchors=anchors,
            fair_low=fair_low,
            fair_high=fair_high,
            pe_percentile=pe_pct,
            pb_percentile=pb_pct,
            notes=notes,
            shareholder=shareholder,
        )


# ──────────────────────────────────────────────────────────────────────────────
# FundamentalsReport — text report generation
# ──────────────────────────────────────────────────────────────────────────────

_INTERNAL_CONTROL_CAVEAT = (
    "内控与合规：审计意见、股权质押、监管处罚这些信息在公告里而不在报表里，"
    "上面这一段是财报上可观测的财务质量代理指标（商誉、其他应收款、存贷双高、扣非比例、现金流含量），"
    "下面一节才是公告扫描结果。"
)

_VALUATION_CAVEAT = (
    "口径：当前估值以自身历史为主，行业估值只作对照；"
    "未来区间是情景参考，不是目标价。"
)

_DISCLAIMER = "⚠️ 本报告基于公开财报与历史估值计算，数据来自东方财富接口；不构成投资建议。"


def _headline(result: FundamentalResult) -> str:
    if result.score >= 85:
        base = "财务质量扎实，利润、现金、资产三块都过得去。"
    elif result.score >= 70:
        base = "财务质量良好，没有明显硬伤。"
    elif result.score >= 55:
        base = "财务质量中性，有短板但不致命，要结合行业位置一起看。"
    elif result.score >= 40:
        base = "财务质量偏弱，短板已经写在报表上了。"
    else:
        base = "财务质量较差，下面几条风险建议逐条核实。"

    severe = next((r for r in result.risks if r.level == RiskLevel.ALERT), None)
    mild = next((r for r in result.risks if r.level == RiskLevel.WATCH), None)
    if severe:
        tail = f"最需要盯的是：{severe.text}。"
    elif mild:
        tail = f"需要留意：{mild.text}。"
    else:
        tail = "暂未发现需要特别警惕的财务信号。"
    shareholder = f" 股东结构：{result.shareholder.conclusion}" if result.shareholder else ""
    return base + tail + shareholder


def _valuation_text(result: FundamentalResult, price: float) -> str:
    low, high = result.fair_low, result.fair_high
    if low is None or high is None or low <= 0 or high <= 0:
        return "合理估值：暂不出区间（可用数据不足，原因见口径提醒）。"
    if price > high:
        position = f"现价 {_money(price)} 元，高出区间上沿 {_pct1((price / high - 1) * 100)}，偏贵"
    elif price < low / 2:
        position = f"现价 {_money(price)} 元，不足区间下沿的一半，偏便宜"
    elif price < low:
        position = f"现价 {_money(price)} 元，低于区间下沿 {_pct1((1 - price / low) * 100)}，偏便宜"
    else:
        position = f"现价 {_money(price)} 元，落在区间内"
    return f"合理估值：{_money(low)} ~ {_money(high)} 元（{position}）"


def _holder_count(value: int) -> str:
    """户数：一万户以上用「万户」，否则直接写户数。"""
    if value >= 10_000:
        return f"{value / 10_000:.2f}万户"
    return f"{value}户"


def _shareholder_flow_text(shareholder: ShareholderAnalysis) -> str:
    funds = shareholder.institutional_net_funds
    if funds is None:
        return "机构资金流向暂缺"
    if funds > 0.0:
        return f"机构资金净流入约 {_yi(funds)} 亿"
    if funds < 0.0:
        return f"机构资金净流出约 {_yi(-funds)} 亿"
    return "机构资金无明显净变动"


def _shareholder_summary(result: FundamentalResult) -> str:
    """快速概览/综合结论用的一行股东结构摘要。"""
    shareholder = result.shareholder
    if shareholder is None:
        return "暂无可比股东数据。"
    holders = _holder_count(shareholder.retail_holder_count) if shareholder.retail_holder_count is not None else "—"
    change = shareholder.retail_holder_change_pct
    change_text = f"（{_signed_pct(change)}）" if change is not None else ""
    return (
        f"散户/中小股东户数（总户数代理）{holders}{change_text}"
        f"｜{_shareholder_flow_text(shareholder)}｜{shareholder.conclusion}"
    )


def _append_shareholder(lines: List[str], result: FundamentalResult) -> None:
    """第六节末尾的股东结构小节（对应 Android FundamentalsReport.appendShareholder）。"""
    shareholder = result.shareholder
    if shareholder is None:
        lines.append("股东结构：暂未取到可比披露数据。")
        return
    holders = _holder_count(shareholder.retail_holder_count) if shareholder.retail_holder_count is not None else "—"
    change = shareholder.retail_holder_change_pct
    lines.append(
        f"股东结构（{shareholder.latest_report_date or '最新披露'}）："
        f"散户/中小股东户数（按公开总股东户数代理）{holders}，"
        f"较上期 {_signed_pct(change) if change is not None else '—'}"
    )
    lines.append(f"{_shareholder_flow_text(shareholder)}（按公开披露机构持仓变动 × 当前价估算）")
    inflow_names = "、".join(c.name for c in shareholder.inflows[:6]) or "—"
    outflow_names = "、".join(c.name for c in shareholder.outflows[:6]) or "—"
    lines.append(f"机构流入：{inflow_names}")
    lines.append(f"机构流出：{outflow_names}")
    lines.append(f"结论：{shareholder.conclusion}")


def _debt_ratio(balance: BalanceSheetSnapshot) -> Optional[float]:
    return balance.total_liabilities / balance.total_assets if balance.total_assets > 0 else None


def _goodwill_ratio(balance: BalanceSheetSnapshot) -> Optional[float]:
    return balance.goodwill / balance.parent_equity if balance.parent_equity > 0 else None


def _asset_ratio(balance: BalanceSheetSnapshot, value: float) -> Optional[float]:
    return value / balance.total_assets if balance.total_assets > 0 else None


def _gross_margin_trend(result: FundamentalResult) -> str:
    margins = [q.gross_margin for q in result.quarters if q.gross_margin is not None]
    if len(margins) < 4:
        return "单季毛利率样本不足，看不出趋势。"
    recent = sum(margins[-2:]) / 2
    before = sum(margins[-4:-2]) / 2
    delta = recent - before
    if delta >= 1.5:
        direction = "毛利率在回升"
    elif delta <= -1.5:
        direction = "毛利率在下滑"
    else:
        direction = "毛利率基本走平"
    return f"单季毛利率近两季均值 {_pct1(recent)}，前两季 {_pct1(before)}，{direction}。"


def _profit_quality_conclusion(result: FundamentalResult) -> str:
    dr = result.deduct_ratio
    if dr is None:
        return "扣非数据缺失，判断不了利润含金量。"
    gap = ""
    if result.ttm_net_profit is not None and result.ttm_deduct_net_profit is not None:
        gap = (
            f"近 12 个月归母 {_yi(result.ttm_net_profit)} 亿、扣非 {_yi(result.ttm_deduct_net_profit)} 亿，"
            f"两者相差 {_yi(abs(result.ttm_net_profit - result.ttm_deduct_net_profit))} 亿。"
        )
    if dr >= 0.95:
        verdict = "利润几乎全部来自主营，含金量高，用归母估值的可信度也高。"
    elif dr >= 0.85:
        verdict = "扣非占比高，一次性损益很少，利润质量没问题。"
    elif dr >= 0.70:
        verdict = "扣非占比尚可，但已有一部分利润不是主营赚的，估值时最好按扣非口径打个折。"
    else:
        verdict = "主营贡献不到七成，含金量偏差；用归母净利算出来的 PE 会比实际便宜，建议按扣非重算。"
    return verdict + gap


def _profitability_conclusion(result: FundamentalResult) -> str:
    margin = ""
    if result.gross_margin is not None and result.net_margin is not None:
        nm = result.net_margin
        if nm >= 20:
            mw = "净利率较高，产品有议价能力。"
        elif nm >= 8:
            mw = "净利率属制造业常见水平，靠规模或周转赚钱。"
        elif nm >= 0:
            mw = "净利率偏薄，成本或价格稍有波动就会吃掉利润。"
        else:
            mw = "净利率为负，当期主营是亏的。"
        margin = f"毛利率 {_pct1(result.gross_margin)}、净利率 {_pct1(result.net_margin)}，{mw}"

    roe = result.roe
    if roe is None:
        roe_word = "ROE 取不到，判断不了股东回报水平。"
    elif roe >= 0.15:
        roe_word = f"ROE {_pct1(roe * 100)}，股东回报优秀。"
    elif roe >= 0.08:
        roe_word = f"ROE {_pct1(roe * 100)}，股东回报中规中矩。"
    elif roe >= 0.0:
        roe_word = f"ROE 只有 {_pct1(roe * 100)}，股东回报偏弱。"
    else:
        roe_word = f"ROE 为负（{_pct1(roe * 100)}），净资产在亏钱。"

    return margin + roe_word + _gross_margin_trend(result)


def _solvency_conclusion(result: FundamentalResult) -> str:
    ocf = result.ocf_to_profit
    if ocf is None:
        cash = "经营现金流/归母净利取不到，这一项跳过。"
    elif ocf >= 1.0:
        cash = f"经营现金流是归母净利的 {_num_or_dash(ocf)} 倍，利润落袋，不靠赊账撑收入。"
    elif ocf >= 0.8:
        cash = f"经营现金流是归母净利的 {_num_or_dash(ocf)} 倍，基本跟得上利润。"
    elif ocf >= 0.5:
        cash = f"经营现金流只有归母净利的 {_num_or_dash(ocf)} 倍，回款偏慢，要盯应收账款。"
    else:
        cash = f"经营现金流远低于归母净利（{_num_or_dash(ocf)} 倍），利润还没变成钱，这是最该盯的一条。"

    ratio = result.net_cash_ratio
    if ratio is None:
        net_cash = "净现金取不到，偿债能力判断不了。"
    elif ratio >= 0.20:
        net_cash = f"净现金占净资产 {_pct1(ratio * 100)}，账上很宽裕。"
    elif ratio >= 0.0:
        net_cash = f"净现金占净资产 {_pct1(ratio * 100)}，现金略多于有息负债，安全。"
    elif ratio >= -0.50:
        net_cash = f"净负债占净资产 {_pct1(-ratio * 100)}，有杠杆但还扛得住。"
    else:
        net_cash = f"净负债已占净资产 {_pct1(-ratio * 100)}，偿债压力不小。"

    debt = _debt_ratio(result.balance) if result.balance else None
    debt_text = f"资产负债率 {debt * 100:.1f}%，" if debt is not None else ""

    return debt_text + cash + net_cash


def _asset_quality_conclusion(result: FundamentalResult) -> str:
    balance = result.balance
    if balance is None:
        return "资产负债表没取到，这一节无法下结论。"
    parts: List[str] = []
    gw = _goodwill_ratio(balance)
    if gw is not None:
        if gw >= 0.30:
            parts.append(f"商誉占净资产 {_pct1(gw * 100)}，一旦减值就直接吃掉利润，要盯并购标的的业绩承诺。")
        elif gw >= 0.10:
            parts.append(f"商誉占净资产 {_pct1(gw * 100)}，有减值敞口但还不算突出。")
        else:
            parts.append(f"商誉占净资产 {_pct1(gw * 100)}，可以忽略。")

    or_val = _asset_ratio(balance, balance.other_receivable)
    if or_val is not None:
        if or_val >= 0.10:
            parts.append(f"其他应收款占总资产 {_pct1(or_val * 100)}，规模异常，通常是资金被关联方占用。")
        elif or_val >= 0.05:
            parts.append(f"其他应收款占总资产 {_pct1(or_val * 100)}，占比偏高，值得翻一下明细。")
        else:
            parts.append(f"其他应收款占总资产 {_pct1(or_val * 100)}，正常。")

    ar_inv = _asset_ratio(balance, balance.accounts_receivable + balance.inventory)
    if ar_inv is not None:
        if ar_inv >= 0.50:
            parts.append(f"应收加存货占总资产 {_pct1(ar_inv * 100)}，资产很重，跌价或收不回来都要计提。")
        elif ar_inv >= 0.30:
            parts.append(f"应收加存货占总资产 {_pct1(ar_inv * 100)}，偏重，但属经营常态。")
        else:
            parts.append(f"应收加存货占总资产 {_pct1(ar_inv * 100)}，占比不高。")

    return "".join(parts) if parts else "报表里能用的代理指标都取不到，这一节只能看后面的公告扫描。"


def _governance_conclusion(result: FundamentalResult) -> str:
    """公告扫描的结论：正文只用于内部判断，报告展示对股东影响的结论。"""
    if result.announcements_scanned == 0:
        return "公告没取到，治理风险判断不了——是「没看到」，不是「没问题」。"
    if not result.governance:
        return (
            f"已扫描 {result.announcements_scanned} 条公告，暂未识别出需要重点提示的事项；"
            "正文获取或识别不完整时，仍需以公告原文为准。"
        )
    alerts = sum(1 for g in result.governance if g.level == RiskLevel.ALERT)
    watches = sum(1 for g in result.governance if g.level == RiskLevel.WATCH)
    good = sum(1 for g in result.governance if g.level == RiskLevel.GOOD)
    if alerts > 0 and good > 0:
        direction = "利好与风险并存，风险事项优先核实"
    elif alerts > 0:
        direction = "整体偏利空，风险事项优先核实"
    elif watches > 0 and good > 0:
        direction = "利好与风险并存，继续跟踪事项进展"
    elif watches > 0:
        direction = "以风险提示为主，暂不能视为利空已经落地"
    elif good > 0:
        direction = "以正面事项为主，但仍需关注执行情况"
    else:
        direction = "暂未形成明确方向判断"
    assessments = [g.assessment for g in result.governance if g.assessment][:4]
    if assessments:
        # 每条评估自带句号，拼接时先摘掉，避免出现「。；」和段尾双句号
        detail = "；".join(a.rstrip("。") for a in assessments)
    else:
        detail = "重点公告正文暂未取到，建议打开公告原文核对"
    return (
        f"共扫描 {result.announcements_scanned} 条公告，整理出 {len(result.governance)} 类重点事项"
        f"（⛔ {alerts}、⚠️ {watches}、✅ {good}）；{direction}。{detail}。"
    )


def _main_business_conclusion(result: FundamentalResult) -> str:
    products = [p for p in result.main_business if p.ratio >= 0.01]
    top = products[0] if products else None
    if top is None:
        return "主营构成没取到，看不出业务结构，也就判断不了它是靠什么赚钱的。"
    margin = _pct1(top.gross_margin * 100) if top.gross_margin is not None else "—"
    head = f"最大一块是「{top.name}」，占收入 {_pct1(top.ratio * 100)}、毛利率 {margin}；"
    if top.ratio >= 0.70:
        concentration = "收入高度依赖单一业务，这块业务的景气度会直接决定业绩。"
    elif top.ratio >= 0.40:
        concentration = "收入以这一块为主、其余为补充，属于有主次的业务结构。"
    else:
        concentration = "收入比较分散，没有绝对主力，抗单一业务波动，但也不容易有超额弹性。"
    losing = [p for p in products if (p.gross_margin or 0.0) < 0]
    losing_text = ""
    if losing:
        losing_text = f"另外「{losing[0].name}」等 {len(losing)} 项毛利率为负，是亏着做的业务，要确认是不是战略投入。"
    return head + concentration + losing_text


def _peer_conclusion(result: FundamentalResult) -> str:
    peers = result.peers
    if peers is None:
        return "没取到同板块数据，行业地位只能自己横向找可比公司。"
    if peers.sample_size == 0:
        return "同板块估值样本为空，这一节无法对比。"
    rank = peers.market_cap_rank
    if rank is not None and peers.total > 0:
        pct = rank / peers.total * 100
        if pct <= 10:
            rank_text = f"市值排板块第 {rank}/{peers.total}，属龙头量级，估值通常带一点龙头溢价。"
        elif pct <= 30:
            rank_text = f"市值排板块第 {rank}/{peers.total}，在头部梯队，规模上不吃亏。"
        else:
            rank_text = f"市值排板块第 {rank}/{peers.total}，在板块后半段，注意流动性和关注度都偏低。"
    else:
        rank_text = "市值排名暂缺。"

    pe = result.current_pe
    pm = peers.pe_median
    if pe is None or pe <= 0:
        pe_word = "自身 PE 不适用（亏损），没法按 PE 和同业比。"
    elif pm is None:
        pe_word = "板块 PE 中位缺失，PE 没法对比。"
    elif pe > pm * 1.2:
        pe_word = f"自身 PE {pe:.2f} 倍比板块中位 {pm:.2f} 倍贵 {(pe / pm - 1) * 100:.0f}%，需要更高的成长性来支撑。"
    elif pe < pm * 0.8:
        pe_word = f"自身 PE {pe:.2f} 倍比板块中位 {pm:.2f} 倍便宜 {(1 - pe / pm) * 100:.0f}%，也可能是市场对它的成长性打折。"
    else:
        pe_word = f"自身 PE {pe:.2f} 倍与板块中位 {pm:.2f} 倍接近，估值上没有明显的溢价或折价。"
    return rank_text + pe_word


def _quarter_conclusion(result: FundamentalResult) -> str:
    rev_yoy = [q.revenue_yoy for q in result.quarters if q.revenue_yoy is not None][-4:]
    profit_yoy = [q.profit_yoy for q in result.quarters if q.profit_yoy is not None][-4:]
    if len(rev_yoy) < 2 or len(profit_yoy) < 2:
        return "可比单季不足，趋势暂不下结论。"

    def _yoy_dir(values: List[float], subject: str) -> str:
        up = sum(1 for v in values if v > 0)
        if up == len(values):
            return f"近 {len(values)} 个单季{subject}全部同比正增长，"
        if up == 0:
            return f"近 {len(values)} 个单季{subject}全部同比下滑，"
        if up * 2 > len(values):
            return f"近 {len(values)} 个单季里 {up} 季{subject}同比正增长，"
        return f"近 {len(values)} 个单季里只有 {up} 季{subject}同比正增长，"

    rev_word = _yoy_dir(rev_yoy, "营收")
    profit_word = _yoy_dir(profit_yoy, "利润")

    if all(v <= 0 for v in profit_yoy) and any(v > 0 for v in rev_yoy):
        nuance = "增收不增利，要查成本和费用。"
    else:
        gap = (_median(profit_yoy) or 0) - (_median(rev_yoy) or 0)
        if gap > 3:
            nuance = "利润增速快于收入。"
        elif gap < -3:
            nuance = "利润增速慢于收入。"
        else:
            nuance = "利润和收入的增速基本同步。"

    return rev_word + profit_word + nuance


def _position_word(price: float, low: float, high: float) -> str:
    if price > high:
        return f"高出区间上沿 {_pct1((price / high - 1) * 100)}"
    if price < low / 2:
        return "不足区间下沿的一半"
    if price < low:
        return f"低于区间下沿 {_pct1((1 - price / low) * 100)}"
    if price >= (low + high) / 2:
        return "落在区间上半段"
    return "落在区间下半段"


def _one_line(result: FundamentalResult, price: float, low: Optional[float], high: Optional[float]) -> str:
    if result.score >= 70:
        quality = "财务质量没问题"
    elif result.score >= 55:
        quality = "财务质量中性"
    elif result.score >= 40:
        quality = "财务质量偏弱"
    else:
        quality = "财务质量较差"

    if low is None or high is None or low <= 0 or high <= 0:
        valuation = "估值区间暂缺，先看盈利质量"
    elif price > high:
        valuation = "估值已经不便宜，除非盈利再上台阶"
    elif price >= (low + high) / 2:
        valuation = "估值在区间上半段，安全边际有限"
    elif price >= low:
        valuation = "估值在区间下半段，不算贵"
    else:
        valuation = "估值低于区间下沿，但要先确认盈利下滑不是结构性的"

    return f"{quality}，{valuation}；动手前先看最需要盯的那条风险有没有改善。"


def _relative_to_peer(current: Optional[float], median: Optional[float], metric: str) -> Optional[str]:
    """单个指标相对同业中位的位置；样本不足时返回 None，不硬比。"""
    if current is None or median is None or current <= 0 or median <= 0:
        return None
    difference = (current / median - 1) * 100
    if difference <= -5:
        return (f"{metric} {_num_or_dash(current)} 倍，低于行业中位 {_num_or_dash(median)} 倍"
                f"（折价 {_pct1(-difference)}）")
    if difference >= 5:
        return (f"{metric} {_num_or_dash(current)} 倍，高于行业中位 {_num_or_dash(median)} 倍"
                f"（溢价 {_pct1(difference)}）")
    return f"{metric} {_num_or_dash(current)} 倍，接近行业中位 {_num_or_dash(median)} 倍"


def _self_valuation_headline(result: FundamentalResult) -> str:
    """自身历史分位的档位判断（不含明细）。"""
    pe_pct = result.pe_percentile
    pb_pct = result.pb_percentile
    if pe_pct is not None and pe_pct >= 70:
        return "相对自身历史偏贵"
    if pb_pct is not None and pb_pct >= 70:
        return "相对自身历史偏贵"
    if pe_pct is not None and pe_pct <= 30 and (pb_pct is None or pb_pct <= 30):
        return "相对自身历史偏低"
    if pb_pct is not None and pb_pct <= 30:
        return "相对自身历史偏低"
    if pe_pct is not None or pb_pct is not None:
        return "相对自身历史居中"
    return "相对自身历史暂无法判断"


def _self_valuation_position(result: FundamentalResult) -> str:
    """自身历史分位的明细 + 档位。"""
    metrics: List[str] = []
    if result.pe_percentile is not None:
        metrics.append(f"PE {_pct1(result.pe_percentile)} 分位")
    if result.pb_percentile is not None:
        metrics.append(f"PB {_pct1(result.pb_percentile)} 分位")
    pe_pct = result.pe_percentile
    pb_pct = result.pb_percentile
    if pe_pct is not None and pe_pct >= 70:
        position = "偏贵"
    elif pb_pct is not None and pb_pct >= 70:
        position = "偏贵"
    elif pe_pct is not None and pe_pct <= 30 and (pb_pct is None or pb_pct <= 30):
        position = "偏低"
    elif pb_pct is not None and pb_pct <= 30:
        position = "偏低"
    elif metrics:
        position = "居中"
    else:
        position = "暂无法判断"
    detail = "｜".join(metrics) if metrics else "无有效 PE/PB 分位"
    return f"相对自身历史：{detail}，{position}"


def _peer_valuation_position(result: FundamentalResult) -> str:
    """相对行业可比的明细；同业缺数据时如实说，不硬比。"""
    peers = result.peers
    if peers is None:
        return "相对行业可比：暂无数据"
    metrics = [
        p for p in (
            _relative_to_peer(result.current_pe, peers.pe_median, "PE"),
            _relative_to_peer(result.current_pb, peers.pb_median, "PB"),
        ) if p
    ]
    if not metrics:
        return "相对行业可比：暂无有效 PE/PB 对比"
    return f"相对行业可比：{'｜'.join(metrics)}"


def _peer_valuation_headline(result: FundamentalResult) -> str:
    """相对行业可比的档位判断（不含明细）。"""
    peers = result.peers
    if peers is None:
        return "行业可比数据不足"
    pe = _relative_to_peer(result.current_pe, peers.pe_median, "PE")
    pb = _relative_to_peer(result.current_pb, peers.pb_median, "PB")
    if (pe and "低于" in pe) or (pb and "低于" in pb):
        return "相对行业可比有折价"
    if (pe and "高于" in pe) or (pb and "高于" in pb):
        return "相对行业可比有溢价"
    if pe is not None or pb is not None:
        return "相对行业可比接近"
    return "行业可比数据不足"


def _next_quarter_outlook(result: FundamentalResult) -> str:
    """下季展望：近几季净利同比方向 + 现金流、扣非质量。"""
    growth = [q.profit_yoy for q in result.quarters if q.profit_yoy is not None][-4:]
    if not growth:
        return "盈利同比数据不足，下一财报期暂不外推。"
    average = sum(growth) / len(growth)
    majority = (len(growth) + 1) // 2
    positive = sum(1 for v in growth if v >= 0)
    negative = sum(1 for v in growth if v < 0)
    if positive >= majority and average >= 8:
        direction = "利润趋势偏支持"
    elif negative >= majority and average <= -8:
        direction = "利润趋势偏承压"
    else:
        direction = "利润趋势基本稳定"
    if result.ocf_to_profit is not None:
        cash = f"经营现金流/归母净利 {_num_or_dash(result.ocf_to_profit)} 倍"
    else:
        cash = "经营现金流数据不足"
    if result.deduct_ratio is not None:
        quality = f"扣非占归母 {_pct1(result.deduct_ratio * 100)}"
    else:
        quality = "扣非数据不足"
    return f"{direction}（近{len(growth)}季净利同比均值 {_signed_pct(average)}）｜{cash}｜{quality}"


def _next_quarter_direction(result: FundamentalResult) -> str:
    if not [q.profit_yoy for q in result.quarters if q.profit_yoy is not None][-4:]:
        return "下一财报期趋势数据不足"
    outlook = _next_quarter_outlook(result)
    if outlook.startswith("利润趋势偏支持"):
        return "下一财报期利润趋势偏支持"
    if outlook.startswith("利润趋势偏承压"):
        return "下一财报期利润趋势偏承压"
    return "下一财报期利润趋势基本稳定"


@dataclass
class ForwardValuation:
    """情景化前瞻估值区间：中性 × 0.85 / 1.15 得悲观 / 乐观。"""

    bear_low: float
    bear_high: float
    base_low: float
    base_high: float
    bull_low: float
    bull_high: float
    basis: str


def _forward_valuation(result: FundamentalResult) -> Optional[ForwardValuation]:
    """用成长/现金/扣非/负债四个修正系数把当前区间外推成三档情景。

    只是情景参考：系数是方向性的，不是盈利预测，所以结果必须标注「不是目标价」。
    """
    low, high = result.fair_low, result.fair_high
    if low is None or high is None or low <= 0 or high <= 0:
        return None

    growth = [q.profit_yoy for q in result.quarters if q.profit_yoy is not None][-4:]
    average_growth = sum(growth) / len(growth) if growth else 0.0
    growth_factor = min(max(1 + average_growth / 100 * 0.5, 0.80), 1.20)

    if result.ocf_to_profit is None:
        cash_factor = 1.0
    elif result.ocf_to_profit < 0.8:
        cash_factor = 0.94
    elif result.ocf_to_profit >= 1.2:
        cash_factor = 1.04
    else:
        cash_factor = 1.0

    if result.deduct_ratio is None:
        quality_factor = 1.0
    elif result.deduct_ratio < 0.70:
        quality_factor = 0.95
    elif result.deduct_ratio >= 0.90:
        quality_factor = 1.03
    else:
        quality_factor = 1.0

    balance_factor = 0.95 if (result.net_cash_ratio is not None and result.net_cash_ratio < 0) else 1.0
    factor = min(max(growth_factor * cash_factor * quality_factor * balance_factor, 0.65), 1.35)

    base_low = low * factor
    base_high = high * factor
    basis_parts = [
        f"利润趋势修正 {_pct1((growth_factor - 1) * 100)}",
        f"现金流修正 {_pct1((cash_factor - 1) * 100)}",
        f"扣非质量修正 {_pct1((quality_factor - 1) * 100)}",
    ]
    if balance_factor < 1.0:
        basis_parts.append("净负债状态下调")
    return ForwardValuation(
        bear_low=base_low * 0.85,
        bear_high=base_high * 0.85,
        base_low=base_low,
        base_high=base_high,
        bull_low=base_low * 1.15,
        bull_high=base_high * 1.15,
        basis="｜".join(basis_parts),
    )


def _current_valuation_summary(result: FundamentalResult, price: float) -> str:
    low, high = result.fair_low, result.fair_high
    if low is None or high is None or low <= 0 or high <= 0:
        return "现状估值：数据不足，暂不出区间"
    if price > high:
        position = "高于区间上沿"
    elif price < low:
        position = "低于区间下沿"
    else:
        position = "处于区间内"
    return f"现状估值：{_money(low)} ~ {_money(high)} 元｜现价 {_money(price)} 元（{position}）"


def _future_valuation_summary(result: FundamentalResult) -> str:
    forward = _forward_valuation(result)
    if forward is None:
        return "数据不足，暂不外推未来估值。"
    return (f"未来 6~9 个月：中性 {_money(forward.base_low)}~{_money(forward.base_high)} 元"
            f"｜悲观 {_money(forward.bear_low)}~{_money(forward.bear_high)} 元"
            f"｜乐观 {_money(forward.bull_low)}~{_money(forward.bull_high)} 元")


def _valuation_headline(result: FundamentalResult, price: float) -> str:
    """第十节的结论：现状位置 + 同业档位 + 下季方向 + 未来中性区间。"""
    low, high = result.fair_low, result.fair_high
    if low is None or high is None or low <= 0 or high <= 0:
        return f"当前估值数据不足；{_next_quarter_outlook(result)}"
    if price > high:
        position = "当前价高于自身历史区间"
    elif price < low:
        position = "当前价低于自身历史区间"
    else:
        position = "当前价处于自身历史区间内"
    forward = _forward_valuation(result)
    if forward is None:
        future = "未来估值暂缺"
    else:
        future = f"未来中性区间 {_money(forward.base_low)}~{_money(forward.base_high)} 元"
    return (f"{position}；{_peer_valuation_headline(result)}；"
            f"{_next_quarter_direction(result)}；{future}")


def _valuation_comparison_short(result: FundamentalResult) -> str:
    """自身历史 + 行业可比的明细组合；两条口径分开说，不混成一个结论。"""
    return (f"{_self_valuation_position(result)}；{_peer_valuation_position(result)}"
            f"；综合以自身历史估值为主。")


def _valuation_comparison(result: FundamentalResult) -> str:
    return (f"估值对比：{_self_valuation_headline(result)}｜{_peer_valuation_headline(result)}")


def _risk_conclusion(result: FundamentalResult) -> str:
    if not result.risks:
        return "本期没有扫到需要提示的财务信号，这一节是空的。"
    alerts = sum(1 for r in result.risks if r.level == RiskLevel.ALERT)
    watches = sum(1 for r in result.risks if r.level == RiskLevel.WATCH)
    good = sum(1 for r in result.risks if r.level == RiskLevel.GOOD)
    base = f"本期共 {len(result.risks)} 条（⛔ {alerts} 条、⚠️ {watches} 条、✅ {good} 条）。"
    if alerts > 0:
        return base + "有 ⛔ 级信号，这些项在买入前应当先核实清楚。"
    if watches > 0:
        return base + "没有 ⛔ 级信号，但 ⚠️ 项会在行业下行时放大，建议列进跟踪清单。"
    return base + "没有需要警惕的信号。"


class FundamentalsReport:
    """Generate multi-section text report from FundamentalResult."""

    @staticmethod
    def format(
        name: str,
        code: str,
        price: float,
        as_of: str,
        result: FundamentalResult,
        entry: Optional[EntryStructure] = None,
    ) -> str:
        red = ReportMarkup.red
        latest = result.latest
        lines: List[str] = []

        lines.append(f"📊 {name}（{code}）基本面分析")
        lines.append(f"行情：{_money(price)} 元｜最新报告期：{latest.date if latest else '—'}｜生成于 {as_of}")
        lines.append("")

        # Section 1: Quick overview
        lines.append("一、快速概览")
        lines.append(f"财务质量评分：{result.score}/100（{result.verdict}）")
        lines.append(f"结论：{_headline(result)}")
        lines.append(f"股东结构：{_shareholder_summary(result)}")
        lines.append(_valuation_text(result, price))
        lines.append("")

        # Section 2: Effective profit
        lines.append("二、有效利润（归母 vs 扣非）")
        if latest:
            lines.append(
                f"最近报告期（{latest.date}，年初至今累计）：营收 {_yi(latest.revenue)} 亿｜"
                f"归母 {_yi(latest.net_profit)} 亿｜扣非 {_yi(latest.deduct_net_profit)} 亿"
            )
        ttm_np = f"{_yi(result.ttm_net_profit)} 亿" if result.ttm_net_profit is not None else "—"
        ttm_dp = f"{_yi(result.ttm_deduct_net_profit)} 亿" if result.ttm_deduct_net_profit is not None else "—"
        lines.append(f"归母 TTM {ttm_np}｜扣非 TTM {ttm_dp}")
        lines.append(f"扣非占归母：{_pct1(result.deduct_ratio * 100 if result.deduct_ratio is not None else None)}")
        lines.append(f"EPS-TTM {_num_or_dash(result.eps)} 元｜BPS {_num_or_dash(result.bps)} 元")
        lines.append(f"结论：{_profit_quality_conclusion(result)}")
        lines.append("")

        # Section 3: Profitability
        lines.append("三、盈利能力")
        lines.append(f"毛利率 TTM {_pct1(result.gross_margin)}｜净利率 TTM {_pct1(result.net_margin)}")
        lines.append(f"ROE（TTM 归母净利 ÷ 期末归母净资产）：{_pct1(result.roe * 100 if result.roe is not None else None)}")
        lines.append(_gross_margin_trend(result))
        lines.append(f"结论：{_profitability_conclusion(result)}")
        lines.append("")

        # Section 4: Cash & solvency
        lines.append("四、现金与偿债")
        lines.append(f"经营现金流 / 归母净利 TTM：{_num_or_dash(result.ocf_to_profit)} 倍")
        lines.append(f"销售收现比 TTM：{_num_or_dash(result.cash_to_revenue)}（含税口径下 1.0~1.13 属正常）")
        lines.append(f"净现金 / 归母净资产：{_pct1(result.net_cash_ratio * 100 if result.net_cash_ratio is not None else None)}")
        balance = result.balance
        if balance:
            lines.append(
                f"货币资金 {_yi(balance.monetary_funds)} 亿｜有息负债 {_yi(balance.interest_bearing_debt)} 亿｜"
                f"净现金 {_yi(balance.net_cash)} 亿"
            )
            dr = _debt_ratio(balance)
            lines.append(f"资产负债率：{_pct1(dr * 100 if dr is not None else None)}")
        lines.append(f"结论：{_solvency_conclusion(result)}")
        lines.append("")

        # Section 5: Asset quality
        lines.append("五、资产质量与内控代理指标")
        if balance:
            gw = _goodwill_ratio(balance)
            lines.append(f"商誉 {_yi(balance.goodwill)} 亿（占归母净资产 {_pct1(gw * 100 if gw is not None else None)}）")
            orr = _asset_ratio(balance, balance.other_receivable)
            lines.append(f"其他应收款 {_yi(balance.other_receivable)} 亿（占总资产 {_pct1(orr * 100 if orr is not None else None)}）")
            ar_inv = _asset_ratio(balance, balance.accounts_receivable + balance.inventory)
            lines.append(
                f"应收+存货 {_yi(balance.accounts_receivable + balance.inventory)} 亿"
                f"（占总资产 {_pct1(ar_inv * 100 if ar_inv is not None else None)}）"
            )
        else:
            lines.append("资产负债表未取到，资产质量部分暂缺。")
        lines.append(_INTERNAL_CONTROL_CAVEAT)
        lines.append(f"结论：{_asset_quality_conclusion(result)}")
        lines.append("")

        # Section 6: Governance
        lines.append("六、公告与治理提示（窗口 180 天）")
        if result.announcements_scanned == 0:
            lines.append("公告接口未返回数据，这一节暂缺（不是因为没问题）。")
        else:
            covered = ""
            if result.announcement_from and result.announcement_to:
                covered = f"，实际覆盖 {result.announcement_from} ~ {result.announcement_to}"
            lines.append(f"已扫描最近 {result.announcements_scanned} 条公告{covered}。")
            if not result.governance:
                lines.append("这些公告中暂未识别出需要重点提示的事项。")
                lines.append("注意：正文获取或识别不完整时，不能把这段内容当成「没有风险」的证明。")
            else:
                for g in result.governance:
                    repeat = f"（同类共 {g.count} 条，只列最新一条）" if g.count > 1 else ""
                    lines.append(f"{_mark(g.level)}{g.date} {g.title}{repeat}")
        _append_shareholder(lines, result)
        lines.append(f"结论：{_governance_conclusion(result)}")
        lines.append("")

        # Section 7: Main business
        lines.append("七、主营业务构成（最近一期）")
        products = result.main_business
        if not products:
            lines.append("接口未返回主营构成，暂缺。")
        else:
            shown = [p for p in products if p.ratio >= 0.01][:5]
            for item in shown:
                margin = _pct1(item.gross_margin * 100) if item.gross_margin is not None else "—"
                lines.append(f"{item.name}：{_yi(item.income)} 亿（占 {_pct1(item.ratio * 100)}）｜毛利率 {margin}")
            rest = [p for p in products if p.ratio < 0.01]
            if rest:
                lines.append(f"其余 {len(rest)} 项合计占 {_pct1(sum(p.ratio for p in rest) * 100)}（单项均不足 1%，略）")
        lines.append(f"结论：{_main_business_conclusion(result)}")
        lines.append("")

        # Section 8: Peers
        lines.append("八、行业地位与同业估值")
        peers = result.peers
        if peers is None or peers.sample_size == 0:
            lines.append("同业对比：未取到同板块数据。")
        else:
            rank = peers.market_cap_rank
            if rank is not None and peers.total > 0:
                rank_text = (
                    f"市值 {_yi(peers.market_cap or 0.0)} 亿，板块内第 {rank}/{peers.total} 名"
                    f"（前 {_pct1(rank / peers.total * 100)}）"
                )
            else:
                rank_text = f"市值 {_yi(peers.market_cap or 0.0)} 亿，板块内排名暂缺"
            lines.append(f"{peers.board_name}板块共 {peers.total} 只｜{rank_text}")
            lines.append(
                f"板块 PE 中位 {_num_or_dash(peers.pe_median)} 倍（按正 PE 的 {peers.sample_size} 只统计）"
                f"｜自身 PE-TTM {_pe_text(result.current_pe)}"
            )
            lines.append(
                f"板块 PB 中位 {_num_or_dash(peers.pb_median)} 倍｜自身 PB-MRQ {_num_or_dash(result.current_pb)} 倍"
            )
        lines.append(f"结论：{_peer_conclusion(result)}")
        lines.append("")

        # Section 9: Quarterly trends
        lines.append(f"九、单季趋势（最近 {len(result.quarters)} 个单季，金额单位：亿元）")
        if not result.quarters:
            lines.append("可用单季不足，趋势暂缺。")
        else:
            for q in result.quarters:
                ry = f"同比 {_signed_pct(q.revenue_yoy)}" if q.revenue_yoy is not None else "同比 —"
                py = f"同比 {_signed_pct(q.profit_yoy)}" if q.profit_yoy is not None else "同比 —"
                lines.append(
                    f"{q.label}：营收 {_yi(q.revenue)}（{ry}）｜"
                    f"归母 {_yi(q.net_profit)}（{py}）｜"
                    f"毛利率 {_pct1(q.gross_margin)}"
                )
        lines.append(f"结论：{_quarter_conclusion(result)}")
        lines.append("")

        # Section 10: Valuation
        lines.append("十、合理估值")
        lines.append(_current_valuation_summary(result, price))
        lines.append(_valuation_comparison(result))
        lines.append(f"下一财报期：{_next_quarter_direction(result)}")
        lines.append(f"未来预期：{_future_valuation_summary(result)}")
        lines.append(_VALUATION_CAVEAT)
        lines.append(f"结论：{_valuation_headline(result, price)}")
        lines.append("")

        # Section 11: Risk list
        lines.append("十一、风险清单")
        if not result.risks:
            lines.append("未扫描到需要提示的财务信号。")
        else:
            for r in result.risks:
                lines.append(f"{_mark(r.level)}{r.text}")
        lines.append(f"结论：{_risk_conclusion(result)}")

        # Section 12: Notes
        if result.notes:
            lines.append("")
            lines.append("十二、口径提醒")
            for n in result.notes:
                lines.append(f"※ {n}")
            lines.append("结论：以上是数据口径和研究边界，不是公司经营本身的变化；列在上面的项，相关数字要按说明打折看。")

        lines.append("")

        # Section 13: Comprehensive conclusion
        lines.append("十三、综合结论（红色为关键数据）")
        lines.append(
            f"· 财务质量：{red(f'{result.score}/100')}（{result.verdict}）"
            f"｜扣非占归母 {red(_pct1(result.deduct_ratio * 100 if result.deduct_ratio is not None else None))}"
        )
        # 基本面 × 当前技术位置：不再只回答「值不值得研究」，也回答「现在适不适合考虑」
        has_alert_risk = any(r.level == RiskLevel.ALERT for r in result.risks)
        bucket = fundamental_bucket_from_score(result.score, has_alert_risk=has_alert_risk)
        if entry is not None:
            lines.append(f"· 当前技术位置：{red(entry.state_label)}")
            lines.append(f"· 基本面×买点：{combine_fundamental_and_technical(bucket, entry)}")
            if entry.stop_price:
                lines.append(f"· 技术失效位：{red(f'{_money(entry.stop_price)} 元')}（{entry.invalid_rule}）")
        else:
            lines.append(
                f"· 基本面档位：{bucket.value}｜技术位置暂缺（未做技术分析时不出买点判断，"
                "避免只凭基本面说『可以买』）"
            )
        lines.append(
            f"· 现金与偿债：经营现金流/归母净利 {red(_num_or_dash(result.ocf_to_profit))} 倍"
            f"｜ROE {red(_pct1(result.roe * 100 if result.roe is not None else None))}"
            f"｜净现金/净资产 {red(_pct1(result.net_cash_ratio * 100 if result.net_cash_ratio is not None else None))}"
        )
        low, high = result.fair_low, result.fair_high
        if low is not None and high is not None and low > 0 and high > 0:
            lines.append(
                f"· 估值：合理区间 {red(f'{_money(low)} ~ {_money(high)} 元')}"
                f"｜现价 {red(f'{_money(price)} 元')}（{_position_word(price, low, high)}）"
            )
        else:
            lines.append("· 估值：可用数据不足，暂不出区间（原因见上一节）")
        first_assessment = next((g.assessment for g in result.governance if g.assessment), None)
        if first_assessment:
            lines.append(f"· 公告影响：{first_assessment}")
        if result.shareholder is not None:
            lines.append(f"· 股东结构：{red(_shareholder_summary(result))}")
        lines.append(f"· 估值判断：{_valuation_comparison_short(result)}")
        worst = next((r for r in result.risks if r.level == RiskLevel.ALERT), None)
        if worst is None:
            worst = next((r for r in result.risks if r.level == RiskLevel.WATCH), None)
        if worst:
            lines.append(f"· 最该盯的：{_mark(worst.level)}{worst.text}")
        lines.append(f"· 一句话：{_one_line(result, price, low, high)}")
        lines.append("")
        # 口径说明集中在这里说一次。原来这四条「说明：」散在各节，
        # 每看一份报告就念一遍会计口径科普 —— 看第一份有用，看第十份是噪音。
        # 2026-10-12 体检：三只股的报告里这四行一字不差。
        lines.append(
            "口径说明：归母含政府补助、处置资产等一次性损益，扣非剔掉这些才是主营赚的钱；"
            "报告期披露的是年初至今累计值，本报告已按同一自然年逐季差分还原成单季，同比只和去年同季比；"
            "公开数据只披露总股东户数和部分机构持仓，机构进出不等同于全部机构的真实交易；"
            "市值排名反映规模与关注度，不等于竞争力，估值对比同业需结合成长性看。"
        )
        lines.append(_DISCLAIMER)

        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Convenience function
# ──────────────────────────────────────────────────────────────────────────────

def fetch_and_analyze(
    code: str,
    name: str,
    today: str = "",
    entry: Optional[EntryStructure] = None,
) -> Tuple[FundamentalResult, str]:
    """Fetch data, analyze, and generate report in one call.

    Args:
        code: Stock code like "sh600519"
        name: Stock name like "贵州茅台"
        today: Date string for report header, defaults to today
        entry: 可选的入场结构判断结果；给了就在综合结论里补
               「基本面 × 当前技术位置」的买点判断

    Returns:
        (FundamentalResult, report_text)
    """
    if not today:
        today = datetime.now().strftime("%Y-%m-%d")
    client = FundamentalsClient()
    payload = client.fetch(code, name)
    result = Fundamentals.analyze(payload)
    report = FundamentalsReport.format(name, code, payload.price, today, result, entry=entry)
    return result, report
