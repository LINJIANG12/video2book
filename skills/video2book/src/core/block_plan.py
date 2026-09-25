"""v4 的纯逻辑块计划。

块计划只回答“哪些分集组成哪个知识块”，不读取、探测或生成音频。物理音频由
``AudioMaterializer`` 在计划落盘之后按需物化；因此本模块的唯一事实源是工作区根目录的
``block_plan.json``，而不是历史 ``audio/_blocks/blocks.json``。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import paths
from .workspace import sanitize_filename


ENV_BLOCK_MINUTES = "BVB_AUDIO_BLOCK_MINUTES"
ENV_ONESHOT_LIMIT_MINUTES = "BVB_AUDIO_ONESHOT_LIMIT_MINUTES"
ENV_BLOCK_MIN_MINUTES = "BVB_AUDIO_BLOCK_MIN_MINUTES"
ENV_BLOCK_MAX_MINUTES = "BVB_AUDIO_BLOCK_MAX_MINUTES"

DEFAULT_BLOCK_MINUTES = 50.0
DEFAULT_ONESHOT_LIMIT_MINUTES = 75.0
DEFAULT_BLOCK_MIN_MINUTES = 40.0
DEFAULT_BLOCK_MAX_MINUTES = 60.0
SPLIT_MIN_TOLERANCE = 0.9

_VERSION = 1
_AUDIO_PATH_KEYS = {
    "audio",
    "audio_dir",
    "audio_file",
    "audio_path",
    "block_plan",
    "file_path",
    "filepath",
    "path",
    "source_audio",
    "source_file",
    "source_path",
}
_REQUIRED_BLOCK_KEYS = {
    "block_id",
    "title",
    "span",
    "episodes",
    "units",
    "segments",
    "duration_sec",
    "duration_min",
    "source",
}


class LegacyWorkspaceError(ValueError):
    """工作区含有旧块链路产物，不能静默当作全新的 v4 计划工作区。"""


class BlockPlan:
    """只依赖分集元数据的 v4 块计划及其追加语义。"""

    VERSION = _VERSION
    PLAN_NAME = "block_plan.json"
    TITLES_NAME = "block_titles.json"
    MATERIALIZATION_STATUS = "pending"

    # ------------------------------------------------------------------
    # 路径与旧工作区检测
    # ------------------------------------------------------------------

    @staticmethod
    def _root_dir(ws: Any) -> Path:
        root = getattr(ws, "root_dir", None)
        return Path(str(root if root is not None else ws))

    @classmethod
    def _audio_root(cls, ws: Any) -> Path:
        audio = getattr(ws, "audio_dir", None)
        return Path(audio) if audio else cls._root_dir(ws) / "audio"

    @classmethod
    def path(cls, ws: Any) -> Path:
        """返回工作区根目录的逻辑计划路径。"""
        return cls._root_dir(ws) / cls.PLAN_NAME

    @classmethod
    def titles_path(cls, ws: Any) -> Path:
        """返回 v4 根目录标题文件路径；绝不回退到旧 ``audio/_blocks``。"""
        return cls._root_dir(ws) / cls.TITLES_NAME

    @classmethod
    def legacy_blocks_path(cls, ws: Any) -> Path:
        """旧清单路径只用于检测，从不打开。"""
        return cls._audio_root(ws) / "_blocks" / "blocks.json"

    @classmethod
    def blocks_dir(cls, ws: Any) -> Path:
        """返回物理块目录路径，但不在纯计划层创建它。"""
        return cls._audio_root(ws) / "blocks"

    @staticmethod
    def _has_entries(path: Path) -> bool:
        try:
            return path.exists() and any(path.iterdir())
        except OSError:
            return False

    @staticmethod
    def _has_files(path: Path) -> bool:
        try:
            return path.is_dir() and any(item.is_file() for item in path.rglob("*"))
        except OSError:
            return False

    @classmethod
    def legacy_reason(cls, ws: Any) -> Optional[str]:
        """返回拒绝原因；只检查路径，不读取任何旧 JSON 内容。"""
        if cls.path(ws).exists():
            return None
        root = cls._root_dir(ws)
        checks = (
            (cls.legacy_blocks_path(ws), "audio/_blocks/blocks.json"),
            (root / "parts.json", "parts.json"),
            (root / "manifest.json", "manifest.json"),
        )
        for candidate, label in checks:
            try:
                if candidate.exists():
                    return label
            except OSError:
                continue
        old_block_dir = cls._audio_root(ws) / "_blocks"
        if old_block_dir.exists():
            return "audio/_blocks/"
        for name in ("audio", "articles", "subtitles", "notes", "textbooks"):
            if cls._has_files(root / name):
                return f"{name}/"
        return None

    @classmethod
    def validate_workspace(cls, ws: Any) -> None:
        """拒绝没有 v4 计划但已有旧 parts/块/产物的旧工作区。"""
        reason = cls.legacy_reason(ws)
        if reason:
            raise LegacyWorkspaceError(
                f"旧工作区不能直接使用 v4 BlockPlan：发现 {reason}；请由调用方先迁移或清理"
            )

    # ------------------------------------------------------------------
    # 标题与参数
    # ------------------------------------------------------------------

    @classmethod
    def load_titles(cls, ws: Any) -> Dict[str, Any]:
        """读取根目录 ``block_titles.json``；缺失或损坏时使用退化标题。"""
        path = cls.titles_path(ws)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return cls._normalise_titles(data)

    @staticmethod
    def _normalise_titles(titles: Any) -> Dict[str, Any]:
        if isinstance(titles, Mapping):
            return {
                str(key): value
                for key, value in titles.items()
                if str(key) != "_course" and str(value or "").strip()
            }
        if isinstance(titles, (list, tuple)):
            return {
                str(index + 1): value
                for index, value in enumerate(titles)
                if str(value or "").strip()
            }
        return {}

    @staticmethod
    def _title_for(titles: Mapping[str, Any], index: int, fallback: str) -> str:
        for key in (f"{index:02d}", str(index)):
            value = titles.get(key)
            if value:
                return str(value).strip()
        return fallback

    @staticmethod
    def _offset_titles(titles: Any, offset: int) -> Dict[str, Any]:
        """保留全局标题号，并兼容只给新增块编号的相对标题表。"""
        normalised = BlockPlan._normalise_titles(titles)
        result: Dict[str, Any] = dict(normalised)
        for key, value in normalised.items():
            try:
                shifted = str(int(key) + offset)
            except (TypeError, ValueError):
                continue
            result.setdefault(shifted, value)
        return result

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        return paths.env_float(name, default)

    @classmethod
    def block_minutes(cls) -> float:
        return cls._env_float(ENV_BLOCK_MINUTES, DEFAULT_BLOCK_MINUTES)

    @classmethod
    def oneshot_limit_minutes(cls) -> float:
        return cls._env_float(ENV_ONESHOT_LIMIT_MINUTES, DEFAULT_ONESHOT_LIMIT_MINUTES)

    @classmethod
    def band_minutes(cls) -> Tuple[float, float]:
        low = cls._env_float(ENV_BLOCK_MIN_MINUTES, DEFAULT_BLOCK_MIN_MINUTES)
        high = cls._env_float(ENV_BLOCK_MAX_MINUTES, DEFAULT_BLOCK_MAX_MINUTES)
        if high < low:
            low, high = high, low
        return low, high

    @classmethod
    def limits(cls, target_minutes: Optional[float] = None) -> Dict[str, float]:
        """返回与旧装箱器一致的目标、上限和区间口径。"""
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

    @classmethod
    def _coerce_limits(cls, value: Any) -> Dict[str, float]:
        if value is None:
            return cls.limits()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return cls.limits(float(value))

        if isinstance(value, Mapping):
            data = dict(value)
            if isinstance(data.get("limits"), Mapping):
                data = dict(data["limits"])
            target = data.get("target", data.get("target_minutes"))
            result = cls.limits(float(target) if target is not None else None)
            aliases = {
                "ceiling": ("ceiling", "effective_limit_minutes"),
                "min": ("min", "min_minutes", "block_min_minutes"),
                "max": ("max", "max_minutes", "block_max_minutes"),
            }
            for key, names in aliases.items():
                for name in names:
                    if name in data:
                        result[key] = float(data[name])
                        break
            return cls._validate_limits(result)

        raise ValueError("limits 必须是数字或包含 target/min/max/ceiling 的映射")

    @staticmethod
    def _validate_limits(limits: Mapping[str, Any]) -> Dict[str, float]:
        required = ("target", "ceiling", "min", "max")
        if any(key not in limits for key in required):
            raise ValueError("limits 缺少 target/ceiling/min/max")
        result: Dict[str, float] = {}
        for key in required:
            try:
                number = float(limits[key])
            except (TypeError, ValueError) as err:
                raise ValueError(f"limits[{key!r}] 不是数字") from err
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"limits[{key!r}] 必须是正数")
            result[key] = number
        result["effective"] = min(result["target"], result["ceiling"])
        if result["min"] > result["max"]:
            result["min"], result["max"] = result["max"], result["min"]
        return {key: result[key] for key in ("target", "ceiling", "effective", "min", "max")}

    @classmethod
    def _stored_limits(cls, plan: Mapping[str, Any]) -> Dict[str, float]:
        raw = plan.get("limits")
        if isinstance(raw, Mapping) and all(key in raw for key in ("target", "ceiling", "min", "max")):
            return cls._validate_limits(raw)
        # 兼容早期手工 v4 文件的平铺字段，但不读取旧 blocks.json。
        flat = {
            "target": plan.get("target_minutes"),
            "ceiling": plan.get("ceiling_minutes"),
            "min": plan.get("min_minutes"),
            "max": plan.get("max_minutes"),
        }
        if all(value is not None for value in flat.values()):
            return cls._validate_limits(flat)
        raise ValueError("block_plan.json 缺少完整 limits，无法安全追加")

    @staticmethod
    def _limits_equal(left: Mapping[str, float], right: Mapping[str, float]) -> bool:
        return all(
            math.isclose(float(left[key]), float(right[key]), rel_tol=0.0, abs_tol=1e-9)
            for key in ("target", "ceiling", "effective", "min", "max")
        )

    # ------------------------------------------------------------------
    # 纯装箱算法（纯元数据规划，不依赖旧清单或音频）
    # ------------------------------------------------------------------

    @classmethod
    def _partition(cls, weights: Sequence[float], num_blocks: int) -> List[List[int]]:
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
            low = cuts[-1] + 1 if cuts else 1
            high = count - (num_blocks - k)
            if high < low:
                high = low
            best = low
            best_gap: Optional[float] = None
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

    @staticmethod
    def _band_penalty(
        groups: Sequence[Sequence[int]],
        weights: Sequence[float],
        min_sec: float,
        max_sec: float,
        target_sec: float,
    ) -> float:
        penalty = 0.0
        for group in groups:
            span = sum(weights[index] for index in group)
            if span < min_sec:
                penalty += (min_sec - span) / 60.0
            if span > max_sec:
                penalty += (span - max_sec) / 60.0 * 10.0
            penalty += abs(span - target_sec) / 60.0 * 0.01
        return penalty

    @classmethod
    def pack(
        cls,
        durations: Sequence[float],
        target_minutes: float,
        max_minutes: float,
        min_minutes: float = 0.0,
    ) -> List[List[int]]:
        """把装箱单元按连续边界装入目标时长块。"""
        count = len(durations)
        if count == 0:
            return []
        weights = [max(0.0, float(value)) for value in durations]
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
    def _part_labels(page: int, parts: int) -> List[str]:
        if parts == 2:
            return [f"P{page:02d}上", f"P{page:02d}下"]
        return [
            f"P{page:02d}" + {0: "上", parts - 1: "下"}.get(index, f"中{index}")
            for index in range(parts)
        ]

    @staticmethod
    def _record_duration(record: Mapping[str, Any]) -> float:
        raw = record.get("duration")
        if raw is None:
            raw = record.get("duration_sec")
        if raw is None and record.get("duration_min") is not None:
            try:
                raw = float(record["duration_min"]) * 60.0
            except (TypeError, ValueError) as err:
                raise ValueError("part duration_min 不是数字") from err
        try:
            value = float(raw or 0.0)
        except (TypeError, ValueError) as err:
            raise ValueError("part duration 不是数字") from err
        if not math.isfinite(value) or value < 0:
            raise ValueError("part duration 必须是非负有限数")
        return value

    @staticmethod
    def _unit(
        page: int,
        label: str,
        start_sec: float,
        end_sec: float,
        split: bool,
    ) -> Dict[str, Any]:
        return {
            "page": int(page),
            "label": str(label),
            "start_sec": round(float(start_sec), 2),
            "end_sec": round(float(end_sec), 2),
            "duration_sec": round(max(0.0, float(end_sec) - float(start_sec)), 2),
            "split": bool(split),
        }

    @classmethod
    def plan_units(
        cls,
        records: Sequence[Mapping[str, Any]],
        min_minutes: float,
        max_minutes: float,
    ) -> List[Dict[str, Any]]:
        """把分集规划为整集单元或超长集的连续劈分腿。"""
        max_sec = max(1.0, float(max_minutes) * 60.0)
        floor_sec = float(min_minutes) * 60.0 * SPLIT_MIN_TOLERANCE
        units: List[Dict[str, Any]] = []
        for record in records:
            page = int(record.get("page") or 0)
            duration = cls._record_duration(record)
            whole = cls._unit(page, f"P{page:02d}", 0.0, duration, False)
            if duration <= max_sec or duration <= 0:
                units.append(whole)
                continue
            parts = max(2, int(math.ceil(duration / max_sec - 1e-9)))
            piece = duration / parts
            if piece < floor_sec:
                units.append(whole)
                continue
            for index, label in enumerate(cls._part_labels(page, parts)):
                start = piece * index
                end = duration if index == parts - 1 else piece * (index + 1)
                units.append(cls._unit(page, label, start, end, True))
        return units

    @staticmethod
    def block_stem(episodes: Sequence[int]) -> str:
        pages = sorted(int(page) for page in episodes)
        if not pages:
            return "P00"
        if len(pages) == 1:
            return f"P{pages[0]:02d}"
        return f"P{pages[0]:02d}-P{pages[-1]:02d}"

    @classmethod
    def block_span(cls, block: Mapping[str, Any]) -> str:
        # 计划落盘时 span 已是锁定的块身份；只有旧/手工输入缺 span 时才从 units 推导。
        locked = str(block.get("span") or "").strip()
        if locked:
            return locked
        units = block.get("units") or []
        labels = [str(unit.get("label") or "") for unit in units if unit.get("label")]
        if not labels:
            return cls.block_stem([int(page) for page in (block.get("episodes") or [])])
        return labels[0] if len(labels) == 1 else f"{labels[0]}-{labels[-1]}"

    @classmethod
    def span(cls, block: Mapping[str, Any]) -> str:
        """下游统一使用的块范围别名。"""
        return cls.block_span(block)

    # ------------------------------------------------------------------
    # parts 归一化与块结构
    # ------------------------------------------------------------------

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _clean_source_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): cls._clean_source_value(item)
                for key, item in value.items()
                if str(key).lower() not in _AUDIO_PATH_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [cls._clean_source_value(item) for item in value]
        if isinstance(value, Path):
            return value.as_posix()
        return value

    @classmethod
    def _source_part(cls, part: Mapping[str, Any], page: int, title: str, duration: float) -> Dict[str, Any]:
        cleaned = cls._clean_source_value(dict(part))
        if not isinstance(cleaned, dict):
            cleaned = {}
        for key in ("page", "title", "duration", "duration_sec", "duration_min"):
            cleaned.pop(key, None)
        cleaned["page"] = int(page)
        cleaned["title"] = str(title)
        cleaned["duration"] = round(float(duration), 2)
        return cleaned

    @classmethod
    def _records(cls, parts: Any) -> List[Dict[str, Any]]:
        if isinstance(parts, (str, bytes)) or not isinstance(parts, Sequence):
            raise ValueError("parts 必须是分集元数据列表")
        records: List[Dict[str, Any]] = []
        for index, part in enumerate(parts):
            if not isinstance(part, Mapping):
                raise ValueError(f"parts[{index}] 必须是对象")
            try:
                page = int(part.get("page"))
            except (TypeError, ValueError) as err:
                raise ValueError(f"parts[{index}] 缺少有效 page") from err
            if page <= 0:
                raise ValueError(f"parts[{index}] page 必须为正数")
            title = str(part.get("title") or "").strip()
            duration = cls._record_duration(part)
            records.append(
                {
                    "page": page,
                    "title": title,
                    "duration": duration,
                    "source": cls._source_part(part, page, title, duration),
                    "fingerprint": cls._digest(dict(part)),
                }
            )
        records.sort(key=lambda record: record["page"])
        pages = [record["page"] for record in records]
        if len(pages) != len(set(pages)):
            raise ValueError("parts 中存在重复 page")
        return records

    @classmethod
    def _source(cls, records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        parts = [copy.deepcopy(record["source"]) for record in records]
        return {
            "kind": "parts",
            "part_count": len(parts),
            "pages": [int(record["page"]) for record in records],
            "parts": parts,
            "part_fingerprints": [str(record.get("fingerprint") or "") for record in records],
            "digest": cls._digest(parts),
        }

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        total = max(0, int(float(seconds)))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    @classmethod
    def _fallback_title(cls, records: Sequence[Mapping[str, Any]], page: int) -> str:
        for record in records:
            if int(record["page"]) == int(page):
                raw = str(record.get("title") or "").strip()
                return sanitize_filename(raw, max_len=24) if raw else f"P{int(page):02d}"
        return f"P{int(page):02d}"

    @classmethod
    def _build_blocks(
        cls,
        records: Sequence[Mapping[str, Any]],
        limits: Mapping[str, float],
        titles: Any = None,
        start_id: int = 1,
    ) -> List[Dict[str, Any]]:
        title_map = cls._normalise_titles(titles)
        units = cls.plan_units(records, float(limits["min"]), float(limits["max"]))
        groups = cls.pack(
            [unit["duration_sec"] for unit in units],
            float(limits["target"]),
            float(limits["max"]),
            float(limits["min"]),
        )
        by_page = {int(record["page"]): record for record in records}
        blocks: List[Dict[str, Any]] = []
        for offset, group in enumerate(groups):
            members = [units[index] for index in group]
            member_records: List[Mapping[str, Any]] = []
            seen_pages = set()
            for member in members:
                page = int(member["page"])
                if page not in seen_pages:
                    member_records.append(by_page[page])
                    seen_pages.add(page)
            pages = sorted(seen_pages)
            span = cls.block_span({"units": members, "episodes": pages})
            fallback = cls._fallback_title(records, pages[0])
            block_id = int(start_id) + offset
            title = cls._title_for(title_map, block_id, fallback)

            unit_json: List[Dict[str, Any]] = []
            segment_json: List[Dict[str, Any]] = []
            cursor = 0.0
            for member in members:
                record = by_page[int(member["page"])]
                source = copy.deepcopy(record["source"])
                unit_json.append(
                    {
                        "page": int(member["page"]),
                        "label": str(member["label"]),
                        "split": bool(member["split"]),
                        "source_offset_sec": round(float(member["start_sec"]), 2),
                        "start_sec": round(float(member["start_sec"]), 2),
                        "end_sec": round(float(member["end_sec"]), 2),
                        "duration_sec": round(float(member["duration_sec"]), 2),
                        "source": source,
                    }
                )
                duration = float(member["duration_sec"])
                end = cursor + duration
                segment_json.append(
                    {
                        "page": int(member["page"]),
                        "label": str(member["label"]),
                        "start_sec": round(cursor, 2),
                        "end_sec": round(end, 2),
                        "start": cls._format_seconds(cursor),
                        "end": cls._format_seconds(end),
                        "duration_sec": round(duration, 2),
                        "episode_split": bool(member["split"]),
                        "source": {
                            "offset_sec": round(float(member["start_sec"]), 2),
                            "metadata": copy.deepcopy(record["source"]),
                        },
                    }
                )
                cursor = end

            total = round(cursor, 2)
            blocks.append(
                {
                    "block_id": block_id,
                    "title": title,
                    "span": span,
                    "episodes": pages,
                    "units": unit_json,
                    "segments": segment_json,
                    "duration_sec": total,
                    "duration_min": round(total / 60.0, 2),
                    "source": cls._source(member_records),
                    "episode_split": any(unit["split"] for unit in members),
                    "single_episode": len(members) == 1 and not members[0]["split"],
                    "audio_ready": False,
                }
            )
        return blocks

    @classmethod
    def _new_plan(
        cls,
        records: Sequence[Mapping[str, Any]],
        limits: Mapping[str, float],
        titles: Any,
    ) -> Dict[str, Any]:
        return {
            "version": cls.VERSION,
            "plan_source": "metadata",
            "audio_ready": False,
            "materialization": {"status": cls.MATERIALIZATION_STATUS, "audio_ready": False},
            "limits": dict(limits),
            "source": cls._source(records),
            "blocks": cls._build_blocks(records, limits, titles),
        }

    @classmethod
    def plan_from_parts(
        cls,
        parts: Sequence[Mapping[str, Any]],
        limits: Any = None,
        titles: Any = None,
    ) -> Dict[str, Any]:
        """仅用 parts 元数据生成完整 v4 计划，不触碰工作区或音频。"""
        records = cls._records(parts)
        return cls._new_plan(records, cls._coerce_limits(limits), titles)

    # ------------------------------------------------------------------
    # 持久化与追加
    # ------------------------------------------------------------------

    @classmethod
    def _validate_plan(cls, plan: Any, strict: bool = False) -> None:
        if not isinstance(plan, Mapping):
            raise ValueError("block_plan 必须是对象")
        try:
            version = int(plan.get("version"))
        except (TypeError, ValueError) as err:
            raise ValueError("block_plan 缺少有效 version") from err
        if version != cls.VERSION:
            raise ValueError(f"不支持的 block_plan version={version}")
        blocks = plan.get("blocks")
        if not isinstance(blocks, list):
            raise ValueError("block_plan.blocks 必须是列表")
        for index, block in enumerate(blocks):
            if not isinstance(block, Mapping):
                raise ValueError(f"block_plan.blocks[{index}] 必须是对象")
            if "audio" in block:
                raise ValueError(f"block_plan.blocks[{index}] 不得写 audio 路径")
            try:
                block_id = int(block.get("block_id"))
            except (TypeError, ValueError) as err:
                raise ValueError(f"block_plan.blocks[{index}] 缺少有效 block_id") from err
            if block_id <= 0 or not isinstance(block.get("episodes"), list) or not block["episodes"]:
                raise ValueError(f"block_plan.blocks[{index}] 结构不完整")
        if strict:
            if plan.get("plan_source") != "metadata":
                raise ValueError("block_plan.plan_source 必须是 metadata")
            if not isinstance(plan.get("source"), Mapping) or not isinstance(plan["source"].get("parts"), list):
                raise ValueError("block_plan 缺少 source.parts，无法安全追加")
            if not isinstance(plan.get("limits"), Mapping):
                raise ValueError("block_plan 缺少 limits，无法安全追加")
            for index, block in enumerate(blocks):
                missing = _REQUIRED_BLOCK_KEYS - set(block)
                if missing:
                    raise ValueError(f"block_plan.blocks[{index}] 缺少字段：{', '.join(sorted(missing))}")

    @classmethod
    def load(cls, ws: Any) -> Optional[Dict[str, Any]]:
        """读取 v4 计划；缺失、损坏或版本不符时返回 ``None``。"""
        path = cls.path(ws)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cls._validate_plan(data)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return None
        return data

    @classmethod
    def load_blocks(cls, ws: Any) -> List[Dict[str, Any]]:
        """读取计划块；绝不读取旧 ``audio/_blocks/blocks.json``。"""
        plan = cls.load(ws) or {}
        blocks = plan.get("blocks") if isinstance(plan, Mapping) else None
        return [block for block in blocks if isinstance(block, dict)] if isinstance(blocks, list) else []

    @classmethod
    def save(cls, ws: Any, plan: Mapping[str, Any]) -> Path:
        """原子保存计划；只写 JSON，不创建或读取音频。"""
        cls._validate_plan(plan)
        path = cls.path(ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".json.tmp.{os.getpid()}")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(plan, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    @classmethod
    def append(
        cls,
        plan: Mapping[str, Any],
        parts: Sequence[Mapping[str, Any]],
        limits: Any = None,
        titles: Any = None,
    ) -> Dict[str, Any]:
        """只把新分集规划成新块，旧块与旧边界原样保留。"""
        cls._validate_plan(plan, strict=True)
        current = cls._records(parts)
        old_source = plan["source"]
        old_parts = old_source.get("parts")
        old_fingerprints = old_source.get("part_fingerprints")
        if not isinstance(old_parts, list):
            raise ValueError("block_plan.source.parts 缺失，无法安全追加")

        old_limits = cls._stored_limits(plan)
        requested_limits = old_limits if limits is None else cls._coerce_limits(limits)
        if not cls._limits_equal(old_limits, requested_limits):
            raise ValueError("block_plan limits 已改变；连载只能沿用旧 limits")

        old_count = len(old_parts)
        if len(current) < old_count:
            raise ValueError(
                f"parts 数量从 {old_count} 减少到 {len(current)}；连载课程不能删除旧 parts"
            )
        for index, old_part in enumerate(old_parts):
            current_part = current[index]["source"]
            same = current_part == old_part
            if isinstance(old_fingerprints, list) and index < len(old_fingerprints):
                same = same and current[index]["fingerprint"] == old_fingerprints[index]
            if not same:
                old_page = old_part.get("page", index + 1)
                raise ValueError(
                    f"旧 parts 前缀第 {index + 1} 项（P{old_page}）已改变；连载只允许追加新 parts"
                )

        if len(current) == old_count:
            return copy.deepcopy(dict(plan))

        next_id = max(
            (int(block.get("block_id") or 0) for block in plan.get("blocks") or []),
            default=0,
        ) + 1
        new_blocks = cls._build_blocks(
            current[old_count:],
            requested_limits,
            cls._offset_titles(titles, next_id - 1),
            start_id=next_id,
        )
        result = copy.deepcopy(dict(plan))
        result["source"] = cls._source(current)
        result["blocks"] = list(result.get("blocks") or []) + new_blocks
        result["audio_ready"] = False
        result["materialization"] = {
            "status": cls.MATERIALIZATION_STATUS,
            "audio_ready": False,
        }
        return result

    @classmethod
    def ensure(
        cls,
        ws: Any,
        parts: Sequence[Mapping[str, Any]],
        limits: Any = None,
        titles: Any = None,
    ) -> Dict[str, Any]:
        """首次创建或安全追加计划，并原子落盘。"""
        plan_path = cls.path(ws)
        existing = cls.load(ws)
        if plan_path.exists():
            if existing is None:
                raise ValueError("已有 block_plan.json 无法读取或版本不符；请由调用方处理")
            if titles is None:
                titles = cls.load_titles(ws)
            updated = cls.append(existing, parts, limits=limits, titles=titles)
            if updated != existing:
                cls.save(ws, updated)
            return updated

        cls.validate_workspace(ws)
        if titles is None:
            titles = cls.load_titles(ws)
        plan = cls.plan_from_parts(parts, limits=limits, titles=titles)
        cls.save(ws, plan)
        return plan

    # ------------------------------------------------------------------
    # 物化路径只推导，不创建、不读取
    # ------------------------------------------------------------------

    @staticmethod
    def _source_parts(block: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        source = block.get("source")
        if isinstance(source, list):
            return [item for item in source if isinstance(item, Mapping)]
        if not isinstance(source, Mapping):
            return []
        parts = source.get("parts")
        if isinstance(parts, list):
            return [item for item in parts if isinstance(item, Mapping)]
        part = source.get("part")
        return [part] if isinstance(part, Mapping) else []

    @classmethod
    def _single_whole(cls, block: Mapping[str, Any]) -> bool:
        units = block.get("units") or []
        if units:
            return len(units) == 1 and not bool(units[0].get("split"))
        episodes = block.get("episodes") or []
        return len(episodes) == 1 and not bool(block.get("episode_split"))

    @classmethod
    def source_audio_path(cls, ws: Any, part: Mapping[str, Any]) -> Path:
        """按 parts 元数据推导分集源音频路径；不创建或读取文件。"""
        page = int(part.get("page") or 0)
        title = sanitize_filename(str(part.get("title") or f"P{page:02d}"))
        return cls._audio_root(ws) / f"P{page:02d}_{title}.m4a"

    @classmethod
    def audio_path(cls, ws: Any, block: Mapping[str, Any]) -> Path:
        """按 v4 物化器规则推导音频路径；绝不创建目录或文件。"""
        audio_root = cls._audio_root(ws)
        if cls._single_whole(block):
            page = int((block.get("episodes") or [0])[0] or 0)
            title = ""
            for part in cls._source_parts(block):
                if int(part.get("page") or 0) == page:
                    title = str(part.get("title") or "")
                    break
            title = title or str(block.get("title") or "") or f"P{page:02d}"
            expected = audio_root / f"P{page:02d}_{sanitize_filename(title)}.m4a"
            if expected.is_file():
                return expected
            try:
                candidates = sorted(
                    item for item in audio_root.glob(f"P{page:02d}_*.m4a") if item.is_file()
                )
            except OSError:
                candidates = []
            return candidates[0] if len(candidates) == 1 else expected

        block_id = int(block.get("block_id") or 0)
        title = sanitize_filename(str(block.get("title") or f"BLK{block_id:02d}"))
        span = cls.span(block) or "P00"
        return cls.blocks_dir(ws) / f"BLK{block_id:02d}_{title}({span}).m4a"

    @classmethod
    def audio_ready(cls, ws: Any, block: Mapping[str, Any]) -> bool:
        """检查推导出的物理音频是否存在且非空，不打开音频内容。"""
        try:
            target = cls.audio_path(ws, block)
            if target.is_file() and target.stat().st_size > 0:
                return True
            units = block.get("units") or []
            if len(units) == 1 and bool(units[0].get("split")):
                label = str(units[0].get("label") or "")
                block_id = int(block.get("block_id") or 0)
                sliced = cls.blocks_dir(ws) / "_parts" / f"BLK{block_id:02d}_{label}.m4a"
                return sliced.is_file() and sliced.stat().st_size > 0
            return False
        except (OSError, TypeError, ValueError):
            return False


__all__ = [
    "BlockPlan",
    "LegacyWorkspaceError",
    "ENV_BLOCK_MINUTES",
    "ENV_ONESHOT_LIMIT_MINUTES",
    "ENV_BLOCK_MIN_MINUTES",
    "ENV_BLOCK_MAX_MINUTES",
    "DEFAULT_BLOCK_MINUTES",
    "DEFAULT_ONESHOT_LIMIT_MINUTES",
    "DEFAULT_BLOCK_MIN_MINUTES",
    "DEFAULT_BLOCK_MAX_MINUTES",
    "SPLIT_MIN_TOLERANCE",
]
