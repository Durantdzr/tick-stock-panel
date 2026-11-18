# KiT 扩散模型预测集成（个股分析）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在个股分析页集成 KiT 扩散模型，对单只股票（A股/美股/crypto）做未来 20 根日线的 CPU 推理预测并叠加可视化。

**Architecture:** KiT 代码 clone 到 `<data_dir>/models/kit/`（git-ignored）+ 独立 venv（CPU torch），主后端通过子进程调用 runner 脚本；服务层 `backend/app/services/kit_prediction.py` 负责取数/映射/编排/汇总，`backend/app/custom/kit_prediction.py` 注册 NDJSON 流式路由；前端在个股分析扩展内加按钮 + 摘要卡 + ECharts 预测叠加。

**Tech Stack:** Python 3.12（主 venv: uv + FastAPI + Polars；KiT venv: torch CPU + kit_v1 + exchange_calendars）、React 18 + TypeScript + ECharts。

**Spec:** `docs/superpowers/specs/2026-11-17-kit-prediction-design.md`（含 6 条实施决定，计划与其一致）

## Global Constraints

- KiT 代码绝不进本仓库；主 venv（`backend/.venv`）不安装 torch / exchange_calendars，它们只进 KiT venv；polars 版本锁 <1.45 不动
- KiT 输入为**前复权** OHLCV（enriched 列），锚点只用**已收盘**完整日线；可用完整日线 <250 根 fail-closed（`insufficient_history`）
- JSON 契约 snake_case；价格 round 6 位、比例 round 4 位；比例字段为小数制（0.05 = 5%）
- 错误 HTTP 语义：校验类错误（unsupported / env_missing / busy / insufficient_history）在流开始前以 HTTP 400/409/503 返回；推理期错误以 NDJSON `error` 事件返回
- 提交一律 `git commit -s -m "..."`；后端验证 `cd backend && uv run pytest ... -q && uv run ruff check ...`；前端每次改动 `pnpm build`
- 工作区有未提交修改（extension.tsx、stock_analyzer.py 等），叠加修改前先读最新内容，不覆盖
- 子进程超时 900s；同时刻仅 1 个预测任务（flock 单飞锁）

## Review Focus

1. **盘中点击预测**：partial bar 必须按市场规则丢弃（A股 15:00 前 / crypto 当日 / 美股 ET 16:10 前），错误保留会让"未来 20 根日线"整体错位——Task 2 测试钉住
2. **KiT 环境缺失/损坏**（未 clone / venv 不存在 / ckpt 没下载）：必须返回明确的 `env_missing` 提示且不影响个股分析其他功能——Task 2/4 测试钉住
3. **子进程超时/崩溃**：900s 超时或非零退出必须 terminate 进程并回 `inference_failed`，不能挂死请求——Task 3 测试钉住
4. **index/etf 误入**：`resolve_kit_market` 返回 None，fail-closed——Task 2/4 测试钉住
5. **us/crypto 预测日期对齐**（节假日/24/7）：exchange_calendars 回退路径仅跳周末——Task 1 冒烟 + Task 8 端到端人工核对，测试覆盖不到需在 PR 注明

---

### Task 1: KiT 环境搭建脚本 + 推理 runner（KiT venv 内冒烟验证）

**Files:**
- Create: `backend/scripts/setup_kit.sh`
- Create: `backend/scripts/kit_predict_runner.py`

**Interfaces:**
- Produces（runner stdin JSON 契约，Task 3 依赖）:
  ```json
  {
    "kit_root": "<data_dir>/models/kit", "ckpt": "<kit_root>/ckpt/step_market_284M/step_market_284M.pt",
    "symbol": "600519.SH", "market": "ashare|us|crypto", "timescale": "1d",
    "bars": [{"datetime": "2025-06-02 15:00:00", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1000.0}],
    "pred_len": 20, "paths": 16, "steps": 32, "seed": 0
  }
  ```
