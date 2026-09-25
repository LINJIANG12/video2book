#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕优先阶段服务。

只消费 BlockPlan 与 parts 元数据，不读取物理音频。每个分集字幕只请求一次；完整块
直接写块级逐字稿，缺字幕块统一返回 ``needs_audio``，由编排层按需物化。
"""

from __future__ import annotations

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
    ) -> Dict[str, Any]:
        """执行字幕阶段，返回稳定的块级状态集合。"""
        blocks = [b for b in (plan.get("blocks") or []) if isinstance(b, Mapping)]
        parts = cls._parts_from_plan(plan)
        result: Dict[str, Any] = {
            "subtitle_ready": [],
            "cached": [],
            "needs_audio": [],
            "written": [],
            "errors": [],
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
        for block in blocks:
            block_id = int(block.get("block_id") or 0)
            target = TaskWorkspace.block_path(ws, dict(block))
            if target.exists() and target.stat().st_size > 0 and not force:
                result["cached"].append(block_id)
                result["subtitle_ready"].append(block_id)
                continue

            page_values: Dict[int, Optional[Dict[str, Any]]] = {}
            for segment in block.get("segments") or []:
                page = int(segment.get("page") or 0)
                if page not in cache:
                    part = parts_by_page.get(page)
                    if not part or part.get("cid") is None:
                        cache[page] = None
                    else:
                        bvid = str(part.get("bvid") or "").strip() or course_bvid
                        duration = float(part.get("duration") or 0.0)
                        diagnostic: Dict[str, Any] = {}
                        try:
                            cache[page] = subtitle_core.fetch_episode_subtitle(
                                bvid,
                                int(part["cid"]),
                                sessdata=sessdata,
                                keys_file=getattr(ws, "wbi_keys_file", None),
                                duration_sec=duration,
                                诊断=diagnostic,
                            )
                        except Exception as err:
                            result["errors"].append({
                                "block_id": block_id,
                                "page": page,
                                "message": str(err),
                            })
                            cache[page] = None
                page_values[page] = cache[page]

            assembled = subtitle_core.assemble_block_transcript(dict(block), page_values)
            if not assembled.get("ok"):
                result["needs_audio"].append(block_id)
                result.setdefault("missing", []).append({
                    "block_id": block_id,
                    "missing_pages": assembled.get("missing_pages") or [],
                    "reason": assembled.get("reason") or "missing_subtitles",
                })
                continue

            TaskWorkspace.write_block_transcript(ws, dict(block), assembled["text"])
            result["subtitle_ready"].append(block_id)
            result["written"].append({
                "block_id": block_id,
                "path": str(target),
                "kind": assembled.get("kind_label") or "",
            })

        return result

    @staticmethod
    def _parts_from_plan(plan: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        source = plan.get("source")
        if isinstance(source, Mapping) and isinstance(source.get("parts"), list):
            return [p for p in source["parts"] if isinstance(p, Mapping)]
        return []


__all__ = ["SubtitleService"]
