#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕优先阶段服务。

只消费 BlockPlan 与 parts 元数据，不读取物理音频。每个分集字幕只请求一次；完整块
直接写块级逐字稿，缺字幕块统一返回 ``needs_audio``，由编排层按需物化。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import subtitles as subtitle_core
from .workspace import TaskWorkspace


class SubtitleService:
    """把 B 站字幕转换为已锁定 BlockPlan 的块级逐字稿。"""

    @staticmethod
    def _parts_by_page(parts: Sequence[Mapping[str, Any]]) -> Dict[int, Mapping[str, Any]]:
        return {
            int(p["page"]): p
            for p in parts
            if isinstance(p, Mapping) and p.get("page") is not None
        }

    @staticmethod
    def _course_bvid(parts: Sequence[Mapping[str, Any]]) -> str:
        return subtitle_core.resolve_course_bvid(list(parts))

    @classmethod
    def run(
        cls,
        ws: TaskWorkspace,
        info: Mapping[str, Any],
        plan: Mapping[str, Any],
        *,
        sessdata: Optional[str] = None,
        force: bool = False,
        allow_downgrade: bool = False,
    ) -> Dict[str, Any]:
        """执行字幕阶段，返回稳定的块级状态集合。

        `allow_downgrade=False`（默认）时，`--force` **不会**用字幕稿覆盖已有的听音稿：
        听音稿由子 agent 逐段听写并整理成通顺段落，质量高于平台字幕碎片（实测被覆盖过
        45721 → 53923 字节，繁体成段变成简体逐句时间戳，且不可逆）。这是**单向质量约束**：
        高质 → 低质必须显式授权。反方向的升级（听音稿覆盖字幕稿）不受此限，但代码侧没有
        写听音稿的路径，所以实际上只会发生"字幕补空缺"。
        """
        blocks = [b for b in (plan.get("blocks") or []) if isinstance(b, Mapping)]
        parts = cls._parts_from_plan(plan)
        result: Dict[str, Any] = {
            "subtitle_ready": [],
            "cached": [],
            "needs_audio": [],
            "written": [],
            "errors": [],
            "kept_audio": [],
        }
        block_ids = [int(b.get("block_id") or 0) for b in blocks]
        if not sessdata:
            result["needs_audio"] = block_ids
            result["reason"] = "missing_sessdata"
            return result

        course_bvid = cls._course_bvid(parts)
        if not course_bvid:
            result["errors"].append({
                "reason": "missing_bvid",
                "message": "parts.json 没有可解析的 B 站稿件号",
            })
            result["needs_audio"] = block_ids
            return result

        parts_by_page = cls._parts_by_page(parts)
        cache: Dict[int, Optional[Dict[str, Any]]] = {}
        # **续跑**：上一轮已取到字幕的那些分集，其原始 cues 存在块旁的 sidecar 里。
        # 不带 --force 时直接复用它们，只重取缺的那几集——否则每次重跑都要把块内全部
        # 分集重新抽一遍签（10 轮重试 + 最坏 67.5s 退避，一块能到几分钟）。
        if not force:
            cache.update(cls._载入已取字幕(ws, blocks))
        # 分集级取回结论（页面 → 诊断）：判不可用的页要把原因留下，别让"本来没有字幕"
        # 与"拿到的不是本集字幕（串台）"两种结局在产物里长得一样。
        page_diag: Dict[int, Dict[str, Any]] = {}
        参照正文 = cls._reference_texts(ws)
        本次正文: List[str] = []
        课程术语: List[str] = []
        参照阈值 = subtitle_core.COURSE_TERMS_MIN_PAGES
        for block in blocks:
            block_id = int(block.get("block_id") or 0)
            target = TaskWorkspace.block_path(ws, dict(block))
            # 本块自己的旧稿不进参照：`--force` 重取时它就在工作区里，而被重取往往正是
            # 因为怀疑它串台——拿它当"课程高频术语"的参照源，等于用可疑样本证明本次取回
            # 的可信，是自我强化。参照只取**其它**块的稿子。
            own = str(target)
            参照他块 = [t for p, t in 参照正文.items() if p != own] if isinstance(参照正文, dict) else list(参照正文)
            if target.exists() and target.stat().st_size > 0 and not force:
                result["cached"].append(block_id)
                result["subtitle_ready"].append(block_id)
                continue
            # 单向质量约束：`--force` 不得把听音稿降级成字幕稿。
            # `mixed`（逐集混合稿）走同一保守分支：它的听音段无法用字幕重建，
            # 覆盖一次就永久丢掉那几集的原声转录。
            if (
                force
                and not allow_downgrade
                and target.exists()
                and target.stat().st_size > 0
                and subtitle_core.transcript_source(target) in ("audio", "mixed")
            ):
                result.setdefault("kept_audio", []).append({
                    "block_id": block_id,
                    "path": str(target),
                    "reason": "已有听音/混合稿，拒绝用字幕稿覆盖（要覆盖请加 --allow-downgrade）",
                })
                result["cached"].append(block_id)
                result["subtitle_ready"].append(block_id)
                continue

            page_values: Dict[int, Optional[Dict[str, Any]]] = {}
            for segment in block.get("segments") or []:
                page = int(segment.get("page") or 0)
                if page not in cache:
                    # 课程术语表（身份判据第二票）：工作区其它块既有逐字稿 + 本次已取回正文，
                    # 参照数每翻一倍重建一次，避免逐页全量重算。
                    参照数 = len(参照他块) + len(本次正文)
                    if 参照数 >= 参照阈值:
                        课程术语 = subtitle_core.course_terms(参照他块 + 本次正文)
                        参照阈值 = max(subtitle_core.COURSE_TERMS_MIN_PAGES, 参照数 * 2)
                    part = parts_by_page.get(page)
                    if not part or part.get("cid") is None:
                        cache[page] = None
                    else:
                        bvid = str(part.get("bvid") or "").strip() or course_bvid
                        duration = float(part.get("duration") or 0.0)
                        # title 是身份判据的锚点（标题术语必须出现在正文里）：串台字幕
                        # 的正文来自别的稿件，标题术语一次都不命中，据此整页判不可用。
                        title = str(part.get("title") or "").strip()
                        diagnostic: Dict[str, Any] = {}
                        try:
                            cache[page] = subtitle_core.fetch_episode_subtitle(
                                bvid,
                                int(part["cid"]),
                                sessdata=sessdata,
                                keys_file=getattr(ws, "wbi_keys_file", None),
                                duration_sec=duration,
                                title=title,
                                课程术语=课程术语,
                                诊断=diagnostic,
                            )
                        except Exception as err:
                            result["errors"].append({
                                "block_id": block_id,
                                "page": page,
                                "message": str(err),
                            })
                            cache[page] = None
                        page_diag[page] = dict(diagnostic, title=title)
                        if cache[page] is None:
                            result.setdefault("unavailable", []).append({
                                "page": page,
                                "title": title,
                                "reason": diagnostic.get("reason") or "无中文字幕",
                                "kind": diagnostic.get("kind") or "",
                                "attempts": int(diagnostic.get("attempts") or 0),
                            })
                        else:
                            本次正文.append("".join(
                                str(条.get("content") or "") for 条 in cache[page].get("cues") or []
                            ))
                page_values[page] = cache[page]

            # 按集兜底：取到字幕的集照常拼装，缺的集只记账。`allow_partial=True` 让
            # 「整块作废」变成「部分稿 + 缺页清单」——只有缺的那几集需要听音补录。
            assembled = subtitle_core.assemble_block_transcript(
                dict(block), page_values, allow_partial=True)
            missing_pages = assembled.get("missing_pages") or []
            result.setdefault("page_sources", {})[block_id] = dict(assembled.get("page_sources") or {})

            if missing_pages:
                # 部分稿落**暂存名**（不带 `_逐字稿.md` 后缀）：`block_path` / `_transcript_exists`
                # / `queue_tracker` 的「文件存在即整块完成」三处判定因此都不受影响——
                # 最终名只在补录齐备并合并后才出现。
                partial_path = TaskWorkspace.partial_transcript_path(ws, dict(block))
                partial_path.parent.mkdir(parents=True, exist_ok=True)
                partial_path.write_text(assembled["text"], encoding="utf-8")
                ready_pages = sorted(p for p, k in (assembled.get("page_sources") or {}).items()
                                     if k == "subtitle")
                state_path = TaskWorkspace.partial_state_path(ws, dict(block))
                state_path.write_text(json.dumps(
                    {"block_id": block_id, "span": str(block.get("span") or ""),
                     "ready_pages": ready_pages, "missing_pages": missing_pages,
                     # 存下已取到那些集的**原始 cues**：续跑时据此跳过重取。合并完成后
                     # sidecar 会被删掉，所以它只是暂存原料，不是长期产物。
                     "cues": {str(p): page_values[p] for p in ready_pages if page_values.get(p)}},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                result.setdefault("partial", []).append({
                    "block_id": block_id,
                    "path": str(partial_path),
                    "state_path": str(state_path),
                    "ready_pages": ready_pages,
                    "missing_pages": missing_pages,
                })
                result["needs_audio"].append(block_id)
                result.setdefault("missing", []).append({
                    "block_id": block_id,
                    "missing_pages": missing_pages,
                    "reason": assembled.get("reason") or "缺中文字幕的分集",
                    "partial_path": str(partial_path),
                    "details": [
                        {"page": page,
                         "reason": (page_diag.get(page) or {}).get("reason") or "无可用中文字幕",
                         "kind": (page_diag.get(page) or {}).get("kind") or ""}
                        for page in missing_pages
                    ],
                })
                continue

            TaskWorkspace.write_block_transcript(ws, dict(block), assembled["text"])
            result["subtitle_ready"].append(block_id)
            result["written"].append({
                "block_id": block_id,
                "path": str(target),
                "kind": assembled.get("kind_label") or "",
            })

        cls._print_unavailable(result)
        cls._print_kept_audio(result)
        return result

    @staticmethod
    def _载入已取字幕(ws: TaskWorkspace, blocks: Sequence[Mapping[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """从各块的 sidecar 读回上一轮已取到的字幕 cues，供续跑复用。

        只认**块自己的** sidecar 里、`ready_pages` 列出的分集；读坏了就当没有——
        续跑是优化，正确性不能建立在它上面。
        """
        复用: Dict[int, Dict[str, Any]] = {}
        for block in blocks:
            try:
                path = TaskWorkspace.partial_state_path(ws, dict(block))
                if not path.is_file():
                    continue
                状态 = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            好的 = {int(p) for p in (状态.get("ready_pages") or [])}
            for 键, 值 in (状态.get("cues") or {}).items():
                try:
                    page = int(键)
                except (TypeError, ValueError):
                    continue
                if page in 好的 and isinstance(值, dict) and 值.get("cues"):
                    复用[page] = 值
        return 复用

    @staticmethod
    def _print_kept_audio(result: Mapping[str, Any]) -> None:
        """把「因已有听音稿而拒绝覆盖」的块打出来：静默保护等于没有保护，用户得看见。"""
        条目 = list(result.get("kept_audio") or [])
        if not 条目:
            return
        print(f"[*] 保留听音稿 {len(条目)} 个块（拒绝用字幕稿降级覆盖）：")
        for 项 in 条目[:12]:
            print(f"    [保留] BLK{int(项.get('block_id') or 0):02d} {Path(str(项.get('path'))).name}")
        if len(条目) > 12:
            print(f"    … 其余 {len(条目) - 12} 个见 manifest.json 的 pipeline.subtitle.kept_audio")

    @staticmethod
    def _print_unavailable(result: Mapping[str, Any]) -> None:
        """把判不可用的分集连原因打到控制台：串台与"本来没有字幕"必须一眼可分。"""
        条目 = list(result.get("unavailable") or [])
        if not 条目:
            return
        print(f"[*] 字幕不可用 {len(条目)} 个分集（已转音频兜底）：")
        for 项 in 条目[:12]:
            print(f"    [字幕不可用]P{int(项.get('page') or 0):02d} {项.get('reason')}")
        if len(条目) > 12:
            print(f"    … 其余 {len(条目) - 12} 个见 manifest.json 的 pipeline.subtitle.unavailable")

    @staticmethod
    def _parts_from_plan(plan: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        source = plan.get("source")
        if isinstance(source, Mapping) and isinstance(source.get("parts"), list):
            return [p for p in source["parts"] if isinstance(p, Mapping)]
        return []

    @staticmethod
    def _reference_texts(ws: TaskWorkspace) -> Dict[str, str]:
        """课程参考正文：`{逐字稿绝对路径: 正文}`，来自工作区里已有的全部逐字稿。

        身份判据的第二票（课程高频术语）靠它建立；已有逐字稿不足时由本次取回的正文补足，
        两者都不够（新工作区首跑的前几页）则退回"只看标题"。这样单块 `--range` 重跑
        也能拿到参照，不依赖本次抓了哪些页。

        返回**路径 → 正文的映射**而不是纯文本列表：调用方要按块剔除"本块自己的旧稿"
        （见 `run` 里的 `参照他块`），拿不到路径就没法剔。
        """
        try:
            文件 = sorted(ws.subtitles_dir.glob("*_逐字稿.md"))
        except OSError:
            return {}
        映射: Dict[str, str] = {}
        for 路径 in 文件:
            try:
                映射[str(路径)] = 路径.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
        return 映射


__all__ = ["SubtitleService"]
