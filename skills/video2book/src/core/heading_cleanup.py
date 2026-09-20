# -*- coding: utf-8 -*-
"""交付物标题去号清理内核（`cli.py check --fix-numbering` 的唯一实现处）。

背景：笔记、长文与模块教材都曾把序号写进标题，而 Typora 等阅读器**自己也会给标题编号**，
两套序号叠在一起渲染成「1. 1. 前端开发工具链」这种双号。长文提示词与教材整编已改为不写号，
本模块负责把**已完成的存量产物**就地清干净。

处理对象（默认两类，不含 notes）：
  <工作区>/textbooks/*.md          模块教材（去 `## 第 N 章：…` 与继承自长文的 `## 2.1 …`）
  <工作区>/articles/*_精读长文.md   模块长文（去 `## 1. …` / `### 2.1 …`）

两条规则：
  ① 标题行序号前缀剥离——规则与门禁共用 `src/core/heading_numbers`，幂等、跳过代码围栏。
     纯数字前缀只在「不是在数东西」时才剥：`## 3 种方案的取舍`、`## 2025 年路线图` 保持不动。
  ② 教材目录行 `- **第 N 章**：标题` → `N. 标题`（改成有序列表，序号交给渲染器）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core.heading_numbers import (
    HEADING_RE,
    first_bare_prefix,
    iter_lines,
    strip_heading_number,
)

# 教材目录行：`- **第 3 章**：标题` → `3. 标题`
TOC_CHAPTER_RE = re.compile(r"^([ \t]*)-[ \t]+\*\*第\s*(\d{1,4})\s*章\*\*[：:][ \t]*(.*)$")

TASK_SUFFIX = "_TASK.md"

# 一轮跑完再看还有没有得改，直到收敛（幂等）；3 轮足够，纯属保险。
MAX_PASSES = 3


def read_with_retry(path: Path, attempts: int = 3) -> Optional[str]:
    """读取文件；被杀软 / 编辑器短暂占用时重试，仍失败则返回 None（报告里列为 skipped）。

    首轮实跑就踩过这个坑：40 份文件读失败被静默 `continue` 跳过，表面上「跑完了」，
    实际留下一批双重编号。宁可重试 + 如实报告，也不要静默漏改。
    """
    for index in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            if index + 1 < attempts:
                time.sleep(0.15)
    return None


def write_text_lf(path: Path, text: str) -> None:
    """按 LF 写回：课程产物统一 LF，而默认 `write_text` 会把 \\n 翻成 CRLF。"""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def collect_files(ws: Any, only: str) -> List[Path]:
    """收集待处理文件；跳过任务书，顺序稳定（便于比对）。"""
    picks: List[Path] = []
    if only in ("textbooks", "both"):
        picks += sorted((ws.root_dir / "textbooks").glob("*.md"))
    if only in ("articles", "both"):
        # 现行命名是模块长文（一块一篇）；逐集长文（PXX_*_精读文章.md）已随逐集链路废除
        picks += sorted((ws.root_dir / "articles").glob("*_精读长文.md"))
    out = []
    for path in picks:
        if path.name.endswith(TASK_SUFFIX):
            continue
        try:
            if path.is_file():
                out.append(path)
        except OSError:
            continue
    return out


def clean_text(text: str) -> Tuple[str, int, int, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """返回 `(新文本, 标题去号条数, 目录归一化条数, 改动明细, 被略过的可疑行)`。"""
    changes: List[Dict[str, Any]] = []
    suspects: List[Dict[str, Any]] = []
    out: List[str] = []
    headings = toc = 0

    for line_no, line, in_fence in iter_lines(text):
        if not in_fence:
            toc_match = TOC_CHAPTER_RE.match(line)
            if toc_match:
                new_line = f"{toc_match.group(1)}{toc_match.group(2)}. {toc_match.group(3)}"
                changes.append({"rule": "toc", "line": line_no,
                                "before": line.strip(), "after": new_line.strip()})
                toc += 1
                out.append(new_line)
                continue

            new_line = strip_heading_number(line)
            if new_line != line:
                changes.append({"rule": "heading", "line": line_no,
                                "before": line.strip(), "after": new_line.strip()})
                headings += 1
                out.append(new_line)
                continue

            heading_match = HEADING_RE.match(line)
            if heading_match:
                number = first_bare_prefix(heading_match.group(3))
                if number:
                    suspects.append({"line": line_no, "number": number,
                                     "text": line.strip()[:120]})
        out.append(line)

    new_text = "\n".join(out)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, headings, toc, changes, suspects


def nonheading_fingerprint(files: List[Path]) -> str:
    """非标题行内容指纹：清理前后必须一致，用来证明「正文一字未动」。"""
    digest = hashlib.sha256()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        digest.update(path.name.encode("utf-8"))
        for line_no, line, in_fence in iter_lines(text):
            if not in_fence and HEADING_RE.match(line):
                continue
            digest.update(f"{line_no}\t{line}\n".encode("utf-8"))
    return digest.hexdigest()


def run_fix_numbering(
    *,
    base_dir: Optional[str] = None,
    task: Optional[str] = None,
    dir_path: Optional[str] = None,
    only: str = "both",
    dry_run: bool = False,
    as_json: bool = False,
    max_samples: int = 5,
    hash_nonheading: bool = False,
) -> int:
    """存量产物标题去号（幂等，可反复跑）。"""
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
        files = collect_files(ws, only)
        entry: Dict[str, Any] = {
            "workspace": ws.root_dir.name,
            "scanned_files": len(files),
            "changed_files": 0,
            "passes": 0,
            "headings": 0,
            "toc": 0,
            "skipped": [],
            "suspects": [],
            "samples": [],
        }
        if hash_nonheading:
            entry["nonheading_sha256"] = nonheading_fingerprint(files)

        changed_paths: set = set()
        # 跑到达标为止：读文件可能被杀软 / 编辑器短暂占用，每轮都重试；
        # 一轮下来零改动即收敛（幂等）。
        for _pass in range(1, MAX_PASSES + 1):
            entry["passes"] = _pass
            changed_this_pass = 0
            for path in files:
                text = read_with_retry(path)
                if text is None:
                    rel = TaskWorkspace.to_relative(path)
                    if rel not in entry["skipped"]:
                        entry["skipped"].append(rel)
                    continue
                new_text, headings, toc, changes, suspects = clean_text(text)
                if headings or toc:
                    changed_this_pass += 1
                    changed_paths.add(str(path))
                    entry["headings"] += headings
                    entry["toc"] += toc
                    if not dry_run:
                        write_text_lf(path, new_text)
                entry["suspects"] += [
                    {**item, "file": TaskWorkspace.to_relative(path)} for item in suspects
                ]
                if changes and len(entry["samples"]) < max_samples:
                    entry["samples"].append({
                        "file": TaskWorkspace.to_relative(path),
                        **changes[0],
                    })
            if dry_run or changed_this_pass == 0:
                break
        entry["changed_files"] = len(changed_paths)
        reports.append(entry)

    totals = {key: sum(r[key] for r in reports)
              for key in ("scanned_files", "changed_files", "headings", "toc")}

    if as_json:
        print(json.dumps({"reports": reports, "totals": totals,
                          "dry_run": bool(dry_run), "only": only},
                         ensure_ascii=False, indent=2))
        return 0

    mode = "预演（不改动）" if dry_run else "就地清理"
    print("=" * 72)
    print("[*] 交付物标题去号" + f"（{mode}；范围={only}）")
    print("=" * 72)
    for report in reports:
        print(f"\n▶ {report['workspace']}")
        print(f"    扫描 {report['scanned_files']} 份 | 需改 {report['changed_files']} 份 | "
              f"标题去号 {report['headings']} 行 | 目录归一 {report['toc']} 行 | "
              f"轮次 {report['passes']}")
        if report["skipped"]:
            print(f"    [!] 读取失败被跳过 {len(report['skipped'])} 份（可能被杀软/编辑器占用），"
                  f"请重跑本命令：")
            for rel in report["skipped"][:5]:
                print(f"        {rel}")
        if report.get("nonheading_sha256"):
            print(f"    非标题行指纹 sha256: {report['nonheading_sha256']}")
        for sample in report["samples"][:max_samples]:
            print(f"    · {sample['before'][:60]}")
            print(f"      → {sample['after'][:60]}")
        if report["suspects"]:
            print(f"    [!] 被规则略过、需人眼确认的纯数字标题 {len(report['suspects'])} 行（前 5 条）：")
            for item in report["suspects"][:5]:
                print(f"        {item['file']} @{item['line']}: {item['text'][:72]}")
    print("\n" + "-" * 72)
    print(f"合计：扫描 {totals['scanned_files']} 份 | 需改 {totals['changed_files']} 份 | "
          f"标题去号 {totals['headings']} 行 | 目录归一 {totals['toc']} 行")
    if dry_run:
        print("[i] 这是预演，未写入任何文件；确认无误后去掉 --dry-run 再跑一次。")
    elif totals["changed_files"] == 0:
        print("[i] 没有需要改动的文件（已全部无手写序号，幂等）。")
    print("=" * 72)
    return 0
