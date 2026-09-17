"""按「集」装箱成块音频的内核（块级转录流水线的第一步）。

**为什么需要这一层**：转录一次调用能处理多长音频，决定了整条链路的调用次数。逐集转录
时，一门 200 集的课就是 200 次取音调用；把连续的几集拼成一个「块」再转录，调用次数直接
按块数走（实测 9 门课 936 集，60 分钟目标下 381 块，降 2.46 倍）。

**块只有「转录与派发」两个用途，对下游不可见**：长文仍是一集一篇、仍落
`articles/PXX_*_精读文章.md`，模块规划/教材分册/笔记/思维导图照旧按集号锚定。所以这里
只产出一份 `audio/_blocks/blocks.json`，把「块 → 集号 → 集在块内的起止时间」记清楚，
供转录后按集切分复原。

**为什么不按固定时长硬切**：一集被劈进两个块，等于把同一集的知识交给两个转录单元各听
一半，切分复原时必然错位——这与「一篇笔记按体积切成两篇后产生 22 处跨篇重复」是同一类
错误。所以装箱一律以**集**为最小单位，只允许在集与集之间下刀。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .audio_chunker import AudioChunker
from .local_media import PROBE_TIMEOUT_SEC, TRANSCODE_TIMEOUT_SEC
from .proc import run_quiet

# 块时长目标（分钟）。默认 60，但**必须可配**——它就是本层的核心参数。
ENV_BLOCK_MINUTES = "BVB_AUDIO_BLOCK_MINUTES"
# 单块硬上限（分钟）：取音侧「整片一次性就绪」的阈值，超过它会被自动分卷，调用次数反而回升。
ENV_ONESHOT_LIMIT_MINUTES = "BVB_AUDIO_ONESHOT_LIMIT_MINUTES"

DEFAULT_BLOCK_MINUTES = 60.0
# 与 omni-media 原生版 MAX_ONESHOT_MINUTES 同值（>75 分钟自动 clamp 成 30 分钟分卷）。
DEFAULT_ONESHOT_LIMIT_MINUTES = 75.0

# 统一转码口径：与 fetcher.py 在线源一致（16kHz 单声道 32k AAC）。
UNIFIED_AUDIO_ARGS = ["-vn", "-acodec", "aac", "-ar", "16000", "-ac", "1", "-b:a", "32k"]


class AudioMerger:
    """块音频的装箱、拼接与清单落盘。全程幂等，可反复重跑。"""

    BLOCK_DIR_NAME = "_blocks"
    MANIFEST_NAME = "blocks.json"
    MANIFEST_VERSION = 1

    # ------------------------------------------------------------------
    # 参数口径（目标 / 硬上限），CLI 与任务书共用同一套换算
    # ------------------------------------------------------------------

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        """读环境变量覆盖值；非法或非正数时回退默认（工具层不因配置笔误而中断）。"""
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            return float(default)
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return float(default)
        return value if value > 0 else float(default)

    @classmethod
    def block_minutes(cls) -> float:
        """块时长目标（分钟）。环境变量 `BVB_AUDIO_BLOCK_MINUTES` 可覆盖，默认 60。"""
        return cls._env_float(ENV_BLOCK_MINUTES, DEFAULT_BLOCK_MINUTES)

    @classmethod
    def oneshot_limit_minutes(cls) -> float:
        """单块硬上限（分钟）。环境变量 `BVB_AUDIO_ONESHOT_LIMIT_MINUTES` 可覆盖，默认 75。"""
        return cls._env_float(ENV_ONESHOT_LIMIT_MINUTES, DEFAULT_ONESHOT_LIMIT_MINUTES)

    @classmethod
    def limits(cls, target_minutes: Optional[float] = None) -> Dict[str, float]:
        """返回本次生效的三元组，供调用方打印与写进清单。

        * `target`  —— 装箱目标，决定块数（`N = ceil(总时长 / target)`）；
        * `ceiling` —— 单块硬上限，装箱结果不得越过它（越过即触发分卷续读）；
        * `effective` —— 目标被上限夹紧后的值，仅用于对外播报「实际按多少分钟在装」。
        """
        target = float(target_minutes) if target_minutes and target_minutes > 0 else cls.block_minutes()
        ceiling = cls.oneshot_limit_minutes()
        return {
            "target": target,
            "ceiling": ceiling,
            "effective": min(target, ceiling),
        }

    # ------------------------------------------------------------------
    # 媒体探测
    # ------------------------------------------------------------------

    @classmethod
    def probe_stream(cls, path: Path) -> Dict[str, Any]:
        """用 ffprobe 取音频流的编码参数与实测时长。

        为什么时长必须实测：`parts.json` 里的 `duration` 与真实音轨有 1~2 秒偏差（实测
        P08 登记 583s、ffprobe 实测 581.9s），逐集累加后误差会随块内集数放大，而块内起止
        时间是切分逐字稿的唯一依据——差几秒就可能把一集的结尾切到下一集去。
        """
        info: Dict[str, Any] = {"duration_sec": 0.0, "codec": "", "sample_rate": 0, "channels": 0}
        ffprobe_bin = shutil.which("ffprobe")
        if not ffprobe_bin:
            info["duration_sec"] = AudioChunker.get_audio_duration(str(path))
            return info
        cmd = [
            ffprobe_bin,
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
            "-of", "json",
            str(path),
        ]
        try:
            res = run_quiet(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=PROBE_TIMEOUT_SEC)
            payload = json.loads(res.stdout or "{}")
        except Exception:
            payload = {}
        streams = payload.get("streams") or []
        if streams and isinstance(streams[0], dict):
            stream = streams[0]
            info["codec"] = str(stream.get("codec_name") or "")
            try:
                info["sample_rate"] = int(stream.get("sample_rate") or 0)
            except (TypeError, ValueError):
                info["sample_rate"] = 0
            try:
                info["channels"] = int(stream.get("channels") or 0)
            except (TypeError, ValueError):
                info["channels"] = 0
        try:
            info["duration_sec"] = float((payload.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            info["duration_sec"] = 0.0
        if info["duration_sec"] <= 0:
            info["duration_sec"] = AudioChunker.get_audio_duration(str(path))
        return info

    # ------------------------------------------------------------------
    # 装箱
    # ------------------------------------------------------------------

    @classmethod
    def _partition(cls, weights: Sequence[float], num_blocks: int) -> List[List[int]]:
        """把按顺序排列的集均分成 `num_blocks` 个**连续**块，使各块时长尽量接近。

        做法与 `AudioChunker.chunk_audio(balanced=True)` 同源（先定块数再均分），只是切点
        被吸附到**集边界**上：第 k 个切点取「累计时长最接近 `k/num_blocks` 目标」的那条集边界。
        连续性是硬要求——块内是连续的几集，转录出来的文本才会是连续的一段讲解。
        """
        count = len(weights)
        if num_blocks >= count:
            return [[index] for index in range(count)]

        prefix = [0.0]
        for weight in weights:
            prefix.append(prefix[-1] + float(weight))
        total = prefix[-1]

        cuts: List[int] = []
        for k in range(1, num_blocks):
            goal = total * k / float(num_blocks)
            low = (cuts[-1] + 1) if cuts else 1
            # 后面还剩 num_blocks - k 个块，每块至少要有一集
            high = count - (num_blocks - k)
            if high < low:
                high = low
            best = low
            best_gap = None
            for index in range(low, high + 1):
                gap = abs(prefix[index] - goal)
                if best_gap is None or gap < best_gap:
                    best, best_gap = index, gap
            cuts.append(best)

        groups: List[List[int]] = []
        previous = 0
        for cut in cuts + [count]:
            groups.append(list(range(previous, cut)))
            previous = cut
        return groups

    @classmethod
    def pack(cls, durations: Sequence[float], target_minutes: float, ceiling_minutes: float) -> List[List[int]]:
        """整数集装箱：先按目标定块数，再逐块检查是否越过硬上限，越过就加一块重装。"""
        count = len(durations)
        if count == 0:
            return []
        target_sec = max(1.0, float(target_minutes) * 60.0)
        ceiling_sec = max(1.0, float(ceiling_minutes) * 60.0)

        num_blocks = max(1, int(math.ceil(sum(float(d) for d in durations) / target_sec)))
        groups = cls._partition(durations, num_blocks)
        while num_blocks < count:
            longest = max(sum(float(durations[i]) for i in group) for group in groups)
            if longest <= ceiling_sec:
                break
            num_blocks += 1
            groups = cls._partition(durations, num_blocks)
        return groups

    # ------------------------------------------------------------------
    # 命名与清单
    # ------------------------------------------------------------------

    @staticmethod
    def block_stem(episodes: Sequence[int]) -> str:
        """块文件名主干：单集为 `P08`，多集为 `P08-P12`（相邻集号区间）。"""
        pages = sorted(int(p) for p in episodes)
        if len(pages) == 1:
            return f"P{pages[0]:02d}"
        return f"P{pages[0]:02d}-P{pages[-1]:02d}"

    @classmethod
    def block_dir(cls, ws: Any) -> Path:
        return Path(ws.audio_dir) / cls.BLOCK_DIR_NAME

    @classmethod
    def manifest_path(cls, ws: Any) -> Path:
        return cls.block_dir(ws) / cls.MANIFEST_NAME

    @classmethod
    def load_manifest(cls, ws: Any) -> Optional[Dict[str, Any]]:
        """读盘上的块清单；缺失/损坏/版本不符时返回 None（调用方重装即可）。"""
        path = cls.manifest_path(ws)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or int(data.get("version") or 0) != cls.MANIFEST_VERSION:
            return None
        if not isinstance(data.get("blocks"), list):
            return None
        return data

    @classmethod
    def save_manifest(cls, ws: Any, manifest: Dict[str, Any]) -> Path:
        """原子落盘（与 `TaskWorkspace.save_parts` 同一套 `.tmp.<pid>` + replace 手法）。"""
        path = cls.manifest_path(ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    @staticmethod
    def _relativize(ws: Any, path: Path) -> str:
        """清单里只存相对产物根的路径，避免机器绝对路径进产物（与 `PATH_KEYS` 同一条纪律）。"""
        try:
            return path.resolve().relative_to(Path(ws.root_dir).resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    @classmethod
    def resolve_episode_audio(cls, ws: Any, page: int) -> Optional[Path]:
        """按集号在 `audio/` 下找该集音频。用正则而非通配前缀，避免 P01 误配 P010。"""
        audio_dir = Path(ws.audio_dir)
        if not audio_dir.is_dir():
            return None
        pattern = re.compile(rf"^P0*{int(page)}_.*\.(m4a|mp3|wav|aac|flac|m4s)$", re.IGNORECASE)
        hits = [p for p in sorted(audio_dir.iterdir()) if p.is_file() and pattern.match(p.name)]
        return hits[0] if hits else None

    @classmethod
    def _input_signature(cls, records: Sequence[Dict[str, Any]], limits: Dict[str, float]) -> str:
        """输入指纹：块数由「集时长 + 目标 + 上限」唯一决定，集音频本身变了也要重装。

        只统计「集号:字节数:实测时长」，不含 mtime——复制/同步改 mtime 但内容不变时不该
        白重跑一遍合并。
        """
        digest = hashlib.sha256()
        digest.update(f"v{cls.MANIFEST_VERSION}|t{limits['target']:.3f}|c{limits['ceiling']:.3f}|".encode("utf-8"))
        for record in records:
            digest.update(
                f"{int(record['page'])}:{int(record['size'])}:{float(record['duration']):.1f}|".encode("utf-8")
            )
        return digest.hexdigest()

    @classmethod
    def merge(
        cls,
        ws: Any,
        episodes: Sequence[int],
        target_minutes: Optional[float] = None,
        force: bool = False,
        skip_existing: bool = True,
    ) -> Dict[str, Any]:
        """把 `episodes` 里的集装箱并拼接成块音频，返回结果字典。

        返回键：
        * `status`   —— `merged` / `cached` / `noop` / `skipped` / `empty`
        * `blocks`   —— 块列表（含 `segments`，转录后按它切分复原）
        * `limits`   —— 本次生效的目标与上限
        * `noop`     —— 装箱后每块仅一集，合并无收益（不拼接，块音频直接指向该集原音频）
        * `diag`     —— 诊断行（`[i]` / `[!]` 前缀），由调用方决定打印到哪
        * `missing`  —— 找不到音频的集号
        """
        limits = cls.limits(target_minutes)
        pages = sorted({int(p) for p in episodes})
        diag: List[str] = []
        result: Dict[str, Any] = {
            "status": "empty",
            "blocks": [],
            "limits": limits,
            "noop": False,
            "diag": diag,
            "missing": [],
        }
        if not pages:
            return result

        # 1) 收集集音频与实测时长
        records: List[Dict[str, Any]] = []
        for page in pages:
            audio = cls.resolve_episode_audio(ws, page)
            if audio is None:
                result["missing"].append(page)
                diag.append(f"[!] P{page:02d} 找不到音频文件，本次装箱跳过该集")
                continue
            probe = cls.probe_stream(audio)
            records.append({
                "page": page,
                "path": audio,
                "size": audio.stat().st_size,
                "duration": float(probe["duration_sec"]) or 0.0,
                "codec": probe["codec"],
                "sample_rate": probe["sample_rate"],
                "channels": probe["channels"],
            })
        if not records:
            diag.append("[!] 没有任何可用音频，块级转录无法启动")
            return result

        signature = cls._input_signature(records, limits)

        # 2) 命中缓存：清单指纹一致、块文件齐备（且非空），直接复用
        existing = cls.load_manifest(ws)
        if existing and not force and str(existing.get("input_signature") or "") == signature:
            stale = []
            for block in existing["blocks"]:
                audio_path = Path(ws.root_dir) / str(block.get("audio") or "")
                if not audio_path.exists() or audio_path.stat().st_size == 0:
                    stale.append(block.get("block_id"))
            if not stale and existing.get("blocks"):
                result["status"] = "cached"
                result["blocks"] = existing["blocks"]
                result["noop"] = bool(existing.get("noop"))
                diag.append(f"[cached] 块清单与输入指纹一致，复用 {len(existing['blocks'])} 个块")
                return result
            diag.append(f"[i] 块文件缺失或不完整（{stale}），本次重新拼接")

        # 3) 装箱
        durations = [record["duration"] for record in records]
        groups = cls.pack(durations, limits["target"], limits["ceiling"])
        noop = all(len(group) == 1 for group in groups)
        if noop:
            diag.append(
                "[i] 装箱后每块仅一集（该课单集时长已接近目标），合并没有收益，跳过拼接"
            )
        result["noop"] = noop

        # 4) 逐块拼接
        block_dir = cls.block_dir(ws)
        block_dir.mkdir(parents=True, exist_ok=True)
        codecs = {(record["codec"], record["sample_rate"], record["channels"]) for record in records}
        mixed_codecs = len(codecs) > 1
        if mixed_codecs and not noop:
            diag.append("[i] 检测到各集编码参数不一致（如在线源 32k 与本地源 64k 混用），块将统一转码为 16kHz 单声道 32k AAC")

        blocks: List[Dict[str, Any]] = []
        for block_id, group in enumerate(groups, 1):
            members = [records[index] for index in group]
            pages_in_block = [int(member["page"]) for member in members]
            stem = f"BLK{block_id:02d}_{cls.block_stem(pages_in_block)}"
            block_path = block_dir / f"{stem}.m4a"

            single = len(members) == 1
            reencoded = False
            if single:
                # 单集块不复制文件：直接把块音频指向该集原音频（省一半磁盘）
                block_path = members[0]["path"]
            else:
                need_encode = mixed_codecs
                if not need_encode and skip_existing and block_path.exists() and block_path.stat().st_size > 0:
                    diag.append(f"[cached] {stem} 已存在，跳过拼接")
                else:
                    cls._concat(ws, block_dir, stem, [member["path"] for member in members], block_path, need_encode)
                    reencoded = need_encode

            segments: List[Dict[str, Any]] = []
            cursor = 0.0
            for member in members:
                end = cursor + float(member["duration"])
                segments.append({
                    "page": int(member["page"]),
                    "start_sec": round(cursor, 2),
                    "end_sec": round(end, 2),
                    "start": AudioChunker.format_seconds(cursor),
                    "end": AudioChunker.format_seconds(end),
                    "duration_sec": round(float(member["duration"]), 2),
                    "source": cls._relativize(ws, Path(member["path"])),
                })
                cursor = end

            block_duration = float(AudioChunker.get_audio_duration(str(block_path))) if not single else float(members[0]["duration"])
            if block_duration <= 0:
                block_duration = cursor
            oversized = block_duration > limits["ceiling"] * 60.0 + 1.0
            if oversized:
                diag.append(
                    f"[!] {stem} 时长 {block_duration / 60:.1f} 分钟越过上限 "
                    f"{limits['ceiling']:.0f} 分钟（该集本身超长），取音侧会自动分卷续读"
                )
            blocks.append({
                "block_id": block_id,
                "episodes": pages_in_block,
                "audio": cls._relativize(ws, block_path),
                "duration_sec": round(block_duration, 2),
                "duration_min": round(block_duration / 60.0, 2),
                "segments": segments,
                "single_episode": single,
                "reencoded": reencoded,
                "oversized": oversized,
            })

        manifest = {
            "version": cls.MANIFEST_VERSION,
            "target_minutes": limits["target"],
            "effective_limit_minutes": limits["ceiling"],
            "noop": noop,
            "input_signature": signature,
            "blocks": blocks,
        }
        cls.save_manifest(ws, manifest)
        result["status"] = "noop" if noop else "merged"
        result["blocks"] = blocks
        result["manifest"] = cls._relativize(ws, cls.manifest_path(ws))
        return result

    @classmethod
    def _concat(
        cls,
        ws: Any,
        block_dir: Path,
        stem: str,
        sources: Sequence[Path],
        output: Path,
        reencode: bool,
    ) -> None:
        """把多集音频无损拼成一个块。

        为什么用 concat demuxer + `-c copy`：各集都是 AAC、同采样率同声道数，`-c copy` 只是
        换封装、不重编码（实测 65 分钟块耗时 498ms），既快又不掉音质。参数不一致时才转码。
        """
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise RuntimeError("音频合并需要 FFmpeg，请确认 ffmpeg 在 PATH 中")
        list_file = block_dir / f"_{stem}_concat.txt"
        # concat demuxer 的清单用单引号包裹路径；路径里的单引号需转义（Windows 路径不会出现，仍防御）
        lines = []
        for source in sources:
            safe = source.as_posix().replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file)]
        cmd += UNIFIED_AUDIO_ARGS if reencode else ["-c", "copy"]
        cmd.append(str(output))
        try:
            res = run_quiet(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=TRANSCODE_TIMEOUT_SEC)
        except subprocess.TimeoutExpired as err:
            raise RuntimeError(f"FFmpeg 音频合并超时（>{TRANSCODE_TIMEOUT_SEC}s）：{stem}") from err
        finally:
            list_file.unlink(missing_ok=True)
        if res.returncode != 0 or not output.exists() or output.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg 音频合并失败：{stem}；stderr={(res.stderr or '').strip()[:300]}")

    # ------------------------------------------------------------------
    # 陈旧派发物清理
    # ------------------------------------------------------------------

    BLOCK_TASK_SUFFIX = "_转录任务书.md"
    BLOCK_TRANSCRIPT_SUFFIX = "_逐字稿.md"

    @classmethod
    def prune_orphans(cls, ws: Any, blocks: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
        """清掉与当前装箱不再对应的块级**任务书**，并把孤立的数据产物报告出来。

        为什么必须清：块边界由「目标时长 + 集时长分布 + 硬上限」共同决定，改一次
        `--block-minutes` 就可能从 15 块变成 12 块。旧编号的任务书留在盘上就是**可被派发的
        幽灵任务**——照着它转录会去读一个已经不存在的块。这与笔记侧的
        `_prune_superseded_tasks` 是同一条纪律。

        只删任务书（纯派发物）。块音频与块级逐字稿是**数据**：删掉会连累已经切出来的分集
        逐字稿，所以只报告、不删，由调用方打印给人看。
        """
        expected = {
            f"BLK{int(block.get('block_id') or 0):02d}_{cls.block_stem(block.get('episodes') or [])}"
            for block in blocks
        }
        removed: List[str] = []
        orphan_audio: List[str] = []
        orphan_transcripts: List[str] = []

        subtitles = Path(ws.subtitles_dir)
        if subtitles.is_dir():
            for path in sorted(subtitles.glob(f"BLK*{cls.BLOCK_TASK_SUFFIX}")):
                stem = path.name[: -len(cls.BLOCK_TASK_SUFFIX)]
                if stem in expected:
                    continue
                try:
                    path.unlink()
                    removed.append(path.name)
                except OSError:
                    continue
            for path in sorted(subtitles.glob(f"BLK*{cls.BLOCK_TRANSCRIPT_SUFFIX}")):
                stem = path.name[: -len(cls.BLOCK_TRANSCRIPT_SUFFIX)]
                if stem not in expected:
                    orphan_transcripts.append(path.name)

        block_dir = cls.block_dir(ws)
        if block_dir.is_dir():
            for path in sorted(block_dir.glob("BLK*.m4a")):
                if path.stem not in expected:
                    orphan_audio.append(path.name)

        return {
            "removed_tasks": removed,
            "orphan_audio": orphan_audio,
            "orphan_transcripts": orphan_transcripts,
        }

    # ------------------------------------------------------------------
    # 对外播报
    # ------------------------------------------------------------------

    @classmethod
    def describe(cls, blocks: Sequence[Dict[str, Any]], limits: Dict[str, float]) -> List[str]:
        """把装箱结果压成几行诊断，供 CLI 打印（调用次数收益一眼可见）。"""
        lines: List[str] = []
        if not blocks:
            return lines
        episodes = sum(len(block.get("episodes") or []) for block in blocks)
        total_min = sum(float(block.get("duration_min") or 0.0) for block in blocks)
        saved = episodes / len(blocks) if blocks else 1.0
        lines.append(
            f"[i] 块时长目标 {limits['target']:g} 分钟（上限 {limits['ceiling']:g} 分钟）："
            f"{episodes} 集 → {len(blocks)} 块，"
            f"取音调用 {episodes} → {len(blocks)} 次（降 {saved:.2f} 倍），合计 {total_min / 60:.1f} 小时"
        )
        return lines
