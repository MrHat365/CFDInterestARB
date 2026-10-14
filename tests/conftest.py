"""pytest 底座：把仓库根注入 sys.path；提供 tests 共用的 db fixture。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def db(tmp_path):
    """每个测试独立临时库，测试结束关闭（run_smoke 自行注入同名 fixture）。"""
    from mt5_trader.database import Database

    instance = Database(tmp_path / "test.db")
    yield instance
    instance.close()
