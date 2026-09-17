"""把块级逐字稿切回「一集一份」的内核（块级转录流水线的第二步）。

**为什么必须切回去**：块只是为了少调用几次取音接口，长文仍是一集一篇、下游（模块规划 /
教材分册 / 笔记 / 思维导图）仍按集号锚定。所以转录完必须把块级文本按 `blocks.json` 里的
时间表还原成分集逐字稿，`subtitles/PXX_*_逐字稿.md` 就是转录与写作两类角色之间的**接口**。

**切分为什么不能靠猜**：块内每集的起止时间是合并时实测出来的（见 `audio_merger`），
只要逐字稿带时间戳，切分就是**机械的、确定性的**——不依赖模型理解，也不会有语义漂移。
拿不到时间戳时**不硬切**：落块级逐字稿并标记 `unsplit`，由写作角色照着时间表自己定位，
门禁据此区分「确定性切分」与「人工定位」，避免把错位的文本当成分集语料喂给下游。
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .audio_chunker import AudioChunker

# 行首时间戳：`[05:20]` / `[00:05:20]` / `[320]`，也兼容全角方括号与紧贴正文的写法。
_LEADING_TS_RE = re.compile(
    r"^\s*[\[【]\s*(?P<stamp>(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?|\d{1,5})\s*[\]】]\s*(?P<rest>.*)$"
)
# 同一行里出现了时间戳（可能不在行首）——只用来统计，不用它改判归属。
_ANY_TS_RE = re.compile(r"[\[【]\s*(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?\s*[\]】]")

# 边界认定容差：集交界处若有时间戳落在 ±该秒数内，认为这条边界被「锚定」住了。
BOUNDARY_TOLERANCE_SEC = 120.0

# 内容配比阈值：某集分到的字符占比 ≥ 它的时长占比 × 该倍数时，块边界视为可疑。
# 用来抓「时间戳只标在话题转换处」这类稀疏标注下的静默过度归属——没有时间戳的行会一直归到
# 上一个时间段，于是下一集整集内容被上一集吞掉（实测某块 5 集只切出 2 集，其中一集混入
# 178 行他集内容）。阈值取 1.5 是「宁可多提醒」：命中后整块标 suspect，不进入写作派发。
OVER_ASSIGN_RATIO = 1.5


class TranscriptSplitter:
    """块级逐字稿 → 分集逐字稿。切分幂等，可重复执行。"""

    SUFFIX = "_逐字稿.md"
    SUSPECT_SUFFIX = ".suspect.json"

    # ------------------------------------------------------------------
    # 时间戳解析
    # ------------------------------------------------------------------

    @staticmethod
    def parse_stamp(stamp: str) -> Optional[float]:
        """把 `05:20` / `00:05:20` / `320` 解析成秒；无法解析返回 None。"""
        raw = str(stamp or "").strip().replace(",", ".")
        if not raw:
            return None
        parts = raw.split(":")
        try:
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                # 两段一律按 MM:SS 解释：转录提示词示例给的就是 `[05:20]` 这种两段式
                return float(parts[0]) * 60 + float(parts[1])
            if len(parts) == 1:
                return float(parts[0])
        except (TypeError, ValueError):
            return None
        return None

    @classmethod
    def leading_timestamp(cls, line: str) -> Optional[float]:
        """取行首时间戳的秒数；没有行首时间戳返回 None。"""
        match = _LEADING_TS_RE.match(line or "")
        if not match:
            return None
        return cls.parse_stamp(match.group("stamp"))

    # ------------------------------------------------------------------
    # 切分（纯函数，便于自检直接断言）
    # ------------------------------------------------------------------

    @classmethod
    def split(cls, text: str, segments: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """按 `segments` 的时间表把块级逐字稿切成每集一份。

        返回：
        * `mode`    —— `timestamp`（机械切分成功）/ `unsplit`（无时间戳，未切分）
        * `buckets` —— `{集号: [行, ...]}`，`unsplit` 时为空
        * `stats`   —— 命中统计（时间戳数、每集行数、被锚定的边界数）
        * `diag`    —— 诊断行，由调用方决定打印
        """
        ordered = sorted(
            [seg for seg in segments if seg and seg.get("page") is not None],
            key=lambda seg: float(seg.get("start_sec") or 0.0),
        )
        diag: List[str] = []
        stats: Dict[str, Any] = {
            "timestamps": 0,
            "inline_timestamps": 0,
            "boundaries": max(0, len(ordered) - 1),
            "anchored_boundaries": 0,
            "lines": 0,
        }
        if not ordered:
            diag.append("[!] 块时间表为空，无法切分逐字稿")
            return {"mode": "unsplit", "buckets": {}, "stats": stats, "diag": diag}

        pages = [int(seg["page"]) for seg in ordered]
        starts = [float(seg.get("start_sec") or 0.0) for seg in ordered]
        ends = [float(seg.get("end_sec") or 0.0) for seg in ordered]

        buckets: Dict[int, List[str]] = {page: [] for page in pages}
        preamble: List[str] = []
        seen: List[float] = []
        current_page: Optional[int] = None
        current_sec: Optional[float] = None

        for raw_line in (text or "").splitlines():
            stats["lines"] += 1
            stamp = cls.leading_timestamp(raw_line)
            body = raw_line
            if stamp is not None:
                stats["timestamps"] += 1
                seen.append(stamp)
                current_sec = stamp
                match = _LEADING_TS_RE.match(raw_line)
                body = (match.group("rest") if match else raw_line).rstrip()
                # 落在哪一集：取最后一个 start<=t 的集
                index = bisect_right(starts, stamp + 1e-6) - 1
                if index < 0:
                    index = 0
                elif index >= len(pages):
                    index = len(pages) - 1
                current_page = pages[index]
            elif _ANY_TS_RE.search(raw_line):
                stats["inline_timestamps"] += 1

            if current_page is None:
                # 第一个时间戳之前的开场白，归到首集
                if raw_line.strip():
                    preamble.append(raw_line)
                continue
            buckets[current_page].append(body)

        mode = "timestamp" if stats["timestamps"] > 0 else "unsplit"
        if mode == "unsplit":
            diag.append(
                "[!] 逐字稿里没有可用时间戳，不做机械切分（落块级逐字稿并标记 unsplit，"
                "由写作角色照块时间表定位）"
            )
            return {"mode": "unsplit", "buckets": {}, "stats": stats, "diag": diag}

        # 边界锚定检查：交界附近有时间戳才算「切得准」，否则是顺着上一段时间插值出来的
        for index in range(1, len(ordered)):
            boundary = starts[index]
            if any(abs(stamp - boundary) <= BOUNDARY_TOLERANCE_SEC for stamp in seen):
                stats["anchored_boundaries"] += 1
            else:
                diag.append(
                    f"[!] P{pages[index]:02d} 的起点（块内 {AudioChunker.format_seconds(boundary)}）"
                    f"附近没有时间戳，该处切分由前后时间戳插值推定"
                )
        stats["anchored_ratio"] = (
            round(stats["anchored_boundaries"] / stats["boundaries"], 3) if stats["boundaries"] else 1.0
        )

        if preamble:
            first = pages[0]
            buckets[first] = preamble + buckets[first]

        # 内容配比体检：分到的内容远超自己的时长占比 → 它把邻集内容吃进来了（见 OVER_ASSIGN_RATIO）
        durations: Dict[int, float] = {}
        for seg in ordered:
            page = int(seg["page"])
            span = float(seg.get("duration_sec") or 0.0)
            if span <= 0:
                span = max(0.0, float(seg.get("end_sec") or 0.0) - float(seg.get("start_sec") or 0.0))
            durations[page] = span
        chars = {page: sum(len(line or "") for line in buckets[page]) for page in pages}
        stats["content_chars"] = chars
        stats["over_assigned"] = []
        total_dur = sum(durations.values())
        total_chars = sum(chars.values())
        if total_dur > 0 and total_chars > 0:
            for page in pages:
                duration_share = durations[page] / total_dur
                content_share = chars[page] / total_chars
                if duration_share > 0 and content_share / duration_share >= OVER_ASSIGN_RATIO:
                    ratio = content_share / duration_share
                    stats["over_assigned"].append({
                        "page": page,
                        "duration_share": round(duration_share, 3),
                        "content_share": round(content_share, 3),
                        "ratio": round(ratio, 2),
                    })
                    diag.append(
                        f"[!] P{page:02d} 分到 {content_share:.0%} 的内容、却只占 {duration_share:.0%} 的时长"
                        f"（{ratio:.1f}×）：多半是相邻集没有时间戳，内容被它吃进来了"
                    )

        empty = [page for page in pages if not [line for line in buckets[page] if line.strip()]]
        if empty:
            diag.append(f"[!] 以下集切分后没有内容，需重转录或改由块级稿定位：{empty}")
        if empty or stats["over_assigned"]:
            diag.append(
                "[i] 成因：转录时模型没做到「每个自然段都标时间戳」。按转录任务书 2.1 节重读该块"
                "（把要求说重一点）后重跑 split-transcript，即可恢复确定性切分"
            )

        return {"mode": mode, "buckets": buckets, "stats": stats, "diag": diag}

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    @staticmethod
    def _prefix_for(page: int, clean_title: str) -> str:
        """与 `export_article_task` 同一套前缀规则：标题已带 `Pxx_` 就不重复加。"""
        return "" if re.match(r"^P\d{2}_", clean_title) else f"P{page:02d}_"

    @classmethod
    def episode_path(cls, ws: Any, page: int, clean_title: str) -> Path:
        """分集逐字稿的**正式**路径（`subtitles/` 是逐字稿的正式存放位置）。"""
        return Path(ws.subtitles_dir) / f"{cls._prefix_for(page, clean_title)}{clean_title}{cls.SUFFIX}"

    @classmethod
    def suspect_path(cls, ws: Any, page: int, clean_title: str) -> Path:
        """可疑分集稿的旁路标记；文件本身保留，但不得被当作 ready 派发。"""
        episode = cls.episode_path(ws, page, clean_title)
        return episode.with_name(episode.name + cls.SUSPECT_SUFFIX)

    @staticmethod
    def _is_suspect(path: Path) -> bool:
        return path.with_name(path.name + TranscriptSplitter.SUSPECT_SUFFIX).is_file()

    @classmethod
    def legacy_path(cls, ws: Any, page: int, clean_title: str) -> Path:
        """历史语料路径 `PXX_<标题>_clean.txt`（人工清洗稿或旧链路的逐集文本）。"""
        return Path(ws.subtitles_dir) / f"{cls._prefix_for(page, clean_title)}{clean_title}_clean.txt"

    @classmethod
    def existing_episode_transcript(
        cls, ws: Any, page: int, clean_title: str
    ) -> Optional[Path]:
        """本集**已有**的可用逐字稿：优先新链路产物，其次历史 `_clean.txt` 语料。

        为什么要认历史语料：本改造之前的工作区里已经有 `subtitles/PXX_*_clean.txt`（人工清洗
        稿或旧链路逐集文本，实测某课 64 份）。它们与逐字稿同义，直接拿来写长文即可——没必要
        为了「统一格式」把已经存在的语料再转录一遍（那是纯烧钱）。返回 None 表示尚无逐字稿，
        该集必须先等转录。
        """
        for candidate in (cls.episode_path(ws, page, clean_title), cls.legacy_path(ws, page, clean_title)):
            try:
                if candidate.exists() and candidate.stat().st_size > 0 and not cls._is_suspect(candidate):
                    return candidate
            except OSError:
                continue
        return None

    @classmethod
    def block_path(cls, ws: Any, block: Dict[str, Any]) -> Path:
        """块级原始逐字稿的路径。

        优先用「块号 + 集号区间」（`BLK03_P18-P22_逐字稿.md`）：它只取决于清单里的块结构，
        与块音频落在哪无关。这一点在**无收益装箱**（每块仅一集、块音频直接指向该集原音频）时
        尤其重要——否则块级稿会跟分集稿同名（都成 `P08_标题_逐字稿.md`）而互相覆盖。
        清单缺块号/集号时退回按音频文件名取名（兼容手写的旧清单）。
        """
        block_id = int(block.get("block_id") or 0)
        pages = sorted(int(p) for p in (block.get("episodes") or []))
        if block_id and pages:
            span = f"P{pages[0]:02d}" if len(pages) == 1 else f"P{pages[0]:02d}-P{pages[-1]:02d}"
            return Path(ws.subtitles_dir) / f"BLK{block_id:02d}_{span}{cls.SUFFIX}"
        stem = Path(str(block.get("audio") or "")).stem or f"BLK{block_id:02d}"
        return Path(ws.subtitles_dir) / f"{stem}{cls.SUFFIX}"

    @classmethod
    def write_block_transcript(cls, ws: Any, block: Dict[str, Any], text: str) -> Path:
        """落块级原始逐字稿。无论能否切分都留一份，它是可溯源的原始事实。"""
        path = cls.block_path(ws, block)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @classmethod
    def write_episode_transcripts(
        cls,
        ws: Any,
        block: Dict[str, Any],
        text: str,
        titles: Optional[Dict[int, str]] = None,
        skip_existing: bool = True,
    ) -> Dict[str, Any]:
        """切分并落分集逐字稿，返回 `{status, mode, files, stats, diag}`。

        `skip_existing` 让重跑具备幂等性：已存在的分集逐字稿不重写（除非内容为空）。
        """
        titles = titles or {}
        segments = block.get("segments") or []
        outcome = cls.split(text, segments)
        diag: List[str] = list(outcome["diag"])
        files: Dict[int, str] = {}
        status = "split" if outcome["mode"] == "timestamp" else "unsplit"
        suspect_pages: List[int] = []

        block_file = cls.write_block_transcript(ws, block, text)
        diag.append(f"[i] 块级逐字稿已落盘：{block_file.name}")

        if outcome["mode"] != "timestamp":
            return {
                "status": "unsplit",
                "mode": "unsplit",
                "files": {},
                "suspect_pages": [],
                "block_file": str(block_file),
                "stats": outcome["stats"],
                "diag": diag,
            }

        empty_pages = [
            int(seg["page"])
            for seg in segments
            if not [line for line in outcome["buckets"].get(int(seg["page"]), []) if line.strip()]
        ]
        if empty_pages or outcome["stats"]["over_assigned"]:
            # 只要边界不可信，就不把一个块里的任何一集当作 ready。错误归属可能污染
            # 多个相邻集，局部放行会把错语料送进写作链路；已有文件保留供排障，但用
            # sidecar 标记屏蔽，重新得到可靠时间戳后再真实重切并解除标记。
            suspect_pages = sorted({int(seg["page"]) for seg in segments})
            payload = {
                "block_id": int(block.get("block_id") or 0),
                "pages": suspect_pages,
                "empty_pages": empty_pages,
                "over_assigned": outcome["stats"]["over_assigned"],
                "reason": "empty_or_over_assigned",
            }
            for page in suspect_pages:
                clean_title = str(titles.get(page) or f"P{page:02d}")
                marker = cls.suspect_path(ws, page, clean_title)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            diag.append(
                "[!] 本块分集边界不可靠，已标记 suspect；"
                "该块所有分集稿暂不进入写作派发，重转录后重跑 split-transcript 会自动解除"
            )
            return {
                "status": "suspect",
                "mode": outcome["mode"],
                "files": {},
                "suspect_pages": suspect_pages,
                "block_file": str(block_file),
                "stats": outcome["stats"],
                "diag": diag,
            }

        for seg in sorted(segments, key=lambda s: float(s.get("start_sec") or 0.0)):
            page = int(seg["page"])
            clean_title = str(titles.get(page) or f"P{page:02d}")
            lines = [line for line in outcome["buckets"].get(page, []) if line.strip()]
            if not lines:
                diag.append(f"[!] P{page:02d} 切分后无内容，未落盘（需重转录该块或用块级稿补）")
                status = "partial"
                continue
            path = cls.episode_path(ws, page, clean_title)
            marker = cls.suspect_path(ws, page, clean_title)
            if skip_existing and path.exists() and path.stat().st_size > 0 and not marker.exists():
                files[page] = str(path)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            start = str(seg.get("start") or AudioChunker.format_seconds(float(seg.get("start_sec") or 0.0)))
            end = str(seg.get("end") or AudioChunker.format_seconds(float(seg.get("end_sec") or 0.0)))
            header = (
                f"# P{page:02d} {clean_title} 逐字稿\n\n"
                f"> 来源：块 BLK{int(block.get('block_id') or 0):02d}"
                f"（P{min(int(s['page']) for s in segments):02d}-P{max(int(s['page']) for s in segments):02d}）"
                f"转录后按块内时间表切分；本集对应块内 {start}-{end}。\n"
                f"> 用途：撰写本集长文的**唯一事实依据**。只依据本文件写，不得引入本文件之外的内容。\n\n"
                f"---\n\n"
            )
            path.write_text(header + "\n".join(lines).rstrip() + "\n", encoding="utf-8")
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
            files[page] = str(path)

        if files:
            diag.append(
                f"[✓] P{min(files):02d}\u2013P{max(files):02d} 共切出 {len(files)} 份分集逐字稿"
                f"（边界锚定 {outcome['stats']['anchored_boundaries']}/{outcome['stats']['boundaries']}）"
            )
        else:
            diag.append("[!] 没有任何一集切分成功")
        return {
            "status": status,
            "mode": outcome["mode"],
            "files": files,
            "suspect_pages": suspect_pages,
            "block_file": str(block_file),
            "stats": outcome["stats"],
            "diag": diag,
        }
