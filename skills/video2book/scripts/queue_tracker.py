#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dynamic Queue Tracker for Micro-Agent Continuous Dispatch.

Tracks completed vs pending episodes in real time, supporting sliding-window
continuous dispatch ("完成一个，立即派生一个") without manual offsets.

`--next N --json` 输出的是**可直接转交子智能体的派发载荷**（任务书路径、音频切片清单、
长文目标路径、本集 token 预算），并附带派发建议（并发数 / 打包粒度 / 是否必须派发）。
`--log-dispatch` 可选地把本次建议写入 `<task>/.dispatch_log.jsonl` 作为派发台账。
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

# 允许从任意工作目录运行（SKILL.md 推荐直接调用 scripts/queue_tracker.py）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.console import enable_utf8_console  # noqa: E402

# 控制台硬化：输出含 `•`/中文，管道捕获时若按 locale(cp936) 编码会崩。
enable_utf8_console()


def get_task_workspace(
    custom_path: Optional[str] = None,
    pattern: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> Path:
    """定位目标任务工作区：显式 --dir > --base-dir > 产物根。

    产物根由 `src/core/paths.py` 解析：`$BVB_OUTPUT_DIR` → 有容器标记（`$BVB_HOME` 或
    `.bvb-home`）时的 `<home>/output` → **否则当前工作目录下的 `output/`**（默认，无需配置）。
    容器布局下从任意工作目录调用都能找到同一批工作区；无容器标记时产物根跟随工作目录，
    因此请在你放产物的那个目录下执行，或用 `--base-dir` 明确指定。
    """
    if custom_path:
        p = Path(custom_path).resolve()
        if p.exists():
            return p
        raise FileNotFoundError(f"Specified workspace directory does not exist: {custom_path}")

    sys.path.insert(0, str(PROJECT_ROOT))
    from src.core import fsutil
    from src.core.paths import resolve_base_dir

    out_dir = resolve_base_dir(base_dir)
    if not fsutil.is_dir(out_dir):
        raise RuntimeError(
            f"产物根不存在: {out_dir}（可用 --base-dir 指定，或设置 ${'BVB_OUTPUT_DIR'}）"
        )

    # 容忍把「工作区目录本身」当作 base-dir 传入（用户很自然会这么用）
    if (out_dir / "parts.json").exists() or (out_dir / "manifest.json").exists():
        return out_dir

    # 枚举走 fsutil：条目不可访问（Windows「不受信任的装入点」连 Path.is_dir() 都会抛
    # OSError）时只跳过它，不让一门课里的坏链接把整个队列追踪打断。
    valid_dirs = [
        d for d in fsutil.iter_child_dirs(out_dir)
        if (d / "parts.json").exists()
    ]

    if not valid_dirs:
        # Fallback to any directory in output
        valid_dirs = list(fsutil.iter_child_dirs(out_dir))

    if not valid_dirs:
        raise RuntimeError(f"No task workspace found under {out_dir}")

    def get_latest_mtime(d: Path) -> float:
        try:
            # Check key subdirs first for speed
            sample_files = []
            for sub in ["articles", "subtitles", "textbooks", "notes"]:
                sub_dir = d / sub
                if sub_dir.exists():
                    sample_files.extend(list(sub_dir.glob("*"))[-10:])
            sample_files.extend([d / "manifest.json", d / "parts.json", d])
            return max([f.stat().st_mtime for f in sample_files if f.exists()] or [0.0])
        except Exception:
            return 0.0

    # If pattern specified, filter by pattern
    if pattern:
        matched = [d for d in valid_dirs if pattern in d.name]
        if matched:
            matched.sort(key=get_latest_mtime, reverse=True)
            return matched[0]

    # Default: sort by most recent activity across workspaces
    valid_dirs.sort(key=get_latest_mtime, reverse=True)
    return valid_dirs[0]



AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".flac"}


