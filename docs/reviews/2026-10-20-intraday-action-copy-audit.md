# 2026-10-20 盘中动作文案绕过审计

## 搜索范围

执行计划 Task 5 的固定搜索式：

`可考虑.*买|小仓补|做T|减仓|清仓|接回`

覆盖 `intraday_tactics.py`、`portfolio_analysis.py`、`dip_rebound.py`、`portfolio_daily_report.py`、`stock_menubar.py`、`ai_analysis.py`、`deep_loss_policy.py`、`market_alert.py`、`observation_ui.py`、`stock_analysis.py`。

## 结论

搜索仍会命中动作词，但它们已按“内部候选/条件式教育/守卫输入/真实输出”分类。经本轮修复，真实盘中通知、模拟信号通知、持仓报告、下探回升、日报告、龙头恢复和交易策略输出不再绕过统一动作守卫。未确认动作只能输出观察、风险事实或下一交易日复核。

## 逐文件归类

| 文件与命中位置 | 分类 | 处理 |
|---|---|---|
| `intraday_tactics.py:42-153` | 信号枚举、阈值和注释 | 内部候选定义，不直接通知 |
| `intraday_tactics.py:315-697` | 旧候选/模板文案 | 结构化动作输入；真实输出经 `_apply_intraday_trade_policy`、`evaluate_trade_action` 和 `trade_action_guard` |
| `portfolio_analysis.py:46-241` | 深套剧本、状态动作候选 | 报告和通知统一经 `guard_actionable_copy` / `guard_intraday_signal` |
| `portfolio_analysis.py:363-396` | 状态含义和报告标签 | 观察/风险解释；动作行仍由守卫生成 |
| `dip_rebound.py:235-250` | 下探回升候选模板 | `_build_alert` 的结论统一经 `guard_actionable_copy` |
| `portfolio_daily_report.py:74-1255` | 分档、信号标签、报告分节 | 内部分类或观察标题 |
| `portfolio_daily_report.py:1563-1579` | 深套/破位/做T候选模板 | 最终 `action_summary` 统一经 `guard_actionable_copy` |
| `portfolio_daily_report.py:460、1405` | 组合级固定规则 | 已改为“复核条件/不直接执行”，不再下达补仓或离场命令 |
| `stock_menubar.py:7115-7240` | 模拟场景、状态短文案 | 模拟通知和状态通知统一经 `guard_intraday_signal` |
| `stock_menubar.py:7384-7570` | 意图分类、T+1降级、确认动作 | 买入/风险/普通卖出/做T分支统一经 `guard_actionable_copy` |
| `stock_menubar.py:8202-8274` | 龙头暂缓/恢复候选 | 恢复动作经 `guard_actionable_copy`；暂缓只表达持有观察 |
| `ai_analysis.py:66-80、1230-1242` | 教育性深套/滚动T条件示例 | 提示词明确本地动作守卫最高优先级，禁止AI直接下达 |
| `deep_loss_policy.py:5-9` | 策略文档字符串 | 基本面/深套方法说明，不是通知输出 |
| `market_alert.py`、`observation_ui.py` | 无命中 | 无需修改 |
| `stock_analysis.py:831` | 风险解释 | 明确“趋势转弱观察，不等同于立即清仓” |

## 本轮额外修复

- 持仓报告中的原始 `intraday_signal` 不再直接显示旧动作正文。
- `_send_portfolio_notification` 对盘中信号和状态变化通知都先过守卫。
- “测试盘中信号”只发送模拟、不可执行的守卫文案，避免测试入口下发真实动作。
- 龙头恢复、日报告、下探回升的候选词保留用于内部分类，但最终用户文案由守卫裁决。

## 回归证据

- `tests/test_all_intraday_action_surfaces.py` 覆盖持仓报告原始信号、AI提示词、下探回升和日报告固定规则。
- `tests/test_intraday_notification.py` 覆盖盘中通知和持仓状态通知不能绕过守卫。
- `tests/test_intraday_trade_integration.py`、`tests/test_trade_date_authority.py` 覆盖T+1和双周期决策。
