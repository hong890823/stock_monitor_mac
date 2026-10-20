# 买点事件账本与模拟复盘一期

**Goal:** 保存观察池合格买点的证据，跟踪后续 1/3/5/10 个市场交易日，并独立计算固定规则下的模拟结果。

**Architecture:** 新增纯计算模块 `entry_review.py`、SQLite 账本 `entry_review_store.py` 和独立后台服务 `entry_review_service.py`。主流程仅通过非阻塞队列投递不可变事件、通知结果；后台独立读取日线、写账本与报告。新增模块加载、初始化、队列或数据库失败均不得改变选股、提醒、真实持仓。

**Tech Stack:** Python 3.9 标准库、sqlite3、threading、queue；复用现有日线请求接口。

**Spec:** 用户已确认前一轮的一期方案，并明确“不影响现在项目的主流程”；本文件记录实现边界。

## 设计约束

- 买点唯一键为策略版本、股票、观察轮次、信号日期；通知重试不会重复建仓。事件在通知发送前记录，发送结果单独保存。旧提醒不倒填成新证据。
- 保存信号时间、触发价、上证指数同时刻价格、选股来源、观察天数、MA20、量比、资金来源与时间、确认样本、失效条件和固定模拟参数。
- 后续交易日以完整指数日线日期为准；个股停牌或缺数据时该日结果留缺，不顺延冒充该日。触发当天完整日线的高低价不得用于触发后最大涨跌。
- 事件永久保留，退出观察池后继续复盘。首次取得的日线留存，复盘快照追加保存，可追溯输入；不修改真实持仓和观察池决策。
- 每事件独立假设资金 10,000 元、100 股一手；下一个市场交易日开盘尝试，滑点 0.1%，佣金 0.025% 且每边最低 5 元，卖出税费假设 0.05%，过户费假设每边 0.001%。这些是冻结的模拟参数，不声明实际券商费率。
- 买入较触发价偏离超过 3%、开盘已低于失效价、无有效开盘区间、一字日或数据不完整时，不假设买入；不追补为更晚日期的买入。
- 卖出遵守模拟 T+1；固定止损为触发失效价，止盈为入场价上方两倍风险距离，第 10 个市场交易日收盘退出。日线同日触及止盈止损时先按止损计算。一字日/缺失日/跳空价格异常不伪造成交，保留待核对或待退出状态。
- 所有成交均标记为“日线假设成交”，无逐笔成交保证；分红送转/复权不连续需核对。独立事件的收益不能相加冒充共享资金组合收益。
- 自动日线复盘仅盘后/盘前独立运行，单线程有界请求，盘中仅落事件。手动复盘可随时触发；队列饱和、异常可见，可一键暂停与恢复整个附加模块。

## 执行与验证

- [x] 纯计算：先写 `tests/test_entry_review.py`，验证实际交易日、未来/未收盘数据隔离、停牌缺口、基准、止损止盈冲突、T+1、费用与不可成交；再实现 `evaluate_event(event, stock_bars, benchmark_bars, at)`。
- [x] 账本：先写 `tests/test_entry_review_store.py`，验证唯一键、证据不覆盖、通知结果、重新打开恢复、失效数据、历史复盘快照与来源分组；再实现 `EntryReviewStore.record_event/review/events/report`。
- [x] 后台：先写 `tests/test_entry_review_service.py`，验证非阻塞投递、写入失败重试、队列饱和、盘中不自动抓日线、暂停、无观察池依赖；再实现 `EntryReviewService`。
- [x] 集成：给提醒增加证据字段，独立异常屏障投递；加入报告/立即复盘/暂停菜单。验证模块失败仍发送原通知，发送失败仍保留事件，去重与现有弹窗行为不变。
- [x] 更新策略文档、索引、变更日志；运行针对性测试、全量 unittest 与 py_compile，报告实际结果。

最终验证：全量 540 项测试通过（本期新增 49 项），5 个涉及的生产 Python 模块编译通过。独立审查服务遇到 429 限流，已终止该审查并完成本地复核；未重启运行中的程序、未发送真实测试通知、未修改真实持仓。

后续最终复核已归档：`docs/reviews/2026-10-14-entry-review-final-review.md`。修复三处边界问题并新增 4 项回归测试；最新全量 544 项、根目录盘中策略 46 项通过，编译通过。本期相关测试现为 53 项。

验证命令：

```sh
PYTHONPYCACHEPREFIX=/private/tmp/stock-alert-pycache python3 -m unittest tests.test_entry_review tests.test_entry_review_store tests.test_entry_review_service tests.test_entry_review_integration
PYTHONPYCACHEPREFIX=/private/tmp/stock-alert-pycache python3 -m unittest discover -s tests
PYTHONPYCACHEPREFIX=/private/tmp/stock-alert-pycache python3 -m py_compile entry_review.py entry_review_store.py entry_review_service.py observation_alerts.py stock_menubar.py
```
