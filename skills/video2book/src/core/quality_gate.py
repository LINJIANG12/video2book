# -*- coding: utf-8 -*-
"""交付质量门禁内核（`cli.py check` 的唯一实现处）。

原三个独立脚本（依据级校验 / 笔记成色 / 渲染合规）收敛到这里，共用一套工作区选择与报告逻辑，
只暴露两个 runner：

- `run_stage1(...)`：阶段一放行门禁——模块长文是否真的基于本块逐字稿（技术实体覆盖率）。
- `run_deliver(...)`：交付前体检——笔记成色 + 渲染合规。

口径（与文档一致，改动前先查门禁断言）：
- 默认**提示级**，只有 `strict=True` 时致命项才返回非零退出码；
- 笔记致命项五类定义在 `deliverable_lint.FATAL_NOTE_KEYS`；
- 渲染致命项为「告警块 / 围栏外裸字符画 / 围栏配对」；「缺语言标识」与「标题手写序号」
  默认只统计，分别由 `require_lang` / `require_no_numbering` 纳入门禁；
- 依据级校验是**启发式**：只测英文标识符与多位数字，无逐字稿的块不参与判定。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core import fsutil
from src.core.audio_merger import AudioMerger
from src.core.deliverable_lint import (
    FATAL_NOTE_KEYS,
    STRUCTURE_KEYS,
    fatal_note_total,
    fatal_render_total,
    lint_heading_numbers,
    lint_note,
    lint_render,
    summarize_note,
    summarize_render,
)
from src.core.task_cleanup import find_workspaces
from src.core.workspace import TaskWorkspace, find_module_article

# 非交付物：任务书是派发物，逐字稿是给写作角色看的原始语料（ASR 文本里围栏不闭合属正常）。
EXCLUDE_NAME_SUFFIXES = (
    "_TASK.md", "_KERNEL_TASK.md", "_转录任务书.md", TaskWorkspace.TRANSCRIPT_SUFFIX,
)

# 依据级校验阈值
DEFAULT_MIN_COVERAGE = 0.5
DEFAULT_MIN_FREQ = 2
# 笔记断句阈值（每份）；基准语料实测为 2（微机原理）/13（软件工程）
DEFAULT_MAX_TRUNCATED = 4

# 时间戳要先把整段抹掉再抽实体：否则「00:12:35」会被当成三个数字实体灌进统计
_TIMESTAMP_BLOCK_RE = re.compile(r"[\[【]\s*(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?\s*[\]】]")
# 英文标识符：字母开头、至少 3 个字符（排除 as / is / in 这类虚词）
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
# 多位数字：单数字噪声太大，只要 2 位以上（含小数）
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def resolve_workspaces(
    base_dir: Optional[str] = None,
    task: Optional[str] = None,
    dir_path: Optional[str] = None,
) -> List[TaskWorkspace]:
    """统一的工作区选择：`--dir` 优先，否则按产物根扫描并用 `--task` 过滤。"""
    if dir_path:
        ws_path = Path(dir_path)
        if not ws_path.exists():
            print(f"[ERROR] 工作区不存在: {ws_path}", file=sys.stderr)
            return []
        return [TaskWorkspace.from_existing(ws_path)]
    workspaces = find_workspaces(base_dir)
    if task:
        workspaces = [w for w in workspaces if str(task) in w.root_dir.name]
    return workspaces


# ---------------------------------------------------------------------------
# 阶段一：依据级校验（模块长文 ↔ 块级逐字稿）
# ---------------------------------------------------------------------------

def extract_entities(text: str, min_freq: int) -> Counter:
    """抽取候选技术实体及其在语料中的出现次数（已抹掉时间戳）。"""
    body = _TIMESTAMP_BLOCK_RE.sub(" ", text or "")
    counter: Counter = Counter()
    for match in _TOKEN_RE.finditer(body):
        counter[match.group(0).lower()] += 1
    for match in _NUMBER_RE.finditer(body):
        raw = match.group(0)
        if len(raw.replace(".", "")) >= 2:
            counter[raw] += 1
    return Counter({token: n for token, n in counter.items() if n >= min_freq})


def check_grounding_block(
    ws: TaskWorkspace, block: Dict[str, Any], min_freq: int, min_coverage: float
) -> Dict[str, Any]:
    """校验单个块：返回覆盖率与缺失实体明细（一块一验）。"""
    block_id = int(block.get("block_id") or 0)
    article = find_module_article(ws.articles_dir, block)
    entry: Dict[str, Any] = {
        "block_id": block_id,
        "span": str(block.get("span") or ""),
        "title": str(block.get("title") or ""),
        "episodes": [int(p) for p in (block.get("episodes") or [])],
        "article": str(article) if article else "",
        "article_bytes": 0,
        "transcript": "",
        "transcript_bytes": 0,
        "entities": 0,
        "covered": 0,
        "coverage": None,
        "missing": [],
        "status": "ok",
    }
    if article is None:
        entry["status"] = "no_article"
        return entry
    try:
        entry["article_bytes"] = article.stat().st_size
        article_text = article.read_text(encoding="utf-8", errors="replace").lower()
    except OSError as err:
        entry["status"] = "read_error"
        entry["error"] = str(err)
        return entry

    transcript = TaskWorkspace.block_path(ws, block)
    if not transcript.exists():
        # 没有逐字稿就没有比对基准：计入不可校验，绝不因此判失败
        entry["status"] = "unverifiable"
        return entry

    try:
        transcript_text = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        entry["status"] = "read_error"
        entry["error"] = str(err)
        return entry
    entry["transcript"] = str(transcript)
    entry["transcript_bytes"] = transcript.stat().st_size

    entities = extract_entities(transcript_text, min_freq)
    if not entities:
        entry["status"] = "no_entities"
        return entry

    missing = [token for token in entities if token not in article_text]
    covered = len(entities) - len(missing)
    coverage = covered / len(entities)
    entry["entities"] = len(entities)
    entry["covered"] = covered
    entry["coverage"] = round(coverage, 4)
    # 缺失清单按语料里的出现频次排序：出现得越多却没写进长文，越可疑
    entry["missing"] = sorted(missing, key=lambda t: -entities[t])[:12]
    entry["status"] = "ok" if coverage >= min_coverage else "low_coverage"
    return entry


def check_grounding_workspace(ws: TaskWorkspace, min_freq: int, min_coverage: float) -> Dict[str, Any]:
    """校验整个工作区的模块长文依据覆盖率（一块一验）。"""
    blocks = (AudioMerger.load_manifest(ws) or {}).get("blocks") or []
    entries = [
        check_grounding_block(ws, b, min_freq, min_coverage)
        for b in blocks if isinstance(b, dict)
    ]
    checked = [e for e in entries if e["status"] in ("ok", "low_coverage")]
    low = [e for e in checked if e["status"] == "low_coverage"]
    unverifiable = [e for e in entries if e["status"] == "unverifiable"]
    avg = round(sum(e["coverage"] for e in checked) / len(checked), 4) if checked else None
    return {
        "workspace": ws.root_dir.name,
        "total": len(entries),
        "checked": len(checked),
        "ok": len(checked) - len(low),
        "low_coverage": len(low),
        "unverifiable": len(unverifiable),
        "no_article": len([e for e in entries if e["status"] == "no_article"]),
        "no_entities": len([e for e in entries if e["status"] == "no_entities"]),
        "read_error": len([e for e in entries if e["status"] == "read_error"]),
        "avg_coverage": avg,
        "min_coverage": min_coverage,
        "min_freq": min_freq,
        "entries": entries,
        "low_entries": low,
    }


def run_stage1(
    *,
    base_dir: Optional[str] = None,
    task: Optional[str] = None,
    dir_path: Optional[str] = None,
    min_freq: int = DEFAULT_MIN_FREQ,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    strict: bool = False,
    as_json: bool = False,
) -> int:
    """阶段一放行门禁：模块长文是否基于本块逐字稿。"""
    workspaces = resolve_workspaces(base_dir=base_dir, task=task, dir_path=dir_path)
    if not workspaces:
        print(f"[ERROR] 未找到可用工作区（base-dir={base_dir or '产物根'}）", file=sys.stderr)
        return 1

    reports: List[Dict[str, Any]] = [
        check_grounding_workspace(ws, min_freq, min_coverage) for ws in workspaces
    ]

    if as_json:
        print(json.dumps({"reports": reports, "strict": bool(strict)}, ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("[*] 长文依据级校验（模块长文 ↔ 块级逐字稿 技术实体覆盖率）")
        print(f"[*] 口径：实体出现次数 ≥ {min_freq}；覆盖率下限 {min_coverage:.0%}；"
              f"无逐字稿的块不参与判定")
        print("[i] 这是启发式：只测英文标识符与数字，中文表述为主但忠实于语料的长文也会偏低；"
              "它证明「用了语料」，不证明「用得对」")
        print("=" * 72)
        for report in reports:
            print(f"\n▶ {report['workspace']}")
            avg = f"{report['avg_coverage']:.1%}" if report["avg_coverage"] is not None else "—"
            print(f"    可校验 {report['checked']}/{report['total']} 块（另 {report['unverifiable']} 块无逐字稿、"
                  f"{report['no_article']} 块无模块长文、{report['no_entities']} 块逐字稿无重复技术实体、"
                  f"{report['read_error']} 块读取失败）")
            print(f"    达标 {report['ok']} | 低于下限 {report['low_coverage']} | 平均覆盖 {avg}")
            for entry in report["low_entries"][:12]:
                print(f"    [✗] BLK{entry['block_id']:02d} {entry['span']} 覆盖 {entry['coverage']:.1%} "
                      f"（{entry['covered']}/{entry['entities']} 个实体）"
                      f" 长文 {entry['article_bytes']:,}B / 逐字稿 {entry['transcript_bytes']:,}B")
                if entry["missing"]:
                    print(f"         └ 逐字稿里高频但长文未出现：{'、'.join(entry['missing'][:8])}")
            if report["low_coverage"] > 12:
                print(f"    … 其余 {report['low_coverage'] - 12} 块见 --json 输出")
            if report["checked"] and report["low_coverage"] == 0:
                print("    ── 全部达标")
            if not report["checked"]:
                print("    ── 无可校验的块（尚无块级逐字稿 / 尚无模块长文 / 逐字稿里没有重复出现的"
                      "英文标识符与多位数字——纯中文口语讲述的块会落在这里，不等于长文有问题）")
        print("\n" + "=" * 72)

    any_low = any(r["low_coverage"] > 0 for r in reports)
    any_no_article = any(r["no_article"] > 0 for r in reports)
    any_unready = any(r["total"] > 0 and r["checked"] == 0 for r in reports)
    if (any_low or any_no_article or any_unready) and strict:
        if any_no_article:
            print("[FAIL] 存在块尚未产出模块长文（阶段一长文未就绪）")
        elif any_unready:
            print("[FAIL] 未检测到任何可校验的长文或逐字稿（阶段一未就绪）")
        if any_low:
            print("[FAIL] 存在模块长文未达依据覆盖率下限（详见上方 [✗]）")
        return 1
    if not as_json:
        has_warnings = any_low or any_no_article or any_unready
        print("[OK] 依据级校验完成" + ("（提示级：加 --strict 可纳入门禁）" if has_warnings else ""))
    return 0


# ---------------------------------------------------------------------------
# 交付前体检：笔记成色
# ---------------------------------------------------------------------------

def collect_notes(ws: Any) -> List[Path]:
    """工作区内的模块笔记成品（排除任务书）。"""
    if not ws.notes_dir.exists():
        return []
    notes: List[Path] = []
    for path in sorted(ws.notes_dir.glob("*.md")):
        if path.name.endswith("_TASK.md"):
            continue
        try:
            if path.stat().st_size < fsutil.PRODUCT_MIN_BYTES:
                continue
        except OSError:
            continue
        notes.append(path)
    return notes


def check_note_workspace(
    ws: Any, max_truncated: int, require_structure: bool = False
) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    totals: Dict[str, int] = {}
    fatal_total = 0

    for path in collect_notes(ws):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as err:
            files.append({"file": path.name, "error": str(err)})
            continue
        lint = lint_note(text)
        summary = summarize_note(lint)
        notes_fatal = fatal_note_total(summary)
        fatal_total += notes_fatal
        for key, value in summary.items():
            totals[key] = totals.get(key, 0) + value

        missing = [k for k in STRUCTURE_KEYS if not lint["structure"].get(k)]
        detail: Dict[str, Any] = {
            "file": TaskWorkspace.to_relative(path),
            "bytes": fsutil.file_size(path),
            "summary": summary,
            "fatal": notes_fatal,
            "structure_missing": missing,
            "samples": {
                key: lint[key][:3] for key in (*FATAL_NOTE_KEYS, "truncated") if lint.get(key)
            },
        }
        files.append(detail)

    failed = [
        f for f in files
        if f.get("fatal", 0) > 0
        or (require_structure and f.get("structure_missing"))
        or f.get("summary", {}).get("truncated", 0) > max_truncated
    ]
    return {
        "workspace": ws.root_dir.name,
        "workspace_path": str(ws.root_dir),
        "file_count": len(files),
        "fatal_total": fatal_total,
        "totals": totals,
        "files": files,
        "failed": [f["file"] for f in failed],
        "fatal_failed": [f["file"] for f in files if f.get("fatal", 0) > 0],
        "structure_gap": [f["file"] for f in files if f.get("structure_missing")],
        "truncated_threshold": max_truncated,
    }


# ---------------------------------------------------------------------------
# 交付前体检：渲染合规
# ---------------------------------------------------------------------------

def collect_markdown(ws: Any) -> List[Path]:
    """工作区内所有 Markdown 成品（递归，跳过任务书、逐字稿与隐藏目录）。"""
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


def check_render_workspace(
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


def run_deliver(
    *,
    base_dir: Optional[str] = None,
    task: Optional[str] = None,
    dir_path: Optional[str] = None,
    max_truncated: int = DEFAULT_MAX_TRUNCATED,
    require_structure: bool = False,
    require_lang: bool = False,
    require_no_numbering: bool = False,
    strict: bool = False,
    as_json: bool = False,
) -> int:
    """交付前体检：笔记成色 + 渲染合规（两者共用一次工作区扫描）。"""
    workspaces = resolve_workspaces(base_dir=base_dir, task=task, dir_path=dir_path)
    if not workspaces:
        print(f"[ERROR] 未找到可用工作区（base-dir={base_dir or '产物根'}）", file=sys.stderr)
        return 1

    note_reports = [check_note_workspace(ws, max_truncated, require_structure) for ws in workspaces]
    render_reports = [
        check_render_workspace(ws, require_lang=require_lang, require_no_numbering=require_no_numbering)
        for ws in workspaces
    ]

    if as_json:
        print(json.dumps({
            "notes": note_reports,
            "render": render_reports,
            "strict": bool(strict),
            "require_structure": bool(require_structure),
            "require_lang": bool(require_lang),
            "require_no_numbering": bool(require_no_numbering),
        }, ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print(f"[*] 交付前体检（笔记成色 + 渲染合规；致命项门禁 = {'/'.join(FATAL_NOTE_KEYS)}）")
        print("=" * 72)
        for report in note_reports:
            print(f"\n▶ {report['workspace']}  （{report['file_count']} 份笔记）")
            if not report["file_count"]:
                print("    （无笔记成品）")
            else:
                for item in report["files"]:
                    if "error" in item:
                        print(f"    [!] {item['file']}: {item['error']}")
                        continue
                    s = item["summary"]
                    flag = "[✗]" if item["file"] in report["failed"] else "[✓]"
                    miss = ("/".join(k.replace("has_", "") for k in item["structure_missing"])) or "-"
                    print(
                        f"    {flag} {Path(item['file']).name[:44]:46s} 套话={s['boilerplate']:4d} "
                        f"空壳={s['hollow_headings']:2d} 分集标题={s['episode_headings']:3d} "
                        f"行内引用={s['inline_quote']:2d} 分集口吻={s['episode_voice']:2d} "
                        f"断句={s['truncated']:2d} 缺件={miss}"
                    )
                    for key in FATAL_NOTE_KEYS:
                        for sample in item["samples"].get(key, [])[:2]:
                            print(f"         └ {key} @{sample['line']}: {sample['text'][:88]}")
                t = report["totals"]
                print(f"    ── 合计：致命 {report['fatal_total']} 处 | 套话 {t.get('boilerplate', 0)} | "
                      f"空壳标题 {t.get('hollow_headings', 0)} | 分集标题 {t.get('episode_headings', 0)} | "
                      f"行内引用 {t.get('inline_quote', 0)} | 分集口吻 {t.get('episode_voice', 0)} | "
                      f"断句合计 {t.get('truncated', 0)}（阈值按**每份**笔记 {report['truncated_threshold']} 处判定）| "
                      f"结构缺件 {t.get('structure_missing', 0)}")
                if report["failed"]:
                    print(f"    ── 未通过：{len(report['failed'])} 份"
                          f"（{'；'.join(Path(f).name for f in report['failed'][:4])}）")
                elif report["structure_gap"]:
                    print(f"    ── 致命项全 0；另有 {len(report['structure_gap'])} 份缺 v2 结构构件"
                          f"（历史工作区遗留，加 --require-structure 可纳入门禁）")
                else:
                    print("    ── 全部通过")

        print("\n" + "-" * 72)
        print("[*] 渲染合规体检（告警块 / 围栏外字符画 / 围栏配对 / 围栏语言标识 / 标题手写序号）")
        scope = "语言标识=门禁项（--require-lang）" if require_lang else "语言标识=提示项（不参与 --strict）"
        scope += "；标题序号=门禁项" if require_no_numbering else "；标题序号=提示项"
        print(f"[*] 门禁口径：{scope}")
        for report in render_reports:
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

    notes_failed = any(r["failed"] for r in note_reports)
    render_fatal = any(r["fatal_total"] > 0 for r in render_reports)

    if strict and (notes_failed or render_fatal):
        if notes_failed:
            print("[FAIL] 笔记成色不达标（详见上方 ✗ 项）")
        if render_fatal:
            print("[FAIL] 存在渲染致命项（详见上方 [✗] 文件）")
        return 1
    if not as_json:
        problems = []
        if notes_failed:
            problems.append("笔记成色")
        if render_fatal:
            problems.append("渲染")
        suffix = f"（存在不达标项：{'、'.join(problems)}；未开启 --strict）" if problems else ""
        print("[OK] 交付前体检完成" + suffix)
    return 0
