# 项目知识库索引

> 这里是程序的「内部知识库」：AI 问答的 `search_project_docs` 工具会全文检索
> 本目录及全部 `.md` / `.py`（含源码注释），**新增文档/策略会被自动纳入，无需任何登记**。

## 📂 目录结构

```
docs/
├── README.md                    ← 本文件（知识库总索引与约定）
├── strategies/                  ← 【策略库】每个策略一份详细文档，可增长
│   ├── 513低位选股策略.md
│   ├── 统一趋势与高位结构决策.md
│   ├── 入场结构与择时策略.md
│   ├── 大盘预警策略.md
│   └── 个股盘中预警策略.md
├── market-summary-template.md   ← 盘面总结模板（收盘版）
├── market-summary-midday-template.md ← 盘面总结模板（午间版）
├── optimization-log-2026-09-24.md    ← 专项优化日志
└── superpowers/                 ← 历史规划/规格存档
```

项目根目录另有：`A股监控使用说明.md`（用户手册）、`CHANGELOG.md`（变更摘要）、
`CHANGES_YYYY-MM-DD.md`（按日归档，含根因/验证/经验记录）。

最终审查归档：`reviews/2026-10-14-entry-review-final-review.md`（买点账本与模拟复盘一期）、
`reviews/2026-10-15-final-review.md`（持仓加仓评估与近几日做T过滤，含边界修复、验证与适用限制）。

## 🧭 策略库（strategies/）的定位

**每个选股/分析策略**在库里占一份文档，与代码、测试一一对应：

| 策略 | 文档 | 代码模块 | 测试 | 菜单位置 |
|---|---|---|---|---|
| 513 低位形态 | `strategies/513低位选股策略.md` | `five_one_three.py` | `tests/test_five_one_three.py` | 📈 指标策略 → 513形态 |
| 选股持续观察池与买点提醒 | `strategies/选股持续观察池.md` | `observation_pool.py`、`observation_alerts.py`、`observation_ui.py`、`stock_menubar.py` | `tests/test_observation_pool.py`、`tests/test_observation_integration.py`、`tests/test_observation_alerts.py`、`tests/test_observation_alert_integration.py`、`tests/test_observation_ui.py`、`tests/test_observation_ui_integration.py` | 📈 指标策略顶部买点 / 🔎 持续观察池 / 浮窗 / 系统通知 |
| 买点事件账本与模拟复盘 | `strategies/买点事件账本与模拟复盘.md` | `entry_review.py`、`entry_review_store.py`、`entry_review_service.py` | `tests/test_entry_review.py`、`tests/test_entry_review_store.py`、`tests/test_entry_review_service.py`、`tests/test_entry_review_integration.py` | 📈 指标策略 → 🔎 持续观察池 → 📒 查看买点复盘报告 |
| 入场结构与择时 | `strategies/入场结构与择时策略.md` | `entry_structure.py` | `tests/test_entry_structure.py` | 持仓分析报告 |
| 统一趋势与高位结构决策 | `strategies/统一趋势与高位结构决策.md` | `trend_assessment.py`、`high_position_policy.py`、`portfolio_analysis.py`、`intraday_tactics.py`、`observation_pool.py`、`portfolio_addition.py` | `tests/test_trend_assessment.py`、`tests/test_trade_gate_policy.py`、`tests/test_high_position_policy.py`、`tests/test_intraday_trend_gate_integration.py`、`tests/test_observation_trend_gate.py`、`tests/test_trend_policy_boundaries.py` | 个股分析、持仓报告、盘中提醒、观察池、加仓评估 |
| 大盘预警 | `strategies/大盘预警策略.md` | `market_alert.py` | `tests/test_market_alert_realert.py`、`tests/test_notify_urgency.py` | 自动检测（无需手动开启） |
| 个股盘中预警 | `strategies/个股盘中预警策略.md` | `intraday_tactics.py`、`signal_confidence.py` | `test_intraday_tactics.py` | 自动检测 + 📊 测试盘中信号 |
| 成交额环境质量与冲高做T | `strategies/成交额环境质量与冲高做T策略.md` | `market_environment.py`、`intraday_tactics.py`、`portfolio_daily_report.py` | `test_intraday_tactics.py`、`tests/test_signal_confidence.py` | 盘中提醒 + 持仓日报 |
| 板块龙头封板与做T | `strategies/板块龙头封板与做T.md` | `sector_leaders.py`、`intraday_tactics.py`、`portfolio_analysis.py`、`stock_menubar.py` | `tests/test_sector_leaders.py`、`test_intraday_tactics.py`、`tests/test_intraday_notification.py` | 📊 板块龙头 + 盘中提醒 |

## ➕ 新增一个策略的落位约定

1. **代码**：独立模块（如 `xxx_strategy.py`），判定核心与取数分离，纯函数可测
2. **测试**：`tests/test_xxx.py`，含边界/回归用例（关键坑要写进用例名或 docstring）
3. **文档**：`docs/strategies/xxx策略.md`，至少覆盖：
   - 策略思想（为什么这样设计）
   - 判定流程与**全部参数**（阈值 + 每条规则的「为什么」）
   - 阶段/显示语义、排序规则
   - 边界行为（已知会怎么判的特殊情况）
   - 与其他版本/平台的差异表（如对齐 Android 版）
   - 使用与限制（数据源口径、免责声明）
4. **接入**：菜单区段（如 📈 指标策略 / 新增菜单）+ 漏斗统计（如有）
5. **登记**：`CHANGELOG.md` 一条 + 当日 `CHANGES_YYYY-MM-DD.md` 归档（根因/验证/经验）
6. **AI 自动可用**：无需额外步骤 —— `search_project_docs` 检索全部 `.md`/`.py`，
   用户问「XXX策略是什么」时模型会先查这里

## 🔍 AI 问答的取信息原则（回顾）

**先判断问题范围**（个股、板块、市场、程序规则或通用概念）→ **只取相关证据**：
个股综合分析才组合技术面、基本面、新闻和市场/板块背景；板块问题不拉取个股报告；通用概念可直接回答。
需要事实或时效性信息时，再立足项目自身（本知识库 + 程序接口/报告）并按需叠加网络检索（行业/市场/事件）。
回答里「项目数据/规则」与「网络观点」分开表述。因此策略文档写得越完整，AI 对该策略的回答越准确——文档就是模型的依据。
