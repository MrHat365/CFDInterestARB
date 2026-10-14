"""扫描本机安装的 MetaTrader 5 客户端。

三条线索（互相补充，漏斗式）：
1. 注册表 Uninstall 项（HKLM/HKCU）中 DisplayName 含 "MetaTrader" 的条目，
   从 DisplayIcon/InstallLocation 反推 terminal64.exe
   —— 注意：便携版/手动复制的终端不一定写入注册表。
2. 正在运行的 terminal64.exe 进程路径（PowerShell Get-Process 直取，零管理权限）。
3. 全盘深度 BFS 扫描 terminal64.exe（限深 5、剪枝系统目录、可取消、时间预算），
   便携/多终端主力的兜底方案。
补充： %APPDATA%\\MetaQuotes\\Terminal\\<HASH>\\origin 记录数据目录->程序目录映射。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

TERMINAL_EXE = "terminal64.exe"

# BFS 深度全程剪枝：目录名黑名单
_SKIP_DIR_NAMES = {
    "windows", "$recycle.bin", "system volume information",
    "winnt", "perflogs", "windows.old", "intel", "amd", "nvidia",
    "werc", "logs", "temp", "tmp", "recovery", "onedrivetemp",
}
_SKIP_DIR_PARTS = {  # 路径组成部分(小写)在这些之下的分支全部丢弃
    "programdata", "postgresql", "node_modules", "site-packages",
    "dotnet", "nuget", "npm-cache", "pip", "yarn", "jetbrains",
    "packages", "steam", "epic games", "warframe", "adobe",
}
_APPDATA_KEEP = {("appdata", "roaming", "metaquotes"), ("appdata", "local", "programs")}


def _reg_enumerate_uninstall() -> dict[str, str]:
    """返回 {键名: "DisplayIcon|InstallLocation"}"""
    import winreg

    found: dict[str, str] = {}
    hives = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for root, sub in hives:
        try:
            key = winreg.OpenKey(root, sub)
        except OSError:
            continue
        try:
            index = 0
            while True:
                try:
                    child = winreg.OpenKey(key, winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
                try:
                    display, _ = winreg.QueryValueEx(child, "DisplayName")
                    icon = location = ""
                    for value_name in ("DisplayIcon", "InstallLocation"):
                        try:
                            val, _ = winreg.QueryValueEx(child, value_name)
                            if value_name == "DisplayIcon":
                                icon = val
                            else:
                                location = val
                        except OSError:
                            pass
                    if "metatrader" in display.lower():
                        found.setdefault(f"{display}#{index}", f"{icon}|{location}")
                except OSError:
                    continue
        finally:
            winreg.CloseKey(key)
    return found


def _recover_exe_from_icon(icon: str) -> Path | None:
    icon = icon.strip().strip('"')
    if not icon:
        return None
    base = icon.split(",")[0]
    p = Path(base)
    if p.name.lower() == TERMINAL_EXE and p.exists():
        return p
    if p.is_dir() and (p / TERMINAL_EXE).exists():
        return p / TERMINAL_EXE
    return None


# ---------------------------------------------------------------------------
# 常见安装目录
# ---------------------------------------------------------------------------
def scan_install_dirs() -> list[str]:
    found: set[str] = set()
    candidates = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", "") and (os.environ["LOCALAPPDATA"] + r"\Programs"),
    ]
    for prog in candidates:
        if not prog:
            continue
        base = Path(prog)
        if not base.exists():
            continue
        for child in base.iterdir():
            if child.is_dir() and "metatrader" in child.name.lower():
                exe = child / TERMINAL_EXE
                if exe.exists():
                    found.add(str(exe))
    return sorted(found)


# ---------------------------------------------------------------------------
# APPDATA 数据目录
# ---------------------------------------------------------------------------
def scan_appdata_terminals() -> list[dict]:
    out: list[dict] = []
    base = Path(os.environ.get("APPDATA", "")) / "MetaQuotes" / "Terminal"
    if not base.exists():
        return out
    for child in base.iterdir():
        if not child.is_dir() or len(child.name) != 32:
            continue  # MT5 数据目录 hash 通常 32 位
        data_folder = str(child)
        exe = None
        origin = child / "origin"
        if origin.exists():
            try:
                prog_dir = Path(origin.read_text(encoding="utf-8", errors="ignore").strip())
                candidate = prog_dir / TERMINAL_EXE
                if candidate.exists():
                    exe = str(candidate)
            except OSError:
                pass
        out.append({"path": exe or "", "data_folder": data_folder})
    return out


# ---------------------------------------------------------------------------
# 正在运行的终端进程
# ---------------------------------------------------------------------------
def scan_running_processes(timeout: float = 20) -> list[str]:
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-Process terminal64 -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty Path",
            ],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    return [
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip().lower().endswith(".exe") and Path(line.strip()).exists()
    ]


# ---------------------------------------------------------------------------
# 全盘深度 BFS（生成器，可取消）
# ---------------------------------------------------------------------------
def _fixed_drives() -> list[Path]:
    import ctypes

    drives = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if mask & (1 << i):
            p = Path(f"{letter}:\\")
            if ctypes.windll.kernel32.GetDriveTypeW(str(p)) == 3:  # DRIVE_FIXED
                drives.append(p)
    return drives


def iter_deep_scan(
    max_depth: int = 5,
    budget_seconds: float = 120,
    max_dirs: int = 300_000,
    should_cancel: callable | None = None,
):
    """生成器：yield {"path": exe, "data_folder": ""}。剪枝+预算。"""
    t0 = time.time()
    visited = set()
    for drive in _fixed_drives():
        stack: list[tuple[Path, int]] = [(drive, 0)]
        while stack:
            if should_cancel and should_cancel():
                return
            if time.time() - t0 > budget_seconds or len(visited) > max_dirs:
                return
            current, depth = stack.pop()
            try:
                entries = sorted(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name.lower() == TERMINAL_EXE:
                    if entry.exists():
                        yield {"path": str(entry), "data_folder": ""}
                    continue
                if not entry.is_dir() or depth >= max_depth:
                    continue
                name = entry.name.lower()
                if name in _SKIP_DIR_NAMES or name in _SKIP_DIR_PARTS:
                    continue
                # AppData 只保留 MetaQuotes 与 Local/Programs 分支
                parts = {p.lower() for p in entry.parts}
                if "appdata" in parts:
                    tail = tuple(p.lower() for p in entry.parts[-3:])
                    if tail not in _APPDATA_KEEP:
                        continue
                if name == "program files":
                    pass  # 允许深入
                stack.append((entry, depth + 1))
                visited.add(entry)


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------
def scan(deep: bool = False, should_cancel: callable | None = None) -> list[dict]:
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(path: str, data_folder: str = "") -> None:
        path = path.strip()
        if not path:
            return
        if path.lower().endswith(".ico"):
            return
        # 存在性校验：文件已删除的路径（幽灵终端）直接丢弃，绝不入库
        if not Path(path).exists():
            return
        key = (path.lower(), data_folder)
        if key in seen:
            return
        seen.add(key)
        results.append({
            "path": path,
            "data_folder": data_folder,
            "name": Path(path).parent.name,
        })

    # 注册表
    for _, info in _reg_enumerate_uninstall().items():
        icon, location = (info.split("|", 1) + [""])[:2]
        exe = _recover_exe_from_icon(icon)
        if exe:
            _add(str(exe))
            continue
        if location:
            lpath = Path(location.strip().strip('"'))
            if (lpath / TERMINAL_EXE).exists():
                _add(str(lpath / TERMINAL_EXE))

    # 常见目录
    for exe in scan_install_dirs():
        _add(exe)

    # APPDATA 数据目录
    for item in scan_appdata_terminals():
        _add(item["path"], item["data_folder"])

    # 运行中的终端进程
    for exe in scan_running_processes():
        _add(exe)

    # 全盘深度 BFS（兜底）
    if deep:
        for item in iter_deep_scan(should_cancel=should_cancel):
            _add(item["path"], item["data_folder"])

    return results
