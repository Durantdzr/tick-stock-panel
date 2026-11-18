# AI 因子进化（ai-mining）

基于 LLM 的因子进化循环：假设提出 → 因子编码（沙箱执行）→ 面板评估 → LLM 反馈 → 记录进化，优胜因子经 ext_data 通道自动注册进面板因子库。

## 功能边界

- 只做 A 股日线、全市场面板的因子挖掘与评估；不做模型进化、不做组合优化、不做自动交易。
- 因子以"值面板"形态接入评估（鸭子引擎零注册），与本仓库 factor registry 四处同步模式完全解耦。
- 循环引擎为进程内后台任务（复用 mining 编排：spawn 子进程隔离 + run store 持久化），非独立脚本。

## 口径

- `IC` = 日截面 Rank IC 均值；`IC_IR` = IC 均值/标准差；胜率 = IC>0 天数占比。
- `annualized_return` / `max_drawdown` 为 Q5（因子值最高组）等权组合的**绝对**口径（T+1 开盘成交、含佣金/滑点/涨跌停），**非对基准超额收益**。
- 多空（Q5-Q1）为理论价差诊断，无做空撮合，只有区间总收益。
- 评估窗口按配置（默认建议 ~90 交易日）；数据窗口目前仅 ~1 年，IC 绝对值小属正常，不构成 alpha 结论。

## 沙箱边界（如实声明）

- 静态层：`_validate_safety` AST 名单（pandas 入白名单，parquet IO 定向豁免，pickle/read_csv 等合同外 IO 全拦）。
- 执行层：受限子进程（cwd 囚笼、环境白名单、超时终止）。无网络强制断开（AST 拦 socket 导入）。
- 信任级别与 AI 策略生成（ai_generator）一致；因子产物只有"值"进入评估，代码本身不进策略目录。

## 门禁与预算

- `max_llm_calls`（默认 120，上限 400）硬预算：耗尽显式终止 run，不静默降级。
- 同一时刻仅允许 1 个进行中的 run（409 防重入）。
- 编码修复回喂 ≤ `max_repairs`（默认 3）；SOTA 去重：候选与既有因子逐日截面秩相关均值 > 0.9 拒绝。
- run 状态机：queued → running → (cancelling) → succeeded/failed/cancelled/interrupted；启动时自动把遗留 running 标记为 interrupted。

## 资源与 API

- run 目录：`data/research/llm_evo/runs/{run_id}/`（manifest/summary/events.jsonl/memory.json/panel.parquet/rounds/{n}/{因子}/）。
- API 前缀 `/api/ai-mining`：`POST /runs`（启动）、`GET /runs`、`GET /runs/{id}`、`GET /runs/{id}/events`（SSE，Last-Event-ID 续传）、`POST /runs/{id}/cancel`、`GET /runs/{id}/result`。
- LLM：项目 AI 设置（设置页 ai_base_url/key/model），chat-only；embedding 不参与。
- SOTA 落地：成功的 run 自动把优胜因子经 ext_data 写入 `data/ext_data/llmevo_<id>/`，因子库自动出现 `ext_llmevo_<id>_value`；同名因子多轮出现取最新轮。
