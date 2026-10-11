# -*- coding: utf-8 -*-
"""公式断裂清理内核（`cli.py check --fix-formula-breaks` 的唯一实现处）。

背景：写作时 `\\neq` 里的 `\\n` 被当成换行符，公式断在中途、下一行以 `eq` 开头：

    > **结论**：系数行列式 $D
    eq 0$ 时方程组有唯一解。

行内情形渲染成「D eq 0」这种假式子（公式真值被改）；落在显示公式里更糟——收尾 `$$` 跑到行尾、
被阅读器判成未闭合，一路吞掉后续正文（阅读侧的 math-guard 护栏就是为这个加的）。
2026-10-11 全库扫描：21 门课、67 份成品、460 处命中。

修复只做一件事：把续行并回上一行，接缝处补 `\\neq `；级联（一处断成两行以上）由内层循环吸收。
判据与门禁共用 `deliverable_lint.EQ_BREAK_RE`，所以「体检报了几处」与「这里改了几处」永远一致。

**默认只报不改**：写盘必须显式 `apply=True`（CLI 上是 `--apply`），与 `--fix-numbering` 同一纪律——
先看预演、确认无误再落盘。每份文件写盘前做一次**保真校验**：`canon(改前) == canon(改后)`，
其中 canon = 去掉全部空白 + 把 `\\neq` 还原成 `eq`。等式成立即证明本次只动了空白与这一处替换，
正文一字未动；校验不过的文件**不写盘**，进 `unverified` 报告。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core.deliverable_lint import EQ_BREAK_RE
from src.core.heading_cleanup import read_with_retry, write_text_lf
from src.core.heading_numbers import iter_lines

# 接缝：`\neq` 两侧各留一个空格，与「本来就写对」的那些地方写法一致
NEQ = " \\neq "


def canon(text: str) -> str:
    """保真校验用的规范形：去掉全部空白 + 把 `\\neq` 还原成 `eq`。

    两边都还原，是为了让**本来就正确**的 `\\neq` 在两侧抵消；于是 `canon(前) == canon(后)`
    成立就只可能意味着：本次除了空白与这一处替换之外，别的字符一个没动。
    """
    return re.sub(r"\s+", "", text).replace("\\neq", "eq")


def fix_text(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """返回 `(新文本, 改动明细)`；明细**按断裂行计数**，与门禁 `formula_breaks` 的口径对齐。

    围栏内的行一律不动（那里可能是代码里的 `eq`）。
    """
    entries = list(iter_lines(text))
    out: List[str] = []
    changes: List[Dict[str, Any]] = []
    index = 0

    while index < len(entries):
        line_no, line, in_fence = entries[index]
        following = entries[index + 1] if index + 1 < len(entries) else None
        if (not in_fence and line.strip() and following is not None
                and not following[2] and EQ_BREAK_RE.match(following[1].strip())):
            joined = line.rstrip()
            group: List[Tuple[int, str]] = []
            index += 1
            while index < len(entries):
                broken_no, broken_line, broken_fence = entries[index]
                if broken_fence or not EQ_BREAK_RE.match(broken_line.strip()):
                    break
                body = broken_line.strip()[2:].lstrip()
                joined = joined.rstrip() + NEQ + body
                group.append((broken_no, broken_line.strip()))
                index += 1
            for broken_no, broken_text in group:
                changes.append({
                    "line": broken_no,
                    "before": broken_text[:120],
                    "after": joined[:160],
                })
            out.append(joined)
            continue
        out.append(line)
        index += 1

    new_text = "\n".join(out)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, changes


def run_fix_formula_breaks(
    *,
    base_dir: Optional[str] = None,
    task: Optional[str] = None,
    dir_path: Optional[str] = None,
    apply: bool = False,
    as_json: bool = False,
    max_samples: int = 5,
) -> int:
    """存量产物公式断裂清理（幂等，可反复跑）。

    **默认只报不改**：判定本身是确定的（以 `eq` 开头的正文行不存在），但先预演一遍再落盘是
    本项目的纪律，也让「改了哪些地方」有机会被人眼扫一遍。
    """
    from src.core.quality_gate import collect_markdown
    from src.core.task_cleanup import find_workspaces
    from src.core.workspace import TaskWorkspace

    if dir_path:
        ws_path = Path(dir_path)
        if not ws_path.exists():
            print(f"[ERROR] 工作区不存在: {ws_path}", file=sys.stderr)
            return 1
        workspaces = [TaskWorkspace.from_existing(ws_path)]
    else:
        workspaces = find_workspaces(base_dir)
        if task:
            workspaces = [w for w in workspaces if str(task) in w.root_dir.name]

    if not workspaces:
        print(f"[ERROR] 未找到可用工作区（base-dir={base_dir or '产物根'}）", file=sys.stderr)
        return 1

    reports: List[Dict[str, Any]] = []
    for ws in workspaces:
        files = collect_markdown(ws)
        entry: Dict[str, Any] = {
            "workspace": ws.root_dir.name,
            "scanned_files": len(files),
            "changed_files": 0,
            "breaks": 0,
            "skipped": [],
            "unverified": [],
            "samples": [],
        }
        changed_paths: set = set()
        for path in files:
            text = read_with_retry(path)
            if text is None:
                rel = TaskWorkspace.to_relative(path)
                if rel not in entry["skipped"]:
                    entry["skipped"].append(rel)
                continue
            new_text, changes = fix_text(text)
            if not changes:
                continue
            if canon(text) != canon(new_text):
                # 保真校验不过 = 改动超出了「并回 + 补 \neq」的范围，宁可不改
                entry["unverified"].append(TaskWorkspace.to_relative(path))
                continue
            changed_paths.add(str(path))
            entry["breaks"] += len(changes)
            if apply:
                write_text_lf(path, new_text)
            if len(entry["samples"]) < max_samples:
                entry["samples"].append({
                    "file": TaskWorkspace.to_relative(path),
                    **changes[0],
                })
        entry["changed_files"] = len(changed_paths)
        reports.append(entry)

    totals = {key: sum(r[key] for r in reports)
              for key in ("scanned_files", "changed_files", "breaks")}

    if as_json:
        print(json.dumps({"reports": reports, "totals": totals,
                          "dry_run": not apply, "applied": bool(apply)},
                         ensure_ascii=False, indent=2))
        return 0

    mode = "就地修复" if apply else "预演（不改动）"
    print("=" * 72)
    print("[*] 公式断裂清理（`\\neq` 被折成换行 + `eq`）" + f"（{mode}）")
    print("=" * 72)
    for report in reports:
        print(f"\n▶ {report['workspace']}")
        print(f"    扫描 {report['scanned_files']} 份 | 需改 {report['changed_files']} 份 | "
              f"断裂点 {report['breaks']} 处")
        if report["skipped"]:
            print(f"    [!] 读取失败被跳过 {len(report['skipped'])} 份（可能被杀软/编辑器占用），"
                  f"请重跑本命令：")
            for rel in report["skipped"][:5]:
                print(f"        {rel}")
        if report["unverified"]:
            print(f"    [!] 保真校验不通过、已跳过不写盘的 {len(report['unverified'])} 份"
                  f"（改动超出预期范围，请人眼确认）：")
            for rel in report["unverified"][:5]:
                print(f"        {rel}")
        for sample in report["samples"][:max_samples]:
            print(f"    · {sample['file']} @{sample['line']}")
            print(f"      改前 {sample['before']}")
            print(f"      改后 {sample['after']}")
    print("\n" + "-" * 72)
    print(f"合计：扫描 {totals['scanned_files']} 份 | 需改 {totals['changed_files']} 份 | "
          f"断裂点 {totals['breaks']} 处")
    if not apply:
        print("[i] 这是预演，未写入任何文件；确认无误后加 --apply 再跑一次。")
    elif totals["changed_files"] == 0:
        print("[i] 没有需要改动的文件（已全部无公式断裂，幂等）。")
    print("=" * 72)
    return 0
