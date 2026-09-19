#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YouTube media provider using in-process ytaudio engine."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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

    def _get_engine_and_cfg(self, **kwargs: Any) -> Tuple[Any, Any]:
        from .ytaudio.config import load_config
        from .ytaudio.engine import Engine

        cfg_path = kwargs.get("config_path")
        cfg = load_config(cfg_path)
        if "output_dir" in kwargs and kwargs["output_dir"]:
            cfg.output_dir = str(kwargs["output_dir"])
        if "proxy" in kwargs and kwargs["proxy"]:
            cfg.proxy = str(kwargs["proxy"])
        if "cookiefile" in kwargs and kwargs["cookiefile"]:
            cfg.cookiefile = str(kwargs["cookiefile"])
        if "cookies_from_browser" in kwargs and kwargs["cookies_from_browser"]:
            cfg.cookies_from_browser = str(kwargs["cookies_from_browser"])
        return Engine(cfg), cfg

    def probe(self, target: str, **kwargs: Any) -> Dict[str, Any]:
        from .ytaudio.channel import canonical_url, collect_channel
        from .ytaudio.single import resolve_video
        from .ytaudio.utils import is_video_url, normalize_channel_base, sanitize_filename, slugify

        engine, cfg = self._get_engine_and_cfg(**kwargs)
        clean_target = target.strip()

        # 1. 单视频探测
        if is_video_url(clean_target) and "/shorts/" not in clean_target:
            info = resolve_video(engine, cfg, clean_target)
            if not info:
                raise IngestionError(f"YouTube 视频解析失败或已被过滤: {clean_target}")

            video_id = str(info.get("id") or "")
            title = str(info.get("title") or video_id)
            duration = int(info.get("duration") or 0)
            uploader = str(info.get("uploader") or info.get("channel") or "YouTube")
            desc = str(info.get("description") or "")[:500]

            # 标识符口径与其它来源一致：有原生 id 用 id，缺 id 则退回清洗后的标题
            # （yt-dlp 正常都带 id；缺 id 时旧写法退化成 "yt_"，工作区名会与别门课撞车）
            ident = video_id or sanitize_filename(title, max_len=60)[:20]

            parts = [{
                "page": 1,
                "title": title,
                "cid": f"yt_{ident}",
                "duration": duration,
                "url": canonical_url(video_id),
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

        # 2. 频道 / 播放列表（合集）探测
        base = normalize_channel_base(clean_target)
        if not base:
            # 尝试直接作为播放列表或者合集
            base = clean_target

        limit = kwargs.get("limit") or cfg.limit
        try:
            channel_name, groups = collect_channel(engine, base, cfg, limit=limit)
        except Exception as err:
            raise IngestionError(f"YouTube 频道/播放列表抓取失败: {err}") from err

        parts: List[Dict[str, Any]] = []
        page_idx = 1
        total_duration = 0

        for group_name, items in groups.items():
            for item in items:
                v_id = str(item.get("id") or "")
                if not v_id:
                    continue
                v_title = str(item.get("title") or v_id)
                dur = int(item.get("duration") or 0)
                total_duration += dur

                display_title = f"[{group_name}] {v_title}" if group_name != "未分类" and len(groups) > 1 else v_title
                parts.append({
                    "page": page_idx,
                    "title": display_title,
                    "cid": f"yt_{v_id}",
                    "duration": dur,
                    "url": canonical_url(v_id),
                    "filepath": "",
                })
                page_idx += 1

        if not parts:
            raise IngestionError(f"未从 YouTube 目标中提取到有效普通视频: {clean_target}")

        slug = slugify(channel_name)
        bvid = f"yt_{slug[:20]}" if slug else f"yt_ch_{page_idx}"

        return {
            "bvid": bvid,
            "title": channel_name,
            "desc": f"YouTube 频道/合集: {channel_name}",
            "duration": total_duration,
            "owner": {"name": channel_name, "mid": 0},
            "video_type": "multi_page" if len(parts) > 1 else "single",
            "type_desc": f"YouTube 课程合集 (共 {len(parts)} P)",
            "cid": parts[0]["cid"],
            "has_multi_pages": len(parts) > 1,
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
        url = episode.get("url")
        if not url:
            cid = str(episode.get("cid") or "")
            if cid.startswith("yt_"):
                v_id = cid[3:]
                from .ytaudio.channel import canonical_url
                url = canonical_url(v_id)
            else:
                raise IngestionError(f"缺少 YouTube 视频链接: {episode.get('title')}")

        output_file = Path(output_file).resolve()
        output_file.parent.mkdir(parents=True, exist_ok=True)

        if output_file.exists() and output_file.stat().st_size >= 10240 and not force:
            if progress_cb:
                progress_cb({"status": "cached", "file": str(output_file)})
            return output_file

        engine, cfg = self._get_engine_and_cfg(**kwargs)

        # yt-dlp outtmpl: 去掉后缀，后置 %(ext)s
        base_tmpl = str(output_file.with_suffix(""))
        outtmpl = base_tmpl + ".%(ext)s"

        import yt_dlp
        opts = engine.download_opts(outtmpl=outtmpl)
        opts["quiet"] = True
        opts["noprogress"] = True

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as err:
            raise IngestionError(f"YouTube 音频下载失败 ({url}): {err}") from err

        # 检查生成的文件
        if output_file.exists() and output_file.stat().st_size > 0:
            if progress_cb:
                progress_cb({"status": "downloaded", "file": str(output_file)})
            return output_file

        # 若生成了其他扩展名（如 .webm, .opus），转为目标 .m4a
        candidates = list(output_file.parent.glob(output_file.stem + ".*"))
        for cand in candidates:
            if cand.suffix.lower() == ".part":
                continue
            if cand.exists() and cand.stat().st_size > 1024:
                # 转封装或重命名为 output_file
                if cand != output_file:
                    shutil.move(str(cand), str(output_file))
                if progress_cb:
                    progress_cb({"status": "downloaded", "file": str(output_file)})
                return output_file

        raise IngestionError(f"YouTube 音频提取未产生有效文件: {output_file}")

    def check_readiness(self) -> Tuple[bool, str]:
        try:
            import yt_dlp
            yt_ver = getattr(getattr(yt_dlp, "version", None), "__version__", "已安装")
        except ImportError:
            return False, "缺少 yt-dlp 依赖 (pip install yt-dlp)"

        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return False, "缺少 ffmpeg，无法进行音频提取与转码"

        return True, f"就绪 (yt-dlp {yt_ver}, ffmpeg {ffmpeg_bin})"