- Produces（runner stdout，最后一行 JSON；Task 3 依赖）:
  ```json
  {"ok": true, "market": "ashare", "timescale": "1d",
   "anchor_ts": "2025-06-02 15:00:00", "prev_close": 12.34,
   "future_timestamps": ["2025-06-03 15:00:00"], 
   "ohlcv": {"open": [[...]], "high": [[...]], "low": [[...]], "close": [[...]], "volume": [[...]]},
   "instrument": {"symbol": "600519.SH", "inst_id": 123, "sector_id": 7, "matched": true},
   "meta": {"model_step": 0, "Lc": 250, "Lh": 20, "n_paths": 16, "steps": 32, "seed": 0,
            "precision": "fp32", "device": "cpu", "elapsed_s": 95.2, "calendar": "xshg|xnys|alldays|weekend_skip"}}
  ```
  失败：`{"ok": false, "error_type": "...", "error": "..."}`，exit 1。进度行写 stderr：`PROGRESS <token>`（`loading_model` / `sampling` / `calendar_fallback_weekend_skip`）。

- [ ] **Step 1: 写 `backend/scripts/setup_kit.sh`**

要点：`KIT_ROOT="${1:-data/models/kit}"`（仓库根相对路径；桌面包由用户按文档传应用数据目录）；`git clone --depth 1 https://github.com/Luciferbobo/KiT`；`uv venv "$KIT_ROOT/.venv"`；CPU torch（`--index-url https://download.pytorch.org/whl/cpu`）→ KiT `requirements.txt` → `exchange_calendars` → checkpoint 下载 `huggingface-cli download Lucifer744/KiT_model --include "step_market_284M/*" --local-dir "$KIT_ROOT/ckpt"`，`HF_ENDPOINT` 默认 `https://hf-mirror.com` 可被环境变量覆盖；每步失败即退出（`set -euo pipefail`）；clone 前若目录已存在则跳过 clone。

- [ ] **Step 2: 实际执行 setup（本机一次性环境）**

Run: `bash backend/scripts/setup_kit.sh`
Expected: `data/models/kit/{kit_v1,...}`, `.venv/bin/python -c "import torch, exchange_calendars"` 通过，`ckpt/step_market_284M/step_market_284M.pt` 与同目录 `stats.json`、`instruments.json` 存在。**checkpoint 下载若因网络失败**：继续后续任务，标注剩余风险，Task 8 前重试。
同时查看并记录 `stats.json` 顶层结构（`ashare|1d|<feature>` 键）与 `instruments.json` 中 A股/美股/crypto 条目的真实字段名（Step 3 的 lookup 依赖它）。

- [ ] **Step 3: 写 `backend/scripts/kit_predict_runner.py`**

独立脚本，只依赖 KiT venv 内的库（不 import app.*）。主流程（签名与测试不能确定的算法，照此实现）：

```python
payload = json.load(sys.stdin)
sys.path.insert(0, payload["kit_root"])
from kit_v1.infer.loader import load_model_and_config
from kit_v1.infer.window import prepare_inference_window
from kit_v1.infer.sampler import flow_sample
from kit_v1.infer.postprocess import denormalize_paths, paths_to_ohlcv

ckpt = payload["ckpt"]; ckpt_dir = Path(ckpt).parent
# PROGRESS loading_model → stderr
model, cfg = load_model_and_config(ckpt, preset="kit_l", seed=payload["seed"])
stats = json.loads((ckpt_dir / "stats.json").read_text())
Lc, Lh = cfg.data.Lc, cfg.data.Lh
half_life_days = cfg.data.vol_ema_half_life_days   # 字段名以 cfg.data 实际属性为准, 实现时核对
# instrument lookup: 按 Step 2 记录的真实结构匹配 payload["symbol"];
# 命中 → (vocab_index, sector_id, matched=True); 未命中/文件缺失 → (0, 0, False)

df = pd.DataFrame(payload["bars"]); df["datetime"] = pd.DatetimeIndex(df["datetime"])
trading_days = _build_trading_days(payload["market"], df)   # 见下
window = prepare_inference_window(df, payload["market"], payload["timescale"], Lc=Lc, Lh=Lh,
    stats=stats, sector_id=sector_id, inst_id=inst_id, trading_days=trading_days,
    half_life_days=half_life_days)
# PROGRESS sampling → stderr
gen = torch.Generator().manual_seed(payload["seed"])
paths = flow_sample(model, window["x_ctx"], window["cond"], Lh=Lh, steps=payload["steps"],
    solver="euler", n_paths=payload["paths"], generator=gen,
    device=torch.device("cpu"), precision="fp32", chunk_size=64)
feats = denormalize_paths(paths[0], payload["market"], payload["timescale"], stats)  # [K,Lh,5]
ohlcv = paths_to_ohlcv(feats, window["prev_close"], window["prev_ema"], window["half_life_bars"])
print(json.dumps({...上文字段...}, ensure_ascii=False))
```