def load_parts(ws: Path) -> List[Dict]:
    """读取分集清单：parts.json 优先，缺失时按可信度回退，取覆盖面最广者。

    清理过产物的旧工作区（只剩逐字稿与音频、或 manifest 逐集详情不全）仍需可判定进度，
    故依次回退 manifest 逐集详情 → 音频文件名 → 讲义文件名 → 模块规划，全不可用才报错。
    """
    parts_file = ws / "parts.json"
    if parts_file.exists():
        try:
            parts = json.loads(parts_file.read_text(encoding="utf-8"))
            if isinstance(parts, list) and parts:
                return parts
        except Exception:
            pass

    manifest = {}
    manifest_file = ws / "manifest.json"
    if manifest_file.exists():
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    candidates: List[List[Dict]] = []

    # 1) manifest 逐集详情：含真实标题
    details = [d for d in manifest.get("details", []) if isinstance(d, dict) and "page" in d]
    if details:
        candidates.append([
            {"page": d["page"], "title": d.get("title", f"P{d['page']:02d}")} for d in details
        ])

    # 2) 音频文件名：阶段一原料，产物被清理后仍在盘，含真实标题
    audio_dir = ws / "audio"
    if audio_dir.exists():
        found = []
        for f in sorted(audio_dir.glob("P*_*")):
            if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
                continue
            m = re.match(r"P(\d+)_(.+)$", f.stem)
            if m:
                found.append({"page": int(m.group(1)), "title": m.group(2)})
        if found:
            candidates.append(found)

    # 3) 讲义文件名：排除任务书
    articles_dir = ws / "articles"
    if articles_dir.exists():
        found = []
        for f in sorted(articles_dir.glob("P*_*.md")):
            if f.name.endswith("_TASK.md"):
                continue
            m = re.match(r"P(\d+)_(.*?)(?:_精读文章)?\.md$", f.name)
            if m:
                found.append({"page": int(m.group(1)), "title": m.group(2).strip()})
        if found:
            candidates.append(found)

    # 4) 模块规划分集编号：覆盖全量但无逐集标题，仅作兜底
    planned = sorted({
        ep for b in (manifest.get("knowledge_blocks_plan") or [])
        for ep in (b.get("episodes") or [])
    })
    if planned:
        candidates.append([{"page": ep, "title": f"P{ep:02d}"} for ep in planned])

    if candidates:
        return max(candidates, key=len)

    raise FileNotFoundError(
        f"未找到分集清单：{ws} 下 parts.json / manifest.json / audio / articles 均不可用，无法判定进度"
    )


def _load_blocks(ws: Path) -> List[Dict]:
    """读块清单（唯一入口：`AudioMerger.load_manifest`，带版本与条目校验）。

    为什么不再自己读 JSON：曾经放过 v1 旧清单进门（无 span/units/title），而 state_sync/pipeline
    只认 v2——同一份清单在两边判出相反结论。统一走 `load_manifest` 后，任何「不合规清单」
    一律按「没有块清单」处理（提示先 merge-audio 重装），绝不再产生第三种判定口径。
    """
    from src.core.audio_merger import AudioMerger

    manifest = AudioMerger.load_manifest(ws)
    return list(manifest.get("blocks") or []) if manifest else []


def _module_article_path(articles_dir: Path, block: Dict) -> Path:
    """块对应的模块长文路径（唯一命名来源在 `src/core/workspace.py`）。"""
    from src.core.workspace import module_article_path

    return module_article_path(articles_dir, block)


def _find_module_article(articles_dir: Path, block: Dict) -> Optional[Path]:
    """磁盘上已就绪的模块长文（唯一命名来源在 `src/core/workspace.py`）。"""
    from src.core.workspace import find_module_article

    return find_module_article(articles_dir, block)


