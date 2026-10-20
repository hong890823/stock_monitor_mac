# 持仓加仓评估与近几日做T过滤：最终复核归档

日期：2026-10-15。范围：复核当天新增的持仓多周期加仓评估、近 3/5 日走势参与做T的过滤逻辑，以及指标策略二级页面展示优化。

## 结论

在当前源码、规格、测试和回归范围内，两项改动均为工程层面的正向优化：

- 加仓评估把原本分散的长周期、基本面和风险预算证据收敛为最多一只条件式候选，并明确“不自动下单、不修改持仓、不发新通知”。
- 做T过滤把近期趋势和量能作为质量门槛，减少刚突破就卖、弱势下跌中接回、放量回踩追接等假信号；缺少近几日数据时保留旧口径。
- 指标策略页面将规则说明移出界面，优先展示严格、宽松两档个股列表；选股规则、排序和扫描范围不变。

本次复核发现一处边界缺陷：空的 `recent_trend` 映射会被误判为已有近几日数据，导致旧调用方的回踩接回被额外过滤。已用失败测试复现，修复为只有至少一个近几日字段可用时才启用新量能过滤，并通过回归。

以上是工程质量、风险边界和行为隔离的判断，不等于加仓建议或做T策略已经获得实盘收益证明。

## 复核范围

| 范围 | 检查点 |
|---|---|
| 多周期计算 | `portfolio_addition.py`：周K/月K/年K重采样、MA250、评分、否决、排名、止损和股数 |
| 数据与失败降级 | `portfolio_addition_service.py`、`portfolio_addition_store.py`：缓存新鲜度、原子持久化、接口失败隔离 |
| 报告集成 | `portfolio_daily_report.py`：加仓机会章节、最多一只候选、无建议和缺失证据文案 |
| 做T口径 | `entry_structure.py`、`intraday_tactics.py`：3/5 日上下文、突破/弱势过滤、缩量回踩和旧口径兼容 |
| 原流程隔离 | `stock_analysis.py`、`portfolio_analysis.py`、`stock_menubar.py`：只透传上下文和过滤信号，不改风险优先级、观察池、持仓和交易 |
| 指标策略 UI | `stock_menubar.py`：严格/宽松列表置顶、513 作为附加结果，移除界面规则说明，保留刷新状态和漏斗统计 |

## 验证记录

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -q
# Ran 53 tests — OK

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
# Ran 620 tests — OK (skipped=4)
```

另对 `entry_structure.py`、`intraday_tactics.py`、`stock_analysis.py`、`portfolio_analysis.py`、`stock_menubar.py`
执行 AST 语法检查通过。测试运行仅有原环境的 urllib3/LibreSSL 警告，无测试失败。

## 保留限制

- 加仓综合分是规则化参考，不能替代基本面持续跟踪、真实盘口和风险承受能力判断。
- 长周期历史不足、当前周期未完成或基本面过期时，宁可输出“不建议加仓”，不补造确定性。
- 近几日数据只做过滤，不把“最近几天涨了”直接解释为买入信号，也不预测未来收益。
