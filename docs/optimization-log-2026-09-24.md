# 优化日志：参考 Android 版全面优化 macOS 股票监控程序

**日期**: 2026-09-24
**参考项目**: `/Users/stephen/Documents/mi/privacy/stock-monitor-android`

---

## 一、优化背景

macOS 版股票监控 (`stock_menubar.py`, 4385行) 功能完整但缺少 Android 版中多个成熟的分析模块。本次优化将 Android 版中经过验证的分析逻辑移植到 macOS 版，提升分析深度和代码可维护性。

---

## 二、新增模块（7个）

### 1. `volume_price.py` — 量价配合信号分析
**来源**: Android `VolumePriceSignal.kt` + `VolumePrice.kt`

功能：
- 14种量价信号（放量上涨/缩量阴跌/放量滞涨等）
- 位置感知判定：同一量价组合在高位/低位含义相反
- 平盘带阈值（±0.5%）、急跌阈值（3%）、低位反弹阈值（10%）

核心接口：
```python
signal = VolumePrice.of(volume_ratio, change_pct, price_context)
tag_text = tag(signal)  # 返回 "🔥放量上涨" 等
```

### 2. `price_position.py` — 价格位置上下文
**来源**: Android `PricePosition.kt`

功能：
- 近60日价格区间计算
- 位置判定：低位（下1/3 + 累计涨幅≤0）、高位（上1/3 + 累计涨幅≥20%）
- 反弹幅度计算

核心接口：
```python
window = PricePosition.of(bars, today="2026-09-24")
context = window.context_of(price)  # 返回 PriceContext(location, rebound)
```

### 3. `volume_ratio.py` — 量比计算统一口径
**来源**: Android `VolumeRatio.kt`

功能：
- 盘中按已交易时间折算量比（避免上午10点算出0.1x）
- 20日均量计算（排除当日，避免分子分母重复）
- 折算时间下限保护（最少5分钟）

核心接口：
```python
ratio = VolumeRatio.of(today_volume, avg_volume_20, elapsed_minutes)
```

### 4. `technical_summary.py` — 技术分析结构化摘要
**来源**: Android `TechnicalSummary.kt`

功能：
- 从分析结果生成结构化摘要
- 包含：评分、趋势、量价、动量、支撑压力、操作建议

核心接口：
```python
summary = TechnicalSummary.from_result(result, price_context, volume_ratio)
text = format_summary(summary)
```

### 5. `turnover_tracker.py` — 成交额同期对比
**来源**: Android `TurnoverTracker.kt`

功能：
- 每15分钟记录两市累计成交额
- 与昨日同一时刻对比（不再用半日累计比全天）
- 全天预估（按昨日同期占比推算）
- 数据落盘保留最近30个交易日

核心接口：
```python
tracker = TurnoverTracker(data_file)
snapshot = tracker.sample(today_turnover, datetime.now())
lines = format_turnover_lines(snapshot)
```

### 6. `intraday_tactics.py` — 盘中买卖点提醒
**来源**: Android `IntradayTactics.kt`

功能：
- 5种信号：跌破支撑、放量突破、冲高做T、回踩接回、回踩支撑
- **两轮确认机制**（与Android版一致）：
  - 跌破支撑：一轮触发，立即提醒（风险信号，不延迟）
  - 其他信号：连续两轮确认（约30秒），避免假信号
- 冷却机制（条件消失后重置，允许重新触发）
- 每日上限（每只股票每种信号最多8次/天）

信号触发策略：

| 信号类型 | 处理方式 | 原因 |
|---------|---------|------|
| 跌破支撑 | 一轮触发，立即提醒 | 风险信号，尤其适合处理"刚买入后开始下跌" |
| 放量突破 | 连续两轮确认，约30秒 | 避免瞬时冲高、假突破 |
| 回踩接回 | 连续两轮确认，约30秒 | 避免价格短暂触及支撑就提示买回 |
| 回踩支撑 | 连续两轮确认，约30秒 | 买入建议不紧急，应优先减少误报 |
| 冲高做T | 连续两轮确认，约30秒 | 避免瞬时拉升导致误卖 |

核心接口：
```python
tactics = IntradayTactics()
signal = tactics.check_signal(
    code, name, price, change_pct, volume_ratio,
    limit_pct, support, resistance, score, quantity
)
```

### 7. `fundamentals.py` — 基本面分析（2176行）
**来源**: Android `Fundamentals.kt` + `FundamentalsReport.kt` + `FundamentalsClient.kt`

功能：
- 从东方财富API获取财务数据（利润表、现金流、资产负债表）
- 5维评分引擎（利润质量、现金质量、资产安全、成长性、资产清洁度）
- 估值分析（历史PE/PB分位数、同行对比、估值陷阱检测）
- 治理风险扫描（公告关键词匹配）
- 13段报告生成

核心接口：
```python
from fundamentals import fetch_and_analyze
report = fetch_and_analyze("sh600519", "贵州茅台")
```

### 8. `signal_confidence.py` — 信号置信度评估系统（280行）
**来源**: 基于Android版思路的自主设计

功能：
- 记录每15秒的tick数据（价格、量比、信号条件状态）
- 计算信号置信度（4个维度加权）
- 根据置信度动态调整确认轮数
- 记录信号触发后的价格变化，用于历史成功率计算

置信度计算（4个维度）：

| 维度 | 权重 | 说明 |
|------|------|------|
| 条件稳定性 | 40% | 信号条件持续满足越久，越可靠 |
| 价格动量 | 30% | 价格在信号方向上稳定移动 |
| 量能一致性 | 20% | 量比持续在阈值以上/下 |
| 历史成功率 | 10% | 该股票类似信号的历史表现 |

动态确认轮数：

