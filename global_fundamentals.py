"""港美股基本面与技术面分析。

对应 Android 版 core/GlobalFundamentalsClient.kt + analysis/GlobalFundamentalsReport.kt：
搜索、财务字段解析（Yahoo quoteSummary / Nasdaq 兜底）、六节报告与结论/风险文案逐条对齐。

数据源在此基础上做了 macOS 侧增强（Android 侧仍在优化数据源，见注释）：
- 东财美股/港股财务分析指标（国内直连，营收/毛利率/净利率/ROE/资产负债率）
- 新浪美股/港股日线（国内直连，现价兜底 + 技术面 K 线）
- Yahoo / Nasdaq 保留为补充与兜底（Yahoo 在国内网络可能需要代理）
取数按「东财 → Yahoo → Nasdaq」合并：先取到的字段优先，缺失字段由后源补齐，
全链路失败才报错，缺失字段不用 A 股数据或假值填补。
"""

from __future__ import annotations

import mini_racer_reuse  # noqa: F401  复用 V8 解码上下文，防 akshare 泄漏 Isolate

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

# ──────────────────────────────────────────────────────────────────────────────
# Data Structures（与 Android GlobalSecurity / GlobalFundamentals 一致）
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class GlobalSecurity:
    symbol: str
    name: str
    exchange: str
    currency: str


@dataclass
class GlobalFundamentals:
    security: GlobalSecurity
    price: float
    currency: str
    market_cap: Optional[float]
    revenue: Optional[float]
    gross_margin: Optional[float]
    operating_margin: Optional[float]
    profit_margin: Optional[float]
    roe: Optional[float]
    operating_cash_flow: Optional[float]
    free_cash_flow: Optional[float]
    cash: Optional[float]
    debt: Optional[float]
    pe: Optional[float]
    pb: Optional[float]
    sector: Optional[str]
    industry: Optional[str]
    summary: Optional[str]
    # 数据来源标注与最新报告期（跟随 Android SEC 优化方向新增）
    data_sources: List[str] = field(default_factory=list)
    latest_period: Optional[str] = None


@dataclass
class SecTicker:
    """SEC company_tickers.json 里的一条：ticker → CIK 映射。"""
    cik: str
    name: str


_MERGE_FIELDS = (
    "market_cap", "revenue", "gross_margin", "operating_margin", "profit_margin",
    "roe", "operating_cash_flow", "free_cash_flow", "cash", "debt",
    "pe", "pb", "sector", "industry", "summary",
)


# ──────────────────────────────────────────────────────────────────────────────
# Yahoo / Nasdaq 解析（与 Android GlobalFundamentalsClient 逐条对应）
# ──────────────────────────────────────────────────────────────────────────────


def _normalize_exchange(value: str) -> str:
    key = (value or "").upper()
    if key in ("NMS", "NAS", "NCM"):
        return "NASDAQ"
    if key in ("NYQ", "NYS", "ASE"):
        return "NYSE"
    if key == "HKG":
        return "HKEX"
    return value if value else "海外市场"


def _currency_of(exchange: str) -> str:
    return "HKD" if exchange == "HKEX" else "USD"


def _raw(parent: Optional[Dict[str, Any]], key: str) -> Optional[float]:
    if not isinstance(parent, dict):
        return None
    node = parent.get(key)
    if not isinstance(node, dict):
        return None
    value = node.get("raw")
    if isinstance(value, (int, float)) and value == value:  # 排除 NaN
        return float(value)
    return None


def _parse_display_number(value: Optional[str]) -> Optional[float]:
    if not value or not str(value).strip():
        return None
    normalized = str(value).replace(",", "").replace("$", "").strip()
    try:
        return float(normalized)
    except ValueError:
        return None


def parse_search_results(text: str) -> List[GlobalSecurity]:
    """Yahoo 搜索结果 → GlobalSecurity 列表（只留 EQUITY，交易所/币种标准化）。"""
    try:
        quotes = json.loads(text).get("quotes")
    except Exception:
        return []
    if not isinstance(quotes, list):
        return []
    results: List[GlobalSecurity] = []
    for row in quotes:
        if not isinstance(row, dict):
            continue
        if str(row.get("quoteType", "")).upper() != "EQUITY":
            continue
        symbol = str(row.get("symbol", "")).strip()
        name = str(row.get("longname") or row.get("shortname") or "").strip()
        if not symbol or not name:
            continue
        exchange = _normalize_exchange(str(row.get("exchange", "")))
        currency = str(row.get("currency") or "") or _currency_of(exchange)
        results.append(GlobalSecurity(symbol, name, exchange, currency))
    seen = set()
    unique = []
    for item in results:
        if item.symbol in seen:
            continue
        seen.add(item.symbol)
        unique.append(item)
    return unique


