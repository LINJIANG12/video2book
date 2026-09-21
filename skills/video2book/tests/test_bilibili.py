# -*- coding: utf-8 -*-
"""B 站「独立 BV 合集」契约：多入口归一 + 跨 BV 取音。

真实验收场景（原 `scripts/selfcheck.py::check_bilibili_independent_bv_collection_contract`）：
`space.bilibili.com/<mid>/lists/<sid>?type=season` 里的每个条目都是**独立 BV**。
旧实现只认当前稿件的 `pages`，于是：

1. 合集页链接解析出来的是「当前那一集」，而不是整门课 → 每个 BV 各建一个工作区；
2. 更贵的一条：`P02` 以后取音时误用 `P01` 的 BV 号 → 整门课全部下载成第一集，
   而且直到转录完成都没人发现。

本模块**完全离线**：详情接口与合集种子解析被替换成手写字面量，
音视频流获取与落盘被替换成记录器；不发任何网络请求，也不依赖开发机的产物目录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.fetcher import AudioFetcher
from src.core.ingestion.bilibili import BilibiliProvider
from src.core.parser import BilibiliParser

MID = 87476569
SEASON_ID = 695667
SEED_BVID = "BV1Le4y1o7v5"
SECOND_BVID = "BV1Ge411u7d5"
THIRD_BVID = "BV1RV4y1T7jf"
COLLECTION_TITLE = "测试独立BV合集"

# 五种真实入口：前四种是合集页 / 播放列表页 / 合集内选集页 / 旧版收藏夹入口，第五种是脚本显式写法。
SEASON_ENTRIES = (
    ("https://space.bilibili.com/87476569/lists/695667?type=season",
     {"mid": MID, "season_id": SEASON_ID}),
    ("https://www.bilibili.com/list/87476569?sid=695667&type=season",
     {"mid": MID, "season_id": SEASON_ID}),
    ("https://www.bilibili.com/list/87476569?sid=695667&bvid=BV1RV4y1T7jf&oid=858000462&type=season",
     {"mid": MID, "season_id": SEASON_ID}),
    ("https://www.bilibili.com/medialist/play/87476569?business=space_collection&business_id=695667",
     {"mid": MID, "season_id": SEASON_ID}),
    ("season:695667",
     {"mid": None, "season_id": SEASON_ID}),
)

# 合集内的选集链接（带 bvid，指向第 2 集）与合集首页链接（不带 bvid）。
ENTRY_LIST = "https://www.bilibili.com/list/87476569?sid=695667&bvid=BV1Ge411u7d5&type=season"
ENTRY_SPACE = "https://space.bilibili.com/87476569/lists/695667?type=season"
COLLECTION_ENTRIES = (ENTRY_LIST, ENTRY_SPACE)

EXPECTED_BVIDS = [SEED_BVID, SECOND_BVID, THIRD_BVID]
EXPECTED_CIDS = [2001, 2002, 2003]


def _episode(section: str, idx: int, bvid: str, aid: int, cid: int, title: str, duration: int) -> dict:
    """旧版合集接口里单个 episode 的真实形态（page / pages / arc 三处都带时长与 cid）。"""
    return {
        "season_id": SEASON_ID,
        "section_id": 1,
        "episode_index": idx,
        "aid": aid,
        "cid": cid,
        "bvid": bvid,
        "title": title,
        "duration": duration,
        "page": {"cid": cid, "page": 1, "part": title, "duration": duration},
        "pages": [{"cid": cid, "page": 1, "part": title, "duration": duration}],
        "arc": {"duration": duration},
        "_section": section,
    }


# 详情接口返回值：稿件自身是「第 2 集」，但它所属的 ugc_season 覆盖全季三集。
RAW_COLLECTION = {
    "title": "P02 临时单集标题",
    "owner": {"name": "测试UP", "mid": MID},
    "desc": "",
    "duration": 30,
    "pic": "cover",
    "cid": 2002,
    "aid": 1002,
    "pages": [{"page": 1, "part": "P02 临时单集标题", "cid": 2002, "duration": 30}],
    "ugc_season": {
        "id": SEASON_ID,
        "title": COLLECTION_TITLE,
        "cover": "cover",
        "intro": "测试简介",
        "ep_count": 3,
        "sections": [{
            "title": "第一章",
            "episodes": [
                _episode("第一章", 1, SEED_BVID, 1001, 2001, "第1集", 10),
                _episode("第一章", 2, SECOND_BVID, 1002, 2002, "第2集", 20),
                _episode("第一章", 3, THIRD_BVID, 1003, 2003, "第3集", 30),
            ],
        }],
    },
}

EPISODE_TWO = {"bvid": SECOND_BVID, "cid": 2002, "title": "第2集"}


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 `urlopen` 换成直接失败：本模块「完全离线」的承诺不靠自觉，任何漏网的请求都会炸出来。"""
    import urllib.request

    def _blocked(*args, **kwargs):
        raise AssertionError("B 站用例必须完全离线：这里出现了网络调用")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


