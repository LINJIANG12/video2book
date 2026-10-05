# -*- coding: utf-8 -*-
"""B 站字幕 → 块级逐字稿：字幕类型判定 + 中文筛选 + 内容身份校验 + 块内拼装。

本模块**完全离线**：只测纯函数（`_is_ai` / `pick_chinese_subtitle` / `subtitle_identity` /
`subtitle_time_band` / `assemble_block_transcript`）与重试预算（打桩，不发网络请求）。
串台样本取自工作区真实逐字稿的页面切片，见 `fixtures/subtitle_pages.json`。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.core import subtitles
from src.core.subtitles import (
    IDENTITY_RETRY_MAX,
    SUBTITLE_COVERAGE_MIN,
    SUBTITLE_OVERRUN_MAX,
    _is_ai,
    _is_chinese,
    assemble_block_transcript,
    merge_block_transcript,
    pick_chinese_subtitle,
    resolve_course_bvid,
    subtitle_coverage,
    subtitle_time_band,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "subtitle_pages.json").read_text(encoding="utf-8")
)


def _cue_list(entry: dict) -> list:
    return [{"from": f, "to": t, "content": c} for f, t, c in entry["cues"]]

# 实测两种字幕的真实形态：人工 zh-Hans/type=0，AI ai-zh/type=1；两者的 ai_type/ai_status 都是 0。
HUMAN_ZH = {
    "lan": "zh-Hans",
    "lan_doc": "中文（简体）",
    "type": 0,
    "ai_type": 0,
    "ai_status": 0,
    "subtitle_url": "//aisubtitle.hdslb.com/bfs/subtitle/abc.json",
}
AI_ZH = {
    "lan": "ai-zh",
    "lan_doc": "中文（自动生成）",
    "type": 1,
    "ai_type": 0,
    "ai_status": 0,
    "subtitle_url": "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/abc.json",
}
EN_US = {
    "lan": "en-US",
    "lan_doc": "英语（美国）",
    "type": 0,
    "subtitle_url": "//aisubtitle.hdslb.com/bfs/subtitle/en.json",
}


# ---------------------------------------------------------------------------
# 字幕类型判定
# ---------------------------------------------------------------------------

def test_is_ai_ignores_ai_type_and_ai_status():
    """ai_type / ai_status 两种字幕都是 0，不能作为判据。"""
    assert _is_ai(HUMAN_ZH) is False
    assert _is_ai(AI_ZH) is True


def test_is_ai_hits_on_any_single_discriminator():
    assert _is_ai({"lan": "zh-CN", "type": 1, "subtitle_url": ""}) is True
    assert _is_ai({"lan": "zh-CN", "type": 0, "subtitle_url": "//x/ai_subtitle/y.json"}) is True
    assert _is_ai({"lan": "zh-CN", "type": 0, "subtitle_url": "//x/bfs/subtitle/y.json"}) is False


def test_is_chinese_accepts_ai_zh_and_rejects_english():
    assert _is_chinese(HUMAN_ZH) is True
    assert _is_chinese(AI_ZH) is True
    assert _is_chinese(EN_US) is False


# ---------------------------------------------------------------------------
# 中文筛选：人工优先，简体优先
# ---------------------------------------------------------------------------

def test_pick_chinese_prefers_human_over_ai():
    选中 = pick_chinese_subtitle([AI_ZH, HUMAN_ZH])
    assert 选中 is HUMAN_ZH


def test_pick_chinese_returns_none_when_only_non_chinese():
    assert pick_chinese_subtitle([EN_US]) is None
    assert pick_chinese_subtitle([]) is None
    assert pick_chinese_subtitle(None) is None


def test_pick_chinese_prefers_simplified_variant():
    繁体 = {**HUMAN_ZH, "lan": "zh-Hant", "lan_doc": "中文（繁體）"}
    简体 = {**HUMAN_ZH, "lan": "zh-CN", "lan_doc": "中文（中国）"}
    assert pick_chinese_subtitle([繁体, 简体]) is 简体


def test_pick_chinese_falls_back_to_ai_when_no_human():
    选中 = pick_chinese_subtitle([EN_US, AI_ZH])
    assert 选中 is AI_ZH


# ---------------------------------------------------------------------------
# 块内拼装
# ---------------------------------------------------------------------------

def _unit(page: int, label: str, source_offset_sec: float = 0.0) -> dict:
    return {"page": page, "label": label, "split": source_offset_sec > 0,
            "source_offset_sec": source_offset_sec, "source_file": f"audio/{label}.m4a"}


def _seg(page: int, label: str, start_sec: float, duration_sec: float) -> dict:
    return {"page": page, "label": label, "start_sec": start_sec, "duration_sec": duration_sec}


def _sub(is_ai: bool, cues) -> dict:
    return {"is_ai": is_ai, "lan": "ai-zh" if is_ai else "zh-Hans", "lan_doc": "",
            "cues": [{"from": f, "content": c} for f, c in cues]}


def test_assemble_offsets_to_block_timeline_and_annotates():
    block = {
        "block_id": 3, "span": "P01-P02", "episodes": [1, 2],
        "units": [_unit(1, "P01"), _unit(2, "P02")],
        "segments": [_seg(1, "P01", 0.0, 100.0), _seg(2, "P02", 100.0, 100.0)],
    }
    result = assemble_block_transcript(block, {
        1: _sub(False, [(10.0, "甲")]),
        2: _sub(True, [(5.0, "乙")]),
    })
    assert result["ok"] is True
    assert result["kind_label"] == "人工上传 + AI 自动生成"
    assert "[00:10] 甲" in result["text"]      # P01 起点 0 + 10s
    assert "[01:45] 乙" in result["text"]      # P02 起点 100 + 5s
    assert "BLK03" in result["text"]
    assert "B 站字幕" in result["text"] and "非听音转录" in result["text"]


def test_assemble_labels_human_only():
    block = {
        "block_id": 1, "episodes": [1], "units": [_unit(1, "P01")],
        "segments": [_seg(1, "P01", 0.0, 60.0)],
    }
    result = assemble_block_transcript(block, {1: _sub(False, [(0.0, "正文")])})
    assert result["kind_label"] == "人工上传"


def test_assemble_missing_subtitle_blocks_whole_block():
    """任一成员分集缺中文字幕 → 整块不产出，避免静默漏内容。"""
    block = {
        "block_id": 2, "episodes": [1, 2], "units": [_unit(1, "P01"), _unit(2, "P02")],
        "segments": [_seg(1, "P01", 0.0, 100.0), _seg(2, "P02", 100.0, 100.0)],
    }
    result = assemble_block_transcript(block, {1: _sub(False, [(0.0, "甲")]), 2: None})
    assert result["ok"] is False
    assert result["missing_pages"] == [2]
    assert result["text"] == ""


def test_assemble_missing_segments_is_not_ok():
    result = assemble_block_transcript({"block_id": 9, "segments": []}, {})
    assert result["ok"] is False and result["text"] == ""


def test_assemble_split_legs_slice_their_own_source_range():
    """超长集的上/下腿各只取自己那一段，不互相覆盖、时间戳仍落回块内。"""
    上 = {
        "block_id": 1, "episodes": [12],
        "units": [_unit(12, "P12上", source_offset_sec=0.0)],
        "segments": [_seg(12, "P12上", 0.0, 600.0)],
    }
    下 = {
        "block_id": 2, "episodes": [12],
        "units": [_unit(12, "P12下", source_offset_sec=600.0)],
        "segments": [_seg(12, "P12下", 0.0, 600.0)],
    }
    subs = {12: _sub(False, [(0.0, "一"), (700.0, "二")])}

    上半 = assemble_block_transcript(上, subs)
    下半 = assemble_block_transcript(下, subs)

    assert "一" in 上半["text"] and "二" not in 上半["text"]
    assert "二" in 下半["text"] and "一" not in 下半["text"]
    assert "[01:40] 二" in 下半["text"]   # 700 − 600（源起点）+ 0（块内起点）


# ---------------------------------------------------------------------------
# 课程级稿件号（multi_page 分集不带 bvid，必须从 url 兜底）
# ---------------------------------------------------------------------------

def test_resolve_course_bvid_from_part_url_when_part_lacks_bvid():
    """实测形态：multi_page 分集只有 {page,title,cid,duration,url}。"""
    parts = [{"page": 1, "cid": 1, "url": "https://www.bilibili.com/video/BV1Wt4y1R7TU?p=1"}]
    assert resolve_course_bvid(parts) == "BV1Wt4y1R7TU"


def test_resolve_course_bvid_prefers_part_own_bvid():
    parts = [{"bvid": "BV0000000000"},
             {"url": "https://www.bilibili.com/video/BV1Wt4y1R7TU?p=1"}]
    assert resolve_course_bvid(parts) == "BV0000000000"


def test_resolve_course_bvid_empty_for_local_media():
    assert resolve_course_bvid([{"page": 1, "cid": 1, "url": "local://x"}]) == ""
    assert resolve_course_bvid([]) == ""


# ---------------------------------------------------------------------------
# 字幕覆盖度（字幕截断 = 与音频截断同一类静默漏内容）
# ---------------------------------------------------------------------------

def test_subtitle_coverage_flags_truncated_subtitle():
    """实测形态：14 秒占位视频的 AI 字幕只到 11.5 秒（82%）。"""
    cues = [{"from": 0.0, "content": "a"}, {"from": 11.5, "content": "b"}]
    assert subtitle_coverage(cues, 14.0) == pytest.approx(11.5 / 14.0)
    assert subtitle_coverage(cues, 14.0) < SUBTITLE_COVERAGE_MIN


def test_subtitle_coverage_accepts_full_length_subtitle():
    cues = [{"from": 0.0, "content": "a"}, {"from": 65.2, "content": "b"}]
    assert subtitle_coverage(cues, 67.0) >= SUBTITLE_COVERAGE_MIN


def test_subtitle_coverage_unknown_duration_does_not_block():
    assert subtitle_coverage([{"from": 3.0, "content": "a"}], 0.0) == 1.0
    assert subtitle_coverage([], 100.0) == 1.0


# ---------------------------------------------------------------------------
# 瞬时故障重试（CDN 对同一 URL 返回残缺正文：200 + 合法 JSON，内容只到开头）
# ---------------------------------------------------------------------------

def _假取(序列, 记录):
    """构造 `_取字幕一次` 替身：按序返回 (字幕 or None, 瞬时原因)。"""
    def _取(bvid, cid, sessdata, keys_file, duration_sec, title="", 课程术语=None):
        记录.append(1)
        return 序列[min(len(记录) - 1, len(序列) - 1)]
    return _取


def _monkeypatch_attempt(monkeypatch, 序列, 记录, 无延迟=True):
    monkeypatch.setattr(subtitles, "_取字幕一次", _假取(序列, 记录))
    if 无延迟:
        monkeypatch.setattr(subtitles.time, "sleep", lambda _s: None)


def test_retry_recovers_from_truncated_cdn_body(monkeypatch):
    """实测形态：连取 6 次仅 1 次完整。残缺时必须重试，而不是直接判"没字幕"。"""
    完整 = ({"is_ai": True, "lan": "ai-zh", "lan_doc": "中文", "coverage": 0.99,
             "cues": [{"from": 0.0, "content": "甲"}]}, "")
    记录: list = []
    _monkeypatch_attempt(monkeypatch, [(None, "覆盖不足（82%）"), (None, "正文为空"), 完整], 记录)

    诊断: dict = {}
    结果 = subtitles.fetch_episode_subtitle("BV1x", 1, 次数=4, 诊断=诊断)

    assert 结果 is not None and 结果["cues"] == [{"from": 0.0, "content": "甲"}]
    assert 结果["attempts"] == 3
    assert len(记录) == 3
    assert 诊断["reason"] == "" and 诊断["attempts"] == 3
    assert 诊断["coverage"] == 0.99


def test_retry_gives_up_after_configured_attempts(monkeypatch):
    """重试用尽才判不可用，且必须把原因交出去，让调用方区分两种"没有字幕"。"""
    记录: list = []
    _monkeypatch_attempt(monkeypatch, [(None, "地址为空")], 记录)

    诊断: dict = {}
    结果 = subtitles.fetch_episode_subtitle("BV1x", 1, 次数=3, 诊断=诊断)

    assert 结果 is None
    assert len(记录) == 3                      # 3 次都试过
    assert 诊断["reason"] == "地址为空"
    assert 诊断["attempts"] == 3


def test_empty_transient_reason_is_definitive(monkeypatch):
    """瞬时原因为空 = 调用方替身表达的"定论性无字幕"→ 一次即返回，不烧重试轮次。

    生产路径不再产生空原因（零轨改走 `REASON_NO_TRACK`，见下一条），
    这条只锁"空原因仍按定论处理"的契约，供替身与未来调用方使用。
    """
    记录: list = []
    _monkeypatch_attempt(monkeypatch, [(None, "")], 记录)

    诊断: dict = {}
    assert subtitles.fetch_episode_subtitle("BV1x", 1, 次数=4, 诊断=诊断) is None
    assert len(记录) == 1
    assert 诊断["reason"] == ""


def test_zero_track_response_is_retried_with_full_budget(monkeypatch):
    """零中文轨是坏抽签，与串台统一享有完整预算，不再提前过早收手。"""
    记录: list = []
    _monkeypatch_attempt(monkeypatch, [(None, subtitles.REASON_NO_TRACK)], 记录)

    诊断: dict = {}
    assert subtitles.fetch_episode_subtitle("BV1x", 1, 次数=8, 诊断=诊断) is None
    assert len(记录) == 8                                  # 统一走满指定的重试上限，不提前截断
    assert 诊断["reason"].startswith(subtitles.REASON_NO_TRACK)
    assert 诊断["kind"] == "transient"
    assert 诊断["dominant_reason"] == subtitles.REASON_NO_TRACK
    assert 诊断["failure_stats"][subtitles.REASON_NO_TRACK] == 8


def test_retry_survives_download_network_error(monkeypatch):
    """下载字幕正文遭遇 412 或网络抖动异常时，被捕获为瞬时重试，不杀掉整集预算。"""
    正确 = ({"is_ai": True, "lan": "ai-zh", "lan_doc": "", "coverage": 0.99,
             "trustworthy": True, "audit": {"band": "ok", "url_identity": True},
             "cues": [{"from": 0.0, "to": 2.0, "content": "正文"}]}, "")
    记录: list = []

    def _假取异常(bvid, cid, sessdata, keys_file, duration_sec, title="", 课程术语=None):
        记录.append(1)
        if len(记录) == 1:
            raise RuntimeError("请求失败（状态码 412）")
        return 正确

    monkeypatch.setattr(subtitles, "_取字幕一次", _假取异常)
    monkeypatch.setattr(subtitles.time, "sleep", lambda _s: None)

    诊断: dict = {}
    结果 = subtitles.fetch_episode_subtitle("BV1x", 1, 次数=5, 诊断=诊断)
    assert 结果 is not None
    assert 结果["attempts"] == 2
    assert len(记录) == 2


def test_diagnosis_tracks_full_history_and_identity_kind(monkeypatch):
    """诊断必须记录全程失败分布与主导原因，即使最后一轮是无轨也保留 identity 分类。"""
    记录: list = []
    _monkeypatch_attempt(monkeypatch, [
        (None, f"{subtitles.REASON_URL_MISMATCH}（文件名 123 而非 456）"),
        (None, f"{subtitles.REASON_URL_MISMATCH}（文件名 123 而非 456）"),
        (None, subtitles.REASON_NO_TRACK),
    ], 记录)

    诊断: dict = {}
    assert subtitles.fetch_episode_subtitle("BV1x", 1, 次数=3, 诊断=诊断) is None
    assert len(记录) == 3
    assert 诊断["kind"] == "identity"                      # 全程出现过 URL 不符，判定为 identity
    assert 诊断["dominant_reason"] == subtitles.REASON_URL_MISMATCH
    assert 诊断["failure_stats"][subtitles.REASON_URL_MISMATCH] == 2
    assert 诊断["failure_stats"][subtitles.REASON_NO_TRACK] == 1
    assert "共3轮" in 诊断["reason"]


def test_zero_track_recovers_on_a_later_draw(monkeypatch):
    """坏抽签之后抽到正常轨 → 必须被接受，这才是"正确字幕不遗漏"。"""
    正确 = ({"is_ai": True, "lan": "ai-zh", "lan_doc": "", "coverage": 0.99,
             "trustworthy": True, "audit": {"band": "ok", "url_identity": True},
             "cues": [{"from": 0.0, "to": 2.0, "content": "正文"}]}, "")
    记录: list = []
    _monkeypatch_attempt(monkeypatch, [
        (None, subtitles.REASON_NO_TRACK),
        (None, subtitles.REASON_NO_TRACK),
        正确,
    ], 记录)

    诊断: dict = {}
    结果 = subtitles.fetch_episode_subtitle("BV1x", 1, 诊断=诊断)
    assert 结果 is not None and 结果["attempts"] == 3 and len(记录) == 3
    assert 诊断["reason"] == ""


def test_retry_backoff_is_linear_and_only_between_attempts(monkeypatch):
    """退避 = 基数 × 轮次，且末轮不再睡（否则白等一个退避时长）。"""
    睡眠: list = []
    记录: list = []
    _monkeypatch_attempt(monkeypatch, [(None, "正文为空")], 记录, 无延迟=False)
    monkeypatch.setattr(subtitles.time, "sleep", 睡眠.append)

    assert subtitles.fetch_episode_subtitle("BV1x", 1, 次数=3) is None
    assert 睡眠 == pytest.approx([
        subtitles.SUBTITLE_RETRY_BACKOFF_SEC,
        subtitles.SUBTITLE_RETRY_BACKOFF_SEC * 2,
    ])


def test_attempts_default_reads_env(monkeypatch):
    # 上限保护：环境变量只能调低轮数，不能突破 MAX_SUBTITLE_ATTEMPTS
    monkeypatch.setenv(subtitles.ENV_SUBTITLE_ATTEMPTS, "99")
    assert subtitles._重试次数() == subtitles.MAX_SUBTITLE_ATTEMPTS
    monkeypatch.setenv(subtitles.ENV_SUBTITLE_ATTEMPTS, "2")
    assert subtitles._重试次数() == 2                 # 调低是允许的
    monkeypatch.setenv(subtitles.ENV_SUBTITLE_ATTEMPTS, "0")
    assert subtitles._重试次数() == 1                 # 下限保护，不允许 0 轮
    monkeypatch.setenv(subtitles.ENV_SUBTITLE_ATTEMPTS, "abc")
    assert subtitles._重试次数() == subtitles.DEFAULT_SUBTITLE_ATTEMPTS
    monkeypatch.delenv(subtitles.ENV_SUBTITLE_ATTEMPTS, raising=False)
    assert subtitles._重试次数() == subtitles.DEFAULT_SUBTITLE_ATTEMPTS


def test_retry_budget_matches_the_measured_hit_rate():
    """重取轮数按实测命中率定：单次抽中正确轨约 28%，每次抽签独立。

    3 轮只覆盖 63%（37% 的分集会被误判成"无字幕"并白烧听音兜底）；8 轮覆盖 93%。
    两类故障共用同一上限——URL 失配（约 45% 的坏抽签）属身份族，若给它更小的预算，
    它就会成为覆盖率的短板。
    """
    assert subtitles.MAX_SUBTITLE_ATTEMPTS == 10
    assert subtitles.DEFAULT_SUBTITLE_ATTEMPTS == 10
    assert subtitles.IDENTITY_RETRY_MAX == subtitles.MAX_SUBTITLE_ATTEMPTS
    # 最坏退避 0.5×(1+…+9) = 22.5 秒（末轮不睡，相比原先 67.5s 提速 3 倍）
    assert subtitles.SUBTITLE_RETRY_BACKOFF_SEC * sum(range(1, 10)) == 22.5
    p = 0.28
    cover3 = 1 - (1 - p) ** 3
    cover10 = 1 - (1 - p) ** 10
    assert cover3 == pytest.approx(0.627, abs=0.005)   # 3 轮 ≈ 62.7%
    assert cover10 == pytest.approx(0.963, abs=0.005)  # 10 轮 ≈ 96.3%
    assert cover10 - cover3 > 0.3


# ---------------------------------------------------------------------------
# URL 身份锚点：B 站 AI 字幕文件名内嵌 `aid+cid`，对不上即服务端串台
# ---------------------------------------------------------------------------

# 实测抓到的真实样本（2026-09-28）
_真AID, _真CID = 55114968, 96352452
_正确URL = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/prod/5511496896352452c86ff134b890/?auth_key=x"
_串台URL = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/prod/113272330197069262031278346582/?auth_key=x"


def test_url_identity_accepts_matching_ai_subtitle_file():
    assert subtitles.url_identity(_正确URL, _真AID, _真CID) is True


def test_url_identity_rejects_cross_video_file():
    """这是唯一能拦住"时长贴合"串台的判据——两条时长判据会放行这种错件。"""
    assert subtitles.url_identity(_串台URL, _真AID, _真CID) is False


def test_url_identity_requires_both_aid_and_cid():
    """只比 aid 会漏：aid 为 `55114968x` 的视频产生同前缀碰撞，拼上 cid 才唯一。"""
    assert subtitles.url_identity(_正确URL, _真AID, 99999999) is False


def test_url_identity_is_inconclusive_for_human_tracks_and_missing_aid():
    """人工 CC 不走 `/prod/`、拿不到 aid 时判据不适用——必须放行，不能拦。"""
    assert subtitles.url_identity("https://i0.hdslb.com/bfs/subtitle/abc.json", _真AID, _真CID) is None
    assert subtitles.url_identity(_正确URL, None, _真CID) is None
    assert subtitles.url_identity(_正确URL, "", _真CID) is None


def test_url_mismatch_is_classified_as_identity_failure():
    """URL 失配要进身份族，才能复用重试预算与 kind 分类。"""
    assert subtitles._身份故障(f"{subtitles.REASON_URL_MISMATCH}（文件名 11327233…）") is True


# ---------------------------------------------------------------------------
# 接受路径接线：锚点必须真的挂在 `_取字幕一次` 上，而不是只当一个纯函数存在
# ---------------------------------------------------------------------------

_本集时长 = 568.0


def _假播放器(monkeypatch, *, aid=_真AID, cid=_真CID, bvid="BV1H4411N7oD", url=_正确URL,
              lan="ai-zh", 轨类型=1):
    """替身到接口层：让 `_取字幕一次` 的接受路径真实跑一遍。

    `轨类型` 默认 1（实测 AI 轨的 `type`），人工轨传 0——`_is_ai` 的三条判据之一是
    `type == 1`，人工轨给它 1 会被判成 AI 并被套上越界上界。
    """
    monkeypatch.setattr(subtitles, "_取播放器数据", lambda *a, **k: {
        "code": 0,
        "data": {
            "aid": aid, "cid": cid, "bvid": bvid,
            "subtitle": {"subtitles": [
                {"lan": lan, "lan_doc": "中文", "subtitle_url": url, "type": 轨类型},
            ]},
        },
    })


def _假正文(monkeypatch, 末条秒, 首句="正文"):
    monkeypatch.setattr(subtitles, "_请求元数据", lambda *a, **k: {
        "body": [{"from": 0.0, "to": 2.0, "content": 首句},
                 {"from": max(0.0, 末条秒 - 2), "to": 末条秒, "content": "末句"}],
    })


def test_accept_path_rejects_duration_fitted_cross_video_file(monkeypatch):
    """**核心回归**：串台文件时长贴合本集时，两条时长判据都会放行它。

    实测样本：某次抽到的错件覆盖率 98.3%（476.62s/485s），落在 [0.9, 1.1] 内——
    只有 URL 锚点能拦住。这条测试锁死"锚点必须挂在接受路径上"。
    """
    _假播放器(monkeypatch, url=_串台URL)
    _假正文(monkeypatch, 末条秒=_本集时长 * 0.98, 首句="good good li加me")
    字幕, 原因 = subtitles._取字幕一次("BV1H4411N7oD", _真CID, None, None, _本集时长, "P01")
    assert 字幕 is None
    assert 原因.startswith(subtitles.REASON_URL_MISMATCH)
    # 前提校验：这份错件的时长确实在时长判据的放行带内，否则本测试证明不了锚点的必要性
    assert subtitles.SUBTITLE_COVERAGE_MIN <= 0.98 <= subtitles.SUBTITLE_OVERRUN_MAX


def test_accept_path_takes_matching_file(monkeypatch):
    _假播放器(monkeypatch, url=_正确URL)
    _假正文(monkeypatch, 末条秒=_本集时长 * 0.997, 首句="我们这门课的名字呢叫做数据结构基础")
    字幕, 原因 = subtitles._取字幕一次("BV1H4411N7oD", _真CID, None, None, _本集时长, "P01")
    assert 原因 == ""
    assert 字幕 is not None and 字幕["audit"]["url_identity"] is True


def test_accept_path_leaves_human_track_to_time_checks(monkeypatch):
    """人工 CC 不走 `/prod/`，锚点不适用 → 必须靠时长判据放行，不能被误杀。"""
    _假播放器(monkeypatch, url="https://i0.hdslb.com/bfs/subtitle/abc.json", lan="zh-CN", 轨类型=0)
    _假正文(monkeypatch, 末条秒=_本集时长 * 0.99)
    字幕, 原因 = subtitles._取字幕一次("BV1H4411N7oD", _真CID, None, None, _本集时长, "P01")
    assert 原因 == "" and 字幕 is not None
    assert 字幕["audit"]["url_identity"] is None
    assert 字幕["is_ai"] is False


def test_accept_path_still_rejects_overrun_and_truncated(monkeypatch):
    """锚点上线的同时，原有两条时长判据不能被绕过。"""
    _假播放器(monkeypatch, url=_正确URL)
    _假正文(monkeypatch, 末条秒=_本集时长 * 3.02)
    字幕, 原因 = subtitles._取字幕一次("BV1H4411N7oD", _真CID, None, None, _本集时长, "P01")
    assert 字幕 is None and 原因.startswith(subtitles.REASON_TIME_OVERRUN)

    _假正文(monkeypatch, 末条秒=_本集时长 * 0.5)
    字幕, 原因 = subtitles._取字幕一次("BV1H4411N7oD", _真CID, None, None, _本集时长, "P01")
    assert 字幕 is None and 原因.startswith("覆盖不足")


def test_accept_path_fails_open_without_aid(monkeypatch):
    """响应缺 aid 时锚点判据不适用 → 放行给时长判据，不能被字段缺失阻塞。"""
    _假播放器(monkeypatch, aid=None, url=_串台URL)
    _假正文(monkeypatch, 末条秒=_本集时长 * 0.98)
    字幕, 原因 = subtitles._取字幕一次("BV1H4411N7oD", _真CID, None, None, _本集时长, "P01")
    assert 字幕 is not None and 字幕["audit"]["url_identity"] is None


def test_wbi_auto_sticky_downgrade_on_first_failure(monkeypatch):
    """**粘性降级**：auto 模式下首集若遇 412，必须一次性降级并记忆，后续分集直连老端点。

    避免 145 集每集都在 WBI 上白白重试 14 秒（全课程立省 33+ 分钟）。
    """
    subtitles.reset_wbi_degraded()
    monkeypatch.delenv(subtitles.ENV_SUBTITLE_ENDPOINT, raising=False)
    请求记录 = []

    def _假请求元数据(url, headers, 超时=15, 重试=True):
        请求记录.append({"url": url, "重试": 重试})
        if "wbi" in url:
            raise RuntimeError("[风控]元数据接口被风控拦截（412）")
        return {"code": 0, "data": {"cid": 1, "bvid": "BV1", "subtitle": {"subtitles": []}}}

    monkeypatch.setattr(subtitles, "_请求元数据", _假请求元数据)
    # 第 1 集：探测 WBI，瞬间捕获 412，降级并记入粘性标记
    subtitles._取播放器数据("BV1", 1, None, None)
    assert len(请求记录) == 2
    assert "wbi" in 请求记录[0]["url"] and 请求记录[0]["重试"] is False  # 探测禁用重试
    assert "player/v2" in 请求记录[1]["url"]

    # 第 2 集：粘性标记生效，直接打老端点，不再尝试 WBI
    请求记录.clear()
    subtitles._取播放器数据("BV1", 2, None, None)
    assert len(请求记录) == 1
    assert "player/v2" in 请求记录[0]["url"]
    assert "wbi" not in 请求记录[0]["url"]
    subtitles.reset_wbi_degraded()




# ---------------------------------------------------------------------------
# 时长带：截断（下限）与越界（上限，仅 ai 轨）
# ---------------------------------------------------------------------------

def test_time_band_marks_truncation_and_overrun():
    assert subtitle_time_band(0.5, True) == "short"
    assert subtitle_time_band(0.99, True) == "ok"
    assert subtitle_time_band(1.05, True) == "warn"       # 轻微越界：采用但标注不可信
    assert subtitle_time_band(1.5, True) == "overrun"     # 判串台
    # 时长未知时覆盖度返回 1.0（无法判断就不拦）
    assert subtitle_time_band(subtitle_coverage([{"from": 3.0}], 0.0), True) == "ok"


def test_time_band_upper_bound_is_ai_only():
    """上界只对 ai 轨生效：人工 CC 可能有片尾冗余，不因此判串台。"""
    assert subtitle_time_band(1.5, False) == "ok"
    assert subtitle_time_band(0.5, False) == "short"


@pytest.mark.parametrize("entry", FIXTURE["overrun"], ids=lambda e: f"P{e['page']}")
def test_overrun_samples_are_rejected_by_upper_bound(entry):
    """实测越界样本：「分体水冷」2937/973、「惠普维修」357/248。"""
    覆盖 = subtitle_coverage(_cue_list(entry), entry["duration"])
    assert 覆盖 > SUBTITLE_OVERRUN_MAX
    assert subtitle_time_band(覆盖, True) == "overrun"


def test_coverage_prefers_to_over_from():
    """末端以 to 为准：只有 from 的旧正文仍按 from 兜底。"""
    assert subtitle_coverage([{"from": 10.0, "to": 12.0}], 20.0) == pytest.approx(0.6)
    assert subtitle_coverage([{"from": 10.0, "to": 0.0}], 20.0) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 身份类故障的重试预算（实测：同一 cid 重取会随机拿到正确内容，重试有实效）
# ---------------------------------------------------------------------------

def test_identity_failure_uses_its_own_retry_budget(monkeypatch):
    记录: list = []
    # 用「时长越界」：标题术语身份判据已删除，它是接受路径上**仅存**的身份类原因。
    # 轮数按常量推导而非写死数字——常量从 3 抬到 12 后，固定写 8 会让封顶不再生效。
    _monkeypatch_attempt(monkeypatch, [(None, "时长越界（末条 2937s / 时长 973s = 302%）")], 记录)
    诊断: dict = {}
    assert subtitles.fetch_episode_subtitle("BV1x", 1, 次数=IDENTITY_RETRY_MAX + 5, 诊断=诊断) is None
    assert len(记录) == IDENTITY_RETRY_MAX      # 身份类按自己的预算收手，早于总轮数
    assert 诊断["reason"].startswith("时长越界")
    assert 诊断["kind"] == "identity"


def test_retry_accepts_correct_content_from_a_later_round(monkeypatch):
    """实测 P91：三次里有一次拿回正确字幕（课程片头+正文）。后一轮的成功要被接受。"""
    正确 = ({"is_ai": True, "lan": "ai-zh", "lan_doc": "", "coverage": 1.0,
             "cues": [{"from": 0.0, "to": 2.0, "content": "应试还得技术流"}], "trustworthy": True,
             "audit": {"title_hits": 3}}, "")
    记录: list = []
    _monkeypatch_attempt(monkeypatch, [
        (None, "内容与分集标题不符（走私犯罪 命中 0 次）"),
        (None, "时长越界（末条 2937s / 时长 973s = 302%）"),
        正确,
    ], 记录)

    诊断: dict = {}
    结果 = subtitles.fetch_episode_subtitle("BV1x", 1, 次数=4, 诊断=诊断)
    assert 结果 is not None and 结果["attempts"] == 3 and len(记录) == 3
    assert 诊断["reason"] == "" and 诊断["coverage"] == 1.0


def test_time_band_overrun_reason_is_classified_as_identity():
    assert subtitles._身份故障("时长越界（末条 2937s / 时长 973s = 302%）") is True
    assert subtitles._身份故障("内容与分集标题不符（走私犯罪 命中 0 次）") is True
    assert subtitles._身份故障("覆盖不足（82%）") is False
    assert subtitles._身份故障("地址为空") is False
    assert subtitles._身份故障("") is False


def test_chinese_candidates_keep_preference_order():
    """候选轨逐个验身份：人工优先、简体优先的顺序不变。"""
    繁体 = {**HUMAN_ZH, "lan": "zh-Hant", "lan_doc": "中文（繁體）"}
    assert [c["lan"] for c in subtitles.chinese_subtitle_candidates([EN_US, AI_ZH, 繁体, HUMAN_ZH])] == \
        ["zh-Hans", "zh-Hant", "ai-zh"]
    assert subtitles.chinese_subtitle_candidates([EN_US]) == []


# ---------------------------------------------------------------------------
# 抬头核验行（取回结论要写在产物里，人工复核才有据可查）
# ---------------------------------------------------------------------------

def _带核验(is_ai: bool, coverage: float, trustworthy: bool, attempts: int) -> dict:
    return {**_sub(is_ai, [(1.0, "正文")]), "coverage": coverage, "trustworthy": trustworthy,
            "attempts": attempts, "audit": {"band": "ok"}}


def _双页块() -> dict:
    return {
        "block_id": 5, "span": "P90-P91", "episodes": [90, 91],
        "units": [_unit(90, "P90"), _unit(91, "P91")],
        "segments": [_seg(90, "P90", 0.0, 100.0), _seg(91, "P91", 100.0, 100.0)],
    }


def test_assemble_verification_line_reports_coverage_and_rounds():
    text = assemble_block_transcript(_双页块(), {
        90: _带核验(True, 0.99, True, 2),
        91: _带核验(True, 0.97, True, 1),
    })["text"]
    assert "核验：时间轴判据通过（覆盖 97.0%–99.0%）" in text
    assert "取回 1–2 轮" in text
    # 标题术语身份判据已删除，抬头不得再出现"标题身份通过"
    assert "标题身份" not in text and "术语命中" not in text


def test_assemble_verification_line_flags_untrustworthy_page():
    text = assemble_block_transcript(_双页块(), {
        90: _带核验(True, 0.99, True, 1),
        91: _带核验(True, 1.05, False, 3),
    })["text"]
    assert "⚠ 核验" in text and "P91" in text and "不可信来源" in text


# ---------------------------------------------------------------------------
# 按集兜底：部分成功装配 + 与听音补录稿合并
# ---------------------------------------------------------------------------

def test_partial_assembly_keeps_available_pages():
    """`allow_partial=True`：取到字幕的集照常拼装，缺的集只记账、不整块作废。

    这是「只对失败分集转录」的地基——历史上任缺一集就整块 `ok=False`、`text=""`。
    """
    结果 = assemble_block_transcript(_双页块(), {90: _带核验(True, 0.99, True, 1), 91: None},
                                    allow_partial=True)
    assert 结果["ok"] is True
    assert 结果["missing_pages"] == [91]
    assert 结果["page_sources"] == {90: "subtitle", 91: "missing"}
    assert "正文" in 结果["text"]                 # P90 的内容在
    assert "不完整" in 结果["text"] and "P91" in 结果["text"]   # 抬头如实声明缺口


def test_partial_assembly_is_opt_in():
    """默认仍是整块语义：任缺一集就 ok=False、text=""（历史行为不能悄悄变）。"""
    结果 = assemble_block_transcript(_双页块(), {90: _带核验(True, 0.99, True, 1), 91: None})
    assert 结果["ok"] is False and 结果["text"] == "" and 结果["missing_pages"] == [91]


def test_merge_orders_pages_and_declares_per_page_sources():
    """合并稿按块内顺序排列，抬头逐集声明来源——这是"只转录失败集"的成品。"""
    部分 = assemble_block_transcript(_双页块(), {90: _带核验(True, 0.99, True, 1), 91: None},
                                     allow_partial=True)
    合并 = merge_block_transcript(_双页块(), 部分, {91: "第九十一集的听音转录正文"})
    assert 合并["ok"] is True and 合并["missing_pages"] == []
    assert 合并["page_sources"] == {90: "subtitle", 91: "audio"}
    # 顺序：P90 的字幕在先，P91 的听音在后
    assert 合并["text"].index("正文") < 合并["text"].index("第九十一集")
    assert "混合（B 站字幕 + 听音转录）" in 合并["text"]
    assert "P91" in 合并["text"]


def test_merge_is_incomplete_until_every_missing_page_arrives():
    """补录还没回来时，合并稿必须标为不完整，不能冒充成品。"""
    部分 = assemble_block_transcript(_双页块(), {90: _带核验(True, 0.99, True, 1), 91: None},
                                     allow_partial=True)
    合并 = merge_block_transcript(_双页块(), 部分, {})
    assert 合并["ok"] is False and 合并["missing_pages"] == [91]
    assert "仍缺分集" in 合并["text"]


def test_merged_transcript_is_classified_as_mixed(tmp_path):
    """混合稿必须被判成 `mixed`：`--force` 时它的听音段无法用字幕重建，不能被覆盖。"""
    部分 = assemble_block_transcript(_双页块(), {90: _带核验(True, 0.99, True, 1), 91: None},
                                     allow_partial=True)
    合并 = merge_block_transcript(_双页块(), 部分, {91: "听音正文"})
    路径 = tmp_path / "BLK05_P90-P91_逐字稿.md"
    路径.write_text(合并["text"], encoding="utf-8")
    assert subtitles.transcript_source(路径) == "mixed"


def test_single_source_headers_still_classify_as_before(tmp_path):
    """混合判定不能把原有的两种单一来源判反。"""
    纯字幕 = tmp_path / "sub.md"
    纯字幕.write_text("# x\n\n> 来源：**B 站字幕（中文·AI 自动生成）**——非听音转录，由平台字幕直接拼装。\n",
                      encoding="utf-8")
    assert subtitles.transcript_source(纯字幕) == "subtitle"
    纯听音 = tmp_path / "aud.md"
    纯听音.write_text("# x\n\n> 来源：**听音转录**——非平台字幕，由讲师原声转录\n", encoding="utf-8")
    assert subtitles.transcript_source(纯听音) == "audio"





# ---------------------------------------------------------------------------
# 字幕阶段服务：被判不可用的分集留原因、整块转音频、标题照传
# ---------------------------------------------------------------------------

def _服务块() -> dict:
    return {
        "block_id": 1, "span": "P01-P02", "episodes": [1, 2], "title": "测试块",
        "units": [_unit(1, "P01"), _unit(2, "P02")],
        "segments": [_seg(1, "P01", 0.0, 100.0), _seg(2, "P02", 100.0, 100.0)],
    }


def test_subtitle_service_records_reason_and_routes_block_to_audio(tmp_path, monkeypatch):
    from src.core.subtitle_service import SubtitleService
    from src.core.workspace import TaskWorkspace

    ws = TaskWorkspace("t-case", base_dir=tmp_path)
    plan = {
        "source": {"parts": [
            {"page": 1, "cid": 11, "duration": 100.0, "bvid": "BV1r8mxYNECZ",
             "title": "第01讲 第01节：刑法的解释"},
            {"page": 2, "cid": 22, "duration": 100.0, "bvid": "BV1r8mxYNECZ",
             "title": "第01讲 第02节：走私犯罪"},
        ]},
        "blocks": [_服务块()],
    }
    调用: list = []

    def _假取(bvid, cid, sessdata=None, keys_file=None, duration_sec=0.0, title="", 课程术语=None, 诊断=None):
        调用.append({"cid": cid, "title": title, "duration": duration_sec})
        if cid == 11:
            return {"is_ai": True, "lan": "ai-zh", "lan_doc": "", "coverage": 0.99, "trustworthy": True,
                    "audit": {"title_hits": 9, "band": "ok"}, "cues": [{"from": 0.0, "to": 2.0, "content": "甲"}]}
        if 诊断 is not None:
            诊断.update(reason="内容与分集标题不符（走私犯罪 0 次）", attempts=3, coverage=0.0, kind="identity")
        return None

    monkeypatch.setattr(subtitles, "fetch_episode_subtitle", _假取)
    结果 = SubtitleService.run(ws, {}, plan, sessdata="x", force=True)

    assert [c["title"] for c in 调用] == ["第01讲 第01节：刑法的解释", "第01讲 第02节：走私犯罪"]
    assert 结果["needs_audio"] == [1] and 结果["subtitle_ready"] == []
    assert 结果["unavailable"] == [{"page": 2, "title": "第01讲 第02节：走私犯罪",
                                    "reason": "内容与分集标题不符（走私犯罪 0 次）",
                                    "kind": "identity", "attempts": 3}]
    assert not (ws.subtitles_dir / "BLK01_P01-P02_逐字稿.md").exists()   # 有页判不可用 → 整块不落盘


def test_subtitle_service_writes_transcript_when_all_pages_pass(tmp_path, monkeypatch):
    from src.core.subtitle_service import SubtitleService
    from src.core.workspace import TaskWorkspace

    ws = TaskWorkspace("t-case2", base_dir=tmp_path)
    plan = {
        "source": {"parts": [
            {"page": 1, "cid": 11, "duration": 100.0, "bvid": "BV1r8mxYNECZ",
             "title": "第01讲 第01节：刑法的解释"},
            {"page": 2, "cid": 22, "duration": 100.0, "bvid": "BV1r8mxYNECZ",
             "title": "第01讲 第02节：走私犯罪"},
        ]},
        "blocks": [_服务块()],
    }

    def _假取(bvid, cid, sessdata=None, keys_file=None, duration_sec=0.0, title="", 课程术语=None, 诊断=None):
        return {"is_ai": True, "lan": "ai-zh", "lan_doc": "", "coverage": 0.99, "trustworthy": True,
                "attempts": 1, "audit": {"band": "ok"},
                "cues": [{"from": 0.0, "to": 2.0, "content": "正文"}]}

    monkeypatch.setattr(subtitles, "fetch_episode_subtitle", _假取)
    结果 = SubtitleService.run(ws, {}, plan, sessdata="x", force=True)

    assert 结果["subtitle_ready"] == [1] and 结果["needs_audio"] == []
    正文 = (ws.subtitles_dir / "BLK01_P01-P02_逐字稿.md").read_text(encoding="utf-8")
    assert "核验：时间轴判据通过（覆盖 99.0%–99.0%）" in 正文 and "取回 1–1 轮" in 正文


# ---------------------------------------------------------------------------
# CLI 入口已收敛到 pipeline；字幕底层与重试契约由本文件纯函数测试覆盖。

# 换行常量：测试正文里要拼多行文本，直接用 chr(10) 免得反斜杠被各层转义吃掉。
NL = chr(10)
NL2 = chr(10) * 2

# ---------------------------------------------------------------------------
# 来源方向性：`--force` 不得把听音稿降级成字幕稿
# ---------------------------------------------------------------------------

AUDIO_TRANSCRIPT = (
    "好，今天我要講的是用於編程和軟件開發的 agent。" + NL2 +
    "在座各位應該很多人，甚至所有人都在用 coding agent。它們底層的功能是什麼，"
    "我基本不用多講。但我想聊聊我們怎麼構建 coding agent。" + NL
)

SUBTITLE_TRANSCRIPT = (
    "# BLK01 P01-P02 块级逐字稿" + NL2 +
    "> 来源：**B 站字幕（中文·AI 自动生成）**——非听音转录，由平台字幕直接拼装。" + NL +
    "> 字幕由平台生成或上传，可能存在识别错误、断句与专业术语偏差。" + NL2 +
    "[00:00] 好今天我要讲的是用于编程和软件开发的agent" + NL +
    "[00:05] 在座各位应该很多人都用coding agent" + NL
)


@pytest.mark.parametrize("text, expected", [
    (AUDIO_TRANSCRIPT, "audio"),                      # 无抬头、无时间戳 → 听音稿
    (SUBTITLE_TRANSCRIPT, "subtitle"),                # 有来源抬头 + 时间戳 → 字幕稿
    ("# BLK01 P01 块级逐字稿" + NL2 + "[00:01] 只有时间戳也能认出字幕稿" + NL, "subtitle"),
    ("> 来源：**听音转录**——非平台字幕" + NL2 + "正文。" + NL, "audio"),
    ("", "unknown"),
])
def test_transcript_source_detection(tmp_path, text, expected):
    """来源判定是防降级的前提：判错了要么白拦、要么放过降级。"""
    path = tmp_path / "t.md"
    path.write_text(text, encoding="utf-8")
    assert subtitles.transcript_source(path) == expected


def test_transcript_source_missing_file_is_unknown(tmp_path):
    assert subtitles.transcript_source(tmp_path / "nope.md") == "unknown"


def _降级用例(tmp_path, monkeypatch, 已有: str, **run_kw):
    from src.core.subtitle_service import SubtitleService
    from src.core.workspace import TaskWorkspace

    ws = TaskWorkspace("t-downgrade", base_dir=tmp_path)
    plan = {
        "source": {"parts": [
            {"page": 1, "cid": 11, "duration": 100.0, "bvid": "BV1r8mxYNECZ",
             "title": "第01讲 第01节：刑法的解释"},
            {"page": 2, "cid": 22, "duration": 100.0, "bvid": "BV1r8mxYNECZ",
             "title": "第01讲 第02节：走私犯罪"},
        ]},
        "blocks": [_服务块()],
    }
    target = ws.subtitles_dir / "BLK01_P01-P02_逐字稿.md"
    ws.subtitles_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(已有, encoding="utf-8")

    def _假取(bvid, cid, sessdata=None, keys_file=None, duration_sec=0.0, title="", 课程术语=None, 诊断=None):
        return {"is_ai": True, "lan": "ai-zh", "lan_doc": "", "coverage": 0.99, "trustworthy": True,
                "attempts": 1, "audit": {"title_hits": 9, "band": "ok", "title_terms": 1},
                "cues": [{"from": 0.0, "to": 2.0, "content": "字幕正文"}]}

    monkeypatch.setattr(subtitles, "fetch_episode_subtitle", _假取)
    结果 = SubtitleService.run(ws, {}, plan, sessdata="x", force=True, **run_kw)
    return 结果, target


def test_force_refuses_to_downgrade_audio_transcript(tmp_path, monkeypatch):
    """`--force` 遇到听音稿必须拒绝覆盖——实测发生过不可逆降级（45721 → 53923 字节，
    繁体成段变成简体逐句时间戳）。"""
    结果, target = _降级用例(tmp_path, monkeypatch, AUDIO_TRANSCRIPT)
    assert target.read_text(encoding="utf-8") == AUDIO_TRANSCRIPT, "听音稿被字幕稿覆盖了"
    assert [项["block_id"] for 项 in 结果["kept_audio"]] == [1]
    assert "allow_downgrade" not in 结果["kept_audio"][0]["reason"].replace("--allow-downgrade", "")
    assert 结果["subtitle_ready"] == [1]


def test_force_overwrites_when_downgrade_explicitly_allowed(tmp_path, monkeypatch):
    """显式 `--allow-downgrade` 才放手——这是唯一的逃生门。"""
    结果, target = _降级用例(tmp_path, monkeypatch, AUDIO_TRANSCRIPT, allow_downgrade=True)
    assert "字幕正文" in target.read_text(encoding="utf-8"), "显式授权后仍未覆盖"
    assert 结果["kept_audio"] == []
    assert 结果["written"], "显式授权后应正常写入"


def test_force_replaces_subtitle_transcript_without_asking(tmp_path, monkeypatch):
    """反方向不受限：已有的是**字幕稿**时，重取照常覆盖——那是同质替换，不是降级。
    否则重取（修串台的主要手段）就被这条护栏堵死了。"""
    结果, target = _降级用例(tmp_path, monkeypatch, SUBTITLE_TRANSCRIPT)
    assert "字幕正文" in target.read_text(encoding="utf-8")
    assert 结果["kept_audio"] == []


# ---------------------------------------------------------------------------
# 端到端：部分字幕稿 → 按集补录 → 合并成最终块级逐字稿
#   （这是「只对失败分集转录」的完整链路，必须整体跑通而不只是各函数单测）
# ---------------------------------------------------------------------------

def _e2e_plan() -> dict:
    return {
        "source": {"parts": [
            {"page": 1, "cid": 11, "duration": 100.0, "bvid": "BV1x", "title": "第01讲 开场"},
            {"page": 2, "cid": 22, "duration": 100.0, "bvid": "BV1x", "title": "第02讲 缺字幕集"},
        ]},
        "blocks": [_服务块()],
    }


def _e2e_取字幕(monkeypatch, cid_to_ok=(11,)):
    def _假取(bvid, cid, sessdata=None, keys_file=None, duration_sec=0.0, title="", 课程术语=None, 诊断=None):
        if int(cid) in cid_to_ok:
            return {"is_ai": True, "lan": "ai-zh", "lan_doc": "", "coverage": 0.99,
                    "trustworthy": True, "audit": {"band": "ok", "url_identity": True},
                    "cues": [{"from": 0.0, "to": 2.0, "content": f"字幕内容{cid}"}]}
        return None
    monkeypatch.setattr(subtitles, "fetch_episode_subtitle", _假取)


def test_end_to_end_only_missing_episode_gets_transcribed(tmp_path, monkeypatch):
    """一集有字幕、一集没有 → 只给**缺的那一集**派补录稿，而不是整块重听。"""
    from src.core.subtitle_service import SubtitleService
    from src.core.workspace import TaskWorkspace

    ws = TaskWorkspace("t-e2e", base_dir=tmp_path)
    _e2e_取字幕(monkeypatch, cid_to_ok=(11,))
    结果 = SubtitleService.run(ws, {}, _e2e_plan(), sessdata="x", force=True)

    # 块未完成，但已产出可用的部分稿 + 缺页状态
    最终 = TaskWorkspace.block_path(ws, _服务块())
    assert not 最终.exists(), "部分稿不得写到最终名——否则下游会误判该块已完工"
    部分 = TaskWorkspace.partial_transcript_path(ws, _服务块())
    assert 部分.exists() and "字幕内容11" in 部分.read_text(encoding="utf-8")
    assert 结果["partial"][0]["missing_pages"] == [2]
    assert 结果["partial"][0]["ready_pages"] == [1]

    # 缺页状态是「哪几集待补录」的权威来源
    状态 = TaskWorkspace.partial_state_path(ws, _服务块())
    assert json.loads(状态.read_text(encoding="utf-8"))["missing_pages"] == [2]

    # 补录稿未落盘前不合并
    from src.pipeline import PipelineCoordinator
    assert PipelineCoordinator._merge_completed_blocks(ws, [_服务块()], {1: [2]}) == []

    # 补录稿落盘后合并：最终稿出现，且被判为 mixed
    补录 = TaskWorkspace.page_transcript_path(ws, _服务块(), 2)
    补录.write_text("> 来源：**听音转录**——非平台字幕\n缺集的原声转录正文", encoding="utf-8")
    产出 = PipelineCoordinator._merge_completed_blocks(ws, [_服务块()], {1: [2]})
    assert len(产出) == 1 and 产出[0]["audio_pages"] == [2]
    assert 最终.exists()
    正文 = 最终.read_text(encoding="utf-8")
    assert "字幕内容11" in 正文 and "缺集的原声转录正文" in 正文
    assert subtitles.transcript_source(最终) == "mixed"
    # 原料清理：补录稿与状态文件移除，避免下一轮重复派发
    assert not 补录.exists() and not 状态.exists()


def test_partial_is_not_rescanned_as_block_transcript(tmp_path, monkeypatch):
    """部分稿/补录稿都不该被「最终稿存在即整块完成」判定为已转录。"""
    from src.core.workspace import TaskWorkspace
    ws = TaskWorkspace("t-e2e2", base_dir=tmp_path)
    TaskWorkspace.partial_transcript_path(ws, _服务块()).parent.mkdir(parents=True, exist_ok=True)
    TaskWorkspace.partial_transcript_path(ws, _服务块()).write_text("部分稿", encoding="utf-8")
    TaskWorkspace.page_transcript_path(ws, _服务块(), 2).write_text("补录稿", encoding="utf-8")
    assert not TaskWorkspace.block_path(ws, _服务块()).exists()


def test_rerun_reuses_already_fetched_subtitles(tmp_path, monkeypatch):
    """**续跑**：第二遍跑只重取缺的那几集，已取到的不再抽签。

    没有这条时，每次重跑都要把块内全部分集重新向 B站 取一遍（10 轮重试 + 最坏
    67.5s 退避），一中止就是二十多分钟。sidecar 里存着原始 cues，所以能直接复用。
    """
    from src.core.subtitle_service import SubtitleService
    from src.core.workspace import TaskWorkspace

    ws = TaskWorkspace("t-resume", base_dir=tmp_path)
    取过: list = []

    def _假取(bvid, cid, sessdata=None, keys_file=None, duration_sec=0.0, title="", 课程术语=None, 诊断=None):
        取过.append(int(cid))
        if int(cid) == 11:
            return {"is_ai": True, "lan": "ai-zh", "lan_doc": "", "coverage": 0.99,
                    "trustworthy": True, "audit": {"band": "ok", "url_identity": True},
                    "cues": [{"from": 0.0, "to": 2.0, "content": "甲集字幕"}]}
        return None

    monkeypatch.setattr(subtitles, "fetch_episode_subtitle", _假取)
    plan = _e2e_plan()
    SubtitleService.run(ws, {}, plan, sessdata="x", force=True)
    assert sorted(取过) == [11, 22], "第一遍应两集都取"

    取过.clear()
    SubtitleService.run(ws, {}, plan, sessdata="x")
    assert 取过 == [22], f"第二遍只应重取缺的那一集，实际取了 {取过}"
    # 复用后部分稿仍应完整保留已取到的那集
    部分 = TaskWorkspace.partial_transcript_path(ws, _服务块()).read_text(encoding="utf-8")
    assert "甲集字幕" in 部分


def test_force_still_refetches_everything(tmp_path, monkeypatch):
    """`--force` 的语义不变：sidecar 不得被当成缓存把重取短路掉。"""
    from src.core.subtitle_service import SubtitleService
    from src.core.workspace import TaskWorkspace

    ws = TaskWorkspace("t-force", base_dir=tmp_path)
    取过: list = []

    def _假取(bvid, cid, **kw):
        取过.append(int(cid))
        return {"is_ai": True, "lan": "ai-zh", "lan_doc": "", "coverage": 0.99,
                "trustworthy": True, "audit": {"band": "ok", "url_identity": True},
                "cues": [{"from": 0.0, "to": 2.0, "content": "甲集字幕"}]}

    monkeypatch.setattr(subtitles, "fetch_episode_subtitle", _假取)
    plan = _e2e_plan()
    SubtitleService.run(ws, {}, plan, sessdata="x", force=True)
    assert TaskWorkspace.block_path(ws, _服务块()).exists()
    取过.clear()
    SubtitleService.run(ws, {}, plan, sessdata="x", force=True)
    assert sorted(取过) == [11, 22], "force=True 时应重取块内全部分集"