def fallback_search_results(keyword: str) -> List[GlobalSecurity]:
    """搜索接口不可用时的内置对照表（与 Android 版一致）。"""
    query = keyword.strip().lower()
    table = {
        "英伟达": ("NVDA", "NVIDIA Corporation", "NASDAQ", "USD"),
        "nvidia": ("NVDA", "NVIDIA Corporation", "NASDAQ", "USD"),
        "nvda": ("NVDA", "NVIDIA Corporation", "NASDAQ", "USD"),
        "微软": ("MSFT", "Microsoft Corporation", "NASDAQ", "USD"),
        "microsoft": ("MSFT", "Microsoft Corporation", "NASDAQ", "USD"),
        "msft": ("MSFT", "Microsoft Corporation", "NASDAQ", "USD"),
        "苹果": ("AAPL", "Apple Inc.", "NASDAQ", "USD"),
        "apple": ("AAPL", "Apple Inc.", "NASDAQ", "USD"),
        "aapl": ("AAPL", "Apple Inc.", "NASDAQ", "USD"),
        "特斯拉": ("TSLA", "Tesla, Inc.", "NASDAQ", "USD"),
        "tesla": ("TSLA", "Tesla, Inc.", "NASDAQ", "USD"),
        "tsla": ("TSLA", "Tesla, Inc.", "NASDAQ", "USD"),
        "谷歌": ("GOOGL", "Alphabet Inc.", "NASDAQ", "USD"),
        "google": ("GOOGL", "Alphabet Inc.", "NASDAQ", "USD"),
        "alphabet": ("GOOGL", "Alphabet Inc.", "NASDAQ", "USD"),
        "goog": ("GOOGL", "Alphabet Inc.", "NASDAQ", "USD"),
        "googl": ("GOOGL", "Alphabet Inc.", "NASDAQ", "USD"),
        "亚马逊": ("AMZN", "Amazon.com, Inc.", "NASDAQ", "USD"),
        "amazon": ("AMZN", "Amazon.com, Inc.", "NASDAQ", "USD"),
        "amzn": ("AMZN", "Amazon.com, Inc.", "NASDAQ", "USD"),
        "台积电": ("TSM", "Taiwan Semiconductor Manufacturing Company Limited", "NYSE", "USD"),
        "tsm": ("TSM", "Taiwan Semiconductor Manufacturing Company Limited", "NYSE", "USD"),
        "taiwan semiconductor": ("TSM", "Taiwan Semiconductor Manufacturing Company Limited", "NYSE", "USD"),
        # 港股常用标的
        "小米": ("01810", "小米集团－Ｗ", "HKEX", "HKD"),
        "小米集团": ("01810", "小米集团－Ｗ", "HKEX", "HKD"),
        "xiaomi": ("01810", "小米集团－Ｗ", "HKEX", "HKD"),
        "腾讯": ("00700", "腾讯控股", "HKEX", "HKD"),
        "腾讯控股": ("00700", "腾讯控股", "HKEX", "HKD"),
        "阿里巴巴": ("09988", "阿里巴巴－ＳＷ", "HKEX", "HKD"),
        "阿里": ("09988", "阿里巴巴－ＳＷ", "HKEX", "HKD"),
        "美团": ("03690", "美团－Ｗ", "HKEX", "HKD"),
        "比亚迪": ("01211", "比亚迪股份", "HKEX", "HKD"),
        "京东": ("09618", "京东集团－ＳＷ", "HKEX", "HKD"),
        "网易": ("09999", "网易－Ｓ", "HKEX", "HKD"),
        "中芯国际": ("00981", "中芯国际", "HKEX", "HKD"),
    }
    hit = table.get(query)
    return [GlobalSecurity(*hit)] if hit else []


def parse_fundamentals(
    text: str,
    symbol: str,
    name: str,
    security: Optional[GlobalSecurity] = None,
) -> GlobalFundamentals:
    """Yahoo quoteSummary 解析（modules=price,summaryDetail,financialData,assetProfile）。"""
    if security is None:
        security = GlobalSecurity(symbol, name, "", "USD")
    try:
        root = json.loads(text)["quoteSummary"]["result"][0]
    except Exception:
        raise RuntimeError("港美股基本面返回为空")
    price = _raw(root.get("price"), "regularMarketPrice")
    if price is None:
        price = _raw(root.get("price"), "regularMarketPreviousClose")
    if price is None:
        raise RuntimeError(f"没有取到 {name} 的当前价格")
    summary = root.get("summaryDetail") if isinstance(root.get("summaryDetail"), dict) else {}
    financial = root.get("financialData") if isinstance(root.get("financialData"), dict) else {}
    profile = root.get("assetProfile") if isinstance(root.get("assetProfile"), dict) else {}
    currency = security.currency or str(root.get("price", {}).get("currency") or "USD")
    actual = replace(security, symbol=symbol, name=name, currency=currency)

    def _text(node: Dict[str, Any], key: str) -> Optional[str]:
        value = node.get(key)
        return value if isinstance(value, str) and value.strip() else None

    return GlobalFundamentals(
        security=actual,
        price=price,
        currency=currency,
        market_cap=_raw(summary, "marketCap"),
        revenue=_raw(financial, "totalRevenue"),
        gross_margin=_raw(financial, "grossMargins"),
        operating_margin=_raw(financial, "operatingMargins"),
        profit_margin=_raw(financial, "profitMargins"),
        roe=_raw(financial, "returnOnEquity"),
        operating_cash_flow=_raw(financial, "operatingCashflow"),
        free_cash_flow=_raw(financial, "freeCashflow"),
        cash=_raw(financial, "totalCash"),
        debt=_raw(financial, "totalDebt"),
        pe=_raw(summary, "trailingPE"),
        pb=_raw(summary, "priceToBook"),
        sector=_text(profile, "sector"),
        industry=_text(profile, "industry"),
        summary=_text(profile, "longBusinessSummary"),
    )


def parse_nasdaq_fundamentals(
    info_text: str,
    summary_text: str,
    security: GlobalSecurity,
) -> GlobalFundamentals:
    """Nasdaq 兜底解析：只有价格、市值、行业，财务字段留空。"""
    try:
        info = json.loads(info_text)["data"]
    except Exception:
        raise RuntimeError("Nasdaq行情返回为空")
    try:
        summary = json.loads(summary_text)["data"]["summaryData"]
    except Exception:
        raise RuntimeError("Nasdaq摘要返回为空")
    primary = info.get("primaryData") if isinstance(info.get("primaryData"), dict) else {}
    price = _parse_display_number(primary.get("lastSalePrice"))
    if price is None and isinstance(summary.get("PreviousClose"), dict):
        price = _parse_display_number(summary["PreviousClose"].get("value"))
    if price is None:
        raise RuntimeError(f"没有取到 {security.name} 的当前价格")
    company = str(info.get("companyName") or "").removesuffix(" Common Stock") or security.name
    actual = replace(
        security,
        symbol=str(info.get("symbol") or "") or security.symbol,
        name=company,
        exchange=_normalize_exchange(str(info.get("exchange") or "")) or security.exchange,
        currency=security.currency or "USD",
    )

    def _summary_value(key: str) -> Optional[str]:
        node = summary.get(key)
        value = node.get("value") if isinstance(node, dict) else None
        return value if isinstance(value, str) and value.strip() else None

    return GlobalFundamentals(
        security=actual,
        price=price,
        currency=actual.currency,
        market_cap=_parse_display_number(_summary_value("MarketCap")),
        revenue=None,
        gross_margin=None,
        operating_margin=None,
        profit_margin=None,
        roe=None,
        operating_cash_flow=None,
        free_cash_flow=None,
        cash=None,
        debt=None,
        pe=None,
        pb=None,
        sector=_summary_value("Sector"),
        industry=_summary_value("Industry"),
        summary=None,
    )


