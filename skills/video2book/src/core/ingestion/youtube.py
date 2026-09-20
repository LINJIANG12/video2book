#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YouTube media provider using lightweight direct yt-dlp integration."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core.proc import run_quiet
from .base import BaseMediaProvider, IngestionError


class YouTubeProvider(BaseMediaProvider):
    name = "youtube"
    display_name = "YouTube (油管)"

    _YT_DOMAINS = ("youtube.com", "youtu.be", "m.youtube.com")

    def match(self, target: str) -> bool:
        if not target or not isinstance(target, str):
            return False
        low = target.lower().strip()
        return any(domain in low for domain in self._YT_DOMAINS)

    def _get_ydl_opts(self, **kwargs: Any) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "ignoreerrors": True,
            "retries": 3,
            "socket_timeout": 30,
        }
        if kwargs.get("proxy"):
            opts["proxy"] = str(kwargs["proxy"])
        if kwargs.get("cookiefile"):
            opts["cookiefile"] = str(kwargs["cookiefile"])
        if kwargs.get("cookies_from_browser"):
            opts["cookiesfrombrowser"] = (str(kwargs["cookies_from_browser"]),)
        return opts

    @staticmethod
    def _is_single_video(url: str) -> bool:
        low = url.lower().strip()
        if "youtu.be/" in low:
            return True
        if "/watch" in low and ("list=" not in low or "index=" not in low):
            return True
        return False

    @staticmethod
    def _sanitize(name: str, max_len: int = 60) -> str:
        s = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
        return s[:max_len] if len(s) > max_len else s

    def probe(self, target: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            import yt_dlp
        except ImportError as e:
            raise IngestionError("缺少 yt-dlp 依赖，请先安装：pip install yt-dlp") from e

        clean_target = target.strip()
        opts = self._get_ydl_opts(**kwargs)
        opts["extract_flat"] = True

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(clean_target, download=False)
        except Exception as err:
            raise IngestionError(f"YouTube 目标解析失败: {err}") from err

        if not info:
            raise IngestionError(f"YouTube 无法提取元数据: {clean_target}")

        entries = info.get("entries")
        if not entries:
            # 单视频
            vid = str(info.get("id") or "")
            title = str(info.get("title") or vid)
            duration = int(info.get("duration") or 0)
            uploader = str(info.get("uploader") or info.get("channel") or "YouTube")
            desc = str(info.get("description") or "")[:500]
            ident = vid or self._sanitize(title, 20)

            parts = [{
                "page": 1,
                "title": title,
                "cid": f"yt_{ident}",
                "duration": duration,
                "url": f"https://www.youtube.com/watch?v={vid}" if vid else clean_target,
                "filepath": "",
            }]

            return {
                "bvid": f"yt_{ident}",
                "title": title,
                "desc": desc,
                "duration": duration,
                "owner": {"name": uploader, "mid": 0},
                "video_type": "single",
                "type_desc": "YouTube 单视频",
                "cid": f"yt_{ident}",
                "has_multi_pages": False,
                "has_ugc_season": False,
                "season_episodes": [],
                "parts": parts,
                "is_local": False,
                "source_type": "youtube",
                "source_path": clean_target,
            }

        # 播放列表 / 频道
        playlist_title = str(info.get("title") or info.get("id") or "YouTube合集")
        channel = str(info.get("channel") or info.get("uploader") or "YouTube")
        pid = str(info.get("id") or self._sanitize(playlist_title, 20))
        limit = kwargs.get("limit") or 200

        parts: List[Dict[str, Any]] = []
        for idx, entry in enumerate(entries, 1):
            if not entry or not isinstance(entry, dict):
                continue
            if len(parts) >= limit:
                break
            v_id = str(entry.get("id") or "")
            v_title = str(entry.get("title") or f"P{idx:02d}")
            v_dur = int(entry.get("duration") or 0)
            v_url = f"https://www.youtube.com/watch?v={v_id}" if v_id else ""
            parts.append({
                "page": len(parts) + 1,
                "title": v_title,
                "cid": f"yt_{v_id or idx}",
                "duration": v_dur,
                "url": v_url,
                "filepath": "",
            })

        if not parts:
            raise IngestionError(f"YouTube 播放列表中未发现有效视频分集: {clean_target}")

        return {
            "bvid": f"yt_{pid}",
            "title": playlist_title,
            "desc": f"共 {len(parts)} 集",
            "duration": sum(p["duration"] for p in parts),
            "owner": {"name": channel, "mid": 0},
            "video_type": "multi_page",
            "type_desc": "YouTube 播放列表/频道",
            "cid": f"yt_{pid}",
            "has_multi_pages": True,
            "has_ugc_season": False,
            "season_episodes": [],
            "parts": parts,
            "is_local": False,
            "source_type": "youtube",
            "source_path": clean_target,
        }

    def fetch_audio(
        self,
        episode: Dict[str, Any],
        output_file: Path,
        *,
        force: bool = False,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        **kwargs: Any,
    ) -> Path:
        if output_file.exists() and output_file.stat().st_size > 10 * 1024 and not force:
            return output_file

        url = episode.get("url")
        if not url:
            raise IngestionError(f"缺少音频下载 URL: {episode.get('title')}")

        try:
            import yt_dlp
        except ImportError as e:
            raise IngestionError("缺少 yt-dlp 依赖") from e

        output_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = output_file.with_suffix(".tmp.m4a")

        opts = self._get_ydl_opts(**kwargs)
        opts.update({
            "format": "bestaudio/best",
            "outtmpl": str(output_file.parent / f"raw_{episode.get('page', 1)}.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }],
        })

        # Download directly or via ffmpeg transcode to 16kHz mono
        ffmpeg_bin = shutil.which("ffmpeg")
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(url, download=True)
            downloaded = Path(ydl.prepare_filename(res)).with_suffix(".m4a")

        if not downloaded.exists():
            # Try finding any downloaded file
            matches = list(output_file.parent.glob(f"raw_{episode.get('page', 1)}.*"))
            if matches:
                downloaded = matches[0]

        if not downloaded.exists():
            raise IngestionError(f"yt-dlp 音频提取失败: {url}")

        if ffmpeg_bin:
            cmd = [
                ffmpeg_bin, "-y", "-i", str(downloaded),
                "-vn", "-acodec", "aac", "-ar", "16000", "-ac", "1", "-b:a", "32k",
                str(tmp_target)
            ]
            r = run_quiet(cmd, timeout=300)
            if r.returncode == 0 and tmp_target.exists():
                try:
                    downloaded.unlink(missing_ok=True)
                except OSError:
                    pass
                tmp_target.replace(output_file)
                return output_file

        downloaded.replace(output_file)
        return output_file

    def check_readiness(self) -> Tuple[bool, str]:
        import importlib.util

        has_ffmpeg = shutil.which("ffmpeg") is not None
        has_ytdlp = importlib.util.find_spec("yt_dlp") is not None

        if has_ffmpeg and has_ytdlp:
            return True, "就绪 (ffmpeg + yt-dlp)"
        missing = []
        if not has_ffmpeg:
            missing.append("ffmpeg")
        if not has_ytdlp:
            missing.append("yt-dlp")
        return False, f"缺少依赖: {', '.join(missing)}"