`_build_trading_days(market, df)`：窗口起点 = df 最早 datetime 前 30 天，终点 = 锚点 + 45 天。crypto → `pd.date_range(start, end, freq="D").values`（meta.calendar=`alldays`）；其余 → `exchange_calendars.get_calendar("XSHG" if market=="ashare" else "XNYS", start=..., end=...).sessions.values`（calendar=`xshg`/`xnys`）；ImportError → 返回 None（calendar=`weekend_skip`）+ stderr PROGRESS `calendar_fallback_weekend_skip`。异常统一 except 打印 `{"ok": false, ...}` 到 stdout 后 `sys.exit(1)`。

- [ ] **Step 4: KiT venv 内冒烟（tiny 随机模型，无需 checkpoint）**

Run:
```bash
echo '{"kit_root":"data/models/kit","ckpt":"none","symbol":"600519.SH","market":"ashare","timescale":"1d","bars":[...60 根合成 OHLCV...],"pred_len":4,"paths":2,"steps":2,"seed":0}' | data/models/kit/.venv/bin/python backend/scripts/kit_predict_runner.py
```
注：ckpt="none" 时 load_model_and_config 走随机 tiny 模型分支；runner 需支持——ckpt="none" 时 `load_model_and_config("none", preset="tiny", seed)` 且跳过 stats/instruments（归一化用全 1 scale、inst_id=0），仅用于本冒烟。合成 bars 用固定公式生成（如 close=100+i*0.1）。
Expected: stdout 最后一行 `"ok": true`，ohlcv.close 形状 [2,4]，`elapsed_s` < 60s。
（真实 checkpoint 的端到端验证在 Task 8。）

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/setup_kit.sh backend/scripts/kit_predict_runner.py
git commit -s -m "feat(kit): KiT 推理 runner 与环境搭建脚本"
```

### Task 2: 服务层纯函数 — 市场映射 / partial bar 丢弃 / 环境检查 / payload 构建（TDD）

**Files:**
- Create: `backend/app/services/kit_prediction.py`（本任务只做 4 个纯函数 + `KitEnv` dataclass + 异常类）
- Test: `backend/tests/test_kit_prediction.py`

**Interfaces:**
- Produces（Task 3/4 依赖）:
  ```python
  KIT_MARKET_BY_ASSET: dict[str, str]  # {"stock": "ashare", "us": "us", "crypto": "crypto"}
  def resolve_kit_market(asset_type: str) -> str | None
  def check_kit_env(data_dir: Path) -> KitEnv          # dataclass(kit_root, python, ckpt, stats, missing: list[str]); available = not missing
  def drop_partial_last_bar(df: pl.DataFrame, asset_type: str, now: datetime | None = None) -> pl.DataFrame
  def build_runner_payload(symbol: str, market: str, df: pl.DataFrame, *, paths: int = 16,
                           steps: int = 32, seed: int = 0, kit_root: Path, ckpt: Path) -> dict
  class KitEnvError(RuntimeError)  # env_missing 用
  ```
- Consumes: `app.market_time` 的 `cn_now()` / `us_now()`；repo 的 `get_daily_asset` 返回的 enriched 日线（列 `date/open/high/low/close/volume`，date 为 pl.Date）。

- [ ] **Step 1: 写失败测试**

```python
def test_resolve_kit_market_maps_and_rejects():            # stock→ashare, us→us, crypto→crypto, index/etf→None
def test_drop_partial_last_bar_ashare_intraday():          # 构造最后 date==今天 + now=10:30 → 丢; now=16:00 → 不丢 (注入 now 参数)
def test_drop_partial_last_bar_crypto_always_drops_today() # crypto 最后一根 date==cn_today() 一律丢 (24/7 收盘在午夜)
def test_drop_partial_last_bar_us_before_close():          # us_now() 在 16:10 前 → 丢; 之后 → 不丢
def test_check_kit_env_missing_reports_all():              # 空 tmp_path → missing 含 kit_root/python/ckpt 三项; available=False
def test_check_kit_env_ok():                               # 造出 .venv/bin/python + ckpt/step_market_284M/{step_market_284M.pt, stats.json} → available=True, missing=[]
def test_build_runner_payload_stamps_close_time():         # ashare 末根 datetime 以 " 15:00:00" 结尾, us " 16:00:00", crypto " 23:59:00"; bars 数 == min(len(df), 320)
def test_build_runner_payload_row_limit():                 # 400 根输入只取最后 320 根
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_kit_prediction.py -v`
Expected: FAIL（module 不存在）

- [ ] **Step 3: 实现 4 个函数**

`drop_partial_last_bar` 丢弃规则（partial 判定基于 `now or cn_now()/us_now()`）：ashare：最后行 date == cn_today() 且北京时间 < 15:00；crypto：最后行 date == cn_today()（一律）；us：最后行 date == us_now().date() 且 ET 时间 < 16:10。`build_runner_payload` 把 date 按市场补收盘时刻（ashare `15:00:00` / us `16:00:00` / crypto `23:59:00`）转成 `datetime` 字符串行；取 `df.tail(320)`。

- [ ] **Step 4: 跑测试通过 + ruff**

Run: `cd backend && uv run pytest tests/test_kit_prediction.py -v && uv run ruff check app/services/kit_prediction.py tests/test_kit_prediction.py`
Expected: PASS, 无告警

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kit_prediction.py backend/tests/test_kit_prediction.py
git commit -s -m "feat(kit): 预测服务层纯函数 (市场映射/partial bar/环境检查/payload)"
```