# ──────────────────────────────────────────────────────────────────────────────
# SEC EDGAR Company Facts（官方 XBRL，美股首选源；对应 Android parseSecTicker/Facts）
# ──────────────────────────────────────────────────────────────────────────────

_SEC_CONCEPTS: Dict[str, Tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
    ),
    "cost": ("CostOfRevenue", "CostOfGoodsAndServicesSold"),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "debt": ("LongTermDebtNoncurrent", "LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"),
    "equity": ("StockholdersEquity",),
    "shares": ("EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"),
}

SEC_SOURCE_NAME = "SEC Company Facts"

# www.sec.gov/files 的 ticker 表在国内常被 403，内置高频股 CIK 作为首选解析
_SEC_TICKER_CIK: Dict[str, str] = {
    "MSFT": "0000789019", "NVDA": "0001045810", "AAPL": "0000320193",
    "TSLA": "0001318605", "GOOGL": "0001652044", "GOOG": "0001652044",
    "AMZN": "0001018724", "META": "0001326801", "TSM": "0001046179",
    "NFLX": "0001065280", "INTC": "0000050863", "AMD": "0000002488",
    "AVGO": "0001730168", "QCOM": "0000804328", "MU": "0000723125",
    "JPM": "0000019617", "V": "0001403161", "MA": "0001141391",
    "WMT": "0000104169", "JNJ": "0000200406", "PG": "0000080424",
    "XOM": "0000034088", "DIS": "0001744489", "NKE": "0000320187",
    "ORCL": "0001341439", "CRM": "0001108524", "ADBE": "0000796343",
    "BABA": "0001577552", "PDD": "0001737806", "JD": "0001549802",
    "BIDU": "0001329099",
}


def _sec_flow_groups(facts: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, float]]:
    """把流量类概念（利润表/现金流量表）按 (start, end) 报告期分组。

    同一 end 常同时挂「单季」与「全年/YTD」两条（start 不同），
    各概念若各取各的最新值会跨期相除（毛利率为负、利润率 400% 就是这么来的），
    所以先按报告期分组、再整期取数。
    """
    groups: Dict[Tuple[str, str], Dict[str, Tuple[float, str]]] = {}
    for key, concepts in _SEC_CONCEPTS.items():
        if key in ("cash", "debt", "equity", "shares"):
            continue  # 存量概念只用 end，单独取
        for concept in concepts:
            node = facts.get(concept)
            if not isinstance(node, dict):
                continue
            for unit_rows in (node.get("units") or {}).values():
                for row in unit_rows or []:
                    if not isinstance(row, dict):
                        continue
                    start, end = row.get("start"), row.get("end")
                    val = row.get("val")
                    if not start or not end or not isinstance(val, (int, float)):
                        continue
                    period = (str(start)[:10], str(end)[:10])
                    filed = str(row.get("filed", ""))
                    bucket = groups.setdefault(period, {})
                    if key not in bucket or bucket[key][1] <= filed:
                        bucket[key] = (float(val), filed)
    return {period: {k: v[0] for k, v in bucket.items()} for period, bucket in groups.items()}


def _sec_pick_flow_period(
    groups: Dict[Tuple[str, str], Dict[str, float]]
) -> Optional[Tuple[str, str]]:
    """选「最新报告期」：end 最新优先，同 end 取覆盖概念多的、再取跨度长的（全年优于单季）。"""
    if not groups:
        return None

    def _days(period: Tuple[str, str]) -> int:
        try:
            return (datetime.fromisoformat(period[1]) - datetime.fromisoformat(period[0])).days
        except ValueError:
            return 0

    def _score(item: Tuple[Tuple[str, str], Dict[str, float]]):
        period, values = item
        return (period[1], len(values), _days(period))

    return max(groups.items(), key=_score)[0]


def _sec_point_value(
    facts: Dict[str, Any], concepts: Tuple[str, ...], target_end: Optional[str]
) -> Optional[float]:
    """存量类概念（现金/债务/股本）：优先取报告期末同日值，否则取最新 end。"""
    entries: List[Tuple[str, float]] = []
    for concept in concepts:
        node = facts.get(concept)
        if not isinstance(node, dict):
            continue
        for unit_rows in (node.get("units") or {}).values():
            for row in unit_rows or []:
                if (
                    isinstance(row, dict)
                    and isinstance(row.get("val"), (int, float))
                    and row.get("end")
                ):
                    entries.append((str(row["end"])[:10], float(row["val"])))
    if not entries:
        return None
    if target_end:
        matched = [val for end, val in entries if end == target_end]
        if matched:
            return matched[-1]
    latest_end = max(end for end, _ in entries)
    return [val for end, val in entries if end == latest_end][-1]


def parse_sec_ticker_results(text: str, symbol: str) -> Optional[SecTicker]:
    """company_tickers.json → 指定 ticker 的 CIK（10 位补零）与公司名。"""
    try:
        data = json.loads(text)
    except Exception:
        return None
    target = symbol.strip().upper()
    for row in data.values() if isinstance(data, dict) else []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker", "")).strip().upper()
        if ticker != target:
            continue
        cik_value = row.get("cik_str")
        if not isinstance(cik_value, int):
            return None
        return SecTicker(cik=f"{cik_value:010d}", name=str(row.get("title", "")).strip())
    return None


def parse_sec_search_cik(text: str, symbol: str) -> Optional[SecTicker]:
    """EDGAR 全文检索（efts.sec.gov）→ 按 '(TICKER)' 匹配申报人取 CIK。

    www.sec.gov 的 ticker 表被拒时的备用解析通道。
    """
    try:
        data = json.loads(text)
    except Exception:
        return None
    target = f"({symbol.strip().upper()})"
    for hit in data.get("hits", {}).get("hits", []) or []:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue
        names = source.get("display_names") or []
        if not any(target in str(name).upper() for name in names):
            continue
        ciks = source.get("ciks") or []
        if not ciks:
            continue
        digits = re.sub(r"\D", "", str(ciks[0]))
        if not digits:
            continue
        name = re.sub(r"\s*\([^)]*\)\s*(\(Filer\))?\s*$", "", str(names[0])).strip()
        return SecTicker(cik=f"{int(digits):010d}", name=name)
    return None


