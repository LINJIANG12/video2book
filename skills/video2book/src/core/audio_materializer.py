#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按既有 BlockPlan 物化块音频。

BlockPlan 决定模块边界；本模块只把计划中的源分集音频变成块音频。它不规划、不改块号、
不重算 span，也不读取旧的块清单。因此字幕优先链路可以先写逻辑块，只有缺字幕的块才
进入这里。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .block_plan import BlockPlan
from .local_media import TRANSCODE_TIMEOUT_SEC
from .proc import run_quiet

FetchEpisode = Callable[[Dict[str, Any], Path], Path]


class AudioMaterializationError(RuntimeError):
    """物理音频无法按已锁定计划生成。"""


class AudioMaterializer:
    """只物化 BlockPlan 中指定的块，不改变逻辑计划。"""

    BLOCKS_DIR = "blocks"
    PARTS_DIR = "_parts"

    @classmethod
    def blocks_dir(cls, ws: Any) -> Path:
        path = Path(ws.audio_dir) / cls.BLOCKS_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def parts_dir(cls, ws: Any) -> Path:
        path = cls.blocks_dir(ws) / cls.PARTS_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def source_audio_path(cls, ws: Any, part: Mapping[str, Any]) -> Path:
        return BlockPlan.source_audio_path(ws, part)

    @classmethod
    def block_audio_path(cls, ws: Any, block: Mapping[str, Any]) -> Path:
        return BlockPlan.audio_path(ws, block)

    @classmethod
    def _validate_block_shape(cls, block: Mapping[str, Any]) -> None:
        units = [u for u in (block.get("units") or []) if isinstance(u, Mapping)]
        segments = [s for s in (block.get("segments") or []) if isinstance(s, Mapping)]
        if not units or len(units) != len(segments):
            raise AudioMaterializationError(
                f"BLK{int(block.get('block_id') or 0):02d} 的 units/segments 数量或结构不一致"
            )
        for index, (unit, segment) in enumerate(zip(units, segments)):
            if (int(unit.get("page") or 0), str(unit.get("label") or "")) != (
                int(segment.get("page") or 0), str(segment.get("label") or "")
            ):
                raise AudioMaterializationError(
                    f"BLK{int(block.get('block_id') or 0):02d} 第 {index + 1} 个 unit/segment 不对应"
                )
            unit_duration = float(unit.get("duration_sec") or 0.0)
            segment_duration = float(segment.get("duration_sec") or 0.0)
            if abs(unit_duration - segment_duration) > 0.05:
                raise AudioMaterializationError(
                    f"BLK{int(block.get('block_id') or 0):02d} 第 {index + 1} 个切片时长不一致"
                )
        total = sum(float(u.get("duration_sec") or 0.0) for u in units)
        expected = float(block.get("duration_sec") or total)
        if abs(total - expected) > 0.1:
            raise AudioMaterializationError(
                f"BLK{int(block.get('block_id') or 0):02d} 计划块时长与 unit 总和不一致"
            )

    @classmethod
    def _ensure_source_audio(
        cls,
        ws: Any,
        info: Dict[str, Any],
        part: Mapping[str, Any],
        fetch_episode: FetchEpisode,
        cache: Dict[int, Path],
        *,
        force: bool,
    ) -> Path:
        page = int(part.get("page") or 0)
        if page in cache:
            return cache[page]
        target = cls.source_audio_path(ws, part)
        if target.exists() and target.stat().st_size >= 10240 and not force:
            cache[page] = target
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            fetched = Path(fetch_episode(dict(part), target))
        except Exception as err:
            raise AudioMaterializationError(f"P{page:02d} 音频下载失败：{err}") from err
        if not fetched.exists() or fetched.stat().st_size <= 0:
            raise AudioMaterializationError(f"P{page:02d} 音频下载后没有有效文件")
        cache[page] = fetched
        return fetched

    @classmethod
    def materialize(
        cls,
        ws: Any,
        info: Dict[str, Any],
        blocks: Sequence[Mapping[str, Any]],
        parts: Mapping[int, Mapping[str, Any]],
        fetch_episode: FetchEpisode,
        *,
        force: bool = False,
    ) -> Dict[int, Path]:
        """按计划物化指定块，返回 ``block_id -> 实际音频路径``。

        同一源分集在多个 split 块中只取音一次；物化过程不写回 BlockPlan。
        """
        del info  # 物理层通过注入的 fetch_episode 取得 info；保留参数用于调用方契约清晰。
        result: Dict[int, Path] = {}
        source_cache: Dict[int, Path] = {}
        for raw_block in blocks:
            block = dict(raw_block)
            cls._validate_block_shape(block)
            block_id = int(block.get("block_id") or 0)
            target = cls.block_audio_path(ws, block)
            if target.exists() and target.stat().st_size > 0 and not force:
                result[block_id] = target
                continue

            source_files: List[Path] = []
            for unit in block.get("units") or []:
                page = int(unit.get("page") or 0)
                part = parts.get(page)
                if not part:
                    raise AudioMaterializationError(
                        f"BLK{block_id:02d} 缺少分集元数据 P{page:02d}，无法物化音频"
                    )
                source = cls._ensure_source_audio(
                    ws, {}, part, fetch_episode, source_cache, force=force
                )
                if not unit.get("split"):
                    source_files.append(source)
                    continue
                label = str(unit.get("label") or f"P{page:02d}")
                sliced = cls.parts_dir(ws) / f"BLK{block_id:02d}_{label}.m4a"
                if not (sliced.exists() and sliced.stat().st_size > 0) or force:
                    cls._slice_unit(
                        source,
                        sliced,
                        float(unit.get("source_offset_sec") or 0.0),
                        float(unit.get("duration_sec") or 0.0),
                    )
                source_files.append(sliced)

            if not source_files:
                raise AudioMaterializationError(f"BLK{block_id:02d} 计划没有可物化的音频单元")
            if len(source_files) == 1 and not block.get("episode_split"):
                result[block_id] = source_files[0]
                continue
            cls._concat(target, source_files)
            result[block_id] = target
        return result

    # -- 交付后回收 ----------------------------------------------------------
    #
    # 音频是**唯一大体积的中间产物**：一套 145 集的课约 476 MB（分集源音频与
    # 块级拼接各占一半，而块音频本就是分集音频的 concat 副本）。而逐字稿正文
    # 一个字节都没提音频路径——写完长文/笔记/教材后，音频就是纯可再生资源。
    #
    # 判定从严：四条件全过才删。缺任一条就一片不动并说清缺什么。
    # 尤其是「逐字稿齐备」这条——`queue_tracker --next-transcribe` 的派发门禁
    # 读的是 `BlockPlan.audio_ready()`（见 block_plan.audio_ready），音频没了
    # 未转录块会**静默地不再派发**。所以这里宁可漏删，绝不误删。

    #: 回收作用域：分集源音频、块级拼接、超长集劈腿切片。
    PURGE_GLOB = "**/*.m4a"
    PURGE_SCOPE = "audio/P*.m4a + audio/blocks/*.m4a + audio/blocks/_parts/*.m4a"

    @classmethod
    def audio_exists(cls, ws: Any) -> bool:
        """audio/ 下是否还有任何音频产物。"""
        audio_dir = Path(ws.audio_dir)
        return audio_dir.is_dir() and any(audio_dir.rglob("*.m4a"))

    @classmethod
    def _missing_completion_reasons(
        cls, ws: Any, blocks: Sequence[Mapping[str, Any]]
    ) -> List[str]:
        """返回未满足的完工条件清单；空列表表示四条件全过。"""
        missing: List[str] = []
        if not blocks:
            missing.append("块清单为空")
            return missing

        from .workspace import TaskWorkspace

        # 1) 每块逐字稿存在且非空——写作与派发的唯一事实来源，缺了不能删输入。
        缺稿 = [
            int(b.get("block_id") or 0) for b in blocks
            if not (lambda p: p.is_file() and p.stat().st_size > 0)(
                TaskWorkspace.block_path(ws, dict(b))
            )
        ]
        if 缺稿:
            missing.append(
                "逐字稿未齐（缺 BLK"
                + "、BLK".join(f"{i:02d}" for i in 缺稿)
                + "）"
            )

        # 2) 模块长文齐备：块数与计划一致，而不是「有一个就算」。
        块数 = len(blocks)
        长文数 = len([
            p for p in Path(ws.articles_dir).glob("*_精读长文.md") if p.is_file()
        ]) if Path(ws.articles_dir).is_dir() else 0
        if 长文数 < 块数:
            missing.append(f"模块长文未齐（{长文数}/{块数} 篇）")

        # 3) 笔记已归并（notes/ 至少一篇成品）。
        笔记目录 = Path(ws.notes_dir)
        笔记数 = len([p for p in 笔记目录.glob("*_笔记.md") if p.is_file()]) \
            if 笔记目录.is_dir() else 0
        if 笔记数 < 1:
            missing.append("复习笔记未产出")

        # 4) 教材已整编（textbooks/ 至少一册）。
        教材目录 = Path(ws.root_dir) / "textbooks"
        教材数 = len([p for p in 教材目录.glob("*.md") if p.is_file()]) \
            if 教材目录.is_dir() else 0
        if 教材数 < 1:
            missing.append("精读全书未整编")

        return missing

    @classmethod
    def purge_audio(
        cls, ws: Any, blocks: Sequence[Mapping[str, Any]], *, dry_run: bool = False
    ) -> Dict[str, Any]:
        """完工后回收物理音频，返回统计与判定理由。

        返回 ``{"purged", "deleted", "bytes", "failed", "dry_run", "reason"}``。
        ``purged`` 表示条件成立且已（或本可）执行；条件不成立时为 ``False`` 且
        ``reason`` 点名缺哪几项——删除不可逆，报告必须能让人自己判断。
        """
        audio_dir = Path(ws.audio_dir)
        if not audio_dir.is_dir():
            return {
                "purged": True, "deleted": 0, "bytes": 0, "failed": [],
                "dry_run": bool(dry_run), "reason": "audio/ 目录不存在，无需回收",
            }

        未满足 = cls._missing_completion_reasons(ws, blocks)
        if 未满足:
            return {
                "purged": False, "deleted": 0, "bytes": 0, "failed": [],
                "dry_run": bool(dry_run), "reason": "；".join(未满足),
            }

        # 只按类型精确匹配，绝不递归删目录：块目录里可能混有 ffmpeg 清单文本，
        # 工作区里可能有嵌套的 x/ 子工作区，都不是回收对象。
        targets = sorted(
            (p for p in audio_dir.rglob("*.m4a") if p.is_file()),
            key=lambda p: len(p.parts),
            reverse=True,          # 先删深层（_parts），再删上层
        )
        total = sum(p.stat().st_size for p in targets)
        if not targets:
            return {
                "purged": True, "deleted": 0, "bytes": 0, "failed": [],
                "dry_run": bool(dry_run), "reason": "音频已回收，无重复清理",
            }

        deleted = 0
        freed = 0
        failed: List[Dict[str, Any]] = []
        for path in targets:
            size = path.stat().st_size
            if dry_run:
                deleted += 1
                freed += size
                continue
            try:
                path.unlink()
            except OSError as err:
                # 失败与「无事可做」分开记：混在一起会把真实故障说成已清理。
                failed.append({"file": str(path), "error": str(err)})
                continue
            deleted += 1
            freed += size

        return {
            "purged": True,
            "deleted": deleted,
            "bytes": freed,
            "failed": failed,
            "dry_run": bool(dry_run),
            "reason": "四条件齐备（逐字稿/长文/笔记/教材）",
        }

    @staticmethod
    def purge_record(result: Mapping[str, Any]) -> Dict[str, Any]:
        """把 ``purge_audio`` 的结果压成可写入 manifest.json 的小记录。"""
        return {
            "deleted": int(result.get("deleted") or 0),
            "bytes": int(result.get("bytes") or 0),
            "at": datetime.now().isoformat(timespec="seconds"),
            "scope": AudioMaterializer.PURGE_SCOPE,
            "dry_run": bool(result.get("dry_run")),
        }

    @staticmethod
    def _slice_unit(source: Path, dest: Path, start_sec: float, duration_sec: float) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AudioMaterializationError("音频物化需要 FFmpeg，请确认 ffmpeg 在 PATH 中")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(round(max(0.0, start_sec), 3)),
            "-i", str(source),
            "-t", str(round(max(0.0, duration_sec), 3)),
            "-c", "copy", "-avoid_negative_ts", "make_zero",
            str(dest),
        ]
        try:
            result = run_quiet(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=TRANSCODE_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as err:
            raise AudioMaterializationError(f"音频切分超时：{dest.name}") from err
        if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            detail = (result.stderr or "").strip()[:300]
            raise AudioMaterializationError(f"音频切分失败：{dest.name}；{detail}")

    @staticmethod
    def _concat(target: Path, sources: Sequence[Path]) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AudioMaterializationError("音频拼接需要 FFmpeg，请确认 ffmpeg 在 PATH 中")
        target.parent.mkdir(parents=True, exist_ok=True)
        list_file = target.parent / f"_{target.stem}_concat.txt"
        # ffmpeg 9.x 的 concat demuxer 会把 `D:/...` 形态的绝对路径当相对路径，
        # 再拼上列表文件所在目录导致打不开。改写相对列表文件目录的路径：
        # 源音频与块音频同在工作区 audio/ 下，同盘必然可表达；跨盘兜底退回绝对路径。
        lines = []
        for source in sources:
            try:
                rel = os.path.relpath(source.resolve(), start=target.parent.resolve())
            except ValueError:
                rel = str(source.resolve())
            lines.append("file '" + rel.replace("\\", "/").replace("'", "'\\''") + "'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(target),
        ]
        try:
            result = run_quiet(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=TRANSCODE_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as err:
            raise AudioMaterializationError(f"音频拼接超时：{target.name}") from err
        finally:
            list_file.unlink(missing_ok=True)
        if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            detail = (result.stderr or "").strip()[:300]
            raise AudioMaterializationError(f"音频拼接失败：{target.name}；{detail}")


__all__ = ["AudioMaterializer", "AudioMaterializationError"]
