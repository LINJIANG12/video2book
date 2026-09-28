"""本进程控制台硬化：把 stdout/stderr 固定为 UTF-8，避免管道下编码崩溃。

背景（实测）：本项目大量 `print` 输出含非 ASCII 符号（`[✓]`、`──►`、`•`、`…`、`→`）。
Python 在输出目标是**真实控制台**时按 UTF-8 编码（Windows 走 PEP 528 的控制台写入），
但输出被**重定向或管道捕获**时改按 `locale.getpreferredencoding()`——中文 Windows 是
cp936/GBK，而 `✓`(U+2713) / `▶`(U+25B6) 不在 GBK 码表里，于是 `print` 抛
`UnicodeEncodeError`，命令以非零码收场。宿主 Agent 捕获子进程输出、CI 记录日志，
恰恰都是管道场景，所以这条路径必须堵上。

`errors="replace"` 是第二道保险：即使某个字符在任何编码下都不可表示，也只降级成 `?`，
不会让整条命令失败。
"""

import os
import sys
from typing import Any


def enable_utf8_console() -> None:
    """把本进程的 stdout/stderr 固定为 UTF-8（幂等，可重复调用）。

    分两种情况，取舍不同：

    - **未显式设置 `PYTHONIOENCODING`**（绝大多数场景，含宿主 Agent 与 CI）：
      编码固定为 UTF-8 并逐行刷新——这是修复点本身。
    - **显式设置了 `PYTHONIOENCODING`**：**保留使用者选定的编码**（不强行改写），
      只补上 `errors="replace"` 这道保险。这样既尊重显式意图，又保证命令不会因为
      个别符号编码失败而崩掉：中文仍可读，只有 `✓` 这类生僻符号降级成 `?`。

    `reconfigure` 自 Python 3.7 可用，在本项目的最低版本 3.10 上始终可用。任何失败
    （流不可重配置、被替换成非文本对象等）都静默跳过——控制台设置失败不应成为
    命令失败的又一个理由。
    """
    user_encoding = os.environ.get("PYTHONIOENCODING", "").strip()

    for stream in (sys.stdout, sys.stderr):
        reconfigure: Any = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if user_encoding:
                # 编码归使用者，容错归我们：宁可个别符号降级为 `?`，也不许命令崩。
                # 缓冲策略仍由本项目决定：设了 PYTHONIOENCODING 不该顺带把行缓冲关掉，
                # 否则长跑命令在管道下退回块缓冲，宿主读不到中间进度。
                reconfigure(errors="replace", line_buffering=True)
            else:
                # line_buffering 与拆分前 cli.py 的行为一致：逐行 flush，便于宿主实时读到进度。
                reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            continue