### Task 3: 服务层编排 — 单飞锁 / 子进程 / 汇总 / NDJSON 流（TDD）

**Files:**
- Modify: `backend/app/services/kit_prediction.py`（追加编排部分）
- Test: `backend/tests/test_kit_prediction.py`（追加）

**Interfaces:**
- Consumes: Task 2 全部函数；Task 1 的 runner stdin/stdout 契约。
- Produces（Task 4 依赖）:
  ```python
  def summarize_prediction(result: dict, symbol: str, asset_type: str, market: str) -> dict   # 见下方 prediction 结构
  class KitBusyError(RuntimeError)
  async def predict_stream(repo, data_dir: Path, symbol: str, *, paths: int = 16, steps: int = 32,
                           seed: int = 0, timeout_s: float = 900.0) -> AsyncIterator[str]      # 逐行 yield json.dumps(str)，不含 \n
  ```
- Produces（前端契约，Task 5 依赖）— `done` 事件 prediction:
  ```json
  {"symbol": "...", "asset_type": "stock", "market": "ashare", "anchor_date": "2026-11-14",
   "prev_close": 12.34,
   "bars": [{"date": "2026-11-17", "open": .., "high": .., "low": .., "close": .., "close_p10": .., "close_p90": ..}],
   "summary": {"median_return": 0.05, "p10_return": -0.02, "p90_return": 0.12,
               "median_max_gain": 0.08, "median_max_drawdown": -0.03},
   "model": {"step": 0, "cutoff": "2025-12-31", "n_paths": 16, "steps": 32, "elapsed_s": 95.2, "calendar": "xshg"}}
  ```
  NDJSON 事件序列：`{"type":"stage","stage":"env|data|model|sampling","detail":...}` → `{"type":"done","prediction":{...}}`；错误 `{"type":"error","code":"insufficient_history|inference_failed|timeout","message":...}`。

- [ ] **Step 1: 写失败测试**（子进程用 stub runner 走真实 asyncio 子进程路径）

```python
STUB_RUNNER = "backend/tests/fixtures/kit_stub_runner.py"   # 新建: 读 stdin JSON, 打 2 行 PROGRESS 到 stderr, 输出固定合法 stdout JSON, exit 0

def test_summarize_prediction_math():        # 构造 4 路径 × 2 bar 的 ohlcv 数组, 精确断言 median/p10/p90/max_gain/max_drawdown (手算值)
def test_summarize_rounding_and_dates():     # price round 6 / ratio round 4; bars[].date 为 YYYY-MM-DD
def test_predict_stream_happy_path(monkeypatch):  # monkeypatch runner 路径与 python 为 sys.executable + stub → 事件序列 [stage:env, stage:data, ...(sampling 由 stub PROGRESS 触发), done]; prediction.summary 存在
def test_predict_stream_env_missing(tmp_path):    # 空 data_dir → NDJSON 无 done; HTTP 层行为由 Task 4 测; 这里断言 raises KitEnvError
def test_predict_stream_insufficient_history(stub_repo_50bars):  # raises/事件 error insufficient_history
def test_predict_stream_subprocess_crash(monkeypatch):  # stub runner exit 3 无输出 → error 事件 inference_failed, message 含 stderr 摘要
def test_predict_stream_timeout(monkeypatch):     # stub runner sleep 30 → timeout_s=0.5 → error 事件 timeout, 进程被 kill (proc.poll() is not None)
def test_single_flight_lock(tmp_path):            # KitPredictLock(data_dir).acquire() 后再次 acquire raises KitBusyError; release 后可再取
```

