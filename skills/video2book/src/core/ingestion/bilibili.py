#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bilibili media provider."""

from __future__ import annotations

import shutil
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from src.core.credentials import resolve_sessdata
from src.core.fetcher import AudioFetcher
from src.core.parser import BilibiliParser
from .base import BaseMediaProvider, IngestionError


class BilibiliProvider(BaseMediaProvider):
    name = "bilibili"
    display_name = "哔哩哔哩 (Bilibili)"

    def match(self, target: str) -> bool:
        if not target or not isinstance(target, str):
            return False
        # 排除包含其他知名域名的 URL
        low = target.lower().strip()
        if "youtube.com" in low or "youtu.be" in low or "douyin.com" in low:
            return False
        if BilibiliParser.extract_bvid(target):
            return True
        if not BilibiliParser.extract_season_ref(target):
            return False
        if low.startswith("season:"):
            return True
        host = (urllib.parse.urlparse(low).hostname or "").lower()
        return host == "bilibili.com" or host.endswith(".bilibili.com") or host == "b23.tv"

    def probe(self, target: str, **kwargs: Any) -> Dict[str, Any]:
        sessdata = kwargs.get("sessdata") or resolve_sessdata()
        info = BilibiliParser.parse_video(target, sessdata=sessdata)
        info["source_type"] = "bilibili"
        info["is_local"] = False
        return info

    def fetch_audio(
        self,
        episode: Dict[str, Any],
        output_file: Path,
        *,
        force: bool = False,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        **kwargs: Any,
    ) -> Path:
        # 合集课程里每一集有自己的 BV 号；顶层 info["bvid"] 只是稳定入口键，
        # 不能覆盖 episode 自身的 BV 号，否则 P02 以后会全部下载成第一集。
        bvid = episode.get("bvid") or kwargs.get("bvid")
        cid = episode.get("cid")
        if not bvid or not cid:
            raise IngestionError(f"B站分集缺少 bvid 或 cid: {episode.get('title')}")

        sessdata = kwargs.get("sessdata") or resolve_sessdata()
        quality = kwargs.get("quality", "low")

        output_file.parent.mkdir(parents=True, exist_ok=True)
        stream_info = AudioFetcher.get_audio_stream_info(
            str(bvid), cid, sessdata=sessdata, prefer_quality=quality
        )
        stream_url = stream_info.get("best_stream_url")
        if not stream_url:
            raise IngestionError(f"未能获取到音频直链: bvid={bvid}, cid={cid}")

        AudioFetcher.download_audio(stream_url, str(output_file), repackage_m4a=True)
        if progress_cb:
            progress_cb({"status": "downloaded", "file": str(output_file)})
        return output_file

    def check_readiness(self) -> Tuple[bool, str]:
        ffmpeg_bin = shutil.which("ffmpeg")
        sessdata = resolve_sessdata()
        status = "就绪"
        if not ffmpeg_bin:
            return False, "缺少 ffmpeg，无法转封装与提取音频"
        if sessdata:
            status += " (已配置 SESSDATA 凭证)"
        else:
            status += " (未配置 SESSDATA，可匿名抓取或按需 login)"
        return True, status
