# 常用量化因子与策略配方（2026-11 扩充批次）

本批次参考外部公开研究与经典文献，向因子注册表补充 7 个常用虚拟因子，并附带 6 个可直接使用的经典策略。所有因子为**虚拟因子**（运行时 Polars/NumPy 表达式计算，无需重新物化 enriched 数据），在注册表、`strategy/scoring.py`、`backtest/matrix.py` 三处口径一致（与 2026-09-05 批次相同的三处同步模式）。

## 一、新增因子（注册表批次 2026-11-02）

追加于目录尾部，前 48 项挖掘调度顺序（`FACTOR_COLUMNS[:48]`）不变。

| 因子 | 分组 | 公式 | 含义与用法 |
| --- | --- | --- | --- |
| `sharpe_momentum_60d` | 动量 | 近60日日收益均值 / 日收益标准差 | 风险调整动量（滚动夏普动量）。比原始动量更稳健，优先选"涨得稳"的标的 |
| `fip_20d` | 收益形态 | 近20日(下跌天数占比 − 上涨天数占比) | 动量平滑度 FIP（Da, Gurun & Warachka 2014）。值越负动量路径越平滑、延续性越好；选动量股时避开"偶发大涨"型 |
| `gain_loss_ratio_20d` | 收益形态 | 近20日平均上涨幅度 / 平均下跌幅度绝对值 | 盈亏比。动量策略胜率通常仅约 50%，核心盈利来源是高盈亏比 |
| `ret_kurt_20d` | 收益形态 | 近20日日收益超额峰度（矩法） | 尾部风险。高值代表极端涨跌频繁，可与 MAX（`max_ret_20d`）互为印证 |
| `drawdown_20d` | 价格位置 | 收盘价 / 近20日最高收盘价 − 1 | 阶段回撤深度。短窗口版回撤，用于超跌反弹与趋势健康度判断 |
| `ma_alignment_score` | 趋势 | sign(收盘/MA−1) 对 MA5/10/20/30/60 求平均，∈[−1,1] | 均线多头排列得分。+1 为五线全多头，−1 为全空头，趋势健康度一目了然 |
| `turnover_trend_5_60` | 流动性 | 5日均换手 / 60日均换手 − 1 | 换手率（拥挤度）趋势。高值代表交易情绪快速升温，与低换手溢价因子（`turnover_mean_20d`）互补 |

注意事项：

- A股经典有效因子集中在**短期反转、低换手、低波动、彩票效应（MAX）、动量平滑度**等行为类因子；换手率类因子（`turnover_trend_5_60`、`turnover_mean_20d`）依赖 `turnover_rate`，crypto 标的该列恒为空（见 `docs/crypto-market.md`），crypto 侧不可用。
- `ret_kurt_20d` 与 Polars 侧表达式逐项对齐（同一组滚动矩），numpy 内核在窗口内有效样本不足时同样返回空值（fail-closed，与全部滚动虚拟因子一致）。

## 二、新增策略（`data/strategies/custom/`，matrix_native）

以下策略文件在启动时由 StrategyEngine 目录扫描加载（经 AST 安全校验），可直接在选股/回测/监控中使用。

| 策略 | 类型 | 入场 | 退出 | 依据 |
| --- | --- | --- | --- | --- |
| `sharpe_momentum_trend` 趋势动量 | 趋势 | 夏普动量达标 + FIP 平滑 + MA20 上方 | 跌破 MA20 | 风险调整动量 + 动量平滑度 |
| `crowding_aware_momentum` 拥挤度感知动量 | 趋势 | 60日动量为正 + 换手趋势未过热 + MA20 上方 | 跌破 MA20 | 动量 + 拥挤度（避免高换手负溢价） |
| `trend_alignment` 均线多头排列 | 趋势 | 五线排列得分 ≥ 0.8 + MA20 上方 | 得分 < 0.4 或跌破 MA20 | 趋势健康度 |
| `low_vol_low_turnover` 低波动低换手 | 异象 | 年化波动与 20日均换手双低 | 波动升破阈值 | A股低波 + 低换手溢价（按波动率升序取最低） |
| `boll_mean_reversion` 布林均值回归 | 均值回归 | 布林位置极低 + RSI 超卖 | 反弹至布林中轨 | 超卖回归 |
| `drawdown_rebound` 超跌回撤反弹 | 反转 | 20日回撤深 + RSI 超卖 | RSI 修复到中轴 | A股短期反转效应（按回撤深度降序取最深） |

使用提醒：

- 所有策略参数均可在前端参数面板覆盖；均值回归/反转类策略按因子值升/降序取极值（`order_by` + `descending`），趋势类按 `score` 加权评分排序。
- 反向因子（低波动、低换手、超卖）的"低方向评分"可通过用户 override 的 `scoring_directions` 实现；种子/配方默认用排序方向替代，行为一致且不依赖 override。
- 上线前请先在目标标的上回测（含走查/鲁棒优化），本文件只描述构造逻辑，不构成收益承诺。

