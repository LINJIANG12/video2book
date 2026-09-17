#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render Compatibility Check（交付物渲染合规体检）。

交付物默认在 Typora 阅读，并需兼容 VS Code Markmap / XMind / 纯文本。本脚本对工作区内**全部**
Markdown 成品（含历史遗留的手工镜像目录）做渲染层面的机器体检：

  ① GitHub 专有告警块 `> [!TIP]`（旧版 Typora 会原样露出 `[!TIP]` 字样）
  ② 围栏外裸字符画（渲染时连续空格被合并，图形会彻底错位）
  ③ 代码围栏未成对闭合 ④ 围栏缺失语言标识 ⑤ 标题手写序号（与阅读器自动编号叠成双号）

  门禁口径：①②③ 为**致命项**，参与 `--strict`；④「缺语言标识」与 ⑤「标题手写序号」
  默认只提示不拦（历史产物既有缺口多），需要死守时分别追加 `--require-lang` /
  `--require-no-numbering`；存量标题序号可用 `scripts/strip_heading_numbers.py` 就地清理。

用法：
    python scripts/render_compat_check.py                    # 体检全部工作区
    python scripts/render_compat_check.py --task 微机原理      # 只体检名称含关键字的工作区
    python scripts/render_compat_check.py --dir "<工作区路径>"
    python scripts/render_compat_check.py --strict           # 有致命项即返回非零
    python scripts/render_compat_check.py --strict --require-lang   # 把「缺语言标识」也纳入门禁
    python scripts/render_compat_check.py --strict --require-no-numbering   # 把「标题序号」也纳入门禁
    python scripts/render_compat_check.py --json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.console import enable_utf8_console  # noqa: E402

# 控制台硬化：输出含 `▶`/`✗`/`└`/`…` 等符号，管道捕获时若按 locale(cp936) 编码会崩。
enable_utf8_console()

from src.core.deliverable_lint import (  # noqa: E402
    fatal_render_total,
    lint_heading_numbers,
    lint_render,
    summarize_render,
)
from src.core import fsutil  # noqa: E402
from src.core.task_cleanup import find_workspaces  # noqa: E402
from src.core.workspace import TaskWorkspace  # noqa: E402

# 排除：任务书与逐字稿（都不是交付物，前者是派发物、后者是给写作角色看的原始语料——
# 语音识别文本里出现没闭合的代码围栏是正常现象，不该算渲染缺陷）与隐藏目录（归档/备份/缓存）
EXCLUDE_NAME_SUFFIXES = ("_TASK.md", "_KERNEL_TASK.md", "_转录任务书.md", "_逐字稿.md")


