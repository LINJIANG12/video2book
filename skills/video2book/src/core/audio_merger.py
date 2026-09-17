"""按「集」装箱成块音频的内核（块级流水线的第一步）。

**为什么需要这一层**：转录一次调用能处理多长音频，决定了整条链路的调用次数。逐集转录时，
一门 200 集的课就是 200 次取音调用；把连续的几集拼成一个「块」再转录，调用次数直接按块数
走（实测 9 门课 936 集，60 分钟目标下 381 块，降 2.46 倍）。

**块不只是传输容器，它就是知识模块**：长文按块成文（`articles/模块XX_*_精读长文.md`）、
教材按块整编、笔记按块归并，块标题（块内分集名的语义组合）直接写进块音频文件名。
因此这里产出的 `audio/_blocks/blocks.json` 既是取音单位，也是**模块边界的唯一事实源**：
块 → 集号 → 集在块内的起止时间，转录后要按集查阅时照它切分。

**装箱以集为最小单位**：默认整集进块，切点只落在集与集之间。唯一的例外是**超长集**
（单集 > 上限，见 `plan_units`）：它按上限均分成 `P12上` / `P12下` 两条腿各自装箱——
锯开一集仍然是有代价的，但不锯就只能把整集塞进一个超出取音阈值的块（会被自动分卷，
调用次数反而回升），两害相权取其轻。
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
from .workspace import sanitize_filename

# 块时长目标（分钟）。默认 50，落进 [下限, 上限] 区间的中段；`--block-minutes` 改的就是它。
ENV_BLOCK_MINUTES = "BVB_AUDIO_BLOCK_MINUTES"
# 单块硬上限（分钟）：取音侧「整片一次性就绪」的阈值，超过它会被自动分卷，调用次数反而回升。
ENV_ONESHOT_LIMIT_MINUTES = "BVB_AUDIO_ONESHOT_LIMIT_MINUTES"
# 块时长区间（分钟）：装箱结果要落进 [min, max]——小集往一块凑，超长集按上限劈成上下两半。
ENV_BLOCK_MIN_MINUTES = "BVB_AUDIO_BLOCK_MIN_MINUTES"
ENV_BLOCK_MAX_MINUTES = "BVB_AUDIO_BLOCK_MAX_MINUTES"

DEFAULT_BLOCK_MINUTES = 50.0
# 与 omni-media 原生版 MAX_ONESHOT_MINUTES 同值（>75 分钟自动 clamp 成 30 分钟分卷）。
DEFAULT_ONESHOT_LIMIT_MINUTES = 75.0

DEFAULT_BLOCK_MIN_MINUTES = 40.0
DEFAULT_BLOCK_MAX_MINUTES = 60.0

# 劈分容差：超长集按 n=ceil(时长/上限) 均分时，允许每段比下限低 10%（76 分钟 → 38+38，
# 比"整块 76 分钟、取音侧分两卷"更贴区间）；低于这个容差就不劈（61 分钟 → 30.5+30.5 不劈）。
SPLIT_MIN_TOLERANCE = 0.9

# 统一转码口径：与 fetcher.py 在线源一致（16kHz 单声道 32k AAC）。
UNIFIED_AUDIO_ARGS = ["-vn", "-acodec", "aac", "-ar", "16000", "-ac", "1", "-b:a", "32k"]


class AudioMerger:
    """块音频的装箱、拼接与清单落盘。全程幂等，可反复重跑。"""

    BLOCK_DIR_NAME = "_blocks"
    MANIFEST_NAME = "blocks.json"
    # 2 起：块清单带「装箱单元」（可含劈分腿）与语义标题，块音频按标题命名
    MANIFEST_VERSION = 2

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
        """块时长目标（分钟，区间中段）。环境变量 `BVB_AUDIO_BLOCK_MINUTES` 可覆盖，默认 50。"""
        return cls._env_float(ENV_BLOCK_MINUTES, DEFAULT_BLOCK_MINUTES)

    @classmethod
    def band_minutes(cls) -> Tuple[float, float]:
        """块时长区间（分钟）。小集往一块凑、超长集按下限/上限劈分都以它为准。"""
        low = cls._env_float(ENV_BLOCK_MIN_MINUTES, DEFAULT_BLOCK_MIN_MINUTES)
        high = cls._env_float(ENV_BLOCK_MAX_MINUTES, DEFAULT_BLOCK_MAX_MINUTES)
        if high < low:
            low, high = high, low
        return low, high

    @classmethod
    def oneshot_limit_minutes(cls) -> float:
        """单块硬上限（分钟）。环境变量 `BVB_AUDIO_ONESHOT_LIMIT_MINUTES` 可覆盖，默认 75。"""
        return cls._env_float(ENV_ONESHOT_LIMIT_MINUTES, DEFAULT_ONESHOT_LIMIT_MINUTES)

    @classmethod
    def limits(cls, target_minutes: Optional[float] = None) -> Dict[str, float]:
        """返回本次生效的三元组，供调用方打印与写进清单。

        * `target`  —— 装箱目标（区间中段），决定块数；
        * `min` / `max` —— 块时长区间：装箱结果要落进去（劈分容差见 `SPLIT_MIN_TOLERANCE`）；
        * `ceiling` —— 单块硬上限，任何块不得越过（越过即触发取音侧分卷续读）；
        * `effective` —— 目标被硬上限夹紧后的值，仅用于对外播报。
        """
        target = float(target_minutes) if target_minutes and target_minutes > 0 else cls.block_minutes()
        ceiling = cls.oneshot_limit_minutes()
        low, high = cls.band_minutes()
        return {
            "target": target,
            "ceiling": ceiling,
            "effective": min(target, ceiling),
            "min": low,
            "max": high,
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

        做法与「先定块数、再按累计时长均分」同一口径，只是切点
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
    def pack(
        cls,
        durations: Sequence[float],
        target_minutes: float,
        max_minutes: float,
        min_minutes: float = 0.0,
    ) -> List[List[int]]:
        """把「装箱单元」按时长装成若干**连续**块，目标是每块落进 `[min, max]` 分钟区间。

        做法：在可行块数附近枚举（`ceil(总时长/max)` 上下各放宽两档），对每个块数用 `_partition`
        做「按累计时长均分、切点吸附到单元边界」，再挑违规代价最小的那一版。

        为什么不用「贪心凑到上限就封块」：那会凑出 59/58/57 分钟的几块再加一条十几分钟的尾巴，
        尾巴必然掉出区间；均分才能让每块都落在带内（实测 197.7 分钟 → 4 块各 ~49 分钟）。
        """
        count = len(durations)
        if count == 0:
            return []
        weights = [max(0.0, float(d)) for d in durations]
        total = sum(weights)
        min_sec = max(1.0, float(min_minutes) * 60.0)
        max_sec = max(min_sec, float(max_minutes) * 60.0)
        target_sec = max(1.0, float(target_minutes) * 60.0)

        low_n = max(1, int(math.ceil(total / max_sec - 1e-9)))
        high_n = max(low_n, int(math.floor(total / min_sec + 1e-9))) if min_sec > 0 else count
        window = range(max(1, low_n - 2), min(count, max(high_n, low_n) + 2) + 1)

        best_groups: Optional[List[List[int]]] = None
        best_score: Optional[float] = None
        for num_blocks in window:
            groups = cls._partition(weights, num_blocks)
            score = cls._band_penalty(groups, weights, min_sec, max_sec, target_sec)
            if best_score is None or score < best_score:
                best_groups, best_score = groups, score
        return best_groups if best_groups is not None else [list(range(count))]

    @staticmethod
    def _band_penalty(
        groups: Sequence[Sequence[int]],
        weights: Sequence[float],
        min_sec: float,
        max_sec: float,
        target_sec: float,
    ) -> float:
        """块长违规代价（分钟）：出区间越远代价越大，越上限比越下限严重（上限硬、下限软）。"""
        penalty = 0.0
        for group in groups:
            span = sum(weights[i] for i in group)
            if span < min_sec:
                penalty += (min_sec - span) / 60.0
            if span > max_sec:
                penalty += (span - max_sec) / 60.0 * 10.0
            penalty += abs(span - target_sec) / 60.0 * 0.01
        return penalty

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
    def block_span(cls, block: Dict[str, Any]) -> str:
        """块覆盖范围的展示串：整集块为 `P01-P06`，含劈分腿时为 `P12上-P13下`。

        与 `block_stem` 的区别：后者只看集号（历史清单没有 units 时的兜底），前者看**装箱单元**，
        所以能把「同一集的上下半」表达清楚——任务书、逐字稿、模块长文都用它作锚。
        """
        units = block.get("units") or []
        labels = [str(u.get("label") or "") for u in units if u.get("label")]
        if not labels:
            return cls.block_stem(block.get("episodes") or [])
        if len(labels) == 1:
            return labels[0]
        return f"{labels[0]}-{labels[-1]}"

    # ------------------------------------------------------------------
    # 装箱单元：整集一个单元；超长集按上限劈成上下两半
    # ------------------------------------------------------------------

    @staticmethod
    def _part_labels(page: int, parts: int) -> List[str]:
        """劈腿标签：两段用「上/下」；三段以上用「上/中N/下」，避免同一集的两腿互相覆盖。"""
        if parts == 2:
            return [f"P{page:02d}上", f"P{page:02d}下"]
        return [f"P{page:02d}" + {0: "上", parts - 1: "下"}.get(i, f"中{i}") for i in range(parts)]

    @classmethod
    def _unit(
        cls, record: Dict[str, Any], label: str, start_sec: float, end_sec: float, split: bool
    ) -> Dict[str, Any]:
        """一个装箱单元：某集（或它的一段）在源音频里的区间。`record` 只在内存里用，不落清单。"""
        return {
            "page": int(record["page"]),
            "label": label,
            "start_sec": round(float(start_sec), 2),
            "end_sec": round(float(end_sec), 2),
            "duration_sec": round(max(0.0, float(end_sec) - float(start_sec)), 2),
            "split": bool(split),
            "record": record,
        }

    @classmethod
    def plan_units(
        cls, records: Sequence[Dict[str, Any]], min_minutes: float, max_minutes: float
    ) -> List[Dict[str, Any]]:
        """把逐集音频规划成装箱单元。

        * 单集 ≤ 上限：整集一个单元；
        * 单集 > 上限：取 `n = ceil(时长/上限)` 均分，每段还需 ≥ 下限 × `SPLIT_MIN_TOLERANCE`；
          不满足就**不劈**——61 分钟劈成 30.5+30.5 是两个都不合格的块，不如整块 61 分钟
          （仍在取音侧一次性就绪的 75 分钟阈值内，不会触发分卷）。
        """
        max_sec = max(1.0, float(max_minutes) * 60.0)
        floor_sec = float(min_minutes) * 60.0 * SPLIT_MIN_TOLERANCE
        units: List[Dict[str, Any]] = []
        for record in records:
            page = int(record["page"])
            duration = float(record.get("duration") or 0.0)
            whole = cls._unit(record, f"P{page:02d}", 0.0, duration, False)
            if duration <= max_sec or duration <= 0:
                units.append(whole)
                continue
            parts = max(2, int(math.ceil(duration / max_sec - 1e-9)))
            piece = duration / parts
            if piece < floor_sec:
                units.append(whole)
                continue
            labels = cls._part_labels(page, parts)
            for index, label in enumerate(labels):
                start = piece * index
                end = duration if index == parts - 1 else piece * (index + 1)
                units.append(cls._unit(record, label, start, end, True))
        return units

    # ------------------------------------------------------------------
    # 块命名：<课程短名>_<序号>_<语义组合标题>(覆盖范围)
    # ------------------------------------------------------------------

    TITLES_NAME = "block_titles.json"

    @classmethod
    def titles_path(cls, ws: Any) -> Path:
        return cls.block_dir(ws) / cls.TITLES_NAME

    @classmethod
    def load_block_titles(cls, ws: Any) -> Dict[str, Any]:
        """读 `audio/_blocks/block_titles.json`（Agent 给的块标题）。

        接受 `{"01": "标题"}`、`{"1": "标题"}`、`["标题一", "标题二"]` 三种写法，另可用
        `"_course": "课程短名"` 覆盖文件名前缀。文件缺失/损坏时返回空表——装箱照跑，
        命名退化为「首集标题 + 覆盖范围」，绝不出现空名。
        """
        path = cls.titles_path(ws)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if isinstance(data, list):
            return {str(i + 1): str(v) for i, v in enumerate(data) if str(v or "").strip()}
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if str(v or "").strip()}

    @staticmethod
    def _title_for(titles: Dict[str, Any], index: int, fallback: str) -> str:
        for key in (f"{index:02d}", str(index)):
            value = titles.get(key)
            if value:
                return str(value).strip()
        return fallback

    @classmethod
    def course_short_name(cls, ws: Any, titles: Optional[Dict[str, Any]] = None) -> str:
        """文件名的课程前缀：优先 titles 里的 `_course`，否则取工作区名（截断到首个 `_BV`）。"""
        override = (titles or {}).get("_course")
        if override:
            return sanitize_filename(str(override), max_len=24)
        name = Path(ws.root_dir).name
        name = re.split(r"_BV|_dy_|_yt_", name)[0].strip() or name
        return sanitize_filename(name, max_len=24)

    @classmethod
    def block_filename(cls, course_short: str, index: int, title: str, span: str) -> str:
        """块音频文件名：`<课程短名>_<序号>_<标题>(覆盖范围).m4a`。"""
        pieces = [p for p in (course_short, f"{index:02d}", title) if p]
        return sanitize_filename("_".join(pieces), max_len=70) + f"({span}).m4a"

    @classmethod
    def block_dir(cls, ws: Any) -> Path:
        # 两类入参都要活：TaskWorkspace（有 .audio_dir）与纯路径（如 integrator 里的 task_dir）。
        # 只认前者会让「读块清单」这个只读动作成为崩溃点（run(blocks=None) 的缺省分支）。
        audio = getattr(ws, "audio_dir", None)
        return (Path(audio) if audio else Path(ws) / "audio") / cls.BLOCK_DIR_NAME

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
        blocks = data.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            return None
        # 条目级最小校验：一条不合法就整份按「无效清单」处理——上层会走「先 merge-audio 重装」
        # 的提示路径。照单全收的后果实测过：条目缺 episodes/units 会让 block_stem 抛 IndexError，
        # 装箱、队列、对账、整编一路崩穿，而报错点离根因隔了好几层。
        for block in blocks:
            if not isinstance(block, dict):
                return None
            try:
                if int(block.get("block_id") or 0) <= 0:
                    return None
            except (TypeError, ValueError):
                return None
            if not isinstance(block.get("episodes"), list) or not block["episodes"]:
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
    def _input_signature(
        cls,
        records: Sequence[Dict[str, Any]],
        limits: Dict[str, float],
        titles: Optional[Dict[str, Any]] = None,
    ) -> str:
        """输入指纹：块结构由「集时长 + 目标 + 区间」唯一决定；块标题也进指纹。

        只统计「集号:字节数:实测时长」，不含 mtime——复制/同步改 mtime 但内容不变时不该
        白重跑一遍合并。标题进指纹是有意的：改了 `block_titles.json` 就得重命名块音频。
        """
        digest = hashlib.sha256()
        digest.update(
            f"v{cls.MANIFEST_VERSION}|t{limits['target']:.3f}|c{limits['ceiling']:.3f}"
            f"|min{limits['min']:.3f}|max{limits['max']:.3f}|".encode("utf-8")
        )
        for key in sorted(str(k) for k in (titles or {})):
            digest.update(f"T{key}={titles.get(key)}|".encode("utf-8"))
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

        # 1) 收集集音频与实测时长（顺带取该集原始标题，供块标题退化命名）
        part_titles = {
            int(part["page"]): str(part.get("title") or "")
            for part in (ws.load_parts() or [])
            if isinstance(part, dict) and part.get("page") is not None
        }
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
                "title": part_titles.get(page, ""),
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

        titles = cls.load_block_titles(ws)
        signature = cls._input_signature(records, limits, titles)

        # 2) 命中缓存：清单指纹一致、块文件齐备（且非空），直接复用
        existing = cls.load_manifest(ws)
        signature_matches = (
            bool(existing) and str(existing.get("input_signature") or "") == signature
        )
        if existing and not force and signature_matches:
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
        elif existing and not signature_matches:
            diag.append("[i] 输入指纹已变化（音频替换/时长变化/标题或装箱参数调整），派生块音频全部重切")

        # 3) 装箱：先把「装箱单元」规划出来（超长集在这里劈成上下两半），再把单元装成块
        units = cls.plan_units(records, limits["min"], limits["max"])
        groups = cls.pack(
            [unit["duration_sec"] for unit in units], limits["target"], limits["max"], limits["min"]
        )
        split_pages = sorted({int(u["page"]) for u in units if u["split"]})
        if split_pages:
            diag.append(
                f"[i] 以下单集超过 {limits['max']:g} 分钟上限，已劈成上下两半分块转录："
                + "、".join(f"P{p:02d}" for p in split_pages)
            )
        noop = len(units) == len(groups)
        if noop:
            diag.append("[i] 装箱后每块仅一集（单集时长已落在区间内），合并没有收益，跳过拼接")
        result["noop"] = noop

        # 4) 逐块拼接：块音频按「课程短名_序号_语义组合标题(覆盖范围)」命名
        block_dir = cls.block_dir(ws)
        block_dir.mkdir(parents=True, exist_ok=True)
        course_short = cls.course_short_name(ws, titles)
        if not titles:
            diag.append(
                f"[i] 未找到 {cls.TITLES_NAME}（块语义标题）：块音频先按「首集标题」命名，"
                "补上标题文件后重跑本命令即自动改名"
            )
        codecs = {(record["codec"], record["sample_rate"], record["channels"]) for record in records}
        mixed_codecs = len(codecs) > 1
        if mixed_codecs and not noop:
            diag.append(
                "[i] 检测到各集编码参数不一致（如在线源 32k 与本地源 64k 混用），块将统一转码为 16kHz 单声道 32k AAC"
            )

        blocks: List[Dict[str, Any]] = []
        expected_files: set = set()
        for block_id, group in enumerate(groups, 1):
            members = [units[index] for index in group]
            # 同名复用派生块音频只有在**输入指纹未变**时才安全：指纹变了说明某个源音频内容
            # 变了（如人工补音频后重装），此时同名文件是旧内容的残骸——照名跳过 ffmpeg 会让
            # 块音频与新生成的段表错位，转录与时间表对不上。
            block = cls._build_block(
                ws, block_dir, block_id, members, limits,
                mixed_codecs=mixed_codecs, skip_existing=skip_existing and signature_matches,
                course_short=course_short, titles=titles, records=records, diag=diag,
            )
            expected_files.add(Path(block["audio"]).name)
            expected_files |= {Path(u["source_file"]).name for u in block["units"] if u.get("source_file")}
            blocks.append(block)

        # 5) 清掉上次装箱残留的块音频/劈分腿（都可由逐集音频重建；留着会与本次命名并存误导人）
        cls._remove_stale_block_audio(block_dir, expected_files, diag)

        manifest = {
            "version": cls.MANIFEST_VERSION,
            "target_minutes": limits["target"],
            "min_minutes": limits["min"],
            "max_minutes": limits["max"],
            "effective_limit_minutes": limits["ceiling"],
            "course_short": course_short,
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
    def _build_block(
        cls,
        ws: Any,
        block_dir: Path,
        block_id: int,
        members: Sequence[Dict[str, Any]],
        limits: Dict[str, float],
        *,
        mixed_codecs: bool,
        skip_existing: bool,
        course_short: str,
        titles: Dict[str, Any],
        records: Sequence[Dict[str, Any]],
        diag: List[str],
    ) -> Dict[str, Any]:
        """拼出第 `block_id` 块：块音频 + 段表 + 名称与标记。

        三种形态：
        * 整集单单元 —— 不复制文件，块音频直接指向该集原音频（省一半磁盘）；
        * 劈分单单元 —— 用 ffmpeg 无损切出这一腿，落 `_blocks/_parts/<label>.m4a`；
        * 多单元 —— 逐单元取文件（整集用原文件、劈腿用切片）后无损拼接。
        """
        part_dir = block_dir / "_parts"
        labels = [str(member["label"]) for member in members]
        span = labels[0] if len(labels) == 1 else f"{labels[0]}-{labels[-1]}"
        pages_in_block = sorted({int(member["page"]) for member in members})
        fallback_title = cls._record_title(records, pages_in_block[0]) if pages_in_block else ""
        title = cls._title_for(titles, block_id, fallback_title)
        stem = f"BLK{block_id:02d}_{span}"
        block_path = block_dir / cls.block_filename(course_short, block_id, title, span)

        # 逐单元准备「可拼接的文件」
        unit_files: List[Path] = []
        for member in members:
            source = Path(member["record"]["path"])
            if not member["split"]:
                unit_files.append(source)
                continue
            part_dir.mkdir(parents=True, exist_ok=True)
            sliced = part_dir / f"{stem}_{member['label']}.m4a"
            if not (skip_existing and sliced.exists() and sliced.stat().st_size > 0):
                cls._slice_unit(source, sliced, float(member["start_sec"]),
                                float(member["duration_sec"]))
            unit_files.append(sliced)

        reencoded = False
        if len(members) == 1:
            block_path = unit_files[0]          # 整集或劈腿独占一块：直接用那份文件
        elif not mixed_codecs and skip_existing and block_path.exists() and block_path.stat().st_size > 0:
            diag.append(f"[cached] {block_path.name} 已存在，跳过拼接")
        else:
            cls._concat(ws, block_dir, block_path.stem, unit_files, block_path, mixed_codecs)
            reencoded = mixed_codecs

        # 段表：块内每单元的起止（劈分腿也各占一条，带 label 与 split 标记）
        segments: List[Dict[str, Any]] = []
        units_json: List[Dict[str, Any]] = []
        cursor = 0.0
        for member, unit_file in zip(members, unit_files):
            span_sec = float(AudioChunker.get_audio_duration(str(unit_file))) or float(member["duration_sec"])
            end = cursor + span_sec
            segments.append({
                "page": int(member["page"]),
                "label": str(member["label"]),
                "start_sec": round(cursor, 2),
                "end_sec": round(end, 2),
                "start": AudioChunker.format_seconds(cursor),
                "end": AudioChunker.format_seconds(end),
                "duration_sec": round(span_sec, 2),
                "source": cls._relativize(ws, Path(member["record"]["path"])),
                "episode_split": bool(member["split"]),
            })
            units_json.append({
                "page": int(member["page"]),
                "label": str(member["label"]),
                "split": bool(member["split"]),
                "source_offset_sec": round(float(member["start_sec"]), 2),
                "source_file": cls._relativize(ws, unit_file),
            })
            cursor = end

        block_duration = cursor
        oversized = block_duration > limits["ceiling"] * 60.0 + 1.0
        undersized = block_duration < limits["min"] * 60.0 - 1.0
        if oversized:
            diag.append(
                f"[!] {block_path.name} 时长 {block_duration / 60:.1f} 分钟越过硬上限 "
                f"{limits['ceiling']:.0f} 分钟（该集本身超长且劈不开），取音侧会自动分卷续读"
            )
        if undersized:
            diag.append(
                f"[i] {block_path.name} 时长 {block_duration / 60:.1f} 分钟低于下限 "
                f"{limits['min']:.0f} 分钟（课程余量不足），按实装箱"
            )

        return {
            "block_id": block_id,
            "title": title,
            "span": span,
            "course_short": course_short,
            "episodes": pages_in_block,
            "units": units_json,
            "episode_split": any(u["split"] for u in units_json),
            "audio": cls._relativize(ws, block_path),
            "duration_sec": round(block_duration, 2),
            "duration_min": round(block_duration / 60.0, 2),
            "segments": segments,
            "single_episode": len(members) == 1 and not members[0]["split"],
            "reencoded": reencoded,
            "oversized": oversized,
            "undersized": undersized,
        }

    @staticmethod
    def _record_title(records: Sequence[Dict[str, Any]], page: int) -> str:
        """退化的块标题：该集在 `parts.json` 里的原始标题（截断到 24 字），绝不空名。"""
        for record in records:
            if int(record.get("page") or 0) == int(page):
                raw = str(record.get("title") or "").strip()
                return sanitize_filename(raw, max_len=24) if raw else f"P{int(page):02d}"
        return f"P{int(page):02d}"

    @classmethod
    def _slice_unit(cls, source: Path, dest: Path, start_sec: float, duration_sec: float) -> None:
        """从源音频里无损切出一腿（`-c copy`，不重编码）。"""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise RuntimeError("音频劈分需要 FFmpeg，请确认 ffmpeg 在 PATH 中")
        cmd = [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(round(float(start_sec), 3)),
            "-i", str(source),
            "-t", str(round(float(duration_sec), 3)),
            "-c", "copy", "-avoid_negative_ts", "make_zero",
            str(dest),
        ]
        try:
            res = run_quiet(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            timeout=TRANSCODE_TIMEOUT_SEC)
        except subprocess.TimeoutExpired as err:
            raise RuntimeError(f"FFmpeg 音频劈分超时（>{TRANSCODE_TIMEOUT_SEC}s）：{dest.name}") from err
        if res.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            raise RuntimeError(
                f"FFmpeg 音频劈分失败：{dest.name}；stderr={(res.stderr or '').strip()[:300]}"
            )

    @classmethod
    def _remove_stale_block_audio(cls, block_dir: Path, expected: set, diag: List[str]) -> None:
        """删掉不再属于本次装箱的块音频与劈分腿（纯派生数据，可由逐集音频重建）。"""
        removed: List[str] = []
        for path in sorted(block_dir.glob("*.m4a")) + sorted((block_dir / "_parts").glob("*.m4a")):
            if path.name in expected:
                continue
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                continue
        if removed:
            diag.append(f"[i] 已清掉上次装箱残留的块音频 {len(removed)} 个：{'、'.join(removed[:6])}"
                        + ("…" if len(removed) > 6 else ""))

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

        为什么必须清：块边界由「目标区间 + 集时长分布」共同决定，改一次 `--block-minutes`
        就可能从 15 块变成 12 块。旧编号的任务书留在盘上就是**可被派发的幽灵任务**——
        照着它转录会去读一个已经不存在的块。这与笔记侧的 `_prune_superseded_tasks` 是同一条纪律。

        块音频与块级逐字稿是**数据**：装箱时 `_remove_stale_block_audio` 已把不属于本次装箱的
        块音频/劈分腿清掉（纯派生、可重建），这里只把仍然孤立的逐字稿报告出来，不删。
        """
        expected = {
            f"BLK{int(block.get('block_id') or 0):02d}_{cls.block_span(block)}"
            for block in blocks
        }
        expected_audio = {Path(str(block.get("audio") or "")).name for block in blocks}
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
            for path in sorted(block_dir.glob("*.m4a")):
                if path.name not in expected_audio:
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
        """把装箱结果压成几行诊断，供 CLI 打印（区间合规与调用次数收益一眼可见）。"""
        lines: List[str] = []
        if not blocks:
            return lines
        episodes = sum(len(block.get("episodes") or []) for block in blocks)
        total_min = sum(float(block.get("duration_min") or 0.0) for block in blocks)
        saved = episodes / len(blocks) if blocks else 1.0
        lines.append(
            f"[i] 块时长区间 {limits['min']:g}–{limits['max']:g} 分钟（硬上限 {limits['ceiling']:g}）："
            f"{episodes} 集 → {len(blocks)} 块，"
            f"取音调用 {episodes} → {len(blocks)} 次（降 {saved:.2f} 倍），合计 {total_min / 60:.1f} 小时"
        )
        out_of_band = [b for b in blocks if b.get("oversized") or b.get("undersized")]
        if out_of_band:
            names = "、".join(f"BLK{int(b['block_id']):02d}" for b in out_of_band)
            lines.append(
                f"[i] 其中 {len(out_of_band)} 块不在区间内（超长集劈不开或课程余量不足）：{names}，"
                "已在清单里逐块标注"
            )
        split_blocks = [b for b in blocks if b.get("episode_split")]
        if split_blocks:
            lines.append(
                "[i] 含劈分腿的块：" + "、".join(f"BLK{int(b['block_id']):02d}({b.get('span')})" for b in split_blocks)
            )
        return lines
