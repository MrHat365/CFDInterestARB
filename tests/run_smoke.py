"""手动冒烟 runner：不依赖 pytest，直接 import 四个 test 模块逐个运行 test_* 函数。

用法：python tests/run_smoke.py
汇总 PASS/FAIL 计数；退出码 0 = 全过，1 = 有失败（含 import/收集错误）。

fixture 注入约定（仅覆盖本套件用到的）：
- tmp_path  -> tempfile.mkdtemp() 的 Path
- db        -> 临时路径上的 Database 实例（测试结束后 close）
"""

from __future__ import annotations

import inspect
import sys
import tempfile
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
for p in (str(REPO_ROOT), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import test_db            # noqa: E402
import test_opportunities  # noqa: E402
import test_spreads        # noqa: E402
import test_strategy       # noqa: E402

MODULES = [test_spreads, test_strategy, test_opportunities, test_db]


def _make_db() -> "test_db.Database":
    return test_db.Database(Path(tempfile.mkdtemp()) / "smoke.db")


def _resolve_args(func) -> tuple[dict, object | None]:
    """按形参名注入 fixture；返回 (kwargs, 需要清理的 db 实例或 None)。"""
    kwargs: dict = {}
    db_instance = None
    for name in inspect.signature(func).parameters:
        if name == "tmp_path":
            kwargs[name] = Path(tempfile.mkdtemp())
        elif name == "db":
            db_instance = _make_db()
            kwargs[name] = db_instance
        else:
            raise RuntimeError(f"run_smoke 不支持 fixture: {name}")
    return kwargs, db_instance


def main() -> int:
    passed: list[str] = []
    failed: list[tuple[str, str]] = []
    for module in MODULES:
        names = sorted(n for n in dir(module) if n.startswith("test_"))
        for name in names:
            func = getattr(module, name)
            if not callable(func):
                continue
            full = f"{module.__name__}.{name}"
            try:
                kwargs, db_instance = _resolve_args(func)
                try:
                    func(**kwargs)
                finally:
                    if db_instance is not None:
                        db_instance.close()
                passed.append(full)
                print(f"PASS  {full}")
            except Exception:
                failed.append((full, traceback.format_exc()))
                print(f"FAIL  {full}")

    print("-" * 60)
    print(f"total={len(passed) + len(failed)}  pass={len(passed)}  fail={len(failed)}")
    for full, tb in failed:
        print(f"\n===== {full} =====\n{tb}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
