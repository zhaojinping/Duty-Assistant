"""dws CLI 子进程封装：触达（待办/消息）与回写共用。

约定：``run_dws(args) -> (returncode, stdout, stderr)``；Windows 下 .cmd/.bat 走 cmd /c。
（注：table_projection 自带早期版本，后续统一切换到本模块。）
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

_DEFAULT_TIMEOUT = 120


def resolve_dws() -> str:
    exe = shutil.which("dws")
    if not exe:
        raise RuntimeError("未找到 dws CLI（请确认 PATH 配置）")
    return exe


def _argv_for_platform(args: list[str]) -> list[str]:
    """Windows 的 cmd 会把参数里的换行当成下一条命令，子进程挂起后占住写锁。

    换行改成行分隔符再送出，钉钉仍按换行显示；超时则结束整棵进程树。
    """
    if sys.platform != "win32":
        return list(args)
    cleaned = []
    for arg in args:
        cleaned.append(arg.replace("\r\n", "\u2028").replace("\n", "\u2028").replace("\r", "\u2028"))
    return cleaned


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True)
        return
    try:
        import os
        import signal
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def run_dws(args: list[str], *, timeout: int = _DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    exe = resolve_dws()
    argv = _argv_for_platform(args)
    command = [exe, *argv]
    if exe.lower().endswith((".cmd", ".bat")):
        command = ["cmd", "/c", exe, *argv]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        detail = ((err or "") + "\ndws 超时，已结束进程").strip()
        return 124, out or "", detail
    return proc.returncode, out or "", err or ""


def run_dws_json(args: list[str], *,
                 timeout: int = _DEFAULT_TIMEOUT) -> tuple[int, dict | None, str]:
    rc, out, err = run_dws(args, timeout=timeout)
    payload = None
    if out.strip():
        try:
            payload = json.loads(out)
        except (TypeError, ValueError):
            payload = None
    return rc, payload, err