stub runner fixture 放 `backend/tests/fixtures/`，注意该目录若已有 `__init__` 约定需沿用（无则直接放文件）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_kit_prediction.py -v`
Expected: 新增用例 FAIL（函数不存在）

- [ ] **Step 3: 实现编排**

- `KitPredictLock`：flock 单文件锁（`<data_dir>/.kit_predict.lock`），POSIX `fcntl.flock` / Windows `msvcrt.locking`，仿 `mining_process_lock.py` 的 `_try_lock_file` 精简实现（约 40 行）；predict_stream 末尾 `finally: release()`
- `predict_stream` 流程：check_kit_env（失败 raise KitEnvError）→ resolve_kit_market（None raise ValueError，Task 4 转 400）→ `repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)` 取 ~450 个自然日窗口 → drop_partial_last_bar → <250 根则 yield error 事件并 return → build_runner_payload → yield stage 事件 → `asyncio.create_subprocess_exec(env.python, runner_path)`，stdin 写 payload 后关闭；stderr 逐行 pump 到 `asyncio.Queue`，主循环 `wait_for(q.get(), 1.0)` 把 `PROGRESS loading_model→stage:model`、`sampling→stage:sampling` 转成事件（循环条件：进程未退或队列非空）→ stdout 整体 parse（取含 `"ok"` 的行）→ ok=false/超时/非零退出 → yield error 事件；timeout 用 `asyncio.wait_for(proc.wait(), timeout_s)`，超时后 `proc.kill()` + `await proc.wait()`
- `summarize_prediction`：`np.median`/`np.quantile` 沿 K 轴；`median_max_gain = median(max(close[t]/prev_close - 1))`，`median_max_drawdown = median(min(close[t]/prev_close - 1))`；future_timestamps 的 date 部分为 bars[].date；cutoff 常量 `"2025-12-31"`
- runner 路径解析：`Path(__file__).resolve().parents[1] / "scripts" / "kit_predict_runner.py"`

- [ ] **Step 4: 跑全部新测试通过 + ruff**

Run: `cd backend && uv run pytest tests/test_kit_prediction.py -v && uv run ruff check app/services/kit_prediction.py tests/`
Expected: PASS, 无告警

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kit_prediction.py backend/tests/test_kit_prediction.py backend/tests/fixtures/kit_stub_runner.py
git commit -s -m "feat(kit): 预测编排 (单飞锁/子进程/汇总/NDJSON 流)"
```

### Task 4: 后端扩展路由 — `/api/kit/predict`（TDD）

**Files:**
- Create: `backend/app/custom/kit_prediction.py`
- Test: `backend/tests/test_kit_prediction_api.py`

**Interfaces:**
- Consumes: Task 3 的 `predict_stream` / `KitEnvError` / `KitBusyError`；Task 2 的 `resolve_kit_market` / `check_kit_env`。
- Produces: `POST /api/kit/predict {symbol}` → NDJSON 流（`with_heartbeat` 包装，同 `stock_analysis.py:156` 模式）；校验错误：symbol 空 400；`resolve_kit_market(repo.resolve_asset_type(symbol))` 为 None → 400 `"该资产类型不支持 KiT 预测"`；env 不可用 → 503 detail 列出缺失项；busy → 409。

- [ ] **Step 1: 写失败测试**

用 FastAPI TestClient + stub repo（`SimpleNamespace(store=SimpleNamespace(data_dir=tmp))` + `resolve_asset_type` / `get_daily_asset` 返回固定 pl.DataFrame），设 `app.state.repo`：

```python
def test_predict_unsupported_market_returns_400():        # symbol "000300.SH" (stub index) → 400, detail 含 "不支持"
def test_predict_env_missing_returns_503():               # 空 tmp data_dir → 503, detail 含缺失项
def test_predict_busy_returns_409():                      # 预先 acquire KitPredictLock → 409
def test_predict_happy_path_streams_done():               # monkeypatch predict_stream 为 3 行假事件 → 200, body 含 done 行
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_kit_prediction_api.py -v`
Expected: FAIL（路由不存在）

- [ ] **Step 3: 实现 `backend/app/custom/kit_prediction.py`**

