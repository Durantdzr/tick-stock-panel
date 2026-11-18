# RD-Agent 桥接 Phase A — 端到端结论与 Phase B 输入

日期：2026-11-18
状态：Phase A 机械通路验证完成（端到端 exit 0）；语义挖掘质量待真实 embedding key
配套文档：`2026-11-18-rdagent-bridge-design.md`（设计）、`../plans/2026-11-18-rdagent-bridge.md`（计划）

## 一、结论

**可行性已实证**。RD-Agent 的 fin_factor 进化循环在本仓库自研回测上端到端跑通，全程 macOS 直跑、零 Docker、零 Qlib：

- 完整循环 5 步（假设生成 → 因子编码 → 桥接评估 → LLM 反馈 → 记录）exit 0，失败被循环优雅隔离。
- 指标交叉验证一致：trace 内 REV5 因子 IC=0.0132/IR=0.063 与手跑 `factor_panel_eval.py` 逐位吻合。
- LLM（glm-5.2，智谱直连）第一轮即生成教科书级因子族：REV5/REV20（反转）、LOWVOL20（低波动）、ABNVOL20（异常波动）、ILLIQ20/AMI20（Amihud 非流动性）、LSVOL20。
- 组合因子信号真实：IC 0.08~0.10、IC_IR 0.50~0.57、IC 胜率 70%+、多空 Sharpe 1.2~2.9（2026-07-01..11-13，5576 只 A 股，T+1/费用/涨跌停约束下）。
- 单轮成本：约 20-30 次 chat 调用 + 数次 embedding，每循环评估耗时约 6 分钟（其中桥接评估仅 ~30 秒）。

**口径声明（防误读）**：
1. `annualized_return`/`max_drawdown` 为 Qn（因子值最高组）**绝对**口径，非对基准超额。
2. 本轮 embedding 为本地 dummy shim（确定性伪向量）——知识检索无语义，**挖掘结论暂无统计意义**；chat 链路（假设/编码）真实。恢复语义验证只需两个动作（见第四节）。
3. 数据窗口仅 ~1 年（251 交易日），单轮因子 IC 不构成 alpha 结论。

## 二、遇到的问题与修复（迁移 B 时全部可跳过）

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | pymupdf/cryptography 源码构建失败 | 最新版无 macOS x86_64 wheel | 钉 pymupdf==1.24.11、cryptography==43.0.3（constraints 文件） |
| 2 | 场景初始化崩（CondaConf） | LocalEnv 硬依赖 conda CLI | clone 补丁：无 conda 时回退 LocalConf（bin_path=venv/bin + enable_cache=False） |
| 3 | `/bin/sh: timeout: command not found` | LocalEnv 用 GNU timeout，macOS 无 | venv/bin 下 20 行 Python timeout 垫片（bin_path 首位自动命中） |
| 4 | enable_cache 吃到修复前的旧结果 | EnvConf.enable_cache 默认 True 持久缓存 | 补丁中已关（见 #2） |
| 5 | 组合因子索引变三层 | pandas groupby.apply 压入分组键 | runner 改用 groupby.transform |
| 6 | feedback KeyError 'Current Result'/'SOTA Result' | process_results 依赖 qlib_res.csv 的字符串列名 "0" | runner 侧 Series name="0" 对齐契约 |
| 7 | chat 400 "模型不存在"（评估阶段） | embedding_model 默认 text-embedding-3-small 在智谱不存在 | 独立 embedding 配置（见 #8） |
| 8 | 智谱 embedding-3/embedding-2 直连恒 429 | 项目 key 的 embedding 配额不可用 | 本地 dummy shim + 激活 embedding_openai_base_url 死配置（clone 补丁） |
| 9 | LITELLM_SETTINGS 日志明文打印 API key | 上游直接 model_dump 入日志 | clone 补丁：脱敏后输出。**注意：修复前的日志文件（e2e_run*.log 首个）含明文 key，已属泄露面，建议轮换该 key** |
| 10 | 首轮跑了默认 QlibFactorRunner | .env 漏配 QLIB_FACTOR_RUNNER | 已补（含 PYTHONPATH） |

**clone 补丁清单（rdagent-bridge/RD-Agent 内 4 个 commit，迁移 B 只需提示词与语义）**：
`build(bridge)` pymupdf 钉版 / `prompt(bridge)` 评估语义改写 / `fix(bridge)` LocalConf 回退+日志脱敏+缓存关闭 / requirements cryptography 经 constraints 文件处理。

