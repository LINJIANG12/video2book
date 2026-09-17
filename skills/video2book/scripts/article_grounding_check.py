#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长文依据级校验：这篇文章到底有没有基于本集逐字稿写。

**为什么需要它**：SKILL.md §4.5 长期把「是否真的听了音频」列为**不可校验**的纪律条款——
工具层只能看文件在不在、字节够不够，看不了内容来源。块级转录流水线改变了这一点：写作的
事实依据从「子智能体听过的音频」变成了**盘上的一份逐字稿**，于是「文章是否基于这份逐字稿」
第一次可以用纯脚本、零 token 地量出来。

**量什么**：从逐字稿里抽「技术实体」——英文标识符（寄存器名、指令名、API 名）与多位数字，
要求它在逐字稿里出现至少 `--min-freq` 次（滤掉 ASR 噪声），再看有多少个出现在长文里。
覆盖率低说明长文没怎么用这份语料：要么写成了通用讲义，要么干脆凭空生成。

**诚实的边界**（务必连同结果一起读）：
1. 这是**启发式**，不是语义判定。中文术语没有可靠的无词典抽取方式，因此只测技术实体；
   一篇完全用中文表述、却忠实于逐字稿的长文，得分也会偏低。
2. 它只能证明「用了语料」，不能证明「用得对」——取舍是否恰当、有没有过度展开，仍需抽样复核。
3. 逐字稿是 ASR 产物，本身可能有识别错误；实体频率下限就是为了压掉这类噪声。
4. 没有逐字稿的集**不参与判定**，单独计入 `unverifiable`（老工作区、未转录的集都属此类），
   不会因此判失败。

用法：
    python scripts/article_grounding_check.py --dir "<工作区>"
    python scripts/article_grounding_check.py --dir "<工作区>" --strict --min-coverage 0.6
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.console import enable_utf8_console  # noqa: E402
from src.core.task_cleanup import find_workspaces  # noqa: E402
from src.core.transcript_splitter import TranscriptSplitter  # noqa: E402
from src.core.workspace import TaskWorkspace  # noqa: E402

# 时间戳要先把整段抹掉再抽实体：否则「00:12:35」会被当成三个数字实体灌进统计
_TIMESTAMP_BLOCK_RE = re.compile(r"[\[【]\s*(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?\s*[\]】]")
# 英文标识符：字母开头、至少 3 个字符（排除 as / is / in 这类虚词）
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
# 多位数字：单数字噪声太大，只要 2 位以上（含小数）
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

DEFAULT_MIN_COVERAGE = 0.5
DEFAULT_MIN_FREQ = 2


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


def check_episode(
    ws: TaskWorkspace, page: int, min_freq: int, min_coverage: float
) -> Dict[str, Any]:
    """校验单集：返回覆盖率与缺失实体明细。"""
    from src.core.kernel_extractor import KernelExtractor

    article = KernelExtractor.find_article(ws, page)
    entry: Dict[str, Any] = {
        "page": page,
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

    part = next((p for p in (ws.load_parts() or []) if int(p.get("page", -1)) == page), None)
    from src.core.workspace import sanitize_filename

    clean_title = sanitize_filename(str((part or {}).get("title") or f"P{page:02d}"))
    transcript = TranscriptSplitter.existing_episode_transcript(ws, page, clean_title)
    if transcript is None:
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


def check_workspace(ws: TaskWorkspace, min_freq: int, min_coverage: float) -> Dict[str, Any]:
    """校验整个工作区的长文依据覆盖率。"""
    pages = sorted(int(p["page"]) for p in (ws.load_parts() or []) if p.get("page") is not None)
    entries = [check_episode(ws, page, min_freq, min_coverage) for page in pages]
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
        "avg_coverage": avg,
        "min_coverage": min_coverage,
        "min_freq": min_freq,
        "entries": entries,
        "low_entries": low,
    }


def main() -> int:
    enable_utf8_console()
    parser = argparse.ArgumentParser(description="长文依据级校验（长文 ↔ 逐字稿实体覆盖率）")
    parser.add_argument("--dir", default=None, help="工作区目录（缺省扫描产物根下全部工作区）")
    parser.add_argument("--base-dir", default=None, help="产物根（缺省由 src/core/paths.py 解析）")
    parser.add_argument("--task", default=None, help="按工作区名关键字过滤")
    parser.add_argument("--min-freq", type=int, default=DEFAULT_MIN_FREQ, dest="min_freq",
                        help=f"实体在逐字稿里的最低出现次数（滤除 ASR 噪声，默认 {DEFAULT_MIN_FREQ}）")
    parser.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE, dest="min_coverage",
                        help=f"覆盖率下限，低于即报警（默认 {DEFAULT_MIN_COVERAGE}）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--strict", action="store_true", help="存在低于覆盖率下限的集即返回非零")
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

    reports: List[Dict[str, Any]] = [
        check_workspace(ws, args.min_freq, args.min_coverage) for ws in workspaces
    ]

    if args.json:
        print(json.dumps({"reports": reports, "strict": bool(args.strict)}, ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("[*] 长文依据级校验（长文 ↔ 逐字稿 技术实体覆盖率）")
        print(f"[*] 口径：实体出现次数 ≥ {args.min_freq}；覆盖率下限 {args.min_coverage:.0%}；"
              f"无逐字稿的集不参与判定")
        print("[i] 这是启发式：只测英文标识符与数字，中文表述为主但忠实于语料的长文也会偏低；"
              "它证明「用了语料」，不证明「用得对」")
        print("=" * 72)
        for report in reports:
            print(f"\n▶ {report['workspace']}")
            avg = f"{report['avg_coverage']:.1%}" if report["avg_coverage"] is not None else "—"
            print(f"    可校验 {report['checked']}/{report['total']} 集（另 {report['unverifiable']} 集无逐字稿、"
                  f"{report['no_article']} 集无长文）")
            print(f"    达标 {report['ok']} | 低于下限 {report['low_coverage']} | 平均覆盖 {avg}")
            for entry in report["low_entries"][:12]:
                print(f"    [✗] P{entry['page']:02d} 覆盖 {entry['coverage']:.1%} "
                      f"（{entry['covered']}/{entry['entities']} 个实体）"
                      f" 长文 {entry['article_bytes']:,}B / 逐字稿 {entry['transcript_bytes']:,}B")
                if entry["missing"]:
                    print(f"         └ 逐字稿里高频但长文未出现：{'、'.join(entry['missing'][:8])}")
            if report["low_coverage"] > 12:
                print(f"    … 其余 {report['low_coverage'] - 12} 集见 --json 输出")
            if report["checked"] and report["low_coverage"] == 0:
                print("    ── 全部达标")
            if not report["checked"]:
                print("    ── 无可校验的集（尚无逐字稿，或尚无长文）")
        print("\n" + "=" * 72)

    any_low = any(r["low_coverage"] > 0 for r in reports)
    if any_low and args.strict:
        print("[FAIL] 存在长文未达依据覆盖率下限（详见上方 [✗]）")
        return 1
    if not args.json:
        print("[OK] 依据级校验完成" + ("（提示级：加 --strict 可纳入门禁）" if any_low else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
