# 板块龙头封板/炸板分档与冲高做T：最终复核归档

日期：2026-10-19。范围：复核当天新增的板块龙头功能（封板抑制做T、炸板分档恢复做T、
龙头候选推荐与手动维护、关联持仓展示），以及它对两条做T路径和 t_filter 观察记录的接入。

## 结论

在当前源码、规格、测试和回归范围内，本项改动是工程层面的正向优化：

- 补上了做T判定缺失的一层——**同板块龙头在干什么**。原判定只看个股量价与全市场环境，
  当天实盘（亨通光电封死涨停、中天科技继续冲高，提示做T反而减在半山腰）正是这一层缺失的后果。
- 放开做T只认两条**可观察硬事实**（确认退潮、当日炸板≥2次），不靠解读盘口强弱。
  第 1 次炸板后守住高位按「强承接」继续持有；反复开板则不再等退潮。
- 接口预算可控：龙头候选每日一次、每板块 1 次批量行情；15 秒轮询只拉已配置龙头。
  **不为找龙头去扫板块成分股**。
- 风险信号优先级、其他信号的判定/阈值/跃迁/冷却规则均未改动；只新增一条抑制分支与恢复提醒。

同日两轮复核各修掉一处缺陷：其一，原设计的 600 秒冷却会吞掉「封板→强承接」的合法提醒
（封板后约 7 分钟定性）；其二，通知文案「龙头封板暂缓」读不通（会被误读成「龙头的封板暂缓了」）。
两处都用失败用例复现后修复并通过回归。

以上是工程质量、风险边界和行为隔离的判断，**不等于做T策略已经获得实盘收益证明**。
本规则未经历史回测，先靠 t_filter 观察事件攒样本复盘。

## 复核范围

| 范围 | 检查点 |
|---|---|
| 龙头状态机 | `sector_leaders.py`：封死双条件（贴涨停 + 卖一为空）、强承接、退潮双确认、当日炸板计数（回封不清零/跨日清零）、重新武装 |
| 龙头认定 | `sector_leaders.py`：成交额 top5、ST/\*ST 排除（不误伤「东方斯特」类名字）、pinned 板块不被自动覆盖 |
| 行情解析 | `stock_menubar.py`：新浪报文的买一/卖一/成交额/成交量字段（报文里已有，此前未解析） |
| 做T抑制 | `intraday_tactics.py`：`T_HOLD_LEADER` 优先于 `strong_market`/`cautious_market`；`signal_confidence.py` 新信号类型补进分支 |
| 低频路径 | `portfolio_analysis.py`：`bias_pct >= 8` 在龙头未转弱时改口「持有观察」，避免与 15 秒路径文案打架 |
| 恢复提醒 | `stock_menubar.py`：退潮/反复开板两个入口、具体动作文案（股数沿用 `_share_hint`/`T_PART`/一手取整） |
| 观察记录 | `t_filter_events` 扩到 `T_HOLD_LEADER`，环境栏记相位与龙头名，报告只写事实 |
| UI | 菜单「📊 板块龙头」：持仓板块、龙头与相位、关联持仓、手动增删、重新自动推荐 |
| 边界 | S佳通 ±5%、新股涨停价哨兵 `-1`、价格远超涨停价的无涨跌幅新股、成交量为 0 时不判退潮、卖一价异常视为未知 |
| 原流程隔离 | 其他信号的触发、冷却、每日上限、共享预算语义不变；风险信号（跌破支撑/离场风险）不被龙头状态过滤 |

## 验证记录

```sh
python3 -m unittest discover -s tests -q
# Ran 684 tests — OK (skipped=4)

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -q
# Ran 62 tests — OK
```

新增用例 37 项：`tests/test_sector_leaders.py`（32）、`test_intraday_tactics.py::TestTLeaderHold`（5），
另在 `tests/test_intraday_notification.py`、`tests/test_portfolio_daily_report.py` 补口径断言。
关键回归：

- `test_seal_then_strong_within_ten_minutes_still_notifies` —— 冷却吞提醒
- `test_first_blowout_without_fade_keeps_suppressing` / `test_second_blowout_releases_even_when_not_faded`
  / `test_blowout_count_resets_next_day` / `test_reseal_rearms_so_next_blowout_can_notify_again`
- `test_fade_requires_both_price_and_vwap` —— 退潮双确认，只破一项不得恢复
- `test_st_filter_does_not_hit_st_substring_names` —— ST 过滤不误伤

对 `sector_leaders.py`、`intraday_tactics.py`、`signal_confidence.py`、`portfolio_analysis.py`、
`stock_menubar.py` 执行语法检查通过。实盘冒烟（2026-10-19 11:30 亨通 +8.25%、卖一重新挂出、
vwap 48.52）：状态机正确判为「强承接」；中天 +3.91% 不在价格带内、不受影响。
测试运行仅有原环境的 urllib3/LibreSSL 警告，无测试失败。

## 保留限制

- **龙头认定**用「当日成交额 top5」是代理指标，不保证等于情绪龙头；可手动改，
  改过的板块锁定不被自动覆盖。
- **强承接**只表示炸板后守住高位、尚未确认退潮，不代表后续必涨；量能跟不上或大盘转弱时
  照样可能补跌。程序只输出相位与证据，不替用户拍板。
- **权衡**：第 1 次炸板未退潮时继续持有的代价是跟风股回吐那一段吃不到（当天中天约 1.1pp）；
  换来的是不会把短暂开板误判成退潮。反复开板即放开做T，是这条权衡的止损阀。
- **未经历史回测**：先靠 `t_filter_events` 记下被暂缓/被放开两类事件，按事实价格表现复盘，
  再决定阈值要不要动。
- Android 版（`IntradayTactics.kt`）本次不同步，差异见
  `docs/strategies/板块龙头封板与做T.md` 的跨端对照表。