def scan_status(ws: Path, min_article_bytes: int = 1000) -> Dict:
    """Scans workspace disk state and returns detailed progress statistics.

    完成单位是**块**：一块一篇模块长文（`articles/模块XX_*_精读长文.md`，≥ 1000 字节）。
    集号只作「块覆盖了哪些集」的派生视图。没有块清单的工作区**不做阶段一判定**
    （`stage1_unit="none"`）：旧链路按逐集长文 `PXX_*_精读文章.md` 判进度，那种产物在
    块级链路里已经不存在，照它判定只会永远判「未完成」。
    """
    parts = load_parts(ws)

    articles_dir = ws / "articles"
    subtitles_dir = ws / "subtitles"
    audio_dir = ws / "audio"
    textbooks_dir = ws / "textbooks"
    notes_dir = ws / "notes"

    blocks = _load_blocks(ws)
    blocks_done: List[Dict] = []
    invalid_articles: Dict[int, Any] = {}
    done_pages: Set[int] = set()
    for block in blocks:
        block_id = int(block.get("block_id") or 0)
        article = _find_module_article(articles_dir, block)
        if article is not None:
            blocks_done.append({
                "block_id": block_id,
                "span": block.get("span") or "",
                "title": block.get("title") or "",
                "article": str(article),
            })
            done_pages.update(int(p) for p in (block.get("episodes") or []))
            continue
        # 有文件但没达标（空壳）必须单独报出来：否则「写了但不够」会表现为「什么都没写」。
        # 任务书（*_TASK.md）与长文同目录同前缀，且体积同样远超门禁，必须先排除。
        for cand in sorted(articles_dir.glob(f"模块{block_id:02d}_*.md")):
            if cand.name.endswith("_TASK.md"):
                continue
            try:
                size = cand.stat().st_size
            except OSError:
                break  # 不可访问的条目不进异常清单（避免一门课的坏文件打断整轮统计）
            if size < min_article_bytes:
                invalid_articles[block_id] = (cand, size)
            break

    pending = [p for p in parts if p["page"] not in done_pages]
    stage1_complete = bool(blocks) and len(blocks_done) == len(blocks)

    textbooks = list(textbooks_dir.glob("*.md")) if textbooks_dir.exists() else []
    # 任务书（*_TASK.md）是派发用的临时产物，不计入交付资产，否则会把计数虚高
    notes = (
        [f for f in notes_dir.glob("*.md") if not f.name.endswith("_TASK.md")]
        if notes_dir.exists() else []
    )
    return {
        "workspace": str(ws),
        "workspace_name": ws.name,
        "total": len(parts),
        "completed_count": len(done_pages),
        "pending_count": len(pending),
        "done_pages": sorted(list(done_pages)),
        "pending_parts": pending,
        "parts": parts,
        "audio_dir": str(audio_dir),
        "subtitles_dir": str(subtitles_dir),
        "articles_dir": str(articles_dir),
        "invalid_articles": invalid_articles,
        "textbooks_count": len(textbooks),
        "notes_count": len(notes),
        "blocks_total": len(blocks),
        "blocks_done": blocks_done,
        "stage1_unit": "module" if blocks else "none",
        "is_stage1_complete": stage1_complete,
    }


def scan_transcript_status(ws: Path) -> Dict:
    """块级转录与逐字稿进度。

    为什么单独算一份：块清单（`audio/_blocks/blocks.json`）与逐字稿是**块级链路独有**的产物。
    老工作区（逐集链路）没有它们，这里返回空统计即可，绝不能让它影响 STAGE1_DONE——
    那条门禁的语义始终是「长文是否齐备」，混入逐字稿条件会让全部历史工作区一夜之间不再完工。

    就绪口径是**块**（`BLKxx_*_逐字稿.md` 存在且非空）：写作按块成文，块稿在即语料在。
    `episode_transcripts` 只是「事后按集查阅」时用 split-transcript 切出来的可选产物，
    缺了它不代表转录没做——它不进任何派发门禁。
    """
    from src.core.audio_merger import AudioMerger
    from src.core.transcript_splitter import TranscriptSplitter
    from src.core.workspace import TaskWorkspace, sanitize_filename

    empty = {
        "has_manifest": False,
        "target_minutes": 0.0,
        "ceiling_minutes": 0.0,
        "blocks_total": 0,
        "blocks_transcribed": 0,
        "blocks_pending": 0,
        "blocks": [],
        "transcript_ready": 0,
        "transcript_pending": [],
    }
    try:
        tws = TaskWorkspace.from_existing(ws)
        manifest = AudioMerger.load_manifest(tws)
    except Exception:
        return empty
    if not manifest:
        return empty

    titles = {}
    for part in load_parts(ws):
        if part.get("page") is None:
            continue
        titles[int(part["page"])] = sanitize_filename(str(part.get("title") or f"P{int(part['page']):02d}"))

    blocks_info = []
    ready = 0
    pending_pages = []
    for block in manifest.get("blocks") or []:
        raw = TranscriptSplitter.block_path(tws, block)
        transcribed = raw.exists() and raw.stat().st_size > 0
        episodes = [int(p) for p in (block.get("episodes") or [])]
        paths = {}
        for page in episodes:
            found = TranscriptSplitter.existing_episode_transcript(tws, page, titles.get(page, f"P{page:02d}"))
            paths[page] = str(found) if found else ""
            if found is not None:
                ready += 1
            else:
                pending_pages.append(page)
        blocks_info.append({
            "block_id": int(block.get("block_id") or 0),
            "episodes": episodes,
            "span": AudioMerger.block_span(block),
            "audio": str(Path(tws.root_dir) / str(block.get("audio") or "")),
            "duration_min": float(block.get("duration_min") or 0.0),
            "block_transcript": str(raw),
            "transcribed": transcribed,
            "episode_transcripts": paths,
        })

    transcribed = sum(1 for b in blocks_info if b["transcribed"])
    return {
        "has_manifest": True,
        "target_minutes": float(manifest.get("target_minutes") or 0.0),
        "ceiling_minutes": float(manifest.get("effective_limit_minutes") or 0.0),
        "blocks_total": len(blocks_info),
        "blocks_transcribed": transcribed,
        "blocks_pending": len(blocks_info) - transcribed,
        "blocks": blocks_info,
        "transcript_ready": ready,
        "transcript_pending": sorted(pending_pages),
    }