按 `app/custom/_template.py.example`：`EXTENSION_ID = "kit.prediction"`，`setup(registrar)` 内 `APIRouter(prefix="/api/kit")` + `registrar.include_router(router)`；请求模型 `KitPredictRequest{symbol: str}`。路由 handler 内**流开始前**按序校验并转 HTTP 错误：symbol 空 → 400；`resolve_kit_market(repo.resolve_asset_type(symbol))` 为 None → 400；`check_kit_env(data_dir).available` 为 False → 503（detail 列 missing 项）；`probe` 锁被持（`KitPredictLock.acquire` 失败即释放）→ 409。全部通过后 `with_heartbeat(predict_stream(...))` + `yield chunk + "\n"`（照 `stock_analysis.py:167-176`）。扩展加载器自动发现 `app.custom` 下模块，无需注册他处。

- [ ] **Step 4: 跑测试通过 + ruff + 应用启动冒烟**

Run: `cd backend && uv run pytest tests/test_kit_prediction_api.py tests/test_kit_prediction.py -q && uv run ruff check app/custom/kit_prediction.py`
Expected: PASS。启动冒烟：`uv run python -c "from app.extensions.loader import configure_backend_extensions"` 或按现有启动测试方式确认扩展加载无路由冲突。

- [ ] **Step 5: Commit**

```bash
git add backend/app/custom/kit_prediction.py backend/tests/test_kit_prediction_api.py
git commit -s -m "feat(kit): /api/kit/predict 流式预测端点 (后端扩展注册)"
```

### Task 5: 前端 API 契约 — kitPredictStream

**Files:**
- Modify: `frontend/src/lib/api.ts`（在 `stockAnalyzeStream` 后追加，约 4616 行处）

**Interfaces:**
- Consumes: Task 4 的 NDJSON 契约。
- Produces（Task 7 依赖）:

```ts
export interface KitPredictionBar { date: string; open: number; high: number; low: number; close: number; close_p10: number; close_p90: number }
export interface KitPrediction {
  symbol: string; asset_type: string; market: string; anchor_date: string; prev_close: number
  bars: KitPredictionBar[]
  summary: { median_return: number; p10_return: number; p90_return: number; median_max_gain: number; median_max_drawdown: number }
  model: { step: number; cutoff: string; n_paths: number; steps: number; elapsed_s: number; calendar: string }
}
export interface KitPredictChunk {
  type: 'stage' | 'done' | 'error' | 'ping'
  stage?: string; detail?: string
  prediction?: KitPrediction
  code?: string; message?: string
}
// async *kitPredictStream(symbol: string) — fetch POST /api/kit/predict, 逐行 NDJSON parse;
// 复用 stockAnalyzeStream 的 reader/decoder/buf 拆行逻辑; HTTP 非 2xx 时 toast + throw (同其 4610-4616 行模式)
```

- [ ] **Step 1: 实现 `kitPredictStream` + 两个 interface**（照 stockAnalyzeStream 模式抄流解析，无独立逻辑）
- [ ] **Step 2: Run: `cd frontend && pnpm build`** — Expected: 通过
- [ ] **Step 3: Commit**: `git commit -s -m "feat(kit): 前端 kitPredictStream API 契约"`

### Task 6: AnalysisKChart 预测叠加（可选 prop，向后兼容）

**Files:**
- Modify: `frontend/src/components/stock-analysis/AnalysisKChart.tsx`

**Interfaces:**
- Consumes: Task 5 的 `KitPredictionBar`。
- Produces（Task 7 依赖）:

```ts
export interface KitForecastOverlay {
  bars: KitPredictionBar[]   // 与主图 date 类目追加对齐
}
// Props 追加: forecast?: KitForecastOverlay  (缺省 undefined → 行为与现状完全一致, PriceAlertDialog 等既有调用方零影响)
```

- [ ] **Step 1: 实现渲染**：`rows` 类目轴追加 forecast bars 的 date；追加第 4 个 series——预测蜡烛 candlestick（紫 `#8B5CF6`，涨/跌用同色不同透明度区分，与红涨绿跌主题区分），`close_p10`/`close_p90` 两条 line（浅紫半透明 areaStyle 构成分位带，stack 模式：p10 为不可见基线 + 带区域 series）。类目轴追加后 axisPointer/tooltip 对预测 bar 显示 OHLC + 区间。日期重复防御：forecast 与历史最后 date 重复时跳过该 bar。
- [ ] **Step 2: Run: `cd frontend && pnpm build`** — Expected: 通过
- [ ] **Step 3: Commit**: `git commit -s -m "feat(kit): 日K图预测蜡烛与分位带叠加"`

