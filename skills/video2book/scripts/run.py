#!/usr/bin/env python3
"""Codex / Agent 运行辅助入口脚本。

支持在任意工作目录下调用当前项目的 CLI 入口。
"""

import sys
from pathlib import Path

# 将项目根目录注入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.console import enable_utf8_console  # noqa: E402
from src.cli import main  # noqa: E402

# 控制台硬化：与 src/cli.py 入口保持一致（管道捕获时不再因非 ASCII 符号编码失败而崩溃）
enable_utf8_console()

if __name__ == "__main__":
    main()