def _transcribe_payload(tstatus: Dict, n: int) -> List[Dict]:
    """转录角色的取载荷入口：尚未转录的块（含时间表与逐字稿目标路径）。

    只返回**没转录过**的块，因此重跑不会让同一个块被两个转录角色各转录一遍（那是纯烧钱）。
    """
    items: List[Dict] = []
    for block in tstatus.get("blocks") or []:
        if block["transcribed"]:
            continue
        subtitles_dir = Path(block["block_transcript"]).parent
        task_file = subtitles_dir / f"BLK{block['block_id']:02d}_{block['span']}_转录任务书.md"
        items.append({
            "block_id": block["block_id"],
            "span": block["span"],
            "episodes": block["episodes"],
            "duration_min": block["duration_min"],
            "audio_file": block["audio"],
            "audio_exists": Path(block["audio"]).exists(),
            "task_file": str(task_file),
            "task_file_exists": task_file.exists(),
            "block_transcript": block["block_transcript"],
            "episode_transcripts": block["episode_transcripts"],
        })
        if len(items) >= n:
            break
    return items


def _blocks_by_page(tws) -> Dict[int, Dict]:
    """块清单的「集号 → 块」索引；老工作区（无块清单）返回空表。

    写作侧的取音早已收敛到转录角色身上，这里给出块音频与「本集在块内的时间区间」，
    只是让派发载荷能指回语料的出处（备查），不再要求任何人去听。
    """
    if tws is None:
        return {}
    try:
        from src.core.audio_merger import AudioMerger

        manifest = AudioMerger.load_manifest(tws) or {}
    except Exception:
        return {}
    index: Dict[int, Dict] = {}
    for block in manifest.get("blocks") or []:
        for page in block.get("episodes") or []:
            index[int(page)] = block
    return index


def _budget_summary(status: Dict) -> Dict:
    """阶段一预算与派发建议（系数/窗口可用环境变量覆盖，见 src/core/budget.py）。"""
    from src.core import budget

    total_sec = sum(float(p.get("duration", 0) or 0) for p in status.get("parts", []))
    prefills = [
        budget.est_episode_prefill_tokens(p.get("duration", 0) or 0)
        for p in status.get("pending_parts", [])
    ]
    info = budget.describe(
        total_seconds=total_sec,
        episodes=status.get("total", 0),
        pending=status.get("pending_count", 0),
        prefills=prefills,
    )
    info["total_audio_min"] = total_sec / 60.0
    info["total_audio_hours"] = total_sec / 3600.0
    return info


def _module_payload(ws: Path, n: int) -> List[Dict]:
    """模块长文的派发载荷（写作侧取载荷入口）：只返回「块逐字稿已就绪且模块长文缺失」的块。

    一个块一篇、读块逐字稿写——这是块级链路对写作角色的全部约束，载荷里给全路径与语料状态，
    主 Agent 不需要自己拼文件名。
    """
    items: List[Dict] = []
    articles_dir = ws / "articles"
    subtitles_dir = ws / "subtitles"
    for block in _load_blocks(ws):
        block_id = int(block.get("block_id") or 0)
        # span 兜底走 block_span（units 标签 → 集号区间），手写清单缺 span 时也能对上真实稿名
        span = str(block.get("span") or AudioMerger.block_span(block))
        transcript = subtitles_dir / f"BLK{block_id:02d}_{span}_逐字稿.md"
        if not transcript.exists() or transcript.stat().st_size <= 0:
            continue
        if _find_module_article(articles_dir, block) is not None:
            continue
        target = _module_article_path(articles_dir, block)
        items.append({
            "block_id": block_id,
            "span": span,
            "title": str(block.get("title") or ""),
            "episodes": block.get("episodes") or [],
            "duration_min": float(block.get("duration_min") or 0.0),
            "block_audio": str(ws / str(block.get("audio") or "")),
            "transcript_file": str(transcript),
            "transcript_bytes": transcript.stat().st_size,
            "task_file": str(articles_dir / f"{target.name[:-len('_精读长文.md')]}_TASK.md"),
            "target_article": str(target),
        })
        if len(items) >= n:
            break
    return items


