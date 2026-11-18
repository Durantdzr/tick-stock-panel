# 个股分析 × KiT 扩散模型预测 — 设计文档

日期：2026-11-17
状态：已与用户确认方案，待实施

## 背景与意图

在现有「个股分析」页面（`frontend/src/custom/stock-analysis/extension.tsx` + `/api/stock-analysis/*`）中集成开源 K 线预测扩散基础模型 [KiT](https://github.com/Luciferbobo/KiT)（arXiv 2609.34507，AGPL-3.0），用于**单只股票的未来 K 线预测**：选中股票后一键预测未来 20 根日线走势（中位数路径 + 分位带），与现有 AI 分析、关键价位并列使用。

用户本地无 GPU，KiT 代码内建 CPU 推理支持（`--device` 默认 `cuda if available else cpu`，CPU 自动 fp32 + sdpa attention），单次预测约 1~3 分钟（K=16 路径 × 32 Euler 步，284M 参数 fp32 ≈ 1.1GB 内存）。

## 目标 / 非目标 / 反目标

**目标**
- 个股分析页对单只股票（A股 / 美股 / crypto）做 KiT 日线预测并可视化
- 流式进度反馈（CPU 推理 1~3 分钟不能让用户干等）
- 环境未配置时 fail-closed 明确提示，不影响个股分析既有功能

**非目标（v1 明确不做）**
- 分钟级预测（1m~2h 七个粒度只做 1d）
- 历史锚点预测 / 验证集回测（KiT 的 `infer_for_eval.py`/`backtest.py` 不集成）
- 训练 / 微调（KiT 训练代码未开源）
- 批量预计算自选股（未来扩展）
- GPU 支持检测与切换（CPU 即可满足单票场景，代码天然兼容）

**反目标（失败标准）**
- KiT 代码混入本仓库（AGPL-3.0 传染）——必须保持子进程隔离
- 在主后端 venv 安装 torch（polars 锁死 <1.45，禁止混装大依赖）
- 预测失败静默吞掉，或环境缺失时页面报错崩溃

## 约束

- KiT 以独立 venv + 子进程方式调用，其代码 clone 到 `data/models/kit/`（git-ignored），本仓库只放调用脚本与集成代码
- 遵循 `docs/secondary-development.md`：后端走 `backend/app/custom/<module>.py` 的 `setup(registrar)` 扩展注册（现有先例：assistant、paper_execution、source_packages）；前端在 `frontend/src/custom/stock-analysis/extension.tsx` 内扩展（该页本身就是前端扩展）
- 核心源码零改动；子进程先例：`app/services/ai_provider.py` codex CLI
- CONTRIBUTING 数据契约：日 K 一律走 repo/enriched，禁止 API 层直读数据源；缓存键含 symbol+日期；查询键集中维护

## 实施决定（builder check 后确定）

1. **checkpoint 选 `step_market_284M/`（三市场，训练截止 2025-12-31）**。HF 上另一 bundle `step_market_284M_train260410/` 仅 A 股且无 us/crypto stats，与本设计"三市场"冲突。摘要卡标注训练截止 2025-12-31。若将来想用更新的 A 股-only bundle，可作为后续选项。
2. **锚点只用已收盘的完整日线**：盘中预测试取"最近一根已收盘日 K"为锚，丢弃盘中未收盘的当日 partial bar（KiT 日线语义为收盘 bar，喂 partial bar 是分布偏移）。收盘后最新 bar 自然完整。`prev_close` 即该 bar 的 close。
3. **取数 ≥320 根、fail-closed 下限 250 根**：按上游 `infer_for_pred.py` 的 keep 公式（Lc + 12×half_life×bars_per_day + 8 ≈ 318）取 320 根，保证 volume EMA 预热充分；`prepare_inference_window` 自行取最后 250 根。
4. **日历**：repo 无前瞻交易日历 API，改由 runner 在 KiT venv 内用 `exchange_calendars` 生成（XSHG=A股 / XNYS=美股），crypto 传全年历日表（24/7，修正 KiT 默认"周末跳过"）；统一传给 `prepare_inference_window(trading_days=...)`。库缺失时回退"仅跳周末"并在 stderr 打 PROGRESS 告警。crypto 日期沿用 repo 的北京日期口径，与历史 bar 对齐；KiT 训练侧日期口径不可考，作为已知轻微限制记录。
5. **instrument embedding 查表**：runner 读 checkpoint 的 `instruments.json`，symbol 匹配（A股 `600519.SH` 格式一致；us/crypto 原文匹配）则用其 `sector_id/vocab_index`，未命中回退 0（unknown）。
6. **安装路径以 `settings.data_dir` 解析**（`<data_dir>/models/kit/`），开发态即 repo `data/models/kit/`，桌面打包态为应用数据目录，两态一致；setup 脚本与 env_missing 检测均按此路径。

## 关键技术事实（已核实）

1. KiT `prepare_inference_window(source=DataFrame, inst_id=0, sector_id=0)`：0 = unknown 标的，无需股票在其训练集；直接喂 DataFrame（`datetime/open/high/low/close/volume`），不需要其 parquet/instruments.json 格式；checkpoint 自带 `instruments.json` 可查已训练标的的 embedding（见实施决定 5）
2. `compute_features` docstring 明确要求 **adjusted prices** —— 与本项目 enriched 前复权 `open/high/low/close` 口径一致
3. 预测输出 `paths_to_ohlcv(feats, prev_close)` 以 prev_close 锚定重建价格 —— prev_close 取最近一根**已收盘**日线 close（见实施决定 2），输出与前复权 K 线可直接拼接
4. `MARKET_IDS = {ashare:1, us:3, crypto:5}`；本项目 `stock→ashare`、`us→us`、`crypto→crypto`；index/etf 不支持 → fail-closed
5. 日线窗口固定：Lc=250 历史根 → Lh=20 预测根；可用完整日线不足 250 根 fail-closed；取数 320 根以保证 volume EMA 预热（见实施决定 3）
6. checkpoint：HuggingFace `Lucifer744/KiT_model`，选 `step_market_284M/`（~1.1GB，三市场，训练截止 2025-12-31），stats.json / instruments.json 随 bundle 提供
7. 子进程输出 JSON（stdout 最后一行），主进程解析；运行时需 KiT venv 的 python（CPU torch + 其 requirements + pandas_market_calendars）

## 方案取舍（已选定 A）

- **A（选定）：子进程按需推理**。单票场景延迟可接受（1~3 分钟），零常驻内存，AGPL 边界干净，与 codex CLI 先例一致。
- B（否决）：常驻模型服务进程 —— 内存 +1.1GB 常驻、进程管理复杂度不值当，单票低频场景用不上。
- C（否决）：每日批量预计算自选股 —— 与"单只票预测"诉求不符，记为未来扩展（届时单飞锁 + 每日管道可复用本次的 runner）。

## 架构

```text
前端 extension.tsx（个股分析页）
  「KiT 预测」按钮 → POST /api/kit/predict {symbol}
  ← NDJSON 流: {type:stage, ...} → {type:done, prediction} / {type:error}
  done 后: K 线图叠加预测 20 根蜡烛（中位数路径，区分色）
          + close p10/p90 分位带阴影 + 摘要卡（预期收益区间等）

后端 backend/app/custom/kit_prediction.py（setup(registrar) 注册路由）
  POST /api/kit/predict
    ├─ 校验: asset_type ∈ {stock, us, crypto}；已收盘 enriched 日线 ≥ 250 根
    ├─ 单飞锁（同时刻 1 个预测任务，复用 mining_process_lock 模式）
    ├─ repo 取 enriched 日线 320 根（复权 OHLCV，含 EMA 预热余量）→ DataFrame
    ├─ subprocess: <data_dir>/models/kit/.venv/bin/python backend/scripts/kit_predict_runner.py
    │     stdin: JSON{market, scale:"1d", ohlcv rows, pred_len:20, steps, paths, seed}
    │     runner(在 KiT venv 中运行): sys.path.insert(KiT根目录)
    │       prepare_inference_window → sampler → denormalize_paths → paths_to_ohlcv
    │       → stdout JSON {paths_ohlcv, future_timestamps, prev_close, meta}
    ├─ 汇总: per-bar median + p10/p90、预期收益 vs prev_close
    └─ NDJSON 流式回传（with_heartbeat 心跳 15s）

环境一次性 setup（scripts/setup_kit.sh + docs 更新）
  git clone KiT → <data_dir>/models/kit/（git-ignored；开发态即 data/models/kit/）
  uv venv <data_dir>/models/kit/.venv + CPU torch + KiT requirements.txt
  huggingface-cli download Lucifer744/KiT_model --include "step_market_284M/*" \
    --local-dir <data_dir>/models/kit/ckpt/
  （国内网络走 hf-mirror.com 镜像回退）
```

## 数据契约

- 输入：enriched 前复权 `datetime/open/high/low/close/volume`，已收盘日线 320 根（repo 缓存路径，与 `_load_kline` 同源；盘中丢弃未收盘的当日 bar）
- 输出（NDJSON `done` 事件 payload）：
  - `anchor_ts`、`pred_len`、`n_paths`、`steps`、`seed`（meta，复现用）
  - `bars`: 20 项 × `{ts, open/high/low/close 的 median, close_p10, close_p90}`（timestamp 为北京墙钟 naive，遵循 CONTRIBUTING 3.3）
  - `summary`: `{median_return, p10_return, p90_return, median_max_gain, median_max_drawdown}`（相对 prev_close 的小数制比例）
- 前端查询键集中加到 `queryKeys.ts`：`kit-prediction` 以 symbol+anchor 为键；切股不串缓存
- 错误事件区分：`env_missing`（未配置）/ `unsupported_market` / `insufficient_history` / `busy` / `inference_failed`

## 测试与验证

- 后端：market/symbol 映射单测；不足 250 根 fail-closed；单飞锁并发用例；runner 输出解析（mock 子进程 stdout/非零退出/超时）；env_missing 显式错误；`uv run pytest tests/test_kit_prediction.py -q` + ruff
- 前端：`pnpm build`；手工检查 loading/错误/空数据/切股状态
- 端到端冒烟：真实 checkpoint 对一只 A 股跑一次 CPU 推理，确认 NDJSON 与图形叠加
- setup 脚本：在干净目录验证 clone/venv/下载三步

## 风险与边界

- KiT（三市场 bundle）训练数据截至 2025-12-31，预测质量随时间衰减——摘要卡标注模型版本与数据截止
- crypto 日期口径沿用 repo 北京日期约定；KiT 训练侧 crypto 的日期口径不可考，存在最多 1 天错位的已知轻微限制（见实施决定 4）
- HF 下载需代理（本机 clash 7897），脚本提供 hf-mirror 回退
- AGPL：仅子进程隔离调用；若将来分发本项目再评估合规
