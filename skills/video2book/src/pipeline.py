#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pipeline Coordination Domain Service（流水线领域调度服务）。

职责：
1. 承载 v4 plan-first 编排：元数据 → BlockPlan → 字幕 → 按需音频物化 → 任务书 → 笔记收尾。
2. 收口跨命令共享的领域辅助函数（目标解析、412 富化、范围解析、任务书导出）。
3. 通过 PipelineGateError 表达硬门禁终止，CLI 层转换为进程退出码。

设计约束：
- 不依赖 argparse 命名空间，参数全部显式传入，可独立单元测试；
- 物理音频永远不进入 BlockPlan，所有路径与范围由 v4 API 推导。
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from src.core.audio_materializer import AudioMaterializer, AudioMaterializationError
from src.core.block_plan import BlockPlan
from src.core.subtitle_service import SubtitleService
from src.core.workspace import TaskWorkspace, sanitize_filename
from src.core import paths as _paths
from src.generator.block_synthesizer import BlockSynthesizer
from src.prompts import ArticlePromptTypeError

# 代码根（skill/）——仅用于断点续跑提示等展示；产物路径一律走 paths.products_root()
PROJECT_ROOT = _paths.code_root()

# 分集作品类型：非视频作品（抖音图文/图集 note）不参与听音与长文生成。
# 见 references/non-video-works.md。
KIND_VIDEO = "video"
KIND_IMAGE_ALBUM = "image_album"


def part_kind(part: Dict[str, Any]) -> str:
    """分集的作品类型；缺失一律按视频处理。

    为什么必须容错：`parts.json` 是**跨版本长期缓存**——B 站 / YouTube / 本地媒体
    三种来源，以及本改动之前建立的所有抖音工作区，条目里都没有 `media_kind`。
    用 `part["media_kind"]` 会 KeyError 让整条流水线崩在阶段一。
    """
    return str(part.get("media_kind") or KIND_VIDEO)


def _resolve_status_file() -> Path:
    """状态文件（记录上次 412/熔断）路径：产物根下唯一一份。

    三域分离后不再探测当前工作目录：在任意目录执行命令都写同一个状态文件，
    不会在别处凭空生成一个 output/。仅保留「读取旧 cwd/output 状态文件」的兼容探测。
    """
    current = _paths.products_root() / ".cli_status.json"
    if current.exists():
        return current
    legacy_cwd = Path.cwd() / "output" / ".cli_status.json"
    if legacy_cwd.exists():
        return legacy_cwd
    return current

_STATUS_FILE = _resolve_status_file()

# 412 断点续跑默认提示命令
_RESUME_HINT = 'video2book pipeline "<链接>" --all --sessdata YOUR_SESSDATA'


class PipelineGateError(RuntimeError):
    """流水线硬门禁终止信号（CLI 层捕获后转换为 sys.exit(exit_code)）。"""

    def __init__(self, exit_code: int, message: str = ""):
        super().__init__(message or f"pipeline gate terminated (exit {exit_code})")
        self.exit_code = exit_code


from src.core.taskbook import (
    TRANSCRIBE_INSTRUCTION,
    export_block_article_task,
    export_block_transcribe_task,
    export_page_transcribe_task,
    resolve_article_type,
)

__all__ = [
    "TRANSCRIBE_INSTRUCTION",
    "export_block_article_task",
    "export_block_transcribe_task",
    "export_page_transcribe_task",
    "resolve_article_type",
    "PipelineCoordinator",
    "part_kind",
    "KIND_VIDEO",
    "KIND_IMAGE_ALBUM",
]