## 三、Phase B 资产清单

**直接迁移（B 的现成内核）**：
1. `backend/scripts/factor_panel_eval.py` — 评估内核（鸭子引擎 + FactorBacktestService + 三指标别名），已测试已实测。
2. `backend/scripts/export_daily_pv.py` — 数据导出（含 symbol 双向映射 fail-closed）。
3. `custom_runner.py` 的指标映射与组合 z-score 逻辑（transform 版）。
4. `RD-Agent/scenarios/qlib/experiment/prompts.yaml` 中改写后的 `qlib_factor_simulator`/`qlib_factor_background`（截面分层/T+1/涨跌停评估语义，1122 字符）——B 的提示词底稿。

**借鉴实现（不抄代码，抄机制）**：
5. RDLoop 五步循环骨架与失败隔离（`rdagent/components/workflow/rd_loop.py`，~240 行可类比实现）。
6. CoSTEER 的"规格→编码→执行反馈→最终裁决"评估链（`components/coder/factor_coder/evaluators.py`、`eva_utils.py`）。
7. Trace DAG + SOTA 追踪 + deduplicate_new_factors 相似度淘汰。

**B 需自研**：Trace 持久化与会话恢复、知识库（真 embedding 后才有意义）、并行 loop、UI。

## 四、恢复语义验证（两个动作）

1. 提供 embedding（SiliconFlow 免费 bge-m3 或修复智谱配额），改 `.env`：
   - 注释掉机械验证三行（EMBEDDING_MODEL=dummy / EMBEDDING_OPENAI_BASE_URL / EMBEDDING_OPENAI_API_KEY）
   - 配 `EMBEDDING_MODEL=litellm_proxy/BAAI/bge-m3` + `LITELLM_PROXY_API_KEY` + `LITELLM_PROXY_API_BASE`（或智谱修好后 `EMBEDDING_MODEL=openai/embedding-3` 走主 base）
2. `kill dummy_embedding_server` 进程，然后 `set -a && source .env && set +a && .venv/bin/rdagent fin_factor --loop-n 3`。

## 五、验证矩阵执行记录

| 验证 | 结果 |
|---|---|
| 导出适配器测试 | 21 passed（含 symbol 双向映射/预热/空面板） |
| 评估适配器测试 | 11 passed（正常/全 NaN/日期越界/qlib 映射/JSON 消毒） |
| 真实数据冒烟 | 91.7 万行 × 5600 只；20 日动量 IC=-0.037（A 股反转效应，方向合理） |
| macOS 冒烟 | ①模块导入 ②真实工作区执行因子 ③LLM 全栈 ④health_check(Docker 项按设计 N/A) |
| Runner 单链路 | validate_runner.py 通过（reverse_5d 因子全指标） |
| 端到端 | run6: exit 0, 5/5 步, 零错误, 组合 IC=0.0828 |
| 指标交叉验证 | REV5 手跑 IC=0.0132/IR=0.063 == trace 内一致 |
| 仓库回归 | worktree 内 backtest 模块 471 passed / 1 skipped（基线未动） |
| ruff | 全部新增文件零告警 |

## 六、Phase B M1 结论（2026-11-18 增补）

B-M1 完成：进化循环已内嵌本仓库后端（`app/ai_mining/`），RD-Agent 本体不再参与运行。

- **端到端实测**（真 LLM glm-5.2，智谱直连，2 轮 × 2 因子，run `b70b1ce7`）：8 次 chat 调用完成全部循环；LLM 第 1 轮产出 REV5/TURN_AB20、第 2 轮基于反馈自主转向 AMP20/ONR20（记忆注入生效，无向量库）；TURN_AB20 IC=0.054/IC_IR=0.637/胜率 75%（真实全市场评估，覆盖率 99.7%+）；AMP20 指标手跑交叉验证一致（IC=0.0458/IR=0.1413）。
- **SOTA 落地实测**：4 个进化因子经 ext_data 自动注册，运行中的服务 `GET /api/factors` 立即可见 `ext_llmevo_{amp20,onr20,rev5,turn_ab20}_value`。
- 运维文档：`docs/ai-mining.md`；设计：`2026-11-18-rdagent-phaseb-design.md`。
- 后续候选（M2）：最小前端工作台页、向量知识库（需 embedding）、模型进化（fin_model 对应物）、 更多市场（etf 已兼容评估路径）。