| 置信度 | 确认轮数 | 说明 |
|--------|---------|------|
| ≥ 0.8 | 1轮（15秒） | 高置信度，快速触发 |
| ≥ 0.6 | 2轮（30秒） | 中等置信度 |
| ≥ 0.4 | 3轮（45秒） | 较低置信度 |
| < 0.4 | 4轮（60秒） | 低置信度，谨慎确认 |

核心接口：
```python
from signal_confidence import SignalConfidence

confidence = SignalConfidence()

# 记录tick数据
confidence.record_tick(code, price, change_pct, volume_ratio, conditions)

# 获取动态确认轮数
rounds = confidence.get_confirmation_rounds(code, signal_type, price, conditions)

# 记录信号触发和结果
confidence.record_signal_trigger(code, signal_type, price)
confidence.update_signal_outcome(code, signal_type, current_price)
```

---

## 三、修改的现有文件

### 1. `stock_analysis.py`
- 集成 `PricePosition` 计算价格位置
- 集成 `VolumeRatio` 统一量比计算
- 集成 `VolumePrice` 生成量价信号标签
- 返回值新增字段：`price_position`、`unified_volume_ratio`、`volume_price_signal`、`volume_price_tag`
- `format_report()` 显示量价信号标签和价格位置

### 2. `portfolio_analysis.py`
- 集成 `IntradayTactics` 盘中买卖点检测
- 集成 `VolumePrice` 量价信号标签
- 返回值新增字段：`intraday_signal`、`volume_price_tag`、`price_position`
- `format_portfolio_report()` 显示盘中信号和量价标签

### 3. `stock_menubar.py`
- 集成 `TurnoverTracker` 替代简单成交额对比
- 菜单栏标题显示「昨同期」成交额对比
- 盘面总结增加 TurnoverTracker 同期对比信息
- 个股分析增加 `TechnicalSummary` 结构化摘要
- 新增「📊 基本面分析」菜单项
- 新增「⚙️ 设置」菜单，包含：
  - 「🔔 测试通知」按钮
  - 「📊 测试盘中信号」按钮
- 新增 `_check_intraday_signals()` 轻量级盘中信号检测（每15秒）
- 通知机制：盘中信号检测到后立即发送 macOS 通知

### 4. `tests/test_menu_format.py`
- 修复测试辅助方法 `_app()` 初始化 `_turnover_snapshot` 属性

---

## 四、新增测试文件

### `test_intraday_tactics.py`
测试用例（13个）：
- `TestLimitPctForBoard`: 涨跌停幅度判定（主板10%、创业板20%、科创板20%）
- `TestBreakSupport`: 跌破支撑信号触发/未持仓不触发
- `TestBreakout`: 放量突破信号触发/无放量不触发
- `TestTSell`: 冲高做T信号触发/未持仓不触发
- `TestTBuy`: 回踩接回信号触发
- `TestPullback`: 回踩支撑信号触发
- `TestAntiSpam`: 防重复机制（同一信号不重复触发、条件消失后可重新触发）

---

## 五、关键设计决策

### 1. 盘中信号检测频率
- **Android版**: 每15秒检测（轻量级，只检查价格/量能条件）
- **macOS版修复后**: 每15秒检测（与Android版一致）
- **之前**: 每30分钟（太慢，会错过信号）

### 2. 通知防重复机制（与Android版一致）
- **跃迁检测**: 只在从"未触发"变为"触发"时通知
- **冷却机制**: 条件消失后重置冷却，允许重新触发
- **每日上限**: 每只股票每种信号最多8次/天

### 3. 量比计算统一口径
- 所有模块使用同一个 `VolumeRatio` 类
- 盘中按已交易时间折算（避免上午10点算出0.1x）
- 20日均量排除当日（避免分子分母重复）

### 4. 价格位置感知
- 量价信号需要结合价格位置判断
- 高位放量滞涨 = 出货信号
- 低位放量滞涨 = 底部换手信号

---

## 六、测试结果

```
现有测试: 26/26 通过
新增测试: 13/13 通过
总计: 39/39 通过
```

---

## 七、使用方式

### 基本面分析
菜单栏 → 📊 基本面分析 → 输入股票代码 → 自动生成报告并用 TextEdit 打开

### 测试通知
菜单栏 → ⚙️ 设置 → 🔔 测试通知

### 测试盘中信号
菜单栏 → ⚙️ 设置 → 📊 测试盘中信号（发送5条模拟信号）

### 盘中信号自动通知
程序每15秒自动检测持仓股的买卖点信号，触发时自动发送 macOS 通知。

---

## 八、文件清单

| 操作 | 文件 | 行数 | 说明 |
|------|------|------|------|
| 新增 | `volume_price.py` | 176 | 量价信号分析 |
| 新增 | `price_position.py` | 151 | 价格位置判定 |
| 新增 | `volume_ratio.py` | 135 | 量比计算 |
| 新增 | `technical_summary.py` | 158 | 技术分析摘要 |
| 新增 | `turnover_tracker.py` | 276 | 成交额同期对比 |
| 新增 | `intraday_tactics.py` | 240 | 盘中买卖点提醒 |
| 新增 | `fundamentals.py` | 2176 | 基本面分析 |
| 新增 | `signal_confidence.py` | 280 | 信号置信度评估系统 |
| 新增 | `test_intraday_tactics.py` | 200 | 盘中信号测试 |
| 修改 | `stock_analysis.py` | +30 | 集成价格位置/量比/量价信号 |
| 修改 | `portfolio_analysis.py` | +25 | 集成盘中信号/量价标签 |
| 修改 | `stock_menubar.py` | +250 | 集成所有新模块 |
| 修改 | `tests/test_menu_format.py` | +1 | 修复测试 |
