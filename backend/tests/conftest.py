"""pytest 全局配置。

network 标记的联网测试仅在显式传入 --run-network 时运行
(pyproject.toml markers 的文档口径), 默认收集阶段直接 skip,
保证离线全量测试不被外网依赖阻塞。
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-network", action="store_true", default=False,
        help="运行 network 标记的联网测试 (默认 skip)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-network", default=False):
        return
    skip = pytest.mark.skip(reason="need --run-network option to run")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