@pytest.fixture
def offline_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """把详情接口与合集种子解析换成手写字面量：整组用例零网络。"""
    def fake_fetch(cls, bvid, sessdata=None, wbi_keys_file=None, workspace=None):
        return RAW_COLLECTION

    def fake_resolve_seed(cls, season_ref, sessdata=None):
        return SEED_BVID

    monkeypatch.setattr(BilibiliParser, "fetch_video_view", classmethod(fake_fetch))
    monkeypatch.setattr(BilibiliParser, "resolve_season_seed_bvid", classmethod(fake_resolve_seed))


@pytest.fixture
def parsed(offline_collection):
    """解析一个入口链接，返回 parse_video 的结构化结果。"""
    def _parse(entry: str) -> dict:
        return BilibiliParser.parse_video(entry)

    return _parse


@pytest.fixture
def audio_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """记录「取音请求了哪个 bvid/cid、是否落盘」的探针，替换掉所有网络与 ffmpeg 动作。"""
    seen: dict = {}

    def fake_stream(cls, bvid, cid, sessdata=None, prefer_quality="low", wbi_keys_file=None):
        seen["bvid"] = bvid
        seen["cid"] = cid
        return {"best_stream_url": "https://example.invalid/audio.m4s"}

    def fake_download(cls, stream_url, output_filepath, repackage_m4a=True, max_bytes=None, sessdata=None):
        seen["downloaded"] = True
        seen["output"] = str(output_filepath)
        return str(output_filepath)

    monkeypatch.setattr(AudioFetcher, "get_audio_stream_info", classmethod(fake_stream))
    monkeypatch.setattr(AudioFetcher, "download_audio", classmethod(fake_download))

    target = tmp_path / "P02.m4a"

    def _fetch(episode: dict, **kwargs) -> Path:
        # sessdata 显式给值：避免凭证解析去读开发机的存档文件。
        kwargs.setdefault("sessdata", "test-sessdata")
        return BilibiliProvider().fetch_audio(episode, target, **kwargs)

    return {"seen": seen, "fetch": _fetch, "target": target}


# ---------------------------------------------------------------------------
# 多入口归一：提取 season 引用
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", SEASON_ENTRIES)
def test_extract_season_ref_normalizes_entry(url: str, expected: dict):
    """合集页 / 播放列表页 / 旧版收藏夹页 / 显式 season: 四种写法必须归一到同一个 season 引用。"""
    assert BilibiliParser.extract_season_ref(url) == expected


@pytest.mark.parametrize("url,_expected", SEASON_ENTRIES)
def test_provider_accepts_every_season_entry(url: str, _expected: dict):
    """所有合集入口都要被 BilibiliProvider 接受：漏一个就等于整门课进不了摄取链路。"""
    assert BilibiliProvider().match(url)


