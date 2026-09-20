#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ingestion Coordinator: unified dispatcher for all media sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core import paths as _paths
from .base import BaseMediaProvider, IngestionError, UnsupportedTargetError
from .bilibili import BilibiliProvider
from .douyin import DouyinProvider
from .local import LocalMediaProvider
from .youtube import YouTubeProvider


class IngestionCoordinator:
    """Central coordinator for media detection, metadata probing, and audio fetching."""

    def __init__(self, providers: Optional[List[BaseMediaProvider]] = None):
        if providers is not None:
            self.providers = list(providers)
        else:
            self.providers = [
                LocalMediaProvider(),
                BilibiliProvider(),
                YouTubeProvider(),
                DouyinProvider(),
            ]

    def register_provider(self, provider: BaseMediaProvider, prepend: bool = False) -> None:
        """Register a custom or additional media provider."""
        if prepend:
            self.providers.insert(0, provider)
        else:
            self.providers.append(provider)

    def find_provider(self, target: str) -> Optional[BaseMediaProvider]:
        """Find the matching provider for the given target."""
        if not target:
            return None
        for p in self.providers:
            try:
                if p.match(target):
                    return p
            except Exception:
                continue
        return None

    def get_provider_by_name(self, name: str) -> Optional[BaseMediaProvider]:
        """Lookup provider by its unique identifier (e.g. 'bilibili', 'youtube', 'douyin', 'local')."""
        for p in self.providers:
            if p.name.lower() == name.lower():
                return p
        return None

    def resolve_target_info(
        self,
        target: str,
        *,
        sessdata: Optional[str] = None,
        custom_task: Optional[str] = None,
        base_dir: Optional[Any] = None,
        limit: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Multi-source target metadata resolution with fallback to offline candidate caches."""
        provider = self.find_provider(target)
        probe_err: Optional[Exception] = None

        if provider:
            try:
                return provider.probe(
                    target,
                    sessdata=sessdata,
                    custom_task=custom_task,
                    base_dir=base_dir,
                    limit=limit,
                    **kwargs,
                )
            except Exception as err:
                probe_err = err

        # 离线自愈与本地工作区缓存重试（支持 B 站等历史任务）
        from src.core.workspace import _offline_candidate_dirs, _parts_from_audio, _workspace_title
        from src.core.parser import BilibiliParser

        bvid = BilibiliParser.extract_bvid(target) or ""
        season_ref = BilibiliParser.extract_season_ref(target) or {}
        season_id = season_ref.get("season_id")
        out_base = _paths.resolve_base_dir(base_dir)

        if bvid or custom_task or season_id:
            cands = _offline_candidate_dirs(out_base, bvid, custom_task, season_id=season_id)
            for cd in cands:
                parts_file = cd / "parts.json"
                cached_parts = []
                m_data = {}
                manifest_f = cd / "manifest.json"
                if manifest_f.exists():
                    try:
                        m_data = json.loads(manifest_f.read_text(encoding="utf-8"))
                    except Exception:
                        m_data = {}

                if parts_file.exists() and parts_file.stat().st_size > 20:
                    try:
                        loaded = json.loads(parts_file.read_text(encoding="utf-8"))
                        if isinstance(loaded, list):
                            cached_parts = [p for p in loaded if isinstance(p, dict) and p.get("page") is not None]
                    except Exception:
                        cached_parts = []

                if not cached_parts:
                    cached_parts = _parts_from_audio(cd)

                if cached_parts:
                    print(f"\n[!] 在线元数据提取受阻（{probe_err}），已自动从本地缓存加载分集拓扑离线运行: {cd.name}")
                    return {
                        "video_type": "multi_page" if len(cached_parts) > 1 else "single",
                        "title": _workspace_title(cd, m_data),
                        "workspace_name": cd.name,
                        "bvid": bvid or cached_parts[0].get("bvid") or cd.name,
                        "owner": "",
                        "owner_mid": 0,
                        "desc": "",
                        "duration": sum(p.get("duration", 0) for p in cached_parts),
                        "pic": "",
                        "has_multi_pages": len(cached_parts) > 1,
                        "parts": cached_parts,
                        "cid": cached_parts[0].get("cid", 0),
                        "url_page": BilibiliParser.extract_page_index(target),
                        "has_ugc_season": False,
                        "season_episodes": [],
                        "type_desc": "离线自愈任务",
                        "is_local": False,
                        "source_type": m_data.get("source_type", "bilibili"),
                    }

        if probe_err:
            raise probe_err

        raise UnsupportedTargetError(
            f"未能识别的媒体目标（不是有效的本地音视频、B站链接、YouTube链接或抖音链接）: {target}"
        )

    def fetch_episode_audio(
        self,
        info: Dict[str, Any],
        episode: Dict[str, Any],
        output_file: Path,
        *,
        force: bool = False,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        **kwargs: Any,
    ) -> Path:
        """Route episode audio fetch to the appropriate provider."""
        source_type = info.get("source_type") or ("local" if info.get("is_local") else "bilibili")
        provider = self.get_provider_by_name(source_type)

        if not provider:
            # 尝试通过 episode url 重新识别
            url = episode.get("url") or episode.get("filepath") or info.get("source_path") or ""
            provider = self.find_provider(url)

        if not provider:
            raise IngestionError(f"未找到可处理 source_type={source_type} 的媒体提供商: {episode.get('title')}")

        return provider.fetch_audio(
            episode,
            output_file,
            force=force,
            progress_cb=progress_cb,
            bvid=info.get("bvid"),
            source_path=info.get("source_path"),
            **kwargs,
        )

    def readiness_report(self) -> Dict[str, Tuple[bool, str]]:
        """Return readiness status for all registered providers."""
        report = {}
        for p in self.providers:
            try:
                ok, msg = p.check_readiness()
                report[p.display_name] = (ok, msg)
            except Exception as err:
                report[p.display_name] = (False, f"自检异常: {err}")
        return report


_DEFAULT_COORDINATOR: Optional[IngestionCoordinator] = None


def get_coordinator() -> IngestionCoordinator:
    """Get the global default IngestionCoordinator instance."""
    global _DEFAULT_COORDINATOR
    if _DEFAULT_COORDINATOR is None:
        _DEFAULT_COORDINATOR = IngestionCoordinator()
    return _DEFAULT_COORDINATOR
