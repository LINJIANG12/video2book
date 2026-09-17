"""静默子进程入口：统一抑制 Windows 控制台窗口。

背景：项目要通过 ffmpeg / ffprobe 抽音频、切片、探测时长，全部是"外部程序"，
每次调用都会新建进程。Windows 的规则是——**父进程有控制台就复用，没有控制台就给
控制台程序新开一个窗口**（`conhost.exe`）。由宿主 Agent 后台托管、并发派发多个子智能体
时，父进程没有可见控制台，于是每个 ffmpeg/ffprobe 都会闪一个黑窗：一门 84 集的课程
约 250 次进程创建，成片闪窗会打断交互。

`run_quiet()` 在 Windows 下统一带上 `CREATE_NO_WINDOW`，把这些窗口彻底消掉；
其它平台行为不变（`creationflags` 仅 Windows 有意义）。

注意：本函数只能消除**我们代码**起的进程窗口。宿主替子智能体执行 shell 命令
（`python src/cli.py …`）所产生的窗口由宿主自己决定，项目侧无法干预。
"""

import os
import subprocess
from typing import Any, Sequence, Union

# Windows 专用：不为子进程创建控制台窗口
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PathLike = Union[str, "os.PathLike[str]"]


def quiet_kwargs(**kwargs: Any) -> dict:
    """补齐"不弹窗 + 不读标准输入"的调用参数（不改动调用方显式传入的值）。"""
    if os.name == "nt" and CREATE_NO_WINDOW:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        # Windows runners default to the active ANSI code page (often cp1252).
        # Project output is UTF-8, so decoding it with the default codec aborts
        # reader threads and leaves stdout as None.
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return kwargs


def run_quiet(cmd: Sequence[PathLike], **kwargs: Any) -> "subprocess.CompletedProcess":
    """`subprocess.run` 的静默包装：默认收走标准输出/错误，Windows 下不弹控制台窗口。

    调用方仍必须显式传 `timeout=`（自检会校验），避免子进程卡死拖住主流程。
    """
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    return subprocess.run(cmd, **quiet_kwargs(**kwargs))