def test_provider_rejects_foreign_host_carrying_sid():
    """非 B 站域名不得因通用 `sid` 参数被误认成合集（否则会把别家链接交给 B 站下载器）。"""
    assert not BilibiliProvider().match("https://example.com/list/1?sid=2")


# ---------------------------------------------------------------------------
# 多入口归一：合集 episode 提升为整门课
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", COLLECTION_ENTRIES)
def test_collection_seed_bvid_is_first_episode(parsed, entry: str):
    """工作区入口键必须固定为首集 BV：否则同一个合集从不同链接进来会各建一个工作区。"""
    assert parsed(entry)["bvid"] == SEED_BVID


@pytest.mark.parametrize("entry", COLLECTION_ENTRIES)
def test_collection_promotes_season_title(parsed, entry: str):
    """课程标题取合集名而非当前稿件标题：否则整门课会被命名成「第2集」那种单集名。"""
    assert parsed(entry)["title"] == COLLECTION_TITLE


@pytest.mark.parametrize("entry", COLLECTION_ENTRIES)
def test_collection_becomes_multi_episode_course(parsed, entry: str):
    """合集必须被提升成 P01..PN 的多集课程拓扑，而不是停在「当前稿件的 pages」。"""
    info = parsed(entry)
    assert info["has_multi_pages"] is True
    assert len(info["parts"]) == 3


@pytest.mark.parametrize("entry", COLLECTION_ENTRIES)
def test_collection_duration_is_sum_of_episodes(parsed, entry: str):
    """合集总时长必须是各集之和（10+20+30）：取当前稿件的时长会让装箱按错误规模切块。"""
    assert parsed(entry)["duration"] == 60


@pytest.mark.parametrize("entry", COLLECTION_ENTRIES)
def test_collection_parts_are_renumbered_from_one(parsed, entry: str):
    """归一后集号必须重排为 1..N：沿用 episode_index 之外的编号会让 --range 对不上。"""
    assert [p["page"] for p in parsed(entry)["parts"]] == [1, 2, 3]


@pytest.mark.parametrize("entry", COLLECTION_ENTRIES)
def test_collection_parts_keep_their_own_bvid(parsed, entry: str):
    """每个 P 都要保留**自己那条 BV**：这是跨 BV 取音的唯一依据，丢了就全下第一集。"""
    assert [p["bvid"] for p in parsed(entry)["parts"]] == EXPECTED_BVIDS


@pytest.mark.parametrize("entry", COLLECTION_ENTRIES)
def test_collection_parts_keep_their_own_cid(parsed, entry: str):
    """每个 P 的 cid 也必须各自就位（cid 混用会取到别集的音轨）。"""
    assert [p["cid"] for p in parsed(entry)["parts"]] == EXPECTED_CIDS


# ---------------------------------------------------------------------------
# 多入口归一：链接定位到具体某一集
# ---------------------------------------------------------------------------

def test_episode_link_selects_its_page(parsed):
    """合集内的选集链接要定位到它对应的 P 序号（否则派发会打到第一集上）。"""
    assert parsed(ENTRY_LIST)["url_page"] == 2


def test_episode_link_selects_its_cid(parsed):
    """选集链接的 cid 必须取那一集自己的，而不是稿件级 cid 碰巧相同的值。"""
    assert parsed(ENTRY_LIST)["selected_cid"] == EXPECTED_CIDS[1]


def test_season_home_link_selects_no_episode(parsed):
    """合集首页链接没有被选中某集：不得默认认成第 1 集（须由用户显式指定范围）。"""
    assert parsed(ENTRY_SPACE)["url_page"] is None


def test_season_home_link_defaults_to_first_episode_cid(parsed):
    """首页链接的默认 cid 取首集，而不是当前那条稿件的 cid（后者属于第 2 集）。"""
    assert parsed(ENTRY_SPACE)["selected_cid"] == EXPECTED_CIDS[0]