def resolve_target_info(
    target: str,
    sessdata: Optional[str] = None,
    custom_task: Optional[str] = None,
    base_dir: Optional[Any] = None,
    limit: Optional[int] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """多态解析本地媒体、B站、YouTube 或抖音元数据；网络失败时用本地缓存离线自愈。"""
    from src.core.ingestion import get_coordinator

    coordinator = get_coordinator()
    return coordinator.resolve_target_info(
        target,
        sessdata=sessdata,
        custom_task=custom_task,
        base_dir=base_dir,
        limit=limit,
        **kwargs,
    )


def resolve_scope_parts(info: Dict[str, Any], ws: Any) -> List[Dict[str, Any]]:
    """集号基准：**工作区 `parts.json` 优先**，缺失才用在线解析结果。

    为什么不能直接用 `info["parts"]`：在线解析永远返回课程**全集**。用户 `--range 9-87`
    建的工作区里只有 P09–P87，拿 185 集当基准会让规划任务书列错集号、让规划校验把
    用户自己的区间判成非法，甚至把整个阶段二卡死——集号是工作区的事实，工具无权放大它。

    在线解析只用于**首次建工作区**（`pipeline`），此后一律以工作区为准。
    """
    local = [
        p for p in (ws.load_parts() or [])
        if isinstance(p, dict) and p.get("page") is not None
    ]
    online = [p for p in (info.get("parts") or []) if isinstance(p, dict)]
    if not local:
        return online
    if online and {int(p["page"]) for p in online} != {int(p["page"]) for p in local}:
        print(
            f"[i] 集号基准取工作区 parts.json（{len(local)} 集，"
            f"P{min(int(p['page']) for p in local):02d}–P{max(int(p['page']) for p in local):02d}）；"
            f"在线全集为 {len(online)} 集——以工作区为准"
        )
    return sorted(local, key=lambda p: int(p["page"]))


def resolve_course_title(info: Dict[str, Any], ws: Any) -> str:
    """课程标题：manifest → parts.json 所属工作区名 → 在线解析（离线也拿得到）。

    标题会写进规划提示词与任务书抬头，取错了会让 Agent 对着别的课名做规划。
    """
    try:
        manifest_title = str((ws.load_manifest() or {}).get("title") or "").strip()
        if manifest_title:
            return manifest_title
    except Exception:
        pass
    online_title = str(info.get("title") or "").strip()
    if online_title:
        return online_title
    return ws.root_dir.name


def parse_range_string(range_str: str, max_val: int) -> List[int]:
    """解析范围字符串（'1-10' / '1,3,5' / '5-'）为 1 起始的分集页码列表。"""
    pages: Set[int] = set()
    if max_val <= 0:
        return []
    for part in range_str.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                s_str, e_str = part.split("-", 1)
                start = int(s_str.strip()) if s_str.strip() else 1
                end = int(e_str.strip()) if e_str.strip() else max_val
                if start <= max_val and end >= 1:
                    for i in range(max(1, start), min(max_val, end) + 1):
                        pages.add(i)
            else:
                p = int(part)
                if 1 <= p <= max_val:
                    pages.add(p)
        except ValueError:
            continue
    return sorted(pages)


class PipelineCoordinator:
    """v4 plan-first 流水线调度器。

    物理音频不是计划的一部分：先由 ``BlockPlan.ensure`` 锁定逻辑块，再按字幕缺口
    选择性物化音频，最后导出任务书。计划文件一旦存在就只允许按分集追加，``--force``
    不会重算或覆盖它。
    """

    @staticmethod
    def _source_type(info: Mapping[str, Any]) -> str:
        return str(
            info.get("source_type")
            or ("local" if info.get("is_local") else "bilibili")
        ).strip().lower()

    @staticmethod
    def _is_bilibili(info: Mapping[str, Any]) -> bool:
        """只把明确的 B 站来源送进字幕服务，不能把 YouTube/本地误判成 B 站。"""
        source = PipelineCoordinator._source_type(info)
        if source == "bilibili":
            return True
        if source in {"local", "youtube", "douyin"}:
            return False
        bvid = str(info.get("bvid") or "").strip().upper()
        return bvid.startswith("BV")

    @staticmethod
    def _clean_parts(parts: Any) -> List[Dict[str, Any]]:
        """去掉解析器临时字段，保留完整分集元数据供计划追加和物化使用。"""
        return [
            {str(k): v for k, v in part.items() if not str(k).startswith("_")}
            for part in (parts or [])
            if isinstance(part, dict) and part.get("page") is not None
        ]

    @staticmethod
    def _parts_by_page(parts: Any) -> Dict[int, Dict[str, Any]]:
        result: Dict[int, Dict[str, Any]] = {}
        for part in parts or []:
            if not isinstance(part, dict) or part.get("page") is None:
                continue
            try:
                result[int(part["page"])] = part
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _block_ids(value: Any) -> List[int]:
        """把字幕服务可能返回的 id/块对象/路径记录统一成块号列表。"""
        if value is None:
            return []
        if isinstance(value, (str, bytes, Path)):
            value = [value]
        if isinstance(value, Mapping):
            value = [value]
        result: List[int] = []
        for item in value:
            if isinstance(item, Mapping):
                raw = item.get("block_id", item.get("id"))
            else:
                raw = item
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in result:
                result.append(number)
        return result

    @classmethod
    def _subtitle_state(
        cls,
        raw: Any,
        blocks: List[Dict[str, Any]],
        ws: Any,
    ) -> Dict[str, Any]:
        """兼容服务返回的稳定字段与早期 ``missing`` 字段，不改变服务实现。"""
        result = dict(raw) if isinstance(raw, Mapping) else {}
        all_ids = [int(b.get("block_id") or 0) for b in blocks]
        valid = set(all_ids)
        ready = [i for i in cls._block_ids(result.get("subtitle_ready")) if i in valid]
        written_ids = [i for i in cls._block_ids(result.get("written")) if i in valid]
        for block_id in written_ids:
            if block_id not in ready:
                ready.append(block_id)
        cached = [i for i in cls._block_ids(result.get("cached")) if i in valid]
        if "needs_audio" in result:
            needs = [i for i in cls._block_ids(result.get("needs_audio")) if i in valid]
        elif "missing" in result:
            needs = [i for i in cls._block_ids(result.get("missing")) if i in valid]
        else:
            needs = [i for i in all_ids if i not in set(ready) | set(cached)]

        # 服务若只返回 written/missing，补齐稳定字段；若它报告了 ready，则不再凭
        # 文件系统猜测其它块，避免把字幕成功的块错误地重新下载。
        transcript_ready: List[int] = []
        for block in blocks:
            try:
                target = TaskWorkspace.block_path(ws, block)
                if target.is_file() and target.stat().st_size > 0:
                    transcript_ready.append(int(block.get("block_id") or 0))
            except (OSError, TypeError, ValueError):
                continue
        if not needs and not ready and not cached:
            needs = [i for i in all_ids if i not in set(transcript_ready)]
        result.update({
            "subtitle_ready": ready,
            "cached": cached,
            "needs_audio": [i for i in needs if i not in set(cached) | set(ready)],
            "written": list(result.get("written") or []),
            "errors": list(result.get("errors") or []),
        })
        return result

    @staticmethod
    def _transcript_exists(ws: Any, block: Mapping[str, Any]) -> bool:
        try:
            path = TaskWorkspace.block_path(ws, dict(block))
            return path.is_file() and path.stat().st_size > 0
        except (OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _page_transcript_exists(ws: Any, block: Mapping[str, Any], page: int) -> bool:
        """块内某一集的**补录稿**是否已落盘。"""
        try:
            path = TaskWorkspace.page_transcript_path(ws, dict(block), int(page))
            return path.is_file() and path.stat().st_size > 0
        except (OSError, TypeError, ValueError):
            return False

    @classmethod
    def _merge_completed_blocks(
        cls, ws: Any, blocks: Sequence[Mapping[str, Any]], 缺页表: Mapping[int, Sequence[int]]
    ) -> List[Dict[str, Any]]:
        """把「字幕部分稿 + 缺的各集补录稿」合并成最终块级逐字稿。

        只处理**补录稿已齐**的块；缺任一集就整块不动——半成品落到最终名会让下游
        （写作派发、阶段一门禁、queue_tracker）误以为该块语料已完工。

        合并所需的两份原料从 sidecar 状态与部分稿重建：部分稿里已含各集拼好的正文，
        这里按 `segments` 顺序把「字幕集正文 + 听音集正文」拼回去。
        """
        from src.core import subtitles as subtitle_core

        产出: List[Dict[str, Any]] = []
        for block in blocks:
            block_id = int(block.get("block_id") or 0)
            缺集 = [int(p) for p in (缺页表.get(block_id) or [])]
            if not 缺集:
                continue
            if cls._transcript_exists(ws, block):
                continue
            if not all(cls._page_transcript_exists(ws, block, p) for p in 缺集):
                continue
            state_path = TaskWorkspace.partial_state_path(ws, dict(block))
            if not state_path.is_file():
                continue
            try:
                状态 = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ready = {int(p) for p in (状态.get("ready_pages") or [])}
            听音稿: Dict[int, str] = {}
            for p in 缺集:
                try:
                    听音稿[p] = TaskWorkspace.page_transcript_path(ws, dict(block), p).read_text(
                        encoding="utf-8", errors="ignore")
                except OSError:
                    pass
            合并 = subtitle_core.merge_block_transcript(dict(block), {
                "page_sources": {p: "subtitle" for p in ready} | {p: "missing" for p in 缺集},
                "missing_pages": 缺集,
                "segment_text": _读部分稿片段(ws, block, state_path),
            }, 听音稿)
            if not 合并.get("ok"):
                continue
            path = TaskWorkspace.write_block_transcript(ws, dict(block), 合并["text"])
            for p in 缺集:
                try:
                    TaskWorkspace.page_transcript_path(ws, dict(block), p).unlink()
                except OSError:
                    pass
            try:
                state_path.unlink()
            except OSError:
                pass
            产出.append({"block_id": block_id, "path": str(path),
                          "audio_pages": 缺集, "page_sources": 合并.get("page_sources") or {}})
        return 产出


    @staticmethod
    def _block_is_video(block: Mapping[str, Any], video_pages: Set[int]) -> bool:
        pages = {
            int(p) for p in (
                block.get("episodes") or []
            ) if str(p).lstrip("-").isdigit()
        }
        units = block.get("units") or []
        if units:
            pages.update(
                int(u.get("page") or 0)
                for u in units
                if isinstance(u, Mapping) and str(u.get("page") or "").lstrip("-").isdigit()
            )
        return bool(pages) and pages.issubset(video_pages)

    @staticmethod
    def _blocks_in_scope(
        blocks: Sequence[Mapping[str, Any]], selected_pages: Set[int]
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """返回完整落在本次选中分集内的块，以及与选中范围相交的块。

        块是原子模块：局部运行不能用半个块生成逐字稿或长文任务书。完整计划仍由
        ``BlockPlan`` 保存，局部运行只在派发/物化阶段收窄处理范围。
        """
        if not selected_pages:
            return [dict(b) for b in blocks], []
        in_scope: List[Dict[str, Any]] = []
        partial: List[Dict[str, Any]] = []
        for raw in blocks:
            pages = {
                int(p) for p in (raw.get("episodes") or [])
                if str(p).lstrip("-").isdigit()
            }
            if pages and pages.issubset(selected_pages):
                in_scope.append(dict(raw))
            elif pages & selected_pages:
                partial.append(dict(raw))
        return in_scope, partial

    @staticmethod
    def _fetch_callback(
        info: Mapping[str, Any],
        sessdata: Optional[str],
        quality: str,
        force: bool,
    ):
        """把物化器的下载回调接到统一 IngestionCoordinator。"""
        from src.core.ingestion import get_coordinator

        coordinator = get_coordinator()

        def _fetch(part: Dict[str, Any], output: Path) -> Path:
            """物化器只传分集元数据；课程级 info 在闭包中捕获，避免参数错位。"""
            if not isinstance(part, Mapping):
                raise AudioMaterializationError("音频物化回调没有收到分集元数据")
            return coordinator.fetch_episode_audio(
                dict(info),
                dict(part),
                Path(output),
                force=force,
                sessdata=sessdata,
                quality=quality,
            )

        return _fetch

    @staticmethod
    def _save_runtime_manifest(
        ws: Any,
        plan: Mapping[str, Any],
        subtitle_state: Mapping[str, Any],
        materialized: Mapping[int, Any],
        video_blocks: List[Dict[str, Any]],
        *,
        mode: str,
    ) -> Dict[str, Any]:
        """只记录计划路径和运行态，不把块定义复制回 manifest。"""
        try:
            manifest = ws.load_manifest(absolute=True)
        except Exception:
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        manifest.pop("audio_blocks", None)
        manifest.pop("blocks_manifest", None)
        block_ids = [int(b.get("block_id") or 0) for b in video_blocks]
        audio_ready = [
            int(block.get("block_id") or 0) for block in video_blocks
            if BlockPlan.audio_ready(ws, block)
        ]
        # 已回收的工作区：音频自然不在盘，`len(audio_ready) == len(block_ids)`
        # 永远不成立，若照常算会把成功回收的证据改写成 `partial`——看起来像
        # 音频物化出了问题。所以先认「回收过」这个既有事实。
        曾回收 = isinstance(manifest.get("audio_purge"), dict)
        if 曾回收 and not audio_ready:
            status = "purged"
        else:
            status = "ready" if not block_ids or len(audio_ready) == len(block_ids) else "partial"
        materialization: Dict[str, Any] = {
            "requested": list(subtitle_state.get("needs_audio") or []),
            "ready": audio_ready,
        }
        if 曾回收:
            # 保留回收发生过的事实：清空 ready 会让重跑看不出曾经回收过多少。
            materialization["purged"] = True
        manifest.update({
            "block_plan": str(BlockPlan.path(ws)),
            "pipeline": {
                "mode": str(mode),
                "status": status,
                "subtitle": {
                    "ready": list(subtitle_state.get("subtitle_ready") or []),
                    "cached": list(subtitle_state.get("cached") or []),
                    "needs_audio": list(subtitle_state.get("needs_audio") or []),
                    "written": list(subtitle_state.get("written") or []),
                    "errors": list(subtitle_state.get("errors") or []),
                    # 判不可用的分集与原因（串台 / 本来没有字幕 / 重试后仍不完整）
                    "unavailable": list(subtitle_state.get("unavailable") or []),
                    # 块级丢页：`unavailable` 是**页级**的，只说「P07 判不可用」；
                    # 这里补上「因此 BLK03 整块转了音频」这条因果链——否则判断某块为何
                    # 掉进听音兜底只能重新去平台上抽样取证。每项含 block_id / missing_pages
                    # / reason / details（逐页原因）。
                    "missing": [
                        项 for 项 in (subtitle_state.get("missing") or [])
                        if int((项 or {}).get("block_id") or 0)
                        not in set(subtitle_state.get("cached") or [])
                        | set(subtitle_state.get("subtitle_ready") or [])
                    ],
                    # 因已有听音稿而拒绝用字幕稿覆盖的块（`_print_kept_audio` 会指到这里）
                    "kept_audio": list(subtitle_state.get("kept_audio") or []),
                    # 逐集来源：块号 → {集号: "subtitle"|"missing"}，供排查「哪一集走的哪条路」
                    "page_sources": {
                        int(k): {int(p): s for p, s in (v or {}).items()}
                        for k, v in (subtitle_state.get("page_sources") or {}).items()
                    },
                },
                "materialization": materialization,
            },
        })
        ws.save_manifest(manifest)
        return manifest

    @staticmethod
    def _mark_audio_purged(ws: Any, purge_result: Mapping[str, Any]) -> Dict[str, Any]:
        """把回收结果写进 manifest，并把 status 显式标成 purged。

        不这么做的话，`_save_runtime_manifest` 下一次会把 status 算成
        ``partial``（音频已不在 → audio_ready 少一块），看起来像"音频物化
        出了问题"，而它恰恰是成功回收的标志。
        """
        try:
            manifest = ws.load_manifest(absolute=True)
        except Exception:
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        pipeline_state = manifest.get("pipeline")
        if not isinstance(pipeline_state, dict):
            pipeline_state = {}
        materialization = pipeline_state.get("materialization")
        if not isinstance(materialization, dict):
            materialization = {}
        materialization["purged"] = True
        materialization["ready"] = []
        pipeline_state["status"] = "purged"
        pipeline_state["materialization"] = materialization
        manifest["pipeline"] = pipeline_state
        manifest["audio_purge"] = AudioMaterializer.purge_record(purge_result)
        ws.save_manifest(manifest)
        return manifest

    @classmethod
    def _purge_audio_if_complete(
        cls,
        ws: Any,
        blocks: Sequence[Mapping[str, Any]],
        *,
        keep_audio: bool = False,
        dry_run: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """收尾回收物理音频；豁免、未完工或出错时返回 ``None``（不打扰主流程）。

        判定与删除细节见 :meth:`AudioMaterializer.purge_audio`——四条件全过才删。
        这里只负责**编排**：打印人话、记账、兜住异常。回收是为了省磁盘，不是
        交付物，所以它失败绝不能掀翻流水线。
        """
        if keep_audio:
            print("[*] 已指定 --keep-audio：保留音频产物")
            return None
        if not AudioMaterializer.audio_exists(ws):
            return None
        try:
            结论 = AudioMaterializer.purge_audio(ws, blocks, dry_run=dry_run)
        except Exception as err:
            print(f"[!] 音频回收已跳过（不影响交付）：{err}", file=sys.stderr)
            return None

        if not 结论.get("purged"):
            print(f"[*] 音频未回收：{结论.get('reason') or '未满足回收条件'}")
            return 结论

        if dry_run:
            print(f"[*] [预演] 将回收 {结论['deleted']} 个音频文件，释放 "
                  f"{结论['bytes'] / 1048576:.1f} MB（加 --keep-audio 可豁免）")
            return 结论

        if 结论.get("failed"):
            print(f"[!] 音频回收部分失败 {len(结论['failed'])} 个文件，详见 manifest",
                  file=sys.stderr)
        else:
            print(f"[*] 已回收 {结论['deleted']} 个音频文件，释放 "
                  f"{结论['bytes'] / 1048576:.1f} MB（逐字稿不受影响）")
        cls._mark_audio_purged(ws, 结论)
        return 结论

    def run(
        self,
        url: str,
        sessdata: Optional[str] = None,
        task: Optional[str] = None,
        base_dir: Optional[Any] = None,
        page: Optional[int] = None,
        range_str: Optional[str] = None,
        process_all: bool = False,
        force: bool = False,
        allow_downgrade: bool = False,
        quality: str = "low",
        article_type: str = "",
        block_minutes: float = 0.0,
        mode: str = "full",
        keep_audio: bool = False,
    ) -> Dict[str, Any]:
        """按 v4 计划优先顺序执行完整流水线。

        ``dry-run`` 在解析拓扑后立即返回，不保存 parts、计划、manifest 或任务书。
        ``audio-only`` 完成计划、字幕、按需音频物化和转录任务书后返回，不派发模块
        长文任务书。

        ``--force`` 只影响物理音频、逐字稿与任务书的重建（跳过"已存在即复用"的缓存短路），
        **它不参与计划更新**。注意别把这句话读成"计划不会变"：`BlockPlan.ensure` 是**每次调用
        都执行**的（不受 force 控制），已锁定的旧块原样保留、只为新分集追加新块；手工裁短
        `source.parts` 会被 `ensure` 的 digest 自校验拒绝（见 `BlockPlan._assert_source_intact`）。
        要排除某些分集，正确做法是**保留 source.parts 全长、只裁 blocks**。

        ``--keep-audio`` 豁免收尾回收：缺省在**长文/笔记/教材全部产出后**自动删除
        物理音频（分集源音频 + 块级拼接 + 劈腿切片，145 集的课约 476 MB），因为
        逐字稿正文不引用音频路径。判定为四条件全过才删，缺任一条只跳过并说明。
        """
        # 1) 元数据解析与工作区绑定在任何磁盘状态变更之前完成。
        info = resolve_target_info(url, sessdata=sessdata, custom_task=task, base_dir=base_dir)
        bvid = str(info.get("bvid") or "")
        ws = TaskWorkspace.create(
            title=str(info.get("title") or "未命名课程"),
            bvid=bvid,
            custom_name=task,
            base_dir=base_dir,
            info_name=info.get("workspace_name"),
        )
        print("=" * 65)
        print(f"[*] v4 计划优先流水线启动 (Task Workspace: {ws.root_dir.name})")
        print("=" * 65)

        # 2) 只决定本次拓扑选择；完整 parts 会在计划建立后落盘。
        info_parts = [p for p in (info.get("parts") or []) if isinstance(p, dict)]
        if process_all or range_str:
            if range_str:
                indices = parse_range_string(range_str, len(info_parts))
                selected_parts = [info_parts[i - 1] for i in indices if 0 < i <= len(info_parts)]
            else:
                selected_parts = list(info_parts)
        elif info.get("has_multi_pages"):
            req_page = page if page is not None else (info.get("url_page") or 1)
            try:
                p_idx = max(1, min(int(req_page), len(info_parts)))
            except (TypeError, ValueError):
                p_idx = 1
            selected_parts = [info_parts[p_idx - 1]] if info_parts else []
        else:
            selected_parts = [{
                "page": 1,
                "title": info.get("title", ""),
                "cid": info.get("cid"),
                "duration": info.get("duration"),
                "filepath": info.get("source_path", ""),
                "url": url,
                "media_kind": KIND_VIDEO,
            }]
        selected_parts = self._clean_parts(selected_parts)
        if not selected_parts:
            raise PipelineGateError(2, "没有可处理的分集；请检查链接或本地课程目录后重跑 pipeline")
        print(f"[*] 本次选中分集: {len(selected_parts)}；拓扑缓存将保留完整 parts.json")

        # 3) dry-run 必须在这里结束；不得触发任何计划、音频或任务书写入。
        if mode == "dry-run":
            print("[*] --dry-run：仅解析拓扑，不保存 parts/block_plan，不下载或写任务书")
            for item in selected_parts:
                duration = item.get("duration")
                duration_text = (
                    f"{max(1, int(round(float(duration) / 60)))} 分钟"
                    if isinstance(duration, (int, float)) and duration else "时长未知"
                )
                print(f"    - P{int(item.get('page') or 0):02d} {item.get('title')}（{duration_text}）")
            print(f"[✓] 工作区: {ws.root_dir}")
            try:
                existing_plan = BlockPlan.load(ws) or {}
            except Exception:
                existing_plan = {}
            return {
                "workspace": ws,
                "manifest": {},
                "plan": existing_plan,
                "blocks": [dict(b) for b in (existing_plan.get("blocks") or []) if isinstance(b, Mapping)],
                "note_plan": [],
                "note_results": [],
                "failed_entries": [],
                "dry_run": True,
            }

        # 4) 完整拓扑先在内存合并。已有计划走 BlockPlan 的安全追加；新工作区必须
        # 先 ensure 再保存 parts.json，否则 BlockPlan 会把刚创建的分集缓存误判为 legacy。
        cached_parts = self._clean_parts(ws.load_parts())
        incoming_parts = self._clean_parts(selected_parts if range_str else (info_parts or selected_parts))
        complete_parts = self._clean_parts(
            TaskWorkspace.merge_parts(cached_parts, incoming_parts)
        )
        if not complete_parts:
            raise PipelineGateError(2, "完整 parts 为空；请检查媒体元数据后重跑 pipeline")

        limits = float(block_minutes) if block_minutes and float(block_minutes) > 0 else None
        plan = BlockPlan.ensure(ws, complete_parts, limits=limits)
        if not isinstance(plan, Mapping):
            plan = BlockPlan.load(ws) or {}
        # ensure 成功后才写分集缓存；连载追加因此始终看到完整、同一份拓扑。
        ws.save_parts(complete_parts)
        all_blocks = [dict(b) for b in (plan.get("blocks") or []) if isinstance(b, Mapping)]
        if not all_blocks:
            raise PipelineGateError(2, "BlockPlan 没有生成任何块；请检查分集时长/块时长参数后重跑")

        selected_pages = {
            int(part["page"]) for part in selected_parts
            if part.get("page") is not None
        }
        blocks, partial_blocks = self._blocks_in_scope(all_blocks, selected_pages)
        if partial_blocks:
            names = ", ".join(
                f"BLK{int(b.get('block_id') or 0):02d}({BlockPlan.span(b)})"
                for b in partial_blocks
            )
            print(f"[*] 跳过 {len(partial_blocks)} 个跨出本次选中范围的块：{names}")
        if not blocks:
            raise PipelineGateError(
                2,
                "本次选中的分集没有完整覆盖任何块；请扩大 --page/--range 或先建立完整 BlockPlan",
            )

        parts_by_page = self._parts_by_page(complete_parts)
        video_pages = {
            page_no for page_no, part in parts_by_page.items()
            if part_kind(part) == KIND_VIDEO
        }
        video_blocks = [
            block for block in blocks
            if self._block_is_video(block, video_pages)
        ]
        if len(video_blocks) != len(blocks):
            print(f"[*] 跳过 {len(blocks) - len(video_blocks)} 个非视频块（图文作品无口播）")

        # 5) 字幕优先：只有 B 站且确有 SESSDATA 才访问字幕接口；没有登录态时
        # 直接把所有视频块交给音频兜底，不把字幕-only 分支做成硬门禁。
        subtitle_state: Dict[str, Any] = {
            "subtitle_ready": [],
            "cached": [],
            "needs_audio": [int(b.get("block_id") or 0) for b in video_blocks],
            "written": [],
            "errors": [],
        }
        subtitle_plan = dict(plan)
        subtitle_plan["blocks"] = blocks
        if video_blocks and self._is_bilibili(info) and sessdata:
            try:
                raw_state = SubtitleService.run(
                    ws, info, subtitle_plan, sessdata=sessdata, force=force,
                    allow_downgrade=allow_downgrade,
                )
            except Exception as err:
                print(f"[!] 字幕阶段异常，缺字幕块将转音频兜底：{err}", file=sys.stderr)
                subtitle_state["errors"].append({"message": str(err)})
            else:
                subtitle_state = self._subtitle_state(raw_state, video_blocks, ws)
        elif video_blocks and self._is_bilibili(info):
            print("[*] B 站未配置 SESSDATA：跳过字幕，所有视频块进入音频物化")
        elif video_blocks:
            print("[*] 非 B 站来源：跳过字幕，所有视频块进入音频物化")

        # 6) 只物化字幕服务标记为 needs_audio 的块；物化器内部复用既有源音频，
        # 回调最终只通过 IngestionCoordinator.fetch_episode_audio 获取缺失分集。
        needs_ids = set(self._block_ids(subtitle_state.get("needs_audio")))
        if not force:
            needs_ids = {
                block_id for block_id in needs_ids
                if not self._transcript_exists(
                    ws, next(
                        (block for block in video_blocks
                         if int(block.get("block_id") or 0) == block_id),
                        {},
                    )
                )
            }
        subtitle_state["needs_audio"] = sorted(needs_ids)
        blocks_to_materialize = [
            block for block in video_blocks
            if int(block.get("block_id") or 0) in needs_ids
        ]
        materialized: Dict[int, Any] = {}
        if blocks_to_materialize:
            fetch_episode = self._fetch_callback(
                info, sessdata=sessdata, quality=quality, force=force
            )
            try:
                materialized = AudioMaterializer.materialize(
                    ws,
                    info,
                    blocks_to_materialize,
                    parts_by_page,
                    fetch_episode,
                    force=force,
                )
            except Exception as err:
                print("\n" + "=" * 65, file=sys.stderr)
                print(f"[✗] 块音频物化失败：{err}", file=sys.stderr)
                print("去向：确认 ffmpeg/ffprobe 在 PATH、audio/ 可写，并检查失败分集源文件后重跑 pipeline。", file=sys.stderr)
                print("=" * 65, file=sys.stderr)
                raise PipelineGateError(3, str(err)) from err
        if not isinstance(materialized, Mapping):
            materialized = {}

        audio_ready_ids = {
            int(block.get("block_id") or 0)
            for block in video_blocks
            if BlockPlan.audio_ready(ws, block)
        }
        runtime_materialized = {
            block_id: BlockPlan.audio_path(ws, block)
            for block_id in audio_ready_ids
            for block in video_blocks
            if int(block.get("block_id") or 0) == block_id
        }
        if audio_ready_ids != needs_ids:
            missing_audio = sorted(needs_ids - audio_ready_ids)
            if missing_audio:
                print(
                    f"[!] 暂有 {len(missing_audio)} 个缺字幕块未达到 audio_ready；"
                    "不会导出无效转录任务书，请排查对应源音频后重跑 pipeline。",
                    file=sys.stderr,
                )

        # 7) 转录任务书只给「缺字幕/无逐字稿 + 音频已就绪」的块；--force 只允许
        # 覆盖已有逐字稿对应的任务书，不触碰计划边界。
        titles = {
            page_no: sanitize_filename(str(part.get("title") or f"P{page_no:02d}"))
            for page_no, part in parts_by_page.items()
        }
        course_title = str(info.get("title") or "").strip() or ws.root_dir.name
        transcribe_blocks = [
            block for block in video_blocks
            if int(block.get("block_id") or 0) in needs_ids
            and int(block.get("block_id") or 0) in audio_ready_ids
            and (force or not self._transcript_exists(ws, block))
        ]
        missing_by_block: Dict[int, List[int]] = {}
        for entry in (subtitle_state.get("missing") or []):
            try:
                missing_by_block[int(entry.get("block_id") or 0)] = [
                    int(p) for p in (entry.get("missing_pages") or [])]
            except (TypeError, ValueError):
                continue
        for block in transcribe_blocks:
            block_id = int(block.get("block_id") or 0)
            # 按集兜底：只把**缺的**那几集写进任务书，音频给分集音频、目标给补录稿。
            # 缺页清单来自刚跑完的字幕阶段；若为空（老工作区/无部分稿）则退回整块任务书。
            待补 = [p for p in missing_by_block.get(block_id, []) if not self._page_transcript_exists(ws, block, p)]
            try:
                if 待补:
                    task_file = export_page_transcribe_task(
                        ws, block, 待补, course_title=course_title
                    )
                    print(f"    [agent] BLK{block_id:02d} {BlockPlan.span(block)} "
                          f"按集补录任务书（缺 {'、'.join(f'P{p:02d}' for p in 待补)}）: {task_file.name}")
                    continue
                task_file = export_block_transcribe_task(
                    ws, block, titles=titles, course_title=course_title
                )
            except Exception as err:
                print(f"[✗] BLK{block_id:02d} 转录任务书导出失败：{err}", file=sys.stderr)
                print("去向：检查 subtitles/ 写权限与磁盘空间后重跑 pipeline。", file=sys.stderr)
                raise PipelineGateError(3, str(err)) from err
            print(f"    [agent] BLK{block_id:02d} {BlockPlan.span(block)} 转录任务书: {task_file.name}")
        if not transcribe_blocks:
            print("[*] 当前没有需要补转录的块（字幕/逐字稿已就绪，或音频尚未就绪）")

        # 7.5) 合并：补录稿齐了的块，把「字幕部分稿 + 补录稿」合成最终块级逐字稿。
        # 合并前**不写最终名**——最终名一出现，`_transcript_exists` / queue_tracker 就会
        # 认定该块已转录完成，所以这一步只在真的凑齐时才落。
        merged = self._merge_completed_blocks(ws, video_blocks, missing_by_block)
        for 项 in merged:
            print(f"    [merge] BLK{int(项.get('block_id') or 0):02d} "
                  f"合并完成（听音补录 {'、'.join(f'P{p:02d}' for p in (项.get('audio_pages') or []))}）"
                  f" -> {Path(str(项.get('path'))).name}")
        if missing_by_block and not merged:
            print("[*] 补录稿尚未齐备，最终块级逐字稿暂不生成")

        runtime_manifest = self._save_runtime_manifest(
            ws,
            plan,
            subtitle_state,
            runtime_materialized,
            video_blocks,
            mode=mode,
        )

        # 8) --audio-only 到此结束：任务书只描述转录输入，不提前派发写作。
        if mode == "audio-only":
            print("[*] --audio-only：计划、字幕、按需音频物化与转录任务书已完成")
            print(f"[✓] BlockPlan: {BlockPlan.path(ws)}")
            print("[i] 下一步：转录角色读取 subtitles/ 任务书，完成后再运行 pipeline 派发模块长文。")
            print("=" * 65)
            return {
                "workspace": ws,
                "manifest": runtime_manifest,
                "plan": dict(plan),
                "blocks": blocks,
                "audio": runtime_materialized,
                "subtitle": subtitle_state,
                "note_plan": [],
                "note_results": [],
                "failed_entries": [],
                "audio_only": True,
            }

        # 9) 所有视频块都导出模块长文任务书；任务书不写动态 ready 状态，写作角色
        # 运行时再按逐字稿文件实际存在性判断。纯图文课程没有可写块，也不触发风格门禁。
        resolved_type: Optional[Dict[str, str]] = None
        if video_blocks:
            try:
                resolved_type = resolve_article_type(article_type)
            except ArticlePromptTypeError as err:
                print("\n" + err.report, file=sys.stderr)
                print("去向：先确认 learning/legacy 长文风格，再用 --article-type 重跑 pipeline。", file=sys.stderr)
                raise PipelineGateError(4, f"长文提示词风格门禁终止：{err.reason}") from err

        page_titles = dict(titles)
        for index, block in enumerate(video_blocks, 1):
            block_id = int(block.get("block_id") or 0)
            span = BlockPlan.span(block)
            transcript_path = TaskWorkspace.block_path(ws, block)
            try:
                task_file = export_block_article_task(
                    ws,
                    block,
                    transcript_path,
                    course_title=course_title,
                    article_type=article_type,
                    page_titles=page_titles,
                )
            except Exception as err:
                print(f"[✗] BLK{block_id:02d} 模块长文任务书导出失败：{err}", file=sys.stderr)
                print("去向：检查 articles/ 写权限与磁盘空间后重跑 pipeline；BlockPlan 与已完成音频会保留。", file=sys.stderr)
                raise PipelineGateError(3, str(err)) from err
            print(
                f"    [agent] BLK{block_id:02d} {span} 模块长文任务书 "
                f"({index}/{len(video_blocks)}): {task_file.name}"
            )
        if video_blocks:
            print(f"[*] 模块长文任务书已导出 {len(video_blocks)} 份，风格: {resolved_type['key']}")
        else:
            print("[*] 本课程没有视频块，跳过模块长文任务书")

        # 10) 阶段三笔记规划逻辑保持原有语义：只有已有模块长文时才归并。
        note_plan: List[Dict[str, Any]] = []
        note_results: List[Dict[str, Any]] = []
        note_dispatched = False
        if process_all:
            scope_parts = [
                part for part in resolve_scope_parts(info, ws)
                if part_kind(part) == KIND_VIDEO
            ]
            from src.core.workspace import find_module_article

            ready_blocks = [
                block for block in video_blocks
                if find_module_article(ws.articles_dir, block) is not None
            ]
            if not ready_blocks:
                print("\n" + "=" * 65)
                print("[*] 阶段三后置：模块长文任务书已就绪，但尚无任何模块长文落盘。")
                print(f"[*] 待宿主 Agent 将长文写入 {ws.articles_dir} 后，重跑 pipeline --all 自动归并。")
                print("=" * 65)
            else:
                print("\n" + "=" * 65)
                print(f"[*] 阶段三：笔记归并（{len(ready_blocks)}/{len(video_blocks)} 块已有模块长文）")
                print("=" * 65)
                outcome = BlockSynthesizer.dispatch_notes(
                    ws,
                    scope_parts,
                    course_title=resolve_course_title(info, ws),
                    force=force,
                )
                note_plan = outcome["notes"]
                note_results = outcome["results"]
                note_dispatched = outcome["note_status"] in ("planned", "salvaged") and bool(note_plan)
                if outcome["note_status"] == "unmerged":
                    print("[*] 阶段三后置：已导出归并任务书，待宿主 Agent 产出 note_plan.json 后重跑。")
        elif info.get("has_multi_pages"):
            print("[*] 分区间运行：归并后置，待 --all 全量语料齐后统一派发")

        # 11) 收尾只做任务书回收与磁盘对账；不把块定义复制到 pipeline manifest。
        try:
            from src.core.task_cleanup import cleanup_completed_tasks as cleanup_tasks

            reclaim = cleanup_tasks(ws, keep_per_category=1)
            if reclaim["deleted"]:
                print(f"[*] 已回收 {len(reclaim['deleted'])} 份已完成任务书（每类保留 1 份范本）")
        except Exception as err:
            print(f"[!] 任务书回收已跳过：{err}", file=sys.stderr)

        try:
            from src.core.state_sync import reconcile_workspace_manifest as reconcile_manifest

            sync = reconcile_manifest(ws)
            print(
                f"[*] 账本对账：分集 {sync['success']}/{sync['total']} 集达标 | "
                f"待办 {sync['pending']} | 模块笔记 {sync['notes']} 份 | "
                f"教材 {sync['textbooks']} 部 | pipeline_completed={sync['pipeline_completed']}"
            )
        except Exception as err:
            print(f"[!] 账本对账已跳过：{err}", file=sys.stderr)

        # 12) 全部交付物齐备后回收音频：它是唯一大体积的中间产物（145 集的课
        # 约 476 MB），而逐字稿正文不引用音频路径。放在对账之后，所以账本先按
        # 「音频在盘」算完，再回收——`--keep-audio` 可豁免。
        self._purge_audio_if_complete(ws, video_blocks, keep_audio=keep_audio)

        final_manifest = ws.load_manifest(absolute=True)
        return {
            "workspace": ws,
            "manifest": final_manifest,
            "plan": dict(plan),
            "blocks": blocks,
            "audio": runtime_materialized,
            "subtitle": subtitle_state,
            "note_plan": note_plan,
            "note_results": note_results,
            "failed_entries": list(final_manifest.get("failed_episodes") or []),
        }


def _读部分稿片段(ws: Any, block: Mapping[str, Any], state_path: Any) -> Dict[str, str]:
    """从已落盘的**字幕部分稿**还原「按段的正文」，供合并时按原顺序拼回去。

    部分稿的行首是块内相对时间戳 `[MM:SS] 文本`，段与段在块内时间轴上首尾相接，
    所以把每一行按时间戳归到它所属的段即可复原，不必在 sidecar 里再存一份正文。
    读不出来时返回空表——合并层会因此拿不到字幕段，此时宁可不合，也不要产出缺段的稿。
    """
    from src.core.workspace import TaskWorkspace as _TW

    try:
        文本 = _TW.partial_transcript_path(ws, dict(block)).read_text(encoding="utf-8", errors="ignore")
    except (OSError, TypeError, ValueError):
        return {}
    段表 = []
    for 段 in (block.get("segments") or []):
        if not isinstance(段, dict):
            continue
        段表.append((
            int(段.get("page") or 0),
            str(段.get("label") or f"P{int(段.get('page') or 0):02d}"),
            float(段.get("start_sec") or 0.0),
            float(段.get("duration_sec") or 0.0),
        ))
    if not 段表:
        return {}
    分段: Dict[str, List[str]] = {label: [] for _, label, _, _ in 段表}
    正文起 = False
    for 行 in 文本.split("\n"):
        if not 正文起:
            # 抬头是首个空行之前的内容；正文从空行之后开始
            正文起 = 行.strip() == ""
            continue
        if not 行.strip():
            continue
        时刻 = _时刻秒(行)
        if 时刻 is None:
            continue
        for page, label, 起, 长 in 段表:
            if 起 - 0.5 <= 时刻 < 起 + 长:
                分段[label].append(行)
                break
    return {label: "\n".join(行) for label, 行 in 分段.items() if 行}


def _时刻秒(行: str) -> Optional[float]:
    """把行首 `[MM:SS] ` / `[HH:MM:SS] ` 解析成秒；不是时间戳行返回 None。"""
    import re as _re

    m = _re.match(r"^\s*\[(\d{1,3}):(\d{2})(?::(\d{2}))?\]", 行)
    if not m:
        return None
    if m.group(3) is not None:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return int(m.group(1)) * 60 + int(m.group(2))
