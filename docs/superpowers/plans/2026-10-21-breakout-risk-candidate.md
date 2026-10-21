# 强势突破候选与动态破位风险 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增加能够识别“突破后不破位”的强势候选，并按价格位置/乖离在MA20与MA5之间动态判断破位风险。

**Architecture:** 新建纯计算策略模块输出强势延续和动态破位状态；选股器产生 `strong` 来源，观察池把日线就绪与完整交易权限拆开；提醒和报告只消费状态，不执行交易。

**Tech Stack:** Python 3.9、dataclasses、Enum、现有 `unittest`。

**Spec:** `docs/superpowers/specs/2026-10-21-breakout-risk-candidate-design.md`

## Global Constraints

- 只做候选识别、风险状态和提醒文案，不包含买卖执行。
- 低位或乖离小于8%使用MA20；高位或乖离不低于8%使用MA5。
- 盘中低于参考线先预警；低于参考线0.5%且5/15分钟不再收回时标记破位确认。
- 高位或高乖离股票不进入确定候选，只进入延伸观察。
- 数据缺失时不做默认放行。
- 使用现有 `unittest`，测试命令统一加 `PYTHONPYCACHEPREFIX=/tmp/codex-pycache`。
- 在当前分支完成，不创建提交或新分支。

---

### Task 1: 动态破位与强势延续纯策略

**Files:**
- Create: `strong_continuation_policy.py`
- Test: `tests/test_strong_continuation_policy.py`

**Interfaces:**
- Consumes: 价格、MA5/MA10/MA20、MA20前值、价格位置、乖离、突破参考位和风险证据。
- Produces: `assess_strong_continuation(StrongContinuationInput) -> StrongContinuationAssessment`。

```python
@dataclass(frozen=True)
class StrongContinuationInput:
    price: float
    ma5: float
    ma10: float
    ma20: float
    ma20_prev: float
    price_location: str
    bias_pct: float
    breakout_reference: Optional[float] = None
    volume_ratio: Optional[float] = None
    lower_pivots_rising: Optional[bool] = None
    support_holds: Optional[bool] = None
    market_ok: Optional[bool] = None
    sector_ok: Optional[bool] = None
    bar_complete: bool = True
    break_confirmed: bool = False
```

- [x] **Step 1: 写失败测试**

覆盖低位跌破MA20、高乖离跌破MA5、盘中刺破未确认、多头突破延续、高乖离延伸观察、数据缺失六种状态。

```python
def test_high_bias_uses_ma5_risk_line(self):
    result = assess_strong_continuation(StrongContinuationInput(
        price=10.8, ma5=11.0, ma10=10.5, ma20=10.0, ma20_prev=9.9,
        price_location="HIGH", bias_pct=12.0,
    ))
    self.assertEqual(result.state, StrongContinuationState.M5_BREAK_RISK)
    self.assertEqual(result.risk_line, RiskLine.MA5)
```

- [x] **Step 2: 运行测试确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_strong_continuation_policy -v`
Expected: FAIL，原因是模块不存在。

- [x] **Step 3: 实现策略模块**

实现 `RiskLine`、`StrongContinuationState`、`StrongContinuationInput`、`StrongContinuationAssessment`。风险优先级为：数据不足、派发风险、假突破、动态破位、延伸观察、强势延续、非强势。

- [x] **Step 4: 运行测试确认通过**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_strong_continuation_policy -v`
Expected: PASS。

### Task 2: 观察池日线就绪与风险降级

**Files:**
- Modify: `observation_pool.py`
- Test: `tests/test_observation_pool.py`

**Interfaces:**
- Consumes: Task 1 的 `assess_strong_continuation()`。
- Produces: 每日复核记录新增 `strong_continuation`、`risk_line`、`risk_price`；`can_enter` 表示日线结构就绪，`trade_permissions` 单独表示完整执行权限。

- [x] **Step 1: 写失败测试**

增加正向 `ready` 测试：完整多头突破在两个完整交易日后进入 `ready`，即使15/30分钟和板块权限缺失。增加风险测试：高乖离跌破MA5后降为观察/淘汰并记录下杀风险。

