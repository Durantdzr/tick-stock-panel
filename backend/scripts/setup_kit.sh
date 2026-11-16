#!/usr/bin/env bash
# KiT (K线预测扩散模型) 一次性环境搭建。
#
# 用法: bash backend/scripts/setup_kit.sh [KIT_ROOT]
#   KIT_ROOT 默认 data/models/kit (仓库根相对路径, data/ 已 git-ignore)。
#   桌面包模式下传应用数据目录, 如 "~/Library/Application Support/TSP/models/kit"。
#
# 步骤: clone KiT -> 独立 venv (CPU torch + KiT requirements + exchange_calendars)
#       -> 下载三市场 checkpoint step_market_284M (~1.1GB)。
# KiT 为 AGPL-3.0, 代码只存在于 KIT_ROOT, 不进本仓库。
set -euo pipefail

KIT_ROOT="${1:-data/models/kit}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export HF_ENDPOINT

abs_root() { case "$1" in /*) echo "$1";; *) echo "$(pwd)/$1";; esac; }
KIT_ROOT_ABS="$(abs_root "$KIT_ROOT")"

echo "==> [1/4] clone KiT -> $KIT_ROOT_ABS"
if [ -d "$KIT_ROOT_ABS/.git" ]; then
  echo "    已存在, 跳过 clone"
else
  mkdir -p "$KIT_ROOT_ABS"
  git clone --depth 1 https://github.com/Luciferbobo/KiT "$KIT_ROOT_ABS"
fi

VENV_PY="$KIT_ROOT_ABS/.venv/bin/python"
VENV_PIP_ARGS=(--python "$VENV_PY")

echo "==> [2/4] 创建 venv 并安装依赖 (torch / KiT requirements / exchange_calendars)"
if [ -x "$VENV_PY" ]; then
  echo "    venv 已存在, 跳过创建 (安装步骤可重入)"
else
  uv venv "$KIT_ROOT_ABS/.venv" --python 3.12
fi
# torch 选型: Linux/Windows 用 CPU-only 索引; macOS x86_64 官方自 2.3 起停发轮子,
# PyPI 上最后支持 Intel Mac 的是 2.2.2 (KiT 声明 >=2.4, 但其用到的 API 均为旧特性, 冒烟验证兼容)。
HOST_ARCH="$(uname -s)-$(uname -m)"
REQS="$KIT_ROOT_ABS/requirements.txt"
if [ "$HOST_ARCH" = "Darwin-x86_64" ]; then
  # Intel Mac: PyPI 最后支持版本 2.2.2; requirements 里的 torch>=2.4 无轮子, 过滤后单独装。
  # torch 2.2.2 只兼容 numpy 1.x ABI, 必须同时钉 numpy<2。
  uv pip install "${VENV_PIP_ARGS[@]}" "torch==2.2.2" "numpy<2"
  grep -v '^torch' "$REQS" > /tmp/kit_requirements_no_torch.txt
  uv pip install "${VENV_PIP_ARGS[@]}" -r /tmp/kit_requirements_no_torch.txt "numpy<2"
else
  uv pip install "${VENV_PIP_ARGS[@]}" torch --index-url https://download.pytorch.org/whl/cpu
  uv pip install "${VENV_PIP_ARGS[@]}" -r "$REQS"
fi
uv pip install "${VENV_PIP_ARGS[@]}" exchange_calendars

echo "==> [3/4] 下载 checkpoint (step_market_284M, 三市场, 训练截至 2025-12-31)"
CKPT="$KIT_ROOT_ABS/ckpt/step_market_284M/step_market_284M.pt"
if [ -f "$CKPT" ]; then
  echo "    checkpoint 已存在, 跳过下载"
else
  # huggingface_hub CLI: 2.x 起入口为 venv/bin/hf (旧版为 huggingface-cli), 取实际存在的那个
  HF_BIN="$KIT_ROOT_ABS/.venv/bin/hf"
  [ -x "$HF_BIN" ] || HF_BIN="$KIT_ROOT_ABS/.venv/bin/huggingface-cli"
  [ -x "$HF_BIN" ] || { uv pip install "${VENV_PIP_ARGS[@]}" huggingface_hub; HF_BIN="$KIT_ROOT_ABS/.venv/bin/hf"; }
  # 显式列文件 (hf 2.x 的 --include 通配对 LFS 大文件匹配不可靠)
  "$HF_BIN" download Lucifer744/KiT_model \
    "step_market_284M/step_market_284M.pt" \
    "step_market_284M/stats.json" \
    "step_market_284M/instruments.json" \
    --local-dir "$KIT_ROOT_ABS/ckpt"
fi

echo "==> [4/4] 验证"
"$VENV_PY" -c "import torch, exchange_calendars; print('torch', torch.__version__, '| exchange_calendars OK')"
for f in "$CKPT" "$KIT_ROOT_ABS/ckpt/step_market_284M/stats.json"; do
  [ -f "$f" ] || { echo "缺少 $f (checkpoint 下载可能未完成, 可重跑本脚本)"; exit 1; }
done
echo "==> KiT 环境就绪: $KIT_ROOT_ABS"
