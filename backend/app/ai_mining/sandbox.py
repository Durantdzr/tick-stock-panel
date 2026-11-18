"""因子代码沙箱: AST 静态校验 + 受限子进程执行 + 输出契约校验。

因子代码合同（提示词同步约定）:
- 读 ``panel.parquet``（enriched 长表: symbol,date,open,high,low,close,volume,amount,turnover_rate）
- 写 ``factor_values.parquet``（symbol,date,value; date 为 ``datetime.date``）
- 只允许 ``import pandas``（含 numpy 经 pandas 生态）; 只允许 parquet 进出，
  其余 IO/进程/网络原语全部拦截。

静态层复用 ``AIStrategyGenerator._validate_safety``（pandas 入 import 白名单、
parquet IO 属性名放行）；执行层 subprocess 隔离（cwd jail + 环境白名单 + 超时杀）。
两层都不是完整沙箱，但组合后与 ai_generator 生成策略的信任级别一致。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from app.strategy.ai_generator import AIStrategyGenerator

# 因子合同必需的 parquet IO 属性名（_FORBIDDEN_ATTR_NAMES 中的定向豁免）。
# 只放行这两个名字: read_csv/excel/hdf/sql/json 与 pickle 系仍全部拒绝。
_FACTOR_CONTRACT_ALLOWED_ATTRS = frozenset({"read_parquet", "to_parquet"})

# pandas 特有的危险入口不在 ai_generator 属性禁名单（那是 polars/numpy 视角），
# 沙箱侧补一层定向拦截：反序列化 RCE 面 + 合同外数据源。
_PANDAS_DANGEROUS_ATTRS = frozenset(
    {
        "read_pickle", "to_pickle", "read_stata", "to_stata", "read_sas",
        "read_spss", "read_gbq", "to_gbq", "HDFStore", "ExcelWriter",
        "read_csv", "to_csv", "read_excel", "to_excel", "read_hdf", "to_hdf",
        "read_json", "to_json", "read_sql", "to_sql", "read_feather", "to_feather",
        "read_html", "read_xml", "read_fwf", "read_spatial",
    }
)

FACTOR_CODE_FILE = "factor.py"
PANEL_DATA_FILE = "panel.parquet"
FACTOR_VALUES_FILE = "factor_values.parquet"

# 子进程环境白名单: 最小化继承面（无代理/无凭证变量泄漏进生成代码）
_SANDBOX_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR")


class FactorSandboxError(Exception):
    """沙箱失败（校验拒绝 / 执行失败 / 输出契约不符），message 可回喂 LLM。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def validate_factor_code(code: str) -> None:
    """AST 静态校验; 不合格抛 FactorSandboxError(code="validation")。"""
    try:
        AIStrategyGenerator._validate_safety(
            code,
            extra_allowed_import_modules=frozenset({"pandas"}),
            extra_allowed_attr_names=_FACTOR_CONTRACT_ALLOWED_ATTRS,
        )
    except SyntaxError as exc:
        raise FactorSandboxError("syntax", f"Python 语法错误: {exc}") from exc
    except ValueError as exc:
        raise FactorSandboxError("validation", str(exc)) from exc
    _reject_pandas_dangerous_attrs(code)


def _reject_pandas_dangerous_attrs(code: str) -> None:
    """pandas 视角的危险成员拦截（ai_generator 名单是 polars/numpy 视角）。"""
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _PANDAS_DANGEROUS_ATTRS:
            raise FactorSandboxError(
                "validation",
                f"禁止使用 pandas.{node.attr} (因子合同只允许 parquet 进出)",
            )
        if isinstance(node, ast.ImportFrom) and (node.module or "") in {"pandas", "pandas.io"}:
            for alias in node.names:
                if alias.name in _PANDAS_DANGEROUS_ATTRS:
                    raise FactorSandboxError(
                        "validation",
                        f"禁止导入 pandas.{alias.name} (因子合同只允许 parquet 进出)",
                    )


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def error_feedback(self, limit: int = 2000) -> str:
        """可回喂 LLM 的错误摘要（超长截中段，路径脱敏交给调用方）。"""
        text = (self.stderr or self.stdout or "无输出").strip()
        if len(text) > limit:
            text = text[: limit // 2] + "\n....省略中间输出....\n" + text[-limit // 2 :]
        tag = "执行超时" if self.timed_out else f"退出码 {self.returncode}"
        return f"{tag}\n{text}"


def run_factor_script(workdir: Path, *, timeout: float = 120.0) -> SandboxResult:
    """在 workdir 里执行 factor.py（受限子进程），返回结构化结果，不抛执行异常。"""
    if not (Path(workdir) / FACTOR_CODE_FILE).is_file():
        raise FactorSandboxError("no_script", f"工作目录缺少 {FACTOR_CODE_FILE}")
    env = {key: os.environ[key] for key in _SANDBOX_ENV_KEYS if os.environ.get(key)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MPLBACKEND"] = "Agg"
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, FACTOR_CODE_FILE],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return SandboxResult(
            returncode=124,
            stdout=stdout,
            stderr=stderr or f"执行超过 {timeout}s 被终止",
            timed_out=True,
            duration_seconds=time.monotonic() - started,
        )
    return SandboxResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        timed_out=False,
        duration_seconds=time.monotonic() - started,
    )


def load_factor_values(path: Path) -> pl.DataFrame:
    """读取并校验因子值输出（symbol,date,value），fail-closed。"""
    if not Path(path).is_file():
        raise FactorSandboxError("no_output", f"未找到因子值输出 {FACTOR_VALUES_FILE}")
    try:
        df = pl.read_parquet(path)
    except Exception as exc:
        raise FactorSandboxError("bad_output", f"因子值 parquet 读取失败: {exc}") from exc
    required = ("symbol", "date", "value")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise FactorSandboxError("bad_output", f"因子值缺少必需列: {missing}，实际列: {df.columns}")
    if df.is_empty():
        raise FactorSandboxError("bad_output", "因子值面板为空")
    try:
        df = df.select(
            pl.col("symbol").cast(pl.String).alias("symbol"),
            pl.col("date").cast(pl.Date, strict=True).alias("date"),
            pl.col("value").cast(pl.Float64, strict=False).alias("value"),
        )
    except Exception as exc:
        raise FactorSandboxError("bad_output", f"因子值列类型不符（date 需可解析为日期）: {exc}") from exc
    if df["value"].null_count() == df.height:
        raise FactorSandboxError("bad_output", "因子值全为空")
    if df["symbol"].null_count() > 0:
        raise FactorSandboxError("bad_output", "symbol 列含空值")
    return df
