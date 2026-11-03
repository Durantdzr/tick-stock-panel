"""Baostock(www.baostock.com)免费 A 股数据 provider。

方法签名对齐 custom.GenericHTTPProvider, 注入 custom loader 注册表后,
各 service 无需改动即可路由到本 provider。匿名登录, 无需 API Key。

实现数据集:
  - adj_factor  除权因子: query_adjust_factor 全历史累积链 → 换算单事件比值
                (项目契约: ex_factor = 事件前/事件后价格连续性比, 见下「口径」)
  - financial   财务五表中的两表:
                  metrics  盈利/成长/偿债/营运/现金流五类季度指标(合并一行/期)
                  shares   按报告期+公告日的总股本/流通股(历史换手率用)
                income/balance_sheet/cash_flow 无科目级数据, 返回空表由上游保留存量。

单位与口径 (CONTRIBUTING §3.1/§3.4, 2026-11 实测校准):
  - ex_factor 换算: baostock foreAdjustFactor 为涨跌幅复权累积因子
    (前复权 = 原始价 × foreAdjustFactor, 与自家 qfq K 线逐日自洽)。
    项目单事件比值 = 事件后累积 / 事件前累积, 即
      ex_factor_t = fore_t / fore_{t-1}   (首个事件以 1.0 为基)
    实测茅台 2024-12-20/2025-06-26/2025-12-19 三事件比值 1.015637/1.019600/
    1.017028 与 qfq/raw 逐日比完全吻合。
    口径注意: 涨跌幅复权法不含分红再投资收益成分(fuyao 为交易所除权参考价
    公式推导), 用于均线/动量类指标一致, 分红收益率类因子可能有微小差异。
    因子直接来自实测价格连续性, 无需 fuyao 式涨跌停自检。
  - 财务指标为小数制 (npMargin 0.525641 = 52.56%), 原样透传;
    pubDate/statDate 分别映射 announce_date/period_end (§3.4 防未来函数)。
  - 股本单位为股, 与 instruments float_shares 同口径。
  - Baostock 仅覆盖沪深 (无北交所), 代码格式 sh.600519 / sz.000001。

并发: baostock SDK 为全局单连接且非线程安全, 全部查询经模块级锁串行化;
登录懒建立, close() 时登出。数据盘后更新 (一般 17:30-19:00), 当日财务数据
以公告实际披露为准, 不伪造。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import polars as pl

from app.data_providers.base import ProviderRequestError

logger = logging.getLogger(__name__)

_DATASETS = ("adj_factor", "financial")

_ADJ_INTERVAL_S = 0.05  # 单标的查询间隔(baostock 服务端宽松, 保守节流)
_FIN_INTERVAL_S = 0.05
_FULL_HISTORY_START = "1990-01-01"  # 因子链必须从上市起点取, 首事件才有前驱

# baostock 季度指标 → 项目 canonical 列名 (对齐 fuyao._METRICS_FIELD_MAP 既有命名)
# 值为小数制, 原样透传
_METRICS_FIELD_MAP: dict[str, dict[str, str]] = {
    "profit": {  # query_profit_data (含 shares 所需的股本列)
        "roeAvg": "roe",
        "npMargin": "net_margin",
        "gpMargin": "gross_margin",
        "netProfit": "net_income",
        "epsTTM": "eps_ttm",
        "MBRevenue": "main_business_revenue",
    },
    "growth": {  # query_growth_data
        "YOYNI": "net_income_yoy",
        "YOYEPSBasic": "eps_yoy",
        "YOYAsset": "total_asset_yoy",
        "YOYEquity": "equity_yoy",
    },
    "balance": {  # query_balance_data
        "liabilityToAsset": "debt_to_asset_ratio",
        "currentRatio": "current_ratio",
        "quickRatio": "quick_ratio",
        "cashRatio": "cash_ratio",
        "assetToEquity": "equity_multiplier",
    },
    "cash_flow": {  # query_cash_flow_data
        "CFOToOR": "operating_cash_to_revenue",
        "CFOToNP": "operating_cash_to_net_income",
    },
    "operation": {  # query_operation_data
        "NRTurnRatio": "receivable_turnover",
        "INVTurnRatio": "inventory_turnover",
        "CATurnRatio": "current_asset_turnover",
        "AssayTurnRatio": "total_asset_turnover",
    },
}

_STATEMENT_TABLES = ("income", "balance_sheet", "cash_flow")  # baostock 无科目级数据
_METRICS_QUARTERS_FULL = 8  # 对齐 fuyao 财务首装全量历史 (最近 8 期季报)
_SHARES_QUARTERS_FULL = 12  # 股本历史适当放长 (§3.4 历史换手率)

_lock = threading.Lock()
_logged_in = False


def availability() -> tuple[bool, str]:
    """loader 启动自检: 依赖可导入 + 登录探测。不抛异常。"""
    try:
        import baostock as bs
    except ImportError:
        return False, "缺少 baostock 依赖(后端默认依赖已含, 请 uv sync 后重启)"
    try:
        with _lock:
            lg = bs.login()
            if lg.error_code != "0":
                return False, f"登录失败: {lg.error_msg}"
            bs.logout()
    except Exception as e:  # noqa: BLE001
        return False, f"登录探测失败(检查网络): {e}"
    return True, "ok"


@dataclass
class _BaostockConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "baostock"
    display_name: str = "Baostock"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _baostock_code(symbol: str) -> str | None:
    """600519.SH → sh.600519。北交所与无法识别的后缀返回 None。"""
    if "." not in symbol:
        return None
    code, suffix = symbol.split(".", 1)
    suffix = suffix.upper()
    if suffix in ("SH", "SZ") and code.isalnum():
        return f"{suffix.lower()}.{code}"
    return None


def _ensure_login() -> None:
    """在持锁状态下确保登录会话有效。调用方必须已持有 _lock。"""
    global _logged_in
    import baostock as bs

    if _logged_in:
        return
    lg = bs.login()
    if lg.error_code != "0":
        raise ProviderRequestError(f"baostock 登录失败: {lg.error_msg}")
    _logged_in = True


def _query_rows(rs) -> list[list[str]]:
    """消费一个 ResultData 为行列表; 错误码非 0 抛 ProviderRequestError。"""
    if rs.error_code != "0":
        raise ProviderRequestError(f"baostock 查询失败: {rs.error_msg}")
    rows: list[list[str]] = []
    while rs.next():
        rows.append(rs.get_row_data())
    return rows


def _query_dicts(rs) -> list[dict[str, str]]:
    """消费一个 ResultData 为 dict 行列表 (fields 与行值按序拼合)。"""
    rows = _query_rows(rs)
    return [dict(zip(rs.fields, row, strict=False)) for row in rows]


def cumulative_to_event_factors(cum_factors: list[float]) -> list[float]:
    """涨跌幅复权累积因子链 → 项目单事件比值序列。

    ex_factor_t = cum_t / cum_{t-1}; 首个事件以 1.0 为基 (上市时无除权)。
    2026-11 实测: 茅台累积链 [7.113689, 7.224927, 7.366525] →
    [7.113689, 1.015637, 1.019600], 与 qfq/raw 逐日比完全吻合。
    """
    out: list[float] = []
    prev = 1.0
    for cum in cum_factors:
        if cum <= 0:
            raise ProviderRequestError(f"baostock 累积因子非法: {cum}")
        out.append(cum / prev)
        prev = cum
    return out


class BaostockProvider:
    """Baostock 数据源。adj_factor = 单事件除权因子; financial = metrics + shares。"""

    name = "baostock"
    builtin = True

    def __init__(self) -> None:
        self.config = _BaostockConfig()

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        global _logged_in
        with contextlib.suppress(Exception):
            import baostock as bs

            with _lock:
                if _logged_in:
                    bs.logout()
                    _logged_in = False

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time=None,
        end_time=None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """单事件除权因子 → 内部契约 (symbol/trade_date/ex_factor)。

        因子链必须自上市首事件取全 (_FULL_HISTORY_START), 否则窗口内首个
        事件没有前驱累积值无法换算; 之后按请求窗口过滤。
        逐标的节流; 单标的失败记日志跳过 (不冒充完整), 全部失败才抛错。
        """
        import baostock as bs

        if not symbols or asset_type != "stock":
            return pl.DataFrame()
        end_d = (end_time or datetime.now()).strftime("%Y-%m-%d")
        start_d = start_time.strftime("%Y-%m-%d") if start_time else None

        out_rows: list[dict] = []
        failed = 0
        with _lock:
            _ensure_login()
            for i, symbol in enumerate(symbols):
                if i:
                    time.sleep(_ADJ_INTERVAL_S)
                code = _baostock_code(symbol)
                if code is None:
                    logger.warning("baostock 因子: 跳过不支持的标的 %s (仅沪深)", symbol)
                    failed += 1
                    continue
                try:
                    rows = _query_rows(bs.query_adjust_factor(
                        code=code, start_date=_FULL_HISTORY_START, end_date=end_d,
                    ))
                except ProviderRequestError as e:
                    logger.warning("baostock 因子拉取失败 %s: %s", symbol, e)
                    failed += 1
                    continue
                dates = [r[1] for r in rows]
                cums = [_to_float(r[2]) for r in rows]
                valid = [(d, c) for d, c in zip(dates, cums, strict=True) if d and c is not None]
                try:
                    events = cumulative_to_event_factors([c for _, c in valid])
                except ProviderRequestError as e:
                    logger.warning("baostock 因子换算失败 %s: %s", symbol, e)
                    failed += 1
                    continue
                for (d, _), ex in zip(valid, events, strict=True):
                    if start_d and d < start_d:
                        continue
                    out_rows.append({
                        "symbol": symbol,
                        "trade_date": datetime.strptime(d, "%Y-%m-%d").date(),
                        "ex_factor": ex,
                    })
                if on_chunk_done:
                    on_chunk_done(i + 1, len(symbols))

        if failed and failed >= len(symbols):
            raise ProviderRequestError(f"baostock 除权因子全部拉取失败 ({failed} 只)")
        if failed:
            logger.warning("baostock 除权因子: %d 只标的失败被跳过", failed)
        if not out_rows:
            return pl.DataFrame(schema={
                "symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64,
            })
        return (
            pl.DataFrame(out_rows)
            .unique(subset=["symbol", "trade_date"], keep="last")
            .sort(["symbol", "trade_date"])
        )

    # ---- financial ----
    def get_financials(self, table: str, symbols: list[str], latest_only: bool = True) -> pl.DataFrame:
        """metrics (五类季度指标) / shares (历史股本); 三大报表返回空表。

        latest_only=True 只取最近 1 期; False 取最近 N 期
        (metrics 8 期 / shares 12 期), 全市场首轮较慢 (逐标的串行)。
        """
        if not symbols:
            return pl.DataFrame()
        if table in _STATEMENT_TABLES:
            logger.info("baostock 无 %s 科目级数据, 返回空表(存量保留)", table)
            return pl.DataFrame()
        if table == "shares":
            return self._financial_shares(symbols, latest_only)
        if table == "metrics":
            return self._financial_metrics(symbols, latest_only)
        logger.warning("baostock: 未知财务表 %s", table)
        return pl.DataFrame()

    def _recent_quarters(self, count: int) -> list[tuple[int, int]]:
        """最近 N 个 (year, quarter), 自当前季度倒推 (含未来季, 查询自然为空)。"""
        today = datetime.now()
        out: list[tuple[int, int]] = []
        year, quarter = today.year, (today.month - 1) // 3 + 1
        while len(out) < count:
            out.append((year, quarter))
            quarter -= 1
            if quarter == 0:
                year -= 1
                quarter = 4
        return out

    def _financial_metrics(self, symbols: list[str], latest_only: bool) -> pl.DataFrame:
        import baostock as bs

        # latest_only: 自当前季度向前回溯, 只取最近一个已披露季度 (当前季度的
        # 报表通常尚未披露, 直接只查当前季会误判为无数据); 否则取最近 8 期。
        quarters = self._recent_quarters(_METRICS_QUARTERS_FULL)
        rows: list[dict] = []
        failed = 0
        with _lock:
            _ensure_login()
            for i, symbol in enumerate(symbols):
                if i:
                    time.sleep(_FIN_INTERVAL_S)
                code = _baostock_code(symbol)
                if code is None:
                    failed += 1
                    continue
                got_any = False
                for year, quarter in quarters:
                    try:
                        profit_rows = _query_dicts(
                            bs.query_profit_data(code=code, year=year, quarter=quarter)
                        )
                    except ProviderRequestError as e:
                        logger.warning("baostock 财务 %s %dQ%d 失败: %s", symbol, year, quarter, e)
                        continue
                    if not profit_rows:
                        if latest_only and got_any is False:
                            # latest 语义: 跳过未披露季度, 继续向前找最近已披露期
                            continue
                        continue
                    got_any = True
                    profit = profit_rows[0]
                    row: dict[str, Any] = {
                        "symbol": symbol,
                        "period_end": profit.get("statDate"),
                        "announce_date": profit.get("pubDate"),
                    }
                    for src, dst in _METRICS_FIELD_MAP["profit"].items():
                        row[dst] = _to_float(profit.get(src))
                    for iface, mapping in _METRICS_FIELD_MAP.items():
                        if iface == "profit":
                            continue
                        fn = getattr(bs, f"query_{iface}_data")
                        try:
                            data = _query_dicts(fn(code=code, year=year, quarter=quarter))
                        except ProviderRequestError as e:
                            logger.warning("baostock %s %s %dQ%d 失败: %s", iface, symbol, year, quarter, e)
                            continue
                        if not data:
                            continue
                        for src, dst in mapping.items():
                            row[dst] = _to_float(data[0].get(src))
                    rows.append(row)
                    if latest_only:
                        break  # 只要最近已披露一期
                if not got_any:
                    failed += 1
        if failed and failed >= len(symbols) and not rows:
            raise ProviderRequestError(f"baostock 财务指标全部拉取失败 ({failed} 只)")
        if failed:
            logger.warning("baostock 财务指标: %d 只标的无数据/失败", failed)
        return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    def _financial_shares(self, symbols: list[str], latest_only: bool) -> pl.DataFrame:
        """历史股本: query_profit_data 的 totalShare/liqaShare, 按 pubDate 公告可用。

        latest_only 只取最近已披露一期; 否则取最近 12 期。
        """
        import baostock as bs

        quarters = self._recent_quarters(_SHARES_QUARTERS_FULL)
        rows: list[dict] = []
        with _lock:
            _ensure_login()
            for i, symbol in enumerate(symbols):
                if i:
                    time.sleep(_FIN_INTERVAL_S)
                code = _baostock_code(symbol)
                if code is None:
                    continue
                for year, quarter in quarters:
                    try:
                        profit_rows = _query_dicts(
                            bs.query_profit_data(code=code, year=year, quarter=quarter)
                        )
                    except ProviderRequestError as e:
                        logger.warning("baostock 股本 %s %dQ%d 失败: %s", symbol, year, quarter, e)
                        continue
                    if not profit_rows:
                        continue
                    profit = profit_rows[0]
                    total, liqa = _to_float(profit.get("totalShare")), _to_float(profit.get("liqaShare"))
                    if liqa is None and total is None:
                        continue
                    rows.append({
                        "symbol": symbol,
                        "period_end": profit.get("statDate"),
                        "announce_date": profit.get("pubDate"),
                        "total_shares": total,
                        "float_shares": liqa,
                    })
                    if latest_only:
                        break  # 只要最近已披露一期
        return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset not in _DATASETS:
            return {
                "provider": self.name, "dataset": dataset, "rows": 0,
                "error": f"baostock 未接入 {dataset} 数据集(自动回退 TickFlow)",
            }
        use = [s for s in (symbols or []) if s][:3] or ["600519.SH"]
        try:
            if dataset == "adj_factor":
                df = self.get_adj_factors(use, None, None, "stock")
            else:
                df = self.get_financials("metrics", use, latest_only=True)
        except ProviderRequestError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        head = df.head(5).to_dicts()
        for row in head:
            for k, v in list(row.items()):
                if isinstance(v, date):
                    row[k] = v.isoformat()
        return {
            "provider": self.name, "dataset": dataset,
            "rows": df.height, "columns": list(df.columns),
            "preview": head,
        }
