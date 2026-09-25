# -*- coding: utf-8 -*-
"""B 站字幕 → 块级逐字稿：字幕类型判定 + 中文筛选 + 块内拼装。

本模块**完全离线**：只测纯函数（`_is_ai` / `pick_chinese_subtitle` / `assemble_block_transcript`）
与两条 CLI 护栏（无登录态、无块清单）。不发任何网络请求，也不依赖开发机的产物目录。
"""

from __future__ import annotations

import pytest

from src.core import subtitles
from src.core.subtitles import (
    SUBTITLE_COVERAGE_MIN,
    _is_ai,
    _is_chinese,
    assemble_block_transcript,
    pick_chinese_subtitle,
    resolve_course_bvid,
    subtitle_coverage,
)

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
    def _取(bvid, cid, sessdata, keys_file, duration_sec):
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


def test_no_chinese_subtitle_is_not_retried(monkeypatch):
    """确实没有中文字幕（瞬时原因为空）→ 一次即返回，不烧重试轮次。"""
    记录: list = []
    _monkeypatch_attempt(monkeypatch, [(None, "")], 记录)

    诊断: dict = {}
    assert subtitles.fetch_episode_subtitle("BV1x", 1, 次数=4, 诊断=诊断) is None
    assert len(记录) == 1
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
    monkeypatch.setenv(subtitles.ENV_SUBTITLE_ATTEMPTS, "7")
    assert subtitles._重试次数() == 7
    monkeypatch.setenv(subtitles.ENV_SUBTITLE_ATTEMPTS, "0")
    assert subtitles._重试次数() == 1              # 下限保护，不允许 0 轮
    monkeypatch.setenv(subtitles.ENV_SUBTITLE_ATTEMPTS, "abc")
    assert subtitles._重试次数() == subtitles.DEFAULT_SUBTITLE_ATTEMPTS
    monkeypatch.delenv(subtitles.ENV_SUBTITLE_ATTEMPTS, raising=False)
    assert subtitles._重试次数() == subtitles.DEFAULT_SUBTITLE_ATTEMPTS


# ---------------------------------------------------------------------------
# CLI 入口已收敛到 pipeline；字幕底层与重试契约由本文件纯函数测试覆盖。
