"""批量块级转录：扫描工作区里「有块音频但还没有逐字稿」的块，并发调 read_media 补齐。

与逐个跑 tools/transcribe_block.py 等价，只是并发批量执行；单块失败不中断整体。
用法：py -3 tools/transcribe_queue.py <工作区路径> [--concurrency 5] [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcribe_block import 转录  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "omni-media"
PROMPT = (Path(__file__).with_name("transcribe_prompt.txt")).read_text(encoding="utf-8").strip()


def 收集待办(工作区: Path) -> list[tuple[int, Path, Path]]:
    """返回 [(块号, 块音频, 逐字稿目标)]，按块号排序。

    目标文件名必须与工具期望的完全一致：`BLK{block_id:02d}_{span}_逐字稿.md`
    （span 取自 block_plan.json，跨集块形如 P01-P02、拆分块形如 P07上）。
    queue_tracker 按这个名字找逐字稿，命名不一致会导致该块被写作队列跳过。
    """
    import json

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "video2book"))
    from src.core.block_plan import BlockPlan

    块表 = {
        int(b["block_id"]): b
        for b in json.loads((工作区 / "block_plan.json").read_text(encoding="utf-8"))["blocks"]
    }
    音频目录 = 工作区 / "audio" / "blocks"
    字幕目录 = 工作区 / "subtitles"
    待办 = []
    if not 音频目录.is_dir():
        return 待办
    for 音频 in sorted(音频目录.glob("BLK*_*.m4a")):
        匹配 = re.match(r"BLK(\d+)_", 音频.name)
        if not 匹配:
            continue
        块号 = int(匹配.group(1))
        if 块号 not in 块表:
            continue
        目标 = 字幕目录 / f"BLK{块号:02d}_{BlockPlan.span(块表[块号])}_逐字稿.md"
        if 目标.exists() and 目标.stat().st_size > 0:
            continue
        待办.append((块号, 音频, 目标))
    return sorted(待办, key=lambda x: x[0])


def 跑一块(块号: int, 音频: Path, 目标: Path) -> tuple[int, bool, str]:
    开始 = time.time()
    try:
        ok, msg = asyncio.run(转录(音频, 目标, PROMPT, None, None, 20))
    except Exception as exc:  # 单块失败不拖垮整批
        return 块号, False, f"{type(exc).__name__}: {exc}"
    大小 = 目标.stat().st_size if 目标.exists() else 0
    耗时 = time.time() - 开始
    return 块号, (ok and 大小 > 0), f"{大小:,} 字节 / {耗时:.0f}s / {msg}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0, help="0 = 全部")
    a = ap.parse_args()

    工作区 = Path(a.workspace).expanduser().resolve()  # read_media 只接受绝对路径
    待办 = 收集待办(工作区)
    if a.limit:
        待办 = 待办[: a.limit]
    print(f"工作区: {工作区.name}")
    print(f"待转录块: {len(待办)}  并发: {a.concurrency}", flush=True)
    if not 待办:
        return 0

    完成, 失败 = [], []
    开始 = time.time()
    with ThreadPoolExecutor(max_workers=a.concurrency) as 池:
        未来 = {池.submit(跑一块, n, a_, t): n for n, a_, t in 待办}
        for i, fut in enumerate(as_completed(未来), 1):
            块号, ok, msg = fut.result()
            (完成 if ok else 失败).append(块号)
            标记 = "OK " if ok else "FAIL"
            print(f"[{i}/{len(待办)}] {标记} BLK{块号:03d} | {msg}", flush=True)

    总耗时 = time.time() - 开始
    print(f"\n完成 {len(完成)} 块，失败 {len(失败)} 块，总耗时 {总耗时/60:.1f} 分钟")
    if 失败:
        print(f"失败块号: {sorted(失败)}")
    return 0 if not 失败 else 1


if __name__ == "__main__":
    sys.exit(main())