def collect_markdown(ws: Any) -> List[Path]:
    """工作区内所有 Markdown 成品（递归，含历史手工镜像目录；跳过任务书与隐藏目录）。

    递归走 `fsutil.iter_files`：工作区里若混入 Windows 不受信任的装入点（junction 等），
    `Path.rglob()` 会整体抛 `OSError`，让这一门课的体检连带整轮扫描一起失败。
    """
    files: List[Path] = []
    for path in fsutil.iter_files(ws.root_dir, "*.md", skip_hidden_dirs=True):
        try:
            rel_parents = path.relative_to(ws.root_dir).parts[:-1]
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel_parents):
            continue  # .archive / .backup_before_render_fix / .backup_single_notes 等
        if any(path.name.endswith(suffix) for suffix in EXCLUDE_NAME_SUFFIXES):
            continue
        try:
            if path.stat().st_size < 200:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def check_workspace(
    ws: Any, require_lang: bool = False, require_no_numbering: bool = False
) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    totals = {
        "alert_blocks": 0,
        "stray_art": 0,
        "fences_unbalanced": 0,
        "fence_without_lang": 0,
        "numbered_headings": 0,
    }

    for path in collect_markdown(ws):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lint = lint_render(text)
        summary = summarize_render(lint)
        for key, value in summary.items():
            totals[key] += value
        # 标题手写序号：阅读器会自动编号，两套号会叠成「1. 第 1 章」这种双号。
        # 与「缺语言标识」同口径——默认只统计提示，显式 --require-no-numbering 才纳入门禁。
        numbered = lint_heading_numbers(text)
        totals["numbered_headings"] += len(numbered)
        fatal_here = fatal_render_total(summary)
        if require_lang:
            fatal_here += summary["fence_without_lang"]
        if require_no_numbering:
            fatal_here += len(numbered)
        if fatal_here == 0:
            continue
        entries.append({
            "file": TaskWorkspace.to_relative(path),
            "fatal": fatal_here,
            "summary": summary,
            "numbered_headings": len(numbered),
            "samples": {
                "alert_blocks": lint["alert_blocks"][:3],
                "stray_art": lint["stray_art"][:3],
                "fence_without_lang": lint["fence_without_lang"][:3],
                "numbered_headings": numbered[:3],
            },
        })

    fatal = fatal_render_total(totals)
    if require_lang:
        fatal += totals["fence_without_lang"]
    if require_no_numbering:
        fatal += totals["numbered_headings"]
    return {
        "workspace": ws.root_dir.name,
        "workspace_path": str(ws.root_dir),
        "scanned_files": len(collect_markdown(ws)),
        "problem_files": len(entries),
        "totals": totals,
        "fatal_total": fatal,
        "require_lang": bool(require_lang),
        "require_no_numbering": bool(require_no_numbering),
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Deliverable render-compatibility check (alert blocks / bare art / fences)")
    parser.add_argument("--dir", default=None, help="直接指定单个工作区目录")
    parser.add_argument("--base-dir", default=None,
                        help="工作区基目录（默认：由 src/core/paths.py 解析的产物根——默认 <当前工作目录>/output，在容器内工作时为 <容器根>/output）")
    parser.add_argument("--task", default=None, help="仅处理目录名包含该关键字的工作区")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--strict", action="store_true", help="存在致命项即返回非零")
    parser.add_argument("--require-lang", action="store_true", dest="require_lang",
                        help="把「围栏缺语言标识」也纳入门禁（默认只提示；历史成品存在既有缺口）")
    parser.add_argument("--require-no-numbering", action="store_true", dest="require_no_numbering",
                        help="把「标题手写序号」也纳入门禁（默认只提示；旧产物可用 "
                             "scripts/strip_heading_numbers.py 清理）")
    args = parser.parse_args()

    if args.dir:
        ws_path = Path(args.dir)
        if not ws_path.exists():
            print(f"[ERROR] 工作区不存在: {ws_path}", file=sys.stderr)
            return 1
        workspaces = [TaskWorkspace.from_existing(ws_path)]
    else:
        workspaces = find_workspaces(args.base_dir)
        if args.task:
            workspaces = [w for w in workspaces if str(args.task) in w.root_dir.name]

    if not workspaces:
        print(f"[ERROR] 未找到可用工作区（base-dir={args.base_dir or '产物根'}）", file=sys.stderr)
        return 1

    reports = [
        check_workspace(ws, require_lang=args.require_lang,
                        require_no_numbering=args.require_no_numbering)
        for ws in workspaces
    ]

    if args.json:
        print(json.dumps({"reports": reports, "strict": bool(args.strict),
                          "require_lang": bool(args.require_lang),
                          "require_no_numbering": bool(args.require_no_numbering)},
                         ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("[*] 交付物渲染合规体检（告警块 / 围栏外字符画 / 围栏配对 / 围栏语言标识 / 标题手写序号）")
        scope = "语言标识=门禁项（--require-lang）" if args.require_lang else "语言标识=提示项（不参与 --strict）"
        scope += "；标题序号=门禁项" if args.require_no_numbering else "；标题序号=提示项"
        print(f"[*] 门禁口径：{scope}")
        print("=" * 72)
        for report in reports:
            print(f"\n▶ {report['workspace']}")
            print(f"    扫描成品 {report['scanned_files']} 份 | 有问题 {report['problem_files']} 份 | "
                  f"致命 {report['fatal_total']} 处")
            t = report["totals"]
            print(f"    ── 告警块 {t['alert_blocks']} | 围栏外字符画 {t['stray_art']} | "
                  f"围栏未闭合 {t['fences_unbalanced']} | 缺语言标识 {t['fence_without_lang']} | "
                  f"标题手写序号 {t.get('numbered_headings', 0)}")
            for entry in report["entries"][:12]:
                s = entry["summary"]
                short = entry["file"].split("/", 2)[-1] if "/" in entry["file"] else entry["file"]
                print(f"    [✗] {short[:66]:68s} 告警块={s['alert_blocks']:3d} "
                      f"裸图={s['stray_art']:2d} 未闭合={s['fences_unbalanced']} "
                      f"缺语言={s['fence_without_lang']} 序号={entry.get('numbered_headings', 0)}")
                for key in ("alert_blocks", "stray_art", "numbered_headings"):
                    for sample in entry["samples"].get(key, [])[:1]:
                        print(f"         └ {key} @{sample['line']}: {sample['text'][:84]}")
            if len(report["entries"]) > 12:
                print(f"    … 其余 {len(report['entries']) - 12} 份见 --json 输出")
            if report["problem_files"] == 0:
                print("    ── 全部合规")
        print("\n" + "=" * 72)

    any_fatal = any(r["fatal_total"] > 0 for r in reports)
    if any_fatal and args.strict:
        print("[FAIL] 存在渲染致命项（详见上方 [✗] 文件）")
        return 1
    if not args.json:
        print("[OK] 渲染合规体检完成" + ("（存在致命项，未开启 --strict）" if any_fatal else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