def _log_dispatch(status: Dict, requested: int, payload: List[Dict], budget_info: Dict) -> None:
    """把本次建议派发的**块**追加写入 <task>/.dispatch_log.jsonl（派发台账，观察性证据）。

    注意：本台账记录的是「工具建议派发了哪些块」，不是「谁真的写了」——执行者身份
    无法在工具层验证；它的用途是事后复盘派发节奏（例如某工作区从未出现台账，
    说明阶段一没有走派发流程）。
    """
    log_path = Path(status["workspace"]) / ".dispatch_log.jsonl"
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "requested": requested,
        "suggested_blocks": [int(i.get("block_id") or 0) for i in payload],
        "suggest_workers": budget_info.get("suggest_workers"),
        "suggest_batch": budget_info.get("suggest_batch"),
        "audio_tokens_per_sec": budget_info.get("audio_tokens_per_sec"),
    }
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as err:
        print(f"[!] 派发台账写入失败: {err}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Real-time Dynamic Queue Tracker (Sliding Window Dispatch)")
    parser.add_argument("--dir", default=None, help="Path to task workspace")
    parser.add_argument("--base-dir", default=None,
                        help="产物根（默认：由 src/core/paths.py 解析——默认 <当前工作目录>/output，在容器内工作时为 <容器根>/output）")
    parser.add_argument("--pattern", default=None, help="Workspace directory name keyword filter")
    parser.add_argument("--next-module", type=int, default=0, dest="next_module_n",
                        help="写作侧取载荷（块级链路）：只返回「块逐字稿已就绪且模块长文缺失」的块")
    parser.add_argument("--next-transcribe", type=int, default=0, dest="next_transcribe_n",
                        help="转录侧取载荷：返回尚未转录的块（含块音频、块内时间表与逐字稿目标路径）")
    parser.add_argument("--log-dispatch", action="store_true", dest="log_dispatch",
                        help="把本次建议派发的块追加写入 <task>/.dispatch_log.jsonl（派发台账；默认关闭）")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument("--summary", action="store_true", help="Output one-line summary for scripting")
    args = parser.parse_args()

    try:
        ws = get_task_workspace(args.dir, pattern=args.pattern, base_dir=args.base_dir)
        status = scan_status(ws)
        tstatus = scan_transcript_status(ws)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    if args.summary:
        budget = _budget_summary(status)
        print(
            f"TOTAL={status['total']};DONE={status['completed_count']};PENDING={status['pending_count']};"
            f"STAGE1_DONE={'1' if status['is_stage1_complete'] else '0'};"
            f"TOTAL_AUDIO_MIN={budget['total_audio_min']:.0f};"
            f"DISPATCH_REQUIRED={'1' if budget['dispatch_required'] else '0'};"
            f"SUGGEST_WORKERS={budget['suggest_workers']};"
            f"SUGGEST_BATCH={budget['suggest_batch']};"
            f"AUDIO_TOKENS_PER_SEC={budget['audio_tokens_per_sec']:g};"
            # 块级转录进度：老工作区（无块清单）会得到全 0，不影响 STAGE1 判定
            f"BLOCKS={tstatus['blocks_total']};"
            f"BLOCKS_TRANSCRIBED={tstatus['blocks_transcribed']};"
            f"BLOCKS_PENDING={tstatus['blocks_pending']};"
            # 转录就绪按块计（分集稿是可选切分产物，不计入）
            f"TRANSCRIPT_READY={tstatus['blocks_transcribed']};"
            f"TRANSCRIPT_PENDING={tstatus['blocks_pending']}"
        )
        return

    # 取载荷：转录侧（块）与写作侧（模块长文）互斥，各自只返回「还没做完」的那批
    transcribe_payload: List[Dict] = []
    if args.next_transcribe_n > 0:
        transcribe_payload = _transcribe_payload(tstatus, args.next_transcribe_n)
    payload: List[Dict] = []
    if args.next_module_n > 0:
        payload = _module_payload(Path(status["workspace"]), args.next_module_n)

    if args.json:
        out = {
            "workspace": status["workspace_name"],
            "workspace_path": status["workspace"],
            "total": status["total"],
            "completed": status["completed_count"],
            "pending": status["pending_count"],
            "is_stage1_complete": status["is_stage1_complete"],
            "budget": _budget_summary(status),
            "transcript": {
                "has_manifest": tstatus["has_manifest"],
                "target_minutes": tstatus["target_minutes"],
                "ceiling_minutes": tstatus["ceiling_minutes"],
                "blocks_total": tstatus["blocks_total"],
                "blocks_transcribed": tstatus["blocks_transcribed"],
                "blocks_pending": tstatus["blocks_pending"],
                "transcript_ready": tstatus["transcript_ready"],
                "transcript_pending": tstatus["transcript_pending"],
            },
            "next": payload,
            "next_transcribe": transcribe_payload,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if args.log_dispatch and payload:
            _log_dispatch(status, len(payload), payload, out["budget"])
        return

    print("=" * 68)
    print(f"[*] 动态任务队列追踪器 (Workspace: {status['workspace_name']})")
    print(f"[*] 总分集数: {status['total']} | 已完工: {status['completed_count']} | 待处理: {status['pending_count']}")
    pct = (status['completed_count'] / status['total']) * 100 if status['total'] else 0
    print(f"[*] 阶段一单集进度: {pct:.1f}% [{status['completed_count']}/{status['total']}]")
    if tstatus["has_manifest"]:
        print(f"[*] 块级转录进度: 块 {tstatus['blocks_transcribed']}/{tstatus['blocks_total']} 已转录"
              f"（块目标 {tstatus['target_minutes']:g} 分钟 / 上限 {tstatus['ceiling_minutes']:g} 分钟）"
              f" | 逐字稿就绪 {tstatus['transcript_ready']} 集 | 待转录 {tstatus['blocks_pending']} 块")
    print(f"[*] 阶段二模块资产: 模块全书 {status['textbooks_count']} 部 | 复习笔记 {status['notes_count']} 篇")
    status_label = "【已竣工 - 可放行进入阶段二模块整编】" if status["is_stage1_complete"] else "【阶段一动态滑动流水线进行中】"
    print(f"[*] 当前阶段状态: {status_label}")
    budget = _budget_summary(status)
    print(f"[*] 派发建议: {'必须派发' if budget['dispatch_required'] else '可主 Agent 串行（≤60 分钟）'}"
          f" | 并发 {budget['suggest_workers']} | 打包粒度 {budget['suggest_batch']} 集/子智能体"
          f" | 音频系数 {budget['audio_tokens_per_sec']:g} tok/s | 窗口 {budget['context_window_tokens']:,}")
    if status.get("invalid_articles"):
        print("\n[!] 发现异常过短文章（低于 1000 字节门禁，已自动重置为待办）：")
        for p_num, (f, sz) in status["invalid_articles"].items():
            print(f"    - P{p_num:02d}: {f.name} (仅 {sz} 字节)")
    print("=" * 68)

    if transcribe_payload:
        print(f"\n【待转录的块 Next {len(transcribe_payload)} 个】：")
        for item in transcribe_payload:
            print(f"  • BLK{item['block_id']:02d} {item['span']} [{item['duration_min']:.1f}m /"
                  f" {len(item['episodes'])} 集]: {item['audio_file']}")
            print(f"    - 转录任务书: {item['task_file']}")
            print(f"    - 块级逐字稿: {item['block_transcript']}")
            print(f"    - 切分后产出: {len(item['episode_transcripts'])} 份分集逐字稿")

    if payload:
        print(f"\n【待派发模块长文 Next {len(payload)} 个块（一个块一篇）】：")
        for item in payload:
            print(f"  • BLK{item['block_id']:02d} {item['span']} [{item['duration_min']:.1f}m /"
                  f" {len(item['episodes'])} 集]: {item['title']}")
            print(f"    - 任务书:  {item['task_file']}")
            print(f"    - 逐字稿:  {item['transcript_file']}（{item['transcript_bytes']:,} 字节）")
            print(f"    - 块音频:  {item['block_audio']}")
            print(f"    - 目标长文: {item['target_article']}")
        if args.log_dispatch:
            print(f"\n[i] 已追加派发台账: {Path(status['workspace']) / '.dispatch_log.jsonl'}")

    if args.log_dispatch and payload and not args.json:
        _log_dispatch(status, len(payload), payload, budget)


if __name__ == "__main__":
    main()