```python
def test_daily_ready_does_not_require_intraday_permissions(self):
    pool = ObservationPool()
    self.review(pool, "2026-10-13", hits=[_hit("strong")])
    self.review(pool)
    record = pool.records["sh600001"]
    self.assertEqual(record["status"], "ready")
    self.assertTrue(record["can_enter"])
    self.assertFalse(record["trade_permissions"]["allow_normal_buy"])
```

- [x] **Step 2: 运行测试确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_observation_pool -v`
Expected: FAIL。

- [x] **Step 3: 拆分日线就绪与执行权限**

`_assess()` 计算强势状态；`can_enter` 改用日线条件，不再使用 `allow_normal_buy`。保留 `trade_permissions` 作为完整执行门控。动态破位风险优先降级，且不得复制旧的 `allow_normal_buy`。

- [x] **Step 4: 扩展历史长度并验证**

将观察池日线请求改为400根，并运行 `tests.test_observation_pool`、`tests.test_observation_trend_gate`、`tests.test_observation_integration`。

### Task 3: 选股器增加强势候选通道

**Files:**
- Modify: `stock_menubar.py`
- Modify: `observation_pool.py`
- Test: `tests/test_observation_integration.py`

**Interfaces:**
- Consumes: Task 1策略、现有MACD/RSI/KDJ扫描数据。
- Produces: `round_result["strong"]`，命中字段包含 `source="strong"`、`strong_state`、`risk_line`、`risk_price`。

- [x] **Step 1: 写失败测试**

验证 `_observation_hits()` 接收 `strong` 列表后生成 `source="strong"`，并保留强势状态和动态破位字段。

```python
def test_strong_hit_keeps_risk_context(self):
    hits = StockMenuBar._observation_hits(
        strict=[], relaxed=[], five=[], strong=[{
            "code": "sz000001", "name": "强势股", "price": 10.0,
            "strong_state": "STRONG_CONTINUATION",
            "risk_line": "MA20", "risk_price": 9.6,
        }],
    )
    self.assertEqual(hits[0]["source"], "strong")
    self.assertEqual(hits[0]["risk_line"], "MA20")
```

- [x] **Step 2: 运行测试确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_observation_integration -v`
Expected: FAIL。

- [x] **Step 3: 在日线扫描中计算强势状态**

用最近日线计算MA5/MA10/MA20、前高、60日价格位置、乖离和量能，调用纯策略；只有 `STRONG_CONTINUATION` 进入确定强势候选，`EXTENDED_CONTINUATION` 只作为观察来源。

- [x] **Step 4: 接入状态保存与观察池**

保存 `strong` 列表并通过 `_observation_hits()` 进入观察池；运行 `tests.test_observation_integration`。

### Task 4: 风险提醒、报告和文档

**Files:**
- Modify: `observation_alerts.py`
- Modify: `portfolio_daily_report.py`
- Modify: `technical_summary.py`
- Modify: `docs/strategies/统一趋势与高位结构决策.md`
- Modify: `CHANGES_2026-10-21.md`
- Test: `tests/test_observation_alerts.py`、`tests/test_portfolio_daily_report.py`

**Interfaces:**
- Consumes: `strong_continuation` 状态、`risk_line`、`risk_price`。
- Produces: `format_strong_risk(info: Dict) -> str` 风险提醒文案和报告字段，不产生交易数量。

- [x] **Step 1: 写失败测试**

验证M5/M20风险显示动态参考线；假破位显示风险解除；报告同时显示日线就绪和“非交易执行”。

```python
def test_report_shows_dynamic_m5_risk_line(self):
    text = format_strong_risk({
        "state": "M5_BREAK_RISK",
        "risk_line": "MA5",
        "risk_price": 11.0,
        "confirmed": True,
    })
    self.assertIn("跌破五日线风险", text)
    self.assertIn("风险参考 11.00", text)
    self.assertNotIn("自动卖出", text)
```

- [x] **Step 2: 运行测试确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_observation_alerts tests.test_portfolio_daily_report -v`
Expected: FAIL。

- [x] **Step 3: 接入提醒和摘要**

风险状态优先展示；文案使用“风险预警/破位确认/风险解除”，不出现自动交易执行。

- [x] **Step 4: 更新文档并跑全量**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest discover -s tests -v`
Run: `git diff --check`
Expected: 全量测试通过且diff无空白错误。

