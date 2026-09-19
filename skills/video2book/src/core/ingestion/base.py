#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Base definitions and contract for media ingestion providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple


class IngestionError(RuntimeError):
    """Base exception for media ingestion failures."""
    pass


class UnsupportedTargetError(IngestionError):
    """Raised when a target cannot be handled by any registered provider."""
    pass


class BaseMediaProvider(ABC):
    """Abstract base class for all media providers (Bilibili, Local, YouTube, Douyin, etc.)."""

    name: str = "base"
    display_name: str = "Base Provider"

    @abstractmethod
    def match(self, target: str) -> bool:
        """Return True if this provider can handle the given target URL, path, or identifier."""
        raise NotImplementedError

    @abstractmethod
    def probe(self, target: str, **kwargs: Any) -> Dict[str, Any]:
        """Probe the target and return standard course/video metadata dictionary.

        Expected schema:
        {
            "video_type": "single" | "multi_page",
            "title": str,
            "desc": str,
            "duration": int,  # seconds
            "owner": {"name": str, "mid": int | str},
            "bvid": str,      # stable identifier used for workspace naming
            "type_desc": str,
            "cid": Any,
            "has_multi_pages": bool,
            "has_ugc_season": bool,
            "season_episodes": List[Any],
            "parts": [
                {
                    "page": int,
                    "title": str,
                    "cid": Any,
                    "duration": int,
                    "url": str,
                    "filepath": Optional[str],
                }
            ],
            "is_local": bool,
            "source_type": str,  # "bilibili" | "local" | "youtube" | "douyin"
            "source_path": Optional[str],
            "workspace_name": Optional[str],
        }
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_audio(
        self,
        episode: Dict[str, Any],
        output_file: Path,
        *,
        force: bool = False,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        **kwargs: Any,
    ) -> Path:
        """Download or transcode audio for the given episode into output_file (.m4a)."""
        raise NotImplementedError

    @abstractmethod
    def check_readiness(self) -> Tuple[bool, str]:
        """Check whether prerequisites for this provider are met in the current environment."""
        raise NotImplementedError