### Task 7: 个股分析页接线 — 按钮 / 进度 / 摘要卡 / 图表叠加

**Files:**
- Create: `frontend/src/components/stock-analysis/KitForecastCard.tsx`
- Modify: `frontend/src/custom/stock-analysis/extension.tsx`（叠加修改前先 Read 最新内容，工作区已有未提交改动）

**Interfaces:**
- Consumes: Task 5 `kitPredictStream` / `KitPrediction`；Task 6 `forecast` prop。
- Produces:

```tsx
// KitForecastCard props: { symbol: string; assetType?: string; onForecast: (p: KitPrediction | null) => void }
// 内部状态: phase 'idle'|'streaming'|'done'|'error'; stage 文案; result;
// streaming 时渲染进度 (stage→中文文案: env=检查环境 / data=加载数据 / model=加载模型 / sampling=采样中)
// 按钮在 streaming 时禁用; symbol 变化时 effect 重置 phase=idle + onForecast(null)
// done 渲染摘要: 「未来20日收益区间 p10%~p90% (中位 median%) · 最大可能涨幅 x% · 最大可能回撤 y%」
//   + 固定标注「KiT-L (284M) · 训练截至 {model.cutoff} · 仅供研究，非投资建议」 (小数→百分比 ×100 显式转换)
```

`extension.tsx`：`doAnalysis` 按钮旁加「KiT 预测」按钮（crypto/us 不禁用——三市场都支持）；看板区挂 `<KitForecastCard symbol={symbol} assetType={assetType} onForecast={setForecast} />`；`AnalysisKChart` 传 `forecast={forecast}`；index/etf 选中时按钮 title 提示不支持并禁用。

（与设计文档的偏差说明：预测结果是流式一次性产物，用组件状态持有并随 symbol 重置，**不进** TanStack Query 缓存/queryKeys——设计文档中 queryKeys 条目不适用，无需添加。）

- [ ] **Step 1: 实现 KitForecastCard**
- [ ] **Step 2: 接线 extension.tsx**（读最新内容后最小增量修改）
- [ ] **Step 3: Run: `cd frontend && pnpm build`** — Expected: 通过
- [ ] **Step 4: 手工状态检查**（`pnpm dev` + 后端：无环境→503 toast；正常→进度→完成；切换股票→卡片重置、图无残留；index/etf→按钮禁用。无 KiT 环境时可先验 503 路径）
- [ ] **Step 5: Commit**: `git commit -s -m "feat(kit): 个股分析页 KiT 预测入口与摘要卡"`

### Task 8: 端到端冒烟 + 文档 + 收尾

**Files:**
- Modify: `docs/secondary-development.md`（新增一节：KiT 预测扩展的安装/路径/env 契约，注明 `<data_dir>/models/kit` 与桌面包路径差异、AGPL 边界、checkpoint 训练截止 2025-12-31）

- [ ] **Step 1: 真实 checkpoint 端到端**

Run: 后端起服务 + 前端 dev，选一只 A 股（如 600519.SH）点「KiT 预测」。
Expected: 1~3 分钟内 done；图表出现紫色预测蜡烛与分位带；摘要卡数值合理（|median_return| < 30%）；`results` 无报错。再验一只 crypto（如 BTCUSDT）确认 24/7 日历路径。CPU 上若 sampling 超 900s（低配机）记录实际耗时。

- [ ] **Step 2: 回归验证**

Run: `cd backend && uv run pytest tests/test_kit_prediction.py tests/test_kit_prediction_api.py -q && uv run ruff check app/services/kit_prediction.py app/custom/kit_prediction.py && cd ../frontend && pnpm build && git diff --check`
Expected: 全部通过；个股分析原有功能（AI 分析/关键价位/报告）不回归——手工点一遍 AI 分析确认。

- [ ] **Step 3: 文档更新 + Commit**

```bash
git add docs/secondary-development.md
git commit -s -m "docs(kit): KiT 预测扩展安装与环境契约"
```

- [ ] **Step 4: 最终 diff 审查**：`git status` / `git diff` 逐文件确认无调试输出、无绝对路径、无 KiT 代码混入、工作区原有未提交修改未被覆盖。
