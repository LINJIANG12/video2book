"""按批取 video2book 派发载荷，原样打印 dispatch_prompt。

用法:
    python tools/v2b_dispatch.py transcribe 6     # 取下一批 6 个转录任务
    python tools/v2b_dispatch.py module 5         # 取下一批 5 个写作任务
    python tools/v2b_dispatch.py note 5           # 取下一批 5 篇笔记任务

滚动补位：额外开关原样透传给 queue_tracker，例如
    python tools/v2b_dispatch.py module 5 --claim              # 登记在途认领
    python tools/v2b_dispatch.py module 5 --claim --exclude BLK03
    python tools/v2b_dispatch.py note 5 --release all

课程定位（不再硬编码）:
    --pattern/-p <关键字>   指定课程工作区（子串匹配 BV 号或标题）
    $V2B_PATTERN            同上，环境变量写法
    产物根下只有一个工作区时**自动选中**，无需任何参数

产物根（与工具链同一套解析链）:
    --base-dir/-b <路径> 或 $V2B_BASE_DIR，或 $BVB_OUTPUT_DIR，
    否则按「容器根 <home>/output → cwd/output」解析。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v2b_env import ROOT, Context, split_argv  # noqa: E402

SEP = "@" * 100
KINDS = {"transcribe": "--next-transcribe", "module": "--next-module", "note": "--next-note"}


def run_tracker(ctx: Context, kind: str, count: int, extra):
    cmd = ctx.tracker_cmd(KINDS[kind], str(count), "--json", "--log-dispatch") + extra
    proc = subprocess.run(
        cmd, cwd=ROOT, env=ctx.subprocess_env, capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        print("TRACKER FAILED", proc.returncode)
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        return None
    raw = proc.stdout.strip()
    try:
        return json.loads(raw[raw.index("{"):])
    except (ValueError, json.JSONDecodeError):
        print("TRACKER 返回的不是 JSON：\n", raw[-2000:])
        return None


def main() -> int:
    positional, options = split_argv(sys.argv[1:])
    if not positional:
        print(__doc__)
        return 2
    kind = positional[0]
    if kind not in KINDS:
        print("unknown kind %r（可选: %s）" % (kind, " / ".join(KINDS)))
        return 2
    count = int(positional[1]) if len(positional) > 1 else 5
    extra = positional[2:]  # 透传 --claim / --release / --exclude 等

    ctx = Context.from_argv(sys.argv[1:])
    data = run_tracker(ctx, kind, count, extra)
    if data is None:
        return 1

    payload_key = "next_transcribe" if kind == "transcribe" else "next"
    items = data.get(payload_key) or []
    claims = data.get("claims") or {}
    active = claims.get("active") or []
    inflight = "、".join(c.get("label", "") for c in active)
    released = (claims.get("released_done") or 0) + (claims.get("released_stale") or 0)

    print("### 课程工作区：%s" % ctx.workspace.name)
    print("### 批次共 %d 项（%s，取 %d）" % (len(items), kind, count))
    if claims:
        print("### 在途认领 %d 条%s%s" % (
            len(active),
            "（%s）" % inflight if inflight else "",
            "；本次自动释放 %d 条" % released if released else "",
        ))
    if not items and inflight:
        print("### 暂无新任务：等 %s 落盘，或 --release 释放后重派" % inflight)

    for it in items:
        bid, note_id = it.get("block_id"), it.get("note_id")
        tag = "BLK%02d" % bid if bid else "NOTE%02d" % (note_id or 0)
        print(SEP)
        print("### TAG=%s" % tag)
        print(it.get("dispatch_prompt") or "(no dispatch_prompt)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())