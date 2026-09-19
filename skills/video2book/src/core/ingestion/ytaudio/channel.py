"""频道：分页抓取全部视频 + 播放列表（合集）识别与归类。

分页逻辑
--------
YouTube 频道标签页与播放列表都采用「首次响应 + continuation token」的续页机制，
yt-dlp 内部已实现续页；本模块在其之上做**受控分页**：

* ``lazy_playlist=False`` 让列表一次性展开，便于统计与切片；
* 需要「只取最新 N 条」时用 ``playlistend`` 限制窗口，**避免全量抓取**；
  若窗口内过滤掉短视频后不足 N 条，则按 3x → 8x → 全量逐步放大重试。

合集识别
--------
YouTube 没有「合集」概念，对应物是**播放列表(Playlist)**。做法：

1. 抓频道 ``/playlists`` 标签，拿到用户创建的播放列表；
2. 逐个扁平提取播放列表条目，建立 ``视频ID → 播放列表名`` 映射（先到先得）；
3. 频道视频按映射归入对应播放列表子目录，不属于任何播放列表的进 ``未分类``。
4. 自动生成的播放列表（如「上传的视频」「直播回放」）不视作合集，自动忽略。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Tuple

from .config import Config
from .engine import Engine
from .filters import is_auto_playlist, is_regular_video
from .utils import get_logger, sanitize_filename

UNCLASSIFIED = "未分类"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


def canonical_url(video_id: str) -> str:
    """统一归一化为标准 watch 链接（避免 shorts/live 形态混入）。"""
    return WATCH_URL.format(video_id=video_id)


class ChannelError(RuntimeError):
    pass


# ---------------------------------------------------------------------- #
# 视频列表
# ---------------------------------------------------------------------- #
def _filter_videos(entries: List[Dict[str, Any]], cfg: Config) -> List[Dict[str, Any]]:
    log = get_logger()
    kept: List[Dict[str, Any]] = []
    for entry in entries:
        keep, reason = is_regular_video(entry, cfg)
        if keep:
            kept.append(entry)
        else:
            log.debug("过滤 %s：%s", entry.get("id"), reason)
    return kept


def fetch_channel_videos(engine: Engine, base: str, cfg: Config,
                         limit: int = 0) -> List[Dict[str, Any]]:
    """抓取频道视频（仅普通视频）。

    :param limit: 只取最新的 N 条；0 表示全部。
    """
    log = get_logger()
    videos_url, _, _ = base + "/videos", base + "/playlists", base + "/shorts"

    if limit and limit > 0:
        # 逐步放大窗口，兼顾速度与「过滤后仍够 N 条」
        for multiplier in (3, 8, 0):
            end = limit * multiplier if multiplier else None
            entries = engine.extract_list(videos_url, playlistend=end)
            videos = _filter_videos(entries, cfg)
            log.info("窗口 %s：解析 %s 条，保留 %s 条普通视频",
                     end or "全部", len(entries), len(videos))
            if len(videos) >= limit or end is None:
                return videos[:limit]
        return videos[:limit]

    entries = engine.extract_list(videos_url, playlistend=cfg.max_videos or None)
    videos = _filter_videos(entries, cfg)
    log.info("频道视频：解析 %s 条，保留 %s 条普通视频", len(entries), len(videos))
    return videos


# ---------------------------------------------------------------------- #
# 播放列表（合集）
# ---------------------------------------------------------------------- #
def fetch_channel_playlists(engine: Engine, base: str, cfg: Config) -> List[Tuple[str, str]]:
    """抓取频道创建的播放列表，返回 ``[(播放列表ID, 标题)]``。"""
    log = get_logger()
    playlists_url = base + "/playlists"
    entries = engine.extract_list(playlists_url, playlistend=cfg.max_playlists or None)

    result: List[Tuple[str, str]] = []
    for entry in entries:
        playlist_id = entry.get("id") or entry.get("playlist_id")
        title = (entry.get("title") or "").strip()
        if not playlist_id or not title:
            continue
        if is_auto_playlist(title):
            log.debug("忽略自动播放列表：%s", title)
            continue
        result.append((str(playlist_id), title))

    log.info("识别到 %s 个播放列表（合集）", len(result))
    return result


def build_playlist_map(engine: Engine, playlists: List[Tuple[str, str]], cfg: Config
                       ) -> "OrderedDict[str, str]":
    """建立 ``视频ID → 播放列表名`` 映射（先到先得，保持播放列表顺序）。"""
    log = get_logger()
    vmap: "OrderedDict[str, str]" = OrderedDict()
    for playlist_id, title in playlists:
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        entries = engine.extract_list(url)
        count = 0
        for entry in entries:
            video_id = entry.get("id")
            if not video_id:
                continue
            keep, _ = is_regular_video(entry, cfg)
            if not keep:
                continue
            if video_id not in vmap:
                vmap[str(video_id)] = title
                count += 1
        log.debug("播放列表「%s」贡献 %s 条新映射", title, count)
    return vmap


def group_by_playlist(videos: List[Dict[str, Any]], vmap: "OrderedDict[str, str]",
                      cfg: Config) -> "OrderedDict[str, List[Dict[str, Any]]]":
    """按播放列表分组；不属于任何播放列表的进 ``未分类``。"""
    groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()

    # 先按播放列表出现顺序建桶，保证目录顺序稳定
    for name in vmap.values():
        groups.setdefault(name, [])

    for video in videos:
        name = vmap.get(str(video.get("id"))) or UNCLASSIFIED
        groups.setdefault(name, []).append(video)

    # 去掉空桶（播放列表里没有本频道视频的情况）
    return OrderedDict((k, v) for k, v in groups.items() if v)


def enrich_with_playlist_videos(engine: Engine, cfg: Config,
                                groups: "OrderedDict[str, List[Dict[str, Any]]]",
                                playlists: List[Tuple[str, str]]
                                ) -> "OrderedDict[str, List[Dict[str, Any]]]":
    """把「只在播放列表中、未出现在频道视频列表」的视频补进对应分组。"""
    log = get_logger()
    known_ids = {str(v.get("id")) for items in groups.values() for v in items}
    added = 0

    for playlist_id, title in playlists:
        bucket = groups.setdefault(title, [])
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        for entry in engine.extract_list(url):
            video_id = str(entry.get("id") or "")
            if not video_id or video_id in known_ids:
                continue
            keep, _ = is_regular_video(entry, cfg)
            if not keep:
                continue
            bucket.append(entry)
            known_ids.add(video_id)
            added += 1

    if added:
        log.info("播放列表补全新增 %s 条视频", added)
    return groups


# ---------------------------------------------------------------------- #
# 一站式
# ---------------------------------------------------------------------- #
def collect_channel(engine: Engine, base: str, cfg: Config, limit: int = 0
                    ) -> Tuple[str, "OrderedDict[str, List[Dict[str, Any]]]"]:
    """抓频道视频并按合集分组。

    :return: ``(频道名, 有序分组)``
    """
    log = get_logger()
    videos = fetch_channel_videos(engine, base, cfg, limit=limit)
    if not videos:
        raise ChannelError("未解析到任何普通视频（可能全为 Shorts/直播，或频道地址有误）")

    channel_name = _guess_channel_name(videos, base)
    log.info("频道：%s，待处理视频 %s 条", channel_name, len(videos))

    vmap: "OrderedDict[str, str]" = OrderedDict()
    playlists: List[Tuple[str, str]] = []
    if not cfg.flat:
        try:
            playlists = fetch_channel_playlists(engine, base, cfg)
            vmap = build_playlist_map(engine, playlists, cfg)
        except Exception as exc:  # noqa: BLE001 - 归类失败不影响下载
            log.warning("播放列表归类失败，全部归入「%s」：%s", UNCLASSIFIED, exc)

    groups = group_by_playlist(videos, vmap, cfg) if not cfg.flat \
        else OrderedDict([(UNCLASSIFIED, videos)])

    if cfg.enrich_playlists and not cfg.flat and playlists:
        groups = enrich_with_playlist_videos(engine, cfg, groups, playlists)

    return channel_name, groups


def _guess_channel_name(videos: List[Dict[str, Any]], base: str) -> str:
    for video in videos:
        for key in ("channel", "uploader", "channel_id"):
            value = video.get(key)
            if value:
                return sanitize_filename(str(value), max_len=60)
    return sanitize_filename(base.rsplit("/", 1)[-1].lstrip("@"), max_len=60)
