#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Douyin media provider using in-process dyaudio engine."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import BaseMediaProvider, IngestionError

# 抖音分享短链：只有跟一次 302 才知道它指向单视频还是**博主主页**
_SHORT_LINK_RE = re.compile(r"https?://v\.douyin\.com/[\w\-]+/?", re.IGNORECASE)


def expand_share_link(session: Any, url: str, timeout: int = 15) -> str:
    """把 ``v.douyin.com`` 短链跟一次 302 展开为最终地址；非短链或展开失败时**原样返回**。

    为什么必需：短链既可能是单视频链，也可能是**博主主页**链（实测
    ``v.douyin.com/PcHMHverMrE/`` → ``/share/user/<sec_uid>``），而二者走完全不同的处理分支。
    没有这一步就只能靠字面量猜（短链里既没有 ``/user/`` 也没有 ``sec_uid``），
    主页短链必然被误判成单视频，随后 ``extract_aweme_id`` 跟跳后找不到数字 ID 而抛
    「未能从链接中解析出作品 ID」——用户看到的是「链接无效」，实际是链接有效但类型判错。
    """
    if not url or "v.douyin.com" not in url:
        return url
    try:
        from .dyaudio.config import MOBILE_UA

        resp = session.get(
            url, timeout=timeout, allow_redirects=True,
            headers={"User-Agent": MOBILE_UA},
        )
        final = str(getattr(resp, "url", "") or "")
        return final if final.startswith("http") else url
    except Exception:
        return url


