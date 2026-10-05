"""按块号原样重建 queue_tracker 的 dispatch_prompt，供批量派发使用。

用法:
    python tools/v2b_prompt.py 1 5 9-12             # 转录类提示词 → <产物根>/_dispatch_prompts/
    python tools/v2b_prompt.py --module 1-53        # 模块长文类 → 同目录，文件名带 _M 后缀
    python tools/v2b_prompt.py --module 1-53 --out D:/tmp/prompts
    python tools/v2b_prompt.py --verify             # 与 queue_tracker 的真实载荷逐字节比对
    python tools/v2b_prompt.py --verify-module

派发稿默认落在**产物根**下的 `_dispatch_prompts/`（下划线开头，不会被工作区枚举当成课程），
用 `--out` 可改到别处；不会写进代码域。

课程定位与产物根：同 v2b_dispatch.py（--pattern/-p、$V2B_PATTERN、--base-dir/-b、$V2B_BASE_DIR），
产物根下只有一个工作区时自动选中，不含任何硬编码课程或路径。

自检：`--verify` 会把本脚本生成的文本与 queue_tracker 返回的 dispatch_prompt
逐字节比对——模板一旦与工具链漂移，这里会报 MISMATCH。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v2b_env import ROOT, Context, split_argv  # noqa: E402

TRANSCRIBE_TEMPLATE = (
    "【执行规范（单任务直达）】：本任务输入与输出路径均已完全指定。直接读取指定输入文件，"
    "完成转录并保存到目标路径；无需也不要检索、扫描项目其他文件或仓库代码。\n"
    "\n"
    "请阅读转录任务书文件：\n"
    "`{ws}/subtitles/{tag}_{span}_转录任务书.md`\n"
    "调用听音工具转录（**优先 read_media**：它能 output_file 直写落盘、全文 0 Token 进上下文；"
    "工具列表里没有 read_media 时才用 read_audio 的 output_mode=\"file\" 取切片自行聆听），"
    "严格按照任务书 2.1 节的要求进行纯文本忠实转录（无需时间戳），**首行写来源抬头** "
    "`> 来源：**听音转录**——非平台字幕，由讲师原声转录`（下游据它拒绝字幕稿降级覆盖），"
    "将完整逐字稿直接写入目标文件：\n"
    "`{ws}/subtitles/{tag}_{span}_逐字稿.md`\n"
    "落盘后仅在最后汇报单行：\n"
    "{tag} | {ws}/subtitles/{tag}_{span}_逐字稿.md | 字节数 | 执行者\n"
    "（严禁在对话中回传逐字稿正文）"
)

MODULE_TEMPLATE = (
    "【执行规范（单任务直达）】：本任务输入与输出路径均已完全指定。直接读取指定输入文件，"
    "完成撰写并保存到目标路径；无需也不要检索、扫描项目其他文件或仓库代码。\n"
    "\n"
    "请阅读模块长文任务书文件：\n"
    "`{ws}/articles/模块{nn}_{title}_TASK.md`\n"
    "以任务书指定的块级逐字稿（`{ws}/subtitles/{tag}_{span}_逐字稿.md`）为唯一事实来源，"
    "严格遵循任务书内嵌的撰写规范与 Typora 渲染硬要求（标题严禁手写数字序号，字符画必须进围栏），"
    "撰写深度模块精读长文，直接写入目标路径：\n"
    "`{ws}/articles/模块{nn}_{title}_精读长文.md`\n"
    "落盘后仅在最后汇报单行：\n"
    "BLK{nn} | {ws}/articles/模块{nn}_{title}_精读长文.md | 字节数 | 执行者\n"
    "（严禁在对话中回传长文正文）"
)



def load_plan(ws: Path) -> dict:
    with open(ws / "block_plan.json", encoding="utf-8") as f:
        return json.load(f)


def plan_index(ws: Path):
    """返回 (block_id -> span, block_id -> title)。缺 span 时退回 title，与工具链同口径。"""
    plan = load_plan(ws)
    spans, titles = {}, {}
    for b in plan["blocks"]:
        spans[b["block_id"]] = b.get("span") or b.get("title", "")
        titles[b["block_id"]] = b.get("title", "")
    return spans, titles


def build_transcribe(block_id: int, ws: Path, spans: dict) -> str:
    return TRANSCRIBE_TEMPLATE.format(ws=ws, tag="BLK%02d" % block_id, span=spans[block_id])


def build_module(block_id: int, ws: Path, spans: dict, titles: dict) -> str:
    nn = "%02d" % block_id
    return MODULE_TEMPLATE.format(
        ws=ws, nn=nn, title=titles[block_id], tag="BLK" + nn, span=spans[block_id]
    )


def _fetch(ctx: Context, flag: str, n: int = 12):
    cmd = ctx.tracker_cmd(flag, str(n), "--json")
    proc = subprocess.run(
        cmd, cwd=ROOT, env=ctx.subprocess_env, capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        print("TRACKER FAILED", proc.returncode, proc.stderr[-1500:])
        return []
    raw = proc.stdout.strip()
    data = json.loads(raw[raw.index("{"):])
    return data.get("next_transcribe" if flag == "--next-transcribe" else "next") or []


def _compare(mine: str, real: str, label: str) -> bool:
    if mine == real:
        return True
    print("MISMATCH %s" % label)
    for a, b in zip(mine.splitlines(), real.splitlines()):
        if a != b:
            print("  mine:", a)
            print("  real:", b)
    return False


def verify(ctx: Context, module: bool) -> int:
    ws = ctx.workspace
    spans, titles = plan_index(ws)
    flag = "--next-module" if module else "--next-transcribe"
    items = _fetch(ctx, flag)
    ok = bad = 0
    for it in items:
        bid = it["block_id"]
        mine = build_module(bid, ws, spans, titles) if module else build_transcribe(bid, ws, spans)
        if _compare(mine, it["dispatch_prompt"], "BLK%02d" % bid):
            ok += 1
        else:
            bad += 1
    if not items:
        print("queue_tracker 没有返回待派发项（可能该环节已跑完），无法比对。")
    print("%s: ok=%d mismatch=%d" % ("verify_module" if module else "verify", ok, bad))
    return 0 if bad == 0 else 1


def expand(tokens):
    for tok in tokens:
        if "-" in tok:
            lo, hi = (int(x) for x in tok.split("-", 1))
            yield from range(lo, hi + 1)
        else:
            yield int(tok)


def main() -> int:
    positional, options = split_argv(sys.argv[1:])
    flags = {a for a in sys.argv[1:] if a.startswith("--") and a in ("--verify", "--verify-module", "--module")}
    ctx = Context.from_argv(sys.argv[1:])
    ws = ctx.workspace

    if "--verify" in flags:
        return verify(ctx, module=False)
    if "--verify-module" in flags:
        return verify(ctx, module=True)

    ids = [int(t) for t in positional if not t.startswith("--")]
    if not ids:
        print(__doc__)
        return 2

    spans, titles = plan_index(ws)
    module = "--module" in flags
    # 默认落在产物根下（下划线开头，工作区枚举会跳过），绝不写进代码域
    out_opt = options.get("--out")
    out = Path(out_opt).expanduser().resolve() if out_opt else (ctx.output_root / "_dispatch_prompts")
    out.mkdir(parents=True, exist_ok=True)
    for bid in expand([str(i) for i in ids]):
        txt = build_module(bid, ws, spans, titles) if module else build_transcribe(bid, ws, spans)
        suffix = "_M" if module else ""
        path = out / ("BLK%02d%s.txt" % (bid, suffix))
        path.write_text(txt, encoding="utf-8")
        print("BLK%02d%s -> %s" % (bid, suffix, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())