## 二之二、交易理论类策略（`data/strategies/custom/`，2026-11 批次）

以下策略来自经典金融交易理论，均以 **matrix_native 向量化近似** 实现。注意：这是理论的工程化简化口径（滚动窗口 + 布尔条件），**不是**完整的递归/人工判读版本（例如缠论的完整笔-线段-中枢递归、艾略特波浪的浪型计数无法向量化），语义以各文件模块 docstring 为准。建议先在目标标的回测确认近似口径的有效性再实盘参考。

### 缠论类（Chan Theory）

| 策略 | 近似口径 | 入场 | 离场 |
| --- | --- | --- | --- |
| `chanlun_third_buy` 三买 | 中枢上沿 ≈ 剔除最近突破段后的 N 日最高价 | 近 M 日内突破上沿 + 回踩最低价不回中枢 + 阳线确认，可选缩量过滤 | 跌回中枢上沿 |
| `chanlun_bottom_divergence` 一买(底背驰) | 背驰 ≈ 近 M 日价格下移而 MACD DIF 上移 | 下跌趋势(MA20<MA60)中价格创 N 日新低 + DIF 抬升 | 收盘站上 MA10 |
| `chanlun_fractal_reversal` 底分型 | 分型 ≈ 最低价低于前后两根 K 线 | 底分型次日收阳并上穿分型高点确认，可选 RSI 过滤 | 顶分型确认（收盘跌破分型低点） |

### 经典交易理论类

| 策略 | 理论 | 近似口径 | 入场 / 离场 |
| --- | --- | --- | --- |
| `turtle_breakout` 海龟突破 | 海龟交易法则 | 唐奇安通道 20/10（剔除当日的滚动极值） | 破 20 日高点入 / 破 10 日低点出，可选 MA60 过滤 |
| `darvas_box_breakout` 达瓦斯箱体 | Darvas Box Theory | 前 N 日高低价构成箱体，高度 ≤ 中线 15% 为窄幅箱体 | 放量破箱顶入 / 跌回箱顶出 |
| `wyckoff_spring` 威科夫弹簧 | Wyckoff Method | 假跌破 = 盘中击穿 N 日支撑但收盘收回 + 收阳 | 弹簧出现入 / 再度跌破支撑出，可选缩量过滤 |
| `dow_trend_confirmation` 道氏理论 | Dow Theory | 高点抬高（收盘破前 N 日高点）+ 低点抬高 + MA20>MA60 + 量能放大 | 四重确认入 / 跌破 MA60 出 |

实现要点：所有滚动极值/位移使用 `app.backtest.matrix` 的 `valid_rolling_max/min/valid_shift` 原语（自动跳过停牌等无效 bar），与回测矩阵的 valid-bar 语义一致；策略文件通过引擎的 AST 安全校验（仅允许 numpy / app.backtest.matrix 白名单导入）。

## 三、公开资料参考

- 清华五道口《聪明的贝塔：来自A股市场因子动量策略的实证研究》：<http://cfrc.pbcsf.tsinghua.edu.cn/__local/4/AE/67/89980D797AD790C70C6AD15BEAB_F3C5BFB8_73992.pdf>（A股 14 个经典异象综述：规模/价值/盈利/波动/反转/换手率等）
- 海通证券《选股因子系列》研报合集：<https://quant-wiki.com/advanced/%E7%A0%94%E6%8A%A5%E7%B2%BE%E9%80%89/%E6%B5%B7%E9%80%9A-%E9%80%89%E8%82%A1%E5%9B%A0%E5%AD%90%E7%B3%BB%E5%88%97>
- 知乎《给你的动量选股策略加点"料"》（FIP 信息离散度 + MAX 彩票效应）：<https://zhuanlan.zhihu.com/p/40468929>
- 雪球《量化动量选股系统的构建》（FIP 与 RPS 动量配合）：<https://xueqiu.com/3733925017/148746256>
- 易方达官泽帆《关于因子表现评价的思考》（A股短期反转因子）：<https://www.efunds.com.cn/c/177/177917.shtml>
- WorldQuant《101 Formulaic Alphas》实现合集：<https://blog.csdn.net/qq_41996852/article/details/126728863>
- Qlib Alpha158 因子库解析：<https://blog.csdn.net/weixin_38175458/article/details/126625502>
- 东方财富《ETF策略：短期、中期还是长期动量效应有效？》（动量策略盈亏比来源）：<https://pdf.dfcfw.com/pdf/H301_AP202412241641416114_1.pdf>
- 北大国发院《特质波动率之谜》论文：<https://nsd.pku.edu.cn/docs/20250910112658145641.pdf>
