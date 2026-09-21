"""dws CLI 子进程封装：触达（待办/消息）与回写共用。

约定：``run_dws(args) -> (returncode, stdout, stderr)``；Windows 下 .cmd/.bat 走 cmd /c。
（注：table_projection 自带早期版本，后续统一切换到本模块。）
"""

from __future__ import annotations

import json
import shutil
import subprocess

_DEFAULT_TIMEOUT = 120


def resolve_dws() -> str:
    exe = shutil.which("dws")
    if not exe:
        raise RuntimeError("未找到 dws CLI（请确认 PATH 配置）")
    return exe


def run_dws(args: list[str], *, timeout: int = _DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    exe = resolve_dws()
    command = [exe, *args]
    if exe.lower().endswith((".cmd", ".bat")):
        command = ["cmd", "/c", exe, *args]
    proc = subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


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
