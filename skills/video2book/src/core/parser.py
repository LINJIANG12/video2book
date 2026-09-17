"""视频地址解析与稿件结构分类器。

分类：
1. 单稿单集：单个独立视频
2. 分集选集：同一稿件下多分集
3. 合集系列：投稿者创建的合集
4. 复合类型：多分集且归属合集
"""

import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .wbi import WbiSigner


class BilibiliParser:
    """视频解析器（详情接口强制签名请求）。"""

    VIEW_API = "https://api.bilibili.com/x/web-interface/wbi/view"
    SEASON_ARCHIVES_API = "https://api.bilibili.com/x/space/fav/season/list"
    SEASON_ARCHIVES_FALLBACK_API = "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.bilibili.com",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }

    @staticmethod
    def _resolve_keys_path(keys_file: Optional[Any] = None, workspace: Optional[Any] = None) -> Optional[str]:
        """解析签名密钥文件路径（优先使用工作区提供的路径）。"""
        try:
            if workspace is not None and hasattr(workspace, "wbi_keys_file"):
                return str(workspace.wbi_keys_file)
        except Exception:
            pass
        if keys_file is not None:
            return str(keys_file)
        return None

    @staticmethod
    def extract_bvid(url_or_bvid: str) -> Optional[str]:
        """从原始字符串、链接或短链中提取合法稿件号。"""
        text = url_or_bvid.strip()
        bv_pattern = re.compile(r"(BV[a-zA-Z0-9]{10})", re.IGNORECASE)
        match = bv_pattern.search(text)
        if match:
            return match.group(1)

        # 处理短链（跟随跳转）
        if "b23.tv" in text:
            short_url_match = re.search(r"https?://b23\.tv/[a-zA-Z0-9]+", text)
            if short_url_match:
                try:
                    req = urllib.request.Request(
                        short_url_match.group(0),
                        headers=BilibiliParser.DEFAULT_HEADERS,
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        final_match = bv_pattern.search(resp.geturl())
                        if final_match:
                            return final_match.group(1)
                except Exception as err:
                    print(f"[Warn] Failed to resolve b23.tv redirect: {err}", file=sys.stderr)

        return None

    @staticmethod
    def extract_season_ref(url_or_id: str) -> Optional[Dict[str, Any]]:
        """从 B 站旧版合集入口提取 ``season_id`` / ``mid``。

        兼容以下常见形态（页面是否被网页风控拦截不影响 API 解析）：

        - ``space.bilibili.com/<mid>/lists/<season_id>?type=season``
        - ``www.bilibili.com/list/<mid>?sid=<season_id>&type=season``
        - ``www.bilibili.com/list/<mid>?season_id=<season_id>``
        - ``www.bilibili.com/medialist/play/<mid>?business_id=<season_id>``
        - 仅在 ``bvid`` / ``oid`` 之外携带 ``sid`` / ``season_id`` 的合集内选集链接

        返回值形如 ``{"mid": 87476569, "season_id": 695667}``；无法识别时返回 ``None``。
        独立的单个 BV 号不在此处识别，仍由 ``extract_bvid`` 负责。
        """
        text = str(url_or_id or "").strip()
        if not text:
            return None

        # 允许脚本显式传 `season:695667`，避免把裸数字当成 BVID 产生歧义。
        explicit = re.fullmatch(r"season[:/](\d+)", text, re.IGNORECASE)
        if explicit:
            return {"mid": None, "season_id": int(explicit.group(1))}

        try:
            parsed = urllib.parse.urlparse(text)
        except Exception:
            return None
        if not parsed.scheme and not parsed.netloc:
            return None

        path = urllib.parse.unquote(parsed.path or "")
        qs = urllib.parse.parse_qs(parsed.query or "")

        mid: Optional[int] = None
        season_id: Optional[int] = None

        # `space.bilibili.com/<mid>/lists/<season_id>`：路径里同时包含 mid 与 season_id。
        matched = re.search(r"/(\d+)/lists/(\d+)(?:/|$)", path, re.IGNORECASE)
        if matched:
            try:
                mid = int(matched.group(1))
                season_id = int(matched.group(2))
            except ValueError:
                pass

        # 新版列表页 / 播放列表入口的路径段是 mid，season id 仍在查询参数里。
        if mid is None:
            for pattern in (
                r"/(?:list)/(\d+)(?:/|$)",
                r"/(?:medialist/play)/(\d+)(?:/|$)",
            ):
                matched = re.search(pattern, path, re.IGNORECASE)
                if matched:
                    try:
                        mid = int(matched.group(1))
                    except ValueError:
                        mid = None
                    break

        # `type=season` 页面可能把合集号放在 sid / season_id / business_id。
        if not season_id:
            for key in ("sid", "season_id", "seasonId", "business_id"):
                values = qs.get(key) or []
                for value in values:
                    try:
                        candidate = int(str(value).strip())
                    except ValueError:
                        continue
                    if candidate > 0:
                        season_id = candidate
                        break
                if season_id:
                    break

        # 某些链接把 season id 放在路径中（如 /list/ml695667），做一层保守兜底。
        if not season_id:
            matched = re.search(r"/(?:ml|l)(\d+)(?:/|$)", path, re.IGNORECASE)
            if matched:
                try:
                    season_id = int(matched.group(1))
                except ValueError:
                    season_id = None

        if not season_id:
            return None
        return {"mid": mid, "season_id": season_id}

    @classmethod
    def _fetch_public_json(
        cls,
        url: str,
        *,
        sessdata: Optional[str] = None,
        timeout: int = 15,
    ) -> Dict[str, Any]:
        """请求无需 WBI 的公开 B 站接口，复用统一限速与浏览器请求头。"""
        headers = dict(cls.DEFAULT_HEADERS)
        if sessdata:
            headers["Cookie"] = f"SESSDATA={sessdata}"
        WbiSigner.wait_rate_limit()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raise RuntimeError(
                f"[网络]合集接口请求失败（{err.code}）。建议动作：检查网络或补充有效 SESSDATA 后重试。"
            ) from err
        except Exception as err:
            raise RuntimeError(
                f"[网络]合集接口请求失败：{err}。建议动作：检查网络连接后重试。"
            ) from err
        if not isinstance(data, dict):
            raise RuntimeError("[网络]合集接口返回非预期结构。")
        if data.get("code") != 0:
            raise RuntimeError(
                f"[风控]合集接口返回异常：code={data.get('code')}，msg={data.get('message')}。"
                "建议动作：确认合集可见性与凭证权限后重试。"
            )
        return data.get("data") or {}

    @classmethod
    def resolve_season_seed_bvid(
        cls,
        season_ref: Dict[str, Any],
        *,
        sessdata: Optional[str] = None,
    ) -> Optional[str]:
        """用旧版合集 id 找一个可打开详情接口的 BV 作为入口。

        详情接口会返回该 BV 所属的完整 ``ugc_season``，随后即可一次性得到全季每集的
        ``bvid`` 与 ``cid``，因此这里只取第一个稿件，不需要逐集请求。
        """
        season_id = int(season_ref.get("season_id") or 0)
        mid = season_ref.get("mid")
        if season_id <= 0:
            return None

        params = urllib.parse.urlencode({"season_id": season_id, "pn": 1, "ps": 1})
        data = cls._fetch_public_json(
            f"{cls.SEASON_ARCHIVES_API}?{params}",
            sessdata=sessdata,
        )
        medias = data.get("medias") or []
        for media in medias:
            bvid = cls.extract_bvid(str(media.get("bvid") or ""))
            if bvid:
                return bvid

        if mid:
            fallback_params = urllib.parse.urlencode({
                "mid": int(mid),
                "season_id": season_id,
                "sort_reverse": "false",
                "page_num": 1,
                "page_size": 1,
            })
            fallback = cls._fetch_public_json(
                f"{cls.SEASON_ARCHIVES_FALLBACK_API}?{fallback_params}",
                sessdata=sessdata,
            )
            for archive in fallback.get("archives") or []:
                bvid = cls.extract_bvid(str(archive.get("bvid") or ""))
                if bvid:
                    return bvid
        return None

    @staticmethod
    def extract_page_index(url_or_bvid: str) -> Optional[int]:
        """从链接中提取分集序号（无则返回空）。"""
        try:
            parsed = urllib.parse.urlparse(url_or_bvid.strip())
            qs = urllib.parse.parse_qs(parsed.query)
            if "p" in qs and qs["p"]:
                val = int(qs["p"][0])
                if val >= 1:
                    return val
        except Exception:
            pass
        return None

    @classmethod
    def fetch_video_view(
        cls,
        bvid: str,
        sessdata: Optional[str] = None,
        wbi_keys_file: Optional[str] = None,
        workspace: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """请求详情接口并返回原始元数据（强制签名，区分导航失败与详情失败）。"""
        # 密钥路径由工作区提供，含有效期，过期由签名器重取
        resolved_keys_path = cls._resolve_keys_path(keys_file=wbi_keys_file, workspace=workspace)

        # 详情参数必须经签名器加签
        try:
            signed_params = WbiSigner.enc_wbi({"bvid": bvid}, sessdata=sessdata, keys_file=resolved_keys_path)
        except RuntimeError as err:
            err_msg = str(err)
            if "[风控]" in err_msg or "[网络]" in err_msg:
                raise RuntimeError(
                    f"[导航失败]{err_msg}"
                    "建议动作：检查登录凭证有效性，等待后降低频率重试。"
                ) from err
            raise RuntimeError(
                f"[签名]导航阶段签名失败：{err}。"
                "建议动作：删除过期密钥文件后重试；仍失败请检查网络。"
            ) from err
        except Exception as err:
            raise RuntimeError(
                f"[签名]导航阶段签名失败：{err}。"
                "建议动作：删除过期密钥文件后重试；仍失败请检查网络。"
            ) from err

        params = urllib.parse.urlencode(signed_params)
        url = f"{cls.VIEW_API}?{params}"
        headers = dict(cls.DEFAULT_HEADERS)
        if sessdata:
            headers["Cookie"] = f"SESSDATA={sessdata}"

        # 复用集中限速，避免高频触发风控；增加 3 次指数退避重试（对齐 fetcher）
        max_retries = 3
        base_backoff = 1.5
        data = None

        for attempt in range(1, max_retries + 2):
            WbiSigner.wait_rate_limit()
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    break
            except urllib.error.HTTPError as err:
                retryable = (err.code == 412) or (500 <= err.code <= 599)
                if retryable and attempt <= max_retries:
                    retry_after = err.headers.get("Retry-After") if hasattr(err, "headers") else None
                    if retry_after and retry_after.isdigit():
                        wait = float(retry_after)
                    else:
                        wait = base_backoff * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5)
                    print(f"[重试]详情接口状态异常（{err.code}），{wait:.1f}秒后重试（第{attempt}次）")
                    time.sleep(wait)
                    continue
                if err.code == 412:
                    raise RuntimeError(
                        "[风控]详情接口被风控拦截（412），签名可能过期或频率过高。"
                        "建议动作：携带有效登录凭证、删除过期密钥文件后降频重试。"
                    ) from err
                if 500 <= err.code <= 599:
                    raise RuntimeError(
                        f"[网络]详情接口服务端异常（{err.code}）。"
                        "建议动作：等待后退避重试。"
                    ) from err
                raise RuntimeError(
                    f"[网络]详情接口请求失败（{err.code}）。"
                    "建议动作：检查网络后重试。"
                ) from err
            except Exception as err:
                if attempt <= max_retries:
                    wait = base_backoff * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5)
                    print(f"[重试]详情接口网络异常：{err}，{wait:.1f}秒后重试（第{attempt}次）")
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    f"[网络]详情接口请求失败：{err}。"
                    "建议动作：检查网络连接或代理后重试。"
                ) from err

        if data.get("code") != 0:
            code = data.get("code")
            msg = data.get("message")
            if code == -412:
                raise RuntimeError(
                    f"[风控]详情接口返回风控拦截：code={code}，msg={msg}。"
                    "建议动作：携带有效登录凭证、删除过期密钥文件并降频重试。"
                )
            if code in (-403, -404, 62002, 62004):
                raise RuntimeError(
                    f"[风控]详情接口访问受限：code={code}，msg={msg}。"
                    "建议动作：确认稿件可见性与凭证权限后重试。"
                )
            raise RuntimeError(
                f"[风控]详情接口返回异常：code={code}，msg={msg}。"
                "建议动作：确认稿件号正确、凭证有效后重试。"
            )
        return data["data"]

    @classmethod
    def parse_video(
        cls,
        url_or_bvid: str,
        sessdata: Optional[str] = None,
        wbi_keys_file: Optional[str] = None,
        workspace: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """分析稿件结构并返回分类与分集信息。"""
        bvid = cls.extract_bvid(url_or_bvid)
        season_ref = cls.extract_season_ref(url_or_bvid)
        if not bvid and season_ref:
            bvid = cls.resolve_season_seed_bvid(season_ref, sessdata=sessdata)
        if not bvid:
            raise ValueError(f"Could not extract a valid BVID from input: {url_or_bvid}")

        raw = cls.fetch_video_view(bvid, sessdata=sessdata, wbi_keys_file=wbi_keys_file, workspace=workspace)

        title = raw.get("title", "")
        owner = raw.get("owner", {}).get("name", "")
        owner_mid = raw.get("owner", {}).get("mid", 0)
        desc = raw.get("desc", "")
        duration = raw.get("duration", 0)
        pic = raw.get("pic", "")
        pages: List[Dict[str, Any]] = raw.get("pages", [])
        ugc_season: Optional[Dict[str, Any]] = raw.get("ugc_season")

        has_multi_pages = len(pages) > 1
        has_ugc_season = bool(ugc_season)

        if not has_multi_pages and not has_ugc_season:
            video_type = "single"
            type_desc = "单个独立视频（单P，无合集）"
        elif has_multi_pages and not has_ugc_season:
            video_type = "multi_page"
            type_desc = f"分P选集视频（稿件内包含 {len(pages)} 个视频选集）"
        elif not has_multi_pages and has_ugc_season:
            video_type = "ugc_season"
            season_title = ugc_season.get("title", "")
            type_desc = f"合集/系列视频（UGC Season，合集名: {season_title}）"
        else:
            video_type = "hybrid"
            season_title = ugc_season.get("title", "")
            type_desc = f"复合型视频（本稿件含 {len(pages)} 个分P，且属于合集【{season_title}】）"

        # 构建干净分集列表
        parts = []
        for p in pages:
            p_num = p.get("page", 1)
            parts.append({
                "page": p_num,
                "title": p.get("part", ""),
                "cid": p.get("cid"),
                "duration": p.get("duration", 0),
                "url": f"https://www.bilibili.com/video/{bvid}?p={p_num}",
            })

        # 构建合集选集列表
        season_episodes = []
        season_info = None
        if has_ugc_season and ugc_season:
            season_info = {
                "season_id": ugc_season.get("id"),
                "title": ugc_season.get("title"),
                "cover": ugc_season.get("cover"),
                "intro": ugc_season.get("intro"),
                "ep_count": ugc_season.get("ep_count", 0),
            }
            sections = ugc_season.get("sections", [])
            for sec_idx, sec in enumerate(sections, 1):
                sec_title = sec.get("title", f"第{sec_idx}部分")
                for ep_idx, ep in enumerate(sec.get("episodes", []), 1):
                    ep_bvid = ep.get("bvid", "")
                    season_episodes.append({
                        "section_title": sec_title,
                        "episode_index": ep_idx,
                        "bvid": ep_bvid,
                        "aid": ep.get("aid"),
                        "cid": (
                            ep.get("cid")
                            or (ep.get("page") or {}).get("cid")
                            or ((ep.get("pages") or [{}])[0].get("cid"))
                        ),
                        "title": ep.get("title", ""),
                        "duration": (
                            ep.get("duration")
                            or (ep.get("page") or {}).get("duration")
                            or ((ep.get("pages") or [{}])[0].get("duration"))
                            or ((ep.get("arc") or {}).get("duration"))
                            or 0
                        ),
                        "url": f"https://www.bilibili.com/video/{ep_bvid}?season_id={ugc_season.get('id')}",
                        "pages_count": len(ep.get("pages", [])),
                    })

        # 旧版合集与「UGC 合集里每集独立 BV」在 API 里都表现为 ugc_season。只要合集里有
        # 多集，就把它们提升成统一课程拓扑：后续 pipeline / audio / cluster-* 不再逐 BV
        # 另建工作区，而是按 P01..PN 处理整门课。
        # 只把「每个稿件都是单 P」的独立 BV 合集提升为统一课程；复合型稿件仍保留原有
        # 分 P 语义，避免改变已经在使用 hybrid 结构的旧工作流。
        collection_mode = (
            len(season_episodes) > 1
            and all(int(ep.get("pages_count") or 0) <= 1 for ep in season_episodes)
        )
        if collection_mode:
            aggregate_parts = []
            for idx, ep in enumerate(season_episodes, 1):
                aggregate_parts.append({
                    "page": idx,
                    "title": ep.get("title") or f"第{idx}集",
                    "cid": ep.get("cid"),
                    "duration": ep.get("duration") or 0,
                    "url": ep.get("url") or f"https://www.bilibili.com/video/{ep.get('bvid')}",
                    "bvid": ep.get("bvid") or "",
                    "aid": ep.get("aid"),
                    "season_id": ugc_season.get("id"),
                    "section_title": ep.get("section_title") or "",
                    "episode_index": ep.get("episode_index") or idx,
                })
            parts = aggregate_parts
            has_multi_pages = True
            title = str(ugc_season.get("title") or title)
            desc = str(ugc_season.get("intro") or desc)
            pic = str(ugc_season.get("cover") or pic)
            duration = sum(int(p.get("duration") or 0) for p in parts)
            type_desc = (
                f"B 站独立 BV 合集（UGC Season，合集名: {title}；"
                f"{len(parts)} 个稿件已归一为 P01-P{len(parts):02d}）"
            )
            video_type = "ugc_season"
            # 统一用一个稳定入口 BV 命名工作区：无论用户传合集页、列表页还是合集内
            # 任意一集链接，都指向同一个工作区。
            bvid = str(parts[0].get("bvid") or bvid)

        input_page = cls.extract_page_index(url_or_bvid)
        url_page = input_page
        if collection_mode:
            selected_idx = 0
            input_bvid = cls.extract_bvid(url_or_bvid)
            if input_bvid:
                for idx, part in enumerate(parts, 1):
                    if str(part.get("bvid") or "").lower() == input_bvid.lower():
                        selected_idx = idx
                        break
            if not selected_idx:
                try:
                    parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(str(url_or_bvid)).query)
                except Exception:
                    parsed_qs = {}
                selected_aid = str((parsed_qs.get("oid") or parsed_qs.get("aid") or [""])[0])
                if selected_aid:
                    for idx, part in enumerate(parts, 1):
                        if str(part.get("aid") or "") == selected_aid:
                            selected_idx = idx
                            break
            if not selected_idx and input_page and 1 <= input_page <= len(parts):
                selected_idx = input_page
            if selected_idx:
                url_page = selected_idx
            elif season_ref:
                url_page = None

        selected_cid = raw.get("cid", 0)
        selected_title = title
        if url_page and 1 <= url_page <= len(parts):
            matched_part = parts[url_page - 1]
            selected_cid = matched_part["cid"]
            selected_title = matched_part["title"]
        elif collection_mode and parts:
            selected_cid = parts[0].get("cid", selected_cid)
            selected_title = parts[0].get("title", selected_title)

        return {
            "bvid": bvid,
            "aid": raw.get("aid"),
            "cid": selected_cid,  # 链接含分集序号时取对应分集，否则取首集
            "title": title,
            "desc": desc,
            "cover": pic,
            "duration": duration,
            "owner": {"name": owner, "mid": owner_mid},
            "video_type": video_type,
            "type_desc": type_desc,
            "has_multi_pages": has_multi_pages,
            "has_ugc_season": has_ugc_season,
            "page_count": len(parts),
            "parts": parts,
            "season_info": season_info,
            "season_episodes": season_episodes,
            "url_page": url_page,
            "selected_cid": selected_cid,
            "selected_title": selected_title,
        }

    # 别名入口
    parse = parse_video