def parse_sec_facts(
    payload: str,
    security: GlobalSecurity,
    price: float,
) -> GlobalFundamentals:
    """SEC companyfacts → GlobalFundamentals（营收/利润率/现金流/市值/PE/PB）。

    流量概念整期取数（同一个 start/end），跨期字段留空而不是混期相除；
    存量概念取报告期末值；市值 = 现价 × 最近披露股本，PE/PB 由市值倒推。
    """
    try:
        root = json.loads(payload)
    except Exception:
        raise RuntimeError("SEC companyfacts 返回为空")
    facts = (root.get("facts") or {}).get("us-gaap") or {}
    if not isinstance(facts, dict) or not facts:
        raise RuntimeError("SEC companyfacts 缺少 us-gaap 数据")

    groups = _sec_flow_groups(facts)
    period = _sec_pick_flow_period(groups)
    flows = groups.get(period, {}) if period else {}
    target_end = period[1] if period else None

    revenue = flows.get("revenue")
    net_income = flows.get("net_income")
    operating_cf = flows.get("operating_cash_flow")
    capex = flows.get("capex")

    cash = _sec_point_value(facts, _SEC_CONCEPTS["cash"], target_end)
    debt = _sec_point_value(facts, _SEC_CONCEPTS["debt"], target_end)
    equity = _sec_point_value(facts, _SEC_CONCEPTS["equity"], target_end)
    shares = _sec_point_value(facts, _SEC_CONCEPTS["shares"], target_end)
    market_cap = price * shares if price > 0 and shares is not None else None

    def _margin(numerator: Optional[float]) -> Optional[float]:
        if numerator is None or revenue is None or revenue == 0:
            return None
        return numerator / revenue

    gross_margin = (
        (revenue - flows["cost"]) / revenue
        if revenue and "cost" in flows and revenue != 0
        else None
    )

    return GlobalFundamentals(
        security=replace(security, currency=security.currency or "USD"),
        price=price,
        currency=security.currency or "USD",
        market_cap=market_cap,
        revenue=revenue,
        gross_margin=gross_margin,
        operating_margin=_margin(flows.get("operating_income")),
        profit_margin=_margin(net_income),
        roe=(net_income / equity) if net_income is not None and equity else None,
        operating_cash_flow=operating_cf,
        free_cash_flow=(
            operating_cf - capex if operating_cf is not None and capex is not None else None
        ),
        cash=cash,
        debt=debt,
        pe=(market_cap / net_income) if market_cap is not None and net_income else None,
        pb=(market_cap / equity) if market_cap is not None and equity else None,
        sector=None,
        industry=None,
        summary=None,
        data_sources=[SEC_SOURCE_NAME],
        latest_period=target_end,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 东财财务指标（国内直连；营收/毛利率/净利率/ROE/资产负债率，百分数→小数）
# ──────────────────────────────────────────────────────────────────────────────


def em_indicator_fields(row: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """东财「财务分析-主要指标」一行 → GlobalFundamentals 字段（百分数转小数）。"""

    def _num(key: str) -> Optional[float]:
        value = row.get(key)
        if isinstance(value, (int, float)) and value == value:
            return float(value)
        return None

    def _ratio(key: str) -> Optional[float]:
        value = _num(key)
        return value / 100.0 if value is not None else None

    return {
        "revenue": _num("OPERATE_INCOME"),
        "gross_margin": _ratio("GROSS_PROFIT_RATIO"),
        "profit_margin": _ratio("NET_PROFIT_RATIO"),
        "roe": _ratio("ROE_AVG"),
    }


def _market_of(security: GlobalSecurity) -> Optional[str]:
    if security.symbol.upper().endswith(".HK") or security.exchange == "HKEX" or _currency_of(security.exchange) == "HKD":
        return "hk"
    if security.exchange in ("NASDAQ", "NYSE", "AMEX") or security.currency == "USD":
        return "us"
    return None


def _plain_symbol(market: str, symbol: str) -> str:
    """把 '0700.HK' / 'NVDA.MX' / '105.MSFT' 归一成接口要的 '00700' / 'MSFT'。"""
    raw = symbol.strip()
    if market == "hk":
        head = raw.split(".")[0]
        digits = re.sub(r"\D", "", head) or head
        return digits.zfill(5)
    # 美股：'105.MSFT' 这类东财带市场前缀的代码取最后一段，'NVDA.MX' 取第一段
    parts = raw.split(".")
    if len(parts) >= 2 and parts[0].isdigit():
        return parts[-1].upper()
    return parts[0].upper()


def _fetch_em_fundamentals(market: str, symbol: str) -> Dict[str, Optional[float]]:
    import akshare as ak

    plain = _plain_symbol(market, symbol)
    if market == "us":
        df = ak.stock_financial_us_analysis_indicator_em(symbol=plain, indicator="年报")
    else:
        df = ak.stock_financial_hk_analysis_indicator_em(symbol=plain, indicator="年度")
    if df is None or len(df) == 0:
        return {}
    row = df.iloc[0].to_dict()
    fields = em_indicator_fields(row)
    return {k: v for k, v in fields.items() if v is not None}


def fetch_daily_bars(market: str, symbol: str) -> "Any":
    """美股/港股日线（新浪，国内直连），用于技术面分析；返回 DataFrame。

    us: stock_us_daily(symbol='MSFT')；hk: stock_hk_daily(symbol='00700')。
    列名中英文皆可（analyze_history 两种都认）。
    """
    import akshare as ak

    plain = _plain_symbol(market, symbol)
    if market == "us":
        return ak.stock_us_daily(symbol=plain, adjust="qfq")
    return ak.stock_hk_daily(symbol=plain, adjust="qfq")


# ──────────────────────────────────────────────────────────────────────────────
# 新浪行情估值字段（国内直连；PE/市值，Yahoo 不可达时的估值来源）
# ──────────────────────────────────────────────────────────────────────────────


def _sina_fields(text: str) -> List[str]:
    match = re.search(r'"(.*)"', text)
    return match.group(1).split(",") if match else []


def parse_sina_us_quote(text: str) -> Dict[str, Optional[float]]:
    """gb_ 美股行情字段：[12]总市值、[13]EPS(TTM)、[14]PE(TTM)。"""
    fields = _sina_fields(text)
    values: Dict[str, Optional[float]] = {}
    if len(fields) > 14:
        values["market_cap"] = _parse_display_number(fields[12])
        values["pe"] = _parse_display_number(fields[14])
    return {k: v for k, v in values.items() if v is not None}


def parse_sina_hk_quote(text: str) -> Dict[str, Optional[float]]:
    """rt_hk 港股行情字段：[13]市盈率(TTM)。"""
    fields = _sina_fields(text)
    if len(fields) > 13:
        pe = _parse_display_number(fields[13])
        return {"pe": pe} if pe is not None else {}
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# 字段合并：先取到的优先，缺失字段由后源补齐
# ──────────────────────────────────────────────────────────────────────────────


def merge_fundamentals(base: GlobalFundamentals, extra: GlobalFundamentals) -> GlobalFundamentals:
    values: Dict[str, Any] = {}
    for field_name in _MERGE_FIELDS:
        base_value = getattr(base, field_name)
        values[field_name] = base_value if base_value is not None else getattr(extra, field_name)
    security = base.security
    if not security.name and extra.security.name:
        security = replace(security, name=extra.security.name)
    price = base.price if base.price and base.price > 0 else extra.price
    currency = base.currency or extra.currency
    sources: List[str] = []
    for source in list(base.data_sources) + list(extra.data_sources):
        if source not in sources:
            sources.append(source)
    return GlobalFundamentals(
        security=security,
        price=price,
        currency=currency,
        data_sources=sources,
        latest_period=base.latest_period or extra.latest_period,
        **values,
    )


def _empty_fundamentals(security: GlobalSecurity, price: float = 0.0) -> GlobalFundamentals:
    return GlobalFundamentals(
        security=security,
        price=price,
        currency=security.currency,
        market_cap=None, revenue=None, gross_margin=None, operating_margin=None,
        profit_margin=None, roe=None, operating_cash_flow=None, free_cash_flow=None,
        cash=None, debt=None, pe=None, pb=None,
        sector=None, industry=None, summary=None,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────────────


class GlobalFundamentalsClient:
    """港美股搜索与取数：新浪现价 + SEC/东财财务优先，Yahoo/Nasdaq 补充兜底。"""

    SEC_USER_AGENT = "stock-monitor-desktop/1.0 (personal research)"
    SEARCH_URL = (
        "https://query1.finance.yahoo.com/v1/finance/search"
        "?q={query}&quotesCount=12&newsCount=0&enableFuzzyQuery=true"
    )
    YAHOO_SUMMARY_URL = (
        "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
        "?modules=price,summaryDetail,financialData,assetProfile"
    )

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    # ── 搜索 ──

    def search(self, keyword: str) -> List[GlobalSecurity]:
        if not keyword.strip():
            return []
        results = self._search_yahoo(keyword)
        if results:
            return results
        results = self._search_sina(keyword)
        return results or fallback_search_results(keyword)

    def _search_yahoo(self, keyword: str) -> List[GlobalSecurity]:
        try:
            text = self._get_text(
                self.SEARCH_URL.format(query=quote(keyword.strip(), "utf-8")),
                referer="https://finance.yahoo.com/",
            )
            return parse_search_results(text)
        except Exception:
            return []

    def _search_sina(self, keyword: str) -> List[GlobalSecurity]:
        """新浪 suggest 兜底搜索（与 A 股 search_stock 同源思路），只留美股/港股。"""
        try:
            text = self._get_text(
                "https://suggest3.sinajs.cn/suggest/type=&key="
                + quote(keyword.strip(), "utf-8"),
                referer="https://finance.sina.com.cn/",
                encoding="gb18030",  # 新浪接口是 GBK 系编码，按 UTF-8 解会乱码
            )
        except Exception:
            return []
        return parse_sina_suggest(text)

    # ── 取数 ──

    def fetch(self, security: GlobalSecurity) -> GlobalFundamentals:
        """取数合并链：新浪日线现价 → SEC（美股）→ 东财财务 → 新浪行情估值 → Yahoo → Nasdaq。

        先取到的字段优先，缺失由后源补齐；每步独立容错，全链无现价才报错。
        """
        merged = _empty_fundamentals(security)
        errors: List[str] = []
        market = _market_of(security)

        # 1) 新浪日线现价（国内直连；同时是技术面 K 线的同源数据）
        if market:
            try:
                bars = fetch_daily_bars(market, security.symbol)
                close_col = next(
                    (c for c in ("close", "收盘", "收盘价") if c in bars.columns), None
                )
                if close_col is not None and len(bars) > 0:
                    merged.price = float(bars[close_col].iloc[-1]) or merged.price
            except Exception as error:
                errors.append(f"新浪日线 {error}")

        # 2) SEC companyfacts（美股官方 XBRL：营收/利润率/现金流/市值/PE/PB）
        if market == "us":
            try:
                merged = merge_fundamentals(
                    merged, self._fetch_sec(security, merged.price)
                )
            except Exception as error:
                errors.append(f"SEC {error}")

        # 3) 东财财务指标（港美股均可，补 SEC 没覆盖的字段）
        if market:
            try:
                fields = _fetch_em_fundamentals(market, security.symbol)
                if fields:
                    for key, value in fields.items():
                        if getattr(merged, key) is None and value is not None:
                            setattr(merged, key, value)
                    if "东方财富财务指标" not in merged.data_sources:
                        merged.data_sources.append("东方财富财务指标")
            except Exception as error:
                errors.append(f"东财财务 {error}")

        # 3.5) 新浪行情估值（PE/市值，国内直连，Yahoo 不可达时的估值来源）
        if market:
            try:
                quote_fields = self._fetch_quote_valuation(market, security.symbol)
                if quote_fields:
                    for key, value in quote_fields.items():
                        if getattr(merged, key) is None and value is not None:
                            setattr(merged, key, value)
                    if "新浪行情" not in merged.data_sources:
                        merged.data_sources.append("新浪行情")
            except Exception as error:
                errors.append(f"新浪行情 {error}")

        # 4) Yahoo quoteSummary（行业/简介/估值兜底）
        try:
            text = self._get_text(
                self.YAHOO_SUMMARY_URL.format(symbol=quote(security.symbol, "utf-8")),
                referer="https://finance.yahoo.com/",
                timeout=10.0,
            )
            yahoo = parse_fundamentals(text, security.symbol, security.name, security)
            yahoo.data_sources.append("Yahoo Finance")
            merged = merge_fundamentals(merged, yahoo)
        except Exception as error:
            errors.append(f"Yahoo {error}")

        # 5) Nasdaq 兜底
        if not merged.price or merged.price <= 0:
            try:
                merged = merge_fundamentals(merged, self._fetch_nasdaq(security))
            except Exception as error:
                errors.append(f"Nasdaq {error}")

        if not merged.price or merged.price <= 0:
            raise RuntimeError(
                f"港美股数据暂不可用：{'；'.join(errors) if errors else '没有取到现价'}"
            )
        return merged

    def _fetch_sec(self, security: GlobalSecurity, price: float) -> GlobalFundamentals:
        """ticker → CIK → companyfacts。SEC 要求带 User-Agent。"""
        ticker = self._resolve_sec_ticker(security.symbol)
        facts_text = self._get_text(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{ticker.cik}.json",
            headers={"User-Agent": self.SEC_USER_AGENT},
            timeout=20.0,
        )
        return parse_sec_facts(facts_text, security, price)

    def _resolve_sec_ticker(self, symbol: str) -> SecTicker:
        """CIK 解析链：内置高频股 → company_tickers.json → efts 全文检索。

        www.sec.gov/files 国内常被 403，data.sec.gov 与 efts.sec.gov 可达，
        所以 ticker→CIK 用内置表优先，官方表次之，全文检索兜底。
        """
        target = symbol.split(".")[0].upper()
        cik = _SEC_TICKER_CIK.get(target)
        if cik:
            return SecTicker(cik=cik, name="")
        try:
            text = self._get_text(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": self.SEC_USER_AGENT},
                timeout=15.0,
            )
            ticker = parse_sec_ticker_results(text, target)
            if ticker:
                return ticker
        except Exception:
            pass
        text = self._get_text(
            f"https://efts.sec.gov/LATEST/search-index?q=%22{target}%22&forms=10-K",
            headers={"User-Agent": self.SEC_USER_AGENT},
            timeout=15.0,
        )
        ticker = parse_sec_search_cik(text, target)
        if ticker is None:
            raise RuntimeError(f"SEC 未收录 {symbol}")
        return ticker

    def _fetch_quote_valuation(self, market: str, symbol: str) -> Dict[str, Optional[float]]:
        """新浪行情的 PE/市值字段（国内直连）。"""
        plain = _plain_symbol(market, symbol)
        if market == "us":
            url = f"https://hq.sinajs.cn/list=gb_{plain.lower()}"
            parser = parse_sina_us_quote
        else:
            code = re.sub(r"\D", "", plain).zfill(5)
            url = f"https://hq.sinajs.cn/list=rt_hk{code}"
            parser = parse_sina_hk_quote
        return parser(self._get_text(
            url, referer="https://finance.sina.com.cn/", encoding="gb18030",
        ))

    def _fetch_nasdaq(self, security: GlobalSecurity) -> GlobalFundamentals:
        plain = security.symbol.split(".")[0]
        encoded = quote(plain, "utf-8")
        info = self._get_text(
            f"https://api.nasdaq.com/api/quote/{encoded}/info?assetclass=stocks",
            referer="https://www.nasdaq.com/",
        )
        summary = self._get_text(
            f"https://api.nasdaq.com/api/quote/{encoded}/summary?assetclass=stocks",
            referer="https://www.nasdaq.com/",
        )
        data = parse_nasdaq_fundamentals(info, summary, security)
        data.data_sources.append("Nasdaq")
        return data

    def fetch_daily_bars(self, security: GlobalSecurity) -> "Any":
        market = _market_of(security)
        if not market:
            raise RuntimeError(f"不支持的市场：{security.exchange}")
        return fetch_daily_bars(market, security.symbol)

    def _get_text(
        self,
        url: str,
        referer: str = "",
        timeout: Optional[float] = None,
        retries: int = 1,
        headers: Optional[Dict[str, str]] = None,
        encoding: str = "utf-8",
    ) -> str:
        request_headers: Dict[str, str] = {}
        if referer:
            request_headers["Referer"] = referer
        if headers:
            request_headers.update(headers)
        last_exc: Optional[Exception] = None
        for _ in range(retries + 1):
            try:
                resp = requests.get(
                    url, headers=request_headers, timeout=timeout or self.timeout
                )
                resp.raise_for_status()
                return resp.content.decode(encoding, errors="replace")
            except Exception as error:
                last_exc = error
        raise RuntimeError(f"请求失败: {last_exc}")


def parse_sina_suggest(text: str) -> List[GlobalSecurity]:
    """新浪 suggest 返回里筛出港股/美股。

    实测字段为「名称,类型,代码,…」，类型 31=港股、41=美股/ADR、11=A股、87=期货；
    代码在第 3 个字段（如 01810 / nvda / xiacy），港美股没有 gb_/hk 前缀，按类型分派。

    排序保持接口相关性顺序；只对「同一公司」（名称去掉 -Ｗ/(ADR) 等后缀后相同）
    做港股正股 > ADR 的调整——搜「小米」时 ADR(xiacy) 常排在正股(01810)前面，
    但不能因此把弱相关的港股（如「博通」错配到「载通」）顶到更相关的美股前面。
    """
    match = re.search(r'"(.*)"', text)
    if not match or not match.group(1).strip():
        return []
    results: List[GlobalSecurity] = []
    for part in match.group(1).split(";"):
        fields = [field.strip() for field in part.split(",")]
        if len(fields) < 3:
            continue
        name, kind, code = fields[0], fields[1], fields[2].lower()
        if not name or not code:
            continue
        if kind == "31":  # 港股
            digits = re.sub(r"\D", "", code) or code
            results.append(GlobalSecurity(digits.zfill(5), name, "HKEX", "HKD"))
        elif kind == "41":  # 美股 / ADR
            results.append(GlobalSecurity(code.upper(), name, "NASDAQ", "USD"))
        # 11=A股、87=期货/商品不归港美股分析管

    def _skeleton(name: str) -> str:
        base = re.sub(r"[（(].*?[）)]", "", name)   # 去掉 (ADR) 之类
        base = re.sub(r"[-—－].*$", "", base)      # 去掉 -Ｗ / -SW / -WR / -S 上市后缀
        return base.strip() or name

    groups: Dict[str, List[GlobalSecurity]] = {}
    group_order: List[str] = []
    for item in results:
        key = _skeleton(item.name)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(item)
    ordered: List[GlobalSecurity] = []
    for key in group_order:
        items = groups[key]
        items.sort(key=lambda it: 0 if it.exchange == "HKEX" else 1)  # 稳定排序，组内正股优先
        ordered.extend(items)
    seen = set()
    unique = []
    for item in ordered:
        if item.symbol in seen:
            continue
        seen.add(item.symbol)
        unique.append(item)
    return unique


# ──────────────────────────────────────────────────────────────────────────────
# Report（与 Android GlobalFundamentalsReport 逐条对应）
# ──────────────────────────────────────────────────────────────────────────────


def _positive(value: Optional[float]) -> bool:
    return value is not None and value > 0.0


def _net_cash(data: GlobalFundamentals) -> bool:
    return data.cash is not None and data.debt is not None and data.cash >= data.debt


def _pct(value: Optional[float]) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"


def _num(value: Optional[float]) -> str:
    return f"{value:.1f} 倍" if value is not None else "—"


def _money(value: float) -> str:
    return f"{value:.2f}"


def money_unit(value: float, currency: str = "") -> str:
    """金额带单位缩放：万亿/亿/万 + 币种，大数字可读（177837000000 → 1778.37 亿美元）。"""
    word = _currency_word(currency) if currency else ""
    abs_value = abs(value)
    if abs_value >= 1e12:
        return f"{value / 1e12:.2f} 万亿{word}"
    if abs_value >= 1e8:
        return f"{value / 1e8:.2f} 亿{word}"
    if abs_value >= 1e4:
        return f"{value / 1e4:.2f} 万{word}"
    return f"{value:.2f} {word}".rstrip()


def _money_or_dash(value: Optional[float], currency: str = "") -> str:
    return money_unit(value, currency) if value is not None else "—"


_CURRENCY_WORDS = {"USD": "美元", "HKD": "港币"}


def _currency_word(currency: str) -> str:
    return _CURRENCY_WORDS.get(currency, currency or "本币")


def valuation_position_text(data: GlobalFundamentals) -> str:
    """只给「现价相对合理区间的位置」这一句结论（不含区间推导过程）。

    综合结论要用这个而不是 valuation_range_text —— 否则整句「价格参考：合理
    区间…（TTM EPS × 20~35 倍 PE 参考带…）」会在报告里出现两遍。
    """
    price, pe = data.price, data.pe
    if not price or price <= 0 or pe is None or pe <= 0:
        return "PE/PB 暂缺，给不出合理价格区间"
    eps = price / pe
    low, high = 20.0 * eps, 35.0 * eps
    word = _currency_word(data.currency)
    if price > high:
        return f"现价 {_money(price)} {word}，高出区间上沿 {(price / high - 1) * 100:.1f}%，偏贵"
    if price < low / 2:
        return f"现价 {_money(price)} {word}，不足区间下沿的一半，偏便宜"
    if price < low:
        return f"现价 {_money(price)} {word}，低于区间下沿 {(1 - price / low) * 100:.1f}%，偏便宜"
    return f"现价 {_money(price)} {word}，落在区间内"


def valuation_range_text(data: GlobalFundamentals) -> str:
    """股价估值：PE 参考带 × TTM EPS 的价格区间 + 现价位置（与 A 股「合理估值」同口径）。

    区间是 20~35 倍 TTM PE 的情景参考（成长股常见带），不是目标价。
    """
    price, pe = data.price, data.pe
    if not price or price <= 0 or pe is None or pe <= 0:
        return "价格参考：PE/PB暂缺，给不出合理价格区间。"
    eps = price / pe
    low, high = 20.0 * eps, 35.0 * eps
    word = _currency_word(data.currency)
    return (
        f"价格参考：合理区间 {_money(low)} ~ {_money(high)} {word}"
        f"（TTM EPS {_money(eps)} × 20~35 倍 PE 参考带；{valuation_position_text(data)}）"
    )


def quality_conclusion(data: GlobalFundamentals) -> str:
    if _positive(data.profit_margin) and _positive(data.free_cash_flow) and _net_cash(data):
        return "盈利能力和现金创造能力较强，资产负债压力较低"
    if _positive(data.profit_margin) and _positive(data.operating_cash_flow):
        return "主营盈利和经营现金流为正，但仍需关注资本开支与负债变化"
    if _positive(data.profit_margin):
        return "当前有盈利，但现金流或资产负债字段不完整，结论需要保守"
    return "公开字段不足或盈利质量偏弱，暂不适合仅凭本报告下结论"


def valuation_conclusion(data: GlobalFundamentals) -> str:
    if data.pe is None and data.pb is None:
        return "PE/PB暂缺，不能给出可靠估值判断"
    if data.pe is not None and data.pe <= 20.0:
        return "当前PE不高，但仍需结合增长速度、周期位置和现金流验证"
    if data.pe is not None and data.pe <= 35.0:
        return "估值处于成长股常见的中高区间，合理性取决于未来利润增速"
    return "估值偏高，股价已经包含较高增长预期，业绩不及预期时估值可能收缩"


def profitability_conclusion(data: GlobalFundamentals) -> str:
    if (
        data.gross_margin is not None and data.gross_margin >= 0.5
        and data.profit_margin is not None and data.profit_margin >= 0.2
    ):
        return "毛利率和净利率较高，具备较强定价或平台能力"
    if _positive(data.profit_margin):
        return "公司目前能够盈利，但利润率水平需要与同行和历史趋势比较"
    return "盈利字段不足或利润率偏弱，不能把收入增长等同于股东回报"


def solvency_conclusion(data: GlobalFundamentals) -> str:
    if _net_cash(data) and _positive(data.free_cash_flow):
        return "现金覆盖债务且自由现金流为正，财务安全边际较好"
    if _positive(data.operating_cash_flow):
        return "经营现金流为正，但现金与债务结构仍需结合完整资产负债表核对"
    return "现金流或债务数据不理想/不完整，需要重点关注偿债和资本开支压力"


def risk_items(data: GlobalFundamentals) -> List[str]:
    items: List[str] = []
    if data.pe is not None and data.pe > 35.0:
        items.append("估值偏高，依赖未来高增长兑现")
    if data.free_cash_flow is not None and data.free_cash_flow < 0.0:
        items.append("自由现金流为负，需核对资本开支和营运资金占用")
    if data.debt is not None and data.cash is not None and data.debt > data.cash:
        items.append("债务高于现金，利率和再融资环境会影响安全边际")
    if not data.summary:
        items.append("公司业务简介暂缺，竞争优势判断不完整")
    return items


def format_report(
    data: GlobalFundamentals,
    as_of: str,
    technical_text: Optional[str] = None,
) -> str:
    """六节报告；technical_text 非空时在「综合结论」前插入技术面小节。"""
    quality = quality_conclusion(data)
    valuation = valuation_conclusion(data)
    risks = risk_items(data)
    risk_line = "；".join(risks) if risks else "暂无可识别的主要风险，仍需结合最新财报核对。"
    lines = [
        f"🌎 {data.security.name}（{data.security.symbol}）港美股基本面分析",
        f"市场：{data.security.exchange}｜币种：{data.currency}｜现价：{money_unit(data.price, data.currency)}｜生成于 {as_of}",
    ]
    if data.data_sources:
        source_line = f"数据来源：{'、'.join(data.data_sources)}"
        if data.latest_period:
            source_line += f"｜最新报告期：{data.latest_period}"
        lines.append(source_line)
    lines += [
        "",
        "一、快速结论",
        f"公司质量：{quality}",
        f"估值判断：{valuation}",
        f"投资者应关注：{risk_line}",
        "",
        "二、盈利能力",
        f"营收：{_money_or_dash(data.revenue, data.currency)}",
        f"毛利率：{_pct(data.gross_margin)}｜营业利润率：{_pct(data.operating_margin)}｜净利率：{_pct(data.profit_margin)}",
        f"ROE：{_pct(data.roe)}",
        f"结论：{profitability_conclusion(data)}",
        "",
        "三、现金流与资产负债",
        f"经营现金流：{_money_or_dash(data.operating_cash_flow, data.currency)}｜自由现金流：{_money_or_dash(data.free_cash_flow, data.currency)}",
        f"现金及等价物：{_money_or_dash(data.cash, data.currency)}｜总债务：{_money_or_dash(data.debt, data.currency)}",
        f"结论：{solvency_conclusion(data)}",
    ]

    # 四、业务与竞争优势：**没数据就说没数据**，不要编一句「需继续核对」充数。
    # 2026-10-12 体检：AAPL 财务数据齐全，业务描述取不到时照样输出
    # 「结论：业务壁垒需要结合…不能只依据公司简介下结论」这种硬编码模板，
    # 而且风险清单里再说一遍「竞争优势判断不完整」，一页说两遍。
    business_missing = not (data.sector or data.industry or data.summary)
    lines += ["", "四、业务与竞争优势"]
    if business_missing:
        lines.append("  本节无数据（行业 / 细分行业 / 公司简介均未取到），不给业务面结论。")
    else:
        lines.append(f"行业：{data.sector or '—'}｜细分行业：{data.industry or '—'}")
        if data.summary:
            lines.append(f"公司简介：{data.summary}")
        else:
            lines.append("公司简介：暂缺（业务壁垒需结合客户集中度、研发投入、产品周期另行核对）")

    lines += [
        "",
        "五、估值",
        f"市值：{_money_or_dash(data.market_cap, data.currency)}｜TTM PE：{_num(data.pe)}｜PB：{_num(data.pb)}",
        # 不再重复「结论：{valuation}」—— 它和一、快速结论一字不差，
        # 2026-10-12 体检时同一句话在报告里出现了 2 次（综合结论里还有第 3 次）。
        valuation_range_text(data),
        "",
        "六、风险提示",
    ]
    if not risks:
        lines.append("当前字段未识别出明确风险，但不代表没有风险。")
    for item in risks:
        lines.append(f"· {item}")
    lines.append("")
    if technical_text:
        lines.append("七、技术面摘要（日线数据源：新浪/东财，复权口径与 A 股模块一致）")
        lines.extend(line for line in technical_text.splitlines())
        lines.append("")
    # 综合结论不复述「公司质量」「估值判断」—— 一、快速结论已经说过，
    # 原来这里第三次照抄同一句话（2026-10-12 体检）。改为说「所以呢」：
    # 价格落在哪、缺了什么字段会让上面的结论偏保守。
    missing = [
        label for label, value in (
            ("行业", data.sector), ("公司简介", data.summary),
            ("营收", data.revenue), ("毛利率", data.gross_margin),
            ("经营现金流", data.operating_cash_flow), ("现金", data.cash),
            ("市值", data.market_cap), ("PE", data.pe),
        ) if not value
    ]
    parts = [valuation_position_text(data)]
    if technical_text:
        parts.append("技术面见上节，技术位与估值区间的重叠处才是可执行的观察点")
    if missing:
        parts.append(f"未取得字段：{'、'.join(missing)}，上面的结论按已取得字段给，偏保守")
    lines.append("综合结论：" + "；".join(parts) + "。")
    lines.append(
        "说明：港股/美股财报口径、币种和披露周期与 A 股不同，本报告只使用已取得的公开字段，"
        "缺失项目不会用 A 股数据补齐；公司简介仅供了解业务范围，业务壁垒仍需结合"
        "客户集中度、研发投入、产品周期和竞争对手变化另行核对。"
    )
    lines.append("⚠️ 本报告仅作公开信息整理和条件式分析，不构成投资建议。")
    return "\n".join(lines)


def format_technical_section(bars: "Any", code: str, name: str) -> str:
    """用项目现有的技术栈（analyze_history + TechnicalSummary）出港美股技术面摘要。"""
    from stock_analysis import analyze_history
    from technical_summary import TechnicalSummary, format_summary

    result = analyze_history(bars, code, name)
    summary = TechnicalSummary.from_result(result)
    return format_summary(summary)


def fetch_and_format(keyword: str, today: str = "") -> Tuple[GlobalFundamentals, str]:
    """搜索 + 取数 + 基本面报告 + 技术面摘要，一键出全文。"""
    client = GlobalFundamentalsClient()
    matches = client.search(keyword)
    if not matches:
        raise RuntimeError(f"没有找到「{keyword}」对应的港股或美股")
    security = matches[0]
    data = client.fetch(security)
    technical_text = None
    try:
        bars = client.fetch_daily_bars(data.security)
        technical_text = format_technical_section(
            bars, data.security.symbol, data.security.name
        )
    except Exception:
        technical_text = "技术面：日线数据暂未取到，本节暂缺。"
    as_of = today or datetime.now().strftime("%Y-%m-%d")
    return data, format_report(data, as_of, technical_text=technical_text)
