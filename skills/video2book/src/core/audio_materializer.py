#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按既有 BlockPlan 物化块音频。

BlockPlan 决定模块边界；本模块只把计划中的源分集音频变成块音频。它不规划、不改块号、
不重算 span，也不读取旧的块清单。因此字幕优先链路可以先写逻辑块，只有缺字幕的块才
进入这里。
"""

from __future__ import annotations

import shutil
import subprocess
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
        lines = [f"file '{Path(source).as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for source in sources]
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
