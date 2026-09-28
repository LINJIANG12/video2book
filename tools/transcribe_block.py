"""video2book 块级转录驱动：以子进程方式起 omni-media 的 ext MCP 服务，
调 read_media 把块音频转成逐字稿，分卷自动续读，直接落盘。

与 video2book 的转录任务书等价：prompt 原样传入、output_file 直写磁盘、全文 0 token
进本进程上下文。用法见 --help。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OMNI_REPO = Path(__file__).resolve().parent.parent / "omni-media"
STATUS_RE = re.compile(r"<!-- OMNI_STATUS: (\{.*?\}) -->")


async def 转录(音频: Path, 落盘: Path, 提示词: str, 配置: Path | None,
              端点名: str | None, 轮次上限: int,
              起点: str | None = None, 时长分: float | None = None) -> tuple[bool, str]:
    env = {
        "PYTHONPATH": str(OMNI_REPO),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    server_args = ["-m", "omni_media.server", "--mode", "ext"]
    if 配置:
        server_args += ["--config", str(配置)]

    params = StdioServerParameters(
        command=sys.executable, args=server_args, cwd=str(OMNI_REPO), env=env
    )
    args: dict = {
        "file_path": str(音频),
        "mode": "transcribe",
        "prompt": 提示词,
        "output_file": str(落盘),
    }
    if 端点名:
        args["endpoint"] = 端点名
    if 起点:
        args["start_time"] = 起点
    if 时长分 is not None:
        args["duration_minutes"] = 时长分

    async with stdio_client(params) as (读, 写):
        async with ClientSession(读, 写) as 会话:
            await 会话.initialize()
            for 轮 in range(1, 轮次上限 + 1):
                结果 = await 会话.call_tool("read_media", args)
                文本 = "\n".join(
                    getattr(b, "text", "") for b in 结果.content
                    if getattr(b, "text", None)
                )
                if getattr(结果, "isError", None) or getattr(结果, "is_error", False):
                    return False, 文本[:3000]
                匹配 = STATUS_RE.search(文本)
                状态 = json.loads(匹配.group(1)) if 匹配 else {}
                if 轮 > 1 or not 状态.get("is_finished", True):
                    print(f"  [第{轮}卷] 累计 {状态.get('chars_written', 0):,} 字符 "
                          f"耗时 {状态.get('elapsed_sec', 0):.0f}s", flush=True)
                if 状态.get("is_finished", True):
                    return True, f"完成，共 {状态.get('chars_written', 0):,} 字符"
                下一 = 状态.get("next_start_time")
                if not 下一:
                    return True, f"无续读游标，共 {状态.get('chars_written', 0):,} 字符"
                args["start_time"] = 下一
                if 状态.get("next_duration_minutes") is not None:
                    args["duration_minutes"] = 状态["next_duration_minutes"]
    return False, f"超过 {轮次上限} 卷上限仍未读完"


def main() -> int:
    ap = argparse.ArgumentParser(description="video2book 块级转录驱动（omni-media ext 通道）")
    ap.add_argument("audio", help="块音频绝对路径")
    ap.add_argument("output", help="逐字稿落盘绝对路径")
    ap.add_argument("--prompt-file", default=str(Path(__file__).with_name("transcribe_prompt.txt")))
    ap.add_argument("--config", default=None, help="omni-media config.json 路径")
    ap.add_argument("--endpoint", default=None, help="配置里的端点名（缺省用 active）")
    ap.add_argument("--max-volumes", type=int, default=20)
    ap.add_argument("--start", default=None, help="起点时间戳，如 00:46:33（整集切成多块时按块边界取）")
    ap.add_argument("--duration", type=float, default=None, help="本次切片分钟数")
    a = ap.parse_args()

    audio, out = Path(a.audio), Path(a.output)
    if not audio.is_file():
        print(f"音频不存在: {audio}", file=sys.stderr)
        return 2
    prompt = Path(a.prompt_file).read_text(encoding="utf-8").strip()
    out.parent.mkdir(parents=True, exist_ok=True)

    ok, msg = asyncio.run(
        转录(audio, out, prompt, Path(a.config) if a.config else None, a.endpoint,
             a.max_volumes, a.start, a.duration)
    )
    size = out.stat().st_size if out.exists() else 0
    if ok and size > 0:
        print(f"OK | {out} | {size} 字节 | {msg}")
        return 0
    print(f"FAIL | {out} | {size} 字节 | {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