# ---------------------------------------------------------------------------
# 跨 BV 取音
# ---------------------------------------------------------------------------

def test_cross_bv_fetch_uses_episode_own_bvid(audio_route):
    """P02 取音必须用它自己的 BV；退回入口 BV 会把整门课都下成第一集。"""
    audio_route["fetch"](EPISODE_TWO, bvid=SEED_BVID)
    assert audio_route["seen"]["bvid"] == SECOND_BVID


def test_cross_bv_fetch_uses_episode_own_cid(audio_route):
    """cid 同样以分集条目为准：取错 cid 会拿到同一稿件里别的分 P。"""
    audio_route["fetch"](EPISODE_TWO, bvid=SEED_BVID)
    assert audio_route["seen"]["cid"] == 2002


def test_cross_bv_fetch_downloads_to_requested_path(audio_route):
    """拿到直链后必须把音频落到该分集的目标文件，并原样返回该路径。"""
    result = audio_route["fetch"](EPISODE_TWO, bvid=SEED_BVID)
    assert audio_route["seen"]["downloaded"] is True
    assert audio_route["seen"]["output"] == str(audio_route["target"])
    assert result == audio_route["target"]


# ---------------------------------------------------------------------------
# 含多 P 稿件的合集：退回旧语义，但必须说清原因（第二阶段 A10）
# ---------------------------------------------------------------------------

def _collection_with_multi_page_episode() -> dict:
    """把第 2 集改成多 P 稿件——这正是让整门课退回旧语义的触发条件。"""
    raw = json.loads(json.dumps(RAW_COLLECTION, ensure_ascii=False))
    episodes = raw["ugc_season"]["sections"][0]["episodes"]
    episodes[1]["pages"] = [
        {"cid": 2002, "page": 1, "part": "第2集上", "duration": 20},
        {"cid": 2003, "page": 2, "part": "第2集下", "duration": 20},
    ]
    return raw


@pytest.fixture
def offline_mixed_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """合集里有一集是多 P：详情接口与种子解析仍走手写字面量，零网络。"""
    def fake_fetch(cls, bvid, sessdata=None, wbi_keys_file=None, workspace=None):
        return _collection_with_multi_page_episode()

    def fake_resolve_seed(cls, season_ref, sessdata=None):
        return SEED_BVID

    monkeypatch.setattr(BilibiliParser, "fetch_video_view", classmethod(fake_fetch))
    monkeypatch.setattr(BilibiliParser, "resolve_season_seed_bvid", classmethod(fake_resolve_seed))


def test_multi_page_episode_keeps_legacy_semantics(offline_mixed_collection, capsys):
    """合集里只要有一集是多 P，整门课退回逐 BV 旧语义（不是只特殊处理那一集）。

    退回本身是**有意的**（避免改变已在用 hybrid 结构的旧工作流），本用例钉住的是：
    退回后 `parts` 来自当前稿件自己的 `pages`，而不是合集归一后的 P01..PN。
    """
    info = BilibiliParser.parse_video(ENTRY_LIST)
    assert info["has_multi_pages"] is False
    assert info["page_count"] == 1, info["parts"]
    assert info["bvid"] == SECOND_BVID, "退回旧语义后工作区入口应是当前稿件自己的 BV"


def test_multi_page_episode_fallback_is_reported(offline_mixed_collection, capsys):
    """退回旧语义时必须**说清是哪一集触发的**。

    第二阶段 A10：以前这里是静默的，用户只会看到产物结构从「P01..PN 一门课」突然变成
    「逐 BV 各建工作区」，却不知道原因。
    """
    BilibiliParser.parse_video(ENTRY_LIST)
    out = capsys.readouterr().out
    assert "多 P" in out, out
    assert "第2集" in out, out
    assert "不会归一到 P01-PN" in out, out