class DouyinProvider(BaseMediaProvider):
    name = "douyin"
    display_name = "抖音 (Douyin)"

    def match(self, target: str) -> bool:
        if not target or not isinstance(target, str):
            return False
        low = target.lower()
        if "douyin.com" in low or "iesdouyin.com" in low:
            return True
        if "v.douyin.com" in low:
            return True
        # 兼容纯数字 aweme_id（18-19位数字）
        if target.strip().isdigit() and len(target.strip()) >= 15:
            return True
        return False

    def _get_client(self, **kwargs: Any) -> Any:
        from .dyaudio.client import DouyinClient
        from .dyaudio.config import load_config

        cfg_path = kwargs.get("config_path")
        cfg = load_config(cfg_path)
        if "cookie" in kwargs and kwargs["cookie"]:
            cfg.cookie = str(kwargs["cookie"])
        if "proxy" in kwargs and kwargs["proxy"]:
            cfg.proxy = str(kwargs["proxy"])
        if "audio_source" in kwargs and kwargs["audio_source"]:
            cfg.audio_source = str(kwargs["audio_source"])
        return DouyinClient(cfg), cfg

    def probe(self, target: str, **kwargs: Any) -> Dict[str, Any]:
        from .dyaudio.share_parser import parse_share_url
        from .dyaudio.user_crawler import (
            collect_awemes_by_mix,
            extract_sec_uid,
        )
        from .dyaudio.utils import sanitize_filename

        client, cfg = self._get_client(**kwargs)
        clean_target = target.strip()

        with client:
            # 0. 短链展开：`v.douyin.com` 既可能是单视频链也可能是主页链，先跟 302 再判类型，
            #    否则主页短链会被误判成单视频（短链字面量里既无 `/user/` 也无 `sec_uid`）。
            resolved = expand_share_link(client.session, clean_target)

            # 1. 判定是否为用户主页
            is_user = "/user/" in resolved or "sec_uid" in resolved
            if is_user:
                # 抖音对**匿名**访问有硬窗口：作品列表只放行约 20 条、翻页第二页即空、
                # 合集接口 403，且主页 HTML 是纯 JS 壳（无内联数据）——免 cookie 无解。
                # 必须主动说明，否则使用者会以为「已经抓全」（实测某博主 216 条只取到 21 条）。
                if not cfg.cookie:
                    print(
                        "\n[!] 未配置抖音登录态 Cookie —— 抖音对匿名访问只放行约 20 条作品，"
                        "翻页会直接返回空列表，\n"
                        "    合集接口亦返回 403，因此**无法取到全部作品**"
                        "（实测某博主真实 216 条、匿名仅取到 21 条）。\n"
                        "    这不是链接或网络问题，且免 cookie 没有可行绕行方案。\n"
                        "    如需完整抓取：浏览器登录 douyin.com → F12 → 应用/存储 → Cookie → 复制整串，再执行\n"
                        '      python src/cli.py login --douyin-cookie "<Cookie 串>"\n'
                        "    选择继续也可：任务会照常执行，但产物只覆盖实际取到的那些分集。\n"
                    )
                try:
                    sec_uid = extract_sec_uid(client.session, resolved)
                except Exception as err:
                    raise IngestionError(f"未能解析抖音用户主页: {err}") from err

                profile, groups = collect_awemes_by_mix(client, sec_uid, max_pages=cfg.max_pages)
                author_name = profile.get("nickname") or sec_uid
                safe_author = sanitize_filename(author_name, max_len=60)

                parts: List[Dict[str, Any]] = []
                page_idx = 1
                total_duration = 0

                for mix_name, awemes in groups.items():
                    for aweme in awemes:
                        aweme_id = str(aweme.get("aweme_id") or "")
                        if not aweme_id:
                            continue
                        desc = str(aweme.get("desc") or aweme_id)
                        dur = int(aweme.get("duration") or 0)
                        if dur > 1000:
                            dur = dur // 1000  # 毫秒转秒
                        total_duration += dur

                        display_title = f"[{mix_name}] {desc}" if mix_name != "未分类" and len(groups) > 1 else desc
                        # 作品类型：博主主页拿的是原始 aweme 节点，本地按 images 判定
                        # （与 dyaudio.share_parser 同一口径）。键名不带下划线，
                        # 否则会被 pipeline 落盘时剥离、进不了 parts.json。
                        media_kind = "image_album" if (aweme.get("images") or []) else "video"
                        parts.append({
                            "page": page_idx,
                            "title": display_title,
                            "cid": f"dy_{aweme_id}",
                            "duration": dur,
                            "url": f"https://www.douyin.com/video/{aweme_id}",
                            "filepath": "",
                            "media_kind": media_kind,
                            "_raw_aweme": aweme,
                        })
                        page_idx += 1

                if not parts:
                    raise IngestionError(f"未从抖音博主主页中获取到有效作品: {clean_target}")

                return {
                    "bvid": f"dy_u_{safe_author[:20]}",
                    "title": author_name,
                    "desc": f"抖音博主: {author_name}",
                    "duration": total_duration,
                    "owner": {"name": author_name, "mid": 0},
                    "video_type": "multi_page" if len(parts) > 1 else "single",
                    "type_desc": f"抖音作品合集 (共 {len(parts)} P)",
                    "cid": parts[0]["cid"],
                    "has_multi_pages": len(parts) > 1,
                    "has_ugc_season": False,
                    "season_episodes": [],
                    "parts": parts,
                    "is_local": False,
                    "source_type": "douyin",
                    "source_path": clean_target,
                }

            # 2. 单视频解析（resolved 已是最终地址，展开失败时等于原输入）
            try:
                info = parse_share_url(client.session, resolved)
            except Exception as err:
                raise IngestionError(f"抖音单视频解析失败: {err}") from err

            aweme_id = str(info.get("aweme_id") or "")
            desc = str(info.get("desc") or aweme_id)
            author = str(info.get("author") or "抖音作者")
            duration = int(info.get("duration") or 0)
            if duration > 1000:
                duration = duration // 1000

            parts = [{
                "page": 1,
                "title": desc,
                "cid": f"dy_{aweme_id}",
                "duration": duration,
                "url": f"https://www.douyin.com/video/{aweme_id}",
                "filepath": "",
                # 单视频分支已走过 parse_share_url → parse_single_aweme，
                # 直接用归一化结果，不在下游重判（单一事实来源）。
                "media_kind": info.get("media_kind") or "video",
                "_raw_aweme": info,
            }]

            return {
                "bvid": f"dy_{aweme_id}",
                "title": desc,
                "desc": f"抖音作品: {desc}",
                "duration": duration,
                "owner": {"name": author, "mid": 0},
                "video_type": "single",
                "type_desc": "抖音单视频",
                "cid": f"dy_{aweme_id}",
                "has_multi_pages": False,
                "has_ugc_season": False,
                "season_episodes": [],
                "parts": parts,
                "is_local": False,
                "source_type": "douyin",
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
        from .dyaudio.downloader import download_audio_for_aweme, normalize_aweme
        from .dyaudio.share_parser import parse_share_url

        output_file = Path(output_file).resolve()
        output_file.parent.mkdir(parents=True, exist_ok=True)

        if output_file.exists() and output_file.stat().st_size >= 10240 and not force:
            if progress_cb:
                progress_cb({"status": "cached", "file": str(output_file)})
            return output_file

        client, cfg = self._get_client(**kwargs)

        # 准备 aweme 信息字典
        raw_aweme = episode.get("_raw_aweme")
        with client:
            if not raw_aweme:
                url = episode.get("url")
                if not url:
                    cid = str(episode.get("cid") or "")
                    if cid.startswith("dy_"):
                        url = f"https://www.douyin.com/video/{cid[3:]}"
                    else:
                        raise IngestionError(f"缺少抖音作品链接: {episode.get('title')}")
                raw_aweme = parse_share_url(client.session, url)

            norm_info = normalize_aweme(raw_aweme) if "aweme_id" in raw_aweme and "video" in raw_aweme else raw_aweme

            # 下载到临时目录然后移至目标 output_file
            temp_dir = output_file.parent / ".dy_tmp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            saved_file = download_audio_for_aweme(norm_info, temp_dir, client.session, cfg)

            if not saved_file or not saved_file.exists() or saved_file.stat().st_size == 0:
                raise IngestionError(f"抖音音频下载失败: {episode.get('title')}")

            # 移动/重命名为预期的 output_file
            if saved_file != output_file:
                shutil.move(str(saved_file), str(output_file))

            # 清理临时目录
            try:
                if temp_dir.exists() and not any(temp_dir.iterdir()):
                    temp_dir.rmdir()
            except Exception:
                pass

        if progress_cb:
            progress_cb({"status": "downloaded", "file": str(output_file)})
        return output_file

    def check_readiness(self) -> Tuple[bool, str]:
        try:
            import requests
            req_ver = getattr(requests, "__version__", "已安装")
        except ImportError:
            return False, "缺少 requests 依赖 (pip install requests)"

        ffmpeg_bin = shutil.which("ffmpeg")
        status = f"就绪 (requests {req_ver})"
        if ffmpeg_bin:
            status += f", ffmpeg: {ffmpeg_bin}"
        else:
            status += ", 未检测到 ffmpeg (从视频抽音轨需 ffmpeg)"
        return True, status
